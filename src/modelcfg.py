"""各 Agent 默认模型的统一设置后端（v0.13.41：设置 → 模型子菜单）。

口径（2026-09-27 用户裁定，三条）：
  ① **双写**：Hub 侧落 per-agent 模型（拉起终端时按白名单注入 --model）+ 同时写入
     该 agent **自己的**配置文件；
  ② **CCR 网关自身的 Router（default/think/longContext/webSearch/background）
     只读展示、不写** —— 它属三方互斥军规保护面，且 ccr 运行时写 config.json
     会被运行态覆盖（fileConfig 只是落盘投影）；
  ③ 写前预览 + 口令 + 时间戳备份三件套（军规铁律：无备份禁止写入）。

为什么不做"通用配置文件编辑器"：每家 agent 的模型落点形状都不同（json / toml /
yaml，pi 还要顺带补模型清单、grok 要建命名 profile），逐一白名单化才给得出
**可读的 diff** 与**可回滚的落点**。一个通用写手必然退化成"整文件重写"，那正是
09-07 pi 改 CCR 配置时把同文件其它 profile 一起改坏的同款事故面。

落点（本机 09-27 实测，与官方文档互证）：
  claude  ~/.claude/settings.json     model + env.{ANTHROPIC_MODEL,CCR_CLAUDE_CODE_MODEL,CODEXL_CLAUDE_CODE_MODEL}
  jcode   ~/.jcode/config.toml        [provider].default_model（+ [providers.ccr].default_model 若存在）
  codex   ~/.codex/config.toml        CCR 托管块内 model = "..."
  pi      ~/.pi/agent/settings.json   defaultModel / defaultProvider（模型不在清单时补进 models.json）
  grok    ~/.grok/config.toml         [model.ccr-hub] 块 + [models].default
  hermes  ~/.hermes/config.yaml       model.default / model.provider
  codebuddy（WorkBuddy 包内 CLI）    **不支持**：--model 只认自有清单
          （hy4-preview / hy3 …，实测 help 输出），与 CCR 的 provider/model ID 不通用。

agent 清单怎么来（2026-10-25 用户令「应该是自动发现并添加」）：
  ① SPECS/UNSUPPORTED 两张硬编码表（逐家实测的读写器与理由）；
  ② **注册表自动发现**：profiles.all_profiles() 里在册的 agent 若两张表都没登记
     （如将来新接入的 agent），以「未接入」+ 如实原因出现在设置页，置灰 ——
     宁可显示「设不了 + 为什么」，也不让用户在页面上找不到这个 agent。
  已按此登记：omp（argv-only，见 _omp_read）、dsh（UNSUPPORTED，TUI 无 --model）。
"""
from __future__ import annotations

import hmac
import json
import logging
import os
import re
import shutil
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import db
import jsonspan

log = logging.getLogger(__name__)

# ── 模型 ID 形状闸门 ────────────────────────────────────────────────
# CCR /v1/models 实测形状：alibaba/qwen3.8-max、openrouter/poolside/laguna-xs-2.1:free。
# 收紧到这个字符集：模型 ID 会被写进 JSON/TOML 的**双引号字符串**与终端 argv，
from modelcfg_shape import *  # noqa: F401,F403
from modelcfg_writers import _toml_get  # 兼容旧调用方（测试直接打这个私有名） —— 对外仍从 modelcfg 取这些名字
from modelcfg_writers import *  # noqa: F401,F403
from modelcfg_writers import _atomic_write, _backup, _home, _now, _read_text  # 私有名
from modelcfg_verify import *  # noqa: F401,F403

# 放宽字符集等于给引号逃逸与 argv 注入开门。


