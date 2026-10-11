"""领域路由 · 会话（C2：从 `main.py` 抽出）。

只依赖 db 与导出/鉴权的纯函数层；无共享状态。
"""
from datetime import datetime, timezone
from typing import Optional

import aiohttp
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import Response
from pydantic import BaseModel

import db
import export_batch                   # 导出取消息的批量 IN（L0 可测的纯函数层）
import sessions_export as export_mod
import writeauth                    # 导出端点按写端点同等鉴权（复用 decide 的 fail-closed）
from config import config

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


@router.get("/api/sessions")
async def list_sessions(agent_id: Optional[str] = None, limit: int = 20):
    sessions = []
    sql = ("SELECT s.*, (SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) "
           "AS messages FROM chat_sessions s")
    params: list = []
    if agent_id:
        sql += " WHERE s.agent_id=?"
        params.append(agent_id)
    sql += " ORDER BY s.updated_at DESC LIMIT ?"
    params.append(limit)
    sessions = db.query(sql, tuple(params))
    # 合并 hub 外部会话（pi）
    if not agent_id or agent_id == "pi":
        pi_sessions = await _get_pi_sessions(max(0, limit - len(sessions)))
        sessions.extend([{"agent": "pi", "id": s.get("id"), **s} for s in pi_sessions])
    return {"sessions": sessions[:limit], "count": len(sessions)}


@router.get("/api/sessions/{session_id}/messages")
async def session_messages(session_id: str, limit: int = 100):
    return {"messages": db.query(
        "SELECT role,content,meta,created_at FROM chat_messages "
        "WHERE session_id=? ORDER BY id ASC LIMIT ?", (session_id, limit))}


@router.get("/api/sessions/export")
async def export_sessions(request: Request, format: str = "json", agent_id: Optional[str] = None,
                          limit: int = 1000, with_messages: int = 1, redact: int = 1):
    """批量导出 hub 自己的会话（0924 方案档 §三「会话导出 P2.5」）。

    三个刻意的设计决定：
      1) **按写端点同等鉴权**：服务绑 0.0.0.0:3102，批量导出正文是数据外流动作，影响面比
         单条 `/messages` 大一个量级。复用 `writeauth.decide`（fail-closed：服务端没配口令
         ⇒ 503 而不是放行），与 09-23「31 个写端点不设防」的收口同一口径。
      2) **默认脱敏**（`redact=1`）：本工作区三次被凭据外流打过（备份镜像 82 个活凭据文件、
         `wiki/log.md` 历史含 CCR web token、外发净仓被闸门拦下 3 个抄了真 token 的文档）。
         导出件正是最容易被顺手 commit/转发的形态；命中数在 meta 里如实回报，**不静默改数据**，
         要原始字节须显式 `redact=0`。
      3) **不导出外部 CLI 的历史会话**（claude/jcode/codex/opencode/grok/hermes 的 session store）：
         那是别的工具链的私有存档，批量外流属另一层隐私裁定，须用户点名；本端点只覆盖
         hub 自己库里的 `chat_sessions` / `chat_messages`。
    """
    verdict, reason = writeauth.decide(
        "POST", request.url.path,                       # 强制按写方法判：导出=数据外流
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        print(f"[export] 拒绝 {verdict}：{request.url.path} "
              f"来源={request.client.host if request.client else '?'} —— {reason}", flush=True)
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401, detail=reason)

    fmt = "csv" if str(format).lower() == "csv" else "json"
    n_lim = max(1, min(int(limit or 1000), 5000))       # 上限防一次性拖库打爆内存
    sql = ("SELECT s.id, s.agent_id, s.title, s.created_at, s.updated_at, "
           "(SELECT COUNT(*) FROM chat_messages m WHERE m.session_id=s.id) AS messages "
           "FROM chat_sessions s")
    params: list = []
    if agent_id:
        sql += " WHERE s.agent_id=?"
        params.append(agent_id)
    sql += " ORDER BY s.updated_at DESC LIMIT ?"
    params.append(n_lim)
    rows = [dict(r) for r in db.query(sql, tuple(params))]

    meta = {"agent_id": agent_id or "*", "limit": n_lim, "with_messages": bool(with_messages)}
    # 2026-10-05：消息正文**一次 IN 查完**（旧实现每会话一次查询，上限 5000 ⇒ 最坏
    # 5001 次，且每次过 db 的全局锁，把所有其他 DB 使用者一起串行化）。
    # 判据与分块逻辑在 src/export_batch.py（纯函数，L0 可测 —— L0 禁 import src.main）。
    if with_messages and rows:
        msgs_by_sid = export_batch.fetch_messages(db.query, [r.get("id") for r in rows])
    else:
        msgs_by_sid = {}
    if fmt == "csv" and with_messages:
        # CSV 是扁平表 ⇒ 导出正文时以「一行一条消息」呈现（表头恒定，下游可断言）
        mrows: list = []
        for r in rows:
            for m in msgs_by_sid.get(r.get("id")) or []:
                mrows.append({"session_id": r.get("id"), "role": m.get("role"),
                              "created_at": m.get("created_at"), "content": m.get("content")})
        body, ctype, fname, meta = export_mod.render(
            mrows, export_mod.MESSAGE_COLUMNS, "csv", "messages", meta=meta, redact=bool(redact))
    else:
        if with_messages and fmt == "json":
            for r in rows:
                r["transcript"] = [
                    {"role": m.get("role"), "created_at": m.get("created_at"),
                     "content": m.get("content")}
                    for m in msgs_by_sid.get(r.get("id")) or []]
        body, ctype, fname, meta = export_mod.render(
            rows, export_mod.SESSION_COLUMNS, fmt, "sessions", meta=meta, redact=bool(redact))

    print(f"[export] {meta.get('kind')} fmt={fmt} rows={meta.get('count')} "
          f"redacted={'yes' if redact else 'no'} hits={meta.get('redacted_hits')} "
          f"来源={request.client.host if request.client else '?'}", flush=True)
    return Response(content=body, media_type=ctype,
                    headers={"Content-Disposition": f'attachment; filename="{fname}"',
                             "X-Export-Count": str(meta.get("count", 0)),
                             "X-Export-Redacted-Hits": str(meta.get("redacted_hits", 0))})


class SessionPatch(BaseModel):
    title: Optional[str] = None


@router.patch("/api/sessions/{session_id}")
async def rename_session(session_id: str, req: SessionPatch):
    if req.title is None:
        raise HTTPException(400, "no fields to update")
    n = db.execute("UPDATE chat_sessions SET title=?, updated_at=? WHERE id=?",
                   (req.title.strip()[:120], _now(), session_id))
    if not n:
        raise HTTPException(404, "session not found")
    return {"status": "renamed", "id": session_id, "title": req.title}


@router.delete("/api/sessions/{session_id}")
async def delete_session(session_id: str):
    n1 = db.execute("DELETE FROM chat_messages WHERE session_id=?", (session_id,))
    n2 = db.execute("DELETE FROM chat_sessions WHERE id=?", (session_id,))
    if not n2:
        raise HTTPException(404, "session not found")
    return {"status": "deleted", "id": session_id, "messages": n1}


async def _get_pi_sessions(limit: int) -> list:
    if limit <= 0:
        return []
    try:
        async with aiohttp.ClientSession() as session:
            async with session.get(
                f"{config.pi_url}/api/sessions",
                timeout=aiohttp.ClientTimeout(total=5)
            ) as resp:
                if resp.status == 200:
                    data = await resp.json()
                    return data.get("sessions", [])[:limit]
    except Exception:  # noqa: BLE001
        pass
    return []