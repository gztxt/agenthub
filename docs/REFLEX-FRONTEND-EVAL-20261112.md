# Reflex 评估与 agenthub 前端升级执行计划（2026-11-12）

> 接单四查（`scripts/intake.sh`，1.3s，判定 ready）：记忆 5 命中/4 相关｜知识库 5 命中/3 相关
> （`前端大文件代码优化策略`、`前端记忆中心首屏优化`、`Execute-Plan-PR计划执行`）｜技能 0 命中
> （最高分 0.191 < 门槛 0.40，未外扩）｜参考库未查（deep=off，任务未触发，原因见 §9）。
> 本文件所有数字均为 2026-11-12 本机实测，非引用旧账。
> 修订①：按 `36 / 12 / 6` 实测分层更正闸门数（原稿误写「30 个前端闸门」），
> 并把 T5 的「双入口一周过渡」改判为「产物 URL 长期兼容契约」。
> 修订②：用户裁定「按此执行」——两项裁决落地（模块图校验双轨并存、契约只保文件/URL
> 不保首屏加载），补模板运行时探针 3 类；开工前置实测见 §6 末。

## 1. 结论

**不引入 Reflex。** 用「零构建的渐进式重构」升级现有前端，分四批（P0→P3）。

三条硬理由（每条都可证伪，见 §3）：

1. **Reflex 覆盖不了最大的一块**：`04-terminal-ws.js` 224KB，占前端 JS 31%，是 xterm 6.0 + PTY 二进制流；
   Reflex 的状态模型是「事件 → 服务端 State diff → 回传」，xterm 只能作为不透明自定义组件整体包进去。
2. **架构方向相反**：现在是 1 个 uvicorn 进程、0 构建、0 npm（`DESIGN.md:83` 明写「无框架无 npm 构建」）；
   Reflex 需要 Node/Bun 构建 + `.web` 生成产物 + 第二个前端进程/WS 代理 ⇒ 与既定设计及
   AGENTS「新增常驻服务必须进健康探针」「托管配置不写死版本号」两条都冲突。
3. **收益是开发者体验，不是用户可见性能**：21 页 587 个顶层函数全部重写，换来的是「用 Python 写 UI」；
   同时 `tests/*.py` 351 个文件里有 **36 个依赖 `static/hub.js` 单文件产物**（12 个经
   `tests/_hub_extract.py` 抽真代码、6 个 CDP 探针写死该 URL；口径见 §6）。整体替换会让这批闸门的
   **被测对象消失** ⇒ 端到端复验能力归零，不是「少几个测试」。

**当前前端真正的瓶颈不是框架，是四件可测的事**：静态资源零缓存、113KB CSS 内联在每次首屏、
723KB JS 全量加载无分割、21 页 537 个 div 首屏全部渲染。这四条用现有技术栈就能修。

## 2. 实测基线

| 指标 | 实测值 | 取证方式 |
|---|---|---|
| `templates/index.html` | 317,829 B / 3,788 行 | `wc -c -l` |
| ├ 其中 `<style>` 内联 | 113,566 B（36%） | 定位 style 块 |
| └ 其中 HTML 结构 | 146,359 B | 同上 |
| 页面数 | 21 个 `<section class="page">` | `grep -c` |
| DOM 首屏全渲染 | 537 个 `<div>`、452 个 id | `grep -o` |
| `static/hub/*.js` 真源 | 15 片 / 10,851 行 / 630,076 B | `wc` |
| 构建产物 `static/hub.js` | 722,877 B（纯拼接，不压缩不转译） | `scripts/build_hubjs.sh` |
| 顶层全局函数 | 587 个（0 个 `import`/`export`，0 个 `type="module"`） | `grep` |
| 前端引用的 `/api/*` 端点 | 62 个 | `grep -oh` |
| `setInterval` 轮询点 | 15 个（散落 7 个分片） | `grep -c` |
| 内联 `onclick` | 86 处 | `grep -c` |
| 传输体积（br） | index 78,284 B（-75%）｜hub.js 214,186 B（-70%）｜xterm 108,955 B（-78%） | `curl -D-` |
| **静态缓存头** | **全部 `cache-control: no-cache`**（含 hub.js / xterm.js） | `curl -D-` |
| 仓内 `.bak` | 148 个 / 29 MB（shard 69 + template 79） | `ls` |
| `tests/*.py` 总数 | 351 | `pathlib.glob` |
| 依赖 `static/hub.js` 产物的测试文件 | **36**（分层 12 / 6 / 19，口径见 §6） | 逐文件匹配 |
| 仅提 `hub.js.bak` / 文案、不依赖产物 | 27 | 同上 |
| 必须重写的闸门 | 1 文件 / 2 类 / 5 断言（`tests/test_hubjs_split.py`） | `python3 -m unittest ... -v` |
| 产物引用点（非测试） | `scripts/build_hubjs.sh` 9 处、`templates/index.html` 12 处 | `grep -c` |
| 运行形态 | 1 进程 uvicorn :3102，Brotli 已开 | `curl` 头 |

