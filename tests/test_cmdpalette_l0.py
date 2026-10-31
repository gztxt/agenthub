"""命令面板扩展 + toast 动作（v0.13.185）—— L0 源码形状断言。

为什么读源码而不是跑浏览器：本仓 L0 是 hermetic（无 chromium、无 jsdom 依赖），
历史判据一律用"形状断言"钉住结构。这里钉的是**回归点**，不是实现细节：

1. 命令面板仍是同一个 #cmdMask（不许出现第二套遮罩 —— AGENTS.md 4.2 浮层唯一性）
2. 开关只有 openCmd / closeCmd 两个入口（别处不许直接 classList.add('on')）
3. 列表项走 data-* 委托，不许 inline onclick（v0.13.131 的 SyntaxError 回归）
4. 三类目（页面/实体/动作）与前缀 'p:' 'a:' 'x:' 都在，且裸 id 仍按实体处理（兼容）
5. ↑↓ 选择只改 class，不重渲染（重渲染会丢焦点、把用户输入打断）
6. toast 第三参 opts 存在，且旧签名 `toast(msg, cls)` 仍合法
7. 新增 CSS 只用 var() token，不出现字面色
"""

import re
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
HUB = ROOT / "static" / "hub"
TM = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")


def read(name):
    return (HUB / name).read_text(encoding="utf-8")


class TestCommandPaletteShape(unittest.TestCase):
    def setUp(self):
        self.js05 = read("05-chat-and-history.js")
        self.js06 = read("06-manager-tasks.js")

    def test_single_overlay_container(self):
        self.assertEqual(TM.count('id="cmdMask"'), 1, "命令面板遮罩必须唯一")
        self.assertIn('class="modal-mask" id="cmdMask"', TM,
                      "复用既有 .modal-mask，不自造第二套浮层样式")

    def test_no_inline_onclick_on_mask(self):
        self.assertNotIn('onclick="if(event.target===this)closeCmd()"', TM,
                         "遮罩关闭已改 data 委托；inline onclick 会让 CSP 无法收紧")
        self.assertIn('id="cmdMask"', TM)
        self.assertIn("e.target === cmdMaskEl", self.js05,
                      "委托必须只认「点在遮罩本身」，点内容区不关")

    def test_only_two_entry_points(self):
        # 除了 openCmd / closeCmd 自身，别的函数不许直接改 cmdMask 的 class
        for m in re.finditer(r"\$\('cmdMask'\)\.classList\.(add|remove)\('on'\)", self.js05):
            start = self.js05.rfind("function ", 0, m.start())
            fn = self.js05[start:start + 40]
            self.assertTrue(
                "function openCmd" in fn or "function closeCmd" in fn,
                "命令面板开关只允许走 openCmd/closeCmd，发现：" + fn.strip())

    def test_three_categories_and_prefixes(self):
        for token in ("function cmdCatalog", "'p'", "'a'", "'x'",
                      "indexOf('p:')", "indexOf('x:')", "indexOf('a:')"):
            self.assertIn(token, self.js05, "缺：" + token)

    def test_bare_id_still_goes_to_agent(self):
        # 旧书签/旧节点可能只存裸 id，必须仍按实体处理（向后兼容）
        tail = self.js05.split("function cmdGo(sel)")[1][:600]
        self.assertIn("gotoChat(s)", tail)
        self.assertLess(tail.index("indexOf('a:')"), tail.index("gotoChat(s)"),
                        "前缀分派必须在裸 id 兜底之前")

    def test_highlight_does_not_rerender(self):
        body = self.js05.split("function cmdHighlight(idx)")[1].split("function cmdMove")[0]
        self.assertNotIn("innerHTML", body, "高亮只改 class；重渲染会丢焦点")
        self.assertIn("classList.toggle('sel'", body)

    def test_keyboard_updown_wired(self):
        self.assertIn("ArrowDown", self.js06)
        self.assertIn("ArrowUp", self.js06)
        self.assertIn("cmdSubmit()", self.js06)
        # 空查询按 Enter 仍是「关闭」而不是「跳第一条」（原口径）
        body = self.js06.split("$('cmdInput').addEventListener('keydown'")[1][:600]
        self.assertIn("if (!e.target.value.trim()) { closeCmd(); return; }", body)

    def test_pages_filtered_by_dom_presence(self):
        self.assertIn("document.getElementById('page-' + p)", self.js05,
                      "页面清单须按 #page-<id> 运行时过滤，避免指向已删页")


class TestToastShape(unittest.TestCase):
    def setUp(self):
        self.js01 = read("01-core-boot.js")

    def test_signature_backward_compatible(self):
        self.assertIn("function toast(msg, cls, opts = {})", self.js01)
        self.assertIn("opts = opts || {}", self.js01,
                      "显式传 null 也要兜住（默认参数只挡 undefined）")

    def test_dedupe_map_exists(self):
        self.assertIn("_toastLive", self.js01)
        self.assertIn("if (live && live.isConnected)", self.js01,
                      "同文复用节点，不堆叠")

    def test_action_button_isolated(self):
        self.assertIn("opts.action.label", self.js01)
        self.assertIn("finally { el.remove();", self.js01,
                      "动作抛错也必须收掉提示，否则永久钉屏")

    def test_action_css_present_and_token_only(self):
        self.assertIn(".toast-act", TM)
        self.assertIn(".cmd-group", TM)
        for line in TM.splitlines():
            if ".toast-act" in line or ".cmd-group" in line:
                self.assertIsNone(re.search(r"#[0-9a-fA-F]{3,8}\b", line),
                                  "禁止字面色：" + line.strip())
                self.assertIsNone(re.search(r"\brgba?\(", line),
                                  "禁止字面色：" + line.strip())

    def test_retry_action_wired_on_agent_failure(self):
        self.assertIn("label: '重试'", self.js01)
        self.assertIn("agentFailStreak = 0", self.js01,
                      "重试前清计数，否则下一次失败会直接跳过 ==2 的报错点")


if __name__ == "__main__":
    unittest.main()
