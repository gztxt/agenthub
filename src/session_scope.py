"""会话可见性口径：哪些 `chat_sessions` 是「子代理/派发产物」而不是用户会话。

唯一判据来源是**生成点**而不是猜标题：`tasks_runner.py` 给每个子任务起的会话 id 是
`task-<trace_id>-<task_id>`（汇总任务同形，`task-<trace>-t_sum`）。这些行要留在
`chat_sessions` 里供协同页审计，但它们不是用户打开的会话 ⇒ 所有「会话历史」列表
必须用同一份条件排除，否则两处各写一份字面量必然漂移（见 `sessions_store` 的
codex 白名单→排除集教训）。

2026-11-09 实测：生产 `chat_sessions` 200 行里 **70 行** id 以 `task-` 开头，
把真实会话挤到下拉框 30 条之外（用户报「子代理会话记录不该出现在会话历史」）。
"""
from __future__ import annotations

#: 派发器生成的会话 id 前缀（`tasks_runner._run_task` 里 `f"task-{trace_id}-{task_id}"`）。
SUBAGENT_SESSION_PREFIX = "task-"


def is_subagent_session_id(sid: str) -> bool:
    return str(sid or "").startswith(SUBAGENT_SESSION_PREFIX)


def user_sessions_where(alias: str = "s") -> str:
    """可直接拼进 `WHERE` 的排除条件（不含前导 AND）。

    `alias` 是 `chat_sessions` 在该查询里的表别名——调用方别写死 `s`，
    否则 join 场景下会指错表、静默放行。
    """
    return f"{alias}.id NOT LIKE '{SUBAGENT_SESSION_PREFIX}%'"
