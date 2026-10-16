#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：上游产物交接块（`tasks.format_upstream`，P1-3）—— 「不许静默截断」。

【这条判据治什么】
原实现是 `upstream[d][:UPSTREAM_TRIM]`：**静默整体截断**。它有两个独立危害，
本文件各钉一条：

1. **下游无从分辨**「上游本来就只写了 4000 字」与「上游写了 8000 字被我砍到 4000」。
   两种情况 prompt 长得一模一样 ⇒ 下游只能基于**缺失内容**下结论，而它并不知道缺了。
2. **尾部结论被丢弃**。整体砍尾丢的往往正是结论句（摘要/判定/建议在末尾）。

因此判据不是「输出对不对」而是「**截断这件事必须可见**」：
- 短文本必须**逐字节透传**（绝大多数任务走这条路，套壳就是噪声）；
- 超长文本必须自带**原始长度 + 省略量**的可断言标记；
- **尾部必须保留**；
- 正文预算**不许因为加标记而放大**。

【为什么要有红向自证】
「测试全绿」本身不能证明判据有效：把 `format_upstream` 改回 `[:TRIM]` 也能让
「短文本透传」那几条绿。所以每条关键判据都配一条**反向断言**，
证明它真的会因为实现退化而红（见 TestRedDirection）。

