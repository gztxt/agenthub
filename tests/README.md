# tests/ —— 三层口径（唯一真相源：`tests/tiers.py`）

本目录不是"一套测试"，是**三套**，混跑会把"全绿"变成谎报。分层前本仓只有一个数字
（`Ran 71 tests ... OK`），而其中相当一部分断言的是**这台 NAS 上恰好存在的目录形态**
—— 换台机器（或干净 CI runner）它们必然红，于是"能不能上 CI"这个问题一直没有答案。

| 层 | 判据 | 换机行为 | 数量（2026-10-12 实测） |
|---|---|---|---|
| **L0 hermetic** | 只依赖纯函数 / `tempfile` / AST 读源码。不读 `~/.claude` 等真盘、不起服务、不打网络、不 fork pty、**不 import `src.main`** | 结论必须一模一样；**出现 SKIP 即分层放错**，闸门判 FAIL（退出码 2） | **1502** |
| **L1 host** | 断言本机真实仓库形态（`~/.grok/sessions`、`~/.claude/projects`、`~/.jcode/sessions`、`~/.qoder/projects`、`~/.hermes/state.db`、`~/.codex/state_5.sqlite`、`/fs` 真目录） | 显式 `SKIP(host-dependent)` + 因果与解法，**绝不静默通过** | **65** |
| **L2 live** | 需要服务在跑：`verify_*.py`、`probe_*.py`（逐只的“需服务 / 不需服务”二分待重测，见下节口径注） | 手动单跑；不被 `discover -p "test_*.py"` 收进来 | **60**（文件数，非用例数：`verify_*.py` 51 + `probe_*.py` 9） |

> ⚠ **2026-10-05 更新计数时踩过的坑，记下来防重蹈**：
> ① 三个数字**必须实测**，不能沿用上一版 —— 本表此前记 983/50/42，实际已是 1162/55/44
>    （CHANGELOG 里的 997 同样漂过）。`tests/test_changelog_drift.py` 现在会拦
>    「CHANGELOG 顶部版本 ≠ `src/main.py::VERSION`」，但**拦不住本表自身的漂移**
>    —— 本表的口径仍是「人工更新 + 偶尔对账」。
> ② `FORCE_COLOR` 会让本仓 22 条用例**假红**（node 把 `console.log(数字)` 染成 ANSI）。
>    `run_tier.py` 已从源头摘掉该变量，但**直接调 `pytest` 时仍会踩到** ——
>    此时加 `env -u FORCE_COLOR` 或直接走 `bash scripts/run_tests.sh`。

> 计数口径（写下来防后人重蹈“表里数字没来源”）：**1486/65** 来自 `run_tests.sh hermetic` 与
> `run_tests.sh host` 的 `[tier] ran=` 行；两者相加 1551 = 收集器对账报的
> `unittest=1551 = pytest=1551`，即**下面那张表与实际收集集一致**，没有“表里有、套件不收”。
> L2 60 是 `tests/verify_*.py`(51) + `tests/probe_*.py`(9) 的**文件数**，它不被 unittest 收，
> 故与前两行不同量纲。
> 历史：983/50/42（2026-10-02）→ 1162/55/44（2026-10-05 补完 v0.13.66~79 后）
> → 1261/55/44（同日再补本轮八批的新闸门）→ 1272/58/45（2026-10-07，v0.13.83；
> L1 多 3 例是 cursor 的 host 档用例）→ 1277/58/46（2026-10-07，v0.13.84；
> L0 多 5 例是鼠标分层闸门 test_term_mouse_tier.py，L2 多 1 只探针 verify_term_mouse_apps.py）
> → 1305/58/47（2026-10-07，v0.13.91）→ 1344/58/52（2026-10-08，v0.13.92；
> L0 多 39 例、L2 多 5 只探针为 09-07~08 多会话批次累计，本批新增
> `test_term_select_persist.py` + `verify_term_select_persist.py`）
> → **1486/65/60（2026-10-10，v0.13.101；09-09~10-10 多会话批次累计：
> L0 多 142 例（含本轮 LOG_DIR 隔离闸门由 1 例改 3 例）、L1 多 7 例、L2 多 8 只探针）**。
> 每一档都**按实测定稿**，旧数只作留痕、不作依据。

## 怎么跑

