# ORCA-FUSION-EVAL-20261024 — stablyai/orca × agenthub 编排架构对比与融合评估

> **评估日期**：2026-10-24（Asia/Shanghai）
> **对象版本**：Orca = `stablyai/orca` main 分支 `docs/readme/README.zh-CN.md`（当日抓取）；
> agenthub = v0.13.148（`src/main.py:105`，生产 boot sha `4ece948c`）
> **方法**：只读取证（README 全文 + agenthub 源码实读 + 编排闸门实跑）+ 接单前置四查一简报。
> **性质**：评估文档，**不含任何代码/配置改动**；执行任一融合项前须走 §7 报请路径。
> **判例遵循**：`wiki/concepts/开源项目融合评估框架.md`（五层栈评估、禁强行合并）、
> `agent-knowledge/156-agenthub自评复核…`（评估类输入先逐条对账再采信）。

---

## 一、结论摘要（TL;DR）

1. **Orca 与 agenthub 不在同一层，不构成竞争，构成互补**：Orca 是**桌面 GUI 编排器**
   （人盯单屏、驱动 CLI coding agent 干装修活）；agenthub 是**无头 Web 管理平面**
   （服务化常驻、管理多机器上的多 agent 生态）。两者交集只在「编排」一词。
2. **可融合，但仅限「设计思想移植」，不做代码库合并**：Orca 是 Electron/TypeScript 桌面应用，
   agenthub 是 Python/FastAPI 无头服务。按《开源项目融合评估框架》「合并代码库导致维护多
   技术栈（TS+Python），维护负担爆炸」⇒ **代码零合并**，只借机制。
3. **最值得借的一条**：Orca 的「**同一提示并行分发给多个 agent、各自隔离 worktree、
   比较结果、合并最佳**」。agenthub 现有 DAG 是「一个子任务绑一个 agent」
   （`src/tasks.py:391` 节点格式 `{"id","task","agent_id","deps"}`），**结构性缺失同题多变体**
   ——这是唯一「Orca 有、agenthub 没有、且 agenthub 三件套（DAG + verifier + judge）
   天然能接」的能力。
4. **次选三条**：diff 行批注回注执行 agent（§5-B）、任务完成通知推小程序桥（§5-C）、
   CLI 脚本化补 worktree 子命令（§5-E）。
5. **明确不借**：移动 companion（已有 WorkBuddy 小程序桥）、终端分屏（term.py 已 WebGL）、
   Computer Use / 设计模式 / 代码库合并（理由见 §6）。

---

## 二、Orca 架构解读（README 全文取证）

### 2.1 定位

> 「面向 100x 构建者的 AI 编排器。并排运行 Codex、Claude Code、OpenCode 或 Pi —
> 每个都在自己的 worktree 中运行，并在一个地方统一跟踪。」

- **形态**：桌面应用（macOS/Windows/Linux，dmg/exe/AppImage，Homebrew cask、AUR），
  MIT 许可，配 iOS/Android 移动 companion。
- **适配哲学**：「适配**任何 CLI 智能体** — 只要能在终端里运行，就能在 Orca 里运行」，
  README 列出 30+ 已适配 CLI（Claude Code、Codex、Grok、Cursor、Copilot、opencode、Pi……）。

### 2.2 编排机制（本评估的核心对象）

