# P3 · Provider 事件契约设计（借鉴 t3code 工程思维）— 供裁决，非实现

- 日期：2026-11-09
- 批次：`PT-20261109-08` 借鉴分支 ⑤ P3（批次计划见 `PENDING-TASKS.md:14413`，原文「P3（最重，3–5 人天，排最后）」）
- 对标物：t3code `packages/provider-core/src/server/ProviderAdapter.ts:48-715` 的 `ProviderAdapterV2Event`（14 类事件）+ runtime 方法 `startTurn/steer/interrupt/rollback/fork`（引自 `agenthub/docs/T3CODE-FUSION-EVAL-20261109.md:57` 与 `:56-57` 架构第 4 条）
- 本文性质：**调研 + 设计**，不实现。本会话写盘**仅此一份文档**；不改 `src/*`、`static/*`、其它 docs、任何 tests；不 git add/commit、不重启服务。
- 取证方式：只读源码（Read / Grep），行号按本会话读取时的磁盘内容。并行会话正在改 `static/hub/03-agents-cards.js`、`02-nav-and-poll.js`、`src/tasks_runner.py` 等（发现 `03-agents-cards.js.bak-20261109_102119-p2conn`），**本文行号合入后可能偏移，不依赖行号做硬编码引用**。
- 事实与推断分离：§1 只陈述、§2 起才评价；凡未实测/未读到的一律写「未确认」。

---

## §1 现状事实（只陈述，不评价）

### 1.1 Agent 注册 / 启动模型（`src/profiles.py`、`src/agent_signatures.json`）

- 画像单一真相源是静态表 `PROFILES: List[dict]`（`profiles.py:414-673`），`all_profiles()` = 静态卡 + 动态 CLI 卡 + 注册表卡 + SHELL 卡（`profiles.py:864-871`）。
- 每张 agent 卡可带三类入口，彼此独立、可并存：
  - `terminal`（pty/TUI）：`{"cmd": ..., "cwd": ...}`；
  - `ui`（原生 Web 界面，按需拉起）：`profiles.UI_START` / `ui_start_for()`（`profiles.py:404-411`，配方收敛在 `capabilities.UI_RECIPES`）；
  - `chat`（直连适配器）：`{"adapter": ...}`，**全表只有 claude 与 jcode 两家带 `chat`**（`profiles.py:421` claude、`profiles.py:511` jcode）。
- 三家对照（任务点名）：

| agent | terminal.cmd | chat | ui | file:line |
|---|---|---|---|---|
| claude | `claude`（cwd `/fs/1000/ftp/技术文档`） | `{"adapter":"claude"}` | `None` | `profiles.py:416-424`（cmd `:420`、chat `:421`） |
| codex | `codex --no-alt-screen` | `None` | `None` | `profiles.py:513-530`（cmd `:528`） |
| pi | `pi` | `None` | `http://127.0.0.1:30141` | `profiles.py:438-448`（cmd `:441`、ui `:440`） |

- 卡片按钮由 `entries_for(p, status)` 按画像派生，类型只有 `embed/open/term/chat/detail` 五种（`profiles.py:1017-1082`；`term` 加于 `:1064-1065`、`chat` 加于 `:1066-1067`）。
- 动态发现：`CLI_ALIASES`（`profiles.py:697-716`）、`AGENT_PROBE`（`profiles.py:734-758`）、`agent_signatures.json`。`agent_signatures.json:1-37` 是 6 条 CLI **签名**（`id/display/names/verify_re/desc`），用于自动识别「装没装这个 CLI」，**不涉及启动命令、会话契约、事件**。
- 起子进程的 PATH 唯一口径 `cli_env()`（`profiles.py:923-938`），终端 / 原生界面 / 派发 CLI 适配器共用。
- 适配器注册表 `registry.py:18-37`：`claude`/`jcode` = `OpenAICompatAdapter`（CCR/jcode 的 OpenAI 兼容端点）；9 个 `claude-code/codex/dsh/pi/codebuddy/grok/hermes/omp/cursor` = `CliAgentAdapter`（无头 CLI）；`tdai` 为桩。`dispatchable_ids()` 是派发池唯一口径（`registry.py:64-102`）。

