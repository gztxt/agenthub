"""把当前终端会话的磁盘转录转成 P3 结构化事件（自动关联，不需用户输入）。

用户口径（2026-11-09）：
  · 点「事件」应自动展示**当前会话**的事件，不是另开一轮新对话；
  · 内容始终只对应当前会话，切换会话即换源。

边界：只读转录文件（codebuddy v1）；不改 pty 字节流、不落库、不调用 provider。
"""
from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

import sessions_store

MAX_OUTPUT_CHARS = 4000
LIVE_WINDOW_S = 4.0


def _iso(ms: Any) -> str:
    try:
        return datetime.fromtimestamp(float(ms) / 1000.0, tz=timezone.utc).isoformat(
            timespec="milliseconds")
    except (TypeError, ValueError, OSError):
        return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def _text_of(content: Any) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        out = []
        for x in content:
            if isinstance(x, dict) and x.get("type") in ("input_text", "output_text", "text"):
                t = x.get("text")
                if isinstance(t, str):
                    out.append(t)
        return "".join(out)
    return ""


def _trim(s: str, n: int = MAX_OUTPUT_CHARS) -> str:
    if not isinstance(s, str):
        s = json.dumps(s, ensure_ascii=False, default=str)
    return s if len(s) <= n else s[:n] + "\n…（已截断 %d 字符）" % (len(s) - n)


def _output_text(output: Any) -> str:
    if isinstance(output, str):
        return _trim(output)
    if isinstance(output, dict):
        if isinstance(output.get("text"), str):
            return _trim(output["text"])
        if "output" in output:
            return _trim(output["output"])
    return _trim(output)


def _bucket(cwd: str) -> str:
    try:
        return sessions_store._compress_path(os.path.realpath(cwd or ""))
    except Exception:  # noqa: BLE001
        return ""


def _first_record_ms(path: Path) -> Optional[int]:
    """读前几行取第一条带 timestamp 的记录（用于把新会话与「同 cwd 的旧活跃会话」分开）。"""
    try:
        with path.open("r", encoding="utf-8", errors="ignore") as f:
            for _ in range(8):
                line = f.readline()
                if not line:
                    break
                try:
                    o = json.loads(line)
                except json.JSONDecodeError:
                    continue
                ms = o.get("timestamp")
                if isinstance(ms, (int, float)):
                    return int(ms)
    except OSError:
        pass
    return None


def find_codebuddy_transcript(sid_hint: str, cwd: str, created: float) -> Optional[Path]:
    """定位当前终端会话对应的 codebuddy 转录文件。

    优先 resume_of（精确 id）；否则在该 cwd 的桶里找「会话创建时间之后仍在写」的最新文件。
    找不到返回 None（面板显示「等待会话写入」而不是报错）。
    """
    base = Path.home() / ".codebuddy" / "projects"
    if sid_hint:
        hits = list(base.glob("*/%s.jsonl" % sid_hint))
        if hits:
            return max(hits, key=lambda p: p.stat().st_mtime)
    bucket = _bucket(cwd)
    if not bucket:
        return None
    d = base / bucket
    if not d.is_dir():
        return None
    cands = []
    created_ms = int((created or 0) * 1000)
    for p in d.glob("*.jsonl"):
        try:
            st = p.stat()
        except OSError:
            continue
        if st.st_size <= 0:
            continue
        if st.st_mtime < (created or 0) - 3:
            continue
        first = _first_record_ms(p)
        # 新建会话的首条记录必须不早于会话创建时间（留 5s 时钟误差）；
        # 否则会把「同 cwd 里另一条仍在写的旧会话」误认成当前会话。
        if first is not None and created_ms and first < created_ms - 5000:
            continue
        cands.append((first or int(st.st_mtime * 1000), p))
    if not cands:
        return None
    # 取「创建后最早出现」的那条：同 cwd 后来再开的会话首条时间更大，不该抢当前会话。
    return min(cands, key=lambda x: x[0])[1]


