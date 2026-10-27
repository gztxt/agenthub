"""任务 DAG 协同引擎（部署方案 S3 的 Python 落地：SQLite 状态机 + asyncio）

对应上游 workflow.rs 的核心语义，砍掉 Redis/petgraph：
- 状态持久化：tasks 表（服务重启不丢，running→启动时标记 failed 由用户重试）
- 拓扑执行：依赖满足即并发派发（每 run 并发上限 4）
- 超时熔断：单任务默认 600s（asyncio.wait_for）+ 后台 sweeper 兜底
- 上下文传递：上游 output 注入下游 prompt（临时记忆区语义，限长防爆炸）
- Leader 拆解：LLM 产出受校验的 JSON DAG（环检测/引用检测/数量与深度限制）
"""
import asyncio
import hashlib
import inspect
import json
import os
import re
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import chain_judge
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

#: B1（2026-10-22）：子任务派发前是否跑一次接单前置检索，把线索拼进 prompt。
#: 默认开；`TASK_RETRIEVAL=0` 关掉（回归对比 / 离线跑测用）。
TASK_RETRIEVAL = os.getenv("TASK_RETRIEVAL", "1").strip().lower() not in ("0", "false", "no", "off")
#: 检索块的正文预算（字符）。与 UPSTREAM_TRIM 同一口径：超了要带**显式省略标记**，
#: 不能让下游分不清「上游只给了这么多」与「被砍掉了」。
RETRIEVAL_TRIM = int(os.getenv("TASK_RETRIEVAL_TRIM", "1200"))
#: 检索自身的超时（秒）。检索只是增强，**绝不能拖死一个子任务**：
#: 超时就当「本次无线索」进 prompt（并显式写出来），子任务照跑。
RETRIEVAL_TIMEOUT_S = float(os.getenv("TASK_RETRIEVAL_TIMEOUT_S", "8"))
#: B3：验证失败且**可修复**时自动重试的最大次数。默认 1（= 最多跑两遍）。
#: 不做无限重试：验证失败如果每次原因相同，重跑只是烧钱（本仓铁律：失败即止损）。
MAX_AUTO_RETRY = int(os.getenv("TASK_AUTO_RETRY", "1"))

#: 全链路编排（2026-10-27）：一个 goal 最多自动跑几**轮**（拆解→派发→汇总→重拆）。
#: 默认 2 = 首轮 + 最多一次重拆。轮与「attempt」不是一回事：attempt 是**同一个子任务**
#: 重跑（MAX_AUTO_RETRY），round 是**整条 run 重新拆解**。两个上限各自独立、都可配。
#: 依据 wiki `多轮迭代终止条件设计` / `终止条件与升级机制`：2 轮作安全上限，
#: 连续两轮 FAIL 签名相同或 diff 无变化 ⇒ 立即升人会签（见 `replan_gate`）。
MAX_ROUNDS = int(os.getenv("TASK_MAX_ROUNDS", "2"))

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
    # 2026-10-22 批 B1/B2：把「子任务执行前检索到了什么」落库。
    # 为什么必须落库：拆解→检索→验证 三段此前**根本没串起来**（tasks.py 零引用
    # skill/relevant/intake/memory），而一旦串起来，「这个子任务拿到的线索是什么」
    # 就变成了复盘的唯一入口 —— 不存下来，出错只能重跑。
    ("retrieval_json", "TEXT"),
    # B3：自动重试计数（上限 MAX_AUTO_RETRY，防验证失败→重跑→再失败→无限循环）。
    ("attempts", "INTEGER"),
    # 2026-10-23：主链判定附注（三态 completed/partial/off_topic + ok/ms/model/cached）。
    # 为什么留原文 JSON 而不是拆列：它是**判定证据**，字段还会随判定原语演进；
    # 而处置只依赖 `label` 一个字段（off_topic ⇒ 不自动接受），拆列没有收益。
    ("judge_json", "TEXT"),
    # 2026-10-27 全链路编排：拆解时的**选型意图**与**难度分**。
    # `model_choice` 与 `model` 必须是两列、两个口径：
    #   · model         = 执行后 adapter **回包**里的实发模型（观测值）；
    #   · model_choice  = Leader 拆解时**请求**的模型（意图值，可能被上游改写/回落）。
    # 合成一列就再也分不清「选了没生效」与「选了但网关换了」——正是 v0.13.167 那条的病根。
    ("model_choice", "TEXT"),
    # difficulty 1~3（1 简单/2 普通/3 困难），只用于**选型依据与复盘**，不参与预算与判活：
    # 拿它当阈值就是拿模型的自评当事实。
    ("difficulty", "INTEGER"),
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


def should_retry(verdict_status: str, repairable: bool, attempts: Any) -> bool:
    """B3 纯函数：这次验证失败该不该自动重试一次（可单测，避免把判据埋进 400 行协程）。

    三条同时成立才重试：
      ① `repairable` —— 产物**明确不合格**（缺内容/非法 JSON）。`insufficient`
         （未知 kind / 没有产物 / 判据缺失）重跑一次不会变好，重试只是烧钱；
      ② `attempts < MAX_AUTO_RETRY` —— 有界，防「失败→重跑→再失败」无限循环；
      ③ 状态是 fail（passthrough 保险：调用方传错也不重试）。

    为什么不在 `insufficient` 上重试：那是**判不了**，不是**做错了**。判不了时重跑同一
    执行者、同一判据，结果只会再来一次判不了 —— 这正是「无限等待」禁令的同族。
    """
    try:
        n = int(attempts or 0)
    except (TypeError, ValueError):
        n = MAX_AUTO_RETRY          # 读不出来就当已用完（保守：不重试）
    return bool(repairable) and verdict_status == verifier.STATUS_FAIL and n < MAX_AUTO_RETRY


