"""L0：拆解并行度闸门（`src/tasks_dag.py` 的 parallelism_note / LEADER_PROMPT 规则）。

判据（对齐 work/collab-mix-review/SPEC.md 判据 #5）：
  · 任务数 >= 3 且「无 deps 任务数 / 总数」< 0.5 ⇒ 返回 warning dict；
  · 否则返回 None（含任务数 < 3、空表、恰好等于阈值）。
  · LEADER_PROMPT 必须含「互不依赖的子任务必须写 deps: []」这条硬规则。

本文件属 L0 hermetic：**零网络**、不 import `src.main`、不写 `data/*.db`。
实证样本来自 run e0f7de9912（10 个任务串成单链，1 次超时致 8 个 blocked）。
"""
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

import tasks_dag  # noqa: E402

parallelism_note = tasks_dag.parallelism_note


def _t(deps):
    return {"id": "x", "task": "t", "agent_id": "claude-code", "deps": deps}


class TestParallelismNote(unittest.TestCase):
    def test_all_parallel_is_clean(self):
        items = [_t([]), _t([]), _t([]), _t([])]
        self.assertIsNone(parallelism_note(items))

    def test_single_chain_warns(self):
        # 复刻 e0f7de9912：t1 无依赖，其余全部串在前一个后面
        items = [_t([])] + [_t(["prev"]) for _ in range(9)]
        note = parallelism_note(items)
        self.assertIsNotNone(note)
        self.assertEqual(note["total"], 10)
        self.assertEqual(note["free"], 1)
        self.assertAlmostEqual(note["ratio"], 0.1)
        self.assertIn("并行度偏低", note["warning"])
        self.assertIn("1/10", note["warning"])

    def test_threshold_boundary_equal_is_clean(self):
        # 4 个任务 2 个无依赖 = 0.5，恰好达到阈值 ⇒ 不告警
        items = [_t([]), _t([]), _t(["t2"]), _t(["t1"])]
        self.assertIsNone(parallelism_note(items))

    def test_threshold_boundary_below_warns(self):
        # 3 个任务 1 个无依赖 = 0.333 < 0.5 ⇒ 告警
        items = [_t([]), _t(["t1"]), _t(["t2"])]
        note = parallelism_note(items)
        self.assertIsNotNone(note)
        self.assertEqual(note["free"], 1)
        self.assertEqual(note["total"], 3)

    def test_too_few_tasks_not_judged(self):
        # 任务太少不做判断（1~2 个任务谈不上并行度）
        for n in (1, 2):
            items = [_t([])] + [_t(["t1"]) for _ in range(n - 1)]
            self.assertIsNone(parallelism_note(items), msg=f"n={n}")

    def test_empty_and_none(self):
        self.assertIsNone(parallelism_note([]))
        self.assertIsNone(parallelism_note(None))

    def test_missing_deps_key_counts_as_free(self):
        # deps 缺省 = 无依赖，与 tasks_dag 落库口径一致（None/[] 都是不含依赖）
        items = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
        self.assertIsNone(parallelism_note(items))

    def test_deps_key_none_counts_as_free(self):
        items = [_t(None), _t(None), _t(None)]
        self.assertIsNone(parallelism_note(items))

    def test_warning_shape_is_json_safe(self):
        items = [_t([]), _t(["t1"]), _t(["t2"]), _t(["t3"])]
        note = parallelism_note(items)
        self.assertIsNotNone(note)
        self.assertEqual(set(note), {"warning", "ratio", "free", "total"})
        self.assertIsInstance(note["warning"], str)
        self.assertIsInstance(note["ratio"], float)
        self.assertIsInstance(note["free"], int)
        self.assertIsInstance(note["total"], int)


class TestLeaderPromptRule(unittest.TestCase):
    def test_prompt_has_parallel_rule(self):
        self.assertIn("互不依赖的子任务必须写 `deps: []`", tasks_dag.LEADER_PROMPT)

    def test_prompt_keeps_old_rules(self):
        # 不许在加规则时改坏既有占位符与既有规则
        for token in ("{agents}", "{default}", "{models}", "{maxn}",
                      "聚合/汇总类任务必须 deps 列出全部上游"):
            self.assertIn(token, tasks_dag.LEADER_PROMPT)


class TestConstants(unittest.TestCase):
    def test_thresholds(self):
        self.assertEqual(tasks_dag.PARALLEL_RATIO_MIN, 0.5)
        self.assertEqual(tasks_dag.PARALLEL_MIN_TASKS, 3)


if __name__ == "__main__":
    unittest.main()