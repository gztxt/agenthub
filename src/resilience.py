"""出站调用韧性层：退避重试（单次调用层）+ 熔断（跨调用层）。

【为什么现在才有】本机出站调用此前是 8 处裸 `urlopen(..., timeout=N)`、零重试
（`gwprobe`/`githubprojects`/`ghsettings`/`vitals`/`hubmcp`/`cloudcli`）。痛不是假想：
`vitals.py` 自己把 `rate_limited` 列为 recurring failure态（`RT_RETRYABLE`）并用
`RATE_RE` 解析 429 文本 —— 但那只是**语义层判因**（拿它决定卡片显示哪一档），
**调用层既不退避也不熔断**，于是同一个限流窗口里每个调用点各自把一次失败吃下去。
GitHub API 与 CCR 网关恰好是最需要退避的两类对端。

【来源与取舍】语义借鉴 `nathangtg/agent-hub`（MIT）的
`app/utils/circuit_breaker.py` + `app/utils/rate_limit_handler.py`
（评估见 `技术文档/nathangtg-agent-hub_融合评估报告.md` §三·A），但**只抄语义不抄实现**：
上游是 async + 全局单例 + `time.time()` 硬时钟，本机是同步 urlopen + 多线程
（FastAPI 线程池 + `gwprobe` 后台探测线程）⇒ 改写为可注入时钟、可测、无全局污染。
上游自己那几个坑也一并避开：`_extract_wait_time` 用裸 `except:` 吞掉一切
（`rate_limit_handler.py:89-91`）、`CircuitBreaker.call` 在 `async with self._lock`
里做状态转换但**锁只护状态不护调用**（`circuit_breaker.py:47-56`，并发下可同时
放行多个半开探针）。**只取「三态机 + 429 不计失败 + Retry-After 优先」三条。**

【关键设计：429 不计入熔断失败】限流是**对端健康的表现**，不是它坏了 ——
把它算作失败会让「对端只是忙」演成「我方熔断」，然后在它最需要被调用的时候
主动切断。这是上游最值得抄的一条（`circuit_breaker.py:74-82`）。

【退避不得拖慢探活】AGENTS.md 立了「探活最多 2 次即结束」，`vitals` 另有
`RT_MAX_TRIES` 预算（09-23 裁定，理由是实测 68 次/天把 swap 打到 90%）。
所以每个调用点必须显式给 **attempts** 与 **sleep_budget_s** 两个上限：
退避总时长超预算就**立刻放弃重试并原样抛出**，而不是「再睡一会儿试试」。
默认 `attempts=1`（=不重试，只熔断）——**接入是逐点显式决定的，不是全局默认**。

【Retry-After 要封顶】对端可以写 `Retry-After: 3600`。照单全收会把一个线程池
worker 挂一小时，在本机直接表现为「页面卡住」。故解析结果一律 `min(值, retry_after_cap_s)`。
"""

from __future__ import annotations

import hashlib
import json
import os
import random
import re
import socket
import threading
import time
import urllib.error
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

CLOSED = "closed"
OPEN = "open"
HALF_OPEN = "half_open"

#: 连续失败多少次开闸。上游默认 5；本机调用点多为「后台探测 + 低频刷新」，
#: 5 次意味着坏对端要坏几分钟才被断开，太慢，取 3。
FAILURE_THRESHOLD = int(os.getenv("HUB_RESILIENCE_FAILS", "3"))
#: 开闸后等多久放一个半开探针（秒）。
RECOVERY_TIMEOUT_S = float(os.getenv("HUB_RESILIENCE_COOLDOWN_S", "30"))
#: 半开态要连成几次才合闸（上游 2，保留）。
SUCCESS_THRESHOLD = int(os.getenv("HUB_RESILIENCE_SUCCESSES", "2"))
#: 指数退避的底与顶（秒）。
BASE_DELAY_S = float(os.getenv("HUB_RESILIENCE_BASE_DELAY_S", "0.5"))
MAX_DELAY_S = float(os.getenv("HUB_RESILIENCE_MAX_DELAY_S", "8"))
#: 对端给的 Retry-After 上限（秒）——见模块 docstring「Retry-After 要封顶」。
RETRY_AFTER_CAP_S = float(os.getenv("HUB_RESILIENCE_RETRY_AFTER_CAP_S", "20"))

