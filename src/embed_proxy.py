"""embed_proxy.py — 第三方 Web UI「外框统一」注入代理（用户 09-20 选定方案 b）。

为什么要有这个代理
------------------
QwenPaw 自带 56px 顶栏，而 hub 的嵌入外框已按 pi-web 实测统一成「行1 菜单 36px + 行2 地址 29px」。
跨源（:3102 载 :8088 的 iframe）使父页任何 CSS/JS 注入都被同源策略挡死，官方也没有
hideHeader / minimal / custom_css 之类的钩子（grep 为 0 命中），唯一可用的官方入口
QWENPAW_CONSOLE_STATIC_DIR 要改包内静态目录并重启 qwenpaw —— qwenpaw 是本机 Agent 会话宿主，
不能为个样式去重启它。于是把注入点挪到 hub 自己这一侧：在 hub 进程内起一个本地反代，
HTML 出栈前插一段 <style>，其余流量原样隧道透传。

为什么是「独立端口」而不是 hub 的路径前缀（/embed/qwenpaw/*）
-----------------------------------------------------------
qwenpaw 的前端资源与接口都是根绝对路径（/assets/…、/api/…）。挂路径前缀就得改写 HTML 里的
URL，还得改写在 JS bundle 字符串里拼出来的 /api 与 WebSocket 路径——那是不稳定的；而 hub 自己
已经占了 /api/*，同源命名空间直接打架。放在独立端口上，宿主应用仍然"住在自己的根"，
零改写、零冲突，WS/SSE 也只是普通字节流转发。

工程约束（改动前请复核）
------------------------
1. 只代理，不解释业务：非 HTML 请求（静态资源、JSON、SSE、WebSocket 升级）走字节隧道，
   一个字段都不动。
2. 注入选择器一律用 antd 布局类 + `[class*=...]`，不写 `__hash` 字面量：
   qwenpaw 升到 CSS Modules hash 变了也不会误伤，最坏是"注入不生效、顶栏仍 56px"（降级而非破坏）。
3. 高度是联动的：qwenpaw 的 sider 写死 `height:calc(100vh - 64px)`（窄屏 56px），
   只压顶栏不改 sider 会在侧栏底部留下 28px 死带 —— 两处必须成对改。
4. 上游要 `Accept-Encoding: identity`，否则拿到 br/gz 就没法在字节层面插 style。
   上游无视 identity 仍回压缩体时：**放弃注入、原样透传**（宁可外框不统一，也不破坏页面）。
"""
from __future__ import annotations

import asyncio
import logging
import re

log = logging.getLogger("agent-hub.embed-proxy")

# 实测基线（09-20，CDP 在 1440/390 两档量的 QwenPaw 2.2.0 console）：
#   header: h=56 pad=0 28px(窄 0 12px) bg=rgb(249,248,244)  子元素 logoWrapper h=64(本就溢出)、space h=32
#   sider : calc(100vh - 64px)，窄屏 calc(100vh - 56px)
# 目标：与 hub .ebar-menu 同高（桌面 36 / ≤768px 35）。
INJECT_CSS = """<style id="agent-hub-embed-unify">
/* 由 agent-hub :3103 注入：把宿主顶栏压到与 hub 外框行1 同高。见 src/embed_proxy.py 顶部说明。 */
.qwenpaw-layout-header[class*="__header__"], .qwenpaw-layout-header {
  height: 36px !important; min-height: 36px !important; padding: 0 10px !important; }
.qwenpaw-layout-header [class*="logoWrapper"], .qwenpaw-layout-header [class*="logoWrapper"] img,
.qwenpaw-layout-header img { max-height: 24px !important; width: auto !important; }
.qwenpaw-layout-header [class*="space"], .qwenpaw-layout-header [class*="headerActions"] { gap: 4px !important; }
/* sider 与顶栏高度联动：不改这条就会在侧栏底部留下死带 */
.qwenpaw-layout-sider, [class*="__sider__"] { height: calc(100vh - 36px) !important; }
@media (max-width: 768px) {
  /* 选得齐平：基础规则里 `.qwenpaw-layout-header[class*="__header__"]` 是 (0,2,0)，
     窄屏要覆盖它必须用同特异度，否则会被上面的 36px 顶回来（实测就是这样好一像素）。 */
  .qwenpaw-layout-header[class*="__header__"], .qwenpaw-layout-header {
    height: 35px !important; min-height: 35px !important; padding: 0 8px !important; }
  .qwenpaw-layout-sider, [class*="__sider__"] { height: calc(100vh - 35px) !important; }
}
</style>"""

