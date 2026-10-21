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


class TestQueryExpansion(unittest.TestCase):
    """查询扩展（PT-20261021-04 召回实验的第 6 轮）：只改查询与匹配口径，不动池。

    守三条判据：
    1. 扩展臂**一条只放一个概念**（多词 AND 是实测召回的主因：robfig/cron 的描述
       只有 "a cron library for go"，`cron scheduler task` 必空）。
    2. 连字符/下划线归一，否则 topics 里的 `full-text-search` 永远匹配不到
       `full text search`。
    3. 同义词只给部分学分；原词命中才满分 —— 满学分会让日历/k8s 类仓占住 top5。
    """

    def test_expansion_arm_single_concept_and_capped(self):
        arms = refscout.build_queries(["health", "uptime", "monitoring", "watchdog"],
                                      "服务健康探针", 20, 180)
        kw = [a for a in arms if a[0].startswith("kw")]
        self.assertTrue(kw, "应至少有一条扩展臂")
        self.assertLessEqual(len(kw), 3, "扩展臂数量必须封顶（搜索配额 30/min）")
        self.assertTrue(all(len(a[1].split(" in:")[0].split()) == 1 for a in kw),
                        "扩展臂不得把多个概念 AND 在一起")

    def test_multiword_keyword_queried_alone(self):
        arms = refscout.build_queries(["full-text search", "search engine", "indexing"],
                                      "全文检索", 20, 180)
        qs = [a[1] for a in arms if a[0].startswith("kw")]
        # `full-text search` 与 core 臂的 `full text search` 归一后是同一条查询
        # （实测 top10 完全相同、total 361 vs 391），不再重复发一条烧配额；
        # 另外两条概念必须各自成臂。
        self.assertFalse(any(q.startswith("full-text search ") for q in qs))
        self.assertTrue(any(q.startswith("search engine ") for q in qs))
        self.assertTrue(any(q.startswith("indexing ") for q in qs))

    def test_hyphen_normalization(self):
        self.assertEqual(refscout._norm_text("full-text_search"), "full text search")
        self.assertTrue(refscout._hit("a full text search engine", "full text search"))


class TestMorphologyAndArmDedup(unittest.TestCase):
    """第七轮：形态归一只做关键词侧是不够的 + 臂去重必须按词元集。

    三个实测事实：
    1. loki 的描述是 `for logs`、topics 是 `logging`，关键词 `log` 永远撞不上 ——
       变体只在关键词一侧生成，被匹配文本一侧从没展开过；
    2. `_forms_of("logging")` 旧实现得 `logg`（双写辅音没去），差一个字母就永不命中；
    3. 去重按**原始查询串**：用户写 `Log Aggregation` 时与 `pair` 臂重复发同一查询
       （白烧 30/min 配额），而短语型关键词与 `pair` 字面全等时 kw 臂被整条吃掉。
    """

    def test_forms_double_consonant(self):
        self.assertIn("log", refscout._forms_of("logging"))
        self.assertNotIn("logg", refscout._forms_of("logging"))
        self.assertIn("process", refscout._forms_of("processing"),
                      "-ss 不能去掉，否则 processing → proces")

    def test_blob_expansion_matches_plural_and_gerund(self):
        blob = refscout._expand_blob("like prometheus but for logs | logging")
        self.assertTrue(refscout._hit(blob, "log"))
        self.assertFalse(refscout._hit(blob, "logic"), "词边界仍必须生效")

    def test_expansion_does_not_create_fake_phrase(self):
        blob = refscout._expand_blob("aggregation logs")
        self.assertFalse(refscout._hit(blob, "log aggregation"),
                         "变体段不得拼出原文没有的短语")

    def test_dedup_by_token_set_not_raw_string(self):
        arms = refscout.build_queries(["Log Aggregation"], "日志聚合", 20, 180)
        # 判等键必须是 (查询串, 排序键)：`core` 与 `fresh` 查询串相同但按 stars/updated
        # 分别排，是两条不同的臂；`reading` 走 in:readme，也是不同查询。
        keys = [(a[1], a[2]) for a in arms]
        self.assertEqual(len(keys), len(set(keys)), "不得发两条等价查询")
        self.assertFalse(any(a[1].startswith("Log Aggregation in:") for a in arms),
                         "用户大写不应绕过去重")

    def test_head_word_arm_off_by_default(self):
        # 实测开臂能把 loki 从「未入池」拉到 #1，但池 66→82 把 ttyd 挤出 top5，
        # v1 子集 0.600→0.533 ⇒ 默认关。这里钉住默认值，防止有人「顺手打开」。
        arms = refscout.build_queries(["log aggregation", "log collector"], "日志聚合", 20, 180)
        self.assertNotIn("head0", {a[0] for a in arms})

    def test_head_word_arm_when_enabled(self):
        import os
        from unittest import mock
        with mock.patch.dict(os.environ, {"REF_HEAD_ARM": "1"}):
            arms = refscout.build_queries(["log aggregation", "log collector"], "日志聚合", 20, 180)
        by_name = {a[0]: a[1] for a in arms}
        self.assertIn("head0", by_name)
        self.assertTrue(by_name["head0"].startswith("log in:name"))

    def test_word_boundary_no_substring_noise(self):
        self.assertFalse(refscout._hit("deep research tool", "search"),
                         "`research` 不应被当成 `search` 命中")

    def test_synonym_gets_partial_credit(self):
        groups = refscout.alias_groups(["health"])
        own, syn = groups[0]
        self.assertIn("health", own)
        self.assertIn("uptime", syn)
        cov_syn, hit_syn = refscout._concept_cov("uptime monitoring", groups, [1.0])
        cov_own, hit_own = refscout._concept_cov("health check", groups, [1.0])
        self.assertEqual(hit_syn, 1)
        self.assertAlmostEqual(cov_syn, refscout.SYN_CREDIT)
        self.assertAlmostEqual(cov_own, 1.0)
        self.assertGreater(cov_own, cov_syn)

    def test_pool_structure_untouched(self):
        """扩展只许改查询/匹配，**不许顺手扩池**（§7.4 的教训：三旋钮必须同调）。"""
        self.assertEqual(refscout.SEARCH_PER_PAGE, 10)
        self.assertEqual(refscout.MAX_CANDIDATES, 8)


