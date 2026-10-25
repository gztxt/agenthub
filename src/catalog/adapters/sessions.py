"""sessions 适配器 —— 归档会话备份扫描（PT-20261025-12 P1）。

扫描 `会话备份/` 目录下的所有 md 文件，提取元数据 + 前 N 行正文作为摘要。
每个文件一条对象，按文件 mtime 做增量判重。
"""
from __future__ import annotations

import os
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of


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
        count = sum(1 for _ in sessions_dir.rglob("*.md"))
        return {"ok": True, "reason": None, "error": None, "count": count}

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

        return {
            "records": records,
            "live": live,
            "complete": complete,
            "note": f"会话 {len(records)} 条" +
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
