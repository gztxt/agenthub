#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：数据集真实评估（scripts/dataset_eval.py）—— D1/D13/D16 的落地链路。

【这条判据治什么】
「跑过一次评估」这件事本身必须**留痕且可比**：
- D13：先 `pin_eval`（钉版本）→ 评估 → 再 `bump_version`。**顺序不能反**，
  反了 `record_eval_result` 会因「评估陈旧」被自身闸门拒掉（那正是闸门的意义）。
- D16：逐题结果必须回写到 `dataset_item`，`badcases()` 才能捞出来。
- D1：结果要能给出「成功率」，而不是只报一个「跑完了」。

【本文件守四条】
1. 评估过的题都带 `last_verdict`（pass/fail/insufficient 之一）；未跑的题不带。
2. 失败/判不了的题进 badcase 池；pass 的不进。
3. 评估后版本前进，且**旧评估变成 stale**（这正是「比较期间不许改题集」的机械形态）。
4. 非会话模型必须 free（`openrouter/auto` 直接拒）。

分层口径（tests/tiers.py）：临时 db + 假 chat_fn，不连网、零 SKIP。
"""
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))
sys.path.insert(0, str(_REPO / "src"))

import dataset         # noqa: E402
import db              # noqa: E402
import dataset_eval as de  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0dseval-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        dataset.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved

    def _fixture(self, n=3):
        ds = dataset.create_dataset("d", grain="session")
        for i in range(n):
            dataset.add_item(ds, split="holdout", input="q%d" % i, grain_key={"k": i})
        return ds


class TestModelDiscipline(unittest.TestCase):
    def test_default_model_is_free(self):
        self.assertEqual(de.DEFAULT_MODEL, "openrouter/free")

    def test_auto_router_rejected(self):
        with self.assertRaises(ValueError):
            de.assert_free_model("openrouter/auto")


class TestVerdictMapping(unittest.TestCase):
    def test_nonempty_answer_passes(self):
        self.assertEqual("pass", de.verdict_for("ok")[0])

    def test_prompt_follows_kind(self):
        """题面与判据必须对得上：问「给结论」而判「JSON」是把模型当替罪羊。"""
        item = {"input": "do X"}
        self.assertIn("JSON 数组", de.build_prompt(item, "json_array"))
        self.assertNotIn("JSON 数组", de.build_prompt(item, "nonempty"))
        self.assertIn("do X", de.build_prompt(item, "json_array"))

    def test_empty_answer_is_not_pass(self):
        v, score = de.verdict_for("")
        self.assertNotEqual("pass", v)
        self.assertLess(score, 1.0)

    def test_missing_answer_is_insufficient(self):
        self.assertEqual("insufficient", de.verdict_for(None)[0])

    def test_unknown_kind_is_insufficient(self):
        self.assertEqual("insufficient", de.verdict_for("x", kind="no_such_kind")[0])


class TestRunEval(_Base):
    def test_eval_records_results_and_bumps_version(self):
        import asyncio
        ds = self._fixture(3)

        async def chat(model, prompt):
            return "answer for " + prompt
        out = asyncio.run(de.run_eval(ds, chat_fn=chat, limit=3))
        self.assertEqual(3, out["items"])
        self.assertEqual(3, out["passed"])
        self.assertEqual([], out["badcases"])
        self.assertEqual(2, out["version"], "评估后应产生首个 version bump")
        self.assertEqual([], dataset.badcases(ds))
        for it in dataset.list_items(ds, "holdout"):
            self.assertEqual("pass", it["last_verdict"])

    def test_failed_answers_land_in_badcases(self):
        import asyncio
        ds = self._fixture(2)

        async def chat(model, prompt):
            return "" if "q0" in prompt else "ok"          # 第一题恒空
        out = asyncio.run(de.run_eval(ds, chat_fn=chat, limit=2))
        self.assertEqual(1, out["items"] - out["passed"])
        bad = dataset.badcases(ds)
        self.assertEqual(1, len(bad))
        self.assertNotEqual("pass", bad[0]["last_verdict"])

    def test_eval_becomes_stale_after_bump(self):
        import asyncio
        ds = self._fixture(1)

        async def chat(model, prompt):
            return "ok"
        out = asyncio.run(de.run_eval(ds, chat_fn=chat, limit=1))
        self.assertTrue(dataset.eval_is_stale(out["eval_id"]),
                        "版本前进后，这次评估必须被判陈旧")

    def test_unmeasured_items_carry_no_verdict(self):
        """没跑到的题不许带 verdict —— 不能把「没测」当成「测坏了」。"""
        import asyncio
        ds = self._fixture(3)

        async def chat(model, prompt):
            return "ok"
        asyncio.run(de.run_eval(ds, chat_fn=chat, limit=1))
        untouched = [r for r in dataset.list_items(ds, "holdout") if r["last_verdict"] is None]
        self.assertEqual(2, len(untouched))

    def test_json_array_kind_fails_prose_answers(self):
        """可证伪的判据：要求 JSON 数组时，散文式回答应落 badcase。"""
        import asyncio
        ds = self._fixture(2)

        async def chat(model, prompt):
            return "我建议先看日志，然后重启服务。"        # 非 JSON
        out = asyncio.run(de.run_eval(ds, chat_fn=chat, limit=2, kind="json_array"))
        self.assertEqual(0, out["passed"])
        self.assertEqual(2, len(dataset.badcases(ds)))

    def test_non_free_model_rejected(self):
        import asyncio
        ds = self._fixture(1)
        with self.assertRaises(ValueError):
            asyncio.run(de.run_eval(ds, chat_fn=lambda *a: "x", model="openrouter/auto"))

    def test_unknown_dataset_rejected(self):
        import asyncio
        with self.assertRaises(KeyError):
            asyncio.run(de.run_eval("nope", chat_fn=lambda *a: "x"))


if __name__ == "__main__":
    unittest.main()