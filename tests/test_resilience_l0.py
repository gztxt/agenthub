"""`src/resilience.py` 的 L0 闸门（PT-20260927-12 A 档）。

**零网络、零真实睡眠、零跳过**（L0 出现 skip 即判红，见 PT-20260927-08 教训）：
时钟与 sleep 全部由`CircuitBreakers(clock=,sleep=)` 注入，`fn` 是纯本地闭包。

覆盖按「判据」而不是按「代码行」组织 —— 每一组都先问「这条判据能区分什么」：

A. **可重试性判定**：4xx 不重试 / 5xx 重试 / 超时重试 / ConnectionRefused 不重试。
   红向：把 401 也判成可重试 ⇒ A 组转红。
B. **Retry-After 优先于盲退避 + 封顶**：对端说等 2 秒就等 2 秒（不是 0.5 秒的
   指数档）；对端说等 3600 秒必须被封顶（本机封20 秒）—— 不封顶 = 线程池 worker
   被挂一小时。红向：去掉 min() 封顶 ⇒ B 组转红。
C. **429 不计入熔断失败**：连撞 9 次 429 也不开闸。这是本层最关键的设计
   （限流是对端健康的表现，不是它坏了）。红向：让 429 也计失败 ⇒ C 组转红。
D. **开闸 → 冷却 → 半开只放一个探针 → 连成N 次合闸**：三态机全链路。
   红向：去掉 `half_open_busy` ⇒ D2（并发只放一个）转红。
E. **半开探针异常后不永久卡死**：`_leave` 在抛异常那条路也必须释放占位。
   红向：删掉 finally ⇒ E 转红（半开态永不自愈，比一直 OPEN 更糟）。
F. **接线断言**（静态，但断言的是**行为契约**不是格式）：`vitals.http_ok` 必须
   零重试 —— 它在 L4 探活路径上，而 AGENTS.md 立着「探活最多 2 次即结束」。
   这条若被后人「顺手补上退避」，本组立刻转红。
"""

import os
import sys
import threading
import time
import unittest
import urllib.error
from email.message import Message
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))

import resilience  # noqa: E402


def http_error(code: int, body: str = "", headers=None) -> urllib.error.HTTPError:
    """造一个带 headers 的 HTTPError（`urlopen` 对 4xx/5xx 就是抛这个）。"""
    hdrs = Message()
    for k, v in (headers or {}).items():
        hdrs[k] = v
    return urllib.error.HTTPError("http://x/y", code, "boom", hdrs, None)


class FakeSleeper:
    """记录睡过多久，并让假时钟同步前进 —— 断言「睡了几次、总共多久」用。"""

    def __init__(self, clk):
        self.calls = []
        self._clk = clk

    def __call__(self, seconds: float) -> None:
        self.calls.append(round(seconds, 4))
        self._clk.advance(seconds)


class FakeClock:
    def __init__(self, t0: float = 1000.0):
        self.t = t0

    def __call__(self) -> float:
        return self.t

    def advance(self, dt: float) -> None:
        self.t += dt


def new_breakers():
    """造一组隔离的 breaker + 假时钟/假睡眠。返回 (b, clk, sleeper)。"""
    clk = FakeClock()
    sleeper = FakeSleeper(clk)
    b = resilience.CircuitBreakers(
        clock=clk, sleep=sleeper, rand=lambda: 0.0)   # jitter=0 ⇒ 退避时长完全确定
    return b, clk, sleeper


