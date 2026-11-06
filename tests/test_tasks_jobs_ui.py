# -*- coding: utf-8 -*-
"""Task 7 协同+定时：runPanel 三态 + jobs 乐观更新 + DAG 缩放 + 加载更多（L0）。"""
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TPL = ROOT / "templates" / "index.html"
WS = ROOT / "static" / "hub" / "04-terminal-ws.js"
CHAT = ROOT / "static" / "hub" / "05-chat-and-history.js"


class TestTasksJobsUi(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tpl = TPL.read_text(encoding="utf-8")
        cls.js = WS.read_text(encoding="utf-8")
        cls.chat = CHAT.read_text(encoding="utf-8")

    def test_runpanel_three_states(self):
        for cls in ("run-decomposing", "run-running", "run-done"):
            self.assertIn(cls, self.js, "缺三态 " + cls)

    def test_jobs_optimistic_update(self):
        self.assertIn("乐观", self.chat)

    def test_dag_zoom_keys(self):
        for label in ("适配宽度", "100%", "放大", "缩小"):
            self.assertIn(label, self.tpl, "缺 DAG 键 " + label)

    def test_runs_more_loading_guard(self):
        self.assertIn("加载更多", self.js)
        self.assertIn("已列全部", self.js)

    def test_runevents_expandable(self):
        self.assertIn("runEventsExpand", self.tpl)
        self.assertIn("expanded", self.tpl)


if __name__ == "__main__":
    unittest.main()
