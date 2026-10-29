#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：全链路编排（拆解 → 选型 → 汇总 → **最多 2 轮**重拆）。

【本文件守六条】
1. **只有「不达标且原因变了」才许进下一轮**：全绿不收尾就是烧钱，原地打转更糟。
2. **2 轮是硬上限**：`TASK_MAX_ROUNDS` 默认 2，调用方传 5 也只能被**收紧**不能放宽。
3. **FAIL 签名相同 ⇒ 停**（连续两轮同一批失败 = 原地打转，转人工）。
4. **选型不成立不许报错**：非法模型降级为 None（用 agent 默认），非在册 agent 不校验，
   清单取不到则整路不校验 —— 三种「判不了」都不许伪造成「选了非法模型」。
5. **`model_choice` 与 `model` 是两个口径**：前者是意图、后者是回包观测，合成一列就再也
   分不清「选了没生效」与「网关换了」。
6. **并发重拆只能成一条**：占位靠 `child_run_id IS NULL` 的条件更新，不是靠时序自觉。

【红向自证（TestSourceInvariants）】纯函数测不出「护栏被搬走」：
判据写成对**活代码**的结构断言 —— 把 `_maybe_replan` 的调用删掉、把条件更新改成无条件、
把闸门挪到 LLM 之后，前五条仍可能全绿。所以那三条单独钉源码形状。

