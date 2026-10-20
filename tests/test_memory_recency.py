#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：记忆检索的「同源近分新者优先」（2026-10-21，用户授权 rec1）。

为什么这张闸门必须存在（红向都能确定性造）：
2026-10-21 实测查「openrouter auto 扣费」，TDAI 回两条：
  · 0.910「严厉禁止任何场景使用 openrouter/auto」    created 10:22
  · 0.909「非会话定时任务统一使用 openrouter/auto」   created 10:06
两条**语义同族**（都是「不许付费」），但后者把已被废止的字面 ID 当结论——
谁排第一原本只由 0.001 的语义分抖动决定。若不钉住：
  · 撤掉重排 ⇒ 旧结论回到第一（本条即红向）；
  · 把带宽放大到跨源/跨主题 ⇒ 强相关的旧条目被无关新条目挤掉（第三条用例守）；
  · 只修 `/api/memory/search` 不修注入通道 ⇒ 检索与开局注入给两个答案（第五条守）。

口径：零宿主依赖（不连 TDAI、不读宿主 DB，只 import 纯函数）。
"""
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import memory  # noqa: E402


def _item(source, rrf, created, mid=""):
    return {"source": source, "rrf": rrf, "id": mid or created, "created_at": created}


class TestRecencyTiebreak(unittest.TestCase):

    def test_near_tie_prefers_newer(self):
        """实测那一对：RRF 相对差 1.6% < 2% 带宽 ⇒ 新结论上前。"""
        old = _item("tdai_l1", 0.01639, "2026-10-04T10:06:23Z", "old")
        new = _item("tdai_l1", 0.01613, "2026-10-04T10:22:42Z", "new")
        out = memory._recency_tiebreak([old, new])
        self.assertEqual([x["id"] for x in out], ["new", "old"])

    def test_clear_gap_is_not_overridden(self):
        """分差超带宽 ⇒ 时效不得推翻相关性（旧条目仍可第一）。"""
        old = _item("tdai_l1", 0.02000, "2026-09-01T00:00:00Z", "old")
        new = _item("tdai_l1", 0.01000, "2026-10-20T00:00:00Z", "new")
        out = memory._recency_tiebreak([old, new])
        self.assertEqual([x["id"] for x in out], ["old", "new"])

    def test_cross_source_never_reordered(self):
        """跨源分值不可比（local 是 LIKE）⇒ 源边界必须隔断重排。"""
        a = _item("tdai_l1", 0.01639, "2026-10-01T00:00:00Z", "a")
        b = _item("local", 0.01630, "2026-10-20T00:00:00Z", "b")
        out = memory._recency_tiebreak([a, b])
        self.assertEqual([x["id"] for x in out], ["a", "b"])

    def test_missing_timestamp_is_treated_as_oldest(self):
        """无时间戳不参与「更新」方向置换，但段内新者有时间的仍靠前。"""
        bare = {"source": "tdai_l1", "rrf": 0.01640, "id": "bare"}
        new = _item("tdai_l1", 0.01635, "2026-10-20T00:00:00Z", "new")
        out = memory._recency_tiebreak([bare, new])
        self.assertEqual([x["id"] for x in out], ["new", "bare"])

    def test_rrf_ladder_is_not_a_tie(self):
        """RRF 阶梯：相邻名次相对差近似常数（实测≈1.6%），**不得**整条按时间重排。

        红向（2026-10-21 影子实例实踩）：带宽判定若拿「前一条」当基准，整条同源
        阶梯每一跳都在带内 ⇒ 段无限延伸，0.01639 的「禁令」被 0.01449 的无关条目顶掉。
        """
        rrfs = [0.01639, 0.01613, 0.01587, 0.01562, 0.01538, 0.01515, 0.01493, 0.01471, 0.01449]
        # 时间刻意逆序：越靠后越新 ⇒ 错误实现会把整段倒过来
        items = [_item("tdai_l1", r, "2026-10-04T10:%02d:00Z" % (i + 1), str(i))
                 for i, r in enumerate(rrfs)]
        out = memory._recency_tiebreak(items)
        ids = [x["id"] for x in out]
        # 只有「与段首相对差 ≤2%」的前两条可以互换（0.01639/0.01613 差 1.6%）；
        # 0.01587 起相对差 3.2% 已出带宽，必须留在原位——这是本条的重点。
        self.assertEqual(sorted(ids[:2]), ["0", "1"])
        self.assertEqual(ids[2:], [str(i) for i in range(2, len(rrfs))],
                         "超出带宽的条目被时效挤走了名次：带宽基准又退回成「前一条」了")

    def test_band_zero_disables(self):
        """band<=0 原样返回：保留 A/B 与排障开关。"""
        old = _item("tdai_l1", 0.01639, "2026-10-04T10:06:23Z", "old")
        new = _item("tdai_l1", 0.01613, "2026-10-04T10:22:42Z", "new")
        out = memory._recency_tiebreak([old, new], band=0)
        self.assertEqual([x["id"] for x in out], ["old", "new"])

    def test_both_channels_share_the_rule(self):
        """检索与注入两条通道必须都过这道重排，否则同一问题两个答案。"""
        src = (_REPO / "src" / "memory.py").read_text(encoding="utf-8")
        calls = re.findall(r"_recency_tiebreak\(", src)
        # 1 处定义 + 2 处调用（/api/memory/search 与 /api/memory/context）
        self.assertGreaterEqual(len(calls), 3, f"调用点不足：{len(calls)}")
        self.assertIn("_recency_tiebreak(fused)", src)


if __name__ == "__main__":
    unittest.main(verbosity=2)
