"""各 agent 的模型读写器 + 定点改写工具（2026-10-31 从 modelcfg.py 拆出，B4 片2）。

这一族是**同构写手**（claude/codex/pi/grok/hermes/opencode/qoder/cursor/omp…），
刻意留在一个文件：拆成「每 agent 一个文件」会让「改一处判据要开 20 个文件」。
依赖：`modelcfg_shape`（常量与错误）；需要网关凭据时**惰性** import modelcfg（防循环）。
"""
from __future__ import annotations

import json
import os
import shutil
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

import jsonspan

from modelcfg_shape import *  # noqa: F401,F403 —— 常量与 ModelCfgError 的形状单一真相源


def _api_key() -> str:
    """惰性取 CCR 凭据（定义在 modelcfg.py：它要读网关配置链，属网络侧）。"""
    import modelcfg as _mc
    return _mc._ccr_api_key()

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
                    "apiKey": _api_key(), "models": []}
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
    api_key = existing or _api_key()
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

