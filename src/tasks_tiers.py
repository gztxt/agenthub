"""任务分层与检索注入（tasks.py 的纯逻辑层，PT-20261031-02 片3 文件级拆分）。

搬运自 `tasks.py` 45–572 段，**不改任何行为**：状态枚举、预算、升档/重试、检索块。
为什么单独一层：这些全是**纯函数/纯判定**，可 L0 直测，不需要 DB 或协程环境；
留在 1900 行的编排文件里时，任何一次改动都要跑整条任务链才敢信。

跨模块引用纪律（见 `agent-knowledge/` 片1 教训）：外部读这些名字一律经模块属性
（`tasks_tiers.X`），不要在别处 `from tasks_tiers import X` 后再打桩到本模块——
名字拷贝会让补丁静默失效。
"""
import asyncio
import os
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional

import db
import verifier


# ── 运行期配置（2026-10-31 随片3 下移到本层）────────────────────────
# 谁需要它们谁读 `_tiers.X`；**本层是唯一定义处**，别再在上层留第二份。
TASK_TIMEOUT_S = int(os.getenv("TASK_TIMEOUT_S", "600"))
MAX_CONCURRENCY = 4
MAX_TASKS = 12
#: 告诉 Leader 的条数上限 = 硬上限 − 1：**留一格给代码追加的汇总节点**
#: （`ensure_synthesis`）。不留这一格，Leader 顶满 12 条时多叶子就拿不到汇总，
#: 又回到「全绿无结论」。这是结构性预留，不是保守取值。
LEADER_MAXN = MAX_TASKS - 1
#: 调度循环步长（秒）。做成常量是为了让 L0 闸门能把它压到毫秒级（不起真等待）。
LOOP_SLEEP_S = float(os.getenv("TASK_LOOP_SLEEP_S", "1.5"))

#: 过了 verifier 后是否**自动接受**（= P1-4 之前的行为，默认开）。
#: 置 0 则任务停在 `submitted` 等外部 `POST /accept`。
#: 做成**模块级常量**（不是每次读 env）：与 MAX_CONCURRENCY/LOOP_SLEEP_S 同口径，测试可整体替换。
AUTO_ACCEPT = os.getenv("TASK_AUTO_ACCEPT", "1").strip().lower() not in ("0", "false", "no", "off")

UPSTREAM_TRIM = 4000
#: P1-3：截断预算**只算正文**，标记开销另计（标记必须存在，否则下游无法分辨
#: 「上游只写了这么多」与「被我砍掉了」—— 静默截断是最坏形态，见 format_upstream 文档串）。
#: 头尾比例 1:1：结论通常在尾部，整体砍尾丢的正是结论。
UPSTREAM_HEAD = UPSTREAM_TRIM // 2
UPSTREAM_TAIL = UPSTREAM_TRIM - UPSTREAM_HEAD


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


#: B1（2026-10-22）：子任务派发前是否跑一次接单前置检索，把线索拼进 prompt。
#: 默认开；`TASK_RETRIEVAL=0` 关掉（回归对比 / 离线跑测用）。
TASK_RETRIEVAL = os.getenv("TASK_RETRIEVAL", "1").strip().lower() not in ("0", "false", "no", "off")
#: 检索块的正文预算（字符）。与 UPSTREAM_TRIM 同一口径：超了要带**显式省略标记**，
#: 不能让下游分不清「上游只给了这么多」与「被砍掉了」。
RETRIEVAL_TRIM = int(os.getenv("TASK_RETRIEVAL_TRIM", "1200"))
#: 检索自身的超时（秒）。检索只是增强，**绝不能拖死一个子任务**：
#: 超时就当「本次无线索」进 prompt（并显式写出来），子任务照跑。
RETRIEVAL_TIMEOUT_S = float(os.getenv("TASK_RETRIEVAL_TIMEOUT_S", "8"))


