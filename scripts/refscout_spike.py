#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""refscout spike v3（P0）：功能模块 → GitHub 借鉴源情报卡。

实测演进（2026-10-21）：
- v1：`in:readme` 让 awesome 清单霸榜；单关键词臂把通用词扩成整个生态；代码命中未入池。
- v2：主臂收窄到 `in:name,description,topics`，精度大幅提升（`terminal websocket pty` 仅 9 条），
  但纯词法仍把「desc 里恰好写了 port forwarding」的仓库排到真答案前面 ⇒ 需要语义重排。
- v3：加入 LLM 三轴重排（非会话免费档），实现轴改用目录树文件名命中（不额外烧代码搜索额度），
  copyleft / 未知许可证在卡上直接标红。

只读：不克隆、不改任何项目。
"""
from __future__ import annotations

import argparse
import base64
import concurrent.futures as futures
import hashlib
import json
import math
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(ROOT, "work", "refscout-cache")
ENV_CANDIDATES = (os.path.join(ROOT, ".env"),
                  "/fs/1000/ftp/技术文档/agenthub/.env")
GH_API = "https://api.github.com"
UA = "refscout-spike/0.3"

LIST_RE = re.compile(r"awesome|curated|list of|book|tutorial|course|roadmap|cheat|interview|examples?$", re.I)
DOC_RE = re.compile(r"docs?$|documentation|wiki|handbook|guide", re.I)
ARCH_TERMS = ("architecture", "design", "trade-off", "tradeoff", "why we", "rationale", "adr")
GPL_RE = re.compile(r"gpl|agpl", re.I)
STOP = {"the", "and", "for", "with", "from", "your", "this", "that", "into", "over",
        "based", "using", "simple", "基于", "实现", "支持", "一个", "以及", "相关"}


# ---------- 基础 IO ----------

def _load_dotenv() -> None:
    for path in ENV_CANDIDATES:
        if not os.path.isfile(path):
            continue
        with open(path, encoding="utf-8", errors="ignore") as fh:
            for line in fh:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                os.environ.setdefault(k.strip(), v.strip().strip('"').strip("'"))
        return


def _token() -> str:
    tok = os.environ.get("GITHUB_TOKEN", "").strip()
    if tok:
        return tok
    try:
        with open("/fs/1000/ftp/技术文档/github.txt", encoding="utf-8") as fh:
            return fh.readline().strip()
    except OSError:
        return ""


TOKEN = ""


def _headers() -> dict:
    h = {"Accept": "application/vnd.github+json", "User-Agent": UA}
    if TOKEN:
        h["Authorization"] = f"Bearer {TOKEN}"
    return h


def _cache_path(url: str) -> str:
    return os.path.join(CACHE_DIR, hashlib.sha1(url.encode()).hexdigest() + ".json")


def _cache_get(url: str, ttl_s: int = 7 * 86400):
    p = _cache_path(url)
    try:
        if time.time() - os.path.getmtime(p) > ttl_s:
            return None
        with open(p, encoding="utf-8") as fh:
            return json.load(fh)
    except (OSError, ValueError):
        return None


def _cache_put(url: str, payload) -> None:
    os.makedirs(CACHE_DIR, exist_ok=True)
    try:
        with open(_cache_path(url), "w", encoding="utf-8") as fh:
            json.dump(payload, fh)
    except OSError:
        pass


class Limiter:
    def __init__(self, per_min: int):
        self.gap = 60.0 / max(1, per_min)
        self.last = 0.0

    def wait(self) -> None:
        delta = time.monotonic() - self.last
        if delta < self.gap:
            time.sleep(self.gap - delta)
        self.last = time.monotonic()


SEARCH_LIMIT = Limiter(25)


def gh(url: str, use_cache: bool = True, limiter: Limiter | None = None):
    if use_cache:
        hit = _cache_get(url)
        if hit is not None:
            return hit, {"ok": True, "ms": 0, "cached": True}
    if limiter:
        limiter.wait()
    t0 = time.monotonic()
    req = urllib.request.Request(url, headers=_headers())
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            data = json.load(resp)
        if use_cache:
            _cache_put(url, data)
        return data, {"ok": True, "ms": int((time.monotonic() - t0) * 1000), "cached": False}
    except urllib.error.HTTPError as exc:
        return None, {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                      "status": exc.code, "error": exc.read().decode("utf-8", "ignore")[:300]}
    except Exception as exc:  # noqa: BLE001
        return None, {"ok": False, "ms": int((time.monotonic() - t0) * 1000),
                      "error": f"{type(exc).__name__}: {exc}"}


# ---------- 查询扩展 ----------

FALLBACK_CN = {
    "端口转发": "port forwarding", "终端": "terminal", "会话": "session", "技能": "skill",
    "检索": "search", "扫描": "scan", "缓存": "cache", "转发": "forward", "网关": "gateway",
    "记忆": "memory", "索引": "index", "调用": "invoke", "记录": "record", "分析": "analyze",
}


def expand_keywords(module: str, given: list[str], use_llm: bool) -> tuple[list[str], str]:
    if given:
        return given, "given"
    if use_llm:
        out = _llm_expand(module)
        if out:
            return out, "llm"
    words = [v for k, v in FALLBACK_CN.items() if k in module]
    return (words or [module]), "fallback"


PAID_MODEL = os.environ.get("MANAGER_LLM_MODEL", "alibaba/qwen3.8-flash")
LAST_LLM_ERR = ""


def _llm_call(prompt: str, max_tokens: int = 2600, timeout: int = 60,
              model: str | None = None) -> str:
    base = os.environ.get("MANAGER_LLM_BASE_URL", "http://127.0.0.1:3456").rstrip("/")
    key = os.environ.get("MANAGER_LLM_API_KEY", "")
    model = model or os.environ.get("MEMORY_LLM_MODEL", "openrouter/openrouter/free")
    if not key:
        return ""
    body = json.dumps({"model": model, "max_tokens": max_tokens,
                       "messages": [{"role": "user", "content": prompt}]}).encode()
    req = urllib.request.Request(
        f"{base}/v1/messages", data=body,
        headers={"Content-Type": "application/json", "anthropic-version": "2023-06-01",
                 "Authorization": f"Bearer {key}"})
    global LAST_LLM_ERR
    try:
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            data = json.load(resp)
        text = "".join(b.get("text", "") for b in data.get("content", []))
        if not text:
            LAST_LLM_ERR = "only-thinking"
        return text
    except urllib.error.HTTPError as exc:
        LAST_LLM_ERR = f"HTTP{exc.code}"
        return ""
    except Exception as exc:  # noqa: BLE001
        LAST_LLM_ERR = type(exc).__name__
        return ""


def _json_block(text: str) -> dict | None:
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except ValueError:
        return None


def _llm_expand(module: str) -> list[str]:
    text = _llm_call("把下面的中文功能模块描述转成 GitHub 检索关键词。输出严格 JSON："
                     "{\"keywords\":[\"英文短语\",...]}，3~5 个，只含技术名词，不要解释。\n"
                     f"模块：{module}", max_tokens=1500, timeout=30)
    data = _json_block(text) or {}
    return [str(k).strip() for k in data.get("keywords", []) if str(k).strip()][:5]


def tokens_of(kws: list[str]) -> list[str]:
    out = []
    for k in kws:
        for t in re.split(r"[^a-z0-9+#.\u4e00-\u9fff]+", k.lower()):
            if len(t) > 2 and t not in STOP:
                out.append(t)
    seen, uniq = set(), []
    for t in out:
        if t not in seen:
            seen.add(t)
            uniq.append(t)
    return uniq


# ---------- 检索臂 ----------

def repo_search(q: str, sort: str, per_page: int) -> tuple[list[dict], dict]:
    url = (f"{GH_API}/search/repositories?q={urllib.parse.quote(q)}"
           f"&sort={sort}&order=desc&per_page={per_page}")
    data, meta = gh(url, limiter=SEARCH_LIMIT)
    items = (data or {}).get("items", []) if data else []
    meta.update({"count": len(items), "total": (data or {}).get("total_count")})
    return items, meta


def code_search(q: str, per_page: int) -> tuple[list[dict], dict]:
    url = f"{GH_API}/search/code?q={urllib.parse.quote(q)}&per_page={per_page}"
    data, meta = gh(url, limiter=Limiter(8))
    items = (data or {}).get("items", []) if data else []
    meta.update({"count": len(items)})
    return items, meta


def build_arms(kws: list[str], module: str, min_stars: int, fresh_days: int):
    cut = (datetime.now(timezone.utc) - timedelta(days=fresh_days)).strftime("%Y-%m-%d")
    base = f" fork:false archived:false stars:>={min_stars}"
    toks = tokens_of(kws)
    arms = [("core", f"{' '.join(toks[:4])} in:name,description,topics" + base, "stars")]
    if len(toks) >= 2:
        arms.append(("pair", f"{toks[0]} {toks[1]} in:name,description,topics" + base, "stars"))
    if len(toks) >= 3:
        arms.append(("pair2", f"{toks[0]} {toks[2]} in:name,description,topics" + base, "stars"))
    arms.append(("fresh", f"{' '.join(toks[:4])} in:name,description,topics" + base
                 + f" pushed:>{cut}", "updated"))
    if re.search(r"[\u4e00-\u9fff]", module):
        arms.append(("cn", f"{module} in:name,description" + base, "stars"))
    arms.append(("reading", f"{toks[0]} {toks[-1]} in:readme" + base, "stars"))
    return arms


# ---------- 分桶与打分 ----------

def classify(item: dict) -> str:
    blob = f"{item.get('name','')} {item.get('description') or ''}"
    if LIST_RE.search(blob):
        return "list"
    if DOC_RE.search(item.get("name", "")):
        return "doc"
    return "impl"


def _name_blob(item: dict) -> str:
    return " ".join([item.get("name", ""), item.get("description") or "",
                     " ".join(item.get("topics") or [])]).lower()


def _doc_freq(pool: dict, toks: list[str]) -> dict:
    n = max(1, len(pool))
    df = {t: sum(1 for r in pool.values() if t in _name_blob(r["item"])) for t in toks}
    return {t: 1.0 + math.log((n + 1) / (df[t] + 1)) for t in toks}


def _weighted_cov(blob: str, toks: list[str], w: dict) -> float:
    total = sum(w[t] for t in toks)
    return sum(w[t] for t in toks if t in blob) / total if total else 0.0


def _days_since(ts: str | None) -> int:
    if not ts:
        return 9999
    try:
        return (datetime.now(timezone.utc) -
                datetime.fromisoformat(ts.replace("Z", "+00:00"))).days
    except ValueError:
        return 9999


def enrich(full: str, branch: str) -> dict:
    def readme():
        data, _ = gh(f"{GH_API}/repos/{full}/readme")
        if not data or not data.get("content"):
            return ""
        try:
            return base64.b64decode(data["content"]).decode("utf-8", "ignore")[:60000]
        except Exception:  # noqa: BLE001
            return ""

    def tree():
        data, _ = gh(f"{GH_API}/repos/{full}/git/trees/{branch}?recursive=1")
        return [n.get("path", "") for n in (data or {}).get("tree", [])
                if n.get("type") in ("blob", "tree")]

    def head():
        data, _ = gh(f"{GH_API}/repos/{full}/git/ref/heads/{branch}")
        return ((data or {}).get("object") or {}).get("sha", "")[:12]

    with futures.ThreadPoolExecutor(max_workers=3) as ex:
        r, t, h = ex.submit(readme), ex.submit(tree), ex.submit(head)
        return {"readme": r.result(), "paths": t.result(), "head": h.result()}


def llm_rerank(module: str, cands: list[dict], allow_paid: bool = False) -> tuple[dict, str, str]:
    """语义重排：只判“与本模块的相关性”，不用星数当证据。返回 (结果, 失败原因)。"""
    if not cands:
        return {}, "no-candidates", ""
    lines = []
    for c in cands:
        files = ", ".join(p for p in c.get("sample_paths", [])[:5])
        lines.append(f"- {c['repo']} | ★{c['stars']} | {c['desc'][:100]} | 文件: {files}")
    prompt = (
        "你是技术选型调研员。判断候选 GitHub 仓库对目标模块的可借鉴价值。\n"
        "不要因为星多或名字像就给高分。三维度 0~1：func=是否解决同一问题；"
        "impl=实现路径是否可参考；design=架构决策是否有借鉴。\n"
        "只共享一个词、或只是清单/教程 ⇒ judge=skip。\n"
        "输出严格 JSON，不要 Markdown：{\"items\":[{\"repo\":\"owner/name\","
        "\"func\":0.0,\"impl\":0.0,\"design\":0.0,\"judge\":\"use|fork|read|skip\","
        "\"why\":\"不超过30字\"}]}\n\n"
        f"目标模块：{module}\n候选：\n" + "\n".join(lines)
    )
    errors = []
    models = ([PAID_MODEL] if allow_paid else []) + [None]
    for model in models:
        text = ""
        for _attempt in range(2):
            text = _llm_call(prompt, max_tokens=1800,
                             timeout=45 if model else 20, model=model)
            if text:
                break
        if not text:
            errors.append(f"{model or 'default'}:{LAST_LLM_ERR or 'empty'}")
            continue
        data = _json_block(text)
        if not data or not isinstance(data.get("items"), list):
            errors.append(f"{model or 'default'}:bad-json")
            continue
        known = {c["repo"] for c in cands}
        out = {}
        for item in data["items"]:
            repo = str(item.get("repo", ""))
            if repo not in known:
                continue
            try:
                out[repo] = {"func": min(1.0, max(0.0, float(item["func"]))),
                             "impl": min(1.0, max(0.0, float(item["impl"]))),
                             "design": min(1.0, max(0.0, float(item["design"]))),
                             "judge": str(item.get("judge", "read")),
                             "why": str(item.get("why", ""))[:80]}
            except (KeyError, TypeError, ValueError):
                continue
        if out and max(max(v["func"], v["impl"], v["design"]) for v in out.values()) >= 0.05:
            return out, "", (model or "openrouter/free")
        errors.append(f"{model or 'default'}:degenerate")
    return {}, "; ".join(errors), ""


# ---------- 主流程 ----------

def collect(module: str, kws: list[str], lang: str, min_stars: int,
            fresh_days: int, identifiers: list[str], rerank: bool,
            allow_paid: bool = False):
    arms_log, pool = [], {}
    for name, q, sort in build_arms(kws, module, min_stars, fresh_days):
        items, meta = repo_search(q, sort, per_page=10)
        arms_log.append({"name": name, "q": q, "ok": meta.get("ok", False),
                         "count": meta.get("count", 0), "ms": meta.get("ms", 0),
                         "error": meta.get("error"), "total": meta.get("total"),
                         "cached": meta.get("cached", False)})
        for it in items:
            key = it["full_name"].lower()
            rec = pool.setdefault(key, {"item": it, "seen": set()})
            rec["seen"].add(name)

    code_hits: dict[str, list[str]] = {}
    for ident in identifiers[:2]:
        items, meta = code_search(f'"{ident}" in:file', per_page=5)
        arms_log.append({"name": f"code:{ident}", "q": ident, "ok": meta.get("ok", False),
                         "count": meta.get("count", 0), "ms": meta.get("ms", 0),
                         "error": meta.get("error"), "cached": meta.get("cached", False)})
        for it in items:
            full = (it.get("repository") or {}).get("full_name", "")
            if not full:
                continue
            key = full.lower()
            code_hits.setdefault(key, []).append(it.get("path", ""))
            if key not in pool:
                data, _ = gh(f"{GH_API}/repos/{full}")
                if data and data.get("full_name"):
                    pool[key] = {"item": data, "seen": {"code"}}

    toks = tokens_of(kws)
    weights = _doc_freq(pool, toks)

    pre = []
    for key, rec in pool.items():
        item = rec["item"]
        if item.get("archived"):
            continue
        bucket = classify(item)
        blob = _name_blob(item)
        cov = _weighted_cov(blob, toks, weights)
        matched = sum(1 for t in toks if t in blob)
        phrase = any((" " in k and k.lower() in blob) for k in kws)
        hits = len(code_hits.get(key, []))
        if bucket == "impl" and cov < 0.18 and hits == 0:
            continue
        if bucket == "impl" and matched < 2 and not phrase and hits == 0:
            continue
        stars = int(item.get("stargazers_count") or 0)
        pre.append({"key": key, "item": item, "bucket": bucket, "cov": cov,
                    "hits": hits, "seen": rec["seen"],
                    "pre": 0.55 * cov + 0.20 * min(1, math.log10(stars + 1) / 4) +
                           0.25 * min(1, hits / 3)})

    pre.sort(key=lambda x: (x["bucket"] != "impl", -x["pre"]))
    for p in [x for x in pre if x["bucket"] == "impl"][:8]:
        branch = p["item"].get("default_branch") or "main"
        p["branch"] = branch
        p.update(enrich(p["item"]["full_name"], branch))

    ranked = []
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
        name_l = item.get("name", "").lower()
        name_bonus = 0.08 if sum(1 for t in toks if t in name_l) >= 2 else 0.0
        func = (0.70 * p["cov"] + 0.20 * min(1, math.log10(stars + 1) / 4)
                + 0.10 * lang_ok + name_bonus - 0.25 * min(2, neg))
        impl = 0.35 * lang_ok + 0.35 * impl_path + 0.30 * p["cov"]
        design = 0.60 * (1.0 if arch_docs else 0.0) + 0.40 * arch_signal
        lic = (item.get("license") or {}).get("spdx_id") or "UNKNOWN"
        ranked.append({
            "repo": item["full_name"], "url": item.get("html_url"),
            "desc": (item.get("description") or "")[:160], "bucket": bucket,
            "seen": sorted(p["seen"]), "func": round(func, 3),
            "impl": round(impl, 3), "design": round(design, 3),
            "coverage": round(p["cov"], 3), "stars": stars, "license": lic,
            "copyleft": bool(GPL_RE.search(lic)), "fresh_days": _days_since(item.get("pushed_at")),
            "arch_docs": arch_docs, "branch": p.get("branch") or item.get("default_branch") or "main",
            "head": p.get("head", ""), "code_files": code_hits.get(p["key"], [])[:3],
            "topics": (item.get("topics") or [])[:8],
            "sample_paths": [x for x in paths if "/" in x][:6],
            "judge": "", "why": "", "llm": None,
        })

    ranked.sort(key=lambda x: (x["bucket"] != "impl",
                               -(0.6 * x["func"] + 0.3 * x["impl"] + 0.1 * x["design"])))
    used = ""
    if rerank:
        impls = [c for c in ranked if c["bucket"] == "impl"][:8]
        got, err, used = llm_rerank(module, impls, allow_paid)
        if got:
            for c in ranked:
                r = got.get(c["repo"])
                if not r:
                    continue
                c.update({"judge": r["judge"], "why": r["why"], "llm": r})
                c["func"] = round(0.55 * c["func"] + 0.45 * r["func"], 3)
                c["impl"] = round(0.55 * c["impl"] + 0.45 * r["impl"], 3)
                c["design"] = round(0.55 * c["design"] + 0.45 * r["design"], 3)
            ranked.sort(key=lambda x: (x["bucket"] != "impl",
                                       x["judge"] == "skip",
                                       -(0.5 * x["func"] + 0.3 * x["impl"] + 0.2 * x["design"])))
        else:
            arms_log.append({"name": "llm-rerank", "q": "", "ok": False, "count": 0,
                             "ms": 0, "error": err or "empty/invalid JSON"})
    return {"arms": arms_log, "pool_size": len(pool),
            "rerank": bool(used), "rerank_model": used,
            "rerank_paid": bool(used and used != "openrouter/free")}, ranked


def render_md(module: str, kws: list[str], lang: str, meta: dict, ranked: list,
              elapsed: float, limit: int = 8) -> str:
    out = [f"# 参考情报：{module}", "",
           f"- as of：{datetime.now().astimezone().isoformat(timespec='seconds')}",
           f"- 关键词：{', '.join(kws)}　语言：{lang or '不限'}",
           f"- 候选池：{meta['pool_size']}　耗时：{elapsed}s",
           (f"- 语义重排：{meta['rerank_model']}"
            + ("（**付费档**，单次约几千 token）" if meta.get("rerank_paid") else "（免费档）")
            if meta["rerank"] else "- 语义重排：无（仅词法，判定列为空）"), "",
           "## 候选（实现桶）", "",
           "| 仓库 | ★ | 最近推送 | 许可证 | 功能 | 实现 | 设计 | 判定 |",
           "|---|---:|---|---|---:|---:|---:|---|"]
    others = [c for c in ranked if c["bucket"] != "impl"]
    impls = [c for c in ranked if c["bucket"] == "impl"]
    if meta.get("rerank"):
        picked = [c for c in impls if c["judge"] and c["judge"] != "skip"]
        low = [c for c in impls if not c["judge"] or c["judge"] == "skip"]
    else:
        picked, low = impls, []
    for c in picked[:limit]:
        lic = c["license"] + ("(copyleft)" if c["copyleft"] else "")
        out.append(f"| [{c['repo']}]({c['url']}) | {c['stars']} | {_badge(c['fresh_days'])} | "
                   f"{lic} | {c['func']:.2f} | {c['impl']:.2f} | {c['design']:.2f} | "
                   f"{c['judge'] or '—'} |")
    out += ["", "## 资料/清单桶（不参与实现对比）", ""]
    out += [f"- [{c['repo']}]({c['url']}) — {c['desc']}" for c in others] or ["- （无）"]
    if low:
        out += ["", "## 未入选（语义判定 skip / 未进重排）", ""]
        for c in low[:10]:
            why = c["why"] or ("未进入重排队列" if meta.get("rerank") else "未标注")
            out.append(f"- {c['repo']}（{c['judge'] or '—'}）：{why}")
    out += ["", "## 可借鉴点（均需人工复核）", ""]
    for c in picked[:limit]:
        out.append(f"### {c['repo']} · 功能{c['func']:.2f}/实现{c['impl']:.2f}/设计{c['design']:.2f}")
        out.append(f"- 一句话：{c['desc']}")
        if c["why"]:
            out.append(f"- 重排理由：{c['why']}（{c['judge']}）")
        out.append(f"- 设计资料：{'有 ADR/architecture 目录' if c['arch_docs'] else '未见 ADR'}")
        if c["head"]:
            out.append(f"- 固定引用：`{c['repo']}@{c['head']}`（分支 {c['branch']}）")
        for p in c["code_files"]:
            out.append(f"- 代码命中：[{p}](https://github.com/{c['repo']}/blob/{c['branch']}/{p})")
        if c["copyleft"]:
            out.append("- ⚠️ copyleft：只允许借鉴设计，**不得摘录代码**")
        elif c["license"] in ("UNKNOWN", "NOASSERTION"):
            out.append("- ⚠️ 许可证未识别：默认不得复用代码，需人工核实")
        out.append("")
    out += ["## 逐路表态", ""]
    for a in meta["arms"]:
        state = "ok" if a["ok"] else f"FAIL {a.get('error') or ''}"
        out.append(f"- {a['name']}：{a['count']} 条 / {a['ms']}ms / {state}")
    return "\n".join(out) + "\n"


def _badge(days: int) -> str:
    return "新" if days <= 90 else ("温" if days <= 365 else "旧")


def main() -> int:
    global TOKEN
    _load_dotenv()
    TOKEN = _token()
    ap = argparse.ArgumentParser()
    ap.add_argument("--module", required=True)
    ap.add_argument("--keywords", default="")
    ap.add_argument("--lang", default="")
    ap.add_argument("--min-stars", type=int, default=20)
    ap.add_argument("--top", type=int, default=8)
    ap.add_argument("--fresh-days", type=int, default=180)
    ap.add_argument("--identifiers", default="")
    ap.add_argument("--no-llm", action="store_true")
    ap.add_argument("--no-rerank", action="store_true")
    ap.add_argument("--allow-paid", action="store_true", help="免费档重排失败时允许用付费快模型")
    ap.add_argument("--out", default="")
    args = ap.parse_args()

    given = [k.strip() for k in args.keywords.split(",") if k.strip()]
    kws, src = expand_keywords(args.module, given, not args.no_llm)
    idents = [i.strip() for i in args.identifiers.split(",") if i.strip()]
    t0 = time.monotonic()
    meta, ranked = collect(args.module, kws, args.lang, args.min_stars,
                           args.fresh_days, idents, not args.no_rerank,
                           args.allow_paid)
    elapsed = round(time.monotonic() - t0, 1)
    md = render_md(args.module, kws, args.lang, meta, ranked, elapsed, args.top)
    base = args.out or os.path.join(ROOT, "work", "refscout-" +
                                    re.sub(r"[^\w\u4e00-\u9fff]+", "-", args.module)[:40])
    with open(base + ".md", "w", encoding="utf-8") as fh:
        fh.write(md)
    with open(base + ".json", "w", encoding="utf-8") as fh:
        json.dump({"module": args.module, "keywords": kws, "keyword_source": src,
                   "as_of": datetime.now().astimezone().isoformat(timespec="seconds"),
                   "elapsed_s": elapsed, "meta": meta, "candidates": ranked},
                  fh, ensure_ascii=False, indent=2)
    print(md)
    print(f"[written] {base}.md / .json")
    return 0 if all(a["ok"] for a in meta["arms"]) else 2


if __name__ == "__main__":
    sys.exit(main())