```bash
bash scripts/run_tests.sh hermetic         # 只 L0，以"零跳过"为闸
bash scripts/run_tests.sh hermetic-clean   # L0 + 把 HOME 换成空目录 ⇒ 真模拟干净 runner
bash scripts/run_tests.sh all              # L0 + L1（本机全量）
bash scripts/run_tests.sh host             # 只 L1
bash scripts/run_tests.sh changed [文件...]  # ★ 开发迭代用：按改动挑子集（**不是闸门**）
bash scripts/run_tests.sh pytest           # 整层交 pytest 跑（对账用途，标准入口不用它）
bash scripts/run_tests.sh probe verify_p1_backend.py http://127.0.0.1:3199
venv/bin/python -m unittest discover -s tests -p "test_*.py"   # 老口径（L0+L1 混在一起）
```

**`changed` 是子集，不是闸门**：它按"测试正文是否提及被改的模块"挑文件（`scripts/run_changed.py`），
映射不到的改动会**显式报出并以退出码 2 结束**（不静默当"没测试要跑"）。它的合法性由
`tests/test_run_changed_select.py`（L0 6 例）钉住。**提交/推送前一律跑 `scripts/prepush.sh`**
——子集绿 ≠ 闸门绿，映射漏了谁它不会知道。

`hermetic`/`host`/`all` 跑完都会附一步**收集器对账**：`unittest` 收进几例 vs `pytest`
收进几例，不等即红（只 `--collect-only`，约 2s，不拿 pytest 跑用例）。

强制开关：`HUB_HOST_TESTS=1|0` 覆盖自动探测（探测判据＝上面六个宿主路径是否**全部**存在）。

## 新测试该放哪一层

0. A4 索引投影层（`src/memindex.py`）的闸门是**现成样板**，但它同时是**分层放错的教材**：
   - `tests/test_memindex_proj.py`（G9~G17，33 例）全在 `tempfile` 里造夹具、**与真 `rg` 逐条对拍**
     ⇒ **L0**，且 `hermetic-clean`（空 HOME）下同样成立（对拍用的是 `rg` 二进制不是网络）。
   - `tests/test_memindex_glob.py` 的 **G18（3 例）是 L0**，**G19（3 例）是 L1**——
     G19 `enumerate_corpus(proj_specs())` 读本机真目录，已打 `@tiers.host_only`。
     初版把 G19 也放进 L0 且没打 host 标，结果 `hermetic-clean` 下 10 例集体报红，
     而失败文案恰好是「投影为 0 个文件」——**与该闸门要抓的真 bug 字面同形**。
     教训：分层放错的代价不是「报红」，是「报出一个与真故障无法区分的红」，人只会当成目录没了。
   两条值得照抄的写法：**用真 rg 对拍**（而非断言硬编码清单）、**红向用例自带对照组**
   （`test_g15_不脱敏就会泄密_对照组` 那种）。
1. 能用 `tempfile` 造出前提 ⇒ **L0**（首选）。例：`test_staticguard.py` 全部在临时目录里造
   `static/`、兄弟目录 `static_evil/`、软链、`.bak` 件。
2. 必须读这台机器的真实仓库 ⇒ **L1**，打 `@tiers.host_only`（class 或 method 都行）。
3. 需要一个在跑的 hub ⇒ **L2**，命名 `verify_*.py` / `probe_*.py`，`HUB_BASE`/首参给地址，
   并**自带红-绿对照**（同一份探针打改前/改后两棵树，别只给一个绿灯）。

## 三条硬规矩（都对应过真实事故）

- **闸门必须写进 `unittest.TestCase`（模块顶层裸 `def test_*` 收不到）**：
  `unittest discover` 只收 TestCase 子类，模块级 pytest 风格函数它**看不见** ⇒
  "标准套件全绿"与"新闸门跑过了"变成两句话。2026-10-02 实测踩中：
  `pytest tests/` 975 例、`run_tests.sh all` 961 例，差额正是 14 条裸函数闸门，
  而报告里"961 全绿"并不假 —— 它只是没覆盖本批。现由
  `tests/test_tier_collector_parity.py` 三条闸门封死（裸函数为 0 / 每个文件都有份 /
  两个收集器例数相等），每条都带**自证样本**，避免元闸门自己恒绿。
- **不 import `src.main`**：它一 import 就跑 lifespan —— 开真库 `data/agents.db`、起后台
  探针任务、绑端口。单测碰它等于碰生产数据。需要测路由里的判据时，把判据抽成纯函数
  （见 `src/staticguard.py`：正是为此从路由里抽出来的）。
- **不靠 `skipTest` 蒙过 L0**：L0 的 `skip` 在干净 runner 上意味着"这条什么都没测却报绿"。
  能力探测失败就退化成一条仍然成立的断言（`test_staticguard.py` 里软链两例就是这么写的），
  或者干脆打上 host 标。

