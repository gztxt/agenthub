"""会话仓库适配层（v0.13.0）：只读把各 agent 自己在磁盘上的历史会话归一成 Item。

安全模型（依据 spec §2 的实测事实）：
- 只读：文件 open(..., errors='ignore')；sqlite 一律 "file:<p>?mode=ro" + uri=True。不写、不删、不 chmod。
- 标题＝用户问题原文（中文），过 mask_title() 遮蔽口令后外发；字母 id 一律不进标题。
- resume argv 由本模块模板拼装：客户端只能提供 session_id，必须过 id_re + 实盘存在双校验。
"""
from __future__ import annotations

import hashlib
import importlib
import json
import re
import sqlite3
import time
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Optional, Tuple
from urllib.parse import unquote

HOME = Path.home()
CACHE_TTL_S = 15          # 菜单 30s 轮询，15s 缓存足够去重
HEAD_CHARS = 262144       # 大 jsonl 只读头部这么多字符找标题（实测 grok 单是 <user_info> 一行就 33KB，64KB 窗口不够）
HARD_BUDGET_S = 1.5       # 单次 list_history 硬预算，超时返回已读到的 + note

UUID_RE = re.compile(r"\A[0-9a-f]{8}(-[0-9a-f]{4}){3}-[0-9a-f]{12}\Z", re.I)
JCODE_RE = re.compile(r"\Asession_[a-z]+_\d{13}_[0-9a-f]{6,16}\Z")
HERMES_RE = re.compile(r"\A\d{8}_\d{6}_[0-9a-f]{6}\Z")
OPENCODE_RE = re.compile(r"\Ases_[0-9a-zA-Z]{6,64}\Z")   # 实测 1.18.32：ses_ + 22 位 base62
OPENCODE_DB = HOME / ".local" / "share" / "opencode" / "opencode.db"
# cursor（Cursor Agent CLI，实装名 cursor-agent）—— 会话分散在两处，见 _t_cursor：
#   ① ~/.cursor/projects/<slug>/agent-transcripts/<agentId>/<agentId>.jsonl  转录（有正文）
#   ② ~/.cursor/chats/<md5(cwd)>/<agentId>/meta.json                         元信息（有 cwd/时间）
#   同目录下的 store.db 是 zlib+XOR 加密的正文库（逐会话 blobEncryptionKey），**不解密**。
#   ⚠️ 两个桶名别搞混（2026-10-07 实测）：projects/ 下是 **slug**（非字母数字→'-'、
#   合并连续'-'、去首尾，取自其 bundle 的 workspace-paths.js），chats/ 下是 **md5(原样 cwd)**。
#   我一度以为后者也是 md5(slug)——单个样本碰巧相等，全量一测 60/60 vs 0/60 直接证伪。
#   本模块**不需要**这两个映射：条目自带 cwd，按 id 定位走 `*/{sid}/meta.json` 通配。
CURSOR_PROJECTS = HOME / ".cursor" / "projects"
CURSOR_CHATS = HOME / ".cursor" / "chats"
# 会话 id 形状实测（2026-10-07，61/61）：标准带连字符 UUID —— 复用 UUID_RE，
# 不另立正则。⚠️ 我一度按「32 位无连字符 hex」写，真机一跑 61 条**全被形状门拒掉**
# （表现为提交 cursor 会话时报 400 "session_id 形状非法"），故此处钉死为 UUID_RE。
# 注：store.db 的加密 meta 里那个 agentId 也是同一个带连字符的 UUID，不是另一种形状。

# 实测：grok 把用户真问题包在 <user_query> 里（chat_history.jsonl 第 4 行）；
# 这几个前缀是注入块（拆不到 user_query 时必须跳过，否则标题会变成 system prompt）。
_UQ = re.compile(r"<user_query>\s*(.*?)\s*</user_query>", re.S)
_INJECT_PREFIX = ("<user_info>", "<system-reminder>", "<command-name>", "<command-message>",
                  "<local-command", "<task-notification>", "<permissions")

# ── 口令遮蔽 ────────────────────────────────────────────────────────────────
# ① KV 形：允许 key 带前缀（实测本机 CCR 入口是 `?ccr_web_token=<值>`，\b 在 `_` 处不成立，
#    所以不能只写 \btoken\b）；② 28+ 位 base64/hex 裸串且含数字。
# 刻意**不**糊含 '-'/'_' 的长串：那会把 UUID、时间戳备份名一并糊掉（单测 test_uuid_title_not_mangled 锁死）。
_KV = re.compile(r"(?i)([A-Za-z0-9_\-]*(?:token|api[_-]?key|apikey|secret|passwd|password|authorization|bearer))"
                 r"\s*[=:]\s*[^\s,;'\"）)]+")
_BLOB = re.compile(r"\b(?=[A-Za-z0-9+/=]{28,}\b)[A-Za-z0-9+/=]*[0-9][A-Za-z0-9+/=]*\b")


def mask_title(text: Optional[str]) -> str:
    t = _KV.sub(lambda m: m.group(1) + "=<masked>", text or "")
    t = _BLOB.sub("<masked>", t)
    return re.sub(r"\s+", " ", t).strip()[:120]


def _iso(ts: Optional[str]) -> int:
    """ISO8601（grok/jcode 混用 秒/微秒/纳秒 + Z）→ epoch 秒；解析失败返回 0。"""
    if not ts:
        return 0
    s = str(ts).strip().replace("Z", "+00:00")
    s = re.sub(r"(\.\d{6})\d+", r"\1", s)          # 纳秒截到微秒：fromisoformat 不吞 9 位小数
    try:
        return int(datetime.fromisoformat(s).timestamp())
    except Exception:  # noqa: BLE001
        return 0


def _head_lines(p: Path, n: int = 400) -> List[dict]:
    """只读文件头 HEAD_CHARS，返回解析成功的 JSON 行（大 jsonl 不整读）"""
    try:
        with open(p, "r", errors="ignore") as f:
            blob = f.read(HEAD_CHARS)
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    for line in blob.splitlines()[:n]:
        try:
            out.append(json.loads(line))
        except Exception:  # noqa: BLE001
            continue
    return out


def _load(p: Path):
    """读整份 JSON 并确保关句柄（单测跑出过 ResourceWarning：一次扫 60 个候选会漏 fd）"""
    with open(p, "r", errors="ignore") as f:
        return json.load(f)


def _text_of(c) -> str:
    """把 message content 拉成纯文本：实测三种形态——str（grok 顶层 / claude message）、
       [{'type':'text','text':…}] 块列表（jcode、部分 claude）、其它形状一律当无文本。"""
    if isinstance(c, str):
        return c.strip()
    if isinstance(c, list):
        return " ".join(str(x.get("text", "")).strip() for x in c
                        if isinstance(x, dict) and x.get("text")).strip()
    return ""


def _first_user_text(objs: List[dict], *, jcode: bool = False) -> str:
    """取「用户第一条问题原文」——比 agent 自生成的摘要更合需求（D2）。
       实测三类干扰，逐个定策：
       ① grok：真问题被包在 `<user_query>…</user_query>` 里（实测第 4 行），必须先拆标签；
          而 `<user_info>` / `<system-reminder>` 是注入，拆不到 user_query 就跳过；
       ② claude/qoder：`<command-name>` 等标签内容是命令回显/meta，无 user_query ⇒ 跳过；
       ③ jcode：`display_role == 'system'` 是注入（不过滤就命中 system prompt）。
    """
    for d in objs:
        if jcode:
            if d.get("role") != "user" or d.get("display_role") == "system":
                continue
            s = _text_of(d.get("content"))
        else:
            if d.get("type") != "user" or d.get("isSidechain") or d.get("isMeta"):
                continue
            # 字段位置两家不同：grok 在顶层 content，claude/qoder 在 message.content
            raw = d.get("content") if d.get("content") is not None else (d.get("message") or {}).get("content")
            s = _text_of(raw)
        if not s:
            continue
        m = _UQ.search(s)
        if m:
            got = m.group(1).strip()
            if got:
                return got
            continue
        if s.startswith(_INJECT_PREFIX) or s.startswith("<"):
            continue
        return s
    return ""


