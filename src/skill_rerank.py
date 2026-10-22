# -*- coding: utf-8 -*-
"""技能候选的**免费模型重排**（2026-10-22，PT-20261022-13 / A4 替代方案）。

## 为什么需要（A4 的原计划为什么落空）

A 批计划里的 A4 是「用 jev `choice` 对 top-5 重排」。实测两条路都断了：
1. `jev` CLI 打上去是 **HTTP 451（区域封禁）**；
2. 服务进程**没有 `TYPESAFE_API_KEY`**（`jev_client.status()` 当时报
   `enabled:true` 只因为它**从未真调过** —— `last_ok_ts:null`）。
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
import re
import time
from typing import Any, Dict, List, Optional

import llm

#: 单次重排的候选上限。再多既没必要（recall@3 已 85%+）也更慢。
MAX_CANDIDATES = 5
#: 默认超时（秒）。**不能用 2s**：实测免费池单次 2.5s 左右，2s 必然超时。
#: 更严重的是「超时怎么实现」——见 `choose()` 里对 `asyncio.shield` 的注释。
DEFAULT_TIMEOUT_S = 8.0
#: 摘要进 prompt 的截断长度：只给「这条技能是干什么的」，不给全文。
_DESC_CHARS = 120

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
    if pick < 1 or pick > len(candidates):
        return None
    why = str(d.get("why") or "")[:80]
    return {"pick": pick, "why": why, "name": candidates[pick - 1].get("name")}


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
        # 0/1 个候选没有重排的意义，直接如实说不适用（不是失败）
        out.update(error="候选不足 2 条，无需重排", ms=int((time.monotonic() - t0) * 1000))
        return out
    # 【2026-10-22 实测踩到的坑，必须写在这里】超时**不能直接 cancel 在途调用**：
    # `asyncio.wait_for(coro, 2.0)` 会取消协程，而 llm 侧的 `resilience` 把
    # `CancelledError` 记成一次失败 ⇒ 连续 3 次就把 **整条 LLM 通道的熔断器打开 30s**
    # （实测 /health.breakers 里 `last_error: "CancelledError: "`）。
    # 后果远超「这次重排没成功」：DAG 拆解、L2 记忆压缩等**共享同一熔断键**的功能一起挂。
    # 所以用 `shield`：超时只放弃**等待**，底层调用照常跑完（它自己的失败才记账），
    # 我们这边如实回 `ok=false`，BM25 顺序保留。
    st = llm.breaker_state()
    if st.get("state") == "open":
        out.update(error="LLM 通道熔断中（冷却 %.1fs），本次不重排"
                         % float(st.get("cooldown_s") or 0),
                   ms=int((time.monotonic() - t0) * 1000))
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
        return out
    except Exception as e:  # noqa: BLE001 — 重排是增强，不许带倒检索
        out.update(error="%s: %s" % (type(e).__name__, e),
                   ms=int((time.monotonic() - t0) * 1000))
        return out
    out["model"] = model or ""
    parsed = _parse(text, cands)
    if not parsed:
        out.update(error="模型输出无法解析为候选编号（原文 %r）" % (text or "")[:120],
                   ms=int((time.monotonic() - t0) * 1000))
        return out
    if parsed["pick"] == 0:
        out.update(error="模型判定「都不适合」", ms=int((time.monotonic() - t0) * 1000))
        return out
    out.update(ok=True, name=parsed["name"], pick=parsed["pick"], why=parsed["why"],
               ms=int((time.monotonic() - t0) * 1000))
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
