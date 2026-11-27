"""协同框架故障复盘（2026-11-27）L0 闸门：provider 级回退 / 错误文本 / 复核短路。

四条都来自同一批实弹数据（11-27 12:46~12:58 的两条 failed run）：
  1. `replan_error: LLM 网关 HTTP 401` —— hub 的 LLM 客户端只绑一个模型，WorkBuddy
     拒签就整条链路死，同刻 dumate/raccoon 实测 200（provider 级回退缺失）；
  2. 熔断键只有 `(BASE_URL,"llm")` —— 一家 401 把整条 magpie 通道熔断；
  3. `exit:1: Reading additional input from stdin...` ×4 —— 真错误在 stdout NDJSON 里，
     被 `(stderr or stdout)` 丢掉；
  4. `executor_claimed_failure; verifier insufficient (nonempty): no_output` ——
     执行者根本没回产物，却写成「verifier 也不足」两件事。

断言口径：**行为**（换没换、报什么、熔断键），不是实现细节。
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import llm  # noqa: E402
import resilience  # noqa: E402
import tasks_tiers as tiers  # noqa: E402
from adapters import cli_agent  # noqa: E402


def _breaker():
    """不真睡的熔断器（与 test_llm_resilience_l0 同一种替身，避免各写一套）。"""
    return resilience.CircuitBreakers(clock=lambda: 0.0, sleep=lambda s: None,
                                      rand=lambda: 0.0)


class TestModelCandidates(unittest.TestCase):
    """回退链的选型：坏 provider 不进链、广度优先、auto/* 不进链。"""

    def setUp(self):
        self._saved = {
            "models": getattr(__import__("magpie_client"), "models", None),
            "health": getattr(__import__("magpie_client"), "provider_health", None),
        }
        import magpie_client
        magpie_client.models = lambda ttl=None: (
            [{"id": "workbuddy/deepseek-v4.1-flash"},
             {"id": "workbuddy/glm-5.3-flash"},
             {"id": "raccoon/sn-sensenova-6-8-flash"},
             {"id": "dumate/deepseek-v4-flash-0731"},
             {"id": "auto/glm-5.3-flash"}], "")
        magpie_client.provider_health = lambda ttl=None: ({
            "workbuddy": {"state": "error", "reason": "sign-in refused"},
            "raccoon": {"state": "ok"},
            "dumate": {"state": "ok"},
        }, "")

    def tearDown(self):
        import magpie_client
        if self._saved["models"]:
            magpie_client.models = self._saved["models"]
        if self._saved["health"]:
            magpie_client.provider_health = self._saved["health"]

    def test_dead_provider_never_enters_chain(self):
        chain = llm.model_candidates("workbuddy/deepseek-v4.1-flash")
        self.assertEqual("workbuddy/deepseek-v4.1-flash", chain[0], "首选位永远是调用方指定")
        self.assertFalse([m for m in chain[1:] if m.startswith("workbuddy/")],
                         "同一死 provider 的其它模型不得作为回退位：换 ID 撞同一把锁")
        self.assertTrue(any(m.startswith("raccoon/") for m in chain[1:]))
        self.assertTrue(any(m.startswith("dumate/") for m in chain[1:]))

    def test_route_group_excluded(self):
        self.assertNotIn("auto/glm-5.3-flash", llm.model_candidates("dumate/x"))

    def test_health_unknown_does_not_block(self):
        import magpie_client
        magpie_client.provider_health = lambda ttl=None: (None, "网关不可达")
        chain = llm.model_candidates("workbuddy/deepseek-v4.1-flash")
        self.assertIn("workbuddy/glm-5.3-flash", chain,
                      "健康读不到 ≠ 坏：不知道时不挡（否则网关一抖整站无模型可用）")

    def test_breaker_key_is_per_provider(self):
        self.assertNotEqual(llm._llm_breaker_key("workbuddy/a"),
                            llm._llm_breaker_key("dumate/b"),
                            "一家 provider 的 401 不得熔断整条 magpie 通道")


class TestCreateMessageFallback(unittest.TestCase):
    """`_create_message` 的真实接线：401 换 provider、400 不换、链耗尽报全名单。"""

    def setUp(self):
        self._saved_inst = resilience.instance
        self._saved_post = llm._post_once
        self._saved_cand = llm.model_candidates
        self._saved_rec = llm._record_attempt
        self._saved_att = llm.LLM_ATTEMPTS
        resilience.instance = _breaker()
        llm.LLM_ATTEMPTS = 1
        llm._record_attempt = lambda rec: None
        llm.model_candidates = lambda preferred=None, limit=None: [
            preferred or "workbuddy/x", "dumate/deepseek-v4-flash-0731"]

    def tearDown(self):
        resilience.instance = self._saved_inst
        llm._post_once = self._saved_post
        llm.model_candidates = self._saved_cand
        llm._record_attempt = self._saved_rec
        llm.LLM_ATTEMPTS = self._saved_att

    def test_401_switches_provider(self):
        calls = []

        async def post(body, request_id):
            calls.append(body["model"])
            if body["model"].startswith("workbuddy/"):
                raise llm.LLMGatewayError(401, "WorkBuddy refused this account's sign-in")
            return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        llm._post_once = post
        data = asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        self.assertEqual([llm.MODEL, "dumate/deepseek-v4-flash-0731"], calls)
        self.assertEqual("dumate/deepseek-v4-flash-0731", data["_model_used"])

    def test_400_model_not_found_still_switches(self):
        """WorkBuddy 摘牌时的原话：`model [xxx] service info not found`（HTTP 400）。

        400 一般代表参数错（换哪家都一样），但**账号/模型级**的 400 是「这个模型不能用」。
        旧实现只按状态码判，这条就被记成终态失败（11-27 实弹同族）。
        """
        calls = []

        async def post(body, request_id):
            calls.append(body["model"])
            if body["model"].startswith("workbuddy/"):
                raise llm.LLMGatewayError(
                    400, 'WorkBuddy: model [x] service info not found')
            return {"content": [{"type": "text", "text": "ok"}], "usage": {}}

        llm._post_once = post
        data = asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        self.assertEqual(2, len(calls))
        self.assertEqual("dumate/deepseek-v4-flash-0731", data["_model_used"])

    def test_400_does_not_switch(self):
        calls = []

        async def post(body, request_id):
            calls.append(body["model"])
            raise llm.LLMGatewayError(400, "bad request")

        llm._post_once = post
        with self.assertRaises(llm.LLMGatewayError):
            asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        self.assertEqual(1, len(calls), "参数错不换模型：换哪家都一样")

    def test_chain_exhausted_reports_every_model(self):
        async def post(body, request_id):
            raise llm.LLMGatewayError(401, "refused this account's sign-in")

        llm._post_once = post
        with self.assertRaises(llm.LLMGatewayError) as cm:
            asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        text = str(cm.exception)
        self.assertIn("模型回退全部失败", text)
        self.assertIn(llm.MODEL, text)
        self.assertIn("dumate/deepseek-v4-flash-0731", text,
                      "台账必须看得见试过谁，不能只剩最后一个模型的报错")


class TestFailureTail(unittest.TestCase):
    """CLI 退出码非 0 时拼错误文本：结构化错误优先，不被 stderr 提示独占。"""

    def test_codex_structured_error_beats_stderr_noise(self):
        stdout = ('{"type":"thread.started","thread_id":"x"}\n'
                  '{"type":"error","message":"401 WorkBuddy refused this account\'s sign-in"}\n')
        tail = cli_agent._failure_tail("Reading additional input from stdin...", stdout)
        self.assertIn("401", tail)
        self.assertIn("refused this account", tail)
        self.assertIn("Reading additional input", tail,
                      "提示行仍保留，但不得是唯一内容")

    def test_plain_stderr_still_reported(self):
        self.assertEqual("boom", cli_agent._failure_tail("boom", ""))


class TestVerifyShortCircuit(unittest.TestCase):
    """执行者自述失败且无产物：不跑 verifier，原因写成单项。"""

    def test_is_model_failure_recognises_real_messages(self):
        self.assertTrue(tiers.is_model_failure(
            'exit:1: [claude-code:unrecognized_model] {"model":"workbuddy/glm-5.3-flash[1m]"}'))
        self.assertTrue(tiers.is_model_failure(
            "LLM 网关 HTTP 401: WorkBuddy refused this account's sign-in (401)"))

    def test_fallback_target_skips_dead_provider(self):
        import magpie_client
        saved_blocked = magpie_client.blocked_providers
        saved_model_agents = tiers._ccr_model_agents
        magpie_client.blocked_providers = lambda ttl=None: (
            {"workbuddy": "sign-in refused"}, "")
        tiers._ccr_model_agents = lambda: {"codex"}
        try:
            nxt = tiers.model_fallback_target(
                "codex", "t1", ["workbuddy/deepseek-v4.1-flash"],
                catalog={"workbuddy/glm-5.3-flash", "dumate/deepseek-v4-flash-0731"},
                catalog_checked=True,
                tiers_cfg={"t1": ["workbuddy/deepseek-v4.1-flash",
                                  "workbuddy/glm-5.3-flash",
                                  "dumate/deepseek-v4-flash-0731"]})
            self.assertEqual("dumate/deepseek-v4-flash-0731", nxt,
                             "换模型不能换到同一个死 provider")
        finally:
            magpie_client.blocked_providers = saved_blocked
            tiers._ccr_model_agents = saved_model_agents


if __name__ == "__main__":
    unittest.main()