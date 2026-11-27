#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：agent 模型配置的写者收敛（波3 S2，2026-11-27）。

【为什么要有这一组】
hub 的 `modelcfg.apply_model()` 与 magpie 都在写同样 5 类配置文件
（hub 的写面在 `src/modelcfg.py:17-21`，magpie 侧见 `~/.config/magpie/applied.json`）。
codex 与 pi 是**双写者**：谁后写谁生效，两边日志都「成功」，出事无法归因。

判据：
1. `MODELCFG_WRITER` 默认 `hub` ⇒ 一个字节的行为都不能变；
2. `=magpie` 时只对 magpie 认得的 agent 走代理，其余仍走 hub；
3. magpie 返回成功但**文件里读不到**新值 ⇒ 判失败（不信它自己的自述）；
4. 记账失败不许反过来报「设置失败」——文件已经改了，报错会让人重复点。
"""
import os
import pathlib
import sys
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import modelcfg  # noqa: E402


class TestWriterRouting(unittest.TestCase):
    def test_default_is_hub(self):
        env = {k: v for k, v in os.environ.items() if k != "MODELCFG_WRITER"}
        with mock.patch.dict(os.environ, env, clear=True):
            self.assertEqual(modelcfg.writer_mode(), "hub")

    def test_magpie_only_for_known_agents(self):
        env = dict(os.environ)
        env["MODELCFG_WRITER"] = "magpie"
        with mock.patch.dict(os.environ, env, clear=True), \
                mock.patch.object(modelcfg, "validate_model", lambda m: m), \
                mock.patch.object(modelcfg, "_apply_via_magpie",
                                  lambda a, m: {"writer": "magpie"}), \
                mock.patch.object(modelcfg, "catalog_ids", return_value={"checked": False}), \
                mock.patch.object(modelcfg, "_spec_or_raise",
                                  side_effect=AssertionError("不该走 hub 写手")):
            # 在名单里的 agent：委派成功
            self.assertEqual(modelcfg.apply_model("pi", "x/y")["writer"], "magpie")
            # 不在名单里：必须回落到原路径（这里用异常标记证明它确实没走代理）
            with self.assertRaises(AssertionError):
                modelcfg.apply_model("cursor", "x/y")


class TestApplyViaMagpie(unittest.TestCase):
    def _spec(self, current):
        return {"read": lambda: {"current": current}, "write": lambda m: []}

    def test_success_verifies_and_books(self):
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=cp), \
                mock.patch.object(modelcfg, "_spec_or_raise", return_value=self._spec("new/m")), \
                mock.patch.object(modelcfg, "set_hub_model") as setter, \
                mock.patch.object(modelcfg, "_record_write") as rec, \
                mock.patch.object(modelcfg, "terminal_argv", return_value=[]):
            out = modelcfg._apply_via_magpie("pi", "new/m")
        self.assertTrue(out["verified"])
        self.assertEqual(out["bookkeeping_error"], "")
        setter.assert_called_once_with("pi", "new/m")
        rec.assert_called_once()

    def test_nonzero_rc_is_502(self):
        cp = mock.Mock(returncode=3, stdout="", stderr="bad model")
        with mock.patch("subprocess.run", return_value=cp), \
                mock.patch.object(modelcfg, "_spec_or_raise", return_value=self._spec("old/m")):
            with self.assertRaises(modelcfg.ModelCfgError) as cm:
                modelcfg._apply_via_magpie("pi", "new/m")
        self.assertEqual(cm.exception.status, 502)
        self.assertIn("bad model", str(cm.exception))

    def test_unverified_write_is_rejected(self):
        """magpie 说成功了，但文件里还是旧值 ⇒ 判失败（不能信自述）。"""
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=cp), \
                mock.patch.object(modelcfg, "_spec_or_raise", return_value=self._spec("old/m")), \
                mock.patch.object(modelcfg, "set_hub_model") as setter:
            with self.assertRaises(modelcfg.ModelCfgError) as cm:
                modelcfg._apply_via_magpie("pi", "new/m")
        self.assertEqual(cm.exception.status, 502)
        setter.assert_not_called()

    def test_bookkeeping_failure_is_not_a_write_failure(self):
        cp = mock.Mock(returncode=0, stdout="", stderr="")
        with mock.patch("subprocess.run", return_value=cp), \
                mock.patch.object(modelcfg, "_spec_or_raise", return_value=self._spec("new/m")), \
                mock.patch.object(modelcfg, "set_hub_model", side_effect=RuntimeError("db down")), \
                mock.patch.object(modelcfg, "terminal_argv", return_value=[]):
            out = modelcfg._apply_via_magpie("pi", "new/m")
        self.assertTrue(out["verified"])
        self.assertIn("db down", out["bookkeeping_error"])


if __name__ == "__main__":
    unittest.main()
