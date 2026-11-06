# -*- coding: utf-8 -*-
"""Task 11 设置×4+日志：diff 自动展开 + 口令 eye + 日志防抖分页（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
BOOT = ROOT / "static" / "hub" / "01-core-boot.js"


class TestSettingsUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.js = BOOT.read_text(encoding="utf-8")

    def test_model_select_auto_previews_diff(self):
        self.assertIn("settingsPreviewModel()", self.js)

    def test_password_eye_toggles(self):
        self.assertIn("pass-eye", self.tpl)
        self.assertIn("passEye", self.js)

    def test_logs_search_debounce(self):
        self.assertIn("logQDebounced", self.js)
        self.assertIn("300", self.js)

    def test_logs_pagination(self):
        self.assertIn("logPage", self.js)
        self.assertIn("上一页", self.js)
        self.assertIn("下一页", self.js)

    def test_logs_default_hints(self):
        self.assertIn("logHint", self.tpl)


if __name__ == "__main__":
    unittest.main()
