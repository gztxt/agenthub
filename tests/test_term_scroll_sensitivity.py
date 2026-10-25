"""L0 hermetic：终端滚轮灵敏度闸门（v0.13.63，PT-20260930-07）。

用户报障原句：「agent 终端页面，桌面浏览器里无法上翻 / 滚动到页顶」。
根因不在 CSS、也不在浮层遮挡，而在**一个从没显式设过的构造参数**：

  xterm 6.0 的 `consumeWheelEvent` 里有 `if (|deltaY| < 50) r *= 0.3` 再 `Math.floor` 取整。
  默认 `scrollSensitivity = 1` ⇒ 标准一格滚轮（deltaY=120、行高 24px）只走
  120/24*0.3 = 1.5 → 取整 1~2 行。2000 行 scrollback 从底部滚到顶要约 940 格，
  体感上就是「滚不动」。

  反直觉的一点：这不是 6.0 升级引入的回归。A/B 实测 5.5.0 与 6.0.0 在默认配置下
  行为**一致**（都是 ~2 行/格）；5.5 是按 `deltaY/行高` 走（自然值 5 行/格），
  6.0 的 0.3 折恰好把体验砍到 1/5，于是「本来就慢」被放大成了「滚不动」。

为什么这批闸门必须是静态的：动态断言要起浏览器 + 真派发滚轮，而**回归最隐蔽的形态
是这个参数被人「顺手调回默认」或被 xterm 未来版本改默认值**——那时页面仍然能滚、
只是又变回 2 行/格，截图和"点几下看看"都发现不了。静态闸门锁的是"显式写了、没有漂"。

  ① 构造参数必须显式存在且 ≥ 5（不是"有没有效果"，是"有没有被钉住"）
  ② 分片与构建产物必须一致（改分片忘重建 hub.js ⇒ 改了等于没改，本仓踩过，
     见 scripts/build_hubjs.sh 的注释；这里再做一次独立对账）
  ③ 版本注释必须记着根因与数字（半年后有人再调它，得知道为什么是 5）
"""
import re
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
HUB = REPO / "static" / "hub"
TERM_JS = HUB / "03-agents-cards.js"
MAIN_PY = (REPO / "src" / "main.py").read_text(encoding="utf-8")

#: 6.0 核心里那段 0.3 折的原文锚点。改法（换 vendor / 换方案）后要同步更新本常量与注释。
VENDOR_030_FOLD = re.compile(
    r"Math\.abs\(\w+\.deltaY\)<50&&\(\w+[\*]?=\.3\)")  # 压缩后的实名，见 vendor 原文

#: 低于此值即判红：与「不按 6.0 打折时的自然值」(5 行/格) 对齐的下限。
MIN_SENSITIVITY = 5


