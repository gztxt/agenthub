#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-6 — 错误分类 / attempt 记录 / 幂等键（src/resilience.py）。

守的判据：

1. **分类与真实重试行为不得漂移**（手册 B6 要防的正是这个）：对一批状态码，
   `classify_error(e)["retryable"]` 必须**逐例等于** `is_retryable(e)`。
   两者各自漂移时（「分类说可重试、实现却不重试」）本文件转红。
2. **kind 是闭集**：必须落在 `ERROR_KINDS` 里，否则账本聚合不动。
3. **attempt 记录**（B7）：一次逻辑调用跨多次尝试共用同一 `call_id`；
   每次尝试一条记录；记录器抛错**不得**把调用打挂。
4. **幂等键确定性**（A6）：同语义同键；`purpose` 不同则键不同；
   **不得**掺时间戳/随机数（否则等于没有幂等 —— 用「两次调用键相同」钉住）。

分层口径（tests/tiers.py）：纯函数 + 假时钟/假睡眠，不起服务、不连网、零 SKIP。
"""
import sys
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import resilience  # noqa: E402


def http_error(code: int, headers=None) -> urllib.error.HTTPError:
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("http://x/y", code, "boom", hdrs, None)


class _FakeClock:
    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def new_breakers():
    clk = _FakeClock()

    def sleeper(seconds: float) -> None:
        clk.advance(seconds)

    return resilience.CircuitBreakers(clock=clk, sleep=sleeper, rand=lambda: 0.0), clk


class TestClassifyKind(unittest.TestCase):
    def test_kind_is_a_closed_set(self):
        for code in (400, 401, 403, 404, 408, 422, 425, 429, 500, 503):
            k = resilience.classify_error(http_error(code))["kind"]
            self.assertIn(k, resilience.ERROR_KINDS, "code=%s 的 kind 越界: %s" % (code, k))

    def test_expected_kinds(self):
        cases = {429: "rate_limit", 401: "auth", 403: "auth", 404: "not_found",
                 422: "invalid_request", 400: "invalid_request",
                 500: "server", 503: "server"}
        for code, want in cases.items():
            self.assertEqual(resilience.classify_error(http_error(code))["kind"], want,
                             "code=%s" % code)
        self.assertEqual(resilience.classify_error(TimeoutError("t"))["kind"], "timeout")
        self.assertEqual(
            resilience.classify_error(urllib.error.URLError(Exception("dns")))["kind"],
            "network")
        self.assertEqual(resilience.classify_error(ValueError("nope"))["kind"], "unknown")

    def test_retryable_agrees_with_is_retryable(self):
        """**本文件的核心不变量**：分类不得与真实重试行为分家。"""
        for code in (400, 401, 403, 404, 408, 422, 425, 429, 500, 502, 503):
            e = http_error(code)
            self.assertEqual(resilience.classify_error(e)["retryable"],
                             resilience.is_retryable(e), "code=%s 分类与 is_retryable 不一致" % code)

    def test_action_matches_retryable(self):
        self.assertEqual(resilience.classify_error(http_error(429))["action"], "retry")
        self.assertEqual(resilience.classify_error(http_error(401))["action"], "abort")

    def test_rate_limit_wins_over_status_text(self):
        """429 即使没带 Retry-After 也归 rate_limit；403 带 Retry-After 也算限流。"""
        self.assertEqual(resilience.classify_error(http_error(429))["kind"], "rate_limit")
        self.assertEqual(
            resilience.classify_error(http_error(403, {"Retry-After": "5"}))["kind"],
            "rate_limit")


class TestIdempotencyKey(unittest.TestCase):
    def test_deterministic_and_no_clock(self):
        a = resilience.idempotency_key("mine_experience", "run-1", "t3")
        b = resilience.idempotency_key("mine_experience", "run-1", "t3")
        self.assertEqual(a, b, "同语义必须同键（否则幂等不成立）")
        self.assertEqual(a, resilience.idempotency_key("mine_experience", "run-1", "t3"))

    def test_purpose_and_parts_change_the_key(self):
        base = resilience.idempotency_key("p", "a", "b")
        self.assertNotEqual(base, resilience.idempotency_key("q", "a", "b"))
        self.assertNotEqual(base, resilience.idempotency_key("p", "a", "c"))
        self.assertNotEqual(base, resilience.idempotency_key("p", "b", "a"))

    def test_is_hex_sha256(self):
        k = resilience.idempotency_key("p", 1)
        self.assertEqual(len(k), 64)
        int(k, 16)   # 非法 hex 会抛


class TestAttemptRecords(unittest.TestCase):
    def test_success_emits_one_record(self):
        b, _ = new_breakers()
        recs = []
        out = b.call("k", lambda: "ok", record=recs.append, call_id="cid")
        self.assertEqual(out, "ok")
        self.assertEqual(len(recs), 1)
        self.assertTrue(recs[0]["ok"])
        self.assertEqual(recs[0]["call_id"], "cid")
        self.assertEqual(recs[0]["kind"], "ok")

    def test_retry_then_success_shares_call_id(self):
        b, _ = new_breakers()
        recs = []
        seq = {"n": 0}

        def fn():
            seq["n"] += 1
            if seq["n"] == 1:
                raise http_error(429, {"Retry-After": "1"})
            return "ok"

        out = b.call("k", fn, attempts=3, sleep_budget_s=10.0,
                     record=recs.append, call_id="cid1")
        self.assertEqual(out, "ok")
        self.assertEqual([r["attempt"] for r in recs], [0, 1])
        self.assertEqual({r["call_id"] for r in recs}, {"cid1"}, "call_id 必须跨 attempt 不变")
        self.assertEqual(recs[0]["kind"], "rate_limit")
        self.assertEqual(recs[0]["action"], "retry")
        self.assertEqual(recs[0]["delay_s"], 1.0, "Retry-After=1 应被采用")
        self.assertFalse(recs[0]["ok"])
        self.assertTrue(recs[1]["ok"])

    def test_abort_recorded_when_not_retryable(self):
        b, _ = new_breakers()
        recs = []
        with self.assertRaises(urllib.error.HTTPError):
            b.call("k", lambda: (_ for _ in ()).throw(http_error(401)),
                   attempts=3, sleep_budget_s=10.0, record=recs.append, call_id="c")
        self.assertEqual(len(recs), 1)
        self.assertEqual(recs[0]["action"], "abort")
        self.assertEqual(recs[0]["kind"], "auth")

    def test_budget_exhausted_recorded(self):
        b, _ = new_breakers()
        recs = []
        with self.assertRaises(urllib.error.HTTPError):
            b.call("k", lambda: (_ for _ in ()).throw(http_error(503)),
                   attempts=3, sleep_budget_s=0.0, record=recs.append, call_id="c")
        self.assertEqual(recs[0]["action"], "budget_exhausted")
        self.assertEqual(recs[0]["delay_s"], 0.0)

    def test_broken_sink_never_breaks_the_call(self):
        """记录器抛错必须被吞掉 —— 可观测不得反噬调用本身。"""
        b, _ = new_breakers()

        def boom(_rec):
            raise RuntimeError("落库挂了")

        self.assertEqual(b.call("k", lambda: "ok", record=boom), "ok")

    def test_no_record_callable_is_zero_cost_path(self):
        b, _ = new_breakers()
        self.assertEqual(b.call("k", lambda: 42), 42)


if __name__ == "__main__":
    unittest.main()