"""任务执行器：调度循环、单任务执行、验证与结算、僵死兜底。

搬运自 `tasks.py`（PT-20261031-02 片3 文件级第二刀），**不改任何行为**。
本层是整个任务链的**最底层**（只依赖 `tasks_tiers`）：`_start_runner` 是所有
「拉起一条 run」的唯一入口，dag/replan/tasks 都指向这里，所以它不能反过来 import 它们。
唯一的例外是 `_execute_run` 收敛后要接下一轮（`_maybe_replan` 在 `tasks_replan`）——
那里用**函数内延迟导入**，否则 import 期就成环。
"""
import asyncio
import inspect
import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import chain_judge
import db
import verifier

import tasks_tiers as _tiers


_runners: Dict[str, asyncio.Task] = {}


def resume_expired_waits(now: Optional[str] = None) -> int:
    """§7-② **自愈**：把过期的等待态推到下一步。返回被处理的行数。

    · 未到唤醒上限 ⇒ 重新排队（pending）+ 拉起 runner（自愈，不靠人记得调 `/resume`）；
    · 到上限 ⇒ `failed`（`等待超时（已唤醒 N 次仍无输入）`）—— 「等不来」如实失败，
      而不是无限期地当一个看似正常的中间态。
    """
    now = now or _tiers._now()
    ph = ",".join("?" * len(_tiers.PARKED_STATUSES))
    rows = db.query("SELECT run_id, task_id, wake_count FROM tasks"
                    " WHERE status IN (%s) AND wait_until IS NOT NULL AND wait_until <= ?" % ph,
                    (*_tiers.PARKED_STATUSES, now))
    if not rows:
        return 0
    resumed_runs = set()
    for r in rows:
        if int(r["wake_count"] or 0) >= _tiers.MAX_WAKES:
            db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       ("等待超时（已唤醒 %d 次仍无输入）" % _tiers.MAX_WAKES, now, now,
                        r["run_id"], r["task_id"]))
        else:
            db.execute("UPDATE tasks SET status='pending',error=NULL,wait_until=NULL,"
                       "wake_count=COALESCE(wake_count,0)+1,updated_at=?"
                       " WHERE run_id=? AND task_id=?", (now, r["run_id"], r["task_id"]))
            resumed_runs.add(r["run_id"])
    for rid in resumed_runs:
        _start_runner(rid)
    return len(rows)


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


def _accepts_kwarg(fn, name: str) -> bool:
    """callable 收不收这个关键字参数（显式声明或 `**kwargs` 都算）。

    与 `_chat_arity` 同一套理由：`set_context` 的接线方不止一处，有的只写
    3 参 lambda。多传一个它不认识的 kwarg 会 TypeError，而那时钱已经花掉了。
    """
    try:
        params = inspect.signature(fn).parameters
    except (TypeError, ValueError):
        return False
    return name in params or any(p.kind == p.VAR_KEYWORD for p in params.values())


async def _call_chat(chat_fn, agent_id: str, prompt: str, tag: str,
                     model: Optional[str]):
    """按 chat_fn 的实际签名决定要不要把选型模型传下去。

    传不下去时**不报错**（拆解意图不等于执行契约），但这条事实是可查的：
    `model_choice` 有值而 `model`（实发回包）是另一个值，就是「选了没生效」的证据。

    超时（2026-10-29）：adapter 一侧必须**晚于**外层 `asyncio.wait_for` 超时，
    否则 TASK_TIMEOUT_S 这道闸永不触发 —— 症状是所有长任务都被记成「执行者失败」，
    归因错一层（真实原因是客户端 120s 先断）。
    """
    kw: Dict[str, Any] = {}
    if _accepts_kwarg(chat_fn, "timeout_s"):
        kw["timeout_s"] = _tiers.TASK_TIMEOUT_S + 30
    if model and _chat_arity(chat_fn) >= 4:
        return await chat_fn(agent_id, prompt, tag, model, **kw)
    return await chat_fn(agent_id, prompt, tag, **kw)


