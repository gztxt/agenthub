# AgentHub 统一数据层方案（catalog.db + 统一接口）

> 2026-10-25 · 对应需求：数据零散 → 单库编目、单接口调用、原生不动、可移植
> 拟登记台账：**PT-20261025-12**（本文只出方案，未开工）
> ⚠ **PT-20261025-11 已被他会话占用**（2026-10-25 15:2x 实测：`src/memory_archive.py`
> 自称 v0.13.161 / PT-20261025-11「统一归档写入口」，且 `src/main.py`/`src/hubmcp.py`
> 同批有未提交改动）⇒ 本方案改号，且**避开**那两个文件的接入，见 §11 P0 分期说明。
> 现状基线版本：agenthub `v0.13.160`（`abfe86c`），生产 `/health` 在跑，`term_sessions=3`

---

## 0. 结论先行

1. **要统一的是「编目 + 索引」，不是「权威副本」。** 一个 `catalog.db` 存全部对象的目录、分块、全文索引、关系与扫描账本；原始数据仍留在各域原地（TDAI 容器 / XERJ / 各 agent 家目录 / agents.db 业务表 / journald）。这与 2026-10-24「物理合并不可达」不矛盾——那次否决的是搬走权威数据，这次建的是**可丢弃、可重建的投影**。
2. **现存 3 个真实缺口**（有实测，见 §2）：① 覆盖碎片化——wiki 4,730 篇、技能 3,054 个、1,834 个 jsonl 会话**没有一处能一起查**；② 2 字中文查询在 trigram 索引上**必然 0 命中**，现走 rg 全量扫描；③ 一次任务要调 4~6 个 MCP 工具才能凑齐情报。
3. **路径**：`catalog.db` 吸收现有 `memindex.db`（不是新增第五个库，是把它升级为全域网关），配 1 个统一工具 `hub_query`，旧工具降级为 wrapper 保留兼容。

---

## 1. 目标与非目标

### 目标（对应用户 5 条）

| # | 需求 | 本方案落点 |
|---|---|---|
| 1 | 所有数据统一一个数据库、不同类型不同表 | `catalog.db`：`obj` 总表 + `x_agent/x_skill/x_project/x_session/x_memory/x_doc/x_log/x_task` 分域表 + `chunk` 全文表 |
| 2 | 原始数据位置不变，只建统一索引与归档 | 适配器**只读**；`obj.uri` 指回原生位置；原文不复制（除用于检索的分块文本） |
| 3 | 统一接口供所有 agent 自动调用 | `hub_query / hub_get / hub_status` 三个 MCP 工具 + `GET /api/catalog/*` + `hubq` CLI |
| 4 | 原生数据独立运行，框架可无缝装到任意 Linux/其他系统 | 能力矩阵降级（缺 TDAI/XERJ/journald 就标 `unavailable` 不报错）+ 配置外置 + 服务管理抽象 |
| 5 | 提升汇总性能与准确性、数据质量、调用性能 | 双 FTS 路由（修 2 字查询）、bm25+权威+时效排序、内容哈希增量、去重/别名、孤儿与漂移校验 |

### 非目标（明确不做，避免重蹈）

- **不做向量检索**：本语料实测纯向量 0.4531 < 纯词法 0.6624（2026-10-17，已否决），`axvec` 已删除，不重建。
- **不做跨进程物理合并**：TDAI 是记忆权威、XERJ 是文档权威，均为跨进程服务，只读接入。
- **不搬各 agent 私产**：`~/.claude`、`~/.codebuddy` 等目录只扫不写；适配器全部以只读方式打开。
- **不做实时**：分钟级滞后可接受（SLO 见 §9）。
- **不索引终端录像正文**：`term_recordings` 2,877,921 行 / 680MB，只进元数据，正文留在 agents.db。

---

## 2. 现状实测（2026-10-25 现场取证）

### 2.1 存储分布

| 落点 | 体积 / 规模 | 内容 | 能否搬 |
|---|---|---|---|
| `data/agents.db` | 862MB（33 表） | 业务表：`term_recordings` 2.88M 行/680MB、`profile_events` 73,747 行/29MB、`chat_messages` 316、`skill_assets` 18、`ref_cards` 40、`tasks` 3… | 否（业务写库） |
| `data/memindex.db` | 3,862MB | `fed_proj` 355,272 块 / 10,804 文件 / 语料 1,072MB，trigram FTS | 是（投影，可重建） |
| TDAI `:8420` | 容器 `tdai-memory-core`（healthy） | 记忆权威 | 否（跨进程） |
| XERJ `:9300` | 独立 ES | 文档权威（`wiki` 4,730 篇 + `agent-knowledge` 243 篇） | 否（跨进程） |
| 各 agent 家目录 | 1,834 个 jsonl + 3,054 个 `SKILL.md` | 会话原文、技能正文 | 否（工具链私产） |
| `会话备份/` | 2,976 篇 md | 归档会话 | 否（原文） |

