"""难度→起始档位（`src/tiercfg.py`）的纯函数测试。

判据不是"代码能跑"，而是**三条边界一条都不能松**：
  ① 显式选择永不被覆盖；② 档位只升不降；③ 不知道就明说（不猜 ID、不猜清单）。
"""
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

import tiercfg as t  # noqa: E402


CFG = {"t1": ["cheap-a", "cheap-b"], "t2": ["mid-a"], "t3": ["strong-a"]}
CAT = {"cheap-a", "mid-a", "strong-a"}


def _item(tid="t1", **kw):
    base = {"id": tid, "task": "x", "agent_id": "claude", "difficulty": 2,
            "deps": [], "model": None}
    base.update(kw)
    return base


class TestParse(unittest.TestCase):
    def test_default_when_env_empty(self):
        cfg = t.parse_tiers("")
        self.assertIn("t1", cfg)
        self.assertTrue(all(cfg.values()), "默认档位不能有空档")

    def test_custom(self):
        self.assertEqual(t.parse_tiers("t1=a,b;t2=c"),
                         {"t1": ["a", "b"], "t2": ["c"]})

    def test_bad_item_does_not_kill_table(self):
        """手抖一项不该让整表失效 —— 否则一次笔误等于全关。"""
        self.assertEqual(t.parse_tiers("t1=a;broken;t2=c"), {"t1": ["a"], "t2": ["c"]})

    def test_mapping_partial_override(self):
        m = t.parse_mapping("3:t1")
        self.assertEqual(m[3], "t1")
        self.assertEqual(m[1], "t1")
        self.assertEqual(m[2], "t2")


class TestLadder(unittest.TestCase):
    def test_order_is_numeric_not_lexical(self):
        self.assertEqual(t.order({"t10": ["x"], "t2": ["y"], "t1": ["z"]}),
                         ["t1", "t2", "t10"])

    def test_next_tier_only_up(self):
        self.assertEqual(t.next_tier("t1", CFG), "t2")
        self.assertEqual(t.next_tier("t2", CFG), "t3")

    def test_top_has_no_next(self):
        self.assertIsNone(t.next_tier("t3", CFG), "最高档不能再升 ⇒ 交给 agent 阶梯")

    def test_unknown_tier_has_no_next(self):
        self.assertIsNone(t.next_tier("explicit", CFG))
        self.assertIsNone(t.next_tier(None, CFG))


class TestTierFor(unittest.TestCase):
    def test_default_mapping(self):
        self.assertEqual(t.tier_for(1, tiers=CFG), "t1")
        self.assertEqual(t.tier_for(3, tiers=CFG), "t3")

    def test_floor_beats_broken_mapping(self):
        """有人把难度 3 映射到 t1 —— 地板必须兜住，这是唯一的硬约束。"""
        self.assertEqual(t.tier_for(3, {1: "t1", 2: "t1", 3: "t1"},
                                    {3: "t2"}, CFG), "t2")

    def test_difficulty_out_of_range_clamps(self):
        self.assertEqual(t.tier_for(0, tiers=CFG), "t1")
        self.assertEqual(t.tier_for(99, tiers=CFG), "t3")
        self.assertEqual(t.tier_for("bogus", tiers=CFG), "t2")

    def test_unknown_target_falls_to_lowest(self):
        self.assertEqual(t.tier_for(2, {1: "t9", 2: "t9", 3: "t9"}, {}, CFG), "t1")


class TestFirstUsable(unittest.TestCase):
    def test_catalog_unavailable_is_not_a_guess(self):
        m, why = t.first_usable("t1", CFG, CAT, checked=False)
        self.assertIsNone(m)
        self.assertEqual(why, "catalog_unavailable")

    def test_fallback_within_tier(self):
        """首选不在清单就用备胎 —— t1 放两家就是为这一步。"""
        m, why = t.first_usable("t1", CFG, {"cheap-b"}, checked=True)
        self.assertEqual((m, why), ("cheap-b", "ok"))

    def test_none_in_catalog(self):
        m, why = t.first_usable("t2", CFG, {"cheap-a"}, checked=True)
        self.assertIsNone(m)
        self.assertEqual(why, "no_tier_model_in_catalog")


