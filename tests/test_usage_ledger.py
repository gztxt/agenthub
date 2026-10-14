#!/usr/bin/env python3
"""L0 hermetic：CCR usage 账本只读门面（src/usage_ledger.py，P0-2）。

钉死四件事：
  ① **只读**——`_connect_ro` 打开的连接**写不进去**（账本是 CCR 生命线资产）；
  ② **cost 不许静默当 0**——`COALESCE(cost_usd,0)` 求和必须同时回 `null_cost`
     与 `cost_sources`（含空 `""` 桶）；免费模型实测 `cost_usd=NULL`；
  ③ **`logical_model` 不许当聚合维度**——2026-09-07 判例：账本逻辑名不是客户端 ID；
  ④ **fail-closed**——账本缺失/损坏回 `ok:False`，**不是**回一堆 0 当成功。

时间边界还有一条隐坑专测：账本存 `…Z`，而 Python 的 `isoformat()` 给 `+00:00`
（`'Z' > '+'`）⇒ 直接用 isoformat 的边界串会把整天漏掉/多收，故 `window_bounds`
必须回 `Z` 结尾。
"""
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import usage_ledger  # noqa: E402

SCHEMA = """
CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT 'unknown',
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'unknown',
    logical_model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'unknown',
    credential_id TEXT NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    cost_source TEXT NOT NULL DEFAULT ''
);
CREATE INDEX usage_events_created_at_idx ON usage_events(created_at);
"""

ROWS = [
    # created_at, request_id, client, model, provider, total_tokens, cost_usd, cost_source
    ("2026-10-14T10:00:00.000Z", "r1", "API Key 1", "deepseek-v4.1-flash", "alibaba", 100, 0.5, "models.dev"),
    ("2026-10-14T11:00:00.000Z", "r2", "API Key 1", "deepseek-v4.1-flash", "alibaba", 300, None, ""),
    ("2026-10-14T12:00:00.000Z", "r3", "API Key 1", "nvidia/nemotron:free", "openrouter", 20, None, ""),
    ("2026-10-14T13:00:00.000Z", "r4", "Local Gateway", "deepseek-v4.1-flash", "alibaba", 50, 0.25, "openrouter"),
]