# ── A. 可重试性判定 ────────────────────────────────────────────────────────
class TestRetryableJudgement(unittest.TestCase):
    def test_5xx_and_408_425_429_retryable(self):
        for code in (408, 425, 429, 500, 502, 503, 504):
            self.assertTrue(resilience.is_retryable(http_error(code)), code)

    def test_other_4xx_not_retryable(self):
        # 401 token 死/ 403 权限不足/ 404 路径错 / 422 请求本身不合法 ——
        # 睡 8 秒再发一次，得到的还是同一个错。
        for code in (400, 401, 403, 404, 409, 422):
            self.assertFalse(resilience.is_retryable(http_error(code)), code)

    def test_timeout_retryable(self):
        self.assertTrue(resilience.is_retryable(TimeoutError("timed out")))
        self.assertTrue(resilience.is_retryable(
            urllib.error.URLError(TimeoutError("timed out"))))

    def test_dns_retryable_but_refused_not(self):
        import socket
        self.assertTrue(resilience.is_retryable(
            urllib.error.URLError(socket.gaierror("Name or service not known"))))
        # 端口没人 listen 是确定事实，不是抖动 ⇒ 重试只是白等。
        self.assertFalse(resilience.is_retryable(
            urllib.error.URLError(ConnectionRefusedError("Connection refused"))))

    def test_arbitrary_exception_not_retryable(self):
        # ValueError（JSON 解析失败之类）不在判定表内 ⇒ 不重试。
        self.assertFalse(resilience.is_retryable(ValueError("bad json")))

    def test_rate_limited_detection(self):
        self.assertTrue(resilience.is_rate_limited(http_error(429)))
        # GitHub 次级限流是 403/503 + `Retry-After` 头（不是 429，也不是正文文案 ——
        # 见 `TestRetryAfter::test_http_error_body_is_not_in_str`）。
        self.assertTrue(resilience.is_rate_limited(
            http_error(403, "", headers={"Retry-After": "60"})))
        self.assertTrue(resilience.is_rate_limited(
            http_error(503, "", headers={"retry-after": "1"})))
        self.assertFalse(resilience.is_rate_limited(http_error(403, "Forbidden")))
        self.assertFalse(resilience.is_rate_limited(http_error(401)))
        self.assertFalse(resilience.is_rate_limited(http_error(404)))
        # reason 形态（status line带文案）也能判
        self.assertTrue(resilience.is_rate_limited(
            urllib.error.HTTPError("u", 429, "Too Many Requests", Message(), None)))