### 1.2 会话创建 / 发消息 / steer / interrupt / 终止（`src/api_agents.py`、`src/term.py`）

**A. 直连对话（chat）路径**

- `POST /api/agents/{agent_id}/chat`：单次请求-响应（`api_agents.py:470-482`），请求体字段 `agent_id/message/session_id/model/cwd`（`api_agents.py:42-48`）。
- `POST /api/agents/{agent_id}/chat/stream`：**全仓唯一 `StreamingResponse`（SSE）**（`api_agents.py:485-498`）；`OpenAICompatAdapter.chat_stream()` 产出 `data: {"delta": ...}` / `{"error":...}` / `[DONE]`（`adapters/openai_compat.py:164-196`）；`CliAgentAdapter.chat_stream()` 只是把批式 `chat()` 的最终文本整体 yield 一次（`adapters/cli_agent.py:492-500`）。
- `_chat_dispatch()`（`api_agents.py:413-467`）：取最近 20 轮历史 → `adapter.chat()`（`:444`）→ `db.log_profile_event` → 落 `chat_sessions`/`chat_messages`（`:455-466`）。
- 适配器抽象基类 `BaseAdapter`（`adapters/base.py:13-47`）：抽象方法只有 `chat / chat_stream / get_sessions / get_models`，加 `health_check / build_headers`；**没有 startTurn / steer / interrupt / fork / rollback 或任何「轮次」方法**。

**B. 终端（pty/TUI）路径**

- `POST /api/term/sessions` 建会话（`term.py:890-956`）：命令只取自画像白名单 `terminal.cmd`，`shlex.split` → `profiles.which` → `pty.fork()` + `execvpe`（`term.py:504-518`）；`session_id` 只用于 `sessions_store.resume_argv()` 从磁盘历史续聊（`term.py:906-913`）。
- `Session`（`term.py:504-723`）维护 `ring`（64KB 回放）、`viewers`（每观看者独立队列）、`coalescers`（5ms 输出合并）、`recorder`（录制）、`last_io`/`last_activity` 双时钟。
- `GET /ws/term/{sid}` 是**全仓唯一 WebSocket 端点**（`term.py:1398`；全 `src` grep `@router.websocket` 仅此一处）。帧形状：

| 方向 | 形状 | 处理 | file:line |
|---|---|---|---|
| C→S | `{"type":"resize","intent","rows","cols"}` | `_apply_size` | `term.py:1612-1622` |
| C→S | `{"type":"hb"}` | 回 `{"type":"hb","t"}`，不写 pty | `term.py:1623-1630` |
| C→S | `{"data": "<字节>"}` 或裸 bytes / 纯文本 | `data` 解出后 `os.write(sess.fd, data)` **原样写入 pty** | `term.py:1631-1658`（写入 `:1643`） |
| S→C | binary（裸 pty 字节） | `ws.send_bytes`，客户端 `term.write(raw)` | `term.py:1551`；`03-agents-cards.js:1413` |
| S→C | binary（首帧 = ring 回放） | `ws.send_bytes(bytes(sess.ring))` | `term.py:1529` |
| S→C | text `{"type":"auth_url"\|"page_url","url","auto"}` | 带外链接播报，不进画面 | `term.py:1518-1520` |
| S→C | text `{"type":"hb","t"}` | 心跳回执 | `term.py:1627` |
| S→C | close `4410` / `4404` / `4403` / `4401` | 会话结束/不存在/无权/未鉴权 | `term.py:1592`、`:1420-1452` |

- 终止：`DELETE /api/term/sessions/{sid}`（`term.py:1085-1113`）→ `Session.kill()`（`:665-673`）→ `_signal_group(SIGTERM)`，2s 后未退则 `SIGKILL`（`:675-689`），`_cleanup()`（`:691-721`）关 fd / 收尸。**这是进程级终止，不是语义级中断**。
- 输入进 pty 的只有字节：用户在界面敲 Ctrl-C 也是作为字节写入（前端 `termSend` 见下），**没有「中断当前轮次但保留会话」的服务端动作**。
- 全 `src` grep `steer|interrupt|startTurn|rollback|turn` 的命中：仅 `cli_agent.py:118` 解析 codex NDJSON 的 `turn.completed`、`hook.py:33` 技能触发 `usage_scope: session|turn`、以及 git `fork`/`fork 炸弹`/概率「归一化」等无关词。**无会话轮次、无插话、无分支/回滚语义**。「归一化」在全仓指 BM25/文本归一化，不是事件归一化。

