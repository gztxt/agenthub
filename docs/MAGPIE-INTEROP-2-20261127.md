# agenthub × magpie 全面改造计划（第二版，2026-11-27）

> 用户裁定（2026-11-27）：
> ① `TASK_TIERS_SOURCE` 转默认 magpie ② `TASK_QUOTA_GATE` 转默认 on ③ 成本自动回填
> ④ 「派发实际就是 agenthub 的协同」→ 前后端优化 + UI 重设计
> ⑤ 提高 flash 模型占用比例 ⑥ **agenthub 独有、magpie 没有的模块仍由 agenthub 执行**
> ⑦ magpie 的数据来源/存储 = 全部 agents 的数据，agenthub 部分自研功能由 magpie 取代
>
> 执行日志：`docs/MAGPIE-INTEROP-2-LOG-20261127.md`（append-only）
> 上一版：`docs/MAGPIE-INTEROP-20261127.md` / `docs/MAGPIE-INTEROP-LOG-20261127.md`

## 0. 归属判据（先定权，再动手）

1. **单一写者**：同一文件/表只允许一个写者；magpie 能写 ⇒ magpie 写，hub 只读或代理。
2. **能力以实测为准**：只认「命令/端点存在 + 本机跑通」，不认文档声称。
3. **无对应即保留**：magpie 无编排/终端/定时能力（`magpie --help` 动词表实测无
   task/dag/cron/pty）⇒ 这些永久留 hub。
4. **迁移可逆**：每个迁移点带 `*_WRITER` 开关，回滚一次配置即可。

## 1. 归属矩阵（证据已核）

| 数据/能力 | magpie 证据 | 归属 |
|---|---|---|
| agent 模型选择（5 类配置文件） | `magpie <agent> <model>`、`applied.json`（codex/dsh/omp/opencode/pi） | **magpie 写** |
| 模型目录 / 定价 / 能力 | `GET /v1/models` = 53 | magpie（已切） |
| 额度 / 成本 | `/v1/magpie/quotas`、`usage.jsonl` | magpie（已切） |
| 技能分发 68 条 | `library.skills` + `magpie library skill` | **magpie 写** |
| MCP 注册 | `library.mcp` 2 条 + router | **magpie 写** |
| 跨 agent 指令 | `library.instructions` | **magpie 写** |
| 长期记忆 | `~/.config/magpie/memory/`（已含 `hub-l2.md`） | **magpie 存** |
| 任务编排 / 拆解 / 验收 / 血缘 | 无对应 | **hub 保留** |
| 终端 pty / agent 卡片 / 服务看护 / 定时 / CLI Apps | 无对应 | **hub 保留** |
| 协同页 UI / 工作台 | 无对应 | **hub 保留并重构** |

**双写者现状（波 3 要消除）**：hub `modelcfg.apply_model()`（`src/modelcfg.py:638`，写面在
`:17-21`）与 magpie（`applied.json`）**同时写** `~/.codex/config.toml` 与 `~/.pi/agent/settings.json`。

## 2. 波 1（低风险，全部可回滚）

### T1 两个开关转默认（写 `.env`，不改代码默认值以免影响测试）

```ini
TASK_TIERS_SOURCE=magpie
TASK_QUOTA_GATE=on
TASK_TIERS_FLASH_FIRST=on
```

- 备份 `.env.bak-<ts>-interop-defaults`；重启只走 `scripts/hub-restart.sh --reason`
- 验收：`GET /api/tasks/tiers` → `tiers_source.source=magpie`、`quota_gate=on`；
  一次真实派发成功
- **保底**：全部 provider 被挡 ⇒ `plan()` 回落 `DEFAULT_TIERS` + `log.warning`，不静默停派

### T2 flash 倾斜（要求 ⑤）

- 判据：模型 id 含 `flash`（大小写不敏感；实测命中 14/53），不引入价格推断
- `derive_tiers` 每档 flash 配额：**t1 3/3、t2 2/3、t3 1/3**（t3 首项保留非 flash 强模型做质量保底）
- 档内 flash 前置 ⇒ `first_usable` 天然取 flash
- 观测：`/api/tasks/stats` 增 `flash_ratio{planned, observed}`
- 验收：连续 10 次派发 `observed ≥ 60%`（基线：近 30 天 45.1%，近 7 天 8/18）

### T3 成本自动回填（要求 ③）

- `tasks_runner` run 终态触发 `cost_backfill.backfill_run()`：后台线程 + 幂等
  （仅 `cost_confidence` 为空时写）+ 失败重试 1 次
- 开关 `COST_AUTOFILL=on`；手动端点保留
- 验收：新 run 30 秒内 `GET /api/tasks/costs?run_id=` 有非空 credits

### T4 观测补齐

- `/api/tasks/pool` 内联 `magpie:{models, providers, blocked, tiers_source}`（协同页一次取齐）
- `/api/magpie/status` 从 `tasks.py` 移到 `src/api_magpie.py` 统一

## 3. 波 2（协同前后端重构 + UI 重设计，要求 ④）

### T5 后端