def parse_codebuddy(path: Path, limit: int = 300) -> Dict[str, Any]:
    """逐行解析 codebuddy jsonl → P3 事件 + 会话级汇总（纯函数，L0 可测）。"""
    events: List[Dict[str, Any]] = []
    turn_no = 0
    turn_id = ""
    turn_started_ms = None
    last_model = ""
    tokens = None
    first_ms = None
    last_ms = None
    tool_calls = 0
    messages = 0
    seq = 0
    cur_src = 0

    def emit(typ: str, ts_ms: Any, src: Optional[int] = None, **payload: Any) -> None:
        nonlocal seq
        seq += 1
        ev = {"v": 1, "seq": seq, "ts": _iso(ts_ms), "sid": path.stem,
              "turn_id": turn_id, "agent_id": "codebuddy", "type": typ,
              "_src": cur_src if src is None else src}
        ev.update(payload)
        events.append(ev)

    try:
        lines = path.read_text(encoding="utf-8", errors="ignore").splitlines()
    except OSError:
        lines = []

    for cur_src, line in enumerate(lines, 1):
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except json.JSONDecodeError:
            continue
        typ = o.get("type")
        ms = o.get("timestamp")
        if ms:
            first_ms = first_ms or ms
            last_ms = ms
        if typ == "message":
            role = o.get("role")
            text = _text_of(o.get("content"))
            if role == "user":
                if not text or text.lstrip().startswith("<"):
                    continue
                turn_no += 1
                turn_id = "t%d" % turn_no
                turn_started_ms = ms
                emit("turn_started", ms, message=_trim(text, 2000), turn=turn_no)
            elif role == "assistant" and text:
                pd = o.get("providerData") or {}
                last_model = pd.get("model") or last_model
                u = pd.get("rawUsage") or {}
                if u.get("total_tokens") is not None:
                    tokens = u
                messages += 1
                emit("message_delta", ms, text=_trim(text, 8000))
        elif typ == "function_call":
            tool_calls += 1
            emit("tool_call", ms, name=o.get("name") or "",
                 call_id=o.get("callId") or "", input=o.get("arguments"))
        elif typ == "function_call_result":
            emit("tool_result", ms, name=o.get("name") or "",
                 call_id=o.get("callId") or "", status=o.get("status") or "",
                 output=_output_text(o.get("output")))

    try:
        mtime = path.stat().st_mtime
    except OSError:
        mtime = time.time()
    live = (time.time() - mtime) < LIVE_WINDOW_S
    if events and not live:
        emit("turn_completed", last_ms, src=-(turn_no or 1),
             success=True, ms=int((last_ms or 0) - (turn_started_ms or 0)),
             turns=turn_no)

    total = len(events)
    shown = events[-limit:] if limit and total > limit else events
    shown = [{k: v for k, v in e.items() if k != "_src"} for e in shown]
    return {
        "supported": True,
        "transcript": path.stem,
        "live": live,
        "summary": {
            "turns": turn_no,
            "messages": messages,
            "tool_calls": tool_calls,
            "tokens": (tokens or {}).get("total_tokens"),
            "prompt_tokens": (tokens or {}).get("prompt_tokens"),
            "completion_tokens": (tokens or {}).get("completion_tokens"),
            "model": last_model,
            "duration_ms": int((last_ms or 0) - (first_ms or 0)) if first_ms and last_ms else None,
            "updated": _iso(mtime * 1000),
        },
        "events": shown,
        "total_events": total,
        "truncated": total > len(shown),
        "_all_events": events,
    }


def _event_key(agent_id: str, transcript: str, ev: Dict[str, Any]) -> str:
    return "%s|%s|%s|%s|%s" % (
        agent_id, transcript, ev.get("turn_id") or "", ev.get("type") or "",
        ev.get("_src") if ev.get("_src") is not None else ev.get("seq"))


