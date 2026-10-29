#!/usr/bin/env python3
"""L0：鼠标模式分层闸门（v0.13.84）。

锁形状，防「以后有人顺手把 wheel 改回一键看门狗」：

  R1  wheel 绑的是分层入口 termWheelNow，live（app 声明过鼠标）时先 return，
      绝不进复位。旧形状（wheel 直绑 termMouseResetNow）出现即红。
  R2  复位函数本体不许再写 termMouseLive —— live 只能由 want 表派生；
      「看门狗顺手置 live=false」正是 claude/opencode 翻不动的元凶。
  R3  mousedown 复位保留 + mouseup 回装必须在 **window bubble**（不是 capture）：
      capture 会抢在 xterm 自己的 mouseup 之前把跟踪态装回、清掉刚建立的选区
      （v0.13.84 第一版实测 B1 双红抓的）。
  R4  会话生命周期：新连接清 want；4404/4410 清 want 与缓存（防死轮）；
      回放信任与 want 缓存都只对非 shell 画像（v0.8.1 乱码教训保留）。
  R5  红基线自证：闸门必须在 v0.13.83 的旧字节上判红（从 git 取，不靠记忆）。

L0 铁律：不 import src.main、不联网、不起服务；读分片源码文本 + git 对象。
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PRE_TIER_SHA = "524972c"   # v0.13.83：分层修复的上一个版本，红基线从这里取


def _git(rev, path):
    r = subprocess.run(["git", "-C", str(REPO), "show", "%s:%s" % (rev, path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        raise AssertionError("取不到 %s:%s —— 红基线取不到，本闸门会退化成自证" % (rev[:7], path))
    return r.stdout


class TestMouseTier(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.s02 = (REPO / "static/hub/02-nav-and-poll.js").read_text(encoding="utf-8")
        cls.s03 = (REPO / "static/hub/03-agents-cards.js").read_text(encoding="utf-8")

    def test_R1_wheel_is_tiered_not_direct_reset(self):
        self.assertIn("el.addEventListener('wheel', termWheelNow", self.s03,
                      "wheel 必须绑分层入口 termWheelNow，不再直绑复位")
        self.assertNotIn("el.addEventListener('wheel', termMouseResetNow", self.s03,
                         "旧形状复活 = claude/opencode 的滚轮又被掐死（用户报障原文）")
        i = self.s03.index("function termWheelNow")
        body = self.s03[i:i + 400]
        # v0.13.92 改判：live 分支从「裸 return」变成「先按选区情况回装 termMouseArm()
        # 再 return」。判据的**意图不变** —— live 分支必须最先命中且不得落到
        # termMouseResetNow()（否则 claude/opencode 的应用内滚动又被掐死）。
        # 锁成结构：live 分支在前 + 分支体内 return + termMouseResetNow 只出现在 live 之后。
        self.assertRegex(body, r"if\s*\(\s*termMouseLive\s*\)\s*\{",
                         "live 必须是第一分支（应用内滚动的分流口）")
        live_br = body[body.index("if (termMouseLive)"):body.index("termMouseResetNow")]
        self.assertIn("return;", live_br,
                      "live 分支内必须 return，不能落到 termMouseResetNow()")

    def test_R2_reset_never_touches_live(self):
        i = self.s03.index("function termMouseResetNow")
        body = self.s03[i:self.s03.index("function termWheelNow")]
        self.assertNotIn("termMouseLive =", body,
                         "复位只动 xterm 解析态；live 由 02 的 want 扫描派生")
        j = self.s02.index("function termScanMouseMode")
        scan = self.s02[j:j + 900]
        self.assertIn("termMouseWant.add", scan)
        self.assertIn("termMouseWant.delete", scan)
        self.assertRegex(self.s02, r"termMouseLive\s*=\s*termMouseWant\.size\s*>\s*0")

    def test_R3_mousedown_keeps_reset_and_mouseup_arms_after_xterm(self):
        # 2026-10-30 改形状而非改语义：mousedown 绑的是**分道入口** termMouseDownNow。
        # 原断言钉死「mousedown 直绑 termMouseResetNow」，那条对 dsh 是致命的 ——
        # 它的按钮/列表/滚动条全靠 SGR 上报，press 被本地同步吞掉 ⇒ 点什么都没反应
        # （用户报障「dsh 终端比 codebuddy 差很远」的主体差异）。语义保留在分支里：
        # 非「差分整屏 TUI + app 已声明鼠标」的会话照旧走复位，拖选/不抢焦点不回退。
        self.assertIn("el.addEventListener('mousedown', termMouseDownNow", self.s03,
                      "mousedown 必须绑分道入口")
        self.assertIn("function termMouseDownNow", self.s03, "分道入口本体丢了")
        i = self.s03.index("function termMouseDownNow")
        body = self.s03[i:self.s03.index("function termMouseResetBind")]
        self.assertRegex(body, r"termMouseLive\s*&&\s*termAltAgent\(\s*termSidAgent\s*\)",
                         "放行条件必须是「app 实时声明鼠标 + 差分行整屏 TUI」，两个缺一不可")
        self.assertIn("termMouseResetNow()", body,
                      "其余分支仍要复位（v0.13.81 的拖选/不抢焦点语义不许回退）")
        self.assertNotIn("el.addEventListener('mousedown', termMouseResetNow", self.s03,
                         "mousedown 不许再直绑复位：那正是 dsh 点了没反应的根因")
        m = re.search(r"window\.addEventListener\('mouseup'[^)]*(\)[^;]*);", self.s03)
        self.assertIsNotNone(m, "必须有 window 级回装（选区常越出终端盒）")
        binding = m.group(0)
        self.assertIn("termMouseArm", binding)
        self.assertNotIn(", true)", binding,
                         "回装必须 bubble：capture 会抢在 xterm 之前装回跟踪态、清掉刚建立的选区")

    def test_R4_lifecycle_and_agent_scoped_trust(self):
        self.assertIn("termMouseWantReset();   // 新连接", self.s03,
                      "新连接必须连 want 一起清（只清 live 会被回放瞬间顶回）")
        self.assertRegex(
            self.s03,
            r"code === 4404 \|\| code === 4410\) \{[^}]*termWantCachePut\(termSid, null\);\s*termMouseWantReset\(\);",
            "会话结束必须清 want+缓存（防死轮）")
        self.assertRegex(
            self.s03,
            r"termSidAgent && termSidAgent !== 'shell'[\s\S]{0,600}termScanMouseMode\(termDecodeFrame\(raw\)\)",
            "非 shell 画像才信任回放帧（TUI 就是 pty 本体，尾巴即现势）")
        self.assertRegex(self.s03, r"\}\s*else \{\s*term\.write\(TERM_MOUSE_OFF\)",
                         "shell 画像维持 v0.8.1 口径：回放里的过期开关不算数")
        self.assertIn("termSidAgent !== 'shell'", self.s02, "want 缓存只写非 shell 会话")

    def test_R6_touch_scroll_reaches_alt_screen_apps(self):
        """触摸滑动在「无 scrollback 的整屏 TUI」上必须转成滚轮上报（2026-10-30）。

        dsh 这类差分渲染的 TUI，内容不进 scrollback（baseY 恒 0）⇒ 旧实现
        scrollToLine(0) 是空操作，手机上「怎么滑都不动」。判据两条：走对分支 +
        合成真实 WheelEvent（自己拼 `\\x1b[<64;…M` 就是第二份会漂的坐标/编码真相）。
        """
        s13 = (REPO / "static/hub/13-term-touch.js").read_text(encoding="utf-8")
        self.assertIn("function ttWheelToApp", s13, "整屏 TUI 的触摸转滚轮分支丢了")
        i = s13.index("function ttScrollByPx")
        body = s13[i:i + 900]
        self.assertRegex(body, r"termMouseLive\s*&&\s*termAltAgent\(\s*termSidAgent\s*\)"
                              r"\s*&&\s*ttWheelToApp\(", "分流条件不完整：必须限定整屏 TUI 且 app 要鼠标")
        self.assertIn("xterm-scrollable-element", s13,
                      "分发目标必须是 xterm 的滚动容器；派到 #termEl 只会冒泡向上，到不了监听器")
        self.assertIn("new WheelEvent(", s13, "必须合成真实滚轮事件，让 xterm 自己编码 SGR")
        self.assertNotIn("\\x1b[<64", s13, "不许手拼 SGR 滚轮上报（坐标系/编码会漂）")

    def test_R5_red_baseline_is_caught(self):
        old = _git(PRE_TIER_SHA, "static/hub/03-agents-cards.js")
        self.assertIn("el.addEventListener('wheel', termMouseResetNow", old,
                      "红基线取错了版本？v0.13.83 的 wheel 是直绑复位的")
        self.assertNotIn("function termWheelNow", old)
        self.assertNotIn("termMouseWant", _git(PRE_TIER_SHA, "static/hub/02-nav-and-poll.js"))
        self.assertRegex(old, r"function termMouseResetNow[\s\S]{0,400}termMouseLive = false;",
                         "红基线的复位必须带着「顺手置 live=false」这句原罪")


if __name__ == "__main__":
    unittest.main()