分层口径（tests/tiers.py）：临时 db + 假 chat_fn / 假 LLM，不起服务、不连网、零 SKIP。
"""
import asyncio
import json
import os
import pathlib
import re
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

# 必须在 modelcfg 被调用前置上：否则 catalog_ids() 会真打 :3456，测试变慢且依赖网关活着。
os.environ["HUB_MODEL_CATALOG"] = "off"
os.environ.setdefault("HUB_TEST_MODE", "1")

import db      # noqa: E402
import llm     # noqa: E402
import tasks   # noqa: E402


def _rows(*specs):
    """(task_id, status, error) → gate 需要的行形状。"""
    return [{"task_id": t, "status": s, "error": e} for t, s, e in specs]


class TestReplanGate(unittest.TestCase):
    """闸门：六种结论各自钉一条，判据顺序即优先级。"""

    def test_unfinished_blocks_before_anything(self):
        rows = _rows(("t1", "success", None), ("t2", "running", None))
        g = tasks.replan_gate(1, 2, rows)
        self.assertFalse(g["go"])
        self.assertEqual(g["kind"], "unfinished")

    def test_all_success_is_complete(self):
        rows = _rows(("t1", "success", None), ("t2", "success", None))
        g = tasks.replan_gate(1, 2, rows)
        self.assertFalse(g["go"])
        self.assertEqual(g["kind"], "complete")

    def test_round_cap_is_hard(self):
        rows = _rows(("t1", "failed", "boom"))
        g = tasks.replan_gate(2, 2, rows)
        self.assertFalse(g["go"])
        self.assertEqual(g["kind"], "max_rounds")

    def test_cap_of_one_never_replans(self):
        """max_rounds=1（= 关掉自动重拆）时第 1 轮就必须停。"""
        g = tasks.replan_gate(1, 1, _rows(("t1", "failed", "boom")))
        self.assertFalse(g["go"])
        self.assertEqual(g["kind"], "max_rounds")

    def test_budget_stops_before_retry(self):
        g = tasks.replan_gate(1, 2, _rows(("t1", "failed", "boom")),
                             budget_over="token 超预算 100")
        self.assertFalse(g["go"])
        self.assertEqual(g["kind"], "budget")

    def test_same_failure_signature_is_spinning(self):
        parent = _rows(("t1", "failed", "timeout 30s"))
        child = _rows(("t1", "failed", "timeout 30s"))
        g = tasks.replan_gate(2, 3, child, parent)
        self.assertFalse(g["go"])
        self.assertEqual(g["kind"], "same_failure")

    def test_changed_failure_goes_next_round(self):
        parent = _rows(("t1", "failed", "timeout 30s"))
        child = _rows(("t1", "failed", "missing input file"))
        g = tasks.replan_gate(1, 2, child, parent)
        self.assertTrue(g["go"])
        self.assertEqual(g["kind"], "go")

    def test_blocked_only_still_goes(self):
        """只有 blocked（上游失败传导）时也要重拆：那正说明上游该重做。"""
        g = tasks.replan_gate(1, 2, _rows(("t1", "failed", "x"), ("t2", "blocked", None)))
        self.assertTrue(g["go"])


class TestFailureSignature(unittest.TestCase):
    def test_order_insensitive(self):
        a = _rows(("t1", "failed", "e1"), ("t2", "failed", "e2"))
        b = _rows(("t2", "failed", "e2"), ("t1", "failed", "e1"))
        self.assertEqual(tasks.failure_signature(a), tasks.failure_signature(b))

    def test_different_error_different_sig(self):
        a = _rows(("t1", "failed", "e1"))
        b = _rows(("t1", "failed", "e2"))
        self.assertNotEqual(tasks.failure_signature(a), tasks.failure_signature(b))

    def test_blocked_not_counted(self):
        """blocked 是传导态，计入会让签名恒等于上一轮 ⇒ 误判原地打转。"""
        only_blocked = _rows(("t1", "blocked", "上游失败"))
        self.assertEqual(tasks.failure_signature(only_blocked), "")


class TestDagSelection(unittest.TestCase):
    """选型与难度：三条「判不了」的降级方向各不相同。"""

    KNOWN = {"claude", "jcode", "custom1"}
    MODELS = {"claude": {"alibaba/qwen3.8-max", "openrouter/openrouter/free"}}

    def _items(self, **kw):
        base = {"id": "t1", "task": "取证", "agent_id": "jcode"}
        base.update(kw)
        return [base]

    def test_valid_model_kept(self):
        out = tasks._validate_dag(self._items(agent_id="claude",
                                             model="alibaba/qwen3.8-max"),
                                  self.KNOWN, "jcode", self.MODELS)
        self.assertEqual(out[0]["model"], "alibaba/qwen3.8-max")

    def test_invalid_model_degrades_to_none_not_400(self):
        out = tasks._validate_dag(self._items(agent_id="claude", model="fake/model"),
                                  self.KNOWN, "jcode", self.MODELS)
        self.assertIsNone(out[0]["model"])

    def test_agent_outside_whitelist_passes_model_through(self):
        """custom1 不在 allowed_models 里 = 模型域在它自己那边，拿 CCR 清单判它会误杀。"""
        out = tasks._validate_dag(self._items(agent_id="custom1", model="its-own/llm"),
                                  self.KNOWN, "jcode", self.MODELS)
        self.assertEqual(out[0]["model"], "its-own/llm")

    def test_none_whitelist_means_no_check(self):
        out = tasks._validate_dag(self._items(agent_id="claude", model="whatever"),
                                  self.KNOWN, "jcode", None)
        self.assertEqual(out[0]["model"], "whatever")

    def test_difficulty_clamped(self):
        for raw, want in ((1, 1), (3, 3), (0, 1), (9, 3), ("2", 2), ("high", 2), (None, 2)):
            out = tasks._validate_dag(self._items(difficulty=raw), self.KNOWN, "jcode", None)
            self.assertEqual(out[0]["difficulty"], want, "difficulty=%r" % (raw,))

    def test_prefix_tolerance_but_no_general_stripping(self):
        self.assertTrue(tasks._model_allowed("ccr/alibaba/qwen3.8-max",
                                             {"alibaba/qwen3.8-max"}))
        self.assertFalse(tasks._model_allowed("openrouter/x", {"x"}),
                         "通用剥离会吃掉真 ID 的首段")

    def test_unknown_agent_still_normalized(self):
        out = tasks._validate_dag(self._items(agent_id="nope"), self.KNOWN, "jcode", None)
        self.assertEqual(out[0]["agent_id"], "jcode")


class TestSchemaAndMeta(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0rounds-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tasks.set_context(chat_fn=None)
        tasks.MAX_CONCURRENCY = 4
        tasks.LOOP_SLEEP_S = 1.5

    def test_task_runs_table_and_columns(self):
        cols = {r["name"] for r in db.query("PRAGMA table_info(task_runs)")}
        self.assertTrue({"run_id", "parent_run_id", "round", "auto_rounds",
                         "child_run_id", "stop_reason"} <= cols)
        tcols = {r["name"] for r in db.query("PRAGMA table_info(tasks)")}
        self.assertIn("model_choice", tcols)
        self.assertIn("difficulty", tcols)

    def test_legacy_run_row_is_created_lazily(self):
        run = tasks._create_run("目标 x", [{"id": "t1", "task": "做", "agent_id": "jcode",
                                           "deps": [], "difficulty": 2, "model": None}],
                                verify_kind="nonempty", budget_ms=None, budget_tokens=None)
        db.execute("DELETE FROM task_runs WHERE run_id=?", (run,))
        # 存量 run（无 task_runs 行）不得因为新表出现就获得自动重拆
        meta = tasks.run_meta(run)
        self.assertEqual(meta["round"], 1)
        self.assertEqual(int(meta["auto_rounds"]), 0)
        meta = tasks._ensure_run_row(run)
        self.assertEqual(meta["origin"], "legacy")
        self.assertEqual(int(meta["auto_rounds"]), 0)


class TestAutoReplan(unittest.TestCase):
    """端到端（L0）：收敛 → 自动重拆一轮 → 第二轮收敛到上限。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0auto-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        tasks.ensure_schema()
        self._saved_conc = tasks.MAX_CONCURRENCY
        self._saved_sleep = tasks.LOOP_SLEEP_S
        self._saved_llm = (llm.configured, llm.chat_tools_loop)
        tasks.MAX_CONCURRENCY = 1
        tasks.LOOP_SLEEP_S = 0.01
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        tasks.MAX_CONCURRENCY = self._saved_conc
        tasks.LOOP_SLEEP_S = self._saved_sleep
        llm.configured, llm.chat_tools_loop = self._saved_llm
        tasks.set_context(chat_fn=None)

    def _fake_chat(self):
        async def chat(agent_id, prompt, tag=None, model=None):
            return {"success": True, "response": "apple", "model": "m1",
                    "usage": {"total_tokens": 1}}
        return chat

    def _stub_llm(self, answer):
        llm.configured = lambda: True

        async def fake_loop(messages, tools, stop, max_rounds=1):
            return answer, {}
        llm.chat_tools_loop = fake_loop

    def _mk_run(self, auto_rounds):
        run = tasks._create_run(
            "目标：产出 banana", [{"id": "t1", "task": "写 banan", "agent_id": "jcode",
                                   "deps": [], "difficulty": 1, "model": None}],
            verify_kind="contains", budget_ms=None, budget_tokens=None,
            auto_rounds=auto_rounds)
        db.execute("UPDATE tasks SET verify_expect='banana', attempts=? WHERE run_id=?",
                   (tasks.MAX_AUTO_RETRY, run))
        return run

    def test_auto_rounds_creates_second_round_then_stops(self):
        self._stub_llm('[{"id":"r1","task":"重做：补 banana","agent_id":"jcode",'
                       '"difficulty":1,"deps":[]}]')
        tasks.set_context(chat_fn=self._fake_chat())
        run = self._mk_run(auto_rounds=True)

        async def go():
            await tasks._execute_run(run)
            for _ in range(100):
                await asyncio.sleep(0.02)
                meta = tasks.run_meta(run)
                if meta.get("child_run_id"):
                    return meta
            return tasks.run_meta(run)

        meta = asyncio.run(go())
        child = meta.get("child_run_id")
        self.assertTrue(child, "自动重拆未生成第二轮")
        cm = tasks.run_meta(child)
        self.assertEqual(int(cm["round"]), 2)
        self.assertEqual(cm["parent_run_id"], run)
        self.assertEqual(cm["origin"], "auto")
        rows = db.query("SELECT * FROM tasks WHERE run_id=?", (child,))
        self.assertEqual(len(rows), 1)
        self.assertTrue(rows[0]["prompt"].startswith("重做："))

    def test_second_round_stops_at_cap(self):
        self._stub_llm('[{"id":"r1","task":"重做：补 banana","agent_id":"jcode",'
                       '"difficulty":1,"deps":[]}]')
        tasks.set_context(chat_fn=self._fake_chat())
        run = self._mk_run(auto_rounds=True)

        async def go():
            await tasks._execute_run(run)
            for _ in range(100):
                await asyncio.sleep(0.02)
                if tasks.run_meta(run).get("child_run_id"):
                    break
            child = tasks.run_meta(run)["child_run_id"]
            await tasks._execute_run(child)      # 第二轮（直接跑，不靠 runner 时序）
            for _ in range(100):
                await asyncio.sleep(0.02)
                if tasks.run_meta(child).get("stop_reason"):
                    break
            return tasks.run_meta(child)

        cm = asyncio.run(go())
        self.assertEqual(cm["stop_reason"], "max_rounds")
        self.assertIsNone(cm["child_run_id"], "第 2 轮不许再拆第 3 轮")

    def test_auto_rounds_off_never_replans(self):
        self._stub_llm('[{"id":"r1","task":"不该被用到","agent_id":"jcode","deps":[]}]')
        tasks.set_context(chat_fn=self._fake_chat())
        run = self._mk_run(auto_rounds=False)

        async def go():
            await tasks._execute_run(run)
            await asyncio.sleep(0.1)
            return tasks.run_meta(run)

        meta = asyncio.run(go())
        self.assertIsNone(meta.get("child_run_id"))
        self.assertIsNone(meta.get("stop_reason"))

    def test_llm_failure_does_not_touch_parent_conclusion(self):
        """重拆失败只记 stop_reason，不改上一轮已收敛的结论，也不生成子 run。"""
        async def boom(messages, tools, stop, max_rounds=1):
            raise RuntimeError("gateway 502")
        llm.configured = lambda: True
        llm.chat_tools_loop = boom
        tasks.set_context(chat_fn=self._fake_chat())
        run = self._mk_run(auto_rounds=True)

        async def go():
            await tasks._execute_run(run)        # 先收敛（子任务失败）
            for _ in range(100):
                await asyncio.sleep(0.02)
                if tasks.run_meta(run).get("stop_reason"):
                    return tasks.run_meta(run)
            return tasks.run_meta(run)

        meta = asyncio.run(go())
        self.assertTrue(str(meta.get("stop_reason")).startswith("replan_error"))
        self.assertIsNone(meta.get("child_run_id"))


