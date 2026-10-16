#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P2 C2 —— ACL **检测与拦截分开**（`effect=enforce|shadow` 灰度）。

【这条判据治什么】
手册 C2：实时阻断必须确定/可解释/可回放，且新规则要能**先影子运行**再上任。
旧 `mcpgw` 只有「拦/不拦」两种结果 ⇒ 加一条 deny 规则就是**当场生效**，
没人能先看「它命中过谁、会拒掉什么」—— 规则一上线就可能把正常调用打死。

【本文件守五条】
1. **shadow 对生死零影响**：影子 deny 规则**不得**让调用被拒。
2. **但必须留证**：命中影子规则时记一条 `mcp_acl_shadow` 的 `would_deny`（可查「本应拒绝」）。
3. **enforce 照样拦**：实际规则仍按原语义 403（shadow 只会多说话，不会少拦）。
4. **旧行不变**：`effect` 缺列/空值/NULL 一律折算 `enforce`（灰度是**选择进入**的）。
5. 写入口校验：`effect` 只认 `enforce|shadow`，别的 400。

分层口径：临时 db + 替身 session，不起服务、不连网、零 SKIP。
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import types
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db       # noqa: E402
import mcpgw    # noqa: E402


class _FakeContent:
    def __init__(self, text="ok"):
        self.type = "text"
        self.text = text


class _FakeResult:
    def __init__(self):
        self.content = [_FakeContent()]
        self.is_error = False


class _FakeSession:
    async def call_tool(self, tool, args):
        return _FakeResult()


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0c2-"))
        self._saved_conn = db._conn
        db.init_db(self.tmp / "t.db")
        mcpgw.ensure_schema()
        db.execute("INSERT INTO mcp_servers(id,name,transport,command,args,env,url,enabled,"
                   "description,created_at,updated_at)"
                   " VALUES('s1','srv','stdio','echo','[]','{}',NULL,1,'','t','t')")
        self._saved = (mcpgw._session_for, mcpgw._check_rate)
        mcpgw._check_rate = lambda *a, **k: None

        async def session_for(server):
            return types.SimpleNamespace(aclose=_noop), _FakeSession()
        mcpgw._session_for = session_for
        self.addCleanup(self._restore)

    def _restore(self):
        mcpgw._session_for, mcpgw._check_rate = self._saved
        db._conn = self._saved_conn

    def _rule(self, *, allow, effect, agent="a1", pattern="*"):
        db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow,effect)"
                   " VALUES(?,NULL,?,?,?)", (agent, pattern, 1 if allow else 0, effect))

    def _call(self, tool="any_tool", agent="a1"):
        body = mcpgw.CallIn(agent_id=agent, server="s1", tool=tool, args={})
        return asyncio.run(mcpgw.mcp_call(body))

    def _shadow_events(self):
        return db.query("SELECT * FROM profile_events WHERE source='mcp_acl_shadow'")


async def _noop():
    return None


class TestShadowDoesNotBlock(_Base):
    def test_shadow_deny_does_not_block(self):
        self._rule(allow=False, effect="shadow")
        out = self._call()                      # 不抛即通过
        self.assertTrue(out["ok"])

    def test_shadow_deny_records_would_deny(self):
        self._rule(allow=False, effect="shadow")
        self._call()
        ev = self._shadow_events()
        self.assertEqual(1, len(ev))
        self.assertEqual("would_deny", ev[0]["status"])
        d = json.loads(ev[0]["detail"])
        self.assertIn("shadow", d["actual"])

    def test_enforce_deny_still_blocks(self):
        self._rule(allow=False, effect="enforce")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._call()
        self.assertEqual(403, cm.exception.status_code)

    def test_shadow_allow_rule_blocks_nothing_and_records_nothing(self):
        """影子 allow 规则不会造成「本应拒绝」，故不该留 would_deny。"""
        self._rule(allow=True, effect="shadow")
        self._call()
        self.assertEqual([], self._shadow_events())

    def test_enforce_deny_wins_over_shadow_allow(self):
        self._rule(allow=False, effect="enforce")
        self._rule(allow=True, effect="shadow")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._call()
        self.assertEqual(403, cm.exception.status_code, "影子规则不得把实际拒掉变成放行")


class TestLegacyRowsStayEnforced(_Base):
    def test_null_effect_is_treated_as_enforce(self):
        self._rule(allow=False, effect="enforce")
        db.execute("UPDATE mcp_acl SET effect=NULL")
        from fastapi import HTTPException
        with self.assertRaises(HTTPException):
            self._call()          # 旧行/空值必须仍是 enforce

    def test_missing_column_rows_default_enforce(self):
        self.assertEqual("enforce", mcpgw._acl_effect({}))
        self.assertEqual("enforce", mcpgw._acl_effect({"effect": ""}))
        self.assertEqual("shadow", mcpgw._acl_effect({"effect": "shadow"}))


class TestWriteValidation(_Base):
    def test_bad_effect_rejected(self):
        from fastapi import HTTPException
        from starlette.requests import Request  # noqa: F401  仅为类型存在性
        body = mcpgw.AclIn(agent_id="a1", tool_pattern="*", server_id="s1",
                           allow=False, effect="nonsense")
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(mcpgw.add_acl(body, _fake_request()))
        self.assertEqual(400, cm.exception.status_code)

    def test_shadow_rule_can_be_added(self):
        body = mcpgw.AclIn(agent_id="a1", tool_pattern="*", server_id="s1",
                           allow=False, effect="shadow")
        asyncio.run(mcpgw.add_acl(body, _fake_request()))
        rows = db.query("SELECT effect FROM mcp_acl")
        self.assertEqual(["shadow"], [r["effect"] for r in rows])


class _FakeReq:
    def __init__(self):
        self.headers = {}
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="test")


def _fake_request():
    return _FakeReq()


if __name__ == "__main__":
    unittest.main()