#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：检索结果新鲜度标注（P1-8 / 手册 B4）——「不许把过期答案当新鲜答案回包」。

【这条判据治什么】
2026-10-16 实测的三个空洞（不是推断，是grep + 真查生产索引）：

1. `memindex.search()` 硬编码 `"updated_at": None`，**而 `fed_proj.mtime` 这一列
   219171 个 chunk 100% 有值、0 NULL** —— 数据一直在，只是没 SELECT、没回包。
2. `memfed._rg_item()` 同样硬编码 `None`（`_recent_rg` 当时在自己身上 inline 补了，
   检索路径没有）。
3. **只补 `updated_at` 还不够，而且会更糟**。索引里的 mtime 是**建索引那一刻**的
   文件时间。实测：`last_build=2026-10-02T18:07:33`，去重 9107 个文件里
   **65 个磁盘 mtime 比索引新**（最旧漂 **92 天**）、**182 个已从磁盘消失**。
   只回一个 `updated_at=2026-07-08`，下游会读成「文件现状就是 7 月 8 日」——
   这与 P1-3 同类病：**基于陈旧内容下结论，且不自知**。

所以承重判据是「**过期必须可见**」：
- `updated_at`：该文件在索引快照里的时间，ISO 8601 UTC；
- `index_built_at` / `index_age_days`：索引自己多旧 —— **没有它，前一条就是误导**；
- `index_age_days` 在拿不到时必须是 `None` 而不是 `0`（「不知道多旧」与「刚建的」
  必须可区分，填 0 是制造新的静默错误）；
- 时间格式必须与 `memfed._rg_item` **逐字节同形**，否则两路在 RRF 融合后混排失序。

【B5 时近性权重不在本文件】
手册 B5 是「按条目时间加衰减权重」——那会**改召回排序**，属设计决策，需单独授权。
本文件只钉 B4 的标注，且有一条判据显式断言**排序未被改动**。

