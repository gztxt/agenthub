#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：D5 轨迹行 schema / D13 版本推进 / D16 badcase 回写（P0-1 收尾三项）。

【三条判据各自治什么】
- **D5**：轨迹若非**统一结构**，「追得到结果、追不到原因」（D4）。所以落库前必须过
  `normalize_trajectory()`（补 seq/metrics）并过 `validate_trajectory()`（缺 steps ⇒ 报缺口）。
- **D13**：比较前先钉版本，**比较期间数据集不许变** ⇒ `eval_is_stale()` 能把「跑完才发现
  题集被改过」当场判成 stale，而不是拿两个不可比的分数去比。
- **D16**：评估结果要**写回 dataset_item**（badcase 池）⇒ `badcases()` 能直接捞「判否」的题，
  下一轮实验用题从库里长出来，而不是靠人记。

分层口径（tests/tiers.py）：临时 db，纯函数 + 幂等 ALTER，不连网、零 SKIP。
"""
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import dataset  # noqa: E402
import db       # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0ds2-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        dataset.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved

    def _ds(self):
        return dataset.create_dataset("d1", grain="session")


class TestTrajectorySchema(_Base):
    """D5：轨迹行 schema —— 统一结构 + 可校验缺口。"""

    def test_normalize_fills_seq_and_metrics(self):
        t = dataset.normalize_trajectory({
            "session_id": "s1", "source": "pi",
            "events": [{"event": "a", "status": "success", "duration_ms": 10},
                       {"event": "b", "status": "failed", "duration_ms": 5}]})
        self.assertEqual(t["schema"], dataset.TRAJECTORY_SCHEMA)
        self.assertEqual([0, 1], [s["seq"] for s in t["steps"]])
        self.assertEqual(2, t["metrics"]["steps"])
        self.assertEqual(1, t["metrics"]["errors"])
        self.assertEqual(15, t["metrics"]["total_ms"])

    def test_normalize_is_idempotent(self):
        a = dataset.normalize_trajectory({"session_id": "s", "source": "x",
                                          "events": [{"event": "e"}]})
        self.assertEqual(a, dataset.normalize_trajectory(a))

    def test_validate_reports_missing_steps(self):
        gaps = dataset.validate_trajectory({"session_id": "s", "source": "x"})
        self.assertTrue(any("steps" in g for g in gaps), gaps)

    def test_validate_ok_for_normalized(self):
        t = dataset.normalize_trajectory({"session_id": "s", "source": "x",
                                          "events": [{"event": "e"}]})
        self.assertEqual([], dataset.validate_trajectory(t))

    def test_validate_reports_wrong_schema_tag(self):
        gaps = dataset.validate_trajectory({"schema": "trajectory/0", "steps": []})
        self.assertTrue(any("schema" in g for g in gaps), gaps)

    def test_item_stores_normalized_trajectory(self):
        ds = self._ds()
        raw = dataset.normalize_trajectory({"session_id": "s", "source": "x",
                                            "events": [{"event": "e"}]})
        i = dataset.add_item(ds, split="train", input="q",
                             trajectory=dataset.dumps_trajectory(raw),
                             grain_key={"k": "q"})
        import json
        self.assertEqual(dataset.TRAJECTORY_SCHEMA,
                         json.loads(dataset.get_item(i)["trajectory"])["schema"])


class TestVersioning(_Base):
    """D13：版本推进 + 评估陈旧判定。"""

    def test_initial_version_is_one(self):
        ds = self._ds()
        self.assertEqual(1, dataset.get_dataset(ds)["version"])

    def test_bump_increments_and_logs(self):
        ds = self._ds()
        self.assertEqual(2, dataset.bump_version(ds, note="add 5 items"))
        self.assertEqual(2, dataset.get_dataset(ds)["version"])
        rows = dataset.list_versions(ds)
        self.assertEqual([2], [r["version"] for r in rows])
        self.assertEqual("add 5 items", rows[0]["note"])

    def test_eval_is_not_stale_when_version_unchanged(self):
        ds = self._ds()
        eid = dataset.pin_eval(ds, split="train", model="m")
        self.assertFalse(dataset.eval_is_stale(eid))

    def test_eval_becomes_stale_after_bump(self):
        ds = self._ds()
        eid = dataset.pin_eval(ds, split="train", model="m")
        dataset.bump_version(ds)
        self.assertTrue(dataset.eval_is_stale(eid),
                        "版本变了而评估还钉在旧版 ⇒ 必须判 stale，否则两个不可比的分数会被拿来比")

    def test_eval_is_stale_for_unknown_eval(self):
        self.assertTrue(dataset.eval_is_stale("no-such-eval"))


class TestBadcaseWriteback(_Base):
    """D16：评估结果写回 dataset_item，badcase 池从库里长出来。"""

    def _fixture(self):
        ds = self._ds()
        a = dataset.add_item(ds, split="holdout", input="q-a", grain_key={"k": "a"})
        b = dataset.add_item(ds, split="holdout", input="q-b", grain_key={"k": "b"})
        eid = dataset.pin_eval(ds, split="holdout", model="m")
        return ds, a, b, eid

    def test_record_result_and_list_badcases(self):
        ds, a, b, eid = self._fixture()
        dataset.record_eval_result(eid, a, verdict="pass", score=1.0)
        dataset.record_eval_result(eid, b, verdict="fail", score=0.0)
        self.assertEqual([b], [r["id"] for r in dataset.badcases(ds)])
        self.assertEqual("pass", dataset.get_item(a)["last_verdict"])

    def test_badcases_excludes_untested_items(self):
        ds, a, b, eid = self._fixture()
        dataset.record_eval_result(eid, a, verdict="pass", score=1.0)
        self.assertEqual([], dataset.badcases(ds), "没跑过的不算 badcase")

    def test_unknown_verdict_is_rejected(self):
        ds, a, b, eid = self._fixture()
        with self.assertRaises(ValueError):
            dataset.record_eval_result(eid, a, verdict="maybe", score=0.5)

    def test_result_for_unknown_item_is_rejected(self):
        ds, a, b, eid = self._fixture()
        with self.assertRaises(KeyError):
            dataset.record_eval_result(eid, 99999, verdict="fail")

    def test_stale_eval_result_is_rejected(self):
        """跑完才发现题集被改过 ⇒ 这条结果不许回写（两个版本不可比）。"""
        ds, a, b, eid = self._fixture()
        dataset.bump_version(ds)
        with self.assertRaises(ValueError):
            dataset.record_eval_result(eid, a, verdict="pass", score=1.0)


class TestMigration(_Base):
    """存量 dataset_items 表幂等补列，不动既有行。"""

    OLD = """
    CREATE TABLE dataset_items (
        id INTEGER PRIMARY KEY AUTOINCREMENT, dataset_id TEXT NOT NULL, split TEXT NOT NULL,
        grain_hash TEXT NOT NULL, input TEXT NOT NULL, output TEXT, trajectory TEXT,
        trace_id TEXT, session_id TEXT, source_ref TEXT,
        created_at TEXT NOT NULL, updated_at TEXT NOT NULL, UNIQUE(dataset_id, grain_hash));
    """

    def test_existing_items_table_gets_new_columns(self):
        db.execute_script("DROP TABLE IF EXISTS dataset_items")
        db.execute_script(self.OLD)
        db.execute("INSERT INTO dataset_items(dataset_id,split,grain_hash,input,created_at,"
                   "updated_at) VALUES('d','train','h','q','t','t')")
        dataset.ensure_schema()
        cols = {r["name"] for r in db.query("PRAGMA table_info(dataset_items)")}
        for c in ("last_eval_id", "last_verdict", "last_score", "last_eval_at"):
            self.assertIn(c, cols, "缺列 %s" % c)
        row = db.query("SELECT * FROM dataset_items WHERE id=1")[0]
        self.assertEqual("q", row["input"], "迁移不得改动既有行")
        self.assertIsNone(row["last_verdict"], "旧行保持未判（不回填）")


if __name__ == "__main__":
    unittest.main()