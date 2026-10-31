"""模型落点有效性判定 / 漂移体检 / 修复建议（2026-10-31 从 modelcfg.py 拆出，B4 片2）。

只读：本模块**不写任何文件**（写盘全在 modelcfg_writers）。需要网关清单或 hub 侧
模型时惰性 import modelcfg —— 那两件事属网络与状态侧，放这里会让依赖成环。
"""
from __future__ import annotations

import json
import os
import time
import urllib.error
import urllib.request
from typing import Dict, List, Optional, Tuple

from modelcfg_shape import *  # noqa: F401,F403
from modelcfg_writers import *  # noqa: F401,F403
from modelcfg_writers import _now  # 私有名不在 star 里


def _mf():
    """惰性拿 modelcfg 模块（catalog_ids / hub_model / apply_model / _ccr_base）。"""
    import modelcfg as _m
    return _m

_CHAT_STATIC = {"claude", "jcode"}


def _has_terminal(agent_id: str) -> bool:
    try:
        import profiles
        p = profiles.get_profile(agent_id) or {}
        return bool(p.get("terminal"))
    except Exception:  # noqa: BLE001 —— 注册表读不到就按"无终端"保守处理
        return False


def _has_native_ui(agent_id: str) -> bool:
    try:
        import profiles
        return bool(profiles.ui_start_for(agent_id))
    except Exception:  # noqa: BLE001
        return False


def _chat_applies(agent_id: str) -> bool:
    if agent_id in _CHAT_STATIC:
        return True
    try:
        rows = db.query("SELECT port FROM custom_agents WHERE id=?", (agent_id,))
        return bool(rows and rows[0]["port"])
    except Exception:  # noqa: BLE001
        return False


def effective_state(agent_id: str) -> dict:
    """该 agent 此刻「实际会用什么模型、在哪些通道生效」。

    只读、不落笔。判据全部可断言，页面直接渲染，不再让用户猜。
    """
    want = _mf().hub_model(agent_id)
    spec = SPECS.get(agent_id)
    mode = WRITE_MODE.get(agent_id, "argv")
    cat = _mf().catalog_ids()
    bare = bare_model_id(agent_id, want) if want else ""
    catalog = {"checked": cat["checked"], "ok": None, "size": cat["size"]}
    if cat["checked"] and want:
        catalog["ok"] = bare in cat["ids"]

    has_term = _has_terminal(agent_id)
    argv = terminal_argv(agent_id, want) if want else []
    channels = {
        "term_new": {"applies": bool(argv) and has_term, "argv": argv},
        "term_resume": {"applies": bool(argv) and has_term, "argv": argv},
        "chat": {"applies": _chat_applies(agent_id) and bool(want),
                 "note": "hub 对话/协同子任务/定时任务经 chat_model 取值" if _chat_applies(agent_id)
                         else "该 agent 无 hub 聊天适配器（对话走它自己的界面）"},
        # 原生界面（Web）启动命令不带 --model：能生效的唯一通路是配置文件写入。
        "ui_native": {"applies": _has_native_ui(agent_id) and bool(want) and mode in ("ccr", "native"),
                      "note": "原生界面按配置文件取值；argv-only 型在这里不生效"},
    }

    warnings: List[str] = []
    if not want:
        warnings.append("hub 侧未设置：不注入 --model，各 agent 使用自己配置文件里的默认值。")
    else:
        if cat["checked"] and catalog["ok"] is False:
            warnings.append(
                f"模型 {bare} 不在 CCR 当前清单（{cat['size']} 条）中：新开终端仍会注入它，"
                f"上游会报 unknown model 或自行回落 —— 请重新选一个在册模型。")
        if has_term and not argv:
            warnings.append("该 agent 没有 --model 白名单：hub 拉起终端不会注入，只写配置文件。")
        if not has_term and mode == "argv":
            warnings.append("该 agent 无终端入口、也无配置文件可落 ⇒ 这个设置不会生效。")
        if mode == "argv" and has_term:
            warnings.append("argv-only：只对 hub 拉起的终端生效；别处直接启动不受影响。")
        if mode == "native":
            warnings.append("本家原生名：CCR 模型不入该字段，CCR 侧只经终端 --model 注入生效。")
    try:
        st = spec["read"]() if spec else {}
    except ModelCfgError:
        st = {}
    cur = str(st.get("current") or "")
    drift = None
    if want and cur and agent_id not in DRIFT_EXEMPT and cur != want:
        drift = {"file": cur, "hub": want}
        warnings.append(f"配置文件现值 {cur} ≠ hub 值 {want}（漂移；巡检会按 hub 值写回）。")

    return {"id": agent_id, "model": want, "source": "hub" if want else "none",
            "write_mode": mode, "file_current": cur, "bare_model": bare,
            "catalog": catalog, "channels": channels, "drift": drift,
            "warnings": warnings}


