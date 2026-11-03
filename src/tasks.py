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
    # 2026-11-03 协同页四框：轮次上限与配置模型归因（纯函数，测试直测）
    ROUNDS_CEILING, ROUND_CHOICES, clamp_rounds, error_bucket,
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
    _allowed_models_for, _model_choices, _apply_pinned_model, decompose,
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
    # 2026-11-01：重启恢复注记（派发时拼进 prompt 最前，拼完即清空）
    ("recovery_note", "TEXT"),
    # 2026-11-03：拆解声明「会碰哪些文件」。文件重叠闸门（tasks_dag.serialize_file_overlaps）
    # 靠它把并行的同文件子任务强制串行；空数组 = 纯推理/检索类（不含文件读写）。
    # 之所以落库而不是只存在内存里：复盘要能回答「这次为什么串行」而不必重跑拆解。
    ("files", "TEXT"),
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
    -- 2026-11-01 重启恢复注记（正文见 tasks_runner.RECOVERY_NOTE）。NULL = 正常派发；
    -- 非空 = 上次被服务重启打断，派发时拼进 prompt 最前面，**拼完即清空**（单次使用）。
    recovery_note TEXT,
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
    -- 2026-11-03（协同页轮次框）：本轮声明的**总轮次上限**。NULL = 存量行/老调用方
    -- ⇒ 重拆层回落 `TASK_MAX_ROUNDS`（默认 2），行为与改版前逐字一致。
    -- 单轮 run 也存 1：那张 run 的 LEFT JOIN 自然属于它自己。
    max_rounds INTEGER,
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
    # 2026-11-03：`task_runs.max_rounds` 同样幂等补列。存量库上 `CREATE TABLE IF NOT EXISTS`
    # 不会改已存在表，不补这一步新列就永远不存在 ⇒ 新建 run 的轮次上限落不了库。
    rcols = {r["name"] for r in db.query("PRAGMA table_info(task_runs)")}
    if "max_rounds" not in rcols:
        db.execute("ALTER TABLE task_runs ADD COLUMN max_rounds INTEGER")


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
            # 2026-11-03：轮次档位与硬上限的**唯一真值**（前端下拉只渲染这份，
            # 不另存 [1,2,3,5]，否则后端调上限时前端仍挂旧档）。
            "rounds": {"choices": list(_tiers.ROUND_CHOICES),
                       "ceiling": _tiers.ROUNDS_CEILING,
                       "default": 1},
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


#: run 聚合的**唯一 SQL 口径**（列表 / 汇总 / 筛选三处共用）。
#: 拆成常量而不是写三遍：三份 GROUP BY 各自漂移过一次（「列表说失败、筛选说部分」）。
#: `agents` / `models` 用 GROUP_CONCAT 去重后供「按组合筛」用；`COALESCE(model, model_planned,
#: model_choice)` 的优先级是**观测值优先**（`model` 是回包真实模型，2026-10-29 网关改写
#: 的证据就在这个差异里）。
_RUN_AGG_SQL = """
    SELECT t.run_id, MIN(t.trace_id) trace_id, MAX(t.goal) goal, COUNT(*) total,
           SUM(CASE WHEN t.status='success' THEN 1 ELSE 0 END) success,
           SUM(CASE WHEN t.status='failed' THEN 1 ELSE 0 END) failed,
           SUM(CASE WHEN t.status='blocked' THEN 1 ELSE 0 END) blocked,
           SUM(CASE WHEN t.status='running' THEN 1 ELSE 0 END) running,
           SUM(CASE WHEN t.status='pending' THEN 1 ELSE 0 END) pending,
           SUM(CASE WHEN t.status IN ('waiting_input','waiting_approval','paused')
                    THEN 1 ELSE 0 END) waiting,
           SUM(CASE WHEN t.status='submitted' THEN 1 ELSE 0 END) submitted,
           MIN(t.created_at) created_at, MAX(t.finished_at) finished_at,
           MAX(COALESCE(r.round,1)) round, MAX(r.parent_run_id) parent_run_id,
           MAX(r.child_run_id) child_run_id, MAX(r.stop_reason) stop_reason,
           MAX(r.max_rounds) max_rounds, MAX(r.auto_rounds) auto_rounds,
           GROUP_CONCAT(DISTINCT t.agent_id) agents,
           GROUP_CONCAT(DISTINCT COALESCE(NULLIF(t.model,''), NULLIF(t.model_planned,''),
                                          NULLIF(t.model_choice,''), '')) models,
           COALESCE(SUM(t.duration_ms),0) duration_ms,
           COALESCE(SUM(t.spent_tokens),0) tokens,
           MAX(t.updated_at) updated_at
    FROM tasks t LEFT JOIN task_runs r ON r.run_id = t.run_id
    GROUP BY t.run_id
"""

