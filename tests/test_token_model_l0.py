"""L0：token 兜底 / model 口径 / 超时降档（`src/tasks_runner.py`）。

判据（对齐 work/collab-mix-review/SPEC.md 判据 #2/#3/#4）：
  #3 token 兜底：回包无 usage ⇒ spent_tokens>0 且 tokens_estimated=1；
                 有 usage.total_tokens ⇒ 用真值且 tokens_estimated=0。
  #4 model 不污染：`model` 记实发值（exec_model），回包值落 `model_sent`；
                 exec_model 为 None ⇒ model 为 NULL。
  #2 超时降档：last_fail_kind='timeout' 且当前档非最底 ⇒ 下次决议降一档并写 escalated_from_model。

本文件属 L0 hermetic：**零网络**、不连真库（db 全部 mock）、不 import `src.main`。
实测样本：run 159ae98077（t2 codex+t3 超时 600s → 升档 claude-code + t3 再超 600s，20 分钟零产出）。
"""
import asyncio
import pathlib
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db  # noqa: E402
import tasks  # noqa: E402
import tasks_runner as r  # noqa: E402
import tasks_tiers as tiers  # noqa: E402
import tiercfg  # noqa: E402


class _TmpDB(unittest.TestCase):
    """真临时库（不 mock db）：ALTER/落库口径只能这样验，mock 验不出列到底建没建。"""

    #: 建库后是否先补三列。判「补列本身」的用例置 False，否则它会先被 setUp 补掉。
    _auto_cols = True

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0tok-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        if self._auto_cols:
            r.ensure_task_columns()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tasks.set_context(chat_fn=None)

    def _insert(self, tid, *, tier=None, model_planned="alibaba/qwen3.8-max",
                model_choice=None, attempts=None, last_fail_kind=None,
                verify_kind="nonempty"):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,attempts,tier,model_planned,model_choice,last_fail_kind,"
            "created_at,updated_at)"
            " VALUES('r1',?,'r1','g',?,'claude','[]','pending',?,?,?,?,?,?,?,?)",
            (tid, "do " + tid, verify_kind,
             tasks.MAX_AUTO_RETRY if attempts is None else int(attempts),
             tier, model_planned, model_choice, last_fail_kind, now, now))

    def _row(self, tid):
        return db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]

    def _set_chat(self, reply):
        async def chat(agent_id, message, trace_id=None):
            return dict(reply)
        tasks.set_context(chat_fn=chat)

    def _run_once(self, tid):
        row = self._row(tid)
        asyncio.run(tasks._run_task(row["run_id"], row))


class TestEstimateTokens(unittest.TestCase):
    def test_empty_still_counts_one(self):
        self.assertEqual(r._estimate_tokens("", ""), 1)

    def test_proportional_to_length(self):
        self.assertEqual(r._estimate_tokens("a" * 400, ""), 100)
        self.assertEqual(r._estimate_tokens("a" * 200, "b" * 200), 100)

    def test_none_safe(self):
        self.assertEqual(r._estimate_tokens(None, None), 1)

    def test_never_zero(self):
        # 上限保护：宁可低估，也不允许 0（0 与「未知」在报表里同形）
        for n in range(0, 20):
            self.assertGreaterEqual(r._estimate_tokens("x" * n, ""), 1)


class TestTimeoutForTier(unittest.TestCase):
    def test_tier_values(self):
        self.assertEqual(r._timeout_s_for("t1"), 300)
        self.assertEqual(r._timeout_s_for("t2"), 600)
        self.assertEqual(r._timeout_s_for("t3"), 900)

    def test_unknown_falls_back(self):
        self.assertEqual(r._timeout_s_for(None), 600)
        self.assertEqual(r._timeout_s_for("t9"), 600)
        self.assertEqual(r._timeout_s_for("explicit"), 600)


