"""memindex —— A4 索引投影层（2026-10-02）。

解决的问题（实测非推断）：联邦 10 路源里**只有 claude_mem 有真索引（FTS5）**，其余每次对整棵树
跑 `rg` ⇒ O(文件数) 无索引。2026-10-02 实测单轮 `/api/memory/search`：`pi_sessions` 1894ms、
`codex_sessions` 2028ms、`archived_sessions` 2506ms——三路在源自身 `timeout_s=1.8` 下**必然超时**。

本模块把慢源的语料投影进一个 FTS5 trigram 索引，让它们从「必然超时」变成毫秒级。

设计书：`docs/superpowers/specs/2026-10-02-memindex-a4-design.md`。

四条硬约束（违反任一即为缺陷，改动前先读设计书对应节）：
1. **枚举必须走 os.scandir + realpath**：`~/.codex/sessions` 是**根符号链接**，GNU `find`/`du`
   默认 `-P` 不跟随根软链 ⇒ 实测 `find` 只看到 1 个文件、`du` 报 0，而 `rg`/`os.walk` 看到 273。
   同理**绝不能用 `du`/`find` 估容量**。
2. **realpath 全局去重**：codex_sessions 的 273 个文件实测全部是 archived_sessions 的子集，
   不去重就是索引虚胖。
3. **容错不得中断建库**：`stat`/打开失败**跳过并计数**。实测 `~/.hermes/skills-hot/internet-search`
   枚举后立刻消失，单次盘点 17 例竞态。
4. **水位取 `statvfs(索引父目录)`**：trimafs **按目录树配额**，`df` 的容量取决于用哪条路径问——
   `df /fs` 读 186G 而 `df /fs/1000/ftp/技术文档` 读 30G。量错路径会让磁盘闸门形同虚设，
   建库跑到一半撞配额上限、留下半截的库。
"""

from __future__ import annotations

import fnmatch
import hashlib
import json
import os
import re
import sqlite3
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Dict, Iterable, Iterator, List, Optional, Tuple

HOME = Path.home()
WORKSPACE = Path("/fs/1000/ftp/技术文档")

#: 索引 schema 版本，构建时写入 `memindex.db` 的 `meta.schema_version`。
#: ⚠ 旧注释称它是「/api/memory/health 的版本源」——该端点**不存在（404）**，
#: 现行观测入口是 `/api/memory/vitals` 的 L1 投影判据（它读 `meta.last_build`，
#: 不消费本常量）。见 `memvitals.py` 头部与 2026-10-18 方案 §1。
SCHEMA_VERSION = 1

#: 索引落位。**派生数据**——不是第二权威副本，各源仍各自权威，删了可重建。
DB_PATH = Path(__file__).resolve().parent.parent / "data" / "memindex.db"


def default_db_path() -> Path:
    """解析索引落位。**惰性**读取环境变量——模块级常量在 import 时求值，
    而 hermetic 夹具是 run_tier.py 在 import 之后才设的，写死常量它看不到。

    取值优先级：
      1. ``MEMINDEX_DB``  测试/旁路专用，直接指到 .db 文件本身
      2. ``DATA_DIR``     与全应用同一真源，取其下的 memindex.db
      3. 仓库相对 ``data/``（原行为，兜底）

    2026-10-02：原先此处是无注入口的硬编码，后果是 hermetic 层（假 HOME）下的
    用例仍会去读**生产那份 1.6 GiB 真实索引**——用例把临时语料根 rmtree 掉后
    投影照样答得出来，隔离性被静默破坏且是假绿（比红更坏：红逼人看，假绿只会
    沉淀成"已通过"的错觉）。故开此覆盖口。
    """
    env = os.getenv("MEMINDEX_DB")
    if env:
        return Path(env)
    data_dir = os.getenv("DATA_DIR")
    if data_dir:
        return Path(data_dir) / "memindex.db"
    return Path(__file__).resolve().parent.parent / "data" / "memindex.db"

#: 分块目标长度（字符）。约 1000：再大则命中窗口精度下降，再小则 chunk 数与索引体积线性上���。
CHUNK_TARGET_CHARS = 1000

#: 单文件上限，与 memfed.RG_MAX_FILESIZE=20M 对齐 ⇒ 召回等价于今天的 rg。
MAX_FILE_BYTES = 20 * 1024 * 1024

#: 二进制探测字节数。NUL 出现即判二进制（与 rg 的启发式同向但更保守：小样本）。
BINARY_PROBE_BYTES = 4096

#: trigram 的结构性下限：查询词不足 3 个字符时 FTS5 trigram **无法匹配**。
#: 这不是调优问题，是分词器决定的事实 ⇒ D2 裁定显式回退 rg，响应标 short_query_fallback。
MIN_FTS_CHARS = 3

#: 出站 content 截断长度（与 memfed.CONTENT_SNIPPET 同口径）。
CONTENT_SNIPPET = 300

#: 磁盘水位（D4）。阈值按「技术文档配额」计量，不是设备总量。
DISK_LOW_GB = 8.0          # < ⇒ 拒绝建库/增量，fail-closed 回退 rg
DISK_CRITICAL_GB = 3.0     # < ⇒ 拒绝一切增量

