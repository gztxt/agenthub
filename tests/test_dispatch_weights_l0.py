"""L0：派发权重的三把尺子（`src/dispatch_weights.py`）。

判据来源：选型层此前**没有任何一处**读历史成绩（`difficulty`→档位是写死映射，
`TASK_ESCALATE_LADDER` 是手填常量）。实测 30 天：`codex` 吃掉 80.8% 的 token，
而它在改前端文件这类任务上 6 次秒挂（平均 2.5 秒）。事实在库里，选型读不到。

本文件钉的是「尺子本身别骗人」，四条：
  · 质量用 Wilson 下界 ⇒ 小样本不许靠运气排第一；
  · 效率用 p50 ⇒ 不被单次 600s 超时拉爆；
  · 成本用「每成功一次 token」⇒ 干得多但大多失败的组合不该显得便宜；
  · 任务族必须分开算 ⇒ 否则跨族混算得到「谁都不该用」。

**不测 `compute()` 的库里数据**（L0 不读生产库），只测聚合与归一这些纯函数。
"""
import pathlib
import sys
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

import dispatch_weights as dw  # noqa: E402


class TestWilsonLower(unittest.TestCase):
    def test_ratio_alone_would_rank_luck_first(self):
        # 2/2 = 100% vs 35/45 = 78%：裸比率会把运气排前面，Wilson 下界必须反过来
        luck = dw.wilson_lower(2, 2)
        solid = dw.wilson_lower(35, 45)
        self.assertLess(luck, solid)
        self.assertGreater(solid, 0.63)   # 78% 的下界仍在 0.63 以上
        self.assertLess(luck, 0.35)       # 2/2 的下界被样本量压到 0.35 以下

    def test_zero_and_empty(self):
        self.assertEqual(dw.wilson_lower(0, 0), 0.0)
        self.assertEqual(dw.wilson_lower(0, 10), 0.0)

    def test_monotone_in_n(self):
        # 同样的比率，样本越多下界越高（这是它能当排名尺子的根本原因）
        self.assertLess(dw.wilson_lower(5, 10), dw.wilson_lower(50, 100))

    def test_never_above_one(self):
        self.assertLessEqual(dw.wilson_lower(1000, 1000), 1.0)
        self.assertGreater(dw.wilson_lower(1000, 1000), 0.99)


class TestP50(unittest.TestCase):
    def test_mean_would_be_ruined_by_timeout(self):
        # 实测：超时熔断 6 次、平均 103s、最长 600s。p50 必须不被那个 600s 拖走
        vals = [2, 2, 3, 3, 4, 600000]
        self.assertEqual(dw._pct(vals, 0.5), 3.0)
        self.assertGreater(sum(vals) / len(vals), 100000)

    def test_empty_is_none_not_zero(self):
        """没数据不能返回 0 —— 0 毫秒会被归一成「最快」，直接污染排名。"""
        self.assertIsNone(dw._pct([], 0.5))
        self.assertIsNone(dw._pct([None, None], 0.5))

    def test_single_value(self):
        self.assertEqual(dw._pct([42], 0.5), 42.0)


class TestNormalize(unittest.TestCase):
    def test_higher_better(self):
        got = dw._normalize({"a": 1.0, "b": 5.0}, higher_better=True)
        self.assertEqual(got, {"a": 0.0, "b": 1.0})

    def test_lower_better(self):
        got = dw._normalize({"a": 1.0, "b": 5.0}, higher_better=False)
        self.assertEqual(got, {"a": 1.0, "b": 0.0})

    def test_none_is_zero_not_best(self):
        # None（无数据）在「越低越好」里若被当极小值就会变成最优 —— 显式钉死为 0
        got = dw._normalize({"a": None, "b": 5.0, "c": 9.0}, higher_better=False)
        self.assertEqual(got["a"], 0.0)

    def test_all_equal_midpoint(self):
        self.assertEqual(dw._normalize({"a": 3, "b": 3}, higher_better=True), {"a": 0.5, "b": 0.5})


class TestLogNormalize(unittest.TestCase):
    def test_linear_would_call_100x_nearly_equal(self):
        """实测：194 vs 87514 vs 364811 token。线性归一给 87514 打 0.76 分 ⇒ 贵 450 倍读成「差一点」。"""
        vals = {"cheap": 194.0, "mid": 87514.0, "dear": 364811.0}
        lin = dw._normalize(vals, higher_better=False)
        log = dw._normalize_log(vals)
        self.assertGreater(lin["mid"], 0.7)      # 线性：中间那个几乎满分
        self.assertLess(log["mid"], 0.35)        # 对数：中间那个该被明显压低

    def test_none_still_zero(self):
        self.assertEqual(dw._normalize_log({"a": None, "b": 100.0})["a"], 0.0)

    def test_zero_value_defined(self):
        # log1p(0)=0，不能因为取对数就把 0 变成 -inf
        got = dw._normalize_log({"fast": 0.0, "slow": 1000.0})
        self.assertEqual(got["fast"], 1.0)


