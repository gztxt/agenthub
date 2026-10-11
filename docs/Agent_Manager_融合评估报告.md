# Agent_Manager 融合评估报告

> 日期：2026-09-06 ｜ 评估对象：https://github.com/Zafer-Liu/Agent_Manager @469504c (v0.3.0, 2026-08-21)
> 目标服务：本机 Agent Hub `~/agent-hub` (FastAPI, :3102)
> 源码克隆：`/fs/1000/ftp/技术文档/Agent_Manager`（浅克隆，39MB，Apache-2.0）
> 分析方法：3 个子代理并行深读 24K 行 Rust + 抽查 15K 行 React 前端，影子实例(:3199) 端到端实测

---

## 一、结论（TL;DR）

**定性**：Agent_Manager 是本机 Agent Hub(:3102) 的**设计原型**（3102 上线时即声明"复用智管设计理念"），
但它是 **Tauri 2 桌面应用**（Rust 后端 + React 前端），与本机 **无头 NAS + 浏览器访问** 形态不兼容。

**融合裁定：架构不融合、代码不搬运、语义移植。**
- ❌ 不可直接复用代码：核心逻辑与 Tauri（`#[tauri::command]`、webview reparent、`app.emit`、
  sidecar 进程编排 qdrant/neo4j）深度耦合，Rust→Python 无法二进制级复用；Apache-2.0 允许移植但需按文件重写。
- ✅ 可移植的是**语义资产**：数据模型（SQLite schema）、协议口径（telemetry 覆盖式 upsert）、
  三层记忆模型、Manager 工具环、项目识别规则表——本次已全部用 Python 复刻并实测。

## 二、项目画像

| 维度 | 事实 |
|---|---|
| 定位 | 本机多 AI Agent + MCP 服务器的桌面统一管理中心 |
| 规模 | Rust 24,128 行（24 模块）+ React 14,888 行（前端 20+ 页面） |
| 热度 | 166 stars / 31 forks / 34 commits，单人项目（Zafer-Liu） |
| 许可 | Apache-2.0（商用/修改/再分发均友好，需保留声明） |
| 版本 | v0.3.0（README 声称 2026-07-12，末次 commit 2026-08-21） |
| 功能矩阵 | 教室视图 Dashboard / Manager Agent 指挥官 / Agent 进程管理 / MCP 客户端(3 传输) / 工作流 DAG / Cloudflare Tunnel + Caddy 发布 / 端口管理 / 三层记忆中心 / Token 用量 / Skill 共享库 |

## 三、代码质量评估

**优点**
1. 数据口径严谨：telemetry「同 (source,session_id) 重报**覆盖**而非叠加」、cached_tokens 是 input 子集
   不重复计总、`reported` 不被 `estimated` 覆盖（带 origin 守卫）——是踩过坑的账单级设计。
2. 记忆链路防丢失设计：Hook 事件先落盘 L0 账本再异步提取（离线不丢采集），L1 幂等靠
   `UNIQUE(source_event_key,ordinal)` + 内容寻址 sha256，清洗前回滚快照 JSON。
3. 注入降级完备：semantic 检索失败自动退 keyword `local_fallback`；转录扫描器带版本号，
   规则变更即全量重扫。

**硬伤（评估中发现的"文档有、代码无"）**
1. ⚠️ `scheduler.rs::start_scheduler()` **无任何调用点** —— v0.3.0 宣称的"定时触发 cron"实际未上线。
2. ⚠️ `workflow_events::init_event_writer()` 无调用点 —— "Metrics 事件源"实际不落盘（emit 静默空写）。
3. ⚠️ sweeper 仅实现场景 1（步骤超时），宣称的 agent 退出→Blocked、Run 卡死检测未实现（dead_code）。
4. Manager Agent 的 `__action__:` 哨兵是最大架构债：动作不真执行、LLM 恒收 "dispatched successfully"
   （假成功），靠前端解析魔法串落地。
5. LLM key 加密用 `SHA256("agent-manager-key-v1"+机器名)` 派生 AES 密钥 —— 换机/容器即不可解密。
6. `/telemetry/events` 与 `/memory/hook` 零鉴权（仅靠 loopback 绑定 + 1MiB 上限）。

**总评**：产品设计与工程口径一流（尤其数据语义），工程完成度约 85%（三处宣称功能未接线），
桌面形态与本机 NAS 无头场景错位。