#: **分步任务必须先拿技能说明书**（用户 2026-10-31 裁定「执行 3」）。
#:
#: 背景：B1 只把「技能命中」写成一行提示（`hub_skill_auto(task=...)`），要不要去调
#: 全靠执行者自觉 —— 实测子任务直接进 LLM，把本该照说明书做的活重新发明一遍，
#: 既慢又偏。这里改成**派发时就把正文注入 prompt**：本地优先（BM25 归一化 ≥0.40），
#: 不够像才走商店（受 `SKILL_STORE_AUTO` / 小时额度 / 质量门槛约束）。
#:
#: 三条边界，缺一条就会变成新的「静默不可用」：
#: ① 失败**不阻塞**子任务（技能是增强，不是前提），但必须把原因写进 `retrieval_json`；
#: ② 正文按 `TASK_SKILL_MAX_CHARS` 截断并**显式标出省略量**（同 `format_upstream` 纪律）；
#: ③ `TASK_SKILL_INJECT=0` 可整体关掉（离线跑测 / 回归对照）。
TASK_SKILL_INJECT = os.getenv("TASK_SKILL_INJECT", "1").strip().lower() not in ("0", "false", "no", "off")
TASK_SKILL_MAX_CHARS = int(os.getenv("TASK_SKILL_MAX_CHARS", "6000"))
TASK_SKILL_TIMEOUT_S = float(os.getenv("TASK_SKILL_TIMEOUT_S", "12"))
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


def _known_agent_ids() -> set:
    """当前可用派发池（`set_context` 的 agent_ids_fn，即 `dispatchable_ids()`）。"""
    fn = _ctx.get("agent_ids_fn")
    if not fn:
        return set()
    try:
        return set(fn())
    except Exception:  # noqa: BLE001 —— 池子读不出来只影响升档，不许带倒任务
        return set()


def _next_escalation_note(agent_id: str, model: Optional[str], next_attempt: int,
                          tier: Optional[str] = None) -> str:
    """写进 error 字段的一句话：下一次重试会升到哪 —— 没有目标就直说没有。"""
    cat, checked = _catalog_snapshot()
    t = escalate_target(agent_id, model, next_attempt, _known_agent_ids(), tier=tier,
                        catalog=cat, catalog_checked=checked)
    return t["reason"]


def _csv_env(name: str, default: str = "") -> List[str]:
    raw = os.getenv(name, default) or ""
    return [x.strip() for x in raw.split(",") if x.strip()]


def _ccr_model_agents() -> set:
    """模型域 = CCR 的 agent（只有它们的 `--model` 能吃同一个模型 ID）。"""
    try:
        from adapters.cli_agent import SPECS as _CLI
        return {"claude", "jcode"} | {a for a, sp in _CLI.items()
                                      if sp.get("model_path") == "ccr"}
    except Exception:  # noqa: BLE001
        return {"claude", "jcode"}


def _catalog_snapshot() -> tuple:
    """CCR 当前可用模型清单 → `(ids, checked)`。取不到就 `(set(), False)`。

    **升档和拆解必须用同一份清单**：2026-10-29 实测 `escalate_target` 的三处调用
    都没传 `catalog`，`first_usable()` 恒返回 None ⇒ 档位升档路径**运行时永不生效**，
    每次都掉到 agent 阶梯（claude-code 超时后换成了 codex，而不是升到 t2 模型）。
    只写单测看不出来，因为单测自己传了 catalog。
    """
    try:
        import modelcfg
        cat = modelcfg.catalog_ids()
        ids = {str(x) for x in (cat.get("ids") or set())}
        return ids, bool(cat.get("checked")) and bool(ids)
    except Exception:  # noqa: BLE001 — 选型是增强，不许带倒任何主流程
        return set(), False


def _plan_tiers(items: List[dict], pool: set) -> tuple:
    """难度→起始档位的**接线层**：只负责取清单与池子，判断全在 `tiercfg.py`（纯函数）。

    清单取不到（网关挂/无凭据）时 `checked=False` ⇒ 不注入任何模型，
    原因写 `catalog_unavailable`。这里绝不"给个默认值凑合"——
    那会把「不知道」伪装成「选过了」。
    """
    import tiercfg as tierlib
    ids, checked = _catalog_snapshot()
    return tierlib.plan(items, catalog=ids, checked=checked, ccr_agents=_ccr_model_agents(),
                        pool=pool)


