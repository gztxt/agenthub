"""任务 DAG 协同引擎（部署方案 S3 的 Python 落地：SQLite 状态机 + asyncio）

对应上游 workflow.rs 的核心语义，砍掉 Redis/petgraph：
- 状态持久化：tasks 表（服务重启不丢，running→启动时标记 failed 由用户重试）
- 拓扑执行：依赖满足即并发派发（每 run 并发上限 4）
- 超时熔断：单任务默认 600s（asyncio.wait_for）+ 后台 sweeper 兜底
- 上下文传递：上游 output 注入下游 prompt（临时记忆区语义，限长防爆炸）
- Leader 拆解：LLM 产出受校验的 JSON DAG（环检测/引用检测/数量与深度限制）

2026-10-31 片3 文件级拆分（`docs/SPLIT-PLAN-20261031.md` §3.3）后的分层：
`tasks_tiers`（纯逻辑/配置）← `tasks_runner`（执行/验证/结算）← `tasks_dag`（拆解）
← `tasks_replan`（重拆）← **本文件（run CRUD + 路由）**。本文件只剩 run 生命周期与
HTTP 出口；`tasks.X` 一律是门面重导出，改实现请去对应层。
"""
import json
from typing import Optional

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

# `json` / `db` / `verifier` 必须留：测试直接打到 `tasks.json`、`tasks.db`、
# `tasks.verifier.verify` 上；它们是**同一个模块对象**，删了就是静默改打桩面。
import db
import verifier

router = APIRouter()


# ── 片3 文件级拆分：tiers ← runner ← dag ← replan 的显式门面 ─────────────
# 为什么要「显式重新导出 + 本文件内一律写 `_x.X`」：`from m import X` 会把名字拷进
# 本模块命名空间，之后在别处 `patch("m.X")` 对拷贝无效 —— 那是片1 踩过的静默假绿。
# 所有历史入口（`tasks.X`，含 main.py 与测试）继续可用，但内部一律走模块属性。
import tasks_tiers as _tiers
from tasks_tiers import (  # noqa: F401  —— 对外门面
    UPSTREAM_TRIM, UPSTREAM_HEAD, UPSTREAM_TAIL,
    TASK_STATUSES, TERMINAL_STATUSES, PARKED_STATUSES, SUBMITTED_STATUSES,
    TASK_RETRIEVAL, RETRIEVAL_TRIM, RETRIEVAL_TIMEOUT_S,
    TASK_SKILL_INJECT, TASK_SKILL_MAX_CHARS, TASK_SKILL_TIMEOUT_S,
    TASK_TIMEOUT_S, MAX_CONCURRENCY, MAX_TASKS, LEADER_MAXN, LOOP_SLEEP_S, AUTO_ACCEPT,
    MAX_AUTO_RETRY, MAX_ROUNDS, WAIT_RESUME_S, MAX_WAKES,
    set_context, budget_exceeded, set_run_budget, run_spent, run_budget_exceeded,
    format_upstream, can_accept, should_retry, escalate_target,
    retrieval_block, skill_inject_block,
    # 私有名也导出：外部（测试/runner）历史上直接读它们，删掉就是**静默少符号**
    _now, _ctx, _known_agent_ids, _next_escalation_note, _csv_env,
    _ccr_model_agents, _catalog_snapshot, _plan_tiers, _schedule_exec_retry,
    _skill_inject_for, _retrieval_for,
)

import tasks_runner as _runner
import tasks_dag as _dag
import tasks_replan as _replan
from tasks_runner import (  # noqa: F401
    _runners, _start_runner, _chat_arity, _accepts_kwarg, _call_chat,
    _prepare_prompt, _resolve_exec_target, _attach_retrieval,
    _settle_self_failure, _verify_and_settle, _run_task, _execute_run,
    sweep_stale_tasks, resume_expired_waits,
)
from tasks_dag import (  # noqa: F401
    LEADER_PROMPT, DecomposeIn, _agent_model_block, _extract_json_array,
    _model_allowed, _clamp_difficulty, _validate_dag, _create_run,
    _allowed_models_for, _model_choices, decompose,
    SYNTH_ID, SYNTH_TASK, _leaves, ensure_synthesis,
)
from tasks_replan import (  # noqa: F401
    REPLAN_PROMPT, ReplanIn, run_meta, _ensure_run_row, _replan_digest,
    _replan_once, _maybe_replan, replan_run, failure_signature, replan_gate,
)

