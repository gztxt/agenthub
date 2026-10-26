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
  `cat|head|tail|less|bat` 读同一个文件、`cd .../skills/<name> && head SKILL.md`。
- 不算：`grep`/`rg`/`ls`/`find`/`echo` 里出现 SKILL.md（那是检索与写代码，不是使用）、
  写文件、以及任何只有文件名没有读取动作的提及。
- 每条事件带 `via='transcript'`、`evidence`（skill_tool/read/bash）、`session`、`call_id`，
  与 hub 通道的 `via='hub'` 可区分。

**七个来源、三种存储形态**（2026-10-27 补后三家）
| caller | 位置 | 形态 | 工具调用长在哪 |
|---|---|---|---|
| codebuddy | `~/.codebuddy/projects/**/*.jsonl` | JSONL | 顶层 `function_call` |
| claude | `~/.claude/projects/**/*.jsonl` | JSONL | `message.content[].tool_use` |
| pi | `~/.pi/agent/sessions/**/*.jsonl` | JSONL | `message.content[].toolCall` |
| codex | `~/.codex/sessions/**/*.jsonl` | JSONL | `message.content[].tool_use` |
| grok | `~/.grok/sessions/**/chat_history.jsonl` | JSONL | `assistant.tool_calls[]` |
| jcode | `~/.jcode/sessions/*.json` | 单文件 JSON（会话数组） | `messages[].content[].tool_use` |
| opencode | `~/.local/share/opencode/opencode.db` | SQLite | `part.data` 里 `type=tool.state.input` |

⚠ grok 只认 `chat_history.jsonl`：同目录还有 `events.jsonl`/`updates.jsonl`/`rewind_points.jsonl`，
扫全部 `*.jsonl` 会把同一个调用记三遍（同一个工具 id 出现在事件流与历史里）。

**幂等**
- JSONL：按字节 offset 增量，半行不推进（agent 正在写）。
- jcode 单文件：按消息数增量，size 变了从**最后一条**重读（流式写入会改写末条）；
  去重键兜住重复。
- opencode SQLite：按 `part.rowid` 水位增量，首次按天数截断；库被重建（max rowid 变小）时归零重扫。
- 三者共用一个 `(caller, call_id)` 去重键池（每文件/库最近 5000 条）。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import re
import time
from typing import Any, Dict, Iterable, List, Tuple

#: JSONL 采集源：caller -> 转录根目录。全部本机只读路径。
ROOTS: Dict[str, str] = {
    "codebuddy": os.path.expanduser("~/.codebuddy/projects"),
    "claude": os.path.expanduser("~/.claude/projects"),
    "pi": os.path.expanduser("~/.pi/agent/sessions"),
    "codex": os.path.expanduser("~/.codex/sessions"),
    "grok": os.path.expanduser("~/.grok/sessions"),
}

#: 单文件 JSON 采集源（不是 JSONL）。jcode 一个会话一个文件，消息在 `messages[]`。
JSON_ROOTS: Dict[str, str] = {
    "jcode": os.path.expanduser("~/.jcode/sessions"),
}

#: SQLite 采集源。opencode 1.x 把 part（工具调用）存在这里。
SQLITE_SOURCES: Dict[str, str] = {
    "opencode": os.path.expanduser("~/.local/share/opencode/opencode.db"),
}

#: 每路 JSONL 的文件名模式。grok 必须收窄：同目录的 events/updates 会重复记同一次调用。
GLOBS: Dict[str, str] = {"grok": "**/chat_history.jsonl"}
DEFAULT_GLOB = "**/*.jsonl"

#: 只扫最近 N 天还在动的转录（首次全扫的规模控制；历史账不该由本模块补）。
DEFAULT_DAYS = 30

#: 点名技能的工具有哪些（小写比较）。
SKILL_TOOLS = {"skill", "hub_skill_invoke", "hub_skill_invoke_tool"}

#: 读取类工具（小写）。grok 叫 read_file，opencode/jcode 叫 read。
READ_TOOLS = {"read", "read_file", "view", "readfile"}