- `decompose` 增 `agents: string[]`、`assign_mode: auto|spread`、`force_agents: bool`
  - `force_agents=true`（UI 默认）：显式多选即硬约束，修掉「`--agent` 被 Leader 覆盖」
  - `spread`：轮转分配；`race` 不做（需新表 + 预算，列 P2）
- 新端点 `GET /api/tasks/overview`：额度三色 / 今日 credits / 运行中数 / flash 占比 / 失败 top5
- `GET /api/tasks/{run_id}` 每任务补 `credits / ttft_ms / cost_confidence / tier_reason`

### T6 前端（新文件 `static/hub/18-collab.js`）

- `04-terminal-ws.js`（4039 行）S3 旧块迁出，减负而非叠加
- ≥1024px 双栏（左参数 / 右看板）；<1024px 单栏 + 分段控件；断点只在一处定义
- 左栏：目标 / Agent 多选芯片（灰显 + 原因）/ 混排方式 / 模型策略（自动 flash 优先·指定）/ 预算 / 预览·启动
- 右栏 Tab：运行中（DAG + 任务表含 credits）/ 历史（复用既有 `sid` 会话归并，**不重造**）/ 汇总（flash 占比·成本-成功率·失败归因）
- 顶部状态条：53 模型 / 3 provider 额度条 / 今日 credits / 运行中 N

### T7 闸门

- 新增 `tests/test_collab_ui_l0.py`：元素 id 齐备、浮层单一出口、断点单一定义、无 inline onclick
- `bash scripts/build_hubjs.sh` 重建，`tests/test_hubjs_split.py` 字节一致
- 真渲染：390 与 1440 两档截图 + 真鼠标点击一次；**不用 `elementFromPoint` 当判据**

## 4. 波 3（数据面交 magpie，逐步单向切换）

| 步 | 动作 | 开关 | 验收 |
|---|---|---|---|
| S1 | 读切：`modelcfg` 读 agent 模型改走 `magpie ls`（保留本地兜底） | 只读 | UI 与 `magpie ls` 逐项一致 |
| S2 | 写代理：`apply_model()` 改调 `magpie <agent> <model>` | `MODELCFG_WRITER=magpie｜hub` | **单写者证明**：操作期间 5 个配置文件只有 magpie 进程写 |
| S3 | 技能/MCP/指令：读 `library.json`，写调 `magpie library …` | `LIBRARY_WRITER=magpie｜hub` | 68 条一致；新增一条只出现一次 |
| S4 | 记忆：hub 联邦读合并 magpie memory；写只走 magpie | 无（已部分） | 同一事实不再两份副本 |
| S5 | 下线旧副本：删自研直写实现，保留只读兼容端点 + 改判横幅 | — | `grep` 无第二写者 |

**保留清单（永久留 hub，写进代码注释防误迁）**：任务编排/拆解/验收/血缘、终端 pty、
agent 卡片、服务看护、定时任务、CLI Apps、协同页 UI。

**单写者判据**：操作期间用 `inotifywait`/`fuser` 监测 5 个配置文件，写者 pid 必须全属 magpie；
出现 hub pid 即判失败并回滚。

## 5. 风险与既有裁定

| 项 | 处理 |
|---|---|
| 双写者已存在（codex/pi 配置） | 波 3 · S2 消除；此前不做并发写测试 |
| 867「记忆自动注入 B 模式永久否决」 | 不推翻：存储交 magpie ≠ 自动注入，仍 A 模式 |
| 865「代码融合否决」 | 全程只做互调/代理，不合并代码 |
| flash 倾斜降低难任务质量 | t3 首项保留非 flash；`TASK_TIERS_FLASH_FIRST=off` 回退 |
| 热点文件（index.html 267 次、hub.js 249 次/90 天） | 开工前 `orchestration-check.sh`；单会话串行；改前备份 |
| 改 agent 配置 = 受保护面 | 备份 → 改 → 文本+真工具调用双验证 → 失败即回滚 |

## 6. 派发链已知缺陷（用它执行改造前必须先修，否则日志不可信）

| # | 缺陷 | 证据 | 处置 |
|---|---|---|---|
| 1 | `--agent` 被 Leader 的 per-task `agent_id` 覆盖 | run 908a6cd0ae 实跑仍派 claude-code | T5 的 `force_agents`（**波 1 前置最小修**） |
| 2 | `spent_tokens` 低报 1~2 个数量级 | 27s 任务记 152 tokens | T3 账本回填 |
| 3 | 产物落 `/tmp` | `/tmp/magpie_models_raw.json` | 派发 prompt 模板加「中间产物落 work/」 |

## 7. 执行方式约定

- 波 1 的 T1–T4 用 `~/bin/hub-dispatch --run` 派发（粒度小、验收可执行）
- 波 2 / 波 3 由主会话串行执行（热点文件、受保护面、需真渲染判断）
- 日志 append-only，字段固定：`run_id / 改动 / 验收 / 回滚 / 偏差`；未完成与错误单独成节
- 期间不打断用户，不中途征求确认；遇到不可逆/安全敏感动作才停
