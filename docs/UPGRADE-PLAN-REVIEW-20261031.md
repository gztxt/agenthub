# 《AgentHub 全面升级方案.docx》对照实测复核报告

- 复核对象：`/fs/1000/ftp/技术文档/AgentHub全面升级方案.docx`（26,681 字，465 段，13 表）
- 复核时点：2026-10-31 10:40 CST
- 被测系统：`agenthub` HEAD `52cbd5a`（v0.13.184），生产 unit `agenthub.service` active，MainPID 3400
- 复核方式：逐条把文档断言当"可证伪命题"，回代码/运行态/GitHub API 取证

> 一句话结论：**框架可用，事实陈旧，引用有假。** 文档三大分析章（UI / 操作 / 工程范式）的结构
> 值得留用，但其中的量化基线、模块清单和竞品引用不能直接当施工依据 —— 至少 11 处硬错、
> 2 个 404 的"竞品"、3 项"待做"实际已上线。

---

## 1. 实测取证表（文档断言 → 实测）

### 1.1 属实的部分

| 断言 | 实测证据 | 判 |
|---|---|---|
| Python 3.11+ | `venv/bin/python -V` → 3.11.2 | ✅ |
| FastAPI + SQLite + asyncio | `src/main.py`、`src/db.py`、全仓 asyncio | ✅ |
| systemd user 单元、无 Docker | `systemctl --user is-active agenthub` → active | ✅ |
| xterm.js 6.0.0（含 6 个 addon 版本） | `static/vendor/README.md:12-19` 逐文件 md5 登记 | ✅ 全对 |
| 无 npm 构建、`build_hubjs.sh` 纯拼接 | `scripts/build_hubjs.sh:1-22`（cat + node --check + 原子 mv） | ✅ |
| 唯一模板 + CSS token 体系 | `templates/index.html` 3162 行，`var(--…)` 591 处 | ✅ |
| 仅暗色主题 | 全模板 `prefers-color-scheme` 命中 0，`theme` 命中 1（非切换） | ✅ |
| 无 CSP / 无 API 版本控制 | `grep Content-Security-Policy src/*.py` → 仅 profiles.py 注释提到 X-Frame-Options | ✅ |
| 无告警通知 | `healthx.py` / `vitals.py` 无 notify/webhook/邮件 | ✅ |
| writeauth fail-closed | `src/writeauth.py`：34 条写路由集中一闸，6 类豁免逐条写理由 | ✅ |
| Stars 2 / Forks 0 | GitHub API `gztxt/agenthub` → 2 / 0 | ✅ |
| `src/` 约 100 个 .py | 实测 101 个（另 adapters/ 5 个） | ✅ |
| 端口 3456/3457/30141/8420 | `ss -ltnp` 全部 LISTEN | ✅ |
| 执行者不得自证 success | `src/tasks.py` 验收链 + judge 门面 | ✅ |

### 1.2 数字过时（同量纲，可算偏差）

| 指标 | 文档 | 实测 | 偏差 |
|---|---|---|---|
| L0 hermetic 用例 | 880 例 | **3439 例**（实跑，0 skip/0 fail） | 3.9× |
| Git commits | 635 | **646** | +11 |
| CHANGELOG | 9277 行 / 638KB | **9440 行 / 652KB** | +163 行 |
| agent-knowledge 篇数 | 155+ | **265** | 1.7× |
| JS 源码分片 | 12 | **13**（新增 `14-botboy.js`） | +1 |
| 模块清单 | 98 行 | 实际 101 个 .py | 缺 3 |
| 前端 bundle | "可控在 50KB 以内" | **568KB raw / 204KB gzip** | 11× |

### 1.3 硬错（事实层面，必须改）