**C. 前端会话交互路径 / WS 帧 / 事件呈现（`static/hub/*`）**

- 卡片按钮按 `entries` 类型渲染：`05-chat-and-history.js:343` 的 `MODE_LABEL = {embed, term, chat, detail, open}`；`:412-420` 实际只渲染 `embed` / `term` / `start` 三个动作按钮，`chat` 按钮在 v0.10.1 被删除（`:414-416` 注释称「chat entry 实测为死入口：claude / jcode 两个实体 POST /api/agents/{id}/chat 均 HTTP 500」）。
- 终端连接 `termConnect()` 在 `03-agents-cards.js:1358-1458`：`new WebSocket(wsUrl('/ws/term/'+sid))`、`binaryType='arraybuffer'`；`onmessage`（`:1364-1415`）把 string 且以 `{` 开头的帧当控制帧（`auth_url`/`page_url`/hb），**其余一律 `term.write(raw)` 交给 xterm.js 画 ANSI**（`:1413`），`termDecodeFrame` 的 `TextDecoder` 只用于 DECSET 鼠标态扫描（`02-nav-and-poll.js:576-582`、`590-617`），不产生语义事件。
- 输入 `termSend(data)`：`termWs.send(JSON.stringify({ data: payload }))`（`02-nav-and-poll.js:630-638`）；resize 帧 `03-agents-cards.js:65`。即客户端只往 pty 灌字节。
- 直连对话面板仍存在但走非流式：`04-terminal-ws.js:95-126` 的 `chatSend` → `POST /api/agents/{id}/chat`，`await` 整个响应后把 `d.response`（或 `d.error` + `model`/`usage`）一次性写成一条 `.msg`（`:117-121`）。
- 全 `static/hub/*.js` grep `EventSource|getReader|ReadableStream|chat/stream` 未见消费者 → **`/chat/stream` 这个 SSE 端点疑似无前端消费者**（未确认，见 §6 存疑项）。

### 1.3 派发执行路径（`src/tasks_runner.py`）

- `_run_task()`（`tasks_runner.py:559-680`）：`result = await asyncio.wait_for(_call_chat(chat_fn, ...), timeout=_tout)`（`:586-590`）→ 单次拿到 `result` dict → 解析 `usage`/`model` → 写 `tasks` 表状态、预算判定、`db.log_profile_event("task_exec", ...)`（`:619`、`:630`、`:668`）→ `_verify_and_settle`（`:637`）。
- `_call_chat()`（`tasks_runner.py:262-279`）：一次 `await chat_fn(...)`，按签名决定是否传 `timeout_s`/`model`。**无流式、无事件迭代**。
- `_execute_run()`（`tasks_runner.py:683-...`）：DAG 编排、CPU ticks 心跳停滞检测、重试/升档、`recover_interrupted_tasks`（`:854`）、`sweep_stale_tasks`（`:882`）。可观测性是 SQLite 状态列 + append-only `profile_events`。
- 全 `src` grep `StreamingResponse|event-stream|task.*stream|progress|broadcast|subscribe`：**任务进度没有独立事件流 / SSE / WS 推送**；前端靠轮询任务列表（`06-manager-tasks.js`）。唯一流式出口是 §1.2A 的 `/chat/stream`。

### 1.4 有没有「归一化事件」的雏形

无统一事件契约。存在三处**局部、异构**的结构，均非归一化事件：

1. **每家 CLI 一套事后清洗**：`adapters/cli_agent.py:98-146` 的 `_clean_text/_clean_codex/_clean_pi/_clean_omp/_clean_dsh`，在 `proc.communicate()` 拿到**整段 stdout** 后（`:445`、`:464-466`）解析成 `text + usage`。其中 `_clean_codex`（`:102-127`）已经在解析 codex 的 NDJSON 事件类型 `item.completed`（正文 `agent_message`，`:114-117`）与 `turn.completed`（`usage`，`:118-124`）——**这是全仓最接近「事件」的一处，但它是批式终端解析器，不是流式事件契约**。`SPECS`（`:151-183`）逐家登记 `bin/argv/clean/model_path/approve`。
2. **OpenAI 兼容流式**：`adapters/openai_compat.py:164-196` 只 yield 文本 `delta`，无工具调用 / 轮次 / 用量事件。
3. **终端 WS 带外控制帧**：`auth_url`/`page_url`/`hb`（`term.py:1518-1520`、`:1627`）——传输层控制，不是领域事件。

