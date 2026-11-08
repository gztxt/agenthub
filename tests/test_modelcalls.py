# -*- coding: utf-8 -*-
"""modelcalls 模型调用计量的 L0 测试（空 HOME、不连网、**不碰真 CCR 账本**）。

两组口径分别钉住：
  A. `record()` / `model_call_logs` —— agenthub 自身 manager LLM 调用的
     append-only 明细（成功/失败两态、token 缺失补 0 不丢行、未知场景落 other）。
  B. `overview()` —— 设置页「模型计量」的**全 agent 用量**报表，数据源是 CCR
     `usage_events`（经 `CCR_USAGE_DB` 注入临时账本），**不是** A 那张表。
     钉住：按模型/来源聚合、时间窗、fail-closed 带 note 不静默 0、`recent()` 明细。

2026-11-07 改判：原实现 overview 读 A 的空表 ⇒ 页面恒 0 行。B 组就是防止
「再接回空表」的回归闸门（同 AGENTS.md 第 11 条反例）。
"""
import os
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
# 2026-11-07：删掉模块级 `os.environ["HOME"] = tempfile.mkdtemp()`。
# 本模块与 HOME 无关（只用 db），但那行在 **import 期**改全局环境：
# unittest discover 会先把所有模块导入完再跑用例 ⇒ L1 的
# test_hublog.test_real_systemd_unit_exists 随后读到假 HOME，报「真单元不存在」。
# 需要隔离 HOME 的用例请在自己的 setUp 里改并还原。

import db  # noqa: E402
import modelcalls  # noqa: E402
import usage_ledger  # noqa: E402


_LEDGER_DDL = """CREATE TABLE usage_events(
    id INTEGER PRIMARY KEY, created_at TEXT, request_id TEXT, client TEXT,
    method TEXT, path TEXT, model TEXT, logical_model TEXT, provider TEXT,
    credential_id TEXT, status_code INTEGER, duration_ms INTEGER,
    input_tokens INTEGER, output_tokens INTEGER, cache_read_tokens INTEGER,
    cache_write_tokens INTEGER, total_tokens INTEGER, cost_usd REAL,
    cost_source TEXT)"""


def _iso_z(dt):
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def _mk_ledger(rows=()):
    """造临时 CCR 账本。rows = [(model, client, tin, tout, duration_ms, minutes_ago)]。

    结构只取 `usage_ledger.summary()`/`recent()` 真正读的列；通过 `CCR_USAGE_DB`
    覆盖点注入（`db_path()` 每次读 env），**绝不连生产账本**。
    """
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    conn = sqlite3.connect(p)
    conn.execute(_LEDGER_DDL)
    now = datetime.now(timezone.utc)
    for i, (model, client, tin, tout, ms, age_min) in enumerate(rows):
        conn.execute(
            "INSERT INTO usage_events(created_at, request_id, client, model, provider,"
            " status_code, duration_ms, input_tokens, output_tokens, cache_read_tokens,"
            " cache_write_tokens, total_tokens, cost_usd, cost_source)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (_iso_z(now - timedelta(minutes=age_min)), "r%d" % i, client, model,
             model.split("/", 1)[0] if "/" in model else "", 200, ms,
             tin, tout, 0, 0, tin + tout, 0.01, "openrouter"))
    conn.commit()
    conn.close()
    return p


def _fresh_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(Path(p))
    modelcalls.ensure_schema()


