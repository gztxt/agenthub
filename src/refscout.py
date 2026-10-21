# -*- coding: utf-8 -*-
"""参考情报（RefScout）：功能模块 → GitHub 借鉴源情报卡。

【它解决什么】技能中心缺一层「先看别人怎么做」的能力：给定本仓一个功能模块，
并行检索 GitHub，按**功能 / 实现 / 设计**三轴出分，落成带 `as of` 时效与
许可证闸门的情报卡。只产出建议与固定引用（`repo@sha`），**不克隆、不改项目、
不自动加依赖**。

【P0 实测结论，直接写进实现】判例见 `docs/REFERENCE-RADAR-P0-20261021.md`：
1. 主检索臂必须收窄到 `in:name,description,topics`。带 `in:readme` 时
   `websocket pty` 的 top1 是 9.6 万星清单 —— 词面命中不等于功能命中。
2. 「最新」与「最相关」是两个轴：按 `updated` 排序的 top3 全是 0~1 star 作业仓。
   故 freshness 单独成臂、单独入池，不参与主排序。
3. LLM 重排**不能做同步硬依赖**：免费档实测多为 only-thinking/超时，付费快档也会
   偶发 45s 超时。因此任务一律异步：先落「词法版卡」并即可读，重排完成再原地更新；
   重排失败不是任务失败，卡上标 `lexical_fallback` 与本轮失败原因。

【并发与状态】任务跑在进程内 2 线程池（`_POOL`），DB 行是唯一真相
（`ref_jobs.status/stage`）。前端看到 `lexical` 即可用，不必等 `done`。

【为什么不进 cronjobs 表】`cronjobs` 是**用户面**的 shell/agent/http 三种任务，
把内部维护任务塞进去会让用户在一个不属于他的列表里看到自己没建的 job。
`refresh_loop` 与 `vitals_loop`/`wal_checkpoint_loop` 同类：hub 进程内定时，
**不写系统 crontab**（军规要求的是后者）。
"""
from __future__ import annotations

import base64
import concurrent.futures as futures
import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db
import llm
import outbound
import tdai_client
import ghsettings
from config import config

log = logging.getLogger("agenthub.refscout")
router = APIRouter(prefix="/api/ref", tags=["ref"])

# ── 常量 ────────────────────────────────────────────────────────────────────
GH_API = "https://api.github.com"
UA = "agenthub-refscout/1.0"
CACHE_TTL_SEARCH = 7 * 86400
CACHE_TTL_ENRICH = 86400
#: 自有来源整账号拉取（含 fork）走 core API（5000/h），不吃 search 的 30/min；
#: 6h 足够新鲜，且让同一来源在多张卡之间复用同一份缓存。
CACHE_TTL_SOURCE = 6 * 3600
SOURCE_PAGE = 100
SOURCE_MAX_REPOS = 300        # 单来源最多 3 页；再多也拉不动，且会稀释候选池
#: 自有来源的**排序**加成。三轴分是「这个仓本身值不值得借鉴」的判断依据，
#: 不能掺「我拥有它」这种非质量信号；加成只作用在最终排序上，用于同分时先看自己的仓。
SOURCE_BONUS = 0.06
CACHE_README_CHARS = 6000
MAX_CANDIDATES = 8            # 进入**语义重排**的上限
#: 详情补全窗口。必须严格大于 `MAX_CANDIDATES`：补全集合与重排集合若不完全包含，
#: 重排就会拿到「没抓过 README」的仓 —— 它的实现轴缺信号、卡上也没摘要（2026-10-22 实测）。
ENRICH_LIMIT = 12
#: 清单展开：把 awesome / HelloGitHub 这类「清单仓」展开成它**收录的项目**。
#: 用户要的是清单里的项目，不是清单本身（2026-10-22 裁定）。
LIST_EXPAND_LISTS = 4         # 展开前 N 个清单仓
LIST_EXPAND_SUBPAGES = 4      # 每个清单追 N 篇子页（HelloGitHub 把项目写在 content/ 下）
LIST_EXPAND_PER_LIST = 30     # 单个清单最多取多少个项目（每个都要一次 core API）
LIST_EXPAND_TOTAL = 80        # 一次采集最多新增多少个项目
LIST_EXPAND_THREADS = 4       # GitHub 建议别并发太高，4 路足够
#: README 里多少个「项目链接 + 同仓子页」才算清单。`HelloGitHub` 的根 README 只有
#: 1 个项目链接 + 36 个 content/*.md，靠描述词（awesome/curated）永远识别不出来。
LIST_SIGNAL_LINKS = 8
SEARCH_PER_PAGE = 10
CARD_TTL_DAYS = 30            # 卡上「下次刷新」的间隔
JOB_KEEP_DAYS = 14
#: `book` 必须带词边界：裸 `book` 会把 `Storybook` / `Facebook` / `notebook`
#: 全判成清单桶（实测 `storybookjs/storybook` 因此跳过实现桶）。
LIST_RE = re.compile(r"awesome|curated|list of|\bbooks?\b|tutorial|course|roadmap|cheat|"
                     r"interview|examples?$", re.I)
DOC_RE = re.compile(r"docs?$|documentation|wiki|handbook|guide", re.I)
ARCH_TERMS = ("architecture", "design", "trade-off", "tradeoff", "why we",
              "rationale", "adr")
GPL_RE = re.compile(r"gpl|agpl", re.I)
STOP = {"the", "and", "for", "with", "from", "your", "this", "that", "into", "over",
        "based", "using", "simple", "基于", "实现", "支持", "一个", "以及", "相关"}

#: 中文兜底词典（LLM 不可用时仍能出可检索的英文词）。
FALLBACK_CN = {
    "端口转发": "port forwarding", "终端": "terminal", "会话": "session", "技能": "skill",
    "检索": "search", "扫描": "scan", "缓存": "cache", "转发": "forward", "网关": "gateway",
    "记忆": "memory", "索引": "index", "调用": "invoke", "记录": "record", "分析": "analyze",
    "调度": "scheduler", "任务": "task", "审计": "audit", "权限": "permission",
    "配额": "quota", "限流": "rate limit", "消息": "message", "队列": "queue",
}

#: 查询扩展词表（唯一知识源）：一个词 → 描述同一功能时常用的等价说法。
#: 只收**跨语言/跨框架都成立**的运维与检索词汇；**不收具体产品名**，否则退化成对
#: 评测集的过拟合。匹配侧与检索侧共用同一张表：扩充了查询，也必须让「等价说法命中」
#: 计入覆盖度，否则扩出来的候选会被 `cov<0.18` / `matched<2` 过滤掉（实测见
#: `docs/REFERENCE-RADAR-QX-20261021.md`）。
SYNONYMS = {
    "health": ["healthcheck", "health check", "liveness", "readiness", "uptime", "status page"],
    "probe": ["healthcheck", "health check", "liveness", "readiness"],
    "uptime": ["status page", "availability", "monitoring", "healthcheck"],
    "monitor": ["monitoring", "observability", "watchdog", "alerting"],
    "monitoring": ["observability", "watchdog", "alerting", "uptime"],
    "watchdog": ["supervisor", "healthcheck", "auto restart", "monitoring"],
    "cron": ["crontab", "cron expression", "scheduler", "scheduling"],
    "scheduler": ["scheduling", "job scheduler", "cron", "task scheduling", "job queue"],
    "scheduling": ["scheduler", "cron", "job scheduler", "task queue"],
    "task": ["job", "worker", "queue", "task queue"],
    "queue": ["task queue", "job queue", "message queue", "worker"],
    "search": ["search engine", "full-text search", "full text search", "query"],
    "engine": ["search engine", "index engine"],
    "index": ["indexing", "inverted index"],
    "indexing": ["inverted index", "index engine", "full-text search"],
    "terminal": ["tty", "pty", "console", "shell", "web terminal"],
    "websocket": ["socket", "realtime", "sse", "server-sent events"],
    "pty": ["tty", "pseudo terminal", "terminal emulator"],
    "port": ["port forwarding", "tunnel"],
    "forwarding": ["port forwarding", "tunnel", "relay"],
    "llm": ["language model", "ai gateway", "model gateway"],
    "gateway": ["proxy", "router", "api gateway", "reverse proxy"],
    "proxy": ["gateway", "relay", "router"],
    "router": ["routing", "gateway", "proxy"],
    "failover": ["fallback", "load balancing", "retry", "high availability"],
    "cache": ["caching", "memoization", "key value store"],
    "auth": ["authentication", "oauth", "sso"],
}

SCHEMA = """
CREATE TABLE IF NOT EXISTS ref_cards (
    id            TEXT PRIMARY KEY,
    module        TEXT NOT NULL,
    keywords      TEXT NOT NULL,          -- JSON array
    lang          TEXT,
    status        TEXT NOT NULL,          -- lexical|reranked|lexical_fallback
    as_of         TEXT NOT NULL,
    next_refresh  TEXT NOT NULL,
    elapsed_s     REAL,
    auto_refresh  INTEGER NOT NULL DEFAULT 1,
    meta          TEXT NOT NULL,          -- JSON：arms / cache / rerank / degraded
    candidates    TEXT NOT NULL,          -- JSON array
    diff          TEXT,                   -- JSON：刷新时的新增/移除/变更
    md            TEXT NOT NULL,
    created_at    TEXT NOT NULL,
    updated_at    TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_ref_cards_module ON ref_cards(module);
CREATE TABLE IF NOT EXISTS ref_jobs (
    id          TEXT PRIMARY KEY,
    card_id     TEXT,
    module      TEXT NOT NULL,
    payload     TEXT NOT NULL,
    status      TEXT NOT NULL,            -- queued|running|done|failed
    stage       TEXT NOT NULL DEFAULT '',
    error       TEXT,
    created_at  TEXT NOT NULL,
    started_at  TEXT,
    finished_at TEXT
);
CREATE INDEX IF NOT EXISTS idx_ref_jobs_status ON ref_jobs(status);
CREATE TABLE IF NOT EXISTS ref_sources (
    id          TEXT PRIMARY KEY,
    kind        TEXT NOT NULL,            -- owner|repo|starred
    value       TEXT NOT NULL,            -- gztxt | gztxt/agenthub | gztxt（星标）
    label       TEXT NOT NULL DEFAULT '',
    enabled     INTEGER NOT NULL DEFAULT 1,
    created_at  TEXT NOT NULL,
    synced_at   TEXT,
    repo_count  INTEGER NOT NULL DEFAULT 0,
    last_error  TEXT
);
-- 唯一键必须带 kind：同一个 `gztxt` 既可能是「他的仓」也可能是「他标星的仓」，
-- 两者候选集完全不同，按 value 去重会把后者静默吞掉。
DROP INDEX IF EXISTS idx_ref_sources_value;
CREATE UNIQUE INDEX IF NOT EXISTS idx_ref_sources_kind_value ON ref_sources(kind, value);
"""

_POOL = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix="refscout")
#: 同一 module 只允许一个在跑的任务（重复点击 = 复用排队中的 job）。
_INFLIGHT: Dict[str, str] = {}
_INFLIGHT_LOCK = threading.Lock()
_token_cache: Dict[str, Any] = {"value": "", "at": 0.0}


# ── 基础 ────────────────────────────────────────────────────────────────────

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec="seconds")


def ensure_schema() -> None:
    db.execute_script(SCHEMA)


def token() -> str:
    """DB（设置页）→ env → token 文件首行。**真值不出本函数**。"""
    if _token_cache["value"] and time.time() - _token_cache["at"] < 60:
        return _token_cache["value"]
    tok = ""
    try:
        tok = ghsettings.db_get("token") or os.getenv("GITHUB_TOKEN", "")
    except Exception:  # noqa: BLE001 —— 设置表未初始化时退到文件
        tok = os.getenv("GITHUB_TOKEN", "")
    if not tok:
        try:
            tok = ghsettings.token_file().read_text(encoding="utf-8",
                                                    errors="ignore").splitlines()[0].strip()
        except (OSError, IndexError):
            tok = ""
    tok = (tok or "").strip()
    _token_cache.update({"value": tok, "at": time.time()})
    return tok


def _scrub(text: str) -> str:
    try:
        return tdai_client.scrub(text, token())[:400]
    except Exception:  # noqa: BLE001 —— 脱敏器坏了也不能把原文漏出去
        return ""


def _cache_dir() -> str:
    p = config.data_dir / "refscout-cache"
    p.mkdir(parents=True, exist_ok=True)
    return str(p)


def _cache_path(url: str) -> str:
    return os.path.join(_cache_dir(), hashlib.sha1(url.encode()).hexdigest() + ".json")


