#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：MCP 技能部署工具 `hub_skill_install` / `hub_skill_remove`（PT-20261024-02 P5）。

钉住六条，对应 P5 验收判据「tools/list 7→9；真 MCP 客户端一次 install 落盘软链可查；
写门禁不回归」：

1. **转发口径**：install → POST /api/skill/install（targets 逗号串拆列表、去空、
   去空白）；remove → DELETE /api/skill/remove（name/route 进 query）。工具层不造
   第二套语义，只转发现有 REST。
2. **无条件注册**：两个工具不关在 `if ALLOW_WRITE:` 里——技能面写工具与
   invoke/acquire/auto 同族，门禁在 REST 层（writeauth + 边界 + 审计）；
   技能工具计数 7→9。若哪天被关进 ALLOW_WRITE 门，未开 env 的部署就没有
   agent 驱动入口（PT-20261019-01 P2 接线的目的就被架空）。
3. **真磁盘软链**（hermetic e2e，不占端口）：MCP 工具函数 → 转发到 in-process
   TestClient 上的真 REST → 真 symlink 落 tmp 盘，realpath 指向权威副本；
   remove 后软链消失、权威目录原样。
4. **remove 硬边界**：真实目录 409 拒删且内容不灭。
5. **写门禁不回归**：无 token ⇒ `missing-credential` 诚实失败（不静默发必然 401
   的请求）；错 token ⇒ REST 层 401（门没被绕过）。
6. **审计成对**：install 记 bind、remove 记 unbind（asset_audit append-only）。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db              # noqa: E402
import skill           # noqa: E402
import writeauth       # noqa: E402

from fastapi import FastAPI           # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

TOKEN = "l0-deploy-token"


def _mk_skill(root: pathlib.Path, name: str, desc: str) -> None:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\nDEMO-BODY\n", encoding="utf-8")


