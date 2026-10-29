"""L0 hermetic：差分行渲染的整屏 TUI 必须「连接后强制整屏重建」（PT-20261030-01）。

用户报障原句：「dsh 嵌入式终端页面 总感觉怪怪的…跟其他 agent 对比」。
根因一条：hub 恒用主屏（`?1049h` 吞掉 + term.clear，v0.13.83），而 pi-tui 的
TuiAltScreen 只重画**与上一帧不同的行** ⇒ 清过屏之后，没变化的行永远不会被重画，
换会话 / 断线重连 / 刷新页面就长期挂半屏空白。实测见
`static/hub/03-agents-cards.js` 里 `TERM_ALT_DIFF_AGENTS` 的注释与
`work/probe_dsh_enter_latency.py`（影子实例 + 真 chromium）。

为什么要静态锁：真浏览器复现要起 dsh 会话（贵、且会写 ~/.dsh），不在
`run_tests.sh` 的自动层里跑；回归形态又极其安静 —— 那个 `true` 参数被顺手删掉时
页面照样能显示，只有「重连后又是半屏」才现形。
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
SHARD = REPO / "static" / "hub" / "03-agents-cards.js"
BUILT = REPO / "static" / "hub.js"

_BLOCK_COMMENT = re.compile(r"/\*.*?\*/", re.S)
_LINE_COMMENT = re.compile(r"(?m)//[^\n]*")


def _strip_comments(s: str) -> str:
    return _LINE_COMMENT.sub("", _BLOCK_COMMENT.sub("", s))


def _fn_body(src: str, name: str) -> str:
    i = src.find("function %s(" % name)
    assert i >= 0, "函数 %s 不在（整屏重建被删？）" % name
    end = src.find("\n}\n", i)
    assert end >= 0, "%s 没有第 0 列的收尾 }" % name
    return src[i:end + 2]


class TestAltDiffAgentForceRepaint(unittest.TestCase):
    def setUp(self):
        self.code = _strip_comments(SHARD.read_text(encoding="utf-8"))

    def test_force_bypasses_blank_ratio(self):
        body = _fn_body(self.code, "termHealNow")
        # force 档必须跳过「空白率 < 2/3 就不打扰」那道闸，否则「看似有内容实为过期」被放过
        self.assertRegex(body, r"if\s*\(\s*!force\s*&&\s*blank\s*\*\s*3\s*<")
        self.assertRegex(body, r"termHealForce\s*=\s*false")

    def test_heal_blank_carries_force_flag(self):
        body = _fn_body(self.code, "termHealBlank")
        self.assertRegex(body, r"if\s*\(\s*force\s*\)\s*termHealForce\s*=\s*true")

    def test_replay_requests_force_for_alt_agent(self):
        body = _fn_body(self.code, "termConnect")
        # 回放那一帧：清过屏（!keepScreen）且该 agent 是差分行渲染的 TUI ⇒ 传 true
        self.assertRegex(
            body,
            r"termHealBlank\(\s*!keepScreen\s*&&\s*termAltAgent\(\s*termSidAgent\s*\)\s*\)",
        )

    def test_force_flag_reset_on_new_connection(self):
        body = _fn_body(self.code, "termConnect")
        self.assertIn("termHealForce = false", body)

    def test_only_diff_rendering_agents_listed(self):
        m = re.search(r"TERM_ALT_DIFF_AGENTS\s*=\s*\[([^\]]*)\]", self.code)
        self.assertIsNotNone(m, "TERM_ALT_DIFF_AGENTS 名单不在")
        names = re.findall(r"'([^']+)'", m.group(1))
        self.assertIn("dsh", names)
        # 这几个虽也发 ?1049h，但每帧整屏重画 / 走行内输出，不该被 resize 打扰
        for other in ("claude", "codex", "opencode", "shell"):
            self.assertNotIn(other, names)

    def test_built_bundle_contains_change(self):
        built = _strip_comments(BUILT.read_text(encoding="utf-8"))
        self.assertIn("TERM_ALT_DIFF_AGENTS", built)
        self.assertIn("termHealBlank(!keepScreen && termAltAgent(termSidAgent))", built)


if __name__ == "__main__":
    unittest.main()
