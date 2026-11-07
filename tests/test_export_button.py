#!/usr/bin/env python3
"""会话导出按钮闸门（件 1）。分层口径见 tests/tiers.py。

  L0 静态不变量 —— 只读 static/hub/04-terminal-ws.js、static/hub.js、templates/index.html 的真文本。
  L1 host       —— 真跑 node，执行从 hub.js **原样抽出**的 exportStateOf / exportStateText。

为什么这个按钮值得单独一层（三个坑都是实测出来的，不是假想）：
1. `/api/sessions/export` 是 **GET 却要写级鉴权**（后端显式 writeauth.decide("POST",…)），
   而 01-core-boot.js 的 api() 只给 POST/PUT/PATCH/DELETE 带 token ⇒ 走 api() 必 401。
2. api() 会把响应体 JSON.parse 成对象 ⇒ CSV/JSON **文件字节**被毁，必须走 blob。
3. `?token=` 会把凭据送进服务端访问日志与浏览器历史（本工作区三次外流前例）⇒ 只准走头。
外加一条与 07-asset-panel.js 同源的红向：**被拒绝不许说成"没有会话"**（四态文案互斥）。
"""
import pathlib
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


class TestExportButtonStatic(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.js = SHARD.read_text(encoding="utf-8")
        cls.hub = read_hub()
        cls.html = TPL.read_text(encoding="utf-8")

    def test_hubjs_contains_new_functions(self):
        """产物形态判据：忘记跑 build_hubjs.sh 时这里就红（先例：test_asset_panel）。"""
        for fn in ("exportStateOf", "exportStateText", "chatSessExport"):
            self.assertIsNotNone(extract_function(self.hub, fn),
                                 "hub.js 里找不到 %s ⇒ 忘记跑 scripts/build_hubjs.sh" % fn)

    def test_uses_blob_download_not_api_helper(self):
        fn = extract_function(self.js, "chatSessExport")
        self.assertIsNotNone(fn)
        self.assertIn("createObjectURL", fn, "必须走 blob 下载")
        self.assertIn("revokeObjectURL", fn, "objectURL 不回收 = 内存泄漏")
        self.assertIn(".download", fn)
        self.assertNotIn("await api(", fn, "api() 会 JSON.parse 毁掉文件字节，且不给 GET 带 token")

    def test_no_token_and_cookie_is_used(self):
        """2026-11-07 改判：浏览器不再持有 token ⇒ 导出只能靠同源 Cookie。
        真正的红线变成「不得把任何凭据拼进 URL / 不得再要求用户输 token」。"""
        fn = extract_function(self.js, "chatSessExport")
        self.assertNotIn("X-TERM-TOKEN", fn, "token 认证已停用，导出不得再读终端 token")
        self.assertNotIn("termToken", fn, "不得回潮到 termToken() 取凭据")
        self.assertNotIn("token=", fn, "URL 里出现 token= ⇒ 凭据外流面")
        self.assertNotIn("location.href", fn, "整页跳转下载会把凭据/会话带进历史")
        self.assertIn("credentials: 'same-origin'", fn, "必须显式带同源 Cookie")

    def test_default_is_redacted(self):
        fn = extract_function(self.js, "chatSessExport")
        self.assertIn("redact", fn)
        self.assertRegex(fn, r"redact['\"]?\s*[:,]\s*['\"]?1", "默认必须 redact=1（不静默给原文）")

    def test_button_uses_delegation_not_inline_onclick(self):
        """AGENTS.md 浮层配套红线：口径并存取最严 ⇒ 新按钮走 data-* 委托，不用 inline onclick。"""
        i = self.html.index('id="chatSessList"')
        seg = self.html[i:self.html.index("</span>", i)]
        self.assertIn('data-export="sessions"', seg)
        self.assertNotIn('onclick="chatSessExport', seg)
        self.assertIn("closest('[data-export]')", self.js.replace('"', "'"),
                      "委托监听不见了 ⇒ 按钮点了没反应")

    def test_icon_exists_in_sprite(self):
        """红向：发明一个不存在的 sprite id，按钮会渲染成空白（先例：test_asset_panel 的色 token 闸门）。"""
        import re
        i = self.html.index('data-export="sessions"')
        seg = self.html[i:i + 300]
        used = re.findall(r'href="#(i-[a-z0-9-]+)"', seg)
        self.assertTrue(used, "按钮没有图标 ⇒ 窄屏上是个看不见的点击区")
        for u in used:
            self.assertIn('id="%s"' % u, self.html, "图标 %s 不在 sprite 里 ⇒ 按钮空白" % u)

    def test_no_bare_localstorage_no_second_breakpoint(self):
        """分档五不变量（AGENTS.md）：不许裸 localStorage、不许第二处断点定义。"""
        sys.path.insert(0, str(_REPO / "tests"))
        from _js_min import strip_comments
        fn = extract_function(self.js, "chatSessExport")
        code = strip_comments(fn)
        self.assertNotIn("localStorage.", code, "必须走 lsGet/lsSet/lsRemove 守卫")
        self.assertNotIn("matchMedia", code)
        self.assertNotIn("innerWidth", code)

    def test_state_wording_sets_are_disjoint(self):
        """★ 四态文案互斥：被拒态里不许出现"空结果"字样，反之亦然。"""
        fn = extract_function(self.js, "exportStateText")
        self.assertIsNotNone(fn)
        rejected = ("need-token", "bad-token", "misconfig", "error")
        for st in rejected:
            self.assertIn("'%s'" % st, fn, "缺 %s 分支" % st)
        # 被拒分支的文案必须明说"不是没有会话"
        self.assertGreaterEqual(fn.count("不是没有会话"), 4,
                                "四种被拒/失败态都必须明说「不是没有会话」，否则用户读成资产为空")
        self.assertIn("'empty'", fn)
        self.assertIn("0 条会话", fn, "真·空结果才准说 0 条")


@tiers.host_only
class TestExportButtonNode(unittest.TestCase):
    """L1：真跑 node，执行从 hub.js **原样抽出**的判定与文案函数（不手抄实现）。"""

    @classmethod
    def setUpClass(cls):
        if not shutil.which("node"):
            raise unittest.SkipTest(tiers.HOST_SKIP_REASON)
        hub = read_hub()
        parts = [extract_function(hub, n) for n in ("exportStateOf", "exportStateText")]
        if any(p is None for p in parts):
            raise AssertionError("hub.js 里抽不到导出函数（或被改名）⇒ 判据无法针对真代码执行")
        cls.src = "\n".join(parts)

    def _run(self):
        js = self.src + "\n" + r"""
function out(tag, s) { console.log(tag + "||" + String(s).replace(/\s+/g, " ")); }
out("S_401_NOTOK", exportStateOf(401, -1, "缺少凭据（x-hub-token / x-term-token / ?token=）"));
out("S_401_BAD", exportStateOf(401, -1, "凭据不匹配"));
out("S_503", exportStateOf(503, -1, "服务端未配置 TERM_TOKEN/HUB_PASSCODE"));
out("S_500", exportStateOf(500, -1, "boom"));
out("S_EMPTY", exportStateOf(200, 0, ""));
out("S_OK", exportStateOf(200, 3, ""));
out("T_NOTOK", exportStateText("need-token", -1, {}));
out("T_BAD", exportStateText("bad-token", -1, {}));
out("T_503", exportStateText("misconfig", -1, {}));
out("T_ERR", exportStateText("error", -1, {status: 500}));
out("T_EMPTY", exportStateText("empty", 0, {}));
out("T_OK", exportStateText("ok", 3, {hits: 2}));
out("T_OK0", exportStateText("ok", 3, {hits: 0}));
"""
        p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=60)
        self.assertEqual(p.returncode, 0, "node 报错：\n" + p.stderr[:800])
        out = {}
        for line in p.stdout.splitlines():
            if "||" in line:
                k, v = line.split("||", 1)
                out[k] = v
        self.assertTrue(out, "node 无任何输出，判定其实是空转")
        return out

    def test_states_are_distinguished(self):
        o = self._run()
        self.assertEqual(o["S_401_NOTOK"], "need-token")
        self.assertEqual(o["S_401_BAD"], "bad-token")
        self.assertEqual(o["S_503"], "misconfig")
        self.assertEqual(o["S_500"], "error")
        self.assertEqual(o["S_EMPTY"], "empty")
        self.assertEqual(o["S_OK"], "ok")

    #: 文案里**故意**写的消歧否定式。朴素 substring 判定分不清"没有会话"与"不是没有会话"
    #: （2026-09-25 实测栽过：正确文案被自己的闸门判红，与 mcpgw 的 bool(body.env) 同族
    #: ＝闸门精度缺陷）。判定前先把否定式摘掉，剩下的才算"声称空态"。
    NEGATIONS = ("不是没有会话", "不是被拒", "文件是空的，不是被拒")

    def _claims_empty(self, text):
        t = text
        for n in self.NEGATIONS:
            t = t.replace(n, "")
        return [b for b in ("0 条会话", "没有会话", "确实是 0") if b in t]

    def test_rejected_never_reads_as_empty(self):
        """★ 本闸门核心红向：被拒态文案不许**声称**空态（否定式提及是消歧，不算声称）。"""
        o = self._run()
        for tag in ("T_NOTOK", "T_BAD", "T_503", "T_ERR"):
            self.assertEqual(self._claims_empty(o[tag]), [],
                             "%s 把被拒渲染成了空态：%s" % (tag, o[tag]))
            self.assertIn("不是没有会话", o[tag], "%s 缺消歧否定式" % tag)

    def test_empty_state_is_honest_about_success(self):
        o = self._run()
        self.assertIn("导出成功", o["T_EMPTY"])
        self.assertIn("0 条会话", o["T_EMPTY"])
        # 判"被拒前缀"而不是裸词"被拒"：空态文案里写"不是被拒"是消歧，不是自认被拒
        self.assertNotIn("导出被拒", o["T_EMPTY"])
        self.assertEqual(self._claims_empty(o["T_EMPTY"]).count("没有会话"), 0)

    def test_ok_state_reports_redaction_hits(self):
        """不静默改数据：打码命中数必须说出来（后端 X-Export-Redacted-Hits 的兑现）。"""
        o = self._run()
        self.assertIn("3", o["T_OK"])
        self.assertIn("2", o["T_OK"])
        self.assertIn("脱敏", o["T_OK"])
        self.assertIn("0 处", o["T_OK0"], "命中 0 处也要如实说，不能省略成「没打码」")

    def test_error_state_carries_http_code(self):
        self.assertIn("500", self._run()["T_ERR"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
