# AgentHub 迭代方案 · 2026-11-20

> 目标：前端只留「真在用的」，数据层收敛到「一个业务库 + 三个派生/只读库」，在不重启、不改线上配置的前提下先把体检做完，再分期动刀。
> 铁律：**稳定优先于收益**。每一步都必须能在 5 分钟内回滚，且不改变当天已验证通过的运行路径。

---

## 0. 判据从哪来（全部为实测，不是估计）

| 证据源 | 取样 | 说明 |
|---|---|---|
| systemd journal `agenthub.service` | 2026-10-30 → 11-20，645,213 行访问日志 | 真实调用频次（含探针噪声，已注明） |
| `/health` 实时快照 | 2026-11-20 00:47 | 子系统状态、目录滞后、保留期 |
| SQLite 直读（`dbstat` / `PRAGMA` / `count`） | 四个库文件 | 容量、行数、配置 |
| 静态可达性扫描 | `templates/index.html` + `static/hub/*.js` | 页面/组件是否可达 |
| 线上 5xx 聚合 | 同 journal | 已知故障点 |

---

## 1. 现状盘点

### 1.1 前端：22 个页面，实际有数据流动的约一半

21 天内对应端点调用次数（每次进页/轮询都算，含多端并发）：

| 页面 | 关键端点 | 21 天调用 | 结论 |
|---|---|---|---|
| 协同 tasks | `/api/tasks/*` | 9,745 | 在用 |
| MCP | `GET /mcp/servers` | 9,423 | 只在浏览；安装/运行/同步 **0** |
| 定时 jobs | `GET /api/jobs` | 9,403 | 只被轮询；**新建/PUT/立即运行 0** |
| 积分签到 checkin | `/api/checkin/*` | 112 | 在用（新功能） |
| 资源 resources | `/api/resources` | 9 | 极少 |
| 端口 ports | `/api/ports` | 3 | 极少 |
| Botboy | `/api/botboy/overview` | 1 | 基本未用 |
| 遥测 telemetry | `/telemetry/events`、`/telemetry/usage/summary` | **0** | 未用（表里仅 1 行） |
| 资产 assets | 逐路点 `memory/kb/skill` 检索 | 无独立端点 | 与记忆/知识库/技能页重复 |
| 设置 · 认证状态 settings-token | 无 | 无 | **孤儿页：无菜单入口、无 `go()` 调用** |

另有两处「已删页面、代码还在」：

- `static/hub/01-core-boot.js:1140-1743`：设置→模型页已于 2026-11-17 删除，但整段 UI 逻辑（`settingsModelsLoad` / `settingsPickAgent` / `settingsApplyModel` / `settingsVerifyModel` / `settingsUndoModel` 等）仍在，全部依赖**模板里已不存在**的 DOM（`setModelSel`、`setApplyBtn`、`setDiffBox`…）。
- 侧栏搜索已删（`templates/index.html:1793` 注释），但 `renderNav()` / `navItemHtml()` 仍读 `#navSearch`、`#navSearchCount`。

菜单有 **三份真相源**，已经漂移：

| 真源 | 位置 | 漂移实例 |
|---|---|---|
| 前端硬编码 | `SYS_PAGES` / `SET_PAGES`（`05-chat-and-history.js:478`） | `checkin` 在 SYS_PAGES，不在后端白名单 |
| 后端白名单 | `MODULE_SYS` / `MODULE_SET`（`users.py:61`） | 缺 `checkin`、`settings-restart`；多 `settings-model`（无页面） |
| DB | `menu_items` 表 | 前端**从不读取**，`enabled` 开关只影响授权白名单，不影响渲染 |

后果：普通用户即使被授予 `group:system`，`checkin` 仍会被 `_validate_perms` 静默丢弃 → 侧栏整项消失。

已知 5xx（21 天）：`POST /api/tasks/decompose` 4 次、`GET /api/term/sessions` 4 次、`POST /api/settings/model/verify-live` 2 次、`POST /api/users/login` 1 次。

### 1.2 数据层：4 个 SQLite 文件，其中 1 个是僵尸、1 个有 13 张空表