async def _prepare_prompt(run_id: str, row: dict) -> str:
    """拼装执行 prompt：上游产物注入 + 上一次验证失败的提示。

    2026-10-31 从 `_run_task`（277 行）拆出，纯拼装、不写库，便于单测。
    """
    ups = db.query("SELECT task_id,output FROM tasks WHERE run_id=? AND status='success'",
                   (run_id,))
    upstream = {u["task_id"]: u["output"] or "" for u in ups}
    prompt = row["prompt"]
    dep_outputs = []
    for d in json.loads(row["deps"] or "[]"):
        if d in upstream:
            dep_outputs.append(_tiers.format_upstream(d, upstream[d]))
    if dep_outputs:
        prompt = f"总目标：{row['goal']}\n\n你的子任务：{prompt}\n\n" + "\n\n".join(dep_outputs)

    # ── B3：上一次验证失败的提示（自动重试时带上，否则重跑就是原样再赌一次）──
    attempts_done = int(row.get("attempts") or 0)
    if attempts_done:
        prompt = ("—— 上一次验证未通过（自动重试 %d/%d）：%s ——\n\n"
                  % (attempts_done, _tiers.MAX_AUTO_RETRY,
                     str(row.get("verify_reason") or "")[:300])) + prompt
    return prompt


def _resolve_exec_target(run_id: str, row: dict, attempts_done: int) -> Tuple[str, Optional[str]]:
    """升档决议：第 N 次重试换一档（模型优先、其次 agent），换不动就写明原因。

    返回 `(agent_id, model)`。副作用：升档成功时改写 tasks.agent_id 并记 escalate 事件
    （`agent_id` 必须同步改写，否则展示面与实际执行者不一致 —— v0.13.177 实弹）。
    """
    exec_agent = row["agent_id"]
    # 三列三口径：model_planned（按档位决议）> model_choice（Leader 显式）>
    # None（用该 agent 自己的默认）。执行前一律以"决议值"为准。
    exec_model = row.get("model_planned") or row.get("model_choice")
    cat, cat_ok = _tiers._catalog_snapshot()
    esc = _tiers.escalate_target(exec_agent, exec_model, attempts_done, _tiers._known_agent_ids(),
                          tier=row.get("tier"), catalog=cat, catalog_checked=cat_ok)
    if esc["changed"]:
        exec_agent, exec_model = esc["agent_id"], esc["model"]
        db.execute("UPDATE tasks SET agent_id=?,escalated_from_agent=?,escalated_from_model=?,"
                   "updated_at=? WHERE run_id=? AND task_id=?",
                   (exec_agent, esc["from_agent"], esc["from_model"], _tiers._now(),
                    run_id, row["task_id"]))
        db.log_profile_event("task_exec", exec_agent, "escalate", 0,
                             trace_id=row["trace_id"],
                             detail={"task": row["task_id"], "reason": esc["reason"],
                                     "from_agent": esc["from_agent"],
                                     "from_model": esc["from_model"],
                                     "tier": row.get("tier"),
                                     "attempt": attempts_done})
    return exec_agent, exec_model


async def _attach_retrieval(run_id: str, row: dict, prompt: str, exec_agent: str) -> str:
    """B1 接单前置检索 + 技能说明书注入，把线索拼进 prompt 并落 retrieval_json。

    失败/超时一律降级（不阻塞），但**必须留下痕迹**（retrieval_json 里的 error）。
    2026-10-31 从 `_run_task` 拆出：这段是「取增强」，与执行/结算无关。
    """
    rmeta: Dict[str, Any] = {}
    if _tiers.TASK_RETRIEVAL:
        block, rmeta = await _tiers._retrieval_for(row.get("goal") or "", prompt,
                                            row.get("agent_id") or "")
        if block:
            prompt = block + "\n\n" + prompt
    if _tiers.TASK_SKILL_INJECT:
        sblock, smeta = await _tiers._skill_inject_for(row.get("goal") or "", row["prompt"] or "",
                                                exec_agent)
        if sblock:
            prompt = sblock + "\n\n" + prompt
        rmeta = dict(rmeta or {})
        rmeta["skill_inject"] = smeta
    if rmeta:
        try:
            db.execute("UPDATE tasks SET retrieval_json=?,updated_at=? "
                       "WHERE run_id=? AND task_id=?",
                       (json.dumps(rmeta, ensure_ascii=False)[:6000], _tiers._now(),
                        run_id, row["task_id"]))
        except Exception:  # noqa: BLE001 — 落库失败不得带倒子任务（检索是增强）
            pass
    return prompt


