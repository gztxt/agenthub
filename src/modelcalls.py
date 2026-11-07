# -*- coding: utf-8 -*-
"""模型调用计量模块（借鉴 LangChat `langchat-monitor`，2026-11-07 评估落地）。

LangChat 的 `AigcModelCallLog` 模式：每次模型调用记
  model / provider / 场景 / 状态 / input+output+total token / 耗时 ms，
统计字段冗余存模型名（避免报表回表关联模型配置）。

本机收口单点：`llm._create_message()`（chat_tools_loop 两处 + complete_free
全过它）。本模块只做**记录**（append-only），不改任何既有调用语义；
报表端点 `/api/monitor/model-calls/overview` 给设置页只读块用。

表 `model_call_logs`：
  id, model, provider, scene, status, input_tokens, output_tokens,
  total_tokens, duration_ms, request_id, created_at
  （status: success|error；scene: agent_chat|manager|memory|kb|refscout|other）
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import db

TABLE = "model_call_logs"

# 场景白名单（报表按 scene 聚合；未知场景落 "other"，不丢弃——计量面宁可多记不可漏记）
SCENES = ("agent_chat", "manager", "memory", "kb", "refscout", "chat", "free", "other")

_SCHEMA = """
CREATE TABLE IF NOT EXISTS model_call_logs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    model TEXT NOT NULL,            -- 冗余存模型名（LangChat 口径：报表不回表关联模型配置）
    provider TEXT DEFAULT '',       -- 模型名里 "/" 前段（alibaba/qwen3.8 → alibaba）
    scene TEXT NOT NULL DEFAULT 'other',
    status TEXT NOT NULL DEFAULT 'success',  -- success|error
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER,
    request_id TEXT,
    error TEXT,                     -- 仅 status=error 时有值（截断 500 字，**不回显凭据**）
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_mcl_created ON model_call_logs(created_at);
CREATE INDEX IF NOT EXISTS idx_mcl_scene ON model_call_logs(scene, created_at);
CREATE INDEX IF NOT EXISTS idx_mcl_model ON model_call_logs(model, created_at);
"""


def ensure_schema() -> None:
    """幂等建表。由 main.startup() 在 db.init_db 之后调用（同 modelgate/tasks 形态）。
    若连接尚未初始化（L0 独立调本模块）则跳过——建表交给调用方的 init_db。"""
    if not db.is_open():
        return
    db.execute_script(_SCHEMA)


def _now() -> str:
    return db._now()


def _split_provider(model: str) -> str:
    """`alibaba/qwen3.8-flash` → `alibaba`；无 "/" → 空串（provider 不可判时不编造）。"""
    m = model or ""
    return m.split("/", 1)[0] if "/" in m else ""


def record(model: str, usage: Optional[dict], scene: str = "other",
           status: str = "success", duration_ms: Optional[int] = None,
           request_id: Optional[str] = None, error: Optional[str] = None) -> None:
    """记一条模型调用（LangChat `ModelCallRecorder.record` 的等价）。

    幂等安全性：记账失败绝不允许带倒真实 LLM 调用——整段 try，失败仅打印一行。
    连接未初始化（L0 未 init_db）则静默跳过，不打断调用方。
    token 三列：上游 usage 缺失时用 0（不丢行——行本身就是「调用发生过」的证据，
    与 asset_audit 的 append-only 纪律同构；「没用量」和「没调用」必须可区分）。
    """
    try:
        if not db.is_open():
            return
        u = usage or {}
        in_t = int(u.get("input_tokens") or u.get("prompt_tokens") or 0)
        out_t = int(u.get("output_tokens") or u.get("completion_tokens") or 0)
        total = int(u.get("total_tokens") or 0)
        if not total:
            total = in_t + out_t
        err = (str(error or ""))[:500] if status == "error" else None
        db.execute(
            "INSERT INTO model_call_logs(model, provider, scene, status, input_tokens,"
            " output_tokens, total_tokens, duration_ms, request_id, error, created_at)"
            " VALUES(?,?,?,?,?,?,?,?,?,?,?)",
            (model or "unknown", _split_provider(model or ""),
             scene if scene in SCENES else "other",
             "success" if status not in ("error", "success") else status,
             in_t, out_t, total, duration_ms,
             request_id or None, err, _now()))
    except Exception as e:  # noqa: BLE001
        print(f"[modelcalls] 记录失败（不打断调用）：{type(e).__name__}: {str(e)[:120]}")


def overview(window_days: int = 7) -> Dict[str, Any]:
    """模型调用总览（LangChat `/api/v1/monitor/model-calls/overview` 的等价）。

    返回：总调用数 / 总 token / 按模型聚合（调用数+token+平均耗时）/ 按场景聚合。
    纯聚合，走 idx_mcl_created 时间窗过滤；无数据返回零值（不是报错——「没计量」是合法状态）。
    db 未初始化 ⇒ 返回全零值（绝不让「看报表」这个只读动作因连接缺失而 500）。
    """
    import datetime as _dt
    if not db.is_open():
        return {"window_days": window_days, "total_calls": 0, "total_tokens": 0,
                "by_model": [], "by_scene": {}, "as_of": _dt.datetime.now(_dt.timezone.utc).isoformat(),
                "note": "db 未初始化（计量尚未启动）"}
    end = _dt.datetime.now(_dt.timezone.utc)
    start = end - _dt.timedelta(days=max(1, int(window_days)))
    prefix = start.strftime("%Y-%m-%dT%H")  # 粗窗口（created_at 是 ISO 全串，前缀比较够用）
    rows = db.query(
        "SELECT model, scene, status, COUNT(*) AS n,"
        " SUM(input_tokens) AS tin, SUM(output_tokens) AS tout, SUM(total_tokens) AS ttotal,"
        " AVG(duration_ms) AS avg_ms"
        " FROM model_call_logs WHERE created_at >= ? GROUP BY model, scene, status",
        (prefix,))
    by_model: Dict[str, Dict[str, Any]] = {}
    by_scene: Dict[str, int] = {}
    total_calls = 0
    total_tokens = 0
    for r in rows:
        total_calls += r["n"]
        total_tokens += int(r["ttotal"] or 0)
        m = r["model"]
        d = by_model.setdefault(m, {"model": m, "provider": _split_provider(m),
                                     "calls": 0, "total_tokens": 0, "avg_ms": 0.0})
        d["calls"] += r["n"]
        d["total_tokens"] += int(r["ttotal"] or 0)
        ms = r["avg_ms"]
        d["avg_ms"] = round((d["avg_ms"] * (d["calls"] - 1) + (ms or 0)) / d["calls"], 1)
        sc = r["scene"]
        by_scene[sc] = by_scene.get(sc, 0) + r["n"]
    for m in by_model.values():
        if not m.get("avg_ms"):
            m["avg_ms"] = 0.0
    return {
        "window_days": window_days,
        "total_calls": total_calls,
        "total_tokens": total_tokens,
        "by_model": sorted(by_model.values(), key=lambda x: -x["total_tokens"]),
        "by_scene": by_scene,
        "as_of": end.isoformat(),
    }


class _ApiRouter:
    """极简路由包装：modelcalls 不 import fastapi（保持底层模块无框架依赖，
    与 llm.py 同纪律——它只在 main 里被 `include_router` 挂进去，但自身不引 FastAPI，
    便于 L0 空 HOME 下直接测 record/overview）。"""


router = None


def build_router():
    """main 导入 fastapi 可用时调用，返回挂接了三个端点的 APIRouter。"""
    global router
    from fastapi import APIRouter, Query
    router = APIRouter()

    @router.get("/api/monitor/model-calls/overview")
    async def mcl_overview(window_days: int = Query(default=7, ge=1, le=90)):
        """模型调用计量总览（只读；设置页报表块消费）。"""
        return overview(window_days)

    @router.get("/api/monitor/model-calls")
    async def mcl_list(limit: int = Query(default=50, le=500), scene: str = ""):
        """最近 N 条明细（admin 排查用；只读）。db 未初始化 ⇒ 空清单（不 500）。"""
        if not db.is_open():
            return {"rows": [], "count": 0}
        sql = ("SELECT model, provider, scene, status, input_tokens, output_tokens,"
                 " total_tokens, duration_ms, request_id, created_at FROM model_call_logs")
        args: list = []
        where = []
        if scene:
            where.append("scene=?")
            args.append(scene)
        if where:
            sql += " WHERE " + " AND ".join(where)
        sql += " ORDER BY id DESC LIMIT ?"
        args.append(limit)
        return {"rows": db.query(sql, tuple(args)), "count": min(limit, 500)}

    return router