class TestMcpFacade(unittest.TestCase):
    """MCP 入口：**默认不花钱**是这条链最难保住的性质。"""

    def setUp(self):
        import hubmcp
        self.hm = hubmcp
        self.seen = {}
        self._saved = (hubmcp._post, hubmcp._get)

        def fake_post(path, body, **params):
            self.seen.update({"path": path, "body": body})
            return {"run_id": "r1", "tasks": [{"id": "t1", "agent_id": "jcode",
                                               "difficulty": 2, "model": None,
                                               "deps": [], "task": "做"}],
                    "dry_run": bool(body.get("dry_run")), "round": 1,
                    "max_rounds": 2, "auto_rounds": bool(body.get("auto_rounds")),
                    "running": bool(body.get("auto_run"))}

        def fake_get(path, **params):
            self.seen.update({"path": path})
            return {"run_id": "r1", "goal": "g", "run": {"round": 1, "stop_reason": None},
                    "max_rounds": 2, "tasks": []}

        hubmcp._post, hubmcp._get = fake_post, fake_get

    def tearDown(self):
        self.hm._post, self.hm._get = self._saved

    def test_default_is_preview_only(self):
        out = json.loads(self.hm.hub_dispatch(goal="整理日志"))
        self.assertEqual(self.seen["path"], "/api/tasks/decompose")
        self.assertTrue(self.seen["body"]["dry_run"])
        self.assertFalse(self.seen["body"]["auto_run"])
        self.assertFalse(self.seen["body"]["auto_rounds"])
        self.assertTrue(out["dry_run"])

    def test_confirm_enables_execution_and_rounds(self):
        json.loads(self.hm.hub_dispatch(goal="整理日志", confirm=True, auto_rounds=True))
        b = self.seen["body"]
        self.assertFalse(b["dry_run"])
        self.assertTrue(b["auto_run"])
        self.assertTrue(b["auto_rounds"])

    def test_auto_rounds_without_confirm_stays_off(self):
        """`auto_rounds=True` 但没确认 ⇒ **不许**变成「自动跑两轮」。"""
        json.loads(self.hm.hub_dispatch(goal="整理日志", auto_rounds=True))
        self.assertFalse(self.seen["body"]["auto_rounds"])
        self.assertTrue(self.seen["body"]["dry_run"])

    def test_status_and_replan_paths(self):
        json.loads(self.hm.hub_run_status("abc123"))
        self.assertEqual(self.seen["path"], "/api/tasks/abc123")
        self.hm.hub_replan("abc123", max_rounds=5)
        self.assertEqual(self.seen["path"], "/api/tasks/abc123/replan")
        self.assertEqual(self.seen["body"]["max_rounds"], 5)


