# AI Agent Handbook（阿里云）融合评估与可采纳项详细方案

> **新建文件登记**（依工作区「文件修改铁律」）：本文件为**新建**，无需备份。
> 建立原因：用户 2026-10-14 要求对 `github.com/aliyun/ai-agent-handbook` 做「详细分析理解评估
> 是否可以部署融合进 agenthub」（`agenthub融合.md`），并追加一篇同源公众号文详读；经 Q1~Q3
> 提问后用户**逐条批准**（见下「授权」），要求产出本报告并**评估收益**。
>
> - 取证日期 **2026-10-14**；被测 `agenthub v0.13.103`、`git f8325bc6`、`:3102` `/health` ok。
> - 上游 `aliyun/ai-agent-handbook` sparse-clone pin **`e97f890d`**，暂存目录
>   `agenthub/work/handbook-eval-20261014/repo/`（`work/` 已 gitignore）。
> - 外部事实来自 GitHub API；公众号文来自 `mp.weixin.qq.com` 服务器渲染 HTML 直取。
> - **授权**：Q1 = **(a) MCP 知识通道 + (c) 方法论对标**；Q2 = **落 docs 报告 + 并行只读子代理**；
>   Q3 = **本轮只读**（不碰 `src`、不重启生产、不 git 写）。
> - **本轮全程只读**：未修改任何 agenthub 源码、未重启服务、未提交。（仅新增本 docs 文件 + `work/` 暂存）

---

## 0. 结论速览

| 维度 | 结论 | 关键数字 / 证据 |
|---|---|---|
| **能否「融合」** | **不能按组件融合，只能按「知识 + 方法论」融合**。手册是 VitePress 文档站，**代码层为零**（0 个 `.py`、0 条路径含 `mcp`） | 根树 349 项，237 项是 `assets`；仅 `.vitepress/*.ts|mts` + 2 个 `check-*.mjs` |
| **`agenthub融合.md` 立论** | 🔴 **核心前提被证伪**。该文称「Handbook 提供 MCP Server，双方均已支持 MCP，仅需配置连接」——上游**既无 MCP server 也无一行 Python**，首选方案建立在**不存在的组件**上 | GitHub trees API 逐路径核对 |
| **手册真实价值** | **内容层**：25+ 章企业 Agent 工程方法论（建/治/调三线），是 agenthub 的**对标清单** | 56 个 md，本次逐章提取出 **63 项可落地做法** |
| **最高收益项** | 集中在 agenthub **已知真缺口**：轨迹→数据集→经验库闭环（TDAI `tasksEnqueued:0`、L1 仅 4 行 38 天未更新、memindex 陈旧 18.5 天） | 与 2026-10-13《AMR 评估》**独立指向同一洞** |
| **建议动作** | 落本报告 + 一份 P0/P1/P2 采纳清单（§5）；知识通道走**零新增凭据**的本机 KB 路（§4），**不建议**写 handbook-mcp | 见 §4 选型表 |
| **明确排除** | 文档第二步「阿里云/计算巢云上部署」——与 agenthub「本机进程 + 终端控制台」定位**正面冲突** | 见 §7 |

**就地结论一句话**：手册**不是可集成组件**，是**方法论镜鉴**——它最大的用处是把 agenthub 已知的
「经验库空转」缺口**用外部命名（RAC / 三挡调优 / 黄金集）说清楚并提供一套可检验的做法**。

---

## 1. 取证与事实（已核实，非空谈）

### 1.1 上游形态

| 项 | 事实 | 取证 |
|---|---|---|
| 仓库 | `aliyun/ai-agent-handbook`，**Apache-2.0**，**1578★ / 233 fork**，created 2026-09-11，pushed **2026-10-14**（活跃） | GitHub API |
| 形态 | **VitePress 文档站（JavaScript）**，中文白皮书 25+ 章 | 树 349 项 |
| 代码 | **`.py` = 0**；路径含 `mcp` = **0**；无 server、无自托管路径 | 逐路径核对 |
| 内容分篇 | `00-preface / 01-architecture / 02-build / 03-run / 04-governance / 05-optimization / 06-case-study / 07-conclusion` + `2026-agent-survey-report.md` | sparse-clone |
| 与 agenthub 同类章 | 第 9 章 AI 网关、第 11 章 Multi-Agent 编排、第 13 章可观测性、第 15 章资产发现、第 19 章轨迹数据、**第 25 章研发小队** | 章节标题 |

### 1.2 证伪 `agenthub融合.md` 的核心前提

`agenthub融合.md` 是一份**未取证的 AI 生成评估**，其推荐路径（MCP 知识集成 / 计算巢部署）
**逐条与上游实际不符**：

