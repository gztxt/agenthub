"""SQLite 数据层（借鉴 Agent_Manager telemetry_store 的账本先行设计）

表：
- telemetry_events  Hook 遥测事件账本（append-only，(source,session_id,event) 覆盖式 upsert）
- memories          L1 可检索记忆（分类 fact/decision/constraint/preference）
- memory_docs       L2 近30天工作记忆 / L3 长期 Profile（整文档存储，用户可手写补充）
- chat_sessions     统一对话会话
- chat_messages     统一对话消息
- custom_agents     动态注册的自定义 Agent
- manager_messages  Manager Agent 指挥官会话（含工具步骤）
- asset_audit       资产变更审计（append-only：谁改了哪个资产；detail 落库前脱敏）
"""
import json
import sqlite3
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Optional

_lock = threading.Lock()
_conn: Optional[sqlite3.Connection] = None

SCHEMA = """
CREATE TABLE IF NOT EXISTS telemetry_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,            -- codex|claude|qoder|workbuddy|...
    session_id TEXT NOT NULL,
    event TEXT NOT NULL,             -- session_usage|hook|...
    cwd TEXT,
    payload TEXT NOT NULL,           -- 原始 JSON
    usage_scope TEXT,
    created_at TEXT NOT NULL,
    UNIQUE(source, session_id, event)
);
CREATE TABLE IF NOT EXISTS memories (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    layer TEXT NOT NULL DEFAULT 'L1',
    category TEXT NOT NULL DEFAULT 'fact',  -- fact|decision|constraint|preference
    content TEXT NOT NULL,
    source TEXT,                     -- 来源 agent / manual
    session_id TEXT,
    status TEXT NOT NULL DEFAULT 'active',  -- active|superseded|deleted
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS memory_docs (
    layer TEXT PRIMARY KEY,          -- L2|L3
    content TEXT NOT NULL,
    manual TEXT NOT NULL DEFAULT '', -- 用户手写补充（独立保存，清洗不触碰）
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_sessions (
    id TEXT PRIMARY KEY,
    agent_id TEXT NOT NULL,
    title TEXT,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS chat_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,              -- user|assistant|tool|error
    content TEXT NOT NULL,
    meta TEXT,
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS custom_agents (
    id TEXT PRIMARY KEY,
    name TEXT NOT NULL,
    type TEXT NOT NULL DEFAULT 'custom',
    command TEXT,
    args TEXT,                       -- JSON array
    working_dir TEXT,
    env TEXT,                        -- JSON object
    port INTEGER,
    endpoint TEXT,
    description TEXT DEFAULT '',
    source TEXT DEFAULT 'manual',    -- manual|scan:docker|scan:systemd
    auto_restart INTEGER DEFAULT 0,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS manager_messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    role TEXT NOT NULL,              -- user|assistant
    content TEXT NOT NULL,
    steps TEXT,                      -- JSON: thought/toolcall/toolresult/answer
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_msg_session ON chat_messages(session_id);
CREATE INDEX IF NOT EXISTS idx_mem_status ON memories(status);
CREATE INDEX IF NOT EXISTS idx_mgr_session ON manager_messages(session_id);
CREATE TABLE IF NOT EXISTS profile_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    source TEXT NOT NULL,        -- hub_chat|manager_tool|task_exec|cron_run
    subject TEXT NOT NULL,       -- agent_id / tool / run:task
    trace_id TEXT,
    status TEXT NOT NULL,        -- success|fail
    duration_ms INTEGER,
    detail TEXT,
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_prof_subject ON profile_events(subject);
-- v0.13.27 runlog：/api/runlog 按 source 过滤 + id 游标分页（id<? 而非 OFFSET——
-- append-only 表 OFFSET 翻页越翻越慢，游标恒定代价）。幂等建索引，存量库启动自动补。
CREATE INDEX IF NOT EXISTS idx_prof_source ON profile_events(source, id);
CREATE TABLE IF NOT EXISTS asset_audit (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    asset_type TEXT NOT NULL,   -- mcp_server|mcp_acl|memory_l1|memory_doc|agent|session|setting
    asset_slug TEXT NOT NULL,   -- server_id / acl_id / mid / L2|L3|batch / agent_id / session_id
    action TEXT NOT NULL,       -- create|update|delete|bind|unbind|rebuild（见 AUDIT_ACTIONS）
    actor TEXT NOT NULL,        -- user:term-token|user:hub-passcode|user:exempt|agent:<slug>|system
    detail TEXT DEFAULT '{}',   -- JSON；落库前过脱敏，**绝不许含凭据原文**
    created_at TEXT NOT NULL,
    -- ── P0-4（v0.13.105）研判三列 ──────────────────────────────────────
    -- 语义**严格限定**为「本事件记载的变更，在 hub 本地是否确实发生了」：
    -- 每个调用点都在变更落库**之后**才调 log_asset_event ⇒ 「这行存在」本身就是
    -- 「变更执行了」的证据。它**不**声明上游（TDAI / 远端 GitHub）效果 ——
    -- 那需要写后核实；核不了就显式传 insufficient，不许默认成 confirmed。
    -- NULL = **未研判**（迁移前的旧行），与 insufficient（判过了、判不了）**分开计数**，
    -- 但两者都让 decisive=False。
    verdict TEXT,               -- confirmed|dismissed|insufficient，NULL=未研判
    severity TEXT,              -- info|warn|high，NULL=未定级
    confidence REAL,            -- 0..1；不可判定时**不给假刻度**，留 NULL
    -- ── P2 A11（v0.13.120）Trace 因果链 ────────────────────────────────
    -- 让审计事件能沿**同一个 trace_id** 与 `profile_events`/`tasks` 排进一条链
    -- （手册 A11：模型/状态/Action/Artifact/Verifier/Outcome 同一条 trace）。
    -- **NULL 是诚实值**：调用点没带 trace 时留空 ⇒ `traceview` 计入 `orphans` 如实报断链，
    -- **绝不**按时间就近归并（那是凭空造因果）。
    trace_id TEXT
);
CREATE INDEX IF NOT EXISTS idx_audit_asset ON asset_audit(asset_type, asset_slug);
CREATE INDEX IF NOT EXISTS idx_audit_created ON asset_audit(created_at);
-- ⚠ v0.13.120 P2 A11：`trace_id` 的索引**不写在这里** —— 它在 SCHEMA 之后由
-- `_add_column_if_missing` 补列，而 `executescript(SCHEMA)` 在存量库上先跑：
-- 若在此处 `CREATE INDEX ... (trace_id)`，**列还不存在**⇒ 整段迁移抛
-- `OperationalError: no such column`，服务启动即挂。索引一律建在 ALTER 之后（见 init_db）。
-- v0.13.36 应用级偏好 KV：两项目页收藏/隐藏状态落服务端（此前只在浏览器
-- localStorage，换浏览器/端侧即失效）。键白名单在 prefs.py，不是自由 KV。
CREATE TABLE IF NOT EXISTS app_prefs (
    key TEXT PRIMARY KEY,
    value TEXT NOT NULL,             -- JSON（形状由 prefs.PrefValue 校验）
    updated_at TEXT NOT NULL
);
-- v0.13.59 终端会话录制（PT-20260927-16）：pty I/O 落盘 + 回放。
-- 独立表（不混 chat_messages），**不进备份链**（运行时库在 agenthub/data 与 /vol1）。
-- data 列是**已脱敏**字节 —— redact_text 在 term_record 里、落盘前跑，库里不会有凭据原文。
-- 单会话/全局双上限与「触顶即停录并可查」在 term_record.py：静默丢帧会让用户事后
-- 分不清「没发生」和「录不下」，所以 capped 是一等状态，经 /api/term/recording/{sid} 报出。
CREATE TABLE IF NOT EXISTS term_recordings (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT NOT NULL,
    agent_id TEXT NOT NULL,
    seq INTEGER NOT NULL,        -- 单调序号 = 回放顺序
    ts REAL NOT NULL,            -- epoch
    direction TEXT NOT NULL,     -- out(pty→客户端) | in(客户端→pty)
    data BLOB NOT NULL,          -- 已脱敏字节
    bytes INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_termrec_sid ON term_recordings(session_id, seq);
-- v0.13.101：给 ts 补索引。保留期清理 `DELETE ... WHERE ts < ?` 原来走
-- SCAN term_recordings（无 ts 索引可用），实测生产库 48万行/160MB 时
-- 单次 77~90ms，且它跑在会话收尾路径上（_cleanup → recorder.close → sweep，
-- 同步调用）⇒ 每关一个会话就独占事件循环近百毫秒。加索引后 EXPLAIN 从
-- SCAN 变 SEARCH USING INDEX idx_termrec_ts，同一查询实测 57.5ms → 0.08ms。
-- 既存库由 init_db 的 executescript(SCHEMA)幂等补建，不需要迁移脚本。
CREATE INDEX IF NOT EXISTS idx_termrec_ts ON term_recordings(ts);
-- v0.13.101：录制预算的**计数表**（单行）+ 两个触发器。彻底消除
-- `SELECT SUM(bytes) FROM term_recordings`——那条查询无索引可用，EXPLAIN 恒为
-- SCAN term_recordings，实测生产库 48 万行 / 160MB 时单次 108~434ms（冷热差异
-- 来自 page cache），而它被同步调在 asyncio 事件循环里 ⇒ 每次建会话
-- （Recorder.__init__ 先 DELETE 同sid 再算预算）都独占事件循环上百毫秒。
--
-- 为什么用触发器而不是「应用层增量维护」：应用层维护必须保证
-- 「每一次 INSERT/DELETE 都恰好更新一次计数」，漏一处就永久偏差，而漏掉的
-- 那一处**不报错**（预算判据只会慢慢失真）。触发器在**数据库侧**做这件事，
-- 写入与计数更新在同一个事务里，不可能各走各的。
-- 实测代价（48 万行库上量）：INSERT+commit p50 0.043ms → 0.063ms，
-- 每帧多 0.02ms，相对 pty 读循环里 5ms 的合并窗口可忽略。
--
-- `CHECK(v >= 0)` 是**故意的**：一旦出现「减得比加得多」就当场报错，
-- 而计数器漂成负数会让预算判据永久失效（每次都以为还有空间）。
-- 宁可炸也不要静默说假话。
CREATE TABLE IF NOT EXISTS term_rec_meta (
    k TEXT PRIMARY KEY,
    v INTEGER NOT NULL
);
CREATE TRIGGER IF NOT EXISTS trg_termrec_ins AFTER INSERT ON term_recordings BEGIN
    INSERT INTO term_rec_meta(k, v) VALUES('total_bytes', NEW.bytes)
    ON CONFLICT(k) DO UPDATE SET v = v + NEW.bytes;
END;
CREATE TRIGGER IF NOT EXISTS trg_termrec_del AFTER DELETE ON term_recordings BEGIN
    UPDATE term_rec_meta SET v = v - OLD.bytes WHERE k = 'total_bytes';
END;
"""


