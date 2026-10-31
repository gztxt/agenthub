"""Leader 拆解：LLM 出 JSON DAG → 校验 → 落库，外加汇总节点与模型白名单。

搬运自 `tasks.py`（PT-20261031-02 片3 文件级第二刀），**不改任何行为**。
依赖方向：本模块 → `tasks_runner`（拆完可自动拉起执行）。不 import `tasks_replan`
（重拆要复用的 `ensure_synthesis` 在**本模块**，这样重拆层只朝下看）。
"""
import json
import re
import uuid
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db
import verifier

import tasks_tiers as _tiers
import tasks_runner as _runner

#: 拆解路由：本模块自带 router，`tasks.py` 用 include_router 挂载
router = APIRouter()


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


LEADER_PROMPT = """你是任务编排器。把用户目标拆解为子任务 DAG，只输出 JSON 数组（不要围栏、不要解释）。
每项格式: {"id":"t1","task":"中文指令（自包含，可独立执行）","agent_id":"执行Agent","difficulty":2,"deps":[]}
规则：
- agent_id 只能从这里选：{agents}（选最合适的；不确定用 {default}）
- **要读文件 / 跑命令 / 改代码**的子任务，必须选带工具环的执行 Agent（claude-code、codex、dsh、pi、codebuddy、grok、hermes、omp、cursor）；claude/jcode 是纯文本端点，做不了这类活，选它必编造
- difficulty 取 1/2/3：1 = 单点取证或机械核对；2 = 要读多份材料后归纳；3 = 要取舍/设计/跨模块推理
- model **一般省略**：省略后由 difficulty 决定起始档位（t1 便宜 / t2 中 / t3 强，失败会自动升档）。
  只有你确实要用某个特定模型时才填，且必须从 {models} 里选
- 子任务数 ≤ {maxn}；无依赖的兄弟任务会被并行执行，有依赖写 deps
- 聚合/汇总类任务必须 deps 列出全部上游
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
    if not (1 <= len(items) <= _tiers.MAX_TASKS):
        raise HTTPException(400, f"子任务数须在 1-{_tiers.MAX_TASKS}，得到 {len(items)}")
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


#: 汇总节点：代码自动追加，**不靠 Leader 记得写**。
#: 判例（2026-10-29）：Leader 提示词只写「聚合类任务必须 deps 列出全部上游」，
#: 而没有任何机制保证它真会写一条聚合 —— 结果可能是「三路全 success、run 收敛，
#: 但没有任何结论」。全绿的空转比失败更难发现，所以这条必须是结构约束。
SYNTH_ID = "t_sum"
SYNTH_TASK = ("汇总以下子任务的产出，给出一份统一结论：先逐条列出各处结论与其依据，"
              "再指出彼此冲突或缺口，最后给出可直接交付的答案。不要复述过程。")


def _leaves(items: List[dict]) -> List[str]:
    """叶子 = 不被任何节点依赖的节点（DAG 里的终点）。"""
    depended = {d for n in items for d in n["deps"]}
    return [n["id"] for n in items if n["id"] not in depended]


def ensure_synthesis(items: List[dict], default_agent: str) -> tuple:
    """多叶子 ⇒ 追加一个汇总节点（deps = 全部叶子）。返回 `(items, note)`。

    两种不追加（都返回原因，静默跳过＝又回到"全绿无结论"）：
      · `single_leaf` —— 本身就是一个结论，再加一层只多烧一次 token；
        注意「Leader 自己写了聚合」也落在这里：聚合节点必然是唯一叶子，
        所以**不需要单独一条 already_aggregated 分支**（那会是死代码）。
      · `capacity`    —— 已到 MAX_TASKS 硬上限，上限优先，由调用方决定是否重拆。
    """
    leaves = _leaves(items)
    if len(leaves) <= 1:
        # Leader 自己写的聚合节点同样是"结论出口"，失败最贵、最难发现 ⇒ 一并钉最高档。
        # 判据是结构（deps 覆盖其余全部），不是标题里有没有"汇总"二字。
        pinned = ""
        if len(leaves) == 1:
            for it in items:
                if it["id"] == leaves[0] and set(it["deps"]) >= {x["id"] for x in items} - {it["id"]}:
                    it["pinned_tier"] = "top"
                    pinned = it["id"]
        return items, {"added": False, "reason": "single_leaf", "leaves": leaves,
                       "pinned": pinned}
    if len(items) >= _tiers.MAX_TASKS:
        return items, {"added": False, "reason": "capacity", "leaves": leaves,
                       "hint": "已到 %d 条硬上限，未追加汇总节点" % _tiers.MAX_TASKS}
    sid, i = SYNTH_ID, 2
    while any(x["id"] == sid for x in items):
        sid = "%s%d" % (SYNTH_ID, i)
        i += 1
    # `pinned_tier="top"`：汇总是最不该省的那一步 —— 上游各档都对、汇总写歪，
    # 整条 run 的结论就废了，而这一点下游**看不出来**（router 文献里的
    # "never demote synthesis" 同一理由：失败最贵、最难发现）。
    out = items + [{"id": sid, "task": SYNTH_TASK, "agent_id": default_agent,
                    "deps": list(leaves), "difficulty": 2, "model": None,
                    "pinned_tier": "top"}]
    return out, {"added": True, "id": sid, "deps": leaves}


def _create_run(goal: str, items: List[dict], *, verify_kind: str,
                budget_ms: Optional[int], budget_tokens: Optional[int],
                parent_run_id: Optional[str] = None, origin: str = "api",
                auto_rounds: bool = False, round_no: int = 1,
                run_id: Optional[str] = None) -> str:
    """落一条 run（tasks 行 + task_runs 行）。decompose 与 replan 共用，
    避免两处 INSERT 各自长出一套字段（那是最典型的「两套口径」起点）。
    `run_id` 可外部给：重拆要**先占位再落库**，否则并发两次重拆会生成两条子 run。"""
    run_id = run_id or uuid.uuid4().hex[:10]
    now = _tiers._now()
    for it in items:
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,budget_ms,budget_tokens,difficulty,model_choice,"
            "tier,model_planned,tier_reason,agent_promoted_from,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, it["id"], run_id, goal, it["task"], it["agent_id"],
             json.dumps(it["deps"]), verify_kind, budget_ms, budget_tokens,
             it.get("difficulty"), it.get("model"),
             it.get("tier"), it.get("model_planned"), it.get("tier_reason"),
             it.get("agent_promoted_from") or "", now, now))
    db.execute("INSERT INTO task_runs(run_id,parent_run_id,round,goal,origin,auto_rounds,created_at)"
               " VALUES(?,?,?,?,?,?,?)",
               (run_id, parent_run_id, round_no, goal, origin,
                1 if auto_rounds else 0, now))
    return run_id


def _allowed_models_for(known_agents: set) -> Optional[Dict[str, set]]:
    """能真正吃下 `model` 参数的 agent 的合法模型清单；取不到清单返回 None（= 不校验）。

    只给**模型域就是 CCR 的** agent 建白名单：claude/jcode（OpenAI 兼容端点）与
    `cli_agent.SPECS` 里 `model_path="ccr"` 的 CLI（pi/codex/claude-code/omp/grok/hermes，
    实测 `--model` 直接吃 CCR ID）。其余不建：
      · codebuddy/cursor 的模型域是厂商自有清单，塞 CCR ID 必报 unknown model；
      · dsh 的模型在 profile 里，没有 CLI 开关；
      · 自定义端口 agent 的模型域在它自己那边 ⇒ 拿清单判它会误杀。
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
    try:
        from adapters.cli_agent import SPECS as _CLI
        ccr = {a for a, sp in _CLI.items() if sp.get("model_path") == "ccr"}
    except Exception:  # noqa: BLE001
        ccr = set()
    return {a: ids for a in ({"claude", "jcode"} | ccr) if a in known_agents} or None


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
    if _tiers._ctx.get("agent_ids_fn"):
        known = set(_tiers._ctx["agent_ids_fn"]())
    default = body.default_agent or ("jcode" if "jcode" in known else (sorted(known)[0] if known else "jcode"))
    allowed = _allowed_models_for(known)
    messages = [{"role": "system", "content": (
        LEADER_PROMPT
        .replace("{agents}", ", ".join(sorted(known)) or "jcode")
        .replace("{default}", default)
        .replace("{models}", _agent_model_block(_model_choices(known)))
        .replace("{maxn}", str(_tiers.LEADER_MAXN)))},
        {"role": "user", "content": body.goal}]
    try:
        answer, _ = await llm.chat_tools_loop(messages, [], None, max_rounds=1)
        items = _validate_dag(_extract_json_array(answer), known, default, allowed)
    except HTTPException:
        raise
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"Leader 拆解失败: {e}")
    # 汇总节点在 try 之外补：它抛错是**我们的** bug，不该被报成「Leader 拆解失败」。
    items, synth = ensure_synthesis(items, default)
    items, tier_note = _tiers._plan_tiers(items, known)
    if body.dry_run:
        return {"run_id": None, "goal": body.goal, "tasks": items, "running": False,
                "dry_run": True, "round": 1, "synthesis": synth, "tiers": tier_note,
                "auto_rounds": bool(body.auto_rounds), "max_rounds": _tiers.MAX_ROUNDS,
                "note": "dry_run：只拆解未落库，确认后传 dry_run=false 再调一次"}
    run_id = _create_run(body.goal, items, verify_kind=body.verify_kind,
                         budget_ms=body.budget_ms, budget_tokens=body.budget_tokens,
                         auto_rounds=body.auto_rounds)
    if body.auto_run:
        _runner._start_runner(run_id)
    return {"run_id": run_id, "goal": body.goal, "tasks": items,
            "running": body.auto_run, "round": 1, "synthesis": synth, "tiers": tier_note,
            "auto_rounds": bool(body.auto_rounds), "max_rounds": _tiers.MAX_ROUNDS}
