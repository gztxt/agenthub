"""端点 → scope 声明表（P0-1：只声明，不执行）。

为什么单独一张表：`writeauth.decide()` 现在只判「是否登录」，99 条写端点里 90 条
登录即全权。P0 要把它改成「identity + scope」，但**改判定前必须先把每条端点的
归属钉死**——否则改一行就是 97 条逐个猜。

纪律（照 T3 Code 的 `satisfies Record<...>` 思路，Python 用测试兜底）：
  · 新增任何写路由，必须在这里加一行；漏加则 `tests/test_authz_scopes.py` 失败；
  · 这里删掉一行而对不上真实路由，同样失败（防「表里有、路由没了」的僵尸授权）；
  · 本模块**当前不参与鉴权**。`write_gate` 仍是唯一闸门，行为零变化。
    接 enforce 是 P0-2，届时先干跑再切。

scope 边界（与 docs/HUB-AUTHZ-INVENTORY-20261109.md 一致）：
  hub.task.write      起/停/重排任务（能拉起 agent）
  hub.agent.write     agent 注册、对话、pty、UI 进程、会话改名
  hub.memory.write    L1/L2/L3 与归档
  hub.skill.write     技能调用/安装/卸载/商店登录
  hub.catalog.write   参考库、卡片、KB、准入、动作面
  hub.mcp.write       MCP server/ACL 管理与任意 call
  hub.settings.write  模型/GitHub/偏好/设置导入
  hub.ops.write       重启、杀进程、扫描、巡检、定时任务、审批
  hub.user.admin      用户与凭据管理
"""

SCOPES = frozenset({
    "hub.task.write",
    "hub.agent.write",
    "hub.memory.write",
    "hub.skill.write",
    "hub.catalog.write",
    "hub.mcp.write",
    "hub.settings.write",
    "hub.ops.write",
    "hub.user.admin",
})