async def _settle_self_failure(run_id: str, row: dict, result: dict, attempts_done: int,
                             exec_agent: str, exec_model, dur: int, spent_tokens: int,
                             used_model) -> bool:
    """执行者自称失败时的结算：过一遍独立复核并留痕；能升档就排重试。

    返回 True 表示已处理（调用方直接 return），False 表示执行者其实报了 success
    （调用方继续走④的验证分支）。2026-10-31 从 `_run_task` 拆出。
    """
    # ③ 自称失败：**失败方向仍保守**（不因产物非空就升级），但必须过一遍独立复核并留痕。
    #    §7-④：执行者不得自证 success（④），也**不得自证 fail**（本支）——旧实现直接 failed、
    #    不看产物、不留结论 ⇒「其实产物合格」被静默掩盖、事后不可复核。
    if not result.get("success"):
        err = (result.get("error") or "unknown")[:500]
        kind = row.get("verify_kind") or verifier.DEFAULT_KIND
        # §7-①：复核走**线程**（`to_thread`）而非就地同步调用 —— 否则协程从不 yield，
        # 置下的 `verifying` 在 DB 上没人读得到。
        db.execute("UPDATE tasks SET status='verifying',verify_started_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?", (_tiers._now(), _tiers._now(), run_id, row["task_id"]))
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
        # ── 执行者自身失败也走升档重试（v0.13.177）──
        # 2026-10-29 实弹：dsh 连吃两次 `RATE_LIMIT 429` → 这条分支**直接写 failed**，
        # 绕过了下面 verifier 失败分支的 `should_retry` ⇒ 整条 run 的 t2/t3 一次都不重试，
        # t4 因为上游 failed 变 blocked，汇总永远跑不到。而 429 恰恰是「换一家就活」
        # 的典型场景 —— 不重试等于把可恢复故障记成终态。
        # 判据用 `escalate_target` 的 `changed`：**没有升档路径就不重试**（同 agent 同模型
        # 原地再赌一次是纯浪费，那正是 v0.13.174 之前的错）。
        if _tiers._schedule_exec_retry(run_id, row, err, reason, attempts_done,
                                exec_agent, exec_model, dur=dur,
                                spent_tokens=spent_tokens, used_model=used_model,
                                kind=kind):
            return True
        # ⚠ 口径冲突处理（2026-10-16）：P0-b 的契约是「**没过 verifier（含自述失败）的路径
        #   `verdict` 必须 NULL**」，而 P0-b 闸门 `test_verdict_null_on_self_reported_failure`
        #   钉着这一条。所以本条修复**不写 `verdict`**（保持 NULL），复核结论落 `verify_reason`
        #   + `mismatch` 事件 —— 目的（**不再静默**）达到了，且不破别人的契约。
        db.execute("UPDATE tasks SET status='failed',error=?,verify_reason=?,"
                   "verify_ms=?,"
                   "duration_ms=?,spent_ms=?,spent_tokens=?,model=?,finished_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   (err, reason, verify_ms, dur, dur, spent_tokens, used_model,
                    _tiers._now(), _tiers._now(), run_id, row["task_id"]))
        db.log_profile_event("task_exec", row["agent_id"], event, dur,
                             trace_id=row["trace_id"],
                             detail={"task": row["task_id"], "verify": kind,
                                     "verify_status": status_v, "claimed_failure": True})
        return True
    return False


