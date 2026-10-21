# 参考情报检索评测 · 基线（P2 ①，2026-10-21）

> 目的：把「top5 相关率」从**单评审结论**变成**可复现的数值**，此后每次改检索式/权重
> 都能回答「有没有变好、好多少」。基线本身**不是**上线门槛，是后续对照点。

## 1. 怎么测

```
venv/bin/python tests/eval_refscout.py --json work/eval.json
```

- 标注集：`tests/refscout_eval/annotations.json`（5 模块 / 13 个目标仓，version 1）。
- 标注规则：只收**能一句话说清解决同一问题**的仓库，`why` 直接引其官方 description；
  13 条在 2026-10-21 逐条打过 `/repos/{owner}/{repo}` 核验存在与描述。
- 隔离：`DATA_DIR` 指向 `work/refscout-eval-*` 临时目录，**冷缓存**（否则测的是缓存命中率），
  跑完即删；不碰生产库、不写 `ref_cards`。
- 默认 **不跑重排**（`do_rerank=False`）：词法基线可重复、零 LLM 成本。要评重排加 `--rerank`。
- 指标：
  - `hit@5`（主）＝ top5 命中的标注仓 / 该模块标注仓总数 ⇒ 衡量**找不找得到**；
  - `mrr` ＝ 第一个命中仓名次倒数 ⇒ 衡量**排得够不够前**；
  - `prec@5`（**只作下界**）＝ top5 中标注仓占比。**未标注 ≠ 不相关**，
    这个数必然偏悲观，报告里一律标「下界」。

## 2. 基线结果（2026-10-21，112~116s／5 模块，0 次 403/429）