| 该文主张 | 实测 | 判定 |
|---|---|---|
| 「Handbook 提供 MCP Server」 | 上游无任何 MCP server、无 Python | ❌ 证伪 |
| 「双方均已支持 MCP，仅需配置连接，开发成本低」 | agenthub 侧确有 MCP 客户端（`src/mcpgw.py`），但**对端不存在可连的 server** | ❌ 前提缺失 |
| 「技术栈：Node.js + MCP Server (Python)」 | 只有 VitePress/JS，**无 Python** | ❌ 与事实不符 |

> **教训**：外部 AI 生成的「融合评估」在动手前必须**逐条回源核对**（本机既有先例：凡未取证的
> 结论都必须在评估报告里降级为「待证」）。

### 1.3 agenthub 侧底数（实测 v0.13.103）

| 面 | 现状 |
|---|---|
| **MCP 通道** | `src/mcpgw.py` = MCP **客户端聚合网关**（官方 SDK，stdio/http，`/mcp/servers` CRUD + ACL + 限流 + probe）⇒ 接 server 是**注册**不是开发 |
| | `src/hubmcp.py` = agenthub **自身 MCP server**（`/hub-mcp/mcp`） |
| **知识库** | `src/kb.py` = 联邦检索门面（`local/tdai/turbovec/workspace/archived`），**刻意不建第二权威副本** |
| | turbovec **7300 chunks / 1.94MB**，索引**陈旧 18.5 天**（built_at 2026-09-26） |
| **记忆** | TDAI(:8420) L1 **≈376** 条 / L0 **≈10899** 条；本地 `memories` 表仅 **4 行**（37 天未更新） |
| **可观测** | `src/runlog.py`（v0.13.27）只记 `subject/status/duration_ms/detail`，**无 token / 无 cost** |
| **任务** | `src/tasks.py` status 仅 `pending|running|success|failed|blocked`，无等待态/预算/证据字段 |
| **测试** | `run_tests.sh all` **1587 例 0 失败 0 跳过** |

---

## 2. 手册全章可采纳项提取（方法论对标 · Q1-c）

**方法**：56 个 md 分四篇，派 **4 只只读子代理**并行提取（每章：做法 / 原文引用 / 对 agenthub 的落点 /
是否已有 / 工作量 / 价值）。原始产物在
`work/handbook-eval-20261014/extract/{build-architecture,run,governance,optimization}.txt`。

**汇总**：共 **63 项可落地做法**（另有 13 个导读/概念页判定「无可落地项」）。

| 篇 | 覆盖章 | 提取做法 |
|---|---|---|
| 架构 + 构建（00~02） | 9 | **13** |
| 运行（03） | 7 | **14** |
| 治理 + 结论 + 调研（04/07/survey） | 7 | **17** |
| 调优（05） | 9 | **19** |
| **合计** | **32** | **63** |

> **覆盖边界**：`06-case-study`（26 章，含第 25 章）**未逐章提取**——案例章是各企业落地叙事，
> 方法论密度低；其中第 25 章「研发小队」由公众号文详读单独覆盖（§3）。此为**如实标注的证据边界**。

### 2.1 架构 + 构建篇（13）

