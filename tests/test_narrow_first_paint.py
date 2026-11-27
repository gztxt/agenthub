"""窄屏**首帧**几何闸门（v0.13.74）。

【为什么要有这个文件】2026-10-04 真渲染实测发现生产 v0.13.73 有这样一条：
用户在窄屏看到的第一眼，是一块 **236px 白板盖住 60.5%(390px)~73.8%(320px)** 的视口，
正文被从中间切断 —— 形态与 09-23「整页被白板糊住」完全一致。
根因不是某条规则写错，而是**判据只覆盖了「稳定后」**：
`tests/verify_narrow_default_iconbar.py` 断言的是 initSidebar 跑完之后的 collapsed=true，
而**从导航到 initSidebar 之间那段（实测 ~1.2s）没人管**。

【本闸门的口径】不跑浏览器（那会变成 L1），只断言**首帧机制本身**成立：

1. 打标记的脚本在 `<head>` 里，且**在 `<aside>` 之前**；
2. `<head>` 脚本只读视口、只挂 `<html>` 的类，不碰 `#sidebar`、不碰 localStorage；
3. CSS 里存在 `html.narrow-rail .sidebar:not(.collapsed)` 且它在 `max-width:767px` 块内；
4. 窄屏抽屉规则 `:not(.collapsed)` **仍在**（真展开那一格没被改）；
5. 断点值 `767px` 在模板窄屏块里只出现一处（分档偏好不变量③）。

【为什么第 1 条要卡「在 <head>」】第一版把脚本放在 `<aside>` 的首个子节点，
320px 生效但 **390px 仍闪** —— 浏览器可以在解析到 `<aside>` 开标签后、
跑完该脚本之前完成首次绘制（实测两档结果不一致 = 竞态）。
移进 `<head>` 后 body 还不存在，没有任何东西可绘制 ⇒ 竞态从根上不存在。
这条断言就是防止有人「优化」回 body 里。

真渲染判据（首帧 ≤60px、位置非 fixed、视口中心归内容）由
`tests/probe_narrow_first_paint.py` 跑，见其 docstring。
"""
import re
import unittest
from pathlib import Path

from _narrow_media import block_containing

_REPO = Path(__file__).resolve().parents[1]
HTML = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")

#: 断点唯一取值（分档偏好不变量③：与 static/hub/01-core-boot.js 的 matchMedia 同值）
BREAKPOINT = 767          # int，别带单位：带 px 会在 int() 上炸（本文件第一版就是这么炸的）

#: 注释会被正则扫到而**不会被执行**——第一版闸门就是因此假红的两次：
#: 「不碰 localStorage」这句话本身写在 <head> 的注释里，于是 head 里有 localStorage；
#: 「不得另写 innerWidth<768」这句话本身也含 innerWidth。凡是**扫源码文本**的断言，
#: 都必须先剥注释，否则它在给自己抓自己。
_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
#: CSS/JS 注释。模板里窄屏规则写在 <style> 内，注释是 `/* */` 而不是 `<!-- -->`；
#: 只剥 HTML 注释的话，CSS 注释里写的**反例**会被正则当成真规则扫进去
#: （本文件已因此假红：注释里的 `.mem-item > p { min-width: 0 }` 被当成了窄屏规则）。
#: 已知局限：模板里的 JS 正则若含 `/*` 字面量会被误剥 —— 当前模板没有这种情况，
#: 真出现了应改成逐段解析，而不是继续往这个正则上堆。
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code() -> str:
    """剥掉 HTML 与 CSS/JS 注释后的模板正文（仅本闸门用于扫源码文本）。"""
    return _CSS_COMMENT.sub("", _HTML_COMMENT.sub("", HTML))