## 为什么 L2 探针是必需的，不是"手工验证"

`test_*.py` 看不见需要两条并发 WS 才显形的缺陷。P1-5 就是例子：`Session.outputs` 曾是
**单个** `asyncio.Queue`，每条 WS 的 pump 在同一队列上 `get()` ⇒ 桌面 + 手机同看一条会话时
两个消费者互相偷字节。`verify_p1_backend.py` 对改前/改后两棵影子树的实测：

```
改前 :3197   观看者A 实收 30 / 观看者B 实收 90（共 120 条被劈开，集合互斥）  → FAIL
改后 :3199   观看者A、B 各 120 且 A-B=[] B-A=[]；hb 回执 {"type":"hb","t":…}   → PASS
静态闸门     .bak 200→404；/static/../static_evil/secret.txt 200(CANARY)→404；304 现带 Vary
```

## 前端解析层的缺陷只能拿真浏览器测：`verify_replay_gate.py`

`test_*.py` 跑在 node/venv 里，看不见 xterm 的**自动应答**行为。这类缺陷的真实形状：
回放帧里夹着上一个 TUI 发过的终端查询，xterm 替终端作答，答案顺 `onData` 灌进 pty，
shell 不认就原样回显 —— 用户看到的是「白屏 + 一串数字字母乱码」。

这个探针有三条硬规定，缺一条它就会给出假绿灯：

1. **抽真代码，不抄示例**：用正则从 `static/hub.js` 里原样抽出 `TERM_QUERY_OSC` 与
   `function termWriteReplay(){…}` 拼进测试页。谁把 DCS 那行删了探针就红；
   手抄一份「我以为的实现」则删真代码也照样绿。
   （抽取按**第 0 列的 `}`** 收尾，不做花括号配对 —— 注释里含 `{prefix:"?",final:"c"}`，
   朴素配对会被注释里的 `}` 提前闭合，截到看不见 DCS 那行，第一版就栽在这。）
2. **对照组必须在同一份产物里**：先不设防写一次（期望**有**应答包），再经闸门写一次
   （期望**无**）。只有"红绿同时在场"才排除掉"解析器被别的东西闷掉了"这种假改善。
3. **线序按字节而不是按文档**：DECRQPS 的中间码只有一个 `$`；照文档写成 `DCS $ q`
   （带空格）时 intermediates 变成 `"$ "`，压根匹配不上处理器，
   于是会得到「xterm 不答 DECRQPS」的**假结论** —— 本探针第一版就是这么错的，
   是它自己的 FAIL 把真代码里被覆盖丢失的那行暴露出来。

```
$ venv/bin/python tests/verify_replay_gate.py
  对照组（不设防）xterm 回了：  "P1$r0"q\"  "P1$r1;10r\"  "[?1;1R"
  实验组（经 termWriteReplay）回了： （空）
  PASS  对照组确实会作答（DECRQPS 两类都答）   [实得 2 个 DCS 应答包]
  PASS  对照组 DSR 也作答（证明 onData 通路活着）
  PASS  实验组零应答（闸门真的吞掉了）   [实得 0 包：[]]
  全部通过 ✅   退出码 0
```


## 浏览器探针（离线四只 + 线上三只）：前端解析层的缺陷只有真引擎能作证

> 计数口径：**上面三列数字均为 09-24 19:0x 实测** —— L0 265 / L1 35 取自
> `bash scripts/run_tests.sh hermetic-clean` 与 `... host` 的 `[tier]` 行；
> L2 29 = `ls tests/verify_*.py`(27) + `ls tests/probe_*.py`(2)。
> **09-25 00:4x 复点**：L0 271（新增 `test_entity_mode_source_of_truth.py` 6 例）；
> L2 30 = `verify_*.py`(28，新增 `verify_claude_menu_term.py`) + `probe_*.py`(2)。
> **09-25 01:5x 复点**：L2 31 = `verify_*.py`(29，新增 `verify_term_key_relay.py`) + `probe_*.py`(2)。
> **09-25 09:2x 复点（会话 `01a0d5db`，worktree `wt/01a0d5db`）**：**L0 313**（新增 42 例：
> `test_gwprobe.py` 19 / `test_healthx.py` 8 / `test_sessions_export.py` 15），`hermetic-clean` 实测
> `ran=313 skipped=0 failures=0`；**L1 host 35 不变**（`skipped=0`）。L2 未动（本批零前端改动）。
> 三只新闸门均 **不 import `src.main`**、零网络（`gwprobe.fetch` 被替换）、零真盘 ⇒ 分层合格。
> 旧版“23 只 = 16 需服务 + 7 离线”的拆分已随文件集变化作废（此后新增的
> `verify_kb_federation` / `verify_skill_facade` / `verify_mcp_facade` 都是 in-process 不占端口），
> 但完整的二分没逐只重数 ⇒ **宁可不写数字，也不拿旧拆分冒充实测**。

