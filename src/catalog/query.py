"""catalog query —— 统一查询入口（PT-20261025-12 P0）。

**长度路由**（方案 §7.1，这是修「2 字中文查询 0 命中」的关键）：

| 查询 | 走哪张索引 | 为什么 |
|---|---|---|
| 全部词元 ≥3 字 | `chunk_fts`（trigram） | 子串精确；trigram 对 <3 字查询数学上必空 |
| 有 1~2 字词元 | `chunk_ftsu`（unicode61 按字切分） | 构造 `"记 忆"` 短语查询，实测可命中 |
| 中英混合 | 两路都查，RRF(k=60) 融合 | 英文缩写（AI/Go）走 uni 路 |

**排序**（方案 §7.2）：
`final = 0.55*bm25_norm + 0.20*title_hit + 0.15*authority + 0.10*recency`
同 oid 取最高分；未指定 domains 时单域最多取 `ceil(k/2)` 条，防一个域刷屏。

**降级必须显式**：`degraded` 非空时调用方知道「这一路没查全」，
而不是把「没查」读成「没有」——本仓反复禁的静默零命中。
"""
from __future__ import annotations

import math
import re
import sqlite3
import time
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import schema as _schema

#: 排序权重（方案 §7.2）
W_BM25, W_TITLE, W_AUTH, W_REC = 0.55, 0.20, 0.15, 0.10
#: RRF 常数
RRF_K = 60
#: 时效半衰期（天）
RECENCY_HALFLIFE_D = 180

#: 候选窗口倍数：先在 FTS 内按 bm25 排序取前缀，再 JOIN 元数据。
#: 实测 `catalog` 有 6,658 个 trigram 候选：旧实现带 4 表 JOIN 排序 844ms(冷)/44ms(热)，
#: 只取前缀再 JOIN 为 6ms。倍数递增是为了「过滤条件把窗口筛空」时不至于少收。
_SCAN_WINDOWS = (4, 16, 64)
#: 窗口上限。到达上限仍凑不满结果 ⇒ meta.capped=True 并进 degraded（不静默少收）。
_MAX_SCAN = 5000

_TOK_SPLIT = re.compile(r"[\s,，;；]+")


def _tokens(q: str) -> List[str]:
    return [t for t in _TOK_SPLIT.split((q or "").strip()) if t]


def _phrase(tok: str) -> str:
    """把单个词元包成 FTS5 短语（内部引号翻倍转义）。"""
    return '"%s"' % tok.replace('"', '""')


def _tri_terms(q: str) -> List[str]:
    """trigram 路可用的词元：只留 ≥3 字的（短词在这张索引上数学上必空）。"""
    return [t for t in _tokens(q) if len(t) >= 3]


def _uni_terms(q: str) -> List[str]:
    """uni 路词元：中文按字切分后包短语（`数据` → `"数 据"`）。"""
    out: List[str] = []
    for t in _tokens(q):
        u = _schema.cjk_split(t)
        out.append(_phrase(u) if u else _phrase(t))
    return out


# ── 主入口 ────────────────────────────────────────────────────────────
def query(con: sqlite3.Connection, q: str, domains: str = "", types: str = "",
          k: int = 10, since: int = 0) -> Dict[str, Any]:
    """统一查询。返回 {ok, count, items, backends, ms, degraded}。"""
    t0 = time.monotonic()
    q = (q or "").strip()
    if not q:
        return {"ok": False, "count": 0, "items": [], "backends": [], "ms": 0.0,
                "degraded": "empty_query",
                "error": "查询为空"}
    want_domains = [d for d in (domains or "").split(",") if d.strip()]
    want_types = [t for t in (types or "").split(",") if t.strip()]
    lim = max(1, min(int(k), 100))

    backends: List[Dict[str, Any]] = []
    merged: Dict[str, Dict[str, Any]] = {}
    degraded: List[str] = []
    routing: List[str] = []

    tri = _tri_terms(q)
    uni = _uni_terms(q)
    toks = _tokens(q)
    # 路由判据：trigram 只接 ≥3 字词元；有 1~2 字词元时必须走 uni 路
    use_tri = bool(tri)
    use_uni = (not use_tri) or any(len(t) < 3 for t in toks)

    if use_tri:
        routing.append("chunk_fts")
        r = _fts_rank(con, "chunk_fts", " AND ".join(_phrase(t) for t in tri),
                      want_domains, want_types, since, lim * 3, toks)
        backends.append({"name": "catalog.chunk_fts", **r["meta"]})
        _merge(merged, r["items"], "fts")
        if not r["meta"]["ok"]:
            degraded.append("chunk_fts:%s" % r["meta"]["error"])
        elif r["meta"].get("capped"):
            degraded.append("chunk_fts:capped_%d" % r["meta"]["scanned"])
    if use_uni:
        routing.append("chunk_ftsu")
        r = _fts_rank(con, "chunk_ftsu", " AND ".join(uni),
                      want_domains, want_types, since, lim * 3, toks)
        backends.append({"name": "catalog.chunk_ftsu", **r["meta"]})
        _merge(merged, r["items"], "ftsu")
        if not r["meta"]["ok"]:
            degraded.append("chunk_ftsu:%s" % r["meta"]["error"])
        elif r["meta"].get("capped"):
            degraded.append("chunk_ftsu:capped_%d" % r["meta"]["scanned"])

    if not merged:
        # 标题兜底：短查询至少能在 title/uri 上 LIKE（全表小，13k 级可接受）
        routing.append("obj_title")
        r = _title_like(con, q, want_domains, want_types, since, lim)
        backends.append({"name": "catalog.obj_title", **r["meta"]})
        _merge(merged, r["items"], "title")
        if not r["meta"]["ok"]:
            degraded.append("obj_title:%s" % r["meta"]["error"])

    items = _finalize(merged, q, lim, not want_domains)
    return {"ok": True, "count": len(items), "items": items,
            "backends": backends, "routing": routing,
            "ms": round((time.monotonic() - t0) * 1000, 1),
            "degraded": ";".join(degraded) or None}


