"""P0-2 · scopes 列 / 解析 / 干跑闸门的行为验证（L0，零网络零服务）。

守四层：
  A parse_scopes 语义（None=沿用现行 vs 空 frozenset=明确只读，不能混）
  B path→scope 反查无歧义、覆盖全表
  C dryrun_verdict 四种 tag，且**不改变准入**
  D 迁移幂等、不回填历史行；write_gate 里干跑日志真的落地且 verdict 仍是 allow
"""
import io
import re
import sqlite3
import sys
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import authz  # noqa: E402
import db  # noqa: E402
import users  # noqa: E402
import writeauth  # noqa: E402


class AParseScopes(unittest.TestCase):
    def test_none_and_empty_values_are_legacy(self):
        for raw in (None, "", 0, False):
            self.assertIsNone(users.parse_scopes(raw))

    def test_non_string_and_malformed_json_are_legacy(self):
        for raw in ("not-json", "[", '{"a":1}', 42, ["hub.memory.write"], {"a": 1}):
            self.assertIsNone(users.parse_scopes(raw), raw)

    def test_empty_array_is_explicit_empty_not_legacy(self):
        self.assertEqual(users.parse_scopes("[]"), frozenset())

    def test_valid_array_keeps_known_scopes(self):
        self.assertEqual(users.parse_scopes('["hub.memory.write","hub.task.write"]'),
                         frozenset({"hub.memory.write", "hub.task.write"}))

    def test_unknown_scopes_filtered_out(self):
        self.assertEqual(users.parse_scopes('["hub.memory.write","bogus","admin"]'),
                         frozenset({"hub.memory.write"}))


class BScopeByPath(unittest.TestCase):
    def test_index_size_and_no_ambiguity(self):
        # /install 同时出现在 skill.py 与 skill_store.py，但两者同属 skill.write
        # ⇒ 98 key（P3 事件出口 chat/events + interrupt 各 +1）。
        self.assertEqual(len(authz.SCOPE_BY_PATH), 98)

    def test_known_and_unknown(self):
        self.assertEqual(authz.scope_for_path("put", "/api/memory/l3"), "hub.memory.write")
        self.assertEqual(authz.scope_for_path("POST", "/api/system/restart"), "hub.ops.write")
        self.assertIsNone(authz.scope_for_path("POST", "/api/nope"))

    def test_every_declared_route_reachable(self):
        for (_mod, method, path), sc in authz.WRITE_ROUTE_SCOPES.items():
            self.assertEqual(authz.scope_for_path(method, path), sc)

    def test_templated_runtime_path_resolves(self):
        self.assertEqual(authz.scope_for_path("PUT", "/api/memory/l1/42"),
                         "hub.memory.write")
        self.assertEqual(authz.scope_for_path("POST", "/api/tasks/run-1/start"),
                         "hub.task.write")
        self.assertEqual(authz.scope_for_path("DELETE", "/api/agents/claude"),
                         "hub.agent.write")

    def test_all_declared_templates_resolve_with_concrete_id(self):
        """把每条声明里的 {param} 换成具体值后，运行时反查必须全部命中。
        守的就是「模板没编译 ⇒ 干跑把带参路由全报成 unscoped」这个漏。"""
        for (method, path) in authz.SCOPE_BY_PATH:
            concrete = re.sub(r"\{[^}]+\}", "42", path)
            self.assertIsNotNone(authz.scope_for_path(method, concrete),
                                 f"{method} {path} 具体化后反查不到")


class CDryrunVerdict(unittest.TestCase):
    def test_legacy_session_always_allowed(self):
        tag, need, _ = authz.dryrun_verdict("POST", "/api/system/restart", None)
        self.assertEqual((tag, need), ("legacy", "hub.ops.write"))

    def test_scoped_session_allow_deny(self):
        self.assertEqual(authz.dryrun_verdict("POST", "/mcp/call",
                         frozenset({"hub.mcp.write"}))[0], "would_allow")
        self.assertEqual(authz.dryrun_verdict("POST", "/mcp/call",
                         frozenset({"hub.memory.write"}))[0], "would_deny")

    def test_empty_scope_set_denies(self):
        self.assertEqual(authz.dryrun_verdict("PUT", "/api/memory/l3", frozenset())[0],
                         "would_deny")

    def test_undeclared_endpoint_flagged(self):
        self.assertEqual(authz.dryrun_verdict("POST", "/api/nope", None)[0], "unscoped")


