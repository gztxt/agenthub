#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""子代理/派发会话不得进「会话历史」——L0 闸门（2026-11-09）。

现场：生产 `chat_sessions` 200 行里 **70 行** id 是 `task-<trace>-<task_id>`
（`tasks_runner._run_task` 派发子任务时写进去的），会话历史下拉只取 30 条
⇒ 真实用户会话被挤到看不见，用户报「子代理会话记录不该出现在会话历史」。

判据落点（三层，缺一层就会再漂）：
1. **行为**：同一张临时表，加不加 `user_sessions_where()` 的结果必须不同——
   不加时 `task-*` 会混进来（红臂在同一产物内，证明「改前确实坏」）。
2. **口径唯一**：前缀常量只在 `session_scope` 里定义；`api_sessions` 与
   `datastore` **两处都必须走它**（AST 闸门）。各写一份字面量必然漂移，
   codex「白名单→排除集」那次就是列表侧与校验侧对不上。
3. **别名不自说自话**：`user_sessions_where(alias)` 必须真的用传入的表别名，
   写死 `s.` 在 join 查询里会指错表并静默放行。

不 import `src.main`（会跑 lifespan 开真库绑端口）；不碰生产 agents.db。
"""
from __future__ import annotations

import ast
import sqlite3
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import session_scope as sc  # noqa: E402


def _mkdb(rows):
    c = sqlite3.connect(":memory:")
    c.row_factory = sqlite3.Row
    c.execute("CREATE TABLE chat_sessions(id TEXT PRIMARY KEY, agent_id TEXT NOT NULL,"
              " title TEXT, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)")
    c.executemany("INSERT INTO chat_sessions VALUES(?,?,?,?,?)", rows)
    return c


_ROWS = [
    ("task-abc123-t1", "codebuddy", "子任务一", "2026-11-09T00:00:00Z", "2026-11-09T00:03:00Z"),
    ("task-abc123-t_sum", "codebuddy", "汇总", "2026-11-09T00:04:00Z", "2026-11-09T00:05:00Z"),
    ("01a1bf56dead", "codebuddy", "真实会话", "2026-11-09T00:01:00Z", "2026-11-09T00:02:00Z"),
    ("e2e-checkpoint", "codex", "另一个真实会话", "2026-11-09T00:00:00Z", "2026-11-09T00:00:30Z"),
]


class TestPredicate(unittest.TestCase):
    def test_prefix(self):
        self.assertTrue(sc.is_subagent_session_id("task-abc-t1"))
        self.assertTrue(sc.is_subagent_session_id("task-abc-t_sum"))
        self.assertFalse(sc.is_subagent_session_id("01a1bf56dead"))
        self.assertFalse(sc.is_subagent_session_id(""))
        self.assertFalse(sc.is_subagent_session_id(None))

    def test_alias_is_honoured(self):
        """写死 s. 的实现在 join 场景会静默放行——这里直接钉字面。"""
        self.assertIn("x.id NOT LIKE", sc.user_sessions_where("x"))
        self.assertNotIn(" s.", sc.user_sessions_where("x"))


class TestSqlFilter(unittest.TestCase):
    def _q(self, where, agent=None):
        sql = ("SELECT s.id FROM chat_sessions s"
               + (" WHERE " + where if where else ""))
        params = ()
        if agent:
            sql += " AND s.agent_id=?" if where else " WHERE s.agent_id=?"
            params = (agent,)
        sql += " ORDER BY s.updated_at DESC LIMIT ?"
        return sql, params + (30,)

    def test_subagent_rows_are_excluded(self):
        c = _mkdb(_ROWS)
        sql, params = self._q(sc.user_sessions_where("s"))
        got = [r["id"] for r in c.execute(sql, params)]
        self.assertEqual(got, ["01a1bf56dead", "e2e-checkpoint"])

    def test_red_arm_without_filter_leaks(self):
        """红臂：不接口径时 task-* 一定混进来——证明判据不是恒真。"""
        c = _mkdb(_ROWS)
        sql, params = self._q("")
        got = [r["id"] for r in c.execute(sql, params)]
        self.assertIn("task-abc123-t1", got)

    def test_agent_filter_still_works(self):
        c = _mkdb(_ROWS)
        sql, params = self._q(sc.user_sessions_where("s"), agent="codebuddy")
        got = [r["id"] for r in c.execute(sql, params)]
        self.assertEqual(got, ["01a1bf56dead"])


class TestBothSurfacesUseIt(unittest.TestCase):
    """列表侧有两处实现（/api/sessions 与 datastore 统一入口），必须同口径。"""

    def _calls(self, rel):
        tree = ast.parse((_REPO / rel).read_text(encoding="utf-8"))
        names = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute):
                if n.func.attr == "user_sessions_where":
                    names.append(n.func.value.id if isinstance(n.func.value, ast.Name) else "?")
        return names

    def test_api_sessions_uses_scope(self):
        self.assertEqual(self._calls("src/api_sessions.py"), ["session_scope"])

    def test_datastore_uses_scope(self):
        self.assertEqual(self._calls("src/datastore.py"), ["session_scope"])


if __name__ == "__main__":
    unittest.main()