_END = b"\r\n\r\n"
_HDR_SPLIT = re.compile(rb"\r?\n")
_HTML_PATH = re.compile(rb"\.html?$", re.I)
_ENC = re.compile(rb"^content-encoding:\s*(?P<v>[^\r\n]+)\r?$", re.I | re.M)
_STATUS = re.compile(rb"^HTTP/\d(?:\.\d)? (?P<code>\d{3})")


def _is_upgrade(header: bytes) -> bool:
    return b"upgrade:" in header.lower() or b"upgrade" in header.lower().split(b"\r\n", 1)[0]


def _is_document(head_line: bytes, header: bytes) -> bool:
    """SPA 文档请求（顶层导航）才注入。判据：GET + 无扩展名或 .html + Accept 里有 text/html。"""
    if not head_line.upper().startswith(b"GET "):
        return False
    if _is_upgrade(header):
        return False
    if b"text/html" not in header.lower():
        return False
    path = head_line.split(b" ")[1] if len(head_line.split(b" ")) > 1 else b"/"
    path = path.split(b"?")[0]
    if _HTML_PATH.search(path):
        return True                      # 明确要 .html
    last = path.rsplit(b"/", 1)[-1]
    return b"." not in last              # 无扩展名 = SPA 路由，服务端回的是 index.html → 要注入


def _inject(body: bytes) -> bytes:
    if b"<head" not in body.lower():
        return body
    low = body.lower()
    i = low.rfind(b"</head>")
    if i < 0:
        return body
    return body[:i] + INJECT_CSS.encode() + body[i:]


def _strip_hop_headers(header: bytes) -> bytes:
    keep = []
    for ln in _HDR_SPLIT.split(header):
        low = ln.lower()
        if not ln.strip():
            continue
        if low.startswith((b"accept-encoding:", b"connection:", b"keep-alive:", b"proxy-connection:",
                           b"transfer-encoding:", b"content-length:", b"host:",
                           # 条件请求头必须拒收：上游一旦回 304（无正文），代理无处插 style，
                           # 浏览器就会拿自己那份未注入的缓存渲染——实测窄屏顶栏打回 56px 的真凶。
                           b"if-none-match:", b"if-modified-since:", b"if-range:", b"if-unmodified-since:",
                           b"max-forwards:")):
            continue
        keep.append(ln)
    return b"\r\n".join(keep)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass
        writer.close()


async def _tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  pre: bytes, up_w: asyncio.StreamWriter, up_r: asyncio.StreamReader) -> None:
    """非 HTML：原样转发 + 双向泵。WS 升级、SSE、静态资源、/api JSON 都走这里。"""
    try:
        up_w.write(pre)
        await up_w.drain()
    except (ConnectionError, OSError):
        writer.close()
        return
    await asyncio.gather(_pipe(up_r, writer), _pipe(reader, up_w))
    up_w.close()


async def _document(writer: asyncio.StreamWriter, head_line: bytes, header: bytes,
                    up_w: asyncio.StreamWriter, up_r: asyncio.StreamReader,
                    pre_body: bytes, authority: bytes) -> None:
    """HTML：整份读回 → 插 <style> → 重算 Content-Length。上游强制 identity + close。

    Host 改写成上游 authority：_strip_hop_headers 会把客户端 Host 摘掉，不带 Host 的
    HTTP/1.1 请求有些服务端直接 400。实测 qwenpaw 不做跳转（SPA 兼底 200），
    所以改写 Host 不会把浏览器甩回 :8088。
    """
    req = (head_line + b"\r\n" + _strip_hop_headers(header) +
           b"\r\nHost: " + authority +
           b"\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n" + pre_body)
    up_w.write(req)
    await up_w.drain()
    chunks = []
    while True:
        d = await up_r.read(65536)
        if not d:
            break
        chunks.append(d)
    up_w.close()
    raw = b"".join(chunks)
    if _END not in raw:
        writer.close()
        return
    rh, rb = raw.split(_END, 1)
    m = _ENC.search(rh)
    if m and m.group("v").strip().lower() != b"identity":
        # 上游无视 identity：放弃注入，原样吐回（宁可不统一外框，也不破坏页面）
        log.warning("上游返回 %s 编码，跳过 style 注入（原样透传）", m.group("v").decode(errors="replace"))
        writer.write(raw)   # 注意：raw 已含结尾空行，别再补 _END，否则多一个 CRLF
    else:
        rb = _inject(rb)
        log.info("[embed] 文档注入: %s -> body %d bytes (style %d)" % (
            head_line.decode(errors="replace")[:38], len(rb), len(INJECT_CSS)))
        lines = [ln for ln in _HDR_SPLIT.split(rh)
                 if ln.strip()
                 and not _ENC.match(ln)
                 and not re.match(rb"^content-length:", ln, re.I)
                 and not re.match(rb"^transfer-encoding:", ln, re.I)
                 and not re.match(rb"^connection:", ln, re.I)
                 # 响应侧同理：不能把 ETag / Last-Modified 递回浏览器，否则它就拿着验证器来要 304。
                 # 正文已被我们改过（多一段 <style>），任何跟上游原文对的缓存校验都是错的。
                 and not re.match(rb"^(etag|last-modified|age):", ln, re.I)]
        # 注：只剥验证器，不动 status 行与其他头；no-store 由上游自己发的 cache-control 保证。
        writer.write(b"\r\n".join(lines) + b"\r\n"
                     + b"Cache-Control: no-store\r\n"
                     + b"Content-Length: " + str(len(rb)).encode()
                     + b"\r\nConnection: close\r\n\r\n" + rb)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    writer.close()


