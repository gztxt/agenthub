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
from unittest import mock

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
    _ENV = ("JEV_DECISION_SCRIPT", "JUDGE_CACHE_PATH", "JUDGE_ALERT_PATH",
            "JUDGE_CACHE_TTL_S", "JUDGE_ALERT_AFTER")

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0lj-"))
        self._saved = {k: os.environ.get(k) for k in self._ENV}
        os.environ["JUDGE_CACHE_PATH"] = str(self.tmp / "judge_cache.json")
        os.environ["JUDGE_ALERT_PATH"] = str(self.tmp / "judge_alerts.jsonl")
        # 2026-10-23 起生产默认 LOCAL_JUDGE=0（本地模型已下线）。本文件测的是**启用时**
        # 的契约，故用例内强制打开；下线态另由 TestDisabled 覆盖，不改生产默认值。
        self._enabled_saved = local_judge.ENABLED
        local_judge.ENABLED = True
        # 本组是**子进程契约**用例：钉死 ollama 后端，绝不因为默认后端是 remote
        # 就去打真网关（L0 必须 hermetic，也避免测试烧 token）。
        self._backend_saved = local_judge.BACKEND
        local_judge.BACKEND = "ollama"
        local_judge.reset_stats()
        self.addCleanup(self._restore)

    def _restore(self):
        for k, v in self._saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        local_judge.ENABLED = self._enabled_saved
        local_judge.BACKEND = self._backend_saved
        local_judge.reset_stats()

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
        local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=20,
                          use_cache=False)
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


class TestCache(_Base):
    """2026-10-23：同样问题直接命中磁盘缓存；失败绝不缓存。"""

    def _counting_script(self):
        counter = self.tmp / "runs.txt"
        self._script(textwrap.dedent(f"""
            import json, pathlib, sys
            p = pathlib.Path({str(counter)!r})
            p.write_text(str(int(p.read_text()) + 1) if p.exists() else "1")
            print(json.dumps({{"answers": {{"q": {{"type": "noul", "noul": 0.5}}}},
                               "model": "fake"}}))
        """))
        return counter

    def test_same_question_and_state_runs_once(self):
        counter = self._counting_script()
        q = {"q": {"type": "noul", "instructions": "同一道题"}}
        a = local_judge.judge(q, state="同一份证据", timeout_s=20)
        b = local_judge.judge(q, state="同一份证据", timeout_s=20)
        self.assertFalse(a["cache"]["hit"])
        self.assertTrue(b["cache"]["hit"])
        self.assertEqual(counter.read_text(encoding="utf-8"), "1",
                         "同题同证据第二次仍跑了子进程 = 缓存没生效")

    def test_different_state_is_a_different_key(self):
        counter = self._counting_script()
        q = {"q": {"type": "noul", "instructions": "同一道题"}}
        local_judge.judge(q, state="证据A", timeout_s=20)
        out = local_judge.judge(q, state="证据B", timeout_s=20)
        self.assertFalse(out["cache"]["hit"])
        self.assertEqual(counter.read_text(encoding="utf-8"), "2")

    def test_failures_are_never_cached(self):
        self._script("import sys; sys.stderr.write('nope\\n'); sys.exit(2)")
        q = {"q": {"type": "noul", "instructions": "坏后端"}}
        for _ in range(2):
            with self.assertRaises(local_judge.LocalJudgeUnavailable):
                local_judge.judge(q, state="s", timeout_s=5)
        self.assertEqual(local_judge.status()["cache"]["hits"], 0)
        self.assertEqual(local_judge.status()["fail"], 2)

    def test_cache_hit_does_not_inflate_call_counters(self):
        self._counting_script()
        q = {"q": {"type": "noul", "instructions": "计数"}}
        local_judge.judge(q, state="s", timeout_s=20)
        local_judge.judge(q, state="s", timeout_s=20)
        st = local_judge.status()
        self.assertEqual(st["calls"], 1, "命中缓存不应算一次真调用")
        self.assertEqual(st["cache"]["hits"], 1)
        self.assertEqual(st["cache"]["misses"], 1)


class TestAlerts(_Base):
    """连续失败 → 告警 + 落盘；恢复 → 清告警并记一条 recovered。"""

    def test_three_consecutive_failures_alert_then_recover(self):
        self._script("import sys; sys.stderr.write('boom\\n'); sys.exit(4)")
        q = {"q": {"type": "noul", "instructions": "坏"}}
        for _ in range(3):
            with self.assertRaises(local_judge.LocalJudgeUnavailable):
                local_judge.judge(q, state="s", timeout_s=5)
        st = local_judge.status()
        self.assertTrue(st["alert"], "连续 3 次失败必须置 alert")
        self.assertEqual(st["consecutive_fail"], 3)
        lines = [json.loads(x) for x in
                 pathlib.Path(st["alert_path"]).read_text(encoding="utf-8").splitlines()]
        self.assertTrue(any(x.get("event") == "alert" for x in lines))
        self.assertEqual(local_judge.recent_alerts(3)[-1]["event"], "alert")

        self._script(_GOOD)
        local_judge.judge({"q": {"type": "noul", "instructions": "好"}},
                          state="s", timeout_s=20, use_cache=False)
        st2 = local_judge.status()
        self.assertFalse(st2["alert"], "成功后必须清告警")
        self.assertEqual(st2["consecutive_fail"], 0)
        self.assertTrue(any(x.get("event") == "recovered"
                            for x in local_judge.recent_alerts(5)))

    def test_below_threshold_does_not_alert(self):
        self._script("import sys; sys.exit(1)")
        for _ in range(local_judge.ALERT_AFTER - 1):
            with self.assertRaises(local_judge.LocalJudgeUnavailable):
                local_judge.judge({"q": {"type": "noul", "instructions": "x"}},
                                  state="s", timeout_s=5)
        self.assertFalse(local_judge.status()["alert"])


