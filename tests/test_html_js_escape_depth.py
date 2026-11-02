"""L0 hermetic：HTML/JS 双语境转义纵深（P2-D，PT-20260930-01）。

要防的漏洞类别（**不是**「忘记 escapeHtml」那种一眼能看出来的问题）：
`onclick="f('…')"` 里的字符串**先按 HTML 解码、再按 JS 解析**——两道语境。
只用 HTML 实体转义不足以让任意 id 安全进 JS 字面量：形如
`x&#39;);evil(//` 的值经 HTML 解码后就是 `' );evil(//`，能提前闭合参数列表。
换句话说，`escapeHtml(…).replace(/'/g,"\\'")` 这种手工补丁**方向对、层级错**：
它在 HTML 层把 `'` 变成 `&#39;`，可 `&#39;` 里的 `&` 自己又会被解码回来。

本测试锁四条：
  ① `jsStr` 存在且用 JSON.stringify 出合法 JS 字面量（并转掉 < > U+2028/29）；
  ② **不再有人手工 `escapeHtml(...).replace(/'/g,…)`**（三处历史补丁已改 jsStr）；
  ③ 资源页三个动作全部走 data-* 委托，页内零 inline onclick
     （inline 会旁路委托、跳过「点完收场」，同 P1-17 的纪律）；
  ④ `escapeHtml` 只有一处实现（副本漂移 = P1-20「四处各判各的」同型亏）。
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HUB = REPO / "static" / "hub"
CORE = HUB / "01-core-boot.js"
TERM = HUB / "04-terminal-ws.js"
RES = HUB / "11-resources.js"
SHARDS = sorted(p for p in HUB.glob("*.js") if ".bak-" not in p.name)


def _src(p: Path) -> str:
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return s


class TestJsStringLiteralEscaping(unittest.TestCase):
    def test_jsstr_exists_and_is_json_based(self):
        s = _src(CORE)
        m = re.search(r"function jsStr\(s\)\s*\{(.*?)\n\}", s, re.S)
        self.assertIsNotNone(m, "01-core-boot.js 必须提供 jsStr()：HTML 实体转义不足以进 JS 字面量")
        body = m.group(1)
        self.assertIn("JSON.stringify", body, "jsStr 必须用 JSON.stringify 出合法 JS 字面量")
        self.assertIn("s == null ? '' : s", body,
                      "jsStr 必须容忍 null/undefined（与 escapeHtml 同口径）")

    def test_jsstr_escapes_html_relevant_chars(self):
        s = _src(CORE)
        m = re.search(r"function jsStr\(s\)\s*\{(.*?)\n\}", s, re.S)
        body = m.group(1)
        for ch, esc in (("<", "u003c"), (">", "u003e")):
            self.assertIn(esc, body, "jsStr 必须把 %s 转成 \\%s：防 </script> 提前收尾" % (ch, esc))
        self.assertIn("u2028", body, "jsStr 必须转 U+2028（ES2019 前是非法行分隔符）")
        self.assertIn("u2029", body, "jsStr 必须转 U+2029（同上）")

    def test_no_manual_quote_patch_after_escapehtml(self):
        """历史三处补丁方向对、层级错（HTML 层替 JS 层转义），必须清零。"""
        offenders = []
        for p in SHARDS:
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"escapeHtml\([^)]*\)\.replace\(\s*/'\s*/g", src):
                offenders.append("%s:%d" % (p.name, src[:m.start()].count("\n") + 1))
        self.assertEqual([], offenders,
                         "escapeHtml(...).replace(/'/g) 是双语境漏转义，改用 jsStr()：%s" % offenders)

    def test_id_bearing_onclick_uses_jsstr(self):
        """带 id/name/path 的 inline onclick 必须走 jsStr，不能只 escapeHtml。"""
        for p in (CORE, TERM):
            src = p.read_text(encoding="utf-8")
            for m in re.finditer(r"onclick=\"[a-zA-Z_$][\w$]*\(([^)]*)\)\"", src):
                args = m.group(1)
                if "escapeHtml(" in args:
                    ln = src[:m.start()].count("\n") + 1
                    self.fail("%s:%d 的 onclick 参数里出现 escapeHtml（HTML 层），应改 jsStr：%s"
                              % (p.name, ln, m.group(0)[:120]))

    def test_no_new_string_argv_in_inline_onclick(self):
        """inline `onclick` 带**字符串实参插值**不许**新增**——新代码一律 data-* + 委托。

        2026-11-03 混排核验实测的闸门盲区：上一条用例的正则
        `onclick="f\\(([^)]*)\\)"` 要求**第一个 `)` 之后立刻是 `"`**，
        而 `onclick="openRun(\\''+ run_id +'\\')"` 在第一个 `)` 后面跟的是 ` +`
        ⇒ 结构性不匹配。于是 6 处双语境漏转义（4 处零层转义 + 2 处把 escapeHtml 当 JS
        转义用）全被放过，pytest 照样 10 passed。**闸门绿 ≠ 转义对。**

        这里用**棘轮**（ratchet）而不是一次清零：存量 12 处散在 02/03/05 三个分片里，
        一次全改的爆炸半径远超本次改动面（军规：发现既存问题→停手报请，不顺手修）。
        所以基线落盘、只判「有没有新增」；存量清了就同步删基线行。
        """
        base = REPO / "tests" / "fixtures" / "inline_onclick_string_argv.txt"
        self.assertTrue(base.exists(), "缺基线文件 %s（棘轮必须有基线，否则等于没有闸门）" % base)
        allowed = {ln.strip() for ln in base.read_text(encoding="utf-8").splitlines()
                   if ln.strip() and not ln.startswith("#")}
        offenders = []
        for p in SHARDS:
            src = _src(p)          # 去注释：注释里的反例不算违规
            for m in re.finditer(r"onclick=\"", src):
                tail = src[m.end():m.end() + 400]
                cut = re.search(r'(?<!\\)"', tail)   # 到本段 JS 字符串结束
                seg = tail[:cut.start()] if cut else tail[:400]
                if "\\'" not in seg:
                    continue
                key = "%s|%s" % (p.name, re.sub(r"\s+", " ", seg)[:60].strip())
                if key not in allowed:
                    offenders.append("%s:%d %s" % (p.name, src[:m.start()].count("\n") + 1, seg[:80]))
        self.assertEqual([], offenders,
                         "新增了 inline onclick 带字符串实参（双语境漏转义）⇒ "
                         "改 data-* + 事件委托；确属存量请登记基线并说明理由：%s" % offenders)


class TestResourceActionsDelegation(unittest.TestCase):
    """P2-D：资源页 inline onclick → data-* 委托（inline 旁路「点完收场」）。"""

    def test_resource_page_has_no_inline_onclick(self):
        src = _src(RES)
        found = re.findall(r"onclick=", src)
        self.assertEqual([], found,
                         "11-resources.js 仍有 inline onclick（%d 处）：卡片展开/结束/强杀"
                         "应改 data-* 走 bindResourceActions 委托" % len(found))

    def test_three_actions_go_through_delegate(self):
        src = _src(RES)
        self.assertIn("function bindResourceActions", src, "必须有 bindResourceActions 单一出口")
        for attr in ("data-toggle", "data-kill", 'dataset.kill'):
            self.assertIn(attr, src, "委托需要 %s：三个动作都要能被委托读到" % attr)
        self.assertIn("addEventListener", src, "委托必须真的绑事件")
        self.assertIn("resBound", src, "bind 必须幂等（采样轮询会重渲染，不能重复绑）")

    def test_kill_button_stops_propagation_in_delegate(self):
        """kill 在可展开的卡片内：不 stopPropagation 会点「结束」顺手展开卡片。"""
        src = _src(RES)
        # 匹配**函数定义**（`function bindResourceActions(...)`），不要匹配调用点：
        # `renderResources` 末尾那句 `bindResourceActions(listEl);` 会被
        # 非贪婪的 `[\s\S]*?\n\}` 先命中，断言就落在一个分号上。
        m = re.search(r"function bindResourceActions\(listEl\)\s*\{[\s\S]*?\n\}", src)
        self.assertIsNotNone(m, "bindResourceActions 函数体缺失")
        self.assertIn("stopPropagation", m.group(0),
                      "kill 分支必须 stopPropagation：收拢到委托一处统一做（不再依赖每个渲染点记得写）")

    def test_agent_id_escaped_in_attributes(self):
        src = _src(RES)
        self.assertNotRegex(src, r'data-agent="\s*\+', 'data-agent 属性拼接处必须走 escapeHtml(jsStr)')


class TestEscapeHtmlSingleSource(unittest.TestCase):
    """同语义两份实现 = 迟早漂（P1-20「四处各判各的」同型）。"""

    def test_only_one_escapehtml_implementation(self):
        owners = [p.name for p in SHARDS if "function escapeHtml" in _src(p)]
        self.assertEqual(["01-core-boot.js"], owners,
                         "escapeHtml 应只有 01-core-boot.js 一份实现（副本会漂，且历史副本有 "
                         "`(s || \"\")` 把 pid=0 渲染成空串的真 bug）：现 %s" % owners)

    def test_canonical_escapehtml_handles_zero(self):
        s = _src(CORE)
        m = re.search(r"function escapeHtml\(s\)\s*\{(.*?)\n\}", s, re.S)
        self.assertIn("s == null ? '' : s", m.group(1),
                      "正本必须用 String(s == null ? '' : s)：`(s || \"\")` 会把 0/false 变空串")


if __name__ == "__main__":
    unittest.main()
