# 协同模块重构设计 · 多 Agent × 多模型混排 + 会话历史 + 执行记录 + 结果汇总

**日期**：2026-11-03｜**基线**：agenthub v0.13.189（`src/main.py:126`）｜**性质**：**增量重构**，非从零设计
**上游判例**：`agent-knowledge/316-协同页四框改版-参数落库与统计口径三坑.md`（四框已落地 + 5 个假绿）
**现状清单来源**：任务 t1 勘察（本文所有 `file:line` 均已回原文核对）

---

## 0. 结论先行：四条需求里只有两条半是新的

需求逐条对照现网代码，**已实现的不得重造**：

| 需求 | 现状（已取证） | 缺口 | 本次工作量 |
|---|---|---|---|
| ① 前端四框（需求/Agent/模型/轮次/执行） | **四框已在**：`templates/index.html:2076-2083`，逻辑在 `static/hub/04-terminal-ws.js:2950-3080`（`:3081` 起是口令桥）；预览/执行分离、轮次档位由 `/api/tasks/pool` 下发 | Agent 与模型都是**单选 `<select>`**（`:2079-2080`）⇒ 无法"多 Agent 多模型混排"；模型联动只按**单个** Agent 拉 `/api/models?agent_id=` | 中：两个框改多选 + 联动过滤接口 |
| ② 会话记录历史，可查看/继续执行 | 有 **run** 级历史：`GET /api/tasks/runs` 带 `q/state/agent/model/days/offset` + `total`（`tasks.py:379-418`）；重拆链 `parent_run_id`/`child_run_id`（`tasks.py:181-200`）；前端历史卡 + 「重拆下一轮」按钮（`04-terminal-ws.js:3161-3205` 的 `loadRuns`、`:3401-3410` 的 `replanRun`） | **没有 Session 实体**：run 是"一轮执行"，不是"一件事的上下文"。刷新页面/换设备后表单参数丢失；同一需求的多次重拆只靠血缘链串，无法按"会话"聚合与续跑 | 中：新表 `collab_sessions` + `task_runs.session_id` + 6 个接口 |
| ③ 执行记录汇总（哪些组合成功/失败、为什么） | `GET /api/tasks/stats` 已有 `combos`（agent×model）+ `failures`（`error_bucket` 归因桶）+ `rounds`（`tasks.py:435-541`）；归因纯函数 `error_bucket`（`tasks_tiers.py`，8 档规则 + `exit:<code>` 兜底，**不返回"其他"**） | `combos` 是**任务级**聚合，而升档会**覆盖** `tasks.agent_id`（`tasks_runner.py:162-165`，注释明写"必须同步改写"）⇒ **首次尝试用的组合在库里已不存在**，只在 `profile_events.detail` 的 JSON 里且 `subject` 语义已被污染。混排评估要的正是"每个组合各自成没成" | **大**：新表 `task_attempts`（attempt 级），这是本次唯一的真·新增数据面 |
| ④ 任务执行结果汇总 | 同上 `stats` 的 `summary`（runs/成功率/耗时/p50/token）+ `verdict`（`n<10` 明写"样本不足"） | 缺 **session 维度**与 **attempt 维度**；混排专属指标（同任务多组合对比、组合命中分布）无 | 小：在既有 `stats` 之上加维度，不另立口径 |

**一句话**：①改控件形态、②③加两张表、④加两个维度。**统计口径与失败归因不重写**——判例 316 已把三个口径坑（GROUP BY 同名歧义 / 本地时区 vs UTC / run 侧漏时间窗）修好并落成断言，重写等于把坑再踩一遍。

---

## 1. 「混排」必须先定义，否则后端会做错

用户原话"多 agent 多模型混排"有三种互斥语义，**实现前必须选定**，因为成本差 N 倍：

| `assign_mode` | 语义 | 子任务数 | token 成本 | 适用 |
|---|---|---|---|---|
| `auto`（**默认**） | 多选 = **收窄 Leader 的候选池**，仍由 Leader 按难度分配 | = 拆解结果 | ×1 | 日常；等价于现状但池子可控 |
| `spread` | 多选 = **轮转强制混排**：第 i 个任务派给 `agents[i % len]`，模型同轮转 | = 拆解结果 | ×1 | 想让多家各跑一部分并横向比较 |
| `race` | 多选 = **同任务并行派多个组合**，首个过 verifier 者胜出 | = 拆解结果 × 组合数 | **×N** | A/B 选型（判例 106：单次读数不可分辨实现差异，必须配对） |

### 三条硬约束

1. **`race` 必须显式点头 + 强制预算**：与 `auto_rounds`（"烧钱 + 改状态"必须显式，`tasks_dag.py:33-36`）同口径。`assign_mode=race` 而 `budget_tokens` 为空 ⇒ **400**，不静默跑飞。
2. **`spread`/`race` 不得绕过模型域判定**：钉死模型只对模型域 = CCR 的 agent 生效（`tasks_dag._allowed_models_for:249-274`）。给 codebuddy/cursor 塞 CCR ID 必报 `unrecognized_model`；dsh 的模型在 profile 里、无 CLI 开关。**轮转遇到非 CCR 域 agent 时：该任务跳过钉死并写进 `model_policy.skipped_agents` 点名，绝不装作钉上了**（判例 316 §二.3）。
3. **`auto` 是现状的严格超集**：不传 `agents`/`assign_mode` 时，`LEADER_PROMPT` 的 `{agents}` 仍填 `dispatchable_ids()` 全集、`default_agent` 逻辑逐字不变（`tasks_dag.py:340-343`）⇒ 老调用方（MCP `hub_dispatch`、`~/bin/hub-dispatch`）行为不变。

> **本设计以 `auto` + `spread` 为 P0，`race` 列 P2**：`race` 需要"同任务多行"的语义变更（`tasks` 表 PK 是 `(run_id, task_id)`，`tasks.py:137-166`，一个任务一行装不下多组合），爆炸半径远超本次改动面。P2 方案见 §8。

---

## 2. 前端交互流程

### 2.1 布局（改 `templates/index.html:2070-2145`，S3 协同卡）

```
┌─ 协同 DAG ─────────────────────────────────────────────────────┐
│ [会话] ▾ 新建会话 / 打开最近…        ← 新增（需求②）            │
│ 需求：[textarea#taskGoal .................................]     │
│ Agent：[☑claude-code ☑codex ☐pi ☐dsh …]  ← 多选，替代单选      │
│        混排方式：(•)自动分配 ( )轮转混排 ( )并行竞速[需预算]     │
│ 模型： [☑deepseek-v4.1-flash ☐qwen3.8-max …] ← 随 Agent 联动   │
│        ⚠ dsh/codebuddy 不吃钉死模型（模型域非 CCR）← 联动提示   │
│ 轮次： [2 轮 · 未完成自动重拆 ▾]        ← 已有，档位来自后端     │
│ [仅预览] [拆解并启动]   提示行#taskPreview                      │
├─ 执行结果汇总 ─────────────────────────────────────────────────┤
│ verdict 行 + summary 五数（已有）+ 会话维度切换（新增）         │
│ 组合表#statsCombos ｜ 失败归因表#statsFailures ｜ 轮次表#statsRounds │
├─ 会话与历史 ───────────────────────────────────────────────────┤
│ 搜索#runQ 状态#runState + [仅看本会话] ← 新增；分页 runsMore     │
└────────────────────────────────────────────────────────────────┘
```

**多选控件实现约束（硬）——复用既有 `mountPicks`，不新造组件**：

本机已有一个正好合用的组件：`static/hub/06-manager-tasks.js:49-77` 的 `mountPicks(selId)`，
v0.13.40 为 `memFedSrcs`（11 项）/`kbRoutes`（5 项）/`instTo`（4 项）三个多选源做的视觉重排
（boot 期挂载点 `:423-426`）。它的口径正是本设计要的：

