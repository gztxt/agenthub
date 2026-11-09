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
import logging
import os
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Optional, Tuple

import adapters.cli_agent as _cli_agent
import chain_judge
import db
import verifier

import tasks_tiers as _tiers

log = logging.getLogger(__name__)

_runners: Dict[str, asyncio.Task] = {}

#: 无 `usage` 时的 token 估算粒度（字符/token）。见 `_estimate_tokens`。
TOKENS_PER_CHAR = 4

#: 本文件自建的三列。`tasks.py` 的 `_TASK_COLUMNS` 不属于本任务归属范围（并行改动会打架），
#: 所以 ALTER 放在这里，由 `ensure_task_columns()` 幂等执行。
#:   model_sent        —— 我们**实际下发**的 model（决议面）；`model` 保持既有契约=回包观测值
#:   tokens_estimated  —— 1 = spent_tokens 是估算值；0 = 真值
#:   last_fail_kind    —— 'timeout' 等失败种类；供降档决议读
_TASK_COLUMNS_EXTRA = (
    ("model_sent", "TEXT"),
    ("tokens_estimated", "INTEGER DEFAULT 0"),
    ("last_fail_kind", "TEXT"),
    # —— 心跳判据（2026-11-03 混排取证后新增）——
    # 病根：claude-code 挂住不返回时，旧逻辑要等满 TASK_TIMEOUT_S(600s) 才升级，
    # 且这次隐藏重试既不进 attempts 也不进 duration_ms ⇒ 统计口径系统性低估
    # （实测 e91b24b879 的 t3 表面 35.5s，实际先吃满 600s 才升级 codex）。
    # 心跳信号源＝子进程 CPU ticks（utime+stime，/proc/<pid>/stat 字段 11-12）。
    # ticks 增长 ⇒ 真在干活；N 秒无增长 ⇒ 挂住，提前升级，不必等满总时长。
    # 这避免误杀 120-590s 的合法长任务（本轮实测有 15 个成功任务跑在这个区间）。
    ("last_progress_at", "TEXT"),
    ("last_cpu_ticks", "INTEGER"),
)


# 心跳判据：连续这么多秒 CPU ticks 无增长即判挂住（区别于总时长 TASK_TIMEOUT_S）。
HEARTBEAT_STALL_S = int(os.getenv("TASK_HEARTBEAT_STALL_S", "120"))


def _proc_cpu_ticks(pid: int):
    """读子进程累计 CPU ticks（utime+stime）。进程已消失/字段异常 ⇒ None。

    判据是**增量**不是绝对值：本轮 ticks − 上轮 ticks > 0 才算有心跳。这把
    「CPU 低但真在跑」（等网络 I/O）与「CPU 零增长且已停」（挂住）区分开。
    """
    try:
        with open("/proc/%d/stat" % int(pid), "r") as fh:
            # comm 字段可能含空格/括号 ⇒ 从最后一个 ')' 后切，后面依次是
            # state(0) ppid(1) pgrp(2) … utime(11) stime(12)。
            fields = fh.read().rsplit(")", 1)[1].split()
        return int(fields[11]) + int(fields[12])
    except Exception:  # noqa: BLE001 — 进程消失是常态，不是错误
        return None

#: 已确认补过列的目标库（`db.current_path()`），避免每个任务都跑一遍 PRAGMA。
_columns_ready = set()


def ensure_task_columns() -> list:
    """幂等补三列：先查 `PRAGMA table_info(tasks)`，缺哪列补哪列。

    返回本次**实际新增**的列名。已存在 ⇒ 不重复 ALTER、不报错；并发下另一进程刚加完
    （SQLite 的 duplicate column name）也一律吞掉 —— 判据是「跑两遍不炸」，不是「跑一遍成功」。
    """
    try:
        path = db.current_path()
    except Exception:  # noqa: BLE001 — 旧版 db 无 current_path
        path = None
    if path in _columns_ready and path is not None:
        return []
    try:
        have = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
    except Exception:  # noqa: BLE001 — 库未就绪 / 无 tasks 表：交给 ensure_schema
        return []
    added = []
    for col, decl in _TASK_COLUMNS_EXTRA:
        if col in have:
            continue
        try:
            db.execute("ALTER TABLE tasks ADD COLUMN %s %s" % (col, decl))
            added.append(col)
        except Exception:  # noqa: BLE001 — 并发重复列：视为已完成
            pass
    if path is not None:
        _columns_ready.add(path)
    return added


