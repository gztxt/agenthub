"""L0 hermetic：终端剪贴板「焦点归还 + 原生粘贴通道」（v0.13.90，2026-10-07 用户报障）。

两件用户报障（同一天，同一处代码）：
 ① 「输入栏无法使用复制粘贴功能，连键盘快捷键都使用不了」
 ② 复制走 P3 的 execCommand 兜底时，临时 textarea 被 `ta.select()` 选中 ⇒
    焦点从 xterm 的 helper textarea 被抢到 body ⇒ **此后所有按键都不再进终端**
    （xterm 的键盘通路完全依赖那个 textarea 持焦）。用户体感就是"快捷键全哑"。

实测（真 chromium + CDP，局域网 http，2026-10-07）：
    复制前 focus=termTA → 复制后 focus=BODY；此后 Ctrl+V 的 paste 事件计数恒 0。
    Ctrl+V 本身：xterm 的 keydown 会 preventDefault ⇒ 浏览器原生粘贴被掐断；
    而改前那条 JS 路径用 navigator.clipboard.readText，本机局域网 http
    （非安全上下文）实测 `navigator.clipboard === undefined` ⇒ 粘贴整个不可用。
    候选修法实测通过：自定义键处理器对 Ctrl+V 返回 false 且**不** preventDefault
    ⇒ 原生粘贴落到 helper textarea ⇒ pasteEvt 1→2、termPasteText 被调用、
    pty 实收 ESC[200~PASTED_TEXT ESC[201~。

本测试锁的是**契约**（静态），行为由 tests/verify_term_clipboard.py 真渲染锁。

v0.13.126（2026-10-18，Mac 端侧报障）改判的契约：
  · 有选区 Ctrl/Cmd+C **不再** preventDefault + JS 写剪贴板，改为只 return false，
    把复制交给浏览器原生 copy 链（xterm 自己在 .xterm 上挂了 copyHandler，本文件
    #termEl 还有一条容器级兜底）。理由：WebKit/Safari 的用户手势不跨 Promise 微任务，
    而局域网 http（无 navigator.clipboard）只剩 execCommand 一条路 ⇒ 改前必然静默失败。
  · 非安全上下文的 termCopyFallback 必须**同步**调 execCommand（右键菜单/OSC 52 仍用它）。
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
CARDS = REPO / "static" / "hub" / "03-agents-cards.js"


def _src(p: Path) -> str:
    s = p.read_text(encoding="utf-8")
    s = re.sub(r"/\*.*?\*/", "", s, flags=re.S)
    s = re.sub(r"//[^\n]*", "", s)
    return s


class TestCopyFallbackRestoresFocus(unittest.TestCase):
    def setUp(self):
        self.s = _src(CARDS)

    def test_snippet_of_copy_fallback(self):
        self.assertIn("function termCopyFallback(", self.s)
        seg = self.s.split("function termCopyFallback(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("execCommand('copy')", seg, "复制兜底不见了")
        # 关键：复制前记下焦点宿主、复制后归还。
        self.assertRegex(seg, r"const prev\s*=\s*document\.activeElement",
                         "复制兜底没记下复制前的焦点宿主 ⇒ 复制一次就把终端输入废掉")
        self.assertRegex(seg, r"prev\.focus\(\)",
                         "复制兜底没有把焦点还回去（用户报障的直接根因）")
        # finally 才是对的：成功/失败/抛异常三条路都要还。
        self.assertRegex(seg, r"}\s*finally\s*\{",
                         "焦点归还必须放在 finally，否则异常路径又会把焦点丢在 body")
        self.assertNotRegex(seg, r"prev\.focus\(\)[\s\S]{0,80}catch[\s\S]{0,40}return false",
                            "finally 里 return 会吞掉复制结果（返回值必须来自 copy 那一步）")


class TestPasteGoesThroughBrowserNative(unittest.TestCase):
    def setUp(self):
        self.s = _src(CARDS)

    def test_ctrl_v_is_not_prevented(self):
        """Ctrl/Cmd+V 必须放给浏览器原生粘贴去处理 —— 不许 preventDefault。"""
        seg = self.s.split("attachCustomKeyEventHandler", 1)[1].split("term.dataset_copyBound", 1)[0]
        m = re.search(r"if \(k === 'v'\) \{([\s\S]*?)\n      \}", seg)
        self.assertIsNotNone(m, "Ctrl+V 分支不见了")
        body = m.group(1)
        self.assertNotIn("preventDefault", body,
                         "Ctrl+V 里 preventDefault 会掐断浏览器原生粘贴 ⇒ 非安全上下文下粘贴整个不可用")
        self.assertRegex(body, r"return false",
                         "Ctrl+V 应返回 false：只让 xterm 别把 ^V 当字节送进 pty")

    def test_ctrl_v_does_not_depend_on_navigator_clipboard(self):
        seg = self.s.split("attachCustomKeyEventHandler", 1)[1].split("term.dataset_copyBound", 1)[0]
        m = re.search(r"if \(k === 'v'\) \{([\s\S]*?)\n      \}", seg)
        body = m.group(1)
        self.assertNotIn("navigator.clipboard", body,
                         "粘贴不该依赖 navigator.clipboard：局域网 http 下它不存在")

    def test_paste_event_path_still_wired(self):
        """原生粘贴的落点是 textarea 的 paste 事件 ⇒ termPasteBind 必须还在。"""
        self.assertIn("function termPasteBind(", self.s)
        self.assertRegex(self.s, r"ta\.addEventListener\('paste'",
                         "没有 paste 监听 ⇒ 放给浏览器的原生粘贴没人接")
        self.assertIn("termPasteText(", self.s, "paste 安全包装丢了（bracketed paste 注入面）")

    def test_ctrl_c_with_selection_defers_to_native_copy(self):
        """v0.13.126：有选区 Ctrl+C 必须**不** preventDefault，交给浏览器原生 copy 链。

        改前（v0.13.90–125）是 preventDefault + termCopySelection()。那条路在
        Mac/Safari + 局域网 http 下会因「用户手势不跨微任务」静默失败（端侧实测）。
        当前形状必须能区分于旧形状，否则后人「顺手加回 preventDefault」不会被发现。
        """
        seg = self.s.split("attachCustomKeyEventHandler", 1)[1].split("term.dataset_copyBound", 1)[0]
        m = re.search(r"if \(k === 'c' && term\.hasSelection\(\)\) \{([\s\S]*?)\n        \}", seg)
        self.assertIsNotNone(m, "有选区 Ctrl+C 分支不见了")
        body = m.group(1)
        self.assertNotIn("preventDefault", body,
                         "有选区 Ctrl+C 不许 preventDefault：它会掐断浏览器原生 copy 事件，"
                         "而 JS 写剪贴板这条路在 Mac/Safari 的 http 源上会丢手势")
        self.assertNotIn("stopPropagation", body, "keydown 上 stopPropagation 会把原生链一起挡掉")
        self.assertNotIn("termCopySelection", body,
                         "键盘复制不该再走 JS 写剪贴板（原生 copy 链才是跨平台的）")
        self.assertRegex(body, r"return false",
                         "就要返回 false：让 xterm 别把 ^C 当字节送进 pty")

    def test_native_copy_chain_has_a_receiver(self):
        """交给原生链的前提：copy 事件必须有人接（容器级兜底在 ⇒ 选区真进 clipboardData）。"""
        self.assertIn("function termCopyBind(", self.s)
        self.assertRegex(self.s, r"el\.addEventListener\('copy'",
                         "#termEl 的容器级 copy 兜底丢了 ⇒ 原生 Cmd+C 没有落地接收方")
        self.assertIn("term.hasSelection()", self.s)

    def test_non_secure_copy_runs_synchronously(self):
        """非安全上下文必须同步 execCommand：不能排在 Promise 微任务里（Safari 会丢手势）。"""
        seg = self.s.split("function termCopyFallback(", 1)[1].split("\nfunction ", 1)[0]
        self.assertIn("execCommand('copy')", seg, "同步兜底不在 termCopyFallback 里")
        # 同步分支的形状：Promise.resolve(viaExec()) —— 同步算完再包成 Promise
        self.assertRegex(seg, r"return Promise\.resolve\(viaExec\(\)\)",
                         "非安全上下文必须是同步 execCommand（改回 p.then(...) 就会在 Safari 丢手势）")
        self.assertRegex(seg, r"window\.isSecureContext",
                         "安全上下文分支要看 isSecureContext，不能只看 navigator.clipboard 有没有")
        # 旧形状（先发 clipboard 写、再在 then 里兜底）出现即红
        self.assertNotRegex(seg, r"return p\.then\(",
                            "旧形状「先 navigator.clipboard 再 p.then 里 execCommand」已否决")

    def test_paste_hint_is_platform_aware(self):
        """右键菜单的「请按 Ctrl+V」在 Mac 上是教错键 —— 提示必须分平台。"""
        self.assertRegex(self.s, r"⌘V|Apple|Mac",
                         "粘贴提示要按平台给 ⌘V/Ctrl+V，不能对 Mac 用户写 Ctrl+V")

    def test_no_selection_ctrl_c_still_signals(self):
        """无选区 Ctrl+C 必须放行 ⇒ 仍是 SIGINT（bash 语义不能被这次修复破坏）。"""
        seg = self.s.split("attachCustomKeyEventHandler", 1)[1].split("term.dataset_copyBound", 1)[0]
        self.assertRegex(seg, r"k === 'c' && term\.hasSelection\(\)",
                         "Ctrl+C 必须带 hasSelection 条件，否则 SIGINT 被吞")


if __name__ == "__main__":
    unittest.main(verbosity=2)