def cache_get(url: str, ttl_s: int):
    p = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(p) > ttl_s:
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def cache_put(url: str, payload) -> None:
    try:
        tmp = _cache_path(url) + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
        os.replace(tmp, _cache_path(url))
    except OSError:
        pass


def cache_stats() -> Dict[str, Any]:
    d = _cache_dir()
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith(".json")]
        size = sum(os.path.getsize(f) for f in files)
        return {"files": len(files), "bytes": size, "dir": d}
    except OSError:
        return {"files": 0, "bytes": 0, "dir": d}


# ── 来源（自有 GitHub 仓，含 fork）──────────────────────────────────────────
# 用户 2026-10-21 追加：除全站搜索外，把「自己的账号 / 组织 / 指定仓」也当候选池的一路。
# 为什么不是只做标注：自有仓常常 **0 star 或已 fork 多时**，全站搜索里根本进不来；
# 只贴标签等于永远标不到东西。这里在池子阶段并入，再走同一套过滤/打分/闸门。
# 代价：自有仓不吃 `stars:>=N`、`fork:false` 两个搜索限定（这正是要点），但
# **archived 仍然拒**——许可证与维护状态是硬闸门，不因「是我自己的」而豁免。

_SOURCE_SEG = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}\Z")
_GITHUB_HOSTS = ("github.com", "www.github.com", "api.github.com")


def _norm_source(raw: str) -> Tuple[str, str]:
    """把用户输入归一成 `(kind, value)`。

    `gztxt` → owner；`gztxt/agenthub` → repo；`starred:gztxt` / `gztxt:starred` /
    `github.com/gztxt?tab=stars` / `github.com/stars/gztxt` → starred（该账号标星的仓）。

    只接受 github.com。拒掉别的主机不是洁癖：`gitlab.com/x` 会被当成 `owner/repo`
    拼进 GitHub API，最后以 404 收场，用户看到的是「来源没数据」而不是「地址写错」。
    """
    v = (raw or "").strip()
    if not v:
        raise HTTPException(400, "来源为空")
    starred = False
    v = re.sub(r"^[A-Za-z][A-Za-z0-9+.-]*://", "", v)     # https://
    head = v.split("/")[0]
    if head.lower() in _GITHUB_HOSTS:
        v = v.partition("/")[2]
    elif "." in head:
        raise HTTPException(400, "只支持 github.com 的账号 / 组织 / 仓库 / 星标列表")
    # `?tab=stars` 是 GitHub 星标页的真实地址；查询串必须在切段之前看，切完就丢了。
    m = re.search(r"[?#](.*)$", v)
    query = m.group(1) if m else ""
    if m:
        v = v[:m.start()]
    if re.search(r"(?:^|&)tab=stars(?:&|$)", query, re.I):
        starred = True
    v = v.strip("/")
    v = re.sub(r"\.git$", "", v, flags=re.I)
    parts = [p for p in v.split("/") if p]
    if parts and parts[0].lower() == "stars":              # github.com/stars/gztxt
        starred, parts = True, parts[1:]
    if parts and ":" in parts[0]:                          # starred:gztxt / gztxt:starred
        left, _, right = parts[0].partition(":")
        if left.lower() == "starred":
            starred, parts[0] = True, right
        elif right.lower() == "starred":
            starred, parts[0] = True, left
        else:
            raise HTTPException(400, "只支持 starred:<账号> 形式的星标来源")
    parts = parts[:2]                                      # /tree/main 之类的尾巴直接丢
    if starred:                                            # 星标按账号取，不接 owner/repo
        if not parts or not _SOURCE_SEG.match(parts[0]):
            raise HTTPException(400, "星标来源只填账号名，如 gztxt:starred")
        return "starred", parts[0]
    if not parts or not all(_SOURCE_SEG.match(p) for p in parts):
        raise HTTPException(400, "来源名不合法（只允许字母数字 . _ -）")
    if len(parts) == 2:
        return "repo", f"{parts[0]}/{parts[1]}"
    return "owner", parts[0]


def _source_label(row: Dict[str, Any]) -> str:
    return (row.get("label") or row.get("value") or "").strip()


def list_sources() -> Dict[str, Any]:
    rows = db.query("SELECT * FROM ref_sources ORDER BY created_at")
    items = []
    for r in rows:
        items.append({"id": r["id"], "kind": r["kind"], "value": r["value"],
                      "label": _source_label(r), "enabled": bool(r["enabled"]),
                      "created_at": r["created_at"], "synced_at": r["synced_at"],
                      "repo_count": int(r["repo_count"] or 0),
                      "last_error": r["last_error"] or ""})
    return {"items": items, "total": len(items)}


def enabled_sources() -> List[Dict[str, Any]]:
    return db.query("SELECT * FROM ref_sources WHERE enabled=1 ORDER BY created_at")


def add_source(value: str, label: str = "") -> Dict[str, Any]:
    kind, norm = _norm_source(value)
    rows = db.query("SELECT * FROM ref_sources WHERE kind=? AND value=?", (kind, norm))
    if rows:                                   # 幂等：重复添加不报错、不产生第二条
        return {"id": rows[0]["id"], "value": norm, "kind": rows[0]["kind"], "deduped": True}
    sid = uuid.uuid4().hex[:10]
    # 星标来源默认带前缀：同名的 owner 与 starred 是两条来源，chip 上必须能分辨。
    name = (label or "").strip() or (f"star:{norm}" if kind == "starred" else norm)
    db.execute("INSERT INTO ref_sources(id,kind,value,label,enabled,created_at,repo_count)"
               " VALUES(?,?,?,?,1,?,0)",
               (sid, kind, norm, name, _now()))
    return {"id": sid, "value": norm, "kind": kind, "deduped": False}


def delete_source(sid: str) -> Dict[str, Any]:
    if not db.query("SELECT id FROM ref_sources WHERE id=?", (sid,)):
        raise HTTPException(404, "来源不存在")
    db.execute("DELETE FROM ref_sources WHERE id=?", (sid,))
    return {"deleted": sid}


def _touch_source(sid: str, count: int, err: Optional[str]) -> None:
    db.execute("UPDATE ref_sources SET synced_at=?,repo_count=?,last_error=? WHERE id=?",
               (_now(), count, (err or "")[:200], sid))


def _fetch_source(kind: str, value: str) -> Dict[str, Any]:
    """拉一个来源的仓库（**含 fork**，按 updated 排序）。失败不抛：来源坏了不能拖垮整卡。"""
    t0 = time.monotonic()

    def done(items: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
        ok = bool(meta.get("ok")) or bool(items)
        return {"items": items[:SOURCE_MAX_REPOS], "ok": ok,
                "error": None if ok else (meta.get("error") or "fetch failed"),
                "ms": int((time.monotonic() - t0) * 1000),
                "cached": bool(meta.get("cached"))}

    if kind == "repo":
        data, meta = gh_get(f"{GH_API}/repos/{value}", ttl_s=CACHE_TTL_SOURCE)
        return done([data] if data else [], meta)

    if kind == "starred":
        # 星标是「我认可的外部项目」，不是我的仓：同样只读、同样并入候选池，
        # 但候选集来自 /starred 而非 /repos。
        items: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {}
        for page in range(1, SOURCE_MAX_REPOS // SOURCE_PAGE + 1):
            url = (f"{GH_API}/users/{value}/starred?per_page={SOURCE_PAGE}&page={page}"
                   f"&sort=updated&direction=desc")
            data, meta = gh_get(url, ttl_s=CACHE_TTL_SOURCE)
            if not meta.get("ok"):
                break
            items.extend(data or [])
            if len(data or []) < SOURCE_PAGE:
                break
        return done(items, meta)

    items: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    for page in range(1, SOURCE_MAX_REPOS // SOURCE_PAGE + 1):
        url = (f"{GH_API}/users/{value}/repos?per_page={SOURCE_PAGE}&page={page}"
               f"&type=owner&sort=updated")
        data, meta = gh_get(url, ttl_s=CACHE_TTL_SOURCE)
        if not meta.get("ok"):
            break
        items.extend(data or [])
        if len(data or []) < SOURCE_PAGE:
            break
    # `/users/{org}` 对组织名恒 404：同一个值可能是组织，只在 404 时改走 /orgs
    # （403/429 是限流，换端点只会多烧一次额度）。
    if not items and meta.get("status") == 404:
        for page in range(1, SOURCE_MAX_REPOS // SOURCE_PAGE + 1):
            url = (f"{GH_API}/orgs/{value}/repos?per_page={SOURCE_PAGE}&page={page}"
                   f"&type=all&sort=updated")
            data, meta = gh_get(url, ttl_s=CACHE_TTL_SOURCE)
            if not meta.get("ok"):
                break
            items.extend(data or [])
            if len(data or []) < SOURCE_PAGE:
                break
    return done(items, meta)


def _sync_sources() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]],
                             Dict[str, Dict[str, Any]]]:
    """拉取全部启用来源。返回 `(逐路日志, 卡面概况, full_name→{item,labels})`。

    并入的是**仓库条目本身**（与搜索结果同一形态），而不是只记一个标签：
    后续过滤/打分/许可证闸门全部复用，避免出现「来源候选走另一套规则」。
    """
    log: List[Dict[str, Any]] = []
    overview: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in enabled_sources():
        label = _source_label(row)
        got = _fetch_source(row["kind"], row["value"])
        repos = [it for it in got["items"]
                 if it.get("full_name") and not it.get("archived")]
        log.append({"name": "src:" + label, "q": row["value"], "ok": got["ok"],
                    "count": len(repos), "ms": got["ms"], "cached": got["cached"],
                    "error": got["error"]})
        overview.append({"id": row["id"], "value": row["value"], "label": label,
                         "kind": row["kind"], "count": len(repos),
                         "ok": got["ok"], "error": got["error"]})
        _touch_source(row["id"], len(repos), got["error"])
        for it in repos:
            key = it["full_name"].lower()
            rec = by_key.setdefault(key, {"item": it, "labels": []})
            if label not in rec["labels"]:
                rec["labels"].append(label)
    return log, overview, by_key


class Limiter:
    """按分钟的简单节流。GitHub 搜索是 30/min（全 search 共享），留余量取 25。"""

    def __init__(self, per_min: int):
        self.gap = 60.0 / max(1, per_min)
        self.last = 0.0
        self.lock = threading.Lock()

    def wait(self) -> None:
        with self.lock:
            delta = time.monotonic() - self.last
            if delta < self.gap:
                time.sleep(self.gap - delta)
            self.last = time.monotonic()


_SEARCH_LIMIT = Limiter(25)
_CODE_LIMIT = Limiter(8)


def gh_get(url: str, ttl_s: Optional[int] = None,
           limiter: Optional[Limiter] = None) -> Tuple[Any, Dict[str, Any]]:
    """GET 一个 GitHub API URL。`ttl_s` 非空才缓存（只缓存静态内容，不缓存限额头）。"""
    if ttl_s:
        hit = cache_get(url, ttl_s)
        if hit is not None:
            return hit, {"ok": True, "ms": 0, "cached": True}
    reason = outbound.check_url(url, require_https=True)
    if reason:
        return None, {"ok": False, "ms": 0, "error": f"出站拒绝：{reason}"}
    if limiter:
        limiter.wait()
    t0 = time.monotonic()
    headers = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    tok = token()
    if tok:
        headers["Authorization"] = f"Bearer {tok}"
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if ttl_s:
            cache_put(url, data)
        return data, {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "cached": False}
    except urllib.error.HTTPError as exc:
        body = ""
        try:
            body = _scrub(exc.read().decode("utf-8", "ignore"))
        except Exception:  # noqa: BLE001
            pass
        return None, {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                      "status": exc.code, "error": f"HTTP{exc.code} {body}"[:300],
                      "rate_limited": exc.code in (403, 429)}
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                      "error": f"{type(exc).__name__}: {str(exc)[:120]}"}


# ── 查询构造 ────────────────────────────────────────────────────────────────

def tokens_of(keywords: List[str]) -> List[str]:
    out = []
    for k in keywords:
        for t in re.split(r"[^a-z0-9+#.\u4e00-\u9fff]+", str(k).lower()):
            if len(t) > 2 and t not in STOP:
                out.append(t)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


