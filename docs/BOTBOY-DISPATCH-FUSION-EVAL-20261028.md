# Botboy × 多 Agent 派发：融合评估

> 2026-10-28 ｜ 对象：`Botboy`（DS Bot 团队只读投影，v0.13.170）与
> `PT-20261027-04` 全链路派发（拆解→选型→派发→汇总→最多 2 轮）
> 性质：**评估，不含施工**。结论可执行，但 P2 未开工。

## 0. 一句话结论

**能融合，但只能上下分层，不能合并成一个调度器。**
hub 出「计划 + 验收 + 账本」，DS Bot 出「带工具的执行会话 + 记忆」；
接口只做「子任务 ↔ 单个 Bot」一条，**不把群聊当执行器**。

理由：两侧的能力缺口正好互补，重叠部分（路由/调度）一旦都开就是双主脑。

| | hub 派发 | DS Bot |
|---|---|---|
| 计划 | DAG 拆解、依赖、难度、模型档 | 无 DAG，管理员 Bot 用自然语言决定谁上 |
| 执行 | `chat()` = **纯文本 LLM，无工具** | 每个 Bot 是独立 DSH 会话，可读写文件/跑命令 |
| 验收 | verifier + `refusal`/`not_a_product` 判负 | 无 verdict，只有「回没回」 |
| 预算 | 估算字段 `budget_ms` / `budget_tokens` | `usage` 按 Bot/模型出**真值** |
| 收敛 | 最多 2 轮重拆，有闸门 | 群聊接力上限 `3 × 成员数` 后暂停等用户 |
| 记录 | SQLite run/tasks，可 pause/resume/cancel | `state.json` 的 `works` / `exchanges` |

## 1. 证据（可回原文核）

**hub 侧**

- `src/tasks.py:675` `/api/tasks/decompose`；`:497` `_validate_dag`；`:31` `MAX_TASKS=12`；`:79` `MAX_ROUNDS=2`
- `src/registry.py:40` `dispatchable_ids()` —— 能接 `chat()` 的只有 claude / jcode（pi、tdai 是恒失败桩）
- 判例（记忆）：派发走 chat = 无工具；实测文件类任务回 `count=27`、真值 97 ⇒ **编造内容 `nonempty` 判不出**
- `src/botboy.py:120` `overview()` 只读投影（已含 `works`）

**DS Bot 侧**（`work/botboy-probe/dsbot`，npm `ds-bot@0.2.2`，仓库 `Fei2411/DS-Bot`）

- `src/host/store.js:45` `state.lock` 单写者；hub 直写 = 伪造锁冲突 ⇒ 只读是硬边界
- `src/host/handoffs.js:99` `openWork` / `:108` `closeWork` —— 委派生命周期；结果**超过 2000 字落文件**
  （`constants.js:27 TEXT_KEPT=2000`），state 里只剩头部 + `file` 指针
- `src/host/groups.js:39` 接力上限 = `3 × 成员数`；`:468` 超限 `paused`，要 `room-continue` 才继续
- `src/host/channel.js:404` `send` = `queue` 后**立即返回 `ok(true)`**（异步，不是 RPC 结果）
- `src/host/channel.js:225` `room-progress` 才是进度；`protocol.md §16.4` 白名单含 `state / send /
  room-progress / usage / schedules / room-continue / stop-member-work`

**网络侧（2026-10-28 实查）**

- `Fei2411/DS-Bot`：4,273 stars，Apache-2.0，最后推送 10-27（仓库已从 `DeepSeek-Bot` 改名，旧链接 301）
- `liguobao/ds-harness-remote`：898 stars，**未声明 license**，最后推送 10-27；npm 最新 `0.5.22`
  （用户贴的 `0.5.18` 已过期）
- 第三方评测一致口径：多 Bot 协作 **token 成本显著高于单 Agent**；远控插件是「手机当控制入口」，
  不改变 Bot 的编排方式

> 未声明 license ⇒ 只准通过插件接口调用，**不复制其代码**。

## 2. 四个融合方案（按成本排序）

| # | 方案 | 改动 | 依赖 | 风险 | 收益 |
|---|---|---|---|---|---|
| A | **只读看板**（现状） | 已完成 | 无 | 0 | 看得见团队/委派，不能派活 |
| B | **口令桥**：把 hub run 的子任务生成「@某 Bot」文本 + 复制 | 前端 ~60 行 | 无 | 极低 | 人工搬运，今天可用 |
| C | **执行适配器**：hub 调 `POST api/bot` 的 `send`，轮询 `exchange`/`works` 收结果 | 新增 adapter + 调度分支 | dsh 必须运行；token 换 cookie | 中高 | 子任务获得**工具能力**，补上 hub 最大短板 |
| D | **反向挂载**：把 hub MCP 挂进 Bot 能力库，Bot 调 `hub_intake`/`hub_kb_search` | 配置 | DS Bot 能力库支持 HTTP MCP（未验证） | 中 | Bot 能用本机记忆/知识库 |