> **原生 `<select multiple>` 仍是唯一数据源**——option 清单与 `selectedOptions` 都不动，
> 只是被 CSS 隐藏（`.picks-src`），前面插一行可换行的芯片复刻勾选语义：
> 点芯片 = 翻转该 `option.selected` = 派发 `change`。芯片行高度由内容与换行决定。
> 幂等：重复调用先摘掉旧芯片行（`loadSkills` 每次重写 option 后要重挂）。

⇒ `#taskAgent` / `#taskModel` 改成 `<select multiple>` + `mountPicks()`，**读值仍用
`Array.from(sel.selectedOptions).map(o => o.value)`**（与 `04-terminal-ws.js:403`、`:2757`
同一写法）。`.picks` / `.pick` / `.pick.on` 三个 CSS 类已存在，**零新增样式**，手机端换行
行为已被那三个多选源验证过。这比新造 checkbox 组少一个失败面（样式、无障碍、幂等重挂都已有先例）。

`mountPicks` 需要**一处扩展**（当前实现的真实缺口，已核对 `:59-70`）：它遍历 `sel.options`
时**不看 `o.disabled`**，芯片一律可点 ⇒ §2.3 要求的"模型域不适用项标灰 + 逐项说明"落不了地。
改法：`b.disabled = o.disabled; b.title = o.title || ''; b.classList.toggle('off', o.disabled)`，
并加 `.pick.off` 样式（灰 + `cursor:not-allowed`）。**这是共用组件，改前先 grep 全部调用点**
（实测：`['memFedSrcs','kbRoutes'].forEach(mountPicks)` 在 `:426`，`instTo` 在 `loadSkills` 内），
扩展必须对现有三个多选源零行为变化（`disabled` 缺省 false ⇒ 逻辑等价）。

- 用 `data-*` + **事件委托**，禁用 `onclick="f('字符串')"`。判例 316 §五之二缺陷⑤：闸门 `tests/test_html_js_escape_depth.py` 的正则对"实参含括号"结构性失明，6 处漏转义它全放过、pytest 照样全绿。存量 **13 条**已落棘轮 fixture `tests/fixtures/inline_onclick_string_argv.txt`（实测 `wc -l`=15，含 2 行注释头；分布在 `02-nav-and-poll.js`×3、`03-agents-cards.js`×2、`05-chat-and-history.js`×8），**只判有没有新增**⇒ 新代码新增一处即红。
- 改 `static/hub/04-terminal-ws.js`（**源分片**），改完必须 `bash scripts/build_hubjs.sh` 重建 `static/hub.js`。闸门 `tests/test_hubjs_split.py` 判"拼接结果与仓库里的 hub.js 逐字节相等"⇒ **忘 build 必红**。build 脚本自带原子写（临时文件 + `mv`）与 `?v=` 提手同步（内容 md5 前 8 位），不要手工改提手。

### 2.2 状态流转

```
       ┌──────────┐  改任一框   ┌──────────┐
       │ idle     │───────────▶│ dirty    │  #taskPreview 显示"参数已改，未预览"
       └──────────┘            └────┬─────┘
              ▲                     │ 点「仅预览」POST decompose{dry_run:true}
              │                     ▼
              │              ┌──────────┐  预览失败(400/502) → err 提示行，回 dirty
              │              │ previewed│  不落库不花钱；显示拆出 N 个 + 组合清单
              │              └────┬─────┘
              │                   │ 点「拆解并启动」POST decompose{auto_run:true}
              │                   ▼
              │              ┌──────────┐  轮询 GET /api/tasks/{run}（既有 02-nav-and-poll.js）
              │              │ running  │  按钮禁用；显示 第N/M轮 · 成功/失败/阻塞
              │              └────┬─────┘
              │        ┌──────────┼───────────┐
              │        ▼          ▼           ▼
              │   ┌────────┐ ┌────────┐ ┌──────────┐
              │   │success │ │partial │ │failed/   │
              │   └───┬────┘ │failed  │ │max_rounds│
              │       │      └───┬────┘ └────┬─────┘
              │       │          │           │ 「重拆下一轮」POST /{run}/replan
              │       │          └───────────┤ 闸门在后端 replan_gate，前端不预判
              │       │                      ▼
              └───────┴──────────── session 续跑（新增）POST /api/collab/sessions/{id}/rerun
```

**五条状态判据**：
1. **`dirty` 必须可视化**：改框后未预览就点执行 ⇒ 允许（执行自带拆解），但提示行明写"未预览直接执行"。判例 316 §二.1 的原始动机就是"选错模型或选错轮次就是一轮 token 打水漂"。
2. **`running` 期间禁用所有参数框**，不只禁按钮。运行中改框会造成"屏上参数 ≠ 落库参数"，这是判例 316 §三.③"同屏两个数自相矛盾"的同族。
3. **`stop_reason` 不伪造**：手动 run（`auto_rounds=0`）该字段恒为 NULL，语义是"重拆为什么停"而非"run 成没成"⇒ 前端按任务状态推导展示文案（现有实现 `04-terminal-ws.js:3237-3259` 的 `renderRunSummary` 分支已正确，沿用，**不回填该字段**）。
4. **会话态与 run 态分开**：session 的 `state` 是"这件事还开着吗"（open/closed），run 的 `state` 是"这一轮跑得怎样"（`_RUN_STATE_SQL`，`tasks.py:370-376`）。UI 上并列显示，不合并成一个字段。
5. **轮询不要新加一路**：协同页的自动刷新**已经存在**——`static/hub/06-manager-tasks.js:414-420`
   的 6 秒 `setInterval`，条件是 `page-tasks` 可见时 `loadRuns(); if (currentRun) openRun(currentRun)`。
   新增的 attempt/session 拉取必须**挂进这条已有周期**（在 `openRun` 内并发拉，见 §6），
   不新增 `setInterval`。理由：每多一路轮询就多一路抖动，而判例 106
   （`agent-knowledge/106-探针方差未量就下结论-单次读数讲不出劣化几倍.md`）的结论正是
   "用读数讲差异之前先量读数自己的方差"；另注：`04-terminal-ws.js:1860` 与
   `12-activity.js:112` 各有一条独立 `setInterval`，后者的注册处有闸门保护
   （注释明写"守卫放在函数体内而不是注册处，动注册会撞红"）⇒ 别去动它。

### 2.3 Agent ↔ 模型联动过滤规则

| 选中 Agent 集合 | 模型框内容 | 提示 |
|---|---|---|
| 空（= 自动混排） | 只有「模型：自动（按难度 t1/t2/t3）」 | "未指定 Agent = 自动混排：钉死一个模型与让不同 agent 各自最合适互斥"（现有逻辑 `04-terminal-ws.js:2997-3005` 的 `if (!agent)` 分支，保留） |
| 全部为 CCR 域 | 该域**并集**清单（去重、按 vendor 分组、标 `· 免费`） | 无 |
| 含非 CCR 域（dsh/codebuddy/cursor/自定义端口） | 并集清单，但**每个不适用项标灰 + 逐项说明** | "钉死模型对 dsh/codebuddy 不生效（模型域非 CCR），这些任务会跳过钉死" |
| 清单取不到（`checked=false`，网关挂/无凭据） | 只有「自动」 | **"模型清单取不到：<原因>"，不显示空清单冒充"没有模型"**（`modelcfg.catalog_ids:123-139` 的 checked 语义；判例 316 §二.2） |

CCR 域判定唯一真值 = `tasks_dag._ccr_model_agents()`（`tasks_tiers.py`）：`{"claude","jcode"} | {a for a,sp in cli_agent.SPECS.items() if sp["model_path"]=="ccr"}`。**前端不得另存一份域名单**（同"轮次档位由后端下发"的既有纪律，`04-terminal-ws.js:2955-2957`）。

---

## 3. 数据模型

### 3.1 新表 `collab_sessions`（需求②）

