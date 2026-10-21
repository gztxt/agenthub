# -*- coding: utf-8 -*-
"""评测脚本自身的口径测试（PT-20261021-04 / P2 ①）。

指标算错会让后续所有「优化」结论失效，所以这几个用例钉死三件事：
1. `hit@k` 的分母是**标注仓数**，不是 k；
2. `mrr` 只取 top-k 内的最好名次；
3. 未入池与排名靠后必须**可区分**（`target_ranks` 为 None vs 数字）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import eval_refscout  # noqa: E402


def _c(repo):
    return {"repo": repo}


class TestScore(unittest.TestCase):
    def test_all_hits_at_top(self):
        ranked = [_c("t/1"), _c("x/a"), _c("t/2")]
        s = eval_refscout._score(ranked, ["t/1", "t/2"], k=5)
        self.assertEqual(s["hit@k"], 1.0)
        self.assertEqual(s["mrr"], 1.0)
        self.assertEqual(s["prec@k"], round(2 / 5, 3))

    def test_rank_and_mrr(self):
        ranked = [_c("x/a"), _c("x/b"), _c("t/1")]
        s = eval_refscout._score(ranked, ["t/1"], k=5)
        self.assertEqual(s["target_ranks"]["t/1"], 3)
        self.assertEqual(s["mrr"], 0.333)

    def test_target_beyond_k_is_not_hit_but_ranked(self):
        ranked = [_c(f"x/{i}") for i in range(5)] + [_c("t/1")]
        s = eval_refscout._score(ranked, ["t/1"], k=5)
        self.assertEqual(s["hit@k"], 0.0)
        self.assertEqual(s["mrr"], 0.0)
        self.assertEqual(s["target_ranks"]["t/1"], 6, "池内名次要保留，否则无法区分召回失败")

    def test_absent_target_is_none(self):
        s = eval_refscout._score([_c("x/a")], ["t/1"], k=5)
        self.assertIsNone(s["target_ranks"]["t/1"])
        self.assertEqual(s["hits"], [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