| # | 做法（摘） | agenthub 落点 | 工作量 | 价值 |
|---|---|---|---|---|
| A1 | **Agent Release**：发布单元 = 模型+参数+预算+Harness 配置+Prompt/Skill/Knowledge 版本+Tool 权限+评估基线，绑定可追溯 | 新增 release 汇总层：聚合 `modelcfg`/`profiles`/`mcpgw`/`skill.py`/`kb.py` 生成带 hash 的 manifest；`/api/agents` 显示 release id | M | 高 |
| A2 | ⭐ **任务契约（Agent Contract）先于框架**：goal/inputs/outputs/allowed/forbidden/approval/acceptance | `src/profiles.py` 每个 agent 补可机读 Contract 段，作启动参数与 ACL 判据；缺失标 `unknown` 而非默认放行 | M | 中 |
| A3 | 多源纳管三条件：版本归属可追溯、Event/Artifact/Trace 关联 Task+Identity、**远程 Agent 不能只回自然语言** | `mcpgw`/`hubmcp` 注册项补 `version+owner`；`mcp_call` 返回带结构化 result+artifact；`profile_events` 增 task 关联 | M | 中 |
| A4 | ⭐ **显式任务状态机**（WAITING_INPUT/APPROVAL/EVENT/PAUSED/VERIFYING/CANCELLED）+ 多维预算 | 扩 `src/tasks.py` status 枚举与 `tasks` 表，加 budget 字段；等待态可释放执行资源 | M | 高 |
| A5 | ⭐ **完成由证据判定、模型只能申请**：Verifier 返回结构化缺口与可修复性 | `tasks.py` 不再由 runner 直接置 `success`；加 `completion_evidence`（artifact/命令输出/hash），按 kind 配 Verifier | M | 高 |
| A6 | 副作用行动重试前先问「上次有没有发生」：幂等键 + 结果留痕 + 先查外部状态 | `cronjobs.py` 三类 job 与 `mcpgw.mcp_call` 写路径加 `idempotency_key` | M | 中 |
| A7 | ⭐ **Context Manifest**：每次模型调用记录来源 id/作用域/版本/可信度/选择理由/token/哈希/排除项 | `memory.injection_context` + `kb.kb_search` 返回加 `manifest` 段，哈希写进 runlog | M | 高 |
| A8 | 工具大结果外置引用化：原始落 Artifact，上下文只留摘要 + 稳定引用 | `mcpgw.mcp_call` 加结果体量阈值 → artifact_ref + 分页读回 | M | 中 |
| A9 | **Skill 当软件资产管理**：owner/作用域/版本/依赖/权限/支持模型/测试/弃用 | 扩 `src/skill.py` + Draft→Test→Review→Publish→Deprecate 状态机 | M | 中 |
| A10 | ⭐ **统一 Action Plane**：行动归一为 Action Request/Result + 生命周期 | `mcpgw` 外增加 Action 层，ACL 降为 policy decision point | M | 高 |
| A11 | 端到端 **Trace 因果链**（模型/版本/Manifest/状态/Action/Artifact/Verifier/Outcome 同一 trace） | 统一 `runlog`/`asset_audit`/`tasks` 到 `trace_id`，提供聚合只读视图 | L | 高 |
| A12 | ⭐ **权限三态 ALLOW/DENY/ASK** + Preview–Approve–Commit–Verify | `src/writeauth.py` 加 ASK 通道（skill_install/modelcfg/写工具/task kill），复用 modelcfg 已有预览+备份工序 | L | 高 |
| A13 | 模型路由/降级变更须与**完整 Harness 一起回归**，协议兼容 ≠ 能力等价 | modelcfg 写入后接 release 级复合探针（真文本 + 真工具调用） | S | 中 |

### 2.2 运行篇（14）

| # | 做法（摘） | agenthub 落点 | 工作量 | 价值 |
|---|---|---|---|---|
| B1 | ⭐ 恢复会话必须**校验工作目录/归属**，不过就保持失败，不得把空/错目录当恢复成功 | `src/term.py`/`sessions_store.py`：`session_cwd()` 目录消失时**静默退回画像目录**（README v0.13.0 行为）⇒ 加 `Path(cwd).is_dir()` 校验，失败拒绝起会话 | S | 中 |
| B2 | 回收/终止资源**不得以 CPU 低**为单一依据 | `src/resources.py` 杀进程补「仍在活跃工具调用/未确认副作用」双判据 | S | 中 |
| B3 | 失去心跳 ≠ 进程已停；恢复前须确认旧实例已停 | `vitals.py` 探针失败与 `term.py` 会话存活分离：超时标 pending/unknown 而非 stopped | M | 低 |
| B4 | ⭐ 检索结果标注**对象版本/索引时间/来源**，区分「答案过期」与「检索失败」 | `kb.py` `backends[]` 补 `as_of`（turbovec mtime / TDAI L1 最新 created_at）；`memory.py` 逐条标 source+时间 | M | 中 |
| B5 | 记忆召回加**时近性权重**，但只影响排序、不对长期约束硬过滤 | `memory.py::_rrf_fuse` 加按条目时间的衰减项，规则/人设类不打折 | M | 中 |
| B6 | ⭐ **错误分类先于重试**：429 读 reset、401 终止、超限交 Harness 压缩而非静默截断 | `src/llm.py`（240s 总超时、无流式）加重试前先分类 | S | 中 |
| B7 | 重试预算**跨层传递**，避免 SDK×网关乘积放大，留可归因 attempt 记录 | `llm.py` + CCR 适配层：一次逻辑调用发 `call_id`+绝对 deadline，尝试用 `attempt_id` 落 runlog | M | 中 |
| B8 | 协议适配统一接口须**先声明能力子集**，不支持参数显式报错不静默丢弃 | `src/adapters/openai_compat.py` Anthropic↔OpenAI 字段转换：无法无损映射即报错/标降级 | S | 中 |
| B9 | 定时触发须有**稳定 occurrence 标识 + 错过策略**，避免重启重复/丢跑 | `src/cronjobs.py::_tick_loop` 现仅比 `last_run`；补 misfire（skip/run_latest）+ occurrence id 写 `profile_events` | M | 中 |
| B10 | 单次状态查询失败**不能推断任务异常**；异常判定基于租约/心跳与权威状态对账 | `tasks.py` sweeper 与 `vitals.py` 探针失败路径：改二次确认再转终态 | M | 中 |
| B11 | ⭐ 交接上下文**四段式**（目标/已知事实/待处理/可用引用），未验证中间推理不传递 | `tasks.py` 上游注入现为 `UPSTREAM_TRIM=4000` **整体截断** ⇒ 改四段式 handoff | M | **高** |
| B12 | ⭐ 把「执行者提交成功」与「审核方接受」**分开**，提交只是候选成果 | `tasks.py` 补 `submitted/accepted` 两级，上游验收与下游就绪分离 | M | 中 |
| B13 | 团队级**停止条件**（最大委派深度/派生数/返工轮次/连续无新产物）须在根 Task 前约定 | `tasks.py` 已有 `MAX_TASKS=12`+深度限制，补返工轮次与「多轮无新产物」判据，触顶标 blocked | M | 中 |
| B14 | 持久通流须有**可恢复续传点**（通道+序号），断连从最后确认位置续接 | `term.py` `/api/term/recording/{sid}/frames` 补 `after_seq`（`term_record` 已按 seq 存） | S | 中 |

