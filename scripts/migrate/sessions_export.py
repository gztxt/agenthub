#!/usr/bin/env python3
"""Export AgentHub's own chat sessions as a read-only JSONL archive.

These are hub-recorded conversations (mostly task/e2e sessions), not the
agents' native transcripts magpie reads. They are kept for provenance and
download; they are not written into magpie's session files, which would make
fake native sessions.

Usage: python3 scripts/migrate/sessions_export.py [out.jsonl]
"""
from __future__ import annotations

import json
import sqlite3
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "agents.db"
OUT = Path(sys.argv[1]) if len(sys.argv) > 1 else ROOT / "data" / "_archive" / "hub-chat-20261120.jsonl"


def main() -> int:
    con = sqlite3.connect(f"file:{DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    sessions = {r["id"]: dict(r) for r in con.execute(
        "select id, agent_id, title, created_at, updated_at from chat_sessions order by created_at")}
    msgs: dict[str, list] = {}
    for r in con.execute(
            "select session_id, role, content, meta, created_at from chat_messages order by id"):
        msgs.setdefault(r["session_id"], []).append({
            "role": r["role"], "content": r["content"],
            "meta": json.loads(r["meta"]) if r["meta"] else None,
            "created_at": r["created_at"],
        })
    con.close()

    OUT.parent.mkdir(parents=True, exist_ok=True)
    n = 0
    with OUT.open("w", encoding="utf-8") as f:
        for sid, s in sessions.items():
            f.write(json.dumps({"type": "session", "session": s,
                                "messages": msgs.get(sid, [])}, ensure_ascii=False) + "\n")
            n += 1
    print(f"wrote {n} sessions / {sum(len(v) for v in msgs.values())} messages -> {OUT}")
    return 0


if __name__ == "__main__":
    sys.exit(main())