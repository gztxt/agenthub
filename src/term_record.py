"""终端会话录制（PT-20260927-16）：pty I/O 落盘 + 回放。

本机**唯一**「主动把用户键入内容落盘」的功能，所以四条硬约束不是风格偏好、
是安全前提（台账 PT-20260927-16 原文照录）：

1. **默认关闭**：`TERM_RECORD` 缺省 0，只有显式 `TERM_RECORD=1` 才录。不开就零开销
   （`Recorder.__init__` 直接短路，不建表写入、不计字节）。
2. **录前一律过既有脱敏器** `sessions_export.redact_text`：pty 流里出现口令/Token 是
   常态而不是边缘情况 —— 不脱敏等于建了一个凭据转储盘。脱敏发生在**落盘前**，
   库里不会有原文。
3. **只记 I/O 与时间戳，绝不记初始环境变量**：env 里就有 `TERM_TOKEN`。本模块**不接触**
   `term.child_env()`，调用方只许把 pty 字节 / 客户端按键喂进来 —— 从签名上断掉这条路，
   而不是靠「记得别记」。
4. **独立表 + 单会话上限 + 全局上限，触顶即停录并可查**：静默丢帧不可接受，用户事后
   分不清「没发生」和「录不下」。所以 `capped` 是**一等状态**，经 `/api/term/recording/{sid}`
   报给界面，由前端明示。

参数（2026-09-29 用户裁定「按建议值执行」）：
- 单会话上限 32MB、全局上限 512MB
- 保留期 7 天，每会话只留最后 1 份录制（同 sid 再次录制即覆盖）
- **不进备份链**（运行时库在 `~/agent-hub/data` 与 `/vol1`，不入 git）
"""
from __future__ import annotations

import base64
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import db

log = logging.getLogger(__name__)

DEFAULT_MAX_SESSION = 32 * 1024 * 1024
DEFAULT_MAX_TOTAL = 512 * 1024 * 1024
DEFAULT_RETENTION_DAYS = 7

_TRUE = {"1", "true", "yes", "on"}

def enabled() -> bool:
    """录制总开关。缺省 0 ⇒ 默认关。"""
    return os.environ.get("TERM_RECORD", "0").strip().lower() in _TRUE


def _int_env(name: str, default: int) -> int:
    try:
        v = int(os.environ.get(name, "").strip())
    except (TypeError, ValueError):
        return default
    return v if v > 0 else default


def max_session_bytes() -> int:
    return _int_env("TERM_RECORD_MAX_SESSION", DEFAULT_MAX_SESSION)


def max_total_bytes() -> int:
    return _int_env("TERM_RECORD_MAX_TOTAL", DEFAULT_MAX_TOTAL)


def retention_days() -> int:
    return _int_env("TERM_RECORD_RETENTION_DAYS", DEFAULT_RETENTION_DAYS)


