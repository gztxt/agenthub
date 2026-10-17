#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：kb 联邦检索扩 workspace/archived 两路（v0.13.26 批3）。

分层口径：零宿主依赖——memfed 的 rg 源猴补到 tmp 夹具、db 指向 tmp 库、不打网络、
不起服务。**不允许 SKIP**。

为什么这些用例必须存在（红向都能确定性造）：
1. **假接入（backends 绿但融合不可见）**：实测满权 tdai 池会把 0.7/0.4 权的低权源
   全部挤出融合前列——两路等于摆设。必须断言融合条目里**真出现** workspace/archived。
2. **失败表态**：源根消失时 rg 源必须 ok=false + error 非空（不许静默空过），
   且**不拖垮**其余路（kb 立身口径）。
3. **路由白名单**：未知 routes 值 400 并回显可用值（防手一抖打出静默空结果）。
"""
import asyncio
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db                     # noqa: E402
import kb                     # noqa: E402
import memfed                 # noqa: E402
import memindex               # noqa: E402
import memory                 # noqa: E402

from fastapi import FastAPI   # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


def _mk_md(root: pathlib.Path, rel: str, text: str) -> pathlib.Path:
    p = root / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


class _KbCase(unittest.TestCase):
    """夹具：tmp 工作区（workspace+archived 两源根）+ tmp 库 + TestClient 挂 kb 路由。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0kb3-"))
        self.ws = self.tmp / "ws"        # workspace_files 的根
        self.ar = self.tmp / "ar"        # archived_sessions 的根
        for p in (self.ws, self.ar):
            p.mkdir()
        _mk_md(self.ws, "memory/MEMORY.md", "CCR 网关口径记录")
        _mk_md(self.ws, "agent-knowledge/x.md", "agent-hub 批3 测试文档")
        _mk_md(self.ar, "claude/s1.md", "CCR 修复会话摘录")
        _mk_md(self.ar, "pi/s2.md", "批3 归档测试")
        # 猴补 memfed 的两个 rg 根（kb 经 _FED_ROUTE_MAP → memfed._RG_TARGETS）。
        # ⚠️ archived_sessions 走 memindex FTS 投影，**不读 _RG_TARGETS**：
        # 必须同时把投影索引指向不存在的路径触发 fallback=no_index，投影才会
        # fail-closed 回退 rg，本类用例才真的在测「根消失」而不是在测生产索引
        # （2026-10-08 修正：test_missing_root_reports_error_not_silent 此前一直假绿）。
        self._saved_dbpath = memindex.default_db_path
        memindex.default_db_path = lambda: self.tmp / "nope-idx.db"
        self._saved_targets = dict(memfed._RG_TARGETS)
        memfed._RG_TARGETS["workspace_files"] = ([str(self.ws)], "*.md")
        memfed._RG_TARGETS["archived_sessions"] = ([str(self.ar)], None)
        db.init_db(self.tmp / "t.db")
        self._saved_wl = memory.SOURCE_WHITELIST
        self.client = TestClient(self._app())
        self.addCleanup(self._restore)

    def _app(self):
        app = FastAPI()
        app.include_router(kb.router)
        return app

    def _restore(self):
        memindex.default_db_path = self._saved_dbpath
        memfed._RG_TARGETS = self._saved_targets
        memory.SOURCE_WHITELIST = self._saved_wl
        shutil.rmtree(self.tmp, ignore_errors=True)


