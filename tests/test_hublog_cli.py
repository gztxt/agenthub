"""hublog-cli（v0.13.48）：给操作 agent 的直查入口——取数与失败码必须可断言。

重点不是"能不能发请求"，而是三条铁律：
  ① 关键字只走 urlencode（自由输入绝不能拼进命令行/裸串）；
  ② 口令只进 Authorization 头（HTTP Basic），任何输出（含报错）都不许带它；
  ③ 失败要有可判别的退出码（鉴权 2 / 网络 3 / 参数 4 / 服务端 5），
     否则子代理拿到一堆 200 空输出会当成"没日志"而不是"没权限"。
"""
import importlib.util
import io
import os
import tempfile
import unittest
import urllib.error
from contextlib import redirect_stderr, redirect_stdout
from unittest import mock

_HERE = os.path.dirname(os.path.abspath(__file__))
_SCRIPT = os.path.join(os.path.dirname(_HERE), "scripts", "hublog-cli.py")

_spec = importlib.util.spec_from_file_location("hublog_cli", _SCRIPT)
hublog_cli = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(hublog_cli)

PC = "S3cr3t-Passcode-Do-Not-Echo"
_AUTH = {"Authorization": "Basic dGVzdDp4"}


def _tmp_env(text):
    fh = tempfile.NamedTemporaryFile("w", suffix=".env", delete=False, encoding="utf-8")
    fh.write(text); fh.close()
    return fh.name


class TestEnvParsing(unittest.TestCase):
    def test_parses_quotes_comments_and_blanks(self):
        p = _tmp_env("# 注释\n\nHUB_PASSCODE='abc def'\nPORT=\"3199\"\nBARE=1\n")
        env = hublog_cli._read_env(p)
        self.assertEqual(env["HUB_PASSCODE"], "abc def")
        self.assertEqual(env["PORT"], "3199")
        self.assertEqual(env["BARE"], "1")
        os.unlink(p)

    def test_missing_file_is_empty_dict(self):
        self.assertEqual(hublog_cli._read_env("/nonexistent/.env-nope"), {})

    def test_env_path_follows_symlink_to_real_repo(self):
        """~/bin/hublog 是软链接：仓库根必须按 realpath 算，否则读到 home/.env。"""
        repo = os.path.dirname(os.path.dirname(os.path.realpath(_SCRIPT)))
        self.assertTrue(os.path.isdir(repo), repo)
        # 2026-10-08 目录改名 agent-hub → agenthub：这里断言的是「realpath 落在真仓库根」，
        # 不是旧目录名本身，故按白名单认两个仓根别名，realpath 判据不动。
        # 2026-10-08 补worktree 形态：隔离工作区目录名带 `-wt-<id>` 后缀（如
        # agenthub-wt-record / agent-hub-wt-01a0ed3a），realpath 会落在 worktree 根而非
        # 主仓根 ⇒ 原白名单在任何 worktree 里都红。这是断言自身没覆盖形态，不是产品缺陷。
        base = os.path.basename(repo)
        stem = base.split("-wt-")[0]
        self.assertIn(stem, ("agenthub", "agent-hub"),
                      "软链接要用 realpath 解析，不能停在链接那一侧（当前仓根=%s）" % base)
        self.assertIn("realpath", open(_SCRIPT, encoding="utf-8").read().lower())


class TestUrlBuilding(unittest.TestCase):
    def test_free_text_is_urlencoded(self):
        url = hublog_cli._build_url("http://127.0.0.1:3102",
                                    {"q": "a b; rm -rf / & x", "limit": 5})
        self.assertNotIn(" ", url, "空格必须被编码，否则命令行/HTTP 解析都会歪")
        self.assertNotIn(";", url)
        self.assertIn("rm+-rf", url.replace("%20", "+") if "%20" not in url else url)
        self.assertIn("limit=5", url)

    def test_passcode_goes_to_header_only(self):
        """口令只进 Authorization 头：URL 会进访问日志/history，头不会。"""
        captured = {}

        class _Resp:
            status = 200

            def read(self): return b"ok\n"

            def __enter__(self): return self

            def __exit__(self, *a): return False

        def _spy(req, timeout=None):
            captured["url"] = req.full_url
            captured["headers"] = dict(req.headers)
            return _Resp()

        with mock.patch.object(hublog_cli.urllib.request, "urlopen", _spy):
            status, body = hublog_cli._fetch("http://127.0.0.1:3102/api/hublog", _AUTH)
        self.assertEqual(status, 200)
        self.assertEqual(captured["headers"].get("Authorization"), _AUTH["Authorization"])
        self.assertNotIn(PC, captured["url"], "口令只在请求头里，URL 会被日志/历史记下")
        self.assertNotIn("passcode", captured["url"])


