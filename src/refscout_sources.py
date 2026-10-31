"""情报来源：自有 GitHub 仓（含 fork）的同步与列表。只碰 GitHub API 与 ghsettings。

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

import refscout_base as _rc_base

_SOURCE_SEG = re.compile('[A-Za-z0-9][A-Za-z0-9._-]{0,99}\\Z')
_GITHUB_HOSTS = ('github.com', 'www.github.com', 'api.github.com')

def _norm_source(raw: str) -> Tuple[str, str]:
    """把用户输入归一成 `(kind, value)`。

    `gztxt` → owner；`gztxt/agenthub` → repo；`starred:gztxt` / `gztxt:starred` /
    `github.com/gztxt?tab=stars` / `github.com/stars/gztxt` → starred（该账号标星的仓）。

    只接受 github.com。拒掉别的主机不是洁癖：`gitlab.com/x` 会被当成 `owner/repo`
    拼进 GitHub API，最后以 404 收场，用户看到的是「来源没数据」而不是「地址写错」。
    """
    v = (raw or '').strip()
    if not v:
        raise HTTPException(400, '来源为空')
    starred = False
    v = re.sub('^[A-Za-z][A-Za-z0-9+.-]*://', '', v)
    head = v.split('/')[0]
    if head.lower() in _GITHUB_HOSTS:
        v = v.partition('/')[2]
    elif '.' in head:
        raise HTTPException(400, '只支持 github.com 的账号 / 组织 / 仓库 / 星标列表')
    m = re.search('[?#](.*)$', v)
    query = m.group(1) if m else ''
    if m:
        v = v[:m.start()]
    if re.search('(?:^|&)tab=stars(?:&|$)', query, re.I):
        starred = True
    v = v.strip('/')
    v = re.sub('\\.git$', '', v, flags=re.I)
    parts = [p for p in v.split('/') if p]
    if parts and parts[0].lower() == 'stars':
        starred, parts = (True, parts[1:])
    if parts and ':' in parts[0]:
        left, _, right = parts[0].partition(':')
        if left.lower() == 'starred':
            starred, parts[0] = (True, right)
        elif right.lower() == 'starred':
            starred, parts[0] = (True, left)
        else:
            raise HTTPException(400, '只支持 starred:<账号> 形式的星标来源')
    parts = parts[:2]
    if starred:
        if not parts or not _SOURCE_SEG.match(parts[0]):
            raise HTTPException(400, '星标来源只填账号名，如 gztxt:starred')
        return ('starred', parts[0])
    if not parts or not all((_SOURCE_SEG.match(p) for p in parts)):
        raise HTTPException(400, '来源名不合法（只允许字母数字 . _ -）')
    if len(parts) == 2:
        return ('repo', f'{parts[0]}/{parts[1]}')
    return ('owner', parts[0])

def _source_label(row: Dict[str, Any]) -> str:
    return (row.get('label') or row.get('value') or '').strip()

def list_sources() -> Dict[str, Any]:
    rows = db.query('SELECT * FROM ref_sources ORDER BY created_at')
    items = []
    for r in rows:
        items.append({'id': r['id'], 'kind': r['kind'], 'value': r['value'], 'label': _source_label(r), 'enabled': bool(r['enabled']), 'created_at': r['created_at'], 'synced_at': r['synced_at'], 'repo_count': int(r['repo_count'] or 0), 'last_error': r['last_error'] or ''})
    return {'items': items, 'total': len(items)}

def enabled_sources() -> List[Dict[str, Any]]:
    return db.query('SELECT * FROM ref_sources WHERE enabled=1 ORDER BY created_at')

def add_source(value: str, label: str='') -> Dict[str, Any]:
    kind, norm = _norm_source(value)
    rows = db.query('SELECT * FROM ref_sources WHERE kind=? AND value=?', (kind, norm))
    if rows:
        return {'id': rows[0]['id'], 'value': norm, 'kind': rows[0]['kind'], 'deduped': True}
    sid = uuid.uuid4().hex[:10]
    name = (label or '').strip() or (f'star:{norm}' if kind == 'starred' else norm)
    db.execute('INSERT INTO ref_sources(id,kind,value,label,enabled,created_at,repo_count) VALUES(?,?,?,?,1,?,0)', (sid, kind, norm, name, _rc_base._now()))
    return {'id': sid, 'value': norm, 'kind': kind, 'deduped': False}

def delete_source(sid: str) -> Dict[str, Any]:
    if not db.query('SELECT id FROM ref_sources WHERE id=?', (sid,)):
        raise HTTPException(404, '来源不存在')
    db.execute('DELETE FROM ref_sources WHERE id=?', (sid,))
    return {'deleted': sid}

def _touch_source(sid: str, count: int, err: Optional[str]) -> None:
    db.execute('UPDATE ref_sources SET synced_at=?,repo_count=?,last_error=? WHERE id=?', (_rc_base._now(), count, (err or '')[:200], sid))

def _fetch_source(kind: str, value: str) -> Dict[str, Any]:
    """拉一个来源的仓库（**含 fork**，按 updated 排序）。失败不抛：来源坏了不能拖垮整卡。"""
    t0 = time.monotonic()

    def done(items: List[Dict[str, Any]], meta: Dict[str, Any]) -> Dict[str, Any]:
        ok = bool(meta.get('ok')) or bool(items)
        return {'items': items[:_rc_base.SOURCE_MAX_REPOS], 'ok': ok, 'error': None if ok else meta.get('error') or 'fetch failed', 'ms': int((time.monotonic() - t0) * 1000), 'cached': bool(meta.get('cached'))}
    if kind == 'repo':
        data, meta = gh_get(f'{_rc_base.GH_API}/repos/{value}', ttl_s=_rc_base.CACHE_TTL_SOURCE)
        return done([data] if data else [], meta)
    if kind == 'starred':
        items: List[Dict[str, Any]] = []
        meta: Dict[str, Any] = {}
        for page in range(1, _rc_base.SOURCE_MAX_REPOS // _rc_base.SOURCE_PAGE + 1):
            url = f'{_rc_base.GH_API}/users/{value}/starred?per_page={_rc_base.SOURCE_PAGE}&page={page}&sort=updated&direction=desc'
            data, meta = gh_get(url, ttl_s=_rc_base.CACHE_TTL_SOURCE)
            if not meta.get('ok'):
                break
            items.extend(data or [])
            if len(data or []) < _rc_base.SOURCE_PAGE:
                break
        return done(items, meta)
    items: List[Dict[str, Any]] = []
    meta: Dict[str, Any] = {}
    for page in range(1, _rc_base.SOURCE_MAX_REPOS // _rc_base.SOURCE_PAGE + 1):
        url = f'{_rc_base.GH_API}/users/{value}/repos?per_page={_rc_base.SOURCE_PAGE}&page={page}&type=owner&sort=updated'
        data, meta = gh_get(url, ttl_s=_rc_base.CACHE_TTL_SOURCE)
        if not meta.get('ok'):
            break
        items.extend(data or [])
        if len(data or []) < _rc_base.SOURCE_PAGE:
            break
    if not items and meta.get('status') == 404:
        for page in range(1, _rc_base.SOURCE_MAX_REPOS // _rc_base.SOURCE_PAGE + 1):
            url = f'{_rc_base.GH_API}/orgs/{value}/repos?per_page={_rc_base.SOURCE_PAGE}&page={page}&type=all&sort=updated'
            data, meta = gh_get(url, ttl_s=_rc_base.CACHE_TTL_SOURCE)
            if not meta.get('ok'):
                break
            items.extend(data or [])
            if len(data or []) < _rc_base.SOURCE_PAGE:
                break
    return done(items, meta)

def _sync_sources() -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], Dict[str, Dict[str, Any]]]:
    """拉取全部启用来源。返回 `(逐路日志, 卡面概况, full_name→{item,labels})`。

    并入的是**仓库条目本身**（与搜索结果同一形态），而不是只记一个标签：
    后续过滤/打分/许可证闸门全部复用，避免出现「来源候选走另一套规则」。
    """
    log: List[Dict[str, Any]] = []
    overview: List[Dict[str, Any]] = []
    by_key: Dict[str, Dict[str, Any]] = {}
    for row in enabled_sources():
        label = _source_label(row)
        got = _fetch_source(row['kind'], row['value'])
        repos = [it for it in got['items'] if it.get('full_name') and (not it.get('archived'))]
        log.append({'name': 'src:' + label, 'q': row['value'], 'ok': got['ok'], 'count': len(repos), 'ms': got['ms'], 'cached': got['cached'], 'error': got['error']})
        overview.append({'id': row['id'], 'value': row['value'], 'label': label, 'kind': row['kind'], 'count': len(repos), 'ok': got['ok'], 'error': got['error']})
        _touch_source(row['id'], len(repos), got['error'])
        for it in repos:
            key = it['full_name'].lower()
            rec = by_key.setdefault(key, {'item': it, 'labels': []})
            if label not in rec['labels']:
                rec['labels'].append(label)
    return (log, overview, by_key)

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

def gh_get(url: str, ttl_s: Optional[int]=None, limiter: Optional[Limiter]=None) -> Tuple[Any, Dict[str, Any]]:
    """GET 一个 GitHub API URL。`ttl_s` 非空才缓存（只缓存静态内容，不缓存限额头）。"""
    if ttl_s:
        hit = _rc_base.cache_get(url, ttl_s)
        if hit is not None:
            return (hit, {'ok': True, 'ms': 0, 'cached': True})
    reason = outbound.check_url(url, require_https=True)
    if reason:
        return (None, {'ok': False, 'ms': 0, 'error': f'出站拒绝：{reason}'})
    if limiter:
        limiter.wait()
    t0 = time.monotonic()
    headers = {'Accept': 'application/vnd.github+json', 'User-Agent': _rc_base.UA}
    tok = _rc_base.token()
    if tok:
        headers['Authorization'] = f'Bearer {tok}'
    req = urllib.request.Request(url, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if ttl_s:
            _rc_base.cache_put(url, data)
        return (data, {'ok': True, 'ms': int((time.monotonic() - t0) * 1000), 'cached': False})
    except urllib.error.HTTPError as exc:
        body = ''
        try:
            body = _rc_base._scrub(exc.read().decode('utf-8', 'ignore'))
        except Exception:
            pass
        return (None, {'ok': False, 'ms': int((time.monotonic() - t0) * 1000), 'status': exc.code, 'error': f'HTTP{exc.code} {body}'[:300], 'rate_limited': exc.code in (403, 429)})
    except Exception as exc:
        return (None, {'ok': False, 'ms': int((time.monotonic() - t0) * 1000), 'error': f'{type(exc).__name__}: {str(exc)[:120]}'})
