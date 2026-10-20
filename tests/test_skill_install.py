#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：技能安装管理（软链双发现点）+ 预算化清单 + 三路扩容（v0.13.26 批2）。

分层口径：零宿主依赖——SKILL_DIRS 猴补到 tmp 夹具、db 指向 tmp 库、不起服务、不打网络。
**不允许 SKIP**。

为什么这些用例必须存在（红向都能确定性造）：
1. **install 覆盖攻击**：name 里带 `../` 或绝对路径能把软链建到白名单目录外
   ⇒ _NAME_RE 拒绝 + 目标路径天然被限制在 SKILL_DIRS 下。
2. **remove 误删本体**：真目录是权威副本（09-19 主权原则），HTTP 端点删本体=不可逆破坏
   ⇒ islink 才删、真目录 409。
3. **幂等**：同 realpath 已存在必须 no-op 而不是 409（重试安全）；
   **不同 realpath 已存在必须 409**（拒绝覆盖，防两份会漂移的副本）。
4. **budget 不静默截断**：被裁条目如实报 truncated/total（「全绿而功能层缺」的清单版）。
"""
import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db                     # noqa: E402
import skill                  # noqa: E402
import writeauth              # noqa: E402

from fastapi import FastAPI   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _mk_skill(root: pathlib.Path, name: str, desc: str = "测试技能") -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8")
    return d


class _SkillCase(unittest.TestCase):
    """夹具：tmp 技能目录三路 + tmp 库 + TestClient（只挂 skill 路由）。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0skillin-"))
        self.a = self.tmp / "a"          # 权威副本路
        self.b = self.tmp / "b"          # 目标发现点 1
        self.c = self.tmp / "c"          # 目标发现点 2
        for p in (self.a, self.b, self.c):
            p.mkdir()
        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "dst1": str(self.b), "dst2": str(self.c)}
        db.init_db(self.tmp / "t.db")
        _app = FastAPI()
        _app.include_router(skill.router)
        # 写鉴权照生产形态挂（main.py：app.middleware("http")(write_gate)）——
        # 只挂 router 不挂闸门测不到鉴权，会把「未拦」测成绿。
        _app.middleware("http")(writeauth.write_gate)
        # 软链安装/卸载是写操作：writeauth fail-closed 会拦（未配口令→503）。
        # 本夹具给进程注入测试口令，让请求带 token 走 allow 分支。
        os.environ["TERM_TOKEN"] = "l0-skill-test-token"
        self.client = TestClient(_app)
        self.addCleanup(self._restore)

    def _restore(self):
        skill.SKILL_DIRS = self._saved_dirs
        os.environ.pop("TERM_TOKEN", None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _hdr(self):
        return {"x-hub-token": "l0-skill-test-token"}


class TestInstall(_SkillCase):
    def setUp(self):
        super().setUp()
        _mk_skill(self.a, "demo-skill", "演示")

    def test_install_creates_symlinks_and_audits(self):
        r = self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "src", "targets": ["dst1", "dst2"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        body = r.json()
        self.assertEqual(body["created"], ["dst1", "dst2"])
        self.assertTrue(os.path.islink(self.b / "demo-skill"))
        self.assertEqual(os.path.realpath(self.b / "demo-skill"),
                         str(self.a / "demo-skill"))
        rows = db.query("SELECT * FROM asset_audit WHERE asset_type='skill'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["action"], "bind")

    def test_idempotent_same_realpath_no_op(self):
        self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "src", "targets": ["dst1"]},
            headers=self._hdr())
        r = self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "src", "targets": ["dst1"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["existed"], ["dst1"])
        self.assertEqual(r.json()["created"], [])

    def test_conflicting_target_409(self):
        _mk_skill(self.c, "demo-skill", "另一份不同副本")
        r = self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "src", "targets": ["dst2"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 409)

    def test_path_traversal_rejected(self):
        for evil in ("../escape", "..", "/etc/passwd", ".hidden", ""):
            r = self.client.post("/api/skill/install", json={
                "name": evil, "from_route": "src", "targets": ["dst1"]},
                headers=self._hdr())
            self.assertIn(r.status_code, (400, 422), f"{evil!r} 必须被拒：{r.status_code}")

    def test_unknown_routes_rejected(self):
        r = self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "nope", "targets": ["dst1"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 400)
        r = self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "src", "targets": ["elsewhere"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 400)

    def test_no_skill_md_rejected(self):
        (self.a / "bare").mkdir()
        (self.a / "bare" / "x.txt").write_text("not a skill", encoding="utf-8")
        r = self.client.post("/api/skill/install", json={
            "name": "bare", "from_route": "src", "targets": ["dst1"]},
            headers=self._hdr())
        self.assertEqual(r.status_code, 400)

    def test_auth_required_without_token(self):
        r = self.client.post("/api/skill/install", json={
            "name": "demo-skill", "from_route": "src", "targets": ["dst1"]})
        self.assertIn(r.status_code, (401, 403, 503), "写操作必须被 writeauth 拦")


class TestRemove(_SkillCase):
    def setUp(self):
        super().setUp()
        _mk_skill(self.a, "gone-skill")
        os.symlink(str(self.a / "gone-skill"), str(self.b / "gone-skill"))
        _mk_skill(self.c, "real-skill")      # 真目录（非软链）

    def test_remove_symlink_ok_and_audits(self):
        r = self.client.delete("/api/skill/remove", params={
            "name": "gone-skill", "route": "dst1"}, headers=self._hdr())
        self.assertEqual(r.status_code, 200, r.text)
        self.assertFalse(os.path.lexists(self.b / "gone-skill"))
        self.assertTrue(self.a.joinpath("gone-skill", "SKILL.md").is_file(),
                        "本体必须毫发无损")
        rows = db.query("SELECT * FROM asset_audit WHERE asset_type='skill' "
                        "AND action='unbind'")
        self.assertEqual(len(rows), 1)

    def test_remove_real_dir_409(self):
        r = self.client.delete("/api/skill/remove", params={
            "name": "real-skill", "route": "dst2"}, headers=self._hdr())
        self.assertEqual(r.status_code, 409)
        self.assertTrue((self.c / "real-skill").is_dir(), "真目录不得被删")

    def test_remove_missing_404(self):
        r = self.client.delete("/api/skill/remove", params={
            "name": "ghost", "route": "dst1"}, headers=self._hdr())
        self.assertEqual(r.status_code, 404)


class TestBudget(_SkillCase):
    def setUp(self):
        super().setUp()
        for i in range(6):
            _mk_skill(self.a, f"skill-{i:02d}", f"第 {i} 号测试技能描述")

    def test_budget_shapes_and_truncation(self):
        r = self.client.get("/api/skill/budget", params={"max_tokens": 60})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("full", d) and self.assertIn("name_only", d)
        self.assertLessEqual(d["used_est"], 60)
        self.assertGreaterEqual(d["truncated"] + len(d["full"]) + len(d["name_only"]), d["total"])

    def test_budget_big_enough_no_truncation(self):
        r = self.client.get("/api/skill/budget", params={"max_tokens": 4000})
        d = r.json()
        self.assertEqual(d["truncated"], 0)
        self.assertEqual(len(d["full"]), 6)

    def test_budget_est_token_sanity(self):
        r = self.client.get("/api/skill/budget", params={"max_tokens": 4000})
        d = r.json()
        for it in d["full"]:
            self.assertGreater(it["est_tokens"], 0)

    def test_budget_truncation_is_explained(self):
        """「为什么只剩 N 条」要能由响应里的数自己回答（v0.13.141）。

        生产实测：预算 1200 ⇒ full 9 条吃 824（档位上限 840）、仅名 61 个吃 373、
        剩 3 tok 装不下下一个名字，`break` 把其后 308 条一起丢掉。
        原先只回 `truncated=308`，面板只能显示结果不能解释成因。
        """
        # 造足量长名条目：6 条短名在 60 tok 里塞得下，不会触发截断
        for i in range(10):
            _mk_skill(self.a, f"long-skill-name-{i:02d}", "第 %d 号测试技能描述" % i)
        skill._scan_cache.clear()   # 同类的更早用例可能已把 6 条的结果缓存住
        r = self.client.get("/api/skill/budget", params={"max_tokens": 60})
        d = r.json()
        self.assertGreater(d["truncated"], 0)
        self.assertEqual(d["full_cap"], 42, "full_cap 必须是预算的 70%")
        self.assertEqual(d["full_used_est"] + d["name_used_est"], d["used_est"])
        # 只断言「两段之和 == used」是恒真的（取错位置就变成 used+0），
        # 必须再钉住两段各自的上界与「仅名真的用了 tok」。
        self.assertLessEqual(d["full_used_est"], d["full_cap"])
        self.assertGreater(d["name_used_est"], 0, "仅名区明明装了名字却报 0 tok")
        self.assertEqual(d["remaining_est"], max(0, d["budget"] - d["used_est"]))
        # 截断点必须点名：被裁是连续尾段，起点就是这个装不下的名字
        self.assertTrue(d["truncated_head"], d)
        self.assertNotIn(d["truncated_head"], d["name_only"])
        # 三段守恒：全条目 + 仅名 + 被裁 == 名录总数
        self.assertEqual(len(d["full"]) + len(d["name_only"]) + d["truncated"], d["total"])

    def test_budget_no_truncation_has_no_head(self):
        d = self.client.get("/api/skill/budget", params={"max_tokens": 4000}).json()
        self.assertEqual(d["truncated"], 0)
        self.assertIsNone(d["truncated_head"])
        self.assertEqual(d["name_used_est"], 0)


class TestThreeNewRoutes(_SkillCase):
    def test_default_dirs_include_new_routes(self):
        """批2 扩容三路（agents/codex/workbuddy）必须在默认表里——漏一路=该 CLI 技能不可见。"""
        for k in ("agents", "codex", "workbuddy"):
            self.assertIn(k, skill._DEFAULT_DIRS)

    def test_real_default_dirs_exist_on_host(self):
        """hermetic 例外声明：这里只做存在性断言（ls 级），不读内容不打服务。
        56 号文档实测三路都在本机存在（39/1/6 项）。"""
        for k in ("agents", "codex", "workbuddy"):
            p = skill._DEFAULT_DIRS[k]
            self.assertTrue(os.path.isdir(p), f"{k} → {p} 不存在？批2前提失效")


if __name__ == "__main__":
    unittest.main(verbosity=2)
