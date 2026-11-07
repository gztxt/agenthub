#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：应用级偏好 KV（src/prefs.py，v0.13.36）。

为什么这些用例必须存在：
1. **键白名单是边界**：/api/prefs 不是自由 KV——自由 KV 迟早被当成无鉴权
   配置面用；未知键必须 404，写路径同样先过白名单再落表。
2. **形状硬顶**：normalize 去空/去重/截断/封顶——坏客户端或旧前端不能写坏表；
   坏行读出来按无偏好处理（None），绝不 500 打断列表渲染。
3. **写闸双保险**：全局 write_gate 之外 put handler 内显式 writeauth.decide；
   匿名 PUT 必须 401（fail-closed），这条钉死"保险带不许退化成装饰"。
4. **前端对称四件**：lp/gh 两分片各有 sync（载入拉取）+ push（切换回写），
   且 api() 写方法自动带 token——缺任何一件 = 状态又退回"只在本浏览器"。
"""
import os
import pathlib
import shutil
import sys
import unittest

from fastapi import HTTPException
from starlette.requests import Request

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

import tiers  # noqa: E402

os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import db                        # noqa: E402
import prefs                     # noqa: E402

_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))


def _force_test_token() -> None:
    """同进程全量跑时其它用例可能改写 TERM_TOKEN——write belt 必须自钉凭据，
    否则 provided 与 secrets 错位 ⇒ 401 假红（09-26 全量跑实测）。"""
    os.environ["TERM_TOKEN"] = "unit-test-token-not-production"
    os.environ.pop("HUB_PASSCODE", None)


def _mktmp(prefix: str) -> pathlib.Path:
    _L0_TMP.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        d = _L0_TMP / (prefix + str(os.getpid()) + "-" + os.urandom(4).hex())
        try:
            d.mkdir()
            # v0.13.58：登记到进程级清单，退出时统一删。此前造完从不删，
            # 实测累积 230MB/2402 个目录 ⇒ 被项目扫描当真实项目（176 vs 应 ≈44）。
            return tiers.l0_fixture_register(d)
        except FileExistsError:
            continue
    raise RuntimeError("造夹具目录失败")


def _anon_put_request(path: str) -> Request:
    """最小 scope 的匿名 PUT Request（无任何 token 头）。"""
    scope = {"type": "http", "method": "PUT", "path": path,
             "headers": [], "query_string": b"", "scheme": "http",
             "server": ("127.0.0.1", 80)}
    return Request(scope)


def _token_put_request(path: str) -> Request:
    scope = {"type": "http", "method": "PUT", "path": path,
             "headers": [(b"x-hub-token", b"unit-test-token-not-production")],
             "query_string": b"", "scheme": "http",
             "server": ("127.0.0.1", 80)}
    return Request(scope)


class TestKeyWhitelist(unittest.TestCase):
    def setUp(self):
        self.tmp = _mktmp("prefs-")
        db.init_db(self.tmp / "prefs.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_unknown_key_read_404(self):
        with self.assertRaises(HTTPException) as cm:
            prefs.get_pref("arbitrary.config")
        self.assertEqual(cm.exception.status_code, 404)

    def test_unknown_key_write_404(self):
        req = _token_put_request("/api/prefs/arbitrary.config")
        with self.assertRaises(HTTPException) as cm:
            prefs.put_pref("arbitrary.config", req, prefs.PrefValue(stars=["/x"]))
        self.assertEqual(cm.exception.status_code, 404)

    def test_known_keys_exact(self):
        self.assertEqual(set(prefs.PREF_KEYS), {"projects.lp", "projects.gh"})


class TestNormalizeAndRoundtrip(unittest.TestCase):
    def setUp(self):
        self.tmp = _mktmp("prefs2-")
        db.init_db(self.tmp / "prefs.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_normalize_dedup_truncate_cap(self):
        v = prefs.PrefValue(stars=["/a", "/a", "", "x" * 600, "/b"],
                            hidden=["/h"])
        out = prefs.normalize_value(v)
        self.assertEqual(out["stars"], ["/a", "x" * 512, "/b"])
        self.assertEqual(out["hidden"], ["/h"])

    def test_normalize_item_cap(self):
        v = prefs.PrefValue(stars=[f"/p{i}" for i in range(prefs.MAX_ITEMS + 50)])
        self.assertEqual(len(prefs.normalize_value(v)["stars"]), prefs.MAX_ITEMS)

    def test_roundtrip(self):
        body = prefs.normalize_value(prefs.PrefValue(stars=["/a"], hidden=["/h"]))
        prefs._save_pref("projects.lp", body, "test")
        d = prefs.get_pref("projects.lp")
        self.assertEqual(d, {"key": "projects.lp",
                             "value": {"stars": ["/a"], "hidden": ["/h"]}})

    def test_corrupt_row_reads_as_none(self):
        db.execute("INSERT INTO app_prefs(key,value,updated_at) VALUES(?,?,?)",
                   ("projects.gh", "{not-json", "2026-09-26"))
        self.assertIsNone(prefs.get_pref("projects.gh")["value"])

    def test_second_write_upserts(self):
        prefs._save_pref("projects.lp", {"stars": ["/a"], "hidden": []}, "test")
        prefs._save_pref("projects.lp", {"stars": ["/b"], "hidden": []}, "test")
        self.assertEqual(prefs.get_pref("projects.lp")["value"]["stars"], ["/b"])


class TestWriteGateBelt(unittest.TestCase):
    """PUT 的显式保险带：匿名必须 401——write_gate 中间件在单测里不挂载，
    handler 内这道 decide 是唯一在测试里可断言的闸。"""

    def setUp(self):
        _force_test_token()
        self.tmp = _mktmp("prefs3-")
        db.init_db(self.tmp / "prefs.db")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_anonymous_put_401(self):
        req = _anon_put_request("/api/prefs/projects.lp")
        with self.assertRaises(HTTPException) as cm:
            prefs.put_pref("projects.lp", req, prefs.PrefValue(stars=["/x"]))
        self.assertEqual(cm.exception.status_code, 401)

    def test_authenticated_put_allowed_and_audited(self):
        req = _token_put_request("/api/prefs/projects.lp")
        d = prefs.put_pref("projects.lp", req, prefs.PrefValue(stars=["/x"]))
        self.assertEqual(d["value"]["stars"], ["/x"])
        rows = db.query("SELECT actor FROM asset_audit WHERE asset_slug='projects.lp'")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["actor"], "user:l0-test")


class TestFrontendSyncPair(unittest.TestCase):
    """两分片的 sync/push 四件 + 切换点必须调用 push——缺一即退回单浏览器语义。"""

    def setUp(self):
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_sync_and_push_defined_for_both(self):
        for fn in ("lpSyncPrefs", "lpPushPref", "ghSyncPrefs", "ghPushPref"):
            self.assertIn(f"async function {fn}(", self.js, f"{fn} 缺失")

    def test_toggles_call_push(self):
        for caller, pushed in (("lpToggleStar", "lpPushPref()"),
                               ("lpToggleHide", "lpPushPref()"),
                               ("ghToggleStar", "ghPushPref()"),
                               ("ghToggleHide", "ghPushPref()")):
            i = self.js.find(f"function {caller}(")
            self.assertGreater(i, 0, f"{caller} 缺失")
            self.assertIn(pushed, self.js[i:i + 600], f"{caller} 必须回写服务端")

    def test_load_triggers_sync_once(self):
        for loader, sync in (("loadLocalProjects", "lpSyncPrefs()"),
                             ("loadGithubRepos", "ghSyncPrefs()")):
            i = self.js.find(f"async function {loader}(")
            self.assertGreater(i, 0)
            self.assertIn(sync, self.js[i:i + 1400], f"{loader} 必须拉后端偏好")

    def test_prefs_endpoints_hit(self):
        self.assertIn("/api/prefs/projects.lp", self.js)
        self.assertIn("/api/prefs/projects.gh", self.js)


if __name__ == "__main__":
    unittest.main()