async def _verify_and_settle(run_id: str, row: dict, result: dict, attempts_done: int,
                            exec_agent: str, exec_model, dur: int, spent_tokens: int,
                            used_model) -> None:
    """④ 执行者不自证：置 verifying → verifier 独立判产物 → 按结论结算。

    三条出口：过 ⇒ 主链判定后 success/submitted；可修复 ⇒ 排自动重试（pending）；
    不可修复 ⇒ failed。2026-10-31 从 `_run_task` 拆出，仅搬代码不改判据。
    """
    # ④ **执行者不自证**：先 verifying，再由 verifier 独立判产物（P0-3 主判据）。
    kind = row.get("verify_kind") or verifier.DEFAULT_KIND
    db.execute("UPDATE tasks SET status='verifying',spent_ms=?,spent_tokens=?,updated_at=?,"
               "verify_started_at=? WHERE run_id=? AND task_id=?",
               (dur, spent_tokens, _tiers._now(), _tiers._now(), run_id, row["task_id"]))
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
        decision = chain_judge.accept_decision(_tiers.AUTO_ACCEPT, jv)
        need_review = decision["review"]
        if decision["accept"]:
            db.execute("UPDATE tasks SET status='success',output=?,duration_ms=?,spent_ms=?,"
                       "spent_tokens=?,model=?,verdict=?,verify_reason=?,verify_ms=?,"
                       "judge_json=?,submitted_at=?,accepted_at=?,accepted_by=?,"
                       "finished_at=?,updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       (out, dur, dur, spent_tokens, used_model, verdict["status"], None,
                        verify_ms, judge_json, _tiers._now(), _tiers._now(), "auto", _tiers._now(), _tiers._now(),
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
                        verify_ms, judge_json, _tiers._now(), _tiers._now(), run_id, row["task_id"]))
        db.log_profile_event("task_exec", row["agent_id"],
                             "success" if (_tiers.AUTO_ACCEPT and not need_review) else "submitted",
                             dur,
                             trace_id=row["trace_id"],
                             detail={"task": row["task_id"], "verify": kind,
                                     "model": used_model,
                                     "auto_accept": _tiers.AUTO_ACCEPT and not need_review,
                                     "judge": jv.get("label") if jv.get("ok") else "unjudged",
                                     "judge_ok": bool(jv.get("ok")),
                                     "judge_ms": jv.get("ms")})
    else:
        gaps = "; ".join("%s: %s" % (g["id"], g["detail"]) for g in verdict["gaps"])
        err = ("verifier %s (%s): %s" % (verdict["status"], kind, gaps))[:500]
        # ── B3：可修复的验证失败，自动重试 MAX_AUTO_RETRY 次（默认 1）──
        # 只对 `repairable` 重试：`insufficient`（未知 kind / 无产物）重跑也不会变好。
        # 次数写进 `attempts` 列并在重跑时把上次的 verify_reason 接进 prompt（否则就是原样再赌）。
        if _tiers.should_retry(verdict["status"], verdict.get("repairable"), attempts_done):
            db.execute("UPDATE tasks SET status='pending',attempts=?,error=?,verify_reason=?,"
                       "verdict=?,verify_ms=?,spent_ms=?,spent_tokens=?,model=?,"
                       "finished_at=NULL,updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       (attempts_done + 1,
                        ("%s（自动重试 %d/%d，升档 %s）"
                         % (err, attempts_done + 1, _tiers.MAX_AUTO_RETRY,
                            _tiers._next_escalation_note(exec_agent, exec_model,
                                                  attempts_done + 1,
                                                  row.get("tier"))))[:500],
                        gaps[:500], verdict["status"], verify_ms, dur, spent_tokens,
                        used_model, _tiers._now(), run_id, row["task_id"]))
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
                    _tiers._now(), _tiers._now(), run_id, row["task_id"]))
        db.log_profile_event("task_exec", row["agent_id"], "fail", dur,
                             trace_id=row["trace_id"],
                             detail={"task": row["task_id"], "verify": kind,
                                     "verify_status": verdict["status"],
                                     "model": used_model})