| 机制 | README 原文要点 | 编排语义 |
|---|---|---|
| 并行 Worktree | 「把一个提示同时分发给五个智能体，每个都在自己隔离的 git worktree 中运行 — 比较结果，合并最佳方案」 | **同题多变体 fan-out + 隔离 + 择优合并** |
| 终端分屏 | Ghostty 级终端、WebGL 渲染、无限分屏、重启保留滚动历史 | 多 agent 会话同屏观察 |
| 标注 AI Diff | 「在任意 diff 行上添加评论并发回给智能体 — 评审、编辑、提交，全程无需离开 Orca」 | **人工评审回路**：diff 批注 → 回注 agent |
| GitHub & Linear 原生集成 | 应用内浏览 PR/issue/看板，「从任意任务打开 worktree」 | 任务源直连执行 |
| SSH Worktree | 远程机器跑 agent，自动重连与端口转发 | 执行面外延到远程 |
| Orca CLI | `orca worktree create` / `snapshot` / `click` / `fill`，「智能体也能驱动 Orca」 | **agent 可反向驱动编排器** |
| 移动 Companion | 手机监控指挥、完成通知、发后续指令 | 异步监督通道 |
| 账号切换与用量追踪 | Claude/Codex 用量与限额重置时间、免登录热切换 | 成本可视 + 凭据管理 |
| 设计模式 / Computer Use | 点 UI 元素抓 HTML/CSS/截图进 prompt；agent 操作桌面 UI | GUI 级上下文注入 |
| 快速打开 / 通知未读 / 仓库预览 | 心流不中断的检索与状态管理 | 体验层 |

### 2.3 一句话概括其编排模型

**「人坐在 GUI 前当 Leader 与验收者；Orca 负责 fan-out（同题多 agent × worktree 隔离）、
观察（终端/diff/通知）与收口（比较合并）；CLI 与移动端是同形态的延伸入口。」**
它的 Leader 是**人**，不是 LLM。

---

## 三、agenthub 编排现状（源码实读）

| 组件 | 事实 | 位置 |
|---|---|---|
| DAG 编排 | `LEADER_PROMPT` 让 LLM 把目标拆成 JSON DAG，`_validate_dag` 做环/引用/数量校验（Kahn） | `src/tasks.py:386`、`:421` |
| 节点结构 | `{"id","task","agent_id","deps"}` —— **一节点一 agent，无多变体字段** | `src/tasks.py:391` |
| 派发 | `_execute_run` 轮询 pending，依赖满足即 `asyncio.create_task`，`Semaphore(MAX_CONCURRENCY=4)` | `src/tasks.py:913`、`tasks.py:28` |
| 执行通道 | `chat_fn=_chat_dispatch` → `registry.get_adapter` → `adapter.chat`，600s 熔断 | `src/tasks.py:914`、`api_agents.py:165` |
| 验收 | `verifier.verify` 三态 pass/fail/insufficient（执行者不得自证）+ `local_judge.judge`（默认 remote=deepseek-flash）补「答非所问」 | `src/verifier.py:29`、`src/local_judge.py:91` |
| worktree 隔离 | 存在于**协议层**（`scripts/worktree-new.sh <repo> <会话短ID>`，AGENTS.md 多会话编排协议），**hub 代码未接线** | `技术文档/scripts/worktree-new.sh` |
| 技能 | 只读门面 + BM25 本地匹配（门槛 0.40）+ 免费模型重排；权威副本=磁盘 SKILL.md | `src/skill.py:1194`、`skill_store.py:251` |
| 记忆 | L1/L2/L3 + 多源 RRF 融合；权威副本 TDAI:8420 + memfed 联邦 + FTS 投影 | `src/memory.py:165` |
| 接单前置 | intake 四查（mem/kb/skill/ref）三档判定 + thin 才外扩 | `src/intake.py:994`、`:378` |
| 定时/聚合 | cronjobs（croniter 20s tick，shell/agent_prompt/http 三类）；mcpgw 聚合本机 MCP | `src/cronjobs.py`、`src/mcpgw.py` |
| 终端 | pty + WebSocket，xterm 6.0.0 WebGL，空闲 TTL 2700s，逐帧录制 | `src/term.py:44` |
| 移动侧 | WorkBuddy 小程序桥（小程序→腾讯云中继→桌面 Electron→本地 CLI） | AGENTS.md §5 资产指针 |
| 安全闸 | writeauth/audit/boundary/approvals/admission/outbound 六道 | `src/` 各模块 |

