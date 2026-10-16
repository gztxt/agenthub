#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-5 其余四件 —— 技能资产（owner / version / scope / 状态机）。

并把 P1-5 首片的**整包摘要**与状态机焊在一起守（A9 + C8）：

1. **显式边**：状态机只能走 `TRANSITIONS` 里列出的边；**跳级**（draft→published）与
   **跨级回退**一律拒，且**拒绝理由必须逐条说清**（不是一句「非法」）。
2. **重复推进不算成功**（`to == frm` ⇒ 拒）——否则「推进了两次」与「推进了一次」不可分辨。
3. **publish 必须有指纹**：给 `path` 现算或直接给 `digest`；两者皆无 ⇒ 拒。
   「发布了但不知道发布的是什么」不可追溯。
4. **digest 变了 ⇒ published 失效**（`effective_status='stale'`）——
   这是「资产变更准入」这条闸门的真正落点：改脚本但不重新评审的包，不得继续被依赖。
5. **不可判定不拦**：算不出当前摘要 ⇒ 不判 stale（同 P0-4 的 `insufficient` 口径）。
6. **owner 不得为空**（无主资产＝无主副本，军规直接禁止）；**已发布版本不得倒退**。
7. **scope 白名单**：global / route:<名> / agent:<id>，自由文本等于没有声明。

