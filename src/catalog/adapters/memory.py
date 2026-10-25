"""memory 适配器 —— 统一归档记忆读取（PT-20261025-12 P1）。

读取 db.memories 表中 layer='ARCH' 的记录，提取内容 + 分类 + 来源，分块索引。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of


@register
class MemoryAdapter(BaseAdapter):
    src_id = "memory.archive"
    domain = "memory"
    kind = "table"
    authority = 1  # 归档记忆在本库即权威
    adapter = "memory_archive"
    scan_mode = "incremental"
    uri = "table://memories WHERE layer='ARCH'"

    def probe(self) -> Dict[str, Any]:
        try:
            import db
            n = (db.query("SELECT COUNT(*) AS n FROM memories WHERE layer=? AND status='active'",
                          ("ARCH",)) or [{"n": 0}])[0]["n"]
            return {"ok": True, "reason": None, "error": None, "count": n}
        except Exception as e:
            return {"ok": False, "reason": "db_err", "error": str(e)}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        try:
            import db
        except ImportError:
            return {
                "records": [],
                "live": [],
                "complete": True,
                "note": "db 模块不可用",
                "error": "import db failed",
            }

        try:
            rows = db.query(
                "SELECT id, category, content, source, session_id, created_at, updated_at"
                " FROM memories WHERE layer=? AND status='active' ORDER BY id DESC",
                ("ARCH",),
            )
        except Exception as e:
            return {
                "records": [],
                "live": [],
                "complete": True,
                "note": f"查询失败：{e}",
                "error": str(e),
            }

        records: List[Dict[str, Any]] = []
        live: List[str] = []
        notes: List[str] = []

        for row in rows:
            mem_id = str(row["id"])
            nkey = f"memory:{mem_id}"

            content = (row.get("content") or "").strip()
            if not content:
                continue

            try:
                from datetime import datetime
                mtime = int(datetime.fromisoformat(str(row.get("updated_at") or row.get("created_at") or "")).timestamp())
            except (ValueError, TypeError, OSError):
                mtime = None

            size = len(content.encode("utf-8"))
            digest = self._hash(nkey, mtime, size, content[:200])
            if self.skip(states, nkey, mtime, size, digest):
                live.append(_oid(nkey))
                continue

            category = row.get("category") or "fact"
            source = row.get("source") or "unknown"
            chunks = chunk_text(content)

            records.append({
                "domain": "memory",
                "type": "memory",
                "native_key": nkey,
                "uri": f"table://memories:{mem_id}",
                "title": f"[{category}] {content[:80]}",
                "summary": content[:1000],
                "mtime": mtime,
                "size": size,
                "hash": digest,
                "ext": {
                    "category": category,
                    "source": source,
                    "session_id": row.get("session_id"),
                    "layer": "ARCH",
                },
                "meta": {"source": "memory_archive",
                         "mem_id": mem_id},
                "chunks": chunks,
            })
            live.append(_oid(nkey))

        return {
            "records": records,
            "live": live,
            "complete": True,
            "note": f"归档记忆 {len(records)} 条" +
                    (f"；错误：{','.join(notes)}" if notes else ""),
        }

    @staticmethod
    def _hash(nkey: str, mtime: int, size: int, preview: str) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(f"{nkey}|{mtime}|{size}|{preview}".encode())
        return h.hexdigest()[:16]


def _oid(native_key: str) -> str:
    return oid_of("memory", "memory.archive", native_key)
