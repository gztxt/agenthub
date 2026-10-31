"""查询构造与打分（**纯函数**：输入字符串/字典，输出分数）。

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
import refscout_sources as _rc_sources

def tokens_of(keywords: List[str]) -> List[str]:
    out = []
    for k in keywords:
        for t in re.split('[^a-z0-9+#.\\u4e00-\\u9fff]+', str(k).lower()):
            if len(t) > 2 and t not in _rc_base.STOP:
                out.append(t)
    seen, uniq = (set(), [])
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq

def build_queries(keywords: List[str], module: str, min_stars: int, fresh_days: int) -> List[Tuple[str, str, str]]:
    """返回 [(臂名, 查询式, 排序)]。主臂收窄，新鲜臂独立成桶。"""
    cut = (datetime.now(timezone.utc) - timedelta(days=fresh_days)).strftime('%Y-%m-%d')
    base = f' fork:false archived:false stars:>={min_stars}'
    toks = tokens_of(keywords)
    arms: List[Tuple[str, str, str]] = []
    if toks:
        arms.append(('core', f"{' '.join(toks[:3])} in:name,description,topics" + base, 'stars'))
    if len(toks) >= 2:
        arms.append(('pair', f'{toks[0]} {toks[1]} in:name,description,topics' + base, 'stars'))
    if len(toks) >= 3:
        arms.append(('pair2', f'{toks[0]} {toks[2]} in:name,description,topics' + base, 'stars'))
    if toks:
        arms.append(('fresh', f"{' '.join(toks[:4])} in:name,description,topics" + base + f' pushed:>{cut}', 'updated'))
    if re.search('[\\u4e00-\\u9fff]', module):
        arms.append(('cn', f'{module} in:name,description' + base, 'stars'))
    if len(toks) >= 2:
        arms.append(('reading', f'{toks[0]} {toks[-1]} in:readme' + base, 'stars'))
    if not arms:
        arms.append(('core', f'{module} in:name,description' + base, 'stars'))
    multi = [k for k in keywords if ' ' in str(k).strip()]
    single = [k for k in keywords if ' ' not in str(k).strip()]

    def _arm_key(q: str) -> frozenset:
        """臂的「概念键」＝查询串里 `in:` 之前的词元集（大小写/连字符已归一）。"""
        return frozenset(tokens_of([q.split(' in:')[0]]))
    seen_k = [_arm_key(a[1]) for a in arms]
    for i, kw in enumerate(list(dict.fromkeys(multi + single))[:3]):
        kt = _arm_key(kw)
        if not kt or any((kt == s for s in seen_k)):
            continue
        arms.append((f'kw{i}', f'{kw} in:name,description,topics' + base, 'stars'))
        seen_k.append(kt)
    if os.getenv('REF_HEAD_ARM', '0') != '0':
        for i, kw in enumerate(list(dict.fromkeys(multi))[:1]):
            tk = tokens_of([kw])
            for pos, word in (('h', tk[0] if tk else ''), ('t', tk[-1] if tk else '')):
                if len(word) < 3 or word in _rc_base.STOP:
                    continue
                kt = frozenset([word])
                if kt in seen_k:
                    continue
                arms.append((f'{pos}ead{i}' if pos == 'h' else f'{pos}ail{i}', f'{word} in:name,description,topics' + base, 'stars'))
                seen_k.append(kt)
    out, seen_q = ([], set())
    for name, q, sort in arms:
        if (q, sort) in seen_q:
            continue
        seen_q.add((q, sort))
        out.append((name, q, sort))
    return out

def classify(item: Dict[str, Any]) -> str:
    blob = f"{item.get('name', '')} {item.get('description') or ''}"
    if _rc_base.LIST_RE.search(blob):
        return 'list'
    if _rc_base.DOC_RE.search(item.get('name', '')):
        return 'doc'
    return 'impl'

def _name_blob(item: Dict[str, Any]) -> str:
    return ' '.join([item.get('name', ''), item.get('description') or '', ' '.join(item.get('topics') or [])]).lower()

def _norm_text(s: str) -> str:
    """分隔符归一：`full-text-search` / `full_text_search` / `full text search` 等价。

    不改这一步，topics 里连字符写法的仓（meilisearch 的 `full-text-search`）永远匹配
    不到关键词 `full text search` —— 这是实测里 6 条「未入池」中 3 条的直接成因。
    """
    return re.sub('\\s+', ' ', re.sub('[-_/]+', ' ', (s or '').lower())).strip()

def _forms_of(word: str) -> set:
    """单词的形态变体（复数/动名词/名词化），只做可逆的几条规则，不做完整词干化。

    ⚠️ 双写辅音必须去掉：旧实现 `logging[:-3]` 得 `logg`，而 blob 里是 `log`/`logs`，
    **拼写差一个字母就永远不命中**。反过来 `processing` 的 `-ss` 不能去（否则成 `proces`）。
    这也是「关键词 `log` 匹配不到 loki 的 `logs`/`logging`」的根因：变体只在关键词一侧
    生成，blob 侧从来没有展开过。
    """
    out = {word}
    if len(word) > 3 and word.endswith('s') and (not word.endswith('ss')):
        out.add(word[:-1])
    if len(word) > 5 and word.endswith('ing'):
        base = word[:-3]
        if len(base) >= 4 and base[-1] == base[-2] and (base[-1] not in 'lsz'):
            base = base[:-1]
        out.add(base)
    if len(word) > 4 and word.endswith('er'):
        out.add(word[:-2])
    if len(word) > 6 and word.endswith('tion'):
        out.add(word[:-3])
    if len(word) > 6 and word.endswith('ctor'):
        out.add(word[:-2])
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
    return blob + ' | ' + ' | '.join(sorted(extra)) if extra else blob

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
        own, syn = ({_norm_text(kw)}, set())
        phrase = ' ' in str(kw).strip()
        for w in re.split('[^a-z0-9+#.]+', str(kw).lower()):
            if not w:
                continue
            own |= {w} if phrase else _forms_of(w)
            for alias in _rc_base.SYNONYMS.get(w, []):
                syn.add(_norm_text(alias))
        syn -= own
        groups.append((sorted((f for f in own if f), key=len, reverse=True), sorted((f for f in syn if f), key=len, reverse=True)))
    return groups

def _hit(blob: str, form: str) -> bool:
    if ' ' in form:
        return form in blob
    return re.search('\\b' + re.escape(form) + '\\b', blob) is not None
SYN_CREDIT = 0.7

def _concept_weights(pool: Dict[str, Dict], groups: List[Tuple[List[str], List[str]]]) -> List[float]:
    """概念级 IDF：越少仓命中，区分度越高（与旧词元 IDF 同式，只换了计数单位）。"""
    n = max(1, len(pool))
    out = []
    for own, syn in groups:
        forms = own + syn
        df = sum((1 for r in pool.values() if any((_hit(_expand_blob(_norm_text(_name_blob(r['item']))), f) for f in forms))))
        out.append(1.0 + math.log((n + 1) / (df + 1)))
    return out

def _concept_cov(blob: str, groups: List[Tuple[List[str], List[str]]], w: List[float]) -> Tuple[float, int]:
    """返回（加权覆盖度, 命中的概念数）。同义词最多给 `SYN_CREDIT`，不会盖过原词。"""
    total = sum(w) or 1.0
    score, matched = (0.0, 0)
    for (own, syn), wt in zip(groups, w):
        if any((_hit(blob, f) for f in own)):
            score += wt
            matched += 1
        elif any((_hit(blob, f) for f in syn)):
            score += wt * SYN_CREDIT
            matched += 1
    return (score / total, matched)

def _days_since(ts: Optional[str]) -> int:
    if not ts:
        return 9999
    try:
        return (datetime.now(timezone.utc) - datetime.fromisoformat(ts.replace('Z', '+00:00'))).days
    except ValueError:
        return 9999

def _enrich(full: str, branch: str) -> Dict[str, Any]:
    """README / 目录树 / head sha。三路并发，全部走缓存（24h）。"""

    def readme() -> str:
        data, _ = _rc_sources.gh_get(f'{_rc_base.GH_API}/repos/{full}/readme', ttl_s=_rc_base.CACHE_TTL_ENRICH)
        if not data or not data.get('content'):
            return ''
        try:
            text = base64.b64decode(data['content']).decode('utf-8', 'ignore')
            return text[:_rc_base.CACHE_README_CHARS]
        except Exception:
            return ''

    def tree() -> List[str]:
        data, _ = _rc_sources.gh_get(f'{_rc_base.GH_API}/repos/{full}/git/trees/{branch}?recursive=1', ttl_s=_rc_base.CACHE_TTL_ENRICH)
        return [n.get('path', '') for n in (data or {}).get('tree', []) if n.get('type') in ('blob', 'tree')]

    def head() -> str:
        data, _ = _rc_sources.gh_get(f'{_rc_base.GH_API}/repos/{full}/git/ref/heads/{branch}', ttl_s=_rc_base.CACHE_TTL_ENRICH)
        return ((data or {}).get('object') or {}).get('sha', '')[:12]
    with futures.ThreadPoolExecutor(max_workers=3) as ex:
        r, t, h = (ex.submit(readme), ex.submit(tree), ex.submit(head))
        return {'readme': r.result(), 'paths': t.result(), 'head': h.result()}

def _gist(readme: str, limit: int=260) -> str:
    """README 前若干字：去掉徽章/链接/代码围栏，只留能说明「它是干什么的」的正文。

    只给描述时模型分不清「技能注册中心」与「agent 可观测平台」——
    实测（2026-10-21）Observal 被排在 skillhub 之前。README 正文是最便宜的纠偏信号。
    """
    text = re.sub('```.*?```', ' ', readme or '', flags=re.S)
    text = re.sub('!\\[[^\\]]*\\]\\([^)]*\\)', ' ', text)
    text = re.sub('\\[([^\\]]*)\\]\\([^)]*\\)', '\\1', text)
    text = re.sub('<[^>]+>', ' ', text)
    text = re.sub('[#*_>`|]+', ' ', text)
    text = re.sub('\\s+', ' ', text).strip()
    return text[:limit]