class TestJudgeRankLadder(unittest.TestCase):
    """判定档位的**严格顺序**：use/fork < read < 未判定 < skip。

    2026-10-21 修：旧实现 `unjudged`=3 而 `skip`=2，未判定排在被明确否决之后。
    """

    def test_full_ladder(self):
        order = sorted(["skip", "unjudged", "read", "use", "fork"],
                       key=refscout._judge_rank)
        self.assertEqual(order[:2], ["use", "fork"])
        self.assertEqual(order[2:], ["read", "unjudged", "skip"])

    def test_unknown_judge_is_not_below_skip(self):
        for j in ("", "weird", None):
            self.assertLess(refscout._judge_rank(j), refscout._judge_rank("skip"),
                            "未知判定不得排到已判 skip 之下（不能把没看过的当已否决）")


class TestPartialRerankOrdering(unittest.TestCase):
    """重排只返回部分条目时，未判定的仓必须排在**已判 skip** 之前。"""

    def _cand(self, repo, func):
        return {"repo": repo, "bucket": "impl", "stars": 100 + func * 10,
                "desc": "d", "topics": [], "readme": "", "readme_gist": "",
                "sample_paths": [], "item": {}, "judge": "unjudged", "why": "",
                "func": func, "impl": func, "design": func}

    def test_unjudged_above_skip(self):
        ranked = [self._cand("a/skip", 0.9), self._cand("b/unjudged", 0.8),
                  self._cand("c/use", 0.1)]
        orig = refscout.rerank
        refscout.rerank = lambda module, impls, allow_paid: (
            {"a/skip": {"func": 0.0, "impl": 0.0, "design": 0.0,
                        "judge": "skip", "why": "相邻"},
             "c/use": {"func": 0.9, "impl": 0.9, "design": 0.9,
                       "judge": "use", "why": "同题"}}, "", "fake", [])
        try:
            err, used, _ = refscout.apply_rerank("模块", ranked, False)
        finally:
            refscout.rerank = orig
        self.assertEqual(err, "")
        self.assertEqual([c["repo"] for c in ranked], ["c/use", "b/unjudged", "a/skip"])