#: 子模块自带 router（拆解/重拆各一条）—— 路径与挂载顺序都不变
router.include_router(_dag.router)
router.include_router(_replan.router)
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
    # 2026-10-29 升档重试：记**从哪一档升上来**。不记的话，一次"升档后又失败"在库里
    # 与"同一档又失败"长得一模一样，复盘必然把"升档无效"读成"能力不足"。
    ("escalated_from_agent", "TEXT"),
    ("escalated_from_model", "TEXT"),
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
    # difficulty 1~3（1 简单/2 普通/3 困难），只用于**起始档位与复盘**，不参与预算与判活：
    # 拿它当阈值就是拿模型的自评当事实（`tiercfg.py` 顶部有两条文献判据）。
    ("difficulty", "INTEGER"),
    # 2026-10-29 难度→档位：`tier` / `model_planned` 是**拆解时的决议**，与 `model`
    # （执行后回包里的实发模型）三列三个口径。不记 `model_planned` 就分不清
    # 「按档位发的」与「Leader 显式点的」；不记 `tier` 就没法算难度到底有没有用。
    ("tier", "TEXT"),
    ("model_planned", "TEXT"),
    ("tier_reason", "TEXT"),
    # 执行者被难度地板抬上来的来源（空 = 没动）。与 `escalated_from_*` 是两个阶段：
    # 这里发生在**拆解期**（还没花钱），escalated 发生在**失败后**（已经花过一次）。
    ("agent_promoted_from", "TEXT"),
)


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


@router.get("/api/tasks/pool")
async def dispatch_pool():
    """当前**真能派活**的 agent 与各自被挡在池外的原因（v0.13.175 扩池的可视面）。

    为什么要这个端点：池子从 2 家扩到 10 家之后，「为什么 Leader 没选 dsh」必须
    能一眼回答 —— 是二进制不在、凭据缺、还是它根本不在池里。模块里的
    `adapter_status()` 是同一份判定，这里只做出口。
    """
    import registry, tiercfg as tierlib
    status = registry.adapter_status()
    return {"ids": registry.dispatchable_ids(), "agents": status,
            "tiers": tierlib.parse_tiers(),
            "escalate": {"models": _tiers._csv_env("TASK_ESCALATE_MODELS"),
                         "ladder": _tiers._csv_env("TASK_ESCALATE_LADDER", "claude-code,codex")}}