def _add_column_if_missing(conn: sqlite3.Connection, table: str, col: str, decl: str) -> None:
    """上游 telemetry_store.rs 的幂等迁移模式"""
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
    if col not in cols:
        conn.execute(f"ALTER TABLE {table} ADD COLUMN {col} {decl}")


def init_db(path: Path) -> None:
    global _conn
    path.parent.mkdir(parents=True, exist_ok=True)
    _conn = sqlite3.connect(str(path), check_same_thread=False)
    _conn.row_factory = sqlite3.Row
    _conn.execute("PRAGMA journal_mode=WAL")
    # ── T05-1（v0.13.16）：把 PRAGMA 从「靠默认值」改成「写死在这一行」───────
    # 0924 方案 v2 提的四个 PRAGMA，**实测后只采纳三个**，因为其中一个本来就有：
    #   busy_timeout：新连接实测已经回 5000 —— 不是本文件设的，是 Python 3.11
    #     `sqlite3.connect(timeout=5.0)` 的默认值。所以“撞锁就 BUSY”的担心不成立；
    #     但把它从「标准库默认」提升为**本仓写死**仍是净收益：换 Python 版本或改
    #     connect() 参数时不会被静默抹掉。
    #   foreign_keys：实测 0（OFF）。SCHEMA 实测 **0 处 REFERENCES/FOREIGN KEY**，
    #     所以打开它今天不会打断任何写入；不打开的代价是「将来加外键时静默不生效」。
    #   synchronous：实测 2=FULL。WAL 下每次 commit 等 fsync，而本机是 NAS；
    #     本库装的是 telemetry/画像/聊天记录（非不可丢数据），NORMAL 是 WAL 的推荐搭配。
    #     ⚠ 这是耐久语义的取舍：断电时可能丢最后几个已提交事务（库不会坏）。
    _conn.execute("PRAGMA busy_timeout=5000")
    _conn.execute("PRAGMA foreign_keys=ON")
    _conn.execute("PRAGMA synchronous=NORMAL")
    # WAL 实测长期不回落（生产主库 1.1MB 而 -wal 4.0MB），原因是**没有任何 checkpoint 触发点**。
    # 只在启动时收一次：运行期做 TRUNCATE 会与写者抢锁，而 hub 有 4 个后台循环在写。
    # 失败绝不打断启动 —— checkpoint 是优化，不是正确性前提。
    try:
        _conn.execute("PRAGMA wal_checkpoint(TRUNCATE)")
    except sqlite3.OperationalError as e:  # noqa: BLE001
        print(f"[db] wal_checkpoint 跳过（不影响启动）：{type(e).__name__}: {e}", flush=True)
    with _lock:
        _conn.executescript(SCHEMA)
        # S2 画像/追踪列（存量库自动迁移）
        _add_column_if_missing(_conn, "telemetry_events", "trace_id", "TEXT")
        _add_column_if_missing(_conn, "telemetry_events", "duration_ms", "INTEGER")
        _add_column_if_missing(_conn, "telemetry_events", "status", "TEXT")
        _add_column_if_missing(_conn, "custom_agents", "source", "TEXT DEFAULT 'manual'")
        # P0-4（v0.13.105）：asset_audit 研判三列。存量库交给 ALTER 幂等补，
        # **不动任何既有行**（旧行的 verdict 保持 NULL = 未研判，这是诚实的，
        # 比回填一个 confirmed 强 —— 回填等于替历史编证据）。
        _add_column_if_missing(_conn, "asset_audit", "verdict", "TEXT")
        _add_column_if_missing(_conn, "asset_audit", "severity", "TEXT")
        _add_column_if_missing(_conn, "asset_audit", "confidence", "REAL")
        # P2 A11（v0.13.120）：asset_audit 补 trace_id。存量库交给 ALTER 幂等补，
        # **旧行的 trace_id 保持 NULL** —— 那是「这条旧审计发生过，但当时没记 trace」的
        # 诚实表达；回填成某个值＝替历史编因果。traceview 把 NULL 计 orphans。
        _add_column_if_missing(_conn, "asset_audit", "trace_id", "TEXT")
        _conn.execute("CREATE INDEX IF NOT EXISTS idx_audit_trace ON asset_audit(trace_id)")
        _conn.commit()