#: 429/限流的错误文本里常见的「等 N 秒」表述。上游只认`retry after N seconds`
#: 一种形态（`rate_limit_handler.py:88`），实测各对端措辞不一，故多收几种。
#: 两种语序都要认：英文「retry after 7 seconds」是「短语在前、数字在后」，
#: 中文「请在 12 秒后重试」是「数字在前、量词在后」—— 只写前者会漏掉后者。
_RETRY_AFTER_TEXT = re.compile(
    r"(?:retry[- ]?after|try again in|retry in)\D{0,8}?(\d+(?:\.\d+)?)\s*"
    r"(?:s\b|sec|secs|second|seconds)"
    r"|\D{0,12}?(\d+(?:\.\d+)?)\s*(?:s\b|sec|secs|second|seconds|秒)\s*(?:后|内|再试|retry)",
    re.I,
)
#: 限流特征 —— 判「是否 429」用。优先看 HTTP 状态码，文本只作兜底（有的对端
#: 用 403 + 文案表达限流，GitHub 的次级限流就是这样）。
_RATE_TEXT = re.compile(r"\b429\b|too many requests|rate limit|abuse detection", re.I)


class BreakerOpen(RuntimeError):
    """熔断已开：本次调用**没有发出去**（与对端拒绝是两件事，前者我方主动）。"""


def _http_code(exc: BaseException) -> Optional[int]:
    if isinstance(exc, urllib.error.HTTPError):
        try:
            return int(exc.code)
        except Exception:  # noqa: BLE001
            return None
    return None


def _headers(exc: BaseException) -> Dict[str, str]:
    if isinstance(exc, urllib.error.HTTPError) and exc.headers is not None:
        try:
            return {k.lower(): v for k, v in exc.headers.items()}
        except Exception:  # noqa: BLE001
            return {}
    return {}


def parse_retry_after(exc: BaseException, cap_s: float = RETRY_AFTER_CAP_S) -> Optional[float]:
    """从异常里解析「对端要求的等待秒数」。取不到返`None`（调用方退化成盲退避）。

    顺序：**HTTP `Retry-After` 头 → 错误文本里的秒数**。头是协议、文本是猜测，
    两者冲突时信头。上游只做后者（`_extract_wait_time`），本机补上头 ——
    GitHub 的次级限流会给 `Retry-After`（这才是它真正会兑现的那个信号）。

    ⚠️ 文本兜底的适用范围见 `is_rate_limited` 的docstring：`str(HTTPError)`
    **不含响应正文**，所以对 urllib 的 4xx/5xx 而言这条兜底实际只能读到
    status line 的 reason（形如 "Too Many Requests"）。它真正的用武之地是
    非 urllib 的异常（aiohttp / 自造异常把消息放进了 `str()`）。

    `Retry-After` 的 **HTTP-date 形态不解析**（换算依赖本机时钟可信度，超出本层职责；
    要按它退避就在调用点自己算）。解析不出来当没有，比猜一个时刻安全。
    """
    raw = _headers(exc).get("retry-after")
    if raw:
        try:
            # 只认「秒数」形态。HTTP-date 形态需要 now()+解析，且本机对端实测
            # 都没给过日期形态；认不出来就当没有，比猜一个时刻安全。
            val = float(str(raw).strip())
        except (TypeError, ValueError):
            val = None
        if val is not None and val >= 0:
            return min(val, cap_s)
    m = _RETRY_AFTER_TEXT.search(str(exc))
    if m:
        # 两个分支各有一个捕获组；哪个先被填上就取哪个。
        val = m.group(1) if m.group(1) is not None else m.group(2)
        try:
            return min(float(val), cap_s)
        except (TypeError, ValueError):
            return None
    return None