**agenthub 的编排模型一句话**：「**LLM 当 Leader 拆 DAG，hub 当调度器并发派发，
verifier+judge 当验收者；人是授权与运维者，不盯屏。**」与 Orca 恰好互补：
Orca 强在「人的观察与收口回路」，agenthub 强在「LLM 拆解 + 自动验收 + 服务化常驻」。

---

## 四、逐维对比

| 维度 | Orca | agenthub | 差距判定 |
|---|---|---|---|
| 编排 Leader | 人（GUI 前） | LLM（LEADER_PROMPT 拆 DAG） | 设计哲学不同，**无差距** |
| 同题多变体 | ✅ 核心特性（1 提示×5 agent） | ❌ 节点单 agent | **agenthub 缺**（§5-A） |
| 执行隔离 | 每 agent 独立 git worktree | 协议层有 worktree-new.sh，hub 未接线 | **半缺**（§5-A 附带） |
| 并发控制 | 桌面本地，无限分屏 | `Semaphore(4)` + 600s 熔断 | agenthub 有显式上限，**更严谨** |
| 验收闭环 | 人看 diff 手动比较合并 | verifier 三态 + judge 自动判 | 哲学不同；**人工 diff 回路 agenthub 缺**（§5-B） |
| 评审交互 | diff 行批注回注 agent | 无（api_agents 只有对话直连） | **agenthub 缺**（§5-B） |
| 任务源集成 | GitHub/Linear 看板→开 worktree | localprojects/githubprojects 仅登记 | agenthub 弱，价值中低（§6 列为不借） |
| 移动监督 | companion App（通知+指挥） | WorkBuddy 小程序桥（已成体系） | **对等，不重复建设** |
| 终端 | 无限分屏+WebGL+历史保留 | pty+WebGL+逐帧录制+TTL | **对等或更优** |
| 远程执行 | SSH worktree 自动重连 | profiles 三路检测但不代理执行 | agenthub 缺，价值中（§5 列为 P2 观察） |
| 脚本化 | CLI（agent 可驱动 Orca） | agent-hubctl.sh + Hub MCP server | 部分对等；worktree 子命令缺（§5-E） |
| 成本可视 | 用量+限额追踪+账号热切换 | modelcfg 管理配置；usage 已采集未聚合看板 | **agenthub 缺看板**（§5-D） |
| 完成通知 | 通知+未读状态 | vitals 心跳 + cronjobs；无任务完成推送 | **agenthub 缺推送**（§5-C） |
| 部署形态 | 桌面 App（重 GUI） | 无头服务（systemd 常驻） | 定位差异，**不合并**（§6） |
| 安全模型 | 未在 README 展开（有遥测隐私文档） | 六道安全闸 + 单写者协议 + 备份铁律 | agenthub **显著更强** |
| 技术栈 | Electron/TS | Python 3.11/FastAPI，零新增第三方依赖 | **禁合并**（§6） |

---

## 五、融合候选清单（按优先级）

### §5-A【P0·核心】DAG 同题多变体 A/B 执行

- **借什么**：Orca「一个提示分发给多个 agent、各自隔离、比较合并最佳」。
- **agenthub 现状差距**：`tasks.py:391` 节点只有单 `agent_id`；leader 选「最合适」的一个。
  同题对比只能人工开多次任务，无法机械化 A/B。
- **融合方案（设计层，未实施）**：
  1. DAG 节点增补可选字段 `variants: [agent_id,...]`（缺失时维持单体语义，**向后兼容**）；
  2. `_execute_run` 对含 variants 的节点 fan-out：每变体一次 `_chat_dispatch`，
     共享 `Semaphore(4)` 预算（**防并发击穿**）；
  3. 变体隔离走 `worktree-new.sh` 同款语义（每变体独立 worktree，禁 hub 内裸 mkdir——
     这是多会话单写者协议的红线）；
  4. 收口复用既有三件套：`verifier.verify` 逐变体验收 → `local_judge.judge` 对比选优 →
     优选结果作为该节点产物回流 DAG 下游。
