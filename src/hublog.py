"""Hub 日志中心（v0.13.46，v0.13.47 并入运行日志）：设置→日志子菜单的数据面。

用户诉求是「全面收集 agent hub 的操作日志和错误日志」，而这两类日志**压根不在
同一个地方**：

  ① **操作日志** = `profile_events` 表（hub_chat / task_exec / cron_run / mcp_call /
     rest / hubself_* / manager_*）。其中 `source='rest'` 是三中心检索留痕（记忆 /
     知识 / 技能 / cc.start），带耗时、通道、降级路、查询词。
  ② **错误日志** = 进程的 stdout/stderr。hub 自己**不落文件**（systemd 的
     StandardOutput=journal），Traceback / uvicorn 5xx / [writegate] 401 全在
     journald 里 —— 不看 journald 就永远看不到"到底报了什么错"。

v0.13.47：系统菜单的「运行日志」页**删除**，它挑 `source='rest'` 的那一份内容并
入本端点 —— `source=rest` 视图 + `subject` 过滤（subject 枚举照抄 runlog.SUBJECTS，
非法值 400）。运行日志页的翻页游标（before_id）不迁：本页 limit 上限 500，一次
拉取足够，游标留着只会让 UI 多一个「下一页」状态要维护。埋点与
`GET /api/runlog` 门面本身保留（7 个端点的 track() 装饰器和外部查询都靠它），
变的只是**唯一 UI 入口**。

设计口径（与 runlog.py 同源，别各写一套）：
- **零新表**：journald 现拉（只读 journalctl，不写任何东西），事件走既有表。
- **subprocess 无 shell**：`-o short-iso` 解析，**用户输入绝不进命令行**（关键字
  过滤在 Python 里做，不喂 `--grep`），从根上断掉命令注入。
- **失败是数据不是异常**：journalctl 不存在 / 非 systemd 运行 / 超时 ⇒ 回
  `sources[].ok=false` + note，另一路照常出，绝不整页 500。
- **鉴权按写方法判**（照抄 /api/runlog 与 /api/audit/list）：日志里有 IP、路径、
  查询词，批量读它＝窥探本机使用史。fail-closed ⇒ 服务端没配口令就 503。

日志条目的统一形状（前端只认这一个）：
    {ts, epoch, src: 'journal'|'event', level: 'info'|'warn'|'error', tag, msg}
"""
from __future__ import annotations

import json
import logging
import os
import re
import subprocess
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from fastapi.responses import PlainTextResponse

import db
import runlog           # 只为复用 SUBJECTS 枚举（三中心 subject 的唯一权威定义）
import writeauth

log = logging.getLogger(__name__)

router = APIRouter()

#: 日志单元。手跑 uvicorn（不经过 systemd）时 journald 里没有东西 —— 这时
#: 端点的 sources 里会把原因讲明白，而不是让用户对着空列表猜。
#:
#: 2026-10-08 随 unit 改名 `agent-hub.service` → `agenthub.service`（仓名/远端/unit
#: 三处一次改齐）。改名的唯一承重耦合点就是这一行：漏改的症状是「设置→日志页
#: 空白但 /health 正常」，因为 `journalctl -u <不存在的单元>` 静默返空、不报错。
#: 任何时候动了 systemd unit 名，这行必须同批改；改完用
#: `journalctl --user -u agenthub.service -n 3` 取证，别只看端点返不返 200。
UNIT_ENV = "HUB_LOG_UNIT"
DEFAULT_UNIT = "agenthub.service"

_JOURNAL_TIMEOUT_S = 6          # 拉 journald 的硬超时（日志页不许把请求挂住）
_JOURNAL_MAX_LINES = 3000       # 原始行上限（先拉后滤，过滤前不能只看 limit 条）
_MSG_MAX_CHARS = 500            # 单行落响应上限（防一条巨长 traceback 撑爆响应）

