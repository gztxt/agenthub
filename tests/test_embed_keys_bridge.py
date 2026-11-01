#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""跨源嵌页 ⌘/Ctrl+K 桥（PT-20261101-09）的 L0 闸门。

L0 口径：不 import src.main、不起服务、不打网络。这里只验三件可静态判定的事：
1. 注入侧：代理往嵌页 HTML 里塞的那段 script 真的会插进 `</head>`，且带捕获阶段；
2. 地址侧：桥地址必须**保留 path/query/fragment** —— dsh 的 token 在 query、
   reasonix 的在 fragment，丢哪一段都是 401/白屏（判过一次，别再踩）；
3. 父页侧：message 收端必须校验 `e.source === embedFrame.contentWindow`，
   否则任意窗口都能替你开面板。

真键盘链路的验收在 `work/probe-bridge/probe_bridge.py`（红/绿各一次真鼠标+真按键），
这一层只保证「改坏契约时立刻红」，不替代那个。
"""
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import embed_proxy  # noqa: E402
import profiles  # noqa: E402

HUB = ROOT / "static" / "hub"
JS05 = (HUB / "05-chat-and-history.js").read_text(encoding="utf-8")
JS06 = (HUB / "06-manager-tasks.js").read_text(encoding="utf-8")
TM = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


class TestInjection(unittest.TestCase):
    def test_keys_script_shape(self):
        js = embed_proxy.KEYS_JS
        self.assertIn("postMessage", js)
        self.assertIn("'cmd-k'", js)
        self.assertIn("preventDefault", js)
        self.assertIn("stopPropagation", js)
        self.assertIn("isComposing", js, "中文输入法组字时必须放行，否则 k 被吃掉")
        self.assertIn("}, true)", js, "必须捕获阶段：冒泡阶段 xterm 已经把键送进 pty")

    def test_inject_into_head(self):
        body = b"<html><head><title>t</title></head><body>x</body></html>"
        out = embed_proxy._inject(body, b"<script>M</script>")
        self.assertIn(b"<script>M</script></head>", out)

    def test_inject_falls_back_to_body(self):
        body = b"<html><body>x</body></html>"
        out = embed_proxy._inject(body, b"<script>M</script>")
        self.assertIn(b"<script>M</script></body>", out)

    def test_inject_no_anchor_is_untouched(self):
        body = b"plain"
        self.assertEqual(embed_proxy._inject(body, b"<script>M</script>"), body)

    def test_empty_extra_is_noop(self):
        body = b"<html><head></head><body></body></html>"
        self.assertEqual(embed_proxy._inject(body, b""), body)

    def test_proxy_builds_extra(self):
        px = embed_proxy.EmbedProxy("t", "127.0.0.1", 1, listen_port=3299)
        self.assertIn(b"agent-hub-keys", px.extra)
        px2 = embed_proxy.EmbedProxy("t", "127.0.0.1", 1, listen_port=3299, keys=False)
        self.assertNotIn(b"agent-hub-keys", px2.extra)
        px3 = embed_proxy.EmbedProxy("t", "127.0.0.1", 1, listen_port=3299, css="<style>X</style>")
        self.assertIn(b"<style>X</style>", px3.extra)
        self.assertIn(b"agent-hub-keys", px3.extra, "CSS 与 JS 必须能同时注入")


class _FakeWriter:
    """最小 StreamWriter 桩：只收字节，够 _document 用。"""

    def __init__(self):
        self.buf = bytearray()
        self.closed = False

    def write(self, b):
        self.buf += b

    async def drain(self):
        pass

    def close(self):
        self.closed = True


class TestChunked(unittest.TestCase):
    """PT-20261101-10：上游 chunked 必须先解帧再注入。

    反例（2026-11-01 用户报障）：dsh 的 index.html 是 `929b\r\n<html>…\r\n0\r\n\r\n`，
    代理剥掉 transfer-encoding 后按原字节重算 Content-Length，浏览器就把块长当正文
    ⇒ 页面左上角 `929b`、左下角 `0`；直连 :3080 正常（只有注入路径改了响应头）。
    """

    def test_plain(self):
        self.assertEqual(embed_proxy._dechunk(b"3\r\nAAA\r\n0\r\n\r\n"), b"AAA")

    def test_ext_and_trailer(self):
        body = b"3;ext=1\r\nABC\r\n0\r\nX-Trailer: v\r\n\r\n"
        self.assertEqual(embed_proxy._dechunk(body), b"ABC")

    def test_dsh_real_shape(self):
        # 0x929b = 37531 ＝ dsh 实测 index.html 原始字节数
        html = b"<html>" + b"A" * (37531 - 6)
        framed = b"929b\r\n" + html + b"\r\n0\r\n\r\n"
        self.assertEqual(embed_proxy._dechunk(framed), html)

    def test_garbage_returns_none(self):
        self.assertIsNone(embed_proxy._dechunk(b"zz\r\n"))
        self.assertIsNone(embed_proxy._dechunk(b"5\r\nAB"))

    def test_document_strips_frames(self):
        import asyncio

        async def run():
            html = b"<html><head></head><body>x</body></html>"
            up_r = asyncio.StreamReader()
            up_r.feed_data(b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\n"
                           b"transfer-encoding: chunked\r\n\r\n"
                           + ("%x" % len(html)).encode() + b"\r\n" + html + b"\r\n"
                           b"0\r\n\r\n")
            up_r.feed_eof()
            w = _FakeWriter()
            await embed_proxy._document(
                w, b"GET / HTTP/1.1", b"\r\nAccept: text/html", _FakeWriter(), up_r,
                b"", b"127.0.0.1:3080", b"<script>K</script>")
            return bytes(w.buf)

        raw = asyncio.run(run())
        head, body = raw.split(b"\r\n\r\n", 1)
        self.assertNotIn(b"transfer-encoding", head.lower())
        self.assertIn(b"<script>K</script></head>", body)
        cl = int([l for l in head.split(b"\r\n") if l.lower().startswith(b"content-length:")][0].split(b":")[1])
        self.assertEqual(cl, len(body), "Content-Length 必须等于解码后正文字节数")
        self.assertTrue(body.endswith(b"</html>"), "末尾不得残留终止块 `0`")


    def test_host_must_stay_client_value(self):
        """两侧 Host 必须一致：dsh 用 Host 签发/校验 cookie 的 authority。

        反例（2026-11-01）：_document 把 Host 改成上游 192.168.5.102:3080，
        _tunnel（WS/API 走这条）原样透传 Host=127.0.0.1:3202 ⇒ authority 不符
        ⇒ 全部 /api/* 与 /api/remote.mux 401，前端无限重连，页面永远"加载中"。
        """
        import asyncio

        async def run():
            html = b"<html><head></head></html>"
            up_r = asyncio.StreamReader()
            up_r.feed_data(b"HTTP/1.1 200 OK\r\ncontent-type: text/html\r\n"
                           b"transfer-encoding: chunked\r\n\r\n"
                           + ("%x" % len(html)).encode() + b"\r\n" + html + b"\r\n0\r\n\r\n")
            up_r.feed_eof()
            up_w, w = _FakeWriter(), _FakeWriter()
            await embed_proxy._document(
                w, b"GET / HTTP/1.1", b"\r\nHost: 127.0.0.1:3202\r\nAccept: text/html",
                up_w, up_r, b"", b"192.168.5.102:3080", b"")
            return bytes(up_w.buf)

        seen = asyncio.run(run())
        self.assertIn(b"\r\nHost: 127.0.0.1:3202\r\n", seen,
                      "上游看到的 Host 必须等于客户端 Host，否则 cookie authority 与隧道侧不一致")


class TestUrlRewrite(unittest.TestCase):
    def setUp(self):
        self._old = dict(profiles.EMBED_PROXY_MAP)
        profiles.EMBED_PROXY_MAP["dsh"] = "http://127.0.0.1:3201"

    def tearDown(self):
        profiles.EMBED_PROXY_MAP.clear()
        profiles.EMBED_PROXY_MAP.update(self._old)

    def test_keeps_query(self):
        u = profiles._embed_proxy_url("dsh", "http://192.168.5.102:3080/?token=abc")
        self.assertEqual(u, "http://127.0.0.1:3201/?token=abc")

    def test_keeps_fragment(self):
        profiles.EMBED_PROXY_MAP["reasonix"] = "http://127.0.0.1:3209"
        u = profiles._embed_proxy_url("reasonix", "http://127.0.0.1:8787#token=xyz")
        self.assertEqual(u, "http://127.0.0.1:3209/#token=xyz")

    def test_keeps_path(self):
        u = profiles._embed_proxy_url("dsh", "http://127.0.0.1:3080/app/x?y=1")
        self.assertEqual(u, "http://127.0.0.1:3201/app/x?y=1")

    def test_unmapped_passthrough(self):
        u = "http://127.0.0.1:3010"
        self.assertEqual(profiles._embed_proxy_url("cloudcli", u), u)


class TestParentSide(unittest.TestCase):
    def test_message_listener_checks_source(self):
        self.assertIn("window.addEventListener('message'", JS05)
        seg = JS05.split("window.addEventListener('message'")[1]
        self.assertIn("e.source !== f.contentWindow", seg, "必须校验来源窗口")
        self.assertIn("d.hub !== 'cmd-k'", seg, "必须校验消息形状")

    def test_search_box_removed(self):
        self.assertNotIn('id="navSearch"', TM)
        self.assertNotIn("搜索名称 / id / 端口", TM)
        self.assertIn("Ctrl+k唤起搜索框支持所有功能检索", TM)

    def test_slash_opens_palette(self):
        seg = JS06.split("if (e.key === '/')")[1][:160]
        self.assertIn("openCmd()", seg, "搜索框已删，`/` 必须落到命令面板")
        self.assertNotIn("navSearch", seg)

    def test_palette_no_stale_action(self):
        self.assertNotIn("nav-search", JS05, "左栏搜索框已删，面板里不该再留这条动作")


if __name__ == "__main__":
    unittest.main()
