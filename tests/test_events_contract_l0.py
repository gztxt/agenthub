"""P3 v1 事件契约的 L0 闸门（零网络、零外部 provider）。

覆盖按「能区分什么」组织：
  A. envelope 必填字段 / 版本号取 P4 / seq 单调；
  B. 6 类业务事件 + error 可序列化，未知 type 不抛异常；
  C. error.reason 枚举收敛（未知值 → provider_error + reason_raw）；
  D. ANSI 不进入事件字段；
  E. BaseAdapter 默认包装的事件顺序与文本等到 chat() 的 response；
  F. openai_compat 把 SSE delta/usage/error 转成事件；
  G. codex NDJSON 单行映射（message_delta / usage / tool_call / tool_result）；
  H. SSE 端点 + interrupt 的 200/404/409 语义；
  I. 运行登记表 find/unregister。
"""
import asyncio
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
sys.path.insert(0, str(Path(__file__).resolve().parent))

import events  # noqa: E402
from adapters import cli_agent  # noqa: E402
from adapters.base import BaseAdapter  # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter  # noqa: E402
import api_events  # noqa: E402
import meta  # noqa: E402
import registry  # noqa: E402
from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _collect(agen):
    async def run():
        return [ev async for ev in agen]
    return asyncio.run(run())


class _FakeAdapter(BaseAdapter):
    def __init__(self):
        super().__init__(config=None)
        self.agent_id = "ev-fake"

    async def chat(self, message, session_id=None, model=None, history=None,
                   cwd=None, **kwargs):
        return {"success": True, "agent": self.agent_id, "response": "hello",
                "usage": {"prompt_tokens": 3, "completion_tokens": 2,
                          "total_tokens": 5}}

    async def chat_stream(self, message, session_id=None, model=None, history=None,
                          **kwargs):
        yield "hello"

    async def get_sessions(self, limit=10):
        return []

    async def get_models(self):
        return []


class _FailAdapter(_FakeAdapter):
    async def chat(self, message, **kwargs):
        return {"success": False, "agent": self.agent_id,
                "error": "timeout(5s)：进程组已终止"}


class _A(unittest.TestCase):
    def test_envelope_fields_and_version(self):
        em = events.EventEmitter("sid1", "codex", "turn1")
        ev = em.emit("turn_started", model="m")
        for k in ("v", "seq", "ts", "sid", "turn_id", "agent_id", "type"):
            self.assertIn(k, ev)
        self.assertEqual(ev["v"], meta.PROTOCOL_VERSION)
        self.assertEqual(ev["v"], events.EVENT_VERSION)
        self.assertEqual(ev["sid"], "sid1")
        self.assertEqual(ev["turn_id"], "turn1")
        self.assertEqual(ev["agent_id"], "codex")
        self.assertIsInstance(ev["ts"], str)
        json.loads(events.dumps(ev))      # 必须可直接序列化

    def test_seq_is_monotonic(self):
        em = events.EventEmitter("s", "a")
        seqs = [em.emit("message_delta", text=str(i))["seq"] for i in range(5)]
        self.assertEqual(seqs, [1, 2, 3, 4, 5])
        self.assertEqual([e["seq"] for e in [em.error("timeout")]], [6])

    def test_six_types_serializable(self):
        em = events.EventEmitter("s", "a")
        payloads = {
            "turn_started": {"model": "m"},
            "message_delta": {"text": "x"},
            "tool_call": {"name": "command", "input": "ls"},
            "tool_result": {"name": "command", "output": "ok"},
            "usage": {"prompt_tokens": 1},
            "turn_completed": {"success": True},
        }
        for typ, payload in payloads.items():
            ev = em.emit(typ, **payload)
            self.assertEqual(ev["type"], typ)
            self.assertEqual(json.loads(events.dumps(ev))["type"], typ)
        self.assertEqual(em.seq, 6)

    def test_unknown_type_does_not_raise(self):
        em = events.EventEmitter("s", "a")
        ev = em.emit("future_type", foo=1)
        self.assertEqual(ev["type"], "future_type")

    def test_error_reason_enum(self):
        em = events.EventEmitter("s", "a")
        for reason in events.ERROR_REASONS:
            self.assertEqual(em.error(reason)["reason"], reason)
        ev = em.error("vendor_private_code")
        self.assertEqual(ev["reason"], "provider_error")
        self.assertEqual(ev["reason_raw"], "vendor_private_code")

    def test_ansi_stripped(self):
        em = events.EventEmitter("s", "a")
        ev = em.emit("message_delta", text="\x1b[31mred\x1b[0m ok")
        self.assertEqual(ev["text"], "red ok")

    def test_sse_frame_shape(self):
        ev = events.EventEmitter("s", "a").emit("turn_started")
        frame = events.to_sse(ev)
        self.assertTrue(frame.startswith("data: "))
        self.assertTrue(frame.endswith("\n\n"))
        self.assertEqual(json.loads(frame[6:]), ev)


