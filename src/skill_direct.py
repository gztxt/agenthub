"""第二数据源：从各家 agent 的会话转录里采集**真实发生的技能调用**。

**为什么需要它（根因）**
`skill_usage` 原来的记账口径是 `source='rest'`——只有 hub 自己的三个显式路由
（`/api/skill/invoke`、`/api/skill/auto`、`/store/acquire`）会落 `skill.invoke`。
但真实使用路径是：注入层只推「技能名 + 一句用途」（`hub_skill_inject.py` / `hub-facade.ts`），
**正文由 agent 自己 Read 磁盘上的 SKILL.md**，这一步完全不经过 hub。于是：

- 全机最热的用法（agent 读技能）⇒ hub 一条账都没有；
- 面板上「调用记录」只有 probe / 测试 caller，用户看到的就是「没显示所有技能调用」。

判例 228 只补了 401 文案与空状态，**数据生产者没动**——那正是「治标不治本」。
本模块补的就是生产者：直接读会话转录里的工具调用，谁读了哪个技能就记一条。

**采集什么 / 不采集什么（防虚高）**
- 算：`Skill` 工具调用（点名技能）、`Read`/`read` 打开 `.../skills/<name>/SKILL.md`、
  `cat|head|tail|less|bat` 读同一个文件。
- 不算：`grep`/`rg`/`ls`/`find`/`echo` 里出现 SKILL.md（那是检索与写代码，不是使用）、
  写文件、以及任何只有文件名没有读取动作的提及。
- 每条事件带 `via='transcript'`、`evidence`（skill_tool/read/bash）、`session`、`call_id`，
  与 hub 通道的 `via='hub'` 可区分。

**幂等**
按文件增量读（state 里存 offset/size），并保留最近 5000 个 `(caller, call_id)` 去重键；
文件被重写（size 变小）时从 0 重扫，靠去重键避免重复记账。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Tuple

#: 采集源：caller -> 转录根目录。全部本机只读路径。
ROOTS: Dict[str, str] = {
    "codebuddy": os.path.expanduser("~/.codebuddy/projects"),
    "claude": os.path.expanduser("~/.claude/projects"),
    "pi": os.path.expanduser("~/.pi/agent/sessions"),
    "codex": os.path.expanduser("~/.codex/sessions"),
}

#: 只扫最近 N 天还在动的转录（首次全扫的规模控制；历史账不该由本模块补）。
DEFAULT_DAYS = 30

#: 点名技能的工具有哪些（小写比较）。
SKILL_TOOLS = {"skill", "hub_skill_invoke", "hub_skill_invoke_tool"}

#: 读取类工具（小写）。
READ_TOOLS = {"read", "read_file", "view", "readfile"}

#: 读命令白名单。刻意不含 grep/rg/awk——它们是检索，不是使用。
READ_CMDS = ("cat", "head", "tail", "less", "more", "bat", "sed")

#: `.../skills/<name>/SKILL.md`。名字里允许中文与点（crawl4ai、techdocs-search）。
_PATH_RE = re.compile(r"skills/([^/\s\"'`()]+)/SKILL\.md", re.I)

#: 去重键保留上限（每文件）
KEYS_PER_FILE = 5000
#: 状态文件
STATE_DEFAULT = os.path.expanduser("~/.cache/agenthub/skill_scan_state.json")


def _maybe_json(v: Any) -> Any:
    if isinstance(v, str):
        s = v.strip()
        if s[:1] in "{[":
            try:
                return json.loads(s)
            except Exception:
                return v
    return v


def _walk_strings(v: Any, out: List[str], depth: int = 0) -> None:
    """收集参数里所有字符串（含嵌套 dict/list）。深度设上限防病态结构。"""
    if depth > 6:
        return
    if isinstance(v, str):
        out.append(v)
    elif isinstance(v, dict):
        for x in v.values():
            _walk_strings(x, out, depth + 1)
    elif isinstance(v, (list, tuple)):
        for x in v:
            _walk_strings(x, out, depth + 1)


def _skill_from_text(text: str) -> str:
    m = _PATH_RE.search(text or "")
    return m.group(1) if m else ""


def _from_tool(name: Any, args: Any, call_id: str) -> List[Tuple[str, str]]:
    """从一个工具调用里判「用了哪个技能」。返回 [(技能名, 证据类型)]。"""
    tool = str(name or "").strip().lower()
    if not tool:
        return []
    args = _maybe_json(args)
    if tool in SKILL_TOOLS:
        # Skill 工具：参数里通常是 {skill: "name"} / {name: "name"} / {id: "..."}
        if isinstance(args, dict):
            for k in ("skill", "name", "id", "skill_name", "command"):
                v = args.get(k)
                if isinstance(v, str) and v.strip() and "/" not in v.strip():
                    return [(v.strip(), "skill_tool")]
        strs: List[str] = []
        _walk_strings(args, strs)
        for s in strs:
            got = _skill_from_text(s)
            if got:
                return [(got, "skill_tool")]
        return []

    strs = []
    _walk_strings(args, strs)
    if not strs:
        return []
    blob = "\n".join(strs)
    got = _skill_from_text(blob)
    if not got:
        return []

    if tool in READ_TOOLS:
        return [(got, "read")]
    if tool in ("bash", "shell", "exec", "run", "powershell", "terminal"):
        # 只认真正「把文件内容读出来」的命令；grep/ls/find/echo 全部排除
        low = blob.lower()
        head = low.split("skills/" + got.lower() + "/skill.md", 1)[0][-80:]
        if any(re.search(r"(^|[|;&\s])%s(\s|$)" % c, head) for c in READ_CMDS):
            return [(got, "bash")]
    return []


def _events_in_entry(d: Dict[str, Any]) -> List[Tuple[str, str, str]]:
    """从一行转录里抽 (技能名, 证据, call_id)。兼容四家格式。

    - codebuddy: `{"type":"function_call","name":...,"arguments":"<json str>","callId":...}`
    - claude:    `{"type":"assistant","message":{"content":[{"type":"tool_use",...}]}}`
    - pi:        `{"type":"message","message":{"content":[{"type":"toolCall",...}]}}`
    - codex:     结构随版本变，走同一条 message.content 分支 + function_call 兼容
    """
    out: List[Tuple[str, str, str]] = []
    t = d.get("type")

    if t == "function_call":
        cid = str(d.get("callId") or d.get("id") or "")
        for n, ev in _from_tool(d.get("name"), d.get("arguments"), cid):
            out.append((n, ev, cid))
        return out

    m = d.get("message")
    if isinstance(m, dict):
        content = m.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict):
                    continue
                if b.get("type") not in ("tool_use", "toolCall", "function_call"):
                    continue
                cid = str(b.get("id") or b.get("callId") or "")
                args = b.get("input", b.get("arguments"))
                for n, ev in _from_tool(b.get("name"), args, cid):
                    out.append((n, ev, cid))
    return out


def _load_state(path: str) -> Dict[str, Any]:
    try:
        with open(path, "r", encoding="utf-8") as fh:
            d = json.load(fh)
        if isinstance(d, dict) and isinstance(d.get("files"), dict):
            return d
    except Exception:
        pass
    return {"version": 1, "files": {}, "last_run": None, "last_result": None}


def _save_state(path: str, state: Dict[str, Any]) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(state, fh, ensure_ascii=False)
    os.replace(tmp, path)


def _record(name: str, caller: str, evidence: str, session: str, call_id: str) -> None:
    """落一条 `skill.invoke`。**失败不抛**：采集是增益，不能拖垮后台循环。"""
    try:
        import db
        db.log_profile_event(
            "scan", "skill.invoke", "success", 0, None,
            {"name": name, "caller": caller, "via": "transcript",
             "evidence": evidence, "session": session, "call_id": call_id,
             "mode": "direct_read"},
        )
    except Exception:
        pass


def scan(roots: Dict[str, str] | None = None, days: int = DEFAULT_DAYS,
         state_path: str | None = None, dry_run: bool = False,
         now: float | None = None) -> Dict[str, Any]:
    """增量扫描一轮。返回本轮证据（供后台日志与 /health 断言用）。

    `dry_run=True` 只解析不落库（测试与首次摸底用）。
    """
    roots = roots if roots is not None else ROOTS
    state_path = state_path or os.environ.get("HUB_SKILL_SCAN_STATE") or STATE_DEFAULT
    now = now if now is not None else time.time()
    cut = now - max(1, int(days)) * 86400
    state = _load_state(state_path)
    files = state["files"]
    result: Dict[str, Any] = {"files": 0, "skipped": 0, "events": 0, "errors": 0,
                              "by_caller": {}, "dry_run": bool(dry_run)}

    for caller, root in roots.items():
        if not root or not os.path.isdir(root):
            continue
        for path in glob.glob(os.path.join(root, "**", "*.jsonl"), recursive=True):
            try:
                st = os.stat(path)
            except OSError:
                continue
            if st.st_mtime < cut:
                continue
            ent = files.get(path) or {"offset": 0, "size": 0, "keys": []}
            if st.st_size == ent.get("size"):
                result["skipped"] += 1
                continue
            offset = 0 if st.st_size < int(ent.get("offset") or 0) else int(ent.get("offset") or 0)
            keys = set(ent.get("keys") or [])
            got = 0
            try:
                # 二进制读：文本模式下不能边迭代边 tell()（Python 会抛
                # "telling position disabled by next() call"），而增量 offset 必须是字节位。
                with open(path, "rb") as fh:
                    fh.seek(offset)
                    for raw in fh:
                        line = raw.decode("utf-8", "replace").strip()
                        if not line:
                            continue
                        if "SKILL.md" not in line and "skill" not in line.lower():
                            continue
                        try:
                            d = json.loads(line)
                        except Exception:
                            continue
                        if not isinstance(d, dict):
                            continue
                        for name, evidence, cid in _events_in_entry(d):
                            key = cid or hashlib.sha1(
                                ("%s|%s|%s" % (name, evidence, line[:200])).encode()).hexdigest()
                            if key in keys:
                                continue
                            keys.add(key)
                            got += 1
                            result["events"] += 1
                            result["by_caller"][caller] = result["by_caller"].get(caller, 0) + 1
                            if not dry_run:
                                _record(name, caller, evidence, os.path.basename(path), cid)
                    offset = fh.tell()
            except Exception:
                result["errors"] += 1
                continue
            result["files"] += 1
            files[path] = {"offset": offset, "size": st.st_size,
                           "keys": list(keys)[-KEYS_PER_FILE:]}

    if not dry_run:
        state["last_run"] = now
        state["last_result"] = {k: v for k, v in result.items() if k != "dry_run"}
        try:
            _save_state(state_path, state)
        except Exception:
            pass
    return result
