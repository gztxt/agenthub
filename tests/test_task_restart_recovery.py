#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：重启恢复（`tasks_runner.recover_interrupted_tasks`）。

【本文件守四条】
1. **重启 ≠ 失败**：启动时 `running` 必须回到 `pending` 并带恢复注记，**不得**直接判
   `failed`（旧实现 `'服务重启恢复:标记中断'` 把「服务重启」与「任务失败」混成同一件事）。
2. **必须重新拉起 runner**：改成 pending 却不起 runner，run 就永远停着 —— 那只是把
   「静默判死」换成「静默挂死」。判据：每个受影响 run 恰好 `_start_runner` 一次。
3. **注记单次使用**：拼进 prompt 后立即清空，否则每次自动重试都会重复一遍。
4. **只动 running**：终态（success/failed/blocked/cancelled）与 waiting_* 一律不许被复活。

依据：DeepSeek dsh-superagent `docs/003-execution.md:144-152`（"host restarted" +
recovery note：先查目录/git/外部系统，再重复对外动作）。

分层口径（tests/tiers.py）：临时 db + 假 chat_fn，不起服务、不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import db      # noqa: E402
import tasks   # noqa: E402
import tasks_runner as runner  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0recover-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self.addCleanup(self._restore)
        # 拦掉真实 runner：本文件只验「有没有被叫」，不真跑调度循环。
        self.started = []
        self._patch = mock.patch.object(runner, "_start_runner",
                                        side_effect=lambda rid: self.started.append(rid))
        self._patch.start()
        self.addCleanup(self._patch.stop)

    def _restore(self):
        db._conn = self._saved
        tasks.set_context(chat_fn=None)

    def _insert(self, run_id, tid, status, *, note=None, prompt=None):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "recovery_note,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'[]',?,?,?,?)",
            (run_id, tid, run_id, "g", prompt or ("do " + tid), "claude",
             status, note, now, now))

    def _row(self, tid):
        return db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]


class TestRecoverInterrupted(_Base):
    def test_running_becomes_pending_with_note(self):
        self._insert("r1", "t1", "running")
        n = runner.recover_interrupted_tasks()
        self.assertEqual(1, n)
        row = self._row("t1")
        self.assertEqual("pending", row["status"], "重启打断的任务必须回到 pending，不是 failed")
        self.assertIsNone(row["error"], "重跑的任务不该顶着上一次的重启错误")
        self.assertTrue((row["recovery_note"] or "").strip(), "必须带恢复注记")
        self.assertIn("先核对现场", row["recovery_note"])

    def test_only_running_is_touched(self):
        for tid, st in (("t_succ", "success"), ("t_fail", "failed"),
                        ("t_blk", "blocked"), ("t_cancel", "cancelled"),
                        ("t_wait", "waiting_input"), ("t_sub", "submitted"),
                        ("t_pend", "pending")):
            self._insert("r1", tid, st)
        self._insert("r1", "t_run", "running")
        n = runner.recover_interrupted_tasks()
        self.assertEqual(1, n, "只该恢复 running 一条")
        for tid, st in (("t_succ", "success"), ("t_fail", "failed"),
                        ("t_blk", "blocked"), ("t_cancel", "cancelled"),
                        ("t_wait", "waiting_input"), ("t_sub", "submitted"),
                        ("t_pend", "pending")):
            self.assertEqual(st, self._row(tid)["status"], "%s 不得被重启恢复改动" % tid)
            self.assertIsNone(self._row(tid)["recovery_note"], "%s 不该被打上注记" % tid)

    def test_runner_started_once_per_run(self):
        self._insert("r1", "t1", "running")
        self._insert("r1", "t2", "running")
        self._insert("r2", "t3", "running")
        runner.recover_interrupted_tasks()
        self.assertEqual(1, self.started.count("r1"), "同一条 run 只起一个 runner")
        self.assertEqual(1, self.started.count("r2"))
        self.assertEqual(2, len(self.started))

    def test_noop_when_nothing_running(self):
        self._insert("r1", "t1", "pending")
        self.assertEqual(0, runner.recover_interrupted_tasks())
        self.assertEqual([], self.started, "没东西可恢复时不得空起 runner")

    def test_idempotent(self):
        self._insert("r1", "t1", "running")
        self.assertEqual(1, runner.recover_interrupted_tasks())
        self.assertEqual(0, runner.recover_interrupted_tasks(), "第二次跑不该再恢复同一行")
        self.assertEqual(1, self.started.count("r1") if self.started else 0)


class TestRecoveryNoteInPrompt(_Base):
    def test_note_lands_at_top_of_prompt(self):
        self._insert("r1", "t1", "pending", note=runner.RECOVERY_NOTE, prompt="修这个 bug")
        prompt = asyncio.run(runner._prepare_prompt("r1", self._row("t1")))
        self.assertTrue(prompt.startswith("—— 上一次执行被服务重启中断 ——"),
                        "注记必须是最外层（先定纪律，再谈任务）")
        self.assertIn(runner.RECOVERY_NOTE, prompt)
        self.assertIn("修这个 bug", prompt)

    def test_no_note_no_banner(self):
        self._insert("r1", "t1", "pending")
        prompt = asyncio.run(runner._prepare_prompt("r1", self._row("t1")))
        self.assertNotIn("服务重启", prompt)

    def test_note_cleared_after_dispatch(self):
        """派发一次后注记必须清空（单次使用），且这次 prompt 里确实带上了。"""
        self._insert("r1", "t1", "pending", note=runner.RECOVERY_NOTE)
        seen = {}

        async def chat(agent_id, message, trace_id=None):
            seen["prompt"] = message
            return {"success": True, "response": "ok", "waiting": "waiting_input"}

        tasks.set_context(chat_fn=chat)
        asyncio.run(runner._run_task("r1", self._row("t1")))
        self.assertIn("先核对现场", seen.get("prompt", ""), "首跑必须带注记")
        self.assertIsNone(self._row("t1")["recovery_note"], "拼完 prompt 即清空")


if __name__ == "__main__":
    unittest.main(verbosity=2)
