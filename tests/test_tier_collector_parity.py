#!/usr/bin/env python3
"""L0 元闸门：**标准层收集器对账**（2026-10-02，PT-20261002-10）。

要防的事故形态：闸门写成了 **unittest discovery 收不到**的样式 ——
本批实测 `pytest tests/` 975 例、`scripts/run_tests.sh all` 961 例，
差的 14 条是 `tests/test_term_touch_authurl.py` 的**模块级 pytest 风格** `def test_*()`。
后果不是"少跑几条"，而是**「标准套件全绿」与「新闸门跑过了」变成两句话** ——
报告里写"961 全绿"是真的，可它压根没覆盖本批新增的闸门。与同族前例一致：
vitals_loop 函数头丢失、前端 TDZ、test_asset_audit 的缩进错位，
都是「**代码存在 ≠ 会被执行**」。

本文件的三条闸门：

  ① 标准层不得存在 unittest 收不到的模块级 test_（pytest 风格裸函数）
  ② 每个 tests/test_*.py 都必须至少贡献 1 例（防"文件在但零收集"）
  ③ unittest 与 pytest 两个收集器的例数必须相等（换个 runner 不掉例）

★ 判据边界（2026-10-02 实测后收窄）：① 只管**模块顶层**的裸函数。那种写法
  pytest 收、unittest 不收，无条件成立。曾在语法上一并管「非 TestCase 类里的
  test_*」，**误报 216 条** —— 仓里的常规写法是 `class TestTransport(TdaiBase)`
  （TdaiBase 是本模块另一 TestCase 子类的别名，unittest 照收，实测 17/10/22 例）。
  **假红的闸门比没有闸门更坏**（会逼人去改断言求绿），故类级差异交给 ③ 兜：
  那条是运行期事实，没有语法推断的余地。

**每条都带自证**：拿临时目录造出违规样本，断言判据真的报出来。
不这么写的元闸门，绿得没有意义 —— 它可能只是压根没跑（这正是本文件要治的病）。
"""
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))

import tiers  # noqa: E402


class TestTierCollectorParity(unittest.TestCase):
    """★ 元闸门：标准层的收集口径必须可判，且判据本身要能被证伪。"""

    # ── ① 不得有 unittest 收不到的 test_ ──────────────────────────────────
    def test_no_collectable_only_test_functions_in_standard_tiers(self):
        bad = tiers.module_level_test_functions()
        self.assertEqual(bad, [],
                         "这些 test_ unittest discover 收不到 ⇒ 跑标准套件时它们**不会执行**：\n  "
                         + "\n  ".join(bad)
                         + "\n  改法：写进 unittest.TestCase，或移到 verify_*.py / probe_*.py（L2）。")

    def test_detector_actually_flags_a_pytest_style_function(self):
        """判据自证 ①：造一份模块级 test_ 的样本，判据必须报出来。"""
        self.assertTrue(self._violations_of('def test_x():\n    assert True\n'))
        # 负样本：合法写法不得误报（否则这条闸门会把人逼到假绿/假红）
        self.assertEqual(
            self._violations_of('import unittest\n\n\n'
                                'class T(unittest.TestCase):\n'
                                '    def test_x(self):\n        assert True\n'), [])
        self.assertEqual(self._violations_of('def helper():\n    return 1\n'), [])

    @staticmethod
    def _violations_of(body: str) -> list:
        """把一段源码当 tests/test_*.py 喂给判据，返回它报的违规。"""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "tests").mkdir()
            (root / "tests" / "test_sample.py").write_text(body, encoding="utf-8")
            return tiers.module_level_test_functions(root)

    # ── ② 每个标准层文件都得有份 ─────────────────────────────────────────
    def test_every_standard_test_file_yields_at_least_one_test(self):
        empty = []
        for p in tiers.standard_test_files():
            if tiers.file_collected_count(p.name) == 0:
                empty.append(p.name)
        self.assertEqual(empty, [],
                         "这些文件被 discovery 收进 0 例（拼写/类名/条件装饰导致的静默失效）：%s" % empty)

    def test_empty_file_really_collects_zero(self):
        """判据自证 ②：一个只有注释的文件确实是 0 例 —— 否则上面那条恒绿。"""
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "test_nothing.py").write_text("# 只有注释\n", encoding="utf-8")
            self.assertEqual(tiers.unittest.TestLoader().discover(td, pattern="test_*.py")
                             .countTestCases(), 0)

    # ── ③ 两个收集器收集到的**集合**必须相等 ─────────────────────────────
    def test_pytest_and_unittest_collect_the_same_number_of_tests(self):
        """两个 runner 都要既“数量相同”又“收的是同一批”。

        【2026-11-28 改判】旧实现用**进程内** `TestLoader().discover()` 对账，而这条
        用例是在跑完 4,300 例之后执行的 —— 那时 sys.modules / os.environ 已被前面的
        用例改过，discovery 看到的是“被污染后的进程”，不是 runner 口径：
        全量跑 4420 ≠ pytest 4421，单独跑却 4421 = 4421。同一个事实两种读数 ⇒
        判据本身不成立（不是用例少了，是尺子在量自己）。
        现改为**两边都开子进程**收，并且比集合而不只比数字：只比数字会漏掉
        “A 少一个、B 多一个、总数仍相等”的情况。
        """
        only_unit, only_pytest, why = tiers.collected_parity()
        if why:
            raise unittest.SkipTest("SKIP(host-dependent): %s" % why)
        self.assertEqual(
            (sorted(only_unit), sorted(only_pytest)), ([], []),
            "只 unittest 有：%s\n只 pytest 有：%s\n"
            "⇒ 有用例只被其中一个 runner 收进来，换台机器/换个入口就会静默少跑"
            "（详见 tests/tiers.py 收集器对账段）。" % (sorted(only_unit), sorted(only_pytest)))
        n_unit, _ = tiers.unittest_collected_ids()
        self.assertGreater(len(n_unit), 4000, "收集集合异常（少于 4000 例）——判据失效，不是绿")

    def test_collector_count_is_not_silently_zero(self):
        """判据自证 ③：对账两端都不能是 0（否则 '0==0' 也是绿）。"""
        self.assertGreater(tiers.unittest_collected_count(), 900)
        n_pytest, why = tiers.pytest_collected_count()
        if n_pytest is not None:
            self.assertGreater(n_pytest, 900)


if __name__ == "__main__":
    unittest.main(verbosity=2)