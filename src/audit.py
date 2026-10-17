"""资产变更审计的**只读查询**门面。

为什么单独一个模块而不是塞进 db.py：db.py 是最底层（被所有模块 import），放路由进去
会让存储层长出 HTTP 依赖。读写分居两层的口径是——
  写：`db.log_asset_event()` 是唯一入口（append-only，落库前脱敏）；
  读：本模块的 `GET /api/audit/list`。

鉴权口径照抄 `/api/sessions/export`（src/main.py:531）的既有先例：**GET 但按写方法判**。
理由是审计行含 actor 与变更细节，批量读它＝数据外流动作，与导出同级；且服务绑
0.0.0.0:3102，不按写判就等于把变更史对整个局域网敞开。
fail-closed：服务端没配口令 ⇒ 503 而不是放行（与 writeauth.decide 同一口径）。

── P0-4（v0.13.105）研判层 ──────────────────────────────────────────────
本模块除「把行列出来」之外，多了一层**研判**：把 `asset_audit` 每行的 `verdict` 当作
一条**原始事实**，走 `evidence` 的三层链路（原始事实 → 候选信号 → 研判），最后给出
`decisive`。三条口径：
  · `verdict IS NULL`（**未研判**，即迁移前的旧行）**不是通过** —— 它进 `unjudged`，
    同样让 `decisive=False`。这是本仓「迁移不替历史编证据」的直接结果：旧行没人保证过，
    就不许因为「没报错」而当成已确认。
  · `verdict='insufficient'`（判过了、判不了）与 `dismissed`（判过了、否决）**三态分明**，
    汇总里分开计数，不得合并。
  · 判不了就不放行：调用方要拿结果做决策时必须走 `evidence.require_decisive()`，
    而不是看 HTTP 200。本模块自己的 HTTP 回包**一定**带 `decisive` 字段，
    让「200 但不可判定」在返回体里就看得出来。
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

import db
import evidence
import traceview
import writeauth

router = APIRouter()

#: 允许的 asset_type —— 与 db.log_asset_event 各调用点用的字面量一一对应。
#: 写死成白名单而不是放开自由文本：查询侧要能枚举，否则前端做不出下拉。
VALID_TYPES = ("mcp_server", "mcp_acl", "memory_l1", "memory_doc",
               "agent", "session", "setting", "skill", "repo")

_LIMIT_MAX = 1000


def _require_read(request: Request) -> None:
    """本模块只读端点的共享鉴权（GET 但按写方法判）。

    抽成函数而不是复制（与 src/runlog.py 同一理由）：两个端点任一漏抄就是一条敞开的通道。
    """
    verdict, reason = writeauth.decide(
        "POST", request.url.path,                       # 强制按写方法判：批量读审计＝数据外流
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        # 只打 verdict/path/来源，绝不打凭据（与 main.py 的 [export] 日志同一口径）
        print(f"[audit] 拒绝 {verdict}：{request.url.path} "
              f"来源={request.client.host if request.client else '?'} —— {reason}", flush=True)
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401, detail=reason)


@router.get("/api/audit/list")
async def audit_list(request: Request,
                     asset_type: Optional[str] = Query(default=None, max_length=40),
                     asset_slug: Optional[str] = Query(default=None, max_length=200),
                     limit: int = Query(default=100, le=_LIMIT_MAX)):
    _require_read(request)
    if asset_type and asset_type not in VALID_TYPES:
        raise HTTPException(400, f"asset_type must be one of {list(VALID_TYPES)}")

    sql = "SELECT id,asset_type,asset_slug,action,actor,detail,created_at FROM asset_audit"
    conds, params = [], []
    if asset_type:
        conds.append("asset_type=?")
        params.append(asset_type)
    if asset_slug:
        conds.append("asset_slug=?")
        params.append(str(asset_slug))
    if conds:
        sql += " WHERE " + " AND ".join(conds)
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(max(1, min(int(limit or 100), _LIMIT_MAX)))
    rows = db.query(sql, tuple(params))
    return {"audit": rows, "count": len(rows), "types": list(VALID_TYPES)}


# ── P0-4：研判层 ─────────────────────────────────────────────────────────

#: 本模块的研判对象 = 审计行自带的三列。它们是**事实**，不是结论：
#: 行的 `verdict` 由写入方（`db.log_asset_event` 的调用点）给出，口径见该函数 docstring。
_VERDICT_FACT_SOURCE = "asset_audit.verdict"


#: 审计行 `verdict` → 规则答案（True=确认 / False=否决 / None=判不了）。
#: **必须显式给 evaluator**，不能用 `evidence` 的缺省规则：缺省规则是 `bool(value)`，
#: 而这里的事实值是字符串 `"dismissed"`（非空 ⇒ 真值）—— 字符串真值会把「已否决」
#: 读成「已确认」，正是本模块要防的那类静默降级。
_VERDICT_TRUTH = {"confirmed": True, "dismissed": False}


def _verdict_evaluator(signal: evidence.Signal,
                       facts: Dict[str, evidence.Fact]) -> Optional[bool]:
    return _VERDICT_TRUTH.get(str(facts[signal.facts[0]].value))


def adjudicate_rows(rows: List[Dict[str, Any]]) -> Dict[str, Any]:
    """把审计行当**原始事实**过一遍三层链路（纯函数，不碰 db。供端点与闸门共用）。

    `verdict IS NULL` ⇒ 事实值 None ⇒ 规则回 None ⇒ `insufficient`；
    这**不是** bug：未研判的行在研判语义里就是「不可判定」。它们另外计入 `unjudged`，
    所以回包里能分出「从来没判过」和「判过但判不了」。
    """
    facts: List[evidence.Fact] = []
    signals: List[evidence.Signal] = []
    unjudged = 0
    for r in rows:
        key = "audit:%s" % r.get("id")
        v = r.get("verdict")
        if v is None:
            unjudged += 1
        facts.append(evidence.Fact(
            key=key, value=v, source=_VERDICT_FACT_SOURCE,
            at=str(r.get("created_at") or ""), ok=True,
            note="verdict IS NULL ⇒ 未研判（迁移前旧行）" if v is None else ""))
        signals.append(evidence.Signal(
            key=key,
            claim="%s/%s %s 是否已在 hub 侧确实发生"
                  % (r.get("asset_type"), r.get("asset_slug"), r.get("action")),
            facts=(key,),
            severity=str(r.get("severity") or "info"),
            rule="audit.verdict"))
    layered = evidence.layer(facts, signals, evaluator=_verdict_evaluator,
                             unjudged=unjudged)
    layered["rows"] = len(rows)
    return layered


@router.get("/api/audit/verdicts")
async def audit_verdicts(request: Request,
                         window: int = Query(default=0, ge=0, le=8760),
                         sample: int = Query(default=50, ge=0, le=_LIMIT_MAX)):
    """审计台账的**研判汇总**（P0-4）。

    ⚠ 读这个端点时请把 `decisive` 当第一眼：HTTP 200 只说明「查到了」，
    `decisive=false` 说明「查到的东西里有不可判定的」——旧行（未研判）也算。
    它 **不** 参与任何鉴权放行（那是 writeauth 的事），它的作用是给**审计覆盖率/可信度**
    一个可断言的口径，供闸门与人看。
    """
    _require_read(request)

    conds, params = [], []
    if window > 0:
        cutoff = (datetime.now(timezone.utc) - timedelta(hours=window)).isoformat()
        conds.append("created_at>=?")
        params.append(cutoff)
    where = (" WHERE " + " AND ".join(conds)) if conds else ""
    rows = db.query(
        "SELECT id,asset_type,asset_slug,action,severity,verdict,confidence,created_at"
        " FROM asset_audit" + where + " ORDER BY id DESC LIMIT ?",
        tuple(params + [_LIMIT_MAX]))

    layered = adjudicate_rows(rows)
    s = layered["summary"]
    # 四字段结果对象（P0-4）：不给一个「看起来像通过」的布尔值。
    return {
        "exec_status": "ran",
        "validity": "pass" if s["decisive"] else "insufficient",
        "summary": s,
        "evidence": {"rows": layered["rows"], "scanned_limit": _LIMIT_MAX,
                     "since": (params[0] if conds else None)},
        "quality": {"zero_unjudged": s["unjudged"] == 0,
                    "source": _VERDICT_FACT_SOURCE},
        "judgments": layered["judgments"][:max(0, int(sample or 0))],
        "window": window,
    }


# ── P2 A11：端到端 Trace 因果链 ───────────────────────────────────────────

#: trace 链里可能出现的表 —— 与 `traceview._SOURCES` 一一对应（查询侧要能枚举）。
TRACE_TABLES = ("profile_events", "tasks", "asset_audit")


@router.get("/api/audit/trace")
async def audit_trace(request: Request,
                      trace_id: str = Query(..., min_length=1, max_length=120),
                      limit: int = Query(default=500, ge=1, le=2000)):
    """按 `trace_id` 把三张表的节点收成一条链（P2 A11，**只读**）。

    放在本模块（而不是新开一个 router）的理由：`asset_audit` 是链上的三段之一，
    而本模块已是审计的只读门面 —— 新开模块还要改 `main.py` 的 include_router，
    在**多会话并发写 main.py** 的当下（AGENTS.md 文件级单写者）那是不必要的冲突面。

    ⚠ 回包里 **`complete` 不等于成功**：它只说「这条 trace 的节点里没有断链的」。
    一个只含 `fail` 事件的 trace 同样 `complete=true` —— 完整 ≠ 成功。
    ⚠ `audit_trace_available=false` 表示**存量库的 `asset_audit` 还没补 `trace_id` 列**
    （迁移发生在下次 `init_db`）⇒ 审计段**不参与**本链；**不是**「这个 trace 没改过资产」。
    这两种「空」必须分开看，否则会把「能力没上线」读成「事实为空」（判例 193 同族）。
    """
    _require_read(request)
    result = traceview.collect(trace_id, limit=limit)
    # 只回该 trace 的链 + 断链诊断；不返回别的 trace 的行（最小暴露面，与 /list 同口径）。
    return {
        "trace_id": result["trace_id"],
        "chain": result["chain"],
        "count": len(result["chain"]),
        "tables": list(TRACE_TABLES),
        "audit_trace_available": result["audit_trace_available"],
        "complete": result["complete"],
        # `orphans` 按表分桶：喂进来的节点若缺 trace 会在此显形（本端点的入参是**已知** trace，
        # 故正常情况下恒为空集；非空即说明 collect() 的取数口径漏了过滤，是自检信号）。
        "orphans": result["orphans"],
    }
