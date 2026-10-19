"""Hub MCP Server —— 把 agent-hub 的本机事实源以 MCP 形式暴露给外部 Agent（Hermes 等）。

设计原则（v2.1 方案 D2）：
- **只读优先**：默认只暴露读取类工具；写工具需显式设 HUB_MCP_ALLOW_WRITE=1 才注册。
- **不重复实现业务**：一律转发 hub 自身 REST（/api/*），事实源唯一。
- **依赖最小化**：用标准库 urllib，不引入 httpx 等新依赖（hub venv 无 httpx）。
- **路径无冲突**：由 main.py 挂载到 /hub-mcp，实际 MCP 端点是 /hub-mcp/mcp
  （streamable_http_app 自带 /mcp 子路由，故不能挂到 /mcp，否则与 mcpgw 的 /mcp/* REST 冲突）。

注册工具：hub_list_agents / hub_list_ports / hub_memory_search / hub_memory_context
          / hub_kb_search / hub_kb_status / hub_skill_list / hub_skill_read / hub_skill_status
          / hub_skill_invoke / hub_skill_catalog（后两个 v0.13.127 PT-20261019-01：
          统一调用入口。invoke 是 POST 但**不算写工具**——只写 hub 自己的遥测账本，
          不过 ALLOW_WRITE 门，理由见 `_post` 注释）
"""
import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Optional

import tdai_client
import memfed
import memindex
import kb as kb_mod
import memory as memory_mod
from config import config as _cfg
from mcp.server.mcpserver import MCPServer

TIMEOUT_S = float(os.getenv("HUB_MCP_TIMEOUT", "10"))
ALLOW_WRITE = os.getenv("HUB_MCP_ALLOW_WRITE", "0") == "1"

#: 自指 URL。**默认从 config.port 推导，不写死 3102**（v0.13.127 PT-20261019-01）：
# 写死的版本在影子/隔离实例上会出一个很难看的失败模式——MCP 工具调用穿过影子
# **直达生产 :3102**，拿着影子的 token 打生产 ⇒ 401「凭据不匹配」，而排障时
# 两边日志都不像自己的错（实测踩到：隔离 smoke 的 invoke 401，真因就是这个）。
# 换端口自环正确以后，该失败模式连同「影子误写生产」的风险一起消失。
# 显式覆盖仍可用 HUB_MCP_SELF_URL。
HUB_URL = os.getenv("HUB_MCP_SELF_URL") or ("http://127.0.0.1:%d" % _cfg.port)

server = MCPServer("agent-hub")


