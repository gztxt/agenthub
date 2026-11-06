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

    放行面：非 GET 方法（写由 write_gate 管）；`/`（嵌页入口，CSP 报告/脚本同源依赖）；
    `/health`（自证+探针）；`/static/`（页面自举，无数据面）；`/api/csp/report`；
    以及 `_READ_TOKEN_ALLOW`（main 注入：`/api/settings/model/catalog` 等无凭据也能读的公共面，
    逐条写理由，同 EXEMPT_PREFIXES 纪律）。其余 GET 一律按 read_verdict 判。"""
    if request.method != "GET":
        return await call_next(request)
    path = request.url.path
    host = request.client.host if request.client else ""
    if (path in ("/", "/health", "/api/csp/report") or path.startswith("/static/")):
        return await call_next(request)
    provided = provided_token(request.headers.raw, request.url.query)
    secrets = secrets_from_env()
    verdict, reason = read_verdict(host, path, provided, secrets)
    if verdict == "allow":
        request.state.actor = "user:reader"
        return await call_next(request)
    status = 503 if verdict == "misconfig" else 401
    print(f"[readgate] {status}：GET {path} 来源={host or '?'} —— {reason}")
    body = dict(READ_DENY_BODY) if verdict == "deny" else {"detail": reason, "read_auth_required": True, "reason": "misconfig"}
    return JSONResponse(body, status_code=status)




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
    verdict, reason = decide(method, path, provided, secrets)
    if verdict in ("allow", "exempt"):
        actor = ("user:exempt" if verdict == "exempt"
                 else "user:" + credential_name(provided, secrets))
        # ── P1-2：ALLOW/DENY 之外的第三态 ASK ──
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
        # 审计身份：只记凭据**名**，绝不记值。starlette 的 Request.state 是可写属性袋。
        request.state.actor = actor
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
