#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P0-a + P0-b —— 成本归因接线 与 验收裁决落库。

【背景：这两项为什么必须做】
P0-3 落地了「执行者不得自证 success」，但**裁决结果本身没被保存**：`verdict` 三态
只被折叠进 `status`/`error` 文本就丢了 ⇒ 验收做了，却没有任何查询面能回答
「这批任务里有多少是 verifier 判的通过、多少是证据不足」。

P0-a 同理：`adapter.chat` **本来就返回** `model` 与 `usage`
（`adapters/openai_compat.py:66-67`），但 `_run_task` 只取了 `usage.total_tokens`
就丢，`model` **完全没被读过** ⇒ 「任务→Agent→模型→token」无法 JOIN，
成本归因断在最后一米。

【本文件守五条】
1. **model 取回包而非请求值**：上游可能改写模型名；记请求值就是把「我们以为用了谁」
   当成「实际用了谁」，恰好毁掉归因的意义。回包缺 model 时必须留 NULL，不许拿请求值兜底。
2. **verdict 三态都要落库**（pass/fail/insufficient），`insufficient` 尤其重要 ——
   它是「判不了且未放行」，与 pass/fail 都不同，丢了就看不出门禁在拦什么。
3. **未走到验收 ⇒ verdict 为 NULL**，不是 'fail'。超时/预算耗尽/自称失败三条早退路径
   从没过 verifier，把它们记成 fail 会虚增失败率、污染后续任何统计。
4. **验收失败也必须记 model**：产物不合格照样花了钱，成本不能跟着 verdict 一起消失。
5. **幂等迁移**：三列靠 `ensure_schema` 的 ALTER 补建，跑两遍不得炸（存量库必过这关）。

