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
TOKEN_HEADERS: Tuple[str, ...] = ("x-hub-token", "x-term-token")

#: 不经过本闸门的写路由，**每条必须自带鉴权**，理由写死在这里。
EXEMPT_PREFIXES: Dict[str, str] = {
    "/api/settings/term-token":
        "它自己校验 HUB_PASSCODE（口令错→401）。若在这里拦掉，用户永远输不进口令＝自锁死。",
    "/telemetry/events/":
        "hook.py 自带方案：设了 HOOK_AUTH_TOKEN 必须 Bearer；未设则只允许回环来源。",
    "/api/settings/model/apply":
        "它自己校验 HUB_PASSCODE（错→401、未配→503），与同页的 term-token 同口径；"
        "再叠一层 token 门 = 用户在设置页永远改不动模型（自锁死）。",
    "/api/settings/model/verify-live":
        "v0.13.164：真实验证会真打一次网关（付费模型按量计费），端点自己校验 "
        "HUB_PASSCODE（错→401、未配→503）。与 apply 同口径，且口令走 POST body 不进 URL；"
        "再叠 token 门会让设置页的「真实验证」永远 401。",
    "/api/settings/model/undo":
        "v0.13.181：撤销上一次模型写入会把配置文件还原（也是写盘），端点自己校验 "
        "HUB_PASSCODE（错→401、未配→503），与 apply 同口径、口令走 POST body。"
        "还原源只取自 DB 里记的备份路径，不接受客户端传路径。",
    "/api/settings/github/":
        "设置→GitHub 四个写端点（test/apply/clear/refresh）各自校验 HUB_PASSCODE"
        "（错→401、未配→503）：地址与 key 只能从设置页改，再叠 token 门同样自锁死。"
        "它们写的是 hub 自己的 github_settings 表（+ 可选回写文件，落笔前备份），"
        "不碰 agent 配置。",
    "/api/users/login":
        "密码登录端点：自带密码校验（错→401）+ 进程内限速（连败 5 次锁 60s）。"
        "若被 write_gate 当普通写端拦掉，未登录用户永远进不来＝自锁死（"
        "同 /api/settings/term-token 的豁免纪律）。它只签发用户自己的会话 Cookie，"
        "不碰其他用户面；改密走 /api/users/{name}/password（需已登录，不豁免）。",
    "/api/users/logout":
        "退出端点：只吊销请求自带 Cookie 对应的会话并清 Cookie，幂等。"
        "未登录也必须能调（否则会话过期后前端连「退出」都做不到）。",
}


def exempt_reason(path: str) -> Optional[str]:
    for p, why in EXEMPT_PREFIXES.items():
        if path.startswith(p):
            return why
    return None


def provided_token(headers: Iterable[Tuple[bytes, bytes]], query: str) -> str:
    """从头或 ?token= 取凭据。query 保留是为了给不能自定义头的客户端留一条路，
       与 term.py 既有口径一致（它一直允许 ?token=）。"""
    want = {h.encode().lower() for h in TOKEN_HEADERS}
    for k, v in headers:
        if k.lower() in want:
            return v.decode("latin-1")
    for part in (query or "").split("&"):
        if part.startswith("token="):
            return part[6:]
    return ""


def decide(method: str, path: str, provided: str, secrets: Iterable[str]) -> Tuple[str, str]:
    """纯判定，便于 L0 空 HOME 下取证。返回 (verdict, reason)。

    fail-closed 的两处细节：
      · 未配置任何凭据 → 503 而不是放行（"没设口令"不等于"不用口令"）；
      · 比较一律 hmac.compare_digest，且**拒绝原因里绝不回显任何凭据**。
    """
    if method.upper() not in WRITE_METHODS:
        return "allow", "非写方法"
    why = exempt_reason(path)
    if why:
        return "exempt", why
    live = [s for s in secrets if s]
    if not live:
        return "misconfig", "服务端未配置 TERM_TOKEN/HUB_PASSCODE，写端点一律拒绝（fail-closed）"
    if not provided:
        return "deny", "缺少凭据（x-hub-token / x-term-token / ?token=）"
    for s in live:
        if hmac.compare_digest(provided, s):
            return "allow", "凭据匹配"
    return "deny", "凭据不匹配"


def secrets_from_env() -> list:
    return [os.getenv("TERM_TOKEN", ""), os.getenv("HUB_PASSCODE", "")]