### 2.2 索引覆盖分布（`memindex.db` 实测）

```
archived_sessions   6,479 文件 / 198,522 块 / 530MB
pi_sessions           486 / 73,393 / 231MB
codex_sessions        418 / 32,709 /  89MB
hermes_skills_docs  2,438 / 29,209 /  25MB
hermes_memory         164 / 12,580 /  18MB
grok_skills_docs      362 /  4,252 /   3MB
workspace_files       364 /  3,708 /   3MB
claude_projects        72 /    234
```

→ 覆盖率缺口：**wiki 4,730 篇不在内**（只在 XERJ）、**codebuddy 会话不在内**、agents/skills/projects 元数据不在内、日志不在内。这就是「零散」的量化形态。

### 2.3 两个已证实的性能/准确性缺陷

1. **trigram 天生不支持 <3 字查询**。本机 SQLite 3.40.1 实测：`统一数据存储方案设计` 建 trigram 索引，查 `数据存储`→1，查 `数据`→**0**，查 `统一`→**0**。`memindex.search()` 因此对短查询直接 `return fallback="short_query"`，交回 rg 全量扫描（`src/memindex.py` 该分支无索引）。
2. **2 字查询是中文主场景**（记忆 / 会话 / 日志 / 技能 / 知识），当前等于把最常用的查询打到最慢的路径上。

---

## 3. 总体架构

```
┌──────────── 消费者 ────────────┐
│ MCP(hub_query/hub_get)  REST  CLI │
└───────────────┬────────────────┘
                │  统一契约 {items, backends, ms, degraded}
┌───────────────▼────────────────┐
│  查询层 query.py               │  长度路由 / 域过滤 / 排序 / 去重 / 缓存
└───────────────┬────────────────┘
┌───────────────▼────────────────┐
│  编目库 data/catalog.db (WAL)  │  obj · x_* · chunk · chunk_fts(tri) · chunk_ftsu(uni)
│  ← 唯一写者：hub 进程          │  edge · alias · src · scan_run · meta(schema_version)
└───────────────┬────────────────┘
┌───────────────▼────────────────┐
│  适配器层 adapters/ (只读)      │  agents skills projects sessions memory kb logs biz term
└───────────────┬────────────────┘
┌───────────────▼────────────────┐
│  原生层（权威，不动）           │  TDAI · XERJ · 各 agent 家目录 · agents.db · journald
└────────────────────────────────┘
```

**写者唯一**：只有 hub 进程写 `catalog.db`；其他进程/agent 通过 HTTP/MCP 只读。SQLite WAL 允许 N 读 1 写，读不阻塞写。

---

## 4. 数据库设计（`data/catalog.db`）

