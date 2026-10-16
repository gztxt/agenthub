"""上游网关（CCR）连通性 + **模型注册清单**的缓存式体检，给 /health 用。

为什么要它（2026-09-25，收口 PT-20260923-05/06 的 M1）：
  · 本机三次同源事故都是**模型 ID 写错或失效**，而 hub 自己看不见上游注册清单，
    只能等一次真请求把错误烧出来：09-06 `minimax-m3:free` HTTP 400、09-19 `'ultra'`
    无效、09-23 `qwen3.8-flash` 缺 provider 前缀（有效 ID 是 `alibaba/qwen3.8-flash`）。
  · 09-23 台账把 M1 记成"阻塞：拿不到 CCR 在线清单（用 ~/.claude 的 key 打 :3456 实测 401）"。
    09-25 实测：用 **hub 自己的** `MANAGER_LLM_API_KEY` 打 `http://127.0.0.1:3456/v1/models`
    返 **200 / 14 个 ID / 1.8ms** ⇒ 阻塞解除，清单可以常驻可观测。
  · 同时改判一条错账：`agnes/agnes-2.0-flash`（vitals 的 L4 探针模型）**不在**免费池
    ——14 个 ID 里带 free 的 5 个全是 `openrouter/*:free`。所以 M1 原口径"换掉免费池成员"
    的前提不成立；真正的长期风险是**改名/下架**，故这里把它做成断言：
    `watch` 里每个 ID 是否仍在注册清单 ⇒ 改名当天就能从 /health 看出来，
    不必等探活烧一轮 token（也就不会再有"全绿而功能层已死"的静默期）。

口径（与 `tdai_client.backend_status()` 同族，照抄它的设计意图）：
  · **纯读缓存、零阻塞**：/health 被前端轮询，绝不能因为它去等一次网络。
    首次调用回 `state=unknown` 并在后台起一探；TTL 内只回旧值；过期走
    stale-while-revalidate（先回旧值，刷新在后台）。
  · **只兑情报、不改 `status`**：上游网关不可达 ≠ hub 坏了（同 `code_stale` 的设计意图）。
  · **绝不回显凭据**：只给 `key_present` / `key_len`；端点只给 `scheme://host:port`。
  · 单飞（single-flight）：同一时刻最多一个探测线程，轮询再密也不会放大成请求风暴。
"""

from __future__ import annotations

import json
import os
import threading
import time
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit
from urllib.request import Request, urlopen

import resilience

TTL_S = float(os.getenv("HUB_GW_TTL_S", "300"))
TIMEOUT_S = float(os.getenv("HUB_GW_TIMEOUT_S", "5"))
MAX_IDS = 60            # /health 里最多回这么多 ID（本机实测 14 个，留足余量）
# 退避重试上限。**刻意给 0 次重试**：本函数已在后台线程跑（`_schedule_probe`），
# 而它的结果进的是 TTL 缓存——晚 8 秒拿到和 8 秒后自然过期拿到，对用户没有区别，
# 却会让「网关此刻不通」这件事晚 8 秒才出现在 /health。退避留给「同一次请求内
# 必须拿到结果」的场景（GitHub API），这里只留熔断：坏网关被快速断开，
# 省掉的是「明知不通还反复试」的等待。
GW_ATTEMPTS = 1

_CFG: Dict[str, Any] = {"base": "", "key": "", "watch": []}
_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None, "count": 0}
_LOCK = threading.Lock()
_INFLIGHT: List[threading.Thread] = []


def configure(base: str = "", key: str = "", watch=()) -> None:
    """启动时注入真值（main.py 从 config + vitals 取），使本模块不依赖任何项目内模块 ⇒ L0 可直读。"""
    with _LOCK:
        _CFG["base"] = (base or "").rstrip("/")
        _CFG["key"] = key or ""
        _CFG["watch"] = [str(w) for w in (watch or ()) if w]


def models_url(base: Optional[str] = None) -> Optional[str]:
    """把 base 归一成 `.../v1/models`。三种写法都要认：裸主机、带 /v1、已带 /models。

    为什么要归一：09-07 那次事故就是把 base_url 改到 3457 网关口 + 路径拼错，
    401/404 混在一起看不出真因。归一后 url 本身也进 /health，一眼可对。
    """
    b = (base if base is not None else _CFG["base"]) or ""
    b = b.rstrip("/")
    if not b:
        return None
    if b.endswith("/models"):
        return b
    if b.endswith("/v1"):
        return b + "/models"
    return b + "/v1/models"


def endpoint_label(base: str = "") -> str:
    """只回 `scheme://host:port` —— path/query 一律不带（防止把凭据形态的串漏进 /health）。"""
    b = base or _CFG["base"] or ""
    if not b:
        return ""
    try:
        p = urlsplit(b)
        return "%s://%s" % (p.scheme, p.netloc) if p.netloc else b
    except Exception:  # noqa: BLE001
        return ""


