"""dsh 工作区联动（v0.13.148）：hub 按 cwd 拉起 dsh 会话时，把 cwd 注册成
dsh 原生界面（web :3080）的工作区。

背景（2026-10-24 用户报障「点击本机项目 选取dsh 新建会话 工作目录没有正常转到
项目目录 其他agent都是正常的」，两层根因）：
  ① 嵌入式 CLI：`~/.local/bin/dsh` 启动器入口无条件 `cd "$DSH_DIR"`，把 term.py
     设好的进程 cwd 覆盖回源码目录（dshctl 仓已修，见判例 257）；
  ② 原生界面：dsh 的侧栏只列**已注册工作区**的会话，而 TUI 会话走
     `agents.create({meta:{cwd}})` 直通路径、不经过 session.create 的
     workspaceId 分支 ⇒ 从本机项目页按 cwd 拉起的会话在原生死界面里落在
     Ungrouped，项目目录侧栏无迹可寻。

本模块补②的联动：建会话前 best-effort 调 dsh web 的 HTTP RPC
（`POST /api/workspace/create`，envelope 形状见 packages/client/connection 的
rpc.ts）把 cwd 注册成工作区。该调用**幂等**（同路径返回既有工作区）。

安全与边界：
- cwd 由 term.py `_cwd_or_none` 先行校验（绝对路径 + 实盘存在 + 无可疑字符），
  这里只把它塞进 JSON body，不进任何命令拼装；
- web 地址（含 token）出自 dsh 自己的 journalctl 横幅（profiles._dsh_web_url，
  与卡片「新窗口」按钮同一个来源、同口径信任），token 只用于换 HttpOnly cookie；
- **best-effort 是硬契约**：dsh web 没跑 / 超时 / RPC 失败一律只记日志返回
  False，绝不让联动失败拖垮会话创建（原生界面都没开时注册本来也无意义）。
"""
from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
from http.cookiejar import CookieJar
from typing import Any, Dict, Optional, Tuple

log = logging.getLogger(__name__)

#: dsh web 的共享 RPC 通道端点（method 与 URL 末段同名）
_RPC_CREATE = "workspace/create"

#: 单次 HTTP 请求超时（秒）。换 cookie 与 RPC 各一次；合计最坏 ~6s，
#: 由调用方放 asyncio.to_thread，不占事件循环。
_TIMEOUT_S = 3.0


def _split_web_url(web_url: str) -> Optional[Tuple[str, str]]:
    """`http://host:port/[path]/?token=XXX` → (`http://host:port/[path]`, `XXX`)。

    形状不符（无 token / 非 http(s)）返回 None —— 取不到 token 时 RPC 必 401，
    不如明确跳过，让调用方记「未注册」而不是「注册失败」。
    """
    try:
        u = urllib.parse.urlparse(web_url)
    except ValueError:
        return None
    if u.scheme not in ("http", "https") or not u.netloc:
        return None
    q = urllib.parse.parse_qs(u.query)
    tok = (q.get("token") or [""])[0]
    if not tok:
        return None
    base = urllib.parse.urlunparse((u.scheme, u.netloc, u.path or "/", "", "", ""))
    return base.rstrip("/"), tok


def _post_rpc(base: str, token: str, method: str, args: Dict[str, Any]) -> Dict[str, Any]:
    """两步走：`?token=` 换 HttpOnly cookie → POST `/api/<method>`。

    dsh 的 API 鉴权只认签名 cookie（browser-auth.ts 的 authenticate 只读
    cookie，API 凭据走另一条要 socket 占位的路）⇒ 必须先访问一次索引页完成
    交换。返回 result.value；RPC 层失败（ok:false）抛 RuntimeError。
    """
    jar = CookieJar()
    opener = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    opener.open(f"{base}/?token={urllib.parse.quote(token, safe='')}", timeout=_TIMEOUT_S)
    body = json.dumps({
        "type": "client-request",
        "rpcId": str(uuid.uuid4()),
        "method": method,
        "payload": {"args": args},
    }).encode("utf-8")
    req = urllib.request.Request(
        f"{base}/api/{method}",
        data=body,
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    with opener.open(req, timeout=_TIMEOUT_S) as resp:
        envelope = json.loads(resp.read().decode("utf-8"))
    result = (envelope or {}).get("result") or {}
    if not result.get("ok"):
        err = result.get("error") or {}
        raise RuntimeError(f"dsh rpc {method}: {err.get('code') or '?'} {err.get('message') or ''}".strip())
    return result.get("value") or {}


def ensure_workspace(cwd: str, web_url: Optional[str] = None) -> bool:
    """把 `cwd` 注册成 dsh 工作区（幂等）。返回是否**确认**注册成功。

    web_url 缺省从 profiles._dsh_web_url() 取（journalctl 实时横幅，30s 缓存）。
    任何失败（地址取不到 / 连不上 / 超时 / RPC 报错）都只记日志返回 False。
    """
    if not cwd:
        return False
    if web_url is None:
        import profiles                       # 延迟导入：测试可注入 web_url 免 journalctl
        web_url = profiles._dsh_web_url()
    split = _split_web_url(web_url or "")
    if split is None:
        log.warning(f"[dshws] 未注册工作区（dsh web 地址/ token 不可用）：{cwd}")
        return False
    base, token = split
    t0 = time.monotonic()
    try:
        value = _post_rpc(base, token, _RPC_CREATE, {"request": {"path": cwd}})
    except (urllib.error.URLError, OSError, ValueError, RuntimeError) as e:
        # URLError 含 connection refused / timeout；ValueError 含 JSON 解析失败
        log.warning(f"[dshws] 工作区注册失败（不阻塞建会话）cwd={cwd}: {type(e).__name__}: {e}")
        return False
    ws = (value or {}).get("workspace") or {}
    log.info(f"[dshws] 工作区就绪 {ws.get('path') or cwd}（created={value.get('created')}，"
             f"{int((time.monotonic() - t0) * 1000)}ms）")
    return True