```sql
PRAGMA page_size=8192;
PRAGMA journal_mode=WAL;
PRAGMA synchronous=NORMAL;

-- ① 源登记：把 datastore.BACKENDS 从代码常量变成可查数据
CREATE TABLE IF NOT EXISTS src (
  src_id      TEXT PRIMARY KEY,        -- 'agents.declared' / 'skills.disk' / 'mem.tdai'
  domain      TEXT NOT NULL,           -- agent|skill|project|session|memory|doc|log|task
  kind        TEXT NOT NULL,           -- table|file|service|remote|code
  authority   INTEGER NOT NULL DEFAULT 0,   -- 1=该域权威源
  uri         TEXT NOT NULL,           -- 物理落点（路径/URL/表名）
  adapter     TEXT NOT NULL,
  scan_mode   TEXT NOT NULL,           -- full|incremental|on_demand
  enabled     INTEGER NOT NULL DEFAULT 1,
  last_ok_at  TEXT, last_ms INTEGER, last_err TEXT,
  cfg         TEXT                     -- JSON（根目录、超时、过滤规则）
);

-- ② 统一对象：一条 = 一个可寻址实体
CREATE TABLE IF NOT EXISTS obj (
  oid         TEXT PRIMARY KEY,        -- sha1(domain|src_id|native_key)[:16]，重扫不变
  domain      TEXT NOT NULL,
  type        TEXT NOT NULL,
  src_id      TEXT NOT NULL REFERENCES src(src_id),
  native_key  TEXT NOT NULL,           -- 路径 / 表主键 / TDAI id
  uri         TEXT,                    -- file:// / http:// / table:// 供 hub_get 打开
  title       TEXT, summary TEXT, lang TEXT,
  mtime       INTEGER, size INTEGER,
  hash        TEXT,                    -- blake2b-128 内容指纹，增量依据
  status      TEXT NOT NULL DEFAULT 'ok',   -- ok|missing|stale|error|archived
  meta        TEXT,                    -- JSON 域扩展
  first_seen  TEXT NOT NULL, last_seen TEXT NOT NULL,
  UNIQUE(domain, src_id, native_key)
);
CREATE INDEX IF NOT EXISTS obj_domain_mtime ON obj(domain, mtime DESC);
CREATE INDEX IF NOT EXISTS obj_hash ON obj(hash);

-- ③ 分域扩展表（"不同类型不同表"）：列存结构化筛选，正文不重复
CREATE TABLE IF NOT EXISTS x_agent(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  agent_id TEXT, installed INTEGER, native_ui INTEGER, ports TEXT,
  models TEXT, states TEXT);                 -- 声明/发现/运行 三态合并进 meta
CREATE TABLE IF NOT EXISTS x_skill(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  name TEXT, route TEXT, dir TEXT, description TEXT,
  tokens INTEGER, enabled INTEGER);
CREATE TABLE IF NOT EXISTS x_project(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  root TEXT, name TEXT, branch TEXT, dirty INTEGER,
  last_commit TEXT, worktrees INTEGER);
CREATE TABLE IF NOT EXISTS x_session(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  agent TEXT, cwd TEXT, project_oid TEXT,
  started_at INTEGER, ended_at INTEGER, msgs INTEGER, bytes INTEGER, model TEXT);
CREATE TABLE IF NOT EXISTS x_memory(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  layer TEXT, category TEXT, source TEXT, created_at TEXT, score REAL);
CREATE TABLE IF NOT EXISTS x_doc(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  section TEXT, slug TEXT, words INTEGER, tags TEXT);
CREATE TABLE IF NOT EXISTS x_log(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  unit TEXT, level TEXT, ts INTEGER, msg_hash TEXT);
CREATE TABLE IF NOT EXISTS x_task(
  oid TEXT PRIMARY KEY REFERENCES obj(oid) ON DELETE CASCADE,
  pt_id TEXT, status TEXT, owner TEXT);

-- ④ 分块 + 双 FTS
CREATE TABLE IF NOT EXISTS chunk(
  cid INTEGER PRIMARY KEY,
  oid TEXT NOT NULL REFERENCES obj(oid) ON DELETE CASCADE,
  seq INTEGER NOT NULL, text TEXT NOT NULL, chars INTEGER
);
CREATE INDEX IF NOT EXISTS chunk_oid ON chunk(oid, seq);
-- ≥3 字：trigram，子串精确，索引约 3~4×（当前 memindex 实测 3.6×）
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_fts USING fts5(
  text, content='chunk', content_rowid='cid', tokenize='trigram');
-- 1~2 字：按字切分的 unicode61，短语查询 '"记 忆"' 可命中（已实测）
CREATE TABLE IF NOT EXISTS chunk_uni(
  cid INTEGER PRIMARY KEY, oid TEXT, text_uni TEXT);   -- 只覆盖 title+summary+uri+短文档
CREATE VIRTUAL TABLE IF NOT EXISTS chunk_ftsu USING fts5(
  text_uni, content='chunk_uni', content_rowid='cid', tokenize='unicode61');

-- ⑤ 关系（技能↔会话↔项目↔记忆）
CREATE TABLE IF NOT EXISTS edge(
  src_oid TEXT, dst_oid TEXT, rel TEXT, weight REAL DEFAULT 1, meta TEXT,
  PRIMARY KEY(src_oid, dst_oid, rel));

-- ⑥ 扫描账本（每次 run 一行，异常可回溯）
CREATE TABLE IF NOT EXISTS scan_run(
  run_id INTEGER PRIMARY KEY AUTOINCREMENT, src_id TEXT, mode TEXT,
  started_at TEXT, finished_at TEXT, status TEXT,
  added INTEGER DEFAULT 0, updated INTEGER DEFAULT 0,
  deleted INTEGER DEFAULT 0, skipped INTEGER DEFAULT 0,
  errors INTEGER DEFAULT 0, ms INTEGER, note TEXT);

-- ⑦ 别名 / 去重（同一 realpath 被多源收录）
CREATE TABLE IF NOT EXISTS alias(
  alias TEXT, oid TEXT, kind TEXT, PRIMARY KEY(alias, oid));

-- ⑧ 元信息
CREATE TABLE IF NOT EXISTS meta(k TEXT PRIMARY KEY, v TEXT);  -- schema_version / built_at
```

