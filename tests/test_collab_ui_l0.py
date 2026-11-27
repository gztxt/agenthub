#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0：协同工作台的三个「看得见」约束（2026-11-27 T6 收尾）。

都是实测踩出来的：
1. 进页自动打开非终态 run —— 只查 `state=running` 会漏掉刚拆解还没开跑的 `pending`
   （实测 run 5e9a6c2a18：`running=[]` 但页面有活任务）。
2. 汇总页「成本 × 成功率」两块数据来自**两个不同端点**，前端只 join 不重算。
3. 新结构里不许出现 inline onclick（会旁路事件委托，前端硬判据 4.2）。
"""
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_JS = (_REPO / "static" / "hub" / "18-collab.js").read_text(encoding="utf-8")
_HTML = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")


def _collab_segment() -> str:
    i = _HTML.index('id="page-tasks"')
    j = _HTML.index('id="page-jobs"')
    return _HTML[i:j]


class TestAutoOpen(unittest.TestCase):
    def test_uses_non_terminal_filter_not_only_running(self):
        self.assertIn("cbAutoOpenRunning", _JS)
        self.assertIn("terminal = {", _JS)
        self.assertIn("r.state", _JS)
        # 反向：只按 state=running 取是已知缺陷，不许回退
        self.assertNotIn("runs?state=running&limit=1", _JS)

    def test_does_not_steal_an_open_run(self):
        self.assertIn("if (currentRun) return;", _JS)


class TestCostPerf(unittest.TestCase):
    def test_joins_two_endpoints(self):
        self.assertIn("/api/tasks/stats", _JS)
        self.assertIn("/api/tasks/costs?group_by=model", _JS)

    def test_renders_into_existing_container(self):
        self.assertIn('id="statsCostPerf"', _collab_segment())

    def test_stats_tab_loads_it(self):
        self.assertIn("cbLoadCostPerf()", _JS)


class TestDelegatedInteractions(unittest.TestCase):
    """T6 新加的三类交互必须走事件委托（data-*），不许 inline onclick。

    为什么只测这三类：运行详情那几个按钮（startRun/retryRun/dagZoom）是 2026-11-03
    就有的静态写法，本轮没动它们，一并收紧会逼着重写无关代码（闸门量错对象）。
    新加的东西必须自己守规矩。"""

    def test_tabs_and_chips_use_data_attributes(self):
        seg = _collab_segment()
        self.assertIn('data-ctab="live"', seg)
        self.assertIn('data-cpanel="live"', seg)
        # Agent 芯片由 JS 渲染，源码里应带 data-ag
        self.assertIn("data-ag=", _JS)
        # 这三类元素上不许挂 inline onclick
        for bad in ('<button class="cb-tab on" data-ctab="live" onclick=',
                    "data-ag=\"' + escapeHtml(id) + '\" onclick="):
            self.assertNotIn(bad, seg + _JS)

    def test_tab_switch_binds_once(self):
        self.assertIn("dataset.bound", _JS)


if __name__ == "__main__":
    unittest.main()


class TestHistoryVisible(unittest.TestCase):
    """2026-11-27 用户报「历史页面无历史任务记录」。

    实测根因：flex 链断 ⇒ `.cb-right` 没有确定高度 ⇒ `.cb-view` 算出 0
    ⇒ 20 条历史渲染了但被裁到看不见（桌面 histViewH=0、窄屏 top=928 在视口外）。
    判据：.cb-right 只靠 min-height:0 跟随高度（不能写 height:0，那会算出 0）。
    """

    def test_cb_right_has_min_height_not_fixed_height(self):
        css = _HTML[_HTML.index(".cb-right {"):_HTML.index(".cb-right {") + 600]
        # 只看**声明**，不看注释（注释里会写「加 height:0 反把…」这句教训）
        self.assertIn("min-height: 0", css)
        self.assertIsNone(re.search(r"^\s*height:\s*0(px)?\s*;", css, re.M),
                          "写死 height:0 会让 .cb-view 高度算出 0")

    def test_narrow_tab_scrolls_right_pane_into_view(self):
        self.assertIn("cbScrollToRight", _JS)
        self.assertIn("HUB_NARROW_MQ", _JS, "断点必须复用全站唯一 matchMedia")

    def test_narrow_left_panel_collapses(self):
        seg = _collab_segment()
        self.assertIn('id="cbLeftToggle"', seg)
        self.assertIn("cbBindLeftToggle", _JS)


class TestOutcomeBar(unittest.TestCase):
    """2026-11-27 用户令：执行情况摘要要显眼；失败项后面直接带「继续执行」。"""

    def test_four_chips_present(self):
        self.assertIn('id="cbOutcome"', _collab_segment())
        for label in ("成功", "失败", "运行中", "待执行"):
            self.assertIn(label, _JS)

    def test_failed_task_has_retry_button(self):
        self.assertIn("data-cb-retry", _JS)
        self.assertIn("/retry", _JS)
        # 单条重试必须带 task_id（整条重试是另一回事）
        self.assertIn("task_id: taskId", _JS)

    def test_history_row_has_retry(self):
        s04 = (_REPO / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
        self.assertIn('data-run-act="retry"', s04)
        self.assertIn("'retry'", s04)
