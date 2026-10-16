#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：任务状态机（src/tasks.py）P0-3 —— 等待态 / 预算 / 执行者不得自证 success。

【本文件守五条】
1. **执行者不得自证 success**：回包 `success=True` 但产物过不了 `verifier` ⇒ **不得**置 success
   （负向 RED→GREEN；这是 P0-3 的主判据）。
2. **等待态是一等状态**：执行者回「等人/等批」⇒ 任务进 waiting_*，**不是** running；
   同一 run 里其它任务照常推进（不占死并发槽、不把整条 run 卡死）。
3. **等待态不触发「疑似悬挂」**：下游 pending 只是**被上游等待**，不得被误判为调度死锁。
4. **预算**：`budget_ms` 只在超限时把任务置 failed，不静默放过（纯函数单测 + 集成各一）。
5. 状态枚举是契约：terminal / parked / active 三组互不重叠。

分层口径（tests/tiers.py）：临时 db + 假 chat_fn，不起服务、不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import db      # noqa: E402
import tasks   # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0tasks-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self._saved_conc = tasks.MAX_CONCURRENCY
        self._saved_sleep = tasks.LOOP_SLEEP_S
        tasks.MAX_CONCURRENCY = 1
        tasks.LOOP_SLEEP_S = 0.01
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tasks.MAX_CONCURRENCY = self._saved_conc
        tasks.LOOP_SLEEP_S = self._saved_sleep
        tasks.set_context(chat_fn=None)

    def _insert(self, run_id, tid, deps=(), *, verify_kind="nonempty",
                budget_ms=None, budget_tokens=None, prompt=None):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,budget_ms,budget_tokens,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?)",
            (run_id, tid, run_id, "g", prompt or ("do " + tid), "claude",
             tasks.json.dumps(list(deps)), verify_kind, budget_ms, budget_tokens, now, now))

    def _status(self, tid):
        return db.query("SELECT status,error,output FROM tasks WHERE task_id=?", (tid,))[0]

    def _set_chat(self, mapping, default=None):
        """mapping: {子串: 回包}；按 message 里是否含子串选回包。"""
        default = default if default is not None else {"success": True, "response": "default-ok"}

        async def chat(agent_id, message, trace_id=None):
            for key, val in mapping.items():
                if key in message:
                    return val
            return default
        tasks.set_context(chat_fn=chat)

    def _run_once(self, run_id):
        asyncio.run(tasks._execute_run(run_id))

    def _run_task_once(self, tid):
        row = db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]
        asyncio.run(tasks._run_task(row["run_id"], row))


class TestStatusEnum(unittest.TestCase):
    def test_terminal_parked_active_partition(self):
        t = set(tasks.TERMINAL_STATUSES)
        p = set(tasks.PARKED_STATUSES)
        s = set(tasks.SUBMITTED_STATUSES)          # P1-4 待接受态：第四组
        self.assertEqual(set(), t & p, "终态与等待态不得重叠")
        self.assertEqual(set(), t & s, "终态与待接受态不得重叠")
        self.assertEqual(set(), p & s, "等待态与待接受态不得重叠（出口不同：/resume vs /accept）")
        for x in ("success", "failed", "blocked", "cancelled"):
            self.assertIn(x, t)
        for x in ("waiting_input", "waiting_approval", "paused"):
            self.assertIn(x, p)
        for x in ("submitted",):
            self.assertIn(x, s)
        self.assertEqual(set(tasks.TASK_STATUSES),
                         t | p | s | {"pending", "running", "verifying"})


#: P0-3 之前的 tasks 表形态（无 verify_kind/budget 列）—— 用来验幂等 ALTER 迁移。
_OLD_TASKS_SCHEMA = """
CREATE TABLE tasks (
    run_id TEXT NOT NULL, task_id TEXT NOT NULL, trace_id TEXT, goal TEXT,
    prompt TEXT NOT NULL, agent_id TEXT, deps TEXT NOT NULL DEFAULT '[]',
    status TEXT NOT NULL DEFAULT 'pending', output TEXT, error TEXT,
    started_at TEXT, finished_at TEXT, duration_ms INTEGER,
    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
    PRIMARY KEY (run_id, task_id)
);
"""

_NEW_TASK_COLUMNS = ("verify_kind", "verify_expect", "budget_ms", "budget_tokens",
                     "spent_ms", "spent_tokens")


class TestMigration(_Base):
    """存量库迁移：新增列靠幂等 ALTER，**不动既有行**（旧行 budget=NULL=不限）。"""

    def test_existing_tasks_table_gets_new_columns_without_touching_rows(self):
        db.execute_script("DROP TABLE IF EXISTS tasks")
        db.execute_script(_OLD_TASKS_SCHEMA)
        db.execute("INSERT INTO tasks(run_id,task_id,prompt,status,created_at,updated_at)"
                   " VALUES('r','t','p','success','a','a')")
        tasks.ensure_schema()
        cols = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
        for c in _NEW_TASK_COLUMNS:
            self.assertIn(c, cols, "存量库缺列 %s" % c)
        row = db.query("SELECT * FROM tasks WHERE task_id='t'")[0]
        self.assertEqual(row["status"], "success", "迁移不得改动既有行")
        self.assertEqual(row["verify_kind"], "nonempty", "新增列应取 DEFAULT")
        self.assertIsNone(row["budget_ms"], "旧行预算为 NULL＝不设限")

    def test_ensure_schema_is_idempotent(self):
        tasks.ensure_schema()
        tasks.ensure_schema()
        cols = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
        self.assertIn("verify_kind", cols)