结论口径：**「各家用各家的裸输出 + 事后清洗」，没有归一化事件层**；也没有 turn 概念、没有 startTurn/steer/interrupt/fork/rollback 契约。

---

## §2 与 t3code 的差距清单（逐条）

> 对标基准：`T3CODE-FUSION-EVAL-20261109.md:57`（14 类 `ProviderAdapterV2Event` + runtime 方法）、`:50-54`（协议硬边界带版本 + 事件溯源 command→event→projection→outbox）。差距按「从 0 建」还是「补齐」标注。

| # | t3code 侧 | agenthub 现状 | 性质 | 证据 |
|---|---|---|---|---|
| 1 | 统一 `ProviderAdapterV2Event`（14 类） | 无统一事件类型；仅 `_clean_*` 各家清洗函数 | **从 0 建** | `cli_agent.py:98-146`、`base.py:13-39` |
| 2 | `startTurn`（明确的「一轮」边界 + id + 状态机） | 无 turn 概念：终端是字节流、chat 是一次 `chat()`、派发是一次 `wait_for` | **从 0 建** | `term.py:1631-1643`、`api_agents.py:444`、`tasks_runner.py:586` |
| 3 | `steer`（轮中插话） | 客户端只能把字节写进 pty；`chat()` 一次性，无法中途注入语义消息 | **从 0 建** | `02-nav-and-poll.js:637`、`api_agents.py:470-482` |
| 4 | `interrupt`（中断当前 turn，保留会话/线程） | 只有进程级 SIGTERM/SIGKILL 终止（会话销毁）与用户手打 Ctrl-C 字节 | **从 0 建，且语义不同** | `term.py:665-689`、`1085-1113` |
| 5 | `fork` / `rollback` + hidden git ref checkpoint | 有「按历史 id 重放新进程」的续聊 `resume_argv`；无分支、无回滚、无 checkpoint | **不做等价物**（见 §5） | `term.py:906-913`；eval 报告 `:60` |
| 6 | event → projection → 前端结构化渲染 | xterm.js 画 ANSI 字节；无 message/tool/thinking/usage 结构化面板 | **从 0 建** | `03-agents-cards.js:1413`、`05-chat-and-history.js:414-416` |
| 7 | 事件溯源 + outbox + 命令幂等 receipt | 派发是批式 DB 状态机 + `profile_events`；无事件流、无 outbox | **部分属 P1 批次**（receipt） | `tasks_runner.py:586-680`；eval 报告 `:181` |
| 8 | 协议硬边界带版本（`orchestrationProtocol=2`，否则 426） | 唯一 WS 无协议版本协商；`/chat/stream` SSE 无版本字段 | **与 P4 批次（协议能力协商 / `/api/meta`）重叠，须协调** | `term.py:1398`；`PENDING-TASKS.md:14412-14413` |
| 9 | 多 provider 统一 adapter 形状（SDK/ACP/JSON-RPC 结构化流） | 三类控制面并存且形状不同：pty TUI（主）/ 原生 embed iframe / chat 适配器 | **只有 headless CLI 适配层能长出统一形状** | `profiles.py:1017-1082`、`registry.py:18-37` |

**最关键的一条结构性差距（决定方案落点）**：agenthub 的**主交互入口是 pty TUI**（claude/codex/pi 的 `terminal.cmd`，`profiles.py:420/441/528`），TUI 往终端写的是 ANSI 字节，**原理上产不出 `tool_call`/`usage`/`turn` 这类结构化事件**；ANSI 解析是有损且各家版本易变的，不能作为事件源。t3code 之所以有 14 类事件，是因为它走的是各家 **SDK / stdio JSON-RPC / ACP 的结构化流**（eval 报告 `:57`）。agenthub 里唯一已经在用结构化输出的是**派发侧的 headless CLI 适配器**（`codex exec --json`、`claude -p --output-format text`、`pi -p` 等，`cli_agent.py:52-90`）——但它把整段 stdout 一次性 `communicate()` 回收再清洗（`cli_agent.py:445`），不是增量事件流。