def build_queries(keywords: List[str], module: str, min_stars: int,
                  fresh_days: int) -> List[Tuple[str, str, str]]:
    """返回 [(臂名, 查询式, 排序)]。主臂收窄，新鲜臂独立成桶。"""
    cut = (datetime.now(timezone.utc) - timedelta(days=fresh_days)).strftime("%Y-%m-%d")
    base = f" fork:false archived:false stars:>={min_stars}"
    toks = tokens_of(keywords)
    arms: List[Tuple[str, str, str]] = []
    if toks:
        arms.append(("core", f"{' '.join(toks[:3])} in:name,description,topics" + base, "stars"))
    if len(toks) >= 2:
        arms.append(("pair", f"{toks[0]} {toks[1]} in:name,description,topics" + base, "stars"))
    if len(toks) >= 3:
        arms.append(("pair2", f"{toks[0]} {toks[2]} in:name,description,topics" + base, "stars"))
    if toks:
        arms.append(("fresh", f"{' '.join(toks[:4])} in:name,description,topics" + base
                     + f" pushed:>{cut}", "updated"))
    if re.search(r"[\u4e00-\u9fff]", module):
        arms.append(("cn", f"{module} in:name,description" + base, "stars"))
    if len(toks) >= 2:
        # 资料臂：只进「资料/清单」桶，**不参与实现对比**（P0 的 awesome 霸榜教训）。
        arms.append(("reading", f"{toks[0]} {toks[-1]} in:readme" + base, "stars"))
    if not arms:
        arms.append(("core", f"{module} in:name,description" + base, "stars"))
    # ── 查询扩展臂：一条查询只放**一个概念** ────────────────────────────────
    # GitHub 仓库搜索的多词是 AND，`core`/`fresh` 把 3~4 个概念 AND 成一条时，只要
    # 有一个词不在仓的描述里，整臂就是 0 结果（robfig/cron 的描述只有
    # "a cron library for go"，`cron scheduler task` 必空；meilisearch 的
    # `full-text search indexing` 同理）。这里不再增加「词」，只把已有概念拆成
    # 可独立命中的查询；池容量（10 条/臂）与候选上限（8）均不变。
    # 顺序：多词关键词本身就是概念短语（`search engine`），优先；再补首个单词关键词。
    multi = [k for k in keywords if " " in str(k).strip()]
    single = [k for k in keywords if " " not in str(k).strip()]

    def _arm_key(q: str) -> frozenset:
        """臂的「概念键」＝查询串里 `in:` 之前的词元集（大小写/连字符已归一）。"""
        return frozenset(tokens_of([q.split(" in:")[0]]))

    seen_k = [_arm_key(a[1]) for a in arms]
    for i, kw in enumerate(list(dict.fromkeys(multi + single))[:3]):
        kt = _arm_key(kw)
        # 旧实现按**原始查询串**去重：`Log Aggregation`（用户大写）与 `pair` 臂的
        # `log aggregation` 字符串不等 ⇒ 同一条查询发两遍，白烧搜索配额（30/min）；
        # 反过来短语型关键词（`log aggregation`）与 `pair` 字面全等 ⇒ kw 臂被整条吃掉。
        # 判等必须落在**词元集**上，且只判**相等**——不能用「子集」：
        # 单词关键词 `health` 是 `health uptime monitoring` 的子集，但后者在 GitHub 上
        # 常是 0 结果，子集判重会把 4 条单词臂全吃掉（实测 0 条 kw 臂），召回反而塌。
        if not kt or any(kt == s for s in seen_k):
            continue
        arms.append((f"kw{i}", f"{kw} in:name,description,topics" + base, "stars"))
        seen_k.append(kt)

    # 短语首/末词臂：多词短语在 GitHub 是 AND，命中面常只剩几十个长尾仓
    # （实测 `log aggregation` total=25、`centralized logging` total=21，而 loki/fluent-bit
    # 都不在里面）。把短语拆成**单个词**再查能把主流实现捞回来：`log` → loki #10、
    # `documentation` → docusaurus #6。只发首词与末词各一条，避免臂数失控。
    # 拆出来的仓仍要过覆盖度闸门，泛词捞回的噪声在 `cov<0.18` 处会被过滤。
    # ⚠️ 默认关。实测（v3 标注集，冷跑）：开臂能让 loki 从「未入池」到 #1，但池从 66→82，
    # 把 terminal 的 ttyd 挤出 top5，v1 子集 0.600→0.533、全量 0.552→0.543。
    # 「一条换一条 + 噪声更差」⇒ 不做。保留开关只为复现；短语拆词的真问题是
    # GitHub 索引里该语义簇就用 logs/logging 描述，拆成单词只会放大星数偏差。
    if os.getenv("REF_HEAD_ARM", "0") != "0":
        for i, kw in enumerate(list(dict.fromkeys(multi))[:1]):
            tk = tokens_of([kw])
            for pos, word in (("h", tk[0] if tk else ""), ("t", tk[-1] if tk else "")):
                if len(word) < 3 or word in STOP:   # `log`(3) 正是最有效的那个词，别按长度砍
                    continue
                kt = frozenset([word])
                # 与 kw 臂不同：单词臂与短语臂的召回集合几乎不重叠，只有**完全相同**才跳过
                if kt in seen_k:
                    continue
                arms.append((f"{pos}ead{i}" if pos == "h" else f"{pos}ail{i}",
                             f"{word} in:name,description,topics" + base, "stars"))
                seen_k.append(kt)
    # 统一去重（查询串 + 排序键）：只有一个关键词时 `core`/`pair` 会拼出同一条查询，
    # `fresh` 虽然查询串相同但按 updated 排，是另一条臂 —— 键里必须带 sort。
    out, seen_q = [], set()
    for name, q, sort in arms:
        if (q, sort) in seen_q:
            continue
        seen_q.add((q, sort))
        out.append((name, q, sort))
    return out


# ── 打分 ────────────────────────────────────────────────────────────────────

def classify(item: Dict[str, Any]) -> str:
    blob = f"{item.get('name', '')} {item.get('description') or ''}"
    if LIST_RE.search(blob):
        return "list"
    if DOC_RE.search(item.get("name", "")):
        return "doc"
    return "impl"


def _name_blob(item: Dict[str, Any]) -> str:
    return " ".join([item.get("name", ""), item.get("description") or "",
                     " ".join(item.get("topics") or [])]).lower()


def _norm_text(s: str) -> str:
    """分隔符归一：`full-text-search` / `full_text_search` / `full text search` 等价。

    不改这一步，topics 里连字符写法的仓（meilisearch 的 `full-text-search`）永远匹配
    不到关键词 `full text search` —— 这是实测里 6 条「未入池」中 3 条的直接成因。
    """
    return re.sub(r"\s+", " ", re.sub(r"[-_/]+", " ", (s or "").lower())).strip()


def _forms_of(word: str) -> set:
    """单词的形态变体（复数/动名词/名词化），只做可逆的几条规则，不做完整词干化。

    ⚠️ 双写辅音必须去掉：旧实现 `logging[:-3]` 得 `logg`，而 blob 里是 `log`/`logs`，
    **拼写差一个字母就永远不命中**。反过来 `processing` 的 `-ss` 不能去（否则成 `proces`）。
    这也是「关键词 `log` 匹配不到 loki 的 `logs`/`logging`」的根因：变体只在关键词一侧
    生成，blob 侧从来没有展开过。
    """
    out = {word}
    if len(word) > 3 and word.endswith("s") and not word.endswith("ss"):
        out.add(word[:-1])
    if len(word) > 5 and word.endswith("ing"):
        base = word[:-3]
        if len(base) >= 4 and base[-1] == base[-2] and base[-1] not in "lsz":
            base = base[:-1]
        out.add(base)
    if len(word) > 4 and word.endswith("er"):
        out.add(word[:-2])
    if len(word) > 6 and word.endswith("tion"):
        out.add(word[:-3])          # aggregation → aggregat / collection → collect
    if len(word) > 6 and word.endswith("ctor"):
        out.add(word[:-2])          # collector → collect
    return out


def _expand_blob(blob: str) -> str:
    """把形态变体**补进被匹配文本**，让 `log` 也能命中 `logs`/`logging`。

    只往关键词一侧加变体是不够的（实测：`_forms_of('log')` 只有自己，而 loki 的描述写
    `for logs`、topics 写 `logging`）。这里用 `" | "` 分隔：单词 `\\blog\\b` 仍能命中，
    但含空格的短语不会横跨分隔符误配。
    """
    extra = set()
    for tok in blob.split():
        extra |= _forms_of(tok)
    extra -= set(blob.split())
    return (blob + " | " + " | ".join(sorted(extra))) if extra else blob


def alias_groups(keywords: List[str]) -> List[Tuple[List[str], List[str]]]:
    """每个关键词 → (自身形态, 同义说法)。覆盖度按**概念**计，不按词元计。

    这样 `uptime` 命中就能满足 `health` 这个概念：GitHub 上同功能的仓用词
    （health / uptime / monitoring / status page）差异极大，按词元算覆盖度会把
    「只说中一半」的正确仓判成不相关。

    **同义词只给部分学分（`SYN_CREDIT`）**：实测（`docs/REFERENCE-RADAR-QX-20261021.md`）
    给同义词满学分时，`cron` 会被 `scheduler`/`scheduling` 命中，日历类与 k8s 直接
    占住 top5。扩展项本来就比原词弱，学分必须体现这一点。
    """
    groups: List[Tuple[List[str], List[str]]] = []
    for kw in keywords:
        own, syn = {_norm_text(kw)}, set()
        # 短语概念**不做形态展开**：短语的每个词都被塞进 own 之后，一个 `log` 就能让
        # 「任何描述里带 logs 的大仓」把 log aggregation 这个概念打满，精确匹配的小仓
        # （logsuck）反被星数压下去（实测 #5→#22，日志模块 hit 0.25→0）。
        # 单词概念仍双向展开（`scheduler` ↔ `scheduling`），这才是 cron 模块受益的那条路径。
        phrase = " " in str(kw).strip()
        for w in re.split(r"[^a-z0-9+#.]+", str(kw).lower()):
            if not w:
                continue
            own |= ({w} if phrase else _forms_of(w))
            for alias in SYNONYMS.get(w, []):
                syn.add(_norm_text(alias))
        syn -= own
        groups.append((sorted((f for f in own if f), key=len, reverse=True),
                       sorted((f for f in syn if f), key=len, reverse=True)))
    return groups


def _hit(blob: str, form: str) -> bool:
    if " " in form:
        return form in blob
    return re.search(r"\b" + re.escape(form) + r"\b", blob) is not None


#: 同义词命中只算部分学分（原词 = 1.0）。取值来自实测：满学分会把日历/k8s 类仓
#: 拉进 top5；过低（<0.5）则扩出来的正确仓排不回前五。0.7 是两者之间的唯一档。
SYN_CREDIT = 0.7


def _concept_weights(pool: Dict[str, Dict],
                     groups: List[Tuple[List[str], List[str]]]) -> List[float]:
    """概念级 IDF：越少仓命中，区分度越高（与旧词元 IDF 同式，只换了计数单位）。"""
    n = max(1, len(pool))
    out = []
    for own, syn in groups:
        forms = own + syn
        df = sum(1 for r in pool.values()
                 if any(_hit(_expand_blob(_norm_text(_name_blob(r["item"]))), f)
                        for f in forms))
        out.append(1.0 + math.log((n + 1) / (df + 1)))
    return out


def _concept_cov(blob: str, groups: List[Tuple[List[str], List[str]]],
                 w: List[float]) -> Tuple[float, int]:
    """返回（加权覆盖度, 命中的概念数）。同义词最多给 `SYN_CREDIT`，不会盖过原词。"""
    total = sum(w) or 1.0
    score, matched = 0.0, 0
    for (own, syn), wt in zip(groups, w):
        if any(_hit(blob, f) for f in own):
            score += wt
            matched += 1
        elif any(_hit(blob, f) for f in syn):
            score += wt * SYN_CREDIT
            matched += 1
    return score / total, matched