结论性读数：**brotli 已经在做压缩了，所以「上压缩」不是剩余优化空间；剩余空间全在缓存策略、
首屏体积与代码分割**。二次加载目前每个静态资源都要回源 revalidate（ETag 304），跨 Tailscale 时
每次冷开页面多付 3~5 个 RTT。

## 3. Reflex 是什么（事实，非评价）

| 项 | 事实 | 来源 |
|---|---|---|
| 许可 | Apache-2.0 | `pyproject.toml` |
| Python | `>=3.11,<4.0` | `pyproject.toml` |
| 编译目标 | **React 前端 + FastAPI 后端** | 官方架构文（2024-03-21） |
| 状态 | 服务端 `rx.State`，WebSocket 同步；UI 只编译成 JS | 同上 |
| 生成物 | `.web/` 为生成目录，「never edit or commit」 | 官方 Installation 页 |
| 写 UI 的方式 | 官方给 AI 的规则原句：**"Do NOT write JS, HTML, or JSX"** | 同上 |
| 自定义组件 | 能包 React 库，但方式是「npm 包 + library/tag 声明」，不是直接复用现成文件 | Wrapping React 文档 |
| 自托管 | 单服务器，或「导出静态前端 + 单独跑 Python 后端」，私网需代理 WS | Self Hosting 文档 |

## 4. 三个硬不适配点（逐条给判据，不是感觉）

### 4.1 终端流不在它的模型里

- 事实：`04-terminal-ws.js` 224,548 B / 4,079 行，xterm 6.0 + 6 addon（本地 vendor，无 CDN）。
- 判据：xterm 必须以**命令式 API 逐块写入**（`term.write(bytes)`），帧率与延迟要求远高于
  「事件 → 服务端 diff」的往返；Reflex 的 state 是服务端权威、按事件粒度回传，
  **不是二进制流通道**。
- 后果：这块只能原样包成一个自定义组件挂进 Reflex 页面，Python 侧看不到行内容
  ⇒ Reflex 只能接管「壳」，接管不了核心交互。这等于在最贵的部分之外再加一层。

### 4.2 与「无构建」的设计前提相反

- 事实：`DESIGN.md:83` 与 AGENTS 都明确本仓前端是「原生 JS，无框架无 npm 构建」，
  唯一构建是 `build_hubjs.sh` 的**纯拼接**。
- Reflex 的代价：Node/Bun 工具链、`.web` 产物、第二个前端进程与 WS 代理、每次部署多一步构建。
- 本仓已有教训：新增常驻服务必须进健康探针（AGENTS §2「升级与高危操作铁律」第 8 条）；
  托管路径不得写死版本号。这两条都会在引入 Node 时被重新触发一次。

### 4.3 重写的体量与闸门作废面