```sql
CREATE TABLE IF NOT EXISTS collab_sessions (
    session_id  TEXT PRIMARY KEY,          -- uuid4().hex[:10]，与 run_id 同形态
    title       TEXT,                      -- 缺省取 goal 前 40 字；用户可改
    goal        TEXT NOT NULL,             -- 需求原文（续跑时回填输入框）
    -- 四个框的**落库快照**（JSON 数组）。为什么存快照而不是只存 session→run 关联：
    -- 续跑要"用上次的参数再来一轮"，参数只存在 run 的推导结果里就还原不出来
    -- （tasks.agent_id 会被升档覆盖，见 §3.3）。
    agents_json TEXT NOT NULL DEFAULT '[]',   -- ["claude-code","codex"]
    models_json TEXT NOT NULL DEFAULT '[]',   -- ["alibaba/deepseek-v4.1-flash"]
    assign_mode TEXT NOT NULL DEFAULT 'auto', -- auto|spread|race
    rounds      INTEGER NOT NULL DEFAULT 1,   -- 该会话声明的轮次预算（clamp 后）
    verify_kind TEXT NOT NULL DEFAULT 'nonempty',
    budget_ms      INTEGER,                -- NULL = 不设限（既有口径 tasks_tiers.budget_exceeded）
    budget_tokens  INTEGER,
    state       TEXT NOT NULL DEFAULT 'open',-- open|closed；closed 仍可查、续跑时自动转回 open
    current_run_id TEXT,                   -- 最近一轮 run（打开会话即定位）
    run_count   INTEGER NOT NULL DEFAULT 0,
    created_by  TEXT,                      -- 调用方标识（前端传 'ui'，MCP 传 agent id）
    created_at  TEXT NOT NULL,             -- UTC isoformat（+00:00），与 tasks.created_at 同口径
    updated_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_collab_sessions_updated ON collab_sessions(updated_at);
```

`task_runs` 加一列（**幂等 ALTER**，与 `max_rounds` 同一套写法，见 `tasks.py:196-206` 的 `ensure_schema`）：

```sql
ALTER TABLE task_runs ADD COLUMN session_id TEXT;   -- NULL = 存量 run / 非会话入口（MCP、CLI）
CREATE INDEX IF NOT EXISTS idx_task_runs_session ON task_runs(session_id);
```

> **不复用 `chat_sessions`**（`db.py:55-61`）：它的 PK 语义是"一个 agent 的一段对话"（`agent_id NOT NULL`），而协同会话是"一个 goal × 多个 agent × 多轮 run"。塞进去会让 `agent_id` 变成假字段，且 `chat_messages` 会被协同 run 污染。**同名字段不等于同一实体**——这正是判例 316 §三.① "两个口径漂移"的成因族。

### 3.2 新表 `task_attempts`（需求③的核心）

```sql
CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_id   TEXT PRIMARY KEY,         -- uuid4().hex[:10]
    run_id       TEXT NOT NULL,
    task_id      TEXT NOT NULL,
    attempt_no   INTEGER NOT NULL,         -- 0 = 首次；1..MAX_AUTO_RETRY = 重试（tasks_tiers.MAX_AUTO_RETRY 默认 1）
    session_id   TEXT,                     -- 冗余存一份：attempt 级统计不必三表 JOIN
    -- 组合四元组（这是"哪些组合成功/失败"的最小可判单元）
    agent_id        TEXT,                  -- **本次实际派发**的执行者（升档后 = 新值）
    model_requested TEXT,                  -- 我方请求值（= 当时的 model_planned or model_choice）
    model_observed  TEXT,                  -- adapter 回包实发值（网关可能改写）
    tier            TEXT,                  -- t1|t2|t3|explicit|native|NULL
    -- 结果
    status       TEXT NOT NULL,            -- pass|fail|timeout|exec_error|parked|budget
    verdict      TEXT,                     -- verifier 三态 pass|fail|insufficient；NULL = 未走到验收
    gap_ids      TEXT,                     -- verifier gaps 的 id 逗号串（empty_output/refusal/self_reported_not_run/…）
    error        TEXT,                     -- 原文（截 500，与 tasks.error 同口径）
    error_bucket TEXT,                     -- tasks_tiers.error_bucket() 的归因结果（落库而非查询时算：口径变了要能看出历史是用哪版算的）
    escalated_from_agent TEXT,             -- 本次是否升档而来（NULL = 没有）
    escalated_from_model TEXT,
    duration_ms  INTEGER,
    spent_tokens INTEGER,
    started_at   TEXT,
    finished_at  TEXT,
    created_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_attempts_run ON task_attempts(run_id, task_id);
CREATE INDEX IF NOT EXISTS idx_attempts_combo ON task_attempts(agent_id, model_observed);
CREATE INDEX IF NOT EXISTS idx_attempts_session ON task_attempts(session_id);
```

**为什么必须新表，不能只查 `profile_events`**（这是本设计最要紧的取舍，已取证）：

1. 升档**覆盖** `tasks.agent_id`：`tasks_runner.py:162-165` `UPDATE tasks SET agent_id=?,escalated_from_agent=?,…`，注释明写"`agent_id` 必须同步改写，否则展示面与实际执行者不一致（v0.13.177 实弹）"⇒ 覆盖是有意的、不能改。于是 `tasks` 表里只剩**最后一次**的组合，`/api/tasks/stats` 的 `combos`（`tasks.py:486-498`）统计的是"最后组合"，首次失败的组合被吞掉。
2. `profile_events` 能部分还原但**语义已污染**：`task_exec` 事件的 `subject` 传的是 `row["agent_id"]`（升档后读到的已是新值，`tasks_runner.py:260,327,369`），`detail` JSON 里有 `model`/`attempt`/`verify`，但 escalate 事件（`:166-172`）与 settle 事件的 agent 口径不一致；且 `detail` 是 TEXT JSON ⇒ 每次统计都要全表扫 + Python 解析，无法建索引。
3. 结论：**新表 append-only，只增不改**。写入点覆盖 `tasks_runner` 的全部 6 个出口（见 §5.2），与既有纪律"三条执行失败出口各写一遍必然漂移"（`tasks_runner._schedule_exec_retry` 文档串）同源⇒ 抽一个 `_record_attempt()` 单点写入。

> **不回填存量**：老 run 的 attempt 无法可靠复原（events 的 agent 语义已污染）。接口对 `attempts` 为空的存量 run **明写"存量 run 无 attempt 记录（本次起才有）"**，不用 tasks 行伪造一条 attempt（伪造 = 判例 316 §六"回填=伪造"同族）。

### 3.3 现有表的既有字段（**沿用，勿改语义**）

| 字段 | 位置 | 口径（已取证，不得合并） |
|---|---|---|
| `tasks.model` | `tasks.py:150` 注释 | adapter **回包**实发模型（观测值） |
| `tasks.model_choice` | `tasks.py:112-118` | Leader 拆解时**请求**的模型（意图值） |
| `tasks.model_planned` / `tier` / `tier_reason` | `tasks.py:125-130` | 难度→档位的**决议**值 |
| `tasks.agent_promoted_from` | `tasks.py:131-134` | 拆解期被难度地板抬上来的来源（未花钱） |
| `tasks.escalated_from_agent/model` | `tasks.py:107-110` | 失败后升档来源（已花过一次钱） |
| `tasks.verdict` / `verify_reason` | `tasks.py:152-156` | 验收裁决；**NULL = 未走到验收，与 'fail' 语义不同，不可混同** |
| `tasks.attempts` | `tasks.py:106` | 自动重试计数（上限 `MAX_AUTO_RETRY`） |
| `tasks.retrieval_json` / `judge_json` | `tasks.py:101-105,119-124` | 检索线索 / 主链判定证据（留原文 JSON，拆列无收益） |
| `task_runs.stop_reason` | `tasks.py:191` | `complete/max_rounds/same_failure/budget/unfinished`；**手动 run 恒 NULL** |
| `task_runs.max_rounds` | `tasks.py:186-189` | NULL = 存量/老调用方 ⇒ 重拆层回落 `TASK_MAX_ROUNDS`（默认 2） |

三列三口径（`model_planned` / `model_choice` / `model`）是既有约定，`tasks.py:112-118` 明写"合成一列就再也分不清「选了没生效」与「选了但网关换了」——正是 v0.13.167 那条的病根"。**attempt 表沿用同一分法**（`model_requested` / `model_observed`）。

---

## 4. 接口签名