def _days_since(ts: Optional[str]) -> int:
    if not ts:
        return 9999
    try:
        return (datetime.now(timezone.utc)
                - datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return 9999


def _enrich(full: str, branch: str) -> Dict[str, Any]:
    """README / 目录树 / head sha。三路并发，全部走缓存（24h）。"""
    def readme() -> str:
        data, _ = gh_get(f"{GH_API}/repos/{full}/readme", ttl_s=CACHE_TTL_ENRICH)
        if not data or not data.get("content"):
            return ""
        try:
            text = base64.b64decode(data["content"]).decode("utf-8", "ignore")
            return text[:CACHE_README_CHARS]
        except Exception:  # noqa: BLE001
            return ""

    def tree() -> List[str]:
        data, _ = gh_get(f"{GH_API}/repos/{full}/git/trees/{branch}?recursive=1",
                         ttl_s=CACHE_TTL_ENRICH)
        return [n.get("path", "") for n in (data or {}).get("tree", [])
                if n.get("type") in ("blob", "tree")]

    def head() -> str:
        data, _ = gh_get(f"{GH_API}/repos/{full}/git/ref/heads/{branch}",
                         ttl_s=CACHE_TTL_ENRICH)
        return ((data or {}).get("object") or {}).get("sha", "")[:12]

    with futures.ThreadPoolExecutor(max_workers=3) as ex:
        r, t, h = ex.submit(readme), ex.submit(tree), ex.submit(head)
        return {"readme": r.result(), "paths": t.result(), "head": h.result()}


def _gist(readme: str, limit: int = 260) -> str:
    """README 前若干字：去掉徽章/链接/代码围栏，只留能说明「它是干什么的」的正文。

    只给描述时模型分不清「技能注册中心」与「agent 可观测平台」——
    实测（2026-10-21）Observal 被排在 skillhub 之前。README 正文是最便宜的纠偏信号。
    """
    text = re.sub(r"```.*?```", " ", readme or "", flags=re.S)
    text = re.sub(r"!\[[^\]]*\]\([^)]*\)", " ", text)
    text = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", text)
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"[#*_>`|]+", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text[:limit]


# ── 清单展开：把 awesome / HelloGitHub 变成它**收录的项目** ─────────────────
#
# 用户要的是清单里的项目，不是清单本身（2026-10-22 裁定）。根 README 往往只是目录：
# `HelloGitHub` 的根 README 只有 1 个项目链接 + 36 个 `content/HelloGitHubNN.md` 子页，
# 所以只解析根 README 等于没展开。这里按「根 → 子页」两级取链接，再回填仓库元数据。

_GH_LINK_RE = re.compile(r"github\.com[/:]([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9_.-]{1,80})")
#: github.com 下的站点功能页，不是用户仓库（误收会把 features/pricing 当项目去拉 API）。
_GH_RESERVED = {
    "features", "topics", "sponsors", "marketplace", "login", "orgs", "users",
    "settings", "about", "pricing", "apps", "collections", "trending", "search",
    "security", "events", "readme", "contact", "enterprise", "solutions",
    "site", "git-guides", "open-source", "customer-stories", "team", "home",
}
_MD_SELF_RE = re.compile(r"\]\(\s*(?!https?://|#)([^)\s]+?\.md)(?:#[^)]*)?\s*\)")


def _gh_links(text: str, self_full: str = "") -> List[str]:
    """从 markdown 抽 `owner/repo`（保序去重），剔站点功能页与自引用。"""
    self_key = (self_full or "").lower()
    seen, out = set(), []
    for owner, repo in _GH_LINK_RE.findall(text or ""):
        repo = re.sub(r"\.git$", "", repo, flags=re.I).strip(".,;:")
        full = f"{owner}/{repo}"
        key = full.lower()
        if owner.lower() in _GH_RESERVED or key == self_key or len(repo) < 2:
            continue
        if key in seen:
            continue
        seen.add(key)
        out.append(full)
    return out


def _md_subpages(text: str, limit: int) -> List[str]:
    """README 里指向同仓的 .md 相对链接（HelloGitHub 的项目就写在 content/*.md）。"""
    out: List[str] = []
    for p in _MD_SELF_RE.findall(text or ""):
        p = p.strip().lstrip("./")
        if not p or p in out or not p.lower().endswith(".md"):
            continue
        if p.lower().split("/")[0] in ("content", "docs", "data", "posts", "pages", "issues"):
            out.append(p)
    return out[:limit] if limit else []


def _blob_text(url: str) -> str:
    """取 GitHub contents API 的 base64 正文；失败返回空串（不抛）。"""
    data, _ = gh_get(url, ttl_s=CACHE_TTL_ENRICH)
    if not isinstance(data, dict) or not data.get("content"):
        return ""
    try:
        return base64.b64decode(data["content"]).decode("utf-8", "ignore")[:CACHE_README_CHARS * 4]
    except Exception:  # noqa: BLE001
        return ""


def _readme_text(full: str) -> str:
    return _blob_text(f"{GH_API}/repos/{full}/readme")


def _contents_text(full: str, path: str) -> str:
    return _blob_text(f"{GH_API}/repos/{full}/contents/{urllib.parse.quote(path)}")


def _pick_lists(pre: List[Dict[str, Any]], limit: int) -> List[Dict[str, Any]]:
    """挑要展开的清单仓，返回仓库 item 列表（按预分顺序）。

    只认两类，不靠「README 链接多」泛判：
    ① 显式 `list` 桶（名字/描述像 awesome/curated）；
    ② **自有来源**里 README 像目录的仓 —— `HelloGitHub` 的描述没有任何 list 词，
       靠描述永远识别不出来；而它正是用户点名要展开的那种。
    链接多 ≠ 清单：实测泛判会把 `gh-repo-stats`、`powerplatform-actions`
    这类普通仓当清单，白烧掉整份展开预算（96 个项目的 API 额度）。
    """
    out: List[Dict[str, Any]] = []
    seen = set()
    for p in pre:
        item = p["item"]
        full = item.get("full_name") or ""
        if not full or full.lower() in seen:
            continue
        owned = bool(p.get("sources"))
        signals = 0
        if owned and p.get("readme"):
            signals = (len(_gh_links(p["readme"], full))
                       + len(_md_subpages(p["readme"], 100)))
        if p["bucket"] == "list" or (owned and signals >= LIST_SIGNAL_LINKS):
            seen.add(full.lower())
            out.append(item)
        if len(out) >= limit:
            break
    return out


def _expand_lists(lists: List[Dict[str, Any]], min_stars: int, known: set,
                  keep: Optional[Any] = None
                  ) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """把清单仓展开成收录项目，返回 ({key: {item, via}}, 逐清单日志)。

    `known` 是已在候选池里的 key 集合（小写 full_name），用于避免重复拉取。
    `archived` 与 `min_stars` 照常生效：清单里常有已归档项目，拉回来只会占重排名额。
    `keep(item)` 是与主池一致的覆盖度闸门：不该只在 `build_pre` 里过滤 —— 那样
    无关清单会把整份展开额度吃光（实测 `awesome-claude-plugins` 一次占掉 90/96）。
    """
    found: Dict[str, Dict[str, Any]] = {}
    log_rows: List[Dict[str, Any]] = []
    for lst in lists[:LIST_EXPAND_LISTS]:
        full = lst.get("full_name") or ""
        if not full or len(found) >= LIST_EXPAND_TOTAL:
            continue
        t0 = time.monotonic()
        readme = _readme_text(full)
        links = _gh_links(readme, full)
        seen = {x.lower() for x in links}
        pages = 0
        # 根 README 项目少 ⇒ 说明它只是目录（HelloGitHub/月刊类），继续追子页
        if len(links) < 20:
            for path in _md_subpages(readme, LIST_EXPAND_SUBPAGES):
                text = _contents_text(full, path)
                if not text:
                    continue
                pages += 1
                for x in _gh_links(text, full):
                    if x.lower() not in seen and len(links) < LIST_EXPAND_PER_LIST:
                        seen.add(x.lower())
                        links.append(x)
                if len(links) >= LIST_EXPAND_PER_LIST:
                    break
        wanted = [x for x in links if x.lower() not in known
                  and x.lower() not in found][:LIST_EXPAND_TOTAL - len(found)]
        added = 0
        with futures.ThreadPoolExecutor(max_workers=LIST_EXPAND_THREADS) as ex:
            futs = {ex.submit(gh_get, f"{GH_API}/repos/{x}", CACHE_TTL_ENRICH): x for x in wanted}
            for fut in futures.as_completed(futs):
                data, _ = fut.result()
                if not isinstance(data, dict) or not data.get("full_name"):
                    continue
                if data.get("archived"):
                    continue
                if int(data.get("stargazers_count") or 0) < min_stars:
                    continue
                if keep is not None and not keep(data):
                    continue
                key = str(data["full_name"]).lower()
                found.setdefault(key, {"item": data, "via": set()})["via"].add(full)
                added += 1
        log_rows.append({"name": f"list:{full}", "q": "", "ok": bool(readme),
                         "count": added, "links": len(links), "pages": pages,
                         "ms": int((time.monotonic() - t0) * 1000), "cached": False,
                         "error": None if readme else "readme 为空/拉取失败"})
    return found, log_rows




def _llm_text(prompt: str, model: str, timeout: int,
              max_tokens: int = 1800) -> Tuple[str, str, str]:
    """同步调 Anthropic 兼容网关。返回 (text, error, stop_reason)。

    地址/凭据直接取 `llm` 模块常量（同源单点，避免在这里再判一次 `/v1` 拼接）。
    """
    base = (llm.BASE_URL or "").rstrip("/")
    key = llm.API_KEY
    if not key:
        return "", "no-key", ""
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages", data=body,
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                 "Authorization": f"Bearer {key}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        return text, ("" if text else "only-thinking"), str(data.get("stop_reason") or "")
    except urllib.error.HTTPError as exc:
        return "", f"HTTP{exc.code}", ""
    except Exception as exc:  # noqa: BLE001
        return "", type(exc).__name__, ""


#: 免费池把重排 prompt 路由到**内容审核模型**时返回的标签开头。
_SAFETY_REPLY = re.compile(r"^\s*User\s+Safety\s*:", re.I)


def _is_safety_reply(text: str) -> bool:
    """这段正文是内容审核标签，不是重排结果。

    2026-10-21 实测（8 候选、`allow_paid=False`，4 轮）：2 轮返回
    `User Safety: safe`，兜底位返回 `User Safety: unsafe` +
    `Safety Categories: Malware, Needs Caution`。**换同一池的下一个模型
    照样撞**（两轮都中），所以命中即停，不把 95s 预算烧在审核模型上。
    这与「模型不听话」是两回事：路由选错了模型。
    """
    return bool(_SAFETY_REPLY.match(text or ""))


def _json_block(text: str) -> Optional[dict]:
    """从严到松解析模型输出。

    实测（2026-10-21）：8 个候选 + 中文理由会在 2000 tokens 处**从字符串中间截断**，
    严格 `json.loads` 必然失败 —— 于是整轮重排白跑。这里按「最后一个完整对象」打捞：
    截断只丢尾部若干候选，已判出的分数仍可用；一个都不剩才返回 None。
    """
    m = re.search(r"\{.*\}", text or "", re.S)
    seg = m.group(0) if m else (text or "").strip()
    if not seg:
        return None
    try:
        return json.loads(seg)
    except ValueError:
        pass
    cuts = [i + 1 for i, ch in enumerate(seg) if ch == "}"][::-1]
    for cut in cuts[:40]:
        head = seg[:cut]
        for suffix in ("", "]}", "}]}"):
            try:
                obj = json.loads(head + suffix)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get("items"), list):
                return obj
    return None


def _ref_model() -> str:
    """重排用的付费档模型（`REF_LLM_MODEL`，默认 `agnes/agnes-2.5-flash`）。

    为什么不复用 `llm.MODEL`：`MANAGER_LLM_MODEL` 同时服务交互式
    `/api/tasks/decompose`，换它会连带改那条链路；重排只需要「稳定回 JSON、
    不做长 thinking」的模型。

    2026-10-21 实测：`alibaba/qwen3.8-flash` 在重排 prompt 上 60s 必超时
    （短请求也 only-thinking）；`agnes/agnes-2.5-flash` 短请求 2.0s 正常返回，
    全量两轮 hit@5 均为 0.467（基线 0.300）。
    """
    return os.getenv("REF_LLM_MODEL", "agnes/agnes-2.5-flash")


def _sample_count() -> int:
    """付费档采样次数（`REF_RERANK_SAMPLES`，默认 3，夹在 1~5）。

    为什么要采样：同一 prompt 同一模型判词会翻。2026-10-21 同一张卡两轮——
    一轮三个候选全 skip，另一轮 `HelloGitHub` 判 read（func 0.8），
    对应「实现桶整段空」与「有一条可借鉴」两种完全不同的结论。
    """
    try:
        n = int(os.getenv("REF_RERANK_SAMPLES", "3"))
    except ValueError:
        n = 3
    return max(1, min(5, n))


