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
    # ── 2026-11-03 用户裁定（协同页四框）────────────────────────────────
    # 模型框：非空 = **钉死整条 run** 用这个模型（`tier="explicit"`）；空 = 沿用
    # difficulty→tiercfg 的自动档。只对模型域是 CCR 的 agent 生效，其余任务跳过并
    # 在返回的 `model_policy.skipped_agents` 里点名（塞 CCR ID 给 codebuddy/cursor
    # 必报 unknown model，静默降级会把「选了没生效」藏起来）。
    model: Optional[str] = Field(default=None, max_length=200)
    # 轮次框：1 = 不自动重拆（默认），2/3/5 = 未达标自动重拆，代码夹到 `ROUNDS_CEILING`。
    # `None` 时回落到老开关 `auto_rounds`（true→2，false→1），保证 MCP 老路径逐字不变。
    rounds: Optional[int] = None


LEADER_PROMPT = """你是任务编排器。把用户目标拆解为子任务 DAG，只输出 JSON 数组（不要围栏、不要解释）。
每项格式: {"id":"t1","task":"中文指令（自包含，可独立执行）","agent_id":"执行Agent","difficulty":2,"deps":[],"files":["src/a.py"]}
规则：
- agent_id 只能从这里选：{agents}（选最合适的；不确定用 {default}）
- **要读文件 / 跑命令 / 改代码**的子任务，必须选带工具环的执行 Agent（claude-code、codex、dsh、pi、codebuddy、grok、hermes、omp、cursor）；claude/jcode 是纯文本端点，做不了这类活，选它必编造
- difficulty 取 1/2/3：1 = 单点取证或机械核对；2 = 要读多份材料后归纳；3 = 要取舍/设计/跨模块推理
- model **一般省略**：省略后由 difficulty 决定起始档位（t1 便宜 / t2 中 / t3 强，失败会自动升档）。
  只有你确实要用某个特定模型时才填，且必须从 {models} 里选
- **`files` 必填**：该子任务会**读或写**的文件路径（相对仓库根，最多 8 个）；纯推理/检索类写 []
- **文件重叠 ⇒ 必须串行**：两个子任务 `files` 有交集时，**不许都是 `deps: []`**——
  要么合并成一个任务，要么给后一个写上前一个的 id。并行改同一文件会互相覆盖，
  实测后果是「同一份代码里并存两套实现，改一处另一处照旧生效」
- 子任务数 ≤ {maxn}；无依赖的兄弟任务会被并行执行，有依赖写 deps
- **互不依赖的子任务必须写 `deps: []`**：拆成单链会让一个任务超时把下游全部阻塞（实测 10 个任务串成链、名册 4 家 agent 只调到 1 家）。能并行的就不要串
- 聚合/汇总类任务必须 deps 列出全部上游
"""

#: 并行度闸门（2026-11-03 混排五缺陷 · 缺陷5）
# 判据：任务数 >= 3 且「无 deps 任务数 / 总数」< 该阈值 ⇒ 拆解可能被串成单链。
# 只告警不拒绝：串行 DAG 在真有依赖时是合法的，闸门的作用是让「退化」可见。
PARALLEL_RATIO_MIN = 0.5
PARALLEL_MIN_TASKS = 3


def parallelism_note(items: Optional[List[dict]]) -> Optional[dict]:
    """拆解后的并行度体检（纯函数，可单测；不读库、不读 env）。

    返回 `None`（合格 / 任务太少不判）或 `{"warning","ratio","free","total"}`。

    为什么值得单列：实证 run `e0f7de9912` 把 10 个子任务排成单链
    （t1→t2→{t3,t4,t6}→…→t9→t10），t2 一次 600s 超时就让剩下 8 个全部 `blocked/
    上游失败`，名册里 4 家 agent 实际只调度到 1 家 —— **混排的「多」在失败时归零**。
    这个退化从返回体里看不出来（每项都合法），所以要显式算一次。
    """
    total = len(items or [])
    if total < PARALLEL_MIN_TASKS:
        return None
    free = sum(1 for it in (items or []) if not (it.get("deps") or []))
    ratio = free / total
    if ratio >= PARALLEL_RATIO_MIN:
        return None
    return {
        "warning": ("并行度偏低：%d/%d 个任务无依赖（%.0f%% < %.0f%%）⇒ 拆解可能被串成单链，"
                    "任一任务超时会把下游全部阻塞（实测会读成「N 个上游失败」）"
                    % (free, total, ratio * 100, PARALLEL_RATIO_MIN * 100)),
        "ratio": round(ratio, 3),
        "free": free,
        "total": total,
    }


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


