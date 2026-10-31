"""卡片渲染（**纯函数**：输入 dict，输出 markdown）。不碰 db / 网络。

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


def _badge(days: int) -> str:
    return '新' if days <= 90 else '温' if days <= 365 else '旧'

def render_md(module: str, keywords: List[str], lang: str, meta: Dict[str, Any], ranked: List[Dict[str, Any]], elapsed: float, as_of: str, next_refresh: str, limit: int=8, diff: Optional[Dict[str, Any]]=None) -> str:
    out = [f'# 参考情报：{module}', '', f'- as of：{as_of}\u3000下次刷新：{next_refresh}', f"- 关键词：{', '.join(keywords)}\u3000语言：{lang or '不限'}", f"- 候选池：{meta['pool_size']}\u3000耗时：{elapsed}s"]
    src = meta.get('keyword_source')
    if src == 'llm':
        out.append('- 关键词来源：模型从模块名抽取（可能抽偏，可手填覆盖）')
    elif src == 'fallback':
        out.append('- 关键词来源：**模型抽词失败，退回内置中文词典**（覆盖有限，建议手填关键词重跑）')
    if meta.get('identifiers'):
        out.append('- 代码标识符臂：' + '、'.join(meta['identifiers']) + '（在代码里搜该串，命中的仓额外加分）')
    srcs = meta.get('sources') or []
    if srcs:
        ok_src = [s for s in srcs if s.get('ok')]
        if ok_src:
            out.append('- 来源（自有仓，含 fork，不占搜索额度）：' + '、'.join((f"{s['label']}（{s['count']} 仓）" for s in ok_src)))
        bad_src = [s for s in srcs if not s.get('ok')]
        if bad_src:
            out.append('- ⚠️ 来源拉取失败：' + '、'.join((f"{s['label']}（{s.get('error') or '失败'}）" for s in bad_src)))
    expand = meta.get('list_expand') or []
    if expand:
        parts = []
        for e in expand:
            name = str(e.get('name') or '').replace('list:', '')
            if not e.get('ok'):
                parts.append(f'{name}（失败）')
            else:
                parts.append(f"{name}（{e.get('pages', 0)} 页 / {e.get('links', 0)} 链接 → 入池 {e['count']}）")
        out.append('- 清单展开：' + '；'.join(parts) + '（清单仓本身是目录，不计入候选）')
    if meta.get('rerank'):
        cost = '（**付费档**，单次约几千 token）' if meta.get('rerank_paid') else '（免费档）'
        out.append(f"- 语义重排：{meta['rerank_model']}{cost}")
    elif meta.get('rerank_error'):
        out.append(f"- 语义重排：**未完成**，本卡仅词法分（原因：{meta['rerank_error']}）")
        if 'safety-router' in str(meta['rerank_error']) and (not meta.get('requested_paid')):
            out.append('- ↳ 免费池被内容审核模型拦截（不是超时）；勾选「允许付费重排」后点「重跑」可恢复语义判定')
    else:
        out.append('- 语义重排：无（仅词法，判定列为空）')
    if meta.get('trigger') == 'auto':
        out.append('- 触发：定时自动刷新（**不付费**；需要语义重排请手动「重跑」）')
    if meta.get('degraded'):
        out.append('- ⚠️ 降级臂：' + '；'.join(meta['degraded']))
    if not meta.get('token'):
        out.append('- ⚠️ 无 GitHub token：限额 60 次/时，结果可能不完整')
    out.append('')
    impls = [c for c in ranked if c['bucket'] == 'impl']
    others = [c for c in ranked if c['bucket'] != 'impl']
    lset = {str(x).lower() for x in meta.get('list_sources') or []}
    if lset:
        impls = [c for c in impls if str(c.get('repo') or '').lower() not in lset]
        others = [c for c in others if str(c.get('repo') or '').lower() not in lset]
    if meta.get('rerank'):
        picked = [c for c in impls if c.get('judge') in ('use', 'fork', 'read')]
        low = [c for c in impls if c.get('judge') not in ('use', 'fork', 'read')]
    else:
        picked, low = (impls, [])
    out += ['## 候选（实现桶）', '', '| 仓库 | 来源 | ★ | 最近推送 | 许可证 | 功能 | 实现 | 设计 | 判定 |', '|---|---|---:|---|---|---:|---:|---:|---|']
    for c in picked[:limit]:
        lic = c['license'] + ('(copyleft)' if c['copyleft'] else '')
        src_cell = '、'.join(c.get('sources') or [])
        if c.get('via'):
            short = str(c['via'][0]).split('/')[-1]
            src_cell = (src_cell + ' · ' if src_cell else '') + f'清单:{short}'
        out.append(f"| [{c['repo']}]({c['url']}) | {src_cell or '—'} | {c['stars']} | {_badge(c['fresh_days'])} | {lic} | {c['func']:.2f} | {c['impl']:.2f} | {c['design']:.2f} | {c.get('judge') or '—'} |")
    if meta.get('sources'):
        shown = {id(c) for c in picked[:limit]}
        own = [c for c in ranked if c.get('sources')]
        out += ['', '## 自有来源命中（含 fork）', '']
        if own:
            for c in own[:12]:
                tail = '（已在上表）' if id(c) in shown else ''
                kind = '清单（已展开）' if str(c['repo']).lower() in lset else c['bucket']
                out.append(f"- [{c['repo']}]({c['url']}) — {kind}，功能 {c['func']:.2f}{tail}：{c['desc']}")
        else:
            out.append('- （无：本轮关键词未命中自有仓；可补关键词，或确认来源已同步）')
    out += ['', '## 资料/清单桶（不参与实现对比）', '']
    out += [f"- [{c['repo']}]({c['url']}) — {c['desc']}" for c in others] or ['- （无）']
    if low:
        out += ['', '## 未入选（语义判定 skip / 未进重排）', '']
        for c in low[:10]:
            out.append(f"- {c['repo']}（{c.get('judge') or '—'}）：{c.get('why') or '未进入重排队列'}")
    if diff and (diff.get('added') or diff.get('removed')):
        out += ['', '## 本次刷新变化', '']
        for r in diff.get('added', [])[:10]:
            out.append(f'- 新增：{r}')
        for r in diff.get('removed', [])[:10]:
            out.append(f'- 移除：{r}')
    out += ['', '## 可借鉴点（均需人工复核）', '']
    for c in picked[:limit]:
        out.append(f"### {c['repo']} · 功能{c['func']:.2f}/实现{c['impl']:.2f}/设计{c['design']:.2f}")
        out.append(f"- 一句话：{c['desc']}")
        if c.get('readme_gist'):
            out.append(f"- 摘要：{c['readme_gist']}")
        if c.get('sources'):
            out.append('- 来源：自有仓（' + '、'.join(c['sources']) + '，不走搜索额度、含 fork）')
        if c.get('via'):
            out.append('- 收录于清单：' + '、'.join(c['via']) + '（本项目由清单展开得到）')
        if c.get('why'):
            out.append(f"- 重排理由：{c['why']}（{c.get('judge')}）")
        out.append(f"- 设计资料：{('有 ADR/architecture 目录' if c['arch_docs'] else '未见 ADR')}")
        if c.get('head'):
            out.append(f"- 固定引用：`{c['repo']}@{c['head']}`（分支 {c['branch']}）")
        for p in c.get('code_files') or []:
            out.append(f"- 代码命中：[{p}](https://github.com/{c['repo']}/blob/{c['branch']}/{p})")
        if c['copyleft']:
            out.append('- ⚠️ copyleft：只允许借鉴设计，**不得摘录代码**')
        elif c['license'] in ('UNKNOWN', 'NOASSERTION'):
            out.append('- ⚠️ 许可证未识别：默认不得复用代码，需人工核实')
        out.append('')
    out += ['## 逐路表态', '']
    for a in meta.get('arms', []):
        state = 'ok' if a['ok'] else f"FAIL {a.get('error') or ''}"
        cached = '（缓存）' if a.get('cached') else ''
        out.append(f"- {a['name']}：{a['count']} 条 / {a['ms']}ms{cached} / {state}")
    return '\n'.join(out) + '\n'
