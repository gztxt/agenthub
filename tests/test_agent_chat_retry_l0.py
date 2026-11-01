#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：PT-20261024-04 —— agent_chat 通道的重试/退避/熔断接线
（`resilience.acall` + `adapters.openai_compat.chat`）。

【这条判据治什么】
实测动机（生产库 profile_events，2026-09-06→10-24）：人工对话 101 次中 25 次失败——
18 次 CCR「HTTP 400 All target providers failed」（**状态码不可重试、语义瞬时**的
聚合外壳）、2 次零星 429/超时。旧 `chat()` 单发一次、非 200 直接返回错误 dict，
一次上游抖动就是一次用户可见失败。

【本文件守六条】
1. **外部契约不变**：仍返回 `{success, agent, response/model/usage}` dict，错误串
   保持 "HTTP {status}: {text}" 旧格式 ⇒ 调用方（api_agents._chat_dispatch）零改动。
2. **瞬时失败被重试**：429 / 5xx / 超时 / CCR-400-上游全败，二次尝试成功即恢复。
3. **硬失败不重试**：普通 400 / 401 一次即返（睡 8 秒再发还是同一个错，纯浪费）。
4. **熔断可区分**：熔断挡住的调用**没发出去**，错误串必须带「熔断中」标记
   （llm.py:139 breaker_state() 同族判据）。
5. **预算即上限**：Retry-After 超过退避预算 ⇒ 放弃重试、原样抛（不「再睡会儿」）。
6. 零真网络：注入假时钟熔断器 + 替身 `_post_once`。

分层口径（tests/tiers.py）：纯函数 + 替身，不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import resilience                      # noqa: E402
from adapters import openai_compat    # noqa: E402
from adapters.openai_compat import OpenAICompatAdapter, AdapterHTTPError  # noqa: E402


async def _noop_sleep(_delay):
    """替身睡眠：退避预算的判定是 `_decide` 里的纯函数（spent 记账），
    与墙钟无关 ⇒ L0 不真睡（15 例省约 12s，且确定性。"""


def _breaker():
    """不真睡的熔断器（rand 固定 ⇒ 退避可预期）。"""
    return resilience.CircuitBreakers(clock=lambda: 0.0, sleep=lambda s: None,
                                      rand=lambda: 0.0)


def _adapter(agent_id="claude"):
    return OpenAICompatAdapter(None, agent_id, base_url="http://127.0.0.1:3456",
                               auth_token="t", default_model="m1")


class _Resp:
    """最小 aiohttp 响应替身（仅 _post_once 用到的两个面）。"""

    def __init__(self, status, text="", headers=None):
        self.status = status
        self._text = text
        self.headers = headers or {}

    async def text(self):
        return self._text

    async def json(self):
        return {"choices": [{"message": {"content": "pong"}}],
                "model": "m1", "usage": {"total_tokens": 3}}


class TestDecideOverride(unittest.TestCase):
    """resilience._decide 的 extra_retryable 补充参数（单一决策源不变）。"""

    def test_override_makes_400_retryable(self):
        exc = AdapterHTTPError(400, "All target providers failed.")
        dec = resilience._decide(exc, 0, 3, 0.0, 10.0, lambda: 0.0,
                                 openai_compat._is_gateway_upstream_failure)
        self.assertTrue(dec["cls"]["retryable"])
        self.assertEqual("retry", dec["action"])

    def test_override_leaves_plain_400_alone(self):
        exc = AdapterHTTPError(400, "bad request")
        dec = resilience._decide(exc, 0, 3, 0.0, 10.0, lambda: 0.0,
                                 openai_compat._is_gateway_upstream_failure)
        self.assertFalse(dec["cls"]["retryable"])
        self.assertEqual("abort", dec["action"])

    def test_override_is_additive_not_replacing(self):
        """并集语义：429 默认可重试，补充判定返回 False 也不得把它抹掉。"""
        exc = AdapterHTTPError(429, "too many requests")
        dec = resilience._decide(exc, 0, 3, 0.0, 10.0, lambda: 0.0,
                                 openai_compat._is_gateway_upstream_failure)
        self.assertTrue(dec["cls"]["retryable"], "只加不减——补充判定不能缩窄默认策略")

    def test_default_path_unchanged_without_override(self):
        exc = AdapterHTTPError(400, "All target providers failed.")
        dec = resilience._decide(exc, 0, 3, 0.0, 10.0, lambda: 0.0)
        self.assertFalse(dec["cls"]["retryable"], "不传补充时默认策略必须原样")