#: 单任务最多记几个文件（护栏，不是业务上限）。多于此数说明子任务边界本身没划清。
FILE_LIST_MAX = 8

#: 文件重叠闸门（2026-11-03 混排第五缺陷）
# 判据：无依赖的两个子任务 `files` 相交 ⇒ 自动给后者补 dep（串行），并留 note。
FILE_OVERLAP_ENFORCE = True


def _norm_files(v: Any) -> List[str]:
    """把 LLM 给的 files 归一成去重路径列表。非列表/坏项一律丢弃（自评字段不倒灌主流程）。"""
    out: List[str] = []
    if isinstance(v, str):
        v = [v]
    if not isinstance(v, (list, tuple)):
        return out
    for f in v:
        t = str(f or "").strip().replace("\\", "/")
        while t.startswith("./"):
            t = t[2:]
        if not t or len(t) > 200 or t in out:
            continue
        out.append(t)
        if len(out) >= FILE_LIST_MAX:
            break
    return out


def _ancestors(items: List[dict]) -> Dict[str, set]:
    """每个任务的全部祖先集（含传递）。前置：DAG 已无环（`_validate_dag` 已查过）。"""
    by_id = {it["id"]: it for it in items}
    memo: Dict[str, set] = {}

    def walk(tid: str, stack: set) -> set:
        if tid in memo:
            return memo[tid]
        if tid in stack:  # 环：交给上层 Kahn 报错，这里不制造二次异常
            return set()
        acc: set = set()
        for d in (by_id.get(tid, {}).get("deps") or []):
            acc.add(d)
            acc |= walk(d, stack | {tid})
        memo[tid] = acc
        return acc

    for it in items:
        walk(it["id"], set())
    return memo


def file_driven_deps(items: List[dict]) -> list:
    """找出「因为有文件交集才该有的依赖」：`(task, dep, 交集文件)`。

    用途：区分**刻意串行**与**退化单链**。Leader 自己就按 `files` 写了 deps 时
    （实测一次 dry_run：t2 依赖 t1、t3 依赖 t2，三者同为 `templates/index.html`），
    `serialize_file_overlaps` 无事可做 ⇒ note 为 None，于是并行度告警会把它读成
    「拆解退化、可能串成单链」。**同一份 DAG，一次是对的，一次被报警** ——
    判据必须落在「这条依赖为什么存在」，而不是「有几个任务无依赖」。
    """
    by_id = {it["id"]: it for it in items}
    out = []
    for it in items:
        cur = set(it.get("files") or [])
        if not cur:
            continue
        for d in it.get("deps") or []:
            dep = by_id.get(d) or {}
            hit = cur & set(dep.get("files") or [])
            if hit:
                out.append((it["id"], d, sorted(hit)[:3]))
    return out