- 21 页 / 587 个顶层函数 / 62 个 API 调用点要落成 `rx.*` 组件树。
- `tests/*.py` 351 个里 **36 个**依赖 `static/hub.js` 单文件产物；其中 12 个经
  `tests/_hub_extract.py:14` 从产物「原样抽真代码」（该文件 `:9/:26` 还依赖「顶层函数第 0 列
  `}` 收尾」；分层口径见 §6），6 个 CDP 探针把 `/static/hub.js` 写死在 `urlPattern` 里。Reflex 没有这个产物
  ⇒ 这批闸门是**被测对象消失**，不是断言变红，而是根本没东西可测。这类闸门恰恰是过去
  「服务端全绿但用户仍坏」的主要拦截手段（判例 AGENTS §4.3）。
- 收益侧：只有「用 Python 写界面」这一条属于本机真实需求（本仓维护者是 Python 侧，
  前端改动频率高），但它不产生用户可见的性能收益。

## 5. 什么情况下 Reflex 才值得

仅当同时满足三条时再评估：① 新增的是**纯数据/表单型**页面（无终端、无高频流）；
② 该页面允许独立部署（独立端口、独立进程），不污染 :3102 的单进程形态；
③ 该页面不依赖现有 36 个产物闸门。当前 21 页无一满足前两条的性价比门槛。

> 建议保留的观察项：若将来要做「对外展示型看板」（只读、低频、可独立部署），
> Reflex 是候选之一，但届时应与「FastAPI + 服务端渲染模板」「Astro 静态导出」同台比较。

## 6. 执行计划（零构建路线，四批）

施工纪律（每批都必须遵守）：目标文件当天若已被他会话写过 ⇒ 停手报请（AGENTS §2 单写者）；
施工进 worktree（`scripts/worktree-new.sh`）；重启只走 `scripts/hub-restart.sh --reason`
（`term_sessions > 0` 时禁 restart）；每批结束跑 L0 全量 + 36 个产物相关闸门 + 生产同款复验。

### P0 缓存与卫生（无需重启，1 天内可完成，可随时回滚）

| # | 动作 | 改动文件 | 验收判据 |
|---|---|---|---|
| T1 | 静态资源改长期缓存：`/static/**` 加 `cache-control: public, max-age=31536000, immutable`，URL 统一带构建 token（hub.js 已有 `?v=`，扩到 `vendor/*`、`hub.css`） | `src/main.py`（静态挂载/中间件）、`templates/index.html` 引用处、`static/vendor/README.md` | 二次加载：`curl -D-` 出现 immutable；DevTools 里静态请求命中 disk cache（不产生 304）；换 token 后立即取新文件 |
| T2 | 148 个 `.bak`（29MB）归档出仓 | 用现成 `scripts/bak_archive.sh --apply` | `git ls-files \| grep -c '\.bak-'` == 0；`du -sh templates static` 下降 ≥25MB |

风险与回滚：T1 唯一风险是「token 没换但文件变了 ⇒ 用户拿旧缓存」。缓解：token 由
`build_hubjs.sh` 写入（已有 `x-asset-token` 机制），且 T1 只对**带 token 的 URL** 开 immutable，
裸路径保持 no-cache。回滚 = 去掉中间件分支。

### P1 首屏减重（3~5 天，需重启一次）

| # | 动作 | 改动文件 | 验收判据 |
|---|---|---|---|
| T3 | 把 113,566 B 内联 CSS 外提为 `static/hub.css`（保持无构建、单文件），模板改 `<link>` | `templates/index.html`、新建 `static/hub.css`、`tests/test_narrow_first_paint.py`（断点单源）、token 断言 | 首屏 HTML 传输 78KB → ≤45KB；`test_narrow_first_paint` 14 例绿；320/768/1920 三档截图与改前像素级一致 |
| T4 | 分页懒渲染：先只做 3 个重页（agents / tasks / kb），首次进入才填充，其余保持原样（灰度） | `static/hub/02-nav-and-poll.js`（`go()`，`01-core-boot.js:662`）、对应三个分片 | 首屏 DOM 节点数下降 ≥30%（`document.querySelectorAll('*').length`）；切页耗时 p95 不超过改前 +50ms；断网/慢网下不回退白屏 |