def is_rate_limited(exc: BaseException) -> bool:
    """是否限流。

    **优先看状态码 429**，其次「403/503 + 带`Retry-After` 头」，最后才退到文本。

    ⚠️ **不要指望 `str(HTTPError)` 里有响应正文** —— urllib 拼的是
    `'HTTP Error %s: %s' % (code, reason)`，`reason` 取自 status line（"Too Many
    Requests"/"Forbidden"），**正文在 `.fp` 里，不在 `str()` 里**。
    第一版实现想「靠正文里的 rate limit 文案兜底」，实测那条路在真实调用中
    **永远不会触发**（永远只看得到 reason）；更糟的是想去读 `.fp` 又会把
    调用方要用的错误正文吃掉。所以正文兜底只保留给**非 HTTPError** 的异常
    （aiohttp 之类会把消息放进 `str()` 的情形）。
    """
    if _http_code(exc) == 429:
        return True
    code = _http_code(exc)
    if code in (403, 503) and _headers(exc).get("retry-after"):
        return True
    if isinstance(exc, urllib.error.HTTPError):
        return bool(_RATE_TEXT.search(exc.reason or "" if exc.reason else ""))
    return bool(_RATE_TEXT.search(str(exc)))


def is_retryable(exc: BaseException) -> bool:
    """该不该重试。**其余一律不重试** —— 尤其是 4xx：

    · 408/425：请求超时/ Too Early，对端明说「可以再来」；
    · 429：对端明说「太密了」；
    · 5xx：对端自己的错，重试有意义；
    · **其它 4xx 不重试**：401 是 token 死了、403 是权限不足、404 是路径错了、
      422 是请求本身不合法 —— 睡 8 秒再发一次，得到的还是同一个错，纯浪费。

    异常类型同理：`socket.timeout` / DNS 解析失败是瞬时的；`ConnectionRefused`
    不列 —— 端口没人 listen 是确定事实，不是抖动。
    """
    code = _http_code(exc)
    if code is not None:
        return code in (408, 425, 429) or 500 <= code < 600
    if isinstance(exc, (socket.timeout, TimeoutError)):
        return True
    if isinstance(exc, urllib.error.URLError):
        reason = getattr(exc, "reason", None)
        if isinstance(reason, (socket.timeout, TimeoutError)):
            return True
        if isinstance(reason, socket.gaierror):
            return True  # DNS 瞬时抖动是常态
        return False
    return False


def backoff_delay(attempt: int, retry_after: Optional[float] = None,
                  base_s: float = BASE_DELAY_S, max_s: float = MAX_DELAY_S,
                  jitter: float = 0.0) -> float:
    """第`attempt` 次重试前睡多久（秒）。`attempt` 从 0 起。

    **对端给了等待秒数就用它的**（`Retry-After`），没给才走 0.5/1/2/4…指数退避。
    `jitter` 是 0~1 的比例（乘在延迟上），默认由调用方传随机数；测试传 0 即完全确定。
    """
    if retry_after is not None:
        return max(0.0, min(retry_after, max_s))
    delay = base_s * (2 ** max(0, attempt))
    delay = min(delay, max_s)
    if jitter:
        delay += delay * jitter
    return max(0.0, delay)


def breaker_key(url: str, purpose: str) -> str:
    """熔断分键：`host:port|用途`。**按 host 而非按URL** —— 同一个对端的多个
    端点是同一个故障域；按 URL 分会把一个真故障拆成 N 个互不知情的独立额度。
    用途（purpose）进键，是为了「同一个 host 上的读与写互不牵连」——
    写失败往往是我方请求有问题，不该把只读路径一起熔断掉。"""
    host = "?"
    try:
        from urllib.parse import urlsplit
        p = urlsplit(str(url or ""))
        host = p.netloc or str(url or "")
    except Exception:  # noqa: BLE001
        host = str(url or "")
    return "%s|%s" % (host or "?", purpose or "default")


#: 错误类闭集（P1-6 / 手册 B6）。**直接进 detail / 账本**，故必须是枚举而非自由文本：
#: 自由文本聚合不动（「HTTP Error 429: Too Many Requests」与「rate limited」是同一类，
#: 按字符串分就是两类）。
ERROR_KINDS = ("rate_limit", "auth", "not_found", "invalid_request", "server",
               "timeout", "network", "unknown")


