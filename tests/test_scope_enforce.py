"""P0-3 · scope enforce（默认关）+ 受限凭据签发 + /api/meta 公共读链路。

L0 口径：不 import src.main、不起服务、不打网络。

四层各守一种失效：
  A enforce_verdict 真值表：只有「开 + 显式受限会话 + 缺 scope + 副作用端点」才拒
  B issue_session 真的把 scopes 写进 user_credentials（默认 NULL，升级零感知）
  C write_gate 接上 enforce：默认关放行；开后 /api/system/restart 对只读会话 403
  D /api/meta 非回环匿名可达，且**经过真实 read_gate 中间件**（不是裸 app）：
    p4 子代理的 contract-only 裸 app 测不到这层，这里补上；反向用例证明放行面没扩大。
"""
import io
import json
import os
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import authz  # noqa: E402
import users  # noqa: E402
import writeauth  # noqa: E402

SIDE = "/api/system/restart"          # 22 条副作用之一 → hub.ops.write
SIDE_TEMPLATED = "/api/tasks/run-1/start"   # 带参副作用 → hub.task.write
PLAIN = "/api/memory/l3"              # 写端点但非副作用 → hub.memory.write


class AEnforceVerdict(unittest.TestCase):
    def test_disabled_always_allows(self):
        for scopes in (None, frozenset(), frozenset({"hub.memory.write"})):
            ok, tag, _, _ = authz.enforce_verdict("POST", SIDE, scopes, False)
            self.assertTrue(ok)
            self.assertEqual(tag, "disabled")

    def test_legacy_null_scopes_allows_even_when_enabled(self):
        ok, tag, _, _ = authz.enforce_verdict("POST", SIDE, None, True)
        self.assertTrue(ok)
        self.assertEqual(tag, "legacy")

    def test_missing_scope_on_side_effect_denied(self):
        ok, tag, need, _ = authz.enforce_verdict("POST", SIDE, frozenset(), True)
        self.assertFalse(ok)
        self.assertEqual(tag, "deny")
        self.assertEqual(need, "hub.ops.write")

    def test_missing_scope_on_plain_write_still_allowed(self):
        ok, tag, _, _ = authz.enforce_verdict("PUT", PLAIN, frozenset(), True)
        self.assertTrue(ok)
        self.assertEqual(tag, "would_deny_but_not_side_effect")

    def test_holding_scope_allowed(self):
        ok, tag, _, _ = authz.enforce_verdict(
            "POST", SIDE, frozenset({"hub.ops.write"}), True)
        self.assertTrue(ok)
        self.assertEqual(tag, "would_allow")

    def test_templated_side_effect_path_denied(self):
        ok, _, need, _ = authz.enforce_verdict("POST", SIDE_TEMPLATED,
                                               frozenset(), True)
        self.assertFalse(ok)
        self.assertEqual(need, "hub.task.write")

    def test_is_side_effect_path_exact_and_template(self):
        self.assertTrue(authz.is_side_effect_path("POST", SIDE))
        self.assertTrue(authz.is_side_effect_path("POST", SIDE_TEMPLATED))
        self.assertFalse(authz.is_side_effect_path("PUT", PLAIN))
        self.assertFalse(authz.is_side_effect_path("POST", "/api/nope"))


class BIssueScopedCredential(unittest.TestCase):
    def test_issue_session_default_writes_null(self):
        with mock.patch("db.execute") as ex, mock.patch("db._now", return_value="t"):
            users.issue_session({"id": 7})
        sql, args = ex.call_args[0]
        self.assertIn("scopes", sql)
        self.assertIsNone(args[-1], "默认不传 scopes 必须落 NULL＝沿用现行")

    def test_issue_session_writes_json_scopes(self):
        with mock.patch("db.execute") as ex, mock.patch("db._now", return_value="t"):
            users.issue_session({"id": 7}, ["hub.memory.write", "hub.task.write"])
        raw = ex.call_args[0][1][-1]
        self.assertEqual(json.loads(raw), ["hub.memory.write", "hub.task.write"])
        self.assertEqual(users.parse_scopes(raw),
                         frozenset({"hub.memory.write", "hub.task.write"}))


class _Req:
    method = "POST"

    class url:
        path = SIDE

    class headers:
        raw = []

    class client:
        host = "100.64.1.2"      # 非回环

    class state:
        pass


class CWriteGateEnforce(unittest.IsolatedAsyncioTestCase):
    async def _run(self, scopes, environ):
        sentinel = object()

        async def nxt(_r):
            return sentinel

        buf = io.StringIO()
        # clear=True 把 HUB_AUTHZ_ENFORCE 从环境清掉，只留 environ 显式给的，
        # 否则开发 shell 的导出会混进结论（prod-env-verification 纪律）。
        with mock.patch("users.identity_of",
                        return_value=({"username": "u", "scopes": scopes}, None, "cookie")), \
             mock.patch("db.is_open", return_value=True), \
             mock.patch("approvals.ask_reason", return_value=None), \
             mock.patch.dict(os.environ, environ, clear=True), \
             redirect_stdout(buf):
            got = await writeauth.write_gate(_Req(), nxt)
        return got, sentinel, buf.getvalue()

    async def test_default_off_allows_even_scoped_session(self):
        got, sentinel, log = await self._run("[]", {})
        self.assertIs(got, sentinel, "默认关：干跑期绝不改变准入")
        self.assertIn("[writegate][dryrun]", log)

    async def test_enabled_and_missing_scope_returns_403(self):
        got, _, log = await self._run("[]", {"HUB_AUTHZ_ENFORCE": "1"})
        self.assertEqual(got.status_code, 403)
        self.assertIn("[writegate][enforce]", log)
        self.assertIn("required_scope", got.body.decode())

    async def test_enabled_but_holding_scope_allows(self):
        got, sentinel, _ = await self._run('["hub.ops.write"]',
                                           {"HUB_AUTHZ_ENFORCE": "1"})
        self.assertIs(got, sentinel)

    async def test_enabled_legacy_session_allows(self):
        got, sentinel, _ = await self._run(None, {"HUB_AUTHZ_ENFORCE": "1"})
        self.assertIs(got, sentinel)


class DMetaPublicRead(unittest.TestCase):
    """p4 的链路洞：裸 app 绕过 read_gate；这里挂真实中间件验证非回环匿名。"""

    @staticmethod
    def _app(extra_path=None):
        from fastapi import FastAPI
        app = FastAPI()

        @app.get("/api/meta")
        def _meta():
            return {"protocol": 1}

        if extra_path:
            @app.get(extra_path)
            def _other():
                return {"x": 1}
        app.middleware("http")(writeauth.read_gate)
        return app

    def test_non_loopback_anonymous_get_meta_is_200(self):
        from fastapi.testclient import TestClient
        with TestClient(self._app()) as c:     # client.host = "testclient" ⇒ 非回环
            r = c.get("/api/meta")
        self.assertEqual(r.status_code, 200, r.text)

    def test_other_non_loopback_read_still_denied(self):
        """反向：放行面没扩大 —— 别的非公共 GET 匿名仍被 read_gate 拒。"""
        from fastapi.testclient import TestClient
        with mock.patch("db.is_open", return_value=True), \
             TestClient(self._app("/api/secret")) as c:
            r = c.get("/api/secret")
        self.assertEqual(r.status_code, 401, r.text)


if __name__ == "__main__":
    unittest.main(verbosity=2)
