# 读令牌门 L0 判据（v0.13.192 P0）：非回环 GET 须凭据、回环匿名保持一版本。
# 2026-11-07 收成 TestCase：原模块级裸 test_（含 1 个 pytest 的 monkeypatch fixture）
# 只有 pytest 收、unittest discover 不收 ⇒ tier parity 对账差 11 条。
# monkeypatch 参数手工改成保存/恢复（unittest 无 fixture 机制）。
import asyncio
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import writeauth  # noqa: E402


class TestReadTokenL0(unittest.TestCase):

    def test_read_verdict_loopback_anonymous_allowed(self):
        # 双栈 `HOST=::` 下 v4 回环连接的对端实测是 `::ffff:127.0.0.1`（v4-mapped v6），
        # 漏它＝本机页面全被拒（2026-11-07 上线首探实踩）。
        for host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:0:0:0:0:0:0:1"):
            v, r = writeauth.read_verdict(host, "/api/agents", False, True)
            self.assertEqual(v, "allow", (host, r))

    def test_read_verdict_nonloopback_anonymous_denied(self):
        v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", False, True)
        self.assertEqual(v, "deny", (v, r))

    def test_read_verdict_nonloopback_identity_allowed(self):
        """2026-11-07：身份 = Cookie 会话 / HTTP Basic，不再是比较共享 token。"""
        v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", True, True)
        self.assertEqual(v, "allow", (v, r))

    def test_read_verdict_users_db_down_is_misconfig_not_open(self):
        v, r = writeauth.read_verdict("192.168.5.9", "/api/agents", False, False)
        self.assertEqual(v, "misconfig", (v, r))

    def test_read_verdict_disabled_gate_always_allows(self):
        old = writeauth.READ_TOKEN_DISABLED
        writeauth.READ_TOKEN_DISABLED = True
        try:
            v, _ = writeauth.read_verdict("8.8.8.8", "/api/memory/search", False, False)
            self.assertEqual(v, "allow")
        finally:
            writeauth.READ_TOKEN_DISABLED = old

    def test_read_allow_prefixes_injected_like_csp_report(self):
        # main 的注入模式：加前缀 → 放行；逐条写理由，测试钉住「注入即生效」
        allow = writeauth._READ_TOKEN_ALLOW
        allow.add("/api/settings/model/catalog")
        try:
            v, _ = writeauth.read_verdict("8.8.8.8", "/api/settings/model/catalog", False, True)
            self.assertEqual(v, "allow")
            v, _ = writeauth.read_verdict("8.8.8.8", "/api/memory/search", False, True)
            self.assertEqual(v, "deny")
        finally:
            allow.discard("/api/settings/model/catalog")

    def test_read_banner_state_shapes(self):
        self.assertEqual(writeauth.read_banner_state("127.0.0.1", "/", False, True)["state"], "ok")
        self.assertEqual(writeauth.read_banner_state("8.8.8.8", "/", False, True)["state"], "locked")
        self.assertEqual(writeauth.read_banner_state("8.8.8.8", "/", False, False)["state"], "misconfig")

    def test_read_gate_is_async_middleware(self):
        self.assertTrue(asyncio.iscoroutinefunction(writeauth.read_gate))

    def test_main_registers_read_gate_before_write_gate(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
        i = src.index("writeauth.read_gate")
        j = src.index('app.middleware("http")(write_gate)')
        self.assertLess(i, j, "read_gate 必须先于 write_gate 注册（外层先放行/拒读，再轮到写门）")

    def test_main_injects_disable_switch_and_allow_prefix(self):
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
        self.assertIn("HUB_READ_TOKENS", src)
        self.assertIn("writeauth._READ_TOKEN_ALLOW.add", src)

    def test_version_bumped(self):
        import re
        src = open(os.path.join(os.path.dirname(__file__), "..", "src", "main.py")).read()
        m = re.search(r'VERSION = "(0\.13\.(\d+))"', src)
        self.assertIsNotNone(m, "读令牌门要求的最低版本未达标")
        self.assertGreaterEqual(int(m.group(2)), 192)


if __name__ == "__main__":
    unittest.main()
