"""L0：run_tier 默认把 HOME 隔到一次性空目录（不写真实 ~/.codebuddy / ~/.claude）。

判例背景（2026-11-02）：上一轮排障发现用户历史列表里多了 4 条测试会话
（cb-par-test / 2 / 3 + 一条 UUID，首句都是「调用 mcp__parallel__web_research：
input=HTTP 418…」）。根因不是某条用例写错，而是**跑 L0 时继承了真实 HOME**：
任何一次 `codebuddy --session-id X` 都会落成 ~/.codebuddy/projects/<真实项目>/X.jsonl。
`--fake-home` 早就存在但默认关着 ⇒ 谁都不加。这里把这个默认值钉死，防止回退。
"""
from __future__ import annotations

import contextlib
import importlib.util
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]


def _load_run_tier():
    spec = importlib.util.spec_from_file_location("run_tier_under_test", REPO / "scripts" / "run_tier.py")
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class HomeIsolationL0(unittest.TestCase):
    def setUp(self):
        self.rt = _load_run_tier()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "HUB_TEST_HOME", "HUB_HOST_TESTS")}
        self.real = tempfile.mkdtemp(prefix="rt-realhome-")

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v

    def test_isolate_home_points_children_at_temp_dir(self):
        """断言的是「子进程看到什么」，不是「本进程变量被改了」——写盘的是子进程。"""
        tmp = self.rt.isolate_home(self.real, log=lambda *_: None)
        self.assertNotEqual(os.environ["HOME"], self.real)
        self.assertTrue(tmp.startswith(self.real + os.sep))
        self.assertEqual(os.environ["HUB_TEST_HOME"], tmp)
        self.assertEqual(os.environ["HUB_HOST_TESTS"], "0")
        self.assertTrue(Path(tmp).is_dir())
        out = subprocess.run([sys.executable, "-c", "import os;print(os.path.expanduser('~'))"],
                             capture_output=True, text=True, timeout=30)
        self.assertEqual(out.stdout.strip(), tmp, "子进程的 ~ 必须落在隔离目录里")

    def test_restore_puts_home_back(self):
        self.rt.isolate_home(self.real, log=lambda *_: None)
        self.rt.restore_home(self.real, self.saved["HUB_HOST_TESTS"])
        self.assertEqual(os.environ["HOME"], self.real)
        self.assertNotIn("HUB_TEST_HOME", os.environ)
        # 幂等：同一个清理再跑一遍不应该炸
        self.rt.restore_home(self.real, self.saved["HUB_HOST_TESTS"])

    def test_main_isolates_for_l0_and_restores_after(self):
        """跑 L0 的**那一刻** HOME 必须是临时目录；main 返回后必须还原。

        用替身替掉发现器与执行器：这里测的是默认值的接线，不是再跑一遍 3600 例。
        """
        seen = {}

        def fake_run(tests, label, stream=None):
            seen["home"] = os.environ.get("HOME")
            seen["hub_test_home"] = os.environ.get("HUB_TEST_HOME")
            return unittest.TestResult(), 0, 0

        self.rt._split = lambda pattern: (["fake_l0"], [], [])
        self.rt._run = fake_run
        self.rt._parity = lambda: 0
        os.environ["HOME"] = self.real
        with contextlib.redirect_stdout(io.StringIO()):
            rc = self.rt.main(["hermetic"])
        self.assertEqual(seen["home"], seen["hub_test_home"])
        self.assertNotEqual(seen["home"], self.real)
        self.assertTrue(str(seen["home"]).startswith(self.real + os.sep), seen["home"])
        self.assertEqual(os.environ.get("HOME"), self.real, "main 返回后必须还原 HOME")
        self.assertEqual(rc, 2, "0 例应当被判成 insufficient（这条同时钉住「0 例 ≠ 全绿」）")

    def test_real_home_optout_keeps_home(self):
        seen = {}

        def fake_run(tests, label, stream=None):
            seen["home"] = os.environ.get("HOME")
            return unittest.TestResult(), 0, 0

        self.rt._split = lambda pattern: (["fake_l0"], [], [])
        self.rt._run = fake_run
        self.rt._parity = lambda: 0
        os.environ["HOME"] = self.real
        with contextlib.redirect_stdout(io.StringIO()):
            self.rt.main(["hermetic", "--real-home"])
        self.assertEqual(seen["home"], self.real)

    def test_pytest_passthrough_mode_isolates_home(self):
        """`run_tests.sh pytest` 是最容易被当成「快速看全绿」的旁路，必须同样隔离。"""
        import unittest.mock as mock

        calls = {}

        def fake_execv(path, args):
            calls["path"] = path
            calls["args"] = args
            calls["home"] = os.environ.get("HOME")
            calls["hub"] = os.environ.get("HUB_TEST_HOME")

        os.environ["HOME"] = self.real
        with mock.patch("os.execv", side_effect=fake_execv):
            with contextlib.redirect_stdout(io.StringIO()):
                self.rt.main(["pytest", "-q"])
        self.assertEqual(calls["home"], calls["hub"], "exec 前 HOME 必须已指向隔离目录")
        self.assertNotEqual(calls["home"], self.real)
        self.assertIn("-m", calls["args"])
        self.assertIn("pytest", calls["args"])

    def test_run_changed_isolates_home_too(self):
        """子集档（run_tests.sh changed）同样不能再碰真实 HOME。"""
        import unittest.mock as mock

        spec = importlib.util.spec_from_file_location(
            "run_changed_under_test", REPO / "scripts" / "run_changed.py")
        rc_mod = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(rc_mod)
        captured = {}

        def fake_run(cmd, cwd=None, env=None):
            captured.update(env=env or {})
            return type("R", (), {"returncode": 0})()

        os.environ["HOME"] = self.real
        with mock.patch.object(rc_mod, "index_tests", return_value={"test_x.py": "import sessions_store"}):
            with mock.patch.object(rc_mod, "select", return_value=(["test_x.py"], [])):
                with mock.patch.object(rc_mod.subprocess, "run", side_effect=fake_run):
                    rc_mod.main(["src/sessions_store.py"])
        self.assertNotEqual(captured["env"].get("HOME"), self.real,
                            "changed 档必须把 HOME 隔离后再起 pytest")
        self.assertTrue(str(captured["env"].get("HOME")).startswith(self.real + os.sep))


if __name__ == "__main__":
    unittest.main()
