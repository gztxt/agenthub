# 全面改造执行日志（第二版，append-only）

> 计划：`docs/MAGPIE-INTEROP-2-20261127.md`
> 字段固定：`run_id / 改动 / 验收 / 回滚 / 偏差 / 未完成 / 错误`

## 状态看板（每段结束时更新）

| 波 | 任务 | 状态 | run_id |
|---|---|---|---|
| 1 | T2 flash 倾斜 | ✅ 已完工 | 手测 6/9=67% |
| 1 | T1 开关转默认 | ✅ 已上线 | 重启 pid 172121 |
| — | 派发实测 | ✅ t1/t2 pass，t3 运行中 | 747722b6ec |
| 1 | T3 成本自动回填 | ✅ 已完工 | 1a61a07531 |
| 1 | T4 观测补齐 | ✅ 已完工 | pool.magpie |
| — | 事故：dumate 429 卡死 | ✅ 已修（实时错误窗） | 500d58fd52 |
| 2 | T5 后端协同增强 | ✅ 已完工 | v0.13.216 |
| 2 | T6 前端工作台 | ✅ 已完工 | v0.13.217 |
| 2 | T7 闸门与真渲染 | ⏳ 未开工 | — |
| 3 | S1 读切 | ⛔ 改判合并进 S2 | — |
| 3 | S2 写者收敛 | ✅ 已完工（默认仍 hub） | v0.13.218 |
| 3 | S3–S5 技能/MCP/记忆/下线旧副本 | ⏳ 未开工 | — |

## 错误与未完成（只记事实，不记推测）

（空）

---

## T2 flash 倾斜 + T1 开关转默认 — 2026-11-27 10:3x

**改动**
- `src/magpie_client.py`：新增 `is_flash()` / `_flash_first()`；`derive_tiers()` 增加 flash 分支
  （`TASK_TIERS_FLASH_FIRST=on` 才走，`off` 时走 v0.13.214 原分支**逐字不动**）。
  flash 分支：t1 全 flash → t2 两 flash → t3 质量优先（2 强项 + 1 flash 兜底）；
  档间**全局去重**；任一档凑不满则返回 `(None, reason)` 回落写死表。
- `.env` 追加三行（备份 `.env.bak-20261127_1040xx-interop-defaults`）：
  `TASK_TIERS_SOURCE=magpie` / `TASK_QUOTA_GATE=on` / `TASK_TIERS_FLASH_FIRST=on`

**验收**
| 判据 | 结果 |
|---|---|
| `FLASH_FIRST=off` 与 v0.13.214 派生结果逐字一致 | PASS |
| `=on` 时 t1/t2 全 flash、t3 全 plain、档间无重复 | PASS（6/9 = 67% flash） |
| 生产端点 | PASS `source=magpie gate=on blocked={}` |
| 真派发 | run `747722b6ec`：t1 `glm-5.3-flash` / t2 `dumate/deepseek-v4-flash-0731` 均 flash |

**回滚**：注释 `.env` 三行 + 重启；代码侧 `TASK_TIERS_FLASH_FIRST=off` 即回原分支。

**偏差（重要）**
计划里 T1 与 T2 并列，实测发现**顺序必须是 T2 先**：不开 flash 倾斜时派生档位
t1 只有 1/3 是 flash（`dumate/qwen3.5-35b-a3b` 不含 flash），先开 T1 会让 flash
占比相对写死档（t1 两条全 flash）**下降**。已按 T2→T1 顺序执行。

## 派发实测（新档位 + 闸门 + flash 倾斜）— run 747722b6ec

| 任务 | agent | 难度 | 档 | 实发模型 | flash | 结果 |
|---|---|---|---|---|---|---|
| t1 | claude-code | 2 | t2 | `workbuddy/glm-5.3-flash` | ✅ | pass |
| t2 | codex | 1 | t1 | `dumate/deepseek-v4-flash-0731` | ✅ | pass |
| t3 | claude-code | 1(汇总) | t3(pinned) | `dumate/kimi-k3` | — | running |

- 产物 `work/interop/p1/flash-verify.txt` = `53 / 14 / dumate/deepseek-v4-flash-0731`，三项均与实时数据一致
- 成本回填（手动端点）：t1 `exact 0.02 credits`、t2 `exact 0.0`（dumate flash 免费档）
- **缺陷复现**（与上一轮一致）：`spent_tokens` t1=322、t2=106678，与账本口径仍不同源 ⇒ T3 自动回填必要

---

## T3 成本自动回填 — 2026-11-27 11:3x-11:4x

