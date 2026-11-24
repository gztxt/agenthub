#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：批4 前端两中心（技能/知识库页）+ /api/kb/browse。

分层口径：零宿主依赖——浏览根猴补到 tmp 夹具；页面断言只读静态文件（不出网、
不渲染 DOM，真渲染闸门在集成批次由端侧验收承担）。**不允许 SKIP**。

为什么这些用例必须存在：
1. **browse 穿越攻击**：sub 拼/绝对路径/.. 会跳出工作区读无关目录（含敏感文件）
   ⇒ 400 拒绝 + 根名白名单匹配。
2. **section 必须真的存在**：go() 对未知页面回退 classroom（09-23 事故第二幕）——
   SYS_PAGES 里有页名但 index.html 没有 section ⇒ 静默退回总览，面板永远看不到。
3. **懒加载钩子成对**：go() 里每页必须挂 loader，漏挂=页面空白（不报错的那种）。
"""
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import kb                     # noqa: E402
import memfed                 # noqa: E402

from fastapi import HTTPException, FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402


class TestBrowse(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0kbb-"))
        self.doc = self.tmp / "doc"         # 知识库根目录
        (self.doc / "agent-knowledge").mkdir(parents=True)
        (self.doc / "agent-knowledge" / "01-x.md").write_text("a", encoding="utf-8")
        (self.doc / "agent-knowledge" / "sub").mkdir()
        (self.doc / "agent-knowledge" / "sub" / "y.md").write_text("b", encoding="utf-8")
        (self.doc / "memory").mkdir()
        (self.doc / "memory" / "m.md").write_text("c", encoding="utf-8")
        (self.doc / "MEMORY.md").write_text("d", encoding="utf-8")
        self._saved = dict(memfed._RG_TARGETS)
        memfed._RG_TARGETS["workspace_files"] = (
            [str(self.doc / "MEMORY.md"), str(self.doc / "memory"),
             str(self.doc / "agent-knowledge")], "*.md")
        app = FastAPI()
        app.include_router(kb.router)
        self.client = TestClient(app)
        self.addCleanup(self._restore)

    def _restore(self):
        memfed._RG_TARGETS = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_browse_top_lists_all_roots(self):
        d = self.client.get("/api/kb/browse").json()
        names = {e["name"] for e in d["entries"]}
        self.assertIn("MEMORY.md", names)
        self.assertIn("memory", names)
        self.assertIn("agent-knowledge", names)

    def test_browse_sub_lists_root_content(self):
        d = self.client.get("/api/kb/browse", params={"sub": "agent-knowledge"}).json()
        names = {e["name"] for e in d["entries"]}
        self.assertIn("01-x.md", names)
        self.assertIn("sub", names)

    def test_browse_traversal_rejected(self):
        for evil in ("../etc", "..", "/etc", "a/b", "a/../b", "."):
            r = self.client.get("/api/kb/browse", params={"sub": evil})
            self.assertIn(r.status_code, (400, 422), f"{evil!r} 必须被拒：{r.status_code}")

    def test_browse_unknown_root_404(self):
        r = self.client.get("/api/kb/browse", params={"sub": "nope"})
        self.assertEqual(r.status_code, 404)

    def test_browse_missing_root_reported_not_silent(self):
        memfed._RG_TARGETS["workspace_files"] = ([str(self.tmp / "ghost")], "*.md")
        d = self.client.get("/api/kb/browse").json()
        self.assertTrue(d["errors"], "根消失必须出现在 errors，不许静默")


class TestFrontendPages(unittest.TestCase):
    """静态断言：section/SYS_PAGES/懒加载钩子成对（不出网不渲染 DOM）。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_sections_exist_for_new_pages(self):
        for pid in ("page-skills", "page-kb"):
            self.assertIn(pid, self.html, f"{pid} section 缺失 ⇒ go() 会静默退回总览")

    def test_sys_pages_registry_has_kb(self):
        # 2026-11-20 v0.13.206「前端瘦身」：「技能中心」页按实测（21 天 0~1 次调用）删除、
        # 端点保留 ⇒ 本闸门只守仍在册的「知识库」。原方法名 has_both 已不成立。
        self.assertIn("['kb', '知识库'", self.js)

    def test_page_labels_cover_both(self):
        self.assertIn("skills: '技能中心'", self.js)
        self.assertIn("kb: '知识库'", self.js)

    def test_lazy_hooks_paired(self):
        self.assertIn("page === 'skills'", self.js)
        self.assertIn("page === 'kb'", self.js)

    def test_loader_functions_defined(self):
        for fn in ("loadSkills", "renderSkillList", "skillInstall", "loadSkillBudget",
                   "kbSearch", "loadKbStatus", "kbBrowse"):
            self.assertIn(f"function {fn}(", self.js, f"{fn} 缺失 ⇒ 面板点了没反应")

    def test_dom_ids_used_by_js_exist_in_html(self):
        for dom_id in ("skillQ", "skillRoute", "skillList", "instName", "instFrom",
                       "instTo", "skillBudget", "kbQ", "kbRoutes", "kbResults",
                       "kbStatus", "kbBrowse"):
            self.assertIn(f'id="{dom_id}"', self.html,
                          f"#{dom_id} 在 JS 里被引用但 HTML 缺失 ⇒ null 崩溃")

    def test_inline_handlers_reference_real_functions(self):
        """新页里每个 inline onclick 的函数名都必须真实存在（防手滑拼错函数名）。"""
        import re
        for m in re.finditer(r'onclick="(?!if\()([a-zA-Z_$][\w$]*)\(', self.html):
            fn = m.group(1)
            self.assertIn(f"function {fn}(", self.js,
                          f"HTML 引用 {fn}() 但 hub.js 未定义（点击即报错）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
