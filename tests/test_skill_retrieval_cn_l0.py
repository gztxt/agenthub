#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：技能检索的中文可用性（2026-10-22 批 A1/A2/A3 + C5）。

## 这组用例守的是什么（全部来自实测，不是设计愿望）

改动前实测（生产 `/api/skill/relevant`，8 中文 + 6 英文真实任务）：

| 语言 | rank1 正确 | `/auto` 接受 |
|---|---|---|
| 中文 | 1/8 | **1/8** |
| 英文 | 5/6 | 4/6 |

根因三条，本文件逐条钉住：

1. **A1 触发词**：中文查询打不中英文技能名（`matched_in` 恒空）。病态证据：
   `aidev-02-implement-audit` 在「调试一个偶发失败的测试」与「给这个新功能写测试」
   两条**不同**查询上得到完全相同的 4.7677 分 —— 只有「测试」一个词在贡献。
   ⇒ 触发词作为 BM25F 第三字段，且**自述优先于策展表**。
2. **A2 功能字二元组噪声**：`检查我的改动是否符合规范` 的命中词里出现 `动是`/`是否`/`我的`
   —— 垃圾二元组 IDF 很高（只在 1~2 篇里出现），把 `web-ui-cdp-verify` 抬到 `code-review` 前面。
3. **A3 接受判据**：`LOCAL_NORM_MIN=0.40` 是**语料相关绝对值**，英文宽松、中文封死。
   ⇒ 补一条相对判据（硬下限 + top1/top2 领先度 + 必须命中 name/trig）。

另加 C5：380 行只有 276 个不同技能名，副本在 top-N 里白占名额
（实测 top-3 出现 `test-driven-development` ×2）。折叠必须在**排序后**做。

⚠ 阈值（`LOCAL_NORM_FLOOR=0.18` / `LOCAL_GAP_MIN=1.8`）是**实测标定值**，不是拍脑袋：
调整本文件的夹具等于改判据，必须同时更新 `skill_store.py` 里的注释与实测记录。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

import tiers                    # noqa: E402
import skill_relevance as sr  # noqa: E402
import skill_store as ss      # noqa: E402
import skill_triggers as st   # noqa: E402


def _item(name, desc="", trig="", route="claude"):
    return {"name": name, "description": desc, "triggers": trig, "route": route}


class TestA2StopBigrams(unittest.TestCase):
    """功能字参与的二元组必须被丢掉（垃圾二元组 IDF 偏高，会把噪声抬到真命中之上）。"""

    def test_function_word_bigrams_dropped(self):
        toks = set(sr.tokenize("检查我的改动是否符合规范"))
        for junk in ("我的", "动是", "是否", "合的", "否符"):
            self.assertNotIn(junk, toks, "垃圾二元组仍参与打分：%r" % junk)

    def test_real_bigrams_survive(self):
        toks = set(sr.tokenize("检查我的改动是否符合规范"))
        for real in ("检查", "改动", "规范", "符合"):
            self.assertIn(real, toks, "真词被误伤：%r" % real)

    def test_single_cjk_char_still_emitted(self):
        """单字分支不受影响：`端` 这类一词查询靠它接住。"""
        self.assertEqual(sr.tokenize("端"), ["端"])

    def test_stop_filter_never_empties_a_run(self):
        """**整段被丢空时必须回落**（2026-10-22 评测集当场抓到的真 bug）。

        `摘要` 是 2 个汉字、其中 `要` 在停用集里 ⇒ 按字面规则这个二元组被丢，
        `tokenize("摘要") == []` ⇒ `/relevant` 回「查询切词后为空」，
        而 `摘要` 正是 `summarize` 技能的触发词 —— 一个**真实短查询从此永远搜不到**。
        规则本意是「长串里少几个二元组无妨」，不是「让短词消失」。
        """
        self.assertEqual(sr.tokenize("摘要"), ["摘要"])
        # 长串仍然降噪（有余量时才过滤）
        toks = set(sr.tokenize("检查我的改动是否符合规范"))
        self.assertNotIn("动是", toks)
        self.assertIn("检查", toks)

    def test_short_query_is_searchable_end_to_end(self):
        """端到端：`摘要` 必须真的能命中 `summarize`（带触发词）。"""
        items = [_item("summarize", desc="Summarize long text", trig="摘要 总结 压缩")]
        res = sr.rank("摘要", items, n=1)
        self.assertEqual([x["item"]["name"] for x in res["items"]], ["summarize"])


