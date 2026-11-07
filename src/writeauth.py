"""写端点鉴权闸门（v0.13.6 / P1-7 扩展）。

为什么做成中间件而不是往 34 个 handler 里各插一行：
`src` 下写类路由实测 34 条，散落脚本改法必然漏，而**漏一条就等于没做**——
攻击者只会去打漏的那条。集中一处 + 一条"路由清单必须被覆盖"的静态护栏
（tests/test_writeauth.py），才是可审计的形态。

红基线（09-23 实测，生产 :3102，匿名空 body）：
    状态码分布 {422:16, 404:8, 400:2, 200:6, 401:2}  ⇒ 34 条里只有 2 条拒了
    其中 6 条对匿名写返回 **200**：PUT /api/memory/l2、PUT /api/memory/l3、
    POST /api/memory/l2/rebuild、POST /api/scan/run、POST /api/vitals/sweep、
    POST /api/tasks/{id}/retry —— 这不是理论风险：本次取证中 rebuild 真的重写了
    用户手写的 L2 记忆（已按 09-06 在册副本逐字回滚，len 338 复核一致）。

豁免名单是这里唯一需要逐条给理由的东西 —— 一条"顺手加的白名单"就是永久的洞。
"""
import hmac
import os
from typing import Dict, Iterable, Optional, Tuple

from starlette.responses import JSONResponse

import approvals

WRITE_METHODS: Tuple[str, ...] = ("POST", "PUT", "PATCH", "DELETE")
#: 2026-11-07 用户裁定：**共享 token 兼容面已删除**。请求凭据只有两种：
#: ① 浏览器 —— HttpOnly 会话 Cookie `hub_session`（/api/users/login 种下）；
#: ② 机器 —— HTTP Basic（同一份用户名+密码）。
#: `x-hub-token` / `x-term-token` / `?token=` 一律不再接受（任何残留调用点都是 bug，
#: 不是"兼容期"）。2026-11-07 C 档：设置写端点已收口到同一登录闸门，
#: HUB_PASSCODE / TERM_TOKEN 两套共享口令全部废弃，全站单一认证面。

#: 不经过本闸门的写路由，**每条必须自带鉴权**，理由写死在这里。
#: 2026-11-07 C 档：设置写端点（model / github / term-token）已全部收回登录闸门，
#: 原来的 HUB_PASSCODE 自校验与对应豁免一并删除；只剩下面三条，都是「不豁免就自锁死」。
EXEMPT_PREFIXES: Dict[str, str] = {
    "/telemetry/events/":
        "hook.py 自带方案：设了 HOOK_AUTH_TOKEN 必须 Bearer；未设则只允许回环来源。",
    "/api/users/login":
        "密码登录端点：自带密码校验（错→401）+ 进程内限速（连败 5 次锁 60s）。"
        "若被 write_gate 当普通写端拦掉，未登录用户永远进不来＝自锁死。"
        "它只签发用户自己的会话 Cookie，不碰其他用户面；改密走 /api/users/{name}/password（需已登录，不豁免）。",
    "/api/users/logout":
        "退出端点：只吊销请求自带 Cookie 对应的会话并清 Cookie，幂等。"
        "未登录也必须能调（否则会话过期后前端连「退出」都做不到）。",
}


def exempt_reason(path: str) -> Optional[str]:
    for p, why in EXEMPT_PREFIXES.items():
        if path.startswith(p):
            return why
    return None


