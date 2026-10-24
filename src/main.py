"""Agent Hub - 主入口（Agent_Manager 融合版 v0.3.0）

融合自 Zafer-Liu/Agent_Manager (Apache-2.0) 的设计与语义：
- Hook 遥测端点（agent_http.rs → src/hook.py）
- 三层记忆中心（memory 子系统 → src/memory.py）
- 端口管理（ports.rs → src/ports.py，只读，无 kill —— NAS 军规）
- 项目类型自动识别（agent_sources.rs → src/sources.py）
"""
import asyncio
import hmac
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

# 必须在导入 config 前加载 .env
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"[Agent Hub] 已加载 .env: {env_path}")

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import db
import modelcfg
from config import config
from discovery import AgentDiscovery, AgentInfo
from ports import list_listeners, port_in_use
from sources import detect_project
import scanner
from writeauth import write_gate
from registry import build_adapters, get_adapter
import hook as hook_mod
import memory as memory_mod
import memfed as memfed_mod
import kb as kb_mod
import skill as skill_mod
import skill_store as skill_store_mod
import dataset as dataset_mod
import tasks as tasks_mod
import mcpgw as mcpgw_mod
import cronjobs as cronjobs_mod
import term as term_mod
import term_record
import profiles as profiles_mod
import vitals as vitals_mod
import embed_proxy as embed_proxy_mod
import selfattest
import staticguard
import tdai_client
import gwprobe                      # 上游网关（CCR）连通性 + 模型注册清单的缓存式体检
import healthx                      # /health 派生量的纯函数层（L0 不 import src.main，故抽出来）
import memview                     # 内存三口径可观测（本体/托管子进程/cgroup 记账）
import retention                    # 保留期定时清理（term_recordings + profile_events）
import resilience                   # 出站韧性层：退避重试 + 熔断（/health 的 breakers 字段）
import settings_bundle as settings_bundle_mod  # 配置包导出/导入（PT-20260927-15，JSON，不引 YAML）
import approvals as approvals_mod   # P1-2：审批三态（ASK + Preview-Approve-Commit-Verify）
import skill_assets as skill_assets_mod   # P1-5 其余四件：技能资产（owner/version/scope/状态机）
import actionplane as actionplane_mod     # P2 A10：统一 Action Plane（请求/裁决/结果 + 生命周期）
import admission as admission_mod         # P2 C7：外部资产准入六阶段
import sessions_export as export_mod
import export_batch                   # 导出取消息的批量 IN（L0 可测的纯函数层，同 staticguard 范式）
import models_cache                   # 模型清单的成功路径缓存（同上，纯函数层）
import writeauth                    # 导出端点按写端点同等鉴权（复用 decide 的 fail-closed）
import audit as audit_mod           # 资产变更审计的只读查询门面（GET /api/audit/list）
import refscout as refscout_mod     # PT-20261021-04：参考情报（GitHub 借鉴源采集 + 三轴打分）
import intake as intake_mod         # PT-20261022-01：接单前置（四查一简报）
import runlog as runlog_mod         # 运行日志：三中心检索留痕 + GET /api/runlog 查询门面
import cloudcli as cloudcli_mod     # CloudCLI 项目直达：项目清单（直读 auth.db）+ 会话启动代理
import localprojects as localprojects_mod  # 本机项目清单（多根 git 扫描 + cloudcli 合并）
import resources as resources_mod          # 资源监控：Agent 进程资源列表 + 一键结束
import githubprojects as github_mod  # GitHub 远端仓库清单 + 即时克隆（本机项目页的远端半程）
import prefs as prefs_mod           # 应用级偏好 KV（v0.13.36）：两项目页收藏/隐藏落服务端
import modelcfg as modelcfg_mod     # 各 Agent 默认模型统一设置（v0.13.41：设置→模型子菜单）
import ghsettings as ghsettings_mod  # GitHub 地址/key/落点（v0.13.42：设置→GitHub 子菜单）
import hublog as hublog_mod        # 日志中心（v0.13.46：设置→日志子菜单，journald + 操作事件聚合）
import api_dataset as api_dataset_mod  # P0-1：数据集/经验库只读门面（无写端点）

# C2（2026-10-12）：领域路由从本文件抽成独立 module —— main 只留组装职责。
import appstate                     # 组装方写、领域方读的共享可变状态（discovery）
from api_agents import router as agents_router, _chat_dispatch   # 启动期 chat_fn 回调仍要它
from api_sessions import router as sessions_router
from api_models import router as models_router
from api_vitals import router as vitals_router, agent_profs_for_vitals
from api_summarize import router as summarize_router   # 2026-10-23：本机摘要提炼
from api_system import router as system_router   # 系统级：前端「需重启」按钮的自重启端点

print(f"[agenthub] 配置: PORT={config.port}, HOST={config.host}")

