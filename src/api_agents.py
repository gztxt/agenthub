"""领域路由 · Agent 与直连对话（C2：从 `main.py` 抽出）。

只依赖 adapter 层与 db；共享状态（`discovery`）走 `appstate` —— 不再是 main 的全局。
"""
import asyncio
import json
import re
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import appstate
import db
import modelcfg
import profiles as profiles_mod
import vitals as vitals_mod
from config import config
from registry import get_adapter
from sources import detect_project

router = APIRouter()


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


class ChatRequest(BaseModel):
    agent_id: Optional[str] = None  # 以路径参数为准（v0.1 遗留必填校验是 bug）
    message: str
    session_id: Optional[str] = None
    model: Optional[str] = None
    cwd: Optional[str] = None


class AgentRegisterRequest(BaseModel):
    dir: str
    name: Optional[str] = None
    port: Optional[int] = None
    command: Optional[str] = None
    args: Optional[list] = None


@router.get("/api/agents")
async def list_agents():
    if appstate.discovery is None:
        raise HTTPException(status_code=503, detail="Discovery not initialized")
    agents = await appstate.discovery.discover_all()
    return {"agents": [a.to_dict() for a in agents], "count": len(agents)}


@router.get("/api/agents/{agent_id}")
async def get_agent(agent_id: str):
    if appstate.discovery is None:
        raise HTTPException(503, "Discovery not initialized")
    agent = appstate.discovery.get_agent(agent_id)
    if not agent:
        raise HTTPException(404, f"Agent {agent_id} not found")
    return agent.to_dict()


@router.post("/api/agents/{agent_id}/verify")
async def verify_agent(agent_id: str):
    """L4 体检：跑一次真实一次性请求。要耗 token 且慢（冷启动可达 60s），
    所以只给显式动作触发，不进自动周期。"""
    p = profiles_mod.get_profile(agent_id)
    if not p:
        raise HTTPException(404, f"未知 agent: {agent_id}")
    if not p.get("verify_argv"):
        raise HTTPException(400, {"error": "该 Agent 未声明 verify_argv（无法做真实应答实测）",
                                 "shape": (vitals_mod.collect(p)).get("agent_shape")})
    rec = await asyncio.to_thread(vitals_mod.vitals.verify, p)
    ev = rec.get("evidence", {})
    out = {k: rec.get(k) for k in ("verdict", "source", "rule_verdict", "confidence",
                                   "menu_noul", "present_noul", "roundtrip_noul",
                                   "block_noul", "jev_error", "rt_state", "rt_flaky")}
    # 应答态与生死判定分两栏回：前端拿 rt_state 说明模型层，拿 verdict 决定颜色
    out["evidence"] = {kk: ev.get(kk) for kk in
                       ("agent_shape", "resolved_path", "version_rc", "run_rc", "run_ok",
                        "run_output", "run_note", "run_model", "run_evidence",
                        "evidence_sha", "endpoint_serving")}
    return out


@router.post("/api/agents/detect")
async def detect(body: AgentRegisterRequest):
    """目录 → 自动识别类型/启动命令/端口（Agent_Manager 添加 Agent 的第一步）"""
    return detect_project(body.dir)


@router.post("/api/agents")
async def register_agent(body: AgentRegisterRequest):
    """注册自定义 Agent（识别 + 入库；进程管理交由原守护，hub 只做视图与对话）"""
    info = detect_project(body.dir)
    if info["type"] == "unknown" and not body.command:
        raise HTTPException(400, {"error": "无法识别项目类型且未提供 command", "detected": info})
    now = _now()
    agent_id = re.sub(r"[^a-z0-9_-]", "-", (body.name or Path(body.dir).name).lower())
    port = body.port or info.get("port")
    endpoint = f"http://127.0.0.1:{port}" if port else None
    db.execute(
        """INSERT INTO custom_agents(id,name,type,command,args,working_dir,env,port,endpoint,description,created_at,updated_at)
           VALUES(?,?,?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(id) DO UPDATE SET name=excluded.name,type=excluded.type,
             command=excluded.command,args=excluded.args,working_dir=excluded.working_dir,
             port=excluded.port,endpoint=excluded.endpoint,description=excluded.description,
             updated_at=excluded.updated_at""",
        (agent_id, body.name or Path(body.dir).name, info["type"],
         body.command or (info.get("command") and " ".join([info["command"]] + info["args"])),
         json.dumps(info.get("args") or []), str(Path(body.dir).expanduser()),
         "{}", port, endpoint, info.get("entry") or "", now, now))
    if appstate.discovery:
        appstate.discovery.reload()
    return {"status": "registered", "id": agent_id, "detected": info}


@router.delete("/api/agents/{agent_id}")
async def unregister_agent(agent_id: str):
    n = db.execute("DELETE FROM custom_agents WHERE id=?", (agent_id,))
    if not n:
        raise HTTPException(404, "custom agent not found (内置 Agent 不可删)")
    if appstate.discovery:
        appstate.discovery.reload()
    return {"status": "deleted", "id": agent_id}


