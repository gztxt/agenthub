#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：任务验证器（src/verifier.py）—— 「执行者不得自证 success」。

【这条判据治什么】
P0-3 之前，`tasks._run_task` 直接信执行者回包里的 `success=True` 就把任务置 `success`。
但「执行者说成了」与「产物真的合格」是两件事（与 P0-4「判不了 ≠ 没问题」同族）：
执行者既是被测对象，又握着判定权 ⇒ 它可以自证。

本文件守四条：
1. **三态**：`pass` / `fail` / `insufficient` 恰好三个；`insufficient` 是一等公民。
2. **判不了绝不放行**：未知 kind / 产物缺失 / 判据（expect）缺失 ⇒ `insufficient`，
   **永不**退化成 `pass`（这是 P0-4 `evidence.py` 口径在验证层的复用）。
3. **fail 要结构化**：`gaps` 是「缺口清单」（每条 id + detail），`repairable` 说能不能重试 ——
   调用方据此决定重跑还是上报，不靠解析自然语言。
4. 真调被测代码（不读注释）。

分层口径（tests/tiers.py）：纯函数，不起服务、不连网、零 SKIP。
"""
import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import verifier  # noqa: E402

THREE = {"pass", "fail", "insufficient"}


class TestEnum(unittest.TestCase):
    def test_statuses_are_exactly_three(self):
        self.assertEqual({verifier.STATUS_PASS, verifier.STATUS_FAIL,
                          verifier.STATUS_INSUFFICIENT}, THREE)

    def test_default_kind_is_registered(self):
        self.assertIn(verifier.DEFAULT_KIND, verifier.known_kinds())

    def test_known_kinds_cover_the_three_contract_kinds(self):
        self.assertEqual({"nonempty", "json_array", "contains"},
                         set(verifier.known_kinds()))


class TestInsufficientNeverPasses(unittest.TestCase):
    """判不了 ⇒ insufficient，绝不退化成 pass。"""

    def test_unknown_kind_is_insufficient_not_pass(self):
        v = verifier.verify("no_such_kind", output="anything")
        self.assertEqual(v["status"], verifier.STATUS_INSUFFICIENT)
        self.assertNotEqual(v["status"], verifier.STATUS_PASS)
        self.assertTrue(v["gaps"], "insufficient 必须说清判不了的原因")
        self.assertEqual(v["gaps"][0]["id"], "unknown_kind")

    def test_missing_output_is_insufficient(self):
        v = verifier.verify("nonempty", output=None)
        self.assertEqual(v["status"], verifier.STATUS_INSUFFICIENT)
        self.assertEqual(v["gaps"][0]["id"], "no_output")

    def test_contains_without_expectation_is_insufficient(self):
        v = verifier.verify("contains", output="hello", expect=None)
        self.assertEqual(v["status"], verifier.STATUS_INSUFFICIENT)
        self.assertEqual(v["gaps"][0]["id"], "no_expectation")

    def test_contains_with_empty_expectation_is_insufficient(self):
        v = verifier.verify("contains", output="hello", expect="")
        self.assertEqual(v["status"], verifier.STATUS_INSUFFICIENT)

    def test_no_kind_returns_pass_for_missing_output(self):
        """对每个已登记 kind，产物缺失都不得判成 pass。"""
        for kind in verifier.known_kinds():
            with self.subTest(kind=kind):
                v = verifier.verify(kind, output=None, expect="x")
                self.assertNotEqual(v["status"], verifier.STATUS_PASS)


class TestNonempty(unittest.TestCase):
    def test_empty_output_fails_and_is_repairable(self):
        v = verifier.verify("nonempty", output="")
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertTrue(v["repairable"])
        self.assertEqual(v["gaps"][0]["id"], "empty_output")

    def test_whitespace_only_fails(self):
        v = verifier.verify("nonempty", output="  \n\t ")
        self.assertEqual(v["status"], verifier.STATUS_FAIL)

    def test_real_output_passes_with_no_gaps(self):
        v = verifier.verify("nonempty", output="done")
        self.assertEqual(v["status"], verifier.STATUS_PASS)
        self.assertEqual([], v["gaps"])

    def test_refusal_is_not_pass(self):
        """实弹 2026-10-27：jcode 回「I don't have direct filesystem access…请粘贴」
        长度足够 ⇒ nonempty 判 pass，整条派发零产出却显示成功。"""
        out = ("I don't have direct filesystem access, so I can't browse that path.\n"
               "If you paste the output of `find ...`, I'll instantly count them.")
        v = verifier.verify("nonempty", output=out)
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertEqual(v["gaps"][0]["id"], "refusal")
        self.assertTrue(v["repairable"], "拒答应允许重试换个执行者/模型")

    def test_chinese_refusal_is_not_pass(self):
        v = verifier.verify("nonempty", output="我无法访问该目录，请把文件列表粘贴过来。")
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertEqual(v["gaps"][0]["id"], "refusal")

    def test_chinese_no_capability_refusal_is_not_pass(self):
        """影子实弹 2026-10-29 原文：旧正则（只认「我无法访问」）漏判 ⇒ pass。

        同一条洞的第二种说法：「我无法**直接读取**…因为我目前**没有可用的…工具**」。
        判据要覆盖说法，不是覆盖某一句。
        """
        out = ("我无法直接读取您系统的 `/etc/hostname` 文件，"
               "因为我目前没有可用的文件系统访问工具。")
        v = verifier.verify("nonempty", output=out)
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertEqual(v["gaps"][0]["id"], "refusal")

    def test_second_person_caveat_still_passes(self):
        """窄口径反向：第二人称的「如果你无法访问…」是操作建议，不是自述没干。"""
        v = verifier.verify("nonempty", output="如果你无法访问该路径，可以先执行 ls 确认。")
        self.assertEqual(v["status"], verifier.STATUS_PASS)

    def test_normal_caveat_still_passes(self):
        """窄口径：正常报告里出现 can't / 无法 不算拒答，别把好人判死。"""
        v = verifier.verify("nonempty",
                            output="src 下共 96 个 .py 文件；更精确的统计不含 .bak 副本。")
        self.assertEqual(v["status"], verifier.STATUS_PASS)

    def test_safety_label_is_not_pass(self):
        """实弹 2026-10-27：免费池路由到 content-safety 模型，回包
        `User Safety: safe` 长度非零 ⇒ nonempty 判 pass，run 以 complete 收敛。"""
        v = verifier.verify("nonempty", output="User Safety: safe")
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertEqual(v["gaps"][0]["id"], "not_a_product")

    def test_real_answer_mentioning_safety_passes(self):
        v = verifier.verify("nonempty",
                            output="译文：The dispatch link is up. 字符数(不含空格)=21")
        self.assertEqual(v["status"], verifier.STATUS_PASS)


