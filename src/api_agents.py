"""领域路由 · Agent 与直连对话（C2：从 `main.py` 抽出）。

只依赖 adapter 层与 db；共享状态（`discovery`）走 `appstate` —— 不再是 main 的全局。
"""
import asyncio
import json
import os
import re
import shutil
import subprocess
import threading
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional
from urllib.parse import urlsplit

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import StreamingResponse
from pydantic import BaseModel

import appstate
import capabilities
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
    # self_addrs：本机自有地址清单，供前端把卡片地址换成**当前访问主机**
    # （局域网 / Tailscale / MagicDNS 各自命中自己的地址）。见 profiles.own_addrs。
    return {"agents": [a.to_dict() for a in agents], "count": len(agents),
            "self_addrs": profiles_mod.own_addrs()}


# ⚠ 注册顺序：`/api/agents/releases` 必须在 `{agent_id}` **之前**。
# 实测（Starlette TestClient）：放在后面时它被 `get_agent("releases")` 吃掉，
# 新端点**永远打不到**且不报错 —— 属于「加了路由却静默不生效」。
# ── P2 A1：Agent Release manifest（只读聚合）─────────────────────────────
# 为什么放在本文件而不是新建 router：`main.py` 的 include_router 列表是多会话
# 共写热点（C3），能不碰就不碰。`api_agents` 本来就是「Agent 视图」的归属处。

@router.get("/api/agents/releases")
async def list_releases(agent_id: Optional[str] = None):
    """列出各 agent 的 release manifest（`agent_id` 给定时只算那一个）。

    **async def 里同步读盘/SQLite**：单 agent 实测 0.3ms、23 个 10ms（热）/ 350ms（冷，
    几乎全在 `skill_assets.ensure_schema` 与 `config` 读盘），不值得为它绕 to_thread。

    路由此前**实测打不到**：放在 `{agent_id}` 之后时，Starlette 先用
    `/api/agents/{agent_id}` 匹配，`get_agent("releases")` 接走请求（未初始化时
    回 503「Discovery not initialized」），新端点静默失效。现已前移。
    """
    import release as release_mod
    try:
        if agent_id:
            return release_mod.build_manifest(agent_id)
        return release_mod.manifests_for()
    except Exception as e:  # noqa: BLE001 —— 聚合失败必须显式报，不得空回包
        raise HTTPException(500, {"error": "release manifest 生成失败",
                                  "reason": "%s: %s" % (type(e).__name__, e)})


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
                                   "block_noul", "judge_error", "rt_state", "rt_flaky")}
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


