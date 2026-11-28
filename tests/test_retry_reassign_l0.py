"""「重新执行」必须换 agent/模型（2026-11-28 用户令）。

用户原话：「主任务和子任务都添加重新执行的按钮 … 但是不一定要使用之前设定的 agent
和模型 … 因为之前执行失败了，肯定是 agent 或者模型有问题」。

判据分两层：
1. 纯函数 `tasks_tiers.reassign_for_retry()`：换了就说清换了什么，换不了就说清为什么
   （`no_reassign_path`），**绝不假装换过**。
2. 接口层 `POST /api/tasks/{run}/retry` 的 `reassign` 开关：缺省 False 逐字不变（旧调用方
   不受影响），True 时把 agent/model/tier/attempts/models_tried 一起落库并回传对照面。
"""
import inspect
import unittest
from unittest import mock

import tasks_tiers as t

ROW = {"agent_id": "claude-code", "model": "workbuddy/glm-5.3-flash",
       "tier": "t2", "attempts": 1, "models_tried": '["workbuddy/glm-5.3-flash"]'}


TIERS = {"t1": ["workbuddy/hy4flash-free"], "t2": ["workbuddy/glm-5.3-flash"],
         "t3": ["workbuddy/glm-5.4", "workbuddy/kimi-k3.1-pro"]}


class TestReassignPure(unittest.TestCase):
    """纯函数层：不依赖线上清单（否则换个进程环境就红——本文件第一版就这么假红过）。"""

    def setUp(self):
        self._p = [mock.patch.object(t, "_catalog_snapshot",
                                     lambda: ({"workbuddy/glm-5.4", "workbuddy/kimi-k3.1-pro",
                                               "workbuddy/glm-5.3-flash"}, True)),
                   mock.patch.object(t, "_unhealthy_providers", lambda: set()),
                   mock.patch.object(t, "_ccr_model_agents", lambda: {"claude-code", "codex", "pi"}),
                   mock.patch.object(t, "_known_agent_ids", lambda: {"claude-code", "codex", "pi"}),
                   mock.patch("tiercfg.parse_tiers", lambda *a, **k: TIERS),
                   mock.patch("tiercfg.parse_mapping", lambda *a, **k: {1: "t1", 2: "t2", 3: "t3"})]
        for x in self._p:
            x.start()

    def tearDown(self):
        for x in reversed(self._p):
            x.stop()

    def test_moves_up_a_tier_without_reusing_old_model(self):
        out = t.reassign_for_retry(dict(ROW))
        self.assertTrue(out["changed"])
        self.assertNotEqual(out["model"], ROW["model"], "不许沿用上次失败的模型")
        self.assertEqual(out["attempts"], 2, "attempts 必须 +1，否则升档判据会被算成首轮")
        self.assertTrue(out["reason"].startswith("tier:"), out["reason"])

    def test_keeps_agent_when_only_model_changes(self):
        out = t.reassign_for_retry(dict(ROW))
        self.assertEqual(out["agent_id"], ROW["agent_id"])

    def test_switches_agent_when_current_is_unavailable(self):
        with mock.patch.object(t, "_known_agent_ids", lambda: {"codex", "pi"}):
            out = t.reassign_for_retry(dict(ROW, tier="t3"))
        self.assertTrue(out["changed"])
        self.assertIn(out["agent_id"], {"codex", "pi"})

    def test_no_path_is_reported_honestly(self):
        with mock.patch.object(t, "_known_agent_ids", lambda: {"claude-code"}), \
                mock.patch.object(t, "_csv_env", lambda name, default="": ""), \
                mock.patch.object(t, "escalate_target",
                                  lambda *a, **k: {"changed": False, "agent_id": "claude-code",
                                                   "model": a[1], "reason": "no_escalation_path"}), \
                mock.patch.object(t, "_catalog_snapshot", lambda: (set(), False)), \
                mock.patch.object(t, "_ccr_model_agents", lambda: set()):
            out = t.reassign_for_retry({"agent_id": "claude-code", "model": "x/1",
                                        "tier": "explicit", "attempts": 1,
                                        "models_tried": "[]"})
        self.assertFalse(out["changed"])
        self.assertIn("no_reassign_path", out["reason"])


class TestRetryEndpoint(unittest.TestCase):
    def test_default_is_old_behavior(self):
        import tasks
        sig = inspect.signature(tasks.RetryIn)
        self.assertEqual(sig.parameters["reassign"].default, False,
                         "缺省必须 False：MCP/脚本的旧调用逐字不变")

    def test_reassign_writes_agent_model_attempts(self):
        import tasks
        src = inspect.getsource(tasks.retry_run)
        # 执行者读 **model_planned**；写成 model= 等于「只在展示列上换了模型」
        for k in ("reassign_for_retry", "agent_id=?", "model_planned=?", "attempts=?",
                  "models_tried=?", "escalated_from_agent=?"):
            self.assertIn(k, src, "缺少 %s ⇒ 要么没换，要么换了没记账" % k)

    def test_ui_has_buttons_on_both_levels(self):
        from pathlib import Path
        root = Path(__file__).resolve().parents[1]
        js = (root / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
        self.assertIn('data-run-act="retry"', js, "主任务行缺重新执行按钮")
        self.assertIn("data-task-retry", js, "子任务行缺重新执行按钮")
        self.assertIn("retryRun(pair[0], pair[1] || '', true)", js,
                      "子任务重跑必须带 reassign=true")


if __name__ == "__main__":
    unittest.main()
