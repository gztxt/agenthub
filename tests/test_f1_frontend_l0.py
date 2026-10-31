#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""任务 F1 的三条 L0 闸门（2026-10-31）。

L0 口径（tests/tiers.py）：不 import src.main、不起服务、不打网络。
因此「CSP 头真实存在」用最小 Starlette app + `csp.middleware` 验（与 main 同一函数），
`main.py` 里那段 report-uri 追加只做**源码形状**断言 —— 它依赖 app 组装，属于 L1 面。

三条判据（每条都能被打回）：
1. 命令面板：Ctrl/Cmd+K 是唯一快捷键入口，仍只有 openCmd/closeCmd，列表走 data-* 委托；
2. showToast(kind,msg)：四种 kind 全部映射到既有 CSS 类，且不新增字面色值；
3. CSP：默认头名是 `Content-Security-Policy-Report-Only`，且带 report-uri 出口。
"""

import re
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

HUB = ROOT / "static" / "hub"
TM = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS01 = (HUB / "01-core-boot.js").read_text(encoding="utf-8")
JS05 = (HUB / "05-chat-and-history.js").read_text(encoding="utf-8")
JS06 = (HUB / "06-manager-tasks.js").read_text(encoding="utf-8")
MAIN = (ROOT / "src" / "main.py").read_text(encoding="utf-8")


class TestCommandPaletteShortcut(unittest.TestCase):
    def test_ctrl_and_cmd_k_open_toggle(self):
        keydown = JS06.split("document.addEventListener('keydown'")[1]
        self.assertIn("(e.metaKey || e.ctrlKey)", keydown, "缺 Cmd/Ctrl 组合键判断")
        self.assertIn("(e.key === 'k' || e.key === 'K')", keydown, "缺 K 键判断")
        self.assertIn("? closeCmd() : openCmd()", keydown,
                      "Ctrl/Cmd+K 必须是开/关切换，不是只开")

    def test_editing_target_does_not_hijack(self):
        """终端/输入位里 Ctrl+K 归 pty（bash 的 kill-line），不许被面板抢走。"""
        body = JS06.split("function keyTargetIsEditing")[1].split("document.addEventListener")[0]
        self.assertIn("t.closest('.xterm')", body)
        self.assertIn("tag === 'INPUT'", body)
        self.assertIn("if (editing) return;", JS06)

    def test_single_mask_and_two_entry_points(self):
        self.assertEqual(TM.count('id="cmdMask"'), 1, "命令面板遮罩必须唯一")
        self.assertEqual(JS05.count("function openCmd"), 1)
        self.assertEqual(JS05.count("function closeCmd"), 1)
        for m in re.finditer(r"\$\('cmdMask'\)\.classList\.(add|remove)\('on'\)", JS05):
            fn = JS05[JS05.rfind("function ", 0, m.start()):][:40]
            self.assertTrue("function openCmd" in fn or "function closeCmd" in fn,
                            "开关只允许 openCmd/closeCmd，发现：" + fn.strip())

    def test_actions_are_delegated_not_inline(self):
        self.assertIn("data-sk-cmdgo", JS05)
        self.assertIn("const c = e.target.closest('[data-sk-cmdgo]')", JS05)
        seg = JS05.split("function renderCmdList")[1].split("function cmdHighlight")[0]
        self.assertNotIn("onclick=", seg, "命令项不许 inline onclick")

    def test_required_actions_present(self):
        """切 agent / 开设置 / 开终端 三类入口都能从面板到达。"""
        for action in ("'terminal'", "'settings'", "'register'", "'scan'"):
            self.assertIn("id: " + action, JS05, "动作条目缺失：" + action)
        for fn in ("gotoChat(pick, 'term')", "go('settings-model')",
                   "openRegister()", "runScan()"):
            self.assertIn(fn, JS05, "动作未接既有入口：" + fn)

    def test_escape_closes(self):
        self.assertIn("if ($('cmdMask') && $('cmdMask').classList.contains('on')) { closeCmd(); return; }",
                      JS06)


class TestShowToastKinds(unittest.TestCase):
    def setUp(self):
        self.body = JS01.split("function showToast(")[1].split("window.showToast")[0]

    def test_four_kinds_mapped(self):
        self.assertIn("const TOAST_KIND_CLASS = { info: 'info', ok: 'ok', warn: 'warn', error: 'err' }",
                      JS01)
        self.assertIn("hasOwnProperty.call(TOAST_KIND_CLASS, kind)", self.body)
        self.assertIn("return toast(", self.body, "showToast 必须复用既有 toast()（不另造容器）")

    def test_unknown_kind_warns_not_silent(self):
        self.assertIn("按 info 处理", self.body)
        self.assertIn("console.warn", self.body)

    def test_css_classes_use_tokens_only(self):
        for cls in ("info", "warn", "err", "ok"):
            self.assertIn("#toast div.%s {" % cls, TM, "缺 toast 类：" + cls)
        for line in TM.splitlines():
            if re.search(r"#toast div\.(info|warn|err|ok)\b", line):
                self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", line), "禁止字面色：" + line)
                self.assertIsNone(re.search(r"\brgba?\(", line), "禁止字面色：" + line)

    def test_loading_uses_skeleton(self):
        self.assertIn(".skel-row", TM)
        self.assertIn("class=\"skel\"", JS01, "boxBusy 应渲染骨架占位")
        self.assertNotIn('<div class="hint">\' + escapeHtml(msg', JS01,
                         "busy 态退回一行文字占位（列表区会塌高度）")


class TestCspReportOnly(unittest.TestCase):
    def test_default_header_is_report_only(self):
        import csp
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def home(request):
            return PlainTextResponse("ok")

        import os
        old = os.environ.pop("HUB_CSP", None)
        try:
            app = Starlette(routes=[Route("/", home)])
            app.add_middleware(BaseHTTPMiddleware, dispatch=csp.middleware)
            r = TestClient(app).get("/")
        finally:
            if old is not None:
                os.environ["HUB_CSP"] = old
        self.assertEqual(r.status_code, 200)
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)
        self.assertNotIn("Content-Security-Policy", r.headers, "默认不得发强制头")
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy-Report-Only"])

    def test_report_uri_outlet_exists(self):
        self.assertIn('CSP_REPORT_PATH = "/api/csp/report"', MAIN)
        self.assertIn("report-uri ", MAIN, "响应头必须追加 report-uri")
        self.assertIn('@app.post("/api/csp/report")', MAIN, "缺只收 JSON 的报告端点（路径必须是字面量，writeauth 静态核对依赖它）")
        self.assertIn("json.loads(raw", MAIN)
        self.assertIn('"/api/csp/report"', MAIN.split("_RATE_EXCLUDED_PREFIXES = (")[1][:200],
                      "报告端点必须免限流（否则浏览器上报会被 429）")

    def test_report_endpoint_is_writegate_exempt(self):
        """浏览器不会带自定义头；端点只记日志 ⇒ 必须在写闸门豁免，否则报告全 401。"""
        self.assertIn("writeauth.EXEMPT_PREFIXES[CSP_REPORT_PATH]", MAIN)
        self.assertIn("只写日志", MAIN)

    def test_csp_module_not_forced_by_default(self):
        import csp
        import os
        old = os.environ.pop("HUB_CSP", None)
        try:
            self.assertEqual(csp.mode(), "report")
            self.assertEqual(csp.header_name(), "Content-Security-Policy-Report-Only")
        finally:
            if old is not None:
                os.environ["HUB_CSP"] = old


if __name__ == "__main__":
    unittest.main(verbosity=2)
