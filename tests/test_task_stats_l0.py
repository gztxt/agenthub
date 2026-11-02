#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：协同页四框改造（2026-11-03）的判据闸门。

守七条（每条都对应一条可能被「顺手简化」掉的判据）：
1. **轮次夹取**：`clamp_rounds` 把 50 夹到 `ROUNDS_CEILING`，读不出来回落默认值（不是 0）。
2. **老调用方零变化**：`MAX_ROUNDS` 仍是 2；只有显式 `rounds`/落库值才走到 5。
3. **钉模型只钉得住 CCR 域**：codebuddy 这类要被跳过并**点名**，不能静默降级。
4. **清单取得且 ID 不在册 ⇒ 400**：静默降级会让用户把「没生效」读成「模型太弱」。
5. **失败归因不是垃圾桶**：`error_bucket` 至少把 模型/限流/超时/预算/上游传导 分开。
6. **历史能筛**：`/api/tasks/runs` 的 q/state/agent 过滤 + `total` 同口径（不是只筛前 20 条）。
7. **汇总带样本量**：样本 <10 时 `verdict` 必须说「样本不足」，不许只给百分比。

分层口径（tests/tiers.py）：临时 db + 直接调 async 端点函数，不起服务、不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import os  # noqa: E402

os.environ["HUB_MODEL_CATALOG"] = "off"
os.environ.setdefault("HUB_TEST_MODE", "1")

import db                 # noqa: E402
import tasks              # noqa: E402
import tasks_dag as dag   # noqa: E402
import tasks_tiers as tiers  # noqa: E402
from fastapi import HTTPException  # noqa: E402


class TestClampRounds(unittest.TestCase):
    def test_clamps_to_ceiling(self):
        self.assertEqual(tiers.clamp_rounds(50), tiers.ROUNDS_CEILING)
        self.assertEqual(tiers.clamp_rounds(0), 1)
        self.assertEqual(tiers.clamp_rounds(-3), 1)

    def test_garbage_falls_back_to_default(self):
        self.assertEqual(tiers.clamp_rounds("abc"), 1)
        self.assertEqual(tiers.clamp_rounds(None), 1)
        self.assertEqual(tiers.clamp_rounds(None, default=2), 2)

    def test_choices_are_within_ceiling(self):
        for n in tiers.ROUND_CHOICES:
            self.assertEqual(tiers.clamp_rounds(n), n,
                             "界面下拉的档位必须原样可落库，否则选项就是假的")


class TestErrorBucket(unittest.TestCase):
    def test_model_not_registered(self):
        self.assertEqual(tiers.error_bucket(
            'exit:1: [claude-code:unrecognized_model] {"model":"deepseek/deepseek-flash"}'),
            "模型不在册")

    def test_rate_limit(self):
        self.assertEqual(tiers.error_bucket("HTTP 429 Too Many Requests"), "限流/配额")

    def test_budget_and_blocked(self):
        self.assertEqual(tiers.error_bucket("run 预算耗尽: tokens"), "预算耗尽")
        self.assertEqual(tiers.error_bucket("上游失败"), "上游失败(传导)")

    def test_unmatched_is_not_a_dumping_ground(self):
        # 没命中规则时必须留下可分辨的信息，而不是统一叫「其他」
        self.assertNotEqual(tiers.error_bucket("exit:127"), "其他")
        self.assertEqual(tiers.error_bucket("exit:127"), "exit:127")
        self.assertEqual(tiers.error_bucket(""), "无错误信息")


class TestPinnedModel(unittest.TestCase):
    ITEMS = [
        {"id": "t1", "agent_id": "claude-code", "model": None},
        {"id": "t2", "agent_id": "codebuddy", "model": None},
    ]

    def test_empty_model_keeps_auto_tiering(self):
        items, note = dag._apply_pinned_model([dict(x) for x in self.ITEMS], "", {"claude-code": {"m1"}})
        self.assertEqual(note["mode"], "auto")
        self.assertIsNone(items[0]["model"])

    def test_pins_only_ccr_domain_and_names_skipped(self):
        items, note = dag._apply_pinned_model(
            [dict(x) for x in self.ITEMS], "m1", {"claude-code": {"m1"}})
        self.assertEqual(items[0]["model"], "m1")
        self.assertEqual(items[0]["tier"], "explicit")
        self.assertIsNone(items[1]["model"], "codebuddy 的模型域不是 CCR，塞 CCR ID 必报 unknown model")
        self.assertEqual(note["skipped_agents"], ["codebuddy"])
        self.assertTrue(note["catalog_checked"])

    def test_invalid_id_is_400_not_silent_downgrade(self):
        with self.assertRaises(HTTPException) as cm:
            dag._apply_pinned_model([dict(x) for x in self.ITEMS], "ghost/model", {"claude-code": {"m1"}})
        self.assertEqual(cm.exception.status_code, 400)

    def test_catalog_unavailable_does_not_block(self):
        """清单取不到 ⇒ 判不了就不拦（与 P0-4 同口径），但只钉 CCR 域 agent。"""
        items, note = dag._apply_pinned_model([dict(x) for x in self.ITEMS], "m1", None)
        self.assertFalse(note["catalog_checked"])
        self.assertEqual(items[0]["model"], "m1")
        self.assertIsNone(items[1]["model"])


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0stats-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved

    def _mk(self, goal, max_rounds=None, parent=None, round_no=1, tasks_=None):
        items = tasks_ or [{"id": "t1", "task": "做", "agent_id": "claude-code",
                            "deps": [], "difficulty": 1, "model": None}]
        return dag._create_run(goal, items, verify_kind="nonempty", budget_ms=None,
                               budget_tokens=None, max_rounds=max_rounds,
                               parent_run_id=parent, round_no=round_no,
                               auto_rounds=(max_rounds or 1) >= 2)

    def _settle(self, run, task_id, status, agent="claude-code", model=None,
                model_planned=None, error=None, ms=1000, tokens=10):
        db.execute("UPDATE tasks SET status=?,agent_id=?,model=?,model_planned=?,error=?,"
                   "duration_ms=?,spent_tokens=?,finished_at=? WHERE run_id=? AND task_id=?",
                   (status, agent, model, model_planned, error, ms, tokens,
                    tiers._now(), run, task_id))