# ── B. Retry-After 优先 + 封顶 ─────────────────────────────────────────────
class TestRetryAfter(unittest.TestCase):
    def test_header_wins_over_backoff(self):
        exc = http_error(429, headers={"Retry-After": "2"})
        self.assertEqual(resilience.parse_retry_after(exc), 2.0)

    def test_header_beats_body(self):
        # 头是协议、正文是猜测，冲突时信头。
        exc = http_error(429, "retry after 9 seconds", headers={"Retry-After": "3"})
        self.assertEqual(resilience.parse_retry_after(exc), 3.0)

    def test_body_parsed_when_no_header(self):
        """文本兜底**只在文本真能到 `str(exc)` 里时**才有效。

        urllib 的 `HTTPError` 把 status line 的 reason 拼进 `str()`，正文在 `.fp`
        里——所以「靠响应正文判限流」这条路在真实调用中永远不会触发（见
        `test_http_error_body_is_not_in_str` 那条）。这里是 reason 形态。
        """
        self.assertEqual(
            resilience.parse_retry_after(
                urllib.error.HTTPError("u", 503, "retry after 7 seconds", Message(), None)), 7.0)
        self.assertEqual(
            resilience.parse_retry_after(
                urllib.error.HTTPError("u", 503, "try again in 4 secs", Message(), None)), 4.0)
        self.assertEqual(
            resilience.parse_retry_after(
                urllib.error.HTTPError("u", 503, "请在 12 秒后重试", Message(), None)), 12.0)

    def test_http_error_body_is_not_in_str(self):
        """**钉住实现坑**：以为「响应正文在 `str(HTTPError)` 里」是错的。

        urllib 拼的是 `'HTTP Error %s: %s' % (code, reason)` —— 正文在 `.fp`。
        所以限流判定绝不能依赖正文（`http_error(403, "…rate limit…")` 这种
        构造出来的 body 根本不会出现在 `str()` 里）。本例若哪天有人把
        `is_rate_limited` 改成读正文，这条转红提醒他重新评估。
        """
        exc = http_error(403, "You have exceeded a secondary rate limit")
        self.assertNotIn("rate limit", str(exc).lower(),
                         "若本断言红了，说明 urllib 行为与本仓假设不同，"
                         "限流判定需重新取证")
        # 可靠信号是 状态码 或 Retry-After 头
        self.assertTrue(resilience.is_rate_limited(
            http_error(403, "You have exceeded a secondary rate limit",
                       headers={"Retry-After": "60"})),
            "403 + Retry-After 头 = 限流的可靠信号")

    def test_absurd_retry_after_capped(self):
        # 不封顶 = 对端能让本机线程池 worker 睡一小时 ⇒ 页面卡死。
        self.assertEqual(
            resilience.parse_retry_after(http_error(429, headers={"Retry-After": "3600"})),
            resilience.RETRY_AFTER_CAP_S)

    def test_http_date_form_not_guessed(self):
        # 日期形态要拿本机时钟换算，本层不认 —— 认不出来当没有，比猜一个时刻安全。
        self.assertIsNone(resilience.parse_retry_after(
            http_error(503, headers={"Retry-After": "Wed, 21 Oct 2026 07:28:00 GMT"})))

    def test_absent_returns_none(self):
        self.assertIsNone(resilience.parse_retry_after(http_error(500)))

    def test_backoff_sequence_and_cap(self):
        # 无 Retry-After：0.5 / 1 / 2 / 4 …，封在 MAX_DELAY_S。
        seq = [resilience.backoff_delay(i) for i in range(8)]
        self.assertEqual(seq[:4], [0.5, 1.0, 2.0, 4.0])
        self.assertTrue(all(v <= resilience.MAX_DELAY_S for v in seq), seq)

    def test_backoff_uses_retry_after_when_present(self):
        self.assertEqual(resilience.backoff_delay(0, retry_after=2.5), 2.5)
        # 但仍受 max_s 约束
        self.assertEqual(resilience.backoff_delay(0, retry_after=999.0),
                         resilience.MAX_DELAY_S)

    def test_jitter_only_adds_and_is_bounded(self):
        self.assertEqual(resilience.backoff_delay(0, jitter=0.0), 0.5)
        self.assertEqual(resilience.backoff_delay(0, jitter=1.0), 1.0)
        # jitter=1 ⇒ 延迟最多翻倍，不是无界
        self.assertLessEqual(resilience.backoff_delay(0, jitter=1.0),
                             resilience.BASE_DELAY_S * 2)


# ── C. 429 不计入熔断失败 ──────────────────────────────────────────────────
class TestRateLimitDoesNotTripBreaker(unittest.TestCase):
    def test_repeated_429_never_opens(self):
        b, _, _ = new_breakers()
        key = "api.github.com|gh_repos"
        for _ in range(resilience.FAILURE_THRESHOLD * 3):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(429)))
        self.assertEqual(b.state_of(key), resilience.CLOSED)
        snap = b.snapshot()
        self.assertEqual(snap["open"], [])
        # 但限流**要被记账** —— 否则「被限流了」这件事在 /health 看不见。
        self.assertEqual(snap["tracked"], 1)

    def test_real_failures_still_open(self):
        # 与 C1 对照组：证明 C1 不是「怎么都不开闸」的恒真断言。
        b, _, _ = new_breakers()
        key = "api.github.com|gh_repos"
        for _ in range(resilience.FAILURE_THRESHOLD):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(b.state_of(key), resilience.OPEN)


