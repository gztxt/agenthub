#!/usr/bin/env python3
"""L0 hermetic 测试：D7 技能中心前端（`templates/index.html` + `static/hub/04-terminal-ws.js`）。

**为什么是 L0 而不是计划书写的 `tests/verify_skill_center_ui.py`**：
仓内 `tests/README.md` 的分层口径是——`verify_*.py`/`probe_*.py` 属 **L2 live，需要服务在跑、
手工单跑、不被 `discover -p "test_*.py"` 收进来**，而 pre-commit 只跑 `run_tests.sh hermetic`。
按计划书那个命名写出来的东西**在提交时根本不会跑**，那不叫闸门，叫摆设。
本文件是纯静态断言（只读三个文件、零服务零网络），因此归 L0，每��提交必过。

**这 6 条为什么必须钉死**：本仓吃过「页面里写死了一个数」的亏——技能页曾长期显示「7 路发现点」，
而真实路由数早就被 D1 改成 20。页面上**第二个真相**（HTML 写死的文案）比第一个真相更危险，
因为它看起来永远正确、不会随后端变。

另外钉住**窄屏断点唯一**（工作区分档偏好五条不变量第 3 条）：JS 与 CSS 必须同源
（`matchMedia('(max-width:767px)')` 与 `@media` 同值），禁写第二处 `innerWidth < 768`。
"""
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
INDEX = _REPO / "templates" / "index.html"
HUBJS = _REPO / "static" / "hub" / "04-terminal-ws.js"
CORE = _REPO / "static" / "hub" / "01-core-boot.js"

HTML = INDEX.read_text(encoding="utf-8")
JS = HUBJS.read_text(encoding="utf-8")
COREJS = CORE.read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """剥掉块注释、行注释与行尾注释，只留真代码。

    **为什么必须有它**：本文件第一版的 rerank 断言是拿注释里的说明字去过的
    （代码里是 `'&n=5&rerank=' + (useJudge ? 'true' : 'false')`，并无 `rerank=false` 字面量）。
    「注释里写了」被当成了「代码里做了」——而这两件事在本仓里**经常不是一回事**
    （TDZ、vitals_loop 函数头丢失、跨档镜像声明，都属同一个坑）。
    """
    out = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    out = re.sub(r"^\s*//.*$", "", out, flags=re.M)
    out = re.sub(r"(?<![:'\"])//[^\n]*$", "", out, flags=re.M)
    return out


class TestNoSecondSourceOfTruth(unittest.TestCase):
    """① 页面里不得存在第二个真相：路数/条数一律来自后端字段。"""

    def test_no_hardcoded_route_count_in_html(self):
        # 「7 路发现点」「7个发现点」这类写死文案必须消失——D1 已把 7 路改成 20 路。
        bad = re.findall(r"[0-9]+\s*路发现点", HTML)
        self.assertEqual(bad, [], "index.html 仍写死路数：%r（路数只能来自后端字段）" % bad)

    def test_no_hardcoded_skill_total_in_html(self):
        bad = re.findall(r"[0-9]+\s*个技能", HTML)
        self.assertEqual(bad, [], "index.html 仍写死技能总数：%r" % bad)

    def test_route_count_comes_from_backend_in_js(self):
        """路数必须由后端返回的 routes 长度算出来。"""
        self.assertRegex(JS, r"SKILL_ROUTES\.length")


class TestZombieBadgeFields(unittest.TestCase):
    """② 列表项要能显示「从未调用」徽章与末次调用时间（数据源：/api/skill/zombies）。"""

    def test_js_calls_zombies_endpoint(self):
        self.assertIn("/api/skill/zombies", JS, "列表未接零调用榜，前端拿不到末次调用时间")

    def test_js_calls_status_endpoint(self):
        self.assertIn("/api/skill/status", JS, "自检卡未接 /api/skill/status")

    def test_never_called_badge_rendered(self):
        self.assertIn("从未调用", JS, "缺「从未调用」徽章文案")

    def test_zombie_only_filter_chip_exists(self):
        self.assertIn("skillZombieOnly", JS, "缺「只看零调用」筛选芯片（id=skillZombieOnly）")

    def test_confidence_ceiling_is_shown_not_hidden(self):
        """僵尸榜的置信度封顶必须显示出来——藏起来的 medium 会被读成 high。"""
        self.assertRegex(JS, r"confidence")
        self.assertIn("medium", JS)


class TestSelfCheckCard(unittest.TestCase):
    """③ 发现点自检卡：覆盖全部 backends + 排除段。"""

    def test_selfcheck_container_exists_in_html(self):
        self.assertIn('id="skillSelfCheck"', HTML)

    def test_excluded_section_rendered(self):
        self.assertIn("排除", JS, "自检卡未渲染排除段（EXCLUDED_DIRS 必须在界面上公开理由）")

    def test_four_states_rendered(self):
        """一路四态 ok/empty/missing/error 必须都能显示，否则「没装」和「配错」又混成一句话。"""
        for st in ("ok", "empty", "missing", "error"):
            self.assertIn(st, JS, "自检卡未处理 state=%s" % st)

    def test_skipped_outside_count_shown(self):
        self.assertIn("skipped_outside", JS, "自检卡未显示白名单外拒读数（看见 vs 拒读必须可区分）")