def _ccr_api_key() -> str:
    """CCR 的本地 key 只从网关配置里现读，不在本文件里硬编码任何凭据。

    grok 的 [model.*] 块必须带 api_key 才能打 CCR；优先复用文件里**已有的** CCR
    块（那把 key 是本机已在用的），没有才回落到网关凭据，再没有就留空并在 changes
    里如实标注（宁可让用户自己补，也不猜一个假 key）。

    ⚠ 2026-10-27 修：旧实现只读 `~/.claude-code-router/config.json`，而本机 CCR 已迁
    到 `config.sqlite`，该文件不存在 ⇒ 恒返回 ""。表现是「grok 的块里 api_key 被写成
    空串」——写入成功、调用 401。现按 config.py 的同一条解析链取值（与网关客户端同源，
    不新增凭据）：CCR_OPENAI_KEY → ~/.config/jcode/provider-ccr.env → CCR_AUTH_TOKEN。"""
    env_key = os.getenv("CCR_OPENAI_KEY", "")
    if env_key:
        return env_key
    p = _home() / ".config" / "jcode" / "provider-ccr.env"
    try:
        m = re.search(r"JCODE_PROVIDER_CCR_API_KEY=(\S+)", p.read_text(encoding="utf-8"))
        if m:
            return m.group(1)
    except OSError:
        pass
    if os.getenv("CCR_AUTH_TOKEN"):
        return os.getenv("CCR_AUTH_TOKEN", "")
    try:
        d = json.loads((_home() / ".claude-code-router" / "config.json").read_text(encoding="utf-8"))
        return str(d.get("APIKEY") or "")
    except Exception:  # noqa: BLE001
        return ""


# ── CCR 模型清单（执行逻辑的判据源）──────────────────────────────────
# 为什么设置页需要它：`agent_models` 里存的是**写入当时**的模型 ID，而网关清单会变
# （人工增删模型、免费池轮换）。设置页只证明"写进去了"，证明不了"这个 ID 现在还在"。
# 存着一个已经不在清单里的 ID ⇒ 新开终端照旧注入它，agent 报 unknown model 或上游
# 回落，用户看到的就是"设置好了没用"。本节的 catalog_* 就是补这条。
_CATALOG = {"ts": 0.0, "ids": set(), "checked": False, "size": 0, "error": ""}
CATALOG_TTL_S = float(os.getenv("HUB_MODEL_CATALOG_TTL", "60"))


def _ccr_base() -> str:
    return os.getenv("CLAUDE_CCR_URL", "http://127.0.0.1:3456").rstrip("/")


def _fetch_catalog_ids() -> Tuple[set, str]:
    """真去网关拉一次清单。返回 (ids, error)。抽成独立函数是为了让 L0 用例能打桩
    —— 单测若真打 :3456，既慢又依赖网关活着（本仓最贵的假绿灯形态）。"""
    try:
        req = urllib.request.Request(
            _ccr_base() + "/v1/models",
            headers={"Authorization": "Bearer " + _ccr_api_key()})
        with urllib.request.urlopen(req, timeout=4) as r:
            data = json.loads(r.read().decode("utf-8", "replace"))
        return {str(m.get("id")) for m in (data.get("data") or []) if m.get("id")}, ""
    except Exception as e:  # noqa: BLE001 —— 取不到只影响判断，不许打断设置页
        return set(), f"{type(e).__name__}: {str(e)[:120]}"


def catalog_ids(force: bool = False) -> dict:
    """CCR 当前模型清单（同步、带 TTL 缓存）。

    返回 `{"checked": bool, "ids": set, "size": int, "error": str}`。
    `checked=False` 表示**取不到**（网关挂着/无凭据），调用方必须把"取不到"与
    "清单里没有"分开：前者不能判死，后者才是真问题（与 intake 的 count=0 口径同源）。
    `HUB_MODEL_CATALOG=off` 可整条关掉（L0 单测用，生产不要关）。"""
    if os.getenv("HUB_MODEL_CATALOG", "").lower() in ("0", "off", "false", "no"):
        return {"ts": 0.0, "ids": set(), "checked": False, "size": 0,
                "error": "disabled by HUB_MODEL_CATALOG"}
    now = time.monotonic()
    if not force and _CATALOG["checked"] and (now - _CATALOG["ts"]) < CATALOG_TTL_S:
        return _CATALOG
    ids, err = _fetch_catalog_ids()
    _CATALOG.update({"ts": now, "ids": ids, "checked": bool(ids), "size": len(ids),
                     "error": err})
    return _CATALOG




# ── Hub 侧 per-agent 模型（拉起终端时注入用）────────────────────────
def _ensure_table() -> None:
    db.execute("CREATE TABLE IF NOT EXISTS agent_models ("
               "agent_id TEXT PRIMARY KEY, model TEXT NOT NULL, updated_at TEXT NOT NULL)")


def hub_model(agent_id: str) -> str:
    try:
        _ensure_table()
        rows = db.query("SELECT model FROM agent_models WHERE agent_id=?", (agent_id,))
    except Exception:  # noqa: BLE001 —— 表没建好只影响注入，不许拖垮设置页
        return ""
    return str(rows[0]["model"]) if rows else ""