class TestSafetyRouter(unittest.TestCase):
    """免费池会把 prompt 路由到内容审核模型，必须识别并立刻收手。"""

    def test_detects_safety_label(self):
        self.assertTrue(refscout._is_safety_reply("User Safety: safe"))
        self.assertTrue(refscout._is_safety_reply(
            " User Safety: unsafe\nSafety Categories: Malware, Needs Caution"))
        self.assertFalse(refscout._is_safety_reply('{"items": []}'))
        self.assertFalse(refscout._is_safety_reply(""))

    def test_rerank_stops_instead_of_burning_budget(self):
        calls = []

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append(model)
            return "User Safety: safe", "", "end_turn"

        cand = {"repo": "a/b", "stars": 1, "desc": "x", "topics": [],
                "readme_gist": "", "sample_paths": [], "item": {}}
        orig = refscout._llm_text
        refscout._llm_text = fake
        try:
            out, err, used, attempts = refscout.rerank("模块", [cand], allow_paid=False)
        finally:
            refscout._llm_text = orig
        self.assertFalse(out)
        self.assertIn("safety-router", err)
        self.assertEqual(len(calls), 1, "同一免费池换模型只会再撞一次，必须命中即停")
        self.assertEqual(attempts[0]["error"], "safety-router")

    def test_paid_model_safety_reply_does_not_block_fallback(self):
        """付费档若也返回标签，不能就此放弃免费兜底（付费与免费不是同一池）。"""
        calls = []

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append(model)
            if len(calls) == 1:
                return "User Safety: safe", "", "end_turn"
            return ('{"items":[{"repo":"a/b","func":0.9,"impl":0.8,'
                    '"design":0.7,"judge":"use","why":"同题"}]}', "", "end_turn")

        cand = {"repo": "a/b", "stars": 1, "desc": "x", "topics": [],
                "readme_gist": "", "sample_paths": [], "item": {}}
        orig = refscout._llm_text
        refscout._llm_text = fake
        try:
            out, err, used, attempts = refscout.rerank("模块", [cand], allow_paid=True)
        finally:
            refscout._llm_text = orig
        self.assertIn("a/b", out)
        self.assertEqual(len(calls), 2)
        self.assertEqual(attempts[0]["error"], "safety-router")


class TestSingleKeyword(unittest.TestCase):
    """单关键词不得被「至少命中 2 个概念」的过滤吃光。

    门槛写成常数 2 时，概念数只有 1 ⇒ `matched<=1` 恒成立 ⇒ 实现桶必空。
    实测 `keywords=["cron"]` 对描述 "a cron library for go" 返回 0 条。
    """

    def test_single_keyword_survives_filter(self):
        item = {"full_name": "robfig/cron", "name": "cron",
                "description": "a cron library for go", "topics": ["cron"],
                "stargazers_count": 12000, "html_url": "https://github.com/robfig/cron",
                "default_branch": "master", "pushed_at": "2026-10-01T00:00:00Z",
                "language": "Go", "license": {"spdx_id": "MIT"}, "archived": False}

        def fake_get(url, ttl_s=None, limiter=None):
            if "/search/repositories" in url:
                return {"total_count": 1, "items": [item]}, {"ok": True, "ms": 1}
            return None, {"ok": True, "ms": 1}

        orig_get, orig_enrich = refscout.gh_get, refscout._enrich
        refscout.gh_get = fake_get
        refscout._enrich = lambda full, branch: {
            "readme": "a cron library for go", "paths": ["cron.go"], "head": "abc"}
        try:
            got = refscout.collect("定时任务", ["cron"], "", 0, 3650, [],
                                   False, do_rerank=False)
        finally:
            refscout.gh_get, refscout._enrich = orig_get, orig_enrich
        impls = [c for c in got["candidates"] if c["bucket"] == "impl"]
        self.assertEqual([c["repo"] for c in impls], ["robfig/cron"])


class TestRerankNormalization(unittest.TestCase):
    """模型输出的大小写不能改变判定：仓库名按规范名回填，judge 统一小写。"""

    def test_repo_case_and_judge_case(self):
        cand = {"repo": "robfig/cron", "stars": 1, "desc": "cron", "topics": [],
                "readme_gist": "", "sample_paths": [], "item": {}}

        def fake(prompt, model, timeout=0, max_tokens=0):
            return ('{"items":[{"repo":"Robfig/Cron","func":0.9,"impl":0.8,'
                    '"design":0.7,"judge":"USE","why":"同题"}]}', "", "end_turn")

        orig = refscout._llm_text
        refscout._llm_text = fake
        try:
            out, err, _used, _att = refscout.rerank("定时任务", [cand], allow_paid=True)
        finally:
            refscout._llm_text = orig
        self.assertEqual(err, "")
        self.assertIn("robfig/cron", out)
        self.assertEqual(out["robfig/cron"]["judge"], "use")
        self.assertEqual(refscout._judge_rank("USE"), 0)
        self.assertEqual(refscout._judge_rank(" Skip "), 3)