- **依赖与前置**：Grill 协议（改共用配置结构/任务 DAG 语义先 `/grill-me`）；
  worktree 隔离接线涉及 AGENTS.md 多会话协议，须报请；**计费明示**：一变体一跑 =
  token 消耗 ×N，无法确定扣费的一律按扣费处理（§1 铁律）。
- **验收判据**：L0 hermetic 用例只增不减（2026-10-24 实测基线 `ran=2934 skipped=0 failures=0`，
  收集器对账 unittest=pytest=3009）；
  一条「同题 3 变体」e2e 探针：fan-out 数==3、Semaphore 未击穿、judge 输出唯一优选；
  向后兼容用例：无 variants 字段的旧 DAG 行为逐位不变。
- **工作量判断**：中（改 tasks.py 派发段 + verifier/judge 收口 + 测试），非 trivial。
- **收益实测（2026-10-24，见 §10）**：无本地基线支撑，建议降级为「DAG 有真实使用后再上」。

### §5-B【P1】diff 行批注回注执行 agent

- **借什么**：Orca「任意 diff 行添加评论并发回给智能体」。
- **方案**：前端评审视图读任务产物 diff（localprojects/githubprojects 已有 git 通道），
  行级批注聚合为一条后续 prompt，经 `api_agents._chat_dispatch`（`api_agents.py:165`）
  发回原执行 agent；复用 `approvals` 闸做写动作审批。
- **价值**：补上 agenthub「自动验收之后、人想插手时无入口」的断点；与 §5-A 是天然搭档
  （A/B 择优后人改批注）。
- **验收判据**：批注→回注→agent 响应闭环 e2e 一例；批注不含凭据（writeauth 覆盖新入口）。

### §5-C【P1】任务完成通知推小程序桥

- **借什么**：Orca「智能体完成时收到通知」。
- **方案**：tasks/cronjobs 完成事件 → 既有 WorkBuddy 小程序桥（不新建移动端）。
- **验收判据**：一次真实任务完成 → 小程序收到（三判据见 AGENTS.md §5 健康口径）。

### §5-D【P2】用量与限额看板

- **借什么**：Orca「Claude/Codex 用量与限额重置、账号热切换」。
- **方案**：adapter 回包 usage 已在 `tasks.py:101`（P0-a 实发模型/usage 采集）落库，
  做聚合看板即可，**零新增采集**。账号热切换**不借**——涉及 CCR 共享生命线，
  按 §1 军规须用户指定唯一执行者，本次只登记不动作。
- **验收判据**：看板数值与 sqlite 逐行对账一致。

### §5-E【P2】hub CLI 补 worktree 子命令

- **借什么**：Orca CLI「`orca worktree create` 等把工作流脚本化」。
- **方案**：`agent-hubctl.sh` 已存在，补 `worktree create/list` 薄封装
  （转调 `scripts/worktree-new.sh`，不重写隔离逻辑）。
- **验收判据**：CLI 建一个 worktree 且 orchestration-check.sh C10 仍 PASS。

---

## 六、明确不融合项与理由

| 不借项 | 理由（引用） |
|---|---|
| 代码库合并（clone/并入 agenthub） | 《开源项目融合评估框架》：「合并代码库导致维护多技术栈（TS+Python+Dart），维护负担爆炸」；「App 层和社区层不可强行合并」。**只借设计，零并代码。** |
| 移动 companion 自建 | WorkBuddy 小程序桥已成体系（AGENTS.md §5），重复建设违反「避免过度工程」。 |
| 终端分屏/WebGL | term.py 已有 xterm 6.0.0 WebGL + 逐帧录制，**对等或更优**。 |
| Computer Use | 与 Orca 桌面形态强绑定；agenthub 无头服务无桌面可操作；有 pty 但不等于 GUI 控制。价值/成本比差。 |
| 设计模式（UI 元素抓取） | 面向「GUI 里装修 UI」场景，与 agenthub「管理平面」定位弱相关。 |
| GitHub/Linear 看板直开 worktree | agenthub 已有 localprojects/githubprojects 登记 + intake 前置；
  看板视图是 UI 增强，排在所有 P1 之后。 |
