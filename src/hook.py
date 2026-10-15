"""Hook 遥测路由（Agent_Manager agent_http.rs 语义移植）

端点：
  POST /telemetry/events/{source}   外部 Agent 推送事件（先落盘账本，异步处理不丢）
  GET  /telemetry/events            查询账本
  GET  /telemetry/usage/summary     Token 用量聚合（同 session 覆盖式口径）

鉴权：设了 HOOK_AUTH_TOKEN 则必须 Bearer 携带；未设则仅允许回环来源。
"""
import os
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import db

router = APIRouter()

ALLOWED_SOURCES = {"codex", "claude", "qoder", "workbuddy", "minimax", "kimi",
                   "pi", "jcode", "custom"}


class TelemetryEvent(BaseModel):
    session_id: str = Field(min_length=1, max_length=200)
    event: str = Field(min_length=1, max_length=100)   # session_usage / hook / ...
    cwd: Optional[str] = None
    usage: Optional[Dict[str, Any]] = None             # input/output/cached tokens
    usage_scope: Optional[str] = None                  # session | turn
    data: Optional[Dict[str, Any]] = None              # 其它负载原样入档


def _effective_client_ip(request: Request) -> str:
    """TRUST_PROXY=1 时信任反代，取 X-Forwarded-For 的最后一跳；默认行为与旧版完全一致。"""
    if os.getenv("TRUST_PROXY", "0") == "1":
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[-1].strip()
    return request.client.host if request.client else ""


def _check_auth(request: Request) -> None:
    token = os.getenv("HOOK_AUTH_TOKEN", "")
    if token:
        got = request.headers.get("authorization", "")
        if got != f"Bearer {token}":
            raise HTTPException(status_code=401, detail="invalid hook token")
        return
    client = _effective_client_ip(request)
    if client not in ("127.0.0.1", "::1", "localhost"):
        print(f"[hook] 拒绝：未设 HOOK_AUTH_TOKEN 且来源非回环（{client}）")
        raise HTTPException(status_code=403, detail="hook writes allowed from loopback only")


@router.post("/telemetry/events/{source}")
async def push_event(source: str, body: TelemetryEvent, request: Request):
    # 本模块自带鉴权；writeauth 对 /telemetry/events/ 的豁免**以本行为前提**
    # （2026-10-12 实测：它此前只定义、全仓零调用 ⇒ 豁免当时没有依据）。
    # 恒等回归：tests/test_writeauth.py::test_telemetry_exempt_premise_holds_hook_enforces_auth。
    _check_auth(request)
    if source not in ALLOWED_SOURCES:
        # 与 Agent_Manager 一致：只收已知 adapter，但留 custom 前缀扩展
        if not source.startswith("custom"):
            raise HTTPException(status_code=400,
                                detail=f"unknown source {source}; allowed: {sorted(ALLOWED_SOURCES)}|custom*")
    payload: Dict[str, Any] = {}
    if body.usage is not None:
        # 口径校验（telemetry_store.rs 规则）：input 必须为全量，cached 仅作明细
        usage = body.usage
        for k in ("input_tokens", "output_tokens", "cached_tokens"):
            v = usage.get(k)
            if v is not None and (not isinstance(v, int) or v < 0):
                raise HTTPException(status_code=400, detail=f"usage.{k} must be a non-negative int")
        payload["usage"] = {k: v for k, v in usage.items()
                            if k in ("input_tokens", "output_tokens", "cached_tokens",
                                     "reasoning_tokens", "total_cost")}
    if body.data:
        payload["data"] = body.data
    db.upsert_telemetry(source, body.session_id, body.event, body.cwd,
                        payload, body.usage_scope)
    return {"status": "recorded", "source": source, "session_id": body.session_id,
            "event": body.event, "ts": datetime.now(timezone.utc).isoformat()}


@router.get("/telemetry/events")
async def list_events(source: Optional[str] = None, session_id: Optional[str] = None,
                      limit: int = Query(default=50, le=500)):
    sql = "SELECT * FROM telemetry_events"
    params: list = []
    conds = []
    if source:
        conds.append("source=?")
        params.append(source)
    if session_id:
        conds.append("session_id=?")
        params.append(session_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, tuple(params))
    return {"events": rows, "count": len(rows)}