class TestRoutes(_KbCase):
    def test_routes_registry_has_two_new(self):
        self.assertIn("workspace", kb.ROUTES)
        self.assertIn("archived", kb.ROUTES)

    def test_unknown_route_400_with_available_list(self):
        r = self.client.get("/api/kb/search", params={"q": "x", "routes": "workspace,ghost"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("workspace", r.json()["detail"])
        self.assertIn("archived", r.json()["detail"])

    def test_fed_map_covers_both(self):
        self.assertEqual(set(kb._FED_ROUTE_MAP.values()),
                         {"workspace_files", "archived_sessions"})


class TestSearch(_KbCase):
    def test_search_workspace_and_archived_ok(self):
        r = self.client.get("/api/kb/search", params={"q": "CCR", "routes": "workspace,archived"})
        self.assertEqual(r.status_code, 200, r.text)
        d = r.json()
        bk = {b["name"]: b for b in d["backends"]}
        self.assertTrue(bk["workspace"]["ok"])
        self.assertGreater(bk["workspace"]["count"], 0)
        self.assertTrue(bk["archived"]["ok"])
        self.assertGreater(bk["archived"]["count"], 0)

    def test_fused_results_really_include_fed_sources(self):
        """假接入护栏：低权源 backends 绿还不够，融合条目必须真出现其来源。"""
        r = self.client.get("/api/kb/search", params={"q": "CCR", "routes": "workspace,archived"})
        d = r.json()
        froms = set()
        for it in d["results"]:
            froms.update(it.get("from") or [])
            froms.add(it.get("source") or "")
        self.assertIn("workspace_files", froms, f"融合结果未见 workspace 条目：{froms}")
        self.assertIn("archived_sessions", froms, f"融合结果未见 archived 条目：{froms}")

    def test_fed_weights_full_in_kb_context(self):
        """kb 语境两路满权（实测 0.7/0.4 会被 tdai 池挤出融合面=假接入）。"""
        W = kb._KB_W if hasattr(kb, "_KB_W") else None
        if W is None:  # 权重在函数体内：走行为断言替代
            r = self.client.get("/api/kb/search",
                                params={"q": "批3", "routes": "workspace,archived", "k": 10})
            d = r.json()
            froms = {it.get("source") for it in d["results"]}
            self.assertTrue(froms & {"workspace_files", "archived_sessions"})
        else:
            self.assertEqual(W["workspace"], 1.0)
            self.assertEqual(W["archived"], 1.0)

    def test_missing_root_reports_error_not_silent(self):
        shutil.rmtree(self.ar)
        r = self.client.get("/api/kb/search", params={"q": "CCR", "routes": "archived,workspace"})
        d = r.json()
        bk = {b["name"]: b for b in d["backends"]}
        self.assertFalse(bk["archived"]["ok"])
        self.assertTrue(bk["archived"]["error"], "根消失必须带 error 说明，不许静默空过")
        self.assertTrue(bk["workspace"]["ok"], "一路挂不许拖垮另一路")

    def test_lone_route_only(self):
        r = self.client.get("/api/kb/search", params={"q": "归档测试", "routes": "archived"})
        d = r.json()
        self.assertEqual([b["name"] for b in d["backends"]], ["archived"])
        self.assertGreater(d["count"], 0)


class TestStatus(_KbCase):
    def test_status_has_fed_sections(self):
        r = self.client.get("/api/kb/status")
        self.assertEqual(r.status_code, 200)
        d = r.json()
        for seg in ("workspace", "archived"):
            self.assertIn(seg, d)
            self.assertIn(d[seg]["available"], (True, False))
            self.assertIn("count", d[seg])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestXerjRoute(_KbCase):
    """2026-10-18：文档路由 turbovec 换成 XERJ。三条都是红向能确定性造的。"""

    def test_xerj_in_default_routes(self):
        self.assertIn("xerj", kb.ROUTES)
        self.assertNotIn("turbovec", kb.ROUTES, "退役路不得留在默认集合里（会白跑子进程）")

    def test_legacy_turbovec_retired_not_400(self):
        """老调用方传 routes=turbovec：不 400、不起子进程，逐路回退役说明。"""
        r = self.client.get("/api/kb/search", params={"q": "x", "routes": "turbovec"})
        self.assertEqual(r.status_code, 200, r.text)
        b = {x["name"]: x for x in r.json()["backends"]}
        self.assertIn("turbovec", b)
        self.assertFalse(b["turbovec"]["ok"])
        self.assertIn("退役", b["turbovec"]["error"])

    def test_workspace_skipped_when_xerj_hits(self):
        """XERJ 命中 ⇒ workspace 不参与融合（否则同一批文档两套排序重复计分）。"""
        async def fake(q, k):
            return {"ok": True, "ms": 1.0, "error": None, "items": [
                {"source": "xerj", "id": "wiki/a.md", "path": "wiki/a.md",
                 "title": "a", "content": "x", "score": 9.0}]}
        saved = kb._xerj_search
        kb._xerj_search = fake
        try:
            r = self.client.get("/api/kb/search", params={"q": "CCR", "routes": "xerj,workspace"})
        finally:
            kb._xerj_search = saved
        d = r.json()
        b = {x["name"]: x for x in d["backends"]}
        self.assertTrue(b["xerj"]["ok"])
        self.assertIn("skipped", b["workspace"])
        self.assertNotIn("workspace_files", {it.get("source") for it in d["results"]})