#: 建库容量闸的余量系数与绝对余量。**待 dry_run 校准**（设计书 §1.2 / §5.1）。
#: 初值按「语料 501.2MB → 索引 ~2.0GB」的 4× 膨胀估；实测后必须回填。
BUILD_HEADROOM_FACTOR = 4.0
BUILD_HEADROOM_ABS_GB = 2.0

GB = 1024 ** 3


# ── 语料范围（D5 重裁定 = A2）────────────────────────────────────────
#
# 组成两段：
#   ① rg 口径 —— 严格照 memfed._RG_TARGETS 的 roots + glob，**保证与今天生产逐字等价**；
#   ② grok/hermes 的 skills/docs 定向追加 —— 兑现 D5 原句「含 skills/docs/插件」，
#      同时**排除已证实的非记忆大件**：hermes-agent 1.4G、marketplace-cache 620M、
#      downloads 522M、bin 313M、logs+traces 451M。

#: 家目录里明确**不收**的子目录（非记忆 / 大件 / 可能含凭据）。
SKIP_DIR_NAMES = frozenset({
    "logs", "log", "traces", "trace", "memtrace", "bin", "binaries", "security",
    "shell-snapshots", "connectors-marketplace", "marketplace-cache", "downloads",
    "cache", "node_modules", ".git", "lsp", "vendor", "state", "models",
})

#: 家目录里**额外收录**的顶层子目录（D5 的「skills/docs/插件」）。
EXTRA_HOME_DIRS = frozenset({
    "skills", "docs", "doc", "prompts", "agents", "rules", "plugins",
})

#: 家目录里额外收录的扩展名（顶层不在 EXTRA_HOME_DIRS 时才生效）。
EXTRA_EXT_SUFFIXES = (".md", ".mdx", ".txt")


@dataclass(frozen=True)
class Spec:
    """一路投影源：roots + 可选 glob。glob 为 None ⇒ 目录内全收（照 rg 的 None 档语义）。"""
    sid: str
    roots: Tuple[str, ...]
    glob: Optional[str] = None
    #: True = 家目录定向档（要套 SKIP_DIR_NAMES / EXTRA_HOME_DIRS 过滤）
    home_mode: bool = False


def proj_specs() -> List[Spec]:
    """A2 语料范围。刻意**不复用** memfed._RG_TARGETS：耦合过去会让投影范围被检索范围
    的任何改动静默带偏，而这里要的正是「检索口径为底 + 显式追加」。"""
    w = WORKSPACE
    return [
        # ① rg 口径
        Spec("pi_sessions", (str(HOME / ".pi/agent/sessions"),), "*.jsonl"),
        Spec("codex_sessions", (str(HOME / ".codex/sessions"),), "*.jsonl"),
        Spec("claude_projects", (str(HOME / ".claude/projects"),), "**/memory/*.md"),
        Spec("grok_memory", (str(HOME / ".grok/memory"), str(HOME / ".grok/sessions")),
             "{*.md,prompt_history.jsonl}"),
        Spec("hermes_memory", (str(HOME / ".hermes/memories"), str(HOME / ".hermes/sessions")),
             "{*.md,session_*.json}"),
        Spec("workbuddy_memory", (
            str(HOME / ".workbuddy/USER.md"), str(HOME / ".workbuddy/SOUL.md"),
            str(HOME / ".workbuddy/IDENTITY.md"), str(HOME / ".workbuddy/memory"),
            str(HOME / ".workbuddy/sessions")), "{*.md,*.json}"),
        Spec("workspace_files", (
            str(w / "MEMORY.md"), str(w / "memory"), str(w / "agent-knowledge"),
            str(w / "digest")), "*.md"),
        Spec("archived_sessions", (str(w / "会话备份"),), None),
        # ② skills/docs 定向追加（家目录档）
        Spec("grok_skills_docs", (str(HOME / ".grok"),), None, home_mode=True),
        Spec("hermes_skills_docs", (str(HOME / ".hermes"),), None, home_mode=True),
    ]


#: 去重时的归属优先级：同一 realpath 被多路命中时归给排在前面的那一路。
#: 取「更具体」的路优先，避免整家 home 档把 rg 口径的具体归属吃掉。
DEDUP_PRIORITY = (
    "pi_sessions", "codex_sessions", "claude_projects", "grok_memory",
    "hermes_memory", "workbuddy_memory", "workspace_files", "archived_sessions",
    "grok_skills_docs", "hermes_skills_docs",
)


# ── 枚举（约束 1/2/3 的落点）─────────────────────────────────────────

def _glob_match(rel: str, pattern: str) -> bool:
    """rg 风格 glob。`fnmatch` 不支持 `{a,b}` 花括号，这里自己展开。

    ⚠️ 红向记录：初版写成了 `_fmatch(head + a + tail, rel)`，而签名是
    `_fmatch(name, pattern)` ⇒ **name/pattern 传反**。后果不是报错，是
    **所有花括号 glob 静默匹配 0 个文件**（grok_memory / hermes_memory /
    workbuddy_memory 三路全废），而 `claude_projects` 的 `**/memory/*.md`、
    `pi_sessions` 的 `*.jsonl` 不走花括号所以照常生效 ⇒ 故障只打三分之一的源。
    这是“全指标绿而功能层空”的教科书形态。
    """
    if "{" not in pattern:
        return _fmatch(rel, pattern)
    head, _, rest = pattern.partition("{")
    body, _, tail = rest.partition("}")
    alts = [a for a in body.split(",") if a]
    return any(_fmatch(rel, head + a + tail) for a in alts)


