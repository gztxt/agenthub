# 参考情报（refscout）P0 验证报告

- 日期：2026-10-21
- 范围：只读 spike，不接生产、不改任何 agent 配置
- 脚本：`scripts/refscout_spike.py`
- 对应台账：`PT-20261021-04`

## 1. 这一期要回答的问题

1. GitHub 上真的能稳定找到“功能/实现/设计最接近”的项目吗？
2. 单一检索臂够不够？多臂 + 分桶 + 三轴分能否把噪声压下去？
3. 语义重排（LLM）是不是必需？免费档能不能扛住？
4. 单次调研的额度、时延、可复现性是否可接受？

## 2. 做法

管线：`问题 → 查询扩展 → 多臂检索 → 去重分桶 → 硬闸门 → 三轴打分 → LLM 重排 → Markdown 卡`

- 主检索臂只用 `in:name,description,topics`（不用 `in:readme`）。
- 另设 fresh 臂（`pushed:>180d`）、中文臂、代码臂（`in:file`）、清单臂（`in:readme`，只进 list/doc 桶）。
- 硬闸门：`archived` 丢弃；无 license / `NOASSERTION` 标红；GPL/AGPL 只给设计参考。
- 三轴：功能 / 实现 / 设计，各出 0~1 分；排序权重 `0.6/0.3/0.1`（重排后 `0.5/0.3/0.2`）。

## 3. 实测结果

### 3.1 检索精度（人工判定，单一评审）

| 用例 | 模式 | top5 相关 | 明显噪声 | 耗时 |
|---|---|---:|---|---:|
| 终端 WebSocket 与端口转发 | 多臂 + LLM 重排 | **5/5**（termpair / webterminal / gotty / ttyd / butterfly） | 0 | 26.8s |
| 同上 | 仅词法（重排失败时） | 4/6（webterminal / termpair / gotty / clsh） | TradingView 策略工具、directgate（明确写“no port forwarding”） | 13.8s |
| 技能中心 技能扫描与调用记账 | 多臂 + LLM 重排 | **4/5**（Observal / mcp-gateway-registry / archestra / skillhub） | agent-skills 偏“清单” | 38.7s |
| 同上 | 仅词法 | 4/6 | `creativetimofficial/ui`、`deer-flow` 属“agent”一词误召回 | 13.9s |

> 判定口径：候选是否真的在解决同一个问题，而不是“名字里有同一个词”。单评审、无盲评，结论只对这两个用例成立。

### 3.2 关键反例（证明单臂不够）

- 按 stars 排：`"port forwarding"` 的 top1 是 2.2 万星的 awesome 清单，第 2 条是 README 偶然命中。
- 按 updated 排：top3 全是 0~1 star 的作业仓。
- `in:readme` + `websocket pty`：top1 是 9.6 万星的 MCP 清单。
- 收紧到 `in:name,description,topics` 后：`terminal websocket pty` 仅 9 条，全部相关。

### 3.3 额度与稳定性

- 单次调研搜索调用 5~7 次（仓库搜索 ≤5、代码搜索 ≤2），**0 次 403/429**。
- GitHub 实测额度：core 5000/h、search 30/min、code search 10/min。
- 缓存：同一输入二次运行检索耗时 **0.0s**（全部命中本地缓存）；但缓存目录 **38MB**，因为把 README/目录树也落了盘——P1 必须只缓存搜索响应。

### 3.4 LLM 重排的真实可靠性（负面结论）

小样本实测（n≈6 次调用，不作稳定结论）：

| 路由 | 观察 |
|---|---|
| `openrouter/openrouter/free` | 1 次成功；其余为 `only-thinking`（思考块吃满 token，无正文）或超时 |
| 具体 `...:free` 模型 | 2 次均只返回 `thinking`，无正文 |
| `alibaba/qwen3.8-flash`（付费快档） | 1 次 11.8s 成功且质量好；后续 2 次 45s 超时 |

**结论**：重排不能放在同步链路里当硬依赖。P1 必须做成异步任务 + 熔断/重试 + 结果缓存，并允许“仅词法”降级；降级时卡上必须显式标注“未重排”。

## 4. 已验证可用的部分

- 多臂检索 + 分桶：awesome 清单不再进实现对比。
- 三轴分与理由：每一分都能回溯到 `coverage / name_bonus / negation / path_hits / arch_docs`。
- 许可证闸门：GPL-3.0、LGPL-3.0、NOASSERTION 均正确标红。
- 固定引用：`repo@sha` 可用；代码搜索结果给 `blob/<branch>/<path>` 文件级链接。
- 逐路表态：每个臂返回 `ok/count/ms/error`，重排失败原因可读（`only-thinking` / `bad-json` / `TimeoutError`）。

## 5. 未达标 / 需要 P1 修的

| 问题 | 证据 | P1 处置 |
|---|---|---|
| 重排是同步阻塞 | 失败时单次 43~72s | 改成后台 job + 轮询；卡片先出“词法版”，重排完成再更新 |
| 重排模型不稳 | 见 3.4 | 免费优先、付费需显式开关；失败即降级并标注 |
| 缓存 38MB | `du -sh work/refscout-cache` | 只缓存 search JSON；README/tree 设短 TTL 或不缓存 |
| 行级 permalink 未做到 | REST code search 不返回行号 | 二期（B 方案）浅克隆后补 `#Lx-Ly` |
| 单评审 | 本文判定 | P1 用 5 个模块做一次人工标注集，算 top5 相关率 |

## 6. 下一步（P1 计划）

1. 把 `scripts/refscout_spike.py` 收敛成 `src/refscout.py`（stdlib + hub 现有 `ghsettings`/`llm`/`cronjobs`）。
2. 端点：`POST /api/ref/scout`（返回 job id）、`GET /api/ref/cards`、`GET /api/ref/cards/{id}`、`POST /api/ref/cards/{id}/refresh`。
3. SQLite 两张表：`ref_jobs`（状态/错误/模型/耗时）、`ref_cards`（模块/关键词/as-of/候选 JSON/证据）。
4. 重排走 `resilience` 熔断；免费失败 → 标注“未重排”，不阻塞卡片产出。
5. 闸门用例：分桶、许可证、否定语境、缓存命中、重排降级、失败逐路表态。
6. 验收：5 个模块 × top5 ≥ 3 相关；P95 ≤ 90s；0 次 429；卡片 100% 带 as-of 与 license。

## 7. 成本声明

- 默认路径（`--no-rerank` 或免费档成功）：**零付费调用**。
- `--allow-paid` 时使用 `MANAGER_LLM_MODEL`（当前 `alibaba/qwen3.8-flash`），每次约几千 token，按量计费；卡片会显式标注“付费档”。
