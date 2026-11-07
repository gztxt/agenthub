#!/usr/bin/env python3
"""L0 hermetic：审计身份解析（writeauth.actor_of / write_gate 打 actor）。

2026-11-07 用户裁定后口径：准入只认「是否已登录」。
  · 浏览器 = HttpOnly 会话 Cookie `hub_session`
  · 机器   = HTTP Basic（用户名+密码）
共享 token（x-hub-token / x-term-token / ?token=）已整体删除，
所以本文件的红向钉子换成两条：
  ① 放行时 actor 必须是 `user:<真实用户名>`，不再是凭据名；
  ② 拒绝正文里绝不出现任何凭据片段，也不再引导用户去用 token。
"""
import asyncio
import os
import pathlib
import sys
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import writeauth  # noqa: E402

USER = {"id": 1, "username": "gztxt", "display": "超管", "is_admin": 1,
        "created_at": "", "updated_at": ""}


class TestActorOf(unittest.TestCase):
    def test_reads_state_actor(self):
        r = types.SimpleNamespace(state=types.SimpleNamespace(actor="user:gztxt"))
        self.assertEqual(writeauth.actor_of(r), "user:gztxt")

    def test_missing_state_is_system_and_never_raises(self):
        self.assertEqual(writeauth.actor_of(types.SimpleNamespace()), "system")
        self.assertEqual(writeauth.actor_of(None), "system")
        self.assertEqual(writeauth.actor_of(object()), "system")

    def test_empty_actor_is_system(self):
        r = types.SimpleNamespace(state=types.SimpleNamespace(actor=""))
        self.assertEqual(writeauth.actor_of(r), "system")


class _FakeUrl:
    def __init__(self, path, query=""):
        self.path, self.query = path, query


class _FakeReq:
    def __init__(self, method="POST", path="/api/agents"):
        self.method, self.url = method, _FakeUrl(path)
        self.headers = types.SimpleNamespace(raw=[])
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace()


class TestWriteGateStampsActor(unittest.TestCase):
    def _run(self, identity, users_ready=True, method="POST", path="/api/agents"):
        sentinel = object()

        async def call_next(req):
            return sentinel

        fake_users = types.SimpleNamespace(
            identity_of=lambda req: (identity, None, "basic" if identity else "anonymous"))
        fake_db = types.SimpleNamespace(is_open=lambda: users_ready)
        with mock.patch.dict(sys.modules, {"users": fake_users, "db": fake_db}):
            r = _FakeReq(method, path)
            out = asyncio.run(writeauth.write_gate(r, call_next))
        return out, r, sentinel

    def test_allow_stamps_username(self):
        out, r, sentinel = self._run(USER)
        self.assertIs(out, sentinel, "放行时必须真的走到 handler")
        self.assertEqual(r.state.actor, "user:gztxt")

    def test_anonymous_write_returns_401(self):
        out, r, _ = self._run(None)
        self.assertEqual(out.status_code, 401)
        self.assertFalse(hasattr(r.state, "actor"), "被拒的请求不该拿到身份")

    def test_users_db_down_returns_503(self):
        out, _, _ = self._run(None, users_ready=False)
        self.assertEqual(out.status_code, 503)

    def test_get_is_allowed_and_stamped_anonymous(self):
        out, r, sentinel = self._run(None, method="GET", path="/api/agents")
        self.assertIs(out, sentinel)
        self.assertEqual(r.state.actor, "user:anonymous",
                         "GET 不要求凭据，但身份仍要如实记为 anonymous")

    def test_exempt_path_stamps_user_exempt(self):
        out, r, sentinel = self._run(None, method="POST", path="/api/users/login")
        self.assertIs(out, sentinel)
        self.assertEqual(r.state.actor, "user:exempt")

    def test_response_never_echoes_credential(self):
        out, _, _ = self._run(None)
        body = out.body.decode()
        self.assertNotIn("x-hub-token", body)
        self.assertNotIn("TERM_TOKEN", body)


if __name__ == "__main__":
    unittest.main(verbosity=2)
