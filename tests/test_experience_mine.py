#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：经验挖掘（scripts/experience_mine.py）P0-1 切片 C（AMR「Dreaming」职责）。

【这条判据治什么】
经验库若只进不出，会退化成又一份「只增不减」的日志。Dreaming 的三件事是
**合并重复 / 删过期 / 解冲突** —— 本文件把三件事钉成**确定性计划**（谁退役、谁顶替、为什么），
再交给 `dataset.retire_experience` 落库（不物理删，留 `superseded_by` 追溯）。

【本文件守四条】
1. **三件事各有一类 reason**：`duplicate` / `conflict` / `expired`，互不混同。
2. **只退役旧的**：每组里保留最新的那条（按 `created_at`），其余退役并记 `superseded_by`。
3. **不同 key 不动**：不相关的经验不得被顺手退役。
4. **非会话模型一律 `openrouter/free`**（模型只用于「从轨迹抽取经验」，不在计划阶段调）。

分层口径（tests/tiers.py）：纯函数 + 临时 db。
"""
import pathlib
import sys
import unittest
import uuid

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "src"))

import db               # noqa: E402
import dataset          # noqa: E402
import experience_mine as em  # noqa: E402

NOW = "2026-10-15T00:00:00+00:00"


def _exp(situation, action, precondition="", created="2026-10-01T00:00:00+00:00",
         exp_id=None):
    return {"id": exp_id or uuid.uuid4().hex[:8], "kind": "pitfall",
            "situation": situation, "action": action, "precondition": precondition,
            "source_trace_ids": "[]", "created_at": created,
            "retired_at": None, "superseded_by": None}


class TestModelDiscipline(unittest.TestCase):
    def test_default_model_is_agnes(self):
        """2026-10-29 用户裁定「取消免费模型」⇒ 默认走本机 CCR 的 agnes。"""
        self.assertEqual(em.DEFAULT_MODEL, "agnes/agnes-2.5-flash")

    def test_auto_router_rejected(self):
        with self.assertRaises(ValueError):
            em.assert_free_model("openrouter/auto")


class TestNormKey(unittest.TestCase):
    def test_whitespace_and_case_insensitive(self):
        a = _exp("Disk  Full", "act", precondition="P")
        b = _exp("disk full", "act", precondition="p")
        self.assertEqual(em.norm_key(a), em.norm_key(b))

    def test_different_situation_differs(self):
        self.assertNotEqual(em.norm_key(_exp("a", "x")), em.norm_key(_exp("b", "x")))


class TestPlan(unittest.TestCase):
    def test_duplicate_pair_retires_older(self):
        old = _exp("disk full", "clean tmp", created="2026-10-01T00:00:00+00:00", exp_id="old1")
        new = _exp("disk full", "clean tmp", created="2026-10-10T00:00:00+00:00", exp_id="new1")
        p = em.plan([old, new], now=NOW)
        retires = {r["id"]: r for r in p["retire"]}
        self.assertIn("old1", retires)
        self.assertNotIn("new1", retires)
        self.assertEqual(retires["old1"]["reason"], "duplicate")
        self.assertEqual(retires["old1"]["superseded_by"], "new1")

    def test_conflicting_actions_retire_older_as_conflict(self):
        old = _exp("disk full", "rm -rf", created="2026-10-01T00:00:00+00:00", exp_id="old2")
        new = _exp("disk full", "clean tmp", created="2026-10-10T00:00:00+00:00", exp_id="new2")
        p = em.plan([old, new], now=NOW)
        retires = {r["id"]: r for r in p["retire"]}
        self.assertEqual(retires["old2"]["reason"], "conflict")
        self.assertEqual(retires["old2"]["superseded_by"], "new2")

    def test_expired_retires_by_age(self):
        stale = _exp("s", "a", created="2020-01-01T00:00:00+00:00", exp_id="stale1")
        p = em.plan([stale], now=NOW, expire_days=180)
        retires = {r["id"]: r for r in p["retire"]}
        self.assertEqual(retires["stale1"]["reason"], "expired")

    def test_unrelated_experiences_are_untouched(self):
        a = _exp("situation A", "x", exp_id="a1")
        b = _exp("situation B", "y", exp_id="b1")
        p = em.plan([a, b], now=NOW)
        self.assertEqual([], p["retire"])

    def test_already_retired_is_skipped(self):
        e = _exp("s", "a", exp_id="r1")
        e["retired_at"] = "2026-03-01T00:00:00+00:00"
        self.assertEqual([], em.plan([e], now=NOW)["retire"])


class _Base(unittest.TestCase):
    def setUp(self):
        import tempfile
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0expmine-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        dataset.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved


class TestApply(_Base):
    def test_apply_plan_retires_in_store(self):
        old = dataset.record_experience("pitfall", situation="disk full", action="clean")
        new = dataset.record_experience("pitfall", situation="disk full", action="clean")
        exps = dataset.list_experiences(active_only=False)
        p = em.plan(exps, now=NOW)
        em.apply_plan(p)
        live = {e["id"] for e in dataset.list_experiences()}
        self.assertEqual({new}, live)
        self.assertEqual(new, dataset.get_experience(old)["superseded_by"])


class TestExtract(unittest.TestCase):
    """从轨迹抽取经验（模型，free）：产物必须过解析 + 字段校验，否则不入库。"""

    def test_parse_valid_object(self):
        c = em.parse_experience('{"kind":"pitfall","situation":"S","action":"A","precondition":"P"}')
        self.assertEqual("S", c["situation"])
        self.assertEqual("A", c["action"])

    def test_parse_fenced_json(self):
        c = em.parse_experience('```json\n{"situation":"S","action":"A"}\n```')
        self.assertEqual("S", c["situation"])

    def test_parse_empty_object_is_none(self):
        self.assertIsNone(em.parse_experience("{}"))

    def test_parse_missing_fields_is_none(self):
        self.assertIsNone(em.parse_experience('{"situation":"S"}'))

    def test_parse_garbage_is_none(self):
        self.assertIsNone(em.parse_experience("no json here"))

    def test_extract_rejects_non_free_model(self):
        import asyncio
        with self.assertRaises(ValueError):
            asyncio.run(em.extract_trajectories([], model="openrouter/auto", chat_fn=None))

    def test_extract_records_experiences(self):
        import asyncio

        async def chat(model, prompt):
            return '{"kind":"pitfall","situation":"disk full","action":"clean tmp"}'
        trajs = [{"session_id": "tr1", "source": "hubself_tool", "events": [{"event": "shell_run"}]}]
        out = asyncio.run(em.extract_trajectories(trajs, chat_fn=chat, ds=_FakeDS()))
        self.assertEqual(1, len(out))
        self.assertEqual("disk full", out[0]["situation"])

    def test_extract_unparseable_yields_nothing(self):
        import asyncio

        async def chat(model, prompt):
            return "抱歉，我无法从这段轨迹中总结出经验。"
        trajs = [{"session_id": "tr1", "source": "x", "events": [{"event": "e"}]}]
        out = asyncio.run(em.extract_trajectories(trajs, chat_fn=chat, ds=_FakeDS()))
        self.assertEqual([], out)


class TestIncrementalExtract(_Base):
    """②自动化：抽取必须**增量** —— 已抽过的轨迹别再花钱抽一遍。"""

    def test_extracted_trace_ids_reads_source_trace_ids(self):
        dataset.record_experience("pitfall", situation="s", action="a",
                                  source_trace_ids=["tr1", "tr2"])
        self.assertEqual({"tr1", "tr2"}, em.extracted_trace_ids())

    def test_extracted_trace_ids_empty_when_none(self):
        self.assertEqual(set(), em.extracted_trace_ids())

    def test_extract_skips_already_done_traces(self):
        import asyncio
        dataset.record_experience("pitfall", situation="s", action="a",
                                  source_trace_ids=["tr1"])
        calls = []

        async def chat(model, prompt):
            calls.append(prompt)
            return '{"kind":"pitfall","situation":"new","action":"act"}'
        trajs = [{"session_id": "tr1", "source": "x", "events": [{"event": "e"}]},
                 {"session_id": "tr2", "source": "x", "events": [{"event": "e"}]}]
        out = asyncio.run(em.extract_trajectories(trajs, chat_fn=chat, limit=None))
        self.assertEqual(1, len(calls), "已抽过的 tr1 不该再调模型")
        self.assertEqual(1, len(out))
        self.assertEqual(["tr2"], out[0]["source_trace_ids"])

    def test_extract_limit_applies_after_skip(self):
        import asyncio

        async def chat(model, prompt):
            return '{"kind":"pitfall","situation":"s","action":"a"}'
        trajs = [{"session_id": "t%d" % i, "source": "x", "events": [{"event": "e"}]}
                 for i in range(5)]
        out = asyncio.run(em.extract_trajectories(trajs, chat_fn=chat, limit=2))
        self.assertEqual(2, len(out))


class _FakeDS:
    def __init__(self):
        self.rows = []

    def record_experience(self, kind, *, situation="", action="", precondition="",
                          source_trace_ids=None):
        eid = "e%d" % (len(self.rows) + 1)
        self.rows.append({"id": eid, "kind": kind, "situation": situation,
                          "action": action, "precondition": precondition,
                          "source_trace_ids": source_trace_ids})
        return eid


if __name__ == "__main__":
    unittest.main()