#: 键 = (模块名不含 .py, HTTP 方法, 路由字面量)。路由里的 `{param}` 原样保留。
#: 105 条 = 2026-11-10 源码实测（PT-20261110-01 新增 6 条 MCP 源/安装/编排写路由）；加/删端点必须同步本表。
WRITE_ROUTE_SCOPES = {
    ("actionplane", "POST", "/api/actions"): "hub.catalog.write",
    ("actionplane", "POST", "/api/actions/{action_id}/decide"): "hub.catalog.write",
    ("actionplane", "POST", "/api/actions/{action_id}/settle"): "hub.catalog.write",
    ("admission", "POST", "/api/admission/scan"): "hub.catalog.write",
    ("admission", "POST", "/api/admission/tickets"): "hub.catalog.write",
    ("admission", "POST", "/api/admission/tickets/{slug}/{version}/stage"): "hub.catalog.write",
    ("api_agents", "POST", "/api/agents"): "hub.agent.write",
    ("api_agents", "POST", "/api/agents/detect"): "hub.agent.write",
    ("api_agents", "DELETE", "/api/agents/{agent_id}"): "hub.agent.write",
    ("api_agents", "POST", "/api/agents/{agent_id}/chat"): "hub.agent.write",
    ("api_agents", "POST", "/api/agents/{agent_id}/chat/stream"): "hub.agent.write",
    ("api_events", "POST", "/api/agents/{agent_id}/chat/events"): "hub.agent.write",
    ("api_events", "POST", "/api/agents/{agent_id}/interrupt"): "hub.agent.write",
    ("api_agents", "POST", "/api/agents/{agent_id}/ui-start"): "hub.agent.write",
    ("api_agents", "POST", "/api/agents/{agent_id}/verify"): "hub.agent.write",
    ("api_sessions", "DELETE", "/api/sessions/{session_id}"): "hub.agent.write",
    ("api_sessions", "PATCH", "/api/sessions/{session_id}"): "hub.agent.write",
    ("api_summarize", "POST", "/api/summarize"): "hub.ops.write",
    ("api_system", "POST", "/api/system/restart"): "hub.ops.write",
    ("api_vitals", "POST", "/api/vitals/sweep"): "hub.ops.write",
    ("approvals", "POST", "/api/approvals/{aid}/approve"): "hub.ops.write",
    ("approvals", "POST", "/api/approvals/{aid}/deny"): "hub.ops.write",
    ("cloudcli", "POST", "/start"): "hub.agent.write",
    ("cronjobs", "POST", "/api/jobs"): "hub.ops.write",
    ("cronjobs", "DELETE", "/api/jobs/{jid}"): "hub.ops.write",
    ("cronjobs", "PUT", "/api/jobs/{jid}"): "hub.ops.write",
    ("cronjobs", "POST", "/api/jobs/{jid}/run"): "hub.ops.write",
    ("ghsettings", "POST", "/api/settings/github/apply"): "hub.settings.write",
    ("ghsettings", "POST", "/api/settings/github/clear"): "hub.settings.write",
    ("ghsettings", "POST", "/api/settings/github/refresh"): "hub.settings.write",
    ("ghsettings", "POST", "/api/settings/github/sources"): "hub.settings.write",
    ("ghsettings", "DELETE", "/api/settings/github/sources/{sid}"): "hub.settings.write",
    ("ghsettings", "PATCH", "/api/settings/github/sources/{sid}"): "hub.settings.write",
    ("ghsettings", "POST", "/api/settings/github/test"): "hub.settings.write",
    ("githubprojects", "POST", "/api/github/clone"): "hub.ops.write",
    ("hook", "POST", "/telemetry/events/{source}"): "hub.ops.write",
    ("intake", "POST", "/task"): "hub.task.write",
    ("main", "POST", "/api/csp/report"): "hub.ops.write",
    ("main", "POST", "/api/scan/run"): "hub.ops.write",
    ("mcpgw", "POST", "/mcp/acl"): "hub.mcp.write",
    ("mcpgw", "DELETE", "/mcp/acl/{acl_id}"): "hub.mcp.write",
    ("mcpgw", "POST", "/mcp/call"): "hub.mcp.write",
    ("mcpgw", "POST", "/mcp/servers"): "hub.mcp.write",
    ("mcpgw", "POST", "/mcp/servers/probe"): "hub.mcp.write",
    ("mcpgw", "DELETE", "/mcp/servers/{sid}"): "hub.mcp.write",
    # PT-20261110-01：源 / 目录 / 安装 / 编排（6 条写路由）
    ("mcpgw", "POST", "/mcp/sources"): "hub.mcp.write",
    ("mcpgw", "PATCH", "/mcp/sources/{sid}"): "hub.mcp.write",
    ("mcpgw", "DELETE", "/mcp/sources/{sid}"): "hub.mcp.write",
    ("mcpgw", "POST", "/mcp/sources/{sid}/sync"): "hub.mcp.write",
    ("mcpgw", "POST", "/mcp/install"): "hub.mcp.write",
    ("mcpgw", "POST", "/mcp/run"): "hub.mcp.write",
    ("memory", "POST", "/api/memory/archive"): "hub.memory.write",
    ("memory", "POST", "/api/memory/l1"): "hub.memory.write",
    ("memory", "POST", "/api/memory/l1/batch"): "hub.memory.write",
    ("memory", "DELETE", "/api/memory/l1/{mid}"): "hub.memory.write",
    ("memory", "PUT", "/api/memory/l1/{mid}"): "hub.memory.write",
    ("memory", "PUT", "/api/memory/l2"): "hub.memory.write",
    ("memory", "POST", "/api/memory/l2/rebuild"): "hub.memory.write",
    ("memory", "PUT", "/api/memory/l3"): "hub.memory.write",
    ("modelcfg", "POST", "/api/settings/model/apply"): "hub.settings.write",
    ("modelcfg", "POST", "/api/settings/model/undo"): "hub.settings.write",
    ("modelcfg", "POST", "/api/settings/model/verify-live"): "hub.settings.write",
    ("prefs", "PUT", "/api/prefs/{key}"): "hub.settings.write",
    ("refscout", "DELETE", "/cards/{card_id}"): "hub.catalog.write",
    ("refscout", "POST", "/cards/{card_id}/refresh"): "hub.catalog.write",
    ("refscout", "DELETE", "/kb/probe"): "hub.catalog.write",
    ("refscout", "POST", "/kb/probe"): "hub.catalog.write",
    ("refscout", "POST", "/kb/promote"): "hub.catalog.write",
    ("refscout", "POST", "/kb/test"): "hub.catalog.write",
    ("refscout", "POST", "/scout"): "hub.catalog.write",
    ("refscout", "POST", "/sources"): "hub.catalog.write",
    ("refscout", "DELETE", "/sources/{sid}"): "hub.catalog.write",
    ("resources", "POST", "/api/resources/kill"): "hub.ops.write",
    ("settings_bundle", "POST", "/api/settings/import"): "hub.settings.write",
    ("skill", "POST", "/auto"): "hub.skill.write",
    ("skill", "POST", "/install"): "hub.skill.write",
    ("skill", "POST", "/invoke"): "hub.skill.write",
    ("skill", "DELETE", "/remove"): "hub.skill.write",
    ("skill_assets", "POST", "/api/skill-assets/{slug}"): "hub.skill.write",
    ("skill_assets", "POST", "/api/skill-assets/{slug}/transition"): "hub.skill.write",
    ("skill_store", "POST", "/acquire"): "hub.skill.write",
    ("skill_store", "POST", "/install"): "hub.skill.write",
    ("skill_store", "POST", "/login"): "hub.skill.write",
    ("skill_store", "POST", "/logout"): "hub.skill.write",
    ("tasks", "DELETE", "/api/tasks/{run_id}"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/accept"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/cancel"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/pause"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/reject"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/resume"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/retry"): "hub.task.write",
    ("tasks", "POST", "/api/tasks/{run_id}/start"): "hub.task.write",
    ("tasks_dag", "POST", "/api/tasks/decompose"): "hub.task.write",
    ("tasks_replan", "POST", "/api/tasks/{run_id}/replan"): "hub.task.write",
    ("term", "POST", "/api/term/sessions"): "hub.agent.write",
    ("term", "DELETE", "/api/term/sessions/{sid}"): "hub.agent.write",
    ("users", "POST", "/api/users"): "hub.user.admin",
    ("users", "POST", "/api/users/login"): "hub.user.admin",
    ("users", "POST", "/api/users/logout"): "hub.user.admin",
    ("users", "DELETE", "/api/users/{name}"): "hub.user.admin",
    ("users", "POST", "/api/users/{name}/credentials"): "hub.user.admin",
    ("users", "POST", "/api/users/{name}/password"): "hub.user.admin",
    ("users", "POST", "/api/users/{name}/password-clear"): "hub.user.admin",
    ("users", "PUT", "/api/users/{name}/perms"): "hub.user.admin",
    ("users", "POST", "/api/users/{name}/revoke"): "hub.user.admin",
}