class TestA1Triggers(unittest.TestCase):
    def test_triggers_are_a_scored_field(self):
        """触发词命中必须**比描述命中更值钱**（W_TRIG=2.2 > W_DESC=1.0）。"""
        items = [
            _item("alpha", desc="调试 排查 复现", trig="", route="a"),      # 只有描述
            _item("beta", desc="无关", trig="调试 排查 复现", route="b"),   # 只有触发词
        ]
        res = sr.rank("调试一个偶发失败的测试", items, n=2)
        self.assertEqual(res["items"][0]["item"]["name"], "beta",
                         "触发词命中没有压过描述命中 —— W_TRIG 没生效")

    def test_name_still_outranks_triggers(self):
        """名字仍是最高权重：触发词不能反压作者给的唯一标识。"""
        items = [
            _item("debugger", desc="", trig="", route="a"),
            _item("other", desc="", trig="debugger", route="b"),
        ]
        res = sr.rank("debugger", items, n=2)
        self.assertEqual(res["items"][0]["item"]["name"], "debugger")

    def test_trig_hit_flag_reported(self):
        items = [_item("beta", desc="无关", trig="调试", route="b")]
        res = sr.rank("调试", items, n=1)
        self.assertTrue(res["items"][0]["trig_hit"], "trig_hit 未回传，接受判据拿不到依据")

    def test_own_triggers_win_over_lexicon(self):
        """技能自述优先：作者写了 triggers 就不再用策展表（否则作者改了就白改）。"""
        self.assertEqual(st.merge("systematic-debugging", "自定义 词表"), "自定义 词表")

    def test_lexicon_used_when_own_missing(self):
        self.assertIn("调试", st.merge("systematic-debugging", ""))

    def test_unknown_name_returns_empty_not_name(self):
        """未知技能回空串而不是名字本身 —— 回名字等于把 name 权重偷偷加成两倍。"""
        self.assertEqual(st.lookup("__no_such_skill__"), "")


class TestA2CoverageFactor(unittest.TestCase):
    """多词命中要压过「单泛词重复命中」。"""

    def test_multi_term_beats_single_term(self):
        items = [
            _item("single", desc="测试", route="a"),
            _item("multi", desc="测试 调试 复现 排查", route="b"),
        ]
        res = sr.rank("测试 调试 复现 排查", items, n=2)
        self.assertEqual(res["items"][0]["item"]["name"], "multi",
                         "覆盖度系数没生效：单泛词仍压过多词命中")
        self.assertGreater(res["items"][0]["coverage"], res["items"][1]["coverage"])

    def test_single_token_query_coverage_is_uniform(self):
        """单词查询下所有命中者的 cov 必须同为 1.0 —— 乘同一个常数，**次序才不会被改变**。

        首版这条写成「`端` 查询里 `a(desc=端)` 排在 `b(desc=端口 端点)` 前面」，
        但 `端口`/`端点` 切出来是二元组、**不含 `端` 这个单字** ⇒ b 压根不命中，
        测的是「召回」而不是「次序」。改成直接断言乘数一致（这才是次序不变的充分条件）。
        """
        items = [_item("a", trig="端", route="a"), _item("b", trig="端 端", route="b")]
        res = sr.rank("端", items, n=2)
        self.assertEqual(len(res["items"]), 2)
        self.assertEqual({x["coverage"] for x in res["items"]}, {1.0})
        # 同乘 2 之后仍按 BM25 原序：tf 高的 b 在前
        self.assertEqual(res["items"][0]["item"]["name"], "b")


