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
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import db

# ── 模型 ID 形状闸门 ────────────────────────────────────────────────
# CCR /v1/models 实测形状：alibaba/qwen3.8-max、openrouter/poolside/laguna-xs-2.1:free。
# 收紧到这个字符集：模型 ID 会被写进 JSON/TOML 的**双引号字符串**与终端 argv，
# 放宽字符集等于给引号逃逸与 argv 注入开门。
MODEL_ID_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/+\-]{0,119}$")

#: 终端注入白名单（实测 `<cli> --help` 输出）：值 = 传给该 CLI 的 flag。
#: 只有这里在册的 agent 才会被 hub 拉起时追加 --model；其余仅写配置文件。
MODEL_ARGV: Dict[str, str] = {
    "claude": "--model",
    "jcode": "--model",
    "codex": "--model",
    "grok": "--model",
    "pi": "--model",
    "hermes": "-m",          # hermes help 只给短选项 [-m MODEL]
    # ── 2026-10-07 新增三家（逐家 `--help` 实测，不是猜的）──
    "opencode": "--model",   # opencode --help: -m, --model  model to use in the format of provider/model
    "qoder": "--model",      # qodercli --help: -m, --model <model>  Model for the current session
    "cursor": "--model",     # cursor-agent --help: --model <model>  Model to use (e.g. gpt-5, sonnet-4-thinking)
}

#: 写入模式（2026-10-07，用户裁定「原生 + 插入」）。三类语义不同，设置页必须分开说：
#:   "ccr"    —— 该字段**就是**给 CCR/OpenAI 兼容网关用的，可直写 CCR 的 `provider/model` ID。
#:   "native" —— 该字段只认**本家原生模型名**（或本家 ID 形状）。hub 可以写，但要写原生名；
#:               塞 CCR ID 会被 CLI 拒（qoder 实测），故 _qoder_write 里硬拒并给出替代路径。
#:   "argv"   —— **无默认配置文件可落**，只能靠 hub 拉起终端时的 `--model` 注入。
#:               设置页对这类要写明「只影响 hub 拉起的会话」，避免"改了没生效"的误解。
#: 这一列同时是设置页文案与测试判据的单一真相源。
WRITE_MODE: Dict[str, str] = {
    "claude": "ccr", "jcode": "ccr", "codex": "ccr", "pi": "ccr",
    "grok": "ccr", "hermes": "ccr",
    "opencode": "ccr", "cursor": "ccr",
    "qoder": "native",
}

#: 该 agent 的模型字段要求的**值前缀**（v0.13.86，2026-10-07）。
#: 为什么需要：CCR 清单（/api/models）给的是裸 ID（`alibaba/qwen3.8-max`），而
#: opencode 的 `model` / `--model` 收的是 `provider/model_id` 形状 —— 官方文档 Models 页
#: 原话「The format is `provider/model`」，本机同名 CLI 亦实测回 `ccr/<id>`（`opencode
#: models ccr` 逐行都是 `ccr/…`）。用户若照着清单点裸 ID，`_opencode_write` 会 409
#: 且指错方向（它说「没有 provider alibaba」，实际缺的是 `ccr/` 前缀）。
#: ⇒ 设置页据此把选项值拼成 `<前缀><CCR ID>`，让"能点到的"与"能写进的"是同一个东西。
VALUE_PREFIX: Dict[str, str] = {"opencode": "ccr/"}

#: 该 agent 的**原生模型清单**（v0.13.86）。取证：`qodercli --list-models` 实测输出
#: （2026-10-07）= Qwen3.8-Max / Qwen3.8-Flash；`~/.qoder/.models/default` 的 key
#: 亦为 `qmodel_38max`，与该清单同族。qoder 的 `model.name` **只认原生名**（塞 CCR ID
#: 会被 `_qoder_write` 硬拒）⇒ 设置页若只摆 CCR 清单，用户点任一 ID 都必然 400，
#: 属"看起来能选、实际必被拒"。故把原生清单透出来，且对 native 型 agent 不再摆 CCR 清单。
#: ⚠ 清单会随 qoder 发版变化：这里的提示语写明「以 qodercli --list-models 为准」，
#: 写入侧仍由 `_qoder_write` 复核形状（真源不在这张表）。
NATIVE_MODELS: Dict[str, List[str]] = {
    "qoder": ["Qwen3.8-Max", "Qwen3.8-Flash"],
}

#: pi 走 CCR 时用的 provider（~/.pi/agent/models.json 里 baseUrl = CCR 的那个）
PI_CCR_PROVIDER = "ccr-free"
#: grok 的命名 profile 键（[model.<key>] + [models].default 指向它）
GROK_PROFILE_KEY = "ccr-hub"
#: hermes 走 CCR 的 provider 名（config.yaml 顶层 model.provider）
HERMES_CCR_PROVIDER = "ccr-free"


class ModelCfgError(Exception):
    """带 HTTP 语义的配置错误（message 直接回给前端）。"""

    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _ccr_api_key() -> str:
    """CCR 的本地 key 只从网关配置里现读，不在本文件里硬编码任何凭据。

    grok 的 [model.*] 块必须带 api_key 才能打 CCR；优先复用文件里**已有的** CCR
    块（那把 key 是本机已在用的），没有才回落到 CCR config.json 的 APIKEY，
    再没有就留空并在 changes 里如实标注（宁可让用户自己补，也不猜一个假 key）。"""
    try:
        d = json.loads((_home() / ".claude-code-router" / "config.json").read_text(encoding="utf-8"))
        return str(d.get("APIKEY") or "")
    except Exception:  # noqa: BLE001
        return ""


