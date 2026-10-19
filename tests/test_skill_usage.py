#!/usr/bin/env python3
"""L0 hermetic 测试：D3 技能调用记账与零调用僵尸榜（`src/skill_usage.py`）。

分层口径（见 `tests/README.md`）：
- 本文件属 **L0**：不读生产 `data/agents.db`、不读任何真实技能目录、不联网、**不允许 SKIP**。
- `counts()` 靠往 `sys.modules["db"]` 塞一个假模块来驱动，因此**不碰真盘**；
  这样还能顺带断言 SQL 的 source/subject/时间窗参数形状（真实 API 是 `db.query`，
  计划书里写的 `db.fetchall` 并不存在——照抄会直接 AttributeError）。

为什么这层值得单独存在：D3 的全部价值在于**不虚报**。设计书 §7 的口径是
「两源皆零 → confidence=high；仅 hub 源为零 → medium」，而本批第二源没接。
所以真正的闸门不是「能不能算出僵尸」，而是**「没取证时敢不敢承认」**——
一旦某次有人顺手把 confidence 写成 high，这个模块就开始骗人了。
"""
import pathlib
import sys
import types
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import skill_usage as U  # noqa: E402


class _FakeDB(types.ModuleType):
    """假 db 模块：记录收到的 (sql, params)，返回预置行。

    **必须按 SQL 的 `subject IN (...)` 过滤**（2026-10-10 起）：原先纯返回预置行，
    于是「relevant 到底在不在 `_SUBJECTS` 里」对结果是**恒绿**的——测试根本证明不了修复
   （判例 102 §5：恒绿判据不算判据）。真实 API 按 subject 过滤，假库照做才不产生假绿。
    """

    def __init__(self, rows=None, boom=False):
        super().__init__("db")
        self.rows = rows or []
        self.boom = boom
        self.calls = []

    def query(self, sql, params=()):
        self.calls.append((sql, params))
        if self.boom:
            raise RuntimeError("no such database")
        if "subject IN" in sql and params:
            subs = set(params[1:-1])            # (source, *subjects, since)
            return [r for r in self.rows if r.get("subject") in subs]
        return self.rows


class _PatchDB:
    def __init__(self, fake):
        self.fake = fake
        self.old = None

    def __enter__(self):
        self.old = sys.modules.get("db")
        sys.modules["db"] = self.fake
        return self.fake

    def __exit__(self, *a):
        if self.old is None:
            sys.modules.pop("db", None)
        else:
            sys.modules["db"] = self.old


class TestZombies(unittest.TestCase):
    """零调用判据 + 置信度封顶（禁 SKIP 当 PASS 的核心闸门）。"""

    def test_zero_read_is_zombie_with_medium_confidence(self):
        items = [{"name": "a", "routes": ["claude"]}, {"name": "b", "routes": ["pi"]}]
        cmap = {"b": {"reads": 2, "injects": 1, "last_at": "2026-10-03T00:00:00+00:00", "via": ["hub"]}}
        z = U.zombies(items, cmap, days=7)
        self.assertEqual([x["name"] for x in z], ["a"])
        self.assertEqual(z[0]["confidence"], "medium")
        self.assertEqual(z[0]["days_idle"], 7)

    def test_never_report_high_when_direct_not_implemented(self):
        z = U.zombies([{"name": "a", "routes": []}], {}, days=7)
        self.assertNotEqual(z[0]["confidence"], "high")

    def test_every_zombie_row_declares_direct_source(self):
        z = U.zombies([{"name": "a", "routes": ["claude"]}], {}, days=7)
        self.assertEqual(z[0]["direct_source"], U.DIRECT_SOURCE)
        self.assertEqual(U.DIRECT_SOURCE, "not-implemented")

    def test_injects_alone_also_disqualifies_from_zombie(self):
        """只注入过也算被用上——注入是给人/agent 看一眼，正是注入面在起作用。"""
        cmap = {"a": {"reads": 0, "injects": 1, "last_at": "", "via": ["hub"]}}
        self.assertEqual(U.zombies([{"name": "a", "routes": ["claude"]}], cmap), [])

    def test_suggested_action_present(self):
        z = U.zombies([{"name": "a", "routes": []}], {}, days=7)
        self.assertIn(z[0]["suggested_action"], ("add_triggers", "widen_visibility", "retire_review"))

    def test_single_route_prefers_widen_visibility_over_retire(self):
        """只在一路可见 ⇒ 它可能压根没机会被选中，不能先判它该退。"""
        z = U.zombies([{"name": "a", "routes": ["claude"], "description": "有描述"}], {}, days=7)
        self.assertEqual(z[0]["suggested_action"], "widen_visibility")

    def test_multi_route_without_description_prefers_add_triggers(self):
        z = U.zombies([{"name": "a", "routes": ["claude", "pi"], "description": "  "}], {}, days=7)
        self.assertEqual(z[0]["suggested_action"], "add_triggers")

    def test_multi_route_with_description_lands_on_retire_review(self):
        z = U.zombies([{"name": "a", "routes": ["claude", "pi"], "description": "有用的东西"}], {}, days=7)
        self.assertEqual(z[0]["suggested_action"], "retire_review")

    def test_sorted_case_insensitively_by_name(self):
        items = [{"name": "b"}, {"name": "A"}, {"name": "c"}]
        self.assertEqual([x["name"] for x in U.zombies(items, {})], ["A", "b", "c"])

    def test_dedupes_repeated_names(self):
        """同一个技能若因多路重复出现，只能算一条——否则僵尸榜条数会被路由数放大。"""
        items = [{"name": "dup", "routes": ["claude"]}, {"name": "dup", "routes": ["pi"]}]
        self.assertEqual(len(U.zombies(items, {})), 1)

    def test_skips_unnamed_entries(self):
        self.assertEqual(U.zombies([{"name": ""}, {"name": "   "}, {}], {}), [])

    def test_empty_inputs_are_not_an_error(self):
        self.assertEqual(U.zombies([], {}), [])
        self.assertEqual(U.zombies(None, {}), [])


