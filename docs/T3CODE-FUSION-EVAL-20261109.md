# T3 Code 评估与融合报告

- 评估对象：`https://github.com/pingdotgg/t3code`（MIT，TypeScript，monorepo）
- 评估时间：2026-11-09 08:30–09:10（UTC+8）
- 评估方式：源码浅克隆（`/vol1/eval/t3code`，HEAD `d5290e9`）+ 官方 docs 全文 + GitHub API + 联网检索 + **本机隔离实测**（独立 `T3CODE_HOME`，端口 3799，不触碰 agenthub）
- 结论口径：只写实测到的；没测的写「未测」

---

## 0. 结论（先看这段）

1. **它不是同类系统，是「控制面」**：agent 仍在你的机器上跑，t3 只做「手机/网页/桌面 → 本机 agent」的远程操控与编排。与 agenthub（编目/记忆/技能/派发）不同层，**可并存，不宜合并**。
2. **远程功能不能直接复用**：T3 Connect = Clerk 身份 + Cloudflare Workers/Durable Objects/Queues + cloudflared + Alchemy 部署 + APNs/FCM，自托管是一整个独立工程；LAN 配对与 Tailscale Serve 两层**可以抄设计与少量代码**。
3. **客户端能复用，但只有一种姿势**：跑一个 t3 server，官方 web/桌面/手机客户端即可连（本机 web 端已端到端验通）；**不能**把 t3 的客户端接到 agenthub 上——那需要实现它约 200 个 RPC 方法。
4. **最值得抄的是四件小事**：一次性配对码 + 会话 scope、连接 supervisor、命令幂等 receipt、hidden git refs checkpoint。合计约 5–6 人天。
5. **两个必须知道的坑**：① 遥测默认开启，会向 `ingest.t3.codes` 上报哈希后的 OpenAI/Claude 账号标识，必须 `T3CODE_TELEMETRY_ENABLED=false`；② 0.1.7 无头部署下 MCP OAuth 审批实测 400 `unknown app`（3 次复现），外部 agent 接入这条路当前不通。

**下一步（2 分钟内）**：决定走哪一档——
- A 并存（推荐，0 代码）：在 3102 主机旁跑 `t3 serve`，用手机/桌面客户端操控同机 claude/codex/pi；
- B 只借鉴（1 周）：按 §6 的 P0/P1 清单改 agenthub；
- C 深度融合：不建议，成本 >> 收益（理由见 §6.3）。

---

## 1. 项目基本面

| 项 | 值 | 取证 |
|---|---|---|
| 定位 | "agent harness control surface"，控制本机已装好的 agent | `README.md:3` |
| 支持的 provider | Claude Code / Codex / Cursor / Grok Build / OpenCode / Antigravity / fx / pi / kiro / muse | `docs/user/providers-*.md`、`apps/server/src/orchestration-v2/Adapters/` |
| License | MIT | `LICENSE:1` |
| 语言/构建 | TypeScript，pnpm 12，`vp`（vite-plus）；根 `engines.node=26.12.1`，server 包 `^22.16 \|\| ^23.11 \|\| >=24.10` | `package.json:68,71,73`、`apps/server/package.json` |
| 规模 | 31,798 文件 / 550MB 工作树；30 个 `packages/*` 全部 `private:true` | `git ls-files \| wc -l`、`du -sh` |
| 热度 | ★ 52,487 / fork 10,015 / open issues 5,955（2026-11-09T00:33Z） | GitHub API |
| 活跃度 | 创建 2026-02-08，最近推送 2026-11-09T00:28Z（当天） | GitHub API |
| 成熟度自述 | "We are very very early… Expect bugs"，且**基本不接受大功能 PR** | README + 第三方评测 |
| 服务端 | 包名 `t3` v0.1.7，默认端口 3773，SQLite（`node:sqlite`），40 个直接依赖，含 `node-pty`/`ffi-rs`/`playwright-core` 等原生件 | `apps/server/src/config.ts:29`、`package.json` |
| 运行体量（本机实测） | 安装 366MB；空载 + 1 次失败线程 + 1 个浏览器会话后 **RSS 420MB**；数据目录 7.9MB | `ps -o rss`、`du -sh` |

