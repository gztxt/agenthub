#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：清单展开 + 补全窗口口径。

守四条判据：

1. **清单只是目录**：`HelloGitHub` 根 README 只有 1 个项目链接、36 个 `content/*.md`，
   只解析根 README 等于没展开（实测）。必须能追子页。
2. **展开结果和普通候选同规则**：`archived` / `min_stars` 照常生效，不能因为是
   「清单推荐的」就绕过质量闸门。
3. **补全集合必须包含重排集合**：否则重排会拿到没抓 README 的仓 —— 实现轴少信号、
   卡上没摘要（2026-10-22 生产实测）。
4. **卡面要能看出项目来自哪份清单**：否则用户分不清「找到的项目」与「清单本身」。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import refscout  # noqa: E402


def _repo_item(name, stars=100, **over):
    item = {"full_name": name, "name": name.split("/")[-1],
            "description": "a tool", "html_url": "https://github.com/" + name,
            "stargazers_count": stars, "fork": False, "archived": False,
            "language": "Python", "pushed_at": "2026-10-01T00:00:00Z",
            "default_branch": "main", "topics": [], "license": {"spdx_id": "MIT"}}
    item.update(over)
    return item


def _cand(**over):
    c = {"repo": "a/b", "url": "https://github.com/a/b", "desc": "d", "bucket": "impl",
         "seen": ["core"], "sources": [], "via": [], "enriched": True,
         "func": 0.5, "impl": 0.5, "design": 0.2, "coverage": 0.5, "stars": 100,
         "license": "MIT", "copyleft": False, "fresh_days": 10, "arch_docs": False,
         "branch": "main", "head": "abc123456789", "code_files": [], "topics": [],
         "readme_gist": "gist", "sample_paths": [], "judge": "read", "why": "ok"}
    c.update(over)
    return c


class TestLinkExtraction(unittest.TestCase):
    def test_extracts_and_dedupes(self):
        text = ("[a](https://github.com/foo/bar) [b](https://github.com/foo/bar) "
                "[c](http://github.com/baz/qux.git)")
        self.assertEqual(refscout._gh_links(text), ["foo/bar", "baz/qux"])

    def test_drops_site_pages_and_self(self):
        text = ("github.com/features/copilot github.com/topics/ai "
                "github.com/521xueweihan/HelloGitHub/blob/master/a.md")
        self.assertEqual(refscout._gh_links(text, "521xueweihan/HelloGitHub"), [])

    def test_subpages_only_same_repo_md(self):
        text = ("[114](content/HelloGitHub114.md) [ext](https://example.com/x.md) "
                "[readme](README.md) [doc](docs/design.md)")
        self.assertEqual(refscout._md_subpages(text, 5),
                         ["content/HelloGitHub114.md", "docs/design.md"])

    def test_subpage_limit_keeps_newest_first(self):
        text = "".join(f"[x](content/HelloGitHub{i:03d}.md)" for i in range(1, 9))
        self.assertEqual(refscout._md_subpages(text, 3),
                         ["content/HelloGitHub001.md", "content/HelloGitHub002.md",
                          "content/HelloGitHub003.md"])