# 单一版本源：/health、FastAPI 元数据、启动横幅与页脚都取这里
VERSION = "0.13.151"
#   v0.13.151：**按需拉起下原生界面按钮复还 + 模型设置自动发现**（用户 2026-10-25
#   报两事：「dsh的原生界面按钮也没有了」「模型设置页面也没有自动发现并添加
#   dsh omp 等agent，应该是自动发现并添加」）。
#   ① 按钮消失的根因是 v0.13.149 与 v0.13.149 的按需令**自相矛盾**：前者给
#     agent string-ui 的 embed/open 带 probe_port、按端口活性过滤（「死面板不出
#     按钮」扩展到 agent ui），后者把全部 Web UI agent 改成常态 stopped ⇒ 每个
#     Web UI agent 的原生界面按钮在**正常状态**下被删。改判：agent 入口不再带
#     probe_port（只 panel 类保留），停着的服务由前端 probeEmbed 出「目标界面
#     未响应」覆盖层（含切模式/重试），按钮本身必须在。同因修 dict-ui 注入
#     （cloudcli 原先端口死连原生会话都不注入）。
#   ② defaultModeOf 的 EMBED_FIRST 加 has('embed') 前置：改前它无条件返回
#     'embed'，而 applyChatMode 回落默认时又问一次它 ⇒ embed entry 缺失时两步
#     都落 embed，用户点开的是一张 url 为空的空白嵌页且出不去（同源第二道闸）。
#   ③ 模型设置页（modelcfg）原只列 SPECS+UNSUPPORTED 两张硬编码表 ⇒ 画像里
#     新登记的 agent 在页面上零存在感。改为合并 profiles 注册表自动发现；
#     omp 入 SPECS（argv-only：实测 `omp --help` 有 --model、配置根是 sqlite
#     无明文模型字段；且实测 --model 收 `ccr/<id>` 前缀形状，裸 ID 会被解析成
#     provider=openrouter 报 No API key ⇒ VALUE_PREFIX 补 omp）；dsh 入
#     UNSUPPORTED（实测 `dsh --profile tui --help` 仅 --resume/--agent-preset）。
#   红向自证三破坏点各自精确变红：加回 probe_port / 去掉 has('embed') /
#   去掉注册表合并段。
#   v0.13.150：**补登 QoderWake 长驻缺口**（用户 2026-10-24 报「不是所有 agent
#   都按需拉起，还有几个在长驻」）。实测：`qoderwake __daemon`（845MB）+
#   `qodercli-wake --sdk`（205MB）两进程长驻，QoderWake.service enabled+
#   Restart=always，而本仓画像零登记 ⇒ v0.13.149 的全军整改没覆盖它。
#   本版只改画像层一件事：profiles.py 加 qoder 静态卡（kind=agent，
#   ui :19820 + qodercli 终端入口，desc 写按需启动命令）。
#   宿主侧配套（随用户裁定执行，不在本仓）：QoderWake.service stop+disable。
#   两个实现陷阱：
#   ① 静态卡 id 必须取 `qoder` —— CLI watchlist 已有同 id 动态卡（qodercli
#      已安装），covered 机制靠 id 相同才压得住；test 钉「动态卡不得含 qoder」。
#   ② detect.systemd 单元名大小写敏感（`u in units` 精确匹配）⇒ 必须写
#      `QoderWake` 不能写 `qoderwake`；proc 两条兜底 daemon/SDK 两进程。
# ── v0.13.148 及更早的逐版根因已于 2026-10-24 归档至 ──
# ── CHANGELOG.md（脚本 scripts/extract_changelog.py + scripts/extract_changelog_newfmt.py 逐字搬运）──
# 此处**只保留当前版本**的根因，避免两份会各自漂移的副本。
_bg_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


app = FastAPI(title="Agent Hub", version=VERSION)


@app.exception_handler(RequestValidationError)
async def _intake_validation_hint(request: Request, exc: RequestValidationError):
    """422 原文是 pydantic 的 `{"type": "string_too_short", "min_length": 2}`，
    MCP / 非 MCP 消费方拿到它只知道"参数错了"，不知道"该怎么办"（2026-10-22 实测：
    `hub_intake(task="")` 把 pydantic 错误体原样透给调用方）。

    只给 `/api/intake*` 补一句人话（`ok`/`error`），`detail` 原样保留 ⇒ 既有按
    `detail` 解析的调用方不受影响；其它端点的 422 形状一个字不动（避免面太大）。
    口径与文案在 `intake.validation_body()`（可单测，不必拉起整个 app）。
    """
    content = intake_mod.validation_body(request.url.path, jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content=content)


#: `/api/models` 的成功缓存，按 adapter 分桶（claude / jcode / …）。
#: 模块级 = 跨请求存活，这是缓存的本意；不随 app 重启清空是刻意的
#: （模型清单重启后立刻重取一次即可，不需要持久化）。


def _parse_cors_origins() -> list:
    """CORS_ORIGINS 逗号分隔解析；异常/为空时退化为仅回环来源（不放松默认安全）"""
    # 原默认值写死了三个内网 IP。换个网段（Tailscale 重新分配、或搬到别的 LAN）就静默失效：
    # 页面能开但所有写端点 400 CORS，用户只会看到「按钮点了没反应」。
    # 改为 env 驱动、**默认只给回环**（更严，且本机自用不受影响），跨网访问显式配 CORS_ORIGINS。
    # 绝不放宽到 "*"：allow_credentials=True 时浏览器会直接拒绝通配来源。
    raw = os.getenv("CORS_ORIGINS", "http://127.0.0.1:3102,http://localhost:3102,http://[::1]:3102")
    try:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        for o in origins:
            if not (o.startswith("http://") or o.startswith("https://")):
                raise ValueError(f"invalid origin scheme: {o}")
        if not origins:
            raise ValueError("empty CORS_ORIGINS")
        return origins
    except Exception as e:  # noqa: BLE001
        print(f"[Agent Hub] CORS_ORIGINS 解析失败（{e}），退化为仅回环来源")
        return ["http://127.0.0.1:3102", "http://localhost:3102", "http://[::1]:3102"]