def set_hub_model(agent_id: str, model: str) -> None:
    _ensure_table()
    db.execute("INSERT INTO agent_models(agent_id, model, updated_at) VALUES(?,?,?) "
               "ON CONFLICT(agent_id) DO UPDATE SET model=excluded.model, updated_at=excluded.updated_at",
               (agent_id, model, _now()))


def clear_hub_model(agent_id: str) -> None:
    """撤掉 hub 侧默认值（2026-10-07）——**删行，不写空串**。

    为什么要它：设置页有「默认（网关路由）」这个选项，但保存路径只认非空 model
    ⇒ 用户选它会被 400 拒（`validate_model("")` 抛"模型 ID 为空"），而前端又在
    自动补预览处静默返回 —— 表现就是**点了保存什么都不发生**。
    语义上"恢复默认"＝「不再由 hub 注入 --model」＝删掉这行持久化值，
    而不是存一个空字符串（存空串会让 `drift_report` 的 `if not want: continue` 与
    `chat_model` 的回退都走到另一条分支，属隐式歧义）。"""
    _ensure_table()
    db.execute("DELETE FROM agent_models WHERE agent_id=?", (agent_id,))


#: 原生字段与 CCR 模型 ID 形状不同、**不能互比**的 agent（2026-10-07）。
#: qoder 的 `model.name` 收的是本家原生名（`qwencli --list-models` 实测 Qwen3.8-Max /
#: Qwen3.8-Flash），CCR 的 `alibaba/xxx` 塞进去不被接受（`_qoder_write` 硬拒）。
#: ⇒ 它的「hub 持久化值（CCR ID）」与「文件现值（原生名）」本来就**必然不等**，
#: 拿它们比会**恒报漂移**，而 `repair_drift` 又会去写一个 qoder 不认的值 ⇒ 假警报 + 真风险。
#: 这类 agent 的漂移比对跳过，由设置页文案说明「CCR 模型只经 --model 注入生效」。


def chat_model(agent_id: str, requested: Optional[str] = None) -> str:
    """对话 / 任务通道（`POST /api/agents/{id}/chat`）的模型取值。

    v0.13.50：这条通道以前**压根不知道 hub 侧的持久化模型** —— adapter 的
    default_model 是**服务启动时**由 config 算出来的常量（.env 未配 CLAUDE_CHAT_MODEL
    就写死 "qwen3.8-flash"），于是设置页改了模型，协同子任务 / 定时任务 / 对话页
    照旧把旧模型发出去（09-28 实弹：POST /api/agents/claude/chat 打给 CCR 的仍是
    qwen3.8-flash，而 agent_models 里存的是 agnes/agnes-2.5-flash）。
    取值顺序：请求显式带 > 本 Agent 的持久化默认值 > adapter 自兜。"""
    req = (requested or "").strip()
    if req:
        return req
    return hub_model(agent_id)


# ── 「设置好了到底有没有效」：三通道 × 清单的单一判定 ────────────────
# 2026-10-27（用户令「重点优化执行逻辑，要设置好了能真正有效」）。
# 病根：保存成功只证明**写进去了**，证明不了**会用上**。四条静默失效路径各自有案例：
#   ① 存了个已经不在 CCR 清单里的 ID ⇒ 终端注入死 ID，报 unknown model；
#   ② agent 没有 --model 白名单 ⇒ 只写配置文件，而该文件可能不是它真正的取值源；
#   ③ argv-only（omp）没有配置文件 ⇒ 只要不走 hub 终端就完全不生效；
#   ④ 配置文件被别的进程改回去（CCR 重写 claude env 三兄弟）⇒ 只有巡检那一轮能修。
# 本节的 effective_state/verify_model 把"会用什么、从哪来、哪条通道生效、有没有风险"
# 一次算齐，供设置页直接展示，而不是让用户靠"改了试试看"去猜。

#: 有静态聊天适配器、且**适配器真的会把 model 传下去**的 agent。
#: ⚠ 只列 claude/jcode：这两家用 OpenAICompatAdapter（body["model"] = 入参）。
#: pi 自 v0.13.175 起是 CLI 适配器（`pi -p --model`），模型在**派发/对话调用时**才注入，
#: 与「设置页改完落盘、下次启动由 CLI 自己读」不是同一条通道 —— 列进来会让人以为
#: 设置页那一栏也管它。tdai 是记忆后端，不在本页范围。