def retrieval_block(rep: Dict[str, Any], limit: int = RETRIEVAL_TRIM) -> str:
    """把一份 intake 结果压成**给执行者看的**线索块（B1，纯函数，可单测）。

    只取三类有动作价值的东西：
      · 约束：带「必须/禁止/不得…」的命中 ⇒ 开工前要知道的红线；
      · 回读：有路径的原文 ⇒ 需要细节时去读而不是凭空发挥；
      · 技能：命中时给 `hub_skill_auto(task=...)` 调用式 ⇒ 拿到说明书。

    **截断必须显式**（与 `format_upstream` 同一条纪律）：超预算时尾部写
    「已省略 N 字符」而不是默默截掉 —— 否则下游无法区分「本来就这么少」与「被砍了」。
    空结果返回空串（调用方据此**不加块**，不制造一个空标题占位）。
    """
    plan = rep.get("plan") or {}
    lines: List[str] = []
    for c in (plan.get("constraints") or [])[:3]:
        lines.append("- 约束（%s）：%s" % (c.get("src") or "?", str(c.get("text") or "")[:160]))
    for x in (plan.get("must_read") or [])[:2]:
        lines.append("- 回读：%s —— %s" % (x.get("path") or "?", str(x.get("why") or "")[:100]))
    sk = rep.get("skill") or {}
    if sk.get("ok"):
        lines.append("- 技能：%s ⇒ 需要步骤时调 hub_skill_auto(task=\"…\")" % (sk.get("name"),))
    if not lines:
        return ""
    head = ("—— 检索线索（hub intake·%s 路·判定 %s；**只是线索不是结论**）——\n"
            % (plan.get("coverage") or "?", plan.get("verdict") or "?"))
    body = head + "\n".join(lines)
    if len(body) <= limit:
        return body
    keep = max(0, limit - len(head))
    return body[:len(head) + keep] + "\n……（检索线索已省略 %d 字符）……" % (len(body) - len(head) - keep)