风险与回滚：T4 会动导航唯一入口 `go()`，须先补一条「导航后浮层为空」的断言（AGENTS §4.2②③）
再加懒渲染；回滚 = `go()` 恢复同步填充分支（保留开关常量，一周后删）。

### P2 模块化 + 代码分割（2 周+，收益最大、风险最高；已裁定，见 §8）

| # | 动作 | 改动文件 | 验收判据 |
|---|---|---|---|
| T5 | 15 片改原生 ESM（`import`/`export`，无打包器），首屏只加载 core-boot + nav，其余 `import()` 按页拉；**`/static/hub.js` 单文件产物 URL 作为长期兼容契约保留**（内部改为 legacy 聚合入口）；**注入点从 `loadHub()` 改为 module 入口 `entry.js`，`?legacy=1` 才走 hub.js** | 全部 `static/hub/*.js`、`templates/index.html`（module + legacy 双入口；含 `loadHub()` `:3740-3745` 与 3 类运行时探针）、`scripts/build_hubjs.sh`、`tests/test_hubjs_split.py`、`tests/_hub_extract.py`、6 个 CDP 探针的 `urlPattern` | 首屏 JS 传输 214KB → ≤80KB；`test_hubjs_split.py` 由「逐字节相等」改「模块图 + 导出面相等」后 5 断言全绿；**36 个产物相关测试文件 `unittest discover` 全绿且 0 个因产物消失而失败**；`_hub_extract.py` 能抽出 `export function`；6 个 CDP 探针仍能拦到请求；3 类模板探针同步后诊断面板不误报「hub.js 加载失败」 |
| T6 | 15 个 `setInterval` 合并为单一调度器（visibility 感知 + 指数退避 + 同域请求合并） | `01-core-boot.js`、`03/04/06/12/13/16` | 后台标签页 5 分钟内请求数 = 0；前台轮询间隔与改前一致（逐点比对）；`/health` 无新增 5xx |

#### T5 闸门盘点（36 / 12 / 6，2026-11-12 实测）

统计口径：`tests/*.py` 共 351 个；逐文件匹配 `static/hub.js|HUB_JS|/static/hub.js` 得 **36** 个
直接依赖产物；引用 `tests/_hub_extract.py` 抽真代码的 **12** 个（其中 11 个也在 36 内，
1 个只 import helper、不写产物路径）；把 `/static/hub.js` 写死在 `urlPattern` 的 **6** 个 CDP 探针
（全部落在 36 内，与 12 零重叠）。36 减去两组并集（17）余 **19** 个纯读文本文件——其中
`_cdp_min.py`、`_hub_extract.py` 是 helper，`test_hubjs_split.py` 是下方必改对象，
**其余 16 个零改动**。另 27 个只提 `.bak`/文案，不受影响；`e2e-probe/**/*.ts` 为 0。
非测试引用点 3 处：`scripts/build_hubjs.sh` 9 处、`templates/index.html` 12 处、
`tests/_hub_extract.py:14` 1 处。

**36 个里 19 个只是读同一份 `static/hub.js` 文本做静态断言** ⇒ 只要产物 URL 还在，
除 2 个必改对象外的 16 个全部照常绿。真正必须动的只有三处：

1. `tests/test_hubjs_split.py`（2 类 / 5 断言）：逐字节相等 → 模块图 + 导出面相等；
   同文件 `TestLadder`（`:26-71`）读产物断言顶层 `function`，一并改。
2. `tests/_hub_extract.py`：`:14` 硬编码产物路径（保留 URL 即不用改），但 `:9/:26` 的
   「顶层函数第 0 列 `}` 收尾」抽取在 `export function` 下失效 ⇒ 抽取器要认识 `export` 前缀。
3. 6 个 CDP `urlPattern`：URL 不变则 0 改动；一旦改成多模块 URL，6 个全要改。

结论：**T5 的「双入口」不是一周过渡，应是长期兼容契约**；保留产物可把作废面从 36 压到
1 个测试文件（`test_hubjs_split.py`）+ 1 个抽取器（`_hub_extract.py`）+ 0~6 个探针。

#### 裁决落地（2026-11-12，用户「按此执行」）