class TestDaysClamp(unittest.TestCase):
    def test_zero_and_negative_and_huge_all_clamped(self):
        for bad in (0, -1, 99999, None, "x"):
            self.assertEqual(U._clamp(bad), 1 if bad in (0, -1, None, "x") else U.MAX_DAYS, bad)

    def test_ordinary_value_untouched(self):
        self.assertEqual(U._clamp(7), 7)
        self.assertEqual(U._clamp(30), 30)

    def test_zombie_days_idle_reflects_clamped_window(self):
        z = U.zombies([{"name": "a", "routes": ["claude"]}], {}, days=0)
        self.assertEqual(z[0]["days_idle"], 1)


class TestNameExtraction(unittest.TestCase):
    def test_parses_json_detail(self):
        self.assertEqual(U._name_of('{"name": "agent-dispatch"}'), "agent-dispatch")

    def test_survives_bad_json(self):
        """一条脏数据不许带倒整张榜。"""
        self.assertEqual(U._name_of("{not json"), "")
        self.assertEqual(U._name_of(None), "")
        self.assertEqual(U._name_of("[1,2,3]"), "")

    def test_accepts_dict_detail_directly(self):
        self.assertEqual(U._name_of({"name": "x"}), "x")

    def test_strips_whitespace(self):
        self.assertEqual(U._name_of('{"name": "  x  "}'), "x")


