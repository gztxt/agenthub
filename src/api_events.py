"""P3 v1 事件出口：SSE 流 + interrupt（不碰 pty 终端链路）。

- POST /api/agents/{agent_id}/chat/events：把适配器的 chat_events 原样转成 SSE，
  每条 data: {envelope}；事件类型在 JSON 的 type 字段里（不额外写 event: 行）。
- POST /api/agents/{agent_id}/interrupt：终止该 agent 当前事件运行的进程组，
  语义 200（已发中断）/ 404（没有活动运行）/ 409（有运行但 provider 不支持中断）。
- 鉴权：沿用 main.py 的全局 write_gate（POST 一律走它），本模块不另造闸门。

设计边界（docs/P3-EVENT-CONTRACT-DESIGN-20261109.md §5）：
  · 不把 JSON 事件写进 term 字节流，不解析 ANSI；
  · 不新建事件溯源表 / outbox；会话落盘仍用 chat_sessions/chat_messages；
  · 不实现 steer / fork / rollback（steer 未支持时只能回 unsupported_steer）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import uuid
from datetime import datetime, timezone
from typing import Optional

from fastapi import APIRouter, HTTPException
from fastapi.responses import JSONResponse, StreamingResponse
from pydantic import BaseModel

import db
import events
import modelcfg
from config import config
from registry import get_adapter

log = logging.getLogger(__name__)

router = APIRouter(tags=["events"])


class EventsChatRequest(BaseModel):
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    cwd: Optional[str] = None
    turn_id: Optional[str] = None
    run_id: Optional[str] = None
    timeout_s: Optional[float] = None


class InterruptRequest(BaseModel):
    session_id: Optional[str] = None
    run_id: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _resolve_adapter(agent_id: str):
    """与 api_agents._chat_dispatch 同口径：注册表优先，custom_agents 端口兜底。"""
    adapter = get_adapter(agent_id)
    if adapter is not None:
        return adapter
    try:
        rows = db.query("SELECT * FROM custom_agents WHERE id=?", (agent_id,))
    except Exception:  # noqa: BLE001
        rows = []
    if rows and rows[0]["port"]:
        from adapters.openai_compat import OpenAICompatAdapter
        return OpenAICompatAdapter(config, agent_id,
                                   base_url="http://127.0.0.1:%s" % rows[0]["port"],
                                   default_model=modelcfg.chat_model(agent_id, None) or "")
    return None


def _history(session_id: Optional[str]) -> list:
    if not session_id:
        return []
    try:
        hist = db.query("SELECT role,content FROM chat_messages WHERE session_id=? "
                        "AND role IN ('user','assistant') ORDER BY id DESC LIMIT 20",
                        (session_id,))
        return [{"role": r["role"], "content": r["content"]} for r in reversed(hist)]
    except Exception:  # noqa: BLE001 —— 历史只是上下文，取不到不许打断本轮
        return []


def _persist(session_id: str, agent_id: str, message: str,
             reply: str, ok: bool, usage: Optional[dict] = None) -> None:
    """会话落盘（append-only，失败不得带倒事件流）。"""
    if not session_id:
        return
    try:
        now = _now()
        db.execute("INSERT OR IGNORE INTO chat_sessions(id,agent_id,title,created_at,updated_at) "
                   "VALUES(?,?,?,?,?)",
                   (session_id, agent_id, message[:60], now, now))
        db.execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                   (session_id, "user", message, now))
        db.execute("INSERT INTO chat_messages(session_id,role,content,meta,created_at) "
                   "VALUES(?,?,?,?,?)",
                   (session_id, "assistant" if ok else "error", reply,
                    json.dumps(usage, ensure_ascii=False) if usage else None, now))
        db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id))
    except Exception as e:  # noqa: BLE001
        log.warning("[events] 会话落盘失败 sid=%s: %s", session_id, e)


@router.post("/api/agents/{agent_id}/chat/events")
async def chat_events(agent_id: str, req: EventsChatRequest):
    adapter = _resolve_adapter(agent_id)
    if adapter is None:
        raise HTTPException(404, "agent %s 无可用适配器（事件流需要 chat 适配器）" % agent_id)
    if not hasattr(adapter, "chat_events"):
        raise HTTPException(501, "agent %s 的适配器未实现 chat_events" % agent_id)

    sid = req.session_id or uuid.uuid4().hex[:12]
    model = modelcfg.chat_model(agent_id, req.model)
    history = _history(sid)
    run_id = req.run_id or uuid.uuid4().hex[:12]

    async def gen():
        parts: list = []
        usage: Optional[dict] = None
        ok = False
        last_seq = 0
        kwargs = {"session_id": sid, "model": model, "history": history,
                  "cwd": req.cwd, "turn_id": req.turn_id, "run_id": run_id}
        if req.timeout_s is not None:
            kwargs["timeout_s"] = req.timeout_s
        try:
            async for ev in adapter.chat_events(req.message, **kwargs):
                last_seq = int(ev.get("seq") or last_seq)
                if ev.get("type") == "message_delta":
                    parts.append(ev.get("text") or "")
                elif ev.get("type") == "usage":
                    usage = {k: v for k, v in ev.items() if k not in events.ENVELOPE_KEYS}
                elif ev.get("type") == "turn_completed":
                    ok = bool(ev.get("success"))
                yield events.to_sse(ev)
        except asyncio.CancelledError:
            raise
        except Exception as e:  # noqa: BLE001 —— 事件流必须有明确终态，不许裸断
            em = events.EventEmitter(sid, agent_id, req.turn_id)
            em.seq = last_seq
            yield events.to_sse(em.error("provider_error",
                                         "%s: %s" % (type(e).__name__, str(e)[:300])))
            yield events.to_sse(em.emit("turn_completed", success=False,
                                        reason="provider_error"))
        finally:
            _persist(sid, agent_id, req.message, "".join(parts), ok, usage)

    return StreamingResponse(gen(), media_type="text/event-stream",
                             headers={"Cache-Control": "no-cache",
                                      "X-Accel-Buffering": "no"})


@router.post("/api/agents/{agent_id}/interrupt")
async def interrupt(agent_id: str, req: InterruptRequest):
    run = events.find_run(agent_id, req.session_id, req.run_id)
    if run is None:
        raise HTTPException(404, {"error": "no_active_run", "agent_id": agent_id,
                                  "session_id": req.session_id,
                                  "run_id": req.run_id})
    cb = run.get("interrupt")
    if not callable(cb):
        # 有运行但 provider 不支持中断：显式回 reason，不静默丢弃（§3.1）。
        return JSONResponse(status_code=409, content={
            "ok": False, "agent_id": agent_id, "run_id": run.get("run_id"),
            "session_id": run.get("sid"),
            "error": {"reason": "unsupported_steer",
                      "message": "该 provider 的事件运行不支持中断"}})
    run["interrupted"] = True
    try:
        cb()
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, {"error": "interrupt_failed",
                                  "detail": "%s: %s" % (type(e).__name__, str(e)[:200])})
    return {"ok": True, "status": "interrupted", "agent_id": agent_id,
            "run_id": run.get("run_id"), "session_id": run.get("sid")}
