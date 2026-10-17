# -*- coding: utf-8 -*-
"""统一 Action Plane（P2 A10；手册：行动归一为 Action Request/Result + 生命周期）。

【病灶】今天「做一件事」在本机有**三种互不相同的表达**：
  · MCP 工具调用 —— `mcpgw.mcp_call`，能写库、能查 ACL、有 `call_id`/`idempotency_key`；
  · 终端命令 —— `term` 会话里直接敲，**不过任何准入**；
  · LLM 请求 —— `llm.acall`，有重试语义但与上面两者无可比字段。
三种表达各自记日志、各自命名、各自判成败 ⇒ 「agent 这一轮到底做了哪些行动、哪一步被谁拒了」
只能靠人把三份日志对着看。A11 已经把**读侧**的因果链接上了（`traceview`），
但写入侧缺一个**所有行动都必经的统一形状**。

【本模块给出什么】一个与传输无关的 **Action** 抽象：

    ActionRequest  谁(actor) · 对什么(target) · 做什么(kind/op) · 参数(params) · 为什么(requested_for)
    ActionDecision **PDP 的裁决**：allow / deny / ask / shadow_would_deny —— 只裁决、不执行
    ActionResult    outcome ∈ {success, failed, denied, error, unknown} + 耗时 + 证据

    submit(request) → 落 `action_requests`（status=pending）
    decide(id, decision) → 落 `action_decisions`（ACL 降为 **policy decision point**，只管裁决）
    settle(id, result) → 落 `action_results`，状态机收敛到终态

【三条刻意不做的合并】
1. **不接管 `mcpgw.mcp_call`**。本模块**零 import mcpgw**：它通过注入的 `executor`
   拿到「怎么真调」，自己不碰 MCP 会话、不碰 ACL 表。理由：① A10 立项时 `mcpgw.py`
   被并发会话占着写（单写者协议）；② 更强的理由 —— Action Plane 一旦直接 import
   MCP SDK 那一坨，就成了「MCP 专用层」，而它要的是**传输无关**。
2. **不把 ACL 判据搬进来**。`decide()` 只**接收**决定（由谁算不管），把
   `allowed/reason/rules/effect` 原样落账。ACL 仍是唯一判据源（`mcpgw._acl_check`
   的 docstring 写着「两份真相必然漂移」），Action Plane 只是它决定的**记账处**。
3. **不自动重试、不自动放行**。`outcome` 里的 `unknown` 是**一等结论**：
   执行抛了异常且拿不到结果时，行动**不是失败**而是「不知道做没做成」——
   把它记成 failed 会让调用方放心重试，而重试一个有副作用的行动正是最危险的动作。

【状态机】
    pending ──decide(allow)──▶ approved ──settle──▶ done(success|failed|error)
       │                          └────settle(unknown)──▶ unknown
       ├──decide(deny)──▶ denied（终态）
       └──decide(ask)──▶ waiting_approval（等人工；可再 decide）
    `shadow_would_deny` **不是**一种状态：它只是 allow 决定上的一个附注
    （与 C2 的 shadow 同口径：只多说话，不改生死）。

【为什么 `pending` 与 `approved` 必须分开】
「请求已登记」≠「已准许执行」。合成一个态，就会出现「记了一行 = 放行了」的读法 ——
那正是本仓反复禁的「写入成功即完成」。

存储走 `db`（幂等建表）；判定部分是纯函数 ⇒ L0 可整层测。
"""

from __future__ import annotations

import json
import time
import uuid
from typing import Any, Callable, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db

router = APIRouter()

#: 行动的种类。**写死枚举**（自由文本 = 事后聚合不了）。
KINDS = ("mcp", "term", "llm", "http", "file", "other")

