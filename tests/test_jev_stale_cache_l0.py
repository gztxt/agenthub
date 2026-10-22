#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：jev 结论的**缓存陈旧降级**（2026-10-22，用户问「jev 是否参与链路」查出）。

## 现象（实测）

`data/vitals.json` 的 `by_sha` 缓存让同一份证据**永不重问 jev**：
270 条记录里 `source=jev` 78 条 + `jev-lowconf` 99 条，最老 `checked_at`
是 **2026-09-22（30.3 天前）**；而 jev 自那以后一直是 HTTP 451（区域封禁）。
于是看板持续显示「jev 参与了判定」，而实际上这个进程**一次都没成功问过 jev**。

## 口径

`source` 表示**本轮判定依据**，不是「历史上曾用过什么」。
缓存里的 jev 结论超过 `JEV_CACHE_FRESH_H` 小时 ⇒ 降级显示 `rule`，
原值留 `source_cached` / `cached_age_h`（**不隐藏证据**，审计仍可看到当年是 jev 判的）。
"""
import pathlib
import sys
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import vitals  # noqa: E402


class TestStaleJevDowngrade(unittest.TestCase):
    def test_threshold_is_sane(self):
        """阈值必须小于「跨天」且大于一个 sweep 周期（900s），否则要么天天误降、要么永不降。"""
        self.assertGreater(vitals.JEV_CACHE_FRESH_H * 3600, 900)
        self.assertLessEqual(vitals.JEV_CACHE_FRESH_H, 24)

    def test_source_means_this_round(self):
        """源码级判据：缓存分支必须按 age 降级，并保留 source_cached。"""
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        self.assertIn("JEV_CACHE_FRESH_H", src)
        i_cache = src.index("if cached and not do_roundtrip:")
        i_guard = src.index('startswith("jev")', i_cache)
        self.assertLess(i_cache, i_guard, "降级逻辑不在缓存分支里（会漏掉复用路径）")
        self.assertIn('rec["source_cached"]', src, "原值得留档（不隐藏证据）")
        self.assertIn('rec["source"] = "rule"', src)

    def test_downgrade_math(self):
        """模拟：30 天前的 jev 结论降级；10 分钟内的保留。"""
        def decide(source, age_h):
            if not str(source or "").startswith("jev"):
                return source, None
            if age_h > vitals.JEV_CACHE_FRESH_H:
                return "rule", source
            return source, None

        self.assertEqual(decide("jev", 30 * 24), ("rule", "jev"))
        self.assertEqual(decide("jev-lowconf", 24), ("rule", "jev-lowconf"))
        self.assertEqual(decide("jev", 0.1), ("jev", None))
        self.assertEqual(decide("rule", 1000), ("rule", None))


class TestBlockedShortCircuitStillWorks(unittest.TestCase):
    """降级不得与 451 短路冲突：短路只影响「要不要发请求」，降级只影响「怎么显示」。"""

    def test_both_exist(self):
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        self.assertIn("def jev_blocked_for", src)
        self.assertIn("JEV_CACHE_FRESH_H", src)


if __name__ == "__main__":
    unittest.main()