# ── D. 三态机 ──────────────────────────────────────────────────────────────
class TestBreakerStateMachine(unittest.TestCase):
    def _trip(self, b, key):
        for _ in range(resilience.FAILURE_THRESHOLD):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))

    def test_opens_after_threshold(self):
        b, _, _ = new_breakers()
        key = "k|x"
        self.assertEqual(b.state_of(key), resilience.CLOSED)   # 未注册过 = closed
        self._trip(b, key)
        self.assertEqual(b.state_of(key), resilience.OPEN)

    def test_open_rejects_without_calling(self):
        b, _, _ = new_breakers()
        key = "k|x"
        self._trip(b, key)
        called = []
        self.assertRaises(resilience.BreakerOpen, b.call, key,
                          lambda: called.append(1))
        self.assertEqual(called, [], "开闸时必须一次都不发")

    def test_cooldown_expires_into_half_open(self):
        b, clk, _ = new_breakers()
        key = "k|x"
        self._trip(b, key)
        self.assertGreater(b.cooldown_remaining_s(key), 0)
        clk.advance(resilience.RECOVERY_TIMEOUT_S + 0.1)
        self.assertEqual(b.state_of(key), resilience.HALF_OPEN)

    def test_half_open_needs_success_threshold_to_close(self):
        b, _, _ = new_breakers()
        key = "k|x"
        self._trip(b, key)
        b._clock()  # noop
        # 冷却到期
        b._st[key]["opened_at"] = b._clock() - resilience.RECOVERY_TIMEOUT_S - 1
        self.assertEqual(b.state_of(key), resilience.HALF_OPEN)
        # 第一次成功还不够（threshold=2）⇒ 仍在半开
        self.assertEqual(b.call(key, lambda: "ok"), "ok")
        self.assertEqual(b.state_of(key), resilience.HALF_OPEN)
        self.assertEqual(b.call(key, lambda: "ok"), "ok")
        self.assertEqual(b.state_of(key), resilience.CLOSED)

    def test_half_open_failure_reopens(self):
        b, _, _ = new_breakers()
        key = "k|x"
        self._trip(b, key)
        b._st[key]["opened_at"] = b._clock() - resilience.RECOVERY_TIMEOUT_S - 1
        self.assertRaises(urllib.error.HTTPError, b.call, key,
                          lambda: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(b.state_of(key), resilience.OPEN)

    def test_success_decays_failure_count(self):
        """成功让失败计数**衰减 1**（不是清零，也不是不变）。

        钉的是**衰减本身**：THRESHOLD=3，先吃 2 次失败（未达阈值）⇒ 补1 次成功
        把计数打回 1 ⇒ 再吃 2 次失败才到3 开闸。若实现写成「成功不衰减」，
        这里第二次循环会在第 1 次就开闸 —— 断言转红。
        """
        b, _, _ = new_breakers()
        key = "k|x"
        for _ in range(resilience.FAILURE_THRESHOLD - 1):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(b.state_of(key), resilience.CLOSED)
        self.assertEqual(b._st[key]["failures"], resilience.FAILURE_THRESHOLD - 1)
        # 一次成功让计数回落1
        self.assertEqual(b.call(key, lambda: "ok"), "ok")
        self.assertEqual(b._st[key]["failures"], resilience.FAILURE_THRESHOLD - 2)
        # 还差1 次才够开闸 ⇒ 此刻必须仍 closed
        self.assertRaises(urllib.error.HTTPError, b.call, key,
                          lambda: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(b.state_of(key), resilience.CLOSED,
                         "衰减生效：补回这一次失败只到阈值-1")
        # 再吃一次才开闸
        self.assertRaises(urllib.error.HTTPError, b.call, key,
                          lambda: (_ for _ in ()).throw(http_error(500)))
        self.assertEqual(b.state_of(key), resilience.OPEN)

    def test_isolated_per_key(self):
        b, _, _ = new_breakers()
        self._trip(b, "a|read")
        # 另一个 host/另一个用途都不受影响 —— 额度不能跨故障域共用。
        self.assertEqual(b.state_of("b|read"), resilience.CLOSED)
        self.assertEqual(b.state_of("a|write"), resilience.CLOSED)
        self.assertEqual(b.call("b|read", lambda: "ok"), "ok")

    def test_breaker_key_is_host_scoped(self):
        # 同一 host 的两个端点必须同一个键（同一个故障域）
        self.assertEqual(
            resilience.breaker_key("https://api.github.com/user/repos?page=1", "x"),
            resilience.breaker_key("https://api.github.com/rate_limit", "x"))
        # 用途要分开（读与写互不牵连）
        self.assertNotEqual(
            resilience.breaker_key("https://api.github.com/x", "read"),
            resilience.breaker_key("https://api.github.com/x", "write"))
        # 端口不同的host 是不同键
        self.assertNotEqual(
            resilience.breaker_key("http://127.0.0.1:3456/v1/models", "x"),
            resilience.breaker_key("http://127.0.0.1:3458/v1/models", "x"))


class TestHalfOpenSingleProbe(unittest.TestCase):
    def test_only_one_probe_admitted(self):
        """半开态只放一个探针 —— 上游没做这件事（锁只护状态不护调用）。

        后果：冷却结束瞬间并发 N 个全打向刚恢复的对端，恰好是对它最坏的时刻。

        同步用`Event` 而非「轮询 + sleep」：轮询读的是**真实时间**，与本模块
        注入的假时钟无关，既慢又不确定（首版就是这么写的，实测 admitted 读到 0）。
        """
        b, _, _ = new_breakers()
        key = "k|x"
        for _ in range(resilience.FAILURE_THRESHOLD):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))
        b._st[key]["opened_at"] = b._clock() - resilience.RECOVERY_TIMEOUT_S - 1
        self.assertEqual(b.state_of(key), resilience.HALF_OPEN)

        admitted = []
        release = threading.Event()
        entered = threading.Event()          # 探针确实进门了
        errors = []

        def slow_probe():
            admitted.append(1)
            entered.set()
            release.wait(5)
            return "ok"

        def run():
            try:
                b.call(key, slow_probe)
            except BaseException as e:  # noqa: BLE001
                errors.append(e)

        t = threading.Thread(target=run)
        t.start()
        self.assertTrue(entered.wait(5), "探针没进门（admitted=%r errors=%r）"
                        % (admitted, errors))
        self.assertEqual(len(admitted), 1)
        # 探针在飞时，后来者一律拒 —— 且不得真的发出去
        sent = []
        for _ in range(5):
            self.assertRaises(resilience.BreakerOpen, b.call, key,
                              lambda: sent.append(1))
        self.assertEqual(sent, [], "半开占位期间不得放第二个探针")
        release.set()
        t.join(5)
        self.assertFalse(t.is_alive())
        self.assertEqual(errors, [], "探针本身不该报错：%r" % (errors,))


