# 协同功能模块重构设计 · 会话身份 / 执行记录 / 统计口径

**日期**：2026-11-03｜**基线**：agenthub v0.13.189（`src/main.py:126`）｜**性质**：增量重构（四框已落地，本文只补数据面）
**取证环境**：`GET /health` → `db_ok:true`、`term_sessions:0`、`needs_restart:false`、`running_matches_head:true`、`code_fp_boot == code_fp_worktree == code_fp_head == 581e0dd09a7d`（运行代码 = HEAD，无需重启）
**上游**：`agent-knowledge/316-协同页四框改版-参数落库与统计口径三坑.md`｜现状清单：任务 t1 勘察
**兄弟文档**：`docs/COLLAB-MULTI-MIX-DESIGN-20261103.md`（661 行，同期同题）。**本文与它的关系见 §8，两处刻意不一致，不是笔误。**

---

## 0. 一句话结论

> **四框、`/api/tasks/{pool,runs,stats,chain,events}` 都已存在，本次真正要造的东西只有两个：`task_attempts` 表（因为升档会覆盖 `tasks.agent_id`，首次组合在库里已被物理销毁），以及一个能落地重拆链上的「会话」身份。会话不需要新表——`task_runs.parent_run_id` 已经是一棵树，会话就是树根。**

四条需求 → 三处口径决定：

| 需求 | 现状（已取证） | 本次唯一要做的事 | 为什么不能靠现有实现 |
|---|---|---|---|
| ① 前端四框 | 四框已在 `templates/index.html:2076-2083`；逻辑 `static/hub/04-terminal-ws.js:2980-3130` | Agent/模型改**多选** + 联动过滤接口 | Agent、模型都是单选 `<select>`（`:2079-2080`），`fillAgentSelect`（`01-core-boot.js:580-586`）只填单选；`_apply_pinned_model`（`tasks_dag.py:277`）签名只收**一个** `model` |
| ② 会话历史 / 继续执行 | run 级历史齐全：`GET /api/tasks/runs`（`tasks.py:380`）带 `q/state/agent/model/days/offset` + `total`；血缘列 `parent_run_id`/`child_run_id`（`tasks.py:181-200`）；链视图 `GET /{run_id}/chain`（`tasks.py:557`） | **会话身份定义 + 列表聚合**（会话表**可选**，见 §2.2） | 现在只有"一条 run 的祖先链"，没有"一件事的会话"。刷新页面即丢表单参数；同一需求多次重拆只能顺着链走，无法直接列出"我最近开过哪几件事" |
| ③ 执行记录（组合成败 + 原因） | `stats.combos` 是**查询时** `GROUP BY agent_id, model_eff`（`tasks.py:486-498`） | **新表 `task_attempts`**（append-only 单点写入） | 升档 `UPDATE tasks SET agent_id=?`（`tasks_runner.py:160-165`，注释明写"必须同步改写"）⇒ 库里只剩**最后一次**组合，首次失败的组合消失；`profile_events` 只留 30 天（`src/retention.py:45`）且 `detail` 是 TEXT JSON 无法建索引 |
| ④ 结果汇总 | `stats.summary` + `verdict`（`tasks.py:531-541`），`n<10` 已明写"样本不足" | 加 **attempt 维度** 与 **会话维度**；口径写死成断言 | `combos` 的 `rate` 是**任务级**，会把"升档后才过"读成"这个组合行"；两个口径不并列就必然被误读 |

**与兄弟文档的重叠**：§1 混排三语义、§3.1 `collab_sessions` DDL、§4.1 `/api/tasks/model-choices`、§4.3 会话接口——那些设计成立，本文不重抄。本文只写它没定的两件事：**会话身份从哪来**、**attempt 的粒度与统计分母怎么算**。

---

## 1. 前端交互流程

### 1.1 布局（改 `templates/index.html:2070-2145` 的 S3 协同卡，样式全在 `:45-1432` 的内联 `<style>`，仓库无 `.css`）

```
┌─ 协同 DAG ───────────────────────────────────────────────────────┐
│ ① 需求  [textarea#taskGoal .....................................] │
│                                                                   │
│ ② Agent（多选）                                                   │
│    [☑ claude-code] [☑ codex] [☐ pi] [☐ dsh] [☐ codebuddy] …      │
│    混排方式：(•)自动分配  ( )轮转混排  ( )并行竞速（需预算，P2）   │
│                                                                   │
│ ③ 模型（多选，随 ② 联动）                                          │
│    [☑ alibaba/deepseek-v4.1-flash ·免费] [☐ qwen3.8-max]         │
│    ⚠ dsh / codebuddy / cursor 不吃钉死模型（模型域非 CCR）        │
│                                                                   │
│ ④ 未完成任务循环轮次  [2 轮 · 跑完自动重拆 ▾]  ← 档位由后端下发   │
│                                                                   │
│ ⑤ [仅预览]  [拆解并启动]        提示行 #taskPreview               │
├─ 结果汇总 ───────────────────────────────────────────────────────┤
│ verdict 行 + 五数 + [会话维度切换]  ← 新增                        │
│ #statsCombos ｜ #statsFailures ｜ #statsRounds ｜ #statsAttempts ← 后两张新增 │
├─ 会话与历史 ─────────────────────────────────────────────────────┤
│ 搜索#runQ 状态#runState [☐ 仅看本会话]  ← 复选新增；分页 runsMore │
└──────────────────────────────────────────────────────────────────┘
```

### 1.2 多选控件的实现路线（**与兄弟文档相反，理由见 §8.1**）

用**隐藏的原生 `<select multiple>` + 芯片壳**，即仓库已有的 `mountPicks(selId)`（`static/hub/06-manager-tasks.js:48-79`），不新写 checkbox 组。三条理由，都可核：

1. **它是现有唯一的多选配方，已被复用 2 处**：`06-manager-tasks.js:426`（`memFedSrcs` / `kbRoutes`）、`04-terminal-ws.js:1037`（`instTo`）。新增一处 = 第三种多选写法。
2. **数据源唯一**：`mountPicks` 让原生 `select` 保持 `display:none`（`.picks-src`）、**仍然是唯一被读的值**，芯片只做"翻转 `option.selected` + `dispatchEvent(change)`"。`taskBodyFromUi`（`04-terminal-ws.js:3026`）因此可以从 `sel.selectedOptions` 读，不需要维护第二份数组——判例 316 §二"前端不存第二份真值"同一口径。
3. **幂等**：`mountPicks` 先删 `selId+'Picks'` 再重挂，Agent 清单每次重写 option 时不会叠壳（`04-terminal-ws.js:1037` 的注释明写"option 每次重写 ⇒ 芯片壳跟着重挂"）。