class TestA3AcceptCriterion(unittest.TestCase):
    """接受判据：绝对分 OR（硬下限 + 领先度 + name/trig 命中）。"""

    def _res(self, top, second, norm, matched_in=None, trig_hit=False, second_score=2.0):
        return {"norm": norm, "query_tokens": ["a", "b", "c"],
                "items": [
                    {"item": {"name": top}, "score": 10.0, "matched": ["a", "b"],
                     "matched_in": matched_in or [], "trig_hit": trig_hit},
                    {"item": {"name": second}, "score": float(second_score),
                     "matched": ["a"], "matched_in": [], "trig_hit": False},
                ]}

    def test_absolute_path_unchanged(self):
        """英文场景（norm 高）行为不变：仍按原绝对门槛接受。"""
        res = self._res("systematic-debugging", "x", 0.9, matched_in=["debug"])
        _top, _cov, accepted = ss._local_verdict(res)
        self.assertTrue(accepted)

    def test_relative_path_accepts_cn_when_leading_and_field_hit(self):
        res = {"norm": 0.22, "query_tokens": ["调试", "测试"],
               "items": [{"item": {"name": "systematic-debugging"}, "score": 9.0,
                          "matched": ["调试"], "matched_in": [], "trig_hit": True},
                         {"item": {"name": "other"}, "score": 3.0, "matched": ["测试"],
                          "matched_in": [], "trig_hit": False}]}
        _top, _cov, accepted = ss._local_verdict(res)
        self.assertTrue(accepted, "中文相对路径没生效 —— 中文 /auto 仍被封死")

    def test_relative_path_rejects_when_not_leading(self):
        """一堆技能并列沾边时不许接受（这正是旧 `norm≥0.4` 想防的事）。"""
        res = {"norm": 0.30, "query_tokens": ["a", "b"],
               "items": [{"item": {"name": "x"}, "score": 10.0, "matched": ["a"],
                          "matched_in": [], "trig_hit": True},
                         {"item": {"name": "y"}, "score": 9.0, "matched": ["b"],
                          "matched_in": [], "trig_hit": False}]}
        _top, _cov, accepted = ss._local_verdict(res)
        self.assertFalse(accepted)

    def test_relative_path_requires_name_or_trig_field(self):
        """只靠一段长描述里的泛词命中 → 不接受（否则泛词又把噪声放进来）。"""
        res = {"norm": 0.30, "query_tokens": ["a", "b"],
               "items": [{"item": {"name": "x"}, "score": 10.0, "matched": ["a"],
                          "matched_in": [], "trig_hit": False},
                         {"item": {"name": "y"}, "score": 2.0, "matched": [],
                          "matched_in": [], "trig_hit": False}]}
        _top, _cov, accepted = ss._local_verdict(res)
        self.assertFalse(accepted, "描述泛词命中被接受了 —— A3 的第三条条件没生效")

    def test_below_floor_rejects(self):
        res = {"norm": 0.05, "query_tokens": ["a"],
               "items": [{"item": {"name": "x"}, "score": 10.0, "matched": ["a"],
                          "matched_in": ["a"], "trig_hit": True}]}
        _top, _cov, accepted = ss._local_verdict(res)
        self.assertFalse(accepted)

    def test_gap_reported_for_explainability(self):
        """不接受时要能回答「为什么」：gap 必须回传。"""
        res = {"norm": 0.09, "query_tokens": ["a"],
               "items": [{"item": {"name": "x"}, "score": 4.0, "matched": ["a"],
                          "matched_in": [], "trig_hit": False},
                         {"item": {"name": "y"}, "score": 3.9, "matched": [], "matched_in": [],
                          "trig_hit": False}]}
        ss._local_verdict(res)
        self.assertIn("gap", res)
        self.assertLess(res["gap"], ss.LOCAL_GAP_MIN)