def _live_probe(model: str) -> dict:
    """真打一次 CCR（max_tokens=1），用账本级的证据判「这个模型现在能不能用」。

    只在用户显式点「真实验证」时调用 —— 付费模型会产生极小额的按量计费，
    所以 HTTP 层对它要求口令（见 settings_model_verify）。"""
    t0 = time.monotonic()
    body = json.dumps({"model": model, "messages": [{"role": "user", "content": "ping"}],
                       "max_tokens": 1, "stream": False}).encode()
    req = urllib.request.Request(
        _mf()._ccr_base() + "/v1/chat/completions", data=body, method="POST",
        headers={"Content-Type": "application/json",
                 "Authorization": "Bearer " + _ccr_api_key()})
    try:
        with urllib.request.urlopen(req, timeout=20) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return {"ok": True, "status": r.status, "served_model": str(data.get("model") or ""),
                "ms": int((time.monotonic() - t0) * 1000)}
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "status": e.code, "error": detail,
                "ms": int((time.monotonic() - t0) * 1000)}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "status": 0, "error": f"{type(e).__name__}: {str(e)[:200]}",
                "ms": int((time.monotonic() - t0) * 1000)}


def verify_model(agent_id: str, live: bool = False) -> dict:
    """「设置好了能真正有效」的自证：把四条判据逐条算成 ok/na 并给结论。

    判据（与 effective_state 同源，不另算一套）：
      ① 落点：配置文件现值 == hub 值（argv-only 型为 na）
      ② 清单：模型仍在 CCR 在册清单里（取不到清单 = unknown，不判死）
      ③ 注入：hub 拉起终端时确实会追加 argv（无终端入口的 agent 为 na）
      ④ 通道：至少有一条通道会用到这个值
    `live=True` 额外真打一次网关（付费模型会计费，故 HTTP 层要求口令）。
    """
    eff = effective_state(agent_id)
    want = eff["model"]
    mode = eff["write_mode"]
    checks: List[dict] = []

    def add(name: str, ok, detail: str = "") -> None:
        checks.append({"name": name, "ok": ok, "detail": detail})

    if not want:
        add("hub 侧取值", None, "未设置模型：不会有任何注入（agent 用自己的默认值）")
    else:
        add("hub 侧取值", True, want)

    if mode == "argv":
        add("配置文件落点", None, "argv-only：无配置文件可写")
    elif not want:
        add("配置文件落点", None, "无目标值")
    else:
        ok = (eff["file_current"] == want)
        add("配置文件落点", ok,
            (f"{eff['file_current'] or '（空）'} == {want}") if ok
            else f"文件里是 {eff['file_current'] or '（空）'}，与 hub 值不一致")

    cat = eff["catalog"]
    add("CCR 清单", (None if (not cat["checked"] or not want) else cat["ok"]),
        "未设置" if not want
        else "清单不可用（网关未响应/无凭据），不判死" if not cat["checked"]
        else (f"{eff['bare_model']} 在册（{cat['size']} 条）" if cat["ok"]
              else f"{eff['bare_model']} 不在 {cat['size']} 条清单内"))

    term_ok = eff["channels"]["term_new"]["applies"]
    add("终端注入", (term_ok if _has_terminal(agent_id) else None) if want else None,
        (" ".join(eff["channels"]["term_new"]["argv"]) if term_ok
         else "无 --model 白名单" if _has_terminal(agent_id) else "该 agent 无终端入口")
        if want else "未设置")

    any_chan = any(v.get("applies") for v in eff["channels"].values())
    add("生效通道", any_chan if want else None,
        "、".join(k for k, v in eff["channels"].items() if v.get("applies"))
        or ("无任何通道会用到该值" if want else "未设置"))

    if live and want:
        probe = _live_probe(bare_model_id(agent_id, want))
        add("真实调用", probe["ok"],
            (f"HTTP {probe['status']}，返回模型 {probe.get('served_model') or '（未回显）'}，"
             f"{probe['ms']}ms") if probe["ok"]
            else f"HTTP {probe['status']}：{probe.get('error') or ''}")
    elif live:
        add("真实调用", None, "无目标模型")

    # 结论：只有明确的 False 才算不通过；None（不适用/取不到）不阻断。
    # 没设过模型不算"验证失败"——那是"未配置"，页面文案必须能区分，否则每个没设置的
    # agent 都挂着红叉，红叉就不再是信号。
    if not want:
        verdict = "unset"
    else:
        verdict = "ok" if all(c["ok"] is not False for c in checks) else "fail"
    return {"agent_id": agent_id, "verdict": verdict, "model": want,
            "checks": checks, "warnings": eff["warnings"],
            "channels": eff["channels"], "at": _now()}


