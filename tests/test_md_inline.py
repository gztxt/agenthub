"""行内 markdown（`mdInline`）的行为与**安全**闸门。

【为什么这个文件的重点是安全，不是渲染】
技能描述取自 `SKILL.md` 的 frontmatter，而那些文件来自 **20+ 个发现点**，
其中包含第三方仓（mattpocock-skills / hallmark / Agent-Reach / crawl4ai）
—— 它们是**外部内容**，不是本仓自己写的文案。
hub 的 origin 里有**终端**（能起 pty）。所以「渲染 markdown」这件事必须按
**处理不可信输入**来做：能写进 frontmatter 的恶意描述一旦渲染成
`<img onerror=…>` 或 `<a href="javascript:…">`，就是**存储型 XSS** 且能直接摸到终端。

口径因此收到最小：**只认 `**粗体**` 与 `` `行内代码` `` 两个行内标记**，
链接/图片/标题/列表/原始 HTML 一律**不渲染**（保持转义后的字面文本）。
理由写在 `01-core-boot.js` 的 `mdInline` 注释里，这里只钉行为。

【夹具的纪律（本文件踩过一次）】
第一版把「真名与手误串两边都手打」，恒真假绿。凡是「手误/变体」类夹具，
输入必须**由原串构造**出来，不许手打。
"""
import re
import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))
from _js_min import strip_comments  # noqa: E402

CORE = _REPO / "static" / "hub" / "01-core-boot.js"


def _extract(name: str) -> str:
    """抠出函数（或 `MD_CODE_PH` 常量及其后的 mdInline）。

    【踩过】第一版把两条分支写成一个正则 `^(function NAME\(|const MD_CODE_PH)` ——
    问 `escapeHtml` 时，`const MD_CODE_PH`（在它前面）先匹配上了，于是
    escapeHtml 和 mdInline 各被抠出**同一个块**，拼起来重复声明 ⇒
    `SyntaxError: Identifier 'MD_CODE_PH' has already been declared`。
    **提取器自己按名字取，不能靠一个跨名字的备选分支。**
    """
    src = CORE.read_text(encoding="utf-8")
    if name == "MD_CODE_PH":
        m = re.search(r"^const MD_CODE_PH\b", src, re.M)
    else:
        m = re.search(r"^function %s\(" % re.escape(name), src, re.M)
    assert m, "01-core-boot.js 里找不到 %s" % name
    if name == "MD_CODE_PH":
        j = src.index("\nfunction mdInline", m.start())
        k = src.index("\n/* ── 模糊搜索", j)
        return src[m.start():k]
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    assert depth == 0, "函数 %s 括号不配对，抠出来的是半截" % name
    return src[m.start():j + 1]


def _run(body: str):
    esc = _extract("escapeHtml")
    js = esc + "\n" + _extract("MD_CODE_PH") + "\n" + body
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, "node 执行失败：%s\n%s" % (p.stderr[:400], body)
    return p.stdout.strip()


class MdInlineTest(unittest.TestCase):
    """渲染：只认两个行内标记，其余保持字面。"""

    def test_bold_and_inline_code(self):
        out = _run(
            "const md = t => eval('mdInline')(t);" if False else
            "console.log(mdInline('用 **仅当落在 X 时** 与 `c4ai`'));")
        self.assertEqual(out, "用 <strong>仅当落在 X 时</strong> 与 <code>c4ai</code>")

    def test_inline_code_wins_over_bold(self):
        """代码里的 `**` 必须原样保留 —— 否则先切粗体会把代码内容切碎。"""
        out = _run("console.log(mdInline('用 `**bold**` 而非 **bold**'));")
        self.assertEqual(out, "用 <code>**bold**</code> 而非 <strong>bold</strong>")

    def test_identifiers_and_globs_untouched(self):
        """**不做斜体**的理由：`snake_case` 与 `*.py` 是标识符/通配符，不是强调。"""
        out = _run("console.log(mdInline('键 hub.sidebar.narrow；匹配 *.py 与 *.js'));")
        self.assertEqual(out, "键 hub.sidebar.narrow；匹配 *.py 与 *.js")

    def test_empty_and_null(self):
        self.assertEqual(_run("console.log(mdInline(''));"), "")
        self.assertEqual(_run("console.log(mdInline(null));"), "")
        self.assertEqual(_run("console.log(mdInline(undefined));"), "")

    def test_placeholder_char_in_input_is_stripped(self):
        """占位符冲突：输入里自带 \\u0001 不得让还原正则错位。"""
        out = _run("console.log(mdInline('a\\u0001b `c`\\u0001d'));")
        self.assertNotIn("\\u0001", out, "占位符字符漏进了输出")
        self.assertIn("<code>c</code>", out)