class TestJsonArray(unittest.TestCase):
    def test_fenced_json_array_passes(self):
        v = verifier.verify("json_array", output="```json\n[1, {\"a\": 2}]\n```")
        self.assertEqual(v["status"], verifier.STATUS_PASS)

    def test_object_is_not_an_array(self):
        v = verifier.verify("json_array", output='{"a": 1}')
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertEqual(v["gaps"][0]["id"], "not_array")

    def test_unparseable_output_fails_repairable(self):
        v = verifier.verify("json_array", output="not json at all")
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertTrue(v["repairable"])

    def test_missing_output_is_insufficient(self):
        v = verifier.verify("json_array", output=None)
        self.assertEqual(v["status"], verifier.STATUS_INSUFFICIENT)


class TestContains(unittest.TestCase):
    def test_present_expectation_passes(self):
        v = verifier.verify("contains", output="hello world", expect="world")
        self.assertEqual(v["status"], verifier.STATUS_PASS)

    def test_absent_expectation_fails(self):
        v = verifier.verify("contains", output="hello", expect="nope")
        self.assertEqual(v["status"], verifier.STATUS_FAIL)
        self.assertEqual(v["gaps"][0]["id"], "missing_expectation")


class TestVerdictShape(unittest.TestCase):
    def test_gaps_are_structured_dicts(self):
        v = verifier.verify("nonempty", output="")
        self.assertIsInstance(v["gaps"], list)
        for g in v["gaps"]:
            self.assertIn("id", g)
            self.assertIn("detail", g)

    def test_verdict_carries_kind_and_detail(self):
        v = verifier.verify("nonempty", output="")
        self.assertEqual(v["kind"], "nonempty")
        self.assertIsInstance(v["detail"], str)


if __name__ == "__main__":
    unittest.main()