#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：三中心 UI 统一（v0.13.27 批3）静态闸门。

覆盖四缺口：
- B 统一加载：boxBusy/boxFail 助手存在 + 六 loader 引用它们（失败上屏，不再只 toast）
- C 技能正文：skillDocDrawer 进 OVERLAY_IDS、renderSkillList 有查看入口、
  skillRead 处理 409 候选（err.payload.detail.candidates）
- D 文档树下钻：kbBrowse(sub) 参数化 + kbCrumb 面包屑 + 单层如实提示
- E 联邦检索：page-memory 顶部联邦面板五件套 DOM id + memFedSearch 逐路徽标
"""
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))


class TestCenterUi(unittest.TestCase):

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")
        self.boot = (_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8")
        self.w04 = (_REPO / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
        self.w05 = (_REPO / "static" / "hub" / "05-chat-and-history.js").read_text(encoding="utf-8")

    # ── B 统一加载 ────────────────────────────────────────────────
    def test_busy_fail_helpers_exist(self):
        self.assertIn("function boxBusy(", self.boot)
        self.assertIn("function boxFail(", self.boot)

    def test_six_loaders_use_three_state(self):
        """六个 loader 必须进函数即 busy、catch 走 boxFail（失败上屏）。"""
        for fn in ("loadMemories", "loadSkills", "loadSkillBudget",
                   "kbSearch", "loadKbStatus", "kbBrowse"):
            m = re.search(r"async function %s\(.*?\n\}" % fn, self.w04, re.S)
            self.assertTrue(m, f"{fn} 函数缺失")
            body = re.sub(r"/\*.*?\*/", "", m.group(0), flags=re.S)
            self.assertIn("boxBusy(", body, f"{fn} 缺 boxBusy（busy 态）")
            self.assertIn("boxFail(", body, f"{fn} 缺 boxFail（失败态上屏，不再只 toast）")

    def test_initial_placeholders_unified(self):
        """初始占位统一「加载中…」——不匹配 class（skillList 是 mem-list，其余 hint tscroll）。"""
        for dom_id in ("skillList", "kbStatus", "kbBrowse", "skillBudget"):
            m = re.search(r'id="%s"[^>]*>([^<]*)<' % dom_id, self.html)
            self.assertTrue(m, f"#{dom_id} 初始占位缺失")
            self.assertEqual(m.group(1), "加载中…",
                             f"#{dom_id} 初始占位应为「加载中…」，实为「{m.group(1)}」")

    def test_center_health_global_exists(self):
        self.assertIn("var CENTER_HEALTH", self.boot, "CENTER_HEALTH 必须 var 声明（TDZ）")

    def test_sidebar_health_badges_wired(self):
        """侧栏系统项读 CENTER_HEALTH 挂 s-badge（三中心行）。"""
        self.assertIn("CENTER_HEALTH", self.w05)
        self.assertIn("s-badge", self.w05)

    # ── C 技能正文 ────────────────────────────────────────────────
    def test_skill_doc_drawer_in_overlay_ids(self):
        self.assertIn("'skillDocDrawer'", self.boot,
                      "OVERLAY_IDS 必须含 skillDocDrawer（唯一性/遮罩/导航清收才接管）")

    def test_skill_doc_drawer_html_and_css(self):
        self.assertIn('id="skillDocDrawer"', self.html)
        self.assertIn("#skillDocDrawer.on { right: 0; }", self.html,
                      "skillDocDrawer 需复用抽屉滑出 CSS")

    def test_render_skill_list_has_view_button(self):
        """技能行必须有「查看」入口。

        v0.13.127 P1 改判：inline onclick + jsStr 是**静默 SyntaxError**形态——
        jsStr 出双引号字面量，嵌在双引号界定的 onclick 属性里被 HTML 解析器提前
        截断（渲染出 onclick="skillRead( ⇒ 点击即抛，探针实测抓到，
        自 v0.13.59 起坏了 19 天）。改走 data-* 委托（§4.2 口径），
        断言跟着改机制：行内 data-sk-view 按钮 + 委托绑定函数存在。
        """
        m = re.search(r"function renderSkillList\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m)
        self.assertIn('data-sk-view="1"', m.group(0), "技能行必须有查看入口（data-sk-view 委托）")
        self.assertIn("data-name=", m.group(0), "查看入口必须带技能名（dataset 读回）")
        self.assertIn("function bindSkillViewDelegation(", self.w04,
                      "查看入口必须真的绑上委托（写了 data-* 没人绑 = 死按钮）")

    def test_no_inline_onclick_with_jsstr_in_skill_shard(self):
        """反向扫：04 分片不许再出现 inline onclick 里嵌 jsStr 的形态
        （那正是 v0.13.59 静默回归的成因）。抽屉候选按钮同样只许 data-*。"""
        bad = [l.strip()[:90] for l in self.w04.split("\n")
               if 'onclick="skillRead(' in l]
        self.assertEqual(bad, [], "04 分片仍有 inline onclick+jsStr 的查看入口：%r" % bad)

    def test_skill_read_handles_409_candidates(self):
        """查看链（v0.13.127 P1 起拆成 skillRead 开门 + skillReadDoc 取文两步）：
        skillRead 只许经 openOverlay 开抽屉并委托给 skillReadDoc；skillReadDoc 必须
        调 /api/skill/read、处理 409 candidates（err.payload.detail.candidates）、
        截断显式提示。拆步不拆契约——两边都钉。"""
        m = re.search(r"async function skillRead\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m, "skillRead 缺失")
        body = m.group(0)
        self.assertIn("openOverlay('skillDocDrawer')", body,
                      "抽屉只许经 openOverlay 开（浮层红线②）")
        self.assertIn("skillReadDoc(", body, "skillRead 必须委托 skillReadDoc 取正文")
        d = re.search(r"async function skillReadDoc\(.*?\n\}", self.w04, re.S)
        self.assertTrue(d, "skillReadDoc 缺失")
        dbody = d.group(0)
        self.assertIn("/api/skill/read", dbody)
        self.assertIn("candidates", dbody, "409 多路冲突必须读 err.payload.detail.candidates")
        self.assertIn("truncated", dbody, "截断必须显式提示（不许静默少内容）")

    def test_close_skill_doc_exists(self):
        self.assertIn("function closeSkillDoc()", self.w04)
        self.assertIn("closeSkillDoc()", self.html, "抽屉关闭按钮要接 closeSkillDoc()")

    # ── D 文档树下钻 ──────────────────────────────────────────────
    def test_kb_browse_parameterized(self):
        m = re.search(r"async function kbBrowse\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m)
        body = m.group(0)
        self.assertIn("sub", body, "kbBrowse 必须参数化（sub 下钻）")
        self.assertIn("kbCrumb", body, "面包屑渲染缺失")
        self.assertIn("暂只支持下钻一层", body, "单层限制必须如实提示（不装多层）")

    def test_kb_crumb_dom_exists(self):
        self.assertIn('id="kbCrumb"', self.html)

    # ── E 联邦检索 ────────────────────────────────────────────────
    def test_memfed_panel_five_pieces(self):
        for dom_id in ("memFedQ", "memFedSrcs", "memFedResults", "memFedBadges", "memFedHint"):
            self.assertIn(f'id="{dom_id}"', self.html,
                          f"#{dom_id} 缺失 ⇒ 联邦检索面板残缺")

    def test_memfed_sources_match_registry(self):
        """前端 source 枚举与 memfed REGISTRY 同源（防两处漂移）。
        v0.13.28 改动态循环：新增源自动入钉，不再靠硬编码 id 清单（漂移已实测发生一次）。"""
        import memfed
        m = re.findall(r'<option value="(\w+)"', self.html)
        got = set(m)
        for sid in memfed.enabled_ids():
            self.assertIn(sid, got, f"联邦源 {sid} 前端缺失")

    def test_memfed_search_renders_backends_badges(self):
        m = re.search(r"async function memFedSearch\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m, "memFedSearch 缺失")
        body = m.group(0)
        self.assertIn("/api/memory/search", body)
        # v0.13.28 徽标渲染抽到共用 fedBadges(d)（两页同构）——断言跟新落点
        self.assertIn("fedBadges(d)", body, "逐路徽标必须走 fedBadges 共用渲染器")
        self.assertIn("degraded", body, "降级路必须点名")
        fb = re.search(r"function fedBadges\(.*?\n\}", self.w04, re.S)
        self.assertTrue(fb, "fedBadges 共用渲染器缺失")
        self.assertIn("backends", fb.group(0), "fedBadges 必须读 backends（不糊成绿）")

    def test_memfed_not_autoloaded(self):
        """按需触发：go('memory') 不自动跑联邦检索（防埋点污染+无谓开销）。"""
        m = re.search(r"page === 'memory'.*?\n", self.boot)
        self.assertTrue(m)
        self.assertNotIn("memFedSearch", m.group(0), "go() 不得自动触发联邦检索")

    # ── v0.13.28 批3：检索回退（用户痛点「搜完无法回退」） ─────────
    def test_query_bars_exist(self):
        for dom_id in ("memFedQueryBar", "kbQueryBar"):
            self.assertIn(f'id="{dom_id}"', self.html, f"#{dom_id} 缺失 ⇒ 无法回退")

    def test_clear_functions_restore_initial(self):
        """Clear 必须还原初始文案 + 清 hint/badges/输入框——回退到「未检索」态。"""
        for fn, init, hint, badges, inp in (
                ("memFedClear", "输入关键词开始联邦检索", "memFedHint", "memFedBadges", "memFedQ"),
                ("kbClear", "输入关键词开始检索", "kbHint", "kbBadges", "kbQ")):
            m = re.search(r"function %s\(\).*?\n\}" % fn, self.w04, re.S)
            self.assertTrue(m, f"{fn} 缺失")
            body = m.group(0)
            self.assertIn(init, body, f"{fn} 必须还原初始文案「{init}」")
            for ref in (hint, badges, inp):
                self.assertIn(ref, body, f"{fn} 必须清 #{ref}")

    def test_clear_does_not_touch_center_health(self):
        """清空结果不许洗掉侧栏健康点（体检态与检索态语义解耦）。
        去注释后查代码本体（函数头说明注释里出现该词不算写）。"""
        for fn in ("memFedClear", "kbClear"):
            m = re.search(r"function %s\(\).*?\n\}" % fn, self.w04, re.S)
            code = re.sub(r"/\*.*?\*/", "", m.group(0), flags=re.S)
            self.assertNotIn("CENTER_HEALTH", code,
                             f"{fn} 不许写 CENTER_HEALTH（清空≠洗健康态）")

    def test_render_query_bar_used_by_both_searches(self):
        for fn in ("memFedSearch", "kbSearch"):
            m = re.search(r"async function %s\(.*?\n\}" % fn, self.w04, re.S)
            self.assertIn("renderQueryBar(", m.group(0), f"{fn} 必须渲染 QueryBar（回退入口）")

    # ── v0.13.28 批4：源徽标（替换裸拼 tag 类名） ───────────────────
    def test_src_abbr_covers_all_fed_sources(self):
        """SRC_ABBR 必须覆盖全部启用源 + kb 路名（缺=回退前两位截断，可辨识性差）。"""
        import memfed
        for sid in memfed.enabled_ids():
            self.assertIn(f"{sid}:", self.w04, f"SRC_ABBR 缺 {sid}")
        for kb_route in ("turbovec", "tdai", "local"):
            self.assertIn(f"{kb_route}:", self.w04, f"SRC_ABBR 缺 kb 路名 {kb_route}")

    def test_src_tag_replaces_bare_class_concat(self):
        """kb 结果卡不得再裸拼 `tag + 源名`（CSS 无定义静默灰底的根因）。"""
        self.assertIn("function srcTag(", self.w04)
        m = re.search(r"async function kbSearch\(.*?\n\}", self.w04, re.S)
        self.assertIn("srcTag(", m.group(0), "kbSearch 结果卡必须走 srcTag()")
        self.assertNotIn("class=\"tag ' + (r.source", m.group(0),
                         "旧裸拼形态必须移除")

    def test_src_tag_css_variants_defined(self):
        for cls in (".tag.src-doc", ".tag.src-session", ".tag.src-index"):
            self.assertIn(cls, self.html, f"{cls} CSS 变体缺失")

    def test_kb_results_have_path_row(self):
        """kb 结果卡必须有路径行（溯源）+ 下钻按钮（kbBrowse）。"""
        m = re.search(r"async function kbSearch\(.*?\n\}", self.w04, re.S)
        body = m.group(0)
        self.assertIn("r.path", body, "路径行必须取 r.path")
        self.assertIn("kbBrowse(", body, "下钻按钮必须接 kbBrowse()")


class TestMemoryLegacyMerged(unittest.TestCase):
    """L0：本机旧层（L1/L2/L3）已并入记忆中心（2026-10-25 用户令）。

    合并的判据不是「文案写了合并」，而是四条可证伪的东西：
      ① 写入口真的在同一页（DOM id 在 #page-memory 段落内）；
      ② 独立设置页真的没了（防止两份 DOM 并存 → 一份没人维护）；
      ③ 展开才加载（首屏 1 请求 / 0 按钮的 P1 判据不许被合并破坏）；
      ④ 列表加载不许再写 CENTER_HEALTH（体检态只归 memVitals）。
    """

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.w04 = (_REPO / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
        self.w01 = (_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8")
        self.w05 = (_REPO / "static" / "hub" / "05-chat-and-history.js").read_text(encoding="utf-8")
        start = self.html.index('id="page-memory"')
        end = self.html.index("</section>", start)
        self.mem_page = self.html[start:end]

    def test_legacy_controls_live_in_memory_page(self):
        for dom_id in ("mxLegacyBox", "memQ", "memList", "memNew", "memNewCat",
                       "l2content", "l2manual", "l3content", "l3manual", "mxStale"):
            self.assertIn('id="%s"' % dom_id, self.mem_page,
                          f"#{dom_id} 不在记忆中心页内 ⇒ 写入口又分居两处")

    def test_settings_memory_page_removed(self):
        self.assertNotIn('id="page-settings-memory"', self.html,
                         "设置 · 记忆存储页仍在 ⇒ 两份 DOM 并存")
        self.assertNotIn("settings-memory", self.w05,
                         "侧栏仍在渲染设置 · 记忆存储")
        self.assertNotIn("page-settings-memory", self.w01,
                         "go() 仍挂 settings-memory 的进页钩子")

    def test_legacy_block_is_lazy(self):
        """进页不得拉旧层：只在 mxLegacyToggle 里加载。"""
        m = re.search(r"function mxLegacyToggle\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m, "mxLegacyToggle 缺失 ⇒ 折叠块展开后不加载")
        body = m.group(0)
        for ref in ("loadMemories()", "loadDoc('l2')", "loadDoc('l3')"):
            self.assertIn(ref, body, "折叠块展开必须拉 %s" % ref)
        go_mem = re.search(r"page === 'memory'.*?\n", self.w01)
        self.assertTrue(go_mem)
        self.assertNotIn("loadMemories", go_mem.group(0),
                         "go('memory') 不得自动加载本机旧层（首屏 1 请求）")

    def test_load_memories_does_not_write_center_health(self):
        m = re.search(r"async function loadMemories\(.*?\n\}", self.w04, re.S)
        self.assertTrue(m)
        code = re.sub(r"/\*.*?\*/", "", m.group(0), flags=re.S)
        self.assertNotIn("CENTER_HEALTH", code,
                         "loadMemories 不许写真假健康点（探针结论只归 memVitals）")

    def test_update_endpoint_wired(self):
        """编辑功能必须真的落到 PUT /api/memory/l1/{id}（不是只画了个铅笔图标）。"""
        self.assertIn("async function saveMemoryEdit(", self.w04)
        m = re.search(r"async function saveMemoryEdit\(.*?\n\}", self.w04, re.S)
        self.assertIn("method: 'PUT'", m.group(0))
        self.assertIn("/api/memory/l1/' + id", m.group(0))


if __name__ == "__main__":
    unittest.main(verbosity=2)
