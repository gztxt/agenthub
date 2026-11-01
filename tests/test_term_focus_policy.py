"""L0 · 终端焦点与全局快捷键的**结构性**护栏（纯文本断言，零宿主依赖）。

为什么需要静态护栏：这两条修复的失效方式很安静 —— 后人只要在别处补一句
`term.focus()`，或者把 `/` 分支挪到 `if (editing) return` 之前，
手机就又开始弹软键盘、终端里又开始吞 `/`，而 node --check 与单元测试全都不会红。
所以这里守的是"**位置**"，不是"存在"：

  1. `term.focus()` 全仓只能出现在 `if (termFocusWanted(opts))` 之后同一行；
  2. 八个 termConnect 入口里，恰好 6 个带 `{ user: true }`（用户主动：
     startAgent / termNew / termResume / lpStart(本机项目页, v0.13.30) /
     ghStart(GitHub 项目页, v0.13.31) / 芯片包装器 termOpenChip），
     2 个刻意不带（自动挂载 / 退避重连）—— 数量与身份都钉住；
  3. 全局 keydown 里 `if (editing) return` 必须出现在 `/` 与 Ctrl+K 分支之前；
  4. 内联 onclick 走的是带 user:true 的包装器 termOpenChip，不是裸 termConnect。

行为层（真 DOM 里 keyTargetIsEditing 认不认得 contenteditable / .xterm 子孙）
在 chromium 探针 tests/verify_key_focus_guard.py 里取证，两层互补。
"""
import re
import unittest
from pathlib import Path

HUB = Path(__file__).resolve().parents[1] / "static" / "hub.js"
SRC = HUB.read_text(encoding="utf-8")


def _lines(pattern):
    return [(n, l.strip()) for n, l in enumerate(SRC.splitlines(), 1) if re.search(pattern, l)]


class TestFocusPolicy(unittest.TestCase):
    def test_only_guarded_term_focus(self):
        """所有 term.focus() 必须被 termFocusWanted 罩着，且在同一行。"""
        hits = _lines(r"(?<!\w)term\.focus\(\)")
        self.assertGreaterEqual(len(hits), 1, "一个 term.focus() 都没有＝功能被删了")
        for n, line in hits:
            with self.subTest(line=n):
                # 认谓词本身，不认入参写法：查找框关闭后的回焦是「用户按了 Ctrl+F」这条
                # 主动路径，写成 termFocusWanted({ user: true }) 才是如实表达，
                # 硬要求 `termFocusWanted(opts)` 只会逼人去造一个没用的 opts 变量。
                self.assertRegex(line, r"termFocusWanted\(",
                                 f"第 {n} 行有无守卫的 term.focus()：{line[:80]}")

    def test_predicate_semantics(self):
        """termFocusWanted 只认 opts.user，别的入参（含 reconnect）一律不抢焦点。"""
        m = re.search(r"function termFocusWanted\(opts\) \{ return (!+|\S+?)\(opts && opts\.user\);", SRC)
        self.assertIsNotNone(m, "termFocusWanted 实现被改写，请同步本护栏")
        self.assertNotIn("reconnect", SRC.split("function termFocusWanted")[1].split("\n")[0],
                         "重连不该参与焦点判断")

    def test_user_entry_points_count(self):
        """6 个用户主动入口带 user:true；2 个自动路径不带。钉数量也钉身份。

        v0.13.31 起 user:true 的直接调用点为 5 处（startAgent / termNew /
        termResume / lpStart / ghStart）+ 芯片包装器 termOpenChip；lpStart/ghStart
        是两个项目页的「新建会话」按钮——用户主动点击，必须抢焦点（否则新起的
        会话黑屏无焦点）。"""
        # 包装器 termOpenChip 的定义行本身也是 `termConnect(sid, agent, { user: true })`：
        # 它是字面上的第 6 处匹配，但不是调用点。第一版就在这条上红，别把测试 bug 记成产品 bug。
        user = [l for _, l in _lines(r"termConnect\([^)]*\{ user: true \}\)") if "function " not in l]
        self.assertEqual(len(user), 5, f"直接调用点应恰好 5 处带 user:true，实得 {len(user)}")
        chip = _lines(r"function termOpenChip\(sid, agent\) \{ termConnect\(sid, agent, \{ user: true \}\); \}")
        self.assertEqual(len(chip), 1, "芯片包装器丢了（内联 onclick 会退化成无守卫直连）")
        # 自动路径：重连 + 自动挂载，必须**不**带 user
        auto = [l for _, l in _lines(r"termConnect\(")
                if ("{ reconnect: true }" in l or re.search(r"termConnect\(last, chatPick\);", l))]
        self.assertEqual(len(auto), 2, f"自动路径应恰好 2 处且不带 user:true，实得 {auto}")
        for l in auto:
            self.assertNotIn("user: true", l, f"自动挂载/重连被加了 user:true＝手机又开始弹键盘：{l[:80]}")

    def test_inline_onclick_uses_wrapper(self):
        """内联 onclick 只能走 termOpenChip，不许出现裸 termConnect。"""
        oc = _lines(r"onclick=\\?\"?termConnect\(")
        self.assertEqual(len(oc), 0, f"内联 onclick 直连 termConnect 会绕过 user 标记：{oc}")
        self.assertEqual(len(_lines(r"onclick=.*termOpenChip\(")), 1, "芯片 onclick 应指向 termOpenChip")


