#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：CHAIN_JUDGE=shadow 的两处接线（tasks_runner 留证 / refscout 不改序）。

【这条判据治什么】
影子档的承诺是「判定照算、证据照记、处置零影响」（借 TokenEconomizer shadow，
与 `mcpgw` ACL `effect=shadow` 同一纪律）。纯函数已在 `test_chain_judge_l0` 钉死；
本文件钉**接线**——承诺要穿过真实调用链才算数：

1. tasks_runner：shadow 档 off_topic 不拦（status=success 照走），但
   `profile_events` 出现 `chain_judge_shadow/would_block`（零影响 + 必留证）；
2. tasks_runner：enforce 档同输入照拦（submitted + verify_reason 写明 off_topic，
   旧行为不回归），且没有 `chain_judge_shadow` 事件；
3. 默认（CHAIN_JUDGE 缺省 off）：判定 skipped、无任何影子事件（接入前行为）；
4. refscout_collect：shadow 档**不得**调 `promote_within_bucket`（不改序）——
   源级钉死 `chain_judge.SHADOW` 守卫在改序调用之前。

分层口径：临时 db + 假 chat_fn + 打桩 `chain_judge.classify_output`（零网络）、
`_start_runner` 置桩，与 `test_task_accept_l0` 同款。
"""
import asyncio
import json
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import chain_judge             # noqa: E402
import db                      # noqa: E402
import tasks                   # noqa: E402
import tasks_tiers as tiers    # noqa: E402
import tasks_runner as runner  # noqa: E402

_RESCOUT_SRC = (_REPO / "src" / "refscout_collect.py").read_text(encoding="utf-8")

_OFF_TOPIC = {"ok": True, "label": "off_topic", "evidence": None,
              "ms": 7, "model": "stub", "why": "", "skipped": False}
_COMPLETED = {"ok": True, "label": "completed", "evidence": "real answer",
              "ms": 5, "model": "stub", "why": "", "skipped": False}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0shdw-"))
        self._saved_conn = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self._saved_conc = tiers.MAX_CONCURRENCY
        self._saved_sleep = tiers.LOOP_SLEEP_S
        tiers.MAX_CONCURRENCY = 1
        tiers.LOOP_SLEEP_S = 0.01
        self.started = []
        self._saved_start = runner._start_runner
        runner._start_runner = lambda rid: self.started.append(rid)
        self._saved_cls = chain_judge.classify_output
        self._saved_shadow = chain_judge.SHADOW
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved_conn
        tiers.MAX_CONCURRENCY = self._saved_conc
        tiers.LOOP_SLEEP_S = self._saved_sleep
        runner._start_runner = self._saved_start
        chain_judge.classify_output = self._saved_cls
        chain_judge.SHADOW = self._saved_shadow
        tasks.set_context(chat_fn=None)

    def _insert(self, run_id, tid):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending','nonempty',?,?)",
            (run_id, tid, run_id, "g", "do " + tid, "claude",
             tasks.json.dumps([]), now, now))

    def _row(self, tid):
        return db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]

    def _set_chat(self):
        async def chat(agent_id, message, trace_id=None):
            return {"success": True, "response": "real answer " * 10}
        tasks.set_context(chat_fn=chat)

    def _run_task_once(self, tid):
        row = self._row(tid)
        asyncio.run(tasks._run_task(row["run_id"], row))

    def _judge_events(self):
        return db.query("SELECT * FROM profile_events WHERE source='chain_judge_shadow'")


class TestShadowWiring(_Base):
    def test_shadow_off_topic_still_success_and_records_would_block(self):
        chain_judge.SHADOW = True
        chain_judge.classify_output = lambda task, out, **kw: dict(_OFF_TOPIC)
        self._insert("r1", "t1")
        self._set_chat()
        self._run_task_once("t1")
        row = self._row("t1")
        self.assertEqual(row["status"], "success", "影子档 off_topic 不得拦（零影响）")
        self.assertEqual("auto", row["accepted_by"])
        ev = self._judge_events()
        self.assertEqual(1, len(ev), "影子必须留证")
        self.assertEqual("would_block", ev[0]["status"])
        d = json.loads(ev[0]["detail"])
        self.assertEqual("t1", d["task"])
        self.assertEqual("off_topic", d["judge"])

    def test_shadow_completed_records_no_block(self):
        chain_judge.SHADOW = True
        chain_judge.classify_output = lambda task, out, **kw: dict(_COMPLETED)
        self._insert("r2", "t1")
        self._set_chat()
        self._run_task_once("t1")
        self.assertEqual("success", self._row("t1")["status"])
        ev = self._judge_events()
        self.assertEqual(1, len(ev))
        self.assertEqual("no_block", ev[0]["status"])

    def test_enforce_off_topic_blocks_as_before(self):
        chain_judge.SHADOW = False
        chain_judge.classify_output = lambda task, out, **kw: dict(_OFF_TOPIC)
        self._insert("r3", "t1")
        self._set_chat()
        self._run_task_once("t1")
        row = self._row("t1")
        self.assertEqual("submitted", row["status"], "enforce 档照拦（旧行为不回归）")
        self.assertIn("off_topic", row["verify_reason"] or "",
                      "复核的人必须能看出「不是 verifier 失败」")
        self.assertEqual([], self._judge_events(), "enforce 档没有影子事件")

    def test_default_off_no_shadow_events(self):
        # 不打桩 classify_output：模块默认 off ⇒ 判定 skipped ⇒ 完全旧行为
        self._insert("r4", "t1")
        self._set_chat()
        self._run_task_once("t1")
        self.assertEqual("success", self._row("t1")["status"])
        self.assertEqual([], self._judge_events())


class TestRefscoutShadowPinned(unittest.TestCase):
    def test_promote_guarded_by_shadow_flag(self):
        i = _RESCOUT_SRC.index
        self.assertIn("chain_judge.SHADOW", _RESCOUT_SRC,
                      "refscout 没有 shadow 守卫 ⇒ 影子档仍会改排序")
        self.assertLess(i("chain_judge.SHADOW"), i("promote_within_bucket(ranked"),
                        "影子守卫必须在改序之前，否则 shadow 档仍会动排序")


if __name__ == "__main__":
    unittest.main()
