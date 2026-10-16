# -*- coding: utf-8 -*-
"""Context Manifest（P1-1，手册 A7 / C9）—— 一次上下文装配的**可回放清单**。

【为什么要它】每次开局/检索都在「往 prompt 里塞东西」，但**塞了什么、什么版本、
为什么没塞某一路**，此前只存在于当次回包（前端一丢就没了），事后无法复盘。
A7 要求记「来源 id / 作用域 / 版本 / 可信度 / 选择理由 / token / 哈希 / 排除项」，
C9 补一句它要「落可查询记录用于回放归因」。

【本机口径（写死在这里，免得各装配点自由发挥）】

1. **只记「确实进了 prompt 的东西」**。候选全集**不进** manifest —— 否则它会变成
   第二个数据源，撞本机「不建第二权威副本」的原则。
2. **排除项必须显式列出并带理由**。归因的核心问题不是「有什么」，而是「**为什么没有**」；
   只列入选项等于把「静默不注入」洗成正常。
3. 时间取**源自己的** `as_of`（沿用 P1-8 口径：快照时间 ≠ 文件现状）；
   未知就写 `None`，**不编**。
4. `sha256` **只对注入文本**算，**不含元数据** —— 否则改个 label 哈希就变，
   失去「这份上下文与上次是不是同一份」的判别力。
5. `manifest_hash` 同样**不含耗时 / 时间戳** —— 同一份上下文必须产出同一个哈希，
   否则「复用率 / 内容漂移」全部不可测。

本模块是**纯函数**（stdlib only）：不起服务、不读盘、不连网，故 L0 可整层测。
"""

from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

SCHEMA = "ctx-manifest/1"


def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def entry(label: str, text: str, source: str, *, scope: str = "global",
          ids: Optional[List[str]] = None, as_of: Optional[str] = None,
          confidence: Optional[float] = None, note: Optional[str] = None) -> Dict[str, Any]:
    """造一个 manifest **条目**（同时它就是注入文本的载体，供 `build_manifest` 用）。

    `text` 是**逐字节要进 prompt 的那段** —— 哈希算的就是它，不是摘要、不是原文。
    """
    return {"label": label, "text": text or "", "source": source, "scope": scope,
            "ids": list(ids or []), "as_of": as_of, "confidence": confidence, "note": note}


def excluded(source: str, reason: str, **extra: Any) -> Dict[str, Any]:
    """造一条**排除项**。`reason` 必填 —— 没有理由的排除项不可归因。"""
    rec = {"source": source, "reason": reason}
    rec.update({k: v for k, v in extra.items() if v is not None})
    return rec


def build_manifest(parts: List[Dict[str, Any]], *, budget_chars: int = 0,
                   truncated: bool = False,
                   excluded_items: Optional[List[Dict[str, Any]]] = None) -> Dict[str, Any]:
    """把条目列表编成 manifest（含 `manifest_hash`）。纯函数，输入同 ⇒ 输出逐字节同。"""
    entries = []
    for p in parts:
        t = p.get("text") or ""
        entries.append({
            "label": p.get("label"),
            "source": p.get("source"),
            "scope": p.get("scope") or "global",
            "ids": p.get("ids") or [],
            "as_of": p.get("as_of"),
            "confidence": p.get("confidence"),
            "note": p.get("note"),
            "chars": len(t),
            "sha256": _sha256(t),
        })
    excl = list(excluded_items or [])
    if truncated:
        excl.append(excluded("(tail)", "超出 budget_chars=%d，尾部被截" % budget_chars))
    manifest: Dict[str, Any] = {
        "schema": SCHEMA,
        "parts": entries,
        "chars": sum(e["chars"] for e in entries),
        "budget_chars": budget_chars,
        "truncated": bool(truncated),
        "excluded": excl,
    }
    manifest["manifest_hash"] = _digest(manifest)
    return manifest


def _digest(manifest: Dict[str, Any]) -> str:
    """manifest 的内容指纹：**只吃** schema + 每段的 (source, chars, sha256) + 排除项理由。

    刻意不吃 label / as_of / 耗时 —— 那些是「怎么呈现」，不是「给了什么」。改个标签
    不该让「同一份上下文」看起来变了；反过来，任何一段正文变了、或某一路从入选
    变成被排除，哈希必须变。
    """
    payload = {
        "schema": manifest.get("schema"),
        "parts": [{"source": e.get("source"), "chars": e.get("chars"),
                   "sha256": e.get("sha256")} for e in manifest.get("parts") or []],
        "excluded": sorted(str(e.get("source")) + "|" + str(e.get("reason"))
                           for e in manifest.get("excluded") or []),
    }
    return _sha256(json.dumps(payload, sort_keys=True, ensure_ascii=False))


def summarize(manifest: Dict[str, Any]) -> Dict[str, Any]:
    """落库用的**紧凑摘要**（完整 manifest 已随回包给前端；账本只留可聚合的几个量）。

    ⚠ 摘要必须能独立回答「这份上下文的指纹是什么、包含哪几路、排除了谁」——
    只存 `len(parts)` 那种计数会让「哈希相同」无法与「内容相同」对上号。
    """
    return {
        "schema": manifest.get("schema"),
        "manifest_hash": manifest.get("manifest_hash"),
        "parts": len(manifest.get("parts") or []),
        "chars": manifest.get("chars"),
        "truncated": manifest.get("truncated"),
        "sources": [e.get("source") for e in manifest.get("parts") or []],
        "excluded": [{"source": e.get("source"), "reason": e.get("reason")}
                     for e in manifest.get("excluded") or []],
    }