def _upstream_candidates(host: str) -> list[str]:
    """回环上游在 v4/v6 互为备胎：uvicorn/asyncio 绑 `::` 时强制 IPV6_V6ONLY=1，
    2026-09-21 qwenpaw 2.2.1 起 127.0.0.1 直连被 RST，需要回落 ::1。"""
    alt = {"127.0.0.1": "::1", "::1": "127.0.0.1"}.get(host)
    return [host, alt] if alt else [host]


def _authority(host: str, port: int) -> bytes:
    # IPv6 写进 Host 头必须加方括号，否则 "::1:8088" 是歧义地址
    return ("[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)).encode()


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  upstream: tuple[str, int]) -> None:
    try:
        header = await reader.readuntil(_END)
    except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
        writer.close()
        return
    parts = header.split(_END, 1)
    head_line = parts[0].split(b"\r\n", 1)[0]
    rest = parts[0][len(head_line) + 2:]
    # 若客户端在同一连接上已带正文（PUT/POST 的 body 紧跟头），已读到的部分要一起转发
    pre_body = parts[1] if len(parts) > 1 else b""
    host, port = upstream
    up_r = up_w = None
    last_err: OSError | None = None
    for try_host in _upstream_candidates(host):
        try:
            up_r, up_w = await asyncio.open_connection(try_host, port)
            host = try_host
            break
        except OSError as e:
            last_err = e
    if up_w is None:
        msg = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        try:
            writer.write(msg)
            await writer.drain()
        except Exception:
            pass
        log.debug("上游不可达 %s:%s (%s)", upstream[0], port, last_err)
        writer.close()
        return
    doc = _is_document(head_line, rest)   # 逐请求埋点已摘（会把 hub 日志刷满）
    if not doc and head_line.upper().startswith(b"GET ") and b"text/html" in rest.lower():
        log.info("[embed] GET+html Accept 但判为非文档，走隧道: %s" % head_line.decode(errors="replace")[:48])
    if doc:
        await _document(writer, head_line, rest, up_w, up_r, pre_body,
                        _authority(host, port))
    else:
        await _tunnel(reader, writer, header + pre_body, up_w, up_r)


class EmbedProxy:
    """一个实例 = 一个本地端口 = 一个上游。多宿主时再各起一个实例。"""

    def __init__(self, name: str, upstream_host: str, upstream_port: int,
                 listen_host: str = "0.0.0.0", listen_port: int = 3103):
        self.name = name
        self.upstream = (upstream_host, int(upstream_port))
        self.listen = (listen_host, int(listen_port))
        self.server: asyncio.base_events.Server | None = None

    async def start(self) -> bool:
        if self.server is not None:
            return True
        try:
            self.server = await asyncio.start_server(
                lambda r, w: _handle(r, w, self.upstream), *self.listen)
        except OSError as e:
            log.warning("[%s] 注入代理监听 %s:%s 失败：%s —— 嵌入视图将回落直连", self.name, *self.listen, e)
            return False
        log.info(f"[Agent Hub] {self.name} 外框注入代理：{self.listen[0]}:{self.listen[1]} → "
                 f"{self.upstream[0]}:{self.upstream[1]}")
        return True

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None
