#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-2 审批三态（src/approvals.py + writeauth 接线）。

守的判据（手册 A12：ALLOW / DENY / **ASK** + Preview–Approve–Commit–Verify）：

1. **默认零行为变化**：ASK 前缀集默认空 ⇒ `ask_reason()` 恒 None ⇒ 写门与改动前一致。
2. **票据五道闸**：存在 / 状态 / TTL / 恒定时间比较 / **绑定 (method,path,actor)** / 单次消费。
   负向逐一验证（少一道就等于把「一次批准」变成「永久放行 + 可挪用」）。
3. **预览必须脱敏**：query 与 body 里的凭据不得原样进预览（`?token=` 是既有合法入口）。
4. **端到端**：`write_gate` 在 ASK 面上先回 **202 + approval**（**不执行**），
   批准后带 `x-hub-approval` 重发才走到 `call_next`（Commit）。
5. **与 P0-5 边界四维无关**：本模块不读 `profiles`、不写边界字段（分工见模块 docstring）。

分层口径（tests/tiers.py）：内存存储 + 假时钟 + 假 Request，不起服务、不连网、零 SKIP。
"""
import asyncio
import os
import pathlib
import sys
import unittest
from email.message import Message

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

os.environ.setdefault("HUB_TEST_MODE", "1")

import approvals  # noqa: E402
import writeauth  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        approvals.reset()
        self._env = dict(os.environ)
        os.environ["TERM_TOKEN"] = "unit-token"
        os.environ.pop("HUB_ASK_PREFIXES", None)
        self.addCleanup(self._restore)

    def _restore(self):
        approvals.reset()
        os.environ.clear()
        os.environ.update(self._env)


class TestAskPrefixes(_Base):
    def test_default_is_empty_so_nothing_changes(self):
        self.assertEqual(approvals.configured_prefixes(), ())
        self.assertIsNone(approvals.ask_reason("/api/mcpgw/servers/x"))

    def test_env_enables_prefix(self):
        os.environ["HUB_ASK_PREFIXES"] = "/api/mcpgw/servers, /api/cronjobs"
        self.assertEqual(approvals.configured_prefixes(),
                         ("/api/mcpgw/servers", "/api/cronjobs"))
        self.assertIsNotNone(approvals.ask_reason("/api/mcpgw/servers/abc/tools"))
        self.assertIsNone(approvals.ask_reason("/api/memory/l2"))

    def test_reason_names_the_prefix(self):
        os.environ["HUB_ASK_PREFIXES"] = "/api/x"
        self.assertIn("/api/x", approvals.ask_reason("/api/x/y"))


class TestHeaderAndPreview(_Base):
    def test_split_header(self):
        self.assertEqual(approvals.split_header("abc:def"), ("abc", "def"))
        for bad in ("", "abc", "a:b:c", " : "):
            self.assertEqual(approvals.split_header(bad), ("", ""))

    def test_preview_redacts_credentials_in_query_and_body(self):
        p = approvals.build_preview("POST", "/api/x", "user:term-token",
                                    query="token=SUPERSECRET_QUERY_123456",
                                    body='{"api_key":"SUPERSECRET_BODY_654321"}')
        blob = p["query"] + p["body_snippet"]
        self.assertNotIn("SUPERSECRET_QUERY_123456", blob, "query 里的凭据不得原样进预览")
        self.assertNotIn("SUPERSECRET_BODY_654321", blob, "body 里的凭据不得原样进预览")
        self.assertGreaterEqual(p["redactions"], 1)

    def test_preview_reports_original_body_length_not_snippet_length(self):
        p = approvals.build_preview("POST", "/api/x", "u", body="a" * 5000)
        self.assertEqual(p["body_chars"], 5000, "长度是判断副作用大小的关键信号")
        self.assertTrue(p["truncated"])


class TestStoreLifecycle(_Base):
    def _mk(self):
        return approvals.create("POST", "/api/x", "user:term-token", "ask", {})["id"]

    def test_approve_then_consume_once(self):
        aid = self._mk()
        out = approvals.approve(aid)
        tk = out["ticket"]
        ok, why = approvals.verify_and_consume(aid, tk, "POST", "/api/x", "user:term-token")
        self.assertTrue(ok, why)
        again, why2 = approvals.verify_and_consume(aid, tk, "POST", "/api/x", "user:term-token")
        self.assertFalse(again, "票据必须单次使用")
        self.assertIn("一次性", why2)

    def test_public_view_never_leaks_ticket(self):
        aid = self._mk()
        tk = approvals.approve(aid)["ticket"]
        pub = approvals.public(aid)
        self.assertNotIn("ticket", pub, "对外视图不得带 ticket 字段")
        self.assertNotIn(tk, str(pub))
        self.assertNotIn(tk, str(approvals.pending(50)))

    def test_pending_is_not_approved(self):
        aid = self._mk()
        ok, why = approvals.verify_and_consume(aid, "whatever", "POST", "/api/x", "user:term-token")
        self.assertFalse(ok)
        self.assertIn("pending", why)

    def test_wrong_ticket_rejected(self):
        aid = self._mk()
        approvals.approve(aid)
        ok, why = approvals.verify_and_consume(aid, "not-the-ticket", "POST", "/api/x",
                                               "user:term-token")
        self.assertFalse(ok)
        self.assertIn("票据不匹配", why)

    def test_ticket_is_bound_to_method_path_actor(self):
        aid = self._mk()
        tk = approvals.approve(aid)["ticket"]
        for m, p, a in (("PUT", "/api/x", "user:term-token"),
                        ("POST", "/api/other", "user:term-token"),
                        ("POST", "/api/x", "user:someone-else")):
            ok, _ = approvals.verify_and_consume(aid, tk, m, p, a)
            self.assertFalse(ok, "票据不得挪用到 %s %s %s" % (m, p, a))

    def test_ttl_expiry(self):
        aid = self._mk()
        tk = approvals.approve(aid, now=1000.0)["ticket"]
        ok, why = approvals.verify_and_consume(
            aid, tk, "POST", "/api/x", "user:term-token",
            now=1000.0 + approvals.TTL_S + 1)
        self.assertFalse(ok)
        self.assertIn("TTL", why)

    def test_denied_cannot_be_approved(self):
        aid = self._mk()
        approvals.deny(aid, reason="不同意")
        with self.assertRaises(ValueError):
            approvals.approve(aid)
        ok, why = approvals.verify_and_consume(aid, "x", "POST", "/api/x", "user:term-token")
        self.assertFalse(ok)
        self.assertIn("denied", why)

    def test_double_approve_does_not_reissue_ticket(self):
        aid = self._mk()
        first = approvals.approve(aid)["ticket"]
        with self.assertRaises(ValueError):
            approvals.approve(aid)
        self.assertEqual(approvals.get(aid)["ticket"], first,
                         "重复批准不得换发新票据（换发＝给重放开后门）")


# ── 端到端：经 write_gate ───────────────────────────────────────────────
def _mk_request(method, path, query="", headers=None, body=b""):
    from starlette.requests import Request
    scope = {"type": "http", "method": method, "path": path,
             "query_string": query.encode(),
             "headers": [(k.lower().encode(), v.encode()) for k, v in (headers or {}).items()],
             "client": ("127.0.0.1", 12345)}
    sent = {"done": False}

    async def receive():
        if sent["done"]:
            return {"type": "http.disconnect"}
        sent["done"] = True
        return {"type": "http.request", "body": body, "more_body": False}
    return Request(scope, receive)


class TestWriteGateAsk(_Base):
    def _gate(self, method, path, headers=None, query="", body=b""):
        called = {"n": 0}

        async def call_next(_req):
            called["n"] += 1
            return "DOWNSTREAM"

        req = _mk_request(method, path, query=query, headers=headers, body=body)
        out = asyncio.run(writeauth.write_gate(req, call_next))
        return out, called

    def test_ask_returns_202_without_executing(self):
        os.environ["HUB_ASK_PREFIXES"] = "/api/mcpgw/servers"
        out, called = self._gate("POST", "/api/mcpgw/servers/reg",
                                 headers={"x-hub-token": "unit-token"},
                                 body=b'{"cmd":"rm"}')
        self.assertNotEqual(out, "DOWNSTREAM")
        self.assertEqual(out.status_code, 202)
        self.assertEqual(called["n"], 0, "ASK 态**不得**执行下游")
        import json
        payload = json.loads(out.body)
        self.assertTrue(payload["requires_approval"])
        self.assertIn("id", payload["approval"])

    def test_approved_ticket_commits(self):
        os.environ["HUB_ASK_PREFIXES"] = "/api/mcpgw/servers"
        out, _ = self._gate("POST", "/api/mcpgw/servers/reg",
                            headers={"x-hub-token": "unit-token"})
        import json
        aid = json.loads(out.body)["approval"]["id"]
        tk = approvals.approve(aid)["ticket"]
        out2, called = self._gate("POST", "/api/mcpgw/servers/reg",
                                  headers={"x-hub-token": "unit-token",
                                           "x-hub-approval": "%s:%s" % (aid, tk)})
        self.assertEqual(out2, "DOWNSTREAM")
        self.assertEqual(called["n"], 1)

    def test_replay_is_blocked_again(self):
        os.environ["HUB_ASK_PREFIXES"] = "/api/mcpgw/servers"
        out, _ = self._gate("POST", "/api/mcpgw/servers/reg",
                            headers={"x-hub-token": "unit-token"})
        import json
        aid = json.loads(out.body)["approval"]["id"]
        tk = approvals.approve(aid)["ticket"]
        h = {"x-hub-token": "unit-token", "x-hub-approval": "%s:%s" % (aid, tk)}
        first, c1 = self._gate("POST", "/api/mcpgw/servers/reg", headers=h)
        self.assertEqual(first, "DOWNSTREAM")
        self.assertEqual(c1["n"], 1)
        out2, c2 = self._gate("POST", "/api/mcpgw/servers/reg", headers=h)
        self.assertEqual(c2["n"], 0, "重放不得第二次执行")
        self.assertEqual(out2.status_code, 202)

    def test_ask_prefix_outside_route_is_untouched(self):
        """ASK 面**之外**的写请求走原路（这条钉住「默认零行为变化」）。"""
        os.environ["HUB_ASK_PREFIXES"] = "/api/mcpgw/servers"
        out, called = self._gate("PUT", "/api/memory/l2",
                                 headers={"x-hub-token": "unit-token"})
        self.assertEqual(out, "DOWNSTREAM")
        self.assertEqual(called["n"], 1)

    def test_bad_token_still_401_not_202(self):
        os.environ["HUB_ASK_PREFIXES"] = "/api/mcpgw/servers"
        out, called = self._gate("POST", "/api/mcpgw/servers/reg",
                                 headers={"x-hub-token": "wrong"})
        self.assertEqual(out.status_code, 401, "凭据错仍是 DENY，不得被 ASK 掩盖")
        self.assertEqual(called["n"], 0)


if __name__ == "__main__":
    unittest.main()