# ── E. 半开占位必释放 ──────────────────────────────────────────────────────
class TestHalfOpenNeverSticks(unittest.TestCase):
    def test_exception_releases_half_open_slot(self):
        """`_leave` 必须在抛异常那条路也执行。

        先说清这里测的是什么、不测什么：探针抛异常后**闸会重新打开**（这是
        `_record` 的正确行为，另见 `test_half_open_failure_reopens`）。所以本例
        不能拿「下一个调用能成功」当判据 —— 那样测的是重开闸，不是占位释放。

        真正要钉的是：**占位（`half_open_busy`）必须被清掉**。若`finally`
        缺失，一次网络卡死就把这个 key 永远钉在半开且 busy ⇒ 之后**所有**调用
        都被拒，而冷却逻辑只对 OPEN 生效 ⇒ **永远不自愈，比一直 OPEN 更糟**。

        判据用可证伪的量：清掉占位 + 冷却再过一次后能重新进门。
        """
        b, clk, _ = new_breakers()
        key = "k|x"
        for _ in range(resilience.FAILURE_THRESHOLD):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))
        b._st[key]["opened_at"] = b._clock() - resilience.RECOVERY_TIMEOUT_S - 1
        self.assertEqual(b.state_of(key), resilience.HALF_OPEN)

        # 一个必然抛的探针
        self.assertRaises(RuntimeError, b.call, key,
                          lambda: (_ for _ in ()).throw(RuntimeError("boom")))
        # ① 占位必须已释放（这是本例的主判据）
        self.assertFalse(b._st[key]["half_open_busy"],
                         "占位未释放 ⇒ 冷却逻辑只对 OPEN 生效，这个 key 将永远不自愈")
        # ② 闸被重新打开（对照：说明① 不是「压根没进过半开」的恒真）
        self.assertEqual(b.state_of(key), resilience.OPEN)
        # ③ 再冷却一次后能重新进门 ⇒ 自愈链路完整
        clk.advance(resilience.RECOVERY_TIMEOUT_S + 0.1)
        self.assertEqual(b.state_of(key), resilience.HALF_OPEN)
        self.assertEqual(b.call(key, lambda: "ok"), "ok")


