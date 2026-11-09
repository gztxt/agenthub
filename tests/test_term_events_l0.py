#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""当前会话事件解析（term_events）L0：不碰 pty、不碰真 HOME、不调 provider。"""
import json
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import term_events  # noqa: E402


def _write_codebuddy(p: Path):
    rows = [
        {"type": "message", "role": "user", "timestamp": 1000,
         "content": [{"type": "input_text", "text": "帮我修一下"}]},
        {"type": "function_call", "timestamp": 1100, "name": "Bash",
         "callId": "call_1", "arguments": {"command": "ls"}},
        {"type": "function_call_result", "timestamp": 1200, "name": "Bash",
         "callId": "call_1", "status": "completed",
         "output": {"type": "text", "text": "a.txt"}},
        {"type": "message", "role": "assistant", "timestamp": 1300,
         "content": [{"type": "output_text", "text": "修好了"}],
         "providerData": {"model": "m1", "rawUsage": {"prompt_tokens": 10,
                                                     "completion_tokens": 3,
                                                     "total_tokens": 13}}},
        {"type": "message", "role": "user", "timestamp": 2000,
         "content": [{"type": "input_text", "text": "再跑一遍"}]},
        {"type": "message", "role": "assistant", "timestamp": 2100,
         "content": [{"type": "output_text", "text": "好了"}],
         "providerData": {"model": "m1", "rawUsage": {"prompt_tokens": 20,
                                                     "completion_tokens": 2,
                                                     "total_tokens": 22}}},
    ]
    p.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in rows) + "\n",
                 encoding="utf-8")


class TestParseCodebuddy(unittest.TestCase):
    def test_event_sequence_and_summary(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sess-1.jsonl"
            _write_codebuddy(p)
            os.utime(p, (time.time() - 30, time.time() - 30))   # 非 live ⇒ 补 turn_completed
            out = term_events.parse_codebuddy(p, limit=100)
            types = [e["type"] for e in out["events"]]
            self.assertEqual(types, ["turn_started", "tool_call", "tool_result",
                                     "message_delta", "turn_started",
                                     "message_delta", "turn_completed"])
            self.assertEqual([e["seq"] for e in out["events"]], list(range(1, 8)))
            self.assertEqual(out["summary"]["turns"], 2)
            self.assertEqual(out["summary"]["tool_calls"], 1)
            self.assertEqual(out["summary"]["tokens"], 22)
            self.assertEqual(out["events"][-1]["reason"] if "reason" in out["events"][-1] else "", "")
            self.assertFalse(out["live"])

    def test_limit_keeps_tail_and_flags_truncated(self):
        with tempfile.TemporaryDirectory() as d:
            p = Path(d) / "sess-2.jsonl"
            _write_codebuddy(p)
            out = term_events.parse_codebuddy(p, limit=2)
            self.assertEqual(len(out["events"]), 2)
            self.assertTrue(out["truncated"])
            self.assertGreater(out["total_events"], 2)

    def test_unsupported_agent_is_explicit(self):
        class S:
            agent_id = "claude"
            id = "x"
        out = term_events.session_events(S(), 10)
        self.assertFalse(out["supported"])
        self.assertIn("codebuddy", out["note"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
