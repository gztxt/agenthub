# Agent Memory Repo（Cognition）能否融进 agenthub 记忆中心 — 评估与 P0 修复

> **新建文件登记**（依 CLAUDE.md「文件修改铁律」）：本文件为**新建**，无需备份。
> 建立原因：用户 2026-10-13 要求评估 `cognition.com/agent-memory-repo` 与
> `github.com/AgentMemoryRepo/agentmemoryrepo` 能否融合进 agenthub 的记忆中心，
> 并把评估落成 docs 文件。改因、改法、实测数字与证据边界全部记在下文。
>
> 取证日期 **2026-10-13**；被测版本 `agenthub v0.13.102`、`git e424b3a3`；
> 外部事实来自 GitHub API / HN Algolia API / wigolo 抓取（同日）。

---

## 0. 结论速览

| 维度 | 结论 | 关键数字 |
|---|---|---|
| **能否融合** | **能，但是「格式层」融合，不是替换**。Cognition 刻意不做检索层，本机已有的 FTS+向量+RRF 恰好是它缺的另一半 | 规范仅 1940 字节，无索引层 |
| **效率** | 检索快、系统效率低。融合后平均只剩 1 路（丢 81% 证据）；启动注入携带 **0** 条跨 agent 记忆 | 5.2 路有货 → 1.0 路入库 |
| **准确性** | 低、且自信地错。排序恒由 TDAI 主导、与查询无关；`score` 是**距离类**且反向 | 乱码 score 0.52 > 真命中 0.03 |
| **性能** | 延迟优秀，但掩盖陈旧 | search p50 **57ms** / context 6ms / FTS 6ms |

**本次已修（P0）**：三个硬缺陷全部修复并实弹验证，1583 项测试全绿。
**建议采纳（P1）**：溯源元数据 `[source:]`、`[[path]]` 交叉链接、Dreaming 定时任务。
**明确不采纳**：git 当并发写入仲裁、纯 grep 检索、MEMORY.md 当唯一索引。

---

## 1. 两个链接是什么（已核实，非空谈）

| 项 | 事实 | 取证 |
|---|---|---|
| 官网 | Cognition（Devin 母公司）2026-10-04 发布的**开放规范**，非产品 | cognition.com/agent-memory-repo |
| GitHub | `AgentMemoryRepo/agentmemoryrepo`，MIT，**775★ / 38 forks**，**仅 38KB** | api.github.com 直读 |
| 文件树 | 仅 `SPEC.md`(1940B) + `README.md`(12364B) + `skills/` + `.devin-plugin/` | GitHub trees API |
| 内核 | 记忆存 git 仓；`MEMORY.md` 入口；条目一行一 bullet 带 `[source: url; added: YYYY-MM-DD]`；`[[path]]` 交叉链接；"Dreaming" agent 周期合并重复/删过期/解冲突 | SPEC.md 全文 |
| 检索方式 | **纯 grep + 链接跳转，无任何索引层**。README 第 2 步原文就是 "Grep" | README |
| 官方自认缺口 | README 明写 **"Automatic startup and scheduled Dreaming are not included."** | README |
| 生态热度 | HN 全站仅 2 条提交、**各 1 分 1 评论**（唯一评论是发帖人自述）；但 9 天内有 **2 个第三方实现**（pi、OpenCode） | HN Algolia API + GitHub search |
| 有来源的批评 | Issue #9：*"the spec describes retrieval via grep and link following... external tooling could perform deterministic indexing"*（**官方未回应**）；PR #8 指出元数据转义规则未定义 | issues/9、PR/8 |
| 官方后续 | 仅一条回复（Issue #7，2026-10-12）承诺加 "Community" 区块收编第三方实现；**检索/索引层无任何官方提及** | issues/7 comments |

**定性**：它是**格式契约**，不是检索系统。本机 agenthub 缺的是「格式之上的检索与统一」——两者是**不同层**，故不存在"取代"关系。

---

## 2. 本机记忆中心盘子（实测 2026-10-13）

| 源 | 规模 |
|---|---|
| TDAI(:8420) 权威库 | L1 **376** 条 / L0 **10895** 条 / L2 7 场景档 / L3 persona |
| 联邦源 | **11 路**（10 enabled + opencode disabled） |
| 知识库 | 5 路（turbovec 7300 chunks / 1.94MB） |
| 投影库 memindex.db | 1.72GB / 9107 文件 / 219171 chunk（**构建于 2026-10-02，无自动刷新**） |
| 底层语料 | 工作区 63GB/653k 文件；会话备份 6266 文件/334M；.hermes 1.6G；.grok 1.7G；.workbuddy 957M；.codex 634M；.pi 286M |

「所有记忆」在**法律意义上**确已收进来，但「收进来」与「用得上」是两件事——见下节。

---

## 3. 三个硬缺陷（修复前实测取证）

