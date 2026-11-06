#!/usr/bin/env python3
"""真结构闸门：模板里的每一页必须真的在 <main id="mainWrap"> 里面。

为什么需要这条：HTML 不会为标签不配对报错。多写一个 </div> 会让浏览器
**提前关闭 <main>**，它后面的页面就成了 <body> 的直接子节点 —— 渲染在菜单
**下方**（整宽、压住侧栏），而不是菜单右侧。2026-10-20 的 37fdd382 就这样
把「知识库 / 资产 / 设置五页」甩出了 main，直到 10-25 用户报「设置子菜单
应该是左右布局不是上下布局」才被发现。这 5 天里：
  - 服务全绿、/api/* 全绿；
  - 既有测试全绿（它们只读字符串、接口、点击态，没有一条看父子关系）。
所以判据必须落在**结构**上，且要能对着旧文件变红。

判据（逐条可证伪，任一不成立即 FAIL）：
  ① 关闭 main 只能由它自己的 </main> 触发；任何「为了匹配而跳过 main」的
     隐式弹栈都算错位（多一个 </div> 正是这种形态）；
  ② 每个 <section class="page ..."> 开标签时，main 都必须在祖先栈里；
  ③ 栈最终为空（没有漏关的开标签）；
  ④ 关键页 id 一个都不能少（防止修结构时误删整段）。

只依赖标准库。html.parser 不做树构建，故这里按 HTML5 的**可选结束标签**
规则（option/li/p/td/tr/dt/dd…）自己收栈 —— 否则合法的 <option> 不闭合
会被误判成结构错误。
"""
import re
import unittest
from html.parser import HTMLParser
from pathlib import Path

TPL = Path(__file__).resolve().parent.parent / "templates" / "index.html"
HUB = Path(__file__).resolve().parent.parent / "static" / "hub.js"

VOID = {"area", "base", "br", "col", "embed", "hr", "img", "input", "link",
        "meta", "param", "source", "track", "wbr"}

# HTML5 可选结束标签：遇到这些「新开标签」时，把仍开着的同类兄弟自动收掉
AUTO_CLOSE_ON = {
    "option": {"option", "optgroup"},
    "optgroup": {"optgroup"},
    "li": {"li"},
    "dt": {"dt", "dd"},
    "dd": {"dt", "dd"},
    "p": {"p", "div", "section", "table", "ul", "ol", "dl", "pre", "form",
          "h1", "h2", "h3", "h4", "h5", "h6", "details", "figure"},
    "td": {"td", "th", "tr", "tbody", "tfoot"},
    "th": {"td", "th", "tr", "tbody", "tfoot"},
    "tr": {"tr", "tbody", "tfoot"},
    "thead": {"tbody", "tfoot"},
    "tbody": {"tbody", "tfoot"},
}

PAGE_IDS = [
    "page-classroom", "page-chat", "page-localprojects", "page-github",
    "page-resources", "page-memory", "page-tasks", "page-jobs", "page-mcp",
    "page-ports", "page-telemetry", "page-skills", "page-kb", "page-assets",
    "page-settings-model", "page-settings-github", "page-settings-token",
    "page-settings-logs",
]