def _median(xs: List[float]) -> float:
    s = sorted(xs)
    return s[len(s) // 2]


def _majority(votes: List[Dict[str, Dict]]) -> Dict[str, Dict]:
    """多次采样取多数：三轴取中位数，judge 取多数票，平票取**更严格**的一档。

    「更严格」＝ `_judge_rank` 更大（skip 最大）——与「没有参考价值就排后」一致：
    两次里一次 read 一次 skip 时按 skip 走，不让一次乐观判定把仓顶上去。
    只拿到少数票的仓直接丢掉（下游记为 unjudged），不拿单票当结论。
    """
    agg: Dict[str, List[Dict[str, Any]]] = {}
    for v in votes:
        for repo, rec in v.items():
            agg.setdefault(repo, []).append(rec)
    if not agg:
        return {}
    need = 2 if len(votes) >= 2 else 1
    out: Dict[str, Dict] = {}
    for repo, recs in agg.items():
        if len(recs) < need:
            continue
        counts: Dict[str, int] = {}
        for r in recs:
            counts[r["judge"]] = counts.get(r["judge"], 0) + 1
        top = max(counts.values())
        judge = max((j for j, c in counts.items() if c == top), key=_judge_rank)
        why = next(r["why"] for r in recs if r["judge"] == judge)
        out[repo] = {"func": _median([r["func"] for r in recs]),
                     "impl": _median([r["impl"] for r in recs]),
                     "design": _median([r["design"] for r in recs]),
                     "judge": judge, "why": why}
    return out


def rerank(module: str, cands: List[Dict[str, Any]], allow_paid: bool,
           budget_s: float = 95.0
           ) -> Tuple[Dict[str, Dict], str, str, List[Dict[str, Any]]]:
    """语义重排。返回 (结果, 失败原因, 用了哪个模型, 逐次尝试记录)。

    `allow_paid` 时**付费档优先**、免费档退居兜底：P0/P1 实测免费两档几乎稳定
    返回 only-thinking（占满超时），若排在前面会吃光预算，付费档根本轮不到。
    总预算 95s 兜住尾延迟：付费档按 `REF_RERANK_SAMPLES`（默认 3）**采样取多数**，
    每次 cap 取「剩余预算 ÷ 剩余次数」（≤60s）；免费档 25s×2 ⇒ 超预算直接放弃并如实标注。
    """
    if not cands:
        return {}, "no-candidates", "", []
    lines = []
    for c in cands:
        files = ", ".join(c.get("sample_paths", [])[:5])
        topics = ", ".join(c.get("topics") or [])
        gist = c.get("readme_gist") or _gist(c.get("readme") or "")
        lines.append(f"- {c['repo']} | ★{c['stars']} | {c['desc'][:100]}"
                     + (f" | topics: {topics}" if topics else "")
                     + (f" | README: {gist}" if gist else "")
                     + f" | 文件: {files}")
    prompt = (
        "你是技术选型调研员。判断候选 GitHub 仓库对目标模块的可借鉴价值。\n"
        "不要因为星多或名字像就给高分。三维度 0~1：func=是否解决同一问题；"
        "impl=实现路径是否可参考；design=架构决策是否有借鉴。\n"
        "只共享一个词、或只是清单/教程 ⇒ skip。**只是相邻领域也要 skip**：\n"
        "例如目标要「技能扫描与调用记账」，则安全扫描、模型可观测性、通用 API 网关"
        "都只是相邻，不能判 read 以上。\n"
        "输出严格 JSON，不要 Markdown：{\"items\":[{\"repo\":\"owner/name\","
        "\"func\":0.0,\"impl\":0.0,\"design\":0.0,\"judge\":\"use|fork|read|skip\","
        "\"why\":\"不超过30字\"}]}\n\n"
        f"目标模块：{module}\n候选：\n" + "\n".join(lines)
    )
    paid = _ref_model() if allow_paid else ""
    free = [m for m in (llm.memory_model(), llm.free_fallback_model()) if m]
    errors: List[str] = []
    attempts: List[Dict[str, Any]] = []
    spent = 0.0

    def _one(model: str, cap: float, mark: Dict[str, Any]
             ) -> Tuple[Optional[Dict[str, Dict]], str]:
        """跑**一次**模型，返回 `(判定表, 失败原因)`；原因空串表示成功。

        `cap` 由调用方给：付费档要按剩余预算均分，免费档固定 25s。
        """
        nonlocal spent
        left = budget_s - spent
        if left < 8:
            errors.append(f"{model}:budget-exhausted")
            return None, "budget-exhausted"
        t0 = time.monotonic()
        text, err, stop = _llm_text(prompt, model, timeout=int(min(cap, left)),
                                    max_tokens=4000)
        spent += time.monotonic() - t0
        rec = {"model": model, "ms": int((time.monotonic() - t0) * 1000),
               "ok": bool(text), "error": err or "", "stop_reason": stop}
        rec.update(mark)
        attempts.append(rec)
        if not text:
            errors.append(f"{model}:{err or 'empty'}")
            return None, err or "empty"
        if _is_safety_reply(text):
            attempts[-1].update({"ok": False, "error": "safety-router",
                                 "head": text.strip()[:80]})
            errors.append(f"{model}:safety-router")
            return None, "safety-router"
        data = _json_block(text)
        if not data or not isinstance(data.get("items"), list):
            attempts[-1].update({"ok": False, "error": "bad-json",
                                 "head": (text or "")[:120]})
            errors.append(f"{model}:bad-json")
            return None, "bad-json"
        # GitHub 的 `full_name` 大小写是规范的（robfig/cron），模型常会自行改写成
        # `Robfig/Cron`。按原样比对会把这条判定直接丢掉；全丢光还会误报
        # `degenerate`（实测）。这里用 lower() 做键，回填规范名。
        known = {str(c["repo"]).lower(): c["repo"] for c in cands}
        out: Dict[str, Dict] = {}
        for item in data["items"]:
            canon = known.get(str(item.get("repo", "")).strip().lower())
            if not canon:
                continue
            try:
                out[canon] = {"func": min(1.0, max(0.0, float(item["func"]))),
                              "impl": min(1.0, max(0.0, float(item["impl"]))),
                              "design": min(1.0, max(0.0, float(item["design"]))),
                              "judge": str(item.get("judge", "read")).strip().lower()[:8],
                              "why": str(item.get("why", ""))[:80]}
            except (KeyError, TypeError, ValueError):
                continue
        if not out:
            # 只把「一个仓库都没对上」当失败。**三轴全 0 不算**：那是模型明确判
            # 全体 skip 的合法结论（2026-10-21 生产实测：三个候选全是 awesome 清单，
            # 模型给全 0 + skip，旧实现按 degenerate 丢掉，等于把「没价值」改判成
            # 「模型没看懂」，再让免费档重烧一遍预算）。
            attempts[-1].update({"ok": False, "error": "empty-verdict"})
            errors.append(f"{model}:empty-verdict")
            return None, "empty-verdict"
        attempts[-1]["items"] = len(out)
        # 逐次判词留档：只记条数看不出「3 次里几次 skip」，判词翻转就无从复核。
        # 卡面 attempts 里能直接看到每次对同一仓给了什么档。
        attempts[-1]["judges"] = {k: v["judge"] for k, v in out.items()}
        return out, ""

    # 付费档：同一 prompt 采样 N 次取多数（2026-10-22 用户裁定）。
    # 「失败补一次」被采样吸收——单次超时只是少一票，不必再单独重试。
    samples = _sample_count() if paid else 1
    if paid:
        votes: List[Dict[str, Dict]] = []
        for i in range(samples):
            left = budget_s - spent
            if left < 8:
                errors.append(f"{paid}:budget-exhausted")
                break
            # 均分剩余预算：前面超时不会把后面的票全吃掉；单次仍不超过 60s。
            cap = min(60.0, max(15.0, left / max(1, samples - i)))
            out, why = _one(paid, cap, {"sample": i + 1})
            if out:
                votes.append(out)
            elif why == "safety-router":
                break          # 内容被审核模型截住，多采几次也一样
        got = _majority(votes)
        if got:
            return got, "", paid, attempts

    # 免费档只跑一次：实测每次都占满超时，采样只是把预算烧得更快。
    for model in free:
        out, why = _one(model, 25, {})
        if out:
            return out, "", model, attempts
        if why == "safety-router":
            break          # 同一免费池换模型只会再撞一次（实测 2/2）
    return {}, "; ".join(errors), "", attempts


# ── 采集主流程（同步，跑在线程池里）──────────────────────────────────────────

def _judge_rank(judge: str) -> int:
    """use/fork < read < 未判定 < skip。未知必须排在「已判 skip」之前、
    「已判可读」之后 —— 把未知混进可借鉴区就是替模型编结论。

    2026-10-21 修正：旧实现把 `skip` 与任意未知字符串并列在 2，又把
    `unjudged` 单列成 3 ⇒ **未判定反而排在被明确否决的仓之后**，与本函数
    注释相反。重排只覆盖词法前 8 名，尾部候选和「模型只返回一部分」的候选
    全是 `unjudged`，卡面等于「没看过的仓先被压到已否决之下」。
    现改为 skip=3、其余未知=2，档位严格是 use/fork < read < 未判定 < skip。
    """
    return {"use": 0, "fork": 0, "read": 1, "skip": 3}.get(str(judge or "").strip().lower(), 2)


def apply_rerank(module: str, ranked: List[Dict[str, Any]],
                 allow_paid: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    """就地改写 `ranked` 的三轴分与排序。返回 (失败原因, 模型, 错误清单)。

    **独立成函数**是为了「先出词法卡、再原地升级」：任务层在重排前已经把卡落库，
    这里失败只影响状态字段，不会让用户没有结果可看。
    """
    # 只从**补全过**的候选里取：补全集合必须包含重排集合，否则会拿到「没抓 README」
    # 的仓——实现轴少一个信号，卡上也没有摘要（2026-10-22 实测 rsain/enesusta）。
    impls = [c for c in ranked if c["bucket"] == "impl" and c.get("enriched")][:MAX_CANDIDATES]
    got, err, used_model, attempts = rerank(module, impls, allow_paid)
    if not got:
        return err, "", attempts
    judged = set(got)
    sliced = {c["repo"] for c in impls}
    for c in ranked:
        if c["bucket"] == "impl" and c["repo"] not in judged:
            # 截断的尾部 / 没进前 8 的候选都没有判定：**未知不等于可借鉴**，单独成档。
            c["judge"] = "unjudged"
            truncated = any(a.get("stop_reason") == "max_tokens" for a in attempts)
            c["why"] = ("输出被截断（max_tokens），本条未判" if (truncated and c["repo"] in sliced)
                        else "模型未返回该条判定" if c["repo"] in sliced
                        else "未进入语义重排队列（补全窗口之外）")
    for c in ranked:
        r = got.get(c["repo"])
        if not r:
            continue
        c.update({"judge": r["judge"], "why": r["why"], "llm": r})
        c["func"] = round(0.55 * c["func"] + 0.45 * r["func"], 3)
        c["impl"] = round(0.55 * c["impl"] + 0.45 * r["impl"], 3)
        c["design"] = round(0.55 * c["design"] + 0.45 * r["design"], 3)
    ranked.sort(key=lambda x: (x["bucket"] != "impl", _judge_rank(x["judge"]),
                               -(0.5 * x["func"] + 0.3 * x["impl"] + 0.2 * x["design"])))
    return "", used_model, attempts


def _order_key(c: Dict[str, Any]) -> Tuple[int, float, float]:
    """排序主键：**参考价值优先**，来源只作同分次序。

    0.06 的 `src_bonus` 曾经**加进**加权分里，结果是「我的仓」能压过功能分略高的外部仓
    —— 与「没有参考价值就排后」直接相反（2026-10-21 用户裁定）。现在它只进第三位：
    三轴分不同就按分排，分数**完全相等**时才让自己的仓靠前。
    """
    return (c["bucket"] != "impl",
            -(0.6 * c["func"] + 0.3 * c["impl"] + 0.1 * c["design"]),
            -c.get("src_bonus", 0.0))


def collect(module: str, keywords: List[str], lang: str, min_stars: int,
            fresh_days: int, identifiers: List[str], allow_paid: bool,
            stage: Optional[Any] = None, do_rerank: bool = True) -> Dict[str, Any]:
    """跑一次采集，返回 {meta, candidates, status, elapsed}。

    `stage(name)` 由任务层注入，用于把进度落到 `ref_jobs.stage`。
    """
    def mark(name: str) -> None:
        if stage:
            stage(name)

    t0 = time.monotonic()
    arms_log: List[Dict[str, Any]] = []
    pool: Dict[str, Dict[str, Any]] = {}

    mark("search")
    for name, q, sort in build_queries(keywords, module, min_stars, fresh_days):
        url = (f"{GH_API}/search/repositories?q={urllib.parse.quote(q)}"
               f"&sort={sort}&order=desc&per_page={SEARCH_PER_PAGE}")
        data, meta = gh_get(url, ttl_s=CACHE_TTL_SEARCH, limiter=_SEARCH_LIMIT)
        items = (data or {}).get("items", []) if data else []
        arms_log.append({"name": name, "q": q, "ok": meta.get("ok", False),
                         "count": len(items), "ms": meta.get("ms", 0),
                         "cached": meta.get("cached", False),
                         "error": meta.get("error"),
                         "total": (data or {}).get("total_count"),
                         "rate_limited": meta.get("rate_limited", False)})
        for it in items:
            key = (it.get("full_name") or "").lower()
            if not key:
                continue
            rec = pool.setdefault(key, {"item": it, "seen": set()})
            rec["seen"].add(name)

    code_hits: Dict[str, List[str]] = {}
    for ident in identifiers[:2]:
        q = urllib.parse.quote('"%s" in:file' % ident)
        url = f"{GH_API}/search/code?q={q}&per_page=5"
        data, meta = gh_get(url, ttl_s=CACHE_TTL_SEARCH, limiter=_CODE_LIMIT)
        items = (data or {}).get("items", []) if data else []
        arms_log.append({"name": f"code:{ident}", "q": ident, "ok": meta.get("ok", False),
                         "count": len(items), "ms": meta.get("ms", 0),
                         "cached": meta.get("cached", False),
                         "error": meta.get("error"),
                         "rate_limited": meta.get("rate_limited", False)})
        for it in items:
            full = (it.get("repository") or {}).get("full_name", "")
            if not full:
                continue
            key = full.lower()
            code_hits.setdefault(key, []).append(it.get("path", ""))
            if key not in pool:
                data2, _ = gh_get(f"{GH_API}/repos/{full}", ttl_s=CACHE_TTL_ENRICH)
                if data2 and data2.get("full_name"):
                    pool[key] = {"item": data2, "seen": {"code"}}

    # 自有来源并入同一个池：不过滤 stars/fork（自有仓多为 0 star 或 fork），
    # 但 archived 在 _sync_sources 里已剔除，后续过滤/打分与搜索结果完全同规则。
    mark("sources")
    src_log, src_overview, src_by_key = _sync_sources()
    arms_log.extend(src_log)
    for key, srec in src_by_key.items():
        rec = pool.setdefault(key, {"item": srec["item"], "seen": set()})
        rec["seen"].update("src:" + lb for lb in srec["labels"])
        rec["sources"] = sorted(set(rec.get("sources") or []) | set(srec["labels"]))

    toks = tokens_of(keywords)
    groups = alias_groups(keywords)

    def build_pre() -> List[Dict[str, Any]]:
        """按**当前池**算覆盖度与预分并排序。

        独立成函数是因为「清单展开」会把新项目塞进池：IDF 与覆盖度都随之变化，
        不重算就等于用旧权重给新候选打分。
        """
        weights = _concept_weights(pool, groups)
        out: List[Dict[str, Any]] = []
        for key, rec in pool.items():
            item = rec["item"]
            if item.get("archived"):
                continue
            bucket = classify(item)
            # 变体要补在**被匹配文本**这一侧，否则「关键词 log / 文档 logs」永远不撞
            blob = _expand_blob(_norm_text(_name_blob(item)))
            cov, matched = _concept_cov(blob, groups, weights)
            phrase = any((" " in str(k) and _norm_text(str(k)) in blob) for k in keywords)
            hits = len(code_hits.get(key, []))
            srcs = rec.get("sources") or []
            if bucket == "impl" and cov < 0.18 and hits == 0:
                continue
            # 「至少命中 2 个概念」不能写成常数 2：概念数来自用户关键词，只有 1 个关键词
            # （或 LLM 抽词只回一条）时 `matched` 上限就是 1，这条过滤会**恒真**，
            # 描述完全命中的仓也被丢光。实测 `keywords=["cron"]` 对 "a cron library for go"
            # 返回 0 条实现桶。门槛必须取 min(2, 概念数)。
            if bucket == "impl" and matched < min(2, len(groups)) and not phrase and hits == 0:
                continue
            # 自有来源里与原模块无关的仓**不占版面**：资料/清单桶对搜索候选不设覆盖度门槛
            # （那是「用户主动搜 wide topic 时也能看到清单」的旧口径），但自有来源动辄几十个
            # fork，没有门槛时 `free-programming-books` 这类 0 覆盖的仓会一直挂在卡上。
            if srcs and matched == 0 and cov < 0.18 and not phrase and hits == 0:
                continue
            stars = int(item.get("stargazers_count") or 0)
            out.append({"key": key, "item": item, "bucket": bucket, "cov": cov, "hits": hits,
                        "seen": rec["seen"], "sources": srcs,
                        "via": rec.get("via") or set(),
                        "pre": 0.55 * cov + 0.20 * min(1, math.log10(stars + 1) / 4)
                        + 0.25 * min(1, hits / 3) + (SOURCE_BONUS if srcs else 0)})
        out.sort(key=lambda x: (x["bucket"] != "impl", -x["pre"]))
        return out

    pre = build_pre()

    def enrich_top(target: List[Dict[str, Any]]) -> None:
        """补全前 `ENRICH_LIMIT` 个实现候选（已补过的跳过）。"""
        n = 0
        for p in target:
            if p["bucket"] != "impl" or p.get("enriched"):
                continue
            if n >= ENRICH_LIMIT:
                break
            n += 1
            p["enriched"] = True
            branch = p["item"].get("default_branch") or "main"
            p["branch"] = branch
            p.update(_enrich(p["item"]["full_name"], branch))

    mark("enrich")
    # 补全窗口必须 > 重排窗口：否则重排会拿到没抓 README 的仓（实现轴缺信号、卡上没摘要）。
    enrich_top(pre)

    # ── 清单展开：awesome / HelloGitHub 只是目录，用户要的是目录里的项目 ──
    # 为什么放在补全之后：`HelloGitHub` 的描述里没有 awesome/curated，按 `classify`
    # 是 impl；它「是清单」的证据在 README 里（1 个项目链接 + 36 个 content 子页），
    # 不先抓 README 就根本识别不出来。展开结果再走同一套覆盖度/三轴/许可证闸门。
    expanded_log: List[Dict[str, Any]] = []
    # 清单仓本身是「目录」，不是「可借鉴的项目」：用户 2026-10-22 明确
    # 「要的是 HelloGitHub 收录的项目，不是 HelloGitHub 本身」。这里记下被展开的目录，
    # 渲染时把它们从候选表里摘掉（不进实现桶表、不占 limit 名额），只留作来源标注。
    list_sources: List[str] = []
    if os.getenv("REF_LIST_EXPAND", "1") != "0":
        lists = _pick_lists(pre, LIST_EXPAND_LISTS)
        list_sources = [str(x.get("full_name") or "") for x in lists if x.get("full_name")]
        if lists:
            mark("expand")
            w0 = _concept_weights(pool, groups)

            def keep(item: Dict[str, Any]) -> bool:
                blob = _expand_blob(_norm_text(_name_blob(item)))
                return _concept_cov(blob, groups, w0)[0] >= 0.18

            found, expanded_log = _expand_lists(lists, min_stars, set(pool), keep)
            for key, rec in found.items():
                pool[key] = {"item": rec["item"], "seen": set(), "via": rec["via"]}
            if found:
                pre = build_pre()      # 池变大 ⇒ IDF/覆盖度重算
                enrich_top(pre)        # 收录项目也可能进重排，同样要补全

    ranked: List[Dict[str, Any]] = []
    for p in pre:
        item, bucket = p["item"], p["bucket"]
        stars = int(item.get("stargazers_count") or 0)
        lang_ok = 1.0 if (not lang or (item.get("language") or "").lower() == lang.lower()) else 0.0
        text = (p.get("readme") or "").lower()
        paths = p.get("paths") or []
        low_paths = [x.lower() for x in paths[:6000]]
        arch_docs = any(("adr" in x or "architecture" in x) for x in low_paths)
        arch_signal = min(1.0, sum(text.count(t) for t in ARCH_TERMS) / 12.0)
        path_hits = sum(1 for x in low_paths if any(t in x for t in toks))
        impl_path = min(1.0, path_hits / 25.0)
        desc_l = (item.get("description") or "").lower()
        neg = sum(1 for t in toks
                  if re.search(rf"\b(no|without|not)\b[^.]{{0,40}}{re.escape(t)}", desc_l))
        name_l = (item.get("name") or "").lower()
        name_bonus = 0.08 if sum(1 for t in toks if t in name_l) >= 2 else 0.0
        func = (0.70 * p["cov"] + 0.20 * min(1, math.log10(stars + 1) / 4)
                + 0.10 * lang_ok + name_bonus - 0.25 * min(2, neg))
        impl = 0.35 * lang_ok + 0.35 * impl_path + 0.30 * p["cov"]
        design = 0.60 * (1.0 if arch_docs else 0.0) + 0.40 * arch_signal
        lic = (item.get("license") or {}).get("spdx_id") or "UNKNOWN"
        ranked.append({
            "repo": item.get("full_name"), "url": item.get("html_url"),
            "desc": (item.get("description") or "")[:160], "bucket": bucket,
            "seen": sorted(p["seen"]),
            "sources": p["sources"],
            "via": sorted(p.get("via") or []),
            "enriched": bool(p.get("enriched")),
            "src_bonus": SOURCE_BONUS if p["sources"] else 0.0,
            "func": round(func, 3), "impl": round(impl, 3), "design": round(design, 3),
            "coverage": round(p["cov"], 3), "stars": stars, "license": lic,
            "copyleft": bool(GPL_RE.search(lic)), "fresh_days": _days_since(item.get("pushed_at")),
            "arch_docs": arch_docs,
            "branch": p.get("branch") or item.get("default_branch") or "main",
            "head": p.get("head", ""), "code_files": code_hits.get(p["key"], [])[:3],
            "topics": (item.get("topics") or [])[:8],
            "readme_gist": _gist(p.get("readme") or ""),
            "sample_paths": [x for x in paths if "/" in x][:6],
            "judge": "", "why": "", "llm": None,
        })

    ranked.sort(key=_order_key)
    rerank_err, used_model = "", ""
    if do_rerank and ranked:
        mark("rerank")
        rerank_err, used_model, _errors = apply_rerank(module, ranked, allow_paid)
        if not used_model:
            arms_log.append({"name": "llm-rerank", "q": "", "ok": False, "count": 0,
                             "ms": 0, "error": rerank_err or "empty/invalid JSON",
                             "cached": False})
    failed = [a for a in arms_log if not a["ok"] and not a["name"].startswith("llm-")]
    meta = {
        "arms": arms_log,
        "pool_size": len(pool),
        "sources": src_overview,
        "list_expand": expanded_log,
        "list_sources": list_sources,
        "rerank": bool(used_model),
        "rerank_model": used_model,
        "rerank_paid": bool(used_model and used_model not in
                            (llm.memory_model(), llm.free_fallback_model())),
        "rerank_error": rerank_err,
        "degraded": [a["name"] + ":" + str(a.get("error") or "fail") for a in failed],
        "all_arms_failed": bool(arms_log) and all(not a["ok"] for a in arms_log
                                                  if not a["name"].startswith("llm-")),
        "cache": cache_stats(),
        "token": bool(token()),
    }
    status = "reranked" if used_model else ("lexical_fallback" if rerank_err else "lexical")
    elapsed = round(time.monotonic() - t0, 1)
    mark("render")
    return {"meta": meta, "candidates": ranked, "status": status, "elapsed": elapsed}


# ── 卡片渲染 ────────────────────────────────────────────────────────────────

def _badge(days: int) -> str:
    return "新" if days <= 90 else ("温" if days <= 365 else "旧")


def render_md(module: str, keywords: List[str], lang: str, meta: Dict[str, Any],
              ranked: List[Dict[str, Any]], elapsed: float, as_of: str,
              next_refresh: str, limit: int = 8,
              diff: Optional[Dict[str, Any]] = None) -> str:
    out = [f"# 参考情报：{module}", "",
           f"- as of：{as_of}　下次刷新：{next_refresh}",
           f"- 关键词：{', '.join(keywords)}　语言：{lang or '不限'}",
           f"- 候选池：{meta['pool_size']}　耗时：{elapsed}s"]
    # 关键词直接决定召回，但用户看不到它是自己填的、模型抽的还是词典兜底。
    # 抽词失败退词典时结果往往「整体跑偏」，不说清楚就会被当成检索不准。
    src = meta.get("keyword_source")
    if src == "llm":
        out.append("- 关键词来源：模型从模块名抽取（可能抽偏，可手填覆盖）")
    elif src == "fallback":
        out.append("- 关键词来源：**模型抽词失败，退回内置中文词典**（覆盖有限，建议手填关键词重跑）")
    if meta.get("identifiers"):
        out.append("- 代码标识符臂：" + "、".join(meta["identifiers"])
                   + "（在代码里搜该串，命中的仓额外加分）")
    srcs = meta.get("sources") or []
    if srcs:
        ok_src = [s for s in srcs if s.get("ok")]
        if ok_src:
            out.append("- 来源（自有仓，含 fork，不占搜索额度）：" +
                       "、".join(f"{s['label']}（{s['count']} 仓）" for s in ok_src))
        bad_src = [s for s in srcs if not s.get("ok")]
        if bad_src:
            out.append("- ⚠️ 来源拉取失败：" +
                       "、".join(f"{s['label']}（{s.get('error') or '失败'}）" for s in bad_src))
    expand = meta.get("list_expand") or []
    if expand:
        parts = []
        for e in expand:
            name = str(e.get("name") or "").replace("list:", "")
            if not e.get("ok"):
                parts.append(f"{name}（失败）")
            else:
                parts.append(f"{name}（{e.get('pages', 0)} 页 / {e.get('links', 0)} 链接"
                             f" → 入池 {e['count']}）")
        out.append("- 清单展开：" + "；".join(parts)
                   + "（清单仓本身是目录，不计入候选）")
    if meta.get("rerank"):
        cost = "（**付费档**，单次约几千 token）" if meta.get("rerank_paid") else "（免费档）"
        out.append(f"- 语义重排：{meta['rerank_model']}{cost}")
    elif meta.get("rerank_error"):
        out.append(f"- 语义重排：**未完成**，本卡仅词法分（原因：{meta['rerank_error']}）")
        if "safety-router" in str(meta["rerank_error"]) and not meta.get("requested_paid"):
            out.append("- ↳ 免费池被内容审核模型拦截（不是超时）；"
                       "勾选「允许付费重排」后点「重跑」可恢复语义判定")
    else:
        out.append("- 语义重排：无（仅词法，判定列为空）")
    if meta.get("trigger") == "auto":
        out.append("- 触发：定时自动刷新（**不付费**；需要语义重排请手动「重跑」）")
    if meta.get("degraded"):
        out.append("- ⚠️ 降级臂：" + "；".join(meta["degraded"]))
    if not meta.get("token"):
        out.append("- ⚠️ 无 GitHub token：限额 60 次/时，结果可能不完整")
    out.append("")

    impls = [c for c in ranked if c["bucket"] == "impl"]
    others = [c for c in ranked if c["bucket"] != "impl"]
    # 目录仓不进候选表（见 collect 里 `list_sources` 的注释）：它的 impl 分是从
    # 目录 README 算出来的，跟「这个项目怎么实现」无关，留着只会占掉 limit 名额。
    lset = {str(x).lower() for x in (meta.get("list_sources") or [])}
    if lset:
        impls = [c for c in impls if str(c.get("repo") or "").lower() not in lset]
        others = [c for c in others if str(c.get("repo") or "").lower() not in lset]
    if meta.get("rerank"):
        picked = [c for c in impls if c.get("judge") in ("use", "fork", "read")]
        low = [c for c in impls if c.get("judge") not in ("use", "fork", "read")]
    else:
        picked, low = impls, []

    out += ["## 候选（实现桶）", "",
            "| 仓库 | 来源 | ★ | 最近推送 | 许可证 | 功能 | 实现 | 设计 | 判定 |",
            "|---|---|---:|---|---|---:|---:|---:|---|"]
    for c in picked[:limit]:
        lic = c["license"] + ("(copyleft)" if c["copyleft"] else "")
        src_cell = "、".join(c.get("sources") or [])
        if c.get("via"):
            short = str(c["via"][0]).split("/")[-1]
            src_cell = (src_cell + " · " if src_cell else "") + f"清单:{short}"
        out.append(f"| [{c['repo']}]({c['url']}) | "
                   f"{src_cell or '—'} | {c['stars']} | "
                   f"{_badge(c['fresh_days'])} | {lic} | {c['func']:.2f} | "
                   f"{c['impl']:.2f} | {c['design']:.2f} | {c.get('judge') or '—'} |")
    # 自有来源单独再列一次：来源的目的就是「先看我自己的仓」，而上表是统一排序 +
    # limit 截断，自己的仓很容易被 1k+ star 的外部仓挤到第 9 名之后而《看不见》。
    # 这里不做二次打分，只保证可见性；已在上表的标出来，避免被当成两条。
    if meta.get("sources"):
        shown = {id(c) for c in picked[:limit]}
        own = [c for c in ranked if c.get("sources")]
        out += ["", "## 自有来源命中（含 fork）", ""]
        if own:
            for c in own[:12]:
                tail = "（已在上表）" if id(c) in shown else ""
                # 目录仓这里仍要露脸（「我 star 过它、它被当清单展开过」是有用信息），
                # 但桶名要照实写「清单」，否则和上面「不计入候选」自相矛盾。
                kind = "清单（已展开）" if str(c["repo"]).lower() in lset else c["bucket"]
                out.append(f"- [{c['repo']}]({c['url']}) — {kind}，功能 "
                           f"{c['func']:.2f}{tail}：{c['desc']}")
        else:
            out.append("- （无：本轮关键词未命中自有仓；可补关键词，或确认来源已同步）")
    out += ["", "## 资料/清单桶（不参与实现对比）", ""]
    out += [f"- [{c['repo']}]({c['url']}) — {c['desc']}" for c in others] or ["- （无）"]
    if low:
        out += ["", "## 未入选（语义判定 skip / 未进重排）", ""]
        for c in low[:10]:
            out.append(f"- {c['repo']}（{c.get('judge') or '—'}）："
                       f"{c.get('why') or '未进入重排队列'}")
    if diff and (diff.get("added") or diff.get("removed")):
        out += ["", "## 本次刷新变化", ""]
        for r in diff.get("added", [])[:10]:
            out.append(f"- 新增：{r}")
        for r in diff.get("removed", [])[:10]:
            out.append(f"- 移除：{r}")
    out += ["", "## 可借鉴点（均需人工复核）", ""]
    for c in picked[:limit]:
        out.append(f"### {c['repo']} · 功能{c['func']:.2f}/实现{c['impl']:.2f}/设计{c['design']:.2f}")
        out.append(f"- 一句话：{c['desc']}")
        # README 首段早就抓了（重排用它纠偏），但一直没落到卡上 ⇒ 用户只能看一句
        # GitHub 描述判断「值不值得点进去」。这里把**已提取的摘要**显出来，不改抓取逻辑。
        if c.get("readme_gist"):
            out.append(f"- 摘要：{c['readme_gist']}")
        if c.get("sources"):
            out.append("- 来源：自有仓（" + "、".join(c["sources"])
                       + "，不走搜索额度、含 fork）")
        if c.get("via"):
            out.append("- 收录于清单：" + "、".join(c["via"]) + "（本项目由清单展开得到）")
        if c.get("why"):
            out.append(f"- 重排理由：{c['why']}（{c.get('judge')}）")
        out.append(f"- 设计资料：{'有 ADR/architecture 目录' if c['arch_docs'] else '未见 ADR'}")
        if c.get("head"):
            out.append(f"- 固定引用：`{c['repo']}@{c['head']}`（分支 {c['branch']}）")
        for p in c.get("code_files") or []:
            out.append(f"- 代码命中：[{p}](https://github.com/{c['repo']}/blob/"
                       f"{c['branch']}/{p})")
        if c["copyleft"]:
            out.append("- ⚠️ copyleft：只允许借鉴设计，**不得摘录代码**")
        elif c["license"] in ("UNKNOWN", "NOASSERTION"):
            out.append("- ⚠️ 许可证未识别：默认不得复用代码，需人工核实")
        out.append("")
    out += ["## 逐路表态", ""]
    for a in meta.get("arms", []):
        state = "ok" if a["ok"] else f"FAIL {a.get('error') or ''}"
        cached = "（缓存）" if a.get("cached") else ""
        out.append(f"- {a['name']}：{a['count']} 条 / {a['ms']}ms{cached} / {state}")
    return "\n".join(out) + "\n"


# ── 任务层 ──────────────────────────────────────────────────────────────────

def _job_row(job_id: str) -> Optional[Dict[str, Any]]:
    rows = db.query("SELECT * FROM ref_jobs WHERE id=?", (job_id,))
    return rows[0] if rows else None


def _set_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ", ".join(f"{k}=?" for k in fields)
    db.execute(f"UPDATE ref_jobs SET {cols} WHERE id=?", tuple(fields.values()) + (job_id,))


def _diff_candidates(old: Dict[str, Any], new: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        old_list = json.loads(old.get("candidates") or "[]")
    except ValueError:
        old_list = []
    old_repos = {c.get("repo") for c in old_list}
    new_repos = {c.get("repo") for c in new}
    changed = []
    old_map = {c.get("repo"): c for c in old_list}
    for c in new:
        o = old_map.get(c["repo"])
        if not o:
            continue
        d = abs(float(c.get("func", 0)) - float(o.get("func", 0)))
        if d >= 0.15:
            changed.append(f"{c['repo']} 功能分 {o.get('func')}→{c.get('func')}")
    return {"added": sorted(new_repos - old_repos), "removed": sorted(old_repos - new_repos),
            "changed": changed[:10]}


def _card_upsert(card_id: str, module: str, keywords: List[str], lang: str,
                 status: str, as_of: str, next_refresh: str, elapsed: float,
                 meta: Dict[str, Any], ranked: List[Dict[str, Any]], md: str,
                 diff: Optional[Dict[str, Any]] = None) -> None:
    now = _now()
    exists = db.query("SELECT id FROM ref_cards WHERE id=?", (card_id,))
    if exists:
        db.execute(
            "UPDATE ref_cards SET status=?,as_of=?,next_refresh=?,elapsed_s=?,meta=?,"
            "candidates=?,diff=?,md=?,updated_at=? WHERE id=?",
            (status, as_of, next_refresh, elapsed, json.dumps(meta, ensure_ascii=False),
             json.dumps(ranked, ensure_ascii=False),
             json.dumps(diff or {}, ensure_ascii=False), md, now, card_id))
    else:
        db.execute(
            "INSERT INTO ref_cards(id,module,keywords,lang,status,as_of,next_refresh,"
            "elapsed_s,auto_refresh,meta,candidates,diff,md,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)",
            (card_id, module, json.dumps(keywords, ensure_ascii=False), lang, status,
             as_of, next_refresh, elapsed, json.dumps(meta, ensure_ascii=False),
             json.dumps(ranked, ensure_ascii=False),
             json.dumps(diff or {}, ensure_ascii=False), md, now, now))


def _run_job(job_id: str) -> None:
    job = _job_row(job_id)
    if not job:
        return
    payload = json.loads(job["payload"] or "{}")
    card_id = job["card_id"] or uuid.uuid4().hex[:12]
    _set_job(job_id, status="running", stage="parse", started_at=_now())
    if not job["card_id"]:
        _set_job(job_id, card_id=card_id)
    t_job = time.monotonic()
    try:
        module = job["module"]
        keywords = payload.get("keywords") or []
        lang = payload.get("lang") or ""
        src = "given"
        if not keywords:
            # 未给关键词：先试 LLM 抽词，失败退中文词典（词典命中才可用）。
            text, err, _stop = _llm_text(
                "把下面的中文功能模块描述转成 GitHub 检索关键词。输出严格 JSON："
                "{\"keywords\":[\"英文短语\",...]}，3~5 个，只含技术名词，不要解释。\n"
                f"模块：{module}", model=llm.memory_model(), timeout=25, max_tokens=1500)
            data = _json_block(text) or {}
            keywords = [str(k).strip() for k in data.get("keywords", []) if str(k).strip()][:5]
            src = "llm" if keywords else "fallback"
            if not keywords:
                keywords = [v for k, v in FALLBACK_CN.items() if k in module] or [module]
        prev = None
        if payload.get("refresh_of"):
            rows = db.query("SELECT * FROM ref_cards WHERE id=?", (payload["refresh_of"],))
            prev = rows[0] if rows else None
        # 先只跑检索与词法分：**卡立即可用**，重排随后原地升级。
        # P0 实测重排可能吃掉 60~80s，把它放在首屏之前等于让用户空等。
        got = collect(module, keywords, lang, int(payload.get("min_stars", 20)),
                      int(payload.get("fresh_days", 180)),
                      payload.get("identifiers") or [],
                      bool(payload.get("allow_paid")),
                      stage=lambda s: _set_job(job_id, stage=s), do_rerank=False)
        meta = got["meta"]
        meta["keyword_source"] = src
        meta["identifiers"] = payload.get("identifiers") or []
        # 记住「本轮是否**请求**了付费」，而不是「是否**用上**了付费」：付费档失败
        # 回落到免费档时 rerank_paid=False，若只看它会丢掉用户的付费意图，之后任何
        # 重跑都永久降级成免费。免费档并非必失败（2026-10-21 线上实测有成功轮），
        # 但会被内容审核路由**偶发**劫持且慢约 6 倍（9.2s vs 54.6s），不能当默认。
        meta["requested_paid"] = bool(payload.get("allow_paid"))
        meta["trigger"] = payload.get("trigger") or "manual"
        as_of = _now()
        next_refresh = _plus_days(CARD_TTL_DAYS)
        diff = _diff_candidates(prev, got["candidates"]) if prev else None
        md = render_md(module, keywords, lang, meta, got["candidates"], got["elapsed"],
                       as_of, next_refresh, diff=diff)
        _card_upsert(card_id, module, keywords, lang, "lexical", as_of, next_refresh,
                     got["elapsed"], meta, got["candidates"], md, diff)
        _set_job(job_id, stage="rerank")
        t_rr = time.monotonic()
        err, used, attempts = apply_rerank(module, got["candidates"],
                                           bool(payload.get("allow_paid")))
        meta["rerank_attempts"] = attempts
        meta["rerank"] = bool(used)
        meta["rerank_model"] = used
        meta["rerank_paid"] = bool(
            used and used not in (llm.memory_model(), llm.free_fallback_model()))
        meta["rerank_error"] = err
        if not used:
            meta["arms"].append({"name": "llm-rerank", "q": "", "ok": False, "count": 0,
                                 "ms": 0, "error": err or "empty/invalid JSON",
                                 "cached": False})
        status = "reranked" if used else ("lexical_fallback" if err else "lexical")
        total = round(time.monotonic() - t_job, 1)
        meta["lexical_elapsed"] = got["elapsed"]
        meta["rerank_elapsed"] = round(time.monotonic() - t_rr, 1)
        md = render_md(module, keywords, lang, meta, got["candidates"], total,
                       as_of, next_refresh, diff=diff)
        _card_upsert(card_id, module, keywords, lang, status, as_of, next_refresh,
                     total, meta, got["candidates"], md, diff)
        _set_job(job_id, stage="done")
        if meta.get("all_arms_failed"):
            # 全军覆没 ≠ 「没有相关项目」：卡留着（可看到逐路失败），任务标失败。
            _set_job(job_id, status="failed", stage="done", finished_at=_now(),
                     error="全部检索臂失败（见卡内逐路表态）")
        else:
            _set_job(job_id, status="done", stage="done", finished_at=_now(),
                     error=meta.get("rerank_error") or None)
    except Exception as exc:  # noqa: BLE001 —— 任何异常都要落成 failed，不能静默
        log.exception("refscout job %s failed", job_id)
        _set_job(job_id, status="failed", stage="error", finished_at=_now(),
                 error=_scrub(f"{type(exc).__name__}: {exc}")[:300])
    finally:
        with _INFLIGHT_LOCK:
            for k in (job["module"], job.get("card_id") or ""):
                if k and _INFLIGHT.get(k) == job_id:
                    _INFLIGHT.pop(k, None)


class SourceIn(BaseModel):
    value: str = Field(min_length=1, max_length=200)
    label: str = Field(default="", max_length=60)


class ScoutIn(BaseModel):
    module: str = Field(min_length=2, max_length=200)
    keywords: List[str] = Field(default_factory=list)
    lang: str = Field(default="", max_length=20)
    min_stars: int = Field(default=20, ge=0, le=100000)
    fresh_days: int = Field(default=180, ge=7, le=3650)
    identifiers: List[str] = Field(default_factory=list)
    allow_paid: bool = False
    refresh_of: Optional[str] = None
    trigger: str = Field(default="manual", max_length=12)


def submit(body: ScoutIn) -> Dict[str, Any]:
    if not token():
        raise HTTPException(503, "GitHub token 不可用（设置页 / GITHUB_TOKEN / github.txt）")
    mod = body.module.strip()
    key = body.refresh_of or mod
    with _INFLIGHT_LOCK:
        running = _INFLIGHT.get(key)
        if running and (_job_row(running) or {}).get("status") in ("queued", "running"):
            return {"job_id": running, "deduped": True}
        job_id = uuid.uuid4().hex[:12]
        _INFLIGHT[key] = job_id
    payload = {"keywords": [k.strip() for k in body.keywords if k.strip()][:8],
               "lang": body.lang.strip(), "min_stars": body.min_stars,
               "fresh_days": body.fresh_days,
               # 与 collect() 的 `identifiers[:2]` 对齐：代码搜索限额 10 次/分，
               # 之前这里切 3、下游只用 2，第三个会被静默丢弃。
               "identifiers": [i.strip() for i in body.identifiers if i.strip()][:2],
               "allow_paid": bool(body.allow_paid),
               "trigger": body.trigger,
               "refresh_of": body.refresh_of}
    card_id = body.refresh_of or uuid.uuid4().hex[:12]
    db.execute("INSERT INTO ref_jobs(id,card_id,module,payload,status,stage,created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (job_id, card_id, mod, json.dumps(payload, ensure_ascii=False),
                "queued", "queued", _now()))
    _POOL.submit(_run_job, job_id)
    return {"job_id": job_id, "card_id": card_id, "deduped": False}


def job_view(job_id: str) -> Dict[str, Any]:
    row = _job_row(job_id)
    if not row:
        raise HTTPException(404, "任务不存在")
    out = dict(row)
    # 任务跑完前卡还没落库是正常中间态：**不能**因为卡缺失就把任务查询整条 404。
    cid = row.get("card_id")
    out["card"] = card_view(cid) if (cid and _card_row(cid)) else None
    return out


def _card_row(card_id: str) -> Optional[Dict[str, Any]]:
    rows = db.query("SELECT * FROM ref_cards WHERE id=?", (card_id,))
    return rows[0] if rows else None


def card_view(card_id: str, with_md: bool = True) -> Dict[str, Any]:
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, "情报卡不存在")
    out = dict(row)
    for k in ("keywords", "meta", "candidates", "diff"):
        try:
            out[k] = json.loads(out.get(k) or ("[]" if k in ("keywords", "candidates") else "{}"))
        except ValueError:
            out[k] = [] if k in ("keywords", "candidates") else {}
    if not with_md:
        out.pop("md", None)
    return out


def list_cards(q: str = "", limit: int = 50) -> Dict[str, Any]:
    if q:
        rows = db.query("SELECT id,module,status,as_of,next_refresh,elapsed_s,meta,"
                        "candidates FROM ref_cards WHERE module LIKE ? "
                        "ORDER BY updated_at DESC LIMIT ?", (f"%{q}%", limit))
        total = (db.query("SELECT COUNT(*) AS n FROM ref_cards WHERE module LIKE ?",
                          (f"%{q}%",)) or [{"n": 0}])[0]["n"]
    else:
        rows = db.query("SELECT id,module,status,as_of,next_refresh,elapsed_s,meta,"
                        "candidates FROM ref_cards ORDER BY updated_at DESC LIMIT ?", (limit,))
        total = (db.query("SELECT COUNT(*) AS n FROM ref_cards") or [{"n": 0}])[0]["n"]
    items = []
    for r in rows:
        try:
            meta = json.loads(r.get("meta") or "{}")
            cands = json.loads(r.get("candidates") or "[]")
        except ValueError:
            meta, cands = {}, []
        impls = [c for c in cands if c.get("bucket") == "impl"]
        top = impls[0] if impls else (cands[0] if cands else None)
        items.append({"id": r["id"], "module": r["module"], "status": r["status"],
                      "as_of": r["as_of"], "next_refresh": r["next_refresh"],
                      "elapsed_s": r["elapsed_s"], "candidates": len(cands),
                      "impl_count": len(impls), "top": top,
                      "rerank": bool(meta.get("rerank")),
                      "degraded": len(meta.get("degraded") or [])})
    return {"items": items, "total": total, "returned": len(items),
            "truncated": total > len(items)}


# ── HTTP 路由 ───────────────────────────────────────────────────────────────

@router.post("/scout")
async def api_scout(body: ScoutIn):
    return submit(body)


@router.get("/sources")
async def api_sources():
    return list_sources()


@router.post("/sources")
async def api_source_add(body: SourceIn):
    return add_source(body.value, body.label)


@router.delete("/sources/{sid}")
async def api_source_delete(sid: str):
    return delete_source(sid)


@router.get("/jobs/{job_id}")
async def api_job(job_id: str):
    return job_view(job_id)


@router.get("/cards")
async def api_cards(q: str = Query(default=""), limit: int = Query(default=50, ge=1, le=200)):
    return list_cards(q, limit)


@router.get("/cards/{card_id}")
async def api_card(card_id: str):
    return card_view(card_id)


@router.get("/cards/{card_id}/md")
async def api_card_md(card_id: str):
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, "情报卡不存在")
    return {"id": card_id, "module": row["module"], "md": row["md"]}