---

## §3 最小可行方案（分层：事件契约 / 服务端适配 / 前端）

### 3.0 落点裁决（先定这一条，否则范围必炸）

**只在「headless / 直连适配层」建事件契约，不碰 pty/TUI 终端链路。** 现有 xterm.js 终端、ring 回放、多观看者队列、鉴权、重连全部保持不变；新事件流是**并列的第二条通道**。理由见 §2 末段。

### 3.1 事件契约层（新增，最小集合）

- 新增 `src/adapters/events.py`（或 `src/events.py`）：一个版本化的事件 envelope + **v1 只 6 类**（不是 14 类）：
  `turn_started` / `message_delta`（文本增量） / `tool_call` / `tool_result` / `usage` / `turn_completed`，外加 `error`。
- 统一字段：`{v, seq, ts, sid, turn_id, agent_id, type, ...}`。`seq` 单调递增、`turn_id` 标识一轮、`v` 供 P4 能力协商对齐。
- 明确 `error{reason}` 的枚举含 `interrupted` / `unsupported_steer` / `timeout` / `provider_error`，禁止静默丢弃。
- **复用**：沿用 `db.log_profile_event` 的「append-only、失败不得带倒主流程」记账习惯（`api_agents.py:446-453`）；不新建事件溯源表 / projection / outbox（§5）。

### 3.2 服务端适配层（复用 + 新增）

**复用现有（不动既有契约）**

- `BaseAdapter.chat()` / `chat_stream()` 签名**不改**（`base.py:13-29`）——派发 `tasks_runner._run_task`、对话页 `_chat_dispatch` 是既有消费者，改签名会连坐。
- `CliAgentAdapter.SPECS`（`cli_agent.py:151-183`）与 `_clean_codex` 的 NDJSON 解析（`:102-127`）作为 **codex 事件解析的种子**（把「事后解析整段」改成「逐行增量解析」）。
- `OpenAICompatAdapter.chat_stream()`（`openai_compat.py:164-196`）作为 `message_delta` 来源（把 `{"delta":...}` 包成事件）。
- `api_agents._chat_dispatch()` 的 history / `chat_sessions` / `chat_messages` 落盘（`api_agents.py:437-466`）作为会话持久化，不另起一套。
- `term.py` 的 WS 传输范式作**参照**：多观看者独立队列（`viewers`，`:587`）、回放（`ring`，`:592`）、合并节流（`_OutputCoalescer`，`:736-783`）、accept 后关闭业务码（`:1427-1436`）、Cookie 会话鉴权（`:1400-1416`）。

**新增（最小）**

- `BaseAdapter` 增一个**可选**方法 `chat_events(message, ...) -> AsyncIterator[Event]`（默认实现 = 把现有 `chat()` 结果包成 `turn_started → message_delta → turn_completed`，零改造即可让所有适配器「有事件」）。
- 2 家 provider 做**真流式** v1：
  - `codex`：`CliAgentAdapter` 把 `proc.communicate()`（`cli_agent.py:445`）换成逐行 `readline()` 增量读 `stdout`，把 `item.completed`/`turn.completed` 映射成事件（复用 `_clean_codex` 的字段识别）；
  - `openai_compat`（claude/jcode 端点）：包装现有 SSE `delta` → `message_delta`。
  - 其余 8 家 CLI 暂走默认包装（只有开始/结束/整段文本），**不逐家做结构化**（§5）。
- 一条新事件出口：`GET /ws/events/{sid}`（或 `POST /api/agents/{id}/chat/events` 的 SSE）。**倾向新端点**：复用 `/ws/term/{sid}` 会把 JSON 事件混进 pty 字节流，正是 `term.py:1457-1458` 注释明令禁止的事。
- 两个控制动词（最小）：
  - `interrupt`：对运行中的 headless 进程组发 `SIGTERM`→`SIGKILL`，复用 `cli_agent._kill_group`（`:231-242`）与 `reap_orphans`（`:280-325`），产出 `turn_completed{reason:"interrupted"}`，**不销毁会话**。
  - `steer`：向该轮的 prompt 队列注入一条消息；不支持者回 `error{reason:"unsupported_steer"}`，不静默吞。
