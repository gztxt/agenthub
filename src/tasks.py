"""任务 DAG 协同引擎（部署方案 S3 的 Python 落地：SQLite 状态机 + asyncio）

对应上游 workflow.rs 的核心语义，砍掉 Redis/petgraph：
- 状态持久化：tasks 表（服务重启不丢，running→启动时标记 failed 由用户重试）
- 拓扑执行：依赖满足即并发派发（每 run 并发上限 4）
- 超时熔断：单任务默认 600s（asyncio.wait_for）+ 后台 sweeper 兜底
- 上下文传递：上游 output 注入下游 prompt（临时记忆区语义，限长防爆炸）
- Leader 拆解：LLM 产出受校验的 JSON DAG（环检测/引用检测/数量与深度限制）
"""
import asyncio
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db
import verifier

router = APIRouter()

TASK_TIMEOUT_S = int(os.getenv("TASK_TIMEOUT_S", "600"))
MAX_CONCURRENCY = 4
MAX_TASKS = 12
UPSTREAM_TRIM = 4000
#: P1-3：截断预算**只算正文**，标记开销另计（标记必须存在，否则下游无法分辨
#: 「上游只写了这么多」与「被我砍掉了」—— 静默截断是最坏形态，见 format_upstream 文档串）。
#: 头尾比例 1:1：结论通常在尾部，整体砍尾丢的正是结论。
UPSTREAM_HEAD = UPSTREAM_TRIM // 2
UPSTREAM_TAIL = UPSTREAM_TRIM - UPSTREAM_HEAD
#: 调度循环步长（秒）。做成常量是为了让 L0 闸门能把它压到毫秒级（不起真等待）。
LOOP_SLEEP_S = float(os.getenv("TASK_LOOP_SLEEP_S", "1.5"))

# ── 状态枚举（P0-3）─────────────────────────────────────────────────
#: 全量状态。`verifying` 是执行者交产物后、验证器判定前的**短暂态**；
#: `waiting_*` / `paused` 是**等待态**（parked）：既不占执行资源，也不算终态。
TASK_STATUSES = ("pending", "running", "verifying", "submitted", "success", "failed", "blocked",
                 "cancelled", "waiting_input", "waiting_approval", "paused")
#: 终态：run 的收敛条件。
TERMINAL_STATUSES = ("success", "failed", "blocked", "cancelled")
#: 等待态：让出执行资源，等外部输入/批准/恢复；run 在此**挂起**而非空转。
PARKED_STATUSES = ("waiting_input", "waiting_approval", "paused")
#: **待接受态（P1-4）**：执行者已交产物**且过了 verifier**，但**尚未被接受**。
#: 手册 B12：把「执行者提交成功」与「审核方接受」分开——提交只是**候选成果**。
#: 资源语义与等待态同类（都已让出执行槽），但**刻意不进 `PARKED_STATUSES`**：
#: 等待态的出口是 `/resume`（→pending 重跑），而 submitted 的出口是 `/accept`→success
#: 或 `/reject`→failed。混进 PARKED_STATUSES 会被 `requeue_waiting` 误置回 pending。
SUBMITTED_STATUSES = ("submitted",)
#: 过了 verifier 后是否**自动接受**（= P1-4 之前的行为，默认开）。
#: 置 0 则任务停在 `submitted` 等外部 `POST /accept`。
#: 做成**模块级常量**（不是每次读 env）：与 MAX_CONCURRENCY/LOOP_SLEEP_S 同口径，测试可整体替换。
AUTO_ACCEPT = os.getenv("TASK_AUTO_ACCEPT", "1").strip().lower() not in ("0", "false", "no", "off")

_ctx: Dict[str, Any] = {}
_runners: Dict[str, asyncio.Task] = {}


def set_context(chat_fn=None, agent_ids_fn=None):
    if chat_fn:
        _ctx["chat_fn"] = chat_fn
    if agent_ids_fn:
        _ctx["agent_ids_fn"] = agent_ids_fn


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


#: §7-② 等待自愈：等待态的**默认期限**（秒）与**最大唤醒次数**。
#: 到期限未等来输入 ⇒ 自动重新排队（自愈）；唤醒次数到顶 ⇒ 如实 failed（不再永久悬空）。
WAIT_RESUME_S = int(os.getenv("TASK_WAIT_RESUME_S", "300"))
MAX_WAKES = int(os.getenv("TASK_MAX_WAKES", "3"))


