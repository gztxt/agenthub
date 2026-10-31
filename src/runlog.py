"""运行日志（v0.13.27）：三中心检索调用的留痕 + 可翻页查询门面。

设计口径（为什么这么小）：
- **零新表**：复用 profile_events（detail 是 JSON TEXT 列已存在），source='rest'。
  既有 source 枚举（hub_chat/task_exec/cron_run/mcp_call）记的是「agent 执行画像」，
  本模块记「三中心被谁调用、多快、哪路降级」——同为 append-only 事件流，口径兼容。
- **埋点是附加价值，不是准入条件**：_fire 整体包 try，DB 炸了绝不打断检索请求
  （但会 print 一行，不静默——本仓纪律）。
- **MCP 通道自动覆盖**：hubmcp 的 9 个工具经 _get() 回环转调 REST ⇒ 在 REST 端点
  埋点即同时覆盖 MCP 通道。区分靠 x-hub-channel 头（hubmcp._get 发起时自带）；
  头可伪造，但这是遥测不是鉴权，误标只产生无害噪声。
- **不埋的点**（防噪声，写死在这里别「顺手加」）：/api/agents、/api/ports、/health
  ——前端 30s 轮询，埋了等于把画像面板淹掉；/api/memory/l1*（管理面非检索面）。

权限：GET /api/runlog **按写方法判**（writeauth.decide("POST", ...)）——运行日志
含查询词可反推用户意图，属敏感面，与 /api/audit/list 同口径（src/audit.py 先例）。
fail-closed：服务端没配口令 ⇒ 503 而不是放行。
"""
from __future__ import annotations

import functools
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

import execsem    # P2 C3：统一执行语义枚举（词表唯一真相源）
import db
import tdai_client
import usage_ledger

log = logging.getLogger(__name__)
import writeauth

router = APIRouter()

#: 本模块管理的 subject 枚举——与 track() 调用点一一对应。查询侧可枚举才做得出下拉。
SUBJECTS = ("mem.search", "mem.context", "kb.search", "kb.browse",
            "kb.status", "skill.list", "skill.read", "skill.relevant",
            "skill.inject", "skill.invoke", "cc.start")

#: 埋点 source（与 hub_chat/task_exec/cron_run/mcp_call 并列）。hook.py 的画像聚合
#: 会排除它，防止高频检索事件把 agent 画像挤出前 50（见批1 连带项）。
SOURCE = "rest"

_Q_KEYS = ("q", "name", "sub")            # 从 handler kwargs 提取查询词
_LIMIT_KEYS = ("limit", "k")
_ROUTE_KEYS = ("routes", "sources")
_Q_MAX_CHARS = 120                         # 查询词落库上限（防长文入库膨胀 detail）
#: v0.13.127 PT-20261019-01：附加上下文（调用流水要答「调的哪个、谁调的、想干什么」）。
#: 只对带这些字段的端点（/api/skill/invoke、/api/skill/read）产出内容，其它端点零噪声。
#: ⚠ `name` 必须在这里再挖一遍 body：**失败路径没有 `_pick_names`**（那个读的是响应体，
#: 而失败时响应体是 HTTPException 的 detail）⇒ 不挖的话 409/404 的 invoke 事件
#: 不带技能名，「调用记录」面板里的失败行没有身份，等于半聋。
_EXTRA_KEYS = ("name", "route", "caller", "task", "via")
_TASK_MAX_CHARS = 500


