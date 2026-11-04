"""sessions 适配器 —— 归档会话备份扫描（PT-20261025-12 P1）。

扫描 `会话备份/` 下的两类文件：
- `*.md`：已提炼的会话摘要（前 50 行正文入库）；
- `*.jsonl`：各 agent 的**原始会话流水**（2026-11-04 补，1,602 个 / 1.2GB）——
  只取首条用户消息 + 末条助手消息做摘要，不整篇分块，避免库体膨胀。

每个文件一条对象，按 (mtime,size) 增量判重。
"""
from __future__ import annotations

import json
import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of

#: jsonl 首尾窗口：只读头尾各 96KB，绝不整篇读（单文件可达几十 MB）。
JSONL_WINDOW = 96 * 1024
#: jsonl 摘要上限（进 chunk 的字符数）。
JSONL_SUMMARY_CHARS = 2500

#: 会话备份下的一级目录 = 工具链名。
AGENT_DIRS = ("claude", "codebuddy", "pi", "codex", "grok", "jcode",
              "qwenpaw", "hermes", "operit")


def _deep_text(node: Any, limit: int = 1200) -> str:
    """从任意 JSON 结构里挖出可读正文（content 可能是 str / list / dict）。"""
    out: List[str] = []

    def walk(v: Any) -> None:
        if len("".join(out)) > limit:
            return
        if isinstance(v, str):
            out.append(v)
        elif isinstance(v, list):
            for it in v:
                walk(it)
        elif isinstance(v, dict):
            for key in ("text", "content", "message", "output",
                        "command", "thinking"):
                if key in v:
                    walk(v[key])

    walk(node)
    text = " ".join(x for x in out if x.strip())
    return re.sub(r"\s+", " ", text).strip()[:limit]


def _jsonl_line_text(line: str) -> str:
    try:
        obj = json.loads(line)
    except (ValueError, TypeError):
        return line.strip()[:1200]
    if isinstance(obj, dict):
        if obj.get("role") in ("user", "assistant"):
            return _deep_text(obj)
        # pi: {"type":"message","message":{"role":…,"content":…}}
        msg = obj.get("message")
        if isinstance(msg, dict) and msg.get("role") in ("user", "assistant"):
            return _deep_text(msg)
        if obj.get("type") in ("message", "user", "assistant"):
            return _deep_text(obj)
    return ""


