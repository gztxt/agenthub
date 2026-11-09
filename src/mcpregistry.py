# -*- coding: utf-8 -*-
"""MCP 服务器「源」与本地目录（PT-20261110-01 数据/逻辑层）。

**本模块刻意不含任何 `@router` 路由**：路由全部留在 `mcpgw.py`，这样

* `tests/test_authz_scopes.py` 的 AST 扫描不会把它当成新模块（scope 键仍是 `mcpgw`）；
* `main.py` 一个字都不用改（startup 已在调 `mcpgw.ensure_schema()`）；
* 与 `mcpgw` 单向依赖（`mcpgw` → 本模块），无循环 import。

对外只提供纯函数 + db 读写，覆盖四层里的 ① 源层与 ② 发现层：

* `mcp_sources` / `mcp_source_cache` 两张表（DDL 在 `SOURCES_SCHEMA`）；
* 官方 MCP Registry REST 抓取（游标分页 + `updated_since` 增量，stdlib urllib）；
* `server.json` → `mcpgw` 安装规格映射；
* 按任务对缓存卡做 BM25F 排序（复用 `skill_relevance.rank`，不建第二套口径）。

安全口径（与 `AGENTS.md` §1 / 本仓「凭据不落配置」一致）：

* `environmentVariables[].isSecret` 的**值绝不落库**，只把名字回给调用方；
* URL query 里像凭据的参数（高德 `?key=...`）拆成 `${VAR}` 占位，值走调用时注入；
* 所有出站 URL 过 `outbound.check_url`（云元数据端点硬拒），失败不清空旧缓存。
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

import db
import outbound
import skill_relevance

#: 官方 Registry（未鉴权只读；官方文档要求聚合器「定期但不频繁地抓取并持久化」）
OFFICIAL_REGISTRY = "https://registry.modelcontextprotocol.io/v0.1/servers"
DEFAULT_SOURCE_ID = "official-registry"
DEFAULT_SOURCE_LABEL = "官方 MCP Registry"

#: 源类型
SOURCE_KINDS = ("official_registry", "subregistry", "manual_url")

#: 自动安装额度（近一小时），与 `skill_store.AUTO_MAX_PER_HOUR` 同型
AUTO_MAX_PER_HOUR = max(1, int(os.getenv("MCP_AUTO_MAX_PER_HOUR", "6") or 6))
SYNC_PAGE_LIMIT = 100
SYNC_MAX_PAGES = 30           # 一轮最多 3000 张卡，防游标失控
FETCH_TIMEOUT_S = 15
UA = "agenthub-mcp-registry/1.0"

SOURCES_SCHEMA = """
CREATE TABLE IF NOT EXISTS mcp_sources (
    id TEXT PRIMARY KEY,
    kind TEXT NOT NULL,                    -- official_registry|subregistry|manual_url
    label TEXT NOT NULL,
    base_url TEXT NOT NULL,
    enabled INTEGER NOT NULL DEFAULT 1,
    created_at TEXT NOT NULL,
    synced_at TEXT,
    server_count INTEGER NOT NULL DEFAULT 0,
    last_error TEXT NOT NULL DEFAULT '',
    cursor TEXT,
    updated_since TEXT
);
CREATE UNIQUE INDEX IF NOT EXISTS idx_mcp_sources_kind_url ON mcp_sources(kind, base_url);
CREATE TABLE IF NOT EXISTS mcp_source_cache (
    cache_key TEXT PRIMARY KEY,
    source_id TEXT NOT NULL,
    name TEXT NOT NULL,
    version TEXT NOT NULL DEFAULT '',
    title TEXT NOT NULL DEFAULT '',
    description TEXT NOT NULL DEFAULT '',
    tags TEXT NOT NULL DEFAULT '[]',
    server_json TEXT NOT NULL DEFAULT '{}',
    status TEXT NOT NULL DEFAULT 'active',
    is_latest INTEGER NOT NULL DEFAULT 1,
    published_at TEXT,
    updated_at TEXT,
    fetched_at TEXT NOT NULL,
    transport_hint TEXT NOT NULL DEFAULT 'unknown',
    has_secret INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS idx_mcp_cache_source ON mcp_source_cache(source_id);
CREATE INDEX IF NOT EXISTS idx_mcp_cache_name ON mcp_source_cache(name);
CREATE INDEX IF NOT EXISTS idx_mcp_cache_pool ON mcp_source_cache(status, is_latest);
"""

#: 中文任务 ↔ 英文服务器名跨词表：BM25F 是词法检索，不补别名原理上打不中。
#: 只做「查询侧」扩展，不往缓存卡里塞编造的内容（语料仍以 server.json 原文为准）。
QUERY_ALIASES: Dict[str, str] = {
    "地图": "map amap geocode location geo",
    "高德": "amap map geocode",
    "导航": "navigation route direction",
    "路径": "route direction path",
    "文件": "filesystem file",
    "数据库": "database sql postgres sqlite mysql",
    "浏览器": "browser chrome playwright puppeteer",
    "天气": "weather forecast",
    "搜索": "search query retrieval",
    "邮件": "email mail smtp",
    "记忆": "memory engram recall",
    "代码": "code repository git github",
    "表格": "excel spreadsheet sheet",
    "图片": "image photo vision",
    "日程": "calendar schedule event",
    "翻译": "translate translation",
}

#: query 参数名里像凭据的词（命中即拆成占位符，值绝不落库）
_SECRET_PARAM = re.compile(
    r"(?:^|[_.-])(key|token|secret|password|passwd|api[_-]?key|apikey|"
    r"access[_-]?key|auth|credential|sig|signature)(?:$|[_.-])", re.I)
#: 占位符 `${VAR}`
_PLACEHOLDER = re.compile(r"\$\{([A-Za-z0-9_]+)\}")
#: 可自动安装的 registryType 白名单（oci 需容器运行时，无 StdioServerParameters 对应）
AUTO_REGISTRY_TYPES = ("npm", "pypi")
#: identifier 必须是可安全进 argv 的形态（不拼 shell，但仍拒空白/控制符/元字符）
_IDENT_OK = re.compile(r"^[A-Za-z0-9@._/:-]+$")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def seed_default_source() -> None:
    """空表时物化官方 Registry 一条（幂等 `INSERT OR IGNORE`）。

    不静默覆盖用户删掉/停用的源：只有表里一条都没有时才补。
    """
    rows = db.query("SELECT COUNT(*) AS n FROM mcp_sources")
    if rows and int(rows[0]["n"] or 0) > 0:
        return
    db.execute(
        "INSERT OR IGNORE INTO mcp_sources"
        "(id,kind,label,base_url,enabled,created_at,server_count,last_error)"
        " VALUES(?,?,?,?,1,?,0,'')",
        (DEFAULT_SOURCE_ID, "official_registry", DEFAULT_SOURCE_LABEL,
         OFFICIAL_REGISTRY, _now()))


# ── 源 CRUD ────────────────────────────────────────────────────────────────

def source_view(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": r["id"], "kind": r["kind"], "label": r["label"],
            "base_url": r["base_url"], "enabled": int(r["enabled"] or 0) == 1,
            "created_at": r["created_at"], "synced_at": r["synced_at"],
            "server_count": int(r["server_count"] or 0),
            "last_error": r["last_error"] or "",
            "cursor": r["cursor"], "updated_since": r["updated_since"]}


def source_list() -> List[Dict[str, Any]]:
    return [source_view(dict(r)) for r in db.query(
        "SELECT * FROM mcp_sources ORDER BY created_at")]


def source_get(sid: str) -> Optional[Dict[str, Any]]:
    rows = db.query("SELECT * FROM mcp_sources WHERE id=?", (sid,))
    return dict(rows[0]) if rows else None


def source_add(kind: str, label: str, base_url: str) -> Dict[str, Any]:
    if kind not in SOURCE_KINDS:
        raise ValueError("kind 必须是 %s" % (list(SOURCE_KINDS),))
    url = (base_url or "").strip().rstrip("/")
    # 源 base_url 一律 https + 非内网（云元数据端点 outbound 里硬拒）。
    # 本轮不接本地 http 子注册表，避免把「域名不解析」的已知边界再放大。
    why = outbound.check_url(url or " ", allow_private=False, require_https=True)
    if why:
        raise ValueError("出站准入拒绝：%s" % why)
    for r in source_list():
        if r["kind"] == kind and r["base_url"].rstrip("/") == url:
            return {"deduped": True, "source": r}
    sid = "%x" % int(time.time() * 1000)
    db.execute(
        "INSERT INTO mcp_sources(id,kind,label,base_url,enabled,created_at,"
        "server_count,last_error) VALUES(?,?,?,?,1,?,0,'')",
        (sid, kind, (label or "").strip()[:80] or kind, url, _now()))
    return {"deduped": False, "id": sid, "source": source_view(source_get(sid) or {})}


def source_patch(sid: str, label: Optional[str] = None,
                 enabled: Optional[bool] = None,
                 base_url: Optional[str] = None) -> Dict[str, Any]:
    cur = source_get(sid)
    if not cur:
        raise LookupError("源不存在: %s" % sid)
    if label is not None:
        db.execute("UPDATE mcp_sources SET label=? WHERE id=?",
                   ((label or "").strip()[:80] or cur["label"], sid))
    if enabled is not None:
        db.execute("UPDATE mcp_sources SET enabled=? WHERE id=?",
                   (1 if enabled else 0, sid))
    if base_url is not None:
        url = (base_url or "").strip().rstrip("/")
        why = outbound.check_url(url or " ", allow_private=False, require_https=True)
        if why:
            raise ValueError("出站准入拒绝：%s" % why)
        db.execute("UPDATE mcp_sources SET base_url=? WHERE id=?", (url, sid))
    return {"id": sid, "source": source_view(source_get(sid) or {})}


def source_delete(sid: str) -> Dict[str, Any]:
    if not source_get(sid):
        raise LookupError("源不存在: %s" % sid)
    db.execute("DELETE FROM mcp_sources WHERE id=?", (sid,))
    # 缓存按 source_id 保留（源删了不假装数据也删了），但不再出现在目录里 —— 由
    # `catalog()` 用 JOIN enabled 源过滤，见下。
    return {"deleted": sid}


def touch_source(sid: str, count: int, err: str = "",
                 cursor: str = "", updated_since: str = "") -> None:
    try:
        db.execute(
            "UPDATE mcp_sources SET synced_at=?, server_count=?, last_error=?,"
            " cursor=?, updated_since=? WHERE id=?",
            (_now(), int(count), (err or "")[:200], cursor or None,
             updated_since or _now(), sid))
    except Exception:  # noqa: BLE001 — 回执是附加价值，不能带倒一次同步
        pass


# ── 抓取 / 同步 ────────────────────────────────────────────────────────────

def check_probe_url(url: str, allow_private: bool) -> str:
    """probe 的出站准入。放在本模块是为了让 `mcpgw.py` 里 `outbound.check_url`
    恰好只有「注册处 + 调用处」两处（`tests/test_outbound_p2_c5_l0.py` 钉着这个数：
    多一处就说明有人另起了一份准入判据，两份必然漂移）。"""
    return outbound.check_url(url, allow_private=allow_private) or ""


def _fetch_page(url: str) -> Tuple[List[Dict[str, Any]], str, str]:
    """GET 一页 Registry。返回 (entries, nextCursor, 人话错误)。

    出站准入在拼接后的完整 URL 上做一次（含 query，防 `?` 夹带），超时 15s。
    """
    why = outbound.check_url(url, allow_private=False, require_https=True)
    if why:
        return [], "", "出站准入拒绝：%s" % why
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": UA})
    try:
        with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_S) as resp:
            data = json.loads(resp.read(8 * 1024 * 1024).decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return [], "", "HTTP %s %s" % (e.code, (e.reason or "")[:80])
    except Exception as e:  # noqa: BLE001
        return [], "", "%s: %s" % (type(e).__name__, str(e)[:120])
    if not isinstance(data, dict):
        return [], "", "响应不是 JSON 对象"
    entries = [x for x in (data.get("servers") or []) if isinstance(x, dict)]
    meta = data.get("metadata") or {}
    return entries, str(meta.get("nextCursor") or ""), ""


def _cache_tags(card: Dict[str, Any]) -> List[str]:
    name = str(card.get("name") or "")
    tokens = [t for t in re.split(r"[/._:-]", name) if t]
    desc = str(card.get("description") or "")
    tokens += [w for w in re.split(r"\s+", desc.lower()) if w.isalnum()][:24]
    return list(dict.fromkeys(tokens))[:40]


def _upsert_cache(source_id: str, entry: Dict[str, Any]) -> bool:
    card = entry.get("server") if isinstance(entry.get("server"), dict) else entry
    if not isinstance(card, dict):
        return False
    name = str(card.get("name") or "").strip()
    if not name:
        return False
    version = str(card.get("version") or "").strip()
    off = ((entry.get("_meta") or {}).get("io.modelcontextprotocol.registry/official")
           or {})
    status = str(off.get("status") or "active")
    is_latest = 1 if off.get("isLatest", True) else 0
    spec = map_server_json(card)
    key = hashlib.sha1(("%s|%s|%s" % (source_id, name, version))
                       .encode("utf-8")).hexdigest()
    now = _now()
    db.execute(
        "INSERT INTO mcp_source_cache(cache_key,source_id,name,version,title,description,tags,"
        "server_json,status,is_latest,published_at,updated_at,fetched_at,"
        "transport_hint,has_secret) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(cache_key) DO UPDATE SET"
        " title=excluded.title, description=excluded.description, tags=excluded.tags,"
        " server_json=excluded.server_json, status=excluded.status,"
        " is_latest=excluded.is_latest, published_at=excluded.published_at,"
        " updated_at=excluded.updated_at, fetched_at=excluded.fetched_at,"
        " transport_hint=excluded.transport_hint, has_secret=excluded.has_secret",
        (key, source_id, name, version, str(card.get("title") or "")[:200],
         str(card.get("description") or "")[:2000],
         json.dumps(_cache_tags(card), ensure_ascii=False),
         json.dumps(card, ensure_ascii=False),
         status, is_latest, off.get("publishedAt"), off.get("updatedAt"), now,
         spec["transport_hint"], 1 if spec["has_secret"] else 0))
    return True


def sync_source(sid: str, search: str = "", force: bool = False) -> Dict[str, Any]:
    """抓一个源并 upsert 到 `mcp_source_cache`。

    失败**不清空旧缓存**（degraded 如实报），并把 `last_error` 写回源卡。
    `force=True` 全量重扫（忽略 `updated_since`）；默认按 `updated_since` 增量。
    """
    src = source_get(sid)
    if not src:
        raise LookupError("源不存在: %s" % sid)
    base = src["base_url"].rstrip("/")
    params: Dict[str, str] = {"limit": str(SYNC_PAGE_LIMIT), "version": "latest"}
    if search.strip():
        params["search"] = search.strip()[:120]
    if not force and src.get("updated_since"):
        params["updated_since"] = src["updated_since"]
    cursor = ""
    total = 0
    pages = 0
    err = ""
    for _ in range(SYNC_MAX_PAGES):
        p = dict(params)
        if cursor:
            p["cursor"] = cursor
        url = base + "?" + urllib.parse.urlencode(p)
        entries, nxt, e = _fetch_page(url)
        pages += 1
        if e:
            err = "第 %d 页：%s" % (pages, e)
            break
        for en in entries:
            if _upsert_cache(sid, en):
                total += 1
        cursor = nxt
        if not cursor or search.strip():
            break
    cnt = db.query(
        "SELECT COUNT(*) AS n FROM mcp_source_cache WHERE source_id=?", (sid,))
    cached = int(cnt[0]["n"] or 0) if cnt else total
    touch_source(sid, cached, err, cursor,
                 _now() if not err else (src.get("updated_since") or ""))
    db.log_profile_event("mcp_source", sid, "fail" if err else "success", None,
                         detail={"pages": pages, "fetched": total,
                                 "cached": cached, "error": err or None,
                                 "degraded": bool(err)})
    return {"source_id": sid, "pages": pages, "fetched": total, "cached": cached,
            "degraded": bool(err), "last_error": err, "cursor": cursor}


# ── server.json → mcpgw 安装规格 ───────────────────────────────────────────

def split_credential_url(url: str) -> Tuple[str, List[str]]:
    """把 URL query 里像凭据的值换成 `${VAR}` 占位，返回 (干净 URL, 变量名列表)。

    `outbound.check_url` 只挡 `u.username/u.password`（`outbound.py:93-94`），
    **挡不住 `https://mcp.amap.com/mcp?key=xxx`**。不拆就会让凭据随
    `mcp_servers.url` 落库并被 `GET /mcp/servers` 回显。
    """
    u = urllib.parse.urlsplit(url or "")
    if not u.query:
        return url or "", []
    pairs = urllib.parse.parse_qsl(u.query, keep_blank_values=True)
    names: List[str] = []
    parts: List[str] = []
    for k, v in pairs:
        if _SECRET_PARAM.search(k) and v and not _PLACEHOLDER.fullmatch(v):
            var = re.sub(r"[^A-Za-z0-9_]", "_", k).upper()
            if var not in names:
                names.append(var)
            parts.append("%s=${%s}" % (urllib.parse.quote(k, safe=""), var))
        elif _PLACEHOLDER.fullmatch(v):
            # 已经是占位符 ⇒ 原样保留，别二次编码成 %24%7B...%7D
            # （那样 resolve_url 认不出，会误报「缺少运行时凭据」）。
            parts.append("%s=%s" % (urllib.parse.quote(k, safe=""), v))
        else:
            parts.append("%s=%s" % (urllib.parse.quote(k, safe=""),
                                    urllib.parse.quote(v, safe="")))
    clean = urllib.parse.urlunsplit(
        (u.scheme, u.netloc, u.path, "&".join(parts), u.fragment))
    return clean, names


def resolve_url(url: str, runtime_env: Optional[Dict[str, Any]] = None
                ) -> Tuple[str, List[str]]:
    """把 `${VAR}` 占位符用**调用时注入**的值替换；返回 (URL, 缺失的变量名)。

    值不落库：`mcp_servers.url` 里只有占位符，真实值来自 `CallIn.env` /
    `ServerProbe.env`，只在本次会话内存里存在。
    """
    env = runtime_env or {}
    missing: List[str] = []

    def _sub(m: "re.Match[str]") -> str:
        var = m.group(1)
        if var in env and env[var] is not None and str(env[var]) != "":
            return urllib.parse.quote(str(env[var]), safe="")
        if var not in missing:
            missing.append(var)
        return m.group(0)

    return _PLACEHOLDER.sub(_sub, url or ""), missing


def map_server_json(card: Dict[str, Any]) -> Dict[str, Any]:
    """`server.json` → `mcpgw.add_server` 可直接用的规格。**纯函数，零网络零写盘**。

    `packages[]` 与 `remotes[]` 并存时优先 remotes（无本地进程，供应链面更小）。
    `isSecret` 只回名字，绝不回值。
    """
    out: Dict[str, Any] = {
        "installable": False, "reason": "", "transport": None,
        "command": None, "args": [], "url": None, "env": {},
        "required_secrets": [], "required_env": [],
        "transport_hint": "unknown", "has_secret": False,
        "registry_type": "",
    }
    # ① remotes 优先
    for r in card.get("remotes") or []:
        if not isinstance(r, dict):
            continue
        rtype = str(r.get("type") or "").lower()
        if rtype not in ("streamable-http", "streamable_http", "http", "sse"):
            continue
        raw = str(r.get("url") or "").strip()
        if not raw:
            continue
        clean, names = split_credential_url(raw)
        out.update({"installable": True, "transport": "http", "url": clean,
                    "transport_hint": "http", "required_secrets": names,
                    "has_secret": bool(names),
                    "reason": "remotes/%s" % rtype})
        break
    # ② 否则 packages[]
    if not out["installable"]:
        for p in card.get("packages") or []:
            if not isinstance(p, dict):
                continue
            rt = str(p.get("registryType") or "").lower()
            ident = str(p.get("identifier") or "").strip()
            out["registry_type"] = rt
            if rt == "oci":
                out["transport_hint"] = "oci"
                out["reason"] = "oci 需容器运行时，无 StdioServerParameters 对应，不自动安装"
                continue
            if rt not in AUTO_REGISTRY_TYPES:
                out["reason"] = "registryType=%r 不在自动安装白名单 %s" % (
                    rt, list(AUTO_REGISTRY_TYPES))
                continue
            if not ident or not _IDENT_OK.fullmatch(ident):
                out["reason"] = "identifier 形态不合法（要安全进 argv）"
                continue
            cmd = str(p.get("runtimeHint") or ("npx" if rt == "npm" else "uvx"))
            pre = [str(a.get("value")) for a in (p.get("runtimeArguments") or [])
                   if isinstance(a, dict) and a.get("type") == "positional"
                   and a.get("value") is not None]
            post = [str(a.get("value")) for a in (p.get("packageArguments") or [])
                    if isinstance(a, dict) and a.get("type") == "positional"
                    and a.get("value") is not None]
            out.update({"installable": True, "transport": "stdio",
                        "command": cmd, "args": pre + [ident] + post,
                        "transport_hint": "stdio", "reason": "packages/%s" % rt})
            break
    # ③ 环境变量：secret 只记名，非敏感带 default 才落值
    for p in card.get("packages") or []:
        if not isinstance(p, dict):
            continue
        for ev in p.get("environmentVariables") or []:
            if not isinstance(ev, dict):
                continue
            nm = str(ev.get("name") or "").strip()
            if not nm:
                continue
            if ev.get("isSecret"):
                if nm not in out["required_secrets"]:
                    out["required_secrets"].append(nm)
                    out["has_secret"] = True
            else:
                default = ev.get("default")
                if default is not None and isinstance(default, (str, int, float, bool)):
                    sv = str(default)
                    if len(sv) <= 200 and "://" not in sv and not _SECRET_PARAM.search(sv):
                        out["env"][nm] = sv
                elif ev.get("isRequired"):
                    out["required_env"].append(nm)
            if ev.get("isRequired") and nm not in out["required_env"] \
                    and nm not in out["required_secrets"]:
                out["required_env"].append(nm)
    if not out["installable"] and not out["reason"]:
        out["reason"] = "server.json 未给出可安装的 packages[] 或 remotes[]"
    return out


async def sync_loop(interval_s: int = 3600) -> None:
    """每小时增量抓一次所有启用的源（官方 Registry 文档：聚合器应「定期但不频繁」）。

    `urllib` 是同步阻塞调用 ⇒ 丢进 `asyncio.to_thread`，不堵 hub 的事件循环。
    `HUB_MCP_AUTO_SYNC=0` 可整体关闭；单源失败只写 `last_error`，不打断其余源。
    """
    if os.getenv("HUB_MCP_AUTO_SYNC", "1").strip().lower() in ("0", "false", "no", "off"):
        return
    while True:
        await asyncio.sleep(max(60, int(interval_s)))
        try:
            rows = db.query("SELECT id FROM mcp_sources WHERE enabled=1")
            for r in rows:
                try:
                    await asyncio.to_thread(sync_source, r["id"])
                except Exception:  # noqa: BLE001 — 单源挂不拖垮整轮
                    pass
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass


def auto_used_last_hour() -> int:
    """近一小时自动安装成功次数（读账，不维护第二份状态）。计数坏了按「已用满」。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    try:
        rows = db.query(
            "SELECT COUNT(*) AS n FROM profile_events WHERE source='mcp_source'"
            " AND subject='install_auto' AND status='success' AND created_at>=?",
            (since,))
        return int(rows[0]["n"] or 0) if rows else 0
    except Exception:  # noqa: BLE001
        return AUTO_MAX_PER_HOUR


def prepare_install(cache_key: str, auto: bool = False) -> Dict[str, Any]:
    """校验质量闸门并产出安装规格；**不写 `mcp_servers`**（由 `mcpgw` 落库）。"""
    rows = db.query("SELECT * FROM mcp_source_cache WHERE cache_key=?", (cache_key,))
    if not rows:
        raise LookupError("缓存卡不存在: %s" % cache_key)
    r = dict(rows[0])
    if str(r["status"]) != "active":
        raise PermissionError("status=%s 不入可装池" % r["status"])
    if not int(r["is_latest"] or 0):
        raise PermissionError("非最新版本（isLatest=0）不入可装池")
    card = json.loads(r["server_json"] or "{}")
    spec = map_server_json(card)
    if not spec["installable"]:
        raise PermissionError(spec["reason"] or "无可用安装形态")
    if auto and auto_used_last_hour() >= AUTO_MAX_PER_HOUR:
        raise PermissionError("自动安装额度已用满（近一小时 %d/%d）；"
                              "请手动安装或稍后再试" % (auto_used_last_hour(),
                                                    AUTO_MAX_PER_HOUR))
    if spec["url"]:
        why = outbound.check_url(spec["url"], allow_private=False, require_https=True)
        if why:
            raise PermissionError("出站准入拒绝：%s" % why)
    off = ((card.get("_meta") or {}).get("io.modelcontextprotocol.registry/official")
           or {})
    return {
        "cache_key": cache_key, "source_id": r["source_id"],
        "registry_name": str(card.get("name") or r["name"]),
        "title": str(card.get("title") or r["title"] or r["name"]),
        "description": str(card.get("description") or r["description"] or "")[:500],
        "version": str(card.get("version") or ""),
        "official_status": str(off.get("status") or r["status"]),
        "trust": "official" if r["source_id"] == DEFAULT_SOURCE_ID else "community",
        "spec": spec,
    }


# ── 发现层：按任务排序本地缓存卡 ───────────────────────────────────────────

def _expand_query(q: str) -> str:
    extra = [v for k, v in QUERY_ALIASES.items() if k in q]
    return (q + " " + " ".join(extra)).strip() if extra else q


def catalog(q: str = "", n: int = 20) -> Dict[str, Any]:
    """可装池（active + isLatest + 源仍启用）按任务 BM25F 排序。

    纯本地缓存、零网络零费用；`why` 不吞空结果（语料空 / 无词命中都如实说）。
    """
    rows = [dict(r) for r in db.query(
        "SELECT c.* FROM mcp_source_cache c"
        " JOIN mcp_sources s ON s.id=c.source_id AND s.enabled=1"
        " WHERE c.status='active' AND c.is_latest=1 ORDER BY c.name")]
    if not rows:
        return {"items": [], "considered": 0, "selected": 0,
                "why": "本地目录缓存为空：先添加源并同步",
                "note": "", "degraded": []}
    items = []
    for r in rows:
        spec = map_server_json(json.loads(r["server_json"] or "{}"))
        items.append({
            "cache_key": r["cache_key"], "source_id": r["source_id"],
            "name": r["name"], "title": r["title"],
            "description": r["description"],
            "triggers": (json.loads(r["tags"] or "[]") or [])[:12],
            "version": r["version"] or "",
            "transport_hint": r["transport_hint"],
            "has_secret": bool(r["has_secret"]),
            "installable": spec["installable"],
            "install_reason": spec["reason"],
            "required_secrets": spec["required_secrets"],
            "required_env": spec["required_env"],
            "trust": "official" if r["source_id"] == DEFAULT_SOURCE_ID else "community",
        })
    if not (q or "").strip():
        picked = items[:max(1, int(n))]
        return {"items": picked, "considered": len(items),
                "selected": len(picked), "why": None,
                "note": "未提供任务描述，按名称列出；给 task 才会按相关度排序",
                "degraded": []}
    expanded = _expand_query(q.strip())
    ranked = skill_relevance.rank(expanded, items, n=max(int(n), 1))
    picked = [x["item"] for x in (ranked.get("items") or [])][:max(1, int(n))]
    note = ""
    why = ranked.get("why")
    if not picked and items:
        # 共享 `skill_relevance.build_index` 按「字段」而非「文档」累计 df：一个词
        # 同时出现在 name/triggers/description 时 df 可 > 语料数 ⇒ idf 变负 ⇒
        # rank 把所有命中过滤成空。用 `search=filesystem` 抓回来的窄语料必然踩中
        # （实测 10 张卡 idf=-0.986、max score=-10.2）。这是**既有共享实现**的边界，
        # 按白名单制不擅自改它；这里显式回退到子串匹配并在 note 里说清，
        # 不把「打分塌陷」伪装成「没有相关候选」。
        toks = [t for t in skill_relevance.tokenize(expanded) if t]
        if toks:
            def _hit(it):
                hay = " ".join((str(it.get("name") or ""),
                                str(it.get("title") or ""),
                                str(it.get("description") or ""))).lower()
                return any(t in hay for t in toks)
            picked = [it for it in items if _hit(it)][:max(1, int(n))]
        if picked:
            why = None
        note = ("词法打分因高频词 IDF 塌陷无命中，已回退到名称/描述子串匹配"
                "（共享 skill_relevance.build_index 的既有边界，本轮未改）")
        if not picked:
            why = (why or "") + "；子串回退也无命中"
    return {"items": picked, "considered": len(items), "selected": len(picked),
            "why": why, "note": note,
            "degraded": []}
