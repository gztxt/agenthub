"""Parity gate for the magpie data migration (2026-11-20).

Run before/after a magpie upgrade:
  venv/bin/python -m pytest -q tests/test_migration_parity.py

It checks the data the hub gave magpie, not the hub's own retired copies.
Paths are the real machine's; a clean test host skips instead of failing.
"""
import json
import os
import unittest
from pathlib import Path

HOME = Path.home()
MEM = HOME / ".config" / "magpie" / "memory"
LIB = HOME / ".config" / "magpie" / "library.json"
ARCHIVE = Path(__file__).resolve().parents[1] / "data" / "_archive"


def _mem_texts():
    out = []
    if not MEM.is_dir():
        return out
    for p in MEM.glob("*.md"):
        s = p.read_text(encoding="utf-8")
        body = s.split("\n---\n", 1)[1] if s.startswith("---\n") and "\n---\n" in s else s
        out.append((p.name, s, body.strip()))
    return out


class MigrationParityTest(unittest.TestCase):
    def test_magpie_memory_has_hub_archive(self):
        rows = _mem_texts()
        if not rows:
            self.skipTest("magpie memory folder absent on this host")
        ids = {n for n, _s, _b in rows}
        for i in (1, 2, 3, 4):
            self.assertIn(f"hub-mem-{i}.md", ids)
        self.assertIn("hub-l2.md", ids)
        # archived: provenance only, never in the active search/digest
        for n, s, _b in rows:
            if n.startswith("hub-"):
                self.assertIn("status: archived", s)

    def test_tdai_l0_is_not_copied(self):
        # An existing magpie memory says TDAI is authoritative and must not be
        # duplicated; the migration keeps L0 out. This fails loudly if someone
        # re-runs memory.py with --include-tdai without deciding to replace it.
        copied = [n for n, _s, _b in _mem_texts() if n.startswith("tdai-")]
        self.assertEqual(copied, [], "TDAI records were copied into magpie memory: %s" % copied[:5])

    def test_magpie_library_has_hub_mcp(self):
        if not LIB.is_file():
            self.skipTest("magpie library.json absent")
        lib = json.loads(LIB.read_text(encoding="utf-8"))
        names = {s.get("name") for s in (lib.get("mcp") or [])}
        self.assertIn("ekko-studio-use", names)
        self.assertIn("magpie-memory", names)
        ekko = next(s for s in lib["mcp"] if s.get("name") == "ekko-studio-use")
        self.assertTrue(ekko.get("local"), "the hub's MCP server must stay local")
        self.assertEqual(ekko.get("agents"), ["hermes"])

    def test_hub_chat_archive_shape(self):
        f = ARCHIVE / "hub-chat-20261120.jsonl"
        if not f.is_file():
            self.skipTest("hub chat archive absent")
        lines = [json.loads(x) for x in f.read_text(encoding="utf-8").splitlines() if x.strip()]
        self.assertEqual(len(lines), 200)
        self.assertTrue(all(x.get("type") == "session" for x in lines))
        self.assertTrue(any(x["messages"] for x in lines))


if __name__ == "__main__":
    unittest.main()