def escalate_target(agent_id: str, model_choice: Optional[str], attempts: Any,
                    known: set, ccr_agents: Optional[set] = None,
                    tier: Optional[str] = None, tiers_cfg: Optional[dict] = None,
                    catalog: Optional[set] = None,
                    catalog_checked: bool = False) -> Dict[str, Any]:
    """失败重试的**升档**目标（纯函数，可单测）。

    旧行为（v0.13.174 之前）：重试＝同 agent、同模型原地再跑一遍，只是把上次的
    `verify_reason` 塞进 prompt —— 同一执行者、同一能力，失败原因若是"能力不够"
    就必然再失败一次，白烧一轮钱。这里改成 cascade：**换一档**再试。

    两条路径，按优先级：
      ① 模型升档：`TASK_ESCALATE_MODELS`（逗号分隔），第 N 次重试用第 N 项。
         只对模型域 = CCR 的 agent 生效 —— 给 codebuddy/cursor 塞 CCR 模型 ID 必报
         `unknown model`，那是把"升档"做成"必失败"。
      ② agent 升档：没有模型通道时换阶梯里的下一个（`TASK_ESCALATE_LADDER`，
         默认 `claude-code,codex`，工具环最完整的两家），且必须在**当前可用池**里。

    都不成立时**原样重跑但写明 reason**（`no_escalation_path`）——
    静默当已升档会让复盘把"同一档又输一次"读成"升档无效"。
    """
    try:
        n = int(attempts or 0)
    except (TypeError, ValueError):
        n = 0
    base = {"agent_id": agent_id, "model": model_choice, "changed": False,
            "from_agent": agent_id, "from_model": model_choice}
    if n <= 0:
        return dict(base, reason="first_attempt")

    # ⓪ 档位路径（v0.13.176）：难度决定了起点档，失败就**上一档**，而不是跳到一个
    #    和当前档无关的模型。已在最高档 ⇒ 落到 agent 阶梯。永不向下（next_tier 只向上）。
    if tier and tier not in ("explicit", "native"):
        import tiercfg as tierlib
        cfg = tiers_cfg if tiers_cfg is not None else tierlib.parse_tiers()
        nxt = tierlib.next_tier(tier, cfg)
        if nxt and agent_id in (ccr_agents if ccr_agents is not None else _ccr_model_agents()):
            m, why = tierlib.first_usable(nxt, cfg, catalog, catalog_checked)
            if m and m != (model_choice or ""):
                return dict(base, agent_id=agent_id, model=m, changed=True,
                            reason="tier:%s:%s" % (nxt, m))

    chain = _csv_env("TASK_ESCALATE_MODELS")
    if chain and agent_id in (ccr_agents if ccr_agents is not None else _ccr_model_agents()):
        m = chain[min(n - 1, len(chain) - 1)]
        if m and m != (model_choice or ""):
            return dict(base, agent_id=agent_id, model=m, changed=True,
                        reason="model:%s" % m)

    for cand in _csv_env("TASK_ESCALATE_LADDER", "claude-code,codex"):
        if cand != agent_id and cand in known:
            return dict(base, agent_id=cand, model=None, changed=True,
                        reason="agent:%s" % cand)

    return dict(base, reason="no_escalation_path")


def _schedule_exec_retry(run_id: str, row: dict, err: str, reason: str,
                         attempts_done: int, exec_agent: str, exec_model: Optional[str],
                         *, dur: int = 0, spent_tokens: int = 0,
                         used_model: Optional[str] = None, kind: str = "",
                         detail: Optional[dict] = None) -> bool:
    """执行者自身失败（异常/超时/429）→ 有升档路径就排一次重试，返回是否已排。

    为什么单独抽出来：`_execute_task` 里现在有**三条**执行失败出口（自称失败、
    超时、任意异常），各写一遍重试必然漂移（2026-10-29 就是只给 verifier 失败
    写了重试，执行失败三处全漏 ⇒ dsh 429 直接终态、汇总永远跑不到）。
    """
    if attempts_done >= MAX_AUTO_RETRY:
        return False
    cat, checked = _catalog_snapshot()
    esc = escalate_target(exec_agent, exec_model, attempts_done + 1,
                          _known_agent_ids(), tier=row.get("tier"),
                          catalog=cat, catalog_checked=checked)
    if not esc.get("changed"):
        return False
    db.execute("UPDATE tasks SET status='pending',attempts=?,error=?,verify_reason=?,"
               "spent_ms=?,spent_tokens=?,model=?,finished_at=NULL,updated_at=?"
               " WHERE run_id=? AND task_id=?",
               (attempts_done + 1,
                ("%s（自动重试 %d/%d，升档 %s→%s）"
                 % (err, attempts_done + 1, MAX_AUTO_RETRY,
                    exec_agent, esc.get("agent_id")))[:500],
                reason[:500], dur, spent_tokens, used_model, _now(),
                run_id, row["task_id"]))
    db.log_profile_event("task_exec", row["agent_id"], "retry", dur,
                         trace_id=row["trace_id"],
                         detail=dict({"task": row["task_id"], "verify": kind,
                                      "exec_failed": True,
                                      "attempt": attempts_done + 1,
                                      "escalate_to": esc.get("agent_id"),
                                      "escalate_reason": esc.get("reason")},
                                     **(detail or {})))
    return True


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