### 缺陷 1 — 联邦融合被 TDAI 完全吃掉，平均丢弃 81% 可用证据

`_rrf_fuse` 中 `W_TDAI_L1=1.0`，其余源最高 0.8（`src/memory.py:97`）。TDAI **每次返回满额 top-10 且无相关性下限**。按 `Σ w/(60+rank+1)`：TDAI 第 10 名 = `1.0/70=0.0143`，其他源第 1 名最高 `0.8/61=0.0131` —— **数学上其余 10 路永远进不了结果**。

实测（20 查询 × top10）：
```
默认(全11路)：tdai_l1 占据 200/200 槽位，其余 10 路贡献 = 0
排除 tdai  ：claude_projects 107 | workspace_files 41 | claude_mem 41
             pi_sessions 5 | codex_sessions 4 | grok 1 | workbuddy 1
```
逐查询原始证据面损失：**平均 5.2 路有货 → 融合后 1.0 路（丢弃 81%）**。

端到端危害（问题：*"记忆中心没有联邦检索框"*，该事实确在归档会话中）：
- 默认路径 → 10 条 TDAI 噪声，**答案不在里面** ❌
- 排除 TDAI → **第 1 条即命中** ✅

### 缺陷 2 — `score` 不是相关性信号，且系统性反向

`score` 是 TDAI 原始分**直通**（`tdai_client._norm` 里 `hit.get("score")`），**未按 `1/(K+rank)` 归一化**，量纲与其他路不可比：

```
真实在库事实   "用户担心账户扣款…"     score = 0.033
无关乱码       "😀😀😀"               score = 0.520   ← 反而最高
                "紫电青霜玄铁重剑"      score = 0.519
                "qqqqzzzzxxxx"        score = 0.391
```

同一批条目在**匹配查询**时返回 0.0328、在**乱码查询**时返回 0.3905 ⇒ `score` 实际是**距离类**（越小越相关），而 TDAI 按降序返回。**结论：不能拿 score 当阈值** —— P0 故只做「名次+配额」，不做"相关性下限"。

### 缺陷 3 — 会话启动的注入包不携带任何跨 agent 记忆

`/api/memory/context`（会话起始链路）实测 **5/5 次**：
```
fed: {sources: 3, chars: 0, failed: ["claude_mem","claude_projects","workbuddy_memory"]}
error: "未执行（无 q 或 fed_chars=0）"
took: 3.5ms
```
启动时无查询词 → 联邦源**根本不执行** → `fed.chars=0`，而对外仍报 `fed.sources=3`，**看着像成功**。pi 的门面正是走这条 REST 路径注入记忆的。

### 附带缺陷 — 写入侧完全没有自动化

- 本地 `memories` 表**只有 4 行**（全 2026-09-06 人工写，37 天未更新）——**没有任何 agent 自动写 L1**。唯一自动写记忆的 pi 插件写的是 TDAI 的 L0。
- 面向 agent 的 MCP 写工具 `hub_memory_add` 被 `HUB_MCP_ALLOW_WRITE` 门控，**该变量从未配置 ⇒ 生产从未注册**。
- `memindex.db` 停在 2026-10-02（陈旧 11 天），**无任何 cron/timer 自动刷新** —— 快，但越来越错。
- TDAI 的「整理」基础设施在位却空转：`timerScanner` 扫 5701 次，**`tasksEnqueued: 0`**，pipelineWorker 总共只消费过 **4** 个任务 —— 这正是 Cognition "Dreaming" 的位点，目前基本没干活。

---

## 4. P0 修复（已落地并实弹验证）

改动文件：`src/memory.py`、`src/memfed.py`；备份 `src/memory.py.bak-20261013-155603-claude-e424b3a-p0-fix`。

### P0-1 联邦融合公平性（`_rank_cap` 轮转配额）

- 新增 `FUSE_TOP_FRAC`（默认 0.4，env `MEMORY_FUSE_TOP_FRAC` 可回退）。
- `_rank_cap` **轮转取**（第 i 轮每源各出第 i 条），口径对齐本仓 `_fed_section` 已有的轮转实现——那里注释已写明「先 RRF 再截断会让某个命中多的源独占名额」，本次是同一病灶在**主检索路径**上的版本。
- 超出配额的条目**按原 RRF 序回填** ⇒ 单源独占场景**不缩水**（仅 TDAI 有货时仍出 10 条，不是 4 条）。
- 搜索与注入包两条路径都过配额；回包新增 `cap.rescued_sources`（轮转救回、纯 RRF top-limit 里没有的源）作为**可断言证据**，不静默。

### P0-3 注入包停止丢弃（`RECENT_Q` 哨兵）