> 第三方资料与本地事实一致：这是「手机当遥控器」的控制面；免费 T3 Connect 限 3 台机器；默认权限档是 Full access；项目极早期。来源：`wpnews.pro`、`juejin.cn/post/7703026692583718955`、`zendot.org`、`crazycoderslab.com`（2026-10 至 11 月）。

---

## 2. 架构理解（六条承重设计）

1. **执行留在环境，客户端只走 RPC**
   > "A remote client must never substitute its own filesystem, provider credentials, or machine state for the environment's." `docs/internals/overview.md:3-6`
   provider 进程、终端、Git、项目文件都归 server；`packages/client-runtime` 只放共享连接/域状态，web 与 mobile 复用，避免重连逻辑分叉（`:10-14`）。

2. **协议是硬边界且带版本**
   `packages/contracts/src/rpc.ts:411-621` 约 200 个点分方法，每个方法在 `apps/server/src/auth/RpcAuthorization.ts:40-217` 静态声明 scope（`satisfies Record<...>` ⇒ 漏声明编译不过）。WS 必须带 `orchestrationProtocol=2`，否则 `426`（`apps/server/src/ws.ts:740-745`）。

3. **事件溯源 v2：command → event → projection → outbox**
   `Orchestrator.ts`（14,672 行）只做决策不碰 I/O；`EventSink` 在一个事务里提交事件 + 投影 + 命令回执 + outbox；`commandReceipts.insertIfAbsent` 提供**命令级幂等**；副作用由 `EffectWorker` 在提交后执行（`docs/internals/overview.md:45-60`、`apps/server/src/orchestration-v2/Orchestrator.ts:13992-14023`）。

4. **provider 适配层**
   统一 `ProviderAdapterV2Event`（14 类事件）与 runtime 方法（startTurn/steer/interrupt/rollback/fork…），Claude 走 `@anthropic-ai/claude-agent-sdk`，Codex 走自研 `effect-codex-app-server`（stdio JSON-RPC），Cursor/Muse/OpenCode 走厂商 SDK，Grok/Fx/Kiro/Antigravity 走 ACP（`packages/provider-core/src/server/ProviderAdapter.ts:48-715`）。

5. **checkpoint 用隐藏 git ref**
   `refs/t3/orchestration-v2/checkpoints/<hash>/ordinal/<n>`，不往用户分支加提交，回滚要同时协调工作区与 provider 会话（`apps/server/src/orchestration-v2/CheckpointService.ts:26,151-162`、`docs/internals/overview.md:88-91`）。

6. **远程四路 + 一套鉴权**
   LAN 配对 / T3 Connect（CF 隧道）/ Tailscale Serve / 桌面托管 SSH；统一落到一次性 token → 会话（cookie / bearer / DPoP），session 带 16 个可授 scope（`packages/contracts/src/auth.ts:31-77`）。

---

## 3. 本机实测（全部可复现，见 §8）

测试环境：`T3CODE_HOME=/home/gztxt/.t3-eval`，`--base-dir .../data`，端口 **3799**，`--host 0.0.0.0 --lan`；agenthub 全程未动（结束后 `/health` 仍 200、`version=0.13.198`、`term_sessions=1`）。

### 3.1 启动与对外描述

| 探测 | 结果 |
|---|---|
| `GET /` | 200，21,702B，SPA |
| `GET /.well-known/t3/environment` | 200，`environmentId=81f64697-…`、`serverVersion=0.1.7`、`orchestrationProtocolVersion=2`、`databaseMigration=79`、约 60 项 capabilities |
| `GET /api/auth/session`（未登录） | 200，`authenticated=false`，`policy=remote-reachable`，`bootstrapMethods=[one-time-token]` |
| `GET /mcp`（无凭据） | **401**，`www-authenticate: Bearer resource_metadata=…/mcp` |
| 从 tailnet `100.117.232.62:3799` / 局域网 `192.168.5.102:3799` | `/` 与 `/api/auth/session` 均 200（外壳公开，数据需鉴权） |
| 启动输出 | `Connection string / Token(12 位) / Pairing URL(http://ip:3799/pair#token=…) / App link(t3code://pair…)` + 终端 QR |