- **不做** `fork` / `rollback`（§5）。

### 3.3 前端（新增一个并列面板，不重写终端）

- 复用 `03-agents-cards.js` 的 `termConnect` WS 客户端范式（hb / 重连 / 尺寸所有权）、`05-chat-and-history.js` 的 `MODE_LABEL` 模式切换。
- 新增一个**结构化事件面板**：按 `type` 渲染 `message_delta`（流式文本）/ `tool_call`/`tool_result`（折叠块）/ `usage`（token 数）/ `turn_completed`；与现有 xterm.js 终端**并列可切换**，两套字节流互不污染。
- 新增「中断」按钮 → 调 `interrupt` 端点；事件面板显示 `interrupted` 终态。
- **不**把 `{"type":...}` 事件写进 `term.write()`（会像 `auth_url` 一样被当正文画出来，见 `03-agents-cards.js:1369-1382` 的既有教训）。

### 3.4 复用 / 新增一览

| 层 | 复用（现有） | 新增（最小） |
|---|---|---|
| 事件契约 | `db.log_profile_event` 记账习惯 | `events.py`（envelope + 6 类 + `v`/`seq`/`turn_id`） |
| 服务端适配 | `BaseAdapter` 形状、`SPECS`、`_clean_codex`、`openai_compat.chat_stream`、`_chat_dispatch` 落盘、`term.py` WS 范式 | `chat_events()` 默认包装、2 家真流式（增量 `readline` 改造）、`/ws/events/{sid}`、`interrupt`/`steer` |
| 前端 | `termConnect` 客户端、`MODE_LABEL` 模式切换、xterm.js 终端（保持不动） | 事件面板 + 中断按钮（并列，不替换终端） |

---

## §4 工作量拆分 + 对「3–5 人天」复核

### 4.1 按 §3 最小方案拆分（人天，含 L0 与影子实例复验）

| # | 工作项 | 人天 | 主要不确定性 |
|---|---|---|---|
| 1 | 事件契约 envelope + 6 类 + 版本/序号/错误枚举 + 单测 | 0.5 | 与 P4 协议版本字段对齐 |
| 2 | `chat_events()` 默认包装（所有适配器零改造有事件）+ 2 家真流式（codex 增量 `readline` 替换 `communicate`、openai SSE 包装） | 1.5 | `communicate()`→增量读是实质改造，需处理 stderr 合流、背压、退出码 |
| 3 | `interrupt`/`steer` 语义（进程组终止 + 轮中注入 + unsupported 显式回包） | 1.0 | 批式 `chat()` 必须先流式化才能插话 |
| 4 | `/ws/events/{sid}`（多观看者队列、回放、鉴权、accept 后业务码）+ `interrupt`/`steer` 端点 | 1.0 | 新端点 vs 复用 term WS 需裁决 |
| 5 | 前端事件面板 + 中断按钮 + 模式切换（与终端并列） | 1.0 | 不能污染 pty 字节流 |
| 6 | 端到端复验：影子实例（`env -i` 净环境）+ L0 全量 + **生产重启窗口**（`term_sessions>0` 须用户签字，走 `hub-restart.sh`）+ prepush ⑦ 指纹闸门 | 0.5–1.0 | 重启窗口不由本批控制 |
| | **合计** | **5.5–6.0** | |

### 4.2 复核结论：**部分不同意**