#: 不参与 scope 判定的写路由：与 `writeauth.EXEMPT_PREFIXES` 逐条对应。
#: 登录/登出必须未登录可达；telemetry 由 hook.py 自带 Bearer/回环校验。
#: 入口本身不走 scope，但它们签发的会话/产生的事件仍受各自通道约束。
EXEMPT_WRITE_ROUTES = frozenset({
    ("users", "POST", "/api/users/login"),
    ("users", "POST", "/api/users/logout"),
    ("hook", "POST", "/telemetry/events/{source}"),
})

#: 27 条有副作用的写路由：起进程、发外网、改全局配置、调 LLM 或重扫。
#: P0-3 先只对这 27 条强制 scope，其余保持登录放行，降低回归面。
SIDE_EFFECT_ROUTES = frozenset({
    ("api_agents", "POST", "/api/agents/{agent_id}/chat"),
    ("api_agents", "POST", "/api/agents/{agent_id}/chat/stream"),
    ("api_events", "POST", "/api/agents/{agent_id}/chat/events"),
    ("api_events", "POST", "/api/agents/{agent_id}/interrupt"),
    ("api_agents", "POST", "/api/agents/{agent_id}/ui-start"),
    ("api_summarize", "POST", "/api/summarize"),
    ("api_system", "POST", "/api/system/restart"),
    ("api_vitals", "POST", "/api/vitals/sweep"),
    ("cloudcli", "POST", "/start"),
    ("cronjobs", "POST", "/api/jobs/{jid}/run"),
    ("ghsettings", "POST", "/api/settings/github/apply"),
    ("githubprojects", "POST", "/api/github/clone"),
    ("main", "POST", "/api/scan/run"),
    ("mcpgw", "POST", "/mcp/call"),
    # PT-20261110-01：发外网 / 起进程 / 改配置的三条
    ("mcpgw", "POST", "/mcp/sources/{sid}/sync"),
    ("mcpgw", "POST", "/mcp/install"),
    ("mcpgw", "POST", "/mcp/run"),
    ("modelcfg", "POST", "/api/settings/model/apply"),
    ("resources", "POST", "/api/resources/kill"),
    ("settings_bundle", "POST", "/api/settings/import"),
    ("skill_assets", "POST", "/api/skill-assets/{slug}"),
    ("tasks", "POST", "/api/tasks/{run_id}/start"),
    ("tasks", "POST", "/api/tasks/{run_id}/retry"),
    ("tasks", "POST", "/api/tasks/{run_id}/resume"),
    ("tasks_dag", "POST", "/api/tasks/decompose"),
    ("tasks_replan", "POST", "/api/tasks/{run_id}/replan"),
    ("term", "POST", "/api/term/sessions"),
})


def scope_for(module: str, method: str, path: str):
    """返回该写路由的 scope；未声明返回 None（测试会把它判成失败）。"""
    return WRITE_ROUTE_SCOPES.get((module, method.upper(), path))


def is_side_effect(module: str, method: str, path: str) -> bool:
    return (module, method.upper(), path) in SIDE_EFFECT_ROUTES