| 模块 | 池 | hit@5 | mrr | 命中 | 未命中的标注仓（真实名次） |
|---|---:|---:|---:|---|---|
| 终端 WebSocket 与端口转发 | 37 | 0.667 | 0.333 | termpair(#3) / gotty(#5) | `tsl0922/ttyd`（池内 #8） |
| LLM 多模型网关与故障转移 | 41 | 0.000 | 0.000 | — | `Portkey-AI/gateway`(#8)、`BerriAI/litellm`(#13) |
| 服务健康探针与自动看护 | 36 | 0.500 | 1.000 | gatus(#1) | `louislam/uptime-kuma`（**未入池**） |
| 定时任务调度（cron 表达式） | 34 | 0.333 | 0.500 | apscheduler(#2) | `robfig/cron`、`celery/celery`（**均未入池**） |
| 自建轻量全文检索服务 | 42 | 0.000 | 0.000 | — | `meilisearch`、`typesense`、`quickwit`（**均未入池**） |
| **汇总** | — | **0.300** | **0.367** | 4/13 | — |

两次独立冷跑（不同临时缓存目录）汇总均为 `hit@5=0.300 / mrr=0.367 / 4/13`，
仅终端模块第 1、2 名互换 ⇒ **词法模式可重复**，差异不是随机抖动。

## 3. 两类失败必须分开处置（评测的主要产出）

`target_ranks` 已把「命中但靠后」与「根本没进候选池」分开，两者修法完全不同：

| 类型 | 数量 | 例子 | 该改什么 |
|---|---:|---|---|
| **排序**（入池但出 top5） | 3 | ttyd #8、Portkey #8、litellm #13 | 调权重：当前 `0.70*cov + 0.20*log(stars) + 0.10*lang + name_bonus`，**覆盖度占比过高**，关键词堆砌的小仓压过权威实现 |
| **召回**（未入池） | 6 | uptime-kuma、robfig/cron、celery、meilisearch、typesense、quickwit | 改检索式：多词 AND 要求**每个词都出现**，而 `robfig/cron` 的描述是「a cron library for go」、meilisearch 是「lightning-fast search engine API」——都缺 `scheduler`/`full-text` |

反例证据（同轮）：LLM 网关模块 top5 全是 `maximhq/bifrost`、`lidge-jun/opencodex`、
`ENH...GoModel`、`9router-go` 这类描述里把 gateway/proxy/failover/router 全塞满的小仓，
而 61k 星的 litellm 在 #13 —— **词面全覆盖 ≠ 功能最接近**，这正是 P0 已经踩过一次的坑的另一种形态。

## 4. 这组数不覆盖什么

1. **未跑重排**：上线默认 `allow_paid=false`，免费档方差大（40~82s）；重排对 hit@5 的影响未知，
   需要单独预算与单独报告。
2. **未标注 ≠ 不相关**：`prec@5=0.16` 只说明「top5 里确定相关的占 16%」，
   `xerj-org/xerj`、`manticoresearch` 这类未标注项未必不相关。
3. **单语料时点**：GitHub 搜索结果随时间变化，本基线绑定 2026-10-21；跨日对比前先重跑确认
   环境未变（同缓存目录二次跑可区分「代码改动」与「上游变化」）。

## 5. 下一步（有基线才敢做的两件事）

1. **排序**：降 `cov` 权重、抬 stars 先验（建议试 `0.55 / 0.30 / 0.10` 与 `0.50 / 0.35 / 0.10` 两档），
   以 `hit@5` 与 `mrr` 同时不降为准；防止再次被关键词堆砌仓霸榜。
2. **召回**：`core` 臂从「全部词元 AND」改为「至少 2 个词元 + 至少 1 个来自模块名」，
   并对 4 词以上查询自动降级为 2 词组合；目标是让 `robfig/cron`、`meilisearch` 这类
   **描述短但精准**的库能进池。

两项都改完后用同一标注集复跑，`hit@5` 应从 0.300 上升；若上升但 `prec@5` 下降，
说明只是把噪声一起捞进来了，需回退。

## 6. 优化尝试：7 组改动全部未超过基线（2026-10-21，均已回退）

基线立好后按 §5 的两条思路做了 7 轮实验（每轮全 5 模块冷跑，同标注集）：

| 轮次 | 改了什么 | hit@5 | mrr | 命中 |
|---|---|---:|---:|---|
| 基线 | — | **0.300** | **0.367** | **4/13** |
| A | 星数权重 0.20→0.35、覆盖度 0.70→0.55 | 0.300 | 0.400 | 4/13 |
| B | 加「引号短语」检索臂（多词关键词整体检索） | 0.233 | 0.340 | 3/13 |
| C | B + 提高星数权重 | 0.233 | 0.350 | 3/13 |
| D | B + 短语命中视作 cov≥0.9 | 0.167 | 0.300 | 2/13 |
| E | D + 排序改为「功能分档优先、同档比星数」 | 0.167 | 0.267 | 2/13 |
| F | E 但功能分按 0.1 一档 | 0.167 | 0.267 | 2/13 |
| G | 短语命中直接记 cov=1.0（+E 的排序） | 0.233 | 0.317 | 3/13 |

**结论（比数字更重要）**：词法层再怎么调权重/检索臂，都动不了 hit@5 —— 因为两类失败里
**召回占 6/13**（`uptime-kuma`/`robfig/cron`/`celery`/`meilisearch` 初次连候选池都进不去），
而召回取决于**查询语义**，不取决于打分公式。B/G 确实把 meilisearch 拉进了 top5
（G 轮 #3），但同时把 `timescale/pg_textsearch`、`qdrant` 这类词面更密的仓一起拉上来，
净效果仍是负的。

**处置**：7 组改动全部 `git checkout -- src/refscout.py` 回退，**一个都没上线** ——
没有指标提升的“优化”不能因为“看起来更合理”就留着（AGENTS.md：改判要留证据，
不能拿偏好当结论）。当前生产仍是基线词法口径 + 可选重排。

**下一刀应该切在哪（按证据排序）**：
1. **先量重排**：本次评测全程 `do_rerank=False`，而生产默认会走免费重排。重排对 hit@5
   的影响**至今没有数字**；用 `--rerank` 跑一轮是当前最便宜、信息量最大的一步。
2. 若重排提升有限，再考虑**查询扩展**（把「健康探针」扩成 `healthcheck/heartbeat/status page`），
   而不是继续动打分权重。
3. 7 轮实验说明「加检索臂」会同时放大噪声 —— 扩臂必须配「池容量上限 + 分桶配额」，
   否则 top5 会被新臂捞进来的词面仓挤占。

## 7. 重排实测：模型选型决定成败，不是「重排有没有用」（2026-10-21，同日续）

标注集不变（v1，5 模块 / 13 仓，冷缓存）。四组跑法对比：

| 跑法 | hit@5 | mrr | 命中 | 重排成功 | 用时 |
|---|---:|---:|---:|---:|---:|
| 词法基线（§2，2 次） | 0.300 | 0.367 | 4/13 | — | 112~116s |
| 免费档 `openrouter/free` | 0.300 | 0.367 | 4/13 | **1/5** | 292s |
| 付费 `alibaba/qwen3.8-flash` | 0.300 | 0.467 | 4/13 | **3/5** | 432s |
| **付费 `agnes/agnes-2.5-flash`** | **0.467** | 0.480~0.650 | **6/13** | **14/15** | 193~269s |

### 7.1 前两组为什么是零

免费档与 qwen 档的 `hit@5` 与词法基线逐位相同，**不是因为重排无效，而是因为重排没跑成**：

- 免费档两模型 `cap=25s`，8 条候选的 prompt 稳定超时（实测 4/5 模块 `TimeoutError`）
- `alibaba/qwen3.8-flash` 的 `cap=60s` 同样超时；单独探测发现它**在 `max_tokens=16` 时也只回
  `only-thinking`**，thinking 吃满输出预算。网关本身不慢（短请求 4.8s 返回 200）

判据：卡上 `status` 字段是 `lexical` / `reranked` / `lexical_fallback` 三态，
**只看汇总数字会把「没跑」误读成「没用」**。引用重排指标前必须同时报 `reranked` 占比。

### 7.2 agnes 可复现（三次全量 + 一次真实代码路径）

| 轮次 | 路径 | hit@5 | mrr | 重排成功 |
|---|---|---:|---:|---:|
| run1 | 内存替换模型 | 0.467 | 0.490 | 4/5 |
| run2 | 内存替换模型 | 0.467 | 0.480 | 5/5 |
| run3 | **`_ref_model()` 真实代码** | 0.467 | 0.650 | 5/5 |

`hit@5` 三次完全相同；`mrr` 在 0.48~0.65 间漂（top5 内部位次摆动，不影响主指标）。
逐模块稳定项：terminal-ws 1.000（termpair/gotty/ttyd 三次全进，ttyd #3/#3/#4）、
llm-gateway 0.500（Portkey #5/#5/#4）、health-probe 0.500、cron-scheduler 0.333。

净变化：**多找到 2 个标注仓（ttmpair 那组的 ttyd、Portkey），代价是耗时 112s→193s**。
重排**只**解决了「入池但排太后」的 3 条里的 2 条（ttyd #8→#3、Portkey #8→#5）；
剩下 7 条未命中里 6 条**从未入池**，重排无能为力（它只对已入池候选重排）。

实施：`REF_LLM_MODEL`（默认 `agnes/agnes-2.5-flash`）只作用于重排，不动
`MANAGER_LLM_MODEL`（后者还服务 `/api/tasks/decompose`）。前端「允许付费重排」默认勾选。

### 7.3 召回层 4 组实验全部未超过 0.467（均已回退）

针对 6 个「未入池」标注仓做的对照实验（其余条件同 §7.2 配置）：

| 轮次 | 改了什么 | hit@5 | 关键观察 |
|---|---|---:|---|
| H | `core` 臂加 `in:readme`（四字段 OR） | 0.300 | 池 36→45，6 个目标仓**一个都没入池** |
| I | 新增 `phrase` 臂：`"full-text search" in:name,description,topics,readme` | 0.300 | 池→50，meili 仍未入池 |
| J | `phrase` 臂改 `in:readme`（去掉其余三字段） | 0.300 | 池→50，meili 仍未入池 |
| K | `SEARCH_PER_PAGE` 10→30 | 0.233 | **meili 入池 #14**，但 llm-gateway 的 Portkey 掉到 #16、litellm #21 |
| L | K + `MAX_CANDIDATES` 8→20（扩重排覆盖） | 0.167 | meili 仍 #15；重排 20 条也没救回，用时 443s |

### 7.4 本轮最硬的发现：`SEARCH_PER_PAGE=10` 在截召回

用 GitHub API 直接取证（同一查询、同一天）：

```
"full-text search" in:readme fork:false archived:false stars:>=20
  per_page=30 → meilisearch/meilisearch 排 #13（在结果里）
  per_page=10 → 不存在（被截断）
```

所以 K 轮 meili 能入池不是巧合，是**每臂只取 10 条**把它挡在门外。但 K/L 证明单纯扩池是负向：
池从 37 涨到 133 后，词法排序被稀释（Portkey #8→#16），而**重排只覆盖词法前 8 名**
（`MAX_CANDIDATES=8`），掉到 #14 的目标仓连重排机会都没有。

⇒ 结论：**池容量、每臂条数、重排覆盖三者是同一个旋钮**，单独拧任何一个都会变差。
当前配置（10 条/臂 + 重排前 8）是自洽的；要动就得三者同调并重测，且预算要按
「20 条候选 ≈ 2.5 倍 prompt」重估。

### 7.5 处置

- H~L 五组全部 `git checkout -- src/refscout.py` 回退，工作树回到 `f0385f9`
- 当前生产：词法基线口径 + `agnes/agnes-2.5-flash` 重排（`hit@5=0.467`）
- **下一步优先级**：先做「查询扩展」（`health probe`→`healthcheck/heartbeat`、
  `scheduler`→`cron/job queue`），而不是继续动池容量 —— 因为 §7.4 证明扩池必须三旋钮同调，
  而查询扩展不改变池结构，风险小得多

## 8. 标注集扩样与逐条核对（v1→v3，2026-10-21 深夜）

> 前七节的 `hit@5` 全部基于同一套 5 模块 / 13 仓的标注集。该集有两个结构性问题：
> ①每模块只有 2~3 条，`hit@5` 的分辨率是 0.33 一格；②只认少数仓，而 top5 里那些
> **真实的同类实现**（PowerJob、manticoresearch…）全被算成"未命中"。第 ① 个问题让
> 指标抖一格就变 0.167，第 ② 个问题让指标系统性偏低。本节做的就是修这两点。

### 8.1 判据：先定"什么算一条合格标注"

全部逐条打 `GET /repos/{owner}/{repo}` 取证（脚本 `scripts/annot_check.py`、
`scripts/annot_check_pool.py`），五条同时成立才收：

| # | 判据 | 为什么 | 取证字段 |
|---|---|---|---|
| A | 存在且 `full_name` 一致 | 名字写错（大小写/改仓）会让该条**永远**不可能命中，是纯假失败 | `full_name` |
| B | 硬过滤：`archived=false`、`fork=false`、`stars>=20` | 这三条分别写在 `collect()` 的 `continue` 和每条查询的 `stars:>=N fork:false archived:false` 里，不满足则**数学上必不命中** | `archived`/`fork`/`stargazers_count` |
| C | 描述能一句话说清「与模块解决同一问题」 | 这是标注的定义本身 | `description` |
| D | 不是"零件" | `node-pty`、`xterm.js` 只是终端分享方案的组件；标注零件会把 `hit@5` 变成"零件命中率" | 人判 |
| E | `stars>=100` | 参考情报的用途是"能抄的成熟实现"，24 星的个人 demo 不算；**此线只用于标注，不是池门槛** | `stargazers_count` |

**判据 B 差点写错，值得单记**：第一版脚本把 `pushed_at<=180d` 也当成硬过滤，据此
`gotty`(810d)/`robfig/cron`(835d)/`termpair`(210d) 全部判死。但代码里 `fresh_days` **只进
`fresh` 臂**（`pushed:>cut` + 按 `updated` 排），`core`/`pair`/`kw*` 臂都不带这个条件 ——
这三个仓在 v1 里**实际命中过**。量错对象会把好标注误删，比漏量更危险。

### 8.2 v1 那 13 条的核对结论

| 结论 | 数量 | 明细 |
|---|---:|---|
| 保留（语义与硬过滤都过） | 12 | termpair / gotty / ttyd / litellm / Portkey / uptime-kuma / gatus / apscheduler / robfig-cron / meilisearch / typesense / quickwit |
| **移除** | 1 | `celery/celery` —— 仓库一句话是「Distributed Task Queue」，beat 虽能 cron 调度，但不是「cron 表达式调度器」。它是 v1 为了让 cron 模块凑够 3 条塞进去的 |
| 陈旧但合法 | 3 | gotty 810d、robfig 835d、termpair 210d：只是失去 `fresh` 臂这一次机会，仍可经 `kw*`/`core` 入池 |

移除 celery 不影响可比性：v1 实测该模块 `hit@5=0`，它本来就没命中，v1 子集仍是 6/12=0.500。

### 8.3 v2：把 5 模块扩到 9 模块（35 仓）

新增 4 个模块（都按同一判据核过）：日志聚合与检索、内网穿透与反向代理、
向量检索与相似度搜索、文档站生成；原 5 模块各补 1~3 条。

**结果（冷缓存，`do_rerank=False`，258s）**：

| 指标 | 值 |
|---|---:|
| hit@5（9 模块平均） | **0.331** |
| 命中 | 10 / 35 |
| mrr | 0.359 |
| **v1 子集（5 模块 / 12 仓，可比口径）** | **0.500 / mrr 0.157** |

v1 子集与 §2 的 v4 逐位相同（0.500 / 0.157）⇒ 扩样 + 脚本改动**没有**动到旧口径，
两套数字可以并列。全量 0.331 低于 v1 的 0.500 不代表"变差"：分母从 12 变 35，
新增模块里有 3 个是 0 分（日志聚合 0/3、全文检索 0/6、文档站 0.25）。

### 8.4 v3：pooling 补标（58 仓）—— 这一步才把"低估"量化出来

v2 的 45 个 top5 位置里有 35 个未标注。逐条取事实后，**23 个是同类的真实实现**
（PowerJob/xxl-job/quartznet/TickerQ、manticoresearch/xerj/RediSearch/tantivy/flexsearch、
openstatus/checkcle、nps×2/rathole/NSmartProxy、infinity/turbovec、docfx/quartz-jackyzha0…）。
把它们补进来（判据同 8.1，语义逐条人判），得 v3 = 58 仓。

**结果（冷缓存，`do_rerank=False`，259s）**：

| 指标 | v2 | v3 |
|---|---:|---:|
| hit@5（9 模块平均） | 0.331 | **0.577** |
| 命中 | 10 / 35 | **32 / 58** |
| mrr | 0.359 | 0.837 |
| prec@5（下界） | 0.222 | 0.711 |
| **v1 子集** | **0.500 / 0.157** | **0.500 / 0.157** |

逐模块（v2 → v3）：

| 模块 | v2 | v3 | 该模块仍未命中的（真实名次） |
|---|---:|---:|---|
| 终端 WebSocket | 1.000 | 1.000 | — |
| LLM 网关 | 0.333 | 0.667 | Portkey #12、one-api #14 |
| 健康探针 | 0.500 | 0.667 | blackbox_exporter（未入池）、upptime #19 |
| 定时任务 | 0.200 | 0.556 | apscheduler #14、robfig #19、quartz-java（未入池）、dkron（未入池） |
| 全文检索 | 0.000 | 0.455 | meili #6、typesense #7、quickwit/sonic/bleve/zinc（未入池） |
| 日志聚合 | 0.000 | 0.250 | **loki、fluent-bit、vector 全部未入池** |
| 内网穿透 | 0.500 | 0.833 | cloudflared #14 |
| 向量检索 | 0.200 | 0.429 | qdrant #14、weaviate #15、pgvector #18、chroma（未入池） |
| 文档站 | 0.250 | 0.333 | mkdocs #16、docusaurus/vitepress/quartz（未入池） |

**这 0.331→0.577 的涨幅全部来自补标，不来自检索**。最硬的证据：v1 子集一分未动
（0.500 / 0.157）。换句话说，前七节那个 0.500 不是"检索只有 50 分"，而是
**"标注集只给了 50 分的题面"**。旧数字没有错，错在把它当成了检索质量的真值。

⚠️ **pooling bias 必须声明**：v3 只补了「v1/v2 的 top5 召回过」的仓。从未被召回的仓
（如 manticoresearch 之外的长尾引擎）不会进标注集 ⇒ 分母仍不完整，v3 的 0.577 是**乐观值**。
同时它也不能再拿去和 v2 比"好坏"——两套分母不同。

### 8.5 冻结规则（防止指标自我美化）

补标能无限抬高 `hit@5`（把 top5 全标上就是 1.0），这会让指标彻底失去诊断力。本轮起定死三条：

1. **标注冻结**：v3 之后不再按单次评测结果追加标注。补标只允许在"换评测目标/发现新模块"时成批做。
2. **永久双报**：任何 `hit@5` 必须同时给「v1 子集」与「全量」两个数，且注明版本。
3. **新补标的命中要单独算**：v3 的 32 个命中里，26 个来自 v2/v3 新补的标注，
   只有 6 个是老标注（=v1 子集那 6 个）⇒ 报"检索变好"时只能说这 6 个。

### 8.6 这轮真正暴露的产品缺陷（不是标注问题）

补标之后剩下的 26 条未命中，失败结构第一次变干净了：

| 失败类型 | 数量 | 说明 |
|---|---:|---|
| 池内但排在 6~19 名 | 12 | 排序问题。词法分 `0.70*cov + 0.20*log(stars)` 让"描述词面最密 + 星最多"的仓霸榜 |
| **完全未入池** | 14 | 召回问题，全部集中在长尾描述 |

两个新发现，按严重度排：

1. **日志聚合模块 0 召回**：`loki`(29k)、`fluent-bit`(8k)、`vector`(22k) 一个都没进池。
   已用 GitHub API 直接取证（2026-10-21 23:0x，非推断）：

   ```
   loki        desc "Like Prometheus, but for logs."        topics [… logging, loki, prometheus …]
   fluent-bit  desc "Fast and Lightweight Logs, Metrics…"   topics [… data-collector, logging, logs …]
   vector      desc "A high-performance observability data pipeline."  topics [… logs, monitoring …]

   q="centralized logging" in:name,description,topics  stars:>=20  → total_count=21
     top10: Kermit 1041 / Logflare 1006 / myntra-pipeline 484 / … / 第6名 ★132
   q="log aggregation"     in:name,description,topics  stars:>=20  → total_count=25
     top10: FalconHound 829 / pq 413 / certstream 360 / … / logsuck 168
   ```

   全站只有 **21 / 25** 个仓同时含这两个词，且全是千星以下的小仓 —— 29k 星的 loki
   根本不在这个结果集里，不是"排在后面"。三个机制叠加造成：
   ① **多词 AND**（`centralized` 和 `logging` 必须同时出现，loki 只有 `logging`）；
   ② **词形**（`\blog\b` 不匹配 `logs`，`log collector` 匹配不到 `data-collector`/`logs`）；
   ③ **星数排序**在 21 条小结果集里毫无意义。
   所以修法不是"再加几个同义词"，而是**词干/单复数归一 + 短语拆词合并（OR + 覆盖度打分）**。
   `hit@5=0.250` 那一格（最小仓 `logsuck` ★168 命中）恰是反面证据：指标上去了，主流实现还是找不到。

   附带查出的第二个缺陷：**关键词本身是短语时，`kw*` 扩展臂整条被 `pair` 去重吃掉**。
   本模块 `tokens_of` ⇒ `[log, aggregation, collector, centralized, logging]`，
   `pair = "log aggregation …"` 与 `kw0` 完全相同、`pair2 = "log collector …"` 与 `kw1` 相同，
   `seen_q` 去重后**只剩 7 个臂**（实测 arms：core 0 / pair 10 / pair2 10 / fresh 0 /
   cn 10 / reading 10 / kw2 10）。即"一条一个概念"的扩展在短语型关键词上**从未生效**。
2. **cron 模块 top5 与本机技术栈不匹配**：实测 top5 = PowerJob(Java 平台)、xxl-job(Java 平台)、
   quartznet(.NET)、TickerQ(.NET)、gocron(Go)，**0 个 Python**；而本机在用的
   `apscheduler`(Py) 排 #14、`robfig/cron`(Go) #19。
   注意这不是"平台压库"——top5 里有 3 个就是库。真因是**语言这一维几乎没权重**
   （`lang` 只占 `func` 的 0.10，且评测/默认调用里为空），检索把"功能词面 + 星数"
   当成了全部信号。修法：从模块所在项目推断语言做先验，或在卡面标出「语言不匹配」，
   而不是继续调权重。

这两条都**没有**在本轮改动代码（用户本轮要求是"扩展标注集并核对标签"），
仅作为下一轮的输入。

### 8.7 复现

```bash
# 1) 核标注（会打出每个仓的 ★/落地天数/archived，并标出被硬过滤的）
venv/bin/python scripts/annot_check.py                      # 候选池（v2 定稿前）
venv/bin/python scripts/annot_check_pool.py work/eval-v3-annot.json   # pooling 补标（v3）

# 2) 跑评测（冷缓存；v1 子集会随全量一起打印）
venv/bin/python tests/eval_refscout.py --json work/eval-v3-annot.json
```

产物：`work/annot-check-20261021.json`、`work/annot-pool-20261021.json`、
`work/eval-v2-annot.json`、`work/eval-v3-annot.json`；
标注集历史版本备份在 `work/annotations.json.bak-*-v1|v2`。

