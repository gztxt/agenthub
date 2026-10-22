#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：免费模型重排（PT-20261022-13，A4 的替代实现）。

## 为什么是「免费模型」而不是 jev

A4 原计划用 jev `choice`，实测两条路都断：CLI **HTTP 451（区域封禁）**、
服务进程**未配 `TYPESAFE_API_KEY`**。于是改用本机既有的免费通道
（`llm.complete_free` → CCR `openrouter/free`，兜底位是具体 `:free`）——
这不是新依赖：DAG 拆解、L2 记忆压缩走同一条路。

## 这组用例钉的是什么

1. **只认候选内编号**：模型编一个候选里没有的技能名 ⇒ `ok=false`，绝不用它排序。
   （把幻觉接进排序键，比不重排难查得多。）
2. **失败必须显式**：超时 / 空输出 / 非 JSON / 越界编号 / 「都不适合」⇒
   `ok=false` + `why`，调用方保留 BM25 顺序。**不静默当成「原序即最优」**。
3. **不改写其余顺序**：`reorder()` 只把选中的提到第一，其余相对次序不变。
4. **不发 `openrouter/auto`**：那会按最便宜可用模型路由，可能是**付费**模型
   （军规：任何场景禁用）。本模块只经 `llm.complete_free`，模型由它决定。