def _mode_of(subject: str, kwargs: Dict[str, Any], forced: str = "") -> str:
    """这条技能事件是「指名调用」还是「自动取用/下发」——权重统计必须先分层再计数。

    口径只有一条：**调用方显式声明才算自动**。注入链（pi 的 hub-facade.ts、claude 的
    hub_skill_inject.py）在用户输入时无条件打 `/relevant`，没人点名任何技能 ⇒ 候选
    是机器选的；`/auto` 更是服务端自己按任务描述选技能并激活。而相关性实验室/手工
    查询虽然也打同一端点，却是人在问。故不能按端点分，只能按调用方显式声明分：
      · skill.invoke + via=auto/hook → auto_invoke（服务端/注入链选出来的技能被激活）
      · skill.invoke 其它             → explicit_invoke（有人点了这个技能的名字）
      · skill.inject                 → auto_inject（注入链**确实把候选塞进了 agent 上下文**）
      · skill.relevant 其它            → query（人工检索，不是使用证据）

    ⚠ 2026-10-27 起注入链不再记 `skill.relevant`：同一个动作以前叫「查询」（query），
    于是「注入」这件事在账上不存在（`skill.inject` 恒 0，只剩桶标签）。现在
    `/relevant?via=hook` 的成功响应改落 `skill.inject`，人工查询仍落 `skill.relevant`。
    两个 subject 互斥（同一请求只会落一个），不会重复计数。
    未声明 `via` 的旧客户端**不推断**为 auto：把它们算进自动桶会把「UI 里试一下」
    当成「agent 主动用了」，权重直接失真。

    `forced` 由端点自己声明（`track(..., mode="auto_invoke")`）——`/auto` 的语义
    写在代码里，不交给客户端传 `via`；客户端伪造 `via` 只能影响它自己的埋点。
    """
    if forced:
        return forced
    if subject == "skill.invoke":
        via = str(kwargs.get("via") or "").strip().lower()
        return "auto_invoke" if via in ("auto", "hook") else "explicit_invoke"
    if subject == "skill.inject":
        return "auto_inject"
    if subject == "skill.relevant":
        return "auto_inject" if str(kwargs.get("via") or "").strip().lower() == "hook" else "query"
    return ""


def _subject_for(subject: str, kwargs: Dict[str, Any], has_names: bool) -> str:
    """把「注入链的查询」改记成 `skill.inject`；其它情况原样返回。

    为什么不在端点上换装饰器：`/api/skill/relevant` 同时服务**人工检索**（相关性实验室）
    与**注入链**（pi `hub-facade.ts` / claude `hub_skill_inject.py`），只有后者才是
    「候选被塞进 agent 上下文」。判据是调用方显式声明的 `via=hook`——与 `_mode_of`
    同一根依据，不另造一套。
    `has_names=False`（本轮没有候选）时不算注入：什么都没塞进去。
    """
    if subject == "skill.relevant" and has_names and \
            str(kwargs.get("via") or "").strip().lower() == "hook":
        return "skill.inject"
    return subject


def _pick_extra(kwargs: Dict[str, Any]) -> Dict[str, Any]:
    """从 kwargs 顶层 + pydantic 请求体里取 name/route/caller/task（v0.13.127）。

    为什么需要翻请求体：`POST /api/skill/invoke` 的这些字段在 **body**
    （`InvokeRequest`）里，而 track 原先只认顶层 kwargs ⇒ 它们整个漏出埋点
    ⇒「调用记录」面板看不到调了哪个、谁调的、想干什么，Grill 裁定 3 的记账模型直接瞎一半。
    BaseModel 用 `model_dump()`/`dict()` duck-type 探测（pydantic v2/v1 都覆盖），
    **不 import pydantic**——runlog 被六个以上模块导入，不加无谓依赖。
    任何异常都吞掉返回空 dict：埋点不许带倒业务（本模块一贯纪律）。

    成功路径上 `name` 会被随后的 `_pick_names` 逐事件值覆盖（同值，见 track 主体）；
    失败路径没有那个覆盖，这里的 `name` 就是失败事件唯一的身份来源。
    """
    out: Dict[str, Any] = {}
    srcs: List[Dict[str, Any]] = []
    for v in list(kwargs.values()) + [kwargs]:
        if isinstance(v, dict):
            srcs.append(v)
            continue
        dump = getattr(v, "model_dump", None) or getattr(v, "dict", None)
        if callable(dump):
            try:
                d = dump()
                if isinstance(d, dict):
                    srcs.append(d)
            except Exception:  # noqa: BLE001 — 探测失败静默跳过，不影响埋点主体
                pass
    for src in srcs:
        for k in _EXTRA_KEYS:
            if k in out:
                continue
            v = src.get(k)
            if v not in (None, ""):
                out[k] = tdai_client.scrub(str(v))[:_TASK_MAX_CHARS]
    return out


def _pick_query(kwargs: Dict[str, Any]) -> str:
    for k in _Q_KEYS:
        v = kwargs.get(k)
        if v:
            return str(v)[:_Q_MAX_CHARS]
    return ""


def _pick_limit(kwargs: Dict[str, Any]) -> Optional[int]:
    for k in _LIMIT_KEYS:
        v = kwargs.get(k)
        if v is not None:
            try:
                return int(v)
            except (TypeError, ValueError):
                continue
    return None


def _pick_routes(kwargs: Dict[str, Any]) -> str:
    for k in _ROUTE_KEYS:
        v = kwargs.get(k)
        if v:
            return str(v)[:_Q_MAX_CHARS]
    return ""


