#!/usr/bin/env python3
"""口令桥闸门（B 方案）：hub 子任务 → DS Bot 可粘贴文本。

为什么单独一层：
1. 方向必须是 hub→人→DS Bot，**不能自动 send**：`send` 是不可重试的 mutation
   （ds-harness-remote `protocol.md` §16.4），超时重发 = 重复派活。
2. 文本里不许出现 bot 名：本机未装 ds-bot，团队不存在 ⇒ 写死名字就是假信息。
3. 子任务正文要取 `prompt`（/api/tasks/{run} 的 SELECT * 里就有），
   取不到时必须显式说明，不许渲染成 `undefined`。

L0 静态：只读 shard / hub.js / 模板的真文本。
L1 host：真跑 node，执行从 hub.js 原样抽出的 handoffText。
"""
import pathlib
import re
import shutil
import subprocess
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))

from _hub_extract import extract_function, read_hub   # noqa: E402
import tiers                                          # noqa: E402

SHARD = _REPO / "static" / "hub" / "04-terminal-ws.js"
TPL = _REPO / "templates" / "index.html"


class TestHandoffStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = SHARD.read_text(encoding="utf-8")
        cls.hub = read_hub()
        cls.html = TPL.read_text(encoding="utf-8")

    def test_hubjs_has_functions(self):
        """忘记跑 build_hubjs.sh 时这里红（同 test_asset_panel 的口径）。"""
        for fn in ("handoffText", "copyHandoff", "copyAllHandoff", "bindHandoff"):
            self.assertIsNotNone(extract_function(self.hub, fn),
                                 "hub.js 缺 %s ⇒ 跑 scripts/build_hubjs.sh" % fn)

    def test_row_button_uses_data_attr(self):
        fn = extract_function(self.js, "renderTaskTable")
        self.assertIn('data-bb-copy=', fn, "行内按钮没挂 data-bb-copy ⇒ 委托收不到")
        self.assertNotIn('onclick="copyHandoff', fn, "行内禁用 inline onclick（浮层配套红线）")

    def test_delegation_binds_once_after_null_check(self):
        fn = extract_function(self.js, "bindHandoff")
        self.assertIsNotNone(fn)
        # 先判空再置标志：页没渲染就置 true ⇒ 之后再也不会绑定（点按钮永远没反应）
        self.assertLess(fn.index("if (!body) return"), fn.index("handoffBound = true"),
                        "标志位必须晚于空检查")
        self.assertIn("closest('[data-bb-copy]')", fn.replace('"', "'"))

    def test_no_auto_send_claim(self):
        fn = extract_function(self.js, "handoffText")
        self.assertIn("hub 不会代发", fn, "必须明说不会自动发送，否则用户以为点了就派出去了")
        self.assertNotIn("api('/api/bot", self.js, "禁止越权直调 api/bot")

    def test_template_header_and_button(self):
        i = self.html.index('id="page-tasks"')
        seg = self.html[i:i + 6000]
        self.assertIn("<th>操作</th>", seg, "表头没加列 ⇒ 行内按钮会错位到最后一列外")
        self.assertIn("copyAllHandoff()", seg, "缺「复制全部派活文本」入口")
        self.assertIn('id="i-copy"', self.html, "图标不在 sprite 里 ⇒ 按钮空白")


@tiers.host_only
class TestHandoffTextNode(unittest.TestCase):
    """L1：真跑 node，跑的是 hub.js 里的原文（不手抄）。"""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest(tiers.HOST_SKIP_REASON)
        fn = extract_function(read_hub(), "handoffText")
        if fn is None:
            raise AssertionError("hub.js 抽不到 handoffText（或被改名）⇒ 判据无法针对真代码执行")
        cls.src = fn

    def _run(self, body: str) -> str:
        js = ("let currentRun = 'r1';\nlet currentRunGoal = 'G0';\nlet currentRunTasks = {};\n"
              + self.src + "\n" + body)
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=20)
        self.assertEqual(p.returncode, 0, p.stderr[:800])
        return p.stdout

    def test_full_text(self):
        out = self._run("console.log(handoffText({task_id:'t1',prompt:'P1',deps:['t0']}))")
        for want in ("run r1 / t1", "总目标：G0", "子任务：P1", "上游：t0", "验收："):
            self.assertIn(want, out, "缺字段：%s" % want)

    def test_missing_prompt_is_explained_not_undefined(self):
        out = self._run("console.log(handoffText({task_id:'t2'}))")
        self.assertNotIn("undefined", out, "空 prompt 渲染成 undefined = 把缺陷交给用户")
        self.assertIn("没留存 prompt", out)

    def test_no_deps_means_no_upstream_line(self):
        out = self._run("console.log(handoffText({task_id:'t3',prompt:'P'}))")
        self.assertNotIn("上游：", out)


if __name__ == "__main__":
    unittest.main()
