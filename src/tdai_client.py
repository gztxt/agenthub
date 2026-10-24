"""tdai_client.py — TDAI 主记忆服务的唯一出口（v0.13.16 / PT-20260924-08 主线 T0-1）

为什么要有这个模块
------------------
本机记忆的**权威副本**是 TDAI（:8420，实测 L1 233 条 / L0 5967 条 / L2 7 场景档 / L3 persona），
不是 hub `data/agents.db` 里那 4 行 `memories`。0924 方案文档想再建一套 `knowledge.db`，
那会是第三份记忆副本 —— 违反「唯一权威副本不让渡」。所以 hub 不存记忆，只经本模块**取**。

为什么不是"把 memory.py 里那段 aiohttp 改改就好"
----------------------------------------------
`memory.py:96-110` 的原实现有**四重错**，且错得静默：
  1. 路径 `/memory/search` 不在 TDAI 的 routeTable 里（实测 HTTP 404）；
  2. 方法应为 POST，原码用 GET；
  3. 缺 `Authorization: Bearer <apiKey>`（server.ts:1084 verifyAuth Layer 1）；
  4. 缺 `x-tdai-service-id`（Layer 2，v2/v3 数据面必需）；
  5. 最致命：`except Exception: pass` 把以上全部吞掉，响应里连一个 error 字段都没有
     ⇒ 「HTTP 200、11ms、全指标绿，而权威库一次都没被查到」。这与 ccpocket-bridge
     「服务 active、端口在听、/health ok，但 Claude 会话起不来，静默 21 天」同形态。
故本模块的第一设计目标不是"能查"，而是**查不到时必须说得出为什么**（用户基线：
「内嵌重试组件须心跳+超时+显式失败」）。

契约来源（全部源码取证，不凭记忆推断）
------------------------------------
  路由表        MemoryCore/src/gateway/v2-router.ts:410 DATAPLANE_HANDLERS
  入参 schema   MemoryCore/src/gateway/generated/schemas.ts:188 atomicSearchRequestSchema
               → {query:1..2048, limit:1..100 默认 5, type?, time_start?, time_end?} + 4 个可选 ID 字段
  返回信封      {code, message, request_id, data:{items:[...atomicDetail, score]}}；code==0 才算成功
  计数          仅 v3 提供（/v3/atomic/count、/v3/conversation/count）
  鉴权          server.ts:1084 verifyAuth（Bearer）+ v2 路由内 handleV2Route 的 service-id 层
  /health       免鉴权（实测 200）

凭据落位
--------
优先级：环境变量 `TDAI_API_KEY`/`TDAI_SERVICE_ID`/`TDAI_URL` → `~/.pi/agent/memory-tencentdb.json`
的 `server.{apiKey,serviceId,endpoint}` → 默认 127.0.0.1:8420。
（读第三方配置文件这一手照抄 `config.py:55-68` 的 `_resolve_ccr_openai_key` 既有范式。）
**apiKey 只在请求头里存在，绝不进日志、绝不进异常 message、绝不出现在本模块任何返回值里。**
"""
from __future__ import annotations

import asyncio
import copy
import json
import os
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

#: 单次探测预算上限。09-23 01:06 红线：探活最多 2 次即停，故本模块**不内建重试**，
#: 一次调用一次请求，失败即结构化返回，由调用方决定是否降级。
DEFAULT_TIMEOUT_S = float(os.getenv("TDAI_TIMEOUT", "5"))
CREDS_PATH = Path.home() / ".pi" / "agent" / "memory-tencentdb.json"

# 后端标识（进 /health 与响应的 backends[] 用，便于前端一眼看出哪路死了）
BACKEND = "tdai"

# ── 查询缓存（PT-20261024-08 C3）──────────────────────────────────────
#:
#: 为什么要：**实测**（2026-10-24）`/api/memory/search?q=备份` 的 6 路里，
#: `tdai_l1` 单独占 **804ms**，总 884ms —— 即 TDAI 一路占 91%。
#: 原因是每次查询都新建一个 `aiohttp.ClientSession`（无连接复用），
#: 且 TDAI 的 `/v2/atomic/search` 本身要走一次语义检索。
#: ⇒ 对**同 query 的重复检索**加短 TTL 缓存：接单前置（intake）与随后的
#:   追问常常是同一个 query（`intake` → `hub_memory_search` → 技能命中后回查），
#:   缓存命中后 804ms → 近 0。
#:
#: **只缓存成功结果**：失败不缓存（否则一次网络抖动会把「查不到」缓存住 TTL 秒，
#:   制造「明明有却查不到」的假空）。
#: **不缓存含凭据/错误的内容**：错误响应里带 `error` 文本，一律不缓存。
#:
#: TTL 默认 60s：够覆盖「同一次接单流程内的重复查询」，
#: 又不至于让记忆库的变化长时间不可见。
QUERY_CACHE_TTL_S = float(os.getenv("TDAI_QUERY_CACHE_TTL", "60"))