@router.get("/telemetry/usage/summary")
async def usage_summary():
    """每 (source,session) 取最后一行 session_usage —— 覆盖而非叠加"""
    rows = db.query("""
        SELECT source, session_id, cwd, payload, usage_scope, created_at
        FROM telemetry_events te
        WHERE event='session_usage' AND id = (
            SELECT id FROM telemetry_events
            WHERE source=te.source AND session_id=te.session_id AND event='session_usage'
            ORDER BY id DESC LIMIT 1)
    """)
    import json as _json
    agg: Dict[str, Dict[str, Any]] = {}
    for r in rows:
        usage = (_json.loads(r["payload"]) or {}).get("usage") or {}
        a = agg.setdefault(r["source"], {"sessions": 0, "input_tokens": 0,
                                         "output_tokens": 0, "cached_tokens": 0})
        a["sessions"] += 1
        for k in ("input_tokens", "output_tokens", "cached_tokens"):
            a[k] += int(usage.get(k) or 0)
    total = {k: sum(a[k] for a in agg.values())
             for k in ("sessions", "input_tokens", "output_tokens", "cached_tokens")}
    # S2 Agent 画像：成功率 / 平均耗时（profile_events 按 subject 聚合）。
    # v0.13.27 排除 source='rest'（runlog 埋的三中心检索事件）：那是「面板被谁调用」
    # 不是「agent 执行画像」，不排会被高频检索霸榜，把真实 agent 挤出前 50。
    prof = db.query("""
        SELECT source, subject, COUNT(*) n,
               SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,
               AVG(duration_ms) avg_ms, MAX(duration_ms) max_ms
        FROM profile_events WHERE source != 'rest'
        GROUP BY source, subject ORDER BY n DESC LIMIT 50""")
    profile = [{"source": p["source"], "subject": p["subject"], "calls": p["n"],
                "success_rate": round(100.0 * (p["ok"] or 0) / p["n"], 1),
                "avg_ms": round(p["avg_ms"] or 0), "max_ms": p["max_ms"]} for p in prof]
    return {"by_source": agg, "total": total, "profile": profile}


# ── P0-1：轨迹级聚合 ────────────────────────────────────────────────
def build_trajectories(rows: list) -> list:
    """把 telemetry 事件行聚成**轨迹**（按 `(source, session_id)` 分组，行序即事件序）。

    「轨迹级聚合」是 P0-1 的前置：数据集管线（`scripts/dataset_pipeline.py`）以**轨迹**
    为单位过滤/采样/切分，而不是以单条事件为单位 —— 否则同一件事会被拆成许多样本。
    **纯函数**：不查库、不调模型。
    """
    groups: Dict[tuple, dict] = {}
    for r in rows or []:
        key = (r.get("source"), r.get("session_id"))
        g = groups.setdefault(key, {"session_id": r.get("session_id"),
                                    "source": r.get("source"),
                                    "cwd": r.get("cwd"), "events": []})
        if r.get("cwd"):
            g["cwd"] = r["cwd"]
        g["events"].append({"event": r.get("event"), "usage_scope": r.get("usage_scope"),
                             "payload": r.get("payload"), "created_at": r.get("created_at")})
    return sorted(groups.values(),
                  key=lambda g: (str(g["source"] or ""), str(g["session_id"] or "")))


def trajectories_from_store(limit: int = 2000) -> list:
    """从 telemetry 账本读事件并聚成轨迹（只读）。"""
    rows = db.query("SELECT source, session_id, cwd, event, payload, usage_scope, created_at"
                    " FROM telemetry_events ORDER BY id ASC LIMIT ?", (limit,))
    return build_trajectories(rows)


@router.get("/telemetry/trajectories")
async def list_trajectories(limit: int = Query(default=200, le=2000)):
    """轨迹列表（数据集管线的只读来源）。"""
    trajs = trajectories_from_store(limit=limit)
    return {"trajectories": trajs, "count": len(trajs)}


# ── P0-1：真实轨迹源（profile_events）──────────────────────────────
# 为什么另有一条：`telemetry_events`（外部 Agent 主动推）在本机**实测只 1 行**，
# 拿它当数据集输入 = 闭环没有燃料。而 `profile_events` 是 hub **自己**每个工具环都写的
# 账本（实测 2970 行、438 行带 trace_id）⇒ 以 `trace_id` 分组才是本机真有的执行轨迹。
def build_traces(rows: list) -> list:
    """`profile_events` 行 → 执行轨迹（按 `trace_id` 分组，行序即事件序）。

    无 `trace_id` 的行**不成轨迹**（无法归属到某次执行）——宁少不多。**纯函数**。
    """
    groups: Dict[str, dict] = {}
    for r in rows or []:
        tid = r.get("trace_id")
        if not tid:
            continue
        g = groups.setdefault(str(tid), {"session_id": str(tid),
                                         "source": r.get("source"),
                                         "cwd": None, "events": []})
        g["events"].append({"event": r.get("subject"), "status": r.get("status"),
                             "duration_ms": r.get("duration_ms"),
                             "detail": r.get("detail"), "created_at": r.get("created_at")})
    return sorted(groups.values(), key=lambda g: g["session_id"])


def traces_from_store(limit: int = 2000) -> list:
    """从 `profile_events` 读事件并聚成轨迹（只读）。"""
    rows = db.query("SELECT source, trace_id, subject, status, duration_ms, detail, created_at"
                    " FROM profile_events WHERE trace_id IS NOT NULL AND trace_id<>''"
                    " ORDER BY id ASC LIMIT ?", (limit,))
    return build_traces(rows)


@router.get("/telemetry/traces")
async def list_traces(limit: int = Query(default=200, le=2000)):
    """真实执行轨迹（来自 profile_events，按 trace_id 聚合）。"""
    traces = traces_from_store(limit=limit)
    return {"traces": traces, "count": len(traces)}