# ── 原生界面按需拉起（v0.13.152）────────────────────────────────────
# 2026-10-25 用户报障「要使用原生界面 都没有拉起的地方」：2026-10-24 全 agent 改
# 按需拉起后，各卡的原生界面常态是停着的；v0.13.151 把按钮复还了，但点开只有
# 「目标界面未响应」覆盖层，用户只能自己去终端敲 systemctl。
#
# 安全边界：执行目标**只能**取自 `profiles.UI_START`（服务端常量，按 agent id 白名单），
# 请求体不参与命令构造 ⇒ 本端点不提供任意命令执行。与 /api/term/sessions 的权限同级
# （同一个写闸门，需要 x-hub-token / ?token=）。
def _ui_targets(agent_id: str, spec: Optional[dict], p: dict) -> List[tuple]:
    """探活目标 `[(host, port), …]`，按可信度排序、去重保序。

    为什么不能只看回环：**dsh 拒绝通配地址**（只能 `--host 192.168.5.102`），
    它的 3080 **不监听 127.0.0.1** ⇒ 回环探活恒失败。2026-10-25 实测后果：
    服务 `active`、iframe 已 200、卡片地址带 token 全对，但 `/ui-status` 恒回
    `ready=false` ⇒ 前端覆盖层一直盖着「原生界面未运行，正在拉起…」，
    用户看到的就是「dsh 原生界面起不来了」（v0.13.153 引入 ui-status 时的回归，
    此前用的是浏览器侧 `_embedAlive`，能连通所以一直是好的）。

    配方里的 `probe` 优先于 `url`：qwenpaw 的 `url` 是 hub 进程内代理 :3103
    （hub 活着它就永远 listening），拿它探活会把停着的后端显示成活的，
    真后端在 `probe`（:8088）里。
    """
    # `ui_start_for()` 只回配方里的 `start` 子字典（url/probe 不在里面）⇒ 地址必须
    # 另取 `capabilities.recipe_for()`；否则 qwenpaw 的 `probe: 8088` 永远读不到，
    # 会用 hub 代理 :3103 判活 —— 后端停着也显示「活的」。
    recipe = capabilities.recipe_for(agent_id) or {}
    port = recipe.get("port") or (spec or {}).get("port") or p.get("port")
    urls: List[str] = []
    if recipe.get("probe"):
        # 有 probe ⇒ **只探它**，不掺 url/画像地址：那是 hub 的进程内代理，
        # hub 活着它就永远 listening，掺进来等于把 `probe` 作废。
        urls.append(recipe["probe"])
    else:
        for src in (recipe.get("url"), (spec or {}).get("url"), p.get("ui")):
            if isinstance(src, dict):
                src = src.get("url")
            if isinstance(src, str) and src:
                urls.append(src)
    out: List[tuple] = []
    for u in urls:
        try:
            sp = urlsplit(u)
            h, pt = sp.hostname, sp.port or port
        except ValueError:
            continue
        if h and pt:
            out.append((h, int(pt)))
            # 声明的地址可能只覆盖一个协议族（qwenpaw 后端只监听 [::]），
            # 同一端口补另一族，避免「地址写 IPv4、后端只听 IPv6」被误判成未运行。
            for alt in (("::1", "127.0.0.1") if ":" in h else ("::1",)):
                if (alt, int(pt)) not in out:
                    out.append((alt, int(pt)))
    if port and not recipe.get("probe"):
        out.append(("127.0.0.1", int(port)))
        out.append(("::1", int(port)))
    seen = set()
    uniq = []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def _ui_port_open(port, host: Optional[str] = None) -> bool:
    """双栈探活，与 `discovery._sync_check_port` 同口径。

    **必须双栈**：qwenpaw 2.2.1 实测只监听 `[::]:8088`，`127.0.0.1:8088` 拒连不等于没起
    （2026-09-21 判例）。单栈判活会把它永远报成「未运行」——2026-10-25 实测：
    服务已 active、UI 经 hub 代理 :3103 返 200，单栈探活仍判死并回「已启动，但界面仍未响应」。
    `host` 给了就只探它（端口已按画像绑定地址算好），否则回环双栈。
    """
    if not port:
        return False
    if host:
        return vitals_mod.port_open(port, host)
    return vitals_mod.port_open(port) or vitals_mod.port_open(port, "::1")


def _ui_ready(agent_id: str, spec: Optional[dict], p: dict) -> tuple:
    """(就绪?, 命中的 host, port)。任一目标通即就绪。"""
    port = (spec or {}).get("port") or p.get("port")
    for host, pt in _ui_targets(agent_id, spec, p):
        if _ui_port_open(pt, host):
            return True, host, pt
    return False, None, port


def _ui_spawn_env() -> Dict[str, str]:
    """原生界面子进程的 env。

    2026-10-29 收口：实体实现移到 `profiles.cli_env()` —— 派发侧的 CLI 适配器
    （`adapters/cli_agent.py`）需要**同一份** PATH，两处各写一份必然漂移。
    """
    return profiles_mod.cli_env()


@router.get("/api/agents/{agent_id}/ui-status")
async def agent_ui_status(agent_id: str):
    """原生界面就绪态（**服务端**判定）。

    为什么不让浏览器直接探 ui/health 地址：探针在客户端跑，`127.0.0.1` 指客户端；
    换成 lanUrl 后又只能打到某一张网卡。qwenpaw 这种 IPv6-only 后端在 IPv4 客户端上
    永远探不通，而它的 UI 走的是 hub 注入代理 :3103 —— 浏览器侧探活得到的是
    「假死」（2026-10-25 实测）。就绪与否由 hub 在本机双栈探一次，客户端只读结论。
    """
    p = profiles_mod.get_profile(agent_id)
    if not p:
        raise HTTPException(404, {"error": "no_agent", "agent_id": agent_id})
    spec = profiles_mod.ui_start_for(agent_id)
    ready, host, port = _ui_ready(agent_id, spec, p)
    # ★ dsh 的「就绪」不能只看端口（2026-10-30 用户报障 dsh 嵌页 401）：
    # - 端口在 listening 那一刻横幅**未必**已打印（实测 4~8s 的窗口），此时嵌进去
    #   只能拿到死 token / 裸地址 ⇒ 帧内是 "dsh web authentication required"。
    # - 所以 token 还属于**已退出进程**（state=pending）时，一律报未就绪，让前端
    #   继续停在「正在拉起…」覆盖层上；横幅一出来自然转 ready 并带着新 token 重载。
    # - state=unknown（journal 被 vacuum / systemctl 不可用）**不拦**：那是明确降级，
    #   拦了会把一个能用的界面永久显示成死的。
    token_state = None
    if agent_id == "dsh":
        token_state = profiles_mod.dsh_token_state()
        if token_state == "pending":
            ready = False
    return {"agent_id": agent_id, "ready": ready, "port": port, "host": host,
            "token_state": token_state}