def serialize_file_overlaps(items: List[dict]) -> tuple:
    """同文件集的并行子任务 ⇒ **强制串行**。返回 `(items, note|None)`。

    为什么这不是"锦上添花"：2026-11-03 实测同一条 UI 需求被拆成 4 个子任务并行派给
    4 家 agent，全都落在 `templates/index.html` 与 `static/hub/04-terminal-ws.js` 上，
    后写覆盖先写 ⇒ 落盘的是「两套并存的实现」，改一处另一处照旧生效。拆解层当时
    没有任何字段能表达「这两个任务会碰同一份文件」，所以**连告警都给不出来**。

    规则只有一条，且只在无环方向加边（后者依赖前者）：
      按声明顺序扫描，若 B 的 files 与某个更早的 A 相交，且 A 不在 B 的祖先集里，
      就给 B 补 `deps += [A]`。因为边只从后指向前，加边不可能造出环。
    """
    if not FILE_OVERLAP_ENFORCE:
        return items, None
    anc = _ancestors(items)
    added: List[dict] = []
    for i, cur in enumerate(items):
        cur_files = set(cur.get("files") or [])
        if not cur_files:
            continue
        # 先做**传递归约**再连边：若 prev 本身是另一个 prev 的祖先，只等后者即可。
        # 少了这一步，3 个任务碰同一文件会连出 t2←{t1}、t3←{t1,t2} 三条边 ——
        # 图是对的，但「等 t1 又等 t2（t2 本来就在等 t1）」是白等，且 note 计数虚高。
        cands = [p for p in items[:i] if cur_files & set(p.get("files") or [])]
        cands = [p for p in cands
                 if not any(p["id"] in anc.get(q["id"], set()) for q in cands if q is not p)]
        for prev in cands:
            if prev["id"] in anc.get(cur["id"], set()):
                continue
            cur.setdefault("deps", [])
            cur["deps"] = list(cur["deps"]) + [prev["id"]]
            anc[cur["id"]] = anc.get(cur["id"], set()) | {prev["id"]} | anc.get(prev["id"], set())
            added.append({"task": cur["id"], "after": prev["id"],
                          "files": sorted(cur_files & set(prev.get("files") or []))[:4]})
    if not added:
        return items, None
    return items, {
        "kind": "file_overlap_serialized",
        "count": len(added),
        "pairs": added[:6],
        "warning": ("%d 个子任务的文件集相交，已自动改为串行（后写覆盖先写会让产物互相抹掉）：%s"
                    % (len(added), "；".join("%s 等 %s" % (a["task"], a["after"]) for a in added[:4]))),
    }


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
                           "model": model, "files": _norm_files(it.get("files"))})
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
                run_id: Optional[str] = None,
                max_rounds: Optional[int] = None) -> str:
    """落一条 run（tasks 行 + task_runs 行）。decompose 与 replan 共用，
    避免两处 INSERT 各自长出一套字段（那是最典型的「两套口径」起点）。
    `run_id` 可外部给：重拆要**先占位再落库**，否则并发两次重拆会生成两条子 run。"""
    run_id = run_id or uuid.uuid4().hex[:10]
    now = _tiers._now()
    for it in items:
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,budget_ms,budget_tokens,difficulty,model_choice,"
            "tier,model_planned,tier_reason,agent_promoted_from,created_at,updated_at,files)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?,?,?,?,?,?)",
            (run_id, it["id"], run_id, goal, it["task"], it["agent_id"],
             json.dumps(it["deps"]), verify_kind, budget_ms, budget_tokens,
             it.get("difficulty"), it.get("model"),
             it.get("tier"), it.get("model_planned"), it.get("tier_reason"),
             it.get("agent_promoted_from") or "", now, now,
             json.dumps(it.get("files") or [])))
    # `max_rounds`：界面轮次框的**落库值**（2026-11-03）。存 NULL = 存量/老调用方，
    # 重拆层回落到 `MAX_ROUNDS`，行为与改版前逐字一致（不因新列出现就多跑几轮）。
    db.execute("INSERT INTO task_runs(run_id,parent_run_id,round,goal,origin,auto_rounds,"
               "max_rounds,created_at) VALUES(?,?,?,?,?,?,?,?)",
               (run_id, parent_run_id, round_no, goal, origin,
                1 if auto_rounds else 0,
                (_tiers.clamp_rounds(max_rounds) if max_rounds is not None else None), now))
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


