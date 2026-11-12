"""L0 · 前端预算闸门（T7，2026-11-12）。

为什么要有：P0→P3 的目标是「首屏减重」，但方向很容易被后续提交悄悄抵消 ——
多一个 `<script src>`、内联一段 CSS、加一个 `setInterval`、直接写 `onclick=`，
每一条都只增几百字节或几毫秒，单看都不刺眼，合起来就把首屏又推回去。

判据分两类，上限都钉在 2026-11-12 实测值：
  · **字节类**留 5% 余量（压缩产物/模板的正常波动不该误报）；
  · **计数类钉死**（script 数 / onclick / setInterval / 分片数）——计数增长才是
    结构性劣化，必须走显式评审：改上限要在同一提交里写清理由并同步改本文件。

反向判据：调高任何上限都必须同时解释「为什么这次的增量是必要的」，
否则本闸门就退化成「记录当前值」的空转（本仓判例：清单类断言最容易这样烂掉）。
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
TPL = REPO / "templates" / "index.html"
BUNDLE = REPO / "static" / "hub.js"
SHARDS = sorted((REPO / "static" / "hub").glob("[0-9][0-9]-*.js"))

# 2026-11-12 实测：722,877 B / 317,829 B / 113,566 B
MAX_BUNDLE = 760_000
MAX_INDEX = 334_000
MAX_INLINE_STYLE = 119_000
# 2026-11-12 实测：script src=7、stylesheet=1、onclick=86、setInterval=17
MAX_SCRIPT_SRC = 7
MAX_STYLESHEET = 1
MAX_INLINE_ONCLICK = 86
MAX_SETINTERVAL = 17
MAX_SHARDS = 16


class TestFrontendBudget(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.html = TPL.read_text(encoding="utf-8")
        i = cls.html.find("<style")
        j = cls.html.find("</style>", i)
        cls.inline_style = j - i if i >= 0 and j > i else 0

    def test_bundle_size_budget(self):
        n = BUNDLE.stat().st_size
        self.assertLessEqual(n, MAX_BUNDLE,
                             "static/hub.js %d B 超出预算 %d B —— 首屏体积回归（先看是否漏了按页拆分）"
                             % (n, MAX_BUNDLE))

    def test_index_size_budget(self):
        n = len(self.html.encode("utf-8"))
        self.assertLessEqual(n, MAX_INDEX,
                             "templates/index.html %d B 超出预算 %d B —— 先看能否外提 CSS/片段"
                             % (n, MAX_INDEX))

    def test_inline_style_budget(self):
        self.assertLessEqual(self.inline_style, MAX_INLINE_STYLE,
                             "内联 <style> %d 字符超出预算 %d —— T3 已把外提写进计划"
                             % (self.inline_style, MAX_INLINE_STYLE))

    def test_first_paint_request_budget(self):
        scripts = len(re.findall(r"<script[^>]+src=", self.html))
        sheets = len(re.findall(r"<link[^>]+stylesheet", self.html))
        self.assertLessEqual(scripts, MAX_SCRIPT_SRC,
                             "首屏 <script src> %d 个 > 预算 %d —— 多一个请求就多一个 RTT"
                             % (scripts, MAX_SCRIPT_SRC))
        self.assertLessEqual(sheets, MAX_STYLESHEET,
                             "首屏 stylesheet %d 个 > 预算 %d" % (sheets, MAX_STYLESHEET))

    def test_inline_onclick_budget(self):
        n = len(re.findall(r"onclick=", self.html))
        self.assertLessEqual(n, MAX_INLINE_ONCLICK,
                             "内联 onclick %d 处 > 预算 %d —— 新交互一律 data-* + 事件委托"
                             "（AGENTS §4.2：inline onclick 会旁路委托）" % (n, MAX_INLINE_ONCLICK))

    def test_polling_budget(self):
        n = sum(p.read_text(encoding="utf-8").count("setInterval") for p in SHARDS)
        self.assertLessEqual(n, MAX_SETINTERVAL,
                             "setInterval 出现 %d 次 > 预算 %d —— 先并进统一调度器（T6）"
                             % (n, MAX_SETINTERVAL))

    def test_shard_count_budget(self):
        self.assertLessEqual(len(SHARDS), MAX_SHARDS,
                             "分片数 %d > 预算 %d —— 加分片前先确认拼接顺序与闸门清单同步"
                             % (len(SHARDS), MAX_SHARDS))
