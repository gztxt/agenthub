#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P2 C5 出站地址准入（src/outbound.py + ghsettings/mcpgw 接线）。

手册 C5：「出站默认拒绝，封禁云元数据端点与内网保留段」。

守的判据：

1. **元数据端点任何场景都拒** —— 即便 `allow_private=True`（MCP 允许本机），
   云元数据也不放行（SSRF 的首要目标）。
2. **内网档由调用方决定**，且两处口径不同且**各有理由**：
   ghsettings 的「远程 API 基址」指向内网无正当用途 ⇒ 拒；
   mcpgw 的 MCP server **常在 127.0.0.1** ⇒ 默认允许（否则是把安全做成可用性事故）。
3. **一份判据两处用**：ghsettings 必须**委托**给 outbound（不是自留一份表）。
4. **如实标注边界**：不做 DNS ⇒ `evil.example.com` 解析到元数据**拦不住**。
   这条**正向断言**（`is None`）是刻意的 —— 把已知边界写进测试，免得日后被当成已覆盖。
5. URL 层：https 强制、拒带凭据、拒非法 scheme/端口。

分层口径（tests/tiers.py）：纯函数 + 源码文本断言，不起服务、不连网、零 SKIP。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import outbound  # noqa: E402


class TestMetadataAlwaysBlocked(unittest.TestCase):
    def test_metadata_blocked_even_when_private_allowed(self):
        for h in ("169.254.169.254", "fd00:ec2::254", "100.100.100.200"):
            for allow in (True, False):
                why = outbound.blocked_reason(h, allow_private=allow)
                self.assertIsNotNone(why, "%s allow_private=%s 必须拒" % (h, allow))
                self.assertIn("元数据", why)

    def test_metadata_hostnames(self):
        for h in ("metadata.google.internal", "metadata", "instance-data"):
            self.assertIsNotNone(outbound.blocked_reason(h, allow_private=True))


class TestPrivateTier(unittest.TestCase):
    PRIVATE = ("127.0.0.1", "10.1.2.3", "192.168.5.102", "172.16.0.1", "::1",
               "fe80::1", "0.0.0.0", "localhost", "svc.local", "x.internal",
               "foo.home.arpa")

    def test_strict_blocks_private(self):
        for h in self.PRIVATE:
            self.assertIsNotNone(outbound.blocked_reason(h, allow_private=False), h)

    def test_lenient_allows_private_but_not_metadata(self):
        for h in self.PRIVATE:
            self.assertIsNone(outbound.blocked_reason(h, allow_private=True), h)

    def test_public_hosts_pass_both_ways(self):
        # ⚠ 不用 203.0.113.x（TEST-NET-3）—— `ipaddress` 把它归入 `is_private`，
        # 拿它当「公网样例」会得到一条假断言。用真公网地址。
        for h in ("github.com", "api.openai.com", "8.8.8.8", "1.1.1.1"):
            self.assertIsNone(outbound.blocked_reason(h, allow_private=False), h)
            self.assertIsNone(outbound.blocked_reason(h, allow_private=True), h)

    def test_empty_host(self):
        self.assertIsNotNone(outbound.blocked_reason(""))


class TestKnownBoundary(unittest.TestCase):
    def test_domain_that_resolves_to_metadata_is_not_caught(self):
        """**如实标注的边界**：本模块零网络、不解析 DNS ⇒ 这种写法拦不住。

        写成正向断言而不是留白：留着的话，日后有人会以为「已覆盖 SSRF」。
        真要覆盖得在**连接时刻** pin 解析结果（已在台账登记为后续项）。
        """
        self.assertIsNone(outbound.blocked_reason("evil.example.com", allow_private=False))


class TestCheckUrl(unittest.TestCase):
    def test_require_https(self):
        self.assertIn("https", outbound.check_url("http://github.com/x", require_https=True))
        self.assertIsNone(outbound.check_url("https://github.com/x", require_https=True))

    def test_rejects_credentials_and_bad_scheme_and_port(self):
        self.assertIn("用户名", outbound.check_url("https://u:p@github.com/x"))
        self.assertIn("http/https", outbound.check_url("file:///etc/passwd"))
        self.assertIn("端口", outbound.check_url("https://github.com:99999/x"))
        self.assertIsNotNone(outbound.check_url(""))

    def test_delegates_host_check(self):
        self.assertIsNotNone(outbound.check_url("http://169.254.169.254/latest/meta-data",
                                                allow_private=True))


class TestWiringGuards(unittest.TestCase):
    """一份判据两处用 —— 接线断了，判据就只剩摆设。"""

    def _src(self, name):
        return (_REPO / "src" / name).read_text(encoding="utf-8")

    def test_ghsettings_delegates_instead_of_keeping_its_own_table(self):
        s = self._src("ghsettings.py")
        self.assertIn("outbound.blocked_reason", s, "ghsettings 必须委托 outbound")
        self.assertNotIn('_BAD_NAME_SUFFIXES = (".local"', s,
                         "不得自留一份后缀表（两份必然漂移）")

    def test_mcpgw_checks_at_register_and_at_call(self):
        s = self._src("mcpgw.py")
        self.assertEqual(s.count("outbound.check_url"), 2,
                         "注册处与调用处都要拦（库里的旧行可能来自本项落地之前）")

    def test_mcpgw_documents_why_private_is_allowed(self):
        s = self._src("mcpgw.py")
        self.assertIn("HUB_MCP_ALLOW_PRIVATE", s)
        self.assertIn("127.0.0.1", s, "必须写清「MCP 常在 127.0.0.1」这个理由")

    def test_ghsettings_still_raises_its_own_error_type(self):
        import ghsettings
        with self.assertRaises(ghsettings.GhSettingsError):
            ghsettings._reject_private_host("127.0.0.1")
        with self.assertRaises(ghsettings.GhSettingsError):
            ghsettings._reject_private_host("")
        ghsettings._reject_private_host("github.com")   # 公网不抛


if __name__ == "__main__":
    unittest.main()