class TestRefreshPaidIntent(unittest.TestCase):
    """重跑的付费档：显式参数 > 上次**请求**的意图 > 上次实际用的档。

    `rerank_paid` 是「上一次真的用上付费」，付费失败回落免费后为 False；只认它
    会把卡永久锁在免费档（免费池又被审核路由拦截），只能重新建卡才能恢复。
    """

    def _row(self, meta):
        return {"id": "c1", "module": "模块", "keywords": '["cron"]', "lang": "",
                "meta": json.dumps(meta)}

    def test_requested_paid_survives_free_fallback(self):
        body = refscout.refresh_body(
            self._row({"requested_paid": True, "rerank_paid": False}), None)
        self.assertTrue(body.allow_paid)
        self.assertEqual(body.trigger, "manual")
        self.assertEqual(body.refresh_of, "c1")

    def test_explicit_flag_wins(self):
        row = self._row({"requested_paid": True, "rerank_paid": True})
        self.assertFalse(refscout.refresh_body(row, False).allow_paid)
        self.assertTrue(refscout.refresh_body(self._row({}), True).allow_paid)

    def test_legacy_card_falls_back_to_used_model(self):
        self.assertTrue(refscout.refresh_body(self._row({"rerank_paid": True}), None).allow_paid)
        self.assertFalse(refscout.refresh_body(self._row({}), None).allow_paid)

    def test_auto_trigger_is_stated_on_card(self):
        meta = {"arms": [], "pool_size": 1, "rerank": False, "rerank_error": "",
                "degraded": [], "token": True, "trigger": "auto"}
        md = refscout.render_md("m", ["k"], "", meta, [], 1.0, "a", "b")
        self.assertIn("定时自动刷新", md)