class TestPrevTier(unittest.TestCase):
    def test_downgrade_chain(self):
        self.assertEqual(r._prev_tier_of("t3"), "t2")
        self.assertEqual(r._prev_tier_of("t2"), "t1")

    def test_bottom_and_unknown(self):
        self.assertIsNone(r._prev_tier_of("t1"))
        self.assertIsNone(r._prev_tier_of("explicit"))
        self.assertIsNone(r._prev_tier_of(None))


class TestFixRetryColumns(unittest.TestCase):
    def test_writes_observed_columns(self):
        calls = []
        with mock.patch.object(r, "db") as m:
            m.execute.side_effect = lambda sql, params=None: calls.append((sql, params))
            r._fix_retry_columns("run1", "t1", "alibaba/qwen3.8-max",
                                 "openrouter/some-vl:free", 1, "timeout")
        self.assertEqual(len(calls), 1)
        sql, params = calls[0]
        for col in ("model_sent=?", "tokens_estimated=?", "last_fail_kind=?"):
            self.assertIn(col, sql)
        self.assertEqual(params[0], "alibaba/qwen3.8-max")   # model_sent = 实发值
        self.assertEqual(params[1], 1)
        self.assertEqual(params[2], "timeout")
        self.assertEqual(list(params[-2:]), ["run1", "t1"])

    def test_model_never_overwritten(self):
        # 回归（集成裁决）：本函数**不许**写 `model` 列 —— 那是既有契约（回包观测值）。
        # 实发值只能进 `model_sent`。
        calls = []
        with mock.patch.object(r, "db") as m:
            m.execute.side_effect = lambda sql, params=None: calls.append((sql, params))
            r._fix_retry_columns("run1", "t1", "alibaba/qwen3.8-max", "reported-x", 0, "timeout")
        sql, params = calls[0]
        self.assertNotIn("model=?", sql)
        self.assertEqual(params[0], "alibaba/qwen3.8-max", "model_sent 取实发值")

    def test_last_fail_kind_optional(self):
        calls = []
        with mock.patch.object(r, "db") as m:
            m.execute.side_effect = lambda sql, params=None: calls.append((sql, params))
            r._fix_retry_columns("run1", "t1", "m", None)
        sql, _params = calls[0]
        self.assertNotIn("last_fail_kind=?", sql)

    def test_db_error_swallowed(self):
        # 观测列写不进去不得带倒任务
        with mock.patch.object(r, "db") as m:
            m.execute.side_effect = RuntimeError("no such column")
            r._fix_retry_columns("run1", "t1", "m", "r", 0, "timeout")


class TestResolveExecTargetDowngrade(unittest.TestCase):
    """last_fail_kind='timeout' ⇒ 降档；其它 ⇒ 走原升档（逐字不变）。"""

    def _row(self, **kw):
        base = {"task_id": "t2", "agent_id": "codex", "trace_id": "tr",
                "tier": "t3", "model_planned": "alibaba/qwen3.8-max",
                "model_choice": None, "last_fail_kind": "timeout"}
        base.update(kw)
        return base

    def test_timeout_downgrades_tier(self):
        calls, events = [], []
        with mock.patch.object(r, "db") as m, \
                mock.patch.object(r._tiers, "_catalog_snapshot", return_value=({"m1"}, True)), \
                mock.patch.object(r._tiers, "escalate_target",
                                  side_effect=AssertionError("不该走升档")):
            m.execute.side_effect = lambda sql, params=None: calls.append((sql, params))
            m.log_profile_event.side_effect = lambda *a, **k: events.append((a, k))
            agent, model = r._resolve_exec_target("run1", self._row(), attempts_done=1)
        self.assertEqual(agent, "codex")            # agent 不动，只降档
        self.assertIsNotNone(model)
        sql, params = calls[0]
        self.assertIn("tier=?", sql)
        self.assertEqual(params[1], "downgrade:timeout")   # tier_reason 参数
        self.assertEqual(params[0], "t2")          # t3 → t2
        self.assertEqual(events[0][0][2], "downgrade")

    def test_timeout_at_bottom_falls_through_to_escalate(self):
        with mock.patch.object(r, "db") as m, \
                mock.patch.object(r._tiers, "_catalog_snapshot", return_value=({"m1"}, True)), \
                mock.patch.object(r._tiers, "_known_agent_ids", return_value={"codex", "claude-code"}), \
                mock.patch.object(r._tiers, "escalate_target",
                                  return_value={"changed": True, "agent_id": "claude-code",
                                                "model": None, "from_agent": "codex",
                                                "from_model": "alibaba/qwen3.8-max",
                                                "reason": "agent:claude-code"}):
            m.execute.side_effect = lambda *a, **k: None
            m.log_profile_event.side_effect = lambda *a, **k: None
            agent, _model = r._resolve_exec_target("run1", self._row(tier="t1"), attempts_done=1)
        self.assertEqual(agent, "claude-code")     # 触底无档可降 ⇒ 仍走升档

    def test_non_timeout_uses_escalate(self):
        with mock.patch.object(r, "db") as m, \
                mock.patch.object(r._tiers, "_catalog_snapshot", return_value=({"m1"}, True)), \
                mock.patch.object(r._tiers, "_known_agent_ids", return_value={"codex"}), \
                mock.patch.object(r._tiers, "escalate_target",
                                  return_value={"changed": False, "agent_id": "codex",
                                                "model": "alibaba/qwen3.8-max",
                                                "from_agent": "codex",
                                                "from_model": "alibaba/qwen3.8-max",
                                                "reason": "first_attempt"}):
            agent, model = r._resolve_exec_target("run1", self._row(last_fail_kind=None), attempts_done=0)
        self.assertEqual((agent, model), ("codex", "alibaba/qwen3.8-max"))


