# -*- coding: utf-8 -*-
"""技能候选的**免费模型重排**（2026-10-22，PT-20261022-13 / A4 替代方案）。

## 为什么需要（A4 的原计划为什么落空）

A 批计划里的 A4 是「用 jev `choice` 对 top-5 重排」。实测两条路都断了：
1. `jev` CLI 打上去是 **HTTP 451（区域封禁）**；
2. 服务进程**没有 `TYPESAFE_API_KEY`**（外部 jev 客户端当时报
   `enabled:true` 只因为它**从未真调过** —— `last_ok_ts:null`；该客户端已于
   2026-10-23 随端点下线删除）。
   教训：「enabled」不等于「可用」，可用性判据必须真打一次。

于是改用本机已有的**免费模型通道**（`llm.complete_free` → CCR `openrouter/free`，
兜底位是具体 `:free`）。这不是新依赖：DAG 拆解、L2 记忆压缩都走同一条路。

## 它能补什么（headroom 是量出来的，不是猜的）

同集 A/B 实测（2026-10-22）：BM25F 的 **recall@3 已经 85.7%~100%**，但 rank1 只有
64%~68% ⇒ 有 1/3 的查询「正确答案就在候选里、只是没排第一」。重排正是打这个缺口。

## 三条纪律（沿用本模块族的既有口径）

1. **只读 top-N 候选**，且模型输出必须**命中候选集里的某个 id** —— 否则判 `ok=false`。
   绝不接受「模型编了一个技能名」然后据此排序（那是把幻觉接进排序键）。
2. **失败一律显式**：超时/空输出/审核标签/非法 JSON ⇒ `ok=false` + `why`，
   调用方原样保留 BM25 顺序。**不静默当成「模型认为原序就对」**。
3. **不改写 BM25 结果**：重排只做「把选中的那条提到第一」，其余相对顺序不变。
   理由与 `skill.py` 里对 jev 的处理一致 —— 一次不可解释的乱序比不重排更难排查。

## 成本与延迟（必须写进报告）

免费池单次 0.3~3s，且会被上游路由到不同成员（偶尔返回审核标签或空正文）。
注入链的预算是 **500ms**（`hub-facade.ts`），所以本模块**默认关闭**，
由调用方显式 `llm_rerank=true` 打开；是否给注入链启用，等实测数据再定（见 PT-13）。
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import time
from typing import Any, Dict, List, Optional

import judge_cache
import llm

#: 单次重排的候选上限。再多既没必要（recall@3 已 85%+）也更慢。
MAX_CANDIDATES = 5
#: 默认超时（秒）。**不能用 2s**：实测免费池单次 2.5s 左右，2s 必然超时。
#: 更严重的是「超时怎么实现」——见 `choose()` 里对 `asyncio.shield` 的注释。
DEFAULT_TIMEOUT_S = 8.0
#: 判定缓存的 TTL（秒）。默认 7 天：判定结论很稳，而**候选集指纹变了会自动失配**
#: （改技能描述/增删技能 ⇒ 键变 ⇒ 重新判定），所以不需要靠短 TTL 保正确性。
#: 这条缓存是为「同样问题不重复消耗」而加的：免费池 p50 3.2~3.7s、成功率 34~44%，
#: 命中一次就把一次 3 秒且有 56~66% 概率失败的网络调用换成 0ms 必然成功。
CACHE_TTL_S = float(os.getenv("SKILL_RERANK_CACHE_TTL", str(7 * 86400)))
#: 摘要进 prompt 的截断长度：只给「这条技能是干什么的」，不给全文。
_DESC_CHARS = 120

#: 进程内统计（2026-10-22 PT-20261022-13 观察项：**重排成功率必须可观测**）。
#: 为什么要暴露：免费池成功率只有 34~44%（会把请求路由到审核模型），
#: 若不在 /health 报出来，"重排偶尔没生效"永远只能靠人工复现——那是盲区。
_STATS: Dict[str, Any] = {
    "calls": 0, "ok": 0, "timeout": 0, "breaker_open": 0, "bad_output": 0,
    "other_fail": 0, "reordered": 0, "ms_sum": 0, "ms_max": 0, "last_error": None,
    "cache_hit": 0, "cache_miss": 0,
}


def _bump(k: str, ms: int = 0, err: Optional[str] = None) -> None:
    _STATS[k] = int(_STATS.get(k) or 0) + 1
    if ms:
        _STATS["ms_sum"] = int(_STATS.get("ms_sum") or 0) + int(ms)
        _STATS["ms_max"] = max(int(_STATS.get("ms_max") or 0), int(ms))
    if err:
        _STATS["last_error"] = str(err)[:200]


def stats() -> Dict[str, Any]:
    """只读统计（给 /health）。**成功率用 `attempted` 做分母**（不含「候选不足」）。"""
    st = dict(_STATS)
    attempted = max(0, int(st.get("calls") or 0))
    st["attempted"] = attempted
    st["ok_rate"] = round((st.get("ok") or 0) / attempted, 4) if attempted else None
    # 缓存命中不计入「模型成功率」分母：命中是**没有调用模型**，混进去会把成功率虚高。
    st["cache_hit_rate"] = round((st.get("cache_hit") or 0) / attempted, 4) if attempted else None
    st["ms_avg"] = int((st.get("ms_sum") or 0) / attempted) if attempted else None
    st["default_timeout_s"] = DEFAULT_TIMEOUT_S
    st["enabled_by_default"] = False
    return st


def reset_stats() -> None:
    """清零（测试用）。生产不需要——统计是累计量，重启即自然清零。"""
    for k in _STATS:
        _STATS[k] = 0 if k != "last_error" else None


_PROMPT_SYS = (
    "你是技能路由器。给定一个任务描述和若干候选技能，选出**最适合完成该任务的那一个**。\n"
    "只输出 JSON：{\"pick\": <候选编号整数>, \"why\": \"<20字以内理由>\"}\n"
    "若没有任何候选适合，输出 {\"pick\": 0, \"why\": \"都不适合\"}。\n"
    "不要输出 markdown 代码块，不要解释，不要编造候选里没有的技能。"
)


def _build_user(query: str, candidates: List[Dict[str, Any]]) -> str:
    lines = ["任务：%s" % (query or "").strip()[:400], "候选："]
    for i, c in enumerate(candidates, 1):
        desc = str(c.get("description") or "").replace("\n", " ")[:_DESC_CHARS]
        trig = str(c.get("triggers") or "").replace("\n", " ")[:80]
        extra = ("（触发词：%s）" % trig) if trig else ""
        lines.append("%d. %s — %s%s" % (i, c.get("name") or "?", desc, extra))
    return "\n".join(lines)


def _parse(text: str, candidates: List[Dict[str, Any]]) -> Optional[Dict[str, Any]]:
    """从模型输出里取 `{pick, why}`。**pick 必须落在候选范围内**，否则 None。

    容错只做到「剥围栏 + 取首个平衡花括号」，不做模糊匹配 —— 重排的可解释性
    依赖「模型明确说了选第几个」；猜出来的选择无法复盘。
    """
    s = re.sub(r"```(?:json)?|```", "", str(text or "")).strip()
    m = re.search(r"\{.*\}", s, re.S)
    if not m:
        return None
    try:
        d = json.loads(m.group(0))
    except Exception:  # noqa: BLE001
        return None
    if not isinstance(d, dict):
        return None
    pick = d.get("pick")
    if isinstance(pick, str) and pick.strip().isdigit():
        pick = int(pick.strip())
    if not isinstance(pick, int):
        return None
    # `0` 是**合法结论**（「都不适合」），不是解析失败：
    # 首版把它当非法值 ⇒ 「都不适合」既不缓存也报成 bad_output，
    # 同一问题每次都要再花 3 秒去得到同一句「不适合」。
    if not isinstance(pick, int) or pick < 0 or pick > len(candidates):
        return None
    why = str(d.get("why") or "")[:80]
    return {"pick": pick, "why": why,
            "name": (candidates[pick - 1].get("name") if pick >= 1 else None)}


async def choose(query: str, candidates: List[Dict[str, Any]],
                 timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """在候选里选一条。**任何失败都返回 ok=false + why**，绝不抛、绝不静默用原序。

    返回：`{ok, name, pick, why, ms, model, timeout_s, error}`
    """
    t0 = time.monotonic()
    cands = [c for c in (candidates or []) if c.get("name")][:MAX_CANDIDATES]
    out: Dict[str, Any] = {"ok": False, "name": None, "pick": None, "why": "",
                           "ms": 0, "model": "", "timeout_s": timeout_s, "error": None}
    if len(cands) < 2:
        # 0/1 个候选没有重排的意义，直接如实说不适用（不是失败，也不进分母）
        out.update(error="候选不足 2 条，无需重排", ms=int((time.monotonic() - t0) * 1000))
        return out
    _bump("calls")
    ck = judge_cache.key("skill_rerank", query, judge_cache.fingerprint(cands))
    hit = judge_cache.get(ck)
    if hit is not None:
        out.update(ok=bool(hit.get("ok")), name=hit.get("name"), pick=hit.get("pick"),
                   why=hit.get("why") or "", ms=0, model=str(hit.get("model") or ""),
                   cached=True, cache_hits=int(hit.get("_cache_hits") or 0))
        _bump("cache_hit")
        return out
    _bump("cache_miss")
    # 【2026-10-22 实测踩到的坑，必须写在这里】超时**不能直接 cancel 在途调用**：
    # `asyncio.wait_for(coro, 2.0)` 会取消协程，而 llm 侧的 `resilience` 把
    # `CancelledError` 记成一次失败 ⇒ 连续 3 次就把 **整条 LLM 通道的熔断器打开 30s**
    # （实测 /health.breakers 里 `last_error: "CancelledError: "`）。
    # 后果远超「这次重排没成功」：DAG 拆解、L2 记忆压缩等**共享同一熔断键**的功能一起挂。
    # 所以用 `shield`：超时只放弃**等待**，底层调用照常跑完（它自己的失败才记账），
    # 我们这边如实回 `ok=false`，BM25 顺序保留。
    st = llm.breaker_state()
    if st.get("state") == "open":
        msg = "LLM 通道熔断中（冷却 %.1fs），本次不重排" % float(st.get("cooldown_s") or 0)
        out.update(error=msg, ms=int((time.monotonic() - t0) * 1000))
        _bump("breaker_open", out["ms"], msg)
        return out
    task = asyncio.ensure_future(
        llm.complete_free(_PROMPT_SYS, _build_user(query, cands)))

    def _consume(t: "asyncio.Future") -> None:
        # 超时后任务仍在后台跑；不消费异常会刷 "Task exception was never retrieved"
        if not t.cancelled():
            t.exception()

    task.add_done_callback(_consume)
    try:
        text, model = await asyncio.wait_for(asyncio.shield(task),
                                             timeout=max(0.05, float(timeout_s)))
    except asyncio.TimeoutError:
        out.update(error="超时 %.1fs（不取消底层调用，保留 BM25 顺序）" % timeout_s,
                   ms=int((time.monotonic() - t0) * 1000))
        _bump("timeout", out["ms"], out["error"])
        return out
    except Exception as e:  # noqa: BLE001 — 重排是增强，不许带倒检索
        out.update(error="%s: %s" % (type(e).__name__, e),
                   ms=int((time.monotonic() - t0) * 1000))
        _bump("other_fail", out["ms"], out["error"])
        return out
    out["model"] = model or ""
    parsed = _parse(text, cands)
    if not parsed:
        out.update(error="模型输出无法解析为候选编号（原文 %r）" % (text or "")[:120],
                   ms=int((time.monotonic() - t0) * 1000))
        _bump("bad_output", out["ms"], out["error"])
        return out
    if parsed["pick"] == 0:
        out.update(error="模型判定「都不适合」", ms=int((time.monotonic() - t0) * 1000))
        _bump("bad_output", out["ms"], out["error"])
        # 「都不适合」是**有结论**的判断（不是故障），同样缓存：
        # 否则同一个问题每次都要再花 3 秒去得到同一个「不适合」。
        judge_cache.put(ck, {"ok": False, "name": None, "pick": 0,
                             "why": parsed.get("why") or "", "model": out["model"],
                             "reason": "none_suitable"}, CACHE_TTL_S)
        return out
    out.update(ok=True, name=parsed["name"], pick=parsed["pick"], why=parsed["why"],
               ms=int((time.monotonic() - t0) * 1000))
    _bump("ok", out["ms"])
    # 只缓存**有结论**的结果（含「都不适合」=pick 0）；失败一律不缓存 ——
    # 缓存一次超时/审核标签，等于把这个瞬时故障固化成 7 天的「事实」。
    judge_cache.put(ck, {"ok": True, "name": parsed["name"], "pick": parsed["pick"],
                         "why": parsed["why"], "model": out["model"]}, CACHE_TTL_S)
    return out


def reorder(rows: List[Dict[str, Any]], picked: Optional[str]) -> List[Dict[str, Any]]:
    """把 `picked` 提到第一，**其余相对顺序不变**（不改写 BM25 的其余结论）。

    找不到该名字时原样返回（调用方据此知道「重排没生效」，而不是被悄悄重排）。
    """
    if not picked or not rows:
        return rows
    hit = [r for r in rows if str(r.get("name") or "") == picked]
    if not hit:
        return rows
    rest = [r for r in rows if str(r.get("name") or "") != picked]
    return hit[:1] + rest