class TestC5CollapseByName(unittest.TestCase):
    """同名多副本必须在排序后折叠，且**如实报出还有几份**。"""

    def test_collapse_keeps_highest_and_counts_rest(self):
        rows = [
            {"item": {"name": "test-driven-development", "route": "claude"}, "score": 9.0},
            {"item": {"name": "test-driven-development", "route": "codex"}, "score": 9.0},
            {"item": {"name": "other", "route": "pi"}, "score": 5.0},
        ]
        out = sr.collapse_by_name(rows)
        self.assertEqual([x["item"]["name"] for x in out], ["test-driven-development", "other"])
        self.assertEqual(out[0]["same_name_count"], 2, "副本数没上报 = 静默丢数据")
        self.assertEqual(out[0]["same_name_routes"], ["codex"])

    def test_collapse_keeps_first_item_identity(self):
        rows = [{"item": {"name": "A", "route": "claude"}, "score": 9.0},
                {"item": {"name": "a", "route": "codex"}, "score": 1.0}]
        out = sr.collapse_by_name(rows)
        self.assertEqual(out[0]["item"]["route"], "claude", "大小写不同的同名没被折叠")

    def test_collapse_empty_is_safe(self):
        self.assertEqual(sr.collapse_by_name([]), [])
        self.assertEqual(sr.collapse_by_name(None), [])


class TestDerivedTriggers(unittest.TestCase):
    """派生词表（PT-20261022-14）：覆盖 106→270/276。

    holdout 实测暴露：`证书`/`邮件` 这类 2 字查询对未覆盖技能**零召回**。
    派生把英文名按词元查 `_EN_ZH`，兜住手写策展表够不到的 164 个技能。
    """

    def test_common_tokens_derive(self):
        cases = {
            "certbot-ssl": "证书",
            "apple-notes": "笔记",
            "IMAP 邮件分类器": "邮件",
            "himalaya": "邮件",
            "xlsx": "excel 表格",
            "linear": "工单",
            "airtable": "表格 数据库",
            "1password": "密码 保险库",
        }
        for name, want in cases.items():
            self.assertIn(want.split()[0], st.derive(name),
                          "%s 派生失败（want⊇%r）：%r" % (name, want, st.derive(name)))

    def test_derive_dedups_and_caps(self):
        d = st.derive("video-cut-video-frames-video")
        self.assertLessEqual(len(d.split()), st._DERIVE_MAX, "派生词数没有上限")
        self.assertEqual(len(d.split()), len(set(d.split())), "派生重复词未去重")

    def test_derive_drops_unknown_tokens(self):
        """查不到的英文词元**丢弃**，不硬造译文（宁可少一个词）。"""
        self.assertEqual(st.derive("zzzqqq-wwwvvv"), "")

    def test_priority_own_gt_curated_gt_derived(self):
        # 自述 > 策展 > 派生
        self.assertEqual(st.source_of("systematic-debugging", "自述词"), "own")
        self.assertEqual(st.source_of("systematic-debugging"), "curated")
        self.assertEqual(st.source_of("certbot-ssl"), "derived")
        self.assertEqual(st.source_of("__nope__"), "none")
        self.assertEqual(st.merge("certbot-ssl", "自述"), "自述")

    def test_curated_is_not_shadowed_by_derived(self):
        """策展词不得被派生词覆盖（wechat-mp-cn 派生只有「微信」，策展是公众号/阅读量/舆情）。"""
        self.assertEqual(st.source_of("wechat-mp-cn"), "curated")
        self.assertIn("公众号", st.merge("wechat-mp-cn"))