@router.post("/api/agents/{agent_id}/ui-start")
async def start_agent_ui(agent_id: str):
    p = profiles_mod.get_profile(agent_id)
    spec = profiles_mod.ui_start_for(agent_id)
    if not p or not spec:
        raise HTTPException(404, {"error": "no_ui_start", "agent_id": agent_id,
                                  "hint": "该 agent 未登记原生界面启动方式（TUI-only 或卡不存在）"})
    ready, _host, port = _ui_ready(agent_id, spec, p)
    if ready:
        return {"status": "already_running", "agent_id": agent_id, "port": port}
    unit = spec.get("unit")
    if unit:
        try:
            r = await asyncio.to_thread(
                subprocess.run, ["systemctl", "--user", "start", unit],
                capture_output=True, text=True, timeout=25)
        except Exception as e:  # noqa: BLE001
            raise HTTPException(500, {"error": "unit_start_spawn_failed", "unit": unit,
                                      "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
        if r.returncode != 0:
            raise HTTPException(502, {"error": "unit_start_failed", "unit": unit,
                                      "detail": ((r.stderr or "") + (r.stdout or ""))[-400:]})
        # 进程刚起来 ⇒ 进程级 token 已换新，旧缓存必须立刻作废（否则嵌页 401 白屏）
        profiles_mod.invalidate_ui_url(agent_id)
        return {"status": "started", "kind": "unit", "unit": unit, "agent_id": agent_id}

    cmd = spec.get("cmd")
    if not cmd:
        raise HTTPException(500, {"error": "bad_ui_start_spec", "agent_id": agent_id})
    # ★ hub 由 systemd --user 拉起，PATH 只有 /usr/{local/,}{s,}bin:/bin（实测
    #   /proc/<hub>/environ）⇒ 装在 ~/.npm-global/bin、~/.local/bin、nvm 里的工具
    #   在服务进程里**找不到**，Popen 直接 FileNotFoundError。
    #
    #   2026-10-25 实测补第二处：reasonix 只存在于 `~/.nvm/versions/node/*/bin`，
    #   旧实现只补 npm-global/local ⇒ 线上点「启动原生界面」报 binary_not_found，
    #   而影子实例（从交互 shell 起、PATH 自带 nvm）是绿的 —— 典型的「开发环境假绿」。
    env = _ui_spawn_env()
    exe = profiles_mod.which(cmd[0]) or shutil.which(cmd[0], path=env["PATH"])
    if not exe:
        raise HTTPException(500, {"error": "binary_not_found", "agent_id": agent_id,
                                  "binary": cmd[0], "path": env["PATH"][-240:]})
    cmd = [exe] + list(cmd[1:])
    log_dir = Path(config.log_dir) / "ui-start"
    try:
        log_dir.mkdir(parents=True, exist_ok=True)
        logf = open(log_dir / (agent_id + ".log"), "ab")   # noqa: SIM115  常驻子进程持有
        proc = subprocess.Popen(
            list(cmd), cwd=spec.get("cwd") or None, env=env,
            stdin=subprocess.DEVNULL, stdout=logf, stderr=subprocess.STDOUT,
            start_new_session=True, close_fds=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, {"error": "spawn_failed", "agent_id": agent_id,
                                  "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
    print(f"[ui-start] {agent_id} pid={proc.pid} cmd={' '.join(cmd)}", flush=True)
    # 回收僵尸：hub 自己不退，子进程退出后没人 wait ⇒ 进程表里留一个 [defunct]
    # （2026-10-25 实测 opencode 停掉后 808366 变 Zs，父进程仍是 hub）。线程里 wait
    # 顺便关掉日志句柄；daemon 线程不拖住 hub 退出。
    def _reap() -> None:
        try:
            proc.wait()
        finally:
            try:
                logf.close()
            except Exception:  # noqa: BLE001
                pass
    threading.Thread(target=_reap, daemon=True, name=f"ui-start-reap-{agent_id}").start()
    profiles_mod.invalidate_ui_url(agent_id)   # 同上：新进程 = 新 token
    return {"status": "started", "kind": "cmd", "pid": proc.pid, "agent_id": agent_id}


# ── 统一对话（Phase 2：真实适配器直连）──────────────────────────────

async def _chat_dispatch(agent_id: str, message: str,
                         session_id: Optional[str] = None,
                         model: Optional[str] = None,
                         cwd: Optional[str] = None,
                         trace_id: Optional[str] = None,
                         timeout_s: Optional[float] = None) -> Dict:
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
                                history=history, cwd=cwd, timeout_s=timeout_s)
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
