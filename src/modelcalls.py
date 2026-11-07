# -*- coding: utf-8 -*-
"""模型调用计量：设置页「模型计量」的**全 agent 用量**报表 + agenthub 自身明细。

数据源口径（2026-11-07 改判，见 agent-knowledge/）：
  唯一覆盖「所有 agent」的是 CCR 账本 `usage_events`
  （`~/.claude-code-router/app-data/usage.sqlite`，`client` 列辨识调用来源），
  由 `usage_ledger` 以 `?mode=ro` 只读聚合。原实现读自建表 `model_call_logs`，
  而那张表只由 `llm._create_message()` 写（agenthub 自己的 manager LLM 客户端），
  其它 agent（pi/claude/codex/codebuddy/grok/jcode…）直连 CCR 3456、一条都不进
  ⇒ 上线后恒 0 行、页面恒显「暂无计量记录」。典型的「两套并行实现接了空的那套」。

两套数据各归各位，别再接错：
  * `overview()` / `GET /api/monitor/model-calls/overview` → **全 agent 用量**（本报表，
    数据源 = CCR usage_events，按 model 与 client 聚合，带均耗时）。
  * `GET /api/monitor/model-calls` 明细 → 同上的最近 N 条（admin 排查用）。
  * `record()` / `model_call_logs` → agenthub **自身** manager LLM 调用的
    append-only 明细（status / error / request_id），仍由 `llm._create_message()` 埋点写入，
    保留不删：它是「调用质量排错」口径，职责与全 agent 计量报表不同，**不是报表数据源**。

返回结构**保留**旧契约（total_calls / total_tokens / by_model[] / by_scene /
window_days / as_of），另增 `rows`（2026-11-08）：一行 = UTC 日 × client × model，
供前端渲染「Agent / 模型 / 日期 / 调用 / 用量 / 耗时」可排序明细表。
旧字段仍由 `summary()` 汇总（total 不受 limit 截断），`rows` 由 `daily()` 单独取，
两者同窗口同源、职责不重叠；前端在 rows 缺失时可退化成旧的按模型视图。
`by_scene` 沿用旧键名，内容改为 **client 分布**（各 agent 来源的调用数）。

fail-closed：CCR 账本缺失 / 打不开 ⇒ `overview()` 回 `note` 让前端显示原因，
**绝不返回 0 冒充「没调用」**（usage_ledger 口径：静默 0 = 谎报）。
"""
from __future__ import annotations

import os
import time
from typing import Any, Dict, Optional

import db
import usage_ledger

TABLE = "model_call_logs"