### 3.2 配对与鉴权（核心能力）

| 测试 | 结果 |
|---|---|
| 一次性 token 换会话 `POST /api/auth/browser-session` | **200**，`authenticated=true`，`expiresAt=+30 天`，16 个 permissions，`sessionMethod=browser-session-cookie` |
| 同一 token 重放 | **401** `invalid_credential`（原子消费，`PairingGrantStore.ts:554-566`） |
| `t3 pair --scope orchestration:read` 再换会话 | **200**，`scopes=["orchestration:read"]`、`permissions` 仅 1 项（scope 真实生效，不是摆设） |
| WS `?orchestrationProtocol=2` + cookie | 握手成功（5s 内无服务端帧，等服务端等客户端先说话） |
| WS `?orchestrationProtocol=1` + cookie | **426** |
| WS 匿名（loopback） | **401** |
| WS 匿名（LAN） | **401** |

### 3.3 浏览器端到端（证明客户端真能用）

用系统 Chromium（154）+ CDP，打开 `http://127.0.0.1:3799/pair#token=<一次性码>`：

- 18s 后 `document.body.innerText` 已进入主界面：`New thread / What should we build? / proj / alibaba/qwen3.8-Flash / Full access`，URL 变为 `/draft/<uuid>`；
- `Runtime.consoleAPICalled` / `Log.entryAdded` / `exceptionThrown` **0 条**；
- 说明：一次性配对码 → cookie 会话 → RPC 建连 → 渲染工作区，整链通。

> 反面记录：用 `--virtual-time-budget` 的 `--dump-dom` 会误报 "T3 Code could not confirm this workspace"——虚拟时间让首屏 15s 超时瞬间触发。**是测法假阴性，不是产品缺陷**，换真实时钟 + CDP 即通过。

### 3.4 provider 控制（真实拉起进程）

| 命令 | 结果 |
|---|---|
| `t3 project add /home/gztxt/.t3-eval/proj` | 200，项目已建 |
| `t3 thread start --provider claude --model …` | 400 `Provider instance "claude" is disabled for project`（实例 id 是 `claudeAgent`，不是 `claude`） |
| `--provider claudeAgent --model claude-sonnet-5-5` | 线程执行到上游：`Failed to authenticate. API Error: 403 … [ccr_fallback\|400] … [ccr_fallback\|402]` |
| `--provider codex --model alibaba/qwen3.8-flash` | 线程执行到上游：`HTTP 403 Forbidden … url: http://127.0.0.1:3456/v1/responses` |

**解读**：t3 正确识别本机 `claude`（2.1.318，`auth.type=customEndpoint`）与 `codex`（0.166.0），并**继承本机 CCR 配置**（`127.0.0.1:3456`）拉起真实进程；失败点是本机网关当前降级——实测 `GET 127.0.0.1:3456/v1/models` 返回 **0 个模型**。**不是 t3 的缺陷**，也没为测试去改 CCR。

### 3.5 MCP / 外部 agent 接入（发现一个真实缺陷）

| 测试 | 结果 |
|---|---|
| `GET /.well-known/oauth-protected-resource` | 200，`scopes_supported=[orchestration:read, orchestration:operate]` |
| `GET /.well-known/oauth-authorization-server` | 200，code + PKCE(S256) + 动态注册 |
| `POST /oauth/mcp/register` | **201**，返回签名式 `client_id`（无状态，带 name/redirect） |
| `GET /oauth/mcp/authorize`（错 PKCE） | 302 `error=invalid_request: PKCE … is required`（校验生效） |
| `POST /oauth/mcp/approval`（正确 client_id + PKCE + 会话 cookie） | **400 `This sign-in link names an unknown app`**，重复 3 次（含同秒注册、带 cookie 注册）均复现 |
| `t3 auth session issue --scope orchestration:read --token-only` 换来的 bearer 调 `/mcp` | **401 invalid_mcp_credential**（bearer 会话不是 MCP 凭据，MCP 要求 `sub=mcp-client`） |

