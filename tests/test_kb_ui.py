# -*- coding: utf-8 -*-
"""Task 6 知识库：防抖 + 分组 + 计数明示 + 面包屑 sticky（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
WS = ROOT / "static" / "hub" / "04-terminal-ws.js"


class TestKbUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.js = WS.read_text(encoding="utf-8")

    def test_search_debounce(self):
        self.assertIn("kbQDebounced", self.tpl)
        self.assertIn("300", self.js)

    def test_results_grouped_by_source(self):
        self.assertIn("kbGroup", self.js)

    def test_count_note_when_full_page(self):
        self.assertIn("单次上限", self.js)

    def test_crumb_sticky(self):
        self.assertRegex(self.tpl, r"#kbCrumb[^{]*\{[^}]*position:\s*sticky")

    def test_health_uses_tokens(self):
        self.assertIn("var(--red)", self.js)


if __name__ == "__main__":
    unittest.main()