# ── 对外：状态 / 预览 / 落笔 ────────────────────────────────────────
def _registry_agent_ids() -> List[str]:
    """hub 注册表里在册的 agent id（静态画像 + 动态 CLI 发现）。

    为什么设置页要合并它（2026-10-25 用户报障「模型设置页面也没有自动发现并添加
    dsh omp 等agent，应该是自动发现并添加」）：本页原先只列 SPECS+UNSUPPORTED 两张
    硬编码表 ⇒ 画像里新登记的 agent（dsh 10-23、omp 10-24）在设置页**零存在感**，
    用户既设不了、也看不到为什么设不了。判据：注册表在册 ⇒ 必须出现在本页
    （可设置，或带实测理由的置灰）。动态发现自带 30s 缓存，成本可接受。"""
    try:
        import profiles
        ids = [p["id"] for p in profiles.all_profiles() if p.get("kind") == "agent"]
    except Exception:  # noqa: BLE001 —— 注册表读取失败不能把设置页搞垮
        ids = []
    # v0.13.155：扫描登记进 custom_agents 的 agent 也要出现（否则「菜单有了、
    # 模型页没有」—— 这正是 2026-10-25 用户报障的第二半）。
    try:
        rows = db.query("SELECT id FROM custom_agents "
                        "WHERE COALESCE(kind,'service')='agent'")
        ids += [str(r["id"]) for r in rows]
    except Exception:  # noqa: BLE001
        pass
    return list(dict.fromkeys(ids))


def agent_state(agent_id: str) -> dict:
    spec = SPECS.get(agent_id)
    if not spec:
        reason = UNSUPPORTED.get(agent_id)
        if not reason:
            # 注册表在册但两张表都没登记 ⇒ 自动发现的新 agent：如实说「未接入」，
            # 不假装能设、也不假装不存在（2026-10-25 用户令「自动发现并添加」）。
            reason = ("未接入模型设置：注册表自动发现的 agent（尚无配置文件读写器、"
                      "CLI 未实测 --model 支持）") \
                if agent_id in set(_registry_agent_ids()) else "不在模型设置白名单内"
        return {"id": agent_id, "supported": False, "writable": False,
                "reason": reason,
                # write_mode 统一给 "argv" 之外的语义：这两个 agent 连注入都不做，
                # 故用 "none"，前端据此不显示"字段认什么"那行（没有字段可认）。
                "write_mode": "none",
                "value_prefix": "", "models": [],
                "current": "", "hub_model": hub_model(agent_id)}
    try:
        st = spec["read"]()
    except ModelCfgError as e:
        st = {"current": "", "files": [], "note": e.args[0]}
    files = st.get("files") or []
    # argv_only（omp）：没有配置文件可落，「writable」的语义是「hub 侧可设置」
    # （落点 = 拉起终端时的 --model 注入），不是「配置文件可写」。
    argv_only = bool(spec.get("argv_only"))
    # 真实会追加的 argv（grok 注入的是 profile 表头键 `ccr-hub`，不是模型 ID）——
    # 页面此前只显示 flag 名 `--model`，用户以为会注入 `--model <上面那个 ID>`，
    # 与实际差一层。这里给出**实际命令**，避免"设置页说一套、终端跑另一套"。
    want = hub_model(agent_id)
    return {"id": agent_id, "name": spec["name"], "supported": True,
            "writable": argv_only or (bool(files) and all(Path(f).exists() for f in files)),
            "current": st.get("current", ""), "provider": st.get("provider", ""),
            "extra": st.get("extra", {}), "note": st.get("note", ""),
            "files": files, "argv": MODEL_ARGV.get(agent_id, ""),
            "argv_effective": terminal_argv(agent_id, want) if want else [],
            "effective": effective_state(agent_id),
            # v0.13.86：设置页拿这两列才能摆出"点了真能写进去"的选项 ——
            # value_prefix 让选项值等于该 agent 字段真正要的形状（opencode 的 ccr/），
            # models 让只认原生名的 agent（qoder）摆原生清单而不是必然被拒的 CCR 清单。
            "value_prefix": VALUE_PREFIX.get(agent_id, ""),
            "models": NATIVE_MODELS.get(agent_id, []),
            # v0.13.85：把「这个 agent 的模型字段认什么」摆到设置页，否则用户会把
            # 原生名/CCR ID/只有注入三条路混着用，改了不生效却看上去"保存成功"。
            "write_mode": WRITE_MODE.get(agent_id, "argv"),
            # v0.13.181：上次落笔的时间（有值 ⇒ 页面上「撤销上次保存」才有东西可撤）
            "undo_at": (last_write(agent_id) or {}).get("at", ""),
            "hub_model": hub_model(agent_id)}