# 场景白名单（仅 record()/model_call_logs 明细用；overview 的分组维度是 CCR 的 client）
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
    """幂等建表（agenthub 自身 LLM 调用明细表）。由 main.startup() 调用。
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
    """记一条 **agenthub 自身** LLM 调用明细（LangChat `ModelCallRecorder.record` 等价）。

    ⚠ 这不是「所有 agent 用量」的数据源（见模块 docstring）——只记经
    `llm._create_message()` 的 manager 调用；全 agent 用量看 `overview()` / CCR 账本。

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
    """**所有 agent** 的模型用量总览（数据源 = CCR usage_events 只读账本）。

    `window_days`（天）换算成小时交给 `usage_ledger.window_bounds()`；
    `summary(group_by=("client","model"))` 一次取回，按 model 聚合 `by_model`
    （avg_ms 按调用数加权）、按 client 聚合 `by_scene`，总计取自 `total`（不受 limit 截断）。

    返回结构兼容前端 `mclRender()`：total_calls / total_tokens / by_model[] /
    by_scene / window_days / as_of，另加 source / attribution / cost_usd，以及
    2026-11-08 新增的 rows / rows_ok / rows_note（按天 × client × model 明细）。
    账本不可用 ⇒ 带 `note` 的零值结构（前端显示原因），不静默伪装成「没调用」。
    """
    import datetime as _dt
    days = max(1, int(window_days))
    since, until = usage_ledger.window_bounds(days * 24)
    data = usage_ledger.summary(group_by=("client", "model"),
                                since=since, until=until,
                                limit=usage_ledger.MAX_LIMIT)
    # 表格行单独取：与 summary 同窗口同账本，但维度是「日 × client × model」。
    # daily() 不设 LIMIT ⇒ 行数即真实分组数，不会静默少报。
    daily = usage_ledger.daily(since=since, until=until)
    rows = daily["rows"] if daily.get("ok") else []
    rows_ok = bool(daily.get("ok"))
    rows_note = None if rows_ok else "按天明细不可用：" + str(daily.get("error") or "unknown")
    as_of = _dt.datetime.now(_dt.timezone.utc).isoformat()
    if not data.get("ok"):
        return {"window_days": days, "total_calls": 0, "total_tokens": 0,
                "by_model": [], "by_scene": {}, "as_of": as_of,
                "source": "ccr_usage_events",
                "rows": rows, "rows_ok": rows_ok, "rows_note": rows_note,
                "note": "CCR 用量账本不可用：" + str(data.get("error") or "unknown")}
    total = data.get("total") or {}
    by_model: Dict[str, Dict[str, Any]] = {}
    by_scene: Dict[str, int] = {}
    for r in data.get("rows") or []:
        m = r.get("model") or "unknown"
        c = r.get("client") or "unknown"
        n = int(r.get("requests") or 0)
        t = int(r.get("total_tokens") or 0)
        ms = float(r.get("avg_ms") or 0)
        d = by_model.setdefault(m, {"model": m, "provider": _split_provider(m),
                                    "calls": 0, "total_tokens": 0, "_ms_num": 0.0})
        d["calls"] += n
        d["total_tokens"] += t
        d["_ms_num"] += ms * n
        by_scene[c] = by_scene.get(c, 0) + n
    for d in by_model.values():
        d["avg_ms"] = round(d.pop("_ms_num") / d["calls"], 1) if d["calls"] else 0.0
    return {
        "window_days": days,
        "total_calls": int(total.get("requests") or 0),
        "total_tokens": int(total.get("total_tokens") or 0),
        "by_model": sorted(by_model.values(), key=lambda x: -x["total_tokens"]),
        "by_scene": by_scene,
        "as_of": as_of,
        "source": "ccr_usage_events",
        "attribution": data.get("attribution"),
        "cost_usd": total.get("cost_usd"),
        "rows": rows, "rows_ok": rows_ok, "rows_note": rows_note,
    }


class _ApiRouter:
    """极简路由包装：modelcalls 不 import fastapi（保持底层模块无框架依赖，
    与 llm.py 同纪律——它只在 main 里被 `include_router` 挂进去，但自身不引 FastAPI，
    便于 L0 空 HOME 下直接测 record/overview）。"""


router = None


def build_router():
    """main 导入 fastapi 可用时调用，返回挂接了两个端点的 APIRouter。"""
    global router
    from fastapi import APIRouter, Query
    router = APIRouter()

    @router.get("/api/monitor/model-calls/overview")
    async def mcl_overview(window_days: int = Query(default=7, ge=1, le=90)):
        """全 agent 模型用量总览（只读；设置页报表块消费）。"""
        return overview(window_days)

    @router.get("/api/monitor/model-calls")
    async def mcl_list(limit: int = Query(default=50, le=500), scene: str = ""):
        """最近 N 条全 agent 调用明细（admin 排查；数据源 = CCR usage_events，只读）。
        `scene` 沿用旧参数名，实际按 client 过滤（保持端点契约兼容）。"""
        data = usage_ledger.recent(limit=limit, client=scene or None)
        if not data.get("ok"):
            return {"rows": [], "count": 0, "source": "ccr_usage_events",
                    "note": str(data.get("error") or "unknown")}
        return {"rows": data["rows"], "count": data["count"],
                "source": "ccr_usage_events"}

    return router
