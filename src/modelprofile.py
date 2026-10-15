"""模型能力**边界**画像 —— 从 CCR 账本实测数据推出，不含任何人工断言。

【为什么叫「边界」而不是「能力」】
勘察（2026-10-16，全量 37583 行）确认两件事：

1. **CCR `/v1/models` 不给任何能力元数据** —— 实测只返回 6 个字段
   （`id/object/created/owned_by/type/display_name`），
   **没有**上下文窗口、**没有**定价、**没有**工具调用支持。
   （此前以为 `api_models.py:85` 「丢弃了上游能力字段」是误判：它丢的只是
   `object/type/created`，能力字段上游本来就不存在。）
2. **账本没有输出正文** —— `usage_events` 只有 token/成本/时延/状态码，
   **没有** response 文本 ⇒ 它答不了「这个模型擅长什么」。

⇒ 本模块只给**边界**：一次调用能不能成、多快、多贵、在哪些协议形状下可用、稳不稳。
「擅长什么」必须等任务级数据（`tasks.model` + `tasks.verdict` + `tasks.output`，
即 P0-a/P0-b 那条线）攒够样本后另算，**不在这里编**。

【本模块最重要的一条：把「负载信号」与「质量信号」分开】
429（限流）占全量失败的 **3782 次**，是最大单一失败原因。它反映**当时有多挤**，
不反映**模型好不好**。实测污染之严重：

    模型                                  原始成功率(跨client跨度)   剔除429后(跨度)
    qwen3.7-max                            70%（60pp）          →  97%（7pp）
    poolside/laguna-xs-2.1:free            80%（100pp）         →  98%（12pp）
    nvidia/nemotron-3-super-120b-a12b:free 93%（38pp）          →  99%（3pp）

⇒ 混算的 `ok_rate` 会把「当时被限流」记成「这模型不行」，
而同一模型换个 client 就从 19.9% 变 85.8%（minimax-m3 实测）⇒ **是记账污染，不是能力差**。

【三态，与 `verifier` 同构】
- `ok`：样本够 **且** 跨 client 稳定 ⇒ 这个数可以用
- `insufficient_sample`：样本不足 ⇒ **不是「这模型差」，是「还没量够」**
- `unstable`：跨 client 跨度太大 ⇒ 率不可信，别拿它做决策
与 verifier 同一条铁律：**判不了就说判不了，绝不编一个数出来**。

分层：纯函数、stdlib-only、不碰 sqlite（读账本由上层 `usage_ledger` 负责，
它已确立 `mode=ro` 的只读纪律）。
"""
from __future__ import annotations

from typing import Any, Dict, Iterable, List, Optional, Sequence

import ledgernorm as ln

#: 判定「够不够量」的最小样本数。**来自实测分布**（剔除占位后 87 个模型）：
#: n≥100 有 19 个、n≥20 有 17 个、n<20 有 51 个
#: ⇒ 20 是「能开始说话」与「样本太薄」之间的实测分界。
MIN_SAMPLE = 20

#: 跨 client 成功率跨度上限（百分点）。**同样来自实测**：剔除 429 后
#: 87 个模型里 24 个跨度 ≤20pp、13 个仍 >20pp ⇒ 20 是「可信 / 不可信」的实测分界。
MAX_CLIENT_SPAN_PP = 20.0

#: 限流码 —— 负载信号，**从质量口径里剔除**。
RATE_LIMIT_CODES = frozenset((429,))

STATUS_OK = "ok"
STATUS_INSUFFICIENT = "insufficient_sample"
STATUS_UNSTABLE = "unstable"
STATUSES = (STATUS_OK, STATUS_INSUFFICIENT, STATUS_UNSTABLE)


def _pct(a: float, b: float) -> Optional[float]:
    return (100.0 * a / b) if b else None


def _percentile(sorted_vals: Sequence[float], q: float) -> Optional[float]:
    if not sorted_vals:
        return None
    if len(sorted_vals) == 1:
        return float(sorted_vals[0])
    idx = int(round(q * (len(sorted_vals) - 1)))
    return float(sorted_vals[idx])


