#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-4 提交/接受分离（src/tasks.py）。

守的判据（手册 B12：「把『执行者提交成功』与『审核方接受』分开，提交只是候选成果」）：

1. **过 verifier ≠ 被接受**：`AUTO_ACCEPT=0` 时产物过 verifier 只到 `submitted`，
   不是 `success`（负向：直接置 success 就红）。
2. **只有 `submitted` 可被接受**：重复接受 / 接受 failed-running-pending 一律 404，
   不得把任何行洗成 success。
3. **run 在 submitted 上挂起但不误判死锁**：下游 pending 不得被标「疑似悬挂」。
4. **接受后下游就绪**：accept → `_execute_run` 能把下游跑起来。
5. **默认行为不变**：`AUTO_ACCEPT` 默认为 1（旧行为），且此时也写 submitted_at/accepted_at
   （于是「提交→接受」时延在两种模式下都可度量）。

分层口径（tests/tiers.py）：临时 db + 假 chat_fn + `_start_runner` 置桩，不起服务、不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import db          # noqa: E402
import tasks       # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0accept-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self._saved_conc = tasks.MAX_CONCURRENCY
        self._saved_sleep = tasks.LOOP_SLEEP_S
        self._saved_auto = tasks.AUTO_ACCEPT
        tasks.MAX_CONCURRENCY = 1
        tasks.LOOP_SLEEP_S = 0.01
        self.started = []
        self._saved_start = tasks._start_runner
        tasks._start_runner = lambda rid: self.started.append(rid)
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tasks.MAX_CONCURRENCY = self._saved_conc
        tasks.LOOP_SLEEP_S = self._saved_sleep
        tasks.AUTO_ACCEPT = self._saved_auto
        tasks._start_runner = self._saved_start
        tasks.set_context(chat_fn=None)

    def _insert(self, run_id, tid, deps=()):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending','nonempty',?,?)",
            (run_id, tid, run_id, "g", "do " + tid, "claude",
             tasks.json.dumps(list(deps)), now, now))

    def _row(self, tid):
        return db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]

    def _set_chat(self, default=None):
        default = default if default is not None else {"success": True, "response": "real answer"}

        async def chat(agent_id, message, trace_id=None):
            return default
        tasks.set_context(chat_fn=chat)

    def _run_task_once(self, tid):
        row = self._row(tid)
        asyncio.run(tasks._run_task(row["run_id"], row))

    def _run_once(self, run_id):
        asyncio.run(tasks._execute_run(run_id))


class TestPureJudge(unittest.TestCase):
    def test_can_accept_only_submitted(self):
        self.assertTrue(tasks.can_accept("submitted"))
        for s in ("success", "pending", "running", "verifying", "failed", "blocked",
                  "cancelled", "waiting_input", "paused"):
            self.assertFalse(tasks.can_accept(s), "%s 不该可接受" % s)

    def test_auto_accept_defaults_on(self):
        """默认必须与 P1-4 之前一致（过了 verifier 即 success），否则是行为回归。"""
        self.assertTrue(tasks.AUTO_ACCEPT)

    def test_submitted_is_its_own_group(self):
        self.assertNotIn("submitted", tasks.PARKED_STATUSES)
        self.assertNotIn("submitted", tasks.TERMINAL_STATUSES)
        self.assertIn("submitted", tasks.TASK_STATUSES)


