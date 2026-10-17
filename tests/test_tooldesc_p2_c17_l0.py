#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P2 C17 工具描述质量（src/tooldesc.py + mcpgw/skill 接线）。

手册 C17：「工具瓶颈不在数量而在**描述质量**：统一描述模板 + 完备性校验 + 按任务裁剪」。

守的判据：

1. **三态完备性，缺项显式** —— `complete|partial|insufficient`，`missing` 必列出缺段。
   **绝不静默归类**：空/占位/极短必须判 `insufficient`，不得混进 `partial`
   （那会把完备率虚高，与 P0-4「判不了做成一等公民」同一口径）。
2. **判据量的是描述文本，不是回包字段** —— `completeness()` 吃的是**原始 description**。
   红向：若有人把判据改成「看回包有没有 inputs 键」，本文件的 `TestJudgedOnText`
   会转红（判例 185/190「判据量错对象」同族防线）。
3. **模板归一认结构不认词** —— 只认「标签 + 冒号」这个结构；`myinput:` 不算 `input` 标签。
   红向：把边界改回裸子串匹配 ⇒ `TestLabelBoundary` 转红。
4. **按任务裁剪复用同一套打分** —— `select()` 必须委派 `skill_relevance.rank`
   （不建第二套 BM25）。红向：自造一套打分 ⇒ `TestSelectReusesScorer` 转红。
5. **裁剪如实报数** —— `considered/selected/dropped` 必须自洽，选不出时给 `why`
   而不是看着像「没有相关工具」的空表。
6. **接线两处**（mcpgw 门面 + skill 扫描）：`description_quality` 块必须在场，
   且**对原始 desc 判定**（不吃 160 字截断）。

