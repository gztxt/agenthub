#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：CloudCLI 项目直达（src/cloudcli.py）。

夹具：临时 auth.db（projects/sessions/app_config/users 最小 schema，字段名与
真实库逐一对齐——2026-09-26 实测锁定）。零宿主依赖：CLOUDCLI_AUTH_DB 指 tmp 夹具，
start 的 cloudcli 转调用 mock 掉（不起真服务）。

为什么这些用例必须存在：
1. **名称精确**：custom_project_name 优先、回落 basename——用户点名「精确显示项目名称」。
2. **列表与启动解耦**：db 直读不依赖 cloudcli 服务活——服务挂时 start 如实报错、
   projects 照常（「查不了」不能糊成「没有」的四态口径）。
3. **JWT 铸造形态**：三段式、payload 与 cloudcli generateToken 字段对齐、短时 2h——
   铸错形态=静默 401 死链路。
4. **start 按写判**：创建会话是写动作（writeauth fail-closed 401/503）。
"""
import asyncio
import base64
import hashlib
import hmac
import json
import os
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import cloudcli  # noqa: E402
import runlog    # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Req:
    """照抄 test_runlog 的桩：method/path/headers/client。"""

    def __init__(self, token="good"):
        self.method = "POST"
        self.url = types.SimpleNamespace(path="/api/cloudcli/start", query="")
        self.headers = types.SimpleNamespace(
            raw=[(b"x-hub-token", token.encode())] if token else [])
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="user:term-token")


def _make_db(root: pathlib.Path) -> pathlib.Path:
    """最小 auth.db：字段与真实库实测 schema 逐一对齐。"""
    db = root / "auth.db"
    con = sqlite3.connect(str(db))
    con.executescript("""
        CREATE TABLE app_config (key TEXT PRIMARY KEY, value TEXT);
        CREATE TABLE users (id INTEGER PRIMARY KEY, username TEXT, password_hash TEXT);
        CREATE TABLE projects (
            project_id TEXT PRIMARY KEY, project_path TEXT UNIQUE,
            custom_project_name TEXT, isStarred INTEGER DEFAULT 0,
            isArchived INTEGER DEFAULT 0);
        CREATE TABLE sessions (
            session_id TEXT PRIMARY KEY, provider TEXT, project_path TEXT,
            isArchived INTEGER DEFAULT 0, created_at TEXT, updated_at TEXT);
        INSERT INTO app_config VALUES('jwt_secret', 'test-secret-0123456789abcdef');
        INSERT INTO users VALUES(1, 'admin', 'x');
        INSERT INTO projects VALUES
            ('p1', '/home/gztxt/agent-hub', 'agent-hub', 1, 0),
            ('p2', '/fs/1000/ftp/技术文档', '技术文档', 1, 0),
            ('p3', '/tmp/abc', NULL, 0, 0),
            ('p4', '/tmp/gone-archived', '归档不出现', 0, 1);
        INSERT INTO sessions VALUES
            ('s1', 'claude', '/home/gztxt/agent-hub', 0, '2026-09-01T00:00:00', '2026-09-26T08:08:00'),
            ('s2', 'claude', '/home/gztxt/agent-hub', 0, '2026-09-01T00:00:00', '2026-09-25T10:00:00'),
            ('s3', 'codex', '/fs/1000/ftp/技术文档', 0, '2026-09-01T00:00:00', '2026-09-24T09:00:00'),
            ('s4', 'claude', '/tmp/abc', 1, '2026-09-01T00:00:00', '2026-09-23T00:00:00');
    """)
    con.commit()
    con.close()
    return db


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0cc-"))
        self.db = _make_db(self.tmp)
        self._saved_db = cloudcli.AUTH_DB
        self._saved_url = cloudcli.CLOUDCLI_URL
        cloudcli.AUTH_DB = self.db
        cloudcli.CLOUDCLI_URL = "http://127.0.0.1:59999"   # 不可达口，mock 前的天然失败态
        self.addCleanup(self._restore)

    def _restore(self):
        cloudcli.AUTH_DB = self._saved_db
        cloudcli.CLOUDCLI_URL = self._saved_url
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestProjects(_DbCase):
    def test_all_active_projects_with_exact_names(self):
        d = asyncio.run(cloudcli.cloudcli_projects())
        self.assertTrue(d["ok"])
        self.assertEqual(d["count"], 3, "归档项目不得出现")
        names = [p["name"] for p in d["projects"]]
        self.assertIn("agent-hub", names)
        self.assertIn("技术文档", names)
        self.assertIn("abc", names, "custom_name 为 NULL 时回落 basename（名称精确不空）")

    def test_sessions_count_and_latest_activity(self):
        d = asyncio.run(cloudcli.cloudcli_projects())
        by = {p["path"]: p for p in d["projects"]}
        self.assertEqual(by["/home/gztxt/agent-hub"]["sessions"], 2)
        self.assertEqual(by["/home/gztxt/agent-hub"]["last_activity"], "2026-09-26T08:08:00")
        self.assertEqual(by["/tmp/abc"]["sessions"], 0, "唯一会话已归档 ⇒ 0（活跃口径）")

    def test_starred_first_ordering(self):
        d = asyncio.run(cloudcli.cloudcli_projects())
        starred = [p["starred"] for p in d["projects"]]
        self.assertEqual(starred, sorted(starred, reverse=True), "★ 优先排序")

    def test_missing_db_reports_not_ok(self):
        cloudcli.AUTH_DB = self.tmp / "ghost.db"
        d = asyncio.run(cloudcli.cloudcli_projects())
        self.assertFalse(d["ok"])
        self.assertIn("auth.db", str(d["error"]))

    def test_schema_drift_degrades_with_error(self):
        con = sqlite3.connect(str(self.db))
        con.execute("ALTER TABLE projects RENAME TO projects_old")
        con.commit()
        con.close()
        d = asyncio.run(cloudcli.cloudcli_projects())
        self.assertFalse(d["ok"], "schema 不兼容必须如实报错，不装空列表")


class TestMintToken(_DbCase):
    def test_token_three_parts_and_payload_shape(self):
        import base64
        tok = cloudcli._mint_token()
        parts = tok.split(".")
        self.assertEqual(len(parts), 3, "JWT 三段式")

        def b64d(s):
            return json.loads(base64.urlsafe_b64decode(s + "=" * (-len(s) % 4)))
        payload = b64d(parts[1])
        self.assertEqual(payload["userId"], 1)
        self.assertEqual(payload["username"], "admin")
        self.assertLessEqual(payload["exp"] - payload["iat"], cloudcli.TOKEN_TTL_S,
                             "短时 token：有效期 ≤ 2h")

    def test_signature_verifies_with_secret(self):
        import hashlib
        tok = cloudcli._mint_token()
        h, p, s = tok.split(".")
        expect = base64.urlsafe_b64encode(
            hmac.new(b"test-secret-0123456789abcdef", f"{h}.{p}".encode(),
                     hashlib.sha256).digest()).decode().rstrip("=")
        self.assertEqual(s, expect, "签名必须能用库内 secret 复算（铸错=静默401）")

    def test_no_users_raises(self):
        con = sqlite3.connect(str(self.db))
        con.execute("DELETE FROM users")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError):
            cloudcli._mint_token()

    def test_no_secret_raises(self):
        con = sqlite3.connect(str(self.db))
        con.execute("DELETE FROM app_config WHERE key='jwt_secret'")
        con.commit()
        con.close()
        with self.assertRaises(RuntimeError):
            cloudcli._mint_token()


class TestStart(_DbCase):
    def _env(self, term="good", passcode=""):
        return mock.patch.multiple(os, environ={**os.environ,
                                                "TERM_TOKEN": term, "HUB_PASSCODE": passcode})

    def _call(self, token="good", path="/home/gztxt/agent-hub"):
        return asyncio.run(cloudcli.cloudcli_start(_Req(token),
                                                   cloudcli.StartIn(path=path)))

    def test_auth_fail_closed(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
            self.assertEqual(cm.exception.status_code, 401)
        with self._env(term="", passcode=""):
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
            self.assertEqual(cm.exception.status_code, 503, "未配口令 fail-closed")

    def test_start_creates_session_via_cloudcli(self):
        """mock urllib：cloudcli 201 → sessionId/url 透传（埋点经 track 装饰器）。"""
        class _Resp:
            def __init__(self, body):
                self._body = body

            def read(self):
                return json.dumps(self._body).encode()

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        captured = {}

        def fake_urlopen(req, timeout=None):
            captured["auth"] = req.headers.get("Authorization", "")
            captured["body"] = json.loads(req.data.decode())
            return _Resp({"success": True, "data": {"sessionId": "sid-123",
                                                     "sessionName": "Untitled Session"}})

        with self._env():
            with mock.patch.object(cloudcli.urllib.request, "urlopen", fake_urlopen):
                d = self._call()
        self.assertEqual(d["sessionId"], "sid-123")
        self.assertEqual(d["url"], "/session/sid-123")
        self.assertTrue(captured["auth"].startswith("Bearer "), "转调带 JWT")
        self.assertEqual(captured["body"]["provider"], "claude")
        self.assertEqual(captured["body"]["projectPath"], "/home/gztxt/agent-hub")

    def test_cloudcli_down_reports_502(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call()          # CLOUDCLI_URL 指不可达口 59999
        self.assertEqual(cm.exception.status_code, 502, "服务不可达必须如实 502")

    def test_missing_sessionid_in_201_is_schema_drift(self):
        class _Resp:
            def read(self):
                return b'{"success": true, "data": {}}'

            def __enter__(self):
                return self

            def __exit__(self, *a):
                return False

        with self._env():
            with mock.patch.object(cloudcli.urllib.request, "urlopen",
                                   lambda req, timeout=None: _Resp()):
                with self.assertRaises(HTTPException) as cm:
                    self._call()
        self.assertIn("sessionId", str(cm.exception.detail))

    def test_runlog_subject_registered(self):
        self.assertIn("cc.start", runlog.SUBJECTS)


class TestIntegrationSurface(unittest.TestCase):
    def test_main_mounts_router(self):
        text = (_REPO / "src" / "main.py").read_text(encoding="utf-8")
        self.assertIn("cloudcli_mod.router", text)

    def test_start_decorated_with_runlog(self):
        text = (_REPO / "src" / "cloudcli.py").read_text(encoding="utf-8")
        self.assertIn('@runlog.track("cc.start")', text, "start 必须留痕（迭代参考）")


class TestFrontendSurface(unittest.TestCase):
    """静态断言：详情抽屉钩子与直达函数成对（照 test_kb_frontend_pages 模式）。"""

    def setUp(self):
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")
        self.boot = (_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8")
        self.w04 = (_REPO / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")

    def test_showdetail_hooks_cloudcli_projects(self):
        """claude 详情抽屉必须挂 loadCloudcliProjects——漏挂=面板永远不出现。"""
        self.assertIn("loadCloudcliProjects", self.boot,
                      "showDetail('claude') 未挂 CloudCLI 项目加载钩子")

    def test_loader_and_start_functions_defined(self):
        for fn in ("loadCloudcliProjects", "cloudcliStart"):
            self.assertIn(f"async function {fn}(", self.w04, f"{fn} 缺失 ⇒ 点击无反应")

    def test_start_navigates_embed_with_dataset_sync(self):
        """iframe 直达必须同步 dataset.src（防 applyChatMode 重置回实体 ui.url）。
        v0.13.80：入口从 claude 详情抽屉拆出为独立 cloudcli 子菜单（embed 面板下方
        项目直达），兜底路径仍保留在 claude 抽屉 ⇒ 两处任一命中即合规。"""
        m = re.search(r"async function cloudcliStart\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m)
        body = m.group(0)
        self.assertIn("gotoChat('cloudcli')", body, "先进独立 cloudcli 子菜单 embed 面板再覆写 src")
        self.assertIn("f.dataset.src = url", body, "dataset 不同步=下次切模式被重置")
        self.assertIn("f.src = url", body)
        self.assertIn("/api/cloudcli/start", body)

    def test_projects_list_exact_name_and_start_button(self):
        """列表必须渲染精确名称 + 「开始会话」按钮（用户核心诉求）。

        v0.13.131（PT-20261019-02）：按钮从 inline onclick 改 `data-sk-ccstart` 委托
        ——原写法 `onclick="cloudcliStart(<jsStr(path)>)"` 被 jsStr 的双引号字面量
        截断成 `cloudcliStart(`，点击静默失败。判据改为「属性在 + 委托接住」。
        """
        m = re.search(r"async function loadCloudcliProjects\(.*?\n\}", self.w04, re.S)
        body = m.group(0)
        self.assertIn("p.name", body, "精确名称（custom_name 优先）必须上屏")
        self.assertIn('data-sk-ccstart="', body, "项目行必须挂开始按钮（data-* 委托）")
        self.assertIn("bindDetailDelegation", self.boot,
                      "data-sk-ccstart 必须有委托接住（#detailBody 由 01 分片统一绑定）")

    def test_failure_state_not_silent(self):
        m = re.search(r"async function loadCloudcliProjects\(.*?\n\}", self.w04, re.S)
        self.assertIn("加载失败", m.group(0), "失败态必须上屏（不装死）")

    def test_html_no_new_section_needed(self):
        html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="detailDrawer"', html, "复用 detailDrawer——不新建 section")

    def test_embed_body_slot_exists(self):
        """v0.13.80：独立 cloudcli 子菜单 embed 面板下方需 #embedBody 槽位
        （renderCloudcliProjects 渲染锚点，旧缓存无槽位时 loadCloudcliProjects 兜底）。"""
        html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.assertIn('id="embedBody"', html, "embedBody 槽位缺失 ⇒ 嵌入面板渲染走兜底路径")

    def test_cloudcli_profile_independent_card(self):
        """v0.13.80：cloudcli 拆出为独立画像卡（Agents 菜单子项），claude 卡不再挂
        cloudcli 宿主 ui ⇒ 终端页纯终端、原生界面独立子菜单。
        用 AST 精确切出各画像 dict 卡（不靠正则猜换行）。"""
        import ast
        src = (_REPO / "src" / "profiles.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        cards = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.AnnAssign) and isinstance(node.target, ast.Name) \
                    and node.target.id == "PROFILES":
                val = node.value
            elif isinstance(node, ast.Assign) and any(
                    isinstance(t, ast.Name) and t.id == "PROFILES" for t in node.targets):
                val = node.value
            else:
                continue
            if isinstance(val, ast.List):
                for el in val.elts:
                    if isinstance(el, ast.Dict) and el.keys and el.values \
                            and isinstance(el.keys[0], ast.Constant) \
                            and el.keys[0].value == "id":
                        ids = el.values[0].value if isinstance(el.values[0], ast.Constant) else str(el.values[0])
                        cards[ids] = ast.get_source_segment(src, el)
        self.assertIn("cloudcli", cards, "cloudcli 独立画像缺失 ⇒ Agents 菜单无子菜单")
        self.assertIn("3010", cards["cloudcli"], "cloudcli 卡未挂 :3010 ⇒ 原生界面无入口")
        self.assertIn("agent", cards["cloudcli"], "cloudcli 必须是 kind=agent 才能进 Agents 菜单")
        self.assertNotIn("3010", cards.get("claude", ""),
                         "claude 画像残留 cloudcli 宿主端口 ⇒ 旧路径未清干净")

if __name__ == "__main__":
    unittest.main(verbosity=2)