class TestRelevanceLab(unittest.TestCase):
    """④ 相关性实验室：打 /api/skill/relevant，并显示 BM25/本地判定分开 + 耗时。"""

    def test_lab_container_exists_in_html(self):
        self.assertIn('id="skillLab"', HTML)

    def test_js_calls_relevant_endpoint(self):
        self.assertIn("/api/skill/relevant", JS)

    def test_lab_input_has_id(self):
        self.assertIn('id="skillLabQ"', HTML)

    def test_shows_matched_terms_and_took(self):
        self.assertIn("matched", JS, "实验室未显示命中词——只给分数答不出「为什么这条排第一」")
        self.assertIn("took_ms", JS, "实验室未显示耗时")

    def test_lab_defaults_to_bm25_only(self):
        """**默认 rerank=false 是硬要求**：实测 rerank=true 时该端点 took_ms=1064.5ms，
        而 hub-facade 的 input 钩子预算是 600ms ⇒ 默认必须关，否则每次都超时降级。

        ⚠ 第一版这条断言写成 `assertIn("rerank=false", JS[端点起 400 字])`，
        结果**蒙对了**——代码里根本没这个字面量，命中的是上方注释里那句说明。
        「文字存在 ≠ 已生效」那一族（TDZ / vitals_loop / 跨档镜像声明 同族）。
        本版改成剥掉注释后判**代码**，并补一条「开关默认不勾」的断言：
        能得到 rerank=true 的唯一路径是那个 checkbox，而它出厂未勾。
        """
        code = strip_js_comments(JS)
        i = code.find("/api/skill/relevant")
        self.assertNotEqual(i, -1, "未找到相关性调用")
        seg = code[i:i + 400]
        self.assertIn("rerank=", seg, "相关性调用没有显式传 rerank（依赖后端默认 = 隐式依赖）")
        self.assertIn("'true' : 'false'", seg, "rerank 必须是显式二选一，不是跟随后端默认")

    def test_judge_toggle_ships_unchecked(self):
        """HTML 里的本地判定开关出厂不得带 checked —— 带了就等于默认开。"""
        m = re.search(r'<input[^>]*id="skillLabJudge"[^>]*>', HTML)
        self.assertIsNotNone(m, "缺少本地判定开关元素，无法断言其默认值")
        self.assertNotIn("checked", m.group(0),
                         "判定开关出厂被勾上 = 每次多花数秒 CPU 推理（rerank=true）")


class TestBudgetCard(unittest.TestCase):
    """⑤ 注入预算卡：full / name_only / truncated 三档分区齐全 + 一句后果说明。"""

    def test_three_zones_rendered(self):
        for z in ("full", "name_only", "truncated"):
            self.assertIn(z, JS, "预算卡缺 %s 分区" % z)

    def test_consequence_explained(self):
        """被裁 = 注入时 agent 看不到它。这句话必须写在界面上，否则用户只看到数字。"""
        self.assertRegex(JS, r"被裁|看不到")


class TestNarrowBreakpointSingleSource(unittest.TestCase):
    """⑥ 断点唯一：禁第二处 innerWidth < 768（不变量 3：JS 与 CSS 必须同源）。"""

    def test_core_boot_defines_the_single_breakpoint(self):
        self.assertIn("HUB_NARROW_MQ = window.matchMedia('(max-width: 767px)')", COREJS)

    def test_no_second_innerwidth_breakpoint(self):
        """**规则注释本身会命中**（index.html 里写着「不得另写 innerWidth<768」），
        所以必须先剥掉注释再判，否则闸门会被自己的规则文本绊倒。
        这个文件里的规则原文藏在**三种**注释里，都得剥：
          ① HTML 注释 `<!-- -->`（第一版漏了 → 误报 index.html:914）
          ② CSS 块注释 `/* */`（index.html 的 <style> 里，漏了 → 误报 index.html:912）
          ③ JS 行注释 `//`
        闸门写错就改闸门，不去改对的代码。反向验证过：往 11-resources.js 里真塞一处
        `innerWidth < 768`，闸门会报出 `11-resources.js:297`（不是只会绿）。
        """
        offenders = []
        for f in sorted((_REPO / "static" / "hub").glob("*.js")) + [INDEX]:
            raw = f.read_text(encoding="utf-8")
            if f.suffix == ".html":
                raw = re.sub(r"<!--.*?-->", "", raw, flags=re.S)
            raw = re.sub(r"/\*.*?\*/", "", raw, flags=re.S)   # CSS/JS 块注释
            for i, line in enumerate(raw.splitlines(), 1):
                code = line.split("//")[0]
                if re.search(r"innerWidth\s*<\s*7[0-9][0-9]", code):
                    offenders.append("%s:%d" % (f.name, i))
        self.assertEqual(offenders, [], "出现第二处窄屏断点：%r" % offenders)

    def test_new_skill_card_does_not_introduce_its_own_media_query(self):
        """D7 新增的卡不得自带 @media 断点值——要么复用现有值，要么根本不写。"""
        skill_ui = JS[JS.find("技能中心"):JS.find("知识库中心")] if "技能中心" in JS else ""
        self.assertNotIn("max-width: 768px", skill_ui)
        self.assertNotIn("max-width: 767px", skill_ui)


class TestTwoViewTabs(unittest.TestCase):
    """v0.13.127 P1（Grill 裁定 5）：技能目录 / 调用记录两视图。

    钉三条：① tab 与两个视图容器都在；② 调用记录**懒加载**（切到才拉，
    首屏请求数不因它增加）；③ 视图状态**不写盘**（分档偏好五条之②：
    加载不写盘——tab 是视图开关不是设备分档，无需记忆）。
    """

    def test_tabs_and_view_containers_exist(self):
        for dom_id in ("skillTabCatalog", "skillTabInvocations",
                       "skillViewCatalog", "skillViewInvocations"):
            self.assertIn('id="%s"' % dom_id, HTML, "#%s 缺失 ⇒ 两视图残缺" % dom_id)

    def test_switch_function_wires_both(self):
        m = re.search(r"function switchSkillView\(.*?\n\}", JS, re.S)
        self.assertTrue(m, "switchSkillView 缺失")
        body = m.group(0)
        self.assertIn("catalog", body)
        self.assertIn("invocations", body)

    def test_invocations_lazy_loaded_on_first_switch(self):
        """切到调用记录才拉数据（SKILL_INV_LOADED 守卫），go('skills') 不自动拉。"""
        m = re.search(r"function switchSkillView\(.*?\n\}", JS, re.S)
        self.assertIn("SKILL_INV_LOADED", m.group(0), "缺懒加载守卫")
        self.assertIn("loadSkillInvocations(", m.group(0))

    def test_view_state_not_persisted_to_disk(self):
        """分档偏好五条之②：加载不写盘。switchSkillView 不许碰 localStorage。"""
        m = re.search(r"function switchSkillView\(.*?\n\}", JS, re.S)
        code = strip_js_comments(m.group(0))
        self.assertNotIn("lsSet", code, "视图状态不许写盘（首屏只读偏好）")
        self.assertNotIn("localStorage", code, "视图状态不许写 localStorage")


