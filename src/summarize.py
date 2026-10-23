# -*- coding: utf-8 -*-
"""本地摘要提炼（2026-10-23，用户令「添加摘要提炼模块」）。

【它是什么】
把长文交给本机 Ollama（`qwen2.5:1.5b`，CPU、零外网、零 key），产出 N 条要点。
实测（2026-10-23）：700 字 11.4s / 3 条，2200 字 28.0s / 4 条，事实基本准确、
无幻觉；小于 2k 字一次能吃完，更长必须分块。

【为什么要分块 + 归并】
模型 `num_ctx=4096`，中文约 2k 字就接近上限。直接截断会静默丢后半篇——
那是「摘要看起来完整、其实只读了开头」的假绿。本模块按段落切到
`SUMMARIZE_CHUNK_CHARS`（默认 1600 字），逐块提炼，再把要点归并一次。

【缓存】
键 = 正文指纹 + 条数 + 风格 + 模型；落 `judge_cache`（7 天，原子写）。
同一篇文章重复摘要是纯浪费——CPU 推理一次 10~30s。

【不做摘要以外的事】
不改写正文、不做事实核查、不联网。模型答不出时**抛异常**（`SummarizeUnavailable`），
由端点回 503；绝不返回空列表冒充「提炼完成」。
"""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Dict, List

import judge_cache

MODEL = os.getenv("SUMMARIZE_MODEL", "qwen2.5:1.5b")
OLLAMA_URL = os.getenv("OLLAMA_URL", "http://127.0.0.1:11434")
#: 2026-10-23 用户裁定：本地摘要质量不达标（关键事实覆盖率 3/10，且模型会改写数字），
#: 已随 Ollama 一并下线。默认 0 ⇒ summarize() 抛 SummarizeUnavailable（端点回 503），
#: 调用方必须按失败处理，不会拿到空要点。复活须恢复 Ollama 并设 SUMMARIZE_ENABLED=1。
ENABLED = os.getenv("SUMMARIZE_ENABLED", "0").strip().lower() in ("1", "true", "on", "yes")
CHUNK_CHARS = int(os.getenv("SUMMARIZE_CHUNK_CHARS", "1600"))
MAX_INPUT = int(os.getenv("SUMMARIZE_MAX_CHARS", "20000"))
TIMEOUT_S = float(os.getenv("SUMMARIZE_TIMEOUT", "300"))
CACHE_TTL_S = float(os.getenv("SUMMARIZE_CACHE_TTL_S", str(7 * 86400)))
MAX_POINTS = 10


class SummarizeUnavailable(RuntimeError):
    """这次没提炼成（Ollama 不在 / 超时 / 输出不可用）。调用方必须按失败处理。"""


def _canonical(text: str) -> str:
    t = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    t = re.sub(r"[ \t]+\n", "\n", t)
    return re.sub(r"\n{3,}", "\n\n", t).strip()


def _split(text: str, limit: int) -> List[str]:
    """按段落切块；单段超长时按句号硬切。**不丢内容**（拼接后与原文等长以外仅差空白）。"""
    paras = [p for p in text.split("\n\n") if p.strip()]
    chunks: List[str] = []
    buf = ""
    for p in paras:
        if len(buf) + len(p) + 2 <= limit:
            buf = (buf + "\n\n" + p) if buf else p
            continue
        if buf:
            chunks.append(buf)
        while len(p) > limit:
            cut = p.rfind("。", 0, limit)
            cut = cut + 1 if cut > limit // 2 else limit
            chunks.append(p[:cut])
            p = p[cut:]
        buf = p
    if buf:
        chunks.append(buf)
    return chunks or [text[:limit]]