class TestExpandLists(unittest.TestCase):
    def setUp(self):
        self.list_item = _repo_item("521xueweihan/HelloGitHub", stars=180000)
        self.readme = "[p1](https://github.com/foo/bar) [114](content/HelloGitHub114.md)"
        self.page = ("[a](https://github.com/baz/qux) [b](https://github.com/old/dead) "
                     "[c](https://github.com/tiny/repo) [d](https://github.com/foo/bar)")
        self._old = (refscout._readme_text, refscout._contents_text, refscout.gh_get)

    def tearDown(self):
        (refscout._readme_text, refscout._contents_text, refscout.gh_get) = self._old

    def _patch(self, stars=None):
        refscout._readme_text = lambda full: self.readme
        refscout._contents_text = lambda full, path: self.page
        table = {
            "foo/bar": _repo_item("foo/bar", stars=(stars or {}).get("foo/bar", 300)),
            "baz/qux": _repo_item("baz/qux", stars=(stars or {}).get("baz/qux", 120)),
            "tiny/repo": _repo_item("tiny/repo", stars=(stars or {}).get("tiny/repo", 3)),
            "old/dead": _repo_item("old/dead", archived=True),
        }

        def fake(url, ttl_s=None, limiter=None):
            name = url.rsplit("/repos/", 1)[-1]
            return table.get(name), {"ok": name in table}

        refscout.gh_get = fake

    def test_expands_readme_and_subpage(self):
        self._patch()
        found, logs = refscout._expand_lists([self.list_item], 0, set())
        self.assertEqual(set(found), {"foo/bar", "baz/qux", "tiny/repo"})
        self.assertEqual(found["foo/bar"]["via"], {"521xueweihan/HelloGitHub"})
        self.assertEqual(logs[0]["pages"], 1, "追了 1 篇子页")
        self.assertTrue(logs[0]["ok"])

    def test_archived_and_min_stars_still_apply(self):
        self._patch()
        found, _ = refscout._expand_lists([self.list_item], 50, set())
        self.assertNotIn("old/dead", found, "archived 不因清单推荐而豁免")
        self.assertNotIn("tiny/repo", found, "min_stars 照常生效")

    def test_known_keys_are_not_refetched(self):
        self._patch()
        found, _ = refscout._expand_lists([self.list_item], 0, {"foo/bar"})
        self.assertNotIn("foo/bar", found)

    def test_total_cap(self):
        self._patch()
        old = refscout.LIST_EXPAND_TOTAL
        refscout.LIST_EXPAND_TOTAL = 2
        try:
            found, _ = refscout._expand_lists([self.list_item], 0, set())
        finally:
            refscout.LIST_EXPAND_TOTAL = old
        self.assertEqual(len(found), 2)

    def test_keep_gate_blocks_irrelevant_items(self):
        self._patch()
        found, _ = refscout._expand_lists(
            [self.list_item], 0, set(), lambda item: item["full_name"] == "foo/bar")
        self.assertEqual(set(found), {"foo/bar"})

    def test_readme_failure_is_logged_not_raised(self):
        self._patch()
        refscout._readme_text = lambda full: ""
        found, logs = refscout._expand_lists([self.list_item], 0, set())
        self.assertEqual(found, {})
        self.assertFalse(logs[0]["ok"])
        self.assertIn("readme", logs[0]["error"])


class TestPickLists(unittest.TestCase):
    def test_detects_hellogithub_style_own_source(self):
        """自有来源里 README 像目录 ⇒ 认成清单（描述里没有 awesome/curated 也要认）。"""
        readme = ("[p](https://github.com/foo/bar)" +
                  "".join(f"[x](content/HelloGitHub{i:03d}.md)" for i in range(1, 15)))
        pre = [{"item": _repo_item("521xueweihan/HelloGitHub"), "bucket": "impl",
                "readme": readme, "sources": ["star:gztxt"]}]
        got = refscout._pick_lists(pre, 4)
        self.assertEqual([x["full_name"] for x in got], ["521xueweihan/HelloGitHub"])

    def test_plain_project_is_not_expanded(self):
        pre = [{"item": _repo_item("apscheduler/apscheduler"), "bucket": "impl",
                "readme": "A cron scheduler. [docs](https://example.com/x)"}]
        self.assertEqual(refscout._pick_lists(pre, 4), [])

    def test_linky_repo_that_is_not_yours_is_not_expanded(self):
        """链接多但既不是 list 桶、也不是自有来源 ⇒ 不展开（否则会烧掉预算）。"""
        readme = "".join(f"[x](https://github.com/o{i}/r{i})" for i in range(12))
        pre = [{"item": _repo_item("mona-actions/gh-repo-stats"), "bucket": "impl",
                "readme": readme, "sources": []}]
        self.assertEqual(refscout._pick_lists(pre, 4), [])

    def test_list_bucket_always_eligible(self):
        pre = [{"item": _repo_item("vinta/awesome-python"), "bucket": "list"}]
        self.assertEqual(len(refscout._pick_lists(pre, 4)), 1)


