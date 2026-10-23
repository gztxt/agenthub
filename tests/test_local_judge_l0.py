#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：本地模型判定（`src/local_judge.py`，2026-10-23 替代已下线外部 jev）。

## 测什么

1. **契约**：stdin 进 `{"state", "questions"}`、stdout 出 `{"answers", ...}`；
   任何失败形态（脚本不在 / 退出非零 / 输出 error / 非 JSON / answers 空 / 超时）
   一律抛 `LocalJudgeUnavailable` —— 调用方必须能区分「判完了」与「没判成」。
2. **零网络**：源码级断言不 import 任何 HTTP 客户端（判定在本机 Ollama 上跑，
   经技能子进程，hub 侧不发请求）。
3. **状态可观测**：`status()` 的 ready/script/counters 形状，供 /api/skill/status
   与 /api/vitals 回显。

全部用例把 `JEV_DECISION_SCRIPT` 指向 tmp 假脚本 ⇒ 不依赖技能是否真的装着、
不依赖 Ollama 在不在，符合 L0「干净机器上可跑、零 SKIP」。
"""
import json
import os
import pathlib
import sys
import tempfile
import textwrap
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import local_judge  # noqa: E402

_GOOD = textwrap.dedent("""
    import sys, json
    print(json.dumps({"answers": {"q": {"type": "choice",
        "probabilities": {"alpha": 0.8, "beta": 0.2}, "choice": "alpha",
        "confidence": 0.9, "reasoning": "fake"}},
        "model": "fake-local-judge", "usage": {"prompt_tokens": 7, "completion_tokens": 3}},
        ensure_ascii=False))