class TestDisabled(_Base):
    """`LOCAL_JUDGE=0` 仍是一键关（2026-10-23 晚接 remote 后端后保留）。

    关闭态必须是**显式不可用**：不静默返空答案、不起请求/子进程、不计失败/告警
    （否则运维指标里会出现「判定一直坏」的假象）。
    """

    def test_default_follows_backend(self):
        """remote 默认开、ollama 默认关——默认值必须按后端给，不能一律写死 0/1。"""
        src = (_REPO / "src" / "local_judge.py").read_text(encoding="utf-8")
        self.assertIn('os.getenv("LOCAL_JUDGE", "1" if BACKEND == "remote" else "0")', src,
                      "默认值不对：remote 关了等于判定没接；ollama 开了等于每轮必失败")

    def test_off_raises_explicitly_and_counts_nothing(self):
        local_judge.ENABLED = False
        before = local_judge.status()
        with self.assertRaises(local_judge.LocalJudgeUnavailable) as cm:
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)
        msg = str(cm.exception)
        self.assertIn("判定已关闭", msg)
        self.assertIn("LOCAL_JUDGE=1", msg, "必须告诉运维怎么复活")
        after = local_judge.status()
        self.assertEqual(after["calls"], before["calls"], "下线态不是一次调用")
        self.assertEqual(after["fail"], before["fail"], "下线态不许计失败")
        self.assertEqual(after["consecutive_fail"], before["consecutive_fail"])
        self.assertFalse(after["alert"])

    def test_off_does_not_run_script(self):
        marker = self.tmp / "ran.txt"
        self._script("open(%r, 'w').write('ran')" % str(marker))
        local_judge.ENABLED = False
        with self.assertRaises(local_judge.LocalJudgeUnavailable):
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, timeout_s=5)
        self.assertFalse(marker.exists(), "下线态仍起了子进程")

    def test_status_reports_enabled(self):
        local_judge.ENABLED = False
        self.assertFalse(local_judge.status()["enabled"])
        local_judge.ENABLED = True
        self.assertTrue(local_judge.status()["enabled"])


class TestRemoteDispatch(_Base):
    """remote 后端分派：只验证「接线与失败语义」，不发真请求（真质量见 work/ 报告）。"""

    def setUp(self):
        super().setUp()
        local_judge.BACKEND = "remote"

    def test_dispatch_returns_answers_and_counts_ok(self):
        fake = {"answers": {"q": {"type": "noul", "noul": 1.0}},
                "model": "deepseek/deepseek-flash",
                "usage": {"prompt_tokens": 11, "completion_tokens": 7, "calls": 1}}
        with mock.patch.object(local_judge.remote_judge, "judge", return_value=fake) as m:
            out = local_judge.judge({"q": {"type": "noul", "instructions": "在不在？"}},
                                    state="s", timeout_s=9, use_cache=False)
        m.assert_called_once()
        self.assertEqual(out["answers"]["q"]["noul"], 1.0)
        self.assertEqual(out["model"], "deepseek/deepseek-flash")
        st = local_judge.status()
        self.assertEqual(st["ok"], 1)
        self.assertEqual(st["backend"], "deepseek-flash")
        self.assertEqual(st["model"], "deepseek/deepseek-flash")

    def test_remote_failure_maps_to_unavailable_with_kind(self):
        err = local_judge.remote_judge.RemoteJudgeUnavailable("网关超时", kind="timeout")
        with mock.patch.object(local_judge.remote_judge, "judge", side_effect=err):
            with self.assertRaises(local_judge.LocalJudgeUnavailable):
                local_judge.judge({"q": {"type": "noul", "instructions": "x"}},
                                  state="s", timeout_s=5, use_cache=False)
        st = local_judge.status()
        self.assertEqual(st["fail"], 1)
        self.assertEqual(st["last_fail_kind"], "timeout")

    def test_cache_hit_does_not_call_remote_twice(self):
        fake = {"answers": {"q": {"type": "noul", "noul": 0.5}},
                "model": "m", "usage": {}}
        with mock.patch.object(local_judge.remote_judge, "judge", return_value=fake) as m:
            local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, state="s")
            second = local_judge.judge({"q": {"type": "noul", "instructions": "x"}}, state="s")
        self.assertEqual(m.call_count, 1, "第二次必须命中磁盘缓存")
        self.assertTrue(second["cache"]["hit"])

    def test_status_reports_remote_config_without_network(self):
        st = local_judge.status()
        self.assertEqual(st["backend_id"], "remote")
        self.assertTrue(st["ready"], "网关地址非空即配置在位")
        self.assertEqual(st["remote"]["model"], local_judge.remote_judge.MODEL)
        self.assertIsNone(st["script"], "remote 后端不该再报技能脚本路径")


if __name__ == "__main__":
    unittest.main()