#: run 状态判定（唯一定义处，同 `_RUN_AGG_SQL`）。
_RUN_STATE_SQL = """CASE WHEN a.running > 0 THEN 'running'
      WHEN a.success = a.total THEN 'success'
      WHEN a.submitted > 0 THEN 'awaiting_accept'
      WHEN a.waiting > 0 THEN 'waiting'
      WHEN a.failed > 0 AND a.pending = 0 AND a.running = 0 THEN 'failed'
      WHEN a.pending = a.total THEN 'pending'
      ELSE 'partial' END"""


@router.get("/api/tasks/runs")
async def list_runs(limit: int = Query(default=20, ge=1, le=100),
                    offset: int = Query(default=0, ge=0),
                    q: str = Query(default="", max_length=120),
                    state: str = Query(default="", max_length=24),
                    agent: str = Query(default="", max_length=64),
                    model: str = Query(default="", max_length=120),
                    days: int = Query(default=0, ge=0, le=3650)):
    """协同历史（2026-11-03）：搜索 / 状态 / agent / 模型 / 时间窗 / 分页。

    为何改成 CTE 两层：`state` 是聚合出来的派生列，SQLite 不允许在 WHERE 里引用 SELECT 别名；
    原实现是在 Python 里逐行补 `state` 后直接返回 20 条——那样「按状态筛」只能筛前 20 条，
    等于给了个只能看到最近 20 条的筛子。现在过滤与分页都在 SQL 里做，`total` 是同口径计数。
    """
    where, args = [], []
    if q.strip():
        where.append("(run_id LIKE ? OR goal LIKE ?)")
        like = "%" + q.strip() + "%"
        args += [like, like]
    if state:
        where.append("state = ?")
        args.append(state)
    if agent.strip():
        where.append("COALESCE(agents,'') LIKE ?")
        args.append("%" + agent.strip() + "%")
    if model.strip():
        where.append("COALESCE(models,'') LIKE ?")
        args.append("%" + model.strip() + "%")
    if days:
        where.append("created_at >= ?")
        args.append(_days_ago(days))
    w = (" WHERE " + " AND ".join(where)) if where else ""
    cte = ("WITH a AS (%s), st AS (SELECT a.*, %s AS state FROM a)" % (_RUN_AGG_SQL, _RUN_STATE_SQL))
    total = db.query(cte + " SELECT COUNT(*) c FROM st" + w, tuple(args))[0]["c"]
    rows = db.query(cte + " SELECT * FROM st" + w +
                    " ORDER BY created_at DESC LIMIT ? OFFSET ?",
                    tuple(args) + (limit, offset))
    return {"runs": rows, "total": total, "limit": limit, "offset": offset,
            "has_more": offset + len(rows) < total,
            "filters": {"q": q, "state": state, "agent": agent, "model": model, "days": days}}


def _days_ago(days: int) -> str:
    """**UTC** 的 `days` 天前，与 `tasks.created_at`（`datetime.now(timezone.utc).isoformat()`）
    同口径、可字符串比大小。

    2026-11-03 实盘缺陷（不是隐患）：初版用了 `datetime.now()`（本地无时区），
    而落库值是 `...+00:00` 的 UTC ⇒ 两者相差 8 小时，窗口边界上的任务会被错判进出。
    当时实盘看着“对”只是碰巧（本地比 UTC 快 8 小时，边界误差落在没人看的那一侧）；
    不能用“数字对得上”当通过。
    """
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=int(days))).replace(
        microsecond=0).isoformat()


@router.get("/api/tasks/weights")
async def task_weights(days: int = Query(default=30, ge=1, le=3650)):
    """派发权重排名（2026-11-03）：agent × 模型 的质量 / 效率 / 成本三维。

    为什么单独一个端点、而不是塞进 `/api/tasks/stats`：stats 是**看过去**（发生了什么），
    这里是**定未来**（下次派谁）。两者口径不同 —— stats 的取消/阻塞要计入「发生过」，
    权重只该按「值不值得再派」算（成本按每成功一次 token，失败任务不进效率分母）。

    ⚠ 本端点**只读建议，不改派发**：选型仍走 `tasks_tiers` 的档位层。
    权重直接接管选型需要先跑 A/B，已登记 PENDING-TASKS，别在这里偷偷改行为。
    """
    import dispatch_weights
    return dispatch_weights.compute(days=days)


