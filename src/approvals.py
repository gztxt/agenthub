# -*- coding: utf-8 -*-
"""审批三态（P1-2，手册 A12）：ALLOW / DENY / **ASK** + Preview–Approve–Commit–Verify。

【与 P0-5「纳管边界四维」的分工 —— 必须先界定，否则两套健康信号打架】

| | 回答的问题 | 时间性 | 状态来源 |
|---|---|---|---|
| P0-5 边界四维 | 某 agent 纳管后，写入面是**被强制挡住**还是**只观测到** | **静态**（能力边界） | `profiles` 纳管矩阵 + 负向测试 |
| 本模块（ASK） | **这一次具体**的写请求要不要先给人看一眼 | **动态**（单次准入） | 审批票据（TTL + 单次 + 绑定） |

一句话：边界四维是「门有多大」，审批三态是「这一脚进不进」。
二者**不共享状态、不互相推导** —— 把 ASK 记进边界四维会让「能力边界」随一次批准抖动。

【状态机】

```
请求 → writeauth.decide() ∈ {allow, deny, exempt, misconfig, ask}
  ask ⇒ 建 pending 审批（带 preview）+ 回 202，**不执行**
    → POST /api/approvals/{id}/approve ⇒ 签发一次性 ticket（TTL 内、绑定 route+actor）
      → 客户端带 `x-hub-approval: <id>:<ticket>` 重发 ⇒ consume ⇒ 放行（Commit）
    → POST /api/approvals/{id}/deny ⇒ 作废，原请求永不通过
```

票据**单次使用**且绑定 `(method, path, actor)`：换路径 / 换人 / 重放一律拒。

【为什么默认空集】ASK 是**新准入语义**。把既有路由默认改成「先审批」＝行为回归，
撞「新能力不换旧默认」。故 `ASK_PREFIXES` 默认**空**，由 `HUB_ASK_PREFIXES`
（逗号分隔的前缀）显式开启。默认下本模块对请求零影响。

本模块核心是纯函数 + 可注入时钟 ⇒ L0 可整层测（不起服务、不连网）。
"""

from __future__ import annotations

import os
import secrets as _secrets
import threading
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

APPROVAL_HEADER = "x-hub-approval"
TTL_S = float(os.getenv("HUB_ASK_TTL_S", "600"))
MAX_BODY_PREVIEW = int(os.getenv("HUB_ASK_PREVIEW_CHARS", "400"))

router = APIRouter()

# ── 路由集（默认空）───────────────────────────────────────────────────
#: 需要「先批准再执行」的写路由前缀。**默认空 ⇒ 零行为变化**。
#: 例：`HUB_ASK_PREFIXES=/api/mcpgw/servers,/api/cronjobs` 开启那两个面。
_env_prefixes = tuple(p.strip() for p in
                      (os.getenv("HUB_ASK_PREFIXES", "") or "").split(",") if p.strip())


def configured_prefixes() -> Tuple[str, ...]:
    """当前生效的 ASK 前缀（读 env，便于运行时/测试控制）。"""
    return tuple(p.strip() for p in
                 (os.getenv("HUB_ASK_PREFIXES", "") or "").split(",") if p.strip())


def ask_reason(path: str) -> Optional[str]:
    """该路径是否落在 ASK 面内；是则返回理由（用于审计与回包）。"""
    for p in configured_prefixes():
        if path.startswith(p):
            return "写入面受 ASK 管控（HUB_ASK_PREFIXES 命中 %s）：需先预览并批准" % p
    return None


# ── 纯函数：预览文本的打码 ────────────────────────────────────────────
def redact(text: Any) -> Tuple[str, int]:
    """复用既有脱敏器（**不另写一份正则** —— 两份正则必然漂移）。

    `sessions_export.redact_text` 返回 (结果, 命中数)；它不抛。取不到时**宁可整段丢弃**
    也不原样吐出入参：预览是给人看的，泄漏一条凭据就把这个功能变成负资产。
    """
    s = text if isinstance(text, str) else ("" if text is None else str(text))
    try:
        import sessions_export
        out, hits = sessions_export.redact_text(s)
        return out, hits
    except Exception:  # noqa: BLE001
        return "<不可脱敏，已丢弃>", -1