**结论**：0.1.7 无头部署下，MCP OAuth 的「注册 → 审批」断在审批（`parseClientId` 验签失败）。**外部 agent 通过 MCP 接 t3 这条路当前不可用**；官方 app 内是否同样失败未验证（无 GUI 环境）。

### 3.6 HTTP 互操作缝（可用）

| 测试 | 结果 |
|---|---|
| `GET /api/projects` 带 `Authorization: Bearer <scoped session>` | **200**，返回项目 JSON（438B） |
| 同上不带 token | **401** |
| `GET /api/orchestration/shell` 带 token | 400（需要 query 参数，未深究） |

即：**读侧有普通 HTTP 接口 + bearer scope**，agenthub 可以低成本只读镜像 t3 的项目/线程；写侧走 WS RPC 或 `/api/projects/mutate`。

### 3.7 遥测（默认开，必须关）

`docs/user/telemetry.md:1-12`：默认向 `ingest.t3.codes` 发产品事件，并带**哈希后的 T3 Connect / OpenAI / Claude 账号标识**与每设备随机 id；不含提示词/文件内容。关闭：`T3CODE_TELEMETRY_ENABLED=false`。relay tracing 另由 `T3CODE_OTEL_SDK_DISABLED` 控制。

---

## 4. 「复用它的远程功能」逐项裁定

| 路径 | 能否复用 | 依据 | 裁定 |
|---|---|---|---|
| T3 Connect（公网中继） | **否** | `infra/relay` 97 个 TS 文件 + Cloudflare Durable Objects/Queues + Clerk + cloudflared 版本钉 + Alchemy 部署 + APNs/FCM；relay 只做发现与一次性凭据，不代理流量（`docs/internals/t3-connect.md:3-12,44-72`） | 自托管=独立项目，且要引入第三方身份与云账号；不碰 |
| LAN 直连 + 配对 | **可抄** | 一次性 12 位 token（去 0/1/I/O）、5 分钟 TTL、fragment 携带、原子消费、scope 化会话（`startupAccess.ts:154-160`、`PairingGrantStore.ts:252-258`） | **P0 借鉴** |
| Tailscale Serve | **可抄** | `packages/tailscale` 仅 2 个依赖（`shared`+`effect`），核心是 `serve --bg --https=<port> http://127.0.0.1:<port>` + `/.well-known/…` 探活（`packages/tailscale/src/tailscale.ts:440-449,653-670`） | **P2 借鉴**（本机 tailnet 已有 `:8443 → 127.0.0.1:3102`，**未改动**） |
| SSH 托管 | **部分** | 实现只在 desktop（`packages/ssh` + `apps/desktop/src/ssh/…`），headless CLI **没有** ssh 子命令；依赖 `contracts/shared/effect` | 搬包成本中等，收益低 |
| MCP 对外 | **当前不可用** | §3.5 审批 400；bearer 不是 MCP 凭据 | 等上游修，或自建 bridge（已有第三方 `deel77/t3code-mcp` 用配对凭据 + CF Access） |

**一句话**：它的「远程」= 订阅云 + 三条自建路；**云那层不能要，三条自建路值得抄两条**。

---

## 5. 「复用其他平台客户端」逐项裁定

| 客户端 | 复用方式 | 实测/依据 | 结论 |
|---|---|---|---|
| Web（自带） | 零成本：跑 server 即可用 | §3.3 端到端通 | ✅ 可直接用 |
| Electron 桌面 | 指向自建 server | 同一 RPC + pairing；未装 GUI 验证 | ✅ 可用（未实测） |
| iOS / Android | 商店 App 指向自建 server | 需 pairing / T3 Connect；**未装移动端实测** | ✅ 设计上可用（未实测） |
| 让 t3 客户端控制 **agenthub 的会话** | 需实现 t3 的 RPC 协议 | 约 200 个方法 + effect/rpc + 命令幂等 + 60 项 capabilities；`packages/contracts` 私有、无 OpenAPI/proto | ❌ 不可行 |
| 把 t3 页面嵌进 agenthub（iframe） | agenthub `:3103` 已有嵌页反代 | 会话 cookie `SameSite=Lax`，跨源 iframe 不发送；实测未做，按语义判定会掉登录 | ❌ 不建议 |
| 反过来：agenthub 读 t3 状态 | HTTP `/api/projects` + scoped bearer | §3.6 200/401 实测 | ✅ 只读可行，成本 <1 天 |

