"""端到端 Trace 因果链（P2 A11）：把分散在三张表里的事件沿**同一个 `trace_id`** 聚成一条链。

## 为什么单独一个模块

手册 A11「端到端 Trace 因果链」要求：模型 / 版本 / Manifest / 状态 / Action / Artifact /
Verifier / Outcome 能在**同一条 trace** 上被一起看到。本机这三样东西**各写各的表、各记各的
时间**，而且**只有一张表带 trace 关联能力以外的一致性**：

| 表 | 记什么 | `trace_id` 现状（2026-10-17 实测 `agents.db`） |
|---|---|---|
| `profile_events` | 调用成不成、多快（画像） | ✅ 有列，3180 行里 **441 行**带值 |
| `tasks` | 任务状态机 / 模型 / verdict（P0-a/b） | ✅ 有列，3 行**全带**值 |
| `asset_audit` | 哪个资产被谁改了（审计） | 🔴 **根本没这一列** ⇒ 审计事件在 trace 视图里是**断的** |

这正是本模块存在的理由：**「有 trace_id」和「能沿 trace 追」是两件事**。
前者是列存在，后者要求三张表用**同一个口径**去 JOIN，且**缺关联时要如实报缺**，
不能把「这行没有 trace」静默算进某条链、也不能把「查不到」读成「什么都没发生」。

## 三条口径（每一条都有对应闸门）

1. **断链如实报 `orphans`，不静默补**：`trace_id IS NULL/''` 的行不属于任何链 ⇒
   单独计数。把它塞进某条链（比如按时间就近归并）会**凭空造出因果**，
   比不显示更坏 —— 与 `evidence.insufficient` 同一条立场。
2. **一个 trace 只有一条链，顺序由 `at` 决定，同刻按表内 id 破平**：
   时间戳来自三张表各自的 `created_at`，刻度不一（字符串 ISO8601，同格式可比）；
   同刻的先后**不可知** ⇒ 用 (at, table, id) 三元组稳定排序，**不让结果随查询顺序漂移**
   （否则同一个 trace 两次查询给出两个故事，G2「可回放」当场破产）。
3. **本模块零写入、零网络**（纯函数 + 一个只读查询函数），**不碰 db 连接生命周期** ——
   与 `ledgernorm`/`outbound` 同范式：能纯函数化的判定一律纯函数，便于 L0 逐条断言。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Tuple

#: 三张表的「链节点」口径：表名 → (时间列, 序号列, 展示用的摘要键)。
#: **写死在这里**而不是各处自由发挥：链的排序键必须唯一真相源，否则同一个 trace
#: 在不同调用点给出不同顺序（G2 可回放要求同一输入恒得同一输出）。
_SOURCES: Tuple[Tuple[str, str, str], ...] = (
    ("profile_events", "created_at", "id"),
    ("tasks", "started_at", "task_id"),
    ("asset_audit", "created_at", "id"),
)

#: 时间列缺失/为空时用什么兜底。**不给 `now()`** —— 那是「伪造一个时刻」，
#: 会让「不知道何时发生」伪装成「刚发生」。空串排最前，且节点带 `time_unknown=True`。
_UNKNOWN_AT = ""


def _node(table: str, at: Any, seq: Any, summary: str,
          detail: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """构造链节点。`at`/`seq` 一律转字符串 —— 同刻比较要稳定，不依赖上游给的是 int 还是 str。"""
    s = str(at) if at not in (None, "") else _UNKNOWN_AT
    return {
        "table": table,
        "at": s,
        "seq": str(seq) if seq not in (None, "") else "",
        "time_unknown": s == _UNKNOWN_AT,
        "summary": str(summary or ""),
        **(detail or {}),
    }


def _order(n: Dict[str, Any]) -> Tuple[str, str, str]:
    """链内稳定排序键：`(at, table, seq)`。

    **为什么用 table 破同刻而不是再用 id**：三张表的 id 各自从 1 开始，跨表比 id 无意义；
    表名是稳定且唯一的第二键。真正「同一表同一时刻」的两行，其先后本来就不可知，
    固定按 seq 排只是**保证可复现**，不声称那是真实因果顺序。
    """
    return (n["at"], n["table"], n["seq"])


def build_chains(nodes: Iterable[Dict[str, Any]]) -> Dict[str, Any]:
    """把带 `trace_id` 的节点聚成链，并把**没 trace 的行**如实报成 `orphans`。

    输入节点**必须**带 `trace_id` 键（值可为 None/''）。约定：
      · `trace_id` 非空 ⇒ 进对应链；
      · 空/缺 ⇒ 计入 `orphans`（按 table 分桶），**绝不归入任何链**。

    返回：
      `chains`   ：trace_id → 节点列表（已按 `_order` 排序）
      `orphans`  ：`{table: 条数}`，只含真被喂进来且缺 trace 的行
      `counts`   ：`{traces: N, nodes: M, orphans: K}`

    **排序确定性**：同一批输入恒得同一输出（`chains` 内不随喂入顺序变化）——
    这是 G2「可回放」的最低要求，闸门有一条用例专门喂两种顺序断言结果相等。
    """
    groups: Dict[str, List[Dict[str, Any]]] = {}
    orphans: Dict[str, int] = {}
    total = 0
    for raw in nodes or []:
        total += 1
        tid = (raw or {}).get("trace_id")
        n = _node(
            str(raw.get("table") or ""),
            raw.get("at"), raw.get("seq"), raw.get("summary") or "",
            detail=raw.get("detail") if isinstance(raw.get("detail"), dict) else None)
        if raw.get("time_unknown") is not None:
            n["time_unknown"] = bool(raw.get("time_unknown"))
        if tid in (None, ""):
            t = n["table"] or "unknown"
            orphans[t] = orphans.get(t, 0) + 1
            continue
        groups.setdefault(str(tid), []).append(n)
    for k in groups:
        groups[k].sort(key=_order)
    return {
        "chains": groups,
        "orphans": orphans,
        "counts": {"traces": len(groups), "nodes": total,
                   "orphans": sum(orphans.values())},
    }


def summarize(chain_result: Dict[str, Any]) -> Dict[str, Any]:
    """链结果的**可断言摘要**（给端点回包与闸门共用）。

    ⚠ `complete` 不是「成功」的同义词：它只说「这个 trace 的节点里一个 orphan 都没有」。
    一个只有一条 `fail` 事件的 trace 同样 `complete=True` —— 完整 ≠ 成功，别把两者混用。
    """
    chains = chain_result.get("chains") or {}
    lengths = sorted(len(v) for v in chains.values())
    multi = sum(1 for n in lengths if n > 1)
    return {
        "traces": len(chains),
        "nodes": chain_result.get("counts", {}).get("nodes", 0),
        "multi_node_traces": multi,
        "max_chain_len": (lengths[-1] if lengths else 0),
        "orphans": dict(chain_result.get("orphans") or {}),
        "complete": sum((chain_result.get("orphans") or {}).values()) == 0,
    }


# ── 只读查询（端点用；纯函数在上，这里只做 I/O）─────────────────────────────

def _audit_has_trace() -> bool:
    """`asset_audit` 是否已补 `trace_id` 列（存量库迁移前**没有**）。

    单独探一次而不是假定有：迁移与本模块的上线**不是同一步**（幂等 ALTER 在 init_db，
    而本函数可能在**旧库**上被调）。列不在时退回「审计不参与链」并显式标注，
    **不许**让 SQL 直接炸掉整个 trace 查询。
    """
    import db
    cols = {r["name"] for r in db.query("PRAGMA table_info(asset_audit)")}
    return "trace_id" in cols


def collect(trace_id: str, limit: int = 500) -> Dict[str, Any]:
    """按 `trace_id` 从三张表收集节点并成链（**只读**）。

    `asset_audit` 缺 `trace_id` 列时**不报错也不假装它空**：回包里带
    `audit_trace_available=False`，让调用方知道「审计这一段链条目前**无法**参与」，
    而不是把「查出来是 0 条」误读成「这个 trace 没改过资产」——
    正是手册反复禁的那类静默降级（本仓判例 193 同族）。
    """
    import db
    limit = max(1, min(int(limit or 500), 2000))
    nodes: List[Dict[str, Any]] = []
    audit_ok = _audit_has_trace()

    for r in db.query(
            "SELECT id,source,subject,status,duration_ms,created_at FROM profile_events"
            " WHERE trace_id=? ORDER BY id ASC LIMIT ?", (trace_id, limit)):
        nodes.append({"table": "profile_events", "at": r["created_at"], "seq": r["id"],
                      "trace_id": trace_id,
                      "summary": "%s/%s %s" % (r["source"], r["subject"], r["status"]),
                      "detail": {"source": r["source"], "subject": r["subject"],
                                 "status": r["status"], "duration_ms": r["duration_ms"]}})

    for r in db.query(
            "SELECT task_id,run_id,agent_id,status,model,verdict,verify_reason,goal,"
            "started_at,finished_at FROM tasks WHERE trace_id=? LIMIT ?", (trace_id, limit)):
        nodes.append({"table": "tasks", "at": r["started_at"], "seq": r["task_id"],
                      "trace_id": trace_id,
                      "summary": "task %s %s" % (r["task_id"], r["status"]),
                      "detail": {"task_id": r["task_id"], "run_id": r["run_id"],
                                 "agent_id": r["agent_id"], "status": r["status"],
                                 "model": r["model"], "verdict": r["verdict"],
                                 "verify_reason": r["verify_reason"],
                                 "goal": r["goal"], "finished_at": r["finished_at"]}})

    if audit_ok:
        for r in db.query(
                "SELECT id,asset_type,asset_slug,action,actor,verdict,created_at"
                " FROM asset_audit WHERE trace_id=? ORDER BY id ASC LIMIT ?",
                (trace_id, limit)):
            nodes.append({"table": "asset_audit", "at": r["created_at"], "seq": r["id"],
                          "trace_id": trace_id,
                          "summary": "%s/%s %s" % (r["asset_type"], r["asset_slug"],
                                                   r["action"]),
                          "detail": {"asset_type": r["asset_type"],
                                     "asset_slug": r["asset_slug"], "action": r["action"],
                                     "actor": r["actor"], "verdict": r["verdict"]}})

    built = build_chains(nodes)
    return {
        "trace_id": trace_id,
        "chain": built["chains"].get(str(trace_id), []),
        **summarize(built),
        # 显式声明能力边界：False ⇒ 审计段**不参与**这条链（不是「审计为空」）。
        "audit_trace_available": audit_ok,
    }
