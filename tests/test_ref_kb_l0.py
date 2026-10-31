"""参考情报长期知识库（2026-10-31）：词元、垫池 SQL、安装候选判据。

不碰真库：只测纯函数与 SQL 形状。写路径由 L2 探针 + 生产实测覆盖
（`/api/ref/kb` 与真实检索后的 `seen_n` 递增）。
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import refscout_kb as kb  # noqa: E402


class TestTokens(unittest.TestCase):
    def test_lowercases_dedupes_and_drops_short(self):
        got = kb.tokens("Event Dispatch event 派发 a,b")
        self.assertEqual(got[:2], ["dispatch", "event"], "长词优先、去重、丢弃 <3 字符")
        self.assertNotIn("a", got)

    def test_keeps_repo_chars(self):
        got = kb.tokens("node-pty @x/y c++")
        self.assertIn("node-pty", got)
        self.assertIn("c++", got)

    def test_empty_ok(self):
        self.assertEqual(kb.tokens(""), [])


class TestSeedSql(unittest.TestCase):
    def test_like_wildcards_are_escaped(self):
        sql, params = kb.seed_sql(["100%_a"])
        self.assertIn("ESCAPE", sql)
        self.assertTrue(any("\\%" in p and "\\_" in p for p in params),
                        "% 与 _ 必须转义，否则一个词就能匹配全表")

    def test_limit_is_applied(self):
        sql, params = kb.seed_sql(["abc"], 7)
        self.assertTrue(sql.rstrip().endswith("LIMIT ?"))
        self.assertEqual(params[-1], 7)

    def test_no_tokens_falls_back_to_popular(self):
        sql, params = kb.seed_sql([], 5)
        self.assertIn("stars DESC", sql)
        self.assertEqual(params[-1], 5)


class TestInstallWorthy(unittest.TestCase):
    def _row(self, **kw):
        base = {"install_state": "", "verdict": "use", "copyleft": 0, "archived": 0, "stars": 500}
        base.update(kw)
        return base

    def test_use_verdict_qualifies(self):
        self.assertTrue(kb.install_worthy(self._row()))

    def test_copyleft_rejected(self):
        self.assertFalse(kb.install_worthy(self._row(copyleft=1)),
                         "copyleft 不得自动进安装候选（本机军规不做传染性依赖）")

    def test_skip_verdict_rejected(self):
        self.assertFalse(kb.install_worthy(self._row(verdict="skip")))

    def test_already_handled_is_not_re_recommended(self):
        for st in ("installed", "tested", "rejected", "candidate"):
            self.assertFalse(kb.install_worthy(self._row(install_state=st)), st)

    def test_min_stars_floor(self):
        self.assertFalse(kb.install_worthy(self._row(stars=3), min_stars=50))


class TestSchemaShape(unittest.TestCase):
    def test_kb_table_declared(self):
        self.assertIn("ref_repo_kb", kb.SCHEMA)
        for col in ("readme_gist", "axes", "verdict", "install_state", "seen_n"):
            self.assertIn(col, kb.SCHEMA, f"缺列 {col}")

    def test_seed_wired_before_pre_score(self):
        src = pathlib.Path(ROOT / "src" / "refscout_collect.py").read_text(encoding="utf-8")
        i_seed = src.index("_rc_kb.seed_rows")
        i_pre = src.index("def build_pre")
        self.assertLess(i_seed, i_pre, "垫池必须在预分之前，否则 IDF 看不到这些候选")

    def test_write_through_after_rerank(self):
        src = pathlib.Path(ROOT / "src" / "refscout_collect.py").read_text(encoding="utf-8")
        i_rr = src.index("apply_rerank(module, ranked")
        i_up = src.index("_rc_kb.upsert(ranked, module, keywords)", i_rr)
        self.assertGreater(i_up, i_rr, "rerank 之后要再写一次，verdict 才有值")