#: 执行命令类工具（小写）。grok 的 `run_terminal_command` 有一个历史拼写错误
#: `run_termial_command`（实测真实转录里存在 1 次），两个都收，否则那条永远漏采。
SHELL_TOOLS = {"bash", "shell", "exec", "run", "powershell", "terminal",
               "run_terminal_command", "run_termial_command"}

#: 读命令白名单。刻意不含 grep/rg/awk——它们是检索，不是使用。
READ_CMDS = ("cat", "head", "tail", "less", "more", "bat", "sed")

#: `.../skills/<name>/SKILL.md`。名字里允许中文与点（crawl4ai、techdocs-search）。
_PATH_RE = re.compile(r"skills/([^/\s\"'`()]+)/SKILL\.md", re.I)
#: `.../skills/<name>`（后面可能跟 `&& head SKILL.md`）。grok/jcode 常见写法是
#: 先 `cd .../skills/<name>` 再读裸文件名，路径不连续，单靠上一条正则命中不了。
_DIR_RE = re.compile(r"(?<![A-Za-z0-9])skills/([^/\s\"'`()]+)", re.I)
#: 读命令 + 同一个命令段里的 SKILL.md（不跨 `;` `&&` `|`）。
_READ_FILE_RE = re.compile(
    r"(?:^|[|;&]\s*|\s)(?:%s)\s+[^;&|]*SKILL\.md" % "|".join(READ_CMDS), re.I)

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


def _skill_from_command(blob: str) -> str:
    """从一段命令里判「读了哪个技能」。

    两种真实写法都要认：
      ① `cat /x/skills/foo/SKILL.md`（路径连续）
      ② `cd /x/skills/foo && head -30 SKILL.md`（grok/jcode 实测写法，路径断开）
    **只认读命令**：② 里如果是 `grep ... SKILL.md`，`_READ_FILE_RE` 不命中，
    与既有「检索不算使用」的口径一致。
    """
    if not blob or "skill.md" not in blob.lower():
        return ""
    got = _skill_from_text(blob)
    if got:
        return got
    for m in _DIR_RE.finditer(blob):
        name = m.group(1)
        tail = blob[m.end():]
        if _READ_FILE_RE.search(tail):
            return name
    return ""


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
    got = _skill_from_text(blob) or _skill_from_command(blob)
    if not got:
        return []

    if tool in READ_TOOLS:
        return [(got, "read")]
    if tool in SHELL_TOOLS:
        # 只认真正「把文件内容读出来」的命令；grep/ls/find/echo 全部排除。
        # 连续路径走原判据（读命令在路径前 80 字内），`cd ... && head SKILL.md`
        # 走 `_skill_from_command` 已经验过的读命令分支。
        low = blob.lower()
        head = low.split("skills/" + got.lower() + "/skill.md", 1)[0][-80:]
        if any(re.search(r"(^|[|;&\s])%s(\s|$)" % c, head) for c in READ_CMDS):
            return [(got, "bash")]
        if got and _skill_from_command(blob) and re.search(
                r"(?:^|[|;&]\s*|\s)(?:%s)\s+[^;&|]*SKILL\.md" % "|".join(READ_CMDS),
                blob, re.I):
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

    # grok：assistant 行带 tool_calls[]（与 OpenAI 同形，arguments 是 JSON 字符串）。
    # 放在最前，因为它没有 `message` 包装；其它家不会同时带这个键。
    tcs = d.get("tool_calls")
    if isinstance(tcs, list):
        for tc in tcs:
            if not isinstance(tc, dict):
                continue
            cid = str(tc.get("id") or tc.get("callId") or "")
            for n, ev in _from_tool(tc.get("name"), tc.get("arguments"), cid):
                out.append((n, ev, cid))
        return out

    if t == "function_call":
        cid = str(d.get("callId") or d.get("id") or "")
        for n, ev in _from_tool(d.get("name"), d.get("arguments"), cid):
            out.append((n, ev, cid))
        return out

    # opencode：part.data 的合成形状 {"type":"tool","tool":…,"state":{"input":…}}
    if t == "tool":
        cid = str(d.get("callID") or d.get("id") or "")
        st = d.get("state") if isinstance(d.get("state"), dict) else {}
        for n, ev in _from_tool(d.get("tool"), st.get("input"), cid):
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

    # jcode：整条消息直接就是 {"role":…,"content":[{"type":"tool_use",…}]}
    content = d.get("content")
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