**现实姿势**：**并存**。agenthub 继续做「编目 / 记忆 / 技能 / 派发 / 面板」；t3 做「手机和桌面上直接操作同一台机器的 claude/codex/pi」。两者共享同一批 CLI 和同一台机器，不共享代码。

---

## 6. 借鉴 / 融合方案

### 6.1 建议抄的四件事（按性价比）

| 优先级 | 能力 | 现状（agenthub） | 抄什么 | 工作量 |
|---|---|---|---|---|
| **P0** | 设备配对 + 一次性码 + 会话 scope | 只有用户名密码 → Cookie/Basic（`src/users.py:157-180`），无配对、无 scope | 12 位无歧义码、TTL、fragment 传 token、原子消费、scope 列表、30 天会话、按会话显示权限 | 1.5–2 天 |
| **P1** | 连接 supervisor | **已有 70%**：心跳 30s 看门狗、半开检测、指数退避、重连前清单对账、回前台/网络恢复补算（`static/hub/03-agents-cards.js:61,79,102-140`）；缺「LAN 3s / 隧道 15s 分级探测」与「离线不耗重试」 | 只补分级探测与离线计数，不重写重连 | 0.5 天 |
| **P1** | 命令幂等 receipt | MCP 动作面**已有** `idempotency_key`（`src/resilience.py:274`、`src/mcpgw.py:556-595`）；缺的是**任务分派层**的控制命令回执（`src/tasks_runner.py:836-880` 只有 recovery_note） | `INSERT … ON CONFLICT(command_id) DO NOTHING RETURNING` + 重放回执（`Orchestrator.ts:13992-14023`）；**只覆盖 hub 自己发起的命令，覆盖不了 agent 在内部执行的 shell** | 2 天（含建表/迁移） |
| **P2** | checkpoint 用隐藏 git ref | 无 | `refs/hub/checkpoints/<hash>/ordinal/<n>`，不污染分支（`CheckpointService.ts:26`） | 1 天 |
| **P2** | Tailscale Serve 封装 | 依赖手工 | `serve --bg --https=…` + 探活 + 冲突检测（端口被占/服务别人的环境） | 0.5 天 |

合计 **约 6 人天**（P0+P1 4.5 天）。全部可增量落地，不推翻现有 FastAPI/前端。

### 6.2 不建议抄

| 项 | 原因 |
|---|---|
| T3 Connect 全套 | 引入 Clerk + Cloudflare + APNs/FCM，运维面 ≥ 本系统本身；且免费 3 台限制绑在对方账号 |
| 整个前端（React + client-runtime） | agenthub 是 Jinja + 12,255 行原生 JS（`scripts/build_hubjs.sh`），重写成本远大于收益 |
| orchestration-v2 全套 | `Orchestrator.ts` 单文件 14,672 行，事件类型与投影耦合极深；只取 receipt 与 checkpoint 两点即可 |
| 设备模拟器 / 远程浏览器 | 依赖 expo-device-hub / headless Chrome / WebCodecs / WebRTC，与本机需求无关 |

### 6.3 为什么「深度合并」不划算

1. 语言栈不同：Python 54,722 行 vs TS monorepo 550MB / 31,798 文件，合并等于重写一侧；
2. 领域不同：agenthub 的核心资产是**编目 38k 对象 / 记忆 / 技能 / 派发**，t3 是**会话控制面**，两者唯一的交集是「本机 CLI 进程」，而这层用进程边界共存即可；
3. 稳定性：t3 0.1.7、5,955 open issues、自述 expect bugs；把它放进生产面板会引入不可控故障面；
4. 法务/合规：MIT 可拷代码，但 30 个包全 `private`、无构建产物导出，只能拷源码进 TS 工程——对 Python 侧的 agenthub 不成立。

---

## 7. 风险与红线

