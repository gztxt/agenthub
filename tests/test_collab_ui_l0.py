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