class ModelCallsL0(unittest.TestCase):
    """两类用例共用：把 CCR_USAGE_DB 指到临时账本，隔离生产数据。"""

    def setUp(self):
        self._saved_ccr = os.environ.get("CCR_USAGE_DB")
        self._ledger = _mk_ledger()
        os.environ["CCR_USAGE_DB"] = self._ledger

    def tearDown(self):
        if self._saved_ccr is None:
            os.environ.pop("CCR_USAGE_DB", None)
        else:
            os.environ["CCR_USAGE_DB"] = self._saved_ccr
        try:
            os.unlink(self._ledger)
        except OSError:
            pass

    def _use_ledger(self, rows):
        """用例内换成带数据的账本（替换 setUp 的空账本），返回路径。"""
        try:
            os.unlink(self._ledger)
        except OSError:
            pass
        self._ledger = _mk_ledger(rows)
        os.environ["CCR_USAGE_DB"] = self._ledger
        return self._ledger

    # ── A 组：agenthub 自身 LLM 调用明细（record / model_call_logs）──

    def test_01_record_success_with_tokens(self):
        _fresh_db()
        modelcalls.record("alibaba/qwen3.8-flash", {"input_tokens": 100, "output_tokens": 50},
                          scene="manager", duration_ms=844, request_id="r1")
        rows = db.query("SELECT model, provider, scene, status, input_tokens, output_tokens,"
                        " total_tokens, duration_ms FROM model_call_logs")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "alibaba/qwen3.8-flash")
        self.assertEqual(r["provider"], "alibaba")
        self.assertEqual(r["scene"], "manager")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["input_tokens"], 100)
        self.assertEqual(r["output_tokens"], 50)
        self.assertEqual(r["total_tokens"], 150)
        self.assertEqual(r["duration_ms"], 844)

    def test_02_record_error_keeps_row_zero_tokens(self):
        _fresh_db()
        modelcalls.record("openrouter/free", {}, scene="memory",
                          status="error", error="429 rate limit")
        rows = db.query("SELECT status, total_tokens, error FROM model_call_logs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["total_tokens"], 0)
        self.assertEqual(rows[0]["error"], "429 rate limit")

    def test_03_unknown_scene_falls_to_other(self):
        _fresh_db()
        modelcalls.record("x/y", {}, scene="bogus-scene")
        rows = db.query("SELECT scene FROM model_call_logs")
        self.assertEqual(rows[0]["scene"], "other")

    def test_04_record_noop_when_db_closed(self):
        # 连接未 init ⇒ record/ensure_schema/overview 静默跳过，绝不抛（计量不打断调用方）
        import db as _dbmod
        saved = _dbmod._conn
        _dbmod._conn = None
        try:
            modelcalls.ensure_schema()   # 不应抛
            modelcalls.record("x/y", {})  # 不应抛
            modelcalls.overview(7)        # 账本为空合法返回，不应抛
        finally:
            _dbmod._conn = saved

    def test_05_provider_split(self):
        self.assertEqual(modelcalls._split_provider("alibaba/qwen3.8"), "alibaba")
        self.assertEqual(modelcalls._split_provider("nvidia/nemotron"), "nvidia")
        self.assertEqual(modelcalls._split_provider("no-slash-model"), "")
        self.assertEqual(modelcalls._split_provider(""), "")

    def test_06_build_router_returns_two_read_endpoints(self):
        r = modelcalls.build_router()
        paths = {getattr(x, "path", "") for x in r.routes}
        self.assertIn("/api/monitor/model-calls/overview", paths)
        self.assertIn("/api/monitor/model-calls", paths)

    # ── B 组：全 agent 用量报表（overview / CCR usage_events）──

    def test_07_overview_reads_ccr_ledger_not_local_table(self):
        """核心回归：往 model_call_logs 写 3 行、往 CCR 账本写 3 行，
        overview 必须读后者（页面不再恒 0）。"""
        _fresh_db()
        modelcalls.record("local/only", {"input_tokens": 1, "output_tokens": 1})
        self._use_ledger([
            ("alibaba/qwen3.8-flash", "Profile: Claude Code", 100, 50, 800, 1),
            ("alibaba/qwen3.8-flash", "pi", 200, 100, 1200, 1),
            ("deepseek-v4.1-flash", "Local Gateway", 10, 5, 300, 1),
        ])
        ov = modelcalls.overview(7)
        self.assertEqual(ov["total_calls"], 3)
        self.assertEqual(ov["total_tokens"], 465)
        self.assertEqual(ov["source"], "ccr_usage_events")
        self.assertEqual(ov["by_scene"],
                         {"Profile: Claude Code": 1, "pi": 1, "Local Gateway": 1})
        m = next(x for x in ov["by_model"] if x["model"] == "alibaba/qwen3.8-flash")
        self.assertEqual(m["calls"], 2)
        self.assertEqual(m["total_tokens"], 450)
        self.assertEqual(m["provider"], "alibaba")
        self.assertGreater(m["avg_ms"], 0)
        self.assertNotIn("note", ov)

    def test_08_overview_window_excludes_old_rows(self):
        self._use_ledger([
            ("m/recent", "pi", 10, 5, 100, 1),
            ("m/old", "codex", 999, 999, 100, 40 * 24 * 60),  # 40 天前
        ])
        self.assertEqual(modelcalls.overview(7)["total_calls"], 1)
        self.assertEqual(modelcalls.overview(30)["total_calls"], 1)
        self.assertEqual(modelcalls.overview(90)["total_calls"], 2)

    def test_09_overview_empty_ledger_returns_zero_without_note(self):
        ov = modelcalls.overview(7)
        self.assertEqual(ov["total_calls"], 0)
        self.assertEqual(ov["total_tokens"], 0)
        self.assertEqual(ov["by_model"], [])
        self.assertNotIn("note", ov)  # 空账本是合法状态，不是故障

    def test_10_overview_fail_closed_when_ledger_missing(self):
        """账本缺失必须给 note 说明原因，不许静默 0 冒充「没调用」。"""
        os.environ["CCR_USAGE_DB"] = "/nonexistent/dir/no-usage.sqlite"
        ov = modelcalls.overview(7)
        self.assertEqual(ov["total_calls"], 0)
        self.assertIn("note", ov)
        self.assertIn("CCR 用量账本不可用", ov["note"])

    def test_11_summary_rows_carry_avg_ms(self):
        self._use_ledger([("m/x", "pi", 10, 5, 1500, 1)])
        data = usage_ledger.summary(group_by=("model",))
        self.assertTrue(data["ok"])
        self.assertEqual(data["rows"][0]["avg_ms"], 1500)

    def test_12_recent_maps_client_to_scene(self):
        self._use_ledger([("m/x", "Profile: Codex", 10, 5, 900, 1)])
        data = usage_ledger.recent(limit=10)
        self.assertTrue(data["ok"])
        self.assertEqual(data["count"], 1)
        self.assertEqual(data["rows"][0]["scene"], "Profile: Codex")
        self.assertEqual(data["rows"][0]["status"], "success")

    # ── C 组：2026-11-08 overview 增 rows（按天 × agent × 模型的可排序明细）──

    def test_13_overview_rows_are_daily_x_client_x_model(self):
        """rows 一行 = UTC 日 × client × model；旧字段仍由 summary 汇总、不回归。

        前 3 行刻意用同一个 age_min ⇒ 时间戳完全相同、必然同一天（避免
        「正好跨 00:00」让用例 flaky）；第 4 行 48 小时前 ⇒ 必然另一个 UTC 日。
        """
        self._use_ledger([
            ("m/x", "pi", 10, 5, 1000, 1),
            ("m/x", "pi", 20, 5, 3000, 1),
            ("m/x", "codex", 30, 5, 500, 1),
            ("m/y", "pi", 40, 5, 700, 48 * 60),
        ])
        ov = modelcalls.overview(7)
        self.assertTrue(ov["rows_ok"])
        self.assertIsNone(ov["rows_note"])
        rows = ov["rows"]
        self.assertEqual(len(rows), 3)
        self.assertEqual(len({r["date"] for r in rows}), 2, "跨日必须拆成不同行")
        r = next(x for x in rows if x["client"] == "pi" and x["model"] == "m/x")
        self.assertEqual(r["requests"], 2)
        self.assertEqual(r["total_tokens"], 40)
        self.assertEqual(r["avg_ms"], 2000)
        self.assertEqual(r["total_ms"], 4000)
        # 旧契约不回归
        self.assertEqual(ov["total_calls"], 4)
        self.assertEqual(ov["total_tokens"], 10 + 5 + 20 + 5 + 30 + 5 + 40 + 5)
        self.assertEqual(len(ov["by_model"]), 2)
        self.assertEqual(ov["source"], "ccr_usage_events")

    def test_14_overview_fail_closed_exposes_rows_state(self):
        os.environ["CCR_USAGE_DB"] = "/nonexistent/dir/no-usage.sqlite"
        ov = modelcalls.overview(7)
        self.assertIn("note", ov)
        self.assertEqual(ov["rows"], [])
        self.assertFalse(ov["rows_ok"])
        self.assertIsNotNone(ov["rows_note"])

    # ── D 组：Agent 归因（2026-11-08）──
    # CCR 账本的 client 是**凭据名**不是 agent 名（实测全机共用一把 key `aoto`）。
    # 这里钉住两条纪律：能映射的映射成 agents 清单里的名字；判不出的显式「未归属」
    # 且保留原始凭据名，**不把凭据名冒充成 agent**。

    def test_15_agent_of_maps_known_clients_only(self):
        self.assertEqual(modelcalls.agent_of("Profile: Claude Code"), ("claude", "Claude Code"))
        self.assertEqual(modelcalls.agent_of("pi"), ("pi", "Pi Agent"))
        self.assertEqual(modelcalls.agent_of("Profile: Codex"), ("codex", "Codex CLI"))
        self.assertEqual(modelcalls.agent_of("Profile: Grok CLI"), ("grok", "Grok CLI"))
        # 凭据名 / 未知值一律不认（宁可未归属，不可冒名）
        self.assertEqual(modelcalls.agent_of("aoto"), (None, None))
        self.assertEqual(modelcalls.agent_of("Local Gateway (rotated 10-18)"), (None, None))
        self.assertEqual(modelcalls.agent_of(""), (None, None))

    def test_16_overview_rows_carry_agent_and_raw_client(self):
        _fresh_db()
        self._use_ledger([
            ("m/x", "Profile: Claude Code", 10, 5, 100, 1),
            ("m/x", "aoto", 20, 5, 200, 1),
        ])
        ov = modelcalls.overview(7)
        self.assertTrue(ov["rows_ok"])
        r_claude = next(r for r in ov["rows"] if r["client"] == "Profile: Claude Code")
        self.assertEqual(r_claude["agent_id"], "claude")
        self.assertEqual(r_claude["agent_name"], "Claude Code")
        self.assertTrue(r_claude["agent_known"])
        self.assertEqual(r_claude["client_raw"], "Profile: Claude Code")
        # 共享凭据：不冒充 agent，但原始来源必须留在行里
        r_shared = next(r for r in ov["rows"] if r["client"] == "aoto")
        self.assertIsNone(r_shared["agent_id"])
        self.assertEqual(r_shared["agent_name"], "未归属")
        self.assertFalse(r_shared["agent_known"])
        self.assertEqual(r_shared["client_raw"], "aoto")
        # by_agent 供页头展示，未归属合成一条而不是丢掉
        self.assertEqual(ov["by_agent"].get("Claude Code"), 1)
        self.assertEqual(ov["by_agent"].get("未归属"), 1)


if __name__ == "__main__":
    unittest.main()