分层口径（tests/tiers.py）：临时 db + 假 chat_fn，不起服务、不连网、零 SKIP。
"""
import asyncio
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import db      # noqa: E402
import tasks   # noqa: E402
import verifier  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0p0ab-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self._saved_conc = tasks.MAX_CONCURRENCY
        self._saved_sleep = tasks.LOOP_SLEEP_S
        tasks.MAX_CONCURRENCY = 1
        tasks.LOOP_SLEEP_S = 0.01
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tasks.MAX_CONCURRENCY = self._saved_conc
        tasks.LOOP_SLEEP_S = self._saved_sleep
        tasks.set_context(chat_fn=None)

    def _insert(self, run_id, tid, *, verify_kind="nonempty", budget_ms=None,
                budget_tokens=None, prompt=None, expect=None, attempts=None):
        """`attempts` 默认 = `MAX_AUTO_RETRY`（重试额度已用完）。

        2026-10-22 批 B3：「可修复的验证失败」会先自动重试一次（`status=pending`）。
        本文件断言的是**最终裁决落库**（model / verdict / verify_reason），
        不是第一次尝试的中间态 ⇒ 默认把额度设为已用完，语义与改动前一致。
        """
        now = tasks._now()
        att = tasks.MAX_AUTO_RETRY if attempts is None else int(attempts)
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,verify_expect,budget_ms,budget_tokens,attempts,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?,?)",
            (run_id, tid, run_id, "g", prompt or ("do " + tid), "claude",
             tasks.json.dumps([]), verify_kind, expect, budget_ms, budget_tokens,
             att, now, now))

    def _row(self, tid):
        return db.query(
            "SELECT status,error,verdict,verify_reason,model,spent_tokens,output "
            "FROM tasks WHERE task_id=?", (tid,))[0]

    def _set_chat(self, reply):
        async def chat(agent_id, message, trace_id=None):
            return dict(reply)
        tasks.set_context(chat_fn=chat)

    def _run_task_once(self, tid):
        row = db.query("SELECT * FROM tasks WHERE task_id=?", (tid,))[0]
        asyncio.run(tasks._run_task(row["run_id"], row))


class TestSchemaMigration(_Base):
    """判据 5：幂等迁移。存量库已有 tasks 表，三列必须补得出来，且第二遍不炸。"""

    def test_three_columns_exist(self):
        cols = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
        for c in ("model", "verdict", "verify_reason"):
            self.assertIn(c, cols, f"缺列 {c}")

    def test_migration_is_idempotent(self):
        tasks.ensure_schema()
        tasks.ensure_schema()
        cols = [r["name"] for r in db.query("PRAGMA table_info(tasks)")]
        self.assertEqual(len(cols), len(set(cols)), "重复 ALTER 导致列重复")

    def test_legacy_row_defaults(self):
        """存量行迁移后 verdict 必须是 NULL（未验收），不能被 DEFAULT 成 'fail'。"""
        self._insert("r0", "t_legacy")
        r = self._row("t_legacy")
        self.assertIsNone(r["verdict"])
        self.assertIsNone(r["model"])


class TestModelAttribution(_Base):
    """判据 1 + 4：model 取回包、验收失败也记 model。"""

    def test_model_recorded_from_adapter_reply(self):
        self._insert("r1", "t_ok")
        self._set_chat({"success": True, "response": "ok-text",
                        "model": "deepseek-v4.1-flash",
                        "usage": {"total_tokens": 1234}})
        self._run_task_once("t_ok")
        r = self._row("t_ok")
        self.assertEqual("deepseek-v4.1-flash", r["model"],
                         "model 必须取 adapter 回包值")
        self.assertEqual(1234, r["spent_tokens"])
        self.assertEqual("success", r["status"])

    def test_model_null_when_reply_lacks_it(self):
        """回包没给 model ⇒ 留 NULL。绝不能拿请求值兜底（那是猜测不是观测）。"""
        self._insert("r2", "t_nomodel")
        self._set_chat({"success": True, "response": "ok-text",
                        "usage": {"total_tokens": 10}})
        self._run_task_once("t_nomodel")
        self.assertIsNone(self._row("t_nomodel")["model"],
                          "回包缺 model 时不得用请求值填充")

    def test_model_recorded_even_when_verify_fails(self):
        """判据 4：产物不合格照样花钱，成本不能跟着 verdict 消失。"""
        self._insert("r3", "t_bad", verify_kind="contains", expect="NEEDLE")
        self._set_chat({"success": True, "response": "empty-answer",
                        "model": "qwen3.8-max", "usage": {"total_tokens": 77}})
        self._run_task_once("t_bad")
        r = self._row("t_bad")
        self.assertEqual("failed", r["status"])
        self.assertEqual("qwen3.8-max", r["model"], "验收失败也必须记 model")


class TestVerdictPersistence(_Base):
    """判据 2 + 3：三态落库；未验收路径 verdict 为 NULL。"""

    def test_verdict_pass(self):
        self._insert("r4", "t_pass")
        self._set_chat({"success": True, "response": "nonempty-answer",
                        "model": "m1", "usage": {"total_tokens": 5}})
        self._run_task_once("t_pass")
        r = self._row("t_pass")
        self.assertEqual("success", r["status"])
        self.assertEqual(verifier.STATUS_PASS, r["verdict"],
                         "通过必须留痕，不能只体现在 status 上")

    def test_verdict_fail_with_reason(self):
        self._insert("r5", "t_fail", verify_kind="contains", expect="NEEDLE")
        self._set_chat({"success": True, "response": "no-marker",
                        "model": "m2", "usage": {"total_tokens": 6}})
        self._run_task_once("t_fail")
        r = self._row("t_fail")
        self.assertEqual("failed", r["status"])
        self.assertEqual(verifier.STATUS_FAIL, r["verdict"])
        self.assertTrue(r["verify_reason"], "失败必须给出可读理由")

    def test_verdict_insufficient_is_distinguishable(self):
        """insufficient 是「判不了且未放行」，第三态，必须与 pass/fail 都可区分。

        触发口径照 verifier 真实语义：`contains` 缺 `expect` ⇒ 判据缺失 ⇒ insufficient
        （`nonempty` 空产物是 fail，不是 insufficient —— 别拿它当 insufficient 的样本）。
        """
        self._insert("r6", "t_insuf", verify_kind="contains", expect=None)
        self._set_chat({"success": True, "response": "任意产物",
                        "model": "m3", "usage": {"total_tokens": 4}})
        self._run_task_once("t_insuf")
        r = self._row("t_insuf")
        self.assertNotEqual("success", r["status"], "判不了不得放行")
        self.assertEqual(verifier.STATUS_INSUFFICIENT, r["verdict"])
        self.assertNotEqual(verifier.STATUS_FAIL, r["verdict"],
                            "insufficient 不是 fail，两者语义不同")

    def test_verdict_null_on_self_reported_failure(self):
        """判据 3：自称失败没过 verifier ⇒ verdict 必须 NULL，不是 fail。"""
        self._insert("r7", "t_self")
        self._set_chat({"success": False, "error": "upstream 500",
                        "model": "m4", "usage": {"total_tokens": 3}})
        self._run_task_once("t_self")
        r = self._row("t_self")
        self.assertEqual("failed", r["status"])
        self.assertIsNone(r["verdict"], "没过 verifier 的路径不得有 verdict")

    def test_verdict_null_on_budget_exhausted(self):
        """用 **token 预算**而非耗时预算触发。

        耗时预算（`spent_ms > budget_ms`）在假 chat 下不可靠：回包瞬返 ⇒ dur=0，
        任何非负预算都判不出超限，测试就成了假绿。token 预算是确定性的。
        """
        self._insert("r8", "t_budget", budget_tokens=1)
        self._set_chat({"success": True, "response": "ok", "model": "m5",
                        "usage": {"total_tokens": 9}})
        self._run_task_once("t_budget")
        r = self._row("t_budget")
        self.assertEqual("failed", r["status"])
        self.assertIsNone(r["verdict"], "预算耗尽没过 verifier")
        self.assertEqual("m5", r["model"], "预算耗尽同样花了钱，要记 model")

    def test_verdict_null_on_timeout(self):
        async def slow(agent_id, message, trace_id=None):
            await asyncio.sleep(5)
            return {"success": True, "response": "late", "model": "m6"}
        tasks.set_context(chat_fn=slow)
        self._insert("r9", "t_to")
        self._saved_to = tasks.TASK_TIMEOUT_S
        tasks.TASK_TIMEOUT_S = 0.05
        try:
            self._run_task_once("t_to")
        finally:
            tasks.TASK_TIMEOUT_S = self._saved_to
        r = self._row("t_to")
        self.assertEqual("failed", r["status"])
        self.assertIsNone(r["verdict"], "超时没过 verifier")


class TestJoinability(_Base):
    """判据收口：这张表现在能不能真的回答业务问题，而不只是存了东西。"""

    def test_can_query_run_cost_and_verdict(self):
        """模拟真实诉求：这一批任务花了多少 token、用了哪些模型、验收分布如何。"""
        self._insert("j1", "a1")
        self._set_chat({"success": True, "response": "ans-a",
                        "model": "deepseek-v4.1-flash",
                        "usage": {"total_tokens": 100}})
        self._run_task_once("a1")

        self._insert("j1", "a2", verify_kind="contains", expect="ZZZ")
        self._set_chat({"success": True, "response": "ans-b",
                        "model": "qwen3.8-max", "usage": {"total_tokens": 250}})
        self._run_task_once("a2")

        rows = db.query(
            "SELECT model, spent_tokens, verdict FROM tasks "
            "WHERE run_id=? AND verdict IS NOT NULL", ("j1",))
        self.assertEqual(2, len(rows))
        total = sum(r["spent_tokens"] for r in rows)
        self.assertEqual(350, total, "按验收过的任务聚合 token")
        models = sorted(r["model"] for r in rows)
        self.assertEqual(["deepseek-v4.1-flash", "qwen3.8-max"], models,
                         "必须能查出每个任务实际用了哪个模型")

    def test_group_by_model_is_possible(self):
        """下一步 P0-c（模型画像）要按模型聚合，先证明聚合面已通。"""
        for tid, mdl in (("g1", "mm"), ("g2", "mm"), ("g3", "nn")):
            self._insert("gr", tid)
            self._set_chat({"success": True, "response": "x", "model": mdl,
                            "usage": {"total_tokens": 10}})
            self._run_task_once(tid)
        rows = db.query(
            "SELECT model, COUNT(*) n, SUM(spent_tokens) tok FROM tasks "
            "WHERE run_id=? GROUP BY model ORDER BY model", ("gr",))
        got = {r["model"]: (r["n"], r["tok"]) for r in rows}
        self.assertEqual({"mm": (2, 20), "nn": (1, 10)}, got)


if __name__ == "__main__":
    unittest.main(verbosity=2)
