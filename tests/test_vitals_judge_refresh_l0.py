#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：vitals 判定刷新策略（2026-10-23「修复自动调用」）。

背景：`by_sha` 以 core_sha 为键，agent 二进制不变就永远命中缓存 ⇒ 本地判定一次都不跑。
本测试锁住三条：
① 超过 `JUDGE_REFRESH_H` 且预算允许 ⇒ 真重判；② 预算为 0 ⇒ 保留旧结论；
③ 语义字段为空（jev 时代遗留 rule 记录）⇒ 即使年龄不够也补判。
"""
import os
import pathlib
import sys
import time
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import vitals  # noqa: E402


def _ev():
    return {"agent_id": "x", "core_sha": "sha-x", "evidence_sha": "sha-x",
            "path": "/bin/ls", "file_kind": "ELF", "is_entry_shim": False,
            "run_rc": None, "run_output": None}


class TestRefresh(unittest.TestCase):
    def setUp(self):
        self.v = vitals.Vitals()
        self.v._by_sha.clear()
        self.v._latest.clear()

    def _seed(self, age_h, source="rule", menu=None, present=None):
        self.v._by_sha["sha-x"] = {
            "verdict": "usable", "rule_verdict": "usable", "rt_state": "answered",
            "source": source, "checked_at": time.time() - age_h * 3600,
            "menu_noul": menu, "present_noul": present,
        }

    def _run(self, refresh=True, budget=0):
        self.v._judge_budget = budget
        calls = {"n": 0}

        def fake_ask(ev, key=None, timeout=None):
            calls["n"] += 1
            return {"underlying_program_present": {"type": "choice",
                                                   "choice": "已安装", "noul": 1.0},
                    "roundtrip_state": {"choice": "answered", "confidence": 0.9}}

        with mock.patch.object(vitals, "collect", lambda p, do_roundtrip=False: _ev()), \
             mock.patch.object(vitals, "ask_local", side_effect=fake_ask):
            rec = self.v.judge({"id": "x"}, do_roundtrip=False, refresh=refresh)
        return rec, calls["n"]

    def test_fresh_cache_is_reused_without_model_call(self):
        self._seed(age_h=1, source="local", menu=1.0, present=1.0)
        rec, n = self._run(refresh=True, budget=2)
        self.assertEqual(n, 0, "新鲜缓存不该重判")
        self.assertEqual(rec["source"], "local")

    def test_stale_cache_refreshes_when_budget_allows(self):
        self._seed(age_h=vitals.JUDGE_REFRESH_H + 2)
        rec, n = self._run(refresh=True, budget=1)
        self.assertEqual(n, 1, "超过刷新窗且有预算 ⇒ 必须真判一次")
        self.assertGreater(rec["checked_at"], time.time() - 60)

    def test_stale_cache_kept_when_budget_exhausted(self):
        self._seed(age_h=vitals.JUDGE_REFRESH_H + 2)
        rec, n = self._run(refresh=True, budget=0)
        self.assertEqual(n, 0, "预算用完 ⇒ 保留旧结论，不许静默跳过又标成新判")
        self.assertLess(rec["checked_at"], time.time() - 3600)

    def test_legacy_rule_record_refreshes_even_if_young(self):
        """jev 时代只留了 source=rule / 无语义字段：年龄不够也要补一次。"""
        self._seed(age_h=1, source="rule", menu=None, present=None)
        _, n = self._run(refresh=True, budget=1)
        self.assertEqual(n, 1)

    def test_manual_verify_always_rejudges(self):
        self._seed(age_h=1, source="local", menu=1.0, present=1.0)
        self.v._judge_budget = 0
        calls = {"n": 0}

        def fake_ask(ev, key=None, timeout=None):
            calls["n"] += 1
            return {"underlying_program_present": {"choice": "已安装", "noul": 1.0}}

        with mock.patch.object(vitals, "collect", lambda p, do_roundtrip=False: _ev()), \
             mock.patch.object(vitals, "ask_local", side_effect=fake_ask):
            self.v.judge({"id": "x"}, do_roundtrip=True, refresh=True)
        self.assertEqual(calls["n"], 1, "手动 verify 必须绕过缓存")


class TestMenuFromEvidence(unittest.TestCase):
    def test_entry_shim_is_hidden(self):
        self.assertEqual(vitals.menu_from_evidence({"is_entry_shim": True, "path": "/x"}), 0.0)

    def test_present_is_shown(self):
        """有 resolved/文件类型 ⇒ 确定存在；只有路径、无任何类型信息 ⇒ 证据模糊（fail-open）。"""
        self.assertEqual(vitals.menu_from_evidence(
            {"path": "/usr/bin/x", "file_kind": "ELF", "version": "1.0",
             "is_entry_shim": False}), 1.0)
        self.assertIsNone(vitals.menu_from_evidence(
            {"path": "/usr/bin/x", "is_entry_shim": False}))

    def test_missing_is_hidden(self):
        """file_kind=missing 是硬证据：不该把「路径写着一个字符串」当成已安装。"""
        self.assertEqual(vitals.menu_from_evidence(
            {"path": "/usr/bin/x", "file_kind": "missing", "is_entry_shim": False}), 0.0)
        self.assertEqual(vitals.menu_from_evidence(
            {"path": "", "file_kind": "missing", "is_entry_shim": False}), 0.0)


if __name__ == "__main__":
    unittest.main()