def _tiercfg_mod():
    """取 `tiercfg` 模块；取不到（并行开发未落地 / import 失败）⇒ None，调用方回落老行为。"""
    try:
        import tiercfg
    except Exception:  # noqa: BLE001
        return None
    return tiercfg


def _timeout_s_for(tier) -> int:
    """该档位的超时秒数。优先 `tiercfg.timeout_for_tier`，取不到 ⇒ 全局 `TASK_TIMEOUT_S`。

    **绝不因函数缺失/抛异常而阻塞**：A 的改动没落地时，行为与改动前完全一致。
    """
    mod = _tiercfg_mod()
    fn = getattr(mod, "timeout_for_tier", None) if mod is not None else None
    if callable(fn):
        try:
            val = int(fn(tier))
        except Exception:  # noqa: BLE001
            return _tiers.TASK_TIMEOUT_S
        if val > 0:
            return val
    return _tiers.TASK_TIMEOUT_S


def _prev_tier_of(tier) -> Optional[str]:
    """降一档。`tiercfg.prev_tier` 缺失/抛异常/已触底 ⇒ None（调用方保持原档）。"""
    mod = _tiercfg_mod()
    fn = getattr(mod, "prev_tier", None) if mod is not None else None
    if not callable(fn):
        return None
    try:
        return fn(tier) or None
    except Exception:  # noqa: BLE001
        return None


def _first_usable_of(tier, catalog, checked) -> Optional[str]:
    """该档位下第一个在清单里的模型；清单取不到 ⇒ None（调用方保留原模型，只降档号）。"""
    mod = _tiercfg_mod()
    fn = getattr(mod, "first_usable", None) if mod is not None else None
    if not callable(fn):
        return None
    try:
        tiers_cfg = mod.parse_tiers()
        model, _why = fn(tier, tiers_cfg, catalog, checked)
    except Exception:  # noqa: BLE001
        return None
    return model or None


