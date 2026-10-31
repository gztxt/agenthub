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

import chain_judge
import db
import llm
import outbound
import tdai_client
import ghsettings
from config import config

log = logging.getLogger("agenthub.refscout")
router = APIRouter(prefix="/api/ref", tags=["ref"])


# ---- B4 切分（2026-10-31）：依赖方向单向 base <- sources <- rank <- collect <- 本文件 ----
import refscout_base as _rc_base
import refscout_sources as _rc_sources
import refscout_collect as _rc_collect
import refscout_render as _rc_render

# ---- 对外兼容再导出：切分前 dir(refscout) 的每个名字仍可拿到（只绑名字，不含逻辑）----
from refscout_base import (  # noqa: F401
    ARCH_TERMS,
    CACHE_README_CHARS,
    CACHE_TTL_ENRICH,
    CACHE_TTL_SEARCH,
    CACHE_TTL_SOURCE,
    CARD_TTL_DAYS,
    DOC_RE,
    ENRICH_LIMIT,
    FALLBACK_CN,
    GH_API,
    GPL_RE,
    JOB_KEEP_DAYS,
    LIST_EXPAND_LISTS,
    LIST_EXPAND_PER_LIST,
    LIST_EXPAND_SUBPAGES,
    LIST_EXPAND_THREADS,
    LIST_EXPAND_TOTAL,
    LIST_RE,
    LIST_SIGNAL_LINKS,
    MAX_CANDIDATES,
    SCHEMA,
    SEARCH_PER_PAGE,
    SOURCE_BONUS,
    SOURCE_MAX_REPOS,
    SOURCE_PAGE,
    STOP,
    SYNONYMS,
    UA,
    _INFLIGHT,
    _INFLIGHT_LOCK,
    _POOL,
    _cache_dir,
    _cache_path,
    _now,
    _plus_days,
    _scrub,
    _token_cache,
    cache_get,
    cache_put,
    cache_stats,
    ensure_schema,
    token,
)
from refscout_sources import (  # noqa: F401
    Limiter,
    _CODE_LIMIT,
    _GITHUB_HOSTS,
    _SEARCH_LIMIT,
    _SOURCE_SEG,
    _fetch_source,
    _norm_source,
    _source_label,
    _sync_sources,
    _touch_source,
    add_source,
    delete_source,
    enabled_sources,
    gh_get,
    list_sources,
)
from refscout_rank import (  # noqa: F401
    SYN_CREDIT,
    _concept_cov,
    _concept_weights,
    _days_since,
    _enrich,
    _expand_blob,
    _forms_of,
    _gist,
    _hit,
    _name_blob,
    _norm_text,
    alias_groups,
    build_queries,
    classify,
    tokens_of,
)
from refscout_collect import (  # noqa: F401
    _GH_LINK_RE,
    _GH_RESERVED,
    _MD_SELF_RE,
    _SAFETY_REPLY,
    _blob_text,
    _contents_text,
    _expand_lists,
    _gh_links,
    _is_safety_reply,
    _json_block,
    _judge_rank,
    _llm_text,
    _majority,
    _md_subpages,
    _median,
    _order_key,
    _pick_lists,
    _readme_text,
    _ref_model,
    _sample_count,
    apply_rerank,
    collect,
    rerank,
)
from refscout_render import (  # noqa: F401
    _badge,
    render_md,
)

def _job_row(job_id: str) -> Optional[Dict[str, Any]]:
    rows = db.query('SELECT * FROM ref_jobs WHERE id=?', (job_id,))
    return rows[0] if rows else None

def _set_job(job_id: str, **fields) -> None:
    if not fields:
        return
    cols = ', '.join((f'{k}=?' for k in fields))
    db.execute(f'UPDATE ref_jobs SET {cols} WHERE id=?', tuple(fields.values()) + (job_id,))