### 4.1 新增：`GET /api/tasks/model-choices`（联动过滤的唯一真值）

为什么不给前端复用 `/api/models?agent_id=`：那个端点**一次只能问一个 agent**（`api_models.py:18-20`），多选要 N 次请求且前端得自己算并集与域判定⇒ 域名单会在前端长出第二份真值。

```
GET /api/tasks/model-choices?agents=claude-code,codex,dsh
```

**响应**
```json
{
  "ok": true,
  "checked": true,
  "agents": ["claude-code", "codex", "dsh"],
  "models": [
    {"id": "alibaba/deepseek-v4.1-flash", "name": "deepseek-v4.1-flash",
     "owner": "alibaba", "billing": "paid", "vendor": "alibaba",
     "applicable": ["claude-code", "codex"]}
  ],
  "groups": {"alibaba": ["alibaba/deepseek-v4.1-flash"], "openrouter": ["…"]},
  "pinnable_agents": ["claude-code", "codex"],
  "skipped_agents": [{"id": "dsh", "reason": "模型在 profile 里，无 CLI 开关"}],
  "count": 1,
  "note": "3 个 Agent 里 2 个可钉死模型；dsh 会跳过钉死"
}
```

**三态必须可区分**（判例 316 §二.2 与 `modelcfg.catalog_ids` 的 `checked` 语义）：
| 情形 | 响应 | 前端 |
|---|---|---|
| 清单取得、有模型 | `checked:true, models:[…]` | 正常多选 |
| 清单取得、agent 全非 CCR 域 | `checked:true, models:[], skipped_agents:[…]` | 只留「自动」+ 逐项原因 |
| **清单取不到**（网关挂/无凭据） | `checked:false, models:[], error:"<原因>"` | 只留「自动」+ **"清单取不到"，不许显示成"没有模型"** |

`agents` 为空 ⇒ `400`（"未选 Agent 时模型只能自动"，与前端 `:2996-3004` 同一口径）。

### 4.2 扩展：`POST /api/tasks/decompose`

`DecomposeIn`（`tasks_dag.py:25-48`）**新增 5 个字段，全部可选**，不传则行为逐字不变：

```python
class DecomposeIn(BaseModel):
    goal: str = Field(min_length=4, max_length=4000)
    default_agent: Optional[str] = None          # 既有：单选兜底，保留
    auto_run: bool = False                       # 既有
    verify_kind: str = "nonempty"                # 既有
    budget_ms: Optional[int] = None              # 既有
    budget_tokens: Optional[int] = None          # 既有
    auto_rounds: bool = False                    # 既有：老开关，rounds 为空时才用
    dry_run: bool = False                        # 既有
    model: Optional[str] = Field(default=None, max_length=200)   # 既有：单模型钉死，保留
    rounds: Optional[int] = None                 # 既有
    # ── 新增（本次）──────────────────────────────────────────
    agents: Optional[List[str]] = None           # 多选候选池；None/[] = 全池（现状）
    models: Optional[List[str]] = None           # 多选模型；配合 assign_mode 使用
    assign_mode: str = "auto"                    # auto|spread|race
    session_id: Optional[str] = None             # 归属会话；给空且 create_session=true 时新建
    create_session: bool = False                 # 显式建会话（同 auto_rounds 的"改状态要点头"口径）
    session_title: Optional[str] = Field(default=None, max_length=120)
```

**校验（全部在 `_validate_*` 层，返回 400 不静默）**
1. `agents` 里每个 id 必须在 `dispatchable_ids()` 内（`registry.py:64-90`）。不在册 ⇒ **400 并点名**，不静默丢弃（丢弃 = "选了没生效"藏起来）。
2. `assign_mode` 必须 ∈ `{auto, spread, race}`；`race` 且 `budget_tokens` 为空 ⇒ 400。
3. `models` 每个 id 必须在 `modelcfg.catalog_ids()` 内（**仅当 `checked=true`**）；`checked=false` ⇒ 不校验（判不了就不拦）。
4. `session_id` 给了但不存在 ⇒ **404**，不自动建（自动建会让拼错的 id 静默开出一个孤儿会话）。
5. `models` 长度 > 1 且 `assign_mode == "auto"` ⇒ **400**：自动分配下 Leader 自己按难度选档，钉多个模型语义不明（同"未选 Agent 时不列具体模型"的既有判据）。

**请求示例**
```json
{
  "goal": "重构协同模块并用混排测试验证",
  "agents": ["claude-code", "codex"],
  "models": ["alibaba/deepseek-v4.1-flash"],
  "assign_mode": "spread",
  "rounds": 2,
  "create_session": true,
  "session_title": "协同重构",
  "auto_run": false,
  "dry_run": true
}
```

**响应（在既有响应上加 3 段，`tasks_dag.py:368-384`）**
```json
{
  "run_id": null, "dry_run": true, "goal": "…",
  "tasks": [{"id":"t1","task":"…","agent_id":"claude-code","difficulty":2,
             "deps":[],"tier":"explicit","model_planned":"alibaba/deepseek-v4.1-flash",
             "tier_reason":"pinned:界面钉死模型","assign":"spread:0"}],
  "synthesis": {"added": true, "id": "t_sum"},
  "tiers": {"…": "既有 _plan_tiers 的 note"},
  "model_policy": {"mode":"pinned","model":"alibaba/deepseek-v4.1-flash",
                   "applied":["t1"],"skipped_agents":["dsh"],"catalog_checked":true,
                   "note":"已钉死 …（1 个任务）"},
  "assign_policy": {"mode":"spread","agents":["claude-code","codex"],
                    "assignment":{"t1":"claude-code","t2":"codex","t_sum":"claude-code"},
                    "pool_restricted":true,
                    "note":"轮转混排：3 个任务派给 2 个 Agent（claude-code×2, codex×1）"},
  "session": {"session_id":"a1b2c3d4e5","title":"协同重构","created":true,
              "run_count":0,"state":"open"},
  "auto_rounds": true, "rounds": 2, "max_rounds": 2, "round_choices": [1,2,3,5]
}
```

`dry_run=true` 时 **`session_id` 不落库**（`created:false, note:"dry_run 不建会话"`）——预览不得产生能跑的 run，与 `rounds` 只在确认时落库同一理由（`hubmcp.py:748-752`）。

### 4.3 会话接口（新增，前缀 `/api/collab/`）

为什么独立前缀而不放 `/api/tasks/sessions`：`/api/tasks/{run_id}`（`tasks.py:601`）是路径参数，`sessions` 会与之歧义（现有 `/api/tasks/runs` 靠**定义顺序在前**才不撞，`tasks.py:379` vs `:601`）⇒ 不再往这个雷区加路由。

| 方法 | 路径 | 用途 |
|---|---|---|
| `GET` | `/api/collab/sessions?limit&offset&q&state&agent&model&days` | 会话历史列表（与 `/api/tasks/runs` 同一套过滤语义） |
| `POST` | `/api/collab/sessions` | 建会话（只存参数，不拆解不花钱） |
| `GET` | `/api/collab/sessions/{session_id}` | 会话详情 + 其下全部 run 的轮次链 |
| `PATCH` | `/api/collab/sessions/{session_id}` | 改标题/表单草稿/state |
| `DELETE` | `/api/collab/sessions/{session_id}` | 删会话（**不删 run**，见下） |
| `POST` | `/api/collab/sessions/{session_id}/rerun` | **继续执行**：用会话参数重拆下一轮 |

**`GET /api/collab/sessions` 响应**
```json
{
  "sessions": [{
    "session_id": "a1b2c3d4e5", "title": "协同重构", "goal": "重构协同模块并用混排测试验证",
    "state": "open",
    "agents": ["claude-code","codex"], "models": ["alibaba/deepseek-v4.1-flash"],
    "assign_mode": "spread", "rounds": 2,
    "run_count": 3, "current_run_id": "186cafc828",
    "last_state": "partial", "last_stop_reason": "same_failure",
    "totals": {"tasks": 21, "success": 16, "failed": 4, "blocked": 1,
               "attempts": 24, "attempt_pass": 17, "duration_ms": 412000, "tokens": 183000},
    "created_by": "ui", "created_at": "2026-11-03T01:37:00+00:00",
    "updated_at": "2026-11-03T02:22:11+00:00"
  }],
  "total": 1, "limit": 20, "offset": 0, "has_more": false,
  "filters": {"q":"", "state":"", "agent":"", "model":"", "days":0}
}
```

