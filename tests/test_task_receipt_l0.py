#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：命令幂等回执（`tasks_runner.claim_command` + 恢复路径）。

借鉴 t3code `Orchestrator.ts` 的 `INSERT … ON CONFLICT(command_id) DO NOTHING
RETURNING` 重放回执思路（只借逻辑不抄代码）。守四条：

1. 同一 command_id 连续 claim 两次 ⇒ 第一次 True、第二次 False，且回执逐字一致。
2. 不同 command_id 互不影响（各自独立成行）。
3. 隔离库连跑 init_db 两次（含字面 :memory:）⇒ command_receipts 表在、幂等、
   不抹掉既有回执行。
4. 恢复路径重放时外部动作计数 = 1（mock 计数器，不真起进程 / 不联网）。

分层口径：临时 db + 假 chat_fn，不起服务、不 import src.main、不打网络。
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

import db               # noqa: E402
import tasks            # noqa: E402
import tasks_runner as runner  # noqa: E402
import tasks_tiers as tiers    # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0receipt-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        # chat_fn 是全局 _ctx，清掉免得泄漏给同进程后续用例
        tiers._ctx.pop("chat_fn", None)

    def _insert(self, run_id, tid, status, *, note=None):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "recovery_note,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,'[]',?,?,?,?)",
            (run_id, tid, run_id, "g", "do " + tid, "claude", status, note, now, now))

    def _row(self, tid):
        return db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]


class TestClaimCommand(_Base):
    def test_same_command_id_second_is_replay(self):
        is_new_1, r1 = runner.claim_command("cmd-A", "r1", "dispatch_chat")
        is_new_2, r2 = runner.claim_command("cmd-A", "r1", "dispatch_chat")
        self.assertTrue(is_new_1, "首次 claim 必须是 is_new=True")
        self.assertFalse(is_new_2, "同一 command_id 第二次必须是 is_new=False（重放）")
        self.assertEqual(r1, r2, "重放必须返回首次回执（逐字一致）")
        # 去重键只有 command_id：换 run_id / action 但键相同，仍算重放，且回执仍是首次那行
        is_new_3, r3 = runner.claim_command("cmd-A", "rX", "other")
        self.assertFalse(is_new_3)
        self.assertEqual(r1, r3)

    def test_different_command_ids_independent(self):
        a, _ = runner.claim_command("cmd-A", "r1", "dispatch_chat")
        b, _ = runner.claim_command("cmd-B", "r1", "dispatch_chat")
        c, _ = runner.claim_command("cmd-C", "r2", "dispatch_chat")
        self.assertTrue(a)
        self.assertTrue(b)
        self.assertTrue(c)
        self.assertEqual(3, len(db.query("SELECT * FROM command_receipts")),
                         "三个不同 command_id 应各自独立成行")

    def test_command_id_stable_and_attempt_scoped(self):
        row = {"task_id": "t1", "attempts": 0}
        same = runner._command_id_for("r1", row)
        self.assertEqual(same, runner._command_id_for("r1", {"task_id": "t1", "attempts": 0}),
                         "稳定标识：同 (run,task,attempt) 不随机、逐字相同")
        self.assertNotEqual(same, runner._command_id_for("r1", {"task_id": "t1", "attempts": 1}),
                            "attempt 变化 ⇒ 合法重试仍应当再派发一次，不能被误去重")


class TestSchemaIdempotent(_Base):
    def test_init_db_twice_table_exists_and_idempotent(self):
        path = self.tmp / "t.db"
        # setUp 已跑一次 init_db；这里同路径再跑两次 ⇒ 共三次 SCHEMA 迁移
        db.init_db(path)
        cols = {r["name"] for r in db.query("PRAGMA table_info(command_receipts)")}
        self.assertTrue({"command_id", "run_id", "action", "result_json", "created_at"} <= cols,
                        "command_receipts 至少含命令要求的五列")
        db.execute("INSERT INTO command_receipts(command_id,run_id,action,result_json,created_at)"
                   " VALUES('keep','r1','a','{}','ts')")
        db.init_db(path)
        self.assertEqual(1, len(db.query(
            "SELECT * FROM command_receipts WHERE command_id='keep'")),
            "幂等迁移不得抹掉既有回执行")
        self.assertEqual(1, len(db.query(
            "SELECT name FROM sqlite_master WHERE type='table' AND name='command_receipts'")),
            "表恰好一张")
        # 字面 :memory: 库：连续两次 init_db 不报错且表在
        saved = db._conn
        try:
            db.init_db(pathlib.Path(":memory:"))
            self.assertTrue(db.query(
                "SELECT name FROM sqlite_master WHERE name='command_receipts'"))
            db.init_db(pathlib.Path(":memory:"))
            self.assertTrue(db.query(
                "SELECT name FROM sqlite_master WHERE name='command_receipts'"))
        finally:
            db._conn = saved


class TestRecoveryReplayOneAction(_Base):
    def test_replay_does_not_repeat_external_action(self):
        self._insert("r1", "t1", "running")

        counter = {"n": 0}

        async def chat(agent_id, message, tag):
            # 唯一「外部动作」计数点：生产里 chat_fn 才是起子进程 / 发 LLM 的源头
            counter["n"] += 1
            return {"success": True, "response": "done"}

        tiers.set_context(chat_fn=chat)

        async def noop_verify(*a, **k):
            return None

        def resolve(run_id, row, attempts_done):
            return (row["agent_id"], None)

        with mock.patch.object(tiers, "TASK_RETRIEVAL", False), \
             mock.patch.object(tiers, "TASK_SKILL_INJECT", False), \
             mock.patch.object(runner, "_verify_and_settle", new=noop_verify), \
             mock.patch.object(runner, "_resolve_exec_target", new=resolve), \
             mock.patch.object(runner, "_start_runner", side_effect=lambda rid: None):
            # 生命周期 1：正常派发，随后被服务重启打断
            asyncio.run(runner._run_task("r1", self._row("t1")))
            self.assertEqual(1, counter["n"], "首次派发应产生 1 次外部动作")

            # 模拟「重启发生在 running 期间」：把现场摆回 running + 首次的 attempts=0
            db.execute("UPDATE tasks SET status='running',attempts=0 WHERE task_id='t1'")
            self.assertEqual(1, runner.recover_interrupted_tasks())

            # 生命周期 2：恢复重放同一 (run,task,attempt) ⇒ 命中首次回执，跳过外部动作
            asyncio.run(runner._run_task("r1", self._row("t1")))

        self.assertEqual(1, counter["n"], "重启恢复重放不得重复对外派发")
        self.assertEqual(1, len(db.query("SELECT * FROM command_receipts")),
                         "同一 command_id 只应落一行回执")
        self.assertIn("跳过", self._row("t1")["error"] or "",
                      "命中回执的任务须落到可诊断终态，不静默挂死")


if __name__ == "__main__":
    unittest.main(verbosity=2)
