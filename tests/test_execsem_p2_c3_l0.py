#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P2 C3 统一执行语义枚举（src/execsem.py + runlog 接线）。

守的判据（手册 C3：「统一 Agent 执行语义枚举作跨框架**公共词表**」）：

1. **未映射不静默归类**：`canonical_outcome()` 对没登记的词返回 `("unknown", False)`。
   反面做法（默认归 `ok`）会把「忘了登记」伪装成「一切正常」——
   与 `vitals_loop` 函数头丢失、`code_stale` 假绿同族。
2. **词表是唯一真相源**：`/api/runlog` 回包的 `sources` 以前是**手写死的五个**，
   而代码已在用 `ctx_manifest`/`resilience` ⇒ 回包静默骗人、前端下拉永远筛不到。
   本文件直接扫 `runlog.py` 的源码文本，**钉住那份手写清单不许回来**。
3. **覆盖闸门**：扫 `src/*.py` 里所有 `log_profile_event("<source>", ...)` 的字面量，
   每个都必须登记在 `SOURCES` 且在其 `STATUS_MAP` 里 —— 新增 source 忘了登记就红。
4. **原始 status 必须留着**：`decorate()` 只**增加** outcome/mapped，不改写事实字段。
   只回解释会让「哪个原始词没被覆盖」永久不可见。

分层口径（tests/tiers.py）：纯函数 + 源码文本断言，不起服务、不连网、零 SKIP。
"""
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import execsem  # noqa: E402


class TestVocabulary(unittest.TestCase):
    def test_sources_are_a_closed_set_with_maps(self):
        for s in execsem.SOURCES:
            self.assertIn(s, execsem.STATUS_MAP, "SOURCES 里的 %s 没有 STATUS_MAP" % s)

    def test_every_mapped_value_is_a_known_outcome(self):
        for src, m in execsem.STATUS_MAP.items():
            self.assertIn(src, execsem.SOURCES, "%s 有映射但不在 SOURCES" % src)
            for raw, out in m.items():
                self.assertIn(out, execsem.OUTCOMES, "%s.%s -> %s 越界" % (src, raw, out))

    def test_no_wildcard_entries(self):
        for src, m in execsem.STATUS_MAP.items():
            self.assertNotIn("*", m, "通配会把新词悄悄吃进来，正是本模块要防的")


class TestCanonicalOutcome(unittest.TestCase):
    def test_mapped_values(self):
        cases = [("hub_chat", "success", "ok"), ("hub_chat", "fail", "failed"),
                 ("task_exec", "parked", "parked"), ("task_exec", "submitted", "parked"),
                 ("ctx_manifest", "degraded", "degraded"),
                 ("resilience", "rate_limit", "degraded"),
                 ("resilience", "auth", "failed"),
                 ("rest", "success", "ok")]
        for src, raw, want in cases:
            got, mapped = execsem.canonical_outcome(src, raw)
            self.assertTrue(mapped, "%s/%s 应被映射" % (src, raw))
            self.assertEqual(got, want, "%s/%s" % (src, raw))

    def test_unmapped_is_unknown_and_flagged(self):
        for src, raw in (("hub_chat", "weird"), ("nope", "success"), ("rest", None)):
            got, mapped = execsem.canonical_outcome(src, raw)
            self.assertEqual(got, "unknown")
            self.assertFalse(mapped, "%s/%s 未映射必须 flagged" % (src, raw))

    def test_parked_and_blocked_are_distinct(self):
        self.assertIn("parked", execsem.OUTCOMES)
        self.assertIn("blocked", execsem.OUTCOMES)
        self.assertNotEqual(execsem.canonical_outcome("task_exec", "parked")[0], "blocked")


class TestDecorate(unittest.TestCase):
    def test_adds_outcome_without_touching_status(self):
        rows = [{"source": "task_exec", "status": "parked", "id": 1},
                {"source": "hub_chat", "status": "weird", "id": 2}]
        out = execsem.decorate(rows)
        self.assertEqual(out[0]["outcome"], "parked")
        self.assertEqual(out[0]["status"], "parked", "原始 status 是事实，不许被改写")
        self.assertTrue(out[0]["mapped"])
        self.assertEqual(out[1]["outcome"], "unknown")
        self.assertFalse(out[1]["mapped"])

    def test_tolerates_empty(self):
        self.assertEqual(execsem.decorate([]), [])
        self.assertEqual(execsem.decorate(None), [])


class TestCoverageGate(unittest.TestCase):
    """把「新 source 忘了登记」从静默漂移变成红灯。"""

    def _write_sites(self):
        found = {}
        for p in sorted((_REPO / "src").glob("*.py")):
            txt = p.read_text(encoding="utf-8", errors="replace")
            for m in re.finditer(r'log_profile_event\(\s*"([A-Za-z_][A-Za-z0-9_]*)"', txt):
                found.setdefault(m.group(1), []).append(p.name)
        return found

    def test_every_written_source_is_registered(self):
        found = self._write_sites()
        self.assertTrue(found, "一个写入点都没扫到 ⇒ 闸门本身失效（正则/路径变了）")
        for src, files in found.items():
            self.assertTrue(execsem.is_known_source(src),
                            "src/%s 写了 source=%r，但没登记在 execsem.SOURCES" % (files[0], src))

    def test_runlog_no_longer_hardcodes_its_source_list(self):
        s = (_REPO / "src" / "runlog.py").read_text(encoding="utf-8")
        self.assertNotIn('"sources": ["rest", "hub_chat"', s,
                         "手写死的 sources 清单必须消失（它曾漏掉 ctx_manifest/resilience）")
        self.assertIn("execsem.vocab()", s)
        self.assertIn("execsem.decorate(rows)", s)

    def test_unmapped_sources_helper(self):
        bad = execsem.unmapped_sources({"hub_chat": {"success", "brand_new_word"},
                                        "nonexistent": {"success"}})
        self.assertEqual(bad["hub_chat"], ["brand_new_word"])
        self.assertIn("nonexistent", bad)
        self.assertEqual(execsem.unmapped_sources({"hub_chat": {"success"}}), {})


if __name__ == "__main__":
    unittest.main()