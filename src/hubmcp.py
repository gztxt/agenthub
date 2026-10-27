"""Hub MCP Server —— 把 agent-hub 的本机事实源以 MCP 形式暴露给外部 Agent（Hermes 等）。

设计原则（v2.1 方案 D2）：
- **只读优先**：默认只暴露读取类工具；写工具需显式设 HUB_MCP_ALLOW_WRITE=1 才注册。
- **不重复实现业务**：一律转发 hub 自身 REST（/api/*），事实源唯一。
- **依赖最小化**：用标准库 urllib，不引入 httpx 等新依赖（hub venv 无 httpx）。
- **路径无冲突**：由 main.py 挂载到 /hub-mcp，实际 MCP 端点是 /hub-mcp/mcp
  （streamable_http_app 自带 /mcp 子路由，故不能挂到 /mcp，否则与 mcpgw 的 /mcp/* REST 冲突）。

注册工具：hub_list_agents / hub_list_ports / hub_memory_search / hub_memory_context
          / hub_memory_archive（v0.13.161 PT-20261025-11：统一归档写入口，POST 但
          默认注册——它是用户令「所有 agent 统一接口写记忆」的唯一 sanctioned 通道：
          身份必填 + 质量门 + 审计，与 l1 写端点同等鉴权。不占 ALLOW_WRITE 门的理由
          同 invoke：门管的是「顺手写配置」类工具，不是产品明要的统一写入口）
          / hub_kb_search / hub_kb_status / hub_skill_list / hub_skill_read / hub_skill_status
          / hub_skill_invoke / hub_skill_catalog（后两个 v0.13.127 PT-20261019-01：
          统一调用入口。invoke 是 POST 但**不算写工具**——只写 hub 自己的遥测账本，
          不过 ALLOW_WRITE 门，理由见 `_post` 注释）
          / hub_skill_acquire（v0.13.135：任务描述 → 本地优先 + 商店自动补货。
          会经 `/api/skill/store/acquire` 往 techdocs 发现点写技能文件，故
          **只在调用方显式传 allow_install=true 时**才可能落盘；默认值见工具说明）
          / hub_skill_auto（v0.13.138：任务描述 → 自动选中并**激活**，一次调用回正文。
          与 acquire 同一条选人逻辑，差别是记账分桶 auto_invoke 且语义是「拿来用」）
          / hub_ref_scout / hub_ref_get（PT-20261021-04：功能模块 → GitHub 借鉴源情报卡。
          scout 是异步任务，先回 job_id；get 读任务进度或整张卡。**只读采集**：
          不克隆、不改项目、不自动加依赖）
          / hub_intake（PT-20261022-01：接单前置四查一简报。只做组合与呈现，
          记忆/知识库走本进程端点、技能只查本地、参考库按需深查）
          / hub_dispatch / hub_run_status / hub_replan（PT-20261027-04：全链路派发。
          拆解 → 选型 → 派发 → 验收 → 未达标自动重拆，最多 2 轮硬上限。默认 `confirm=False`
          只拆不落库 ⇒ 不花钱；`confirm=True` 才落库执行，`auto_rounds=True` 才自动重拆）
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


def _post(path: str, body: dict, _timeout: float = 0, **params) -> dict:
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
        with urllib.request.urlopen(req, timeout=_timeout or TIMEOUT_S) as resp:
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
def hub_memory_archive(content: str, agent: str, category: str = "fact",
                      kind: str = "distilled", session_id: str = "",
                      note: str = "") -> str:
    """统一归档写入口（v0.13.161，PT-20261025-11）：把一条记忆提交进记忆中心。

    **所有 agent 都该用这个入口**往记忆中心写——提交即带 agent 身份与溯源，
    过质量门（脱敏 → 精确去重 → 近似 supersede → 每 agent 上限），并留审计。
    参数：
      content  记忆正文（必填，≤8000 字符）
      agent    提交方标识（必填；在册 agent id 或 'hub'。手动补齐用 REST 端点的
              agent='manual', kind='manual'——MCP 是 agent 通道，不代人工署名）
      category fact|decision|constraint|preference
      kind     distilled（agent 提炼，默认）| manual
    返回 status：created（新入库）| duplicate（精确重复，返回已存在 id）|
    superseded（近似重复，新行入库、旧行标 superseded 并列出 id）。
    **不复制**你本地已有的记忆库——本入口只收新提交；读请用 hub_memory_search。
    """
    if not content or not content.strip():
        return json.dumps({"error": "content 不能为空"}, ensure_ascii=False)
    if not agent or not agent.strip():
        return json.dumps({"error": "agent 不能为空（统一接口要能追责到提交方）"},
                          ensure_ascii=False)
    body = {"content": content.strip(), "agent": agent.strip(), "category": category,
            "kind": kind}
    if session_id:
        body["session_id"] = session_id
    if note:
        body["note"] = note
    return json.dumps(_post("/api/memory/archive", body=body), ensure_ascii=False)


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
def hub_skill_invoke(name: str, route: str = "", caller: str = "", task: str = "",
                     allow_store: bool = True) -> str:
    """**统一调用一个技能**（v0.13.127 PT-20261019-01）：回传它的 SKILL.md 指令正文，
    并在 hub 记一条调用流水（`skill.invoke`）。

    语义边界（重要）：hub 代理的是「激活 + 记账」——**执行仍由你（agent 侧）完成**：
    按返回的 instructions 行事，技能自带脚本用你自己的 bash/工具跑。hub 不代跑代码。

    什么时候用：hub_skill_list / hub_skill_catalog 里看到匹配的技能后，**点名**取它的
    完整指令。`caller` 建议传你的客户端名（如 "codex"），调用记录面板按它分组；
    不传则记 "mcp"（自报身份，不是协议取证）。`task` 一句话说明你要用它干什么。

    **v0.13.135 自动补货**：`allow_store=true`（默认）时，本地找不到该名字会去 SkillHub
    商店搜同名技能，按相似度门槛装到 `techdocs` 发现点再返回正文（响应里
    `store_fallback` 写明装了哪个 slug、版本、来源；`trust=community` 表示第三方文本，
    用前建议人工过一遍）。不想让它写盘就传 `allow_store=false`。
    若你手里只有任务描述、没有具体技能名，用 `hub_skill_acquire` 更合适。

    失败态与 /read 同族：同名多路 409 带 candidates（带 route 再调）；未知名 404
    （`allow_store=true` 且商店也没装到时，detail 里带 `store_fallback` 的失败原因）；
    正文超上限 `truncated=true` 并给 `bytes_total`（不静默截断）。"""
    body = {"name": name, "route": route, "caller": caller, "task": task,
            "allow_store": bool(allow_store)}
    return json.dumps(_post("/api/skill/invoke", body), ensure_ascii=False)


@server.tool()
def hub_skill_acquire(query: str, caller: str = "", task: str = "",
                      allow_install: bool = True) -> str:
    """**按任务描述自动取技能**（v0.13.135）：本地优先，没有就搜 SkillHub 商店并按门槛装好。

    这是「我需要在做 X，但技能中心没有对应技能」时的入口——传一句任务/需求
    （中文英文都可以），不用先知道技能叫什么：
      ① 本地 BM25 命中够强 ⇒ `source=local`，直接回正文，**不联网、不装东西**；
      ② 本地不够像 ⇒ 搜商店。**同类往往有十几条**，所以不是「搜到第一条就装」：
         按六维打分（相关性/安全报告/下载量/维护度/认证/是否要自带 Key）排序，
         安全报告 `suspicious` 与「不像」都是硬否决，质量分低于 0.45 也不装；
         同类有免 Key 的就优先免 Key。装好（默认装到 `techdocs`）后**立刻**回该技能
         的指令正文（`source=store`，带 `trust=community` 警告）。
         `store.ranking` 里逐条给出各维度分与否决理由，可据此判断要不要换词重试。
      ③ 都没有 ⇒ `ok=false` + `reason`，并回 `local.candidates` / `store.ranking`
         供你自己判断下一步，**不会随便装一个不相干的东西**。

    `allow_install=false` 只检索、报告「将会装什么」（配合 `dry_run` 预览）。
    自动安装有小时额度（默认 6 次/小时），超了会明确说「额度已满」。
    返回正文同样只是**说明书**：执行仍在你自己的工具环境里完成，hub 不代跑脚本。
    """
    body = {"query": query, "caller": caller or "mcp", "task": task,
            "allow_install": bool(allow_install)}
    return json.dumps(_post("/api/skill/store/acquire", body), ensure_ascii=False)


@server.tool()
def hub_skill_auto(task: str, caller: str = "", allow_store: bool = True) -> str:
    """**按任务描述自动选技能并激活**（v0.13.138）：不用先知道技能叫什么。

    什么时候用：接到一个任务、**手里没有具体技能名**时，先调它一次。服务端按
    任务描述排序：本地技能够像就直接回正文（不联网）；不够像才搜 SkillHub 商店
    并按六维质量分择优安装（默认 techdocs），装完立刻回正文。
    这比「先 list 再挑名字再 invoke」少两轮，也不会因为名字记错而漏掉本机等价技能。

    返回 `source=local|store|none` 说明这次从哪拿到；`content` 是技能的 SKILL.md
    指令正文。**执行仍由你在自己的工具环境里完成**——hub 只做「选中 + 激活 + 记账」，
    不代跑技能自带脚本（与 hub_skill_invoke 同一边界）。

    失败是 200 + `ok=false` + `reason`，并回 `local.candidates` / `store.ranking`
    让你判断要不要换更具体的词；**不会为了交差随便装一个不相干的技能**（商店有
    「不像 / 安全可疑 / 质量分 <0.45」三重门槛，自动安装还有小时额度）。
    `allow_store=false` = 只用本地，不联网不写盘（短预算/离线场景）。
    """
    body = {"task": task, "caller": caller or "mcp", "allow_store": bool(allow_store)}
    return json.dumps(_post("/api/skill/auto", body), ensure_ascii=False)


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
def hub_ref_scout(module: str, keywords: str = "", lang: str = "",
                  identifiers: str = "", allow_paid: bool = False) -> str:
    """**给一个功能模块找 GitHub 借鉴源**（PT-20261021-04）。异步任务：立即返回
    `job_id`，用 `hub_ref_get(job_id=...)` 轮询；检索完成前就能按 card_id 读词法版卡。

    `keywords` 传英文技术词、逗号分隔（如 "terminal,websocket,pty"）；留空则由
    后台用免费模型抽词，失败退中文词典。`identifiers` 是代码里的函数/类名，用于
    代码搜索臂（最多 2 个）。`allow_paid=true` 时**付费快档优先**、失败再试免费档（单次约几千
    token，**默认关闭**）；关着时只走免费档，失败就降级为纯词法并在卡上注明。
    为什么付费优先：实测免费两档几乎稳定返回 only-thinking 并占满超时，排在前面会把
    预算吃光，付费档根本轮不到。

    卡上每条都有 `as of`、下次刷新时间、SPDX 许可证与 copyleft 提示。
    **只出建议与固定引用（repo@sha），不克隆、不改项目、不自动加依赖。**"""
    body = {"module": module,
            "keywords": [k.strip() for k in keywords.split(",") if k.strip()],
            "lang": lang.strip(),
            "identifiers": [i.strip() for i in identifiers.split(",") if i.strip()],
            "allow_paid": bool(allow_paid)}
    data = _post("/api/ref/scout", body)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({**data,
                       "next": "轮询 hub_ref_get(job_id=...)；status=done 后读 card_id 的 md"},
                      ensure_ascii=False)


@server.tool()
def hub_ref_get(card_id: str = "", job_id: str = "", q: str = "",
                limit: int = 10) -> str:
    """读参考情报：`job_id` 看任务进度；`card_id` 取整张卡（含 Markdown 正文）；
    都不传给则列出最近的卡（`q` 按模块名过滤）。

    情报卡的读法：先看候选表的「功能/实现/设计」三列与判定列，
    再看「可借鉴点」里的 `repo@sha` 固定引用。**判定列为 `unjudged` 表示本轮
    重排没覆盖到（输出截断），不要当成已认可**；`lexical_fallback` 表示整卡只有
    词法分，相关度需要人工复核。"""
    if job_id:
        data = _get(f"/api/ref/jobs/{job_id}")
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        card = data.get("card") or {}
        return json.dumps({
            "job_id": job_id, "status": data.get("status"), "stage": data.get("stage"),
            "error": data.get("error"), "card_id": data.get("card_id"),
            "card_status": card.get("status"), "elapsed_s": card.get("elapsed_s"),
            "ready": bool(card),
        }, ensure_ascii=False)
    if card_id:
        data = _get(f"/api/ref/cards/{card_id}")
        if "error" in data:
            return json.dumps(data, ensure_ascii=False)
        return json.dumps({"id": data.get("id"), "module": data.get("module"),
                           "status": data.get("status"), "as_of": data.get("as_of"),
                           "next_refresh": data.get("next_refresh"),
                           "md": data.get("md")}, ensure_ascii=False)
    data = _get("/api/ref/cards", q=q, limit=max(1, min(limit, 50)))
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "total": data.get("total"),
        "items": [{"id": i.get("id"), "module": i.get("module"),
                   "status": i.get("status"), "as_of": i.get("as_of"),
                   "candidates": i.get("candidates"), "top": (i.get("top") or {}).get("repo")}
                  for i in data.get("items", [])],
        "note": "传 card_id 取整张卡；传 job_id 看任务进度",
    }, ensure_ascii=False)


@server.tool()
def hub_intake(task: str, deep: bool = False, auto_deep: bool = True,
               caller: str = "", escalate: str = "auto") -> str:
    """**接单前置**：拿到任务后先调它，一次把「记忆 / 知识库 / 技能 / 参考库」四查做完，
    返回一份可复核简报（`brief` 是 markdown，可直接贴进对话）。

    为什么要有这个工具：这四类资料各有入口，但**没有任何一步要求 agent 都过一遍**，
    结果常见形态是「凭印象开工，出问题才回头翻」。本工具只做组合与呈现，不新增检索能力。

    读法（四条，缺一条就会把它误用成结论）：
    - 先看 `verdict`：`ready`（有可用线索）/ `thin`（四路都通但无线索）/ `degraded`（有路挂了）。
      后两者都不该直接开工：`thin` 换词重查，`degraded` 先修那一路。
    - `counts` 只报条数，**`memory.count=0` 必须**同时看 `degraded`：0 命中与「那一路挂了」
      在简报里是两种写法，别把后者当成「本机没有相关资料」。
    - `skill` 只做**本地**匹配、不装新技能（接单阶段不该有写盘副作用）。命中后要用
      `hub_skill_invoke(name=...)` 取正文再照着做。
    - `ref`（参考库）默认**不查**：单次 21.8~56.8s 吃 GitHub 额度。任务命中
      「新增/实现/选型/对标…」才自动升级，`deep_reason` 会写明原因；`deep=True` 强制查。
    简报只给线索与出处，**引用前回原文核对**。

    **本地没线索时会外扩**（`escalate=auto` 且判定 `thin`）：技能中心（只查不装）→
    GitHub 仓库搜索 → 参考库卡片；联网搜索本机无可用 provider，简报里会如实写「不可用」。
    外扩结果看 `external`，**不是本机结论**，用前自己核。`escalate="off"` 可只查本地。

    非 MCP 工具链用 `scripts/intake.sh "<任务>"` 取同一份简报（同一端点，无第二套口径）。"""
    body = {"task": task, "deep": deep, "auto_deep": auto_deep, "caller": caller,
            "escalate": escalate}
    # 参考库深查默认等 90s，外层超时必须放宽，否则 MCP 层先超时、上游还在跑。
    data = _post("/api/intake/task", body, _timeout=(200 if deep else 120))
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    ext = data.get("external") or {}
    return json.dumps({
        "brief": data.get("brief"),
        "verdict": (data.get("plan") or {}).get("verdict"),
        "coverage": (data.get("plan") or {}).get("coverage"),
        "gaps": (data.get("plan") or {}).get("gaps"),
        "counts": {"memory": (data.get("memory") or {}).get("count"),
                   "kb": (data.get("kb") or {}).get("count"),
                   "skill": (data.get("skill") or {}).get("name") or None,
                   "useful": (data.get("plan") or {}).get("useful"),
                   "noise": (data.get("plan") or {}).get("noise"),
                   "meta": (data.get("plan") or {}).get("meta")},
        "external": {"triggered": ext.get("triggered"),
                     "status": ext.get("status"),
                     "reason": ext.get("reason"),
                     "skill": (ext.get("skill_store") or {}).get("picked"),
                     "repos": [x.get("repo") for x in
                               ((ext.get("gh") or {}).get("rows") or [])],
                     "ref_card": (ext.get("ref") or {}).get("card_id"),
                     # 卡还没落库（实测 25~70s）时 `ref_card` 有值但取必 404：
                     # 状态与原因必须一起给，否则调用方只能从空 rows 猜。
                     "ref_state": (ext.get("ref") or {}).get("state"),
                     "ref_note": (ext.get("ref") or {}).get("reason"),
                     "web": (ext.get("web") or {}).get("error"),
                     "took_s": ext.get("took_s")},
        "deep_reason": data.get("deep_reason"),
        "card_id": (data.get("ref") or {}).get("card_id"),
        "elapsed_s": data.get("elapsed_s"),
        "degraded": {"memory": (data.get("memory") or {}).get("degraded"),
                     "kb": (data.get("kb") or {}).get("degraded")},
    }, ensure_ascii=False)


@server.tool()
def hub_dispatch(goal: str, agent: str = "", confirm: bool = False,
                 auto_run: bool = True, auto_rounds: bool = False,
                 verify_kind: str = "nonempty") -> str:
    """**全链路派发**（PT-20261027-04）：把一句目标变成 DAG → 派给各 agent 执行 →
    验收 → 若未达标**自动重拆一轮**（最多 2 轮，硬上限不可放宽）。

    这条链路治的是「派发技能只有手册、没有编排」：技能 `agent-dispatch` 讲清了
    「该不该派、派给谁、怎么判活」，但没有任何入口把「拆解→选型→派发→汇总→再派」
    串起来，每个 agent 只能手动拆、手动 curl。本工具是那条链路的唯一入口。

    用法（两段式，**默认不花钱**）：
      1. `hub_dispatch(goal="…")`          → 只拆解，返回 DAG 预览（不落库、不执行）
      2. `hub_dispatch(goal="…", confirm=True)` → 真落库并执行
      再传 `auto_rounds=True` 才允许「跑完没达标自动重拆第 2 轮」；不传就停在第一轮
      等人工判断。为什么拆成两个开关：自动重拆会真的再花一轮 token 并自动接受产物，
      属于「烧钱 + 改状态」，必须显式点头（与 `hub_skill_acquire` 的 allow_install 同口径）。

    返回里 `tasks[].difficulty` 是 Leader 自评（1~3，只作选型依据不作判据），
    `tasks[].model` 是**选型意图**；实际用了什么模型要看 `hub_run_status` 的 `model`
    （回包观测值）。两者不同 = 选了没生效，不是 bug 静默。

    要盯进度/取结果用 `hub_run_status(run_id)`。"""
    body = {"goal": goal, "verify_kind": verify_kind,
            "dry_run": not confirm,
            "auto_run": bool(auto_run) and bool(confirm),
            "auto_rounds": bool(auto_rounds) and bool(confirm)}
    if agent:
        body["default_agent"] = agent
    data = _post("/api/tasks/decompose", body, _timeout=120)
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    return json.dumps({
        "run_id": data.get("run_id"),
        "dry_run": data.get("dry_run", False),
        "round": data.get("round"),
        "max_rounds": data.get("max_rounds"),
        "auto_rounds": data.get("auto_rounds"),
        "running": data.get("running"),
        "tasks": [{"id": t.get("id"), "agent": t.get("agent_id"),
                   "difficulty": t.get("difficulty"), "model": t.get("model"),
                   "deps": t.get("deps"), "task": t.get("task")}
                  for t in (data.get("tasks") or [])],
        "note": data.get("note"),
    }, ensure_ascii=False)


@server.tool()
def hub_run_status(run_id: str) -> str:
    """看一条派发 run 的实际状态与**观测到的模型**。

    判读顺序：`run.stop_reason` 先看（`complete` 完工 / `max_rounds` 到轮次上限转人工 /
    `same_failure` 原地打转 / `budget` / `replan_error:…` 重拆失败），再看每个任务的
    `status` + `verdict` + `error`。`model_choice`（意图）与 `model`（回包）不一致
    是「选了没生效」的唯一可查证据，别把它当噪声。"""
    data = _get("/api/tasks/%s" % urllib.parse.quote(run_id))
    if "error" in data:
        return json.dumps(data, ensure_ascii=False)
    tasks = data.get("tasks") or []
    return json.dumps({
        "run_id": data.get("run_id"),
        "goal": data.get("goal"),
        "run": data.get("run"),
        "max_rounds": data.get("max_rounds"),
        "counts": {"total": len(tasks),
                   "success": sum(1 for t in tasks if t.get("status") == "success"),
                   "failed": sum(1 for t in tasks if t.get("status") == "failed"),
                   "other": sum(1 for t in tasks
                                if t.get("status") not in ("success", "failed"))},
        "tasks": [{"id": t.get("task_id"), "status": t.get("status"),
                   "agent": t.get("agent_id"), "verdict": t.get("verdict"),
                   "difficulty": t.get("difficulty"),
                   "model_choice": t.get("model_choice"), "model": t.get("model"),
                   "error": (t.get("error") or "")[:200] or None,
                   "output_head": (t.get("output") or "")[:400] or None}
                  for t in tasks],
    }, ensure_ascii=False)


@server.tool()
def hub_replan(run_id: str, max_rounds: int = 2) -> str:
    """对一条**已收敛**的 run 手动重拆下一轮（与自动重拆同一套闸门）。

    `max_rounds` 只能**收紧**：传 5 也会被压回 2（用户裁定的「最多自动 2 轮」）。
    返回 `gate.kind` 即结论：`go` 已生成下一轮 / `complete` 全绿无需重拆 /
    `max_rounds` 到上限 / `same_failure` 原地打转 / `budget` 预算耗尽。"""
    data = _post("/api/tasks/%s/replan" % urllib.parse.quote(run_id),
                 {"auto_run": True, "max_rounds": int(max_rounds)}, _timeout=120)
    return json.dumps(data, ensure_ascii=False)


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


if os.getenv("HUB_CATALOG", "0") == "1":
    @server.tool()
    def hub_query(q: str, domains: str = "", types: str = "",
                  k: int = 10, since: str = "") -> str:
        """跨 agent/技能/项目等编目域统一检索。只查可重建索引，不改原生数据。
        domains/types 用逗号分隔；since 接受 epoch 秒或 ISO-8601。
        count=0 时必须同时看 degraded 与 backends，区分无命中和索引不可用。"""
        data = _get("/api/catalog/query", q=q, domains=domains, types=types,
                    k=max(1, min(int(k), 100)), since=since)
        return json.dumps(data, ensure_ascii=False)

    @server.tool()
    def hub_get(oid: str = "", domain: str = "", native_key: str = "",
                src_id: str = "") -> str:
        """按 oid 取编目对象详情；也可传 domain+native_key，跨源重名时补 src_id。"""
        data = _get("/api/catalog/get", oid=oid, domain=domain,
                    native_key=native_key, src_id=src_id)
        return json.dumps(data, ensure_ascii=False)

    @server.tool()
    def hub_status(domain: str = "") -> str:
        """查看统一编目各源的扫描时间、滞后、对象数和失败原因；domain 可选。"""
        return json.dumps(_get("/api/catalog/status", domain=domain),
                          ensure_ascii=False)


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


@server.tool()
def hub_summarize(text: str, points: int = 3, style: str = "") -> str:
    """把长文提炼成 N 条要点（2026-10-23）：本机 Ollama，零外网、零 key。

    什么时候用：需要把一篇文档/日志/对话压成可读要点，且**不想把正文发到外部模型**时。
    超过 `SUMMARIZE_MAX_CHARS`（默认 2 万字）会直接报错要求先分段——不会静默截断。
    服务端按 `SUMMARIZE_CHUNK_CHARS`（默认 1600 字）分块，再归并；同一篇正文命中缓存
    时 `cache.hit=true`（10~30s 的 CPU 推理不必重跑）。

    返回 `points`（列表）、`text`（拼好的要点）、`chunks`、`model`、`elapsed_s`、`cache`。
    失败是 200 + `error`（正文过长/Ollama 不在），**不会返回空列表冒充成功**。
    """
    body = {"text": text, "points": int(points or 3), "style": style or ""}
    return json.dumps(_post("/api/summarize", body, _timeout=300), ensure_ascii=False)


def build_app():
    """返回可挂载到 FastAPI 的 Starlette 子应用。"""
    return server.streamable_http_app()