#: 技能名落库上限。技能名实测最长 ~30 字符（`repo-to-local-kb-fusion`），
#: 留 2倍余量；**必须设上限** —— detail 要进日志中心的文本检索，无上限就是往库里灌长文本。
_NAME_MAX_CHARS = 64

#: 从端点返回体里挖技能名。**这是`skill_usage.counts()` 能记账的唯一前提**——
#: 它只认 `detail["name"]`，而本模块原先只记 q/limit/routes/channel，
#: 于是「零调用僵尸榜」恒等于「全部技能」（判例 103：counted=0 时整榜不可用于删除决策）。
#:
#: 为什么放在这里而不是让skill.py 自己拼 detail：装饰器是**单一落库出口**，
#: 散写必漏（模块 docstring 的原话）。端点只要 `@runlog.track("skill.*")` 就自动带上名字。
#:
#: 逐层降级，任一层形状不认识就返回空串，**绝不让埋点抛异常带倒业务端点**：
#:   ① {"bm25": {"items": [{"name": ...}]}}← /api/skill/relevant 的真实形状
#:   ② {"items": [{"name": ...}]}             ← 扁平形状（/api/skill/list）
#:   ③ {"names": ["a", "b"]}                  ← 只有名字列表
#:   ④ ["a", "b"]                              ← 直接是列表
#:   ⑤ {"name": "x", ...}                     ← /api/skill/read 的单对象形状（**无 items 包装**）
def _pick_names(data: Any) -> List[str]:
    """从返回体里提取技能名列表（去重、保序、截断）。**任何异常都返回空列表**。

    ⚠ **第⑤层（单对象）不能省**：`/api/skill/read` 回的是**一个**技能对象（顶层直接是
    `name`/`content`/`route`…），**不套 `items`**。少了它，`skill.read` 事件永远不带 `name`
    ⇒ `skill_usage.counts()` 的 reads 桶恒空（`_name_of` 取不到名就丢行）。
    这不是「少记一条日志」，是「读技能这件事在僵尸榜上不可见」——与判例 103 同族。
    """
    try:
        items: Any = None
        if isinstance(data, dict):
            bm = data.get("bm25")
            if isinstance(bm, dict):
                items = bm.get("items")
            if items is None:
                items = data.get("items")
            if items is None and isinstance(data.get("names"), list):
                items = data["names"]
            if items is None and isinstance(data.get("name"), str):
                # /api/skill/read：单个技能对象。**只认顶层 str 型 name**——
                # `list`/`relevant` 的顶层没有 `name` 键，故不会把它们的包装对象误当成条目。
                items = [data]
        elif isinstance(data, list):
            items = data
        if not isinstance(items, list):
            return []
        out: List[str] = []
        for it in items:
            name = None
            if isinstance(it, dict):
                name = it.get("name") or it.get("skill")
            elif isinstance(it, str):
                name = it
            if name and str(name).strip() and str(name) not in out:
                out.append(str(name).strip()[:_NAME_MAX_CHARS])
        return out
    except Exception:  # noqa: BLE001
        return []


def _route_of(data: Any) -> str:
    """单对象返回体的来源路（`/read`、`/invoke` 顶层有 `route`）。取不到返回空串。"""
    try:
        if isinstance(data, dict):
            return str(data.get("route") or "").strip()[:_NAME_MAX_CHARS]
    except Exception:  # noqa: BLE001
        pass
    return ""


def _route_by_name(data: Any) -> Dict[str, str]:
    """列表型返回体里的 `name → route` 映射（`/relevant` 的 bm25.items[]）。

    **为什么需要**：`/relevant` 的 route 是**逐条目**字段，原先只取名字 ⇒ 自动下发事件
    没有 route，(name, route) 维度的权重表会把「曝光」和「使用」拆成两行，除以 0 次曝光，
    自动下发对分数完全失效。逐名事件必须带上它自己那条的 route。
    同名多路在 `_dedup` 阶段已合并为规范路，这里取首个非空值即可。
    """
    out: Dict[str, str] = {}
    try:
        items: Any = None
        if isinstance(data, dict):
            bm = data.get("bm25")
            items = bm.get("items") if isinstance(bm, dict) else data.get("items")
        if not isinstance(items, list):
            return out
        for it in items:
            if not isinstance(it, dict):
                continue
            nm = str(it.get("name") or "").strip()
            rt = str(it.get("route") or "").strip()
            if nm and rt and nm not in out:
                out[nm] = rt[:_NAME_MAX_CHARS]
    except Exception:  # noqa: BLE001
        return {}
    return out


