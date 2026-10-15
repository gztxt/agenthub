#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：数据集/经验库存储（src/dataset.py）P0-1 切片 A。

【这条判据治什么】
P0-1 要的是「轨迹→数据集→经验库」闭环的**地基**：四张表 + `grain` 唯一键 + `split` 隔离。
本切片只落**确定性**部分（建表 / 入样 / 隔离断言），不落管线与经验挖掘（切片 B/C）。

【本文件守五条】
1. **四表幂等建表**（验收①）：重复 ensure_schema 不炸、不重复。
2. **`grain` 唯一键**：同一逻辑样本（同 grain_key）重复写入**不产生第二行** ——
   否则数据集会被同一件事灌水，指标随之失真。
3. **`split` 隔离**（验收②）：holdout 样本**永不**出现在优化（train）批次里；
   且已入样样本的 split **不可改**（否则「隔离」可被一次改写抹掉）。
4. **字段映射显式**（D7）：input/output 不是同一列的同义词 —— 写进去读回来必须是原值。
5. 真调被测代码（临时 db，不连网、零 SKIP）。

分层口径（tests/tiers.py）：临时 db；不起服务、不连网。
"""
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import dataset  # noqa: E402
import db       # noqa: E402

FOUR_TABLES = ("datasets", "dataset_items", "eval_runs", "experiences")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0dataset-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        dataset.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved

    def _ds(self, name="d1", grain="task"):
        return dataset.create_dataset(name, grain=grain)


class TestSchema(_Base):
    def test_four_tables_exist(self):
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in FOUR_TABLES:
            self.assertIn(t, names, "缺表 %s" % t)

    def test_ensure_schema_is_idempotent(self):
        dataset.ensure_schema()
        dataset.ensure_schema()
        names = {r["name"] for r in db.query(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        for t in FOUR_TABLES:
            self.assertIn(t, names)

    def test_create_dataset_is_idempotent_by_name(self):
        a = self._ds("same")
        b = self._ds("same")
        self.assertEqual(a, b, "同名数据集不得重复建")


class TestGrainUniqueness(_Base):
    def test_same_grain_key_does_not_duplicate(self):
        ds = self._ds()
        i1 = dataset.add_item(ds, split="train", input="do X", grain_key={"goal": "X"})
        i2 = dataset.add_item(ds, split="train", input="do X again", grain_key={"goal": "X"})
        self.assertEqual(i1, i2, "同 grain 的样本必须复用同一行")
        self.assertEqual(1, dataset.counts(ds)["total"])

    def test_different_grain_key_creates_new_row(self):
        ds = self._ds()
        dataset.add_item(ds, split="train", input="X", grain_key={"goal": "X"})
        dataset.add_item(ds, split="train", input="Y", grain_key={"goal": "Y"})
        self.assertEqual(2, dataset.counts(ds)["total"])

    def test_grain_hash_is_deterministic_and_key_order_insensitive(self):
        h1 = dataset.grain_hash("task", {"a": 1, "b": 2})
        h2 = dataset.grain_hash("task", {"b": 2, "a": 1})
        self.assertEqual(h1, h2)


class TestSplitIsolation(_Base):
    """验收② ：优化批次不得纳入 holdout。"""

    def test_training_items_exclude_holdout(self):
        ds = self._ds()
        t = dataset.add_item(ds, split="train", input="train-me", grain_key={"k": "t"})
        h = dataset.add_item(ds, split="holdout", input="hold-me", grain_key={"k": "h"})
        train_ids = {it["id"] for it in dataset.training_items(ds)}
        self.assertIn(t, train_ids)
        self.assertNotIn(h, train_ids, "holdout 样本混进了优化批次")

    def test_assert_train_only_rejects_holdout_id(self):
        ds = self._ds()
        h = dataset.add_item(ds, split="holdout", input="hold", grain_key={"k": "h"})
        with self.assertRaises(ValueError):
            dataset.assert_train_only(ds, [h])

    def test_assert_train_only_accepts_train_id(self):
        ds = self._ds()
        t = dataset.add_item(ds, split="train", input="train", grain_key={"k": "t"})
        dataset.assert_train_only(ds, [t])       # 不抛即通过

    def test_counts_separates_splits(self):
        ds = self._ds()
        dataset.add_item(ds, split="train", input="a", grain_key={"k": "a"})
        dataset.add_item(ds, split="holdout", input="b", grain_key={"k": "b"})
        c = dataset.counts(ds)
        self.assertEqual(1, c["train"])
        self.assertEqual(1, c["holdout"])

    def test_existing_item_split_cannot_be_changed(self):
        """已入样样本的 split 不可改 —— 否则隔离可被一次改写抹掉。"""
        ds = self._ds()
        dataset.add_item(ds, split="holdout", input="x", grain_key={"k": "x"})
        with self.assertRaises(ValueError):
            dataset.add_item(ds, split="train", input="x", grain_key={"k": "x"})
        self.assertEqual(1, dataset.counts(ds)["holdout"])

    def test_invalid_split_is_rejected(self):
        ds = self._ds()
        with self.assertRaises(ValueError):
            dataset.add_item(ds, split="validation", input="x", grain_key={"k": "x"})


class TestItemMapping(_Base):
    """D7：字段映射显式，别把最终答案与轨迹接反。"""

    def test_input_output_roundtrip(self):
        ds = self._ds()
        i = dataset.add_item(ds, split="train", input="QUESTION",
                             output="ANSWER", trajectory='{"steps": 1}',
                             trace_id="tr1", session_id="s1", source_ref="ref",
                             grain_key={"k": "q"})
        row = dataset.get_item(i)
        self.assertEqual(row["input"], "QUESTION")
        self.assertEqual(row["output"], "ANSWER")
        self.assertEqual(row["trajectory"], '{"steps": 1}')
        self.assertEqual(row["trace_id"], "tr1")
        self.assertEqual(row["session_id"], "s1")

    def test_empty_input_is_rejected(self):
        ds = self._ds()
        with self.assertRaises(ValueError):
            dataset.add_item(ds, split="train", input="", grain_key={"k": "q"})


class TestEvalRun(_Base):
    def test_pin_eval_records_dataset_version(self):
        ds = self._ds()
        dataset.add_item(ds, split="holdout", input="h", grain_key={"k": "h"})
        ev = dataset.pin_eval(ds, split="holdout", model="m", config_hash="abc")
        row = dataset.get_eval_run(ev)
        self.assertEqual(row["dataset_id"], ds)
        self.assertEqual(row["split"], "holdout")
        self.assertEqual(row["config_hash"], "abc")
        self.assertEqual(row["dataset_version"], dataset.get_dataset(ds)["version"])


class TestExperience(_Base):
    def test_record_and_list_active(self):
        eid = dataset.record_experience(kind="pitfall", situation="S", action="A",
                                        precondition="P", source_trace_ids=["tr1"])
        rows = dataset.list_experiences()
        self.assertEqual(1, len(rows))
        self.assertEqual(rows[0]["id"], eid)
        self.assertEqual(rows[0]["situation"], "S")

    def test_retired_experience_is_hidden_from_active(self):
        eid = dataset.record_experience(kind="pitfall", situation="S", action="A")
        dataset.retire_experience(eid, superseded_by="newer")
        self.assertEqual([], dataset.list_experiences())
        self.assertEqual(1, len(dataset.list_experiences(active_only=False)))
        self.assertEqual("newer", dataset.get_experience(eid)["superseded_by"])


class TestListAndSearch(_Base):
    """只读门面（路由层用）：数据集清单 + 经验搜索（召回路径）。"""

    def test_list_datasets_returns_created(self):
        ds = self._ds("d1")
        rows = dataset.list_datasets()
        self.assertEqual([ds], [r["id"] for r in rows])

    def test_search_experiences_hits_situation(self):
        dataset.record_experience("pitfall", situation="disk full", action="clean tmp")
        dataset.record_experience("recipe", situation="other", action="x")
        hits = dataset.search_experiences("disk")
        self.assertEqual(1, len(hits))
        self.assertEqual("disk full", hits[0]["situation"])

    def test_search_excludes_retired(self):
        eid = dataset.record_experience("pitfall", situation="disk full", action="clean")
        dataset.retire_experience(eid)
        self.assertEqual([], dataset.search_experiences("disk"))

    def test_search_empty_query_returns_nothing(self):
        dataset.record_experience("pitfall", situation="disk full", action="clean")
        self.assertEqual([], dataset.search_experiences(""))


if __name__ == "__main__":
    unittest.main()