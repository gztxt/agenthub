#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：judge 的概率闸（`remote_judge.apply_gate`，2026-10-30）。

## 为什么要有它（判例式的反面）

`choice` 的 `probabilities` 被归一化成和 = 1 ⇒ **必然存在 argmax**。三个选项
0.40/0.35/0.25 时 `choice="第一个"` 读起来像「模型选了它」，实际是一次掷硬币；
`noul=0.52` 同罪。下游两条链都在拿这个 argmax 当真：`vitals` 把 `choice` 直接映射成
已安装/未安装，`/relevant` 把 `choice` 当「选中了哪个技能」。

## 测什么

1. **阈值判定**：choice 取各选项最大、noul 取 `max(p,1-p)`；分数恰好等于阈值算过。
2. **判不出必须清决策字段**：只加 `decided=false` 不清值 = 闸门不存在。
3. **幂等**：已过闸的答案再过一次逐字段相同（缓存读回时会再过一次）。
4. **score 不适用**：`decided=None`，不给它编假刻度。
5. **闸在门面、不在原始层**：`local_judge.judge()` 过闸，`_judge_raw()` 不过 ——
   这样缓存里存的是原始分布，改 `JUDGE_MIN_P` 后旧缓存按新阈值重新裁决。
6. **消费端兜底**：choice 被清空后 `vitals.present_from_evidence` 退回硬证据/unknown，
   不会把 0.4 的掷硬币当成「已安装」。