def _post(prompt: str, timeout: float) -> Dict:
    body = json.dumps({"model": MODEL, "prompt": prompt, "stream": False,
                       "options": {"temperature": 0.1, "num_ctx": 4096}}).encode("utf-8")
    req = urllib.request.Request(OLLAMA_URL + "/api/generate", data=body,
                                 headers={"Content-Type": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=max(1.0, timeout)) as r:
            return json.loads(r.read().decode("utf-8"))
    except (urllib.error.URLError, TimeoutError, ValueError) as e:
        raise SummarizeUnavailable("本机 Ollama 调用失败：%s: %s"
                                   % (type(e).__name__, str(e)[:140]))


def _bullets(raw: str, n: int) -> List[str]:
    """从模型输出里取要点。模型常常多给/少给，这里做保守归一。"""
    out: List[str] = []
    for ln in (raw or "").splitlines():
        s = ln.strip()
        if not s:
            continue
        s = re.sub(r"^[-*•·]\s*", "", s)
        s = re.sub(r"^\d+[.、)]\s*", "", s)
        s = re.sub(r"^#+\s*", "", s)
        if not s or s.startswith("```"):
            continue
        if len(s) > 120:                     # 一句话写成一段 ⇒ 按句号切开取前几段
            out.extend([x.strip() for x in re.split(r"(?<=[。；;])", s) if x.strip()])
        else:
            out.append(s)
    # 去重保序
    seen, uniq = set(), []
    for s in out:
        k = s.rstrip("。；;")
        if k and k not in seen:
            seen.add(k)
            uniq.append(s)
    return uniq[:n]


def _chunk_summary(text: str, n: int, style: str, timeout: float) -> List[str]:
    style_line = ("侧重：%s。\n" % style) if style else ""
    prompt = ("你是技术文档摘要器。只依据给定文本，不补充外部知识，不评价。\n"
              f"请用 {n} 条要点总结下面的内容，每条不超过 40 字，用「- 」开头，不要前言。\n"
              + style_line +
              "\n文本：\n" + text)
    d = _post(prompt, timeout)
    return _bullets(d.get("response") or "", n)


def _merge(parts: List[str], n: int, timeout: float) -> List[str]:
    prompt = ("你是技术文档摘要器。下面是同一篇文章各段落的要点，请合并成最终 "
              f"{n} 条要点：去重、按重要性排序，每条不超过 40 字，用「- 」开头，"
              "不要补充原文没有的信息。\n\n分段要点：\n" +
              "\n".join("- " + p for p in parts))
    d = _post(prompt, timeout)
    merged = _bullets(d.get("response") or "", n)
    return merged or parts[:n]


def summarize(text: str, points: int = 3, style: str = "",
              timeout: float = TIMEOUT_S, use_cache: bool = True) -> Dict:
    """提炼要点。返回 `{"points": [...], "text", "chunks", "model", "elapsed_s", "cache"}`。"""
    t = _canonical(text)
    if not t:
        raise ValueError("正文为空")
    if not ENABLED:
        raise SummarizeUnavailable(
            "本地摘要已下线（2026-10-23）：本机 Ollama 与模型文件已删除。"
            "要复活须恢复 Ollama 并设 SUMMARIZE_ENABLED=1")
    if len(t) > MAX_INPUT:
        raise ValueError("正文过长：%d 字，上限 %d 字；请先分段" % (len(t), MAX_INPUT))
    n = max(1, min(MAX_POINTS, int(points or 3)))

    ck = None
    if use_cache and CACHE_TTL_S > 0:
        fp = hashlib.sha256(t.encode("utf-8", "surrogateescape")).hexdigest()[:32]
        ck = judge_cache.key("summarize:v1", "%s:%d:%s" % (fp, n, style or ""), "", MODEL)
        hit = judge_cache.get(ck)
        if hit:
            out = {k: v for k, v in hit.items() if not k.startswith("_")}
            out["cache"] = {"hit": True, "hits": int(hit.get("_cache_hits") or 0),
                            "cached_at": hit.get("_cached_at")}
            return out

    t0 = time.monotonic()
    chunks = _split(t, max(200, CHUNK_CHARS))
    if len(chunks) == 1:
        pts = _chunk_summary(chunks[0], n, style, timeout)
    else:
        per = max(2, min(4, n))
        parts: List[str] = []
        for c in chunks:
            parts.extend(_chunk_summary(c, per, style, timeout))
        pts = _merge(parts, n, timeout) if parts else []
    if not pts:
        raise SummarizeUnavailable("模型没有给出可用要点（输出为空或不可解析）")

    out = {"points": pts, "text": "\n".join("- " + p for p in pts),
           "chunks": len(chunks), "chars": len(t), "model": MODEL,
           "elapsed_s": round(time.monotonic() - t0, 1),
           "cache": {"hit": False}}
    if ck:
        judge_cache.put(ck, out, CACHE_TTL_S)
    return out


def status() -> Dict:
    cs = judge_cache.stats()
    return {"backend": "ollama-local", "enabled": ENABLED, "model": MODEL,
            "chunk_chars": CHUNK_CHARS,
            "max_input": MAX_INPUT, "ttl_s": CACHE_TTL_S,
            "cache": {"entries": cs.get("entries"), "hits": cs.get("hits"),
                      "misses": cs.get("misses"), "path": cs.get("path")}}
