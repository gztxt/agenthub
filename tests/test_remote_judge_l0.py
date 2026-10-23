#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：远端判定后端（`src/remote_judge.py`，DeepSeek-Flash）。

## 测什么

1. **三原语契约**：noul/score/choice 的 prompt 组装与解析，与已测版本
   （`zerocost_jev._ask`）逐字段对齐——`{"p"}` / `{"score","label"}` /
   `{"probabilities"}` → `answers[名字]` 的形状。
2. **失败必须可分辨**：HTTP 错误 / 超时 / 非 JSON / 正文为空 / **长度截断**
   分别是不同 `kind`。截断单独判失败，因为「解析出半个对象」会被 `_clamp01`
   补成 0，看起来像「模型判 0」而不是「这次没判成」。
3. **不联网**：`_post` 是唯一出口，全部用例打桩；任何用例真发请求即视为污染。

口径来源：`agent-knowledge/250`、`work/judge-paid-20261023/REPORT.md`。
"""
import json
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import remote_judge  # noqa: E402


def _resp(content: str, finish: str = "stop", model: str = "deepseek-flash") -> dict:
    return {"model": model,
            "choices": [{"message": {"role": "assistant", "content": content},
                         "finish_reason": finish}],
            "usage": {"prompt_tokens": 10, "completion_tokens": 8}}


class TestPrimitives(unittest.TestCase):
    def test_noul_parses_p_and_clamps(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp('{"p": 1.7, "confidence": 0.9, "reason": "ok"}')):
            ans, u = remote_judge.ask_one({"type": "noul", "instructions": "装了吗？"}, "证据")
        self.assertEqual(ans["type"], "noul")
        self.assertEqual(ans["noul"], 1.0, "越界概率必须 clamp 到 1")
        self.assertEqual(ans["confidence"], 0.9)
        self.assertEqual(u["prompt_tokens"], 10)

    def test_noul_accepts_markdown_fence(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp('```json\n{"p": 0.2}\n```')):
            ans, _ = remote_judge.ask_one({"type": "noul", "instructions": "x"})
        self.assertEqual(ans["noul"], 0.2)

    def test_score_keeps_label(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp('{"score": 0.5, "label": "中", "confidence": 0.7}')):
            ans, _ = remote_judge.ask_one({"type": "score", "instructions": "打分",
                                           "criteria": ["低", "中", "高"]})
        self.assertEqual(ans["score"], 0.5)
        self.assertEqual(ans["label"], "中")

    def test_choice_probabilities_normalized_and_picked(self):
        body = '{"probabilities": {"a": 2, "b": 1, "c": 1}, "confidence": 0.8}'
        with mock.patch.object(remote_judge, "_post", return_value=_resp(body)):
            ans, _ = remote_judge.ask_one({"type": "choice", "instructions": "选",
                                           "criteria": {"a": "甲", "b": "乙", "c": "丙"}})
        self.assertEqual(ans["choice"], "a")
        self.assertAlmostEqual(sum(ans["probabilities"].values()), 1.0, places=3)
        # 单值先 clamp 到 0..1 再归一（与 zerocost_jev 原实现一致）：
        # 2,1,1 → 1,1,1 → 各 1/3。**不是** 2/4=0.5。
        self.assertAlmostEqual(ans["probabilities"]["a"], round(1 / 3, 4), places=4)

    def test_choice_accepts_list_criteria(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp('{"probabilities": {"x": 1, "y": 0}}')):
            ans, _ = remote_judge.ask_one({"type": "choice", "instructions": "选",
                                           "criteria": ["x", "y"]})
        self.assertEqual(ans["choice"], "x")

    def test_choice_without_options_is_bad_question(self):
        with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
            remote_judge.ask_one({"type": "choice", "instructions": "选"})
        self.assertEqual(cm.exception.kind, "bad_question")

    def test_unknown_type_is_bad_question(self):
        with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
            remote_judge.ask_one({"type": "bogus", "instructions": "x"})
        self.assertEqual(cm.exception.kind, "bad_question")


class TestFailureKinds(unittest.TestCase):
    def test_truncated_is_not_parsed(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp('{"p": 0.', finish="length")):
            with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
                remote_judge.generate("x")
        self.assertEqual(cm.exception.kind, "truncated")

    def test_empty_content_is_empty_kind(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp("", model="nemotron-content-safety")):
            with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
                remote_judge.generate("x")
        self.assertEqual(cm.exception.kind, "empty")

    def test_reasoning_content_is_used_when_content_empty(self):
        body = {"model": "deepseek-flash", "choices": [{"message": {
            "role": "assistant", "content": "", "reasoning_content": "{\"p\":0.4}"},
            "finish_reason": "stop"}], "usage": {}}
        with mock.patch.object(remote_judge, "_post", return_value=body):
            txt, _ = remote_judge.generate("x")
        self.assertIn("0.4", txt)

    def test_non_json_is_parse_kind(self):
        with mock.patch.object(remote_judge, "_post",
                               return_value=_resp("我觉得应该装上了")):
            with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
                remote_judge.ask_one({"type": "noul", "instructions": "x"})
        self.assertEqual(cm.exception.kind, "parse")

    def test_http_error_is_http_kind(self):
        import urllib.error
        err = urllib.error.HTTPError("u", 401, "Unauthorized", {}, None)
        with mock.patch.object(remote_judge, "_post", side_effect=err):
            with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
                remote_judge.generate("x")
        self.assertEqual(cm.exception.kind, "http")
        self.assertIn("401", str(cm.exception))

    def test_socket_timeout_is_timeout_kind(self):
        with mock.patch.object(remote_judge, "_post", side_effect=TimeoutError("timed out")):
            with self.assertRaises(remote_judge.RemoteJudgeUnavailable) as cm:
                remote_judge.generate("x")
        self.assertEqual(cm.exception.kind, "timeout")


class TestJudgeRound(unittest.TestCase):
    def test_partial_failure_raises_whole_round(self):
        """一题失败就整轮失败：缺一题的答案集不能当成功（调用方会按题名取）。"""
        ok = _resp('{"p": 1}')
        bad = _resp("not json")
        seq = [ok, bad]
        with mock.patch.object(remote_judge, "_post", side_effect=lambda *a, **k: seq.pop(0)):
            with self.assertRaises(remote_judge.RemoteJudgeUnavailable):
                remote_judge.judge({"a": {"type": "noul", "instructions": "x"},
                                    "b": {"type": "noul", "instructions": "y"}})

    def test_usage_sums_across_questions(self):
        with mock.patch.object(remote_judge, "_post", return_value=_resp('{"p": 1}')):
            out = remote_judge.judge({"a": {"type": "noul", "instructions": "x"},
                                      "b": {"type": "noul", "instructions": "y"}})
        self.assertEqual(set(out["answers"]), {"a", "b"})
        self.assertEqual(out["usage"]["calls"], 2)
        self.assertEqual(out["usage"]["prompt_tokens"], 20)
        self.assertEqual(out["model"], remote_judge.MODEL)

    def test_empty_questions_rejected(self):
        with self.assertRaises(remote_judge.RemoteJudgeUnavailable):
            remote_judge.judge({})


class TestTokenBudget(unittest.TestCase):
    """判定与通用生成用不同预算：判定 1200（JSON 很小），生成 4000（摘要会写长）。

    回归点（2026-10-23 实测）：两处共用 1200 时 `sum-xerj` 摘要被截断，
    `finish_reason=length` ⇒ 按失败处理，关键项命中直接掉到 0/5。
    """

    def test_judge_and_generate_have_separate_budgets(self):
        seen = []

        def spy(body, timeout_s):
            seen.append(body["max_tokens"])
            return _resp('{"p": 1}')

        with mock.patch.object(remote_judge, "_post", side_effect=spy):
            remote_judge.ask_one({"type": "noul", "instructions": "x"})
            remote_judge.generate("y")
        self.assertEqual(seen, [remote_judge.MAX_TOKENS, remote_judge.GEN_MAX_TOKENS])
        self.assertLess(remote_judge.MAX_TOKENS, remote_judge.GEN_MAX_TOKENS)

    def test_explicit_max_tokens_wins(self):
        seen = []

        def spy(body, timeout_s):
            seen.append(body["max_tokens"])
            return _resp("ok")

        with mock.patch.object(remote_judge, "_post", side_effect=spy):
            remote_judge.generate("y", max_tokens=777)
        self.assertEqual(seen, [777])


if __name__ == "__main__":
    unittest.main()
