# 难度 → 档位：调研、设计与实测（2026-10-29，v0.13.176）

> 用户令：「实现难度到档位的映射；功能逻辑不要生搬硬套，不要为实现而实现，先联网调研评估。」
> 本文是那次调研的结论 + 因此**拒绝做**的东西 + 实际落地的东西 + 怎么证伪。

## 1. 调研结论（三条，直接改写了设计）

| 结论 | 来源（2026-10-29 检索） | 对本机的含义 |
|---|---|---|
| **用模型自评的 `difficulty: low` 做路由，是路由器最常见的病害**（"confidence theater"）：产出一个看起来在工作的系统，但决策与结果不相关，直到任务成功率漂移才被追溯 | scixa《Model Tier Switching Within the Execution Paths of AI Agents》 | Leader 自评的 1/2/3 **不能当闸门**（不能据此接受/拒绝/降级），只能当起点先验 |
| 20 个前沿模型 × 6 个基准：剔除「所有模型都答对」的题后，**置信度与表现的关系基本消失**；模型只有"这类模型能做什么"的认知，没有对自己的 | arXiv 2605.24299《LLMs Show No Signs Of Individuated Metacognition》 | 自评分数不能用来推断"这题它会不会做"；需要一个**外部**信号（verdict）来纠偏 |
| RouterArena 上的诚实消融：把难度路由器整条拿掉，成本-准确率前沿几乎不动（62.4% / $26.69 vs 62.4% / $26.73）——增益来自**检索证据**，不来自难度估计 | TokenTrim `jev-routing-experiment` | 难度映射本身**不产生收益**；收益必须由"失败升档 + 验收结论"承担 |
| 降级必须由结果证据支撑；**自评难度可以触发升档，不能触发降档**；goal 解释 / 不可逆动作 / 验收闸门永不降档 | scixa（同上，hard constraints 一节） | 决定了下面的"只升不降" |
| cascade 的经济性：便宜档先跑、失败再升；摊薄后成本随升档率线性上升，升档率过高会反超"一直用强模型" | FrugalGPT（arXiv:2305.05176）与 2026 年的复算 | 本机 `MAX_AUTO_RETRY=1` ⇒ 单任务最多两跳，升档率上限 50%，仍在划算区间 |

## 2. 因此**没做**的三件事（都是"为实现而实现"的典型）

1. **没有做"难度 1→便宜模型、难度 3→强模型"的硬路由表。** 那就是上面第一条的病害本身：
   表能写、能跑、能在 UI 上显示，但没有任何证据说明它比"全用同一个"更好，且一旦难度自评漂移，
   系统会**静默**把难任务降级。
2. **没有做"按难度换 agent 全家桶"的自动降级。** 降级要有结果证据；本机历史里没有这样的证据。
3. **没有把 `difficulty` 接进预算或验收阈值。** 同一理由：那是拿自评当事实。

## 3. 实际落地（两档，语义不对称）

### 3.1 模型档位：只填空，不覆盖

`t1 / t2 / t3`（`src/tiercfg.py`），默认 `1→t1, 2→t2, 3→t3`，地板 `3→t2`。

- **只填空**：Leader 显式给了 `model` 就一律不动（`tier="explicit"`）；
- **不猜**：清单取不到 → `model_planned=NULL` + `catalog_unavailable`；ID 不在清单 → `no_tier_model_in_catalog`。
  "返回个默认值凑合"等于把不知道伪装成知道；
- **不越域**：模型域不是 CCR 的 agent（codebuddy / cursor）不注入 CCR ID，否则必报 unknown model；
- **失败按档位上一档**：`escalate_target()` 新增档位路径，`t2 → t3`，已在 `t3` 才落回 agent 阶梯；**永不向下**。

### 3.2 执行者档位：本机唯一真的改变能力的杠杆（只升不降）

`a1`（纯文本端点 claude/jcode）< `a2`（pi/omp/grok/hermes/codebuddy/cursor/dsh）< `a3`（claude-code/codex）。