#: P0-3 新增列（幂等 ALTER 补建）。
_TASK_COLUMNS = (
    ("verify_kind", "TEXT NOT NULL DEFAULT 'nonempty'"),  # 产物由哪个验证 kind 判
    ("verify_expect", "TEXT"),                            # contains 类判据（写在任务定义里，不由执行者给）
    ("budget_ms", "INTEGER"),                              # 单任务耗时预算（毫秒）
    ("budget_tokens", "INTEGER"),                          # 单任务 token 预算
    ("spent_ms", "INTEGER"),                               # 实测耗时
    ("spent_tokens", "INTEGER"),                           # 实测 token
    # P0-a：实发模型（取 adapter 回包的 data.model，不是我们请求的值 —— 上游可能改写）
    ("model", "TEXT"),
    # P0-b：验收裁决。三态照 verifier.STATUS_*；NULL = 未走到验收这一步。
    ("verdict", "TEXT"),
    ("verify_reason", "TEXT"),
    # §7-①：让「正在验」可被外部读到并**可度量**（旧实现是同一协程内的瞬时态）
    ("verify_started_at", "TEXT"),
    ("verify_ms", "INTEGER"),
    # §7-②：等待态的期限与唤醒计数（无此二列则「等多久」不可知、不可自愈）
    ("wait_until", "TEXT"),
    ("wake_count", "INTEGER"),
    # P1-4：提交/接受分离。submitted_at = 产物过 verifier 的时刻；
    # accepted_at/accepted_by = 被接受的时刻与主体（'auto' = 自动接受）。
    ("submitted_at", "TEXT"),
    ("accepted_at", "TEXT"),
    ("accepted_by", "TEXT"),
)


def budget_exceeded(row: dict, spent_ms: Optional[int], spent_tokens: int = 0) -> Optional[str]:
    """任务级预算闸（纯函数，便于单测）：返回超限原因，未超返回 None。

    预算为 None = **不设限**；预算与实测同时存在才比较（判不了就不拦 —— 与 P0-4 同口径，
    但这里“不拦”是设计选择，因为预算是成本护栏、不是正确性判据）。
    """
    bm = row.get("budget_ms")
    if bm is not None and spent_ms is not None and spent_ms > bm:
        return "耗时 %sms 超预算 %sms" % (spent_ms, bm)
    bt = row.get("budget_tokens")
    if bt is not None and spent_tokens is not None and spent_tokens > bt:
        return "token %s 超预算 %s" % (spent_tokens, bt)
    return None


# ── §7-③ run 级预算 ────────────────────────────────────────────────────
def set_run_budget(run_id: str, budget_ms: Optional[int] = None,
                   budget_tokens: Optional[int] = None) -> None:
    """设/改 run 级预算（**upsert**：一个 run 只一行）。None = 该项不设限。"""
    db.execute("INSERT INTO task_run_budgets(run_id,budget_ms,budget_tokens,created_at)"
               " VALUES(?,?,?,?)"
               " ON CONFLICT(run_id) DO UPDATE SET budget_ms=excluded.budget_ms,"
               " budget_tokens=excluded.budget_tokens",
               (run_id, budget_ms, budget_tokens, _now()))


def run_spent(run_id: str) -> dict:
    """run 累计消耗 = 各任务 `spent_ms` / `spent_tokens` 之和（未跑的不计）。"""
    r = db.query("SELECT COALESCE(SUM(spent_ms),0) ms, COALESCE(SUM(spent_tokens),0) tk"
                 " FROM tasks WHERE run_id=?", (run_id,))[0]
    return {"ms": int(r["ms"] or 0), "tokens": int(r["tk"] or 0)}


def run_budget_exceeded(run_id: str) -> Optional[str]:
    """run 级累计超限原因；**未设预算 ⇒ None**（不设限，不是「超了」）。

    与任务级同一口径：只有「预算与实测同时存在」才比较 ⇒ 判不了就不拦。
    """
    rows = db.query("SELECT * FROM task_run_budgets WHERE run_id=?", (run_id,))
    if not rows:
        return None
    b, s = rows[0], run_spent(run_id)
    bm, bt = b.get("budget_ms"), b.get("budget_tokens")
    if bm is not None and s["ms"] > bm:
        return "耗时 %sms 超 run 预算 %sms" % (s["ms"], bm)
    if bt is not None and s["tokens"] > bt:
        return "token %s 超 run 预算 %s" % (s["tokens"], bt)
    return None


def format_upstream(task_id: str, text: str) -> str:
    """上游产物注入下游 prompt 时的**交接块**（P1-3，纯函数）。

    原实现是 `upstream[d][:UPSTREAM_TRIM]` —— **静默整体截断**：不留标记、不保尾部。
    那个形态最坏，有两个独立的原因：

    1. **下游无从分辨**「上游本来就只写了 4000 字」与「上游写了 8000 字被我砍到 4000」。
       两种情况的 prompt 长得一模一样，下游只能猜；猜错就是**基于缺失内容下结论**。
    2. **事后不可复盘**。任务表里不留「这条被截了多少」，出了错只能回放原始 DB 才知道。

    所以截断必须**自带可断言的标记**：原始长度 + 省略量，且**保留头尾**
    （结论通常在尾部，砍尾丢的正是结论）。

    短文本（≤ `UPSTREAM_TRIM`）**逐字节原样返回，不加任何标记** —— 绝大多数任务走这条路，
    给它们套一层壳纯属噪声（这也是最容易写坏的地方，见 tests 的红向自证）。

    超长时返回四段：`头` + 省略标记（含原始总长与省略量）+ `尾` + 任务标题。
    正文字符数**恒 ≤ `UPSTREAM_TRIM`**；标记与标题是固定开销，不占正文预算。
    """
    body = text or ""
    if len(body) <= UPSTREAM_TRIM:
        return "—— 任务 %s 的结果 ——\n%s" % (task_id, body)
    omitted = len(body) - UPSTREAM_TRIM
    return ("—— 任务 %s 的结果 ——（原文 %d 字符，已省略中间 %d 字符，"
            "保留头 %d + 尾 %d）——\n%s\n……（以上省略 %d 字符）……\n%s"
            % (task_id, len(body), omitted, UPSTREAM_HEAD, UPSTREAM_TAIL,
               body[:UPSTREAM_HEAD], omitted, body[-UPSTREAM_TAIL:]))


