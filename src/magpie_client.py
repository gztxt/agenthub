"""magpie 只读互调客户端（2026-11-27）。

## 定位

hub 与 magpie 的**唯一**数据读取入口。只读，不写 magpie 任何配置。

- `GET /v1/models`          模型清单 + 能力元数据（context / pricing / reasoning / tools）
- `GET /v1/magpie/quotas`   订阅账号额度（每卡若干 window，`remaining` 是**百分比** 0~100）

两个端点都免 key、本机回环可读（2026-11-27 实测：53 模型 / 4 张额度卡）。

## 为什么需要（缺口证据）

派发池原先写死 6 条且全是 `workbuddy/*`，而网关有 53 个模型、4 个 provider
（workbuddy / raccoon / dumate / auto）⇒ 88% 的模型不可选，raccoon、dumate 的额度
在派发侧完全不可见。本模块把「有什么模型、还能不能用」变成可查询事实。

## 降级契约（不许静默）

任何失败都返回 `(None, reason)`；调用方必须把 reason 带进响应/日志，
不得把「读不到」伪装成「没有模型」。
"""
from __future__ import annotations

import json
import logging
import os
import threading
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

log = logging.getLogger("magpie_client")

#: 网关 base（裸主机）。与 modelcfg 同源，零新增配置。
def _base() -> str:
    return os.getenv("CLAUDE_CCR_URL", "http://127.0.0.1:3425").rstrip("/")


MODELS_TTL_S = float(os.getenv("MAGPIE_MODELS_TTL", "60"))
QUOTAS_TTL_S = float(os.getenv("MAGPIE_QUOTAS_TTL", "10"))
TIMEOUT_S = float(os.getenv("MAGPIE_HTTP_TIMEOUT", "6"))

#: 额度低于此百分比即视为「耗尽」。
MIN_REMAINING_PCT = float(os.getenv("MAGPIE_QUOTA_MIN_PCT", "3"))

#: `auto/*` 是路由组（一组别的模型的包装），不是具体模型 ⇒ 不参与档位派生。
EXCLUDE_PREFIXES = ("auto/",)

_cache: Dict[str, Tuple[float, Any, str]] = {}
_lock = threading.Lock()


def _get_json(path: str, ttl: float, tag: str) -> Tuple[Optional[Any], str]:
    """带 TTL 的 GET。返回 (data, reason)；失败 data=None、reason 非空。"""
    now = time.monotonic()
    with _lock:
        hit = _cache.get(tag)
        if hit and (now - hit[0]) < ttl:
            return hit[1], hit[2]
    url = _base() + path
    try:
        with urllib.request.urlopen(url, timeout=TIMEOUT_S) as resp:  # noqa: S310
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        reason = "magpie HTTP %s: %s" % (e.code, url)
        log.warning("[magpie] %s", reason)
        return None, reason
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        reason = "magpie 不可达（%s）: %s" % (url, type(e).__name__)
        log.warning("[magpie] %s", reason)
        return None, reason
    except (ValueError, KeyError) as e:
        reason = "magpie 回包不可解析: %s" % type(e).__name__
        log.warning("[magpie] %s", reason)
        return None, reason
    with _lock:
        _cache[tag] = (now, data, "")
    return data, ""


def models(ttl: Optional[float] = None) -> Tuple[Optional[List[dict]], str]:
    """模型清单。失败返回 (None, reason)。"""
    data, reason = _get_json("/v1/models", MODELS_TTL_S if ttl is None else ttl, "models")
    if data is None:
        return None, reason
    ms = data.get("data") if isinstance(data, dict) else None
    if not isinstance(ms, list):
        return None, "magpie /v1/models 回包无 data 数组"
    return ms, ""


def quotas(ttl: Optional[float] = None) -> Tuple[Optional[List[dict]], str]:
    """额度卡。失败返回 (None, reason)。"""
    data, reason = _get_json("/v1/magpie/quotas",
                             QUOTAS_TTL_S if ttl is None else ttl, "quotas")
    if data is None:
        return None, reason
    cards = data.get("data") if isinstance(data, dict) else None
    if not isinstance(cards, list):
        return None, "magpie /v1/magpie/quotas 回包无 data 数组"
    return cards, ""


def _card_state(card: dict) -> Tuple[str, str]:
    """单张额度卡 → (state, reason)。state ∈ ok / exhausted / error。"""
    if card.get("error"):
        return "error", str(card["error"])[:160]
    windows = card.get("windows") or []
    if not windows:
        # 无窗口 = 供应商不报额度，不等于不可用。
        return "ok", "no-window"
    mins = []
    for w in windows:
        try:
            mins.append(float(w.get("remaining")))
        except (TypeError, ValueError):
            continue
    if mins and max(mins) <= MIN_REMAINING_PCT:
        worst = min(mins)
        return "exhausted", "全部窗口剩余≤%.0f%%（最低 %.1f%%）" % (MIN_REMAINING_PCT, worst)
    return "ok", ""


