# AI Agent Handbook 方法论采纳 · P0 立项施工书

> **新建文件登记**（依工作区「文件修改铁律」）：本文件为**新建**，无需备份。
> 建立原因：用户 2026-10-14 对 `docs/AI-AGENT-HANDBOOK-融合评估-20261014.md` §8 的四项待裁定
> 逐条给出裁决（见 §0），要求据此**立项施工**。本文件是**立项书**——定义范围、可写路径、
> 接口、验收、排序与授权请求；**尚未动 `src`、未重启生产、未 git 写**。
>
> 取证日期 **2026-10-14**（23:5x）；被测 `agenthub v0.13.103`、`git f8325bc6`（ahead 3）。
> 规格来源：`docs/AI-AGENT-HANDBOOK-融合评估-20261014.md`（本仓）+ `docs/AGENT-MEMORY-REPO-评估-20261013.md`（本仓）。

---

## 0. 本批裁决（用户 2026-10-14）

| # | 待裁定项 | 裁决 |
|---|---|---|
| 1 | P0-1~P0-5 是否立项施工 | ✅ **立项施工**（各须先报可写路径 / 授权形态，见 §5） |
| 2 | P0-1 与《AMR 评估》P1「Dreaming」是否合并 | ✅ **合并为同一条台账项**（见 §3 P0-1） |
| 3 | 知识通道是否按 A-②（本机 KB 路）落地 | ✅ **按 A-② 落地**（见 §2） |
| 4 | P0-2 的 CCR usage 账本对接口径 | ✅ **本文件 §1 定案** |

---

## 1. P0-2 的 CCR usage 账本对接口径（裁决 4，已取证）

### 1.1 账本事实（实测 2026-10-14 23:51）

- 位置 `~/.claude-code-router/app-data/usage.sqlite`（23.7 MB，`-wal` 活跃）。
- 表 `usage_events`，**37,036 行**，范围 `2026-07-26T12:06:46Z → 2026-10-14T15:51:59Z`。
- 列（全部可用）：
  `created_at, request_id, client, method, path, model, logical_model, provider,
   credential_id, status_code, duration_ms, input_tokens, output_tokens,
   cache_read_tokens, cache_write_tokens, total_tokens, cost_usd, cost_source`。
- 已索引维度：`client`、`model(+created_at)`、`provider(+created_at)`、
  `credential_id(+created_at)`、`created_at`、复合 `(created_at,provider,model,credential_id)`。
- 近 30 天 `client` 分布（实测）：

  | client | 请求数 | total_tokens | Σcost_usd | cost 为 NULL 数 |
  |---|---|---|---|---|
  | `API Key 1` | 10077 | 1,111,104,748 | 18.7616 | 2882 |
  | `Local Gateway` | 3033 | 286,077,268 | 2.7282 | 2507 |
  | `Profile: Claude Code` | 504 | 41,752,248 | 0.0 | 504 |
  | `unknown` | 314 | 15,096,146 | 0.2718 | 162 |
  | `Profile: Codex` | 23 | 170 | 0.0 | 23 |

- `cost_source` 分布：`models.dev` 7453 / `openrouter` 420 / `request_log` 12 / **空 6066**。

### 1.2 口径定案

1. **禁用 `logical_model` 作聚合维度**——它是内部逻辑名
   （形如 `provider-alibaba-b600d5a733::openai_chat_completions/deepseek-v4.1-flash`），
   不是客户端/模型 ID（撞 2026-09-07 判例：账本逻辑名当客户端 ID 致 jcode 不可用）。
2. **主聚合维度 = `client × model`**（`client` 辨识调用来源，`model` 是真实模型 ID）；
   需要费用时按 `provider × model` 交叉。时间轴一律走**已索引**的
   `(created_at,…)` 复合索引，按 **UTC 日切**（`created_at` 带 `Z`），展示层再转本地。
3. **cost 处理：`COALESCE(cost_usd,0)` 聚合，但必须同时回 `null_cost` 计数**——
   禁止把「未计价/NULL」静默当 0（近 30 天仅 `API Key 1` 就有 2882 条 NULL）。
   空 `cost_source` 的 6066 条须在回包里显式标 `cost_source=""`。
4. **与 runlog 的任务粒度对接**：`usage_events.request_id` 是**可用关联键**，
   但 `src/runlog.py` 现**不带 request id**（`src/llm.py` 也未发 `X-Request-Id`）。
   ⇒ 落地第一步是**单次只读探针**：验证 CCR 是否透传客户端 `X-Request-Id` 到
   `usage_events.request_id`；透传则 agenthub 侧补发 header 即可 task↔usage 对齐；
   不透传则退化为「按 `client×model×时间窗` 近似归因」，并在回包显式标 `attribution=approximate`。