`last_state` 用**与 `/api/tasks/runs` 同一个** `_RUN_STATE_SQL`（`tasks.py:370-376`）算，不另写一份 CASE（两套判定必然漂移）。

**`POST /api/collab/sessions/{id}/rerun`**
```json
// 请求（全部可省；省 = 用会话里存的快照）
{"goal": "…可覆盖…", "agents": null, "models": null, "assign_mode": null,
 "rounds": 1, "auto_run": true, "note": "人工再推一轮"}
// 响应
{"ok": true, "session_id": "a1b2c3d4e5", "run_id": "9f8e7d6c5b", "round": 4,
 "parent_run_id": "186cafc828", "max_rounds": 2,
 "gate": {"go": true, "kind": "go"},
 "note": "已生成第 4 轮；轮次上限只允许收紧（传 5 也压回 2）"}
```
闸门复用 `tasks_replan.replan_gate`（`tasks_replan.py:56-90`），**不在会话层另判一遍**（判例 316 §五之二："前端自己判一遍就又变成两个入口两套口径"）。`max_rounds` 只能收紧（`hub_replan` 同口径）。

**`DELETE` 语义**：只删 `collab_sessions` 行并把其下 `task_runs.session_id` 置 NULL；**不删 tasks / task_attempts**（执行记录是审计面，删会话不等于销毁证据）。要删 run 用既有 `DELETE /api/tasks/{run_id}`（`tasks.py:736-743`）。

### 4.4 执行记录接口（需求③）

**`GET /api/collab/executions`** —— attempt 级明细，跨 run/session 查
```
GET /api/collab/executions?session_id=&run_id=&agent=&model=&status=&bucket=&days=30&limit=50&offset=0
```
```json
{
  "attempts": [{
    "attempt_id":"7c1d…","run_id":"2b997a1f15","task_id":"t2","attempt_no":0,
    "session_id":"a1b2c3d4e5","round":1,
    "agent_id":"codex","model_requested":"alibaba/qwen3.8-flash",
    "model_observed":"alibaba/qwen3.8-flash","tier":"t2",
    "status":"timeout","verdict":null,"gap_ids":"",
    "error":"执行超时 600s（进程未退）","error_bucket":"超时",
    "escalated_from_agent":null,"escalated_from_model":null,
    "duration_ms":600012,"spent_tokens":0,
    "started_at":"2026-11-03T02:10:00+00:00","finished_at":"2026-11-03T02:20:00+00:00",
    "goal":"…","task_excerpt":"核验闸门正则对含括号实参…"
  }],
  "total": 24, "limit": 50, "offset": 0, "has_more": false,
  "window_from": "2026-10-04T02:22:00+00:00",
  "degraded": {"legacy_runs_without_attempts": 7,
               "note": "7 个存量 run 无 attempt 记录（本次起才有），不计入本页分子分母"}
}
```
`degraded` 段**必须存在**：把"真没有"与"存量取不到"分开（AGENTS.md 接单前置同一口径；判例 316 §六）。

**`GET /api/tasks/{run_id}/attempts`** —— run 内 attempt 明细（run 面板用）
```json
{"run_id":"2b997a1f15","attempts":[…同上…],
 "by_task":{"t1":[{"attempt_no":0,"agent_id":"claude-code","status":"pass"}],
            "t2":[{"attempt_no":0,"agent_id":"codex","status":"timeout"},
                  {"attempt_no":1,"agent_id":"claude-code","status":"fail",
                   "escalated_from_agent":"codex"}]},
 "legacy": false}
```

### 4.5 汇总统计接口（需求④）

**`GET /api/collab/stats?days=30&top=6&session_id=&level=attempt|task|both`**

**沿用 `/api/tasks/stats` 的四段结构**（`summary/combos/failures/rounds`，`tasks.py:531-541`），新增三段：

```json
{
  "ok": true, "days": 30, "window_from": "2026-10-04T02:22:00+00:00", "level": "both",
  "summary": {
    "runs": 15, "runs_success": 6, "runs_rate": 0.4, "states": {"success":6,"partial":5,"failed":4},
    "tasks": 62, "success": 48, "failed": 11, "blocked": 3, "retried": 9,
    "duration_ms": 1830000, "p50_ms": 19290, "tokens": 940000,
    "sessions": 4, "sessions_open": 2
  },
  "attempts": {
    "n": 71, "pass": 52, "fail": 15, "timeout": 3, "exec_error": 1,
    "pass_rate": 0.732,
    "task_pass_rate": 0.774,
    "rate_gap_note": "attempt 级 73.2% < task 级 77.4%：9 条任务靠重试/升档才过",
    "escalated": 9, "first_pass": 43
  },
  "combos": [{
    "agent_id":"codex","model_eff":"alibaba/qwen3.8-flash",
    "n":11,"ok":9,"failed":2,"blocked":0,"retried":2,"escalated":1,
    "rate":0.818,"avg_ms":21000,
    "attempts_n":13,"attempts_pass":9,"attempts_rate":0.692,
    "top_buckets":[{"bucket":"超时","n":2},{"bucket":"验证未过","n":1}],
    "sample_insufficient": false
  }],
  "failures": [{"bucket":"超时","n":5,
                "agents":[["codex",3],["claude-code",2]],
                "combos":[["codex","alibaba/qwen3.8-flash",3]],
                "sample":"执行超时 600s（进程未退）","gap_ids":[""]}],
  "rounds": [{"round":1,"runs":14,"n":35,"ok":28},{"round":2,"runs":1,"n":3,"ok":1}],
  "mix": {
    "assign_modes": {"auto":9,"spread":5,"race":0},
    "pool_sizes": {"1":4,"2":8,"3":2},
    "spread_effective": {"n":5,"agents_used_avg":2.0,
                         "note":"轮转混排 5 条 run，平均每条真用到 2.0 个 Agent"},
    "pinned": {"n":6,"applied":31,"skipped_agents":{"dsh":4,"codebuddy":2},
               "model_mismatch":1,
               "note":"6 条 run 钉死模型；1 条回包模型 ≠ 请求值（网关改写，选型未生效）"},
    "verdict": "样本不足（5 条 spread run）：不给'混排优于自动'的结论，继续观察"
  },
  "verdict": "成功率 40.0%（6/15 个 run 全绿）；62 条任务里 11 条失败"
}
```

**统计口径（六条，前五条来自判例 316 已判过的坑，一条不许破）**
1. **同一时间窗**：run 侧与 task 侧、attempt 侧都必须过 `_win()`。破了的实盘形态 = "run 15 个"与"轮次表合计 14 个 run"同屏（判例 316 §三.③）。**断言：`summary.runs == sum(rounds[].runs)`**。
2. **`GROUP BY` 写表达式**，不写同名别名（SQLite 优先绑源列 ⇒ NULL 与 1 分两组，两行都显示"第 1 轮"）。**断言：同一 round 只出一行**。
3. **窗口用 UTC**：`_days_ago` 必须带 `+00:00`（`tasks.py:421-432` 已修，复用不改）。**断言：同一时刻落库值必须被自己的窗口包含**（`test_days_ago_is_utc_like_created_at`）。
4. **比率必带 n**；`n < 10` ⇒ `sample_insufficient:true` + verdict 明写"样本不足，不给结论"。5 条样本算出来的比率是噪声不是证据。
5. **`stop_reason` 不参与 run 成败判定**（手动 run 恒 NULL），展示文案由任务状态推导。
6. **新增：attempt 级与 task 级成功率必须并列报，且差值要有解释**（`rate_gap_note`）。只报 task 级会掩盖"升档后才过"；只报 attempt 级会把重试成本读成能力差。**断言：`attempts.n >= summary.tasks`（每个任务至少一次尝试），且 `attempts.pass >= summary.success`（任务级 success 必然对应至少一次 attempt pass）**。

