"""dsh 原生界面卡（2026-10-23）

为什么要有这张静态卡
--------------------
dsh 原来只在 `CLI_ALIASES` 里（B 档动态发现），而动态卡硬编码 `"ui": None`
⇒ 菜单上只有「终端」。用户想在浏览器里用 dsh 自带的 Web UI，就只能在 hub 的
嵌入式终端里手敲 `dsh web`；敲第二次时 3080 已被第一次占着，整条命令随
`webserver` 插件 EADDRINUSE 退出（2026-10-23 实测）。

三条不变量
----------
① 静态卡必须存在且 `ui` 是字符串 —— 否则 `entries_for()` 不会出 embed/open；
② 动态发现不得再出一张 dsh 卡（`_dynamic_cli_agents` 的 covered 集合含静态 id）；
③ token 不得写进画像 —— 它是进程级随机（browser-auth.ts 的 randomBytes(32)），
   写死既是「凭据落配置」，也会在每次 restart 后立刻失效。

2026-10-24 第四条（见判例 253，已被同日 01:15 改判）
--------------------------------------------------
原先是「加了 Web UI 也不能丢终端」，随后改成**必须没有终端**：dsh 没有 CLI 交互会话
（`~/.dsh/profiles` 只有 web/headless，裸 `dsh` 经启动器 `case "${1:-web}"` 直接变
`dsh web`）⇒ 每次"新会话"都在后台多起一个 Web 服务器、抢端口并争写 `~/.dsh`。

2026-10-24 01:15 第五条（用户第三次报障「调研显示可以使用终端」后反转回来）
------------------------------------------------------------------
「没有 CLI 交互会话」只对了半句：DSH **本体**不带终端前端，但终端前端是**外置插件**，
装上 `@tomowang/dsh-tui`（MIT，peerDeps 钉 ^0.2.1-rc.2）后 `dsh --profile tui` 就是
全屏 TUI。2026-10-24 01:14 实测：PTY 内起界面 → bash 工具跑 `date` 回值；
01:17 再在 hub 的 xterm 里起同一命令，输入 `/status` 有回显与工具调用。
⇒ terminal 恢复，但**命令必须钉死** `--profile tui`：裸 `dsh` 仍会变成 `dsh web`
（判例 252 §四.2 的端口/争写风险只对裸 dsh 成立）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import profiles  # noqa: E402


class DshProfileTest(unittest.TestCase):
    def test_static_card_exists(self):
        p = profiles.get_profile("dsh")
        self.assertIsNotNone(p, "dsh 必须有画像卡")
        self.assertEqual(p.get("port"), 3080, "端口必须与 dsh.service 一致")

    def test_ui_is_string(self):
        p = profiles.get_profile("dsh")
        self.assertIsInstance(
            p.get("ui"), str,
            "ui 必须是字符串：entries_for 只对 str 形态出 embed/open，dict 形态要 discovery 另处理",
        )
        self.assertNotIn("token=", p.get("ui", ""), "token 不得写进画像")

    def test_no_duplicate_card(self):
        static = [p["id"] for p in profiles.PROFILES]
        self.assertEqual(static.count("dsh"), 1, "PROFILES 里只应有 1 张 dsh 卡")
        dyn = [p["id"] for p in profiles._dynamic_cli_agents(gate=False)]
        self.assertNotIn("dsh", dyn, "动态发现必须跳过已收录的 dsh（否则菜单两张卡）")

    def test_entries_expose_web_and_term(self):
        """Web 入口 + 终端入口都要在（2026-10-24 01:15 改判，见文件头第五条）。

        旧断言是「term 必须不在」；改判依据：dsh 本体不带终端前端，但外置插件
        @tomowang/dsh-tui 提供全屏 TUI，`dsh --profile tui` 已在 PTY 与 hub xterm 双实测。
        """
        p = profiles.get_profile("dsh")
        types = [e["type"] for e in profiles.entries_for(p, "running")]
        self.assertIn("embed", types, "应给出嵌入会话入口")
        self.assertIn("open", types, "应给出新窗口入口")
        self.assertIn("term", types, "dsh 的终端形态来自 dsh --profile tui，必须给")

    def test_terminal_cmd_pins_tui_profile(self):
        """画像层钉死命令：必须是 `dsh --profile tui`，绝不能退化成裸 `dsh`。

        裸 `dsh` 经启动器 `case "${1:-web}"` 变成 `dsh web` ⇒ 再起一个 Web 服务器，
        抢 3080 之后的端口并争写 `~/.dsh`（判例 252 §四.2）。
        """
        p = profiles.get_profile("dsh")
        term = p.get("terminal") or {}
        cmd = term.get("cmd", "")
        self.assertIn("--profile tui", cmd, "dsh 的终端命令必须是 `dsh --profile tui`")
        self.assertNotRegex(cmd, r"(^|/)dsh\s*$", "不得裸 dsh（会变成 dsh web）")
        self.assertEqual(term.get("cwd"), "/fs/1000/ftp/技术文档")
        # cli 仍留着：detect / vitals 靠它判活，去掉会让卡片降级成 unknown
        self.assertTrue(p.get("cli"), "cli 不能一起删——判活要用")

    def test_token_failure_degrades_to_bare_url(self):
        """取不到 token 时退回裸地址：不抛异常、url 不为空（打开会 401，属明确降级）。"""
        p = profiles.get_profile("dsh")
        orig = profiles._dsh_web_url
        try:
            profiles._dsh_web_url = lambda: None
            es = [e for e in profiles.entries_for(p, "running") if e["type"] in ("embed", "open")]
            self.assertEqual(len(es), 2)
            self.assertTrue(all(e["url"] for e in es), "降级后仍须给出裸地址，不能是空串")
        finally:
            profiles._dsh_web_url = orig


if __name__ == "__main__":
    unittest.main()