@router.get("/api/tasks/stats")
async def task_stats(days: int = Query(default=30, ge=0, le=3650),
                     top: int = Query(default=5, ge=1, le=20)):
    """执行结果汇总（2026-11-03）：「哪套 agent×模型 真能跑成、跑不成是因为什么」。

    三块，每块都带**样本量**：没有 n 的比率不许上屏（本仓假绿判例反复昭昭）。
      · summary  —— run 级分布 / 成功率 / 耗时 / token；
      · combos   —— agent × 模型（观测优先）的成败、重试、升档、均耗时；
      · failures —— 失败原因归因桶（`error_bucket`）+ 每个桶的代表 agent 与样例。
    样本不足 10 条时 `verdict` 明写「样本不足」，不拿 3 条样本算百分比当结论。
    """
    since = _days_ago(days) if days else ""

    def _win(col: str = "created_at") -> str:
        """时间窗子句（`col` 供 JOIN 时加表别名）；空窗返回空串 —— 不拼半截 WHERE。"""
        return (" WHERE " + col + " >= ?") if since else ""

    def _win_and(col: str = "created_at") -> str:
        return (" AND " + col + " >= ?") if since else ""

    targs = (since,) if since else ()

    # 时间窗必须与任务侧**同一个**：初版漏了这一句，于是 days=7 时「run 15 个」与
    # 「轮次表 14 个 run」同时上屏（同一份数据的两个窗口，2026-11-03 实盘）。
    run_rows = db.query(
        "WITH a AS (%s), st AS (SELECT a.*, %s AS state FROM a)"
        " SELECT state, COUNT(*) n FROM st" % (_RUN_AGG_SQL, _RUN_STATE_SQL)
        + _win() + " GROUP BY state", targs)
    state_counts = {r["state"]: int(r["n"] or 0) for r in run_rows}
    runs_total = sum(state_counts.values())
    ok = state_counts.get("success", 0)

    agg = db.query(
        "SELECT COUNT(*) n,"
        " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,"
        " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,"
        " SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked,"
        " SUM(CASE WHEN COALESCE(attempts,0)>0 THEN 1 ELSE 0 END) retried,"
        " COALESCE(SUM(duration_ms),0) ms, COALESCE(SUM(spent_tokens),0) tokens,"
        " AVG(duration_ms) avg_ms"
        " FROM tasks" + _win(), targs)[0]
    n = int(agg["n"] or 0)
    # 中位数：SQLite 没有 median，取排序后两条中间值（偶数取平均）。样本少时同一份算法。
    dur = [r["duration_ms"] for r in db.query(
        "SELECT duration_ms FROM tasks WHERE duration_ms IS NOT NULL"
        + _win_and() + " ORDER BY duration_ms", targs)]
    p50 = None
    if dur:
        mid = len(dur) // 2
        p50 = int(dur[mid]) if len(dur) % 2 else int((dur[mid - 1] + dur[mid]) / 2)

    combos = db.query(
        "SELECT agent_id,"
        " COALESCE(NULLIF(model,''),NULLIF(model_planned,''),NULLIF(model_choice,''),'')"
        "   AS model_eff, COUNT(*) n,"
        " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok,"
        " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,"
        " SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked,"
        " SUM(CASE WHEN COALESCE(attempts,0)>0 THEN 1 ELSE 0 END) retried,"
        " SUM(CASE WHEN COALESCE(escalated_from_agent,'')<>''"
        "           OR COALESCE(escalated_from_model,'')<>'' THEN 1 ELSE 0 END) escalated,"
        " COALESCE(SUM(duration_ms),0) ms, COALESCE(SUM(spent_tokens),0) tokens,"
        " AVG(duration_ms) avg_ms"
        " FROM tasks" + _win() + " GROUP BY agent_id, model_eff ORDER BY n DESC", targs)
    for c in combos:
        c["model_eff"] = c["model_eff"] or "(未指定)"
        c["rate"] = round(int(c["ok"] or 0) / int(c["n"] or 1), 3)
        c["avg_ms"] = int(c["avg_ms"] or 0)

    fail_rows = db.query("SELECT agent_id, error FROM tasks WHERE status IN ('failed','blocked')"
                         + _win_and(), targs)
    buckets: dict = {}
    for r in fail_rows:
        b = _tiers.error_bucket(r["error"])
        e = buckets.setdefault(b, {"bucket": b, "n": 0, "agents": {}, "sample": ""})
        e["n"] += 1
        e["agents"][r["agent_id"] or "-"] = e["agents"].get(r["agent_id"] or "-", 0) + 1
        if not e["sample"]:
            e["sample"] = str(r["error"] or "")[:160]
    failures = sorted(buckets.values(), key=lambda x: -x["n"])[:top]
    for f in failures:
        f["agents"] = sorted(f["agents"].items(), key=lambda kv: -kv[1])[:3]

    rounds = db.query(
        "SELECT COALESCE(r.round,1) round, COUNT(DISTINCT t.run_id) runs,"
        " COUNT(*) n, SUM(CASE WHEN t.status='success' THEN 1 ELSE 0 END) ok"
        " FROM tasks t LEFT JOIN task_runs r ON r.run_id=t.run_id" + _win("t.created_at") +
        # 必须 GROUP BY **表达式**，不能写 `GROUP BY round`：SQLite 同名时会优先绑到
        # 源列 `r.round`，于是「无 task_runs 行的存量 run（NULL）」会与「round=1」分成
        # 两组，两行都经 COALESCE 显示成「第 1 轮」（2026-11-03 实盘：1 组 3 条 / 1 组 35 条）。
        " GROUP BY COALESCE(r.round,1) ORDER BY COALESCE(r.round,1)", targs)

    verdict = "样本不足（%d 条任务）：不给结论，继续观察" % n if n < 10 else (
        "成功率 %.1f%%（%d/%d 个 run 全绿）；%d 条任务里 %d 条失败"
        % (100.0 * ok / runs_total if runs_total else 0.0, ok, runs_total,
           n, int(agg["failed"] or 0)))
    return {"ok": True, "days": days, "window_from": since or None,
            "summary": {"runs": runs_total, "runs_success": ok,
                        "runs_rate": round(ok / runs_total, 3) if runs_total else None,
                        "states": state_counts, "tasks": n,
                        "success": int(agg["ok"] or 0), "failed": int(agg["failed"] or 0),
                        "blocked": int(agg["blocked"] or 0),
                        "retried": int(agg["retried"] or 0),
                        "duration_ms": int(agg["ms"] or 0), "p50_ms": p50,
                        "tokens": int(agg["tokens"] or 0)},
            "combos": combos, "failures": failures, "rounds": rounds,
            "verdict": verdict}