def list_agents() -> List[dict]:
    """SPECS（可设置）+ UNSUPPORTED（置灰带理由）+ 注册表自动发现（2026-10-25）。

    顺序即页面顺序：先两张硬编码表（按加入序），再注册表里漏掉的新 agent。
    重复 id 去重（静态表优先）——qoder 等在册且已登记的不出现两次。"""
    ids = list(SPECS.keys()) + list(UNSUPPORTED.keys())
    for aid in _registry_agent_ids():
        if aid not in ids:
            ids.append(aid)
    return [agent_state(i) for i in ids]


def ccr_router_view() -> dict:
    """CCR Router 五场景：只读（军规：不写网关配置）。"""
    p = _home() / ".claude-code-router" / "config.json"
    out = {"file": str(p), "router": {}, "fallback": {}, "read_only": True}
    if not p.exists():
        out["note"] = "未找到 ~/.claude-code-router/config.json"
        return out
    try:
        d = json.loads(_read_text(p))
    except json.JSONDecodeError as e:
        out["note"] = f"config.json 解析失败：{e}"
        return out
    out["router"] = {k: str(v) for k, v in (d.get("Router") or {}).items()}
    out["fallback"] = {k: list(v or []) for k, v in (d.get("fallback") or {}).items()}
    return out


def _spec_or_raise(agent_id: str) -> dict:
    spec = SPECS.get(agent_id)
    if not spec:
        raise ModelCfgError(UNSUPPORTED.get(agent_id, f"{agent_id} 不在模型设置白名单内"), 400)
    return spec


def preview_clear(agent_id: str) -> dict:
    """「默认（网关路由）」的预览：**不写任何文件**，只撤销 hub 侧的注入。

    为什么清空不去改 agent 配置文件（2026-10-07 定）：我们无法知道该文件
    「原本」是什么值 —— hub 首次落笔时覆盖掉的就是用户原先的默认模型，而那个值
    只存在于时间戳备份里，不保证还在（可被清理）。凭猜测往用户配置里写一个"默认"
    是**制造**错误而非修复。故清空的语义严格限定为：「hub 不再注入 --model」。
    设置页必须把这句原样说出来，否则用户会以为"配置也被还原了"。"""
    spec = _spec_or_raise(agent_id)
    return {"agent_id": agent_id, "model": "", "mode": "clear",
            "write_mode": WRITE_MODE.get(agent_id, "argv"),
            "value_prefix": VALUE_PREFIX.get(agent_id, ""),
            "models": NATIVE_MODELS.get(agent_id, []),
            "hub_model_from": hub_model(agent_id), "hub_model_to": "",
            "argv": [], "files": [],
            "note": f"只撤销 hub 侧默认模型，不再向 {spec['name']} 注入 --model；"
                    f"该 agent 自己的配置文件保持现值不变（不猜测、不回写）。"}


def clear_model(agent_id: str) -> dict:
    """落笔版的清空：删 hub 持久化行。不碰任何配置文件，故无需备份。"""
    spec = _spec_or_raise(agent_id)
    was = hub_model(agent_id)
    clear_hub_model(agent_id)
    return {"agent_id": agent_id, "model": "", "mode": "clear", "applied": [],
            "hub_model_from": was, "hub_model_to": "", "argv": [], "at": _now(),
            "note": f"已撤销 hub 侧默认模型（原 {was or '（无）'}）；"
                    f"{spec['name']} 的配置文件未改动。"}


def preview(agent_id: str, model: str) -> dict:
    """写前预览：只算 diff，落零字节。

    `model == ""` ⇒ **清空语义**（对应设置页的「默认（网关路由）」），只撤 hub 侧注入，
    不动任何 agent 配置文件（理由见 clear_model）。"""
    if not (model or "").strip():
        return preview_clear(agent_id)
    model = validate_model(model)
    spec = SPECS.get(agent_id)
    if not spec:
        raise ModelCfgError(UNSUPPORTED.get(agent_id, f"{agent_id} 不在模型设置白名单内"), 400)
    try:
        writes = spec["write"](model)
    except ModelCfgError:
        raise
    except Exception as e:  # noqa: BLE001
        raise ModelCfgError(f"生成变更失败：{e}", 500) from e
    files = []
    for p, _new, changes in writes:
        files.append({"file": str(p), "exists": p.exists(),
                      # v0.13.44：预览就把"能不能写"摆出来（09-27 报障：pi 的
                      # settings.json 被 chattr +i 设了不可变，保存一路走到 500，
                      # 用户只看到一句 4 秒就消失的 toast ⇒ 观感「保存不生效」）。
                      "writable": os.access(p, os.W_OK),
                      "backup": str(p) + ".bak-<时间戳>-hub-modelcfg-" + agent_id,
                      "changes": [c for c in changes if c.get("op") != "skip"]})
    return {"agent_id": agent_id, "model": model, "mode": "set",
            "hub_model_from": hub_model(agent_id), "hub_model_to": model,
            "write_mode": WRITE_MODE.get(agent_id, "argv"),
            "value_prefix": VALUE_PREFIX.get(agent_id, ""),
            "models": NATIVE_MODELS.get(agent_id, []),
            "argv": terminal_argv(agent_id, model), "files": files}