兄弟文档反对它的理由是"手机端多选体验差"——该顾虑成立，但 `mountPicks` **不是**原生多选弹层：原生 select 已被隐藏，用户点的是芯片（`<button>`），触控目标与 checkbox 同级。**"移动端体验差"这条反对意见打的是 `<select multiple>` 的原生弹层，而这套配方的全部意义就是绕开那个弹层。**

**代价（必须承认）**：`mountPicks` 目前没有"分项禁用态"，而模型框需要给非 CCR 域 agent 标灰。补法：在 `mountPicks` 里给 `option.disabled` 的项渲染 `aria-disabled="true"`、不绑 click、芯片加 `.picks-off`（一行 CSS，落在 `:929` 的 `.chip` 附近）。**这是对 `mountPicks` 的向后兼容增强——`option.disabled` 现有 3 个调用点都未使用，加分支不影响它们。**

**硬约束（沿用兄弟文档，两条都对）**：

- 新按钮走 `data-*` + 事件委托，**禁新增 `onclick="f('字符串')"`**。闸门 `tests/test_html_js_escape_depth.py` 对"实参含括号"结构性失明，存量 12 处已落棘轮 fixture `tests/fixtures/inline_onclick_string_argv.txt`（只判有没有**新增**）。
- 改的是**源分片** `static/hub/04-terminal-ws.js`，改完必须 `bash scripts/build_hubjs.sh` 重建 `static/hub.js`；`tests/test_hubjs_split.py` 判拼接结果与仓库 `hub.js` **逐字节相等**，忘 build 必红。

### 1.3 状态流转

```
idle ──改任一框──▶ dirty ──「仅预览」POST decompose{dry_run:true}──▶ previewed
  ▲                  │                                                   │
  │                  │「拆解并启动」直接执行（允许，提示"未预览直接执行"）│
  │                  └──────────────────┬────────────────────────────────┘
  │                                     ▼
  │                              running（POST decompose{auto_run:true}）
  │                          轮询 GET /api/tasks/{run_id}（既有 02-nav-and-poll.js）
  │                                     │
  │              ┌──────────────────────┼──────────────────────┐
  │              ▼                      ▼                      ▼
  │          success                 partial                  failed
  │              │                      │                      │
  └──────────────┴──── ①同 run 续跑 /start  ②重拆下一轮 /replan  ③会话续跑 /rerun
```

**四条状态判据**：

1. **`dirty` 必须可视化**：改框后未预览就执行 ⇒ 允许（执行自带拆解），提示行明写"未预览直接执行"。
2. **`running` 期间禁用全部参数框**，不只禁按钮。运行中改框会造成"屏上参数 ≠ 落库参数"。
3. **`stop_reason` 不伪造**：手动 run（`auto_rounds=0`）该字段恒 NULL，语义是"**重拆为什么停**"不是"run 成没成"。前端按任务状态推导文案（`04-terminal-ws.js:3228` 的 `RUN_STOP_TEXT` 分支已正确，沿用）。
4. **会话态与 run 态分开显示**：session 是"这件事还开着吗"，run 是"这一轮跑得怎样"（`_RUN_STATE_SQL`，`tasks.py:370-376`）。两个字段并列，不合并。

### 1.4 Agent ↔ 模型联动过滤

| 选中 Agent 集合 | 模型框内容 | 提示 |
|---|---|---|
| 空 | 只有「自动（按难度 t1/t2/t3）」 | 现有逻辑 `04-terminal-ws.js:2993-3020` 保留 |
| 全为 CCR 域 | 该域**并集**（去重、按 vendor 分组、标 `· 免费`） | 无 |
| 含非 CCR 域 | 并集，但该项芯片**标灰 + `option.disabled`** | "钉死模型对 dsh/codebuddy/cursor 不生效" |
| 清单取不到（`checked=false`） | 只有「自动」 | **"模型清单取不到：<原因>"**，不显示空清单冒充"没有模型" |

**域判定唯一真值** = `tasks_dag._allowed_models_for()`（`tasks_dag.py:249-274`）：`{"claude","jcode"} | {a for a,sp in cli_agent.SPECS.items() if sp["model_path"]=="ccr"}`，且 `checked=False` ⇒ 返回 `None`（判不了就不拦）。**前端不得另存一份域名单**——同"轮次档位由后端下发"的既有纪律（`04-terminal-ws.js:2980`）。

---

## 2. 会话（Session）数据模型与历史结构

### 2.1 问题定义：现在缺的到底是什么

现有能回答的问题：**"run X 是怎么来的"** —— `GET /api/tasks/{run_id}/chain`（`tasks.py:557`）顺着 `parent_run_id` 往上走。

现有**不能**回答的问题：**"我最近开过哪几件事，每件事现在什么状态"**。因为：

- 重拆链是 run **自带**的，不是显式实体 ⇒ 无法列出"所有链的根"。
- 表单参数（选了哪些 Agent、哪些模型、混排方式）只以**推导结果**形式散在 `tasks.agent_id` / `model_planned` 里，而 `agent_id` 会被升档覆盖（`tasks_runner.py:160-165`）、`model_planned` 只记在子任务行上 ⇒ **无法还原"我当时选了什么"**。
- 刷新页面全丢（`enterTasksPage`，`04-terminal-ws.js:3415`，只拉清单不拉草稿）。

### 2.2 会话身份 = 血缘根（**决策 D-1，与兄弟文档不同**）

```python
def session_key(run_id: str) -> str:
    """一条 run 所属会话的键 = 它血缘链的**根 run_id**。

    唯一实现处。不许在接口层各写一遍递归（两套必然漂移）。
    `task_runs.round` 与 `parent_run_id` 已保证链是线性的（`_maybe_replan` 单点建子 run）。
    """
```

**语义**：`parent_run_id IS NULL` 的 run 就是会话头；其所有后代（含孙代）同属该会话。会话 id **就是**根 run_id，不新发 uuid。

**为什么这样定，而不是新表 `collab_sessions`**（兄弟文档方案）：

| 维度 | 血缘根（本文） | 新表 `collab_sessions`（兄弟文档） |
|---|---|---|
| 存量数据 | **立刻可用**：现有 5 条 run（`e0f7de9912`/`2b997a1f15`/`58562e9e87`/`186cafc828`/`de785396e5`）自动各成一个会话，无需回填 | 存量 run `session_id` 全 NULL，会话页头几天是空的 |
| 漂移风险 | 零：会话归属**由已有列推导**，不可能与实际血缘不一致 | 高：`session_id` 与 `parent_run_id` 是两个独立事实，写漏一处就出现"run 在同一条链上但分属两个会话" |
| "用上次参数续跑" | 参数从**根 run 的拆解响应快照**取（需存，见 2.3） | 从 `collab_sessions.agents_json` 取 |
| 删会话 | 无此操作（会话是推导物，不存在可删的行） | 要定义 DELETE 语义，且 "删行不删 run" 会让 `session_id` 悬空 → 必须置 NULL（兄弟文档 §4.3 已意识到） |
| 一次会话里开**并行**多条 run | 不支持（会话 = 一条链） | 支持 |

