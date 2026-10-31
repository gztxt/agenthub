"""CSP / 安全响应头（v0.13.185）—— L0 纯函数层。

不导入 `src.main`：它会建库、读 .env、探测网关，属于 L1 面。这里只验 `csp` 模块
自身的行为，并用一个最小 Starlette app 验中间件真的把头写进响应。

判据（每条都能被打回）：
- 默认是 **Report-Only**（强制头名只允许在 HUB_CSP=enforce 时出现）
- `off` 一个头都不加（不是"加空值"）
- 头名与策略内容一致（enforce ↔ Content-Security-Policy）
- 关键指令必须在：default-src / frame-ancestors / connect-src / worker-src
"""

import os
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import csp  # noqa: E402


class TestPolicyShape(unittest.TestCase):
    def test_recommended_directives_present(self):
        for d in ("default-src 'self'", "frame-ancestors 'none'", "object-src 'none'",
                  "connect-src 'self' ws: wss:", "worker-src 'self' blob:",
                  "base-uri 'self'", "form-action 'self'"):
            self.assertIn(d, csp.POLICY, f"缺指令：{d}")

    def test_no_wildcard_source(self):
        # `*` 作为源会让整条策略失去意义；通配只允许出现在 `ws:`/`wss:` 这种 scheme 位
        for part in csp.POLICY.split(";"):
            toks = part.strip().split()
            self.assertNotIn("*", toks, f"出现通配源：{part.strip()}")

    def test_hardening_headers_are_safe_values(self):
        self.assertEqual(csp.HARDENING["X-Content-Type-Options"], "nosniff")
        self.assertEqual(csp.HARDENING["X-Frame-Options"], "DENY")
        self.assertEqual(csp.HARDENING["Referrer-Policy"], "same-origin")


class TestModeSwitch(unittest.TestCase):
    def setUp(self):
        self._old = os.environ.get("HUB_CSP")

    def tearDown(self):
        if self._old is None:
            os.environ.pop("HUB_CSP", None)
        else:
            os.environ["HUB_CSP"] = self._old

    def test_default_is_report_only(self):
        os.environ.pop("HUB_CSP", None)
        self.assertEqual(csp.mode(), "report")
        self.assertEqual(csp.header_name(), "Content-Security-Policy-Report-Only")

    def test_enforce_switches_header_name(self):
        os.environ["HUB_CSP"] = "enforce"
        self.assertEqual(csp.header_name(), "Content-Security-Policy")

    def test_typo_falls_back_to_report_not_enforce(self):
        # 拼错一律宽松：静默变 enforce 会让用户侧"终端黑屏"且服务端零错误
        os.environ["HUB_CSP"] = "enforc"
        self.assertEqual(csp.mode(), "report")
        self.assertEqual(csp.header_name("enforc"), "Content-Security-Policy-Report-Only")


class TestApply(unittest.TestCase):
    def _resp(self):
        from starlette.responses import Response
        return Response("ok")

    def test_off_adds_nothing(self):
        r = csp.apply(self._resp(), "off")
        for k in list(csp.HARDENING) + ["Content-Security-Policy",
                                        "Content-Security-Policy-Report-Only"]:
            self.assertNotIn(k, r.headers)

    def test_report_adds_both_kinds(self):
        r = csp.apply(self._resp(), "report")
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)
        self.assertNotIn("Content-Security-Policy", r.headers)
        self.assertEqual(r.headers["X-Frame-Options"], "DENY")

    def test_enforce_adds_forced_header_only(self):
        r = csp.apply(self._resp(), "enforce")
        self.assertIn("Content-Security-Policy", r.headers)
        self.assertNotIn("Content-Security-Policy-Report-Only", r.headers)

    def test_existing_header_not_overwritten(self):
        # 上游（反代/自定义路由）已经设过的头不许被覆盖：本模块是兜底不是权威
        r = self._resp()
        r.headers["X-Frame-Options"] = "SAMEORIGIN"
        r = csp.apply(r, "report")
        self.assertEqual(r.headers["X-Frame-Options"], "SAMEORIGIN")


class TestMiddleware(unittest.TestCase):
    def test_middleware_sets_header_on_real_response(self):
        from starlette.applications import Starlette
        from starlette.middleware.base import BaseHTTPMiddleware
        from starlette.responses import PlainTextResponse
        from starlette.routing import Route
        from starlette.testclient import TestClient

        async def home(request):
            return PlainTextResponse("ok")

        app = Starlette(routes=[Route("/", home)])
        # Starlette 没有 FastAPI 的 @app.middleware 装饰器，要走 BaseHTTPMiddleware
        app.add_middleware(BaseHTTPMiddleware, dispatch=csp.middleware)
        os.environ.pop("HUB_CSP", None)
        c = TestClient(app)
        r = c.get("/")
        self.assertEqual(r.status_code, 200)
        self.assertIn("Content-Security-Policy-Report-Only", r.headers)
        self.assertIn("default-src 'self'", r.headers["Content-Security-Policy-Report-Only"])


if __name__ == "__main__":
    unittest.main()


class TestFrameSrc(unittest.TestCase):
    """hub 的核心用法是嵌入各 agent 自己的 Web 界面 ⇒ 必须显式给 frame-src。

    2026-10-31 实测：缺 frame-src 时浏览器回落 `default-src 'self'`，
    每个嵌入面板都在控制台报 `Framing 'http://100.117.232.62:30141/' violates ...`。
    这条闸门钉的是**「frame-src 不许被 default-src 兜走」**，不是某个具体网段。
    """

    def test_frame_src_directive_present(self):
        self.assertIn("frame-src ", csp.POLICY,
                      "缺 frame-src ⇒ 嵌入面板全部回落 default-src 'self' 报违规")

    def test_default_frame_src_covers_remote_panels(self):
        # 默认值必须容得下局域网/Tailscale/IPv6 三类网段（IP 段通配在 CSP 里非法，
        # 所以只能按 scheme 放开；收紧走 HUB_CSP_FRAME_SRC，见 csp._frame_src 注释）
        seg = [p for p in csp.POLICY.split(";") if p.strip().startswith("frame-src")][0]
        for need in ("'self'", "http:", "https:"):
            self.assertIn(need, seg, f"frame-src 缺 {need}：远程面板会被拦")

    def test_env_can_narrow_frame_src(self):
        old = os.environ.get("HUB_CSP_FRAME_SRC")
        try:
            os.environ["HUB_CSP_FRAME_SRC"] = "'self' http://192.168.5.102:*"
            self.assertEqual(csp._frame_src(), "'self' http://192.168.5.102:*",
                             "HUB_CSP_FRAME_SRC 必须能收窄白名单")
        finally:
            if old is None:
                os.environ.pop("HUB_CSP_FRAME_SRC", None)
            else:
                os.environ["HUB_CSP_FRAME_SRC"] = old