def _emit_entry(d: Dict[str, Any], keys: set, caller: str, session: str,
                dry_run: bool, result: Dict[str, Any], fallback: str) -> int:
    """从一条转录里抽事件并落库；返回本条目新增条数。"""
    got = 0
    for name, evidence, cid in _events_in_entry(d):
        key = cid or hashlib.sha1(
            ("%s|%s|%s|%s" % (name, evidence, session, fallback)).encode()).hexdigest()
        if key in keys:
            continue
        keys.add(key)
        got += 1
        result["events"] += 1
        result["by_caller"][caller] = result["by_caller"].get(caller, 0) + 1
        if not dry_run:
            _record(name, caller, evidence, session, cid)
    return got


def _scan_jsonl(caller: str, root: str, cut: float, files: Dict[str, Any],
                dry_run: bool, result: Dict[str, Any]) -> None:
    """JSONL 源：按字节 offset 增量，末尾半行不推进（agent 正在写）。"""
    pattern = GLOBS.get(caller, DEFAULT_GLOB)
    for path in glob.glob(os.path.join(root, pattern), recursive=True):
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
        processed = offset
        try:
            # 二进制读：文本模式下不能边迭代边 tell()（Python 会抛
            # "telling position disabled by next() call"），而增量 offset 必须是字节位。
            with open(path, "rb") as fh:
                fh.seek(offset)
                while True:
                    raw = fh.readline()
                    if not raw:
                        break
                    if not raw.endswith(b"\n"):
                        # 半行：agent 正在写这个文件。**不推进 offset**，
                        # 否则这半行被跳过一次就永久丢了（只在 EOF 出现，代价是下轮重读）。
                        break
                    processed = fh.tell()
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
                    _emit_entry(d, keys, caller, os.path.basename(path), dry_run,
                                result, line[:200])
        except Exception:
            result["errors"] += 1
            continue
        result["files"] += 1
        files[path] = {"offset": processed, "size": st.st_size,
                       "keys": list(keys)[-KEYS_PER_FILE:]}


def _scan_json(caller: str, root: str, cut: float, files: Dict[str, Any],
               dry_run: bool, result: Dict[str, Any]) -> None:
    """单文件 JSON 源（jcode）：一个会话一个文件，工具调用在 `messages[].content[]`。

    不按字节 offset：JSON 数组不能从中间切开。按**消息数**增量，且 size 变了要
    从最后一条重读——流式写入会改写末条消息（先出 text 块，后补 tool_use 块）。
    """
    for path in glob.glob(os.path.join(root, "*.json")):
        try:
            st = os.stat(path)
        except OSError:
            continue
        if st.st_mtime < cut:
            continue
        ent = files.get(path) or {"msgs": 0, "size": 0, "keys": []}
        if st.st_size == ent.get("size"):
            result["skipped"] += 1
            continue
        try:
            with open(path, "r", encoding="utf-8", errors="replace") as fh:
                d = json.load(fh)
        except Exception:
            result["errors"] += 1
            continue
        msgs = d.get("messages") if isinstance(d, dict) else None
        if not isinstance(msgs, list):
            result["errors"] += 1
            continue
        keys = set(ent.get("keys") or [])
        start = int(ent.get("msgs") or 0)
        if len(msgs) < start:
            start, keys = 0, set()          # 文件被重写（更短）⇒ 从头扫，靠 key 去重
        elif start:
            start -= 1                      # 末条可能被流式补全 ⇒ 重读一次
        for idx in range(start, len(msgs)):
            m = msgs[idx]
            if not isinstance(m, dict):
                continue
            _emit_entry(m, keys, caller, os.path.basename(path), dry_run,
                        result, "msg:%d" % idx)
        result["files"] += 1
        files[path] = {"msgs": len(msgs), "size": st.st_size,
                       "keys": list(keys)[-KEYS_PER_FILE:]}