async def _retrieval_for(goal: str, prompt: str, agent_id: str) -> tuple:
    """跑一次接单前置检索，返回 `(块文本, 落库用的 dict)`。**任何失败都不阻塞子任务**。

    为什么要串这一层（B1 的全部理由）：本轮实测发现「自动拆解→检索→验证」三段
    各自存在但**从未串联**（`grep -E 'skill|relevant|intake|memory|kb' src/tasks.py` = 0 命中），
    生产 `tasks` 表也只有 1 个 run（2026-09-06）。拆出来的子任务直接进 LLM，
    拿不到任何记忆/文档/技能线索 ⇒ 同一个任务，人在对话里做会先查旧账，
    流水线做则凭空重写一遍。

    ⚠ 边界：`deep=False` + `escalate="off"` —— 派发阶段**不准联网、不准查 GitHub**
    （那是 20~90s 且吃额度），只走本地三路；且整段有 `RETRIEVAL_TIMEOUT_S` 超时，
    超时/异常一律降级为空块，**在返回里写明原因**（不许静默当作「没有线索」）。
    """
    import intake as intake_mod          # 局部导入：intake 依赖 skill_store/refscout，
                                        # 顶层导入会把启动依赖面拉爆（且可能成环）
    t0 = asyncio.get_event_loop().time()
    try:
        rep = await asyncio.wait_for(
            intake_mod.run(intake_mod.IntakeIn(
                task=(goal + " :: " + prompt)[:2000], caller="task:%s" % (agent_id or "?"),
                deep=False, auto_deep=False, escalate="off")),
            timeout=RETRIEVAL_TIMEOUT_S)
    except asyncio.TimeoutError:
        return "", {"ok": False, "error": "检索超时 %.1fs" % RETRIEVAL_TIMEOUT_S}
    except Exception as e:  # noqa: BLE001 — 检索是增强，不许带倒任务
        return "", {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    plan = rep.get("plan") or {}
    meta = {
        "ok": True,
        "verdict": plan.get("verdict"),
        "coverage": plan.get("coverage"),
        "strong": plan.get("strong"),
        "skill_strong": plan.get("skill_strong"),
        "skill": (rep.get("skill") or {}).get("name"),
        "constraints": len(plan.get("constraints") or []),
        "must_read": [x.get("path") for x in (plan.get("must_read") or [])][:4],
        "ms": int((asyncio.get_event_loop().time() - t0) * 1000),
    }
    return retrieval_block(rep), meta


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
-- 2026-10-27 全链路编排：run 级血缘与轮次（一个 run 一行）。
-- 为什么轮次必须落在**表**里而不是 tasks 的列：轮次是 **run 的属性**（一条 run = 一轮），
-- 写进 tasks 就是同一份事实存 N 份，任何一次漏改都会让两处口径漂移。
CREATE TABLE IF NOT EXISTS task_runs (
    run_id TEXT PRIMARY KEY,
    parent_run_id TEXT,                 -- 上一轮 run（首轮为 NULL）
    round INTEGER NOT NULL DEFAULT 1,   -- 第几轮（1 起）
    goal TEXT,
    origin TEXT NOT NULL DEFAULT 'api', -- api / replan / auto
    auto_rounds INTEGER NOT NULL DEFAULT 0,  -- 1 = 本轮跑完自动重拆（受 MAX_ROUNDS 约束）
    child_run_id TEXT,                  -- 已重拆出的下一轮（幂等占位，防空转重拆）
    stop_reason TEXT,                   -- complete / max_rounds / same_failure / budget / unfinished
    created_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_task_runs_parent ON task_runs(parent_run_id);
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
    # 2026-10-27 全链路编排：本轮跑完若未达标，是否自动重拆一次（受 MAX_ROUNDS 约束）。
    # **默认关**：自动重拆会真的再花一轮 token 且自动接受产物，属于「烧钱 + 改状态」
    # 的组合动作，必须由调用方显式点头（同 hub_skill_acquire 的 allow_install 口径）。
    auto_rounds: bool = False
    # 只拆不落库：给「先看拆解再决定跑不跑」用（MCP hub_dispatch 的默认路径）。
    # 不做的话每次预览都会在 tasks 表里留一条没人要的 run，把真 run 顶出列表。
    dry_run: bool = False


class ReplanIn(BaseModel):
    """手动触发重拆。`max_rounds` 供一次性紧急放行，但**不能超过** MAX_ROUNDS 的硬上限
    —— 上限是「最多自动 2 轮」这条用户裁定的落地，不是建议值。"""
    auto_run: bool = True
    max_rounds: Optional[int] = None


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
每项格式: {"id":"t1","task":"中文指令（自包含，可独立执行）","agent_id":"执行Agent","difficulty":2,"deps":[]}
规则：
- agent_id 只能从这里选：{agents}（选最合适的；不确定用 {default}）
- difficulty 取 1/2/3：1 = 单点取证或机械核对；2 = 要读多份材料后归纳；3 = 要取舍/设计/跨模块推理
- model 可选：给了就必须从 {models} 里选；机械任务优先便宜模型，难度 3 才用强模型。不确定就**省略该字段**
- 子任务数 ≤ {maxn}；无依赖的兄弟任务会被并行执行，有依赖写 deps
- 聚合/汇总类任务必须 deps 列出全部上游
"""

REPLAN_PROMPT = """你是任务编排器的**第二轮**。上一轮 DAG 的执行结果在下面，只输出**需要补做/重做**的子任务
（格式与第一轮相同：JSON 数组，不要围栏、不要解释）。
规则：
- 已经达标的任务**不要重做**；只针对失败、被阻塞、或产物明显不合格的部分
- 若全部达标，输出 `[]`（空数组）——这是唯一的「完工」信号，不要输出解释文字
- agent_id 只能从这里选：{agents}；difficulty 取 1/2/3；model 可选（从 {models} 选）
- 每项 task 必须以「重做：」或「补做：」开头，并写明针对上一轮的哪个 id
- 子任务数 ≤ {maxn}；重拆只有这一轮，别假设还有下一次
"""


def _agent_model_block(models: Optional[List[str]]) -> str:
    """把模型候选压成提示词里的一个枚举。取不到清单时明写「省略该字段」，
    而不是给一个空枚举（空枚举会让 Leader 编一个不存在的 ID）。"""
    if models:
        return ", ".join(models)
    return "（模型清单暂不可用：省略 model 字段，用该 agent 的默认模型）"


def _extract_json_array(text: str) -> list:
    text = re.sub(r"```(?:json)?|```", "", text)
    s, e = text.find("["), text.rfind("]")
    if s < 0 or e <= s:
        raise ValueError("LLM 未返回 JSON 数组")
    return json.loads(text[s:e + 1])


def _model_allowed(model: str, allowed: set) -> bool:
    """模型 ID 是否在候选清单里（纯函数）。

    容忍 provider 前缀：opencode/omp 的落点值是 `ccr/alibaba/...`，而清单里是裸 ID。
    只剥**已知的**前缀，不做通用剥离 —— 通用剥离会把 `openrouter/xxx` 这种真 ID 的首段吃掉。
    """
    m = (model or "").strip()
    if not m:
        return False
    if m in allowed:
        return True
    for pre in ("ccr/", "ccr:"):
        if m.startswith(pre) and m[len(pre):] in allowed:
            return True
    return False


def _clamp_difficulty(v: Any) -> int:
    """难度分归一到 1~3。读不出来就取 2（普通）**而不是报错**：
    这是 LLM 自评字段，格式噪声不该带倒整条 run；但也不能留空让下游各自猜默认值。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return 2
    return max(1, min(3, n))


def _validate_dag(items: list, known_agents: set, default_agent: str,
                  allowed_models: Optional[Dict[str, set]] = None) -> List[dict]:
    """校验并归一 DAG。

    `allowed_models`：`{agent_id: {合法模型 ID}}`，**只校验在册的 agent**。
    不在字典里的 agent（自定义端口 agent）模型域在它自己那边，拿 CCR 清单判它非法是误杀。
    `None` = 整路不校验（清单取不到 ⇒ 判不了 ⇒ 不拦，与 P0-4 同口径）。
    非法模型**降级为 None**（= 用该 agent 的持久化默认）而不是 400：
    model 是 LLM 产出，幻觉一个 ID 不该让整条 run 拆不出来；而 `model_choice` 落库为 NULL，
    「这次没用上选型」在复盘里是可查的，不算静默。
    """
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
        model = None
        cand = str(it.get("model") or "").strip()
        if cand:
            if allowed_models is None or agent not in allowed_models:
                # 清单取不到 / 该 agent 的模型域不归 CCR 判 ⇒ **原样透传**，不替它做决定
                model = cand
            elif _model_allowed(cand, allowed_models[agent]):
                model = cand
        normalized.append({"id": tid, "task": str(it.get("task") or it.get("prompt") or "").strip(),
                           "agent_id": agent, "deps": [str(d) for d in (it.get("deps") or [])],
                           "difficulty": _clamp_difficulty(it.get("difficulty")),
                           "model": model})
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


def failure_signature(rows: list) -> str:
    """FAIL 签名（纯函数）：失败任务的 `(id, error 前 60 字)` 排序后取 sha1 前 12 位。

    用途只有一条：**判「原地打转」**。连续两轮签名相同 ⇒ 再拆一次也是同一批失败，
    继续自动重拆只是烧钱（本仓铁律「失败即止损」，判据见 wiki `多轮迭代终止条件设计`）。
    只看 failed，不看 blocked：blocked 是「上游失败」的传导，重复计入会让签名恒等于上一轮。
    """
    parts = sorted("%s|%s" % (r.get("task_id"), str(r.get("error") or "")[:60])
                   for r in rows if r.get("status") == "failed")
    if not parts:
        return ""
    return hashlib.sha1("\n".join(parts).encode("utf-8")).hexdigest()[:12]


def replan_gate(round_no: Any, max_rounds: Any, rows: list,
                parent_rows: Optional[list] = None,
                budget_over: Optional[str] = None) -> Dict[str, Any]:
    """要不要自动重拆下一轮（纯函数，可单测；判据顺序即优先级）。

    六种结论，**没有一种叫「再试试」**：
      · unfinished → 还有任务在跑/等输入，现在拆就是并发打架；
      · complete   → 全绿，收工（这是唯一的正常完工出口）；
      · max_rounds → 到硬上限（默认 2 轮），转人工；
      · budget     → run 预算已耗尽，再拆等于透支；
      · same_failure → 与上一轮 FAIL 签名相同 = 原地打转，转人工；
      · go         → 确有不达标项且原因**变了**，进下一轮。
    顺序不能换：unfinished 必须在 complete 之前（半途全绿是假象），
    max_rounds 必须在 go 之前（上限是硬闸，不因原因不同而放行）。
    """
    st = {r.get("task_id"): r.get("status") for r in rows}
    if any(s not in TERMINAL_STATUSES for s in st.values()):
        return {"go": False, "kind": "unfinished", "reason": "run 未收敛（仍有非终态任务）"}
    if st and all(s == "success" for s in st.values()):
        return {"go": False, "kind": "complete", "reason": "全部子任务达标"}
    try:
        rnd = int(round_no or 1)
    except (TypeError, ValueError):
        rnd = 1
    try:
        cap = int(max_rounds or 1)
    except (TypeError, ValueError):
        cap = 1
    if rnd >= cap:
        return {"go": False, "kind": "max_rounds", "reason": "已达自动轮次上限 %d" % cap}
    if budget_over:
        return {"go": False, "kind": "budget", "reason": "预算耗尽: %s" % budget_over}
    if parent_rows is not None:
        cur, prev = failure_signature(rows), failure_signature(parent_rows)
        if cur and cur == prev:
            return {"go": False, "kind": "same_failure",
                    "reason": "与上一轮 FAIL 签名相同（%s），原地打转，转人工" % cur}
    return {"go": True, "kind": "go",
            "reason": "有未达标项，进入第 %d 轮" % (rnd + 1)}


def run_meta(run_id: str) -> dict:
    """run 级血缘/轮次。**存量 run 没有行**（本表 2026-10-27 才有）⇒ 返回默认值，
    语义等价于「首轮、不自动重拆」，不制造 404 也不倒推猜测。"""
    rows = db.query("SELECT * FROM task_runs WHERE run_id=?", (run_id,))
    if rows:
        return dict(rows[0])
    return {"run_id": run_id, "parent_run_id": None, "round": 1, "auto_rounds": 0,
            "child_run_id": None, "stop_reason": None, "origin": "api"}


def _create_run(goal: str, items: List[dict], *, verify_kind: str,
                budget_ms: Optional[int], budget_tokens: Optional[int],
                parent_run_id: Optional[str] = None, origin: str = "api",
                auto_rounds: bool = False, round_no: int = 1,
                run_id: Optional[str] = None) -> str:
    """落一条 run（tasks 行 + task_runs 行）。decompose 与 replan 共用，
    避免两处 INSERT 各自长出一套字段（那是最典型的「两套口径」起点）。
    `run_id` 可外部给：重拆要**先占位再落库**，否则并发两次重拆会生成两条子 run。"""
    run_id = run_id or uuid.uuid4().hex[:10]
    now = _now()
    for it in items:
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,budget_ms,budget_tokens,difficulty,model_choice,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
            (run_id, it["id"], run_id, goal, it["task"], it["agent_id"],
             json.dumps(it["deps"]), verify_kind, budget_ms, budget_tokens,
             it.get("difficulty"), it.get("model"), now, now))
    db.execute("INSERT INTO task_runs(run_id,parent_run_id,round,goal,origin,auto_rounds,created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (run_id, parent_run_id, round_no, goal, origin,
                1 if auto_rounds else 0, now))
    return run_id