class TestColumnDdl(unittest.TestCase):
    def test_extra_columns_declared(self):
        cols = dict(r._TASK_COLUMNS_EXTRA)
        self.assertIn("model_sent", cols)
        self.assertIn("tokens_estimated", cols)
        self.assertIn("last_fail_kind", cols)

    def test_ensure_is_idempotent_shape(self):
        # 已存在该列 ⇒ 不 ALTER；返回新增列表
        with mock.patch.object(r, "db") as m:
            m.current_path.return_value = "/tmp/never.db"
            m.query.return_value = [{"name": c} for c, _ in r._TASK_COLUMNS_EXTRA]
            added = r.ensure_task_columns()
        self.assertEqual(added, [])


class TestEnsureTaskColumnsRealDB(_TmpDB):
    """判据 #4：ALTER 幂等 —— **真库连跑两次**不炸，列不重复。"""

    _auto_cols = False   # 这一组要验「补列」本身，setUp 不能替它先补

    def test_adds_all_declared_columns(self):
        r._columns_ready.clear()
        # tasks.ensure_schema 不管这些观测列 ⇒ 第一次必须真的补出来
        added = r.ensure_task_columns()
        declared = [c for c, _ in r._TASK_COLUMNS_EXTRA]
        self.assertEqual(sorted(added), sorted(declared))
        cols = {x["name"] for x in db.query("PRAGMA table_info(tasks)")}
        for c in declared:
            self.assertIn(c, cols, "缺列 %s" % c)

    def test_second_run_no_error_no_duplicate(self):
        r._columns_ready.clear()
        r.ensure_task_columns()
        r._columns_ready.clear()          # 逼它真的再查一遍 PRAGMA + 再尝试一次
        r.ensure_task_columns()
        r._columns_ready.clear()
        self.assertEqual(r.ensure_task_columns(), [])   # 第三遍：无新增、无异常
        names = [x["name"] for x in db.query("PRAGMA table_info(tasks)")]
        self.assertEqual(len(names), len(set(names)), "重复 ALTER 导致列重复")


