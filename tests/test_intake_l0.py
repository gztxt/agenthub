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


if __name__ == "__main__":
    unittest.main()