def can_accept(status: str) -> bool:
    """纯函数（P1-4）：该状态可否被接受。**只有 `submitted` 可** ——
    `success` 已经接受过（重复接受应是 no-op，不该被算成一次接受）；
    其余状态（pending/running/failed/…）根本没有可接受的候选产物。"""
    return status in SUBMITTED_STATUSES


SCHEMA_TASKS = """
CREATE TABLE IF NOT EXISTS tasks (
    run_id TEXT NOT NULL,
    task_id TEXT NOT NULL,
    trace_id TEXT,
    goal TEXT,
    prompt TEXT NOT NULL,
    agent_id TEXT,
    deps TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending',  -- TASK_STATUSES（P0-3 起含等待态/verifying）
    output TEXT,
    error TEXT,
    verify_kind TEXT NOT NULL DEFAULT 'nonempty',  -- 产物由哪个验证器判（P0-3）
    -- P0-a：把「任务 → Agent → 模型 → token」变成可 JOIN 的记录。
    -- 数据本来就在 adapter 返回里（openai_compat.py 返回 model+usage），此前被丢弃。
    model TEXT,                                  -- 实发模型（上游回包，非我方请求值）
    -- P0-b：验收裁决落库。verdict 三态照 verifier.STATUS_*，
    -- NULL = 未走到验收（等待/预算/自称失败/超时）—— 与 'fail' 语义不同，不可混同。
    verdict TEXT,
    verify_reason TEXT,
    -- P1-4：提交≠接受。submitted_at 在过 verifier 时写；accepted_at/by 在被接受时写。
    submitted_at TEXT,
    accepted_at TEXT,
    accepted_by TEXT,
    started_at TEXT,
    finished_at TEXT,
    duration_ms INTEGER,
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id)
);
CREATE INDEX IF NOT EXISTS idx_tasks_run ON tasks(run_id, status);
-- §7-③：run 级预算（一个 run 一行；不设的行不存在 ⇒ 不设限）
CREATE TABLE IF NOT EXISTS task_run_budgets (
    run_id TEXT PRIMARY KEY,
    budget_ms INTEGER,
    budget_tokens INTEGER,
    created_at TEXT NOT NULL
);
"""


def ensure_schema():
    db.execute_script(SCHEMA_TASKS)
    # P0-3：幂等 ALTER 补建新列（存量库不动既有行；旧行 verify_kind 取 DEFAULT、预算 NULL）。
    have = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
    for col, decl in _TASK_COLUMNS:
        if col not in have:
            db.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (col, decl))


class DecomposeIn(BaseModel):
    goal: str = Field(min_length=4, max_length=4000)
    default_agent: Optional[str] = None
    auto_run: bool = False
    # P0-3：产物验证 kind + 单任务预算（执行者只能产出，不能自定判据）。
    verify_kind: str = "nonempty"
    budget_ms: Optional[int] = None
    budget_tokens: Optional[int] = None


class RetryIn(BaseModel):
    task_id: Optional[str] = None


class AcceptIn(BaseModel):
    """P1-4：接受已提交的产物。`task_id` 缺省 = 接受该 run 下**全部** submitted。"""
    task_id: Optional[str] = None
    by: Optional[str] = Field(default=None, max_length=64)


class RejectIn(BaseModel):
    """P1-4：驳回已提交的产物（→ failed，下游按「上游失败」阻塞）。"""
    task_id: Optional[str] = None
    reason: Optional[str] = Field(default=None, max_length=300)


LEADER_PROMPT = """你是任务编排器。把用户目标拆解为子任务 DAG，只输出 JSON 数组（不要围栏、不要解释）。
每项格式: {"id":"t1","task":"中文指令（自包含，可独立执行）","agent_id":"执行Agent","deps":[]}
规则：
- agent_id 只能从这里选：{agents}（选最合适的；不确定用 {default}）
- 子任务数 ≤ {maxn}；无依赖的兄弟任务会被并行执行，有依赖写 deps
- 聚合/汇总类任务必须 deps 列出全部上游
"""


