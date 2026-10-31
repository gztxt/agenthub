#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：参考情报「来源」——自有 GitHub 仓（含 fork）并入候选池。

守四条判据（每条都对应一个具体的坏后果）：

1. **只认 github.com**：`gitlab.com/x` 若被当成 `owner/repo`，拼进 GitHub API 只会
   以 404 收场，用户看到的是「这个来源没数据」，不是「地址写错了」。
2. **来源必须真的进池**：只给卡片贴「这是你的仓」标签是无效的——自有仓多为 0 star
   或 fork，全站搜索本来就进不来，标不到任何东西。
3. **archived 仍是硬闸门**：不因「是我自己的仓」破例；许可证/维护状态不豁免。
4. **来源坏了不能拖垮整卡**：单个来源 403/404 只进「逐路表态 / 降级」，检索照常出卡。
"""
import json
import pathlib
import re
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db          # noqa: E402
import refscout    # noqa: E402
import refscout_sources  # B4 切分：补丁要打在使用它的模块上


def _repo_item(name, desc, **over):
    item = {"full_name": name, "name": name.split("/")[-1],
            "description": desc, "html_url": "https://github.com/" + name,
            "stargazers_count": 0, "fork": True, "archived": False,
            "language": "Go", "pushed_at": "2026-10-01T00:00:00Z",
            "default_branch": "main", "topics": [],
            "license": {"spdx_id": "MIT"}}
    item.update(over)
    return item


class TestSourceInput(unittest.TestCase):
    def test_accepts_account_and_repo_forms(self):
        self.assertEqual(refscout._norm_source("gztxt"), ("owner", "gztxt"))
        self.assertEqual(refscout._norm_source("github.com/gztxt"), ("owner", "gztxt"))
        self.assertEqual(refscout._norm_source("https://github.com/gztxt"),
                         ("owner", "gztxt"))
        self.assertEqual(refscout._norm_source("gztxt/agenthub"), ("repo", "gztxt/agenthub"))
        self.assertEqual(refscout._norm_source("https://github.com/gztxt/agenthub.git"),
                         ("repo", "gztxt/agenthub"))
        self.assertEqual(refscout._norm_source("github.com/gztxt/agenthub/tree/main/src"),
                         ("repo", "gztxt/agenthub"))

    def test_rejects_other_hosts_and_bad_names(self):
        for bad in ("gitlab.com/x", "a b", "gztxt/../etc", "x" * 200):
            with self.assertRaises(Exception, msg=bad):
                refscout._norm_source(bad)

    def test_accepts_starred_forms(self):
        """星标来源：`starred:gztxt` / 星标页地址都要归一成同一条。"""
        for raw in ("starred:gztxt", "gztxt:starred",
                    "https://github.com/gztxt?tab=stars",
                    "github.com/stars/gztxt",
                    "github.com/stars/gztxt/lists/ai"):
            self.assertEqual(refscout._norm_source(raw), ("starred", "gztxt"), raw)

    def test_starred_marker_must_be_exact(self):
        for bad in ("gztxt:whatever", "starred:", "stars:"):
            with self.assertRaises(Exception, msg=bad):
                refscout._norm_source(bad)


class TestSourceCrud(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        db.init_db(self.tmp / "agents.db")
        refscout.ensure_schema()

    def test_add_is_idempotent_and_delete_removes(self):
        a = refscout.add_source("https://github.com/gztxt")
        self.assertFalse(a["deduped"])
        b = refscout.add_source("gztxt")
        self.assertTrue(b["deduped"])
        self.assertEqual(a["id"], b["id"])
        self.assertEqual(refscout.list_sources()["total"], 1)
        refscout.delete_source(a["id"])
        self.assertEqual(refscout.list_sources()["total"], 0)

    def test_delete_missing_is_404(self):
        with self.assertRaises(Exception) as cm:
            refscout.delete_source("nope")
        self.assertEqual(getattr(cm.exception, "status_code", None), 404)

    def test_same_account_owner_and_starred_coexist(self):
        """`gztxt`（自己的仓）与 `gztxt:starred`（标星的仓）候选集不同，必须各存一条。"""
        a = refscout.add_source("gztxt")
        b = refscout.add_source("gztxt:starred")
        self.assertNotEqual(a["id"], b["id"])
        self.assertEqual(a["kind"], "owner")
        self.assertEqual(b["kind"], "starred")
        self.assertEqual(refscout.list_sources()["total"], 2)
        labels = [s["label"] for s in refscout.list_sources()["items"]]
        self.assertIn("star:gztxt", labels)
        self.assertTrue(refscout.add_source("gztxt:starred")["deduped"])


class TestSourceFetch(unittest.TestCase):
    def test_owner_paginates_and_keeps_forks(self):
        calls = []

        def fake(url, ttl_s=None, limiter=None):
            calls.append(url)
            return [_repo_item("gztxt/a", "x"), _repo_item("gztxt/b", "y")], \
                {"ok": True, "ms": 1, "cached": False}

        old = refscout_sources.gh_get
        refscout_sources.gh_get = fake
        try:
            got = refscout._fetch_source("owner", "gztxt")
        finally:
            refscout_sources.gh_get = old
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["items"]), 2)
        self.assertIn("type=owner", calls[0])
        self.assertIn("/users/gztxt/repos", calls[0])

    def test_org_fallback_only_on_404(self):
        def fake(url, ttl_s=None, limiter=None):
            if "/users/acme/" in url:
                return None, {"ok": False, "status": 404, "error": "HTTP404"}
            return [_repo_item("acme/one", "z")], {"ok": True, "ms": 1}

        old = refscout_sources.gh_get
        refscout_sources.gh_get = fake
        try:
            got = refscout._fetch_source("owner", "acme")
        finally:
            refscout_sources.gh_get = old
        self.assertTrue(got["ok"])
        self.assertEqual(got["items"][0]["full_name"], "acme/one")

    def test_rate_limit_does_not_try_org_endpoint(self):
        urls = []

        def fake(url, ttl_s=None, limiter=None):
            urls.append(url)
            return None, {"ok": False, "status": 403, "error": "HTTP403 rate limit"}

        old = refscout_sources.gh_get
        refscout_sources.gh_get = fake
        try:
            got = refscout._fetch_source("owner", "acme")
        finally:
            refscout_sources.gh_get = old
        self.assertFalse(got["ok"])
        self.assertFalse(any("/orgs/" in u for u in urls),
                         "403 是限流，换端点只会多烧一次额度")

    def test_starred_paginates_and_keeps_forks(self):
        """星标走 /starred，不带 type 过滤（星标里本来就可能是我认可的 fork）。"""
        urls = []
        page1 = [_repo_item(f"o/f{i}", "x") for i in range(100)]

        def fake(url, ttl_s=None, limiter=None):
            urls.append(url)
            return (page1 if re.search(r"[?&]page=1(&|$)", url) else []), {"ok": True, "ms": 1}

        old = refscout_sources.gh_get
        refscout_sources.gh_get = fake
        try:
            got = refscout._fetch_source("starred", "gztxt")
        finally:
            refscout_sources.gh_get = old
        self.assertTrue(got["ok"])
        self.assertEqual(len(got["items"]), 100)
        self.assertIn("/users/gztxt/starred", urls[0])
        self.assertNotIn("type=owner", urls[0])
        self.assertIn("page=2", urls[1], "第一页满 100 条就必须继续翻页")


class TestSourceInPool(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        db.init_db(self.tmp / "agents.db")
        refscout.ensure_schema()
        refscout.add_source("gztxt")

    def _fake_gh(self, source_items, source_meta=None):
        def fake(url, ttl_s=None, limiter=None):
            if url.startswith(refscout.GH_API + "/users/gztxt/repos"):
                return source_items, (source_meta or {"ok": True, "ms": 3, "cached": False})
            if "/search/repositories" in url:
                return {"items": []}, {"ok": True, "ms": 1}
            if "/search/code" in url:
                return {"items": []}, {"ok": True, "ms": 1}
            if url.endswith("/readme") or "/git/trees/" in url or "/git/ref/" in url:
                return None, {"ok": False, "status": 404}
            return None, {"ok": False, "status": 404}
        return fake

    def _collect(self, fake):
        old = refscout_sources.gh_get
        refscout_sources.gh_get = fake
        try:
            return refscout.collect("终端复用", ["terminal", "websocket", "pty"], "", 20,
                                    180, [], False, do_rerank=False)
        finally:
            refscout_sources.gh_get = old

    def test_zero_star_fork_still_enters_candidates(self):
        got = self._collect(self._fake_gh([
            _repo_item("gztxt/term-demo", "share a terminal over websocket with pty"),
            _repo_item("gztxt/gardening", "notes about tomato planting"),
        ]))
        repos = [c["repo"] for c in got["candidates"]]
        self.assertIn("gztxt/term-demo", repos, "0 star / fork 的自有仓必须能进池")
        self.assertNotIn("gztxt/gardening", repos, "不相关自有仓仍要被覆盖度闸门挡住")
        cand = next(c for c in got["candidates"] if c["repo"] == "gztxt/term-demo")
        self.assertEqual(cand["sources"], ["gztxt"])
        self.assertGreater(cand["src_bonus"], 0)
        self.assertEqual(got["meta"]["sources"][0]["count"], 2)

    def test_archived_source_repo_is_rejected(self):
        got = self._collect(self._fake_gh([
            _repo_item("gztxt/dead", "terminal websocket pty", archived=True),
        ]))
        self.assertNotIn("gztxt/dead", [c["repo"] for c in got["candidates"]])

    def test_source_failure_is_reported_not_fatal(self):
        got = self._collect(self._fake_gh([], {"ok": False, "status": 403,
                                               "error": "HTTP403 rate limit"}))
        self.assertEqual(got["meta"]["sources"][0]["ok"], False)
        self.assertTrue(any("src:gztxt" in d for d in got["meta"]["degraded"]))

    def test_card_marks_source_column_and_header(self):
        got = self._collect(self._fake_gh([
            _repo_item("gztxt/term-demo", "share a terminal over websocket with pty")]))
        md = refscout.render_md("终端复用", ["terminal", "websocket", "pty"], "",
                                got["meta"], got["candidates"], got["elapsed"],
                                "2026-10-21T00:00:00+00:00",
                                "2026-11-20T00:00:00+00:00")
        self.assertIn("| 仓库 | 来源 |", md)
        self.assertIn("来源（自有仓，含 fork，不占搜索额度）：gztxt", md)
        self.assertIn("| [gztxt/term-demo]", md)
        self.assertIn("gztxt，不走搜索额度", md)


class TestStarredInPool(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        db.init_db(self.tmp / "agents.db")
        refscout.ensure_schema()
        refscout.add_source("gztxt:starred")

    def test_starred_repos_join_pool_with_own_label(self):
        def fake(url, ttl_s=None, limiter=None):
            if "/users/gztxt/starred" in url:
                return [_repo_item("lukasmasuch/best-of-python",
                                   "A ranked list of awesome Python open-source libraries",
                                   fork=False),
                        _repo_item("someone/ttyd", "share a terminal over websocket with pty",
                                   fork=False)], {"ok": True, "ms": 2}
            if "/search/" in url:
                return {"items": []}, {"ok": True, "ms": 1}
            return None, {"ok": False, "status": 404}

        old = refscout_sources.gh_get
        refscout_sources.gh_get = fake
        try:
            got = refscout.collect("终端复用", ["terminal", "websocket", "pty"], "", 20,
                                   180, [], False, do_rerank=False)
        finally:
            refscout_sources.gh_get = old
        cand = next(c for c in got["candidates"] if c["repo"] == "someone/ttyd")
        self.assertEqual(cand["sources"], ["star:gztxt"])
        self.assertEqual(got["meta"]["sources"][0]["kind"], "starred")


class TestValueFirstOrder(unittest.TestCase):
    """排序主键：参考价值优先，来源只作同分次序（用户 2026-10-21 裁定）。"""

    def _c(self, bucket="impl", func=0.6, impl=0.5, design=0.2, bonus=0.0):
        return {"bucket": bucket, "func": func, "impl": impl, "design": design,
                "src_bonus": bonus}

    def test_source_bonus_never_beats_value(self):
        ext = self._c(func=0.62)
        own = self._c(func=0.50, bonus=refscout.SOURCE_BONUS)
        self.assertLess(refscout._order_key(ext), refscout._order_key(own),
                        "0.06 加成若压过功能分，就是「是我的仓」优先于「值得借鉴」")

    def test_source_wins_only_on_exact_tie(self):
        plain = self._c(func=0.60)
        own = self._c(func=0.60, bonus=refscout.SOURCE_BONUS)
        self.assertLess(refscout._order_key(own), refscout._order_key(plain))

    def test_low_value_impl_still_ahead_of_list_bucket(self):
        low_impl = self._c(func=0.10, impl=0.10, design=0.0)
        high_list = self._c(bucket="list", func=0.99, impl=0.99, design=0.99)
        self.assertLess(refscout._order_key(low_impl), refscout._order_key(high_list))


class TestCardSummary(unittest.TestCase):
    def test_card_shows_extracted_readme_summary(self):
        cand = {"repo": "a/b", "url": "https://github.com/a/b", "desc": "d",
                "bucket": "impl", "sources": [], "src_bonus": 0.0,
                "func": 0.6, "impl": 0.5, "design": 0.2, "coverage": 0.5,
                "stars": 10, "license": "MIT", "copyleft": False, "fresh_days": 3,
                "arch_docs": False, "branch": "main", "head": "",
                "code_files": [], "topics": [], "sample_paths": [],
                "readme_gist": "把 GitHub 上的清单聚合成可检索的索引",
                "judge": "", "why": "", "llm": None}
        md = refscout.render_md("X", ["a"], "", {"pool_size": 1, "arms": [], "sources": []},
                                [cand], 1.0, "2026-10-21T00:00:00+00:00",
                                "2026-11-20T00:00:00+00:00")
        self.assertIn("- 摘要：把 GitHub 上的清单聚合成可检索的索引", md)


class TestSourceApiShape(unittest.TestCase):
    def test_status_counts_sources(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        db.init_db(tmp / "agents.db")
        refscout.ensure_schema()
        refscout.add_source("gztxt")
        self.assertEqual(len(refscout.list_sources()["items"]), 1)
        self.assertEqual(json.loads(json.dumps(refscout.list_sources()))["total"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
