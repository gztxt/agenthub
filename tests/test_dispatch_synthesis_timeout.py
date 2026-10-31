"""全链路派发的两条结构约束（2026-10-29）：

1. **强制汇总节点**：多叶子 DAG 必须有一个汇总，否则「全绿但没结论」；
2. **超时口径**：adapter 侧必须晚于外层 `wait_for`，否则 600s 闸门是死代码。

两条都用纯函数/静态取证钉住 —— 不依赖起服务、不花钱。
"""
import os
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
SRC = str(_REPO / "src")
sys.path.insert(0, SRC)


def _items(*pairs):
    """pairs: (id, deps) → _validate_dag 归一后的形状。"""
    return [{"id": i, "task": "t-" + i, "agent_id": "claude",
             "deps": list(d), "difficulty": 2, "model": None} for i, d in pairs]


class TestEnsureSynthesis(unittest.TestCase):
    def setUp(self):
        import tasks
        self.t = tasks

    def test_two_leaves_get_sum_node(self):
        items = _items(("t1", []), ("t2", []))
        out, note = self.t.ensure_synthesis(items, "claude")
        self.assertTrue(note["added"])
        self.assertEqual(len(out), 3)
        sum_node = out[-1]
        self.assertEqual(sum_node["id"], "t_sum")
        self.assertEqual(sorted(sum_node["deps"]), ["t1", "t2"])
        self.assertEqual(sum_node["agent_id"], "claude")
        # 叶子判定：加完之后只剩 t_sum 一个叶子
        self.assertEqual(self.t._leaves(out), ["t_sum"])

    def test_single_leaf_not_added(self):
        items = _items(("t1", []), ("t2", ["t1"]))
        out, note = self.t.ensure_synthesis(items, "claude")
        self.assertFalse(note["added"])
        self.assertEqual(note["reason"], "single_leaf")
        self.assertEqual(len(out), 2)

    def test_leader_written_aggregator_needs_no_second_one(self):
        """Leader 自己写聚合时它就是唯一叶子 —— 不能叠出第二个汇总。"""
        items = _items(("t1", []), ("t2", []), ("t3", ["t1", "t2"]))
        out, note = self.t.ensure_synthesis(items, "claude")
        self.assertFalse(note["added"])
        self.assertEqual(note["reason"], "single_leaf")
        self.assertEqual(self.t._leaves(out), ["t3"])
        self.assertEqual(len(out), 3)

    def test_capacity_is_reported_not_silent(self):
        items = _items(*[(("t%d" % i), []) for i in range(1, self.t.MAX_TASKS + 1)])
        out, note = self.t.ensure_synthesis(items, "claude")
        self.assertFalse(note["added"])
        self.assertEqual(note["reason"], "capacity")
        self.assertEqual(len(out), self.t.MAX_TASKS)
        self.assertIn("hint", note)          # 静默跳过就是最坏形态

    def test_id_collision_gets_suffix(self):
        items = _items(("t1", []), ("t2", []), ("t_sum", ["t1"]))
        out, note = self.t.ensure_synthesis(items, "claude")
        self.assertTrue(note["added"])
        self.assertEqual(note["id"], "t_sum2")
        self.assertEqual(sorted(note["deps"]), ["t2", "t_sum"])

    def test_leader_prompt_reserves_a_slot(self):
        """上限告诉 Leader 11 而不是 12：不预留就永远加不进汇总节点。"""
        self.assertEqual(self.t.LEADER_MAXN, self.t.MAX_TASKS - 1)

    def test_both_entrypoints_call_it(self):
        """decompose 与 replan 是两个入口，漏一个就有半边 run 没有汇总。"""
        # 片3 文件级拆分：两个入口分别在 tasks_dag（decompose）与 tasks_replan
        src = ""
        for name in ("tasks_dag.py", "tasks_replan.py"):
            with open(os.path.join(SRC, name), encoding="utf-8") as f:
                src += f.read()
        self.assertEqual(len(re.findall(r"^\s*items, synth = (?:_dag\.)?ensure_synthesis\(", src, re.M)), 2)


class TestChatTimeout(unittest.TestCase):
    def setUp(self):
        import tasks
        self.t = tasks

    def test_accepts_kwarg_detection(self):
        async def with_kw(a, b, timeout_s=None):
            return a
        async def with_star(a, **kw):
            return a
        async def without(a, b):
            return a
        self.assertTrue(self.t._accepts_kwarg(with_kw, "timeout_s"))
        self.assertTrue(self.t._accepts_kwarg(with_star, "timeout_s"))
        self.assertFalse(self.t._accepts_kwarg(without, "timeout_s"))

    def test_timeout_passed_only_when_supported(self):
        seen = {}

        async def fn(agent, prompt, tag, model=None, timeout_s=None):
            seen["t"] = timeout_s
            return {"success": True, "response": "ok"}

        async def legacy(agent, prompt, tag):
            return {"success": True, "response": "ok"}

        import asyncio
        asyncio.run(self.t._call_chat(fn, "claude", "p", "tag", None))
        self.assertEqual(seen["t"], self.t.TASK_TIMEOUT_S + 30)
        # 3 参 lambda 不得被传 kwarg（会 TypeError，且此时钱已花掉）
        asyncio.run(self.t._call_chat(legacy, "claude", "p", "tag", None))

    def test_adapter_uses_caller_timeout(self):
        """adapter 侧硬编码 120 是这条 bug 的根；静态钉住它现在读 kwargs。"""
        p = os.path.join(SRC, "adapters", "openai_compat.py")
        with open(p, encoding="utf-8") as f:
            src = f.read()
        self.assertIn('kwargs.get("timeout_s")', src)
        self.assertIn("ClientTimeout(total=total_s)", src)
        self.assertNotIn("ClientTimeout(total=120)", src)


if __name__ == "__main__":
    unittest.main()
