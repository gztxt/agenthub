#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：模型边界画像（src/modelprofile.py）。

【守五条】
1. **限流是负载信号不是质量信号**：429 必须从质量口径剔除，且**单列计数**。
   （实测污染：`qwen3.7-max` 含 429 时 70%，剔除后 97%；跨 client 跨度 60pp → 7pp。）
2. **三态与 verifier 同构**：样本不足 = `insufficient_sample`（**不是「模型差」**），
   跨 client 不稳 = `unstable`（率不可信）。**判不了就说判不了，不编数**。
3. **免费模型 `cost_usd` 为 NULL 不当 0**：`calls_null_cost` 单列，
   `cost_per_1k_tok` 在真有成本时才给值。
4. **占位/路由器不构成画像**：`unknown` / `openrouter/free` / 测试残留直接跳过。
5. **不宣称「擅长什么」**：模块只出边界；账本无正文，语义能力另有来源。

分层：纯函数、stdlib-only、零 SKIP、不起服务不连网不碰 sqlite。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import modelprofile as mp  # noqa: E402


def _row(model="m", code=200, ms=100, ti=0, to=0, cost=None,
         provider="alibaba", client="API Key 1"):
    return {"model": model, "status_code": code, "duration_ms": ms,
            "input_tokens": ti, "output_tokens": to, "cost_usd": cost,
            "provider": provider, "client": client}


class TestRateLimitSeparation(unittest.TestCase):
    """判据 1：429 单列且不污染质量口径。"""

    def test_rate_limited_counted_separately(self):
        rows = [_row(code=200)] * 7 + [_row(code=429)] * 3
        p = mp.profile(rows)
        self.assertEqual(10, p["calls"])
        self.assertEqual(3, p["rate_limited"])
        self.assertEqual(7, p["ok"])
        self.assertEqual(0, p["failed_other"])

    def test_ok_rate_excl_rl_ignores_rate_limit(self):
        rows = [_row(code=200)] * 7 + [_row(code=429)] * 3
        p = mp.profile(rows, min_sample=1)
        self.assertAlmostEqual(70.0, p["ok_rate"], places=1)
        self.assertAlmostEqual(100.0, p["ok_rate_excl_rl"], places=1,
                               msg="剔除 429 后质量口径必须是 100%")

    def test_non_429_failures_still_count_as_quality_fail(self):
        rows = [_row(code=200)] * 5 + [_row(code=500)] * 3 + [_row(code=429)] * 2
        p = mp.profile(rows, min_sample=1)
        self.assertEqual(3, p["failed_other"])
        self.assertAlmostEqual(62.5, p["ok_rate_excl_rl"], places=1,
                               msg="5xx 是真实失败，不得被当成限流一并剔除")


class TestThreeStates(unittest.TestCase):
    """判据 2：三态语义。"""

    def test_empty_is_insufficient_not_bad(self):
        p = mp.profile([])
        self.assertEqual(mp.STATUS_INSUFFICIENT, p["status"])
        self.assertEqual(0, p["calls"])
        self.assertTrue(p["reasons"], "必须说明为什么")

    def test_small_sample_is_insufficient_not_bad(self):
        p = mp.profile([_row()] * 5)
        self.assertEqual(mp.STATUS_INSUFFICIENT, p["status"])
        self.assertIn("样本", " ".join(p["reasons"]))

    def test_single_client_is_unstable(self):
        """只有一个 client ⇒ 无法交叉验证 ⇒ 率不可信。"""
        p = mp.profile([_row(client="API Key 1")] * 40)
        self.assertEqual(mp.STATUS_UNSTABLE, p["status"])
        self.assertIsNone(p["client_span_pp"])

    def test_cross_client_spread_marks_unstable(self):
        """同模型在两个 client 差 60pp ⇒ 不可信（实测 minimax-m3 的形态）。"""
        rows = [_row(client="A", code=200)] * 30 + [_row(client="B", code=429)] * 30
        p = mp.profile(rows)
        self.assertEqual(mp.STATUS_UNSTABLE, p["status"])
        # 但剔除 429 后质量口径仍然分离可见 —— 说明是负载不是模型
        self.assertEqual(2, p["clients"])

    def test_stable_multi_client_is_ok(self):
        rows = ([_row(client="A", code=200)] * 20
                + [_row(client="B", code=200, cost=0.01)] * 20)
        p = mp.profile(rows)
        self.assertEqual(mp.STATUS_OK, p["status"])
        self.assertEqual(0.0, p["client_span_pp"])

    def test_statuses_are_closed_set(self):
        for rows in ([], [_row()], [_row()] * 40):
            self.assertIn(mp.profile(rows)["status"], mp.STATUSES)