#: `2026-09-27T16:44:40+0800 zzst agenthub[423114]: INFO:     192.168.5.99 - "GET /" 200`
#: （旧单元名下 SyslogIdentifier 是 `agent-hub`；2026-10-08 起为 `agenthub`）
_J_RE = re.compile(
    r"^(?P<ts>\d{4}-\d{2}-\d{2}T\S+)\s+(?P<host>\S+)\s+"
    r"(?P<tag>[^\[:]+?)(?:\[(?P<pid>\d+)\])?:\s?(?P<msg>.*)$")

#: 级别判定：先认 uvicorn 的行首前缀（最准），再退到关键字。
#: 4xx 里只有门禁类（401/403/409）算 warn —— 普通 404 是噪声，别把日志染黄。
_ERR_RE = re.compile(
    r"Traceback \(most recent|\bCRITICAL\b|\bFATAL\b|\bALERT\b|"
    r"Exception in ASGI|\bHTTP/1\.1\" 5\d\d")
_WARN_RE = re.compile(r"\bWARNING\b|\bWARN\b|\[writegate\]|\" 4(01|03|09|22|29) ")


def _unit() -> str:
    return (os.getenv(UNIT_ENV) or DEFAULT_UNIT).strip() or DEFAULT_UNIT


def _level_of(msg: str) -> str:
    head = msg[:12]
    if head.startswith("ERROR:") or head.startswith("CRITICAL:"):
        return "error"
    if head.startswith("WARNING:"):
        return "warn"
    if _ERR_RE.search(msg):
        return "error"
    if _WARN_RE.search(msg):
        return "warn"
    return "info"


def _epoch(ts: str) -> float:
    """ISO 串 → epoch，用于跨来源统一排序（journald 是 +0800、DB 是 UTC，
       直接比字符串会排错）。解析不出来给 0（排在最后，绝不抛）。"""
    try:
        return datetime.fromisoformat(ts).timestamp()
    except Exception:  # noqa: BLE001 —— 时间戳形状漂移不该把日志页打挂
        return 0.0


def _cutoff_iso(window_h: int) -> Optional[str]:
    """DB 侧的时间下界（created_at 存的是 UTC）。"""
    if not window_h or window_h <= 0:
        return None
    return (datetime.now(timezone.utc) - timedelta(hours=window_h)).isoformat()


def _journal_since(window_h: int) -> Optional[str]:
    """journald 侧的时间下界。**按本机时区**算 —— `--since` 收的是本地时间字符串，
       拿 UTC 时刻喂进去会整整偏一个时区（实测：UTC 下界 ⇒ 窗口宽了 8 小时）。"""
    if not window_h or window_h <= 0:
        return None
    return (datetime.now().astimezone() - timedelta(hours=window_h)).strftime("%Y-%m-%d %H:%M:%S")


# ---------------------------------------------------------------- journald
def _journal_env() -> Dict[str, str]:
    """journalctl --user 认 XDG_RUNTIME_DIR；手跑（nohup/setsid 起的探针实例）
       常常没这个变量 ⇒ 补成 /run/user/<uid>，否则"日志页空白"会被误判成后端坏。"""
    env = dict(os.environ)
    env.setdefault("XDG_RUNTIME_DIR", f"/run/user/{os.getuid()}")
    return env


def _journal_lines(window_h: int, max_lines: int) -> Tuple[List[str], str]:
    """返回 (原始行[新→旧], 说明)。失败一律返回 ([], 可读原因) —— 不抛。

    两个实测坑（别"顺手简化"掉）：
      · `--since` **必须配 `-r`**：带 `--since` 时 journalctl 从窗口起点**正序**读，
        此时 `-n` 截的是窗口里**最旧**的 N 条（09-27 实测：不带 -r 拿到的是 24h 前
        那 50 条，最新的全丢了）。`-r` 才是"取最新 N 条"。
      · 时间下界用**本机时区**串（`_journal_since`），不喂 UTC。
    """
    cmd = ["journalctl", "--user", "-u", _unit(), "--no-pager",
           "-o", "short-iso", "-r", "-n", str(max_lines)]
    cut = _journal_since(window_h)
    if cut:
        cmd += ["--since", cut]
    try:
        p = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=_JOURNAL_TIMEOUT_S, env=_journal_env())
    except FileNotFoundError:
        return [], "journalctl 不存在（非 systemd 环境）"
    except subprocess.TimeoutExpired:
        return [], f"journalctl 超时（>{_JOURNAL_TIMEOUT_S}s），日志量可能过大"
    except Exception as e:  # noqa: BLE001
        return [], f"journalctl 调用失败：{type(e).__name__}"
    if p.returncode != 0:
        note = (p.stderr or "").strip().splitlines()
        return [], (note[-1] if note else f"journalctl 退出码 {p.returncode}")
    return (p.stdout or "").splitlines(), ""


