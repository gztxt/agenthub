# CONTRACTS — 可执行操作契约（v0.13.192 / 2026-11-07）

> 本文件只收录**仍然有效的契约**，每条标版本与取证日期。本机 155+ 篇施工判据不搬迁，
> 只在 `技术文档/agent-knowledge/` 留原文；引用前回原文核到 file:line。
> 与代码目录互校：凡本文点名的文件路径不存在，CI 失败（`tests/test_contracts_paths.py`）。

## 1. 任务状态机

`pending → running → verifying → submitted → success`；旁路 `failed → replan → running`、`blocked`、`cancelled`。

- `running` 只能由 runner 写（`src/tasks_runner.py`）。
- `submitted → success` 只有接受者（非执行者）可写：`src/tasks.py`（P1-4，见文件内 §P1-4 注释）。
  执行 Agent 的工具输出里出现 "done/success" 字样**不改变**状态。
- 终端态：`success / failed / blocked / cancelled`（`src/tasks_tiers.py`）。
- 失败必须带证据（日志尾/命令/判定理由）；无证据的失败显示「中断」而非「失败」。

## 2. 记忆层（M1/M2/M3，2026-11-07 起唯一口径）

| 层 | 名字 | 内容 | 写入者 | 现有模块 |
|---|---|---|---|---|
| M1 | 最近 | 会话摘录、终端摘要、任务轨迹（默认 30 天） | hub 自动 | `memory.py` / `sessions_store` / `term_record` |
| M2 | 库 | FTS + 向量 + RRF，可按 Agent/项目/任务过滤 | 归档任务或人工钉住 | `memindex` / `memfed` / `kb.py` |
| M3 | 画像 | 稳定偏好、环境事实、禁止项 | 仅人工或带引用的晋升 | `memory_archive` 晋升结果 |

- 检索结果必须带来源层标签（最近/库/画像）。
- 无嵌入服务时 M2 退化为 FTS，界面标明「未启用语义」，不得声称 RRF 已融合。
- 旧口径「L1 本地 / L2 FTS+向量+RRF / L3 压缩重建」与 DESIGN 旧 L1/L2/L3 已作废（加横幅不删）。

## 3. 鉴权档（两档，无完整 RBAC）

| 档 | 凭据 | 能力 |
|---|---|---|
| 本机操作者 | 登录会话 Cookie `hub_session` / 机器 HTTP Basic（用户名+密码） | 按用户 perms 授权 |
| 观察者 | 无凭据，且来源为回环（127.0.0.1 / ::1 / v4-mapped） | 读；非回环一律 401（v0.13.192 起） |

- 读门：`writeauth.read_gate`（v0.13.192）——回环匿名保持一个版本；`HUB_READ_TOKENS=0` 整门回滚。
- 写门：`writeauth.write_gate`（fail-closed；未配凭据 = 503 不放行）。
- 双栈 `HOST=::` 下 v4 回环连接的对端实测是 `::ffff:127.0.0.1`，回环判定必须含 v4-mapped 形式（v0.13.192 修复）。

## 4. 出站 HTTP

- 唯一入口目标：`outbound` 客户端（超时/重试上限/体大小/脱敏日志）。
- 现状（2026-11-07）：18 个模块各自 `urlopen`，收敛在 P3 批次，顺序 `hubmcp → vitals → gwprobe`。
- 新写接口必须声明：读或写、所需档位、是否进审计；缺声明在启动时拒绝注册（测试闸门，P4 落地）。

## 5. 重启护栏

- `term_sessions > 0` 时禁 `systemctl restart agenthub`；唯一入口 `scripts/hub-restart.sh`。
- 强杀在跑会话必须显式 `HUB_RESTART_FORCE=1`。

## 6. 版本与文档防漂

- `VERSION` 唯一在 `src/main.py`（v0.13.192）；文档引用版本号必须带取证日期。
- 本文点名的文件若失效，`tests/test_contracts_paths.py` 必须转红（只校文件名不校行号，行号漂移由 L0 测试钉）。