def _allowed_models_for(known_agents: set) -> Optional[Dict[str, set]]:
    """能真正吃下 `model` 参数的 agent 的合法模型清单；取不到清单返回 None（= 不校验）。

    只给走 OpenAI 兼容适配器的 **claude / jcode** 建白名单：
      · pi 的适配器 chat() 恒返回「需通过 Web UI 交互」(adapters/pi.py:21)，给它选模型是自欺；
      · 自定义端口 agent 的模型域在它自己那边，CCR 清单里没有它的 ID ⇒ 拿清单判它会误杀。
    `catalog_ids()` 取不到（网关挂/无凭据）时 `checked=False` ⇒ 返回 None，
    与 P0-4「判不了就不拦」同口径，绝不把「取不到」写成「选了非法模型」。
    """
    try:
        import modelcfg
        cat = modelcfg.catalog_ids()
        ids = {str(x) for x in (cat.get("ids") or set())}
        if not cat.get("checked") or not ids:
            return None
    except Exception:  # noqa: BLE001 — 选型是增强，不许带倒拆解
        return None
    return {a: ids for a in ("claude", "jcode") if a in known_agents} or None


def _model_choices(known_agents: set) -> Optional[List[str]]:
    """给 Leader 提示词用的模型候选（排序后取前 40，避免把整张清单灌进 token）。"""
    m = _allowed_models_for(known_agents)
    if not m:
        return None
    union = sorted(set().union(*m.values()))
    return union[:40]


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
    allowed = _allowed_models_for(known)
    messages = [{"role": "system", "content": (
        LEADER_PROMPT
        .replace("{agents}", ", ".join(sorted(known)) or "jcode")
        .replace("{default}", default)
        .replace("{models}", _agent_model_block(_model_choices(known)))
        .replace("{maxn}", str(MAX_TASKS)))},
        {"role": "user", "content": body.goal}]
    try:
        answer, _ = await llm.chat_tools_loop(messages, [], None, max_rounds=1)
        items = _validate_dag(_extract_json_array(answer), known, default, allowed)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Leader 拆解失败: {e}")
    if body.dry_run:
        return {"run_id": None, "goal": body.goal, "tasks": items, "running": False,
                "dry_run": True, "round": 1,
                "auto_rounds": bool(body.auto_rounds), "max_rounds": MAX_ROUNDS,
                "note": "dry_run：只拆解未落库，确认后传 dry_run=false 再调一次"}
    run_id = _create_run(body.goal, items, verify_kind=body.verify_kind,
                         budget_ms=body.budget_ms, budget_tokens=body.budget_tokens,
                         auto_rounds=body.auto_rounds)
    if body.auto_run:
        _start_runner(run_id)
    return {"run_id": run_id, "goal": body.goal, "tasks": items,
            "running": body.auto_run, "round": 1,
            "auto_rounds": bool(body.auto_rounds), "max_rounds": MAX_ROUNDS}