- **3 人天**：只够「缩减版」——事件契约 + 1 家 provider 真流式 + 只读事件投影（**不做** `steer`/`interrupt`、不做前端交互）。按 §3 的完整最小方案不成立。
- **5 人天（上限）**：接近但**略超**。§4.1 表合计 5.5–6.0，超出的 0.5–1.0 天来自三处被低估的成本：
  1. **`communicate()` → 增量读的流式化改造**（`cli_agent.py:445`）——现有 CLI 适配器是「一次收整段 stdout 再清洗」，`steer`/`interrupt` 必须先把它改成增量，这是新代码不是接线；
  2. **生产重启窗口与重启军规**（`PENDING-TASKS.md:14412`：P3 需改核心代码并重启，当前 `term_sessions=1`，须用户指定窗口或签字 `HUB_RESTART_FORCE=1`）——P3 排最后但重启成本不归零；改完还须「先重启再过 prepush ⑦」（指纹闸门，AGENTS.md）；
  3. **L0 全量基线 4067**（`PENDING-TASKS.md:14411`）与多观看者 / 不污染 pty 字节流的回归面。
- **若按字面「把 t3 的 14 类事件 + 5 个控制动词都移植到现有多 provider（含 pty TUI 会话）」**：远超 3–5 人天，粗估 **8–12 人天**，且 pty TUI 路径原理上不可靠（§2 末段）。eval 报告 `:193` 已判「orchestration-v2 全套：事件类型与投影耦合极深，只取 receipt 与 checkpoint 两点即可」——本文 §3 与该结论一致，只借「归一化事件契约」这一层设计，不搬整套。
- 另需澄清口径：**「3–5 人天」是否包含生产重启窗口与 L0 全量复验，未确认**（`PENDING-TASKS.md:14413` 未写明）。若不含，纯编码面约 4–5 人天，与原文基本吻合。

---

## §5 明确不做（避免过度设计）

1. **不解析 pty/TUI 的 ANSI 输出**成事件（有损、各家版本易变）；现有 xterm.js 终端与 `/ws/term/{sid}` 字节流保持原样。
2. **不实现 14 类事件的完整集合**；v1 只 6 类 + `error`，够支撑「同一前端看多家 provider 的一轮」即可。
3. **不做 `fork` / `rollback` / checkpoint / hidden git ref**：依赖工作区与会话协同、会污染仓库（eval 报告 `:60`、`:182`），属另批；agenthub 已有 `resume_argv` 续聊够用。
4. **不引入事件溯源 / Orchestrator / projection / outbox 全套**：eval 报告 `:193` 已判不建议；receipt 是 P1 批次（`PENDING-TASKS.md:14412`），不在此重复。
5. **不重写前端为 React / 不接 t3 客户端**（约 200 个 RPC，eval 报告 `:165`）；不替换现有对话页与派发链路。
6. **不改 `BaseAdapter.chat()` / `chat_stream()` 既有签名**、不改 `chat()` 返回 dict 契约——派发（`tasks_runner`）与对话页是既有消费者，避免连坐。
7. **不把 10 家 CLI 全部结构化**：v1 只 2 家真流式，其余走默认包装；不逐家维护 `argv`/输出格式映射表。
8. **不碰 CCR / 共享配置、不新增凭据、不改 systemd unit**；不动 `src/authz.py`、`writeauth.py`、`users.py`、`meta.py`、`main.py`、`db.py`（并行会话在改）。
9. **本批不执行生产重启、不 git commit**：重启须用户指定窗口 / 签字，走 `agenthub/scripts/hub-restart.sh --reason "..."`。
10. **不与 P4（协议能力协商 `/api/meta`）重复定义版本字段**：事件 `v` 字段须与 P4 协商结果对齐，冲突时以 P4 为准。

---

## §6 验收判据（可执行）