### 2.3 治理 + 结论 + 调研篇（17）

| # | 做法（摘） | agenthub 落点 | 工作量 | 价值 |
|---|---|---|---|---|
| C1 | ⭐ 审计**分层链路**：原始事实→候选信号→研判→已确认；研判三态，`severity`/`confidence` 分开，证据不足不得当安全结论 | `src/audit.py`+`db.py`：`asset_audit` 加 `verdict(confirmed|dismissed|insufficient)` 与两列，`insufficient` 不走放行 | M | 高 |
| C2 | **检测与拦截分开建模**：实时阻断须确定/可解释/可回放，并有影子运行/灰度/回滚 | `mcpgw` ACL 规则加 `effect(enforce|shadow)`，新规则先 shadow 记「本应拒绝」 | M | 中 |
| C3 | **统一 Agent 执行语义枚举**作跨框架公共词表 | `runlog`/`adapters` 为 pi/claude/codex/codebuddy 写事件名→枚举映射（现仅 4 个粗粒度 source） | S | 中 |
| C4 | 能力资产接入/更新做**准入评估**：实现、描述、参数定义、版本变更都视为行为变更 | `githubprojects.py`+`asset_audit`：纳管外部项目记来源/版本/许可证/摘要，描述或参数变更不得只 bind 生效 | M | 中 |
| C5 | **出站默认拒绝**，封禁云元数据端点与内网保留段（防 SSRF） | `mcpgw` server 注册/调用补出站校验；复用 `ghsettings.py` 已有 `ipaddress` 拦截函数 | S | 中 |
| C6 | 外部内容（网页/邮件/工具返回）**保留来源、当数据层**，不得升级为执行要求 | `kb.py` 与注入装配处：片段带来源+时间戳+「数据非指令」边界标记 + 冲突检查 | M | 中 |
| C7 | ⭐ 外部资产**可信准入六阶段**：候选区（对生产不可见）→来源核实→摘要→静态检查→隔离验证→人工评审→发布 | `githubprojects.py` 前置候选态，落 `agenthub/work/`（**禁落 /tmp**），逐项留证 | L | 高 |
| C8 | ⭐ 资产摘要须覆盖**完整能力包**（SKILL.md+scripts+templates+references），只算说明文件会漏脚本变化 | `skill.py`/`asset_audit` 加 `sha256(整包)`+文件清单（现**无任何 digest 字段**） | S | 中 |
| C9 | **Context Manifest**（调用实际解析出的资源组合+版本+摘要）落可查询记录用于回放归因 | `runlog`/`profile_events.detail` 会话/任务起始记一条 manifest | M | 中 |
| C10 | ⭐ 一次运行拆**四个独立维度**（执行状态/仿真有效性/证据完整性/任务质量），不压成单一成功失败 | `run_tests.sh`/闸门产物：结果对象改 `exec_status + validity + evidence + quality` 四字段；**与既有「禁把 SKIP 当 PASS」同向且可断言** | M | 高 |
| C11 | ⭐ 证据分三层（records→observations→evidence）+ 三检查，不足输出「不可判定」 | `audit.py` 与判定层：原始输出单存不改写，显式产出 `insufficient` 并计数 | M | 高 |
| C12 | ⭐ **强制边界 vs 观测边界**：只有覆盖全路径 + fail-closed + 负向测试证明绕过被拒，才可称强制 | `profiles.py` 纳管矩阵 + scanner：每 agent 标四维；未过负向测试前只能写「未观察到写操作」 | M | 高 |
| C13 | **Evidence 三状态**（planned/sent/confirmed）：已发出未返回先查状态再决定重试/补偿 | `profile_events.detail` 加 `phase`；重试前读 `sent` 记录核实 | M | 高 |
| C14 | 工具元数据补**三类可机读声明**：有无外部副作用/是否幂等/失败可否安全重试 | `mcpgw` 工具登记加 `side_effect/idempotent/retry_safe` 三列 | S | 中 |
| C15 | 评估从**人工抽查转向轨迹自动评估**，量化任务成功率 | `hook.py` 已有 `profile_events` 聚合，在其上做轨迹级回归集 + 成功率 | M | 中 |
| C16 | **成本归因**按模型/调用方聚合 token 与费用 | `runlog`/`profile_events` 加 token/cost（或对接 CCR usage 账本），按 agent 聚合 | M | 中 |
| C17 | 工具瓶颈不在数量而在**描述质量**：统一描述模板 + 校验 + 按任务裁剪工具集 | `skill.py`/`mcpgw` 清单补结构化描述 + 完备性校验 + 按任务裁剪（现只截断 description 到 160 字符） | M | 中 |