app.add_middleware(CORSMiddleware, allow_origins=_parse_cors_origins(),
                   allow_methods=["*"], allow_headers=["*"], allow_credentials=False)

# ── 非 MCP 写路径按 IP 滑动窗口限流（风格对齐 mcpgw._check_rate）──────
API_RATE_PER_MIN = int(os.getenv("API_RATE_PER_MIN", "60"))
# ⚙设置口令：查看 TERM_TOKEN 等敏感配置时要求提供（为空则设置页不可用）
HUB_PASSCODE = os.getenv("HUB_PASSCODE", "")
_api_rate: Dict[str, deque] = defaultdict(deque)
_RATE_EXCLUDED_PREFIXES = ("/telemetry/events/", "/health", "/mcp")
_RATE_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
#: P0-6：_api_rate 是进程级字典，每个见过的 IP 留一个 deque **永不删除**。
#: 唯一来源是内网/反代，但 IP 会随设备休眠、DHCP 续租、容器重建不断变化 ⇒ 长时间运行
#: 内存单调增长（mcpgw 同型缺陷）。上限按「记住多少个 IP 还在限流窗口内」取，
#: 超出就整条丢弃（该 IP 下次请求会以 0 计数重新开始，等价于放过一次，超了仍会 429）。
_RATE_MAX_IPS = int(os.getenv("API_RATE_MAX_IPS", "4096"))


def _rate_prune(now: float) -> None:
    """回收 _api_rate 里已经过窗口的 IP 条目（每个 IP 至少来过一次才会被记）。"""
    if len(_api_rate) <= _RATE_MAX_IPS:
        return
    cut = now - 60
    stale = [ip for ip, w in _api_rate.items() if not w or w[-1] < cut]
    for ip in stale:
        _api_rate.pop(ip, None)
    # 极端情况：所有 IP 都在窗口内（真实 DDoS 或超大 NAT）⇒ 超额部分按最旧的整条丢，
    # 保证表本身有硬上界，不因来客太多而无限长。
    while len(_api_rate) > _RATE_MAX_IPS:
        oldest = min(_api_rate, key=lambda ip: _api_rate[ip][-1])
        _api_rate.pop(oldest, None)


@app.middleware("http")
async def api_rate_limit(request: Request, call_next):
    # 只拦写方法；GET（前端 30s 轮询）与外部推送/健康检查/MCP（自带限流）不拦
    if request.method in _RATE_WRITE_METHODS:
        path = request.url.path
        if not any(path.startswith(p) for p in _RATE_EXCLUDED_PREFIXES):
            ip = request.client.host if request.client else "unknown"
            _rate_prune(time.monotonic())
            window = _api_rate[ip]
            cut = time.monotonic() - 60
            while window and window[0] < cut:
                window.popleft()
            if len(window) >= API_RATE_PER_MIN:
                print(f"[rate] 429：{ip} 超过 {API_RATE_PER_MIN}/min（{request.method} {path}）")
                return JSONResponse(
                    {"detail": f"API 限流：超过 {API_RATE_PER_MIN}/min"}, status_code=429)
            window.append(time.monotonic())
    return await call_next(request)


# ── P1-7 扩展：写端点鉴权闸门（实测 34 条写路由里 32 条此前不设防，6 条对匿名写回 200）。
#    Starlette 里后注册的中间件在最外层 ⇒ 本闸门先于 api_rate_limit：被拒的请求既不该占限流预算，
#    更不该走到 handler 里产生副作用（rebuild 重写记忆就是这类副作用）。 ──
app.middleware("http")(write_gate)

templates_dir = Path(__file__).parent.parent / "templates"
static_path = Path(__file__).parent.parent / "static"
templates = Jinja2Templates(directory=str(templates_dir))

# v0.5.2.7 自定义静态资源路由（替代原 StaticFiles mount）；v0.12.4 改口径：
# 原来是 no-store ⇒ 浏览器每进一次终端页都要重下 290KB 的 vendor/xterm.js，而且从不压缩。
# 现在 no-cache（每次仍回源校验）+ 自己处理 If-None-Match ⇒ 文件没变只回 304 空响应，
# 文件一改 ETag 就变 ⇒ 拿不到旧 JS（当初写 no-store 就是怕这个，304 同样防得住）。
if static_path.exists():
    @app.get("/static/{file_path:path}")
    async def _static_no_cache(file_path: str, request: Request):
        # 整条策略（判据 + 组装 + 发射）2026-10-12 已收进 src/staticguard.py —— 此前只抽了判据、
        # 组装内联在这里，⇒「静态资源怎么正确地服务」的 interface 横跨两处且组装不可测。
        # 现在路由退化为一次调用；行为逐字节冻结，不变量见 staticguard.serve 的 docstring，
        # golden 矩阵见 tests/test_staticguard_serve.py（L0）与 verify_static_headers_matrix.py（L2）。
        plan = staticguard.serve(
            static_path, file_path,
            (request.query_params.get("v") or "").strip(),
            request.headers.get("accept-encoding") or "",
            request.headers.get("if-none-match") or "",
        )
        if plan.status == 404:
            raise HTTPException(404)
        return staticguard.to_response(plan)
    # 不再 mount StaticFiles；自定义路由接管 /static/