def _get(path: str, **params) -> dict:
    """转发 GET 到 hub 自身 REST；失败时返回带 error 字段的 dict 而非抛异常，
    避免 MCP 会话被单个接口故障打断。"""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{HUB_URL}{path}" + (f"?{qs}" if qs else "")
    try:
        # x-hub-channel: mcp —— runlog 埋点靠它区分「MCP 工具转调」与「浏览器直连」。
        # 头可伪造，但只影响遥测归因（误标为 web），不涉鉴权；比按 UA 猜可靠。
        req = urllib.request.Request(url, headers={"x-hub-channel": "mcp"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 上游的 4xx/5xx **正文里才有诊断**（例如 /api/skill/read 的 409 带 candidates、
        # /api/kb/search 的 400 带「可用值清单」）。旧写法只回 "hub REST 409"，等于在 MCP 层
        # 把 hub 刚做好的失败表态又剥掉一次 —— 与本文件 99-101 行记的那条教训同型。
        # 补 detail 是 additive：error / path 两个旧键不动，既有工具不受影响。
        # 正文必须过 scrub：技能与记忆正文是凭据高危面。
        detail = ""
        try:
            detail = tdai_client.scrub(e.read().decode("utf-8", "replace"))[:400]
        except Exception:  # noqa: BLE001 — 取不到正文不该盖掉原本的 HTTP 码
            pass
        return {"error": f"hub REST {e.code}", "path": path,
                "http": e.code, "detail": detail}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "path": path}


def _post(path: str, body: dict, **params) -> dict:
    """转发 POST 到 hub 自身 REST（v0.13.127 PT-20261019-01：`hub_skill_invoke` 用）。

    与 `_get` 的两处差别，都是有原因的：
    ① **带凭据**：POST 过 writeauth 全局中间件（x-hub-token / ?token=）。MCP server
       与 hub 同进程（main.py 挂载），凭据名与 writeauth 完全同源
       （`TERM_TOKEN`/`HUB_PASSCODE`，.env 经 load_dotenv 进 os.environ）⇒
       **零新增凭据**，也不把 token 写进任何配置文件。取不到时如实回
       `error=...missing-credential`，不静默发一个必然 401 的请求。
    ② **不过 ALLOW_WRITE 门**：invoke 只往 hub 自己的遥测账本（profile_events）
       写一行，不碰任何用户状态/配置；若把它算「写工具」，未开 HUB_MCP_ALLOW_WRITE
       的部署就根本没有统一调用入口——Grill 裁定 2 的功能直接不存在。
       真正的配置写工具仍在 `if ALLOW_WRITE:` 段里，不受影响。
    """
    token = os.getenv("TERM_TOKEN", "") or os.getenv("HUB_PASSCODE", "")
    if not token:
        return {"error": "missing-credential", "path": path,
                "detail": "hub 进程未配置 TERM_TOKEN/HUB_PASSCODE，POST 类工具不可用"}
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{HUB_URL}{path}" + (f"?{qs}" if qs else "")
    payload = json.dumps(body or {}).encode("utf-8")
    req = urllib.request.Request(url, data=payload, method="POST",
                                 headers={"Content-Type": "application/json",
                                          "x-hub-token": token,
                                          "x-hub-channel": "mcp"})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        # 409 的 candidates / 400 的可用值清单都在上游正文里——剥掉它等于在 MCP 层
        # 把 hub 的失败表态再灭一次（与 _get 同一条教训）。
        detail = ""
        try:
            detail = tdai_client.scrub(e.read().decode("utf-8", "replace"))[:400]
        except Exception:  # noqa: BLE001 — 取不到正文不该盖掉原本的 HTTP 码
            pass
        return {"error": f"hub REST {e.code}", "path": path,
                "http": e.code, "detail": detail}
    except Exception as e:  # noqa: BLE001
        return {"error": f"{type(e).__name__}: {e}", "path": path}


@server.tool()
def hub_list_agents(kind: str = "", status: str = "") -> str:
    """列出本机已探测到的实体（agent/gateway/service/memory/tool）及运行状态与访问入口。
    kind 可填 agent|gateway|service|memory|tool；status 可填 running|installed|stopped。都留空则返回全部。"""
    data = _get("/api/agents")
    agents = data.get("agents", data if isinstance(data, list) else [])
    if kind:
        agents = [a for a in agents if a.get("kind") == kind]
    if status:
        agents = [a for a in agents if a.get("status") == status]
    slim = [{
        "id": a.get("id"), "kind": a.get("kind"), "status": a.get("status"),
        "port": a.get("port"), "desc": (a.get("desc") or "")[:80],
    } for a in agents]
    return json.dumps({"count": len(slim), "agents": slim}, ensure_ascii=False)


@server.tool()
def hub_list_ports(agent: str = "") -> str:
    """列出本机 TCP 监听端口及归属进程，用于端口冲突排查与网络诊断。agent 非空时按归属过滤。"""
    data = _get("/api/ports")
    ports = data.get("ports", data if isinstance(data, list) else [])
    if agent:
        ports = [p for p in ports if agent.lower() in str(p.get("process", "")).lower()
                 or agent.lower() in str(p.get("owner", "")).lower()]
    slim = [{
        "port": p.get("port"), "addr": p.get("addr"), "pid": p.get("pid"),
        "process": p.get("process"),
    } for p in ports]
    return json.dumps({"count": len(slim), "ports": slim}, ensure_ascii=False)


@server.tool()
def hub_memory_search(q: str, limit: int = 10,
                      sources: str = "local,tdai," + ",".join(memfed.enabled_ids())) -> str:
    """检索本机记忆：默认**全部记忆源并联**后 RRF 融合（本地 L1 + TDAI 权威库 + 各 agent
    记忆库 claude-mem/claude 原生/pi/codex/grok/hermes/workbuddy + 工作区文件 + 归档会话）。
    返回命中的记忆条目 + 逐路健康度；这是找回本机历史决策与约束的首选工具。
    **看到 count=0 请继续读 degraded / backends**：只有它们能区分「真没有这条记忆」
    与「上游挂了」——两者对外都是空结果，但含义完全相反。
    只想查权威库时显式传 sources="local,tdai"（更快）；sources 写错直接 400 不静默空结果。

    默认**不**收窄成快路集（与 hub_memory_context 相反）：这是显式召回工具，广度优先。
    2026-10-02 实测单次墙钟 448ms（查询 "CCR"），但多词中文查询实测到过 3.2s 且三路
    rg 超时（pi/codex/archived 1.9~2.5s）——所以在**交互卡顿可容忍**的场景显式传
    sources 收窄，在**要全量**的场景才用默认。A4 索引投影落地前，rg 逐文件扫描是慢源
    的主要成本。"""
    if limit > 50:
        limit = 50
    data = _get("/api/memory/search", q=q, limit=limit, sources=sources)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "engine": data.get("engine"), "count": data.get("count", 0),
        "memories": [{
            "id": m.get("id"), "layer": m.get("layer"),
            # TDAI 条目带的是 `type`，本地条目带的是 `category`；不兼容一下，
            # 外部 agent 看到的权威命中就是 category:null，丢语义。
            "category": m.get("category") or m.get("type"),
            # 不告诉调用方命中来自哪一路，它就分不出「233 条权威库」与
            # 「4 行陈旧便签」，而这两者的可信度差两个量级。
            "source": m.get("source"),
            "content": (m.get("content") or "")[:400],
        } for m in data.get("memories", [])],
        "tdai": bool(data.get("tdai")),
        # ↓ 三个字段是 v0.13.16 本批改动的**全部意义所在**。旧写法只往上提 engine/count/memories，
        # 于是 hub 后端刚修好的「静默不可用」在本层被原样复刻：外部 agent 永远只能看到一个
        # count:0 的 200，TDAI 挂了它无从得知（09-24 独立复核：本文件是最重的一条发现）。
        "degraded": data.get("degraded") or [],
        "backends": data.get("backends") or [],
        "took_ms": data.get("took_ms"),
    }, ensure_ascii=False)


