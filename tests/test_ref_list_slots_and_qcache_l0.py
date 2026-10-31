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