# ── F. 退避与预算 ──────────────────────────────────────────────────────────
class TestRetryBudget(unittest.TestCase):
    def test_retries_then_succeeds(self):
        b, _, sleeper = new_breakers()
        seq = []

        def flaky():
            seq.append(1)
            if len(seq) < 3:
                raise http_error(503)
            return "ok"

        self.assertEqual(
            b.call("k|x", flaky, attempts=3, sleep_budget_s=10), "ok")
        self.assertEqual(len(seq), 3)
        # 退避 0.5 + 1.0（jitter=0）
        self.assertEqual(sleeper.calls, [0.5, 1.0])

    def test_attempts_one_means_no_retry(self):
        b, _, sleeper = new_breakers()
        n = []

        def always_503():
            n.append(1)
            raise http_error(503)

        self.assertRaises(urllib.error.HTTPError, b.call, "k|x", always_503,
                          attempts=1)
        self.assertEqual(len(n), 1)
        self.assertEqual(sleeper.calls, [])

    def test_sleep_budget_stops_retrying(self):
        """**退避不得拖慢探活**那条约束的落点：预算不够睡就放弃，如实抛最后一次的真异常。"""
        b, _, sleeper = new_breakers()
        n = []
        # 对端每次都说等 3 秒；预算 1 秒 ⇒ 一次都睡不成 ⇒ 只发 1 次
        def limited():
            n.append(1)
            raise http_error(429, headers={"Retry-After": "3"})

        self.assertRaises(urllib.error.HTTPError, b.call, "k|x", limited,
                          attempts=3, sleep_budget_s=1.0)
        self.assertEqual(len(n), 1)
        self.assertEqual(sleeper.calls, [])

    def test_zero_budget_never_sleeps(self):
        b, _, sleeper = new_breakers()
        self.assertRaises(urllib.error.HTTPError, b.call, "k|x",
                          lambda: (_ for _ in ()).throw(http_error(503)),
                          attempts=5, sleep_budget_s=0.0)
        self.assertEqual(sleeper.calls, [])

    def test_non_retryable_not_retried_even_with_budget(self):
        b, _, sleeper = new_breakers()
        n = []

        def always_401():
            n.append(1)
            raise http_error(401)

        self.assertRaises(urllib.error.HTTPError, b.call, "k|x", always_401,
                          attempts=5, sleep_budget_s=100)
        self.assertEqual(len(n), 1)
        self.assertEqual(sleeper.calls, [], "4xx 不该睡")

    def test_last_error_reraised_verbatim(self):
        """放弃重试时抛的必须是**最后一次**那个异常，不是第一次的 ——
        否则「先 500 后 403」会被报成 500，把凭据问题说成网络问题。"""
        b, _, _ = new_breakers()
        seq = []

        def mixed():
            seq.append(1)
            if len(seq) == 1:
                raise http_error(500)
            raise http_error(403, "Forbidden")

        with self.assertRaises(urllib.error.HTTPError) as ctx:
            b.call("k|x", mixed, attempts=3, sleep_budget_s=10)
        self.assertEqual(ctx.exception.code, 403)


