# -*- coding: utf-8 -*-
"""判定结果持久化缓存（2026-10-22，用户要求「同样问题无需再次消耗 token」）。

## 现状（改造前，实测）

| 位置 | 缓存 | 落盘 | 命中后还花钱吗 |
|---|---|---|---|
| `jev_client.score_candidates`（**外部端点已下线，2026-10-23 删除**） | 内存 `_S["cache"]`，TTL 600s，键=query+候选名 | **否**（重启即丢） | 否 |
| `vitals` 的 `by_sha` | 按**证据 sha** 复用，TTL 无（靠证据变化失效） | **是**（`data/vitals.json`，实测 270 条 / 107KB） | 否 |
| `skill_rerank`（免费模型重排） | **完全没有** | — | **是**（同一问题每次都重算） |

所以「同样问题无需再次消耗」在免费模型重排这条链上**当时并不成立**：免费池 p50 3.2~3.7s、
成功率只有 34~44%，同一个问题重问一次既慢又可能失败。jev 侧虽有缓存，但**只在进程内**，
重启即归零，且它是按「问题+候选名」——这正是能复用的正确粒度，只是没落盘。

## 本模块做什么

一个**进程内 + 磁盘**两级缓存，键 = `(kind, model, query, 候选集指纹)`：

- 键里带**候选集指纹**（名字+描述摘要），技能增删/改描述后自动失配 ⇒ **不会把旧判定
  套到新候选上**（这是缓存最容易出的错：键太粗，世界变了还返回旧答案）。
- 只缓存**有结论**的结果：`ok=true`（选定某条）与「明确判为都不适合」。
  **不缓存失败**（超时/熔断/模型返回审核标签）——缓存一个瞬时故障，等于把故障固化成事实。
- 落盘用原子写（tmp + os.replace），LRU 上限默认 500 条；损坏的文件**当空缓存**而不是崩。
- 每次命中/未命中都计数，`stats()` 给 `/health` —— 「省了多少次调用」必须可观测，
  否则「有缓存」和「缓存从没命中」在外部看起来一样。

## 刻意不做的事

- **不缓存后端不可用**（外部 jev 的 451 / 本地判定的脚本缺失与超时同理）：
  那是可用性问题，缓存它会把「现在不能问」变成「永远不该问」。
- **不给不确定结果加长 TTL**：TTL 由调用方给（重排 7 天；jev 时代的 24h 随端点
  下线作废），本模块不猜。
"""
from __future__ import annotations

import hashlib
import json
import os
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

#: 默认落盘位置：跟随 `DATA_DIR`（影子实例/测试隔离时缓存不落生产根），
#: 未覆盖则与 `data/vitals.json` 同目录（同一套运维认知，不新增数据根）。
#: 可用 `JUDGE_CACHE_PATH` 覆盖（L0 测试与影子实例靠它隔离）。
def _default_path() -> Path:
    root = os.getenv("DATA_DIR")
    if root:
        return Path(root) / "judge_cache.json"
    return Path(__file__).resolve().parent.parent / "data" / "judge_cache.json"


DEFAULT_PATH = _default_path()

#: 条目上限（LRU 淘汰最旧的）。判定条目很小（<1KB），500 条 ≈ 0.5MB。
MAX_ENTRIES = int(os.getenv("JUDGE_CACHE_MAX", "500"))

_LOCK = threading.Lock()
_STAT: Dict[str, int] = {"hits": 0, "misses": 0, "stores": 0, "evicted": 0, "corrupt": 0}
_MEM: Dict[str, Dict[str, Any]] = {}
#: `at`=上次加载时间，`path`=上次加载的路径。
#: 为什么要记 path：测试/L0 用 `JUDGE_CACHE_PATH` 逐用例隔离，而 `_MEM` 是模块级全局
#: ⇒ 换了路径却不清内存，就会把上一份缓存的条目带进新实例（实测导致 2 个用例假红）。
_LOADED = {"at": 0.0, "path": ""}


def path() -> Path:
    return Path(os.getenv("JUDGE_CACHE_PATH") or DEFAULT_PATH)