@router.get("/api/tasks/tiers")
async def dispatch_tiers():
    """难度→档位的**配置**与**证伪数据**（v0.13.176）。

    为什么要第二个接口：档位顺序（t1<t2<t3）是**声明**不是测量。
    没有这个接口，`difficulty` 就只是个"看着很聪明"的字段 ——
    判断它有没有用，只能靠 `audit` 里 difficulty 与首轮成功率的关系。
    样本不足 10 条时**不给结论**：5 条样本算出来的比率是噪声，不是证据。
    """
    import tiercfg as tierlib
    cfg = tierlib.parse_tiers()
    mapping = tierlib.parse_mapping()
    try:
        import modelcfg
        cat = modelcfg.catalog_ids()
        ids = {str(x) for x in (cat.get("ids") or set())}
        checked = bool(cat.get("checked")) and bool(ids)
    except Exception:  # noqa: BLE001
        ids, checked = set(), False

    import registry
    pool = set(registry.dispatchable_ids())
    a_cfg = tierlib.parse_agent_tiers()
    a_floor = tierlib.parse_agent_floor()
    resolved = []
    for d in (1, 2, 3):
        t = tierlib.tier_for(d, mapping, tierlib.DEFAULT_FLOORS, cfg)
        m, why = tierlib.first_usable(t, cfg, ids, checked)
        # 用默认执行者（最常见的 jcode）走一遍地板，让"难度 3 会被抬到谁"直接可读。
        prom, frm, ar = tierlib.promote_agent("jcode", d, pool, a_cfg, a_floor)
        resolved.append({"difficulty": d, "tier": t, "model": m, "reason": why,
                         "agent_default": "jcode", "agent": prom,
                         "agent_promoted_from": frm, "agent_reason": ar})

    rows = db.query(
        "SELECT difficulty, COUNT(*) n,"
        " SUM(CASE WHEN status='success' AND COALESCE(attempts,0)=0 THEN 1 ELSE 0 END) first_pass,"
        " SUM(CASE WHEN COALESCE(attempts,0)>0 THEN 1 ELSE 0 END) retried,"
        " SUM(CASE WHEN COALESCE(escalated_from_agent,'')<>''"
        "           OR COALESCE(escalated_from_model,'')<>'' THEN 1 ELSE 0 END) escalated,"
        " COALESCE(SUM(spent_tokens),0) tokens,"
        " COALESCE(SUM(spent_ms),0) ms"
        " FROM tasks WHERE difficulty IS NOT NULL GROUP BY difficulty ORDER BY difficulty")
    # 模型档位**到底生效没有**：计划模型 ≠ 回包模型 ⇒ 网关改写了（本机 2026-10-29 实测
    # 5 个 ID 全回 agnes-2.5-flash）。这个数不报，"档位映射"就是自我感觉良好。
    mt = db.query("SELECT COUNT(*) n,"
                  " SUM(CASE WHEN COALESCE(model,'')<>'' THEN 1 ELSE 0 END) observed,"
                  " SUM(CASE WHEN COALESCE(model_planned,'')<>'' AND COALESCE(model,'')<>''"
                  "           AND model_planned<>model THEN 1 ELSE 0 END) mismatch"
                  " FROM tasks WHERE COALESCE(model_planned,'')<>''")[0]
    mt_n, mt_o, mt_m = int(mt["n"] or 0), int(mt["observed"] or 0), int(mt["mismatch"] or 0)
    # 分母只用**有观测值**的样本：`model` 为空说明回包没带模型，那不是"生效"，是"没观测"。
    # 把没观测算进分母＝制造一个好看的 100%，正是本模块要防的假绿。
    model_eff = {"planned": mt_n, "observed": mt_o, "rewritten": mt_m,
                 "effective_rate": round(1 - mt_m / mt_o, 3) if mt_o else None,
                 "note": ("回包模型≠计划模型 ⇒ 网关改写了 ID，档位不影响能力"
                          if mt_o else "无观测值（回包未带 model）⇒ 判不了，不是 0% 也不是 100%")}

    audit = []
    for r in rows:
        n = int(r["n"] or 0)
        fp = int(r["first_pass"] or 0)
        audit.append({"difficulty": r["difficulty"], "n": n,
                      "first_pass_rate": round(fp / n, 3) if n else None,
                      "retried": int(r["retried"] or 0), "escalated": int(r["escalated"] or 0),
                      "tokens": int(r["tokens"] or 0), "ms": int(r["ms"] or 0)})

    verdict = "样本不足：无法判断难度与结果是否相关"
    big = [a for a in audit if a["n"] >= 10]
    if len(big) >= 2:
        rates = [a["first_pass_rate"] for a in big if a["first_pass_rate"] is not None]
        spread = round(max(rates) - min(rates), 3) if len(rates) >= 2 else None
        if spread is not None and spread < 0.1:
            verdict = ("难度与首轮成功率无显著关联（极差 %.3f < 0.1）⇒ "
                       "该映射没有证据支持，应考虑删掉而不是继续调阈值" % spread)
        elif spread is not None:
            verdict = "难度与首轮成功率极差 %.3f（n≥10 的各档），映射值得保留并继续观察" % spread

    return {"ok": True, "order": tierlib.order(cfg), "tiers": cfg,
            "mapping": {str(k): v for k, v in sorted(mapping.items())},
            "floors": {str(k): v for k, v in sorted(tierlib.DEFAULT_FLOORS.items())},
            "catalog": {"checked": checked, "size": len(ids)},
            "agent_order": sorted(a_cfg.keys()),
            "agent_tiers": a_cfg,
            "agent_floor": {str(k): v for k, v in sorted(a_floor.items())},
            "model_tier_effective": model_eff,
            "resolved": resolved, "audit": audit, "verdict": verdict}


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
            "run": _replan.run_meta(run_id), "max_rounds": _tiers.MAX_ROUNDS}


