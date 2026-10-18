"""memvitals —— 记忆中心健康探针（PT-20261018-08 P0，2026-10-18）

盘上现状（实测，不是推断）：
  · `/api/memory/health` 在 `memindex.py:41` 的注释里被当成既有端点，**实际 404**；
  · 「源健康」11 路里没有 `local` / `tdai`，而它们是默认检索的两路；
  · `pi/codex/archived` 三行同数 10139，因为 `memfed._probe_one` 对三路都调
    `memindex.probe()`（全库统计）⇒ 页面「各源条目合计」30973 而真实唯一 10139；
  · note 被 `[:70]` 截断，**「构建于 …」正好被切掉**，于是「索引停更」永不可见。

因此本模块做三件不同的事：
  1. 四层判据：L0 可达 / L1 新鲜 / L2 语义 / L3 一致性；
  2. **聚合在后台做**（这里可以慢，页面读文件 O(1)）——每源真数直接 GROUP BY fed_proj；
  3. 所有不是 ok 的项**必须带 reason**（`tests/test_memvitals.py` 断言）。

四条铁律（改动前先读）：
  1. **只读**：memindex.db 一律 `mode=ro`；不碰 memories / agents.db 的写路径。
  2. **探针自身失败 ≠ 被测对象失败**：超时/异常进 `probe_error`，不许渲染成「零命中」。
  3. **判据必须能红**：红必须点名对象 + 给处置方向；只返回布尔即为缺陷。
  4. **不猜**：拿不到就 `unknown` 并写原因，不用 0 或 True 顶替。

产物：`data/memvitals.json`（派生数据，删了下次自动重生，不进备份）。
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

SCHEMA = 1

REPO = Path(__file__).resolve().parent.parent
OUT = Path(os.environ.get("MEMVITALS_OUT") or (REPO / "data" / "memvitals.json"))
PROJ_DB = Path(os.environ.get("MEMVITALS_PROJ_DB") or (REPO / "data" / "memindex.db"))

HUB = os.environ.get("HUB_BASE", "http://127.0.0.1:3102")
TDAI = os.environ.get("TDAI_BASE", "http://127.0.0.1:8420")

HISTORY_MAX = 288                 # 10 分钟一次 ⇒ 2 天
PROJECTION_WARN_H = 36.0          # timer 每日 05:10 增量，36h 未构建即异常
PROJECTION_ERROR_H = 72.0
L1_STALE_DAYS = 14                # 与 memstats.STALE_DAYS 同口径
DEVIATION_WARN = 0.30             # 每源条数相对上次偏差
LATENCY_FACTOR = 2.0              # 相对基线的劣化倍数
LATENCY_STRIKES = 3               # 连续次数（判例 106：单次读数不做劣化结论）
HTTP_TIMEOUT = 10.0

#: L2 探针集：**断言来源，不只看「有没有返回」**。
#: 「返回了东西」是恒真判据——排序坏掉、索引空壳都能返回 3 条。
#: 这里要求命中来源里必须出现期望集合（少一路即失败，多出来的不管）。
PROBES: List[Dict[str, Any]] = [
    {"id": "probe:disk", "q": "磁盘空间不足", "expect": {"tdai_l1"}},
    {"id": "probe:ccr", "q": "CCR 轮换", "expect": {"tdai_l1", "claude_mem"}},
    {"id": "probe:backup", "q": "备份 恢复", "expect": {"tdai_l1"}},
]

#: 反向探针：一个正常语料不可能出现的串 ⇒ 词法通道必须 0 命中。
#: 它防的是「什么都返回」这一类退化（打分塌陷/全库兜底）。
#:
#: ⚠ 首次实跑（2026-10-18 21:11）发现：**不能把 tdai 算进来**。同一串去查 TDAI 语义检索，
#: 返回 3 条、top score **0.77** —— 语义通道没有相关性下限，永远返回最近邻。
#: 那是上游行为、不是本机故障，把它判红就是「恒红」（与恒真一样没用）。
#: 故本判据只量**词法通道**；语义通道无下限这件事进 notes（信息项），不进 verdict。
REVERSE_Q = "ZZQX_不存在的词_9f3a1c"
LEX_SOURCES = "local,claude_mem,claude_projects,workbuddy_memory,grok_memory,hermes_memory,workspace_files,experiences"

#: 注入包探针：Agent 开局真实走的那条路。无 q 时它的哨兵是 RECENT_Q（memory.py P0-3）。
CTX_SOURCES = "local,tdai,claude_mem,workbuddy_memory,claude_projects"

#: 由 memindex 投影承载的三路（它们的 fedsources.count 是全库同一个数，不可用）
PROJ_IDS = ("pi_sessions", "codex_sessions", "archived_sessions")


class ProbeError(RuntimeError):
    """探针自身失败（网络/解析）。与被测对象的「零命中」严格区分。"""


# ── 小工具 ────────────────────────────────────────────────────────────

def _now() -> datetime:
    return datetime.now().astimezone()


def _iso(dt: Optional[datetime]) -> Optional[str]:
    return dt.isoformat(timespec="seconds") if dt else None


def _http_json(url: str, timeout: float = HTTP_TIMEOUT) -> Dict[str, Any]:
    req = urllib.request.Request(url, headers={"Accept": "application/json"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        # str(HTTPError) 不含正文，必须自己读——判例：这条坑过一次
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")[:200]
        except Exception:  # noqa: BLE001
            pass
        raise ProbeError(f"HTTP {e.code} {url.split('?')[0]} {body}".strip()) from e
    except Exception as e:  # noqa: BLE001
        raise ProbeError(f"{type(e).__name__}: {e}") from e


def _age_hours(iso_ts: Optional[str], now: Optional[datetime] = None) -> Optional[float]:
    """ISO 串 → 距今小时数。naive 按本机时区解释（systemd/meta 写的是本地时间）。"""
    if not iso_ts or not isinstance(iso_ts, str):
        return None
    try:
        t = datetime.fromisoformat(iso_ts.strip().replace("Z", "+00:00"))
    except (ValueError, TypeError):
        return None
    if t.tzinfo is None:
        t = t.astimezone()
    now = now or _now()
    return round((now - t).total_seconds() / 3600.0, 2)


def _check(cid: str, layer: str, name: str, status: str, reason: str = "",
           evidence: Optional[Dict[str, Any]] = None, hard: bool = False) -> Dict[str, Any]:
    return {"id": cid, "layer": layer, "name": name, "status": status,
            "reason": reason, "evidence": evidence or {}, "hard": hard}


# ── L0 可达 ──────────────────────────────────────────────────────────

def _fresh_claude_mem() -> Optional[str]:
    """claude-mem 最后一条写入时间。

    这张表没有 `created_at` 文本列，只有 `created_at_epoch`（整数秒）。
    取它是为了能看出「capture 静默停更」——正是本探针存在的理由之一。
    """
    p = Path(os.path.expanduser("~/.claude-mem/claude-mem.db"))
    if not p.is_file():
        return None
    try:
        con = sqlite3.connect(f"file:{p}?mode=ro", uri=True, timeout=1.0)
        try:
            v = con.execute(
                "SELECT MAX(t) FROM (SELECT created_at_epoch AS t FROM observations "
                "UNION ALL SELECT created_at_epoch AS t FROM session_summaries)"
            ).fetchone()[0]
        finally:
            con.close()
        if not v:
            return None
        ts = float(v)
        if ts > 1e11:      # 实测是毫秒（1792254671154）；秒级时保持原样
            ts /= 1000.0
        return _iso(datetime.fromtimestamp(ts).astimezone())
    except Exception:  # noqa: BLE001
        return None


def collect_sources() -> List[Dict[str, Any]]:
    """源清单 + 可达性。

    ⚠ 这里**只取 enabled/ok**，**不取 count**：fedsources 的 count 对三个投影源
    是同一个全库数（见模块头），直接用会把 3 倍虚高带进 vitals。真数在
    `collect_projection()` 里按 source 分组算。
    """
    try:
        d = _http_json(f"{HUB}/api/memory/fedsources")
    except ProbeError as e:
        return [{"id": "sources", "label": "联邦源清单", "enabled": True, "ok": False,
                 "status": "error", "reason": f"清单取不到：{e}", "count": None,
                 "fresh_at": None, "note": ""}]
    out: List[Dict[str, Any]] = []
    for s in d.get("sources", []):
        p = s.get("probe") or {}
        enabled = bool(s.get("enabled"))
        # 三个投影源的 count 在 fedsources 里是**同一个全库数**（memfed._probe_one 的缺陷），
        # 直接信它会把 3 倍虚高带进 vitals；它们的真数由 collect_projection 分组算。
        cnt = None if s.get("id") in PROJ_IDS else p.get("count")
        if not enabled:
            status, reason = "off", "已停用（不参与检索）"
        elif p.get("ok"):
            status, reason = "ok", ""
        else:
            status, reason = "error", f"不可达：{p.get('note') or '无说明'}"
        out.append({"id": s.get("id"), "label": s.get("label") or s.get("id"),
                    "enabled": enabled, "ok": bool(p.get("ok")), "status": status,
                    "reason": reason, "count": cnt, "fresh_at": None,
                    "ms": p.get("ms") if isinstance(p.get("ms"), (int, float)) else None,
                    "note": str(p.get("note") or "")[:120]})

    # local / tdai 不在 fedsources 注册表里，但它们是默认检索的两路 ⇒ 必须单列
    out.append(_probe_local())
    out.append(_probe_tdai())
    # claude-mem 的 fedsources probe 不带时间；单查一次（329 行，毫秒级）
    for s in out:
        if s.get("id") == "claude_mem":
            s["fresh_at"] = _fresh_claude_mem()
    return out


def _probe_local() -> Dict[str, Any]:
    base = {"id": "local", "label": "本地 L1 便签（hub 自有库）", "enabled": True,
            "count": None, "fresh_at": None, "ms": None, "note": "memories 表 status=active"}
    t0 = time.monotonic()
    try:
        d = _http_json(f"{HUB}/api/memory/l1?limit=200")
    except ProbeError as e:
        return {**base, "ok": False, "status": "error", "reason": f"接口取不到：{e}"}
    rows = d.get("memories") or []
    newest = max((str(r.get("created_at") or "") for r in rows), default="")
    base.update({"ok": True, "status": "ok", "reason": "",
                 "count": len(rows), "fresh_at": newest[:10] or None,
                 "ms": round((time.monotonic() - t0) * 1000, 1)})
    return base


def _probe_tdai() -> Dict[str, Any]:
    base = {"id": "tdai", "label": "TDAI 权威库（:8420）", "enabled": True,
            "count": None, "fresh_at": None, "ms": None,
            "note": "L1/L0 总量取自 hub /health 的缓存"}
    t0 = time.monotonic()
    try:
        h = _http_json(f"{TDAI}/health", timeout=5.0)
    except ProbeError as e:
        return {**base, "ok": False, "status": "error",
                "reason": f"权威库不可达：{e}"}
    base["ms"] = round((time.monotonic() - t0) * 1000, 1)
    stores = h.get("stores") or {}
    bad = [k for k in ("vectorStore", "embeddingService") if not stores.get(k)]
    try:
        hb = _http_json(f"{HUB}/health", timeout=5.0).get("memory_backend") or {}
    except ProbeError:
        hb = {}
    base["count"] = hb.get("l1_total")
    base["note"] = f"L1 {hb.get('l1_total')} / L0 {hb.get('l0_total')} · 状态缓存 {round(hb.get('age_s') or 0)}s 前"
    if bad:
        return {**base, "ok": False, "status": "error",
                "reason": f"TDAI /health 声明子系统不可用：{','.join(bad)}",
                "evidence": {"stores": stores}}
    if hb.get("state") and hb.get("state") != "ok":
        return {**base, "ok": False, "status": "error",
                "reason": f"hub 侧 memory_backend={hb.get('state')}：{hb.get('error') or ''}"[:200]}
    return {**base, "ok": True, "status": "ok", "reason": ""}


# ── L1 新鲜 / L3 一致性（投影库） ─────────────────────────────────────

def collect_projection(prev: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """读 memindex 元数据 + **按 source 分组**的真数。

    全表 GROUP BY 在本机冷启约 5s、热 0.4s —— 后台 oneshot 付得起，
    页面付不起（这正是把它移出查询路径的理由）。

    ⚠ 2026-10-18 复测纠正：上面那句「热 0.4s」在**内存吃紧时**不成立。本机可用内存
    仅几百 MB，2.4GB 表的页缓存放不下 ⇒ 连跑两次都是 **12s**（不是冷启一次）。
    该表只在每日 `memindex incremental` 时变化（`agenthub-memindex.timer`：
    `OnCalendar=05:10` + `RandomizedDelaySec=10m`，实际 05:10–05:20）⇒ 用 **db mtime 当缓存键**：
    mtime 没动就复用上一轮分组结果。这不是拍脑袋的 TTL —— 数据没变就没有重算的理由，
    也不会像「TTL 等于采集周期」那样每轮都恰好过期。

    判据（L1）：稳态 p95 < 1s；**每日 `memindex incremental` 后首轮**（05:10–05:20）
    页缓存全失效，实测 ≈19s，**一次性成本，必须从 p95 样本中排除**。
    彻底消除要给 `fed_proj` 加 `source` 索引——2.4GB 表在线建索引的代价未评估，未做。
    """
    if not PROJ_DB.exists():
        return {"ok": False, "error": "索引不存在（检索会退回 rg）", "by_source": {}}
    try:
        db_mtime = PROJ_DB.stat().st_mtime
    except OSError as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "by_source": {}}
    old = (prev or {}).get("projection") or {}
    if old.get("ok") and old.get("db_mtime") == db_mtime and old.get("by_source"):
        return {**old, "ms": 0.0, "cached": True}
    t0 = time.monotonic()
    try:
        con = sqlite3.connect(f"file:{PROJ_DB}?mode=ro", uri=True, timeout=10)
        try:
            meta = {str(k): str(v) for k, v in con.execute("SELECT k, v FROM meta")}
            rows = con.execute(
                "SELECT source, COUNT(*), MAX(mtime) FROM fed_proj GROUP BY source"
            ).fetchall()
        finally:
            con.close()
    except sqlite3.Error as e:
        return {"ok": False, "error": f"{type(e).__name__}: {e}", "by_source": {}}
    by_source: Dict[str, Dict[str, Any]] = {}
    for sid, cnt, mt in rows:
        by_source[str(sid)] = {"chunks": int(cnt), "fresh_at": _iso(_epoch_to_dt(mt))}
    return {"ok": True,
            "files": int(meta.get("files") or 0),
            "chunks": int(meta.get("chunks") or 0),
            "last_build": meta.get("last_build"),
            "by_source": by_source,
            "db_mtime": db_mtime,
            "cached": False,
            "ms": round((time.monotonic() - t0) * 1000, 1)}


def _epoch_to_dt(v: Any) -> Optional[datetime]:
    try:
        f = float(v)
    except (TypeError, ValueError):
        return None
    if f <= 0:
        return None
    return datetime.fromtimestamp(f, tz=timezone.utc).astimezone()


# ── L2 语义 ──────────────────────────────────────────────────────────

def collect_probes() -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    for p in PROBES:
        out.append(_run_probe(p["id"], p["q"], set(p["expect"])))
    out.append(_run_reverse())
    out.append(_run_context())
    return out


def _search(q: str, limit: int = 5, sources: Optional[str] = None) -> Dict[str, Any]:
    import urllib.parse
    url = f"{HUB}/api/memory/search?q={urllib.parse.quote(q)}&limit={limit}"
    if sources:
        url += f"&sources={urllib.parse.quote(sources)}"
    return _http_json(url)


def _run_probe(cid: str, q: str, expect: set) -> Dict[str, Any]:
    t0 = time.monotonic()
    try:
        d = _search(q)
    except ProbeError as e:
        return {"id": cid, "layer": "L2", "name": f"检索「{q}」", "status": "error",
                "reason": f"探针自身失败：{e}", "hard": True, "evidence": {"q": q}}
    took = d.get("took_ms")
    took = (time.monotonic() - t0) * 1000 if took is None else took
    hit = {b.get("name") for b in (d.get("backends") or []) if b.get("count")}
    missing = sorted(expect - hit)
    ok = bool(d.get("count")) and not missing
    reason = "" if ok else (
        f"零命中（count=0）" if not d.get("count")
        else f"期望来源未命中：{','.join(missing)}（实际命中 {','.join(sorted(hit)) or '无'}）")
    return {"id": cid, "layer": "L2", "name": f"检索「{q}」",
            "status": "ok" if ok else "error", "reason": reason, "hard": True,
            "evidence": {"q": q, "count": d.get("count"), "took_ms": round(float(took), 1),
                         "hit": sorted(hit), "expect": sorted(expect)}}


def _run_reverse() -> Dict[str, Any]:
    """词法通道必须 0 命中；语义通道无下限的情形如实记进 evidence（不进判定）。"""
    try:
        d = _search(REVERSE_Q, limit=3, sources=LEX_SOURCES)
    except ProbeError as e:
        return {"id": "probe:reverse", "layer": "L2", "name": "反向探针（词法应 0 命中）",
                "status": "error", "reason": f"探针自身失败：{e}", "hard": True}
    n = int(d.get("count") or 0)
    evidence: Dict[str, Any] = {"q": REVERSE_Q, "count": n, "scope": "lexical"}
    # 顺带取一次语义通道的同一串：只为留证（0.77 分这种现象要能被看见），不参与判定
    try:
        f = _search(REVERSE_Q, limit=3)
        if int(f.get("count") or 0) > 0:
            top = max((float(m.get("score") or 0) for m in (f.get("memories") or [])), default=0.0)
            evidence["semantic_floor"] = {"count": int(f.get("count") or 0),
                                          "top_score": round(top, 3)}
    except ProbeError:
        pass
    return {"id": "probe:reverse", "layer": "L2", "name": "反向探针（词法应 0 命中）",
            "status": "ok" if n == 0 else "error",
            "reason": "" if n == 0 else f"不可能命中的词在词法通道返回 {n} 条 ⇒ 兜底/打分退化",
            "hard": True, "evidence": evidence}


def _run_context() -> Dict[str, Any]:
    try:
        d = _http_json(f"{HUB}/api/memory/context?sources={CTX_SOURCES}")
    except ProbeError as e:
        return {"id": "probe:context", "layer": "L2", "name": "开局注入包",
                "status": "error", "reason": f"探针自身失败：{e}", "hard": True}
    chars = int(d.get("chars") or 0)
    fed = d.get("fed") or {}
    nsrc = fed.get("sources") if isinstance(fed.get("sources"), int) else 0
    ok = chars > 0
    return {"id": "probe:context", "layer": "L2", "name": "开局注入包",
            "status": "ok" if ok else "error",
            "reason": "" if ok else "注入包 0 字符 ⇒ Agent 开局拿不到任何记忆（指标绿、功能死）",
            "hard": True,
            "evidence": {"chars": chars, "fed_sources": nsrc,
                         "fed_chars": fed.get("chars"), "took_ms": d.get("took_ms")}}


# ── 纯逻辑：判定 ──────────────────────────────────────────────────────

def _prev_median_took(prev: Optional[Dict[str, Any]], cid: str) -> Optional[float]:
    """取某探针最近 20 次 took_ms 的中位数（跨 history 不够，用 history 里的 probes）。"""
    if not prev:
        return None
    vals: List[float] = []
    for h in (prev.get("history") or [])[-20:]:
        for p in h.get("probes") or []:
            if p.get("id") == cid and isinstance(p.get("took_ms"), (int, float)):
                vals.append(float(p["took_ms"]))
    vals.sort()
    return vals[len(vals) // 2] if vals else None


def build_report(sources: List[Dict[str, Any]], proj: Dict[str, Any],
                 probes: List[Dict[str, Any]], prev: Optional[Dict[str, Any]] = None,
                 now: Optional[datetime] = None,
                 took_ms: Optional[float] = None) -> Dict[str, Any]:
    """把采集结果折叠成一份报告。**纯函数**（无 IO）——测试直接喂构造数据。"""
    now = now or _now()
    checks: List[Dict[str, Any]] = []

    # L0：每路源
    for s in sources:
        if s.get("status") == "off":
            continue
        if not s.get("enabled", True):
            continue
        checks.append(_check(f"l0:{s['id']}", "L0", s.get("label") or s["id"],
                             "ok" if s.get("ok") else "error",
                             "" if s.get("ok") else (s.get("reason") or "不可达"),
                             {"count": s.get("count"), "note": s.get("note")},
                             hard=True))

    # L1：新鲜度
    if not proj.get("ok"):
        checks.append(_check("l1:projection", "L1", "会话投影索引", "error",
                             f"读不到投影库：{proj.get('error')}", hard=True))
    else:
        age = _age_hours(proj.get("last_build"), now)
        if age is None:
            checks.append(_check("l1:projection", "L1", "会话投影索引", "warn",
                                 "meta 里没有可解析的 last_build"))
        else:
            st = "ok" if age < PROJECTION_WARN_H else ("warn" if age < PROJECTION_ERROR_H else "error")
            reason = "" if st == "ok" else f"投影已 {age}h 未重建（阈值 {PROJECTION_WARN_H:g}h 黄 / {PROJECTION_ERROR_H:g}h 红）"
            checks.append(_check("l1:projection", "L1", "会话投影索引", st, reason,
                                 {"last_build": proj.get("last_build"), "age_h": age},
                                 hard=(st == "error")))

    tdai = next((s for s in sources if s.get("id") == "tdai"), None)
    if tdai and tdai.get("ok"):
        ev = {"note": tdai.get("note")}
        stale = "stale" in str(tdai.get("note") or "")
        checks.append(_check("l1:tdai", "L1", "TDAI 状态缓存", "warn" if stale else "ok",
                             "状态缓存标记 stale" if stale else "", ev))

    local = next((s for s in sources if s.get("id") == "local"), None)
    if local and local.get("ok"):
        freshen = local.get("fresh_at")
        age_d = _age_hours(freshen + "T00:00:00" if freshen and len(str(freshen)) == 10 else freshen, now)
        age_d = round(age_d / 24.0, 1) if age_d is not None else None
        # 状态取 **info 不是 warn**：这 4 条便签是已被 TDAI 取代的历史样例，
        # 让它把整页永久染黄就是「狼来了」（本机已有 15 天静默停更的教训）。
        # 它仍然必须可见、必须有理由，只是不参与 verdict。
        st = "ok" if (age_d is not None and age_d <= L1_STALE_DAYS) else "info"
        checks.append(_check("l1:local", "L1", "本地 L1 便签", st,
                             "" if st == "ok" else
                             f"最新一条 {freshen or '未知'}（>{L1_STALE_DAYS} 天）；该层已被 TDAI 取代，仅作历史存档",
                             {"count": local.get("count"), "fresh_at": freshen}))

    # L2：语义
    for p in probes:
        checks.append(_check(p["id"], "L2", p.get("name") or p["id"], p["status"],
                             p.get("reason") or "", p.get("evidence") or {},
                             hard=bool(p.get("hard"))))

    # 延迟：只有连续 LATENCY_STRIKES 次超基线 2× 才黄（判例 106）
    for p in probes:
        base = _prev_median_took(prev, p["id"])
        took = (p.get("evidence") or {}).get("took_ms")
        if not base or not isinstance(took, (int, float)):
            continue
        if took > LATENCY_FACTOR * base:
            strikes = 1 + int(_strikes(prev, p["id"], LATENCY_FACTOR))
            st = "warn" if strikes >= LATENCY_STRIKES else "ok"
            checks.append(_check(f"lat:{p['id']}", "L2", f"{p.get('name')} 延迟", st,
                                 "" if st == "ok" else f"{took:.0f}ms > 基线 {base:.0f}ms 的 {LATENCY_FACTOR:g}×（连续 {strikes} 次）",
                                 {"took_ms": took, "baseline_ms": base, "strikes": strikes}))

    # L3：一致性
    if proj.get("ok"):
        rows_total = sum(v.get("chunks", 0) for v in (proj.get("by_source") or {}).values())
        chunks = int(proj.get("chunks") or 0)
        drift = rows_total - chunks
        checks.append(_check("l3:rows", "L3", "投影行数 vs meta.chunks",
                             "ok" if drift == 0 else "error",
                             "" if drift == 0 else f"meta 说 {chunks}，实际 {rows_total}（漂移 {drift:+d}）",
                             {"rows": rows_total, "chunks": chunks}, hard=True))
        prev_src = {}
        if prev:
            prev_src = {s["id"]: s.get("count") for s in (prev.get("sources") or [])
                        if isinstance(s.get("count"), int)}
        for sid, info in (proj.get("by_source") or {}).items():
            cnt = info.get("chunks")
            old = prev_src.get(sid)
            if not isinstance(old, int) or old <= 0 or not isinstance(cnt, int):
                continue
            dev = abs(cnt - old) / old
            if dev > DEVIATION_WARN:
                checks.append(_check(f"l3:dev:{sid}", "L3", f"{sid} 条数突变", "warn",
                                     f"较上次 {old} → {cnt}（{dev * 100:.0f}%）",
                                     {"prev": old, "now": cnt}))

    bad_hard = [c for c in checks if c["status"] == "error" and c["hard"]]
    bad_soft = [c for c in checks if c["status"] == "warn" or (c["status"] == "error" and not c["hard"])]
    notes = [c for c in checks if c["status"] == "info"]
    verdict = "error" if bad_hard else ("warn" if bad_soft else "ok")

    # 上游属性（不是故障）：语义通道无相关性下限。实测同一串 0.77 分返回 3 条。
    rev = next((p for p in probes if p.get("id") == "probe:reverse"), None)
    floor = ((rev or {}).get("evidence") or {}).get("semantic_floor")
    if floor:
        notes.append(_check("note:semantic_floor", "L2", "语义通道无相关性下限", "info",
                            f"不存在的词在 TDAI 语义检索仍返回 {floor['count']} 条（top {floor['top_score']}）"
                            "；属上游行为，故反向探针只量词法通道",
                            {"semantic_floor": floor}))

    # 每源真数回填（fedsources 的 count 不可信，用投影分组数覆盖）
    for s in sources:
        info = (proj.get("by_source") or {}).get(s.get("id") or "")
        if info:
            s["count"] = info.get("chunks")
            s["fresh_at"] = info.get("fresh_at")

    enabled = [s for s in sources if s.get("enabled")]
    rep: Dict[str, Any] = {
        "schema": SCHEMA,
        "as_of": _iso(now),
        "verdict": verdict,
        "summary": {
            "sources_total": len(enabled),
            "sources_ok": sum(1 for s in enabled if s.get("ok")),
            "sources_off": sum(1 for s in sources if not s.get("enabled")),
            "probes_total": len(probes),
            "probes_ok": sum(1 for p in probes if p.get("status") == "ok"),
            "anomalies": len(bad_hard) + len(bad_soft),
            "notes": len(notes),
            "took_ms": took_ms,
        },
        # evidence 一并带出：前端「看证据」要能自证，不能让用户只看到一句结论
        "anomalies": [{"id": c["id"], "layer": c["layer"], "name": c["name"],
                       "status": c["status"], "reason": c["reason"],
                       "evidence": c.get("evidence")}
                      for c in (bad_hard + bad_soft)],
        "notes": [{"id": c["id"], "layer": c["layer"], "name": c["name"],
                   "reason": c["reason"]} for c in notes],
        # 投影快照要进报告：它是下一轮的分组缓存键（db_mtime）。
        # 不落盘 ⇒ 每轮都重扫 2.4GB 表，缓存形同不存在（实测每轮 9–36s）。
        "projection": proj,
        "checks": checks,
        "sources": sources,
        "probes": [{"id": p["id"], "name": p["name"], "status": p["status"],
                    "took_ms": (p.get("evidence") or {}).get("took_ms")} for p in probes],
    }
    # L2：took_ms 必须逐轮落进 history —— 只写 summary 时每轮被下一轮覆盖，
    # 事后无法算 p95（实测落盘 history 的 took_ms 全为 null，判据「稳态 p95<1s」无法自证）。
    entry = {"as_of": rep["as_of"], "verdict": verdict,
             "anomalies": len(rep["anomalies"]), "took_ms": took_ms,
             "probes": rep["probes"]}
    hist = list(prev.get("history") or []) if prev else []
    rep["history"] = (hist + [entry])[-HISTORY_MAX:]
    return rep


def _strikes(prev: Optional[Dict[str, Any]], cid: str, factor: float) -> int:
    """历史上**连续多少次**超基线。

    ⚠ 基线必须用**全长中位数**：若拿"最近 3 次"算基线，连续劣化时基线自己会被抬高，
    阈值跟着上移 ⇒ 永远判不出劣化（第一版实踩，测试 test_repeated_spike_warns 抓出）。
    """
    if not prev:
        return 0
    hist = (prev.get("history") or [])[-20:]
    base = _prev_median_took({"history": hist}, cid)
    if not base:
        return 0
    n = 0
    for h in reversed(hist):
        p = next((x for x in (h.get("probes") or []) if x.get("id") == cid), None)
        if not p or not isinstance(p.get("took_ms"), (int, float)):
            break
        if p["took_ms"] > factor * base:
            n += 1
        else:
            break
    return n


# ── IO ───────────────────────────────────────────────────────────────

def load_prev() -> Optional[Dict[str, Any]]:
    try:
        return json.loads(OUT.read_text(encoding="utf-8"))
    except Exception:  # noqa: BLE001
        return None


def write_report(rep: Dict[str, Any], out: Path = OUT) -> None:
    """原子写：同目录 tmp + os.replace（避免页面读到半截 JSON）。"""
    out.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=str(out.parent), prefix=".memvitals.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as f:
            json.dump(rep, f, ensure_ascii=False, indent=1)
            f.flush()
            os.fsync(f.fileno())
        os.replace(tmp, out)
    finally:
        if os.path.exists(tmp):
            os.unlink(tmp)


def run() -> Dict[str, Any]:
    t0 = time.monotonic()
    prev = load_prev()
    sources = collect_sources()
    proj = collect_projection(prev)
    probes = collect_probes()
    took = round((time.monotonic() - t0) * 1000, 1)
    return build_report(sources, proj, probes, prev, took_ms=took)


def _main(argv: List[str]) -> int:
    rep = run()
    write_report(rep)
    if "--print" in argv or "-p" in argv:
        s = rep["summary"]
        print(f"verdict={rep['verdict']} as_of={rep['as_of']} "
              f"源 {s['sources_ok']}/{s['sources_total']} 探针 {s['probes_ok']}/{s['probes_total']} "
              f"异常 {s['anomalies']} 耗时 {s['took_ms']}ms")
        for a in rep["anomalies"]:
            print(f"  [{a.get('layer')}] {a['name']}: {a['reason']}")
    return 0 if rep["verdict"] != "error" else 2


if __name__ == "__main__":
    sys.exit(_main(sys.argv[1:]))