def fingerprint(items, fields: Tuple[str, ...] = ("name", "description", "triggers")) -> str:
    """候选集指纹：对每个候选的**身份字段**取摘要。

    为什么要描述/触发词也进指纹：改一条描述就可能改变「哪条更合适」，
    只拿名字做键会让旧判定继续生效（「世界变了还返回旧答案」）。
    顺序也要固定（排序后拼），否则候选排列不同会被当成新键、白白失效。
    """
    rows = []
    for it in items or []:
        if isinstance(it, dict):
            rows.append([str(it.get(f) or "") for f in fields])
        else:
            rows.append([str(it)])
    rows.sort()
    blob = json.dumps(rows, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8", "surrogateescape")).hexdigest()[:32]


def key(kind: str, query: str, cand_fp: str, model: str = "") -> str:
    """缓存键。**四要素缺一不可**：类型、问题、候选集指纹、判官身份。"""
    blob = json.dumps({"kind": kind, "q": (query or "").strip(),
                       "cand": cand_fp, "model": model or ""},
                      ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(blob.encode("utf-8", "surrogateescape")).hexdigest()[:40]


def _load() -> None:
    p = path()
    if _LOADED["path"] and _LOADED["path"] != str(p):
        with _LOCK:
            _MEM.clear()
        _LOADED["at"] = 0.0
    _LOADED["path"] = str(p)
    try:
        st = p.stat()
    except OSError:
        with _LOCK:
            _MEM.clear()          # 文件不在 ⇒ 这个路径就是空缓存（不是"保持上次内容"）
        _LOADED["at"] = time.time()
        return
    if _LOADED["at"] and st.st_mtime <= _LOADED["at"]:
        return
    try:
        d = json.loads(p.read_text(encoding="utf-8"))
        entries = d.get("entries") if isinstance(d, dict) else None
        if isinstance(entries, dict):
            with _LOCK:
                _MEM.clear()
                _MEM.update(entries)
    except Exception:  # noqa: BLE001 —— 文件坏了当空缓存，绝不让缓存打挂业务
        _STAT["corrupt"] += 1
    _LOADED["at"] = time.time()


def _save() -> None:
    p = path()
    try:
        p.parent.mkdir(parents=True, exist_ok=True)
        tmp = p.with_suffix(".json.tmp.%d" % os.getpid())
        tmp.write_text(json.dumps({"entries": _MEM, "saved_at": time.time()},
                                  ensure_ascii=False), encoding="utf-8")
        os.replace(tmp, p)
    except OSError:
        pass          # 落盘失败不影响本次命中（内存里仍然有）


def get(k: str) -> Optional[Dict[str, Any]]:
    """取一条；过期/不存在返回 None。

    返回值 = 当年存下的 value + `_cache_hits` / `_cached_at`（**命中元数据必须一起返回**，
    否则调用方无法在响应里说清「这是缓存还是刚算的」——那正是本仓反复禁的静默形态）。
    """
    _load()
    with _LOCK:
        e = _MEM.get(k)
        if not e:
            _STAT["misses"] += 1
            return None
        saved = float(e.get("saved_at") or 0)
        ttl = float(e.get("ttl_s") or 0)
        if ttl and (time.time() - saved) > ttl:
            _MEM.pop(k, None)
            _STAT["misses"] += 1
            return None
        e["hits"] = int(e.get("hits") or 0) + 1
        e["last_hit_at"] = time.time()
        _STAT["hits"] += 1
        out = dict(e.get("value") or {})
        out["_cache_hits"] = int(e["hits"])
        out["_cached_at"] = float(e.get("saved_at") or 0)
        return out


def put(k: str, value: Dict[str, Any], ttl_s: float) -> None:
    """写一条。超过 `MAX_ENTRIES` 时按 `saved_at` 淘汰最旧的（LRU 近似）。"""
    _load()
    with _LOCK:
        _MEM[k] = {"value": value, "saved_at": time.time(), "ttl_s": float(ttl_s),
                   "hits": 0}
        _STAT["stores"] += 1
        while len(_MEM) > MAX_ENTRIES:
            oldest = min(_MEM, key=lambda x: _MEM[x].get("saved_at") or 0)
            _MEM.pop(oldest, None)
            _STAT["evicted"] += 1
    _save()


def stats() -> Dict[str, Any]:
    """只读统计（给 /health）。命中率必须可观测，否则「有缓存」与「从没命中」长得一样。"""
    _load()
    with _LOCK:
        hits, misses = _STAT["hits"], _STAT["misses"]
        total = hits + misses
        sizes = []
        for e in _MEM.values():
            try:
                sizes.append(len(json.dumps(e.get("value") or {}, ensure_ascii=False)))
            except Exception:  # noqa: BLE001
                pass
        return {
            "entries": len(_MEM),
            "max_entries": MAX_ENTRIES,
            "hits": hits,
            "misses": misses,
            "stores": _STAT["stores"],
            "evicted": _STAT["evicted"],
            "corrupt_loads": _STAT["corrupt"],
            "hit_rate": round(hits / total, 4) if total else None,
            "avg_value_bytes": int(sum(sizes) / len(sizes)) if sizes else 0,
            "path": str(path()),
        }


def clear() -> None:
    """清空（测试/运维用）。**不会**被自动调用 —— 判定缓存清了只是重新花钱。"""
    with _LOCK:
        _MEM.clear()
    _save()


def reset_stats() -> None:
    for k in list(_STAT):
        _STAT[k] = 0