async def _run_task(run_id: str, row: dict):
    chat_fn = _tiers._ctx.get("chat_fn")
    if not chat_fn:
        db.execute("UPDATE tasks SET status='failed',error='chat 引擎未就绪',updated_at=? "
                   "WHERE run_id=? AND task_id=?", (_tiers._now(), run_id, row["task_id"]))
        return
    prompt = await _prepare_prompt(run_id, row)
    attempts_done = int(row.get("attempts") or 0)
    exec_agent, exec_model = _resolve_exec_target(run_id, row, attempts_done)
    prompt = await _attach_retrieval(run_id, row, prompt, exec_agent)
    t0 = asyncio.get_event_loop().time()
    try:
        result = await asyncio.wait_for(
            _call_chat(chat_fn, exec_agent, prompt,
                       f"task-{row['trace_id']}-{row['task_id']}",
                       exec_model),
            timeout=_tiers.TASK_TIMEOUT_S)
        dur = int((asyncio.get_event_loop().time() - t0) * 1000)
        spent_tokens = int(((result.get("usage") or {}).get("total_tokens")) or 0)
        # P0-a：实发模型。取 **adapter 回包** 的 model 而非请求值 —— 上游可能改写，
        # 记请求值会把「我们以为用了谁」当成「实际用了谁」，正好毁掉归因的意义。
        # 回包缺 model 时留 NULL，不拿请求值兜底（那是猜测，不是观测）。
        used_model = result.get("model") or None
        # ① 等待态：执行者说「卡在等人/等批」⇒ 让出执行资源，不占槽、不算终态。
        waiting = result.get("waiting")
        if waiting in _tiers.PARKED_STATUSES:
            # §7-②：等待**必须有期限**（旧实现不写期限 ⇒ 无人叫醒就永久悬空）
            deadline = (datetime.now(timezone.utc)
                        + timedelta(seconds=_tiers.WAIT_RESUME_S)).isoformat()
            db.execute("UPDATE tasks SET status=?,spent_ms=?,spent_tokens=?,model=?,wait_until=?,"
                       "wake_count=COALESCE(wake_count,0),updated_at=?"
                       " WHERE run_id=? AND task_id=?",
                       (waiting, dur, spent_tokens, used_model, deadline, _tiers._now(),
                        run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"], "parked", dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "waiting": waiting})
            return
        # ② 预算：超限一律 failed（不静默放过）。
        over = _tiers.budget_exceeded(row, dur, spent_tokens)
        if over:
            db.execute("UPDATE tasks SET status='failed',error=?,spent_ms=?,spent_tokens=?,"
                       "model=?,finished_at=?,updated_at=? WHERE run_id=? AND task_id=?",
                       ("预算耗尽: " + over, dur, spent_tokens, used_model,
                        _tiers._now(), _tiers._now(), run_id, row["task_id"]))
            db.log_profile_event("task_exec", row["agent_id"], "fail", dur,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "budget": over})
            return
        if await _settle_self_failure(run_id, row, result, attempts_done, exec_agent,
                                      exec_model, dur, spent_tokens, used_model):
            return
        await _verify_and_settle(run_id, row, result, attempts_done, exec_agent,
                                 exec_model, dur, spent_tokens, used_model)
    except asyncio.TimeoutError:
        # 2026-10-15 P0-a 取证时发现既存缺陷（非本次引入）：原句把 `'超时熔断 %ds'`
        # 写成字面量却按 5 个参数传（SQL 只有 4 个占位符）⇒ sqlite3.ProgrammingError
        # 在**超时这个异常处理器内部**抛出，任务永远停在 running，且异常冒泡进
        # `_execute_run`。改成正常绑定，顺带把 model 一起记上。
        _err = "超时熔断 %ds" % _tiers.TASK_TIMEOUT_S
        if _tiers._schedule_exec_retry(run_id, row, _err, _err, int(row.get("attempts") or 0),
                                row.get("agent_id") or "",
                                row.get("model_planned") or row.get("model_choice"),
                                dur=_tiers.TASK_TIMEOUT_S * 1000):
            return
        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   (_err, _tiers._now(), _tiers._now(), run_id, row["task_id"]))
        db.log_profile_event("task_exec", row["agent_id"], "fail", _tiers.TASK_TIMEOUT_S * 1000,
                             trace_id=row["trace_id"], detail={"task": row["task_id"], "timeout": True})
    except Exception as e:  # noqa: BLE001
        _err = repr(e)[:500]
        if _tiers._schedule_exec_retry(run_id, row, _err, _err, int(row.get("attempts") or 0),
                                row.get("agent_id") or "",
                                row.get("model_planned") or row.get("model_choice")):
            return
        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   (_err, _tiers._now(), _tiers._now(), run_id, row["task_id"]))


