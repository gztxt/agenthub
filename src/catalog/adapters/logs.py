"""logs 适配器 —— hub 运行日志扫描（PT-20261025-12 P1）。

扫描 `data/logs/` 下的日志文件，提取时间戳 + 级别 + 消息，分块索引。
仅索引近期日志（30 天内），避免索引过大的历史文件。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of


# 日志正则：匹配 common log format
LOG_PATTERNS = [
    # agent-hub.log 格式: 2026-10-25 12:00:00 | INFO | message
    re.compile(r'^(\d{4}-\d{2}-\d{2}\s+\d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*(.+)$'),
    # restart.log 格式: 2026-10-25T12:00:00 | INFO | message
    re.compile(r'^(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})\s*\|\s*(\w+)\s*\|\s*(.+)$'),
]


def _parse_log_line(line: str) -> Tuple[int, str, str]:
    """解析日志行，返回 (timestamp_int, level, message)。"""
    for pattern in LOG_PATTERNS:
        match = pattern.match(line)
        if match:
            ts_str, level, msg = match.groups()
            try:
                ts_str = ts_str.replace("T", " ")
                ts = int(datetime.fromisoformat(ts_str).timestamp())
                return ts, level.upper(), msg.strip()
            except (ValueError, OSError):
                continue
    return 0, "UNKNOWN", line.strip()


@register
class LogsAdapter(BaseAdapter):
    src_id = "logs.hub"
    domain = "log"
    kind = "file"
    authority = 1  # hub 自有日志即权威
    adapter = "hub_logs"
    scan_mode = "incremental"
    uri = "file://data/logs/**"

    def probe(self) -> Dict[str, Any]:
        logs_dir = Path("/fs/1000/ftp/技术文档/agenthub/data/logs")
        if not logs_dir.exists():
            return {"ok": False, "reason": "no_dir",
                    "error": "logs 目录不存在"}
        total_size = sum(
            p.stat().st_size for p in logs_dir.iterdir() if p.is_file()
        )
        return {"ok": True, "reason": None, "error": None,
                "total_size": total_size}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        logs_dir = Path("/fs/1000/ftp/技术文档/agenthub/data/logs")
        cutoff = int((datetime.now() - timedelta(days=30)).timestamp())

        records: List[Dict[str, Any]] = []
        live: List[str] = []
        notes: List[str] = []

        for filepath in sorted(logs_dir.iterdir()):
            if not filepath.is_file():
                continue
            # 跳过 .heartbeat、.jsonl 等格式
            if filepath.suffix not in (".log", ".txt"):
                continue

            nkey = str(filepath)
            try:
                mtime = int(filepath.stat().st_mtime)
                size = filepath.stat().st_size
                content = filepath.read_text(encoding="utf-8", errors="replace")
            except OSError as e:
                notes.append(f"{filepath}: {e}")
                continue

            # 解析日志行
            lines = content.split('\n')
            parsed_lines = []
            for line in lines:
                if not line.strip():
                    continue
                ts, level, msg = _parse_log_line(line)
                if ts >= cutoff and msg:
                    parsed_lines.append((ts, level, msg))

            if not parsed_lines:
                live.append(_oid(nkey))
                continue

            # 按时间排序并分组
            parsed_lines.sort(key=lambda x: x[0])

            # 生成摘要
            summary_parts = []
            for ts, level, msg in parsed_lines[-50:]:  # 只取最近 50 条
                ts_str = datetime.fromtimestamp(ts).strftime("%H:%M:%S")
                summary_parts.append(f"[{ts_str}] {level}: {msg[:200]}")
            summary = "\n".join(summary_parts[-20:])  # 最多 20 行摘要

            # 分块
            chunks = chunk_text(summary)

            records.append({
                "domain": "log",
                "type": "log",
                "native_key": nkey,
                "uri": f"file://{filepath}",
                "title": filepath.stem[:200],
                "summary": summary,
                "mtime": mtime,
                "size": size,
                "hash": self._hash(nkey, mtime, size, summary),
                "ext": {
                    "unit": filepath.stem,
                    "level": "all",  # 包含所有级别
                    "lines_count": len(parsed_lines),
                    "cutoff_days": 30,
                },
                "meta": {"source": "hub_logs"},
                "chunks": chunks,
            })
            live.append(_oid(nkey))

        return {
            "records": records,
            "live": live,
            "complete": True,
            "note": f"日志 {len(records)} 个文件" +
                    (f"；错误：{','.join(notes)}" if notes else ""),
        }

    @staticmethod
    def _hash(nkey: str, mtime: int, size: int, preview: str) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(f"{nkey}|{mtime}|{size}|{preview}".encode())
        return h.hexdigest()[:16]


def _oid(native_key: str) -> str:
    return oid_of("log", "logs.hub", native_key)