def _diff_candidates(old: Dict[str, Any], new: List[Dict[str, Any]]) -> Dict[str, Any]:
    try:
        old_list = json.loads(old.get('candidates') or '[]')
    except ValueError:
        old_list = []
    old_repos = {c.get('repo') for c in old_list}
    new_repos = {c.get('repo') for c in new}
    changed = []
    old_map = {c.get('repo'): c for c in old_list}
    for c in new:
        o = old_map.get(c['repo'])
        if not o:
            continue
        d = abs(float(c.get('func', 0)) - float(o.get('func', 0)))
        if d >= 0.15:
            changed.append(f"{c['repo']} 功能分 {o.get('func')}→{c.get('func')}")
    return {'added': sorted(new_repos - old_repos), 'removed': sorted(old_repos - new_repos), 'changed': changed[:10]}

def _card_upsert(card_id: str, module: str, keywords: List[str], lang: str, status: str, as_of: str, next_refresh: str, elapsed: float, meta: Dict[str, Any], ranked: List[Dict[str, Any]], md: str, diff: Optional[Dict[str, Any]]=None) -> None:
    now = _rc_base._now()
    exists = db.query('SELECT id FROM ref_cards WHERE id=?', (card_id,))
    if exists:
        db.execute('UPDATE ref_cards SET status=?,as_of=?,next_refresh=?,elapsed_s=?,meta=?,candidates=?,diff=?,md=?,updated_at=? WHERE id=?', (status, as_of, next_refresh, elapsed, json.dumps(meta, ensure_ascii=False), json.dumps(ranked, ensure_ascii=False), json.dumps(diff or {}, ensure_ascii=False), md, now, card_id))
    else:
        db.execute('INSERT INTO ref_cards(id,module,keywords,lang,status,as_of,next_refresh,elapsed_s,auto_refresh,meta,candidates,diff,md,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,1,?,?,?,?,?,?)', (card_id, module, json.dumps(keywords, ensure_ascii=False), lang, status, as_of, next_refresh, elapsed, json.dumps(meta, ensure_ascii=False), json.dumps(ranked, ensure_ascii=False), json.dumps(diff or {}, ensure_ascii=False), md, now, now))

