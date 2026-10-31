"""常量 + 基础工具（时间 / 缓存 / 脱敏 / token / HTTP 头）。被其余全部模块依赖，自身不 import 本包任何模块。

由 `src/refscout.py` 按 `docs/SPLIT-PLAN-20261031.md` §3.1 机械切出（纯搬运，无行为变更）。

**跨模块引用一律走模块属性**（`_rc_rank._enrich(...)` 而非 `from refscout_rank import _enrich`）：
名字绑定是拷贝，给某个模块打补丁时拷贝不会跟着变 —— 2026-10-31 实测踩过（5 条红）。
"""
from __future__ import annotations

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
import chain_judge
import db
import llm
import outbound
import tdai_client
import ghsettings
from config import config


GH_API = 'https://api.github.com'
UA = 'agenthub-refscout/1.0'
CACHE_TTL_SEARCH = 7 * 86400
CACHE_TTL_ENRICH = 86400
CACHE_TTL_SOURCE = 6 * 3600
SOURCE_PAGE = 100
SOURCE_MAX_REPOS = 300
SOURCE_BONUS = 0.06
CACHE_README_CHARS = 6000
MAX_CANDIDATES = 8
ENRICH_LIMIT = 12
LIST_EXPAND_LISTS = 4
LIST_EXPAND_SUBPAGES = 4
LIST_EXPAND_PER_LIST = 30
LIST_EXPAND_TOTAL = 80
LIST_EXPAND_THREADS = 4
LIST_SIGNAL_LINKS = 8
SEARCH_PER_PAGE = 10
CARD_TTL_DAYS = 30
JOB_KEEP_DAYS = 14
LIST_RE = re.compile('awesome|curated|list of|\\bbooks?\\b|tutorial|course|roadmap|cheat|interview|examples?$', re.I)
DOC_RE = re.compile('docs?$|documentation|wiki|handbook|guide', re.I)
ARCH_TERMS = ('architecture', 'design', 'trade-off', 'tradeoff', 'why we', 'rationale', 'adr')
GPL_RE = re.compile('gpl|agpl', re.I)
STOP = {'the', 'and', 'for', 'with', 'from', 'your', 'this', 'that', 'into', 'over', 'based', 'using', 'simple', '基于', '实现', '支持', '一个', '以及', '相关'}
FALLBACK_CN = {'端口转发': 'port forwarding', '终端': 'terminal', '会话': 'session', '技能': 'skill', '检索': 'search', '扫描': 'scan', '缓存': 'cache', '转发': 'forward', '网关': 'gateway', '记忆': 'memory', '索引': 'index', '调用': 'invoke', '记录': 'record', '分析': 'analyze', '调度': 'scheduler', '任务': 'task', '审计': 'audit', '权限': 'permission', '配额': 'quota', '限流': 'rate limit', '消息': 'message', '队列': 'queue'}
SYNONYMS = {'health': ['healthcheck', 'health check', 'liveness', 'readiness', 'uptime', 'status page'], 'probe': ['healthcheck', 'health check', 'liveness', 'readiness'], 'uptime': ['status page', 'availability', 'monitoring', 'healthcheck'], 'monitor': ['monitoring', 'observability', 'watchdog', 'alerting'], 'monitoring': ['observability', 'watchdog', 'alerting', 'uptime'], 'watchdog': ['supervisor', 'healthcheck', 'auto restart', 'monitoring'], 'cron': ['crontab', 'cron expression', 'scheduler', 'scheduling'], 'scheduler': ['scheduling', 'job scheduler', 'cron', 'task scheduling', 'job queue'], 'scheduling': ['scheduler', 'cron', 'job scheduler', 'task queue'], 'task': ['job', 'worker', 'queue', 'task queue'], 'queue': ['task queue', 'job queue', 'message queue', 'worker'], 'search': ['search engine', 'full-text search', 'full text search', 'query'], 'engine': ['search engine', 'index engine'], 'index': ['indexing', 'inverted index'], 'indexing': ['inverted index', 'index engine', 'full-text search'], 'terminal': ['tty', 'pty', 'console', 'shell', 'web terminal'], 'websocket': ['socket', 'realtime', 'sse', 'server-sent events'], 'pty': ['tty', 'pseudo terminal', 'terminal emulator'], 'port': ['port forwarding', 'tunnel'], 'forwarding': ['port forwarding', 'tunnel', 'relay'], 'llm': ['language model', 'ai gateway', 'model gateway'], 'gateway': ['proxy', 'router', 'api gateway', 'reverse proxy'], 'proxy': ['gateway', 'relay', 'router'], 'router': ['routing', 'gateway', 'proxy'], 'failover': ['fallback', 'load balancing', 'retry', 'high availability'], 'cache': ['caching', 'memoization', 'key value store'], 'auth': ['authentication', 'oauth', 'sso']}
SCHEMA = "\nCREATE TABLE IF NOT EXISTS ref_cards (\n    id            TEXT PRIMARY KEY,\n    module        TEXT NOT NULL,\n    keywords      TEXT NOT NULL,          -- JSON array\n    lang          TEXT,\n    status        TEXT NOT NULL,          -- lexical|reranked|lexical_fallback\n    as_of         TEXT NOT NULL,\n    next_refresh  TEXT NOT NULL,\n    elapsed_s     REAL,\n    auto_refresh  INTEGER NOT NULL DEFAULT 1,\n    meta          TEXT NOT NULL,          -- JSON：arms / cache / rerank / degraded\n    candidates    TEXT NOT NULL,          -- JSON array\n    diff          TEXT,                   -- JSON：刷新时的新增/移除/变更\n    md            TEXT NOT NULL,\n    created_at    TEXT NOT NULL,\n    updated_at    TEXT NOT NULL\n);\nCREATE INDEX IF NOT EXISTS idx_ref_cards_module ON ref_cards(module);\nCREATE TABLE IF NOT EXISTS ref_jobs (\n    id          TEXT PRIMARY KEY,\n    card_id     TEXT,\n    module      TEXT NOT NULL,\n    payload     TEXT NOT NULL,\n    status      TEXT NOT NULL,            -- queued|running|done|failed\n    stage       TEXT NOT NULL DEFAULT '',\n    error       TEXT,\n    created_at  TEXT NOT NULL,\n    started_at  TEXT,\n    finished_at TEXT\n);\nCREATE INDEX IF NOT EXISTS idx_ref_jobs_status ON ref_jobs(status);\nCREATE TABLE IF NOT EXISTS ref_sources (\n    id          TEXT PRIMARY KEY,\n    kind        TEXT NOT NULL,            -- owner|repo|starred\n    value       TEXT NOT NULL,            -- gztxt | gztxt/agenthub | gztxt（星标）\n    label       TEXT NOT NULL DEFAULT '',\n    enabled     INTEGER NOT NULL DEFAULT 1,\n    created_at  TEXT NOT NULL,\n    synced_at   TEXT,\n    repo_count  INTEGER NOT NULL DEFAULT 0,\n    last_error  TEXT\n);\n-- 唯一键必须带 kind：同一个 `gztxt` 既可能是「他的仓」也可能是「他标星的仓」，\n-- 两者候选集完全不同，按 value 去重会把后者静默吞掉。\nDROP INDEX IF EXISTS idx_ref_sources_value;\nCREATE UNIQUE INDEX IF NOT EXISTS idx_ref_sources_kind_value ON ref_sources(kind, value);\n"
_POOL = futures.ThreadPoolExecutor(max_workers=2, thread_name_prefix='refscout')
_INFLIGHT: Dict[str, str] = {}
_INFLIGHT_LOCK = threading.Lock()
_token_cache: Dict[str, Any] = {'value': '', 'at': 0.0}

