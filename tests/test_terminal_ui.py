# -*- coding: utf-8 -*-
"""Task 3 终端：状态条三态 + 重连按钮 + 芯片元数据 + 字号 + composer 多行（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
CARDS = ROOT / "static" / "hub" / "03-agents-cards.js"
WS = ROOT / "static" / "hub" / "04-terminal-ws.js"
POLL = ROOT / "static" / "hub" / "02-nav-and-poll.js"


class TestTerminalUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.cards = CARDS.read_text(encoding="utf-8")
        cls.ws = WS.read_text(encoding="utf-8")
        cls.poll = POLL.read_text(encoding="utf-8")

    def test_term_status_bar_three_states(self):
        self.assertIn('id="termStatus"', self.tpl)
        for cls in ("st-ok", "st-conn", "st-rc", "st-off"):
            self.assertIn(cls, self.tpl, "缺状态条态类 " + cls)
        # 已断开时「立即重连」按钮复用 termRcFire
        self.assertRegex(self.tpl, r'id="termReconnect"[^>]*termRcFire')

    def test_status_updates_in_reconnect_path(self):
        self.assertIn("termStatus", self.cards)
        self.assertIn("st-rc", self.cards)

    def test_chip_title_has_last_active(self):
        self.assertIn("最后活跃", self.cards)

    def test_desktop_font_buttons(self):
        self.assertIn('id="termFontDown"', self.tpl)
        self.assertIn('id="termFontUp"', self.tpl)
        self.assertIn("termFontDelta", self.cards)

    def test_composer_multiline_and_send_disable(self):
        self.assertIn('<textarea id="chatInput"', self.tpl)
        self.assertIn("shiftKey", self.tpl)
        self.assertIn('id="chatSendBtn"', self.tpl)
        self.assertIn("chatSendBtn", self.ws)

    def test_offline_input_not_silently_dropped(self):
        # Step7 裁定：不排队（既有错误上下文风险），但显式提示已在
        self.assertIn("终端未连接，输入不会被送达", self.poll)


if __name__ == "__main__":
    unittest.main()