| 库 | 文件 | 内容 | 状态 |
|---|---|---|---|
| `agents.db` | 968 MiB | 业务唯一写库（34 张表） | 活跃，WAL 9.2 MiB |
| `memindex.db` | 5.78 GiB | 派生索引：`fed_proj` 2.02 GiB + trigram FTS 3.56 GiB | 活跃 |
| `catalog.db` | 1.61 GiB | 派生目录：464,157 chunk，39,550 obj，2 套 FTS | 活跃 |
| `hub.db` | 172 KiB | **全表 0 行**，最后写入 2026-11-08，代码无引用 | 僵尸 |

`memindex.db` 里另有 13 张与业务库同名的**空表**（`memories` / `telemetry_events` / `chat_messages` / `asset_audit` / `app_prefs`…，全部 0 行）——历史上某次对派生库跑过 `db.init_db()`，把整套业务 SCHEMA 灌了进去。

`agents.db` 容量构成（`dbstat`）：

| 表/索引 | 大小 | 行数 | 备注 |
|---|---|---|---|
| `term_recordings` | 702.8 MiB | 2,951,514 | 其中 `idx_termrec_sid` 66.2 MiB、`idx_termrec_ts` 51.9 MiB |
| `profile_events` | 68.2 MiB | 200,517 | 30 天保留期在跑（上轮删 332 行） |
| `mcp_source_cache` | 57.8 MiB | 38,756 | **无保留期**，38,456 条 `active/latest` |
| `chat_messages` | 1.3 MiB | 488 | — |
| `memories` | <1 MiB | 6 | 记忆真源不在主库 |

关键异常：**终端录像从 2026-11-11 起停止写入**（11-12 之后 0 行；`TERM_RECORD=1` 在 `.env:73`，`term.py:615` 仍会构造 Recorder）。录像数据本身已累积 512 MiB，而 `/api/term/recording/*` 21 天 0 次调用。

PRAGMA 现状（`agents.db`）：`journal_mode=wal`、`synchronous=NORMAL`、`foreign_keys=ON`、`busy_timeout=5000`、`cache_size=-2000`（2 MiB）、`mmap_size=0`、`auto_vacuum=0`、`freelist_count=726`。代码里除启动那一次 `wal_checkpoint(TRUNCATE)` 外，**没有任何 `ANALYZE` / `PRAGMA optimize` / 定期 checkpoint**。

磁盘：`/fs` 169 GiB，已用 147 GiB，**剩 21 GiB（88%）**。仅 `data/*.bak*` 备份就占约 12.6 GiB（agents 5.1 GiB + catalog 2.6 GiB + memindex 4.9 GiB）。

### 1.3 运行健康（`/health`，v0.13.205）

| 子系统 | 状态 | 证据 |
|---|---|---|
| 主库、记忆后端、CCR 网关 | ok | `db_ok=true`；memory L1=376/L0=13227；网关 52 个模型 |
| 保留期 | ok | 上轮 `profile_events:332`，`term_recordings:0` |
| catalog 新鲜度 | **滞后 17.7 h** | `catalog_lag_s=63676`，最差源 `logs.hub` |
| 档位模型映射 | **缺 6/13** | `model_gate.state=missing`（t1/t2/t3 各 2 个 `alibaba/*` 不在网关清单） |
| 进程内存 | 高 | cgroup 3.72 GiB（含 page cache，主库读放大） |

catalog 只有 `sessions.archived` 在按 20 分钟增量扫；`docs.filesystem`、`projects.local`、`logs.hub` 等依赖手动 `scripts/hub-catalog-scan.sh`，没有 systemd timer。

---

## 2. 前端瘦身方案（先做不可见的，再让用户裁决可见的）

### P1a · 零可见变化清理（建议立刻做，回滚=git revert）

**已完成（2026-11-20）**：孤儿页 `page-settings-token` 已删除（`templates/index.html`），同步摘掉 `SET_PAGE_IDS` / `PAGE_LABELS` 条目，并更新 `tests/test_dom_structure.py`、`tests/test_overlay_exclusion.py` 的契约；`build_hubjs.sh` 重建后 `?v=effc091d`，`pytest` 6 个闸门 99 例全绿。

