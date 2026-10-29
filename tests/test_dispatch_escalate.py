"""升档重试（cascade）的 L0 闸门。

背景（2026-10-29 用户令）：旧的重试是"同 agent、同模型、把上次原因塞进 prompt 再赌一次"。
能力不够的任务必然再输一次，这一轮钱纯烧。现在第 N 次重试换一档 ——
模型优先（同域才换），没有模型通道就换 agent，换不动要**写明原因**。

这里钉四条：
  ① 第一次执行绝不升档（否则等于永远用贵的）；
  ② 模型升档只给模型域 = CCR 的 agent（给 codebuddy 塞 CCR ID ＝ 做成必失败）；
  ③ agent 升档必须落在当前可用池里（不在池子里的目标不许写）；
  ④ 无路可升时 `changed=False` + reason，不假装升过。
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import tasks  # noqa: E402


class TestEscalate(unittest.TestCase):
    def test_first_attempt_never_escalates(self):
        t = tasks.escalate_target("jcode", None, 0, {"claude-code", "jcode"})
        self.assertFalse(t["changed"])
        self.assertEqual(t["reason"], "first_attempt")

    def test_model_chain_steps_once_per_attempt(self):
        known = {"claude", "jcode", "claude-code"}
        with mock.patch.dict(os.environ,
                             {"TASK_ESCALATE_MODELS": "alibaba/qwen3.8-max,alibaba/qwen3.8-max2"},
                             clear=False):
            t1 = tasks.escalate_target("jcode", None, 1, known, ccr_agents={"jcode"})
            t2 = tasks.escalate_target("jcode", None, 2, known, ccr_agents={"jcode"})
        self.assertTrue(t1["changed"])
        self.assertEqual(t1["model"], "alibaba/qwen3.8-max")
        self.assertEqual(t2["model"], "alibaba/qwen3.8-max2")

    def test_model_chain_clamps_at_last(self):
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_MODELS": "m1,m2"}, clear=False):
            t = tasks.escalate_target("jcode", None, 9, {"jcode"}, ccr_agents={"jcode"})
        self.assertEqual(t["model"], "m2")

    def test_native_model_agent_does_not_get_ccr_model(self):
        """codebuddy 的模型域是厂商清单：给它 CCR ID ＝ 必报 unknown model。"""
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_MODELS": "alibaba/qwen3.8-max"},
                             clear=False):
            t = tasks.escalate_target("codebuddy", None, 1, {"codebuddy", "claude-code"},
                                      ccr_agents={"jcode", "claude-code"})
        self.assertEqual(t["agent_id"], "claude-code", "应改走 agent 升档")
        self.assertIsNone(t["model"])

    def test_agent_ladder_skips_self_and_unavailable(self):
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_LADDER": "codex,claude-code"},
                             clear=False):
            t = tasks.escalate_target("codex", None, 1, {"codex", "claude-code"})
        self.assertEqual(t["agent_id"], "claude-code")
        self.assertEqual(t["reason"], "agent:claude-code")

    def test_no_path_is_reported_not_faked(self):
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_LADDER": "codex"}, clear=False):
            t = tasks.escalate_target("codex", None, 1, {"codex"})
        self.assertFalse(t["changed"])
        self.assertEqual(t["reason"], "no_escalation_path")

    def test_from_fields_record_the_origin(self):
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_MODELS": "m1"}, clear=False):
            t = tasks.escalate_target("jcode", "old-model", 1, {"jcode"}, ccr_agents={"jcode"})
        self.assertEqual(t["from_agent"], "jcode")
        self.assertEqual(t["from_model"], "old-model")
        self.assertEqual(t["model"], "m1")

    def test_same_model_is_not_an_escalation(self):
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_MODELS": "same"}, clear=False):
            t = tasks.escalate_target("jcode", "same", 1, {"jcode", "codex"},
                                      ccr_agents={"jcode"})
        # 同模型等于原地再赌：continue 到 agent 阶梯，而不是报 changed=True
        self.assertEqual(t["reason"], "agent:codex")

    def test_tier_path_goes_one_step_up(self):
        """难度定起点档 ⇒ 失败就上一档，而不是跳到一个与当前档无关的模型。"""
        cfg = {"t1": ["cheap"], "t2": ["mid"], "t3": ["strong"]}
        t = tasks.escalate_target("claude", "cheap", 1, {"claude"},
                                  ccr_agents={"claude"}, tier="t1",
                                  tiers_cfg=cfg, catalog={"cheap", "mid", "strong"},
                                  catalog_checked=True)
        self.assertTrue(t["changed"])
        self.assertEqual(t["model"], "mid")
        self.assertEqual(t["reason"], "tier:t2:mid")

    def test_tier_top_falls_to_agent_ladder(self):
        """已在最高档：再升没有模型可给了 ⇒ 换 agent（工具环更强）。"""
        cfg = {"t1": ["cheap"], "t2": ["mid"], "t3": ["strong"]}
        t = tasks.escalate_target("claude", "strong", 1, {"claude", "claude-code"},
                                  ccr_agents={"claude"}, tier="t3", tiers_cfg=cfg,
                                  catalog={"cheap", "mid", "strong"}, catalog_checked=True)
        self.assertTrue(t["changed"])
        self.assertEqual(t["agent_id"], "claude-code")

    def test_tier_never_escalates_down(self):
        """起点就是 t3、池里只有更低档 —— 不许给出降档目标。"""
        cfg = {"t1": ["cheap"], "t2": ["mid"], "t3": ["strong"]}
        t = tasks.escalate_target("claude", "strong", 1, {"claude"}, ccr_agents={"claude"},
                                  tier="t3", tiers_cfg=cfg, catalog={"cheap", "mid"},
                                  catalog_checked=True)
        self.assertFalse(t["changed"])
        self.assertEqual(t["reason"], "no_escalation_path")

    def test_tier_without_catalog_falls_back_to_chain(self):
        """清单取不到 ⇒ 档位路径不成立，退回旧链路（同时不假称升过档）。"""
        cfg = {"t1": ["cheap"], "t2": ["mid"]}
        with mock.patch.dict(os.environ, {"TASK_ESCALATE_MODELS": "m1"}, clear=False):
            t = tasks.escalate_target("claude", "cheap", 1, {"claude"}, ccr_agents={"claude"},
                                      tier="t1", tiers_cfg=cfg, catalog=set(),
                                      catalog_checked=False)
        self.assertEqual(t["model"], "m1")

    def test_escalation_columns_declared(self):
        """升档来源必须落库：不记就分不清"升档后又败"与"同档又败"。"""
        cols = {c for c, _t in tasks._TASK_COLUMNS}
        self.assertIn("escalated_from_agent", cols)
        self.assertIn("escalated_from_model", cols)


if __name__ == "__main__":
    unittest.main()