def _estimate_tokens(prompt, response) -> int:
    """无 `usage` 时的 token 估算（可解释规则，不调用分词器）。

    规则：`(len(prompt) + len(response)) // TOKENS_PER_CHAR`，最小 1。
    依据：英文本约 4 字符/token，中文约 1~1.5 字/token；取 4 是**下界**，
    宁可低估也不虚增成本口径。调用发生过就至少花 1 token，故下限为 1。
    """
    text = "%s%s" % (prompt or "", response or "")
    return max(1, len(text) // TOKENS_PER_CHAR)


def _fix_retry_columns(run_id: str, task_id: str, exec_model, used_model,
                       tokens_estimated: int = 0, last_fail_kind=None) -> None:
    """补两条**新增**观测列：`model_sent`=本次实际下发值、`tokens_estimated`（+可选 `last_fail_kind`）。

    ⚠ **不写 `model` 列**：那是既有契约（adapter 回包观测值）。2026-11-03 集成裁决：
    「实发值」与「回包值」分成两列，才能同时答出「我们让谁跑」与「实际谁跑了」；
    把 `model` 改成实发值会吃掉 P0-a 的观测面（`test_task_attribution` 会红）。

    保留 `used_model` 形参只为调用点兼容（回包值仍由既有的 `model=?` UPDATE 写入）。
    """
    sets = ["model_sent=?", "tokens_estimated=?"]
    params: list = [exec_model, int(tokens_estimated)]
    if last_fail_kind is not None:
        sets.append("last_fail_kind=?")
        params.append(last_fail_kind)
    params += [run_id, task_id]
    try:
        db.execute("UPDATE tasks SET " + ",".join(sets) + " WHERE run_id=? AND task_id=?",
                   tuple(params))
    except Exception:  # noqa: BLE001 — 观测列写不进去不影响任务本身
        pass


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
                     model: Optional[str], tier=None):
    """按 chat_fn 的实际签名决定要不要把选型模型传下去。

    传不下去时**不报错**（拆解意图不等于执行契约），但这条事实是可查的：
    `model_choice` 有值而 `model`（实发回包）是另一个值，就是「选了没生效」的证据。

    超时（2026-10-29）：adapter 一侧必须**晚于**外层 `asyncio.wait_for` 超时，
    否则 TASK_TIMEOUT_S 这道闸永不触发 —— 症状是所有长任务都被记成「执行者失败」，
    归因错一层（真实原因是客户端 120s 先断）。
    """
    kw: Dict[str, Any] = {}
    if _accepts_kwarg(chat_fn, "timeout_s"):
        # 2026-11-03 缺陷1：adapter 侧超时按档位走（tier 未知 ⇒ 回落 TASK_TIMEOUT_S）。
        kw["timeout_s"] = _timeout_s_for(tier) + 30
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

    # ── 重启恢复注记：放最前（它定的是「先核对现场再动手」的总纪律，优先于其余上下文）──
    note = str(row.get("recovery_note") or "").strip()
    if note:
        prompt = "—— 上一次执行被服务重启中断 ——\n%s\n\n" % note + prompt
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
    # ── 2026-11-03 缺陷2：超时后**降一档**（不是升档）──────────────────
    # 超时的语义是「任务太重 / 预算不够」，升档只会更慢更难在预算内跑完
    # （实测 run 159ae98077 t2：codex+t3 超时 600s → 升档 claude-code + t3 再超 600s，
    #  两轮共 20 分钟零产出）。此处只对 `last_fail_kind='timeout'` 生效；其它失败仍走
    # `escalate_target` 的升档逻辑，逐字不变。
    if str(row.get("last_fail_kind") or "") == "timeout":
        nxt = _prev_tier_of(row.get("tier"))
        if nxt:
            new_model = _first_usable_of(nxt, cat, cat_ok) or exec_model
            db.execute("UPDATE tasks SET tier=?,tier_reason=?,model_planned=?,"
                       "escalated_from_model=?,updated_at=? WHERE run_id=? AND task_id=?",
                       (nxt, "downgrade:timeout", new_model, exec_model, _tiers._now(),
                        run_id, row["task_id"]))
            db.log_profile_event("task_exec", exec_agent, "downgrade", 0,
                                 trace_id=row["trace_id"],
                                 detail={"task": row["task_id"], "reason": "timeout",
                                         "from_tier": row.get("tier"), "to_tier": nxt,
                                         "from_model": exec_model, "to_model": new_model,
                                         "attempt": attempts_done})
            return exec_agent, new_model
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
    # 观测三列在 `_execute_run` 入口补过一次；直调 `_run_task` 的路径（L0 用例、恢复）
    # 没有那一步，这里再幂等补一次（已补过 ⇒ 只读一次 PRAGMA，代价可忽略）。
    try:
        ensure_task_columns()
    except Exception:  # noqa: BLE001
        pass
    chat_fn = _tiers._ctx.get("chat_fn")
    if not chat_fn:
        db.execute("UPDATE tasks SET status='failed',error='chat 引擎未就绪',updated_at=? "
                   "WHERE run_id=? AND task_id=?", (_tiers._now(), run_id, row["task_id"]))
        return
    prompt = await _prepare_prompt(run_id, row)
    if row.get("recovery_note"):
        # 单次使用：拼进 prompt 即失效，避免自动重试时重复同一段提示（会稀释成噪音）。
        # 再崩也不会丢：下一次启动按 status='running' 重新生成注记，不依赖这个旧值。
        db.execute("UPDATE tasks SET recovery_note=NULL,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   (_tiers._now(), run_id, row["task_id"]))
        row["recovery_note"] = None
    attempts_done = int(row.get("attempts") or 0)
    exec_agent, exec_model = _resolve_exec_target(run_id, row, attempts_done)
    prompt = await _attach_retrieval(run_id, row, prompt, exec_agent)
    # 缺陷1：本任务的超时按**它自己的档位**取（A 未落地时回落 TASK_TIMEOUT_S）。
    _tout = _timeout_s_for(row.get("tier"))
    # ── P1 命令幂等闸（借鉴 t3code 重放回执，只借逻辑不抄代码；实现见文件末尾
    #    「命令幂等回执」段）──
    # 全链路唯一对外派发点是下面的 `_call_chat`：它调 chat_fn，而 chat_fn 才是
    # 起 agent 子进程 / 发 LLM 请求 / 触发 git push 等外部动作的源头。claim 放在
    # 紧邻它、且唯一的位置：同一 (run,task,attempt) 在「服务重启 → 恢复重放」时
    # 会命中首次回执 ⇒ 跳过，不再产生第二次外部动作。
    is_new, _receipt = claim_command(_command_id_for(run_id, row), run_id, "dispatch_chat")
    if not is_new:
        # 命中回执 = 上一次生命周期已派发过该外部动作。为杜绝重复对外动作，
        # 不重新派发；首次执行被重启打断时其结果可能已丢失，故落 failed 待人工
        # 按现场核对后决定是否重跑（不静默假装成功、也不静默挂死）。
        db.execute("UPDATE tasks SET status='failed',error=?,finished_at=?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   ("重启恢复：外部动作已派发（command 回执命中），跳过以避免重复对外动作；"
                    "请核对现场后决定是否重跑", _tiers._now(), _tiers._now(),
                    run_id, row["task_id"]))
        return
    t0 = asyncio.get_event_loop().time()
    try:
        result = await asyncio.wait_for(
            _call_chat(chat_fn, exec_agent, prompt,
                       f"task-{row['trace_id']}-{row['task_id']}",
                       exec_model, row.get("tier")),
            timeout=_tout)
        dur = int((asyncio.get_event_loop().time() - t0) * 1000)
        spent_tokens = int(((result.get("usage") or {}).get("total_tokens")) or 0)
        # 缺陷3：回包不带 usage（claude-code 就是这样）⇒ 按字数估算，并与真值分开记账。
        # 记 0 会把「未知」与「真的没花」写成同一个值，成本统计单边失真（实测 570s 的任务记 0）。
        tokens_estimated = 0
        if spent_tokens <= 0:
            spent_tokens = _estimate_tokens(prompt, result.get("response") or "")
            tokens_estimated = 1
        # 缺陷4（集成裁决 2026-11-03）：`model` 保持**既有契约** = adapter 回包观测值
        # （P0-a 的理由：记请求值会把「我们以为用了谁」当成「实际用了谁」；
        #  tests/test_task_attribution.py 三个用例钉着它）。
        # 新增 `model_sent` 记**实际下发值**（决议面）—— 两列并存才答得出
        # 「哪个组合失败、失败时我们让谁跑」。实测 t3 钉 `alibaba/qwen3.8-max`、
        # 回包却是 `openrouter/nvidia/nemotron-3.5-super-vl:free`。
        used_model = result.get("model") or None
        _fix_retry_columns(run_id, row["task_id"], exec_model, used_model,
                           tokens_estimated, None)
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
    except asyncio.CancelledError:
        # 心跳停滞路径：`_execute_run` 检测到 CPU ticks 无增长后 `atask.cancel()` 到这里。
        # 不再排重试（_execute_run 已调 _schedule_exec_retry 升级换人），但必须把
        # 本次耗时**累加**进 spent_ms（不是覆盖）——否则隐藏重试的耗时仍不进账，
        # 统计口径继续低估（本轮实测：表面 35.5s 的任务实际先吃满 600s）。
        dur_c = int((asyncio.get_event_loop().time() - t0) * 1000)
        db.execute("UPDATE tasks SET spent_ms=COALESCE(spent_ms,0)+?,updated_at=?"
                   " WHERE run_id=? AND task_id=?",
                   (dur_c, _tiers._now(), run_id, row["task_id"]))
        raise
    except asyncio.TimeoutError:
        # 2026-10-15 P0-a 取证时发现既存缺陷（非本次引入）：原句把 `'超时熔断 %ds'`
        # 写成字面量却按 5 个参数传（SQL 只有 4 个占位符）⇒ sqlite3.ProgrammingError
        # 在**超时这个异常处理器内部**抛出，任务永远停在 running，且异常冒泡进
        # `_execute_run`。改成正常绑定，顺带把 model 一起记上。
        _err = "超时熔断 %ds" % _tout
        # 缺陷2：标上失败种类，下一次 `_resolve_exec_target` 据此**降档**而非继续升档。
        # ⚠ 顺序：`_schedule_exec_retry` 自己会写 `model=?`（传的是它收到的 exec_model），
        # 所以口径纠正必须在它**之后**做，否则会被它覆盖回来。
        if _tiers._schedule_exec_retry(run_id, row, _err, _err, int(row.get("attempts") or 0),
                                row.get("agent_id") or "", exec_model,
                                dur=_tout * 1000, used_model=exec_model):
            _fix_retry_columns(run_id, row["task_id"], exec_model, None, 0, "timeout")
            return
        db.execute("UPDATE tasks SET status='failed',error=?,spent_ms=?,model=NULL,"
                   "model_sent=?,tokens_estimated=0,last_fail_kind='timeout',"
                   "finished_at=?,updated_at=? WHERE run_id=? AND task_id=?",
                   (_err, _tout * 1000, exec_model, _tiers._now(), _tiers._now(),
                    run_id, row["task_id"]))
        db.log_profile_event("task_exec", row["agent_id"], "fail", _tout * 1000,
                             trace_id=row["trace_id"],
                             detail={"task": row["task_id"], "timeout": True,
                                     "tier": row.get("tier"), "exec_model": exec_model})
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
    # 缺陷3/4/2 的三个观测列由本模块自建（不动 tasks.py 的列清单，避免并行改动打架）。
    # 必须在本轮任何 UPDATE 之前补上，否则第一次 ``_fix_retry_columns`` 会静默失败。
    try:
        ensure_task_columns()
    except Exception:  # noqa: BLE001 — 补列失败不得让整条 run 起不来
        pass
    sem = asyncio.Semaphore(_tiers.MAX_CONCURRENCY)
    live: Dict[str, asyncio.Task] = {}
    _hb_prev: Dict[str, tuple] = {}   # task_id -> (上轮 cpu_ticks, 采样时刻)，心跳判据用
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
                # run 收尾必扫孤儿：2026-11-03 实测一条 run 在 01:53 已闭合，它派出的
                # `claude -p`（PPID=hub）43 分钟后仍在跑并持续读写仓库 —— 产物无人收集，
                # token 照烧。`_kill_group` 只覆盖超时分支，run 整体收敛时没有任何一处收子进程。
                try:
                    import adapters.cli_agent as _cli
                    killed = _cli.reap_orphans(os.getpid())
                    if killed:
                        db.log_profile_event("task_run", "runner", "reap_orphans", 0,
                                             trace_id=run_id,
                                             detail={"killed": killed, "run": run_id})
                except Exception:  # noqa: BLE001 — 回收失败不得带倒 run 收尾
                    pass
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
                    db.execute("UPDATE tasks SET status='running',started_at=?,"
                               "last_progress_at=?,last_cpu_ticks=NULL,updated_at=?"
                               " WHERE run_id=? AND task_id=?",
                               (_tiers._now(), _tiers._now(), _tiers._now(),
                                run_id, r["task_id"]))

                    async def guarded(row=r):
                        async with sem:
                            await _run_task(run_id, row)
                    live[r["task_id"]] = asyncio.create_task(guarded())

            # —— 心跳判据（缺陷1：600s 满超时才升级，且这次隐藏重试不进统计）——
            # 每轮对在跑任务采一次子进程 CPU ticks：增长 ⇒ 把 last_progress_at 推到现在；
            # 距 last_progress_at 已超 HEARTBEAT_STALL_S 且 ticks 无增长 ⇒ 判挂住，
            # **不等满 TASK_TIMEOUT_S**，直接取消并走 _schedule_exec_retry 升级换人。
            # 误杀防护：ticks 增长就永不判死，所以 120-590s 的合法长任务（本轮实测 15 个）
            # 只要还在干活就不会被中断；只有真挂住（CLI 无输出且 CPU 不动）才提前止损。
            if live:
                try:
                    pid_by_task = {t.get("tag"): p for p, t in
                                   ((x["pid"], x) for x in _cli_agent.live_processes())
                                   if t.get("tag")}
                    now_ts = datetime.now(timezone.utc).timestamp()
                    for tid, atask in list(live.items()):
                        if atask.done():
                            continue
                        pid = pid_by_task.get("task-%s-%s" % (run_id, tid))
                        ticks = _proc_cpu_ticks(pid) if pid else None
                        if ticks is None:
                            # 采不到子进程（未 spawn / 在等 sem / 已退出）⇒ 不判 stall。
                            # 要不然 semaphore 排队 >120s 的任务会被误杀（它根本没跑）。
                            continue
                        db.execute("UPDATE tasks SET last_progress_at=COALESCE(last_progress_at,?),"
                                   "last_cpu_ticks=?,updated_at=? WHERE run_id=? AND task_id=?",
                                   (_tiers._now(), ticks, _tiers._now(), run_id, tid))
                        prev = _hb_prev.get(tid)
                        if prev is not None and ticks > prev[0]:
                            db.execute("UPDATE tasks SET last_progress_at=?,last_cpu_ticks=?,"
                                       "updated_at=? WHERE run_id=? AND task_id=?",
                                       (_tiers._now(), ticks, _tiers._now(), run_id, tid))
                        _hb_prev[tid] = (ticks, now_ts)
                        row_hb = next((x for x in rows if x["task_id"] == tid), None)
                        if not row_hb:
                            continue
                        try:
                            lpa = datetime.fromisoformat(row_hb["last_progress_at"] or
                                                         row_hb["started_at"]).timestamp()
                        except (TypeError, ValueError):
                            continue
                        silent_s = now_ts - lpa
                        if silent_s < HEARTBEAT_STALL_S:
                            continue
                        # 挂住：取消并升级换人。_schedule_exec_retry 会 attempts+1、
                        # 记 retry 事件（attempt/escalate_to 入账），下轮重新派发。
                        atask.cancel()
                        live.pop(tid, None)
                        _hb_prev.pop(tid, None)
                        row_hb = db.query("SELECT * FROM tasks WHERE run_id=? AND task_id=?",
                                          (run_id, tid))
                        row_hb = row_hb[0] if row_hb else None
                        if not row_hb:
                            continue
                        _tiers._schedule_exec_retry(
                            run_id, row_hb,
                            "心跳停滞 %.0fs（CPU ticks 无增长），提前升级" % silent_s,
                            "heartbeat_stall", int(row_hb.get("attempts") or 0),
                            row_hb.get("agent_id") or "", row_hb.get("model_sent"),
                            dur=int(silent_s * 1000), used_model=row_hb.get("model_sent"),
                            kind="heartbeat_stall",
                            detail={"silent_s": int(silent_s), "cpu_ticks": ticks})
                except Exception:  # noqa: BLE001 — 心跳采样失败不得带倒调度主循环
                    pass
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