**裁决 1 · 模块图校验 → 接受，但双轨并存、不是替换。**

- 两者防不同故障：逐字节守「产物没漂移」，模块图守「依赖契约没漂移」；模块图**抓不到**
  「某行误删一个字符」⇒ 逐字节判定不得删除。
- 精确落法（5 断言里 2 保留 / 1 新增层 / 2 重写）：
  ① `test_concat_equals_served_file` **保留**，挂在 legacy 聚合产物上（仍由 `build_hubjs.sh` 生成）；
  ② **新增**模块图层：模块清单 / 依赖边 / 导出符号名与数量 / 入口可解析全图；
  ③ `TestLadder` 2 断言**重写**：函数变 `export function`、单文件行号轴消失 ⇒ 改按模块
  「导出符号存在 + 首尾覆盖」；
  ④ `test_parts_exist`、`test_no_part_is_empty_or_reordered` 改成模块文件存在 + 命名规范，仍有效。

**裁决 2 · 长期契约 → 确认，但只保「文件 + URL」，不保「首屏加载」。**

- 实测 `templates/index.html:3740-3745`：`loadHub()` 现在**正是**把 `/static/hub.js` 动态注入首屏
  （`s.src = HUB_SRC` ⇒ `document.body.appendChild(s)`）⇒ 保留文件 ≠ 保留首屏，这一点原稿未核。
- 落法：T5 把注入点换成 module 入口 `entry.js`（首屏只 core-boot + nav，其余按页 `import()`），
  `?legacy=1` 才走 `hub.js`；legacy 产物继续完整生成，供 36 个闸门 / `_hub_extract.py` /
  6 个 CDP 探针读 ⇒ 与「首屏 214KB→80KB」不矛盾。

**新发现的第 4 类链路点（36/12/6 静态盘点未覆盖）**：模板 12 处引用里有 3 类**运行时探针**，
注入点一改就全要跟着改 —— ① `:3613` 查 `script[src*="hub.js"]`；② 诊断面板「资源加载失败
hub.js」文案；③ `s.onerror` 兜底（`:3744`）。故上文「16 个零改动」须收窄为
「16 个**测试文件**零改动，模板探针另计」。

#### 开工前置实测（2026-11-12）

| 判据 | 实测 | 处置 |
|---|---|---|
| `scripts/orchestration-check.sh`（工作区级，非仓内） | **EXIT=1**，C10 FAIL=1：主 checkout 有 1 条未提交改动（即本评估文档） | 先清场再建 worktree；C3/C4/C5/C7/C8 全 PASS，无跨会话同文件写 |
| 首次跑错路径 `agenthub/scripts/orchestration-check.sh` | EXIT=127（文件不存在） | 探针失败 ≠ 无此闸门，已用正确路径重跑并如实记录 |
| `GET /health` | `term_sessions=4`、`code_stale=False` | **严禁自动重启**；上线须 `hub-restart.sh`，强承担需显式 `HUB_RESTART_FORCE=1` |
| 基线 `python3 -m unittest tests.test_hubjs_split -v` | 5/5 OK（0.176s） | §9 要求的开工前基线已留证 |
| `git status --short` | 仅 `?? docs/REFLEX-FRONTEND-EVAL-20261112.md` | 清场处置见下 |

> **本轮施工边界**：裁决定稿 + 前置清场 + worktree 隔离区就绪；**不改 15 个生产分片、不重启**。
> 原因：`term_sessions=4` 下无法做生产复验，且 P2 需分批逐点回归（L0 全量 + 36 闸门 + L1）。
> 第一刀从 worktree 内的 `tests/test_hubjs_split.py` 双轨改造开始，改完逐点验证再铺开分片。

风险与回滚：ESM 化会同时动 15 个文件与模板加载方式 ⇒ 必须串行一人完成，且在 worktree 里做完再合。
回滚开关：index.html 保留 `?legacy=1` 走旧 `hub.js`（长期保留，不删）；
唯一新增风险是 `_hub_extract.py` 抽不到 `export function` ⇒ 验收判据里已单列该条。

