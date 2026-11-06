# -*- coding: utf-8 -*-
"""Task 9 资源/端口/遥测/MCP：Kill 确认 + 范围档 + JSON 校验（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
RES = ROOT / "static" / "hub" / "11-resources.js"
WS = ROOT / "static" / "hub" / "04-terminal-ws.js"
CHAT = ROOT / "static" / "hub" / "05-chat-and-history.js"


class TestResourcesUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.res = RES.read_text(encoding="utf-8")
        cls.js = WS.read_text(encoding="utf-8")
        cls.chat = CHAT.read_text(encoding="utf-8")

    def test_kill_has_confirm_with_target(self):
        self.assertIn("confirm(", self.res)
        self.assertIn("PID", self.res)

    def test_telemetry_range_keys(self):
        for label in ("1h", "6h", "24h", "全量"):
            self.assertIn(label, self.tpl, "缺遥测档 " + label)

    def test_mc_args_json_validation_locates_error(self):
        self.assertIn("JSON.parse", self.chat)
        self.assertIn("args 需为 JSON", self.chat)

    def test_port_agent_tag_uses_token_class(self):
        self.assertIn("tag agent", self.js)


if __name__ == "__main__":
    unittest.main()