def provider_health(ttl: Optional[float] = None) -> Tuple[Optional[Dict[str, dict]], str]:
    """provider → {state, reason, accounts, remaining_pct}。

    聚合规则：**有一张卡 ok 就算该 provider 可用**（同 provider 多账号可轮换）。
    全部 error ⇒ error；有卡耗尽且无 ok ⇒ exhausted。
    """
    cards, reason = quotas(ttl)
    if cards is None:
        return None, reason
    out: Dict[str, dict] = {}
    for c in cards:
        prov = str(c.get("provider") or "")
        if not prov:
            continue
        state, why = _card_state(c)
        rem = []
        for w in (c.get("windows") or []):
            try:
                rem.append(float(w.get("remaining")))
            except (TypeError, ValueError):
                continue
        slot = out.setdefault(prov, {"state": "error", "reason": "", "accounts": 0,
                                     "remaining_pct": None})
        slot["accounts"] += 1
        if rem:
            best = max(rem)
            if slot["remaining_pct"] is None or best > slot["remaining_pct"]:
                slot["remaining_pct"] = round(best, 1)
        if state == "ok":
            slot["state"] = "ok"
            slot["reason"] = ""
        elif slot["state"] != "ok":
            slot["state"] = state
            slot["reason"] = why
    return out, ""


def blocked_providers(ttl: Optional[float] = None
                      ) -> Tuple[Optional[Dict[str, str]], str]:
    """不可用 provider → 原因。读不到额度时返回 (None, reason)（不猜）。"""
    health, reason = provider_health(ttl)
    if health is None:
        return None, reason
    return {p: (h.get("reason") or h.get("state") or "blocked")
            for p, h in health.items() if h.get("state") != "ok"}, ""


def _price_of(m: dict) -> Optional[float]:
    p = (m.get("pricing") or {}).get("prompt")
    try:
        return float(p) if p is not None else None
    except (TypeError, ValueError):
        return None


def derive_tiers(per_tier: int = 3, ttl: Optional[float] = None
                 ) -> Tuple[Optional[Dict[str, List[str]]], str]:
    """从网关清单派生 t1/t2/t3。

    **规则（声明，不是测量）**：按 provider 分组，组内按 prompt 单价升序；
    只保留额度可用的 provider；t1 取各 provider 最便宜、t3 取最贵、t2 取中位。
    **不猜**：清单或额度读不到 ⇒ (None, reason)，调用方回落写死默认值。
    """
    ms, reason = models(ttl)
    if ms is None:
        return None, reason
    health, hreason = provider_health(ttl)
    if health is None:
        return None, hreason

    by_prov: Dict[str, List[dict]] = {}
    for m in ms:
        mid = str(m.get("id") or "")
        if not mid or "/" not in mid:
            continue
        if any(mid.startswith(p) for p in EXCLUDE_PREFIXES):
            continue
        prov = mid.split("/")[0]
        h = health.get(prov)
        if h is None or h.get("state") != "ok":
            continue
        by_prov.setdefault(prov, []).append(m)
    if not by_prov:
        return None, "没有额度可用的 provider"

    cheapest: List[str] = []
    priciest: List[str] = []
    middles: List[str] = []
    # 按 provider 的模型数降序（确定性优先级）：provider 多于 `per_tier` 时，
    # 被截掉的是小 provider 而不是按字母序误杀主力 provider。
    for prov, items in sorted(by_prov.items(), key=lambda kv: (-len(kv[1]), kv[0])):
        priced = [m for m in items if _price_of(m) is not None]
        priced.sort(key=lambda m: (_price_of(m) or 0.0, str(m.get("id"))))
        pool = priced or sorted(items, key=lambda m: -(int(m.get("context_window") or 0)))
        if pool:
            cheapest.append(str(pool[0]["id"]))
        if len(pool) > 1:
            priciest.append(str(pool[-1]["id"]))
        if len(pool) > 2:
            middles.append(str(pool[len(pool) // 2]["id"]))

    def _uniq(xs: List[str]) -> List[str]:
        seen, out = set(), []
        for x in xs:
            if x not in seen:
                seen.add(x)
                out.append(x)
        return out

    tiers = {
        "t1": _uniq(cheapest)[:per_tier],
        "t2": _uniq(middles + cheapest)[:per_tier],
        "t3": _uniq(priciest + middles)[:per_tier],
    }
    for k, v in tiers.items():
        if not v:
            return None, "%s 档派生为空" % k
    return tiers, ""


def status() -> dict:
    """给 /health 或调试端点的一段摘要。"""
    ms, mreason = models()
    health, hreason = provider_health()
    out: Dict[str, Any] = {
        "base": _base(),
        "models": len(ms) if ms else None,
        "models_error": mreason,
        "providers": health,
        "quotas_error": hreason,
        "min_remaining_pct": MIN_REMAINING_PCT,
    }
    blocked, _ = blocked_providers()
    out["blocked"] = blocked
    return out
