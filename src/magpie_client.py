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
LIBRARY_TTL_S = float(os.getenv("MAGPIE_LIBRARY_TTL", "30"))
TIMEOUT_S = float(os.getenv("MAGPIE_HTTP_TIMEOUT", "6"))

#: 额度低于此百分比即视为「耗尽」。
MIN_REMAINING_PCT = float(os.getenv("MAGPIE_QUOTA_MIN_PCT", "3"))

#: 实时错误窗（2026-11-27 事故：dumate 连续 429 每次耗 38 秒，闸门只看额度 ⇒ 派发卡死）。
#: 判据：窗口内 fail ≥ N 且 fail 占比 ≥ 比例 ⇒ 该 provider 临时不可用。
ERR_WINDOW_MIN = float(os.getenv("MAGPIE_ERR_WINDOW_MIN", "10"))
ERR_MIN_FAILS = int(os.getenv("MAGPIE_ERR_MIN_FAILS", "3"))
ERR_RATIO = float(os.getenv("MAGPIE_ERR_RATIO", "0.5"))
ERR_TTL_S = float(os.getenv("MAGPIE_ERR_TTL", "30"))

#: `auto/*` 是路由组（一组别的模型的包装），不是具体模型 ⇒ 不参与档位派生。
EXCLUDE_PREFIXES = ("auto/",)

#: flash 族判据：id 含 `flash`（大小写不敏感）。2026-11-27 实测 53 个模型里命中 14 个。
#: 为什么不按价格推断：价格字段只有 27/53 有值，且 0 价既有 flash 也有非 flash（hy4std）
#: ⇒ 用价格会同时误判两类。名称判据可解释、可复核、可回滚。
_FLASH_MARK = "flash"


def _flash_first() -> bool:
    """flash 倾斜开关（用户令 2026-11-27：「提高 flash 的模型占用比例」）。

    关闭时走 v0.13.214 的**原分支逐字不动**（见 `derive_tiers` 的 else 分支）。
    """
    return (os.getenv("TASK_TIERS_FLASH_FIRST", "off") or "off").strip().lower() in (
        "1", "true", "yes", "on")


def is_flash(model_id: str) -> bool:
    return _FLASH_MARK in (model_id or "").lower()

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


_err_cache: Tuple[float, Dict[str, dict]] = (0.0, {})


def recent_failures(minutes: Optional[float] = None) -> Dict[str, dict]:
    """从 magpie 账本尾部统计近 N 分钟各 provider 的成败。

    返回 `{provider: {"total": n, "fail": m, "ratio": r, "last_error": s}}`。
    读不到账本 ⇒ `{}`（**不挡**：不知道不等于坏）。
    """
    global _err_cache
    now = time.monotonic()
    ts, cached = _err_cache
    if cached and (now - ts) < ERR_TTL_S:
        return cached
    win = float(minutes if minutes is not None else ERR_WINDOW_MIN)
    path = os.getenv("MAGPIE_USAGE_PATH", "~/.config/magpie/usage.jsonl")
    path = os.path.expanduser(path)
    out: Dict[str, dict] = {}
    if not os.path.isfile(path):
        _err_cache = (now, {})
        return {}
    from datetime import datetime, timedelta, timezone
    lo = datetime.now(timezone.utc) - timedelta(minutes=win)
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    continue
                try:
                    t = datetime.fromisoformat(str(row.get("t")))
                except ValueError:
                    continue
                if t.tzinfo is None:
                    t = t.replace(tzinfo=timezone.utc)
                if t < lo:
                    continue
                prov = str(row.get("provider") or "")
                if not prov:
                    continue
                slot = out.setdefault(prov, {"total": 0, "fail": 0, "ratio": 0.0,
                                             "last_error": ""})
                slot["total"] += 1
                status = str(row.get("status"))
                if status not in ("200", "0", ""):
                    slot["fail"] += 1
                    slot["last_error"] = "HTTP " + status
    except OSError:
        _err_cache = (now, {})
        return {}
    for slot in out.values():
        if slot["total"]:
            slot["ratio"] = round(slot["fail"] / slot["total"], 2)
    _err_cache = (now, out)
    return out