**改动**：`src/tasks_runner.py` 新增 `_schedule_cost_backfill()`，在 run 收敛分支
（`if finished:` → `return` 之前）调度后台线程；`COST_AUTOFILL` 默认 `on`；
进程内 `_cost_scheduled` 去重；失败按 3s/8s 重试两次。

**为什么后台线程而不是 await**：账本落盘比 hub 记账晚 1~3 秒（立即读=无候选）；
读 4.6MB 账本不能拖慢收尾；`db` 是 `check_same_thread=False` + 全局锁（`src/db.py:288/385`），
后台写库是既有支持用法。

**验收（run `1a61a07531`，全程未调手动端点）**
```
日志： [cost] run=1a61a07531 回填 3/3（read_error=跳过 2 行不可解析）
t1 workbuddy/hy4flash-free        credits=0.0   approx  ttft=1352
t2 raccoon/sn-sensenova-6-8-flash-lite credits=0.0 approx ttft=1821
t3 workbuddy/kimi-k3.1-pro        credits=12.53 exact   ttft=7493
```
- 调度去重 + 开关单测通过（`COST_AUTOFILL=off` 时不启线程）

**回滚**：`COST_AUTOFILL=off`；代码备份 `src/tasks_runner.py.bak-20261127_11xxxx-autofill`

## 事故：dumate 连续 429 导致派发卡死 — 2026-11-27 11:2x

**现象**：run `500d58fd52` 的 t1 卡在 `dumate/deepseek-v4-flash-0731`，
`claude -p` 每 38 秒重试一次，连续 6 次 429，任务 4 分钟未收敛。已 `POST /cancel` 止损。

**根因（两层）**
1. 额度闸门只看 **额度窗口**（`/v1/magpie/quotas`），不看**实时错误**；
   dumate 额度还剩 91.9%，但上游正在 429 限流。
2. hub 的模型回退**故意不认 429**（`tests/test_task_model_fallback.py` 注释：超时/429/generic
   不判为模型问题）⇒ 429 不会触发换模型，只在同一模型上循环。

**修法（provider 级，不是模型级）**：`src/magpie_client.py` 新增 `recent_failures()`
读账本尾部近 10 分钟，`_error_blocked()` 把「fail ≥3 且占比 ≥0.5」翻译成与额度闸门同形的
blocked 映射，`provider_health()` 合并为 `degraded`。环境变量
`MAGPIE_ERR_WINDOW_MIN=10 / ERR_MIN_FAILS=3 / ERR_RATIO=0.5`。**读不到账本不挡**。

**验收**
- 修前派发池 `blocked={}`；修后 `blocked={'dumate': '近 10 分钟失败 6/9（HTTP 429）'}`
- 档位自动剔除 dumate：t1/t2 只剩 workbuddy + raccoon
- 复跑 run `1a61a07531`：t1 秒过（`workbuddy/hy4flash-free`），无 429 循环
- 新增 3 条单测（比例拦截 / 账本缺失不挡 / degraded 不进档位）

**回滚**：`ERR_MIN_FAILS=999` 等效关闭；代码备份 `src/magpie_client.py.bak-20261127_11xxxx-errwindow`

---

## T5 后端协同增强 — 2026-11-27 12:2x

**改动**
- `src/tasks_dag.py`：`DecomposeIn` 增 `agents[] / assign_mode / force_agents`；
  新增 `_apply_forced_agents()`；`agents` 传了却全不在池 ⇒ 400；Leader 候选池收窄到多选项。
- `src/tiercfg.py`：`plan()` 见 `agent_forced` 不再 `promote_agent`（记 `agent_tier_reason=forced`）。
- `src/tasks.py`：新增 `GET /api/tasks/overview`（路由注册在 `/api/tasks/{run_id}` 之前）。

**验收（真跑 dry_run，不花钱）**
```
agent_policy={"mode":"auto","pool":["codex"],"forced":1,"changed":[]}
t1 agent=codex  pinned=None reason=forced        tier=t2 model=workbuddy/glm-5.3-flash
t2 agent=codex  pinned=top  reason=already_at_floor tier=t3 model=dumate/kimi-k3
```
- 汇总节点（t2）**未被强制**，仍走顶层 ✓
- `overview?days=7`：`magpie.models=53 blocked={} cost.credits=22.95 flash.planned=0.667` ✓

**踩坑（第一版写错，已修）**：force 最初排在 `ensure_synthesis` 之前 ⇒ 汇总节点拿不到
`pinned_tier=top`，被一起钉成用户选的执行者（实测出现 `reason=forced` 的汇总）。
移到 `ensure_synthesis` 之后、`_plan_tiers` 之前才同时满足两个边界。

