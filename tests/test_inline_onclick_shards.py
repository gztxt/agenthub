#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：inline onclick 里嵌 `jsStr()` 的**静默 SyntaxError** 反向闸门。

## 为什么需要它

`jsStr()` 产出的是**双引号** JS 字面量（`JSON.stringify`）。把它拼进
`onclick="…"`（同样是双引号定界）时，HTML 解析器在第一个内层 `"` 处收尾：

    源串： '<div onclick="f(' + jsStr(id) + ')">'
    渲染： <div onclick="f(">                ← 属性值只剩「函数名+左括号」

点击时浏览器执行 `f(` ⇒ 抛 SyntaxError，**只进控制台，UI 无任何反馈**。
v0.13.59 的 `1e3648f` 为防注入把三处 onclick 换成 jsStr，却没同步属性定界符，
于是「按钮还在、点了没反应」活了 19 天；期间静态闸门全绿——因为它们只查
JS 源码里有没有调对函数，不查渲染出的属性是否完整。

## 判据分层（本文件只管第一层）

1. **源码层**（本文件）：所有活跃分片的**代码行**不得同时出现 `onclick=` 与 `jsStr(`。
2. **渲染层**（`tests/probe_inline_onclick.py`，CDP 真浏览器）：六个点位渲染后
   `getAttribute('onclick')` 必须为空（已全改 data-*），且真点击产生预期行为。
   —— 第一层绿不能推出第二层绿，这正是 19 天失明的教训，故两层都钉。

## 反向自证

`test_detector_self_check` 用**合成坏行**证明检测器真的会红；
否则「扫不到」与「没有」不可区分（闸门恒绿的经典成因）。

