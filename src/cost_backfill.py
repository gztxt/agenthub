"""magpie 账本 → hub 任务成本回填（2026-11-27 互调 P3）。

## 数据链

    magpie `~/.config/magpie/usage.jsonl`（每次调用一行）
        → 按 run 的 task 时间窗 + 实发模型匹配
        → 写回 hub `tasks.credits / ttft_ms / cost_confidence`

## 为什么必须回填

hub 自己的 `spent_tokens` 只是 adapter 回包里那段 usage（实测一次 27 秒的 claude-code
任务只记 152 tokens，而 magpie 账本记的是几万 in/out），它**不能当成本口径**。
成本真源只在 magpie 账本里（含 `credits`，本机免费档也有额度计量）。

## 匹配可信度（不伪装成精确）

| confidence | 条件 |
|---|---|
| `exact`   | 时间窗内**恰好一条**候选，且模型字面相同 |
| `approx`  | 多条候选（同模型并发）或模型靠 `served` 后缀匹配 |
| `none`    | 无候选 —— 写 NULL，不写 0（0 会被读成「零成本」） |

不做「按相似度猜一条」：账本是账单，不是语料。

## 为何不用 per-run gateway key 做精确关联（R1）

`magpie gateway-key issue` 能在协议层给出唯一关联键，但要求每个 run 注入独立凭据、
并改 adapter 的鉴权注入路径 —— 属共享配置变更，本轮先不做；时间窗近似先跑通，
精度不够再按 R1 申请。
"""
from __future__ import annotations

import json
import logging
import os
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

log = logging.getLogger("cost_backfill")

USAGE_PATH = Path(os.getenv("MAGPIE_USAGE_PATH", "~/.config/magpie/usage.jsonl")).expanduser()

#: 时间窗两侧各放宽多少秒（派发开始到真正打网关之间有排队/启动开销）。
WINDOW_PAD_S = float(os.getenv("COST_WINDOW_PAD_S", "15"))

#: 只回填最近 N 天的 run（账本随时间长，全量扫没有收益）。
MAX_DAYS = int(os.getenv("COST_BACKFILL_DAYS", "7"))

_COLS = (
    ("credits", "REAL"),
    ("ttft_ms", "INTEGER"),
    ("cost_confidence", "TEXT"),
    ("cost_matched_at", "TEXT"),
)


def ensure_schema(db) -> None:
    """幂等补列（不动 tasks.py 的建表脚本，存量库逐列 ALTER）。"""
    have = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
    for col, decl in _COLS:
        if col not in have:
            db.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (col, decl))


def _parse_ts(v: Any) -> Optional[datetime]:
    if not v:
        return None
    try:
        d = datetime.fromisoformat(str(v))
    except ValueError:
        return None
    return d if d.tzinfo else d.replace(tzinfo=timezone.utc)


def read_usage(path: Optional[Path] = None,
               since: Optional[datetime] = None,
               until: Optional[datetime] = None) -> Tuple[List[dict], str]:
    """读账本并按时间过滤。返回 (rows, error)。账本不存在 ⇒ ([], 原因) 而不是抛异常。"""
    p = Path(path or USAGE_PATH)
    if not p.is_file():
        return [], "usage.jsonl 不存在：%s" % p
    rows: List[dict] = []
    bad = 0
    try:
        with p.open("r", encoding="utf-8", errors="replace") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    row = json.loads(line)
                except ValueError:
                    bad += 1
                    continue
                t = _parse_ts(row.get("t"))
                if t is None:
                    bad += 1
                    continue
                if since and t < since:
                    continue
                if until and t > until:
                    continue
                row["_t"] = t
                rows.append(row)
    except OSError as e:
        return [], "usage.jsonl 读取失败：%s" % type(e).__name__
    reason = "" if not bad else "跳过 %d 行不可解析" % bad
    return rows, reason