class TestDuckTyping(unittest.TestCase):
    def test_http_code_reads_status(self):
        self.assertEqual(400, resilience._http_code(
            AdapterHTTPError(400, "All target providers failed.")))

    def test_gateway_predicate_is_narrow(self):
        self.assertTrue(openai_compat._is_gateway_upstream_failure(
            AdapterHTTPError(400, '{"error":{"message":"All target providers failed."}}')))
        self.assertFalse(openai_compat._is_gateway_upstream_failure(
            AdapterHTTPError(400, "bad request")))
        self.assertFalse(openai_compat._is_gateway_upstream_failure(
            AdapterHTTPError(500, "All target providers failed.")),
            "500 默认就可重试，不需要也不应该走 400 特例")


class TestChatRetryWiring(unittest.TestCase):
    """真接线：chat() 必须经过重试编排（替身 _post_once）。"""

    def setUp(self):
        self._saved_inst = resilience.instance
        self._saved_att = openai_compat.AGENT_CHAT_ATTEMPTS
        self._saved_bud = openai_compat.AGENT_CHAT_RETRY_BUDGET_S
        self._saved_rec = openai_compat._record_attempt
        resilience.instance = _breaker()
        openai_compat.AGENT_CHAT_ATTEMPTS = 3
        openai_compat.AGENT_CHAT_RETRY_BUDGET_S = 10.0
        self.recs = []
        openai_compat._record_attempt = self.recs.append
        # 退避不真睡（见 _noop_sleep）：预算判定是纯函数，与墙钟无关
        self._sleep_patch = mock.patch.object(asyncio, "sleep", new=_noop_sleep)
        self._sleep_patch.start()
        self.addCleanup(self._restore)

    def _restore(self):
        self._sleep_patch.stop()
        resilience.instance = self._saved_inst
        openai_compat.AGENT_CHAT_ATTEMPTS = self._saved_att
        openai_compat.AGENT_CHAT_RETRY_BUDGET_S = self._saved_bud
        openai_compat._record_attempt = self._saved_rec

    def _patch_post(self, script):
        """script: list，元素为 Exception 或 _Resp；按调用次序消费。

        替身契约与真 `_post_once` 一致：**返回解析后的 JSON dict**（非 200 抛异常）。
        """
        calls = []

        async def post(url, body, total_s=120.0):
            # master 2026-10-29 起 `_post_once` 多一个超时入参（派发链路传 600s）；
            # 替身必须同签名，否则接线一改这里就 TypeError 假红。
            calls.append((url, body, total_s))
            item = script[min(len(calls) - 1, len(script) - 1)]
            if isinstance(item, Exception):
                raise item
            return await item.json()
        a = _adapter()
        a._post_once = post
        return a, calls

    def test_429_then_success(self):
        a, calls = self._patch_post([AdapterHTTPError(429, "too many requests"),
                                     _Resp(200)])
        out = asyncio.run(a.chat("hi"))
        self.assertTrue(out["success"])
        self.assertEqual("pong", out["response"])
        self.assertEqual(2, len(calls), "限流必须被重试掉")
        self.assertEqual(2, len(self.recs), "两次尝试两条留痕（失败+成功各一）")
        self.assertEqual(1, len({r["call_id"] for r in self.recs}),
                         "同一逻辑调用的 call_id 跨 attempt 不变")

    def test_gateway_400_then_success(self):
        """CCR「All target providers failed」是主错形态（18/25）——必须可重试。"""
        a, calls = self._patch_post(
            [AdapterHTTPError(400, "All target providers failed."), _Resp(200)])
        out = asyncio.run(a.chat("hi"))
        self.assertTrue(out["success"])
        self.assertEqual(2, len(calls))

    def test_plain_400_not_retried(self):
        a, calls = self._patch_post([AdapterHTTPError(400, "bad request")])
        out = asyncio.run(a.chat("hi"))
        self.assertFalse(out["success"])
        self.assertEqual("HTTP 400: bad request", out["error"], "错误串保持旧格式")
        self.assertEqual(1, len(calls))

    def test_401_not_retried(self):
        a, calls = self._patch_post([AdapterHTTPError(401, "bad token")])
        out = asyncio.run(a.chat("hi"))
        self.assertFalse(out["success"])
        self.assertEqual(1, len(calls), "token 死了不该睡醒再发一次")

    def test_timeout_then_success(self):
        a, calls = self._patch_post([asyncio.TimeoutError(), _Resp(200)])
        out = asyncio.run(a.chat("hi"))
        self.assertTrue(out["success"])
        self.assertEqual(2, len(calls))

    def test_exhausted_attempts_surface_last_error(self):
        a, calls = self._patch_post([AdapterHTTPError(503, "busy")])
        out = asyncio.run(a.chat("hi"))
        self.assertFalse(out["success"])
        self.assertEqual("HTTP 503: busy", out["error"])
        self.assertEqual(3, len(calls), "attempts=3：一次原始 + 两次重试")

    def test_budget_exhaustion_stops_retrying(self):
        """退避预算 0 ⇒ 一次都睡不起，原样抛（不「再睡会儿试试」）。

        注：Retry-After 会被 `MAX_DELAY_S=8` 封顶，所以「RA=60」本身不等于
        「睡不起」——真正的判据是 spent+delay > sleep_budget_s。
        """
        openai_compat.AGENT_CHAT_RETRY_BUDGET_S = 0.0
        a, calls = self._patch_post(
            [AdapterHTTPError(429, "slow down", {"Retry-After": "60"})])
        out = asyncio.run(a.chat("hi"))
        self.assertFalse(out["success"])
        self.assertEqual(1, len(calls), "预算 0 ⇒ 一次都不该重试")

    def test_breaker_open_is_distinguishable(self):
        """熔断挡住的调用没发出去；错误串必须带熔断标记。"""
        b = resilience.instance
        for _ in range(resilience.FAILURE_THRESHOLD):
            a, _c = self._patch_post([AdapterHTTPError(503, "busy")])
            asyncio.run(a.chat("hi"))
        a, calls = self._patch_post([_Resp(200)])
        out = asyncio.run(a.chat("hi"))
        self.assertFalse(out["success"])
        self.assertIn("熔断中", out["error"])
        self.assertEqual(0, len(calls), "熔断中请求不该发出去")

    def test_success_contract_unchanged(self):
        a, _c = self._patch_post([_Resp(200)])
        out = asyncio.run(a.chat("hi"))
        self.assertEqual({"success", "agent", "response", "model", "usage"},
                         set(out), "dict 契约逐键不变")
        self.assertEqual("claude", out["agent"])
        self.assertEqual({"total_tokens": 3}, out["usage"])

    def test_kill_switch_attempts_1(self):
        """AGENT_CHAT_ATTEMPTS=1 必须逐字回到旧行为（一次即返）。"""
        openai_compat.AGENT_CHAT_ATTEMPTS = 1
        a, calls = self._patch_post([AdapterHTTPError(429, "slow down")])
        out = asyncio.run(a.chat("hi"))
        self.assertFalse(out["success"])
        self.assertEqual(1, len(calls))


if __name__ == "__main__":
    unittest.main()