def _run_job(job_id: str) -> None:
    job = _job_row(job_id)
    if not job:
        return
    payload = json.loads(job['payload'] or '{}')
    card_id = job['card_id'] or uuid.uuid4().hex[:12]
    _set_job(job_id, status='running', stage='parse', started_at=_rc_base._now())
    if not job['card_id']:
        _set_job(job_id, card_id=card_id)
    t_job = time.monotonic()
    try:
        module = job['module']
        keywords = payload.get('keywords') or []
        lang = payload.get('lang') or ''
        src = 'given'
        if not keywords:
            text, err, _stop = _rc_collect._llm_text(f'把下面的中文功能模块描述转成 GitHub 检索关键词。输出严格 JSON：{{"keywords":["英文短语",...]}}，3~5 个，只含技术名词，不要解释。\n模块：{module}', model=llm.memory_model(), timeout=25, max_tokens=1500)
            data = _rc_collect._json_block(text) or {}
            keywords = [str(k).strip() for k in data.get('keywords', []) if str(k).strip()][:5]
            src = 'llm' if keywords else 'fallback'
            if not keywords:
                keywords = [v for k, v in _rc_base.FALLBACK_CN.items() if k in module] or [module]
        prev = None
        if payload.get('refresh_of'):
            rows = db.query('SELECT * FROM ref_cards WHERE id=?', (payload['refresh_of'],))
            prev = rows[0] if rows else None
        got = _rc_collect.collect(module, keywords, lang, int(payload.get('min_stars', 20)), int(payload.get('fresh_days', 180)), payload.get('identifiers') or [], bool(payload.get('allow_paid')), stage=lambda s: _set_job(job_id, stage=s), do_rerank=False)
        meta = got['meta']
        meta['keyword_source'] = src
        meta['identifiers'] = payload.get('identifiers') or []
        meta['requested_paid'] = bool(payload.get('allow_paid'))
        meta['trigger'] = payload.get('trigger') or 'manual'
        as_of = _rc_base._now()
        next_refresh = _rc_base._plus_days(_rc_base.CARD_TTL_DAYS)
        diff = _diff_candidates(prev, got['candidates']) if prev else None
        md = _rc_render.render_md(module, keywords, lang, meta, got['candidates'], got['elapsed'], as_of, next_refresh, diff=diff)
        _card_upsert(card_id, module, keywords, lang, 'lexical', as_of, next_refresh, got['elapsed'], meta, got['candidates'], md, diff)
        _set_job(job_id, stage='rerank')
        t_rr = time.monotonic()
        err, used, attempts = _rc_collect.apply_rerank(module, got['candidates'], bool(payload.get('allow_paid')))
        meta['rerank_attempts'] = attempts
        meta['rerank'] = bool(used)
        meta['rerank_model'] = used
        meta['rerank_paid'] = bool(used and used not in (llm.memory_model(), llm.free_fallback_model()))
        meta['rerank_error'] = err
        if not used:
            meta['arms'].append({'name': 'llm-rerank', 'q': '', 'ok': False, 'count': 0, 'ms': 0, 'error': err or 'empty/invalid JSON', 'cached': False})
        status = 'reranked' if used else 'lexical_fallback' if err else 'lexical'
        total = round(time.monotonic() - t_job, 1)
        meta['lexical_elapsed'] = got['elapsed']
        meta['rerank_elapsed'] = round(time.monotonic() - t_rr, 1)
        md = _rc_render.render_md(module, keywords, lang, meta, got['candidates'], total, as_of, next_refresh, diff=diff)
        _card_upsert(card_id, module, keywords, lang, status, as_of, next_refresh, total, meta, got['candidates'], md, diff)
        _set_job(job_id, stage='done')
        if meta.get('all_arms_failed'):
            _set_job(job_id, status='failed', stage='done', finished_at=_rc_base._now(), error='全部检索臂失败（见卡内逐路表态）')
        else:
            _set_job(job_id, status='done', stage='done', finished_at=_rc_base._now(), error=meta.get('rerank_error') or None)
    except Exception as exc:
        log.exception('refscout job %s failed', job_id)
        _set_job(job_id, status='failed', stage='error', finished_at=_rc_base._now(), error=_rc_base._scrub(f'{type(exc).__name__}: {exc}')[:300])
    finally:
        with _rc_base._INFLIGHT_LOCK:
            for k in (job['module'], job.get('card_id') or ''):
                if k and _rc_base._INFLIGHT.get(k) == job_id:
                    _rc_base._INFLIGHT.pop(k, None)

class SourceIn(BaseModel):
    value: str = Field(min_length=1, max_length=200)
    label: str = Field(default='', max_length=60)

class ScoutIn(BaseModel):
    module: str = Field(min_length=2, max_length=200)
    keywords: List[str] = Field(default_factory=list)
    lang: str = Field(default='', max_length=20)
    min_stars: int = Field(default=20, ge=0, le=100000)
    fresh_days: int = Field(default=180, ge=7, le=3650)
    identifiers: List[str] = Field(default_factory=list)
    allow_paid: bool = False
    refresh_of: Optional[str] = None
    trigger: str = Field(default='manual', max_length=12)