### 2.4 调优篇（19）

| # | 做法（摘） | agenthub 落点 | 工作量 | 价值 |
|---|---|---|---|---|
| D1 | ⭐ 用**单位成功任务成本**（总成本÷成功数，失败消耗计入分子）衡量收益，固定任务构成并同报成功率 | `runlog` 现无 token/任务粒度 ⇒ 先只做只读聚合脚本（`src` 只读、禁 crontab） | M | 中 |
| D2 | ⭐ **失败归因三层判据与顺序**：绕过模型仍复现=环境；只改上下文/协议即好转=Harness；信息动作反馈俱全仍复发=模型 | `vitals.py`/`healthx.py`/`hublog.py` 给失败任务加 `env|harness|model` 标签，作 `modelcfg` 换模型前置闸门 | S | 中 |
| D3 | ⭐ 真实运行事实整理成**三类可复用产物**：样本/标准/可验证改动 | `db.py` 增 `dataset/dataset_item/eval_run` 三表 + 离线 pipeline | L | 高 |
| D4 | 闭环各环节**互相关联**（轨迹↔样本↔评分↔版本），否则追得到结果追不到原因 | `dataset_item` 带 `trace_id/session_id/source_ref`；`eval_run` 带 `dataset_version`+被测配置 hash | M | 高 |
| D5 | 用**统一轨迹结构**组织行为主干（步骤/消息/工具调用+观察/指标/来源） | 定 trajectory 行 schema，复用 `term_record`/`sessions_store` 已有部分，新表统一 | L | 高 |
| D6 | 区分 **Trace/Session/Trajectory**；SessionID 不能证明同任务 | 会话↔任务用显式 `task_id`，禁按时间邻近/文本相似推断 | S | 中 |
| D7 | 候选集**最小字段集**：输入/输出/轨迹；写入时核对字段映射，别把最终答案与轨迹接反 | `dataset_item` 起步列 + 显式映射 + 断言入闸门 | S | 中 |
| D8 | 整理方法存成**声明式 Pipeline**，按周期持续生成 Dataset | JSON 管线定义（source/modules/schedule/output），排程走 systemd user timer 或端点触发（**禁写 crontab**） | L | 高 |
| D9 | 处理前先明确**「一行代表什么」**（grain），决定分组/采样/评分口径 | `dataset_item` 加唯一键（task 级 trace_id 或 (session_id,turn)） | S | 高 |
| D10 | 处理顺序**按成本安排**：先确定性过滤/采样，最后才调模型 | 经验挖掘管线先过滤采样，非会话调用**强制 `openrouter/free`、禁 `openrouter/auto`** | M | 中 |
| D11 | 黄金样本起步字段：题目/原回答/参考答案/**逐题 rubric**/场景/来源 | `dataset_item` 列名照手册；rubric 用 TEXT 存「逐题」判据 | M | 高 |
| D12 | ⭐ 参与改写 Prompt/生成 Skill 的题必须与独立检查的题**分 hold-out** | `dataset_item` 加 `split(train|holdout)`，优化批次不得纳入 holdout | S | 高 |
| D13 | 比较前创建**数据集版本**，比较期间不追加/修改 | `dataset_version` 只读快照表，`eval_run` 引用 `version_id` | M | 中 |
| D14 | 评估器分三类：格式/必填走 **Code 规则**；切题/遗漏走 **LLM**；多步与「工具结果 vs 最终回复一致」走 **Agent**；**禁简单平均** | 评估层定义 `evaluator_type`，先做 Code 规则（读 `runlog` status/detail），Agent 评估后置 | L | 中 |
| D15 | 第一轮只改**最有依据的一项**；验收看三件事（严重错误消失/原正常无退化/耗时消耗可接受）+ 独立样本独立会话 | 实验记录 `baseline/candidate` + 变更项白名单（只一项）；三看写入 `eval_run` | M | 中 |
| D16 | 评估结果**写回数据集**供人工复核，形成下一轮实验用题 | `eval_run` 回写 `dataset_item`（badcase 池），透传 trace/input/output | M | 中 |
| D17 | ⭐ 建**经验库**（情况→处理 + 适用条件），入库前抽查三问并回源 Trace 核对 | 补缺口②：TDAI L1 空转 ⇒ `experience{situation,action,precondition,source_trace_ids}`，离线挖掘（非会话用 `openrouter/free`），落 db 表 + scripts | L | 高 |
| D18 | 经验接入后做**一次真实召回验证**：开关=on 不算证据，须留真实命中+来源取证；失败按四类分现象 | `memory.py`/`memfed.py`：经验库作新一路源，须留召回命中取证，禁反复调阈值 | M | 高 |
| D19 | ⭐ 召回策略本身用**实验选**：留 Baseline（不用经验），新会话+重置依赖状态对比 | 注入加 on/off 与 top-k/阈值参数；判据回业务（根因正确/无效查询减少），开销计入单位成功任务成本 | M | 中 |