5. **对接方式 = 只读**：agenthub 以 `file:…usage.sqlite?mode=ro` 只读打开，
   **绝不写账本**（账本是 CCR 生命线资产）。任何聚合失败一律回
   `{ok:false,error}`，不得静默返回 0。
6. **P0-2 不新增表**：token/cost 先并入 `profile_events.detail`（复用 runlog 既有口径），
   待 P0-1 建 `eval_run` 时再决定是否物化。

---

## 2. A-② 知识通道落地设计（裁决 3）

### 2.1 关键发现（决定落法）

**直接 clone 到手册目录 ≠ 可检索**——两路都不覆盖：

| 通路 | 覆盖规则（实测源码） | 手册落在 `…/<任意子目录>/**` 时 |
|---|---|---|
| `turbovec`（语义） | `DOC_DIR=/fs/1000/ftp/技术文档`，`os.walk` **maxdepth=2**（顶层 + 每个顶层子目录的**直接文件**），带 JUNK/backup/`ARCHIVED` 排除 | 🔴 章节目录是第 2 层（`<dir>/01-architecture/x.md`）⇒ **不进索引** |
| `workspace_files`（rg 实时） | 白名单根**仅** `MEMORY.md` / `memory/` / `agent-knowledge/` / `digest/`（`src/memfed.py:_RG_TARGETS`） | 🔴 不在白名单 ⇒ **不被 rg 源扫** |

### 2.2 落法（推荐 A-②-1，备份 A-②-2）

- **A-②-1（零 src 改动，推荐）**：把手册 56 篇 md **扁平化**落到
  `/fs/1000/ftp/技术文档/ai-agent-handbook/*.md`（**深度 1**，文件名带章号前缀防重名），
  再跑一次 `turbovec` rebuild（**约 1800 s 窗口**，须用户授权）。
  优点：不进 `src`、语义+全文双通路天然生效；缺点：须 rebuild 窗口。
- **A-②-2（小改 src，备选）**：在 `src/memfed.py:_RG_TARGETS` 增一路
  `handbook: ([str(WORKSPACE/"ai-agent-handbook")], "**/*.md")` 并登记 `REGISTRY`
  （rg 的 `*` 不跨 `/`，必须 `**/*.md`）。优点：零 rebuild、实时；缺点：动 `src`+改联邦源清单。
- **不要** clone 到 `agenthub/work/`（已 gitignore，且不入任何检索路）。
- Pin：上游 `e97f890d`（Apache-2.0，无 copyleft 风险）；落盘时记来源 URL+版本+日期到 `README`。

---

## 3. P0 施工清单（每项：目标 / 可写路径 / 接口 / 验收 / 依赖）

> 所有 `src` 改动**均在 worktree 内**进行（`scripts/worktree-new.sh`），主 checkout 只由集成者合并；
> 逐文件改前取时间戳 `cp` 备份（`<f>.bak-YYYYMMDD_HHMMSS-<说明>`）。

### P0-2 ｜ runlog token/cost + CCR 账本只读对接 ｜ 依赖：无（先做）

- **目标**：让「单位成功任务成本」可度量（现状 runlog 无 token/cost）。
- **Create**：`src/usage_ledger.py`（只读 `usage.sqlite` 的聚合门面；
  `summary(group_by=["client","model"], since, until)`、`by_request(ids)`）。
- **Modify**：`src/llm.py`（请求头加 `X-Request-Id`，值=runlog call_id）；
  `src/runlog.py`（`detail` 增 `tokens/cost/usage_ref` + 新增 `GET /api/runlog/cost` 聚合端点）；
  `src/main.py`（挂 router）。
- **验收**：① 单次探针确认 CCR 是否透传 `X-Request-Id`（≤2 次，如实报 PASS/不可判定）；
  ② `usage_ledger.summary` 对账：与 `sqlite3` 手算同值（回 `null_cost`）；
  ③ `runlog` 回包含新字段；④ `run_tests.sh all` 0 失败；⑤ 负向：账本文件缺失→`ok:false` 不静默 0。

### P0-4 ｜ 审计/证据分层 + 不可判定态 ｜ 依赖：无

- **目标**：把 PASS/FAIL 二元升级为「不可判定」一等公民（「禁把 SKIP 当 PASS」可断言化）。
- **Modify**：`src/db.py`（`asset_audit` 增 `verdict(confirmed|dismissed|insufficient)`、
  `severity`、`confidence` 三列，走 `init_db` 幂等补建）；
  `src/audit.py`（分层链路：原始事实→候选信号→研判；`insufficient` **不走放行**）；
  `run_tests.sh` / 闸门产物（结果对象拆 `exec_status + validity + evidence + quality` 四字段）。