# ── G. /health 快照 ────────────────────────────────────────────────────────
class TestSnapshot(unittest.TestCase):
    def test_closed_keys_absent_from_open_list(self):
        b, _, _ = new_breakers()
        b.call("a|x", lambda: "ok")
        b.call("b|y", lambda: "ok")
        snap = b.snapshot()
        self.assertEqual(snap["open"], [])
        self.assertEqual(snap["tracked"], 2)
        self.assertEqual(snap["policy"]["failure_threshold"], resilience.FAILURE_THRESHOLD)

    def test_open_row_has_cooldown_and_error(self):
        b, clk, _ = new_breakers()
        key = "api.github.com|gh_repos"
        for _ in range(resilience.FAILURE_THRESHOLD):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))
        snap = b.snapshot()
        self.assertEqual(len(snap["open"]), 1)
        row = snap["open"][0]
        self.assertEqual(row["key"], key)
        self.assertEqual(row["state"], resilience.OPEN)
        self.assertGreater(row["cooldown_remaining_s"], 0)
        self.assertIn("HTTPError", row["last_error"])
        clk.advance(resilience.RECOVERY_TIMEOUT_S + 0.1)
        self.assertEqual(b.snapshot()["open"], [], "冷却完就不再列")

    def test_rate_limit_hits_visible_in_snapshot(self):
        b, _, _ = new_breakers()
        key = "k|x"
        self.assertRaises(urllib.error.HTTPError, b.call, key,
                          lambda: (_ for _ in ()).throw(http_error(429)))
        # 不开闸，但记账在 last_error 里可查（snapshot 只列 open ⇒ 用 state/内部量）
        st = b._st[key]
        self.assertEqual(st["rate_limited_hits"], 1)

    def test_reset_clears(self):
        b, _, _ = new_breakers()
        key = "k|x"
        for _ in range(resilience.FAILURE_THRESHOLD):
            self.assertRaises(urllib.error.HTTPError, b.call, key,
                              lambda: (_ for _ in ()).throw(http_error(500)))
        b.reset()
        self.assertEqual(b.state_of(key), resilience.CLOSED)
        self.assertEqual(b.snapshot()["tracked"], 0)


# ── H. 接线契约（静态断言行为，不是格式）──────────────────────────────────
class TestWiringContract(unittest.TestCase):
    """把「哪些调用点接了、哪些刻意没接」钉成断言 —— 防止后人「顺手补全」。

    `vitals.http_ok` **必须零重试**：它在 L4 探活路径上，而 AGENTS.md 立着
    「探活最多 2 次即结束」（09-23 裁定）。给它加退避 = 把硬线变成
    「2 次 × N 个退避」，且违反 09-23 那个「一轮最多 2 次」的实测依据。
    """

    SRC = Path(__file__).resolve().parent.parent / "src"

    def _read(self, name: str) -> str:
        return (self.SRC / name).read_text(encoding="utf-8")

    def test_vitals_http_ok_has_no_retry(self):
        src = self._read("vitals.py")
        # 取 http_ok 的函数体（到下一个顶层 def 为止）
        body = src.split("def http_ok(", 1)[1].split("\ndef ", 1)[0]
        self.assertNotIn("resilience", body,
                         "vitals.http_ok 在 L4 探活路径上，禁止接入退避/熔断")

    def test_retry_budgets_are_explicit_at_every_call_site(self):
        for name in ("githubprojects.py", "ghsettings.py"):
            src = self._read(name)
            self.assertIn("sleep_budget_s=", src, name)
            self.assertIn("attempts=", src, name)

    def test_breakers_keyed_per_purpose(self):
        """每个接线点都要有**自己的用途标签** —— 共用一个额度会互相拖累。"""
        gp = self._read("githubprojects.py")
        gs = self._read("ghsettings.py")
        gw = self._read("gwprobe.py")
        labels = set()
        for src in (gp, gs, gw):
            for line in src.splitlines():
                if "breaker_key(" in line:
                    labels.add(line.split('"')[-2] if '"' in line else line)
        self.assertGreaterEqual(len(labels), 3,
                                "三个接线点应各有用途标签，实际=%r" % (labels,))

    def test_module_is_stdlib_only(self):
        """本机硬约束：零新增第三方依赖（无 httpx/tenacity/pyyaml）。"""
        src = self._read("resilience.py")
        for banned in ("import httpx", "import tenacity", "import requests",
                       "import yaml", "import aiohttp"):
            self.assertNotIn(banned, src, banned)


if __name__ == "__main__":
    unittest.main()