def _ensure_run_row(run_id: str) -> dict:
    """保证 task_runs 有这一行（存量 run 首次重拆时补建），返回 run_meta。

    补建出来的行 `origin='legacy'`、`round=1`、`auto_rounds=0` —— 存量 run 不因为
    新表出现就突然获得「自动重拆」能力，那是替用户改行为。
    """
    if not db.query("SELECT 1 FROM task_runs WHERE run_id=?", (run_id,)):
        r = db.query("SELECT MIN(goal) g, MIN(created_at) c FROM tasks WHERE run_id=?", (run_id,))
        goal = (r[0]["g"] if r else "") or ""
        db.execute("INSERT OR IGNORE INTO task_runs(run_id,parent_run_id,round,goal,origin,"
                   "auto_rounds,created_at) VALUES(?,NULL,1,?,'legacy',0,?)",
                   (run_id, goal, _now()))
    return run_meta(run_id)


def _replan_digest(rows: list, limit: int = 600) -> str:
    """把上一轮结果压成给 Leader 看的清单。**每条都带状态与 verdict**：
    只给 output 会让 Leader 以为失败项也「有产出」，于是重拆出一份重复劳动。"""
    out = []
    for r in sorted(rows, key=lambda x: str(x.get("task_id"))):
        body = (r.get("output") if r.get("status") == "success" else r.get("error")) or ""
        out.append("- [%s] %s｜agent=%s｜difficulty=%s｜verdict=%s｜%s" % (
            r.get("task_id"), r.get("status"), r.get("agent_id") or "-",
            r.get("difficulty") if r.get("difficulty") is not None else "-",
            r.get("verdict") or "-",
            str(body).replace("\n", " ")[:limit]))
    return "\n".join(out)


