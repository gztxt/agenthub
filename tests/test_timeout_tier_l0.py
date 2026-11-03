"""L0：超时按档位（`src/tiercfg.py` 的 parse_timeout_tiers / timeout_for_tier / prev_tier）。

判据（对齐 work/collab-mix-review/SPEC.md 判据 #1/#2，逐条可断言）：
  #1 超时按档：timeout_for_tier('t1')==300、('t3')==900、(None)==600、非法值 'abc' ⇒ 600；
              TASK_TIMEOUT_TIERS 为空 ⇒ 三条全 600。
  #2 超时降档：prev_tier('t3')=='t2'、('t2')=='t1'、('t1') is None、('explicit') is None、
              (None) is None；与 next_tier 严格相反。

本文件属 L0 hermetic：**零网络**、不 import `src.main`、不写 `data/*.db`。
回落值 600 = `TASK_TIMEOUT_S`（tasks_tiers.py:23）；为隔离 env，多数用例把
`tiercfg._fallback_timeout` 打成固定 600，另有一例验证「回落到真·TASK_TIMEOUT_S」的接线。
"""
import pathlib
import sys
import unittest
from unittest import mock

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

import tiercfg as t  # noqa: E402

#: 三档阶梯（与 test_tiers.py 同口径）。prev_tier 走 parse_tiers 的模型档位表，
#: 故需传一个含 t1/t2/t3 的表来固定顺序。
CFG = {"t1": ["cheap-a", "cheap-b"], "t2": ["mid-a"], "t3": ["strong-a"]}

#: 分档超时的标准配置串（判据 #1 用它）。
TIERS_RAW = "t1=300;t2=600;t3=900"


class TestParseTimeoutTiers(unittest.TestCase):
    def test_normal(self):
        """正常路径：'t1=300;t2=600;t3=900' → 三档字典。"""
        self.assertEqual(t.parse_timeout_tiers(TIERS_RAW),
                         {"t1": 300, "t2": 600, "t3": 900})

    def test_empty_gives_empty_dict(self):
        """边界·空配置：None/'' ⇒ {}（调用方据此回落 TASK_TIMEOUT_S，不是整表用默认）。"""
        self.assertEqual(t.parse_timeout_tiers(""), {})
        self.assertEqual(t.parse_timeout_tiers(None), {})
        self.assertEqual(t.parse_timeout_tiers("   "), {})

    def test_bad_item_does_not_kill_table(self):
        """边界·非法输入：单项格式错整项忽略，其余保留（对齐 parse_tiers 语义）。"""
        # 'broken' 无 '='、't2=abc' 秒数非整数、't3=0' 非正 ⇒ 三项全丢，只剩 t1。
        self.assertEqual(t.parse_timeout_tiers("t1=300;broken;t2=abc;t3=0"),
                         {"t1": 300})

    def test_negative_and_nonint_dropped(self):
        """边界·非法值：负数 / 非整数秒 ⇒ 丢弃该项。"""
        self.assertEqual(t.parse_timeout_tiers("t1=-5;t2=300.5;t3=900"),
                         {"t3": 900})

    def test_duplicate_last_wins(self):
        """边界·重复项：同名档位后者覆盖前者（最后一次赋值胜出）。"""
        self.assertEqual(t.parse_timeout_tiers("t1=300;t1=450"),
                         {"t1": 450})

    def test_deterministic_rank_order(self):
        """确定性排序：按档位 rank 升序（t1<t2<t10），不是字典序、不是插入序。"""
        out = t.parse_timeout_tiers("t10=1000;t2=600;t1=300")
        self.assertEqual(list(out.keys()), ["t1", "t2", "t10"])


