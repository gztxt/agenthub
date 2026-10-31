"""清单展开（awesome / HelloGitHub → 它收录的项目）+ 采集主流程。网络密集，跑在线程池。

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
import refscout_rank as _rc_rank

_GH_LINK_RE = re.compile('github\\.com[/:]([A-Za-z0-9][A-Za-z0-9_.-]*)/([A-Za-z0-9_.-]{1,80})')
_GH_RESERVED = {'features', 'topics', 'sponsors', 'marketplace', 'login', 'orgs', 'users', 'settings', 'about', 'pricing', 'apps', 'collections', 'trending', 'search', 'security', 'events', 'readme', 'contact', 'enterprise', 'solutions', 'site', 'git-guides', 'open-source', 'customer-stories', 'team', 'home'}
_MD_SELF_RE = re.compile('\\]\\(\\s*(?!https?://|#)([^)\\s]+?\\.md)(?:#[^)]*)?\\s*\\)')

def _gh_links(text: str, self_full: str='') -> List[str]:
    """从 markdown 抽 `owner/repo`（保序去重），剔站点功能页与自引用。"""
    self_key = (self_full or '').lower()
    seen, out = (set(), [])
    for owner, repo in _GH_LINK_RE.findall(text or ''):
        repo = re.sub('\\.git$', '', repo, flags=re.I).strip('.,;:')
        full = f'{owner}/{repo}'
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
    for p in _MD_SELF_RE.findall(text or ''):
        p = p.strip().lstrip('./')
        if not p or p in out or (not p.lower().endswith('.md')):
            continue
        if p.lower().split('/')[0] in ('content', 'docs', 'data', 'posts', 'pages', 'issues'):
            out.append(p)
    return out[:limit] if limit else []

def _blob_text(url: str) -> str:
    """取 GitHub contents API 的 base64 正文；失败返回空串（不抛）。"""
    data, _ = _rc_sources.gh_get(url, ttl_s=_rc_base.CACHE_TTL_ENRICH)
    if not isinstance(data, dict) or not data.get('content'):
        return ''
    try:
        return base64.b64decode(data['content']).decode('utf-8', 'ignore')[:_rc_base.CACHE_README_CHARS * 4]
    except Exception:
        return ''

def _readme_text(full: str) -> str:
    return _blob_text(f'{_rc_base.GH_API}/repos/{full}/readme')

def _contents_text(full: str, path: str) -> str:
    return _blob_text(f'{_rc_base.GH_API}/repos/{full}/contents/{urllib.parse.quote(path)}')

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
        item = p['item']
        full = item.get('full_name') or ''
        if not full or full.lower() in seen:
            continue
        owned = bool(p.get('sources'))
        signals = 0
        if owned and p.get('readme'):
            signals = len(_gh_links(p['readme'], full)) + len(_md_subpages(p['readme'], 100))
        if p['bucket'] == 'list' or (owned and signals >= _rc_base.LIST_SIGNAL_LINKS):
            seen.add(full.lower())
            out.append(item)
        if len(out) >= limit:
            break
    return out

def _expand_lists(lists: List[Dict[str, Any]], min_stars: int, known: set, keep: Optional[Any]=None) -> Tuple[Dict[str, Dict[str, Any]], List[Dict[str, Any]]]:
    """把清单仓展开成收录项目，返回 ({key: {item, via}}, 逐清单日志)。

    `known` 是已在候选池里的 key 集合（小写 full_name），用于避免重复拉取。
    `archived` 与 `min_stars` 照常生效：清单里常有已归档项目，拉回来只会占重排名额。
    `keep(item)` 是与主池一致的覆盖度闸门：不该只在 `build_pre` 里过滤 —— 那样
    无关清单会把整份展开额度吃光（实测 `awesome-claude-plugins` 一次占掉 90/96）。
    """
    found: Dict[str, Dict[str, Any]] = {}
    log_rows: List[Dict[str, Any]] = []
    for lst in lists[:_rc_base.LIST_EXPAND_LISTS]:
        full = lst.get('full_name') or ''
        if not full or len(found) >= _rc_base.LIST_EXPAND_TOTAL:
            continue
        t0 = time.monotonic()
        readme = _readme_text(full)
        links = _gh_links(readme, full)
        seen = {x.lower() for x in links}
        pages = 0
        if len(links) < 20:
            for path in _md_subpages(readme, _rc_base.LIST_EXPAND_SUBPAGES):
                text = _contents_text(full, path)
                if not text:
                    continue
                pages += 1
                for x in _gh_links(text, full):
                    if x.lower() not in seen and len(links) < _rc_base.LIST_EXPAND_PER_LIST:
                        seen.add(x.lower())
                        links.append(x)
                if len(links) >= _rc_base.LIST_EXPAND_PER_LIST:
                    break
        wanted = [x for x in links if x.lower() not in known and x.lower() not in found][:_rc_base.LIST_EXPAND_TOTAL - len(found)]
        added = 0
        with futures.ThreadPoolExecutor(max_workers=_rc_base.LIST_EXPAND_THREADS) as ex:
            futs = {ex.submit(_rc_sources.gh_get, f'{_rc_base.GH_API}/repos/{x}', _rc_base.CACHE_TTL_ENRICH): x for x in wanted}
            for fut in futures.as_completed(futs):
                data, _ = fut.result()
                if not isinstance(data, dict) or not data.get('full_name'):
                    continue
                if data.get('archived'):
                    continue
                if int(data.get('stargazers_count') or 0) < min_stars:
                    continue
                if keep is not None and (not keep(data)):
                    continue
                key = str(data['full_name']).lower()
                found.setdefault(key, {'item': data, 'via': set()})['via'].add(full)
                added += 1
        log_rows.append({'name': f'list:{full}', 'q': '', 'ok': bool(readme), 'count': added, 'links': len(links), 'pages': pages, 'ms': int((time.monotonic() - t0) * 1000), 'cached': False, 'error': None if readme else 'readme 为空/拉取失败'})
    return (found, log_rows)

def _llm_text(prompt: str, model: str, timeout: int, max_tokens: int=1800) -> Tuple[str, str, str]:
    """同步调 Anthropic 兼容网关。返回 (text, error, stop_reason)。

    地址/凭据直接取 `llm` 模块常量（同源单点，避免在这里再判一次 `/v1` 拼接）。
    """
    base = (llm.BASE_URL or '').rstrip('/')
    key = llm.API_KEY
    if not key:
        return ('', 'no-key', '')
    body = json.dumps({'model': model, 'max_tokens': max_tokens, 'messages': [{'role': 'user', 'content': prompt}]}).encode()
    req = urllib.request.Request(f'{base}/v1/messages', data=body, headers={'Content-Type': 'application/json', 'anthropic-version': '2023-06-01', 'Authorization': f'Bearer {key}'})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        text = ''.join((b.get('text', '') for b in data.get('content', [])))
        return (text, '' if text else 'only-thinking', str(data.get('stop_reason') or ''))
    except urllib.error.HTTPError as exc:
        return ('', f'HTTP{exc.code}', '')
    except Exception as exc:
        return ('', type(exc).__name__, '')
_SAFETY_REPLY = re.compile('^\\s*User\\s+Safety\\s*:', re.I)

def _is_safety_reply(text: str) -> bool:
    """这段正文是内容审核标签，不是重排结果。

    2026-10-21 实测（8 候选、`allow_paid=False`，4 轮）：2 轮返回
    `User Safety: safe`，兜底位返回 `User Safety: unsafe` +
    `Safety Categories: Malware, Needs Caution`。**换同一池的下一个模型
    照样撞**（两轮都中），所以命中即停，不把 95s 预算烧在审核模型上。
    这与「模型不听话」是两回事：路由选错了模型。
    """
    return bool(_SAFETY_REPLY.match(text or ''))

def _json_block(text: str) -> Optional[dict]:
    """从严到松解析模型输出。

    实测（2026-10-21）：8 个候选 + 中文理由会在 2000 tokens 处**从字符串中间截断**，
    严格 `json.loads` 必然失败 —— 于是整轮重排白跑。这里按「最后一个完整对象」打捞：
    截断只丢尾部若干候选，已判出的分数仍可用；一个都不剩才返回 None。
    """
    m = re.search('\\{.*\\}', text or '', re.S)
    seg = m.group(0) if m else (text or '').strip()
    if not seg:
        return None
    try:
        return json.loads(seg)
    except ValueError:
        pass
    cuts = [i + 1 for i, ch in enumerate(seg) if ch == '}'][::-1]
    for cut in cuts[:40]:
        head = seg[:cut]
        for suffix in ('', ']}', '}]}'):
            try:
                obj = json.loads(head + suffix)
            except ValueError:
                continue
            if isinstance(obj, dict) and isinstance(obj.get('items'), list):
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
    return os.getenv('REF_LLM_MODEL', 'agnes/agnes-2.5-flash')

def _sample_count() -> int:
    """付费档采样次数（`REF_RERANK_SAMPLES`，默认 3，夹在 1~5）。

    为什么要采样：同一 prompt 同一模型判词会翻。2026-10-21 同一张卡两轮——
    一轮三个候选全 skip，另一轮 `HelloGitHub` 判 read（func 0.8），
    对应「实现桶整段空」与「有一条可借鉴」两种完全不同的结论。
    """
    try:
        n = int(os.getenv('REF_RERANK_SAMPLES', '3'))
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
            counts[r['judge']] = counts.get(r['judge'], 0) + 1
        top = max(counts.values())
        judge = max((j for j, c in counts.items() if c == top), key=_judge_rank)
        why = next((r['why'] for r in recs if r['judge'] == judge))
        out[repo] = {'func': _median([r['func'] for r in recs]), 'impl': _median([r['impl'] for r in recs]), 'design': _median([r['design'] for r in recs]), 'judge': judge, 'why': why}
    return out

def rerank(module: str, cands: List[Dict[str, Any]], allow_paid: bool, budget_s: float=95.0) -> Tuple[Dict[str, Dict], str, str, List[Dict[str, Any]]]:
    """语义重排。返回 (结果, 失败原因, 用了哪个模型, 逐次尝试记录)。

    `allow_paid` 时**付费档优先**、免费档退居兜底：P0/P1 实测免费两档几乎稳定
    返回 only-thinking（占满超时），若排在前面会吃光预算，付费档根本轮不到。
    总预算 95s 兜住尾延迟：付费档按 `REF_RERANK_SAMPLES`（默认 3）**采样取多数**，
    每次 cap 取「剩余预算 ÷ 剩余次数」（≤60s）；免费档 25s×2 ⇒ 超预算直接放弃并如实标注。
    """
    if not cands:
        return ({}, 'no-candidates', '', [])
    lines = []
    for c in cands:
        files = ', '.join(c.get('sample_paths', [])[:5])
        topics = ', '.join(c.get('topics') or [])
        gist = c.get('readme_gist') or _rc_rank._gist(c.get('readme') or '')
        lines.append(f"- {c['repo']} | ★{c['stars']} | {c['desc'][:100]}" + (f' | topics: {topics}' if topics else '') + (f' | README: {gist}' if gist else '') + f' | 文件: {files}')
    prompt = f'你是技术选型调研员。判断候选 GitHub 仓库对目标模块的可借鉴价值。\n不要因为星多或名字像就给高分。三维度 0~1：func=是否解决同一问题；impl=实现路径是否可参考；design=架构决策是否有借鉴。\n只共享一个词、或只是清单/教程 ⇒ skip。**只是相邻领域也要 skip**：\n例如目标要「技能扫描与调用记账」，则安全扫描、模型可观测性、通用 API 网关都只是相邻，不能判 read 以上。\n输出严格 JSON，不要 Markdown：{{"items":[{{"repo":"owner/name","func":0.0,"impl":0.0,"design":0.0,"judge":"use|fork|read|skip","why":"不超过30字"}}]}}\n\n目标模块：{module}\n候选：\n' + '\n'.join(lines)
    paid = _ref_model() if allow_paid else ''
    free = [m for m in (llm.memory_model(), llm.free_fallback_model()) if m]
    errors: List[str] = []
    attempts: List[Dict[str, Any]] = []
    spent = 0.0

    def _one(model: str, cap: float, mark: Dict[str, Any]) -> Tuple[Optional[Dict[str, Dict]], str]:
        """跑**一次**模型，返回 `(判定表, 失败原因)`；原因空串表示成功。

        `cap` 由调用方给：付费档要按剩余预算均分，免费档固定 25s。
        """
        nonlocal spent
        left = budget_s - spent
        if left < 8:
            errors.append(f'{model}:budget-exhausted')
            return (None, 'budget-exhausted')
        t0 = time.monotonic()
        text, err, stop = _llm_text(prompt, model, timeout=int(min(cap, left)), max_tokens=4000)
        spent += time.monotonic() - t0
        rec = {'model': model, 'ms': int((time.monotonic() - t0) * 1000), 'ok': bool(text), 'error': err or '', 'stop_reason': stop}
        rec.update(mark)
        attempts.append(rec)
        if not text:
            errors.append(f"{model}:{err or 'empty'}")
            return (None, err or 'empty')
        if _is_safety_reply(text):
            attempts[-1].update({'ok': False, 'error': 'safety-router', 'head': text.strip()[:80]})
            errors.append(f'{model}:safety-router')
            return (None, 'safety-router')
        data = _json_block(text)
        if not data or not isinstance(data.get('items'), list):
            attempts[-1].update({'ok': False, 'error': 'bad-json', 'head': (text or '')[:120]})
            errors.append(f'{model}:bad-json')
            return (None, 'bad-json')
        known = {str(c['repo']).lower(): c['repo'] for c in cands}
        out: Dict[str, Dict] = {}
        for item in data['items']:
            canon = known.get(str(item.get('repo', '')).strip().lower())
            if not canon:
                continue
            try:
                out[canon] = {'func': min(1.0, max(0.0, float(item['func']))), 'impl': min(1.0, max(0.0, float(item['impl']))), 'design': min(1.0, max(0.0, float(item['design']))), 'judge': str(item.get('judge', 'read')).strip().lower()[:8], 'why': str(item.get('why', ''))[:80]}
            except (KeyError, TypeError, ValueError):
                continue
        if not out:
            attempts[-1].update({'ok': False, 'error': 'empty-verdict'})
            errors.append(f'{model}:empty-verdict')
            return (None, 'empty-verdict')
        attempts[-1]['items'] = len(out)
        attempts[-1]['judges'] = {k: v['judge'] for k, v in out.items()}
        return (out, '')
    samples = _sample_count() if paid else 1
    if paid:
        votes: List[Dict[str, Dict]] = []
        for i in range(samples):
            left = budget_s - spent
            if left < 8:
                errors.append(f'{paid}:budget-exhausted')
                break
            cap = min(60.0, max(15.0, left / max(1, samples - i)))
            out, why = _one(paid, cap, {'sample': i + 1})
            if out:
                votes.append(out)
            elif why == 'safety-router':
                break
        got = _majority(votes)
        if got:
            return (got, '', paid, attempts)
    for model in free:
        out, why = _one(model, 25, {})
        if out:
            return (out, '', model, attempts)
        if why == 'safety-router':
            break
    return ({}, '; '.join(errors), '', attempts)

def _judge_rank(judge: str) -> int:
    """use/fork < read < 未判定 < skip。未知必须排在「已判 skip」之前、
    「已判可读」之后 —— 把未知混进可借鉴区就是替模型编结论。

    2026-10-21 修正：旧实现把 `skip` 与任意未知字符串并列在 2，又把
    `unjudged` 单列成 3 ⇒ **未判定反而排在被明确否决的仓之后**，与本函数
    注释相反。重排只覆盖词法前 8 名，尾部候选和「模型只返回一部分」的候选
    全是 `unjudged`，卡面等于「没看过的仓先被压到已否决之下」。
    现改为 skip=3、其余未知=2，档位严格是 use/fork < read < 未判定 < skip。
    """
    return {'use': 0, 'fork': 0, 'read': 1, 'skip': 3}.get(str(judge or '').strip().lower(), 2)

def apply_rerank(module: str, ranked: List[Dict[str, Any]], allow_paid: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    """就地改写 `ranked` 的三轴分与排序。返回 (失败原因, 模型, 错误清单)。

    **独立成函数**是为了「先出词法卡、再原地升级」：任务层在重排前已经把卡落库，
    这里失败只影响状态字段，不会让用户没有结果可看。
    """
    impls = [c for c in ranked if c['bucket'] == 'impl' and c.get('enriched')][:_rc_base.MAX_CANDIDATES]
    got, err, used_model, attempts = rerank(module, impls, allow_paid)
    if not got:
        return (err, '', attempts)
    judged = set(got)
    sliced = {c['repo'] for c in impls}
    for c in ranked:
        if c['bucket'] == 'impl' and c['repo'] not in judged:
            c['judge'] = 'unjudged'
            truncated = any((a.get('stop_reason') == 'max_tokens' for a in attempts))
            c['why'] = '输出被截断（max_tokens），本条未判' if truncated and c['repo'] in sliced else '模型未返回该条判定' if c['repo'] in sliced else '未进入语义重排队列（补全窗口之外）'
    for c in ranked:
        r = got.get(c['repo'])
        if not r:
            continue
        c.update({'judge': r['judge'], 'why': r['why'], 'llm': r})
        c['func'] = round(0.55 * c['func'] + 0.45 * r['func'], 3)
        c['impl'] = round(0.55 * c['impl'] + 0.45 * r['impl'], 3)
        c['design'] = round(0.55 * c['design'] + 0.45 * r['design'], 3)
    ranked.sort(key=lambda x: (x['bucket'] != 'impl', _judge_rank(x['judge']), -(0.5 * x['func'] + 0.3 * x['impl'] + 0.2 * x['design'])))
    return ('', used_model, attempts)

def _order_key(c: Dict[str, Any]) -> Tuple[int, float, float]:
    """排序主键：**参考价值优先**，来源只作同分次序。

    0.06 的 `src_bonus` 曾经**加进**加权分里，结果是「我的仓」能压过功能分略高的外部仓
    —— 与「没有参考价值就排后」直接相反（2026-10-21 用户裁定）。现在它只进第三位：
    三轴分不同就按分排，分数**完全相等**时才让自己的仓靠前。
    """
    return (c['bucket'] != 'impl', -(0.6 * c['func'] + 0.3 * c['impl'] + 0.1 * c['design']), -c.get('src_bonus', 0.0))

def collect(module: str, keywords: List[str], lang: str, min_stars: int, fresh_days: int, identifiers: List[str], allow_paid: bool, stage: Optional[Any]=None, do_rerank: bool=True) -> Dict[str, Any]:
    """跑一次采集，返回 {meta, candidates, status, elapsed}。

    `stage(name)` 由任务层注入，用于把进度落到 `ref_jobs.stage`。
    """

    def mark(name: str) -> None:
        if stage:
            stage(name)
    t0 = time.monotonic()
    arms_log: List[Dict[str, Any]] = []
    pool: Dict[str, Dict[str, Any]] = {}
    mark('search')
    for name, q, sort in _rc_rank.build_queries(keywords, module, min_stars, fresh_days):
        url = f'{_rc_base.GH_API}/search/repositories?q={urllib.parse.quote(q)}&sort={sort}&order=desc&per_page={_rc_base.SEARCH_PER_PAGE}'
        data, meta = _rc_sources.gh_get(url, ttl_s=_rc_base.CACHE_TTL_SEARCH, limiter=_rc_sources._SEARCH_LIMIT)
        items = (data or {}).get('items', []) if data else []
        arms_log.append({'name': name, 'q': q, 'ok': meta.get('ok', False), 'count': len(items), 'ms': meta.get('ms', 0), 'cached': meta.get('cached', False), 'error': meta.get('error'), 'total': (data or {}).get('total_count'), 'rate_limited': meta.get('rate_limited', False)})
        for it in items:
            key = (it.get('full_name') or '').lower()
            if not key:
                continue
            rec = pool.setdefault(key, {'item': it, 'seen': set()})
            rec['seen'].add(name)
    code_hits: Dict[str, List[str]] = {}
    for ident in identifiers[:2]:
        q = urllib.parse.quote('"%s" in:file' % ident)
        url = f'{_rc_base.GH_API}/search/code?q={q}&per_page=5'
        data, meta = _rc_sources.gh_get(url, ttl_s=_rc_base.CACHE_TTL_SEARCH, limiter=_rc_sources._CODE_LIMIT)
        items = (data or {}).get('items', []) if data else []
        arms_log.append({'name': f'code:{ident}', 'q': ident, 'ok': meta.get('ok', False), 'count': len(items), 'ms': meta.get('ms', 0), 'cached': meta.get('cached', False), 'error': meta.get('error'), 'rate_limited': meta.get('rate_limited', False)})
        for it in items:
            full = (it.get('repository') or {}).get('full_name', '')
            if not full:
                continue
            key = full.lower()
            code_hits.setdefault(key, []).append(it.get('path', ''))
            if key not in pool:
                data2, _ = _rc_sources.gh_get(f'{_rc_base.GH_API}/repos/{full}', ttl_s=_rc_base.CACHE_TTL_ENRICH)
                if data2 and data2.get('full_name'):
                    pool[key] = {'item': data2, 'seen': {'code'}}
    mark('sources')
    src_log, src_overview, src_by_key = _rc_sources._sync_sources()
    arms_log.extend(src_log)
    for key, srec in src_by_key.items():
        rec = pool.setdefault(key, {'item': srec['item'], 'seen': set()})
        rec['seen'].update(('src:' + lb for lb in srec['labels']))
        rec['sources'] = sorted(set(rec.get('sources') or []) | set(srec['labels']))
    toks = _rc_rank.tokens_of(keywords)
    groups = _rc_rank.alias_groups(keywords)

    def build_pre() -> List[Dict[str, Any]]:
        """按**当前池**算覆盖度与预分并排序。

        独立成函数是因为「清单展开」会把新项目塞进池：IDF 与覆盖度都随之变化，
        不重算就等于用旧权重给新候选打分。
        """
        weights = _rc_rank._concept_weights(pool, groups)
        out: List[Dict[str, Any]] = []
        for key, rec in pool.items():
            item = rec['item']
            if item.get('archived'):
                continue
            bucket = _rc_rank.classify(item)
            blob = _rc_rank._expand_blob(_rc_rank._norm_text(_rc_rank._name_blob(item)))
            cov, matched = _rc_rank._concept_cov(blob, groups, weights)
            phrase = any((' ' in str(k) and _rc_rank._norm_text(str(k)) in blob for k in keywords))
            hits = len(code_hits.get(key, []))
            srcs = rec.get('sources') or []
            if bucket == 'impl' and cov < 0.18 and (hits == 0):
                continue
            if bucket == 'impl' and matched < min(2, len(groups)) and (not phrase) and (hits == 0):
                continue
            if srcs and matched == 0 and (cov < 0.18) and (not phrase) and (hits == 0):
                continue
            stars = int(item.get('stargazers_count') or 0)
            out.append({'key': key, 'item': item, 'bucket': bucket, 'cov': cov, 'hits': hits, 'seen': rec['seen'], 'sources': srcs, 'via': rec.get('via') or set(), 'pre': 0.55 * cov + 0.2 * min(1, math.log10(stars + 1) / 4) + 0.25 * min(1, hits / 3) + (_rc_base.SOURCE_BONUS if srcs else 0)})
        out.sort(key=lambda x: (x['bucket'] != 'impl', -x['pre']))
        return out
    pre = build_pre()

    def enrich_top(target: List[Dict[str, Any]]) -> None:
        """补全前 `ENRICH_LIMIT` 个实现候选（已补过的跳过）。"""
        n = 0
        for p in target:
            if p['bucket'] != 'impl' or p.get('enriched'):
                continue
            if n >= _rc_base.ENRICH_LIMIT:
                break
            n += 1
            p['enriched'] = True
            branch = p['item'].get('default_branch') or 'main'
            p['branch'] = branch
            p.update(_rc_rank._enrich(p['item']['full_name'], branch))
    mark('enrich')
    enrich_top(pre)
    expanded_log: List[Dict[str, Any]] = []
    list_sources: List[str] = []
    if os.getenv('REF_LIST_EXPAND', '1') != '0':
        lists = _pick_lists(pre, _rc_base.LIST_EXPAND_LISTS)
        list_sources = [str(x.get('full_name') or '') for x in lists if x.get('full_name')]
        if lists:
            mark('expand')
            w0 = _rc_rank._concept_weights(pool, groups)

            def keep(item: Dict[str, Any]) -> bool:
                blob = _rc_rank._expand_blob(_rc_rank._norm_text(_rc_rank._name_blob(item)))
                return _rc_rank._concept_cov(blob, groups, w0)[0] >= 0.18
            found, expanded_log = _expand_lists(lists, min_stars, set(pool), keep)
            for key, rec in found.items():
                pool[key] = {'item': rec['item'], 'seen': set(), 'via': rec['via']}
            if found:
                pre = build_pre()
                enrich_top(pre)
    ranked: List[Dict[str, Any]] = []
    for p in pre:
        item, bucket = (p['item'], p['bucket'])
        stars = int(item.get('stargazers_count') or 0)
        lang_ok = 1.0 if not lang or (item.get('language') or '').lower() == lang.lower() else 0.0
        text = (p.get('readme') or '').lower()
        paths = p.get('paths') or []
        low_paths = [x.lower() for x in paths[:6000]]
        arch_docs = any(('adr' in x or 'architecture' in x for x in low_paths))
        arch_signal = min(1.0, sum((text.count(t) for t in _rc_base.ARCH_TERMS)) / 12.0)
        path_hits = sum((1 for x in low_paths if any((t in x for t in toks))))
        impl_path = min(1.0, path_hits / 25.0)
        desc_l = (item.get('description') or '').lower()
        neg = sum((1 for t in toks if re.search(f'\\b(no|without|not)\\b[^.]{{0,40}}{re.escape(t)}', desc_l)))
        name_l = (item.get('name') or '').lower()
        name_bonus = 0.08 if sum((1 for t in toks if t in name_l)) >= 2 else 0.0
        func = 0.7 * p['cov'] + 0.2 * min(1, math.log10(stars + 1) / 4) + 0.1 * lang_ok + name_bonus - 0.25 * min(2, neg)
        impl = 0.35 * lang_ok + 0.35 * impl_path + 0.3 * p['cov']
        design = 0.6 * (1.0 if arch_docs else 0.0) + 0.4 * arch_signal
        lic = (item.get('license') or {}).get('spdx_id') or 'UNKNOWN'
        ranked.append({'repo': item.get('full_name'), 'url': item.get('html_url'), 'desc': (item.get('description') or '')[:160], 'bucket': bucket, 'seen': sorted(p['seen']), 'sources': p['sources'], 'via': sorted(p.get('via') or []), 'enriched': bool(p.get('enriched')), 'src_bonus': _rc_base.SOURCE_BONUS if p['sources'] else 0.0, 'func': round(func, 3), 'impl': round(impl, 3), 'design': round(design, 3), 'coverage': round(p['cov'], 3), 'stars': stars, 'license': lic, 'copyleft': bool(_rc_base.GPL_RE.search(lic)), 'fresh_days': _rc_rank._days_since(item.get('pushed_at')), 'arch_docs': arch_docs, 'branch': p.get('branch') or item.get('default_branch') or 'main', 'head': p.get('head', ''), 'code_files': code_hits.get(p['key'], [])[:3], 'topics': (item.get('topics') or [])[:8], 'readme_gist': _rc_rank._gist(p.get('readme') or ''), 'sample_paths': [x for x in paths if '/' in x][:6], 'judge': '', 'why': '', 'llm': None})
    ranked.sort(key=_order_key)
    lj = chain_judge.pick_candidate(('模块 %s；关键词 %s' % (module, '、'.join(keywords[:8]))).strip(), [{'name': r['repo'], 'desc': r['desc']} for r in ranked[:4]])
    if lj.get('ok'):
        lj['moved'] = chain_judge.promote_within_bucket(ranked, int(lj['index']))
        for r in ranked:
            if r['repo'] == lj.get('picked'):
                r['local_judge'] = {'picked': True, 'moved': lj['moved'], 'confidence': lj.get('confidence'), 'ms': lj.get('ms'), 'model': lj.get('model')}
                break
    rerank_err, used_model = ('', '')
    if do_rerank and ranked:
        mark('rerank')
        rerank_err, used_model, _errors = apply_rerank(module, ranked, allow_paid)
        if not used_model:
            arms_log.append({'name': 'llm-rerank', 'q': '', 'ok': False, 'count': 0, 'ms': 0, 'error': rerank_err or 'empty/invalid JSON', 'cached': False})
    failed = [a for a in arms_log if not a['ok'] and (not a['name'].startswith('llm-'))]
    meta = {'arms': arms_log, 'local_judge': lj, 'pool_size': len(pool), 'sources': src_overview, 'list_expand': expanded_log, 'list_sources': list_sources, 'rerank': bool(used_model), 'rerank_model': used_model, 'rerank_paid': bool(used_model and used_model not in (llm.memory_model(), llm.free_fallback_model())), 'rerank_error': rerank_err, 'degraded': [a['name'] + ':' + str(a.get('error') or 'fail') for a in failed], 'all_arms_failed': bool(arms_log) and all((not a['ok'] for a in arms_log if not a['name'].startswith('llm-'))), 'cache': _rc_base.cache_stats(), 'token': bool(_rc_base.token())}
    status = 'reranked' if used_model else 'lexical_fallback' if rerank_err else 'lexical'
    elapsed = round(time.monotonic() - t0, 1)
    mark('render')
    return {'meta': meta, 'candidates': ranked, 'status': status, 'elapsed': elapsed}