v0.13.131（PT-20261019-02）。
"""
import pathlib
import re
import sys
import unittest
from html.parser import HTMLParser

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))


def _shards():
    """活跃分片（排除 .bak-*）。**空集必须显式失败**，否则 glob 一变就恒绿。"""
    return sorted(p for p in (_REPO / "static" / "hub").glob("[0-9][0-9]-*.js")
                  if ".bak-" not in p.name)


def _code_lines(text):
    """返回 [(lineno, line)]，跳过整行注释。

    只跳「行首是注释标记」的行：本仓块注释体内每行都以 `*` 开头。
    不做 `//` 的全文剥离——字符串里的 `http://` 会被误伤。
    """
    for i, line in enumerate(text.split("\n"), 1):
        s = line.lstrip()
        if s.startswith("*") or s.startswith("//") or s.startswith("/*"):
            continue
        yield i, line


def _looks_broken(line):
    """这一**代码行**是不是「inline onclick + jsStr」形态。"""
    return "onclick=" in line and "jsStr(" in line


class TestNoInlineOnclickJsStr(unittest.TestCase):

    def setUp(self):
        self.shards = _shards()
        self.texts = {p.name: p.read_text(encoding="utf-8") for p in self.shards}

    # ── 闸门本体 ────────────────────────────────────────────────
    def test_shards_found(self):
        """防「扫了个寂寞」：分片数 < 5 或扫描行数过少都判红。"""
        self.assertGreaterEqual(len(self.shards), 5,
                                "分片没扫到：%r" % [p.name for p in self.shards])
        total = sum(1 for t in self.texts.values() for _ in _code_lines(t))
        self.assertGreater(total, 4000, "扫描行数异常（%d），闸门可能空转" % total)

    def test_no_inline_onclick_with_jsstr(self):
        bad = []
        for name, text in self.texts.items():
            for lineno, line in _code_lines(text):
                if _looks_broken(line):
                    bad.append("%s:%d  %s" % (name, lineno, line.strip()[:100]))
        self.assertEqual(bad, [],
                         "inline onclick 里嵌 jsStr ⇒ 属性被双引号截断、点击静默失败；"
                         "改 data-* + 事件委托：\n" + "\n".join(bad))

    def test_built_artifact_also_clean(self):
        """拼接产物 static/hub.js 同样不许有（漏重建会让源码绿、线上坏）。"""
        hub = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")
        bad = ["hub.js:%d  %s" % (n, l.strip()[:100])
               for n, l in _code_lines(hub) if _looks_broken(l)]
        self.assertEqual(bad, [], "hub.js 是旧产物或未重建：\n" + "\n".join(bad))

    def test_detector_self_check(self):
        """合成坏行必须被抓到；data-* 行必须不被误判。"""
        self.assertTrue(_looks_broken(
            """'<div onclick="f(' + jsStr(x) + ')">'"""))
        self.assertTrue(_looks_broken(
            """'<button onclick="event.stopPropagation();g(' + jsStr(p.path) + ')">'"""))
        self.assertFalse(_looks_broken(
            """'<div data-sk-ccstart="' + escapeHtml(p.path) + '">'"""))
        # 注释行不算（否则本文件的说明文字自己把闸门点红）
        skipped = [l for l in _code_lines(" * 原形态 onclick=\"f(\" + jsStr(id)")]
        self.assertEqual(skipped, [], "注释行应被跳过")

    # ── 六个点位：必须带 data-* 且值走 escapeHtml ───────────────
    def _body(self, shard, pattern):
        m = re.search(pattern, self.texts[shard], re.S)
        self.assertTrue(m, "%s 里找不到 %r" % (shard, pattern))
        return m.group(0)

    def test_site1_verify_agent(self):
        body = self._body("01-core-boot.js", r"function showDetail\(.*?\n\}")
        self.assertIn('data-sk-verify="\' + escapeHtml(a.id)', body)

    def test_site2_3_cloudcli_embed_row_and_button(self):
        body = self._body("02-nav-and-poll.js", r"function renderCloudcliProjects\(.*?\n\}")
        self.assertIn("""data-sk-ccstart="' + escapeHtml(p.path) + '">'""", body,
                      "#2 行必须带 data-sk-ccstart")
        self.assertNotIn("onclick", body, "#2/#3 不许再有 inline onclick")

    def test_site4_cloudcli_detail_button(self):
        body = self._body("04-terminal-ws.js", r"async function loadCloudcliProjects\(.*?\n\}")
        self.assertIn("""data-sk-ccstart="' +\n      escapeHtml(p.path)""", body)
        self.assertNotIn("onclick", body)

    def test_site5_pick_tool_two_datasets(self):
        body = self._body("05-chat-and-history.js", r"async function loadMcp\(.*?\n\}")
        self.assertIn('data-sk-pt-server="\' + escapeHtml(x.server)', body)
        self.assertIn('data-sk-pt-name="\' + escapeHtml(x.name)', body)

    def test_site6_cmd_go(self):
        body = self._body("05-chat-and-history.js", r"function renderCmdList\(.*?\n\}")
        # v0.13.185：命令面板扩成三类目（页面/实体/动作），选择值带前缀 ⇒ 属性值
        # 从 `a.id` 变成 `r.k + ':' + r.id`。判据不变：**整值必须过 escapeHtml**
        # （不是查某个字段名 —— 查字段名会在字段改名时假红）。
        self.assertRegex(body, r"""data-sk-cmdgo="' \+\s*escapeHtml\(r\.k \+ ':' \+ r\.id\)""")

    # ── 委托必须真的绑上（写了 data-* 没人绑 = 死按钮）──────────
    def test_delegations_defined(self):
        for shard, fn in (("01-core-boot.js", "bindDetailDelegation"),
                          ("02-nav-and-poll.js", "bindCloudcliDelegation"),
                          ("05-chat-and-history.js", "bindChatDelegation")):
            self.assertIn("function %s(" % fn, self.texts[shard],
                          "%s 缺 %s" % (shard, fn))

    def test_delegations_called_on_render_path(self):
        self.assertIn("bindDetailDelegation();", self._body("01-core-boot.js", r"function showDetail\(.*?\n\}"))
        self.assertIn("bindCloudcliDelegation();",
                      self._body("02-nav-and-poll.js", r"function renderCloudcliProjects\(.*?\n\}"))
        w05 = self.texts["05-chat-and-history.js"]
        self.assertIn("bindChatDelegation();", self._body("05-chat-and-history.js",
                                                          r"async function loadMcp\(.*?\n\}"))
        self.assertIn("bindChatDelegation();",
                      self._body("05-chat-and-history.js", r"function openCmd\(.*?\n\}"))

    # ── 转义契约：属性值不会被 " 或 & 撕开 ─────────────────────
    def test_escape_html_contract_for_attributes(self):
        """escapeHtml 必须转义 `"` 与 `&`；宿主值经它进属性后能原样读回。

        这是**转义函数**的契约测试，不是整条渲染链的证明（那层由 CDP 探针覆盖）。
        """
        boot = (_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8")
        m = re.search(r"function escapeHtml\(s\) \{(.*?)\n\}", boot, re.S)
        self.assertTrue(m, "escapeHtml 缺失")
        self.assertIn("'\"': '&quot;'", m.group(1), "escapeHtml 必须转义双引号")
        self.assertIn("'&': '&amp;'", m.group(1), "escapeHtml 必须转义 &")

        # 用 Python 侧同一映射复现，验证 HTML 解析后属性值 == 原值
        def esc(v):
            return (str(v).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
                    .replace('"', "&quot;").replace("'", "&#39;"))

        hostile = 'a" onmouseover="alert(1)'
        html = '<div data-sk-ccstart="%s">x</div>' % esc(hostile)

        class P(HTMLParser):
            seen = None

            def handle_starttag(self, tag, attrs):
                if tag == "div":
                    self.seen = dict(attrs)

        p = P()
        p.feed(html)
        self.assertEqual(p.seen.get("data-sk-ccstart"), hostile,
                         "属性值必须原样读回（这是 dataset 拿到正确路径的前提）")
        self.assertNotIn("onmouseover", p.seen, "不允许凭空多出事件属性（属性注入）")


if __name__ == "__main__":
    unittest.main()
