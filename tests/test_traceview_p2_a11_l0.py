"""P2 A11 · 端到端 Trace 因果链 的 L0 闸门。

## 在测什么（每条对应一个真缺口，不是「跑起来就算」）

1. **断链如实报**：`trace_id` 为空的行**不得**进任何链（`orphans` 分表计数）。
2. **排序确定**：同一批输入换喂入顺序 ⇒ 输出逐字节相同（G2 可回放）。
3. **同刻可复现**：三表 `created_at` 相同时按 (表, seq) 破平，结果不漂。
4. **缺列不炸**：存量库 `asset_audit` 无 `trace_id` 列时，查询**不抛**且显式
   `audit_trace_available=False`（能力边界，不是「审计为空」）。
5. **写入路径**：`log_asset_event(trace_id=...)` 落库；不传则 NULL。
6. **迁移幂等**：`init_db` 跑两遍不报错，旧行 trace_id 保持 NULL（不替历史编因果）。

## 红向自证（判据必须能被证伪，跑过才认）

- 让 `build_chains` 把无 trace 的行归入某条链 ⇒ 用例 1 转红。
- 让排序退回「按喂入顺序」⇒ 用例 2 转红。
- 让 `collect` 在缺列时直接查 `trace_id` ⇒ 用例 4 转红（OperationalError）。
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import traceview  # noqa: E402


def _n(table, at, seq, tid, summary="x"):
    return {"table": table, "at": at, "seq": seq, "trace_id": tid, "summary": summary}


class TestOrphansAreHonest(unittest.TestCase):
    """判据 1：无 trace 的行不进任何链，且被计数。"""

    def test_no_trace_rows_go_to_orphans_not_chains(self):
        nodes = [
            _n("profile_events", "2026-10-17T01:00:00Z", 1, "T1"),
            _n("profile_events", "2026-10-17T01:00:01Z", 2, None),   # 无 trace
            _n("asset_audit", "2026-10-17T01:00:02Z", 3, ""),        # 空 trace
            _n("tasks", "2026-10-17T01:00:03Z", "t-1", "T1"),
        ]
        r = traceview.build_chains(nodes)
        self.assertEqual(set(r["chains"]), {"T1"})
        self.assertEqual(len(r["chains"]["T1"]), 2, "只有带 T1 的两行进链")
        self.assertEqual(r["orphans"], {"profile_events": 1, "asset_audit": 1},
                         "无 trace 的行必须按表分桶计数，不得静默丢弃")

    def test_orphans_count_matches_counts(self):
        nodes = [_n("a", "t", 1, None), _n("b", "t", 2, None), _n("c", "t", 3, "T")]
        r = traceview.build_chains(nodes)
        self.assertEqual(r["counts"]["orphans"], 2)
        self.assertEqual(sum(r["orphans"].values()), 2)
        self.assertEqual(r["counts"]["traces"], 1)


class TestOrderIsDeterministic(unittest.TestCase):
    """判据 2/3：输入顺序无关 + 同刻按 (表, seq) 破平。"""

    def test_shuffle_does_not_change_result(self):
        import random
        nodes = [
            _n("profile_events", "2026-10-17T01:00:00Z", 2, "T1", "b"),
            _n("profile_events", "2026-10-17T01:00:00Z", 1, "T1", "a"),
            _n("tasks", "2026-10-17T00:59:00Z", "t-1", "T1", "c"),
            _n("asset_audit", "2026-10-17T01:00:00Z", 5, "T1", "d"),
        ]
        base = traceview.build_chains(nodes)["chains"]["T1"]
        for _ in range(5):
            random.shuffle(nodes)
            got = traceview.build_chains(nodes)["chains"]["T1"]
            self.assertEqual(
                [(n["table"], n["seq"]) for n in got],
                [(n["table"], n["seq"]) for n in base],
                "同一批输入换顺序必须给出同一链序（可回放）")

    def test_same_timestamp_tiebreak_by_table_then_seq(self):
        # 三表同一时刻：必须按 (table) 稳定破平，不能随喂入顺序漂。
        a = _n("asset_audit", "2026-10-17T01:00:00Z", 9, "T")
        b = _n("profile_events", "2026-10-17T01:00:00Z", 1, "T")
        c = _n("tasks", "2026-10-17T01:00:00Z", "s", "T")
        order = [n["table"] for n in traceview.build_chains([a, b, c])["chains"]["T"]]
        self.assertEqual(order, ["asset_audit", "profile_events", "tasks"])
        order2 = [n["table"] for n in traceview.build_chains([c, a, b])["chains"]["T"]]
        self.assertEqual(order, order2, "破平规则必须与喂入顺序无关")

    def test_unknown_time_sorts_first_and_is_flagged(self):
        nodes = [
            _n("profile_events", "2026-10-17T01:00:00Z", 1, "T"),
            _n("tasks", None, "s", "T"),          # 时间未知
        ]
        chain = traceview.build_chains(nodes)["chains"]["T"]
        self.assertEqual(chain[0]["table"], "tasks", "未知时间排最前")
        self.assertTrue(chain[0]["time_unknown"], "未知时间必须显式标记，不许伪装成某时刻")


class TestSummarySemantics(unittest.TestCase):
    """`complete` 是「无断链」不是「成功」—— 用例把两者分开。"""

    def test_complete_true_even_when_all_fail(self):
        nodes = [_n("profile_events", "2026-10-17T01:00:00Z", 1, "T", "fail")]
        s = traceview.summarize(traceview.build_chains(nodes))
        self.assertTrue(s["complete"], "无断链 ⇒ complete=True")
        self.assertEqual(s["traces"], 1)

    def test_complete_false_when_orphans(self):
        nodes = [_n("profile_events", "t", 1, "T"), _n("tasks", "t", 2, None)]
        s = traceview.summarize(traceview.build_chains(nodes))
        self.assertFalse(s["complete"])


class TestCollectDegradesOnMissingColumn(unittest.TestCase):
    """判据 4：缺列不炸，且显式声明能力边界。"""

    def setUp(self):
        import db
        self.db = db

    def test_collect_on_empty_db_no_crash_and_flag(self):
        # 全景内存库：不加 asset_audit.trace_id 列，模拟迁移前的存量库。
        import sqlite3
        con = sqlite3.connect(":memory:")
        con.row_factory = sqlite3.Row
        con.executescript(
            "CREATE TABLE profile_events(id INTEGER PRIMARY KEY, source TEXT, subject TEXT,"
            " trace_id TEXT, status TEXT, duration_ms INTEGER, detail TEXT, created_at TEXT);"
            "CREATE TABLE tasks(task_id TEXT, run_id TEXT, trace_id TEXT, agent_id TEXT,"
            " status TEXT, model TEXT, verdict TEXT, verify_reason TEXT, goal TEXT,"
            " started_at TEXT, finished_at TEXT);"
            "CREATE TABLE asset_audit(id INTEGER PRIMARY KEY, asset_type TEXT,"
            " asset_slug TEXT, action TEXT, actor TEXT, verdict TEXT, created_at TEXT);"
        )
        self.db._conn = con
        try:
            got = traceview._audit_has_trace()
            self.assertFalse(got, "无列时必须判 False，不许假定有")
            res = traceview.collect("no-such-trace", limit=10)
            self.assertFalse(res["audit_trace_available"],
                             "缺列 ⇒ 显式报能力边界，不是静默当空集")
            self.assertEqual(res["chain"], [])
        finally:
            con.close()


class TestCollectReturnsRealChain(unittest.TestCase):
    """★ 判据 7（红向抓出来的缺失用例）：`collect()` 有数据时必须真回出链。

    首版 `collect()` 构造的节点**没带 `trace_id`** ⇒ `build_chains` 把它们全算 orphan
    ⇒ 端点恒回空链而 `nodes>0`。这个 bug 单测漏掉是因为只测了「空库 / 缺列」两条降级路径，
    **没测「查到行」这条主路径** —— 正是判例 185「判据量错对象」的同族：
    断言只覆盖降级态，主路径完全没被验。本用例钉死主路径。
    """

    def setUp(self):
        import sqlite3
        import db
        self.db = db
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        self.con.executescript(
            "CREATE TABLE profile_events(id INTEGER PRIMARY KEY, source TEXT, subject TEXT,"
            " trace_id TEXT, status TEXT, duration_ms INTEGER, detail TEXT, created_at TEXT);"
            "CREATE TABLE tasks(task_id TEXT, run_id TEXT, trace_id TEXT, agent_id TEXT,"
            " status TEXT, model TEXT, verdict TEXT, verify_reason TEXT, goal TEXT,"
            " started_at TEXT, finished_at TEXT);"
            "CREATE TABLE asset_audit(id INTEGER PRIMARY KEY, asset_type TEXT,"
            " asset_slug TEXT, action TEXT, actor TEXT, verdict TEXT, created_at TEXT,"
            " trace_id TEXT);"
        )
        self._orig = db._conn
        db._conn = self.con

    def tearDown(self):
        self.db._conn = self._orig
        self.con.close()

    def test_collect_builds_chain_from_rows(self):
        self.con.execute("INSERT INTO profile_events(source,subject,status,duration_ms,"
                         "created_at,trace_id) VALUES('hub_chat','run','success',5,"
                         "'2026-10-17T01:00:00Z','TR-9')")
        self.con.execute("INSERT INTO profile_events(source,subject,status,duration_ms,"
                         "created_at,trace_id) VALUES('task_exec','run','fail',7,"
                         "'2026-10-17T01:00:01Z','TR-9')")
        self.con.execute("INSERT INTO tasks(task_id,status,started_at,trace_id)"
                         " VALUES('t-1','success','2026-10-17T01:00:02Z','TR-9')")
        self.con.commit()
        res = traceview.collect("TR-9", limit=50)
        self.assertEqual(len(res["chain"]), 3,
                         "查到的 3 行必须进链 —— 恒回空链说明节点丢了 trace_id")
        self.assertEqual(res["orphans"], {}, "已知 trace 取数不该产生 orphan")
        self.assertEqual([n["table"] for n in res["chain"]],
                         ["profile_events", "profile_events", "tasks"])
        self.assertTrue(res["complete"])

    def test_collect_excludes_other_traces(self):
        self.con.execute("INSERT INTO profile_events(source,subject,status,created_at,"
                         "trace_id) VALUES('a','x','success','2026-10-17T01:00:00Z','TR-1')")
        self.con.execute("INSERT INTO profile_events(source,subject,status,created_at,"
                         "trace_id) VALUES('b','y','success','2026-10-17T01:00:00Z','TR-2')")
        self.con.commit()
        res = traceview.collect("TR-1", limit=50)
        self.assertEqual(len(res["chain"]), 1, "只回本 trace 的行")
        # `_node()` 把 detail 的键**摊平**进节点（见 traceview._node），故直接读 source。
        self.assertEqual(res["chain"][0]["source"], "a")


class TestWritePathCarriesTrace(unittest.TestCase):
    """判据 5：写入路径能把 trace_id 落库。"""

    def setUp(self):
        import sqlite3
        import db
        self.db = db
        self.con = sqlite3.connect(":memory:")
        self.con.row_factory = sqlite3.Row
        # 用真实 SCHEMA 保证列口径与生产一致。
        import re
        schema = db.SCHEMA
        self.con.executescript(schema)
        self._orig = db._conn
        db._conn = self.con

    def tearDown(self):
        self.db._conn = self._orig
        self.con.close()

    def test_trace_id_persisted(self):
        self.db.log_asset_event("setting", "model.x", "update", "user:test",
                                detail={"a": 1}, trace_id="TR-1")
        row = self.con.execute(
            "SELECT trace_id FROM asset_audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertEqual(row["trace_id"], "TR-1")

    def test_missing_trace_is_null_not_empty_string(self):
        self.db.log_asset_event("setting", "model.y", "update", "user:test")
        row = self.con.execute(
            "SELECT trace_id FROM asset_audit ORDER BY id DESC LIMIT 1").fetchone()
        self.assertIsNone(row["trace_id"], "未传 ⇒ NULL（诚实），不是空串")


class TestSchemaHasColumn(unittest.TestCase):
    """判据 6：SCHEMA 已含 trace_id 列 + 幂等迁移不炸。"""

    def test_schema_declares_trace_id(self):
        import db
        self.assertIn("trace_id TEXT", db.SCHEMA,
                      "asset_audit 的 SCHEMA 必须声明 trace_id")

    def test_init_db_idempotent(self):
        import sqlite3
        import tempfile
        import db
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.db"
            db.init_db(p)          # 第一遍
            db.init_db(p)          # 第二遍（幂等 ALTER 不应炸）
            cols = [r[1] for r in db._conn.execute("PRAGMA table_info(asset_audit)")]
            self.assertIn("trace_id", cols)

    def test_existing_rows_keep_null_trace(self):
        """迁移不替历史编因果：先建无 trace 列的库插旧行，再 init_db，旧行仍 NULL。"""
        import sqlite3
        import tempfile
        import db
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "t.db"
            con = sqlite3.connect(str(p))
            con.executescript(
                "CREATE TABLE asset_audit(id INTEGER PRIMARY KEY AUTOINCREMENT,"
                " asset_type TEXT NOT NULL, asset_slug TEXT NOT NULL, action TEXT NOT NULL,"
                " actor TEXT NOT NULL, detail TEXT DEFAULT '{}', created_at TEXT NOT NULL);")
            con.execute("INSERT INTO asset_audit(asset_type,asset_slug,action,actor,detail,"
                        "created_at) VALUES('setting','old','update','user:x','{}','2026-01-01')")
            con.commit()
            con.close()
            db.init_db(p)
            row = db._conn.execute(
                "SELECT trace_id FROM asset_audit WHERE asset_slug='old'").fetchone()
            self.assertIsNone(row["trace_id"], "旧行必须保持 NULL，不得回填")


if __name__ == "__main__":
    unittest.main(verbosity=2)
