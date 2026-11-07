# Agent Hub（智管）设计方案

## 架构概览

```
┌─────────────────────────────────────────────────────┐
│            Agent Hub（智管）                         │
│         Python FastAPI + 原生 JS（无构建）          │
│          端口 :3102 (:: 双栈，见下「监听口径»)        │
├─────────────────────────────────────────────────────┤
│  Dashboard    │  Agent List  │  Chat  │  Memory  │
│  (教室视图)   │  (管理面板)   │ (统一对话)│ (记忆中心) │
└───────────┬───────────────────┬────────────────────┘
            │                   │
     ┌──────▼──────┐    ┌──────▼──────┐
     │  Agent Adapters   │  Memory Sync    │
     │  (适配器层)       │  (同步层)       │
     └───┬─────┬─────┬───┘    └──────┬──────┘
         │     │     │               │
    ┌────▼──┐ ┌▼────▼──┐ ┌─────────▼───┐
    │Claude │ │  pi    │ │   jcode     │
    │(CCR:3 │ │(:30141)│ │  (:3457)    │
    │ 456)  │ │        │ │             │
    └───────┘ └────────┘ └─────────────┘
              │
         ┌────▼────┐
         │  TDAI   │
         │(:8420)  │
         │(记忆中心)│
         └─────────┘
```

### 监听口径（2026-09-30 订正，AGENTS.md §5）

`DESIGN.md` 此处原写「端口 :3102 (0.0.0.0)」，**与本机军规矛盾**：启动 HTTP 服务必须绑
`::`（双栈 IPv6），绑 `0.0.0.0` 是 IPv4 only，外网打不开。

- 真实配置：`.env` 的 `HOST=::`；`run_dualstack.py` 作为 `ExecStart` 兜底
  （旧 unit 把 .env 的 HOST 完全盖住，改了 .env 也不生效）
- `src/config.py` 的默认值仍是 `os.getenv("HOST", "0.0.0.0")` —— 那只是**缺省值**，
  生产以 `.env`／`run_dualstack.py` 为准；影子实例等隔离场景显式传 `HOST=127.0.0.1`
- 实测：`ss -tlnp | grep 3102` → `LISTEN *:3102`（双栈通吃）

## 核心模块

### 1. Agent Discovery（自动发现）
- 扫描已知 Agent 配置（~/.claude, ~/.pi, ~/.jcode）
- 检测端口占用（:3456, :30141, :3457, :8420）
- 自动生成 Agent Registry（SQLite）