def decide(method: str, path: str, identity: bool, users_ready: bool) -> Tuple[str, str]:
    """纯判定，便于 L0 空 HOME 下取证。返回 (verdict, reason)。

    `identity`：本次请求是否已解析出登录用户（Cookie 会话或 HTTP Basic）。
    `users_ready`：用户库是否可用（不可用 ⇒ 503，而不是把所有人的写操作放行）。

    fail-closed 的两处细节：
      · 用户库不可用 → 503 而不是放行（"不知道该不该信"不等于"可以写"）；
      · 拒绝原因里绝不回显任何凭据。
    """
    if method.upper() not in WRITE_METHODS:
        return "allow", "非写方法"
    why = exempt_reason(path)
    if why:
        return "exempt", why
    if identity:
        return "allow", "登录用户（Cookie 会话 / HTTP Basic）"
    if not users_ready:
        return "misconfig", "用户库不可用，写端点一律拒绝（fail-closed）"
    return "deny", "未登录（须 hub 会话 Cookie 或 HTTP Basic）"


def decide_req(request, method: str = "") -> Tuple[str, str]:
    """给各模块「保险带」用的一行式封装：自己解析身份，不再接受共享 token。

    `method` 留空则用请求自身的；导出/审计这类「GET 也算数据外流」的端点显式传
    "POST"，保持改动前「强制按写方法判」的语义。
    """
    try:
        ok = identity_ok(request)
    except Exception:  # noqa: BLE001
        ok = False
    try:
        import db as _db
        ready = _db.is_open()
    except Exception:  # noqa: BLE001
        ready = False
    return decide(method or request.method, request.url.path, ok, ready)


#: 读端点令牌门（v0.13.192 / P0，方案 AgentHub优化设计方案.md §6.6）。
#: `_is_loopback` 与 `_READ_TOKEN_ALLOW` 由 main.py 注册前注入（同 CSP_REPORT 的
#: 注入模式，避免本模块 import main）。
_is_loopback = lambda host: host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:0:0:0:0:0:0:1")  # noqa: E731
_READ_TOKEN_ALLOW: set = set()
READ_TOKEN_DISABLED = False

#: 非回环读请求的 401 体：detail 给人话（为什么 + 下一步），横幅按 detail 渲染，
#: 两个字段给前端程序化判断（main.health() 复用同一判断，见 read_banner_state）。
READ_DENY_BODY = {
    "detail": "读端点需要登录：非本机访问须先在 hub 登录（浏览器）或带 HTTP Basic（脚本）。本机 127.0.0.1 保持匿名（保持一个版本）。",
    "read_auth_required": True,
    "reason": "non-loopback-anonymous",
}


def read_verdict(client_host: str, path: str, identity: bool,
                 users_ready: bool) -> Tuple[str, str]:
    """读令牌门纯判定（便于 L0 空 HOME 取证，同 decide 的形态）。返回 (verdict, reason)。

    verdict："allow" / "deny" / "misconfig"。
      · 回环来源 → allow（本地脚本/探针/嵌页零改动，保持一个版本）；
      · 非回环 + 已登录（Cookie/Basic）→ allow；
      · 非回环 + 未登录：用户库不可用 → misconfig（503，与写门同口径），否则 deny（401）。
    """
    if READ_TOKEN_DISABLED:
        return "allow", "HUB_READ_TOKENS=0 整门关闭"
    if _is_loopback(client_host or ""):
        return "allow", "回环来源保持匿名"
    if any(path.startswith(p) for p in _READ_TOKEN_ALLOW):
        return "allow", "读放行前缀"
    if identity:
        return "allow", "登录用户（Cookie 会话 / HTTP Basic）"
    if not users_ready:
        return "misconfig", "用户库不可用，非回环读端点一律拒绝（fail-closed）"
    return "deny", "非本机访问读端点须先登录（Cookie 会话 / HTTP Basic）"


def read_banner_state(client_host: str, path: str, identity: bool, users_ready: bool) -> dict:
    """`/health` 内嵌字段：前端工作台据此渲染红色横幅。只报判定结果，不回显凭据值。"""
    verdict, reason = read_verdict(client_host, path, identity, users_ready)
    if verdict == "allow":
        return {"state": "ok", "reason": None}
    return {"state": "locked" if verdict == "deny" else "misconfig", "reason": reason}