1. **L0**：新增 `tests/test_event_contract.py`——每类事件必填字段、`v`/`seq` 单调递增、未知 `type` 不抛异常、`error.reason` 枚举收敛；`bash scripts/run_tests.sh hermetic-clean` **全绿且 ≥ 现有基线 4067**（`PENDING-TASKS.md:14411`）。
2. **影子实例复验（净环境，不碰生产）**：按 `env -i` 净环境起影子 hub（避免宿主 PATH/端口/HOME 假绿，AGENTS.md 口径），回环探针加 `--noproxy '*'`；对 codex 与 openai_compat 各跑一次真实请求，断言事件序列为 `turn_started` → ≥1 条 `message_delta` → `turn_completed`（含 `usage`），且**无 ANSI 字节混入事件字段**。
3. **语义逐位不变**：同一 prompt 走旧 `chat()` 与把新事件流 `message_delta` 拼回的文本 `==`（保证加事件层不改回答内容）。
4. **interrupt**：长任务发起后调 `interrupt` → ≤2s 收到 `turn_completed{reason:"interrupted"}`；`os.killpg` 后无孤儿（`cli_agent.reap_orphans` 可查）；**会话仍可续发下一条**（不是销毁）。
5. **steer**：轮中注入一条消息 → 后续事件里出现该输入的回包；不支持者必须回 `error{reason:"unsupported_steer"}`，**不得静默丢弃**。
6. **前端并存**：影子实例里事件面板渲染出结构化 message/tool/usage，且同一 sid 的 xterm.js 终端仍可正常敲键/回滚；两套通道互不污染（断言 pty 字节流里不含 `{"type":`）。
7. **多观看者**：2 个 WS 连同一 sid，各收到**完整且相同**的事件序列（复用 `term.py` `viewers` 独立队列语义，不互相偷帧）。
8. **协议版本**：事件帧带 `v`；不带 / 错版回明确错误码（对齐 P4 `/api/meta`，不冲突、不重复定义）。
9. **生产复验顺序**：改完**先重启再过 prepush ⑦**（指纹闸门拿运行进程 boot sha 对磁盘代码）；重启走 `hub-restart.sh --reason`，`term_sessions>0` 须用户签字；重启后 `/health` 200 且 `code_stale=false`。
10. **不回归**：派发链路仍走批式 `chat()`，一条真任务跑通 `success`；对话页 `POST /api/agents/{id}/chat` 仍 200；`GET /api/term/activity`、`/ws/term/{sid}` 行为逐字不变。

---

## §7 未确认 / 存疑（如实标注，供裁决时补证）

1. **claude-code 在本机是否支持 `--output-format stream-json`（含 `--include-partial-messages`）**：未实测。本机只在派发侧确认了 `claude -p --output-format text`（`cli_agent.py:53`）。v1 真流式选了 codex（已有 `--json` NDJSON，`_argv_codex` `:56-59`）与 openai_compat（已有 SSE），故不依赖此结论；若要扩到 claude SDK 流式，须先实测。
2. **t3 的 14 类 `ProviderAdapterV2Event` 具体名称**：eval 报告只写「14 类」未逐一列名（`:57`）；本文 §3.1 的 6 类是按通用语义拟的**最小集**，未逐条对照 `ProviderAdapter.ts:48-715` 源码。若要逐条对标，须回 `/vol1/eval/t3code` 读原文（该目录 eval 报告 `:408` 标注「保留待裁后删」，可能仍在）。
3. **`/chat/stream` 是否有前端消费者**：全 `static/hub/*.js` grep 未见 `EventSource`/`getReader`/`chat/stream`，疑似无消费者；但 `04-terminal-ws.js:95-126` 有非流式 `chatSend`、`05-chat-and-history.js:414-416` 注释称卡片 chat 按钮已删且 chat 是「死入口」。**chat 模式当前在 UI 上是否可达、chat 端点现网是否仍 500，未确认**。
4. **事件出口选新 `/ws/events/{sid}` 还是复用 `/ws/term/{sid}` 带外帧**：本文倾向新端点（避免污染 pty 字节流），但复用可省鉴权/多观看者代码——**需裁决**。
5. **`steer` 的语义边界**：t3 的 steer 是「轮中插话」，而 agenthub 的 headless CLI 多为「一进程一任务、无会话续接」（`cli_agent.py:9-10`）。v1 的 `steer_unsupported` 回包是否算「借鉴到位」，需用户裁定。
6. **并行改动导致行号漂移**：`static/hub/03-agents-cards.js`（p2-conn）、`02-nav-and-poll.js`、`src/tasks_runner.py` 等正在被其它会话修改（发现 `03-agents-cards.js.bak-20261109_102119-p2conn`）。本文事实按本会话读到的磁盘内容，合入后行号可能偏移；**设计结论不依赖精确行号**。
7. **「3–5 人天」口径**：是否含生产重启窗口、L0 全量复验、影子实例，原文未写明（§4.2）。

---

*本文为只读调研 + 设计，未修改 agenthub 任何源码 / 配置 / 服务端状态；唯一写盘为本文件。*