@server.tool()
def hub_memory_context(sources: str = "") -> str:
    """获取开局上下文包（L3 画像 + L2 工作记忆 + 相关 L1 事实 + **其他 Agent 记忆的联邦段**）。
    新会话初始化时先调它，避免在不知道本机约定与约束的情况下动手。

    默认 sources 走**快路联邦集**（`local,tdai` + claude_mem/workbuddy_memory/claude_projects），
    而不是端点原来的 `local,tdai` —— 2026-10-02 实测：默认口径整包 `fed.sources=0`、
    正文 1585 字符、联邦段**完全缺席**，也就是说 claude/codex 的开局包看不到任何别家
    agent 的记忆（而它们恰恰是本机 6 路 CLI 里最会沉淀约定的一路）。改快路口径后同一次
    真实请求 230ms 走完，5 个联邦源全 ok。
    慢三路（pi_sessions/codex_sessions/archived_sessions，实测 rg 1.9~2.5s）**不在默认里**，
    等 A4 索引投影落地后再议。

    只要权威库：显式传 `sources="local,tdai"`（更快，但无联邦段）。
    sources 写错直接 400 不静默空结果。返回含 `degraded`（为真＝这一包**不完整**）与
    `fed`：逐源 `done` / `skipped_budget` / `failed` —— 只看正文分不清「别家没这条记忆」
    与「别家的源被预算掐了或挂了」，两者对外都是"没有相关内容"。"""
    # 空串 ⇒ 用端点侧的 FED_FAST_SOURCES（唯一真源在 memory.py）：
    # 工具层不抄一份字符串，否则又多一处会各自漂移的默认值。
    data = _get("/api/memory/context", max_chars=5000,
                sources=sources or memory_mod.FED_FAST_SOURCES)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    # 不再对 JSON 字符串硬截 [:6000]：那会截出**非法 JSON** 交给调用方，
    # 对端解析器直接报错，比返回一段长文本糟糕得多。截断交给服务端的 max_chars
    # 在正文层面做，本层保证结构完整。degraded / backends 也随包透传。
    return json.dumps(data, ensure_ascii=False)


