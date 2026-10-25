"""catalog 统一编目库 —— schema 定义与幂等迁移（PT-20261025-12 P0）。

**这个库是什么**：全部对象（agent / 技能 / 项目 / 会话 / 记忆 / 文档 / 日志 / 任务）的
**单一编目库**：目录行 + 分域扩展表 + 分块全文索引 + 关系 + 扫描账本。
它是**派生数据**，不是权威：权威仍在 TDAI / XERJ / 各 agent 家目录 / agents.db。

**设计判据（方案文档 §4）**：
- 对象 id 由 `sha1(domain|src_id|native_key)[:16]` 派生 ⇒ 重扫不变、幂等 upsert；
- 每张表 `IF NOT EXISTS` + `schema_version` 版本守卫 ⇒ 迁移可反复跑；
- 全部建在同一文件里，`ATTACH` 迁入旧 memindex 时只需一个 `PRAGMA` 组。

**不做什么**：不建第五个库（它吸收 memindex.db，库数净不增）；不改现有业务表。
"""
from __future__ import annotations

import sqlite3
from typing import Dict, List, Optional, Tuple

#: 当前 schema 版本。升版本只许**加表/加列**，删列必须走新库 + 迁移脚本。
SCHEMA_VERSION = 1

#: pragma 基线（方案 §8.2）。按需连接后由 store.apply_pragmas() 执行，
#: 这里单独列出来是为了测试可断言。
PRAGMAS: Tuple[Tuple[str, str], ...] = (
    ("page_size", "8192"),
    ("journal_mode", "WAL"),
    ("synchronous", "NORMAL"),
    ("busy_timeout", "5000"),
    ("temp_store", "MEMORY"),
    ("cache_size", "-262144"),   # 256MB
    ("mmap_size", "268435456"),  # 256MB
    ("wal_autocheckpoint", "2000"),
)

#: 全量 DDL。**顺序即依赖顺序**：src → obj → x_* → chunk → FTS → 其余。
DDL: Tuple[str, ...] = (
    # ① 源登记：把 datastore.BACKENDS 从代码常量变成可查数据
    """CREATE TABLE IF NOT EXISTS src (
  src_id      TEXT PRIMARY KEY,
  domain      TEXT NOT NULL,
  kind        TEXT NOT NULL,
  authority   INTEGER NOT NULL DEFAULT 0,
  uri         TEXT NOT NULL,
  adapter     TEXT NOT NULL,
  scan_mode   TEXT NOT NULL,
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_ok_at  TEXT, last_ms INTEGER, last_err TEXT,
  cfg         TEXT
)""",
    """CREATE TABLE IF NOT EXISTS obj (
  oid         TEXT PRIMARY KEY,
  domain      TEXT NOT NULL,
  type        TEXT NOT NULL,
  src_id      TEXT NOT NULL REFERENCES src(src_id),
  native_key  TEXT NOT NULL,
  uri         TEXT,
  title       TEXT, summary TEXT, lang TEXT,
  mtime       INTEGER, size INTEGER,
  hash        TEXT,
  status      TEXT NOT NULL DEFAULT 'ok',
  meta        TEXT,
  first_seen  TEXT NOT NULL, last_seen TEXT NOT NULL,
  UNIQUE(domain, src_id, native_key)
)""",
    "CREATE INDEX IF NOT EXISTS obj_domain_mtime ON obj(domain, mtime DESC)",
    "CREATE INDEX IF NOT EXISTS obj_hash ON obj(hash)",
    # ③ 分域扩展表
    """CREATE TABLE IF NOT EXISTS x_agent(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  agent_id TEXT, installed INTEGER, native_ui INTEGER, ports TEXT,
  models TEXT, states TEXT)""",
    """CREATE TABLE IF NOT EXISTS x_skill(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  name TEXT, route TEXT, dir TEXT, description TEXT,
  tokens INTEGER, enabled INTEGER)""",
    """CREATE TABLE IF NOT EXISTS x_project(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  root TEXT, name TEXT, branch TEXT, dirty INTEGER,
  last_commit TEXT, worktrees INTEGER)""",
    """CREATE TABLE IF NOT EXISTS x_session(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  agent TEXT, cwd TEXT, project_oid TEXT,
  started_at INTEGER, ended_at INTEGER, msgs INTEGER, bytes INTEGER, model TEXT)""",
    """CREATE TABLE IF NOT EXISTS x_memory(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  layer TEXT, category TEXT, source TEXT, created_at TEXT, score REAL)""",
    """CREATE TABLE IF NOT EXISTS x_doc(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  section TEXT, slug TEXT, words INTEGER, tags TEXT)""",
    """CREATE TABLE IF NOT EXISTS x_log(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  unit TEXT, level TEXT, ts INTEGER, msg_hash TEXT)""",
    """CREATE TABLE IF NOT EXISTS x_task(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  pt_id TEXT, status TEXT, owner TEXT)""",
    """CREATE TABLE IF NOT EXISTS x_service(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  name TEXT, command TEXT, port INTEGER, endpoint TEXT,
  auto_restart INTEGER, unit TEXT)""",
    # ④ 分块 + 双 FTS
    """CREATE TABLE IF NOT EXISTS chunk(
  cid INTEGER PRIMARY KEY,
  oid TEXT NOT NULL REFERENCES obj(oid) ON DELETE CASCADE,
  seq INTEGER NOT NULL, text TEXT NOT NULL, chars INTEGER)""",
    "CREATE INDEX IF NOT EXISTS chunk_oid ON chunk(oid, seq)",
    # ≥3 字：trigram，子串精确（索引约 3~4× 原文）
    """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  text, content='chunk', content_rowid='cid', tokenize='trigram')""",
    # 1~2 字：按字切分的 unicode61，短语查询 '"记 忆"' 可命中
    """CREATE TABLE IF NOT EXISTS chunk_uni(
  cid INTEGER PRIMARY KEY, oid TEXT, text_uni TEXT)""",
    """CREATE VIRTUAL TABLE IF NOT EXISTS chunk_ftsu USING fts5(
  text_uni, content='chunk_uni', content_rowid='cid', tokenize='unicode61')""",
    # ⑤ 关系
    """CREATE TABLE IF NOT EXISTS edge(
  src_oid TEXT, dst_oid TEXT, rel TEXT, weight REAL DEFAULT 1, meta TEXT,
  PRIMARY KEY(src_oid, dst_oid, rel))""",
    # ⑥ 扫描账本
    """CREATE TABLE IF NOT EXISTS scan_run(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, src_id TEXT, mode TEXT,
  started_at TEXT, finished_at TEXT, status TEXT,
  added INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, ms INTEGER, note TEXT)""",
    # ⑦ 别名 / 去重
    """CREATE TABLE IF NOT EXISTS alias(
  alias TEXT, oid TEXT, kind TEXT, PRIMARY KEY(alias, oid))""",
    # ⑧ 元信息
    "CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT)",
)

