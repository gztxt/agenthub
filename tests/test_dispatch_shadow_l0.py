#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：派发影子留证（`tasks_tiers._shadow_dispatch_note`）——只多说话不改选型。

【这条判据治什么】
`dispatch_weights` 自述「权重直接接管选型需要先跑 A/B（否则『优化』与『回归』
不可分辨），这条写进 PENDING-TASKS」——但 A/B 的**数据通道**此前不存在。本文件
钉住 `_plan_tiers` → `_shadow_dispatch_note` 这条接线与四条不变量：

1. **留证不选型**：suggest 有建议 ⇒ `profile_events` 出现 `dispatch_shadow/would_pick`
   （detail 带 suggested/actual 对照面），items **逐字节不变**；
2. **无建议零事件**：suggest 返回 None ⇒ 不记（「没有够格的证据」不是一条建议）；
3. **异常零外溢**：suggest 抛错 ⇒ 静默跳过（增强层降级口径同 `_catalog_snapshot`）；
4. **接线钉死**：`_plan_tiers` 源码必须调 `_shadow_dispatch_note`，且在
   `tierlib.plan` **之后**（先有 actual 才有对照面）——防「接了但从不调用」的
   休眠态（判例：escalate_target 三处调用点都没传 catalog，档位升档运行时永不生效）。

分层口径：临时 db + monkeypatch `dispatch_weights.suggest`，不碰生产库、不连网、零 SKIP。
（借 TokenEconomizer 的 shadow 设计：决策照算、证据照记、行为零影响。）
"""
import json
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db                # noqa: E402
import dispatch_weights  # noqa: E402
import tasks             # noqa: E402
import tasks_runner      # noqa: E402
import tasks_tiers       # noqa: E402

_TIERS_SRC = (_REPO / "src" / "tasks_tiers.py").read_text(encoding="utf-8")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0dshdw-"))
        self._saved_conn = db._conn
        db.init_db(self.tmp / "t.db")
        # `dispatch_weights._by_family` 要读 tasks 表（含 runner 迁移列 model_sent
        # 等）⇒ 必须 ensure_schema + ensure_task_columns；不建齐时增强层会静默降级
        # （by design），但那样留证路径就没被测到。
        tasks.ensure_schema()
        tasks_runner.ensure_task_columns()
        self._saved_suggest = dispatch_weights.suggest
        self._seen = None
        self.addCleanup(self._restore)

    def _restore(self):
        dispatch_weights.suggest = self._saved_suggest
        db._conn = self._saved_conn

    def _events(self):
        return db.query("SELECT * FROM profile_events WHERE source='dispatch_shadow'")

    def _items(self):
        return [{"task_id": "t1", "id": "t1", "prompt": "改 src/a.py 修 bug",
                 "files": ["src/a.py"], "agent_id": "codex",
                 "model_planned": "m-x", "model": None}]

    def _suggest(self, out):
        def _s(pool, family, by_family=None):
            self._seen = (set(pool), family)
            return out
        return _s


class TestRecordsWithoutSelecting(_Base):
    def test_records_would_pick_and_items_unchanged(self):
        dispatch_weights.suggest = self._suggest(
            {"agent_id": "claude", "model": "m-best", "quality": 0.91, "n": 12})
        items = self._items()
        before = json.dumps(items, sort_keys=True, ensure_ascii=False)
        tasks_tiers._shadow_dispatch_note(items, {"claude", "codex"})
        ev = self._events()
        self.assertEqual(1, len(ev))
        self.assertEqual("would_pick", ev[0]["status"])
        d = json.loads(ev[0]["detail"])
        self.assertEqual("claude", d["suggested_agent"])
        self.assertEqual("m-best", d["suggested_model"])
        self.assertEqual("codex", d["actual_agent"])
        self.assertEqual("m-x", d["actual_model"])
        self.assertEqual("code", d["family"])
        self.assertEqual(before, json.dumps(items, sort_keys=True, ensure_ascii=False),
                         "影子不得改任何选型字段（A/B 的 actual 面必须原样）")

    def test_family_passthrough_uses_item_shape(self):
        """task_family 判据要吃 items 的形状（id 兜底 / files 最强信号 / prompt 动词）。"""
        dispatch_weights.suggest = self._suggest(None)
        items = [{"id": "t2", "prompt": "检索并核对相关资料"}]
        tasks_tiers._shadow_dispatch_note(items, set())
        self.assertEqual("research", self._seen[1])
        items2 = [{"id": "t3", "prompt": "检索", "files": ["a.py"]}]
        tasks_tiers._shadow_dispatch_note(items2, set())
        self.assertEqual("code", self._seen[1], "有文件集 ⇒ code 族（最强信号）")


class TestNoSuggestionNoEvent(_Base):
    def test_none_suggestion_records_nothing(self):
        dispatch_weights.suggest = self._suggest(None)
        tasks_tiers._shadow_dispatch_note(self._items(), {"claude"})
        self.assertEqual([], self._events(), "「没有够格的证据」不是一条建议")

    def test_empty_items_is_noop(self):
        tasks_tiers._shadow_dispatch_note([], {"claude"})
        self.assertEqual([], self._events())


class TestResilience(_Base):
    def test_suggest_crash_does_not_propagate(self):
        def boom(pool, family, by_family=None):
            raise RuntimeError("gateway down")
        dispatch_weights.suggest = boom
        tasks_tiers._shadow_dispatch_note(self._items(), {"claude"})  # 不得抛
        self.assertEqual([], self._events())


class TestWiringPinned(unittest.TestCase):
    def test_plan_tiers_calls_shadow_note_after_plan(self):
        i = _TIERS_SRC.index
        self.assertIn("_shadow_dispatch_note(items, pool)", _TIERS_SRC,
                      "_plan_tiers 没接影子留证 ⇒ A/B 数据通道不存在（休眠态）")
        self.assertLess(i("tierlib.plan"), i("_shadow_dispatch_note(items, pool)"),
                        "留证必须在 plan 之后：先有 actual 才有对照面")


if __name__ == "__main__":
    unittest.main()
