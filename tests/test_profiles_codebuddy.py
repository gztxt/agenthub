"""L0：CodeBuddy Code / Pi 两张卡——原生界面 + 终端入口并存（本机项目页候选框口径）。

三次定案（2026-09-27，全部由用户报障驱动，不是假想）：
1. **入口必须是原生界面**（v0.13.37）：`codebuddy --serve` 自带 Web 遥控端 :35431，
   卡片按服务型画像走（`ui` 字符串 ⇒ 「嵌入会话 / 新窗口」）。
2. **终端入口不能顺手加在没 CLI 的东西上**（同一版）：形态优先级 `embed > term > chat`
   （`02-nav-and-poll.js:defaultModeOf`，单一真源），加 terminal 会顶掉默认 embed。
   ⇒ 但**有真 CLI 的 Agent 就必须加**：v0.13.38 用户报「本机项目/GitHub 项目的 agent
   候选框少了 pi / codebuddy」，根因是候选框只收 `entries` 含 `term` 的卡片
   （`09-local-projects.js:165` / `10-github-projects.js:205`）—— 不是会话起不来。
   pi（v0.85.1，默认交互 TUI）与 codebuddy（2.137.1，WorkBuddy 包内）都有 CLI ⇒ 补
   terminal 后即可在选中项目目录拉起会话；qwenpaw 无 CLI，仍只有原生界面。
3. **codebuddy 的 cmd 必须是绝对路径**：二进制只在 WorkBuddy 包内、不在 PATH，
   `which("codebuddy")` 落空 ⇒ term.py `which(cmd[0])` 会 400「命令未在本机找到」。
4. **2026-11-09 换装**：CLI 升级到 2.181.0（官方 native 安装 `~/.local/bin`），cmd 随
   `CODEBUDDY_CLI` 走新路径；包内 2.137.1 只服务桌面端与 :35431 原生界面。

本文件属 L0 hermetic：只读画像表与判定函数，**零网络**（不探测 35431 是否真活着）。
"""
import os
import re
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import profiles  # noqa: E402

try:
    import tiers
except ImportError:  # 单文件直跑时降级：不强制
    tiers = None

PORT = 35431


def _card(pid: str) -> dict:
    for p in profiles.all_profiles(include_blocked=True):
        if p.get("id") == pid:
            return p
    raise AssertionError("profiles 里没有 %s 卡" % pid)


def _lp_candidates() -> list:
    """本机项目/GitHub 项目页 agent 候选框的口径（与前端过滤器同式）。"""
    return [a["id"] for a in profiles.all_profiles(include_blocked=True)
            if a["kind"] == "agent"
            and (profiles.entries_for(a, "running") and
                 any(e["type"] == "term" for e in profiles.entries_for(a, "running")))]


class TestCodebuddyNativeCard(unittest.TestCase):
    """① 卡片形态：原生界面仍在，且终端入口用绝对路径"""

    def test_card_shape(self):
        c = _card("codebuddy")
        self.assertEqual(c["kind"], "agent")
        self.assertEqual(c["port"], PORT)
        self.assertIsNone(c.get("cli"))          # 包内二进制，不在 PATH

    def test_ui_is_loopback_for_frontend_rewrite(self):
        """UI 必须写 loopback，由前端按访问主机名改写（LAN IP 写死会跨网失效）"""
        c = _card("codebuddy")
        self.assertEqual(c["ui"], "http://127.0.0.1:%d" % PORT)

    def test_terminal_cmd_is_absolute_and_resolvable(self):
        """cmd 必须是绝对路径且 which() 能解析；2026-11-09 起默认指向 ~/.local/bin 的 2.181.0"""
        c = _card("codebuddy")
        cmd = (c.get("terminal") or {}).get("cmd")
        self.assertTrue(cmd and cmd.startswith("/"), cmd)
        self.assertEqual(profiles.which(cmd), cmd)
        local = os.path.expanduser("~/.local/bin/codebuddy")
        if os.access(local, os.X_OK):
            self.assertEqual(cmd, local)   # 装了新版就必须用它，不得静默回落包内 2.137.1

    def test_entries_native_ui_first_then_term(self):
        """embed 必须排在 term 之前：形态优先级 embed>term>chat，原生界面是默认"""
        c = _card("codebuddy")
        types = [e["type"] for e in profiles.entries_for(c, "running")]
        self.assertIn("embed", types)
        self.assertIn("term", types)
        self.assertLess(types.index("embed"), types.index("term"))
        self.assertFalse(c.get("frame_deny"))              # :35431 无 X-Frame-Options

    def test_detect_matches_serve_cmdline(self):
        """`node /opt/WorkBuddy/.../codebuddy --serve --port 35431 --host 0.0.0.0`"""
        procs = [{"comm": "node",
                  "cmdline": ("node /opt/WorkBuddy/resources/app.asar.unpacked/"
                              "cli/bin/codebuddy --serve --port 35431 --host 0.0.0.0")}]
        self.assertEqual(profiles.detect_status(_card("codebuddy"), procs, set(), {}),
                         "running")


class TestProjectPageCandidates(unittest.TestCase):
    """② 两项目页候选框：有 CLI 的 Web 型 Agent 必须在列，无 CLI 的不在"""

    def test_pi_and_codebuddy_are_selectable(self):
        ids = _lp_candidates()
        self.assertIn("pi", ids)
        self.assertIn("codebuddy", ids)

    @tiers.host_only
    def test_pi_terminal_cmd_resolves(self):
        # 依赖真实宿主：profiles.which() 兜底链走 Path.home()/".npm-global/bin"，
        # 而 pi 不在服务进程的 PATH 上 ⇒ 假 HOME（hermetic-clean）下必然 None。
        # 2026-10-02：由 A4 推送前的既存红修复，与 A4 无关，改判为 L1 真跑而非跳过。
        p = _card("pi")
        cmd = (p.get("terminal") or {}).get("cmd")
        self.assertEqual(cmd, "pi")
        self.assertIsNotNone(profiles.which(cmd))

    def test_qwenpaw_has_no_terminal_so_stays_out(self):
        """qwenpaw 无 CLI（which 落空）⇒ 只能进原生界面，候选框里不该出现"""
        q = _card("qwenpaw")
        self.assertIsNone(q.get("terminal"))
        self.assertNotIn("qwenpaw", _lp_candidates())


if __name__ == "__main__":
    unittest.main()
