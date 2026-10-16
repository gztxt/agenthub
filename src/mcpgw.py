"""MCP 聚合网关（部署方案 S4：统一工具路由 + ACL + 限流）

用官方 mcp Python SDK 做客户端（不手搓协议）。聚合本机 MCP servers，
Agent 只连 hub 一个端点即可看到全部工具；敏感凭证留在 server 配置内。
- 每次调用短连接（stdio 子进程按需拉起，60s 上限）；tools 列表缓存 60s
- ACL：**deny 优先**且与规则插入顺序无关（判定口径唯一真相在 `_resolve_acl`）。
  只要该 agent 命中任意规则行（**含 `*` 通配行**）即进入白名单语义；一行都不命中才默认放行。
  ⚠ 旧措辞「一旦有 allow 规则则白名单、无规则默认放行」与实际不符：库里存在 `*` 的 deny 行时，
  所有 agent（含匿名）都已是白名单语义。09-24 按生产 4 行实测判决表核对后改成现在的写法。
- 限流：每主体滑动窗口计数（MCP_RATE_PER_MIN，默认 60/min）
"""
import asyncio
import fnmatch
import json
import os
import time
from collections import defaultdict, deque
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from contextlib import AsyncExitStack
from fastapi import APIRouter, HTTPException, Request
from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client
import outbound    # P2 C5：出站地址准入（与 ghsettings 共用同一份判据）

#: MCP server 是否允许指向本机/内网。**默认允许** —— MCP server 本来就常在 127.0.0.1
#: （本机 stdio/http 桥），一刀切会把最正常的用法挡死（把安全做成可用性事故）。
#: 但**云元数据端点任何场景都拒**（`outbound` 里硬编码，不受本开关影响）。
MCP_ALLOW_PRIVATE = os.getenv("HUB_MCP_ALLOW_PRIVATE", "1").strip().lower() not in (
    "0", "false", "no", "off")
from mcp.client.streamable_http import streamable_http_client
from pydantic import BaseModel

import db
import config
import writeauth

router = APIRouter()

RATE_PER_MIN = int(os.getenv("MCP_RATE_PER_MIN", "60"))
CALL_TIMEOUT_S = int(os.getenv("MCP_CALL_TIMEOUT_S", "60"))
LIST_TTL_S = 60
DOC_CHARS_MAX = 160   # description 展示截断；**必须与 description_truncated 同时出现**

SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_servers (
    id TEXT PRIMARY KEY, name TEXT NOT NULL,
    transport TEXT NOT NULL DEFAULT 'stdio',   -- stdio|http
    command TEXT, args TEXT DEFAULT '[]', env TEXT DEFAULT '{}',
    url TEXT, enabled INTEGER DEFAULT 1,
    description TEXT DEFAULT '',
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS mcp_acl (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    agent_id TEXT NOT NULL,            -- 'manager' / 'jcode' / '*'
    server_id TEXT,                    -- NULL = 任意 server
    tool_pattern TEXT NOT NULL,        -- fnmatch，如 'read_*' 或 '*'
    allow INTEGER NOT NULL DEFAULT 1
);
"""

_tool_cache: Dict[str, Tuple[float, list]] = {}
_rate: Dict[str, deque] = defaultdict(deque)


def ensure_schema():
    db.execute_script(SCHEMA)


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _check_rate(subject: str) -> None:
    window = _rate[subject]
    cut = time.monotonic() - 60
    while window and window[0] < cut:
        window.popleft()
    if len(window) >= RATE_PER_MIN:
        raise HTTPException(429, f"MCP 限流：{subject} 超过 {RATE_PER_MIN}/min")
    window.append(time.monotonic())


def _resolve_acl(rows: list, server_id: str, tool: str) -> Tuple[bool, str, List[int]]:
    """ACL 判定的**唯一真相**（纯函数，不碰 db，便于 L0 逐条断言）。

    入参 rows 必须是已经按 agent 筛过的规则行（含 `*` 通配行）。
    返回 (allowed, 人类可读理由, 决定性规则 id 列表)。

    判定顺序（09-24 定案，**刻意做成与插入顺序无关**）：
      1. 无一行覆盖本 server/tool → 若该 agent 完全没有规则行：放行（"首次接入零摩擦"）；
         若有规则但不覆盖：拒（白名单语义，未覆盖即拒）。
      2. 覆盖行里出现任何 `allow=0` → **拒**（deny 优先）。
      3. 否则 → 放行。

    为什么不再"取第一条匹配行定生死"：`mcp_acl` 除主键外零索引，今天靠 rowid 序看着稳定，
    一旦有人给 `agent_id` 建索引，查询计划从 `SCAN` 翻成 `SEARCH USING INDEX`，
    同一份数据的判定结果就会**翻面**（deny 抢先 = 原本放行的 agent 直接 403）。
    那是"优先级 = 插入顺序"的隐藏语义，实测复现过，故改为显式 deny 优先。
    """
    covering = [r for r in rows
                if (not r["server_id"] or r["server_id"] == server_id)
                and fnmatch.fnmatch(tool, r["tool_pattern"] or "")]
    if not covering:
        if not rows:
            return True, "无规则行：默认放行（首次接入零摩擦）", []
        return False, "白名单未覆盖该工具", []
    denied = [int(r["id"]) for r in covering if not r["allow"]]
    if denied:
        return False, "命中显式 deny 规则", denied
    return True, "命中 allow 规则且无 deny 覆盖", [int(r["id"]) for r in covering]


def _acl_rows(agent_id: str) -> list:
    # ORDER BY id 只为让**日志与闸门**可复现；判定本身已不依赖顺序（见 _resolve_acl）。
    return db.query("SELECT * FROM mcp_acl WHERE agent_id IN (?, '*') ORDER BY id",
                    (agent_id,))


def _acl_check(agent_id: Optional[str], server_id: str, tool: str) -> None:
    aid = (agent_id or "anon").strip() or "anon"   # 缺省身份折算成 anon，受 `*` 规则约束
    # 改前：`if not agent_id: return` —— agent_id 来自请求体自报，留空即跳过整套 ACL；
    # 那 4 条规则（guest 全拒 / * 对 ekko 全拒）看起来"已配置"，实际全是装饰。
    rows = _acl_rows(aid)
    allowed, reason, by = _resolve_acl(rows, server_id, tool)
    if allowed:
        return
    raise HTTPException(403, f"ACL 拒绝 {aid} 调用 {server_id}/{tool}：{reason}"
                        + (f"（规则 {by}）" if by else ""))


async def _session_for(server: dict) -> Tuple[AsyncExitStack, ClientSession]:
    stack = AsyncExitStack()
    if server["transport"] == "http":
        # mcp SDK 各版本返回 2 或 3 元组，取前两个即可（旧代码按 3 元组解包，在 2.x 下会 ValueError）
        _tup = await stack.enter_async_context(
            streamable_http_client(server["url"]))
        read, write = _tup[0], _tup[1]
    else:
        params = StdioServerParameters(
            command=server["command"],
            args=json.loads(server["args"] or "[]"),
            env={k: str(v) for k, v in json.loads(server["env"] or "{}").items()} or None)
        read, write = await stack.enter_async_context(stdio_client(params))
    session = await stack.enter_async_context(ClientSession(read, write))
    await session.initialize()
    return stack, session


class ServerIn(BaseModel):
    name: str
    transport: str = "stdio"
    command: Optional[str] = None
    args: Optional[list] = None
    env: Optional[dict] = None
    url: Optional[str] = None
    description: str = ""


@router.post("/mcp/servers")
async def add_server(body: ServerIn, request: Request):
    if body.transport not in ("stdio", "http"):
        raise HTTPException(400, "transport 仅支持 stdio|http")
    if body.transport == "stdio" and not body.command:
        raise HTTPException(400, "stdio 需要 command")
    if body.transport == "http" and not body.url:
        raise HTTPException(400, "http 需要 url")
    # P2 C5：出站准入。注册处拦一次（挡住直白的内网/元数据写法），调用处再拦一次。
    # 两处都拦是因为**注册先于调用**：库里的旧行可能来自本项落地之前。
    if body.url:
        why = outbound.check_url(body.url, allow_private=MCP_ALLOW_PRIVATE)
        if why:
            raise HTTPException(400, "出站准入拒绝：%s" % why)
    sid = f"{int(time.time()*1000):x}"
    now = _now()
    db.execute(
        "INSERT INTO mcp_servers(id,name,transport,command,args,env,url,description,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?)",
        (sid, body.name, body.transport, body.command,
         json.dumps(body.args or []), json.dumps(body.env or {}),
         body.url, body.description, now, now))
    # env 装的是凭据 ⇒ 只记布尔，绝不记值（审计行会成为下一次导出的正文）
    db.log_asset_event("mcp_server", sid, "create", writeauth.actor_of(request),
                       {"name": body.name, "transport": body.transport,
                        "has_env": bool(body.env), "url": body.url})

    return {"id": sid, "status": "added"}


@router.get("/mcp/servers")
async def list_servers():
    rows = db.query("SELECT * FROM mcp_servers ORDER BY created_at")
    for r in rows:
        r["args"] = json.loads(r["args"] or "[]")
        r["env"] = "(redacted)" if r["env"] and json.loads(r["env"]) else "{}"
    return {"servers": rows}


@router.delete("/mcp/servers/{sid}")
async def del_server(sid: str, request: Request):
    if not db.execute("DELETE FROM mcp_servers WHERE id=?", (sid,)):
        raise HTTPException(404, "server not found")
    _tool_cache.clear()
    db.log_asset_event("mcp_server", sid, "delete", writeauth.actor_of(request))
    return {"status": "deleted"}


class ServerProbe(BaseModel):
    command: str
    args: list = []


@router.post("/mcp/servers/probe")
async def probe_server(body: ServerProbe):
    """临时起 stdio server 看它有哪些工具（不落库）——添加前的'预览'语义"""
    fake = {"transport": "stdio", "command": body.command,
            "args": json.dumps(body.args), "env": "{}", "url": None}
    tools = await _list_tools(fake, use_cache=False)
    return {"tools": [{"name": t.name, "description": (t.description or "")[:120]}
                      for t in tools]}


async def _list_tools(server: dict, use_cache: bool = True):
    """列某 server 的工具。`use_cache=False` 给 probe 用——
    probe 是"改完 server 代码后预览"的语义，吃 60s 缓存会**静默给出旧工具表**，
    属于本机最禁的"全绿但信息是旧的"那一族。"""
    key = server["id"] if "id" in server else server["command"]
    hit = _tool_cache.get(key)
    if use_cache and hit and time.monotonic() - hit[0] < LIST_TTL_S:
        return hit[1]
    async with AsyncExitStack() as outer:
        stack, session = await _session_for(server)
        outer.push_async_callback(stack.aclose)
        resp = await asyncio.wait_for(session.list_tools(), timeout=30)
        tools = resp.tools
    if use_cache:
        _tool_cache[server.get("id", key)] = (time.monotonic(), tools)
    return tools


class AclIn(BaseModel):
    agent_id: str
    tool_pattern: str
    server_id: Optional[str] = None
    allow: bool = True


@router.post("/mcp/acl")
async def add_acl(body: AclIn, request: Request):
    aid = None
    if body.server_id:
        rows = db.query("SELECT id FROM mcp_servers WHERE id=? OR name=?",
                        (body.server_id, body.server_id))
        # 空列表时 [0] 抛 IndexError ⇒ 500，且看不出是"传了个不存在的 server"；
        # 语义上就是 404，调用方可据此区分参数错与内部故障。
        if not rows:
            raise HTTPException(404, f"mcp server 不存在: {body.server_id}")
        aid = rows[0]["id"]
    db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow) VALUES(?,?,?,?)",
               (body.agent_id, aid, body.tool_pattern, 1 if body.allow else 0))
    db.log_asset_event("mcp_acl", str(aid or "*"), "bind", writeauth.actor_of(request),
                       {"agent_id": body.agent_id, "tool_pattern": body.tool_pattern,
                        "allow": bool(body.allow)})
    return {"status": "added"}


@router.get("/mcp/acl")
async def list_acl():
    return {"rules": db.query("SELECT * FROM mcp_acl ORDER BY id")}


@router.delete("/mcp/acl/{acl_id}")
async def del_acl(acl_id: int, request: Request):
    row = db.query("SELECT agent_id,server_id,tool_pattern,allow FROM mcp_acl WHERE id=?",
                   (acl_id,))
    if not db.execute("DELETE FROM mcp_acl WHERE id=?", (acl_id,)):
        raise HTTPException(404, "rule not found")
    db.log_asset_event("mcp_acl", str(acl_id), "unbind", writeauth.actor_of(request),
                       dict(row[0]) if row else None)
    return {"status": "deleted"}


def _tool_shape(t) -> Dict[str, Any]:
    """把 mcp SDK 的 Tool 对象摊平成门面输出。**必须带 inputSchema**：
    改前只出 4 个键，调用方拿不到参数 schema，而前端是个自由 JSON 文本框——
    等于让 agent 猜参数名（09-24 侦察实测 mcp 2.1.1 的 `Tool` 有 `input_schema`，
    别名 `inputSchema`，缓存里存的就是真 Tool 对象 ⇒ 补 schema 零协议改动）。
    description 仍可截断，但**截断必须留痕**（`description_truncated` + 原长度），
    静默截断会让"这个工具就是这么个用途"的假象进 agent 上下文。"""
    desc = t.description or ""
    d: Dict[str, Any] = {"name": t.name, "description": desc[:DOC_CHARS_MAX]}
    if len(desc) > DOC_CHARS_MAX:
        d["description_truncated"] = True
        d["description_chars"] = len(desc)
    schema = None
    got = None
    try:  # 优先走 pydantic 别名（不同 mcp 版本属性名是 input_schema / inputSchema）
        dumped = t.model_dump(by_alias=True)
        # ⚠ 用「是不是 dict」判定，不用真值判定：无参工具的 inputSchema 就是 {}
        #   （闸门 test_envelope_all_ok_has_empty_note 抓出来的：写 `a or b` 会把
        #    合法的「真的没参数」误报成 schema_missing，白给 agent 一条假警告。）
        if isinstance(dumped.get("inputSchema"), dict):
            got = dumped["inputSchema"]
        elif isinstance(dumped.get("input_schema"), dict):
            got = dumped["input_schema"]
    except Exception:  # noqa: BLE001
        got = (getattr(t, "input_schema", None)
               if isinstance(getattr(t, "input_schema", None), dict) else None)
        if got is None and isinstance(getattr(t, "inputSchema", None), dict):
            got = t.inputSchema
    if got is None:
        d["inputSchema"] = {}
        d["schema_missing"] = True   # 拿不到 schema 要**说出来**，不能给个空 dict 当"没有参数"
    else:
        d["inputSchema"] = got
    return d


@router.get("/mcp/tools")
async def aggregated_tools():
    """聚合工具清单（门面信封：逐 server 的 `backends[]/degraded/note/took_ms`）。

    旧键 `tools`/`count`/`errors` 一个不动——前端 `loadMcp()` 与 `pickTool()` 在吃它们。
    """
    t0 = time.monotonic()
    out, errors, backends = [], {}, []
    servers = db.query("SELECT * FROM mcp_servers WHERE enabled=1 ORDER BY created_at")
    for s in servers:
        bs = int((time.monotonic() - t0) * 1000)
        try:
            tools = await _list_tools(dict(s))
            for t in tools:
                item = _tool_shape(t)
                item.update({"server": s["id"], "server_name": s["name"]})
                out.append(item)
            backends.append({"name": s["name"], "ok": True, "count": len(tools),
                             "ms": int((time.monotonic() - t0) * 1000) - bs,
                             "error": None})
        except Exception as e:  # noqa: BLE001  # 单路挂≠整体挂：如实记 degraded，继续跑下一路
            errors[s["name"]] = repr(e)[:200]
            backends.append({"name": s["name"], "ok": False, "count": 0,
                             "ms": int((time.monotonic() - t0) * 1000) - bs,
                             "error": repr(e)[:200]})
    degraded = [b["name"] for b in backends if not b["ok"]]
    resp: Dict[str, Any] = {
        "tools": out, "count": len(out), "errors": errors,
        "backends": backends, "degraded": degraded,
        "took_ms": int((time.monotonic() - t0) * 1000),
    }
    notes = []
    if degraded:
        notes.append(f"{len(degraded)} 个 server 列工具失败，清单不完整：{','.join(degraded)}")
    if any(x.get("description_truncated") for x in out):
        notes.append(f"部分 description 已截到 {DOC_CHARS_MAX} 字（见 description_truncated）")
    if any(x.get("schema_missing") for x in out):
        notes.append("部分工具未给出 inputSchema（上游没返回，参数名需自查）")
    resp["note"] = "；".join(notes)
    return resp


class RegistryIn(BaseModel):
    agent: str = "manager"


@router.get("/mcp/registry")
async def tool_registry(agent: str = ""):
    """工具注册表面门：某个 agent 视角下「有哪些工具 + 能不能调 + 被哪条规则决定」。

    为什么要它：ACL 的判定此前**只在 403 那一刻可见**，运维在前端看到 4 条规则
    却推不出「manager 到底能调哪些」——而 hub 的口径是门面 + ACL + 审计，
    "可解释的生效策略"本就该是门面的一等输出。**不新建表**，纯派生视图。
    """
    t0 = time.monotonic()
    aid = (agent or "anon").strip() or "anon"
    rows = _acl_rows(aid)
    servers = db.query("SELECT * FROM mcp_servers WHERE enabled=1 ORDER BY created_at")
    out, backends = [], []
    for s in servers:
        bs = int((time.monotonic() - t0) * 1000)
        try:
            tools = await _list_tools(dict(s))
            items = []
            for t in tools:
                allowed, reason, by = _resolve_acl(rows, s["id"], t.name)
                items.append({"name": t.name, "allowed": allowed,
                              "reason": reason, "decided_by": by})
            out.append({"server": s["id"], "server_name": s["name"],
                        "enabled": True, "tools": items,
                        "allowed_count": sum(1 for i in items if i["allowed"])})
            backends.append({"name": s["name"], "ok": True, "count": len(items),
                             "ms": int((time.monotonic() - t0) * 1000) - bs, "error": None})
        except Exception as e:  # noqa: BLE001
            out.append({"server": s["id"], "server_name": s["name"], "enabled": True,
                        "tools": [], "allowed_count": 0, "error": repr(e)[:200]})
            backends.append({"name": s["name"], "ok": False, "count": 0,
                             "ms": int((time.monotonic() - t0) * 1000) - bs,
                             "error": repr(e)[:200]})
    return {"agent": aid, "rules": [{"id": int(r["id"]), "agent_id": r["agent_id"],
                                     "server_id": r["server_id"],
                                     "tool_pattern": r["tool_pattern"],
                                     "allow": int(r["allow"])} for r in
                                    db.query("SELECT * FROM mcp_acl ORDER BY id")
                                    if r["agent_id"] in (aid, "*")],
            "servers": out, "count": len(out),
            "backends": backends,
            "degraded": [b["name"] for b in backends if not b["ok"]],
            "semantics": "deny 优先，与插入顺序无关；无规则行则默认放行；有规则未覆盖则拒",
            "note": "生效策略为派生视图，判定与 _acl_check 同源（_resolve_acl 是唯一真相）",
            "took_ms": int((time.monotonic() - t0) * 1000)}


class CallIn(BaseModel):
    server: str            # server id 或 name
    tool: str
    args: dict = {}
    agent_id: Optional[str] = None


@router.post("/mcp/call")
async def mcp_call(body: CallIn):
    # 确保 db 已初始化（manager 调 mcp_call 路径可能绕过 main.py init）
    # ⚠ 09-24 改：以前是无条件 `init_db(config.Config().db_path)`，而 init_db 会把**全局连接重指**。
    #   生产上看似无害（路径相同），但：① 任何 in-process 测试一发 /mcp/call 就会从 tmp 库
    #   切到生产库，后续写入全落在真库上（本机已实际造成一次污染，见 PENDING-TASKS 当日台账）；
    #   ② P0 往 init_db 里加了 wal_checkpoint(TRUNCATE)，本意“只在启动时收一次”，
    #   按次重进＝每次工具调用都与 4 个后台写循环抢 checkpoint。故改成「没连接才初始化」。
    if not db.is_open():
        try:
            db.init_db(config.Config().db_path)
        except Exception:  # noqa: BLE001
            pass
    _check_rate(body.agent_id or "anon")
    rows = db.query("SELECT * FROM mcp_servers WHERE enabled=1 AND (id=? OR name=?)",
                    (body.server, body.server))
    if not rows:
        raise HTTPException(404, f"MCP server 未注册: {body.server}")
    # P2 C5：调用前再过一次出站准入 —— 库里可能有**本项落地之前**注册的行。
    # 零网络开销（纯字符串/ipaddress 判定，不做 DNS）。
    srv = rows[0]
    if srv.get("url"):
        why = outbound.check_url(srv["url"], allow_private=MCP_ALLOW_PRIVATE)
        if why:
            raise HTTPException(400, "该 server 地址被出站准入拒绝：%s" % why)
    server = dict(rows[0])
    _acl_check(body.agent_id, server["id"], body.tool)
    t0 = time.monotonic()
    status = "success"
    try:
        async with AsyncExitStack() as outer:
            stack, session = await _session_for(server)
            outer.push_async_callback(stack.aclose)
            result = await asyncio.wait_for(
                session.call_tool(body.tool, body.args), timeout=CALL_TIMEOUT_S)
        texts = [c.text for c in (result.content or []) if getattr(c, "type", "") == "text"]
        is_err = bool(getattr(result, "is_error", False))
        out = {"ok": not is_err,
               "content": "\n".join(texts)[:20000],
               "is_error": is_err}
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        status = "fail"
        out = {"ok": False, "error": repr(e)[:500]}
    finally:
        db.log_profile_event("mcp_call", f"{server['name']}/{body.tool}", status,
                             int((time.monotonic() - t0) * 1000),
                             detail={"agent_id": body.agent_id})
    if not out.get("ok"):
        raise HTTPException(502, json.dumps(out, ensure_ascii=False))
    return out