class TestSourceInvariants(unittest.TestCase):
    """把「护栏」钉在源码形状上 —— 纯函数全绿也挡不住有人把它挪走。"""

    @classmethod
    def setUpClass(cls):
        cls.src = (_REPO / "src" / "tasks.py").read_text(encoding="utf-8")
        # 去 docstring，只量会被执行的代码（同 test_task_handoff_l0 的口径）
        cls.code = re.sub(r'""".*?"""', "", cls.src, flags=re.S)

    def test_maybe_replan_called_on_finish(self):
        i_fin = self.code.index("if finished:")
        i_call = self.code.index("await _maybe_replan(run_id)", i_fin)
        # 用「行首 16 空格 + return」定位真正的 return 语句：注释里也出现了 return 这个词
        i_ret = self.code.index("\n                return", i_fin)
        self.assertLess(i_call, i_ret, "收敛分支必须在 return 之前接下一轮")

    def test_child_reservation_is_conditional(self):
        self.assertIn("AND child_run_id IS NULL", self.code,
                      "占位必须靠条件更新，否则并发重拆会生成两条子 run")

    def test_gate_runs_before_llm(self):
        i_gate = self.code.index("gate = replan_gate(")
        i_llm = self.code.index("llm.chat_tools_loop(", i_gate)
        self.assertLess(i_gate, i_llm, "先判闸门再问 LLM：反了会在不该花钱时花钱")

    def test_default_round_cap_is_two(self):
        self.assertIn('TASK_MAX_ROUNDS", "2"', self.code)

    def test_round_cap_only_tightens(self):
        self.assertIn("min(int(max_rounds), MAX_ROUNDS)", self.code)


