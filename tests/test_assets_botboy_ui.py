# -*- coding: utf-8 -*-
"""Task 10 资产门面 + Botboy：失败路重试 + 相对时间 full（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
ASSET = ROOT / "static" / "hub" / "07-asset-panel.js"
BB = ROOT / "static" / "hub" / "14-botboy.js"


class TestAssetsBotboyUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.asset = ASSET.read_text(encoding="utf-8")
        cls.bb = BB.read_text(encoding="utf-8")

    def test_allsettled_partial_success(self):
        self.assertIn("allSettled", self.asset)

    def test_failure_path_has_retry(self):
        self.assertIn("data-asset=\"search\"", self.asset)
        self.assertIn("重试", self.asset)

    def test_botboy_relative_time_has_full(self):
        self.assertIn("bbAgo", self.bb)
        self.assertIn("title=", self.bb)


if __name__ == "__main__":
    unittest.main()