`mix.pinned.model_mismatch` 复用 `/api/tasks/tiers` 已有的同口径查询（`tasks.py:296-300`：`model_planned<>model` 即网关改写，注释记着 2026-10-29 实测"5 个 ID 全回 agnes-2.5-flash"）——"档位到底生效没有"这个数不报，混排结论就是自我感觉良好。

---

## 5. 后端改动清单

### 5.1 文件级

| 文件 | 改动 | 类型 |
|---|---|---|
| `src/tasks.py` | ① `SCHEMA_TASKS` 加 `collab_sessions` / `task_attempts` 两张表 + `task_runs.session_id` 幂等 ALTER（照 `ensure_schema:186-206` 现有写法）② `_RUN_AGG_SQL:345-367` 加 `MAX(r.session_id) session_id` ③ 新增 `GET /api/tasks/model-choices`、`GET /api/tasks/{run_id}/attempts` ④ 重导出新名字（门面纪律：`ROUNDS_CEILING` 等已在 `:754-768` 一带重导出） | 改 |
| `src/tasks_dag.py` | ① `DecomposeIn:25-48` 加 5 字段 ② 新增 `_restrict_pool()`（agents 白名单校验）、`_apply_spread()`（轮转分配，**在 `_plan_tiers` 之后、`_apply_pinned_model` 之前**）、`_apply_multi_model()` ③ `decompose:331-384` 接线 + `assign_policy`/`session` 两段响应 ④ `_create_run:217-246` 加 `session_id` 形参与 INSERT 列 | 改 |
| `src/collab.py` | **新文件**：`collab_sessions` 的 CRUD + rerun + 列表聚合。为什么独立文件：`tasks.py` 已 1358 行、`tasks_dag.py` 已按片拆分（`docs/SPLIT-PLAN-20261031.md`），会话是新领域面，塞进 tasks.py 会让门面继续膨胀 | 新增 |
| `src/collab_stats.py` | **新文件**：`/api/collab/stats` 与 `/api/collab/executions` 的纯 SQL 聚合 + 口径断言。与 `tasks.py:task_stats` 并列，**不改动后者**（它已被 24 例闸门覆盖，`tests/test_task_stats_l0.py`） | 新增 |
| `src/tasks_runner.py` | ① 新增 `_record_attempt()` **单点写入** ② 在 6 个出口调用：升档（`:162-172`）、自称失败（`:254-266`）、超时、任意异常、verifier 判定（`:327-373` 的 pass/fail 两路）、预算耗尽（`:423-428`）、parked（`:415-417`）③ `_resolve_exec_target:147-173` 返回 `model_requested` 供 attempt 落库 | 改 |
| `src/tasks_replan.py` | `_replan_once:140-225` 的子 run **继承 `session_id`**（现有已继承 `max_rounds`，`:225`），并 `run_count+1` / 更新 `current_run_id` | 改 |
| `src/hubmcp.py` | `hub_dispatch:722-760` 加 `agents`/`assign_mode`/`session_id` 形参（MCP 侧同口径，否则 UI 与 MCP 两套参数=判例 316 §四"链路点"漏项） | 改 |
| `src/main.py` | `include_router`（`:754-768`）挂 `collab.router`、`collab_stats.router`；`VERSION:126` 升版 | 改 |
| `~/bin/hub-dispatch` | bash CLI 加 `--agents`/`--assign`（与 MCP 同口径） | 改 |

### 5.2 `_record_attempt()` 签名与调用点（防漂移的关键）

```python
def _record_attempt(run_id: str, row: dict, *, attempt_no: int,
                    agent_id: str, model_requested: Optional[str],
                    model_observed: Optional[str], tier: Optional[str],
                    status: str, verdict: Optional[str] = None,
                    gaps: Optional[list] = None, error: Optional[str] = None,
                    escalated_from: Optional[tuple] = None,
                    duration_ms: int = 0, spent_tokens: int = 0,
                    started_at: Optional[str] = None) -> Optional[str]:
    """attempt 级执行记录的**唯一写入点**（append-only，只增不改）。

    为什么必须单点：`tasks_runner` 现在有 6 个执行出口，各写一遍必然漂移
    —— 同一个文件里已经判过一次这个错（`_schedule_exec_retry` 文档串：
    "2026-10-29 就是只给 verifier 失败写了重试，执行失败三处全漏"）。
    **写库失败不得带倒子任务**（try/except + log），与检索/技能注入同口径。
    """
```

6 个调用点（缺一即"某类失败在汇总里不存在"）：

| # | 出口 | 位置 | `status` |
|---|---|---|---|
| 1 | verifier 判 pass | `tasks_runner.py:327-336` | `pass` |
| 2 | verifier 判 fail/insufficient | `:338-373` | `fail` |
| 3 | 执行者自称失败 | `:254-266` | `exec_error` |
| 4 | 执行超时（600s，判例 316 §六未闭合项） | `_execute_task` 超时分支 | `timeout` |
| 5 | 任意异常 | 同上 except 分支 | `exec_error` |
| 6 | 预算耗尽 / parked | `:415-428` | `budget` / `parked` |

升档（`:162-172`）**不单独记 attempt**，只在下一次 settle 时带 `escalated_from`——否则一次重试会出两条 attempt，`attempts.n >= tasks` 的断言仍然成立但"尝试次数"被虚增一倍。

---

## 6. 前端改动清单

| 文件 | 改动 |
|---|---|
| `templates/index.html:2070-2145` | ① `#taskAgent`/`#taskModel` 由 `<select>` 换成 `div.chipset[role=group]` + checkbox ② 新增混排方式 radio 组 `#taskAssign` ③ 新增会话条 `#sessionBar`（下拉 + 新建 + 续跑按钮）④ 历史卡加 `#runOnlySession` 复选 ⑤ 汇总卡加 attempt 级表 `#statsAttempts` 与混排表 `#statsMix` ⑥ run 面板加 `#runAttempts`（attempt 明细） |
| `templates/index.html:2076-2145` | ① `#taskAgent`（`:2079`）/`#taskModel`（`:2080`）由单选 `<select>` 改成 `<select multiple>`，去掉 `onchange="onTaskAgentChange()"` 内联属性改走委托 ② 新增混排方式 radio 组 `#taskAssign`（radio 值非字符串实参，不触棘轮）③ 新增会话条 `#sessionBar`（下拉 + 新建 + 续跑按钮）④ 历史卡加 `#runOnlySession` 复选 ⑤ 汇总卡加 attempt 级表 `#statsAttempts` 与混排表 `#statsMix` ⑥ run 面板加 `#runAttempts`（attempt 明细） |
| `static/hub/06-manager-tasks.js:49-77` | `mountPicks` 扩展 `disabled`/`title` 渲染（见 §2.1）；`:426` 的挂载数组加 `taskAgent`/`taskModel`。**共用组件，改前 grep 全部调用点，对现有三源零行为变化** |
| `static/hub/04-terminal-ws.js`（行号已按实物校正） | ① `fillAgentSelect($('taskAgent'), true)`（`:3416`）→ 改为填 option 后 `mountPicks('taskAgent')`；**注意 `fillAgentSelect` 定义在 `01-core-boot.js:580`、还被 jobs 页 `:573` 用（`$('jobAgent')`）⇒ 不改其签名，只在协同页调用点后补挂载** ② `fillTaskModelSelect`（`:2993-3022`）→ 调 `/api/tasks/model-choices`（一次请求，不再逐 agent 拉 `/api/models`）③ `onTaskAgentChange`（`:3024`）→ 读 `selectedOptions` 数组 ④ `taskBodyFromUi`（`:3026-3032`）输出 `agents[]`/`models[]`/`assign_mode`/`session_id` ⑤ 新增 `loadSessions()`/`openSession()`/`rerunSession()`/`saveSessionDraft()` ⑥ `loadRuns`（`:3161-3205`）加"仅看本会话"过滤 ⑦ 新增 `loadRunAttempts()`/`renderAttemptTable()`，由 `openRun`（`:3207-3225`）与既有 `/events`（`:3277`）**同一轮并发**拉 ⑧ `loadTaskStats`（`:3295-3340`）→ 改调 `/api/collab/stats` 并渲染 attempt/mix 两表 ⑨ 所有新按钮走 `data-*` + 委托（沿用 `bindRunActions:3139-3159` 的幂等绑定模式） |
| ~~`static/hub/02-nav-and-poll.js`~~ | **不改**（原设计写错了文件）：协同页轮询真位置是 `06-manager-tasks.js:414-420` 的 6s `setInterval`；`grep -n "tasks\|runs\|openRun" static/hub/02-nav-and-poll.js` = **0 命中**（已实测）。新增拉取挂进 `openRun`，不新增轮询周期 |
| `static/hub.js` | **由 `bash scripts/build_hubjs.sh` 重建**，禁手改（闸门 `test_hubjs_split.py` 判逐字节相等） |