@server.tool()
def hub_kb_search(q: str, k: int = 8, routes: str = ",".join(kb_mod.ROUTES)) -> str:
    """**联邦检索**：并发调起本机检索面（记忆权威库 TDAI、XERJ 文档全文、工作区全文兜底、
    归档会话）后 RRF 融合。找「本机以前是否记过这件事 / 有无相关文档」时优先用它。

    返回 `results` + 逐路 `backends` + `degraded` + `note`。**看到 count=0 必读 degraded/note**：
    「真没有」与「那路挂了」在这里是可区分的，别把两者当成一件事。
    routes 可用值：tdai / xerj / workspace / archived；旧名 turbovec / local 仍接受但会回
    退役说明（turbovec 2026-10-18 已删，local 默认不参与）。"""
    if k > 30:
        k = 30
    data = _get("/api/kb/search", q=q, k=k, routes=routes)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "count": data.get("count", 0),
        "engine": data.get("engine"),
        "results": [{
            "source": r.get("source"),
            "path": r.get("path"), "chunk": r.get("chunk"),   # 文档路独有，给调用方能开原文
            "category": r.get("category") or r.get("type"),
            "content": (r.get("content") or r.get("preview") or "")[:400],
            "score": r.get("score"),
        } for r in data.get("results", [])],
        "backends": data.get("backends") or [],
        "degraded": data.get("degraded") or [],
        "note": data.get("note"),
        "took_ms": data.get("took_ms"),
    }, ensure_ascii=False)


@server.tool()
def hub_kb_status() -> str:
    """查本机各检索面是否可用、文档索引多新（chunks/built_at/age_days）、记忆库多大。
    动手前先调它一次，确认你要的那路是不是已经 degraded——**不要拿一个缺失的检索面当「没有资料」**。"""
    data = _get("/api/kb/status")
    return json.dumps(data, ensure_ascii=False)


@server.tool()
def hub_skill_list(q: str = "", routes: str = "", limit: int = 50) -> str:
    """列出本机**真实在用**的技能（SKILL.md）：磁盘四路 claude / pi / techdocs / superpowers，
    再加 TDAI 技能注册表一路。要判断「本机是否已有现成技能能干这件事」时**先调它**，
    别凭印象造轮子，也别只查记忆。

    返回 `items` + 逐路 `backends` + `degraded` + `note` + `dedup`（软链别名，同一技能只列一次）。
    **看到 count=0 必读 degraded/note**：「TDAI 注册表 0 行、磁盘 37 条在用」是本机现状
    （技能权威源未裁，PT-20260924-08 T2-1），不等于「本机没有技能」。
    routes 留空＝全部路；写错值直接 400，不给你静默空结果的机会。
    `fm=false` 的条目是**没有 frontmatter 的技能**（名字回退成目录名），不是坏数据。
    返回里的 `total` / `truncated` 是**截断自报**（v0.13.128）：`count < total` 时
    说明这一页没装下，调大 `limit`（上限 1000）或加 `q`——**别把 count 读成总数**。"""
    if limit > 1000:
        limit = 1000
    data = _get("/api/skill/list", q=q, routes=routes, limit=limit)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "count": data.get("count", 0),
        # v0.13.128：与后端同口径。MCP 层如果只透传 count，等于在门面上复刻静默截断
        # （v0.13.127 之前 max 200 硬顶，371 条只回 200）。
        "total": data.get("total", 0),
        "truncated": data.get("truncated", False),
        "engine": data.get("engine"),
        "items": [{
            "name": i.get("name"),
            "description": (i.get("description") or "")[:300],
            "route": i.get("route"), "routes": i.get("routes"),
            "path": i.get("path"), "via_symlink": i.get("via_symlink"),
            "fm": i.get("fm"), "bytes": i.get("bytes"),
            # 下面四个只有 TDAI 注册表那一路才有（磁盘路为 None）
            "id": i.get("id"), "status": i.get("status"),
            "version": i.get("version"), "owner_agent_id": i.get("owner_agent_id"),
        } for i in data.get("items", [])],
        "dedup": data.get("dedup") or {},
        # 以下四个键**必须原样透传**：剥掉就等于在 MCP 层复刻「静默不可用」
        "backends": data.get("backends") or [],
        "degraded": data.get("degraded") or [],
        "note": data.get("note"),
        "took_ms": data.get("took_ms"),
    }, ensure_ascii=False)


