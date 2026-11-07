# P3 纳入闭环 / wb_summary 端点 L0 判据（v0.13.194）
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


class TestWbSummaryL0(unittest.TestCase):

    def test_wb_summary_registered_before_wildcard(self):
        """`/api/agents/wb-summary` 必须在 `/{agent_id}` 之前注册（同 /releases 纪律），
        否则被 get_agent("wb-summary") 吃掉，永远 404。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api_agents.py")).read()
        i = src.index('"/api/agents/wb-summary"')
        j = src.index('"/api/agents/{agent_id}"')
        self.assertLess(i, j, "wb-summary 路由必须在 {agent_id} 之前")

    def test_wb_summary_uses_hub_db_not_tasks_module(self):
        """任务计数走 hub 自己的 db（tasks 表），不 import tasks 模块（循环依赖面）。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api_agents.py")).read()
        block = src[src.index('wb_summary'):]
        self.assertIn("import db as _hub_db", block)
        if "def " in block[100:]:
            self.assertNotIn("from tasks", block.split("def ")[1])

    def test_wb_summary_failure_is_optional(self):
        """任务面不可用时 submitted/failed_24h = None（带 reason），agent 面仍正常。"""
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api_agents.py")).read()
        self.assertIn('out["tasks_reason"]', src)


if __name__ == "__main__":
    unittest.main()