def profile(rows: Iterable[Dict[str, Any]], *,
            min_sample: int = MIN_SAMPLE,
            max_span_pp: float = MAX_CLIENT_SPAN_PP) -> Dict[str, Any]:
    """从一个模型的一批账本行算出边界画像。

    每行需含：`model` / `status_code` / `duration_ms` / `input_tokens` /
    `output_tokens` / `cost_usd` / `provider` / `client`。
    返回 dict；`status` ∈ `STATUSES`，`reasons` 说明为什么不是 `ok`。

    >>> profile([])['status']
    'insufficient_sample'
    """
    calls = 0
    ok = 0
    rate_limited = 0
    failed_other = 0
    durations: List[float] = []
    tok_in = tok_out = 0
    cost = 0.0
    null_cost = 0
    shapes = set()
    per_client: Dict[str, List[int]] = {}

    for r in rows:
        mid = ln.canonical_model(r.get("model"))
        if not ln.is_model(mid)[0]:
            continue                      # 占位/路由器不构成模型画像
        calls += 1
        sc = r.get("status_code")
        ok_here = bool(sc) and 200 <= sc < 300
        if ok_here:
            ok += 1
        elif sc in RATE_LIMIT_CODES:
            rate_limited += 1
        else:
            failed_other += 1

        d = r.get("duration_ms")
        if d:
            durations.append(float(d))
        tok_in += int(r.get("input_tokens") or 0)
        tok_out += int(r.get("output_tokens") or 0)
        c = r.get("cost_usd")
        if c is None:
            null_cost += 1                # 免费模型落库 NULL 是事实，静默当 0 就是谎报
        else:
            cost += float(c)
        _, shape = ln.canonical_provider(r.get("provider"))
        shapes.add(shape)

        cl = r.get("client") or "?"
        per_client.setdefault(cl, [0, 0])
        per_client[cl][1] += 1
        if ok_here:
            per_client[cl][0] += 1

    durations.sort()
    tok_total = tok_in + tok_out
    # 跨 client 跨度：只用「剔除限流后」的每 client 成功率
    rates = []
    for _cl, (o, n) in sorted(per_client.items()):
        if n:
            rates.append(100.0 * o / n)
    span = (max(rates) - min(rates)) if len(rates) >= 2 else None

    reasons: List[str] = []
    if calls < min_sample:
        reasons.append("样本 %d < 阈值 %d" % (calls, min_sample))
    if span is not None and span > max_span_pp:
        reasons.append("跨 client 跨度 %.0fpp > 阈值 %.0fpp" % (span, max_span_pp))
    if span is None and calls >= min_sample:
        reasons.append("只在一个 client 下被调用过，无法交叉验证")

    status = STATUS_OK
    if calls < min_sample:
        status = STATUS_INSUFFICIENT
    elif span is None or span > max_span_pp:
        status = STATUS_UNSTABLE

    ok_excl = ok
    denom_excl = calls - rate_limited

    # 单价：**全为 NULL ⇒ 单价未知（None），不是 0**。
    # 免费模型落库 cost_usd=NULL 是实测事实；返回 0.0 会被下游读成「这台模型免费」，
    # 而真相是「我们没量到成本」—— 这就是 usage_ledger docstring 说的「静默当 0 = 谎报」。
    #
    # ⚠ **口径提醒**：`cost_per_1k_tok` = 成本总额 / **全部** token。
    # 当一个模型混着免费档与付费档时，它是「这段时期的**混合平均单价**」，
    # **不是**「付费时的单价」。后者要拿成本除以*有成本那部分*的 token。
    # 两个数含义不同，下游别混用 —— 混用会把「一半免费」读成「这模型很便宜」。
    # 需要区分时看 `calls_null_cost / calls` 的免费占比。
    if tok_total and calls and null_cost < calls:
        price = cost / tok_total * 1000
    else:
        price = None

    return {
        "calls": calls,
        "ok": ok,
        "rate_limited": rate_limited,
        "failed_other": failed_other,
        # 两个口径都留：ok_rate 是「含限流的体感」，ok_rate_excl_rl 是「质量」
        "ok_rate": _pct(ok, calls),
        "ok_rate_excl_rl": _pct(ok_excl, denom_excl),
        "latency_p50_ms": _percentile(durations, 0.50),
        "latency_p95_ms": _percentile(durations, 0.95),
        "tokens_in": tok_in,
        "tokens_out": tok_out,
        "tokens_total": tok_total,
        "cost_usd": round(cost, 6),
        "cost_per_1k_tok": round(price, 6) if price is not None else None,
        "calls_null_cost": null_cost,
        "clients": len(per_client),
        "client_span_pp": round(span, 1) if span is not None else None,
        "api_shapes": sorted(s for s in shapes if s),
        "status": status,
        "reasons": reasons,
    }
