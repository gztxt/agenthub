#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：数据集管线（scripts/dataset_pipeline.py）P0-1 切片 B。

【这条判据治什么】
管线要「**声明式** + 确定性优先」：过滤/采样/切分都必须是**可复现的纯函数**，
模型只准出现在**最后一步**（调模型 = 不可复现 + 花钱 ⇒ 不能混进地基）。
手册 D1/D3/D7 的落点：固定任务构成、确定性过滤在前、采样可复现、字段映射显式。

【本文件守五条】
1. **确定性**：同输入 + 同 seed ⇒ 逐字相同；采样与输入顺序无关。
2. **模型在最后**：`build_items()` **不得**调模型（产物 `output` 为空）；
   只有 `enrich()` 调，且**非会话一律 `openrouter/free`**（`openrouter/auto` 直接拒）。
3. **切分稳定**：同一 `grain_hash` 永远落同一 split（否则重跑会把 holdout 洗进 train）。
4. **过滤是纯函数**：无事件的轨迹、来源不在白名单的轨迹被剔除。
5. 真调被测代码（不起服务、不连网、零 SKIP）。

分层口径（tests/tiers.py）：纯函数 + 假 chat_fn。
"""
import asyncio
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "src"))

import dataset_pipeline as dp  # noqa: E402
import hook                    # noqa: E402

TRAJS = [
    {"session_id": "s1", "source": "pi", "cwd": "/w",
     "events": [{"event": "session_usage", "usage": {"input_tokens": 10}}]},
    {"session_id": "s2", "source": "claude", "cwd": "/w",
     "events": [{"event": "session_usage", "usage": {"input_tokens": 20}}]},
    {"session_id": "s3", "source": "pi", "cwd": "/w", "events": []},          # 无事件 ⇒ 剔
    {"session_id": "s4", "source": "unknown-cli", "cwd": "/w",
     "events": [{"event": "hook"}]},                                          # 来源不在白名单 ⇒ 剔
]

SPEC = {"sources": ["pi", "claude"], "require_events": True,
        "limit": 10, "seed": 7, "holdout_frac": 0.5}


class TestModelDiscipline(unittest.TestCase):
    def test_default_model_is_free_router(self):
        self.assertEqual(dp.DEFAULT_MODEL, "openrouter/free")

    def test_auto_router_is_rejected(self):
        with self.assertRaises(ValueError):
            dp.assert_free_model("openrouter/auto")

    def test_paid_model_is_rejected(self):
        with self.assertRaises(ValueError):
            dp.assert_free_model("alibaba/deepseek-v4.1-flash")

    def test_free_models_are_accepted(self):
        dp.assert_free_model("openrouter/free")
        dp.assert_free_model("nvidia/nemotron-3-super-120b-a12b:free")

    def test_ccr_form_of_free_router_is_accepted(self):
        """工作区军规：**经 CCR 时写 `openrouter/openrouter/free`** —— 这种写法也得认。"""
        dp.assert_free_model("openrouter/openrouter/free")


class TestBuildItemsDeterministic(unittest.TestCase):
    def test_filters_out_empty_and_unknown_source(self):
        items = dp.build_items(TRAJS, SPEC)
        sids = {it["session_id"] for it in items}
        self.assertEqual({"s1", "s2"}, sids)

    def test_same_input_same_seed_is_identical(self):
        a = dp.build_items(TRAJS, SPEC)
        b = dp.build_items(TRAJS, SPEC)
        self.assertEqual(a, b)

    def test_input_order_does_not_matter(self):
        a = dp.build_items(TRAJS, SPEC)
        b = dp.build_items(list(reversed(TRAJS)), SPEC)
        self.assertEqual(a, b, "采样/切分不得依赖输入顺序")

    def test_build_items_never_calls_model(self):
        """模型只准在最后一步：build 阶段产物 output 必须为空。"""
        for it in dp.build_items(TRAJS, SPEC):
            self.assertEqual("", it["output"])

    def test_build_items_emits_schema_tagged_trajectory(self):
        """D5：管线产出的轨迹必须已是统一 schema（不是原始 events 堆）。"""
        import json
        import dataset
        it = dp.build_items(TRAJS, SPEC)[0]
        d = json.loads(it["trajectory"])
        self.assertEqual(dataset.TRAJECTORY_SCHEMA, d["schema"])
        self.assertEqual([], dataset.validate_trajectory(d))

    def test_item_carries_explicit_field_mapping(self):
        it = dp.build_items(TRAJS, SPEC)[0]
        for k in ("input", "output", "trajectory", "trace_id", "session_id",
                  "source_ref", "grain_hash", "split"):
            self.assertIn(k, it)
        self.assertEqual(it["trace_id"], it["session_id"])


class TestSampling(unittest.TestCase):
    def test_sample_is_seeded_and_stable(self):
        items = dp.build_items(TRAJS, SPEC)
        self.assertEqual(dp.sample(items, 1, seed=3), dp.sample(items, 1, seed=3))

    def test_sample_limit_respected(self):
        items = dp.build_items(TRAJS, SPEC)
        self.assertLessEqual(len(dp.sample(items, 1, seed=3)), 1)


class TestSplitAssignment(unittest.TestCase):
    def test_split_is_stable_for_same_grain(self):
        gh = "abcdef0123456789"
        self.assertEqual(dp.assign_split(gh, 0.5), dp.assign_split(gh, 0.5))

    def test_holdout_fraction_is_roughly_respected(self):
        hs = [format(i * 7919, "x") for i in range(1000)]
        n = sum(1 for h in hs if dp.assign_split(h, 0.2) == "holdout")
        self.assertTrue(120 <= n <= 280, "holdout 占比偏离预期：%d/1000" % n)


class TestTrajectoryAggregation(unittest.TestCase):
    """hook 轨迹级聚合（P0-1）：按 (source, session_id) 分组，事件保序。"""

    def test_groups_by_session_and_keeps_event_order(self):
        rows = [
            {"source": "pi", "session_id": "s1", "cwd": "/w", "event": "a",
             "payload": "{}", "usage_scope": None, "created_at": "t1"},
            {"source": "pi", "session_id": "s1", "cwd": "/w", "event": "b",
             "payload": "{}", "usage_scope": None, "created_at": "t2"},
            {"source": "claude", "session_id": "s1", "cwd": "/x", "event": "a",
             "payload": "{}", "usage_scope": None, "created_at": "t1"},
        ]
        trajs = hook.build_trajectories(rows)
        self.assertEqual(2, len(trajs))
        pi = [t for t in trajs if t["source"] == "pi"][0]
        self.assertEqual(["a", "b"], [e["event"] for e in pi["events"]])

    def test_empty_input_yields_no_trajectories(self):
        self.assertEqual([], hook.build_trajectories([]))


class TestTraceAggregation(unittest.TestCase):
    """profile_events → 轨迹（真实数据源）：按 trace_id 分组，无 trace_id 不成轨迹。"""

    def test_groups_by_trace_id(self):
        rows = [
            {"source": "hubself_tool", "trace_id": "tr1", "subject": "shell_run",
             "status": "success", "duration_ms": 10, "detail": None, "created_at": "t1"},
            {"source": "hubself_tool", "trace_id": "tr1", "subject": "list_dir",
             "status": "success", "duration_ms": 5, "detail": None, "created_at": "t2"},
            {"source": "rest", "trace_id": None, "subject": "mem.search",
             "status": "success", "duration_ms": 3, "detail": None, "created_at": "t3"},
        ]
        traces = hook.build_traces(rows)
        self.assertEqual(1, len(traces))
        self.assertEqual("tr1", traces[0]["session_id"])
        self.assertEqual(["shell_run", "list_dir"], [e["event"] for e in traces[0]["events"]])

    def test_rows_without_trace_id_are_dropped(self):
        self.assertEqual([], hook.build_traces([{"source": "rest", "trace_id": ""}]))

    def test_empty(self):
        self.assertEqual([], hook.build_traces([]))


class TestEnrich(unittest.TestCase):
    def test_enrich_sets_outputs_with_free_model(self):
        items = dp.build_items(TRAJS, SPEC)
        calls = []

        async def chat(model, prompt):
            calls.append((model, prompt))
            return "answer for " + prompt
        out = asyncio.run(dp.enrich(items, model=dp.DEFAULT_MODEL, chat_fn=chat))
        self.assertEqual(len(items), len(calls))
        for it in out:
            self.assertTrue(it["output"].startswith("answer for "))

    def test_enrich_rejects_non_free_model(self):
        items = dp.build_items(TRAJS, SPEC)
        with self.assertRaises(ValueError):
            asyncio.run(dp.enrich(items, model="openrouter/auto", chat_fn=lambda *a: "x"))


if __name__ == "__main__":
    unittest.main()