⚠ 全部用 stub 替换 `llm.complete_free`，零网络、零宿主依赖 ⇒ L0。
"""
import asyncio
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import llm            # noqa: E402
import skill_rerank   # noqa: E402

CANDS = [
    {"name": "systematic-debugging", "description": "调试与根因分析", "triggers": "调试 排查"},
    {"name": "test-driven-development", "description": "先写测试", "triggers": "单元测试 红绿"},
    {"name": "code-review", "description": "代码审查", "triggers": "评审 规范"},
]


def _stub(reply, model="stub-model"):
    async def fake(system, user, call=None):
        return (reply, model)
    return fake


class _Base(unittest.TestCase):
    def setUp(self):
        self._orig = llm.complete_free

    def tearDown(self):
        llm.complete_free = self._orig

    def run_choose(self, reply, timeout=2.0):
        llm.complete_free = _stub(reply)
        return asyncio.run(skill_rerank.choose("调试一个偶发失败", CANDS, timeout_s=timeout))


class TestPicksOnlyFromCandidates(_Base):
    def test_valid_pick(self):
        r = self.run_choose('{"pick": 2, "why": "要求先写测试"}')
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "test-driven-development")
        self.assertEqual(r["pick"], 2)
        self.assertEqual(r["model"], "stub-model")

    def test_markdown_fence_tolerated(self):
        r = self.run_choose('```json\n{"pick": 3, "why": "审查"}\n```')
        self.assertTrue(r["ok"])
        self.assertEqual(r["name"], "code-review")

    def test_pick_out_of_range_rejected(self):
        r = self.run_choose('{"pick": 9, "why": "瞎选"}')
        self.assertFalse(r["ok"], "越界编号被当成了有效选择")
        self.assertIn("无法解析", r["error"])

    def test_pick_zero_means_none_suitable(self):
        r = self.run_choose('{"pick": 0, "why": "都不适合"}')
        self.assertFalse(r["ok"])
        self.assertIn("都不适合", r["error"])

    def test_invented_skill_name_rejected(self):
        """模型不按编号、编了个候选里没有的技能名 ⇒ 拒绝（不许拿幻觉当排序键）。"""
        r = self.run_choose('{"pick": "magic-skill", "why": "我觉得它行"}')
        self.assertFalse(r["ok"])

    def test_non_json_rejected(self):
        r = self.run_choose("我觉得第二个不错")
        self.assertFalse(r["ok"])
        self.assertIn("无法解析", r["error"])


class TestFailuresAreExplicit(_Base):
    def test_timeout(self):
        async def slow(system, user, call=None):
            await asyncio.sleep(1.0)
            return ('{"pick": 1}', "m")
        llm.complete_free = slow
        r = asyncio.run(skill_rerank.choose("q", CANDS, timeout_s=0.01))
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["error"])

    def test_empty_output(self):
        r = self.run_choose("")
        self.assertFalse(r["ok"])
        self.assertIn("无法解析", r["error"])

    def test_exception_degrades(self):
        async def boom(system, user, call=None):
            raise RuntimeError("CCR 502")
        llm.complete_free = boom
        r = asyncio.run(skill_rerank.choose("q", CANDS))
        self.assertFalse(r["ok"])
        self.assertIn("CCR 502", r["error"])

    def test_single_candidate_is_not_a_failure_but_not_applicable(self):
        r = asyncio.run(skill_rerank.choose("q", CANDS[:1]))
        self.assertFalse(r["ok"])
        self.assertIn("无需重排", r["error"])


class TestTimeoutDoesNotPoisonSharedBreaker(_Base):
    """超时必须**不取消底层调用**（2026-10-22 真实事故）。

    `wait_for(coro, 2)` 会取消在途 HTTP，而 llm 侧把 `CancelledError` 记成失败
    ⇒ 连续 3 次把整条 LLM 通道的熔断器打开 30s（实测 `/health.breakers`
    `last_error: "CancelledError: "`），连带 DAG 拆解 / L2 压缩一起挂。
    用 `shield` 后：超时只放弃等待，底层照跑，熔断器只按**真实**失败计数。
    """

    def test_inner_task_keeps_running_after_timeout(self):
        finished = {}

        async def slow(system, user, call=None):
            await asyncio.sleep(0.15)
            finished["done"] = True
            return ('{"pick": 1, "why": "x"}', "m")

        async def main():
            llm.complete_free = slow
            r = await skill_rerank.choose("q", CANDS, timeout_s=0.01)
            self.assertFalse(r["ok"])
            self.assertIn("超时", r["error"])
            await asyncio.sleep(0.3)      # 给底层任务跑完的机会
            return r

        asyncio.run(main())
        self.assertTrue(finished.get("done"),
                        "超时把底层 LLM 调用取消了 —— 会污染共享熔断器")

    def test_breaker_open_is_reported_not_mistaken_for_empty_output(self):
        """熔断打开时必须说「熔断中」，不能伪装成「模型输出为空」。"""
        orig = llm.breaker_state
        try:
            llm.breaker_state = lambda: {"state": "open", "cooldown_s": 13.8}
            r = asyncio.run(skill_rerank.choose("q", CANDS))
            self.assertFalse(r["ok"])
            self.assertIn("熔断", r["error"])
            self.assertIn("13.8", r["error"])
        finally:
            llm.breaker_state = orig

    def test_default_timeout_is_above_free_pool_latency(self):
        """默认超时不得低于免费池实测延迟（2.5s 级），否则永远超时。"""
        self.assertGreaterEqual(skill_rerank.DEFAULT_TIMEOUT_S, 5.0)


class TestStatsAreObservable(_Base):
    """PT-20261022-13 观察项：重排成功率必须可观测（免费池只有 34~44%）。

    不报出来，"重排偶尔没生效"只能靠人工复现 —— 那是盲区。
    """

    def setUp(self):
        super().setUp()
        skill_rerank.reset_stats()

    def test_ok_rate_counts_only_attempts(self):
        llm.complete_free = _stub('{"pick": 1, "why": "x"}')
        asyncio.run(skill_rerank.choose("q", CANDS))
        asyncio.run(skill_rerank.choose("q", CANDS))
        st = skill_rerank.stats()
        self.assertEqual(st["calls"], 2)
        self.assertEqual(st["ok"], 2)
        self.assertEqual(st["ok_rate"], 1.0)

    def test_timeout_and_breaker_are_separate_buckets(self):
        async def slow(system, user, call=None):
            await asyncio.sleep(0.3)
            return ('{"pick": 1}', "m")
        llm.complete_free = slow
        asyncio.run(skill_rerank.choose("q", CANDS, timeout_s=0.01))
        self.assertEqual(skill_rerank.stats()["timeout"], 1)
        self.assertEqual(skill_rerank.stats()["ok"], 0)

    def test_breaker_open_counted(self):
        orig = llm.breaker_state
        try:
            llm.breaker_state = lambda: {"state": "open", "cooldown_s": 5.0}
            asyncio.run(skill_rerank.choose("q", CANDS))
            self.assertEqual(skill_rerank.stats()["breaker_open"], 1)
        finally:
            llm.breaker_state = orig

    def test_single_candidate_not_in_denominator(self):
        asyncio.run(skill_rerank.choose("q", CANDS[:1]))
        st = skill_rerank.stats()
        self.assertEqual(st["calls"], 0, "候选不足不该计入分母（会虚低成功率）")
        self.assertIsNone(st["ok_rate"])

    def test_stats_shape_for_health(self):
        st = skill_rerank.stats()
        for k in ("calls", "ok", "timeout", "breaker_open", "bad_output",
                  "reordered", "ms_avg", "ok_rate", "default_timeout_s",
                  "enabled_by_default"):
            self.assertIn(k, st)
        self.assertFalse(st["enabled_by_default"], "默认必须是关（注入链预算 500ms）")


class TestReorderKeepsRestStable(unittest.TestCase):
    def test_moves_pick_to_front_only(self):
        rows = [{"name": "a"}, {"name": "b"}, {"name": "c"}]
        out = skill_rerank.reorder(rows, "c")
        self.assertEqual([r["name"] for r in out], ["c", "a", "b"])

    def test_unknown_pick_returns_original(self):
        rows = [{"name": "a"}, {"name": "b"}]
        self.assertEqual(skill_rerank.reorder(rows, "zzz"), rows)

    def test_empty_pick_returns_original(self):
        rows = [{"name": "a"}]
        self.assertEqual(skill_rerank.reorder(rows, None), rows)
        self.assertEqual(skill_rerank.reorder([], "a"), [])


class TestNoPaidRouting(unittest.TestCase):
    def test_module_never_uses_auto_route(self):
        """军规：任何场景禁用 `openrouter/auto`（会按最便宜可用模型路由，可能是付费）。"""
        src = (_REPO / "src" / "skill_rerank.py").read_text(encoding="utf-8")
        self.assertNotIn("openrouter/auto", src)
        self.assertNotIn("open_router/auto", src)

    def test_candidate_cap_is_small(self):
        """候选上限必须有界：免费池单次 0.3~3s，取多了既慢又没必要。"""
        self.assertLessEqual(skill_rerank.MAX_CANDIDATES, 8)


class TestWiringInRelevant(unittest.TestCase):
    """/relevant 的接线（真正跑端点需要 TestClient + 服务，见 L1 探测）。"""

    def test_endpoint_accepts_llm_rerank_and_passes_timeout(self):
        src = (_REPO / "src" / "skill.py").read_text(encoding="utf-8")
        self.assertIn("llm_rerank: bool = Query(default=False", src,
                      "端点没有 llm_rerank 参数，或默认不是关")
        self.assertIn("skill_rerank.choose(", src)
        self.assertIn("llm_block", src)
        self.assertIn('"llm_rerank": llm_block', src, "响应里没把重排结果回传（不可复盘）")

    def test_endpoint_default_timeout_follows_module_constant(self):
        """端点默认值必须**引用**模块常量而不是写字面量。

        实测踩过：`skill_rerank.DEFAULT_TIMEOUT_S` 改成 8s 了，端点 Query 还写死
        `default=2.0` ⇒ 显式 `llm_rerank=true` 仍必然超时（两处默认值漂移，
        正是本仓反复禁的「同一件事两个真相」）。
        """
        src = (_REPO / "src" / "skill.py").read_text(encoding="utf-8")
        self.assertIn("default=skill_rerank.DEFAULT_TIMEOUT_S", src,
                      "端点 llm_timeout 默认值没有引用模块常量（会与模块默认漂移）")

    def test_default_off_because_injection_budget(self):
        """默认必须关：注入链预算 500ms，而免费池 p50 就超了（实测见 PT-13）。"""
        src = (_REPO / "src" / "skill.py").read_text(encoding="utf-8")
        self.assertRegex(src, r"llm_rerank: bool = Query\(default=False")


if __name__ == "__main__":
    unittest.main()