def classify_error(exc: BaseException) -> Dict[str, Any]:
    """把异常归入**稳定闭集**并给出行动（B6：错误分类**先于**重试）。

    返回 `{kind, action, retryable, retry_after_s, code}`：
      · `kind ∈ ERROR_KINDS`，可直接落库聚合；
      · `action ∈ {'retry','abort'}`（不含预算判断——那是 `call()` 的事）。

    ⚠ 本函数**不重复判定逻辑**，一律调用既有的 `is_rate_limited` / `is_retryable` /
    `parse_retry_after` —— 否则分类与真实重试行为会各自漂移（那正是 B6 要防的：
    「分类说可重试，实现却不重试」）。
    """
    code = _http_code(exc)
    retryable = is_retryable(exc)
    if is_rate_limited(exc):
        kind = "rate_limit"
    elif code in (401, 403):
        kind = "auth"
    elif code == 404:
        kind = "not_found"
    elif code is not None and 400 <= code < 500:
        kind = "invalid_request"
    elif code is not None and 500 <= code < 600:
        kind = "server"
    elif isinstance(exc, (socket.timeout, TimeoutError)):
        kind = "timeout"
    elif isinstance(exc, urllib.error.URLError):
        kind = "network"
    else:
        kind = "unknown"
    return {"kind": kind, "action": "retry" if retryable else "abort",
            "retryable": retryable, "retry_after_s": parse_retry_after(exc), "code": code}


