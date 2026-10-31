"""派发时注入技能说明书（2026-10-31 用户裁定「执行 3」）。

判据来源：B1 只把「技能命中」写成一行提示 `hub_skill_auto(task=...)`，
实测执行者不会去调 —— 子任务直接进 LLM，把该照说明书做的活重新发明一遍。
本闸门钉四件事：

1. 拿到正文 ⇒ 出块，且块里**带技能名与来源**（执行者知道自己在照谁的说明书做）；
2. 没拿到正文（ok 但空）⇒ 给调用式指引，**不冒充内容**；
3. 截断**显式**（写出省略了多少字符），不是默默砍；
4. 注入点在 `_call_chat` **之前**，且失败不阻塞（`_skill_inject_for` 捕获一切异常）。
"""

import asyncio
import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import tasks  # noqa: E402

SRC = (ROOT / "src" / "tasks.py").read_text(encoding="utf-8")
# 片3 拆分后常量与注入实现在 `tasks_tiers`：源码级断言必须扫**两处**
SRC_TIERS = (ROOT / "src" / "tasks_tiers.py").read_text(encoding="utf-8")


class TestBlockShape(unittest.TestCase):
    def test_block_has_name_and_source(self):
        blk = tasks.skill_inject_block({"ok": True, "name": "dispatching-parallel-agents",
                                        "route": "claude", "source": "local",
                                        "content": "1. 先列可并行项\n2. 再派发"})
        self.assertIn("dispatching-parallel-agents", blk)
        self.assertIn("来源 local", blk)
        self.assertIn("先列可并行项", blk)

    def test_ok_without_body_gives_pointer_not_fake_content(self):
        blk = tasks.skill_inject_block({"ok": True, "name": "x", "route": "r",
                                        "source": "store", "content": ""})
        self.assertIn("hub_skill_invoke", blk)
        self.assertNotIn("```", blk)

    def test_not_ok_returns_empty(self):
        self.assertEqual(tasks.skill_inject_block({"ok": False}), "")

    def test_truncation_is_explicit(self):
        big = "A" * 20000
        blk = tasks.skill_inject_block({"ok": True, "name": "n", "route": "r",
                                        "source": "local", "content": big}, limit=2000)
        self.assertLessEqual(len(blk), 2400, "必须按 limit 截断")
        self.assertIn("已省略", blk, "截断必须显式写出省略量（不能默默砍）")


class TestFailOpen(unittest.TestCase):
    def test_exception_does_not_raise(self):
        import skill_store
        old = skill_store.acquire

        async def boom(*a, **k):
            raise RuntimeError("store down")
        skill_store.acquire = boom
        try:
            blk, meta = asyncio.run(tasks._skill_inject_for("目标", "子任务", "claude"))
        finally:
            skill_store.acquire = old
        self.assertEqual(blk, "")
        self.assertFalse(meta["ok"])
        self.assertIn("store down", meta["error"], "失败必须留痕，不许静默当作没有技能")

    def test_timeout_does_not_raise(self):
        import skill_store
        old = skill_store.acquire
        import tasks_tiers
        old_to = tasks_tiers.TASK_SKILL_TIMEOUT_S

        async def slow(*a, **k):
            await asyncio.sleep(5)
            return {"ok": True, "content": "late"}
        skill_store.acquire = slow
        tasks_tiers.TASK_SKILL_TIMEOUT_S = 0.05
        try:
            blk, meta = asyncio.run(tasks._skill_inject_for("目标", "子任务", "claude"))
        finally:
            skill_store.acquire = old
            tasks_tiers.TASK_SKILL_TIMEOUT_S = old_to
        self.assertEqual(blk, "")
        self.assertIn("超时", meta["error"])


class TestWiring(unittest.TestCase):
    def test_injected_before_chat_call(self):
        i_inject = SRC.index("_skill_inject_for(row.get")
        i_chat = SRC.index("_call_chat(chat_fn, exec_agent, prompt")
        self.assertLess(i_inject, i_chat, "注入必须在真正调用执行者之前")

    def test_meta_is_persisted(self):
        self.assertIn('rmeta["skill_inject"] = smeta', SRC)

    def test_can_be_disabled(self):
        self.assertIn("TASK_SKILL_INJECT", SRC_TIERS)
        self.assertIn('os.getenv("TASK_SKILL_INJECT", "1")', SRC_TIERS)