#: 重启恢复注记（2026-11-01，照 DeepSeek dsh-superagent `docs/003-execution.md:144-152`）。
#:
#: 为什么改：旧实现启动时无条件 `running → failed`（'服务重启恢复:标记中断'），把
#: **服务重启**与**任务失败**混成同一件事 —— 一次重启等于把当时所有在跑的活判死，
#: 且不留现场提示，重跑只能从零开始。dsh 的做法是：中断的 Assignment 以
#: "host restarted" 结束，新 Assignment **带一条 recovery note**，明确要求执行者
#: 先查自己的目录、git 历史与外部系统，再重复任何对外动作。
#:
#: 代价（dsh 原文同样承认）：重启那一刻可能已经产生了副作用，恢复重跑有**重复对外动作**
#: 的风险。用「注记 + 让执行者先核对现场」换「不静默丢活」，这是有意识的取舍，不是遗漏。
RECOVERY_NOTE = (
    "服务重启打断了上一次执行。先核对现场再继续：检查你的工作目录（未提交/未完成的改动）、"
    "git 历史与外部系统（GitHub / CI / 已经发出的消息）当前状态，判断上次实际做到哪一步。"
    "核对完成之前，不要重复任何对外动作（提交、推送、发消息、改线上配置）；"
    "已经做完的沿用结果，不要从头重做。"
)


