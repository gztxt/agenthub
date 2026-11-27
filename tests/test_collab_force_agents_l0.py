#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：协同页「指定执行者」必须真的作数（T5，2026-11-27）。

【本文件守四条】
1. `force_agents=true` 时，非汇总节点的执行者**一律**改成用户选的，逐条留 `from → to`。
2. **汇总节点不碰** —— 它靠 `pinned_tier=top` 被抬到顶层；钉成 a1/a2 会做不了汇总。
3. `spread` 是轮转，不是「全都第一个」。
4. `tiercfg.plan` 见到 `agent_forced=True` **不再做难度升档** —— 之前的缺陷正是这里：
   `--agent pi` 被 `promote_agent` 静默换成 claude-code（run 908a6cd0ae 实证）。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import tasks_dag  # noqa: E402
import tiercfg    # noqa: E402


def _items():
    return [
        {"id": "t1", "agent_id": "claude-code", "difficulty": 2, "files": ["a.py"]},
        {"id": "t2", "agent_id": "pi", "difficulty": 1, "files": []},
        {"id": "t3", "agent_id": "codex", "difficulty": 3, "files": ["b.py"]},
    ]


class TestForceAgents(unittest.TestCase):
    def test_auto_forces_all_and_records_changes(self):
        items, note = tasks_dag._apply_forced_agents(_items(), ["codex"], "auto")
        self.assertTrue(all(i["agent_id"] == "codex" for i in items))
        self.assertEqual(note["forced"], 3)
        self.assertEqual({c["id"] for c in note["changed"]}, {"t1", "t2"})
        self.assertEqual(items[0]["agent_forced_from"], "claude-code")

    def test_spread_round_robins(self):
        items, note = tasks_dag._apply_forced_agents(_items(), ["codex", "dsh"], "spread")
        self.assertEqual([i["agent_id"] for i in items], ["codex", "dsh", "codex"])
        self.assertEqual(note["mode"], "spread")

    def test_synthesis_node_is_never_forced(self):
        items = _items() + [{"id": "t9", "agent_id": "claude-code", "difficulty": 2,
                             "pinned_tier": "top", "files": []}]
        items, note = tasks_dag._apply_forced_agents(items, ["codex"], "auto")
        self.assertEqual(items[-1]["agent_id"], "claude-code")
        self.assertEqual(note["forced"], 3)

    def test_text_only_agent_with_files_warns(self):
        _, note = tasks_dag._apply_forced_agents(_items(), ["claude"], "auto")
        self.assertTrue(note.get("warnings"))
        self.assertTrue(any("纯文本" in w for w in note["warnings"]))

    def test_empty_pool_is_noop(self):
        items, note = tasks_dag._apply_forced_agents(_items(), [], "auto")
        self.assertEqual([i["agent_id"] for i in items], ["claude-code", "pi", "codex"])
        self.assertEqual(note["forced"], 0)


class TestPlanRespectsForced(unittest.TestCase):
    def test_forced_agent_not_promoted(self):
        # 难度 3 默认会把执行者抬到 a3；强制 pi（a2）后必须原样保留
        items = [{"id": "t1", "agent_id": "pi", "difficulty": 3, "agent_forced": True}]
        out, stats = tiercfg.plan(items, catalog={"workbuddy/glm-5.4"}, checked=True,
                                  ccr_agents={"pi", "claude-code"},
                                  pool={"pi", "claude-code"})
        self.assertEqual(out[0]["agent_id"], "pi")
        self.assertEqual(out[0]["agent_tier_reason"], "forced")
        self.assertEqual(stats["agent_counts"].get("forced"), 1)

    def test_unforced_agent_still_promoted(self):
        items = [{"id": "t1", "agent_id": "pi", "difficulty": 3}]
        out, _ = tiercfg.plan(items, catalog={"workbuddy/glm-5.4"}, checked=True,
                              ccr_agents={"pi", "claude-code"},
                              pool={"pi", "claude-code"})
        self.assertEqual(out[0]["agent_id"], "claude-code")


if __name__ == "__main__":
    unittest.main()
