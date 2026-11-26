# agenthub × magpie 互调融合计划（2026-11-27）

> 用户裁定（2026-11-27）：「**不是合并代码，只是功能模块和数据存储互相调用**」。
> 本文是唯一施工依据；执行日志见同目录 `MAGPIE-INTEROP-LOG-20261127.md`（append-only）。

## 0. 一句话目标

hub 主动拉 magpie 的三个**只读**面，magpie 核心零改动：把「派发池 / 额度闸门 / 成本归因」三处从**写死**改成**派生**。

## 1. 范围与边界

**做**
- hub → magpie：`GET /v1/models`（模型目录+能力元数据）、`GET /v1/magpie/quotas`（额度）、`usage.jsonl`（逐次调用账单）
- 双向**语义**互调：magpie library 指令里写明「hub 有 `/api/tasks/costs`」，agent 侧可查任务成本
- 派发日志标准化，用于反向优化派发功能

**不做**
- 合并代码 / 修改 magpie Go 源码 / 动 magpie 插件
- 把 hub 业务表搬进 magpie，或反之
- 让 magpie 反向回调 hub（避免循环依赖）

**真源分配（单一权威，不复制）**

| 事实 | 真源 | 消费方 |
|---|---|---|
| 模型清单 / 能力 / 定价 | magpie `GET /v1/models` | hub `modelcfg` / `tiercfg` / `modelgate` |
| 额度 / 配额窗 | magpie `GET /v1/magpie/quotas` | hub 派发前闸门 |
| 逐次调用成本 | magpie `usage.jsonl` | hub `tasks` 成本回填 |
| 任务血缘 / 验收 / 判词 | hub `tasks` + `task_runs` | 派发日志、成本反查 |

## 2. 已取证事实（决定计划形状）

| # | 事实 | 证据 |
|---|---|---|
| 1 | 网关 53 模型 / 4 provider（workbuddy、raccoon、dumate、auto），27 个带 pricing | `curl :3425/v1/models` 实测 2026-11-27 01:5x |
| 2 | 派发池只写死 6 条且全是 `workbuddy/*` ⇒ 47 模型（88%）不可选 | `src/tiercfg.py:47` + `/api/tasks/tiers` 实测 |
| 3 | 额度端点免 key、200、2.2KB、4 张卡（1 张 401 报错态） | `curl :3425/v1/magpie/quotas` |
| 4 | usage.jsonl 逐次记录 agent/provider/model/in/out/credits/ms/ttft/session | `~/.config/magpie/usage.jsonl` 末行 |
| 5 | hub adapter 支持 per-task 环境注入（可做成本关联键） | `src/adapters/cli_agent.py:511,641` `env=self.env()` |
| 6 | hub `tasks` 已有 `model`/`spent_tokens`/`verdict`/`duration_ms`，**无 credits** | `src/tasks.py:161` 注释、`PRAGMA table_info` |
| 7 | 生产 DB 在 `agenthub/data/agents.db`（1.0 GB，勿全表扫） | `src/config.py:126` + `ls` |
| 8 | 派发链路 dry-run 可用，拆出 t1/t2 两任务 | `hub-dispatch "..."` 实测 |

## 3. 融合架构：四条互调链

```
L1 目录（单向拉）   magpie GET /v1/models ──▶ tiercfg.derive_tiers()  ──▶ 派发池
                                          └─▶ modelgate 闸门（现有）

L2 额度（派发前）   magpie GET /v1/magpie/quotas ──▶ quota_gate.blocked()
                                                    └─▶ tasks.model_skip_reason

L3 成本（派发后）   magpie usage.jsonl ──▶ cost_backfill.match(run) ──▶ tasks.credits/ttft_ms
                   关联键：per-run 短期 gateway key（可选）或 时间窗+model+tokens（兜底）

L4 反查（双向语义） hub GET /api/tasks/costs ──▶ magpie library 指令 / agent 技能
```

**降级原则**：magpie 任一端点不可达 ⇒ 回落现有写死值 + 在响应里带 `source:"fallback"` 与原因，**不静默**。

## 4. 执行机制

- 每个阶段 = **一个 run**，用 `~/bin/hub-dispatch --run` 派发；`run_id` 写进日志。
- 派发 prompt 必须内联四件事：目标文件绝对路径 / 允许改动范围 / 验收命令 / 禁止事项。
- 日志 schema（每段固定字段）：

```
## P<n> <阶段名> — <时间>
run_id: <id or manual>
改动: <file:line 列表>
验收: <命令 + 实测输出摘要 + PASS/FAIL>
回滚: <备份文件名>
偏差: <与计划的差异 + 原因>
```

