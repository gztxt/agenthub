#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：语义结论的**缓存陈旧降级**（2026-10-22 查出；2026-10-23 随外部 jev 下线改版）。

## 现象（实测）

`data/vitals.json` 的 `by_sha` 缓存让同一份证据**永不重问判定**：
270 条记录里 `source=jev` 78 条 + `jev-lowconf` 99 条，最老 `checked_at`
是 **2026-09-22（30.3 天前）**。外部 jev 自那以后一直是 HTTP 451（区域封禁），
2026-10-23 端点整体下线 ⇒ 看板却持续显示「判定参与了本轮」。

## 口径

`source` 表示**本轮判定依据**，不是「历史上曾用过什么」。
缓存里的语义结论超过 `JUDGE_CACHE_FRESH_H` 小时 ⇒ 降级显示 `rule`，
原值留 `source_cached` / `cached_age_h`（**不隐藏证据**，审计仍可看到当年判据）。
匹配范围 `jev*`（史实数据）+ `local*`（现值，本地判定同样受 by_sha 复用影响）。
"""
import pathlib
import sys
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import vitals  # noqa: E402


class TestStaleJudgeDowngrade(unittest.TestCase):
    def test_threshold_is_sane(self):
        """阈值必须小于「跨天」且大于一个 sweep 周期（900s），否则要么天天误降、要么永不降。"""
        self.assertGreater(vitals.JUDGE_CACHE_FRESH_H * 3600, 900)
        self.assertLessEqual(vitals.JUDGE_CACHE_FRESH_H, 24)

    def test_source_means_this_round(self):
        """源码级判据：缓存分支必须按 age 降级，并保留 source_cached。"""
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        self.assertIn("JUDGE_CACHE_FRESH_H", src)
        i_cache = src.index("if cached and not do_roundtrip:")
        i_guard = src.index('startswith(("jev", "local"))', i_cache)
        self.assertLess(i_cache, i_guard, "降级逻辑不在缓存分支里（会漏掉复用路径）")
        self.assertIn('rec["source_cached"]', src, "原值得留档（不隐藏证据）")
        self.assertIn('rec["source"] = "rule"', src)

    def test_downgrade_math(self):
        """模拟：30 天前的语义结论降级；10 分钟内的保留。jev 史实与 local 现值同律。"""
        def decide(source, age_h):
            if not str(source or "").startswith(("jev", "local")):
                return source, None
            if age_h > vitals.JUDGE_CACHE_FRESH_H:
                return "rule", source
            return source, None

        self.assertEqual(decide("local", 30 * 24), ("rule", "local"))
        self.assertEqual(decide("local-lowconf", 24), ("rule", "local-lowconf"))
        self.assertEqual(decide("jev", 30 * 24), ("rule", "jev"))       # 史实数据照样降级
        self.assertEqual(decide("jev-lowconf", 24), ("rule", "jev-lowconf"))
        self.assertEqual(decide("local", 0.1), ("local", None))
        self.assertEqual(decide("rule", 1000), ("rule", None))          # 规则源不重复降级


class TestSnapshotAlsoDowngrades(unittest.TestCase):
    """降级不能只做在 `judge()` 的缓存分支 —— `_latest` 是另一份字典。

    实测：`aider`/`cline`/`dsh` 已不在扫描面 ⇒ 它们的记录**永不被覆盖**，
    30 天前的 `source=jev-lowconf` 一直出现在面板上。修完 judge 分支后仍有 3/16 是假的，
    把降级加到 `snapshot()` 才归零（生产实测 16/16 变 rule，10 条留 source_cached）。
    """

    def test_snapshot_applies_downgrade(self):
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        i = src.index("def snapshot(self)")
        body = src[i:i + 1600]
        self.assertIn("JUDGE_CACHE_FRESH_H", body, "snapshot 没做陈旧降级（_latest 会漏）")
        self.assertIn('rec["source"] = "rule"', body)
        self.assertIn('rec["source_cached"]', body, "原值必须留档（不隐藏证据）")

    def test_latest_and_by_sha_both_covered(self):
        """两处都要覆盖：只改一处 ⇒ 面板与缓存结论不一致（同一屏两份真相）。"""
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        self.assertGreaterEqual(src.count('rec["source_cached"] ='), 2,
                                "by_sha 与 _latest 只改了一处")


class TestExternalJudgeFullyRemoved(unittest.TestCase):
    """外部 jev 已下线（2026-10-23）：src 里不许再留它的**活引用**。

    历史注释里出现 jev/typesafe 字样是有意的迁移记录，不断；
    这里只断**代码面**：端点 URL、key 变量、客户端模块、旧函数定义。
    """

    def test_client_module_gone(self):
        self.assertFalse((_REPO / "src" / "jev_client.py").exists(),
                         "jev_client.py 必须随端点下线删除")

    def test_no_live_endpoint_or_key(self):
        for name in ("vitals.py", "skill.py", "api_vitals.py", "api_agents.py"):
            src = (_REPO / "src" / name).read_text(encoding="utf-8")
            self.assertNotIn("https://api.typesafe.ai", src, "%s 仍带外部端点 URL" % name)
            self.assertNotIn("TYPESAFE_API_KEY=", src, "%s 仍读外部 key" % name)

    def test_old_functions_gone_local_wired(self):
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        for dead in ("def ask_jev", "def profiles_jev_key", "def jev_blocked_for",
                     "JEV_BLOCKED_COOLDOWN_S"):
            self.assertNotIn(dead, src, "vitals 仍留着外部判定的活代码：%s" % dead)
        self.assertIn("def ask_local", src)
        self.assertIn("import local_judge", src)
        sk = (_REPO / "src" / "skill.py").read_text(encoding="utf-8")
        self.assertNotIn("import jev_client", sk)
        self.assertIn("import local_judge", sk)


if __name__ == "__main__":
    unittest.main()