class TestCounts(unittest.TestCase):
    def test_counts_never_raises_without_db(self):
        with _PatchDB(_FakeDB(boom=True)):
            self.assertIsInstance(U.counts(days=7), dict)
        with _PatchDB(_FakeDB(boom=True)):
            self.assertEqual(U.counts(days=7), {})

    def test_returns_empty_when_db_module_absent(self):
        old = sys.modules.pop("db", None)
        try:
            self.assertEqual(U.counts(), {})
        finally:
            if old is not None:
                sys.modules["db"] = old

    def test_query_params_pin_source_and_subjects(self):
        """source='rest' + 全部 subject + 时间窗——形状错了就记不上账或记错账。"""
        fake = _FakeDB([])
        with _PatchDB(fake):
            U.counts(days=7)
        sql, params = fake.calls[0]
        self.assertIn("source = ?", sql)
        self.assertEqual(params[0], "rest")
        # subject 集合钉死（含 skill.relevant：注入链唯一可见事件，见模块 docstring；
        # 含 skill.invoke：v0.13.127 统一调用入口的显式激活，比 read 更强的使用证据）。
        self.assertEqual(U._SUBJECTS,
                         ("skill.read", "skill.inject", "skill.relevant", "skill.invoke"))
        self.assertEqual(params[1:1 + len(U._SUBJECTS)], U._SUBJECTS)
        # SQL 里的 ? 个数必须与「source + N 个 subject + 时间窗」一致——
        # 写死 `(?, ?)` 会在加 subject 时错位，是历史 bug 的形态。
        self.assertEqual(sql.count("?"), 1 + len(U._SUBJECTS) + 1)
        self.assertTrue(params[-1].endswith("+00:00"), params[-1])

    def test_relevant_row_counts_as_inject(self):
        """注入链（D4/D5）只打 /api/skill/relevant ⇒ 它必须被计入，否则 counted 恒 0。

        这是 2026-10-10 补的缺口：`_pick_names` 认得 relevant 形状（带 name），
        但 `_SUBJECTS` 原先只认 read/inject ⇒ 事件写了、名字也有，却不被计数。
        """
        rows = [{"subject": "skill.relevant", "detail": '{"name": "agent-dispatch"}',
                 "created_at": "2026-10-10T01:00:00.000000+00:00"}]
        with _PatchDB(_FakeDB(rows)):
            c = U.counts(days=7)
        self.assertEqual(c["agent-dispatch"]["injects"], 1)
        self.assertEqual(c["agent-dispatch"]["reads"], 0)
        # 该技能不再是僵尸（正是台账 PT-20261009-04 的验收判据）
        self.assertEqual(U.zombies([{"name": "agent-dispatch", "routes": ["pi"]}], c), [])

    def test_invoke_row_counts_as_invoke_bucket(self):
        """v0.13.127 PT-20261019-01：skill.invoke 单列 invokes 桶（不并入 reads/injects）。

        两条判据：① 桶分开——面板要能分「被看过」与「被调用」；
        ② 有 invoke ⇒ 不再是僵尸——天天被点名调用的技能上零调用榜与榜单目的正相反。
        """
        rows = [{"subject": "skill.invoke",
                 "detail": '{"name": "crawl4ai", "caller": "codex"}',
                 "created_at": "2026-10-19T01:00:00.000000+00:00"}]
        with _PatchDB(_FakeDB(rows)):
            c = U.counts(days=7)
        self.assertEqual(c["crawl4ai"]["invokes"], 1)
        self.assertEqual(c["crawl4ai"]["reads"], 0)
        self.assertEqual(c["crawl4ai"]["injects"], 0)
        self.assertEqual(U.zombies([{"name": "crawl4ai", "routes": ["techdocs"]}], c), [])

    def test_tallies_reads_and_injects_separately(self):
        rows = [
            {"subject": "skill.read", "detail": '{"name": "a"}', "created_at": "2026-10-03T01:00:00.000000+00:00"},
            {"subject": "skill.inject", "detail": '{"name": "a"}', "created_at": "2026-10-03T02:00:00.000000+00:00"},
            {"subject": "skill.read", "detail": '{"name": "a"}', "created_at": "2026-10-03T03:00:00.000000+00:00"},
            {"subject": "skill.read", "detail": '{"name": "b"}', "created_at": "2026-10-03T02:30:00.000000+00:00"},
        ]
        with _PatchDB(_FakeDB(rows)):
            c = U.counts(days=7)
        self.assertEqual(c["a"]["reads"], 2)
        self.assertEqual(c["a"]["injects"], 1)
        self.assertEqual(c["a"]["last_at"], "2026-10-03T03:00:00.000000+00:00")
        self.assertEqual(c["a"]["via"], ["hub"])
        self.assertEqual(c["b"]["reads"], 1)

    def test_rows_without_name_are_dropped_not_counted(self):
        rows = [{"subject": "skill.read", "detail": "{}", "created_at": "2026-10-03T01:00:00.000000+00:00"},
                {"subject": "skill.read", "detail": "broken", "created_at": "2026-10-03T01:00:00.000000+00:00"}]
        with _PatchDB(_FakeDB(rows)):
            self.assertEqual(U.counts(), {})

    def test_via_is_listed_once_per_skill(self):
        rows = [{"subject": "skill.read", "detail": '{"name": "a"}', "created_at": "2026-10-03T01:00:00+00:00"} for _ in range(3)]
        with _PatchDB(_FakeDB(rows)):
            self.assertEqual(U.counts()["a"]["via"], ["hub"])