#: 查询缓存条目上限（防无界增长）。按 LRU 淘汰。
QUERY_CACHE_MAX = 256

#: {(path, body 规范化串): (monotonic_ts, 结果dict)}
_query_cache: Dict[str, Any] = {}
_query_cache_order: List[str] = []


def _cache_key(path: str, body: Dict[str, Any]) -> str:
    return path + "|" + json.dumps(body, sort_keys=True, ensure_ascii=False)


def _cache_get(path: str, body: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    key = _cache_key(path, body)
    hit = _query_cache.get(key)
    if hit is None:
        return None
    ts, data = hit
    if (time.monotonic() - ts) >= QUERY_CACHE_TTL_S:
        _query_cache.pop(key, None)
        return None
    # 命中也要标 cached（口径诚实：调用方要能看出「这次没真查」）
    # **深拷贝**：浅拷贝下 items 列表与缓存共享，调用方改 items[0]
    # 会污染后续每一次命中（缓存是进程级全局的，污染面是所有人）。
    out = copy.deepcopy(data)
    out["cached"] = True
    return out


def _cache_put(path: str, body: Dict[str, Any], data: Dict[str, Any]) -> None:
    if not data.get("ok"):
        return                      # 失败不缓存（见上方注释）
    key = _cache_key(path, body)
    if key not in _query_cache:
        _query_cache_order.append(key)
    _query_cache[key] = (time.monotonic(), copy.deepcopy(data))
    # LRU 淘汰
    while len(_query_cache_order) > QUERY_CACHE_MAX:
        old = _query_cache_order.pop(0)
        _query_cache.pop(old, None)


def cache_stats() -> Dict[str, Any]:
    """查询缓存状态（进 /health 与调试用）。"""
    now = time.monotonic()
    live = sum(1 for ts, _ in _query_cache.values() if (now - ts) < QUERY_CACHE_TTL_S)
    return {"entries": len(_query_cache), "live": live,
            "ttl_s": QUERY_CACHE_TTL_S, "max": QUERY_CACHE_MAX}


def clear_query_cache() -> None:
    """清空查询缓存（记忆库写入后、或改了 TTL 后调用）。"""
    _query_cache.clear()
    _query_cache_order.clear()


# ── 配置解析 ──────────────────────────────────────────────────────────

def resolve_creds() -> Dict[str, Any]:
    """返回 {endpoint, api_key, service_id, source}。**永不返回 None**，缺 key 也照实报。

    `source` 用来告诉调用方凭据是从哪来的（env / file / default），
    出问题时一眼能区分「没配 key」和「key 配了但错」。
    """
    endpoint = (os.getenv("TDAI_URL") or "").strip()
    api_key = (os.getenv("TDAI_API_KEY") or "").strip()
    service_id = (os.getenv("TDAI_SERVICE_ID") or "").strip()
    source = "env" if (api_key or endpoint) else "none"

    if not api_key or not service_id or not endpoint:
        try:
            raw = json.loads(CREDS_PATH.read_text(encoding="utf-8"))
            srv = raw.get("server", {}) if isinstance(raw, dict) else {}
            endpoint = endpoint or (srv.get("endpoint") or "")
            api_key = api_key or (srv.get("apiKey") or "")
            service_id = service_id or (srv.get("serviceId") or "")
            if srv:
                source = "env+file" if source == "env" else "file"
        except FileNotFoundError:
            pass
        except (OSError, ValueError) as e:  # 权限/JSON 坏了也要说得出原因，不能装作没这个文件
            return {"endpoint": endpoint or "http://127.0.0.1:8420", "api_key": api_key,
                    "service_id": service_id, "source": source,
                    "creds_error": f"{type(e).__name__}: {e}"}

    return {"endpoint": (endpoint or "http://127.0.0.1:8420").rstrip("/"),
            "api_key": api_key, "service_id": service_id, "source": source}


# ── 传输层 ────────────────────────────────────────────────────────────

#: 上游把凭据回显在错误文本里是**真实网关的常见行为**（如 "invalid key sk-xxx"）。
#: 本模块的 error 会外溢到 /health 与 /api/memory/search 响应，一旦不脱敏就等于把
#: 权威库的钥匙发给前端。规则与 writeauth.decide() 同源：拒绝原因里绝不回显凭据。
_KEY_PATTERNS = (
    r"sk-[A-Za-z0-9_-]{8,}",
    r"Bearer\s+[A-Za-z0-9._~+/-]{8,}",
)


def scrub(text: str, api_key: str = "") -> str:
    """把任何疑似凭据的子串换成 <redacted>。刻意保守：宁可多打码一个词，不可漏一个 key。

    为什么公开（无下划线）：调用方（memory.py 的 gather 兵底分支）也要能把上游异常文本
    洗一遍再拼进 `backends[].error`——09-24 独立复核指出那里漏了一道。脱敏只能有一个实现。
    口径与 `writeauth.decide()` 同源：拒绝原因里绥不绥绡不凭据。
    """
    s = str(text or "")
    if api_key and len(api_key) >= 6 and api_key in s:
        s = s.replace(api_key, "<redacted>")
    import re
    for pat in _KEY_PATTERNS:
        s = re.sub(pat, "<redacted>", s)
    return s


_scrub = scrub          # 仓内既有调用点写法不变；新代码请用 scrub()


def _fail(error: str, http: Optional[int] = None, ms: float = 0.0, **extra) -> Dict[str, Any]:
    out = {"ok": False, "backend": BACKEND, "items": [], "count": 0,
           "error": error, "http": http, "ms": round(ms, 1)}
    out.update(extra)
    return out


async def _post(path: str, body: Dict[str, Any],
                timeout_s: float = DEFAULT_TIMEOUT_S,
                use_cache: bool = False) -> Dict[str, Any]:
    """POST 一个 v2/v3 数据面请求，拆信封，把一切失败都变成结构化返回。

    刻意不抛异常：调用方（memory.py / kb）要在 200ms 预算内拿到「有/没有/为什么没有」，
    而不是被一个 traceback 打断对话链路。但**绝不静默** —— error 字段必填。

    `use_cache=True` 时走**查询缓存**（PT-20261024-08 C3）：
    实测 `tdai_l1` 单路 804ms 占检索总耗时 91% ⇒ 同 query 的重复检索（接单前置
    → 追问 → 技能命中后回查）应当命中缓存。
    ⚠ 只缓存**成功**结果；失败不缓存（否则一次网络抖动会缓存出「查不到」）。
    """
    t0 = time.monotonic()
    if use_cache:
        hit = _cache_get(path, body)
        if hit is not None:
            hit["ms"] = round((time.monotonic() - t0) * 1000, 1)
            return hit
    c = resolve_creds()
    if not c["api_key"]:
        # 这是最容易被误判成「TDAI 挂了」的一种：服务好好的，只是本进程没拿到 key
        return _fail("凭据缺失：环境变量 TDAI_API_KEY 未设且 "
                     f"{CREDS_PATH} 不可读（creds_source={c['source']}）",
                     None, (time.monotonic() - t0) * 1000,
                     creds_source=c["source"], creds_error=c.get("creds_error"))
    try:
        import aiohttp
    except ImportError as e:
        return _fail(f"依赖缺失 aiohttp：{e}", None, (time.monotonic() - t0) * 1000)

    url = f"{c['endpoint']}{path}"
    headers = {"Authorization": f"Bearer {c['api_key']}",
               "Content-Type": "application/json"}
    if c["service_id"]:
        headers["x-tdai-service-id"] = c["service_id"]
    else:
        return _fail("凭据不完整：service_id 为空（v2/v3 数据面必需）", None,
                     (time.monotonic() - t0) * 1000, creds_source=c["source"])

    try:
        to = aiohttp.ClientTimeout(total=timeout_s)
        async with aiohttp.ClientSession(timeout=to) as s:
            async with s.post(url, json=body, headers=headers) as resp:
                ms = (time.monotonic() - t0) * 1000
                if resp.status != 200:
                    # 先脱敏、后截断。反过来的时候 key 恰好跨在截断边界上，
                    # 精确替换与正则**都会失配**，半个 key 就随响应出去了（09-24 独立复核）。
                    txt = _scrub(await resp.text(), c["api_key"])[:200]
                    return _fail(f"HTTP {resp.status} {txt}", resp.status, ms,
                                 creds_source=c["source"])
                try:
                    env = await resp.json(content_type=None)
                except (ValueError, aiohttp.ClientError) as e:
                    return _fail(f"响应非 JSON：{type(e).__name__}", 200, ms)
    except Exception as e:  # noqa: BLE001 — 先脱敏再判类，异常文本可能带 URL/头
        ms = (time.monotonic() - t0) * 1000
        emsg = _scrub(str(e)[:160], c["api_key"])
        ename = type(e).__name__
        kind = "超时" if ename in ("TimeoutError", "ServerTimeoutError") else \
            "连接失败" if "ClientConnector" in ename else "异常"
        return _fail(f"{kind}：{ename}: {emsg}", None, ms, creds_source=c["source"])

    # 信封语义：HTTP 200 不等于成功，必须看 code（实测 400 也是 HTTP 400，但 code 才是契约）
    if not isinstance(env, dict):
        return _fail("信封非对象", 200, ms)
    code = env.get("code")
    if code != 0:
        return _fail("业务码 code=%s message=%s" % (code,
                     _scrub(env.get("message"), c["api_key"])[:160]),
                     200, ms, request_id=env.get("request_id"))
    data = env.get("data") or {}
    items = data.get("items") if isinstance(data, dict) else None
    if items is None:
        items = data.get("results") if isinstance(data, dict) else None
    out = {"ok": True, "backend": BACKEND, "items": items or [],
           "data": data if isinstance(data, dict) else {},
           "count": len(items or []) if isinstance(items, list) else int(data.get("total") or 0),
           "total": data.get("total") if isinstance(data, dict) else None,
           "error": None, "http": 200, "ms": round(ms, 1),
           "request_id": env.get("request_id"), "creds_source": c["source"]}
    if use_cache:
        _cache_put(path, body, out)
    return out


def _norm(hit: Dict[str, Any], source: str = "tdai") -> Dict[str, Any]:
    """把 atomicDetail/conversationDetail 收敛成 hub 内部统一形状。

    TDAI 一期内外 6 字段 + 4 可选 ID + score，字段名在两处不完全一致（id vs record_id），
    故在此兜住，不让上游改字段名变成 hub 的隐性崩溃。

    `source` **必须逐路不同**（tdai_l1 / tdai_l0）：两路 id 不在同一命名空间，若都叫
    "tdai"，融合层去重键 `tdai|<id>` 撞 id 时会把两条不同记忆**合并加分**——
    那比不融合更坑，因为它偽装成「两路都证实了这条」。09-24 独立复核发现。
    """
    return {
        "id": hit.get("id") or hit.get("record_id") or hit.get("message_id"),
        "content": hit.get("content") or hit.get("text") or "",
        "type": hit.get("type") or "",
        "score": hit.get("score"),
        "scene": hit.get("scene_name") or hit.get("scene") or "",
        "created_at": hit.get("created_at") or hit.get("timestamp_start") or "",
        "source": source,
    }


# ── 对外能力 ──────────────────────────────────────────────────────────

async def search_memories(q: str, limit: int = 10, mem_type: Optional[str] = None,
                          timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """L1 结构化记忆语义检索。query 长度契约 1..2048，超限直接本地拒（省一次往返）。"""
    q = (q or "").strip()
    if not q:
        return _fail("query 为空")
    if len(q) > 2048:
        return _fail(f"query 超长（{len(q)}>2048，TDAI 契约上限）")
    body: Dict[str, Any] = {"query": q, "limit": max(1, min(100, int(limit)))}
    if mem_type:
        body["type"] = mem_type
    r = await _post("/v2/atomic/search", body, timeout_s, use_cache=True)
    if r["ok"]:
        r["items"] = [_norm(h, "tdai_l1") for h in r["items"]]
    return r


async def search_conversations(q: str, limit: int = 5,
                               timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """L0 原始会话检索 —— episodic 记忆的正确来源（勿在 hub 侧重建会话索引）。"""
    q = (q or "").strip()
    if not q:
        return _fail("query 为空")
    r = await _post("/v2/conversation/search",
                    {"query": q, "limit": max(1, min(100, int(limit)))}, timeout_s, use_cache=True)
    if r["ok"]:
        r["items"] = [_norm(h, "tdai_l0") for h in r["items"]]
    return r


async def core_read(timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """L3 长期画像（persona.md）。实测契约：`POST /v2/core/read {}` → `data.content`。
    hub 自己的 `memory_docs` 里 **L3 是空行**，所以开局包的「长期 Profile」段只能从这里来。"""
    r = await _post("/v2/core/read", {}, timeout_s, use_cache=True)
    if r["ok"]:
        r["content"] = (r.get("data") or {}).get("content") or ""
    return r


async def scenario_ls(path_prefix: str = "",
                      timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """L2 场景档清单（实测：`data.entries[].{path,summary}` + total）。"""
    body: Dict[str, Any] = {}
    if path_prefix:
        body["path_prefix"] = path_prefix
    r = await _post("/v2/scenario/ls", body, timeout_s)
    if r["ok"]:
        d = r.get("data") or {}
        r["entries"] = d.get("entries") or []
        r["count"] = len(r["entries"])
    return r


async def stats() -> Dict[str, Any]:
    """权威库规模（/v3/*/count）。用于 /health 与资产面板显示「本 hub 之外还有多少条」。

    两次计数**并发发**，不串行：它坐在 backend_status 的探针链路里，串行会把
    「TDAI 已死 → 仍报 ok」的假绿窗口整整拉长一倍（09-24 独立复核）。
    """
    a, c = await asyncio.gather(_post("/v3/atomic/count", {}),
                                _post("/v3/conversation/count", {}))
    return {"ok": a["ok"] and c["ok"], "backend": BACKEND,
            "l1_total": (a.get("data") or {}).get("total") if a["ok"] else None,
            "l0_total": (c.get("data") or {}).get("total") if c["ok"] else None,
            "error": None if (a["ok"] and c["ok"]) else (a.get("error") or c.get("error")),
            "ms": round(max(a["ms"], c["ms"]), 1)}


async def skill_list(limit: int = 100) -> Dict[str, Any]:
    """技能注册表清单。TDAI 侧 /v3/skill/* 已 live（实测 list→200），
    hub **不再自建 skill 表**，只做只读代理，避免造出第 5 份无主副本。"""
    r = await _post("/v3/skill/list", {"limit": max(1, min(100, int(limit)))})
    if r["ok"]:
        r["items"] = [{"id": h.get("skill_id") or h.get("id"), "name": h.get("name"),
                       "description": h.get("description") or "",
                       "version": h.get("version"), "owner_agent_id": h.get("owner_agent_id"),
                       "status": h.get("status"), "source": BACKEND}
                      for h in r["items"]]
    return r


async def reachable(timeout_s: float = 2.0) -> Dict[str, Any]:
    """/health 免鉴权，故用它区分「服务没起」和「我没凭据」——这两件事被混为一谈时，
    排障方向会整个反掉（0924 文档原实现就是把 404 当成了服务不可用）。"""
    t0 = time.monotonic()
    c = resolve_creds()
    try:
        import aiohttp
        async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=timeout_s)) as s:
            async with s.get(f"{c['endpoint']}/health") as resp:
                body = await resp.text()
                ms = (time.monotonic() - t0) * 1000
                return {"ok": resp.status == 200, "http": resp.status, "ms": round(ms, 1),
                        "error": None if resp.status == 200 else f"HTTP {resp.status}",
                        "raw": _scrub(body[:240], c.get("api_key", ""))}
    except Exception as e:  # noqa: BLE001
        ms = (time.monotonic() - t0) * 1000
        return {"ok": False, "http": None, "ms": round(ms, 1),
                "error": _scrub(f"{type(e).__name__}: {str(e)[:160]}", c.get("api_key", ""))}


def creds_summary() -> Dict[str, Any]:
    """给 /health 用的**无密钥**凭据摘要。刻意不回显 api_key 本身，只回有没有、多长、哪来的。"""
    c = resolve_creds()
    return {"endpoint": c["endpoint"], "has_key": bool(c["api_key"]),
            "key_len": len(c["api_key"]) if c["api_key"] else 0,
            "has_service_id": bool(c["service_id"]), "creds_source": c["source"],
            "creds_error": c.get("creds_error")}


# ── 后端健康缓存（stale-while-revalidate）───────────────────────────
# 为什么 /health 里**绝对不能等网络**：hub 自己被 watchdog / vitals 以短超时探测，
# 一次同步探活把 /health 拖慢 2s，就会变成「agent-hub 健康检查失败」的**假告警**；
# 而记忆后端挂不挂本来就不该让 hub 本体变红（照 `code_stale` 只兑情报、不改 status 的既有口径）。
#
# ⚠ TTL 默认值是对 09-23「探活最多 2 次、禁止反复频繁探测」红线的正面回应：
# /health 会被 hub 自己的 watchdog 高频调，若 TTL 很短，本模块就会把对 TDAI 的
# 探测**隐式提速到 watchdog 频率**——那不是健康检查，那是拿监视器名义造第二个轮询源。
# 故默认 **300s（低频复检）**，要更勤就改 env TDAI_PROBE_TTL；要关掉周期复检就设很天。
PROBE_TTL_S = float(os.getenv("TDAI_PROBE_TTL", "300"))
_PROBE: Dict[str, Any] = {"ts": 0.0, "data": None, "task": None, "count": 0}
# ^ ts 一律用 **monotonic**。用 time.time() 时，NAS 上 NTP 向前拨回会让
#   `age = now - ts` 变负 ⇒ `age > TTL` 永假 ⇒ 缓存**永不过期**，
#   /health 会无限期地报一个陈旧的 ok——那正是本批次要消除的假绿形态。09-24 独立复核发现。


async def _probe_once() -> Dict[str, Any]:
    h, s = await asyncio.gather(reachable(timeout_s=3.0), stats(), return_exceptions=True)
    if isinstance(h, Exception):
        h = {"ok": False, "http": None, "ms": None, "error": f"{type(h).__name__}"}
    if isinstance(s, Exception):
        s = {"ok": False, "error": f"{type(s).__name__}", "ms": None}
    if h.get("ok") and not s.get("ok"):
        state = "degraded"      # 服务活着，但我查不动它（典型：凭据不对 / 路由改了）
    elif h.get("ok"):
        state = "ok"
    else:
        state = "down"
    return {"state": state, "http": h.get("http"), "probe_ms": h.get("ms"),
            "error": s.get("error") if state == "degraded" else h.get("error"),
            "l1_total": s.get("l1_total"), "l0_total": s.get("l0_total"),
            "stats_ms": s.get("ms")}


def _schedule_probe() -> None:
    """后台发一次探测。没有运行中的 loop（导入期、单测）则静默不探，
    下次 backend_status() 再试——但不能抛，/health 不能因为记忆后端而 500。"""
    if _PROBE["task"] is not None and not _PROBE["task"].done():
        return
    try:
        loop = asyncio.get_running_loop()
    except RuntimeError:
        return

    async def _run():
        try:
            _PROBE["data"] = await _probe_once()
            _PROBE["ts"] = time.monotonic()
            _PROBE["count"] += 1
        except Exception as e:  # noqa: BLE001 — 缓存型探活不得影响宿主，但必须留痕
            _PROBE["data"] = {"state": "down", "error": f"探针自身异常 {type(e).__name__}"}
            _PROBE["ts"] = time.monotonic()

    _PROBE["task"] = loop.create_task(_run())


def backend_status() -> Dict[str, Any]:
    """**纯读缓存、零阻塞**，给 /health 用。首次调用回 state=unknown 并触发后台首探。

    为什么除了 state 还必须给 `stale`：state 只是「上一探的结果」，TTL 内的故障只能被 age
    反映。只读 state 的自动化监控会拿到一个**看上去绿实则陈旧**的结论（最长≈TTL），
    所以下游可以直接用 `state=="ok" and not stale` 作判据，不必自己理解 age。
    """
    d = _PROBE["data"]
    age = round(time.monotonic() - _PROBE["ts"], 1) if _PROBE["ts"] else None
    if d is None:
        _schedule_probe()
        return {"state": "unknown", "stale": True, "probes": 0, "age_s": None,
                "ttl_s": PROBE_TTL_S, "creds": creds_summary(),
                "query_cache": cache_stats()}
    stale = bool(age is not None and age > PROBE_TTL_S)
    if stale:
        _schedule_probe()       # 先回旧值，刷新在后台——这就是 stale-while-revalidate
    return {**d, "age_s": age, "stale": stale, "probes": _PROBE["count"],
            "ttl_s": PROBE_TTL_S, "creds": creds_summary(),
            "query_cache": cache_stats()}
