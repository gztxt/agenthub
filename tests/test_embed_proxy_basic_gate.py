"""桥内登录（Basic 面板）：未登录给表单，登录后由桥代补 Authorization。

背景（2026-11-02 用户报障「3208 面板没有登录框、直接报错」）：
proxy-panel(:8083) 回 401 + `WWW-Authenticate: Basic`，而浏览器**不在跨源 iframe 里弹**
认证框 ⇒ 嵌页只剩上游那句 `auth required`。桥因此自己当门：表单 → cookie → 代补 Basic。
"""
import asyncio
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import embed_proxy  # noqa: E402

SECRET = "test-secret-42"


async def _fake_upstream(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    """上游：带 Authorization 才 200，否则与 proxy-panel 同形地回 401。"""
    try:
        head = await reader.readuntil(b"\r\n\r\n")
    except (asyncio.IncompleteReadError, ConnectionError):
        writer.close()
        return
    ok = b"authorization: basic " in head.lower()
    body = b"upstream-ok" if ok else b"auth required"
    code = b"200 OK" if ok else b"401 Unauthorized"
    head_out = (b"HTTP/1.1 " + code + b"\r\nContent-Type: text/html; charset=utf-8"
                b"\r\nContent-Length: " + str(len(body)).encode() + b"\r\n\r\n")
    writer.write(head_out + body)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    writer.close()


async def _req(port: int, raw: bytes) -> bytes:
    r, w = await asyncio.open_connection("127.0.0.1", port)
    w.write(raw)
    await w.drain()
    data = await r.read()
    w.close()
    return data


class TestBasicGate(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self) -> None:
        self.tmp = tempfile.NamedTemporaryFile("w", suffix=".secret", delete=False)
        self.tmp.write(SECRET + "\n")
        self.tmp.close()
        self.up = await asyncio.start_server(_fake_upstream, "127.0.0.1", 0)
        up_port = self.up.sockets[0].getsockname()[1]
        # hub_auth=False：本类测的是**桥内表单兜底**（EMBED_HUB_AUTH=0 排障态）。
        # 默认 hub_auth=True 时门在 hub 会话上，未登录文档 302 回 hub 登录页，
        # 那条路径由 tests/test_embed_authority_rewrite.py::TestBridgeHubGate 覆盖。
        self.px = embed_proxy.EmbedProxy(
            "测试桥", "127.0.0.1", up_port, listen_host="127.0.0.1",
            listen_port=0, keys=True, auth_secret=self.tmp.name, title="Proxy Panel",
            hub_auth=False)
        self.assertTrue(await self.px.start())
        self.port = self.px.server.sockets[0].getsockname()[1]

    async def asyncTearDown(self) -> None:
        await self.px.stop()
        self.up.close()
        try:
            os.unlink(self.tmp.name)
        except OSError:
            pass

    async def test_login_page_when_anonymous(self) -> None:
        out = await _req(self.port, b"GET / HTTP/1.1\r\nHost: x\r\nAccept: text/html\r\n\r\n")
        self.assertIn(b"200 OK", out.split(b"\r\n", 1)[0])
        self.assertIn("需要登录".encode(), out)
        self.assertIn(b"__hub_login", out)

    async def test_api_blocked_when_anonymous(self) -> None:
        out = await _req(self.port, b"GET /api/status HTTP/1.1\r\nHost: x\r\n\r\n")
        self.assertIn(b"401", out.split(b"\r\n", 1)[0])
        self.assertNotIn(b"upstream-ok", out)

    async def test_wrong_password_rejected(self) -> None:
        body = b"p=nope"
        raw = (b"POST /__hub_login HTTP/1.1\r\nHost: x\r\nContent-Type: "
               b"application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        out = await _req(self.port, raw)
        self.assertIn(b"401", out.split(b"\r\n", 1)[0])
        self.assertNotIn(b"Set-Cookie", out)

    async def test_login_then_proxy_injects_auth(self) -> None:
        body = ("p=" + SECRET).encode()
        raw = (b"POST /__hub_login HTTP/1.1\r\nHost: x\r\nContent-Type: "
               b"application/x-www-form-urlencoded\r\nContent-Length: %d\r\n\r\n" % len(body)) + body
        out = await _req(self.port, raw)
        self.assertIn(b"303", out.split(b"\r\n", 1)[0])
        self.assertIn(b"Set-Cookie:", out)
        cookie = out.split(b"Set-Cookie: ", 1)[1].split(b";", 1)[0]
        got = await _req(self.port,
                         b"GET / HTTP/1.1\r\nHost: x\r\nAccept: text/html\r\nCookie: " + cookie + b"\r\n\r\n")
        self.assertIn(b"upstream-ok", got)


class TestHelpers(unittest.TestCase):
    def test_req_line(self) -> None:
        self.assertEqual(embed_proxy._req_line(b"GET /a/b?x=1 HTTP/1.1\r\nHost: y\r\n\r\n"),
                         (b"GET", b"/a/b"))

    def test_cookie_has(self) -> None:
        hdr = b"GET / HTTP/1.1\r\nCookie: a=1; hub_bridge_3208=tok\r\n\r\n"
        self.assertTrue(embed_proxy._cookie_has(hdr, "hub_bridge_3208", "tok"))
        self.assertFalse(embed_proxy._cookie_has(hdr, "hub_bridge_3208", "other"))


if __name__ == "__main__":
    unittest.main()