#: 生命周期状态（显式列边，同 `skill_assets` 的立场：隐式规则在加态时必然出错）。
TRANSITIONS: Dict[str, tuple] = {
    "pending": ("approved", "denied", "waiting_approval"),
    "waiting_approval": ("approved", "denied"),
    "approved": ("done", "unknown"),
    "done": (),
    "denied": (),
    "unknown": ("done",),       # 事后查清（例如去下游确认了副作用确实发生）
}
STATES = tuple(TRANSITIONS)

#: 决定结果。`allow`/`deny`/`ask`/`shadow` 是 PDP 能给的三种半；`shadow` 只在
#: allow 之上附加信息，**不单独出现**（见 `validate_decision`）。
DECISIONS = ("allow", "deny", "ask")

#: 终局结论。`unknown` 与 `failed` **不可合并**（见模块 docstring 第 3 条）。
OUTCOMES = ("success", "failed", "denied", "error", "unknown")


# ── 纯判定 ────────────────────────────────────────────────────────────
def validate_request(kind: str, actor: str, target: str, op: str) -> Optional[str]:
    """请求形状校验；返回**错误原因**（None = 合法）。"""
    if (kind or "").strip() not in KINDS:
        return "kind 必须是 %s 之一：%r" % ("/".join(KINDS), kind)
    if not (actor or "").strip():
        return "actor 不能为空（无主行动无法归因，也不能过 ACL —— 空身份会被折算成 anon 而不自知）"
    if not (target or "").strip():
        return "target 不能为空（对什么动手必须写清：server/会话/模型/路径）"
    if not (op or "").strip():
        return "op 不能为空（做什么）"
    return None


def action_key(kind: str, actor: str, target: str, op: str) -> str:
    """**确定性**行动指纹：同 actor 对同 target 做同一 op ⇒ 同键。

    用途是「这件事是否已经发生过」的粗粒度去重与聚合，**不是**幂等键 ——
    真正的幂等键由执行器（如 `resilience.idempotency_key`）按规范化参数另算。
    刻意**不吃** params/时间戳：params 里常有一次性字段（游标、随机 id），
    吃进去会让同一次行动每次都像新行动。
    """
    import hashlib
    raw = "\x00".join((kind or "", actor or "", target or "", op or ""))
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:16]


def transition_reason(frm: str, to: str) -> Optional[str]:
    """能否从 `frm` 走到 `to`；返回**拒绝原因**（None = 允许）。纯函数。"""
    frm, to = (frm or "").strip(), (to or "").strip()
    if frm not in TRANSITIONS:
        return "未知状态 %r（可用 %s）" % (frm, ", ".join(STATES))
    if to not in TRANSITIONS:
        return "未知目标态 %r（可用 %s）" % (to, ", ".join(STATES))
    if to == frm:
        return "状态未变化（%s → %s）：重复推进不得当成功" % (frm, frm)
    if to not in TRANSITIONS[frm]:
        return ("不允许 %s → %s（从 %s 只能到 %s）"
                % (frm, to, frm, " / ".join(TRANSITIONS[frm]) or "（终态）"))
    return None


def validate_decision(decision: str, reason: Optional[str]) -> Optional[str]:
    """PDP 裁决校验；返回**错误原因**（None = 合法）。

    deny **必须带理由** —— 与 `admission.waived` 同口径：无理由的拒绝无法被申诉，
    也无法在事后判断「是不是规则写错了」。
    """
    d = (decision or "").strip()
    if d not in DECISIONS:
        return "decision 必须是 %s 之一：%r" % ("/".join(DECISIONS), decision)
    if d == "deny" and not (reason or "").strip():
        return "deny 必须带 reason（无理由的拒绝无法申诉，也无法判断是不是规则写错）"
    return None


