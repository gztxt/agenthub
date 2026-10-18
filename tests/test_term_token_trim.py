"""L0/L1 · 终端 token 两端不一致：**存量带空白的 token 会让终端永久空白**（2026-10-18）。

【病灶，实弹取证不是推断】
同一个 token 在两条鉴权路径上待遇不同：

  · HTTP：`X-TERM-TOKEN: <43字符> ` ⇒ **200**（HTTP 解析器按 OWS 把尾随空白裁掉）
  · WS  ：`/ws/term/<sid>?token=<43字符>%20` ⇒ **403**

于是浏览器 localStorage 里一份带尾随空白的 token（`prompt()` 粘贴带入）表现为：
`POST /api/term/sessions` **200**（pty 起得来、`codebuddy --version` 正常渲染）⇒
`WebSocket /ws/term/<sid>` **403** ⇒ xterm 一个字节都收不到 ⇒ 终端永久空白。
journald 实测：干净 token 26 次、带 `%20` 10 次；`/api/term/sessions` 建库有、`sessions` 空。

【为什么不许在服务端修】
`term.py::_check_term_token` / `term_ws` 用 `hmac.compare_digest` **精确**比对是对的 ——
放宽成 `strip()` 等于允许「token + 任意尾随空白」通过，是往鉴权闸上开口子。
脏值只能由**产出侧**（浏览器）在读写时自愈，故闸门钉在 `static/hub.js` 的三处 trim。

【闸门两层】
  · 静态层：三处 trim 必须还在（后人删掉时，功能照旧"大部分能用" ⇒ 只有闸门会红）；
  · 行为层：抽真代码进 node 跑，存量脏值自愈、prompt 写入自愈、HTTP 侧语义不变。
"""
import json
import shutil
import subprocess
import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "tests"))
from _hub_extract import extract_function  # noqa: E402

HUB = ROOT / "static" / "hub.js"
SRC = HUB.read_text(encoding="utf-8")
HAVE_NODE = bool(shutil.which("node"))


def _fn(name):
    body = extract_function(SRC, name)
    assert body, "static/hub.js 抽不到 %s（改名/删除了？那这条判据要同步改）" % name
    return body


class TestTrimSitesPresent(unittest.TestCase):
    """静态层：trim 不许被后人顺手删掉（删了功能"大部分还能用"，只有本闸门会红）。"""

    def test_ws_url_trims_stored_token(self):
        body = _fn("wsUrl")
        self.assertIn(".trim()", body,
                      "wsUrl 读 localStorage 时必须 trim —— 否则存量带空白的 token 让 WS 永久 403")
        self.assertIn("encodeURIComponent", body)

    def test_term_token_trims_read_and_prompt(self):
        body = _fn("termToken")
        self.assertGreaterEqual(body.count(".trim()"), 2,
                                "termToken 读存量 + prompt 返回值两处都要 trim（少一处就有一条路漏脏值）")

    def test_settings_apply_trims(self):
        body = SRC.split("function settingsApplyToken", 1)[1].split("\n}", 1)[0]
        self.assertIn(".trim()", body,
                      "「应用到终端」是写入口，不 trim 会把脏值重新写回 localStorage")


@unittest.skipUnless(HAVE_NODE, "SKIP(node-missing): 行为层要真 node 跑抽出来的真代码")
class TestTrimBehaviour(unittest.TestCase):
    """L1：拿 node 跑**抽出**的 termToken/wsUrl（手抄一份实现等于假绿）。"""

    def _run(self, stored, prompt_return="", call="termToken"):
        js = """
        var __store = {};
        function lsGet(k){ return Object.prototype.hasOwnProperty.call(__store, k) ? __store[k] : null; }
        function lsSet(k, v){ __store[k] = v; }
        function prompt(){ return %s; }
        var location = { protocol: 'http:', host: '127.0.0.1:3102' };
        if (%s !== null) __store['hub.term.token'] = %s;
        %s
        process.stdout.write(JSON.stringify({
          value: %s(),
          stored: Object.prototype.hasOwnProperty.call(__store, 'hub.term.token')
                    ? __store['hub.term.token'] : null,
          url: wsUrl('/ws/term/abc')
        }));
        """ % (json.dumps(prompt_return), json.dumps(stored), json.dumps(stored),
               "\n".join([_fn("termToken"), _fn("wsUrl")]), call)
        r = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(r.returncode, 0, "node 报错：" + r.stderr.strip()[:400])
        return json.loads(r.stdout.strip())

    def test_poisoned_storage_self_heals_on_read(self):
        """存量 `TOK ` ⇒ termToken 返干净值、localStorage 仍是脏值（读侧不写盘）。"""
        out = self._run("TOK ")
        self.assertEqual(out["value"], "TOK", "存量带空白的 token 未被 trim ⇒ WS 会 403")
        self.assertEqual(out["url"], "ws://127.0.0.1:3102/ws/term/abc?token=TOK",
                         "wsUrl 里仍带 %%20 ⇒ 浏览器侧握手必 403")
        self.assertEqual(out["stored"], "TOK ", "读侧不自愈写盘（避免每次读都写 localStorage）")

    def test_prompt_result_is_trimmed_on_write(self):
        """首次 prompt 粘到 " TOK " ⇒ 落盘必须是干净的。"""
        out = self._run(None, prompt_return=" TOK \n")
        self.assertEqual(out["value"], "TOK")
        self.assertEqual(out["stored"], "TOK", "prompt 写入未 trim ⇒ 脏值被固化进 localStorage")

    def test_clean_token_semantics_unchanged(self):
        out = self._run("TOK")
        self.assertEqual(out["value"], "TOK")
        self.assertEqual(out["url"], "ws://127.0.0.1:3102/ws/term/abc?token=TOK")


if __name__ == "__main__":
    unittest.main()
