#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：接单前置（intake）四查一简报。

守四条判据：

1. **「0 命中」与「那一路挂了」必须写成两种话**：两者对外都是空结果，混写就等于
   把「本机真没记过」和「记忆服务今天不可用」当成同一件事 —— 后者的处置是去修服务。
2. **参考库默认不查**，且查了要能说出为什么（`deep_reason` = manual / trigger:<词>）。
   接单阶段每单 20~90s + GitHub 额度，不写明原因就会被当成默认成本。
3. **技能只查本地、不装包**：接单不该产生写盘副作用。
4. **简报只是线索**：必须带「引用前回原文核对」的告诫与出处。
"""
import asyncio
import pathlib
import sys
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import intake  # noqa: E402


def _mem(n=1, degraded=None, error=""):
    if error:
        return {"ok": False, "count": 0, "items": [], "error": error}
    return {"ok": True, "count": n, "took_ms": 12,
            "degraded": degraded or [],
            "items": [{"layer": "L1", "source": "local", "category": "fact",
                       "content": "本机约定：改共享配置先备份"}] * n}


def _kb(n=1, degraded=None):
    return {"ok": True, "count": n, "took_ms": 30, "degraded": degraded or [],
            "items": [{"source": "xerj", "path": "wiki/x.md", "category": "doc",
                       "content": "相关段落", "score": 1.0}] * n}


def _skill(ok=True):
    return {"ok": ok, "name": "backup-before-edit" if ok else None,
            "route": "local" if ok else "",
            "reason": "bm25" if ok else "no local skill matched",
            "matched": 0.62, "matched_in": "description",
            "how_to_get": ('hub_skill_auto(task="改共享配置前先备份")'
                           if ok else "换词重试")}


class TestDeepReason(unittest.TestCase):
    def test_manual_wins(self):
        self.assertEqual(intake.deep_reason("看下日志", True, True), "manual")

    def test_trigger_word(self):
        self.assertEqual(intake.deep_reason("新增一个导出按钮", False, True), "trigger:新增")

    def test_auto_off(self):
        self.assertEqual(intake.deep_reason("新增一个导出按钮", False, False), "off")

    def test_plain_task_not_deep(self):
        self.assertEqual(intake.deep_reason("解释这行日志", False, True), "off")


class TestInterpret(unittest.TestCase):
    """任务解读：链路里「解读任务」这一步，必须可核对、0 成本、不做语义猜测。"""

    def test_plain_task_says_ref_off_and_why(self):
        it = intake.interpret("看下日志里的 502 来自哪里", "off")
        self.assertTrue(it["tokens"])
        self.assertIn("不查", it["ref"])
        self.assertIn("额度", it["ref"], "只说「不查」等于没解释，必须带成本理由")

    def test_trigger_reports_word(self):
        it = intake.interpret("新增导出按钮", "trigger:新增")
        self.assertEqual(it["trigger"], "新增")
        self.assertIn("新增", it["ref"])

    def test_manual_is_distinguishable(self):
        self.assertIn("手动", intake.interpret("x", "manual")["ref"])

    def test_escalate_wording(self):
        self.assertIn("thin", intake.interpret("x", "off", "auto")["escalate"])
        self.assertIn("无条件", intake.interpret("x", "off", "always")["escalate"])

    def test_brief_leads_with_intent(self):
        """解读必须在 §0 之前 —— 否则读者先看结论、后看系统怎么理解任务。"""
        res = {"task": "t", "caller": "", "deep_reason": "off",
               "as_of": "2026-10-22T00:00:00+00:00", "elapsed_s": 0.1,
               "memory": _mem(), "kb": _kb(), "skill": _skill(),
               "ref": {"ok": False, "error": "未查"}}
        md = intake.brief_md("看下日志里的 502", res)
        self.assertIn("任务解读", md)
        self.assertLess(md.index("任务解读"), md.index("## 0. 开工前先看"))


class TestBriefWording(unittest.TestCase):
    def _res(self, **over):
        res = {"task": "t", "caller": "cb", "deep_reason": "off",
               "as_of": "2026-10-22T00:00:00+00:00", "elapsed_s": 0.4,
               "memory": _mem(), "kb": _kb(), "skill": _skill(),
               "ref": {"ok": False, "error": "未查"}}
        res.update(over)
        return res

    def test_zero_hits_and_down_are_different(self):
        zero = intake.brief_md("t", self._res(memory=_mem(0)))
        down = intake.brief_md("t", self._res(memory=_mem(error="timed out")))
        self.assertIn("0 命中", zero)
        self.assertIn("不可用", down)
        self.assertIn("timed out", down)
        self.assertNotIn("不可用", zero, "0 命中不能写成检索不可用")

    def test_degraded_is_surfaced(self):
        md = intake.brief_md("t", self._res(kb=_kb(0, degraded=["xerj:timeout"])))
        self.assertIn("xerj:timeout", md)

    def test_skill_how_to_get(self):
        md = intake.brief_md("t", self._res())
        self.assertIn("backup-before-edit", md)
        self.assertIn("hub_skill_auto", md)
        self.assertNotIn("hub_skill_invoke", md,
                         "取正文不得要求调用方点名技能（应报任务让服务端自选）")

    def test_ref_off_is_explicit(self):
        self.assertIn("未查", intake.brief_md("t", self._res()))

    def test_ref_rows_rendered(self):
        ref = {"ok": True, "card_id": "abc123", "status": "done", "waited_s": 30,
               "rows": [{"repo": "a/b", "stars": 10, "func": 0.5, "impl": 0.6,
                         "judge": "read", "gist": "一句话"}]}
        md = intake.brief_md("t", self._res(ref=ref, deep_reason="trigger:选型"))
        self.assertIn("abc123", md)
        self.assertIn("a/b", md)
        self.assertIn("trigger:选型", md)

    def test_caveat_present(self):
        self.assertIn("引用前回原文核对", intake.brief_md("t", self._res()))

    def test_pending_ref_does_not_promise_instant_fetch(self):
        """卡还没落库时不能只写「稍后用卡取」—— 那时取必 404。"""
        ref = {"ok": False, "state": "pending", "card_id": "abc123",
               "status": "running", "waited_s": 8, "rows": [],
               "reason": intake.REF_PENDING_HINT}
        md = intake.brief_md("t", self._res(ref=ref, deep_reason="trigger:选型"))
        self.assertIn("abc123", md)
        self.assertIn("尚未落库", md)
        self.assertIn("404", md)


class TestRefJob(unittest.TestCase):
    """参考库：卡片落库地板实测 25~70s，远大于升级的 8s 预算。

    旧实现两处踩空：① 截止时 `card_view` 404 被 `except: pass` 吞成"没有候选"；
    ② `submit` 去重分支不回 card_id，`_ref` 拿 submit 的值 ⇒ 卡号丢成 null。
    """

    def setUp(self):
        self._old = (intake.refscout.submit, intake.refscout.job_view,
                     intake.refscout.token, intake.time.sleep)
        intake.refscout.token = lambda: "tok"
        intake.time.sleep = lambda s: None

    def tearDown(self):
        (intake.refscout.submit, intake.refscout.job_view,
         intake.refscout.token, intake.time.sleep) = self._old

    @staticmethod
    def _card():
        return {"candidates": [
            {"repo": "a/b", "stars": 9, "func": 0.5, "impl": 0.6, "judge": None,
             "readme_gist": "g", "bucket": "impl"},
            {"repo": "c/d", "bucket": "list"},
        ]}

    def test_pending_when_card_not_created(self):
        intake.refscout.submit = lambda body: {"job_id": "j1", "card_id": "c1"}
        intake.refscout.job_view = lambda jid: {"job_id": jid, "card_id": "c1",
                                                "status": "running", "card": None}
        r = intake._ref("tt", 0, "", True)
        self.assertEqual(r["state"], "pending")
        self.assertFalse(r["ok"])
        self.assertEqual(r["card_id"], "c1", "卡号必须带出，供调用方稍后取")
        self.assertIn("落库", r["reason"])
        self.assertNotIn("error", r, "卡还没建不是故障，不许写成 error")

    def test_deduped_submit_card_id_from_job_view(self):
        intake.refscout.submit = lambda body: {"job_id": "j1", "deduped": True}
        intake.refscout.job_view = lambda jid: {"card_id": "c0", "status": "running",
                                                "card": self._card()}
        r = intake._ref("tt", 0, "", True)
        self.assertEqual(r["card_id"], "c0", "去重分支必须从 job_view 补卡号")
        self.assertTrue(r["ok"])
        self.assertEqual([x["repo"] for x in r["rows"]], ["a/b"], "只收 impl 桶")

    def test_early_card_returns_without_waiting_rerank(self):
        calls = {"n": 0}

        def jv(jid):
            calls["n"] += 1
            return {"card_id": "c0", "status": "lexical", "card": self._card()}

        intake.refscout.submit = lambda body: {"job_id": "j1", "card_id": "c0"}
        intake.refscout.job_view = jv

        def no_sleep(_s):
            raise AssertionError("卡已落库就该立刻返回，不该再睡")

        intake.time.sleep = no_sleep
        r = intake._ref("tt", 8, "", True)
        self.assertTrue(r["ok"])
        self.assertEqual(calls["n"], 1)
        self.assertEqual(r["state"], "ok")

    def test_deep_still_waits_for_done(self):
        """deep 要判词分：卡已落库也不能提前返回词法卡。"""
        states = [{"status": "lexical", "card": self._card()},
                  {"status": "rerank", "card": self._card()},
                  {"status": "done", "card": self._card()}]
        seq = iter(states)

        def jv(jid):
            try:
                st = next(seq)
            except StopIteration:
                st = states[-1]
            return dict(st, card_id="c0")

        intake.refscout.submit = lambda body: {"job_id": "j1", "card_id": "c0"}
        intake.refscout.job_view = jv
        r = intake._ref("tt", 8, "", False)
        self.assertEqual(r["status"], "done")
        self.assertTrue(r["ok"])


class TestRunComposition(unittest.TestCase):
    """`run()` 只做组合：任何一路失败都必须仍在简报里出现，而不是整份 500。"""

    def _run(self, monkey, inp):
        old = (intake._mem, intake._kb, intake._skill, intake._ref)
        intake._mem, intake._kb, intake._skill, intake._ref = monkey
        try:
            return asyncio.run(intake.run(inp))
        finally:
            intake._mem, intake._kb, intake._skill, intake._ref = old

    def test_partial_failure_still_returns_brief(self):
        def bad(*a, **k):
            raise RuntimeError("boom")

        res = self._run((bad, lambda *a, **k: _kb(), bad, bad),
                        intake.IntakeIn(task="解释这行日志", auto_deep=False,
                                        escalate="off"))
        self.assertIn("brief", res)
        self.assertIn("boom", res["brief"])

    def test_ref_not_called_when_escalate_off(self):
        called = {"ref": 0}

        def ref(*a, **k):
            called["ref"] += 1
            return {"ok": False, "error": "x"}

        res = self._run((lambda *a, **k: _mem(), lambda *a, **k: _kb(),
                         lambda *a, **k: asyncio.sleep(0, result=_skill()), ref),
                        intake.IntakeIn(task="解释这行日志", auto_deep=False,
                                        escalate="off"))
        self.assertEqual(called["ref"], 0, "escalate=off 时不许摸参考库")
        self.assertFalse(res["external"]["triggered"])
        self.assertIn("escalate=off", res["external"]["reason"])

    def test_thin_escalates_to_store_github_and_ref(self):
        """本地四路都没沾边 ⇒ 外扩（2026-10-22 用户裁定）。"""
        called = {"skill": 0, "gh": 0, "ref": 0}

        async def store(*a, **k):
            called["skill"] += 1
            return {"ok": True, "picked": {"name": "日志排查", "quality": 0.78},
                    "how_to_get": "hub_skill_auto(...)"}

        def gh(*a, **k):
            called["gh"] += 1
            return {"ok": True, "rows": [{"repo": "a/b", "stars": 9, "lang": "Go",
                                          "desc": "d", "pushed": "2026-10-01"}],
                    "arms": [{"arm": "core", "count": 1}]}

        def ref(*a, **k):
            called["ref"] += 1
            return {"ok": False, "card_id": "c1", "status": "running"}

        old = (intake._skill_store, intake._gh_repos)
        intake._skill_store, intake._gh_repos = store, gh
        try:
            res = self._run((lambda *a, **k: _mem(),
                             lambda *a, **k: {"ok": True, "count": 0, "items": []},
                             lambda *a, **k: asyncio.sleep(0, result={
                                 "ok": False, "reason": "no local skill"}),
                             ref),
                            intake.IntakeIn(task="解释这行日志", auto_deep=False))
        finally:
            intake._skill_store, intake._gh_repos = old
        self.assertEqual(res["plan"]["verdict"], "thin")
        self.assertTrue(res["external"]["triggered"])
        self.assertEqual(called, {"skill": 1, "gh": 1, "ref": 1})
        self.assertEqual(res["external"]["status"], "found")
        self.assertIn("日志排查", res["brief"])
        self.assertIn("a/b", res["brief"])
        self.assertIn("未配置搜索 provider", res["brief"], "联网搜索必须如实报不可用")

    def test_elapsed_includes_escalation(self):
        """`elapsed_s` 必须含外扩耗时。

        判例（2026-10-22 生产实测）：thin 单外扩真实 12.4s，简报头却写「用时 0.2s」——
        计时在 `_escalate` 之前算的；而 §3 里另有「外扩用时 12.1s」，同一屏两个数自相矛盾。
        """
        def slow_gh(*a, **k):
            time.sleep(0.35)
            return {"ok": True, "rows": [], "arms": []}

        old = intake._gh_repos
        intake._gh_repos = slow_gh
        try:
            res = self._run((lambda *a, **k: _mem(),
                             lambda *a, **k: {"ok": True, "count": 0, "items": []},
                             lambda *a, **k: asyncio.sleep(0, result={"ok": False}),
                             lambda *a, **k: {"ok": False, "error": "x"}),
                            intake.IntakeIn(task="解释这行日志", auto_deep=False))
        finally:
            intake._gh_repos = old
        self.assertTrue(res["external"]["triggered"])
        self.assertGreaterEqual(res["elapsed_s"], 0.3,
                                "用时早于外扩计算 ⇒ 会低估数十倍，必须包含外扩")
        self.assertIn(f"用时：{res['elapsed_s']}s", res["brief"], "简报头用的就是这个数")

    def test_ready_does_not_escalate(self):
        called = {"skill": 0}

        async def store(*a, **k):
            called["skill"] += 1
            return {"ok": False}

        old = intake._skill_store
        intake._skill_store = store
        try:
            res = self._run((lambda *a, **k: {
                                 "ok": True, "count": 1,
                                 "items": [{"layer": "L1", "source": "tdai_l1",
                                            "content": "解释日志时先看 502 的上游"}]},
                lambda *a, **k: {"ok": True, "count": 0, "items": []},
                lambda *a, **k: asyncio.sleep(0, result=_skill()),
                lambda *a, **k: {"ok": False}),
                intake.IntakeIn(task="解释日志里的 502", auto_deep=False))
        finally:
            intake._skill_store = old
        self.assertEqual(res["plan"]["verdict"], "ready")
        self.assertEqual(called["skill"], 0, "有可用线索就不该花外部配额")
        self.assertIn("未触发", res["brief"])

        self.assertEqual(res["deep_reason"], "off")


class TestAssess(unittest.TestCase):
    """整理层：去重、定级、提炼、评估 —— 都是纯词面的，**不许调模型**。"""

    TASK = "改共享配置前先备份"

    def _res(self, mem_items=(), kb_items=(), mem_ok=True):
        return {
            "task": self.TASK, "deep_reason": "off", "caller": "", "as_of": "x",
            "elapsed_s": 0.1,
            "memory": {"ok": mem_ok, "count": len(mem_items),
                       "items": list(mem_items),
                       "error": "" if mem_ok else "timed out"},
            "kb": {"ok": True, "count": len(kb_items), "items": list(kb_items)},
            "skill": {"ok": False, "reason": "no local skill matched"},
            "ref": {"ok": False, "error": "未查"},
        }

    def test_cross_route_dedup(self):
        text = "改共享配置前必须先备份，并核对 diff"
        res = self._res(
            mem_items=[{"layer": "L1", "source": "tdai_l1", "content": text}],
            kb_items=[{"source": "tdai_l1", "path": "", "content": text}])
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["dups"], 1, "同一正文跨路出现必须只算一次")
        self.assertIn("跨路重复 1 条", intake.brief_md(self.TASK, res))

    def test_weak_hits_folded_but_counted(self):
        res = self._res(mem_items=[{"layer": "L1", "source": "tdai_l1",
                                    "content": "把周报模板换成深色主题"}])
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["noise"], 1)
        self.assertEqual(plan["verdict"], "thin")
        md = intake.brief_md(self.TASK, res)
        self.assertIn("折叠弱命中 1 条", md)
        # 弱命中**不逐条占屏**（不占「已列出」的条目位），只留一行标题索引 ——
        # 全隐藏会让 thin 看起来像"没查"，读者无法判断该换词还是真没资料。
        self.assertIn("记忆：1 条命中／0 条列出", md)
        weak_lines = [ln for ln in md.splitlines() if "深色主题" in ln]
        self.assertEqual(len(weak_lines), 1, "弱命中最多一行汇总，不得逐条列")
        self.assertTrue(weak_lines[0].startswith("- 弱命中（仅列标题"))

    def test_constraints_sorted_by_relevance(self):
        res = self._res(mem_items=[
            {"layer": "L1", "source": "tdai_l1", "content": "所有服务必须绑定 127.0.0.1"},
            {"layer": "L1", "source": "tdai_l1",
             "content": "改共享配置前必须先备份，禁止 sed -i"}])
        plan = intake.assess(self.TASK, res)
        self.assertIn("禁止 sed -i", plan["constraints"][0]["text"],
                      "越贴近任务用词的约束排越前")

    def test_brief_has_no_section_duplication(self):
        res = self._res(mem_items=[{"layer": "L1", "source": "tdai_l1",
                                    "content": "改共享配置前必须先备份，禁止 sed -i"}])
        md = intake.brief_md(self.TASK, res)
        self.assertEqual(md.count("禁止 sed -i"), 2,
                         "同一句只在 §0（行动项）与 §2（明细）各出现一次，不做二次摘要")

    def test_title_only_hit_is_not_a_constraint(self):
        """「（仅标题命中：X）」里没有判据，抽成约束会得到一条只有标题的空规则。"""
        res = self._res(kb_items=[{"source": "xerj", "path": "wiki/备份.md",
                                   "title": "配置文件修改前必须备份",
                                   "content": "（仅标题命中：配置文件修改前必须备份）"}])
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["constraints"], [])
        self.assertEqual(plan["must_read"][0]["path"], "wiki/备份.md",
                         "拿不到正文的信封仍可作为「回读」指针")

    def test_front_matter_stripped_from_kb_body(self):
        """XERJ 返回的 content 换行被压成空格，front matter 仍要剥掉。"""
        res = self._res(kb_items=[{
            "source": "xerj", "path": "wiki/x.md", "title": "备份细则",
            "content": '--- type: concept t<em>i</em>tle: "备份细则" created: 2026-07-30 '
                       'status: proposed --- # 备份细则 > 改文件前必须先备份'}])
        plan = intake.assess(self.TASK, res)
        text = plan["rows"][-1]["text"]
        self.assertTrue(text.startswith("# 备份细则"), text)
        self.assertNotIn("type: concept", text)
        self.assertNotIn("<em>", text)

    def test_degraded_route_shows_error_in_table(self):
        res = self._res(mem_items=[], mem_ok=False)
        md = intake.brief_md(self.TASK, res)
        self.assertIn("degraded", md)
        self.assertIn("| 记忆 | 不可用 | 0 | 0 | timed out |", md,
                      "表里要直接看到失败原因，别让读者去别处对")

    def test_constraint_extracted_from_hit(self):
        res = self._res(mem_items=[{"layer": "L1", "source": "local",
                                    "content": "改共享配置前必须先备份，禁止 sed -i"}])
        plan = intake.assess(self.TASK, res)
        self.assertEqual(len(plan["constraints"]), 1)
        self.assertIn("禁止 sed -i", plan["constraints"][0]["text"])
        self.assertIn("## 0. 开工前先看", intake.brief_md(self.TASK, res))

    def test_must_read_from_kb_path(self):
        res = self._res(kb_items=[{"source": "xerj", "path": "wiki/备份细则.md",
                                   "content": "改共享配置前先备份"}])
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["must_read"][0]["path"], "wiki/备份细则.md")
        self.assertIn("`wiki/备份细则.md`", intake.brief_md(self.TASK, res))

    def test_verdict_degraded_beats_thin(self):
        res = self._res(mem_ok=False)
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["verdict"], "degraded")
        self.assertTrue(any("记忆" in g and "不可用" in g for g in plan["gaps"]))
        self.assertIn("不可用（timed out）", intake.brief_md(self.TASK, res))

    def test_verdict_ready_when_useful(self):
        res = self._res(mem_items=[{"layer": "L1", "source": "local",
                                    "content": "改共享配置前必须先备份"}])
        self.assertEqual(intake.assess(self.TASK, res)["verdict"], "ready")

    def test_tokens_have_no_stopword_bigrams(self):
        toks = intake._tokens("备份的配置文件")
        self.assertIn("备份", toks)
        self.assertFalse(any(t[0] in intake._STOP or t[1] in intake._STOP for t in toks),
                         "虚词参与的二元组不该计入覆盖率分母")


class TestKbEmptyContent(unittest.TestCase):
    """XERJ 只命中标题时 highlight 为空 —— 简报不许打出「path：」空行。"""

    def test_title_fallback(self):
        old = intake._self_get
        intake._self_get = lambda *a, **k: {"count": 1, "results": [
            {"source": "xerj", "path": "wiki/x.md", "title": "备份细则",
             "content": "", "score": 1.0}]}
        try:
            got = intake._kb("备份", "xerj")
        finally:
            intake._self_get = old
        self.assertEqual(got["items"][0]["content"], "（仅标题命中：备份细则）")

    def test_highlight_tags_stripped(self):
        old = intake._self_get
        intake._self_get = lambda *a, **k: {"count": 1, "results": [
            {"source": "xerj", "path": "wiki/x.md", "title": "t",
             "content": "改<em>i</em>前先备份", "score": 1.0}]}
        try:
            got = intake._kb("备份", "xerj")
        finally:
            intake._self_get = old
        self.assertNotIn("<em>", got["items"][0]["content"])

    def test_half_frontmatter_stripped(self):
        """highlight 从词中截断时，`---` 之前的半截 front matter 必须剥掉。

        实测样本（2026-10-22，`服务改名残留排查清单`）：
        `ated: 2026-10-18 updated: 2026-10-18 status: proposed tags: - distilled … --- # 标题 > 正文`
        """
        raw = ('ated: 2026-10-18 updated: 2026-10-18 status: proposed tags: - distilled '
               '- concept related_pending: - "agenthub.service" --- # 服务改名残留排查清单 '
               '> 改名后需按6类位置系统排查残留')
        body = intake._body(raw)
        self.assertTrue(body.startswith("# 服务改名残留排查清单"), body)
        self.assertNotIn("related_pending", body)
        self.assertNotIn("ated:", body)

    def test_body_keeps_divider_when_not_frontmatter(self):
        """正文里的 `---` 分隔线不是 front matter，别把前半段切掉。"""
        raw = "先备份再改 —— 这是判据 --- 下面是例子"
        self.assertEqual(intake._body(raw), raw)

    def test_frontmatter_only_fragment_dropped(self):
        r"""整段只有 front matter（闭合符后直接接 `...`）⇒ 无正文可摘，返回空。

        实测样本（2026-10-22，`wiki 索引重建 build-v3`，`raw[218]`）：
        `--- type: entity title: "…" … related_pending: - "技术文档索引器" ---...`
        `---` 后接的是 `...` 不是空白，两条归正（`\s---\s+`）都不匹配 ⇒ 旧实现原样返回。
        4/40 条查询的简报就是这么带上 front matter 的。
        """
        raw = ('--- type: entity title: "turbovec 索引重建的授权窗口与可搜索性判据" '
               'created: 2026-10-16 updated: 2026-10-16 status: proposed tags: - distilled '
               '- entity related: - "[[GitHub-直连超时时的API备用取证]]" '
               'related_pending: - "技术文档索引器" ---...')
        self.assertEqual(intake._body(raw), "", "整段 FM 不能当摘要")

    def test_half_frontmatter_bare_related(self):
        """从 FM 中间起算、只剩裸 `related:` 可见时，也要剥到正文。

        实测样本（2026-10-22，`备份脚本 命名 时间戳`）：
        `...oncept related: - "[[git commit]]" - "[[破坏性操作]]" --- # 文件修改备份命名规范 > …`
        `type:` 被切成 `oncept`，只有 `related:` 完整 ⇒ 旧 `related_\\w+` 不匹配。
        """
        raw = ('...oncept related: - "[[git commit]]" - "[[破坏性操作]]" --- '
               '# 文件修改备份命名规范 > 修改已有文件前必须备份')
        body = intake._body(raw)
        self.assertTrue(body.startswith("# 文件修改备份命名规范"), body)

    def test_frontmatter_only_falls_back_to_title(self):
        """`_body()` 交出空串后，明细行要回退标题，而不是留空行。"""
        old = intake._self_get
        intake._self_get = lambda *a, **k: {"count": 1, "results": [
            {"source": "xerj", "path": "wiki/x.md", "title": "索引重建工具与写入防护",
             "content": '--- type: concept title: "索引重建工具与写入防护" created: 2026-10-13 ---...',
             "score": 1.0}]}
        try:
            rows = intake._flatten("索引重建", {"kb": intake._kb("索引重建", "xerj")})
        finally:
            intake._self_get = old
        self.assertEqual(rows[0]["text"], "索引重建工具与写入防护")
        self.assertNotIn("type:", rows[0]["text"])

    def test_provenance_footer_not_scored(self):
        """出处尾注是**引用来源**，不参与评分（否则回声：简报→卡片→尾注里的原查询）。

        实测（2026-10-22）：「解释日志里的 502」命中的 3 张卡全是讲这次检索本身的，
        因为尾注写着 `intake/2026-10-22_101626-解释一下这次日志里的-502-来自哪里.md`。
        """
        footer = ("--- *提炼自会话 `intake/2026-10-22_101626-解释一下这次日志里的-502-来自哪里.md`"
                  "（2026-10-22）*")
        body = intake._body("四路通但无词面重合视为缺口 " + footer)
        self.assertNotIn("提炼自", body)
        self.assertNotIn("502", body, "尾注里的原查询不该留在正文里参与评分")

        res = {"task": "解释日志里的 502", "deep_reason": "off", "caller": "", "as_of": "x",
               "elapsed_s": 0.1,
               "memory": {"ok": True, "count": 0, "items": []},
               "kb": {"ok": True, "count": 1, "items": [
                   {"source": "xerj", "path": "wiki/concepts/无检索线索时的处理流程.md",
                    "content": "四路通但无词面重合视为缺口 " + footer}]},
               "skill": {"ok": False, "reason": "no local skill"},
               "ref": {"ok": False, "error": "未查"}}
        plan = intake.assess("解释日志里的 502", res)
        self.assertNotEqual(plan["verdict"], "ready", "只靠尾注凑上的命中不许判 ready")


class TestGradingEdges(unittest.TestCase):
    """定级的两处实测反例：覆盖率 >1、单条「相关」判 ready。"""

    TASK = "改共享配置前先备份"

    def test_coverage_capped_at_one(self):
        task = "MCP 工具调用超时怎么排查"
        toks = intake._tokens(task)
        self.assertGreater(len(toks), 6, "该查询词元 >6，才可能触发分母封顶")
        cov = intake._coverage(task, toks)      # 自己和自己比，命中率必为 100%
        self.assertEqual(cov, 1.0, "覆盖率是比例，封顶 1.0；旧实现算出 10/6=1.67")

    def test_related_only_is_thin(self):
        res = {"task": self.TASK, "deep_reason": "off", "caller": "", "as_of": "x",
               "elapsed_s": 0.1,
               "memory": {"ok": True, "count": 1, "items": [
                   {"layer": "L1", "source": "tdai_l1", "content": "备份与配置的关系说明"}]},
               "kb": {"ok": True, "count": 0, "items": []},
               "skill": {"ok": False, "reason": "no local skill"},
               "ref": {"ok": False, "error": "未查"}}
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["rows"][0]["rel"], "相关", "构造：只命中 2 个词元")
        self.assertEqual(plan["verdict"], "thin", "只有相关命中不配 ready")
        self.assertEqual(plan["strong"], 0)
        self.assertTrue(any("无直接命中" in g for g in plan["gaps"]))
        self.assertIn("直接 0 条／相关 1 条", intake.brief_md(self.TASK, res))

    def test_single_token_coverage_cannot_be_direct(self):
        """单词任务词元数=1 ⇒ 旧实现 cov 恒 1.0，任何含该词的条目都算「直接」。

        2026-10-22 实测：「备份」10 条命中里多条是 `准备修复 StatisticsService…｜备份`
        这类噪声，判 ready 且永不 thin（外扩永不触发）。分母给下限 3 后最多 0.33。
        """
        toks = intake._tokens("备份")
        self.assertEqual(len(toks), 1, "夹具前提：单词任务的词元数就是 1")
        cov = intake._coverage("准备修复 StatisticsService｜备份 StatisticsService.ja", toks)
        self.assertLess(cov, intake.REL_STRONG, "1 词元不许判「直接」")
        self.assertGreaterEqual(cov, intake.REL_WEAK, "仍要算「相关」，别把命中丢掉")

    def test_two_token_coverage_can_still_be_direct(self):
        """下限 3 不能把双词任务也打死：2 个词元全中仍是 0.67 ≥ 0.5。"""
        cov = intake._coverage("备份 网关", {"备份", "网关"})
        self.assertAlmostEqual(cov, 2 / 3, places=4)
        self.assertGreaterEqual(cov, intake.REL_STRONG)

    def test_single_word_task_is_thin_and_says_why(self):
        res = {"task": "备份", "deep_reason": "off", "caller": "", "as_of": "x",
               "elapsed_s": 0.1,
               "memory": {"ok": True, "count": 1, "items": [
                   {"layer": "L1", "source": "tdai_l1",
                    "content": "准备修复 StatisticsService｜备份 StatisticsService.ja"}]},
               "kb": {"ok": True, "count": 0, "items": []},
               "skill": {"ok": False, "reason": "no local skill"},
               "ref": {"ok": False, "error": "未查"}}
        plan = intake.assess("备份", res)
        self.assertEqual(plan["strong"], 0, "单词任务不许产生「直接」命中")
        self.assertEqual(plan["verdict"], "thin")
        self.assertTrue(any("检索词元" in g for g in plan["gaps"]),
                        f"必须写明任务过短，别让调用方把宽匹配当有资料：{plan['gaps']}")

    def test_validation_body_hint_is_intake_only(self):
        errs = [{"loc": ["body", "task"], "msg": "String should have at least 2 characters",
                 "type": "string_too_short"}]
        got = intake.validation_body("/api/intake/task", errs)
        self.assertFalse(got["ok"])
        self.assertIn("完整描述", got["error"])
        self.assertEqual(got["detail"], errs, "detail 原样保留，别动既有契约")
        other = intake.validation_body("/api/skill/list", errs)
        self.assertNotIn("error", other, "非 intake 端点的 422 形状一个字不动")
        self.assertEqual(other, {"detail": errs})

    def test_kb_json_exposes_cleaned_content(self):
        """`content` 保留检索原文，另给 `content_clean` 供直接读 JSON 的消费方。"""
        saved = intake._self_get
        intake._self_get = lambda path, **kw: {"count": 1, "results": [{
            "source": "xerj", "path": "wiki/x.md", "title": "备份细则",
            "content": '--- type: concept title: "备份细则" created: 2026-10-13 ---...',
            "score": 1.0}]}
        try:
            got = intake._kb("备份", "xerj")
        finally:
            intake._self_get = saved
        it = got["items"][0]
        self.assertIn("type: concept", it["content"], "原文保留（既有调用方兼容）")
        self.assertEqual(it["content_clean"], "（仅标题命中：备份细则）",
                         "整段只有 front matter ⇒ 清洗后回退标题，别把字段名当摘要")

    def test_ledger_doc_not_counted_as_clue(self):
        res = {"task": self.TASK, "deep_reason": "off", "caller": "", "as_of": "x",
               "elapsed_s": 0.1,
               "memory": {"ok": True, "count": 0, "items": []},
               "kb": {"ok": True, "count": 1, "items": [
                   {"source": "xerj", "path": "项目详细清单.md",
                    "content": "改共享配置前先备份必须核对 diff"}]},
               "skill": {"ok": False, "reason": "no local skill"},
               "ref": {"ok": False, "error": "未查"}}
        plan = intake.assess(self.TASK, res)
        self.assertEqual(plan["meta"], 1)
        self.assertEqual(plan["verdict"], "thin", "聚合清单页不算线索，不许顶成 ready")


class TestSkillAutoAndAccount(unittest.TestCase):
    """技能路两条口径（2026-10-22 用户裁定）。

    ① **取正文按任务自动选**，不点名：简报里给的是 `hub_skill_auto(task=...)`，
       不再是 `hub_skill_invoke(name=...)`——后者要调用方先知道技能名，就是手动指定。
    ② **命中记曝光，不记使用**：`skill.relevant` + `mode=auto_inject` + `via=intake`。
       只回名字没交付正文，记成 `skill.invoke` 会让权重榜把"被推荐"算成"被使用"。
    ③ **未命中不记账**：没有候选就没有可曝光的对象，不许留空名流水。
    """

    GOT = {"ok": True, "name": "tailscale-offline-diagnose", "route": "workbuddy",
           "matched": ["tailscale"], "matched_in": ["tailscale"], "source": "local",
           "local": {"norm": 0.71}, "task": "tailscale 链路延迟排查"}

    def _run(self, got):
        old_acq, old_note = intake.skill_store.acquire, intake.runlog.note
        seen = []

        async def fake_acq(*a, **k):
            return got

        intake.skill_store.acquire = fake_acq
        intake.runlog.note = lambda subject, detail, **kw: seen.append(
            (subject, detail, kw))
        try:
            out = asyncio.run(intake._skill("tailscale 链路延迟排查", "cb"))
        finally:
            intake.skill_store.acquire, intake.runlog.note = old_acq, old_note
        return out, seen

    def test_hit_points_at_auto_by_task(self):
        out, _ = self._run(self.GOT)
        self.assertIn("hub_skill_auto(task=", out["how_to_get"])
        self.assertNotIn("hub_skill_invoke", out["how_to_get"])
        self.assertIn("tailscale 链路延迟排查", out["how_to_get"])

    def test_quote_in_task_does_not_break_call(self):
        got = dict(self.GOT, task='说 "hello" 的技能')
        out, _ = self._run(got)
        self.assertEqual(out["how_to_get"].count('"'), 2,
                         "任务里的引号必须先抹平，否则调用式被截断")

    def test_hit_records_exposure_not_use(self):
        _, seen = self._run(self.GOT)
        self.assertEqual(len(seen), 1)
        subject, detail, kw = seen[0]
        self.assertEqual(subject, "skill.relevant")
        self.assertEqual(detail["mode"], "auto_inject")
        self.assertEqual(detail["via"], "intake")
        self.assertEqual(detail["name"], "tailscale-offline-diagnose")
        self.assertEqual(detail["caller"], "cb")
        self.assertNotEqual(subject, "skill.invoke")
        self.assertGreaterEqual(kw.get("duration_ms", 0), 0)

    def test_miss_records_nothing(self):
        _, seen = self._run({"ok": False, "reason": "norm 0.092 < 0.40"})
        self.assertEqual(seen, [], "没有候选就没有曝光，不许留空名流水")


if __name__ == "__main__":
    unittest.main()