class TestRerankShortlist(unittest.TestCase):
    def test_only_enriched_candidates_are_reranked(self):
        """补全集合 ⊆ 重排集合，反过来就会拿到没 README 的仓。"""
        seen = {}

        def fake(module, cands, allow_paid):
            seen["repos"] = [c["repo"] for c in cands]
            return {}, "boom", "", []

        old = refscout.rerank
        refscout.rerank = fake
        try:
            ranked = [_cand(repo=f"e{i}", enriched=True) for i in range(10)]
            ranked += [_cand(repo="no-readme", enriched=False, impl=0.99)]
            refscout.apply_rerank("m", ranked, False)
        finally:
            refscout.rerank = old
        self.assertEqual(len(seen["repos"]), refscout.MAX_CANDIDATES)
        self.assertNotIn("no-readme", seen["repos"])
        self.assertTrue(all(r.startswith("e") for r in seen["repos"]))


class TestCardShowsProvenance(unittest.TestCase):
    def test_via_and_expand_summary_rendered(self):
        meta = {"pool_size": 10, "list_expand": [
            {"name": "list:521xueweihan/HelloGitHub", "ok": True, "pages": 4,
             "links": 58, "count": 40}]}
        cand = _cand(repo="foo/bar", via=["521xueweihan/HelloGitHub"])
        md = refscout.render_md("m", ["k"], "", meta, [cand], 12.3,
                               "2026-10-22T00:00:00+00:00", "2026-11-21T00:00:00+00:00")
        self.assertIn("清单展开", md)
        self.assertIn("入池 40", md)
        self.assertIn("收录于清单：521xueweihan/HelloGitHub", md)
        self.assertIn("清单:HelloGitHub", md, "主表来源列也要标出来")


class TestCatalogItselfIsNotACandidate(unittest.TestCase):
    """用户裁定（2026-10-22）：要的是清单**收录的项目**，不是清单本身。

    反例（生产卡 `0d010734abb8`）：`521xueweihan/HelloGitHub` 被 `classify` 判成
    `impl`（描述里有 GitHub 二字），于是它自己占了实现桶表一行，而它的 impl 分是从
    「目录 README」算出来的，与「这个项目怎么实现」无关。
    """

    def _md(self, meta_extra=None):
        meta = {"pool_size": 10,
                "sources": [{"label": "star:gztxt", "count": 261, "ok": True}],
                "list_expand": [{"name": "list:521xueweihan/HelloGitHub", "ok": True,
                                 "pages": 1, "links": 30, "count": 1}]}
        meta.update(meta_extra or {})
        cands = [
            _cand(repo="521xueweihan/HelloGitHub", stars=183527,
                  sources=["star:gztxt"], desc="分享 GitHub 上有趣的开源项目"),
            _cand(repo="seart-group/ghs", via=["521xueweihan/HelloGitHub"]),
        ]
        return refscout.render_md("m", ["k"], "", meta, cands, 12.3,
                                  "2026-10-22T00:00:00+00:00",
                                  "2026-11-21T00:00:00+00:00")

    def test_catalog_not_in_candidate_table(self):
        md = self._md({"list_sources": ["521xueweihan/HelloGitHub"]})
        table = md.split("## 候选（实现桶）")[1].split("##")[0]
        self.assertNotIn("521xueweihan/HelloGitHub", table,
                         "目录仓不能占实现桶表的一行")
        self.assertIn("seart-group/ghs", table, "收录的项目要在表里")

    def test_catalog_still_visible_as_source(self):
        md = self._md({"list_sources": ["521xueweihan/HelloGitHub"]})
        self.assertIn("清单仓本身是目录，不计入候选", md)
        own = md.split("## 自有来源命中")[1].split("##")[0]
        self.assertIn("521xueweihan/HelloGitHub", own, "自己 star 的目录仍要露脸")
        self.assertIn("清单（已展开）", own)

    def test_without_list_sources_nothing_is_filtered(self):
        """旧卡没有 `list_sources` 字段（历史数据）⇒ 不能因为缺字段把候选吃掉。"""
        md = self._md()
        table = md.split("## 候选（实现桶）")[1].split("##")[0]
        self.assertIn("521xueweihan/HelloGitHub", table)


if __name__ == "__main__":
    unittest.main()