class NarrowFirstPaintTest(unittest.TestCase):
    def test_marker_script_is_in_head_before_the_sidebar(self):
        """打标记脚本必须在 `<head>` 内，且出现在 `<aside id="sidebar">` 之前。"""
        head_end = HTML.index("</head>")
        aside_at = HTML.index('<aside class="sidebar" id="sidebar"')
        marker = "narrow-rail"
        first_mark = HTML.index(marker)
        self.assertLess(first_mark, head_end,
                        "narrow-rail 标记不在 <head> 内 ⇒ 竞态回来了（浏览器可在脚本前完成首绘）")
        self.assertLess(first_mark, aside_at,
                        "标记出现在 <aside> 之后 ⇒ 首帧时侧栏几何已按抽屉算过了")
        # 且必须是脚本里的字符串，不是 CSS 选择器
        self.assertIn("document.documentElement.classList.add('narrow-rail')", HTML[:head_end])

    def test_marker_script_does_not_touch_storage_or_sidebar(self):
        """只挂 `<html>` 的类：不碰 `#sidebar`（此时还不存在）、不碰 localStorage。

        · 碰 `#sidebar` 会在 `<head>` 阶段拿到 null（元素还没建）；
        · 碰 localStorage 会违反分档偏好不变量②「加载不写盘」。
        """
        head = _code()[:_code().index("</head>")]
        self.assertNotIn("localStorage", head, "<head> 可执行代码里出现了 localStorage ⇒ 首屏写盘")
        self.assertNotIn("getElementById('sidebar')", head,
                         "<head> 阶段 #sidebar 还不存在，这里取到的是 null")

    def test_css_rule_exists_inside_the_narrow_media_block(self):
        """`html.narrow-rail .sidebar:not(.collapsed)` 必须落在窄屏块内。"""
        self.assertIn("html.narrow-rail .sidebar:not(.collapsed)", HTML,
                      "缺首帧图标条规则 ⇒ <head> 打的标记没人认")
        m = block_containing(HTML, "html.narrow-rail .sidebar:not(.collapsed)",
                             what="首帧图标条规则所属窄屏块")
        self.assertIn("html.narrow-rail .sidebar:not(.collapsed)", m,
                      "narrow-rail 规则不在窄屏 @media 块内 ⇒ 宽屏也会被改成图标条")

    def test_real_drawer_rule_survives(self):
        """真正的「展开成抽屉」规则**必须还在** —— 首帧修复不得顺手改掉展开态。"""
        body = block_containing(HTML, ".sidebar:not(.collapsed)",
                                what="窄屏抽屉规则所属块")
        self.assertIn(".sidebar:not(.collapsed) { position: fixed;", body,
                      "窄屏抽屉规则被删了 ⇒ 用户点开侧栏将不再是覆盖式抽屉")
        self.assertIn("width: 236px", body, "抽屉宽度 236px 没了")

    def test_breakpoint_value_is_single_sourced(self):
        """**管辖侧栏的 @media 块有且只有一个，且它的断点是 767px**（分档偏好不变量③）。

        【为什么不扫「模板里只有一个 max-width 值」】那会假红：模板里另有
        `max-width:1000px` / `1100px` 两个与分档**无关**的布局断点，它们是合法的。
        真正要卡的第二真相源是「侧栏/窄屏档在两个不同阈值上各写一套」，
        所以只数**管辖侧栏的块**。
        """
        # 连头带体一起抓，避免在 body 里回头找头部（第一版那么干，捕获组吃到了 '767px'）。
        pairs = re.findall(r"@media \(max-width[ :]+(\d+)px\) \{(.*?)\n        \}", HTML, re.S)
        sidebar_blocks = [int(n) for n, b in pairs if ".sidebar" in b]
        self.assertTrue(sidebar_blocks,
                        "找不到管辖侧栏的 @media 块（media 头 + 含 .sidebar 的体）")
        widths = sorted(set(sidebar_blocks))
        self.assertEqual(widths, [BREAKPOINT],
                         "管辖侧栏的 @media 断点有多个取值：%s（应只有 %s）" % (widths, BREAKPOINT))

    def test_marker_must_be_removed_when_js_takes_over(self):
        """**首帧标记必须在 JS 接管后被摘掉**——否则抽屉永远打不开。

        2026-10-04 用户报「窄屏左侧菜单栏展开不正常」，实为 v0.13.75 引入的回归：
        `<head>` 里的 `narrow-rail` 是首帧专用标记，但**打完就没再摘**。
        `html.narrow-rail .sidebar:not(.collapsed)` 的**特异性高于**
        `.sidebar:not(.collapsed)`，于是用户点「展开」时 JS 确实移除了 `collapsed`，
        几何却仍被按回 52px 图标条 ⇒ 抽屉打不开，**而遮罩照样亮**
        （实测点哪都点不到 = 整页锁死）。

        这条与上面「标记必须在 head」是**一对**：只管打、不管摘，等于把首帧的补丁
        变成了永久的补丁。真渲染判据在 `tests/probe_narrow_layout.py`
        （点开后抽屉须 ≥200px/fixed 且遮罩亮）。
        """
        shard = (_REPO / "static" / "hub" / "06-manager-tasks.js").read_text(encoding="utf-8")
        self.assertIn("classList.remove('narrow-rail')", shard,
                      "没有摘除 narrow-rail 的代码 ⇒ 抽屉永远打不开（v0.13.75 回归）")
        # 且必须在 initSidebar 里、且**在 resolve() 之后**（按源码下标比大小，不按字符距离——
        # 第一版用「resolve() 后面 400 字内」的窗口，结果被自己那 12 行注释顶出去了，假红一次。
        # 这是本轮第 4 次「闸门写得太紧 ⇒ 自己把自己判红」：断言应当表达**关系**，不是距离。
        i_res = shard.index("resolve();")
        i_rm = shard.index("classList.remove('narrow-rail')")
        self.assertLess(i_res, i_rm,
                        "摘除动作在 resolve() 之前 ⇒ 首帧标记可能在接管前就摘了，首帧又会闪")

    def test_collapsed_hide_rule_covers_every_badge_class_in_markup(self):
        """收起态的隐藏规则必须覆盖模板里**真实存在的**每个徽标类名。

        2026-10-04 用户报「收起后总览后面数字没有隐藏」。根因：规则写的是 `.badge`，
        而侧栏徽标的真实类名是 `.nav-badge`（`05-chat-and-history.js` 里拼的是
        `class="nav-badge"`）—— 选择器对不上，CSS **静默不生效**，
        48px 图标条上一直挤着个「10」。

        与 v0.13.75 那次「类名写错、样式不生效」同族，但这次反过来：
        不是**写出来的**类名不存在，而是**规则里的**类名不存在。

        口径：把模板与分片里出现过的徽标类名收集起来，逐个要求被收起态规则覆盖。
        真渲染判据在 `tests/probe_narrow_layout.py` 的第六组量
        （收起态侧栏内不得有可见数字）—— 那条不看类名，只看「有没有数字露在外面」，
        同族漏网都能抓到。
        """
        # 模板 + 分片里真实出现的徽标类。
        # 【这里踩过一次】先用 `class="[^"]*\b(badge|nav-badge)\b` 抓，抓到的**只有 badge** ——
        # 因为 `[^"]*` 贪婪吃掉了前缀 `nav-`，交给分支的只剩 `badge`；把分支按长度排序
        # 也救不了（能吃掉的永远只剩尾巴）。**正确做法是把 class 属性值切成 token 再比对**，
        # 而不是用正则去「找类名」。这与 v0.13.74「CSS 里写错类名静默不生效」同族：
        # 类名是 token，不是一段可以正则搜的子串。
        WANT = {"badge", "nav-badge", "s-badge"}
        found = set()
        for src in [_code()] + [
                q.read_text(encoding="utf-8") for q in
                sorted((_REPO / "static" / "hub").glob("*.js")) if not q.name.endswith(".bak")]:
            for m in re.finditer(r'class="([^"]*)"', src):
                found |= (set(m.group(1).split()) & WANT)
        classes = found
        self.assertTrue(classes, "收集不到任何徽标类名 —— 本闸门已空转")
        # 收起态规则有**多条**（.nav-hist / .nav-search / … 各自一条），
        # 只 grep 第一条会漏掉真正的徽标规则 —— 第一版就是这么写的，假红一次。
        # 这里收集**全部** `.sidebar.collapsed … { … }` 再逐个类名找。
        rules = re.findall(r"\.sidebar\.collapsed[^{}]*\{[^}]*\}", _code(), re.S)
        self.assertTrue(rules, "找不到收起态隐藏规则")
        rule = "\n".join(rules)
        for c in classes:
            with self.subTest(cls=c):
                self.assertIn("." + c, rule,
                              "收起态隐藏规则没覆盖 .%s ⇒ 收起后它仍可见" % c)

    def test_sidebar_toggle_button_still_present(self):
        """首帧默认收起之后，用户必须**有办法展开** —— 收起按钮不能被顺手藏掉。"""
        self.assertIn('id="btnSideToggle"', HTML,
                      "找不到侧栏展开按钮 ⇒ 窄屏首帧收起后就再也打不开了")

    # ── 类名存在性（v0.13.74 补）───────────────────────────────────────────
    def test_narrow_typography_selectors_exist_in_dom(self):
        """本批在窄屏块里写下的**每一个类选择器**都必须真实存在于模板 DOM 里。

        【为什么要这条】v0.13.74 第一版把窄屏字号规则写成 `.home-title` / `.home-lead` /
        `.home-stats` —— 这三个类名在模板里**根本不存在**。后果：CSS 静默不生效，
        真渲染截图里字号与密度**原封未动**，而当时**全部测试全绿**，
        因为没有任何一条断言去看它。**CSS 写错类名不报错**，只会安静地什么都不做 ——
        与本仓「文字存在 ≠ 已生效」（TDZ / vitals_loop / 跨档镜像）同一个家族，
        但这条更隐蔽：它连报错的机会都不给。

        口径：只校验**本批新写的那几条**，不做全量 CSS-DOM 交叉校验
        （那会把「有意不匹配的类名」也一并炸出来，噪声大于收益）。
        以后往窄屏块里加选择器，就往这个列表里加一条。
        """
        narrow_block = block_containing(_code(), ".home-wrap", what="主页窄屏排版块")
        # 本批在窄屏块里新写的类选择器（不含 .sidebar / .header 等既有结构）
        NEW_SELECTORS = (".home-brand-txt", ".home-desc", ".home-kicker",
                         ".home-stat", ".home-wrap", ".only-wide", ".only-narrow")
        for sel in NEW_SELECTORS:
            with self.subTest(sel=sel):
                self.assertIn('class="', HTML, "")
                # 类名要在 DOM 里出现过：找 `class="..."` 里含该词的写法
                found = re.search(r'class="[^"]*\b%s\b[^"]*"' % re.escape(sel[1:]), HTML)
                self.assertIsNotNone(found,
                                     "窄屏块里写了 %s，但模板 DOM 里没有这个类 ⇒ CSS 静默不生效"
                                     % sel)
                self.assertIn(sel, narrow_block,
                              "%s 应在窄屏块内生效" % sel)