@server.tool()
def hub_skill_read(name: str, route: str = "") -> str:
    """读一个技能的**全文**（已脱敏）。`name` 取 hub_skill_list 里的 name。

    同名技能在多路下指向**不同文件**时回 409，`detail` 里带 candidates —— 带上 route 再调一次，
    门面不会替你静默挑一个。正文超上限会 `truncated=true` 并给 `bytes_total`，**不会静默截断**。
    路径穿越会被 400 拒（realpath 必须落在技能目录内）。"""
    return json.dumps(_get("/api/skill/read", name=name, route=route), ensure_ascii=False)


@server.tool()
def hub_skill_status() -> str:
    """查技能资产面板：每个技能目录是否可用、各有多少条、哪些缺 frontmatter、哪些经软链、
    TDAI 注册表有几行、以及磁盘与注册表的缺口（`gap.note`）。
    动手前先调它一次，别把「某一路 degraded」当成「本机没有那个技能」。"""
    return json.dumps(_get("/api/skill/status"), ensure_ascii=False)


@server.tool()
def hub_skill_invoke(name: str, route: str = "", caller: str = "", task: str = "") -> str:
    """**统一调用一个技能**（v0.13.127 PT-20261019-01）：回传它的 SKILL.md 指令正文，
    并在 hub 记一条调用流水（`skill.invoke`）。

    语义边界（重要）：hub 代理的是「激活 + 记账」——**执行仍由你（agent 侧）完成**：
    按返回的 instructions 行事，技能自带脚本用你自己的 bash/工具跑。hub 不代跑代码。

    什么时候用：hub_skill_list / hub_skill_catalog 里看到匹配的技能后，**点名**取它的
    完整指令。`caller` 建议传你的客户端名（如 "codex"），调用记录面板按它分组；
    不传则记 "mcp"（自报身份，不是协议取证）。`task` 一句话说明你要用它干什么。

    失败态与 /read 同族：同名多路 409 带 candidates（带 route 再调）；未知名 404；
    正文超上限 `truncated=true` 并给 `bytes_total`（不静默截断）。"""
    body = {"name": name, "route": route, "caller": caller, "task": task}
    return json.dumps(_post("/api/skill/invoke", body), ensure_ascii=False)


@server.tool()
def hub_skill_catalog(q: str = "", limit: int = 200) -> str:
    """技能说明目录（Agent Skills 标准 L1「发现层」）：每条技能的 name + description +
    路由分布 + 整包 digest + 估算 token。**不带正文**——正文用 hub_skill_invoke 点名取。

    与 hub_skill_list 的分工：list 偏诊断（逐路 backends/degraded/dedup），
    catalog 偏「有什么可用」（纯 L1 面 + digest，供判断技能是否被改过）。
    `q` 与列表页同一套模糊口径（先精确子串，容忍手误按编辑距离）。"""
    if limit > 1000:
        limit = 1000
    data = _get("/api/skill/catalog", q=q, limit=limit)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "count": data.get("count", 0),
        "total": data.get("total", 0),
        # v0.13.128：catalog 后端本来就回 total；门面把「是否少收」算成一个布尔，
        # 免得调用方自己比 count/total（比漏了就是又一次静默截断）。
        "truncated": data.get("count", 0) < data.get("total", 0),
        "items": [{
            "name": i.get("name"),
            "description": (i.get("description") or "")[:300],
            "routes": i.get("routes"),
            "digest": i.get("digest"),
            "est_tokens": i.get("est_tokens"),
        } for i in data.get("items", [])],
        "note": "正文不在本目录里：对某条技能感兴趣就 hub_skill_invoke(name=...) 点名取指令",
        "took_ms": data.get("took_ms"),
    }, ensure_ascii=False)