**迁移兼容**：`schema_version` 单调递增；`migrate.py` 每步幂等（`IF NOT EXISTS` + 版本守卫），旧库先 `cp` 时间戳备份。

---

## 5. 适配器矩阵

每个适配器实现同一接口：

```python
class Adapter:
    src_id: str
    domain: str
    requires: tuple[str, ...]          # 'http:8420' / 'journald' / 'dir:~/.claude'
    def probe(self) -> dict            # 可用性：ok / unavailable(原因) / error
    def scan(self, since: int) -> Iterator[Record]   # 只读，可中断
    def normalize(self, rec) -> (obj_row, ext_row, chunks)
```

| 适配器 | 源 | 产出 | 增量键 | 备注 |
|---|---|---|---|---|
| `agents` | `profiles.PROFILES`(21) + `custom_agents`(9) + 进程探测 | `x_agent` | agent_id | 三态合并，`meta.states` 保留语义差异 |
| `skills` | 15+ 目录 3,054 个 `SKILL.md` | `x_skill` | path+mtime | 只读 frontmatter，正文按需分块 |
| `projects` | `localprojects.ROOTS` | `x_project` | path+git HEAD | 排除派生 worktree 沿用现有逻辑 |
| `sessions` | `sessions_store` 10 个适配器，1,834 jsonl | `x_session` | path+mtime | 只读头部 256KB 提标题；正文分块入库 |
| `memory` | TDAI HTTP + `memindex` 旧数据 | `x_memory` | TDAI id | TDAI 不可用 → 标 unavailable，本库自有分块仍可查 |
| `kb` | XERJ + `wiki/`+`agent-knowledge/`+`会话备份/` 7,949 篇 | `x_doc` | path+mtime | XERJ 缺席时纯本地 FTS 兜底 |
| `logs` | journald（可选）+ hub 运行日志 | `x_log` | unit+ts+msg_hash | 只存元数据与首行，不搬全文 |
| `biz` | `agents.db` 的 tasks/dataset/ref_cards/experiences/skill_assets | `x_task` 等 | 表主键+updated_at | 只读连接（`mode=ro`） |
| `term` | `term_recordings` 元数据 | `x_session(meta)` | session_id | 只索引会话范围，正文不搬 |

**命名空间完整性保护**：`scan()` 返回 `complete=True` 才允许把本 `src_id` 下未见到的对象标 `missing`。根目录不存在时 `complete=False` —— 防止 `~/.claude` 临时不可读就把 500 条对象全删。

---

## 6. 统一接口

### 6.1 契约（所有入口同形）

```json
{
  "ok": true,
  "count": 12,
  "items": [{
    "oid": "a1b2c3d4e5f60718",
    "domain": "session", "type": "session",
    "title": "统一数据层方案讨论",
    "snippet": "…把所有数据汇总到一个库…",
    "score": 0.83,
    "uri": "file:///home/gztxt/.codebuddy/projects/.../x.jsonl",
    "source": "sessions.codebuddy",
    "freshness": {"mtime": 1761380000, "age_s": 3600}
  }],
  "backends": [{"name": "catalog.chunk_fts", "ok": true, "hits": 12, "ms": 18.2}],
  "ms": 24.1,
  "degraded": null
}
```

### 6.2 三个工具（MCP）

| 工具 | 参数 | 用途 |
|---|---|---|
| `hub_query` | `q, domains="", types="", k=10, since="", deep=false` | 唯一检索入口，跨域一次取齐 |
| `hub_get` | `oid` 或 `domain+native_key` | 取对象详情 + 邻接（`edge`）+ 原文打开方式 |
| `hub_status` | `domain=""` | 各源 `last_ok_at`、滞后秒数、对象数、扫描失败原因 |

旧工具（`hub_memory_search` / `hub_kb_search` / `hub_skill_list` / `hub_list_agents` …）保留为 wrapper，内部转 `hub_query(domains=...)`；一个版本后按调用量决定是否下线。

### 6.3 非 MCP 工具链