class TestKeyGuard(unittest.TestCase):
    def setUp(self):
        # 不能取「第一个」keydown 监听器：全仓已有多个（v0.13.5x 起终端内查找 Ctrl+F
        # 也注册了一个，且它所在的分片排序在前 ⇒ `SRC.find` 会先命中它，
        # 拿到的 body 里自然没有 `if (editing) return`，护栏就假红了 —— 实测踩到）。
        # 判据改成**身份**：认那个调用 keyTargetIsEditing 的焦点策略监听器。
        self.body = ""
        for m in re.finditer(r"document\.addEventListener\('keydown'", SRC):
            tail = SRC[m.start():]
            # 先切到「下一处 keydown 监听」之前，再找收尾 —— 否则捕获阶段那个
            # 以 `}, true);` 结尾的监听器（v0.13.186 的 ⌘K）会被一起吞进来，
            # 于是"Ctrl+K 不许出现在受限监听器里"这条判据永远为假红。
            nxt = tail.find("document.addEventListener", 10)
            if nxt > 0:
                tail = tail[:nxt]
            body = tail                      # 到这里只剩一个监听器
            if "keyTargetIsEditing" in body:
                self.body = body
                break
        self.assertNotEqual(self.body, "", "带 keyTargetIsEditing 的全局 keydown 处理器找不到了")

    def test_predicate_defined(self):
        self.assertIn("function keyTargetIsEditing(e)", SRC, "守卫谓词被删")
        for pat in (r"closest\('\.xterm'\)", r"'INPUT'", r"'TEXTAREA'", r"'SELECT'",
                    r"isContentEditable"):
            self.assertRegex(SRC, pat, f"keyTargetIsEditing 少了 {pat} 这一类输入位的识别")

    def test_editing_return_before_slash_but_not_ctrl_k(self):
        """守卫只挡 `/`；⌘/Ctrl+K 自 2026-11-01 起在捕获阶段全局生效，不再受它约束。

        改前本用例断言「Ctrl+K 必须排在守卫之后（让给 pty）」。用户当天报障
        「Ctrl+K 在会话页有时唤不起」并裁定「应该全局唤起，任何页面都可以」——
        焦点在 #chatInput 时正是被这道守卫挡住的。契约随之改变，判据同步改判，
        但**另一半没变**：`/` 仍必须被守卫挡住（终端里 `/` 是路径分隔符，一天几百次）。
        """
        gate = re.search(r"if \(editing\) return", self.body)
        self.assertIsNotNone(gate, "编辑器守卫没接进监听器")
        j = re.search(r"e\.key === '/'", self.body)
        self.assertIsNotNone(j, "/ 分支不见了")
        self.assertLess(gate.end(), j.start(), "/ 分支排在守卫之前＝仍然劫持终端按键")
        # Ctrl+K 若回到这个受限监听器里，会话页又会「有时能开有时不能开」
        self.assertNotIn("=== 'k'", self.body,
                         "⌘/Ctrl+K 又回到受 editing 守卫约束的监听器里了")

    def test_escape_defers_to_pty_except_search_box(self):
        """Esc 在终端里必须原样给 pty（vim 要用），只有搜索框自己认领清空。

        P1-21 改写过这段实现（editing 早退挪到了抽屉/命令面板判定之后），
        但**契约没变**：终端里的 Esc 仍归 pty。原先这里匹配的是一条字面量
        `if (editing && !(e.target.id==='navSearch')) return`，实现改成更严格的
        `.xterm` 显式早退后字面量失配——闸门照旧被改成行为断言，不再绑写法。
        """
        guard = re.search(r"e\.target\.closest\('\.xterm'\)\) return", self.body)
        self.assertIsNotNone(guard, "Esc 分支不再把终端让给 pty＝vim 的 Esc 会被界面吃掉")
        self.assertLess(guard.start(), re.search(r"editing\) return", self.body).start(),
                        "editing 早退排在终端早退之前＝终端 Esc 会被界面吃掉")


if __name__ == "__main__":
    unittest.main(verbosity=2)