def idempotency_key(purpose: str, *parts: Any) -> str:
    """幂等键（A6/B7）—— 对写操作**确定性构造**，使「上次到底发生没发生」可查。

    口径（本机裁定，写死在这里以免各调用点自由发挥）：

    1. **只吃调用方显式给出的语义字段**（`purpose` + 业务 id 等 parts）。
       **绝不吃时间戳 / 随机数 / 进程 id** —— 含这些的键每次都不同，等于没有幂等。
    2. `json.dumps(..., sort_keys=True)`，保证同语义同键（dict 顺序不影响）。
    3. 取 `sha256` 全 64 位 hex：键要进 DB 索引，截短既省不了什么又抬高撞率。

    用法：副作用行动重试前先 `SELECT ... WHERE idempotency_key=?`，命中就不再发。
    """
    payload = json.dumps({"purpose": str(purpose), "parts": [str(p) for p in parts]},
                         ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def new_call_id() -> str:
    """一次**逻辑调用**的标识（P1-6）。同一个 `call_id` 跨多次 attempt 不变，
    从而「第 1 次 429、等 2s、第 2 次 200」能串成一条链 —— 这是 B7 说的
    「可归因 attempt 记录」的前提（没标识就只能看到孤立的一条条记录）。

    与 `idempotency_key()` 的差别：后者是**确定性**的（同语义同键，用于去重），
    前者是**一次性**的（用于归因），两者不可混用。
    """
    return uuid.uuid4().hex[:12]


def _emit_attempt(record: Optional[Callable[[Dict[str, Any]], None]], **rec: Any) -> None:
    """把一条 attempt 记录交给调用方的 sink（B7）。

    **sink 抛错必须被吞掉**：记录是为了可观测，绝不能因为落库失败把一次
    本来成功的调用打挂。`record is None` 时零开销。
    """
    if record is None:
        return
    try:
        record(rec)
    except Exception:  # noqa: BLE001
        pass


class CircuitBreakers:
    """按 key 管理的熔断器集合（进程内，无全局单例污染 —— 实例由`instance()`给出）。

    半开态**只放一个探针**：`_half_open_busy` 为真时后来的调用直接吃 `BreakerOpen`。
    上游没做这件事（锁只护状态转换不护调用），后果是冷却结束瞬间并发 N 个
    全打向刚恢复的对端 —— 对「刚从限流里缓过来」的对端恰好是最坏的时刻。
    """

    def __init__(self, *, clock: Optional[Callable[[], float]] = None,
                 sleep: Optional[Callable[[float], None]] = None,
                 rand: Optional[Callable[[], float]] = None) -> None:
        self._clock = clock or time.monotonic
        self._sleep = sleep if sleep is not None else time.sleep
        self._rand = rand or random.random
        self._lock = threading.Lock()
        # key -> {state, failures, successes, opened_at, last_failure_at,
        #          last_error, rate_limited_hits, calls, rejected, half_open_busy}
        self._st: Dict[str, Dict[str, Any]] = {}

    # ── 状态查询（只读，供 /health 与测试）────────────────────────────
    def state_of(self, key: str) -> str:
        with self._lock:
            st = self._st.get(key)
            return CLOSED if st is None else self._refresh(st)["state"]

    def cooldown_remaining_s(self, key: str) -> float:
        with self._lock:
            st = self._st.get(key)
            if not st or self._refresh(st)["state"] != OPEN:
                return 0.0
            return max(0.0, round(RECOVERY_TIMEOUT_S - (self._clock() - (st["opened_at"] or 0.0)), 1))

    def snapshot(self) -> Dict[str, Any]:
        """/health 用：**只回开闸的**（含冷却剩余秒）+ 总数。closed 的不列 ——
        `/health` 被前端轮询，把十几个 closed 的细节全塞进去只会更难读。"""
        now = self._clock()
        with self._lock:
            keys = list(self._st)
            rows: List[Dict[str, Any]] = []
            for k in keys:
                st = self._refresh(self._st[k])
                if st["state"] != OPEN:
                    continue
                rows.append({
                    "key": k,
                    "state": st["state"],
                    "failures": st["failures"],
                    "cooldown_remaining_s": max(
                        0.0, round(RECOVERY_TIMEOUT_S - (now - (st["opened_at"] or 0.0)), 1)),
                    "last_error": st["last_error"],
                    "rate_limited_hits": st["rate_limited_hits"],
                })
        rows.sort(key=lambda r: r["key"])
        return {"tracked": len(keys), "open": rows,
                "policy": {"failure_threshold": FAILURE_THRESHOLD,
                           "recovery_timeout_s": RECOVERY_TIMEOUT_S,
                           "success_threshold": SUCCESS_THRESHOLD}}

    # ── 主入口 ────────────────────────────────────────────────────────
    def call(self, key: str, fn: Callable[[], Any], *, attempts: int = 1,
             sleep_budget_s: float = 0.0,
             record: Optional[Callable[[Dict[str, Any]], None]] = None,
             call_id: str = "") -> Any:
        """在熔断保护下调用 `fn()`，失败按 `is_retryable` 退避重试。

        参数
        · `attempts`：**总**尝试次数（1 = 不重试）。默认 1 —— 接入是逐点显式决定的。
        · `sleep_budget_s`：**本次调用允许的退避总时长上限**。下一次要睡的秒数
          超过剩余预算就放弃重试、原样抛出。**这是「退避不得拖慢探活」那条
          约束的落点**：宁可少试一次，也不在 /health 的路径上睡出用户可见的卡顿。
        · `record`（P1-6）：可选的**每次尝试**回调，收到
          `{call_id, attempt, kind, action, retryable, retry_after_s, delay_s, slept_s,
          elapsed_ms, ok}`。这是 B7「留可归因 attempt 记录」的落点 —— 传
          `db.log_profile_event` 之类的适配器即可落库；不传则零开销。
        · `call_id`（P1-6）：一次**逻辑调用**的标识，**跨 attempt 不变**。
          重试前先按它查「上次到底发生没发生」，是幂等的前提（写路径另用
          `idempotency_key()` 构键）。
        """
        attempts = max(1, int(attempts))
        self._enter(key)
        spent = 0.0
        t0 = self._clock()
        try:
            for attempt in range(attempts):
                try:
                    out = fn()
                except BaseException as exc:  # noqa: BLE001 —— 要原样重抛，仅在此处拦
                    cls = classify_error(exc)
                    self._record(key, exc, limited=(cls["kind"] == "rate_limit"))
                    last_attempt = attempt >= attempts - 1
                    action = "abort" if (last_attempt or not cls["retryable"]) else "retry"
                    delay = 0.0
                    if action == "retry":
                        delay = backoff_delay(attempt, cls["retry_after_s"],
                                              jitter=self._rand())
                        if spent + delay > sleep_budget_s:
                            # 预算不够睡这次 —— 如实放弃重试（调用方看到的是最后一次的真异常）
                            action, delay = "budget_exhausted", 0.0
                    _emit_attempt(record, call_id=call_id, attempt=attempt, kind=cls["kind"],
                                  action=action, retryable=cls["retryable"],
                                  retry_after_s=cls["retry_after_s"],
                                  delay_s=round(delay, 3),
                                  slept_s=round(delay, 3) if action == "retry" else 0.0,
                                  elapsed_ms=int((self._clock() - t0) * 1000), ok=False)
                    if action != "retry":
                        raise
                    spent += delay
                    self._sleep(delay)
                    continue
                self._record_ok(key)
                _emit_attempt(record, call_id=call_id, attempt=attempt, kind="ok",
                              action="return", retryable=False, retry_after_s=None,
                              delay_s=0.0, slept_s=0.0,
                              elapsed_ms=int((self._clock() - t0) * 1000), ok=True)
                return out
            raise AssertionError("unreachable")  # pragma: no cover
        finally:
            self._leave(key)

    # ── 内部：状态机 ──────────────────────────────────────────────────
    def _refresh(self, st: Dict[str, Any]) -> Dict[str, Any]:
        """把超时的 OPEN 推进到 HALF_OPEN。**必须持锁调用**，返回该状态字典本身。"""
        if st["state"] == OPEN and self._clock() - (st["opened_at"] or 0.0) >= RECOVERY_TIMEOUT_S:
            st["state"] = HALF_OPEN
            st["half_open_busy"] = False
            st["successes"] = 0
        return st

    def _blank(self) -> Dict[str, Any]:
        return {"state": CLOSED, "failures": 0, "successes": 0, "opened_at": None,
                "last_failure_at": None, "last_error": None, "rate_limited_hits": 0,
                "calls": 0, "rejected": 0, "half_open_busy": False}

    def _enter(self, key: str) -> None:
        """进门：判定是否放行。半开态只放一个探针，其余直接拒。"""
        with self._lock:
            st = self._st.setdefault(key, self._blank())
            self._refresh(st)
            st["calls"] += 1
            if st["state"] == OPEN:
                st["rejected"] += 1
                raise BreakerOpen(key)
            if st["state"] == HALF_OPEN:
                if st["half_open_busy"]:
                    st["rejected"] += 1
                    raise BreakerOpen(key)
                st["half_open_busy"] = True

    def _leave(self, key: str) -> None:
        """出门（含抛异常那条路）：半开探针的占位必须释放，否则一次网络卡死
        就让这个 key 永远停在 HALF_OPEN —— 那比一直 OPEN 还糟（永远不自愈）。"""
        with self._lock:
            st = self._st.get(key)
            if st:
                st["half_open_busy"] = False

    def _record(self, key: str, exc: BaseException, *, limited: bool) -> None:
        with self._lock:
            st = self._st.setdefault(key, self._blank())
            if limited:
                # 429 不计失败（模块 docstring「关键设计」）——只记账。
                st["rate_limited_hits"] += 1
                st["last_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
                st["last_failure_at"] = self._clock()
                return
            st["failures"] += 1
            st["last_failure_at"] = self._clock()
            st["last_error"] = "%s: %s" % (type(exc).__name__, str(exc)[:160])
            if st["state"] == HALF_OPEN:
                st["state"] = OPEN
                st["opened_at"] = self._clock()
                st["successes"] = 0
            elif st["state"] == CLOSED and st["failures"] >= FAILURE_THRESHOLD:
                st["state"] = OPEN
                st["opened_at"] = self._clock()
                st["successes"] = 0

    def _record_ok(self, key: str) -> None:
        with self._lock:
            st = self._st.setdefault(key, self._blank())
            if st["state"] == HALF_OPEN:
                st["successes"] += 1
                if st["successes"] >= SUCCESS_THRESHOLD:
                    st.update(self._blank())     # 干净复位（含 calls/rejected 归零）
            elif st["state"] == CLOSED:
                # 成功让失败计数**衰减**而非清零：偶发抖动不该永久累积到开闸，
                # 但连续成功理应让额度慢慢回来。
                st["failures"] = max(0, st["failures"] - 1)
                st["last_error"] = None

    def reset(self) -> None:
        """只给测试用（L0 必须在无副作用状态下可重复跑）。"""
        with self._lock:
            self._st.clear()


#: 进程内唯一实例。**刻意不是模块级散落的全局 dict** —— 测试要能整体替换掉它。
instance = CircuitBreakers()