def _fmatch(name: str, pattern: str) -> bool:
    """fnmatch 但 `*` **不跨 `/`**（rg 的 glob 语义；fnmatch 的 `*` 会跨目录）。"""
    if "/" not in pattern:
        return fnmatch.fnmatch(name, pattern)
    regex = (re.escape(pattern)
             .replace(r"\*\*/", "(?:.*/)?")
             .replace(r"\*\*", ".*")
             .replace(r"\*", "[^/]*"))
    return re.fullmatch(regex, name) is not None


@dataclass
class CorpusEntry:
    """一个待投影文件。realpath 是唯一键（约束 2）。"""
    sid: str
    path: str
    realpath: str
    size: int
    mtime: int


@dataclass
class EnumerateReport:
    entries: List[CorpusEntry] = field(default_factory=list)
    #: sid -> 该路命中文件数（去重前，用于探针报「覆盖 N 文件」）
    per_source: Dict[str, int] = field(default_factory=dict)
    bytes_total: int = 0
    #: 竞态/权限失败计数。>0 属**正常**，不是失败（约束 3）。
    skipped_race: int = 0
    #: 被二进制/超大/空文件过滤掉的计数
    skipped_binary: int = 0
    skipped_oversize: int = 0
    dup_collapsed: int = 0


def _is_binary(path: Path) -> Optional[bool]:
    """True=二进制 / False=文本 / **None=读不到**。

    三态而不是两态：早期版本把打开失败当作「是二进制」，于是**权限错误、文件已删
    这两类真实故障被静默归类成「二进制文件」**——而二进制是正常过滤、读不到是竞态，
    两者计数分开（约束 3）才有可用的诊断信号。
    """
    try:
        with open(path, "rb") as f:
            return b"\x00" in f.read(BINARY_PROBE_BYTES)
    except OSError:
        return None


def _iter_scandir(root: Path) -> Iterator[Path]:
    """os.scandir 手写递归（约束 1）。**不用 os.walk**：需要自己控住
    「跳过目录」与「根符号链接跟随」两处语义。
    """
    try:
        it = os.scandir(root)
    except OSError:
        return
    with it:
        try:
            entries = list(it)
        except OSError:
            return
        for e in entries:
            p = Path(e.path)
            try:
                is_dir = e.is_dir(follow_symlinks=True)
            except OSError:
                continue
            if is_dir:
                yield from _iter_scandir(p)
            else:
                yield p


def enumerate_corpus(specs: Optional[List[Spec]] = None) -> EnumerateReport:
    """按 A2 范围枚举语料。realpath 全局去重，归属按 DEDUP_PRIORITY。

    任何单文件的 stat/打开失败都只计入 skipped_race 并继续（约束 3）。
    """
    specs = specs or proj_specs()
    rep = EnumerateReport()
    seen: Dict[str, CorpusEntry] = {}
    # 同一 realpath 可能被多路看见：按优先级决定归属，先到先占但低优先路的先跑。
    order = sorted(specs, key=lambda s: DEDUP_PRIORITY.index(s.sid)
                    if s.sid in DEDUP_PRIORITY else len(DEDUP_PRIORITY))

    for spec in order:
        n = 0
        for root_s in spec.roots:
            root = Path(root_s)
            if not os.path.exists(root_s):
                continue
            if root.is_file():
                cands = [root]
                base = root.parent
            else:
                cands = _iter_scandir(root)
                base = root
            for p in cands:
                try:
                    rp = os.path.realpath(p)
                except OSError:
                    rep.skipped_race += 1
                    continue
                if rp in seen:
                    rep.dup_collapsed += 1
                    continue
                if spec.glob is not None:
                    try:
                        rel = os.path.relpath(str(p), str(base))
                    except ValueError:
                        continue
                    if not _glob_match(rel, spec.glob):
                        continue
                elif spec.home_mode:
                    # 家目录档：排除非记忆大件，只收 EXTRA_HOME_DIRS 或文档类扩展名
                    try:
                        rel = Path(os.path.relpath(str(p), str(base)))
                    except ValueError:
                        continue
                    parts = rel.parts[:-1]
                    if parts and any(x in SKIP_DIR_NAMES for x in parts):
                        continue
                    top = parts[0] if parts else ""
                    in_extra = top in EXTRA_HOME_DIRS
                    is_doc = str(p).lower().endswith(EXTRA_EXT_SUFFIXES)
                    # 顶层就是 skills/docs 这类目录 ⇒ 收；否则只有文档类扩展名才收
                    if not (in_extra or is_doc):
                        continue
                try:
                    st = os.stat(p)
                except OSError:
                    rep.skipped_race += 1
                    continue
                if st.st_size == 0:
                    continue
                if st.st_size > MAX_FILE_BYTES:
                    rep.skipped_oversize += 1
                    continue
                try:
                    is_bin = _is_binary(p)
                except OSError:
                    rep.skipped_race += 1
                    continue
                if is_bin is None:          # 读不到 ⇒ 竞态，不是二进制
                    rep.skipped_race += 1
                    continue
                if is_bin:
                    rep.skipped_binary += 1
                    continue
                seen[rp] = CorpusEntry(spec.sid, str(p), rp, st.st_size, int(st.st_mtime))
                n += 1
        rep.per_source[spec.sid] = n

    rep.entries = sorted(seen.values(), key=lambda e: (e.sid, e.path))
    rep.bytes_total = sum(e.size for e in rep.entries)
    return rep