def outcome_of(ok: Optional[bool], *, raised: bool = False,
               got_result: bool = True) -> str:
    """把执行器回来的三态折成 `OUTCOMES` 之一。**纯函数**。

    · `raised=True` 且 `got_result=False` ⇒ `unknown`：抛了但拿不到结果 ⇒
      **不能**记 failed（见模块 docstring 第 3 条）；
    · `ok=False` ⇒ `failed`（拿到了「没成功」这个结论）；
    · `ok is None` ⇒ `unknown`（执行器自己都说不出成没成）。
    """
    if raised and not got_result:
        return "unknown"
    if ok is None:
        return "unknown"
    return "success" if ok else "failed"


# ── 存储 ──────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS action_requests (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,
    actor       TEXT NOT NULL,
    target      TEXT NOT NULL,
    op          TEXT NOT NULL,
    params      TEXT NOT NULL DEFAULT '{}',
    requested_for TEXT,
    trace_id    TEXT,
    action_key  TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'pending',
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_req_status ON action_requests(status, created_at);
CREATE INDEX IF NOT EXISTS idx_action_req_trace ON action_requests(trace_id);
CREATE TABLE IF NOT EXISTS action_decisions (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    action_id   TEXT NOT NULL,
    decision    TEXT NOT NULL,
    reason      TEXT,
    rules       TEXT NOT NULL DEFAULT '[]',
    effect      TEXT NOT NULL DEFAULT 'enforce',
    shadow_would_deny INTEGER NOT NULL DEFAULT 0,
    shadow_rules TEXT NOT NULL DEFAULT '[]',
    by_whom     TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_action_dec_action ON action_decisions(action_id);
CREATE TABLE IF NOT EXISTS action_results (
    action_id   TEXT PRIMARY KEY,
    outcome     TEXT NOT NULL,
    ok          INTEGER,
    duration_ms INTEGER,
    error       TEXT,
    evidence    TEXT NOT NULL DEFAULT '{}',
    settled_by  TEXT NOT NULL,
    created_at  TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    db.execute_script(SCHEMA)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _new_id() -> str:
    return "act-" + uuid.uuid4().hex[:16]


def get(action_id: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    rows = db.query("SELECT * FROM action_requests WHERE id=?", (action_id,))
    return dict(rows[0]) if rows else None


def list_actions(limit: int = 100, status: Optional[str] = None) -> List[Dict[str, Any]]:
    ensure_schema()
    if status:
        rows = db.query("SELECT * FROM action_requests WHERE status=? "
                        "ORDER BY created_at DESC LIMIT ?", (status, int(limit)))
    else:
        rows = db.query("SELECT * FROM action_requests ORDER BY created_at DESC LIMIT ?",
                        (int(limit),))
    return [dict(r) for r in rows]


def submit(kind: str, actor: str, target: str, op: str, *, params: Optional[dict] = None,
           requested_for: Optional[str] = None, trace_id: Optional[str] = None,
           by_whom: str = "system") -> Dict[str, Any]:
    """登记一条 Action Request（**只登记，不执行、不裁决**）。

    `params` 落库前**只保留键名与类型摘要**：行动参数里常有凭据/业务敏感值，
    整包落库等于把凭据复制进事件表（本仓已有「detail 落库前脱敏」的军规）。
    """
    err = validate_request(kind, actor, target, op)
    if err:
        raise ValueError(err)
    ensure_schema()
    aid = _new_id()
    now = _now()
    db.execute(
        "INSERT INTO action_requests(id,kind,actor,target,op,params,requested_for,trace_id,"
        "action_key,status,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?)",
        (aid, kind.strip(), actor.strip(), target.strip(), op.strip(),
         json.dumps(_param_digest(params), ensure_ascii=False),
         requested_for, trace_id, action_key(kind, actor, target, op),
         "pending", now, now))
    rec = get(aid)
    try:
        db.log_profile_event("action", "%s/%s" % (kind.strip(), target.strip()), "requested",
                             None, trace_id=trace_id,
                             detail={"action_id": aid, "actor": actor.strip(),
                                     "op": op.strip(), "by": by_whom})
    except Exception:  # noqa: BLE001  记账不得带倒一次登记
        pass
    return rec


def _param_digest(params: Optional[dict]) -> Dict[str, Any]:
    """参数**形状**摘要：`{键: 类型}`，不吃值。非 dict 一律当空（不猜）。"""
    if not isinstance(params, dict):
        return {}
    out = {}
    for k, v in list(params.items())[:64]:
        out[str(k)[:60]] = type(v).__name__
    return out


def decide(action_id: str, decision: str, *, reason: Optional[str] = None,
           rules: Optional[List[int]] = None, effect: str = "enforce",
           shadow_would_deny: bool = False, shadow_rules: Optional[List[int]] = None,
           by: str = "system") -> Dict[str, Any]:
    """落一条 PDP 裁决并推进状态。**本函数不执行任何行动**。

    `shadow_would_deny` 只允许出现在 `allow` 上：deny 本身就是拒绝，
    「本应拒绝」对它没有增量信息；ask 阶段尚未定生死，也不该谈影子。
    """
    rec = get(action_id)
    if not rec:
        raise KeyError(action_id)
    err = validate_decision(decision, reason)
    if err:
        raise ValueError(err)
    if shadow_would_deny and decision != "allow":
        raise ValueError("shadow_would_deny 只能附在 allow 上（deny/ask 本身已表达立场）")
    to = {"allow": "approved", "deny": "denied", "ask": "waiting_approval"}[decision]
    why = transition_reason(rec["status"], to)
    if why:
        raise ValueError(why)
    now = _now()
    db.execute(
        "INSERT INTO action_decisions(action_id,decision,reason,rules,effect,"
        "shadow_would_deny,shadow_rules,by_whom,created_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (action_id, decision, reason, json.dumps(list(rules or [])),
         "shadow" if effect == "shadow" else "enforce",
         1 if shadow_would_deny else 0, json.dumps(list(shadow_rules or [])), by, now))
    db.execute("UPDATE action_requests SET status=?,updated_at=? WHERE id=?",
               (to, now, action_id))
    try:
        db.log_profile_event("action", "%s/%s" % (rec["kind"], rec["target"]),
                             "decision:%s" % decision, None,
                             trace_id=rec.get("trace_id"),
                             detail={"action_id": action_id, "reason": reason,
                                     "shadow_would_deny": bool(shadow_would_deny),
                                     "by": by})
    except Exception:  # noqa: BLE001
        pass
    return get(action_id)


def settle(action_id: str, outcome: str, *, ok: Optional[bool] = None,
           duration_ms: Optional[int] = None, error: Optional[str] = None,
           evidence: Optional[dict] = None, by: str = "system") -> Dict[str, Any]:
    """落终局结论。只允许从 `approved`（或 `unknown` 的复查）进入。

    **同一个 action 只能 settle 一次**（`action_results.action_id` 是主键）——
    「第二次 settle 把 failed 改成 success」正是「执行者自证」的老毛病。
    要改判必须走 `unknown → done` 的显式复查通道，而不是覆盖。
    """
    rec = get(action_id)
    if not rec:
        raise KeyError(action_id)
    if outcome not in OUTCOMES:
        raise ValueError("outcome 必须是 %s 之一：%r" % ("/".join(OUTCOMES), outcome))
    if rec["status"] in ("done", "unknown"):
        # `unknown` 也在这里拦：它是**待复查**，改判要走下面的专用通道，不能当普通 settle。
        raise ValueError("该 action 已 settle 过（当前 %s）：结论不得覆盖，只能走 unknown→done 复查"
                         % rec["status"])
    # `denied` 是 PDP 的终局结论，**不是**一次执行结果 —— 由 `run()` 直接写，不经此函数。
    if rec["status"] != "approved":
        raise ValueError("只有 approved 的行动才能 settle（当前 %s）：%s"
                         % (rec["status"], transition_reason(rec["status"], "done")))
    to = "unknown" if outcome == "unknown" else "done"
    now = _now()
    try:
        db.execute(
            "INSERT INTO action_results(action_id,outcome,ok,duration_ms,error,evidence,"
            "settled_by,created_at) VALUES(?,?,?,?,?,?,?,?)",
            (action_id, outcome, None if ok is None else (1 if ok else 0),
             duration_ms, error, json.dumps(evidence or {}, ensure_ascii=False), by, now))
    except Exception as e:  # noqa: BLE001
        if "UNIQUE" in str(e) or "PRIMARY" in str(e).upper():
            raise ValueError("该 action 已 settle 过（%s）：结论不得覆盖，只能走 unknown→done 复查"
                             % action_id)
        raise
    db.execute("UPDATE action_requests SET status=?,updated_at=? WHERE id=?",
               (to, now, action_id))
    try:
        db.log_profile_event("action", "%s/%s" % (rec["kind"], rec["target"]),
                             "settled:%s" % outcome, duration_ms,
                             trace_id=rec.get("trace_id"),
                             detail={"action_id": action_id, "error": error, "by": by})
    except Exception:  # noqa: BLE001
        pass
    return get(action_id)


def history(action_id: str) -> Dict[str, Any]:
    """一条行动的完整时间线：请求 → 全部裁决 → 结论。缺哪段就**如实缺**，不补空壳。"""
    rec = get(action_id)
    if not rec:
        raise KeyError(action_id)
    ensure_schema()
    decs = [dict(r) for r in db.query(
        "SELECT * FROM action_decisions WHERE action_id=? ORDER BY id", (action_id,))]
    res = [dict(r) for r in db.query(
        "SELECT * FROM action_results WHERE action_id=?", (action_id,))]
    return {"request": rec, "decisions": decs,
            "result": res[0] if res else None,
            "settled": bool(res)}


async def run(executor: Callable, *, kind: str, actor: str, target: str, op: str,
              params: Optional[dict] = None, requested_for: Optional[str] = None,
              trace_id: Optional[str] = None, decision: Optional[dict] = None,
              by_whom: str = "system") -> Dict[str, Any]:
    """**一步到位**的便捷通道：登记 → 裁决 → 执行 → 落结论。

    `decision` 由调用方给（通常是 `mcpgw._acl_check` 的结果 dict，`allowed/reason/rules`
    同形状）；不给则视为「无策略层 → allow」，**并在理由里写明 no-pdp**，
    否则事后分不清「策略放行」与「根本没有策略」。

    `executor` 是可调用对象，签名 `executor() -> (ok, evidence)`；同步/异步均可。
    执行抛异常且调用方拿不到结果 ⇒ `unknown`（见 `outcome_of`）。
    """
    import asyncio
    import inspect

    rec = submit(kind, actor, target, op, params=params,
                 requested_for=requested_for, trace_id=trace_id, by_whom=by_whom)
    aid = rec["id"]
    if decision is None:
        d = {"allowed": True, "reason": "no-pdp：调用方未提供策略裁决", "rules": []}
    else:
        d = decision
    if not d.get("allowed"):
        decide(aid, "deny", reason=d.get("reason") or "策略拒绝", rules=d.get("rules"),
               shadow_would_deny=bool(d.get("shadow_would_deny")),
               shadow_rules=d.get("shadow_rules"), by=by_whom)
        # 拒绝**不写 action_results**：`denied` 是 PDP 的终局结论，不是「执行结果」。
        # 给它编一条结果行，会让「没执行」与「执行了但被拒」在 results 表里长得一样。
        return history(aid)
    decide(aid, "allow", reason=d.get("reason"), rules=d.get("rules"),
           shadow_would_deny=bool(d.get("shadow_would_deny")),
           shadow_rules=d.get("shadow_rules"), by=by_whom)

    t0 = time.monotonic()
    ok: Optional[bool] = None
    evidence: dict = {}
    error = None
    raised = False
    got_result = False
    try:
        out = executor()
        if inspect.isawaitable(out):
            out = await out
        got_result = True
        if isinstance(out, tuple) and len(out) == 2:
            ok, evidence = bool(out[0]), (out[1] if isinstance(out[1], dict) else {})
        else:
            ok, evidence = bool(out), {}
    except Exception as e:  # noqa: BLE001
        raised = True
        error = "%s: %s" % (type(e).__name__, str(e)[:300])
    dur = int((time.monotonic() - t0) * 1000)
    outcome = outcome_of(ok, raised=raised, got_result=got_result)
    settle(aid, outcome, ok=ok, duration_ms=dur, error=error, evidence=evidence, by=by_whom)
    return history(aid)


# ── 端点 ──────────────────────────────────────────────────────────────
class SubmitIn(BaseModel):
    kind: str = Field(min_length=1, max_length=16)
    actor: str = Field(min_length=1, max_length=120)
    target: str = Field(min_length=1, max_length=200)
    op: str = Field(min_length=1, max_length=120)
    params: Optional[dict] = None
    requested_for: Optional[str] = Field(default=None, max_length=300)
    trace_id: Optional[str] = Field(default=None, max_length=64)


class DecideIn(BaseModel):
    decision: str = Field(min_length=1, max_length=16)
    reason: Optional[str] = Field(default=None, max_length=500)
    rules: Optional[List[int]] = None
    effect: str = Field(default="enforce", max_length=16)
    shadow_would_deny: bool = False
    shadow_rules: Optional[List[int]] = None


class SettleIn(BaseModel):
    outcome: str = Field(min_length=1, max_length=16)
    ok: Optional[bool] = None
    duration_ms: Optional[int] = None
    error: Optional[str] = Field(default=None, max_length=1000)
    evidence: Optional[dict] = None


@router.get("/api/actions")
async def list_action_requests(limit: int = Query(default=100, le=500),
                               status: Optional[str] = Query(default=None)):
    """行动清单（**只读**；`status` 过滤走白名单，未知值按空表如实返回 0 条而不是全量）。"""
    if status and status not in STATES:
        raise HTTPException(400, "未知 status=%r（可用 %s）" % (status, "/".join(STATES)))
    rows = list_actions(limit, status)
    return {"actions": rows, "count": len(rows), "states": list(STATES),
            "kinds": list(KINDS), "decisions": list(DECISIONS), "outcomes": list(OUTCOMES)}


@router.get("/api/actions/{action_id}")
async def get_action(action_id: str):
    try:
        return history(action_id)
    except KeyError:
        raise HTTPException(404, "行动未登记：%s" % action_id)


@router.post("/api/actions")
async def submit_action(body: SubmitIn):
    try:
        return submit(body.kind, body.actor, body.target, body.op, params=body.params,
                      requested_for=body.requested_for, trace_id=body.trace_id, by_whom="api")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/actions/{action_id}/decide")
async def decide_action(action_id: str, body: DecideIn):
    try:
        return decide(action_id, body.decision, reason=body.reason, rules=body.rules,
                      effect=body.effect, shadow_would_deny=body.shadow_would_deny,
                      shadow_rules=body.shadow_rules, by="api")
    except KeyError:
        raise HTTPException(404, "行动未登记：%s" % action_id)
    except ValueError as e:
        raise HTTPException(409 if "不允许" in str(e) or "状态未变化" in str(e) else 400, str(e))


@router.post("/api/actions/{action_id}/settle")
async def settle_action(action_id: str, body: SettleIn):
    try:
        return settle(action_id, body.outcome, ok=body.ok, duration_ms=body.duration_ms,
                      error=body.error, evidence=body.evidence, by="api")
    except KeyError:
        raise HTTPException(404, "行动未登记：%s" % action_id)
    except ValueError as e:
        raise HTTPException(409 if "不允许" in str(e) or "状态未变化" in str(e)
                            or "已 settle" in str(e) else 400, str(e))
