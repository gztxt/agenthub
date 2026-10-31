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
    # ── 2026-10-25 新增（omp 入册，实测 omp --help：`--model=<value>  Model to use
    #    (fuzzy match: "opus", "gpt-5.2", or "openai/gpt-5.2")`）──
    "omp": "--model",
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
    # argv-only：无默认模型配置文件可落（见 _omp_read），hub 侧设置只经终端注入。
    "omp": "argv",
}

#: 该 agent 的模型字段要求的**值前缀**（v0.13.86，2026-10-07；2026-10-25 补 omp）。
#: 为什么需要：CCR 清单（/api/models）给的是裸 ID（`alibaba/qwen3.8-max`），而
#: opencode 的 `model` / `--model` 收的是 `provider/model_id` 形状 —— 官方文档 Models 页
#: 原话「The format is `provider/model`」，本机同名 CLI 亦实测回 `ccr/<id>`（`opencode
#: models ccr` 逐行都是 `ccr/…`）。用户若照着清单点裸 ID，`_opencode_write` 会 409
#: 且指错方向（它说「没有 provider alibaba」，实际缺的是 `ccr/` 前缀）。
#: ⇒ 设置页据此把选项值拼成 `<前缀><CCR ID>`，让"能点到的"与"能写进的"是同一个东西。
#: omp 同族（2026-10-25 实测）：`omp --model openrouter/openrouter/free` 报
#: 「No API key found for openrouter」—— 它把裸 ID 的**首段**当成 provider 名；
#: 正确形状是 `ccr/openrouter/openrouter/free`（provider key = ~/.omp/agent/models.yml
#: 里的 `ccr`，实测该形状一句话正常返回）。⚠ 前缀跟着 provider key 走，用户若把
#: models.yml 里的 provider 改名，这里要同步改。
VALUE_PREFIX: Dict[str, str] = {"opencode": "ccr/", "omp": "ccr/"}

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


def bare_model_id(agent_id: str, model: str) -> str:
    """把该 agent 的落点值还原成 CCR 裸模型 ID（用于比对清单）。

    opencode/omp 的字段带 provider 前缀（`ccr/alibaba/...`），reasonix 若接入同族。
    只剥**该 agent 登记过的**前缀，不做通用剥离 —— 通用剥离会把 `openrouter/xxx`
    这种真 ID 的首段吃掉，造成"明明在清单里却判成不在"。"""
    pre = VALUE_PREFIX.get(agent_id, "")
    if pre and model.startswith(pre):
        return model[len(pre):]
    return model


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


def _json_load(text: str):
    """按 **JSONC** 解析（`//` 与 `/* */` 注释先抹成空格）。

    为什么不能直接用 `json.loads`（v0.13.182）：opencode 的配置文件官方允许注释，
    用户写一行 `// 为什么选这个模型` 就让读侧显示"解析失败"、写侧直接 500 —
    而它本来是完全合法的配置。解析失败抛 `JsonSpanError`（与 JSONDecodeError 同族处理）。
    """
    return jsonspan.validate(text)


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