def _fetch_raw(url: str, key: str, timeout: float) -> Tuple[int, List[str]]:
    """裸调用，**不含退避**。抽出它是为了让熔断只包住真正发网络的那一步，
    而 JSON 解析留在外面（解析失败不该计入对端的失败次数）。"""
    headers = {"Authorization": "Bearer " + key} if key else {}
    with urlopen(Request(url, headers=headers), timeout=timeout) as r:  # noqa: S310
        body = r.read()
        code = int(r.getcode() or 0)
    data = json.loads(body.decode("utf-8", "replace")) or {}
    items = data.get("data") if isinstance(data, dict) else None
    ids = [m.get("id") for m in (items or []) if isinstance(m, dict)]
    return code, [i for i in ids if i]


def fetch(url: str, key: str, timeout: float = TIMEOUT_S) -> Tuple[int, List[str]]:
    """真网络调用。**模块级函数**，L0 测试直接替换它 ⇒ 空 HOME 下零网络也能取证。

    熔断键 = `host|gw_models`：与其它用途分开，因为「/v1/models 拿不到」和
    「GitHub API 拿不到」是两个独立故障域，共用一个额度会互相拖累。
    """
    breakers = resilience.instance
    return breakers.call(
        resilience.breaker_key(url, "gw_models"),
        lambda: _fetch_raw(url, key, timeout),
        attempts=GW_ATTEMPTS,
    )


def is_free(model_id: str) -> bool:
    """免费池成员判据：`*:free` 或 `*/free`（本机实测 5 个全属此形态）。

    为什么单独抽出来：军规第 4 条禁把 `:free` 写成长期默认值（CCR 每日 12:00 轮换会摘成员），
    所以"哪些是免费池"必须是可断言的量，而不是靠人眼扫清单。
    """
    s = str(model_id or "")
    return s.endswith(":free") or s.endswith("/free")


def _probe_once() -> Dict[str, Any]:
    url = models_url()
    out: Dict[str, Any] = {
        "state": "error", "http": None, "probe_ms": None, "error": None,
        "url": url, "endpoint": endpoint_label(),
        "key_present": bool(_CFG["key"]), "key_len": len(_CFG["key"]),
        "models_count": 0, "free_count": 0, "models": [], "watch": {},
    }
    if not url:
        out["error"] = "no_base_url"
        return out
    t0 = time.monotonic()
    try:
        code, ids = fetch(url, _CFG["key"])
        out["http"] = code
        out["probe_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["models_count"] = len(ids)
        out["free_count"] = sum(1 for i in ids if is_free(i))
        out["models"] = ids[:MAX_IDS]
        out["watch"] = {w: (w in ids) for w in _CFG["watch"]}
        out["state"] = "ok" if code == 200 and ids else ("error" if code != 200 else "empty")
        if code != 200:
            out["error"] = "http_%d" % code
        elif not ids:
            out["error"] = "empty_model_list"
    except Exception as e:  # noqa: BLE001
        out["probe_ms"] = round((time.monotonic() - t0) * 1000, 1)
        out["error"] = "%s: %s" % (type(e).__name__, str(e)[:120])
    return out


def _commit(d: Dict[str, Any]) -> None:
    with _LOCK:
        _CACHE["data"] = d
        _CACHE["ts"] = time.monotonic()
        _CACHE["count"] += 1


def _schedule_probe() -> bool:
    """后台起一探；已有在飞的就不再起（单飞）。返回是否真的起了新线程。"""
    with _LOCK:
        for t in _INFLIGHT:
            if t.is_alive():
                return False
        _INFLIGHT.clear()

    def run() -> None:
        try:
            _commit(_probe_once())
        except Exception as e:  # noqa: BLE001  # 线程里抛出会静默杀掉刷新能力
            _commit({"state": "error", "error": "%s: %s" % (type(e).__name__, str(e)[:120]),
                     "endpoint": endpoint_label(), "key_present": bool(_CFG["key"]),
                     "key_len": len(_CFG["key"]), "models_count": 0, "free_count": 0,
                     "models": [], "watch": {}, "http": None, "probe_ms": None, "url": None})

    t = threading.Thread(target=run, name="gwprobe", daemon=True)
    with _LOCK:
        _INFLIGHT.append(t)
    t.start()
    return True


def status() -> Dict[str, Any]:
    """/health 专用：**只读缓存**，永不阻塞。"""
    with _LOCK:
        d, ts, n = _CACHE["data"], _CACHE["ts"], _CACHE["count"]
    age = round(time.monotonic() - ts, 1) if ts else None
    base = {"endpoint": endpoint_label(), "key_present": bool(_CFG["key"]),
            "key_len": len(_CFG["key"]), "ttl_s": TTL_S, "probes": n,
            "watch": {w: None for w in _CFG["watch"]}}
    if d is None:
        _schedule_probe()
        return {**base, "state": "unknown", "stale": True, "age_s": None,
                "models_count": 0, "free_count": 0, "models": []}
    stale = bool(age is not None and age > TTL_S)
    if stale:
        _schedule_probe()          # 先回旧值，刷新在后台
    return {**d, **{k: v for k, v in base.items() if k not in d}, "age_s": age,
            "stale": stale, "probes": n}


def probe_now() -> Dict[str, Any]:
    """同步探一次并落缓存。给 L2 live 闸门与启动预热用；**/health 绝不调它**。"""
    d = _probe_once()
    _commit(d)
    return d


def reset_cache_for_test() -> None:
    with _LOCK:
        _CACHE["ts"] = 0.0
        _CACHE["data"] = None
        _CACHE["count"] = 0
        _INFLIGHT.clear()
