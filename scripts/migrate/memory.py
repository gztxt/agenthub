#!/usr/bin/env python3
"""Migrate AgentHub / TDAI memories into magpie's Memory folder.

Why archived by default:
- AgentHub's `memories` rows are from 2026-09-06 and reference CCR/FCC, both
  retired on 2026-11-17. They are history, not live facts. They are imported
  with `status: archived` so magpie keeps them for provenance and never puts
  them in a digest or a search unless someone sets them active.
- TDAI is the authoritative memory store on this machine (376-411 L1 rows,
  13k L0 conversations). An existing magpie memory says: do not copy TDAI into
  magpie, to avoid two token-paying copies. `--include-tdai` is therefore off
  by default; only run it after an explicit decision to replace that rule.

Usage:
  python3 scripts/migrate/memory.py --dry-run
  python3 scripts/migrate/memory.py --apply
  python3 scripts/migrate/memory.py --apply --include-tdai
"""
from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

HUB_DB = Path(__file__).resolve().parents[2] / "data" / "agents.db"
MAGPIE_MEMORY = Path.home() / ".config" / "magpie" / "memory"
TDAI_RECORDS = Path("/vol1/tdai-data/records")

# Credentials that must never enter a memory digest (it is sent in prompts).
SECRET_PATTERNS = [
    re.compile(r"sk-[A-Za-z0-9_-]{16,}"),
    re.compile(r"ghp_[A-Za-z0-9]{20,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"-----BEGIN [A-Z ]*PRIVATE KEY-----"),
    re.compile(r"(?i)\bbearer\s+[A-Za-z0-9._-]{16,}"),
]

KIND = {
    "fact": "fact",
    "decision": "rule",
    "constraint": "rule",
    "preference": "preference",
}


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def q(s: str) -> str:
    """YAML-safe double-quoted scalar (JSON strings are valid YAML)."""
    return json.dumps(s, ensure_ascii=False)


def text_of(path: Path) -> str:
    s = path.read_text(encoding="utf-8")
    if not s.startswith("---\n"):
        return s
    end = s.find("\n---\n", 4)
    return s[end + 5:] if end >= 0 else s


def existing_texts() -> dict[str, Path]:
    out: dict[str, Path] = {}
    for p in sorted(MAGPIE_MEMORY.glob("*.md")):
        try:
            t = text_of(p).strip()
        except OSError:
            continue
        if t:
            out[hashlib.sha256(t.encode("utf-8")).hexdigest()] = p
    return out


def has_secret(s: str) -> str:
    for pat in SECRET_PATTERNS:
        m = pat.search(s)
        if m:
            return m.group(0)[:12] + "…"
    return ""


def render(mid: str, kind: str, status: str, tags: list[str], text: str,
           created: str, updated: str, source_agent: str, source_session: str = "",
           why: str = "") -> str:
    lines = ["---", f"kind: {kind}", "scope: global", f"status: {status}",
             "origin: imported", f"tags: {json.dumps(tags, ensure_ascii=False)}"]
    if why:
        lines.append(f"why: {q(why)}")
    lines.append(f"created: {q(created or now())}")
    lines.append(f"updated: {q(updated or created or now())}")
    lines.append("sources:")
    lines.append(f"  - agent: {source_agent}")
    if source_session:
        lines.append(f"    session: {q(source_session)}")
    lines.append(f"    at: {q(created or now())}")
    lines += ["---", "", text.strip(), ""]
    return "\n".join(lines)


def records(include_tdai: bool) -> list[dict]:
    out: list[dict] = []
    con = sqlite3.connect(f"file:{HUB_DB}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    for r in con.execute(
            "select id, category, content, source, session_id, status, created_at, updated_at from memories"):
        if r["status"] == "deleted":
            continue  # deleted stays deleted
        out.append({
            "id": f"hub-mem-{r['id']}", "kind": KIND.get(r["category"], "fact"),
            "status": "archived",  # stale CCR-era notes: provenance only
            "tags": ["hub", "imported", r["category"]],
            "text": r["content"], "created": r["created_at"], "updated": r["updated_at"],
            "agent": r["source"] or "hub", "session": r["session_id"] or "",
            "why": "AgentHub 迁移，CCR/FCC 时代记录，保留作证据",
        })
    for r in con.execute("select layer, content, manual, updated_at from memory_docs"):
        text = (r["content"] or "").strip()
        manual = (r["manual"] or "").strip()
        if manual:
            text += "\n\n用户手写备注：\n" + manual
        out.append({
            "id": f"hub-{r['layer'].lower()}", "kind": "procedure",
            "status": "archived", "tags": ["hub", "imported", r["layer"]],
            "text": text, "created": r["updated_at"], "updated": r["updated_at"],
            "agent": "hub", "session": "", "why": "AgentHub 分层记忆文档归档",
        })
    con.close()

    if include_tdai:
        for f in sorted(TDAI_RECORDS.glob("*.jsonl")):
            for line in f.read_text(encoding="utf-8").splitlines():
                if not line.strip():
                    continue
                d = json.loads(line)
                typ = d.get("type") or "episodic"
                pri = int(d.get("priority") or 0)
                kind = {"instruction": "rule", "persona": "profile"}.get(typ, "episode")
                status = "active" if pri >= 80 and typ != "persona" else "candidate"
                out.append({
                    "id": f"tdai-{d.get('id')}", "kind": kind, "status": status,
                    "tags": ["tdai", "imported", typ, f"priority-{pri}"],
                    "text": d.get("content") or "",
                    "created": d.get("createdAt") or "", "updated": d.get("updatedAt") or "",
                    "agent": d.get("agentId") or "tdai", "session": d.get("sessionId") or "",
                    "why": "TDAI L1 导入",
                })
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--apply", action="store_true")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--include-tdai", action="store_true")
    ap.add_argument("--force", action="store_true")
    args = ap.parse_args()
    apply = args.apply and not args.dry_run

    MAGPIE_MEMORY.mkdir(parents=True, exist_ok=True)
    known = existing_texts()
    wrote = skipped = secret = 0
    report = []
    for r in records(args.include_tdai):
        text = (r["text"] or "").strip()
        if not text:
            skipped += 1
            continue
        if len(text) > 2000:
            report.append((r["id"], f"too-long {len(text)}", "skipped"))
            skipped += 1
            continue
        hit = has_secret(text)
        if hit:
            report.append((r["id"], f"secret {hit}", "skipped"))
            secret += 1
            continue
        h = hashlib.sha256(text.encode("utf-8")).hexdigest()
        dest = MAGPIE_MEMORY / f"{r['id']}.md"
        if h in known:
            report.append((r["id"], f"duplicate of {known[h].name}", "skipped"))
            skipped += 1
            continue
        if dest.exists() and not args.force:
            report.append((r["id"], "exists", "skipped"))
            skipped += 1
            continue
        body = render(r["id"], r["kind"], r["status"], r["tags"], text,
                      r["created"], r["updated"], r["agent"], r["session"], r["why"])
        report.append((r["id"], f"{r['kind']}/{r['status']} {len(text)}c", "write" if apply else "dry"))
        if apply:
            tmp = dest.with_suffix(".md.tmp")
            tmp.write_text(body, encoding="utf-8")
            os.chmod(tmp, 0o600)
            os.replace(tmp, dest)
            known[h] = dest
            wrote += 1

    for mid, why, what in report:
        print(f"{what:6} {mid:40} {why}")
    print(f"\n{'applied' if apply else 'dry-run'}: write={wrote if apply else sum(1 for x in report if x[2]=='dry')} "
          f"skipped={skipped} secret={secret} total={len(report)}")
    if not apply:
        print("no files written; pass --apply to write")
    return 0


if __name__ == "__main__":
    sys.exit(main())