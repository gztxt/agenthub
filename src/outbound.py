# -*- coding: utf-8 -*-
"""出站地址准入（P2 C5；手册 C5：「**出站默认拒绝**，封禁云元数据端点与内网保留段」）。

【为什么集中一处】此前只有 `ghsettings` 有一份「字面 IP / localhost 家族」拦截，
而 **mcpgw**（可注册任意外部 MCP server 并**替本机出站请求**）没有。
两份判据必然漂移，且这类漂移是**静默**的：漏一处就等于那条出站路径没有护栏。
故把**判定**抽到这里（纯函数），调用点只负责把理由转成自己的错误类型。

【口径 —— 三档，而不是一刀切】

| 档 | 判定 | 用在哪 |
|---|---|---|
| **元数据端点** | **一律拒**（永远） | 所有出站路径 |
| 内网/回环保留段 | 由 `allow_private` 决定 | 见下 |
| 公网 | 放行 | — |

**为什么内网不是一律拒**：MCP server **本来就常在 127.0.0.1**（本机 stdio/http 桥），
一刀切会把最正常的用法挡死 —— 那是把安全做成可用性事故。而 ghsettings 的「远程 API 基址」
指向内网没有任何正当用途，故那里 `allow_private=False`。

**两处如实标注的边界（不做成假绿）**：

1. **域名不做 DNS 解析**（保持零网络：解析会引入新的超时/失败面）。所以本模块挡的是
   「直白的内网写法」，**挡不住** `evil.example.com` 解析到 `169.254.169.254`。
   完整方案要在**连接时刻** pin 解析结果，属后续项。
2. 元数据端点名单是**枚举**（AWS/GCP/Azure 的 `169.254.169.254`、IPv6 `fd00:ec2::254`、
   阿里云 `100.100.100.200`），不是「凡是 link-local 都叫元数据」—— 枚举能给出人话理由。
"""

from __future__ import annotations

import ipaddress
from typing import Optional
from urllib.parse import urlsplit

#: 本机/内网主机名后缀（不做 DNS 也能拦下的那一类）。
#: ⚠ 与 `ghsettings._BAD_NAME_SUFFIXES` **逐字一致** —— 抽出来就是为了「一份判据两处用」，
#: 抽的时候顺手加项会让 ghsettings 的行为静默变宽/变窄（那是没写进任何提交说明的行为变更）。
BAD_NAME_SUFFIXES = (".local", ".localhost", ".internal", ".home.arpa")

#: 云元数据端点（SSRF 的首要目标）。**即便 allow_private=True 也拒。**
METADATA_ADDRS = ("169.254.169.254", "fd00:ec2::254", "100.100.100.200")
METADATA_NAMES = ("metadata.google.internal", "metadata", "instance-data")


def _norm_host(host: str) -> str:
    h = (host or "").strip().lower().rstrip(".")
    if h.startswith("[") and h.endswith("]"):       # [::1] 形态
        h = h[1:-1]
    return h


def blocked_reason(host: str, *, allow_private: bool = False) -> Optional[str]:
    """出站主机是否该拒。返回**人话理由**（None = 放行）。纯函数，零网络。

    `allow_private=True` 表示「本机/内网是合法去处」（MCP server 场景），
    但**元数据端点仍然拒**。
    """
    h = _norm_host(host)
    if not h:
        return "主机名不能为空"
    if h in METADATA_NAMES:
        return "拒绝云元数据端点：%s（SSRF 的首要目标，任何场景都不放行）" % h
    if h in BAD_NAME_SUFFIXES or any(h.endswith(s) for s in BAD_NAME_SUFFIXES) or h == "localhost":
        if allow_private:
            return None
        return "拒绝本机/内网主机名：%s" % h
    try:
        ip = ipaddress.ip_address(h)
    except ValueError:
        return None                                  # 普通域名：不做 DNS（见模块 docstring）
    if str(ip) in METADATA_ADDRS:
        return "拒绝云元数据端点：%s（SSRF 的首要目标，任何场景都不放行）" % h
    if (ip.is_loopback or ip.is_private or ip.is_link_local or ip.is_reserved
            or ip.is_multicast or ip.is_unspecified):
        if allow_private:
            return None          # 包括回环/私网/link-local：MCP 场景里本机是合法去处
        return ("拒绝内网/保留地址：%s（link-local 段含云元数据，整段不例外）" % h)
    return None


def check_url(url: str, *, allow_private: bool = False,
              require_https: bool = False) -> Optional[str]:
    """校验一个出站 URL。返回拒绝理由（None = 放行）。纯函数。"""
    s = (url or "").strip()
    if not s:
        return "地址不能为空"
    u = urlsplit(s)
    if require_https and u.scheme != "https":
        return "必须 https（明文 http 会把凭据裸发上链路）"
    if u.scheme not in ("http", "https"):
        return "只接受 http/https（得到 %r）" % (u.scheme or "")
    if u.username or u.password:
        return "地址里不能带用户名/密码"
    if not u.hostname:
        return "解析不出主机名"
    try:
        port = u.port          # 越界端口在这里抛 ValueError（urlsplit 不做范围校验）
    except ValueError:
        return "端口号非法"
    if port is not None and not (1 <= port <= 65535):
        return "端口号非法"
    return blocked_reason(u.hostname, allow_private=allow_private)