def _atomic_write(p: Path, text: str) -> None:
    """原子落盘：同目录临时文件 → fsync → os.replace（magpie 的 temp-and-rename）。

    为什么必须原子（2026-10-30 借 magpie 设计）：`write_text` 是「先截断、再写」，
    截断与写完之间有一个窗口，此刻任何并发读取方（agent 启动读 settings.json、
    CCR 启动重写、看门狗巡检）都会读到**空文件或半截 JSON**。备份救不了这种情形
    —— 备份救的是「写坏了」，救不了「读到半截」。同文件系统内 rename 是原子的，
    读方要么看到旧版要么看到新版。
    权限位显式沿用原文件：os.replace 用的是临时文件的 mode，不带过去会变成 0600，
    把用户 0644 的配置改成只有自己能读。
    """
    mode = p.stat().st_mode & 0o777
    fd, tmp = tempfile.mkstemp(dir=str(p.parent), prefix=f".{p.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            f.write(text)
            f.flush()
            os.fsync(f.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, p)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


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


def _json_set_many(text: str, sets: List[Tuple[Tuple[str, ...], str]]):
    """逐个字符串键做**定点替换**，返回 (新文本, [(路径, 旧值)])。

    任一键缺失或不是字符串 ⇒ 返回 (None, [])，由调用方回落到"解析→改→整份重写"。
    这样保留老路径的兜底语义（新键要新建时仍能建成），同时让**常见情形**只改那段字节。
    """
    out = text
    olds: List[Tuple[Tuple[str, ...], str]] = []
    try:
        for path, val in sets:
            out, old = jsonspan.set_string(out, path, val)
            olds.append((path, old))
    except (KeyError, jsonspan.JsonSpanError):
        return None, []
    return out, olds


def _claude_write(model: str) -> List[Tuple[Path, str, List[dict]]]:
    p = _claude_paths()[0]
    text = _read_text(p)
    changes: List[dict] = []
    env_keys = ("ANTHROPIC_MODEL", "CCR_CLAUDE_CODE_MODEL", "CODEXL_CLAUDE_CODE_MODEL")
    # v0.13.181：先走定点替换（magpie 那套「只动那一个 key」）。本机 settings.json 是
    # 4 空格缩进，而 json.dumps(indent=2) 会把整份 55 行重排 ⇒ 用户 diff 全红、
    # 与其它写入方（CCR 启动重写）的差异被噪声盖住。
    new, olds = _json_set_many(text, [(("model",), model)] +
                               [(("env", k), model) for k in env_keys])
    if new is not None:
        changes.append({"where": "settings.json:model", "from": olds[0][1], "to": model, "op": "set"})
        for (path, old), _v in zip(olds[1:], env_keys):
            changes.append({"where": f"env.{path[-1]}", "from": old, "to": model, "op": "set"})
        return [(p, new, changes)]
    # 兜底：键不全（新装环境）时按老路径补齐后整份重写，语义与改前一致
    d = json.loads(text)
    old = str(d.get("model") or "")
    d["model"] = model
    changes.append({"where": "settings.json:model", "from": old, "to": model, "op": "set"})
    env = d.setdefault("env", {})
    for k in env_keys:
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
    text = _read_text(sp)
    changes = []
    pairs = [("defaultModel", model), ("defaultProvider", PI_CCR_PROVIDER)]
    # v0.13.181：定点替换优先（settings.json 未改动字节一律保留）
    new, olds = _json_set_many(text, [((k,), v) for k, v in pairs])
    if new is not None:
        for (k, v), (_path, old) in zip(pairs, olds):
            changes.append({"where": f"settings.json:{k}", "from": old, "to": v, "op": "set"})
        out.append((sp, new, changes))
    else:                                   # 键不全（新装环境）⇒ 老路径补齐
        s = json.loads(text)
        for k, v in pairs:
            old = str(s.get(k) or "")
            s[k] = v
            changes.append({"where": f"settings.json:{k}", "from": old, "to": v, "op": "set"})
        out.append((sp, json.dumps(s, ensure_ascii=False, indent=2) + "\n", changes))
    # 模型不在 ccr-free 清单里时补一条：pi 只认清单内的 id（实测 --model 传清单外 id 会报 unknown model）
    if mp.exists():
        mtext = _read_text(mp)
        m = json.loads(mtext)
        prov = (m.get("providers") or {}).get(PI_CCR_PROVIDER)
        ch2: List[dict] = []
        if prov is None:
            prov = {"api": "openai-completions", "baseUrl": "http://127.0.0.1:3456/v1",
                    "apiKey": _ccr_api_key(), "models": []}
            m.setdefault("providers", {})[PI_CCR_PROVIDER] = prov
            ch2.append({"where": f"models.json:providers.{PI_CCR_PROVIDER}", "from": "",
                        "to": "新建 provider（CCR :3456/v1）", "op": "add-section"})
            ids = []
        else:
            ids = [str(x.get("id") or "") for x in (prov.get("models") or [])]
        if model not in ids:
            entry = {"id": model, "name": model}
            # 不带 contextWindow 会被 pi 静默兜底成 128000（pi-web 回显 128k）
            entry.update(_pi_model_specs(model, m))
            prov.setdefault("models", []).append(entry)
            ch2.append({"where": f"models.json:providers.{PI_CCR_PROVIDER}.models",
                        "from": f"{len(ids)} 条", "to": f"{len(ids) + 1} 条（+ {model}）", "op": "add-key"})
            # 追加走定点插入（只插新元素，160+ 行原样不动）；provider 是新建的
            # 或形状不认识时回落整份重写。
            try:
                out.append((mp, jsonspan.append_object(
                    mtext, ("providers", PI_CCR_PROVIDER, "models"), entry), ch2))
                ch2_done = True
            except (KeyError, jsonspan.JsonSpanError):
                ch2_done = False
        else:
            ch2_done = False
        if ch2 and not ch2_done:
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
        # v0.13.182：尾随行内注释 `  # 说明` 必须原样留住。原写法 `(\S.*)$` 会把它
        # 一起吞掉 —— 改一次默认模型就悄悄删掉用户写在那行的理由（本机 config.yaml
        # 的 fallback_providers 里就有这种行内注释）。
        pat = re.compile(rf"^(\s*{key}:\s*)(\S.*?)(\s+#.*)?$", re.M)
        m = pat.search(body)
        if m:
            old = m.group(2).strip()
            body = pat.sub(lambda _m, v=val: f"{_m.group(1)}{v}{_m.group(3) or ''}", body, count=1)
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
        d = _json_load(_read_text(p))
    except (json.JSONDecodeError, jsonspan.JsonSpanError) as e:
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
    d = _json_load(text)
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
    follow_small = bool(old_small and old and old_small == old)
    if follow_small:
        d["small_model"] = model
        changes.append({"where": "opencode.json:small_model", "from": old_small, "to": model, "op": "set"})

    ids = list((prov.get("models") or {}).keys())
    add_model = mid not in ids
    if add_model:
        prov.setdefault("models", {})[mid] = {"name": mid.split("/", 1)[-1]}
        changes.append({"where": f"opencode.json:provider.{prov_id}.models.{mid}",
                        "from": "", "to": f"登记模型（原 {len(ids)} 条 → {len(ids) + 1} 条）",
                        "op": "add-key"})
    # v0.13.182：定点写（只替换/插入目标字节）。opencode.json 允许 JSONC 注释，
    # 整份 json.dumps 会把注释与用户缩进一起抹掉；这里未触碰字节逐字不变，
    # 键缺失/形状不认识才回落整份重写（老路径）。
    try:
        new, _old = jsonspan.set_string(text, ("model",), model)
        if follow_small:
            new, _old_small = jsonspan.set_string(new, ("small_model",), model)
        if add_model:
            new = jsonspan.append_member(new, ("provider", prov_id, "models"), mid,
                                         {"name": mid.split("/", 1)[-1]})
        return [(p, new, changes)]
    except (KeyError, jsonspan.JsonSpanError):
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
        d = _json_load(_read_text(p))
    except (json.JSONDecodeError, jsonspan.JsonSpanError) as e:
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
    d = _json_load(text)
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
    chg = [{"where": "settings.json:model.name", "from": old, "to": model, "op": "set"}]
    # v0.13.182：定点写。settings.json 里还有 securityScan/permissions/security 三段，
    # 整份重排会让用户看不出"其实只改了模型名"；键缺失才回落整份重写。
    try:
        new, _old = jsonspan.set_string(text, ("model", "name"), model)
        return [(p, new, chg)]
    except (KeyError, jsonspan.JsonSpanError):
        return [(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n", chg)]


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
        d = _json_load(_read_text(p))
    except (json.JSONDecodeError, jsonspan.JsonSpanError) as e:
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
    d = _json_load(text)
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
    add_param = model not in params
    if add_param:
        params[model] = []
        changes.append({"where": f"cli-config.json:modelParameters.{model}",
                        "from": "", "to": "空参数表", "op": "add-key"})
    d["modelParameters"] = params
    d["hasChangedDefaultModel"] = True
    changes.append({"where": "cli-config.json:hasChangedDefaultModel", "from": "", "to": "true", "op": "set"})
    # v0.13.182：定点写 8 处字节。这个文件里还有 permissions/display/editor/sandbox/
    # network/hooks 等用户面配置，整份重排会把它们一并卷进 diff（看不出只改了模型）。
    try:
        new = text
        for k in ("modelId", "displayModelId", "displayName", "displayNameShort"):
            new, _o = jsonspan.set_string(new, ("model", k), model)
        new, _o = jsonspan.set_string(new, ("selectedModel", "modelId"), model)
        new, _o = jsonspan.set_value(new, ("hasChangedDefaultModel",), True)
        if add_param:
            new = jsonspan.append_member(new, ("modelParameters",), model, [])
        return [(p, new, changes)]
    except (KeyError, jsonspan.JsonSpanError):
        return [(p, json.dumps(d, ensure_ascii=False, indent=2) + "\n", changes)]

# ── argv-only 型 agent（2026-10-25：omp 入册）────────────────────────
# 判据（实测，不是推断）：omp 的配置根 ~/.omp/agent 里是 sqlite（agent.db /
# models.db）+ models.yml 的 provider 清单，**没有** pi 那样的 settings.json
# 明文默认模型字段（`omp config list` 也列不出 defaultModel 键）⇒ 无可定点
# 改写的配置文件；而 `omp --help` 实测有 `--model=<value>`（fuzzy match:
# "opus" / "openai/gpt-5.2" 两种形状）⇒ 唯一合法落点就是 hub 拉起终端时注入。
# 这类 agent 在本文件里叫 argv_only：read 报告「无文件可读」，write 返回空写表，
# apply_model 只落 hub 侧持久化行（set_hub_model），不碰任何文件。
# ⚠ 为什么不给 omp 写 models.yml / 动 sqlite：models.yml 是用户手写的 provider
#   清单（含 !cmd 凭据只读取用），sqlite 是运行库 —— 两者都不属于「模型设置」
#   该动的面（「不破坏原生」裁定，见第二阶段注释）。


def _omp_read() -> dict:
    """omp 无默认模型配置文件可读：配置根 ~/.omp/agent 内是 sqlite 与 models.yml
    （provider 清单），没有 settings.json 那样的明文默认模型字段。

    note 里必须点出 `ccr/` 前缀（VALUE_PREFIX）：实测 `omp --model <裸ID>` 会把
    首段当 provider 名报「No API key found」，设置页存的值必须带 provider key。"""
    return {"current": "", "files": [],
            "note": "omp 的默认模型在 ~/.omp/agent 的 sqlite 内（无明文配置文件）；"
                    "hub 侧设置经终端 --model 注入生效（值带 ccr/ 前缀，新开终端才带）"}


def _omp_write(model: str):
    """argv-only：不落任何文件，返回空写表（hub 侧注入是唯一落点）。"""
    return []


#: agent_id -> {name, read, write, argv}
#: argv_only=True 的条目：writable 语义 = 「hub 侧可设置」（无配置文件可落）。
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
    # ── 2026-10-25 新增（用户报障「模型设置页面也没有自动发现并添加 dsh omp 等
    #    agent，应该是自动发现并添加」）──
    "omp": {"name": "oh-my-pi (omp)", "read": _omp_read, "write": _omp_write,
            "argv_only": True},
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
    # 2026-10-25 实测取证：dsh 是 profile 启动器，`dsh --profile tui --help` 的全部
    # 选项只有 --resume / --agent-preset / -h —— **没有 --model**（模型由 agent
    # preset 与 Web UI 内的选择决定）。故 hub 侧无处可注入，只能如实置灰。
    "dsh": "dsh TUI 无 --model（实测 dsh --profile tui --help 仅 --resume/--agent-preset）："
           "模型由 agent preset 与 Web UI 内选择决定，hub 无注入入口",
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
    want = hub_model(agent_id)
    spec = SPECS.get(agent_id)
    mode = WRITE_MODE.get(agent_id, "argv")
    cat = catalog_ids()
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
        _ccr_base() + "/v1/chat/completions", data=body, method="POST",
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