async def _replan_once(run_id: str, auto: bool = False,
                       max_rounds: Optional[int] = None) -> dict:
    """对一条**已收敛**的 run 做一次重拆。返回 `{ok, gate, created}`，不抛业务异常。

    顺序刻意如此：**先判闸门 → 再问 LLM → 最后占位落库**。
    反过来（先占位再问 LLM）一旦 LLM 超时，`child_run_id` 就永久占着，
    这条 run 再也重拆不了，而症状是「点重拆没反应」——最坏的静默失败形态。
    """
    import llm
    meta = _ensure_run_row(run_id)
    rows = db.query("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at", (run_id,))
    if not rows:
        raise HTTPException(404, "run not found")
    cap = MAX_ROUNDS
    if max_rounds is not None:
        # 只允许**收紧**：调用方传 5 也不能突破用户裁定的 2 轮硬上限。
        try:
            cap = max(1, min(int(max_rounds), MAX_ROUNDS))
        except (TypeError, ValueError):
            cap = MAX_ROUNDS
    parent_rows = None
    if meta.get("parent_run_id"):
        parent_rows = db.query("SELECT * FROM tasks WHERE run_id=?", (meta["parent_run_id"],))
    gate = replan_gate(meta.get("round") or 1, cap, rows, parent_rows,
                       run_budget_exceeded(run_id))
    if not gate["go"]:
        db.execute("UPDATE task_runs SET stop_reason=? WHERE run_id=?", (gate["kind"], run_id))
        return {"ok": True, "run_id": run_id, "gate": gate, "created": None}
    if not llm.configured():
        raise HTTPException(503, "LLM 未配置，无法重拆")
    known = set()
    if _ctx.get("agent_ids_fn"):
        known = set(_ctx["agent_ids_fn"]())
    default = ("jcode" if "jcode" in known else (sorted(known)[0] if known else "jcode"))
    allowed = _allowed_models_for(known)
    goal = meta.get("goal") or rows[0].get("goal") or ""
    messages = [{"role": "system", "content": (
        REPLAN_PROMPT
        .replace("{agents}", ", ".join(sorted(known)) or "jcode")
        .replace("{models}", _agent_model_block(_model_choices(known)))
        .replace("{maxn}", str(MAX_TASKS)))},
        {"role": "user", "content":
            "总目标：%s\n\n上一轮（第 %s 轮）的执行结果：\n%s" % (goal, meta.get("round") or 1,
                                                        _replan_digest(rows))}]
    try:
        answer, _ = await llm.chat_tools_loop(messages, [], None, max_rounds=1)
        raw = _extract_json_array(answer)
    except Exception as e:  # noqa: BLE001
        # 重拆失败**不改上一轮的结论**（它已经收敛），只如实记一笔。
        db.execute("UPDATE task_runs SET stop_reason=? WHERE run_id=?",
                   ("replan_error: %s" % str(e)[:150], run_id))
        return {"ok": False, "run_id": run_id, "gate": gate,
                "error": "重拆失败: %s" % e, "created": None}
    if not raw:
        # 空数组是 Leader 唯一的「完工」信号 —— 不许在这里替它猜「大概是好了」。
        ok = {"go": False, "kind": "complete_by_model", "reason": "重拆返回空数组：Leader 判定已无缺口"}
        db.execute("UPDATE task_runs SET stop_reason='complete_by_model' WHERE run_id=?", (run_id,))
        return {"ok": True, "run_id": run_id, "gate": ok, "created": None}
    try:
        items = _validate_dag(raw, known, default, allowed)
    except HTTPException as e:
        db.execute("UPDATE task_runs SET stop_reason=? WHERE run_id=?",
                   ("replan_invalid: %s" % str(e.detail)[:150], run_id))
        return {"ok": False, "run_id": run_id, "gate": gate,
                "error": "重拆 DAG 非法: %s" % e.detail, "created": None}
    # 占位：只有第一个调用者能把 child_run_id 从 NULL 改掉 ⇒ 并发重拆不会产生两条子 run。
    child_id = uuid.uuid4().hex[:10]
    won = db.execute("UPDATE task_runs SET child_run_id=? WHERE run_id=? AND child_run_id IS NULL",
                     (child_id, run_id))
    if not won:
        return {"ok": True, "run_id": run_id, "gate": dict(gate, kind="already_replanned",
                                                           reason="本轮已重拆过"),
                "created": run_meta(run_id).get("child_run_id")}
    first = rows[0]
    _create_run(goal, items, verify_kind=first.get("verify_kind") or verifier.DEFAULT_KIND,
                budget_ms=first.get("budget_ms"), budget_tokens=first.get("budget_tokens"),
                parent_run_id=run_id, origin=("auto" if auto else "replan"),
                auto_rounds=bool(meta.get("auto_rounds")),
                round_no=int(meta.get("round") or 1) + 1, run_id=child_id)
    if auto:
        _start_runner(child_id)
    db.log_profile_event("task_replan", "-", "replan", None, trace_id=run_id,
                         detail={"run": run_id, "child": child_id,
                                 "round": int(meta.get("round") or 1) + 1,
                                 "tasks": len(items), "auto": bool(auto)})
    return {"ok": True, "run_id": run_id, "gate": gate, "created": child_id,
            "round": int(meta.get("round") or 1) + 1, "tasks": items}