class TestPaidSampling(unittest.TestCase):
    """付费档：上限 60s + 同一 prompt 采样取多数。

    2026-10-21 生产实测两件事各自成立：
    ① `cap = 60 if model == llm.MODEL else 25` —— `llm.MODEL` 已改成交互链路的
       `alibaba/qwen3.8-flash`，与付费档无关 ⇒ 永远走 25s 分支，docstring 的 60s 从未生效；
    ② 同一张卡两轮判词相反（一轮三候选全 skip，一轮 HelloGitHub=read）
       ⇒ 单次判定不可当结论，必须采样。
    """

    cand = {"repo": "a/b", "stars": 1, "desc": "x", "topics": [],
            "readme_gist": "", "sample_paths": [], "item": {}}
    good = ('{"items":[{"repo":"a/b","func":0.9,"impl":0.8,"design":0.7,'
            '"judge":"read","why":"同题"}]}')
    skip = ('{"items":[{"repo":"a/b","func":0,"impl":0,"design":0,'
            '"judge":"skip","why":"无"}]}')

    def setUp(self):
        self.orig = (refscout._llm_text, refscout._sample_count,
                     refscout.llm.memory_model, refscout.llm.free_fallback_model)
        refscout.llm.memory_model = lambda: "free/a"
        refscout.llm.free_fallback_model = lambda: "free/b"
        self.paid = refscout._ref_model()
        self.samples = 3
        refscout._sample_count = lambda: self.samples

    def tearDown(self):
        (refscout._llm_text, refscout._sample_count, refscout.llm.memory_model,
         refscout.llm.free_fallback_model) = self.orig

    def test_single_sample_still_gets_60s(self):
        """采样关掉时付费档仍必须拿 60s（cap 判断不能再看 llm.MODEL）。"""
        self.samples = 1
        calls = []

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append((model, timeout))
            return self.good, "", "end_turn"

        refscout._llm_text = fake
        out, err, used, _att = refscout.rerank("m", [self.cand], allow_paid=True)
        self.assertEqual(err, "")
        self.assertEqual(used, self.paid)
        self.assertIn("a/b", out)
        self.assertEqual(calls[0], (self.paid, 60))

    def test_three_samples_take_majority(self):
        calls = []
        seq = [self.good, self.skip, self.good]

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append(model)
            return seq[len(calls) - 1], "", "end_turn"

        refscout._llm_text = fake
        out, err, _used, attempts = refscout.rerank("m", [self.cand], allow_paid=True)
        self.assertEqual(err, "")
        self.assertEqual(len(calls), 3, "默认采 3 次")
        self.assertEqual(out["a/b"]["judge"], "read", "2:1 取多数")
        self.assertEqual(out["a/b"]["func"], 0.9)
        self.assertEqual([a["sample"] for a in attempts], [1, 2, 3],
                         "每次采样都要留痕，卡面 attempts 能看出采了几次")
        self.assertEqual([a["judges"]["a/b"] for a in attempts],
                         ["read", "skip", "read"],
                         "逐次判词必须留档，否则「2:1」无从复核")

    def test_tie_takes_stricter_verdict(self):
        """2 次里 read/skip 各一票 ⇒ 按 skip（没价值就排后，不让乐观票顶上去）。"""
        self.samples = 2
        seq = [self.good, self.skip]

        def fake(prompt, model, timeout=0, max_tokens=0):
            return seq.pop(0), "", "end_turn"

        refscout._llm_text = fake
        out, _err, _used, _att = refscout.rerank("m", [self.cand], allow_paid=True)
        self.assertEqual(out["a/b"]["judge"], "skip")

    def test_repo_seen_once_in_three_is_dropped(self):
        one = ('{"items":[{"repo":"a/b","func":0.9,"impl":0.8,"design":0.7,'
               '"judge":"read","why":"x"},{"repo":"b/c","func":0.5,"impl":0.5,'
               '"design":0.5,"judge":"read","why":"y"}]}')
        only_other = ('{"items":[{"repo":"b/c","func":0.5,"impl":0.5,'
                      '"design":0.5,"judge":"read","why":"y"}]}')
        seq = [one, only_other, only_other]
        cands = [self.cand, {"repo": "b/c", "stars": 1, "desc": "y", "topics": [],
                             "readme_gist": "", "sample_paths": [], "item": {}}]

        def fake(prompt, model, timeout=0, max_tokens=0):
            return seq.pop(0), "", "end_turn"

        refscout._llm_text = fake
        out, _err, _used, _att = refscout.rerank("m", cands, allow_paid=True)
        self.assertNotIn("a/b", out, "只被 1/3 采到 ⇒ 不拿单票当结论")
        self.assertIn("b/c", out)

    def test_one_timeout_costs_one_vote(self):
        seq = [("", "TimeoutError", ""), (self.good, "", "end_turn"),
               (self.good, "", "end_turn")]

        def fake(prompt, model, timeout=0, max_tokens=0):
            return seq.pop(0)

        refscout._llm_text = fake
        out, err, used, attempts = refscout.rerank("m", [self.cand], allow_paid=True)
        self.assertEqual(err, "")
        self.assertEqual(used, self.paid)
        self.assertIn("a/b", out)
        self.assertEqual(attempts[0]["error"], "TimeoutError")

    def test_all_samples_fail_then_free_fallback(self):
        self.samples = 3
        calls = []

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append(model)
            return ("", "TimeoutError", "") if model == self.paid else (self.good, "", "end_turn")

        refscout._llm_text = fake
        out, err, used, _att = refscout.rerank("m", [self.cand], allow_paid=True)
        self.assertEqual(used, "free/a")
        self.assertIn("a/b", out)
        self.assertEqual(calls, [self.paid] * 3 + ["free/a"])
        self.assertEqual(err, "")

    def test_safety_stops_sampling_without_retry(self):
        """内容被审核模型截住：多采几次结果一样，只留一次记录。"""
        calls = []

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append(model)
            return "User Safety: safe", "", "end_turn"

        refscout._llm_text = fake
        out, err, _used, attempts = refscout.rerank("m", [self.cand], allow_paid=True)
        self.assertFalse(out)
        self.assertIn("safety-router", err)
        self.assertEqual(calls.count(self.paid), 1, "付费档只发一次就收工")
        self.assertEqual(len([a for a in attempts if a["model"] == self.paid]), 1)

    def test_free_arm_is_not_sampled(self):
        calls = []

        def fake(prompt, model, timeout=0, max_tokens=0):
            calls.append((model, timeout))
            return self.good, "", "end_turn"

        refscout._llm_text = fake
        _out, err, used, _att = refscout.rerank("m", [self.cand], allow_paid=False)
        self.assertEqual(err, "")
        self.assertEqual(used, "free/a")
        self.assertEqual(calls, [("free/a", 25)], "免费档一次 25s，不采样")