class TestSnapshot(unittest.TestCase):
    def test_shape_and_toplevel_confidence(self):
        items = [{"name": "a", "routes": ["claude"]}, {"name": "b", "routes": ["pi"]}]
        rows = [{"subject": "skill.read", "detail": '{"name": "b"}', "created_at": "2026-10-03T01:00:00+00:00"}]
        with _PatchDB(_FakeDB(rows)):
            s = U.snapshot(items, days=7)
        self.assertEqual(s["total"], 2)
        self.assertEqual(s["zombies_count"], 1)
        self.assertEqual(s["zombies"][0]["name"], "a")
        self.assertEqual(s["confidence"], "medium")
        self.assertEqual(s["direct_source"], "not-implemented")
        self.assertEqual(s["counted"], 1)     # 账里只有 b ⇒ 前端能区分「没人用」与「没记账」

    def test_counted_zero_when_db_unreachable_marks_everything(self):
        """账读不到 ⇒ counted=0 且全部上榜。这是**保守**方向：多提醒，不漏提醒。"""
        items = [{"name": "a"}, {"name": "b"}]
        with _PatchDB(_FakeDB(boom=True)):
            s = U.snapshot(items, days=7)
        self.assertEqual(s["counted"], 0)
        self.assertEqual(s["zombies_count"], 2)
        self.assertEqual(s["total"], 2)

    def test_generated_at_is_utc_iso(self):
        with _PatchDB(_FakeDB([])):
            s = U.snapshot([], days=3)
        self.assertTrue(s["generated_at"].endswith("+00:00"), s["generated_at"])
        self.assertEqual(s["days"], 3)

    def test_note_states_the_ceiling(self):
        with _PatchDB(_FakeDB([])):
            s = U.snapshot([])
        self.assertIn("medium", s["note"])
        self.assertIn("未取证", s["note"])


class TestRedDirection(unittest.TestCase):
    """红向自证：把修复撤掉，对应断言必须转红（恒绿判据不算判据，判例 102 §5）。"""

    def test_red_when_relevant_removed_from_subjects(self):
        """把 `_SUBJECTS` 退回旧的两元素 ⇒ 同一条 relevant 行不再被计入。

        假库按 `subject IN (...)` 过滤（见 `_FakeDB`），所以这条是**真红向**：
        撤掉 `skill.relevant` 后 `counts()` 变空，还原本 bug 现场（生产 counted=0 的机理）。
        """
        rows = [{"subject": "skill.relevant", "detail": '{"name": "x"}',
                 "created_at": "2026-10-10T01:00:00.000000+00:00"}]
        old = U._SUBJECTS
        try:
            U._SUBJECTS = ("skill.read", "skill.inject")   # 2026-10-10 之前的旧值
            with _PatchDB(_FakeDB(rows)):
                self.assertEqual(U.counts(days=7), {},
                                 "旧 _SUBJECTS 下 relevant 行必须被过滤掉（红向成立）")
        finally:
            U._SUBJECTS = old

    def test_relevant_row_would_be_false_green_without_filter(self):
        """守假库本身：若假库不按 subject 过滤，relevant 用例会恒绿。

        这条钉住「假库确实在过滤」——否则 `test_relevant_row_counts_as_inject`
        无论 `_SUBJECTS` 含不含 relevant 都会通过，修复就没人验。
        """
        rows = [{"subject": "skill.relevant", "detail": '{"name": "x"}',
                 "created_at": "2026-10-10T01:00:00.000000+00:00"}]
        fake = _FakeDB(rows)
        # 传一个**不含** relevant 的 subject 集合：假库必须把该行滤掉
        got = fake.query("SELECT ... WHERE source = ? AND subject IN (?, ?) AND created_at >= ?",
                         ("rest", "skill.read", "skill.inject", "2026-10-10T00:00:00+00:00"))
        self.assertEqual(got, [], "假库没有按 subject 过滤 ⇒ 会掩盖 _SUBJECTS 的缺失")
        got2 = fake.query("SELECT ... WHERE source = ? AND subject IN (?, ?, ?) AND created_at >= ?",
                          ("rest", "skill.read", "skill.inject", "skill.relevant",
                           "2026-10-10T00:00:00+00:00"))
        self.assertEqual(len(got2), 1, "含 relevant 时必须保留该行")


if __name__ == "__main__":
    unittest.main()
