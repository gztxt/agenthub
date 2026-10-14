# P1/P2 落地记录：AMR 格式的采纳与边界（2026-10-13）

> **新建文件登记**：本文件为**新建**，无需备份。建立原因：用户授权执行评估的 P1/P2。
> 本文件记录 P1（采纳项）与 P2（格式消费源）的**实际落地形态**，以及**为什么有些项只能
> 落成约定/接口而不能落成运行代码** —— 后者是本次最重要的事实，不写下来下一个人还会重推一遍。
>
> 关联：评估全文 `docs/AGENT-MEMORY-REPO-评估-20261013.md`；代码 `src/memfed.py`（AMR 源）。

---

## 0. 一句话

P1 的三项里，**两项的约定本机早已存在**（不该重复发明），第三项（Dreaming）受**权威库只读**
的硬约束而只能在本地 L1 落笔；P2 落成了一个**真正可用**的最小增量：AMR 格式的只读消费源。

---

## 1. P1-1 溯源元数据 `[source: url; added: 日期]` —— **已在位**

**核查事实**：`memories` 表结构里**已有** `source TEXT`（来源 agent / manual）与
`session_id TEXT` 两列（`data/agents.db` `.schema memories`）；写入函数 `db.add_memory(...)`
已接受并落库这两个字段。

**结论**：**不新增 schema、不加迁移**。AMR 的 `[source: ...; added: ...]` 与既有两列同义
（`source`≈来源、`session_id`≈会话锚点），再开一列就是本仓最忌讳的「第二份权威副本」。

**唯一真实缺口**（记录，未改）：本地 L1 **长期只有 4 行、无任何 agent 自动写**
（详见评估 §3 附带缺陷）⇒ 该列目前基本没被填过。缺的是**写入方**，不是**字段**。

---

## 2. P1-2 `[[path]]` 交叉链接 + 短 MEMORY.md 入口 —— **已在位**

**核查事实**：`/fs/1000/ftp/技术文档/MEMORY.md`（40KB）**已在用** `[[path]]` 语法，实测
7 处：`[[NAS-飞牛网络]]`、`[[Hermes-定位与四方协同]]`、`[[OpenClaw下线后三方协同架构]]`、
`[[Grok-Build-CLI]]`、`[[Hermes-config.yaml-省token调优]]` 等。且该文件**已被 `workspace_files`
联邦源收录**（`memfed._RG_TARGETS["workspace_files"]` 的第一个根就是它）。

**结论**：**不新建入口文件、不建新源**。AMR 的"MEMORY.md 作入口 + `[[path]]` 交叉链接"
在本机已有等价物，且它已经进得了检索。

**唯一差异**（记录）：本机 MEMORY.md 是**长文**（40KB），AMR 规范建议"keep it short"
（只放每次会话都要的东西 + 指向其余）。这是**风格差异而非能力缺口**；若将来要收窄，
那是内容编辑问题，不是代码问题。

---

## 3. P1-3 Dreaming（周期整理：合并重复/删过期/对照来源解冲突）—— **受阻，仅本地可落笔**

**核查事实（关键约束）**：`src/tdai_client.py` 里**对 TDAI 的写调用数 = 0** ——
本模块对权威库**严格只读**（对外能力全是 `search_memories`/`search_conversations`/
`core_read`/`scenario_ls`/`stats`/`skill_list`/`reachable`，无任何 add/update/write）。

**这意味着**：本机记忆的**权威副本在 TDAI（L1 376 / L0 10895）**，而 hub **写不动它** ⇒
真正的 "Dreaming"（在权威库上合并/删除）**hub 做不了**，必须由有权写 TDAI 的一方做
（本机是 pi 的 tdai 记忆插件，见评估 §3）。

**同时**：TDAI 自己的整理基础设施**在位却空转** —— `timerScanner` 扫了 **5701** 次，
**`tasksEnqueued: 0`**，`pipelineWorker` 累计只消费过 **4** 个任务。**这正是 Dreaming 该坐的位点**。

**结论**：**不在 hub 侧伪造一个 Dreaming**（那会造出第二份权威副本或空转的假机制）。
可落笔的只有**本地 L1**（hub 唯一写得动的地方），但本地 L1 只有 4 行 ⇒ 现在做"整理 4 行"
毫无意义。

**建议路径（待用户裁定，未实施）**：
1. 先解决**写入方**（本地 L1 自动化，或接上 TDAI 写权限）；
2. 再在 `timerScanner`/`pipelineWorker` 那一位点接 Dreaming 职责（合并重复 → 删过期 →
   对照来源解冲突，语义直接照抄 AMR 规范）；