@server.tool()
def hub_cloudcli_projects() -> str:
    """列用户在 CloudCLI（Claude 原生 Web 界面）的全部项目：精确名称、路径、
    是否星标、活跃会话数、最近活动时间。直读 auth.db（与 cloudcli 服务活死解耦）。
    用户要在某个项目上开始工作时，先看这个清单再决定派发目标。"""
    data = _get("/api/cloudcli/projects")
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "count": data.get("count", 0),
        "ok": data.get("ok"),
        "projects": [{
            "name": p.get("name"),          # 精确名称：自定义名 > basename
            "path": p.get("path"),
            "starred": p.get("starred"),
            "sessions": p.get("sessions"),
            "last_activity": p.get("last_activity"),
        } for p in (data.get("projects") or [])],
    }, ensure_ascii=False)


if ALLOW_WRITE:
    @server.tool()
    def hub_memindex_rebuild(scope: str = "dry_run") -> str:
        """重建/刷新记忆索引投影层（memindex FTS）。默认不注册——需 HUB_MCP_ALLOW_WRITE=1。

        `scope` 三选一，**先跑 dry_run**：
          · `dry_run`    —— 不写库，只采样外推 `est_final_bytes` 与建库耗时。
                          首次务必用它：容量倍率必须按实测真数定（规格 §八 裁定 2：
                          写死 1.5 属拍脑袋，不接受）。
          · `full`       —— 全量重建（清空旧投影后重建）。
          · `incremental`—— 增量刷新。

        磁盘闸在开写前判：水位不足直接返回 ok=False，检索侧 fail-closed 回退 rg，
        **绝不自动删索引**——索引是可重建的派生数据，但删除权不在本工具。

        代价（规格 §八 裁定 3）：`memindex.db` **不进备份轮换**，丢了只能重建，
        重建期间检索降级回 rg（变慢，不返错）。
        """
        if scope not in ("dry_run", "full", "incremental"):
            return json.dumps({"ok": False,
                               "error": f"scope 须为 dry_run|full|incremental，收到 {scope!r}"},
                              ensure_ascii=False)
        import threading
        rep = memindex.build(scope=scope)
        out = {"ok": rep.ok, "scope": rep.scope, "files": rep.files,
               "bytes_corpus": rep.bytes_corpus, "chunks": getattr(rep, "chunks", None),
               "ms": getattr(rep, "ms", None), "notes": list(rep.notes or [])}
        out = {k: v for k, v in out.items() if v is not None}
        out["probe"] = memindex.probe()
        return json.dumps(out, ensure_ascii=False)

    @server.tool()
    def hub_memory_add(content: str, category: str = "fact", layer: str = "L1") -> str:
        """写入一条 hub 记忆。默认不注册——需 HUB_MCP_ALLOW_WRITE=1 才启用。
        category 可填 fact|decision|constraint|preference。"""
        # v0.13.6：写端点统一要凭据（writeauth 闸门）。本工具默认不注册（HUB_MCP_ALLOW_WRITE=1 才有），
        # 但若哪天打开，不带 token 就是"功能静默 401"——所以这里就把凭据带上。
        _h = {"Content-Type": "application/json"}
        _t = os.getenv("TERM_TOKEN", "")
        if _t:
            _h["x-hub-token"] = _t
        req = urllib.request.Request(
            f"{HUB_URL}/api/memory/l1",
            data=json.dumps({"content": content, "category": category, "layer": layer}).encode(),
            headers=_h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
                return resp.read().decode("utf-8")
        except Exception as e:  # noqa: BLE001
            return json.dumps({"error": f"{type(e).__name__}: {e}"}, ensure_ascii=False)


def build_app():
    """返回可挂载到 FastAPI 的 Starlette 子应用。"""
    return server.streamable_http_app()
