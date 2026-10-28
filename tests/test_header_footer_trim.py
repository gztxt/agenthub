"""顶栏「GitHub 地址」+ 底部状态栏删除 的静态守卫（2026-10-08）。

【本批改动】用户 23:3x 两条指令：
  ① 删除底部状态栏 `.footer`（版本号+ Apache-2.0 鸣谢 + 每秒时钟）；
  ② 顶栏添加 github.com/gztxt/agenthub 仓库地址。

【为什么删除要连带三处，而不是只删 DOM】
`.footer` 那一行的文本由 JS 每秒写一次（`#ftTime`）。只删挂载点、写端留着，
就是一条每 1000ms 往 null 上写 `.textContent` 的死调用 —— 本仓 P1-18 注释
点名过的形态，且它**不报错**：页面看着完全正常，只是控制台每秒一条 TypeError。
所以本闸门把「挂载点 / CSS / 写端」当成**一个整体**来守：任一处复活即红。

【为什么扫源码前必须剥注释】
模板与JS 的注释里大量复述了被删的字面量（本文件顶部的说明、模板里那段
「原先这里有 .footer…」的存档注释、06 里的「tick 的唯一消费者是 #ftTime」）。
不剥注释的断言会 100% 把自己判红 —— 与 test_narrow_first_paint.py 第一版
同一种假绿/假红。判据一律在**剥完注释的正文**上扫。

【正对照】本仓同族的「写错不报错、只是安静地不生效」：`.home-title` /
`.brand-glyph` 这类。故每组删除断言都配一条**存在性**断言（GitHub 链接的
href / target / rel、symbol 定义、窄屏规则），避免「搜不到就当通过」。
"""
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
_HTML_RAW = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
_JS_RAW = (_REPO / "static" / "hub" / "06-manager-tasks.js").read_text(encoding="utf-8")
_HUBJS_RAW = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

# 剥注释（与 test_narrow_first_paint.py 同一套口径；局限见该文件说明）
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _strip(src: str) -> str:
    return _CSS_COMMENT.sub("", _HTML_COMMENT.sub("", src))


HTML = _strip(_HTML_RAW)
JS = _strip(_JS_RAW)
HUBJS = _strip(_HUBJS_RAW)

#: 唯一权威远端（2026-10-08 仓改名 agent-hub→agenthub 时同步改的）。
#: 与 `git remote get-url origin` 一致；判据里写死是为了「改名后忘了改文案」能变红。
REPO_URL = "https://github.com/gztxt/agenthub"

#: 断点唯一取值（分档偏好不变量③，与 01-core-boot.js 的 matchMedia 同值）
BREAKPOINT = 767


class FooterRemovedTest(unittest.TestCase):
    """① 底部状态栏：三处（挂载点 / CSS / 写端）必须同时消失。"""

    def test_footer_mount_point_gone(self):
        self.assertNotRegex(HTML, r'<div[^>]*class="[^"]*\bfooter\b',
                            "底部状态栏挂载点复活了（用户 2026-10-08 已裁定删除）")
        self.assertNotIn('id="ftTime"', HTML, "#ftTime 挂载点复活 ⇒ 又要每秒往它写字")

    def test_footer_css_gone(self):
        """两条 CSS（宽屏 .footer + 窄屏 .footer/.foot-long）都不得复活。

        判据按**选择器**而不是按字符串 `.footer`：存档注释里出现该词是合理的，
        而 `class="footer"` 这类选择器写法只可能来自真规则。
        """
        for sel in (r"\.footer\s*\{", r"\.footer\s+\.foot-long\s*\{"):
            self.assertNotRegex(HTML, sel, f"底部状态栏 CSS 复活：匹配到 {sel}")

    def test_footer_writer_gone(self):
        """写端：$('ftTime') 与只服务它的 tick() / setInterval(tick, 1000)。"""
        self.assertNotIn("ftTime", JS, "06-manager-tasks.js 还在写 #ftTime ⇒ 每秒一条死调用")
        self.assertNotRegex(JS, r"function\s+tick\s*\(", "tick() 未随挂载点一起删")
        self.assertNotRegex(JS, r"setInterval\(\s*tick\s*,", "setInterval(tick, …) 残留")

    def test_built_artifact_carries_no_writer(self):
        """产物 static/hub.js 同样不得含写端。

        为什么单独一条：`static/hub.js` 是 `static/hub/*.js` 的拼接产物，
        源分片删了但产物没重建（漏跑 scripts/build_hubjs.sh）时，**页面照样
        每秒报一次错**，而源分片的断言全绿 —— 这个缝隙只有直接查产物才盖得住。
        """
        self.assertNotIn("ftTime", HUBJS, "产物 static/hub.js 仍含 #ftTime 写端 ⇒ 该重建产物了")

    def test_apache_attribution_survives_in_readme(self):
        """删掉的只是页面上那行重复短鸣谢，Apache-2.0 的正式声明仍在 README 首段。

        这是本批唯一一处「删界面」可能踩到许可证的风险，故钉一条正向断言：
        README 必须仍写明上游 Zafer-Liu/Agent_Manager 与 Apache-2.0。
        """
        readme = (_REPO / "README.md").read_text(encoding="utf-8")[:1200]
        self.assertIn("Agent_Manager", readme, "README 首段的上游声明不见了")
        self.assertIn("Apache-2.0", readme, "README 首段的许可证声明不见了")