**表单草稿持久化**（需求②"方便后续查看和继续执行"的端侧一半）：`localStorage['collab.draft.<session_id>']` 存四框当前值，`enterTasksPage:3413-3430` 时回填。**localStorage 只是便利层，真值在 `collab_sessions`**——两处不一致时以服务端为准并提示（判例 316 §二"前端不存第二份真值"同族）。

---

## 7. 页面 ↔ 接口依赖关系

```
【进页 enterTasksPage】（顺序有依赖，现有注释 04-terminal-ws.js:3411-3413 已说明）
  1. GET /api/agents               → Agent chip 组（含 available/reason，不可用的标灰但可见）
  2. GET /api/tasks/pool           → rounds.choices/ceiling（轮次框唯一真值）+ escalate 配置
  3. GET /api/collab/sessions      → 会话下拉（新增）
  4. GET /api/tasks/runs           → 历史卡
  5. GET /api/collab/stats         → 汇总卡（新增；替代 loadTaskStats 的 /api/tasks/stats）

【选 Agent】chip change
  → GET /api/tasks/model-choices?agents=…   → 模型 chip 组（联动过滤，一次请求）
  → 混排方式 radio 可用性：race 需 budget_tokens，未填时该 radio 禁用并说明

【仅预览】btnPreview
  → POST /api/tasks/decompose {dry_run:true, create_session:false}
  → 渲染 tasks[] + assign_policy + model_policy.skipped_agents + round_choices 回填

【拆解并启动】btnDecompose
  → POST /api/tasks/decompose {auto_run:true, create_session:true|session_id}
  → openRun(run_id) + loadSessions() + loadRuns() + loadCollabStats()

【打开 run】openRun
  → GET /api/tasks/{run}          → 任务表 + DAG + runSummary（既有）
  → GET /api/tasks/{run}/chain    → 轮次链（既有）
  → GET /api/tasks/{run}/events   → 时间线（既有，读 profile_events）
  → GET /api/tasks/{run}/attempts → attempt 明细（新增，按 task 分组显示"第几次用什么组合"）
  ↑ 四路由 openRun（04-terminal-ws.js:3207）一次并发拉；6s 周期由 06-manager-tasks.js:414-420
    驱动，不新增 setInterval

【继续执行】两条路，语义不同，UI 上要能区分
  A. 同 run 续跑   → POST /api/tasks/{run}/start   （runner 掉线/暂停后接着跑，不花钱拆解）
  B. 重拆下一轮    → POST /api/tasks/{run}/replan  （闸门 replan_gate，真的调 LLM）
  C. 会话级续跑    → POST /api/collab/sessions/{id}/rerun（新增；= B + 用会话快照参数 + 归入同一 session）

【汇总卡筛选】statsDays / level / session_id change
  → GET /api/collab/stats?days=&level=&session_id=
【执行记录页筛选】agent/model/status/bucket/days
  → GET /api/collab/executions?…
```

**依赖倒置风险（必须在实现时挡住）**：模型框依赖 Agent 框（域判定），Agent 框依赖 `/api/agents` 与 `/api/tasks/pool` 两者都返回。任一接口失败时的降级：
- `/api/agents` 失败 ⇒ Agent chip 组空 + 明写"Agent 清单取不到：<原因>"，**不许退化成"无可用 Agent"**（同 `count=0` vs `degraded` 的口径纪律）。
- `/api/tasks/model-choices` 失败或 `checked:false` ⇒ 模型框只留「自动」，其余三框照常可用（拆解仍可跑，只是不能钉模型）。
- `/api/collab/stats` 失败 ⇒ 汇总卡显示错误，**不影响执行**（统计是只读增强面）。

---

## 8. 分期与非目标

**P0（本次）**：§2 多选控件 + §4.1 联动接口 + §3.1 会话表与 6 接口 + §3.2 attempt 表与 §4.4 两接口 + §4.5 汇总扩展 + §5.2 单点写入。

**P2（不在本次，需另立项）**：
1. `assign_mode=race`（同任务多组合并行）：`tasks` 表 PK `(run_id, task_id)` 一任务一行，装不下多组合 ⇒ 需要 PK 变更或影子 run 机制，属结构性改动，须先 `/grill-me`（AGENTS.md §方案拷问：改多项目共用配置结构 ⇒ 先拷问）。
2. 存量 12 处 inline onclick 带字符串实参（判例 316 §六未闭合，棘轮已挡新增）。
3. codex 执行者 600s 超时"报告已写盘、进程未退"是否算完成（判例 316 §六，需另判）。
4. run 依赖 hub 进程存活、重启不自动恢复（t1 清单 D 类缺口）。

**非目标（明确不做）**：
- 不改 `verifier` 判定逻辑与 `error_bucket` 规则（判例 316 刚修过 `_NOT_RUN_RE` 三个边界，动它要另配 6 条用例）。
- 不改 `_RUN_STATE_SQL` / `_RUN_AGG_SQL` 的既有语义（只加列）。
- 不改难度→档位映射 `tiercfg`（t1 清单 D1 缺口"映射无证据支持"是**数据不足**问题，不是代码问题，靠 `mix` 段积累证据后再议）。
- 不引入前端框架/构建器（现状无框架、原生 JS 分片拼接；引入 = 手机端全面回归，超出本次范围）。
- 不做多用户/权限（`created_by` 只记标识，不鉴权）。

---

## 9. 验收判据（实现方按此自证，缺一不算完成）

**L0 单测（新增 3 个文件，与 `tests/test_task_stats_l0.py` 同风格）**
1. `tests/test_collab_sessions_l0.py`：建/查/改/删/rerun；`session_id` 不存在 ⇒ 404；DELETE 不删 tasks；子 run 继承 session_id。
2. `tests/test_task_attempts_l0.py`：6 个出口各写一条且**只写一条**；升档不额外记 attempt；`error_bucket` 落库值与 `tasks_tiers.error_bucket()` 一致；写库失败不带倒子任务。
3. `tests/test_collab_stats_l0.py`：六条口径断言——`summary.runs == sum(rounds[].runs)`；同一 round 只出一行；`_days_ago` 带 `+00:00` 且包含同刻落库值；`n<10 ⇒ sample_insufficient`；`attempts.n >= tasks`；`attempts.pass >= success`。
4. 改 `tests/test_task_rounds_l0.py`：`agents`/`assign_mode` 入参的上限与白名单断言。
5. 改 `tests/test_dispatch_handoff.py`：页面窗口切 `</section>`（判例 316 §四已改过一次，新增卡要跟着调）。
6. **闸门自证**（判例 316 §七.5：闸门只证明它看得见的那一类）：给 `test_html_js_escape_depth.py` 注入一行违规必须变红；给新加的 6 条口径断言各注入一次错值必须变红。

