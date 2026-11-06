#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic: ghsettings._atomic_write_text 原子落盘（A5）。

崩溃/断电窗口不能留下半截 .env/token：必须同目录临时文件 + os.replace，
且落盘后内容完整、权限 0o600、临时文件不残留。
"""
import os
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

import tiers  # noqa: E402

import ghsettings as gs  # noqa: E402


class TestAtomicWrite(unittest.TestCase):
    def test_content_and_mode_and_no_tmp_residue(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / ".env"
            gs._atomic_write_text(p, "A=1\nB=2\n")
            self.assertEqual(p.read_text(encoding="utf-8"), "A=1\nB=2\n")
            self.assertEqual(oct(p.stat().st_mode & 0o777), "0o600")
            leftovers = [x.name for x in pathlib.Path(d).iterdir() if ".tmp-" in x.name]
            self.assertEqual(leftovers, [])

    def test_overwrite_replaces_atomically(self):
        import tempfile
        with tempfile.TemporaryDirectory() as d:
            p = pathlib.Path(d) / "tok.txt"
            gs._atomic_write_text(p, "old\n")
            gs._atomic_write_text(p, "new\n")
            self.assertEqual(p.read_text(encoding="utf-8"), "new\n")


if __name__ == "__main__":
    unittest.main()