def _error_blocked(errs: Dict[str, dict]) -> Dict[str, str]:
    """把「窗口内失败过多」翻译成与额度闸门同形的 blocked 映射。"""
    out: Dict[str, str] = {}
    for prov, e in errs.items():
        total, fail = int(e.get("total") or 0), int(e.get("fail") or 0)
        if fail >= ERR_MIN_FAILS and (e.get("ratio") or 0) >= ERR_RATIO:
            out[prov] = "近 %.0f 分钟失败 %d/%d（%s）" % (
                ERR_WINDOW_MIN, fail, total, e.get("last_error") or "error")
    return out


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
    # 2026-11-27：额度健康 ≠ 现在能用。用账本尾部把「刚刚一直 429/5xx」的 provider
    # 一并标坏，否则派发会每秒 38 秒地空烧（实测 dumate 连续 6 次 429）。
    try:
        err_blocked = _error_blocked(recent_failures())
    except Exception as e:  # noqa: BLE001 —— 错误窗是增强，不许打死额度读取
        log.warning("[magpie] 错误窗统计失败：%s", type(e).__name__)
        err_blocked = {}
    for prov, why in err_blocked.items():
        slot = out.setdefault(prov, {"state": "error", "reason": "", "accounts": 0,
                                     "remaining_pct": None})
        if slot.get("state") == "ok":
            slot["state"] = "degraded"
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

    if _flash_first():
        # ── flash 倾斜（2026-11-27 用户令）─────────────────────────────
        # 目标：t1 全 flash、t2 2 flash + 1 强项、t3 质量优先（2 强项 + 1 贵 flash 兜底）。
        # 判据只认名称含 flash（14/53 实测），不做价格推断。
        # 档间**全局去重**：flash 池小（dumate 1 个、workbuddy 2 个），
        # 不去重会让 t1 与 t2 出现同一模型，派发日志就看不出档位差异。
        fpool: Dict[str, List[str]] = {}
        ppool: Dict[str, List[str]] = {}
        for prov, items in sorted(by_prov.items(), key=lambda kv: (-len(kv[1]), kv[0])):
            priced = [m for m in items if _price_of(m) is not None]
            priced.sort(key=lambda m: (_price_of(m) or 0.0, str(m.get("id"))))
            pool = [str(m["id"]) for m in (priced or sorted(
                items, key=lambda m: -(int(m.get("context_window") or 0))))]
            fpool[prov] = [x for x in pool if is_flash(x)]
            ppool[prov] = [x for x in pool if not is_flash(x)]

        used: set = set()

        def _rr(pools: Dict[str, List[str]], idx: int) -> List[str]:
            return [lst[idx] for lst in pools.values() if len(lst) > idx]

        def _tail(pools: Dict[str, List[str]]) -> List[str]:
            return [lst[-1] for lst in pools.values() if lst]

        def _take(cands: List[str], n: int) -> List[str]:
            out: List[str] = []
            for c in cands:
                if c not in used and c not in out:
                    out.append(c)
                    if len(out) >= n:
                        break
            used.update(out)
            return out

        t1 = _take(_rr(fpool, 0) + _rr(fpool, 1) + _rr(ppool, 0), per_tier)
        t2 = _take(_rr(fpool, 1) + _rr(fpool, 0) + _rr(fpool, 2)
                   + _rr(ppool, 1) + _rr(ppool, 0), per_tier)
        t3 = _take(_tail(ppool) + [p[-1] for p in fpool.values() if p]
                   + _rr(ppool, 1), per_tier)
        # 任何一档凑不满（provider 太少）⇒ 判失败，回落写死表，不交半张表出去
        if not (t1 and t2 and t3):
            return None, "flash 倾斜后档位不完整（t1=%d t2=%d t3=%d）" % (
                len(t1), len(t2), len(t3))
        tiers = {"t1": t1, "t2": t2, "t3": t3}
    else:
        tiers = {
            "t1": _uniq(cheapest)[:per_tier],
            "t2": _uniq(middles + cheapest)[:per_tier],
            "t3": _uniq(priciest + middles)[:per_tier],
        }
    for k, v in tiers.items():
        if not v:
            return None, "%s 档派生为空" % k
    return tiers, ""


#: magpie 的技能/MCP/指令库（它是**分发者**：往各 agent 目录建软链）。
#: 2026-11-27 实测：68 条技能的 source.dir 全部指向 `/fs/1000/ftp/技术文档/skills/<name>`，
#: 与 hub 扫描到的是**同一批目录**（68/68 命中）——不是两份数据，所以不需要「写代理」，
#: 需要的是把「这条技能被哪些 agent 启用」这个只有 magpie 知道的事实暴露出来。
def _library_path() -> str:
    """每次调用都重新读环境变量 —— 写死成模块常量会让测试/影子实例无法指向别的库。"""
    return os.path.expanduser(
        os.getenv("MAGPIE_LIBRARY", "~/.config/magpie/library.json"))
_lib_cache: Tuple[float, dict, str] = (0.0, {}, "")


def library(ttl: Optional[float] = None) -> Tuple[Optional[dict], str]:
    """读 magpie 的 library.json（只读）。失败返回 (None, reason)。"""
    global _lib_cache
    now = time.monotonic()
    ttl = LIBRARY_TTL_S if ttl is None else ttl
    ts, cached, why = _lib_cache
    if cached and (now - ts) < ttl:
        return cached, why
    path = _library_path()
    if not os.path.isfile(path):
        _lib_cache = (now, {}, "library.json 不存在：%s" % path)
        return None, _lib_cache[2]
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
    except (OSError, ValueError) as e:
        _lib_cache = (now, {}, "library.json 读取失败：%s" % type(e).__name__)
        return None, _lib_cache[2]
    _lib_cache = (now, data, "")
    return data, ""


def library_skills_map() -> Tuple[Dict[str, dict], str]:
    """真实目录（os.path.realpath）→ {name, agents, from, kind}。

    用目录作键而不是名字：实测有 3 条**同名目录不同名**（`agent-browser-cli` 在 hub 叫
    `agent-browser`），按名字 join 会漏。
    """
    data, why = library()
    if data is None:
        return {}, why
    out: Dict[str, dict] = {}
    for s in (data.get("skills") or []):
        src = (s.get("source") or {})
        d = src.get("dir") or ""
        if not d:
            continue
        out[os.path.realpath(d)] = {
            "name": s.get("name") or "",
            "agents": list(s.get("agents") or []),
            "from": s.get("from") or "",
            "kind": src.get("kind") or "",
        }
    return out, ""


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