class TestInvocationsPanel(unittest.TestCase):
    """调用记录面板：数据源 /api/skill/invocations（零新表只读投影）。"""

    def test_containers_exist(self):
        for dom_id in ("skillInvocations", "skillInvSummary", "invName", "invCaller", "invDays"):
            self.assertIn('id="%s"' % dom_id, HTML, "#%s 缺失 ⇒ 调用记录面板残缺" % dom_id)

    def test_js_calls_invocations_endpoint(self):
        self.assertIn("/api/skill/invocations", JS)

    def test_loader_uses_three_state(self):
        m = re.search(r"async function loadSkillInvocations\(.*?\n\}", JS, re.S)
        self.assertTrue(m)
        body = strip_js_comments(m.group(0))
        self.assertIn("boxBusy(", body)
        self.assertIn("boxFail(", body, "调用流水读不到要上屏表态，不装成空面板")

    def test_bad_detail_counted_not_fatal(self):
        """坏 detail 行由服务端计数（bad_detail）——前端要显示它，
        一行脏数据不许打挂整个面板（与服务端 _pick_extra 同一条纪律）。"""
        self.assertIn("bad_detail", JS)

    def test_summary_scope_is_window_not_history(self):
        """汇总是抓取窗口口径，界面必须照抄 scope——写成「全部历史」就是第二个真相。"""
        self.assertIn("window_fetched", JS)
        self.assertIn("不是全历史", JS)

    def test_row_carries_route_and_opens_that_route(self):
        """逐行必须显示技能来源路（by_route），且「查看」要带 route。

        只显示技能名时，同名技能跨路无法区分；data-route 留空还会让 read 走 409。
        """
        m = re.search(r"function renderSkillInvocations\(.*?\n\}", JS, re.S)
        self.assertTrue(m, "renderSkillInvocations 缺失")
        body = strip_js_comments(m.group(0))
        self.assertIn("by_route", body)
        self.assertIn("x.route", body)
        self.assertIn('data-route="\' + escapeHtml(route)', body,
                      "查看按钮必须用流水里的 route，不能写死空串")

    def test_success_status_not_rendered_as_warning(self):
        """运行日志成功态是 'success'；旧判断只认 'ok' ⇒ 每次成功都挂警告色。"""
        m = re.search(r"function renderSkillInvocations\(.*?\n\}", JS, re.S)
        body = strip_js_comments(m.group(0))
        self.assertIn("'success'", body)