| SSH worktree 远程执行 | 概念诱人但触及「hub 代理远程执行」这一新攻击面（outbound/admission 闸须重设计），
  列为**长期观察项**，不进本期。 |
| 账号热切换 | CCR 是全部 AI 会话共享生命线（§1 军规三方互斥），**禁止本次触碰**。 |
| 照搬遥测体系 | agenthub 凭据不落配置、零新增凭据优先；遥测采集面与本地铁律冲突，不引入。 |

---

## 七、分期与报请路径

| 期 | 项 | 前置动作 |
|---|---|---|
| P0 | §5-A 同题多变体 A/B | ① `/grill-me` 过 DAG 语义变更；② PT 台账立项（§8）；
| | | ③ worktree 施工会话；④ 计费方案随方案一并报请 |
| P1 | §5-B diff 批注回注 | Grill（新入口=新攻击面，writeauth/approvals 覆盖） |
| P1 | §5-C 完成通知推送 | 轻量，可随 P0 收尾 |
| P2 | §5-D 用量看板 / §5-E CLI 子命令 | 只读聚合/薄封装，随版本迭代 |
| 不做 | §6 全部 | 已定判，留档防重复提案 |

**任何一项开工前还须**：`bash scripts/orchestration-check.sh` 单写者检查；
受保护面变更走 §1 五条（读现值→时间戳备份→写→端到端验证含一次真工具调用）。

---

## 八、风险与红线

1. **受保护面**：`agenthub/**` 在 AGENTS.md §1 受保护面清单内 —— 本评估只读未写；
   后续实施按共享配置变更军规执行。
2. **计费**：§5-A 多变体并行 = 执行次数 ×N；`openrouter/auto` 系本机唯一扣费模型 ID 写法，
   任何场景禁出现；方案须显式写明每条调用点是否扣费。
3. **并发击穿**：fan-out 必须共享既有 `Semaphore(4)`，禁另起并发通道。
4. **单写者/worktree**：变体隔离若接线 hub，必须复用 `worktree-new.sh` 协议语义，
   禁 hub 内裸建 worktree（多会话编排协议判例：17 文件被双写、`.git` 被换）。
5. **文档滞后提示**：`agenthub/README.md:111`「二期候选」称 DAG/MCP 未移植，与
   `tasks.py`/`mcpgw.py` 现状不符——实施前以代码为准，别被旧文档带偏。

---

## 十、收益账（2026-10-24 实测取证）

> 回答「收益多少」。方法：全部取自本机生产库 `data/agents.db` 实查，不用估计值。

### 10.1 生产负载画像（profile_events 56,366 条，2026-09-06 → 2026-10-24）

| 来源 | 次数 | 占比 | 含义 |
|---|---:|---:|---|
| rest | 47,913 | 85.0% | 其中：**巡检/探活 28,283**（routes_ok 377 条路由）、**记忆检索 ≈11,730**、**kb/XERJ 检索 ≈6,357** |
| ctx_manifest | 7,757 | 13.8% | 上下文清单投影 |
| hubself_tool | 308 | 0.5% | hub 自检工具 |
| hub_chat | 101 | 0.2% | 人工直连 agent（76 成功 / 25 失败） |
| cron_run | 28 | 0.05% | 定时任务 |
| **task_exec** | **3** | **0.005%** | **DAG 编排执行（全 success、verdict/attempts/judge 全空）** |