# 子路由（Hook / 记忆 / 指挥官）
app.include_router(hook_mod.router)
app.include_router(memory_mod.router)
app.include_router(memfed_mod.router)
app.include_router(kb_mod.router)
app.include_router(skill_mod.router)
app.include_router(skill_store_mod.router)   # PT-20261020-02：商店搜索/安装（写侧独立模块）
app.include_router(tasks_mod.router)
app.include_router(mcpgw_mod.router)
app.include_router(cronjobs_mod.router)
app.include_router(term_mod.router)
app.include_router(audit_mod.router)
app.include_router(api_dataset_mod.router)   # P0-1：/api/dataset/* + /api/experience/*（只读）
app.include_router(runlog_mod.router)
app.include_router(cloudcli_mod.router)
app.include_router(localprojects_mod.router)
app.include_router(resources_mod.router)
app.include_router(github_mod.router)
app.include_router(prefs_mod.router)
app.include_router(modelcfg_mod.router)
app.include_router(ghsettings_mod.router)
app.include_router(settings_bundle_mod.router)   # PT-20260927-15：/api/settings/export|import
app.include_router(approvals_mod.router)         # P1-2：/api/approvals（ASK 审批队列与决定端）
app.include_router(skill_assets_mod.router)      # P1-5：/api/skill-assets（资产登记与状态机）
app.include_router(actionplane_mod.router)       # P2 A10：/api/actions（行动台账，只读为主）
app.include_router(admission_mod.router)        # P2 C7：/api/admission（外部资产准入六阶段）
app.include_router(hublog_mod.router)
app.include_router(refscout_mod.router)          # PT-20261021-04：/api/ref/*（情报卡）
app.include_router(intake_mod.router)            # PT-20261022-01：/api/intake/task（四查一简报）

# D2：Hub MCP Server —— 把本机事实源以 MCP 暴露给 Hermes 等外部 Agent。
# 端点为 /hub-mcp/mcp（streamable_http_app 自带 /mcp 子路由，故挂在 /hub-mcp 下，避免与 mcpgw 的 /mcp/* REST 冲突）。
# 用 try 包裹：挂载失败不得影响主服务启动。
try:
    import contextlib

    import hubmcp

    # session_manager 是懒创建的：必须先 build_app() 再取。
    _mcp_app = hubmcp.build_app()
    _mcp_sm = hubmcp.server.session_manager

    # Starlette 不会自动运行 mount 子应用的 lifespan，需手动并入主应用 lifespan，
    # 否则报 "Task group is not initialized. Make sure to use run()."
    _orig_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _hub_lifespan(app_):
        async with _mcp_sm.run():
            async with _orig_lifespan(app_):
                yield

    app.router.lifespan_context = _hub_lifespan
    app.mount("/hub-mcp", _mcp_app)
    print("[Agent Hub] Hub MCP server 已挂载：/hub-mcp/mcp（lifespan 已并入）")
except Exception as e:  # noqa: BLE001
    print(f"[Agent Hub] Hub MCP server 挂载失败（主服务不受影响）：{type(e).__name__}: {e}")


