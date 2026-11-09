# HUB 鉴权盘点与 scope 清单（2026-11-09）

> 目的：为「P0 权限 scope 化」提供基线。只读盘点，未改任何代码、未重启服务。
> 复现：`python3 /vol1/eval/hubauthz/scan.py`（源码正则解析）+ `curl /openapi.json`（活体对账）。

## 0. 取证口径

| 项 | 值 | 方式 |
|---|---|---|
| 源码路由 | **212** 条（41 个文件） | 扫 `src/*.py` 的 `@app/@router` 装饰器 |
| 活体 OpenAPI | **211** operations | `GET :3102/openapi.json`（200，168,798B） |
| 差额 | 1 条 | 唯一 WebSocket `/ws`，OpenAPI 不列 |
| 写 / 读 | **97 / 115** | 写 = POST/PUT/PATCH/DELETE |
| 匿名写探测 | `POST /api/prefs/__inv_probe` → **401** | 活体，2026-11-09 |
| 匿名读探测 | `GET /health` → **200** | 设计内豁免 |
| 非管理员用户 | **0** | `SELECT count(*) FROM users WHERE is_admin=0`（只读） |

## 1. 五条结论

1. **写面是「登录即全权」，不是「按模块授权」**：`writeauth.decide()` 只判 `identity`，命中就 `allow`（`src/writeauth.py:66-75`）。97 条写端点拆开是：**88 条登录即全权**（无任何 admin/模块校验）+ **7 条 admin 校验**（全在 `src/users.py`）+ **2 条豁免**（登录/登出）。
2. **现有 `user_perms` 管的是菜单可见性，不参与写判定**：定义即「按菜单节点 id 授权」（`src/users.py:10`），读取只用于过滤导航（`src/users.py:96-97`）。所以「给某人只开记忆模块」目前只影响他看不看得见入口，不影响他能不能直接 POST。
3. **3 条豁免是有理由的，不是漏网**：`/telemetry/events/`、`/api/users/login`、`/api/users/logout`（`src/writeauth.py:36-47`），每条都自带鉴权或必须未登录可达。
4. **97 条写里 22 条有外部副作用**，最危险的 5 条按后果排序：`POST /api/system/restart`（杀活会话）、`POST /api/resources/kill`（杀进程）、`POST /mcp/call`（任意工具调用）、`POST /api/settings/model/apply`（改全局模型路由）、`POST /api/settings/github/apply`（改 GitHub 凭据）。
5. **当前风险不是「已有人越权」，而是「未来设备令牌一出生就是管理员」**：本机只有 1 个 admin 用户、0 个非管理员。一旦按 t3 模式发只读配对令牌，若不先做 scope，拿到令牌的手机等于拿到全部 97 条写权限。

## 2. 建议的 9 个 scope

| scope | 写端点 | 外部副作用 | 边界 |
|---|---|---|---|
| `hub.task.write` | 11 | 5 | 任务创建/派发/暂停/取消/重排；**能起 agent** |
| `hub.agent.write` | 12 | 5 | agent 注册/删除/校验/对话、pty、UI 拉起、会话改名 |
| `hub.memory.write` | 8 | 0 | L1/L2/L3 与归档；纯本地数据 |
| `hub.skill.write` | 10 | 1 | 技能调用/安装/卸载/商店登录 |
| `hub.catalog.write` | 15 | 0 | 参考库、卡片、KB 探针、准入、动作面 |
| `hub.mcp.write` | 6 | 1 | MCP server/ACL 管理 + 任意 call |
| `hub.settings.write` | 12 | 3 | 模型/GitHub/偏好/设置导入 |
| `hub.ops.write` | 14 | 7 | 重启、杀进程、扫描、巡检、定时任务、审批 |
| `hub.user.admin` | 9 | 0 | 用户与凭据管理；其中 7 条已有 admin 校验，2 条是登录/登出豁免 |

**读面**：115 条 GET 不单列 scope。现状是「回环匿名、非回环须身份」（`src/writeauth.py:142-172`）；设备只读令牌应只拿 `hub.read` + 明确排除 `/api/users`、`/api/settings`、`/api/term` 的敏感读。

## 3. 逐条清单