# ── 分块（格式无关，D3）──────────────────────────────────────────────

def chunk_text(text: str, target: int = CHUNK_TARGET_CHARS
               ) -> Iterator[Tuple[int, int, int, str]]:
    """`text` → (l1, l2, chunk_no, chunk)。短行累积到 target 成一条；**超长行整行不切**。

    为什么超长行不切：pi 会话平均 1707 字符/行、76% 的行 >200 字符（2026-10-02 实测），
    硬切会破坏「一条消息」的语义边界，检索出来是半句话。
    """
    lines = text.splitlines()
    buf: List[str] = []
    buf_len = 0
    start = 1
    n = 0
    for i, ln in enumerate(lines, start=1):
        if buf_len and buf_len + len(ln) > target:
            yield start, i - 1, n, "\n".join(buf)
            n += 1
            buf, buf_len, start = [], 0, i
        buf.append(ln)
        buf_len += len(ln) + 1
        if buf_len >= target:
            yield start, i, n, "\n".join(buf)
            n += 1
            buf, buf_len, start = [], 0, i + 1
    if buf:
        yield start, start + len(buf) - 1, n, "\n".join(buf)


# ── 磁盘闸（约束 4 的落点）───────────────────────────────────────────

def disk_free_bytes(path: Optional[Path] = None) -> int:
    """索引**父目录**的可用字节。刻意不接受任意路径：调用方必须显式给出索引所在目录。

    trimafs 按目录树配额 ⇒ `df /fs` 读 186G 而 `df /fs/1000/ftp/技术文档` 读 30G。
    量错路径 ⇒ 「可用 > 8G」恒真 ⇒ 闸门虚设 ⇒ 建库撞配额留半截库。
    """
    target = Path(path) if path else default_db_path().parent
    target = _nearest_existing(target)
    st = os.statvfs(str(target))
    return st.f_bavail * st.f_frsize


def _nearest_existing(p: Path) -> Path:
    """向上找最近的存在目录。`statvfs` 对不存在的路径抛 FileNotFoundError，
    而首次建库时 `data/` 可能还没建 ⇒ 不能让容量闸在开工前先崩。"""
    cur = p
    while True:
        if cur.exists():
            return cur
        parent = cur.parent
        if parent == cur:
            return cur
        cur = parent


def disk_state(free: Optional[int] = None) -> str:
    """ok / low / critical。**绝不删索引**：索引是我生成的派生数据，
    但我没资格替用户销毁已经建好的东西（D4）。

    **写闸与读闸故意不同线**（设计书 §5 补充）：
      - `low`  ⇒ 拒**写**（建库/增量）：写才会吃空间；
      - `critical` ⇒ 额外拒**读**：只剩几 G 时 SQLite 的 WAL/temp 也可能落不下去，
        在这种水位上继续查等于拿一个可能半损坏的库赌召回。
    读闸设在 critical 而不是 low，是因为**读索引不消耗空间**——若在 7.9G 就停止召回，
    用户会看到「明明有索引却什么都不返回」，那正是本层要消灭的静默丢召回。
    """
    free = disk_free_bytes() if free is None else free
    if free < DISK_CRITICAL_GB * GB:
        return "critical"
    if free < DISK_LOW_GB * GB:
        return "low"
    return "ok"


# ── 库 ──────────────────────────────────────────────────────────────

SCHEMA = """
CREATE TABLE IF NOT EXISTS fed_proj(
  source TEXT, path TEXT, realpath TEXT,
  l1 INT, l2 INT,
  mtime INT, size INT,
  chunk_no INT, text TEXT);
CREATE UNIQUE INDEX IF NOT EXISTS ux_proj ON fed_proj(realpath, chunk_no);
CREATE VIRTUAL TABLE IF NOT EXISTS fed_proj_fts USING fts5(
  text, content='fed_proj', content_rowid='rowid', tokenize='trigram');
CREATE TRIGGER IF NOT EXISTS fed_proj_ai AFTER INSERT ON fed_proj BEGIN
  INSERT INTO fed_proj_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TRIGGER IF NOT EXISTS fed_proj_ad AFTER DELETE ON fed_proj BEGIN
  INSERT INTO fed_proj_fts(fed_proj_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
END;
CREATE TRIGGER IF NOT EXISTS fed_proj_au AFTER UPDATE ON fed_proj BEGIN
  INSERT INTO fed_proj_fts(fed_proj_fts, rowid, text) VALUES ('delete', old.rowid, old.text);
  INSERT INTO fed_proj_fts(rowid, text) VALUES (new.rowid, new.text);
END;
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);
"""