class TestInvocationLayout(unittest.TestCase):
    """调用记录的文字排版（2026-10-20 用户报「文字没对齐、主题不清晰」）。

    根因不是配色而是**容器与换行手段不匹配**：`.sk-inv-sum` 是 `display:flex`，
    JS 却用 `<br>` 分行——`<br>` 在 flex 容器里只是一个空 flex 项，不产生换行，
    三行文案被摊成一串长度不齐的横向项。判据必须钉在「用块级行 + grid 两栏」，
    否则有人再把 `<br>` 加回来也照样能过（样式在、结构没变）。
    """

    def test_summary_is_two_column_grid(self):
        css = re.search(r"\.sk-inv-sum\s*\{[^}]*\}", HTML).group(0)
        self.assertIn("grid-template-columns: max-content minmax(0, 1fr)", css,
                      "汇总区必须是「标签列自适应 + 值列拉伸」的网格，值才对齐")
        body = re.search(r"function renderSkillInvocations\(.*?\n\}", JS, re.S).group(0)
        seg = body[body.index("if (sumEl)"):body.index("el.innerHTML = items.map")]
        self.assertNotIn("'<br>", seg,
                         "汇总区不许用 <br> 分行（flex 容器里它不换行）")
        self.assertIn("sk-k", seg)
        self.assertIn("sk-v", seg)

    def test_rows_use_two_column_grid_with_aligned_side(self):
        css = re.search(r"\.sk-inv-row\s*\{[^}]*\}", HTML).group(0)
        self.assertIn("grid-template-columns", css, "流水行必须是网格，右列才能逐行对齐")
        self.assertIn("minmax(0, 1fr)", css, "主列要能收缩，否则长任务文本撑破布局")
        meta = re.search(r"\.sk-inv-meta\s*\{[^}]*\}", HTML).group(0)
        self.assertIn("tabular-nums", meta, "时间/耗时必须等宽数字，否则右边缘参差")
        body = re.search(r"function renderSkillInvocations\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("sk-inv-row", body)
        self.assertIn("sk-inv-side", body)

    def test_status_has_explicit_text_not_only_color(self):
        """成功/失败要有文字（颜色只是辅助）：只靠色相区分在灰度屏与色弱下失效。"""
        body = re.search(r"function renderSkillInvocations\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("sk-inv-ok", body)
        self.assertIn("成功", body)


class TestStoreView(unittest.TestCase):
    """技能商店（PT-20261020-02）：搜索 / 安装 / 装后调用。

    三条判据：
    ① 商店是**第三个视图**（tab + panel 同 id 族，switchSkillView 认它）；
    ② 安装的目标只来自后端返回的 targets（前端不拼路径）；
    ③ 不接在线执行（`skillhub run` 会把正文发到第三方服务）。
    """

    def test_tab_and_panel_exist(self):
        self.assertIn('id="skillTabStore"', HTML)
        self.assertIn('id="skillViewStore"', HTML)
        self.assertIn('data-skill-view="store"', HTML)
        body = re.search(r"function switchSkillView\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("'store'", body, "switchSkillView 不认识第三个视图")

    def test_targets_come_from_backend(self):
        body = re.search(r"async function installStore\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("/api/skill/store/install", body)
        self.assertIn("slug", body)
        self.assertIn("route", body)
        # 目标下拉只由后端 targets 填充；禁止前端写死路径（只查商店相关函数，别查整份 JS）
        seg = re.search(r"function _fillStoreTargets\(.*?(?=/\* 顶部「刷新」)", JS, re.S).group(0)
        self.assertNotIn("/home/", seg, "商店代码里不许出现绝对路径")
        self.assertIn("_fillStoreTargets", JS)
        self.assertIn("/api/skill/store/status", JS)

    def test_no_remote_execution_path(self):
        """`skillhub run` 是**在线执行**：正文会离开本机，本视图不接。

        只查代码不查注释：模块注释里正好写着「不接 skillhub run」，
        拿全文查会把「声明不做」当成「做了」——这是本仓遇到过的判据量错对象。
        """
        code = strip_js_comments(JS)
        self.assertNotIn("skillhub run", code)
        self.assertNotIn("/store/run", code)

    def test_install_buttons_use_delegation(self):
        """结果来自远端 ⇒ 不许 inline onclick（会把 slug 拼进 HTML 事件属性）。"""
        self.assertIn("bindStoreActions", JS)
        self.assertIn("data-store-install", JS)
        seg = re.search(r"function renderStore\(.*?\n\}", JS, re.S).group(0)
        self.assertNotIn("onclick=", seg)

    def test_auto_policy_is_rendered_from_backend(self):
        """自动兜底的开关/目标/额度只准来自 `/store/status` 的 `auto` 段。

        前端若能自己写死 6 次/小时、techdocs 路径，界面就会在服务端改配置后
        继续说旧话（「还能装」→ 实际 429）。
        """
        self.assertIn('id="storeAutoLine"', HTML)
        body = re.search(r"function _renderStoreMeta\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("auto.enabled", body)
        self.assertIn("auto.max_per_hour", body)
        self.assertIn("auto.used_last_hour", body)
        self.assertNotIn("/home/", body, "额度/目标不许由前端拼路径")
        self.assertNotIn("AUTO_MAX", body)

    def test_install_target_defaults_to_auto_route(self):
        """默认目标必须是自动兜底那个（`auto.route`），不是下拉第一项。

        面板同时显示「自动兜底 → techdocs」与「安装目标 claude」时，用户点安装
        会把技能写进另一个 agent 的发现点——实测过（2026-10-20 真浏览器截图）。
        """
        body = re.search(r"function _fillStoreTargets\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("preferred", body)
        self.assertIn("sel.value = preferred", body)
        call = re.search(r"_fillStoreTargets\(STORE_STATE\.targets[^)]*\)", JS).group(0)
        self.assertIn("auto", call, "调用点必须把 auto.route 传进去")

    def test_store_rows_show_choice_signals(self):
        """同类技能多 ⇒ 行里必须摆出选择依据（下载量、是否要自带 Key）。

        `downloads=null`（CLI 兜底路径）要写「未知」而不是 `0`——把未知画成 0
        等于告诉用户「没人用过」，那是编数据。
        """
        body = re.search(r"function renderStore\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("fmtCount(it.downloads)", body)
        self.assertIn("it.needs_key", body)
        self.assertIn("下载未知", body)
        self.assertNotIn("5187606", body, "不许把某个具体技能的下载量写死进前端")
        self.assertNotIn("requires_api_key", body, "字段名归后端，前端只读归一化后的 needs_key")

    def test_install_reports_security_and_needs_explicit_force(self):
        """安装要带安全结论；确认可疑仍要装必须**显式 force**（前端只在用户确认后传）。"""
        body = re.search(r"async function installStore\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("check_security: true", body)
        self.assertIn("force: !!force", body)
        self.assertIn("confirm(", body, "可疑拦截后必须先问人，不许静默重试")
        self.assertIn("d.security", body, "装完要把安全结论回显给用户")

    def test_auth_entry_is_password_input_and_delegated(self):
        """账号入口：Token 输入必须 type=password，且走 data-* 委托（值不进事件属性）。"""
        body = re.search(r"function _renderStoreMeta\(.*?\n\}", JS, re.S).group(0)
        self.assertIn('type="password"', body)
        self.assertIn("data-store-login", body)
        self.assertNotIn("onclick=", body)
        login = re.search(r"async function storeLogin\(.*?\n\}", JS, re.S).group(0)
        self.assertIn("/api/skill/store/login", login)
        self.assertIn("inp.value = ''", login, "提交后必须清空输入框，不在页面上留副本")


class TestInvokeButton(unittest.TestCase):
    """调用（操作）与查看分钮（裁定 3）：invoke 记账、read 不记账，两个入口。"""

    def test_js_posts_invoke_with_caller_web(self):
        m = re.search(r"async function skillInvokeCur\(.*?\n\}", JS, re.S)
        self.assertTrue(m, "skillInvokeCur 缺失")
        body = m.group(0)
        self.assertIn("/api/skill/invoke", body)
        self.assertIn("caller", body)
        self.assertIn("web", body, "web 面板调用必须自报 caller=web（自报身份，服务端落 caller_reported）")

    def test_invoke_button_exists_in_drawer(self):
        self.assertIn("skillInvokeCur()", HTML)

    def test_read_button_exists_in_drawer(self):
        """查看（/read 不记账）与调用（/invoke 记账）是抽屉里两个分开的按钮。"""
        self.assertIn("skillReadDoc()", HTML)

    def test_invoke_failure_surfaces_409_candidates(self):
        """调用失败同样要能选路（与查看链同口径），不静默失败。"""
        m = re.search(r"async function skillInvokeCur\(.*?\n\}", JS, re.S)
        self.assertIn("candidates", m.group(0))


class TestStatsBarAndBadges(unittest.TestCase):
    """统计条 + 调用徽标 + digest 指纹：全部来自后端字段，页面不存第二个真相。"""

    def test_stats_container_and_renderer_exist(self):
        self.assertIn('id="skillStats"', HTML)
        m = re.search(r"function renderSkillStats\(.*?\n\}", JS, re.S)
        self.assertTrue(m, "renderSkillStats 缺失")

    def test_invoke_badge_source_is_backend_summary(self):
        """徽标数来自 /api/skill/invocations 的 summary.by_skill——不是前端自己数的
        「页面上点过几次」（那统计的是点击不是调用）。"""
        m = re.search(r"function renderSkillList\(.*?\n\}", JS, re.S)
        self.assertIn("SKILL_INV_BY_SKILL", m.group(0))
        self.assertIn("d.summary", JS, "徽标必须读后端 summary（不是前端计数）")
        self.assertIn("by_skill", JS, "徽标必须取 by_skill 分组")

    def test_digest_fingerprint_rendered(self):
        """digest 指纹（整包摘要）展示在行上——「技能是否被改过」的比对依据。"""
        m = re.search(r"function renderSkillList\(.*?\n\}", JS, re.S)
        self.assertIn("pkg_digest", m.group(0))
        self.assertIn("sk-fp", HTML, "指纹样式类缺失")

    def test_degraded_routes_computed_from_status(self):
        """/status 无顶层 degraded 字段——降级路必须从 disk[*].available 现算，
        读一个不存在的键然后当成「没有降级」就是静默假绿。"""
        m = re.search(r"function renderSkillStats\(.*?\n\}", JS, re.S)
        self.assertIn("available", m.group(0))

    def test_refresh_invocations_also_refreshes_tab_badge(self):
        """调用记录页刷新必须同时刷徽标：徽标固定近 7 天，视图窗口可切 30/90 天，
        两者不是同一份数据。实测缺口：列表 4 条、徽标仍 3。"""
        m = re.search(r"function refreshSkillView\(.*?\n\}", JS, re.S)
        self.assertTrue(m, "refreshSkillView 缺失")
        body = strip_js_comments(m.group(0))
        self.assertIn("loadSkillInvocations(", body)
        self.assertIn("loadSkillInvBadges(", body)


class TestInlineTabsAndInteraction(unittest.TestCase):
    """2026-10-19 用户裁定：tab 跟在「技能中心」文字后面（不另起一行）+ 操作逻辑优化。

    钉住四类回归：
      ① tablist 必须在 .sk-titlebar 内、紧跟 h3（有人再包一层 .sp-tools 就红）；
      ② ARIA tabs 契约（aria-controls / aria-labelledby / roving tabindex / 方向键）；
      ③ 目录行整行可点、搜索清除、排序下拉、结果计数都在；
      ④ 调用记录空状态必须解释「为什么是 0」与 401 的下一步，不许只写一行空文案。
    """

    def test_tabs_are_inline_with_title(self):
        self.assertRegex(
            HTML,
            r'<div class="sp-hd">\s*<div class="sk-titlebar">\s*<h3>技能中心</h3>\s*'
            r'<div class="sk-tabs" id="skillTabs"',
            "tab 必须与「技能中心」同处 .sk-titlebar（用户要求：不另起一行）")
        # 两个 tab 都不许再挂 inline onclick（§4.2 委托口径；也避免 jsStr 旧坑）
        self.assertNotIn('onclick="switchSkillView', HTML)

    def test_aria_tabs_contract(self):
        self.assertIn('role="tablist"', HTML)
        self.assertIn('aria-controls="skillViewCatalog"', HTML)
        self.assertIn('aria-controls="skillViewInvocations"', HTML)
        self.assertIn('role="tabpanel" aria-labelledby="skillTabCatalog"', HTML)
        self.assertIn('role="tabpanel" aria-labelledby="skillTabInvocations"', HTML)
        self.assertIn("tab.tabIndex = on ? 0 : -1", JS, "缺 roving tabindex")
        for key in ("ArrowRight", "ArrowLeft", "Home", "End"):
            self.assertIn(key, JS, "tab 键盘导航缺 %s" % key)

    def test_row_click_opens_detail(self):
        """目录行整行可点（右侧「查看」仍是独立按钮）。"""
        self.assertIn('data-sk-row="1"', JS, "目录行未挂整行点击标记")
        self.assertIn("closest('[data-sk-row]')", JS, "委托未处理整行点击")
        self.assertIn("'skillList', 'skillInvocations', 'skillDocDrawer'", JS,
                      "调用记录行未接入查看委托")

    def test_search_clear_sort_and_count(self):
        for dom_id in ("skillQClear", "skillSort", "skillCount", "invCount"):
            self.assertIn('id="%s"' % dom_id, HTML, "#%s 缺失" % dom_id)
        self.assertIn("function clearSkillFilter()", JS)
        self.assertIn("function clearInvFilters()", JS)
        m = re.search(r"function renderSkillList\(.*?\n\}", JS, re.S)
        self.assertTrue(m)
        self.assertIn("skillSort", m.group(0), "排序下拉未接进目录渲染")

    def test_refresh_is_contextual(self):
        """一个刷新入口按当前视图刷新，避免两个视图各一个按钮点错。"""
        self.assertIn("function refreshSkillView()", JS)
        self.assertIn('id="skillRefresh"', HTML)
        self.assertNotIn('onclick="loadSkills()"><svg', HTML)

    def test_invocations_empty_state_explains_zero_and_401(self):
        """调用记录 0 条时必须解释口径与下一步（2026-10-19 用户报障）。"""
        self.assertIn("agent 直接读磁盘", JS, "空状态未解释直读旁路不可见")
        self.assertIn("TERM_TOKEN", JS, "空状态未说明 401 凭据失效这一常见原因")
        self.assertIn("skillInvTabBadge", HTML, "调用记录 tab 缺少数量徽标")


class TestDiagnosticsFold(unittest.TestCase):
    """自检与实验室下沉诊断折叠区（裁定 5）：details/summary，默认折叠。"""

    def test_diag_details_exists_and_defaults_collapsed(self):
        m = re.search(r'<details[^>]*id="skillDiagBox"([^>]*)>', HTML)
        self.assertTrue(m, "skillDiagBox 缺失")
        self.assertNotIn("open", m.group(1).split('"'), "诊断区默认折叠（绿是常态，展开是取证需要）")

    def test_selfcheck_and_lab_moved_inside(self):
        diag = HTML[HTML.find('id="skillDiagBox"'):]
        diag = diag[:diag.find("</details>")]
        self.assertIn('id="skillSelfCheck"', diag, "自检卡必须收进诊断折叠区")
        self.assertIn('id="skillLab"', diag, "实验室必须收进诊断折叠区")


class TestBudgetPanel(unittest.TestCase):
    """③ 预算面板：分路过滤 + 失败表态必须真的在代码里，不只是注释里。

    2026-10-19 起因：`/api/skill/budget` 加了 `route=` 与 `routes_failed`，但前端
    契约没有闸门。删掉 `&route=` 只要后端还好着，页面会静默变成「总是全部路」——
    与「HTML 写死 20 路」同属第二真相，因此按同一口径钉。

    **作用域必须是函数体**：第一版写成对整份 JS 的 `assertIn("&route=")`，反证时
    把预算那处改成 `&routX=` 仍然全绿——因为技能详情按钮那行（`SKILL_DOC.route`）
    也含 `&route=`，断言被它借过了。整份文件级别的 assertIn 在本仓等同于没写。
    """

    @staticmethod
    def budget_fn() -> str:
        """截出 `loadSkillBudget` 的函数体（剥注释后）。"""
        code = strip_js_comments(JS)
        start = code.find("async function loadSkillBudget(")
        assert start != -1, "未找到 loadSkillBudget（面板被改名？）"
        rest = code[start:]
        # 到下一个顶层 `async function` / `function` 定义为止
        nxt = min([i for i in (rest.find("\nasync function ", 1),
                               rest.find("\nfunction ", 1)) if i != -1] or [len(rest)])
        return rest[:nxt]

    def test_html_has_route_select_and_1200_default(self):
        self.assertIn('id="skillBudgetRoute"', HTML, "预算面板缺分路下拉")
        m = re.search(r'id="skillTok"[^>]*value="(\d+)"', HTML)
        self.assertIsNotNone(m, "预算输入框缺失")
        self.assertEqual(m.group(1), "1200", "预算默认值须为 1200（后端 Query 默认同步）")

    def test_js_sends_route_param(self):
        fn = self.budget_fn()
        self.assertIn("'/api/skill/budget?max_tokens='", fn, "预算请求 URL 变了")
        self.assertIn("&route=", fn, "预算请求未带 route 参数")
        self.assertIn("encodeURIComponent", fn, "route 未编码（路名含空格/逗号会拼坏 URL）")

    def test_js_explains_truncation(self):
        """「为什么只剩 N 条」必须能由面板回答（v0.13.141）。

        生产实测 1200 tok：full 9 条吃 824、仅名 61 个吃 373、剩 3 ⇒ `break` 丢掉 308。
        原面板只显示「被裁 308」，用户只能猜。四条一起断言：
        ① 读 `full_cap`（分段上限）；② 读 `truncated_head`（断在哪个名字）；
        ③ 用户可见的成因文案；④ `why` 真的拼进 innerHTML（只算不拼 = 白算）。
        另钉老后端守卫：缺 `full_cap` 时不渲染（否则页面上是 `undefined tok`）。
        """
        fn = self.budget_fn()
        self.assertIn("full_cap", fn, "未用服务端给的分段上限")
        self.assertIn("truncated_head", fn, "未说出截断起点")
        self.assertIn("为什么被裁", fn, "缺用户可见的成因文案")
        self.assertRegex(fn, r"\+\s*why\s*\+", "why 算出来了却没拼进 innerHTML")
        self.assertRegex(fn, r"d\.truncated\s*&&\s*d\.full_cap != null",
                         "没有老后端守卫：缺字段会渲染出 undefined")

    def test_js_fills_select_from_backend(self):
        self.assertIn("routes_available", self.budget_fn(),
                      "下拉未由后端 routes_available 填充（写死即第二真相）")

    def test_js_renders_scan_failures(self):
        """扫描失败必须显示。`_scan_one_uncached` 超时返回 ok=False/items=[]，
        前端不表态时「整路静默消失」看起来和「这一路本来没有技能」一模一样。

        三条一起断言，缺一条都拦不住真实回归：
        ① `routes_failed` 被读了；② 算出的 `failed` 真的拼进 innerHTML
        （只算不拼 = 白算，反证时把 `+ failed +` 删掉仍全绿过，故补这条）；
        ③ 用户真能看到的字面量。
        """
        fn = self.budget_fn()
        self.assertIn("routes_failed", fn, "前端未读 routes_failed")
        self.assertRegex(fn, r"\+\s*failed\s*\+", "failed 算出来了却没拼进 innerHTML")
        self.assertIn("路扫描失败", fn, "缺用户可见的失败文案")

    def test_js_renders_nested_docs(self):
        """被排除的子文档必须可见（2026-10-19 新增 `/budget.nested_docs`）。

        为什么要有这条：`_is_nested_doc` 把 ima-skills/{notes,knowledge-base}
        从名录里剔掉后，`total` 会比磁盘上的 SKILL.md 少 2 —— 如果面板不说，
        下一个人看到「总数对不上」只能去翻代码。同 `routes_failed` 的口径：
        **排除可以，但必须说得出排除了谁、为什么**。

        三条同样缺一不可：读字段 / 真拼进 innerHTML / 用户可见文案。
        反证方式：把 `+ nested +` 删掉，本用例必须变红。
        """
        fn = self.budget_fn()
        self.assertIn("nested_docs", fn, "前端未读 nested_docs（排除变成静默）")
        self.assertRegex(fn, r"\+\s*nested\s*\+", "nested 算出来了却没拼进 innerHTML")
        self.assertIn("条子文档已排除", fn, "缺用户可见的排除文案")

    def test_js_fallback_matches_backend_default(self):
        m = re.search(r"skillTok'\)\.value,\s*10\)\s*\|\|\s*(\d+)", self.budget_fn())
        self.assertIsNotNone(m, "未找到预算兜底值")
        self.assertEqual(m.group(1), "1200", "兜底值与后端默认不一致")


class TestInvocationBadgeFreshness(unittest.TestCase):
    """tab 徽标必须是「当前」的近 7 天调用数，不是首屏快照。

    2026-10-20 用户报「调用记录按钮后面一直显示 0」：`go('skills')` 用
    `skillsLoaded` 一次性闸，首次进页之后再也不取汇总 ⇒ 首屏时库里 0 条，
    之后 agent 调多少次都仍是 0。三条出口缺一不可（重进页 / 切到记录视图 / 定时保活）。
    """

    def test_reenter_skills_refreshes_badge(self):
        body = strip_js_comments(COREJS)
        m = re.search(r"if\s*\(page === 'skills'\)\s*\{(.*?)\n  \}", body, re.S)
        self.assertTrue(m, "go() 的 skills 分支缺失")
        self.assertIn("loadSkillInvBadges(", m.group(1),
                      "重进技能页没补刷徽标 ⇒ 徽标永远停在首屏那一刻")

    def test_switching_to_invocations_refreshes_badge(self):
        m = re.search(r"function switchSkillView\(.*?\n\}", strip_js_comments(JS), re.S)
        self.assertTrue(m, "switchSkillView 缺失")
        self.assertIn("loadSkillInvBadges(", m.group(0))

    def test_badge_has_visibility_guarded_keepalive(self):
        body = strip_js_comments(JS)
        m = re.search(r"setInterval\(function \(\) \{.*?\}, 60000\);", body, re.S)
        self.assertTrue(m, "缺 60s 保活：页面开着时数字不会跟着涨")
        seg = m.group(0)
        self.assertIn("curPage === 'skills'", seg, "保活必须限定在技能页")
        self.assertIn("visibilityState", seg, "后台标签页不该继续打请求")
        self.assertIn("loadSkillInvBadges(false)", seg, "保活只许刷数字，不许重绘行")

    def test_badge_refresh_can_skip_row_rebuild(self):
        m = re.search(r"async function loadSkillInvBadges\(([^)]*)\)", strip_js_comments(JS))
        self.assertTrue(m, "loadSkillInvBadges 缺失")
        self.assertNotEqual(m.group(1).strip(), "", "必须能只刷数字（否则每次轮询重绘整页）")
        self.assertRegex(strip_js_comments(JS), r"if \(rebuildRows !== false\) renderSkillList\(\)")


class TestAutoInjectSurfaced(unittest.TestCase):
    """「自动下发」必须与「指名调用」在界面上分开显示。

    用户要求能分辨 agent 是执行任务时自动拿到的技能，还是人手动指定的。
    后端已按 subject/via 分桶（invocations.auto），界面混成一行就等于没分。
    """

    def test_summary_renders_auto_bucket(self):
        fn = re.search(r"function renderSkillInvocations\(.*?\n\}", strip_js_comments(JS), re.S)
        self.assertTrue(fn, "renderSkillInvocations 缺失")
        body = fn.group(0)
        self.assertIn("SKILL_INV_VIEW.auto", body, "界面没读 auto 桶")
        self.assertIn("自动下发", body, "缺用户可见的自动下发文案")

    def test_unknown_caller_is_reported_not_guessed(self):
        body = re.search(r"function renderSkillInvocations\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        self.assertIn("unknown_caller", body,
                      "未自报的调用方必须显式报数，不能静默算成 anonymous")

    def test_query_and_legacy_are_not_folded_into_auto(self):
        """同端点的人工检索与旧账要单列：折进「自动下发」会把曝光分母吹大（656 vs 6）。"""
        body = re.search(r"function renderSkillInvocations\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        self.assertIn("auto.query", body, "人工检索条数没显示")
        self.assertIn("auto.legacy", body, "旧账条数没显示")
        self.assertIn("不计入", body, "必须明说这些条目不参与自动下发计数")


class TestUsageView(unittest.TestCase):
    """权重榜视图（v0.13.140）：端点早有、页面零入口 ⇒ 这一组钉住「接上且不重算」。

    为什么必须钉：`/api/skill/usage` 10-20 就有 14 例测试，但没人能看见它
    ——「数据有了没人看」与「登记不等于入役」是同一个病。接上之后最容易犯的错
    是**在渲染里顺手重算一遍分数**：公式一旦在服务端与前端各存一份，漂移之后
    同一技能就有两个分数，而页面上那个看起来永远是对的。
    """

    def test_weights_is_fourth_view(self):
        self.assertIn('id="skillTabWeights"', HTML)
        self.assertIn('id="skillViewWeights"', HTML)
        self.assertIn('aria-controls="skillViewWeights"', HTML)
        self.assertIn('role="tabpanel" aria-labelledby="skillTabWeights"', HTML)
        body = re.search(r"function switchSkillView\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        self.assertIn("'weights'", body, "switchSkillView 不认识第四个视图")

    def test_panel_lives_inside_the_tab_card(self):
        """面板必须落在 tab 卡内（store 面板之后、预算卡之前）。

        **实测踩过**：首版把 `#skillViewWeights` 插在预算卡之后、`.sp` 之下，
        切换后 `display:block` 成立、127 行也渲染了，测试全绿——但真浏览器里
        排行榜在**预算卡下方 900px**（实测 top=913），首屏只看得到「安装与注入预算」。
        「元素存在 + display 不是 none」证明不了「用户看得到它」，这是第三次
        同型：判据量的是 DOM 属性，而报障量的是视觉位置。
        静态断言能钉住的就是**顺序**：budget 卡是 tab 卡的兄弟节点，
        面板只要排在它前面，就一定还在 tab 卡里。
        """
        i_store = HTML.index('id="skillViewStore"')
        i_weights = HTML.index('id="skillViewWeights"')
        i_budget = HTML.index('安装与注入预算')
        self.assertGreater(i_weights, i_store, "权重面板跑到 store 面板前面了")
        self.assertLess(i_weights, i_budget,
                        "权重面板在预算卡之后 ⇒ 切换后排行榜被挤到首屏下方")

    def test_filters_hit_server_not_client(self):
        """三个下拉必须触发重新请求：`caller` 是「先过滤再算分」。

        前端后过滤会把别人的曝光留在分母里 ⇒ 分数虚低，而用户以为那是这个 agent 的真实分。
        """
        body = re.search(r"async function loadSkillUsage\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        self.assertIn("&route=", body, "发现点过滤没打服务端")
        self.assertIn("&caller=", body, "调用方过滤没打服务端")
        self.assertIn("days=", body)
        render = re.search(r"function renderSkillUsage\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        # 只禁「对数据行做过滤」：`filter(Boolean)` 是拼文本时去空串，不是筛数据。
        # 判据要量对象本身（哪一批被筛），不能见 `.filter(` 就报红——那是量错了对象
        # （2026-10-20 首版断言就是这么误伤的）。
        self.assertNotIn("items.filter(", render, "渲染里又筛了一遍数据行——那会与后端口径打架")
        self.assertNotIn("all.filter(", render, "渲染里又筛了一遍数据行")

    def test_score_is_not_recomputed(self):
        """分数只认服务端：渲染里不许出现乘除（一旦有，就是第二套公式的开端）。"""
        render = re.search(r"function renderSkillUsage\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        self.assertNotIn("Math.", render, "渲染里出现了算术 ⇒ 疑似重算分数")
        self.assertNotIn("use * use", render)
        self.assertIn("it.score", render, "必须直接用服务端给的 score")

    def test_error_and_empty_are_distinct(self):
        """空榜 ≠ 读失败：两者同形会让「这个窗口没人用」和「服务挂了」看起来一样。"""
        render = re.search(r"function renderSkillUsage\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        # 口径必须是**条件分支**，不是「出现过 d.error 这个字」：
        # 首版写成 assertIn("d.error")，把 `if (d.error)` 改成 `if (false)` 后
        # 仍能通过（错误文案里还有 d.error），红臂当场证伪了这个断言。
        self.assertRegex(render, r"if \(d\.error\)", "没有失败分支")
        self.assertIn("读取权重失败", render)
        self.assertIn("还没有可计分的技能", render)

    def test_summary_reconciles_per_bucket(self):
        """汇总必须能逐桶对账（v0.13.141）：`totals` 是全量、`items` 是 limit 子集，
        无名事件又不进榜 ⇒ 「点名 26 / 榜内 24」必须同时可见，差额拆成两份。

        同时钉住**老后端兼容**：静态文件即时生效而 API 要重启，缺 `listed_by_bucket`
        时若照算，会把每一桶都说成「榜内 0」。判据是那个守卫**存在且是条件分支**，
        不是「hasListed 这个词出现过」（同族于 `assertIn("d.error")` 的教训）。
        """
        render = re.search(r"function renderSkillUsage\(.*?\n\}", strip_js_comments(JS), re.S).group(0)
        self.assertIn("listed_by_bucket", render, "汇总未读逐桶榜内数")
        self.assertIn("unnamed_by_bucket", render, "差额未按无名事件拆开")
        self.assertIn("（榜内 ", render, "缺用户可见的榜内口径")
        self.assertRegex(render, r"t\.listed_by_bucket\s*&&\s*typeof t\.listed_items",
                         "没有老后端守卫：缺字段时会把每桶都说成榜内 0")
        self.assertIn("dropped_by_limit", render, "分页截掉的行未表态")
        # 行数只能用行数来源：`t.named` 是**有名事件数**（实测 927）不是行数（138），
        # 拿它当「共 M 行」是同一个量纲错换了个位置（真浏览器探针抓出）。
        self.assertIn("rowsTotal", render, "总行数未由 listed_items + dropped_by_limit 推出")
        self.assertNotRegex(render, r"t\.named\s*\|\|", "把有名事件数当成行数用了")

    def test_filters_use_delegation(self):
        """下拉走 `data-usage-filter` 委托，不写 inline onchange（§4.2 同族口径）。"""
        for sid in ("usageDays", "usageRoute", "usageCaller"):
            m = re.search(r'<select[^>]*id="' + sid + r'"[^>]*>', HTML)
            self.assertTrue(m, sid + " 缺失")
            self.assertIn("data-usage-filter", m.group(0), sid + " 没挂委托标记")
            self.assertNotIn("onchange=", m.group(0), sid + " 用了 inline onchange")
        self.assertIn("bindUsageFilters", strip_js_comments(JS))


if __name__ == "__main__":
    unittest.main()