""")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0lj-"))
        self._saved = os.environ.get("JEV_DECISION_SCRIPT")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("JEV_DECISION_SCRIPT", None)
        else:
            os.environ["JEV_DECISION_SCRIPT"] = self._saved

    def _script(self, body: str, name: str = "fake_decision.py") -> pathlib.Path:
        p = self.tmp / name
        p.write_text(body, encoding="utf-8")
        os.environ["JEV_DECISION_SCRIPT"] = str(p)
        return p


class TestContract(_Base):
    def test_happy_path_returns_answers_and_model(self):
        self._script(_GOOD)
        out = local_judge.judge({"q": {"type": "choice", "instructions": "选一个",
                                       "criteria": {"alpha": "甲", "beta": "乙"}}},
                                state="背景", timeout_s=20)
        a = out["answers"]["q"]
        self.assertEqual(a["choice"], "alpha")
        self.assertAlmostEqual(a["probabilities"]["beta"], 0.2)
        self.assertEqual(out["model"], "fake-local-judge")
        self.assertEqual(out["usage"]["completion_tokens"], 3)

    def test_payload_shape_on_stdin(self):
        """技能入口的契约：stdin 必须是 {"state", "questions"}，题面原样透传。"""
        seen = self.tmp / "seen.json"
        self._script(textwrap.dedent(f"""
            import sys, json
            open({str(seen)!r}, "w").write(sys.stdin.read())
            print(json.dumps({{"answers": {{"q": {{"type": "noul", "noul": 0.5,
                                                  "confidence": 0.5}}}}}}))
        """))
        qs = {"q": {"type": "noul", "instructions": "在不在？",
                    "criteria": {"true": "在", "false": "不在"}}}
        local_judge.judge(qs, state="证据JSON", timeout_s=20)
        payload = json.loads(seen.read_text(encoding="utf-8"))
        self.assertEqual(set(payload), {"state", "questions"})
        self.assertEqual(payload["state"], "证据JSON")
        self.assertEqual(payload["questions"]["q"]["instructions"], "在不在？")

    def test_judge_one_wraps_single_question(self):
        self._script(_GOOD)
        out = local_judge.judge_one({"type": "choice", "instructions": "x",
                                     "criteria": {"alpha": "甲", "beta": "乙"}},
                                    timeout_s=20)
        self.assertIn("q", out["answers"])

    def test_multi_question_answers_keyed_by_name(self):
        """answers 必须按题名对齐（不按序）——调用方按名取，防串位。"""
        self._script(textwrap.dedent("""
            import sys, json
            print(json.dumps({"answers": {"b": {"type": "noul", "noul": 0.1},
                                          "a": {"type": "noul", "noul": 0.9}}}))
        """))
        out = local_judge.judge({"a": {"type": "noul", "instructions": "甲？"},
                                 "b": {"type": "noul", "instructions": "乙？"}},
                                timeout_s=20)
        self.assertAlmostEqual(out["answers"]["a"]["noul"], 0.9)
        self.assertAlmostEqual(out["answers"]["b"]["noul"], 0.1)


class TestFailureModes(_Base):
    """每种失败都必须抛 LocalJudgeUnavailable，且原因可读。"""

    def test_missing_script(self):
        os.environ["JEV_DECISION_SCRIPT"] = str(self.tmp / "nope.py")
        with self.assertRaises(local_judge.LocalJudgeUnavailable) as cm:
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)
        self.assertIn("不在", str(cm.exception))

    def test_nonzero_exit(self):
        self._script("import sys; sys.stderr.write('boom\\n'); sys.exit(3)")
        with self.assertRaises(local_judge.LocalJudgeUnavailable) as cm:
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)
        self.assertIn("3", str(cm.exception))

    def test_error_json(self):
        self._script('import json; print(json.dumps({"error": "模型不在"}))')
        with self.assertRaises(local_judge.LocalJudgeUnavailable) as cm:
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)
        self.assertIn("模型不在", str(cm.exception))

    def test_non_json_stdout(self):
        self._script("print('我不是 JSON')")
        with self.assertRaises(local_judge.LocalJudgeUnavailable):
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)

    def test_empty_answers(self):
        self._script('import json; print(json.dumps({"answers": {}}))')
        with self.assertRaises(local_judge.LocalJudgeUnavailable):
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)

    def test_timeout(self):
        self._script("import time; time.sleep(30)")
        with self.assertRaises(local_judge.LocalJudgeUnavailable) as cm:
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=0.5)
        self.assertIn("超时", str(cm.exception))

    def test_empty_questions_is_a_bug_not_a_call(self):
        with self.assertRaises(local_judge.LocalJudgeUnavailable):
            local_judge.judge({}, timeout_s=5)


class TestStatusAndNoNetwork(_Base):
    def test_status_shape_and_ready_follows_script_presence(self):
        os.environ["JEV_DECISION_SCRIPT"] = str(self.tmp / "nope.py")
        st = local_judge.status()
        self.assertEqual(st["backend"], "ollama-local")
        self.assertFalse(st["ready"])
        self.assertIn("note", st)
        self._script(_GOOD)
        self.assertTrue(local_judge.status()["ready"])

    def test_counters_move_on_real_calls(self):
        self._script(_GOOD)
        before = local_judge.status()
        local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=20)
        after = local_judge.status()
        # _STAT 是模块级累计量（同进程内多用例共享）⇒ 只断言增量，不断言绝对值
        self.assertEqual(after["calls"], before["calls"] + 1)
        self.assertEqual(after["ok"], before["ok"] + 1)
        self.assertEqual(after["model"], "fake-local-judge")
        self.assertIsNotNone(after["last_ok_ts"])

    def test_no_http_client_in_source(self):
        """判定在本机跑：hub 侧模块不许 import 任何 HTTP 客户端。"""
        src = (_REPO / "src" / "local_judge.py").read_text(encoding="utf-8")
        for bad in ("import aiohttp", "import requests", "urlopen", "httpx"):
            self.assertNotIn(bad, src, "local_judge 不该依赖 HTTP 客户端：%s" % bad)

    def test_default_script_points_at_jev_decision_skill(self):
        p = local_judge.DEFAULT_SCRIPT
        self.assertTrue(str(p).endswith("jev-decision/scripts/decision.py"),
                        "默认脚本路径漂了：%s" % p)


if __name__ == "__main__":
    unittest.main()