class DMigration(unittest.TestCase):
    def test_add_column_idempotent_and_no_backfill(self):
        conn = sqlite3.connect(":memory:")
        conn.execute("CREATE TABLE user_credentials (id INTEGER, token_hash TEXT)")
        conn.execute("INSERT INTO user_credentials VALUES (1, 'abc')")
        db._add_column_if_missing(conn, "user_credentials", "scopes", "TEXT")
        db._add_column_if_missing(conn, "user_credentials", "scopes", "TEXT")
        cols = [r[1] for r in conn.execute("PRAGMA table_info(user_credentials)")]
        self.assertIn("scopes", cols)
        self.assertEqual(conn.execute("SELECT scopes FROM user_credentials").fetchone()[0], None)
        conn.close()

    def test_init_db_wires_the_migration(self):
        src = (SRC / "db.py").read_text(encoding="utf-8")
        self.assertIn('_add_column_if_missing(_conn, "user_credentials", "scopes", "TEXT")', src)

    def test_resolve_token_selects_the_column(self):
        src = (SRC / "users.py").read_text(encoding="utf-8")
        self.assertIn("c.scopes FROM user_credentials", src)


class _Req:
    method = "POST"

    class url:
        path = "/api/memory/l1"

    class headers:
        raw = []

    class client:
        host = "127.0.0.1"

    class state:
        pass


class EWriteGateDryrun(unittest.IsolatedAsyncioTestCase):
    async def test_allow_verdict_unchanged_and_dryrun_logged(self):
        sentinel = object()

        async def nxt(_r):
            return sentinel

        buf = io.StringIO()
        with mock.patch("users.identity_of",
                        return_value=({"username": "u", "scopes": None}, None, "cookie")), \
             mock.patch("db.is_open", return_value=True), \
             mock.patch("approvals.ask_reason", return_value=None), \
             redirect_stdout(buf):
            got = await writeauth.write_gate(_Req(), nxt)
        self.assertIs(got, sentinel, "干跑不许改变准入：仍须交给下游 handler")
        self.assertIn("[writegate][dryrun] legacy", buf.getvalue())

    async def test_scoped_session_dryrun_reports_would_allow_without_blocking(self):
        sentinel = object()

        async def nxt(_r):
            return sentinel

        buf = io.StringIO()
        with mock.patch("users.identity_of",
                        return_value=({"username": "u",
                                       "scopes": '["hub.memory.write"]'}, None, "cookie")), \
             mock.patch("db.is_open", return_value=True), \
             mock.patch("approvals.ask_reason", return_value=None), \
             redirect_stdout(buf):
            got = await writeauth.write_gate(_Req(), nxt)
        self.assertIs(got, sentinel)
        self.assertIn("would_allow", buf.getvalue())

    async def test_dryrun_parse_failure_never_blocks(self):
        """观测面异常必须被吞掉：不能因为它把合法写请求打成 500。"""
        sentinel = object()

        async def nxt(_r):
            return sentinel

        buf = io.StringIO()
        with mock.patch("users.identity_of",
                        return_value=({"username": "u", "scopes": 12345}, None, "cookie")), \
             mock.patch("db.is_open", return_value=True), \
             mock.patch("approvals.ask_reason", return_value=None), \
             redirect_stdout(buf):
            got = await writeauth.write_gate(_Req(), nxt)
        self.assertIs(got, sentinel)
        self.assertIn("[writegate][dryrun] legacy", buf.getvalue())


if __name__ == "__main__":
    unittest.main()