class _DeployCase(unittest.TestCase):
    """公共夹具：两路发现点（src=权威 / dst=目标）+ in-process REST + db。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0deploy-"))
        self.a = self.tmp / "a"          # 权威副本所在路
        self.b = self.tmp / "b"          # 目标路
        self.a.mkdir()
        self.b.mkdir()
        _mk_skill(self.a, "demo", "演示技能")

        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "dst": str(self.b)}
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)

        db.init_db(self.tmp / "t.db")
        app = FastAPI()
        app.include_router(skill.router)
        app.middleware("http")(writeauth.write_gate)
        self.client = TestClient(app)

        self._saved_env = {k: os.environ.get(k) for k in ("TERM_TOKEN", "HUB_PASSCODE")}
        os.environ["TERM_TOKEN"] = TOKEN
        os.environ["SKILL_LOCAL_ROOTS_JSON"] = "[]"

        import hubmcp
        self.hubmcp = hubmcp
        self._saved_post = hubmcp._post
        self._saved_delete = hubmcp._delete
        self._install_fakes()
        self.addCleanup(self._restore)

    # ── 转发到 in-process 真 REST 的假 _post/_delete（口径与真函数同签名）──
    # 4xx 也要复刻真函数的响应形态（error/http/detail）：真 _post 走 urllib 拿不到
    # TestClient 的 r.json()，HTTPError 分支就是这么造的；夹具少造一层，被测的
    # 「失败表态透传」断言就会假绿。
    def _install_fakes(self, token: str = TOKEN):
        client = self.client

        def fake_post(path, body, _timeout=0, **params):
            r = client.post(path, json=body, headers={"x-hub-token": token})
            if r.status_code >= 400:
                return {"error": f"hub REST {r.status_code}", "path": path,
                        "http": r.status_code, "detail": r.text[:400]}
            return r.json()

        def fake_delete(path, _timeout=0, **params):
            r = client.delete(path, params=params, headers={"x-hub-token": token})
            if r.status_code >= 400:
                return {"error": f"hub REST {r.status_code}", "path": path,
                        "http": r.status_code, "detail": r.text[:400]}
            return r.json()

        self.hubmcp._post = fake_post
        self.hubmcp._delete = fake_delete

    def _restore(self):
        self.hubmcp._post = self._saved_post
        self.hubmcp._delete = self._saved_delete
        skill.SKILL_DIRS = self._saved_dirs
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        os.environ.pop("SKILL_LOCAL_ROOTS_JSON", None)
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _audit(self, action):
        return db.query("SELECT * FROM asset_audit WHERE asset_type='skill' AND action=?",
                        (action,))


class TestForwarding(_DeployCase):
    """① 转发口径：路径、参数形态、拆串规则。"""

    def test_install_forwards_split_targets(self):
        seen = {}

        def spy(path, body, _timeout=0, **params):
            seen.update({"path": path, "body": body})
            return {"status": "installed", "created": ["dst"], "existed": []}

        self.hubmcp._post = spy
        out = json.loads(self.hubmcp.hub_skill_install(
            name="demo", from_route="src", targets=" dst , claude ,, "))
        self.assertEqual(seen["path"], "/api/skill/install")
        self.assertEqual(seen["body"],
                         {"name": "demo", "from_route": "src", "targets": ["dst", "claude"]})
        self.assertEqual(out["status"], "installed")

    def test_remove_forwards_query_params(self):
        seen = {}

        def spy(path, _timeout=0, **params):
            seen.update({"path": path, "params": params})
            return {"status": "removed", "name": "demo", "route": "dst"}

        self.hubmcp._delete = spy
        out = json.loads(self.hubmcp.hub_skill_remove(name="demo", route="dst"))
        self.assertEqual(seen["path"], "/api/skill/remove")
        self.assertEqual(seen["params"], {"name": "demo", "route": "dst"})
        self.assertEqual(out["status"], "removed")


class TestRegistration(_DeployCase):
    """② 注册门面：无条件注册、技能工具 7→9、入参 schema 可读。"""

    def test_registered_without_allow_write(self):
        tools = {t.name: t for t in self.hubmcp.server._tool_manager.list_tools()}
        skill_tools = {n for n in tools if n.startswith("hub_skill_")}
        self.assertIn("hub_skill_install", tools)
        self.assertIn("hub_skill_remove", tools)
        self.assertEqual(len(skill_tools), 9,
                         f"技能工具应 7→9，实得 {sorted(skill_tools)}")
        # 本夹具环境未设 HUB_MCP_ALLOW_WRITE ⇒ 上述在场即证明不在 ALLOW_WRITE 门里
        self.assertFalse(self.hubmcp.ALLOW_WRITE, "夹具环境不应开 ALLOW_WRITE")

    def test_input_schema_shape(self):
        tools = {t.name: t for t in self.hubmcp.server._tool_manager.list_tools()}

        def _schema(t):
            # mcpserver 的 Tool 用 `parameters`；mcp.types.Tool 用 `input_schema`
            return getattr(t, "parameters", None) or getattr(t, "input_schema", None) or {}

        inst = (_schema(tools["hub_skill_install"]).get("properties")) or {}
        rem = (_schema(tools["hub_skill_remove"]).get("properties")) or {}
        self.assertEqual(set(inst), {"name", "from_route", "targets"})
        self.assertEqual(set(rem), {"name", "route"})
        # targets 是逗号串（与 routes 同款口径），不是 list——MCP 客户端传串
        self.assertEqual(inst["targets"].get("type"), "string")


class TestE2ESymlink(_DeployCase):
    """③④⑥ MCP → REST → 真磁盘软链 + remove 边界 + 审计成对。"""

    def test_install_creates_symlink_remove_deletes_it(self):
        out = json.loads(self.hubmcp.hub_skill_install(
            name="demo", from_route="src", targets="dst"))
        self.assertEqual(out["status"], "installed", out)
        self.assertEqual(out["created"], ["dst"])
        link = self.b / "demo"
        self.assertTrue(link.is_symlink(), "install 必须在目标路落真软链")
        self.assertEqual(os.path.realpath(link), str((self.a / "demo").resolve()))
        binds = self._audit("bind")
        self.assertEqual(len(binds), 1, "install 必须记一条 bind 审计")

        out2 = json.loads(self.hubmcp.hub_skill_remove(name="demo", route="dst"))
        self.assertEqual(out2["status"], "removed", out2)
        self.assertFalse(link.exists(), "remove 后软链必须消失")
        self.assertTrue((self.a / "demo" / "SKILL.md").is_file(),
                        "权威副本不许被 remove 伤到")
        self.assertEqual(len(self._audit("unbind")), 1, "remove 必须记一条 unbind 审计")

    def test_install_is_idempotent(self):
        self.hubmcp.hub_skill_install(name="demo", from_route="src", targets="dst")
        out = json.loads(self.hubmcp.hub_skill_install(
            name="demo", from_route="src", targets="dst"))
        self.assertEqual(out["created"], [])
        self.assertEqual(out["existed"], ["dst"])
        self.assertEqual(len(self._audit("bind")), 2, "幂等命中也记账（审计不丢）")

    def test_remove_refuses_real_directory_409(self):
        _mk_skill(self.b, "demo", "目标路上的真实副本")
        out = json.loads(self.hubmcp.hub_skill_remove(name="demo", route="dst"))
        self.assertEqual(out.get("http"), 409, out)
        self.assertTrue((self.b / "demo" / "SKILL.md").is_file(),
                        "409 拒删时真实目录内容不许灭")

    def test_install_unknown_route_400_with_list(self):
        out = json.loads(self.hubmcp.hub_skill_install(
            name="demo", from_route="src", targets="nope"))
        self.assertEqual(out.get("http"), 400, out)
        self.assertIn("dst", out.get("detail", ""), "400 detail 必须带可用发现点清单")


class TestWriteGate(_DeployCase):
    """⑤ 写门禁不回归：缺凭据诚实失败；错凭据被 REST 层 401。"""

    def test_missing_credential_is_honest_failure(self):
        self.hubmcp._post = self._saved_post      # 用真转发（它会先查凭据）
        self.hubmcp._delete = self._saved_delete
        os.environ.pop("TERM_TOKEN", None)
        os.environ.pop("HUB_PASSCODE", None)
        out = json.loads(self.hubmcp.hub_skill_install(
            name="demo", from_route="src", targets="dst"))
        self.assertEqual(out.get("error"), "missing-credential", out)
        out2 = json.loads(self.hubmcp.hub_skill_remove(name="demo", route="dst"))
        self.assertEqual(out2.get("error"), "missing-credential", out2)

    def test_wrong_token_rejected_by_rest_gate(self):
        self._install_fakes(token="wrong-token")
        out = json.loads(self.hubmcp.hub_skill_install(
            name="demo", from_route="src", targets="dst"))
        self.assertEqual(out.get("http"), 401, out)
        self.assertFalse((self.b / "demo").exists(), "401 不许留下半点副作用")


if __name__ == "__main__":
    unittest.main()