async def _maybe_replan(run_id: str) -> None:
    """runner 收敛后的自动钩子。**只对 `auto_rounds=1` 的 run 生效**，
    其余一律不动（人工 run 的轮次由人决定）。异常吞掉但留 stop_reason，
    不许让重拆失败冒泡进 `_execute_run`（那会把已收敛的 run 变成异常）。"""
    try:
        meta = run_meta(run_id)
        if not int(meta.get("auto_rounds") or 0):
            return
        # 已重拆过 / 已有结论 ⇒ 直接退出。缺这一条，任何一次 `_start_runner` 重入
        # 都会再问一次 LLM（占位 UPDATE 会挡住落库，但钱已经花了）。
        if meta.get("child_run_id") or meta.get("stop_reason"):
            return
        await _replan_once(run_id, auto=True)
    except Exception as e:  # noqa: BLE001
        try:
            db.execute("UPDATE task_runs SET stop_reason=? WHERE run_id=?",
                       ("auto_replan_error: %s" % str(e)[:150], run_id))
        except Exception:  # noqa: BLE001
            pass


@router.post("/api/tasks/{run_id}/replan")
async def replan_run(run_id: str, body: ReplanIn = ReplanIn()):
    """手动重拆（第二轮）。闸门与自动重拆**完全同一套**（`replan_gate`），
    区别只有 `origin` 记 `replan` 与是否自动启动。`max_rounds` 只允许**收紧**。"""
    out = await _replan_once(run_id, auto=False, max_rounds=body.max_rounds)
    if out.get("created") and body.auto_run:
        _start_runner(out["created"])
    return out


@router.get("/api/tasks/runs")
async def list_runs(limit: int = Query(default=20, le=100)):
    rows = db.query("""
        SELECT t.run_id, MIN(t.trace_id) trace_id, MAX(t.goal) goal, COUNT(*) total,
               SUM(CASE WHEN t.status='success' THEN 1 ELSE 0 END) success,
               SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) failed,
               SUM(CASE WHEN t.status='running' THEN 1 ELSE 0 END) running,
               SUM(CASE WHEN t.status='pending' THEN 1 ELSE 0 END) pending,
               SUM(CASE WHEN t.status IN ('waiting_input','waiting_approval','paused')
                        THEN 1 ELSE 0 END) waiting,
               SUM(CASE WHEN t.status='submitted' THEN 1 ELSE 0 END) submitted,
               MIN(t.created_at) created_at, MAX(t.finished_at) finished_at,
               MAX(COALESCE(r.round,1)) round, MAX(r.parent_run_id) parent_run_id,
               MAX(r.child_run_id) child_run_id, MAX(r.stop_reason) stop_reason
        FROM tasks t LEFT JOIN task_runs r ON r.run_id = t.run_id
        GROUP BY t.run_id ORDER BY created_at DESC LIMIT ?""", (limit,))
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
    return {"run_id": run_id, "goal": rows[0]["goal"], "tasks": rows,
            "run": run_meta(run_id), "max_rounds": MAX_ROUNDS}


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


def _chat_arity(fn) -> int:
    """chat_fn 能接几个**位置参数**（≥4 才能收 model）。取不到签名就当 3（保守）。

    为什么要探测而不是直接传：`set_context` 的接线方不止一处（main.py 的对话通道、
    cron、L0 用例），有的只写 3 参 lambda。直接多传一个参数会 TypeError，
    而「重试一次」的写法会在已花钱之后才炸 ⇒ 只能事前问签名。
    """
    try:
        params = inspect.signature(fn).parameters.values()
    except (TypeError, ValueError):
        return 3
    n = 0
    for p in params:
        if p.kind in (p.POSITIONAL_ONLY, p.POSITIONAL_OR_KEYWORD):
            n += 1
        elif p.kind == p.VAR_POSITIONAL:
            return 99
    return n


