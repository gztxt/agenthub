"""B：难度地板抬到 t3 + 档内按**实测**成功率排序（2026-11-28 用户令）。

为什么地板要抬：本机 audit（`/api/tasks/tiers` 的 audit 段）实测首轮成功率
d1 72.5%(n=51) / d2 45.8%(n=59) / d3 **7.7%**(n=26)。d3 落 t2 时首轮几乎必败，
后续只能靠升档补救 ⇒ 起点直接 t3 更省（少一次失败 + 一次重跑）。

为什么排序要按实测：档内顺序原本是 magpie 派生的单价升序（声明）。声明决定
"进哪一档"，决定不了"档内先试谁"。实测分数用 Wilson 下界（n 小惩罚重），
样本 < 3 不入表 —— 不拿噪声换顺序，也不把"不知道"当成"更差"。
"""
import os
import unittest

import tiercfg as t


class TestFloor(unittest.TestCase):
    def test_difficulty_three_never_starts_below_t3(self):
        tiers = {"t1": ["a/1"], "t2": ["b/2"], "t3": ["c/3"]}
        self.assertEqual(t.tier_for(3, tiers=tiers), "t3")
        # 显式 mapping 把人往低档引，也要被地板拦住（地板是硬约束）
        self.assertEqual(t.tier_for(3, mapping={3: "t1"}, tiers=tiers), "t3")

    def test_floor_does_not_affect_easy_tasks(self):
        tiers = {"t1": ["a/1"], "t2": ["b/2"], "t3": ["c/3"]}
        self.assertEqual(t.tier_for(1, tiers=tiers), "t1")
        self.assertEqual(t.tier_for(2, tiers=tiers), "t2")


class TestMeasuredOrder(unittest.TestCase):
    def setUp(self):
        self.tiers = {"t1": ["a/1", "b/1", "c/1"]}
        self.cat = {"a/1", "b/1", "c/1"}

    def test_without_measurement_keeps_declared_order(self):
        m, why = t.first_usable("t1", self.tiers, self.cat, True, perf={})
        self.assertEqual((m, why), ("a/1", "ok"))

    def test_measurement_reorders(self):
        perf = {"a/1": {"score": 0.10, "n": 9},
                "b/1": {"score": 0.80, "n": 7},
                "c/1": {"score": 0.30, "n": 5}}
        m, _ = t.first_usable("t1", self.tiers, self.cat, True, perf=perf)
        self.assertEqual(m, "b/1")

    def test_unmeasured_models_keep_relative_order_after_measured(self):
        # 只有 b 有实测 ⇒ b 先，其余按声明序（a 在 c 前）
        perf = {"b/1": {"score": 0.5, "n": 4}}
        m, _ = t.first_usable("t1", self.tiers, self.cat, True, perf=perf)
        self.assertEqual(m, "b/1")

    def test_blocked_provider_still_wins_over_measurement(self):
        perf = {"b/1": {"score": 0.9, "n": 9}, "c/1": {"score": 0.1, "n": 9}}
        m, why = t.first_usable("t1", {"t1": ["b/1", "c/1"]}, {"b/1", "c/1"}, True,
                                blocked={"b": "quota"}, perf=perf)
        self.assertEqual(m, "c/1")
        self.assertEqual(why, "ok")

    def test_catalog_unavailable_never_returns_a_model(self):
        m, why = t.first_usable("t1", self.tiers, None, False, perf={})
        self.assertIsNone(m)
        self.assertEqual(why, "catalog_unavailable")


class TestPerfTable(unittest.TestCase):
    """`tier_perf.model_perf()` 只读终态 + 首轮（attempts=0），样本 < 3 不入表。"""

    class _FakeDB:
        def __init__(self, rows):
            self.rows = rows
            self.sql = ""

        def query(self, sql, params=()):
            self.sql = sql
            return self.rows

    def test_filters_min_n_and_computes_score(self):
        import tier_perf
        fake = self._FakeDB([
            {"m": "a/1", "n": 10, "ok": 8},
            {"m": "b/1", "n": 2, "ok": 2},      # 样本不足 ⇒ 不入表（不拿 2/2 当 100%）
            {"m": "", "n": 9, "ok": 1},          # 没模型 ID ⇒ 跳过
        ])
        out = tier_perf.model_perf(db=fake)
        self.assertIn("a/1", out)
        self.assertNotIn("b/1", out)
        self.assertNotIn("", out)
        self.assertAlmostEqual(out["a/1"]["rate"], 0.8)
        self.assertLess(out["a/1"]["score"], out["a/1"]["rate"], "Wilson 下界必须严于裸比率")
        self.assertIn("attempts", fake.sql, "分母只算首轮，否则重试成功会抬高低能模型")

    def test_wilson_is_monotonic_and_bounded(self):
        import tier_perf
        self.assertEqual(tier_perf.wilson_lower(0, 0), 0.0)
        lo_small = tier_perf.wilson_lower(3, 3)
        lo_big = tier_perf.wilson_lower(75, 100)
        self.assertGreater(lo_small, 0.0)
        self.assertLess(lo_small, 1.0)
        self.assertLess(lo_small, lo_big, "3/3 的下界必须低于 75/100（小样本惩罚）")

    def test_db_failure_returns_empty(self):
        import tier_perf
        class Boom:
            def query(self, *a, **k):
                raise RuntimeError("db closed")
        self.assertEqual(tier_perf.model_perf(db=Boom()), {},
                         "读不到 ⇒ 空表 ⇒ 保持声明序，绝不当成「更差」")


class TestSwitchSurvives(unittest.TestCase):
    def test_explicit_tier_env_still_wins(self):
        old = os.environ.get("TASK_TIERS")
        try:
            os.environ["TASK_TIERS"] = "t1=x/1;t2=y/1;t3=z/1"
            self.assertEqual(t.tier_for(3), "t3")
            self.assertEqual(t.parse_tiers()["t1"], ["x/1"])
        finally:
            if old is None:
                os.environ.pop("TASK_TIERS", None)
            else:
                os.environ["TASK_TIERS"] = old


if __name__ == "__main__":
    unittest.main()