def is_open() -> bool:
    """当前是否已有连接。**09-24 新增**：`mcpgw.mcp_call` 以前每次都 `init_db()`，
    而 `init_db` 会把全局连接**重指**到传入路径——生产上看不出来（路径相同），
    但在任何进进程测试里，一发 `/mcp/call` 就会把测试的 tmp 库悄悄换成生产库，
    后续写入全落在真库上（本机 09-24 实际就这么污染过一次 mcp_servers/mcp_acl）。
    另一个代价：P0 往 `init_db` 里加了 `wal_checkpoint(TRUNCATE)`，“只在启动时收一次”；
    按次重进等于每次工具调用抢一次 checkpoint，与本仓自己的注释直接相远。"""
    return _conn is not None


def current_path() -> str:
    """当前连接指向的库文件（给测试做“库指向金丝雀”断言用，拿不到则空串）。"""
    if _conn is None:
        return ""
    try:
        return str(_conn.execute("PRAGMA database_list").fetchone()[2])
    except Exception:  # noqa: BLE001
        return ""


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def query(sql: str, params: tuple = ()) -> list[dict]:
    with _lock:
        cur = _conn.execute(sql, params)
        return [dict(r) for r in cur.fetchall()]


def execute(sql: str, params: tuple = ()) -> int:
    with _lock:
        cur = _conn.execute(sql, params)
        _conn.commit()
        return cur.rowcount


