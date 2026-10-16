#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-6 接线① —— LLM 路径的重试/熔断/attempt 留痕（`resilience.acall` + `llm.py`）。

【这条判据治什么】
旧现状：`llm._create_message` 单发一次，非 200 抛**纯 RuntimeError**（状态码只在字符串里）
⇒ 429/5xx **既分类不出、也没有重试、也没有熔断**；一次上游限流就是一次用户可见的失败。

【本文件守五条】
1. **结构化携带状态**：`LLMGatewayError` 带 `status`/`headers` ⇒ `_http_code/_headers`
   （鸭子类型）读得到，`classify_error` 才能把 429 判成 `rate_limit`。
2. **异步孪生不漂移**：`acall` 与 `call` 共用 `_decide`（分类/退避/预算）——
   同一种错误在两条入口得到同一结论。
3. **attempt 留痕**：每次尝试一条记录，且**同一逻辑调用的 `call_id` 跨 attempt 不变**。
4. **预算即上限**：退避总时长超预算 ⇒ 放弃重试并**原样抛出**（不「再睡一会儿试试」）。
5. 零真网络：用注入 `sleep`/`rand` 的熔断器与替身 `_post_once`。

分层口径（tests/tiers.py）：纯函数 + 替身，不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import llm          # noqa: E402
import resilience   # noqa: E402


def _breaker():
    """不真睡的熔断器（rand 固定 ⇒ 退避可预期）。"""
    return resilience.CircuitBreakers(clock=lambda: 0.0, sleep=lambda s: None,
                                      rand=lambda: 0.0)


class TestStructuredStatus(unittest.TestCase):
    def test_http_code_reads_duck_typed_status(self):
        self.assertEqual(429, resilience._http_code(llm.LLMGatewayError(429, "slow down")))

    def test_headers_are_lowercased(self):
        e = llm.LLMGatewayError(429, "x", {"Retry-After": "3"})
        self.assertEqual("3", resilience._headers(e).get("retry-after"))

    def test_classify_429_as_rate_limit(self):
        cls = resilience.classify_error(llm.LLMGatewayError(429, "rate limit exceeded"))
        self.assertEqual("rate_limit", cls["kind"])
        self.assertTrue(cls["retryable"])

    def test_retry_after_header_is_used(self):
        cls = resilience.classify_error(llm.LLMGatewayError(429, "x", {"Retry-After": "2"}))
        self.assertEqual(2.0, cls["retry_after_s"])

    def test_urllib_http_error_still_classified(self):
        """鸭子类型分支不得把原有的 urllib 路径改坏。"""
        import urllib.error
        e = urllib.error.HTTPError("http://x", 503, "busy", {}, None)
        self.assertEqual(503, resilience._http_code(e))


class TestAcall(unittest.TestCase):
    def test_retries_retryable_then_succeeds(self):
        calls = []

        async def fn():
            calls.append(1)
            if len(calls) < 3:
                raise llm.LLMGatewayError(503, "busy")
            return {"ok": True}
        out = asyncio.run(_breaker().acall("k", fn, attempts=3, sleep_budget_s=10.0))
        self.assertEqual({"ok": True}, out)
        self.assertEqual(3, len(calls))

    def test_attempt_records_share_call_id(self):
        recs = []

        async def fn():
            raise llm.LLMGatewayError(503, "busy")
        with self.assertRaises(llm.LLMGatewayError):
            asyncio.run(_breaker().acall("k", fn, attempts=2, sleep_budget_s=10.0,
                                         record=recs.append, call_id="cid-1"))
        self.assertEqual(2, len(recs))
        self.assertEqual({"cid-1"}, {r["call_id"] for r in recs},
                         "同一逻辑调用的 call_id 跨 attempt 必须不变")
        self.assertEqual([0, 1], [r["attempt"] for r in recs])

    def test_budget_exhaustion_stops_retrying(self):
        calls = []

        async def fn():
            calls.append(1)
            raise llm.LLMGatewayError(503, "busy")
        with self.assertRaises(llm.LLMGatewayError):
            asyncio.run(_breaker().acall("k", fn, attempts=5, sleep_budget_s=0.0))
        self.assertEqual(1, len(calls), "预算 0 ⇒ 一次都不该重试")

    def test_non_retryable_is_not_retried(self):
        calls = []

        async def fn():
            calls.append(1)
            raise llm.LLMGatewayError(400, "bad request")
        with self.assertRaises(llm.LLMGatewayError):
            asyncio.run(_breaker().acall("k", fn, attempts=3, sleep_budget_s=10.0))
        self.assertEqual(1, len(calls))

    def test_open_breaker_rejects(self):
        b = _breaker()
        for _ in range(resilience.FAILURE_THRESHOLD):
            with self.assertRaises(llm.LLMGatewayError):
                asyncio.run(b.acall("k", lambda: _fail(), attempts=1))

        async def ok():
            return 1
        with self.assertRaises(resilience.BreakerOpen):
            asyncio.run(b.acall("k", ok, attempts=1))


async def _fail():
    raise llm.LLMGatewayError(503, "busy")


class TestLlmWiring(unittest.TestCase):
    """真接线：`llm._create_message` 必须**经过**重试编排（替身 `_post_once`）。"""

    def setUp(self):
        self._saved_inst = resilience.instance
        self._saved_post = llm._post_once
        self._saved_rec = llm._record_attempt
        self._saved_att = llm.LLM_ATTEMPTS
        self._saved_bud = llm.LLM_RETRY_BUDGET_S
        resilience.instance = _breaker()
        llm.LLM_ATTEMPTS, llm.LLM_RETRY_BUDGET_S = 3, 10.0
        self.recs = []
        llm._record_attempt = self.recs.append
        self.addCleanup(self._restore)

    def _restore(self):
        resilience.instance = self._saved_inst
        llm._post_once = self._saved_post
        llm._record_attempt = self._saved_rec
        llm.LLM_ATTEMPTS, llm.LLM_RETRY_BUDGET_S = self._saved_att, self._saved_bud

    def test_transient_failures_are_retried(self):
        calls = []

        async def post(body, request_id):
            calls.append(1)
            if len(calls) < 3:
                raise llm.LLMGatewayError(503, "busy")
            return {"content": [{"type": "text", "text": "hi"}], "usage": {"input_tokens": 1}}
        llm._post_once = post
        data = asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        self.assertEqual(3, len(calls), "两次瞬时失败必须被重试掉")
        self.assertEqual("hi", data["content"][0]["text"])
        self.assertEqual(3, len(self.recs))
        self.assertEqual(1, len({r["call_id"] for r in self.recs}))

    def test_hard_failure_surfaces_structured_error(self):
        async def post(body, request_id):
            raise llm.LLMGatewayError(400, "bad request")
        llm._post_once = post
        with self.assertRaises(llm.LLMGatewayError) as cm:
            asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        self.assertEqual(400, cm.exception.status)


if __name__ == "__main__":
    unittest.main()