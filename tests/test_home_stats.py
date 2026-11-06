# -*- coding: utf-8 -*-
"""Task 2 总览统计：320px 不裁字 + 可钻取 + as_of 新鲜度（L0 文本断言）。

判据对源码事实写（模板 CSS/HTML 字面量 + JS 分片字面量），不靠真机渲染。
Step1 判断点结论：.home-stat 为 minmax(0,1fr) 网格，320px 下列宽可收缩，不裁字，
故只做钻取 + as_of，不改网格列数。
"""
import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
BOOT = ROOT / "static" / "hub" / "01-core-boot.js"


class TestHomeStats(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.js = BOOT.read_text(encoding="utf-8")

    def test_grid_shrinks_not_cropped_at_320(self):
        # minmax(0,1fr) 允许列收缩到 0，数字不被视口裁断；窄屏降为 2 列
        self.assertRegex(self.tpl, r"\.home-stat\s*\{[^}]*repeat\(4,\s*minmax\(0,\s*1fr\)")
        self.assertRegex(self.tpl, r"\.home-stat\s*\{[^}]*repeat\(2,\s*minmax\(0,\s*1fr\)")

    def test_cards_are_drill_buttons(self):
        # 四数变可点击按钮（data-hs 钻取键），含「可用」与「基础设施」两条计划钻取
        for key in ("agents", "running", "infra", "error"):
            self.assertIn('data-hs="%s"' % key, self.tpl, "缺钻取卡 data-hs=%s" % key)
        self.assertRegex(self.tpl, r'<button class="hs"[^>]*data-hs="running"')

    def test_as_of_element_present(self):
        self.assertIn('id="hsAsOf"', self.tpl)

    def test_render_writes_as_of_and_drill_opens_group(self):
        # renderHomeStats 写 as_of，并把钻取映射到 navOpen 组（agents/infra）
        self.assertIn("hsAsOf", self.js)
        self.assertRegex(self.js, r"navOpen\s*=\s*g")
        self.assertIn("infra: 'infra'", self.js)
        self.assertIn("running: 'agents'", self.js)
        self.assertIn("renderNav()", self.js)


if __name__ == "__main__":
    unittest.main()