def _home() -> Path:
    """每次调用现取：L0 用例靠改 HOME 造假盘，模块级常量会让夹具失效。"""
    return Path(os.path.expanduser("~"))


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _read_text(p: Path) -> str:
    try:
        return p.read_text(encoding="utf-8")
    except FileNotFoundError as e:
        raise ModelCfgError(f"配置文件不存在：{p}", 404) from e
    except OSError as e:  # noqa: BLE001
        raise ModelCfgError(f"配置文件不可读：{p}（{e}）", 500) from e


def _backup(p: Path, tag: str) -> str:
    """军规铁律：落笔前先时间戳备份。返回备份路径（相对 HOME 展示）。"""
    stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
    dst = p.with_name(p.name + f".bak-{stamp}-hub-modelcfg-{tag}")
    # 同一秒内连续落笔（设置页保存刚过、巡检写回就跟上来）会让时间戳撞车 ⇒
    # 后一份把前一份拷成同一个文件名覆盖掉，"留了备份"就成了假象。撞车就加序号。
    n = 1
    while dst.exists():
        n += 1
        dst = p.with_name(p.name + f".bak-{stamp}-{n:02d}-hub-modelcfg-{tag}")
    shutil.copy2(p, dst)
    return str(dst)


# ── 定点改写工具（只动目标 key，绝不整文件重写 / 全局替换）────────────
def _esc(v: str) -> str:
    return v.replace("\\", "\\\\").replace('"', '\\"')


def _section_range(lines: List[str], section: Optional[str]) -> Tuple[int, int]:
    """返回 (正文起, 正文止)：section=None 表示整个文件。"""
    if not section:
        return 0, len(lines)
    head = f"[{section}]"
    start = -1
    for i, ln in enumerate(lines):
        if ln.strip() == head:
            start = i
            break
    if start < 0:
        return -1, -1
    end = len(lines)
    for j in range(start + 1, len(lines)):
        s = lines[j].strip()
        if s.startswith("[") and s.endswith("]"):
            end = j
            break
    return start + 1, end


def _replace_in_range(lines: List[str], lo: int, hi: int, key: str, new_line: str) -> Tuple[bool, str]:
    """在 [lo,hi) 内把 key 的赋值行换成 new_line；不存在则返回插入用的 old=""。"""
    pat = re.compile(rf"^\s*{re.escape(key)}\s*=")
    for i in range(lo, hi):
        if pat.match(lines[i]):
            old = lines[i].split("=", 1)[1].strip().strip('"')
            lines[i] = new_line
            return True, old
    return False, ""


def _set_kv(text: str, key: str, value: str, section: Optional[str] = None,
            where: Optional[str] = None) -> Tuple[str, dict]:
    """TOML/YAML 通用定点赋值：改得到就改，改不到就插（section 缺失则整段追加）。"""
    new_line = f'{key} = "{_esc(value)}"'
    lines = text.splitlines()
    lo, hi = _section_range(lines, section)
    if lo < 0:
        block = (f"[{section}]\n" if section else "") + new_line + "\n"
        joined = text if text.endswith("\n") or not text else text + "\n"
        return joined + "\n" + block, {"where": where or f"{section}.{key}", "from": "", "to": value,
                                       "op": "add-section"}
    found, old = _replace_in_range(lines, lo, hi, key, new_line)
    if found:
        return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), \
            {"where": where or f"{section}.{key}", "from": old, "to": value, "op": "set"}
    insert_at = hi
    lines.insert(insert_at, new_line)
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), \
        {"where": where or f"{section}.{key}", "from": "", "to": value, "op": "add-key"}


def _set_blocked_kv(text: str, begin: str, end: str, key: str, value: str,
                    where: str) -> Tuple[str, dict]:
    """在 BEGIN/END 标记块内赋值（codex 的 CCR 托管块就用这个）。"""
    lines = text.splitlines()
    lo = next((i for i, ln in enumerate(lines) if ln.strip().startswith(begin)), -1)
    hi = next((i for i, ln in enumerate(lines) if ln.strip().startswith(end)), len(lines))
    if lo < 0:
        raise ModelCfgError(f"未找到托管块起始标记：{begin}（手写配置已被改形，拒绝落笔）", 409)
    lo += 1
    found, old = _replace_in_range(lines, lo, hi, key, f'{key} = "{_esc(value)}"')
    if not found:
        lines.insert(hi, f'{key} = "{_esc(value)}"')
        old = ""
    return "\n".join(lines) + ("\n" if text.endswith("\n") else ""), \
        {"where": where, "from": old, "to": value, "op": "set" if old else "add-key"}


def _set_comment_kv(text: str, prefix: str, value: str, where: str) -> Tuple[str, dict]:
    """改注释行里记的那份值（codex 的 '# CCR configured model = "..."'）。"""
    pat = re.compile(rf"^(\s*{re.escape(prefix)}\s*=\s*)\"([^\"]*)\"", re.M)
    m = pat.search(text)
    if not m:
        return text, {"where": where, "from": "", "to": value, "op": "skip"}
    old = m.group(2)
    return pat.sub(lambda _m: f'{_m.group(1)}"{_esc(value)}"', text, count=1), \
        {"where": where, "from": old, "to": value, "op": "set"}