---

## 3. 第 25 章「15 个 Agent 研发小队」对标（公众号文详读）

> **出处**：公众号「云原生（阿里云云原生）」，标题《15 个 Agent 组成研发小队：多智能体协作的端到端
> 研发体系》，`ct` = **2026-10-14 10:50 CST**；正文 6,091 字已提取（`work/` 暂存），9 张配图下载。
> **同源**：与手册 `06-case-study/第25章 研发效能/多 Agent 组成研发小队…md`（8,421 B）**同一案例**，
> 公众号版更全（多出 33%→70%、手动挡/半自动挡/自动挡、RAC、19 题结果）。**性质：产品营销文**——
> 推介 **AgentCore（运行面）+ AgentLoop（优化面）** 两个**托管服务**，无代码、无 MCP、无自托管路径。

**文章主张（摘要）**：编码提速 ≠ 交付提速（20 工作日 / 编码仅 2 天；10× 编码提速仅降到 18.2 天）；
AgentCore 调度 15 个分工 Agent，任务状态跨 Agent 保留，反复不收敛 → Human Gate；
AgentLoop 采 Trace/Session/Trajectory → Pipeline→Dataset→Evaluation→Experiment，分**手动挡/半自动挡/自动挡**，
自动挡靠 **RAC 经验库**自沉淀+注入；四类持续问题（架构约束未传递/开放输入超预期/同坑反复踩/**修一处退化他处**）。

**证据强度**：全部厂商自报、无一可复现（33%→70% 无出处；20→18.2 天是算术示例非实测；
19 题只报 10+5，其余 4 题与「约」未交代；无 Rubric / 无数据集 / 无仓库）。「1 月→1 周」含**选择偏差**
（同文自述先限定任务范围与验收条件）。

**对 agenthub 的映射（结论：已有大半、缺一环）**：

| 文章概念 | agenthub 现状（实测） | 判定 |
|---|---|---|
| Leader/Worker 调度 | 多 agent 纳管 + 派发（Agent/dispatch） | **已有** |
| 实现/验证分离 | superpowers `subagent-driven-development` + `verification-before-completion` | **已有** |
| Human Gate | AGENTS.md §2：探活 ≤2 次 / 判据重试 ≤2 次 / 失败即上报 | **已有（更严）** |
| 回归 Benchmark | `run_tests.sh all` **1587 例 0 失败** + 各闸门 | **已有** |
| Trace 留痕 | `src/runlog.py`（只记「谁调用、多快、哪路降级」） | **部分** |
| **轨迹→数据集→经验库（RAC/自动挡）** | **缺**：TDAI `tasksEnqueued:0`；L1 仅 4 行 38 天未自动更新；memindex 陈旧 18.5 天 | **真缺口** |

🔴 **关键交叉结论**：文章称最有价值的「经验库持续积累」，正是 **2026-10-13《AMR 评估》** 已定位的
**同一个洞**——AMR §3「写入侧完全没有自动化」、§6「Dreaming 职责定义正好激活那个 `tasksEnqueued=0`
的空转 scanner」。**两篇独立来源指向同一缺口，可信度互相加强。**

- **不可抽取**：15-agent 编排器本体、AgentCore/AgentLoop 服务、钉钉群（闭源托管，NAS 不可部署）。
- **可抽取**：① 经验库/反模式沉淀（＝AMR 的 Dreaming，落 agenthub 定时任务而非新表）；
  ② Trace→Trajectory 样本化；③「专家 commit 反造需求」离线 Benchmark 法；④「架构约束随任务传递」强化。

---

## 4. 详细方案 A —— 手册知识通道（Q1-a）

**目标**：让 agenthub 内的 agent 能检索/引用手册内容。**上游无 MCP server**，故需先选"承载方式"。

| 方案 | 做法 | 开发成本 | 时效 | 耦合 | 凭据 | 判定 |
|---|---|---|---|---|---|---|
| **A-① 写 `handbook-mcp`** | 本地 clone + rg/FTS → MCP resources/tools，注册进 `mcpgw` | 中（须自研 + 维护） | 需手动 sync | 中 | 零新增 | 可选，**非首选** |
| **A-② 走本机 KB 路** ⭐ | clone 进 `work/` 或工作区 → 走 `workspace`/`turbovec` 联邦检索 | **低**（近零，注册 + rebuild） | 需 rebuild 索引 | **低** | 零新增 | **推荐** |
| **A-③ UI 加「手册」面板** | 静态镜像 VitePress 站 | 中 | 静态 | 中 | — | 可选（收益低） |
| **A-④ 当对标清单** ⭐ | 不集成，纳入方法论审查（= §5 清单） | **零** | — | 零 | — | **已在本报告落地** |
| **A-⑤ 云上部署** | 阿里云/计算巢 | — | — | — | — | **明确排除**（§7） |

**推荐组合 A-② + A-④**：A-④ 已经在本次报告完成；A-② 的落地是「把 clone 进工作区 → 让 turbovec
索引纳入 handbook 章 → `kb.py` 联邦检索天然可查」，**零新增凭据、零新增服务**，符合本机「唯一权威
副本 + 零新增凭据」偏好。**不建议**先写 `handbook-mcp`：对端内容变化慢、单册 1.7MB，自研 server 的
维护成本高于收益（撞 §4「按净收益裁定」判据）。

---

## 5. 详细方案 C —— 方法论采纳清单（Q1-c，P0/P1/P2）

> 从 §2 的 63 项中按「真缺口 + 高价值 × 低成本」排序。**本轮只读，不落地**；这是**待批的施工清单**。

### P0（真缺口 / 高价值，建议优先）

| 编号 | 项 | 对应手册做法 | 治什么（现状数字） |
|---|---|---|---|
| **P0-1** | **轨迹→数据集→经验库闭环**：`dataset/dataset_item/eval_run/experience` 四表 + 离线 Pipeline + 经验挖掘脚本 | D3/D4/D5/D8/D11/D17 | TDAI `tasksEnqueued:0`、L1 仅 4 行 38 天未更新、memindex 陈旧 18.5 天 |
| **P0-2** | **runlog 加 token/cost**（对接 CCR usage 账本）与任务粒度 | D1/C16/D2 | 现无 token/cost ⇒ **成本归因与「单位成功任务成本」目前不可度量** |
| **P0-3** | **任务状态机 + 预算 + 证据判定**：扩 `tasks.py` 枚举与表，完成须过 Verifier | A4/A5 | 执行者自证 `success` 即解锁下游；无等待态/预算 |
| **P0-4** | **审计/证据分层 + 不可判定态**：`asset_audit` 加 `verdict`+`severity`/`confidence`；测试结果四维 | C1/C10/C11 | 与既有「禁把 SKIP 当 PASS」红线同向，且**可断言** |
| **P0-5** | **强制边界 vs 观测边界**：纳管矩阵四维字段 + 负向测试 | C12 | 治「夺管 ≠ 可用 / 只读未证」的旧账（09-23 首扫假阴性） |

### P1（高价值中成本）

| 编号 | 项 | 对应 |
|---|---|---|
| P1-1 | **Context Manifest**（调用实际资源组合+版本+摘要） | A7/C9 |
| P1-2 | **权限三态 ASK + Preview–Approve–Commit–Verify** | A12 |
| P1-3 | **交接上下文四段式**（替换 `UPSTREAM_TRIM=4000` 整体截断） | B11 |
| P1-4 | **提交/接受分离**（`submitted/accepted`） | B12/B13 |
| P1-5 | **Skill 资产管理**（owner/version/scope/状态机 + 整包 digest） | A9/C8/C7 |
| P1-6 | **幂等键 + 重试分类 + attempt 记录** | A6/B6/B7/C13/C14 |
| P1-7 | **会话恢复工作目录校验**（`session_cwd` 静默退回画像目录 → 显式失败） | B1 |
| P1-8 | **检索结果版本/时间标注 + 时近性权重** | B4/B5 |

### P2（分阶段/大改造，先立项再动）

- **Trace 因果链统一**（A11）、**统一 Action Plane**（A10）、**Agent Release manifest**（A1）
- **外部资产准入六阶段候选区**（C7）、**出站 SSRF 防护**（C5）、**管理剧本 `shadow` 灰度**（C2）
- **统一执行语义枚举**（C3）、**工具描述质量 + 按任务裁剪**（C17）、**续传点 `after_seq`**（B14）

> **注**：P0-1 与 2026-10-13《AMR 评估》的 P1「Dreaming」**是同一件事**——本次是**第二份独立来源**，
> 建议两案合并为一条 `PT-` 台账项，避免重复立项。

---

## 6. 收益评估

| 项 | 现状（可量化） | 采纳后收益 | 可度量性 |
|---|---|---|---|
| P0-1 经验库闭环 | TDAI L1 **4 行 / 38 天**未更新；`tasksEnqueued:0`；memindex 陈旧 **18.5 天** | 记忆/经验从「只进不出」转为「持续沉淀 + 可召回」；跨 agent 经验可复用 | 复发率、根因正确率（需 P0-2） |
| P0-2 token/cost | runlog **无 token / 无 cost** | 首次可算「**单位成功任务成本**」，优化收益可度量 | 直接可算 |
| P0-3 状态机/证据 | status 仅 5 态；执行者自证 `success` | 区分「执行慢/审批慢/工具慢」；**完成由证据判定**，减少假完成 | 假完成率、等待时长 |
| P0-4 证据分层 | 测试/审计结果压成 PASS/FAIL | 「**不可判定**」成为一等公民（禁把 SKIP 当 PASS 的**可断言化**） | 判定误报率 |
| P0-5 边界分级 | 纳管矩阵无强制/观测字段 | 「夺管 ≠ 可用」的旧账获得**可检验分级** | 负向测试通过率 |
| P1-* | 见 §5 | 可解释性（Manifest）、安全（ASK/SSRF）、正确性（恢复校验/交接上下文） | 各项有对应埋点 |

**方法论收益（不可零成本量化但重要）**：手册为 agenthub 提供了一套**外部命名与判据**
（Agent Contract / Action Plane / Context Manifest / Evidence 三态 / 强制 vs 观测边界 / 黄金集 / RAC），
使既有做法从「本机惯例」升级为「有外部对标的工程判据」；同时**证伪了 `agenthub融合.md` 的伪前提**，
避免在不存在组件上投入开发。

---

## 7. 风险、边界与明确排除

- **明确排除（记录理由）**：手册第二步「阿里云/计算巢云上部署」——与 agenthub「本机进程 + 终端
  控制台、单用户 NAS」**定位正面冲突**；且撞 09-23「禁重启生产」、根分区约 95% 等物理约束。
- **证书/许可**：手册 Apache-2.0（宽松），可安全引用；**无 copyleft 传染风险**。
- **证据边界（如实标注）**：
  - `06-case-study` 26 章**未逐章提取**（仅第 25 章经公众号文覆盖）；
  - 9 张配图**图内数据未读全**（当前模型不支持图片，`tesseract` 仅 `eng` 无 `chi_sim`）；
  - §2 各章「原文引用」由子代理逐条回源核对，但**未逐条二次人工复核**；
  - 上游为**活跃仓**（10-14 仍在 push），本报告绑定的 `pin=e97f890d`，后续版本可能漂移。
- **未做**：本报告**未落地任何代码/配置**，**未改 `src`、未重启生产、未 git 写**。

---

## 8. 待裁定（均未擅动）

1. **P0-1~P0-5 是否立项施工**？各需先报请可写路径与授权形态（`src` 只读 + 禁 git 写 + 禁重启生产）。
2. **是否把 P0-1 与《AMR 评估》P1「Dreaming」合并为同一条台账项**？
3. **手册知识通道是否按 A-②（本机 KB 路）落地**？若要落，需授权一次 turbovec rebuild（约 1800 秒窗口）。
4. **P0-2 的 CCR usage 账本对接口径**：账本 `logical_model` 是内部逻辑名非客户端 ID（既有教训），
   需先确认可用的聚合维度。

---

*报告完毕。本轮为只读评估：仅新增本 docs 文件与 `work/` 暂存；agenthub 源码、服务、git 均未触动。*