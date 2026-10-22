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
            "how_to_get": ('hub_skill_invoke(name="backup-before-edit")'
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
        self.assertIn("hub_skill_invoke", md)

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
                        intake.IntakeIn(task="解释这行日志", auto_deep=False))
        self.assertIn("brief", res)
        self.assertIn("boom", res["brief"])

    def test_ref_not_called_when_off(self):
        called = {"ref": 0}

        def ref(*a, **k):
            called["ref"] += 1
            return {"ok": False, "error": "x"}

        res = self._run((lambda *a, **k: _mem(), lambda *a, **k: _kb(),
                         lambda *a, **k: asyncio.sleep(0, result=_skill()), ref),
                        intake.IntakeIn(task="解释这行日志", auto_deep=False))
        self.assertEqual(called["ref"], 0, "deep=off 时不许摸参考库")
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


if __name__ == "__main__":
    unittest.main()