> 说明：「外部副作用」= 会起进程、发外网请求、改全局配置或产生 LLM 费用；`—` = 只写本地库/文件。
> 位置列是装饰器行号，可直接跳转。

| scope | 写端点 | 其中外部副作用 | 说明 |
|---|---|---|---|
| `hub.task.write` | 11 | 5 | |
| `hub.agent.write` | 12 | 5 | |
| `hub.memory.write` | 8 | 0 | |
| `hub.skill.write` | 10 | 1 | |
| `hub.catalog.write` | 15 | 0 | |
| `hub.mcp.write` | 6 | 1 | |
| `hub.settings.write` | 12 | 3 | |
| `hub.ops.write` | 14 | 7 | |
| `hub.user.admin` | 9 | 0 | |

### hub.task.write（11 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/api/tasks/decompose` | `src/tasks_dag.py:487` | 🟠 起 agent |
| DELETE | `/api/tasks/{run_id}` | `src/tasks.py:865` | — |
| POST | `/api/tasks/{run_id}/accept` | `src/tasks.py:766` | — |
| POST | `/api/tasks/{run_id}/cancel` | `src/tasks.py:849` | — |
| POST | `/api/tasks/{run_id}/pause` | `src/tasks.py:836` | — |
| POST | `/api/tasks/{run_id}/reject` | `src/tasks.py:788` | — |
| POST | `/api/tasks/{run_id}/replan` | `src/tasks_replan.py:257` | 🟠 起 agent |
| POST | `/api/tasks/{run_id}/resume` | `src/tasks.py:826` | 🟠 起 agent |
| POST | `/api/tasks/{run_id}/retry` | `src/tasks.py:754` | 🟠 起 agent |
| POST | `/api/tasks/{run_id}/start` | `src/tasks.py:745` | 🟠 起 agent |
| POST | `/task` | `src/intake.py:1180` | — |

### hub.agent.write（12 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/api/agents` | `src/api_agents.py:178` | — |
| POST | `/api/agents/detect` | `src/api_agents.py:172` | — |
| DELETE | `/api/agents/{agent_id}` | `src/api_agents.py:204` | — |
| POST | `/api/agents/{agent_id}/chat` | `src/api_agents.py:470` | 🟠 调 LLM |
| POST | `/api/agents/{agent_id}/chat/stream` | `src/api_agents.py:485` | 🟠 调 LLM |
| POST | `/api/agents/{agent_id}/ui-start` | `src/api_agents.py:341` | 🟠 起进程 |
| POST | `/api/agents/{agent_id}/verify` | `src/api_agents.py:149` | — |
| PATCH | `/api/sessions/{session_id}` | `src/api_sessions.py:131` | — |
| DELETE | `/api/sessions/{session_id}` | `src/api_sessions.py:142` | — |
| POST | `/api/term/sessions` | `src/term.py:890` | 🟠 起 pty |
| DELETE | `/api/term/sessions/{sid}` | `src/term.py:1085` | — |
| POST | `/start` | `src/cloudcli.py:155` | 🟠 起 UI 进程 |

### hub.memory.write（8 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/api/memory/archive` | `src/memory.py:387` | — |
| POST | `/api/memory/l1` | `src/memory.py:460` | — |
| POST | `/api/memory/l1/batch` | `src/memory.py:472` | — |
| PUT | `/api/memory/l1/{mid}` | `src/memory.py:345` | — |
| DELETE | `/api/memory/l1/{mid}` | `src/memory.py:483` | — |
| PUT | `/api/memory/l2` | `src/memory.py:625` | — |
| POST | `/api/memory/l2/rebuild` | `src/memory.py:642` | — |
| PUT | `/api/memory/l3` | `src/memory.py:636` | — |

### hub.skill.write（10 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/acquire` | `src/skill_store.py:1214` | — |
| POST | `/api/skill-assets/{slug}` | `src/skill_assets.py:319` | 🟢 本地产物 |
| POST | `/api/skill-assets/{slug}/transition` | `src/skill_assets.py:328` | — |
| POST | `/auto` | `src/skill.py:1286` | — |
| POST | `/install` | `src/skill.py:1676` | — |
| POST | `/install` | `src/skill_store.py:1186` | — |
| POST | `/invoke` | `src/skill.py:1214` | — |
| POST | `/login` | `src/skill_store.py:1247` | — |
| POST | `/logout` | `src/skill_store.py:1279` | — |
| DELETE | `/remove` | `src/skill.py:1717` | — |

