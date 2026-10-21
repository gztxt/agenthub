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
CACHE_README_CHARS = 6000
MAX_CANDIDATES = 8            # 进入详情补全 / 重排的上限
SEARCH_PER_PAGE = 10
CARD_TTL_DAYS = 30            # 卡上「下次刷新」的间隔
JOB_KEEP_DAYS = 14
LIST_RE = re.compile(r"awesome|curated|list of|book|tutorial|course|roadmap|cheat|"
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
    return arms


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


def _doc_freq(pool: Dict[str, Dict], toks: List[str]) -> Dict[str, float]:
    n = max(1, len(pool))
    df = {t: sum(1 for r in pool.values() if t in _name_blob(r["item"])) for t in toks}
    return {t: 1.0 + math.log((n + 1) / (df[t] + 1)) for t in toks}


def _weighted_cov(blob: str, toks: List[str], w: Dict[str, float]) -> float:
    total = sum(w[t] for t in toks)
    return sum(w[t] for t in toks if t in blob) / total if total else 0.0


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


# ── LLM 重排（失败即降级，不是任务失败）──────────────────────────────────────

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


def rerank(module: str, cands: List[Dict[str, Any]], allow_paid: bool,
           budget_s: float = 95.0
           ) -> Tuple[Dict[str, Dict], str, str, List[Dict[str, Any]]]:
    """语义重排。返回 (结果, 失败原因, 用了哪个模型, 逐次尝试记录)。

    `allow_paid` 时**付费档优先**、免费档退居兜底：P0/P1 实测免费两档几乎稳定
    返回 only-thinking（占满超时），若排在前面会吃光预算，付费档根本轮不到。
    总预算 95s 兜住尾延迟：付费 60s + 免费 20s×2 ⇒ 超预算直接放弃并如实标注。
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
    free = [m for m in (llm.memory_model(), llm.free_fallback_model()) if m]
    models = ([_ref_model()] + free) if allow_paid else free
    errors: List[str] = []
    attempts: List[Dict[str, Any]] = []
    spent = 0.0
    for model in models:
        left = budget_s - spent
        if left < 8:
            errors.append(f"{model}:budget-exhausted")
            continue
        t0 = time.monotonic()
        cap = 60 if model == llm.MODEL else 25
        text, err, stop = _llm_text(prompt, model, timeout=int(min(cap, left)),
                                    max_tokens=4000)
        spent += time.monotonic() - t0
        attempts.append({"model": model, "ms": int((time.monotonic() - t0) * 1000),
                         "ok": bool(text), "error": err or "",
                         "stop_reason": stop})
        if not text:
            errors.append(f"{model}:{err or 'empty'}")
            continue
        data = _json_block(text)
        if not data or not isinstance(data.get("items"), list):
            attempts[-1].update({"ok": False, "error": "bad-json",
                                 "head": (text or "")[:120]})
            errors.append(f"{model}:bad-json")
            continue
        known = {c["repo"] for c in cands}
        out: Dict[str, Dict] = {}
        for item in data["items"]:
            repo = str(item.get("repo", ""))
            if repo not in known:
                continue
            try:
                out[repo] = {"func": min(1.0, max(0.0, float(item["func"]))),
                             "impl": min(1.0, max(0.0, float(item["impl"]))),
                             "design": min(1.0, max(0.0, float(item["design"]))),
                             "judge": str(item.get("judge", "read"))[:8],
                             "why": str(item.get("why", ""))[:80]}
            except (KeyError, TypeError, ValueError):
                continue
        if out and max(max(v["func"], v["impl"], v["design"]) for v in out.values()) >= 0.05:
            attempts[-1]["items"] = len(out)
            return out, "", model, attempts
        attempts[-1].update({"ok": False, "error": "degenerate"})
        errors.append(f"{model}:degenerate")
    return {}, "; ".join(errors), "", attempts


# ── 采集主流程（同步，跑在线程池里）──────────────────────────────────────────

def _judge_rank(judge: str) -> int:
    """use/fork < read < 未判定 < skip。未知必须排在「已判 skip」之前、
    「已判可读」之后 —— 把未知混进可借鉴区就是替模型编结论。"""
    return {"use": 0, "fork": 0, "read": 1}.get(judge, 3 if judge == "unjudged" else 2)


def apply_rerank(module: str, ranked: List[Dict[str, Any]],
                 allow_paid: bool) -> Tuple[str, str, List[Dict[str, Any]]]:
    """就地改写 `ranked` 的三轴分与排序。返回 (失败原因, 模型, 错误清单)。

    **独立成函数**是为了「先出词法卡、再原地升级」：任务层在重排前已经把卡落库，
    这里失败只影响状态字段，不会让用户没有结果可看。
    """
    impls = [c for c in ranked if c["bucket"] == "impl"][:MAX_CANDIDATES]
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
                        else "未进入语义重排队列（词法前 8 名之外）")
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

    toks = tokens_of(keywords)
    weights = _doc_freq(pool, toks)

    pre: List[Dict[str, Any]] = []
    for key, rec in pool.items():
        item = rec["item"]
        if item.get("archived"):
            continue
        bucket = classify(item)
        blob = _name_blob(item)
        cov = _weighted_cov(blob, toks, weights)
        matched = sum(1 for t in toks if t in blob)
        phrase = any((" " in k and k.lower() in blob) for k in keywords)
        hits = len(code_hits.get(key, []))
        if bucket == "impl" and cov < 0.18 and hits == 0:
            continue
        if bucket == "impl" and matched < 2 and not phrase and hits == 0:
            continue
        stars = int(item.get("stargazers_count") or 0)
        pre.append({"key": key, "item": item, "bucket": bucket, "cov": cov, "hits": hits,
                    "seen": rec["seen"],
                    "pre": 0.55 * cov + 0.20 * min(1, math.log10(stars + 1) / 4)
                    + 0.25 * min(1, hits / 3)})

    pre.sort(key=lambda x: (x["bucket"] != "impl", -x["pre"]))
    mark("enrich")
    for p in [x for x in pre if x["bucket"] == "impl"][:MAX_CANDIDATES]:
        branch = p["item"].get("default_branch") or "main"
        p["branch"] = branch
        p.update(_enrich(p["item"]["full_name"], branch))

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

    ranked.sort(key=lambda x: (x["bucket"] != "impl",
                               -(0.6 * x["func"] + 0.3 * x["impl"] + 0.1 * x["design"])))
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
    if meta.get("rerank"):
        cost = "（**付费档**，单次约几千 token）" if meta.get("rerank_paid") else "（免费档）"
        out.append(f"- 语义重排：{meta['rerank_model']}{cost}")
    elif meta.get("rerank_error"):
        out.append(f"- 语义重排：**未完成**，本卡仅词法分（原因：{meta['rerank_error']}）")
    else:
        out.append("- 语义重排：无（仅词法，判定列为空）")
    if meta.get("degraded"):
        out.append("- ⚠️ 降级臂：" + "；".join(meta["degraded"]))
    if not meta.get("token"):
        out.append("- ⚠️ 无 GitHub token：限额 60 次/时，结果可能不完整")
    out.append("")

    impls = [c for c in ranked if c["bucket"] == "impl"]
    others = [c for c in ranked if c["bucket"] != "impl"]
    if meta.get("rerank"):
        picked = [c for c in impls if c.get("judge") in ("use", "fork", "read")]
        low = [c for c in impls if c.get("judge") not in ("use", "fork", "read")]
    else:
        picked, low = impls, []

    out += ["## 候选（实现桶）", "",
            "| 仓库 | ★ | 最近推送 | 许可证 | 功能 | 实现 | 设计 | 判定 |",
            "|---|---:|---|---|---:|---:|---:|---|"]
    for c in picked[:limit]:
        lic = c["license"] + ("(copyleft)" if c["copyleft"] else "")
        out.append(f"| [{c['repo']}]({c['url']}) | {c['stars']} | "
                   f"{_badge(c['fresh_days'])} | {lic} | {c['func']:.2f} | "
                   f"{c['impl']:.2f} | {c['design']:.2f} | {c.get('judge') or '—'} |")
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


class ScoutIn(BaseModel):
    module: str = Field(min_length=2, max_length=200)
    keywords: List[str] = Field(default_factory=list)
    lang: str = Field(default="", max_length=20)
    min_stars: int = Field(default=20, ge=0, le=100000)
    fresh_days: int = Field(default=180, ge=7, le=3650)
    identifiers: List[str] = Field(default_factory=list)
    allow_paid: bool = False
    refresh_of: Optional[str] = None


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
               "identifiers": [i.strip() for i in body.identifiers if i.strip()][:3],
               "allow_paid": bool(body.allow_paid),
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


@router.post("/cards/{card_id}/refresh")
async def api_refresh(card_id: str):
    row = _card_row(card_id)
    if not row:
        raise HTTPException(404, "情报卡不存在")
    try:
        kw = json.loads(row["keywords"] or "[]")
        meta = json.loads(row["meta"] or "{}")
    except ValueError:
        kw, meta = [], {}
    body = ScoutIn(module=row["module"], keywords=kw, lang=row["lang"] or "",
                   identifiers=meta.get("identifiers") or [],
                   allow_paid=bool(meta.get("rerank_paid")), refresh_of=card_id)
    return submit(body)


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
            "card_ttl_days": CARD_TTL_DAYS,
            "paid_rerank": _ref_model(),
            "free_models": [llm.memory_model(), llm.free_fallback_model()]}


# ── 定时刷新 ────────────────────────────────────────────────────────────────

async def refresh_loop(interval_s: int = 1800) -> None:
    """到期的卡自动重跑（默认 30 天一次）。**每轮最多 1 张**，避免限流雪崩。"""
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
                           refresh_of=cid))
            log.info("refscout auto refresh queued: %s", cid)
        except Exception as exc:  # noqa: BLE001 —— 定时任务失败只记日志，不拖垮循环
            log.warning("refscout auto refresh failed: %s", exc)