class TestTokenFallbackRealDB(_TmpDB):
    """判据 #3：无 usage ⇒ 估算 + tokens_estimated=1；有 usage ⇒ 真值 + 0。"""

    def test_no_usage_estimates_and_flags(self):
        self._insert("t_no_usage")
        self._set_chat({"success": True, "response": "x" * 4000})   # 无 usage、无 model
        self._run_once("t_no_usage")
        row = self._row("t_no_usage")
        self.assertGreater(row["spent_tokens"], 0, "无 usage 必须估算，不能记 0")
        self.assertEqual(row["tokens_estimated"], 1)

    def test_real_usage_kept_and_flag_zero(self):
        self._insert("t_usage")
        self._set_chat({"success": True, "response": "ok",
                        "usage": {"total_tokens": 1234}})
        self._run_once("t_usage")
        row = self._row("t_usage")
        self.assertEqual(row["spent_tokens"], 1234, "有真值必须用真值，不许估算覆盖")
        self.assertEqual(row["tokens_estimated"], 0)

    def test_estimate_matches_documented_rule(self):
        self._insert("t_rule", model_planned=None)
        reply = {"success": True, "response": "y" * 800}
        sent = []

        async def chat(agent_id, message, trace_id=None):
            sent.append(message)          # 判据吃的是**实际下发**的 prompt，不是 tasks.prompt 原列
            return dict(reply)
        tasks.set_context(chat_fn=chat)
        self._run_once("t_rule")
        self.assertEqual(len(sent), 1)
        row = self._row("t_rule")
        self.assertEqual(row["spent_tokens"],
                         r._estimate_tokens(sent[0], reply["response"]))
        self.assertEqual(row["tokens_estimated"], 1)


class TestModelPurityRealDB(_TmpDB):
    """判据 #4（集成裁决 2026-11-03）：`model` 保持既有契约 = adapter 回包值（观测面，
    判据 #4：`model_sent` = 实发值（`exec_model`）；`model` 保持**既有契约** = 回包自报值。
    两列不混写 —— 混写就等于把「我们让谁跑」与「实际谁跑了」合成一列。"""

    def test_exec_model_wins_over_reply_model(self):
        self._insert("t_exec", model_planned="alibaba/qwen3.8-max")
        self._set_chat({"success": True, "response": "ok",
                        "model": "openrouter/nvidia/nemotron:free",
                        "usage": {"total_tokens": 5}})
        self._run_once("t_exec")
        row = self._row("t_exec")
        self.assertEqual(row["model_sent"], "alibaba/qwen3.8-max", "model_sent 必须是实发值")
        self.assertEqual(row["model"], "openrouter/nvidia/nemotron:free",
                         "model 保持既有契约 = 回包观测值")

    def test_no_exec_model_leaves_model_null(self):
        self._insert("t_none", model_planned=None, model_choice=None)
        self._set_chat({"success": True, "response": "ok", "model": "some/other"})
        self._run_once("t_none")
        row = self._row("t_none")
        self.assertIsNone(row["model_sent"], "未钉模型时 model_sent 必须 NULL，不许拿回包值填")
        self.assertEqual(row["model"], "some/other", "回包值仍按既有契约记 model")

    def test_model_recorded_even_when_verify_fails(self):
        self._insert("t_fail", model_planned="m-exec", verify_kind="json_array")
        self._set_chat({"success": True, "response": "not-json",
                        "model": "m-reply", "usage": {"total_tokens": 7}})
        self._run_once("t_fail")
        row = self._row("t_fail")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["model_sent"], "m-exec")
        self.assertEqual(row["model"], "m-reply")