class TestManualAcceptance(_Base):
    def test_pass_with_manual_accept_stops_at_submitted(self):
        tasks.AUTO_ACCEPT = False
        self._insert("r1", "t1")
        self._set_chat()
        self._run_task_once("t1")
        row = self._row("t1")
        self.assertEqual(row["status"], "submitted", "过 verifier 不得直接 success")
        self.assertIsNotNone(row["submitted_at"], "submitted_at 必须写（否则时延不可度量）")
        self.assertIsNone(row["accepted_at"], "未被接受则 accepted_at 必须为空")
        self.assertEqual(row["verdict"], "pass")

    def test_accept_moves_submitted_to_success(self):
        tasks.AUTO_ACCEPT = False
        self._insert("r2", "t1")
        self._set_chat()
        self._run_task_once("t1")
        asyncio.run(tasks.accept_run("r2", tasks.AcceptIn(task_id="t1", by="alice")))
        row = self._row("t1")
        self.assertEqual(row["status"], "success")
        self.assertEqual(row["accepted_by"], "alice")
        self.assertIsNotNone(row["accepted_at"])
        self.assertIsNotNone(row["finished_at"])
        self.assertIn("r2", self.started, "接受后必须重启 runner 让下游就绪")

    def test_reject_moves_submitted_to_failed_with_reason(self):
        tasks.AUTO_ACCEPT = False
        self._insert("r3", "t1")
        self._set_chat()
        self._run_task_once("t1")
        asyncio.run(tasks.reject_run("r3", tasks.RejectIn(task_id="t1", reason="缺数据来源")))
        row = self._row("t1")
        self.assertEqual(row["status"], "failed")
        self.assertIn("缺数据来源", row["error"])

    def test_accept_404_when_nothing_submitted(self):
        self._insert("r4", "t1")
        self.assertEqual(self._row("t1")["status"], "pending")
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(tasks.accept_run("r4", tasks.AcceptIn(task_id="t1")))
        self.assertEqual(cm.exception.status_code, 404)

    def test_accept_does_not_rewrite_success_or_failed(self):
        """重复接受 / 接受失败任务都是 no-op —— 不得把任何行洗成 success。"""
        self._insert("r5", "t1")
        self._insert("r5", "t2")
        now = tasks._now()
        db.execute("UPDATE tasks SET status='success',created_at=?,updated_at=? "
                   "WHERE task_id='t1'", (now, now))
        db.execute("UPDATE tasks SET status='failed',error='boom',updated_at=? "
                   "WHERE task_id='t2'", (now,))
        with self.assertRaises(HTTPException):
            asyncio.run(tasks.accept_run("r5", tasks.AcceptIn(task_id="t1")))
        with self.assertRaises(HTTPException):
            asyncio.run(tasks.accept_run("r5", tasks.AcceptIn(task_id="t2")))
        self.assertEqual(self._row("t1")["status"], "success")
        self.assertIsNone(self._row("t1")["accepted_by"])
        self.assertEqual(self._row("t2")["status"], "failed")


class TestRunParksOnSubmitted(_Base):
    def test_pending_downstream_not_marked_deadlocked(self):
        tasks.AUTO_ACCEPT = False
        self._insert("p1", "t1")
        self._insert("p1", "t2", deps=["t1"])
        self._set_chat()
        self._run_once("p1")
        self.assertEqual(self._row("t1")["status"], "submitted")
        self.assertEqual(self._row("t2")["status"], "pending",
                         "下游 pending 是被上游等待，不得被标「疑似悬挂」")

    def test_accept_then_downstream_runs(self):
        tasks.AUTO_ACCEPT = False
        self._insert("p2", "t1")
        self._insert("p2", "t2", deps=["t1"])
        self._set_chat()
        self._run_once("p2")
        self.assertEqual(self._row("t2")["status"], "pending")
        asyncio.run(tasks.accept_run("p2", tasks.AcceptIn(task_id="t1")))
        self.assertEqual(self._row("t1")["status"], "success")
        self._run_once("p2")
        t2 = self._row("t2")
        # ⚠ 断言不能写 "success"：手动接受模式下**下游自己也会停在 submitted**。
        # 要证的是「下游被调度并跑出了候选产物」—— submitted + submitted_at 正好是这个证据。
        self.assertEqual(t2["status"], "submitted",
                         "接受上游后下游必须就被跑起来（并同样停在待接受）")
        self.assertIsNotNone(t2["submitted_at"])


class TestAutoAcceptBackCompat(_Base):
    def test_default_path_still_success_and_records_both_timestamps(self):
        self._insert("b1", "t1")
        self._set_chat()
        self._run_task_once("t1")
        row = self._row("t1")
        self.assertEqual(row["status"], "success", "默认（AUTO_ACCEPT=1）行为必须不变")
        self.assertEqual(row["accepted_by"], "auto")
        self.assertIsNotNone(row["submitted_at"])
        self.assertIsNotNone(row["accepted_at"])


if __name__ == "__main__":
    unittest.main()