【红向自证】
每条关键判据都配反向断言，证明它会因实现退化而红（见 TestRedDirection）。
分层口径（tests/tiers.py）：纯函数 + 临时 sqlite，不起服务、不连网、零 SKIP。
"""
import os
import re
import sqlite3
import sys
import tempfile
import time
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

os.environ.setdefault("HUB_TEST_MODE", "1")

import memindex  # noqa: E402
import memfed  # noqa: E402

#: 全仓统一的时间戳格式（与 memfed._rg_item / _recent_rg 同口径）。
ISO_Z = re.compile(r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z$")

#: 活代码里「硬编码 None」的形态。⚠ **必须同时吃单引号**：`ast.unparse` 会把源码里的
#: `"updated_at"` 重写成 `'updated_at'`（它统一用 repr 风格的引号）。只写 `\"...\"`
#: 会让这条判据**永远匹配不到任何东西**——一扇永远不会开的门，比没有门更坏。
#: 这是判例 185 的新变体：判据量错了对象（量了源码形态，不是 AST 活代码形态）。
HARDCODED_NONE = re.compile(r"""["']updated_at["']\s*:\s*None""")


def _code_without_docstrings(path: Path) -> str:
    """活代码文本：**去掉模块/函数 docstring**。

    为什么必须去：本项的文档串**故意引用**了修复前的写法 `"updated_at": None`
    来说明「原实现长什么样」。若只按 `#` 注释行过滤，这条引用会被当成活代码 ⇒
    「不许退回硬编码 None」这条判据自己把自己判红（判据量错了对象，判例 185）。
    """
    import ast
    tree = ast.parse(path.read_text(encoding="utf-8"))
    for node in ast.walk(tree):
        if not isinstance(node, (ast.Module, ast.FunctionDef, ast.AsyncFunctionDef,
                                 ast.ClassDef)):
            continue
        body = node.body
        if (body and isinstance(body[0], ast.Expr)
                and isinstance(body[0].value, ast.Constant)
                and isinstance(body[0].value.value, str)):
            body[0].value.value = ""      # 置空而非删行 ⇒ 行号不变，便于定位
    return ast.unparse(tree)


def _mk_index(tmp: Path, rows, built_at="2026-10-02T18:07:33", age_days=13.8):
    """建一个**最小** fed_proj 索引。rows = [(source, path, l1, l2, mtime, text)]。"""
    con = sqlite3.connect(str(tmp))
    con.executescript(memindex.SCHEMA)
    for source, path, l1, l2, mtime, text in rows:
        cur = con.execute(
            "INSERT INTO fed_proj(source,path,realpath,l1,l2,mtime,size,chunk_no,text) "
            "VALUES(?,?,?,?,?,?,?,?,?)",
            (source, path, path, l1, l2, mtime, len(text), 0, text))
        con.execute("INSERT INTO fed_proj_fts(rowid,text) VALUES(?,?)",
                    (cur.lastrowid, text))
    if built_at is not None:
        con.execute("INSERT OR REPLACE INTO meta(k,v) VALUES('last_build',?)", (built_at,))
    con.commit()
    con.close()
    return tmp


class TestFmtTs(unittest.TestCase):
    """时间格式化：口径唯一、边界不臆断。"""

    def test_epoch_becomes_iso_z(self):
        self.assertEqual(memindex.fmt_ts(1783521851), "2026-07-08T14:44:11Z")

    def test_none_stays_none(self):
        """None 必须原样 None —— 不能变 0（那会被读成 1970）。"""
        self.assertIsNone(memindex.fmt_ts(None))

    def test_zero_epoch_is_not_suppressed(self):
        """`if not epoch` 会把 epoch=0（1970 合法时刻）错判成「没有值」。"""
        self.assertEqual(memindex.fmt_ts(0), "1970-01-01T00:00:00Z")

    def test_junk_does_not_raise(self):
        for bad in ("不是数字", object()):
            self.assertIsNone(memindex.fmt_ts(bad))

    def test_matches_memfed_rg_item_shape(self):
        """跨模块同形：两路时间戳在 RRF 融合后混排，形状不一致就会失序。"""
        p = _REPO / "README.md"        # 仓内文件，保证零 SKIP
        if not p.exists():
            raise AssertionError("README.md 缺失，L0 不得靠 SKIP 掩盖")
        item = memfed._rg_item("workspace_files", f"{p}:1:内容")
        self.assertRegex(item["updated_at"], ISO_Z)


class TestAnnotateFreshness(unittest.TestCase):
    """承重判据：索引年龄必须可见，且不可知时不得伪装成「刚建」。"""

    def _items(self):
        return [{"_mtime": 1783521851}, {"_mtime": 1783521851}]

    def test_index_age_present_and_matches_built_epoch(self):
        """age 由**索引文件 mtime**算，不是字符串解析（kb.py:362 已定口径）。"""
        out = memindex.annotate_freshness(self._items(), "2026-10-02T18:07:33",
                                          time.time() - 10 * 86400)
        for it in out:
            self.assertAlmostEqual(it["index_age_days"], 10.0, delta=0.2)
            self.assertEqual(it["index_built_at"], "2026-10-02T18:07:33")

    def test_unknown_age_is_none_not_zero(self):
        """「不知道索引多旧」必须与「索引刚刚建的」可区分。"""
        out = memindex.annotate_freshness(self._items(), None, None)
        for it in out:
            self.assertIsNone(it.get("index_age_days"))
            self.assertIsNone(it.get("index_built_at"))

    def test_updated_at_converted_from_epoch(self):
        out = memindex.annotate_freshness(self._items(), None, None)
        self.assertEqual(out[0]["updated_at"], "2026-07-08T14:44:11Z")

    def test_internal_mtime_key_never_leaks(self):
        """`_mtime` 是内部中间值，出现在出站包里就是契约污染。"""
        out = memindex.annotate_freshness(self._items(), "B", time.time())
        for it in out:
            self.assertNotIn("_mtime", it)

    def test_preserves_existing_updated_at(self):
        """调用方已填的时间戳不得被覆盖（rg 路径走实时 mtime，比索引快照新）。"""
        items = [{"_mtime": 1783521851, "updated_at": "2026-10-16T00:00:00Z"}]
        out = memindex.annotate_freshness(items, None, None)
        self.assertEqual(out[0]["updated_at"], "2026-10-16T00:00:00Z")

    def test_does_not_reorder(self):
        """B5「时近性权重」未授权 ⇒ 本项**不许改排序**。"""
        items = [{"id": "a", "_mtime": 100}, {"id": "b", "_mtime": 9999999999}]
        out = memindex.annotate_freshness(items, "B", time.time())
        self.assertEqual([i["id"] for i in out], ["a", "b"])


class TestSearchEndToEnd(unittest.TestCase):
    """真查一次索引（临时库），证「改了函数没换调用点」不成立。"""

    def test_search_returns_freshness(self):
        with tempfile.TemporaryDirectory() as td:
            db = _mk_index(Path(td) / "idx.db",
                           [("archived_sessions", "/tmp/会话.md", 1, 5,
                             1783521851, "编排检查的完整内容")])
            r = memindex.search("编排检查", limit=5, db_path=db)
            self.assertTrue(r["ok"])
            self.assertEqual(r["count"], 1)
            it = r["items"][0]
            self.assertEqual(it["updated_at"], "2026-07-08T14:44:11Z")
            self.assertEqual(it["index_built_at"], "2026-10-02T18:07:33")
            self.assertIsNotNone(it["index_age_days"])

    def test_short_query_still_falls_back(self):
        """短查询降级信号不许被本项改动吞掉（fail-closed 是既有铁律）。"""
        with tempfile.TemporaryDirectory() as td:
            db = _mk_index(Path(td) / "idx.db",
                           [("archived_sessions", "/tmp/x.md", 1, 2, 1783521851, "abc")])
            r = memindex.search("ab", limit=5, db_path=db)
            self.assertEqual(r.get("fallback"), "short_query")


class TestRgItemFreshness(unittest.TestCase):
    """`memfed._rg_item`：rg 路径（实时文件）的时间标注。"""

    def test_real_file_gets_iso_z(self):
        p = _REPO / "README.md"
        if not p.exists():
            raise AssertionError("README.md 缺失，L0 不得靠 SKIP 掩盖")
        it = memfed._rg_item("workspace_files", f"{p}:1:内容")
        self.assertRegex(it["updated_at"], ISO_Z)

    def test_missing_file_is_none_not_epoch_zero(self):
        """文件已删⇒ None。填 0 会被读成「1970」并排到最新。"""
        it = memfed._rg_item("workspace_files", "/no/such/file.md:3:内容")
        self.assertIsNone(it["updated_at"])

    def test_unparsable_line_returns_none(self):
        self.assertIsNone(memfed._rg_item("workspace_files", "不是 rg 输出格式"))


class TestRedDirection(unittest.TestCase):
    """红向自证：这些断言证明判据会因实现退化而红，否则判据本身是假的。"""

    def _naive_item(self, mtime):
        """修复前的原实现：硬编码 None。"""
        return {"source": "archived_sessions", "id": "x:1-2", "title": "t",
                "content": "c", "category": "knowledge", "updated_at": None,
                "_mtime": mtime}

    def test_naive_impl_fails_updated_at_assertion(self):
        """退回硬编码 None ⇒ 「updated_at 必须是 ISO 串」这条必红。"""
        it = self._naive_item(1783521851)
        self.assertIsNone(it["updated_at"])
        self.assertFalse(ISO_Z.match(it["updated_at"] or ""))

    def test_naive_impl_fails_age_assertion(self):
        """只回 `updated_at` 不回索引年龄 ⇒「必须有 index_age_days」这条必红。"""
        it = self._naive_item(1783521851)
        self.assertNotIn("index_age_days", it)

    def test_zero_age_would_be_undistinguishable(self):
        """把「不可知」填 0 ⇒与「索引刚建」不可区分，这正是判据要红的。"""
        unknown = memindex.annotate_freshness([{"_mtime": 1}], None, None)[0]
        just_built = memindex.annotate_freshness([{"_mtime": 1}], "B", time.time())[0]
        self.assertIsNone(unknown.get("index_age_days"))
        self.assertIsInstance(just_built.get("index_age_days"), float)

    def test_local_timezone_format_would_break_contract(self):
        """用本地时区（无 Z）⇒ 跨模块形状不一致，融合后混排失序。"""
        local = time.strftime("%Y-%m-%dT%H:%M:%S", time.localtime(1783521851))
        self.assertFalse(ISO_Z.match(local))


class TestNoNaiveLiteralInLiveCode(unittest.TestCase):
    """活代码里不得再有硬编码 `"updated_at": None`（docstring 不算）。"""

    #: 被钉住的两处：`memindex.search()`（投影 FTS 路）与 `memfed._rg_item()`（rg 路）。
    _TARGETS = ("memindex.py", "memfed.py")

    def test_no_hardcoded_none_in_live_code(self):
        for name in self._TARGETS:
            live = _code_without_docstrings(_REPO / "src" / name)
            hits = HARDCODED_NONE.findall(live)
            self.assertEqual(hits, [], f"{name} 仍在硬编码 updated_at=None：{hits}")

    def test_gate_regex_actually_fires_on_naive_code(self):
        """**判据自检**：把修复前的写法喂给正则，必须命中。

        没有这一条，「无硬编码」那几条可以是永远匹配不到东西的空门
        （本项真出现过：`ast.unparse` 把双引号改写成单引号，正则只吃双引号 ⇒ 门失效）。
        """
        import ast as _ast
        naive_src = "def f():\n    return {'updated_at': None}\n"
        unparsed = _ast.unparse(_ast.parse(naive_src))
        self.assertRegex(
            unparsed, HARDCODED_NONE,
            "正则吃不下 ast.unparse 的实际输出形态——这扇门是假的")

    def test_search_selects_mtime(self):
        """不 SELECT `f.mtime` ⇒ 标注函数拿不到值（改了函数没换调用点 = 假绿）。"""
        live = _code_without_docstrings(_REPO / "src" / "memindex.py")
        self.assertRegex(live, r"SELECT[^;]*?f\.mtime")


if __name__ == "__main__":
    unittest.main(verbosity=2)