**推荐：B 立刻做，C 先做 spike，D 只开只读工具。**
D 若把 `hub_dispatch` 也开出去，Bot 就会再派一层活 ⇒ 递归烧 token，必须白名单排除。

## 3. 六个硬冲突（不裁就别接 C）

1. **双主脑**：Chief 会自己判断「该找谁」。hub 派活必须直接 `send` 给**指定 Bot 的私有会话**，
   不进群聊 —— 进群聊就是两个调度器抢同一件事。
2. **幂等**：`send` 是 mutation，`protocol.md` 明写**结果未知不得重发**。hub 的 `MAX_AUTO_RETRY`
   直接套过来 = 重复派活。需要业务幂等键（文本前缀带 `run_id/task_id`，回读时按前缀去重）。
3. **结果截断**：`works` 结果 >2000 字只剩头部 + `file`。验收若只读 `state.json` 会**拿半截判 pass**，
   必须取 `file` 全量（对应 hub 现有「`nonempty` 判不出编造」的旧账）。
4. **预算口径**：hub 的 `budget_tokens` 是拆解时估的，DS Bot `usage` 是实发真值。
   **两个数不能并表报**（同 memory 三口径教训）。要么只报 `usage`，要么分别标口径。
5. **dsh 生命周期**：按 10-24 裁定 dsh 常态 inactive、按需拉起；web token 是**进程级随机**，
   只能从 journal 取（`src/profiles.py:177`）。派活 = 拉起 → 等 ready → 取 token → send，
   且**不能杀有活会话的 dsh**（同 hub 重启军规）。
6. **单写者**：hub 永远不写 `state.json`；写只走 `api/bot`。读可以随时读。

## 4. 推荐路线

### P0（**已实现 v0.13.171**，零风险）

落点从 Botboy 页改到**任务页**：hub 的子任务在那边，Botboy 页只有 DS Bot 侧的委派，
方向反了。每行一个「复制派活」+ run 栏「复制全部派活文本」，生成：

```
[agenthub → DS Bot] run <run_id> / <task_id>
总目标：…
子任务：…（取 /api/tasks/{run} 的 prompt）
验收：产出必须可核（路径 / 命令输出 / 引用）；只回「收到」不算完成。
（在 DS Bot 里 @ 一个 Bot 或进群聊粘贴；hub 不会代发。）
```

**不带 bot 名的原因**：本机没装 ds-bot，团队不存在，写死名字就是假信息。

**验收（已过）**：影子 3196 + headless 真点三次按钮 ⇒ 3 个按钮、文本字段齐全、
空 prompt 显示「没留存 prompt」不出现 `undefined`、0 个 JS 异常；
单测 `tests/test_dispatch_handoff.py` 8 例（L0 5 + L1 node 3）。

### P1（半天，只读）

对账视图：本地存 `run_id ↔ roomId/botId` 映射（prefs 或新表），Botboy 页显示
「这个 run 的子任务在 DS Bot 里的进度」。数据仍只读 `state.json`。
**验收**：一个手工映射的 run 能显示某 Bot 的 `works` 条目与 `usage` 数；映射缺失时显示「未关联」而非 0。

### P1.5 反向挂 MCP（**已实现 v0.13.172，待重启上生产**）

D 方案先落地的是「出口面收窄 + 挂载验证」两件：

1. `HUB_MCP_ALLOW_TOOLS`（白名单，设了就只留列出的）+
   `HUB_MCP_ALLOW_DISPATCH`（派发闸门，**默认开**，`=0` 硬关）。
   顺序：先扣派发、再套白名单；冲突记 `blocked`、拼错记 `unknown`，都进启动日志。
2. dsh 侧用自带 `dsh-mcp-client`（`transport: streamable-http`）挂上去。

**生产重启后把这段追加到目标 profile 的 `cordis.patch.yml`**（`insert:` 是唯一能新增条目的写法，
直接写 `- id: mcp-hub` 会报 `patch: entry "mcp-hub" not found`）：