class TestVerifierGate(_Base):
    """验收① ：未过 Verifier 不得置 success。"""

    def test_runner_success_with_empty_output_is_not_success(self):
        self._insert("r1", "t1")
        self._set_chat({"do t1": {"success": True, "response": "   "}})
        self._run_task_once("t1")
        row = self._status("t1")
        self.assertEqual(row["status"], "failed",
                         "执行者自称 success 但产物为空 ⇒ 不得置 success")
        self.assertIn("empty_output", row["error"])

    def test_runner_success_with_valid_output_is_success(self):
        self._insert("r2", "t1")
        self._set_chat({"do t1": {"success": True, "response": "real answer"}})
        self._run_task_once("t1")
        self.assertEqual(self._status("t1")["status"], "success")

    def test_kind_json_array_rejects_non_json_output(self):
        self._insert("r3", "t1", verify_kind="json_array")
        self._set_chat({"do t1": {"success": True, "response": "plain text"}})
        self._run_task_once("t1")
        row = self._status("t1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("json", row["error"].lower())

    def test_missing_output_is_insufficient_and_not_success(self):
        self._insert("r4", "t1")
        self._set_chat({"do t1": {"success": True, "response": None}})
        self._run_task_once("t1")
        row = self._status("t1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("insufficient", row["error"])

    def test_unknown_verify_kind_is_not_success(self):
        self._insert("r5", "t1", verify_kind="no_such_kind")
        self._set_chat({"do t1": {"success": True, "response": "anything"}})
        self._run_task_once("t1")
        row = self._status("t1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("insufficient", row["error"])


class TestWaitingState(_Base):
    """验收② ：等待态释放执行资源。"""

    def test_waiting_input_parks_without_blocking_sibling(self):
        self._insert("rw", "t1")
        self._insert("rw", "t2")
        self._set_chat({"do t1": {"waiting": "waiting_input"},
                        "do t2": {"success": True, "response": "sibling-ok"}})
        self._run_once("rw")            # 并发=1：等待态不释放则 t2 永远跑不完
        self.assertEqual(self._status("t1")["status"], "waiting_input")
        self.assertEqual(self._status("t2")["status"], "success",
                         "等待态必须让出执行资源，兄弟任务应照常完成")

    def test_waiting_task_is_not_running(self):
        self._insert("rw2", "t1")
        self._set_chat({"do t1": {"waiting": "waiting_approval"}})
        self._run_task_once("t1")
        self.assertEqual(self._status("t1")["status"], "waiting_approval")

    def test_downstream_of_waiting_is_not_declared_deadlocked(self):
        """下游 pending 只是被上游等待，不得被误判成「疑似悬挂」。"""
        self._insert("rw3", "t1")
        self._insert("rw3", "t2", deps=("t1",))
        self._set_chat({"do t1": {"waiting": "waiting_input"}})
        self._run_once("rw3")
        self.assertEqual(self._status("t1")["status"], "waiting_input")
        self.assertEqual(self._status("t2")["status"], "pending",
                         "上游在等待时下游应保持 pending，而不是 failed")

    def test_resume_requeues_waiting_tasks(self):
        self._insert("rw4", "t1")
        db.execute("UPDATE tasks SET status='waiting_input' WHERE task_id='t1'")
        calls = []
        self._saved_start = tasks._start_runner
        tasks._start_runner = lambda rid: calls.append(rid)
        try:
            n = tasks.requeue_waiting("rw4")
        finally:
            tasks._start_runner = self._saved_start
        self.assertEqual(n, 1)
        self.assertEqual(self._status("t1")["status"], "pending")


class TestBudget(_Base):
    def test_budget_exceeded_is_pure_and_typed(self):
        self.assertIsNone(tasks.budget_exceeded({"budget_ms": 1000, "budget_tokens": 100}, 10, 5))
        self.assertIn("耗时", tasks.budget_exceeded({"budget_ms": 100}, 101, 0) or "")
        self.assertIn("token", tasks.budget_exceeded({"budget_tokens": 10}, 1, 11) or "")
        self.assertIsNone(tasks.budget_exceeded({"budget_ms": None, "budget_tokens": None}, 9e9, 9e9))

    def test_over_budget_task_is_failed_not_success(self):
        self._insert("rb", "t1", budget_ms=0)

        async def slow_chat(agent_id, message, trace_id=None):
            await asyncio.sleep(0.02)
            return {"success": True, "response": "ok"}
        tasks.set_context(chat_fn=slow_chat)
        self._run_task_once("t1")
        row = self._status("t1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("预算", row["error"])


if __name__ == "__main__":
    unittest.main()