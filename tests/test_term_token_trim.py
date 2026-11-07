"""L0/L1 · 浏览器不持有任何终端 token（2026-11-07 用户裁定后改判）。

【本文件的历史】原判据是「三处 trim 必须还在」——那时 token 由 localStorage 承载，
带尾随空白的存量值会让 `?token=%20` 的 WS 握手恒 403、终端永久空白。
2026-11-07 用户裁定「彻底删除 token 认证，全量用户名+密码」后，浏览器改走
HttpOnly 会话 Cookie：token 既不落 localStorage，也不进 URL，trim 问题整类消失。

【为什么不是删掉测试】
旧测试钉的是「脏值不外溢」；新架构下真正承重的红线变成「**token 不许回潮**」：
后人若为图省事把 localStorage token 或 `?token=` 加回来，凭据又会进浏览器历史/代理日志，
且刷新时的登录态判定会再次分叉。故保留同形闸门，判据反转。

【两层】
  · 静态层：termToken/wsUrl/settingsApplyToken 不得读写 `hub.term.token`、不得拼 `?token=`；
  · 行为层：node 跑抽出的真代码 —— 即使 localStorage 里有脏 token，也必须完全不用它。
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
from _js_min import strip_comments  # noqa: E402

HUB = ROOT / "static" / "hub.js"
SRC = HUB.read_text(encoding="utf-8")
HAVE_NODE = bool(shutil.which("node"))


def _fn(name):
    body = extract_function(SRC, name)
    assert body, "static/hub.js 抽不到 %s（改名/删除了？那这条判据要同步改）" % name
    return body


def _code(name):
    """去注释后的函数体：判据只看**代码**，注释里提到 token 不算回潮。"""
    return strip_comments(_fn(name))


class TestTokenSitesRemoved(unittest.TestCase):
    """静态层：token 回潮即红（这是用户裁定的红线，不是风格问题）。"""

    def test_ws_url_has_no_token_in_url(self):
        body = _code("wsUrl")
        self.assertNotIn("token", body,
                         "wsUrl 里出现 token ⇒ 凭据会进浏览器历史/代理日志"
                         "（2026-11-07 起会话由同源 WS 握手的 Cookie 证明）")
        self.assertIn("location.host", body, "wsUrl 仍须按当前 host 建连")

    def test_term_token_no_longer_reads_storage(self):
        body = _code("termToken")
        self.assertNotIn("hub.term.token", body,
                         "termToken 又读 localStorage token ⇒ 登录态判定分叉（刷新后假死/假登录）")
        self.assertNotIn("lsSet", body, "termToken 不得写回任何凭据")

    def test_settings_apply_no_longer_writes_token(self):
        self.assertNotIn("function settingsApplyToken", SRC,
                         "「应用到终端」入口已随共享口令废弃删除；若又出现 ⇒ token 认证回潮")


@unittest.skipUnless(HAVE_NODE, "SKIP(node-missing): 行为层要真 node 跑抽出来的真代码")
class TestNoTokenBehaviour(unittest.TestCase):
    """L1：拿 node 跑**抽出**的 termToken/wsUrl，证明存量脏值也不会被使用。"""

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

    def test_stored_token_is_ignored(self):
        """存量 localStorage 里即使有 token，termToken 也返回空串且不消费它。"""
        out = self._run("TOK ")
        self.assertEqual(out["value"], "")
        self.assertEqual(out["stored"], "TOK ", "行为层不得清理用户存量（清理由页面引导做）")

    def test_ws_url_never_carries_token(self):
        out = self._run("TOK")
        self.assertEqual(out["url"], "ws://127.0.0.1:3102/ws/term/abc")
        self.assertNotIn("token=", out["url"])

    def test_prompt_is_never_used(self):
        out = self._run(None, prompt_return=" TOK \n")
        self.assertEqual(out["value"], "")
        self.assertIsNone(out["stored"], "不得把 prompt 结果写进 localStorage")
        self.assertNotIn("token=", out["url"])


if __name__ == "__main__":
    unittest.main()
