#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：系统子菜单页的统一骨架（v0.13.40）静态闸门。

背景（用户 2026-09-27 裁定）：
  「系统菜单里的子菜单点进去，右边内容框顶部的标题和分割线都要删除；
    另外右边页面的排版都要优化，实在是乱七八糟的 —— 每个子菜单的右边内容框
    页面都要优化和重新设计。」
改完之后最容易复发的是三件事，本闸门逐条钉死：

  ① **页顶标题悄悄回来**：renderPageCrumb 是全站唯一写 #crumb 的地方（实体页由
     renderModeBar 写），只要它又开始往 crumb 里拼页名，opBar 就不再判 void，
     「标题 + 分割线」会重新出现在每个系统页顶部 —— 而这件事在截图上很容易被
     当成"没改到"，在 diff 里更是两行的事。这里断言它只清空、不写字。
  ② **迁移只做了几页**：10 个系统页必须全部走 .sp 骨架、且不再混用 .panel
     自搭（混用时页面间距/卡头/滚动归属立刻不一致，就是本次报障的原始形态）。
  ③ **多选芯片壳把数据源换掉了**：芯片只是原生 <select multiple> 的可视壳，
     option 清单与 selectedOptions 读法是唯一真源（memfed 源清单还被
     test_center_ui_l0 按 <option value="…"> 逐个校验）。谁把 select 删了换成
     自造状态，这里判红。

分层：只读 templates/index.html 与 static/hub/*.js 的**真文本**，不起服务、
不用 node ⇒ 干净 runner 上结论一模一样，且不允许 SKIP。
"""
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))

from _js_min import strip_comments   # noqa: E402

TPL = _REPO / "templates" / "index.html"
SHARD04 = _REPO / "static" / "hub" / "04-terminal-ws.js"
SHARD05 = _REPO / "static" / "hub" / "05-chat-and-history.js"
SHARD06 = _REPO / "static" / "hub" / "06-manager-tasks.js"
SHARD07 = _REPO / "static" / "hub" / "07-asset-panel.js"

#: 系统菜单（左栏「系统」手风琴）下的全部子菜单页 —— 本次重排的对象
SYS_PAGES = ("ports", "telemetry", "memory", "skills", "kb",
             "mcp", "tasks", "jobs", "assets")   # v0.13.47：runlog 页删除，并入设置→日志

#: 单表/单列表页：表格必须在限高盒（.sp-bd.box + .tscroll）里，sticky 表头才有滚动容器
TABLE_PAGES = ("ports", "jobs", "tasks")

#: 既有的断点声明（值原文）。新页面只许复用，不许再开一档 —— 断点多了必然出现
#: "某个档忘了适配"，而那种漏在宽屏上是看不出来的。
ALLOWED_MEDIA = {
    "(min-width: 768px)",
    "(max-width: 767px)",
    "(max-width: 1000px)",
    "(max-width: 1100px)",
    "(prefers-reduced-motion: reduce)",
}


def _css(html):
    """取 <style> 块内的样式文本，并去掉 CSS 注释。

    为什么不能拿整份模板当 CSS 判卷：模板正文的 HTML 注释里写着「不新建 @media」
    这类自律声明，正则 `@media[^{]*{` 会从那里一路吞到文件尾（实测把半份模板当成
    一档断点名）—— 判卷对象必须是样式块本身。
    """
    start = html.index("<style>") + len("<style>")
    end = html.index("</style>")
    return re.sub(r"/\*.*?\*/", "", html[start:end], flags=re.S)


def _sections(html):
    """把 <main> 里每个 <section class="page" id="page-X">…</section> 切出来。

    不用 HTML 解析器：模板里没有嵌套 section，按起始标签切 + 找最近的 </section>
    足够，且避免给 L0 测试引入新依赖。"""
    out = {}
    for m in re.finditer(r'<section class="page[^"]*" id="page-([\w-]+)">', html):
        end = html.index("</section>", m.end())
        out[m.group(1)] = html[m.end():end]
    return out


