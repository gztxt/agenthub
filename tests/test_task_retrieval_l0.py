#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：拆解→检索→验证 的**串联**（2026-10-22 批 B1/B2/B3）。

## 改动前的实测事实（这是这组用例存在的全部理由）

`grep -E "skill|relevant|intake|memory|kb" src/tasks.py` → **0 命中**；
`src/adapters/*.py` 同样 0 命中；生产 `tasks` 表 `COUNT(*)=3, runs=1`（2026-09-06）。
即：「自动拆解」「检索」「验证」三个能力各自都在，但**从来没有串成一条流水线**——
拆出来的子任务直接进 LLM，拿不到任何记忆/文档/技能线索。

## 三条判据

1. **B1 检索块**：只取有动作价值的三类（约束/回读/技能），且**超预算必须显式标注省略**
   （与 `format_upstream` 同一纪律：静默截断会让下游分不清「本来就这么少」与「被砍了」）。
2. **B1 降级**：检索超时/异常 ⇒ 空块 + 原因，子任务照跑（检索是增强，不许带倒任务）。
3. **B3 重试**：只在 `fail && repairable` 且次数未用尽时重试；`insufficient`（判不了）
   **不重试** —— 重跑同一执行者同一判据只会再来一次判不了，那是「无限等待」的同族。