class MdInlineSecurityTest(unittest.TestCase):
    """安全：外部描述**不得**产出任何可执行标签或链接。"""

    def _no_active_markup(self, out: str):
        """输出里**只允许** `strong` / `code` 两个标签。

        这条就是安全判据的全部。理由：既然后台只可能插入这两个标签，
        而其余字符**全部**经过了 `escapeHtml`，那么 `<img onerror>` 之类
        在输出里只能是 `&lt;img onerror&gt;` 这样的**字面文本**，不会被解析成标签。

        【这条判据被我自己写错过两次】先前还加了「输出里不能出现 onerror /
        href= / javascript:」—— 全是错判据：转义后的文本里这些**字面量本来就会保留**
        （那正是「用户写的内容被如实显示」），断言它们不存在等于要求把内容也抹掉。
        真正的不变量只有一条：**没有被插入白名单外的标签。**
        """
        for tag in re.findall(r"<\s*/?\s*([a-zA-Z][a-zA-Z0-9]*)", out):
            self.assertIn(tag.lower(), ("strong", "code"),
                          "出现了白名单外的标签 <%s> ⇒ XSS 面：%s" % (tag, out))
        # 另加一条等价判据：白名单标签不得带任何属性（strong/code 不需要属性）
        for attrs in re.findall(r"<(strong|code)([^>]*)>", out):
            self.assertEqual(attrs[1].strip(), "",
                             "<%s> 带了属性 %r ⇒ 不该由我们生成" % attrs)

    def test_img_onerror_payload(self):
        out = _run("console.log(mdInline('<img src=x onerror=alert(1)>'));")
        self.assertIn("&lt;img", out)
        # 注意：**不要**断言「输出里没有 onerror」—— 转义后的 `&lt;img … onerror=…&gt;`
        # 里那个 onerror 只是字面文字，无害。真正的判据是「它有没有变成标签」，
        # 由 _no_active_markup 判（只允许 strong / code 两个标签）。
        self._no_active_markup(out)

    def test_script_inside_bold(self):
        out = _run("console.log(mdInline('**<script>alert(1)</script>**'));")
        self.assertIn("<strong>", out)
        self.assertIn("&lt;script&gt;", out)
        self._no_active_markup(out)

    def test_markdown_link_is_not_a_link(self):
        """`[x](javascript:…)` **必须保持字面文本** —— 绝不生成 <a>。"""
        out = _run("console.log(mdInline('[点我](javascript:alert(1))'));")
        self.assertNotIn("<a", out.lower())
        self.assertEqual(out, "[点我](javascript:alert(1))")

    def test_markdown_image_is_not_an_img(self):
        out = _run("console.log(mdInline('![alt](x)'));")
        self.assertNotIn("<img", out.lower())
        self.assertIn("![alt](x)", out)

    def test_raw_html_inside_code(self):
        out = _run("console.log(mdInline('`<svg onload=alert(1)>`'));")
        self.assertIn("<code>&lt;svg onload=alert(1)&gt;</code>", out)
        self._no_active_markup(out)

    def test_escape_order_is_escape_then_substitute(self):
        """顺序不可颠倒：若先切 markdown 再转义，`<strong>` 会被自己的转义吃掉。
        这里断言「标签是**原样**的 `<strong>`」而不是 `&lt;strong&gt;`。"""
        out = _run("console.log(mdInline('**a<b>**'));")
        self.assertIn("<strong>", out)
        self.assertIn("&lt;b&gt;", out)


class MdInlineWiringTest(unittest.TestCase):
    """接线：三个技能描述渲染点都必须用 mdInline，而不是 escapeHtml。"""

    SITES = [
        ("static/hub/04-terminal-ws.js",
         ["mdInline(String(s.description", "mdInline(String(x.description", "mdInline(f.descript"]),
        # MCP 工具描述（来源：MCP server 的 tools/list，同样是外部内容）
        ("static/hub/05-chat-and-history.js", ["mdInline(x.description)"]),
    ]

    def test_all_description_sites_use_mdinline(self):
        for rel, needles in self.SITES:
            src = (_REPO / rel).read_text(encoding="utf-8")
            for needle in needles:
                with self.subTest(site=needle):
                    self.assertIn(needle, src,
                                  "%s 里的技能描述渲染点没走 mdInline" % needle)

    def test_mcp_tool_name_is_escaped_for_both_contexts(self):
        """MCP 工具名同时进 **HTML 正文** 与 **属性值**，两个语境都要设防。

        工具名来自 MCP server（外部注册）。原实现两处都没设防：
        `<b>' + x.name + '</b>` 直接拼 HTML，`onclick="pickTool('' + x.name + '')"` 直接拼 JS。
        本仓 `jsStr` 的注释早就写明 HTML 实体转义不足以让任意串安全进 JS 字面量。

        v0.13.131（PT-20261019-02）改机制：`onclick` + jsStr 本身有害（双引号字面量
        被双引号属性截断 ⇒ 点击静默失败），现改两个 `data-sk-pt-*` 属性 + 委托，
        值走 escapeHtml。判据跟着改：查属性值转义 + 正文转义 + 不许退回 onclick。
        """
        src = strip_comments((_REPO / "static" / "hub" / "05-chat-and-history.js")
                            .read_text(encoding="utf-8"))
        line = [l for l in src.split("\n")
                if "data-sk-pt-server" in l and "mem-item" in l]
        self.assertTrue(line, "没找到 MCP 工具行（data-sk-pt-server 形态）")
        l = line[0]
        self.assertIn("escapeHtml(x.server)", l, "server 进属性值没转义")
        self.assertIn("escapeHtml(x.name)", l, "HTML 正文/属性里的工具名没转义")
        self.assertNotIn("<b>' + x.name + '</b>", l, "工具名仍是裸拼进 HTML")
        self.assertNotIn("onclick", l, "不许退回 inline onclick（jsStr 会截断属性）")

    def test_skill_center_description_uses_mdinline_not_escapehtml(self):
        """技能中心那一处是最常被看到的；逐行核对它没有退回 escapeHtml。"""
        # strip_comments 返回**字符串**；写成 [0] 会拿到第一个字符（本文件第二次踩这个）
        src = strip_comments((_REPO / "static" / "hub" / "04-terminal-ws.js")
                            .read_text(encoding="utf-8"))
        lines = [l for l in src.split("\n")
                 if "description" in l and ("escapeHtml" in l or "mdInline" in l)]
        self.assertTrue(lines, "没找到技能描述渲染行")
        for l in lines:
            self.assertNotIn("escapeHtml(String(s.description", l,
                             "技能中心描述退回 escapeHtml ⇒ 星号又原样显示了")


if __name__ == "__main__":
    unittest.main()