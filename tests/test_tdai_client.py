"""L0 · TDAI 客户端（空 HOME 可跑，零网络 —— 用注入的假 aiohttp 走全部分支）。

本模块存在的理由是「查不到时必须说得出为什么」，所以测试的重心不在"能查通"，
而在**每一条失败路径都会产出结构化 error，且绝不静默、绝不泄露凭据**。
三层各守一种失效：
  A 凭据解析      —— 守"把『我没拿到 key』误判成『服务挂了』"（0924 原文的四重错之一）
  B 本地拒收      —— 守契约前置校验（空串 / 超长 / 缺 service_id），省一次往返
  C 传输与信封    —— 守 HTTP≠200、code≠0、连接失败、超时四类，且响应里永不出现 apiKey
"""
import asyncio
import json
import sys
import types
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import tdai_client  # noqa: E402

FAKE_KEY = "sk" + "-FAKE-DO-NOT-LEAK-0123456789"  # 运行时拼接：源码不出现 sk- 连写（推前闸门①），值仍为 sk- 形态以验脱敏


# ── 假 aiohttp：按脚本决定返回什么 / 抛什么 ─────────────────────────

class _Resp:
    def __init__(self, status=200, body=None, text_body=None):
        self.status = status
        self._body = body
        self._text = text_body if text_body is not None else json.dumps(body or {})

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class ClientError(Exception):
    pass


class ClientConnectorError(ClientError):
    pass


class ServerTimeoutError(ClientError):
    pass


class _Session:
    def __init__(self, timeout=None, **kw):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _go(self, verb, url, **kw):
        script = _STATE["script"]
        rec = _STATE["record"]
        rec.append({"verb": verb, "url": url, **kw})
        if isinstance(script, Exception):
            raise script
        return _Resp(status=script.get("status", 200), body=script.get("body"),
                     text_body=script.get("text"))

    def post(self, url, **kw):
        return self._go("POST", url, **kw)

    def get(self, url, **kw):
        return self._go("GET", url, **kw)


class ClientTimeout:
    def __init__(self, total=None):
        self.total = total


_STATE = {"script": None, "record": None}


def install_fake_aiohttp():
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = ClientTimeout
    m.ClientSession = _Session
    m.ClientError = ClientError
    m.ClientConnectorError = ClientConnectorError
    m.ServerTimeoutError = ServerTimeoutError
    sys.modules["aiohttp"] = m


install_fake_aiohttp()