| # | 文档说法 | 实际 | 证据 |
|---|---|---|---|
| 1 | `manager.py`（Manager Agent 自然语言指挥官），表格单列 B+ | **文件不存在**。对话入口是 `/api/agents/{id}/chat`；模型配置是 `config.manager_llm_*`；Bot 面板是 `botboy.py` | `ls src/manager.py` → no such file；OpenAPI 无 manager 路由 |
| 2 | 任务编排"验收三态 pending/running/completed" | **11 态**，且 `completed` 不是合法值 | `src/tasks.py:48-49` TASK_STATUSES |
| 3 | Agent 发现 = 扫配置目录 + 端口占用检测 | **画像驱动，非端口判定** | `src/discovery.py:1` docstring；`profiles.py` 三路（/proc + systemd + docker） |
| 4 | "定义了四档断点 320/390/768/1280" | CSS 实际断点只有 **1100 / 767 / 768**；320/390/1280 是**验证视口** | `DESIGN.md:309` 原文是"窄屏判据…scrollWidth==clientWidth"；`grep @media` 无 320/390/1280 |
| 5 | "HTTP 只用 urllib，缺连接池、超时控制" | **aiohttp 3.14.3 是直接依赖**，10 个模块在用 | `requirements.txt`；`src/discovery.py:12`、`llm.py`、`kb.py`、`tdai_client.py` |
| 6 | "SQLite 需要引入 WAL 模式" | **已启用** WAL + 启动 checkpoint + 定期 checkpoint | `src/db.py:212`、`:231`、`:313` |
| 7 | "9 个 Skill 模块文档几乎为零（C 级）" | 每模块 docstring **689~2095 字符** | `ast.get_docstring` 实测 9 个文件 |
| 8 | "MCP 客户端"列为 Phase 2 P0 待做（2-3 周） | **已实现**：ClientSession + stdio + streamable_http | `src/mcpgw.py:23-34`、`:186-199` |
| 9 | "Skill 共享库"列为 P1 待做 | **已上线**：商店 + 自动安装 + 技能中心两视图 | `src/skill_store.py:223`、`:873`；模板 v0.13.127 注释 |
| 10 | "无移动端优化" | **已有触摸层**：惯性滚动 / 长按选区 / 双指缩放 | `static/hub/13-term-touch.js:1-15` |
| 11 | "12 个 JS 分片通过全局状态通信" | 运行时只加载 `/static/hub.js` 单文件，分片仅构建期存在 | `templates/index.html` script 标签；`build_hubjs.sh` |

### 1.4 捏造引用（最严重）

| 文档条目 | 实测 |
|---|---|
| 表 1.1 竞品 "Karpathy AgentHub"，附录 B `github.com/karpathy/agenthub` | **HTTP 404 / Not Found**（GitHub API） |
| 表 1.1 竞品 "Hermes Web UI"，附录 B `github.com/NousResearch/hermes-web-ui` | **HTTP 404 / Not Found** |

> 文档自己在 6.3 写了"外部评估报告不可信 → 所有引用必须 grep 验证"，但 1.1 的竞品对比表
> 恰好没有过这道闸。**这是文档最该修的一处：竞品结论全部建立在两个不存在的仓库上。**

---

## 2. 升级方案的可执行性评估

### 2.1 可以直接做（低成本、与现有架构不冲突）

| 项 | 理由 | 落地注意 |
|---|---|---|
| 全局命令面板 / 统一快捷键 | 现状只有零散 `keydown`（01/03/04/06 各一处），无统一体系 | 禁 inline `onclick`，走 `data-*` 委托（AGENTS.md 4.2） |
| 统一错误提示 + loading skeleton | 属实缺失 | 复用现有 `:root` 语义色，不新增色 token |
| CSP / 安全响应头 | 实测确实没有 | 先 `Content-Security-Policy-Report-Only`，避免打断 xterm WebGL |
| 结构化日志（自写 JSON formatter） | 73 处 `print` vs 仅 7 个模块 `import logging` | **不要 structlog**（见 2.2），用标准库 `logging` + JSON formatter |
| 测试数据工厂 | 现有 `run_tier` 已防 skip 假绿，但夹具仍散落 | 复用 `tests/tiers.py` 口径，不新建第二套 runner |

### 2.2 必须否决或改写（与既有军规/实测结论冲突）

| 文档建议 | 冲突点 | 建议 |
|---|---|---|
| 引入 Qdrant/Chroma 替代 SQLite 向量 | 2026-10-17 实测：纯向量 nDCG@5 **0.4531** < 纯词法 **0.6624**，`axvec` 已删除 | **否决**。要提升检索只能扩评测集/调模板，不是换库 |
| 引入 httpx / tenacity / structlog / pyyaml | AGENTS.md 零新增依赖硬约束，明令禁用三者 | **否决**；需要的能力用标准库自写（已有先例：urllib + 自实现重试） |
| Cloudflare Tunnel 发布 | 本机优先 + 已有 Tailscale 链路（IPv6 直连 121ms） | **否决新增公网入口**；远程访问走现有 Tailscale |
| 文件浏览器（本地+远程读写） | 任意路径读写直接绕过 writeauth 边界 | **降级**：先做只读 + 路径白名单，写入必须二次确认 |
| Vue3/React/Vite 迁移 | 与"零构建"哲学冲突；单人维护 | 文档自己的结论（不立即迁移）是对的，保留 |
| DAG 引擎"基于 MCP SDK 重写" | MCP SDK 已是依赖，但 DAG 与 MCP 是两件事，重写动机不成立 | 改为"补 DAG 可视化 + 补偿重试"，不重写引擎 |

