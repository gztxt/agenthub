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
