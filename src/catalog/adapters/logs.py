"""logs 适配器 —— hub 运行日志扫描（PT-20261025-12 P1 / 2026-11-04 重写）。

扫描 `data/logs/` 下的日志文件，**以文件为对象**（一个文件一条记录），
索引尾部窗口（默认 200 行）作为正文。仅看 30 天内修改过的文件。

⚠ 2026-11-04 修：旧实现只认 `2026-10-25 12:00:00 | INFO | msg` 与
`2026-10-25T12:00:00 | INFO | msg` 两种格式，而生产实际是 uvicorn 的
`INFO:     Started server process [85512]`、`[Agent Hub] 启动完成 …`
以及 JSONL（`{"ts": …}`）⇒ 全部行解析失败被过滤 ⇒ **该源上线 10 天产出 0 条**，
`status` 里却一直显示 ok（`probe` 只查目录存在）。现在改成「格式无关」：
级别用宽松正则提取，正文直接取尾部窗口，不再要求任何时间戳格式。
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of

LOGS_DIR = Path("/fs/1000/ftp/技术文档/agenthub/data/logs")

#: 只收这几种后缀；`.heartbeat` / `.gz` / 无后缀一律跳过。
SUFFIXES = (".log", ".txt", ".jsonl")

#: 宽松级别提取：uvicorn `INFO: …`、管道格式 `| ERROR |`、JSONL `"level": "warn"`。
LEVEL_RE = re.compile(
    r'\b(DEBUG|INFO|WARNING|WARN|ERROR|CRITICAL|FATAL)\b', re.IGNORECASE)

#: 尾部窗口：一个日志文件只索引最近这么多行（控制库体，历史靠文件自身留存）。
TAIL_LINES = 200
#: 尾部读取上限：先读最后 256KB，再取行 —— 避免把 1GB 日志整篇读进内存。
TAIL_BYTES = 256 * 1024
#: 摘要上限（进 chunk 的字符数）。
SUMMARY_CHARS = 6000


def _tail_text(path: Path) -> str:
    """读文件尾部窗口；小于阈值就整篇读。"""
    try:
        size = path.stat().st_size
        with path.open("rb") as fh:
            if size > TAIL_BYTES:
                fh.seek(-TAIL_BYTES, 2)
                raw = fh.read()
                # 丢掉被截断的首行
                nl = raw.find(b"\n")
                raw = raw[nl + 1:] if nl >= 0 else raw
            else:
                raw = fh.read()
    except OSError:
        return ""
    return raw.decode("utf-8", errors="replace")


def _summarize(text: str) -> Tuple[str, Dict[str, int], int]:
    """返回 (尾部摘要, 级别计数, 总行数)。"""
    lines = [ln for ln in text.splitlines() if ln.strip()]
    levels: Dict[str, int] = {}
    for ln in lines:
        m = LEVEL_RE.search(ln)
        if m:
            lv = m.group(1).upper().replace("WARN", "WARNING")
            levels[lv] = levels.get(lv, 0) + 1
    tail = lines[-TAIL_LINES:]
    summary = "\n".join(tail)
    if len(summary) > SUMMARY_CHARS:
        summary = summary[-SUMMARY_CHARS:]
    return summary, levels, len(lines)


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
        if not LOGS_DIR.exists():
            return {"ok": False, "reason": "no_dir", "error": "logs 目录不存在"}
        files = [p for p in LOGS_DIR.iterdir()
                 if p.is_file() and p.suffix in SUFFIXES]
        return {"ok": True, "reason": None, "error": None,
                "files": len(files)}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        cutoff = int((datetime.now() - timedelta(days=30)).timestamp())
        records: List[Dict[str, Any]] = []
        live: List[str] = []
        notes: List[str] = []
        skipped_old = 0

        for filepath in sorted(LOGS_DIR.iterdir()):
            if not filepath.is_file() or filepath.suffix not in SUFFIXES:
                continue
            nkey = str(filepath)
            try:
                st = filepath.stat()
            except OSError as e:
                notes.append(f"{filepath.name}: {e}")
                continue
            mtime, size = int(st.st_mtime), st.st_size

            # 30 天没动过的日志不再收录（历史仍在盘上，不属于「近期可检索面」）
            if mtime < cutoff:
                skipped_old += 1
                live.append(_oid(nkey))
                continue

            digest = self._hash(nkey, mtime, size)
            if self.skip(states, nkey, mtime, size, digest):
                live.append(_oid(nkey))
                continue

            summary, levels, nlines = _summarize(_tail_text(filepath))
            if not summary:
                # 空文件：登记但正文为空（harness 自愈时会补 chunk 头）
                summary = f"{filepath.name}（空/不可读）"

            records.append({
                "domain": "log",
                "type": "log",
                "native_key": nkey,
                "uri": f"file://{filepath}",
                "title": filepath.stem[:200],
                "summary": summary,
                "mtime": mtime,
                "size": size,
                "hash": digest,
                "ext": {
                    "unit": filepath.stem,
                    "level": max(levels, key=levels.get) if levels else "all",
                    "levels": levels,
                    "lines_count": nlines,
                    "tail_lines": min(nlines, TAIL_LINES),
                    "cutoff_days": 30,
                },
                "meta": {"source": "hub_logs"},
                "chunks": chunk_text(summary),
            })
            live.append(_oid(nkey))

        return {
            "records": records,
            "live": live,
            "complete": True,
            "note": f"日志 {len(records)} 个文件（超 30 天跳过 {skipped_old}）"
                    + (f"；错误：{','.join(notes)}" if notes else ""),
        }

    @staticmethod
    def _hash(nkey: str, mtime: int, size: int) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(f"{nkey}|{mtime}|{size}".encode())
        return h.hexdigest()[:16]


def _oid(native_key: str) -> str:
    return oid_of("log", "logs.hub", native_key)