def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec='seconds')

def _plus_days(days: int) -> str:
    return (datetime.now(timezone.utc) + timedelta(days=days)).isoformat(timespec='seconds')

def ensure_schema() -> None:
    db.execute_script(SCHEMA)

def token() -> str:
    """DB（设置页）→ env → token 文件首行。**真值不出本函数**。"""
    if _token_cache['value'] and time.time() - _token_cache['at'] < 60:
        return _token_cache['value']
    tok = ''
    try:
        tok = ghsettings.db_get('token') or os.getenv('GITHUB_TOKEN', '')
    except Exception:
        tok = os.getenv('GITHUB_TOKEN', '')
    if not tok:
        try:
            tok = ghsettings.token_file().read_text(encoding='utf-8', errors='ignore').splitlines()[0].strip()
        except (OSError, IndexError):
            tok = ''
    tok = (tok or '').strip()
    _token_cache.update({'value': tok, 'at': time.time()})
    return tok

def _scrub(text: str) -> str:
    try:
        return tdai_client.scrub(text, token())[:400]
    except Exception:
        return ''

def _cache_dir() -> str:
    p = config.data_dir / 'refscout-cache'
    p.mkdir(parents=True, exist_ok=True)
    return str(p)

def _cache_path(url: str) -> str:
    return os.path.join(_cache_dir(), hashlib.sha1(url.encode()).hexdigest() + '.json')

def cache_get(url: str, ttl_s: int):
    p = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(p) > ttl_s:
            return None
        with open(p, encoding='utf-8') as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None

def cache_put(url: str, payload) -> None:
    try:
        tmp = _cache_path(url) + '.tmp'
        with open(tmp, 'w', encoding='utf-8') as fh:
            json.dump(payload, fh)
        os.replace(tmp, _cache_path(url))
    except OSError:
        pass

def cache_stats() -> Dict[str, Any]:
    d = _cache_dir()
    try:
        files = [os.path.join(d, f) for f in os.listdir(d) if f.endswith('.json')]
        size = sum((os.path.getsize(f) for f in files))
        return {'files': len(files), 'bytes': size, 'dir': d}
    except OSError:
        return {'files': 0, 'bytes': 0, 'dir': d}