### 2.3 已完成、应从"待做清单"移除

- MCP 客户端（`mcpgw.py`，mcp 2.1.1）
- Skill 共享库/商店（`skill_store.py`）
- 移动端触摸层（`13-term-touch.js`）
- WAL 与 checkpoint（`db.py:212`）
- Agent 自动注册（`profiles.all_profiles()` 合并点，v0.13.155）

### 2.4 工时不现实的地方

- **CI/CD（GitHub Actions）**：L0 本地 103 秒可跑完，单机单开发者，收益低于维护成本。
  若要闸门，用已有 `scripts/prepush.sh`（含 ⑦ 自检指纹），不引 GitHub runner。
- **pip-audit**：会引入新依赖。替代：`pip list --outdated` + 手工比对 `requirements.txt` 锁定值。
- **600-820 人时总盘**：按文档口径（Phase 0 40-60h）尚可，但 Phase 2/3 里有 3 项已完成，
  实际剩余量至少砍掉 1/3。

---

## 3. 测试验证总结（本次实跑）

| # | 验证项 | 命令/口径 | 结果 |
|---|---|---|---|
| 1 | L0 hermetic 全量 | `bash scripts/run_tests.sh hermetic` | **3439 例，OK，skipped=0，failures=0，errors=0**，102.8s |
| 2 | 收集器对账 | 同上内置 | unittest **3519** = pytest **3519** ✅ |
| 3 | 生产健康 | `curl /health` | v0.13.184、`db_ok=true`、`term_sessions=1`、`NRestarts=4`、uptime 13258s |
| 4 | 自证层字段 | 同上 | `git_sha_boot=52cbd5a6`、`code_fp_boot==code_fp_worktree`、`needs_restart=false` |
| 5 | 端点规模 | `GET /openapi.json` | **161 paths / 181 operations** |
| 6 | 回环延迟（/health） | n=30 顺序采样 | median **3.2ms**、p95 **4.2ms**、spread 3.0ms |
| 7 | 回环延迟（/api/agents） | n=30 顺序采样 | median **26.3ms**、p95 **27.4ms**、spread 64.1ms（含一次 89ms 尖峰） |
| 8 | 端口面 | `ss -ltnp` | 3102 / 3456 / 3457 / 30141 / 8420 全部 LISTEN |
| 9 | Git 同步 | `git rev-list --count origin/master..HEAD` | 领先远端 **11** 提交 |
| 10 | GitHub 元数据 | GitHub API | 2★ / 0 fork；两个"竞品"仓库 404 |

**口径声明（必须随数字一起引用）**
- 延迟是**回环单机**口径，不是生产 P95，不能拿去和 200ms 目标比"达标/不达标"；
  `/api/agents` 的 spread 64ms 已大于两档读数间差异，**只报量级不报倍数**。
- 未测覆盖率（`coverage.py` 未接入），文档"代码覆盖率未测量"这条属实。
- L0 全绿**不代表生产正确**：本次只跑 L0，未跑 L1 host / e2e 探针。

---

## 4. 对文档的处置建议

1. **保留**：第二/三/四章的"问题分类框架"（模块评级、UI 问题清单、操作效率瓶颈表）—— 作为检查清单模板。
2. **重采基线**：1.5、2.1、5.2 的所有数字（用例数、行数、模块数、分片数、bundle 体积）
   用脚本重生成一次，不要手工写。
3. **删**：1.1 竞品表 + 附录 B 两条 404 链接；或换成真实可核的对标（如 `modelcontextprotocol`、
   `n8n`，这两条链接是真的）。
4. **改**：`manager.py`、三态、端口发现、四档断点、urllib、WAL、MCP/Skill/移动端已完成项。
5. **压**：二期清单 10 项 → 7 项（删已完成 3 项），Phase 2/3 工时按 2/3 折算。

---

## 5. 两分钟内可复核的一条命令

```bash
cd /fs/1000/ftp/技术文档/agenthub && bash scripts/run_tests.sh hermetic
```

期望：`Ran 3439 tests … OK` + `[tier] ✅ 收集器对账：unittest=3519 = pytest=3519`。
与文档"880 例"的差异即本报告 1.2 第 1 行的来源。