async def read_gate(request, call_next):
    """读端点门中间件。Starlette 后注册者最外层 ⇒ 注册在 write_gate 之前、
    csp.middleware 之后 ⇒ csp 头覆盖它回 401，顺序安全。

    2026-11-07 用户裁定：浏览器身份 = Cookie 会话（用户名+密码登录后由
    /api/users/login 种下），机器身份 = HTTP Basic（同样用户名+密码）。
    **共享 token（x-hub-token / x-term-token / ?token=）已整体删除**，不再有兼容分支。

    放行面：非 GET 方法（写由 write_gate 管）；`/`（嵌页入口，CSP 报告/脚本同源依赖）；
    `/health`（自证+探针）；`/static/`（页面自举，无数据面）；`/api/csp/report`；
    以及 `_READ_TOKEN_ALLOW`（main 注入：`/api/settings/model/catalog` 等无凭据也能读的公共面，
    逐条写理由，同 EXEMPT_PREFIXES 纪律）。其余 GET 按「身份 → 回环」依次判。
    """
    if request.method != "GET":
        return await call_next(request)
    path = request.url.path
    host = request.client.host if request.client else ""
    # 公共面先放行：静态资源每页几十个请求，不该为它们查会话表。
    if (path in ("/", "/health", "/api/csp/report") or path.startswith("/static/")):
        return await call_next(request)
    # Cookie / Basic 身份（浏览器与脚本的同一份用户名+密码）
    urow = None
    uperms = None
    try:
        import users as _users_ident
        urow, uperms, _how = _users_ident.identity_of(request)
    except Exception:  # noqa: BLE001 —— 用户库不可用不许把读面全打挂
        urow, uperms = None, None
    if urow is not None:
        request.state.actor = "user:" + urow.get("username", "?")
        request.state.user = urow
        request.state.user_perms = uperms
        return await call_next(request)
    try:
        import db as _db_r
        users_ready = _db_r.is_open()
    except Exception:  # noqa: BLE001
        users_ready = False
    verdict, reason = read_verdict(host, path, False, users_ready)
    if verdict == "allow":
        request.state.actor = "user:reader"
        # 匿名放行（回环 / 公共读前缀）⇒ 没有身份。
        # 2026-11-07：此前这里塞了一个 legacy 全模块伪用户，导致
        # /api/users/whoami 对匿名访客也回 username=legacy ⇒ 登录闸门
        # 把未登录当成已登录，直接放行工作台（刷新后绕过登录页）。
        request.state.user = None
        request.state.user_perms = None
        return await call_next(request)
    status = 503 if verdict == "misconfig" else 401
    print(f"[readgate] {status}：GET {path} 来源={host or '?'} —— {reason}")
    body = dict(READ_DENY_BODY) if verdict == "deny" else {"detail": reason, "read_auth_required": True, "reason": "misconfig"}
    return JSONResponse(body, status_code=status)




def identity_ok(request) -> bool:
    """本请求是否已有登录身份（Cookie 会话 / HTTP Basic）。
    供各模块的「保险带」复用（api_sessions/audit/prefs/cloudcli 都在
    全局 write_gate 之外再显式判一次）：有身份就放行，没有再走旧凭据判定，
    避免这些二级闸门把已登录用户的请求重新打回 401。"""
    try:
        import users as _users
        u, _p, _how = _users.identity_of(request)
        return u is not None
    except Exception:  # noqa: BLE001
        return False


def actor_of(request) -> str:
    """从 request.state 取审计身份；取不到 → "system"（后台循环/内部调用）。

    **绝不抛**：审计身份缺失不许把业务请求打挂（审计是附加价值，不是准入条件）。
    """
    try:
        a = getattr(getattr(request, "state", None), "actor", "")
        return a or "system"
    except Exception:  # noqa: BLE001
        return "system"


