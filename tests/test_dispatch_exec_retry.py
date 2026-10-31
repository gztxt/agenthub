"""执行者自身失败 → 升档重试的 L0 闸门（v0.13.177）。

背景（2026-10-29 实弹）：dsh 连吃两次 `RATE_LIMIT 429`，`_execute_task` 的
「自称失败」分支**直接写 failed**，绕过了 verifier 失败分支的 `should_retry`
⇒ t2/t3 一次都不重试，t4 因上游 failed 变 blocked，汇总永远跑不到。

这里钉三条：
  ① 有升档路径 ⇒ 排一次重试（status=pending、attempts+1、reason 写明升到谁）；
  ② 没有升档路径 ⇒ **不重试**（同 agent 同模型原地再赌一次是纯浪费）；
  ③ attempts 已用满 ⇒ 不重试（有界，防失败→重跑→再失败无限循环）。
"""
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import tasks  # noqa: E402


class TestExecFailureRetry(unittest.TestCase):
    def setUp(self):
        tasks.set_context(agent_ids_fn=lambda: {"dsh", "claude-code", "codex"})
        self.row = {"task_id": "t2", "agent_id": "dsh", "trace_id": "tr1", "tier": None}

    def _run(self, attempts, known=None):
        if known is not None:
            tasks.set_context(agent_ids_fn=lambda: known)
        with mock.patch.object(tasks.db, "execute") as ex, \
                mock.patch.object(tasks.db, "log_profile_event"):
            ok = tasks._schedule_exec_retry(
                "run1", self.row, "dsh: RATE_LIMIT 429", "executor_claimed_failure",
                attempts, "dsh", None, dur=1234, kind="nonempty")
        return ok, ex

    def test_escalates_when_path_exists(self):
        ok, ex = self._run(0)
        self.assertTrue(ok, "有升档路径却判定不重试 ⇒ 429 会被记成终态")
        sql, params = ex.call_args[0][0], ex.call_args[0][1]
        self.assertIn("status='pending'", sql)
        self.assertEqual(params[0], 1, "attempts 必须是 1（0 会被当成首次执行）")
        self.assertIn("升档 dsh→", params[1])
        self.assertIn("claude-code", params[1])

    def test_no_path_does_not_retry(self):
        ok, ex = self._run(0, known={"dsh"})
        self.assertFalse(ok, "池里只有自己时不许重试")
        ex.assert_not_called()

    def test_exhausted_does_not_retry(self):
        ok, ex = self._run(tasks.MAX_AUTO_RETRY)
        self.assertFalse(ok, "attempts 用满后必须停")
        ex.assert_not_called()

    def test_all_escalate_calls_pass_catalog(self):
        """每个 `escalate_target(` 调用点都必须传 `catalog=`。

        2026-10-29 实弹：三处调用全没传 ⇒ `first_usable()` 恒 None ⇒
        **档位升档路径运行时永不生效**，claude-code 超时后换成了 codex 而不是升到 t2。
        单测发现不了，因为单测自己传了 catalog —— 只能对源码做形状断言。
        """
        # 片3 文件级拆分后调用点散在 tiers/runner：只扫 tasks.py 会漏（假绿）
        src = "".join((_REPO / "src" / n).read_text(encoding="utf-8")
                      for n in ("tasks.py", "tasks_tiers.py", "tasks_runner.py",
                                "tasks_dag.py", "tasks_replan.py"))
        calls, i = [], 0
        while True:
            i = src.find("escalate_target(", i)
            if i < 0:
                break
            j, depth = i + len("escalate_target("), 1
            while j < len(src) and depth:
                depth += {"(": 1, ")": -1}.get(src[j], 0)
                j += 1
            blk = src[i:j]
            if not src[max(0, i - 20):i].rstrip().endswith("def"):
                calls.append(blk)
            i = j
        self.assertGreaterEqual(len(calls), 3, "调用点少于 3 处说明扫描口径变了")
        for blk in calls:
            self.assertIn("catalog", blk,
                          "调用点缺 catalog（档位升档会静默失效）：%s" % blk[:90])


if __name__ == "__main__":
    unittest.main()
