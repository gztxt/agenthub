# Agent Hub（智管 Web）

本机多 Agent 统一管理中心（无头 Web 版），**语义移植自 [Zafer-Liu/Agent_Manager](https://github.com/Zafer-Liu/Agent_Manager) v0.3.0（Apache-2.0）**——
移植数据模型/协议口径/交互语义，未复制上游源文件；桌面形态（进程起停/Tauri 命令/
Qdrant sidecar）按本机 NAS 军规有意裁剪。评估全文见
[`docs/Agent_Manager_融合评估报告.md`](docs/Agent_Manager_融合评估报告.md)（2026-09-06 融合裁定）。

**第一次用请先读 [`docs/QUICKSTART.md`](docs/QUICKSTART.md)** —— 从零跑起来、配置、验收、故障排查。

> **版本历史见 [`CHANGELOG.md`](CHANGELOG.md)** —— 本 README 只讲「现在是什么」，不讲「哪一版改了什么」。
> 逐版本施工流水见 [`docs/HISTORY-features.md`](docs/HISTORY-features.md)。

## 环境要求

- **Python 3.11+**（生产 venv 实测 `3.11.x`；用到 `X | None` 形式注解与 `asyncio.to_thread`）
- 依赖锁见 [`requirements.txt`](requirements.txt) —— 版本号取自生产 venv `pip freeze` 实测值，
  **不是**随手写的（无 `pyproject.toml`/`uv.lock`，重建 venv 就靠它，锁错无人察觉）
- 零新增第三方依赖是硬约束：不用 httpx / tenacity / pyyaml，HTTP 只用 `urllib`
- 部署形态：systemd user 单元（无 Docker）——pty + 容器化会消解本机的进程隔离假设

## 现在是什么

四类对象一张卡片墙（讲台 + 座位视图），判定**不靠端口**：

- **Agent**（`agent`）：pi / claude / codex / jcode / grok / opencode / cloudcli / qwenpaw …
  四种入口 = 原生会话(embed) / 终端(term) / 对话(chat) / ↗UI。CLI 型走 hub 自建 pty
  （`src/term.py`：WebSocket + 本地化 xterm.js，argv 不经客户端、无 shell，命令取自画像）
- **网关**（`gateway`）：CCR 唯一在役（FCC 已退役，列此仅为防误判）
- **服务 / 记忆 / 工具**：只给快捷方式，无对话按钮

四层能力：**画像驱动检测**（`src/profiles.py`，/proc + systemd + docker 三路判定）、
**三层记忆中心**（L1 本地 / L2 FTS+向量+RRF / L3 压缩重建）、**任务编排**
（声明式 DAG + 验收三态，执行者不得自证success）、**自证层**
（`/health` 回 `git_sha_boot` / `code_stale`，能区分「改了没重启」与「跑的是最新」）。

接口清单见 `src/` 各模块 docstring；逐版本做了什么见 `CHANGELOG.md`。

## 知识索引

本仓的**施工判据与事故取证**沉淀在本机工作区 `技术文档/agent-knowledge/`（155+ 篇，本仓内无副本，
路径对本仓外部读者不可达——这是本仓的已知文档缺口，见 `docs/HISTORY-features.md` 顶部说明）。
下面按主题列出与本仓直接相关、且**结论可复用**的篇目（编号即文件名前缀）：

| 主题 | 篇目 |
|---|---|
| **架构与模块边界** | 157 静态服务深化（补成深模块）｜158 组装与领域路由分离（路由表不变量 + 惰性 include 的坑）｜169 纳管边界四维（只读须用负向测试兑换）|
| **测试与闸门** | 92 两类测试假绿与派生检出判据失效｜101 e2e 试点的三个假绿与「API 先读」｜151 L0 依赖未跟踪文件该归 host_only｜152 ground truth 落进被测可读树 ⇒ 正对照恒真｜179 `setdefault` 撞 `override=True`：空操作伪装成夹具｜180 我报的隐患不存在（重复踩别人已修的坑）|
| **终端与 pty** | 75 惯性尾巴被逐帧取整吞掉｜83 鼠标跟踪卡死与看门狗｜85/90 备用屏跨会话污染｜91 选不中/复制不动（鼠标回装清选区）｜96 OSC11 应答被pty 回显 |
| **前端与界面判据** | 39 窄屏抽屉白遮挡与**分档偏好不变量**｜40 窄屏浮层残留与**浮层唯一性三条**｜100 flex 间距两个隐蔽来源｜153 换树未证明停稳 ⇒ 静态资源全 404 而 `/` 仍 200｜155 项目页 agent 下拉空框（同步渲染撞异步数据源）|
| **模型路由与网关** | 45 同 key 双网关冗余判定与 FCC 退役｜48 CCR 模型清单可观测化｜61 各 agent 模型统一设置口径｜69 模型设置持久化的三条调用通道与配置漂移｜161 pi provider 重名分叉｜177 HiRoute 融合报告的六处捏造（**外部评估不可轻信**的样本）|
| **记忆与检索** | 58 联邦化六批改造收口｜163 AMR 评估：格式契约 ≠ 检索系统｜172 D18 经验库接入记忆联邦 |
| **任务与闭环** | 167 P0-2 成本度量（静默 0 即谎报）｜168 P0-4 证据分层（把「判不了」做成一等公民）｜170 P0-3 任务状态机｜171 P0-1 轨迹→数据集→经验库闭环｜174 经验抽取自动化与首次真实评估 |
| **部署与运行** | 41 自证层改内容指纹与四类静默失效｜72跨文件系统迁移（worktree 指针要改两处）｜94 单元改名：漏改不报错只静默变空｜160 停稳闸只认 inactive 而 SIGTERM 143 落 failed｜173 xray 无单元无自启致16h 空窗 |
| **多会话与并发** | 44 多会话编排审计与**单写者协议**｜120 并发写入取证与一次假红的自我拆穿｜72 迁移期的worktree 指针问题 |
| **安全与鉴权** | 37 写端点统一闸门与取证副作用纪律｜53 凭据闸门要「先取值再横扫」｜99 全仓密钥历史清理（改历史 ≠ 密钥安全）｜107 `.env` 覆盖致隔离失效的修法 |

**读法建议**：先看本README 的「安全边界」与 `docs/QUICKSTART.md`，再按上面的主题挑。
这些篇目的共同点是**每条都绑定取证日期与版本号**，改判时加横幅不删原文 —— 当结论与本机现状冲突时，
**以代码与实测为准**，不要照抄旧条目。

### 外部项目融合评估（结论摘要）

本仓的定位与边界是**逐份外部评估取证后裁定的**，不是拍脑袋。原文在 `docs/`：

| 上游 | 裁定 | 原文 |
|---|---|---|
| Zafer-Liu/Agent_Manager v0.3.0（Apache-2.0） | **架构不融合**，只移植语义（数据模型/协议口径/交互），未复制源文件；桌面形态按 NAS 军规裁剪 | [`docs/Agent_Manager_融合评估报告.md`](docs/Agent_Manager_融合评估报告.md) |
| nathangtg/agent-hub @5bdc972（MIT） | **赛道不同**（上游＝让 LLM 调工具的企业编排平台／本机＝管本机 CLI 的控制台），重叠 <15%。只取 2 个零依赖横切设施：熔断器 + 限流退避 ⇒ 落`src/resilience.py` | `技术文档/nathangtg-agent-hub_融合评估报告.md`（本机工作区） |
| aliyun/ai-agent-handbook @e97f890d（Apache-2.0） | **文档站、代码层为零**（0 个 `.py`）⇒ 不能按组件融合，只能「方法论对标 + 知识通道」 | [`docs/AI-AGENT-HANDBOOK-融合评估-20261014.md`](docs/AI-AGENT-HANDBOOK-融合评估-20261014.md) |
| AgentMemoryRepo/agentmemoryrepo（MIT） | **能融合但是格式层**，不是替换：它刻意不做检索层（SPEC 仅 1940 字节）⇒ 与本机 FTS+向量+RRF 不同层 | [`docs/AGENT-MEMORY-REPO-评估-20261013.md`](docs/AGENT-MEMORY-REPO-评估-20261013.md) |
| higress-group/HiRoute | **报告结论不可采信、不实施**：该报告对 hub 架构大面积凭空捏造，且 HiRoute 是替换网关的本地网关（接法在 CCR 层，与本仓无关） | `agent-knowledge/177`（本机工作区） |

> **一条元教训**（见 `agent-knowledge/177`）：LLM 生成的「本项目评估报告」可能整篇虚构本仓的类名与文件路径。
> 采纳任何外部评估前，先grep 验证它提到的每个符号**确实存在**。

## 访问

- 本地: http://127.0.0.1:3102/ ｜ 局域网: http://192.168.5.102:3102/ ｜ Tailscale: http://100.117.232.62:3102/

## API

```
GET  /health                          POST /api/agents/{id}/chat    {message,session_id?}
GET  /api/agents/{id}                 POST /api/agents/{id}/chat/stream (SSE)
POST /api/agents/detect  {dir}        GET  /api/sessions | /api/sessions/{id}/messages
POST /api/agents         {dir,name?}  GET  /api/ports
DELETE /api/agents/{id}               GET  /api/memory/l1|search|l2|l3|context|l2/rebuild
POST /telemetry/events/{source}       GET  /telemetry/events | /telemetry/usage/summary

GET  /api/term/history/{agent_id}?limit=3   盘上历史会话（标题＝用户问题原文，需 x-term-token）
POST /api/term/sessions  {agent_id, session_id?}   带 session_id 即续聊该条历史
```

## 配置（.env）

- `CCR_OPENAI_KEY` 留空时自动读 `~/.config/jcode/provider-ccr.env`（3456 OpenAI 口实测唯一有效链）
- `MANAGER_LLM_BASE_URL=http://127.0.0.1:8082`（FCC，Anthropic /v1/messages）+ `MANAGER_LLM_API_KEY` + `MANAGER_LLM_MODEL`
- `HOOK_AUTH_TOKEN` 留空=仅回环可写；对外接入必须设置

## 控制

```bash
bash agenthub/agent-hubctl.sh {start|stop|restart|status|log|token}   # 仓内脚本，自解析所在目录
bash agenthub/agent-hubctl.sh token                # 打印当前生效的终端鉴权 token（只读 + 实弹验证）
```

机制：起停一律经 `systemctl --user agenthub.service`（`Restart=always` 归 systemd 管），
**不再生成/采信 pidfile**；`status` 与 `systemctl --user is-active` 同口径。
`~/.config/systemd/user/agenthub.service` 由 `deploy/agenthub.service` 安装而来。

## 二期候选（上游已有、本机未移植）

工作流 DAG 引擎（建议基于官方 `mcp` Python SDK 重写）、MCP 客户端、
Cloudflare Tunnel 发布、Skill 共享库、L1 自动提取（Hook 采集→LLM 提炼）。

## 安全边界（与上游的差异，均为有意设计）

1. **kill 的边界划在「谁的东西」上，不是「一律不做」**：
   - 端口枚举（`/api/ports`）**只读无 kill**——NAS 上的服务归 systemd/守护方管理，
     越权杀进程违反本机军规（`src/ports.py` 头注释即此意）。
   - 但**本机 Agent 自己的进程可一键结束**（`/api/resources/kill`，v0.13.52 起）：
     资源监控页列出运行中 Agent 的 CPU/内存，支持 SIGTERM/SIGKILL。四道护栏：
     ① 只对画像归类为 Agent 的进程开放；② 杀前校 `/proc/<pid>/cmdline` 归属，
     不属于该 Agent 直接 404；③ 目标进程**已不存在视为成功**（可目标状态已达成，
     不是失败）；④ 前端二次确认 + 写端点鉴权（`writeauth`，fail-closed）。
   - 一句话：**杀自己的 Agent 可以，杀别人的服务不行。**
2. telemetry/hook 带鉴权选项——上游零鉴权的补课
3. 凭据仅在 .env(0600) 与 jcode env 文件，不引入机器名派生加密反模式
