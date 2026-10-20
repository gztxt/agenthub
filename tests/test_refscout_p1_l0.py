#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1 参考情报（RefScout）—— 不联网、不起服务。

守 P0 实测过的四个坑（每个都是「看着在做事，其实没保证」）：

1. **主臂不许带 `in:readme`**：带上的那次 `websocket pty` top1 是 9.6 万星清单。
   资料臂只允许进 `list/doc` 桶，不得参与实现对比。
2. **「最新」与「最相关」不合并**：新鲜臂必须独立命名（`fresh`），
   且其排序键是 `updated` 而不是 `stars` —— 两者合成一个排序就必然出作业仓噪声。
3. **许可证是硬闸门**：GPL/AGPL 只允许「设计参考」，未知许可证默认不得复用代码。
   卡面必须出现这两类提示，缺一即视为闸门失效。
4. **降级要写在卡上**：重排失败 ≠ 任务失败，但卡上必须写明「仅词法 / 原因」，
   否则旧卡会冒充「已语义筛选」。

另守两条状态口径：
5. `as_of` 与 `next_refresh` 必须同时存在（缺一个就无法判断「旧卡」）。
6. 刷新 diff 只报事实（新增/移除/分数变化），不做「变好了」的判断。
"""
import json
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db          # noqa: E402
import refscout    # noqa: E402


class TestQueries(unittest.TestCase):
    def test_main_arm_is_narrow(self):
        arms = refscout.build_queries(["terminal", "websocket", "pty"], "终端", 20, 180)
        by = {a[0]: a for a in arms}
        self.assertIn("in:name,description,topics", by["core"][1])
        self.assertNotIn("in:readme", by["core"][1])
        self.assertIn("fork:false", by["core"][1])
        self.assertIn("archived:false", by["core"][1])

    def test_reading_arm_is_separate_and_only_place_with_readme(self):
        arms = refscout.build_queries(["terminal", "websocket"], "终端", 20, 180)
        readme_arms = [a for a in arms if "in:readme" in a[1]]
        self.assertEqual([a[0] for a in readme_arms], ["reading"])

    def test_fresh_arm_sorts_by_updated_not_stars(self):
        arms = {a[0]: a for a in refscout.build_queries(["alpha", "beta"], "x", 20, 90)}
        self.assertEqual(arms["fresh"][2], "updated")
        self.assertEqual(arms["core"][2], "stars")
        self.assertIn("pushed:>", arms["fresh"][1])

    def test_cn_arm_only_when_module_has_cjk(self):
        cn = {a[0] for a in refscout.build_queries(["ports", "forward"], "端口转发", 20, 180)}
        en = {a[0] for a in refscout.build_queries(["ports", "forward"], "port forwarding", 20, 180)}
        self.assertIn("cn", cn)
        self.assertNotIn("cn", en)

    def test_tokens_dedup_and_stopwords(self):
        toks = refscout.tokens_of(["the terminal", "terminal websocket"])
        self.assertEqual(toks, ["terminal", "websocket"])


class TestBuckets(unittest.TestCase):
    def test_awesome_goes_to_list_not_impl(self):
        self.assertEqual(refscout.classify(
            {"name": "awesome-tunneling", "description": "curated list"}), "list")
        self.assertEqual(refscout.classify(
            {"name": "docs", "description": ""}), "doc")
        self.assertEqual(refscout.classify(
            {"name": "gotty", "description": "Share your terminal as a web app"}), "impl")


class TestJudgeAndGist(unittest.TestCase):
    def test_unjudged_sits_between_read_and_skip(self):
        self.assertLess(ap := refscout._judge_rank("read"), refscout._judge_rank("unjudged"))
        self.assertLess(ap, refscout._judge_rank("skip"))

    def test_gist_strips_badges_and_links(self):
        g = refscout._gist("[![ci](x.png)](y) # Title\n\nA **skill** registry for agents.")
        self.assertNotIn("ci", g)
        self.assertNotIn("**", g)
        self.assertIn("skill registry", g)

    def test_render_excludes_unjudged_from_borrow_list(self):
        cand = {"repo": "u/1", "url": "u", "desc": "d", "bucket": "impl",
                "seen": [], "func": 0.5, "impl": 0.5, "design": 0.5, "coverage": 0.5,
                "stars": 10, "license": "MIT", "copyleft": False, "fresh_days": 5,
                "arch_docs": False, "branch": "main", "head": "", "code_files": [],
                "topics": [], "sample_paths": [], "judge": "unjudged",
                "why": "模型未返回该条判定", "llm": None}
        meta = {"arms": [], "pool_size": 1, "rerank": True, "rerank_model": "m",
                "rerank_paid": False, "rerank_error": "", "degraded": [], "token": True}
        md = refscout.render_md("m", ["k"], "", meta, [cand], 1.0, "a", "b")
        self.assertIn("未入选", md)
        self.assertNotIn("## 可借鉴点", md.split("未入选")[0] + "x")
        self.assertNotIn("### u/1", md)


class TestGates(unittest.TestCase):
    def _card(self, **over):
        cand = {"repo": "x/y", "url": "https://github.com/x/y", "desc": "d",
                "bucket": "impl", "seen": ["core"], "func": 0.8, "impl": 0.7,
                "design": 0.6, "coverage": 0.8, "stars": 100, "license": "MIT",
                "copyleft": False, "fresh_days": 10, "arch_docs": False,
                "branch": "main", "head": "abc123", "code_files": [],
                "topics": [], "sample_paths": [], "judge": "read", "why": "w",
                "llm": {}}
        cand.update(over)
        meta = {"arms": [{"name": "core", "ok": True, "count": 1, "ms": 5,
                          "cached": False}], "pool_size": 1, "rerank": True,
                "rerank_model": "m", "rerank_paid": False, "rerank_error": "",
                "degraded": [], "token": True}
        return refscout.render_md("m", ["k"], "", meta, [cand], 1.0,
                                  "2026-10-21T00:00:00+00:00",
                                  "2026-11-20T00:00:00+00:00")

    def test_copyleft_is_flagged(self):
        md = self._card(license="GPL-3.0", copyleft=True)
        self.assertIn("copyleft", md)
        self.assertIn("不得摘录代码", md)

    def test_unknown_license_is_flagged(self):
        md = self._card(license="UNKNOWN")
        self.assertIn("许可证未识别", md)

    def test_no_rerank_is_stated(self):
        meta = {"arms": [], "pool_size": 0, "rerank": False, "rerank_model": "",
                "rerank_paid": False, "rerank_error": "openrouter/free:only-thinking",
                "degraded": [], "token": True}
        md = refscout.render_md("m", ["k"], "", meta, [], 1.0, "a", "b")
        self.assertIn("仅词法", md)
        self.assertIn("only-thinking", md)

    def test_asof_and_next_refresh_required(self):
        md = self._card()
        self.assertIn("as of：2026-10-21", md)
        self.assertIn("下次刷新：2026-11-20", md)


class TestCardsDb(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        db.init_db(self.tmp / "agents.db")
        refscout.ensure_schema()

    def test_insert_list_get_and_refresh_in_place(self):
        refscout._card_upsert("c1", "终端", ["terminal"], "python", "lexical",
                              "2026-10-21T00:00:00+00:00", "2026-11-20T00:00:00+00:00",
                              3.0, {"rerank": False, "degraded": []},
                              [{"repo": "a/b", "bucket": "impl", "func": 0.5}], "# md")
        items = refscout.list_cards()["items"]
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["top"]["repo"], "a/b")
        # 同一 card_id 再写 = 原地更新，不是新增
        refscout._card_upsert("c1", "终端", ["terminal"], "python", "reranked",
                              "2026-10-22T00:00:00+00:00", "2026-11-21T00:00:00+00:00",
                              4.0, {"rerank": True, "degraded": []},
                              [{"repo": "a/b", "bucket": "impl", "func": 0.9}], "# md2")
        self.assertEqual(len(refscout.list_cards()["items"]), 1)
        got = refscout.card_view("c1")
        self.assertEqual(got["status"], "reranked")
        self.assertEqual(got["candidates"][0]["func"], 0.9)

    def test_diff_reports_added_removed_changed(self):
        old = {"candidates": json.dumps([
            {"repo": "a/1", "func": 0.5}, {"repo": "a/2", "func": 0.9}])}
        new = [{"repo": "a/1", "func": 0.9}, {"repo": "a/3", "func": 0.4}]
        d = refscout._diff_candidates(old, new)
        self.assertEqual(d["added"], ["a/3"])
        self.assertEqual(d["removed"], ["a/2"])
        self.assertEqual(len(d["changed"]), 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