# ── 统一对话（Phase 2：真实适配器直连）──────────────────────────────

async def _chat_dispatch(agent_id: str, message: str,
                         session_id: Optional[str] = None,
                         model: Optional[str] = None,
                         cwd: Optional[str] = None,
                         trace_id: Optional[str] = None) -> Dict:
    import time as _time
    t0 = _time.monotonic()
    # v0.13.50：模型取值的唯一入口 —— 请求显式带了就用请求的，没带就回落「设置 → 模型」
    # 落库的那个持久化默认值。以前这条通道完全不知道它的存在，adapter 的
    # default_model 是启动时算出的常量（实测发出去的是 qwen3.8-flash）⇒ 设置页改了
    # 模型，对话页 / 协同子任务 / 定时任务照旧用旧模型。
    model = modelcfg.chat_model(agent_id, model)
    adapter = get_adapter(agent_id)
    if adapter is None:
        rows = db.query("SELECT * FROM custom_agents WHERE id=?", (agent_id,))
        if rows and rows[0]["port"]:
            from adapters.openai_compat import OpenAICompatAdapter
            adapter = OpenAICompatAdapter(config, agent_id,
                                          base_url=f"http://127.0.0.1:{rows[0]['port']}",
                                          default_model=model or "")
        if adapter is None:
            return {"success": False, "error": f"agent {agent_id} 无可用适配器",
                    "hint": "CLI/TUI Agent 请通过原生界面访问"}
    # 取历史
    history = []
    if session_id:
        hist = db.query("SELECT role,content FROM chat_messages WHERE session_id=? "
                        "AND role IN ('user','assistant') ORDER BY id DESC LIMIT 20",
                        (session_id,))
        history = [{"role": r["role"], "content": r["content"]} for r in reversed(hist)]
    result = await adapter.chat(message, session_id=session_id, model=model,
                                history=history, cwd=cwd)
    # S2 画像埋点：append-only，成功率/耗时统计源
    dur = int((_time.monotonic() - t0) * 1000)
    db.log_profile_event("hub_chat", agent_id,
                         "success" if result.get("success") else "fail", dur,
                         trace_id=trace_id,
                         detail={"session_id": session_id,
                                 "usage": result.get("usage"),
                                 "error": (result.get("error") or "")[:200] or None})
    # 会话落盘
    if session_id:
        now = _now()
        db.execute("INSERT OR IGNORE INTO chat_sessions(id,agent_id,title,created_at,updated_at) "
                   "VALUES(?,?,?,?,?)",
                   (session_id, agent_id, message[:60], now, now))
        db.execute("INSERT INTO chat_messages(session_id,role,content,created_at) VALUES(?,?,?,?)",
                   (session_id, "user", message, now))
        reply = result.get("response") or result.get("error") or ""
        db.execute("INSERT INTO chat_messages(session_id,role,content,meta,created_at) VALUES(?,?,?,?,?)",
                   (session_id, "assistant" if result.get("success") else "error", reply,
                    json.dumps(result.get("usage"), ensure_ascii=False) if result.get("usage") else None, now))
        db.execute("UPDATE chat_sessions SET updated_at=? WHERE id=?", (now, session_id))
    return result


@router.post("/api/agents/{agent_id}/chat")
async def chat(agent_id: str, request: Request, req: ChatRequest):
    session_id = req.session_id or uuid.uuid4().hex[:12]
    trace_id = request.headers.get("x-trace-id")
    # P0-1 回归（实测取证）：v0.10.0 commit 2cf96fa 把 tools/repair_mode 从 ChatRequest
    # 字段表里删了，但调用点仍写 req.tools ⇒ 本端点从 09-20 起每请求必 500
    # （AttributeError），而 /health 全程 200、vitals 全绿 —— 直连对话框静默不可用三天。
    # 这两个参数在 v0.10.0 移除 hub-self 工具环后已无实体，**别再往回加**。
    # 钉死它的测：tests/test_pydantic_attr_drift.py（AST 静态取证，不导 main）
    result = await _chat_dispatch(agent_id, req.message, session_id, req.model,
                                  cwd=req.cwd, trace_id=trace_id)
    return {"agent_id": agent_id, "session_id": session_id,
            "message": req.message, "timestamp": _now(), **result}


@router.post("/api/agents/{agent_id}/chat/stream")
async def chat_stream(agent_id: str, request: ChatRequest):
    adapter = get_adapter(agent_id)
    if adapter is None:
        raise HTTPException(404, f"agent {agent_id} 不支持流式对话")
    session_id = request.session_id or uuid.uuid4().hex[:12]

    async def gen():
        yield f"data: {json.dumps({'session_id': session_id})}\n\n"
        async for chunk in adapter.chat_stream(request.message, session_id=session_id,
                                               model=modelcfg.chat_model(agent_id, request.model),
                                               cwd=request.cwd):
            yield chunk
    return StreamingResponse(gen(), media_type="text/event-stream")