def checkpoint(mode: str = "PASSIVE") -> None:
    """跑一次 WAL 检查点（2026-10-11）。

    调用方是 main.wal_checkpoint_loop（后台定时、走 to_thread），**不是**逐帧写路径：
    SQLite 的自动检查点每积满 wal_autocheckpoint(默认 1000) 页就在「触发它的那条写
    语句」里同步 checkpoint，而 term_record 的每个按键/每帧输出都是一条 INSERT+commit
    ⇒ 实测每 183 帧一次 ~90ms 尖峰，全落在 asyncio 事件循环里（p99 6.9ms → 关自动后
    0.17ms）。所以 hub 主连接关掉自动检查点，改由本函数定期做。

    mode 默认 PASSIVE：**不阻塞**读/写者，能在写者空闲的间隙把 WAL 回写主库；
    它可能回写不满（有人正写着），但下一轮会继续 —— 比 TRUNCATE 更安全（TRUNCATE
    要独占，正是 09-2x 注释里「运行期做 TRUNCATE 会与写者抢锁」担心的那件事）。
    与 db.init_db 的启动期 wal_checkpoint(TRUNCATE) 分工：启动做一次彻底收口，
    运行期用 PASSIVE 维持。
    """
    with _lock:
        _conn.execute(f"PRAGMA wal_checkpoint({mode})")