**结论：P0 用血缘根。** 只有在需求④真的需要"同一会话下并行多条链"时才升到新表——而那是 `assign_mode=race` 的场景，兄弟文档自己已把它列 P2。**为一个 P2 场景在 P0 引入一张需要双向维护的表，是把漂移风险提前买进来。**

### 2.3 会话语义扩展：`task_runs` 加两列（幂等 ALTER，照 `tasks.py:200-211` 写法）

```sql
ALTER TABLE task_runs ADD COLUMN collab_json TEXT;      -- 四框参数快照（JSON）；NULL = 存量/非协同入口
ALTER TABLE task_runs ADD COLUMN session_title TEXT;    -- 会话标题；只在根 run 上有值
```

`collab_json` 结构（写入点唯一：`_create_run`，`tasks_dag.py:217`）：

```json
{
  "agents": ["claude-code", "codex"],
  "models": ["alibaba/deepseek-v4.1-flash"],
  "assign_mode": "spread",
  "rounds": 2,
  "verify_kind": "nonempty",
  "budget_ms": null,
  "budget_tokens": null
}
```

**子 run 继承**：`_replan_once` 建子 run 时 `collab_json` / `session_title` 直接复制父 run（与 `max_rounds` 已继承同一处、同一写法，`tasks_replan.py:225`）。子 run 的 `collab_json` 与父 run 不同（参数被覆盖）时也照实存 —— **但会话列表读的是根 run 的**，口径在 SQL 里一次写死，不在前端拼。

### 2.4 历史列表结构（`GET /api/tasks/sessions`，**不是** `/api/collab/sessions`）

为什么挂 `/api/tasks/` 下而不新开 `/api/collab/` 前缀：**路由歧义。** `/api/tasks/{run_id}`（`tasks.py:601`）是路径参数，任何 `/api/tasks/<字面量>` 都必须定义在它**之前**——现有 `/api/tasks/runs`（`:380`）、`/api/tasks/pool`（`:230`）、`/api/tasks/stats`（`:436`）、`/api/tasks/tiers`（`:251`）都靠这个顺序活着。再往这个前缀加字面量，是往已知雷区继续加雷。

**但兄弟文档的结论方向相反**（它为此另开 `/api/collab/`）。**本文采纳它的顾虑，给出不同解法**：新路由一律用**复数名词 + 定义在 `:601` 之前**，并在 `tests/` 加一条**路由顺序断言**（遍历 `router.routes`，断言所有 `/api/tasks/<字面量>` 的注册下标小于 `/api/tasks/{run_id}`）。**这是把隐式约定变成机械断言——比换个前缀更根本。**

```http
GET /api/tasks/sessions?limit=20&offset=0&q=&state=&agent=&model=&days=0
```

```json
{
  "sessions": [{
    "session_id": "186cafc828",
    "title": "协同重构",
    "goal": "重构协同模块并用混排测试验证",
    "root_run_id": "186cafc828",
    "current_run_id": "de785396e5",
    "run_count": 3,
    "round_max": 3,
    "state": "partial",
    "stop_reason": "same_failure",
    "collab": {"agents": ["claude-code","codex"], "models": ["alibaba/deepseek-v4.1-flash"],
               "assign_mode": "spread", "rounds": 2},
    "totals": {"tasks": 21, "success": 16, "failed": 4, "blocked": 1,
               "attempts": 24, "attempt_pass": 17,
               "duration_ms": 412000, "tokens": 183000},
    "created_at": "2026-11-03T01:37:00+00:00",
    "updated_at": "2026-11-03T02:22:11+00:00"
  }],
  "total": 1, "limit": 20, "offset": 0, "has_more": false,
  "filters": {"q": "", "state": "", "agent": "", "model": "", "days": 0}
}
```

**字段口径**：

| 字段 | 定义 | 唯一真值 |
|---|---|---|
| `session_id` | 血缘根 run_id | `session_key()` |
| `state` | 会话内**任一** run 为 `running` ⇒ running；否则取 `current_run_id` 的 state | 复用 `_RUN_STATE_SQL`（`tasks.py:370-376`），**不另写一份 CASE** |
| `stop_reason` | `current_run_id` 的 `stop_reason`（手动 run 为 NULL） | 直接读列，不回填 |
| `run_count` | 链上 run 数 | `COUNT(*)` over chain |
| `round_max` | 链上最大 `round` | `MAX(round)`，**表达式分组见 §4 口径 3** |
| `totals.attempts` | 链上 attempt 数 | `task_attempts`；存量 run 无 → 见 §3.4 的 `degraded` |

### 2.5 「继续执行」三条路，语义不同，UI 必须区分

| 入口 | 接口 | 花不花钱 | 新建 run |
|---|---|---|---|
| A 同 run 续跑 | `POST /api/tasks/{run_id}/start`（`tasks.py:616`） | 否 | 否 |
| B 重拆下一轮 | `POST /api/tasks/{run_id}/replan`（`tasks_replan.py:257`） | **是**（真的调 LLM） | 是，闸门 `replan_gate`（`:56`） |
| C 会话续跑 | `POST /api/tasks/sessions/{session_id}/rerun`（**新增**） | 是 | 是 |

C = B + 用根 run 的 `collab_json` 回填参数 + 显式指定父 run 为 `current_run_id`。**闸门复用 `replan_gate`，不在会话层另判一遍**（前端已有先例：`04-terminal-ws.js:3399` 注释"闸门在后端同一套 `replan_gate` —— 这里不做前置判断，只把结论如实转述"）。

```json
// 请求（全部可省；省 = 用根 run 的快照）
{"goal": null, "agents": null, "models": null, "assign_mode": null,
 "rounds": 1, "auto_run": true, "note": "人工再推一轮"}
// 响应
{"ok": true, "session_id": "186cafc828", "run_id": "9f8e7d6c5b",
 "round": 4, "parent_run_id": "de785396e5", "max_rounds": 2,
 "gate": {"go": true, "kind": "go"},
 "note": "已生成第 4 轮；轮次上限只允许收紧（传 5 也压回 2）"}
```

---

## 3. 执行记录（`task_attempts`）

### 3.1 为什么必须新表（三条取证，全部已回原文核对）

