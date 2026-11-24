#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：协同 agent 的模型不能正常使用 ⇒ 自动换可用模型（2026-11-21 用户裁定）。

【本文件守四条】
1. **只认显式模型报文**：`unknown model` / `model_resolution` / `请使用 --model` /
   `模型不存在` 判为模型问题；超时、429、generic 异常**不判**（误判会白烧一次调用）。
2. **换模型不占验证重试额度**：`attempts` 用满（=1）时仍能回退一次模型；
   次数由独立的 `model_fallbacks` / `MAX_MODEL_FALLBACK` 约束，防无限换。
3. **不换不该换的**：模型域不是 CCR 的 agent（codebuddy/cursor）、清单取不到时
   返回 None —— 不猜 ID。
4. **派发标签 / trace 必须传到适配器**：心跳判死靠 `task-<run>-<task>` 找回 pid；
   传不到 = 600s 满超时升级形同虚设（11-03 实弹）。

另附一条**真进程**用例：取消（心跳判死 / 外层超时）必须杀掉整个 CLI 进程组，
否则「已放弃的任务」继续跑、重试再起一份 ⇒ 同一子任务两个进程并发（实测 43 分钟孤儿）。
"""
import asyncio
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import adapters.cli_agent as cli  # noqa: E402
import db                         # noqa: E402
import tasks                      # noqa: E402
import tasks_tiers as tiers       # noqa: E402
import tasks_runner as runner     # noqa: E402


class TestModelFailureClassify(unittest.TestCase):
    def test_explicit_model_messages(self):
        for text in ("unknown model: agnes/agnes-2.5-flash",
                     "HTTP 400: model_resolution",
                     "Please use --model <model_id>",
                     "400 model not found",
                     'Error: Model "alibaba/deepseek-v4.1-flash" not found.',
                     "模型不存在：alibaba/nope",
                     "该模型不可用"):
            self.assertTrue(tiers.is_model_failure(text), text)

    def test_generic_failures_are_not_model_failures(self):
        for text in ("超时熔断 600s", "RATE_LIMIT 429", "upstream connect error",
                     "empty-output: 退出码 0 但无正文", "", None):
            self.assertFalse(tiers.is_model_failure(text), repr(text))

    def test_channel_credential_failures(self):
        """模型 ID 合法、走的通道没凭据 —— 实测 pi/omp 的 alibaba 全系都这样。"""
        for text in ("exit:1: No API key found for vercel-ai-gateway.",
                     "missing api key for provider",
                     "no available channel for this model"):
            self.assertTrue(tiers.is_channel_failure(text), text)
        self.assertFalse(tiers.is_channel_failure("unknown model: foo"))

    def test_local_catalog_miss_prefers_agent_default(self):
        """pi/omp 的本地清单不含 tier 表里的 CCR ID —— 换同档 ID 只会再撞一次。"""
        for text in ('Error: Model "alibaba/x" not found. Use --list-models to see available models.',
                     "Run `omp models find <pattern>` to search, or `omp models` to list all."):
            self.assertTrue(tiers.is_agent_catalog_miss(text), text)
            self.assertTrue(tiers.prefers_agent_default(text), text)


class TestEventVocabulary(unittest.TestCase):
    def test_model_fallback_event_is_mapped(self):
        """新事件必须进公共词表，否则 runlog 里显示 unknown（处置过 = 看不懂）。"""
        import execsem
        for status in ("model_fallback", "model_switch"):
            out, mapped = execsem.canonical_outcome("task_exec", status)
            self.assertTrue(mapped, status)
            self.assertEqual("degraded", out, status)


class TestFallbackTarget(unittest.TestCase):
    TIERS = {"t1": ["m1", "m1b"], "t2": ["m2", "m2b"], "t3": ["m3"]}
    CAT = {"m1", "m1b", "m2", "m2b", "m3"}

    def _pick(self, agent="pi", tier="t2", tried=(), checked=True):
        return tiers.model_fallback_target(
            agent, tier, list(tried), catalog=self.CAT, catalog_checked=checked,
            tiers_cfg=self.TIERS)

    def test_same_tier_first(self):
        self.assertEqual("m2", self._pick())
        self.assertEqual("m2b", self._pick(tried=["m2"]))

    def test_then_higher_then_lower(self):
        self.assertEqual("m3", self._pick(tried=["m2", "m2b"]))
        self.assertEqual("m1", self._pick(tried=["m2", "m2b", "m3"]))

    def test_explicit_tier_scans_all(self):
        self.assertEqual("m1", self._pick(tier="explicit"))

    def test_native_agent_has_no_model_channel(self):
        self.assertIsNone(self._pick(agent="codebuddy"), "native 模型域塞 CCR ID 必失败")

    def test_catalog_unavailable_does_not_guess(self):
        self.assertIsNone(self._pick(checked=False))

    def test_all_tier_models_tried_falls_to_agent_default(self):
        got = self._pick(tried=["m1", "m1b", "m2", "m2b", "m3"])
        self.assertIn(got, {tiers.AGENT_DEFAULT_MODEL, tiers._agent_default_model("pi")},
                      "全部档位都试过 ⇒ 最后的安全网是 agent 自己的默认模型")
        self.assertIsNotNone(got)

    def test_everything_tried_returns_none(self):
        tried = ["m1", "m1b", "m2", "m2b", "m3",
                 tiers.AGENT_DEFAULT_MODEL, tiers._agent_default_model("pi")]
        self.assertIsNone(self._pick(tried=[x for x in tried if x]))

    def test_channel_failure_prefers_agent_default(self):
        """通道没凭据时换同档模型只会再撞同一把锁 ⇒ 先回 agent 默认。"""
        with mock.patch.object(tiers, "_agent_default_model", return_value="agent/default"):
            self.assertEqual("agent/default", tiers.model_fallback_target(
                "pi", "t2", [], catalog=self.CAT, catalog_checked=True,
                tiers_cfg=self.TIERS, prefer_agent_default=True))
            self.assertEqual("m2", tiers.model_fallback_target(
                "pi", "t2", [], catalog=self.CAT, catalog_checked=True,
                tiers_cfg=self.TIERS, prefer_agent_default=False))


class TestRetrySchedulesModelFallback(unittest.TestCase):
    def setUp(self):
        tasks.set_context(agent_ids_fn=lambda: {"pi", "claude-code", "codex"})
        self.addCleanup(tasks.set_context, chat_fn=None, agent_ids_fn=lambda: set())
        self.row = {"task_id": "t2", "agent_id": "pi", "trace_id": "run1",
                    "tier": "t2", "model_planned": "bad/model",
                    "model_sent": "bad/model", "model_fallbacks": 0}

    def test_model_failure_falls_back_even_when_attempts_exhausted(self):
        """attempts 用满（MAX_AUTO_RETRY）也必须能回退模型：换模型是另一维度。"""
        with mock.patch.object(tiers, "_catalog_snapshot",
                               return_value=({"bad/model", "good/model"}, True)), \
                mock.patch("tiercfg.parse_tiers",
                           return_value={"t1": ["good/model"],
                                         "t2": ["bad/model", "good/model"]}), \
                mock.patch.object(db, "execute") as ex, \
                mock.patch.object(db, "log_profile_event"):
            ok = tiers._schedule_exec_retry(
                "run1", self.row, "unknown model: bad/model", "executor_claimed_failure",
                tiers.MAX_AUTO_RETRY, "pi", "bad/model", kind="nonempty")
        self.assertTrue(ok, "模型不可用却没排回退 ⇒ 直接终态")
        sql, params = ex.call_args[0][0], ex.call_args[0][1]
        self.assertIn("model_planned=?", sql)
        self.assertEqual("good/model", params[0])
        self.assertIn("model_fallbacks", sql)
        self.assertIn("bad/model", params[2], "tried 必须含失败模型，防换回同一个")

    def test_fallback_budget_is_bounded(self):
        row = dict(self.row, model_fallbacks=tiers.MAX_MODEL_FALLBACK)
        with mock.patch.object(tiers, "_catalog_snapshot",
                               return_value=({"bad/model", "good/model"}, True)), \
                mock.patch.object(db, "execute") as ex, \
                mock.patch.object(db, "log_profile_event"):
            ok = tiers._schedule_exec_retry(
                "run1", row, "unknown model: bad/model", "executor_claimed_failure",
                tiers.MAX_AUTO_RETRY, "pi", "bad/model", kind="nonempty")
        self.assertFalse(ok, "回退次数与 attempts 都用满后必须停（防无限换）")
        ex.assert_not_called()


class TestCallChatForwardsTag(unittest.IsolatedAsyncioTestCase):
    async def test_tag_and_trace_reach_chat_fn(self):
        seen = {}

        async def chat(agent_id, message, session_id=None, model=None,
                       tag=None, trace_id=None):
            seen.update(agent_id=agent_id, session_id=session_id, model=model,
                        tag=tag, trace_id=trace_id)
            return {"success": True, "response": "ok"}

        await runner._call_chat(chat, "pi", "prompt", "task-run1-t1", "m1", "t2",
                                trace_id="run1")
        self.assertEqual("task-run1-t1", seen["tag"], "CLI 登记/心跳只认 tag")
        self.assertEqual("run1", seen["trace_id"])
        self.assertEqual("m1", seen["model"])

    async def test_three_arg_chat_fn_still_works(self):
        async def chat(agent_id, message, trace_id=None):
            return {"success": True, "response": "ok"}

        r = await runner._call_chat(chat, "pi", "p", "task-r-t", "m", "t1")
        self.assertTrue(r["success"])


class _DbBase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0modelfb-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        tasks.set_context(agent_ids_fn=lambda: set())
        self._saved_conc = tiers.MAX_CONCURRENCY
        self._saved_sleep = tiers.LOOP_SLEEP_S
        tiers.MAX_CONCURRENCY = 1
        tiers.LOOP_SLEEP_S = 0.01
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tiers.MAX_CONCURRENCY = self._saved_conc
        tiers.LOOP_SLEEP_S = self._saved_sleep
        tasks.set_context(chat_fn=None)

    def _insert(self, run_id, tid, model="bad/model", tier="t2"):
        now = tasks._now()
        db.execute(
            "INSERT INTO tasks(run_id,task_id,trace_id,goal,prompt,agent_id,deps,status,"
            "verify_kind,attempts,tier,model_planned,created_at,updated_at)"
            " VALUES(?,?,?,?,?,?,?,'pending',?,?,?,?,?,?)",
            (run_id, tid, run_id, "g", "do it", "pi", "[]", "nonempty",
             tiers.MAX_AUTO_RETRY, tier, model, now, now))


class TestEndToEndModelFallback(_DbBase):
    def test_bad_model_is_swapped_then_task_succeeds(self):
        self._insert("r1", "t1")

        async def chat(agent_id, message, trace_id=None, model=None):
            if model == "bad/model":
                return {"success": False, "error": "unknown model: bad/model",
                        "model": "bad/model", "ms": 5}
            return {"success": True, "response": "ok-from-%s" % model, "model": model}

        tasks.set_context(chat_fn=chat)
        with mock.patch.object(tiers, "_catalog_snapshot",
                               return_value=({"bad/model", "good/model"}, True)), \
                mock.patch("tiercfg.parse_tiers",
                           return_value={"t1": ["good/model"],
                                         "t2": ["bad/model", "good/model"]}):
            asyncio.run(tasks._execute_run("r1"))
        row = db.query("SELECT * FROM tasks WHERE run_id='r1' AND task_id='t1'")[0]
        self.assertEqual("success", row["status"], row["error"] or "")
        self.assertEqual(1, row["model_fallbacks"])
        self.assertIn("bad/model", tiers.parse_models_tried(row["models_tried"]))
        self.assertEqual("good/model", row["model_planned"])
        ev = db.query("SELECT * FROM profile_events WHERE source='task_exec'"
                      " AND status='model_fallback'")
        self.assertTrue(ev, "自动换模型必须留痕（复盘要能回答换了什么）")
        self.assertEqual("good/model", json.loads(ev[0]["detail"])["to_model"])


class TestEndToEndChannelFallback(_DbBase):
    def test_no_api_key_switches_to_agent_default(self):
        """实测形态：pi 派 alibaba/* ⇒ `No API key found for vercel-ai-gateway`。"""
        self._insert("r2", "t1")

        async def chat(agent_id, message, trace_id=None, model=None):
            if model == "bad/model":
                return {"success": False,
                        "error": "exit:1: No API key found for vercel-ai-gateway.",
                        "model": "bad/model", "ms": 5}
            return {"success": True, "response": "ok-from-%s" % model, "model": model}

        tasks.set_context(chat_fn=chat)
        with mock.patch.object(tiers, "_catalog_snapshot",
                               return_value=({"bad/model", "good/model"}, True)), \
                mock.patch("tiercfg.parse_tiers",
                           return_value={"t1": ["good/model"],
                                         "t2": ["bad/model", "good/model"]}), \
                mock.patch.object(tiers, "_agent_default_model",
                                  return_value="agent/default"):
            asyncio.run(tasks._execute_run("r2"))
        row = db.query("SELECT * FROM tasks WHERE run_id='r2' AND task_id='t1'")[0]
        self.assertEqual("success", row["status"], row["error"] or "")
        self.assertEqual("agent/default", row["model_planned"],
                         "通道故障必须先回 agent 默认模型，不是同档换个 ID 再撞")
        self.assertEqual(1, row["model_fallbacks"])


class TestEndToEndCatalogMiss(_DbBase):
    def test_pi_model_not_found_switches_in_one_hop(self):
        """pi 首错 `Model "x" not found. Use --list-models`：换同档 ID 必再撞，必须一步回默认。"""
        self._insert("r3", "t1")

        async def chat(agent_id, message, trace_id=None, model=None):
            if model == "bad/model":
                return {"success": False,
                        "error": 'Error: Model "bad/model" not found. '
                                 'Use --list-models to see available models.',
                        "model": "bad/model", "ms": 5}
            return {"success": True, "response": "ok-from-%s" % model, "model": model}

        tasks.set_context(chat_fn=chat)
        with mock.patch.object(tiers, "_catalog_snapshot",
                               return_value=({"bad/model", "other/model"}, True)), \
                mock.patch("tiercfg.parse_tiers",
                           return_value={"t1": ["bad/model", "other/model"]}), \
                mock.patch.object(tiers, "_agent_default_model",
                                  return_value="agent/default"):
            asyncio.run(tasks._execute_run("r3"))
        row = db.query("SELECT * FROM tasks WHERE run_id='r3' AND task_id='t1'")[0]
        self.assertEqual("success", row["status"], row["error"] or "")
        self.assertEqual("agent/default", row["model_planned"],
                         "本地清单没有该 ID ⇒ 不许先试同档兄弟（那是可预见的第二次失败）")
        self.assertEqual(1, row["model_fallbacks"])


class TestCancellationKillsProcess(unittest.IsolatedAsyncioTestCase):
    """取消 = 杀进程组 + 摘登记。不杀 ⇒ 重试期间两个进程并发跑同一子任务。"""

    async def test_cancel_kills_group_and_unregisters(self):
        cli.SPECS["__cancel_probe__"] = {
            "bin": "/bin/sh",
            "argv": lambda p, m, x: ["-c", "sleep 30"],
            "clean": lambda out: (out, None),
            "model_path": "native",
        }
        self.addCleanup(cli.SPECS.pop, "__cancel_probe__", None)
        ad = cli.CliAgentAdapter(None, "__cancel_probe__")
        task = asyncio.create_task(ad.chat("x", tag="task-run1-t1"))
        pid = None
        for _ in range(100):
            await asyncio.sleep(0.05)
            live = [p for p in cli.live_processes() if p["tag"] == "task-run1-t1"]
            if live:
                pid = live[0]["pid"]
                break
        self.assertIsNotNone(pid, "子进程没有按 tag 登记 ⇒ 心跳永远采不到 pid")
        task.cancel()
        with self.assertRaises(asyncio.CancelledError):
            await task
        self.assertFalse([p for p in cli.live_processes() if p["tag"] == "task-run1-t1"],
                         "取消后登记表仍有残留")
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


if __name__ == "__main__":
    unittest.main()