共同硬规定（三条都是踩出来的，缺一就变成"自己说好了"）：

1. **被测代码从 `static/hub.js` 原样抽取**（`tests/_hub_extract.py`），不手抄 —— 手抄的
   副本会在重构时静默失联，抽取版则"函数被改名/删行"直接把探针判红。
2. **对照组与实验组必须在同一份产物里**：每个探针都同时跑"改前写法"与"改后写法"，
   并断言改前**确实坏**。只证明改后能用 = 零信息（可能那条件根本不触发）。
3. **测线上字节，不测文档式间距**：`ESC P $ q "q ESC \` 里 `$` 后带空格，intermediates
   就成了 `"$ "` 而非 `"$"` —— 按 README 里的写法造样本会造出一个永不匹配的探针。
4. **面板类诊断工具必须一行一键**：`verify_diag_panel.py` 连吃两次假 FAIL —— 键名里带 `=`、两个键挤同一行，都会让 `split('=', 1)` 取不到值。取不到值时断言必须**红**，不得静默通过。
5. **CDP 的 `Fetch` 域按 session 启用**：`failRequest` 必须与 `Fetch.enable` 用**同一条连接**；另开一条去 enable、再拿旧连接应答 ⇒ `Fetch domain is not enabled`（本轮真实踩过）。
6. **报障为「点不动 / 被盖住」时必须真点一次**：`elementFromPoint` 只回答「此刻谁在最上面」，
   不回答「用户那一下落在哪、之后残留了哪一层」。09-23 在这上面连吃三轮「探针全绿 + 用户说还是
   坏的」—— 必须用 `Input.dispatchMouseEvent` 按坐标点，并断言**导航之后**浮层为空。
7. **闸门必须自证「能抓住已知事故」**：红基线优先用 `Fetch.fulfillRequest` 把**修复前的 git 版本**
   喂进同一页面（不往生产目录写文件）。只在修好的代码上跑绿不算数 —— 还必须证明**这条路径真的被
   执行过**（本次靠服务端 `term/history` 命中定案，而不是靠面板自证）。
   另：**改了缓存策略会打穿自己的闸门** —— v0.13.11 上 `immutable` 后 Chrome 对已缓存
   的 `hub.js` 连请求都不发 ⇒ `Fetch` 无事件可拦 ⇒ 红况**静默变绿**（实测第二次跑就栽在
   这里）。凡红绿靠请求拦截，必须**冷档案**（`tempfile.mkdtemp()` 作 user-data-dir）。
8. **浮层必须唯一且可逃生**：同一时刻最多一个抽屉是 `on`；任何导航都要清掉抽屉；遮罩只有一个
   计算出口，且点它一次关干净（手机上没有 ESC，点空白是唯一逃生路径）。

9. **分档行为不得依赖「按 origin 隔离的存量」**：凡是按视口档位生效的交互，其
   结果不得因 `localStorage` 里的历史值而改变——同一份代码在局域网 IP 与
   Tailscale IP 上是**两个不同的 origin，各有一套 storage**，一旦首屏判定读取存量，
   用户就会看到「浏览器正常 / APP  abnormal」这类**看起来像网络问题**的分叉。
   09-23 23:3x 四格实测（两个 origin × `hub.hist` 有/无）证明差异 100% 来自存量、
   与 Tailscale 无关；修法见闸门 `verify_collapse_symmetry.py`：窄屏首屏**不恢复**
   `hub.hist`，宽屏照旧记忆。写代码时的口径：判定用 `hubNarrow()`（断点唯一真源
   在 `01-core-boot.js`，不变量 3），**不要**再自己 `matchMedia` 或比 `innerWidth`。


10. **改作用域的活必须起真服务测**：09-24 两次假动作都是 `ast.parse` 通过、逻辑已废——
    ① 以 0 缩进往 `if static_path.exists():` 里插类，把该块截断，`_GZ_SUFFIX` 与路由函数
    一起掉进类的命名空间 ⇒ `/static` 全 500；② 覆写 `FileResponse.set_headers`，而本
    Starlette 版本没有这个钩子（真钩子是 `set_stat_headers`，在 `__call__` stat 后才调）
    ⇒ 方法是死代码，头照发。**判据：新闸门 `verify_static_headers_matrix.py` 用影子实例
    （随机端口 + 隔离 `DATA_DIR`）跑响应头矩阵，并带红臂自证裸 FileResponse 确实补 etag。**
    推论：覆写第三方类的私有/半私有方法前，必须先确认该方法在本版本里真的存在被调用。


| 探针 | 钉住的缺陷 | 对照组实测 | 实验组实测 |
|---|---|---|---|
| `verify_replay_gate.py` | 回放历史时替终端作答查询码：ring 里躺着的 `DECRQPS`（`DCS $ q`）会被 xterm 应答并**注回 pty**，成为可见垃圾。改前的闸门只列 DA1/DA2，**DA3  `{prefix:'?',final:'c'}` 在本机 vendor 版里根本不存在**（grep 实测），真正漏的是 DCS 族 | 直接 write → xterm 回 2 个 DCS 包 + DSR | 经 `termWriteReplay` → **0 包** |
| `verify_term_heal_viewport.py` | 白屏自愈按**缓冲区绝对行**数空行：`getLine(0)` 是 scrollback 最老一行，有历史时读到的是早已滚出屏幕的内容 ⇒ 判"画面不空"⇒ **自愈在长会话里永不触发** | 60 行历史 + ED2 清屏：旧口径 blank=**0**（被误抑制） | 同一现场新口径 blank=10/10（会自愈）；无 scrollback 时两口径一致（防回归） |
| `verify_stream_decode.py` | 每帧 `new TextDecoder()`：UTF-8 多字节被 WS 帧劈开时**必然**吐 U+FFFD；`[?1003h` 跨帧时两帧都匹配不上正则 ⇒ `termMouseLive` 记不下 ⇒ 滚轮上报被闸门吃掉 | '中文终端输出…' 在第 2 字节劈帧 → `'??文终端输出…'`；DECSET 跨帧逐帧扫 → `termMouseLive=false` | 共用解码器 + 24 字符尾巴 → 逐字相等 / `true` |
| `verify_key_focus_guard.py` | 全局 keydown 无 target 守卫：终端里敲 `/` 被 `preventDefault` + 焦点跳搜索框；`term.focus()` 无条件执行 ⇒ 手机自动挂载必弹软键盘 | 真事件冒泡到 document 且 `e.target.closest('.xterm')` 命中（**证明旧写法必劫持**） | `keyTargetIsEditing` 8 个输入位 true / 2 个普通元素 false；`termFocusWanted` 只认 `user:true` |
| `verify_sidebar_narrow.py` | 线上五场景：窄屏首屏不得被 `position:fixed` 白底抽屉接走视口中心；**首屏解析不得写 localStorage**；跳断点（拖宽/转屏）必须自动重算；一档一键不得互渗（2026-09-23 白板事故本体） | —（红-绿对照在下一只） | A~E 5/5 PASS：390px `collapsed=true w=48 centerHit=div.hs`；桌面 `w=240` 不回退 |
| `verify_sidebar_redgreen.py` | 同上缺陷的**红基线**：旧实现按内容从 git 回溯（不钉 HEAD、不读 `.bak`），只经 CDP `Fetch` 喂给本 headless 客户端 ⇒ 生产零影响 | 改前字节 + 污染值 + 390px：`collapsed=false w=236 pos=fixed` 且中心被抽屉接走 | 同条件下生产真字节：`collapsed=true w=48` 中心不受影响 |
| `verify_overlay_exclusion.py` | 窄屏「设置」抽屉（`min(400px,92vw)`/`z:60`）盖掉 92% 画面且**永不随导航关闭**：`#btnSettings` 用 inline `onclick` 旁路了侧栏事件委托（而“点完收抽屉”只写在委托里）+ `go()` 不清抽屉 + 遮罩只看侧栏态不看抽屉态 ⇒ 手机上「整页白板、完全点不动」且无逃生路径 | 红基线在 `test_overlay_exclusion.py` L1：git 内容搜到 `bd0f84a8`（HTML 带 `onclick="openSettings()"`、无 `data-settings`）与 `119f1c7b`（hub.js 无 `openOverlay`/`closeDrawers`） | 真鼠标 16/16 PASS：★点「总览」后残留层=`[]`、中心命中 `div.hs`、`pageOn=page-classroom`；点遮罩留白一次关干净；桌面无回退 |
| `probe_sidebar_width_sweep.py` | 通用诊断（不钉缺陷）：320→1920 **宽度轴**一次扫完侧栏几何/可见按钮数/中心接住者/JS 异常 | 320~767：收起 48px、6 按钮、0 列表项（图标条设计如此） | 768+：展开 240px、14 按钮；**全带无空白** ⇒ 把方向从“布局/几何”逼到“事件与浮层残留” |
| `verify_page_fallback.py` | `go()` 直接吃 `localStorage.getItem('hub.page')`，而这个值可能是**跨版本已改名的页名**（09-20 界面统一改过一批）⇒ 认不出时 `toggle('on', s.id === 'page-' + page)` 把**所有页面一起关掉** ⇒ 正文整块空白、页内零个可点元素。而 localStorage **按 origin 隔离** ⇒ 同一份代码『局域网那个源正常、Tailscale 那个源空白』 | 9 个取值里 7 个（dashboard/manager/agents/watch/terminal/乱写/overview）→ `section.page.on=(无)`、可点数=0 | 修后 9/9 全部落到 `page-classroom` 且有可点元素 ⇒ 退出码 0 |
| `test_tdz_order.py`(L0) + `verify_hist_tdz.py` | **顶层 IIFE 早于 `let` 声明 ⇒ TDZ**：`histBootstrap()` → `histLoad()` → `renderNav()` 读到 1861 行才声明的 `_navHtml` ⇒ hub.js 当场死亡（菜单空白 + `initSidebar` 从未执行 + 抽屉停在展开态遮住正文）。门闩 = `histOpen ∈ TERM_HIST_AGENTS` **且 localStorage 有 `hub.term.token`** ⇒ 手机那份有、浏览器那份没有 ⇒ 这才是「浏览器正常/APP 异常」「局域网正常/Tailscale 异常」的真判据 | 红：**用 `Fetch.fulfillRequest` 把修复前的 HEAD 版喂进同一页面**（不往生产 static 目录写文件），逐字符复现 `JS异常 @ hub.js:1907` + `navTree子项=0` + `collapsed=false` | 绿 14/14，且主动调 `histLoad('claude')` 返回 DRIVE-OK、服务端确有 `/api/term/history` 命中（第一版只注入 localStorage 就报 8/8 绿，被日志否证为**空转**） |
| `test_gwprobe.py`(L0) | 模型 ID 失效而 `/health` 全绿（本机三次同源事故：09-06 `minimax-m3:free` HTTP 400 / 09-19 `'ultra'` 无效 / 09-23 缺 provider 前缀）⇒ 上游注册清单必须可观测；另钉住一条**改判**：`agnes/agnes-2.0-flash` 并不在免费池（实测 14 个 ID 里 5 个 free 全是 `openrouter/*:free`），`PT-20260923-05` 的旧断言不得再当前提用 | 变异：`watch` 改成恒真（模拟上游改名看不出来）⇒ `test_watch_detects_rename_not_stuck_true` **FAIL** | 复原后 19/19 绿；另含凭据泄漏闸门（配置里的 key 值不得出现在 `status()` 任何字段）、`200 但清单为空` 必须区别于 ok、冷缓存 `unknown`+后台单飞 |
| `test_healthx.py`(L0) | 只看 `last_sweep` 会被"新一轮扫了 6 家、漏了第 7 家"骗过 ⇒ 那一家可以在册、心跳新鲜、却从没被检测（＝09-22「在册却静默不可用 21 天」的形态）；另拦 NaN/Inf 被序列化成裸 `NaN` 把前端 `JSON.parse(/health)` 整页炸掉 | 6 家刚扫过 + 1 家无 `checked_at` ⇒ 断言 `state=ok` 但 `unchecked=1`、`oldest_check_age_s=20.0`（最坏值必须浮出来）；`STALE_ROUNDS` 边界正反各一例 | 8/8 绿；脏输入（None/字符串时间戳/`sweep_every=0`）只降级不抛 —— `/health` 是自证端点，情报字段不得把它打挂 |
| `test_sessions_export.py`(L0) | 导出默认脱敏**必须递归**：JSON 把正文嵌在 `transcript` 里，只扫顶层字符串会整层漏掉 —— 而那一层恰恰是唯一带正文的地方；且脱敏**不得静默**（命中数要回报，`redact=0` 必须逐字 faithful） | 变异：脱敏改回"只扫顶层" ⇒ `test_nested_transcript_is_redacted` **FAIL** | 复原后 15/15 绿；含 CSV 表头恒定/缺列留空/多列丢弃/逗号换行转义、文件名 ASCII-only（端侧 WebView 的 `Content-Disposition` 中文行为不一）、未知 format 回落 JSON 但如实记录请求值 |
| `verify_diag_panel.py` | 端侧自检面板 `?diag=1` **自身失效**就等于永远拿不到端侧事实（本机对手机/APP 零探针是长期缺口）。给它做红绿：绿=如实报『异常清单: 无』；红=**同一条 CDP 连接**掐断 `hub.js` | 红况面板精确报「资源加载失败 …/hub.js」+ `go函数=undefined` + `navTree子项=0` + **`侧栏collapsed=false`（证明 JS 不跑时抽屉停在展开态遮住正文）** | 绿 8/8 + 红 6/6 + 无 `?diag` 时面板不存在 ⇒ 17/17 |
| `verify_collapse_symmetry.py` | 窄屏「点 agent 名称后是否自动收起侧栏」**依赖 `hub.hist` 存量** ⇒ 同一动作在局域网 / Tailscale 两个 origin 上分叉（用户报「Tailscale 好像不行」） | 改前四格矩阵：hist 空→`collapsed=False`、hist='claude'→`collapsed=True`（两 origin 各自一致、彼此不同） | 改后四格全 `collapsed=False`，对称性成立 ✅ |
| `verify_narrow_default_iconbar.py` | 窄屏首屏是否**图标条**（用户 09-24 裁定的默认态） | C 格掐断 hub.js ⇒ `collapsed=False`(236px 铺满) | A 格无存档 ⇒ `collapsed=True`(48px 图标条)；B 格本档存 `'0'` ⇒ `False`（证明非 stuck-true） |
| `verify_ls_guard_live.py` | `localStorage` 抛异常/quota 满/存储被禁 ⇒ 启动链断裂 | CDP 注入 `0822164` 旧字节 + setItem 抛 ⇒ `navTree=0` + `Uncaught SecurityError` | 新字节下三种破坏仍 `navTree=3 collapsed=true fails>0` 且零未捕获异常 (23/23) |
| `verify_static_headers_matrix.py` | immutable 分支带 `etag`/`last-modified` ⇒ 端侧可拿 304 复用"提手对不上"的旧副本；及改作用域后 `/static` 全 500 而语法检查放过 | 红臂：裸 `FileResponse` 构造同文件 ⇒ 头里确实有 `etag=`（判据有力度） | 影子实例 18/18：immutable 无校验器且永不 304、revalidate 保留 ETag+304、gzip 侧同样干净、服务出参与磁盘逐字节一致、生产 pid/version 未变 |
| `verify_claude_menu_term.py` | 菜单点「Claude Code」进不去终端页：`openEntity()` 与 `defaultModeOf()` **各写了一份** embed>term 优先级，而 claude 恰是唯一同时带 embed（discovery 见 cloudcli :3010 端口活就注入「原生会话」）与 term 的 Agent ⇒ 点行必进那张要独立登录的 iframe（实测 `:3010` 首页＝"Welcome Back / Your session expired"）；v0.12.3 又把行内动作图标 `display:none`、模式 tab 也已停用 ⇒ **站内零出口**。另一半：`gotoChat` 把**推导出的**形态无差别写进 `hub.chatmode.<id>` ⇒ 默认即成假偏好，只改默认救不回存量浏览器 | 红基线跑在修复前 HEAD（`fa14a0d`）影子实例：R3 `mode=embed`、R5 取不到 `term` 对象、R7b 无切换按钮 ⇒ 8/15 | 绿 14/14：真鼠标点行 → `#termPane` 在、`#embedPane` 不在；点「+ 新会话」→ xterm 屏幕真出现 Claude Code TUI 首屏，且服务端多出一条 claude 活会话；对照组 Pi（有 embed 无 term）仍进 embed（证明判据非恒绿）；「原生界面」按钮可切回嵌入页，且**只有这一步**写偏好 |
| `verify_term_key_relay.py` | 终端页焦点一掉到终端外，按键就**静默丢失**（用户 09-25 报「Grok 会话窗口空格不能用，一按就出现重复的文字内容」）。先证明链路不重复发：把 pty 设成 `-echo -icanon` 交给 cat 逐字对账，`hello`/空格×3/IME 上屏都只到一份；再证明丢键发生在焦点上 —— `焦点=BODY` 时敲 `c`+空格+`d`，pty 实收 **0 份**。用户接着点一下终端再敲，而 Grok TUI 在主屏缓冲区反复重画整屏（首帧无 `?1049h`）⇒ 同一份文字出现两遍，现象被报成"空格一按就重复" | 红基线跑在改前影子实例：★B 实收 `''`（判据要求 `c d`）⇒ 9/10；A/C/D/E 改前也成立，证明 B 的红来自缺陷本身 | 改后 10/10：★B 实收恰 `'c d'`（一份，不多发）· C 搜索框敲 `e f` 时 pty 收 0 份且文字进搜索框（P2-11 不劫持输入位）· D 文档与终端视口都不因空格滚动 · E 零 JS 异常 |

### 真页面探针的另一条硬规定：CDP 必须有常驻读线程

`_cdp_min.py` 的存在理由：第一版探针在 `Page.reload` 后 `sleep(3)` 再取结果，期间到达的
`Fetch.requestPaused` **没人应答** ⇒ 被拦资源永远 pending ⇒ 页面吊死、量到半成品。
“事件随时会插进来”是 CDP 的基本事实，不能拿“请求-应答”的直觉去写。

对应的**结构性**护栏（不需要浏览器、进 L0 常规跑）在 `tests/test_term_focus_policy.py`：
它守的是"位置"而非"存在" —— `term.focus()` 只能出现在 `termFocusWanted(opts)` 之后同一行、
`if (editing) return` 必须排在 `/` 与 Ctrl+K 分支之前、四个用户主动入口与两个自动路径的
`user:true` 数量与身份钉死。变异检验已做：把守卫挪到分支之后 / 给自动挂载补 `user:true`，
两条各自变红。


## 生产体检：`verify_prod_smoke.py`（重启后出结论用的就是它）

09-23 用户裁定「探活最多 2 次即停」之后，探针的设计口径变了：**一轮取齐全部证据**，
而不是一个端点一个端点串行试。这个脚本对真生产做 19 项复合检查（响应码 + 时间戳 +
`/health` 语义字段 + 头大小写 + WS 业务关闭码 + 日志异常计数），**且刻意不建 pty 会话**
—— 重启后跑它不会打断任何在用的终端。

三条实测出来的使用注意（都是本探针第一版自己踩的）：
1. 响应头必须**大小写不敏感**地读：uvicorn 发的是小写 `vary`，`dict(r.headers).get("Vary")`
   恒为 None，会把一条正确实现误判成 FAIL；
2. 两个终端端点外层各包一级（`{"sessions":[…]}` / `{"agents":[…]}`），按裸列表断言必错；
3. WS 关闭码用 `recv()` 超时后读 `ws.close_code`；
4. chat 端点的回复字段是 **`response`**（实测顶层键：`agent_id/session_id/message/timestamp/success/agent/response/model/usage`）—— 按 `reply`/`text` 读会把一条成功的真 LLM 回复判成空字符串（照抄 `verify_p1_backend.py` 的可用取法，
   别在第二个文件里发明第二种）。

`--no-chat` 可跳过那一次真 LLM 调用；不带则该脚本共 19 项。


## `verify_write_gate.py`：一条**只能有一半在生产上跑**的探针

写端点闸门（32 条被拒 / 2 条自带鉴权）的取证有个硬约束：**"带凭据逐条打写端点"这一腿
在生产上做不得**。09-23 就是拿匿名空 body 打生产做红基线，`POST /api/memory/l2/rebuild`
回 200 并把用户手写的 L2 记忆重写了（已按 09-06 在册副本逐字回滚）。所以本探针分两模式：

- `--safe`：只打无凭据那一腿。401 由中间件在 handler **之前**返回 ⇒ 可证明零副作用，生产可跑。
- 默认（影子树）：无凭据 401 + **带凭据非 401** 两腿都打。缺后一腿就是假绿 ——
  一个把所有请求一律 401 的闸门同样能让 `--safe` 全绿。

影子树起法与隔离核验（`fd` 指向自己的 DATA_DIR、指向真库必须 0）：

```bash
mkdir -p /tmp/ah-gate/data /tmp/ah-gate/log && cp -r src static templates /tmp/ah-gate/
cd /tmp/ah-gate && DATA_DIR=... TERM_TOKEN=gate-shadow-token nohup <repo>/venv/bin/python -m uvicorn src.main:app --port 3197 &
```
不复制 `.env` ⇒ 凭据只来自命令行，影子树里根本没有生产口令。

两处期望是**故意放宽**的（放宽方向经过判断，不是将就）：
`/api/settings/term-token` 在未配 `HUB_PASSCODE` 时返 503 —— 那是 fail-closed 的正确行为，
记成失败会诱导后人"为了让探针绿去给影子塞口令"；`/telemetry/events/{source}` 空 body 先撞
Pydantic 校验返 422，其鉴权在 handler 内（`hook.py` 的 Bearer/回环方案），不归本闸门。
