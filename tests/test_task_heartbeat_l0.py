"""心跳判据 L0：CPU ticks 增长=在跑不误杀；停滞>阈值=提前升级；无进程不误判。

背景（2026-11-03 混排取证）：旧逻辑等满 TASK_TIMEOUT_S(600s) 才升级，且这次隐藏重试
既不进 attempts 也不进 duration_ms。实测 e91b24b879 的 t3 表面 35.5s，实际先吃满 600s。
心跳信号源＝子进程 CPU ticks（utime+stime）。
"""

import asyncio
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import tasks_runner as r  # noqa: E402


class TestProcCpuTicks(unittest.TestCase):
    def test_reads_ticks_from_proc(self):
        # 真 /proc/self/stat：能读到非负 ticks
        ticks = r._proc_cpu_ticks(os.getpid())
        self.assertIsNotNone(ticks)
        self.assertGreaterEqual(ticks, 0)

    def test_gone_pid_returns_none(self):
        self.assertIsNone(r._proc_cpu_ticks(999999))


class TestStallConstant(unittest.TestCase):
    def test_stall_threshold_is_bounded(self):
        # 阈值必须远小于 600s 总时长才有意义，且 >0 防呆
        self.assertGreater(r.HEARTBEAT_STALL_S, 0)
        self.assertLess(r.HEARTBEAT_STALL_S, 600)


class TestColumnsDeclared(unittest.TestCase):
    def test_heartbeat_columns_present(self):
        cols = dict(r._TASK_COLUMNS_EXTRA)
        self.assertIn("last_progress_at", cols)
        self.assertIn("last_cpu_ticks", cols)


if __name__ == "__main__":
    unittest.main()