def _fts_rank(con: sqlite3.Connection, fts: str, match: str,
              want_domains: List[str], want_types: List[str], since: int,
              limit: int, toks: Sequence[str]) -> Dict[str, Any]:
    """跑一张 FTS，按 bm25 取 top-N，返回 [{oid, cid, text, bm25, snippet}] + meta。

    **窗口化**：`bm25()` 仍在 FTS 索引内算完整排名，但只取排名前缀，再 JOIN 元数据。
    旧实现把 4 表 JOIN 塞进 `ORDER BY bm25()` ⇒ 候选有多少就 JOIN 多少：实测 `catalog`
    （trigram 6,658 候选）热 44ms，且规划器会反向从 `obj_domain_mtime` 驱动（带域名过滤时
    扫全部 33k 文档、190ms）。先取前缀后，同一查询 21~23ms，小候选查询 1.8ms。

    排序不变；前缀被过滤条件筛空时才放大窗口，放到 `_MAX_SCAN` 仍不够就标 `capped`
    并进 `degraded`——**不许静默少收**。
    """
    t0 = time.monotonic()
    frag, fargs = _filter_sql(want_domains, want_types, since)
    sql = ("SELECT c.oid AS oid, c.cid AS cid, c.text AS text, "
           "o.title AS title, o.uri AS uri, o.mtime AS mtime, "
           "o.domain AS domain, o.type AS type, o.summary AS summary, "
           "o.native_key AS native_key, o.src_id AS src_id, "
           "s.authority AS authority, f.bm AS bm FROM ("
           "SELECT rowid AS cid, bm25(%(f)s) AS bm FROM %(f)s "
           "WHERE %(f)s MATCH ? ORDER BY bm LIMIT ?) f "
           "JOIN chunk c ON c.cid = f.cid JOIN obj o ON o.oid = c.oid "
           "JOIN src s ON s.src_id = o.src_id "
           "WHERE o.status != 'missing' %(frag)s "
           "ORDER BY f.bm LIMIT ?" % {"f": fts, "frag": frag})
    scanned, capped, rows = 0, False, []
    try:
        for mult in _SCAN_WINDOWS:
            window = min(_MAX_SCAN, max(limit, limit * mult))
            rows = con.execute(sql, [match, window] + fargs + [limit]).fetchall()
            scanned = window
            if len(rows) >= limit:
                break
            more = con.execute(
                "SELECT 1 FROM %s WHERE %s MATCH ? LIMIT 1 OFFSET ?"
                % (fts, fts), (match, window)).fetchone() is not None
            if not more:          # 候选已取尽，再放大也没有新行
                break
            capped = window >= _MAX_SCAN
    except sqlite3.Error as e:
        return {"items": [], "meta": {"ok": False, "hits": 0, "ms": 0.0,
                                      "error": "%s: %s" % (type(e).__name__, e)}}
    items = []
    for r in rows:
        items.append({
            "oid": r["oid"], "cid": r["cid"], "text": r["text"],
            "title": r["title"], "uri": r["uri"], "mtime": r["mtime"],
            "domain": r["domain"], "type": r["type"],
            "summary": r["summary"], "native_key": r["native_key"],
            "src_id": r["src_id"], "authority": r["authority"],
            "bm25": r["bm"], "snippet": _snippet(r["text"], toks),
        })
    return {"items": items,
            "meta": {"ok": True, "hits": len(items), "scanned": scanned,
                     "capped": capped,
                     "ms": round((time.monotonic() - t0) * 1000, 1),
                     "error": None}}


