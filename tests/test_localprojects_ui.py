# -*- coding: utf-8 -*-
"""Task 8 本机/GitHub 项目：防抖 + 收藏隐藏 + 克隆中态（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
LP = ROOT / "static" / "hub" / "09-local-projects.js"
GH = ROOT / "static" / "hub" / "10-github-projects.js"


class TestLocalProjectsUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.lp = LP.read_text(encoding="utf-8")
        cls.gh = GH.read_text(encoding="utf-8")

    def test_search_debounce_both_pages(self):
        self.assertIn("lpQDebounced", self.tpl)
        self.assertIn("ghQDebounced", self.tpl)
        self.assertIn("300", self.lp)
        self.assertIn("300", self.gh)

    def test_star_hidden_persist_and_filter(self):
        self.assertIn("hub.lp.stars", self.lp)
        self.assertIn("hub.lp.hidden", self.lp)
        self.assertIn("hub.gh.stars", self.gh)
        self.assertIn("hub.gh.hidden", self.gh)

    def test_clone_in_progress_button_text(self):
        self.assertIn("克隆中…", self.gh)

    def test_empty_state_clear_filter(self):
        self.assertIn("清除筛选", self.lp)
        self.assertIn("清除筛选", self.gh)


if __name__ == "__main__":
    unittest.main()