class _B(unittest.TestCase):
    def test_default_wrapper_success(self):
        ad = _FakeAdapter()
        evs = _collect(ad.chat_events("hi", session_id="s1", model="m"))
        self.assertEqual([e["type"] for e in evs],
                         ["turn_started", "message_delta", "usage", "turn_completed"])
        text = "".join(e.get("text") or "" for e in evs if e["type"] == "message_delta")
        self.assertEqual(text, "hello")
        self.assertTrue(evs[-1]["success"])
        self.assertEqual([e["seq"] for e in evs], [1, 2, 3, 4])
        self.assertEqual({e["sid"] for e in evs}, {"s1"})

    def test_default_wrapper_failure_maps_timeout(self):
        ad = _FailAdapter()
        evs = _collect(ad.chat_events("hi", session_id="s"))
        types = [e["type"] for e in evs]
        self.assertEqual(types, ["turn_started", "error", "turn_completed"])
        self.assertEqual(evs[1]["reason"], "timeout")
        self.assertFalse(evs[2]["success"])
        self.assertEqual(evs[2]["reason"], "timeout")

    def test_default_wrapper_unregisters_run(self):
        ad = _FakeAdapter()
        _collect(ad.chat_events("hi", run_id="run-default"))
        self.assertIsNone(events.get_run("run-default"))


class _C(unittest.TestCase):
    def test_openai_stream_delta_usage(self):
        ad = OpenAICompatAdapter(None, "claude", base_url="http://127.0.0.1:1")

        async def fake_stream(*a, **k):
            yield 'data: {"delta": "he"}\n\n'
            yield 'data: {"delta": "llo"}\n\n'
            yield 'data: {"usage": {"prompt_tokens": 3, "completion_tokens": 2}}\n\n'
            yield "data: [DONE]\n\n"

        ad.chat_stream = fake_stream
        evs = _collect(ad.chat_events("hi", session_id="s", model="m"))
        self.assertEqual([e["type"] for e in evs],
                         ["turn_started", "message_delta", "message_delta",
                          "usage", "turn_completed"])
        self.assertEqual("".join(e.get("text") or "" for e in evs
                                 if e["type"] == "message_delta"), "hello")
        self.assertTrue(evs[-1]["success"])

    def test_openai_stream_error(self):
        ad = OpenAICompatAdapter(None, "claude", base_url="http://127.0.0.1:1")

        async def fake_stream(*a, **k):
            yield 'data: {"error": "HTTP 500: boom"}\n\n'

        ad.chat_stream = fake_stream
        evs = _collect(ad.chat_events("hi", session_id="s"))
        self.assertEqual([e["type"] for e in evs],
                         ["turn_started", "error", "turn_completed"])
        self.assertEqual(evs[1]["reason"], "provider_error")
        self.assertFalse(evs[2]["success"])


class _D(unittest.TestCase):
    def test_codex_line_mapping(self):
        msg = cli_agent._codex_line_events(
            {"type": "item.completed",
             "item": {"type": "agent_message", "text": "hi"}})
        self.assertEqual(msg, [("message_delta", {"text": "hi"})])

        usage = cli_agent._codex_line_events(
            {"type": "turn.completed",
             "usage": {"input_tokens": 10, "output_tokens": 5}})
        self.assertEqual(usage[0][0], "usage")
        self.assertEqual(usage[0][1]["total_tokens"], 15)

        call = cli_agent._codex_line_events(
            {"type": "item.started",
             "item": {"id": "c1", "type": "command_execution", "command": "ls"}})
        self.assertEqual(call[0][0], "tool_call")
        self.assertEqual(call[0][1]["name"], "command_execution")

        res = cli_agent._codex_line_events(
            {"type": "item.completed",
             "item": {"id": "c1", "type": "command_execution",
                      "aggregated_output": "ok", "exit_code": 0}})
        self.assertEqual(res[0][0], "tool_result")
        self.assertEqual(res[0][1]["output"], "ok")

    def test_unknown_codex_line_is_ignored(self):
        self.assertEqual(cli_agent._codex_line_events({"type": "thread.started"}), [])
        self.assertEqual(cli_agent._codex_line_events("not-a-dict"), [])


