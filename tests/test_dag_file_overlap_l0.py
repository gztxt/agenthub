"""L0：拆解「文件重叠 ⇒ 强制串行」闸门（`src/tasks_dag.py` 的 serialize_file_overlaps）。

判据来源（2026-11-03 实测）：同一条 UI 需求被拆成 4 个子任务并行派给 4 家 agent，
全部落在 `templates/index.html` + `static/hub/04-terminal-ws.js` 上，后写覆盖先写，
落盘结果是「同一份代码里并存两套实现，改一处另一处照旧生效」。
拆解层当时没有能表达「这两个任务会碰同一份文件」的字段，所以连告警都给不出来。

本闸门判据：
  · 无依赖的兄弟任务 `files` 相交 ⇒ 后者自动补 dep（串行），note.count 记数；
  · 已声明依赖（含传递）⇒ 不重复加边，不制造冗余等待；
  · 边只从后指向前 ⇒ 加边不可能造环（调用前后都断言无环）；
  · 非重叠任务保持并行（不许为了省事把整条 DAG 串成链）；
  · `files` 坏值（字符串/None/非列表/超长/重复）一律归一，不倒灌主流程；
  · LEADER_PROMPT 必须把 `files` 写成必填字段，否则闸门永远拿到空集。

L0 hermetic：零网络、不 import `src.main`、不写 `data/*.db`。
"""
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

import tasks_dag  # noqa: E402

serialize = tasks_dag.serialize_file_overlaps
norm = tasks_dag._norm_files


def _t(tid, files, deps=None, agent="claude-code"):
    return {"id": tid, "task": "t", "agent_id": agent, "deps": list(deps or []),
            "files": list(files)}


def _acyclic(items):
    """独立复算一遍无环性（不信任被测函数的内部判断）。"""
    indeg = {it["id"]: len(it["deps"]) for it in items}
    kids = {it["id"]: [] for it in items}
    for it in items:
        for d in it["deps"]:
            kids.setdefault(d, []).append(it["id"])
    q = [k for k, v in indeg.items() if v == 0]
    seen = 0
    while q:
        cur = q.pop()
        seen += 1
        for c in kids.get(cur, []):
            indeg[c] -= 1
            if indeg[c] == 0:
                q.append(c)
    return seen == len(items)


class TestSerializeFileOverlaps(unittest.TestCase):
    def test_no_overlap_keeps_parallel(self):
        items = [_t("t1", ["a.py"]), _t("t2", ["b.py"]), _t("t3", [])]
        out, note = serialize(items)
        self.assertIsNone(note)
        self.assertEqual([x["deps"] for x in out], [[], [], []])
        self.assertTrue(_acyclic(out))

    def test_same_file_becomes_serial(self):
        # 复刻事故：两个任务都改同一模板
        items = [_t("t1", ["templates/index.html"]), _t("t2", ["templates/index.html"])]
        out, note = serialize(items)
        self.assertIsNotNone(note)
        self.assertEqual(note["kind"], "file_overlap_serialized")
        self.assertEqual(note["count"], 1)
        self.assertEqual(out[1]["deps"], ["t1"])
        self.assertIn("串行", note["warning"])
        self.assertTrue(_acyclic(out))

    def test_shared_file_among_three_chains_up(self):
        items = [_t("t1", ["x.js"]), _t("t2", ["x.js"]), _t("t3", ["x.js"])]
        out, note = serialize(items)
        self.assertEqual(note["count"], 2)
        self.assertEqual(out[1]["deps"], ["t1"])
        self.assertEqual(out[2]["deps"], ["t2"])
        self.assertTrue(_acyclic(out))

    def test_declared_dep_not_duplicated(self):
        items = [_t("t1", ["x.py"]), _t("t2", ["x.py"], deps=["t1"])]
        out, note = serialize(items)
        self.assertIsNone(note)
        self.assertEqual(out[1]["deps"], ["t1"])

    def test_transitive_dep_counts_as_covered(self):
        # t3 经 t2 已传递依赖 t1 ⇒ 不需要再加「等 t1」
        items = [_t("t1", ["x.py"]), _t("t2", ["y.py"], deps=["t1"]),
                 _t("t3", ["x.py"], deps=["t2"])]
        out, note = serialize(items)
        self.assertIsNone(note)
        self.assertEqual(out[2]["deps"], ["t2"])
        self.assertTrue(_acyclic(out))

    def test_partial_overlap_only_touches_that_pair(self):
        items = [_t("t1", ["a.py", "b.py"]), _t("t2", ["c.py"]),
                 _t("t3", ["b.py"]), _t("t4", ["d.py"])]
        out, note = serialize(items)
        self.assertEqual(note["count"], 1)
        self.assertEqual(out[2]["deps"], ["t1"])
        self.assertEqual(out[1]["deps"], [])
        self.assertEqual(out[3]["deps"], [])

    def test_cross_agent_still_serialized(self):
        # 换 agent 不解决覆盖问题：不同 agent 改同一文件同样丢改动
        items = [_t("t1", ["h.js"], agent="claude-code"),
                 _t("t2", ["h.js"], agent="codex")]
        out, note = serialize(items)
        self.assertEqual(note["count"], 1)
        self.assertEqual(out[1]["deps"], ["t1"])

    def test_empty_files_never_touched(self):
        # 纯推理/检索类声明 []：不参与重叠判断，不能因此被串起来
        items = [_t("t1", []), _t("t2", []), _t("t3", [])]
        out, note = serialize(items)
        self.assertIsNone(note)
        self.assertEqual([x["deps"] for x in out], [[], [], []])

    def test_note_is_bounded(self):
        # note 进的是 HTTP 回包与 profile_event：不许把 12 个任务的 pairwise 全塞进去
        items = [_t("t%d" % i, ["same.py"]) for i in range(1, 13)]
        out, note = serialize(items)
        self.assertEqual(note["count"], 11)
        self.assertLessEqual(len(note["pairs"]), 6)
        self.assertTrue(_acyclic(out))


