# -*- coding: utf-8 -*-
"""L0 · /api/system/shutdown 的防误触与派发形状（不关机、不起服务）。

守三件：
  A 缺 `confirm=poweroff` ⇒ 400，且绝不 spawn（防手滑/拼错 URL 就断电）；
  B 有活终端会话且未 force ⇒ 409，且绝不 spawn（与 restart 同口径）；
  C 通过后只起一个**脱离 cgroup** 的 systemd 瞬态单元，命令必须是
    `sudo -n loginctl poweroff`（user unit 直接 poweroff 无权限；带交互提示
    会永远卡住，所以必须 `-n`）。

测试只 patch `subprocess.Popen` 与 `term_mod.alive_count`，不触碰真实系统。
"""
import asyncio
import sys
import unittest
from pathlib import Path
from unittest import mock

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import api_system  # noqa: E402
from fastapi import HTTPException  # noqa: E402


def _call(**kw):
    return asyncio.run(api_system.system_shutdown(**kw))


class ShutdownGate(unittest.TestCase):
    def setUp(self):
        self.popen = mock.patch.object(api_system.subprocess, "Popen").start()
        self.addCleanup(mock.patch.stopall)
        self.popen.reset_mock()

    def test_requires_explicit_confirm(self):
        with mock.patch.object(api_system.term_mod, "alive_count", return_value=0):
            for bad in ("", "yes", "poweroff "):
                with self.subTest(confirm=bad):
                    with self.assertRaises(HTTPException) as cm:
                        _call(confirm=bad)
                    self.assertEqual(cm.exception.status_code, 400)
                    self.assertEqual(cm.exception.detail["error"], "confirm_required")
            self.popen.assert_not_called()

    def test_live_sessions_need_force(self):
        with mock.patch.object(api_system.term_mod, "alive_count", return_value=2):
            with self.assertRaises(HTTPException) as cm:
                _call(confirm="poweroff")
            self.assertEqual(cm.exception.status_code, 409)
            self.assertEqual(cm.exception.detail["term_sessions"], 2)
            self.popen.assert_not_called()

    def test_spawns_detached_poweroff(self):
        with mock.patch.object(api_system.term_mod, "alive_count", return_value=1):
            out = _call(confirm="poweroff", force=1)
        self.assertEqual(out["status"], "powering_off")
        self.assertEqual(out["term_sessions"], 1)
        self.popen.assert_called_once()
        argv = self.popen.call_args.args[0]
        self.assertEqual(argv[:4], ["systemd-run", "--user", "--collect", "--unit"])
        self.assertEqual(argv[4], out["unit"])
        self.assertEqual(argv[5], "/bin/sh")
        self.assertEqual(argv[6], "-c")
        self.assertIn("sleep 1;", argv[7])
        self.assertIn("/usr/bin/sudo -n /usr/bin/loginctl poweroff", argv[7])
        self.assertIs(self.popen.call_args.kwargs.get("start_new_session"), True)

    def test_spawn_failure_is_reported_not_silent(self):
        self.popen.side_effect = FileNotFoundError("systemd-run")
        with mock.patch.object(api_system.term_mod, "alive_count", return_value=0):
            with self.assertRaises(HTTPException) as cm:
                _call(confirm="poweroff")
            self.assertEqual(cm.exception.status_code, 500)
            self.assertEqual(cm.exception.detail["error"], "spawn_failed")


if __name__ == "__main__":
    unittest.main()