class Structure(HTMLParser):
    def __init__(self):
        super().__init__(convert_charrefs=True)
        self.stack = []          # [(tag, lineno, attrs)]
        self.errors = []         # 结构错误（人话）
        self.page_outside = []   # 不在 main 里的 section.page
        self.page_ids = []       # 找到的 section.page id

    # ── 工具 ──────────────────────────────────────────────
    def _attrs(self, attrs):
        return {k: (v or "") for k, v in attrs}

    def _auto_close(self, tag):
        while self.stack:
            top = self.stack[-1][0]
            if tag in AUTO_CLOSE_ON.get(top, set()):
                self.stack.pop()
            else:
                break

    def _pop_to(self, tag, lineno):
        """按浏览器语义收栈；如果为了找 tag 必须跳过 main，就是结构错误。"""
        for i in range(len(self.stack) - 1, -1, -1):
            if self.stack[i][0] == tag:
                skipped = self.stack[i + 1:]
                if any(s[0] == "main" for s in skipped):
                    self.errors.append(
                        "第 %d 行的 </%s> 会隐式关闭 <main>（跳过了 %s）——"
                        "多了一个不配对的结束标签，main 之后的页面会掉到 body 下"
                        % (lineno, tag, "、".join("<%s>" % s[0] for s in skipped)))
                del self.stack[i:]
                return True
        self.errors.append("第 %d 行出现无配对的 </%s>" % (lineno, tag))
        return False

    # ── HTMLParser 回调 ──────────────────────────────────
    def handle_starttag(self, tag, attrs):
        if tag in VOID:
            return
        self._auto_close(tag)
        a = self._attrs(attrs)
        if tag == "section" and "page" in a.get("class", "").split():
            pid = a.get("id", "")
            self.page_ids.append(pid)
            if not any(s[0] == "main" for s in self.stack):
                self.page_outside.append((pid, self.getpos()[0]))
        self.stack.append((tag, self.getpos()[0], a))

    def handle_startendtag(self, tag, attrs):
        # <div/> 之类：HTML 里等价于开标签，但不能当自闭合用
        if tag not in VOID:
            self.handle_starttag(tag, attrs)

    def handle_endtag(self, tag):
        if tag in VOID:
            return
        self._pop_to(tag, self.getpos()[0])

    def close(self):
        super().close()
        if self.stack:
            self.errors.append(
                "文件结束时仍未关闭：%s（多半是漏写结束标签）"
                % "、".join("<%s>(第 %d 行)" % (t, ln) for t, ln, _ in self.stack))


def _parse():
    p = Structure()
    p.feed(TPL.read_text(encoding="utf-8"))
    p.close()
    return p


class TestDomStructure(unittest.TestCase):
    """四条判据写成 TestCase：模块级 test_ 函数 unittest discover 收不到
    （tests/test_tier_collector_parity.py 会直接判红）。"""

    @classmethod
    def setUpClass(cls):
        cls.p = _parse()
        cls.src = TPL.read_text(encoding="utf-8")

    def test_no_tag_closes_main_early(self):
        self.assertEqual(self.p.errors, [],
                         "HTML 结构错误：\n  " + "\n  ".join(self.p.errors))

    def test_all_page_sections_inside_main(self):
        self.assertEqual(self.p.page_outside, [],
                         "以下 section.page 不在 #mainWrap 里（会渲染在菜单下方而不是右侧）：\n  "
                         + "\n  ".join("%s（第 %d 行）" % x for x in self.p.page_outside))

    def test_every_expected_page_present(self):
        missing = [x for x in PAGE_IDS if x not in self.p.page_ids]
        self.assertEqual(missing, [], "页面 section 缺失：%s" % missing)

    def test_main_closed_exactly_once(self):
        self.assertEqual(len(re.findall(r"</main\s*>", self.src)), 1,
                         "</main> 必须且只能出现一次")


# ── Task 1（2026-11-05 全站壳与导航）新增结构判据 ──────────────────
# 判据全部对着「源码事实」写（JS 分片拼接产物 + 模板 CSS 字面量），不靠
# 真机渲染：本仓 L0 惯例（test_hubjs_split 同族）= 在文件文本上断言可证伪的量。

