#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：接单前置的两条判定修正（2026-10-22 批 C1/C2）。

## 为什么这两条必须钉（实测，不是推测）

用 10 条真实中文任务跑生产 `/api/intake/task`，改动前的结果是**方向反的**：

| 任务 | 技能 | 改动前 verdict | 问题 |
|---|---|---|---|
| debug a flaky test failure | 命中 `systematic-debugging`（正确） | **thin** | 触发外扩，白烧 GitHub 额度 |
| 调试一个偶发失败的测试用例 | 未命中 | **ready** | 唯一「直接命中」是 `（仅标题命中：API提炼偶发失败自愈）`——**没有正文** |

两条根因：
1. **C1**：`assess()` 把「仅标题命中」当「直接线索」。`_constraints()` 早就排除这个前缀，
   定级处漏排是口径不统一。假 ready = 把 SKIP 当 PASS。
2. **C2**：`strong` 只从 memory/kb 两路取，**技能命中完全不参与定级**。

本文件不重新实现「技能算不算命中」——那个判据在 `skill_store._local_verdict`
（见 `test_skill_retrieval_cn_l0.py`），这里只认它的 `ok`，防止两套门槛漂移。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import intake  # noqa: E402

TITLE_ONLY_ROW = "（仅标题命中：API提炼偶发失败自愈）"


def _mem(items):
    return {"ok": True, "count": len(items), "items": items, "degraded": [], "took_ms": 5}


def _kb(items):
    return {"ok": True, "count": len(items), "items": items, "degraded": [], "took_ms": 5}


def _skill(ok=False, name=None, route=None, matched_in=None):
    return {"ok": ok, "name": name, "route": route, "matched_in": matched_in or [],
            "how_to_get": "hub_skill_auto(task=\"…\")" if ok else ""}


def _res(mem=(), kb=(), skill=None, ref=None, deep="off"):
    return {"memory": _mem(list(mem)), "kb": _kb(list(kb)),
            "skill": skill or _skill(),
            "ref": ref or {"ok": False, "state": "skipped", "error": "未查"},
            "deep_reason": deep}


def _hit(content, path=""):
    return {"layer": "L1", "source": "local", "category": "fact",
            "content": content, "path": path, "title": ""}


class TestC1TitleOnlyIsNotEvidence(unittest.TestCase):
    """「仅标题命中」没有正文 ⇒ 不是线索，不能把 thin 顶成 ready。"""

    def test_title_only_alone_stays_thin(self):
        res = _res(kb=[_hit(TITLE_ONLY_ROW, path="wiki/API提炼偶发失败自愈.md")])
        plan = intake.assess("调试一个偶发失败的测试用例", res)
        self.assertEqual(plan["verdict"], "thin",
                         "仅标题命中被当成直接线索 —— 这就是假 ready")
        self.assertEqual(plan["strong"], 0)

    def test_title_only_not_counted_in_useful(self):
        res = _res(kb=[_hit(TITLE_ONLY_ROW, path="wiki/x.md")])
        plan = intake.assess("调试一个偶发失败的测试", res)
        self.assertEqual(plan["useful"], 0, "仅标题命中被计入 useful")

    def test_real_body_still_counts(self):
        """反向自证：有正文的命中仍是 直接 ⇒ ready。否则 C1 会把真线索也删掉。"""
        res = _res(kb=[_hit("调试偶发失败的完整做法：先复现、再看栈、最后加埋点",
                            path="wiki/debug.md")])
        plan = intake.assess("调试偶发失败", res)
        self.assertEqual(plan["verdict"], "ready")
        self.assertGreaterEqual(plan["strong"], 1)


class TestC2SkillCountsAsStrong(unittest.TestCase):
    def test_skill_hit_makes_ready(self):
        res = _res(skill=_skill(True, "systematic-debugging", "superpowers",
                                matched_in=["debug"]))
        plan = intake.assess("debug a flaky test failure", res)
        self.assertEqual(plan["verdict"], "ready",
                         "技能命中没参与定级 —— 正确命中却判 thin（触发无谓外扩）")
        self.assertTrue(plan["skill_strong"])

    def test_skill_hit_does_not_inflate_strong_counter(self):
        """`strong` 保持「记忆/知识库直接命中数」口径不变（简报/前端/既有用例都读它）。"""
        res = _res(skill=_skill(True, "systematic-debugging", "superpowers", ["debug"]))
        plan = intake.assess("debug flaky test", res)
        self.assertEqual(plan["strong"], 0)
        self.assertTrue(plan["skill_strong"])

    def test_skill_miss_keeps_thin(self):
        res = _res(kb=[_hit(TITLE_ONLY_ROW, path="wiki/x.md")])
        plan = intake.assess("调试一个偶发失败的测试", res)
        self.assertEqual(plan["verdict"], "thin")
        self.assertFalse(plan["skill_strong"])

    def test_skill_hit_surfaces_next_action(self):
        """ready 是因为技能命中时必须说出来 —— 否则用户不知道下一步是去取正文。"""
        res = _res(skill=_skill(True, "systematic-debugging", "superpowers", ["debug"]))
        plan = intake.assess("debug flaky test", res)
        joined = " ".join(plan["gaps"])
        self.assertIn("技能已命中", joined)
        self.assertIn("systematic-debugging", joined)

    def test_degraded_wins_over_skill_hit(self):
        """有路挂了仍是 degraded：技能命中不能掩盖服务故障（先修那一路）。"""
        res = _res(skill=_skill(True, "x", "claude", ["x"]))
        res["memory"] = {"ok": False, "count": 0, "items": [], "error": "记忆服务 500"}
        plan = intake.assess("任意任务描述", res)
        self.assertEqual(plan["verdict"], "degraded")

    def test_gap_text_when_only_related_hits(self):
        """无直接命中、也无技能命中 ⇒ 必须提示「线索偏弱」，不许说成有资料。"""
        res = _res(mem=[_hit("准备修复 StatisticsService｜备份")])
        plan = intake.assess("备份", res)
        self.assertIn(plan["verdict"], ("thin", "degraded"))
        self.assertTrue(any("相关" in g or "词元" in g or "没有一条" in g for g in plan["gaps"]),
                        plan["gaps"])


class TestBriefMarksSkillHit(unittest.TestCase):
    """简报头必须能看出「ready 来自技能命中」。"""

    def test_brief_header_mentions_skill_hit(self):
        res = _res(skill=_skill(True, "systematic-debugging", "superpowers", ["debug"]))
        res.update({"task": "debug flaky test", "caller": "t", "as_of": "2026-10-22T00:00:00+00:00",
                    "elapsed_s": 0.1, "intent": None, "external": None})
        res["plan"] = intake.assess(res["task"], res)
        md = intake.brief_md(res["task"], res)
        self.assertIn("技能命中", md)


if __name__ == "__main__":
    unittest.main()