class TestEvalCaseFile(unittest.TestCase):
    """评测集自身也要有闸门（2026-10-22 第 3 项）。

    标签烂掉不会报错：目标名写错只表现为「怎么都搜不到」，与「系统差」长得一样。
    本仓已因评测集出过三次错（v1 照抄文件名→虚高 0.908；v2 标签指错文档；
    v5 十一条标签不可检），所以**评测集的完整性必须机器可判**。
    """

    PATH = _REPO / "scripts" / "skill-eval-cases.txt"

    def _cases(self):
        rows = []
        for ln, line in enumerate(self.PATH.read_text(encoding="utf-8").splitlines(), 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            self.assertGreaterEqual(len(parts), 3, "第 %d 行格式不对" % ln)
            rows.append({"q": parts[0].strip(), "cat": parts[1].strip().upper(),
                         "target": parts[2].strip(),
                         "sib": [x.strip() for x in (parts[3] if len(parts) > 3 else "").split(",") if x.strip()]})
        return rows

    def test_at_least_30_cases(self):
        """少于 30 条不足以给合格率（14 条时只能证明「存在结构性缺陷」）。"""
        self.assertGreaterEqual(len(self._cases()), 30)

    def test_all_categories_present(self):
        cats = {c["cat"] for c in self._cases()}
        self.assertEqual(cats, {"D", "P", "S", "X"}, "类别缺失或拼错：%r" % cats)

    def test_no_duplicate_queries(self):
        qs = [c["q"] for c in self._cases()]
        self.assertEqual(len(qs), len(set(qs)), "评测集里有重复查询")

    def test_every_case_has_target(self):
        for c in self._cases():
            self.assertTrue(c["target"], "空目标：%r" % c)
            self.assertNotIn(c["target"], c["sib"], "目标同时被列进兄弟：%r" % c)

    def test_queries_do_not_copy_target_name(self):
        """查询不得照抄技能名（那就是在测子串匹配，v1 虚高 0.908 的根因）。"""
        for c in self._cases():
            t = c["target"].lower()
            if len(t) >= 4 and not t.isascii():          # 中文技能名才可比
                self.assertNotEqual(c["q"].lower(), t, "查询照抄了技能名：%r" % c)


@tiers.host_only
class TestLexiconLiveness(unittest.TestCase):
    """策展表里**不许有死条目**：名字写错不会报错，只安静失效（与分词器错误同族）。

    这条读真盘语料，故归 L1（`@host_only`）；L0 下不跑，避免干净 runner 假红。
    """

    @staticmethod
    def _names():
        """真实语料里的技能名集合。

        ⚠ 取**目录名**而不是 `it["name"]`：条目名已经过 `tdai_client.scrub`，实测
        `content-risk-detector` 会被脱敏成 `content-ri<redacted>` ⇒ 拿它比对会报假死条目
        （首版就是这么红的）。目录名是磁盘真相，不经过脱敏。
        """
        import os
        import skill
        names = set()
        for r in skill.disk_routes():
            scan = skill._scan_one(r, skill.route_roots(r))
            for it in scan.get("items") or []:
                rp = it.get("realpath") or it.get("path")
                if rp:
                    names.add(os.path.basename(os.path.dirname(rp)))
                if it.get("name"):
                    names.add(it["name"])
        return names

    def test_no_dead_lexicon_entries(self):
        names = self._names()
        self.assertTrue(names, "未扫到任何技能：语料不可读，本用例判不了（不是通过）")
        dead = [n for n in st.known_names() if n not in names]
        self.assertEqual(dead, [], "策展表里有不存在的技能名：%r" % dead)

    def test_eval_case_targets_exist_in_corpus(self):
        """评测集的 32 个目标必须真存在于语料，否则是死标签（与死触发器同族）。"""
        names = self._names()
        self.assertTrue(names, "未扫到任何技能：判不了（不是通过）")
        cases = TestEvalCaseFile()._cases()
        missing = sorted({c["target"] for c in cases if c["target"] not in names})
        self.assertEqual(missing, [], "评测集里有不存在的技能：%r" % missing)

    def test_lexicon_covers_the_eight_eval_tasks(self):
        """评测集里的 8 个目标技能必须有触发词，否则中文查询仍打不中。"""
        for name in ("systematic-debugging", "test-driven-development",
                     "dispatching-parallel-agents", "code-review", "grill-me",
                     "writing-skills", "techdocs-search", "diagnosing-bugs"):
            self.assertTrue(st.lookup(name), "%s 没有触发词" % name)


if __name__ == "__main__":
    unittest.main()