```bash
bash agenthub/scripts/hubq.sh "记忆 存储" --domains memory,doc --k 8
```

### 6.4 自动调用怎么落地

- `intake.sh` / `hub_intake` 的第一跳改为 `hub_query`（现在四查各走各的入口，实测快路 0.9s，改后可压到一次往返）。
- `AGENTS.md` 接单前置一节改成「先 `hub_query`，结果不够再点域名深挖」。
- 保留 `deep=false` 默认：不预热、不拉全库（沿用现有按需口径）。

---

## 7. 查询路由与排序（修准确性）

### 7.1 长度路由（关键修复）

| 查询长度 | 索引 | 说明 |
|---|---|---|
| ≥3 字 | `chunk_fts`（trigram） | 子串精确，bm25 排序 |
| 1~2 字 | `chunk_ftsu`（unicode61 按字切分） | 构造 `"记 忆"` 短语查询；实测可命中 |
| 混合/英文缩写 | 两路都查，RRF(k=60) 融合 | 英文短词（AI/Go）走 uni 路 |
| 2 字但仍想查正文 | `deep=true` | 走 LIKE 限流扫描（500ms 超时，显式标 `degraded:"deep_like"`） |

`chunk_ftsu` 只覆盖 `title + summary + uri + ≤8KB 的短文档`，预估 <150MB，避免全文再翻一倍索引。

### 7.2 排序

```
final = 0.55*norm(bm25) + 0.20*title_hit + 0.15*authority + 0.10*recency
```
- `authority` 取 `src.authority`（同一事实多源时权威源置顶）；
- `recency = exp(-age_days/180)`；
- 同 `hash` 或同 `realpath` 只留最高分（`alias` 记录其余来源）；
- 域多样性：未指定 `domains` 时单域最多取 `k/2` 条，防一个域刷屏。

---

## 8. 性能设计

### 8.1 写入（扫描）

- 单适配器单事务，批 2,000 行 `executemany`；`BEGIN IMMEDIATE` 避免升级锁。
- 增量三判：`(native_key 存在?) → (mtime,size 相同?) → (hash 相同?)`，任一不同才读正文重分块。
- 分块参数：目标 1,200 字 / 重叠 150 字（与现 `memindex` 对齐，保证迁移后命中可比）。
- 大批量后执行 `INSERT INTO chunk_fts(chunk_fts) VALUES('optimize')`。
- 扫描与查询**同进程但不同连接**：查询连接 `mode=ro`，互不阻塞。

### 8.2 PRAGMA 基线

```
cache_size=-262144 (256MB)   mmap_size=268435456   temp_store=MEMORY
busy_timeout=5000            synchronous=NORMAL    wal_autocheckpoint=2000
```

### 8.3 目标值（写成 bench 断言，不达标不算完成）

| 指标 | 目标 | 测法 |
|---|---|---|
| 查询 p95（≥3 字，40 万块） | ≤ 80ms | `scripts/bench_catalog.py --n 200` |
| 查询 p95（1~2 字） | ≤ 150ms | 同上，`q=记忆` |
| 增量扫描 1,000 文件 | ≤ 2s | 连跑两次取第二次 |
| 全量会话扫描（1,834 jsonl） | ≤ 120s | 单次 |
| catalog.db 体积 | ≤ 4.5GB | 迁移后实测（memindex 3.86GB 为基线） |
| 查询缓存命中 | 同 query 二次 ≤ 5ms | 复用 `tdai_client` 的 LRU+TTL 模式 |

### 8.4 磁盘水位

`/fs` 当前 **96% 已用（剩 146G）**。catalog 预估 4.0~4.3GB；迁移期新旧并存需额外 3.9GB。**P1 迁移前先清理 `agents.db.bak-20261021_230912-preseed`（642MB）+ memindex 迁完即删**，净增 ≈ 0。

---

## 9. 数据质量与归档

### 9.1 质量闸门（`hub catalog verify`，每日 03:0x）

| 检查 | 判据 | 失败处置 |
|---|---|---|
| FTS 完整性 | `INSERT INTO chunk_fts(chunk_fts) VALUES('integrity-check')` 不抛错 | 重建该索引 |
| 孤儿块 | `chunk LEFT JOIN obj WHERE obj.oid IS NULL` = 0 | 级联删除 |
| 悬空扩展 | `x_*` 有行但 `obj` 无 | 同上 |
| 哈希漂移 | 抽样 1% 重算 `hash`，不一致率 | 标记 `stale` 并重扫 |
| 重复对象 | `GROUP BY hash HAVING count>1` 占比 <2% | 合并进 `alias` |
| 滞后 | `now - src.last_ok_at` 超阈值（session 15min / doc 24h / skills 6h） | `hub_status` 标红 |