def apply_model(agent_id: str, model: str) -> dict:
    """落笔：先备份 → 再写 → 再存 Hub 侧。任一步失败即抛错（不半写）。"""
    if not (model or "").strip():
        return clear_model(agent_id)
    model = validate_model(model)
    spec = _spec_or_raise(agent_id)
    # ⚠ 顺序要紧：**先让写手做它自己的形状校验，再问清单**。
    # 反过来的话，opencode 的「缺 provider 前缀」会被清单闸门抢先报成 409
    # （它自己的 400 更精确，且带"正确形状长什么样"）。写手是纯函数，不落盘。
    writes = spec["write"](model)
    # 2026-10-27：写入前再问一句「这个 ID 现在还活着吗」。
    # 只拦**明确不在册**的情况；网关取不到清单时放行（不能把"取不到"当成"没有"）。
    # 这条挡的是最常见的"设置好了没用"：存进去的是早已下线的模型 ID，终端照旧注入，
    # agent 报 unknown model —— 而设置页显示"已生效"。
    cat = catalog_ids(force=True)
    if cat["checked"] and WRITE_MODE.get(agent_id) == "ccr":
        bare = bare_model_id(agent_id, model)
        if bare not in cat["ids"]:
            raise ModelCfgError(
                f"模型 {bare} 不在 CCR 当前清单（{cat['size']} 条）中，写下去只会让 agent "
                f"报 unknown model。请从下拉里重新选一个在册模型；"
                f"若刚在网关侧新增，等 {int(CATALOG_TTL_S)}s 后再试", 409)
    for p, _new, _c in writes:
        if not p.exists():
            raise ModelCfgError(f"配置文件不存在，拒绝新建：{p}", 404)
        # v0.13.44 预检：先问"能不能写"再落备份。顺序反过来的话（09-27 pi 事故面）
        # 会在不可写的文件旁留下一堆备份、再抛 500，用户既没改成也多出一堆垃圾。
        if not os.access(p, os.W_OK):
            raise ModelCfgError(
                f"配置文件不可写：{p}（常见原因是被设了不可变属性或只读挂载；"
                f"`lsattr {p}` 若见 i 标记，需 `chattr -i {p}` 后才能保存）", 409)
    backups = [_backup(p, agent_id) for p, _n, _c in writes]
    hub_from = hub_model(agent_id)
    applied = []
    try:
        for (p, new, changes), bak in zip(writes, backups):
            # 原子写（v0.13.181）：本行原先是 p.write_text(new, ...)，见 _atomic_write 注释。
            _atomic_write(p, new)
            applied.append({"file": str(p), "backup": bak,
                            "changes": [c for c in changes if c.get("op") != "skip"]})
    except OSError as e:  # noqa: BLE001
        # 半写止损：把已写的从备份还原回去，不让配置文件停在半截状态
        for (p, _n, _c), bak in zip(writes, backups):
            try:
                shutil.copy2(bak, p)
            except OSError:
                pass
        raise ModelCfgError(f"写入失败已回滚：{e}", 500) from e
    set_hub_model(agent_id, model)
    _record_write(agent_id, hub_from, applied)
    return {"agent_id": agent_id, "model": model, "mode": "set", "applied": applied,
            "write_mode": WRITE_MODE.get(agent_id, "argv"),
            "argv": terminal_argv(agent_id, model), "at": _now()}