def build_preview(method: str, path: str, actor: str, query: str = "",
                  body: Optional[str] = None, body_chars: Optional[int] = None) -> Dict[str, Any]:
    """待批准动作的**可读预览**（纯函数）。P-A-C-V 里的那个 P。

    ⚠ 三个刻意的取舍：
      1. **query 也打码**（`?token=` 是既有合法入口，明文进预览就是泄漏）；
      2. 正文只留**截断 + 打码后的片段**，并同时报**原始字符数**——只给片段会让
         审批人以为「就这么点」，而长度本身是判断副作用大小的关键信号；
      3. **不回显上游头**（头里全是凭据）。
    """
    q_out, q_hits = redact(query) if query else ("", 0)
    snip, b_hits = redact((body or "")[:MAX_BODY_PREVIEW])
    n = len(body or "") if body_chars is None else int(body_chars)
    return {
        "method": method.upper(),
        "path": path,
        "actor": actor,
        "query": q_out,
        "body_chars": n,
        "body_snippet": snip,
        "truncated": n > len(snip),
        "redactions": q_hits + max(b_hits, 0),
    }


# ── 存储（进程内；单机自用，重启即清空——审批是**短暂**授权，不该跨重启存活）──
_lock = threading.Lock()
_store: Dict[str, Dict[str, Any]] = {}
_MAX_PENDING = 200


def _now() -> float:
    return time.time()


def reset() -> None:
    """测试用：清空存储。"""
    with _lock:
        _store.clear()


def _gc(now: Optional[float] = None) -> None:
    """清掉超时的 pending / 已消费或过期的票据。**必须持锁调用。**"""
    t = _now() if now is None else now
    for aid in [a for a, r in _store.items() if t - r["created_at"] > TTL_S]:
        _store.pop(aid, None)


def create(method: str, path: str, actor: str, reason: str,
           preview: Optional[Dict[str, Any]] = None, now: Optional[float] = None) -> Dict[str, Any]:
    """建一条 pending 审批。返回**对外可见**的记录（**绝不含 ticket**）。"""
    t = _now() if now is None else now
    aid = _secrets.token_urlsafe(12)
    with _lock:
        _gc(t)
        if len(_store) >= _MAX_PENDING:
            # 溢出时丢**最老的 pending**（不是最新的）：审批队列不该因为刷请求而
            # 让刚提交的那条消失——那会让正常用户在忙时永远批不上。
            oldest = min(_store, key=lambda k: _store[k]["created_at"])
            _store.pop(oldest, None)
        _store[aid] = {"id": aid, "method": method.upper(), "path": path, "actor": actor,
                       "reason": reason, "preview": preview or {},
                       "state": "pending", "ticket": None, "by": None,
                       "created_at": t, "decided_at": None, "consumed_at": None}
        return public(aid)


def _public(rec: Dict[str, Any]) -> Dict[str, Any]:
    return {k: rec[k] for k in ("id", "method", "path", "actor", "reason", "preview",
                                "state", "by", "created_at") if k in rec}


def public(aid: str) -> Dict[str, Any]:
    """对外视图：**永不含 ticket**（ticket 只在 approve 的回包里出现一次）。"""
    rec = _store.get(aid)
    return _public(rec) if rec else {}


def get(aid: str) -> Optional[Dict[str, Any]]:
    with _lock:
        _gc()
        rec = _store.get(aid)
        return dict(rec) if rec else None


def pending(limit: int = 50) -> List[Dict[str, Any]]:
    with _lock:
        _gc()
        rows = [r for r in _store.values() if r["state"] == "pending"]
    rows.sort(key=lambda r: r["created_at"])
    return [_public(r) for r in rows[:limit]]


def approve(aid: str, by: str = "user", now: Optional[float] = None) -> Dict[str, Any]:
    """批准 → 签发**一次性** ticket。返回 `{approval, ticket}`（ticket 仅此一次回包）。

    幂等边界：已批准过的再批一次 → 走 `raise HTTPException(409)`（由路由层转）。
    **不换发新 ticket** —— 换发等于给「重放」开了后门。
    """
    t = _now() if now is None else now
    with _lock:
        _gc(t)
        rec = _store.get(aid)
        if not rec:
            raise KeyError(aid)
        if rec["state"] != "pending":
            raise ValueError("审批已处于 %s，不可重复批准" % rec["state"])
        rec["state"] = "approved"
        rec["ticket"] = _secrets.token_urlsafe(24)
        rec["by"] = by
        rec["decided_at"] = t
        return {"approval": _public(rec), "ticket": rec["ticket"]}