def _filter_sql(want_domains: List[str], want_types: List[str],
                since: int) -> Tuple[str, List[Any]]:
    """过滤条件片段（obj 侧别名固定为 o）。"""
    sql, args = "", []
    if want_domains:
        sql += "AND o.domain IN (%s) " % ",".join("?" * len(want_domains))
        args += want_domains
    if want_types:
        sql += "AND o.type IN (%s) " % ",".join("?" * len(want_types))
        args += want_types
    if since:
        sql += "AND o.mtime >= ? "
        args.append(int(since))
    return sql, args


def _snippet(text: str, toks: Sequence[str], width: int = 120) -> str:
    """命中窗口：取**首次出现**的词元位置切前后各 width 字。

    不用 FTS5 的 `snippet()`：它对 30 条要 10ms（比检索本身还贵），且只用于展示，
    不参与排序。这里用已经取回的 `chunk.text`，零额外查询。
    """
    t = (text or "").strip()
    if not t:
        return ""
    low = t.lower()
    pos, hit = -1, ""
    for tok in toks:
        if not tok:
            continue
        i = low.find(tok.lower())
        if i >= 0 and (pos < 0 or i < pos):
            pos, hit = i, tok
    if pos < 0:
        return t[:width * 2]
    a, b = max(0, pos - width), min(len(t), pos + len(hit) + width)
    head = "…" if a > 0 else ""
    tail = "…" if b < len(t) else ""
    return head + t[a:pos] + "[" + t[pos:pos + len(hit)] + "]" + t[pos + len(hit):b] + tail


def _title_like(con: sqlite3.Connection, q: str, want_domains: List[str],
                want_types: List[str], since: int, limit: int) -> Dict[str, Any]:
    """标题/uri 的 LIKE 兜底（短查询全表小，可接受）。"""
    sql = ("SELECT oid, domain, type, title, uri, mtime, summary, native_key, "
           "src_id FROM obj WHERE status!='missing' "
           "AND (lower(title) LIKE ? ESCAPE '\\' "
           "OR lower(uri) LIKE ? ESCAPE '\\') ")
    literal = q.lower().replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
    like = "%%%s%%" % literal
    args: List[Any] = [like, like]
    if want_domains:
        sql += "AND domain IN (%s) " % ",".join("?" * len(want_domains))
        args += want_domains
    if want_types:
        sql += "AND type IN (%s) " % ",".join("?" * len(want_types))
        args += want_types
    if since:
        sql += "AND mtime >= ? "
        args.append(int(since))
    sql += "ORDER BY mtime DESC LIMIT ?"
    args.append(limit)
    t0 = time.monotonic()
    try:
        rows = con.execute(sql, args).fetchall()
    except sqlite3.Error as e:
        return {"items": [], "meta": {"ok": False, "hits": 0,
                                      "ms": round((time.monotonic() - t0) * 1000, 1),
                                      "error": "%s: %s" % (type(e).__name__, e)}}
    return {"items": [dict(r) for r in rows],
            "meta": {"ok": True, "hits": len(rows),
                     "ms": round((time.monotonic() - t0) * 1000, 1), "error": None}}


def _merge(merged: Dict[str, Dict[str, Any]], items: Sequence[Dict[str, Any]],
           how: str) -> None:
    """RRF 融合：同一 oid 多路命中按名次累加。"""
    for rank, it in enumerate(items, 1):
        oid = it["oid"]
        rrf = 1.0 / (RRF_K + rank)
        cur = merged.get(oid)
        if cur is None:
            cur = dict(it)
            cur["rrf"] = 0.0
            merged[oid] = cur
        cur["rrf"] += rrf
        cur.setdefault("_hits", []).append(how)