1. ~~删除孤儿页 `page-settings-token`~~（已完成）。
2. 删除设置→模型死代码块 `01-core-boot.js:1140-1743`，**保留**终端仍在用的 4 个函数：`loadSetAgents`、`modelMetaFromValue`、`modelConfirmPaid`、`modelOptionLabel`（`04-terminal-ws.js` 依赖）。
3. 删除侧栏搜索残留：`renderNav()` 的 `q` 分支、`navItemHtml()` 的 `hlMatch`、`#navSearchCount` 写入；`navMatch/hlMatch` 一并删。
4. 删除 `tests/test_settings_ui.py::test_model_select_auto_previews_diff`（钉的是已删页面的实现），`tests/probe_model_billing.py:170` 的 `settingsPickAgent` 调用改指终端选择器。

验收：`node --check`、`bash scripts/build_hubjs.sh`、`tests/test_hubjs_split.py`、`test_dom_structure.py`、`test_ghsettings.py`、`test_hublog.py`、`test_tdz_order.py` 全绿；`?v=` 提手由构建脚本按内容重算。

### P1b · 菜单单一真源（修漂移，不改外观）

- 后端新增只读 `GET /api/menu`，从 `menu_items` 返回 `{module,label,icon,grp,order,enabled}`。
- 前端 `SYS_PAGES`/`SET_PAGES` 改为「接口优先、失败回落硬编码」（旧后端 404 时行为不变）。
- 授权页 `authMenuTreeHtml()` 与渲染共用同一份清单，`enabled=0` 真正隐藏。
- 补齐 `MODULE_SYS += checkin`、`MODULE_SET += settings-restart`、移除 `settings-model`（无页面）；`menu_items` 保留历史行只置 `enabled=0`，不删数据。

验收：普通用户授予 `group:system` 后能看到 checkin；`tests/test_users.py` 白名单用例扩展；接口 404 时回落路径有单测。

### P1c · 页面存废（**需要你拍板**，我只给证据与建议）

| 候选 | 21 天证据 | 建议 |
|---|---|---|
| 遥测 telemetry | 0 次调用；`telemetry_events` 仅 1 行 | **删页面 + 首页「遥测」按钮**；`POST /telemetry/events/{source}` 保留给外部上报 |
| 资产 assets | 与记忆/知识库/技能三页检索重复 | **删页面**；不动三路 API |
| Botboy | 1 次打开 | 删页面；`/api/botboy/overview` 保留 |
| 端口 ports | 3 次打开 | 保留（排障用），从首页动作区移除 |
| 定时 jobs | 0 次新建/改/运行 | 先查清 9,403 次轮询来自谁；确认无真实 job 再删页面 |
| MCP 动作区（安装/运行/同步） | 0 次 | 保留浏览，隐藏动作区或折叠 |
| 记忆 L2/L3 重建、归档矩阵 | 0–2 次 | 折叠到「高级」 |

删页面时同步：HTML section、`SYS_PAGES`、`PAGE_LABELS`、`go()` 钩子、对应 JS 分片（构建脚本按 `NN-*.js` 全量拼接，删文件要同步）、`tests/test_dom_structure.py` 必需页清单。**一次只删一个页面，跑完全套闸门再删下一个**。

### P1d · 轮询与重复请求

- `GET /api/jobs` 9,403 次、`GET /mcp/servers` 9,423 次却几乎无写操作 → 先定位轮询者（`04-terminal-ws.js:3995` 和页面驻留），合并为「可见页才轮询 + 30s 退避」。
- `loadAgents` 30s 轮询会顺带触发 `renderNav()` 重建；已有 `_navHtml` diff，保持不重写 DOM 的纪律。

---

## 3. 数据层统一与提效方案

### 设计原则（来自 SQLite 官方与生产实践）