class TestSchemaAndRounds(_DbCase):
    def test_task_runs_has_max_rounds(self):
        cols = {r["name"] for r in db.query("PRAGMA table_info(task_runs)")}
        self.assertIn("max_rounds", cols)

    def test_max_rounds_persisted_and_clamped(self):
        r5 = self._mk("g5", max_rounds=5)
        self.assertEqual(tasks.run_meta(r5)["max_rounds"], 5)
        big = self._mk("g50", max_rounds=50)
        self.assertEqual(tasks.run_meta(big)["max_rounds"], tiers.ROUNDS_CEILING,
                         "传 50 必须落库成上限值（夹取在写库前做），不是原样存 50")

    def test_legacy_run_keeps_null(self):
        """存量/老调用方：`max_rounds` 存 NULL ⇒ 重拆层回落 MAX_ROUNDS，行为不变。"""
        r = self._mk("legacy")
        self.assertIsNone(tasks.run_meta(r)["max_rounds"])
        self.assertEqual(int(tasks.run_meta(r)["auto_rounds"]), 0)


class TestTimeWindow(_DbCase):
    def test_summary_and_rounds_share_one_window(self):
        """`days` 窗口必须同时约束 run 侧与任务侧。

        2026-11-03 实盘：days=7 时「run 15 个」与「轮次表 14 个 run」同时上屏
        —— 同一份数据的两个窗口。判据：两处 run 总数必须相等。
        """
        inside = self._mk("窗口内的 run")
        outside = self._mk("窗口外的 run")
        # 把一条 run 推到 400 天前，且按同一口径改 task_runs 行
        old = "2024-01-01T00:00:00+00:00"
        db.execute("UPDATE tasks SET created_at=? WHERE run_id=?", (old, outside))
        db.execute("UPDATE task_runs SET created_at=? WHERE run_id=?", (old, outside))
        d = asyncio.run(tasks.task_stats(days=30, top=5))
        runs_in_window = d["summary"]["runs"]
        runs_in_rounds = sum(r["runs"] for r in d["rounds"])
        self.assertEqual(runs_in_window, 1, "窗口外的 run 不得计入 summary")
        self.assertEqual(runs_in_rounds, runs_in_window,
                         "summary 与轮次表必须同一窗口（否则两个数字自相矛盾）")

    def test_days_ago_is_utc_like_created_at(self):
        """窗口下界必须与 `tasks.created_at` 同口径（UTC +00:00）。

        本地无时区字符串与 `+00:00` 比较会差 8 小时（2026-11-03 实盘写法），
        边界上的任务会被静默错判。判据写成「带时区且是 UTC」，不是「看着差不多」。
        """
        v = tasks._days_ago(7)
        self.assertTrue(v.endswith("+00:00"), v)
        # 同一时刻落库值必须能被自己的窗口包含（回归：本地时间写法会差 8 小时）
        row = db.query("SELECT ? AS a, ? AS b", (tiers._now(), v))[0]
        self.assertGreaterEqual(row["a"], row["b"])


