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
import base64
import hashlib
import hmac
import logging
import os
import re
import urllib.parse

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

# 跨源嵌页的 ⌘/Ctrl+K 桥（PT-20261101-09）
# ------------------------------------------------
# 为什么必须注入到嵌页里：iframe 是**跨源**（各占一个端口），帧内的 keydown 不会冒泡到
# 父页，父页写多少监听都收不到；用户实测「鼠标点过页面最顶部（父页外框）之后才能唤起」
# 正是这个原因。唯一出路是嵌页自己接键、postMessage 回父页。
# 逐家改 12 个宿主不现实（其中 6 个是第三方二进制），所以复用本代理的注入点：HTML 出栈前
# 插这段 script，宿主仍然"住在自己的根"，零 URL 改写。
# 父页收端见 static/hub/05-chat-and-history.js 的 message 监听（校验 e.source）。
KEYS_JS = """<script id="agent-hub-keys">
(function () {
  if (window.parent === window) return;            // 独立打开时不抢浏览器自己的 ⌘K
  document.addEventListener('keydown', function (e) {
    if (!((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'k' || e.key === 'K'))) return;
    if (e.isComposing || e.keyCode === 229) return; // 中文输入法组字中不抢
    e.preventDefault();
    e.stopPropagation();
    try { window.parent.postMessage({ hub: 'cmd-k' }, '*'); } catch (_) {}
  }, true);
})();
</script>"""

_END = b"\r\n\r\n"
_HDR_SPLIT = re.compile(rb"\r?\n")
_HTML_PATH = re.compile(rb"\.html?$", re.I)
_ENC = re.compile(rb"^content-encoding:\s*(?P<v>[^\r\n]+)\r?$", re.I | re.M)
_STATUS = re.compile(rb"^HTTP/\d(?:\.\d)? (?P<code>\d{3})")