- **验收**：① 新列幂等补建不破坏既有行；② `insufficient` 拦截测试（负向）RED→GREEN；
  ③ 闸门产物四字段可断言；④ 全量测试 0 失败。

### P0-5 ｜ 强制边界 vs 观测边界（纳管四维） ｜ 依赖：无

- **目标**：治「纳管 ≠ 可用 / 只读未证」旧账（09-23 首扫假阴性同族）。
- **Modify**：`src/profiles.py`（纳管矩阵每 agent 标四维：`强制/观测 × 覆盖/未覆盖`）、
  `src/discovery.py`（扫描产物带四维）；`tests/`（每维**负向测试**：绕过路径必须被拒）。
- **验收**：① 未过负向测试的项只能写「未观察到写操作」，不得写「只读」；
  ② 四维字段进 `/api/agents` 契约；③ 负向测试 RED→GREEN；④ 全量测试 0 失败。

### P0-3 ｜ 任务状态机 + 预算 + 证据判定 ｜ 依赖：P0-4（证据语义）

- **目标**：执行者不得自证 `success`；补等待态/预算。
- **Modify**：`src/tasks.py`（status 枚举加 `waiting_input|waiting_approval|paused|verifying|cancelled`；
  表加 budget 字段；`success` 改为由 Verifier 判定，runner 只 `submit`）；
  `src/db.py` 若涉及共用表；**Create**：`src/verifier.py`（按 kind 返回结构化缺口+可修复性）。
- **验收**：① 未过 Verifier 不得置 `success`（负向 RED→GREEN）；② 等待态释放执行资源可按断言验证；
  ③ 全量测试 0 失败。

### P0-1（**已合并 AMR Dreaming**）｜ 轨迹→数据集→经验库闭环 ｜ 依赖：P0-2 + P0-3

- **目标**（两来源合并）：`dataset/dataset_item/eval_run/experience` 四表 + 离线 Pipeline +
  经验挖掘（= AMR「Dreaming」职责：合并重复/删过期/解冲突），激活 TDAI `tasksEnqueued:0` 空转位。
- **Create**：`src/dataset.py`（四表 CRUD + `split(train|holdout)` + `grain` 唯一键）；
  `scripts/dataset_pipeline.py`（声明式管线，确定性过滤→采样→最后调模型）；
  `scripts/experience_mine.py`（经验挖掘，**非会话一律 `openrouter/free`**）。
- **Modify**：`src/db.py`（建四表）、`src/hook.py`（轨迹级聚合）、`src/main.py`（挂 router）。
- **调度**：systemd **user timer**（**禁写 crontab**），或端点触发。
- **验收**：① 四表幂等建表；② `split` 隔离断言（优化批次不得纳入 holdout）；
  ③ 一次**真实召回验证**（开关=on 不算证据，须留真实命中+来源取证）；
  ④ 全量测试 0 失败。

### 排序（依赖 + 风险）

`P0-2 → P0-4 → P0-5 → P0-3 → P0-1`，逐项**串行**（AGENTS.md 单会话可追踪）。
A-② 可独立并行（不碰 `src`）。

---

## 4. 验收与回滚（全项适用）

- **闸门**：`bash run_tests.sh all`（基线 1587 例 0 失败 0 跳过）+ `bash scripts/prepush.sh` 六步全绿。
- **负向测试**：每个「边界/证据」判据必须有**证明绕过被拒**的负向测试（否则只能写「未观察到」）。
- **生产取证**：禁重启（不打断活跃会话）；改动以 `/health` 指纹 + 端点实测取证；重启交由用户/自然重启。
- **回滚点**：逐文件 `cp` 时间戳备份；worktree 隔离；**禁对 `.git` 做恢复/替换/reset**。
- **模型纪律**：非会话调模一律 `openrouter/free`（**严禁 `openrouter/auto`**）。

## 5. ⏸ 需用户授权的项（未授权不擅动）

1. **`src` 可写授权 + worktree 施工**：本批须 `src/*.py`、`scripts/*`、`tests/*` 可写（限 worktree 内）；
   主 checkout 由集成者合并。（另：C10 闸门报主 checkout 有 2 条未提交 ⇒ 施工前须处置。）
2. **turbovec rebuild 窗口**（A-②-1）：约 1800 s，须授权。
3. **调度载体**：P0-1 管线排程用 **systemd user timer**（禁 crontab）——which?
4. **`git` 写**：P0 各批完成须 `git commit`（当前 ahead=3，含本批未提交 docs）。
5. **台账单写者**：`PENDING-TASKS.md` 今日已被 ≥2 会话写（C3b FAIL）⇒ 本合并项 PT 登记**停手报请**。
6. **A-②-2**（改 `src/memfed.py`）是否需要，取决于是否接受 rebuild 窗口。

---

*立项完毕。本轮仅新增本文件；`src`、服务、git 均未触动。*