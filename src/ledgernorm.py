"""CCR 账本字段归一化 —— P0-c 建模型画像前的口径定义。

【为什么需要】
CCR 账本（`~/.claude-code-router/app-data/usage.sqlite`）是**只读的生命线资产**，
不能改。于是「按模型聚合能力画像」这件事只能靠**读取侧归一化**。
但实测（2026-10-16，37583 行全量）发现三处口径缺失，直接 GROUP BY 会读出假结论：

1. **模型 ID 带网关前缀**。同一模型在不同路由层下写法不同，直接聚合会拆成多行：
   - `nvidia/nemotron-3-ultra-550b-a55b:free`（1446）
   - `openrouter/nvidia/nemotron-3-ultra-550b-a55b:free`（3）
   - `open_router/nvidia/…`（4）／`openrouter-free/nvidia/…`（1）
   - `anthropic/openrouter/nvidia/…`（1）　← 双层嵌套
   归一后合并成 1 个 key，涉及 **12398 行**（占全量 33%）。

2. **`provider` 一名两写**。同一个 provider 有两种拼法：
   - `alibaba`（18978） vs `provider-alibaba-b600d5a733::openai_chat_completions`（1344）
   后者是 CCR 的**内部逻辑名**（AGENTS.md 军规第 1 条已记「不可当客户端/模型 ID」），
   但它同时携带了**协议形状**（`openai_chat_completions` / `openai_responses`）——
   这个信息要留，不能一起丢。

3. **占位 ID 混在真实模型里**：`unknown`（220）、`default`（1）、
   `gpt-9-no-such-model-e2e-20260923`（3，09-23 一次 e2e 测试**故意造的假 ID**）、
   `openrouter/free`（37，**路由器不是模型**，AGENTS.md 军规第 4 条）。

【四条口径（都来自实测，不是拍脑袋）】
1. **只剥网关前缀，不剥厂商前缀。** `nvidia/` `minimax/` `stealth/` `qwen/`
   是**模型身份的一部分**，剥了会把不同模型合并成假的「同一个」。
   网关前缀是**传输路径**：`openrouter` `open_router` `openrouter-free`
   `tokenrouter` `anthropic`（最后一个是 Anthropic 兼容层包装，见
   `anthropic/open_router/minimax/…` 的双层嵌套实证）。故**反复剥离**直到不再是网关前缀。
2. **`:free` 不合并进 paid，但能力口径要合并。** 成本统计里二者天差地别（`cost_usd`
   甚至可能为 NULL —— 免费模型落库 NULL 是实测事实，静默当 0 就是谎报）；
   而「这个模型会什么」不该因计费档而裂成两条。故拆成两个字段：
   `canonical_model`（保留 `:free`，用于成本/用量）与 `base_model`（去档位，用于能力）。
3. **归一在读取侧，不改账本。** 本模块**纯函数、stdlib-only、不碰 sqlite**，
   连得上层怎么用都不由它决定（`usage_ledger` 仍按 `mode=ro` 打开）。
4. **不靠「名字看着怪」猜非模型。** 只认精确字面量 + 一个显式测试痕迹标记
   （`no-such-model`）。启发式猜会在上游新增正经模型时误杀。

【大小写】
实测全量**零**组「仅大小写不同的同模型」⇒ 统一小写是安全的防御性归一
（防上游将来混写），不会把两个不同模型合并。
"""
from __future__ import annotations

import re
from typing import Dict, Optional, Tuple

#: 网关/传输层前缀 —— 剥掉它们，剩下的才是模型身份。反复剥离（`anthropic/open_router/x`）。
GATEWAY_PREFIXES = frozenset((
    "openrouter",
    "open_router",      # FCC 时代写法（AGENTS.md 军规：CCR 用斜杠、FCC 用下划线）
    "openrouter-free",
    "tokenrouter",
    "anthropic",        # Anthropic 兼容层包装，非厂商身份
))

#: 厂商前缀 —— **禁止剥离**，它们是身份的一部分。
#: 列出是为了让「为什么 `nvidia/` 没被剥」有据可查，并防将来有人把 GATEWAY_PREFIXES 抄宽。
VENDOR_PREFIXES = frozenset((
    "nvidia", "minimax", "stealth", "inclusionai", "poolside", "nex-agi",
    "dots-studio", "cohere", "thinkingmachines", "liquid", "agents",
    "apodex", "qwen", "alibaba", "google", "volcano", "agnes",
    "deepseek", "deepseek-anthropic", "unbiased", "openai", "sakana",
    "scaledown",
))

#: 精确字面量：这些**不是模型**。
#: `free` 在此 —— `openrouter/free` 是**路由器**（自动挑模型的那个），
#: 它没有固定能力，P0-c 不能把它当「某个模型」。
NON_MODEL_LITERALS = frozenset((
    "", "unknown", "default", "free", "auto", "none", "null",
))