def _apply_pinned_model(items: List[dict], model: Optional[str],
                        allowed_models: Optional[Dict[str, set]]) -> tuple:
    """界面「模型」框 → 整条 run 钉死一个模型。返回 `(items, note)`。

    为什么在 `_plan_tiers` **之后** 调：档位层会先按难度提升执行者（`promote_agent`），
    钉模型必须以**最终执行者**判模型域；反过来先钉再抬，抬到 codebuddy 上就是必失败。

    四条边界：
      · 空值 ⇒ 不动（`mode=auto`，按难度→档位）；
      · 清单取得了但 ID 不在册 ⇒ **400**（界面只列在册 ID，到这里说明调用方写错了；
        静默降级会让用户把「没生效」当成模型太弱）；
      · 清单取不到（`checked=False`）⇒ 判不了就不拦，只给 CCR 域 agent 钉；
      · 模型域不是 CCR 的 agent ⇒ 跳过并点名（与 `_allowed_models_for` 同一口径）。
    """
    m = (model or "").strip()
    if not m:
        return items, {"mode": "auto",
                       "note": "未钉死模型：按难度→档位（t1/t2/t3），失败自动升档"}
    checked = allowed_models is not None
    if checked:
        allowed_ids = set().union(*allowed_models.values()) if allowed_models else set()
        if m not in allowed_ids:
            raise HTTPException(400, "模型 %s 不在网关清单里（界面只列在册 ID）；"
                                     "清单取不到时不会走到这条判据" % m)
        target = set(allowed_models.keys())
    else:
        target = _tiers._ccr_model_agents()
    applied, skipped = [], []
    for it in items:
        if it.get("agent_id") in target:
            it["model"] = m
            it["tier"] = "explicit"
            it["model_planned"] = m
            it["tier_reason"] = "pinned:界面钉死模型"
            applied.append(it["id"])
        else:
            skipped.append(it.get("agent_id") or "-")
    note = {"mode": "pinned", "model": m, "applied": applied,
            "skipped_agents": sorted(set(skipped)), "catalog_checked": checked}
    note["note"] = ("已钉死 %s（%d 个任务）" % (m, len(applied)) if applied else
                    "该模型不适用于本条 run 的 agent（%s），已跳过"
                    % ",".join(sorted(set(skipped))))
    return items, note


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
    # 文件重叠闸门必须在 ensure_synthesis **之前**、档位层**之前**：
    #   · 之前（相对汇总节点）：串行化会改 deps，汇总节点的 deps 要按**串行化后的叶子**算，
    #     否则会 dep 到一个已经不是叶子的节点（冗余等待，且掩盖真正缺口）；
    #   · 之前（相对档位层）：叶子才钉最高档，档位层必须看到最终形态。
    items, file_note = serialize_file_overlaps(items)
    # 汇总节点在 try 之外补：它抛错是**我们的** bug，不该被报成「Leader 拆解失败」。
    items, synth = ensure_synthesis(items, default)
    items, tier_note = _tiers._plan_tiers(items, known)
    # 钉死模型必须在档位层**之后**（见 `_apply_pinned_model` 文档串）：档位层会提升执行者，
    # 先钉再抬可能把 CCR 模型 ID 抬到一个不吃它的 agent 上。
    items, pin_note = _apply_pinned_model(items, body.model, allowed)
    # 并行度体检：只告警不拒绝（串行 DAG 在真有依赖时合法，这里让「退化」可见）。
    par_note = parallelism_note(items)
    if par_note and not file_note:
        # Leader 自己按 files 串的：不是退化。全部非空依赖都有文件交集 ⇒ 直接不报警；
        # 只有部分有交集才补一句说明（剩余部分仍可能是真退化，不能一并静音）。
        driven = file_driven_deps(items)
        total_deps = sum(len(it.get("deps") or []) for it in items)
        if driven and total_deps and len(driven) >= total_deps:
            par_note = None
        elif driven:
            par_note = dict(par_note, reason="partly_file_overlap",
                            file_driven_deps=len(driven),
                            warning=par_note["warning"] + (
                                "（其中 %d 条依赖由同文件声明决定，属刻意串行，不算退化）"
                                % len(driven)))
    if par_note and file_note:
        # 刻意串行不是退化：不加这句，「为防同文件覆盖而串行」会被读成「Leader 又拆成单链」。
        par_note = dict(par_note, reason="file_overlap",
                        file_serialization=file_note["count"],
                        warning=par_note["warning"] + (
                            "（其中 %d 处是为避免同文件覆盖而**刻意**串行，见 file_serialization）"
                            % file_note["count"]))
    # 轮次：显式 `rounds` 优先，其次老开关 `auto_rounds`（true→2/false→1），最后默认 1。
    rounds = _tiers.clamp_rounds(
        body.rounds if body.rounds is not None else (2 if body.auto_rounds else 1))
    auto_rounds = rounds >= 2
    if body.dry_run:
        return {"run_id": None, "goal": body.goal, "tasks": items, "running": False,
                "dry_run": True, "round": 1, "synthesis": synth, "tiers": tier_note,
                "model_policy": pin_note,
                "parallelism_warning": (par_note or {}).get("warning"),
                "file_serialization": file_note,
                "auto_rounds": auto_rounds, "rounds": rounds, "max_rounds": rounds,
                "round_choices": list(_tiers.ROUND_CHOICES),
                "note": "dry_run：只拆解未落库，确认后传 dry_run=false 再调一次"}
    run_id = _create_run(body.goal, items, verify_kind=body.verify_kind,
                         budget_ms=body.budget_ms, budget_tokens=body.budget_tokens,
                         auto_rounds=auto_rounds, max_rounds=rounds)
    if body.auto_run:
        _runner._start_runner(run_id)
    if file_note:
        try:
            db.log_profile_event("task_plan", body.default_agent or "leader", "file_overlap",
                                 0, trace_id=run_id, detail=dict(file_note, run=run_id))
        except Exception:  # noqa: BLE001 —— 留痕失败不得让拆解整体失败
            pass
    if par_note:
        # 只有真落库的 run 才留痕（dry_run 不写库是既有约定，见 DecomposeIn.dry_run 注释）
        try:
            db.log_profile_event("task_plan", body.default_agent or "leader", "low_parallelism",
                                 0, trace_id=run_id, detail=dict(par_note, run=run_id))
        except Exception:  # noqa: BLE001 —— 留痕失败不得让拆解整体失败
            pass
    return {"run_id": run_id, "goal": body.goal, "tasks": items,
            "running": body.auto_run, "round": 1, "synthesis": synth, "tiers": tier_note,
            "model_policy": pin_note,
            "parallelism_warning": (par_note or {}).get("warning"),
            "file_serialization": file_note,
            "auto_rounds": auto_rounds, "rounds": rounds, "max_rounds": rounds,
            "round_choices": list(_tiers.ROUND_CHOICES)}