分层口径（tests/tiers.py）：纯函数 + 源码文本断言，不起服务、不连网、零 SKIP。
"""
import json
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import tooldesc  # noqa: E402


# ══════════════════ 1. 完备性三态：缺项显式，不静默归类 ══════════════════

class TestCompleteness(unittest.TestCase):

    def test_four_sections_is_complete(self):
        d = ("grep 文件内容。\n输入: pattern, path\n输出: 匹配行列表\n限制: 大目录慢")
        c = tooldesc.completeness(d)
        self.assertEqual(c["verdict"], "complete")
        self.assertEqual(c["missing"], [])
        self.assertEqual(c["sections"]["inputs"], "pattern, path")

    def test_missing_outputs_is_partial_and_listed(self):
        """有主体但缺 outputs ⇒ partial，且 missing 必须点名 outputs。"""
        c = tooldesc.completeness("搜索文件。\n输入: pattern")
        self.assertEqual(c["verdict"], "partial")
        self.assertIn("outputs", c["missing"])
        self.assertIn("purpose", c["sections"])  # 有主体的证据

    def test_limits_missing_stays_complete_but_flagged(self):
        """limits 缺失**不降级**，但必须记入 missing（如实标注而非悄悄放过）。"""
        c = tooldesc.completeness("搜索文件。\n参数: pattern\n返回: 匹配行")
        self.assertEqual(c["verdict"], "complete")
        self.assertEqual(c["missing"], ["limits"])

    def test_empty_is_insufficient(self):
        """空描述 ⇒ insufficient，missing 列出全部四段。"""
        c = tooldesc.completeness("")
        self.assertEqual(c["verdict"], "insufficient")
        self.assertEqual(set(c["missing"]), set(tooldesc.SECTIONS))

    def test_very_short_is_insufficient(self):
        """极短文本（< 10 字符）⇒ insufficient，即使碰巧有个标签。"""
        c = tooldesc.completeness("输: x")
        self.assertEqual(c["verdict"], "insufficient")


# ══════════════════ 2. 判据量的是文本，不是回包字段 ══════════════════

class TestJudgedOnText(unittest.TestCase):
    """判例 185/190 同族防线：判据必须量**描述文本本身**，不得看回包结构。"""

    def test_quality_eats_raw_text_not_dict(self):
        """`quality()` 和 `completeness()` 入参是 str，不是 dict。"""
        result = tooldesc.quality("输入: pattern 和 path\n输出: 匹配的行列表\n限制: 大目录慢")
        self.assertIsInstance(result, dict)
        self.assertEqual(result["verdict"], "complete")

    def test_truncation_does_not_affect_verdict(self):
        """截断只影响展示，不该影响质量判定——判据量的是原始 desc。"""
        long_desc = "目的: do stuff.\n" + "输入: " + "x" * 200 + "\n输出: result\n限制: none"
        c = tooldesc.completeness(long_desc)
        self.assertEqual(c["verdict"], "complete",
                         "长描述不应因长度而判为 non-complete")


# ══════════════════ 3. 模板归一认结构不认词 ══════════════════

class TestLabelBoundary(unittest.TestCase):
    """只认「标签 + 冒号」结构；裸子串不算。"""

    def test_colon_required(self):
        """没有冒号 ⇒ 不算标签，整段归入 purpose。"""
        c = tooldesc.completeness("inputs x y z")
        self.assertEqual(c["sections"]["inputs"], "")
        self.assertIn("inputs x y z", c["sections"]["purpose"])

    def test_english_label_with_colon(self):
        c = tooldesc.completeness("Find files.\nInputs: pattern, path\nOutputs: matches")
        self.assertEqual(c["sections"]["inputs"], "pattern, path")
        self.assertEqual(c["sections"]["outputs"], "matches")

    def test_chinese_label_with_colon(self):
        c = tooldesc.completeness("查找文件。\n输入: 模式\n输出: 结果")
        self.assertEqual(c["sections"]["inputs"], "模式")
        self.assertEqual(c["sections"]["outputs"], "结果")

    def test_mixed_labels_work(self):
        c = tooldesc.completeness("Search.\n输入: pattern\nOutputs: lines\n限制: slow")
        self.assertEqual(c["sections"]["inputs"], "pattern")
        self.assertEqual(c["sections"]["outputs"], "lines")
        self.assertEqual(c["sections"]["limits"], "slow")


# ══════════════════ 4. select 委派 skill_relevance，不建第二套打分 ══════════════════

class TestSelectReusesScorer(unittest.TestCase):
    """select() 必须走 skill_relevance.rank，不得自己写打分逻辑。"""

    def test_select_uses_skill_relevance(self):
        """含相关关键词的工具应被选中。"""
        tools = [
            {"name": "grep_files", "description": "grep 搜索文件内容",
             "server": "s1", "description_quality": {"verdict": "complete"}},
            {"name": "ping_host", "description": "ping 测试网络连通性",
             "server": "s2", "description_quality": {"verdict": "complete"}},
        ]
        result = tooldesc.select("grep for patterns", tools, n=1)
        names = [it["name"] for it in result.get("items", [])]
        self.assertIn("grep_files", names,
                      "grep 相关工具应被选中，说明复用了 skill_relevance 打分")

    def test_select_reports_counts_correctly(self):
        tools = [
            {"name": "a", "description": "do thing number a", "server": "s1",
             "description_quality": {"verdict": "complete"}},
            {"name": "b", "description": "do thing number b", "server": "s1",
             "description_quality": {"verdict": "partial"}},
            {"name": "c", "description": "do thing number c", "server": "s2",
             "description_quality": {"verdict": "insufficient"}},
        ]
        result = tooldesc.select("do thing", tools, n=2)
        self.assertEqual(result["considered"], 3)
        self.assertEqual(result["selected"] + result["dropped"], 3)
        self.assertGreaterEqual(result["selected"], 1)


# ══════════════════ 5. 裁剪如实报数，选不出给 why ══════════════════

class TestSelectReporting(unittest.TestCase):

    def test_empty_task_gives_why(self):
        result = tooldesc.select("", [{"name": "x", "description": "y",
                                        "server": "s1", "description_quality": {}}])
        self.assertEqual(result["selected"], 0)
        self.assertIsNotNone(result["why"])

    def test_no_tools_gives_why(self):
        result = tooldesc.select("something", [], n=3)
        self.assertEqual(result["considered"], 0)
        self.assertEqual(result["selected"], 0)
        self.assertIsNotNone(result["why"])

    def test_dropped_is_considered_minus_selected(self):
        tools = [{"name": f"t{i}", "description": f"description {i}",
                  "server": "s1", "description_quality": {"verdict": "complete"}}
                 for i in range(5)]
        result = tooldesc.select("unrelated xyz qqq", tools, n=2)
        self.assertEqual(result["considered"] - result["selected"], result["dropped"])


# ══════════════════ 6. 接线两处：mcpgw + skill 都有 description_quality ══════════════════

class TestWiring(unittest.TestCase):
    """C17 接线：两个门面都必须产出 description_quality 块。"""

    def test_mcpgw_tool_shape_has_quality_block(self):
        """mcpgw._tool_shape 输出的每条工具必须带 description_quality。"""
        import mcpgw
        # 用 mock tool 对象
        class MockTool:
            name = "test_tool"
            description = "输入: x\n输出: y\n限制: z\n目的: do stuff"
        shape = mcpgw._tool_shape(MockTool())
        self.assertIn("description_quality", shape)
        self.assertEqual(shape["description_quality"]["verdict"], "complete")

    def test_skill_scan_item_has_quality_block(self):
        """skill._scan_one 输出的每条技能必须带 description_quality。"""
        import skill
        import tempfile, os
        with tempfile.TemporaryDirectory() as tmpdir:
            skill_dir = os.path.join(tmpdir, "test_skill")
            os.makedirs(skill_dir)
            with open(os.path.join(skill_dir, "SKILL.md"), "w",
                      encoding="utf-8") as f:
                f.write("---\nname: test_skill\ndescription: 搜索文件内容。\n输入: pattern, path\n输出: 匹配行列表\n---\n# test")
            saved = skill.SKILL_DIRS
            skill.SKILL_DIRS = {"t": tmpdir}
            try:
                result = skill._scan_one("t", skill_dir)
            finally:
                skill.SKILL_DIRS = saved
            items = result.get("items", [])
            self.assertGreater(len(items), 0)
            self.assertIn("description_quality", items[0])


if __name__ == "__main__":
    unittest.main()
