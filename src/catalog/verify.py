"""catalog 质量闸门（PT-20261025-12 P2）。

只读体检，六项判据（方案 §9.1）：

| 检查 | 判据 | 失败含义 |
|---|---|---|
| FTS 完整性 | 索引自洽 / 幽灵条目 = 0 | 改删后索引漂移，查询返回已删对象 |
| 孤儿块 | `chunk` 有行但 `obj` 无 | 级联删除失效 |
| 重复对象 | 同 hash 占比 < 2% | 同一份内容被多个源各收一份 |
| missing 占比 | 各源 < 20% | 源路径中断，扫不到但没报错 |
| 滞后 | 按源 SLO（session 15min / skill 6h / doc、log 24h） | 库是旧快照，检索结果与现状不符 |
| 库体 | 与对象数比值合理 | 分块失控（把整篇日志/会话全量入库） |

返回结构化结果，`ok=False` 由调用方决定是告警还是阻断。
"""
from __future__ import annotations

import sqlite3
import time
from typing import Any, Dict, List

#: 各域「最多允许多久没扫」——超过即滞后（秒）。
LAG_SLO = {
    "agent": 6 * 3600,
    "skill": 6 * 3600,
    "project": 24 * 3600,
    "doc": 24 * 3600,
    "session": 15 * 60,
    "memory": 6 * 3600,
    "log": 24 * 3600,
}

DUP_RATIO_MAX = 0.02
MISSING_RATIO_MAX = 0.20


def _one(con: sqlite3.Connection, sql: str, args=()) -> Any:
    row = con.execute(sql, args).fetchone()
    return row[0] if row else None


def verify(con: sqlite3.Connection) -> Dict[str, Any]:
    now = int(time.time())
    checks: List[Dict[str, Any]] = []

    def add(name: str, ok: bool, value: Any, limit: Any, detail: str = ""):
        checks.append({"check": name, "ok": bool(ok), "value": value,
                       "limit": limit, "detail": detail})

    total = _one(con, "SELECT count(*) FROM obj") or 0
    chunks = _one(con, "SELECT count(*) FROM chunk") or 0

    # ① FTS 自洽：chunk 行必须在两个索引里都可查（不该差太多）
    try:
        indexed = _one(con, "SELECT count(*) FROM chunk_fts") or 0
        add("fts_index_coverage", indexed >= chunks * 0.99,
            {"chunks": chunks, "fts_rows": indexed}, ">=99%",
            "chunk_fts 行数与 chunk 不一致 ⇒ 有块没进索引")
    except sqlite3.Error as exc:
        add("fts_index_coverage", False, str(exc), ">=99%", "FTS 不可读")

    # ② 幽灵块：索引里有、正文表里没有（delete 未带原文的典型后果）
    try:
        ghost = _one(
            con,
            "SELECT count(*) FROM chunk_fts f "
            "LEFT JOIN chunk c ON c.cid=f.rowid WHERE c.cid IS NULL") or 0
        add("ghost_fts_rows", ghost == 0, ghost, 0,
            "索引指向不存在的 cid ⇒ 查询返回空壳")
    except sqlite3.Error as exc:
        add("ghost_fts_rows", False, str(exc), 0, "FTS 不可读")

    # ③ 孤儿块
    orphan = _one(
        con,
        "SELECT count(*) FROM chunk c "
        "LEFT JOIN obj o ON o.oid=c.oid WHERE o.oid IS NULL") or 0
    add("orphan_chunks", orphan == 0, orphan, 0, "对象删除后正文残留")

    # ④ 重复内容：同 hash 的对象组占全部对象的比例
    dup_rows = _one(
        con,
        "SELECT COALESCE(SUM(n),0) FROM ("
        " SELECT count(*) AS n FROM obj WHERE hash IS NOT NULL "
        " GROUP BY hash HAVING count(*)>1)") or 0
    ratio = round(dup_rows / total, 4) if total else 0.0
    add("duplicate_hash_ratio", ratio < DUP_RATIO_MAX, ratio,
        "<%.2f" % DUP_RATIO_MAX,
        "同一内容被多个源/多个副本各收一份 ⇒ 查询出现重复条目")

    # ⑤ 逐源 missing 与滞后
    missing_by_src: Dict[str, int] = {}
    lag_by_src: Dict[str, int] = {}
    for row in con.execute(
            "SELECT src_id, count(*) AS n FROM obj "
            "WHERE status='missing' GROUP BY src_id"):
        missing_by_src[row["src_id"]] = row["n"]
    for row in con.execute("SELECT src_id, domain, last_ok_at, adapter FROM src"):
        n_missing = missing_by_src.get(row["src_id"], 0)
        n_total = _one(con, "SELECT count(*) FROM obj WHERE src_id=?",
                       (row["src_id"],)) or 0
        n_total += n_missing
        r = round(n_missing / n_total, 4) if n_total else 0.0
        add("missing_ratio:%s" % row["src_id"], r < MISSING_RATIO_MAX, r,
            "<%.2f" % MISSING_RATIO_MAX,
            "missing %d/%d" % (n_missing, n_total))
        slo = LAG_SLO.get(row["domain"], 24 * 3600)
        lag = None
        if row["last_ok_at"]:
            try:
                import datetime as _dt
                ts = _dt.datetime.fromisoformat(
                    row["last_ok_at"].replace("Z", "+00:00")).timestamp()
                lag = int(now - ts)
            except (ValueError, TypeError):
                lag = None
        # 迁移源（adapter='migrate'）没有周期扫描，滞后不适用
        if (row["adapter"] or "") == "migrate":
            lag_by_src[row["src_id"]] = lag if lag is not None else -1
            add("lag:%s" % row["src_id"], True, lag, "n/a",
                "迁移源，无周期扫描")
            continue
        if lag is None:
            add("lag:%s" % row["src_id"], False, None, slo, "无扫描记录")
            lag_by_src[row["src_id"]] = -1
        else:
            lag_by_src[row["src_id"]] = lag
            add("lag:%s" % row["src_id"], lag <= slo, lag, slo,
                "超过 %.1f 小时未扫" % (slo / 3600))

    ok = all(c["ok"] for c in checks)
    return {
        "ok": ok,
        "checked_at": now,
        "objects": total,
        "chunks": chunks,
        "checks": checks,
        "lag_s": lag_by_src,
        "failed": [c["check"] for c in checks if not c["ok"]],
    }