def deny(aid: str, reason: str = "", by: str = "user", now: Optional[float] = None) -> Dict[str, Any]:
    t = _now() if now is None else now
    with _lock:
        _gc(t)
        rec = _store.get(aid)
        if not rec:
            raise KeyError(aid)
        if rec["state"] != "pending":
            raise ValueError("审批已处于 %s，不可驳回" % rec["state"])
        rec["state"] = "denied"
        rec["by"] = by
        rec["decided_at"] = t
        if reason:
            rec["reason"] = "%s｜驳回：%s" % (rec["reason"], reason)
        return _public(rec)


def verify_and_consume(aid: str, ticket: str, method: str, path: str, actor: str,
                       now: Optional[float] = None) -> Tuple[bool, str]:
    """校验并**消费**票据（单次）。返回 (是否放行, 原因)。

    五道闸缺一不可：
      1. 审批存在且 state=approved；
      2. ticket 恒定时间比较（`compare_digest`）；
      3. TTL 内；
      4. **绑定三元组** (method, path, actor) 逐一相等 —— 票据不可挪用到别的路径/他人；
      5. 未被消费过（消费即置 consumed）。
    """
    t = _now() if now is None else now
    with _lock:
        _gc(t)
        rec = _store.get(aid)
        if not rec:
            return False, "审批不存在或已过期"
        if rec["consumed_at"] is not None:
            return False, "票据已被使用过（一次性）"
        if rec["state"] != "approved":
            return False, "审批状态为 %s（需先 /approve）" % rec["state"]
        if t - (rec["decided_at"] or rec["created_at"]) > TTL_S:
            return False, "票据超出 TTL（%.0fs）" % TTL_S
        if not ticket or not _secrets.compare_digest(str(ticket), str(rec["ticket"] or "")):
            return False, "票据不匹配"
        if rec["method"] != method.upper() or rec["path"] != path:
            return False, "票据绑定的动作不符（%s %s）" % (rec["method"], rec["path"])
        if rec["actor"] != actor:
            return False, "票据绑定的身份不符"
        rec["state"] = "consumed"
        rec["consumed_at"] = t
        return True, "已批准"


def split_header(value: str) -> Tuple[str, str]:
    """`x-hub-approval: <id>:<ticket>` → (id, ticket)。格式非法一律 ("", "")。"""
    v = (value or "").strip()
    if v.count(":") != 1:
        return "", ""
    aid, tk = v.split(":", 1)
    return aid.strip(), tk.strip()


# ── 读端 / 决定端（P-A-C-V 的 A）─────────────────────────────────────
class DecisionIn(BaseModel):
    reason: str = Field(default="", max_length=300)


@router.get("/api/approvals")
async def list_approvals(limit: int = Query(default=50, le=_MAX_PENDING)):
    """待批准队列（只读）。**故意不列已决定的** —— 队列页只关心还要不要人动手。"""
    rows = pending(limit)
    return {"pending": rows, "count": len(rows), "ttl_s": TTL_S,
            "ask_prefixes": list(configured_prefixes())}


@router.post("/api/approvals/{aid}/approve")
async def approve_one(aid: str, request: Request):
    try:
        out = approve(aid, by="user:approve-endpoint")
    except KeyError:
        raise HTTPException(404, "审批不存在或已过期")
    except ValueError as e:
        # 409 而不是 400：状态冲突是**幂等**语义，客户端据 409 就不再重试批准。
        raise HTTPException(409, str(e))
    # ticket 只在这一次回包里出现；本仓库其余地方（日志/审计）一律只见 id。
    return {"ok": True, "approval": out["approval"], "ticket": out["ticket"],
            "use": "重发原请求时带头 %s: %s:%s" % (APPROVAL_HEADER, aid, out["ticket"])}


@router.post("/api/approvals/{aid}/deny")
async def deny_one(aid: str, body: DecisionIn = DecisionIn()):
    try:
        return {"ok": True, "approval": deny(aid, reason=body.reason)}
    except KeyError:
        raise HTTPException(404, "审批不存在或已过期")
    except ValueError as e:
        raise HTTPException(409, str(e))