@router.post("/api/tasks/{run_id}/start")
async def start_run(run_id: str):
    n = db.query("SELECT COUNT(*) c FROM tasks WHERE run_id=?", (run_id,))[0]["c"]
    if not n:
        raise HTTPException(404, "run not found")
    _runner._start_runner(run_id)
    return {"run_id": run_id, "status": "scheduled"}


@router.post("/api/tasks/{run_id}/retry")
async def retry_run(run_id: str, body: RetryIn):
    if body.task_id:
        db.execute("UPDATE tasks SET status='pending',error=NULL,updated_at=? "
                   "WHERE run_id=? AND task_id=?", (_tiers._now(), run_id, body.task_id))
    else:
        db.execute("UPDATE tasks SET status='pending',error=NULL,updated_at=? "
                   "WHERE run_id=? AND status IN ('failed','blocked')", (_tiers._now(), run_id))
    _runner._start_runner(run_id)
    return {"run_id": run_id, "status": "retry-scheduled"}


@router.post("/api/tasks/{run_id}/accept")
async def accept_run(run_id: str, body: AcceptIn = AcceptIn()):
    """P1-4：接受已提交的产物（`submitted` → `success`）。

    **只有 `submitted` 接受**：重复接受是 no-op（不篡改已 success 的行，
    也不把 failed/running 洗成成功）。接受后重启 runner，让下游就绪。
    """
    now = _tiers._now()
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
    _runner._start_runner(run_id)
    return {"run_id": run_id, "accepted": n, "by": who}


@router.post("/api/tasks/{run_id}/reject")
async def reject_run(run_id: str, body: RejectIn = RejectIn()):
    """P1-4：驳回已提交的产物（`submitted` → `failed`）。

    驳回后下游按「上游失败」阻塞（`_execute_run` 的既有判据），而不是默默带着
    不合格产物继续跑。"""
    now = _tiers._now()
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
    _runner._start_runner(run_id)
    return {"run_id": run_id, "rejected": n}


def requeue_waiting(run_id: str) -> int:
    """等待态（waiting_*/paused）→ pending，返回改动行数（P0-3）。

    等待态让出了执行资源，run 已挂起；恢复必须**显式**把任务重新排队，
    再重启 runner（由调用方 `_start_runner`）。
    §7-②：人工唤醒**也计数**（`wake_count+1`）并清掉旧期限 —— 否则可被无限人工续命，
    「有界」就名存实亡。
    """
    now = _tiers._now()
    ph = ",".join("?" * len(_tiers.PARKED_STATUSES))
    return db.execute(
        "UPDATE tasks SET status='pending',error=NULL,wait_until=NULL,"
        "wake_count=COALESCE(wake_count,0)+1,updated_at=? "
        "WHERE run_id=? AND status IN (%s)" % ph,
        (now, run_id, *_tiers.PARKED_STATUSES))


@router.post("/api/tasks/{run_id}/resume")
async def resume_run(run_id: str):
    """等待态 → pending 并重启 runner（人在外部输入/批准后调）。"""
    n = requeue_waiting(run_id)
    if not n:
        raise HTTPException(404, "no waiting task for run")
    _runner._start_runner(run_id)
    return {"run_id": run_id, "status": "resumed", "requeued": n}


@router.post("/api/tasks/{run_id}/pause")
async def pause_run(run_id: str):
    """把可停的任务（pending / 等待态）置 paused。**不动正在执行的任务**。"""
    now = _tiers._now()
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
    if run_id in _runner._runners:
        _runner._runners[run_id].cancel()
        _runner._runners.pop(run_id)
    now = _tiers._now()
    n = db.execute(
        "UPDATE tasks SET status='cancelled',finished_at=?,updated_at=? "
        "WHERE run_id=? AND status NOT IN ('success','failed','blocked','cancelled')",
        (now, now, run_id))
    if not n:
        raise HTTPException(404, "no cancellable task for run")
    return {"run_id": run_id, "status": "cancelled", "cancelled": n}


@router.delete("/api/tasks/{run_id}")
async def delete_run(run_id: str):
    if run_id in _runner._runners:
        _runner._runners[run_id].cancel()
        _runner._runners.pop(run_id)
    n = db.execute("DELETE FROM tasks WHERE run_id=?", (run_id,))
    if not n:
        raise HTTPException(404, "run not found")
    return {"status": "deleted", "run_id": run_id}


