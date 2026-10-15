#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：经验库作为记忆联邦的一路源（P0-1 D18）。

【这条判据治什么】
经验库（`experiences` 表 + Dreaming 产出）若只躺在 DB 里，就不是「记忆」——
D18 要求它**进入记忆联邦**：能作为 `sources=` 的一路被检索、能进注入段、能被 probe。
本文件把这三件事钉住，并守住两条口径：

1. **只召回活跃条**：退役（`retired_at` 非空）的经验**不得**从检索路回来。
2. **源形状与其它联邦源一致**：item 必须有 `source/id/title/content/category`，
   否则 RRF 与前端「整理分类」面板会对不上一路。

分层口径（tests/tiers.py）：临时 db + 假数据，不连网、不起服务。
"""
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import dataset  # noqa: E402
import db       # noqa: E402
import memfed   # noqa: E402

SID = "experiences"


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0expfed-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        dataset.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved


class TestRegistry(_Base):
    def test_source_is_registered_and_enabled(self):
        self.assertIn(SID, memfed.REGISTRY)
        src = memfed.REGISTRY[SID]
        self.assertTrue(src.enabled, "经验库源应默认启用（本地 sqlite，成本低）")
        self.assertEqual(src.kind, "hub_sqlite")

    def test_source_appears_in_enabled_ids(self):
        self.assertIn(SID, memfed.enabled_ids())


class TestSearch(_Base):
    def test_search_returns_experience_items(self):
        dataset.record_experience("pitfall", situation="disk full", action="clean tmp",
                                  precondition="NAS", source_trace_ids=["tr1"])
        dataset.record_experience("recipe", situation="other", action="x")
        r = memfed._search_experiences("disk", 10, 1.0)
        self.assertTrue(r["ok"])
        self.assertEqual(1, r["count"])
        it = r["items"][0]
        self.assertEqual(SID, it["source"])
        self.assertIn("clean tmp", it["content"])
        self.assertIn("disk full", it["title"])

    def test_search_excludes_retired(self):
        eid = dataset.record_experience("pitfall", situation="disk full", action="clean")
        dataset.retire_experience(eid)
        r = memfed._search_experiences("disk", 10, 1.0)
        self.assertTrue(r["ok"])
        self.assertEqual(0, r["count"], "退役的经验不得从检索路回来")

    def test_item_shape_matches_other_fed_sources(self):
        dataset.record_experience("pitfall", situation="s", action="a")
        it = memfed._search_experiences("s", 10, 1.0)["items"][0]
        for k in ("source", "id", "title", "content", "category"):
            self.assertIn(k, it)


class TestRecent(_Base):
    def test_recent_returns_newest(self):
        dataset.record_experience("pitfall", situation="first", action="a")
        dataset.record_experience("pitfall", situation="second", action="b")
        r = memfed._recent_experiences(10, 1.0)
        self.assertTrue(r["ok"])
        self.assertEqual(2, r["count"])


class TestProbe(_Base):
    def test_probe_reports_count(self):
        dataset.record_experience("pitfall", situation="s", action="a")
        p = memfed._probe_one(SID)
        self.assertTrue(p["ok"])
        self.assertEqual(1, p["count"])


if __name__ == "__main__":
    unittest.main()