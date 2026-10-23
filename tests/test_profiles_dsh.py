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
        p = profiles.get_profile("dsh")
        types = [e["type"] for e in profiles.entries_for(p, "running")]
        self.assertIn("embed", types, "应给出嵌入会话入口")
        self.assertIn("open", types, "应给出新窗口入口")
        self.assertIn("term", types, "终端入口不能因为加了 Web UI 而丢掉")

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
