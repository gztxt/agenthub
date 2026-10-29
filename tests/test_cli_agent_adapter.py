"""CLI Agent 适配器（v0.13.175 扩池）的 L0 闸门。

钉的是**结构不变量**，不是"某次跑通了"：
  · argv 把 prompt 当**单个参数**传（不拼 shell）—— 提示词里有反引号/分号时不许被解释；
  · 模型只对 model_path="ccr" 的 CLI 透传（codebuddy/cursor 塞 CCR ID 必报错）；
  · 退出码 0 但没正文 ⇒ 失败（否则"什么都没干"会被记成干完了）；
  · 超时杀进程组，且错误文案能区分 timeout / exit / empty 三类。
"""
import os
import pathlib
import stat
import sys
import tempfile
import types
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

from adapters import cli_agent  # noqa: E402
from adapters.cli_agent import CliAgentAdapter, SPECS  # noqa: E402


def _cfg():
    return types.SimpleNamespace()


class TestArgv(unittest.TestCase):
    def test_prompt_is_single_argv_element(self):
        p = "读 /tmp/a; rm -rf /  # `boom`"
        for aid, sp in SPECS.items():
            argv = sp["argv"](p, None)
            self.assertIn(p, argv, "%s 必须把 prompt 作为单个 argv 元素" % aid)
            self.assertFalse(any("sh" == a or a.endswith("/sh") for a in argv),
                             "%s 不许经 shell 解释" % aid)

    def test_each_spec_has_binary_and_cleaner(self):
        for aid, sp in SPECS.items():
            self.assertTrue(sp.get("bin"), aid)
            self.assertTrue(callable(sp.get("argv")), aid)
            self.assertTrue(callable(sp.get("clean")), aid)

    def test_headless_flags_are_the_measured_ones(self):
        """这些旗标是 2026-10-29 逐个实跑验过的，改动前请重跑一次。"""
        self.assertEqual(cli_agent._argv_claude("P", None), ["-p", "P", "--output-format", "text"])
        self.assertEqual(cli_agent._argv_codex("P", None)[:3], ["exec", "--skip-git-repo-check", "--json"])
        self.assertEqual(cli_agent._argv_dsh("P", None), ["headless", "P"])
        self.assertEqual(cli_agent._argv_pi("P", None), ["-p", "P"])
        self.assertEqual(cli_agent._argv_grok("P", None), ["-p", "P"])
        self.assertEqual(cli_agent._argv_hermes("P", None), ["-z", "P"])
        self.assertEqual(cli_agent._argv_omp("P", None), ["-p", "P"])
        self.assertEqual(cli_agent._argv_cursor("P", None), ["-p", "P"])


class TestModelPassing(unittest.TestCase):
    def test_native_model_domain_never_gets_ccr_id(self):
        for aid in ("codebuddy", "cursor"):
            ad = CliAgentAdapter(_cfg(), aid)
            self.assertIsNone(ad.resolve_model("alibaba/qwen3.8-max"),
                              "%s 的模型域是厂商自有清单，塞 CCR ID 必报错" % aid)

    def test_ccr_domain_passes_through(self):
        for aid in ("claude-code", "codex", "pi", "grok", "hermes", "omp"):
            ad = CliAgentAdapter(_cfg(), aid)
            self.assertEqual(ad.resolve_model("alibaba/qwen3.8-max"), "alibaba/qwen3.8-max")

    def test_pi_has_fallback_because_its_default_is_broken(self):
        ad = CliAgentAdapter(_cfg(), "pi")
        self.assertTrue(ad.resolve_model(None), "pi 的默认模型实测返回空响应，必须补一个")

    def test_approval_flags_per_cli(self):
        """免审批旗标是逐家验过的：改动前先跑一次真命令。"""
        os.environ["TASK_CLI_YOLO"] = "1"
        self.assertEqual(cli_agent._approve_flags("claude-code"),
                         ["--permission-mode", "bypassPermissions"])
        self.assertEqual(cli_agent._approve_flags("codex"), ["-s", "workspace-write"])
        self.assertEqual(cli_agent._approve_flags("hermes"), ["--yolo"])
        self.assertEqual(cli_agent._approve_flags("pi"), [], "pi 没有审批开关")

    def test_yolo_can_be_disabled(self):
        os.environ["TASK_CLI_YOLO"] = "0"
        try:
            self.assertEqual(cli_agent._approve_flags("codex"), [])
        finally:
            os.environ["TASK_CLI_YOLO"] = "1"

    def test_dsh_has_no_model_flag(self):
        ad = CliAgentAdapter(_cfg(), "dsh")
        self.assertIsNone(ad.resolve_model("alibaba/qwen3.8-max"))


