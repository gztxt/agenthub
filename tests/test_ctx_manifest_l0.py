#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-1 Context Manifest（src/ctxmanifest.py + 两个装配点）。

守的判据（手册 A7 / C9）：

1. **哈希只认内容**：`manifest_hash` 必须对 `label` / `as_of` **不敏感**（改标签不算换上下文），
   对**正文**与**排除项**必须**敏感**（正文变了、某一路从入选变成被排除，哈希必须变）。
2. **排除项必须有理由**：`excluded()` 的 `reason` 是必填参数（缺了直接 TypeError）——
   「为什么没有」是归因的核心，没有理由的排除项等于把静默不注入洗成正常。
3. **超预算必须留下痕迹**：`truncated=True` 时自动补一条 `(tail)` 排除项。
4. **两个装配点都真的挂了 manifest**（不是只写了函数没人调）：
   `memory.injection_context` 与 `kb.kb_search` 的回包都要有 `manifest` 键；
   且 `memory.py` 里**只许有一处** `parts.append(`（在 `_add` 内）——
   任何绕过 `_add` 的新段都会让「清单与文本漂移」，本守卫就是拦它的。

分层口径（tests/tiers.py）：纯函数 + 源码文本断言，不起服务、不连网、零 SKIP。
"""
import hashlib
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import ctxmanifest as cm  # noqa: E402


class TestManifestEntries(unittest.TestCase):
    def test_entry_hashes_the_exact_injected_text(self):
        txt = "## 长期 Profile\nabc"
        m = cm.build_manifest([cm.entry("长期 Profile", txt, "l3")])
        e = m["parts"][0]
        self.assertEqual(e["chars"], len(txt))
        self.assertEqual(e["sha256"], hashlib.sha256(txt.encode()).hexdigest(),
                         "哈希必须算「逐字节要进 prompt 的那段」")
        self.assertEqual(m["chars"], len(txt))

    def test_empty_part_is_zero_chars_not_error(self):
        m = cm.build_manifest([cm.entry("x", "", "l2")])
        self.assertEqual(m["parts"][0]["chars"], 0)


class TestManifestHash(unittest.TestCase):
    def _hash(self, parts, **kw):
        return cm.build_manifest(parts, **kw)["manifest_hash"]

    def test_label_and_asof_do_not_change_hash(self):
        a = self._hash([cm.entry("A", "same text", "l3", as_of="2026-01-01")])
        b = self._hash([cm.entry("B", "same text", "l3", as_of="2030-12-31")])
        self.assertEqual(a, b, "改标签/时间不是「换了上下文」，哈希不得变")

    def test_content_changes_hash(self):
        a = self._hash([cm.entry("A", "text-1", "l3")])
        b = self._hash([cm.entry("A", "text-2", "l3")])
        self.assertNotEqual(a, b)

    def test_dropping_a_source_changes_hash(self):
        a = self._hash([cm.entry("A", "x", "l3"), cm.entry("B", "y", "l2")])
        b = self._hash([cm.entry("A", "x", "l3")])
        self.assertNotEqual(a, b)

    def test_excluded_reason_changes_hash(self):
        p = [cm.entry("A", "x", "l3")]
        a = self._hash(p, excluded_items=[cm.excluded("tdai", "timeout")])
        b = self._hash(p, excluded_items=[cm.excluded("tdai", "disabled")])
        self.assertNotEqual(a, b, "同一路「为什么没来」不同 ⇒ 上下文等价性不同")

    def test_same_input_is_bit_identical(self):
        p = lambda: [cm.entry("A", "x", "l3"), cm.entry("B", "y", "l2")]
        self.assertEqual(cm.build_manifest(p()), cm.build_manifest(p()))


class TestExcluded(unittest.TestCase):
    def test_reason_is_mandatory(self):
        with self.assertRaises(TypeError):
            cm.excluded("tdai")           # type: ignore[call-arg]

    def test_truncated_appends_tail_exclusion(self):
        m = cm.build_manifest([cm.entry("A", "x" * 50, "l3")],
                              budget_chars=10, truncated=True)
        tails = [e for e in m["excluded"] if e["source"] == "(tail)"]
        self.assertEqual(len(tails), 1)
        self.assertIn("10", tails[0]["reason"], "理由里要能读出预算值")

    def test_not_truncated_has_no_tail_entry(self):
        m = cm.build_manifest([cm.entry("A", "x", "l3")], budget_chars=100)
        self.assertEqual(m["excluded"], [])


class TestSummarize(unittest.TestCase):
    def test_summary_is_self_sufficient(self):
        m = cm.build_manifest([cm.entry("A", "x", "l3")],
                              excluded_items=[cm.excluded("tdai", "timeout")])
        s = cm.summarize(m)
        self.assertEqual(s["manifest_hash"], m["manifest_hash"])
        self.assertEqual(s["sources"], ["l3"])
        self.assertEqual(s["excluded"], [{"source": "tdai", "reason": "timeout"}])


class TestWiringGuards(unittest.TestCase):
    """两个装配点必须真的挂了 manifest，且 memory.py 不得有绕过 `_add` 的段。"""

    def _src(self, name):
        return (_REPO / "src" / name).read_text(encoding="utf-8")

    def test_memory_has_exactly_one_parts_append(self):
        # 用**词边界**匹配：`mparts.append(` 含子串 `parts.append(`，朴素 count 会误报（本轮实测）。
        n = len(re.findall(r"\bparts\.append\(", self._src("memory.py")))
        self.assertEqual(n, 1,
                         "memory.py 里只许 _add 内部有一处 parts.append；"
                         "多出来的那处会绕过 manifest 造成「清单与文本漂移」")

    def test_memory_returns_manifest(self):
        s = self._src("memory.py")
        self.assertIn("ctxmanifest.build_manifest(mparts", s)
        self.assertIn('"manifest": manifest', s)

    def test_kb_returns_manifest(self):
        s = self._src("kb.py")
        self.assertIn("ctxmanifest.build_manifest(kparts", s)
        self.assertIn('"manifest": manifest', s)

    def test_both_persist_manifest_hash(self):
        for name, subject in (("memory.py", "injection_context"), ("kb.py", "kb_search")):
            s = self._src(name)
            self.assertIn("ctxmanifest.summarize(manifest)", s,
                          "%s 没把清单摘要落库" % name)
            self.assertIn('"ctx_manifest"', s)


if __name__ == "__main__":
    unittest.main()