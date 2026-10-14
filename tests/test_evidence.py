#!/usr/bin/env python3
"""L0 hermetic：证据分层与研判原语（src/evidence.py，P0-4）。

分层口径（tests/tiers.py）：零宿主依赖 —— 纯函数模块，不碰 db、不起服务、不打网络、
**不允许 SKIP**。

本闸门守的**唯一不变量**（其余都是它的推论）：

  > 证据缺失、观测失败、规则引用了不存在的事实、规则抛异常、规则明确返回「不知道」
  > —— 一律 `insufficient`，**永不**退化成 `dismissed`（=「没发现问题」）
  > 或 `confirmed`（=「已确认」）。

为什么值得单独一层闸门：本仓已经反复栽在「把 SKIP 当 PASS」上（L0 偷偷 skipTest、
收集器收不到的 pytest 风格闸门报全绿、探针全挂却判「一切正常」）。这句话在文档里写过很多次，
每次都得靠人记得；本模块把每一类「判不了」都做成**能确定性造红**的用例。
"""
import json
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import evidence  # noqa: E402


def _sig(key="s1", facts=("f1",), severity="info", rule="test"):
    return evidence.Signal(key=key, claim="命题 " + key, facts=tuple(facts),
                           severity=severity, rule=rule)


def _facts(*pairs):
    return [evidence.Fact(key=k, value=v) for k, v in pairs]


class TestThreeStateVerdicts(unittest.TestCase):
    def test_verdicts_are_three_state(self):
        """三态而不是布尔：「判不了」必须是一等公民，不是一个 None 或 False。"""
        self.assertEqual(evidence.VERDICTS, ("confirmed", "dismissed", "insufficient"))

    def test_confirmed_and_dismissed_are_real_verdicts(self):
        ok = evidence.judge(_sig(), {"f1": evidence.Fact("f1", True)})
        no = evidence.judge(_sig(), {"f1": evidence.Fact("f1", False)})
        self.assertEqual(ok.verdict, "confirmed")
        self.assertEqual(no.verdict, "dismissed")
        self.assertNotEqual(no.verdict, "insufficient", "「判否」与「判不了」是两回事")


class TestInsufficientNeverDegrades(unittest.TestCase):
    """★ 本文件的核心。每一类「判不了」都要确定性地落到 insufficient。"""

    def test_missing_fact_is_insufficient(self):
        j = evidence.judge(_sig(facts=("nope",)), {})
        self.assertEqual(j.verdict, "insufficient")
        self.assertIn("不存在的事实", "".join(j.reasons))

    def test_failed_observation_is_insufficient_not_dismissed(self):
        """观测没跑成（探针超时/文件读不到）**不是**「观测到没问题」。"""
        j = evidence.judge(_sig(), {"f1": evidence.Fact("f1", False, ok=False)})
        self.assertEqual(j.verdict, "insufficient")
        self.assertIn("观测失败", "".join(j.reasons))

    def test_failed_observation_wins_over_good_value(self):
        """即使 value 看着「真」，只要 ok=False 就不许判 confirmed。"""
        j = evidence.judge(_sig(), {"f1": evidence.Fact("f1", True, ok=False)})
        self.assertEqual(j.verdict, "insufficient")

    def test_empty_evidence_never_votes(self):
        """空证据集不许投票 ——「零覆盖」不是「零问题」（收集器收不到的假绿同源）。"""
        j = evidence.judge(_sig(facts=()), {})
        self.assertEqual(j.verdict, "insufficient")

    def test_evaluator_none_is_a_legitimate_third_answer(self):
        j = evidence.judge(_sig(), {"f1": evidence.Fact("f1", 1)},
                           evaluator=lambda s, f: None)
        self.assertEqual(j.verdict, "insufficient")

    def test_evaluator_raising_is_insufficient(self):
        def boom(s, f):
            raise ZeroDivisionError("规则自己坏了")
        j = evidence.judge(_sig(), {"f1": evidence.Fact("f1", 1)}, evaluator=boom)
        self.assertEqual(j.verdict, "insufficient")
        self.assertIn("ZeroDivisionError", "".join(j.reasons))

    def test_insufficient_never_carries_a_fake_confidence(self):
        """不可判定时留 NULL —— 编一个 0.0/0.5 出来就是「用假刻度掩盖不知道」。"""
        for j in (evidence.judge(_sig(facts=()), {}),
                  evidence.judge(_sig(), {"f1": evidence.Fact("f1", 1, ok=False)}),
                  evidence.judge(_sig(), {"f1": evidence.Fact("f1", 1)},
                                 evaluator=lambda s, f: None)):
            self.assertEqual(j.verdict, "insufficient")
            self.assertIsNone(j.confidence)

    def test_matrix_no_bad_evidence_shape_ever_confirms(self):
        """矩阵化反例：把「判不了」的各种形状过一遍，断言**没有一种**被判成 confirmed。"""
        shapes = [
            ({}, ("f1",)),                                     # 事实缺失
            ({"f1": evidence.Fact("f1", True, ok=False)}, ("f1",)),   # 观测失败
            ({"f1": evidence.Fact("f1", True)}, ()),            # 空证据集
        ]
        for facts, refs in shapes:
            with self.subTest(facts=list(facts), refs=refs):
                j = evidence.judge(_sig(facts=refs), facts)
                self.assertEqual(j.verdict, "insufficient")