def _channel_of(request: Any) -> str:
    try:
        h = request.headers.get("x-hub-channel", "")
        return "mcp" if h == "mcp" else "web"
    except Exception:  # noqa: BLE001 —— 桩请求/异常头都按 web 记，埋点不许抛
        return "web"


def _backend_routes(data: Any) -> Dict[str, Any]:
    """从返回载荷泛取逐路健康（memory/kb/skill 三处 backends 形状已确认同构）。

    拿不到就返回空 dict——形状漂移时埋点降级为只记 count，绝不抛。
    """
    if not isinstance(data, dict):
        return {}
    out: Dict[str, Any] = {}
    try:
        backends = data.get("backends") or []
        ok = [b.get("name") for b in backends if isinstance(b, dict) and b.get("ok")]
        out["routes_ok"] = [x for x in ok if x]
        degraded = [b.get("name") for b in backends if isinstance(b, dict) and not b.get("ok")]
        out["degraded"] = [x for x in degraded if x]
        if data.get("count") is not None:
            out["count"] = data.get("count")
    except Exception:  # noqa: BLE001
        return {}
    return out


def _fire(subject: str, status: str, duration_ms: int, detail: Dict[str, Any]) -> None:
    """唯一落库出口。整体包 try：埋点失败绝不打断业务请求，但 print 不静默。"""
    try:
        db.log_profile_event(SOURCE, subject, status, duration_ms, detail=detail)
    except Exception as e:  # noqa: BLE001
        log.warning(f"[runlog] 埋点失败(不影响业务)：{subject} {type(e).__name__}: {e}")


def note(subject: str, detail: Dict[str, Any], status: str = "success",
         duration_ms: int = 0) -> None:
    """**进程内**埋点：给不经 HTTP 的调用方（intake 直接调 Python 实现）。

    为什么需要另一个出口：`track()` 是**端点装饰器**，只包 REST 请求。同一份能力被
    进程内直接调用时（`intake._skill` → `skill_store.acquire`）装饰器根本不执行 ⇒
    动作发生了、账上什么都没有。2026-10-22 实测：接单简报当天 13 份、其中命中
    `tailscale-offline-diagnose` 并回给调用方，而 `skill.invoke` 计数恒 41、
    最近一条停在 10-20 —— 不是查询坏，是**这条链路没有埋点**。

    口径与 `track()` 一致：`detail` 必须自带 `mode`（`skill_weights.classify` 按它
    分桶），落库仍走 `_fire`（唯一出口，失败只打印不抛）。
    """
    _fire(subject, status, int(duration_ms), detail)


