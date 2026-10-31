"""重拆（第二轮）：闸门、digest、自动钩子。人工与自动**同一套闸门**。

搬运自 `tasks.py`（PT-20261031-02 片3 文件级第二刀），**不改任何行为**。
依赖方向：本模块 → `tasks_dag`（复用拆解校验/落库）+ `tasks_runner`（可自动启动）。
"""
import hashlib
import uuid
from typing import Any, Dict, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

import db
import verifier

import tasks_tiers as _tiers
import tasks_dag as _dag
import tasks_runner as _runner

#: 重拆路由：本模块自带 router，`tasks.py` 用 include_router 挂载
router = APIRouter()


class ReplanIn(BaseModel):
    """手动触发重拆。`max_rounds` 供一次性紧急放行，但**不能超过** MAX_ROUNDS 的硬上限
    —— 上限是「最多自动 2 轮」这条用户裁定的落地，不是建议值。"""
    auto_run: bool = True
    max_rounds: Optional[int] = None


REPLAN_PROMPT = """你是任务编排器的**第二轮**。上一轮 DAG 的执行结果在下面，只输出**需要补做/重做**的子任务
（格式与第一轮相同：JSON 数组，不要围栏、不要解释）。
规则：
- 已经达标的任务**不要重做**；只针对失败、被阻塞、或产物明显不合格的部分
- 若全部达标，输出 `[]`（空数组）——这是唯一的「完工」信号，不要输出解释文字
- agent_id 只能从这里选：{agents}；difficulty 取 1/2/3；model 可选（从 {models} 选）
- 每项 task 必须以「重做：」或「补做：」开头，并写明针对上一轮的哪个 id
- 子任务数 ≤ {maxn}；重拆只有这一轮，别假设还有下一次
"""


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
    if any(s not in _tiers.TERMINAL_STATUSES for s in st.values()):
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
                   (run_id, goal, _tiers._now()))
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
    cap = _tiers.MAX_ROUNDS
    if max_rounds is not None:
        # 只允许**收紧**：调用方传 5 也不能突破用户裁定的 2 轮硬上限。
        try:
            cap = max(1, min(int(max_rounds), _tiers.MAX_ROUNDS))
        except (TypeError, ValueError):
            cap = _tiers.MAX_ROUNDS
    parent_rows = None
    if meta.get("parent_run_id"):
        parent_rows = db.query("SELECT * FROM tasks WHERE run_id=?", (meta["parent_run_id"],))
    gate = replan_gate(meta.get("round") or 1, cap, rows, parent_rows,
                       _tiers.run_budget_exceeded(run_id))
    if not gate["go"]:
        db.execute("UPDATE task_runs SET stop_reason=? WHERE run_id=?", (gate["kind"], run_id))
        return {"ok": True, "run_id": run_id, "gate": gate, "created": None}
    if not llm.configured():
        raise HTTPException(503, "LLM 未配置，无法重拆")
    known = set()
    if _tiers._ctx.get("agent_ids_fn"):
        known = set(_tiers._ctx["agent_ids_fn"]())
    default = ("jcode" if "jcode" in known else (sorted(known)[0] if known else "jcode"))
    allowed = _dag._allowed_models_for(known)
    goal = meta.get("goal") or rows[0].get("goal") or ""
    messages = [{"role": "system", "content": (
        REPLAN_PROMPT
        .replace("{agents}", ", ".join(sorted(known)) or "jcode")
        .replace("{models}", _dag._agent_model_block(_dag._model_choices(known)))
        .replace("{maxn}", str(_tiers.LEADER_MAXN)))},
        {"role": "user", "content":
            "总目标：%s\n\n上一轮（第 %s 轮）的执行结果：\n%s" % (goal, meta.get("round") or 1,
                                                        _replan_digest(rows))}]
    try:
        answer, _ = await llm.chat_tools_loop(messages, [], None, max_rounds=1)
        raw = _dag._extract_json_array(answer)
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
        items = _dag._validate_dag(raw, known, default, allowed)
        items, synth = _dag.ensure_synthesis(items, default)
    except HTTPException as e:
        db.execute("UPDATE task_runs SET stop_reason=? WHERE run_id=?",
                   ("replan_invalid: %s" % str(e.detail)[:150], run_id))
        return {"ok": False, "run_id": run_id, "gate": gate,
                "error": "重拆 DAG 非法: %s" % e.detail, "created": None}
    items, _tier_note = _tiers._plan_tiers(items, known)
    # 占位：只有第一个调用者能把 child_run_id 从 NULL 改掉 ⇒ 并发重拆不会产生两条子 run。
    child_id = uuid.uuid4().hex[:10]
    won = db.execute("UPDATE task_runs SET child_run_id=? WHERE run_id=? AND child_run_id IS NULL",
                     (child_id, run_id))
    if not won:
        return {"ok": True, "run_id": run_id, "gate": dict(gate, kind="already_replanned",
                                                           reason="本轮已重拆过"),
                "created": run_meta(run_id).get("child_run_id")}
    first = rows[0]
    _dag._create_run(goal, items, verify_kind=first.get("verify_kind") or verifier.DEFAULT_KIND,
                budget_ms=first.get("budget_ms"), budget_tokens=first.get("budget_tokens"),
                parent_run_id=run_id, origin=("auto" if auto else "replan"),
                auto_rounds=bool(meta.get("auto_rounds")),
                round_no=int(meta.get("round") or 1) + 1, run_id=child_id)
    if auto:
        _runner._start_runner(child_id)
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
        _runner._start_runner(out["created"])
    return out