class TestPlan(unittest.TestCase):
    def test_explicit_model_never_overridden(self):
        items, note = t.plan([_item(difficulty=3, model="my-pick")],
                             catalog=CAT, checked=True, ccr_agents={"claude"}, tiers=CFG)
        self.assertEqual(items[0]["model_planned"], "my-pick")
        self.assertEqual(items[0]["tier"], "explicit")
        self.assertEqual(note["counts"]["explicit"], 1)

    def test_difficulty_picks_tier_and_model(self):
        items, _ = t.plan([_item(difficulty=2)], catalog=CAT, checked=True,
                          ccr_agents={"claude"}, tiers=CFG)
        self.assertEqual(items[0]["tier"], "t2")
        self.assertEqual(items[0]["model_planned"], "mid-a")

    def test_non_ccr_agent_gets_no_model(self):
        """codebuddy/cursor 的模型域不是 CCR：塞 CCR ID 必报 unknown model。"""
        items, _ = t.plan([_item(agent_id="cursor", difficulty=3)], catalog=CAT,
                          checked=True, ccr_agents={"claude"}, tiers=CFG)
        self.assertIsNone(items[0]["model_planned"])
        self.assertEqual(items[0]["tier_reason"], "agent_model_domain_not_ccr")

    def test_pinned_top_for_synthesis(self):
        items, _ = t.plan([_item(tid="t_sum", difficulty=1, pinned_tier="top")],
                          catalog=CAT, checked=True, ccr_agents={"claude"}, tiers=CFG)
        self.assertEqual(items[0]["tier"], "t3")
        self.assertEqual(items[0]["model_planned"], "strong-a")

    def test_catalog_down_plans_nothing_but_keeps_tier(self):
        """清单取不到：档位照样算（可复盘），但不下发模型 —— 不拿默认值凑合。"""
        items, _ = t.plan([_item(difficulty=1)], catalog=set(), checked=False,
                          ccr_agents={"claude"}, tiers=CFG)
        self.assertEqual(items[0]["tier"], "t1")
        self.assertIsNone(items[0]["model_planned"])
        self.assertEqual(items[0]["tier_reason"], "catalog_unavailable")


class TestAgentFloor(unittest.TestCase):
    """执行者档位：**只升不降**，且只在池里挑。"""

    A = {"a1": ["jcode", "claude"], "a2": ["pi", "omp"], "a3": ["claude-code", "codex"]}

    def test_difficulty_3_lifts_text_endpoint_to_tool_agent(self):
        """这就是本机修的真问题：难度 3 也让纯文本端点跑 ⇒ 必然编造。"""
        a, frm, why = t.promote_agent("jcode", 3, {"jcode", "claude-code"},
                                      self.A, {3: "a3"})
        self.assertEqual(a, "claude-code")
        self.assertEqual(frm, "jcode")
        self.assertTrue(why.startswith("agent_floor"))

    def test_difficulty_1_and_2_never_change_agent(self):
        """降级需要结果证据，自评难度不配。"""
        for d in (1, 2):
            a, frm, why = t.promote_agent("claude-code", d, {"jcode", "claude-code"},
                                          self.A, {3: "a3"})
            self.assertEqual((a, frm), ("claude-code", ""))
            self.assertEqual(why, "no_agent_floor")

    def test_never_demotes_high_tier(self):
        a, frm, why = t.promote_agent("codex", 3, {"jcode", "codex"}, self.A, {3: "a3"})
        self.assertEqual((a, frm), ("codex", ""))
        self.assertEqual(why, "already_at_floor")

    def test_floor_target_missing_from_pool_is_reported(self):
        a, frm, why = t.promote_agent("jcode", 3, {"jcode", "pi"}, self.A, {3: "a3"})
        self.assertEqual((a, frm), ("jcode", ""))
        self.assertEqual(why, "agent_floor_unavailable")

    def test_unknown_agent_is_left_alone(self):
        a, frm, why = t.promote_agent("my-custom", 3, {"my-custom", "codex"},
                                      self.A, {3: "a3"})
        self.assertEqual((a, frm), ("my-custom", ""))
        self.assertEqual(why, "agent_tier_unknown")

    def test_plan_applies_floor_before_model_resolution(self):
        """先定执行者再定模型：jcode→claude-code 后，模型仍按 CCR 域注入。"""
        items, _ = t.plan([_item(difficulty=3, agent_id="jcode")], catalog=CAT, checked=True,
                          ccr_agents={"jcode", "claude-code"}, pool={"jcode", "claude-code"},
                          tiers=CFG, agent_tiers=self.A, agent_floor={3: "a3"})
        self.assertEqual(items[0]["agent_id"], "claude-code")
        self.assertEqual(items[0]["agent_promoted_from"], "jcode")
        self.assertEqual(items[0]["model_planned"], "strong-a")


if __name__ == "__main__":
    unittest.main()