def track(subject: str, skip_when=None, mode: str = ""):
    """装饰器：包住三中心只读检索端点，记 q/limit/routes/channel/逐路健康/耗时。

    用装饰器而不是逐点手写：6+1 个端点形状同构（kwargs 进 dict 出），散写必漏；
    漏一个不是「少一条日志」，是「通道盲区」——和 writeauth 做中间件的理由同型。

    `skip_when(data) -> bool`：返回 True 就**不记这条成功事件**。给「语义上是查询
    不是使用」的调用用——`/api/skill/store/acquire` 的 dry_run / allow_install=false
    只是报「将会装什么」，什么都没交付。它原先记成 `skill.invoke` 且 name 为空，
    在调用记录里长成一条**没有技能名的使用流水**，权重账也跟着虚增（2026-10-20 实测）。

    `mode`：端点自己声明的分层（如 `/auto` 的 `auto_invoke`）。传了就不再看
    kwargs 里的 `via`——避免「客户端传个 via 就改了自己的账目口径」。
    """
    def deco(fn):
        @functools.wraps(fn)
        async def wrapper(*args, **kwargs):
            t0 = time.monotonic()
            # FastAPI 以 kwargs 注入 request；但直调/测试可能走位置参数——
            # 两处都找一遍，找到第一个就叫 request（签名里 request 永远是首个参数）。
            request = kwargs.get("request")
            if request is None and args:
                request = args[0]
            _mode = _mode_of(subject, kwargs, mode)
            try:
                data = await fn(*args, **kwargs)
                if skip_when is not None:
                    try:
                        _skip = bool(skip_when(data))
                    except Exception:  # noqa: BLE001 — 判据自己坏了不许带倒业务
                        _skip = False
                    if _skip:
                        return data
                base = {
                    **_backend_routes(data),
                    "q": tdai_client.scrub(_pick_query(kwargs)),
                    "limit": _pick_limit(kwargs),
                    "routes": _pick_routes(kwargs),
                    "channel": _channel_of(request),
                    # v0.13.127：caller/task（仅 invoke 有；其它端点这两键不出现）
                    **_pick_extra(kwargs),
                    # 2026-10-20：自动下发 / 指名调用分层。没有这一列，
                    # 「agent 自己找的技能」与「人点名的技能」在账上长得一样。
                    **({"mode": _mode} if _mode else {}),
                }
                # 来源路以**返回体**兜底：/read 不传 route 时（名字唯一）也要能记下实际那一路；
                # /relevant 的 route 只能从返回体拿（逐条目字段）。
                _rr = _route_of(data)
                if _rr and not base.get("route"):
                    base["route"] = _rr
                _routes = _route_by_name(data)
                if subject == "skill.invoke":
                    # /invoke 的 caller 是自报（body 字段或 x-hub-channel 通道头），
                    # 不是协议取证。响应里写 caller_reported=true，流水也必须同样落——
                    # 否则面板会把自报身份显示成已取证，权重统计的来源就带毒。
                    base["caller_reported"] = True
                names = _pick_names(data)
                # 注入链的候选一旦有名字，就是**真被塞进 agent 上下文**了 ⇒ 改记 skill.inject。
                fire_subject = _subject_for(subject, kwargs, bool(names))
                if not names:
                    _fire(subject, "success", int((time.monotonic() - t0) * 1000), base)
                else:
                    # 一行记一个技能：`skill_usage.counts()` 每行只取 `detail["name"]`，
                    # 塞成数组它会整个str() 成一个怪串、账照样记不上（判例 103）。
                    # first 名重复写进 base.first_name 供人读，完整清单在 names。
                    for i, nm in enumerate(names):
                        ev = {**base, "name": nm, **({"first_name": names[0]} if i == 0 else {}),
                              "name_count": len(names)}
                        # 逐名事件带自己那条的 route（auto 的曝光必须落到 (name, route) 同一行）
                        _rt = _routes.get(nm)
                        if _rt:
                            ev["route"] = _rt
                        _fire(fire_subject, "success", int((time.monotonic() - t0) * 1000), ev)
                return data
            except HTTPException as e:
                # 失败路径同样留痕（查询词 scrub 后截 200），然后原样 re-raise——
                # HTTP 语义（404/409/400 带诊断正文）一字不动。
                _fire(subject, "fail", int((time.monotonic() - t0) * 1000), {
                    "q": tdai_client.scrub(_pick_query(kwargs)),
                    "limit": _pick_limit(kwargs),
                    "routes": _pick_routes(kwargs),
                    "channel": _channel_of(request),
                    # v0.13.127：失败的 invoke（409/404）同样要答「谁调的、想干什么」
                    **_pick_extra(kwargs),
                    **({"mode": _mode} if _mode else {}),
                    **({"caller_reported": True} if subject == "skill.invoke" else {}),
                    "http": e.status_code,
                    "err": tdai_client.scrub(str(e.detail))[:200],
                })
                raise
        return wrapper
    return deco


def _require_read(request: Request) -> None:
    """本模块所有只读端点的共享鉴权（GET 但按写方法判）。

    运行日志含查询词、成本端点含账单 ⇒ 批量读它们＝窥探本机使用史 / 数据外流，
    与 `/api/audit/list`（src/audit.py 先例）同级敏感。fail-closed：服务端没配口令
    ⇒ 503 而不是放行。**抽成函数而不是复制**：两个端点任一漏抄就是一条敞开的通道。
    """
    verdict, reason = writeauth.decide(
        "POST", request.url.path,
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        # 只打 verdict/path/来源，绝不打凭据（与 audit.py 同一口径）
        log.warning(f"[runlog] 拒绝 {verdict}：{request.url.path} "
                    f"来源={request.client.host if request.client else '?'} —— {reason}")
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401, detail=reason)


