#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：CCR 账本字段归一化口径（src/ledgernorm.py）。

【本文件的用例全部是账本里的真实取值】
2026-10-16 对 `usage.sqlite`（37583 行）全量实测后取样，**不是编的**。
每条用例后面标了实测条数 —— 改口径时对着这些数字看，别凭想象。

【守六条】
1. **只剥网关前缀**：`nvidia/` `minimax/` `stealth/` 等厂商前缀必须原样保留
   （剥了会把不同模型合并成假的「同一个」）。
2. **反复剥离**：`anthropic/open_router/minimax/…` 双层嵌套要剥到底。
3. **`:free` 分两轴**：成本口径用 `canonical`（保留档位），
   能力口径用 `base`（去档位）。**二者不许互相污染**。
4. **占位/路由器不冒充模型**：`unknown` `default` `free`
   （`openrouter/free` 是路由器）`no-such-model` 测试残留，一律 `is_model=False`。
5. **provider 一名两写要合并，但协议形状不许丢**：
   `provider-alibaba-<hash>::openai_responses` ⇒ `('alibaba','openai_responses')`。
6. **不许靠「看着怪」猜**：只认精确字面量 + 显式测试痕迹标记。

分层口径：纯函数、stdlib-only、零 SKIP、不起服务不连网不碰 sqlite。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import ledgernorm as ln  # noqa: E402


class TestCanonicalModel(unittest.TestCase):
    """判据 1 + 2：网关前缀反复剥、厂商前缀不剥。"""

    #: 账本实测：同一模型 5 种写法 → 归一后必须落成同一个 key。
    NEMOTRON_VARIANTS = (
        ("nvidia/nemotron-3-ultra-550b-a55b:free", 1446),
        ("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", 3),
        ("open_router/nvidia/nemotron-3-ultra-550b-a55b:free", 4),
        ("openrouter-free/nvidia/nemotron-3-ultra-550b-a55b:free", 1),
        ("anthropic/openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", 1),
    )

    def test_all_variants_collapse_to_one(self):
        keys = {ln.canonical_model(m) for m, _ in self.NEMOTRON_VARIANTS}
        self.assertEqual(1, len(keys), "归一后仍分裂：%s" % keys)
        self.assertEqual("nvidia/nemotron-3-ultra-550b-a55b:free", keys.pop())

    def test_double_nested_prefix_fully_stripped(self):
        self.assertEqual("minimax/minimax-m3:free",
                         ln.canonical_model("anthropic/open_router/minimax/minimax-m3:free"))

    def test_vendor_prefix_preserved(self):
        """判据 1：厂商前缀是身份，剥了就是合并假模型。"""
        for raw, want in (
            ("nvidia/nemotron-3.5-lightning:free", "nvidia/nemotron-3.5-lightning:free"),
            ("minimax/minimax-m3:free", "minimax/minimax-m3:free"),
            ("stealth/space-bunny-alpha", "stealth/space-bunny-alpha"),
            ("qwen/qwen3.8-27b:free", "qwen/qwen3.8-27b:free"),
            ("deepseek-anthropic/deepseek-v4-flash", "deepseek-anthropic/deepseek-v4-flash"),
        ):
            self.assertEqual(want, ln.canonical_model(raw), raw)

    def test_bare_name_unchanged(self):
        self.assertEqual("qwen3.8-flash", ln.canonical_model("qwen3.8-flash"))
        self.assertEqual("qwen3.8-flash", ln.canonical_model("tokenrouter/qwen3.8-flash"))

    def test_lower_case_and_trim(self):
        self.assertEqual("qwen3.8-flash", ln.canonical_model("  QWEN3.8-FLASH  "))

    def test_none_and_empty(self):
        self.assertEqual("", ln.canonical_model(None))
        self.assertEqual("", ln.canonical_model("   "))


class TestTierAndBase(unittest.TestCase):
    """判据 3：`:free` 分两轴，互不污染。"""

    def test_tier(self):
        self.assertEqual(ln.TIER_FREE, ln.tier_of(ln.canonical_model("minimax/minimax-m3:free")))
        self.assertEqual(ln.TIER_PAID, ln.tier_of(ln.canonical_model("qwen3.8-flash")))
        self.assertEqual(ln.TIER_UNKNOWN, ln.tier_of(ln.canonical_model("unknown")))
        self.assertEqual(ln.TIER_UNKNOWN, ln.tier_of(""))

    def test_base_strips_free_suffix(self):
        self.assertEqual("minimax/minimax-m3", ln.base_model("minimax/minimax-m3:free"))
        self.assertEqual("minimax/minimax-m3", ln.base_model("minimax/minimax-m3"))

    def test_free_and_paid_share_base_but_differ_in_canonical(self):
        """同一模型跨计费档：能力口径合并、成本口径分开。"""
        free = ln.normalize("minimax/minimax-m3:free")
        paid = ln.normalize("minimax/minimax-m3")
        self.assertEqual(free["base"], paid["base"], "能力口径必须合并")
        self.assertNotEqual(free["canonical"], paid["canonical"], "成本口径必须分开")
        self.assertEqual(ln.TIER_FREE, free["tier"])
        self.assertEqual(ln.TIER_PAID, paid["tier"])

    def test_free_suffix_only_stripped_at_end(self):
        """名字中间出现 `free` 不能被当档位剥掉。"""
        self.assertEqual("somevendor/freestyle-model",
                         ln.base_model("somevendor/freestyle-model"))