def _model_match(row: dict, task_model: str) -> str:
    """返回 exact / served / none。"""
    tm = (task_model or "").strip()
    if not tm:
        return "none"
    req = str(row.get("req") or "")
    if req == tm:
        return "exact"
    served = str(row.get("served") or "")
    model = str(row.get("model") or "")
    bare = tm.split("/", 1)[1] if "/" in tm else tm
    if served and served == bare:
        return "served"
    if model and model == bare:
        return "served"
    return "none"


def match_task(task: dict, rows: Iterable[dict]) -> Dict[str, Any]:
    """单任务 → 成本字段。`task` 需含 model / started_at / finished_at。"""
    start = _parse_ts(task.get("started_at"))
    end = _parse_ts(task.get("finished_at")) or start
    if start is None:
        return {"cost_confidence": "none", "reason": "无 started_at"}
    pad = timedelta(seconds=WINDOW_PAD_S)
    lo, hi = start - pad, (end or start) + pad
    cands: List[Tuple[str, dict]] = []
    for r in rows:
        t = r.get("_t")
        if t is None or not (lo <= t <= hi):
            continue
        if str(r.get("status")) not in ("200", "0", ""):
            continue
        kind = _model_match(r, task.get("model") or "")
        if kind != "none":
            cands.append((kind, r))
    if not cands:
        return {"cost_confidence": "none", "reason": "时间窗内无同模型调用"}

    credits = 0.0
    ttft = None
    for _kind, r in cands:
        try:
            credits += float(r.get("credits") or 0)
        except (TypeError, ValueError):
            pass
        try:
            v = float(r.get("ttft_ms"))
            ttft = v if ttft is None else min(ttft, v)
        except (TypeError, ValueError):
            pass
    conf = "exact" if len(cands) == 1 and cands[0][0] == "exact" else "approx"
    return {"credits": round(credits, 6), "ttft_ms": int(ttft) if ttft is not None else None,
            "cost_confidence": conf, "matches": len(cands),
            "reason": "" if conf == "exact" else "多条/靠 served 匹配"}


def backfill_run(db, run_id: str, *, now: Optional[datetime] = None,
                 rows: Optional[List[dict]] = None) -> Dict[str, Any]:
    """回填一个 run 下所有已完成任务。返回摘要（不静默失败）。"""
    ensure_schema(db)
    tasks = db.query(
        "SELECT task_id, model, model_planned, started_at, finished_at, cost_confidence"
        " FROM tasks WHERE run_id=? AND status IN ('success','failed')", (run_id,))
    if not tasks:
        return {"run_id": run_id, "updated": 0, "reason": "无可回填任务"}
    if rows is None:
        ts = [_parse_ts(t.get("started_at")) for t in tasks]
        ts = [x for x in ts if x]
        now = now or datetime.now(timezone.utc)
        floor = now - timedelta(days=MAX_DAYS)
        since = min([floor] + ts) if ts else floor
        until = max(ts) + timedelta(seconds=WINDOW_PAD_S) if ts else now
        rows, read_err = read_usage(since=since, until=until)
    else:
        read_err = ""

    out = []
    for t in tasks:
        t = dict(t)
        t["model"] = t.get("model") or t.get("model_planned") or ""
        res = match_task(t, rows)
        if res.get("cost_confidence") == "none":
            out.append({"task_id": t["task_id"], "confidence": "none",
                        "reason": res.get("reason", "")})
            continue
        db.execute(
            "UPDATE tasks SET credits=?, ttft_ms=?, cost_confidence=?, cost_matched_at=?"
            " WHERE run_id=? AND task_id=?",
            (res.get("credits"), res.get("ttft_ms"), res["cost_confidence"],
             datetime.now(timezone.utc).isoformat(timespec="seconds"), run_id, t["task_id"]))
        out.append({"task_id": t["task_id"], "confidence": res["cost_confidence"],
                    "credits": res.get("credits"), "ttft_ms": res.get("ttft_ms"),
                    "matches": res.get("matches", 0)})
    updated = sum(1 for x in out if x["confidence"] != "none")
    return {"run_id": run_id, "updated": updated, "total": len(out),
            "read_error": read_err, "tasks": out}