@router.get("/api/tasks/{run_id}/events")
async def run_events(run_id: str, limit: int = Query(default=200, ge=1, le=1000)):
    """run 的执行时间线（派发/升档/验证/重拆）。

    数据源是 `profile_events`：`_create_run` 把 `trace_id` 直接写成 `run_id`，
    所以这里零迁移就能取到整条链——新建一张事件表反而会变成第二份同义记录。
    """
    rows = db.query("SELECT id,source,subject,status,duration_ms,detail,created_at"
                    " FROM profile_events WHERE trace_id=? ORDER BY id DESC LIMIT ?",
                    (run_id, limit))
    return {"run_id": run_id, "events": rows}


@router.get("/api/tasks/{run_id}/chain")
async def run_chain(run_id: str):
    """轮次链：往上游找 parent，往下游找 child，按 round 排序返回每一轮的汇总。

    用于「继续执行」：看到第 N 轮的 `stop_reason`（`max_rounds`/`same_failure`/`complete`）
    才知道下一步该点「续跑」还是「重拆」。链深度硬上限 10：环/脏数据不得把接口拖死。
    """
    seen, order = set(), []

    def _node(rid: str):
        r = db.query("SELECT * FROM task_runs WHERE run_id=?", (rid,))
        meta = dict(r[0]) if r else {"run_id": rid, "round": 1, "auto_rounds": 0,
                                     "parent_run_id": None, "child_run_id": None,
                                     "stop_reason": None, "max_rounds": None,
                                     "goal": None, "created_at": None}
        agg = db.query("SELECT COUNT(*) total,"
                       " SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) success,"
                       " SUM(CASE WHEN status='failed' THEN 1 ELSE 0 END) failed,"
                       " SUM(CASE WHEN status='blocked' THEN 1 ELSE 0 END) blocked"
                       " FROM tasks WHERE run_id=?", (rid,))
        a = dict(agg[0]) if agg else {}
        meta.update({k: int(v or 0) for k, v in a.items()})
        return meta

    cur, guard = run_id, 0
    while cur and cur not in seen and guard < 10:
        seen.add(cur)
        node = _node(cur)
        order.append(node)
        cur = node.get("parent_run_id")
        guard += 1
    order.reverse()
    cur, guard = (order[-1].get("child_run_id") if order else None), 0
    while cur and cur not in seen and guard < 10:
        seen.add(cur)
        node = _node(cur)
        order.append(node)
        cur = node.get("child_run_id")
        guard += 1
    order.sort(key=lambda x: (x.get("round") or 1, x.get("created_at") or ""))
    return {"run_id": run_id, "chain": order, "rounds": len(order),
            "ceiling": _tiers.ROUNDS_CEILING}


@router.get("/api/tasks/{run_id}")
async def get_run(run_id: str):
    rows = db.query("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at, task_id", (run_id,))
    if not rows:
        raise HTTPException(404, "run not found")
    for r in rows:
        r["deps"] = json.loads(r["deps"] or "[]")
    return {"run_id": run_id, "goal": rows[0]["goal"], "tasks": rows,
            "run": _replan.run_meta(run_id),
            # 界面上「第 N/M 轮」用这个 M：run 落库值优先，存量 run 回落全局默认。
            "max_rounds": (_replan.run_meta(run_id).get("max_rounds")
                           or _tiers.MAX_ROUNDS),
            "rounds_ceiling": _tiers.ROUNDS_CEILING}


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