def connect(db_path: Optional[Path] = None, readonly: bool = False) -> sqlite3.Connection:
    p = Path(db_path) if db_path else default_db_path()
    if readonly:
        try:
            return sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        except sqlite3.OperationalError:
            return sqlite3.connect(f"file:{p}?mode=ro&immutable=1", uri=True, timeout=1.0)
    p.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(p), timeout=30.0)
    con.execute("PRAGMA journal_mode=WAL")
    con.execute("PRAGMA synchronous=NORMAL")
    return con


def _meta_set(con: sqlite3.Connection, k: str, v) -> None:
    con.execute("INSERT INTO meta(k,v) VALUES(?,?) ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (k, json.dumps(v, ensure_ascii=False) if not isinstance(v, str) else v))


def _meta_get(con: sqlite3.Connection, k: str, default=None):
    row = con.execute("SELECT v FROM meta WHERE k=?", (k,)).fetchone()
    return row[0] if row else default


# ── 构建 ────────────────────────────────────────────────────────────

def _insert_file(con: sqlite3.Connection, e: CorpusEntry, stats: Dict[str, int]) -> int:
    try:
        text = Path(e.path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        stats["skipped_race"] += 1
        return 0
    rows = [(e.sid, e.path, e.realpath, l1, l2, e.mtime, e.size, cn, tx)
            for (l1, l2, cn, tx) in chunk_text(text)]
    if rows:
        con.executemany(
            "INSERT INTO fed_proj(source,path,realpath,l1,l2,mtime,size,chunk_no,text) "
            "VALUES(?,?,?,?,?,?,?,?,?)", rows)
        stats["chunks"] += len(rows)
    return len(rows)


@dataclass
class BuildReport:
    scope: str
    files: int = 0
    chunks: int = 0
    bytes_corpus: int = 0
    bytes_db: int = 0
    skipped_race: int = 0
    skipped_binary: int = 0
    skipped_oversize: int = 0
    dup_collapsed: int = 0
    per_source: Dict[str, int] = field(default_factory=dict)
    elapsed_s: float = 0.0
    dry_run: bool = False
    est_final_bytes: int = 0
    disk_free_before: int = 0
    notes: List[str] = field(default_factory=list)
    ok: bool = True


def sample_estimate(rep: EnumerateReport, sample: int = 2000) -> Dict[str, float]:
    """按 D3 规则采样前 N 个文件实测 chunk 密度，外推索引体积。

    容量闸的输入。**外推而非拍脑袋**：D6 初版的「1.5 倍余量」是拍的，
    设计书 §5.1 已把它标为待校准项。
    """
    pool = rep.entries[:sample]
    if not pool:
        return {"sample_files": 0, "sample_bytes": 0, "sample_chunks": 0}
    con = sqlite3.connect(":memory:")
    con.executescript(SCHEMA)
    stats = {"chunks": 0, "skipped_race": 0}
    sb = 0
    for e in pool:
        sb += e.size
        _insert_file(con, e, stats)
    con.commit()
    con.close()
    scale = len(rep.entries) / max(1, len(pool))
    return {
        "sample_files": len(pool),
        "sample_bytes": sb,
        "sample_chunks": stats["chunks"],
        "scaled_chunks": int(stats["chunks"] * scale),
        "bytes_corpus": rep.bytes_total,
    }


def build(scope: str = "full", db_path: Optional[Path] = None,
          sample: int = 2000, batch: int = 2000,
          specs: Optional[List[Spec]] = None) -> BuildReport:
    """建库。scope ∈ {full, dry_run, incremental}。

    磁盘闸在**开写之前**判：水位不足 ⇒ 直接返回 ok=False，调用方 fail-closed 回退 rg，
    绝不自动删索引（D4）。`specs` 只给测试注入夹具用，生产走 proj_specs()。
    """
    t0 = time.monotonic()
    db_path = Path(db_path) if db_path else default_db_path()
    rep = enumerate_corpus(specs)
    br = BuildReport(scope=scope, files=len(rep.entries),
                     bytes_corpus=rep.bytes_total,
                     skipped_race=rep.skipped_race,
                     skipped_binary=rep.skipped_binary,
                     skipped_oversize=rep.skipped_oversize,
                     dup_collapsed=rep.dup_collapsed,
                     per_source=dict(rep.per_source))

    if scope == "dry_run":
        est = sample_estimate(rep, sample=sample)
        br.dry_run = True
        br.est_final_bytes = int(est.get("scaled_chunks", 0) * 0)  # 占位，下面重算
        br.disk_free_before = disk_free_bytes(db_path.parent)
        # 索引体积按「每 chunk 均摊字符数」外推，实测语料里 trigram 索引 ≈ 语料 × EXPANSION
        n_chunks = max(1, est.get("scaled_chunks", 1))
        avg_chunk = max(1, rep.bytes_total / n_chunks)
        br.est_final_bytes = int(n_chunks * avg_chunk * _FTS_EXPANSION)
        br.chunks = n_chunks
        br.notes.append(f"sample_files={est.get('sample_files')} "
                        f"sampled_chunks={est.get('sample_chunks')} "
                        f"avg_chunk_chars={avg_chunk}")
        br.notes.append(f"est_final_bytes={br.est_final_bytes} "
                        f"({br.est_final_bytes / GB:.2f} GB)")
        need = br.est_final_bytes * BUILD_HEADROOM_FACTOR + BUILD_HEADROOM_ABS_GB * GB
        br.notes.append(f"disk_free={br.disk_free_before / GB:.1f} GB  "
                        f"gate_need={need / GB:.1f} GB "
                        f"(est×{BUILD_HEADROOM_FACTOR}+{BUILD_HEADROOM_ABS_GB}GB)")
        br.ok = br.disk_free_before >= need
        if not br.ok:
            br.notes.append("容量闸未过：拒绝建库（fail-closed 回退 rg，不自动删索引）")
        br.elapsed_s = time.monotonic() - t0
        return br

    # ── 写库 ──
    state = disk_state(disk_free_bytes(db_path.parent))
    br.disk_free_before = disk_free_bytes(db_path.parent)
    if state != "ok":
        br.ok = False
        br.notes.append(f"磁盘水位 {state}：拒绝写入，回退 rg")
        br.elapsed_s = time.monotonic() - t0
        return br

    if scope == "full" and db_path.exists():
        br.notes.append("scope=full：清空旧投影后重建")
    con = connect(db_path)
    try:
        con.executescript(SCHEMA)
        if scope == "full":
            con.execute("DELETE FROM fed_proj")
            con.commit()
        elif scope == "incremental":
            known = {r[0]: (r[1], r[2]) for r in
                     con.execute("SELECT DISTINCT realpath, mtime, size FROM fed_proj")}
            changed = [e for e in rep.entries
                       if known.get(e.realpath) != (e.mtime, e.size)]
            gone = set(known) - {e.realpath for e in rep.entries}
            # 2026-10-18 修：变更文件必须**先删旧行再插**。原实现只删 gone，
            # 变更文件直接 INSERT ⇒ 第二个 chunk 就撞 UNIQUE(realpath, chunk_no)；
            # 而失败点前的批次已提交 ⇒ 库里留下同文件旧+新两份（静默脏数据）。
            # 这也解释了 memindex.db 自 2026-10-02 起就再没更新过。
            for rp in list(gone) + [e.realpath for e in changed]:
                con.execute("DELETE FROM fed_proj WHERE realpath=?", (rp,))
            con.commit()
            br.files = len(changed)
            br.notes.append(f"incremental: changed={len(changed)} removed={len(gone)} "
                            f"total_seen={len(rep.entries)}")
            rep.entries = changed
            br.bytes_corpus = sum(e.size for e in changed)

        stats = {"chunks": 0, "skipped_race": 0}
        n = 0
        for e in rep.entries:
            _insert_file(con, e, stats)
            n += 1
            if n % batch == 0:
                con.commit()          # 分批提交：压 WAL 峰值
        con.commit()
        con.execute("INSERT INTO fed_proj_fts(fed_proj_fts) VALUES('optimize')")
        con.commit()
        br.chunks = stats["chunks"]
        br.skipped_race = rep.skipped_race + stats["skipped_race"]
        n_fts = con.execute("SELECT COUNT(*) FROM fed_proj_fts").fetchone()[0]
        br.notes.append(f"fed_proj_fts rows={n_fts}")
        if n_fts == 0:
            br.ok = False
            br.notes.append("建库后 FTS 行数为 0 —— 触发器时序错（G9 红向）")
        _meta_set(con, "schema_version", SCHEMA_VERSION)
        _meta_set(con, "last_build", f"{time.strftime('%Y-%m-%dT%H:%M:%S')}")
        _meta_set(con, "index_age", "0")
        # 2026-10-18 修：incremental 时 rep.entries 只剩 changed，不能拿它当总数写 meta
        # （否则 probe 报「投影 37 文件」，而库里是 219k 行 —— 旧代码就是这个洞）。
        total_files = con.execute("SELECT COUNT(DISTINCT realpath) FROM fed_proj").fetchone()[0]
        total_chunks = con.execute("SELECT COUNT(*) FROM fed_proj").fetchone()[0]
        _meta_set(con, "files", str(total_files))
        _meta_set(con, "chunks", str(total_chunks))
        _meta_set(con, "corpus_bytes", str(br.bytes_corpus))
        con.commit()
    finally:
        con.close()

    br.bytes_db = _dir_size(db_path)
    br.elapsed_s = time.monotonic() - t0
    br.notes.append(f"db_bytes={br.bytes_db} ({br.bytes_db / GB:.2f} GB) "
                    f"elapsed={br.elapsed_s:.1f}s")
    return br


def _dir_size(p: Path) -> int:
    tot = 0
    try:
        for f in p.parent.glob(p.name + "*"):
            try:
                tot += f.stat().st_size
            except OSError:
                pass
    except OSError:
        pass
    return tot


#: trigram 索引相对语料的膨胀倍数（实测校准项，初值按经验 4×）。
_FTS_EXPANSION = 4.0


# ── 查询 ────────────────────────────────────────────────────────────

def is_short_query(q: str) -> bool:
    """查询词不足 MIN_FTS_CHARS 个字符 ⇒ trigram 结构上匹配不了（D2）。"""
    return len((q or "").strip()) < MIN_FTS_CHARS


def _fts_term(q: str) -> str:
    """整包引号短语 + 内部引号剔除（与 memfed._fts_term 同口径：字面包含，对齐 rg -F）。"""
    inner = re.sub(r'["\s]+', " ", q or "").strip()
    return '"' + inner.replace('"', "") + '"'


def _hit_window(text: str, q: str, width: int = CONTENT_SNIPPET) -> str:
    """命中窗口。**不用 snippet()**：出站要的是可回溯的 `path:l1-l2` 行号区间，
    而 D6 全文入库后上下文本来就在库里，自算才能给区间。"""
    if not text:
        return ""
    low = text.lower()
    pos = low.find((q or "").strip().lower())
    if pos < 0:
        return text[:width]
    start = max(0, pos - width // 3)
    out = text[start:start + width]
    return ("…" if start > 0 else "") + out + ("…" if start + width < len(text) else "")


def fmt_ts(epoch: Optional[int]) -> Optional[str]:
    """epoch 秒 → `2026-10-16T12:00:00Z`。

    **复用 `memfed.py:581` 的既有口径**（`strftime %Y-%m-%dT%H:%M:%SZ` + `gmtime`），
    不新建第三套时间格式——跨模块的 `updated_at` 必须逐字节同形，否则
    `_recent_rg` 补的时间戳和本层回的时间戳会在融合后混排失序。
    """
    if epoch is None:
        return None
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(int(epoch)))
    except (TypeError, ValueError, OSError, OverflowError):
        return None


def _built_epoch(db_path: Optional[Path] = None) -> Optional[float]:
    """索引文件的 mtime（epoch 秒）——**不解析 meta 里的 `last_build` 字符串**。

    沿用 `kb.py:362` 已定的口径：实测 `last_build` 是 `2026-10-02T18:07:33` 这种
    **无时区**形态，与 aware datetime 相减直接 TypeError（kb 的闸门第一版就死在这，
    只捕了 ValueError）。索引文件 mtime 是内核记的 UTC epoch，无时区歧义，
    且「rebuild 没回写 meta 字符串、mtime 仍会动」⇒ 更接近真相。
    """
    try:
        p = Path(db_path) if db_path else default_db_path()
        return os.path.getmtime(p)
    except (OSError, TypeError, ValueError):
        return None


def annotate_freshness(items: List[Dict], built_at: Optional[str],
                       built_epoch: Optional[float]) -> List[Dict]:
    """就地给每条 item 补新鲜度标注（P1-8 / 手册 B4）。

    只补两个字段，**不改排序**：B5「时近性权重」是独立一步（会改召回序，
    属设计决策），本函数只解决 B4 的「区分『答案过期』与『检索失败』」。

    补什么：
    - `updated_at`：该 chunk 所属文件在**建索引那一刻**的 mtime（不是当前磁盘 mtime）。
    - `index_age_days`：索引本身建了多久。**这是本项的承重字段**——只给 `updated_at`
      会让下游把「快照里的文件时间」误读成「文件现状」：实测 2026-10-16 索引
      `last_build=2026-10-02`，而 9107 个文件里 **65 个磁盘 mtime 比索引新**（最旧漂 92 天）、
      182 个已从磁盘消失。不给这个量，下游拿到的仍是「看起来完整的过期答案」。

    `built_at` 为 None（索引刚建/meta 缺失）时 age 给 None 而不是 0：
    「不知道多旧」与「刚刚建的」必须可区分，填 0 就是在制造新的静默错误。
    """
    age = None
    if built_epoch is not None:
        try:
            age = round(max(0.0, (time.time() - float(built_epoch)) / 86400.0), 1)
        except (TypeError, ValueError):
            age = None
    for it in items:
        # 无条件赋值（而不是「有值才补」）：`updated_at` 这个键必须**总是存在**，
        # 取不到时是 `None`。不留 `"updated_at": None` 占位再事后覆盖 ——
        # 那个占位就是「看起来填了、实则没填」的静默形态。
        if it.get("updated_at") is None:
            it["updated_at"] = fmt_ts(it.get("_mtime"))
        it.pop("_mtime", None)
        if age is not None:
            it["index_age_days"] = age
        if built_at is not None:
            it["index_built_at"] = built_at
    return items


def search(q: str, limit: int = 10, db_path: Optional[Path] = None,
           sanitize: Optional[Callable[[str], str]] = None,
           sources: Optional[Iterable[str]] = None) -> Dict:
    """查投影索引。返回 {ok, count, items, ms, short_query?, degraded?}。

    **短查询（<3 字）不查 trigram**：返回 ok=True 但 `fallback="short_query"`，
    由调用方走 rg——本模块**不自己降级**，避免两个降级源互相掩盖。
    """
    t0 = time.monotonic()
    if is_short_query(q):
        return {"ok": True, "count": 0, "items": [], "fallback": "short_query",
                "ms": round((time.monotonic() - t0) * 1000, 3)}
    db_path = Path(db_path) if db_path else default_db_path()
    if not db_path.exists():
        return {"ok": False, "count": 0, "items": [], "fallback": "no_index",
                "error": "索引不存在", "ms": round((time.monotonic() - t0) * 1000, 3)}

    state = disk_state(disk_free_bytes(db_path.parent))
    if state == "critical":
        return {"ok": False, "count": 0, "items": [], "fallback": "low_disk",
                "degraded": "proj_disabled:low_disk",
                "error": "磁盘水位 critical，索引只读暂停",
                "ms": round((time.monotonic() - t0) * 1000, 3)}

    want = set(sources) if sources else None
    sql = ("SELECT f.source, f.path, f.l1, f.l2, f.text, f.mtime FROM fed_proj_fts "
           "JOIN fed_proj f ON f.rowid = fed_proj_fts.rowid "
           "WHERE fed_proj_fts MATCH ? ")
    args: List = [_fts_term(q)]
    if want:
        sql += "AND f.source IN (%s) " % ",".join("?" * len(want))
        args += sorted(want)
    sql += "ORDER BY rank LIMIT ?"
    args.append(max(1, int(limit)))

    try:
        con = connect(db_path, readonly=True)
    except sqlite3.Error as e:
        return {"ok": False, "count": 0, "items": [], "fallback": "corrupt",
                "error": f"{type(e).__name__}: {e}"[:160],
                "ms": round((time.monotonic() - t0) * 1000, 3)}
    try:
        try:
            rows = con.execute(sql, args).fetchall()
        except sqlite3.Error as e:
            return {"ok": False, "count": 0, "items": [], "fallback": "query_error",
                    "error": f"{type(e).__name__}: {e}"[:160],
                    "ms": round((time.monotonic() - t0) * 1000, 3)}
        items = []
        for source, path, l1, l2, text, mtime in rows:
            body = _hit_window(text or "", q)
            name = os.path.basename(path)
            items.append({
                "source": source, "id": f"{path}:{l1}-{l2}",
                "title": (sanitize(name) if sanitize else name)[:120],
                "content": (sanitize(body) if sanitize else body),
                "category": "session" if str(source).endswith("sessions") else "knowledge",
                # `updated_at` 由 annotate_freshness 从这个 epoch 中间值转出；
                # 此处**不预置占位**（预置 `None` 会让「已标注」与「待标注」同形）。
                "_mtime": mtime,
            })
        built_at = _meta_get(con, "last_build")
    finally:
        con.close()
    annotate_freshness(items, built_at, _built_epoch(db_path))
    return {"ok": True, "count": len(items), "items": items,
            "ms": round((time.monotonic() - t0) * 1000, 3)}


def probe(db_path: Optional[Path] = None) -> Dict:
    """健康探针。**不跑 `rg --files`**（那是 O(文件数) 的老毛病），
    改读 meta：索引覆盖 N 文件 / M chunk / 最后构建于 T。"""
    t0 = time.monotonic()
    db_path = Path(db_path) if db_path else default_db_path()
    if not db_path.exists():
        return {"ok": False, "count": 0, "note": "索引不存在（查询走 rg）",
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    try:
        con = connect(db_path, readonly=True)
    except sqlite3.Error as e:
        return {"ok": False, "count": 0, "note": f"{type(e).__name__}",
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    try:
        files = int(_meta_get(con, "files", "0") or 0)
        chunks = int(_meta_get(con, "chunks", "0") or 0)
        built = _meta_get(con, "last_build", "?")
        live = con.execute("SELECT COUNT(*) FROM fed_proj").fetchone()[0]
    except sqlite3.Error as e:
        return {"ok": False, "count": 0, "note": f"{type(e).__name__}",
                "ms": round((time.monotonic() - t0) * 1000, 1)}
    finally:
        con.close()
    stale = "" if live == chunks else f" ⚠行数漂移 {chunks}→{live}"
    return {"ok": True, "count": files,
            "note": f"投影 {files} 文件 / {chunks} chunk / {live} 行，构建于 {built}{stale}",
            "ms": round((time.monotonic() - t0) * 1000, 1)}


# ── CLI ─────────────────────────────────────────────────────────────

def _main(argv: List[str]) -> int:
    import argparse
    ap = argparse.ArgumentParser(prog="memindex", description="A4 索引投影层")
    ap.add_argument("--db", default=None, help="索引路径（默认 data/memindex.db）")
    ap.add_argument("--json", action="store_true")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name, help_ in (("build", "建库"), ("dry-run", "容量估算（不写库）"),
                        ("incremental", "增量刷新"), ("probe", "健康探针"),
                        ("search", "查索引")):
        s = sub.add_parser(name, help=help_)
        if name in ("build", "dry-run", "incremental"):
            s.add_argument("--sample", type=int, default=2000)
        if name == "search":
            s.add_argument("q")
            s.add_argument("--limit", type=int, default=10)
    a = ap.parse_args(argv)
    db = Path(a.db) if a.db else None

    if a.cmd == "probe":
        out = probe(db)
    elif a.cmd == "search":
        out = search(a.q, a.limit, db_path=db)
    elif a.cmd == "dry-run":
        out = build("dry_run", db_path=db, sample=a.sample)
    else:
        out = build(a.cmd, db_path=db, sample=getattr(a, "sample", 2000))

    if a.json:
        d = out if isinstance(out, dict) else out.__dict__
        print(json.dumps(d, ensure_ascii=False, indent=2, default=str))
    else:
        for k, v in (out.items() if isinstance(out, dict) else out.__dict__.items()):
            print(f"{k}: {v}")
    return 0


if __name__ == "__main__":
    raise SystemExit(_main(sys.argv[1:]))