def run(coro):
    """跑一个协程并**关掉 loop**——不然在 unittest discover 里会污染成全局 ResourceWarning。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


class TdaiBase(unittest.TestCase):
    def setUp(self):
        _STATE["record"] = []
        # 查询缓存是**进程级全局**的（PT-20261024-08 C3）⇒ 不清就会把上一个用例的
        # 成功结果漏给下一个用例（实测：test_auth_headers 缓存了空结果，
        # 后面 7 个用例的脚本全被挡住）。与重置 _STATE/env 同一个位置。
        tdai_client.clear_query_cache()
        self._env = {k: v for k, v in os_environ_snapshot().items()}
        for k in ("TDAI_URL", "TDAI_API_KEY", "TDAI_SERVICE_ID"):
            import os
            os.environ.pop(k, None)
        import os
        os.environ["TDAI_URL"] = "http://127.0.0.1:8420"
        os.environ["TDAI_API_KEY"] = FAKE_KEY
        os.environ["TDAI_SERVICE_ID"] = "default"

    def tearDown(self):
        import os
        for k in ("TDAI_URL", "TDAI_API_KEY", "TDAI_SERVICE_ID"):
            os.environ.pop(k, None)


def os_environ_snapshot():
    import os
    return dict(os.environ)


# ── A 凭据解析 ────────────────────────────────────────────────────────

class TestCreds(TdaiBase):
    def test_env_wins(self):
        c = tdai_client.resolve_creds()
        self.assertEqual(c["api_key"], FAKE_KEY)
        self.assertEqual(c["service_id"], "default")
        self.assertTrue(c["source"].startswith("env"))

    def test_missing_key_is_reported_not_silently_empty(self):
        """核心不变式：没有 key 时**不能**伪装成"服务返回了空结果"。
        0924 原实现就是这个区别被抹平，导致 21 天无人发现。"""
        import os
        os.environ.pop("TDAI_API_KEY")
        os.environ.pop("TDAI_SERVICE_ID")
        tdai_client.CREDS_PATH = Path("/nonexistent/memory-tencentdb.json")
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["items"], [])
        self.assertIn("凭据缺失", r["error"])
        self.assertEqual(r["http"], None)          # 根本没发出去，不能谎报状态码
        self.assertIn("creds_source", r)


# ── B 本地拒收 ────────────────────────────────────────────────────────

class TestLocalReject(TdaiBase):
    def test_empty_query(self):
        r = run(tdai_client.search_memories("   "))
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "query 为空")
        self.assertEqual(len(_STATE["record"]), 0)   # 断言"一次请求都没发"

    def test_oversize_query(self):
        r = run(tdai_client.search_memories("x" * 2049))
        self.assertFalse(r["ok"])
        self.assertIn("超长", r["error"])
        self.assertEqual(len(_STATE["record"]), 0)

    def test_limit_clamped_to_contract(self):
        _STATE["script"] = {"body": {"code": 0, "message": "ok", "data": {"items": []}}}
        run(tdai_client.search_memories("端口", limit=9999))
        self.assertEqual(_STATE["record"][0]["json"]["limit"], 100)


# ── C 传输与信封 ──────────────────────────────────────────────────────

class TestTransport(TdaiBase):
    def test_ok_and_normalization(self):
        _STATE["script"] = {"body": {"code": 0, "message": "ok", "request_id": "req-1",
                                     "data": {"items": [{"record_id": "m_1", "content": "端口基线",
                                                         "type": "fact", "score": 0.76,
                                                         "scene_name": "ops",
                                                         "created_at": "2026-09-20T10:00:00Z"}]}}}
        r = run(tdai_client.search_memories("端口"))
        self.assertTrue(r["ok"], r.get("error"))
        self.assertEqual(r["count"], 1)
        self.assertEqual(r["items"][0]["id"], "m_1")      # record_id → id
        self.assertEqual(r["items"][0]["score"], 0.76)
        self.assertEqual(r["request_id"], "req-1")

    def test_auth_headers_actually_sent(self):
        """路径/方法/两个头 —— 正是 0924 原实现错掉的四项，逐条钉死。"""
        _STATE["script"] = {"body": {"code": 0, "data": {"items": []}}}
        run(tdai_client.search_memories("端口"))
        sent = _STATE["record"][0]
        self.assertEqual(sent["verb"], "POST")
        self.assertTrue(sent["url"].endswith("/v2/atomic/search"))
        h = sent["headers"]
        self.assertEqual(h["Authorization"], f"Bearer {FAKE_KEY}")
        self.assertEqual(h["x-tdai-service-id"], "default")

    def test_http_404_is_failure_not_empty_success(self):
        _STATE["script"] = {"status": 404, "body": None,
                            "text": '{"error":"Not found: GET /memory/search"}'}
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertEqual(r["http"], 404)
        self.assertIn("HTTP 404", r["error"])

    def test_business_code_nonzero_is_failure(self):
        """HTTP 200 + code!=0 必须算失败：TDAI 的契约在信封里，不在状态码里。"""
        _STATE["script"] = {"body": {"code": 400, "message": "team_id: Invalid input"}}
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertIn("code=400", r["error"])
        self.assertIn("team_id", r["error"])

    def test_connector_error_maps_to_connect_fail(self):
        _STATE["script"] = ClientConnectorError("refused")
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertIn("连接失败", r["error"])

    def test_timeout_maps_to_timeout(self):
        _STATE["script"] = ServerTimeoutError()
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertIn("超时", r["error"])

    def test_non_json_body_is_failure(self):
        _STATE["script"] = {"body": None, "text": "<html>502</html>"}
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertIn("响应非 JSON", r["error"])

    def test_never_leaks_api_key(self):
        """任何返回路径（含错误路径）都不得出现 apiKey 字面值。"""
        cases = [{"body": {"code": 0, "data": {"items": []}}},
                 {"status": 401, "body": None, "text": "bad token"},
                 {"body": {"code": 403, "message": f"key {FAKE_KEY} rejected"}},
                 ClientConnectorError(FAKE_KEY)]
        for c in cases:
            _STATE["script"] = c
            r = run(tdai_client.search_memories("端口"))
            self.assertNotIn(FAKE_KEY, json.dumps(r, ensure_ascii=False),
                             f"凭据泄露于：{r}")

    def test_leak_guard_survives_non_standard_key_shape(self):
        """sk- 正则只防常见形态；形态不同的 key 必须靠**精确替换**兜住。
        上游一旦改 key 格式，这条会提醒我们正则不是万能的。"""
        import os
        odd = "Zm9vYmFyQkFSMjMyMw=="
        os.environ["TDAI_API_KEY"] = odd
        _STATE["script"] = {"body": {"code": 403, "message": f"invalid {odd}"}}
        r = run(tdai_client.search_memories("端口"))
        self.assertFalse(r["ok"])
        self.assertNotIn(odd, json.dumps(r, ensure_ascii=False), f"凭据泄露于：{r}")
        self.assertIn("<redacted>", r["error"])

    def test_leak_guard_survives_key_in_body_text(self):
        """HTTP 非 200 时整块正文会被搬进 error，上游网关常把请求头回显在 401 页里。"""
        import os
        odd = "Zm9vYmFyQkFSMjMyMw=="
        os.environ["TDAI_API_KEY"] = odd
        _STATE["script"] = {"status": 401, "body": None,
                            "text": f"Authorization: Bearer {odd} rejected"}
        r = run(tdai_client.search_memories("端口"))
        self.assertNotIn(odd, json.dumps(r, ensure_ascii=False), f"凭据泄露于：{r}")

    def test_creds_summary_has_no_secret(self):
        s = tdai_client.creds_summary()
        self.assertNotIn("api_key", s)
        self.assertTrue(s["has_key"])
        self.assertEqual(s["key_len"], len(FAKE_KEY))

    def test_reachable_uses_unauthenticated_health(self):
        """/health 免鉴权 ⇒ 能用它区分「服务没起」与「我没凭据」。"""
        _STATE["script"] = {"body": {"status": "ok", "stores": {"vectorStore": True}}}
        r = run(tdai_client.reachable())
        self.assertTrue(r["ok"])
        self.assertNotIn("headers", _STATE["record"][0])   # 不该带凭据


if __name__ == "__main__":
    unittest.main()