class TestCleaners(unittest.TestCase):
    def test_codex_ndjson(self):
        out = "\n".join([
            '{"type":"thread.started","thread_id":"x"}',
            '{"type":"item.completed","item":{"type":"agent_message","text":"可用"}}',
            '{"type":"turn.completed","usage":{"input_tokens":10,"output_tokens":2}}',
        ])
        text, usage = cli_agent._clean_codex(out)
        self.assertEqual(text, "可用")
        self.assertEqual(usage["total_tokens"], 12)

    def test_pi_banner_stripped(self):
        out = "[mem] ✅ 已加载\n[hub-facade] ✅ 已加载\n可用"
        self.assertEqual(cli_agent._clean_pi(out)[0], "可用")

    def test_omp_working_line_stripped(self):
        self.assertEqual(cli_agent._clean_omp("Working...\n可用")[0], "可用")

    def test_dsh_reasoning_stripped(self):
        out = "dsh: reasoning:\n用户要两个字。\n\n\n可用"
        self.assertEqual(cli_agent._clean_dsh(out)[0], "可用")


class TestExec(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _fake(self, body: str, name: str = "fake") -> str:
        p = self.dir / name
        p.write_text("#!/bin/sh\n" + body, encoding="utf-8")
        p.chmod(p.stat().st_mode | stat.S_IEXEC)
        return str(p)

    def _adapter(self, bin_path: str, agent: str = "codex") -> CliAgentAdapter:
        ad = CliAgentAdapter(_cfg(), agent)
        ad.spec = dict(SPECS[agent], bin=bin_path, argv=lambda p, m, x=(): [p])
        ad._bin = None
        return ad

    async def test_success_returns_stdout(self):
        ad = self._adapter(self._fake('echo "结果是 42"'))
        r = await ad.chat("算一下", timeout_s=5)
        self.assertTrue(r["success"], r)
        self.assertIn("结果是 42", r["response"])

    async def test_empty_output_is_failure(self):
        ad = self._adapter(self._fake("exit 0"))
        r = await ad.chat("算一下", timeout_s=5)
        self.assertFalse(r["success"])
        self.assertIn("empty-output", r["error"])

    async def test_nonzero_exit_reports_code_and_stderr(self):
        ad = self._adapter(self._fake("echo 认证失败 >&2; exit 3"))
        r = await ad.chat("x", timeout_s=5)
        self.assertFalse(r["success"])
        self.assertIn("exit:3", r["error"])
        self.assertIn("认证失败", r["error"])

    async def test_timeout_is_distinguishable(self):
        ad = self._adapter(self._fake("sleep 30"))
        r = await ad.chat("x", timeout_s=0.4)
        self.assertFalse(r["success"])
        self.assertIn("timeout", r["error"])

    async def test_missing_binary_not_available(self):
        ad = CliAgentAdapter(_cfg(), "codex")
        ad.spec = dict(SPECS["codex"], bin=str(self.dir / "nope"))
        ad._bin = None
        ok, why = ad.available()
        self.assertFalse(ok)
        self.assertIn("binary_not_found", why)
        r = await ad.chat("x")
        self.assertFalse(r["success"])
        self.assertIn("binary_not_found", r["error"])


if __name__ == "__main__":
    unittest.main()
