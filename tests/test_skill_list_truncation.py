#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：`/api/skill/list` 的截断必须自报（PT-20261019-03 / v0.13.128）。

**【病灶】** v0.13.127 之前 `merged = merged[:limit]`，`limit` 默认 200，响应里只有
`count=len(merged)`。生产实测 `walked=487 → unique=371 → count=200`：
**12 路整路不可见**（jcode 33 / hermes-web 22 / techdocs 18 / superpowers 15 …），
前端统计条把 200 写成「共 200 个技能」，真浏览器探针也据此报「200 技能 / 21 路」。
少收得毫无痕迹——正好是 `EXCLUDED_DIRS` 注释里禁的「悄悄少收」形态。

**【判据】** `count` 与 `total` 必须分开：
- `total` = **过滤后**、截断前的条数（不是磁盘全局数，q 生效时它就是命中数）；
- `truncated` = `total > count`；
- `items` 长度 == `min(limit, total)`，三者必须自洽。

**【为什么造 205 条】** 默认 limit 就是 200——夹具不跨过这个数，
「默认截断」这条根本不会被触发，闸门会假绿。用 3 条夹具测截断是最典型的假闸门。

**【为什么还要查前端】** 后端修好而前端不传 limit，生产仍然是 200 条。
两处必须同批钉住：请求带 `limit=1000`，统计条读 `SKILL_TOTAL` 而非 `SKILLS.length`。
"""
import json
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db        # noqa: E402
import skill     # noqa: E402

from fastapi import FastAPI          # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

SHARD = _REPO / "static" / "hub" / "04-terminal-ws.js"


def _mk_skill(root: pathlib.Path, name: str, desc: str = "测试技能") -> pathlib.Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8")
    return d


class _ListCase(unittest.TestCase):
    """夹具：两路 tmp 技能根 + tmp 库 + 只挂 skill 路由的 TestClient（不起服务、不打网络）。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0listcap-"))
        self.a = self.tmp / "a"
        self.b = self.tmp / "b"
        for p in (self.a, self.b):
            p.mkdir()
        self._saved_dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"src": str(self.a), "other": str(self.b)}
        # 扫描缓存是进程级共享状态：每轮 tmp 都是新目录，不清会读到上一轮的
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        db.init_db(self.tmp / "t.db")
        _app = FastAPI()
        _app.include_router(skill.router)
        self.client = TestClient(_app)
        self.addCleanup(self._restore)

    def _restore(self):
        skill.SKILL_DIRS = self._saved_dirs
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _get(self, **params):
        r = self.client.get("/api/skill/list", params=params)
        self.assertEqual(r.status_code, 200, r.text)
        return r.json()


class TestListTruncation(_ListCase):
    def test_default_limit_reports_truncation(self):
        """默认 200 条被跨过时：count=200 / total=205 / truncated=True。"""
        for i in range(205):
            _mk_skill(self.a, "s%03d" % i)
        b = self._get()
        self.assertEqual(b["count"], 200)
        self.assertEqual(b["total"], 205, "total 必须是截断前的条数，不是磁盘全局数")
        self.assertTrue(b["truncated"], "少收了 5 条却不自报 = PT-20261019-03 原样复现")
        self.assertEqual(len(b["items"]), 200)
        self.assertEqual(b["dedup"]["unique"], 205)

    def test_full_page_clears_flag(self):
        """limit 覆盖全量时：count == total == 条数，truncated 必须为 False。"""
        for i in range(12):
            _mk_skill(self.a, "s%02d" % i)
        b = self._get(limit=1000)
        self.assertEqual(b["count"], 12)
        self.assertEqual(b["total"], 12)
        self.assertFalse(b["truncated"])
        self.assertEqual(len(b["items"]), 12)

    def test_total_is_post_filter_not_global(self):
        """`total` 是**过滤后**的条数：q 命中 3 条时不能报「本机共 12 条」。"""
        for i in range(12):
            _mk_skill(self.a, "alpha%02d" % i, "同类")
        _mk_skill(self.a, "beta-only", "别的")
        b = self._get(q="beta-only", limit=1000)
        self.assertEqual(b["total"], 1)
        self.assertFalse(b["truncated"])

    def test_truncated_when_filtered_set_exceeds_limit(self):
        """过滤后仍有截断时也要自报（total 与 count 的口径在 q 下同样成立）。"""
        for i in range(12):
            _mk_skill(self.a, "alpha%02d" % i, "同类")
        b = self._get(q="alpha", limit=5)
        self.assertEqual(b["count"], 5)
        self.assertEqual(b["total"], 12)
        self.assertTrue(b["truncated"])

    def test_cap_raised_to_1000(self):
        """上限 500→1000：生产 unique=371 已逼近旧上限，留出余量。"""
        _mk_skill(self.a, "one")
        self.assertEqual(self.client.get("/api/skill/list", params={"limit": 1000}).status_code, 200)
        self.assertEqual(self.client.get("/api/skill/list", params={"limit": 1001}).status_code, 422)


class TestFrontendRequestsFullAndShowsTruncation(unittest.TestCase):
    """后端修好但前端不传 limit，生产依旧少收——两处必须同批钉。"""

    def setUp(self):
        self.src = SHARD.read_text(encoding="utf-8")

    def test_loadskills_requests_full_page(self):
        self.assertIn("/api/skill/list?limit=1000", self.src,
                      "前端不传 limit ⇒ 后端默认 200，371 条里 12 路整路不可见")

    def test_skills_length_is_not_used_as_total(self):
        self.assertIn("const nSkill = SKILL_TOTAL || SKILLS.length;", self.src,
                      "统计条把「显示到的」当总量，正是这个 bug 骗过探针的方式")
        self.assertIn("if (SKILL_TRUNCATED)", self.src,
                      "截断时没有任何上屏提示 ⇒ 用户仍会读成「本机只有这么多」")


class TestMcpFacadePropagatesTruncation(unittest.TestCase):
    """MCP 门面只透传 count 的话，agent 侧照样看到「本机就这么多」——
    双层门面（REST + MCP）必须同口径。`_get` 猴补，不打网络。"""

    def setUp(self):
        import hubmcp
        self.h = hubmcp
        self._saved = hubmcp._get
        self.addCleanup(lambda: setattr(hubmcp, "_get", self._saved))

    def test_list_passes_total_and_truncated(self):
        self.h._get = lambda *a, **k: {
            "count": 200, "total": 371, "truncated": True, "items": [], "engine": "x"}
        out = json.loads(self.h.hub_skill_list(limit=999))
        self.assertEqual(out["total"], 371)
        self.assertTrue(out["truncated"])

    def test_catalog_computes_truncated_from_total(self):
        self.h._get = lambda *a, **k: {"count": 200, "total": 371, "items": []}
        out = json.loads(self.h.hub_skill_catalog())
        self.assertTrue(out["truncated"], "count<total 却没标 truncated ⇒ 门面复刻静默截断")


if __name__ == "__main__":
    unittest.main()