### hub.catalog.write（15 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/api/actions` | `src/actionplane.py:482` | — |
| POST | `/api/actions/{action_id}/decide` | `src/actionplane.py:491` | — |
| POST | `/api/actions/{action_id}/settle` | `src/actionplane.py:503` | — |
| POST | `/api/admission/scan` | `src/admission.py:438` | — |
| POST | `/api/admission/tickets` | `src/admission.py:429` | — |
| POST | `/api/admission/tickets/{slug}/{version}/stage` | `src/admission.py:453` | — |
| DELETE | `/cards/{card_id}` | `src/refscout.py:471` | — |
| POST | `/cards/{card_id}/refresh` | `src/refscout.py:464` | — |
| POST | `/kb/probe` | `src/refscout.py:492` | — |
| DELETE | `/kb/probe` | `src/refscout.py:508` | — |
| POST | `/kb/promote` | `src/refscout.py:514` | — |
| POST | `/kb/test` | `src/refscout.py:500` | — |
| POST | `/scout` | `src/refscout.py:411` | — |
| POST | `/sources` | `src/refscout.py:419` | — |
| DELETE | `/sources/{sid}` | `src/refscout.py:423` | — |

### hub.mcp.write（6 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/mcp/acl` | `src/mcpgw.py:304` | — |
| DELETE | `/mcp/acl/{acl_id}` | `src/mcpgw.py:331` | — |
| POST | `/mcp/call` | `src/mcpgw.py:527` | 🔴 任意工具调用 |
| POST | `/mcp/servers` | `src/mcpgw.py:214` | — |
| POST | `/mcp/servers/probe` | `src/mcpgw.py:267` | — |
| DELETE | `/mcp/servers/{sid}` | `src/mcpgw.py:253` | — |

### hub.settings.write（12 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| PUT | `/api/prefs/{key}` | `src/prefs.py:95` | — |
| POST | `/api/settings/github/apply` | `src/ghsettings.py:902` | 🔴 改 GitHub 凭据 |
| POST | `/api/settings/github/clear` | `src/ghsettings.py:923` | — |
| POST | `/api/settings/github/refresh` | `src/ghsettings.py:929` | — |
| POST | `/api/settings/github/sources` | `src/ghsettings.py:963` | — |
| PATCH | `/api/settings/github/sources/{sid}` | `src/ghsettings.py:971` | — |
| DELETE | `/api/settings/github/sources/{sid}` | `src/ghsettings.py:979` | — |
| POST | `/api/settings/github/test` | `src/ghsettings.py:889` | — |
| POST | `/api/settings/import` | `src/settings_bundle.py:320` | 🟠 覆盖设置 |
| POST | `/api/settings/model/apply` | `src/modelcfg.py:848` | 🔴 改全局模型路由 |
| POST | `/api/settings/model/undo` | `src/modelcfg.py:859` | — |
| POST | `/api/settings/model/verify-live` | `src/modelcfg.py:826` | — |

### hub.ops.write（14 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/api/approvals/{aid}/approve` | `src/approvals.py:274` | — |
| POST | `/api/approvals/{aid}/deny` | `src/approvals.py:288` | — |
| POST | `/api/csp/report` | `src/main.py:797` | — |
| POST | `/api/github/clone` | `src/githubprojects.py:745` | 🟠 外网克隆 |
| POST | `/api/jobs` | `src/cronjobs.py:73` | — |
| PUT | `/api/jobs/{jid}` | `src/cronjobs.py:109` | — |
| DELETE | `/api/jobs/{jid}` | `src/cronjobs.py:132` | — |
| POST | `/api/jobs/{jid}/run` | `src/cronjobs.py:139` | 🟠 立即跑任务 |
| POST | `/api/resources/kill` | `src/resources.py:306` | 🔴 杀进程 |
| POST | `/api/scan/run` | `src/main.py:1117` | 🟢 本地扫描 |
| POST | `/api/summarize` | `src/api_summarize.py:25` | 🟠 调 LLM |
| POST | `/api/system/restart` | `src/api_system.py:33` | 🔴 重启服务（杀会话） |
| POST | `/api/vitals/sweep` | `src/api_vitals.py:41` | 🟢 本地扫描 |
| POST | `/telemetry/events/{source}` | `src/hook.py:59` | — |