```yaml
- insert:
    - id: mcp-hub
      name: '@deepseek-ai/dsh-mcp-client'
      config:
        serverName: hub
        transport: streamable-http
        url: http://127.0.0.1:3102/hub-mcp/mcp
        headers:
          x-hub-token: !!js process.env.HUB_MCP_TOKEN
```

**已验（2026-10-28，全部真跑）**：

- 影子 `:3195` 带 7 项白名单：MCP 真客户端 `list_tools` 得 7 个工具、`hub_kb_search` 返真实结果。
- `dsh headless` 会话工具清单恰为
  `mcp__hub__hub_intake / kb_search / kb_status / memory_context / memory_search / skill_list / skill_read`；
  让它调 `mcp__hub__hub_kb_search(q=限流,k=1)` 回 `count=1`，与直连客户端一致。
- 验证用的 patch 条目**已还原**（当时指向影子，影子停掉即死指针）；上生产按上面片段改回 `:3102`。

**两个坑（实测）**：

- token 走 `!!js process.env.HUB_MCP_TOKEN`，**不写进 yaml**（凭据不落配置）；
  但 `dsh headless` 手动跑时不读 `dsh-src/.env`，要 `set -a; . .env; set +a` 先导出。
- hub 的 `TERM_TOKEN` 是 `.env` 里的值（隔离变量不覆盖它）；影子实例用自定义 token 会 401，
  别把「401 凭据不匹配」当成鉴权坏了。


### P2（**spike 已跑通，2026-10-29**；接入待裁，≥2 天）

30 行脚本跑通一条闭环：起 dsh → 从 journal 取 token 换 cookie → `send` 给单 Bot →
轮询 → 拿到全文。**这条不通，C 就不成立**，不要先改 `tasks.py`。
**验收（spike）**：一条真实任务从 send 到取回结果 ≤ 10 分钟、文本完整、无重复派发；
失败时能区分「Bot 拒答」「超时」「dsh 没起来」三种，不许都报 failed。

**实测（2026-10-29，`work/botboy-probe/spike_send.py`，dsh 0.2.2 / agnes-2.5-flash）**

| 项 | 实测值 |
|---|---|
| 鉴权 | `GET /?token=<journal 里那个>` → 303 + `dsh-auth-*` cookie；之后 `POST /api/bot` |
| 发送 | `send {sessionId, text}` 立即回 `ok(true)`（queue），**不是结果** |
| **结果面** | **`chat {botId}`**（分页 items，含 `turn/seq/kind/text`）。**`state.exchanges` 恒为 0** —— 那张表只记 bot↔bot，私聊不进 ⇒ 评估文档原句「轮询 exchange 回收结果」**已证伪** |
| 往返 | 纯对话 ~13s；带工具（pwd）13.4s；长输出 24.9s（均含鉴权+轮询粒度 2s） |
| 全文 | 1093 字 / 300 行**逐行完整**，未见 2000 字截断（截断只在 `state.works` 投影里） |
| 幂等 | 同 `[hub:run/task]` 复跑 21ms 返回 `idempotent-hit`，**未重发** |
| 用量 | `usage` 端点给 per-owner / per-model 真值（含 cacheRead），与 hub 估算列**天然两口径** |

**工具能力（实测，不是宣称）**：Chief 自报并实调 `bash / 文件读写 / glob / grep / web_search /
subagent / workflow / create_bot / schedule / remember / skill`。⇒ 「子任务拿到工具」这个前提成立。

**新发现的硬约束（接 C 前必须处理）**：
1. dsh web 只绑一张网卡（`192.168.5.102:3080`），**127.0.0.1 没有监听**；探活必须先探端口再定 base。
2. token 是进程级随机，换 cookie 只有 `GET /?token=` 一条路（urllib 会自动跟 303）。
3. `chat` 页是**分页**的：长会话要跟 `cursor`，否则只看到最后一屏。


### P2.1 影子实弹 5 条（2026-10-29，影子 `:3198`，非生产）

| # | 目标 | 子任务 | 结果 | 判词 |
|---|---|---|---|---|
| 1 | RAID5 vs RAID10 三点取舍 | 3 并行 + **代码补的 `t_sum`** | 4/4 pass | 汇总节点真跑（t_sum 5272 tok） |
| 2 | 1 到 100 求和 | 1 | pass，答案正确 | — |
| 3 | 读 `/etc/hostname` | 1（claude） | **pass** | **编造**：真值 `zzst`，回 `AGNES-2.5-FLASH` |
| 3b | 同上（修拒答正则后复跑） | 1 | 仍 pass | 见下 |
| 4 | Python 回文函数 | 2 | pass | — |
| 5 | TCP/UDP 各一句 + 合并 | 3（含自建汇总） | 3/3 pass | — |