class TestTaskFamily(unittest.TestCase):
    def test_synthesis_by_id(self):
        self.assertEqual(dw.task_family({"task_id": "t_sum", "prompt": "改代码"}), dw.FAMILY_SYNTH)
        self.assertEqual(dw.task_family({"task_id": "t_sum2", "prompt": ""}), dw.FAMILY_SYNTH)

    def test_files_declared_means_code(self):
        self.assertEqual(dw.task_family({"task_id": "t1", "files": '["src/a.py"]',
                                         "prompt": "看看这个"}), dw.FAMILY_CODE)

    def test_code_verbs_without_files(self):
        self.assertEqual(dw.task_family({"task_id": "t1", "prompt": "修复登录页滚动"}),
                         dw.FAMILY_CODE)

    def test_research_fallback(self):
        self.assertEqual(dw.task_family({"task_id": "t1", "prompt": "检索相关文档"}),
                         dw.FAMILY_RESEARCH)

    def test_bad_files_json_is_tolerated(self):
        self.assertEqual(dw.task_family({"task_id": "t1", "files": "{not json",
                                         "prompt": "检索"}), dw.FAMILY_RESEARCH)


class TestAgg(unittest.TestCase):
    def _row(self, agent, status="success", dur=100, tok=1000, att=0, model="m1",
             tid="t1", fam=None):
        return {"agent_id": agent, "model_sent": model, "model": None, "status": status,
                "duration_ms": dur, "spent_tokens": tok, "attempts": att,
                "task_id": tid, "prompt": "改代码", "files": '["a.py"]',
                "last_fail_kind": None, "created_at": "2026-11-03"}

    def test_cost_is_per_success_not_per_task(self):
        """干得多但大多失败的组合，不该因为 token 分母大显得便宜。"""
        rows = ([self._row("busy", "success", tok=100)] * 1
                + [self._row("busy", "failed", tok=100)] * 9)
        got = dw._agg(rows)[0]
        self.assertEqual(got["tokens_per_success"], 1000)  # (100*10)/1 次成功
        self.assertEqual(got["success"], 1)

    def test_efficiency_ignores_failed_durations(self):
        # 失败任务常是超时（600s），把它计入 p50 会变成「谁最倒霉」
        rows = [self._row("a", "success", dur=10), self._row("a", "success", dur=20),
                self._row("a", "failed", dur=600000)]
        got = dw._agg(rows)[0]
        self.assertEqual(got["p50_ms"], 15.0)

    def test_no_success_means_no_cost_ratio(self):
        got = dw._agg([self._row("dead", "failed", tok=5000)])[0]
        self.assertIsNone(got["tokens_per_success"])

    def test_model_comes_from_model_sent(self):
        # 观测值 model 是回包可能被上游改写的；权重必须按「我们下发的」算
        rows = [dict(self._row("a", model="planned"), **{"model": "rewritten"})]
        self.assertEqual(dw._agg(rows)[0]["model"], "planned")

    def test_low_sample_flagged(self):
        got = dw._agg([self._row("a") for _ in range(3)])[0]
        self.assertTrue(got["low_sample"])
        got = dw._agg([self._row("a") for _ in range(dw.MIN_SAMPLE)])[0]
        self.assertFalse(got["low_sample"])

    def test_fail_kinds_top5(self):
        rows = [self._row("a", "failed", tok=0) for _ in range(3)]
        for r in rows:
            r["last_fail_kind"] = "timeout"
        got = dw._agg(rows)[0]
        self.assertEqual(got["fail_kinds"], {"timeout": 3})


class TestWeightsMeta(unittest.TestCase):
    def test_weights_sum_to_one(self):
        self.assertAlmostEqual(sum(dw.WEIGHTS.values()), 1.0)

    def test_suggest_returns_none_without_evidence(self):
        # 空族 ⇒ None（调用方必须回落既有选型，不能拿空结果当「随便挑一个」）
        self.assertIsNone(dw.suggest({"claude-code"}, family="no_such_family", by_family={}))

    def test_suggest_skips_low_sample_and_unpicked_agents(self):
        table = {"code": [
            {"agent_id": "ghost", "quality": 0.9, "low_sample": False},   # 不在池里
            {"agent_id": "lucky", "quality": 0.8, "low_sample": True},    # 样本不足
            {"agent_id": "solid", "quality": 0.7, "low_sample": False},   # 唯一够格
        ]}
        got = dw.suggest({"lucky", "solid"}, family="code", by_family=table)
        self.assertEqual(got["agent_id"], "solid")


if __name__ == "__main__":
    unittest.main()