### hub.user.admin（9 条）

| 方法 | 路径 | 位置 | 外部副作用 |
|---|---|---|---|
| POST | `/api/users` | `src/users.py:425` | — |
| POST | `/api/users/login` | `src/users.py:273` | — |
| POST | `/api/users/logout` | `src/users.py:306` | — |
| DELETE | `/api/users/{name}` | `src/users.py:525` | — |
| POST | `/api/users/{name}/credentials` | `src/users.py:479` | — |
| POST | `/api/users/{name}/password` | `src/users.py:320` | — |
| POST | `/api/users/{name}/password-clear` | `src/users.py:348` | — |
| PUT | `/api/users/{name}/perms` | `src/users.py:508` | — |
| POST | `/api/users/{name}/revoke` | `src/users.py:495` | — |

## 4. 与 P0 的衔接

按本清单，P0 不是「加登录」（登录已有），而是把 `write_gate` 的单一 `allow` 改成「identity + scope」。分三步，两个判断点单列：

1. **声明层**：建 `SCOPES` 表，给 97 条逐条标 scope（默认 `hub.ops.write` 仅限现存的 14 条，其余必须显式声明）。
2. **判据**：加测试「每条写路由必须在声明表里，漏一条就失败」——照 t3 的 `satisfies Record` 思路，但 Python 用测试兜底。
3. **看第 2 步结果再决定兼容策略**：admin 自动全通；非 admin 按 `user_perms` 映射只读（因为当前 0 非管理员，不会伤到现网）。
4. **干跑 24h**：只记 403 候选、不真拒，确认页面无回归。
5. **切换 enforce**：先只对 22 条副作用端点强制 scope，其余保持登录放行；稳定后再全量。

**验收判据（可执行）**：只读令牌打 22 条副作用端点 → 全部 403；admin 会话 → 全通；现有前端零 403；`tests/test_writeauth.py` 的清单覆盖断言仍绿。

## 5. P0-1 落地状态（2026-11-09）

- **声明表（唯一权威）**：`src/authz.py`。97 条 `(module, method, path) → scope`，9 个写 scope，22 条副作用集合，3 条豁免（`EXEMPT_WRITE_ROUTES`，与 `writeauth.EXEMPT_PREFIXES` 逐条对齐）。
- **一致性闸门**：`tests/test_authz_scopes.py`，8 条断言。核心是「真实路由集合 == 声明集合」的双向比对：漏声明、僵尸声明、未知 scope、豁免口径分叉、副作用条数漂移，任一条都失败。
- **行为零变化**：`writeauth.decide()` 未被修改，本表尚不参与鉴权。P0-2 才接。
- **验收命令**：`venv/bin/python -m pytest -q tests/test_authz_scopes.py tests/test_writeauth.py` → 22 passed。
- **分布核对**：task 11 / agent 12 / memory 8 / skill 10 / catalog 15 / mcp 6 / settings 12 / ops 14 / user 9 = 97。

### P0-2 的第一个判断点：scope 挂在哪

现状会话 = `user_credentials` 里的一行（只存 token sha256，`src/users.py:132-137`），**没有 scope 列**。两条路：

| 方案 | 改动 | 代价 | 适用 |
|---|---|---|---|
| A 加列 | `user_credentials` 增 `scopes TEXT NULL`；NULL = 沿用现行（admin 全通 / 普通用户按 user_perms 只读），非 NULL = 显式 scope 白名单 | 1 次幂等迁移 + 1 个读取点 | 先让现有登录不受影响，再加配对令牌 |
| B 新表 | `token_scopes(token_hash, scope, expires_at)` 多行 | 多一张表、多一次 join | 一 token 多 scope 且可单独过期（配对场景更干净） |

建议 **A 先行**：现网 0 非管理员，NULL 语义能保证「升级零感知」；配对令牌落地时再把 B 作为增量，不返工。