# ─────────────────────────── 命令幂等回执（P1）───────────────────────────
# 借鉴 t3code `Orchestrator.ts:13992-14023` 的
# `INSERT … ON CONFLICT(command_id) DO NOTHING RETURNING` + 重放回执思路，
# **只借逻辑不抄代码**：一个稳定去重键 + 「首次落一行回执、重放返回首次回执」。
#
# 覆盖边界（如实口径，见交付说明）：
#   · 覆盖到「对外派发」这一层 —— `_run_task` 里调 chat_fn 的那一次派发。
#   · 不覆盖 agent 子进程**内部**发生的外部动作（它自己 git push / 发消息 / 改文件）：
#     那些动作在子进程里、chat_fn 返回之后才发生，回执无法回滚，也不在派发点之内。
#   · 回执写在派发**之前** ⇒ 语义是 at-most-once 派发。窄窗口（声明已落库、动作
#     还没真跑就重启）无法自证「动作到底跑没跑」，这是 at-most-once 的固有边界：
#     宁可漏一次（落 failed 待人核对），也不重复对外动作。
def _command_id_for(run_id: str, row: dict) -> str:
    """稳定 run 级去重键：同一 (run, task, attempt) 在重启前后逐字相同（不随机）。

    为什么要带 `attempts`：合法的自动重试 / 升档会 attempts+1 后重新派发，那是
    **应当**再发一次外部动作的新尝试；带上次数才不会把重试误去重成「跳过」。
    而重启恢复（`recover_interrupted_tasks`）不改 attempts ⇒ 恢复重放与被打断的
    首次派发命中同一个 command_id，从而实现「同一 run 重启恢复只产生一次外部动作」。
    """
    attempt = int(row.get("attempts") or 0)
    return "task-dispatch:%s:%s:%d" % (run_id, row["task_id"], attempt)