def _journal_entries(window_h: int, max_lines: int) -> Tuple[List[Dict[str, Any]], str]:
    lines, note = _journal_lines(window_h, max_lines)
    if not note and not lines:
        note = f"窗口内无条目（单元 {_unit()}；手跑实例不经 systemd，日志不在 journald 里）"
    # journalctl 无论正序还是 `-r`，**每条消息都是头行在前、续行紧随其后**，
    # 所以"续行并入上一条"两种顺序都成立 ⇒ 不翻数组，少一次反转也少一处会错的地方。
    out: List[Dict[str, Any]] = []
    for ln in lines:
        m = _J_RE.match(ln)
        if not m:
            # 多行 traceback 的续行：并入上一条，别切成"没有时间戳的孤儿行"
            if out:
                out[-1]["msg"] += "\n" + ln.strip()
            continue
        msg = m.group("msg") or ""
        out.append({"ts": m.group("ts"), "epoch": _epoch(m.group("ts")),
                    "src": "journal", "level": _level_of(msg),
                    "tag": (m.group("tag") or "").strip(), "msg": msg})
    return out, note


# ------------------------------------------------------------ 操作事件(DB)
def _event_msg(row: Dict[str, Any]) -> str:
    subj = row.get("subject") or row.get("source") or "event"
    st = row.get("status") or ""
    bits = [f"{subj} {st}".strip()]
    if row.get("duration_ms") is not None:
        bits.append(f"{row['duration_ms']}ms")
    try:
        d = json.loads(row.get("detail") or "{}") or {}
    except Exception:  # noqa: BLE001 —— detail 形状漂移就当没有附加信息
        d = {}
    for k in ("q", "err", "agent", "channel", "routes"):
        v = d.get(k)
        if v:
            bits.append(f"{k}={v}")
    if d.get("degraded"):
        bits.append("degraded=" + ",".join(str(x) for x in d["degraded"])[:60])
    if d.get("count") is not None:
        bits.append(f"count={d['count']}")
    return " · ".join(str(b) for b in bits if b)


def _event_entries(window_h: int, limit: int, subject: str = "",
                   only_rest: bool = False) -> Tuple[List[Dict[str, Any]], str]:
    """事件路取数。`only_rest` = 只要三中心检索留痕（原「运行日志」页的那一份）。"""
    sql = "SELECT id,source,subject,trace_id,status,duration_ms,detail,created_at FROM profile_events"
    conds: List[str] = []
    params: List[Any] = []
    cut = _cutoff_iso(window_h)
    if cut:
        conds.append("created_at>=?")
        params.append(cut)
    if only_rest:
        conds.append("source=?")
        params.append(runlog.SOURCE)
    if subject:
        conds.append("subject=?")
        params.append(subject)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    try:
        rows = db.query(sql, tuple(params))
    except Exception as e:  # noqa: BLE001
        return [], f"事件表读取失败：{type(e).__name__}: {e}"
    out = []
    for r in rows:
        st = (r.get("status") or "").lower()
        lvl = "error" if st in ("fail", "error", "failed") else "info"
        if lvl == "info":
            try:
                if (json.loads(r.get("detail") or "{}") or {}).get("degraded"):
                    lvl = "warn"
            except Exception:  # noqa: BLE001
                pass
        ts = r.get("created_at") or ""
        out.append({"ts": ts, "epoch": _epoch(ts), "src": "event",
                    "level": lvl, "tag": r.get("source") or "", "msg": _event_msg(r)})
    return out, ""


