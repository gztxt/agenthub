#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嵌页桥的 Host/Origin 改写（2026-11-04「dsh 原生界面一直连接中」）。

L0 口径：这里只起**本地回环**的假上游，不打外网、不 import src.main。

验的是三件会在生产上直接变成白屏/死循环的契约：
1. `rewrite_authority` 打开时，文档与 WebSocket(B) 两条路径都必须把 Host 换成
   上游 authority —— 只改一条就是 11-01 那次的 401 自洽性缺口；
2. Origin 必须摘掉：dsh 的围栏拿 Origin 比对 Host，改写后带着客户端 authority
   过去必 403（2026-11-04 实测：Oray 的 `172.16.3.227:3202` 不在 trusted 名单，
   文档 303 正常、`/api/remote.mux` 403 ⇒ 前端无限重连）；
3. 没开的宿主（qwenpaw/pi-web/cloudcli…）行为必须一字不变 —— 它们用 Host 拼
   绝对跳转地址，跟着改写会把浏览器送到上游内网地址。
"""
import asyncio
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import embed_proxy  # noqa: E402
import profiles  # noqa: E402

CLIENT_HOST = "172.16.3.227:3202"          # Oray VPN 下 hub 的访问 authority
CLIENT_ORIGIN = "http://172.16.3.227:3202"


def _ws_upgrade(host: str, origin: str) -> bytes:
    return (
        "GET /api/remote.mux HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        "Upgrade: websocket\r\nConnection: Upgrade\r\n"
        "Sec-WebSocket-Key: dGhlIHNhbXBsZSBub25jZQ==\r\nSec-WebSocket-Version: 13\r\n"
        f"Origin: {origin}\r\n\r\n"
    ).encode()


def _doc_get(host: str, origin: str) -> bytes:
    return (
        "GET / HTTP/1.1\r\n"
        f"Host: {host}\r\n"
        f"Origin: {origin}\r\n"
        "Sec-Fetch-Site: same-origin\r\n"
        "Accept: text/html\r\n\r\n"
    ).encode()


class TestRewriteHelper(unittest.TestCase):
    def test_replaces_host_and_drops_browser_markers(self):
        raw = _doc_get(CLIENT_HOST, CLIENT_ORIGIN)
        out = embed_proxy._rewrite_authority(raw, b"192.168.5.102:3080")
        self.assertIn(b"Host: 192.168.5.102:3080\r\n", out)
        self.assertNotIn(CLIENT_HOST.encode(), out)
        self.assertNotIn(b"Origin:", out)
        self.assertNotIn(b"Sec-Fetch-Site:", out)
        # 其他头必须原样保留，否则 dsh 的 cookie / 条件请求全废
        self.assertIn(b"Accept: text/html", out)
        self.assertTrue(out.endswith(b"\r\n\r\n"), "结尾空行不能被吃掉")

    def test_keeps_body_after_header_block(self):
        raw = b"POST /api/x HTTP/1.1\r\nHost: a:1\r\n\r\npayload"
        out = embed_proxy._rewrite_authority(raw, b"b:2")
        self.assertTrue(out.endswith(b"\r\n\r\npayload"))
        self.assertIn(b"Host: b:2", out)


class _Recorder:
    """假上游：记下第一份请求头，按请求形态回最小合法响应。"""

    def __init__(self):
        self.seen = []

    async def __call__(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        self.seen.append(head)
        if b"upgrade: websocket" in head.lower():
            writer.write(b"HTTP/1.1 101 Switching Protocols\r\n"
                         b"Upgrade: websocket\r\nConnection: Upgrade\r\n\r\n")
        else:
            body = b"<html><head></head><body>ok</body></html>"
            writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: text/html; charset=utf-8\r\n"
                         b"Content-Length: " + str(len(body)).encode()
                         + b"\r\nConnection: close\r\n\r\n" + body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()


class _KeepAliveRecorder:
    """假上游：同一条 socket 上连续收多个请求，逐个记头并回 keep-alive 响应。

    真浏览器就是这么干活的（连接复用），旧实现只改首个请求 ⇒ 第 2 个起 403。
    """

    def __init__(self):
        self.seen = []

    async def __call__(self, reader, writer):
        while True:
            try:
                head = await reader.readuntil(b"\r\n\r\n")
            except (asyncio.IncompleteReadError, ConnectionError):
                break
            self.seen.append(head)
            if b"transfer-encoding: chunked" in head.lower():
                while True:                       # 吃掉正文，别让分块被当成下一个请求
                    line = await reader.readuntil(b"\r\n")
                    size = int(line.split(b";")[0], 16)
                    await reader.readexactly(size + 2)
                    if size == 0:
                        break
            else:
                m = re.search(rb"^content-length:\s*(\d+)", head, re.I | re.M)
                n = int(m.group(1)) if m else 0
                if n:
                    await reader.readexactly(n)
            try:
                writer.write(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok")
                await writer.drain()
            except (ConnectionError, OSError):
                break
        writer.close()


class TestBridgeEndToEnd(unittest.IsolatedAsyncioTestCase):
    async def _boot(self, rewrite: bool, handler=None):
        self.up = handler or _Recorder()
        self.up_srv = await asyncio.start_server(self.up, "127.0.0.1", 0)
        up_port = self.up_srv.sockets[0].getsockname()[1]
        self.px = embed_proxy.EmbedProxy(
            "测试桥", "127.0.0.1", up_port, listen_host="127.0.0.1",
            listen_port=0, keys=False, rewrite_authority=rewrite)
        self.assertTrue(await self.px.start())
        self.port = self.px.server.sockets[0].getsockname()[1]
        return f"127.0.0.1:{up_port}"

    async def _teardown(self):
        await self.px.stop()
        self.up_srv.close()

    async def test_document_request_rewritten(self):
        want = await self._boot(True)
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(_doc_get(CLIENT_HOST, CLIENT_ORIGIN))
        await w.drain()
        data = await asyncio.wait_for(r.read(65536), 3)
        w.close()
        await self._teardown()
        self.assertIn(b"ok", data)
        seen = self.up.seen[0]
        self.assertIn(b"Host: " + want.encode(), seen)
        self.assertNotIn(b"Origin:", seen)
        self.assertNotIn(CLIENT_HOST.encode(), seen)

    async def test_websocket_upgrade_rewritten(self):
        want = await self._boot(True)
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(_ws_upgrade(CLIENT_HOST, CLIENT_ORIGIN))
        await w.drain()
        data = await asyncio.wait_for(r.read(200), 3)
        w.close()
        await self._teardown()
        self.assertIn(b"101", data)
        seen = self.up.seen[0]
        self.assertIn(b"Host: " + want.encode(), seen)
        self.assertNotIn(b"Origin:", seen)
        self.assertIn(b"Sec-WebSocket-Key:", seen, "握手头本身必须保留")

    async def test_keepalive_second_request_rewritten(self):
        """同一条连接上的第 2 个请求也必须改写（2026-11-04 生产实测 req1=200/req2=403）。"""
        want = await self._boot(True, handler=_KeepAliveRecorder())
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        req = (b"POST /api/settings/describe HTTP/1.1\r\n"
               b"Host: " + CLIENT_HOST.encode() + b"\r\n"
               b"Content-Type: application/json\r\nContent-Length: 2\r\n\r\n{}")
        chunked = (b"POST /api/session/list HTTP/1.1\r\n"
                   b"Host: " + CLIENT_HOST.encode() + b"\r\n"
                   b"Transfer-Encoding: chunked\r\n\r\n"
                   b"4\r\n{\"a\"\r\n0\r\n\r\n")
        w.write(req + req + chunked)
        await w.drain()
        got = b""
        while got.count(b"ok") < 3:
            try:
                got += await asyncio.wait_for(r.read(4096), 3)
            except asyncio.TimeoutError:
                break
        w.close()
        await self._teardown()
        self.assertEqual(got.count(b"ok"), 3, "三个请求都要拿到上游响应")
        self.assertEqual(len(self.up.seen), 3)
        for i, seen in enumerate(self.up.seen):
            self.assertIn(b"Host: " + want.encode(), seen, f"第 {i+1} 个请求没被改写")
            self.assertNotIn(CLIENT_HOST.encode(), seen, f"第 {i+1} 个请求仍带客户端 Host")

    async def test_other_hosts_keep_client_host(self):
        await self._boot(False)
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(_doc_get(CLIENT_HOST, CLIENT_ORIGIN))
        await w.drain()
        await asyncio.wait_for(r.read(65536), 3)
        w.close()
        await self._teardown()
        seen = self.up.seen[0]
        self.assertIn(b"Host: " + CLIENT_HOST.encode(), seen, "未开 rewrite 的宿主必须保持原样")
        self.assertIn(b"Origin:", seen)


class TestTargets(unittest.TestCase):
    def test_only_dsh_asks_for_rewrite(self):
        flags = {t["id"]: t.get("rewrite_host") for t in profiles.embed_proxy_targets()}
        self.assertTrue(flags.get("dsh"), "dsh 的 /api 围栏依赖 bind 地址，必须改写")
        others = [k for k, v in flags.items() if k != "dsh" and v]
        self.assertEqual(others, [], "别家按 Host 拼绝对地址，不能被默认改写")


if __name__ == "__main__":
    unittest.main()