class TestRunsHistory(_DbCase):
    def setUp(self):
        super().setUp()
        self.r1 = self._mk("整理日志并汇总")
        self._settle(self.r1, "t1", "success", model="alibaba/qwen3.8-flash")
        self.r2 = self._mk("修复模型 ID 不在册的问题")
        self._settle(self.r2, "t1", "failed", agent="codex",
                     error='exit:1: [codex:unrecognized_model] {"model":"ghost"}')

    def _list(self, **kw):
        args = {"limit": 20, "offset": 0, "q": "", "state": "", "agent": "",
                "model": "", "days": 0}
        args.update(kw)
        return asyncio.run(tasks.list_runs(**args))

    def test_total_is_same_filter_as_rows(self):
        d = self._list()
        self.assertEqual(d["total"], len(d["runs"]))
        self.assertEqual(d["total"], 2)

    def test_search_and_state_and_agent_filters(self):
        self.assertEqual([r["run_id"] for r in self._list(q="整理")["runs"]], [self.r1])
        self.assertEqual([r["run_id"] for r in self._list(state="failed")["runs"]], [self.r2])
        self.assertEqual([r["run_id"] for r in self._list(agent="codex")["runs"]], [self.r2])
        self.assertEqual([r["run_id"] for r in self._list(model="qwen3.8")["runs"]], [self.r1])

    def test_pagination_reports_has_more(self):
        d = self._list(limit=1)
        self.assertEqual(len(d["runs"]), 1)
        self.assertTrue(d["has_more"])
        d2 = self._list(limit=1, offset=1)
        self.assertFalse(d2["has_more"])

    def test_state_is_derived_once(self):
        """列表与汇总对同一条 run 的状态判定必须一致（同一份 `_RUN_STATE_SQL`）。"""
        runs = {r["run_id"]: r for r in self._list()["runs"]}
        stats = asyncio.run(tasks.task_stats(days=0, top=5))
        self.assertEqual(runs[self.r1]["state"], "success")
        self.assertEqual(runs[self.r2]["state"], "failed")
        self.assertEqual(stats["summary"]["runs"], 2)
        self.assertEqual(stats["summary"]["runs_success"], 1)


class TestStats(_DbCase):
    def test_combos_and_failure_buckets(self):
        run = self._mk("组合统计")
        items = [{"id": "t%d" % i, "task": "x", "agent_id": "claude-code",
                  "deps": [], "difficulty": 1, "model": None} for i in (1, 2, 3)]
        run = dag._create_run("组合统计", items, verify_kind="nonempty", budget_ms=None,
                              budget_tokens=None)
        self._settle(run, "t1", "success", model="m-good")
        self._settle(run, "t2", "failed", model="m-bad",
                     error="HTTP 429 Too Many Requests")
        self._settle(run, "t3", "blocked", model="m-bad", error="上游失败")
        d = asyncio.run(tasks.task_stats(days=0, top=5))
        combos = {(c["agent_id"], c["model_eff"]): c for c in d["combos"]}
        self.assertEqual(combos[("claude-code", "m-good")]["ok"], 1)
        self.assertEqual(combos[("claude-code", "m-bad")]["failed"], 1)
        buckets = {f["bucket"]: f["n"] for f in d["failures"]}
        self.assertEqual(buckets.get("限流/配额"), 1)
        self.assertEqual(buckets.get("上游失败(传导)"), 1)
        # 样本不足必须明说，不许拿 3 条样本给百分比结论
        self.assertIn("样本不足", d["verdict"])


class TestStatsRoundsGrouping(_DbCase):
    def test_legacy_run_without_meta_row_merges_into_round_one(self):
        """存量 run（无 task_runs 行 ⇒ r.round 为 NULL）必须与 round=1 归成同一行。

        2026-11-03 实盘缺陷：`GROUP BY round` 在 SQLite 里绑到了源列 `r.round`，
        NULL 与 1 分成两组、两行都显示成「第 1 轮」（实盘 1 组 3 条 / 1 组 35 条）。
        判据写成「轮次行数」，不是「数字看着对不对」。
        """
        self._mk("有元数据的 run")
        b = self._mk("无元数据的存量 run")
        db.execute("DELETE FROM task_runs WHERE run_id=?", (b,))
        d = asyncio.run(tasks.task_stats(days=0, top=5))
        r1 = [r for r in d["rounds"] if r["round"] == 1]
        self.assertEqual(len(r1), 1, "同一轮次只能出一行（NULL 与 1 不许分家）")
        self.assertEqual(r1[0]["runs"], 2)
        self.assertEqual(r1[0]["n"], 2)


class TestChainAndEvents(_DbCase):
    def test_chain_walks_parent_and_child(self):
        parent = self._mk("跨两轮")
        child = self._mk("跨两轮", parent=parent, round_no=2, max_rounds=3)
        db.execute("UPDATE task_runs SET child_run_id=? WHERE run_id=?", (child, parent))
        d = asyncio.run(tasks.run_chain(child))
        self.assertEqual([n["run_id"] for n in d["chain"]], [parent, child])
        self.assertEqual(d["rounds"], 2)
        self.assertEqual(d["ceiling"], tiers.ROUNDS_CEILING)

    def test_events_read_profile_events_by_trace_id(self):
        run = self._mk("看时间线")
        db.log_profile_event("task_exec", "claude-code", "success", 1000,
                             trace_id=run, detail={"task": "t1"})
        d = asyncio.run(tasks.run_events(run, limit=50))
        self.assertTrue(d["events"], "派发事件必须能按 run 取回（trace_id=run_id 是约定）")


if __name__ == "__main__":
    unittest.main()