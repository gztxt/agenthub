"""tasks 适配器 —— 任务派发记录读取（2026-11-05 P2）。

读取 agents.db 的 tasks 表（hub 任务池派发记录），提取目标/提示/输出，分块索引。
此前 catalog 无 task 适配器模块，x_task 恒为 0 —— 本模块补齐该域。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of


@register
class TasksAdapter(BaseAdapter):
    src_id = "tasks.local"
    domain = "task"
    kind = "table"
    authority = 1  # 任务记录在本库即权威
    adapter = "tasks_local"
    scan_mode = "incremental"
    uri = "table://tasks"

    def probe(self) -> Dict[str, Any]:
        try:
            import db
            from pathlib import Path
            data_dir = Path(__file__).resolve().parents[3] / "data"
            db.init_db(data_dir / "agents.db")
            result = db.query("SELECT COUNT(*) AS n FROM tasks")
            n = (result or [{"n": 0}])[0]["n"]
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
                "SELECT run_id, task_id, goal, prompt, agent_id, status,"
                " output, error, model, verdict, created_at, updated_at"
                " FROM tasks ORDER BY updated_at DESC",
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

        for row in rows:
            run_id = str(row["run_id"] or "")
            task_id = str(row["task_id"] or "")
            nkey = f"task:{run_id}:{task_id}"

            goal = (row.get("goal") or "").strip()
            prompt = (row.get("prompt") or "").strip()
            output = (row.get("output") or "").strip()
            content = "\n".join(p for p in (goal, prompt, output) if p)
            if not content:
                continue

            try:
                from datetime import datetime
                mtime = int(datetime.fromisoformat(
                    str(row.get("updated_at") or row.get("created_at") or "")
                ).timestamp())
            except (ValueError, TypeError, OSError):
                mtime = None

            size = len(content.encode("utf-8"))
            digest = self._hash(nkey, mtime, size, content[:200])
            if self.skip(states, nkey, mtime, size, digest):
                live.append(_oid(nkey))
                continue

            title_base = goal or prompt
            records.append({
                "domain": "task",
                "type": "task",
                "native_key": nkey,
                "uri": f"table://tasks:{run_id}/{task_id}",
                "title": title_base[:80],
                "summary": content[:1000],
                "mtime": mtime,
                "size": size,
                "hash": digest,
                "ext": {
                    "pt_id": run_id,
                    "status": row.get("status") or "pending",
                    "owner": row.get("agent_id") or "",
                },
                "meta": {"source": "tasks_local",
                         "run_id": run_id, "task_id": task_id,
                         "model": row.get("model") or "",
                         "verdict": row.get("verdict") or ""},
                "chunks": chunk_text(content),
            })
            live.append(_oid(nkey))

        return {
            "records": records,
            "live": live,
            "complete": True,
            "note": f"任务记录 {len(records)} 条",
        }

    @staticmethod
    def _hash(nkey: str, mtime: int, size: int, preview: str) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(f"{nkey}|{mtime}|{size}|{preview}".encode())
        return h.hexdigest()[:16]


def _oid(native_key: str) -> str:
    return oid_of("task", "tasks.local", native_key)