### 9.2 归档（"汇总归档"落地）

- **会话**：`mtime` > 180 天的 jsonl，正文转 `archive/YYYY/MM/*.zst`，`obj.uri` 指向压缩包内偏移，`status='archived'`，标题+摘要在库内保留 → 检索仍命中，原文按需解压。
- **日志**：只保留 30 天元数据（与 `retention.py` 同口径），更早的聚合为 `x_log` 日计数。
- **不归档**：`agents.db` 业务表、TDAI、XERJ（各自有生命周期）。

---

## 10. 可移植（"装到任何 Linux / 其他系统"）

### 10.1 当前硬编码清单（实测）

| 位置 | 硬编码 | 处置 |
|---|---|---|
| `src/localprojects.py:59` | `LOCALPROJECT_ROOTS=/home/gztxt,/fs/1000/ftp/技术文档,/vol1/@apphome` | 移入配置，默认 `$HOME` + 自动探测工作区 |
| `src/skill.py` | `SKILL_DIRS` / `ARCHIVE_ROOTS` 绝对路径 | 已是 env 可覆盖，改为「默认按 `$HOME` 推导 + 配置覆盖」 |
| `src/hublog.py` | journald | 抽象为 `LogSource`，无 systemd 时降级读文件 |
| `src/config.py` | 数据目录 | 统一为 `AGENTHUB_HOME`（默认 `~/.agenthub`） |
| 部署 | `systemd --user` unit | 生成器：systemd / launchd / Windows 计划任务 |

### 10.2 配置

`~/.agenthub/hub.toml`：

```toml
[paths]
data_dir = "~/.agenthub/data"
log_dir  = "~/.agenthub/logs"
workspace = "~/work"

[services]
tdai = "http://127.0.0.1:8420"   # 空 = 禁用该适配器
xerj = "http://127.0.0.1:9300"

[scan]
sessions_interval_s = 900
docs_interval_s     = 3600
max_chunk_chars     = 1200
```

### 10.3 安装与自检

```bash
uv tool install agenthub          # 或 pipx install
agenthub init --workspace ~/work  # 建库 + 探测源 + 首次扫描
agenthub doctor                   # 逐源打印 ok/unavailable(原因)，非 0 退出码=有源挂了
agenthub serve --port 3102
agenthub service install          # 按平台生成 systemd/launchd/计划任务
```

`doctor` 输出即能力矩阵：缺 docker → `memory.tdai: unavailable(no docker)`，但 `doc/skill/session` 仍可用。**空库也能起服务**。

### 10.4 打包（可选，P3 之后）

- 离线优先：PyInstaller 单文件（捆绑依赖，无网络也能跑）。
- 跨平台构建：uvbox（Go 引导器，一次构建 Linux/macOS/Windows，但首跑需联网）。
- 二选一，先做 PyInstaller 保离线；CI 里加 `ubuntu-latest` + `macos-latest` 双跑 hermetic 测试。

---

## 11. 分期实施

> 每期独立可交付、可回滚。**任何一期都不改现有 162 个路由的行为**；新能力挂 `/api/catalog/*`，用 `HUB_CATALOG=1` 开关灰度。

### P0 · 立骨（骨架 + 3 域） — ✅ 已实施（2026-10-25，提交 `9625efb`）