class TestMajorityAggregation(unittest.TestCase):
    """「取多数」的口径：多数票 + 中位数，平票往严格方向落。"""

    def _rec(self, func, impl, design, judge):
        return {"func": func, "impl": impl, "design": design,
                "judge": judge, "why": judge}

    def test_median_resists_one_outlier(self):
        votes = [{"a/b": self._rec(0.8, 0.8, 0.6, "read")},
                 {"a/b": self._rec(0.0, 0.0, 0.0, "skip")},
                 {"a/b": self._rec(0.9, 0.9, 0.7, "read")}]
        got = refscout._majority(votes)
        self.assertEqual(got["a/b"]["judge"], "read")
        self.assertEqual(got["a/b"]["func"], 0.8, "三次取中位数，不被 0 拉低")
        self.assertEqual(got["a/b"]["impl"], 0.8)

    def test_majority_verdict_can_be_use(self):
        votes = [{"a/b": self._rec(0.9, 0.9, 0.9, "use")},
                 {"a/b": self._rec(0.9, 0.9, 0.9, "use")},
                 {"a/b": self._rec(0.1, 0.1, 0.1, "read")}]
        got = refscout._majority(votes)
        self.assertEqual(got["a/b"]["judge"], "use")

    def test_single_vote_dataset_keeps_everything(self):
        votes = [{"a/b": self._rec(0.5, 0.5, 0.5, "read")}]
        got = refscout._majority(votes)
        self.assertIn("a/b", got, "只有一次有效采样时，那一票就是结论（need=1）")

    def test_empty_votes(self):
        self.assertEqual(refscout._majority([]), {})

    def test_sample_count_env_is_clamped(self):
        import os
        old = os.environ.get("REF_RERANK_SAMPLES")
        try:
            for raw, want in (("1", 1), ("3", 3), ("9", 5), ("0", 1), ("x", 3)):
                os.environ["REF_RERANK_SAMPLES"] = raw
                self.assertEqual(refscout._sample_count(), want, raw)
        finally:
            if old is None:
                os.environ.pop("REF_RERANK_SAMPLES", None)
            else:
                os.environ["REF_RERANK_SAMPLES"] = old
class TestCardDisclosure(unittest.TestCase):
    """卡面必须写清楚「关键词哪来的」与「有没有代码标识符臂」。

    `keyword_source=fallback` 意味着模型抽词失败、走了内置词典，召回往往整体跑偏；
    不写出来，用户只会看到一张「检索不准」的卡而不知道该改关键词。
    """

    def _md(self, **meta_kw):
        meta = {"arms": [], "pool_size": 1, "rerank": False, "rerank_error": "",
                "degraded": [], "token": True}
        meta.update(meta_kw)
        return refscout.render_md("m", ["cron"], "", meta, [], 1.0, "a", "b")

    def test_fallback_keywords_are_flagged(self):
        self.assertIn("退回内置中文词典", self._md(keyword_source="fallback"))

    def test_llm_keywords_are_labeled(self):
        md = self._md(keyword_source="llm")
        self.assertIn("关键词来源：模型从模块名抽取", md)
        self.assertNotIn("内置中文词典", md)

    def test_user_keywords_stay_quiet(self):
        self.assertNotIn("关键词来源", self._md(keyword_source="given"))

    def test_identifiers_arm_is_listed(self):
        md = self._md(identifiers=["node-pty", "websocket"])
        self.assertIn("代码标识符臂：node-pty、websocket", md)


class TestIdentifierLimit(unittest.TestCase):
    """代码搜索限额 10 次/分：payload 与 collect 的切片必须一致（都是 2）。

    旧实现 payload 切 3、collect 只用前 2 ⇒ 第三个标识符**静默丢弃**，卡上却看不出来。
    """

    def test_payload_keeps_two(self):
        rows = []

        def fake_execute(sql, args):
            rows.append((sql, args))

        orig = (refscout._POOL.submit, refscout.db.query, refscout.db.execute, refscout.token)
        refscout._POOL.submit = lambda fn, job_id: None
        refscout.db.query = lambda *a, **k: []
        refscout.db.execute = fake_execute
        refscout.token = lambda: "t"
        try:
            refscout.submit(refscout.ScoutIn(module="终端", identifiers=["a", "b", "c"]))
        finally:
            (refscout._POOL.submit, refscout.db.query,
             refscout.db.execute, refscout.token) = orig
        payload = json.loads(rows[0][1][3])
        self.assertEqual(payload["identifiers"], ["a", "b"])

    def test_collect_reads_two(self):
        import inspect
        self.assertIn("identifiers[:2]", inspect.getsource(refscout.collect))


if __name__ == "__main__":
    unittest.main(verbosity=2)
