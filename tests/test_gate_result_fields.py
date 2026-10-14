#!/usr/bin/env python3
"""L0 hermetic：闸门产物的四字段结果对象（scripts/run_tier.py，P0-4）。

为什么这条值得单独一层闸门：闸门自己的输出就是这个仓的**结论来源**。旧输出是一行
`[tier] L0-hermetic: ran=1544 skipped=0 ...` 加一个退出码 —— 而「跑了 1544 例、
跳过 3 例」这种情形在旧的布尔口径里**没有位置**：它既不是绿也不是红，于是它被读成绿。
本仓原话是「禁把 SKIP 当 PASS」；本闸门把这句话钉在**闸门产物自己**身上。

四字段：`exec_status + validity + evidence + quality`。
  · `exec_status` 说**执行**（ran / empty）
  · `validity` 说**结论**（pass / fail / **insufficient**）
  · `evidence` 只放原始计数，不折算、不加工
  · `quality` 说这份证据可不可信（要不要零跳过、decisive 与否）

红向钉子（★）：`zero_skip=True` 且 `skipped>0` ⇒ **必须** insufficient + 退出码 2，
**永不允许**在这个形状上回 pass。
"""
import importlib.util
import json
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))


def _load_run_tier():
    """按路径加载脚本（不改 sys.modules 里的名字，避免与别处的 run_tier 撞）。"""
    path = _REPO / "scripts" / "run_tier.py"
    spec = importlib.util.spec_from_file_location("_p04_run_tier", path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)          # 模块级只做 import + 常量，无副作用
    return mod


rt = _load_run_tier()


class TestValidityVocabulary(unittest.TestCase):
    def test_validity_is_three_state(self):
        self.assertEqual(rt.VALIDITY, ("pass", "fail", "insufficient"))

    def test_rc_mapping_is_total_and_distinct(self):
        self.assertEqual(sorted(rt.RC_BY_VALIDITY.keys()), sorted(rt.VALIDITY))
        self.assertEqual(rt.RC_BY_VALIDITY["pass"], 0)
        self.assertEqual(rt.RC_BY_VALIDITY["fail"], 1)
        self.assertEqual(rt.RC_BY_VALIDITY["insufficient"], 2, "沿用旧脚本对 L0-skip 的退出码 2，不新造码")


class TestFourFields(unittest.TestCase):
    def test_has_exactly_the_four_fields_plus_tier(self):
        gr = rt.gate_result("L0-hermetic", ran=10, skipped=0)
        self.assertEqual(sorted(gr.keys()),
                         ["evidence", "exec_status", "quality", "tier", "validity"])

    def test_evidence_is_raw_counts(self):
        gr = rt.gate_result("L0-hermetic", ran=10, skipped=1, failures=2, errors=3)
        self.assertEqual(gr["evidence"], {"ran": 10, "skipped": 1, "failures": 2, "errors": 3})

    def test_quality_records_the_skip_policy(self):
        gr = rt.gate_result("L0-hermetic", ran=10, skipped=1, zero_skip=True)
        self.assertTrue(gr["quality"]["zero_skip_required"])
        self.assertEqual(gr["quality"]["skips_were_treated_as"], "insufficient")
        self.assertFalse(gr["quality"]["decisive"])

    def test_quality_decisive_tracks_validity(self):
        for validity_case in (
                rt.gate_result("t", ran=1, skipped=0),
                rt.gate_result("t", ran=1, failures=1),
                rt.gate_result("L0-hermetic", ran=1, skipped=1, zero_skip=True)):
            self.assertEqual(validity_case["quality"]["decisive"],
                             validity_case["validity"] != "insufficient")

    def test_json_serializable(self):
        gr = rt.gate_result("L0-hermetic", ran=1, skipped=0)
        self.assertEqual(json.loads(json.dumps(gr)), gr)