**端到端（真跑，不是只看接口 200）**
7. `bash scripts/build_hubjs.sh` 后 `test_hubjs_split.py` 绿（逐字节相等）。
8. 一条真 run：`assign_mode=spread` + 2 个 Agent + 1 个钉死模型 + `rounds=1` ⇒ 验 `assign_policy.assignment` 真的分给了两家、回包 `model` 与钉死值**逐字一致**（判例 316 §五的取证方式）。
9. 一条故意失败的 run（选一个非 CCR 域 agent + 钉死模型）⇒ 验 `model_policy.skipped_agents` 点名、attempt 表记下 `error_bucket="模型不在册"`、汇总卡失败归因表出现该桶。
10. 会话续跑：对 §8 那条 run 走 `POST /api/collab/sessions/{id}/rerun` ⇒ 新 run 的 `session_id`/`parent_run_id`/`round+1` 正确，`run_count` 递增。
11. `bash scripts/prepush.sh` 全绿（⑥ 测试闸门 + ⑦ 本模块自检）。

**部署（硬约束）**
12. 重启必须走 `bash /fs/1000/ftp/技术文档/agenthub/scripts/hub-restart.sh --reason "协同混排重构"`。**禁 `systemctl --user restart agenthub`**——`KillMode=control-group` 会把 cgroup 内所有 codebuddy 进程与 pty 一起杀掉，用户侧表现为"频繁自动断开"（AGENTS.md 生产单元重启军规，2026-10-25 有 9 次这样的重启）。判据：`GET /health` 的 `term_sessions > 0` 时禁止 restart。
13. 改完 agenthub 代码后**先重启再过 prepush**：⑦ 指纹闸门拿运行进程的 boot sha 对磁盘代码，不重启就推送必被拦（`HUB_SKIP_SELFCHECK=1` 只算未验证）。
14. **页面渲染需端侧确认**：本机只能证到"接口返回正确 / 静态资源 200 / 标记齐全"；窄屏与 chip 组换行、表格列对齐按军规属端侧判据，须用户在手机与桌面各看一次（判例 316 §六同一条未闭合项）。

---

## 10. 实现顺序（每步一个可验判据，步数 = 判断点数）

1. **建表 + `_record_attempt()` 单点写入**（`tasks.py` / `tasks_runner.py`）→ 判据：跑一条真 run，`SELECT COUNT(*) FROM task_attempts` 与任务数对得上；L0 ①② 绿。
2. **`/api/collab/executions` + `/api/collab/stats`**（`collab_stats.py`）→ 判据：六条口径断言绿；attempt 级与 task 级两个成功率并列且差值有解释。
3. **`collab_sessions` 表 + 6 个会话接口**（`collab.py`）→ 判据：L0 ③ 绿；rerun 出的 run 归入同一 session。
4. **`DecomposeIn` 扩字段 + `_apply_spread()` + `/api/tasks/model-choices`**（`tasks_dag.py`）→ 判据：不传新字段时老调用方行为**逐字不变**（MCP `hub_dispatch` 与 `~/bin/hub-dispatch` 各跑一次对照）；`spread` 的分配结果可核。
5. **前端多选（复用 `mountPicks`）+ 会话条 + attempt/mix 两表**（`index.html` / `04-terminal-ws.js` / `06-manager-tasks.js`）→ 判据：`build_hubjs.sh` 后 split 闸门绿；escape 棘轮零新增（fixture 仍 13 条）；`mountPicks` 扩展后 `memFedSrcs`/`kbRoutes`/`instTo` 三源行为不变（各点一次验勾选与 `change` 派发）。
6. **重启 + 端到端 8~11 + 端侧确认 14**→ 判据：全绿才可报"完成"；任一步失败如实报失败点 + 影响面 + 回滚方案（回滚用时间戳备份，禁 `reset --hard`）。

> 步骤 1→2→3 有依赖（统计要读 attempt，会话要挂 run），4 可与 3 并行，5 依赖 4 的响应字段定型。**每步改文件前先做时间戳备份**（AGENTS.md 备份细则：`<名>.bak-$(date +%Y%m%d_%H%M%S)-<英文短说明>`，git 管理的文件也做）。

---

## 附：本文所有取证点（可复核）

| 断言 | 证据 |
|---|---|
| 四框已存在，Agent/模型为单选 | `templates/index.html:2076-2083`；`static/hub/04-terminal-ws.js:2950-3130` |
| 轮次档位由后端下发，前端不存第二份真值 | `tasks.py:230-249`（`/api/tasks/pool` 的 `rounds` 段）；`04-terminal-ws.js:2955-2957,2975-2986` |
| 模型钉死必须在分档之后 | `tasks_dag.py:277-319`（`_apply_pinned_model` 文档串）；`tasks_dag.py:361-363` 调用点注释 |
| CCR 域判定唯一真值 | `tasks_dag.py:249-274`；`tasks_tiers._ccr_model_agents()` |
| 升档覆盖 `tasks.agent_id` ⇒ 首次组合丢失 | `tasks_runner.py:147-173`（`_resolve_exec_target`，含 UPDATE 与"必须同步改写"注释） |
| 三列三口径不得合并 | `tasks.py:112-134`（`model_choice`/`model_planned`/`model`/`agent_promoted_from`/`escalated_from_*` 注释） |
| `combos`/`failures`/`rounds`/`verdict` 已存在 | `tasks.py:435-541` |
| 历史列表已支持搜索分页 | `tasks.py:379-418` |
| 时间窗必须 UTC + run 侧同窗 | `tasks.py:421-432`、`:457-462` 注释（实盘缺陷记录） |
| `GROUP BY` 同名歧义 | `tasks.py:518-525` 注释（实盘 1 组 3 条 / 1 组 35 条） |
| `stop_reason` 手动 run 恒 NULL，不回填 | `tasks.py:191`；`04-terminal-ws.js:3246-3262`；判例 316 §六 |
| inline onclick 闸门结构性失明 + 棘轮 | 判例 316 §五之二缺陷⑤；`tests/test_html_js_escape_depth.py`、`tests/fixtures/inline_onclick_string_argv.txt` |
| hub.js 是分片拼接产物，逐字节闸门 | `scripts/build_hubjs.sh` 头部注释；`tests/test_hubjs_split.py` |
| `chat_sessions` 语义不匹配（不复用） | `db.py:55-61`（`agent_id NOT NULL`） |
| `profile_events` 可部分还原但语义污染 | `db.py:98-111`；`tasks_runner.py:166-172,260-266,327-336,356-361,369-373,415-428` |
| `race` 受 PK 限制 | `tasks.py:137-166`（`PRIMARY KEY (run_id, task_id)`） |
| 重启军规与 `term_sessions` 判据 | AGENTS.md「生产单元重启军规（2026-10-25）」；判例 `agent-knowledge/266` |
| 判例 316 的 5 个假绿与 7 条复用判据 | `agent-knowledge/316-协同页四框改版-参数落库与统计口径三坑.md` |
| `mountPicks` 是既有 chip 多选组件，原生 select 为唯一数据源、幂等重挂 | `static/hub/06-manager-tasks.js:38-77`（含 v0.13.40 口径注释）；挂载点 `:423-426`；同写法读值 `04-terminal-ws.js:403`、`:2757` |
| `mountPicks` 不处理 `o.disabled`（需扩展） | `static/hub/06-manager-tasks.js:59-70`（`forEach` 内无 disabled 分支，已逐行核对） |
| 协同页轮询真位置 = 6s setInterval，不在 `02-nav-and-poll.js` | `static/hub/06-manager-tasks.js:414-420`；`grep -n "tasks\|runs\|openRun" static/hub/02-nav-and-poll.js` = 0 命中 |
| `fillAgentSelect` 被 jobs 页共用（不可改签名） | 定义 `static/hub/01-core-boot.js:580-586`；调用 `:573`（`$('jobAgent')`）与 `04-terminal-ws.js:3416`（`$('taskAgent')`） |
| 棘轮 fixture 实为 13 条（判例 316 写"12 处"） | `wc -l tests/fixtures/inline_onclick_string_argv.txt` = 15（2 行注释头 + 13 条） |
| `/api/tasks/stats` 闸门为 24 例 | `grep -cE "^\s*(async )?def test" tests/test_task_stats_l0.py` = 24 |
| 执行超时出口在 `asyncio.TimeoutError` 分支（"超时熔断 %ds"） | `tasks_runner.py:397,435-440`（含 2026-10-15 P0-a 的既存缺陷注记） |