def refresh_body(row: Dict[str, Any], allow_paid: Optional[bool] = None) -> ScoutIn:
    """重跑请求：**显式参数优先**，未给才沿用上次「请求过」的付费意图。

    不能只看 `rerank_paid`：它的含义是「上一次真的用上了付费档」，付费档失败回落
    到免费档后就是 False。用它决定重跑 ⇒ 一次失败就把卡永久锁在免费档，而免费池
    会被内容审核模型拦截，用户再点多少次「重跑」都回不到语义重排。
    """
    try:
        kw = json.loads(row["keywords"] or "[]")
        meta = json.loads(row["meta"] or "{}")
    except ValueError:
        kw, meta = [], {}
    if allow_paid is None:
        want = bool(meta.get("requested_paid", meta.get("rerank_paid", False)))
    else:
        want = bool(allow_paid)
    return ScoutIn(module=row["module"], keywords=kw, lang=row["lang"] or "",
                   identifiers=meta.get("identifiers") or [],
                   allow_paid=want, refresh_of=row["id"], trigger="manual")


@router.post("/cards/{card_id}/refresh")
async def api_refresh(card_id: str, allow_paid: Optional[bool] = Query(default=None)):
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, "情报卡不存在")
    return submit(refresh_body(row, allow_paid))


@router.delete("/cards/{card_id}")
async def api_delete(card_id: str):
    if not _card_row(card_id):
        raise HTTPException(404, "情报卡不存在")
    db.execute("DELETE FROM ref_cards WHERE id=?", (card_id,))
    return {"deleted": card_id}