# ── 撤销上一次写入（v0.13.181，借 magpie 的 disconnect 语义）──────────
# magpie 的卖点之一是「断开时把文件逐字节还回接入前的样子」。本机原来只有
# 「时间戳备份」——回滚要人自己去一目录里挑 .bak、判断哪份对、再手工 cp，
# 那一步在手机上根本做不了。这里把「刚写的那次」记账，一次点击还回去。
def _ensure_writes_table() -> None:
    db.execute("CREATE TABLE IF NOT EXISTS model_writes ("
               "agent_id TEXT PRIMARY KEY, at TEXT NOT NULL, "
               "hub_from TEXT NOT NULL, files TEXT NOT NULL)")


def _record_write(agent_id: str, hub_from: str, applied: List[dict]) -> None:
    """记下这次落笔的文件与备份路径（每个 agent 只留最近一次）。

    记账失败**不许**把已经成功的写入变成失败：写盘已经发生，报错只会让用户重试，
    而重试会再写一遍。故整段包 try。
    """
    try:
        _ensure_writes_table()
        db.execute("INSERT INTO model_writes(agent_id, at, hub_from, files) VALUES(?,?,?,?) "
                   "ON CONFLICT(agent_id) DO UPDATE SET at=excluded.at, "
                   "hub_from=excluded.hub_from, files=excluded.files",
                   (agent_id, _now(), hub_from or "",
                    json.dumps([{"file": a["file"], "backup": a["backup"]} for a in applied],
                               ensure_ascii=False)))
    except Exception:  # noqa: BLE001
        pass


def last_write(agent_id: str) -> Optional[dict]:
    try:
        _ensure_writes_table()
        rows = db.query("SELECT agent_id, at, hub_from, files FROM model_writes WHERE agent_id=?",
                        (agent_id,))
    except Exception:  # noqa: BLE001 —— 表没建好只影响撤销按钮
        return None
    return rows[0] if rows else None


def _undo_source(p: Path, backup: str) -> Path:
    """校验备份路径可安全用作还原源（防越界 + 防张冠李戴）。"""
    b = Path(backup)
    home = _home().resolve()
    try:
        rp = b.resolve()
    except OSError:
        raise ModelCfgError(f"备份路径不可解析：{backup}", 409) from None
    if home not in rp.parents:
        raise ModelCfgError(f"拒绝还原到 HOME 之外：{backup}", 409)
    if not rp.name.startswith(p.name + ".bak-"):
        raise ModelCfgError(f"备份文件名与目标不匹配：{rp.name} 不属于 {p.name}", 409)
    if not rp.exists():
        raise ModelCfgError(f"备份已不存在：{backup}（可能被清理）", 404)
    return rp


def undo_model(agent_id: str) -> dict:
    """把该 agent **最近一次** apply_model 的落点逐字节还原。

    三条边界：
      ① 只撤销 hub 自己记过账的那一次（文件与备份路径都取自 DB，不接收客户端传路径）；
      ② 撤销本身也是写入 ⇒ 先给对方现状做时间戳备份，撤错了还能再撤回去；
      ③ 清空（clear_model）不落盘，故不记账、也无从撤销 —— 由前端文案说明。
    """
    _spec_or_raise(agent_id)
    rec = last_write(agent_id)
    if not rec:
        raise ModelCfgError(f"{agent_id} 没有可撤销的写入记录"
                            f"（只记录经本页落过盘的成功写入）", 404)
    files = json.loads(rec.get("files") or "[]")
    if not files:
        raise ModelCfgError(f"{agent_id} 上次保存没有改动任何配置文件，无需撤销", 404)
    restored = []
    for f in files:
        p = Path(f["file"])
        src = _undo_source(p, f["backup"])
        if not p.exists():
            raise ModelCfgError(f"配置文件已不存在：{p}（撤销会新建它，需人工确认）", 409)
        _backup(p, f"undo-{agent_id}")      # 军规：撤销前也备份
        _atomic_write(p, src.read_text(encoding="utf-8"))
        restored.append({"file": str(p), "from_backup": str(src)})
    if rec.get("hub_from"):
        set_hub_model(agent_id, rec["hub_from"])
    else:
        clear_hub_model(agent_id)
    db.execute("DELETE FROM model_writes WHERE agent_id=?", (agent_id,))
    return {"agent_id": agent_id, "mode": "undo", "restored": restored,
            "hub_model_to": rec.get("hub_from") or "", "at": _now(),
            "note": "已按上次保存前的备份逐字节还原；清空操作不落盘，不在撤销范围。"}