def skill_inject_block(got: Dict[str, Any], limit: int = 0) -> str:
    """把 acquire 结果压成**注入给执行者**的技能块（纯函数，L0 可直测）。

    只在真的拿到正文时出块：`ok=true` 但正文为空（`with_body=false` 场景）
    说明「找到技能但没取内容」——那种情况给一行指引比给个空标题有用。
    截断**显式**：尾部写省略了多少字符，否则下游分不清「说明书就这么短」与「被砍了」。
    """
    limit = limit or TASK_SKILL_MAX_CHARS
    if not isinstance(got, dict) or not got.get("ok"):
        return ""
    name = got.get("name") or "?"
    route = got.get("route") or "?"
    src = got.get("source") or "local"
    body = str(got.get("content") or "")
    head = ("—— 技能说明书（%s · 来源 %s；**按它做，别自己重发明**）——\n" % (name, src))
    if not body:
        return head + "- 已定位技能 `%s`（route=%s），正文未取回：需要细节时调 `hub_skill_invoke(name=\"%s\")`。\n" % (name, route, name)
    keep = max(0, limit - len(head))
    if len(body) > keep:
        tail = "\n……（技能正文已省略 %d 字符；需要全文调 hub_skill_invoke(name=\"%s\")）……" % (len(body) - keep, name)
        body = body[:keep] + tail
    return head + body + "\n"


async def _skill_inject_for(goal: str, prompt: str, agent_id: str) -> tuple:
    """派发前取一次技能说明书，返回 `(块文本, 落库用的 dict)`。**任何失败都不阻塞**。

    调 `skill_store.acquire`（与 `/api/skill/auto` 同一条链路，不另立门槛 ——
    两套门槛必然漂移，漂移形态是「同一任务两个入口选到不同技能」）。
    """
    import skill_store as store_mod        # 局部导入：skill_store 依赖 skill，顶层会成环
    t0 = asyncio.get_event_loop().time()
    meta: Dict[str, Any] = {"enabled": TASK_SKILL_INJECT, "ok": False}
    try:
        got = await asyncio.wait_for(
            store_mod.acquire((goal + " :: " + prompt)[:1500], caller="task:%s" % (agent_id or "?"),
                              task=(goal or "")[:500], allow_install=True, local_only=False),
            timeout=TASK_SKILL_TIMEOUT_S)
    except asyncio.TimeoutError:
        meta["error"] = "超时 %.0fs" % TASK_SKILL_TIMEOUT_S
        meta["ms"] = int((asyncio.get_event_loop().time() - t0) * 1000)
        return ("", meta)
    except Exception as e:  # noqa: BLE001 —— 技能链路坏了不许带倒子任务
        meta["error"] = "%s: %s" % (type(e).__name__, str(e)[:200])
        meta["ms"] = int((asyncio.get_event_loop().time() - t0) * 1000)
        return ("", meta)
    meta.update({"ok": bool(got.get("ok")), "source": got.get("source"),
                 "name": got.get("name"), "route": got.get("route"),
                 "norm": (got.get("local") or {}).get("norm"),
                 "installed": bool((got.get("store") or {}).get("installed")),
                 "reason": (got.get("store") or {}).get("reason") or got.get("reason"),
                 "ms": int((asyncio.get_event_loop().time() - t0) * 1000)})
    return (skill_inject_block(got), meta)


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