def _extract_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?|```", "", text)
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e <= s:
        raise ValueError("LLM 未返回 JSON 数组")
    return json.loads(text[s:e + 1])


def _validate_dag(items: list, known_agents: set, default_agent: str) -> List[dict]:
    if not (1 <= len(items) <= MAX_TASKS):
        raise HTTPException(400, f"子任务数须在 1-{MAX_TASKS}，得到 {len(items)}")
    normalized, ids = [], set()
    for i, it in enumerate(items):
        tid = str(it.get("id") or f"t{i+1}")
        if tid in ids:
            raise HTTPException(400, f"任务 id 重复: {tid}")
        ids.add(tid)
        agent = it.get("agent_id") or default_agent
        if agent not in known_agents:
            agent = default_agent if default_agent in known_agents else "jcode"
        normalized.append({"id": tid, "task": str(it.get("task") or it.get("prompt") or "").strip(),
                           "agent_id": agent, "deps": [str(d) for d in (it.get("deps") or [])]})
    for n in normalized:
        for d in n["deps"]:
            if d not in ids:
                raise HTTPException(400, f"依赖不存在的任务: {n['id']} -> {d}")
    # Kahn 环检测
    indeg = {n["id"]: len(n["deps"]) for n in normalized}
    children: Dict[str, list] = {n["id"]: [] for n in normalized}
    for n in normalized:
        for d in n["deps"]:
            children[d].append(n["id"])
    queue = [k for k, v in indeg.items() if v == 0]
    visited = 0
    while queue:
        cur = queue.pop()
        visited += 1
        for ch in children[cur]:
            indeg[ch] -= 1
            if indeg[ch] == 0:
                queue.append(ch)
    if visited != len(normalized):
        raise HTTPException(400, "检测到循环依赖，已拒绝该 DAG")
    return normalized


@router.post("/api/tasks/decompose")
async def decompose(body: DecomposeIn):
    import llm
    if body.verify_kind not in verifier.known_kinds():
        raise HTTPException(400, "未知 verify_kind: %s（可用 %s）"
                            % (body.verify_kind, ", ".join(verifier.known_kinds())))
    if not llm.configured():
        raise HTTPException(503, "LLM 未配置，无法拆解")
    known = set()
    if _ctx.get("agent_ids_fn"):
        known = set(_ctx["agent_ids_fn"]())
    default = body.default_agent or ("jcode" if "jcode" in known else (sorted(known)[0] if known else "jcode"))
    messages = [{"role": "system", "content": (
        LEADER_PROMPT
        .replace("{agents}", ", ".join(sorted(known)) or "jcode")
        .replace("{default}", default)
        .replace("{maxn}", str(MAX_TASKS)))},
        {"role": "user", "content": body.goal}]
    try:
        answer, _ = await llm.chat_tools_loop(messages, [], None, max_rounds=1)
        items = _validate_dag(_extract_json_array(answer), known, default)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Leader 拆解失败: {e}")
    run_id = uuid.uuid4().hex[:10]
    now = _now()
    for it in items:
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,budget_ms,budget_tokens,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
            (run_id, it["id"], run_id, body.goal, it["task"], it["agent_id"],
             json.dumps(it["deps"]), body.verify_kind, body.budget_ms,
             body.budget_tokens, now, now))
    if body.auto_run:
        _start_runner(run_id)
    return {"run_id": run_id, "goal": body.goal, "tasks": items,
            "running": body.auto_run}


@router.get("/api/tasks/runs")
async def list_runs(limit: int = Query(default=20, le=100)):
    rows = db.query("""
        SELECT run_id, MIN(trace_id) trace_id, MAX(goal) goal, COUNT(*) total,
               SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) success,
               SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,
               SUM(CASE WHEN status='running' THEN 1 ELSE 0 END) running,
               SUM(CASE WHEN status='pending' THEN 1 ELSE 0 END) pending,
               SUM(CASE WHEN status IN ('waiting_input','waiting_approval','paused')
                        THEN 1 ELSE 0 END) waiting,
               SUM(CASE WHEN status='submitted' THEN 1 ELSE 0 END) submitted,
               MIN(created_at) created_at, MAX(finished_at) finished_at
        FROM tasks GROUP BY run_id ORDER BY created_at DESC LIMIT ?""", (limit,))
    for r in rows:
        r["state"] = ("running" if r["running"] else
                      "success" if r["success"] == r["total"] else
                      "awaiting_accept" if r["submitted"] else
                      "waiting" if r["waiting"] else
                      "failed" if r["failed"] and r["pending"] == r["running"] == 0 else
                      "pending" if r["pending"] == r["total"] else "partial")
    return {"runs": rows}


@router.get("/api/tasks/{run_id}")
async def get_run(run_id: str):
    rows = db.query("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at, task_id", (run_id,))
    if not rows:
        raise HTTPException(404, "run not found")
    for r in rows:
        r["deps"] = json.loads(r["deps"] or "[]")
    return {"run_id": run_id, "goal": rows[0]["goal"], "tasks": rows}


@router.post("/api/tasks/{run_id}/start")
async def start_run(run_id: str):
    n = db.query("SELECT COUNT(*) c FROM tasks WHERE run_id=?", (run_id,))[0]["c"]
    if not n:
        raise HTTPException(404, "run not found")
    _start_runner(run_id)
    return {"run_id": run_id, "status": "scheduled"}


@router.post("/api/tasks/{run_id}/retry")
async def retry_run(run_id: str, body: RetryIn):
    if body.task_id:
        db.execute("UPDATE tasks SET status='pending',error=NULL,updated_at=? "
                   "WHERE run_id=? AND task_id=?", (_now(), run_id, body.task_id))
    else:
        db.execute("UPDATE tasks SET status='pending',error=NULL,updated_at=? "
                   "WHERE run_id=? AND status IN ('failed','blocked')", (_now(), run_id))
    _start_runner(run_id)
    return {"run_id": run_id, "status": "retry-scheduled"}


@router.post("/api/tasks/{run_id}/accept")
async def accept_run(run_id: str, body: AcceptIn = AcceptIn()):
    """P1-4：接受已提交的产物（`submitted` → `success`）。

    **只有 `submitted` 接受**：重复接受是 no-op（不篡改已 success 的行，
    也不把 failed/running 洗成成功）。接受后重启 runner，让下游就绪。
    """
    now = _now()
    who = (body.by or "user")[:64]
    sql = ("UPDATE tasks SET status='success',accepted_at=?,accepted_by=?,"
           "finished_at=?,updated_at=? WHERE run_id=? AND status='submitted'")
    args = [now, who, now, now, run_id]
    if body.task_id:
        sql += " AND task_id=?"
        args.append(body.task_id)
    n = db.execute(sql, tuple(args))
    if not n:
        raise HTTPException(404, "no submitted task to accept")
    _start_runner(run_id)
    return {"run_id": run_id, "accepted": n, "by": who}


@router.post("/api/tasks/{run_id}/reject")
async def reject_run(run_id: str, body: RejectIn = RejectIn()):
    """P1-4：驳回已提交的产物（`submitted` → `failed`）。

    驳回后下游按「上游失败」阻塞（`_execute_run` 的既有判据），而不是默默带着
    不合格产物继续跑。"""
    now = _now()
    why = ("驳回: " + (body.reason or "未说明"))[:500]
    sql = ("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=? "
           "WHERE run_id=? AND status='submitted'")
    args = [why, now, now, run_id]
    if body.task_id:
        sql += " AND task_id=?"
        args.append(body.task_id)
    n = db.execute(sql, tuple(args))
    if not n:
        raise HTTPException(404, "no submitted task to reject")
    _start_runner(run_id)
    return {"run_id": run_id, "rejected": n}


def requeue_waiting(run_id: str) -> int:
    """等待态（waiting_*/paused）→ pending，返回改动行数（P0-3）。

    等待态让出了执行资源，run 已挂起；恢复必须**显式**把任务重新排队，
    再重启 runner（由调用方 `_start_runner`）。
    §7-②：人工唤醒**也计数**（`wake_count+1`）并清掉旧期限 —— 否则可被无限人工续命，
    「有界」就名存实亡。
    """
    now = _now()
    ph = ",".join("?" * len(PARKED_STATUSES))
    return db.execute(
        "UPDATE tasks SET status='pending',error=NULL,wait_until=NULL,"
        "wake_count=COALESCE(wake_count,0)+1,updated_at=? "
        "WHERE run_id=? AND status IN (%s)" % ph,
        (now, run_id, *PARKED_STATUSES))


def resume_expired_waits(now: Optional[str] = None) -> int:
    """§7-② **自愈**：把过期的等待态推到下一步。返回被处理的行数。

    · 未到唤醒上限 ⇒ 重新排队（pending）+ 拉起 runner（自愈，不靠人记得调 `/resume`）；
    · 到上限 ⇒ `failed`（`等待超时（已唤醒 N 次仍无输入）`）—— 「等不来」如实失败，
      而不是无限期地当一个看似正常的中间态。
    """
    now = now or _now()
    ph = ",".join("?" * len(PARKED_STATUSES))
    rows = db.query("SELECT run_id, task_id, wake_count FROM tasks"
                    " WHERE status IN (%s) AND wait_until IS NOT NULL AND wait_until <= ?" % ph,
                    (*PARKED_STATUSES, now))
    if not rows:
        return 0
    resumed_runs = set()
    for r in rows:
        if int(r["wake_count"] or 0) >= MAX_WAKES:
            db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       ("等待超时（已唤醒 %d 次仍无输入）" % MAX_WAKES, now, now,
                        r["run_id"], r["task_id"]))
        else:
            db.execute("UPDATE tasks SET status='pending',error=NULL,wait_until=NULL,"
                       "wake_count=COALESCE(wake_count,0)+1,updated_at=?"
                       " WHERE run_id=? AND task_id=?", (now, r["run_id"], r["task_id"]))
            resumed_runs.add(r["run_id"])
    for rid in resumed_runs:
        _start_runner(rid)
    return len(rows)


@router.post("/api/tasks/{run_id}/resume")
async def resume_run(run_id: str):
    """等待态 → pending 并重启 runner（人在外部输入/批准后调）。"""
    n = requeue_waiting(run_id)
    if not n:
        raise HTTPException(404, "no waiting task for run")
    _start_runner(run_id)
    return {"run_id": run_id, "status": "resumed", "requeued": n}


@router.post("/api/tasks/{run_id}/pause")
async def pause_run(run_id: str):
    """把可停的任务（pending / 等待态）置 paused。**不动正在执行的任务**。"""
    now = _now()
    n = db.execute(
        "UPDATE tasks SET status='paused',updated_at=? "
        "WHERE run_id=? AND status IN ('pending','waiting_input','waiting_approval')",
        (now, run_id))
    if not n:
        raise HTTPException(404, "no pausable task for run")
    return {"run_id": run_id, "status": "paused", "paused": n}


@router.post("/api/tasks/{run_id}/cancel")
async def cancel_run(run_id: str):
    """取消整条 run：停 runner + 非终态任务→cancelled（终态）。"""
    if run_id in _runners:
        _runners[run_id].cancel()
        _runners.pop(run_id)
    now = _now()
    n = db.execute(
        "UPDATE tasks SET status='cancelled',finished_at=?,updated_at=? "
        "WHERE run_id=? AND status NOT IN ('success','failed','blocked','cancelled')",
        (now, now, run_id))
    if not n:
        raise HTTPException(404, "no cancellable task for run")
    return {"run_id": run_id, "status": "cancelled", "cancelled": n}


@router.delete("/api/tasks/{run_id}")
async def delete_run(run_id: str):
    if run_id in _runners:
        _runners[run_id].cancel()
        _runners.pop(run_id)
    n = db.execute("DELETE FROM tasks WHERE run_id=?", (run_id,))
    if not n:
        raise HTTPException(404, "run not found")
    return {"status": "deleted", "run_id": run_id}


def _start_runner(run_id: str):
    old = _runners.get(run_id)
    if old and not old.done():
        return  # 已在跑
    t = asyncio.create_task(_execute_run(run_id))
    _runners[run_id] = t
    t.add_done_callback(lambda _f: _runners.pop(run_id, None))


async def _run_task(run_id: str, row: dict):
    chat_fn = _ctx.get("chat_fn")
    if not chat_fn:
        db.execute("UPDATE tasks SET status='failed',error='chat 引擎未就绪',updated_at=? "
                   "WHERE run_id=? AND task_id=?", (_now(), run_id, row["task_id"]))
        return
    # 上下文传递：上游 success 输出注入
    ups = db.query("SELECT task_id,output FROM tasks WHERE run_id=? AND status='success'",
                   (run_id,))
    upstream = {u["task_id"]: u["output"] or "" for u in ups}
    prompt = row["prompt"]
    dep_outputs = []
    for d in json.loads(row["deps"] or "[]"):
        if d in upstream:
            dep_outputs.append(format_upstream(d, upstream[d]))
    if dep_outputs:
        prompt = f"总目标：{row['goal']}\n\n你的子任务：{prompt}\n\n" + "\n\n".join(dep_outputs)
    t0 = asyncio.get_event_loop().time()
    try:
        result = await asyncio.wait_for(
            chat_fn(row["agent_id"], prompt,
                    f"task-{row['trace_id']}-{row['task_id']}"),
            timeout=TASK_TIMEOUT_S)
        dur = int((asyncio.get_event_loop().time() - t0) * 1000)
        spent_tokens = int(((result.get("usage") or {}).get("total_tokens")) or 0)
        # P0-a：实发模型。取 **adapter 回包** 的 model 而非请求值 —— 上游可能改写，
        # 记请求值会把「我们以为用了谁」当成「实际用了谁」，正好毁掉归因的意义。
        # 回包缺 model 时留 NULL，不拿请求值兜底（那是猜测，不是观测）。
        used_model = result.get("model") or None
        # ① 等待态：执行者说「卡在等人/等批」⇒ 让出执行资源，不占槽、不算终态。
        waiting = result.get("waiting")
        if waiting in PARKED_STATUSES:
            # §7-②：等待**必须有期限**（旧实现不写期限 ⇒ 无人叫醒就永久悬空）
            deadline = (datetime.now(timezone.utc)
                        + timedelta(seconds=WAIT_RESUME_S)).isoformat()
            db.execute("UPDATE tasks SET status=?,spent_ms=?,spent_tokens=?,model=?,wait_until=?,"
                       "wake_count=COALESCE(wake_count,0),updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       (waiting, dur, spent_tokens, used_model, deadline, _now(),
                        run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"], "parked", dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "waiting": waiting})
            return
        # ② 预算：超限一律 failed（不静默放过）。
        over = budget_exceeded(row, dur, spent_tokens)
        if over:
            db.execute("UPDATE tasks SET status='failed',error=?,spent_ms=?,spent_tokens=?,"
                       "model=?,finished_at=?,updated_at=? WHERE run_id=? AND task_id=?",
                       ("预算耗尽: " + over, dur, spent_tokens, used_model,
                        _now(), _now(), run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"], "fail", dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "budget": over})
            return
        # ③ 自称失败：**失败方向仍保守**（不因产物非空就升级），但必须过一遍独立复核并留痕。
        #    §7-④：执行者不得自证 success（④），也**不得自证 fail**（本支）——旧实现直接 failed、
        #    不看产物、不留结论 ⇒「其实产物合格」被静默掩盖、事后不可复核。
        if not result.get("success"):
            err = (result.get("error") or "unknown")[:500]
            kind = row.get("verify_kind") or verifier.DEFAULT_KIND
            # §7-①：复核走**线程**（`to_thread`）而非就地同步调用 —— 否则协程从不 yield，
            # 置下的 `verifying` 在 DB 上没人读得到。
            db.execute("UPDATE tasks SET status='verifying',verify_started_at=?,updated_at=?"
                       " WHERE run_id=? AND task_id=?", (_now(), _now(), run_id, row["task_id"]))
            v0 = asyncio.get_event_loop().time()
            verdict = await asyncio.to_thread(verifier.verify, kind,
                                              output=result.get("response"),
                                              expect=row.get("verify_expect"))
            verify_ms = int((asyncio.get_event_loop().time() - v0) * 1000)
            status_v = verdict["status"]
            if status_v == verifier.STATUS_PASS:
                reason = ("executor_claimed_failure_but_output_verified: %s" % err)[:500]
                event = "mismatch"
            else:
                gaps = "; ".join("%s: %s" % (g["id"], g["detail"]) for g in verdict["gaps"])
                reason = ("executor_claimed_failure; verifier %s (%s): %s"
                          % (status_v, kind, gaps))[:500]
                event = "fail"
            # ⚠ 口径冲突处理（2026-10-16）：P0-b 的契约是「**没过 verifier（含自述失败）的路径
            #   `verdict` 必须 NULL**」，而 P0-b 闸门 `test_verdict_null_on_self_reported_failure`
            #   钉着这一条。所以本条修复**不写 `verdict`**（保持 NULL），复核结论落 `verify_reason`
            #   + `mismatch` 事件 —— 目的（**不再静默**）达到了，且不破别人的契约。
            db.execute("UPDATE tasks SET status='failed',error=?,verify_reason=?,"
                       "verify_ms=?,"
                       "duration_ms=?,spent_ms=?,spent_tokens=?,model=?,finished_at=?,updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       (err, reason, verify_ms, dur, dur, spent_tokens, used_model,
                        _now(), _now(), run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"], event, dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "verify": kind,
                                         "verify_status": status_v, "claimed_failure": True})
            return
        # ④ **执行者不自证**：先 verifying，再由 verifier 独立判产物（P0-3 主判据）。
        kind = row.get("verify_kind") or verifier.DEFAULT_KIND
        db.execute("UPDATE tasks SET status='verifying',spent_ms=?,spent_tokens=?,updated_at=?,"
                   "verify_started_at=? WHERE run_id=? AND task_id=?",
                   (dur, spent_tokens, _now(), _now(), run_id, row["task_id"]))
        v0 = asyncio.get_event_loop().time()
        # §7-①：走线程 ⇒ 协程在此 yield，外部读得到 `verifying`
        verdict = await asyncio.to_thread(verifier.verify, kind,
                                          output=result.get("response"),
                                          expect=row.get("verify_expect"))
        verify_ms = int((asyncio.get_event_loop().time() - v0) * 1000)
        if verdict["status"] == verifier.STATUS_PASS:
            # P1-4：**产物过 verifier ≠ 被接受**。AUTO_ACCEPT 开（默认）则立刻接受（＝旧行为），
            # 关则停在 `submitted` 等外部 `POST /accept`。两条路都写 submitted_at，
            # 于是「提交→接受」的时延在两种模式下都可度量。
            out = str(result.get("response") or "")[:20000]
            if AUTO_ACCEPT:
                db.execute("UPDATE tasks SET status='success',output=?,duration_ms=?,spent_ms=?,"
                           "spent_tokens=?,model=?,verdict=?,verify_reason=?,verify_ms=?,"
                           "submitted_at=?,accepted_at=?,accepted_by=?,finished_at=?,updated_at=?"
                           " WHERE run_id=? AND task_id=?",
                           (out, dur, dur, spent_tokens, used_model, verdict["status"], None,
                            verify_ms, _now(), _now(), "auto", _now(), _now(), run_id,
                            row["task_id"]))
            else:
                db.execute("UPDATE tasks SET status='submitted',output=?,duration_ms=?,spent_ms=?,"
                           "spent_tokens=?,model=?,verdict=?,verify_reason=?,verify_ms=?,"
                           "submitted_at=?,updated_at=?"
                           " WHERE run_id=? AND task_id=?",
                           (out, dur, dur, spent_tokens, used_model, verdict["status"], None,
                            verify_ms, _now(), _now(), run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"],
                                 "success" if AUTO_ACCEPT else "submitted", dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "verify": kind,
                                         "model": used_model, "auto_accept": AUTO_ACCEPT})
        else:
            gaps = "; ".join("%s: %s" % (g["id"], g["detail"]) for g in verdict["gaps"])
            err = ("verifier %s (%s): %s" % (verdict["status"], kind, gaps))[:500]
            db.execute("UPDATE tasks SET status='failed',error=?,duration_ms=?,spent_ms=?,"
                       "spent_tokens=?,model=?,verdict=?,verify_reason=?,"
                       "finished_at=?,updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       (err, dur, dur, spent_tokens, used_model, verdict["status"], gaps[:500],
                        _now(), _now(), run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"], "fail", dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "verify": kind,
                                         "verify_status": verdict["status"],
                                         "model": used_model})
    except asyncio.TimeoutError:
        # 2026-10-15 P0-a 取证时发现既存缺陷（非本次引入）：原句把 `'超时熔断 %ds'`
        # 写成字面量却按 5 个参数传（SQL 只有 4 个占位符）⇒ sqlite3.ProgrammingError
        # 在**超时这个异常处理器内部**抛出，任务永远停在 running，且异常冒泡进
        # `_execute_run`。改成正常绑定，顺带把 model 一起记上。
        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   ("超时熔断 %ds" % TASK_TIMEOUT_S, _now(), _now(),
                    run_id, row["task_id"]))
        db.log_profile_event("task_exec", row["agent_id"], "fail", TASK_TIMEOUT_S * 1000,
                             trace_id=row["trace_id"], detail={"task": row["task_id"], "timeout": True})
    except Exception as e:  # noqa: BLE001
        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   (repr(e)[:500], _now(), _now(), run_id, row["task_id"]))


async def _execute_run(run_id: str):
    sem = asyncio.Semaphore(MAX_CONCURRENCY)
    live: Dict[str, asyncio.Task] = {}
    try:
        while True:
            rows = db.query("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at", (run_id,))
            status = {r["task_id"]: r["status"] for r in rows}
            finished = all(s in TERMINAL_STATUSES for s in status.values())
            if finished:
                return
            for r in rows:
                if r["task_id"] in live or r["status"] != "pending":
                    continue
                deps = json.loads(r["deps"] or "[]")
                if any(status.get(d) in ("failed", "blocked", "cancelled") for d in deps):
                    db.execute("UPDATE tasks SET status='blocked',error='上游失败',finished_at=?,"
                               "updated_at=? WHERE run_id=? AND task_id=?",
                               (_now(), _now(), run_id, r["task_id"]))
                    continue
                if all(status.get(d) == "success" for d in deps):
                    # §7-③ run 级预算：在**派发前**查累计消耗（事后算 = 已经花完了）。
                    # 放内层（逐 pending）而非整轮 continue：不为预算引入新的循环出口，
                    # 避免与 parked/submitted 的挂起逻辑打架。
                    r_over = run_budget_exceeded(run_id)
                    if r_over:
                        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,"
                                   "updated_at=? WHERE run_id=? AND task_id=?",
                                   ("run 预算耗尽: " + r_over, _now(), _now(),
                                    run_id, r["task_id"]))
                        db.log_profile_event("task_exec", r.get("agent_id") or "-", "fail", None,
                                             trace_id=r.get("trace_id"),
                                             detail={"task": r["task_id"],
                                                     "run_budget": r_over})
                        continue
                    db.execute("UPDATE tasks SET status='running',started_at=?,updated_at=?"
                               " WHERE run_id=? AND task_id=?",
                               (_now(), _now(), run_id, r["task_id"]))

                    async def guarded(row=r):
                        async with sem:
                            await _run_task(run_id, row)
                    live[r["task_id"]] = asyncio.create_task(guarded())
            if not live:
                parked = [t for t, s in status.items() if s in PARKED_STATUSES]
                awaiting = [t for t, s in status.items() if s in SUBMITTED_STATUSES]
                pend = [t for t, s in status.items() if s == "pending"]
                if parked or awaiting:
                    # 整条 run 挂起：等外部输入/批准/恢复（parked），或等 `/accept`（submitted）
                    # ⇒ 释放执行资源，不空转、也不算死锁。
                    return
                if pend:
                    db.execute("UPDATE tasks SET status='failed',error='无法调度(疑似悬挂)',"
                               "updated_at=? WHERE run_id=? AND status='pending'", (_now(), run_id))
                    return
                return
            done = [tid for tid, t in live.items() if t.done()]
            for tid in done:
                live.pop(tid)
            await asyncio.sleep(LOOP_SLEEP_S)
    except asyncio.CancelledError:
        for t in live.values():
            t.cancel()
        raise


async def sweep_stale_tasks():
    """服务启动恢复 + 周期兜底：running 超时→failed（上游 sweeper 语义）"""
    db.execute("UPDATE tasks SET status='failed',error='服务重启恢复:标记中断',finished_at=?,"
               "updated_at=? WHERE status='running'", (_now(), _now()))
    while True:
        await asyncio.sleep(30)
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - (TASK_TIMEOUT_S + 120)
            stale = db.query("SELECT run_id,task_id,started_at FROM tasks WHERE status='running'")
            for r in stale:
                try:
                    started = datetime.fromisoformat(r["started_at"]).timestamp()
                except (TypeError, ValueError):
                    continue
                if started < cutoff:
                    db.execute("UPDATE tasks SET status='failed',error='超时兜底',finished_at=?,"
                               "updated_at=? WHERE run_id=? AND task_id=?",
                               (_now(), _now(), r["run_id"], r["task_id"]))
            # §7-②：等待态自愈（过期唤醒 / 到顶失败）——挂在这里是为了共用这个已有的心跳，
            # 不为等待态另起一个 loop。
            resume_expired_waits()
        except Exception:  # noqa: BLE001
            pass