# ── Basic 鉴权面板的「桥内登录」（2026-11-02，PT-20261102 报障）──────────
# 问题：proxy-panel(:8083) 回 `401 + WWW-Authenticate: Basic`，而浏览器**不在跨源 iframe
# 里弹**认证框（只有顶层导航才弹）⇒ 用户点「嵌入会话」只看到上游那句 `auth required`，
# 而浏览器直开 :8083 正常。这就是「3208 面板没有登录框、直接报错」的全部成因。
# 两种错解：① 删掉嵌入入口（用户侧=功能没了）；② 桥里直接补 Authorization（等于把
# 带鉴权的面板经桥变成 LAN 上免密可读，凭空扩攻击面）。所以桥自己做一层**等价的门**：
# 未登录先给本页表单 → POST 校验 → 种 HttpOnly cookie → 此后每个上游请求由服务端补
# Authorization。口令仍只有一份（画像里的 `bridge_secret` 指向的面板口令文件）。
LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
html,body{height:100%;margin:0;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
background:#12141a;color:#e6e8ee;display:flex;align-items:center;justify-content:center}
form{background:#1b1e26;border:1px solid #2c313d;border-radius:10px;padding:22px 24px;
width:min(320px,86vw);box-shadow:0 8px 28px rgba(0,0,0,.45)}
h1{font-size:15px;margin:0 0 14px;font-weight:600}
input{width:100%;box-sizing:border-box;padding:9px 10px;border-radius:7px;border:1px solid #333a48;
background:#0e1015;color:inherit;font-size:14px}
button{margin-top:12px;width:100%;padding:9px;border:0;border-radius:7px;background:#3b6df6;
color:#fff;font-size:14px;cursor:pointer}
button:hover{background:#4b7bff}
.e{color:#ff6b6b;margin:0 0 10px;font-size:13px}
.h{color:#8a91a3;font-size:12px;margin:10px 0 0}
</style></head><body><form method="post" action="/__hub_login">
<h1>__TITLE__</h1>__ERR__
<input type="password" name="p" placeholder="口令" autofocus autocomplete="current-password">
<button type="submit">登录</button>
<p class="h">口令取自该面板的本地口令文件；登录状态只记在本桥端口上。</p>
</form></body></html>"""


def _login_page(title: str, err: str = "") -> bytes:
    import html as _html
    msg = '<p class="e">%s</p>' % _html.escape(err) if err else ""
    return (LOGIN_HTML.replace("__TITLE__", _html.escape(title))
            .replace("__ERR__", msg)).encode()


def _req_line(header: bytes) -> tuple[bytes, bytes]:
    """(method, path) —— 只取请求行，路径去掉 query。"""
    parts = header.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2:
        return b"", b"/"
    return parts[0].upper(), parts[1].split(b"?", 1)[0]


def _cookie_has(header: bytes, name: str, token: str) -> bool:
    for ln in header.split(b"\r\n"):
        if not ln.lower().startswith(b"cookie:"):
            continue
        for part in ln[7:].split(b";"):
            k, _, v = part.strip().partition(b"=")
            if k.decode(errors="replace") == name and hmac.compare_digest(
                    v.decode(errors="replace"), token):
                return True
    return False



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


def _dechunk(body: bytes) -> bytes | None:
    """解 `Transfer-Encoding: chunked`。解析失败返回 None（调用方原样透传，不猜）。

    为什么必须有（2026-11-01 实测）：dsh 的 web 服务（Node）**所有响应都是 chunked**，
    而 _document 会把响应头的 transfer-encoding 剥掉、按 `len(rb)` 重算 Content-Length。
    不解码时 rb 仍是 `929b\\r\\n<html>…\\r\\n0\\r\\n\\r\\n`，浏览器把十六进制块长当正文首行、
    把终止块 `0` 当正文末行 ⇒ 页面左上角显示 `929b`、左下角显示 `0`（用户报障原话）。
    直连 :3080 正常、只有 hub 嵌页异常，正因为只有这条注入路径改了响应头。
    """
    out = []
    i, n = 0, len(body)
    while i < n:
        j = body.find(b"\r\n", i)
        if j < 0:
            return None
        line = body[i:j].split(b";", 1)[0].strip()
        try:
            size = int(line, 16)
        except ValueError:
            return None
        if size == 0:                      # 终止块；后面是 trailer，正文到此为止
            return b"".join(out)
        start = j + 2
        if start + size > n:
            return None
        out.append(body[start:start + size])
        i = start + size + 2               # 跳过块尾 CRLF
    return None


def _inject(body: bytes, extra: bytes) -> bytes:
    """把 extra（<style>/<script>）插到 </head> 之前；没有 <head> 就退回 </body> 之前。"""
    if not extra:
        return body
    low = body.lower()
    i = low.rfind(b"</head>")
    if i < 0:
        i = low.rfind(b"</body>")
    if i < 0:
        return body
    return body[:i] + extra + body[i:]


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
                    pre_body: bytes, authority: bytes, extra: bytes = b"") -> None:
    """HTML：整份读回 → 插 <style> → 重算 Content-Length。上游强制 identity + close。

    Host 必须用**客户端原值**，不能改写成上游 authority（2026-11-01 实测定案）：
    _strip_hop_headers 会把客户端 Host 摘掉，但 HTTP/1.1 不带 Host 有些服务端直接 400，
    所以要补回来——补的是原值。原因：dsh 用 Host 签发 auth cookie 的 `authority` 字段
    并在每次校验时比对当前 Host；HTML 走本函数（曾改写成 192.168.5.102:3080），
    而 WebSocket/SSE/静态资源走 `_tunnel`（原样透传，Host 仍是 127.0.0.1:3202）⇒
    两侧 authority 不一致 ⇒ `/api/remote.mux` 每次 401，前端无限重连，页面永远"加载中"。
    （netlog 实测同一页面 27 次 ws 重连，而 JS 总体积只有 pi 的 5.7 倍、传输仅 238ms，
    慢的不是下载。）两侧统一用客户端 Host 后与浏览器 origin 一致，cookie 才自洽。
    """
    mh = re.search(rb"^host:\s*(?P<v>[^\r\n]+)", header, re.I | re.M)
    host_hdr = mh.group("v").strip() if mh else authority
    req = (head_line + b"\r\n" + _strip_hop_headers(header) +
           b"\r\nHost: " + host_hdr +
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
    if re.search(rb"^transfer-encoding:\s*chunked", rh, re.I | re.M):
        # 剥头前先解帧：下面会把 transfer-encoding 摘掉并回 Content-Length，
        # 不解码就等于把 chunk 长度行当正文交给浏览器（见 _dechunk 注释）。
        dec = _dechunk(rb)
        if dec is None:
            log.warning("[embed] chunked 解码失败，原样透传（保留上游 transfer-encoding 头）")
            writer.write(raw)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        rb = dec
    m = _ENC.search(rh)
    if m and m.group("v").strip().lower() != b"identity":
        # 上游无视 identity：放弃注入，原样吐回（宁可不统一外框，也不破坏页面）
        log.warning("上游返回 %s 编码，跳过 style 注入（原样透传）", m.group("v").decode(errors="replace"))
        writer.write(raw)   # 注意：raw 已含结尾空行，别再补 _END，否则多一个 CRLF
    else:
        rb = _inject(rb, extra)
        log.info("[embed] 文档注入: %s -> body %d bytes (注入 %d)" % (
            head_line.decode(errors="replace")[:38], len(rb), len(extra)))
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
                  upstream: tuple[str, int], extra: bytes = b"",
                  pre_header: bytes | None = None, auth: bytes = b"") -> None:
    if pre_header is None:
        try:
            header = await reader.readuntil(_END)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
    else:
        header = pre_header
    parts = header.split(_END, 1)
    head_line = parts[0].split(b"\r\n", 1)[0]
    rest = parts[0][len(head_line) + 2:]
    # 桥内登录成功后由服务端补 Basic（客户端自带的 Authorization 不覆盖）：
    # 浏览器在跨源 iframe 里拿不到表单，也弹不出上游的认证框，只能由桥代持。
    if auth and not re.search(rb"^authorization:", rest, re.I | re.M):
        rest = rest + b"\r\nAuthorization: Basic " + auth
        header = head_line + b"\r\n" + rest + _END
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
                        _authority(host, port), extra)
    else:
        await _tunnel(reader, writer, header + pre_body, up_w, up_r)


def _lan_ip() -> str:
    """本机默认路由出口 IP（UDP connect 不发包，只让内核选路）。取不到返回空串。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


#: 回环/未声明地址的字面量（比较前统一 strip("[]").lower()）
_LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1"}
#: 起法里表达「监听地址」的旗标（opencode --hostname / zcode --host / reasonix --addr）
_BIND_FLAGS = ("--host", "--hostname", "--host-ip", "--addr", "--address",
               "--bind", "--listen")


def _start_binds_wide(start) -> bool:
    """起法里是否声明了「上游对局域网开放」。

    两条来源，按可信度排：
    ① `start["bind"]` —— 给 unit 式起法的显式声明。unit 的 ExecStart 在画像里读不到
       （pi-web 实际就是 `--hostname 0.0.0.0 --port 30141`），不声明就只能退回运行时
       探测；而按需拉起的 agent 在 hub 启动那一刻通常停着 ⇒ 探测必假死 ⇒ 桥永久锁
       回环（2026-11-02 pi 报障的同因，dsh 已于 v0.13.18x 改判）。
    ② `start["cmd"]` 里的监听旗标：`--host 0.0.0.0` / `--addr 0.0.0.0:8787` /
       `--host=192.168.5.102` 都算「上游自己就要对局域网开放」。
    """
    start = start or {}
    if _host_is_wide(str(start.get("bind") or "")):
        return True
    cmd = start.get("cmd") or []
    for i, a in enumerate(cmd):
        s = a if isinstance(a, str) else ""
        val = ""
        for flag in _BIND_FLAGS:
            if s == flag and i + 1 < len(cmd):
                val = str(cmd[i + 1])
            elif s.startswith(flag + "="):
                val = s.split("=", 1)[1]
        if val and _host_is_wide(val):
            return True
    return False


def _host_is_wide(val: str) -> bool:
    """`0.0.0.0` / `::` / `*` / 具体网卡（`192.168.x.x`、`host:port`）⇒ True。"""
    raw = (val or "").strip().lower()
    if raw.startswith("["):               # [::] / [::]:8080 / [::1]:8080
        raw = raw[1:].split("]", 1)[0]
    elif raw not in {"::"}:
        raw = raw.split(":", 1)[0]        # host:port 只取主机段
    if raw in {"0.0.0.0", "::", "*"}:
        return True
    return bool(raw) and raw not in _LOOPBACK_HOSTS


def listen_host_for(port: int, default: str = "0.0.0.0", *,
                    upstream_host: str = "", start=None) -> str:
    """桥监听地址 = 上游**会**有的可达面（不外扩暴露面）。

    2026-11-02 改判：旧实现只看「此刻从本机 LAN IP 连不连得上上游」，而按需拉起的
    agent 常态是停着的 —— hub 启动那一刻连不上就把桥**永久**锁在回环。实测后果：
    hub 于 2026-11-01 23:51 启动（dsh 已停），桥 :3202 绑 127.0.0.1，局域网访问
    `http://192.168.5.102:3202/` 恒 ERR_CONNECTION_REFUSED，而 `/ui-status` 判的是
    上游 3080（活着）⇒ 前端认为「就绪」、不挂覆盖层，用户看到的是一张裸的拒绝连接页。
    同批被绑死的还有 opencode/zcode/qoder/cloudcli（都是启动时停着的）。

    所以静态可达面优先、运行时探测兜底：
    ① 声明地址是具体网卡/通配（dsh 写死 192.168.5.102）⇒ 上游本来就是 LAN 可达；
    ② 起法里显式 `--host 0.0.0.0` 之类（opencode/zcode/reasonix）⇒ 同上；
    ③ 两者都没有（claude-mem / qoder 这类只绑回环的面板）⇒ 才连一次实测，
       连不上就保持 127.0.0.1，绝不把无鉴权面板经桥抬到局域网。
    探测拿不到 LAN IP 一律按回环处理：宁可 LAN 用不了，也不静默扩面。
    """
    host = (upstream_host or "").strip().strip("[]").lower()
    if host in {"0.0.0.0", "::", "*"}:
        return default
    if host not in _LOOPBACK_HOSTS:
        return default
    if _start_binds_wide(start):
        return default
    return default if _upstream_lan_reachable(port) else "127.0.0.1"


def _upstream_lan_reachable(port: int) -> bool:
    """此刻从本机 LAN IP 能不能连上上游端口（只给 listen_host_for 的兜底分支用）。"""
    ip = _lan_ip()
    if not ip:
        return False
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class EmbedProxy:
    """一个实例 = 一个本地端口 = 一个上游。多宿主时各起一个实例。

    `css` 只为外框统一（qwenpaw 顶栏）；`keys=True` 给每个嵌页注入 ⌘/Ctrl+K 桥。
    两个注入项都走同一条字节级插入路径，互不依赖：CSS 空而 KEYS 在也照常工作。
    """

    def __init__(self, name: str, upstream_host: str, upstream_port: int,
                 listen_host: str = "0.0.0.0", listen_port: int = 3103,
                 css: str = "", keys: bool = True,
                 auth_secret: str = "", title: str = ""):
        self.name = name
        self.upstream = (upstream_host, int(upstream_port))
        self.listen = (listen_host, int(listen_port))
        self.server: asyncio.base_events.Server | None = None
        self.extra = (css.encode() if css else b"") + (KEYS_JS.encode() if keys else b"")
        # 非空 ⇒ 这个桥是「带门的桥」：未登录先给表单，登录后服务端代补 Basic。
        self.auth_secret = (auth_secret or "").strip()
        self.title = title or name
        self._secret_cache: tuple[float | None, str] = (None, "")

    # ── 口令读取（按 mtime 缓存：面板轮换口令后无需重启 hub）──
    def _secret(self) -> str:
        if not self.auth_secret:
            return ""
        try:
            mtime = os.stat(self.auth_secret).st_mtime
        except OSError:
            return ""
        if self._secret_cache[0] != mtime:
            try:
                with open(self.auth_secret, encoding="utf-8") as fh:
                    self._secret_cache = (mtime, fh.read().strip())
            except OSError:
                return ""
        return self._secret_cache[1]

    def _cookie_token(self) -> str:
        s = self._secret()
        return hashlib.sha256(s.encode()).hexdigest()[:32] if s else ""

    def _cookie_name(self) -> str:
        return "hub_bridge_%d" % self.listen[1]

    def _auth_header(self) -> bytes:
        s = self._secret()
        return base64.b64encode(("hub:" + s).encode()) if s else b""

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        """无口令桥：行为与改前完全一致；有口令桥：先过门，再带 Auth 透传。"""
        if not self.auth_secret:
            await _handle(reader, writer, self.upstream, self.extra)
            return
        try:
            header = await reader.readuntil(_END)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
        method, path = _req_line(header)
        token = self._cookie_token()
        if method == b"POST" and path == b"/__hub_login":
            await self._login(reader, writer, header, token)
            return
        if token and _cookie_has(header, self._cookie_name(), token):
            await _handle(reader, writer, self.upstream, self.extra,
                          pre_header=header, auth=self._auth_header())
            return
        # 未登录：文档请求给表单；其余（XHR/静态资源）一律 401，别把半截页面喂给前端。
        # 文档判据与 _is_document 同向（GET + .html 或 Accept 含 text/html），另加「无扩展名
        # 路径」——iframe 顶层导航不保证带 Accept，别让用户看到裸的 login required。
        doc_like = (method == b"GET" and (b"text/html" in header.lower()
                                          or path == b"/" or bool(_HTML_PATH.search(path))))
        if doc_like:
            await self._send(writer, 200, _login_page(self.title + " 需要登录"))
        else:
            await self._send(writer, 401, b"login required")

    async def _login(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     header: bytes, token: str) -> None:
        m = re.search(rb"^content-length:\s*(\d+)", header, re.I | re.M)
        n = min(int(m.group(1)), 8192) if m else 0
        body = b""
        if n:
            try:
                body = await reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                body = b""
        got = (urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("p") or [""])[0]
        want = self._secret()
        if want and got and hmac.compare_digest(got.encode(), want.encode()):
            head = ("HTTP/1.1 303 See Other\r\nLocation: /\r\n"
                    "Set-Cookie: %s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                    % (self._cookie_name(), token)).encode()
            await self._write(writer, head)
            return
        await self._send(writer, 401, _login_page(self.title + " 需要登录", "口令不正确"))

    async def _send(self, writer: asyncio.StreamWriter, code: int, body: bytes) -> None:
        head = ("HTTP/1.1 %d %s\r\nContent-Type: text/html; charset=utf-8\r\n"
                "Content-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                % (code, "OK" if code == 200 else "Unauthorized", len(body))).encode()
        await self._write(writer, head + body)

    async def _write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        try:
            writer.write(data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        try:
            writer.close()
        except Exception:
            pass

    async def start(self) -> bool:
        if self.server is not None:
            return True
        try:
            self.server = await asyncio.start_server(self._serve, *self.listen)
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