**读数一：agenthub 的生产价值 98%+ 在「管理平面」（发现/巡检/检索/投影），DAG 编排引擎
48 天内仅 3 次执行（2026-09-06 测试残留）。P0 是给一个几乎零使用的引擎加并行 A/B——
在没有「DAG 被真实使用」的前提前，其边际收益不可从本地数据估计。**

### 10.2 §5-A 收益上限测算：本机数据不支持

拟议收益是「多变体至少一个成功 → 对抗单跑失败」。实测人工直连失败率 25/101=24.8%
（剔除 5 次「pi 需 Web UI」设计约束后 20/96=20.8%），但**失败原因分布**否决了冗余论点：

| 失败类型 | 次数 | 时间分布 | A/B 能否对冲 |
|---|---:|---|---|
| HTTP 400「All target providers failed」 | 18 | **挤在 4 个同时段窗口**（09-06 10:0x 三次、16:0x 三次、23:36–23:38 **十二次**、09-07 11:07–11:09 五次） | **否**——网关级相关故障，同刻多变体一起死 |
| HTTP 429 / TimeoutError | 2 | 零星单发 | 可能（且需变体走不同渠道） |
| pi 需 Web UI | 5 | 单发 | 否（设计约束） |

**读数二：主导失效模式（18/20）是同时段相关的网关故障**——按 `agent-knowledge/106`
「先量探针方差/相关性再讲差异」的纪律，同刻 fan-out 的冗余对相关失败**数学上无效**；
可被冗余救回的只有零星非相关失败约 2~4/101（≈2~4%），且要求变体走不同网关渠道
（本机 adapter 多数同走 CCR，实际更低）。

**结论：P0「同题多变体 A/B」在本机的收益上限 ≈ 2~4% 调用成功率恢复，
成本是 token×N + 并发挤占 Semaphore(4) + 中等工程量。收益/成本比不成立，建议：
① 降级为「DAG 有真实使用基线后再上」；② 先补 DAG 使用度量（为何 48 天仅 3 次——
入口/触发问题才是真瓶颈）；③ 若要对抗那 18 次网关故障，正确解法是重试/退避/多渠道
failover（运维项，非 Orca 融合项）。**

### 10.3 其余各项收益排序（按实测负载）

| 项 | 可量化收益 | 判定 |
|---|---|---|
| §5-B diff 批注回注 | 分母未测：hub_chat 76 次成功人工介入中「看 diff 后回注」形态占比未知 | **先埋点度量再决定**；人工介入真实存在（101 次）是唯一有分母支撑的候选 |
| §5-C 完成通知 | 可通知事件 = cron 28 + task 3 ≈ 0.65 次/天 | 收益极小、成本极小，随缘做 |
| §5-D 用量看板 | 省人工登录网关查账频次未知 | 便利性 |
| §5-E CLI worktree 子命令 | 零（worktree-new.sh 已在协议层存在） | 纯便利 |

**总判定：本次评估的五个融合候选，没有一个有「高收益」的本地证据；
按数据说话，当前更高收益的方向是度量 DAG 使用瓶颈与网关失败重试，均在 Orca 融合范围之外。**

---

## 九、取证与局限

- Orca 侧仅 README（功能宣传口径），**未读其源码/架构文档**——「并行 worktree 比较合并」
  的具体语义（合并是自动还是人工、比较维度）以 onorca.dev/docs 为准，本评估按 README 字面解读。
- 参考库卡 `480a41e0c535`（lexical_fallback，语义重排因免费模型超时未完成）另发现同类
  `slopus/happy`（「Orchestrate Claude, Codex and Grok in one app」，MIT）——同赛道第二个样本，
  若后续要深评可作对照。
- agenthub 侧：`main.py` 142KB 未逐行（只读入口/挂载/启动段），profiles 22 画像未逐条核。
- 本文件未做任何 git 提交；是否提交/推送由用户裁定。