#: e2e/测试残留的显式痕迹。实测命中 `gpt-9-no-such-model-e2e-20260923`（3 条）。
_TEST_MARKER = "no-such-model"

#: 计费档后缀（CCR/OpenRouter 侧的免费池标记）。
_FREE_SUFFIX = ":free"

#: provider 内部逻辑名：`provider-<name>-<hash>::<api_shape>`。
#: 实证样本：`provider-alibaba-b600d5a733::openai_chat_completions`。
_PROVIDER_INTERNAL = re.compile(r"^provider-(.+)-([0-9a-f]{6,})::(.+)$")

#: 计费档
TIER_FREE = "free"
TIER_PAID = "paid"
TIER_UNKNOWN = "unknown"


def _strip_gateway(model: str) -> Tuple[str, int]:
    """反复剥离网关前缀。返回 `(剩余, 剥了几层)`。"""
    peeled = 0
    while "/" in model:
        head, rest = model.split("/", 1)
        if head.lower() in GATEWAY_PREFIXES:
            model, peeled = rest, peeled + 1
        else:
            break
    return model, peeled


def canonical_model(raw: Optional[str]) -> str:
    """归一后的模型 ID（**保留** `:free` 档位后缀），用于成本/用量口径。

    >>> canonical_model("anthropic/open_router/minimax/minimax-m3:free")
    'minimax/minimax-m3:free'
    >>> canonical_model("tokenrouter/qwen3.8-flash")
    'qwen3.8-flash'
    >>> canonical_model("nvidia/nemotron-3-ultra-550b-a55b:free")
    'nvidia/nemotron-3-ultra-550b-a55b:free'      # 厂商前缀原样保留
    """
    if raw is None:
        return ""
    s = str(raw).strip().lower()
    s, _ = _strip_gateway(s)
    return s.strip("/").strip()


def tier_of(canonical: str) -> str:
    """计费档：`:free` → free，`:free` 之外有值 → paid，空/未知 → unknown。"""
    c = (canonical or "").strip()
    if not c or c in NON_MODEL_LITERALS:
        return TIER_UNKNOWN
    return TIER_FREE if c.endswith(_FREE_SUFFIX) else TIER_PAID


def base_model(raw: Optional[str]) -> str:
    """去计费档后的模型 ID，用于**能力画像**口径（同模型跨档不裂成两条）。

    >>> base_model("minimax/minimax-m3:free")
    'minimax/minimax-m3'
    """
    c = canonical_model(raw)
    return c[:-len(_FREE_SUFFIX)] if c.endswith(_FREE_SUFFIX) else c


def is_model(raw: Optional[str]) -> Tuple[bool, str]:
    """这个 ID 是不是一个**具体模型**。返回 `(是, 理由)`，理由恒非空可审计。

    >>> is_model("unknown"), is_model("openrouter/free")
    ((False, '占位/路由器字面量: unknown'), (False, '占位/路由器字面量: free'))
    >>> is_model("gpt-9-no-such-model-e2e-20260923")[1]
    '测试残留(含 no-such-model)'
    """
    c = canonical_model(raw)
    if c in NON_MODEL_LITERALS:
        return False, "占位/路由器字面量: %s" % (c or "<空>")
    if _TEST_MARKER in c:
        return False, "测试残留(含 %s)" % _TEST_MARKER
    return True, "具体模型"


def canonical_provider(raw: Optional[str]) -> Tuple[str, str]:
    """归一 provider。返回 `(规范名, 协议形状)`。

    内部逻辑名 `provider-alibaba-<hash>::openai_responses` ⇒ `('alibaba', 'openai_responses')`；
    干净名 `alibaba` ⇒ `('alibaba', '')`。网关名归一到 `openrouter`。

    >>> canonical_provider("provider-alibaba-b600d5a733::openai_chat_completions")
    ('alibaba', 'openai_chat_completions')
    >>> canonical_provider("open_router")
    ('openrouter', '')
    """
    p = (str(raw).strip().lower() if raw else "")
    m = _PROVIDER_INTERNAL.match(p)
    if m:
        return m.group(1), m.group(3)
    if p in ("open_router", "openrouter-free", "tokenrouter"):
        return "openrouter", ""       # 网关写法归一
    return p, ""


def normalize(raw_model: Optional[str], raw_provider: Optional[str] = None) -> Dict:
    """一次拿全：P0-c 聚合时每个维度该用什么，一目了然（避免各处各写一套口径）。

    返回字典键：`canonical` / `base` / `tier` / `is_model` / `why` /
    `provider` / `api_shape`。
    """
    canonical = canonical_model(raw_model)
    ok, why = is_model(canonical)
    provider, api_shape = canonical_provider(raw_provider)
    return {
        "canonical": canonical,
        "base": base_model(canonical),
        "tier": tier_of(canonical),
        "is_model": ok,
        "why": why,
        "provider": provider,
        "api_shape": api_shape,
    }
