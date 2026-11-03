"""L0：孤儿进程回收（`src/adapters/cli_agent.py` 的 reap_orphans / 登记表）。

实证（2026-11-03）：一条 run 在 01:53 已判 `failed/max_rounds` 并闭合，它派出去的
`claude -p`（PPID=hub 主进程）**43 分钟后仍在运行**，持续读写仓库文件：20 秒采样
`rchar +1261B / wchar +1168B`，到网关 3456 与外网 443 的连接一直是 ESTAB。
产物无人收集，token 照烧。`_kill_group` 只覆盖「超时」分支，run 整体收敛 / 服务重启
时没有任何一处去收子进程。

本文件钉三条安全边界（回收器最怕的就是误杀）：
  ① 只杀 `PPID == 指定父进程` 的；
  ② 只杀 `pgid == pid`（`start_new_session` 建的独立组）；
  ③ 只杀存活超过 `min_age_s` 的（不碰刚拉起的活任务）。
"""
import os
import pathlib
import signal
import subprocess
import sys
import time
import unittest

_SRC = str(pathlib.Path(__file__).resolve().parents[1] / "src")
sys.path.insert(0, _SRC)

import adapters.cli_agent as cli  # noqa: E402


def _spawn_sleeper(seconds=60):
    """起一个独立进程组的睡眠进程，模拟 CLI 子进程。"""
    return subprocess.Popen([sys.executable, "-c", "import time;time.sleep(%d)" % seconds],
                            start_new_session=True,
                            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def _alive(pid):
    try:
        os.kill(pid, 0)
        return True
    except OSError:
        return False


class TestReapOrphans(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_procs", []):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def _track(self, p):
        self._procs = getattr(self, "_procs", []) + [p]
        return p

    def test_reaps_own_old_child(self):
        p = self._track(_spawn_sleeper())
        time.sleep(0.3)
        killed = cli.reap_orphans(os.getpid(), min_age_s=0.0)
        self.assertIn(p.pid, killed)
        # 必须用 wait() 判定：SIGTERM 之后进程是**僵尸**，`os.kill(pid,0)` 仍然成功
        # ⇒ 拿它当"还活着"的判据会把回收成功读成失败（本次实测踩过）。
        rc = p.wait(timeout=6)
        self.assertEqual(rc, -signal.SIGTERM)

    def test_does_not_touch_fresh_child_before_min_age(self):
        """回收器与派发器并发跑：刚拉起的活任务不许被当成孤儿杀掉。"""
        p = self._track(_spawn_sleeper())
        killed = cli.reap_orphans(os.getpid(), min_age_s=120.0)
        self.assertNotIn(p.pid, killed)
        self.assertTrue(_alive(p.pid))

    def test_does_not_touch_other_parents_children(self):
        """只碰自己的子进程 —— 传一个不是自己 pid 的父进程号，必须一个都不杀。"""
        p = self._track(_spawn_sleeper())
        time.sleep(0.3)
        killed = cli.reap_orphans(os.getppid(), min_age_s=0.0)
        self.assertNotIn(p.pid, killed)
        self.assertTrue(_alive(p.pid))

    def test_survives_missing_proc_root(self):
        # /proc 不可读（容器/权限）时不许抛，返回空表
        self.assertEqual(cli.reap_orphans(os.getpid(), proc_root="/nonexistent"), [])


class TestRegistry(unittest.TestCase):
    def tearDown(self):
        for p in getattr(self, "_procs", []):
            try:
                p.kill()
                p.wait(timeout=5)
            except Exception:  # noqa: BLE001
                pass

    def test_register_and_unregister(self):
        p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs = [p]
        cli.register_process(p, "run1/t1")
        live = cli.live_processes()
        self.assertTrue(any(x["pid"] == p.pid and x["tag"] == "run1/t1" for x in live))
        cli.unregister_process(p)
        self.assertFalse(any(x["pid"] == p.pid for x in cli.live_processes()))

    def test_kill_all_live_empties_registry(self):
        p = subprocess.Popen([sys.executable, "-c", "import time;time.sleep(30)"],
                             start_new_session=True,
                             stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self._procs = [p]
        cli.register_process(p, "run1/t2")
        killed = cli.kill_all_live()
        self.assertIn(p.pid, killed)
        self.assertEqual(cli.live_processes(), [])

    def test_unregister_unknown_is_noop(self):
        cli.unregister_process(type("P", (), {"pid": 999999})())


if __name__ == "__main__":
    unittest.main()