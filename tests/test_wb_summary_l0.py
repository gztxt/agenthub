# P3 纳入闭环 / wb_summary 端点 L0 判据（v0.13.194）
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))


def test_wb_summary_registered_before_wildcard():
    """`/api/agents/wb-summary` 必须在 `/{agent_id}` 之前注册（同 /releases 纪律），
    否则被 get_agent("wb-summary") 吃掉，永远 404。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api_agents.py")).read()
    i = src.index('"/api/agents/wb-summary"')
    j = src.index('"/api/agents/{agent_id}"')
    assert i < j, "wb-summary 路由必须在 {agent_id} 之前"


def test_wb_summary_uses_hub_db_not_tasks_module():
    """任务计数走 hub 自己的 db（tasks 表），不 import tasks 模块（循环依赖面）。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api_agents.py")).read()
    block = src[src.index('wb_summary'):]
    assert "import db as _hub_db" in block
    assert "from tasks" not in block.split('def ')[1] if 'def ' in block[100:] else True


def test_wb_summary_failure_is_optional():
    """任务面不可用时 submitted/failed_24h = None（带 reason），agent 面仍正常。"""
    src = open(os.path.join(os.path.dirname(__file__), "..", "src", "api_agents.py")).read()
    assert 'out["tasks_reason"]' in src