分层口径（tests/tiers.py）：临时 db + 纯函数，不起服务、不连网、零 SKIP。
"""
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db             # noqa: E402
import skill_assets as sa  # noqa: E402


class TestPureJudgement(unittest.TestCase):
    def test_parse_version(self):
        self.assertEqual(sa.parse_version("1.2.3"), (1, 2, 3))
        self.assertEqual(sa.parse_version("0.13.115"), (0, 13, 115))
        self.assertEqual(sa.parse_version("1.2.3-pre.1"), (1, 2, 3))
        for bad in ("", "1.2", "v1.2.3", "1.2.3.4", "one.two.three"):
            self.assertIsNone(sa.parse_version(bad), bad)

    def test_compare_version_refuses_invalid(self):
        self.assertEqual(sa.compare_version("1.2.3", "1.2.4"), -1)
        self.assertEqual(sa.compare_version("2.0.0", "1.9.9"), 1)
        self.assertEqual(sa.compare_version("1.2.3", "1.2.3"), 0)
        self.assertIsNone(sa.compare_version("bad", "1.2.3"),
                          "非法版本不得当 0 处理（那会把「判不了」变成「相等」）")

    def test_validate_scope(self):
        for ok in ("global", "route:pi", "agent:claude", "agent:01a14261"):
            self.assertIsNone(sa.validate_scope(ok), ok)
        for bad in ("", "everything", "route:", "agent:", "global stuff", "route:a/b"):
            self.assertIsNotNone(sa.validate_scope(bad), bad)

    def test_transition_edges_are_explicit(self):
        self.assertIsNone(sa.transition_reason("draft", "tested"))
        self.assertIsNone(sa.transition_reason("tested", "reviewed"))
        self.assertIsNone(sa.transition_reason("reviewed", "published"))
        self.assertIsNone(sa.transition_reason("published", "deprecated"))
        for frm in ("tested", "reviewed", "published", "deprecated"):
            self.assertIsNone(sa.transition_reason(frm, "draft"), "revise 必须允许：%s" % frm)

    def test_skipping_a_stage_is_refused_with_reason(self):
        why = sa.transition_reason("draft", "published")
        self.assertIsNotNone(why, "跳级必须拒")
        self.assertIn("draft", why)
        self.assertIn("tested", why, "理由要说清「只能到哪」")

    def test_no_op_is_refused(self):
        self.assertIn("未变化", sa.transition_reason("draft", "draft"))

    def test_unknown_state_refused(self):
        self.assertIsNotNone(sa.transition_reason("nope", "draft"))
        self.assertIsNotNone(sa.transition_reason("draft", "nope"))

    def test_stale_only_when_published_and_digest_differs(self):
        pub = {"status": "published", "digest": "aaa"}
        self.assertFalse(sa.is_stale(pub, "aaa"))
        self.assertTrue(sa.is_stale(pub, "bbb"))
        self.assertEqual(sa.effective_status(pub, "bbb"), "stale")
        self.assertEqual(sa.effective_status(pub, "aaa"), "published")
        # 不可判定 ⇒ 不当 stale
        self.assertFalse(sa.is_stale(pub, None))
        self.assertEqual(sa.effective_status(pub, None), "published")
        # 非 published 不判 stale
        self.assertFalse(sa.is_stale({"status": "draft", "digest": "aaa"}, "bbb"))
        self.assertEqual(sa.effective_status({"status": "reviewed", "digest": "aaa"}, "bbb"),
                         "reviewed")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0sa-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        sa.ensure_schema()
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved

    def _mk(self, slug="demo", ver="1.0.0"):
        return sa.upsert(slug, "gztxt", ver, "global", by_whom="test")


class TestStore(_Base):
    def test_upsert_requires_owner(self):
        with self.assertRaises(ValueError) as cm:
            sa.upsert("x", "  ", "1.0.0", "global")
        self.assertIn("owner", str(cm.exception))

    def test_upsert_requires_semver(self):
        with self.assertRaises(ValueError):
            sa.upsert("x", "gztxt", "1.0", "global")

    def test_upsert_rejects_bad_scope(self):
        with self.assertRaises(ValueError):
            sa.upsert("x", "gztxt", "1.0.0", "everything")

    def test_full_lifecycle(self):
        self._mk()
        self.assertEqual(sa.get("demo")["status"], "draft")
        for to in ("tested", "reviewed"):
            sa.transition("demo", to, by="test")
        with self.assertRaises(ValueError):
            sa.transition("demo", "published", by="test")   # 没指纹
        rec = sa.transition("demo", "published", digest="d" * 64, by="test")
        self.assertEqual(rec["status"], "published")
        self.assertEqual(rec["digest"], "d" * 64)
        rec = sa.transition("demo", "deprecated", by="test")
        self.assertEqual(rec["status"], "deprecated")
        self.assertEqual(sa.transition("demo", "draft", by="test")["status"], "draft")

    def test_publish_can_compute_digest_from_path(self):
        self._mk()
        sa.transition("demo", "tested", by="t")
        sa.transition("demo", "reviewed", by="t")
        pkg = self.tmp / "pkg"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text("# demo", encoding="utf-8")
        rec = sa.transition("demo", "published", path=str(pkg), by="t")
        self.assertGreaterEqual(len(rec["digest"] or ""), 32, "应从 path 现算整包摘要")
        self.assertEqual(rec["path"], str(pkg))

    def test_publish_refuses_when_digest_uncomputable(self):
        self._mk()
        sa.transition("demo", "tested", by="t")
        sa.transition("demo", "reviewed", by="t")
        with self.assertRaises(ValueError) as cm:
            sa.transition("demo", "published", path=str(self.tmp / "nope"), by="t")
        self.assertIn("摘要", str(cm.exception))

    def test_published_version_cannot_go_backwards(self):
        self._mk("d2", "2.0.0")
        sa.transition("d2", "tested", by="t")
        sa.transition("d2", "reviewed", by="t")
        sa.transition("d2", "published", digest="e" * 64, by="t")
        with self.assertRaises(ValueError) as cm:
            sa.upsert("d2", "gztxt", "1.9.9", "global")
        self.assertIn("倒退", str(cm.exception))
        sa.upsert("d2", "gztxt", "2.0.1", "global")   # 前进可以
        self.assertEqual(sa.get("d2")["version"], "2.0.1")

    def test_scope_change_leaves_a_trail(self):
        self._mk("d3")
        sa.upsert("d3", "gztxt", "1.0.0", "agent:claude", note="换作用域")
        self.assertIn("scope 变更", sa.get("d3")["note"] or "")

    def test_stale_after_package_changes(self):
        """A9 + C8 焊点：发布后包变了 ⇒ 对外态立刻变 stale。"""
        pkg = self.tmp / "pkg2"
        pkg.mkdir()
        (pkg / "SKILL.md").write_text("# v1", encoding="utf-8")
        self._mk("d4")
        sa.transition("d4", "tested", by="t")
        sa.transition("d4", "reviewed", by="t")
        rec = sa.transition("d4", "published", path=str(pkg), by="t")
        import skill
        self.assertEqual(sa.effective_status(rec, skill.package_digest(str(pkg))["digest"]),
                         "published")
        (pkg / "SKILL.md").write_text("# v2", encoding="utf-8")
        cur = skill.package_digest(str(pkg))["digest"]
        self.assertEqual(sa.effective_status(rec, cur), "stale",
                         "改了包而不重新评审 ⇒ 不得继续以 published 被依赖")


if __name__ == "__main__":
    unittest.main()