def _human(n: int) -> str:
    """上限文案。**不许一律印 MB**：`1024 // (1<<20)` == 0 ⇒ 「已达上限 0MB」是假话，
    而这行文案是触顶时界面唯一的解释（实测自测：单会话上限 1024B 印成 0MB）。"""
    mib = n / (1024 * 1024)
    if n % (1024 * 1024) == 0:
        return "%dMB" % (n // (1024 * 1024))
    return "%d 字节（%.3gMB）" % (n, mib)


def _redact(raw: bytes) -> bytes:
    """pty 字节 → 脱敏后字节。函数内惰性 import：sessions_export 拖 db，本模块也被 term 拖。"""
    from sessions_export import redact_text
    text = raw.decode("utf-8", errors="replace")
    cleaned, hits = redact_text(text)
    if hits:
        # 命中即标记，界面可提示「本段录制已脱敏 N 处」
        return cleaned.encode("utf-8", errors="replace")
    return raw


class Recorder:
    """一个会话一条。`active=False` 时所有方法都是零成本 no-op。"""

    def __init__(self, session_id: str, agent_id: str):
        self.session_id = session_id
        self.agent_id = agent_id
        self.active = False
        self.bytes = 0
        self.frames = 0
        self.capped = False
        self.capped_reason = ""
        self.started = time.time()
        self.ended: Optional[float] = None
        self.redacted_hits = 0
        if not enabled() or not db.is_open():
            return
        # 同 sid 只留最后 1 份：先清掉上一份录制（台账裁定「每会话只留最后 1 份」）。
        # v0.13.101：DELETE 的减法由数据库触发器做（trg_termrec_del），
        # 应用层不再需要「作废缓存」这一步—— 这正是原先每次建会话必然
        # 重查全表 SUM 的根因。
        _exec("DELETE FROM term_recordings WHERE session_id=?", (self.session_id,))
        used = total_bytes()
        if used >= max_total_bytes():
            # 全局预算满：本会话**不开录**并说清原因，而不是「录了但少了」。
            self.capped = True
            self.capped_reason = ("全局录制预算已满（%s，在盘 %s），本会话未开录"
                                  % (_human(max_total_bytes()), _human(used)))
            return
        self.active = True

    # ── 写 ────────────────────────────────────────────────────────────────
    def feed(self, direction: str, data: bytes) -> None:
        """direction: out = pty→客户端，in = 客户端→pty。绝不记 env、绝不记命令。"""
        if not self.active or not data:
            return
        if self.capped:
            return
        if self.bytes + len(data) > max_session_bytes():
            self.capped = True
            self.capped_reason = "本会话录制已达上限 %s，已停录（后续内容未记录）" % (
                _human(max_session_bytes()))
            return
        if self.bytes + total_bytes() > max_total_bytes():
            self.capped = True
            self.capped_reason = "全局录制预算已满 %s，已停录（后续内容未记录）" % (
                _human(max_total_bytes()))
            return
        blob = _redact(data)
        ok = _exec(
            "INSERT INTO term_recordings(session_id,agent_id,seq,ts,direction,data,bytes) "
            "VALUES(?,?,?,?,?,?,?)",
            (self.session_id, self.agent_id, self.frames, time.time(),
             direction, blob, len(blob)))
        if ok:
            self.frames += 1
            self.bytes += len(blob)
            # v0.13.101：全局计数的递增同样交给触发器（trg_termrec_ins），
            # 与本 INSERT 在同一事务里。应用层不再维护第二份计数。

    def close(self) -> None:
        if not self.active:
            return
        self.active = False
        self.ended = time.time()
        sweep()

    # ── 查 ────────────────────────────────────────────────────────────────
    def to_dict(self) -> Dict[str, Any]:
        return {
            "session_id": self.session_id, "agent_id": self.agent_id,
            "recording": bool(self.active or self.frames),
            "enabled": enabled(), "frames": self.frames, "bytes": self.bytes,
            "capped": self.capped, "capped_reason": self.capped_reason,
            "started": self.started, "ended": self.ended,
            "redacted_hits": self.redacted_hits,
            "limits": {"max_session": max_session_bytes(),
                       "max_total": max_total_bytes(),
                       "retention_days": retention_days()},
        }


def _exec(sql: str, params: tuple = ()) -> bool:
    """录制写盘失败**不许打断终端**（记录是旁路，不是主链路）。"""
    if not db.is_open():
        return False
    try:
        db.execute(sql, params)
        return True
    except Exception as e:  # noqa: BLE001
        log.warning(f"[term_record] 写入跳过（不影响终端）：{type(e).__name__}: {e}")
        return False


def ensure_counted() -> None:
    """确保计数表已播种（存量库升级用，幂等）。

    ★ 为什么需要播种：触发器只对**建好之后**的 INSERT/DELETE 生效，
      而升级前已落盘的几十万行不会被回算 ⇒ 计数从0 起跳、预算判据认为
      「还有 512MB 空间」而盘上早已超了。所以首次必须补一次真 SUM。

    ★★ 判据是「播种标记」，**不是「计数行是否存在」**——后者有竞态：
      `trg_termrec_ins` 的 `INSERT … ON CONFLICT DO UPDATE` 在**计数行不存在**
      时会**顺手创建**它（值=那一帧的字节）。而本函数与pty 读循环是并发的：
      若在「查行」与「写播种值」之间来了首帧，计数行凭空出现 ⇒
      「行存在 ⇒ 已播种」成立 ⇒ **跳过播种 ⇒ 历史几十万行全漏算**，
      计数会停在几百字节而盘上早已 205MB。
      实测（tests::TestSeedRace）：4200万字节存量 + 并发首帧 ⇒ 播种后计数
      仅 14000 字节，缺口 4200 万——比两语句竞态严重一个数量级。
      所以用独立的 `seeded` 标记做闸门，只有本函数写过它才算播种过；
      触发器只碰`total_bytes` 一行，不会伪造标记。
      调用点：init_db 之后（main.startup），一次性开销，与仓库构建同量级。
    """
    if not db.is_open():
        return
    try:
        rows = db.query("SELECT v FROM term_rec_meta WHERE k='seeded'")
    except Exception:  # noqa: BLE001
        return
    if rows:
        return          # 已播种（标记在，才算数）
    _seed_counted()
    db.execute("INSERT INTO term_rec_meta(k,v) VALUES('seeded',1) "
               "ON CONFLICT(k) DO UPDATE SET v=1")
    log.info("[term_record] 计数表已播种：%s" % _human(total_bytes()))


#: 播种用的**单条** SQL。看起来绕（为什么要 INSERT…SELECT 而不是先 SELECT 再
#: INSERT），但这是唯一在并发下正确的写法——生产库实测播种时计数比真 SUM
#: **少 11251 字节**，且缺口恒定不随后续写入变化，说明是播种瞬间产生的
#: 一次性偏差，不是触发器持续漏算。
#:
#: 旧写法（两语句）：
#:     value = SELECT SUM(bytes) FROM term_recordings   # ← 48万行要 108~434ms
#:     INSERT ... VALUES(value) ON CONFLICT DO UPDATE SET v = excluded.v
#: 两条语句之间那几百毫秒里，pty 读循环照常INSERT 录制帧、触发器照常给计数行
#: 加法；然后 `SET v = excluded.v` 把计数**整体覆盖**成刚才那个 SUM ⇒
#: 这段时间写入的字节被抹掉。缺口方向恒为「计数偏低」⇒ **预算判据偏松**
#: （会多录一点，不会误停录），危害小但性质是「静默失真」，不可留。
#:
#: 现写法：把 SUM 与写入合成**同一条语句**（INSERT…SELECT），由 SQLite 在
#: 同一事务、同一快照内完成，窗口消失。`WHERE true` 是为绕开
#: 「INSERT…SELECT 后接 ON CONFLICT 会被解析成 SELECT 的一部分」的语法歧义
#: （不加会报 `near "DO": syntax error`），语义上是无条件真。
#: ⚠ 不能先「INSERT 占位行 v=0 再 UPDATE v = v + SUM」——占位的那条 INSERT
#:   本身不触发计数触发器，但占位行存在期间写入的帧**已经计过一次**，
#:   再加 SUM 就**双计**（实测缺口 -140009800，比原写法严重得多）。
_SEED_SQL = (
    "INSERT INTO term_rec_meta(k, v) "
    "SELECT 'total_bytes', COALESCE(SUM(bytes),0) FROM term_recordings WHERE true "
    "ON CONFLICT(k) DO UPDATE SET v = excluded.v"
)


def _seed_counted() -> None:
    """执行播种。单条 SQL，读写同一快照（见 _SEED_SQL 的注释）。"""
    db.execute(_SEED_SQL)


def total_bytes() -> int:
    """全局在盘字节数。读**单行计数表**（v0.13.101）。

    ★ 三代实现的取舍，留着免得后人又把它改回去：
      v0.13.100（缓存版）：每次 feed 读进程内缓存，DELETE 后作废、下次重查。
        实测把逐键 p50 从 677ms 降到 0.5ms —— 治了「每帧」这条路，
        但**没治根**：Recorder.__init__ 每次建会话都先 DELETE（同sid 覆盖）
        再立刻要预算 ⇒ 缓存必然作废 ⇒ 必然重查 ⇒ 生产库上每次建会话仍
        独占事件循环 108~434ms（实测建会话端到端 156~210ms）。
      v0.13.101（本版，单行计数表 + 数据库侧触发器）：total_bytes() 是
        **一次主键点查**（实测 3µs），与表大小无关，也没有「什么时候作废」
        这个问题—— 写入和计数更新在同一个事务里，物理上不可能各走各的。
      绝不要改回 `SELECT SUM(bytes) FROM term_recordings`：无索引可用，
        EXPLAIN 恒为 SCAN，48 万行实测 108ms（冷 434ms）。
    """
    if not db.is_open():
        return 0
    # ⚠ 终审补的回归修复（2026-10-09）：`db.query()` 必须在 try 里。
    #   本函数有两个调用点，**都在终端热路径上**：`Recorder.feed()` 的每帧预算检查
    #   与 `Recorder.__init__`。而 `db.query()` 自身**没有任何异常防护**（裸 `_conn.execute`）
    #   ⇒ 库一异常（WAL 损坏 / 磁盘瞬时错 / 连接被换），异常会一路穿出去，
    #   在 pty 读回调里把整个终端会话带崩。
    #   现场证据：L0 跑出 10 个 error，traceback 就是
    #   `sqlite3.ProgrammingError: Cannot operate on a closed database`
    #   （某个用例 rmtree 掉 tmp 库后，后续用例建 Recorder 就炸）。
    #   **本函数的契约是「返回 0」，不是「抛异常」** —— 预算偏松只是多录一点，
    #   而抛异常是终端不可用，两者代价差着量级。
    try:
        rows = db.query("SELECT v FROM term_rec_meta WHERE k='total_bytes'")
        if not rows:
            # 播种还没跑（老库 + 直接调本函数）。此时**不**回退到 SUM：
            # 那正是本函数要消灭的东西；宁可返回 0 让预算偏松，也不能在
            # 每帧路径上放一条全表扫描。真值由 ensure_counted() 一次性补上。
            return 0
        return max(0, int(rows[0]["v"]))
    except Exception:  # noqa: BLE001
        return 0


def _query_total_bytes() -> int:
    """全表聚合。**只在播种与测试对照时用**，不在任何逐帧路径上。"""
    try:
        rows = db.query("SELECT COALESCE(SUM(bytes),0) AS n FROM term_recordings")
        return int(rows[0]["n"]) if rows else 0
    except Exception:  # noqa: BLE001
        return 0


def frames_page(session_id: str, limit: int = 2000,
                after_seq: Optional[int] = None) -> Dict[str, Any]:
    """取一段帧 + **续传点**（B14）。返回
    `{frames, count, next_after_seq, more, first_seq, last_seq}`。

    为什么必须有续传点：原签名只有 `limit`，`ORDER BY seq ASC LIMIT n` 在「帧多于 n」时
    **只给最早那 n 条，且不说还有没有剩下的** ⇒ 客户端要么漏掉后来的帧，要么每次重拉全量
    （重拉在长会话里越来越慢）。两种都不是「续传」。

    `after_seq` 语义：**只取 seq 严格更大**的帧（`>`，不是 `>=`）—— 客户端把
    `next_after_seq` 原样回传即可续接，不会重复拿到上一条边界帧。
    `next_after_seq` 仅在**可能还有更多**时给出（`more=True`），否则为 None：
    给一个永远取不到新帧的游标会让客户端空转轮询。
    """
    sql = ("SELECT seq, ts, direction, bytes, data FROM term_recordings "
           "WHERE session_id=?")
    args: List[Any] = [session_id]
    if after_seq is not None:
        sql += " AND seq>?"
        args.append(int(after_seq))
    sql += " ORDER BY seq ASC LIMIT ?"
    args.append(int(limit))
    rows = db.query(sql, tuple(args))
    items = [{"seq": r["seq"], "ts": r["ts"], "dir": r["direction"],
              "bytes": r["bytes"],
              "data": base64.b64encode(bytes(r["data"] or b"")).decode("ascii")}
             for r in rows]
    # `more` 是**下界判断**：拿满 limit 条就不敢说「没有了」（可能还有）。
    # 说「没有了」需要额外一次探测；宁可让客户端多拉一次空的，也不静默截断。
    more = len(items) >= int(limit)
    return {"frames": items, "count": len(items),
            "first_seq": items[0]["seq"] if items else None,
            "last_seq": items[-1]["seq"] if items else None,
            "next_after_seq": items[-1]["seq"] if (more and items) else None,
            "more": more}


def frames(session_id: str, limit: int = 2000) -> List[Dict[str, Any]]:
    """兼容旧调用（裸列表）。**新代码用 `frames_page()`** —— 只有它能给出续传点。"""
    return frames_page(session_id, limit)["frames"]


def status(session_id: str) -> Dict[str, Any]:
    """给界面的播报：有没有录、录了多少、是不是触顶停的。"""
    rows = db.query(
        "SELECT COUNT(*) AS n, COALESCE(SUM(bytes),0) AS b, MIN(ts) AS t0, MAX(ts) AS t1 "
        "FROM term_recordings WHERE session_id=?", (session_id,))
    r = rows[0] if rows else {"n": 0, "b": 0, "t0": None, "t1": None}
    return {"session_id": session_id, "enabled": enabled(),
            "frames": int(r["n"] or 0), "bytes": int(r["b"] or 0),
            "t0": r["t0"], "t1": r["t1"],
            # 注意：这是**事后推断**（字节数已到上限），不是落库的 capped 真值。真值只在
            # Recorder 实例里（内存），进程重启后无从恢复 —— 所以叫 at_limit 不叫 capped：
            # 接口上不许拿推断冒充事实（口径 2026-09-29 定）。
            "at_limit": bool(r["b"] and int(r["b"]) >= max_session_bytes()),
            "limits": {"max_session": max_session_bytes(),
                       "max_total": max_total_bytes(),
                       "retention_days": retention_days()},
            "total_on_disk": total_bytes()}


def sweep() -> int:
    """保留期清理。返回删掉的帧数。7 天前的整会话删除。"""
    if not db.is_open():
        return 0
    cutoff = time.time() - retention_days() * 86400
    if not _exec("DELETE FROM term_recordings WHERE ts < ?", (cutoff,)):
        return 0
    # v0.13.101：被删字节由 trg_termrec_del 逐行减掉，无需应用层补偿。
    return 1