#: 域 → 扩展表名。P0 只用 agent/skill/project/service，其余先登记后建（v2 迁移补齐）。
DOMAIN_TABLE: Dict[str, str] = {
    "agent": "x_agent", "skill": "x_skill", "project": "x_project",
    "session": "x_session", "memory": "x_memory", "doc": "x_doc",
    "log": "x_log", "task": "x_task", "service": "x_service",
}

#: 分块参数（与 memindex 对齐，保证迁移后命中可比）
CHUNK_CHARS = 1200
CHUNK_OVERLAP = 150

#: CJK 统一表意区（基本区 + 扩展A + 兼容区）。用于把中文按字切分，
#: 让 unicode61 能用短语查询命中 1~2 字查询（trigram 的天生盲区）。
_CJK_RANGES = (
    (0x3400, 0x4DBF), (0x4E00, 0x9FFF), (0xF900, 0xFAFF),
    (0x20000, 0x2A6DF), (0x2A700, 0x2EBEF), (0x2F800, 0x2FA1F),
    (0x3000, 0x303F),   # CJK 标点
)


def _is_cjk(ch: str) -> bool:
    cp = ord(ch)
    return any(lo <= cp <= hi for lo, hi in _CJK_RANGES)


def cjk_split(text: str) -> str:
    """把 CJK 连续段按字拆开，英文/数字按词保留。

    `统一数据存储` → `统 一 数 据 存 储`，于是 FTS5 unicode61 的短语查询
    `"数 据"` 能命中 —— 实测确认（SQLite 3.40.1）。

    索引体积代价：unicode61 对单字建 token，约为原文 1~2×；因此
    `chunk_ftsu` **只覆盖 title/summary/uri 与短文档**（store 侧控制），
    不为全文建第二份索引。
    """
    if not text:
        return ""
    out: List[str] = []
    buf: List[str] = []

    def flush() -> None:
        if buf:
            out.append("".join(buf))
            buf.clear()

    for ch in text:
        if _is_cjk(ch):
            flush()
            out.append(ch)
        elif ch.isspace():
            flush()
        else:
            buf.append(ch)
    flush()
    return " ".join(t for t in out if t)


def apply_pragmas(con: sqlite3.Connection) -> None:
    """按顺序执行 pragma。`page_size` 必须在**空库**上设才生效，故放最前。"""
    for k, v in PRAGMAS:
        con.execute("PRAGMA %s=%s" % (k, v))