- 默认只对 **难度 3** 生效：抬到 `a3`，且目标必须在**当前可用池**里，否则 `agent_floor_unavailable` 如实记；
- 难度 1/2 **一律不动** Leader 的选择——降级需要结果证据，自评不配；
- 来源落 `agent_promoted_from` 列（拆解期就记，与失败后的 `escalated_from_*` 是两个阶段、两个口径）；
- **汇总节点一律钉顶层**：自动补的 `t_sum` 与 Leader 自写的聚合（结构判据：deps 覆盖其余全部）
  都钉 `pinned_tier="top"`，模型与执行者**同时**钉。理由：汇总是"失败最贵、最难发现"的那一步，
  上游都对、汇总写歪，整条 run 的结论就废了，而下游看不出来。

## 4. 实测（这是本次最重要的发现）

### 4.1 模型档位在本机**当前等于空转**

对 5 个不同 ID 直连 CCR（`127.0.0.1:3456`，真实 key）各打一次 `max_tokens=5`：

| 请求 `model` | 回包 `model` |
|---|---|
| `alibaba/deepseek-v4.1-flash` | `agnes-2.5-flash` |
| `deepseek/deepseek-v4-pro` | `agnes-2.5-flash` |
| `alibaba/qwen3.8-max` | `agnes-2.5-flash` |
| `openrouter/openai/gpt-6.1-terra-pro` | `agnes-2.5-flash` |
| `agnes/agnes-2.5-flash` | `agnes-2.5-flash` |

CCR 目录里那次的备份名是 `config.json.bak-20261029_005625-agnes-fallback`，同期还出现
`LLM 网关 HTTP 429: All target ...`（上游限流）。**结论：只改 `model` 的档位映射在本机当前不影响能力。**

处置不是"把代码删了当作没写"，而是**把它变成可观测的**：`GET /api/tasks/tiers` 的
`model_tier_effective` 报 `planned / observed / rewritten / effective_rate`，
分母只取**有回包模型**的样本（没观测不算 100%）。这样"档位到底生效没有"从感觉变成数字。

### 4.2 执行者档位有效

影子 `:3200` 干跑（`dry_run=true`，不花钱）：

```
t1  agent claude-code  d=3  tier t3  promoted_from=jcode
t2  agent jcode        d=2  tier t2  （不动）
t3  agent claude-code  d=3  tier t3  promoted_from=jcode
t4  agent jcode        d=2  tier t2  （不动）
agent_counts: {'agent_floor:claude-code': 2, 'no_agent_floor': 2}
```

`GET /api/tasks/tiers` 的 `resolved` 三条：`d=1→t1/jcode`、`d=2→t2/jcode`、`d=3→t3/claude-code (from jcode)`。

## 5. 怎么证伪这个功能（半年后该看什么）

`GET /api/tasks/tiers`：

- `audit[]`：每个难度的 `n / first_pass_rate / retried / escalated / tokens`；
- `verdict`：**n≥10 的两档极差 < 0.1 ⇒ 明写"难度与首轮成功率无显著关联，该映射没有证据支持，应考虑删掉"**。
  样本不足时不给结论（5 条样本的比率是噪声）；
- `model_tier_effective.effective_rate`：长期接近 0 ⇒ 模型档位是死的，只保留执行者档位。

判据写进代码而不是文档，是因为文档会被忘；接口每次都在。

## 6. 遗留（明确未做）

- **难度自评的校准未做**：没有用历史 verdict 反推"自评 3 的真实成功率"，也就没有依据调 mapping。
  先积累 ≥30 条真实 run 再看 `audit`。
- **模型档位依赖 CCR 恢复多模型路由**：当前 fallback + 429 状态下无法验证真实效果。
- **`model` 列在本次影子实弹里出现 NULL**（jcode 任务 success 但未落实发模型），
  导致 `model_tier_effective` 的观测分母为 0。这本身是**独立缺陷**，需单独排查；
  本版只保证"没观测时不出假数"。
- 难度只影响起点，**不影响预算**（`budget_ms/tokens` 仍由调用方给）。