class _E(unittest.TestCase):
    def setUp(self):
        registry._adapters["ev-fake"] = _FakeAdapter()
        self.app = FastAPI()
        self.app.include_router(api_events.router)
        self.client = TestClient(self.app)
        self.addCleanup(self._cleanup)

    def _cleanup(self):
        registry._adapters.pop("ev-fake", None)
        for r in events.active_runs():
            events.unregister_run(r["run_id"])

    def test_sse_endpoint_streams_ordered_events(self):
        r = self.client.post("/api/agents/ev-fake/chat/events",
                             json={"message": "hi", "session_id": "sse1",
                                   "model": "test-model"})
        self.assertEqual(r.status_code, 200)
        self.assertTrue(r.headers["content-type"].startswith("text/event-stream"))
        evs = [json.loads(line[6:]) for line in r.text.splitlines()
               if line.startswith("data: ")]
        self.assertEqual([e["type"] for e in evs],
                         ["turn_started", "message_delta", "usage", "turn_completed"])
        self.assertEqual([e["seq"] for e in evs], sorted(e["seq"] for e in evs))
        self.assertEqual({e["sid"] for e in evs}, {"sse1"})
        self.assertTrue(evs[-1]["success"])

    def test_endpoint_unknown_agent_404(self):
        r = self.client.post("/api/agents/no-such/chat/events",
                             json={"message": "hi"})
        self.assertEqual(r.status_code, 404)

    def test_interrupt_404_without_run(self):
        r = self.client.post("/api/agents/ev-fake/interrupt", json={})
        self.assertEqual(r.status_code, 404)

    def test_interrupt_200_calls_callback(self):
        calls = []
        events.register_run("r1", "ev-fake", "s1",
                            interrupt=lambda: calls.append(1) or True)
        r = self.client.post("/api/agents/ev-fake/interrupt",
                             json={"session_id": "s1"})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "interrupted")
        self.assertEqual(calls, [1])
        self.assertTrue(events.get_run("r1")["interrupted"])

    def test_interrupt_409_when_unsupported(self):
        events.register_run("r2", "ev-fake", "s2", interrupt=None)
        r = self.client.post("/api/agents/ev-fake/interrupt",
                             json={"session_id": "s2"})
        self.assertEqual(r.status_code, 409)
        self.assertEqual(r.json()["error"]["reason"], "unsupported_steer")

    def test_find_run_prefers_latest(self):
        events.register_run("old", "ev-fake", "s3", interrupt=None)
        events.register_run("new", "ev-fake", "s3", interrupt=None)
        events.get_run("old")["started_at"] = 1.0
        events.get_run("new")["started_at"] = 2.0
        self.assertEqual(events.find_run("ev-fake", "s3")["run_id"], "new")
        self.assertEqual(events.find_run("ev-fake", run_id="old")["run_id"], "old")
        self.assertIsNone(events.find_run("other", "s3"))


class TestAutoSessionPanel(unittest.TestCase):
    """用户口径（2026-11-09）：点「事件」自动汇总当前终端会话，不主动输入消息。"""

    def test_panel_binds_current_term_session(self):
        p = Path(__file__).resolve().parent.parent / "static" / "hub" / "16-events-panel.js"
        src = p.read_text(encoding="utf-8")
        self.assertIn("/api/term/sessions/", src, "必须读当前终端会话的事件端点")
        self.assertIn("termSid", src, "必须绑定当前终端会话 id")
        self.assertIn("evLoad", src, "必须自动轮询加载")
        self.assertNotIn("evSend", src, "不应再有主动发送消息的入口")
        self.assertNotIn("chat/events", src, "不应再走「新开一轮对话」的 SSE 端点")

    def test_template_has_no_composer(self):
        p = Path(__file__).resolve().parent.parent / "templates" / "index.html"
        html = p.read_text(encoding="utf-8")
        self.assertIn('id="evSum"', html, "必须有会话级汇总条")
        self.assertNotIn('id="evSendBtn"', html, "不应再有发送事件流按钮")
        self.assertNotIn('id="evInput"', html, "不应再有事件输入框")


if __name__ == "__main__":
    unittest.main()