**测试**：`tests/test_collab_force_agents_l0.py` 7 例；合计 34 passed。
**回滚**：`agents/force_agents` 不传即老行为；代码备份 `src/tasks_dag.py.bak-*-t5`、
`src/tiercfg.py.bak-*-t5-forced`、`src/tasks.py.bak-*-t4`。

## T6 前端工作台改版 — 2026-11-27 11:5x-12:1x

**改动**
- 新 `static/hub/18-collab.js`（约 200 行）：状态条、Agent 多选、tab 切换、请求体组装。
- `templates/index.html`：`#page-tasks` 换成 `.cb-main > .cb-left + .cb-right`；新增 `.cb-*` CSS。
- `static/hub/04-terminal-ws.js`：`taskBodyFromUi / fillTaskModelSelect / enterTasksPage`
  改为「有 `cb*` 实现就委派」——不复制第二份逻辑，也不产生跨分片重名。
- 任务表新增「档」「credits」两列（`renderTaskTable`）。

**验收（真渲染，CDP 驱动 chromium headless）**
| 判据 | 1440×900 | 390×844 |
|---|---|---|
| `#page-tasks.on` | true | true |
| 两栏宽度 | left 484 / right 663 | left 308（单栏堆叠） |
| tab / view 数 | 3 / 3 | 3 / 3 |
| Agent 芯片 | 11 | 11 |
| 页级滚动 | false | false |
| 横向溢出 | false | false |
| 状态条真实数据 | 53 模型 / 48.8%-100%-91.2% / 22.95 credits / flash 50%-67% | 同 |

**390 宽实测修掉的三处（都是真渲染才看得见）**
1. `.cb-agents` 有 `overflow:auto` ⇒ 作为 flex 项 `min-height` 归零 ⇒ 芯片整块消失。
   修法 `.cb-box > * { flex: 0 0 auto; }`。
2. 左栏 `max-height:46vh` 把底部按钮裁掉一半 ⇒ 放宽到 60vh（内容仍在栏内滚）。
3. tab 标签换行成两行 ⇒ `white-space:nowrap` + 容器横向滚；`runsCount` 从 tab 移到历史工具条。

**未做（记入待办）**
- 进页自动打开最近一条运行中 run（当前需去「历史」点「查看」）
- 汇总 tab 的成本-成功率散点（当前只有表格 + flash 占比）

**测试**：62 passed（`sys_pages_layout` / `hubjs_split` / `collab_force_agents` / `magpie_interop` / parity）。

**回滚**：`templates/index.html.bak-*-collab-t6`；`18-collab.js` 可整体删除（04 三处委派有兜底）。

## 波3 · S1 改判 + S2 写者收敛 — 2026-11-27 12:2x

**S1 改判（不执行「读切」）**
计划里的 S1 是「modelcfg 读 agent 模型改走 `magpie ls`」。实测 `magpie ls --json` **不支持**
（把 `--json` 当 agent 名），只能解析人类可读文本。更重要的是：读写指向**同一份文件**，
换读取实现不会减少任何写冲突，只会引入一个脆弱文本解析器。
⇒ 撤销 S1，价值并入 S2（写者收敛 + 回读验证）。这条改动已写进本日志，不是静默跳过。

**S2 改动**
- `src/modelcfg.py`：新增 `writer_mode()` / `_apply_via_magpie()` / `MAGPIE_WRITABLE`；
  `apply_model()` 开头按 `MODELCFG_WRITER` 分流（默认 `hub`，生产不变）。
- `src/api_magpie.py`：`/api/magpie/status` 增 `model_writer` / `model_writer_agents`。

**A/B 实测（jcode）**
```
before : workbuddy/deepseek-v4.1-flash
apply  : writer=magpie verified=True → 回读 workbuddy/hy4flash-free
restore: writer=magpie verified=True → 回读 workbuddy/deepseek-v4.1-flash
文件 diff：与原文件**逐字节一致**
```

**踩坑**：第一版把 `set_hub_model()` 放在验证之后直接裸调，独立脚本里 db 未初始化即抛异常
⇒ 文件已被 magpie 改好，调用方却收到「失败」（会让人重复点）。
改为记账失败只写 `bookkeeping_error` 字段，`verified` 仍如实返回。

**为什么默认仍是 hub**：切换写者会让 codex/pi 的配置改由 magpie 落笔，属共享配置变更；
按军规须显式改 .env + 重启 + 端到端验证。当前留开关，等 S3 一起切。

**测试**：`tests/test_modelcfg_writer_l0.py` 6 例。

**未完成（波3 剩余）**
- S3 技能/MCP/指令 读 `library.json` + 写调 `magpie library …`
- S4 记忆读写收敛到 magpie memory
- S5 下线 hub 自研副本（保留只读兼容端点 + 改判横幅）