async def _execute_run(run_id: str):
    sem = asyncio.Semaphore(_tiers.MAX_CONCURRENCY)
    live: Dict[str, asyncio.Task] = {}
    try:
        while True:
            rows = db.query("SELECT * FROM tasks WHERE run_id=? ORDER BY created_at", (run_id,))
            status = {r["task_id"]: r["status"] for r in rows}
            finished = all(s in _tiers.TERMINAL_STATUSES for s in status.values())
            if finished:
                # 2026-10-27 全链路编排：收敛后若本轮声明了 `auto_rounds`，在这里接下一轮。
                # 放在 return 前而不是收尾处，是因为「收敛」只在这一处判定 —— 另起一个
                # 监听会变成第二份「什么叫跑完」的定义。
                import tasks_replan as _replan  # 延迟导入：runner 在依赖链底端，重拆层在它上面
                await _replan._maybe_replan(run_id)
                return
            for r in rows:
                if r["task_id"] in live or r["status"] != "pending":
                    continue
                deps = json.loads(r["deps"] or "[]")
                if any(status.get(d) in ("failed", "blocked", "cancelled") for d in deps):
                    db.execute("UPDATE tasks SET status='blocked',error='上游失败',finished_at=?,"
                               "updated_at=? WHERE run_id=? AND task_id=?",
                               (_tiers._now(), _tiers._now(), run_id, r["task_id"]))
                    continue
                if all(status.get(d) == "success" for d in deps):
                    # §7-③ run 级预算：在**派发前**查累计消耗（事后算 = 已经花完了）。
                    # 放内层（逐 pending）而非整轮 continue：不为预算引入新的循环出口，
                    # 避免与 parked/submitted 的挂起逻辑打架。
                    r_over = _tiers.run_budget_exceeded(run_id)
                    if r_over:
                        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,"
                                   "updated_at=? WHERE run_id=? AND task_id=?",
                                   ("run 预算耗尽: " + r_over, _tiers._now(), _tiers._now(),
                                    run_id, r["task_id"]))
                        db.log_profile_event("task_exec", r.get("agent_id") or "-", "fail", None,
                                             trace_id=r.get("trace_id"),
                                             detail={"task": r["task_id"],
                                                     "run_budget": r_over})
                        continue
                    db.execute("UPDATE tasks SET status='running',started_at=?,updated_at=?"
                               " WHERE run_id=? AND task_id=?",
                               (_tiers._now(), _tiers._now(), run_id, r["task_id"]))

                    async def guarded(row=r):
                        async with sem:
                            await _run_task(run_id, row)
                    live[r["task_id"]] = asyncio.create_task(guarded())
            if not live:
                parked = [t for t, s in status.items() if s in _tiers.PARKED_STATUSES]
                awaiting = [t for t, s in status.items() if s in _tiers.SUBMITTED_STATUSES]
                pend = [t for t, s in status.items() if s == "pending"]
                if parked or awaiting:
                    # 整条 run 挂起：等外部输入/批准/恢复（parked），或等 `/accept`（submitted）
                    # ⇒ 释放执行资源，不空转、也不算死锁。
                    return
                if pend:
                    db.execute("UPDATE tasks SET status='failed',error='无法调度(疑似悬挂)',"
                               "updated_at=? WHERE run_id=? AND status='pending'", (_tiers._now(), run_id))
                    return
                return
            done = [tid for tid, t in live.items() if t.done()]
            for tid in done:
                live.pop(tid)
            await asyncio.sleep(_tiers.LOOP_SLEEP_S)
    except asyncio.CancelledError:
        for t in live.values():
            t.cancel()
        raise


async def sweep_stale_tasks():
    """服务启动恢复 + 周期兜底：running 超时→failed（上游 sweeper 语义）"""
    db.execute("UPDATE tasks SET status='failed',error='服务重启恢复:标记中断',finished_at=?,"
               "updated_at=? WHERE status='running'", (_tiers._now(), _tiers._now()))
    while True:
        await asyncio.sleep(30)
        try:
            cutoff = datetime.now(timezone.utc).timestamp() - (_tiers.TASK_TIMEOUT_S + 120)
            stale = db.query("SELECT run_id,task_id,started_at FROM tasks WHERE status='running'")
            for r in stale:
                try:
                    started = datetime.fromisoformat(r["started_at"]).timestamp()
                except (TypeError, ValueError):
                    continue
                if started < cutoff:
                    db.execute("UPDATE tasks SET status='failed',error='超时兜底',finished_at=?,"
                               "updated_at=? WHERE run_id=? AND task_id=?",
                               (_tiers._now(), _tiers._now(), r["run_id"], r["task_id"]))
            # §7-②：等待态自愈（过期唤醒 / 到顶失败）——挂在这里是为了共用这个已有的心跳，
            # 不为等待态另起一个 loop。
            resume_expired_waits()
        except Exception:  # noqa: BLE001
            pass