## 四、与 3102 Agent Hub 的关系

| 维度 | Agent_Manager（桌面） | Agent Hub :3102（本机） |
|---|---|---|
| 形态 | Tauri 桌面 App | 无头 FastAPI + 浏览器 |
| Agent 管理 | 起停进程（Std::Child） | **只观测不调度**（服务归 systemd/守护方，杀进程违反 NAS 军规） |
| 记忆 | L0 账本+L1-L3+Qdrant/Neo4j sidecar | SQLite 轻量三层（无向量库，keyword+可扩 TDAI） |
| 指挥官 | 7 工具+前端哨兵执行 | 9 工具服务端真执行 |
| 会话持久化 | 刷新即丢（仅内存） | SQLite 落盘（净超越上游） |
| UI | React+i18n+DAG 画布 | 单文件 HTML 教室视图（本次重写） |

## 五、融合决策表

| 上游能力 | 决策 | 落点（v0.2.0 已实施） |
|---|---|---|
| 教室视图交互语义（讲台/座位/悬停/状态动画/哈希头像） | ✅ 复刻 | templates/index.html 重写 |
| `/telemetry/events/{source}` 覆盖式账本 + 用量聚合口径 | ✅ 移植 | src/hook.py（+Bearer 鉴权补上游缺） |
| L1/L2/L3 三层记忆 + manual 隔离 + 注入 context 端点 | ✅ 移植 | src/memory.py + db.py |
| Manager Agent 工具环 | ✅ 移植+修复 | src/manager.py+llm.py（去 `__action__` 哨兵，服务端真执行；9 工具；会话落盘） |
| 项目类型自动识别（scan_project_dir 规则+端口探测） | ✅ 移植 | src/sources.py（tomllib/真则化） |
| 端口管理枚举 | ✅ 移植+收敛 | src/ports.py（/proc 实现，**只读不 kill**——NAS 安全边界） |
| 统一对话（CCR OpenAI 口直连） | ✅ 完成 Phase 2 遗留 | src/adapters/openai_compat.py + registry.py |
| Agent 进程起停/auto-restart | ❌ 砍 | 无头 NAS 上服务归 systemd，越权起停违反「不删改在用工具」约束 |
| MCP 客户端 3 传输 / 工作流 DAG（~7K 行） | ⏸ 二期 | 价值高工作量大；Python 侧应基于官方 `mcp` SDK 重写而非直译 |
| Qdrant/Neo4j sidecar 语义检索 | ⏸ 不搬 | 绑死桌面 bundle 布局；本机用 TDAI(:8420) 或后续接嵌入模型 |
| Cloudflare Tunnel / Caddy 发布 | ⏸ 二期 | 本机已有 FCC relay/fcitx 等入口，重复；Tunnel 可单文件移植 |
| Skill 共享库 / GitHub 安装器 | ⏸ 二期 | 直译成本低（★级），按需再做 |
| 机器名派生密钥加密 | ❌ 不学 | 反模式；本机凭据走 .env 0600 |

## 六、本次融合实测证据（2026-09-06，影子实例 :3199）

1. Manager 工具环：`thought→list_agents→list_ports→answer`，端口/状态回答全部来自真实探测，非臆测 ✅
2. 统一对话：jcode/claude → CCR:3456 真回包（"收到"/"明白"）含 usage ✅；会话 SQLite 落盘可回放 ✅
3. 遥测：push→覆盖式 upsert（100→120 非 220）→summary 聚合 ✅
4. 记忆：L1 批量灌入→LLM 压缩 L2（分组 Markdown、保留精确端口值）→manual 隔离 ✅
5. 注册：proxy-panel 目录自动识别 python/server.py/端口 8083 →列表 running→删除还原 ✅
6. 端口：125 监听枚举 + pid/进程名归属 + Agent 徽章标注 ✅

## 七、对上游仓库的反哺建议（可提 Issue）

1. `start_scheduler`/`init_event_writer` 未接线（README 已宣称功能）
2. `__action__` 假成功协议 → 建议服务端真执行 + SSE 结构化动作事件
3. telemetry/hook 端点补 Bearer 鉴权（loopback 依赖在共享开发机上是漏洞面）
4. 机器名派生密钥 → 改 Fernet/OS keyring

---
*本报告与 src/*.py 移植代码同批产出；移植仅参考语义与算法，未复制上游源文件（Apache-2.0 合规，README 已注明来源）。*