1. **升档物理覆盖**. `tasks_runner.py:160-165`：
   ```python
   db.execute("UPDATE tasks SET agent_id=?,escalated_from_agent=?,escalated_from_model=?,"
              "updated_at=? WHERE run_id=? AND task_id=?", ...)
   ```
   文档串原话："`agent_id` 必须同步改写，否则展示面与实际执行者不一致（v0.13.177 实弹）"。⇒ 覆盖是**有意设计，不许改**。于是 `stats.combos`（`tasks.py:486-498`）统计的是"最后组合"，首次组合只剩 `escalated_from_agent` 一个字段名，**它的模型、耗时、失败原因全部丢失**。
2. **`profile_events` 只能补充，不能当主表**. 保留期 30 天（`src/retention.py:45`）；`detail` 是 TEXT JSON ⇒ 每次统计要全表扫 + Python 解析，无法建索引；且 escalate 事件与 settle 事件的 agent 口径不一致。
3. **一条失败任务的真实形状是"两次换手"**. 首次 `codex + qwen` 超时 → 升档 `claude-code + deepseek` 通过。要回答"哪些组合成功/失败"，**必须把这两次分别记账**。`tasks` 表 PK `(run_id, task_id)` 一任务一行，物理上装不下。

### 3.2 DDL

```sql
CREATE TABLE IF NOT EXISTS task_attempts (
    attempt_id      TEXT PRIMARY KEY,       -- uuid4().hex[:10]，与 run_id 同形态
    run_id          TEXT NOT NULL,
    task_id         TEXT NOT NULL,
    attempt_no      INTEGER NOT NULL,       -- 本任务第几次执行（0 起）；**含升档**
    -- 组合四元组：这是"哪些组合成功/失败"的最小可判单元
    agent_id        TEXT,                   -- 本次**实际派发**的执行者（升档后 = 新值）
    model_requested TEXT,                   -- 我方请求值（= 当时的 model_planned or model_choice）
    model_observed  TEXT,                   -- adapter 回包实发值（网关可能改写）
    tier            TEXT,                   -- t1|t2|t3|explicit|native|NULL
    -- 结果
    status          TEXT NOT NULL,           -- pass|fail|exec_error|timeout|budget|parked
    verdict         TEXT,                   -- verifier 三态；NULL = 未走到验收（与 'fail' 语义不同）
    gap_ids         TEXT,                   -- verifier gaps 的 id 逗号串
    error           TEXT,                   -- 原文，截 500（与 tasks.error 同口径）
    error_bucket    TEXT,                   -- 落库而非查询时算：口径变了要能看出历史用哪版算的
    escalated_from_agent TEXT,              -- 本次是否升档而来（NULL = 没有）
    escalated_from_model TEXT,
    duration_ms     INTEGER,
    spent_tokens    INTEGER,
    started_at      TEXT,
    finished_at     TEXT,
    created_at      TEXT NOT NULL           -- UTC isoformat（+00:00）
);
CREATE INDEX IF NOT EXISTS idx_attempts_run     ON task_attempts(run_id, task_id, attempt_no);
CREATE INDEX IF NOT EXISTS idx_attempts_combo   ON task_attempts(agent_id, model_observed);
CREATE INDEX IF NOT EXISTS idx_attempts_created ON task_attempts(created_at);
```

**无 `session_id` 冗余列**（兄弟文档有）。理由：会话 id 由血缘根推导，冗余存一份就是第二个事实，重拆时写漏即漂移。代价是统计要 JOIN `task_runs` 一次——**那是本地 SQLite 的主键/索引 JOIN，成本远低于两套口径不一致的排查成本。**