class TestNullCostNotZero(unittest.TestCase):
    """判据 3：免费模型 NULL 不当 0。"""

    def test_null_cost_counted_and_price_is_none(self):
        p = mp.profile([_row(ti=100, to=100, cost=None)] * 30)
        self.assertEqual(30, p["calls_null_cost"])
        self.assertEqual(0.0, p["cost_usd"])
        self.assertIsNone(p["cost_per_1k_tok"],
                          "全是 NULL 时不得给出单价（0 会被读成「免费」）")

    def test_mixed_null_and_real_cost(self):
        """一半 NULL、一半真实：总额按有成本的算，单价按**全部** token 摊（混合口径）。"""
        rows = [_row(ti=100, to=100, cost=None)] * 10 + \
               [_row(ti=100, to=100, cost=1.0)] * 10
        p = mp.profile(rows)
        self.assertEqual(10, p["calls_null_cost"])
        self.assertAlmostEqual(10.0, p["cost_usd"], places=4)   # 10 行 × 1.0
        self.assertEqual(4000, p["tokens_total"])               # 20 行 × 200 token
        # 混合单价 = 全部成本 / 全部 token：10.0 / 4000 * 1000 = 2.5
        # （口径：这是「这段时期里这个模型的平均单价」，**不是**「付费时的单价」——
        #   后者要除以有成本那部分的 token。两者含义不同，别混用。）
        self.assertAlmostEqual(2.5, p["cost_per_1k_tok"], places=4)


class TestPlaceholdersExcluded(unittest.TestCase):
    """判据 4：占位/路由器不构成画像。"""

    def test_placeholder_models_skipped(self):
        rows = [_row(model="unknown")] * 50 + [_row(model="openrouter/free")] * 50 \
            + [_row(model="gpt-9-no-such-model-e2e-20260923")] * 50
        p = mp.profile(rows)
        self.assertEqual(0, p["calls"], "占位/路由器/测试残留都不该进画像")

    def test_gateway_prefixes_collapse(self):
        """同一模型三种写法须合成一份画像（判例 178 的口径在此生效）。"""
        rows = [_row(model="nvidia/x:free")] * 10 + \
               [_row(model="openrouter/nvidia/x:free")] * 10 + \
               [_row(model="anthropic/open_router/nvidia/x:free")] * 10
        p = mp.profile(rows)
        self.assertEqual(30, p["calls"], "网关前缀写法须归一到同一模型")


class TestLatencyAndShape(unittest.TestCase):
    """边界项：时延分位与协议形状。"""

    def test_percentiles(self):
        rows = [_row(ms=v) for v in range(1, 101)]
        p = mp.profile(rows)
        self.assertIsNotNone(p["latency_p50_ms"])
        self.assertGreaterEqual(p["latency_p95_ms"], p["latency_p50_ms"])

    def test_latency_none_when_no_data(self):
        p = mp.profile([_row(ms=None)] * 30)
        self.assertIsNone(p["latency_p50_ms"])

    def test_api_shapes_collected(self):
        rows = ([_row(provider="alibaba")] * 10
                + [_row(provider="provider-alibaba-b600d5a733::openai_responses")] * 10)
        p = mp.profile(rows)
        self.assertIn("openai_responses", p["api_shapes"])

    def test_thresholds_are_data_derived(self):
        """阈值不是拍脑袋：必须落在实测分界上。"""
        self.assertEqual(20, mp.MIN_SAMPLE)
        self.assertEqual(20.0, mp.MAX_CLIENT_SPAN_PP)


if __name__ == "__main__":
    unittest.main(verbosity=2)