def _toml_get(text: str, section: Optional[str], key: str) -> str:
    try:
        import tomllib
        data = tomllib.loads(text)
    except Exception:  # noqa: BLE001 —— 读坏了只影响展示，不许 500 打断设置页
        return ""
    node = data
    if section:
        # ⚠ 2026-10-07 修的真缺陷：section 可能**自带点号**。grok 的表头是 `[model.ccr-hub]`，
        # tomllib 里它就是字符串 "model.ccr-hub" **一个键**（官方文档：header 名即模型 ID，
        # ID 本身含点号）。旧实现一律按 "." 切分 ⇒ data["model"]["ccr-hub"] ⇒ None
        # ⇒ `_grok_read()` 的 current 恒空。**实测症状**：文件里明写
        # `default = "ccr-hub"` + `[model.ccr-hub] model = "alibaba/deepseek-v4.1-flash"`，
        # 设置页 grok 的「当前」却显示"（未读到）"，而 provider 串位显示成模型 ID。
        # 口径：**先整键查**，查不到再逐级下钻（保留对嵌套 section 如 "providers.ccr" 的兼容）。
        node = data.get(section)
        if node is None:                      # 整键查不到 ⇒ 退回逐级下钻（嵌套 section）
            node = data
            for part in section.split("."):
                node = node.get(part) if isinstance(node, dict) else None
                if node is None:
                    break
        if node is None:
            return ""
    v = node.get(key) if isinstance(node, dict) else None
    return str(v) if v is not None else ""


# ── 各家读写器 ──────────────────────────────────────────────────────
def _claude_paths() -> List[Path]:
    return [_home() / ".claude" / "settings.json"]


def _claude_read() -> dict:
    p = _claude_paths()[0]
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.claude/settings.json"}
    try:
        d = json.loads(_read_text(p))
    except json.JSONDecodeError as e:
        return {"current": "", "files": [str(p)], "note": f"settings.json 解析失败：{e}"}
    env = d.get("env") or {}
    return {"current": str(d.get("model") or ""),
            "provider": "ccr",
            "extra": {"env.ANTHROPIC_MODEL": str(env.get("ANTHROPIC_MODEL") or ""),
                      "env.CCR_CLAUDE_CODE_MODEL": str(env.get("CCR_CLAUDE_CODE_MODEL") or "")},
            "files": [str(p)]}