def submit(body: ScoutIn) -> Dict[str, Any]:
    if not _rc_base.token():
        raise HTTPException(503, 'GitHub token 不可用（设置页 / GITHUB_TOKEN / github.txt）')
    mod = body.module.strip()
    key = body.refresh_of or mod
    with _rc_base._INFLIGHT_LOCK:
        running = _rc_base._INFLIGHT.get(key)
        if running and (_job_row(running) or {}).get('status') in ('queued', 'running'):
            return {'job_id': running, 'deduped': True}
        job_id = uuid.uuid4().hex[:12]
        _rc_base._INFLIGHT[key] = job_id
    payload = {'keywords': [k.strip() for k in body.keywords if k.strip()][:8], 'lang': body.lang.strip(), 'min_stars': body.min_stars, 'fresh_days': body.fresh_days, 'identifiers': [i.strip() for i in body.identifiers if i.strip()][:2], 'allow_paid': bool(body.allow_paid), 'trigger': body.trigger, 'refresh_of': body.refresh_of}
    card_id = body.refresh_of or uuid.uuid4().hex[:12]
    db.execute('INSERT INTO ref_jobs(id,card_id,module,payload,status,stage,created_at) VALUES(?,?,?,?,?,?,?)', (job_id, card_id, mod, json.dumps(payload, ensure_ascii=False), 'queued', 'queued', _rc_base._now()))
    _rc_base._POOL.submit(_run_job, job_id)
    return {'job_id': job_id, 'card_id': card_id, 'deduped': False}

def job_view(job_id: str) -> Dict[str, Any]:
    row = _job_row(job_id)
    if not row:
        raise HTTPException(404, '任务不存在')
    out = dict(row)
    cid = row.get('card_id')
    out['card'] = card_view(cid) if cid and _card_row(cid) else None
    return out

def _card_row(card_id: str) -> Optional[Dict[str, Any]]:
    rows = db.query('SELECT * FROM ref_cards WHERE id=?', (card_id,))
    return rows[0] if rows else None

def card_view(card_id: str, with_md: bool=True) -> Dict[str, Any]:
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, '情报卡不存在')
    out = dict(row)
    for k in ('keywords', 'meta', 'candidates', 'diff'):
        try:
            out[k] = json.loads(out.get(k) or ('[]' if k in ('keywords', 'candidates') else '{}'))
        except ValueError:
            out[k] = [] if k in ('keywords', 'candidates') else {}
    if not with_md:
        out.pop('md', None)
    return out

def list_cards(q: str='', limit: int=50) -> Dict[str, Any]:
    if q:
        rows = db.query('SELECT id,module,status,as_of,next_refresh,elapsed_s,meta,candidates FROM ref_cards WHERE module LIKE ? ORDER BY updated_at DESC LIMIT ?', (f'%{q}%', limit))
        total = (db.query('SELECT COUNT(*) AS n FROM ref_cards WHERE module LIKE ?', (f'%{q}%',)) or [{'n': 0}])[0]['n']
    else:
        rows = db.query('SELECT id,module,status,as_of,next_refresh,elapsed_s,meta,candidates FROM ref_cards ORDER BY updated_at DESC LIMIT ?', (limit,))
        total = (db.query('SELECT COUNT(*) AS n FROM ref_cards') or [{'n': 0}])[0]['n']
    items = []
    for r in rows:
        try:
            meta = json.loads(r.get('meta') or '{}')
            cands = json.loads(r.get('candidates') or '[]')
        except ValueError:
            meta, cands = ({}, [])
        impls = [c for c in cands if c.get('bucket') == 'impl']
        top = impls[0] if impls else cands[0] if cands else None
        items.append({'id': r['id'], 'module': r['module'], 'status': r['status'], 'as_of': r['as_of'], 'next_refresh': r['next_refresh'], 'elapsed_s': r['elapsed_s'], 'candidates': len(cands), 'impl_count': len(impls), 'top': top, 'rerank': bool(meta.get('rerank')), 'degraded': len(meta.get('degraded') or [])})
    return {'items': items, 'total': total, 'returned': len(items), 'truncated': total > len(items)}

@router.post('/scout')
async def api_scout(body: ScoutIn):
    return submit(body)

@router.get('/sources')
async def api_sources():
    return _rc_sources.list_sources()

@router.post('/sources')
async def api_source_add(body: SourceIn):
    return _rc_sources.add_source(body.value, body.label)

@router.delete('/sources/{sid}')
async def api_source_delete(sid: str):
    return _rc_sources.delete_source(sid)

@router.get('/jobs/{job_id}')
async def api_job(job_id: str):
    return job_view(job_id)

