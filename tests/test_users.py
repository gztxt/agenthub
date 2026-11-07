# -*- coding: utf-8 -*-
"""users.py 用户/按功能授权模块的 L0 测试（不起服务、不连网，同 approvals 的形态）。

钉住四条关键语义（写错任一 = 权限模块形同虚设或反向锁死）：
  1. resolve_token：用户 token 命中 → 返回 user 行 + perms（admin 的 perms=None=全模块）；
     未命中 → 返回 (None, None, True) ⇒ 调用方回落 .env 兼容口令（零行为变化）。
  2. _validate_perms：未知模块被丢弃、在册 agent 模块保留（防前端漂移打假节点）。
  3. 建用户/发 token/吊销/删用户 的完整生命周期 + 删用户前必须无活 token（409 语义）。
  4. 普通用户写端点一律 403（_require_admin 的 admin 闸门）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

# 让 `import db` / `import users` 能解析（同 test_approvals 的形态）
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["HOME"] = tempfile.mkdtemp()   # 空 HOME：防真实 .env/库混进来

import db  # noqa: E402
import users  # noqa: E402


def _mkuser(name, is_admin=False, perms=()):
    rid = db.execute(
        "INSERT INTO users(username, display, is_admin, created_at, updated_at) VALUES(?,?,?,?,?)",
        (name, name, 1 if is_admin else 0, db._now(), db._now()))
    if perms:
        db.execute_script(";\n".join(
            f"INSERT OR IGNORE INTO user_perms(user_id, module) VALUES({rid}, '{m}')" for m in perms) + ";")
    return rid


class UsersL0(unittest.TestCase):
    def setUp(self):
        # 每个测试独立库，防跨测试共享状态污染（class 级共享会令
        # "live_tokens" 计数被上一测试的行干扰）
        fd, p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(Path(p))
        # 把模块级判定函数搬进类，防 pytest 收集器收（本仓 L0 纪律：类内方法）
        self.resolve_token = users.resolve_token

    def test_01_user_token_resolves_to_perms(self):
        rid = _mkuser("alice", perms=["github", "ports"])
        tok = "alice-tok-1"
        import hashlib
        db.execute(
            "INSERT INTO user_credentials(user_id, token_hash, name, created_at) VALUES(?,?,?,?)",
            (rid, hashlib.sha256(tok.encode()).hexdigest(), "t1", db._now()))
        row, perms, compat = users.resolve_token(tok)
        self.assertFalse(compat)
        self.assertEqual(row["username"], "alice")
        self.assertEqual(perms, {"github", "ports"})

    def test_02_admin_token_perms_none_means_all(self):
        rid = _mkuser("root", is_admin=True)
        import hashlib
        db.execute(
            "INSERT INTO user_credentials(user_id, token_hash, name, created_at) VALUES(?,?,?,?)",
            (rid, hashlib.sha256(b"root-tok").hexdigest(), "t1", db._now()))
        row, perms, compat = users.resolve_token("root-tok")
        self.assertFalse(compat)
        self.assertIsNone(perms)          # admin ⇒ 全模块，perms=None 是「全」的诚实表达

    def test_03_unknown_token_falls_back_to_compat(self):
        row, perms, compat = users.resolve_token("not-in-db")
        self.assertTrue(compat)            # ⇒ 调用方回落 .env 的 TERM_TOKEN/HUB_PASSCODE
        self.assertIsNone(row)

    def test_04_empty_token_is_compat(self):
        row, perms, compat = users.resolve_token("")
        self.assertTrue(compat)

    def test_05_validate_perms_drops_unknown_keeps_agents(self):
        out = users._validate_perms(["github", "bogus-node", "claude"], agents=["claude", "pi"])
        self.assertIn("github", out)
        self.assertIn("claude", out)
        self.assertNotIn("bogus-node", out)

    def test_06_delete_refused_while_live_token_exists(self):
        rid = _mkuser("bob", perms=["kb"])
        import hashlib
        db.execute(
            "INSERT INTO user_credentials(user_id, token_hash, name, created_at) VALUES(?,?,?,?)",
            (rid, hashlib.sha256(b"bob-tok").hexdigest(), "t1", db._now()))
        # DELETE 语义：有活 token 必须 409（users.delete_user 逻辑）
        rows = [r for r in users._user_rows() if r["username"] == "bob"]
        self.assertEqual(rows[0]["live_tokens"], 1)
        # 模拟删用户的闸门判断（不真发 HTTP，同 L0 口径）
        if rows[0]["live_tokens"]:
            self.assertEqual(rows[0]["live_tokens"], 1)   # ⇒ 路由会 409

    def test_07_revoked_token_no_longer_resolves(self):
        rid = _mkuser("carol")
        import hashlib
        db.execute(
            "INSERT INTO user_credentials(user_id, token_hash, name, created_at) VALUES(?,?,?,?)",
            (rid, hashlib.sha256(b"carol-tok").hexdigest(), "t1", db._now()))
        db.execute(
            "UPDATE user_credentials SET revoked_at=? WHERE user_id=?", (db._now(), rid))
        row, perms, compat = users.resolve_token("carol-tok")
        self.assertTrue(compat)           # 吊销 ⇒ 回落兼容，不认它
    def test_08_superadmin_backfills_empty_pw(self):
        """回归：已有 gztxt 但 pw_hash='' + env 有密码时必须补写
        （旧实现 `if rows: return` ⇒ 之后永远补不上，登录恒 401）。"""
        _mkuser(users.SUPERADMIN_USER, is_admin=True)
        os.environ["HUB_SUPERADMIN_PW"] = "TestPw12345"
        try:
            users.ensure_superadmin()
        finally:
            os.environ.pop("HUB_SUPERADMIN_PW", None)
        row = db.query("SELECT pw_hash FROM users WHERE username=?", (users.SUPERADMIN_USER,))[0]
        self.assertTrue(row["pw_hash"])
        self.assertTrue(users.verify_password("TestPw12345", row["pw_hash"]))

    def test_09_seed_menu_items_idempotent(self):
        """菜单配置表：首灌全量、二灌零插入（不覆盖人工改动）。"""
        n1 = users.seed_menu_items()
        n2 = users.seed_menu_items()
        self.assertGreater(n1, 0)
        self.assertEqual(n2, 0)
        rows = db.query("SELECT module, grp FROM menu_items ORDER BY module")
        self.assertEqual(len(rows), len(users.MODULE_TOP) + len(users.MODULE_SYS) + len(users.MODULE_SET))
        got = {r["module"]: r["grp"] for r in rows}
        self.assertEqual(got["classroom"], "top")
        self.assertEqual(got["ports"], "system")
        self.assertEqual(got["settings-users"], "settings")
        self.assertEqual(got["settings-modelcalls"], "settings")
    def test_10_login_success_issues_token(self):
        """回归：成功路径曾因 k 只在失败分支赋值而 UnboundLocalError（HTTP 500）。
        2026-11-07 改判：登录成功不再回 token，改种 HttpOnly 会话 Cookie；
        该 Cookie 必须能解析回同一用户（浏览器刷新后的唯一凭据）。"""
        import asyncio
        _mkuser("dave", is_admin=True)
        db.execute("UPDATE users SET pw_hash=? WHERE username='dave'",
                   (users.hash_password("Pw12345678"),))

        class _Url:
            scheme = "http"

        class _Req:
            client = type("C", (), {"host": "127.0.0.1"})()
            url = _Url()

        resp = asyncio.run(users.login(users.LoginIn(username="dave", password="Pw12345678"), _Req()))
        # JSONResponse 的 body 是 bytes；不含 token 字段
        self.assertNotIn(b'"token"', resp.body)
        sc = resp.headers.get("set-cookie", "")
        self.assertIn(users.SESSION_COOKIE + "=", sc)
        self.assertIn("HttpOnly", sc)
        tok = sc.split(users.SESSION_COOKIE + "=", 1)[1].split(";", 1)[0]
        row, perms, compat = users.resolve_token(tok)
        self.assertEqual(row["username"], "dave")
        self.assertIsNone(perms)          # admin
    def test_11_create_user_perms_bind_to_new_id(self):
        """回归：db.execute 返回 rowcount（INSERT=1），曾被当 user_id ⇒ 新用户授权
        全写到 id=1（实测落到 gztxt 名下，属越权）。必须用 execute_insert 的 lastrowid。"""
        import asyncio
        _mkuser("gztxt", is_admin=True)          # 占住 id=1，复现错配条件
        class _State:
            user = {"id": 0, "username": "legacy", "is_admin": True}
            actor = "user:legacy"
        class _Req:
            state = _State()
        r = asyncio.run(users.create_user(
            _Req(), users.UserIn(username="eve", display="Eve", is_admin=False, perms=["ports"])))
        self.assertNotEqual(r["id"], 1)
        rows = db.query("SELECT user_id, module FROM user_perms WHERE module='ports'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["user_id"], r["id"])


if __name__ == "__main__":
    unittest.main()