class TestSummarizeAndGate(unittest.TestCase):
    def _js(self, verdicts):
        return [evidence.Judgment(key="k%d" % i, claim="c", verdict=v)
                for i, v in enumerate(verdicts)]

    def test_counts_are_separate_per_verdict(self):
        s = evidence.summarize(self._js(["confirmed", "confirmed", "dismissed", "insufficient"]))
        self.assertEqual((s["confirmed"], s["dismissed"], s["insufficient"]), (2, 1, 1))
        self.assertEqual(s["total"], 4)

    def test_decisive_is_false_when_anything_is_insufficient(self):
        self.assertFalse(evidence.summarize(self._js(["confirmed", "insufficient"]))["decisive"])
        self.assertTrue(evidence.summarize(self._js(["confirmed", "dismissed"]))["decisive"])

    def test_unjudged_blocks_decisive_even_with_zero_insufficient(self):
        """★ 「从来没判过」与「判过但判不了」分开计数，但**都**让 decisive=False。"""
        s = evidence.summarize(self._js(["confirmed"]), unjudged=3)
        self.assertEqual(s["insufficient"], 0)
        self.assertEqual(s["unjudged"], 3)
        self.assertFalse(s["decisive"], "未研判不许因为「没报错」而被当成已确认")

    def test_blocking_lists_only_insufficient_keys(self):
        s = evidence.summarize(self._js(["confirmed", "dismissed"]) + [
            evidence.Judgment(key="bad", claim="c", verdict="insufficient")])
        self.assertEqual(s["blocking"], ["bad"])

    def test_severity_ignores_dismissed(self):
        s = evidence.summarize(self._js(["dismissed", "confirmed"]))
        self.assertEqual(s["severity"].get("info"), 1)

    def test_require_decisive_raises_on_insufficient(self):
        """★ 「insufficient 不走放行」的机械形态：不是返回 False，是**抛**。"""
        s = evidence.summarize(self._js(["insufficient"]))
        with self.assertRaises(evidence.InsufficientEvidence) as cm:
            evidence.require_decisive(s, "P0-4 闸门")
        self.assertEqual(cm.exception.what, "P0-4 闸门")
        self.assertIn("insufficient", str(cm.exception))

    def test_require_decisive_passes_through_when_decisive(self):
        s = evidence.summarize(self._js(["confirmed"]))
        self.assertIs(evidence.require_decisive(s, "x"), s)

    def test_insufficient_evidence_is_not_a_valueerror(self):
        """调用方若要吞异常，必须**指名道姓**地吞 —— 不许靠 `except Exception` 顺手吞掉。"""
        self.assertTrue(issubclass(evidence.InsufficientEvidence, RuntimeError))
        self.assertFalse(issubclass(evidence.InsufficientEvidence, ValueError))


class TestLayerAndShape(unittest.TestCase):
    def test_layer_returns_three_layers_plus_summary(self):
        out = evidence.layer(_facts(("f1", True)), [_sig()])
        self.assertEqual(sorted(out.keys()), ["facts", "judgments", "signals", "summary"])
        self.assertEqual(out["summary"]["confirmed"], 1)

    def test_layer_output_is_json_serializable(self):
        """闸门产物要能直接进回包/落盘，不许塞不可序列化的对象。"""
        out = evidence.layer(_facts(("f1", True)), [_sig()])
        self.assertIsInstance(json.dumps(out, ensure_ascii=False), str)

    def test_duplicate_fact_keys_rejected(self):
        """同名两条观测 ⇒「引用了哪条」不可复述 ⇒ 直接拒绝，不静默取后者。"""
        with self.assertRaises(ValueError):
            evidence.layer(_facts(("f1", 1), ("f1", 2)), [_sig()])

    def test_unknown_severity_falls_back_to_info(self):
        j = evidence.judge(_sig(severity="catastrophic"), {"f1": evidence.Fact("f1", True)})
        self.assertEqual(j.severity, "info")

    def test_default_evaluator_is_boolean_only(self):
        """缺省规则就是 `bool(value)`，**不做推理** —— 所以字符串集合必须显式给 evaluator。

        这条钉的是真事故面：`audit.py` 的事实值是 `"dismissed"`，非空字符串恒真，
        用缺省规则会把「已否决」读成「已确认」。audit.py 因此必须传 `_verdict_evaluator`。
        """
        j = evidence.judge(_sig(), {"f1": evidence.Fact("f1", "dismissed")})
        self.assertEqual(j.verdict, "confirmed", "缺省规则确实是布尔语义（所以别拿它判字符串）")

    def test_reasons_are_human_readable_evidence(self):
        j = evidence.judge(_sig(rule="my.rule"), {"f1": evidence.Fact("f1", True)})
        self.assertTrue(j.reasons, "判定必须能说出依据，否则「为什么这么判」无法复查")
        self.assertIn("my.rule", " ".join(j.reasons))


if __name__ == "__main__":
    unittest.main(verbosity=2)