def _finalize(merged: Dict[str, Dict[str, Any]], q: str, lim: int,
              diversify: bool) -> List[Dict[str, Any]]:
    """加权定分 + 去重 + 域多样性裁剪，输出对外形状。"""
    now = time.time()
    toks = [t.lower() for t in _tokens(q)]
    scored: List[Tuple[float, Dict[str, Any]]] = []
    for oid, it in merged.items():
        s_bm = 1.0 / (1.0 + abs(it.get("bm25") or 0.0))
        title = (it.get("title") or "").lower()
        native = (it.get("native_key") or "").lower()
        # title_hit：查询词元在标题/路径里出现得越多越靠前
        if title and toks:
            hit = sum(1 for t in toks if t in title or t in native)
            s_title = min(1.0, hit / len(toks))
        else:
            s_title = 0.2
        s_auth = 1.0 if (it.get("authority") or 0) else 0.5
        age_d = max(0.0, (now - (it.get("mtime") or now)) / 86400.0)
        s_rec = math.pow(0.5, age_d / RECENCY_HALFLIFE_D)
        score = (W_BM25 * s_bm + W_TITLE * s_title
                 + W_AUTH * s_auth + W_REC * s_rec)
        scored.append((score, it))
    scored.sort(key=lambda x: -x[0])
    out: List[Dict[str, Any]] = []
    per_domain: Dict[str, int] = {}
    cap = max(1, math.ceil(lim / 2)) if diversify else lim
    for score, it in scored:
        dom = it.get("domain") or ""
        if diversify and per_domain.get(dom, 0) >= cap:
            continue
        per_domain[dom] = per_domain.get(dom, 0) + 1
        out.append({
            "oid": it["oid"], "domain": dom, "type": it.get("type"),
            "title": (it.get("title") or "")[:200],
            "snippet": (it.get("snippet") or "")[:400],
            "score": round(score, 4),
            "uri": it.get("uri"), "source": it.get("src_id"),
            "native_key": it.get("native_key"),
            "freshness": {"mtime": it.get("mtime"),
                          "age_s": int(max(0, now - (it.get("mtime") or now)))},
            "_hits": sorted(set(it.get("_hits") or [])),
        })
        if len(out) >= lim:
            break
    return out


# ── 详情 / 状态 ────────────────────────────────────────────────────────
def get(con: sqlite3.Connection, oid: str) -> Dict[str, Any]:
    """对象详情：obj 行 + 分域扩展行 + 分块数 + 邻接。"""
    row = con.execute("SELECT * FROM obj WHERE oid=?", (oid,)).fetchone()
    if not row:
        return {"ok": False, "error": "oid 不存在：%s" % oid, "item": None}
    out: Dict[str, Any] = {"ok": True, "item": dict(row)}
    tbl = _schema.DOMAIN_TABLE.get(row["domain"])
    if tbl:
        ext = con.execute("SELECT * FROM %s WHERE oid=?" % tbl,
                          (oid,)).fetchone()
        out["ext"] = dict(ext) if ext else None
    out["chunks"] = con.execute(
        "SELECT COUNT(*) AS n FROM chunk WHERE oid=?", (oid,)).fetchone()["n"]
    out["neighbors"] = [dict(r) for r in con.execute(
        "SELECT dst_oid, rel, weight FROM edge WHERE src_oid=? "
        "UNION ALL SELECT src_oid, rel, weight FROM edge WHERE dst_oid=?",
        (oid, oid)).fetchall()]
    return out


def status(con: sqlite3.Connection) -> Dict[str, Any]:
    """各源健康：last_ok_at / 滞后秒数 / 对象数 / 失败原因。"""
    import datetime as _dt
    now = _dt.datetime.now(_dt.timezone.utc)
    out: List[Dict[str, Any]] = []
    for r in con.execute("SELECT * FROM src ORDER BY domain, src_id").fetchall():
        counts = {c["status"]: c["n"] for c in con.execute(
            "SELECT status, COUNT(*) AS n FROM obj WHERE src_id=? GROUP BY status",
            (r["src_id"],)).fetchall()}
        lag = None
        if r["last_ok_at"]:
            try:
                ts = _dt.datetime.strptime(
                    r["last_ok_at"], "%Y-%m-%dT%H:%M:%SZ").replace(
                    tzinfo=_dt.timezone.utc)
                lag = int((now - ts).total_seconds())
            except ValueError:
                lag = None
        out.append({
            "src_id": r["src_id"], "domain": r["domain"], "kind": r["kind"],
            "authority": r["authority"], "uri": r["uri"],
            "adapter": r["adapter"], "enabled": r["enabled"],
            "scan_mode": r["scan_mode"],
            "last_ok_at": r["last_ok_at"], "lag_s": lag,
            "last_ms": r["last_ms"], "last_err": r["last_err"],
            "objects": counts,
            "object_domains": {d["domain"]: d["n"] for d in con.execute(
                "SELECT domain, COUNT(*) AS n FROM obj WHERE src_id=? "
                "AND status!='missing' GROUP BY domain", (r["src_id"],)).fetchall()},
        })
    ver = con.execute("SELECT v FROM meta WHERE k='schema_version'").fetchone()
    # 域清单按**实际对象**统计，不按源登记表 —— 一个源可以产出多个域
    # （agents.registry 同时产出 agent 与 service 两个域）。
    dom_rows = con.execute(
        "SELECT domain, COUNT(*) AS n FROM obj WHERE status!='missing' "
        "GROUP BY domain ORDER BY domain").fetchall()
    return {"ok": True, "sources": out,
            "schema_version": int(ver[0]) if ver else None,
            "domains": [r["domain"] for r in dom_rows],
            "domain_objects": {r["domain"]: r["n"] for r in dom_rows}}