class TestNonModel(unittest.TestCase):
    """判据 4 + 6：占位/路由器/测试残留。"""

    def test_placeholders_are_not_models(self):
        for raw in ("unknown", "default", "none", "null", "auto"):
            ok, why = ln.is_model(raw)
            self.assertFalse(ok, "%r 被当成模型了" % raw)
            self.assertTrue(why)

    def test_openrouter_free_is_a_router_not_a_model(self):
        """`openrouter/free` 是路由器（自动挑最便宜的），没有固定能力。"""
        ok, why = ln.is_model("openrouter/free")
        self.assertFalse(ok, "路由器不得当成模型")
        self.assertIn("free", why)
        # 剥掉网关前缀后剩下字面量 `free` —— 正是 NON_MODEL_LITERALS 里的那一个
        self.assertEqual("free", ln.canonical_model("openrouter/free"))
        # CCR 侧写法（军规第 4 条：`openrouter/free` 经 CCR 写 `openrouter/openrouter/free`）
        # 因**反复剥离**而归到同一个 `free` 路由器 —— 这正是口径想要的效果
        self.assertEqual("free", ln.canonical_model("openrouter/openrouter/free"))

    def test_test_residue_detected(self):
        """09-23 e2e 故意造的假 ID（账本实测 3 条）。"""
        ok, why = ln.is_model("gpt-9-no-such-model-e2e-20260923")
        self.assertFalse(ok)
        self.assertIn("no-such-model", why)

    def test_real_models_pass(self):
        for raw in ("qwen3.8-flash", "deepseek-v4.1-flash", "glm-5.3",
                    "nvidia/nemotron-3-ultra-550b-a55b:free", "claude-opus-5-5"):
            ok, _ = ln.is_model(raw)
            self.assertTrue(ok, "%r 被误判成非模型" % raw)

    def test_no_heuristic_guessing(self):
        """判据 6：名字「看着怪」不构成非模型判据。"""
        for raw in ("unknown-model-v2", "default-plus", "gpt-9", "free-tier-model"):
            ok, _ = ln.is_model(raw)
            self.assertTrue(ok, "启发式误杀了正经模型：%r" % raw)


class TestProvider(unittest.TestCase):
    """判据 5：一名两写要合并，协议形状不许丢。"""

    def test_internal_logical_name_parsed(self):
        self.assertEqual(("alibaba", "openai_chat_completions"),
                         ln.canonical_provider(
                             "provider-alibaba-b600d5a733::openai_chat_completions"))
        self.assertEqual(("openrouter", "openai_responses"),
                         ln.canonical_provider(
                             "provider-openrouter-f86c8ca19f::openai_responses"))

    def test_clean_name_passes_through(self):
        self.assertEqual(("alibaba", ""), ln.canonical_provider("alibaba"))

    def test_same_provider_two_spellings_merge_but_keep_shape(self):
        clean = ln.normalize("qwen3.8-flash", "alibaba")
        internal = ln.normalize("qwen3.8-flash",
                                "provider-alibaba-b600d5a733::openai_chat_completions")
        self.assertEqual(clean["provider"], internal["provider"], "provider 必须合并")
        self.assertEqual("", clean["api_shape"])
        self.assertEqual("openai_chat_completions", internal["api_shape"],
                         "协议形状必须保留（它决定口径）")

    def test_gateway_provider_spellings_normalize(self):
        for raw in ("open_router", "openrouter-free", "tokenrouter"):
            self.assertEqual(("openrouter", ""), ln.canonical_provider(raw), raw)

    def test_provider_none(self):
        self.assertEqual(("", ""), ln.canonical_provider(None))
        self.assertEqual(("", ""), ln.canonical_provider("  "))


class TestNormalizeBundle(unittest.TestCase):
    """一次拿全，避免各处各写一套口径。"""

    def test_shape(self):
        got = ln.normalize("anthropic/open_router/minimax/minimax-m3:free",
                           "provider-alibaba-b600d5a733::openai_chat_completions")
        self.assertEqual({
            "canonical": "minimax/minimax-m3:free",
            "base": "minimax/minimax-m3",
            "tier": ln.TIER_FREE,
            "is_model": True,
            "why": "具体模型",
            "provider": "alibaba",
            "api_shape": "openai_chat_completions",
        }, got)

    def test_normalize_accepts_canonical_input(self):
        """幂等：对已归一的值再调一次不得变化（聚合时会反复过）。"""
        once = ln.normalize("openrouter/nvidia/nemotron-3-ultra-550b-a55b:free", "openrouter")
        twice = ln.normalize(once["canonical"], once["provider"])
        self.assertEqual(once["canonical"], twice["canonical"])
        self.assertEqual(once["base"], twice["base"])
        self.assertEqual(once["tier"], twice["tier"])
        self.assertEqual(once["provider"], twice["provider"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