async def write_gate(request, call_next):
    """注册进 main.py。Starlette 里**后注册的中间件在最外层**，所以本闸门先于
    api_rate_limit 执行：被拒的请求不该占用限流预算，也不该走到 handler 产生副作用。

    P1-2 在「凭据已过」之后再加一态：`allow` 但落在 ASK 面 ⇒ 回 202 + 预览，**不执行**；
    带有效一次性票据才放行（Commit）。ASK 面默认空（`HUB_ASK_PREFIXES`）⇒ 零行为变化。
    """
    method = request.method
    path = request.url.path
    # 2026-11-07 用户裁定：写请求只认 Cookie 会话 / HTTP Basic；
    # 共享 token（x-hub-token / x-term-token / ?token=）已整体删除。
    ureq = None
    uperms_req = None
    try:
        import users as _users_ident
        ureq, uperms_req, _how = _users_ident.identity_of(request)
    except Exception:  # noqa: BLE001
        ureq, uperms_req = None, None
    try:
        import db as _db_w
        users_ready = _db_w.is_open()
    except Exception:  # noqa: BLE001
        users_ready = False
    verdict, reason = decide(method, path, ureq is not None, users_ready)
    if verdict in ("allow", "exempt"):
        actor = ("user:exempt" if verdict == "exempt"
                 else ("user:" + ureq["username"] if ureq else "user:anonymous"))
        request.state.actor = actor
        if verdict == "allow":
            ask = approvals.ask_reason(path)
            if ask:
                aid, tk = approvals.split_header(approval_header(request.headers.raw))
                ok, why = approvals.verify_and_consume(aid, tk, method, path, actor)
                if not ok:
                    # ⚠ 只在这一支读 body：此处立刻 return，不会把下游的 receive 抽干。
                    snippet, total = await read_body_preview(request)
                    preview = approvals.build_preview(method, path, actor,
                                                      request.url.query, snippet, total)
                    rec = approvals.create(method, path, actor, ask, preview)
                    print(f"[writegate] 202：{method} {path} ASK（{why}）审批 id={rec['id']}")
                    return JSONResponse({"detail": ask, "requires_approval": True,
                                         "approval": rec, "why": why}, status_code=202)
        # 身份注入：Cookie/Basic 已解析出 ureq ⇒ 直接用；exempt 面（自带口令的
        # 设置端点）没有会话身份，但仍要记 state.user 供审计读（admin 语义）。
        try:
            if ureq is not None:
                request.state.user = ureq
                request.state.user_perms = uperms_req
            else:
                request.state.user = {"id": 0, "username": "exempt", "display": "豁免端点",
                                      "is_admin": True, "created_at": "", "updated_at": ""}
                request.state.user_perms = None
        except Exception:  # 审计面失败不得打断业务（与 actor_of 同口径）
            request.state.user = None
            request.state.user_perms = None
        return await call_next(request)
    status = 503 if verdict == "misconfig" else 401
    print(f"[writegate] {status}：{method} {path} 来源={request.client.host if request.client else '?'}"
          f" —— {reason}")
    return JSONResponse({"detail": reason}, status_code=status)


def approval_header(headers: Iterable[Tuple[bytes, bytes]]) -> str:
    """取 `x-hub-approval` 头（P1-2）。没带 → 空串（⇒ 走 ASK 分支）。"""
    want = approvals.APPROVAL_HEADER.encode()
    for k, v in headers:
        if k.lower() == want:
            return v.decode("latin-1")
    return ""


async def read_body_preview(request, cap: int = 8192) -> Tuple[str, int]:
    """读一段正文做预览，返回 (截断后的文本, **原始字节数**)。

    ⚠ **只能用在「读完整即 return」的分支里**（本闸门的 ASK 分支正是如此）：
    在 BaseHTTPMiddleware 里 `await request.body()` 会把 receive 抽干，
    若随后还调 `call_next`，下游 handler 读 body 会挂死。
    """
    try:
        raw = await request.body()
    except Exception:  # noqa: BLE001
        return "", 0
    return raw[:cap].decode("utf-8", "replace"), len(raw)