3. 顺序不可颠倒 —— 没有存量就先做整理，等于给空库写清洁工。

---

## 4. P2 AMR 格式消费源 —— **已落地（`amr_memory`）**

### 做了什么
新增联邦源 `amr_memory`（`src/memfed.py`）：
- 根路径 `AMR_ROOT`：env `AMR_MEMORY_ROOT` 优先，默认 `/fs/1000/ftp/技术文档/memory-repo`。
- **走 env 而非写死**：AMR 是**外部规范**，本机此刻**尚无**该形态的仓
  （实测无 `.git` 记忆仓）⇒ 根是"约定位点 + 可覆盖"，不是既成事实。
- 走既有 `rg_text` 管线，**只读**，无需改融合逻辑（注册表加一条即可，符合模块的开闭原则）。
- 权重 0.7（与 `workspace_files` 同级：都是成文的记忆文件层）。
- 前端同步：`templates/index.html` 加 option、`04-terminal-ws.js` 加 `SRC_ABBR` 缩写 `AM`、
  重建 `static/hub.js`；`test_sys_pages_layout` 的契约计数 11→**12**。

### 为什么这样设计（而不是"改用 AMR 格式"）
AMR 的检索方式是**纯 grep + 链接跳转、无索引层**（SPEC 全文 1940 字节，见评估 §1）。
本机已有的 FTS + 语义 + RRF **正是它缺的那一半**。所以正解是**把它当只读语料消费**，
而不是把本机降级成 grep。这也回应了评估 §1 的定性：**它是格式契约，不是检索系统**。

### 验收（测试）
新增 `tests/test_memfed_amr.py`（L0 hermetic，4 例）钉住：
1. 源在注册表、enabled、带权重（否则融合层不带上它）；
2. **真仓存在时**按 rg 只读召回，条目带 `source=amr_memory`，且 `[source:]` 溯源串随内容召回；
3. **根不存在时 `ok=False` 并带 error**（禁静默返空 —— 本仓最忌讳的失效形态）；
4. 条目形状能被 `_rrf_fuse` 消费（带 `source` 与 `id`，去重键依赖二者）。

> 夹具写在 `tmp`、不读真盘、不起服务、不许 SKIP —— 符合 `tests/README.md` 的 L0 口径。
> **为什么必须靠夹具**：真盘上没有 AMR 样本可断言，"能消费该格式"这件事只能由夹具证明。

---

## 5. 本次改动清单

| 文件 | 改动 | 性质 |
|---|---|---|
| `src/memfed.py` | 加 `AMR_ROOT` + `amr_memory` 注册表条目 + `_RG_TARGETS` 条目 | 新增只读源 |
| `templates/index.html` | 加 `amr_memory` option（计数 11→12） | 前端同源契约 |
| `static/hub/04-terminal-ws.js` | `SRC_ABBR` 加 `amr_memory: 'AM'` | 前端同源契约 |
| `static/hub.js` | 重建（改分片必须重建） | 构建产物 |
| `tests/test_memfed_amr.py` | 新增 4 例 L0 闸门 | 新测试 |
| `tests/test_sys_pages_layout.py` | 计数 11→12 | 契约随改动 |
| `tests/test_term_scroll_sensitivity.py` | 版本钉跟上 0.13.103 | 仓库既有约定 |
| `src/main.py` / `CHANGELOG.md` | 版本 0.13.103 + 条目 | 版本管理 |

测试：`./scripts/run_tests.sh` 全量 **1587 passed**（L0+L1 收集器对账一致）。

---

## 6. 仍未落地 / 待用户裁定

1. **crontab 安装**（P0-3b 的 `scripts/memindex_watchdog.sh`）：脚本已写好并实测通过
   （正确识别 11 天陈旧、退出码 3、只告警不重建），但**安装被安全分类器拦为「未授权持久化」**
   —— 装定时任务是系统级持久化，应由用户确认。**未绕过**。
2. **hub `jobs` 引擎修或删**：实测是坏的（白名单只允许 `echo,date,…`；
   `turbovec-rebuild` 指向的 `/home/gztxt/agent-hub/scripts/rebuild_turbovec.sh` 路径不存在；
   `profile_events` 里一条 `cron_run` 都没有）。本次**未动**（破坏性且超 P0/P1/P2 范围）。
3. **P1-3 Dreaming**：需先解决写入方 / TDAI 写权限（见 §3）。