def _jsonl_head_tail(path: Path) -> Tuple[str, str, str]:
    """返回 (首条用户消息, 末条助手消息, cwd)。只读头尾窗口。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            head = fh.read(JSONL_WINDOW).decode("utf-8", errors="replace")
            if size > JSONL_WINDOW:
                fh.seek(max(0, size - JSONL_WINDOW))
                tail = fh.read().decode("utf-8", errors="replace")
            else:
                tail = head
    except OSError:
        return "", "", ""

    first_user = ""
    cwd = ""
    for line in head.splitlines():
        if not line.strip():
            continue
        if not cwd:
            m = re.search(r'"cwd"\s*:\s*"([^"]+)"', line)
            if m:
                cwd = m.group(1)
        if not first_user and '"user"' in line:
            text = _jsonl_line_text(line)
            if text:
                first_user = text[:1200]
        if first_user and cwd:
            break

    last_assistant = ""
    for line in reversed(tail.splitlines()):
        if not line.strip():
            continue
        if '"assistant"' in line or '"role":"assistant"' in line:
            text = _jsonl_line_text(line)
            if text:
                last_assistant = text[-1200:]
                break
    return first_user, last_assistant, cwd


def _jsonl_record(filepath: Path, mtime: int, size: int,
                  rel_agent: str, digest: str) -> Dict[str, Any]:
    first_user, last_assistant, cwd = _jsonl_head_tail(filepath)
    parts = []
    if first_user:
        parts.append("用户：" + first_user)
    if last_assistant:
        parts.append("助手：" + last_assistant)
    summary = "\n".join(parts)[:JSONL_SUMMARY_CHARS] or f"{filepath.name}（空会话）"

    sid = ""
    m = re.search(r'([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})',
                  filepath.name)
    if m:
        sid = m.group(1)
    started_at = None
    m = re.search(r'(\d{4}-\d{2}-\d{2})[T_](\d{2})[-:](\d{2})[-:](\d{2})',
                  filepath.name)
    if m:
        try:
            started_at = int(datetime.fromisoformat(
                f"{m.group(1)}T{m.group(2)}:{m.group(3)}:{m.group(4)}").timestamp())
        except ValueError:
            pass

    return {
        "domain": "session",
        "type": "session",
        "native_key": str(filepath),
        "uri": f"file://{filepath}",
        "title": filepath.stem[:200],
        "summary": summary,
        "mtime": mtime,
        "size": size,
        "hash": digest,
        "ext": {
            "session_id": sid,
            "agents": [rel_agent] if rel_agent else [],
            "model": "",
            "msgs": 0,          # 不整篇读，行数留空（需要时看 mtime/size）
            "started_at": started_at,
            "cwd": cwd,
            "format": "jsonl",
            "filepath": str(filepath),
        },
        "meta": {"source": "archived_sessions", "format": "jsonl",
                 "agent_count": 1 if rel_agent else 0},
        "chunks": chunk_text(summary),
    }


def _extract_session_meta(content: str, filepath: str) -> Dict[str, Any]:
    """从会话备份文件内容提取关键元数据。"""
    # 提取会话 ID
    session_id_match = re.search(r'`([0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12})`', content[:2000])
    session_id = session_id_match.group(1) if session_id_match else ""

    # 提取 agent/客户端
    agents = []
    for pattern in [r'claude', r'codebuddy', r'pi', r'codex', r'grok', r'jcode', r'qwenpaw']:
        if re.search(rf'\b{pattern}\b', content[:2000], re.IGNORECASE):
            agents.append(pattern)

    # 提取模型
    model_match = re.search(r'模型\s*\|\s*`([^`]+)`', content[:500])
    model = model_match.group(1).strip() if model_match else ""

    # 提取消息数
    msgs_match = re.search(r'消息数\s*\|\s*(\d+)', content[:500])
    msgs = int(msgs_match.group(1)) if msgs_match else 0

    # 提取时间戳
    ts_match = re.search(r'(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})', content[:300])
    started_at = None
    if ts_match:
        try:
            started_at = int(datetime.fromisoformat(ts_match.group(1)).timestamp())
        except (ValueError, OSError):
            pass

    return {
        "session_id": session_id,
        "agents": agents,
        "model": model,
        "msgs": msgs,
        "started_at": started_at,
    }


@register
class SessionsAdapter(BaseAdapter):
    src_id = "sessions.archived"
    domain = "session"
    kind = "file"
    authority = 0  # 会话备份是派生数据
    adapter = "archived_sessions"
    scan_mode = "incremental"
    uri = "file://会话备份/**"

    def probe(self) -> Dict[str, Any]:
        base = Path("/fs/1000/ftp/技术文档")
        sessions_dir = base / "会话备份"
        if not sessions_dir.exists():
            return {"ok": False, "reason": "no_dir",
                    "error": "会话备份目录不存在"}
        md = sum(1 for _ in sessions_dir.rglob("*.md"))
        jsonl = sum(1 for _ in sessions_dir.rglob("*.jsonl"))
        return {"ok": True, "reason": None, "error": None,
                "md": md, "jsonl": jsonl, "count": md + jsonl}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        base = Path("/fs/1000/ftp/技术文档")
        sessions_dir = base / "会话备份"

        records: List[Dict[str, Any]] = []
        live: List[str] = []
        complete = True
        notes: List[str] = []

        for filepath in sessions_dir.rglob("*.md"):
            try:
                mtime = int(filepath.stat().st_mtime)
            except OSError:
                continue

            nkey = str(filepath)

            # 读取内容（仅前 N 行用于摘要）
            try:
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except Exception as e:
                complete = False
                notes.append(f"{filepath}: {e}")
                continue

            # 增量判重
            size = filepath.stat().st_size
            digest = self._hash_for_skip(nkey, mtime, size, content[:1000])
            if self.skip(states, nkey, mtime, size, digest):
                live.append(_oid(nkey))
                continue

            # 提取元数据
            meta = _extract_session_meta(content, str(filepath))

            # 生成摘要（前 2000 字符）
            summary_lines = []
            for line in content.split('\n')[:50]:
                if len('\n'.join(summary_lines)) >= 2000:
                    break
                summary_lines.append(line)
            summary = '\n'.join(summary_lines).strip()

            # 分块索引
            chunks = chunk_text(summary)

            records.append({
                "domain": "session",
                "type": "session",
                "native_key": nkey,
                "uri": f"file://{filepath}",
                "title": filepath.stem[:200],
                "summary": summary[:1000],
                "mtime": mtime,
                "size": size,
                "hash": digest,
                "ext": {
                    "session_id": meta["session_id"],
                    "agents": meta["agents"],
                    "model": meta["model"],
                    "msgs": meta["msgs"],
                    "started_at": meta["started_at"],
                    "filepath": str(filepath),
                },
                "meta": {"source": "archived_sessions",
                         "agent_count": len(meta["agents"])},
                "chunks": chunks,
            })
            live.append(_oid(nkey))

        # ── 原始会话流水（jsonl）──
        n_jsonl = 0
        for filepath in sessions_dir.rglob("*.jsonl"):
            rel = filepath.relative_to(sessions_dir).parts
            if rel and rel[0] == "deleted-sessions":
                continue          # 已删除会话的隔离区，不进检索面
            try:
                st = filepath.stat()
            except OSError:
                continue
            mtime, size = int(st.st_mtime), st.st_size
            nkey = str(filepath)
            digest = self._hash_for_skip(nkey, mtime, size, "")
            if self.skip(states, nkey, mtime, size, digest):
                live.append(_oid(nkey))
                continue
            agent = rel[0] if rel and rel[0] in AGENT_DIRS else (
                rel[0] if rel else "")
            records.append(_jsonl_record(filepath, mtime, size, agent, digest))
            live.append(_oid(nkey))
            n_jsonl += 1

        return {
            "records": records,
            "live": live,
            "complete": complete,
            "note": f"会话 {len(records)} 条（含原始流水 {n_jsonl} 个）" +
                    (f"；部分失败：{','.join(notes)}" if notes else ""),
        }

    @staticmethod
    def _hash_for_skip(nkey: str, mtime: int, size: int,
                       preview: str) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(f"{nkey}|{mtime}|{size}|{preview}".encode())
        return h.hexdigest()[:16]


def _oid(native_key: str) -> str:
    return oid_of("session", "sessions.archived", native_key)
