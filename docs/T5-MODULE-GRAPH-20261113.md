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

## 施工顺序（每步可独立验收）

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