def store_events(session_id: str, agent_id: str, transcript: str,
                 events: List[Dict[str, Any]]) -> int:
    """append-only 入库；同一事件重复轮询靠 event_key UNIQUE + INSERT OR IGNORE 去重。"""
    if not events:
        return 0
    import db
    now = datetime.now(timezone.utc).isoformat(timespec="seconds")
    rows = []
    for ev in events:
        payload = {k: v for k, v in ev.items() if k != "_src"}
        rows.append((
            _event_key(agent_id, transcript, ev), session_id, agent_id, transcript,
            ev.get("turn_id") or "", ev.get("seq"), ev.get("ts"), ev.get("type") or "",
            json.dumps(payload, ensure_ascii=False, default=str), now,
        ))
    return db.executemany(
        "INSERT OR IGNORE INTO session_events"
        "(event_key,session_id,agent_id,transcript,turn_id,seq,ts,type,payload,created_at) "
        "VALUES(?,?,?,?,?,?,?,?,?,?)", rows)


def stored_events(session_id: Optional[str] = None, agent_id: Optional[str] = None,
                  limit: int = 200) -> List[Dict[str, Any]]:
    """从库里取已留存事件（核查/引用）；过滤条件至少给一个，否则只回最近 limit 条。"""
    import db
    where, params = [], []
    if session_id:
        where.append("session_id=?"); params.append(session_id)
    if agent_id:
        where.append("agent_id=?"); params.append(agent_id)
    sql = "SELECT * FROM session_events"
    if where:
        sql += " WHERE " + " AND ".join(where)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 200), 2000)))
    rows = db.query(sql, tuple(params))
    out = []
    for r in rows:
        try:
            ev = json.loads(r.get("payload") or "{}")
        except json.JSONDecodeError:
            ev = {"type": r.get("type"), "raw": r.get("payload")}
        out.append({"id": r.get("id"), "session_id": r.get("session_id"),
                    "agent_id": r.get("agent_id"), "transcript": r.get("transcript"),
                    "stored_at": r.get("created_at"), "event": ev})
    return out


def session_events(sess, limit: int = 300) -> Dict[str, Any]:
    """按 hub 终端会话取事件；非 codebuddy 先如实标 unsupported（v1）。"""
    agent = getattr(sess, "agent_id", "") or ""
    if agent != "codebuddy":
        return {"supported": False, "agent_id": agent, "events": [],
                "note": "v1 结构化事件暂只接了 codebuddy；该会话仍可在终端查看原始输出。"}
    sid_hint = getattr(sess, "resume_of", "") or ""
    cwd = getattr(sess, "cwd", "") or ""
    created = getattr(sess, "created", 0) or 0
    path = find_codebuddy_transcript(sid_hint, cwd, created)
    if path is None:
        return {"supported": True, "agent_id": agent, "events": [],
                "summary": {}, "note": "当前会话尚未写入转录，稍后自动刷新。"}
    try:
        st = path.stat()
    except OSError:
        return {"supported": True, "agent_id": agent, "events": [], "summary": {}}
    cache_key = (str(path), st.st_mtime_ns, st.st_size, limit)
    cached = getattr(sess, "_ev_cache", None)
    if cached and cached[0] == cache_key:
        return cached[1]
    out = parse_codebuddy(path, limit=limit)
    out["agent_id"] = agent
    out["session_id"] = getattr(sess, "id", "")
    all_events = out.pop("_all_events", [])
    try:
        store_events(out["session_id"], agent, out.get("transcript", ""), all_events)
    except Exception as e:  # noqa: BLE001 —— 入库失败不能打断面板读取
        import logging
        logging.getLogger(__name__).warning("[term_events] 入库失败：%s", e)
    try:
        sess._ev_cache = (cache_key, out)
    except Exception:  # noqa: BLE001
        pass
    return out