#: 凭据的**名字**（顺序与 secrets_from_env 严格一致）。审计只记"用了哪把钥匙"，绝不记钥匙本身。
SECRET_NAMES: Tuple[str, ...] = ("term-token", "hub-passcode")


def credential_name(provided: str, secrets: Iterable[str]) -> str:
    """provided 命中了哪一把凭据的**名字**；未提供/未命中 → "anonymous"。

    为什么新增而不改 `decide()`：decide 的 (verdict, reason) 被中间件与
    `/api/sessions/export` 端点共用，且 tests/test_writeauth.py 12 例钉着签名。
    改返回值＝零收益地撞 12 例既有闸门；新增纯函数则两边都不动。
    """
    if not provided:
        return "anonymous"
    for name, s in zip(SECRET_NAMES, secrets):
        if s and hmac.compare_digest(provided, s):
            return name
    return "anonymous"


#: 读端点令牌门（v0.13.192 / P0，方案 AgentHub优化设计方案.md §6.6）。
#: `_is_loopback` 与 `_READ_TOKEN_ALLOW` 由 main.py 注册前注入（同 CSP_REPORT 的
#: 注入模式，避免本模块 import main）。
_is_loopback = lambda host: host in ("127.0.0.1", "::1", "::ffff:127.0.0.1", "::ffff:0:0:0:0:0:0:1")  # noqa: E731
_READ_TOKEN_ALLOW: set = set()
READ_TOKEN_DISABLED = False

#: 非回环读请求的 401 体：detail 给人话（为什么 + 下一步），横幅按 detail 渲染，
#: 两个字段给前端程序化判断（main.health() 复用同一判断，见 read_banner_state）。
READ_DENY_BODY = {
    "detail": "读端点需要凭据：非本机访问须带 x-hub-token / x-term-token（或 ?token=）。本机 127.0.0.1 保持匿名（保持一个版本）。",
    "read_auth_required": True,
    "reason": "non-loopback-without-token",
}


def read_verdict(client_host: str, path: str, provided: str, secrets: Iterable[str]) -> Tuple[str, str]:
    """读令牌门纯判定（便于 L0 空 HOME 取证，同 decide 的形态）。返回 (verdict, reason)。

    verdict："allow" / "deny" / "misconfig"。
      · 回环来源 → allow（本地脚本/探针/嵌页零改动，保持一个版本）；
      · 非回环 + 凭据命中 secrets → allow；
      · 非回环 + 未命中：secrets 未配置 → misconfig（503，fail-closed 与写门同口径），
        否则 → deny（401）。
    """
    if READ_TOKEN_DISABLED:
        return "allow", "HUB_READ_TOKENS=0 整门关闭"
    if _is_loopback(client_host or ""):
        return "allow", "回环来源保持匿名"
    if any(path.startswith(p) for p in _READ_TOKEN_ALLOW):
        return "allow", "读放行前缀"
    live = [s for s in secrets if s]
    if not live:
        return "misconfig", "服务端未配置 TERM_TOKEN/HUB_PASSCODE，非回环读端点一律拒绝（fail-closed）"
    if not provided:
        return "deny", "非本机访问读端点需要凭据（x-hub-token / x-term-token / ?token=）"
    for s in live:
        if hmac.compare_digest(provided, s):
            return "allow", "凭据匹配"
    return "deny", "凭据不匹配"


def read_banner_state(client_host: str, path: str, provided: str, secrets: Iterable[str]) -> dict:
    """`/health` 内嵌字段：前端工作台据此渲染红色横幅。只报判定结果，不回显凭据值。"""
    verdict, reason = read_verdict(client_host, path, provided, secrets)
    if verdict == "allow":
        return {"state": "ok", "reason": None}
    return {"state": "locked" if verdict == "deny" else "misconfig", "reason": reason}


