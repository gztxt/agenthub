#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：主链判定（`src/chain_judge.py`）。

不碰 Ollama：`chain_judge._gen` 全部打桩，`local_judge.record` 也打桩（不写生产告警文件）。
测的是**方向与降级语义**，不是模型质量（质量见 `work/judge-quality-20261023/REPORT.md`）：

- 判错只能往「多一次人看」的方向错（off_topic ⇒ 转 submitted），不能往「放过」错；
- 判定没跑成（关闭/超短/幻觉摘录/两序不一致/队列忙）⇒ **完全旧行为**；
- 提位只在**同桶**内发生（跨桶会把设计参考顶成实现参考）。
"""
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import chain_judge  # noqa: E402
import local_judge  # noqa: E402

_OUT = ("def add(a, b):\n    return a + b\n\nprint(add(2, 3))  # 输出 5\n\n"
        "说明：函数接收两个参数并返回它们的和。")


class _Base(unittest.TestCase):
    def setUp(self):
        self._enabled = chain_judge.ENABLED
        chain_judge.ENABLED = True
        self._rec = mock.patch.object(local_judge, "record", lambda *a, **k: None)
        self._rec.start()
        self.addCleanup(self._rec.stop)
        self.addCleanup(self._restore)

    def _restore(self):
        chain_judge.ENABLED = self._enabled


class TestClassify(_Base):
    def test_disabled_is_skipped_and_never_calls_model(self):
        chain_judge.ENABLED = False
        with mock.patch.object(chain_judge, "_gen") as m:
            r = chain_judge.classify_output("任务", _OUT)
        self.assertFalse(r["ok"])
        self.assertTrue(r["skipped"])
        m.assert_not_called()

    def test_short_output_is_skipped(self):
        with mock.patch.object(chain_judge, "_gen") as m:
            r = chain_judge.classify_output("任务", "完成")
        self.assertFalse(r["ok"])
        self.assertTrue(r["skipped"])
        m.assert_not_called()

    def test_none_means_off_topic(self):
        with mock.patch.object(chain_judge, "_gen", return_value="NONE"):
            r = chain_judge.classify_output("写一个 add 函数", "今天天气不错。" * 10)
        self.assertTrue(r["ok"])
        self.assertEqual(r["label"], "off_topic")
        self.assertIsNone(r["evidence"])

    def test_verbatim_evidence_means_completed(self):
        with mock.patch.object(chain_judge, "_gen",
                               return_value="print(add(2, 3))  # 输出 5"):
            r = chain_judge.classify_output("写一个 add 函数并给示例", _OUT)
        self.assertTrue(r["ok"])
        self.assertEqual(r["label"], "completed")
        self.assertIn("add(2, 3)", r["evidence"])

    def test_whitespace_differences_still_count_as_verbatim(self):
        # 模型常把换行复述成空格：归一化后仍算「产物里有」
        with mock.patch.object(chain_judge, "_gen",
                               return_value="def add(a, b): return a + b"):
            r = chain_judge.classify_output("写一个 add 函数", _OUT)
        self.assertTrue(r["ok"])
        self.assertEqual(r["label"], "completed")

    def test_hallucinated_evidence_is_not_judged(self):
        # 产物里没有这句话 ⇒ 不许据此判 completed，也不许当成 off_topic 去拦人
        with mock.patch.object(chain_judge, "_gen",
                               return_value="所有单元测试均已通过并通过 CI"):
            r = chain_judge.classify_output("写一个 add 函数", _OUT)
        self.assertFalse(r["ok"])
        self.assertIn("幻觉", r["why"])

    def test_model_error_never_raises(self):
        with mock.patch.object(chain_judge, "_gen", side_effect=RuntimeError("boom")):
            r = chain_judge.classify_output("写一个 add 函数", _OUT)
        self.assertFalse(r["ok"])
        self.assertIn("RuntimeError", r["why"])


class TestQueueGuard(_Base):
    """Ollama 串行：另一路在跑时不许排队拖死主链（等不到就不判）。"""

    def test_busy_queue_skips_instead_of_waiting(self):
        self.assertTrue(chain_judge._SEM.acquire(timeout=0.1))
        self.addCleanup(chain_judge._SEM.release)
        old = chain_judge.QUEUE_WAIT_S
        chain_judge.QUEUE_WAIT_S = 0.01
        self.addCleanup(lambda: setattr(chain_judge, "QUEUE_WAIT_S", old))
        with mock.patch.object(chain_judge, "_gen") as m:
            r = chain_judge.classify_output("写一个 add 函数", _OUT)
        self.assertFalse(r["ok"])
        self.assertTrue(r["skipped"])
        self.assertIn("队列忙", r["why"])
        m.assert_not_called()


class TestPick(_Base):
    _CANDS = [{"name": "olive/weather", "desc": "天气预报 CLI"},
              {"name": "pdf2docx", "desc": "PDF 转 Word"},
              {"name": "pylogscan", "desc": "日志聚合与检索"}]

    def test_too_few_candidates_skipped(self):
        with mock.patch.object(chain_judge, "_gen") as m:
            r = chain_judge.pick_candidate("查日志", self._CANDS[:2])
        self.assertFalse(r["ok"])
        self.assertTrue(r["skipped"])
        m.assert_not_called()

    def test_agreement_returns_original_index(self):
        # 正序答 3、倒序答 1（都指向 pylogscan）⇒ 一致
        with mock.patch.object(chain_judge, "_gen", side_effect=["3\n日志", "1\n日志"]):
            r = chain_judge.pick_candidate("查日志里的 502", self._CANDS)
        self.assertTrue(r["ok"])
        self.assertTrue(r["agreed"])
        self.assertEqual(r["index"], 2)
        self.assertEqual(r["picked"], "pylogscan")

    def test_disagreement_is_not_judged(self):
        # 两次都「选第一个」= 位置偏置回来了 ⇒ 不许排序
        with mock.patch.object(chain_judge, "_gen", side_effect=["1\n", "1\n"]):
            r = chain_judge.pick_candidate("查日志里的 502", self._CANDS)
        self.assertFalse(r["ok"])
        self.assertIn("不一致", r["why"])

    def test_unparsable_answer_is_not_judged(self):
        with mock.patch.object(chain_judge, "_gen", side_effect=["我说不好", "我说不好"]):
            r = chain_judge.pick_candidate("查日志里的 502", self._CANDS)
        self.assertFalse(r["ok"])


class TestAcceptDecision(unittest.TestCase):
    """方向判据：模型只能**收紧**，不能放宽。"""

    def test_auto_accept_with_clean_judge(self):
        d = chain_judge.accept_decision(True, {"ok": True, "label": "completed"})
        self.assertTrue(d["accept"])
        self.assertFalse(d["review"])

    def test_off_topic_downgrades_to_review(self):
        for auto in (True, False):
            d = chain_judge.accept_decision(auto, {"ok": True, "label": "off_topic"})
            self.assertFalse(d["accept"])
            self.assertEqual(d["status"], "submitted")
            self.assertTrue(d["review"])
            # 复核的人必须能看出「不是 verifier 失败」
            self.assertIn("off_topic", d["reason"])
            self.assertIn("verifier", d["reason"])
            self.assertIn("结构判据已通过", d["reason"])

    def test_unjudged_keeps_old_behaviour(self):
        # 判定没跑成 = 接入前的行为：AUTO_ACCEPT 开就接受
        d = chain_judge.accept_decision(True, {"ok": False, "label": None})
        self.assertTrue(d["accept"])
        d2 = chain_judge.accept_decision(False, {"ok": False, "label": None})
        self.assertFalse(d2["accept"])
        self.assertFalse(d2["review"])


class TestPromote(unittest.TestCase):
    def test_promotes_to_bucket_head(self):
        rows = [{"repo": "i1", "bucket": "impl"}, {"repo": "i2", "bucket": "impl"},
                {"repo": "d1", "bucket": "design"}]
        self.assertTrue(chain_judge.promote_within_bucket(rows, 1))
        self.assertEqual([r["repo"] for r in rows], ["i2", "i1", "d1"])

    def test_cross_bucket_stays_in_its_bucket(self):
        rows = [{"repo": "i1", "bucket": "impl"}, {"repo": "d1", "bucket": "design"},
                {"repo": "d2", "bucket": "design"}]
        chain_judge.promote_within_bucket(rows, 2)
        # d2 只能到 design 段首，不能越过 impl
        self.assertEqual([r["repo"] for r in rows], ["i1", "d2", "d1"])

    def test_already_head_is_noop(self):
        rows = [{"repo": "i1", "bucket": "impl"}, {"repo": "i2", "bucket": "impl"}]
        self.assertFalse(chain_judge.promote_within_bucket(rows, 0))

    def test_out_of_range_is_noop(self):
        rows = [{"repo": "i1", "bucket": "impl"}]
        self.assertFalse(chain_judge.promote_within_bucket(rows, 5))
        self.assertEqual([r["repo"] for r in rows], ["i1"])


if __name__ == "__main__":
    unittest.main()
