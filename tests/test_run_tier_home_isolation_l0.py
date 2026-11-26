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


class FakeHomeSelfCleanup(unittest.TestCase):
    """2026-11-27：假 HOME 建了从不删 ⇒ 实测累积 2502 个 / 234M。

    用户裁定「单独立一条『测试后自清理』的修复，仅保留测试摘要文档即可」。
    这组用例守四条：①本次跑完自删；②摘要落 LAST-RUN.md；③陈旧残留启动时 sweep；
    ④pytest 直通档（execv 后 finally 不跑）用待清理清单兜住。
    """

    def setUp(self):
        self.rt = _load_run_tier()
        self.saved = {k: os.environ.get(k) for k in ("HOME", "HUB_TEST_HOME", "HUB_HOST_TESTS")}
        self.real = tempfile.mkdtemp(prefix="rt-clean-")
        self.base = Path(self.real) / "hub-l0test-fixtures"
        os.environ["HOME"] = self.real

    def tearDown(self):
        for k, v in self.saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        import shutil
        shutil.rmtree(self.real, ignore_errors=True)

    def test_isolated_home_is_removed_by_cleanup(self):
        """① 本次建出来的假 HOME，cleanup 必须真删掉（这是 234M 的根因）。"""
        p = self.rt.isolate_home(self.real, log=lambda *_: None)
        self.assertTrue(Path(p).is_dir(), "先确保真建了")
        self.rt.cleanup_isolated_home(log=lambda *_: None)
        self.assertFalse(Path(p).exists(), "假 HOME 必须自清理")
        self.assertEqual(self.rt.cleanup_isolated_home(log=lambda *_: None), "",
                         "第二次调用必须是空操作（幂等），不能抛也不能再删")

    def test_cleanup_refuses_foreign_dir(self):
        """只删自己建的那种名字 —— 防清单/环境被污染后 rmtree 掉别人目录。"""
        other = Path(self.real) / "not-a-fixture"
        other.mkdir(parents=True, exist_ok=True)
        self.rt._ISOLATED_HOME = str(other)
        self.rt.cleanup_isolated_home(log=lambda *_: None)
        self.assertTrue(other.is_dir(), "非 hub-clean-home-* 的目录绝不能删")

    def test_summary_written_and_overwrites(self):
        """② 摘要落 LAST-RUN.md，且是覆盖写（不累积成第二个日志库）。"""
        self.rt.write_summary("第一次\n")
        f = self.base / "LAST-RUN.md"
        self.assertTrue(f.is_file())
        self.rt.write_summary("第二次\n")
        self.assertEqual(f.read_text(encoding="utf-8"), "第二次\n", "必须是覆盖写")

    def test_sweep_removes_only_stale(self):
        """③ 陈旧（超 _STALE_HOURS）删，新鲜的留 —— 并发跑测试时不能互删。"""
        self.base.mkdir(parents=True, exist_ok=True)
        old = self.base / "hub-clean-home-old1"
        fresh = self.base / "hub-clean-home-new1"
        old.mkdir()
        fresh.mkdir()
        os.utime(old, (0, 0))          # mtime = epoch ⇒ 必然陈旧
        n = self.rt.sweep_stale(log=lambda *_: None)
        self.assertEqual(n, 1)
        self.assertFalse(old.exists(), "陈旧残留要清")
        self.assertTrue(fresh.exists(), "新鲜目录绝不能动")

    def test_pending_consumed_next_run(self):
        """④ pytest 直通档 execv 后 finally 不跑 ⇒ 靠清单在下次删掉。"""
        p = self.rt.isolate_home(self.real, log=lambda *_: None)
        self.rt.register_pending(p)
        self.assertTrue(Path(p).is_dir())
        n = self.rt.consume_pending(log=lambda *_: None)
        self.assertEqual(n, 1)
        self.assertFalse(Path(p).exists(), "登记过的假 HOME 要在下次启动时被删")
        self.assertFalse((self.base / ".pending-cleanup").exists(), "消费后清单必须清空")


if __name__ == "__main__":
    unittest.main()