def claim_command(command_id: str, run_id: str, action: str) -> Tuple[bool, dict]:
    """领一次「外部派发」的幂等回执，返回 `(is_new, receipt)`。

    · 首次（command_id 没出现过）：INSERT 一行「已派发」声明 ⇒ `is_new=True`，
      调用方据此**执行**外部动作。
    · 重放（同一 command_id 再来一次，典型是服务重启恢复）：`ON CONFLICT DO NOTHING`
      让 INSERT 静默不写（rowcount=0）⇒ `is_new=False`，并返回**首次那行回执**，
      调用方据此**跳过**外部动作，避免重复对外副作用。

    去重键只有 `command_id`（主键）；传入的 run_id/action 仅用于首次落库，重放一律
    以首次回执为准。回执行**写一次不再 UPDATE**，所以重放拿到的就是首次值。
    回执面不可用（缺表等）时降级 fail-open 返回 `(True, ...)`：宁可照旧派发，
    也不让幂等闸带倒任务执行（与本仓「增强失败不阻塞」纪律一致）。
    """
    now = _tiers._now()
    receipt_decl = json.dumps({"dispatched": True}, ensure_ascii=False)
    try:
        n = db.execute(
            "INSERT INTO command_receipts(command_id,run_id,action,result_json,created_at)"
            " VALUES(?,?,?,?,?) ON CONFLICT(command_id) DO NOTHING",
            (command_id, run_id, action, receipt_decl, now))
        rows = db.query("SELECT * FROM command_receipts WHERE command_id=?", (command_id,))
    except Exception as e:  # noqa: BLE001 — 回执面不可用 ⇒ 降级照旧派发，不阻塞任务
        log.warning("[receipt] claim 失败（%s: %s），降级照旧派发", type(e).__name__, e)
        return True, {"command_id": command_id, "run_id": run_id, "action": action,
                      "result_json": receipt_decl, "created_at": now, "degraded": True}
    receipt = rows[0] if rows else {"command_id": command_id, "run_id": run_id,
                                    "action": action, "result_json": receipt_decl,
                                    "created_at": now}
    return (n == 1), receipt


