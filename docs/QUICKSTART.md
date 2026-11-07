# 快速上手（从零跑起来）

> 面向**新机器 / 外部用户**：本仓是**无头 Web 服务**（FastAPI + 原生 JS）。
> 本文只讲「怎么把它跑起来、怎么确认它真的在跑」；功能语义见 [`../README.md`](../README.md)，
> 设计决策见 [`../DESIGN.md`](../DESIGN.md)，版本历史见 [`../CHANGELOG.md`](../CHANGELOG.md)。
> **所有命令都已在本机实测**（2026-10-12，v0.13.102）。

## 0. 前提

| 项 | 要求 | 验证命令 |
|---|---|---|
| Python | **3.11**（本机 `3.11.2`；`ast`/类型标注按此写） | `python3 -V` |
| systemd user | 有余量（本机用它守护，也可裸跑） | `systemctl --user is-system-running` |
| 可选 CLI | 被管理的 agent 要**本机自己装好**（hub 不代装） | `which claude pi codex jcode hermes` |

**Hub 不含任何模型**：它管理的是「本机已有的 agent」，对话/终端能力依赖被管理方自己的凭据。

## 1. 三步跑起来

```bash
git clone https://github.com/gztxt/agenthub.git && cd agenthub
python3 -m venv venv && venv/bin/pip install -r requirements.txt   # ① 依赖（无构建工具链）
cp .env.example .env 2>/dev/null || true                           # ② 配置（见 §2；本机 .env 不入库）
venv/bin/python run_dualstack.py                                   # ③ 前台起服务
```

起来了怎么确认（另开一个终端）：

```bash
curl -s http://127.0.0.1:3102/health
# {"status":"ok","service":"agenthub","version":"0.13.102","port":3102,"db_ok":true, ...}
```

浏览器打开 `http://127.0.0.1:3102/` 即控制台。

> `run_dualstack.py` 的作用：uvicorn 的 `--host ::` 实测是 **IPv6-only**
> （asyncio 建 socket 时硬写 `IPV6_V6ONLY=1`），这个脚本自建 `V6ONLY=0` 的双栈 socket
> 再把 fd 交给 uvicorn ⇒ v4/v6 同一监听。**不要**把它换成 `--host 0.0.0.0`（会退化成 IPv4-only）。

## 2. 配置（`.env`）

`.env` 由 `src/config.py` 以**覆盖模式**加载（环境变量优先），且**不入 git**（含凭据）。
最小可用集合：

| 键 | 作用 | 必须 |
|---|---|---|
| `PORT` | 监听端口（默认 `3102`） | 建议 |
| `HOST` | 监听地址，**必须 `::`**（双栈铁律） | 建议 |
| `DATA_DIR` / `LOG_DIR` | 数据与日志目录（默认仓内 `./data`、`./logs`） | 建议 |
| `TERM_TOKEN` | **已废弃（2026-11-07 C 档）**：终端鉴权改走登录会话 Cookie / HTTP Basic，此项保留仅为兼容历史 .env | 勿用 |
| `HUB_PASSCODE` | **已废弃（2026-11-07 C 档）**：设置写端点改由登录闸门保护，此项保留仅为兼容历史 .env | 勿用 |
| `HOOK_AUTH_TOKEN` | 遥测写入口令；**空=仅回环可写** | 对外接入则必须 |
| `CLAUDE_CCR_URL` / `JCODE_URL` / `PI_WEB_URL` / `TDAI_URL` | 各下游端点（自定义部署时改） | 视环境 |
| `TERM_RECORD` | `1` 开终端录制（会写 `term_recordings` 表，见 §5 容量） | 可选 |

**凭据口径**（2026-11-07 C 档后，`src/writeauth.py` 登录闸门覆盖全部写端点）：
浏览器用登录会话 Cookie `hub_session`，机器侧用 HTTP Basic（同一份用户名 + 密码）；
`x-hub-token` / `x-term-token` / `?token=` 与 `TERM_TOKEN` / `HUB_PASSCODE` 全部废弃、不再接受。
**未登录 → 401；用户库不可用 → 503（fail-closed，不放行）**。

## 3. 用 systemd user 守护（可选，本机做法）

仓内 `deploy/agenthub.service` 是**与生产 unit 逐字镜像**的副本（`tests/test_hublog.py::test_deploy_copy_matches_real_unit`
把注释外的键值集合钉死）：

```bash
cp deploy/agenthub.service ~/.config/systemd/user/agenthub.service
# 按需改 WorkingDirectory / ExecStart 的绝对路径为本机 clone 位置
systemctl --user daemon-reload && systemctl --user enable --now agenthub.service
systemctl --user status agenthub.service
```

两个设计约束（改了会静默故障）：

- `RequiresMountsFor=`：ExecStart 落在挂载点上时，必须让它等挂载，否则开机 `203/EXEC`（约 13~14s 不可用）。
- `Restart=always`：**不要**手工 `kill` 主进程，用 `systemctl --user restart`。

## 4. 验收：怎么算「跑对了」

| 层 | 命令 | 期望 |
|---|---|---|
| L0 干净机器可跑 | `bash scripts/run_tests.sh hermetic` | `ran=1496 skipped=0 failures=0`（数字随版本变） |
| L1 本机形态 | `bash scripts/run_tests.sh all` | L0+L1 全绿，0 skip |
| 推前闸门 | `bash scripts/prepush.sh` | 九步全 PASS（**默认不推送**，加 `--push` 才推） |
| 端点自证 | `curl -s 127.0.0.1:3102/health` | `status=ok`、`db_ok=true` |

`/health` 里两个字段最有用：`code_stale`（**改了但没重启**）与 `git_sha_boot` vs `git_sha_now`
（**跑的是不是最新代码**）—— 只看进程活不活是会骗人的。

## 5. 容量与运维（本机实测，绝对值不宜外推）

- `data/agents.db` 是**单写者 SQLite**，本机已达 **118 万行**（`term_recordings`，录制所致）。
  写入路径已被三轮优化（v0.13.100/101/102）：SUM 收进程内缓存 → 单行计数表+触发器 → 关 WAL 自动检查点改后台。
  **不要**在这条路径上再挂同步动作（家族根因见 `CHANGELOG.md` 的 v0.13.100/101/102）。
- `TERM_RECORD=0` 可停止录制（省容量，代价是没有终端回放）。

## 6. 故障排查（按取证顺序，别跳步）

1. **页面起不来** → 先看 `/health` 的 `code_stale`；`true` 就 `systemctl --user restart agenthub.service`。
2. **终端连不上 / 反复要求登录** → 先在页面完成「登录」（会话 Cookie），机器侧带 HTTP Basic；C 档后终端只认登录态，没有 TERM_TOKEN 可配。
3. **某个 agent 卡在启动** → 见 `src/term.py` 的 PTY 初始尺寸注释（`0×0` 会让部分 TUI 永久卡死，v0.13.102 已修）。
4. **改完不生效** → 前端静态资源走 ETag（由 `mtime+size` 算出）⇒ **`?v=` 破不了端侧缓存**，要硬刷新。

## 7. 这个仓的外部依赖（都不是「装完即用」）

- 被管理的 agent CLI：本机各自装、各自鉴权。
- 网关：本机用 CCR（OpenAI 兼容口 :3456）供 claude/jcode 走对话；自定义部署需改 `CLAUDE_CCR_URL`。
- 记忆后端：TDAI（:8420）可选；不配也能跑（记忆中心降级）。
