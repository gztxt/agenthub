"""配置模型 ⊂ 网关清单 —— 启动 + `/health` 闸门（2026-11-02 第 11 点）。

## 为什么要有它（四次同源事故的收口）

| 日期 | 死掉的 ID | 症状 | 当时修法 |
|---|---|---|---|
| 09-06 | `minimax-m3:free` | Hermes HTTP 400 断连 | 只改 ID |
| 09-19 | `'ultra'` | 功能层 FAIL | 只改 ID |
| 09-23 | `qwen3.8-flash` | 缺 provider 前缀 | 只改 ID |
| 11-02 | `agnes/*` 整族 + `deepseek/deepseek-flash` | 拆解 100% 400、后台压缩静默返空 | **本模块** |

前三次都只改那一行，所以第四次照样发生。这一次的正解不是换 ID，而是让
「配置里的 ID 不在网关清单里」这件事**自己报出来**。

## 覆盖范围（不是只盖 `.env`）

`.env` 只是第一个点。2026-11-02 逐文件核出来的写死模型共有 **9 类 16 处**：

1. `MANAGER_LLM_MODEL`（`.env` → `config.manager_llm_model`）
2. `MEMORY_LLM_MODEL`（`llm.memory_model()`，后台压缩/技能重排）
3. `MEMORY_LLM_FALLBACK_MODEL`（`llm.free_fallback_model()`，兜底槽）
4. `tiercfg.DEFAULT_TIERS`（t1/t2/t3 各 2 个成员 = 6 处）
5. `vitals.RT_MODEL`（L4 真请求探针；`/health` 的 `watch` 就是它）
6. `remote_judge.MODEL`（判定后端）
7. `refscout_collect._ref_model()`（检索重排）
8. `profiles.AGENT_PROBE[*]["rt_model"]`（各 agent 的实测应答模型）
9. `adapters/cli_agent.PI_DEFAULT_MODEL`（未指定 model 时的任务默认）

少盖任何一条，下一次事故就会从那个缺口复发 —— 这正是 §11「不缝补」的判据：
能说出链路上共 N 个点、改了哪几个、其余为何不改。

## 口径

- `checked=False`（网关挂了 / 没凭据 / 被 `HUB_MODEL_CATALOG=off` 关掉）
  ⇒ 报 `unknown`，**不判死**。与 `intake` 的 `count=0` vs「那一路挂了」同源。
- `checked=True` 且某个 ID 不在清单里 ⇒ 真问题，进 `missing`。
- **不改 `/health` 的 `status`**：网关不可达 ≠ hub 坏了（与 `ccr_gateway` /
  `code_stale` / `memory_backend` 同一设计意图）。它兑情报，不拦启动。
- 前缀归一：CCR `/v1/models` 给**裸 ID**（`alibaba/x`），而 opencode/omp 侧存
  `ccr/alibaba/x`。比对前剥掉 `ccr/`，否则每条 opencode 记录都是假阳性。

## 已知边界（写清，别当成已根治）

它只能证明「ID 在清单里」，**证不出这个 ID 真能出话** —— `alibaba/decision-model-preview`
在册但每次 400（文档 F6）。真正的准入应像 dsh 的 `settings.save()` 那样在**写入时**
真解析一次（`max_tokens=1` 打一发）。本模块是那道闸门的第一层，不是全部。
"""

from __future__ import annotations

import os
from typing import Dict, Iterable, Optional

#: 允许的 provider 前缀（剥 `ccr/` 之后的形态）。CCR 清单当前只有 `alibaba/*`；
#: 其余前缀留在表里是为了未来接回时不用改代码，不代表它们现在可用。
_KNOWN_PREFIXES = ("alibaba/", "deepseek/", "openrouter/", "agnes/", "qwen")


def normalize(model: str) -> str:
    """把任何客户端的写法归一成 CCR 清单的裸 ID 形态。

    `ccr/alibaba/qwen3.8-flash` → `alibaba/qwen3.8-flash`（opencode / omp 的写法）
    """
    m = str(model or "").strip()
    if m.startswith("ccr/"):
        m = m[4:]
    return m


def configured_models() -> Dict[str, str]:
    """收集**所有**写死模型的位置，返回 `{标签: 原始 ID}`。

    每个来源单独 try/except：一个模块导入失败不许让整条闸门变成假绿，
    也不许把它误报成「没配置」——失败项进 `errors`。
    """
    out: Dict[str, str] = {}
    errors: Dict[str, str] = {}

    def _add(label: str, fn) -> None:
        try:
            v = str(fn() or "")
        except Exception as e:  # noqa: BLE001
            errors[label] = "%s: %s" % (type(e).__name__, str(e)[:80])
            return
        if v:
            out[label] = v

    def _manager() -> str:
        from config import config
        return config.manager_llm_model

    def _memory() -> str:
        import llm
        return llm.memory_model()

    def _fallback() -> str:
        import llm
        return llm.free_fallback_model()

    def _tiers() -> Dict[str, tuple]:
        import tiercfg
        return tiercfg.DEFAULT_TIERS

    def _vitals() -> str:
        import vitals
        return vitals.RT_MODEL

    def _judge() -> str:
        import remote_judge
        return remote_judge.MODEL

    def _refscout() -> str:
        import refscout_collect
        return refscout_collect._ref_model()

    def _pi_default() -> str:
        from adapters import cli_agent
        return cli_agent.PI_DEFAULT_MODEL

    _add("manager", _manager)
    _add("memory", _memory)
    _add("memory_fallback", _fallback)
    _add("vitals_rt", _vitals)
    _add("judge", _judge)
    _add("refscout", _refscout)
    _add("cli_default", _pi_default)

    try:
        for tier, members in (_tiers() or {}).items():
            for i, m in enumerate(members or ()):
                if m:
                    out["tier/%s/%d" % (tier, i)] = str(m)
    except Exception as e:  # noqa: BLE001
        errors["tiers"] = "%s: %s" % (type(e).__name__, str(e)[:80])

    try:
        import profiles
        for name, prof in (getattr(profiles, "AGENT_PROBE", {}) or {}).items():
            m = (prof or {}).get("rt_model")
            if m:
                out["agent_probe/%s" % name] = str(m)
    except Exception as e:  # noqa: BLE001
        errors["agent_probe"] = "%s: %s" % (type(e).__name__, str(e)[:80])

    setattr(configured_models, "_last_errors", errors)
    return out


