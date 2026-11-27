#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：magpie 长期记忆进入 hub 联邦检索（波3 S4，2026-11-27）。

为什么这条要守：hub 的 7 路联邦里此前**没有** magpie memory，
而 magpie 侧写着枢纽级事实（`hub-l2.md` 等 9 个 .md）⇒ 那些记忆在 hub 搜索里不可见。
判据：源已注册 + 已启用 + rg 目标指向 magpie memory 目录 + 默认快路包含它。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import memfed  # noqa: E402
import memory  # noqa: E402


class TestMagpieMemorySource(unittest.TestCase):
    def test_registered_and_enabled(self):
        self.assertIn("magpie_memory", memfed.REGISTRY)
        self.assertIn("magpie_memory", memfed.enabled_ids())

    def test_target_points_at_magpie_memory(self):
        paths, glob = memfed._RG_TARGETS["magpie_memory"]
        self.assertTrue(any(p.endswith(".config/magpie/memory") for p in paths), paths)
        self.assertEqual(glob, "*.md")

    def test_weight_is_fact_level(self):
        # 成文事实 0.9 > 会话档 0.5；被调成会话级说明有人把两类混了
        self.assertGreaterEqual(memfed.REGISTRY["magpie_memory"].weight, 0.8)

    def test_in_default_fast_sources(self):
        self.assertIn("magpie_memory", memory.FED_FAST_SOURCES.split(","))

    def test_role_in_whitelist(self):
        self.assertIn("magpie_memory", memory.SOURCE_WHITELIST)


if __name__ == "__main__":
    unittest.main()
