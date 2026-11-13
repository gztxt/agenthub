# T5 模块化施工蓝图（2026-11-13）

> 实测口径：`static/hub/*.js` 15 片 / 588 个顶层函数 / **279 条跨分片引用**。
> 扫描方式：逐文件正则 `^function NAME(` + 跨文件词边界匹配（含注释，数字偏保守）。

## 为什么不能「先试点一片」

15 片共享同一个全局作用域（纯拼接进一个 `<script>`）。实测 **148 个函数被其它分片引用**，
最强的是 `api`（13 片）、`go`（12 片）、`escapeHtml`（11 片）、`lsGet`（10 片）、`toast`（9 片）。
把任何一片改成 ESM，它的函数就不再挂到 `window`，引用它的 10+ 片立刻 `ReferenceError`
⇒ **T5 是「全有或全无」，没有增量试点路径**。

## 依赖矩阵

| 分片 | 导出(顶层函数) | 依赖分片数 | 依赖的跨片符号数 |
|---|---|---|---|
| 01-core-boot.js | 127 | 12 | 38 |
| 02-nav-and-poll.js | 36 | 4 | 30 |
| 03-agents-cards.js | 73 | 5 | 33 |
| 04-terminal-ws.js | 181 | 5 | 39 |
| 05-chat-and-history.js | 50 | 6 | 26 |
| 06-manager-tasks.js | 13 | 6 | 36 |
| 07-asset-panel.js | 13 | 1 | 5 |
| 09-local-projects.js | 20 | 4 | 13 |
| 10-github-projects.js | 24 | 4 | 15 |
| 11-resources.js | 9 | 3 | 7 |
| 12-activity.js | 4 | 3 | 9 |
| 13-term-touch.js | 16 | 4 | 11 |
| 14-botboy.js | 8 | 2 | 5 |
| 15-workbench.js | 5 | 3 | 8 |
| 16-events-panel.js | 9 | 3 | 4 |

## 核心层（被 ≥5 片引用，共 12 个）

`api`、`go`、`escapeHtml`、`lsGet`、`toast`、`lsSet`、`ico`、`loadAgents`、`gotoChat`、
`termHeaders`、`termConnect`、`initSidebar`

## 路线修正（2026-11-13 实测）

ESM 化**不是必要条件**。实测 `01-core-boot.js` 顶层**零跨片调用**（0 命中），38 个跨片符号引用
全部在函数体内（切页/事件触发才执行）⇒ **按片延迟加载即可达成体积目标**，且不必改 15 片的作用域语义
（ESM 化会让顶层 `function` 不再挂 `window`，`onclick=` 与 587 个全局符号都要桥接，风险面远大于收益）。

**首屏必需 vs 可延迟**（依据：`01` 的 38 符号依赖矩阵 + `go()` 调用点）：

- 必需：`01`（自身）、`05`（`go()` 每页调 `renderNav` / `navItemHtml`）、
  `02`（`renderChatSide` / `renderModeBar`）、`03`（classroom 卡片与登录模态）
- 可延迟：`04`（13 个符号，全部切页才用）、`06`、`07`、`09`、`10`、`11`、`12`、`14`、`15`

**预期收益**：首屏 4 片 ≈ 344 KB 原始 / ≈100 KB br（现 723 KB / 214 KB）⇒ **-53%**。

**代价（为什么仍需专项 2 周）**：模板注入方式改 + 9 个片的调用点改异步（`ensureShard().then()`）
+ 约 40 条闸门改数据源（与 T3 同量级：T3 只外提 CSS 就产生 40 条红）。
任何单点增量都会撞「逐字节 + 36 个产物闸门」，所以仍是**全有或全无**，只是实现方式从 ESM 换成延迟加载。

**修正后的 4 步**：
1. `static/hub/00-loader.js`：`ensureShard(name)` + 首屏 4 片按序注入（经典脚本，非 module）
2. 模板 `loadHub()` 改注入 loader；`?legacy=1` 仍走 `static/hub.js`（长期契约）
3. 9 个延迟片的调用点改 `ensureShard(...).then(fn)`，并保留 `typeof fn === 'function'` 兜底
4. **逐片验证 + 闸门数据源同步**，五类判据缺一不可：
   - **加载链路**：首屏 4 片按序各 200；`typeof window.go/renderNav/renderChatSide === 'function'`；
     `?diag=1` 的符号阶梯 30 项全为 1（现成探针复用，不新写）。
   - **延迟触发**：进 assets/kb/tasks 页时 Network 只新增 **1 个** shard 请求；二次进页 **0 新增**
     （promise 缓存）；404/网络失败不白屏（`typeof fn === 'function'` 兜底 + toast）。
   - **功能等价**：9 个延迟页各一条行为断言（assets 列表非空 / kb 表有行 / tasks 有运行记录 /
     jobs 有计数 / telemetry 有事件 / 本地项目与 GitHub 各非空 / workbench 有任务 / botboy 有投影）。
   - **闸门同步**：约 40 条读模板或产物的断言改数据源后全绿；`test_hubjs_split.py` 逐字节轨
     继续守 legacy `hub.js`；`test_frontend_budget.py` 的 bundle 上限按新首屏下调（否则预算失效）。
   - **回退**：`?legacy=1` 走 `hub.js`，抽样 3 页对比 DOM 节点数与改造前一致（长期契约的验收点）。

## 施工顺序（原 ESM 方案，已被上节取代，保留归档）

1. **收敛核心层**：新建 `static/hub/00-core-api.js`（ESM），把 12 个符号及其私有依赖搬进去并 `export`。
   ⚠ 此步必须**同时**切换加载方式为 `<script type="module">` —— `import` 在经典脚本里是语法错，
   所以「先搬代码、后换加载」不可行。
2. **建入口**：`static/hub/entry.js` 依次 `import` 各片；模板 `loadHub()` 改注入 `entry.js`；
   `/static/hub.js` 保留为 legacy 聚合产物（长期契约，双轨闸门的逐字节轨道继续守它）。
3. **按依赖逆序逐片改**（叶子先：07 → 14 → 16 → 11 → 12 → 15 → 09 → 10 → 13 → 05 → 06 → 02 → 03 → 04 → 01），
   每片改完跑一次 L0 + 页面探针；依赖矩阵就是拆分顺序的唯一依据。
4. **代码分割**：非首屏页（07/09/10/11/12/14/15/16）改 `import()` 按页拉，首屏只留 00/01/02。

## 与既有闸门的关系（已就绪）

- `tests/test_hubjs_split.py`：双轨已在（逐字节挂 legacy 产物 + 模块图/导出面）；第 1 步起模块图层真正生效
- `tests/_hub_extract.py`：已支持 `export function` 前缀
- 6 个 CDP `urlPattern`：URL 不变 ⇒ 零改动
- 模板 3 类运行时探针：注入点改 `entry.js` 后必须同步（`:3613` 的 `script[src*="hub.js"]`、
  诊断面板「资源加载失败 hub.js」文案、`s.onerror` 兜底）

## 未做（T5 本体）

本轮只完成**前置**：双轨闸门 + 本蓝图。分片 ESM 化未开工——按上表工作量与「全有或全无」约束，
需专项排期，不可与其他批次并行（文件级单写者）。
