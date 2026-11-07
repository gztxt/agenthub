#!/usr/bin/env python3
"""L0 hermetic：审计查询端点的鉴权与过滤（src/audit.py）。

鉴权口径照抄 /api/sessions/export（src/main.py:531）的既有先例：**GET 但按写方法判**
（批量读审计行＝数据外流动作）。红向钉子：无凭据必须 401 且**不回显任何凭据**；
服务端未配口令必须 503（fail-closed，不是放行）。
"""
import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import audit  # noqa: E402
import db     # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Req:
    def __init__(self, token=""):
        self.method = "GET"
        self.url = types.SimpleNamespace(path="/api/audit/list", query="")
        self.headers = types.SimpleNamespace(
            raw=[(b"x-hub-token", token.encode())] if token else [])
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="user:term-token")


class TestAuditApi(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0auditapi-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        db.log_asset_event("mcp_server", "s1", "create", "user:term-token", {"name": "a"})
        db.log_asset_event("mcp_acl", "9", "bind", "user:hub-passcode", {"agent_id": "pi"})
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, token="good", **kw):
        args = {"request": _Req(token), "asset_type": None, "asset_slug": None, "limit": 100}
        args.update(kw)
        return asyncio.run(audit.audit_list(**args))

    def _env(self, term="good", passcode=""):
        return mock.patch.dict(os.environ, {"TERM_TOKEN": term, "HUB_PASSCODE": passcode})

    def test_no_token_is_401_and_silent(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
        self.assertEqual(cm.exception.status_code, 401)
        self.assertNotIn("good", str(cm.exception.detail))

    def test_legacy_token_header_is_not_a_credential(self):
        """2026-11-07 用户裁定删除共享 token 兼容面：真实解析器只认 Cookie/Basic。"""
        import authfix
        req = types.SimpleNamespace(headers={"x-hub-token": "good"})
        u, _p, how = authfix.real_identity_of(req)
        self.assertIsNone(u, "x-hub-token 仍被当成凭据 —— 兼容面没删干净")
        self.assertEqual(how, "anonymous")

    def test_userdb_closed_is_503_not_allowed(self):
        with mock.patch.object(db, "is_open", lambda: False):
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
        self.assertEqual(cm.exception.status_code, 503, "fail-closed：用户库不可用不等于放行")

    def test_authorized_lists_newest_first(self):
        with self._env():
            d = self._call(token="good")
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["audit"][0]["asset_type"], "mcp_acl", "必须 id DESC（最新在前）")
        self.assertEqual(d["types"], list(audit.VALID_TYPES))

    def test_filter_by_type_and_slug(self):
        with self._env():
            self.assertEqual(self._call(token="good", asset_type="mcp_server")["count"], 1)
            self.assertEqual(self._call(token="good", asset_slug="9")["count"], 1)
            self.assertEqual(self._call(token="good", asset_type="mcp_acl", asset_slug="9")["count"], 1)
            self.assertEqual(self._call(token="good", asset_type="mcp_acl", asset_slug="1")["count"], 0)

    def test_bad_type_is_400(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="good", asset_type="not_a_type")
        self.assertEqual(cm.exception.status_code, 400)

    def test_limit_is_clamped_not_exploded(self):
        with self._env():
            self.assertEqual(self._call(token="good", limit=999999)["count"], 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