# ── HTTP 层 ────────────────────────────────────────────────────────
# 三个端点两个读一个写。写端点自带 HUB_PASSCODE 校验（与 /api/settings/term-token
# 同口径：口令错 401、服务端没配口令 503 —— "没设口令"不等于"不用口令"），
# 并在 writeauth.EXEMPT_PREFIXES 里登记理由，避免"先要 token 又要口令"的双重门。
from fastapi import APIRouter, HTTPException, Request  # noqa: E402
from pydantic import BaseModel  # noqa: E402

router = APIRouter(tags=["modelcfg"])


class ApplyIn(BaseModel):
    agent_id: str
    model: str
    passcode: str = ""


def _check_passcode(pc: str, request) -> None:
    want = os.getenv("HUB_PASSCODE", "")
    if not want:
        raise HTTPException(503, "未设置 HUB_PASSCODE，模型设置已停用（fail-closed）")
    if not hmac.compare_digest(pc or "", want):
        ip = request.client.host if request and request.client else "?"
        log.warning(f"[settings] 模型设置口令错误：{ip}")
        raise HTTPException(401, "口令错误")


def _guard(e: ModelCfgError) -> HTTPException:
    return HTTPException(e.status, e.args[0])


@router.get("/api/settings/models")
async def settings_models():
    """设置→模型子菜单的首屏数据：各 agent 现值 + CCR Router 只读视图 + 漂移体检。

    v0.13.164：每个 agent 多带 `effective`（三通道生效判定 + 风险），顶层多带 `catalog`
    （清单是否取到、多少条）。页面据此渲染「设置好了到底有没有效」，而不是只报"已保存"。
    """
    cat = catalog_ids()
    return {"agents": list_agents(), "ccr": ccr_router_view(), "drift": drift_report(),
            "catalog": {"checked": cat["checked"], "size": cat["size"],
                        "error": cat["error"]}}


@router.get("/api/settings/model/verify")
async def settings_model_verify(agent_id: str):
    """「验证生效」（静态）：逐条判据自证，只读、不产生任何调用。

    真实调用见 `POST /api/settings/model/verify-live` —— 口令不能进 URL
    （会被访问日志/浏览器历史留存），故另立 POST 端点。"""
    try:
        return verify_model(agent_id, live=False)
    except ModelCfgError as e:
        raise _guard(e) from e


class VerifyLiveIn(BaseModel):
    agent_id: str
    passcode: str = ""


class UndoIn(BaseModel):
    agent_id: str
    passcode: str = ""


@router.post("/api/settings/model/verify-live")
async def settings_model_verify_live(body: VerifyLiveIn, request: Request):
    """「真实调用验证」：用当前模型真打一次 CCR（max_tokens=1）。

    会计费（付费模型按量）⇒ 与保存同口径要口令；口令走 POST body，不进 URL。"""
    _check_passcode(body.passcode, request)
    try:
        return verify_model(body.agent_id, live=True)
    except ModelCfgError as e:
        raise _guard(e) from e


@router.get("/api/settings/model/preview")
async def settings_model_preview(agent_id: str, model: str = ""):
    """`model` 允许为空（= 设置页的「默认（网关路由）」，清空语义，见 preview_clear）。

    2026-10-07 修：原先 `model: str` 是必填且空值会在 validate_model 里抛 400，
    而前端"保存自己补预览"那条路在拿不到 SET_DIFF 时**静默 return** ⇒
    用户选「默认」再点保存，看日志连一条请求都没有，表现就是"点了没反应"。"""
    try:
        return preview(agent_id, model)
    except ModelCfgError as e:
        raise _guard(e) from e


@router.post("/api/settings/model/apply")
async def settings_model_apply(body: ApplyIn, request: Request):
    _check_passcode(body.passcode, request)
    try:
        out = apply_model(body.agent_id, body.model)
    except ModelCfgError as e:
        raise _guard(e) from e
    act = "清空默认模型" if out.get("mode") == "clear" else f"设为 {body.model}"
    log.info(f"[settings] 模型设置落笔：{body.agent_id} → {act}")
    return out


@router.post("/api/settings/model/undo")
async def settings_model_undo(body: UndoIn, request: Request):
    """撤销上一次保存（v0.13.181）：把配置文件还原到那次保存前的字节。

    与 apply 同一把口令：它同样是写盘动作，且写的是 agent 自己的配置文件。
    """
    _check_passcode(body.passcode, request)
    try:
        out = undo_model(body.agent_id)
    except ModelCfgError as e:
        raise _guard(e) from e
    log.info(f"[settings] 撤销模型写入：{body.agent_id} → "
             f"{len(out['restored'])} 个文件已还原")
    return out