- 护栏：改前 `backup_file`（`.bak-<ts>-<说明>`）；禁 `sed -i`/全局替换；生产重启走
  `bash agenthub/scripts/hub-restart.sh --reason "<改了什么>"`。

## 5. 阶段与验收

### P0 基线 + 派发冒烟（只读，约 20 分钟）

- 动作：快照 `/v1/models`、`/v1/magpie/quotas`、`/api/tasks/tiers` 到 `work/interop/p0/`；跑一次真实 `--run` 冒烟并把 run_id 记入日志。
- 验收：三个快照文件非空；冒烟 run 在 `/api/tasks/{run_id}` 可查、`tasks.model` 有值。
- 回滚：无写操作，不需回滚。

### P1 目录互调：派发池从网关派生（约 2 小时）

- 新文件 `src/magpie_client.py`：`models(ttl=60)`、`quotas()`、`available()`，失败返回 `(None, reason)`。
- 改 `src/tiercfg.py`：新增 `derive_tiers()` 按能力元数据（context/pricing/reasoning）分档，**默认不启用**，`TASK_TIERS_SOURCE=magpie` 才走；`DEFAULT_TIERS` 原样保留作 kill switch。
- 验收：
  - `curl /api/tasks/tiers` 在开关打开时出现 `raccoon/*` 或 `dumate/*`；关闭时逐字等于今天。
  - `TASK_TIERS_SOURCE=off` 回落测试：`/api/tasks/tiers` 的 tiers 与 P0 快照逐字节相同。
- 回滚：`tiercfg.py.bak-<ts>-magpie-derive`，开关置 off 即可。

### P2 额度闸门（约 1.5 小时）

- 新文件 `src/quota_gate.py`：读 quotas，产出 `blocked_providers()`（**区分 `error` 与额度耗尽**）。
- 改派发前选路：被挡 provider 的模型跳过，原因写 `tasks.model_skip_reason`。
- 验收：构造一次「provider 有 error」情形，派发应跳过并落原因；未挡 provider 照常派发。
- 回滚：`TASK_QUOTA_GATE=off`（默认 off 上线，观察一版再默认 on）。

### P3 成本归因（约 3 小时）

- 新文件 `src/cost_backfill.py`：按 `usage.jsonl` 关联 run，回填 `credits`/`ttft_ms`/真值 tokens。
- 关联键**两段式**：① 若 run 带 `magpie_key_id`（per-run 短期 key）→ 精确匹配；② 否则时间窗 + model + tokens 近似，并标 `match_confidence=approx`。
- 幂等：同一 task 只回填一次（`credits_source` 记录来源）。
- 验收：一次真实 run 后 `sqlite3 data/agents.db "select task_id,model,credits from tasks where run_id='<id>'"` 有非空 credits 或明确 `approx` 标记。
- 回滚：新增列保留（可空），停用回填任务即可。

### P4 反查端点 + 日志分析（约 1.5 小时）

- 新端点 `GET /api/tasks/costs?run_id=&group_by=agent|model|provider`：JOIN `tasks` 与回填列。
- magpie library 指令追加一句（`magpie library instructions`）：查任务成本走 hub 端点。
- 用 P0–P3 真实日志出「派发功能缺陷清单」（不少于 5 条，每条带证据与建议）。
- 验收：端点返回 200 且字段齐全；缺陷清单落 `agent-knowledge/`。
- 回滚：端点独立，删除路由即可。

## 6. 护栏

1. **受保护面**：`agenthub/**` 属受保护面 ⇒ 每次改前时间戳备份，禁 `sed -i`、禁全局替换。
2. **生产重启**：只走 `hub-restart.sh --reason`；`term_sessions>0` 时须 `HUB_RESTART_FORCE=1` 签字，本计划**不主动重启**，纯代码提交后由巡检窗口重启。
3. **探活 ≤2 次**：每个判据最多两轮，不轮询。
4. **免费模型**：任何非会话调模一律 `openrouter/free`；本计划主要用 hub 池内 `workbuddy/*`。
5. **不改 magpie**：`~/.config/magpie/**` 只读；不新增 gateway key（除非 P3 需要且用户另行批准）。

## 7. 待裁定（不阻塞 P0–P2）

| # | 事项 | 我的默认做法 |
|---|---|---|
| R1 | P3 是否允许 `magpie gateway-key issue --ttl 2h` 建 per-run 短期 key | **先不做**，用时间窗近似；精度不够再申请 |
| R2 | `TASK_TIERS_SOURCE=magpie` 是否最终默认 on | 先默认 off，观察一版派发日志后由用户裁 |
| R3 | P4 是否动前端（`static/hub.js` 249 次提交的热点文件） | **只出 JSON API**，不动前端，避免并发冲突 |