def configured_errors() -> Dict[str, str]:
    """上一次 `configured_models()` 的收集失败项（只用于诊断，不参与判定）。"""
    return dict(getattr(configured_models, "_last_errors", {}) or {})


def check(catalog: Optional[Iterable[str]] = None,
          configured: Optional[Dict[str, str]] = None) -> dict:
    """闸门主体。

    `catalog` 传 None 时自己去 `modelcfg.catalog_ids()` 拉（带 TTL 缓存）。
    `/health` 应把 `gwprobe.status()` 已取到的 `models` 传进来 —— 那是后台线程取的，
    免掉 /health 再阻塞一次网络（本仓反复踩过的「自证端点自己卡住」）。

    返回：
    ```python
    {"state": "ok"|"missing"|"unknown", "checked": bool, "ok": bool,
     "total": int, "catalog_size": int, "missing": {label: id},
     "unknown_prefix": {label: id}, "error": str, "errors": {label: why}}
    ```
    """
    if configured is None:
        configured = configured_models()
    errors = configured_errors()

    err = ""
    if catalog is None:
        try:
            import modelcfg
            cat = modelcfg.catalog_ids()
            checked = bool(cat.get("checked"))
            ids = {normalize(i) for i in (cat.get("ids") or set())}
            err = str(cat.get("error") or "")
        except Exception as e:  # noqa: BLE001
            checked, ids = False, set()
            err = "%s: %s" % (type(e).__name__, str(e)[:120])
    else:
        ids = {normalize(i) for i in catalog if i}
        checked = bool(ids)
        if not checked:
            err = "catalog empty"

    norm = {label: normalize(m) for label, m in configured.items()}
    if not checked:
        return {"state": "unknown", "checked": False, "ok": False,
                "total": len(norm), "catalog_size": 0, "missing": {},
                "unknown_prefix": {}, "error": err or "catalog unavailable",
                "errors": errors}

    missing = {k: v for k, v in norm.items() if v not in ids}
    # 前缀不认识 ⇒ 可能是别的 provider 侧直连，不当成死 ID 报，但要可见。
    unknown_prefix = {k: v for k, v in missing.items()
                      if not v.startswith(_KNOWN_PREFIXES)}
    missing = {k: v for k, v in missing.items() if k not in unknown_prefix}
    return {"state": "missing" if missing else "ok", "checked": True,
            "ok": not missing, "total": len(norm), "catalog_size": len(ids),
            "missing": missing, "unknown_prefix": unknown_prefix,
            "error": "", "errors": errors}


def gate_disabled() -> bool:
    """`HUB_MODEL_GATE=0` 可整条关掉（L0 单测 / 离线排障用，生产不要关）。"""
    return os.getenv("HUB_MODEL_GATE", "1").strip().lower() in ("0", "off", "false", "no")


def status(catalog: Optional[Iterable[str]] = None) -> dict:
    """/health 用入口：关掉时明确报 disabled，而不是伪装成 unknown。"""
    if gate_disabled():
        return {"state": "disabled", "checked": False, "ok": False,
                "total": 0, "catalog_size": 0, "missing": {},
                "unknown_prefix": {}, "error": "disabled by HUB_MODEL_GATE",
                "errors": {}}
    try:
        return check(catalog=catalog)
    except Exception as e:  # noqa: BLE001 —— 情报字段不得把 /health 打挂
        return {"state": "unknown", "checked": False, "ok": False,
                "total": 0, "catalog_size": 0, "missing": {},
                "unknown_prefix": {}, "error": "%s: %s" % (type(e).__name__, str(e)[:120]),
                "errors": {}}


def log_summary(res: dict, log) -> None:
    """启动/巡检打一行结论。缺失项逐条列出 —— 只说「有 N 个缺失」等于没说。"""
    st = res.get("state")
    if st == "ok":
        log.info("[modelgate] 配置模型全部在册（%d 个，清单 %d 个）"
                 % (res.get("total", 0), res.get("catalog_size", 0)))
        return
    if st in ("unknown", "disabled"):
        log.warning("[modelgate] 不可判定（%s）⇒ 不判死，按设计只兑情报"
                    % (res.get("error") or st))
        return
    log.error("[modelgate] %d 个配置模型不在网关清单里（清单 %d 个）："
              % (len(res.get("missing") or {}), res.get("catalog_size", 0)))
    for label, mid in (res.get("missing") or {}).items():
        log.error("[modelgate]   %-22s %s" % (label, mid))
    if res.get("unknown_prefix"):
        log.warning("[modelgate] 另有 %d 个前缀未识别（可能走别的 provider，不判死）：%s"
                    % (len(res["unknown_prefix"]), ", ".join(res["unknown_prefix"].values())))