# ── 各仓库适配器：统一返回 (items, note)。note 为中文，供前端空态/降级直显 ──────
def _t_grok(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """跳目录口径（用户 09-22 裁定）：不按画像 cwd 分桶，取全局时间最近的 limit 条。
       桶结构 ~/.grok/sessions/<URL编码cwd>/<uuid>/summary.json，实测 7 桶共 136 条。"""
    root = HOME / ".grok" / "sessions"
    if not root.is_dir():
        return [], "grok 无会话仓库"
    files = sorted(root.glob("*/*/summary.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    if not files:
        return [], "grok 无历史会话"
    items: List[dict] = []
    for f in files:
        if time.time() - t0 > HARD_BUDGET_S:
            return items, "扫描超时，仅显示已读到的条目"
        try:
            d = _load(f)
        except Exception:  # noqa: BLE001
            continue
        sess = f.parent
        info = d.get("info") or {}
        # D2：标题＝用户问题原文优先；session_summary 是 agent 自生成的摘要，实测会出英文
        # （'Agent Hub sidebar menu name status badge layout fixes'），只能当兼底。
        title = _first_user_text(_head_lines(sess / "chat_history.jsonl", 20)) or \
            (d.get("session_summary") or "").strip()
        scwd = info.get("cwd") or unquote(sess.parent.name)      # 缺字段时从桶名反解
        items.append({"agent": "grok", "id": info.get("id") or sess.name,
                      "title": mask_title(title) or "未命名会话", "ts": _iso(d.get("updated_at")),
                      "msgs": d.get("num_messages"), "cwd": scwd})
        if len(items) >= limit:
            break
    return items, ""


def _cwd_of_records(objs: List[dict]) -> str:
    """claude/qoder 的 jsonl 每行都带 cwd（实测），取第一个字符串值即该会话的真实目录"""
    return next((d.get("cwd") for d in objs if isinstance(d.get("cwd"), str)), "")


def _t_jsonl_dir(kind: str, projects_dir: Path, cwd: str, limit: int, t0: float, qoder: bool = False) -> Tuple[List[dict], str]:
    """claude / qoder 共用：~/.<kind>/projects/<cwd 脱敏名>/<sid>.jsonl，**跳目录**按 mtime 取最近。"""
    if not projects_dir.is_dir():
        return [], f"{kind} 无会话仓库"
    files = sorted(projects_dir.glob("*/*.jsonl"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return [], f"{kind} 暂无可续会话（只有目录骨架）"
    items: List[dict] = []
    for p in files:
        if time.time() - t0 > HARD_BUDGET_S:
            return items, "扫描超时，仅显示已读到的条目"
        objs = _head_lines(p)
        title = ""
        if qoder:                       # qoder 实测有 last-prompt 行（取的就是用户自己那句）
            title = next((d.get("lastPrompt", "") for d in objs if d.get("type") == "last-prompt"), "")
        title = title or _first_user_text(objs)
        items.append({"agent": kind, "id": p.stem, "title": mask_title(title) or "未命名会话",
                      "ts": int(p.stat().st_mtime), "msgs": None, "cwd": _cwd_of_records(objs) or cwd})
        if len(items) >= limit:
            break
    return items, ""


#: codebuddy（CodeBuddy Code，WorkBuddy 包内捆绑 CLI）的会话仓库。
#: 布局实测 2026-10-08（73 个 jsonl / 5 个桶）：``~/.codebuddy/projects/<compressPath(cwd)>/<sid>.jsonl``
#: ＋ 同名目录（放 tool-results / subagents / file-history，**不是**会话本体）。
#: 桶名算法 = ``canonicalizeStorePath``(realpathSync，失败原样返回) 后
#: ``compressPath``（`/ \ :`→`-`、去首尾 `-`、合并连续 `-`）—— 与 claude/qoder 的
#: 「slug」不是一回事，**不可套用**。
CODEBUDDY_PROJECTS = HOME / ".codebuddy" / "projects"


def _compress_path(p: str) -> str:
    """codebuddy 的桶名算法（PathUtils.compressPath 的 Python 等价，规则逐条对齐 dist 实测源码）。"""
    s = re.sub(r"[/\\:]", "-", p or "")
    return re.sub(r"-+", "-", s.strip("-"))


def _cb_cli() -> str:
    """codebuddy CLI 绝对路径。profiles 是终端入口的同一真相源（profiles.py:33 CODEBUDDY_CLI），
       两处各写一份字面量就会在换包路径时静默走偏（同 codex 09-30 半边修复的教训）。
       本模块有两种引用形态（`import sessions_store`：src 在 sys.path 上，见 term.py；
       `from src import sessions_store`：见 tests），两条 import 路径都得试。"""
    for mod in ("profiles", "src.profiles"):
        try:
            m = importlib.import_module(mod)
        except ImportError:
            continue
        return m.CODEBUDDY_CLI
    raise RuntimeError("codebuddy CLI 路径取不到：profiles 模块两种形态都 import 失败")


def _cb_bucket_of(cwd: str) -> str:
    """一条 codebuddy 会话所在桶名 = compressPath(realpath(cwd))；realpath 失败则按原串算
       （对齐 dist 的 canonicalizeStorePath：realpathSync 抛错就返回入参本身）。"""
    if not cwd:
        return ""
    try:
        rp = str(Path(cwd).resolve())
    except OSError:
        rp = cwd
    return _compress_path(rp)


def _cb_effective_bucket(rec_cwd: str, fallback: str) -> str:
    """续聊时**真正会生效**的工作目录 = `session_cwd()` 的口径：记录目录在盘上就用它，
       否则退回画像目录。桶名必须按这个算，不能按记录目录算 —— 记录目录已删的那一半
       （本机 73 条里worktree 桶那类）正是这样被挡掉的。"""
    return _cb_bucket_of(rec_cwd if (rec_cwd and Path(rec_cwd).is_dir()) else fallback)


def _codebuddy_first_user(objs: List[dict]) -> str:
    """codebuddy 版「首句用户提问」——**不能复用** `_first_user_text()`：
       实测行形状是 ``{"type":"message","role":"user","content":[{"type":"input_text","text":…}]}``
       （content 里是 `input_text`/`output_text`，不是 claude 的 `text`），
       而 `_first_user_text` 的通用分支要求 `type=='user'` ⇒ 恒返回空。
       参照 `_cursor_first_user` 单设一个 role 版；`<user_query>` 拆标签与注入前缀过滤共用。
       本机实测 62 个非空会话出标题 60 条（另2 条全是注入块，见 `_t_codebuddy` 的说明）。"""
    for d in objs:
        if d.get("type") != "message" or d.get("role") != "user":
            continue
        if d.get("isSidechain") or d.get("isMeta"):
            continue
        s = _text_of(d.get("content"))
        if not s:
            continue
        m = _UQ.search(s)
        if m:
            got = m.group(1).strip()
            if got:
                return got
            continue
        if s.startswith(_INJECT_PREFIX) or s.startswith("<"):
            continue
        return s
    return ""


def _t_codebuddy(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """codebuddy 历史会话：``~/.codebuddy/projects/<桶>/<sid>.jsonl``，**跳目录**（用户 09-22 裁定）。

       与 claude/qoder 同为「jsonl 一文件一会话」，但三处形状完全不同，逐条实测钉死：
       ① **行判据**：``type=='message' and role=='user'``（不是 claude 的 ``type=='user'``）。
       ② **桶名**：compressPath(realpath(cwd))，不是 slug ⇒ `_store_path` 只能 glob 定位。
       ③ **零字节文件要挡掉**（本机 11/73）：codebuddy 建会话时先 writeFile("") 占位，
          没发言就退出的会话留下空文件；resume 它会得到一条**空会话**（deserialize 返回
          history=[]），与 opencode 的 `exists(message)`、cursor 的 `hasConversation` 同口径。

       **可续判据必须自己算**：dist 里 `findExistingSession()` 调 `sessionManager.get(id)`
       **不传第二参数** ⇒ `getPreferredProjectDir(undefined)` 直接落空 ⇒ 只剩
       `getSessionFilePath()` = **当前进程 cwd 那个桶**。也就是说 codebuddy 的 resume 是
       「按起 pty 的工作目录定位会话」的，跟 cursor 同构（判例见 `_t_cursor` 的注释）。
       所以这里只列「桶名 == compressPath(该条**将会生效**的工作目录)」的条目
       （cursor 用 md5(cwd) 做同一件事）：记录目录已删的条目（`session_cwd()` 会退回画像目录）
       桶名必然对不上 ⇒ 点了也 resume 不到 ⇒ 列得出来就必须点得动（v0.13.62 教训）。
       """
    if not CODEBUDDY_PROJECTS.is_dir():
        return [], "codebuddy 无会话仓库"
    files = sorted(CODEBUDDY_PROJECTS.glob("*/*.jsonl"),
                   key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return [], "codebuddy 暂无可续会话（只有目录骨架）"
    items: List[dict] = []
    seen: set = set()          # 同一个 sid 会在多个桶各存一份（实测 7a3124d8：主仓 + worktree 桶）
    for p in files:
        if time.time() - t0 > HARD_BUDGET_S:
            return items, "扫描超时，仅显示已读到的条目"
        if p.stat().st_size == 0:                       # 空占位会话，续了也是空会话
            continue
        bucket = p.parent.name
        objs = _head_lines(p)
        rec_cwd = _cwd_of_records(objs)
        if bucket != _cb_effective_bucket(rec_cwd, cwd):
            continue                                    # 生效目录对不上 ⇒ 点了也 resume 不到
        sid = p.stem
        if sid in seen:                                 # 同 id 多桶：留 mtime 最新的那份（列表已倒序）
            continue
        seen.add(sid)
        items.append({"agent": "codebuddy", "id": sid,
                      "title": mask_title(_codebuddy_first_user(objs)) or "未命名会话",
                      "ts": int(p.stat().st_mtime), "msgs": None,
                      "cwd": rec_cwd or cwd})
        if len(items) >= limit:
            break
    return items, ("" if items else "codebuddy 暂无可续会话")


def _t_claude(cwd: str, limit: int, t0: float):
    return _t_jsonl_dir("claude", HOME / ".claude" / "projects", cwd, limit, t0)


def _t_qoder(cwd: str, limit: int, t0: float):
    return _t_jsonl_dir("qoder", HOME / ".qoder" / "projects", cwd, limit, t0, qoder=True)


def _t_jcode(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """仓库以 ~/.jcode/sessions/*.json 为准（sqlite 的 recent_sessions 会被 prune，只作辅助）。
       .bak 文件名以 .json.bak 结尾，glob('session_*.json') 天然不会命中。
       跳目录口径（用户 09-22 裁定）：不按 working_dir 过滤，取全局 mtime 最近 limit 条。"""
    root = HOME / ".jcode" / "sessions"
    if not root.is_dir():
        return [], "jcode 无会话仓库"
    files = sorted(root.glob("session_*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return [], "jcode 无会话仓库"
    items: List[dict] = []
    # 教训保留（v0.13.1）：此仓库是**平铺混所有 cwd** 的目录，绝不可「先取最新 N 个再按 cwd 过滤」
    # ——实测那样会让技术文档目录下 89 条只剩 1 条可见（被当日 10 条探针占满窗口）。
    for p in files:
        if time.time() - t0 > HARD_BUDGET_S:
            return items, "扫描超时，仅显示已读到的条目"
        try:
            d = _load(p)
        except Exception:  # noqa: BLE001
            continue
        msgs = d.get("messages") or []
        title = _first_user_text(msgs, jcode=True) or (d.get("title") or "").strip()   # D2：问题原文优先
        items.append({"agent": "jcode", "id": d.get("id") or p.stem, "title": mask_title(title) or "未命名会话",
                      "ts": _iso(d.get("last_active_at") or d.get("updated_at")),
                      "msgs": len(msgs) or None, "cwd": d.get("working_dir") or cwd})
        if len(items) >= limit:
            break
    return items, ""


#: DSH（DeepSeek Harness）会话仓库（2026-10-24 接入，判例 254）。
#: 真事件日志：`~/.dsh/sessions/<压缩cwd>/session-<uuid>/session.v4.jsonl.zstd`（zstd，需解压）。
#: 列表读它的**投影缓存** `~/.dsh/storages/session_projcache/sessions/session-<uuid>.json`：
#:   `{record:{identity:{createdAt,cwd}, rows:{title:{val}, titleInput:{val:{first:{text}}}}}}`
#: ⇒ 毫秒级、不用解压；续聊时 TUI 自己按 id 找回事件日志。
#: ⚠️ resume 的 id 是**裸 uuid**：TUI 退出提示实测为 `dsh --profile tui --resume <uuid>`
#: （不带 `session-` 前缀）；仓库文件名/目录名才带前缀。
DSH_SESSIONS = HOME / ".dsh" / "storages" / "session_projcache" / "sessions"


def _dsh_title_and_cwd(p: Path) -> Tuple[str, str]:
    """从一条投影缓存里取 (标题, cwd)。标题优先 rows.title.val，
       没有就退 titleInput.val.first.text（首条用户输入）——两者都没有即空会话。"""
    rec = (_load(p) or {}).get("record") or {}
    ident = rec.get("identity") or {}
    rows = rec.get("rows") or {}
    title = str((rows.get("title") or {}).get("val") or "").strip()
    if not title:
        first = ((rows.get("titleInput") or {}).get("val") or {}).get("first") or {}
        title = str(first.get("text") or "").strip()
    return title, str(ident.get("cwd") or "").strip()


def _t_dsh(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """DSH 历史：全局平铺（每条自带 cwd，跳目录口径与 jcode 同），按缓存 mtime 倒序。
       **只起界面、没提问的空会话不占历史位**（实测 TUI 启动即建 3 事件会话）。"""
    root = DSH_SESSIONS
    if not root.is_dir():
        return [], "dsh 无会话仓库（~/.dsh/storages/session_projcache）"
    files = sorted(root.glob("session-*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
    if not files:
        return [], "dsh 暂无可续会话"
    items: List[dict] = []
    for p in files:
        if time.time() - t0 > HARD_BUDGET_S:
            return items, "扫描超时，仅显示已读到的条目"
        sid = p.stem[len("session-"):]
        if not UUID_RE.match(sid):
            continue
        try:
            title, s_cwd = _dsh_title_and_cwd(p)
        except Exception:  # noqa: BLE001
            continue
        if not title:
            continue
        items.append({"agent": "dsh", "id": sid, "title": mask_title(title) or "未命名会话",
                      "ts": int(p.stat().st_mtime), "msgs": None, "cwd": s_cwd or cwd})
        if len(items) >= limit:
            break
    return items, ""


def _ro(db: Path) -> sqlite3.Connection:
    c = sqlite3.connect(f"file:{db}?mode=ro", uri=True)
    c.row_factory = sqlite3.Row
    return c


def _t_hermes(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """权威仓库是 state.db（~/.hermes/sessions/ 已于 08-05 停写）。152/209 条 cwd 为 NULL ⇒
       不按 cwd 过滤、按 source='cli' 全量（D7），并在 note 里写明口径。"""
    db = HOME / ".hermes" / "state.db"
    if not db.exists():
        return [], "hermes 无 state.db"
    sql = ("select s.id as id, s.title as title, s.last_activity_at as ts, s.cwd as scwd, "
           "(select m.content from messages m where m.session_id=s.id and m.role='user' and m.active=1 "
           " order by m.id limit 1) as first_u "
           "from sessions s where s.source='cli' order by s.last_activity_at desc limit ?")
    try:
        with _ro(db) as c:
            rows = c.execute(sql, (limit,)).fetchall()
    except Exception as e:  # noqa: BLE001
        return [], f"hermes 读取失败：{type(e).__name__}"
    items = [{"agent": "hermes", "id": r["id"],
              "title": mask_title(r["first_u"] or r["title"] or "") or "未命名会话",   # D2：问题原文优先，LLM 标题当兼底
              "ts": int(r["ts"] or 0), "msgs": None, "cwd": r["scwd"] or ""} for r in rows]
    return items, "口径：hermes 按 source=cli 全量跳目录（历史多数条目未记 cwd）"


#: v0.13.61：codex threads.source 的**排除**集合（不是白名单）。
#: 起因（09-30 实测）：老代码写死 `source='cli'`，而 09-29 起用户实际在 **vscode/IDE 扩展**
#: 里开的会话 source 记的是 `vscode` ⇒ 最新会话被整体过滤掉，侧栏 agent 名下的历史停在 09-28。
#: 白名单会随 codex 每次新增入口（cli/vscode/…）再次静默漏 ⇒ 改成「只排噪音」的排除集：
#:   - `exec`：**探针**。本仓多处用 `codex exec` 做版本/能力探测，它不是人开的会话（实测 74 条，
#:     rollout 恒为 45KB 量级的固定探针，first_user_message 是探测指令）。
#:   - `{"subagent":{...}}`：子代理线程（JSON 串），无独立用户语境，续聊无意义。
#: 真实用户会话（cli / vscode / 未来任何新入口）一律收进来。
CODEX_NOISE_SOURCE = ("exec",)
CODEX_SUBAGENT_PREFIX = '{"subagent":'


def _codex_real_user_sql(where: str) -> Tuple[str, tuple]:
    """codex「真实用户会话」的**唯一** SQL 判据 —— 列表与续聊校验共用。

    09-30 事故的教训：v0.13.61 把列表侧换成排除集，校验侧却还写死 `source='cli'`，
    两处各写一份字面量就必然再次走偏（列表放行 ⇒ 点不动 ⇒ 404）。
    ⇒ 这里返回 `(where 片段, 绑定参数)`，调用方只需把自己的 id 条件 AND 进来。
    噪音判据仍只能落在 threads.source：exec 探针的标题**就是真实用户提问**。"""
    frags, params = [], []
    for n in CODEX_NOISE_SOURCE:
        frags.append("source not like ?")
        params.append(n + "%")
    frags.append("source not like ?")
    params.append(CODEX_SUBAGENT_PREFIX + "%")
    return (" and " + " and ".join(frags) if frags else ""), tuple(params)


def _t_codex(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """列**真实用户会话**（排除 exec 探针与 subagent 子线程），**跳目录**（用户 09-22 裁定）。
       实测 updated_at/created_at 为 epoch 秒；
       `has_user_event` 实测在真会话上也恒为 0 ⇒ 不可当过滤条件。
       limit 要放宽再查：排除是在 SQL 里做的，命中数可能少于 limit，故多取一些再截断。"""
    db = HOME / ".codex" / "state_5.sqlite"
    if not db.exists():
        return [], "codex 无 state_5.sqlite"
    real, real_params = _codex_real_user_sql("")      # 谓词参数在前、limit 在后，顺序即 ? 的顺序
    try:
        with _ro(db) as c:
            rows = c.execute(
                "select id, title, cwd, updated_at, source from threads "
                "where archived=0" + real +
                " order by updated_at desc limit ?",
                real_params + (max(limit * 3, limit),)).fetchall()
    except Exception as e:  # noqa: BLE001
        return [], f"codex 读取失败：{type(e).__name__}"
    items = [{"agent": "codex", "id": r["id"], "title": mask_title(r["title"] or "") or "未命名会话",
              "ts": int(r["updated_at"] or 0), "msgs": None, "cwd": r["cwd"] or ""}
             for r in rows][:limit]
    return items, ("" if items else "codex 无交互式历史（exec 探针与子代理线程不计）")


def _t_opencode(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """opencode 1.x 的仓库是 SQLite（实测 1.18.32：~/.local/share/opencode/opencode.db）。
       排除子代理子会话（parent_id 非空）与归档（time_archived 非空）；
       exists(message) 挡掉零消息空会话——它们的标题是 "New session - <ISO>" 占位，无续聊价值。
       跳目录（09-22 裁定同样适用：条目可能来自任何工程，起 pty 用 session_cwd）。time_* 为毫秒。"""
    if not OPENCODE_DB.exists():
        return [], "opencode 无 opencode.db"
    sql = ("select s.id as id, s.title as title, s.directory as dir, s.time_updated as ts, "
           "(select json_extract(p.data,'$.text') from part p join message m on p.message_id = m.id "
           "  where m.session_id = s.id and json_extract(m.data,'$.role') = 'user' "
           "  and json_extract(p.data,'$.type') = 'text' "
           "  and length(json_extract(p.data,'$.text')) > 0 "
           "  order by p.time_created limit 1) as first_u "
           "from session s where (s.parent_id is null or s.parent_id='') "
           "and s.time_archived is null "
           "and exists (select 1 from message m where m.session_id = s.id) "
           "order by s.time_updated desc limit ?")
    try:
        with _ro(OPENCODE_DB) as c:
            rows = c.execute(sql, (limit,)).fetchall()
    except Exception as e:  # noqa: BLE001
        return [], f"opencode 读取失败：{type(e).__name__}"
    items = [{"agent": "opencode", "id": r["id"],
              "title": mask_title((r["title"] or "") if not (r["title"] or "").startswith("New session - ")
                                  else (r["first_u"] or r["title"] or "")) or "未命名会话",
              "ts": int(r["ts"] or 0) // 1000, "msgs": None, "cwd": r["dir"] or ""}
             for r in rows][:limit]
    return items, ("" if items else "opencode 无可续聊历史")


# ── cursor 专用助手 ─────────────────────────────────────────────────────────
def _cursor_first_user(p: Path) -> str:
    """从转录 jsonl 取首句用户提问。

       ⚠️ 形状与各家都不同，所以**不能复用** `_first_user_text()`（它要求 `type=='user'`）：
       实测 cursor 的行是 `{"role":"user","message":{"content":[{"type":"text","text":…}]}}`
       —— **role 键**、且正文在 message.content 里。`<user_query>` 拆标签照样复用 `_UQ`。
       头部只读 HEAD_CHARS、errors=ignore：与其它适配器同口径（只读、不写不删）。"""
    for d in _head_lines(p, 400):
        if d.get("role") != "user":
            continue
        s = _text_of((d.get("message") or {}).get("content"))
        if not s:
            continue
        m = _UQ.search(s)
        if m:
            got = m.group(1).strip()
            if got:
                return got
            continue
        if s.startswith(_INJECT_PREFIX) or s.startswith("<"):
            continue
        return s
    return ""


def _cursor_title(sid: str, meta: dict, transcripts: Optional[Dict[str, Path]] = None) -> str:
    """cursor 的标题 = **用户问题原文优先**，`meta.title` 只当兜底（D2，同 hermes/codex/opencode）。

    ⚠️ 2026-10-08 实测根因：本机 99 条 meta 里 5 条有 `title`，**无一例外全是 Cursor
    服务端生成的英文**（`Cursor Startup Garbled Characters` / `Top Image Bar Sticky
    Issue` / `Initial Greeting` …），而同一批会话的用户原话是中文
    （`cursor启动会带入乱码字符` / `美容仪器项目产品详情页 顶部图片栏没有固定悬浮`）。
    旧代码 title 优先 ⇒ 侧栏历史里凡是服务端生成了标题的那几条**整排显示英文**。
    `meta.title` 来自服务端会话列表、离线读不到也不受我们控制，**不能当主口径**。

    列表侧（`_t_cursor`）与续聊侧（`_title_of_session`）**必须共用本函数**：
    这两处各写一份字面量必然漂移，是本仓「列得出必须点得动」教训的同构版
    （见 `_codex_real_user_sql` 注释：09-30 就是两处口径不一致走的弯路）。

    `transcripts` 由列表侧一次性建好传进来（全局扫一遍比逐条 glob 便宜）；
    单查时留空走按 sid 通配。"""
    f = transcripts.get(sid) if transcripts is not None else \
        next(iter(CURSOR_PROJECTS.glob(f"*/agent-transcripts/{sid}/{sid}.jsonl")), None)
    if f:
        t = _cursor_first_user(f)
        if t:
            return t
    return (meta.get("title") or "").strip()


def _cursor_meta(sid: str) -> Optional[dict]:
    """按会话 id 定位它的 meta.json（跳目录口径下 id 全局唯一，实测各桶不撞名）。"""
    if not UUID_RE.match(sid or ""):
        return None
    hit = next(iter(CURSOR_CHATS.glob(f"*/{sid}/meta.json")), None)
    if not hit:
        return None
    try:
        return _load(hit)
    except Exception:  # noqa: BLE001
        return None


def _cursor_resumable(meta_path: Path, meta: dict, fallback_cwd: str) -> bool:
    """这一条**真续得上吗** —— 判据取 cursor 自己的解析规则，不是「目录还在不在」。

    ⚠️ 实测根因（2026-10-07）：`cursor-agent --resume <chatId>` 是**按当前工作目录定位会话**的
    —— 它算 `md5(进程 cwd)` 当桶名，再去 `<chats>/<md5(cwd)>/<chatId>/` 找那条。而我们起 pty
    用的是 `session_cwd()`：记录的 cwd 已不存在就退回画像目录。两者一旦不等，cursor 就找不到
    该会话、**静默开一条新的**——实测从 `/fs/1000/ftp/技术文档` 续一条 cwd 已删的
    `/tmp/cursorfwd` 会话，屏上**没有** `Loading conversation`；续同一工作区的会话就有。
    对用户的表现是「点了历史条目却落进别的/空白会话」，**比"没有历史下拉"更糟**
    （v0.13.62 那条教训的同构：列得出来就必须点得动，半边修复最害人）。
    所以桶名对不上的一律不列。

    桶名 = `md5(原样 cwd)`，与 `projects/` 下的 slug 不是一回事（见上方常量注释）。
    反过来的好处：cwd 字段缺失但桶名恰好等于画像目录的会话**照常可续**（本机实测有 1 条），
    比「目录存在性」判据更准。"""
    bucket = meta_path.parent.parent.name
    cwd = (meta.get("cwd") or "").strip()
    resolved = cwd if (cwd and Path(cwd).is_dir()) else fallback_cwd
    return hashlib.md5(resolved.encode()).hexdigest() == bucket


def _t_cursor(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """cursor 的历史会话分散在两处，**以 chats/meta.json 为主表、转录只供标题**：

       ① `~/.cursor/chats/<md5(cwd)>/<agentId>/meta.json`
          有 cwd、有 updatedAtMs（**毫秒**）、有 hasConversation；**没有正文**。
          title 字段只有极少数条目有，且实测（2026-10-08，本机 5/99）**全是服务端生成的英文**
          ⇒ 不能当主口径，标题一律走 `_cursor_title()`（问题原文优先、它兜底）。
       ② `~/.cursor/projects/<slug>/agent-transcripts/<agentId>/<agentId>.jsonl`
          第一行就是用户原话，包在 `<user_query>` 里。

       实测（2026-10-07，本机 57/61）：②的目录集合与「①中 hasConversation=true」**完全
       同一集合**（57 vs 57，无孤儿）⇒ `hasConversation` 足以当「可续聊」判据，
       不需要额外的 proc 探测。（`meta.json` 里的 `cwd` 恰好能补 `agent-transcripts`
       只有 slug 没有真路径的那一半，两个仓库是互补的，故必须同时用。）
       `hasConversation=false` 的是无正文的空会话（Cursor 自己的选择器里显示 "New Agent"），
       没有续聊价值，与 opencode 用 `exists(message)` 挡空会话同口径。
       **还要过 `_cursor_resumable()`**：cursor 的 --resume 是按 md5(当前 cwd) 定位会话的，
       记录目录已删（本机 4 条）或与画像目录不同桶的条目点了也续不上（会静默开新会话）
       ⇒ 列得出来就必须点得动（v0.13.62 codex 那条教训），不满足的一律不列。

       跳目录（09-22 裁定）：不按 cwd 过滤，按时间倒序取 limit 条，cwd 逐条自带。"""
    if not CURSOR_CHATS.is_dir():
        return [], "cursor 无会话仓库"
    metas: List[Tuple[str, dict]] = []
    for p in CURSOR_CHATS.glob("*/*/meta.json"):
        if time.time() - t0 > HARD_BUDGET_S:
            break
        try:
            m = _load(p)
        except Exception:  # noqa: BLE001
            continue
        if not m.get("hasConversation"):     # 空会话：无正文，续不了
            continue
        if not _cursor_resumable(p, m, cwd):  # 桶名对不上 ⇒ 点了也是开新会话
            continue
        metas.append((p.parent.name, m))
    if not metas:
        return [], "cursor 暂无可续聊历史"
    # 转录表一次建好（跳目录口径下条目可能来自任何工程，不能按 cwd 的 slug 收窄，
    # 收窄会漏掉别的工程的历史）。全局扫一遍比逐条 glob 便宜。
    transcripts: Dict[str, Path] = {}
    for f in CURSOR_PROJECTS.glob("*/agent-transcripts/*/*.jsonl"):
        transcripts[f.parent.name] = f
    items: List[dict] = []
    for sid, m in metas:
        if time.time() - t0 > HARD_BUDGET_S:
            return items, "扫描超时，仅显示已读到的条目"
        t = _cursor_title(sid, m, transcripts)
        items.append({"agent": "cursor", "id": sid,
                      "title": mask_title(t) or "未命名会话",
                      "ts": int(m.get("updatedAtMs") or 0) // 1000,   # 实测毫秒（同 opencode）
                      "msgs": None, "cwd": m.get("cwd") or ""})
    items.sort(key=lambda x: x["ts"], reverse=True)
    return items[:limit], ""
# ── omp（oh-my-pi）专用助手 ────────────────────────────────────────────────────
#: omp 会话仓库：``~/.omp/agent/sessions/<compressed-cwd>/<iso-timestamp>_<uuid>.jsonl``
#: 文件名格式：`2026-10-26T10-04-24-907Z_01a177ab-1bcb-7000-b474-2b93755c5670.jsonl`
#: UUID 是文件名中最后一个 `_` 之后的部分（不含 .jsonl 后缀）。
#: 标题在第一行的 {"type":"title","title":"..."} 字段；cwd 在 {"type":"session",...} 行。
#: resume 命令：`omp --resume <uuid>`（官方支持 UUID 前缀匹配）。
OMP_SESSIONS = HOME / ".omp" / "agent" / "sessions"
OMP_FILENAME_UUID_RE = re.compile(
    r"[_-]([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})\.jsonl\Z", re.I)


def _omp_uuid_from_filename(name: str) -> Optional[str]:
    """从 omp 文件名提取 UUID。失败返回 None。"""
    m = OMP_FILENAME_UUID_RE.search(name)
    return m.group(1) if m else None


def _t_omp(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """omp 历史：``~/.omp/agent/sessions/<桶>/<时间戳>_<uuid>.jsonl``，**跳目录**按 mtime 倒序。"""
    if not OMP_SESSIONS.is_dir():
        return [], "omp 无会话目录"
    items: List[dict] = []
    for p in sorted(OMP_SESSIONS.rglob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        sid = _omp_uuid_from_filename(p.name)
        if not sid or not UUID_RE.match(sid):
            continue
        objs = _head_lines(p, 400)
        title = ""
        for d in objs:
            if d.get("type") == "title":
                title = d.get("title") or ""
                break
        ts = 0
        for d in objs:
            if d.get("type") == "session":
                ts = _iso(d.get("timestamp"))
                break
        items.append({"agent": "omp", "id": sid,
                      "title": mask_title(title) or "未命名会话",
                      "ts": ts, "msgs": None, "cwd": cwd})
        if len(items) >= limit + 10:
            break
    items = items[:limit]
    return items, ("" if items else "omp 暂无可续会话")


# ── pi（earendil-works/pi-coding-agent）专用助手 ────────────────────────────────
#: pi 会话仓库：``~/.pi/agent/sessions/<compressed-cwd>/<iso-timestamp>_<uuid>.jsonl``
#: 与 omp 同构：文件名格式 `<timestamp>_<uuid>.jsonl`，UUID 在文件名末尾。
#: resume 命令：`pi --resume <uuid>`（官方支持 UUID 前缀匹配）。
PI_SESSIONS = HOME / ".pi" / "agent" / "sessions"
PI_FILENAME_UUID_RE = OMP_FILENAME_UUID_RE  # 同构格式，复用正则


def _pi_first_user(objs: List[dict]) -> str:
    """从 pi jsonl 取首句用户提问。

    ⚠️ pi 形状与 `_first_user_text` 不兼容：消息是
    `{"type":"message","message":{"role":"user","content":[...]}}`
    ——`type` 恒为 `message`，真正判据在 `message.role`，正文在
    `message.content`。所以不能复用 `_first_user_text`（它查顶层 `type=='user'`）。
    复用 `_cursor_first_user` 的解析逻辑（role 在 message 里、content 是数组）。"""
    for d in objs:
        if d.get("type") != "message":
            continue
        msg = d.get("message") or {}
        if msg.get("role") != "user":
            continue
        s = _text_of(msg.get("content"))
        if not s:
            continue
        m = _UQ.search(s)
        if m:
            got = m.group(1).strip()
            if got:
                return got
            continue
        if s.startswith(_INJECT_PREFIX) or s.startswith("<"):
            continue
        return s
    return ""


def _t_pi(cwd: str, limit: int, t0: float) -> Tuple[List[dict], str]:
    """pi 历史：``~/.pi/agent/sessions/<桶>/<时间戳>_<uuid>.jsonl``，**跳目录**按 mtime 倒序。"""
    if not PI_SESSIONS.is_dir():
        return [], "pi 无会话目录"
    items: List[dict] = []
    for p in sorted(PI_SESSIONS.rglob("*.jsonl"), key=lambda x: x.stat().st_mtime, reverse=True):
        sid = _omp_uuid_from_filename(p.name)  # 同构格式，复用 omp 提取函数
        if not sid or not UUID_RE.match(sid):
            continue
        objs = _head_lines(p, 400)
        title = ""
        for d in objs:
            if d.get("type") == "title":
                title = d.get("title") or ""
                break
        if not title:
            title = _pi_first_user(objs)
        ts = 0
        for d in objs:
            if d.get("type") == "session":
                ts = _iso(d.get("timestamp"))
                break
        items.append({"agent": "pi", "id": sid,
                      "title": mask_title(title) or "未命名会话",
                      "ts": ts, "msgs": None, "cwd": cwd})
        if len(items) >= limit + 10:
            break
    items = items[:limit]
    return items, ("" if items else "pi 暂无可续会话")


SESSION_STORES: Dict[str, dict] = {
    "grok":     {"kind": "grok_dir",       "id_re": UUID_RE,      "resume": ["grok", "--resume", "{id}"],             "fn": _t_grok},
    "claude":   {"kind": "claude_dir",     "id_re": UUID_RE,      "resume": ["claude", "--resume", "{id}"],           "fn": _t_claude},
    "qoder":    {"kind": "qoder_dir",      "id_re": UUID_RE,      "resume": ["qodercli", "-w", "{cwd}", "-r", "{id}"], "fn": _t_qoder},
    "jcode":    {"kind": "jcode_json",     "id_re": JCODE_RE,     "resume": ["jcode", "--resume", "{id}"],            "fn": _t_jcode},
    "hermes":   {"kind": "hermes_sqlite",  "id_re": HERMES_RE,    "resume": ["hermes", "--resume", "{id}"],           "fn": _t_hermes},
    # 同上的理由：续聊进来的 codex TUI 一样会进备用屏、一样滚不动（实测与依据见
    # profiles.py 的 codex 条目）。--no-alt-screen 是顶层选项（`codex [OPTIONS] <COMMAND>`），
    # 放子命令之前；实测 `codex --no-alt-screen resume <id>` 与放在后面都被接受。
    # v0.13.83：前端已在解析层吞掉 ?1049h（termAltScreenBlock），备用屏不再靠这个 flag 收口；
    # 保留它是因为它让 codex 的输出落进主屏 scrollback（官方 flag 原文），也少一层无谓的吞。
    "codex":    {"kind": "codex_sqlite",   "id_re": UUID_RE,      "resume": ["codex", "--no-alt-screen", "resume", "{id}"], "fn": _t_codex},
    "opencode": {"kind": "opencode_sqlite", "id_re": OPENCODE_RE, "resume": ["opencode", "--session", "{id}"],        "fn": _t_opencode},
    # cursor：`--resume [chatId]` 由实装入口 ~/.local/bin/cursor-agent 转发到它的 local
    # runtime（官方 `cursor-agent --help` 原文：`--resume [chatId]  Select a session to resume`）。
    # 用实装名 cursor-agent 而非官网名 cursor —— 后者 which 落空（与 profiles.CLI_ALIASES 同款理由）。
    "cursor":   {"kind": "cursor_json",    "id_re": UUID_RE, "resume": ["cursor-agent", "--resume", "{id}"],     "fn": _t_cursor},
    # codebuddy：**必须写绝对路径**（同profiles.py 的 CODEBUDDY_CLI 理由）——该 CLI 只在
    # WorkBuddy 包内、不在 PATH，写 "codebuddy" 会被 term.py 的 which() 判定失败、会话拉不起来。
    # 这里 import profiles 是安全的：profiles 不 import本模块（反向 term.py 才 import 两者）。
    "codebuddy": {"kind": "codebuddy_json", "id_re": UUID_RE, "resume": [_cb_cli(), "--resume", "{id}"], "fn": _t_codebuddy},
    # dsh（DeepSeek Harness，2026-10-24）：`dsh` 在 ~/.local/bin，profiles.which() 的兜底目录
    # 已覆盖，不必写绝对路径；但**必须带 `--profile tui`**——裸 dsh 会变成 `dsh web`（判例 252 §四.2）。
    # `--resume` 接裸 uuid（TUI 自己的退出提示就是这个形状）。
    "dsh":      {"kind": "dsh_json",       "id_re": UUID_RE,      "resume": ["dsh", "--profile", "tui", "--resume", "{id}"], "fn": _t_dsh},
    # omp（oh-my-pi）：~/.omp/agent/sessions/<桶>/<时间戳>_<uuid>.jsonl，UUID 在文件名末尾。
    # resume 接裸 uuid（`omp --resume <uuid>`），与 dsh 同形。
    "omp":      {"kind": "omp_jsonl",    "id_re": UUID_RE,      "resume": ["/home/gztxt/.local/bin/omp", "--resume", "{id}"], "fn": _t_omp},
    # pi（@earendil-works/pi-coding-agent）：~/.pi/agent/sessions/<桶>/<时间戳>_<uuid>.jsonl
    # resume 接裸 uuid（`pi --resume <uuid>`），与 omp 同形。
    "pi":       {"kind": "pi_jsonl",     "id_re": UUID_RE,      "resume": ["pi", "--resume", "{id}"],      "fn": _t_pi},
}

_CACHE: Dict[tuple, Tuple[float, dict]] = {}


def supports(agent_id: str) -> bool:
    return agent_id in SESSION_STORES


def list_history(agent_id: str, cwd: str, limit: int = 3) -> dict:
    st = SESSION_STORES.get(agent_id)
    if not st:
        return {"items": [], "cwd": cwd, "note": f"{agent_id} 无历史会话仓库"}
    limit = max(1, min(int(limit or 3), 20))
    key = (agent_id, cwd, limit)
    hit = _CACHE.get(key)
    if hit and time.time() - hit[0] < CACHE_TTL_S:
        return hit[1]
    try:
        items, note = st["fn"](cwd, limit, time.time())
    except Exception as e:  # noqa: BLE001
        items, note = [], f"历史读取失败：{type(e).__name__}"
    items.sort(key=lambda x: x.get("ts") or 0, reverse=True)
    out = {"items": items[:limit], "cwd": cwd, "note": note}
    _CACHE[key] = (time.time(), out)
    return out


def known_ids(agent_id: str, cwd: str) -> set:
    """只用于「当前可展示清单」的集合（受 list_history 的 limit≤20 上限制）。
       ⚠ 不要拿它做续聊存在性校验：第 21 条以后的老会话会被误判 404，走 _exists_on_disk()。"""
    return {i["id"] for i in list_history(agent_id, cwd, 20)["items"]}


def _sql_one(db: Path, sql: str, params: tuple):
    try:
        with _ro(db) as c:
            return c.execute(sql, params).fetchone()
    except Exception:  # noqa: BLE001
        return None


def _store_path(agent_id: str, sid: str):
    """按仓库结构定位「那一条」落在哪个文件（跳目录口径下 id 仍是全局唯一：实测各桶不撞名）"""
    if agent_id == "grok":
        return next(iter((HOME / ".grok" / "sessions").glob(f"*/{sid}/summary.json")), None)
    if agent_id in ("claude", "qoder"):
        base = HOME / (".claude" if agent_id == "claude" else ".qoder") / "projects"
        return next(iter(base.glob(f"*/{sid}.jsonl")), None)
    if agent_id == "jcode":
        f = HOME / ".jcode" / "sessions" / f"{sid}.json"
        return f if f.is_file() else None
    if agent_id == "cursor":
        if not UUID_RE.match(sid or ""):
            return None
        return next(iter(CURSOR_CHATS.glob(f"*/{sid}/meta.json")), None)
    if agent_id == "codebuddy":
        # ⚠️ 与 claude 同构但**不能**只按 cwd 定位：桶名是 compressPath(realpath(cwd))，
        # 跳目录口径下 sid 跨桶可能撞名（实测 7a3124d8 同时存在于主仓桶与 worktree 桶）。
        # 取 mtime 最新的那份，与 _t_codebuddy 列表口径一致 ⇒ 列出的那条就是取到的那条。
        hits = list(CODEBUDDY_PROJECTS.glob(f"*/{sid}.jsonl"))
        if not hits:
            return None
        return max(hits, key=lambda p: p.stat().st_mtime)
    if agent_id == "dsh":
        # 投影缓存文件名带 `session-` 前缀，但 id 对外是裸 uuid（resume 用）。
        f = DSH_SESSIONS / f"session-{sid}.json"
        return f if (f.is_file() and f.stat().st_size > 0) else None
    if agent_id == "omp":
        # omp：文件名是 <timestamp>_<uuid>.jsonl，UUID 在末尾。
        hits = list(OMP_SESSIONS.rglob(f"*_{sid}.jsonl"))
        if not hits:
            return None
        return max(hits, key=lambda p: p.stat().st_mtime)
    if agent_id == "pi":
        # pi：同 omp 同构格式，文件名是 <timestamp>_<uuid>.jsonl。
        hits = list(PI_SESSIONS.rglob(f"*_{sid}.jsonl"))
        if not hits:
            return None
        return max(hits, key=lambda p: p.stat().st_mtime)
    return None


def _exists_on_disk(agent_id: str, sid: str, cwd: str = "") -> bool:
    """续聊前的「实盘存在」硬校验——按仓库结构直接定位那一条，不看 mtime 排名。
       教训（v0.13.1）：早先用 known_ids() 校验，而它走 list_history(limit<=20)
       ⇒ 第 21 条以后的真会话会被误判「不在实盘清单」→ 404 续不了。"""
    if agent_id == "hermes":
        return bool(_sql_one(HOME / ".hermes" / "state.db",
                             "select 1 from sessions where id=? and source='cli'", (sid,)))
    if agent_id == "codex":
        # v0.13.62：与 _t_codex **同一套**排除集，不再写死 source='cli'。
        # 半边修复的代价（09-30 实测）：v0.13.61 只改了列表侧，IDE 扩展（source='vscode'）
        # 开的会话能列出来却点不动，POST /api/term/sessions 必 404。
        # 判据只能取自 threads.source：exec 探针的标题**就是真实用户提问**（见上）。
        real, real_params = _codex_real_user_sql("")
        return bool(_sql_one(HOME / ".codex" / "state_5.sqlite",
                             "select 1 from threads where id=? and archived=0" + real,
                             (sid,) + real_params))
    if agent_id == "opencode":
        return bool(_sql_one(OPENCODE_DB,
                             "select 1 from session where id=? and (parent_id is null or parent_id='')"
                             " and time_archived is null", (sid,)))
    if agent_id == "codebuddy":
        # 与 _t_codebuddy **同一套**判据（零字节空会话 + 生效目录的桶名对得上），
        # 否则就是 v0.13.62 的半边修复：列表里能看见、点了必404。
        f = _store_path("codebuddy", sid)
        if not f or f.stat().st_size == 0:
            return False
        return f.parent.name == _cb_effective_bucket(_cwd_of_records(_head_lines(f, 5)), cwd)
    if agent_id == "cursor":
        # 与 _t_cursor **同一套**判据：hasConversation（有正文）+ _cursor_resumable
        # （桶名与**将要生效**的 cwd 一致 ⇒ 点了真能续上那条）。
        # 半边修复的代价见上面 codex 那条（列表能列、点不动必 404）；cursor 这里更隐蔽
        # ——桶名不对时 cursor 不报错，而是**静默开一条新会话**，看着像"续聊失败但没提示"。
        # fallback 用 cwd 参数（term.py 传的是画像目录），与 session_cwd() 的口径一致。
        m = _cursor_meta(sid)
        if not (m and m.get("hasConversation")):
            return False
        hit = next(iter(CURSOR_CHATS.glob(f"*/{sid}/meta.json")), None)
        return bool(hit) and _cursor_resumable(hit, m, cwd)
    return _store_path(agent_id, sid) is not None


def _session_cwd_recorded(agent_id: str, sid: str) -> str:
    """会话记录里写的 cwd（**原样**，不判存在性）。取不到 / 解析失败一律回空串。"""
    c = ""
    try:
        f = _store_path(agent_id, sid)
        if agent_id == "grok" and f:
            info = _load(f).get("info") or {}
            c = info.get("cwd") or unquote(f.parent.parent.name)
        elif agent_id in ("claude", "qoder") and f:
            c = _cwd_of_records(_head_lines(f, 5))
        elif agent_id == "jcode" and f:
            c = _load(f).get("working_dir") or ""
        elif agent_id == "hermes":
            r = _sql_one(HOME / ".hermes" / "state.db", "select cwd from sessions where id=?", (sid,))
            c = (r["cwd"] or "") if r else ""
        elif agent_id == "codex":
            r = _sql_one(HOME / ".codex" / "state_5.sqlite", "select cwd from threads where id=?", (sid,))
            c = (r["cwd"] or "") if r else ""
        elif agent_id == "opencode":
            r = _sql_one(OPENCODE_DB, "select directory from session where id=?", (sid,))
            c = (r["directory"] or "") if r else ""
        elif agent_id == "codebuddy" and f:
            c = _cwd_of_records(_head_lines(f, 5))
        elif agent_id == "cursor":
            m = _cursor_meta(sid) or {}
            c = m.get("cwd") or ""      # 实测有 1 条 cwd 为空 ⇒ 走下面的 fallback
        elif agent_id == "dsh" and f:
            c = _dsh_title_and_cwd(f)[1]
    except Exception:  # noqa: BLE001
        c = ""
    return (c or "").strip()


def session_cwd_ex(agent_id: str, sid: str, fallback: str = "") -> Dict[str, str]:
    """P1-7：`session_cwd` 的**带来源**版本，返回 `{cwd, recorded, source}`。

    - `source='recorded'`：记录的目录在盘上且被采用；
    - `source='fallback'`：**记录过** cwd，但该目录已消失 ⇒ 退回了 fallback；
    - `source='none'`：记录里本就没有 cwd（合法，例如 cursor 有空 cwd 条目）⇒ 退回 fallback。

    为什么要「来源」：静默退回 fallback 会把**恢复失败伪装成恢复成功** ——
    用户在 fallback（画像）目录里拿到一条空的新会话，以为续上了那条历史
    （手册 B1：「不过就保持失败，不得把空/错目录当恢复成功」）。
    调用方据此决定「拒启」还是「显式告警」；**不得**用 `cwd != fallback` 反推缺口
    （那会把「记录目录恰好就是 fallback」误判成回退）。
    """
    rec = _session_cwd_recorded(agent_id, sid)
    if rec and Path(rec).is_dir():
        return {"cwd": rec, "recorded": rec, "source": "recorded"}
    return {"cwd": fallback, "recorded": rec, "source": "fallback" if rec else "none"}


def session_cwd(agent_id: str, sid: str, fallback: str = "") -> str:
    """会话自己的 cwd。跳目录之后必须按这一条来起 pty / 拼 qoder 的 -w，
       否则会「在技术文档目录里打开一条 agent-hub 的会话」。目录不存在则退回 fallback。

    ⚠ 本函数**保留静默回退**（兼容既有调用点）；需区分「目录消失」与「本就无记录」
    的调用方（续聊起点）请用 `session_cwd_ex()` 看 `source`。"""
    return session_cwd_ex(agent_id, sid, fallback)["cwd"]


def resume_cwd_guard(cwd_ex: Dict[str, str]) -> str:
    """P1-7 纯函数：续聊起点该不该拒启。返回**拒绝原因**（空串 = 放行）。

    只有 `source == 'fallback'`（**记录过** cwd 但目录已消失）才拒；
    `none`（记录里本就没 cwd，如 cursor 空 cwd 条目）是合法情形，照旧回落。

    ⚠ 刻意**不提供请求级放行开关**：`create_session` 的既有安全不变量是
    「只许读 agent_id / session_id / cwd」（`tests/test_term_launch_guard.py`），
    任何新 body 字段都会破它。想在其它目录另起一条，走「不带 session_id 的新会话」那条路。
    """
    if cwd_ex.get("source") == "fallback":
        return ("会话记录的工作目录已不存在: %s —— 拒绝把此当成恢复成功；"
                "若要另起一条，请改用新建会话（不带 session_id）"
                % cwd_ex.get("recorded"))
    return ""


def resume_argv(agent_id: str, session_id: str, cwd: str) -> List[str]:
    st = SESSION_STORES.get(agent_id)
    if not st:
        raise ValueError(f"{agent_id} 不支持历史续聊")
    sid = str(session_id or "")
    if len(sid) > 128 or not st["id_re"].match(sid):
        raise ValueError("session_id 形状非法")
    if not _exists_on_disk(agent_id, sid, cwd):
        raise ValueError("session_id 不在实盘清单内")
    # {cwd} 用会话自己的目录（跳目录后不能用画像 cwd 硬套，否则 qoder 会开错工程）
    real_cwd = session_cwd(agent_id, sid, cwd)
    argv = [t.replace("{id}", sid).replace("{cwd}", real_cwd) for t in st["resume"]]
    for t in argv:                                  # 兜底栅栏：id 已过 ^…\Z，这里护住 cwd
        if any(c in t for c in "\x00\n;|&$`"):
            raise ValueError("拼装结果含可疑字符")
    return argv


# ── 活会话 pid → 中文标题（顶栏芯片去字母用；各 agent 登记表结构均实测）────────
#: P0-8 缓存：live_titles() 是本仓最重的同步 IO（实测 jcode 分支要 json.load
#: 159 个文件 / 29MB，单次 130ms 冷缓存更慢）。它被 term.py 的 async 端点
#: create_session / list_sessions 直接调用 ⇒ 每开一个终端、每 30s 轮询一次列表，
#: 就把整个事件循环按住上百毫秒，期间所有 WebSocket 帧停摆（终端看起来「卡了一下」）。
#: 缓存键 = (agent, 该 agent 目录下最新 mtime, 文件数)——磁盘没动就直接复用，
#: agent 退出/新开会话必然改写自己的 session 文件 ⇒ mtime 变 ⇒ 自动失效。
_LIVE_TITLES_CACHE: Dict[tuple, Dict[int, str]] = {}
_LIVE_TITLES_TTL_S = 15.0
_LIVE_TITLES_TS: Dict[str, float] = {}


def _live_titles_fingerprint(agent_id: str) -> tuple:
    """该 agent 的会话目录指纹（最新 mtime + 文件数）。取不到就返回 None ⇒ 不走缓存。"""
    dirs = {"jcode": HOME / ".jcode" / "sessions",
            "claude": HOME / ".claude" / "sessions",
            "grok": HOME / ".grok" / "sessions"}
    d = dirs.get(agent_id)
    if not d or not d.is_dir():
        return None
    try:
        newest, count = 0.0, 0
        for p in d.iterdir():
            if not p.is_file():
                continue
            count += 1
            m = p.stat().st_mtime
            if m > newest:
                newest = m
        return (round(newest, 3), count)
    except OSError:
        return None


def live_titles_cached(agent_id: str) -> Dict[int, str]:
    """带指纹缓存的 live_titles。同步接口不变（调用方无需改），只在安全时复用旧值。

    缓存只在「目录指纹未变」时生效。指纹读不出来（目录没了/权限）⇒ 直接算，宁可慢
    一次也不能给过期标题——标题错一次用户就以为认错了会话。
    """
    fp = _live_titles_fingerprint(agent_id)
    now = time.time()
    if fp is not None:
        hit = _LIVE_TITLES_CACHE.get((agent_id, fp))
        if hit is not None and now - _LIVE_TITLES_TS.get(agent_id, 0.0) < _LIVE_TITLES_TTL_S:
            return hit
    out = live_titles(agent_id)
    if fp is not None:
        _LIVE_TITLES_CACHE[(agent_id, fp)] = out
        _LIVE_TITLES_TS[agent_id] = now
        # 同 agent 的旧指纹条目最多留一份（mtime 变了就换 key），防无界增长
        for k in [k for k in _LIVE_TITLES_CACHE if k[0] == agent_id and k[1] != fp]:
            _LIVE_TITLES_CACHE.pop(k, None)
    return out


def live_titles(agent_id: str) -> Dict[int, str]:
    out: Dict[int, str] = {}
    try:
        if agent_id == "grok":            # [{"session_id","pid","cwd","opened_at"}]
            for e in _load(HOME / ".grok" / "active_sessions.json"):
                t = _title_of_session("grok", e.get("session_id"))
                if e.get("pid") and t:
                    out[int(e["pid"])] = t
        elif agent_id == "claude":        # ~/.claude/sessions/<pid>.json
            for p in (HOME / ".claude" / "sessions").glob("[0-9]*.json"):
                try:
                    d = _load(p)
                except Exception:  # noqa: BLE001
                    continue
                t = _title_of_session("claude", d.get("sessionId"), d.get("cwd"))
                if d.get("pid") and t:
                    out[int(d["pid"])] = t
        elif agent_id == "hermes":        # {"entries":[{"pid","session_id",...}]}
            db = HOME / ".hermes" / "state.db"
            try:
                entries = _load(HOME / ".hermes" / "runtime" / "active_sessions.json").get("entries", [])
            except Exception:  # noqa: BLE001
                entries = []
            if db.exists():
                for e in entries:
                    if not (e.get("pid") and e.get("session_id")):
                        continue
                    try:
                        with _ro(db) as c:
                            r = c.execute("select title from sessions where id=?", (e["session_id"],)).fetchone()
                    except Exception:  # noqa: BLE001
                        continue
                    if r and r["title"]:
                        out[int(e["pid"])] = mask_title(r["title"])
        elif agent_id == "jcode":         # session json 的 last_pid（实测字段存在）
            for p in (HOME / ".jcode" / "sessions").glob("session_*.json"):
                try:
                    d = _load(p)
                except Exception:  # noqa: BLE001
                    continue
                t = _first_user_text(d.get("messages") or [], jcode=True) or (d.get("title") or "").strip()
                if d.get("last_pid") and t:
                    out[int(d["last_pid"])] = mask_title(t)
    except Exception:  # noqa: BLE001
        return out
    return out


def _title_of_session(agent: str, sid: Optional[str], cwd: Optional[str] = None) -> str:
    st = SESSION_STORES.get(agent or "")
    if not sid or not st or not st["id_re"].match(sid):   # id 会进 glob / SQL，先过形状门
        return ""
    if agent == "grok":
        hit = next(iter((HOME / ".grok" / "sessions").glob(f"*/{sid}/summary.json")), None)
        if not hit:
            return ""
        try:
            d = _load(hit)
        except Exception:  # noqa: BLE001
            return ""
        return mask_title(_first_user_text(_head_lines(hit.parent / "chat_history.jsonl", 20)) or
                          (d.get("session_summary") or "").strip())
    if agent == "claude":
        # id 是 UUID、跨桶唯一 ⇒ 不按 cwd 定位（跳目录口径下条目可能来自任何工程）
        f = next(iter((HOME / ".claude" / "projects").glob(f"*/{sid}.jsonl")), None)
        if f:
            return mask_title(_first_user_text(_head_lines(f, 60)))
    if agent == "jcode":
        f = HOME / ".jcode" / "sessions" / f"{sid}.json"
        if not f.exists():
            return ""
        try:
            d = _load(f)
        except Exception:  # noqa: BLE001
            return ""
        return mask_title(_first_user_text(d.get("messages") or [], jcode=True)
                          or (d.get("title") or "").strip())
    if agent == "hermes":
        try:
            with _ro(HOME / ".hermes" / "state.db") as c:
                r = c.execute("select s.title as t, (select m.content from messages m "
                              "where m.session_id=s.id and m.role='user' and m.active=1 "
                              "order by m.id limit 1) as u from sessions s where s.id=?", (sid,)).fetchone()
        except Exception:  # noqa: BLE001
            return ""
        return mask_title((r["u"] or r["t"]) if r else "")
    if agent == "codex":
        try:
            with _ro(HOME / ".codex" / "state_5.sqlite") as c:
                r = c.execute("select title from threads where id=?", (sid,)).fetchone()
        except Exception:  # noqa: BLE001
            return ""
        return mask_title(r["title"] if r else "")
    if agent == "opencode":
        try:
            with _ro(OPENCODE_DB) as c:
                r = c.execute("select s.title as t, "
                              "(select json_extract(p.data,'$.text') from part p join message m on p.message_id = m.id "
                              "  where m.session_id = s.id and json_extract(m.data,'$.role') = 'user' "
                              "  and json_extract(p.data,'$.type') = 'text' "
                              "  and length(json_extract(p.data,'$.text')) > 0 "
                              "  order by p.time_created limit 1) as u "
                              "from session s where s.id=?", (sid,)).fetchone()
        except Exception:  # noqa: BLE001
            return ""
        t = (r["t"] or "") if r else ""
        return mask_title(t if not t.startswith("New session - ") else ((r["u"] if r else "") or t))
    if agent == "codebuddy":
        # 与 _t_codebuddy 同口径：_store_path 已按 mtime 取跨桶最新那份（列表同一条），
        # 标题走 role=='user' 版抽取（_first_user_text 对本形状恒空）。绝不回落成 id 前缀（D2）。
        f = _store_path("codebuddy", sid)
        if not f:
            return ""
        return mask_title(_codebuddy_first_user(_head_lines(f, 60)))
    if agent == "qoder":
        hit = next(iter((HOME / ".qoder" / "projects").glob(f"*/{sid}.jsonl")), None)
        if not hit:
            return ""
        objs = _head_lines(hit, 400)
        p = next((d.get("lastPrompt", "") for d in objs if d.get("type") == "last-prompt"), "")
        return mask_title(p or _first_user_text(objs))
    if agent == "cursor":
        # 与 _t_cursor 共用 _cursor_title（问题原文优先、meta.title 兜底）——理由见该函数注释。
        # 绝不回落成 id 前缀（D2）。
        return mask_title(_cursor_title(sid, _cursor_meta(sid) or {}))
    if agent == "dsh":
        # 与 _t_dsh 同口径：投影缓存里的标题/首条输入，取不到就空串（D2：不回落成 id 前缀）。
        f = _store_path("dsh", sid)
        if not f:
            return ""
        try:
            return mask_title(_dsh_title_and_cwd(f)[0])
        except Exception:  # noqa: BLE001
            return ""
    if agent == "omp":
        # omp：从 .jsonl 文件第一行的 title 字段取标题。
        f = _store_path("omp", sid)
        if not f:
            return ""
        try:
            for line in open(f, "r", errors="ignore"):
                line = line.strip()
                if not line:
                    continue
                try:
                    d = json.loads(line)
                    if d.get("type") == "title":
                        return mask_title(d.get("title") or "")
                    break
                except Exception:
                    break
        except Exception:  # noqa: BLE001
            pass
        return ""
    return ""


def title_for(agent_id: str, session_id: str, cwd: str = "") -> str:
    """按 id 直查盘上标题。续聊会话本来就知道自己 resume 了哪条（`resume_of`），
       所以不需要依赖 pid 反查——实测 jcode/codex/qoder 的 pid 映射要么只在退出时写、
       要么根本不存在，只靠 live_titles() 会回退成 sid 前缀（正是本次要消除的东西）。"""
    return _title_of_session(agent_id, session_id, cwd)
