#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：产物分类器（src/outputguard.py）。

【为什么要有这个文件】
2026-10-15 真数据探针实测到一条**假绿**：
`model=nvidia/nemotron-3.5-content-safety:free`、`output='User Safety: safe'`、
`verdict=pass` —— 免费池把请求路由到审核类模型，返回标签而非正文，
而 `nonempty` 验证器只判非空，于是「标签」被记成「模型成功产出了内容」。
P0-c 若拿它当画像素材，「模型擅长什么」就建在审核模型输出上。

【本文件守四条】
1. **认得出真样本**：`'User Safety: safe'` + content-safety 模型 ⇒ `label`。
2. **不误伤真正文**：生产库实测的三条真实产物（Python/Rust 问答）必须判 `content`。
3. **只认强信号**：同族通用生成模型（`nemotron-3.5-lightning` 等）**不得**被
   `content-safety` 的家族规则连带误伤 —— 这是最容易写坏的一条。
4. **每条判定带 reason**，且 `empty` 单独成档（无产物 ≠ 标签产物）。

分层口径：纯函数、stdlib-only、零 SKIP、不起服务不连网。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import outputguard as og  # noqa: E402


class TestRealSamples(unittest.TestCase):
    """判据 1 + 2：真样本与真正文。"""

    #: 生产库 `tasks` 三条真实产物（2026-10-15 取证原文），一个都不能被误判。
    REAL_PRODUCTIONS = (
        "Python 是一种简洁易读、应用广泛的高级通用编程语言，常用于 Web 开发、"
        "数据科学、人工智能、自动化和脚本编写。",
        "Rust 是一门注重内存安全、高性能和高并发的系统编程语言。",
        "Python 是一种简洁易读、应用广泛的高级通用编程语言，常用于 Web 开发、"
        "数据科学、人工智能、自动化和脚本编写；而 Rust 则是一门注重内存安全、"
        "高性能和高并发的系统编程语言，更适合对性能与安全性要求较高的底层和",
    )

    def test_the_observed_false_green_is_label(self):
        """判据 1：本轮探针实测到的那一条，必须判成 label。"""
        v, reason = og.classify_output("User Safety: safe",
                                       "nvidia/nemotron-3.5-content-safety:free")
        self.assertEqual(og.LABEL, v)
        self.assertTrue(reason)
        self.assertFalse(og.is_usable_evidence("User Safety: safe",
                                                "nvidia/nemotron-3.5-content-safety:free"),
                         "标签型产物不得进画像统计")

    def test_real_productions_are_content(self):
        """判据 2：生产库实测真正文全部判 content（宁漏勿伤的反面）。"""
        for text in self.REAL_PRODUCTIONS:
            v, _ = og.classify_output(text, "qwen3.8-flash")
            self.assertEqual(og.CONTENT, v, "误伤真正文：%r" % text[:40])

    def test_real_productions_usable_as_evidence(self):
        for text in self.REAL_PRODUCTIONS:
            self.assertTrue(og.is_usable_evidence(text, "qwen3.8-flash"))


class TestNoFalsePositive(unittest.TestCase):
    """判据 3：只认强信号。"""

    def test_sibling_nemotron_generative_models_not_flagged(self):
        """同族通用生成模型不得被家族规则连带误伤 —— 最易写坏的一条。"""
        for m in ("nvidia/nemotron-3.5-lightning:free",
                  "nvidia/nemotron-3-super-120b-a12b:free",
                  "nvidia/nemotron-3-ultra-550b-a55b:free",
                  "nvidia/nemotron-3-nano-omni-30b-a3b-reasoning:free"):
            v, reason = og.classify_output("一段正常的回答内容。", m)
            self.assertEqual(og.CONTENT, v, "误伤 %s：%s" % (m, reason))

    def test_normal_answer_with_colon_not_flagged(self):
        """正常回答里带冒号/带空格的值，不得被「标签: 值」形状误抓。"""
        for text in ("Note: 见 README 的第 3 节",
                     "结论: 这个方案在吞吐和延迟之间做了取舍",
                     "总结如下",
                     "User Safety: 这是一段真正的说明性正文而不是标签"):
            v, _ = og.classify_output(text, "qwen3.8-flash")
            self.assertEqual(og.CONTENT, v, "误伤：%r" % text)

    def test_code_output_not_flagged(self):
        """代码/结构化输出不是标签。"""
        for text in ('{"ok": true}', "def f(x):\n    return x * 2",
                     "- item one\n- item two"):
            v, _ = og.classify_output(text, "deepseek-v4.1-flash")
            self.assertEqual(og.CONTENT, v, "误伤：%r" % text[:40])

    def test_empty_model_name_does_not_crash(self):
        v, reason = og.classify_output("一些内容", None)
        self.assertEqual(og.CONTENT, v)
        self.assertTrue(reason)
        v, reason = og.classify_output("一些内容", "")
        self.assertEqual(og.CONTENT, v)


class TestEmptyIsItsOwnVerdict(unittest.TestCase):
    """判据 4：空产物单独成档。"""

    def test_empty_output_is_empty_not_label(self):
        for text in (None, "", "   ", "\n\t "):
            v, reason = og.classify_output(text, "qwen3.8-flash")
            self.assertEqual(og.EMPTY, v, "%r 判成了 %s" % (text, v))
            self.assertTrue(reason)

    def test_empty_is_not_usable_evidence(self):
        """「模型什么都没产出」属 verdict 维度，不进「擅长什么」统计。"""
        self.assertFalse(og.is_usable_evidence("", "qwen3.8-flash"))

    def test_empty_takes_priority_over_label_model(self):
        """空产物 + 分类族模型 ⇒ 报 empty（更具体的事实优先）。"""
        v, reason = og.classify_output("", "nvidia/nemotron-3.5-content-safety:free")
        self.assertEqual(og.EMPTY, v)
        self.assertTrue(reason)


class TestReasonAlwaysPresent(unittest.TestCase):
    """判据 4：reason 恒非空，且能区分是哪条规则命中。"""

    def test_reason_non_empty_for_all_branches(self):
        cases = (
            ("User Safety: safe", "nvidia/nemotron-3.5-content-safety:free"),
            ("User Safety: safe", "qwen3.8-flash"),      # 靠形状规则
            ("一段正文。", "qwen3.8-flash"),
            ("", "qwen3.8-flash"),
            ("随便什么", "some-guard-model"),             # 靠家族规则
        )
        for text, m in cases:
            _, reason = og.classify_output(text, m)
            self.assertTrue(reason and reason.strip(),
                            "reason 为空：%r/%r" % (text, m))

    def test_family_and_shape_reasons_differ(self):
        _, r1 = og.classify_output("User Safety: safe",
                                   "nvidia/nemotron-3.5-content-safety:free")
        _, r2 = og.classify_output("User Safety: safe", "qwen3.8-flash")
        self.assertNotEqual(r1, r2, "两条规则的 reason 应可区分，便于审计")

    def test_verdicts_are_closed_set(self):
        for text, m in (("x", None), ("", None),
                        ("User Safety: safe", "safety-model")):
            self.assertIn(og.classify_output(text, m)[0], og.VERDICTS)


if __name__ == "__main__":
    unittest.main(verbosity=2)