def _scan_sqlite(caller: str, db_path: str, cut: float, files: Dict[str, Any],
                 dry_run: bool, result: Dict[str, Any]) -> None:
    """SQLite 源（opencode）：工具调用是 `part.data` 里的 `{"type":"tool",...}`。

    增量按 `part.rowid` 水位（opencode 只插入不重排）。库被重建时 max(rowid) 会变小
    ⇒ 归零重扫；去重键保证重扫不重复记账。**只读打开**（mode=ro），绝不给采集写权限。
    """
    if not db_path or not os.path.isfile(db_path):
        return
    try:
        import sqlite3
        from pathlib import Path
        uri = Path(db_path).resolve().as_uri() + "?mode=ro"
        con = sqlite3.connect(uri, uri=True, timeout=2.0)
    except Exception:
        result["errors"] += 1
        return
    key = "sqlite:" + db_path
    ent = files.get(key) or {"rowid": 0, "keys": []}
    keys = set(ent.get("keys") or [])
    last = int(ent.get("rowid") or 0)
    try:
        maxrow = con.execute("SELECT COALESCE(MAX(rowid),0) FROM part").fetchone()[0]
        if maxrow < last:
            last, keys = 0, set()           # 库被重建 ⇒ 从头扫
        # opencode 的 time_created 是毫秒（实测 1790180884109）；兼容秒级库。
        ts_max = con.execute("SELECT COALESCE(MAX(time_created),0) FROM part").fetchone()[0] or 0
        cut_ts = cut * 1000.0 if ts_max > 1e11 else cut
        cur = con.execute(
            "SELECT rowid, data, time_created FROM part WHERE rowid > ? AND time_created >= ? "
            "ORDER BY rowid", (last, cut_ts))
        for rowid, data, _ts in cur:
            try:
                d = json.loads(data)
            except Exception:
                continue
            if not isinstance(d, dict) or d.get("type") != "tool":
                continue
            _emit_entry(d, keys, caller, os.path.basename(db_path), dry_run,
                        result, "rowid:%s" % rowid)
            last = int(rowid)
        result["files"] += 1
        files[key] = {"rowid": max(last, int(maxrow)), "keys": list(keys)[-KEYS_PER_FILE:]}
    except Exception:
        result["errors"] += 1
    finally:
        try:
            con.close()
        except Exception:
            pass


def scan(roots: Dict[str, str] | None = None, days: int = DEFAULT_DAYS,
         state_path: str | None = None, dry_run: bool = False,
         now: float | None = None, json_roots: Dict[str, str] | None = None,
         sqlite_sources: Dict[str, str] | None = None) -> Dict[str, Any]:
    """增量扫描一轮。返回本轮证据（供后台日志与 /health 断言用）。

    `dry_run=True` 只解析不落库（测试与首次摸底用）。
    不传 `roots` 时使用全部内置源（JSONL + JSON + SQLite）；传了 `roots`
    （测试夹具）时，JSON/SQLite 源默认不启用，避免测试去打真实家目录。
    """
    use_defaults = roots is None
    roots = ROOTS if roots is None else roots
    json_roots = (JSON_ROOTS if json_roots is None and use_defaults else json_roots) or {}
    sqlite_sources = (SQLITE_SOURCES if sqlite_sources is None and use_defaults
                      else sqlite_sources) or {}
    state_path = state_path or os.environ.get("HUB_SKILL_SCAN_STATE") or STATE_DEFAULT
    now = now if now is not None else time.time()
    cut = now - max(1, int(days)) * 86400
    state = _load_state(state_path)
    files = state["files"]
    result: Dict[str, Any] = {"files": 0, "skipped": 0, "events": 0, "errors": 0,
                              "by_caller": {}, "dry_run": bool(dry_run),
                              "sources": {"jsonl": sorted(roots), "json": sorted(json_roots),
                                          "sqlite": sorted(sqlite_sources)}}

    for caller, root in roots.items():
        if root and os.path.isdir(root):
            _scan_jsonl(caller, root, cut, files, dry_run, result)
    for caller, root in json_roots.items():
        if root and os.path.isdir(root):
            _scan_json(caller, root, cut, files, dry_run, result)
    for caller, db_path in sqlite_sources.items():
        _scan_sqlite(caller, db_path, cut, files, dry_run, result)

    if not dry_run:
        state["last_run"] = now
        state["last_result"] = {k: v for k, v in result.items() if k != "dry_run"}
        try:
            _save_state(state_path, state)
        except Exception:
            pass
    return result
