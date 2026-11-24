#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""嵌页桥的 CSS @layer 兼容（2026-11-21「pi web 会话内容划不动」）。

L0 口径：只起本地回环假上游，不打外网、不 import src.main。

验的契约：
1. @layer 外壳拆除后，规则一条不少、顺序不变（老引擎丢的就是这一层）；
2. 字符串 / 注释里的花括号不能把拆层拆坏；
3. 只有 Chromium <99 的文档才会被加上 hubcss=1（新浏览器字节零改动）；
4. 兜底：上游回压缩体 / 非 text/css / 没有 @layer ⇒ 原样透传。
"""
import asyncio
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import embed_proxy  # noqa: E402

OLD_UA = (b"Mozilla/5.0 (Linux; Android 13) AppleWebKit/537.36 (KHTML, like Gecko) "
          b"Chrome/96.0.4664.104 Mobile Safari/537.36 PiWeb/3.1")
NEW_UA = (b"Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) "
          b"Chrome/126.0.0.0 Safari/537.36")

CSS = b"""@layer properties{@supports (x:y){*{--a:1}}}
@layer theme{:root{--bg:#fff}}
@layer base;@layer utilities{.flex{display:flex}.h-full{height:100%}.min-h-0{min-height:0}
.overflow-y-auto{overflow-y:auto}.content-\"x\"{color:red}}
.chat-content{color:var(--text)}"""


class TestFlattenLayers(unittest.TestCase):
    def test_unwraps_and_keeps_order(self):
        out = embed_proxy._flatten_layers(CSS)
        self.assertNotIn(b"@layer", out.lower())
        # 规则与顺序都在
        order = [out.index(t) for t in (b"--a:1", b"--bg:#fff", b".flex{", b".overflow-y-auto{",
                                        b".chat-content{")]
        self.assertEqual(order, sorted(order), "拆层后顺序必须保持源码顺序")
        self.assertIn(b".overflow-y-auto{overflow-y:auto}", out)

    def test_declaration_form_removed(self):
        out = embed_proxy._flatten_layers(b"@layer base;body{margin:0}")
        self.assertEqual(out, b"body{margin:0}")

    def test_braces_in_strings_and_comments_survive(self):
        css = b'@layer utilities{a:before{content:"}"}/* } */a{color:red}'
        out = embed_proxy._flatten_layers(css)
        self.assertIn(b'content:"}"', out)
        self.assertIn(b"/* } */", out)
        self.assertIn(b"a{color:red}", out)

    def test_without_layer_is_identity(self):
        css = b".a{color:red}"
        self.assertIs(embed_proxy._flatten_layers(css), css)

    def test_nested_layers(self):
        out = embed_proxy._flatten_layers(b"@layer a{@layer b{x{y:z}}}")
        self.assertEqual(out, b"x{y:z}")


class TestClientGate(unittest.TestCase):
    def test_old_chromium_needs_flatten(self):
        self.assertTrue(embed_proxy._needs_layer_flatten(OLD_UA))

    def test_new_chromium_untouched(self):
        self.assertFalse(embed_proxy._needs_layer_flatten(NEW_UA))

    def test_unknown_engine_untouched(self):
        self.assertFalse(embed_proxy._needs_layer_flatten(b"User-Agent: Mozilla/5.0 Gecko/20100101"))

    def test_bust_only_next_css_links(self):
        body = (b'<link rel="stylesheet" href="/_next/static/css/abc.css" data-precedence="next">'
                b'<link rel="stylesheet" href="/other/app.css">')
        out = embed_proxy._bust_css_urls(body)
        self.assertIn(b'href="/_next/static/css/abc.css?hubcss=1"', out)
        self.assertIn(b'href="/other/app.css"', out)

    def test_compat_stylesheet_marker(self):
        head = b"GET /_next/static/css/abc.css?hubcss=1 HTTP/1.1\r\nHost: h\r\n\r\n"
        self.assertTrue(embed_proxy._is_compat_stylesheet(head.split(b"\r\n")[0], head))
        plain = b"GET /_next/static/css/abc.css HTTP/1.1\r\nHost: h\r\n\r\n"
        self.assertFalse(embed_proxy._is_compat_stylesheet(plain.split(b"\r\n")[0], plain))


class _CssUpstream:
    def __init__(self, body: bytes, ctype: bytes = b"text/css; charset=utf-8"):
        self.body = body
        self.ctype = ctype
        self.seen = []

    async def __call__(self, reader, writer):
        try:
            head = await reader.readuntil(b"\r\n\r\n")
        except (asyncio.IncompleteReadError, ConnectionError):
            writer.close()
            return
        self.seen.append(head)
        writer.write(b"HTTP/1.1 200 OK\r\nContent-Type: " + self.ctype
                     + b"\r\nCache-Control: public, max-age=31536000, immutable"
                     + b"\r\nETag: W/\"deadbeef\""
                     + b"\r\nContent-Length: " + str(len(self.body)).encode()
                     + b"\r\nConnection: close\r\n\r\n" + self.body)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()


class TestStylesheetBridge(unittest.IsolatedAsyncioTestCase):
    async def _boot(self, handler):
        self.up = handler
        self.up_srv = await asyncio.start_server(handler, "127.0.0.1", 0)
        up_port = self.up_srv.sockets[0].getsockname()[1]
        self.px = embed_proxy.EmbedProxy("测试桥", "127.0.0.1", up_port,
                                         listen_host="127.0.0.1", listen_port=0,
                                         keys=False, hub_auth=False)
        self.assertTrue(await self.px.start())
        self.port = self.px.server.sockets[0].getsockname()[1]

    async def _teardown(self):
        await self.px.stop()
        self.up_srv.close()

    async def _get(self, target: bytes) -> bytes:
        r, w = await asyncio.open_connection("127.0.0.1", self.port)
        w.write(b"GET " + target + b" HTTP/1.1\r\nHost: h\r\n"
                b"Accept: text/css,*/*;q=0.1\r\nAccept-Encoding: gzip, deflate, br\r\n\r\n")
        await w.drain()
        data = b""
        while True:
            try:
                d = await asyncio.wait_for(r.read(65536), 3)
            except asyncio.TimeoutError:
                break
            if not d:
                break
            data += d
        w.close()
        return data

    async def test_marked_css_flattened(self):
        await self._boot(_CssUpstream(CSS))
        data = await self._get(b"/_next/static/css/abc.css?hubcss=1")
        await self._teardown()
        self.assertNotIn(b"@layer", data.lower())
        self.assertIn(b".overflow-y-auto{overflow-y:auto}", data)
        self.assertNotIn(b"ETag", data, "正文改过就必须剥掉验证器")
        # 上游必须收到 identity，否则拿不到明文就拆不了层
        self.assertIn(b"Accept-Encoding: identity", self.up.seen[0])

    async def test_unmarked_css_byte_identical(self):
        await self._boot(_CssUpstream(CSS))
        data = await self._get(b"/_next/static/css/abc.css")
        await self._teardown()
        self.assertIn(b"@layer utilities", data)
        self.assertIn(b'ETag: W/"deadbeef"', data)

    async def test_non_css_content_type_passthrough(self):
        await self._boot(_CssUpstream(CSS, ctype=b"application/javascript"))
        data = await self._get(b"/_next/static/css/abc.css?hubcss=1")
        await self._teardown()
        self.assertIn(b"@layer utilities", data)

    async def test_css_without_layer_passthrough(self):
        plain = b".a{color:red}"
        await self._boot(_CssUpstream(plain))
        data = await self._get(b"/_next/static/css/abc.css?hubcss=1")
        await self._teardown()
        self.assertIn(plain, data)
        self.assertNotIn(b"no-store", data)


class TestViewportFallback(unittest.TestCase):
    """2026-11-21 第二层病根：没有 dvh 的引擎整页 height 退化成 auto。

    pi-web 的 --app-viewport-height 只在 display-mode:standalone 里定义，其余
    一律 var(--app-viewport-height, 100dvh)；WebView < Chrome 108 没有 dvh ⇒
    height:auto ⇒ 聊天滚动区高度=内容高度（实测 10847px），永远划不动。
    """

    def test_extra_carries_dvh_fallback(self):
        px = embed_proxy.EmbedProxy("t", "127.0.0.1", 1, keys=False, hub_auth=False)
        self.assertIn(b"agent-hub-vh", px.extra)
        self.assertIn(b"@supports not (height: 100dvh)", px.extra)
        self.assertIn(b"--app-viewport-height: 100vh", px.extra)

    def test_fallback_is_scoped(self):
        css = embed_proxy.VIEWPORT_CSS
        self.assertTrue(css.startswith("<style"))
        self.assertIn("html, body { height: 100vh; }", css)
        self.assertNotIn("!important", css, "不能压过页面自己写的内联高度（键盘弹出时靠它）")


if __name__ == "__main__":
    unittest.main()