# ── 运行时反查（P0-2 干跑用）───────────────────────────────────────────────
# 闸门手里只有 (method, path)，拿不到「模块名」；声明表按模块分键，故在这里
# 折一张 path 索引。构建时若同一 (method, path) 在不同模块落到不同 scope，
# 直接抛错——歧义必须显式消解，不许运行时随机挑一个。
def _build_path_index():
    out = {}
    for (mod, method, path), sc in WRITE_ROUTE_SCOPES.items():
        key = (method, path)
        if key in out and out[key] != sc:
            raise RuntimeError(f"scope 反查歧义：{key} → {out[key]} / {sc}")
        out[key] = sc
    return out


SCOPE_BY_PATH = _build_path_index()

import re  # noqa: E402 —— 本段专用；放这里避免动文件头


def _compile_path(tpl: str):
    """把 `/api/memory/l1/{mid}` 编译成正则；`{...}` 只吃一段非斜杠字符。"""
    parts = re.split(r"(\{[^}]+\})", tpl)
    pattern = "".join(r"[^/]+" if p.startswith("{") else re.escape(p) for p in parts)
    return re.compile("^" + pattern + "$")


#: 运行时 `request.url.path` 是**实际值**（/api/memory/l1/42），不是声明表里的
#: 模板（/api/memory/l1/{mid}）；带参路由必须靠模板匹配，否则干跑全变 unscoped。
#: 无参数的精确项排在前面，避免 `/api/agents/detect` 被 `{agent_id}` 吞掉。
_PATH_PATTERNS = [
    (m, _compile_path(p), sc)
    for (m, p), sc in sorted(SCOPE_BY_PATH.items(), key=lambda kv: "{" in kv[0][1])
]


def scope_for_path(method: str, path: str):
    """运行时按 (method, path) 反查所需 scope；未声明返回 None。

    先精确命中，再按模板匹配（`/api/memory/l1/42` → `/api/memory/l1/{mid}`）。
    """
    m = method.upper()
    exact = SCOPE_BY_PATH.get((m, path))
    if exact:
        return exact
    for pm, rx, sc in _PATH_PATTERNS:
        if pm == m and rx.match(path):
            return sc
    return None


def dryrun_verdict(method: str, path: str, scopes):
    """P0-2 干跑：只算「若 enforce 会怎样」，**绝不改变准入**。

    返回 (tag, needed_scope, why)：
      legacy       scopes is None ⇒ 沿用现行，会放行
      would_allow  会话已持该 scope
      would_deny   会话缺该 scope（干跑期仍放行，只记账）
      unscoped     端点没有 scope 声明（正常不该发生，出现即要补表）
    """
    needed = scope_for_path(method, path)
    if needed is None:
        return "unscoped", None, "端点无 scope 声明"
    if scopes is None:
        return "legacy", needed, "NULL=沿用现行，会放行"
    if needed in scopes:
        return "would_allow", needed, "会话已持该 scope"
    return "would_deny", needed, "会话缺该 scope（干跑不拒）"


# ── P0-3 enforce（默认关；只强制 22 条副作用端点）───────────────────────────
# SIDE_EFFECT_ROUTES 按 (module, method, path) 分键，但闸门手里只有 (method, path)
# 且 path 是具体值（/api/tasks/run-1/start），故同样建 path 索引 + 模板匹配。
def _build_side_index():
    exact = set()
    patterns = []
    for (_mod, method, path) in SIDE_EFFECT_ROUTES:
        if "{" in path:
            patterns.append((method, _compile_path(path)))
        else:
            exact.add((method, path))
    return exact, patterns


_SIDE_EXACT, _SIDE_PATTERNS = _build_side_index()


def is_side_effect_path(method: str, path: str) -> bool:
    """运行时按 (method, path) 判是否副作用端点；带参路由走模板匹配。"""
    m = method.upper()
    if (m, path) in _SIDE_EXACT:
        return True
    return any(pm == m and rx.match(path) for pm, rx in _SIDE_PATTERNS)


def enforce_verdict(method: str, path: str, scopes, enabled: bool):
    """P0-3 准入判定（纯函数）：返回 (allow, tag, needed_scope, why)。

    只有四个条件**同时**成立才拒：enabled、scopes 非 None（显式受限会话）、
    缺所需 scope、且该端点在 22 条副作用集合内。其余一律放行 ——
    这是「先只对副作用端点强制」的落地，其余 75 条保持登录即放行。
    """
    needed = scope_for_path(method, path)
    if not enabled:
        return True, "disabled", needed, "HUB_AUTHZ_ENFORCE 未开启"
    if scopes is None:
        return True, "legacy", needed, "NULL=沿用现行，放行"
    if needed is None:
        return True, "unscoped", None, "端点无 scope 声明，放行（待补表）"
    if needed in scopes:
        return True, "would_allow", needed, "会话已持该 scope"
    if not is_side_effect_path(method, path):
        return True, "would_deny_but_not_side_effect", needed, "仅对 22 条副作用端点强制"
    return False, "deny", needed, "会话缺该 scope 且端点有外部副作用"
