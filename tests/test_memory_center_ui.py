# -*- coding: utf-8 -*-
"""Task 4 记忆中心：排序 + 截断文案 + 搜索防抖 + 矩阵 sticky（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
WS = ROOT / "static" / "hub" / "04-terminal-ws.js"


class TestMemoryCenterUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.js = WS.read_text(encoding="utf-8")

    def test_truncation_copy_shows_total_and_limit(self):
        self.assertIn("当前显示最近", self.js)
        self.assertIn("上限", self.js)

    def test_sort_select_recent_and_category(self):
        self.assertIn('id="memSort"', self.tpl)
        self.assertIn('value="recent"', self.tpl)
        self.assertIn('value="cat"', self.tpl)
        self.assertIn("memSort", self.js)

    def test_search_debounce_300ms(self):
        self.assertIn("memQDebounced", self.tpl)
        self.assertIn("300", self.js)

    def test_matrix_sticky_first_column(self):
        self.assertRegex(self.tpl, r"\.mx-matrix (th|td):first-child[^{]*\{[^}]*position:\s*sticky")

    def test_fed_result_expandable_full_text(self):
        # 联邦检索结果 300 字截断处保留全文入口（title 全文）
        self.assertIn("mem-item", self.js)


if __name__ == "__main__":
    unittest.main()