class TestValidityRules(unittest.TestCase):
    def test_clean_run_is_pass(self):
        gr = rt.gate_result("L0-hermetic", ran=1544, skipped=0, zero_skip=True)
        self.assertEqual((gr["exec_status"], gr["validity"]), ("ran", "pass"))

    def test_failures_are_fail(self):
        gr = rt.gate_result("L0-hermetic", ran=1543, skipped=0, failures=1, zero_skip=True)
        self.assertEqual(gr["validity"], "fail")

    def test_errors_are_fail(self):
        gr = rt.gate_result("L0-hermetic", ran=1543, skipped=0, errors=1, zero_skip=True)
        self.assertEqual(gr["validity"], "fail")

    def test_failure_beats_skip(self):
        """又挂又跳 ⇒ fail（挂是更硬的结论）；insufficient 只用于「判不了」。"""
        gr = rt.gate_result("L0-hermetic", ran=9, skipped=1, failures=1, zero_skip=True)
        self.assertEqual(gr["validity"], "fail")

    def test_skip_in_zero_skip_tier_is_insufficient(self):
        """★ 本条就是「禁把 SKIP 当 PASS」。旧口径在这里回的是「全绿」。"""
        gr = rt.gate_result("L0-hermetic", ran=1541, skipped=3, zero_skip=True)
        self.assertEqual(gr["validity"], "insufficient")
        self.assertEqual(rt.RC_BY_VALIDITY[gr["validity"]], 2)

    def test_skip_in_host_tier_is_pass(self):
        """L1（host）换机时应整层显式跳过 —— 那里的 skip 是**预期**，不是不可判定。"""
        gr = rt.gate_result("L1-host", ran=0, skipped=65, zero_skip=False)
        self.assertEqual(gr["validity"], "pass")
        self.assertEqual(gr["quality"]["skips_were_treated_as"], "n/a")

    def test_zero_tests_is_empty_not_pass(self):
        """★ 「一个用例都没发现」不是绿：判据没被发现 = 发现器坏了 / 文件不在。"""
        gr = rt.gate_result("L0-hermetic", ran=0, skipped=0, zero_skip=True)
        self.assertEqual(gr["exec_status"], "empty")
        self.assertEqual(gr["validity"], "insufficient")

    def test_matrix_never_passes_when_zero_skip_is_violated(self):
        """矩阵化反例：任何 (failures, errors, skipped) 组合下，
        「要求零跳过却出现跳过」都不许产出 pass。"""
        for f in (0, 1):
            for e in (0, 1):
                for s in (0, 1, 5):
                    with self.subTest(failures=f, errors=e, skipped=s):
                        gr = rt.gate_result("L0-hermetic", ran=10, skipped=s,
                                            failures=f, errors=e, zero_skip=True)
                        if s and not (f or e):
                            self.assertEqual(gr["validity"], "insufficient")
                        self.assertIn(gr["validity"], rt.VALIDITY)

    def test_every_validity_has_an_rc(self):
        """矩阵穷举：不许出现「产出了一个没有退出码的 validity」。"""
        seen = set()
        for zs in (True, False):
            for s in (0, 1):
                for f in (0, 1):
                    for e in (0, 1):
                        gr = rt.gate_result("t", ran=1, skipped=s, failures=f, errors=e,
                                            zero_skip=zs)
                        seen.add(gr["validity"])
                        self.assertIn(gr["validity"], rt.RC_BY_VALIDITY)
        self.assertEqual(seen, set(rt.VALIDITY), "三种结论都应可达")


class TestEmitter(unittest.TestCase):
    def test_emit_is_one_machine_readable_line(self):
        import io
        buf = io.StringIO()
        old = sys.stdout
        try:
            sys.stdout = buf
            rt._emit_gate(rt.gate_result("L0-hermetic", ran=3, skipped=0))
        finally:
            sys.stdout = old
        line = buf.getvalue().rstrip("\n")
        self.assertTrue(line.startswith("[gate] "), line)
        self.assertEqual(json.loads(line[len("[gate] "):])["validity"], "pass")

    def test_emit_rejects_an_unknown_validity(self):
        with self.assertRaises(AssertionError):
            rt._emit_gate({"tier": "t", "validity": "probably-fine"})


if __name__ == "__main__":
    unittest.main(verbosity=2)