class TestTimeoutForTier(unittest.TestCase):
    def setUp(self):
        # 隔离 env：把回落值钉成 600（= TASK_TIMEOUT_S 默认），让判据可复现。
        patcher = mock.patch.object(t, "_fallback_timeout", return_value=600)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_hit_tiers(self):
        """正常路径：命中档位取该档秒数（判据 #1 的 t1==300 / t3==900）。"""
        self.assertEqual(t.timeout_for_tier("t1", TIERS_RAW), 300)
        self.assertEqual(t.timeout_for_tier("t2", TIERS_RAW), 600)
        self.assertEqual(t.timeout_for_tier("t3", TIERS_RAW), 900)

    def test_none_tier_falls_back(self):
        """边界·档位为空：tier=None ⇒ 回落 600。"""
        self.assertEqual(t.timeout_for_tier(None, TIERS_RAW), 600)

    def test_illegal_value_falls_back(self):
        """边界·非法值：未配置的档位名 / 'explicit' / 'abc' ⇒ 回落 600。"""
        self.assertEqual(t.timeout_for_tier("abc", TIERS_RAW), 600)
        self.assertEqual(t.timeout_for_tier("explicit", TIERS_RAW), 600)
        self.assertEqual(t.timeout_for_tier("t9", TIERS_RAW), 600)

    def test_empty_config_all_fall_back(self):
        """边界·空配置：TASK_TIMEOUT_TIERS 为空 ⇒ 三条全 600（判据 #1 末条）。"""
        for tier in ("t1", "t3", None):
            self.assertEqual(t.timeout_for_tier(tier, ""), 600)

    def test_whitespace_tier_falls_back(self):
        """边界·档位带空白：' t1 ' strip 后仍命中；纯空白 ⇒ 回落。"""
        self.assertEqual(t.timeout_for_tier("  t1  ", TIERS_RAW), 300)
        self.assertEqual(t.timeout_for_tier("   ", TIERS_RAW), 600)

    def test_no_uncaught_exception_on_garbage(self):
        """兜底：喂各种垃圾输入都不得抛未捕获异常，一律回落 600。"""
        for bad in (None, "", "abc", 123, [], {}, object()):
            try:
                got = t.timeout_for_tier(bad, TIERS_RAW)  # type: ignore[arg-type]
            except Exception as e:  # noqa: BLE001
                self.fail("timeout_for_tier(%r) 抛了未捕获异常: %r" % (bad, e))
            self.assertEqual(got, 600)


class TestTimeoutFallbackWiring(unittest.TestCase):
    def test_fallback_is_real_task_timeout_s(self):
        """接线验证（不打桩）：空配置回落值 == 真·tasks_tiers.TASK_TIMEOUT_S（唯一定义处）。"""
        import tasks_tiers  # noqa: PLC0415  # 延迟 import，验证回落确实取自该模块
        self.assertEqual(t.timeout_for_tier(None, ""), tasks_tiers.TASK_TIMEOUT_S)
        self.assertEqual(t.timeout_for_tier("t_unknown", ""), tasks_tiers.TASK_TIMEOUT_S)


class TestPrevTier(unittest.TestCase):
    def test_downgrade(self):
        """正常路径·降档：t3→t2、t2→t1（判据 #2 前两条）。"""
        self.assertEqual(t.prev_tier("t3", CFG), "t2")
        self.assertEqual(t.prev_tier("t2", CFG), "t1")

    def test_bottom_is_none(self):
        """边界·首档：已在最低档 ⇒ None（不是首档本身、不是异常）。"""
        self.assertIsNone(t.prev_tier("t1", CFG))

    def test_unknown_tier_is_none(self):
        """边界·未知档位：'explicit' 不在阶梯 ⇒ None（判据 #2）。"""
        self.assertIsNone(t.prev_tier("explicit", CFG))

    def test_none_tier_is_none(self):
        """边界·None：prev_tier(None) ⇒ None（判据 #2）。"""
        self.assertIsNone(t.prev_tier(None, CFG))

    def test_strict_opposite_of_next_tier(self):
        """与 next_tier 严格相反：next 上去再 prev 下来应回到原档。"""
        for tier in ("t1", "t2"):
            up = t.next_tier(tier, CFG)
            self.assertIsNotNone(up)
            self.assertEqual(t.prev_tier(up, CFG), tier)
        # 顶档 next 为 None；底档 prev 为 None —— 两端对称。
        self.assertIsNone(t.next_tier("t3", CFG))
        self.assertIsNone(t.prev_tier("t1", CFG))

    def test_never_goes_up(self):
        """降档方向永不向上：prev_tier 的 rank 必须严格小于入参 rank。"""
        self.assertLess(t.rank(t.prev_tier("t3", CFG), CFG), t.rank("t3", CFG))
        self.assertLess(t.rank(t.prev_tier("t2", CFG), CFG), t.rank("t2", CFG))

    def test_default_cfg_when_tiers_none(self):
        """tiers=None 时走 parse_tiers() 默认表：t3→t2、t1→None 仍成立。"""
        self.assertEqual(t.prev_tier("t3"), "t2")
        self.assertIsNone(t.prev_tier("t1"))


if __name__ == "__main__":
    unittest.main()