**两条必须写进结论的发现**

1. **「假装干了」判不出来**。同一条文件任务，jcode 走「我无法直接读取…没有可用的文件系统访问工具」
   （自述没干，新正则已判 `refusal`），claude 直接**编了一个值**（`AGNES-2.5-FLASH`）—— 后者与
   真实产物在文本上不可区分，`nonempty` 与任何正则都判 pass。**这不是验收器的洞，是"没有工具"的必然结果**：
   只有让子任务真的跑在带工具的执行面（C）上，`/etc/hostname` 才会回 `zzst`。
2. **汇总节点按设计生效，但换了 agent**：Leader 出的 3 个并行节点是 `claude`，代码追加的 `t_sum`
   用 `default_agent`（`jcode`）。**同一条 run 内 agent 会不同**，不是「所有子任务同一个 agent」。

### P2.2 子任务是不是同一个 agent？能不能按难易度智能调度？

**代码事实（可回原文核）**

| 问题 | 事实 | 出处 |
|---|---|---|
| agent 谁定 | Leader 逐项填 `agent_id`；超池/未填回落 `default`（jcode） | `src/tasks.py:516-518` |
| 可选池多大 | **2 家**（claude / jcode）+ 自定义端口 | `src/registry.py:40` |
| 汇总节点用谁 | `default_agent`（不跟随上游） | `ensure_synthesis()` |
| `difficulty` 管什么 | **只管复盘**，不参与预算、不参与选型、不参与判活 | `src/tasks.py:143-145` |
| `model` 谁定 | Leader 可填，按 CCR 清单校验；非法降为 NULL | `src/tasks.py:519-526` |
| 实测填没填 | 5 条 run、11 个子任务，`model` **全 NULL**，实发全部 `agnes-2.5-flash` | 影子库 `tasks` 表 |

**判词**：**「智能调度」目前是名义上的，不是机制上的。**
选择信号只有 LLM 自评（`difficulty` 由 Leader 拍），没有校准、没有反馈闭环、没有按 agent 能力画像；
池子只有 2 家，实际结果就是「Leader 顺手选谁就是谁」，实测里 8/11 是 claude、3/11 是 jcode。
这对应 RouteLLM / RouterArena 那条结论的最低档：**未校准的路由器在均值上看不出来，错都错在少数难样本上**。

**若要真做，三条按性价比排序**

1. **把 retry 改成 cascade**：现在验证失败是**同一模型**重跑（`MAX_AUTO_RETRY=1`），等于原样再赌一次；
   改成「失败升一档模型再跑」才有多模型路由的意义（FrugalGPT 路线，代价是难任务双付）。
2. **difficulty → 模型档位写进代码表**，不留在提示词里；前提是先用 20~30 条真实任务标定
   「difficulty 与实发模型质量」的关系，否则只是把拍脑袋从模型换成硬编码。
3. **扩池**：给 dsh / cloudcli 造 chat 适配器，或直接接 C 的 bot 通道 —— 池子 2 家时，
   再怎么调度的上限也就是「二选一」。

### 明确不做

- 群聊当执行器：非确定性 + 接力上限 + token 成本，且结果要人工汇总。
- 双向全量同步：两个 `state` 互相写，必然出现双写者。
- 把 `hub_dispatch` 挂给 Bot：双层调度，预算失控。

## 5. 未验证项（不要当结论用）

> 2026-10-29 复核：原 5 条里 4 条已实测（见 P2 表），**下面只留仍然没验的**。

- `works` 的结果文件读取路径（`resultPath`）：本轮所有结果都从 `chat` 页取，没走这条。
- DS Bot 能力库是否支持 **HTTP 型** MCP 服务（D 方案前提）：仍未查证。
- `chat` 页分页上限与 `cursor` 翻页：只验了单屏（≤10 条），长会话未跑。
- 生产（`:3102`）未接 C：本轮全部在影子 `:3198` 与 `work/` 脚本上跑。

## 6. 参考

- 本地：`work/botboy-probe/protocol.md` §16.4；`dsbot/src/host/{store,handoffs,groups,channel}.js`
- 仓库：<https://github.com/Fei2411/DS-Bot>（Apache-2.0）、<https://github.com/liguobao/ds-harness-remote>（无 license）
- 包：`ds-bot@0.2.2`（npm，无依赖）、`ds-harness-remote@0.5.22`
- 评测（第三方）：Pandaily / AI-TLDR / 微信公众号评测，口径一致：多 Bot 协作 token 成本更高、预览版