class TestWiringAttrsResolvable(unittest.TestCase):
    """装配层属性必须真实存在（2026-10-27 实弹 500 的回归钉）。

    事故：`main.py` 的 `agent_ids_fn` 写的是 `appstate.discovery.all_configs()`，
    而 `AgentDiscovery` 从来没有这个方法。单测全程注入假 ctx/假 chat_fn，走的
    是 `_ctx["agent_ids_fn"]` 这个函数对象，**装配那一行从未被执行** ⇒ 33 例全绿、
    线上 `POST /api/tasks/decompose` 必 500。这类 bug 只有「拿真类校验源码里引用的
    属性名」能挡住，任何注入式测试都是假绿。
    """

    def test_discovery_attrs_used_by_main_exist(self):
        import discovery
        src = (_REPO / "src" / "main.py").read_text(encoding="utf-8")
        used = set(re.findall(r"appstate\.discovery\.([A-Za-z_]\w*)", src))
        self.assertTrue(used, "main.py 里至少要引用一次 discovery，否则本测试失去意义")
        missing = sorted(a for a in used if not hasattr(discovery.AgentDiscovery, a))
        self.assertEqual(missing, [],
                         "main.py 引用了 AgentDiscovery 不存在的方法：%s" % missing)

    def test_agent_ids_wiring_uses_dispatchable_pool(self):
        src = (_REPO / "src" / "main.py").read_text(encoding="utf-8")
        i = src.index("agent_ids_fn=")
        block = src[i:i + 200]
        self.assertIn("dispatchable_ids", block,
                      "派发目标必须取 registry.dispatchable_ids()（可执行池唯一口径）")
        self.assertNotIn("all_profiles", block,
                         "菜单 ≠ 可执行池：20 张卡只有 claude/jcode 能真跑 chat()")


class TestDispatchablePool(unittest.TestCase):
    """选型池必须只含真能干活的 agent（2026-10-27 实跑 239b4d553a 的回归钉）。

    事故：池子取全量画像 ⇒ Leader 抽中 dsh ⇒ `agent dsh 无可用适配器` ⇒ 整轮 0 产出。
    """

    def setUp(self):
        import registry
        self.registry = registry
        self._bak = dict(registry._adapters)
        registry._adapters.clear()
        registry._adapters.update({"claude": object(), "jcode": object(),
                                   "pi": object(), "tdai": object()})

    def tearDown(self):
        self.registry._adapters.clear()
        self.registry._adapters.update(self._bak)

    def test_stub_adapters_excluded(self):
        ids = self.registry.dispatchable_ids()
        self.assertIn("claude", ids)
        self.assertIn("jcode", ids)
        self.assertIn("pi", ids, "v0.13.175 起 pi 是 CLI 执行者（pi -p 实测可用）")
        self.assertNotIn("tdai", ids, "TDAI 是记忆中心，不是对话 Agent")

    def test_unavailable_cli_excluded(self):
        """注册 ≠ 可用：二进制不在时必须挡在池外，别让 Leader 抽中注定失败的任务。"""
        class Dead:
            def available(self):
                return False, "binary_not_found: codex"
        self.registry._adapters["codex"] = Dead()
        ids = self.registry.dispatchable_ids()
        self.assertNotIn("codex", ids)


if __name__ == "__main__":
    unittest.main()