@router.get('/cards')
async def api_cards(q: str=Query(default=''), limit: int=Query(default=50, ge=1, le=200)):
    return list_cards(q, limit)

@router.get('/cards/{card_id}')
async def api_card(card_id: str):
    return card_view(card_id)

@router.get('/cards/{card_id}/md')
async def api_card_md(card_id: str):
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, '情报卡不存在')
    return {'id': card_id, 'module': row['module'], 'md': row['md']}

def refresh_body(row: Dict[str, Any], allow_paid: Optional[bool]=None) -> ScoutIn:
    """重跑请求：**显式参数优先**，未给才沿用上次「请求过」的付费意图。

    不能只看 `rerank_paid`：它的含义是「上一次真的用上了付费档」，付费档失败回落
    到免费档后就是 False。用它决定重跑 ⇒ 一次失败就把卡永久锁在免费档，而免费池
    会被内容审核模型拦截，用户再点多少次「重跑」都回不到语义重排。
    """
    try:
        kw = json.loads(row['keywords'] or '[]')
        meta = json.loads(row['meta'] or '{}')
    except ValueError:
        kw, meta = ([], {})
    if allow_paid is None:
        want = bool(meta.get('requested_paid', meta.get('rerank_paid', False)))
    else:
        want = bool(allow_paid)
    return ScoutIn(module=row['module'], keywords=kw, lang=row['lang'] or '', identifiers=meta.get('identifiers') or [], allow_paid=want, refresh_of=row['id'], trigger='manual')

@router.post('/cards/{card_id}/refresh')
async def api_refresh(card_id: str, allow_paid: Optional[bool]=Query(default=None)):
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, '情报卡不存在')
    return submit(refresh_body(row, allow_paid))

@router.delete('/cards/{card_id}')
async def api_delete(card_id: str):
    if not _card_row(card_id):
        raise HTTPException(404, '情报卡不存在')
    db.execute('DELETE FROM ref_cards WHERE id=?', (card_id,))
    return {'deleted': card_id}

@router.get('/status')
async def api_status():
    jobs = db.query('SELECT status, COUNT(*) AS n FROM ref_jobs GROUP BY status')
    return {'token': bool(_rc_base.token()), 'cache': _rc_base.cache_stats(), 'jobs': {r['status']: r['n'] for r in jobs}, 'sources': len(_rc_sources.list_sources()['items']), 'card_ttl_days': _rc_base.CARD_TTL_DAYS, 'paid_rerank': _rc_collect._ref_model(), 'free_models': [llm.memory_model(), llm.free_fallback_model()]}

async def refresh_loop(interval_s: int=1800) -> None:
    """到期的卡自动重跑（默认 30 天一次）。**每轮最多 1 张**，避免限流雪崩。

    **自动刷新永不付费**（`allow_paid=False`）：定时任务花钱不可能每次都有用户确认。
    代价是免费池若被安全分类模型拦截，卡会退回 `lexical_fallback` 并在卡面写明原因；
    想恢复语义重排必须人工点「重跑」并显式选付费档。
    """
    import asyncio
    while True:
        await asyncio.sleep(interval_s)
        if os.getenv('REF_AUTO_REFRESH', '1') != '1':
            continue
        try:
            rows = db.query('SELECT id FROM ref_cards WHERE auto_refresh=1 AND next_refresh<=? ORDER BY next_refresh LIMIT 1', (_rc_base._now(),))
            if not rows:
                continue
            cid = rows[0]['id']
            row = _card_row(cid)
            kw = json.loads(row['keywords'] or '[]')
            submit(ScoutIn(module=row['module'], keywords=kw, lang=row['lang'] or '', identifiers=json.loads(row['meta'] or '{}').get('identifiers') or [], allow_paid=False, refresh_of=cid, trigger='auto'))
            log.info('refscout auto refresh queued: %s', cid)
        except Exception as exc:
            log.warning('refscout auto refresh failed: %s', exc)