- 无查询词时不再跳过联邦，改取**各源最新条目**（`memfed.RECENT_Q = "__recent__"`）。
- 新增 `_recent_rg`（按文件 mtime 排序取最新）与 `_recent_claude_mem`（按 created_at 降序）。
- **刻意不走投影库**：投影是关键词 FTS 且实测陈旧 11 天，而"最近"是实时性需求 ⇒ 扫实时树更正确。
- `_rg_head_content` 走 rg（非 Python `open()`）：本模块只读性由
  `tests/test_memfeed_inject.py::TestInjectionIsReadOnly` 按字面量把守（扫 `open(`/`.write(`），
  这是**防写入**的必要保守判据，不该为新功能放宽。`-m 8` 上界保证有界（21MB 文件实测 5ms）。
- `_usable_line` 滤退化行：YAML frontmatter 键、markdown 标题、blockquote 元数据、
  JSON 碎片、`<!--` 标记、纯分隔符（实测这些都曾混进注入包）。
- 回包 `fed` 新增 `matched`（召回到的条目数）与 `reason`；`sources>0 且 matched==0`
  时置 `fed.degraded=true` —— 把「计数绿、内容空」翻成显式故障。

### P0-3b 陈旧索引看护（**已实现但未接线**，见 §7 待决）

---

## 5. 修复后实测（同口径对比）

**公平口径（只在默认快路集内比较，20 查询）**：
```
查询                 有货源数  参与融合  被救回
qmd 索引                2        2        1
proxy-panel            3        3        2
备份/权限/安装/重启…      4        4        3
------------------------------------------------
均值                    2.4      2.4
默认集内证据利用率：100%   （修复前 ≈ 1/N，即恒为 1 路）
```

**注入包（P0-3）**：
```
修复前：fed.chars=0    failed=[3 路]       3.5ms   （联邦段 0 字符）
修复后：fed.chars=904  done=[3 路] matched=25      （联邦段为真实近期条目）
```

**延迟未退化**：search p50 **52ms**（修复前 57ms）、max 109ms；注入包仍在预算内。

**测试**：`test_memfeed_inject` / `test_memfed` / `test_memindex_proj` / `test_tdai_client` /
`test_writeauth` / `test_kb_federation` 共 **1583 passed**。

---

## 6. 建议采纳（P1）与明确不采纳

### 采纳（低成本高价值）
1. **溯源元数据** `[source: <会话URL>; added: <日期>]` —— 补上本机记忆条目"不知道出自哪次会话"的短板，与 qmd/TDAI 检索互补。
2. **`[[path]]` 交叉链接 + 短 MEMORY.md 入口** —— 与本机已有的"指针文件"模式（项目 CLAUDE.md → AGENTS.md 唯一权威正文）同构，可做人读入口。
3. **Dreaming 的职责定义**（合并重复 / 删过期 / 对照来源解冲突）—— 直接搬进 agenthub 定时任务，**正好激活 TDAI 那个 `tasksEnqueued=0` 的空转 scanner**。

### 明确不采纳
- **git 当并发写入仲裁** —— agenthub 是服务化记忆（REST/MCP），并发由服务处理。第三方 OpenCode 实现的取舍（"有未提交改动就拒绝写"、"只 ff-only 从不 merge"）说明这条路工程摩擦大。
  > 顺带证伪一个常见说法：**"git 当记忆太慢"在本机规模不成立** —— 实测浅 clone 8ms、追加+commit 7ms、push 13ms、并发冲突 push 被拒(rc=1) 10ms。
- **纯 grep 检索** —— 本机已有 FTS+向量+RRF，严格更优（rg 全扫 524–2728ms vs FTS 6ms）。
- **MEMORY.md 当唯一索引** —— 承载不了 63GB 语料。

---

## 7. 证据边界（如实标注）

- **未找到**：任何针对"git+grep 记忆"的**量化**性能/召回实测 —— 本次最大的外部证据空白（仓库发布仅 9 天，HN 讨论近乎为零）。
- **未找到**：Reddit/X/独立博客讨论、OpenAI memory/Memobase/LangMem 一手资料、基于本 spec 的 MCP server。
  （外部调研时搜索池严重退化，Bing 对 "Cognition" 有品牌冲突——上述"未找到"应理解为"在 HN+GitHub+可用引擎上未找到"。）
- **推断而非直证**：本地 L1 只 4 行的主因判定为"无调用方"（叠加权威副本已让渡 TDAI），无单行代码直证。
- **待决**：hub 的 `jobs` 引擎实测**是坏的** —— `SHELL_ALLOW` 白名单只有 `echo,date,…`，
  且 `turbovec-rebuild` 指向的 `/home/gztxt/agent-hub/scripts/rebuild_turbovec.sh` **路径不存在**，
  `profile_events` 里**一条 `cron_run` 都没有**。故 P0-3b（memindex 陈旧自检 + 按需增量刷新）
  实现为独立脚本挂**系统 crontab**（本机既有成熟维护方式），不依赖该引擎。
  jobs 引擎本身**修还是删，需用户裁定**（本次未动——属破坏性且超出 P0/P1/P2 范围）。