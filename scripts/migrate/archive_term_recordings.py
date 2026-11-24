#!/usr/bin/env python3
"""Archive AgentHub terminal recordings older than N days.

Default is a dry run: it prints how many rows/bytes would move. `--apply`
writes them to a separate SQLite file and deletes them from agents.db in one
transaction. Run only in a maintenance window; the hub keeps live terminal
sessions in the same table.

Usage:
  python3 scripts/migrate/archive_term_recordings.py --days 30
  python3 scripts/migrate/archive_term_recordings.py --days 30 --apply
"""
from __future__ import annotations

import argparse
import sqlite3
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
DB = ROOT / "data" / "agents.db"
ARCHIVE = ROOT / "data" / "archive" / "term-recordings-archive.db"

SCHEMA = """CREATE TABLE IF NOT EXISTS term_recordings (
  id INTEGER PRIMARY KEY,
  session_id TEXT NOT NULL,
  agent_id TEXT NOT NULL,
  seq INTEGER NOT NULL,
  ts REAL NOT NULL,
  direction TEXT NOT NULL,
  data BLOB NOT NULL,
  bytes INTEGER NOT NULL
)"""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30)
    ap.add_argument("--apply", action="store_true")
    args = ap.parse_args()
    cutoff = time.time() - args.days * 86400

    con = sqlite3.connect(DB, timeout=30)
    n, b = con.execute(
        "select count(*), coalesce(sum(bytes),0) from term_recordings where ts < ?",
        (cutoff,)).fetchone()
    print(f"cutoff {time.strftime('%Y-%m-%d %H:%M', time.localtime(cutoff))}: "
          f"{n} rows / {b/1048576:.1f} MiB")
    if not args.apply:
        print("dry-run; pass --apply in a maintenance window")
        con.close()
        return 0

    ARCHIVE.parent.mkdir(parents=True, exist_ok=True)
    con.execute("ATTACH DATABASE ? AS arc", (str(ARCHIVE),))
    con.execute(SCHEMA.replace("term_recordings", "arc.term_recordings", 1))
    con.execute("BEGIN IMMEDIATE")
    try:
        con.execute("INSERT INTO arc.term_recordings SELECT * FROM main.term_recordings WHERE ts < ?",
                    (cutoff,))
        con.execute("DELETE FROM main.term_recordings WHERE ts < ?", (cutoff,))
        con.commit()
    except Exception:
        con.rollback()
        raise
    finally:
        con.execute("DETACH DATABASE arc")
        con.close()
    print(f"moved {n} rows -> {ARCHIVE}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())