class TestFileDrivenDeps(unittest.TestCase):
    """区分「刻意串行」与「退化单链」：判据是「这条依赖为什么存在」。"""

    def test_leader_serialized_by_files_is_recognized(self):
        # 实测 dry_run 原样：三者同为 templates/index.html，Leader 自己写成链
        items = [_t("t1", ["templates/index.html"]),
                 _t("t2", ["templates/index.html"], deps=["t1"]),
                 _t("t3", ["templates/index.html"], deps=["t2"])]
        driven = tasks_dag.file_driven_deps(items)
        self.assertEqual(len(driven), 2)
        self.assertEqual(driven[0][:2], ("t2", "t1"))

    def test_dep_without_file_overlap_is_not_driven(self):
        items = [_t("t1", ["a.py"]), _t("t2", ["b.py"], deps=["t1"])]
        self.assertEqual(tasks_dag.file_driven_deps(items), [])

    def test_task_without_files_never_driven(self):
        items = [_t("t1", ["a.py"]), _t("t2", [], deps=["t1"])]
        self.assertEqual(tasks_dag.file_driven_deps(items), [])

    def test_partial_chain_reports_only_real_part(self):
        items = [_t("t1", ["a.py"]), _t("t2", ["a.py"], deps=["t1"]),
                 _t("t3", ["b.py"], deps=["t2"])]
        self.assertEqual(len(tasks_dag.file_driven_deps(items)), 1)


class TestNormFiles(unittest.TestCase):
    def test_string_becomes_single_item(self):
        self.assertEqual(norm("src/a.py"), ["src/a.py"])

    def test_dot_slash_and_backslash_normalized(self):
        self.assertEqual(norm(["./src/a.py", "src\\b.py"]), ["src/a.py", "src/b.py"])

    def test_dedup_and_cap(self):
        got = norm(["f%d.py" % i for i in range(12)] + ["f0.py"])
        self.assertEqual(len(got), tasks_dag.FILE_LIST_MAX)
        self.assertEqual(got[0], "f0.py")

    def test_garbage_dropped(self):
        self.assertEqual(norm([None, "", "  ", 3]), ["3"])
        self.assertEqual(norm(None), [])
        self.assertEqual(norm({"a": 1}), [])

    def test_long_path_dropped(self):
        self.assertEqual(norm(["x" * 300]), [])


class TestLeaderPromptContract(unittest.TestCase):
    def test_files_is_required_field(self):
        self.assertIn('"files"', tasks_dag.LEADER_PROMPT)

    def test_overlap_rule_present(self):
        """规则必须成文：只说「加了字段」不足以让 Leader 写出可判的 files。"""
        self.assertIn("文件重叠", tasks_dag.LEADER_PROMPT)
        self.assertIn("deps: []", tasks_dag.LEADER_PROMPT)

    def test_validate_dag_keeps_files(self):
        items = tasks_dag._validate_dag(
            [{"id": "t1", "task": "x", "agent_id": "claude-code", "files": ["./a.py", "a.py"]}],
            {"claude-code"}, "claude-code", None)
        self.assertEqual(items[0]["files"], ["a.py"])

    def test_validate_dag_tolerates_missing_files(self):
        items = tasks_dag._validate_dag(
            [{"id": "t1", "task": "x", "agent_id": "claude-code"}],
            {"claude-code"}, "claude-code", None)
        self.assertEqual(items[0]["files"], [])


if __name__ == "__main__":
    unittest.main()