### P3 长效防腐（可选，各 0.5~1 天）

| # | 动作 | 验收判据 |
|---|---|---|
| T7 | 前端性能预算进 `scripts/prepush.sh`：产物字节上限、首屏请求数上限、`.bak` 计数、`inline onclick` 增量不得为正 | 故意超预算时闸门退出非 0；正常提交无感（≤2s） |
| T8 | 重复 UI（卡片/表格/浮层）抽成原生 Web Components（无构建） | 抽出后至少 3 处调用点复用；浮层唯一性断言（AGENTS §4.2①）仍绿 |

### 批次依赖与顺序

```
P0(T1,T2) ──► P1(T3,T4) ──► P2(T5,T6) ──► P3(T7,T8)
               │              │
               └ 每批都要重启 ┴ 每批都要 L0+36 产物闸门+生产同款复验
```

P2 之前不动测试契约；P2 之后 T7 才有意义（否则预算无从设定）。

## 7. 与「Reflex 整体替换」的成本对照

| 维度 | 保持零构建 + 渐进重构（§6） | Reflex 整体替换 |
|---|---|---|
| 需要重写的代码 | 0 行业务逻辑（只做结构迁移） | 21 页 + 587 函数 |
| 新增运行时 | 无 | Node/Bun 工具链 + 前端进程/代理 |
| 终端（31% JS） | 原样保留 | 只能原样包成不透明组件 |
| 现有 36 个产物闸门 | 保留（T5 只改 1 测试文件 + 1 抽取器） | 被测对象消失，36 个集体失去意义 |
| 用户可见收益 | 首屏 78KB→45KB HTML、214KB→80KB JS、二次加载 0 请求 | 取决于重写质量，无结构性收益 |
| 可回滚性 | 每批一个开关 | 不可分批，只能整体回滚 |

## 8. 待裁定

1. ✅ **已裁定（2026-11-12 用户「按此执行」）**：① 模块图校验 = 双轨并存（逐字节保留挂 legacy
   产物 + 新增模块图层 + `TestLadder` 重写）；② `/static/hub.js` 作长期兼容契约，但只保文件/URL、
   注入点改 `entry.js`、`?legacy=1` 走 legacy。裁决依据与第 4 类模板探针见 §6。
   **仍未授权**：生产重启（`term_sessions=4`，须显式 `HUB_RESTART_FORCE=1`）。
2. ⏸ **是否要 Reflex 的独立试点**（只读看板、独立端口、不碰 :3102）？我的建议：不做。
3. ⏸ **优先级**：先修性能（T1→T3→T5）还是先修可维护性（T2→T4→T6）？

## 9. 未取证 / 未做（如实声明，避免被当成结论）

- **参考库未查**：`intake` 判 ready 且未触发 deep，未消耗 GitHub 额度；若要对标同类前端，
  可单独跑 `intake.sh ... --deep`（20~90s）。
- **Reflex 的 Node 版本号未实机取证**：`raw.githubusercontent.com` 本机 curl 连接被 reset
  （`curl: (35) Recv failure: Connection reset by peer`，后台跑了 3m57s 后失败；WebFetch 通、curl 不通），
  故正文只写「需要 Node/Bun 构建」，未写具体版本。
- **未跑 Lighthouse / 未做真机端侧测量**：本文件所有体积数字来自服务端 `curl`，
  **渲染耗时、LCP、移动端解析成本均未测**。T3/T4 的验收判据里要求补端侧实测，
  这条不可省——按本仓判例，服务端全绿不能证明前端正常（AGENTS §4.3）。
- **36/12/6 是静态盘点，未逐个跑过**：只统计了「哪些文件引用产物」，未验证「保留产物后
  36 个是否真的全绿」。开工前最小基线已跑（`tests.test_hubjs_split` 5/5 OK），
  但**完整 `unittest discover` 基线仍未跑**，须在动分片前补跑并留证。
- **未评估替代框架**（Astro / htmx / Alpine / Svelte）：本文件只回答「Reflex 是否合适」。
  若后续要选型，应单独立项并同台比较。