@app.get("/health")
async def health():
    """自证端点（P0-2）：不只看活没活，还要能看出「跑的是哪份代码」。

    旧口径只回 {status,service,version,port} ⇒ 版本漂移（进程报 0.13.2 / HEAD 已 0.13.3）
    与「对话端点必 500」这类**静默不可用**全都看不见。
    新增字段全 additive；前端 pollHealth 只读 status，不会被改坏。
    code_stale 只兑情报、不改 status：代码改了没重启不等于服务坏了。
    """
    try:
        db.query("SELECT 1 FROM sqlite_master LIMIT 1")
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_ok = False
        print(f"[health] db 自检失败：{type(e).__name__}: {str(e)[:120]}", flush=True)
    out = {"status": "ok", "service": "agenthub", "version": VERSION, "port": config.port,
           "db_ok": db_ok,
           "term_sessions": term_mod.alive_count(),
           "term_idle_max_s": term_mod.idle_max_s()}
    # 内存三口径（PT-20261024-08 B5）：**本体 / 托管子进程 / cgroup 记账**。
    # 为什么必须分开：2026-10-24 实测同一服务三个口径差 7 倍（判例 agent-knowledge/259）
    # ——进程 RSS 232MB、cgroup 进程合计 753MB、cgroup 记账 1.4~2.0GB（差额是 page cache）。
    # 只报一个数必然误导：按进程口径低估（漏掉托管的 agent CLI 与文件缓存），
    # 只看 cgroup 总量又看不出「是本体大还是被托管的进程大」。
    # ⚠ 三个字段都是**读数**不是常量；用它们做告警判据前必须先量自己的方差
    #   （判例 259：轮间极差 < |两读数之差| 才是「有差异」的前提）。
    # ⚠ 拿不到一律是 null（不是 0）：0 与「测不到」是两件事。
    try:
        out["memory"] = memview.memory_snapshot()
    except Exception as e:  # noqa: BLE001
        out["memory"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    out.update(selfattest.snapshot())
    # 记忆后端体检（P0-6）：权威库在 TDAI，它挂不挂必须从 /health 能看出来。
    # 旧态是「/api/memory/search 永远回 count:0 且无任何错误字段」——全绿而功能层已死。
    # 纯读缓存不起网络（见 tdai_client.backend_status 注释），且**不改 status**：
    # 记忆后端不可用不等于 hub 坏了，同 code_stale 只兑情报的设计意图。
    out["memory_backend"] = tdai_client.backend_status()
    # 上游网关（CCR）连通性 + 模型注册清单 —— 0924 方案档 §三「health 增强（运维 P3→P2）」收口。
    # 为什么必须有：本机三次同源事故都是**模型 ID 失效而 /health 全绿**（09-06 `minimax-m3:free`
    # HTTP 400、09-19 `'ultra'` 无效、09-23 `qwen3.8-flash` 缺 provider 前缀）——即 09-22 定名的
    # 「静默不可用」家族。现在 watch 里的每个写死 ID 是否仍在册，直接是 /health 的可断言字段。
    # 纯读缓存（gwprobe 自己 stale-while-revalidate，TTL 300s），**不改 status**：
    # 上游网关不可达不等于 hub 坏了，与 code_stale / memory_backend 同一设计意图。
    out["ccr_gateway"] = gwprobe.status()
    # 出站熔断器状态（PT-20260927-12 A 档）：只列**开着的**闸 + 冷却剩余秒。
    # 为什么必须有：熔断一开，那些调用点会在**毫秒内**返回（不发网络）。
    # 没有这个字段，用户看到的是「GitHub 项目页空了」而没有任何原因字段——
    # 正是本仓反复治理的「静默不可用」家族，且它比网络故障更难自查。
    # 纯读、不改 status（熔断 ≠ hub 坏了），异常不得把 /health 打挂。
    try:
        out["breakers"] = resilience.instance.snapshot()
        # 2026-10-22 PT-13：免费模型重排的成功率必须可观测。
        # 免费池实测成功率 34~44%（会把请求路由到审核模型），不报出来就只能靠人工复现。
        try:
            import skill_rerank
            out["rerank"] = skill_rerank.stats()
            # 2026-10-22：判定缓存必须可观测 —— 否则「有缓存」与「缓存从没命中」长得一样。
            import judge_cache
            out["judge_cache"] = judge_cache.stats()
        except Exception as e:  # noqa: BLE001 — 自证端点不许被情报字段打挂
            out["rerank"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    except Exception as e:  # noqa: BLE001  # 情报字段不得把 /health 打挂
        out["breakers"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    # 画像最近检测时间（同属「health 增强」的另一半：CCR 连通性 + 画像检测时间 + DB 状态）。
    # 只给 last_sweep 会被「新一轮扫了 6 家、漏了第 7 家」骗过 ⇒ 必须给最坏值 oldest_check_age_s
    # 与 unchecked（在册却从没被扫到的家数，正是 09-22「在册却静默不可用 21 天」的形态）。
    try:
        out["profiles_last_check"] = healthx.profiles_last_check(
            vitals_mod.vitals.snapshot(), vitals_mod.vitals.last_sweep, vitals_mod.SWEEP_EVERY)
    except Exception as e:  # noqa: BLE001  # 情报字段不得把 /health 打挂
        out["profiles_last_check"] = {"state": "error",
                                     "error": f"{type(e).__name__}: {str(e)[:120]}"}
    if not db_ok:
        out["status"] = "degraded"
    return out


# ── 领域路由（C2：已从本文件抽成独立 module，见 src/api_*.py）──
app.include_router(agents_router)
app.include_router(vitals_router)
app.include_router(summarize_router)
app.include_router(sessions_router)
app.include_router(models_router)
app.include_router(system_router)


# ── ⚙设置（口令保护的敏感配置查看）────────────────────────

class PasscodeRequest(BaseModel):
    passcode: str = ""


@app.post("/api/settings/term-token")
async def settings_term_token(request: Request, req: PasscodeRequest):
    """口令正确时返回 TERM_TOKEN。

    有意用 POST 而非 GET：现有 api_rate_limit 只拦写方法，
    口令爆破会被 429 限流拦住（60 次/分钟/IP）。
    """
    if not HUB_PASSCODE:
        raise HTTPException(503, "未设置 HUB_PASSCODE，请先在 .env 配置后再使用设置页")
    if not hmac.compare_digest(req.passcode, HUB_PASSCODE):
        ip = request.client.host if request.client else "?"
        print(f"[settings] 口令错误：{ip}")
        raise HTTPException(401, "口令错误")
    tok = os.getenv("TERM_TOKEN", "")
    return {"term_token": tok, "set": bool(tok)}


# ── 端口管理 ──────────────────────────────────────────────────────────

# ── 只读发现扫描（S2）────────────────────────────────

class ScanIn(BaseModel):
    auto_register: bool = True


@app.post("/api/scan/run")
async def scan_run(body: ScanIn):
    """发现源扫描（docker/systemd/CLI 名单，全部只读探测）

    2026-09-24：扫描收尾补一次「定向重判」。原实现只跑 scanner.run_scan，
    而它「仅报告安装状态（不注册）」（见 scanner.py 顶部注释），卡片补发由
    profiles._dynamic_cli_agents 负责，其闸门读的是**上一轮 vitals 结论**
    ⇒ 点「自动扫描」永远刷不动一枚陈旧 not_installed，新装 CLI 出不来（opencode 实例）。
    重判只走 L1/L2（which / --version / --help），不碰模型、不烧 token。"""
    result = await asyncio.to_thread(scanner.run_scan, body.auto_register, db)
    rejudge: dict = {}
    try:
        rejudge = await asyncio.to_thread(
            vitals_mod.vitals.rejudge_stale, agent_profs_for_vitals())
    except Exception as e:  # noqa: BLE001  重判挂了不能把扫描本身弄失败
        rejudge = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    result["vitals_rejudge"] = rejudge
    need_reload = bool(rejudge.get("changed")) or bool(
        body.auto_register and result.get("added"))
    if need_reload and appstate.discovery:
        if rejudge.get("changed"):
            profiles_mod.invalidate_cli_cache()   # 30s 候补缓存不得压住刚翻案的卡
        appstate.discovery.reload()
    return result


@app.get("/api/ports")
async def api_ports():
    rows = list_listeners()
    # 标注归属已知 Agent
    known = {}
    if appstate.discovery:
        for a in await appstate.discovery.discover_all():
            if a.port:
                known[a.port] = a.id
    for r in rows:
        r["agent"] = known.get(r["port"])
    return {"listeners": rows, "count": len(rows)}


@app.get("/api/ports/{port}")
async def api_port_detail(port: int):
    return {"port": port, "in_use": port_in_use(port)}


# ── Dashboard ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    resp = templates.TemplateResponse(request, "index.html", {"version": VERSION})
    # 2026-09-26：HTML 原先不带任何缓存头/验证器 ⇒ 浏览器与已开标签页长期不自愈，
    # 部署新版后用户看到的仍是旧 UI。no-cache = 可存，但每次导航必须回源校验。
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── 兼容旧端点：/api/memory（别名到 L1 列表）──────────────────────────

@app.get("/api/memory")
async def get_memory_alias(request: Request, query: Optional[str] = None, limit: int = 10):
    """旧端点别名。**无 query** 时回本地 L1 列表；**有 query** 时转发到 /api/memory/search。

    ⚠ 2026-10-24 修一处现网 500：`GET /api/memory?query=x&limit=5` 实测返
    `AttributeError: 'Query' object has no attribute 'split'`。
    根因：本函数**位置传参** `search_memory(query, limit)`，而 search_memory 的签名是
    `(request, q, limit, sources, episodic)`（src/memory.py）⇒ query 落到 request 位、
    limit 落到 q 位、**sources 落到 limit 位**，拿到的是 FastAPI 的 `Query(default=…)`
    对象而不是字符串 ⇒ `_split_sources(sources)` 调 `.split()` 炸。
    引入于 v0.13.27~29 给 search_memory 加 request 参数时 alias 未同步；
    之所以一直没被发现，是因为**没有任何测试覆盖本 alias 的带 query 路径**
    （`grep -rn 'api/memory"' tests/*.py` 空）。
    修法：**改关键字传参**，并显式带 request。
    """
    if not query:
        rows = db.query("SELECT * FROM memories WHERE status='active' ORDER BY id DESC LIMIT ?",
                        (limit,))
        return {"memories": rows, "count": len(rows)}
    return await memory_mod.search_memory(request=request, q=query, limit=limit)


_embed_proxy = None      # 外框注入代理句柄（EMBED_UNIFY=0 时保持 None）


async def prov_loop():
    """代码溯源刷新：每 30s 一次 git status（走线程，不进任何请求路径）。

    为什么不搭 vitals 的慢拍：VITALS_SWEEP_SEC 默认 900s，而“工作区改了没”是
    健康台账里最想要分钟级响应的字段；搭慢拍会让它在 15 分钟里拿着旧值讲现测。
    """
    while True:
        try:
            await asyncio.to_thread(selfattest.refresh)
        except Exception as e:  # noqa: BLE001
            print("[prov] 溯源刷新异常（下轮重试）%s: %s" % (
                type(e).__name__, str(e)[:160]), flush=True)
        await asyncio.sleep(float(os.getenv("HUB_PROV_SEC", "30")))


#: WAL 周期检查点（2026-10-11）。默认 120s；置 0 关闭本条后台循环。
#: 背景：SQLite 的**自动**检查点是「每积满 wal_autocheckpoint（默认 1000）页，就在
#: 触发它的那条写语句里同步 checkpoint」。而 term_record 的逐帧写（每个按键 / 每帧
#: 输出一条 INSERT+commit）正落在这条路上 ⇒ 每 ~180 帧就有一帧被同步 checkpoint
#: 拖住。本机实测（100 万行真库副本，4000 帧）：
#:   wal_autocheckpoint=1000（默认）⇒ 每 183 帧一次尖峰，单帧 p99 6.9ms / max 116ms
#:   wal_autocheckpoint=0（关自动）  ⇒ 尖峰 0 次，单帧 p99 0.17ms / max 16ms
#: 而 checkpoint 本身**不是正确性前提**（WAL 本就是崩溃安全的），它只是防止 -wal
#: 无限增长。所以把它从「逐帧同步」挪到「后台定时」：事件循环不再被逐帧路径独占，
#: 总 fsync 次数不变（只是换了执行位置），WAL 也不会失控。
#: 为何不把默认值写进 db.py 的 PRAGMA：那是**全局**连接语义，别的库/别的连接
#: （engine 侧、MCP 侧）不该被本模块的取舍顺带改掉；改在 hub 自己的后台循环里，
#: 影响面就是「hub 主连接」这一个对象。成本 / 收益与 term_record 的预算修复同源：
#: 都是「一个同步动作挂在高频写路径上」。
HUB_WAL_CHECKPOINT_SEC = int(os.getenv("HUB_WAL_CHECKPOINT_SEC", "120"))


async def wal_checkpoint_loop():
    """定期在 to_thread 里 checkpoint（见 HUB_WAL_CHECKPOINT_SEC 的取证注释）。

    异常不打死循环：checkpoint 是优化，失败（被写者占着）下一轮再来。
    """
    if HUB_WAL_CHECKPOINT_SEC <= 0:
        return
    while True:
        await asyncio.sleep(HUB_WAL_CHECKPOINT_SEC)
        try:
            await asyncio.to_thread(db.checkpoint)
        except Exception as e:  # noqa: BLE001
            print(f"[db] WAL 检查点异常（下轮重试）{type(e).__name__}: {str(e)[:160]}", flush=True)


MODEL_DRIFT_SWEEP_SEC = int(os.getenv("HUB_MODEL_DRIFT_SWEEP_SEC", "300"))
#: 漂移写回开关（用户 09-28 授权默认开；置 0/false/no ⇒ 退回 v0.13.50 的只读告警）
MODEL_DRIFT_REPAIR = os.getenv("HUB_MODEL_DRIFT_REPAIR", "1") not in ("0", "false", "no")


def _drift_tick() -> dict:
    """一轮「体检 +（授权时）写回」，返回值即证据。纯同步，跑在 to_thread 里。"""
    out: dict = {"repaired": [], "skipped": [], "drift": modelcfg.drift_report()}
    if MODEL_DRIFT_REPAIR:
        out.update(modelcfg.repair_drift())
    return out


def _drift_log(out: dict, where: str) -> None:
    for _d in out.get("drift") or []:
        _bad = "，".join(f"{k}={v}" for k, v in (_d.get("owned") or {}).items())
        print(f"[modelcfg] {where} 配置漂移：{_d['id']} 持久化={_d['hub_model']} 但文件里 {_bad}",
              flush=True)
    for _x in out.get("repaired") or []:
        print(f"[modelcfg] {where} 漂移已写回：{_x['id']} → {_x['model']}"
              + (f"（备份 {len(_x['backups'])} 份）" if not _x.get("dry_run") else "（dry-run）"),
              flush=True)
    for _x in out.get("skipped") or []:
        print(f"[modelcfg] {where} 漂移未写回：{_x['id']} —— {_x['reason']}", flush=True)


async def model_drift_loop():
    """配置漂移巡检。为什么不能只靠启动那一次：CCR 与 hub 都在开机时起来（今日实测
    同秒），CCR 落笔改写 claude 的 env 三兄弟可能**晚于** hub 的启动修复 ⇒ 修完又被
    盖回去。所以按 MODEL_DRIFT_SWEEP_SEC（默认 300s）定期复检并写回。异常不打死循环。"""
    while True:
        await asyncio.sleep(MODEL_DRIFT_SWEEP_SEC)
        try:
            _drift_log(await asyncio.to_thread(_drift_tick), "巡检")
        except Exception as e:  # noqa: BLE001
            print(f"[modelcfg] 巡检异常（下轮重试）{type(e).__name__}: {str(e)[:160]}", flush=True)


async def vitals_loop():
    """可用心跳慢周期：首轮延后 2s（先让 hub 开接请求），之后每 VITALS_SWEEP_SEC 一轮。
    只跑 L1/L2（which / 文件头 / --version / --help / 端点探活），不碰模型；
    任何异常都不打死循环（否则一次偶发就把菜单永久冻在旧结论上）。"""
    await asyncio.sleep(2)
    while True:
        try:
            r = await asyncio.to_thread(vitals_mod.vitals.sweep, agent_profs_for_vitals())
            print("[vitals] sweep %s" % r, flush=True)
        except Exception as e:  # noqa: BLE001
            print("[vitals] sweep 异常（下轮重试）%s: %s" % (
                type(e).__name__, str(e)[:160]), flush=True)
        await asyncio.sleep(vitals_mod.SWEEP_EVERY)


@app.on_event("startup")
async def startup():
    global _embed_proxy
    db.init_db(config.db_path)
    # 关掉 hub 主连接的**自动**检查点，改由 wal_checkpoint_loop 定时做（见该循环注释：
    # 自动检查点每 1000 页就在「触发它的那条写语句」里同步 fsync，而 term_record 的
    # 逐帧写正是高频写路径 —— 实测每 183 帧一次 ~90ms 尖峰，全部落在事件循环里）。
    # 放在这一行是因为它必须紧跟 init_db：PRAGMA 是连接级状态，换连接就失效。
    try:
        db.execute("PRAGMA wal_autocheckpoint=0")
    except Exception as e:  # noqa: BLE001 —— 关不掉只是保留旧行为（有尖峰），不该拖垮启动
        print(f"[db] 关闭自动检查点失败（保留默认行为）：{type(e).__name__}: {str(e)[:120]}", flush=True)
    # v0.13.101：录制预算的计数表播种（存量库升级后计数从0 起跳会让预算判据失真）。
    # 幂等且只在计数行缺失时才做那一次全表 SUM；播种失败不许拖垮启动——
    # 计数表缺失时 total_bytes() 返回 0，代价是预算偏松（多录一点），
    # 而代价反过来（启动失败）要大得多。
    try:
        term_record.ensure_counted()
    except Exception as e:  # noqa: BLE001
        print(f"[term_record] 计数表播种跳过：{type(e).__name__}: {str(e)[:160]}", flush=True)
    appstate.discovery = AgentDiscovery(config, db=db)
    build_adapters(config)
    # v0.13.50 起的漂移体检 + v0.13.51 的授权写回：启动先修一轮（CCR 可能还没改写，
    # 所以另有定期巡检兜底）。写回属共享配置写入 ⇒ 落笔前时间戳备份，见 repair_drift。
    try:
        _drift_log(_drift_tick(), "启动")
    except Exception as e:  # noqa: BLE001 —— 体检/写回出岔子不许拖垮整个启动
        print(f"[modelcfg] 启动漂移处理异常：{type(e).__name__}: {str(e)[:160]}", flush=True)
    # 上游网关注入（/health 的 ccr_gateway 情报源）。watch 放两个「写死在配置里的模型 ID」：
    # manager 用的那个 + vitals L4 探针用的那个。上游一旦改名/下架，watch.<id>=false 当天可见，
    # 不必等探活烧一轮 token 才发现（09-23 的 M1 阻塞「拿不到在线清单」就此长期解除）。
    gwprobe.configure(config.manager_llm_base, config.manager_llm_key,
                      watch=[config.manager_llm_model, vitals_mod.RT_MODEL])
    tasks_mod.ensure_schema()
    # P0-1：数据集/评估/经验库四表（幂等建表，切片 A）
    dataset_mod.ensure_schema()
    # P1-5：技能资产表（幂等建表；与 skill.py 的整包摘要一起构成 A9 的资产准入）
    skill_assets_mod.ensure_schema()
    # P2 A10：行动三表（requests/decisions/results），幂等建表
    actionplane_mod.ensure_schema()
    # PT-20261021-04：情报卡与采集任务两表（幂等建表）
    refscout_mod.ensure_schema()
    tasks_mod.set_context(
        chat_fn=lambda a, m, s=None, mo=None, tr=None: _chat_dispatch(a, m, s, mo, tr),
        agent_ids_fn=lambda: [c["id"] for c in appstate.discovery.all_configs()])
    _spawn(tasks_mod.sweep_stale_tasks())
    _spawn(vitals_loop())
    _spawn(model_drift_loop())   # 配置漂移巡检（CCR 重启会把模型改回旧值）
    _spawn(prov_loop())   # 代码溯源（工作区脏度）刷新
    # P0-4：终端会话回收必须有独立心跳，不能寄生在前端轮询上
    _spawn(term_mod.reap_loop())
    # PT-20260929-02 补做：零输出即死的子进程没有任何 IO 事件可监听，
    # 只能靠定期 waitpid 探活发现。缺这条快车道，reap_loop 的 60s 间隔
    # 就是「前端空白最长 60 秒」的同义词。
    _spawn(term_mod.fast_reap_loop())
    _spawn(wal_checkpoint_loop())   # WAL 周期检查点（把自动检查点从逐帧写路径挪走）
    _spawn(refscout_mod.refresh_loop())   # 情报卡到期自动重跑（30 天/次，REF_AUTO_REFRESH=0 可关）
    # PT-20261024-08 B3/B4：保留期清理必须有独立心跳。
    # 为什么必须有（实测）：`term_recordings` 258.7 万行 / 597MB，而 `sweep()`
    # **只在 `Recorder.close()` 里调** ⇒ 长开会话 ⇒ 录像只增不减
    # （src/main.py 原注释自己写着「⚠ 未治本：sweep() 只在会话 close 时跑，
    #   表一天长 44 万行」）；`profile_events` 3,250 行/天且**全仓无 DELETE**。
    # ⇒ 不寄生在任何会话生命周期上，自带 1 小时心跳。
    _spawn(retention.retention_loop())
    mcpgw_mod.ensure_schema()
    cronjobs_mod.ensure_schema()
    cronjobs_mod.set_context(
        chat_fn=lambda a, m, s=None, mo=None, tr=None: _chat_dispatch(a, m, s, mo, tr))
    cronjobs_mod.start_engine()
    # 外框统一注入代理（用户 09-20 方案 b）：:3103 → qwenpaw :8088，HTML 出栈前插 <style>。
    # 起不来也不能影响 hub 本体：裹 try/except，顶多外框不统一（嵌入视图仍直连可用）。
    if profiles_mod.EMBED_UNIFY:
        try:
            qp = next((p for p in profiles_mod.PROFILES if p.get("id") == "qwenpaw"), None)
            if qp and qp.get("port"):
                _embed_proxy = embed_proxy_mod.EmbedProxy(
                    "QwenPaw", "127.0.0.1", qp["port"],
                    listen_host=os.getenv("EMBED_PROXY_HOST", config.host),
                    listen_port=profiles_mod.EMBED_PROXY_PORT)
                await _embed_proxy.start()
        except Exception as e:
            print(f"[Agent Hub] 注入代理启动失败（不影响其他功能）：{type(e).__name__}: {e}")
            _embed_proxy = None
    sa = selfattest.boot()   # 记下启动那一刻的 sha，供 /health 判 code_stale
    print(f"[Agent Hub] 启动完成 v{VERSION} sha={sa['git_sha_boot'] or '?'}，"
          f"监听 {config.host}:{config.port}")
    print(f"[Agent Hub] CCR: {config.ccr_url} | pi: {config.pi_url} | "
          f"jcode: {config.jcode_url} | TDAI: {config.tdaI_url}")
    print(f"[Agent Hub] LLM（记忆 L2 重建 / DAG 拆解）: {config.manager_llm_base} "
          f"model={config.manager_llm_model}")


@app.on_event("shutdown")
async def shutdown():
    for t in list(_bg_tasks):
        t.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        _bg_tasks.clear()
    if _embed_proxy is not None:
        await _embed_proxy.stop()
    term_mod.kill_all()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=config.host, port=config.port, log_level="info")