⚠ 其中两条是**源码级断言**（`_run_task` 里确实调了 `_retrieval_for`、确实写 `retrieval_json`）：
真正执行 `_run_task` 需要 DB + chat_fn，属于 L1。源码级断言不是「文字存在即生效」——
它是配合上面那些**真跑**的函数用例用的：函数行为由用例证明，接线由这条钉住。
"""
import asyncio
import inspect
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import tasks      # noqa: E402
import tasks_tiers  # noqa: E402  —— 常量定义处：打桩要打在**读它的模块**上
import verifier   # noqa: E402
import intake     # noqa: E402


def _rep(verdict="ready", coverage="3/4", constraints=(), must_read=(), skill=None,
         skill_strong=False, strong=0):
    return {"plan": {"verdict": verdict, "coverage": coverage, "strong": strong,
                     "skill_strong": skill_strong,
                     "constraints": [{"text": t, "src": "记忆/L1"} for t in constraints],
                     "must_read": [{"path": p, "why": w} for p, w in must_read]},
            "skill": {"ok": bool(skill), "name": skill, "route": "techdocs"}}


class TestRetrievalBlock(unittest.TestCase):
    def test_contains_three_action_kinds(self):
        blk = tasks.retrieval_block(_rep(constraints=["改共享配置先备份"],
                                         must_read=[("wiki/x.md", "判例")],
                                         skill="systematic-debugging", skill_strong=True))
        self.assertIn("检索线索", blk)
        self.assertIn("约束", blk)
        self.assertIn("改共享配置先备份", blk)
        self.assertIn("回读", blk)
        self.assertIn("wiki/x.md", blk)
        self.assertIn("技能", blk)
        self.assertIn("hub_skill_auto", blk)

    def test_header_states_verdict_and_caveat(self):
        blk = tasks.retrieval_block(_rep(constraints=["x"]))
        self.assertIn("ready", blk)
        self.assertIn("3/4", blk)
        self.assertIn("只是线索不是结论", blk, "缺少「线索不是结论」的告诫")

    def test_empty_gives_empty_string_not_empty_header(self):
        """没有可用线索时返回空串 ⇒ 调用方不加块，不制造一个空标题占位。"""
        self.assertEqual(tasks.retrieval_block(_rep()), "")

    def test_skill_miss_produces_no_skill_line(self):
        blk = tasks.retrieval_block(_rep(constraints=["x"]))
        self.assertNotIn("hub_skill_auto", blk)

    def test_truncation_is_marked_and_bounded(self):
        blk = tasks.retrieval_block(_rep(constraints=["约束" * 400, "第二条"], must_read=[]),
                                    limit=200)
        self.assertIn("已省略", blk, "超预算截断没有标记 —— 静默截断")
        # 标记之外的正文字符数不得超出预算太多（标记本身是固定开销）
        self.assertLess(len(blk), 200 + 60)

    def test_short_block_not_marked(self):
        blk = tasks.retrieval_block(_rep(constraints=["短"]), limit=1200)
        self.assertNotIn("已省略", blk)


class TestRetrievalDegrades(unittest.TestCase):
    """检索是增强：失败/超时一律降级，且**必须留下原因**。"""

    def setUp(self):
        self._orig = intake.run

    def tearDown(self):
        intake.run = self._orig

    def test_success(self):
        async def fake_run(inp):
            return _rep(constraints=["改前备份"], skill="x", skill_strong=True)
        intake.run = fake_run
        blk, meta = asyncio.run(tasks._retrieval_for("总目标", "子任务", "codex"))
        self.assertIn("改前备份", blk)
        self.assertTrue(meta["ok"])
        self.assertEqual(meta["verdict"], "ready")
        self.assertTrue(meta["skill_strong"])
        self.assertIn("ms", meta)

    def test_exception_degrades_with_reason(self):
        async def boom(inp):
            raise RuntimeError("memory 500")
        intake.run = boom
        blk, meta = asyncio.run(tasks._retrieval_for("总目标", "子任务", "codex"))
        self.assertEqual(blk, "", "异常时不应产出半截块")
        self.assertFalse(meta["ok"])
        self.assertIn("memory 500", meta["error"])

    def test_timeout_degrades_with_reason(self):
        async def slow(inp):
            await asyncio.sleep(1.0)
            return _rep(constraints=["不该到这一步"])
        intake.run = slow
        old = tasks_tiers.RETRIEVAL_TIMEOUT_S
        tasks_tiers.RETRIEVAL_TIMEOUT_S = 0.01
        try:
            blk, meta = asyncio.run(tasks._retrieval_for("总目标", "子任务", "codex"))
        finally:
            tasks_tiers.RETRIEVAL_TIMEOUT_S = old
        self.assertEqual(blk, "")
        self.assertFalse(meta["ok"])
        self.assertIn("超时", meta["error"])


class TestShouldRetry(unittest.TestCase):
    def test_retries_repairable_failure_once(self):
        self.assertTrue(tasks.should_retry(verifier.STATUS_FAIL, True, 0))

    def test_stops_after_budget(self):
        self.assertFalse(tasks.should_retry(verifier.STATUS_FAIL, True, tasks.MAX_AUTO_RETRY))

    def test_never_retries_insufficient(self):
        """判不了 ≠ 做错了：重跑只会再来一次判不了。"""
        self.assertFalse(tasks.should_retry(verifier.STATUS_INSUFFICIENT, False, 0))
        self.assertFalse(tasks.should_retry(verifier.STATUS_INSUFFICIENT, True, 0))

    def test_never_retries_non_repairable(self):
        self.assertFalse(tasks.should_retry(verifier.STATUS_FAIL, False, 0))

    def test_never_retries_pass(self):
        self.assertFalse(tasks.should_retry(verifier.STATUS_PASS, True, 0))

    def test_garbage_attempts_is_conservative(self):
        """读不出来的 attempts 当作已用完 —— 宁可不重试，也不冒无限循环风险。"""
        self.assertFalse(tasks.should_retry(verifier.STATUS_FAIL, True, "abc"))

    def test_default_budget_is_one(self):
        self.assertEqual(tasks.MAX_AUTO_RETRY, 1, "默认必须是 1：失败即止损")


class TestWiringIsReal(unittest.TestCase):
    """源码级钉住接线（函数行为由上面用例证明，这里证明它**被调用**）。"""

    def test_run_task_calls_retrieval_and_persists_it(self):
        # 2026-10-31 拆 `_run_task`（B4 片3）：检索段搬到 `_attach_retrieval`，
        # 断言随之改成「两段都要在」—— 只改一处会漏掉「谁调用谁」这条。
        run_src = inspect.getsource(tasks._run_task)
        att_src = inspect.getsource(tasks._attach_retrieval)
        self.assertIn("_attach_retrieval(", run_src, "_run_task 不再串检索段")
        self.assertIn("_retrieval_for(", att_src, "_attach_retrieval 没有调用检索")
        self.assertIn("retrieval_json", att_src, "检索结果没有落库 ⇒ 事后无法复盘")
        self.assertIn("TASK_RETRIEVAL", att_src, "缺少开关，出问题无法一键关掉检索")

    def test_run_task_uses_should_retry(self):
        # 2026-10-31 再拆：验证分支搬到 `_verify_and_settle`，断言跟到那里。
        src = inspect.getsource(tasks._verify_and_settle)
        self.assertIn("should_retry(", src)
        self.assertIn("attempts", src)

    def test_schema_has_new_columns(self):
        cols = dict(tasks._TASK_COLUMNS)
        self.assertIn("retrieval_json", cols)
        self.assertIn("attempts", cols)

    def test_retry_does_not_touch_success_path(self):
        """接受路径（AUTO_ACCEPT）不得被重试逻辑污染：`submitted`/`success` 只写一次。"""
        src = inspect.getsource(tasks._verify_and_settle)
        i = src.find("STATUS_PASS")
        self.assertNotEqual(i, -1)
        self.assertNotIn("should_retry", src[i:i + 400])


if __name__ == "__main__":
    unittest.main()
