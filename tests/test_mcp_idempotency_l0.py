#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-6 接线② —— MCP 工具调用的 **call_id + 幂等键**落地，以及「不自动重试」的理由。

【这条判据治什么】
`mcpgw.mcp_call` 此前记的 detail 只有 `agent_id` ⇒ 一次工具调用**没有可复用的身份**：
下游想判「这件事是否已经发生过」、想把同一次逻辑调用的多次尝试归到一起，都做不到。

【本文件守四条】
1. **幂等键确定性**：同 (server, tool, args) ⇒ 同一把键；args 键序无关；**绝不吃时间戳/随机数**
   （否则「重发得到同一把键」这条前提直接不成立）。
2. **同一逻辑调用一个 `call_id`**（`new_call_id()` 一次性，与幂等键刻意分开）。
3. 两者**都进回包**与 **`profile_events.detail`**（可查、可聚合）。
4. **刻意不自动重试**（`attempts=1`）：MCP 工具副作用未知，盲目重试可能重复执行。
   ⇒ 本文件把「重试次数必须为 1」也钉住，防止日后有人顺手打开。

分层口径：替身 `_session_for` + 临时 db，不起服务、不连网、零 SKIP。
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import types
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db       # noqa: E402
import mcpgw    # noqa: E402
import resilience  # noqa: E402


class _FakeContent:
    def __init__(self, text):
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self, text="ok"):
        self.content = [_FakeContent(text)]
        self.is_error = False


class _FakeSession:
    def __init__(self):
        self.calls = 0

    async def call_tool(self, tool, args):
        self.calls += 1
        return _FakeResult()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0mcp-"))
        self._saved_conn = db._conn
        db.init_db(self.tmp / "t.db")
        mcpgw.ensure_schema()
        db.execute("INSERT INTO mcp_servers(id,name,transport,command,args,env,url,enabled,"
                   "description,created_at,updated_at)"
                   " VALUES('s1','srv','stdio','echo','[]','{}',NULL,1,'','t','t')")
        self.sess = _FakeSession()
        self._saved_session_for = mcpgw._session_for
        self._saved_acl = mcpgw._acl_check
        self._saved_rate = mcpgw._check_rate

        async def session_for(server):
            return types.SimpleNamespace(aclose=_noop_async), self.sess
        mcpgw._session_for = session_for
        mcpgw._acl_check = lambda *a, **k: {"allowed": True, "reason": "test",
                                            "rules": [], "shadow_would_deny": False,
                                            "shadow_rules": []}
        mcpgw._check_rate = lambda *a, **k: None
        self.addCleanup(self._restore)

    def _restore(self):
        mcpgw._session_for = self._saved_session_for
        mcpgw._acl_check = self._saved_acl
        mcpgw._check_rate = self._saved_rate
        db._conn = self._saved_conn

    def _call(self, tool="git_status", args=None):
        body = mcpgw.CallIn(agent_id="a1", server="s1", tool=tool, args=args or {})
        return asyncio.run(mcpgw.mcp_call(body))


async def _noop_async():
    return None


class TestIdempotencyKey(unittest.TestCase):
    def test_deterministic_and_order_insensitive(self):
        a = resilience.idempotency_key("mcp_call", "s1", "t", json.dumps({"a": 1, "b": 2}, sort_keys=True))
        b = resilience.idempotency_key("mcp_call", "s1", "t", json.dumps({"b": 2, "a": 1}, sort_keys=True))
        self.assertEqual(a, b)

    def test_different_args_differ(self):
        a = resilience.idempotency_key("mcp_call", "s1", "t", "{}")
        b = resilience.idempotency_key("mcp_call", "s1", "t", '{"x": 1}')
        self.assertNotEqual(a, b)

    def test_two_calls_same_key_same_args(self):
        self.assertEqual(resilience.idempotency_key("mcp_call", "s1", "t", "{}"),
                         resilience.idempotency_key("mcp_call", "s1", "t", "{}"))


class TestMcpCallWiring(_Base):
    def test_response_carries_call_id_and_key(self):
        out = self._call()
        self.assertTrue(out["ok"])
        self.assertTrue(out["call_id"])
        self.assertTrue(out["idempotency_key"])

    def test_same_args_yield_same_key_across_calls(self):
        a = self._call(args={"x": 1})
        b = self._call(args={"x": 1})
        self.assertEqual(a["idempotency_key"], b["idempotency_key"], "幂等键必须可重发复现")
        self.assertNotEqual(a["call_id"], b["call_id"], "call_id 是一次性的，两码事")

    def test_event_detail_records_both(self):
        self._call()
        rows = db.query("SELECT detail FROM profile_events WHERE source='mcp_call'")
        self.assertEqual(1, len(rows))
        d = json.loads(rows[0]["detail"])
        self.assertIn("call_id", d)
        self.assertIn("idempotency_key", d)

    def test_attempts_is_one_so_no_duplicate_side_effects(self):
        """MCP 工具副作用未知 ⇒ 不许自动重试（成功路径也只许调用一次）。"""
        self._call()
        self.assertEqual(1, self.sess.calls)


class TestNoAutoRetryOnFailure(_Base):
    def test_failure_does_not_retry(self):
        async def boom(tool, args):
            self.sess.calls += 1
            raise RuntimeError("backend down")
        self.sess.call_tool = boom
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._call()
        self.assertEqual(1, self.sess.calls, "副作用未知的工具调用不得自动重发")


if __name__ == "__main__":
    unittest.main()