class TestNavAccordionA11y(unittest.TestCase):
    """手风琴头（.nav-acc-head）键盘化 + M3 激活指示条 + 触控目标（brief Step 2/3/4）。"""

    @classmethod
    def setUpClass(cls):
        cls.js = HUB.read_text(encoding="utf-8")
        cls.css = TPL.read_text(encoding="utf-8")

    def _rule(self, sel):
        """从模板里取一条选择器对应的 CSS 规则体（首个命中）。"""
        m = re.search(re.escape(sel) + r"\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "CSS 里找不到规则：" + sel)
        return m.group(1)

    def test_accordion_head_has_aria_expanded_and_role_button(self):
        # renderNav 生成的手风琴头：aria-expanded 随展开同步，role=button 明示可按键操作
        self.assertRegex(self.js, r'(?s)<button class="nav-acc-head".{0,80}role="button".{0,40}aria-expanded="',
                         "手风琴头缺 role=button / aria-expanded（renderNav 生成处）")

    def test_accordion_keyboard_handlers_present(self):
        # MDN 键盘可导航 widget：组头方向键/Home/End 移焦，Enter/Space 切换展开
        self.assertIn("navAccKeydown", self.js)
        self.assertRegex(self.js, r"ArrowDown|ArrowUp")
        m = re.search(r"function\s+navAccKeydown\(.*?\n\}", self.js, re.S)
        self.assertIsNotNone(m, "navAccKeydown 不是顶层函数声明（拼写/嵌套变了要同步符号阶梯）")
        body = m.group(0)
        # Enter/Space 由原生 <button> 的 click 委托接管（data-group 分支），
        # navAccKeydown 只负责移焦，故不要求 Enter 分支（控制器裁定 F4）。
        for k in ("ArrowDown", "ArrowUp", "Home", "End"):
            self.assertIn(k, body, "navAccKeydown 缺键盘处理：%s" % k)

    def test_no_positive_tabindex_in_nav_or_palette(self):
        self.assertNotIn('tabindex="1"', self.js)
        self.assertNotIn('tabindex="2"', self.js)
        self.assertNotIn("tabindex='1'", self.js)

    def test_nav_item_active_indicator_rule(self):
        # 2026-11-07 用户裁定：选中只靠整行高亮，最左竖线指示条已删除
        self.assertNotIn(".nav-item.on::before", self.css)

    def test_nav_child_larger_than_parent_font(self):
        # 子菜单名字大小/样式与父菜单一致、左对齐（不缩进、不缩小）
        body = self._rule(".nav-item.is-child .lbl")
        self.assertIn("var(--fs-base)", body)
        self.assertIn("text-align: left", body)

    def test_nav_icon_size_is_24px_token(self):
        # M3 导航图标 24dp。尺寸只许走 token：模板声明 --ico-nav: 24px，
        # 行内图标宽度引用 token（不写死 24px 字面量到选择器里）。
        self.assertRegex(self.css, r"--ico-nav:\s*24px")
        self.assertRegex(self.css, r"\.nav-item svg\.i,\.side-item svg\.i\s*\{[^}]*var\(--ico-nav\)")

    def test_touch_targets_min_44px_wide_34px_narrow_tier(self):
        # 触控目标：宽屏 44px；窄屏档 34px（分档偏好「一档一键」：同一档位内
        # 用键名区分，不跨档写全局）。既有断言值不得改口径，只能核对在档。
        self.assertIn("min-height: 44px", self.css)
        self.assertIn("min-height: 34px", self.css)


class TestNavKeyboardTopLevel(unittest.TestCase):
    """navAccKeydown 是顶层 function 声明 ⇒ 必须已登记模板「符号阶梯」。"""

    @classmethod
    def setUpClass(cls):
        cls.js = HUB.read_text(encoding="utf-8")
        cls.src = TPL.read_text(encoding="utf-8")

    def test_nav_acc_keydown_registered_in_ladder_or_no_longer_toplevel(self):
        top = re.search(r"^function\s+navAccKeydown\s*\(", self.js, re.M)
        ladder = re.search(r"'符号阶梯='\s*\+\s*\[(.*?)\]", self.src, re.S)
        self.assertIsNotNone(ladder, "模板里找不到 符号阶梯 清单")
        if top:
            self.assertIn("'navAccKeydown'", ladder.group(1),
                          "新增顶层 function navAccKeydown 未登记符号阶梯（TestLadder 会红）")


if __name__ == "__main__":
    unittest.main()