1. **单写者**：所有写仍只经 `db.execute`（进程内单连接 + 锁）。外部 CLI 只读。参考 [SQLite WAL 文档](https://sqlite.org/wal.html) 与 [WAL 运维坑位](https://nalar.dev/sqlite-wal-mode-concurrency-checkpoints-and-operational-pitfalls)。
2. **不做运行期 VACUUM**：大库重写会长时间持写锁；空间回收用 `auto_vacuum=INCREMENTAL` + `PRAGMA incremental_vacuum(N)` 小步进行（[官方 PRAGMA](https://sqlite.org/pragma.html)，[回收实践](https://oneuptime.com/blog/post/2026-09-08-reclaim-sqlite-disk-space-vacuum-auto-vacuum/view)）。
3. **只读库用 `mode=ro&immutable=1` + mmap + 大 cache**：`memindex.db` / `catalog.db` 是无写竞争的派生库，适合 `PRAGMA mmap_size=268435456`、`cache_size=-65536`（[服务器调优清单](https://kerkour.com/sqlite-for-servers)、[PowerSync 优化](https://powersync.com/blog/sqlite-optimizations-for-ultra-high-performance)）。
4. **FTS5 保持 external content**，不复制正文；定期 `optimize`，不在热路径重建（[FTS5 文档](https://sqlite.org/fts5.html)）。
5. **零新依赖**（仓库硬约束）：不引 ORM/连接池库；FastAPI 同一线程池内的同步 sqlite 连接沿用现有模式，`check_same_thread=False` + 全局锁是可接受且已实测稳定的（[FastAPI 讨论](https://github.com/fastapi/fastapi/discussions/5199)）。

### P2a · 体检与冻结（只读，0 风险）

产出 `data/_reports/db-baseline-YYYYMMDD/`：每个库的 `page_count/page_size/freelist_count/journal_mode`、`dbstat` 表级大小、各表行数与时间范围、`integrity_check` 结果、sha256。

固化 SQL（可重复执行）：

```sql
PRAGMA page_count; PRAGMA page_size; PRAGMA freelist_count;
SELECT name, SUM(pgsize)/1048576.0 mb FROM dbstat GROUP BY name ORDER BY mb DESC LIMIT 20;
SELECT * FROM sqlite_master WHERE type='index' AND sql IS NULL;  -- 隐式索引（期望为空）
```

### P2b · 僵尸库与重复 SCHEMA（低风险）

1. `data/hub.db` → `data/_archive/hub.db.zombie-20261120`，**不直接 `rm`**；加启动断言：任何模块把连接指向 `hub.db` 即 `log.error`（防回潮）。
   **已完成（2026-11-20）**：16 张表全 0 行、无进程句柄、代码无引用，已归档；sha256 `c9bda69f…60a74cfa4`；移动后 `/health` 仍 `ok / db_ok=true`。启动断言待补。
2. `memindex.db` 备份后 `DROP` 13 张空业务表；只保留 `fed_proj` + FTS + `meta`。同时在 `db.init_db()` 增加路径守卫：只允许 `config.db_path`，其他路径必须显式 `allow_derived=True`（修 09-24 已记录的「重指全局连接」同族 bug）。
3. 验收：`tests/test_db_pragma.py`、`test_memindex_proj.py`、`test_health_selfattest.py` 全绿；`memindex` 重建仍成功。

### P2c · 统一连接层（核心，风险中）

新增 `db.connect_readonly(path, *, immutable=False)` 与 `db.connect_derived(path)`，统一：

| 参数 | 只读派生库 | 主写库 |
|---|---|---|
| `journal_mode` | 保持 wal（只读不写） | wal |
| `busy_timeout` | 5000 | 5000 |
| `cache_size` | -65536（64 MiB） | -16000（16 MiB） |
| `mmap_size` | 256 MiB | 先 0，压测后再开 |
| `query_only` | 1 | 0 |
| `temp_store` | memory | memory |

替换 10 处散落的 `sqlite3.connect(...)`（`memfed.py:257`、`memindex.py:454`、`memvitals.py:144`、`sessions_store.py:432`、`skill_direct.py:437`、`usage_ledger.py:72`、`api_models.py:43`、`cloudcli.py:66`、`modelcfg.py:145`…）。每处逐个改、逐个跑对应测试，**不一次性大改**。

预期收益：catalog/memindex 的冷查询（当前 `kb/search` 冷启 3.1 s，`memindex` FTS 全量命中 1.6 s）主要耗在页读与 FTS 段合并；mmap + 大 cache + 定期 optimize 的组合收益需用同一查询前后各测 10 次取 p50 验证，达不到 20% 就不保留。

### P2d · 维护例程（定时、低峰、可关）

- 每小时：`PRAGMA optimize`（官方推荐，代价极低）。
- 每日 04:30：`PRAGMA wal_checkpoint(TRUNCATE)`（现在只有启动一次，WAL 9.2 MiB 属健康，但持续增长无上限）。
- 每周：`PRAGMA incremental_vacuum(2000)`（切 `auto_vacuum` 需**一次性离线重建**，见 P2e）。
- 所有任务走 `retention.py` 同款模式：独立 try/except、失败不影响主循环、状态进 `/health`。

### P2e · 容量治理（收益最大，需维护窗口）

| 对象 | 动作 | 预期回收 |
|---|---|---|
| `term_recordings`（702 MiB） | 先定位 11-11 停录原因；把 >30 天记录 `ATTACH` 迁到 `data/archive/recordings-202611.db`，主库 DELETE | ~700 MiB 表 + 118 MiB 索引 |
| `mcp_source_cache`（58 MiB） | 30 天 TTL：只留 `active/latest`，其余按 `fetched_at` 清 | 30–50 MiB |
| `data/*.bak*`（12.6 GiB） | 只留最新 1 份 + 上周 1 份，其余归档到 NAS | ~9 GiB，磁盘回到 30 GiB 可用 |
| `catalog.db`（1.61 GiB） | 评估两套 FTS 是否都必要（trigram + unicode61）；先在副本上删一套跑检索验收 | 最多 ~500 MiB |
| `memindex.db`（5.78 GiB） | 在副本上试 `detail=none`（体积降、bm25 不可用）与 `page_size=8192`；命中质量不过关就不采纳 | 1–3 GiB |

主库重建（`auto_vacuum=INCREMENTAL`）流程：`VACUUM INTO` 到新文件 → `integrity_check` → 校验行数/关键表哈希 → 停机 2 分钟原子替换 → 启服务看 `/health`。**不在运行中 VACUUM。**

### P2f · 新鲜度与已知错误

- 给 `hub-catalog-scan.sh --source all` 加 systemd timer（6 小时），并修 `logs.hub` 适配器滞后；验收 `catalog_lag_s < 21600`。
- `model_gate` 缺 6 个 `alibaba/*`：要么补网关清单，要么把档位映射改成网关现存的 `magpie/*`；先只读核对 `data/agents.db` 的 `agent_models` 与网关 `/v1/models`。
- `POST /api/tasks/decompose` 的 4 次 5xx：抓一次堆栈再定，别猜。

---

## 4. 分期与闸门

| 期 | 内容 | 工时 | 风险 | 闸门 / 回滚 |
|---|---|---|---|---|
| P0 | 体检基线、慢查询埋点（只读） | 1–2 h | 无 | 记录 sha256，不改代码 |
| P1a | 前端死代码 + 孤儿页 | 2–3 h | 极低（不可见） | `node --check` + 7 个 L0 用例；git revert |
| P1b | 菜单单一真源 | 3–5 h | 低 | 用户/授权用例 + 手工两账号验证 |
| P1c | 页面存废（需裁定） | 1–2 h/页 | 中（可见） | 每删一页跑 `run_tests.sh changed` + 浏览器冒烟 |
| P2a–b | 体检 + 僵尸库清理 | 1 h + 30 min 备份 | 低 | `integrity_check`；文件归档可回拷 |
| P2c | 连接层统一 | 3–4 h | 中 | 每改一处跑对应模块测试；灰度一个模块观察 24 h |
| P2d–e | 维护例程 + 容量 | 4–6 h | 中（需窗口） | 先副本演练；`VACUUM INTO` + 原子替换 |
| P2f | 新鲜度/错误 | 2–3 h | 低 | `/health` 指标连续 3 天达标 |

时区约束：所有 DB 维护放在 03:00–05:00；前端改动随时可发，但发完必须 `build_hubjs.sh`（内容哈希改 `?v=`，客户端自动换新）。

---

## 5. 验收指标

| 指标 | 现状 | 目标 |
|---|---|---|
| `/health.catalog_lag_s` | 63,676 | < 21,600 |
| `kb/search` 冷启 p50 | 3.14 s | < 1.5 s |
| `memindex` FTS p50 | 1.62 s（高频词） | 不劣化，体积下降优先 |
| `agents.db` 文件 | 968 MiB | < 250 MiB（录像归档后） |
| 磁盘可用 | 21 GiB | > 40 GiB |
| 页面数 | 22 | ≤ 17（按 P1c 裁定） |
| 菜单真相源 | 3 | 1（`menu_items`，前端回落一份） |
| 已知 5xx | decompose 4 次 | 0 |

---

## 6. 明确不做

- 不迁 PostgreSQL、不引 ORM/连接池依赖（违反仓库零新增依赖约束）。
- 不在运行期对 1 GiB 级库跑 `VACUUM`。
- 不删任何 API（页面删了，端点留给 MCP/外部调用方）。
- 不动 `.env` 密钥与登录链路；不改 `?v=` 缓存协议。

---

## 7. 立即可做的 5 个零风险动作（今天）

1. `data/hub.db` 归档 + 启动断言（10 分钟，可回拷）。
2. 前端 P1a 四个文件清理 + 构建 + 7 个 L0 用例（2–3 小时，不可见）。
3. 副本上演练 `VACUUM INTO` + `incremental_vacuum`，量真实耗时（1 小时，不碰线上）。
4. 备份轮转：留最新 + 上周，释放 ~9 GiB（30 分钟，NAS 上另存）。
5. 定位两处轮询源（`/api/jobs`、`/mcp/servers` 各 ~9,400 次/21 天），决定合并策略（1 小时，只读分析）。

---

## 8. 执行回执（2026-11-20，已完成并验证）

| 动作 | 结果 | 证据 |
|---|---|---|
| 删除页面 遥测 / 资产 / Botboy | 模板 section、`SYS_PAGES`、`PAGE_LABELS`、`go()` 钩子、`07-asset-panel.js`、`14-botboy.js` 全链路移除；首页「遥测」按钮删除；端口页保留 | 线上 `/` 已无 `page-telemetry/assets/botboy`；浏览器实跑 `renderNav()` 得系统组 8 项、设置组 5 项 |
| 删除设置→模型死代码 | `01-core-boot.js` 移除 23 个顶层声明/函数（`settingsModelsLoad`…`settingsRenderUndone`、`SET_WRITE_MODE` 等），保留终端在用的 `loadSetAgents/modelMetaFromValue/modelConfirmPaid/modelOptionLabel` | `node --check` 通过；`test_models_billing` 24 例绿 |
| 侧栏搜索残留 | `navMatch/hlMatch/#navSearch` 全删，渲染只按权限+排序 | `test_hubjs_split`/`test_tdz_order`/`test_ls_guard` 绿 |
| 菜单真相源对齐 | 后端 `MODULE_SYS/SET + ICONS/LABELS` 与前端逐项一致（补 `checkin`/`settings-restart`，去 `telemetry/assets/botboy/settings-model/settings-token`）；`menu_items` 中已删项 `enabled=0` | `test_users`/`test_authz_scopes` 等 179 例绿 |
| 备份轮转 | 每个家族保留最新 2 份；删除 125 个旧备份（~3.2 GiB） | `data/agents.db.bak-*` 仅剩 11-19 两份；`*.bak-*` 0 个被 git 跟踪 |
| 僵尸库 | `hub.db` 归档（0 行、无句柄、无引用） | sha256 `c9bda69f…60a74cfa4`；`/health` 仍 ok |
| 退役 CCR 栈 | 9 个 `ccr*.service` 归档到 `data/_archive/systemd-units-20261120/`；6 个孤儿 socat/代理进程停止，3456/3458/13458 端口释放 | `ss -ltnp` 已无这些端口；magpie `:3425` 不受影响 |
| 坏 MCP 条目 | `hub-demo`（指向不存在的 `/home/gztxt/agent-hub`）删除，行备份在 `data/_archive/mcp-demo-row.json` | `quick_check ok`；`mcp_servers` 仅剩 `ekko-studio-use` |
| 测试垃圾 | `work/` 下 >7 天产物清掉：6.9 GiB → 28 KiB | `work/` 仅剩最近 11-13 的文件 |
| 结构闸门 bug | `test_tdz_order` 函数体切片多算一行（off-by-one），已修正，红基线用例仍通过 | `test_tdz_order` 4 例绿 |

**未执行（需重启/需你拍板）**：`/api/menu` 单一真源（要后端重启才生效）、`memindex.db` 空表清理（需停机备份窗口）、录像归档与 `incremental_vacuum`（需维护窗口）、jobs 页存废（先查 9,403 次轮询来源）。