def _claude_write(model: str) -> List[Tuple[Path, str, List[dict]]]:
    p = _claude_paths()[0]
    text = _read_text(p)
    d = json.loads(text)
    changes: List[dict] = []
    old = str(d.get("model") or "")
    d["model"] = model
    changes.append({"where": "settings.json:model", "from": old, "to": model, "op": "set"})
    env = d.setdefault("env", {})
    for k in ("ANTHROPIC_MODEL", "CCR_CLAUDE_CODE_MODEL", "CODEXL_CLAUDE_CODE_MODEL"):
        oldv = str(env.get(k) or "")
        env[k] = model
        changes.append({"where": f"env.{k}", "from": oldv, "to": model, "op": "set"})
    return [(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n", changes)]


def _jcode_read() -> dict:
    p = _home() / ".jcode" / "config.toml"
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.jcode/config.toml"}
    text = _read_text(p)
    return {"current": _toml_get(text, "provider", "default_model"),
            "provider": _toml_get(text, "provider", "default_provider"),
            "files": [str(p)]}


def _jcode_write(model: str):
    p = _home() / ".jcode" / "config.toml"
    text = _read_text(p)
    changes = []
    text, c1 = _set_kv(text, "default_model", model, section="provider")
    changes.append(c1)
    text, c2 = _set_kv(text, "default_model", model, section="providers.ccr")
    changes.append(c2)
    return [(p, text, changes)]


def _codex_read() -> dict:
    p = _home() / ".codex" / "config.toml"
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.codex/config.toml"}
    text = _read_text(p)
    return {"current": _toml_get(text, None, "model"),
            "provider": _toml_get(text, None, "model_provider"),
            "files": [str(p)]}


def _codex_write(model: str):
    p = _home() / ".codex" / "config.toml"
    text = _read_text(p)
    changes = []
    text, c1 = _set_blocked_kv(text, "# BEGIN CCR managed profile", "# END CCR managed profile",
                               "model", model, "config.toml:model(CCR托管块)")
    changes.append(c1)
    text, c2 = _set_comment_kv(text, "# CCR configured model", model, "CCR configured model 注释")
    changes.append(c2)
    return [(p, text, changes)]


def _pi_model_specs(model_id: str, m: dict) -> Dict[str, int]:
    """新增清单条目时补上 contextWindow / maxTokens。

    缺这两个字段时 pi 会静默落兜底（`provider-composer.js`:
    `definition.contextWindow ?? 128000` / `maxTokens ?? 16384`）⇒ pi-web 回显 128k、
    压缩阈值也按 128k 算，上游能吃 1M 也用不满（agent-knowledge/165）。

    只从**本机已有声明**里抄，不联网、不内置常量表：
      ① 同一份 models.json 里其它 provider 的同 id 条目；
      ② pi 内置 models-store.json（同 id，或 id 末段唯一匹配）。
    两处都没有就返回空 dict —— 宁缺不猜，写个假数会被上游按 256k/1M 边界拒。
    """
    keys = ("contextWindow", "maxTokens")
    out: Dict[str, int] = {}

    def _take(entry: dict) -> None:
        for k in keys:
            v = entry.get(k)
            if k not in out and isinstance(v, int) and not isinstance(v, bool) and v > 0:
                out[k] = v

    for prov in (m.get("providers") or {}).values():
        if not isinstance(prov, dict):
            continue
        for x in (prov.get("models") or []):
            if isinstance(x, dict) and str(x.get("id") or "") == model_id:
                _take(x)
    if len(out) == len(keys):
        return out

    try:
        store = json.loads((_home() / ".pi" / "agent" / "models-store.json")
                           .read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001 —— 内置清单缺失/损坏不影响写配置
        return out
    tail = model_id.rsplit("/", 1)[-1]
    for prov in (store or {}).values() if isinstance(store, dict) else []:
        if not isinstance(prov, dict):
            continue
        for x in (prov.get("models") or []):
            if not isinstance(x, dict):
                continue
            mid = str(x.get("id") or "")
            if mid == model_id or mid.rsplit("/", 1)[-1] == tail:
                _take(x)
            if len(out) == len(keys):
                return out
    return out


def _pi_read() -> dict:
    sp = _home() / ".pi" / "agent" / "settings.json"
    mp = _home() / ".pi" / "agent" / "models.json"
    if not sp.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.pi/agent/settings.json"}
    try:
        s = json.loads(_read_text(sp))
    except json.JSONDecodeError as e:
        return {"current": "", "files": [str(sp)], "note": f"settings.json 解析失败：{e}"}
    return {"current": str(s.get("defaultModel") or ""),
            "provider": str(s.get("defaultProvider") or ""),
            "files": [str(sp), str(mp) if mp.exists() else str(sp)]}


def _pi_write(model: str):
    sp = _home() / ".pi" / "agent" / "settings.json"
    mp = _home() / ".pi" / "agent" / "models.json"
    out = []
    s = json.loads(_read_text(sp))
    changes = []
    for k, v in (("defaultModel", model), ("defaultProvider", PI_CCR_PROVIDER)):
        old = str(s.get(k) or "")
        s[k] = v
        changes.append({"where": f"settings.json:{k}", "from": old, "to": v, "op": "set"})
    out.append((sp, json.dumps(s, ensure_ascii=False, indent=2) + "\n", changes))
    # 模型不在 ccr-free 清单里时补一条：pi 只认清单内的 id（实测 --model 传清单外 id 会报 unknown model）
    if mp.exists():
        m = json.loads(_read_text(mp))
        prov = (m.get("providers") or {}).get(PI_CCR_PROVIDER)
        ch2: List[dict] = []
        if prov is None:
            prov = {"api": "openai-completions", "baseUrl": "http://127.0.0.1:3456/v1",
                    "apiKey": _ccr_api_key(), "models": []}
            m.setdefault("providers", {})[PI_CCR_PROVIDER] = prov
            ch2.append({"where": f"models.json:providers.{PI_CCR_PROVIDER}", "from": "",
                        "to": "新建 provider（CCR :3456/v1）", "op": "add-section"})
        ids = [str(x.get("id") or "") for x in (prov.get("models") or [])]
        if model not in ids:
            entry = {"id": model, "name": model}
            # 不带 contextWindow 会被 pi 静默兜底成 128000（pi-web 回显 128k）
            entry.update(_pi_model_specs(model, m))
            prov.setdefault("models", []).append(entry)
            ch2.append({"where": f"models.json:providers.{PI_CCR_PROVIDER}.models",
                        "from": f"{len(ids)} 条", "to": f"{len(ids) + 1} 条（+ {model}）", "op": "add-key"})
        if ch2:
            out.append((mp, json.dumps(m, ensure_ascii=False, indent=2) + "\n", ch2))
    return out


def _grok_read() -> dict:
    """grok 的「默认模型」＝ `[models] default` 指向的那个**表头键**，值在 `[model.<键>] model`。

    2026-10-07 修（原先 current 恒空，见 _toml_get 的注释）。本机实测全链：
      `[models] default = "ccr-hub"` + `[model.ccr-hub] model = "alibaba/deepseek-v4.1-flash"`
      ⇒ 真正发出去的模型 ID 是 `alibaba/deepseek-v4.1-flash`（CCR 账本 served 逐次命中）。
    故展示**模型 ID**（那是用户关心的"现在用哪个模型"），`provider` 字段放表头键（
    用于区分「内置 grok-4.6」与「自定义 CCR 块」两类来源）。"""
    p = _home() / ".grok" / "config.toml"
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.grok/config.toml"}
    text = _read_text(p)
    key = _toml_get(text, "models", "default")
    if not key:
        return {"current": "", "provider": "", "files": [str(p)]}
    # `[models] default` 允许两种写法，grok 两种都认（2026-10-07 实测）：
    #   ① 表头键（官方 README 的写法：`default = "company-grok"` → `[model.company-grok]`）
    #   ② 某个 `[model.*]` 块里的 model **值**（模型 ID 本身）
    # ⚠ 本机现场正是 ②：文件里是 `default = "alibaba/deepseek-v4.1-flash"`，
    # 而 `grok models` 报 `Default model: ccr-hub` ⇒ grok 自己会按"值→块"反解出表头。
    # 设置页要对齐 grok 的口径：current 报**真正发出去的模型 ID**，provider 报表头键。
    tbl = {}
    try:
        import tomllib
        tbl = tomllib.loads(text).get("model") or {}
    except Exception:  # noqa: BLE001 —— 读坏了只影响展示
        tbl = {}
    if key in tbl:
        blk = tbl.get(key) if isinstance(tbl.get(key), dict) else {}
        return {"current": str(blk.get("model") or key), "provider": key, "files": [str(p)]}
    for name, blk in tbl.items():
        if isinstance(blk, dict) and str(blk.get("model") or "") == key:
            return {"current": key, "provider": name, "files": [str(p)]}
    # 既不是表头也不是任何块的值 ⇒ 内置模型 ID（如官方示例 grok-4.6）
    return {"current": key, "provider": key, "files": [str(p)]}


def _grok_write(model: str):
    p = _home() / ".grok" / "config.toml"
    text = _read_text(p)
    changes: List[dict] = []
    sec = f"model.{GROK_PROFILE_KEY}"
    # key 先复用文件里已有的 CCR 块（本机正在用的那把），再回落 CCR config.json
    existing = _toml_get(text, sec, "api_key") or ""
    if not existing:
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith("api_key") and "3456" in text[max(0, text.find(ln) - 400):text.find(ln)]:
                existing = s.split("=", 1)[1].strip().strip('"')
                break
    api_key = existing or _ccr_api_key()
    for k, v in (("model", model), ("base_url", "http://127.0.0.1:3456/v1"),
                 ("name", f"CCR {model}"), ("api_key", api_key),
                 ("api_backend", "chat_completions"), ("auth_scheme", "bearer")):
        text, c = _set_kv(text, k, v, section=sec)
        changes.append(c)
    text, c = _set_kv(text, "default", GROK_PROFILE_KEY, section="models")
    changes.append(c)
    return [(p, text, changes)]


def _hermes_read() -> dict:
    p = _home() / ".hermes" / "config.yaml"
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.hermes/config.yaml"}
    cur = prov = ""
    in_block = False
    for ln in _read_text(p).splitlines():
        if ln.rstrip() == "model:":
            in_block = True
            continue
        if in_block:
            if ln and not ln.startswith((" ", "\t")):
                break
            s = ln.strip()
            if s.startswith("default:"):
                cur = s.split(":", 1)[1].strip()
            elif s.startswith("provider:"):
                prov = s.split(":", 1)[1].strip()
    return {"current": cur, "provider": prov, "files": [str(p)]}


def _hermes_write(model: str):
    p = _home() / ".hermes" / "config.yaml"
    text = _read_text(p)
    lines = text.splitlines()
    lo = next((i for i, ln in enumerate(lines) if ln.rstrip() == "model:"), -1)
    if lo < 0:
        raise ModelCfgError("config.yaml 顶层没有 model: 块（手写配置已被改形，拒绝落笔）", 409)
    hi = len(lines)
    for j in range(lo + 1, len(lines)):
        if lines[j] and not lines[j].startswith((" ", "\t")):
            hi = j
            break
    body = "\n".join(lines[lo + 1:hi])
    changes: List[dict] = []
    for key, val in (("default", model), ("provider", HERMES_CCR_PROVIDER)):
        pat = re.compile(rf"^(\s*{key}:\s*)(\S.*)$", re.M)
        m = pat.search(body)
        if m:
            old = m.group(2).strip()
            body = pat.sub(lambda _m, v=val: f"{_m.group(1)}{v}", body, count=1)
            op = "set"
        else:
            old = ""
            body = body.rstrip("\n") + f"\n  {key}: {val}\n"
            op = "add-key"
        changes.append({"where": f"config.yaml:model.{key}", "from": old, "to": val, "op": op})
    new_lines = lines[:lo + 1] + body.split("\n") + lines[hi:]
    return [(p, "\n".join(new_lines) + "\n", changes)]


# ── 第二阶段（2026-10-07）新增三家的读写器 ──────────────────────────
#
# 【用户裁定：模型选择按「原生 + 插入」】
#   ① **不破坏原生模型**：hub 只往该 agent「本来就要读」的字段里落值，绝不新建/覆盖
#      agent 自己的模型清单、默认值以外的结构、provider 定义、凭据。
#   ② **插入要精确**：只动目标 key（逐行定点改写），不整文件重写、不全局替换。
#   ③ **不破坏 agent 原有代码**：hub 只写配置文件；除 argv 注入外不碰 agent 的安装目录。
#
# 本组三家与既有六家最大的不同：它们的 CLI 都用 `--model`，故 **argv 注入是首选通路**，
# 配置文件写入是「让不带 --model 的启动（如用户自己在终端敲、或续聊）也跟随」的兜底。
# 两件事互相独立：注入只影响 hub 拉起的会话，配置只影响该 agent 的默认值。


def _opencode_path() -> Path:
    return _home() / ".config" / "opencode" / "opencode.json"


def _opencode_read() -> dict:
    """读 opencode 的默认模型（顶层 `model` = `provider_id/model_id`）。

    ⚠ 本机实测的坑（agent-knowledge/50 第九节）：opencode 是**双登记** ——
    光把 ID 写进 `opencode.json` 的 provider.models 还不够，CLI 侧还要认这个 ID；
    两者缺一，`opencode run --model ccr/...` 会回 `UnknownError / Unexpected server error`
    （**HTTP 层看不出是模型问题**）。所以写入时必须在**同一个 provider** 内登记，
    而不是凭空造一条。"""
    p = _opencode_path()
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.config/opencode/opencode.json"}
    try:
        d = json.loads(_read_text(p))
    except json.JSONDecodeError as e:
        return {"current": "", "files": [str(p)], "note": f"opencode.json 解析失败：{e}"}
    cur = str(d.get("model") or "")
    # provider 取**现值自己的**前缀，不是写死 "ccr"：现值恒为 `provider/model_id`
    # 形状，写死 ccr 会在用户把 provider 换成别家时给出假信息（v0.13.86 前即如此）。
    return {"current": cur,
            "provider": cur.partition("/")[0] if "/" in cur else "",
            "extra": {"small_model": str(d.get("small_model") or "")},
            "files": [str(p)]}


def _opencode_write(model: str):
    """把 `<provider>/<model_id>` 写进顶层 `model`（opencode 的官方默认模型落点）。

    **只在 JSON 的 3 个键上落值**，其余原样保留（json.loads/dumps 保序、缩进 2 格）：
      model        —— 本次选定值（含 provider 前缀，opencode 要求该形状）
      small_model  —— 仅当它**本来就等于旧的 model** 时跟随更新。
                      为什么要这条：本机 `small_model` 与 `model` 原本同值，留旧值会造成
                      「主模型换了、轻量任务仍走旧模型」的**静默分叉**（用户看到的仍是旧模型在工作）。
                      ⚠ 只在「两者原本相同」时才跟随 —— 用户若**故意**把 small_model 设成别的模型，
                      那是他的配置意图，hub 不许替他改。
      provider.ccr.models —— **精确插入**：仅当目标 ID 不在该 provider 的清单里才追加一条
                      （`{id, name}`，与既有条目同形）。**不删除、不重排、不动其它 provider**。
                      这是「插入」二字的落点：opencode 只认清单内 ID，缺了就整个跑不起来。

    ⚠ provider 前缀是**必须**的：本机实测 `/model` 的默认值即 `ccr/...` 形状，
    而 provider 段已存在（`provider.ccr`），故本函数**不新建 provider**。
    若该 provider 段不存在 ⇒ 抛错而不是凭空造一个（凭空造会缺 baseURL/apiKey，
    等于给用户一个连不上的 provider，比报错更坏）。"""
    p = _opencode_path()
    text = _read_text(p)
    d = json.loads(text)
    prov_id, _, mid = model.partition("/")
    prov = (d.get("provider") or {}).get(prov_id)
    if prov_id == model or not mid:
        # 没有 provider 前缀 ⇒ opencode 无法定位 provider（实测会 UnknownError）
        raise ModelCfgError(
            f"OpenCode 的模型 ID 必须带 provider 前缀（形如 ccr/{prov_id}）："
            f"当前 {model!r} 缺前缀，直接写下去会报 UnknownError", 400)
    if prov is None:
        raise ModelCfgError(
            f"opencode.json 里没有 provider {prov_id!r}（本机应已配好 ccr）。"
            f"拒绝凭空新建 provider：那样会缺 baseURL/apiKey，等于给一个连不上的配置", 409)

    changes: List[dict] = []
    old = str(d.get("model") or "")
    d["model"] = model
    changes.append({"where": "opencode.json:model", "from": old, "to": model, "op": "set"})

    old_small = str(d.get("small_model") or "")
    if old_small and old and old_small == old:
        d["small_model"] = model
        changes.append({"where": "opencode.json:small_model", "from": old_small, "to": model, "op": "set"})

    ids = list((prov.get("models") or {}).keys())
    if mid not in ids:
        prov.setdefault("models", {})[mid] = {"name": mid.split("/", 1)[-1]}
        changes.append({"where": f"opencode.json:provider.{prov_id}.models.{mid}",
                        "from": "", "to": f"登记模型（原 {len(ids)} 条 → {len(ids) + 1} 条）",
                        "op": "add-key"})
    return [(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n", changes)]


def _qoder_path() -> Path:
    return _home() / ".qoder" / "settings.json"


def _qoder_read() -> dict:
    """读 qoder 的默认模型（`~/.qoder/settings.json` 的 `model.name`，原生模型名）。

    ⚠ 官方文档（docs.qoder.com/cli/custom-models，2026-10-07 实测抓取）明确：
    BYOK 自定义模型**必须走 `/model` 的 Custom 向导**，且「**不要手工写进 settings.json**」
    —— 可用 provider/模型/凭据字段由当前账号的 BYOK 目录决定。故 hub **只写原生模型名**，
    **不替它造 custom provider 配置**（那正是官方禁止的写法，也是"破坏原生"的典型）。

    ⚠ `model.name` 取的是**原生模型名**（如 `Qwen3.8-Max`），不是 CCR 的 provider/model ID。
    若 hub 侧存着 CCR ID，本函数会如实把「原生值 vs hub 值」的差异摆在设置页，
    由用户用命令行 `-m` 的注入值去覆盖 —— **不把 CCR ID 塞进这个字段**（塞了 qoder 不认）。"""
    p = _qoder_path()
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.qoder/settings.json"}
    try:
        d = json.loads(_read_text(p))
    except json.JSONDecodeError as e:
        return {"current": "", "files": [str(p)], "note": f"settings.json 解析失败：{e}"}
    return {"current": str((d.get("model") or {}).get("name") or ""),
            "provider": "",
            "files": [str(p)]}


def _qoder_write(model: str):
    """只改 `settings.json:model.name` 一个键，其余（securityScan/permissions/security）原样保留。

    ⚠ 传进来的若是 CCR ID（含 `/` 或 `:`），qoder 不认 —— 这里**拒绝落笔**而不是写进去。
    为什么必须拒：写进去的后果是「设置页显示已保存、而 qoder 侧 default 变成不存在的模型」，
    静默失败比报错更难查。CCR 模型在 qoder 上只能靠 `--model` 注入（那种会话里 qoder 只是
    个普通客户端，模型由 CCR 侧决定），故设置页对近类 agent 的文案要说明这一点。"""
    p = _qoder_path()
    text = _read_text(p)
    d = json.loads(text)
    if "/" in model or ":" in model:
        raise ModelCfgError(
            f"Qoder CLI 的模型名是本家的原生名（如 Qwen3.8-Max），不接受 CCR 的 "
            f"provider/model ID：{model!r}。要用 CCR 模型请改走「终端 --model 注入」"
            f"（在终端里拉起 qoder 会话时 hub 会追加 --model）", 400)
    blk = d.get("model")
    if not isinstance(blk, dict):
        blk = {}
        d["model"] = blk
    old = str(blk.get("name") or "")
    blk["name"] = model
    return [(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n",
             [{"where": "settings.json:model.name", "from": old, "to": model, "op": "set"}])]


def _cursor_path() -> Path:
    return _home() / ".cursor" / "cli-config.json"


def _cursor_read() -> dict:
    """读 cursor-agent 的默认模型（`~/.cursor/cli-config.json`）。

    落点是**三个同源键**（本机实测，2026-10-06 用 `--model alibaba/deepseek-v4.1-flash`
    之后 CLI 自己写下来的形状）：`model.modelId` / `selectedModel.modelId` 与
    `hasChangedDefaultModel` 旗标。`model` 块还被 `modelParameters` 按 ID 索引
    ⇒ 换模型时要在那里补一条空参数表，否则参数面板查不到当前模型。"""
    p = _cursor_path()
    if not p.exists():
        return {"current": "", "files": [], "note": "未找到 ~/.cursor/cli-config.json"}
    try:
        d = json.loads(_read_text(p))
    except json.JSONDecodeError as e:
        return {"current": "", "files": [str(p)], "note": f"cli-config.json 解析失败：{e}"}
    return {"current": str((d.get("model") or {}).get("modelId") or ""),
            "provider": "ccr",
            "files": [str(p)]}


def _cursor_write(model: str):
    """精确落 4 处：`model.{modelId,displayModelId,displayName,displayNameShort}`、
    `selectedModel.modelId`、`modelParameters[model]`、`hasChangedDefaultModel=true`。

    **绝不碰的**：permissions / display / editor / sandbox / attribution / network / hooks
    —— 这些是用户与 cursor 自己的配置面，hub 只负责「默认模型」这一件事。"""
    p = _cursor_path()
    text = _read_text(p)
    d = json.loads(text)
    changes: List[dict] = []
    blk = d.get("model") if isinstance(d.get("model"), dict) else {}
    old = str(blk.get("modelId") or "")
    for k in ("modelId", "displayModelId", "displayName", "displayNameShort"):
        blk[k] = model
        changes.append({"where": f"cli-config.json:model.{k}", "from": old if k == "modelId" else "",
                        "to": model, "op": "set"})
    d["model"] = blk
    sel = d.get("selectedModel") if isinstance(d.get("selectedModel"), dict) else {}
    sel["modelId"] = model
    sel.setdefault("parameters", [])
    d["selectedModel"] = sel
    changes.append({"where": "cli-config.json:selectedModel.modelId", "from": "", "to": model, "op": "set"})
    params = d.get("modelParameters") if isinstance(d.get("modelParameters"), dict) else {}
    if model not in params:
        params[model] = []
        changes.append({"where": f"cli-config.json:modelParameters.{model}",
                        "from": "", "to": "空参数表", "op": "add-key"})
    d["modelParameters"] = params
    d["hasChangedDefaultModel"] = True
    changes.append({"where": "cli-config.json:hasChangedDefaultModel", "from": "", "to": "true", "op": "set"})
    return [(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n", changes)]

#: agent_id -> {name, read, write, argv}
SPECS: Dict[str, dict] = {
    "claude": {"name": "Claude Code", "read": _claude_read, "write": _claude_write},
    "jcode": {"name": "JCode", "read": _jcode_read, "write": _jcode_write},
    "codex": {"name": "Codex CLI", "read": _codex_read, "write": _codex_write},
    "pi": {"name": "Pi Agent", "read": _pi_read, "write": _pi_write},
    "grok": {"name": "Grok CLI", "read": _grok_read, "write": _grok_write},
    "hermes": {"name": "Hermes", "read": _hermes_read, "write": _hermes_write},
    # ── 2026-10-07 新增三家（用户报障「模型设置里没有 agents 的所有模型」）──
    "opencode": {"name": "OpenCode", "read": _opencode_read, "write": _opencode_write},
    "qoder": {"name": "Qoder CLI", "read": _qoder_read, "write": _qoder_write},
    "cursor": {"name": "Cursor Agent", "read": _cursor_read, "write": _cursor_write},
}

#: 在册但**不可统一设置**的 agent（理由必须写清，前端据此置灰）
UNSUPPORTED: Dict[str, str] = {
    "codebuddy": "WorkBuddy 包内 CLI：--model 只认自有清单（hy4-preview/hy3/…），与 CCR 的 provider/model ID 不通用",
    "qwenpaw": "纯 Web 型，无 CLI 与本机模型配置文件",
    # 2026-10-07 实测取证（不是想当然）：cloudcli --help 的全部命令是
    # start / sandbox / browser-use-mcp / status / update / help / version，
    # 无 --model 选项，其模型由内嵌的 claude / codex / opencode 客户端各自决定。
    "cloudcli": "CloudCLI 是 Web 宿主（cloudcli --help 实测无 --model 选项）："
                "它的模型由内嵌的 claude/codex/opencode 客户端各自决定，应在对应 agent 上设置",
}


def validate_model(model: str) -> str:
    m = (model or "").strip()
    if not m:
        raise ModelCfgError("模型 ID 为空")
    if not MODEL_ID_RE.match(m):
        raise ModelCfgError("模型 ID 形状不合法（只允许字母数字与 . _ : / + -，≤120 字符）")
    return m


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
DRIFT_EXEMPT: Dict[str, str] = {
    "qoder": "字段为本家原生模型名，与 CCR 的 provider/model ID 不可互比，"
             "CCR 模型只经终端 --model 注入生效",
}


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
        want = hub_model(aid)
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
            res = apply_model(aid, want)
        except ModelCfgError as e:
            out["skipped"].append({"id": aid, "reason": f"写回失败：{e.args[0]}"})
            continue
        out["repaired"].append({"id": aid, "model": want,
                                "backups": [a["backup"] for a in res["applied"]],
                                "owned": d.get("owned") or {}})
    return out


def terminal_argv(agent_id: str, model: str) -> List[str]:
    """拉起终端时追加的 argv；不在白名单或没设模型则返回 []（绝不猜 flag）。

    调用点两处（v0.13.51）：新会话与**续聊**都要加 —— 续聊命令 `claude --resume <id>`
    不带 --model 时用 agent 自己的配置文件，而那份文件会被 CCR 启动改写（见
    drift_report），等于"设置页改了、重启后照样退回旧模型"。追加位置固定在模板
    末尾，不动 `--resume <id>` 这类位置参数。"""
    flag = MODEL_ARGV.get(agent_id)
    if not flag or not model:
        return []
    # ⚠ 2026-10-07 精准化（判据＝官方 README + 本机实弹）：**grok 的 `-m` 收的是
    # `[model.*]` 的表头键，不是 provider/model ID**。
    #   · `grok -m alibaba/qwen3.8-max`       ⇒ 硬报 `unknown model id` 并 exit（实测）
    #   · `grok -m ccr-hub` / `-m ccr-qwen38-flash` ⇒ 正常，账本 served 精确命中（实测）
    #   · README 原话：「The name in the TOML header is what appears in the model picker;
    #     the `model` field is the identifier sent to the API」，示例 `-m my-model` 亦为表头键。
    # 而 `_grok_write` 恒定把选中的 ID 落进 `[model.ccr-hub].model` 并把 `[models].default`
    # 指向该键 ⇒ **注入这个键才与配置文件同源、且换任一个模型都不会落空**。
    # 若这里继续注入裸 ID，只因该 ID「恰好等于某块里的 model 值」才碰巧能用 —— 属侥幸，
    # 一旦值不再匹配（如又切了一次模型）就会变成 `unknown model id` 硬失败。
    if agent_id == "grok":
        return [flag, GROK_PROFILE_KEY]
    return [flag, model]


# ── 对外：状态 / 预览 / 落笔 ────────────────────────────────────────
def agent_state(agent_id: str) -> dict:
    spec = SPECS.get(agent_id)
    if not spec:
        return {"id": agent_id, "supported": False, "writable": False,
                "reason": UNSUPPORTED.get(agent_id, "不在模型设置白名单内"),
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
    return {"id": agent_id, "name": spec["name"], "supported": True,
            "writable": bool(files) and all(Path(f).exists() for f in files),
            "current": st.get("current", ""), "provider": st.get("provider", ""),
            "extra": st.get("extra", {}), "note": st.get("note", ""),
            "files": files, "argv": MODEL_ARGV.get(agent_id, ""),
            # v0.13.86：设置页拿这两列才能摆出"点了真能写进去"的选项 ——
            # value_prefix 让选项值等于该 agent 字段真正要的形状（opencode 的 ccr/），
            # models 让只认原生名的 agent（qoder）摆原生清单而不是必然被拒的 CCR 清单。
            "value_prefix": VALUE_PREFIX.get(agent_id, ""),
            "models": NATIVE_MODELS.get(agent_id, []),
            # v0.13.85：把「这个 agent 的模型字段认什么」摆到设置页，否则用户会把
            # 原生名/CCR ID/只有注入三条路混着用，改了不生效却看上去"保存成功"。
            "write_mode": WRITE_MODE.get(agent_id, "argv"),
            "hub_model": hub_model(agent_id)}


def list_agents() -> List[dict]:
    ids = list(SPECS.keys()) + list(UNSUPPORTED.keys())
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
    writes = spec["write"](model)
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
    applied = []
    try:
        for (p, new, changes), bak in zip(writes, backups):
            p.write_text(new, encoding="utf-8")
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
    return {"agent_id": agent_id, "model": model, "mode": "set", "applied": applied,
            "write_mode": WRITE_MODE.get(agent_id, "argv"),
            "argv": terminal_argv(agent_id, model), "at": _now()}


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
        print(f"[settings] 模型设置口令错误：{ip}", flush=True)
        raise HTTPException(401, "口令错误")


def _guard(e: ModelCfgError) -> HTTPException:
    return HTTPException(e.status, e.args[0])


@router.get("/api/settings/models")
async def settings_models():
    """设置→模型子菜单的首屏数据：各 agent 现值 + CCR Router 只读视图 + 漂移体检。"""
    return {"agents": list_agents(), "ccr": ccr_router_view(), "drift": drift_report()}


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
    print(f"[settings] 模型设置落笔：{body.agent_id} → {act}", flush=True)
    return out