def recover_interrupted_tasks() -> int:
    """启动时把被重启打断的 `running` 拉回 `pending`，写恢复注记并重新拉起 runner。

    返回被恢复的 task 数。三条设计约束：

    ① **只动 `running`**：`pending` 本来就没派发、`waiting_*` 是主动让出槽位，都不需要
       恢复注记；`verifying`/`submitted` 已有产物，重跑会浪费钱。
    ② **必须重新拉起 runner**：旧实现把所有行推到终态，run 自然收敛；改成 pending 之后
       若不起 runner，run 就永远停在那儿 —— 从「静默判死」变成「静默挂死」，更糟。
    ③ **注记单次使用**（由 `_run_task` 拼完 prompt 后清空）：否则每次自动重试都会重复
       一遍「先核对现场」，把注记稀释成噪音。若这次又崩在中途，下一次启动会重新写入
       一条新注记 —— 因为恢复是按 `status='running'` 重新生成的，不依赖旧值。
    """
    rows = db.query("SELECT run_id, task_id FROM tasks WHERE status='running'")
    if not rows:
        return 0
    now = _tiers._now()
    runs = set()
    for r in rows:
        db.execute("UPDATE tasks SET status='pending',recovery_note=?,error=NULL,updated_at=?"
                   " WHERE run_id=? AND task_id=? AND status='running'",
                   (RECOVERY_NOTE, now, r["run_id"], r["task_id"]))
        runs.add(r["run_id"])
    for rid in runs:
        _start_runner(rid)
    return len(rows)


async def sweep_stale_tasks():
    """服务启动恢复 + 周期兜底：打断的 running 拉回 pending，running 超时→failed。"""
    recover_interrupted_tasks()
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