### 3.3 写入：`_record_attempt()` 单点

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

    为什么必须单点：`tasks_runner` 有多个执行出口，各写一遍必然漂移——同一文件里
    已经判过一次（`_schedule_exec_retry` 文档串：2026-10-29 只给 verifier 失败写了
    重试，执行失败三处全漏）。
    **写库失败不得带倒子任务**（try/except + log），与检索/技能注入同口径。
    """
```

**六个调用点**（缺一即"某类失败在汇总里不存在"）：

| # | 出口 | 位置 | `status` |
|---|---|---|---|
| 1 | verifier 判过 | `tasks_runner.py` pass 分支 | `pass` |
| 2 | verifier 判不过/证据不足 | 同文件 fail 分支 | `fail` |
| 3 | 执行者自称失败 | `_settle_self_failure`（`:206`） | `exec_error` |
| 4 | 执行超时 | 超时分支 | `timeout` |
| 5 | 任意异常 | except 分支 | `exec_error` |
| 6 | 预算耗尽 / parked | `:415-420`（`budget_exceeded`） | `budget` / `parked` |

**升档本身不记 attempt**。`_resolve_exec_target`（`:147-173`）只负责返回新的 `(agent_id, model)`；下一次 settle 时带 `escalated_from` 落一条。**否则一次重试出两条 attempt，"尝试次数"虚增一倍，且 `attempt_no` 失去"第几次执行"的含义。**

`attempt_no` 口径：`SELECT COALESCE(MAX(attempt_no)+1, 0) FROM task_attempts WHERE run_id=? AND task_id=?`。**不复用 `tasks.attempts`**（那是 `MAX_AUTO_RETRY` 的重试计数，与升档是两套计数；混用会让 `attempt_no` 跳号）。

### 3.4 存量不回填

老 run 的 attempt 无法可靠复原（`tasks.agent_id` 已被覆盖、`profile_events` 的 agent 语义已污染）。接口对无 attempt 的 run **明写"存量 run 无 attempt 记录（本次起才有）"**，返回在 `degraded` 段：

```json
"degraded": {"legacy_runs_without_attempts": 7,
             "note": "7 个存量 run 无 attempt 记录（本次起才有），不计入本页分子分母"}
```

`degraded` 段**必须始终存在**（哪怕为 0）：把"真没有"与"取不到"分开。用 tasks 行伪造一条 attempt = 回填 = 伪造。

---

## 4. 结果汇总统计口径（八条，全部落成断言）

### 4.1 八条口径

1. **时间窗同源**：run 侧、task 侧、attempt 侧**都必须**过同一个窗口。`_days_ago`（`tasks.py:421`）必须带 `+00:00`（已修，复用不改）。破了的实盘形态 = "run 15 个"与"轮次表合计 14 个 run"同屏（判例 316 §三.③）。
2. **三把分母，永不互算**：
   - `attempts.n` —— 分母是**执行次数**。回答"这个组合好不好"。
   - `summary.tasks` —— 分母是**任务数**。回答"事情做完没有"。
   - `summary.runs` —— 分母是**run 数**。回答"这一轮整体绿没绿"。
   三者并列报，**任何比率必须带自己的 n**。
3. **`GROUP BY` 写表达式，不写同名别名**：`GROUP BY COALESCE(r.round,1)`，不能写 `GROUP BY round`。SQLite 同名时优先绑源列 `r.round`，于是"无 `task_runs` 行的存量 run（NULL）"与"round=1"分成两组、两行都经 COALESCE 显示成"第 1 轮"（2026-11-03 实盘：1 组 3 条 / 1 组 35 条）。**这条已在 `tasks.py:520-527` 的注释里写死，新查询照抄。**
4. **`model_eff` 观测值优先**：`COALESCE(NULLIF(model,''), NULLIF(model_planned,''), NULLIF(model_choice,''), '')`。三列三口径不许合并（`tasks.py:116-118`："合成一列就再也分不清「选了没生效」与「选了但网关换了」——v0.13.167 的病根"）。**attempt 表沿用同一分法**：`model_requested` / `model_observed`。
5. **失败归因只在 attempt 级算，且判据写死**：一条 attempt 计入失败归因，当且仅当 `status IN ('fail','exec_error','timeout','budget')` 且 `error_bucket` 非空。**`verdict IS NULL` ≠ fail**（`tasks.py:152-156` 注释：NULL = 未走到验收，与 'fail' 语义不同）。归因函数 `error_bucket()`（`tasks_tiers.py:147`）**不返回"其他"**，不改。
6. **`stop_reason` 不参与成败判定**：手动 run 恒 NULL，语义是"重拆为什么停"。展示文案由任务状态推导，**不回填该列**。
7. **`n < 10` 不给结论**：比率照算但标 `sample_insufficient:true`，verdict 明写"样本不足（N 条）：不给结论，继续观察"。5 条样本的比率是噪声。
8. **组合级必须双率并列**：`rate`（任务级）+ `attempts_rate`（attempt 级）+ `rate_gap_note`。只报任务级会掩盖"升档后才过"；只报 attempt 级会把重试成本读成能力差。

### 4.2 可机械化的断言（写进 `tests/test_collab_stats_l0.py`）

| # | 断言 | 对应口径 | 破了会怎样 |
|---|---|---|---|
| A1 | `summary.runs == sum(rounds[].runs)` | 1 | 同屏两个 run 数 |
| A2 | 每个 `round` 在 `rounds[]` 里只出一行 | 3 | 两行都显示"第 1 轮" |
| A3 | `_days_ago(d)` 落库值能被 `_win()` 自身包含 | 1 | 最新 run 永远不出现在自己的窗口里 |
| A4 | `attempts.n >= summary.tasks`（窗口内） | 2 | 有任务没记账 ⇒ 某个出口漏写 |
| A5 | `attempts.pass >= summary.success` | 2 | task 说成功但没有任何一次尝试成功 ⇒ 状态机漏 |
| A6 | 每条 `status='success'` 的 task 至少有一条 `status='pass'` 的 attempt | 2/5 | 成功无证据 |
| A7 | `n < 10` 时 `sample_insufficient == true` 且 `verdict` 含"样本不足" | 7 | 5 条样本当结论 |
| A8 | `degraded` 键在 `/executions` 与 `/sessions` 响应里**始终存在** | 3.4 | "真没有"与"取不到"混淆 |
| A9 | 所有 `/api/tasks/<字面量>` 路由的注册下标 < `/api/tasks/{run_id}`（`:601`） | §2.4 | 字面量路由被路径参数吞掉 |

**A4/A5/A6 是这三张表存在意义的守门人**：它们一旦为假，说明"六出口单点写入"有漏，而这类漏**在界面上表现为"某类失败在汇总里不存在"**——正是判例 316 §六的未闭合项。

### 4.3 `/api/tasks/stats` 的扩展（不新开 `/api/collab/stats`）

在既有响应上**加三段**，既有四段（`summary/combos/failures/rounds`）语义**一字不改**——`tasks.py:436` 的 `task_stats` 已有 24 例闸门（`tests/test_task_stats_l0.py`）覆盖，另立端点等于把口径分叉成两份。

```json
{
  "ok": true, "days": 30, "window_from": "2026-10-04T02:22:00+00:00",
  "summary": {"…既有保持不变…", "sessions": 4, "sessions_open": 2},
  "attempts": {
    "n": 71, "pass": 52, "fail": 15, "timeout": 3, "exec_error": 1,
    "pass_rate": 0.732, "task_pass_rate": 0.774,
    "rate_gap_note": "attempt 级 73.2% < task 级 77.4%：9 条任务靠重试/升档才过",
    "escalated": 9, "first_pass": 43
  },
  "combos": [{
    "agent_id": "codex", "model_eff": "alibaba/qwen3.8-flash",
    "n": 11, "ok": 9, "failed": 2, "rate": 0.818, "avg_ms": 21000,
    "attempts_n": 13, "attempts_pass": 9, "attempts_rate": 0.692,
    "top_buckets": [{"bucket": "超时", "n": 2}],
    "sample_insufficient": false
  }],
  "mix": {
    "assign_modes": {"auto": 9, "spread": 5, "race": 0},
    "pool_sizes": {"1": 4, "2": 8, "3": 2},
    "pinned": {"n": 6, "applied": 31, "skipped_agents": {"dsh": 4},
               "model_mismatch": 1,
               "note": "6 条 run 钉死模型；1 条回包模型 ≠ 请求值（网关改写，选型未生效）"},
    "verdict": "样本不足（5 条 spread run）：不给'混排优于自动'的结论，继续观察"
  },
  "verdict": "成功率 40.0%（6/15 个 run 全绿）；62 条任务里 11 条失败"
}
```

`mix.pinned.model_mismatch` 复用 `/api/tasks/tiers` 已有的同口径查询（`tasks.py:296-300`：`model_planned <> model` 即网关改写）——"档位到底生效没有"这个数不报，混排结论就是自我感觉良好。

### 4.4 执行记录查询接口

```http
GET /api/tasks/attempts?session_id=&run_id=&agent=&model=&status=&bucket=&days=30&limit=50&offset=0
```

```json
{
  "attempts": [{
    "attempt_id": "7c1d4a9b2e", "run_id": "2b997a1f15", "task_id": "t2", "attempt_no": 0,
    "session_id": "2b997a1f15", "round": 1,
    "agent_id": "codex", "model_requested": "alibaba/qwen3.8-flash",
    "model_observed": "alibaba/qwen3.8-flash", "tier": "t2",
    "status": "timeout", "verdict": null, "gap_ids": "",
    "error": "执行超时 600s（进程未退）", "error_bucket": "超时",
    "escalated_from_agent": null, "escalated_from_model": null,
    "duration_ms": 600012, "spent_tokens": 0,
    "started_at": "2026-11-03T02:10:00+00:00", "finished_at": "2026-11-03T02:20:00+00:00",
    "task_excerpt": "核验闸门正则对含括号实参…"
  }],
  "total": 24, "limit": 50, "offset": 0, "has_more": false,
  "window_from": "2026-10-04T02:22:00+00:00",
  "degraded": {"legacy_runs_without_attempts": 7, "note": "…"}
}
```

外加 run 面板用的 `GET /api/tasks/{run_id}/attempts`，返回 `{run_id, attempts:[…], by_task:{t1:[…]}, legacy:false}`，`by_task` 让界面直接渲染"第几次用什么组合"。

---

## 5. 文件改动清单

| 文件 | 改动 | 类型 |
|---|---|---|
| `src/tasks.py` | ① `SCHEMA_TASKS` 加 `task_attempts` ② `ensure_schema`（`:200`）加 `collab_json`/`session_title` 两列幂等 ALTER ③ 新增 `GET /api/tasks/sessions`、`/api/tasks/attempts`、`/api/tasks/{run_id}/attempts`（**全部定义在 `:601` 之前**）④ `session_key()` 唯一实现 ⑤ `task_stats`（`:436`）尾部加 `attempts`/`mix` 两段、`combos` 加 `attempts_*` 三键 ⑥ 重导出 `_record_attempt`/`session_key` | 改 |
| `src/tasks_dag.py` | ① `DecomposeIn`（`:25`）加 5 字段（`agents`/`models`/`assign_mode`/`session_id`/`session_title`），全部可选 ② 新增 `_restrict_pool()`（白名单校验）、`_apply_spread()`（**在 `_plan_tiers` 之后、`_apply_pinned_model` 之前**，`tasks_dag.py:361-363` 的顺序注释已说明原因）③ `decompose`（`:332`）接线 + `assign_policy`/`collab` 两段响应 ④ `_create_run`（`:217`）写 `collab_json`/`session_title` | 改 |
| `src/tasks_attempts.py` | **新文件**：`_record_attempt()` 唯一写入点 + attempt 查询的两个 SQL。为什么独立成文件：`tasks.py` 已 746 行、`tasks_runner.py` 已 599 行，且它有独立闸门与独立口径断言 | 新增 |
| `src/tasks_runner.py` | 六出口调用 `_record_attempt`（§3.3 表）；`_resolve_exec_target`（`:147`）返回 `model_requested` 供落库 | 改 |
| `src/tasks_replan.py` | `_replan_once` 子 run 继承 `collab_json`/`session_title`（与 `max_rounds` 同一处） | 改 |
| `src/hubmcp.py` | `hub_dispatch`（`:722`）加 `agents`/`assign_mode`/`session_id` 形参——UI 与 MCP 两套参数就是链路漏项 | 改 |
| `src/main.py` | `VERSION`（`:126`）升版；`include_router` 无需新增（`tasks_attempts` 用同一 `router` 挂载或单独挂） | 改 |
| `templates/index.html` | ① `:2079-2080` 两个 `<select>` 改 `<select multiple>` + 挂 `mountPicks` ② 新增混排方式 radio 组 `#taskAssign` ③ 新增会话条 `#sessionBar` ④ 历史卡加 `#runOnlySession` ⑤ 汇总卡加 `#statsAttempts`/`#statsMix` ⑥ run 面板加 `#runAttempts` ⑦ `.picks-off` 一条 CSS（落在 `:929` 的 `.chip` 附近） | 改 |
| `static/hub/06-manager-tasks.js` | `mountPicks`（`:48`）加 `option.disabled` 分支（`aria-disabled` + `.picks-off` + 不绑 click）——向后兼容，现有 3 个调用点均未用 disabled | 改 |
| `static/hub/04-terminal-ws.js` | ① `fillAgentSelect` 改 `fillAgentMulti`（**`01-core-boot.js:580` 的原函数被别处复用，改签名前先 grep 调用点，或在此另写**）② `fillTaskModelSelect`（`:2993`）改调 `/api/tasks/model-choices`（一次请求，不再逐 agent 拉）③ `taskBodyFromUi`（`:3026`）输出 `agents[]`/`models[]`/`assign_mode`/`session_id` ④ `onTaskAgentChange`（`:3024`）触发联动重拉 ⑤ 新增 `loadSessions`/`openSession`/`rerunSession` ⑥ `loadRuns`（`:3161`）加"仅看本会话" ⑦ 新增 `loadRunAttempts` ⑧ `loadTaskStats`（`:3295`）渲染 attempt/mix 两表 ⑨ 新按钮走 `data-*` + 委托（照 `bindRunActions` 的幂等绑定） | 改 |
| `static/hub/01-core-boot.js` | 仅当确认 `fillAgentSelect` 无外部调用方时才改；否则不动 | 改/不动 |
| `static/hub.js` | **由 `bash scripts/build_hubjs.sh` 重建**，禁手改（`test_hubjs_split.py` 判逐字节相等） | 生成 |
| `~/bin/hub-dispatch` | bash CLI 加 `--agents`/`--assign`（与 MCP 同口径） | 改 |
| `tests/test_collab_stats_l0.py` | 新增：A1–A8 八条断言 | 新增 |
| `tests/test_task_attempts_l0.py` | 新增：六出口各写且只写一条；升档不额外记；`attempt_no` 不跳号；写库失败不带倒子任务 | 新增 |
| `tests/test_route_order_l0.py` | 新增：A9 路由顺序断言 | 新增 |
| `tests/test_task_stats_l0.py` | 改：既有四段语义未变的回归（加断言而非改断言） | 改 |

---

## 6. 页面 ↔ 接口依赖关系

```
【进页 enterTasksPage（04-terminal-ws.js:3415）】—— 顺序有依赖
  1. GET /api/agents（api_agents.py:58）        → Agent 芯片组（available/reason，不可用标灰）
  2. GET /api/tasks/pool（tasks.py:230）        → rounds.choices/ceiling（轮次框唯一真值）+ escalate 配置
  3. GET /api/tasks/sessions（新增）            → 会话下拉
  4. GET /api/tasks/runs（tasks.py:380）        → 历史卡
  5. GET /api/tasks/stats（tasks.py:436）       → 汇总卡（含新增 attempts/mix 两段）

【选 Agent】芯片 change
  → GET /api/tasks/model-choices?agents=…     → 模型芯片组（联动过滤，一次请求）
  → 混排 radio 可用性：race 需 budget_tokens，未填则禁用并说明

【仅预览】#btnPreview
  → POST /api/tasks/decompose {dry_run:true}
  → 渲染 tasks[] + assign_policy + model_policy.skipped_agents + round_choices 回填
  ⚠ dry_run 不落 collab_json（预览不得产生可续跑的会话）

【拆解并启动】#btnDecompose
  → POST /api/tasks/decompose {auto_run:true}
  → openRun(run_id) + loadSessions() + loadRuns() + loadTaskStats()

【打开 run】openRun
  → GET /api/tasks/{run_id}            → 任务表 + DAG（既有）
  → GET /api/tasks/{run_id}/chain      → 轮次链（既有）
  → GET /api/tasks/{run_id}/events     → 时间线（既有，读 profile_events）
  → GET /api/tasks/{run_id}/attempts   → attempt 明细（新增）

【继续执行】三条路，UI 必须能区分（§2.5）

【汇总卡筛选】statsDays / session_id change → GET /api/tasks/stats?days=&session_id=
【执行记录筛选】agent/model/status/bucket/days → GET /api/tasks/attempts?…
```

**降级（必须挡住的依赖倒置）**：

- `/api/agents` 失败 ⇒ Agent 芯片组空 + 明写"Agent 清单取不到：<原因>"，**不许退化成"无可用 Agent"**。
- `/api/tasks/model-choices` 失败或 `checked:false` ⇒ 模型框只留「自动」，其余三框照常可用（拆解仍能跑）。
- `task_attempts` 查询失败 ⇒ 汇总卡的 attempt 段显示错误，**既有四段照常显示**（统计是只读增强面，不得因新段挂掉旧段）。

---

## 7. 分期与非目标

**P0（本次）**：§1 两个框改多选 + `/api/tasks/model-choices`；§2 `task_runs` 两列 + `session_key()` + 三个会话接口；§3 `task_attempts` 表 + 六出口单点写入 + 两个查询接口；§4 `stats` 加两段 + 八条口径断言。

**P1（下一轮，不在本次）**：
1. `assign_mode=spread`（轮转混排）——需要 `_apply_spread()` 与 `assign_policy` 响应段。
2. 表单草稿 `localStorage['collab.draft.<session_id>']`（**只是便利层，真值在 `collab_json`**，两处不一致以服务端为准并提示）。

**P2（需另立项）**：
1. `assign_mode=race`：`tasks` 表 PK `(run_id, task_id)` 一任务一行，装不下多组合 ⇒ 需 PK 变更或影子 run，属结构性改动，须先 `/grill`（AGENTS.md：改共用配置结构 ⇒ 先拷问）。
2. 升级到 `collab_sessions` 实体表（当且仅当"一会话并行多链"成为真实需求）。
3. 存量 12 处 `onclick` 带字符串实参（棘轮已挡新增）。
4. codex 600s 超时"报告已写盘、进程未退"算不算完成（判例 316 §六未闭合）。

**非目标**：
- 不改 `verifier` 判定与 `error_bucket` 规则。
- 不改 `_RUN_STATE_SQL` / `_RUN_AGG_SQL` / `task_stats` 既有四段的**语义**（只加段、只加列）。
- 不改 `tiercfg` 的难度→档位映射（t1 清单 D1"映射无证据"是数据不足，靠 `mix` 段积累后再议）。
- 不引入前端框架/构建器（现状原生 JS 分片拼接，引入 = 手机端全面回归）。
- 不做多用户/权限。

---

## 8. 与 `COLLAB-MULTI-MIX-DESIGN-20261103.md` 的两处刻意差异

两文档同题同期，**必须有一份说清"以谁为准"**，否则就是判例 316 §三.①"两个口径漂移"的成因族。本节是裁决表。

### 8.1 多选控件：`<select multiple>` + `mountPicks`（本文）vs `div[role=group]` + checkbox（兄弟）

**采纳兄弟的理由，否决其结论。** 它反对的是"原生多选弹层在手机上难用"——成立；但 `mountPicks`（`06-manager-tasks.js:48-79`）的全部意义就是**把原生 select 隐藏（`.picks-src{display:none}`）、只让芯片可见**，用户从不接触弹层。

三条本文更硬的判据：
1. **不新增第三种多选写法**：现有 3 个调用点（`06-manager-tasks.js:426` ×2、`04-terminal-ws.js:1037`）。
2. **数据源唯一**：原生 select 仍是唯一被读的值，避免"芯片数组 vs select 值"两份真值。
3. **它已有的缺陷本文承认并补**：`option.disabled` 分项禁用态，一行 CSS + 一个分支，向后兼容。

**若实现方实测手机端芯片换行/触控确实不可用**，则以实测为准改用 checkbox 组——**但那要先有端侧截图，不能靠推断。**

### 8.2 会话身份：血缘根（本文）vs 新表 `collab_sessions`（兄弟）

见 §2.2 对照表。核心分歧一句话：**兄弟方案给会话发一个新 id，本文认为会话 id 应该就是它血缘根的 run_id。**

裁决依据是"存量可用性 + 漂移面"：兄弟方案上线首日会话页是空的（存量 run 无 `session_id`），且 `session_id` 与 `parent_run_id` 成为两个必须同步维护的事实。本文方案零回填、零同步。

**兄弟方案支持而本文不支持的能力**：同一会话下并行多条 run（`race` 的天然载体）。该能力本文列 P2，兄弟也列 P2 —— **为一个 P2 能力在 P0 加一张双向维护的表，不划算。**

### 8.3 本文**直接采纳**兄弟文档的部分（不重写）

- §1 混排三语义（`auto`/`spread`/`race`）与三条硬约束（race 需显式点头 + 强制预算、不得绕过模型域判定、auto 是现状严格超集）。
- `/api/tasks/model-choices` 的响应形状与三态区分（含 `pinnable_agents`/`skipped_agents`）。
- `DecomposeIn` 五个新字段的校验清单（白名单 400 点名、`checked=false` 不拦、`session_id` 不存在 404、`models>1 && auto ⇒ 400`）。
- `_record_attempt()` "写库失败不带倒子任务"的纪律，与"升档不单独记 attempt"的判据。
- 六条既有统计口径（本文扩到八条，前六条与它一致）。
- 验收里"重启必须走 `scripts/hub-restart.sh`，禁 `systemctl --user restart`"。

### 8.4 路由前缀分歧

兄弟开 `/api/collab/` 前缀（理由：避 `/api/tasks/{run_id}` 歧义）。本文用 `/api/tasks/<复数名词>` + **路由顺序断言 A9**（§2.4）。

裁决：**两者都能解决歧义，但断言比换前缀更根本**——现有 `/api/tasks/runs|pool|stats|tiers` 四个字面量**已经**依赖定义顺序活着，这个隐式约定现在是无人看守的。**换前缀只是绕开雷区，加断言是排雷。** 若实现方认为断言难写，可退回兄弟方案，但**必须二选一，不许两套前缀并存**。

---

## 9. 验收判据（缺一不算完成）

**L0 单测**
1. `tests/test_task_attempts_l0.py`：六出口各写**且只写一条**；升档不额外记 attempt；`attempt_no` 不跳号；写库失败不带倒子任务。
2. `tests/test_collab_stats_l0.py`：A1–A8 八条断言全绿。
3. `tests/test_route_order_l0.py`：A9 路由顺序。
4. `tests/test_task_stats_l0.py`：**既有 24 例不改断言**，新增段各自独立断言（证明旧口径未被碰）。
5. **闸门自证**（判例 316 §七.5：闸门只证明它看得见的那一类）：向 A1 与 A4 各注入一次错值，必须变红。

**端到端（真跑，不是只看 200）**
6. `bash scripts/build_hubjs.sh` 后 `tests/test_hubjs_split.py` 绿（逐字节相等）。
7. 一条真 run 跑完 ⇒ `SELECT COUNT(*) FROM task_attempts WHERE run_id=?` **≥** 该 run 的任务数；每条 `status='success'` 的 task 都有对应 `pass` attempt。
8. 一条**故意失败并升档**的 run ⇒ attempt 表出现两条同 `task_id`、`attempt_no` 连续、`agent_id` 不同的行；第二次带 `escalated_from_agent`；`stats.combos` 里两个组合各占一行。
9. 会话：对同一条链连跑两次 replan ⇒ `GET /api/tasks/sessions` 里**只增加 0 个会话**、该会话 `run_count` +2（会话是血缘根，不会因为多一轮而多一条会话）。
10. 存量 run ⇒ `/attempts` 返回 `legacy:true` + `degraded` 段，且**不伪造** attempt 行。
11. `bash scripts/prepush.sh` 全绿。

**部署（硬约束）**
12. 重启必须走 `bash /fs/1000/ftp/技术文档/agenthub/scripts/hub-restart.sh --reason "…"`。**禁 `systemctl --user restart agenthub`**——`KillMode=control-group` 会把 cgroup 内所有 codebuddy 与 pty 一起杀掉（AGENTS.md 生产单元重启军规）。`term_sessions > 0` 时禁止 restart。
13. 改完**先重启再过 prepush**：指纹闸门拿运行进程 boot sha 对磁盘代码，不重启就推送必被拦（`HUB_SKIP_SELFCHECK=1` 只算未验证）。
14. **页面渲染需端侧确认**：本机只能证到"接口返回正确 / 静态资源 200 / 标记齐全"；芯片组窄屏换行、表格列对齐须用户在手机与桌面各看一次（判例 316 §六同一未闭合项）。

---

## 10. 实现顺序（步数 = 判断点数）

1. **建 `task_attempts` + `_record_attempt()` 六出口**（`tasks_attempts.py` / `tasks_runner.py`）→ 判据：跑一条真 run，`task_attempts` 行数 ≥ 任务数，A4/A5/A6 绿。
2. **`tasks.py` 三个新查询接口 + `task_stats` 加两段** → 判据：A1–A3、A7、A8 绿；既有 24 例不改断言仍绿。
3. **`session_key()` + 三个会话接口 + 两列 ALTER** → 判据：判据 9/10 绿（连跑 replan 不增会话；存量标 legacy）。
4. **`DecomposeIn` 扩字段 + `/api/tasks/model-choices`** → 判据：不传新字段时老调用方**逐字不变**（MCP `hub_dispatch` 与 `~/bin/hub-dispatch` 各跑一次对照）。
5. **前端两框改多选 + `mountPicks` 禁用态 + build** → 判据：`test_hubjs_split.py` 绿；手机端截图确认。
6. **`scripts/prepush.sh` 全绿 → `hub-restart.sh` 重启 → `/health` 的 `code_fp_boot == code_fp_head`** → 判据：三项指纹相等且 `needs_restart:false`。

---

## 附：本文取证点（可复核）

| 主张 | 位置 |
|---|---|
| 四框已在 | `templates/index.html:2076-2083` |
| Agent/模型是单选 | `templates/index.html:2079-2080`；`static/hub/01-core-boot.js:580-586` |
| 单模型钉死只收一个 | `src/tasks_dag.py:277`（`_apply_pinned_model(items, model, allowed)`） |
| 升档覆盖 `agent_id` | `src/tasks_runner.py:160-165`（含"必须同步改写"原文） |
| `combos` 是查询时聚合 | `src/tasks.py:486-498` |
| `profile_events` 保留 30 天 | `src/retention.py:45` |
| `_RUN_STATE_SQL` 唯一定义 | `src/tasks.py:370-376` |
| `_RUN_AGG_SQL` | `src/tasks.py:345` |
| `GROUP BY` 表达式铁律 | `src/tasks.py:520-527`（注释，含 1 组 3 条 / 1 组 35 条实盘） |
| `_days_ago` UTC | `src/tasks.py:421` |
| `error_bucket` 不返回"其他" | `src/tasks_tiers.py:147` |
| `TASK_STATUSES` | `src/tasks_tiers.py:49` |
| `mountPicks` 幂等 + 隐藏原生 select | `static/hub/06-manager-tasks.js:48-79` |
| `mountPicks` 现有调用点 | `06-manager-tasks.js:426`；`04-terminal-ws.js:1037` |
| `.chip` / `.sp-card` 样式（无 .css 文件） | `templates/index.html:45-1432`，`.chip` 在 `:929`，`.sp-card` 在 `:655` |
| `replan_gate` | `src/tasks_replan.py:56`；路由 `:257`（经 `tasks.py:74` 挂载） |
| 前端不预判闸门 | `static/hub/04-terminal-ws.js:3399` |
| `RUN_STOP_TEXT` | `static/hub/04-terminal-ws.js:3228` |
| `hub_dispatch` 现有形参 | `src/hubmcp.py:722` |
| `dispatchable_ids` | `src/registry.py:64` |
| `/api/agents` | `src/api_agents.py:58` |
| `/api/models` 一次只问一个 agent | `src/api_models.py:18-20` |
| `_apply_pinned_model` 必须在档位层之后 | `src/tasks_dag.py:361-363` |
| `_ccr_model_agents` 域判定 | `src/tasks_dag.py:249-274`（`_allowed_models_for`） |
| `chat_sessions` 是聊天实体（`agent_id NOT NULL`） | `src/db.py:54-61` |
| `ensure_schema` 幂等 ALTER 写法 | `src/tasks.py:200-211` |
| 闸门文件均存在 | `tests/test_hubjs_split.py`、`tests/test_task_stats_l0.py`、`tests/test_html_js_escape_depth.py`、`tests/fixtures/inline_onclick_string_argv.txt`、`scripts/build_hubjs.sh`、`scripts/prepush.sh`、`scripts/hub-restart.sh` |
| 现网 5 条 run | `GET /api/tasks/runs?days=1`：`e0f7de9912`/`2b997a1f15`/`58562e9e87`/`186cafc828`/`de785396e5` |