class TestUsageLedger(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0usage-"))
        self.db = self.tmp / "usage.sqlite"
        conn = sqlite3.connect(str(self.db))
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO usage_events(created_at,request_id,client,model,provider,"
            "method,path,total_tokens,cost_usd,cost_source) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [(r[0], r[1], r[2], r[3], r[4], "POST", "/v1/messages", r[5], r[6], r[7])
             for r in ROWS])
        conn.commit()
        conn.close()
        self._saved = os.environ.get("CCR_USAGE_DB")
        os.environ["CCR_USAGE_DB"] = str(self.db)
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("CCR_USAGE_DB", None)
        else:
            os.environ["CCR_USAGE_DB"] = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ① 只读 --------------------------------------------------------------
    def test_connection_is_read_only(self):
        conn = usage_ledger._connect_ro(str(self.db))
        self.addCleanup(conn.close)
        with self.assertRaises(sqlite3.OperationalError, msg="mode=ro 必须真的写不进去"):
            conn.execute("INSERT INTO usage_events(method,path) VALUES('POST','/x')")

    # ② cost 不许静默当 0 -------------------------------------------------
    def test_null_cost_is_reported_not_silently_zero(self):
        d = usage_ledger.summary(group_by=("client",))
        self.assertTrue(d["ok"], d)
        by_client = {r["client"]: r for r in d["rows"]}
        self.assertAlmostEqual(by_client["API Key 1"]["cost_usd"], 0.5, places=6)
        self.assertEqual(by_client["API Key 1"]["null_cost"], 2,
                         "两条 NULL cost 必须显式计数，不能被 COALESCE 吞掉")
        self.assertEqual(d["total"]["null_cost"], 2)
        self.assertAlmostEqual(d["total"]["cost_usd"], 0.75, places=6)
        self.assertEqual(d["total"]["cost_sources"][""], 2,
                         "空 cost_source 必须有独立的 \"\" 桶")
        self.assertEqual(d["attribution"], usage_ledger.ATTRIBUTION)

    def test_group_by_client_model_default(self):
        d = usage_ledger.summary()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["group_by"], ["client", "model"])
        keys = {(r["client"], r["model"]) for r in d["rows"]}
        self.assertIn(("API Key 1", "deepseek-v4.1-flash"), keys)
        self.assertIn(("API Key 1", "nvidia/nemotron:free"), keys)
        row = next(r for r in d["rows"]
                   if r["client"] == "API Key 1" and r["model"] == "deepseek-v4.1-flash")
        self.assertEqual(row["requests"], 2)
        self.assertEqual(row["total_tokens"], 400)

    def test_totals_count_every_row(self):
        d = usage_ledger.summary(group_by=("client",))
        self.assertEqual(d["total"]["requests"], 4)
        self.assertEqual(d["total"]["total_tokens"], 470)

    # ③ logical_model 明确拒绝 --------------------------------------------
    def test_logical_model_is_rejected(self):
        with self.assertRaises(ValueError) as cm:
            usage_ledger.summary(group_by=("logical_model",))
        self.assertIn("logical_model", str(cm.exception))
        self.assertNotIn("logical_model", usage_ledger.GROUP_COLS)

    def test_empty_group_by_is_rejected(self):
        with self.assertRaises(ValueError):
            usage_ledger.summary(group_by=())

    # ④ fail-closed -------------------------------------------------------
    def test_missing_ledger_is_not_zeros(self):
        os.environ["CCR_USAGE_DB"] = str(self.tmp / "nope.sqlite")
        d = usage_ledger.summary()
        self.assertFalse(d["ok"])
        self.assertIn("not found", d["error"])
        self.assertIsNone(d["total"], "拿不到账本时不许编一个 total=0")
        ok, why = usage_ledger.available()
        self.assertFalse(ok)
        self.assertTrue(why)

    def test_corrupt_ledger_is_not_zeros(self):
        bad = self.tmp / "bad.sqlite"
        bad.write_bytes(b"this is not a sqlite file")
        os.environ["CCR_USAGE_DB"] = str(bad)
        d = usage_ledger.summary()
        self.assertFalse(d["ok"])
        self.assertIn("error", d)

    # 时间窗 --------------------------------------------------------------
    def test_window_bounds_use_Z_suffix(self):
        since, until = usage_ledger.window_bounds(24)
        self.assertTrue(since.endswith("Z") and until.endswith("Z"),
                        f"必须是账本口径的 Z 结尾，得到 {since!r}/{until!r}")
        self.assertNotIn("+00:00", since)
        self.assertEqual(usage_ledger.window_bounds(0), (None, None))
        self.assertEqual(usage_ledger.window_bounds(None), (None, None))

    def test_window_filters_rows(self):
        d = usage_ledger.summary(since="2026-10-14T11:30:00.000Z",
                                 until="2026-10-14T13:30:00.000Z")
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["total"]["requests"], 2, "窗口内应只剩 r3/r4")

    def test_limit_is_clamped(self):
        d = usage_ledger.summary(group_by=("model",), limit=9999)
        self.assertTrue(d["ok"])
        self.assertLessEqual(len(d["rows"]), usage_ledger.MAX_LIMIT)

    # by_request ----------------------------------------------------------
    def test_by_request_exact_match(self):
        d = usage_ledger.by_request(["r2", "r4"])
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["count"], 2)
        self.assertEqual({r["request_id"] for r in d["rows"]}, {"r2", "r4"})
        self.assertEqual(usage_ledger.by_request([])["count"], 0)

    def test_by_request_missing_ledger(self):
        os.environ["CCR_USAGE_DB"] = str(self.tmp / "nope.sqlite")
        d = usage_ledger.by_request(["r1"])
        self.assertFalse(d["ok"])


if __name__ == "__main__":
    unittest.main(verbosity=2)