async def read_gate(request, call_next):
    """读端点令牌门中间件。Starlette 后注册者最外层 ⇒ 注册在 write_gate 之前、
    csp.middleware 之后 ⇒ csp 头覆盖它回 401，顺序安全。

    2026-11-07 用户裁定后口径：浏览器身份 = Cookie 会话（用户名+密码登录后由
    /api/users/login 种下），机器身份 = HTTP Basic（同样用户名+密码）。
    `x-hub-token` 仅作为内部兼容面保留（既有脚本/MCP 未迁移完），**浏览器不再使用**。

    放行面：非 GET 方法（写由 write_gate 管）；`/`（嵌页入口，CSP 报告/脚本同源依赖）；
    `/health`（自证+探针）；`/static/`（页面自举，无数据面）；`/api/csp/report`；
    以及 `_READ_TOKEN_ALLOW`（main 注入：`/api/settings/model/catalog` 等无凭据也能读的公共面，
    逐条写理由，同 EXEMPT_PREFIXES 纪律）。其余 GET 按「身份 → 回环 → 旧凭据」依次判。
    """
    if request.method != "GET":
        return await call_next(request)
    path = request.url.path
    host = request.client.host if request.client else ""
    # 公共面先放行：静态资源每页几十个请求，不该为它们查会话表。
    if (path in ("/", "/health", "/api/csp/report") or path.startswith("/static/")):
        return await call_next(request)
    # ① Cookie / Basic 身份（浏览器与脚本的同一份用户名+密码）
    urow = None
    uperms = None
    try:
        import users as _users_ident
        urow, uperms, _how = _users_ident.identity_of(request)
    except Exception:  # noqa: BLE001 —— 用户库不可用不许把读面全打挂
        urow, uperms = None, None
    # ② 未命中再看旧内部凭据（x-hub-token / ?token=），供未迁移的脚本/MCP
    provided = provided_token(request.headers.raw, request.url.query)
    secrets = secrets_from_env()
    if urow is None and provided:
        try:
            import users as _users_early
            _r, _p, _compat = _users_early.resolve_token(provided)
            if _r and not _compat:
                urow, uperms = _r, _p
        except Exception:  # noqa: BLE001
            pass
    if urow is not None:
        request.state.actor = "user:" + urow.get("username", "?")
        request.state.user = urow
        request.state.user_perms = uperms
        return await call_next(request)
    verdict, reason = read_verdict(host, path, provided, secrets)
    if verdict == "allow":
        request.state.actor = "user:reader"
        try:
            if provided:
                import users as _users
                urow2, uperms2, compat = _users.resolve_token(provided)
                if compat or not urow2:
                    urow2 = {"id": 0, "username": "legacy", "display": "兼容旧令牌",
                             "is_admin": True, "created_at": "", "updated_at": ""}
                    uperms2 = None
                request.state.user = urow2
                request.state.user_perms = uperms2
            else:
                # 匿名放行（回环 / 公共读前缀）⇒ 没有身份。
                # 2026-11-07：此前这里塞了一个 legacy 全模块伪用户，导致
                # /api/users/whoami 对匿名访客也回 username=legacy ⇒ 登录闸门
                # 把未登录当成已登录，直接放行工作台（刷新后绕过登录页）。
                request.state.user = None
                request.state.user_perms = None
        except Exception:
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
    provided = provided_token(request.headers.raw, request.url.query)
    secrets = secrets_from_env()
    # 2026-11-07 用户裁定：浏览器面写请求认 Cookie 会话，脚本面认 HTTP Basic；
    # x-hub-token 仅作内部兼容（未迁移的脚本/MCP），浏览器不再使用。
    ureq = None
    uperms_req = None
    try:
        import users as _users_ident
        ureq, uperms_req, _how = _users_ident.identity_of(request)
    except Exception:  # noqa: BLE001
        ureq, uperms_req = None, None
    if ureq is None and provided:
        try:
            import users as _users_w
            _r, _p, _c = _users_w.resolve_token(provided)
            if _r and not _c:
                ureq, uperms_req = _r, _p
        except Exception:  # noqa: BLE001
            pass
    if ureq is not None:
        verdict, reason = "allow", "用户会话（Cookie/Basic）"
    else:
        verdict, reason = decide(method, path, provided, secrets)
    if verdict in ("allow", "exempt"):
        actor = ("user:exempt" if verdict == "exempt"
                 else ("user:" + ureq["username"] if ureq else "user:" + credential_name(provided, secrets)))
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
        # 2026-11-07 用户授权：解析「这个 token 属于哪个用户 + 可见模块」。
        # 命中用户表 ⇒ state.user(行) + state.user_perms(set|None)；
        # 回落 .env 兼容口令（含 exempt 面）⇒ 全模块（user_perms=None）保证零行为变化。
        try:
            if ureq is not None:
                request.state.user = ureq
                request.state.user_perms = uperms_req
                return await call_next(request)
            import users as _users
            urow, uperms, compat = _users.resolve_token(provided) if verdict == "allow" else (None, None, True)
            if compat or not urow:
                urow = {"id": 0, "username": "legacy", "display": "兼容旧令牌",
                        "is_admin": True, "created_at": "", "updated_at": ""}
                uperms = None
            request.state.user = urow
            request.state.user_perms = uperms
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
