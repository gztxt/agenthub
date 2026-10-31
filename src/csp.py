"""CSP / 安全响应头（v0.13.185）。

**为什么先上 Report-Only 而不是强制拦截**：本项目前端是「无构建 + 单文件 hub.js」，
模板里还有 inline `style=` 与 `onclick=`（主内容区合法用法，见 AGENTS.md 4.2）。
直接发强制 CSP 会在不改模板的前提下打断 xterm WebGL 与内联事件 ⇒ 用户侧表现为
「终端黑屏 / 按钮点了没反应」，而且没有任何服务端错误可归因。

Report-Only 的成本是零：浏览器不阻断，只在控制台报违规。观察一两个版本、
把违规项清完（或确认必须保留 `unsafe-inline`）之后再改强制。

策略口径：
- `connect-src 'self' ws: wss:` —— 终端 WebSocket 与同源轮询；不放开任意 https。
- `worker-src 'self' blob:` —— xterm 的 WebGL/搜索 addon 用 blob worker。
- `img-src 'self' data: blob:` —— SVG sprite 与终端截图预览。
- `frame-ancestors 'none'` —— 与 profiles.py 里「控制台自设 X-Frame-Options: DENY」同口径。
- `unsafe-inline` / `unsafe-eval` 暂时保留：模板 inline style、主内容区 inline onclick、
  以及 xterm 的 `new Function` 路径。**这是待偿债务，不是设计目标**。

配置：`HUB_CSP=off` 关掉；`HUB_CSP=enforce` 改强制（默认 report-only）。
"""

from __future__ import annotations

import os
from typing import Optional

from starlette.requests import Request
from starlette.responses import Response

#: 单行策略（浏览器对多行不敏感，但单行便于 grep 与日志取证）
POLICY = "; ".join([
    "default-src 'self'",
    "base-uri 'self'",
    "form-action 'self'",
    "frame-ancestors 'none'",
    "object-src 'none'",
    "script-src 'self' 'unsafe-inline' 'unsafe-eval'",
    "style-src 'self' 'unsafe-inline'",
    "img-src 'self' data: blob:",
    "font-src 'self' data:",
    "connect-src 'self' ws: wss:",
    "worker-src 'self' blob:",
    "manifest-src 'self'",
])

#: 一并补的「零风险」头（不依赖前端改造，浏览器立即生效）
HARDENING = {
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "same-origin",
    "X-Frame-Options": "DENY",
    # 不设 Permissions-Policy：语音/摄像头本仓未用，但设了会挡住用户未来的本地插件；
    # 要加时单独评审，别顺手塞进这里。
}


def mode() -> str:
    """off / report / enforce（默认 report）。拼错一律退回 report，不静默变成 enforce。"""
    v = (os.getenv("HUB_CSP") or "").strip().lower()
    return v if v in ("off", "report", "enforce") else "report"


def header_name(m: Optional[str] = None) -> str:
    return ("Content-Security-Policy" if (m or mode()) == "enforce"
            else "Content-Security-Policy-Report-Only")


def apply(resp: Response, m: Optional[str] = None) -> Response:
    """把安全头写进响应；`off` 时原样返回。"""
    m = m or mode()
    if m == "off":
        return resp
    for k, v in HARDENING.items():
        resp.headers.setdefault(k, v)
    resp.headers.setdefault(header_name(m), POLICY)
    return resp


async def middleware(request: Request, call_next):
    """ASGI 中间件形态（main.py 用 `app.middleware("http")(...)` 挂）。

    注意：SSE / 大文件流式响应也走这里 —— 只设 header，不读 body，不影响流。
    """
    resp = await call_next(request)
    return apply(resp)