class TestTimeoutMarkerRealDB(_TmpDB):
    """判据 #2：超时写 last_fail_kind='timeout'，下次决议据此降一档。"""

    def test_timeout_writes_marker_and_next_decision_downgrades(self):
        self._insert("t_to", tier="t3", model_planned="m3",
                     attempts=tasks.MAX_AUTO_RETRY)   # 额度用满 ⇒ 终态 failed 分支

        async def slow(*a, **k):
            await asyncio.sleep(5)
        tasks.set_context(chat_fn=slow)
        with mock.patch.object(r, "_timeout_s_for", return_value=0.05), \
                mock.patch.object(tiercfg.timeout_for_tier, "__call__", create=True):
            row = self._row("t_to")
            asyncio.run(tasks._run_task(row["run_id"], row))
        row = self._row("t_to")
        self.assertEqual(row["status"], "failed")
        self.assertEqual(row["last_fail_kind"], "timeout")
        self.assertEqual(row["model_sent"], "m3", "超时也要记下我们让谁跑")
        self.assertIsNone(row["model"], "超时没有回包 ⇒ model 留 NULL（不许拿实发值填）")

        # 下一次决议：读标记 ⇒ 降一档并写 escalated_from_model
        calls = []
        with mock.patch.object(r, "db") as m, \
                mock.patch.object(r._tiers, "_catalog_snapshot", return_value=(set(), False)):
            m.execute.side_effect = lambda sql, params=None: calls.append((sql, params))
            m.log_profile_event.side_effect = lambda *a, **k: None
            _agent, model = r._resolve_exec_target("r1", row, attempts_done=1)
        self.assertIn("tier=?", calls[0][0])
        self.assertEqual(calls[0][1][0], "t2", "t3 超时后必须降到 t2")
        self.assertIn("escalated_from_model=?", calls[0][0])


class TestTiercfgMissingFallback(_TmpDB):
    """A 未落地（函数缺失）⇒ 老行为，且**绝不抛异常**。"""

    def _without(self, *names):
        """把 tiercfg 上的若干函数临时删掉（用空模块顶替，等价于「函数不存在」）。"""
        stub = types.SimpleNamespace(**{k: v for k, v in vars(tiercfg).items()
                                        if k not in names})
        return mock.patch.object(r, "_tiercfg_mod", return_value=stub)

    def test_timeout_falls_back_to_global(self):
        with self._without("timeout_for_tier"):
            self.assertEqual(r._timeout_s_for("t1"), tiers.TASK_TIMEOUT_S)
            self.assertEqual(r._timeout_s_for("t3"), tiers.TASK_TIMEOUT_S)
            self.assertEqual(r._timeout_s_for(None), tiers.TASK_TIMEOUT_S)

    def test_timeout_falls_back_when_module_missing(self):
        with mock.patch.object(r, "_tiercfg_mod", return_value=None):
            self.assertEqual(r._timeout_s_for("t3"), tiers.TASK_TIMEOUT_S)
            self.assertIsNone(r._prev_tier_of("t3"))

    def test_prev_tier_missing_means_no_downgrade(self):
        with self._without("prev_tier"):
            self.assertIsNone(r._prev_tier_of("t3"))
        # 决议退回原升档路径，不因降档函数缺失而卡死
        calls = []
        with self._without("prev_tier"), \
                mock.patch.object(r, "db") as m, \
                mock.patch.object(r._tiers, "_catalog_snapshot", return_value=({"m1"}, True)), \
                mock.patch.object(r._tiers, "_known_agent_ids", return_value={"codex"}), \
                mock.patch.object(r._tiers, "escalate_target",
                                  return_value={"changed": False, "agent_id": "codex",
                                                "model": "m3", "from_agent": "codex",
                                                "from_model": "m3", "reason": "no_path"}):
            m.execute.side_effect = lambda sql, params=None: calls.append((sql, params))
            m.log_profile_event.side_effect = lambda *a, **k: None
            row = {"task_id": "t1", "agent_id": "codex", "trace_id": "tr",
                   "tier": "t3", "model_planned": "m3", "last_fail_kind": "timeout"}
            agent, model = r._resolve_exec_target("r1", row, attempts_done=1)
        self.assertEqual((agent, model), ("codex", "m3"))
        self.assertFalse([c for c in calls if "downgrade" in str(c)], "缺函数时不许假降档")

    def test_timeout_for_tier_raising_is_swallowed(self):
        boom = types.SimpleNamespace(timeout_for_tier=lambda t: 1 / 0, prev_tier=lambda t: 1 / 0)
        with mock.patch.object(r, "_tiercfg_mod", return_value=boom):
            self.assertEqual(r._timeout_s_for("t3"), tiers.TASK_TIMEOUT_S)
            self.assertIsNone(r._prev_tier_of("t3"))


if __name__ == "__main__":
    unittest.main()