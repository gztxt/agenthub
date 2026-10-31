"""清单展开的「显式来源保位」与查询级缓存（2026-10-31 用户裁定 1/2/4 + 缓存）。

实测背景（卡 `62ebb8e53d02`，module=派发）：
- 用户显式登记了 `521xueweihan/HelloGitHub`，来源同步正常（取了 1 个仓）
- 但 4 个展开名额全被无关 list 仓占掉（`Sfedfcv/redesigned-pancake` README 拉不到…）
- 结果 `list_expand` 四条全 count=0 ⇒ **显式来源一次都没被展开**

四条判据：
1. 显式来源（`sources` 非空）优先占位，不参与相关度竞争；
2. 每个显式来源至少 1 个名额（round-robin），一个来源吃不光全部名额；
3. 子页上限 12（HelloGitHub 一年约 12 期，原来只读 4 页）；
4. 显式来源的收录关系免覆盖度闸门（`trusted`），硬指标（archived/min_stars）照旧。
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import refscout_base as base  # noqa: E402
import refscout_collect as collect  # noqa: E402
import refscout  # noqa: E402


def _pre(full, bucket="impl", sources=None, readme=""):
    return {"item": {"full_name": full}, "bucket": bucket,
            "sources": sources or [], "readme": readme}


class TestExplicitSourceSlots(unittest.TestCase):
    def test_explicit_source_beats_higher_scored_junk(self):
        # 预分顺序里前 4 个都是无关 list 仓（旧实现照单全收）
        pre = [_pre("junk/a", "list"), _pre("junk/b", "list"),
               _pre("junk/c", "list"), _pre("junk/d", "list"),
               _pre("521xueweihan/HelloGitHub", "impl", sources=["521xueweihan/HelloGitHub"])]
        got = [x["full_name"] for x in collect._pick_lists(pre, base.LIST_EXPAND_LISTS)]
        self.assertIn("521xueweihan/HelloGitHub", got,
                      "显式来源必须占到名额 —— 用户加它就是「我认定它是知识源」")

    def test_each_explicit_source_gets_a_slot(self):
        # 两个来源各2个候选，只给 2 个名额 ⇒ 必须一边一个（round-robin），不能一个来源吃掉全部
        pre = [_pre("own/one", "list", sources=["own"]),
               _pre("own/two", "list", sources=["own"]),
               _pre("star/x", "list", sources=["star:gztxt"]),
               _pre("star/y", "list", sources=["star:gztxt"])]
        got = [x["full_name"] for x in collect._pick_lists(pre, 2)]
        self.assertEqual(len(got), 2)
        self.assertIn("star/x", got, "第二个来源必须也拿到名额（round-robin）")
        self.assertIn("own/one", got)

    def test_non_source_repos_still_not_expanded_blindly(self):
        pre = [_pre("someone/normal", "impl")]
        self.assertEqual(collect._pick_lists(pre, 4), [],
                         "非来源的普通仓不得当清单展开（会白烧预算）")

    def test_trusted_set_covers_explicit_lists_only(self):
        pre = [_pre("521xueweihan/HelloGitHub", "impl", sources=["521xueweihan/HelloGitHub"]),
               _pre("donnemartin/system-design-primer", "list")]
        lists = [{"full_name": "521xueweihan/HelloGitHub"},
                 {"full_name": "donnemartin/system-design-primer"}]
        trust = collect._trusted_lists(lists, pre)
        self.assertIn("521xueweihan/hellogithub", trust)
        self.assertNotIn("donnemartin/system-design-primer", trust,
                         "非显式来源仍要过覆盖度闸门")


class TestSourceSurvivesUpstreamGate(unittest.TestCase):
    """上游闸门回归（2026-10-31 实测翻车点）。

    保位写在**下游** `_pick_lists`，而覆盖度闸门在**上游** `build_pre`：
    HelloGitHub × 「派发」实测 cov=0.0 / matched=0 ⇒ 被上游剔掉，保位一句未生效，
    `list_sources` 仍是 4 个无关仓。以下三条钉住「先放行、后收敛」两段式。
    """

    def test_zero_overlap_explicit_source_passes_upstream_gate(self):
        self.assertTrue(
            collect._keep_in_pre("impl", 0.0, 0, False, 0, 3, True),
            "显式来源必须活到清单展开 —— 否则下游保位等于没写")

    def test_zero_overlap_plain_repo_still_filtered(self):
        self.assertFalse(
            collect._keep_in_pre("impl", 0.0, 0, False, 0, 3, False),
            "非来源仓的覆盖度闸门不得因此放宽（候选池会爆）")
        self.assertFalse(collect._keep_in_pre("impl", 0.5, 0, False, 0, 3, False))

    def test_source_converged_after_list_expansion(self):
        hello = {"full_name": "521xueweihan/HelloGitHub", "cov": 0.0, "hits": 0,
                 "sources": ["521xueweihan/HelloGitHub"],
                 "item": {"full_name": "521xueweihan/HelloGitHub",
                          "description": "分享 GitHub 上有趣、入门级的开源项目",
                          "topics": []}}
        self.assertFalse(
            collect._keep_after_lists(hello, ["event dispatch", "task dispatcher"]),
            "零重叠来源在展开后必须退出候选结果，否则 44+288 个仓会涌进结果页")
        relevant = dict(hello, cov=0.4)
        self.assertTrue(collect._keep_after_lists(relevant, ["event dispatch"]),
                        "与本查询相关的来源仓照常进结果")
        plain = {"full_name": "x/y", "cov": 0.0, "hits": 0, "sources": [],
                 "item": {"full_name": "x/y", "description": "", "topics": []}}
        self.assertTrue(collect._keep_after_lists(plain, ["event dispatch"]),
                        "非来源仓不受这条收敛影响")


    def test_probe_budget_shared_per_source(self):
        """探针预算按来源分配（2026-10-31 第二轮实测根因）。

        全局预算 12 会被 288 仓的大来源烧光，小来源（用户手点的那一个）一个都没探到
        ⇒ 仍然挑不到 HelloGitHub。这里复刻：大来源 20 个无 README 的仓，小来源 1 个真清单。
        """
        big = [_pre("own/r%d" % i, "impl", sources=["own"]) for i in range(20)]
        small = _pre("521xueweihan/HelloGitHub", "impl", sources=["hand"])
        calls = {"n": 0}
        orig = collect._readme_text

        def fake(full):
            calls["n"] += 1
            if full == "521xueweihan/HelloGitHub":
                return "\n".join("[x%d](https://github.com/a/b%d)" % (i, i) for i in range(10))
            return ""

        collect._readme_text = fake
        try:
            got = [x["full_name"] for x in collect._pick_lists(big + [small], 3)]
        finally:
            collect._readme_text = orig
        self.assertIn("521xueweihan/HelloGitHub", got,
                      "小来源必须拿到自己的探针额（全局预算会被大来源烧光）")
        self.assertLessEqual(calls["n"], collect._README_PROBE_CAP + 6,
                             "探针总量仍需有界，不能因分额而无限探")


class TestSignalGate(unittest.TestCase):
    def test_plain_own_repo_without_links_is_not_a_list(self):
        # gztxt/term-demo 这类普通仓：来源命中但它不是目录 ⇒ 不得被当清单展开，
        # 否则它会被「清单仓本身不计入候选」摘掉，候选池反而变小（本闸门第一版踩过）
        pre = [_pre("gztxt/term-demo", "impl", sources=["gztxt"],
                    readme="share a terminal over websocket with pty")]
        self.assertEqual(collect._pick_lists(pre, 4), [])


class TestSubpageBudget(unittest.TestCase):
    def test_subpages_is_twelve(self):
        self.assertEqual(base.LIST_EXPAND_SUBPAGES, 12)


class TestQueryCache(unittest.TestCase):
    def test_same_params_same_card_id(self):
        a = refscout.ScoutIn(module="派发", keywords=["event dispatch"], lang="py")
        b = refscout.ScoutIn(module="  派发 ", keywords=["EVENT DISPATCH"], lang="PY")
        self.assertEqual(refscout._query_fp(a), refscout._query_fp(b),
                         "同参数（大小写/空白无关）必须落到同一张卡")

    def test_different_params_different_card_id(self):
        a = refscout.ScoutIn(module="派发", keywords=["event dispatch"])
        b = refscout.ScoutIn(module="派发", keywords=["task queue"])
        self.assertNotEqual(refscout._query_fp(a), refscout._query_fp(b))

    def test_ttl_zero_disables_cache(self):
        old = refscout.REF_QUERY_TTL_H
        try:
            refscout.REF_QUERY_TTL_H = 0
            self.assertIsNone(refscout._cached_card("qwhatever"))
        finally:
            refscout.REF_QUERY_TTL_H = old

    def test_submit_checks_cache_before_inflight(self):
        seg = pathlib.Path(ROOT / "src" / "refscout.py").read_text(encoding="utf-8")
        seg = seg[seg.index("def submit("):]
        seg = seg[:seg.index("_INFLIGHT_LOCK")]
        self.assertIn("_cached_card", seg,
                      "缓存判定必须在抢 _INFLIGHT 之前，否则命中缓存也会占键")