class GithubLinkTest(unittest.TestCase):
    """② 顶栏 GitHub 仓库入口：存在性 + 外链三要素 + 图标 + 窄屏档位。

    v0.13.98（用户「右上角 github 链接只保留图标和版本号」）起，可见文字是**版本号**
    而不是仓库名 —— 但入口本身一个属性都没少，故本类只把断言从「文字是地址」改成
    「文字是版本号 + 地址仍在 href/title/aria 三处」。
    """

    def test_link_exists_in_header(self):
        self.assertIn('class="gh-link"', HTML, "顶栏没有 GitHub 地址链接")
        self.assertIn(REPO_URL, HTML, f"仓库地址不是 {REPO_URL}")

    def test_link_is_in_header_block(self):
        """必须落在 `.header` 内（顶栏），不能只存在于页面别处。"""
        h = HTML.index('<div class="header">')
        seg = HTML[h:HTML.index("</header>", h) if "</header>" in HTML[h:] else h + 4000]
        self.assertIn('class="gh-link"', seg, "GitHub 链接不在顶栏 .header 块内")

    def test_external_link_safety_attrs(self):
        """新窗口外链必须带 target=_blank **且** rel=noopener noreferrer。

        只查 target 不查 rel 是不够的：缺 rel 时新页面能通过 window.opener
        反向操纵本页（tabnabbing）。两条一起断言。
        """
        m = re.search(r'<a class="gh-link"[^>]*>', HTML)
        self.assertIsNotNone(m, "找不到 gh-link 锚标签")
        tag = m.group(0)
        self.assertIn('target="_blank"', tag, "外链缺 target=_blank")
        self.assertIn('rel="noopener noreferrer"', tag, "外链缺 rel=noopener noreferrer")
        self.assertIn("github.com/gztxt/agenthub", tag, "title/aria 未给出可读的完整地址")

    def test_icon_symbol_defined_and_used(self):
        """sprite 里有 i-github 定义，且消费侧带 class="i fill"。

        `svg.i` 默认 fill:none（描边语言），GitHub 标记是实心图形 ⇒ 漏掉 fill
        会渲染成一个空心方框，**不报任何错**。故 fill 必须被断言。
        """
        self.assertRegex(HTML, r'<symbol id="i-github"', "sprite 里没有 i-github 定义")
        self.assertRegex(HTML, r'<use href="#i-github"/>', "没有任何地方用 i-github")
        m = re.search(r'<svg class="([^"]*)"[^>]*><use href="#i-github"/>', HTML)
        self.assertIsNotNone(m, "i-github 的消费侧结构与预期不符")
        self.assertIn("fill", m.group(1), "i-github 消费侧缺 fill 类 ⇒ 实心标记被画成空框")

    def test_narrow_hides_text_keeps_icon(self):
        """窄屏（≤767px）只留图标：文字隐藏规则必须在既有窄屏块内。

        为什么卡「在 media 块内」：把 .gh-txt { display:none } 写在块外会让
        **宽屏也只剩图标**（v0.13.98 起宽屏那行字是版本号，是常驻身份信息），
        而那正是顶栏唯一的版本/仓库入口 —— 与 test_narrow_first_paint.py 里
        narrow-rail 那条同源的静默失效。
        """
        self.assertIn(".gh-link .gh-txt", HTML, "缺窄屏文字隐藏规则")
        m = re.search(r"@media \(max-width: %dpx\) \{(.*?)\n        \}" % BREAKPOINT, HTML, re.S)
        self.assertIsNotNone(m, "找不到窄屏 @media 块")
        self.assertIn(".gh-link .gh-txt", m.group(1), "文字隐藏规则不在窄屏块内 ⇒ 宽屏也没地址")

    def test_visible_text_is_version_not_repo_name(self):
        """可见文字必须是版本号：`.gh-txt` 里既不许写死版本号，也不许留仓库名。

        两条都卡，因为它们的失效方式相反且**都不报错**：
          · 写死 `0.13.97` 字面量 ⇒ bump 后顶栏与 /health 各说各话（无告警漂移，
            本仓 CHANGELOG 顶部漂过 13 版、tests/README 计数漂过两轮）；
          · 留 `gztxt/agenthub` ⇒ 用户「只保留图标和版本号」的指令没落地，
            页面看着一切正常，只是那行字又宽了 6 个字符。
        """
        m = re.search(r'<span class="gh-txt">(.*?)</span>', HTML, re.S)
        self.assertIsNotNone(m, "找不到 .gh-txt（顶栏 GitHub 入口的可见文字）")
        txt = m.group(1).strip()
        self.assertEqual("v{{ version }}", txt,
                         ".gh-txt 应是 Jinja 变量 v{{ version }}，实际 %r —— "
                         "写死版本号会与 /health 漂移，留仓库名则用户指令未落地" % txt)

    def test_version_reaches_template_from_main(self):
        """模板变量必须真被喂到：`/` 路由要把 `VERSION` 传进模板。

        为什么单独一条：模板里写 `{{ version }}` 而路由忘了传 context 时，
        **Jinja 默认渲染成空串**（不抛异常）⇒ 顶栏变成一个光秃秃的 GitHub 图标，
        页面完全正常。判据读 `src/main.py` 源码文本（L0 禁 import src.main）。
        """
        main_py = (_REPO / "src" / "main.py").read_text(encoding="utf-8")
        self.assertRegex(
            main_py, r'TemplateResponse\(\s*request,\s*"index\.html",\s*\{[^}]*"version"',
            "index.html 用到了 {{ version }}，但 / 路由没把 VERSION 传进模板 "
            "⇒ 顶栏会静默渲染成空版本号")

    def test_no_second_narrow_tier_source(self):
        """分档偏好不变量③：窄屏档在 JS 侧只有一个真相源，且与 CSS 同值。

        【为什么不扫「模板里只有一个 max-width 值」】那会假红：模板里另有
        `max-width:1100px` 这个与分档**无关**的布局断点（`.mem-grid` 单列化），
        它合法且早于本批存在（test_narrow_first_paint.py 的同名注释已记录）。
        （`.home-wrap` 的 1000px 上限已于 2026-10-29 随「内容区随窗口长」一并去掉。）真正要卡的是「窄屏档在两个不同阈值上各写一套」，
        所以只数 JS侧的 matchMedia(max-width:) 定义，并要求它等于 CSS 的 767。
        """
        js_all = _strip((_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8"))
        mqs = re.findall(r"matchMedia\('\(max-width:\s*(\d+)px\)'\)", js_all)
        self.assertEqual(
            [int(x) for x in mqs], [BREAKPOINT],
            "JS 侧窄屏 matchMedia 的阈值应只有 %d 一个，实际 %s"
            % (BREAKPOINT, mqs))


if __name__ == "__main__":
    unittest.main()