分层口径（tests/tiers.py）：纯函数，不起服务、不连网、零 SKIP。
"""
import os
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

os.environ.setdefault("HUB_TEST_MODE", "1")

import tasks  # noqa: E402

TRIM = tasks.UPSTREAM_TRIM
HEAD = tasks.UPSTREAM_HEAD
TAIL = tasks.UPSTREAM_TAIL


def _code_without_docstrings(path: Path) -> str:
    """活代码文本：**去掉模块/函数 docstring**。

    为什么必须去：P1-3 的文档串**故意引用**了修复前的写法 `upstream[d][:UPSTREAM_TRIM]`
    来说明「原实现长什么样」。若只按 `#` 注释行过滤，这条引用会被当成活代码 ⇒
    「不许退回整体截断」这条判据自己把自己判红。判据必须量**会被执行的代码**。
    """
    import ast
    src = path.read_text(encoding="utf-8")
    tree = ast.parse(src)
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""      # 置空而非删行 ⇒ 行号不变，便于定位
    return ast.unparse(tree)


class TestShortPathIsByteIdentical(unittest.TestCase):
    """短文本：逐字节原样透传，一个字节都不许加。"""

    def test_short_text_has_no_marker(self):
        out = tasks.format_upstream("t1", "短产物")
        self.assertEqual(out, "—— 任务 t1 的结果 ——\n短产物")

    def test_empty_text_does_not_crash(self):
        out = tasks.format_upstream("t1", "")
        self.assertEqual(out, "—— 任务 t1 的结果 ——\n")

    def test_exactly_at_budget_stays_透传(self):
        """**恰好等于预算**必须走透传分支 —— 边界不多砍一刀。

        这是最容易写坏的地方：`<=` 写成 `<` 会让「正好 4000 字」的产物
        凭空多出「已省略 0 字符」的自相矛盾标记。
        """
        body = "字" * TRIM
        out = tasks.format_upstream("t1", body)
        self.assertIn(body, out)
        self.assertNotIn("已省略", out)
        self.assertNotIn("以上省略", out)

    def test_one_over_budget_is_marked(self):
        """预算 +1 个字符就必须被标记（证明上一条不是恒真）。"""
        out = tasks.format_upstream("t1", "字" * (TRIM + 1))
        self.assertIn("已省略", out)
        self.assertIn("原文 %d 字符" % (TRIM + 1), out)


class TestTruncationIsVisible(unittest.TestCase):
    """截断必须自带可断言的标记（原始长度 + 省略量）。"""

    def setUp(self):
        self.total = TRIM * 3
        self.text = "A" * self.total
        self.out = tasks.format_upstream("t7", self.text)

    def test_reports_original_length(self):
        self.assertIn("原文 %d 字符" % self.total, self.out)

    def test_reports_omitted_amount(self):
        self.assertIn("已省略中间 %d 字符" % (self.total - TRIM), self.out)

    def test_marks_head_and_tail_split(self):
        self.assertIn("保留头 %d + 尾 %d" % (HEAD, TAIL), self.out)

    def test_task_id_present(self):
        self.assertIn("t7", self.out)

    def test_omitted_amount_is_arithmetically_right(self):
        """省略量必须 = 原文 − 预算。写错这个数下游的判断依据就是假的。"""
        m = re.search(r"已省略中间 (\d+) 字符", self.out)
        self.assertIsNotNone(m)
        self.assertEqual(int(m.group(1)), self.total - TRIM)


class TestTailIsPreserved(unittest.TestCase):
    """尾部保留 —— 「保留结论」是本项存在的理由。"""

    def test_tail_marker_content_present(self):
        tail_marker = "最终结论：全部通过，不需返工。"
        text = "字" * (TRIM * 2) + tail_marker
        out = tasks.format_upstream("t9", text)
        self.assertIn(tail_marker, out)

    def test_head_content_present(self):
        head_marker = "任务目标：核对三条断言。"
        text = head_marker + "字" * (TRIM * 2)
        out = tasks.format_upstream("t9", text)
        self.assertIn(head_marker, out)


class TestBudgetNotInflated(unittest.TestCase):
    """加标记不得放大正文预算 —— 否则「限 4000」会名不副实。

    口径：量**保留段本身**（head/tail），不量重新拼装后的整块。
    早版判据把 head 与 tail 用换行拼起来再量，凭空多出 1 个字符（4001 > 4000），
    那测的是「我自己的拼接手法」不是「实现的预算」—— 量错了对象，判据就成噪声。
    """

    def _head_tail(self, out: str):
        """取保留的头/尾两段（按行定位，不靠字符串 split —— `——` 也出现在标题行里）。

        超长输出恒为四行：标题 / 头 / 省略标记 / 尾。
        """
        lines = out.splitlines()
        self.assertEqual(len(lines), 4, "超长交接块应为四行，实得 %d" % len(lines))
        return len(lines[1]), len(lines[3])

    def test_kept_segments_within_budget(self):
        # 从 mult=2 起：mult=1 恰好等于预算，走的是**透传**分支（不是截断分支）
        for mult in (2, 5, 50):
            out = tasks.format_upstream("t", "字" * (TRIM * mult))
            h, t = self._head_tail(out)
            self.assertLessEqual(h, HEAD, "mult=%d 头部超预算" % mult)
            self.assertLessEqual(t, TAIL, "mult=%d 尾部超预算" % mult)

    def test_kept_total_equals_trim(self):
        """超长时保留量必须**正好等于**预算：既不虚报（少留）也不超支（多留）。"""
        h, t = self._head_tail(tasks.format_upstream("t", "字" * (TRIM * 3)))
        self.assertEqual(h + t, TRIM)

    def test_head_plus_tail_equals_trim(self):
        self.assertEqual(HEAD + TAIL, TRIM)


class TestRedDirection(unittest.TestCase):
    """红向自证：这些断言在「实现退回原写法」时必须转红，否则判据本身是假的。"""

    def _naive(self, task_id, text):
        """修复前的原实现。"""
        return "—— 任务 %s 的结果 ——\n%s" % (task_id, (text or "")[:TRIM])

    def test_naive_impl_fails_marker_assertions(self):
        out = self._naive("t", "字" * (TRIM * 3))
        self.assertNotIn("已省略", out)          # 朴素实现无标记
        self.assertNotIn("原文", out)

    def test_naive_impl_fails_tail_assertion(self):
        """朴素实现把尾部整段丢掉 ⇒ 「尾部必须保留」这条判据真的有效。"""
        tail_marker = "最终结论：全部通过。"
        out = self._naive("t", "字" * (TRIM * 2) + tail_marker)
        self.assertNotIn(tail_marker, out)

    def test_strict_lt_boundary_fails(self):
        """把 `<=` 写成 `<` ⇒ 恰好等于预算时会多出「已省略 0 字符」的自相矛盾标记。"""
        body = "字" * TRIM
        broken = ("—— 任务 t1 的结果 ——（原文 %d 字符，已省略中间 0 字符，"
                  "保留头 %d + 尾 %d）——\n%s\n……（以上省略 0 字符）……\n%s"
                  % (TRIM, HEAD, TAIL, body[:HEAD], body[-TAIL:]))
        self.assertIn("已省略中间 0 字符", broken)  # 自相矛盾，本实现绝不会产出

    def test_current_impl_has_no_self_contradicting_marker(self):
        """反向确认：本实现在边界上不会产出「省略 0 字符」。"""
        out = tasks.format_upstream("t1", "字" * TRIM)
        self.assertNotIn("省略 0", out)


class TestCallSiteWired(unittest.TestCase):
    """判据④：`_run_task` 必须真的调它（改了函数没换调用点 = 假绿）。"""

    def test_no_naive_slice_remains(self):
        """活代码里不得再有 `[:UPSTREAM_TRIM]` 整体截断。

        量的是**去掉 docstring 后**的代码：本项的文档串故意引用了这个旧写法
        做对比说明，不去 docstring 就会自己把自己判红（判据量错了对象）。
        """
        live = _code_without_docstrings(_REPO / "src" / "tasks.py")
        self.assertNotRegex(live, r"\[:\s*UPSTREAM_TRIM\s*\]")

    def test_call_site_uses_format_upstream(self):
        src = (_REPO / "src" / "tasks.py").read_text(encoding="utf-8")
        self.assertIn("format_upstream(d, upstream[d])", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)