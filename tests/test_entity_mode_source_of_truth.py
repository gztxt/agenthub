#!/usr/bin/env python3
"""L0 闸门：工作台形态只能有**一处**优先级，偏好键只能有一个，且默认推导不许写盘。

为什么钉这三条（2026-09-25 用户报障「菜单栏点 Claude Code 进不去终端页」）：
1. `openEntity()` 与 `defaultModeOf()` 曾各写一份 `embed > term > chat`。claude 是全仓唯一
   同时有 embed（cloudcli 宿主端口活 ⇒ discovery 注入）与 term 的实体 ⇒ 两份优先级只在它身上
   分叉出"必进 iframe"。两份同源判据放在一起，漂移只是时间问题。
2. 偏好键 `hub.chatmode.<id>` 曾被 `gotoChat()` 无条件写入 —— 连**推导出来的**默认形态也写，
   于是默认被固化成假偏好；后来只改默认值救不回存量浏览器（⇒ 本次被迫换键 `hub.chatmode2.`）。

判据全部走 AST/文本，不 import `src.main`、不起服务、不碰网络（L0 口径）。
红基线：同一份判据打在修复前的 `fa14a0d` 字节上必须判红（`test_red_baseline_from_git`）。
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HUB = ROOT / "static" / "hub.js"
RED_SHA = "fa14a0d"      # 修复前最后一个生产 hub.js（换键/单一真源都不成立）

from _js_min import strip_comments   # noqa: E402


def _body(src, name):
    """取出顶层 `function name(...) {…}` 的函数体（按大括号配平，注释已去）。"""
    m = re.search(r"^function %s\s*\([^)]*\)\s*\{" % re.escape(name), src, re.M)
    if not m:
        return None
    i, depth = m.end() - 1, 0
    for j in range(i, len(src)):
        if src[j] == "{":
            depth += 1
        elif src[j] == "}":
            depth -= 1
            if depth == 0:
                return src[i + 1:j]
    return None


def hub_bytes(rev=RED_SHA):
    r = subprocess.run(["git", "-C", str(ROOT), "show", "%s:static/hub.js" % rev],
                       capture_output=True)
    return r.stdout.decode("utf-8", "replace") if r.returncode == 0 else None


class ModeSourceOfTruth(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = strip_comments(HUB.read_text(encoding="utf-8"))

    def test_default_mode_is_single_source(self):
        d = _body(self.src, "defaultModeOf")
        self.assertIsNotNone(d, "defaultModeOf 不见了")
        self.assertRegex(d, r"kind\s*===\s*'agent'[\s\S]*has\('term'\)",
                         "有原生终端的 Agent 必须先给终端页")
        self.assertRegex(d, r"has\('embed'\)", "无终端的实体仍要能进嵌入页")

    def test_embed_first_requires_embed_entry(self):
        """EMBED_FIRST 必须同时要求 embed entry 存在（v0.13.151）。

        改前它无条件返回 'embed'，而 applyChatMode 的「形态不可用回落默认」又会
        再问一次 defaultModeOf ⇒ embed entry 缺失时两步都落 embed，用户点开的
        是一张 url 为空的空白嵌页且没有按钮能出去（2026-10-25「dsh的原生界面
        按钮也没有了」同源故障的第二道闸）。破坏方向：去掉 `&& has('embed')`
        ⇒ 本断言红。"""
        d = _body(self.src, "defaultModeOf")
        self.assertIsNotNone(d, "defaultModeOf 不见了")
        self.assertRegex(d, r"EMBED_FIRST\.has\(a\.id\)\s*&&\s*has\('embed'\)",
                         "EMBED_FIRST 分支必须带 has('embed') 前置，否则 embed entry "
                         "缺失时会落空白嵌页（applyChatMode 回落也会再问到这里）")

    def test_open_entity_does_not_reimplement_priority(self):
        o = _body(self.src, "openEntity")
        self.assertIsNotNone(o, "openEntity 不见了")
        calls = re.findall(r"gotoChat\(([^)]*)\)", o)
        self.assertTrue(calls, "openEntity 必须经 gotoChat 进工作台")
        # 带第二个参数＝在这里另挑形态 ⇒ 与 defaultModeOf 形成两份优先级（本次故障本体）。
        # 不带参数时 gotoChat 自己按「显式偏好 → defaultModeOf」解析，判据只有一处。
        for c in calls:
            self.assertNotIn(",", c,
                             "openEntity 不得指定形态（形态只由 defaultModeOf 决定）：%s" % c)
        self.assertNotRegex(o, r"if\s*\(has\('embed'\)\)\s*return\s+gotoChat",
                            "改前那种 embed 优先链不得复现")

    def test_preference_key_is_the_new_one_only(self):
        self.assertIn("hub.chatmode2.", self.src, "偏好键必须是 hub.chatmode2.*")
        # 旧键只允许出现在注释里（strip_comments 已去注释 ⇒ 命中即代码仍在读写旧键）
        self.assertNotIn("hub.chatmode.", self.src, "旧偏好键不得再被代码引用")

    def test_only_explicit_choice_persists(self):
        g = _body(self.src, "gotoChat")
        self.assertRegex(g, r"if\s*\(\s*mode\s*\)\s*lsSet\(",
                         "gotoChat 只能在调用方点名形态时写盘")
        a = _body(self.src, "applyChatMode")
        self.assertNotIn("lsSet(", a, "applyChatMode 的回落是推导，一律不得写盘")

    def test_mode_switch_buttons_exist_and_are_gated(self):
        html = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
        for bid in ("embedToTerm", "termToEmbed"):
            self.assertIn('id="%s"' % bid, html, "%s 缺失：embed/term 必须互留出口" % bid)
        a = _body(self.src, "applyChatMode")
        for bid in ("toTerm", "toEmbed"):
            self.assertRegex(a, bid + r"[\s\S]{0,120}style\.display",
                             "%s 必须按该实体有无对应 entry 显隐（零尺寸按钮=坏入口）" % bid)


class RedBaselineFromGit(unittest.TestCase):
    """红对照：同一份判据打在修复前的字节上必须判红，否则上面的断言是空转。"""

    def test_red_baseline_fails_the_new_gates(self):
        old = hub_bytes()
        self.assertIsNotNone(old, "取不到 %s 的 hub.js，红对照无法成立" % RED_SHA)
        src = strip_comments(old)
        o = _body(src, "openEntity") or ""
        self.assertRegex(o, r"gotoChat\(\s*id\s*,\s*'embed'\s*\)",
                         "改前 openEntity 应自带 embed>term 优先级（红基线失效说明判据在自证）")
        self.assertNotIn("hub.chatmode2.", src, "改前不得已有新偏好键")
        self.assertIn("hub.chatmode.", src, "改前代码仍在读写旧偏好键")


if __name__ == "__main__":
    unittest.main(verbosity=2)