### 2. Agent Adapters（适配器）
| Agent | 协议 | 认证 | 端点 |
|-------|------|------|------|
| Claude | CCR OpenAI 兼容 | x-ccr-core-auth | :3456/v1/chat/completions |
| pi | HTTP REST | Bearer Token | :30141/api/* |
| jcode | OpenAI 兼容 | x-ccr-core-auth | :3457/v1/chat/completions |
| TDAI | REST | 无 | :8420/* |

### 3. Unified Chat（统一对话）
- 单 Agent 对话：转发到对应 Adapter
- 多 Agent 串流：Manager Agent 调度
- 会话历史：统一存储在 SQLite

### 4. Memory Center（记忆中心）
> ⚠️ **旧口径（L1 可检索 / L2 近 30 天 / L3 长期 Profile）已作废（2026-11-07，v0.13.192）**，
> 以 M1/M2/M3 为准（README 有对照表）：M1 最近 / M2 库 / M3 画像。旧句保留不删。
- L1：可检索记忆（语义搜索）
- L2：近 30 天工作记忆
- L3：长期 Profile
- 同步到 TDAI（如有）

### 5. Dashboard（教室视图）
- 所有 Agent 状态可视化
- 一键启动/停止（CLI Agent）
- 内嵌 Web UI（pi/TDAI）
- 实时日志

## 技术栈

| 层 | 技术 | 版本 |
|----|------|------|
| 后端 | Python FastAPI | 0.104+ |
| 前端 | 原生 JS，**无框架无 npm 构建**；真源 `static/hub/*.js` 12 分片 → `scripts/build_hubjs.sh` 拼成产物 `static/hub.js`；终端用本地 vendor xterm **6.0.0** + 6 addon（离线，无 CDN；canvas addon 已随 6.0 移除，取证见 `static/vendor/README.md`）| - |
| 数据库 | SQLite | 内置 |
| 异步 | asyncio + httpx | - |
| 部署 | 直接运行 | :3102 |

## 目录结构

```
~/agent-hub/
├── src/                          # 119 个平铺 .py（2026-11-07 实测 48875 行；旧值「43 个」已过期，仅 adapters/catalog 两子包）
│   ├── main.py                   # FastAPI 入口（VERSION 单一版本源 + 路由装配）
│   ├── config.py                 # 配置管理（读 .env，override=True）
│   ├── discovery.py              # Agent 自动发现
│   ├── registry.py               # Agent 注册表（SQLite）
│   ├── adapters/                 # 唯一的子包：Agent 协议适配器
│   │   ├── base.py               # 抽象基类
│   │   ├── openai_compat.py      # claude/jcode → CCR:3456（OpenAI 兼容口径）
│   │   └── ...                   # pi / TDAI 等
│   ├── term.py                   # 嵌入式终端：PTY、WS、TTL 回收、尺寸所有权
│   ├── memory.py / memfed.py / memstats.py / kb.py   # 记忆三层 + 联邦检索
│   ├── cronjobs.py               # 定时任务（**注意：不是 scheduler.py**）
│   ├── llm.py / manager.py       # Manager Agent（自然语言指挥官，FCC Anthropic 工具环）
│   ├── hublog.py / runlog.py     # 日志中心 + 运行日志
│   ├── sessions_store.py         # 外部 CLI 会话仓库**只读**适配层（不写不删）
│   ├── sessions_export.py        # 会话导出（默认脱敏）
│   ├── resources.py / ports.py   # 资源监控（可 kill）/ 端口枚举（**只读无 kill**）
│   ├── vitals.py / healthx.py / selfattest.py  # 健康自证
│   ├── writeauth.py              # 写端点统一鉴权（fail-closed）
│   └── ...                       # 其余平铺模块
├── static/
│   ├── hub.js                    # **构建产物**：由 static/hub/*.js 拼出，改分片勿手改
│   ├── hub/                      # 前端分片真源（12 片，无 08；清单由 tests/test_ls_guard.py 闸门锁）
│   │   ├── 01-core-boot.js       # 启动、api()、loadAgents、Esc 出口、agentHealth 单一真源
│   │   ├── 02-nav-and-poll.js    # 侧栏渲染与轮询
│   │   ├── 03-agents-cards.js    # Agent 卡片（终端渲染器分支在此，改 xterm 必看）
│   │   ├── 04-terminal-ws.js     # 终端 WS 客户端（尺寸 intent claim/update）
│   │   ├── 05-chat-and-history.js# 对话与历史会话（含侧栏忙碌点挂载）
│   │   ├── 06-manager-tasks.js   # Manager 对话、任务、全局 Esc 优先级出口
│   │   ├── 07-asset-panel.js     # 设置抽屉（模型/GitHub/日志）
│   │   ├── 09-local-projects.js  # 本机项目
│   │   ├── 10-github-projects.js # GitHub 项目
│   │   ├── 11-resources.js       # 资源监控页（懒加载）
│   │   └── 12-activity.js        # 跨 Agent 活动指示（8s 独立轮询）
│   └── vendor/                   # xterm 6.0.0 + 6 addon（离线，见 vendor/README.md）
├── templates/index.html          # 唯一模板：结构 + :root 设计 token（视觉权威源）
├── tests/                        # L0 hermetic（880 例）+ L1 host + verify_* 真渲染探针
├── data/                         # 运行时数据（agents.db / logs / term 录放），不入 git
├── scripts/                      # build_hubjs.sh（分片→hub.js）、run_tests.sh、ctl
├── run_dualstack.py              # 双栈监听兜底（ExecStart）
└── .env                          # 环境变量（0600，凭据真源）
```

> **订正记录（2026-09-30）**：旧版目录树列的 `chat.py` / `scheduler.py` **都不存在**
> （统一对话在 `src/manager.py` + `src/llm.py`，定时任务是 `src/cronjobs.py`），
> 且漏掉了 `src/term.py`（终端是本仓最大子系统）、`static/hub/` 分片化与 `static/vendor/`。
> 前端「单文件 `static/hub.js`」的说法同样过期：`hub.js` 现在是**构建产物**，
> 真源是 `static/hub/*.js` 分片（AGENTS.md 军规：主 checkout 只由集成者重建 build 产物）。

## API 设计

> 2026-09-30 按真实路由重录（旧表只有 9 条且含已不存在的 `/api/docs` 与不存在的
> `POST /api/tasks`）。实际规模：**95 个 HTTP 端点 + 1 个 WebSocket**（`grep -oE
> '@(app|router)\.(get|post|put|patch|delete|websocket)\('` 全量取证）。下表按
> 子系统分组，只列**主干**；分组标题即 `src/` 里的模块名，便于对照源码。
> 鉴权口径（2026-11-07 C 档后）：写端点统一走 `src/writeauth.py::decide`
> （只认登录会话 Cookie / HTTP Basic；fail-closed：用户库不可用 ⇒ 503 而不是放行）。
> 共享 token（TERM_TOKEN / HUB_PASSCODE）已废弃；非回环读端点须登录。

**核心（`main.py`）**

```
GET    /health                              # 健康自证：version / code_matches_head / vitals / term 状态
GET    /api/agents                          # 列出所有 Agent（含 vitals 实测 verdict）
POST   /api/agents                          # 注册自定义 Agent（写）
DELETE /api/agents/{agent_id}               # 注销（写）
GET    /api/agents/{agent_id}               # 详情
POST   /api/agents/detect                   # 重新探测（写）
POST   /api/agents/{agent_id}/verify        # 实弹验证（写）
POST   /api/agents/{agent_id}/chat          # 统一对话（非流式）
POST   /api/agents/{agent_id}/chat/stream   # 统一对话（SSE 流式）
GET    /api/vitals          POST /api/vitals/sweep   # 健康实测结论（单一异常判据来源）
GET    /api/models          POST /api/settings/model/{preview,apply}   # 模型设置
```

**终端（`term.py`）——本仓最大子系统**

```
WS     /ws/term/{sid}                       # PTY 双向流（5ms 前后沿 coalescer）
GET    /api/term/sessions                   # 会话列表（前端活动指示的唯一数据源，只认 s.alive）
POST   /api/term/sessions                   # 开会话（写）
DELETE /api/term/sessions/{sid}             # 关会话（写）
GET    /api/term/history/{agent_id}         # 该 Agent 的历史会话
```

**会话与导出（`sessions_store.py` / `sessions_export.py`）**

```
GET    /api/sessions                        # hub 自身会话 + 外部 CLI 只读聚合
GET    /api/sessions/{session_id}/messages
PATCH  /api/sessions/{session_id}           # 改标题（写）
DELETE /api/sessions/{session_id}           # 删（写）
GET    /api/sessions/export                 # 批量导出：**按写端点鉴权**（导出=数据外流），
                                            # 默认 redact=1 脱敏，命中数在 meta 如实回报
```

**记忆（`memory.py` / `memfed.py` / `memstats.py` / `kb.py`）**

```
GET    /api/memory            /api/memory/{l1,l2,l3}      # 三层记忆
GET    /api/memory/search     /api/memory/context         # 检索与注入
POST   /api/memory/l1         /api/memory/l1/batch        # 写入（写）
DELETE /api/memory/l1/{mid}                                 # 删（写）
PUT    /api/memory/l2         /api/memory/l3              # 改（写）
POST   /api/memory/l2/rebuild                              # 重建压缩（写）
GET    /api/memory/fedsources                              # 联邦检索源清单
```

**资源与端口（`resources.py` / `ports.py`）—— kill 口径不同，别混**

```
GET    /api/resources                        # 运行中 Agent 进程 CPU/内存
POST   /api/resources/kill                   # 结束 Agent 进程（写）：校 /proc/<pid>/cmdline 归属，
                                            # 目标已不存在视为成功，前端二次确认
GET    /api/ports   /api/ports/{port}        # 端口枚举：**只读无 kill**（NAS 服务归 systemd）
```

**任务与调度（`tasks.py` / `cronjobs.py`）**

```
GET    /api/tasks/runs   /api/tasks/{run_id}
POST   /api/tasks/decompose                   # 拆解（写）
POST   /api/tasks/{run_id}/{start,retry}      # 启/重试（写）
DELETE /api/tasks/{run_id}                    # 删（写）
GET/POST/PUT/DELETE /api/jobs[/{jid}[/run]]   # 定时任务（cronjobs.py）
POST   /api/scan/run                          # 扫描（写）
```

**项目与资产（`localprojects.py` / `githubprojects.py` / `ghsettings.py`）**

```
GET    /api/localprojects                    GET  /api/github/repos  /api/github/sync
POST   /api/github/clone                      # 写
GET/POST /api/settings/github/{test,refresh,apply,clear}   # 写
GET    /api/prefs/{key}   PUT /api/prefs/{key}              # 偏好：读/写
```

**日志与审计（`hublog.py` / `runlog.py` / `audit.py`）**

```
GET    /api/hublog   /api/runlog   /api/audit/list
```

**MCP 网关（`mcpgw.py` / `hubmcp.py`）**

```
GET    /mcp/{servers,tools,acl,registry}      POST /mcp/{call,acl,servers,servers/probe}
DELETE /mcp/{acl/{acl_id},servers/{sid}}
```

**遥测（`hook.py`，Bearer 鉴权，补上游零鉴权的短板）**

```
GET    /telemetry/events   /telemetry/usage/summary
POST   /telemetry/events/{source}
```

## 实施计划

### Phase 1（核心骨架）- 2小时
- [ ] 项目结构搭建
- [ ] Agent Discovery 模块
- [ ] SQLite Registry
- [ ] 基础 API（/api/agents, /health）
- [ ] 简单 Web UI（HTML + JS）

### Phase 2（适配器层）- 3小时
- [ ] Claude/CCR Adapter
- [ ] pi Adapter
- [ ] jcode Adapter
- [ ] 统一对话接口

### Phase 3（记忆中心）- 2小时
- [ ] 本地记忆存储
- [ ] TDAI 同步
- [ ] 语义搜索（可选）

### Phase 4（Dashboard）- 2小时
- [ ] 教室视图 UI
- [ ] 实时状态刷新
- [ ] 日志查看器

总计：约 9 小时

## 视觉系统（2026-09-26 补：hallmark 审计 M1）

> 本节补齐一个真实缺口：此前 `DESIGN.md` 只有架构与实施计划，**零视觉约束**，
> 而真正的设计系统住在 `templates/index.html` 的 CSS 注释里（v0.9 配色、v0.10.1 说明、
> pi-web 内嵌条尺寸契约）。后果是任何后续 agent 读本文档都学不到视觉规则，改页面必然漂。
> **权威源仍是 `templates/index.html` 的 `:root` 块**，本节只做语义索引，不复制取值
> （复制即制造第二份会漂的副本）。

### 一条总原则

**UI 骨架一律中性灰阶，色相只发给语义。** 层级靠「底色差 + 描边 + 字重 + 留白」表达，
不靠颜色装饰。允许出现色相的只有四个语义位：`--ok`（成功）/ `--warn`（告警）/
`--danger`（危险）/ `--busy`（进行中，≠ 健康绿）。

### token 角色表（取值以 `:root` 为准，此处只定义"谁负责什么"）

| 角色 | token | 用途约束 |
|---|---|---|
| 画布 | `--canvas` | 页面底，承托内容框 |
| 内容面 | `--bg` / `--surface` / `--surface-2` / `--field` | 卡片、输入、表格、代码块、框内次级块 |
| 描边 | `--border` / `--divider` / `--line` | 框体分隔；**内嵌条专用** `--embed-line` / `--embed-bg`（与 divider/hover 是不同角色，禁止合并复用） |
| 文字三级 | `--text-1` / `--text-2` / `--muted` | 全部须达 WCAG AA 正文 4.5:1 以上 |
| 实心块字色 | `--on-accent` | 主按钮 / 用户气泡 / 徽标上的字；**禁止写字面 `#fff`** |
| 选中·聚焦 | `--sel-*` / `--focus-*` | 一律半透明，不用实黑反色块 |
| 字体 | `--font-sans`（正文）/ `--font-mono`（数据）/ `--font-display`（＝mono，展示位） | CJK-first，**不引入 webfont**（中文字体动辄数 MB）；display 与 body 同源是有意取舍 |

### 硬约束（改动前必读）

1. **禁止字面色**：任何 `#rrggbb` / `rgb()` / `oklch()` 只能出现在 `:root` 内。需要新值就先进
   token 块再起名字，然后 `var()` 引用（hallmark 称 mid-render token improvisation）。
2. **数字列必须 `font-variant-numeric: tabular-nums`**（端口、时间戳、表格数值）。
3. **grid 的 fr 轨道一律 `minmax(0, …fr)`**，禁止裸 `1fr` —— 轨道内的 xterm/`<pre>`/表格
   固有宽度大会顶破容器，钉到 0 后溢出由子元素自己的 `overflow-x: auto` 承接。
4. **`body` 是 `height:100vh` + flex column + `overflow:hidden` 的 app shell**，内层各自滚动。
   这层 `hidden` 是**承重**的：改成 `visible` 会造出横向滚动，改成 `clip` 需连带回归测试
   全部 fixed 抽屉（`#detailDrawer` / `#settingsDrawer` / `.sidebar`）。
5. **窄屏判据**：320 / 390 / 768 / 1280 四档均须 `documentElement.scrollWidth == clientWidth`
   （无横向溢出）。真渲染取证法见 `agent-knowledge/57`（headless chromium + iframe 视口法，
   因为 `--window-size` 在本机 chromium 152 有 500px 下限）。
6. **z-index 尺度**：45 / 46 / 50 / 60 / 99（尚未 token 化，见 `agent-knowledge/57` m1）。