"""
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import remote_judge  # noqa: E402
import local_judge  # noqa: E402
import vitals  # noqa: E402


def _choice(probs):
    return {"type": "choice", "probabilities": probs, "choice": max(probs, key=probs.get),
            "confidence": 0.9, "reasoning": "fake"}


class TestMaxP(unittest.TestCase):
    def test_choice_max_over_options(self):
        self.assertAlmostEqual(
            remote_judge._max_p(_choice({"a": 0.7, "b": 0.2, "c": 0.1})), 0.7)

    def test_noul_uses_complement(self):
        """noul 是 P(是)：p=0.1 说明「否」有 0.9，把握不小 ⇒ max_p=0.9。"""
        self.assertAlmostEqual(remote_judge._max_p({"type": "noul", "noul": 0.1}), 0.9)
        self.assertAlmostEqual(remote_judge._max_p({"type": "noul", "noul": 0.9}), 0.9)

    def test_score_has_no_probability(self):
        self.assertIsNone(remote_judge._max_p({"type": "score", "score": 0.8}))

    def test_missing_or_garbage_is_none_not_zero(self):
        """读不出 ≠ 判 0：缺字段必须回 None，不能被补成 0 再当成「模型很确定地说否」。"""
        self.assertIsNone(remote_judge._max_p({"type": "noul"}))
        self.assertIsNone(remote_judge._max_p({"type": "choice", "probabilities": {}}))
        self.assertIsNone(remote_judge._max_p({"type": "choice",
                                               "probabilities": {"a": "x"}}))


class TestGate(unittest.TestCase):
    def test_below_threshold_clears_decision(self):
        out = remote_judge.apply_gate({"q": _choice({"a": 0.40, "b": 0.35, "c": 0.25})},
                                      threshold=0.6)
        a = out["q"]
        self.assertFalse(a["decided"])
        self.assertEqual(a["gate"]["result"], "undecided")
        self.assertEqual(a["gate"]["threshold"], 0.6)
        self.assertIsNone(a["choice"], "闸门不清 choice 就等于没接")
        self.assertEqual(a["would_choose"], "a", "原始 argmax 必须留档供复核")
        self.assertAlmostEqual(a["max_p"], 0.40)
        # 分布不动：复核的人要能看到模型原本怎么分布的
        self.assertAlmostEqual(a["probabilities"]["a"], 0.40)

    def test_above_threshold_keeps_decision(self):
        out = remote_judge.apply_gate({"q": _choice({"a": 0.8, "b": 0.2})}, threshold=0.6)
        self.assertTrue(out["q"]["decided"])
        self.assertEqual(out["q"]["choice"], "a")
        self.assertNotIn("would_choose", out["q"])

    def test_exactly_at_threshold_passes(self):
        out = remote_judge.apply_gate({"q": _choice({"a": 0.6, "b": 0.4})}, threshold=0.6)
        self.assertTrue(out["q"]["decided"])

    def test_noul_coin_flip_is_undecided(self):
        out = remote_judge.apply_gate({"q": {"type": "noul", "noul": 0.52}},
                                      threshold=0.6)
        a = out["q"]
        self.assertFalse(a["decided"])
        self.assertIsNone(a["noul"])
        self.assertAlmostEqual(a["p_model"], 0.52)
        self.assertAlmostEqual(a["max_p"], 0.52)

    def test_noul_strong_negative_is_decided(self):
        out = remote_judge.apply_gate({"q": {"type": "noul", "noul": 0.05}},
                                      threshold=0.6)
        self.assertTrue(out["q"]["decided"])
        self.assertAlmostEqual(out["q"]["noul"], 0.05, msg="强否就是判出来了，不能被清空")

    def test_score_is_not_applicable(self):
        out = remote_judge.apply_gate({"q": {"type": "score", "score": 0.4,
                                             "confidence": 0.9}}, threshold=0.6)
        self.assertIsNone(out["q"]["decided"], "score 没有最大概率 ⇒ n/a，不是 True")
        self.assertEqual(out["q"]["gate"]["result"], "n/a")
        self.assertAlmostEqual(out["q"]["score"], 0.4)

    def test_idempotent(self):
        src = {"q": _choice({"a": 0.3, "b": 0.7}),
               "r": {"type": "noul", "noul": 0.5},
               "s": {"type": "score", "score": 0.5}}
        once = remote_judge.apply_gate(src, threshold=0.6)
        twice = remote_judge.apply_gate(once, threshold=0.6)
        self.assertEqual(once, twice, "缓存读回会再过一次闸，不幂等就会把 p_model 洗成 None")

    def test_threshold_is_configurable(self):
        a = remote_judge.apply_gate({"q": _choice({"a": 0.7, "b": 0.3})}, threshold=0.9)
        self.assertFalse(a["q"]["decided"])
        self.assertIsNone(a["q"]["choice"])

    def test_empty_and_non_dict_answers_pass_through(self):
        self.assertEqual(remote_judge.apply_gate({}), {})
        out = remote_judge.apply_gate({"q": "raw"})
        self.assertEqual(out["q"], "raw")


class TestFacadeWiring(unittest.TestCase):
    """闸必须在门面这一层：原始层保持原样，缓存里存的是原始分布。"""

    def _raw(self, probs):
        return {"answers": {"q": _choice(probs)}, "model": "fake",
                "usage": {"calls": 1}, "elapsed_ms": 1,
                "cache": {"hit": False}}

    def test_judge_applies_gate(self):
        with mock.patch.object(local_judge, "_judge_raw",
                               return_value=self._raw({"a": 0.4, "b": 0.6})):
            out = local_judge.judge({"q": {"type": "choice", "instructions": "x"}})
        self.assertTrue(out["answers"]["q"]["decided"])
        self.assertEqual(out["answers"]["q"]["choice"], "b")

    def test_raw_layer_does_not_gate(self):
        """`_judge_raw` 若也过闸，缓存里就存了「已清空 choice」的副本 ⇒ 调阈值不可逆。"""
        with mock.patch.object(remote_judge, "judge",
                               return_value={"answers": {"q": _choice({"a": 0.4, "b": 0.6})},
                                             "model": "fake", "usage": {}}):
            out = local_judge._judge_raw({"q": {"type": "choice", "instructions": "x"}},
                                         state="s", timeout_s=5, use_cache=False)
        self.assertNotIn("decided", out["answers"]["q"])

    def test_regating_after_threshold_change_uses_raw(self):
        """改阈值不用清缓存：raw 里 probabilities 还在，重新过闸就能改判。"""
        raw = self._raw({"a": 0.7, "b": 0.3})
        with mock.patch.object(local_judge, "_judge_raw", return_value=raw):
            ok = local_judge.judge({"q": {"type": "choice", "instructions": "x"}})
        with mock.patch.object(local_judge, "_judge_raw", return_value=raw), \
                mock.patch.object(remote_judge, "MIN_P", 0.9):
            strict = local_judge.judge({"q": {"type": "choice", "instructions": "x"}})
        self.assertTrue(ok["answers"]["q"]["decided"])
        self.assertFalse(strict["answers"]["q"]["decided"])
        self.assertIsNone(strict["answers"]["q"]["choice"])


class TestConsumerFallback(unittest.TestCase):
    def test_nulled_choice_falls_back_to_evidence(self):
        """vitals 拿 None 走硬证据；证据也模糊则 unknown —— 不许把掷硬币当「已安装」。"""
        self.assertEqual(vitals.present_from_evidence({}, None), (None, "unknown"))
        self.assertEqual(vitals.present_from_evidence({"resolved": False}, None),
                         (0.0, "rule"))
        self.assertEqual(vitals.present_from_evidence({"resolved": True}, None),
                         (1.0, "rule"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