def ensure_schema(con: sqlite3.Connection) -> Dict[str, object]:
    """建库 + 幂等迁移。返回 {version, created, tables}。

    幂等判据：DDL 全 `IF NOT EXISTS`；`schema_version` 只在**大于**当前时才推进
    （防止旧代码把新库降级）。任何一步失败抛异常，由调用方决定回滚。

    ⚠ pragma 必须在事务**外**设：`synchronous` 等在事务内改会
    `Safety level may not be changed inside a transaction`。
    """
    before = _tables(con)
    apply_pragmas(con)
    con.execute("BEGIN IMMEDIATE")
    try:
        for stmt in DDL:
            con.execute(stmt)
        row = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
        cur = int(row[0]) if row else 0
        created = cur == 0
        if created or cur < SCHEMA_VERSION:
            con.execute(
                "INSERT INTO meta(k,v) VALUES('schema_version',?) "
                "ON CONFLICT(k) DO UPDATE SET v=excluded.v",
                (str(SCHEMA_VERSION),))
            con.execute(
                "INSERT INTO meta(k,v) VALUES('built_at',datetime('now')) "
                "ON CONFLICT(k) DO NOTHING")
        after = _tables(con)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"version": SCHEMA_VERSION, "created": created,
            "tables": sorted(after - before), "total_tables": len(after)}


def _tables(con: sqlite3.Connection) -> set:
    return {r[0] for r in con.execute(
        "SELECT name FROM sqlite_master WHERE type IN ('table','view')")}


def oid_of(domain: str, src_id: str, native_key: str) -> str:
    """派生对象 id：sha1(domain|src_id|native_key)[:16]。

    必须**确定**（同一实体任何时候算出来都一样）——幂等 upsert 与重扫稳定都靠它。
    """
    import hashlib
    return hashlib.sha1(
        ("%s|%s|%s" % (domain, src_id, native_key)).encode("utf-8")
    ).hexdigest()[:16]


def integrity_check(con: sqlite3.Connection,
                    allow_write: bool = False) -> Dict[str, object]:
    """结构自检（方案 §9.1 质量闸门的可测部分）。

    返回 {orphan_chunks, dangling_ext, fts_ok, fts_counts}。前两个应为 0。

    ⚠ `chunk_fts` 的 `'integrity-check'` 命令是**写操作**，只读连接上会
    `attempt to write a readonly database` ⇒ 只读连接改用**计数对账**
    （content 表行数 vs FTS 行数），语义等价且不需要写权限。
    """
    out: Dict[str, object] = {}
    out["orphan_chunks"] = con.execute(
        "SELECT COUNT(*) FROM chunk c LEFT JOIN obj o ON o.oid=c.oid "
        "WHERE o.oid IS NULL").fetchone()[0]
    dangling = 0
    for t in DOMAIN_TABLE.values():
        try:
            dangling += con.execute(
                "SELECT COUNT(*) FROM %s x LEFT JOIN obj o ON o.oid=x.oid "
                "WHERE o.oid IS NULL" % t).fetchone()[0]
        except sqlite3.Error:
            pass
    out["dangling_ext"] = dangling
    n_chunk = con.execute("SELECT COUNT(*) FROM chunk").fetchone()[0]
    n_uni = con.execute("SELECT COUNT(*) FROM chunk_uni").fetchone()[0]
    try:
        n_fts = con.execute("SELECT COUNT(*) FROM chunk_fts").fetchone()[0]
        n_ftsu = con.execute("SELECT COUNT(*) FROM chunk_ftsu").fetchone()[0]
    except sqlite3.Error as e:
        out["fts_ok"] = False
        out["fts_err"] = "%s: %s" % (type(e).__name__, e)
        return out
    out["fts_counts"] = {"chunk": n_chunk, "chunk_uni": n_uni,
                         "chunk_fts": n_fts, "chunk_ftsu": n_ftsu}
    out["fts_ok"] = (n_fts == n_chunk and n_ftsu == n_uni)
    if allow_write and not out["fts_ok"]:
        try:
            con.execute("INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check')")
            con.execute("INSERT INTO chunk_ftsu(chunk_ftsu) VALUES('integrity-check')")
        except sqlite3.Error as e:
            out["fts_err"] = "%s: %s" % (type(e).__name__, e)
    return out


def chunk_text(text: str, target: int = CHUNK_CHARS,
               overlap: int = CHUNK_OVERLAP,
               max_chunks: int = 400) -> List[str]:
    """按目标长度切块，带重叠；**空文本返回空表**（不许造空块污染索引）。

    超长文本截到 max_chunks（防御：终端录像之类不可索引的巨物）。
    """
    if target <= 0:
        raise ValueError("target 必须为正数")
    if overlap < 0 or overlap >= target:
        raise ValueError("overlap 必须满足 0 <= overlap < target")
    if max_chunks < 0:
        raise ValueError("max_chunks 不能为负数")
    if not text or not text.strip() or max_chunks == 0:
        return []
    text = text.strip()
    if len(text) <= target:
        return [text]
    out: List[str] = []
    step = max(1, target - overlap)
    pos = 0
    while pos < len(text) and len(out) < max_chunks:
        out.append(text[pos:pos + target])
        pos += step
    return out