# ------------------------------------------------------------------- 端点
@router.get("/api/hublog")
async def hublog_query(request: Request,
                       source: str = Query(default="all", max_length=16),
                       level: str = Query(default="all", max_length=8),
                       q: str = Query(default="", max_length=120),
                       window: int = Query(default=24, ge=0, le=720),
                       limit: int = Query(default=200, ge=1, le=500),
                       subject: str = Query(default="", max_length=40),
                       format: str = Query(default="json", max_length=4)):
    """日志中心查询：journald 服务日志 + profile_events 操作事件，合并按时间倒序。

    `source=rest` = 原「运行日志」页那一份（三中心检索留痕）；`subject` 按
    runlog.SUBJECTS 校验（非法 400，与 /api/runlog 同口径）。

    GET 但**按写方法鉴权**（与 /api/runlog 同口径）：日志含 IP/路径/查询词。
    """
    verdict, reason = writeauth.decide(
        "POST", request.url.path,
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        log.warning(f"[hublog] 拒绝 {verdict}：{request.url.path} "
                    f"来源={request.client.host if request.client else '?'} —— {reason}")
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401, detail=reason)

    if source not in ("all", "journal", "event", "error", "rest"):
        raise HTTPException(400, "source must be all|journal|event|error|rest")
    if level not in ("all", "info", "warn", "error"):
        raise HTTPException(400, "level must be all|info|warn|error")
    if subject and subject not in runlog.SUBJECTS:
        raise HTTPException(400, f"subject must be one of {list(runlog.SUBJECTS)}")

    jr_entries, jr_note = ([], "本视图未取 journald") if source in ("event", "rest") \
        else _journal_entries(window, _JOURNAL_MAX_LINES)
    ev_entries, ev_note = ([], "本视图未取事件表") if source == "journal" \
        else _event_entries(window, max(limit * 2, 200), subject=subject or "",
                            only_rest=(source == "rest"))

    entries = jr_entries + ev_entries
    if source == "rest":
        entries = [e for e in entries if e["src"] == "event" and e["tag"] == runlog.SOURCE]
    if source == "error" or level == "error":
        entries = [e for e in entries if e["level"] == "error"]
    elif level == "warn":
        entries = [e for e in entries if e["level"] in ("warn", "error")]

    kw = (q or "").strip().lower()[:120]
    if kw:
        entries = [e for e in entries if kw in (e["msg"] or "").lower()
                   or kw in (e["tag"] or "").lower()]

    entries.sort(key=lambda e: e["epoch"], reverse=True)
    truncated = len(entries) > limit
    entries = entries[:limit]
    for e in entries:
        e["msg"] = (e["msg"] or "")[:_MSG_MAX_CHARS]

    stats = {"total": len(entries), "error": 0, "warn": 0, "info": 0,
             "journal": 0, "event": 0}
    for e in entries:
        stats[e["level"]] = stats.get(e["level"], 0) + 1
        stats[e["src"]] = stats.get(e["src"], 0) + 1

    sources = [
        {"id": "journal", "label": f"服务日志（journald · {_unit()}）",
         "ok": not jr_note, "note": jr_note or "含 uvicorn 访问行、print 埋点、Traceback",
         "count": sum(1 for e in entries if e["src"] == "journal")},
        {"id": "event", "label": "操作事件（profile_events 全量 source）",
         "ok": not ev_note, "note": ev_note or "对话/任务/定时/MCP/自建工具调用留痕",
         "count": sum(1 for e in entries if e["src"] == "event")},
    ]

    if format == "text":
        body = "\n".join(
            f"{e['ts']} [{e['level'][:4].upper()}] {e['src']}:{e['tag']} {e['msg']}"
            for e in entries)
        return PlainTextResponse(
            body, media_type="text/plain; charset=utf-8",
            headers={"Content-Disposition": 'attachment; filename="agenthub.log"'})

    return {"entries": entries, "count": len(entries), "stats": stats,
            "sources": sources, "truncated": truncated,
            "subjects": list(runlog.SUBJECTS),
            "query": {"source": source, "level": level, "q": q,
                      "window": window, "limit": limit, "subject": subject}}