> ✅ **P0 已完成**（隔离 worktree `wt/cat1025`，rebase 到 master `66bcc49` 之上）。
> 交付物全部落地：`src/catalog/`（schema/store/adapter/query + 三适配器）、
> `src/api_catalog.py`、`scripts/hubq.sh`、`scripts/hub-catalog-scan.sh`、
> `scripts/shadow-catalog-instance.sh`、`tests/test_catalog.py`（50 例）。
> REST/MCP 注册那一刀**已补上**（PT-20261025-11 落地后无冲突，rebase 干净）。
>
> **实测结论（与本方案 §7.1 的预判一致，但暴露一个方案没写到的缺陷）**：
>
> | 项 | 实测 |
> |---|---|
> | 对象数 | agent 27 / service 6 / skill 376 / project 48 = **457** |
> | 影子查询延迟 | 1.1~2.5ms（4 域全扫） |
> | 2 字中文 | `chunk_ftsu`（unicode61 按字）命中，正常 |
> | 误删 | 0（`complete=False` 闸门生效） |
> | 开关关闭 | :3198 对照组 `/api/catalog/*` 全 404 |
> | L0 | 3118 例全绿（新增 50 例），收集器对账 3193=3193 |
>
> 🚨 **方案漏掉的一个真缺陷（已修）**：**「哈希相同」不等于「索引完整」**。
> 无正文对象（agent/project 名录）早期只写 `obj` 行、不写 `chunk`；此后每轮增量
> 都因哈希未变而跳过它们 ⇒ 它们**永远没有 chunk ⇒ FTS 永远搜不到**。
> 实测 `"claude"` 命中 30 行**全在 skill 域**，agent/project 全部缺席——
> 统一查询的价值恰好丢在「跨域」上。修法两条：
> ① `existing_states()` 对无 chunk 的对象把 hash 置 `NULL` ⇒ 增量判重必然失败
> 一次 ⇒ 强制重扫并补写 chunk（此后恢复跳过）；
> ② `upsert_objects()` 跳过路径核对 chunk 存在性，缺失即就地自愈（`healed` 计数）。
> **判据沉淀：任何「哈希相同就跳过」的增量机制，都必须额外核对产物是否完整。**
>
> ⚠ **其他 5 处实测缺陷**（均已修，见提交信息）：ISO 时间戳当 `int()` 用 /
> 「已跳过」计数恒 0 / 6 个 service 被误标 missing / 无 mtime 名录无指纹 /
> LIKE 未转义（`%_` 通配全表）/ `status` 的域清单按源而非按实际对象统计。

**交付**
1. `src/catalog/` 包：`schema.py`（DDL+迁移）、`store.py`（连接/事务/批量）、`adapter.py`（基类+注册表）、`query.py`（路由+排序）。
2. 适配器：`agents` / `skills` / `projects`（数据量小、风险低，约 3,100 对象）。
3. REST `/api/catalog/status|query|get`；MCP `hub_query/hub_get/hub_status`（**只加不改**）。
4. `scripts/hubq.sh`；`scripts/hub-catalog-scan.sh`（可 cron、可手动）。

**验收**
- `hub_query("技能")` 跨 3 域返回，`backends` 逐路有 ms；
- 关掉 `HUB_CATALOG` 后行为与 v0.13.160 完全一致；
- L0 全绿（≥2,989 例），新增用例 ≥25。

**回滚**：删 `catalog.db` + 关开关，无副作用（业务库未动）。

### P1 · 全量接入 + memindex 收编

**交付**
1. 适配器：`sessions` / `docs` / `memory` / `logs` / `biz` / `term`。
2. `scripts/catalog_migrate_memindex.py`：`ATTACH memindex.db` 分批导入 → 计算 `oid` → 建双 FTS；导入后**双跑对账**（同一批 30 条查询，top10 重合率 ≥95%）。
3. 1~2 字查询路由上线（`chunk_ftsu`）。
4. 扫描调度：复用 `cronjobs` 框架，`session 15min / skills 6h / docs 24h`。

**验收**
- 对账报告 ≥95%，差异逐条给出原因；
- p95 达标（§8.3 前两行）；
- `memindex.db` 转只读备份，`HUB_CATALOG=1` 为默认。

**回滚**：`HUB_CATALOG=0` 回旧链路，`memindex.db` 未删。

### P2 · 质量 + 归档

**交付**：去重/别名/权威合并；tombstone；`verify` 每日任务接进 `retention.py` 心跳；归档器（会话 >180 天 → zst）；质量面板进 `/health`。

**验收**：重复率 <2%、孤儿 0、抽样漂移 <0.5%、滞后 SLO 达标。

### P3 · 可移植

**交付**：配置外置（§10.2）；`LogSource` 抽象；`doctor`；`service install` 三平台模板；CI 双平台；`agenthub init` 空目录冷启动。

**验收**：在干净容器（无 TDAI/XERJ/docker）里 `init → doctor → serve → query` 四步通过，缺的源明确写 `unavailable` 而非 500。

### P4 · 收口

`datastore.py` 改为 catalog 门面（保留 `describe()` 形状）；旧 MCP 工具转 wrapper；`AGENTS.md` 接单前置改成一次 `hub_query`；文档更新。

**总量估算**：P0 约 1 天，P1 约 2 天（含 4GB 迁移与对账），P2 1 天，P3 1.5 天，P4 0.5 天。

---

## 12. 风险与对策