1. **遥测默认开**（§3.7）：任何试点先设 `T3CODE_TELEMETRY_ENABLED=false`，并把该变量写进 systemd unit 的 `Environment=`，不能只写在 shell。
2. **默认权限是 Full access**（第三方评测与 `permission-modes` 文档一致）：给手机装了就等于给本机 shell；试点先用 `Supervised`。
3. **配对 URL / 授权码等同密码**（`docs/user/remote-access.md:450-451`）：不要进截图、日志、报告。
4. **`--lan` 会绑 `0.0.0.0`**：本机实测确实从 tailnet 与局域网可达，靠 pairing 拦；不要在没有 pairing 的情况下裸跑。
5. **原生件与平台**：`node-pty`/`ffi-rs`/`transcoder` 等；Linux 需 glibc（musl 不支持）；本机 Node 24.18 可用（server 要求 ≥24.10）。
6. **不要为测试动 CCR**：本次失败是网关降级（`/v1/models` 返回 0），按军规未改任何共享配置。

---

## 8. 复现命令（照抄即可）

```bash
# 1) 安装（隔离目录，勿用 /vol1：该卷目录权限会致 EACCES）
mkdir -p /home/gztxt/.t3-eval
T3CODE_HOME=/home/gztxt/.t3-eval npx -y t3@0.1.7 --version

# 2) 启动（独立端口、独立数据目录）
T3CODE_HOME=/home/gztxt/.t3-eval setsid \
  /home/gztxt/.t3-eval/runtime/versions/0.1.7/t3 serve \
  --port 3799 --host 0.0.0.0 --lan --no-browser \
  --base-dir /home/gztxt/.t3-eval/data /path/to/project \
  >> /tmp/t3server.log 2>&1 &

# 3) 公开描述 / 会话策略
curl -sS --noproxy '*' http://127.0.0.1:3799/.well-known/t3/environment | python3 -m json.tool
curl -sS --noproxy '*' http://127.0.0.1:3799/api/auth/session | python3 -m json.tool

# 4) 取一次性配对码（TTL 默认 5m；--scope 可只给只读）
T3CODE_HOME=/home/gztxt/.t3-eval /home/gztxt/.t3-eval/runtime/versions/0.1.7/t3 \
  pair --base-dir /home/gztxt/.t3-eval/data --scope orchestration:read --ttl 5m

# 5) 用码换会话（第二次必 401）
curl -sS --noproxy '*' -c /tmp/cj -X POST http://127.0.0.1:3799/api/auth/browser-session \
  -H 'content-type: application/json' -d '{"credential":"<12位码>"}'

# 6) 浏览器端到端
chromium --headless=new --remote-debugging-port=9223 --user-data-dir=/tmp/chr about:blank
# 再打开 http://127.0.0.1:3799/pair#token=<新码>，等 15s 看是否出现 “New thread”

# 7) HTTP 只读互操作
T3CODE_HOME=/home/gztxt/.t3-eval /home/gztxt/.t3-eval/runtime/versions/0.1.7/t3 \
  auth session issue --base-dir /home/gztxt/.t3-eval/data --scope orchestration:read --ttl 10m
curl -sS --noproxy '*' -H "Authorization: Bearer <token>" http://127.0.0.1:3799/api/projects
```

清理：`kill $(pidof t3)`；删除 `/home/gztxt/.t3-eval`（373MB）与 `/vol1/eval/t3code`（550MB）即可，无系统级残留（未安装 service、未改 tailscale、未动 CCR）。

---

## 9. 未测清单（如实标注）

1. iOS / Android 真机连自建 server（无设备）；
2. Electron 桌面端连自建 server（无 GUI 会话）；
3. T3 Connect 全链路（需 Clerk 账号 + 自有域名 + Cloudflare 账号）；
4. `--tailscale-serve` 实配（会改 tailnet 共享状态，按军规未执行；本机已有 `:8443→3102` 映射）；
5. SSH 托管（仅桌面端提供）；
6. 成功完成一次 agent 对话（本机 CCR `/v1/models` 返回 0，网关降级；未改共享配置）。

---

*本报告只读评估，未修改 agenthub 任何代码、配置或服务端状态。*
