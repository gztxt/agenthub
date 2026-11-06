# -*- coding: utf-8 -*-
"""Task 5 技能中心：tab hash 记忆 + 搜索防抖（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
WS = ROOT / "static" / "hub" / "04-terminal-ws.js"


class TestSkillCenterUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.js = WS.read_text(encoding="utf-8")

    def test_tab_hash_remembers_view(self):
        self.assertIn("skill-", self.js)
        self.assertIn("location.hash", self.js)

    def test_tab_not_persisted_to_localstorage(self):
        # 加载不写盘：switchSkillView 不得 lsSet tab 键
        self.assertNotRegex(self.js, r"lsSet\('hub\.skill")
        self.assertNotRegex(self.js, r"lsSet\(\"hub\.skill")

    def test_search_debounce_300ms(self):
        self.assertIn("skillQDebounced", self.tpl)
        self.assertIn("300", self.js)

    def test_scan_badge_and_weight_tiebreak_exist(self):
        # 调用记录「转录采集」徽章与权重榜并列按名升序（既有口径锁定）
        self.assertIn("转录采集", self.js)
        self.assertIn("localeCompare", self.js)


if __name__ == "__main__":
    unittest.main()