class OverviewBadgeRemovedTest(unittest.TestCase):
    """侧栏「总览」行尾**不得有数字徽章**（2026-10-08 用户要求删除）。

    【为什么挂在这个文件里】本仓关于「侧栏徽标」的反向闸门已经在此（见
    `test_collapsed_hide_rule_covers_every_badge_class_in_markup`），同族。

    【口径：两处一起钉】挂载点（模板 `#badge-classroom`）与写端
    （`setBadge('classroom', …)`）必须**同时**不存在：
    - 只删挂载点 ⇒ 留下每 60s 跑一次的 `el=null` 死调用（P1-18 那条注释的原意）；
    - 只删写端   ⇒ 元素还在，`setBadge` 不再写它，徽章恒空但**占位元素还在 DOM 里**，
      收起态隐藏规则与 `.nav-badge:empty` 都被迫继续为一个永不显示的东西兜底。

    【为什么静态判据够用、但真渲染才是终判】本仓同族教训：v0.13.74 `.home-title`、
    v0.13.77 `.badge` vs `.nav-badge` 都是「写错不报错、只是安静地不生效」。
    这里判的是「元素在不在 DOM 里」，是可直接断言的量；真渲染终判在
    `tests/verify_overview_badge.py`（量真实 DOM 与真实几何，含红向自证）。
    """

    def test_home_button_has_no_badge_element(self):
        code = _code()                       # 剥注释：注释里写着 #badge-classroom
        m = re.search(r'<button[^>]*id="btnNavHome".*?</button>', code, re.S)
        self.assertIsNotNone(m, "模板里找不到 #btnNavHome ⇒ 常驻「总览」项被误删")
        self.assertNotIn('id="badge-classroom"', m.group(0),
                         "总览行尾的徽章挂载点回来了 ⇒ 用户要的「删除数字」被回退")

    def test_no_setbadge_call_for_classroom(self):
        """写端同步消失：否则留下一条永远 `el === null` 的死调用。"""
        from _js_min import strip_comments
        shard = strip_comments(
            (_REPO / "static" / "hub" / "06-manager-tasks.js").read_text(encoding="utf-8"))
        self.assertNotIn("setBadge('classroom'", shard,
                         "setBadge('classroom', …) 又出现了，但模板挂载点已删 ⇒ 死调用")

    def test_other_page_badges_are_untouched(self):
        """正对照：系统组（mcp/jobs）的徽章**必须还在** —— 不能连坐删掉整族。"""
        self.assertIn("/mcp/servers", (_REPO / "static" / "hub" / "06-manager-tasks.js")
                      .read_text(encoding="utf-8"), "MCP 徽章的数据源被误删")
        self.assertIn("/api/jobs", (_REPO / "static" / "hub" / "06-manager-tasks.js")
                      .read_text(encoding="utf-8"), "定时任务徽章的数据源被误删")
        self.assertIn("nav-badge", (_REPO / "static" / "hub" / "05-chat-and-history.js")
                      .read_text(encoding="utf-8"),
                      "系统组子项的 .nav-badge 挂载点被连坐删掉了")