@router.get("/status")
async def api_status():
    jobs = db.query("SELECT status, COUNT(*) AS n FROM ref_jobs GROUP BY status")
    return {"token": bool(token()), "cache": cache_stats(),
            "jobs": {r["status"]: r["n"] for r in jobs},
            "sources": len(list_sources()["items"]),
            "card_ttl_days": CARD_TTL_DAYS,
            "paid_rerank": _ref_model(),
            "free_models": [llm.memory_model(), llm.free_fallback_model()]}


# ── 定时刷新 ────────────────────────────────────────────────────────────────

async def refresh_loop(interval_s: int = 1800) -> None:
    """到期的卡自动重跑（默认 30 天一次）。**每轮最多 1 张**，避免限流雪崩。

    **自动刷新永不付费**（`allow_paid=False`）：定时任务花钱不可能每次都有用户确认。
    代价是免费池若被安全分类模型拦截，卡会退回 `lexical_fallback` 并在卡面写明原因；
    想恢复语义重排必须人工点「重跑」并显式选付费档。
    """
    import asyncio
    while True:
        await asyncio.sleep(interval_s)
        if os.getenv("REF_AUTO_REFRESH", "1") != "1":
            continue
        try:
            rows = db.query(
                "SELECT id FROM ref_cards WHERE auto_refresh=1 AND next_refresh<=? "
                "ORDER BY next_refresh LIMIT 1", (_now(),))
            if not rows:
                continue
            cid = rows[0]["id"]
            row = _card_row(cid)
            kw = json.loads(row["keywords"] or "[]")
            submit(ScoutIn(module=row["module"], keywords=kw,
                           lang=row["lang"] or "",
                           identifiers=json.loads(row["meta"] or "{}").get("identifiers") or [],
                           allow_paid=False, refresh_of=cid, trigger="auto"))
            log.info("refscout auto refresh queued: %s", cid)
        except Exception as exc:  # noqa: BLE001 —— 定时任务失败只记日志，不拖垮循环
            log.warning("refscout auto refresh failed: %s", exc)
