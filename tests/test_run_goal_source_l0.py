"""协同历史「主任务」的口径闸门（2026-11-28，用户第三次反馈显示不对）。

用户的定义很明确：**主任务 = 他在目标框手动输入的那条**；子任务 = 拆解后的。
所以这里钉的不是"渲染长得对"，而是**取数口径**：

1. 主任务文本的权威源 = root run 的 `task_runs.goal`（创建时原样落库）；
   `tasks.goal` 只是同一次写入的副本，**老数据缺 task_runs 行时**才回退。
2. 全仓**没有任何 UPDATE 改写 goal** —— 一旦有人加，主任务就再也不是用户输入的那条。
3. 前端行头只能取 `r.goal`（会话级 goal）+ 来源标注；不得用 `tasks[].prompt` 当标题。
4. 未记录时写「(目标未记录)」，不写「(无目标)」—— 后者会被读成"用户没填"。
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TASKS_PY = (ROOT / "src" / "tasks.py").read_text(encoding="utf-8")
S04 = (ROOT / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
HTML = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


class TestGoalSource(unittest.TestCase):
    def test_root_run_goal_is_authoritative(self):
        i = TASKS_PY.index("_CHAIN_SEL_SQL = ")
        seg = TASKS_PY[i:i + 1100]
        self.assertIn("COALESCE(NULLIF(r0.run_goal,''), p.goal) AS goal", seg,
                      "主任务必须优先取 root run 的 task_runs.goal（run_goal 来自 task_runs）")
        self.assertIn("MAX(r.goal) run_goal", TASKS_PY,
                      "task_runs.goal 必须真的进了聚合投影，否则又退回 tasks 副本")
        self.assertIn("MAX(r.origin) run_origin", TASKS_PY)
        self.assertIn("goal_source", seg, "来源必须可读，否则下次又只能猜")
        self.assertIn("goal_origin", seg, "重拆轮的 origin 必须带出去")

    def test_nothing_updates_goal(self):
        """写作路径只有 INSERT：`_create_run` 与 replan 的 task_runs 落库。"""
        bad = re.findall(r"UPDATE\s+tasks\s+SET[^\"']*?\bgoal\s*=", TASKS_PY, re.I)
        self.assertEqual(bad, [], "有代码在 UPDATE goal ⇒ 主任务会被改写，必须改成不可变")
        # 两处 INSERT 必须都在
        dag = (ROOT / "src" / "tasks_dag.py").read_text(encoding="utf-8")
        self.assertIn("INSERT INTO task_runs", dag)
        self.assertIn('(run_id, it["id"], run_id, goal,', dag,
                      "_create_run 必须把 run goal 原样写进 tasks.goal")

    def test_ui_uses_session_goal_not_task_prompt(self):
        self.assertIn("var goalTxt = r.goal || '';", S04)
        self.assertIn("(目标未记录)", S04, "未记录与'用户没填'必须分开写")
        self.assertIn("重拆目标", S04)
        self.assertNotIn("r.tasks[0]", S04)
        self.assertIn(".run-goalnote", HTML)



class TestProjectionColumns(unittest.TestCase):
    """`_CHAIN_SEL_SQL` 里 `r0.<col>` 引用的列必须在聚合投影里真的出现。

    2026-11-28 实踩：给 `_CHAIN_SEL_SQL` 加了 `r0.origin`，但 `_RUN_AGG_SQL` 没投影
    origin ⇒ 线上 `/api/tasks/runs` 直接 500（`no such column: r0.origin`）。
    SQL 的列错误只在运行时报，所以要有个静态闸门：`st` = `a.*`，`r0` 能用什么
    完全取决于 `a` 投影了什么，于是「聚合段文本里有没有这个词」就是充分判据。
    """

    def test_r0_columns_appear_in_aggregate_projection(self):
        agg = TASKS_PY[TASKS_PY.index("_RUN_AGG_SQL = "):TASKS_PY.index("_RUN_STATE_SQL")]
        sel = TASKS_PY[TASKS_PY.index("_CHAIN_SEL_SQL = "):]
        sel = sel[:sel.index("\n\"\"\"") + 1]
        used = sorted(set(re.findall(r"r0\.([A-Za-z_][A-Za-z0-9_]*)", sel)))
        self.assertIn("run_goal", used, "主任务必须走 task_runs.goal 的投影")
        missing = [c for c in used if not re.search(r"\b%s\b" % c, agg)]
        self.assertEqual(missing, [],
                         "这些 r0.* 列没进聚合投影 ⇒ /api/tasks/runs 会 500：%s" % missing)


if __name__ == "__main__":
    unittest.main()