def drift_report() -> List[dict]:
    """只读体检：列出「配置文件现值 ≠ hub 持久化值」的 agent（**不落笔**）。

    为什么要这个（09-28 实取证）：CCR 每次启动都会重写 ~/.claude/settings.json 的
    env.ANTHROPIC_MODEL / CCR_CLAUDE_CODE_MODEL / CODEXL_CLAUDE_CODE_MODEL 三兄弟
    （07:06:28 把 agnes 改回 alibaba/qwen3.8-flash[1m]，顶层 `model` 反而不动），
    ⇒ 凡是**不带 --model** 的 claude 启动（续聊、用户在别处直接敲 claude）重启后
    就退回旧模型 —— 这就是用户报的「重启之前是对的、重启之后又不对」。写回配置
    属共享配置写入（受保护面，须用户授权），这里先把漂移摆到明面上。"""
    out: List[dict] = []
    for aid in SPECS:
        want = _mf().hub_model(aid)
        if not want:
            continue
        if aid in DRIFT_EXEMPT:
            continue                  # 见 DRIFT_EXEMPT：形状不可比，比了是恒假的警报
        try:
            st = SPECS[aid]["read"]()
        except ModelCfgError as e:
            out.append({"id": aid, "hub_model": want, "current": "",
                        "extra": {}, "files": [], "note": e.args[0]})
            continue
        owned = {"current": str(st.get("current") or "")}
        for k, v in (st.get("extra") or {}).items():
            owned[str(k)] = str(v)
        # 判据是「**任一**落点与持久化值不一致就算漂移」，而不是"还剩下某个落点对得上就算没事"：
        # CCR 重启只改 env 三兄弟、顶层 `model` 原样留着 —— 而真正决定这次调用用哪个模型的
        # 恰好是被改掉的 env。所以这里比对的是**全部**落点，并把不一致的那几个摆出来。
        bad = {k: v for k, v in owned.items() if v and v != want}
        if bad:
            out.append({"id": aid, "hub_model": want,
                        "current": str(st.get("current") or ""),
                        "extra": st.get("extra") or {}, "owned": bad,
                        "files": st.get("files") or []})
    return out


def repair_drift(only: Optional[List[str]] = None, dry_run: bool = False) -> dict:
    """把漂移的 agent 配置文件写回 hub 持久化值（用户 09-28 授权，默认开启）。

    为什么需要它：CCR 每次启动都会重写 ~/.claude/settings.json 的 env 三兄弟
    （今早 07:06:28 实测把 agnes 改回 qwen），凡是**不带 --model** 的启动就退回旧
    模型 —— 这就是「设置里是 Agens、重启之后又变 qwen」。终端注入只覆盖 hub 拉起的
    会话，用户在别处直接敲 claude 覆盖不到，所以要按持久化值把文件修回去。

    三条护栏（共享配置写入铁律）：
      ① 落笔前 `_backup()` 时间戳备份（apply_model 内自带，无法绕过）；
      ② 文件不存在 / 不可写 / 模型 ID 形状不合法 ⇒ **跳过并记原因**，绝不硬写；
      ③ `dry_run=True` 只算不做，给体检与自测用。
    返回 {"repaired": [...], "skipped": [...]}，每一项都带得出来证据的字段。"""
    out: dict = {"repaired": [], "skipped": []}
    for d in drift_report():
        aid = d["id"]
        if only is not None and aid not in only:
            continue
        want = d["hub_model"]
        files = d.get("files") or []
        if not files or not all(Path(f).exists() for f in files):
            out["skipped"].append({"id": aid, "reason": "配置文件缺失"})
            continue
        unwritable = [f for f in files if not os.access(f, os.W_OK)]
        if unwritable:
            out["skipped"].append({"id": aid, "reason": "文件不可写（常见 chattr +i）",
                                   "files": unwritable})
            continue
        try:
            validate_model(want)
        except ModelCfgError as e:
            out["skipped"].append({"id": aid, "reason": f"持久化模型 ID 不合法：{e.args[0]}"})
            continue
        if dry_run:
            out["repaired"].append({"id": aid, "model": want, "dry_run": True,
                                    "owned": d.get("owned") or {}})
            continue
        try:
            res = _mf().apply_model(aid, want)
        except ModelCfgError as e:
            out["skipped"].append({"id": aid, "reason": f"写回失败：{e.args[0]}"})
            continue
        out["repaired"].append({"id": aid, "model": want,
                                "backups": [a["backup"] for a in res["applied"]],
                                "owned": d.get("owned") or {}})
    return out



