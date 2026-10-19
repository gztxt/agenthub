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
    （代码里是 `'&n=5&rerank=' + (useJev ? 'true' : 'false')`，并无 `rerank=false` 字面量）。
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
    """④ 相关性实验室：打 /api/skill/relevant，并显示 BM25/jev 分开 + 耗时。"""

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

    def test_jev_toggle_ships_unchecked(self):
        """HTML 里的 jev 开关出厂不得带 checked —— 带了就等于默认开。"""
        m = re.search(r'<input[^>]*id="skillLabJev"[^>]*>', HTML)
        self.assertIsNotNone(m, "缺少 jev 开关元素，无法断言其默认值")
        self.assertNotIn("checked", m.group(0), "jev 开关出厂被勾上 = 默认重排 = 每次多等 1 秒")


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

    def test_js_fallback_matches_backend_default(self):
        m = re.search(r"skillTok'\)\.value,\s*10\)\s*\|\|\s*(\d+)", self.budget_fn())
        self.assertIsNotNone(m, "未找到预算兜底值")
        self.assertEqual(m.group(1), "1200", "兜底值与后端默认不一致")


if __name__ == "__main__":
    unittest.main()