def execute_script(sql: str) -> None:
    """executescript 会先隐式 commit 当前事务再执行脚本，末尾不 commit 时
    后续连接看不到已执行语句的效果（读不到 / 关进程丢失）⇒ 显式补 commit。"""
    with _lock:
        _conn.executescript(sql)
        _conn.commit()


def upsert_telemetry(source: str, session_id: str, event: str,
                     cwd: Optional[str], payload: dict, usage_scope: Optional[str],
                     trace_id: Optional[str] = None, duration_ms: Optional[int] = None,
                     status: Optional[str] = None) -> None:
    """同一 (source,session_id,event) 重报覆盖而非叠加（Agent_Manager 口径）"""
    execute(
        """INSERT INTO telemetry_events(source,session_id,event,cwd,payload,usage_scope,
             trace_id,duration_ms,status,created_at)
           VALUES(?,?,?,?,?,?,?,?,?,?)
           ON CONFLICT(source,session_id,event) DO UPDATE SET
             cwd=excluded.cwd, payload=excluded.payload,
             usage_scope=excluded.usage_scope, trace_id=excluded.trace_id,
             duration_ms=excluded.duration_ms, status=excluded.status,
             created_at=excluded.created_at""",
        (source, session_id, event, cwd, json.dumps(payload, ensure_ascii=False),
         usage_scope, trace_id, duration_ms, status, _now()))


def log_profile_event(source: str, subject: str, status: str,
                      duration_ms: Optional[int], trace_id: Optional[str] = None,
                      detail: Optional[dict] = None) -> None:
    """S2 画像事件（append-only，统计成功率/耗时的口径源）"""
    execute(
        "INSERT INTO profile_events(source,subject,trace_id,status,duration_ms,detail,created_at)"
        " VALUES(?,?,?,?,?,?,?)",
        (source, subject, trace_id, status, duration_ms,
         json.dumps(detail, ensure_ascii=False) if detail else None, _now()))


#: 审计动作枚举——写死在这里，避免各调用点自由发挥（自由文本 = 查不动）
AUDIT_ACTIONS = ("create", "update", "delete", "bind", "unbind", "rebuild")


