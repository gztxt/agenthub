# -*- coding: utf-8 -*-
"""统一执行语义枚举（P2 C3；手册 C3：「统一 Agent 执行语义枚举作跨框架**公共词表**」）。

【为什么必须有 —— 实测，不是假想】

`GET /api/runlog` 回包里的 `sources` 是**手写死的五个**：

    ["rest", "hub_chat", "task_exec", "cron_run", "mcp_call"]

而代码里已经在用的 source 至少还有 **`ctx_manifest`**（P1-1）与 **`resilience`**（P1-6）。
即：**回包在骗人，而且是静默的** —— 前端按它做下拉筛选，这两类事件**永远筛不到**。
这正是 C3 要消灭的形态：**写入点各自发明词、读取点各自维护一份名单**，
两边永远不会同时更新，且没有任何东西会因此报错。

【两层词表，都不许自由发挥】

- `SOURCES` —— `profile_events.source` 的**闭集**：回答「**谁在写**」；
- `OUTCOMES` —— 跨 source 的**执行结果语义闭集**：回答「**结果怎样**」；
- `STATUS_MAP` —— `source -> {原始 status -> OUTCOME}`，**逐条显式**。

**未映射不静默归类**：`canonical_outcome()` 返回 `("unknown", mapped=False)`。
于是漂移有两处能被看见：① `tests/` 的覆盖闸门变红；② 回包里 `mapped=False` 可见。
（反面做法是「默认归 ok」——那会把「忘了登记」伪装成「一切正常」，与 `vitals_loop`
函数头丢失、`code_stale` 假绿同族。）

【与别的层的关系】本模块只管**词表与映射**，不管存储：
`profile_events` 仍是唯一的 append-only 事实表（撞「不建第二权威副本」）。
"""

from __future__ import annotations

from typing import Any, Dict, Optional, Tuple

#: profile_events.source 闭集（**谁在写**）。新增 source 必须同时补 STATUS_MAP，
#: 否则覆盖闸门 `tests/test_execsem_p2_c3_l0.py` 会红。
SOURCES: Tuple[str, ...] = (
    "hub_chat",       # api_agents：单轮对话
    "task_exec",      # tasks：DAG 子任务
    "cron_run",       # cronjobs：定时作业
    "mcp_call",       # mcpgw：MCP 工具调用
    "mcp_acl_shadow", # P2 C2：影子 ACL 规则「本应拒绝」的记录（不拦，但必须可查）
    "rest",           # runlog：REST 端点留痕
    "ctx_manifest",   # P1-1：上下文清单
    "resilience",     # P1-6：出站重试的逐次尝试
    "action",         # P2 A10：统一 Action Plane 的请求/裁决/结论三拍
    "scan",           # v0.13.164：技能调用第二数据源（扫会话转录，补 hub 看不到的直读）
)

#: 跨 source 的执行结果语义闭集（**结果怎样**）。
#: `parked` 与 `blocked` 刻意分开：前者「在等人/等批」（可自愈），后者「上游失败」（要人动）。
OUTCOMES: Tuple[str, ...] = (
    "ok", "degraded", "failed", "blocked", "parked", "skipped", "cancelled", "unknown",
)

#: 逐条显式映射。**不写通配** —— 通配会把「新词」悄悄吃进来，正是本模块要防的。
STATUS_MAP: Dict[str, Dict[str, str]] = {
    "hub_chat": {"success": "ok", "fail": "failed"},
    "task_exec": {"success": "ok", "fail": "failed", "parked": "parked",
                  "submitted": "parked"},
    "cron_run": {"success": "ok", "fail": "failed", "skipped": "skipped"},
    "mcp_call": {"success": "ok", "fail": "failed"},
    # P2 C2：影子规则只观测不拦 ⇒ 命中即「有异常但未阻断」= degraded（不是 failed）。
    "mcp_acl_shadow": {"would_deny": "degraded"},
    "rest": {"success": "ok", "fail": "failed", "degraded": "degraded"},
    "ctx_manifest": {"ok": "ok", "degraded": "degraded"},
    # resilience：`ok` + `classification` 的八类错误（见 src/resilience.ERROR_KINDS）
    # P2 A10：Action Plane 三拍。`requested` 只是「登记了」，**不是结果** ⇒ skipped
    # （把登记记成 ok 会让「请求数」冒充「成功数」）；被拒是策略结论 ⇒ blocked；
    # 结论直接沿用 OUTCOMES 的词。
    "action": {"requested": "skipped", "decision:allow": "ok",
               "decision:ask": "parked", "decision:deny": "blocked",
               "settled:success": "ok", "settled:failed": "failed",
               "settled:denied": "blocked", "settled:error": "failed",
               "settled:unknown": "unknown"},
    # scan：转录采集只落「读到/调用了」这一种事实，没有失败态；出现别的词说明
    # 生产者写错了，宁可 mapping 查不到（unknown）也不要被通配吃掉。
    "scan": {"success": "ok"},
    "resilience": {"ok": "ok", "rate_limit": "degraded", "auth": "failed",
                   "not_found": "failed", "invalid_request": "failed",
                   "server": "failed", "timeout": "failed", "network": "failed",
                   "unknown": "failed"},
}


def is_known_source(source: str) -> bool:
    return source in SOURCES


def canonical_outcome(source: str, status: Any) -> Tuple[str, bool]:
    """(outcome, mapped)。

    `mapped=False` 的三种情形，**都如实报 unknown 而不是猜**：
      · source 未登记；· 该 source 未登记任何 status；· status 不在该 source 的词表里。
    """
    st = "" if status is None else str(status)
    table = STATUS_MAP.get(source)
    if not table:
        return "unknown", False
    out = table.get(st)
    return (out, True) if out else ("unknown", False)


def decorate(rows) -> list:
    """给 `profile_events` 行补 `outcome` / `mapped`（**不改原始 status**）。

    原始词必须留着：它是**事实**，而 outcome 是**解释**。只回解释会让「哪个原始词
    没被覆盖」这件事永久不可见（本仓反复栽在「解释覆盖事实」上）。
    """
    out = []
    for r in rows or []:
        d = dict(r)
        oc, mapped = canonical_outcome(d.get("source", ""), d.get("status"))
        d["outcome"] = oc
        d["mapped"] = mapped
        out.append(d)
    return out


def unmapped_sources(writes: Dict[str, set]) -> Dict[str, list]:
    """覆盖检查（供闸门用）：`{source: {原始 status...}}` 里哪些没被映射。

    返回 `{source: [未映射的 status...]}`；全部覆盖则空 dict。
    """
    bad: Dict[str, list] = {}
    for src, statuses in (writes or {}).items():
        unknown = sorted(s for s in statuses if not canonical_outcome(src, s)[1])
        if unknown or not is_known_source(src):
            bad[src] = unknown
    return bad


def vocab() -> Dict[str, Any]:
    """词表本体（给 `/api/runlog` 回包与前端下拉用）。**唯一真相源**。"""
    return {"sources": list(SOURCES), "outcomes": list(OUTCOMES),
            "status_map": {k: dict(v) for k, v in STATUS_MAP.items()}}