class TestScrollSensitivityPinned(unittest.TestCase):
    def test_03_agents_cards_declares_sensitivity(self):
        src = TERM_JS.read_text(encoding="utf-8")
        m = re.search(r"scrollSensitivity:\s*(\d+)", src)
        self.assertIsNotNone(
            m, "Terminal 构造里没有显式 scrollSensitivity —— 回落到 xterm 默认 1 即 2 行/格")
        self.assertGreaterEqual(
            int(m.group(1)), MIN_SENSITIVITY,
            "scrollSensitivity=%s 低于下限 %d（6.0 的 0.3 折后不到 5 行/格）"
            % (m.group(1), MIN_SENSITIVITY))

    def test_fast_scroll_sensitivity_untouched(self):
        """修滚速不得顺手把 Alt/Ctrl/Shift 的快速滚动关掉。"""
        src = TERM_JS.read_text(encoding="utf-8")
        for opt in ("fastScrollModifier", "fastScrollSensitivity"):
            pass  # 允许不写（用默认 alt/5），但不得显式调小
        bad = re.findall(r"fastScrollSensitivity:\s*(\d+)", src)
        for v in bad:
            self.assertGreaterEqual(int(v), MIN_SENSITIVITY,
                                    "fastScrollSensitivity=%s 会让快速滚动比普通滚动还慢" % v)

    def test_vendor_still_has_the_fold(self):
        """钉住根因本身：哪天 vendor 里没有这段 0.3 折了，说明上游改了默认行为，
        本闸门的下限就该重新量（否则会拿着过期结论调参数）。"""
        v = (REPO / "static" / "vendor" / "xterm.js").read_text(
            encoding="utf-8", errors="replace")
        self.assertTrue(
            VENDOR_030_FOLD.search(v),
            "vendor/xterm.js 里已找不到 `|deltaY| < 50 … *= 0.3` —— "
            "要么换了 xterm 版本，要么上游改了滚轮算法；请重新量每格行数后更新本测试")

    def test_built_hubjs_matches_shard(self):
        """改分片必须重建产物。`?v=` 提手派生于产物 md5，所以**看起来**会自愈，
        实际是模板指向的 /static/hub.js 里根本没有这段代码 ⇒ 改了等于没改。"""
        built = (REPO / "static" / "hub.js").read_text(encoding="utf-8")
        shard = TERM_JS.read_text(encoding="utf-8")
        m = re.search(r"scrollSensitivity:\s*(\d+)", shard)
        self.assertIsNotNone(m, "分片里没找到 scrollSensitivity")
        self.assertIn(
            "scrollSensitivity: %s" % m.group(1), built,
            "static/hub.js 与分片不同步 —— 跑 bash scripts/build_hubjs.sh")

    def test_template_token_matches_hubjs_md5(self):
        """模板里的 ?v= 提手必须等于 hub.js 内容的 md5 前 8 位。"""
        import hashlib
        tok = hashlib.md5((REPO / "static" / "hub.js").read_bytes(),
                          usedforsecurity=False).hexdigest()[:8]
        tpl = (REPO / "templates" / "index.html").read_text(encoding="utf-8")
        m = re.search(r'/static/hub\.js\?v=([0-9a-f]{8})', tpl)
        self.assertIsNotNone(m, "模板里找不到 /static/hub.js?v= 提手")
        self.assertEqual(m.group(1), tok,
                         "?v=%s 与 hub.js 实际内容 %s 不符 —— 跑 scripts/build_hubjs.sh"
                         % (m.group(1), tok))

    #: 每个已 bump 的版本号 -> 该版根因注释里必须在场的关键字。
    #: 新增一行就是新增一道“半年后没人知道当时为何这么改”的闸门；删除一行等于销账。
    VERSION_ROOT_CAUSES = {
        "0.13.65": ("fed.sources=0", "skipped_budget", "全指标绿而功能层已死", "scrollSensitivity"),
        "0.13.66": ("PT-20261002-11", "find 不带 -L", "同源副本", "四态"),
        "0.13.67": ("子串", "name命中恒为空", "不改写排序", "PT-20261002-12"),
        "0.13.68": ("not-implemented", "counted", "suggested_action", "PT-20261002-13"),
        "0.13.69": ("第二个真相", "7 路发现点", "蒙对的闸门", "HUB_NARROW_MQ"),
        "0.13.70": ("退出码会让 Claude 拒绝输入", "禁改面拒绝", "报冲突不覆盖", "窄授权不得读成宽授权"),
        # 0.13.71 两条根因。关键字取自 main.py 版本注释里**实际写下的字句**，
        # 不是事后编的摘要——该闸门的作用是「注释掉了根因就红」，所以关键字必须可寻址。
        "0.13.71": ("归档根与发现点是两张表", "架空 `EXCLUDED_DIRS`", "crawl1ai", "≈近似"),
        "0.13.72": ("模糊匹配不再是「技能中心专属」", "恒真的 L0 用例", "想写也写不成", "那个 6 是我数错的"),
        "0.13.73": ("同一台机器两套口径", "MCP 门面 `hubmcp.py`", "两侧一起错", "不改排序"),
        "0.13.74": ("窄屏第一眼是一块白板", "首帧", "为什么必须在 <head>", "夹具必须判别"),
        "0.13.75": ("每个字一行", "修的是那一个选择器", "判不了像素", "短页面底部有留白"),
        "0.13.76": ("抽屉永远打不开", "点哪都点不到", "从来没点过开", "只验一个时刻"),
        "0.13.77": ("按不可信输入处理", "存储型 XSS", "顺序不可颠倒", "**规则里的**类名不存在"),
        "0.13.78": ("形状完全一样", "既没转义、也没过", "HTML 正文", "JS 字面量"),
        # 0.13.79：全指标绿而闸门说谎的那一条 —— FORCE_COLOR 让 node 把数字染成
        # ANSI，22 条用例假红，而「全绿/全红」本身就是本轮一切判据的前提。
        # 关键字取自 src/main.py 的 VERSION 注释里实际写下的字句。
        "0.13.79": ("FORCE_COLOR", "假红", "逐版根因已于 2026-10-05 归档",
                    "逐字搬运", "只缓存成功路径"),
        # 0.13.80：CloudCLI 独立子菜单拆出，claude 卡不再挂 :3010 宿主。
        # 根因：cloudcli 宿主嵌在 claude 卡里时，点 Claude Code 菜单行会进
        # embed（cloudcli 原生界面），终端页对 cloudcli 用户不可达；同时
        # cloudcli 原生界面没有独立子菜单入口，项目直达只能走 claude 详情抽屉。
        # 拆开后：claude=纯终端+对话，cloudcli=独立卡+embed原生界面+项目直达面板。
        "0.13.80": ("cloudcli", "独立子菜单", "embedBody", "renderCloudcliProjects"),
        # 0.13.81：终端鼠标跟踪看门狗。TUI 开 1002/1003 后滚轮/拖选被吞，
        # 异常退出不发 ?1003l 时 mouseTrackingMode 卡 any，只有重连才复位。
        # 修复：capture 阶段 wheel/mousedown，同步切 activeProtocol=NONE。
        "0.13.81": ("鼠标跟踪", "1003", "看门狗", "activeProtocol", "TERM_MOUSE_OFF"),
        # 0.13.82：备用屏（?1049）跨会话污染。codex 的 TUI 开机发 ?1049h 进备用屏，
        # 而 xterm.js 的备用屏按设计没有 scrollback；整页共用一个 xterm 实例、切会话的
        # term.clear() 又不退出备用屏（只有 term.reset() 会）⇒ 一次 codex 把整页拖进去，
        # 之后所有 agent（含 cursor Agent）都滚不动。cloudcli 正常因它是普通 shell→TTY。
        # 修复：前端 TERM_STATE_RESET 在换会话那一帧写 + codex 启动带 --no-alt-screen
        # + 工具栏「缓冲区状态字 / 退出备用屏」。
        # ⚠ 与 0.13.81 是**两条不同的腿**：那版治鼠标跟踪态，从没量过 buffer.active.type。
        "0.13.82": ("备用屏", "?1049h", "TERM_STATE_RESET", "--no-alt-screen", "term.reset()"),
        # 0.13.83：用户裁定「只用主屏」+「输入控件关闭鼠标」+「cursor 没有会话记录」。三条：
        #   ① 备用屏改从**解析层**禁掉（DECSET 1049/1047/47 注册成吞掉，返回 true 即拦下，
        #      activateAltBuffer 不执行），v0.13.82 的 TERM_ALT_OFF/TERM_STATE_RESET、
        #      状态字 #termBufChip 与按钮 #termAltOut 全删（备用屏不会发生 ⇒ 死码）。
        #   ② 终端页唯一能吃鼠标的输入控件（查找框 #termFindInput）加 pointer-events:none，
        #      鼠标事件透传给终端 ⇒ 滚轮只滚 scrollback，不再两边抢焦点。鼠标上报转发**不动**。
        #   ③ cursor 补 SESSION_STORES + TERM_HIST_AGENTS：此前 CLI_ALIASES 有、这张表没有
        #      ⇒ 前端根本不发历史请求。适配器以 chats/meta.json 为主表、转录只供标题。
        # ⚠ 与 0.13.82 的关系：那版是「补写退出序列 + 手动逃生」，这版是**禁止进入**，
        #   方向相反 —— 所以 0.13.82 的 TERM_ALT_OFF 在代码里已不存在，只留在注释与 CHANGELOG。
        "0.13.83": ("termAltScreenBlock", "pointer-events", "cursor_json",
                    "#termFindInput", "hasConversation"),
        # 0.13.84：同一套嵌入式终端「别的 agent 好了、claude/opencode 还不行」。机制分层：
        #   全屏 TUI 的历史不在 scrollback（主屏一屏高、viewportY 恒 0），向上翻只能把
        #   滚轮 SGR 上报喂回 pty 由 app 自滚。want（app 意图）与 xterm 临时态分家、
        #   wheel 分层、mouseup 回装、非 shell 回放信任 + per-sid 缓存、4410 清零防死轮。
        "0.13.84": ("termMouseWant", "termMouseArm", "4410", "死轮", "应用内滚动"),
        # v0.13.85 不是终端批次，但版本钉只跟当前版本号走 ⇒ 这一版的"根因关键字"
        # 取它自己的四件事（缺任一件，半年后就没人知道模型设置为何长这样）。
        # 关键词取自 main.py 的 VERSION 注释块（2400 字符窗口内）与 CHANGELOG v0.13.85 段。
        "0.13.85": ("write_mode", "profile 表头键", "撤销", "漂移"),
        # v0.13.86 同属模型设置批次：把 v0.13.85 遗留的"能选但写不进"补掉
        # （用户准则「插入要精确」落在选项值这一层）。
        "0.13.86": ("VALUE_PREFIX", "NATIVE_MODELS", "值前缀", "原生清单"),
        # v0.13.87 回到终端批次：用户报「嵌入式终端总是提示登录链接」。
        # 关键词取「两路判据 + 线索词方向」—— 这正是本版最容易在半年后被改坏的两点
        # （把判据合回一条、或把方向表当多余）。取自 main.py 的 VERSION 注释块。
        "0.13.87": ("auth_url", "page_url", "线索词", "方向正确"),
        # v0.13.88 是**同一条报障的二次**（用户 2026-10-07 复用后仍弹登录提示）：
        # v0.13.87 修的是线索词与方向，判定范围仍是整段 scrollback ⇒ 一次真提示把其后
        # 10 行输出整段毒化。关键词取「位置轴 + 中毒带 + 换轴到当前块」，这正是本版
        # 最容易在半年后被"顺手改回 scrollback"的一点。
        "0.13.88": ("last_chunk", "位置轴", "中毒带", "当前这一块"),
        # v0.13.89 是**第三次**报障（同日）：「总是出现 检测到链接 这样的提示」，
        # 用户口径「全部静默，连登录提示也不要」⇒ 整条带外通道加总开关、默认关闭。
        # 关键词取「开关名 + 静默 + 用户真实诉求」——这正是最容易在半年后被"顺手打开"
        # 或误判成"功能坏了"的一点。
        "0.13.89": ("LINK_BYPASS_ENABLED", "全部静默", "总开关", "不要在终端里弹"),
        # v0.13.90 是**同日两项独立报障**：① 顶栏会话数一时显示一时不显示；
        # ② 终端复制粘贴与快捷键全废。关键词各取两条最容易在半年后被改坏的点：
        #   · 会话数侧：数据源必须**免 token**（`/api/term/activity`），口径是
        #     「在跑 N · 会话 M」两个数都给 —— 容易被人"顺手"合回要口令的清单端点；
        #   · 剪贴板侧：`termCopyFallback` **焦点归还** 与 `preventDefault` 的**删掉**
        #     （粘贴放给浏览器原生路径）—— 这两处都极易被后人"补回 preventDefault"
        #     或"省掉那次 focus()"，而失效方式完全安静（探针会红，肉眼看不出来）。
        "0.13.90": ("api/term/activity", "在跑 N · 会话 M", "焦点归还",
                    "preventDefault", "原生粘贴"),
        # v0.13.91 回到备用屏这条线：v0.13.83「吞掉 ?1049h」的副作用 —— 只吞不清，
        # TUI 在旧画面上作画 ⇒ 用户报「jcode 启动嵌入式终端带入乱码」。关键词取
        # 三处最易被后人改坏/删掉的点：拦截函数名、同步清屏这一声、以及"必须同步
        # （不能改成异步 term.write）"这条判据本身。
        "0.13.91": ("termAltScreenBlock", "同步清屏", "备用屏", "乱码"),
        # v0.13.92 是另一条终端报障线（用户 2026-10-08）：「嵌入式终端 页面文字无法选择
        # 复制，按右键显示是图片」。关键词取三处最容易在半年后被改坏/删掉的点：
        # ① 回装清选区的机制来源（`SelectionService.disable` —— xterm 一进鼠标跟踪就
        #    clearSelection，忘了它就会把「有选区不回装」这行"顺手删掉"）；
        # ② 右键菜单的唯一入口/出口（浮层纪律，AGENTS 4.2）；
        # ③ 用户原话「图片另存为」——它记着"为什么必须自己出菜单"。
        "0.13.92": ("SelectionService.disable", "回装", "termCtxOpen", "图片另存为"),
        # v0.13.93 codebuddy 接入历史会话：三处与 claude 不同的形状 + 可续判据。
        "0.13.93": ("role=='user'", "compressPath", "桶名", "SessionNotFound"),
        # v0.13.94 unit 改名：关键词取「静默失效」那两处 —— journalctl 对不存在的单元
        # 返空且退出码 0（所以漏改不报错，只是日志页空白）；EXCLUDE 是前缀匹配。
        "0.13.94": ("DEFAULT_UNIT", "EXCLUDE_PATTERNS", "静默", "前缀"),
        # v0.13.95 改名收尾：清 3 处运行时旧名残留 + 加「产物/提手」闸门。
        # 关键词取「为什么这类漏改抓不到」那两点。
        "0.13.95": ("降级文案", "?v=", "md5", "无消费方"),
        # v0.13.96 cursor 历史标题显示英文：关键词取「取错了源」那三点——
        #   ① meta.title 是**服务端生成的英文**（不是编码/locale 问题，搜「编码」会跑偏）；
        #   ② 唯一判据函数 `_cursor_title`（列表侧与 title_for 侧共用一份优先级）；
        #   ③ D2「问题原文优先」——原文优先、LLM/服务端标题当兜底。
        "0.13.96": ("服务端", "_cursor_title", "原文优先", "兜底"),
        # v0.13.97 全站右侧内容区留白缩小 60%：关键词取「先取证是哪一层 + 范围是全站
        # + 不许动黑底内衬 + 两档各自 ×0.4」这四点——
        #   ① 留白唯一来源是 main 的 padding（.chat-grid/.chat-main 都是 0，gap 不产生列间距）；
        #   ② #termEl/.term-body 的 padding 被 FitAddon 铁律钉死为 0（给黑底加内衬会裁掉底部一行）；
        #   ③ 范围是**全站**——第一版用 :has() 只收窄终端页、被用户否掉（点别的菜单边距又变了）；
        #   ④ --main-pad-* 唯一真相源，且两档各自 ×0.4（窄屏基准 8/8 ⇒ 3.2/3.2，不是照抄宽屏）。
        "0.13.97": ("FitAddon", "全站", "--main-pad-", "第二判据"),
        # v0.13.98 顶栏 GitHub 入口「只保留图标和版本号」：关键词取「减的是可见文字 +
        # 版本号走 Jinja 变量而非字面量」这两点——
        #   ① href/target/rel/title/aria-label 五项一个不少（外链安全 + 地址可抄走两条旧裁定）；
        #   ② 顶栏 .hstats 是 nowrap+overflow:hidden，右缘先被切的是最右那一项 ⇒ 常驻宽度要省；
        #   ③ `{{ version }}` 单一真相源 —— 写死字面量会得到「/health 报新版、顶栏写旧版」的
        #      无告警漂移（本仓 CHANGELOG 顶部漂过 13 版是同型）。
        "0.13.98": ("hstats", "Jinja", "变量", "右缘"),
        # v0.13.99 终端页编辑键（焦点不在终端时也能用）：关键词取「证伪的那条 +
        # 为什么不能靠回退 v0.13.83 来修」这两点——
        #   ① pointerEvents:none 不影响键盘（滚轮与键盘是两条**独立**通路）⇒ 修键盘
        #      必须走键映射表，**不能**回退那行（那会让滚轮重新被输入框抢焦点）；
        #   ② 放开所有非单字符键 = 把语义交还浏览器（方向键变**滚动页面**），
        #      而终端输入位上这些键的语义只有一个来源：pty ⇒ 必须显式映射。
        "0.13.99": ("pointerEvents", "独立", "TERM_EDIT_KEYS", "滚动页面"),
        # v0.13.100 终端输入延迟根治（录制预算的全表 SUM 收进进程内缓存）：
        # 关键词取「为什么是全表聚合 + 为什么必须作废而不是减法修正」这两点——
        #   ① SUM(bytes) 走 SCAN（全表聚合），唯一的 idx_termrec_sid(session_id,seq)
        #      盖不住 ⇒ 每帧一次全表扫，而 feed() 是**同步**在事件循环里的；
        #   ② 增量法遇 DELETE 会永久偏高且不可逆 ⇒ 一律作废重查，不做减法修正。
        "0.13.100": ("SUM(bytes)", "全表", "作废", "事件循环"),
        # v0.13.101：关键字全部取自 main.py 版本注释里**实际写下的字句**。
        # 「失效条件被高频路径命中 = 等于没优化」是本版最该被记住的一句。
        "0.13.101": ("trg_termrec_ins", "ensure_counted", "idx_termrec_ts",
                     "等于没优化", "term_rec_meta"),
    }
    CURRENT_VERSION = "0.13.154"

    #: v0.13.154：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「拉起成功后嵌页仍空白」「applyChatMode 只在 URL 变时才赋值」
    #: 「启动前失败导航留在框里」「显式 f.src = f.src 重载」。
    VERSION_ROOT_CAUSES["0.13.154"] = (
        "嵌页仍是", "空白", "applyChatMode", "dataset.src", "f.src = f.src",
        "defunct", "僵尸",
    )

    #: v0.13.153：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「点击原生界面没有拉起」「ui-status 双栈判活」「termHeaders 二次弹窗」
    #: 「两个 .embed-dead」「PATH 补用户级 bin」。
    VERSION_ROOT_CAUSES["0.13.153"] = (
        "点击原生界面", "没有拉起", "ui-status", "双栈", "termHeaders",
        "两个 .embed-dead", "PATH",
    )

    #: v0.13.152：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「设置子菜单上下布局」「模板多一个 </div>」「main 提前关闭」
    #: 「7 页错位」「新增 test_dom_structure.py」。
    VERSION_ROOT_CAUSES["0.13.152"] = (
        "上下布局", "</div>", "提前关闭", "test_dom_structure.py",
    )

    #: v0.13.151：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「按钮复还」「probe_port 自相矛盾」「空白嵌页」「模型设置自动发现」
    #: 「ccr/ 前缀形状」「红向自证三破坏点」。
    VERSION_ROOT_CAUSES["0.13.151"] = (
        "按需拉起下原生界面按钮复还", "probe_port", "空白嵌页",
        "模型设置自动发现", "ccr/", "三破坏点",
    )

    #: v0.13.150：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「补登 QoderWake 长驻缺口」「双进程长驻实测」「单元名大小写敏感」
    #: 「静态卡 id 压动态卡（covered）」。
    VERSION_ROOT_CAUSES["0.13.150"] = (
        "补登 QoderWake 长驻缺口", "qoderwake __daemon", "QoderWake.service",
        "covered", "大小写敏感",
    )

    #: v0.13.149：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「全 agent 按需拉起」「probe_port 死按钮过滤」「注入代理误判」
    #   「URL host 补探（dsh LAN 绑定）」「端口相等+非回环双闸」「判据恒假」。
    VERSION_ROOT_CAUSES["0.13.149"] = (
        "全 agent 按需拉起", "probe_port", "注入代理",
        "URL host 补探", "端口相等", "判据恒假",
    )

    #: v0.13.148：关键词取自版本注释里**实际写下的字句**（已归档 CHANGELOG，
    #   本条目保留供 test_earlier_version_root_causes_survive 稽查「不许蒸发」）。
    #: 核心是「dsh 工作区联动」「启动器 cd 覆盖 cwd」「workspace/create 注册」
    #   「req_cwd 才联动」「to_thread 不占循环」。
    VERSION_ROOT_CAUSES["0.13.148"] = (
        "dsh 工作区联动", 'cd "$DSH_DIR"', "workspace/create",
        "req_cwd", "asyncio.to_thread",
    )

    #: v0.13.147：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「dsh 拒绝通配地址」「self_addrs」「整段 host 相等」。
    VERSION_ROOT_CAUSES["0.13.147"] = (
        "dsh 拒绝通配地址", "self_addrs", "整段 host 相等",
    )

    #: v0.13.146：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「接 deepseek-flash」「质量已饱和」「截断即失败」「LOCAL_JUDGE=0 一键关」。
    VERSION_ROOT_CAUSES["0.13.146"] = (
        "接 deepseek-flash", "质量已饱和", "截断即失败", "LOCAL_JUDGE=0",
    )

    #: v0.13.145：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「判定进主链」「只在判成 off_topic 时降级」「同桶首位」「CHAIN_JUDGE=0 回滚」。
    VERSION_ROOT_CAUSES["0.13.145"] = (
        "判定进主链", "off_topic", "同桶首位", "CHAIN_JUDGE=0",
    )

    #: v0.13.144：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「只缓存成功」「连续 3 次失败」「分块归并」「硬证据优先」。
    VERSION_ROOT_CAUSES["0.13.144"] = (
        "只缓存成功", "连续 3 次失败", "分块归并", "硬证据优先",
    )

    #: v0.13.143：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「20s 余量是按空负载算的」「不能外推」「宁可慢也不能一开就降级」。
    VERSION_ROOT_CAUSES["0.13.143"] = (
        "判定附注超时 20→45s", "不能外推", "宁可慢也不能一开就降级", "2.3 倍余量",
    )

    #: v0.13.142：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「外部 jev 恒 451 已下线」「失败必抛不吞」「精排默认关守注入预算」。
    VERSION_ROOT_CAUSES["0.13.142"] = (
        "外部 jev 代码层彻底清除", "恒 451", "失败必抛", "默认关",
    )

    #: v0.13.141：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「totals 与 items 的差额要逐桶归因」「budget 被裁要说得清从哪断」。
    VERSION_ROOT_CAUSES["0.13.141"] = (
        "永远不相等", "418 条无名", "为什么只剩 3 条", "连续",
    )

    #: v0.13.140：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「权重榜接上页面」「不在前端重算」「过滤打服务端」。
    VERSION_ROOT_CAUSES["0.13.140"] = (
        "权重榜", "不在前端重算", "过滤一律打服务端", "失败态与空态分开",
    )

    #: v0.13.139：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「中文技能不被语言扣分」「整句搜不到靠派生短词」「短词命中不许自动装」。
    VERSION_ROOT_CAUSES["0.13.139"] = (
        "qcov_name", "_cn_segments", "matched_main", "派生短词",
    )

    #: v0.13.138：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「自动取用」「local_only 不等于 allow_install=false」「auto_invoke 单列」。
    VERSION_ROOT_CAUSES["0.13.138"] = (
        "自动取用", "local_only", "auto_invoke", "auto_use",
    )

    #: v0.13.137：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「计划态不冒充未命中」「计划态不算使用」「cli_present 同源」「默认目标＝兜底目标」。
    VERSION_ROOT_CAUSES["0.13.137"] = (
        "计划态", "skip_when", "cli_present", "自动兜底目标",
    )

    #: v0.13.136：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「富字段取回」「六维加权」「两条硬否决」「中选者必查报告」。
    VERSION_ROOT_CAUSES["0.13.136"] = (
        "富字段", "六维加权", "硬否决", "中选者必查过报告",
    )

    #: v0.13.135：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「找不到才补货」「归一化 BM25 判本地」「store_auto 记账当额度」「第三方要标明来源」。
    VERSION_ROOT_CAUSES["0.13.135"] = (
        "自动补货", "归一化 BM25", "skill.store_auto", "trust=community",
    )

    #: v0.13.133：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「曝光只作分母」与「逐名 route 缺失会让 auto 配不到使用上」。
    #: v0.13.134：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「flex 容器里的 <br> 不换行」与「两列网格才对齐」。
    VERSION_ROOT_CAUSES["0.13.134"] = (
        "flex 里的 <br>", "两列网格", "inline-flex", "成功",
    )

    VERSION_ROOT_CAUSES["0.13.133"] = (
        "PT-20261020-01", "只作分母", "use^2/(use+auto)", "逐条目字段", "legacy",
    )

    #: v0.13.132：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「一次性加载闸把实时数冻结成快照」与「曝光不是使用，必须按 via=hook 分桶」。
    VERSION_ROOT_CAUSES["0.13.132"] = (
        "PT-20261020-01", "一次性加载闸", "via=hook", "explicit_invoke", "unknown_caller",
    )

    #: v0.13.130：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「调用记录恒 0 是写入被 401 拒，不是查询坏」（三层取证：HTTP → 库行 → 窗口），
    #: 以及「闸门自己会红」——夹具仓被当项目，根因是排除优先级写反。
    #: v0.13.131：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「双引号属性 + jsStr 双引号字面量 = 属性被截断」——只查源码调没调函数
    #: 的静态闸门看不见它，故必须配渲染态探针。
    VERSION_ROOT_CAUSES["0.13.131"] = (
        "PT-20261019-02", "data-* 委托", "双引号属性", "静默 SyntaxError",
        "反向闸门", "渲染态探针",
    )

    VERSION_ROOT_CAUSES["0.13.130"] = (
        "PT-20261019-01", "PT-20261019-04", "sk-titlebar", "ARIA tabs",
        "skill.invoke", "401", "writegate", "212 > 50",
        "hub-l0test-fixtures", "硬排除优先", "点名根豁免压过硬排除",
    )

    #: v0.13.129：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「子文档不是技能」——补 frontmatter 会让模型绕过父级的 MANDATORY 校验；
    #: 以及「reset 拦不住就留恢复点」（refs/wip/auto）。
    VERSION_ROOT_CAUSES["0.13.129"] = (
        "真发现点", "63", "git clone", "nested", "ima-skills", "MANDATORY",
        "子文档", "375 → 373", "reset --hard", "refs/wip/auto",
    )

    #: v0.13.128（PT-20261019-03）：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「截断必须自报」——只有 count 时，200 既可能是全部也可能是少收，
    #: 前端与探针都把少收读成了总量（生产实测 371 只回 200、12 路整路不可见）。
    VERSION_ROOT_CAUSES["0.13.128"] = (
        "PT-20261019-03", "默认 limit=200", "12 路整路不可见",
        "total", "truncated", "截断必须自报", "上限 500→1000", "本次只显示",
    )

    #: v0.13.127：关键词取自版本注释里**实际写下的字句**（PT-20261019-01 P0）。
    #: 核心是「hub 只代理激活+记账、不代跑代码」的语义边界，与「HUB_URL 不许写死端口」。
    VERSION_ROOT_CAUSES["0.13.127"] = (
        "PT-20261019-01", "POST /api/skill/invoke", "激活+记账", "不代跑代码",
        "GET /api/skill/catalog", "GET /api/skill/invocations", "零新表",
        "hub_skill_invoke", "hub_skill_catalog", "codebuddy", "local", "opencode",
        "HUB_URL", "3102", "config.port", "_pick_extra",
    )

    #: v0.13.126：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「Mac/局域网 http ⇒ 只剩 execCommand，而 WebKit 的用户手势不跨微任务」，
    #: 以及「有选区 Cmd+C 不该自己 preventDefault，把 xterm 的原生 copy 链废掉」。
    VERSION_ROOT_CAUSES["0.13.126"] = (
        "Mac", "isSecureContext", "navigator.clipboard", "execCommand",
        "用户手势", "微任务", "原生 copy", "preventDefault", "SIGINT", "不重启生产",
    )

    #: v0.13.125：关键词取自版本注释里**实际写下的字句**。
    #: 核心是「同一份 token 在两条鉴权路径上待遇不同」——OWS 裁掉、`%20` 不裁 ⇒
    #: REST 全绿而 WS 403、终端永久空白；服务端精确比对**不许放宽**，脏值只在读侧自愈。
    VERSION_ROOT_CAUSES["0.13.125"] = (
        "尾随空白", "OWS", "%20", "精确比对", "存量脏值", "不重启生产",
    )

    #: v0.13.124（P2 C17）：关键词取自版本注释里**实际写下的字句**。
    #: 工具描述质量三态 + 按任务裁剪。
    VERSION_ROOT_CAUSES["0.13.124"] = (
        "tooldesc", "description_quality", "completeness", "select", "insufficient",
    )

    #: v0.13.123（P2 A10）：关键词取自版本注释里**实际写下的字句**。
    #: Action Plane：登记≠放行、unknown≠failed、结论不可覆盖、ACL 降为 PDP。
    VERSION_ROOT_CAUSES["0.13.123"] = (
        "Action Plane", "pending", "unknown", "policy decision point", "零 import mcpgw",
    )

    #: v0.13.122（P2 C7）：关键词取自版本注释里**实际写下的字句**。
    VERSION_ROOT_CAUSES["0.13.122"] = (
        "admission", "六阶段", "incomplete", "pending", "不是安全结论",
    )

    #: v0.13.121（P2 A1）：关键词取自版本注释里**实际写下的字句**（不是想当然的提法）。
    VERSION_ROOT_CAUSES["0.13.121"] = (
        "release_id", "semantic", "tool_pattern", "degraded", "换目录不是换版本",
    )

    #: v0.13.120（P2 A11）：关键词取自版本注释里**实际写下的字句**。
    #: Trace 因果链：有列≠能追、旧行不编因果、两种「空」分开、主路径自抓。
    VERSION_ROOT_CAUSES["0.13.120"] = (
        "trace_id", "asset_audit", "orphans", "audit_trace_available", "能力没上线",
    )

    #: v0.13.119（P2 C2）：关键词取自版本注释里**实际写下的字句**。
    #: ACL 检测与拦截分开：effect 灰度、shadow 只记 would_deny 不拦。
    VERSION_ROOT_CAUSES["0.13.119"] = (
        "effect", "shadow", "would_deny", "mcp_acl_shadow", "enforce",
    )

    #: v0.13.118（170 §7 后两项）：关键词取自版本注释里**实际写下的字句**。
    #: verifying 可观测且可度量 + waiting 有界自愈。
    VERSION_ROOT_CAUSES["0.13.118"] = (
        "verify_started_at", "verify_ms", "wait_until", "wake_count",
        "resume_expired_waits", "TASK_MAX_WAKES",
    )

    #: v0.13.117（P1-6 接线两处）：关键词取自版本注释里**实际写下的字句**。
    #: 异步孪生 acall + 结构化网关异常 + MCP 幂等键（且刻意不自动重试）。
    VERSION_ROOT_CAUSES["0.13.117"] = (
        "acall", "LLMGatewayError", "幂等键", "attempts=1", "_decide",
    )

    #: v0.13.116（170 §7 前两项）：关键词取自版本注释里**实际写下的字句**。
    #: ① 执行者不得自证 fail（复核落 verify_reason + mismatch 事件）；② run 级预算派发前止损。
    VERSION_ROOT_CAUSES["0.13.116"] = (
        "执行者不得自证 fail", "verify_reason", "mismatch", "task_run_budgets",
        "run_budget_exceeded", "派发前",
    )

    #: v0.13.115（PT-20260927-12/13/14/15，外部评估蓝图链四项）：关键词取自版本注释
    #: 里**实际写下的字句**。挑的是「这四项最容易被后人顺手改坏/删掉」的点：
    #: ① 韧性层：429 豁免、Retry-After 封顶、半开单探、异常路径必须释放占位、
    #:    以及 `vitals.http_ok` 刻意零重试（在探活路径上，重试会破坏「最多 2 次」红线）；
    #: ② 依赖锁：裸包名 vs 锁定；
    #: ③ token 取用：它是**可用性**改善而非放宽鉴权，别被当成安全缺口「顺手删掉」；
    #: ④ 知识索引：指针闸门能验指针、**验不了**判据时效，所以那句限定语必须在；
    #: ⑤ 配置包：不导凭据/地址、幂等 upsert 不清本机条目、未来版本显式拒绝、
    #:    导出按写方法判鉴权、一律 JSON 不引 YAML。
    VERSION_ROOT_CAUSES["0.13.115"] = (
        "429 不计熔断失败", "Retry-After", "半开只放一个探针", "_leave",
        "http_ok", "裸包名", "pip freeze", "可用性", "journald",
        "知识索引", "test_knowledge_index_l0", "取证日期",
        "mcp_servers", "幂等 upsert", "显式拒绝", "按写方法判", "不引 YAML",
    )

    #: v0.13.114（P0-a + P0-b）：关键词取自版本注释里**实际写下的字句**。
    #: 核心是两条不可退让的口径：**取 adapter 回包值而非请求值**（否则归因自欺）、
    #: **NULL 严格区别于 'fail'**（否则失败率被虚增）。
    VERSION_ROOT_CAUSES["0.13.114"] = (
        "adapter 回包值", "NULL", "verdict", "verify_reason",
        "insufficient", "total_tokens", "超时",
        "outputguard", "标签型", "封闭枚举",
        "ledgernorm", "网关前缀", "厂商前缀", "只读生命线资产", "协议形状",
        "setdefault", "空操作", "override=True", "先过鉴权", "再验边界",
        "顺序无关", "modelprofile", "边界画像",
        "负载信号", "质量信号", "insufficient_sample", "unstable", "剔除",
    )

    #: v0.13.113（P0-1 收尾三项）：关键词取自版本注释里**实际写下的字句**。
    #: D5 轨迹 schema / D13 版本推进与陈旧判定 / D16 badcase 回写。
    VERSION_ROOT_CAUSES["0.13.113"] = (
        "TRAJECTORY_SCHEMA", "trajectory/1", "dataset_versions", "eval_is_stale",
        "record_eval_result", "badcases",
    )

    #: v0.13.112（D18）：关键词取自版本注释里**实际写下的字句**。
    #: 经验库接入联邦：新 kind hub_sqlite + 只召回活跃条 + 进默认集。
    VERSION_ROOT_CAUSES["0.13.112"] = (
        "D18", "hub_sqlite", "只召回活跃条", "FED_FAST_SOURCES", "SRC_ABBR",
    )

    #: v0.13.111（P0-1 收尾）：关键词取自版本注释里**实际写下的字句**。
    #: 真实轨迹源（profile_events）+ free 抽取 + 验收③ 真实召回取证。
    VERSION_ROOT_CAUSES["0.13.111"] = (
        "profile_events", "真实轨迹", "extract_trajectories",
        "openrouter/openrouter/free", "验收③",
    )

    #: v0.13.110（P0-1 切片 B/C）：关键词取自版本注释里**实际写下的字句**。
    #: 管线声明式、模型在最后、轨迹级聚合、Dreaming 三类 reason。
    VERSION_ROOT_CAUSES["0.13.110"] = (
        "声明式管线", "模型在最后", "轨迹级聚合", "Dreaming",
        "duplicate", "conflict", "expired", "assert_free_model",
    )

    #: v0.13.109（P0-1 切片 A）：关键词取自版本注释里**实际写下的字句**。
    #: 地基：四表 + grain 唯一键 + split 隔离（holdout 永不进优化批次）。
    VERSION_ROOT_CAUSES["0.13.109"] = (
        "grain 唯一键", "split 隔离", "assert_train_only", "dataset_version",
        "experiences", "经验库",
    )

    #: v0.13.108（P0-3）：关键词取自版本注释里**实际写下的字句**。
    #: 主判据：执行者不得自证 success；等待态让出执行资源；判不了不放行。
    VERSION_ROOT_CAUSES["0.13.108"] = (
        "执行者不得自证", "verifier", "等待态", "让出执行资源",
        "budget_exceeded", "insufficient", "verify_kind",
    )

    #: v0.13.107（P0-5 修正）：关键词取自版本注释里**实际写下的字句**。
    #: ① label 按 kind 取词（exec 不得冒充只读）；② `covered`/`fail_closed` 由绕过面
    #: 机械导出，不再手填布尔。
    VERSION_ROOT_CAUSES["0.13.107"] = (
        "label 按 kind", "受闸", "绕过面清单", "derive_coverage",
        "手填布尔", "覆盖全路径",
    )

    #: v0.13.106（P0-5）：关键词取自版本注释里**实际写下的字句**。闸门意图是
    #: 「注释掉了根因就红」——这条治的是「纳管 ≠ 可用 / 只读未证」：
    #: ① 判据三条（覆盖全部绕过路径 + fail-closed + 负向测试）才配叫**强制边界**；
    #: ② 唯一升级通道是 `NEGATIVE_PROOFS` 登记表，核心不变式是**只降不升**，
    #:    降级后只能说「未观察到写操作」。
    VERSION_ROOT_CAUSES["0.13.106"] = (
        "纳管四维", "强制边界", "未观察到写操作", "只降不升",
        "NEGATIVE_PROOFS", "observed_uncovered", "fail-closed",
    )

    #: v0.13.105 的两条根因都要在版本注释里可寻址（该闸门的作用是「注释掉了根因就红」）：
    #: ① 三层链路（原始事实 → 候选信号 → 研判）+ **空证据集不许投票**：
    #:    证据缺失/观测失败/规则炸/规则回 None 一律落到 `insufficient`，不许退化成「判否」；
    #: ② `decisive` = 「禁把 SKIP 当 PASS」的机械形态；`verdict IS NULL`（未研判）
    #:    与 insufficient 分开计数但**同样**让 decisive=False（迁移不替历史编证据）。
    VERSION_ROOT_CAUSES["0.13.105"] = (
        "三层链路", "insufficient", "空证据集不许投票",
        "decisive", "verdict IS NULL", "禁把 SKIP 当 PASS",
    )

    #: v0.13.104 的两条根因都要在版本注释里可寻址（该闸门的作用是「注释掉了根因就红」）：
    #: ① 账本必须**只读**（`?mode=ro`，CCR 生命线资产，写不进去有负向测试钉着）+ 聚合维度
    #:    白名单**故意不含 `logical_model`**（内部逻辑名，2026-09-07 判例致 jcode 不可用）；
    #: ② `COALESCE(cost)` 必须同时回 `null_cost`（免费模型实测 cost_usd=NULL，静默 0 = 谎报）
    #:    + 归因 `attribution=approximate`（探针 `hubprobe-` 实测 CCR 不透传 X-Request-Id）。
    VERSION_ROOT_CAUSES["0.13.104"] = (
        "usage_ledger", "mode=ro", "logical_model", "null_cost",
        "attribution=approximate", "hubprobe-",
    )

    #: v0.13.103 的两条根因都要在版本注释里可寻址（该闸门的作用是「注释掉了根因就红」）：
    #: ① 联邦融合被 TDAI 吃满（权重 1.0 + 满额 top10 + 无下限 ⇒ 其余 10 路永不出结果）——
    #:    关键字取自实际写下的字句（含那个「数学上」的判据）；
    #: ② 注入包无 q 时不查联邦却报 sources=3（「计数绿、内容空」）—— 含 fed.chars 与哨兵名。
    VERSION_ROOT_CAUSES["0.13.103"] = (
        "单源配额", "200/200", "5.2 路有货", "FUSE_TOP_FRAC",
        "距离类", "乱码查询 0.52", "fed.chars=0", "计数绿、内容空", "RECENT_Q",
    )

    #: 2026-10-05 起，历史版本的根因**逐字归档在 CHANGELOG.md**（main.py 的
    #: VERSION 注释块从 1417 行缩到 1047 行，只留当前版 + 一行指针）。
    #: 本闸门的「不许随 bump 蒸发」意图**不变**，只是把「在哪」从 main.py 放宽到
    #: 「main.py 或 CHANGELOG 任一处」—— 判据是**内容还在**，不是**文件没变**。
    #: ⚠ MAIN_PY 是**已读出的文本**不是 Path（见 :31），所以路径要从 REPO 拼。
    CHANGELOG_MD = REPO / "CHANGELOG.md"

    def test_version_comment_records_root_cause(self):
        # 版本钉随版本号走：它是一道**随行闸门**，逼迫 bump 的人回头看根因注释还在不在，
        # 而不是让上一版的注释默默过期。本闸门只跟当前版本号走，不承担跨版本归档职责。
        self.assertIn('VERSION = "%s"' % self.CURRENT_VERSION, MAIN_PY)
        seg = MAIN_PY[MAIN_PY.index('VERSION = "%s"' % self.CURRENT_VERSION):][:2400]
        for kw in self.VERSION_ROOT_CAUSES[self.CURRENT_VERSION]:
            self.assertIn(kw, seg,
                          "%s 的版本注释里丢了 %s —— 半年后没人知道当时为何这么改"
                          % (self.CURRENT_VERSION, kw))

    def test_earlier_version_root_causes_survive(self):
        """更早批次的根因关键字必须**仍在**（main.py 或 CHANGELOG）——版本钉只跟当前版走，
        但注释不许随 bump 蒸发，否则 0.13.65「全指标绿而功能层已死」那条教训就没了。

        2026-10-05：历史块搬进 CHANGELOG.md（`scripts/extract_changelog.py` 逐字搬运，
        368 行），main.py 只留指针。原判据只查 MAIN_PY ⇒ 归档后 22 条关键字判红。
        这是**闸门与新约定冲突**，不是代码坏了；改判据而不是把归档退回去——
        因为「两份各自会漂的副本」正是本仓反复吃亏的东西（CHANGELOG 与 README
        的测试计数就漂过一轮）。
        """
        try:
            changelog = self.CHANGELOG_MD.read_text(encoding="utf-8")
        except OSError:
            changelog = ""
        # ⚠ MAIN_PY 已是**文本**（见 :31），别再 .read_text() —— 那是本条的第二版。
        where = MAIN_PY + "\n" + changelog
        missing = []
        for ver, kws in self.VERSION_ROOT_CAUSES.items():
            if ver == self.CURRENT_VERSION:
                continue
            for kw in kws:
                if kw not in where:
                    missing.append("%s: %s" % (ver, kw))
        self.assertEqual([], missing,
                         "这些版本的根因关键字从 main.py 与 CHANGELOG.md 双双消失了：%s"
                         % missing)


if __name__ == "__main__":
    unittest.main()