def log_asset_event(asset_type: str, asset_slug: str, action: str, actor: str,
                    detail: Optional[dict] = None, verdict: Optional[str] = None,
                    severity: Optional[str] = None,
                    confidence: Optional[float] = None,
                    trace_id: Optional[str] = None) -> None:
    """资产变更审计（**append-only**）。与 log_profile_event 同族但口径不同：
    profile_events 记「调用成不成、多快」，asset_audit 记「哪个资产被谁改了」。

    三条硬约束（都有 L0 闸门钉着，见 tests/test_asset_audit.py）：
      1) **只 INSERT**：审计表一旦可被 UPDATE/DELETE 就不再是审计。
      2) **detail 落库前整体过脱敏**：审计行会成为下一次会话导出的正文，凭据写进去＝二次外流。
         做法是先 json.dumps 再对整串 redact_text ⇒ 嵌套层也覆盖（只扫顶层值会漏 dict 里的 dict）。
      3) **action 不在枚举里 ⇒ 打 action_invalid 标记**，绝不静默丢弃也绝不改写。

    ── P0-4（v0.13.105）第四问：这次变更「证实了吗」──
    `verdict` 的语义**严格限定为 hub 本地效果**：本函数在每个调用点都在变更落库**之后**
    调用（mcpgw 的 INSERT、memory.add_memory、githubprojects 的克隆后 origin 校验均在前），
    所以「审计行存在」本身就是「变更在 hub 侧执行了」的证据 —— 默认 `confirmed` 不是乐观，
    是**这条事实本身**。
    ⚠ 它**不**声明上游效果（TDAI 权威库 / 远端 GitHub）：那需要写后核实，而本仓已知
    `tdai_client` 对 TDAI 的写调用数为 0 ⇒ 对权威库的效果就是 `insufficient`。
    做不到核实的调用点请**显式传** `insufficient`；**不许**用默认值把「不知道」说成「已确认」。
    `verdict` 不在枚举里 ⇒ 原样落库 + detail 打 `verdict_invalid`（与 action 同纪律：
    不静默丢弃、不改写，丢/改比记错更贵）。

    ── P2 A11（v0.13.120）第五参：`trace_id` ──
    带上它，这条审计就能与 `profile_events`/`tasks` 排进**同一条 trace**
    （`traceview.collect()`）。默认 `None` ⇒ 落 NULL ⇒ 计入 `orphans`（**断链如实报**）。
    调用点**能拿得到就传**（`request.headers['x-trace-id']` 或当前任务的 trace），
    拿不到就留空 —— 留空是诚实，编号是造假。
    """
    d = dict(detail or {})
    if action not in AUDIT_ACTIONS:
        d = {**d, "action_invalid": True}
    # 局部 import：db 是最底层，只依赖叶子模块 evidence（它不 import 任何 src 模块）
    from evidence import SEVERITIES, VERDICTS
    if verdict is not None and verdict not in VERDICTS:
        d = {**d, "verdict_invalid": True}
    if severity is not None and severity not in SEVERITIES:
        d = {**d, "severity_invalid": True}
    body = json.dumps(d, ensure_ascii=False)
    try:
        from sessions_export import redact_text   # 局部 import：db 是最底层，不许成环
        body = redact_text(body)[0]
    except Exception:                             # noqa: BLE001
        pass                                      # 脱敏器不可用时仍要留下事件（完整性 > 打码）
    _conf = None if confidence is None else float(confidence)
    execute(
        "INSERT INTO asset_audit(asset_type,asset_slug,action,actor,detail,created_at,"
        "verdict,severity,confidence,trace_id) VALUES(?,?,?,?,?,?,?,?,?,?)",
        (asset_type, str(asset_slug), action, actor, body, _now(),
         verdict if verdict is not None else "confirmed", severity, _conf,
         (str(trace_id) if trace_id not in (None, "") else None)))


def add_memory(content: str, category: str = "fact", source: str = "manual",
               session_id: Optional[str] = None, layer: str = "L1") -> int:
    now = _now()
    with _lock:
        cur = _conn.execute(
            "INSERT INTO memories(layer,category,content,source,session_id,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?)",
            (layer, category, content, source, session_id, now, now))
        _conn.commit()
        return cur.lastrowid