class TestFetchAndExitCodes(unittest.TestCase):
    def _run(self, argv, status, body):
        with mock.patch.object(hublog_cli, "_fetch", return_value=(status, body)):
            err = io.StringIO(); out = io.StringIO()
            try:
                with redirect_stderr(err), redirect_stdout(out):
                    hublog_cli.main(argv)
                code = 0
            except SystemExit as e:
                code = e.code
        return code, out.getvalue(), err.getvalue()

    def test_401_means_auth_exit2_without_echoing_passcode(self):
        env = _tmp_env("HUB_SUPERADMIN_PW=%s\nPORT=3102\n" % PC)
        code, out, err = self._run(
            ["--env", env, "--source", "error"], 401, '{"detail":"bad token"}')
        self.assertEqual(code, hublog_cli.EXIT_AUTH)
        self.assertNotIn(PC, err + out, "报错里不许回显口令")
        os.unlink(env)

    def test_503_means_misconfig_exit2(self):
        """503 = 服务端用户库不可用（fail-closed），必须报「先修什么」而不是只给 HTTP 码。"""
        env = _tmp_env("HUB_SUPERADMIN_PW=%s\n" % PC)
        code, _o, err = self._run(["--env", env], 503, "misconfig")
        self.assertEqual(code, hublog_cli.EXIT_AUTH)
        self.assertIn("用户库不可用", err)
        os.unlink(env)

    def test_network_failure_exit3(self):
        env = _tmp_env("HUB_SUPERADMIN_PW=%s\n" % PC)
        code, _o, err = self._run(["--env", env], 0, "Connection refused")
        self.assertEqual(code, hublog_cli.EXIT_NET)
        self.assertIn("连不上", err)
        os.unlink(env)

    def test_bad_param_exit4_surfaces_server_reason(self):
        env = _tmp_env("HUB_SUPERADMIN_PW=%s\n" % PC)
        code, _o, err = self._run(["--env", env, "--subject", "rm -rf"],
                                  400, "subject must be one of [...]")
        self.assertEqual(code, hublog_cli.EXIT_PARAM)
        self.assertIn("subject", err, "400 的原因必须透出来（白名单里有哪些）")
        os.unlink(env)

    def test_happy_path_prints_body(self):
        env = _tmp_env("HUB_SUPERADMIN_PW=%s\n" % PC)
        code, out, _e = self._run(["--env", env], 200, "2026-09-27T16:00:00+0800 [ERRO] x\n")
        self.assertEqual(code, 0)
        self.assertIn("[ERRO]", out)
        os.unlink(env)

    def test_missing_passcode_refuses_before_calling_server(self):
        env = _tmp_env("PORT=3102\n")
        with mock.patch.object(hublog_cli, "_fetch", side_effect=AssertionError("不该发请求")):
            err = io.StringIO()
            try:
                with redirect_stderr(err):
                    hublog_cli.main(["--env", env])
                code = 0
            except SystemExit as e:
                code = e.code
        self.assertEqual(code, hublog_cli.EXIT_AUTH, "没口令就别发请求，直接判鉴权失败")
        os.unlink(env)

    def test_httperror_body_is_still_readable(self):
        """4xx/5xx 的原因写在响应体里，urlopen 会抛 —— 必须读出来再返回。"""
        body = b'{"detail":"nope"}'
        with mock.patch.object(hublog_cli.urllib.request, "urlopen",
                               side_effect=urllib.error.HTTPError(
                                   "u", 400, "Bad Request", {}, io.BytesIO(body))):
            status, text = hublog_cli._fetch("http://x/api/hublog", _AUTH)
        self.assertEqual(status, 400)
        self.assertIn("nope", text)


if __name__ == "__main__":
    unittest.main()