class NarrowMemItemTest(unittest.TestCase):
    """`.mem-item` 在窄屏不得把描述压成「一个字母宽」（2026-10-04 用户真机截图）。

    【病】`.mem-item` 是 `display:flex` + **nowrap**，`<p>` 是 `flex:1`(=1 1 0%)。
    390px 实测：容器 296 = 名字 141 + 操作区 128 + gap 16 ⇒ 描述只剩 **3px**
    ⇒ 每个字一行。用户截图里那段「行)、小 / 字母描 / 客转文 / 字、」就是这么来的。

    【为什么早先没抓到】本仓对**同类**结构修过一次：`.sp-list > .mem-item > p { min-width: 0 }`。
    但**技能中心列表不在 `.sp-list` 里** ⇒ 同样的病只修了一处。
    这是「按选择器修 bug」的典型漏网：修的是那一个选择器，不是**那一类形态**。

    本闸门只做**静态**判据（描述独占整行 + 必须允许换行）；
    真实像素由 `tests/probe_narrow_memitem.py` 出（那个才量得出 3px 还是 292px）。
    """

    #: 一律从**剥掉注释**的模板里取窄屏块：注释里写着 `.mem-item > p { min-width: 0 }`
    #: 当反例，而正则会把它当成真规则扫进去 —— 本文件已经因此假红过一次
    #: （`localStorage` 只出现在「不碰 localStorage」这句注释里）。同一个坑不踩第二次。
    NARROW = block_containing(_code(), ".mem-item", what="记忆条目窄屏块")

    def test_mem_item_wraps_at_narrow(self):
        self.assertRegex(self.NARROW, r"\.mem-item\s*\{[^}]*flex-wrap:\s*wrap",
                         "窄屏块里没有 `.mem-item{flex-wrap:wrap}` ⇒ 描述会被压到 3px")

    def test_mem_item_description_takes_a_full_row(self):
        """描述必须 `flex-basis:100%`（独占整行）+ `min-width:0`。

        两者缺一不可：缺 basis 它跟名字抢宽度；缺 min-width 则长英文/URL
        在 flex 里也能把行撑爆（`min-width:auto` 是 flex item 的默认值）。
        """
        m = re.search(r"\.mem-item > p\s*\{([^}]*)\}", self.NARROW)
        self.assertIsNotNone(m, "窄屏块里没有 `.mem-item > p` 规则")
        body = m.group(1)
        self.assertIn("min-width: 0", body, "描述缺 min-width:0 ⇒ 长串会撑爆行")
        self.assertRegex(body, r"flex:[^;]*100%", "描述没独占整行 ⇒ 仍与名字/操作区抢宽度")


if __name__ == "__main__":
    unittest.main()