class TestSysPagesLayout(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.html = TPL.read_text(encoding="utf-8")
        cls.sections = _sections(cls.html)
        cls.css = _css(cls.html)
        cls.s06 = SHARD06.read_text(encoding="utf-8")
        cls.s04 = SHARD04.read_text(encoding="utf-8")
        cls.s05 = SHARD05.read_text(encoding="utf-8")

    # ── ① 页顶标题与分割线 ────────────────────────────────────────────
    def test_crumb_is_never_written_for_system_pages(self):
        """renderPageCrumb 只清空、不拼页名 ⇒ syncOpBar 判 void ⇒ 整条 opBar 收起。

        红向写法是 `crumb.innerHTML = '<b>' + label + '</b>'`（v0.13.40 之前的样子）。"""
        m = re.search(r"function renderPageCrumb\(.*?\n\}", self.s06, re.S)
        self.assertTrue(m, "renderPageCrumb 缺失")
        body = strip_comments(m.group(0))
        self.assertNotIn("PAGE_LABELS", body,
                         "renderPageCrumb 又往面包屑里写页名了 ⇒ 系统页顶部标题/分割线复发")
        self.assertNotIn("<b>", body, "面包屑里不该再拼任何标题文本")
        self.assertIn("crumb.innerHTML = ''", body, "必须清空 crumb（否则残留上一页标题）")
        self.assertIn("syncOpBar()", body, "清空后必须重算 opBar 的 void 态")

    def test_entity_workspace_still_owns_its_bar(self):
        """实体工作台（chat）必须保持早退 —— 它的顶栏由 renderModeBar 接管，
        被本次改动顺手清掉会把「实体名 + 形态」那套一起弄没。"""
        m = re.search(r"function renderPageCrumb\(.*?\n\}", self.s06, re.S)
        self.assertIn("page === 'chat'", strip_comments(m.group(0)))

    def test_page_labels_kept_for_future_use(self):
        """PAGE_LABELS 仍要留着（页名→中文名唯一映射；test_asset_panel 钉着 assets 那条）。"""
        self.assertIn("assets: '资产'", self.s05)

    # ── ② 十个页面全部迁到统一骨架 ────────────────────────────────────
    def test_all_system_pages_present_and_marked(self):
        for p in SYS_PAGES:
            self.assertIn(p, self.sections, "#page-%s 缺失" % p)
            self.assertIn("sys", self.sections[p].split(">")[0] +
                          self.html[self.html.index('id="page-%s"' % p) - 40:
                                    self.html.index('id="page-%s"' % p)],
                          "#page-%s 必须带 sys 类（页级滚动的那一半靠它）" % p)

    def test_every_system_page_uses_shared_skeleton(self):
        for p in SYS_PAGES:
            seg = self.sections[p]
            self.assertIn('class="sp"', seg, "#page-%s 没有 .sp 骨架" % p)
            self.assertGreaterEqual(seg.count('class="sp-card"'), 1,
                                    "#page-%s 没有 .sp-card 内容卡" % p)

    def test_no_legacy_panel_left_in_system_pages(self):
        """迁移完整性闸：系统页里不许再出现 .panel 自搭（那种页面间距/卡头/滚动
        归属都跟别人不一样 —— 就是本次报障「乱七八糟」的原始形态）。"""
        for p in SYS_PAGES:
            self.assertNotIn("panel", self.sections[p],
                             "#page-%s 还在用 .panel 自搭，没走统一骨架" % p)

    def test_table_pages_use_bounded_scroll_box(self):
        """单表页的表格进限高盒：盒是 sticky 表头的滚动容器，卡片留在视口里。"""
        for p in TABLE_PAGES:
            self.assertIn("sp-bd box flush tscroll", self.sections[p],
                          "#page-%s 的表格缺少 .sp-bd.box.flush.tscroll（表头将不再钉住）" % p)

    def test_shared_kit_css_present(self):
        for rule in (".sp-card", ".sp-hd h3", ".sp-tools", ".sp-bd.box", ".sp-note",
                     ".sp-list", ".sp-grid", ".sp-cell"):
            self.assertIn(rule, self.css, "骨架缺 %s 规则" % rule)
        # 表头 sticky 与它的前提（限高盒）都在
        self.assertIn("thead th { position: sticky", self.css,
                      "表头 sticky 规则被删/改名 ⇒ 长表失去列名锚点")

    def test_no_new_breakpoint_added(self):
        got = set(m.group(1).strip()
                  for m in re.finditer(r"@media\s*([^{]+)\{", self.css))
        self.assertEqual(got - ALLOWED_MEDIA, set(),
                         "系统页骨架不该新增断点档：用 auto-fit/minmax 自己响应式")

    # ── ③ 多选芯片：壳换了，数据源没换 ────────────────────────────────
    def test_mount_picks_exists_and_is_idempotent(self):
        m = re.search(r"function mountPicks\(.*?\n\}", strip_comments(self.s06), re.S)
        self.assertTrue(m, "mountPicks 缺失 ⇒ 多选源会以原生 listbox 形态撑破卡片")
        body = m.group(0)
        self.assertIn("old.remove()", body, "mountPicks 必须幂等（instTo 的 option 会被重写）")
        self.assertIn("o.selected = !o.selected", body, "芯片点击必须写回原生 option")
        self.assertIn("picks-src", body, "原生 select 必须被标记隐藏类（CSS 才敢 display:none）")

    def test_mount_picks_wired_for_all_three_selects(self):
        self.assertIn("['memFedSrcs', 'kbRoutes'].forEach(mountPicks)", self.s06,
                      "boot 期未挂记忆/知识库两处芯片壳")
        m = re.search(r"async function loadSkills\(.*?\n\}", strip_comments(self.s04), re.S)
        self.assertTrue(m)
        self.assertIn("$('instTo').innerHTML = opt", m.group(0))
        idx = m.group(0).index("$('instTo').innerHTML = opt")
        self.assertIn("mountPicks('instTo')", m.group(0)[idx:],
                      "instTo 的 option 被重写后必须重挂芯片壳（否则壳里是旧发现点）")

    def test_selects_remain_the_single_source_of_truth(self):
        """三处多选的 option 清单与读法都不许被芯片壳替换。"""
        # memFedSrcs 11→12（v0.13.103 P2 加 amr_memory）—— 该数字是**前后端同源契约**
        # 的计数，加源时必须同步（这正是本断言的用意：安静加一个源 = 前端收不到它）。
        for sel, n in (("memFedSrcs", 12), ("kbRoutes", 5)):
            m = re.search(r'<select id="%s"[^>]*>(.*?)</select>' % sel, self.html, re.S)
            self.assertTrue(m, "#%s 原生 select 没了 ⇒ 数据源被换成自造状态" % sel)
            self.assertEqual(len(re.findall(r"<option", m.group(1))), n,
                             "#%s 的 option 清单被改动（前后端源清单是同源契约）" % sel)
        self.assertIn("Array.from($('memFedSrcs').selectedOptions)", self.s04)
        self.assertIn("Array.from($('kbRoutes').selectedOptions)", self.s04)
        self.assertIn("Array.from($('instTo').selectedOptions)", self.s04)

    def test_assets_page_keeps_mem_grid_contract(self):
        """资产页窄屏塌单列靠 .mem-grid 自身响应式（verify_asset_panel_live N4 钉着），
        重排后这个容器必须还在。"""
        self.assertIn('class="mem-grid"', self.sections["assets"])

    def test_visible_panels_reveal_as_flex_not_block(self):
        """默认隐藏的卡片显隐只切 inline 值：写死 display:block 会把 flex 列打回块级
        （卡头/卡体的排布立刻错位）。"""
        for fn, el in (("previewCtx", "ctxPanel"), ("openRun", "runPanel")):
            m = re.search(r"function %s\(.*?\n\}" % fn, strip_comments(self.s04), re.S)
            self.assertTrue(m, "%s 缺失" % fn)
            self.assertNotIn("'%s').style.display = 'block'" % el, m.group(0),
                             "%s 里出现了 display:block（应为 ''，让 .sp-card 保持 flex）" % fn)


if __name__ == "__main__":
    unittest.main()