async def _call_chat(chat_fn, agent_id: str, prompt: str, tag: str,
                     model: Optional[str]):
    """按 chat_fn 的实际签名决定要不要把选型模型传下去。

    传不下去时**不报错**（拆解意图不等于执行契约），但这条事实是可查的：
    `model_choice` 有值而 `model`（实发回包）是另一个值，就是「选了没生效」的证据。
    """
    if model and _chat_arity(chat_fn) >= 4:
        return await chat_fn(agent_id, prompt, tag, model)
    return await chat_fn(agent_id, prompt, tag)


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

    # ── B3：上一次验证失败的提示（自动重试时带上，否则重跑就是原样再赌一次）──
    attempts_done = int(row.get("attempts") or 0)
    if attempts_done:
        prompt = ("—— 上一次验证未通过（自动重试 %d/%d）：%s ——\n\n"
                  % (attempts_done, MAX_AUTO_RETRY,
                     str(row.get("verify_reason") or "")[:300])) + prompt

    # ── B1：接单前置检索（记忆/知识库/技能），把线索拼进 prompt ──
    # 失败/超时一律降级（不阻塞），但**必须留下痕迹**（retrieval_json 里的 error）。
    if TASK_RETRIEVAL:
        block, rmeta = await _retrieval_for(row.get("goal") or "", prompt,
                                            row.get("agent_id") or "")
        if block:
            prompt = block + "\n\n" + prompt
        try:
            db.execute("UPDATE tasks SET retrieval_json=?,updated_at=? "
                       "WHERE run_id=? AND task_id=?",
                       (json.dumps(rmeta, ensure_ascii=False)[:4000], _now(),
                        run_id, row["task_id"]))
        except Exception:  # noqa: BLE001 — 落库失败不得带倒子任务（检索是增强）
            pass
    t0 = asyncio.get_event_loop().time()
    try:
        result = await asyncio.wait_for(
            _call_chat(chat_fn, row["agent_id"], prompt,
                       f"task-{row['trace_id']}-{row['task_id']}",
                       row.get("model_choice")),
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
            # ── 主链判定（2026-10-23）──
            # verifier 只判**结构**（非空 / 是 JSON 数组 / 含指定子串），不判「有没有答任务」。
            # 这里补一次三态分类（completed / partial / off_topic），处置方向刻意单向：
            #   判成 off_topic ⇒ 不自动接受，转 `submitted` 等人工复核；
            #   判成 partial/completed ⇒ 与接入前一致（partial 只记附注，不拦）；
            #   判定没跑成（脚本缺失/超时/标签不在枚举） ⇒ **完全旧行为**，不拦不降级。
            # 为什么不是「判成 completed 才接受」：1.5b 的置信度不可信（451 题以 0.95 选错），
            # 拿它当放行闸等于把「模型觉得像」当成事实；反过来只让它拦，错了最多多一次人看。
            jv = await asyncio.to_thread(
                chain_judge.classify_output,
                row.get("goal") or row.get("prompt") or "", out)
            judge_json = json.dumps(jv, ensure_ascii=False)[:2000]
            # 处置折成纯函数（`chain_judge.accept_decision`），L0 才能把「方向」钉死：
            # 判成 off_topic 只降级不丢弃；判定没跑成 = 旧行为。
            decision = chain_judge.accept_decision(AUTO_ACCEPT, jv)
            need_review = decision["review"]
            if decision["accept"]:
                db.execute("UPDATE tasks SET status='success',output=?,duration_ms=?,spent_ms=?,"
                           "spent_tokens=?,model=?,verdict=?,verify_reason=?,verify_ms=?,"
                           "judge_json=?,submitted_at=?,accepted_at=?,accepted_by=?,"
                           "finished_at=?,updated_at=?"
                           " WHERE run_id=? AND task_id=?",
                           (out, dur, dur, spent_tokens, used_model, verdict["status"], None,
                            verify_ms, judge_json, _now(), _now(), "auto", _now(), _now(),
                            run_id, row["task_id"]))
            else:
                # need_review 时这里就是「verifier 过了但判定说答非所问」——原因写进
                # verify_reason（它此时必为 NULL），让复核的人知道**不是** verifier 失败。
                reason = decision["reason"]
                db.execute("UPDATE tasks SET status='submitted',output=?,duration_ms=?,spent_ms=?,"
                           "spent_tokens=?,model=?,verdict=?,verify_reason=?,verify_ms=?,"
                           "judge_json=?,submitted_at=?,updated_at=?"
                           " WHERE run_id=? AND task_id=?",
                           (out, dur, dur, spent_tokens, used_model, verdict["status"], reason,
                            verify_ms, judge_json, _now(), _now(), run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"],
                                 "success" if (AUTO_ACCEPT and not need_review) else "submitted",
                                 dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "verify": kind,
                                         "model": used_model,
                                         "auto_accept": AUTO_ACCEPT and not need_review,
                                         "judge": jv.get("label") if jv.get("ok") else "unjudged",
                                         "judge_ok": bool(jv.get("ok")),
                                         "judge_ms": jv.get("ms")})
        else:
            gaps = "; ".join("%s: %s" % (g["id"], g["detail"]) for g in verdict["gaps"])
            err = ("verifier %s (%s): %s" % (verdict["status"], kind, gaps))[:500]
            # ── B3：可修复的验证失败，自动重试 MAX_AUTO_RETRY 次（默认 1）──
            # 只对 `repairable` 重试：`insufficient`（未知 kind / 无产物）重跑也不会变好。
            # 次数写进 `attempts` 列并在重跑时把上次的 verify_reason 接进 prompt（否则就是原样再赌）。
            if should_retry(verdict["status"], verdict.get("repairable"), attempts_done):
                db.execute("UPDATE tasks SET status='pending',attempts=?,error=?,verify_reason=?,"
                           "verdict=?,verify_ms=?,spent_ms=?,spent_tokens=?,model=?,"
                           "finished_at=NULL,updated_at=?"
                           " WHERE run_id=? AND task_id=?",
                           (attempts_done + 1,
                            ("%s（自动重试 %d/%d）" % (err, attempts_done + 1, MAX_AUTO_RETRY))[:500],
                            gaps[:500], verdict["status"], verify_ms, dur, spent_tokens,
                            used_model, _now(), run_id, row["task_id"]))
                db.log_profile_event("task_exec", row["agent_id"], "retry", dur,
                                     trace_id=row["trace_id"],
                                     detail={"task": row["task_id"], "verify": kind,
                                             "verify_status": verdict["status"],
                                             "attempt": attempts_done + 1,
                                             "model": used_model})
                return
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
                # 2026-10-27 全链路编排：收敛后若本轮声明了 `auto_rounds`，在这里接下一轮。
                # 放在 return 前而不是收尾处，是因为「收敛」只在这一处判定 —— 另起一个
                # 监听会变成第二份「什么叫跑完」的定义。
                await _maybe_replan(run_id)
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