@router.get("/api/runlog")
async def runlog_query(request: Request,
                       source: Optional[str] = Query(default=None, max_length=40),
                       subject: Optional[str] = Query(default=None, max_length=40),
                       status: Optional[str] = Query(default=None, max_length=20),
                       outcome: Optional[str] = Query(default=None, max_length=20),
                       window: int = Query(default=0, ge=0, le=720),
                       limit: int = Query(default=100, ge=1, le=500),
                       before_id: Optional[int] = Query(default=None, ge=1)):
    """运行日志查询：source/subject/status 过滤 + 时间窗 + id 游标翻页。

    照抄 /api/audit/list 的鉴权先例（GET 但按写方法判）：运行日志含查询词，
    批量读它＝窥探本机使用史，与导出同级敏感。
    """
    _require_read(request)

    sql = ("SELECT id,source,subject,trace_id,status,duration_ms,detail,created_at"
           " FROM profile_events")
    conds, params = [], []

    if source:
        conds.append("source=?")
        params.append(source)
    if subject:
        if subject not in SUBJECTS:
            raise HTTPException(400, f"subject must be one of {list(SUBJECTS)}")
        conds.append("subject=?")
        params.append(subject)
    if status:
        if status not in ("success", "fail"):
            raise HTTPException(400, "status must be success|fail")
        conds.append("status=?")
        params.append(status)
    if outcome:
        # P2 C3：按**跨 source 的语义**筛，而不是让前端记住每家各自的原始词。
        if outcome not in execsem.OUTCOMES:
            raise HTTPException(400, "outcome must be one of %s" % list(execsem.OUTCOMES))
        pairs = [(s, st) for s, m in execsem.STATUS_MAP.items()
                 for st, o in m.items() if o == outcome]
        if not pairs:
            # 该 outcome 当前没有任何映射 ⇒ **空集**，而不是「忽略过滤条件」。
            # （静默忽略会让调用方把「没这类事件」读成「全量」—— 本仓反复禁的就是这个。）
            conds.append("0")
        else:
            conds.append("(" + " OR ".join("(source=? AND status=?)" for _ in pairs) + ")")
            for s, st in pairs:
                params.extend([s, st])
    if window > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()
        conds.append("created_at>=?")
        params.append(cutoff)
    if before_id:
        # append-only 表用 id<? 游标翻页，不用 OFFSET（越翻越慢）
        conds.append("id<?")
        params.append(before_id)
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)

    rows = db.query(sql, tuple(params))
    next_before_id = rows[-1]["id"] if len(rows) == limit else None
    return {
        # C3：补 outcome/mapped（**不改原始 status** —— 那是事实，outcome 是解释）。
        "events": execsem.decorate(rows),
        "count": len(rows),
        "next_before_id": next_before_id,
        # ⚠ 以前这里是**手写死的五个**，而代码已在用 `ctx_manifest`/`resilience` ⇒
        # 回包静默骗人、前端下拉永远筛不到那两类。现在改读词表（唯一真相源）。
        **execsem.vocab(),
        "subjects": list(SUBJECTS),
    }


@router.get("/api/runlog/cost")
async def runlog_cost(request: Request,
                      group_by: str = Query(default="client,model", max_length=80),
                      window: int = Query(default=24, ge=0, le=8760),
                      limit: int = Query(default=100, ge=1, le=500)):
    """单位成功任务成本：CCR usage 账本的**只读**聚合（P0-2）。

    单独一个 path、**不复用 /api/runlog**：数据源不同（那边是 hub 自己的
    `profile_events`，这边是 CCR 的 `usage.sqlite`），混进一个回包会让「日志」与
    「账单」两套语义互相污染（一个走 db.query、一个走只读 URI，连失败模式都不同）。

    `group_by` 只收 `usage_ledger.GROUP_COLS`；`logical_model` **明确拒绝**
    （2026-09-07 判例：账本逻辑名不是客户端/模型 ID，误用致 jcode 不可用）。
    回包带 `attribution`：当前实测 CCR 不透传客户端 `X-Request-Id` ⇒ `approximate`
    （按 client×model×时间窗近似），调用方不得当作精确对齐。

    fail-closed：账本缺失/打不开 ⇒ **503 + ok:false**，绝不回 0 当成功
    （免费模型 cost_usd 就是 NULL，静默 0 = 谎报成本）。
    """
    _require_read(request)

    cols = tuple(c.strip() for c in str(group_by).split(",") if c.strip())
    if not cols:
        raise HTTPException(400, "group_by 不能为空")
    bad = [c for c in cols if c not in usage_ledger.GROUP_COLS]
    if bad:
        raise HTTPException(
            400, f"group_by 只允许 {list(usage_ledger.GROUP_COLS)}，收到 {bad}"
                 "（logical_model 是 CCR 内部逻辑名，不是客户端/模型 ID）")

    since, until = usage_ledger.window_bounds(window)
    data = usage_ledger.summary(group_by=cols, since=since, until=until, limit=limit)
    if not data.get("ok"):
        raise HTTPException(503, f"usage ledger unavailable: {data.get('error')}")
    return data