| 风险 | 触发条件 | 对策 |
|---|---|---|
| 单一库损坏 = 全挂 | 磁盘满 / 写入中断 | WAL + 每日 `VACUUM INTO` 快照；`catalog.db` 可 100% 重建（不是权威） |
| 迁移期双库写不一致 | 迁移中途重启 | 迁移脚本幂等 + `scan_run` 断点；`meta.migrate_state` 记阶段 |
| 扫描把生产拖慢 | 首扫 1,834 jsonl | 限速：单次 ≤200MB/s，`PRAGMA cache_size` 上限，扫描线程低优先级 |
| 误删对象 | 根目录临时不可读 | `complete=False` 不删（§5） |
| 隐私泄露 | 会话/日志含凭据 | 分块前复用 `sanitize`（`memindex` 已有）；`hub_get` 返回前二次脱敏 |
| 重启杀会话 | 部署时 `restart` | **只用** `scripts/hub-restart.sh`；开发验证用影子实例（`PORT=3199 DATA_DIR=/tmp/cat`），不碰生产 |
| 磁盘 96% | 迁移期新旧并存 | 先删 642MB 旧备份，迁完删 memindex（§8.4） |

---

## 13. 参考资料（2026-10-25 检索）

1. **FTS5 trigram 的边界与代价**（中文场景实践）：trigram 索引约为原文 3~4×，**<3 字查询静默返空**，需显式 fallback；`detail='none'` 不可用（短语匹配依赖位置信息）。来源：dev.to《Rebuilding a Video Metadata Service…Without Losing CJK Search》、ipipp《SQLite FTS5 建表到中文分词》。
2. **双 tokenizer 分列策略**：同一接口下按列选 tokenizer（短英文 unicode61 / 中文长文 trigram），查询侧各自 sanitize。来源：Starcat 设计文档《关键词与全文检索设计》。
3. **联邦编目（Federated index）**：中央只做索引与查询，源系统保留权威；批量爬取 + 事件增量混合；幂等键用 canonical qualifiedName。来源：beefed.ai《Integrating Your Data Catalog…》、systemoverflow《Metadata Ingestion and the Universal Catalog》。
4. **编目失败模式表**（F1 陈旧 / F3 索引漂移 / F4 高延迟 / F5 隐私泄露）：直接映射到 §9.1 检查项。来源：thedataops《Searchable catalog》。
5. **可移植打包**：PyInstaller 全离线 vs uvbox 跨平台引导（首跑联网）。来源：AmadeusITGroup/uvbox README 对比表。

---

## 14. 立即动作（确认后按序执行）

> ✅ **已执行**（2026-10-25）：worktree `wt/cat1025` 建好 → P0 落地 → 提交 `9625efb`
> （rebase 到 master `66bcc49` 之上）。**下一步是合并 + 重启 + 生产扫描**，见文末「P0 之后」。

```bash
# 1) 登记台账（本方案状态 ⏸ 待批）
#    在 PENDING-TASKS.md 追加 PT-20261025-11

# 2) 建隔离 worktree（主 checkout 不动）
bash /fs/1000/ftp/技术文档/scripts/worktree-new.sh agenthub cat1025

# 3) P0 第一刀：schema + 3 个只读适配器 + 影子实例验证
#    影子实例口径（不碰生产、不重启）：
env -i HOME=$HOME PATH=/usr/bin:/bin \
  PORT=3199 DATA_DIR=/tmp/cat-data HUB_CATALOG=1 \
  venv/bin/python -m src.main
```

### P0 之后（下一步，按序）

1. **合并**：`git checkout master && git merge --no-ff wt/cat1025`（主 checkout 由
   集成者合并；施工会话只提交隔离分支）。
2. **台账**：在 `PENDING-TASKS.md` 追加 `PT-20261025-12`（P0 实施），状态
   `◐待重启`；把方案里原拟的 `PT-20261025-11` 编号让给本项（避免同号两义）。
3. **生产扫描**：合并+重启后跑一次 `scripts/hub-catalog-scan.sh` 建生产
   `data/catalog.db`（影子那份在 `work/cat-p0-data/`，路径不同，不能直接用）。
4. **灰度**：`HUB_CATALOG=1` 进 `agenthub.service` 的环境文件（**只加不改**，
   默认关 ⇒ 不重启即行为不变），观察一周后再考虑转默认。
5. **P1**：接 `sessions` / `docs` / `memory` / `logs` 四域 + memindex 收编
   （对账 top10 重合 ≥95%）。
