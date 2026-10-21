# 参考情报：自有来源（方案 A）落地记录 — 2026-10-21

> PT-20261021-04 追加项。用户当日裁定：**方案 A + 支持多来源可增删**——
> 即把「自己的 GitHub 仓（含 fork）」当成候选池的**一路来源**，而不是只给候选贴一个标签。
>
> **追加（同日晚）**：用户澄清「是本仓库或者账户 stars 过的项目」⇒ 来源扩到
> `kind=starred`（该账号标星的仓）。starred 与 owner 走完全相同的并入 / 过滤 / 打分 / 闸门，
> 只有候选集来源不同（`/users/{v}/starred` 而不是 `/users/{v}/repos`）。

## 1. 为什么不是「只做标注」

第一版设想的 B 方案（检索仍走全站，只标「你已 fork/拥有」）在实测里不成立：

- 自有仓的典型状态是 **0~1 star 且 `fork=true`**，而检索主臂写死了
  `fork:false archived:false stars:>=N`（N 默认 20）⇒ 这些仓**从来进不了池**，
  标签自然一条也贴不上。
- 实测 `github.com/gztxt` 公开仓 43 个，其中 25 个是 fork，star 全是 0~2。

因此并入必须发生在**池子阶段**：与搜索结果同形态入库，再走同一套过滤 / 三轴打分 /
许可证闸门。差别只在两个搜索限定被跳过（`stars:>=N`、`fork:false`），
**`archived` 仍然拒**——维护状态是硬闸门，不因「是我自己的」而豁免。

## 2. 形态

| 层 | 内容 |
|---|---|
| 表 | `ref_sources(id, kind, value, label, enabled, created_at, synced_at, repo_count, last_error)`，唯一键 `(kind, value)` |
| 取值 | `kind=owner`（账号或组织，含 fork）/ `kind=repo`（单仓）/ `kind=starred`（该账号标星的仓） |
| 拉取 | owner：`GET /users/{v}/repos?type=owner&sort=updated`，404 才回落 `/orgs/{v}/repos`；repo：`GET /repos/{v}`；starred：`GET /users/{v}/starred?sort=updated`（不带 `type=owner`，星标里本来就可能是我认可的 fork） |
| 缓存 | 6h（core API 5000/h，不吃 search 的 30/min）；单来源上限 300 仓（3 页） |
| 并入 | 池内同键合并，候选带 `sources: ["gztxt", "star:gztxt", ...]` 与 `src_bonus` |
| 卡面 | 头部列「来源（自有仓，含 fork，不占搜索额度）」+ 候选表新增「来源」列 + 独立「自有来源命中」段 |
| REST | `GET/POST /api/ref/sources`、`DELETE /api/ref/sources/{id}`（`POST` 幂等） |
| UI | 参考情报视图「来源」工具条：类型下拉（账号/组织 · 单个仓库 · 星标列表）+ 输入框 + chip（`已同步 N 仓 / 待同步 / 失败`，点 chip 即删）；输入框也认 `gztxt`、`gztxt/agenthub`、`gztxt:starred`、完整 URL 与 `/tree/...` 尾巴 |

> **唯一键为什么带 `kind`**：`gztxt`（自己的仓）与 `gztxt:starred`（标星的仓）是两个完全
> 不同的候选集。按 `value` 去重时后加的那条会被静默判成「已存在」，用户看到 chip 却又拿不到
> 星标候选。默认 label 也据此区分：`gztxt` vs `star:gztxt`。

## 3. 三个口径（都是为避免「看起来生效」）

1. **加成只进排序，不进三轴分**：`SOURCE_BONUS=0.06` 只参与
   `0.6*func + 0.3*impl + 0.1*design` 的排序项。三轴分回答「这个仓本身值不值得借鉴」，
   掺「我拥有它」这种非质量信号会让卡面数字失去可比性。
2. **自有来源的无关键仍要挡**：资料/清单桶对搜索候选不设覆盖度门槛（旧口径），
   但自有来源动辄几十个 fork，不加门槛时 `free-programming-books` 这种 0 覆盖的仓
   会永久挂在卡上。现在自有来源统一要求 `matched>0 or cov>=0.18 or 短语/代码命中`。
3. **单独再列一段「自有来源命中」**：主表是统一排序 + `limit=8` 截断，
   自己的仓很容易被 1k+ star 的外部仓挤到第 9 名之后 ⇒ 加了来源却「看不见」。
   该段只做可见性保证，不二次排序，已进主表的标「（已在上表）」。
4. **排序＝参考价值优先，来源只作同分次序**（2026-10-21 用户裁定「主要是要有参考价值，
   没有参考价值就排后」）：`src_bonus` 原先**加进**加权分，0.06 足以让「我的仓」压过功能分
   略高的外部仓。现改为 `_order_key = (桶, 三轴加权分, src_bonus)`——分不同按分排，
   **完全同分**时才让自己的仓靠前。语义重排后另有 `judge` 档位（`use/fork` → `read` →
   `unjudged` → `skip`），判定 skip 的一律沉底。
5. **摘要落到卡面**：`readme_gist`（README 首段去徽章/链接/代码块，260 字）原本只喂给重排模型，
   卡上看不到 ⇒ 用户只能凭一句 GitHub 描述判断值不值得点开。现在「可借鉴点」每条追加
   `- 摘要：…`；「自有来源命中」也补一句描述，便于先扫价值再看仓。

## 4. 实测

| 项 | 结果 |
|---|---|
| 真实拉取 `gztxt` | 43 仓 / 953ms（冷）、1ms（6h 缓存命中）；`gztxt/agenthub` 1 仓 / 563ms |
| 真实拉取 `gztxt:starred` | **273 仓**（`Link` 头 `rel=last` 同为 273，翻页取全）/ **12.5s 冷**、20ms 热；archived 12 → 实际入池 261；其中 fork 20 |
| 真实采集（终端 WebSocket 与端口转发，`do_rerank=false`） | 冷 29.3s、热 0.2s；池 110；`gztxt/agenthub` 进候选（func 0.515 / cov 0.559） |
| 真实采集（GitHub 资料收集整理，仅 starred 来源） | 46 候选；`EbookFoundation/free-programming-books`、`lukasmasuch/best-of-python`、`mahmoud/awesome-python-applications`、`punkpeye/awesome-mcp-servers`、`yzfly/Awesome-MCP-ZH` 均带 `src=['star:gztxt']` |
| 价值优先复验（两条来源都在库，池 330/353） | 「GitHub 资料收集整理」top1-3 = `HelloGitHub`(f .65/i .85) / `computer-science` / `build-your-own-x`，全是 `star:gztxt`；「终端复用」top10 **无一条来源仓**（无关的不会因为「是我的」上榜） |
| 无关自有仓 | `free-programming-books`（终端复用查询下 cov 0）被第 3 节口径挡住，不再进卡 |
| 单元测试 | `tests/test_refscout_sources_l0.py` **21 例**（starred 归一化 2 / 同类名共存 1 / starred 翻页 1 / starred 入池 1 / 价值优先排序 3 / 卡面摘要 1） |
| 全量 L0 | `run_tests.sh hermetic`：**2621 例 / 0 跳过 / 0 失败**（含新增 21 例） |
| 真 Chromium 探针 | `tests/probe_refscout_ui.py` 于影子 :3219 **14/14**（原 9 项 + 来源工具条 3 项 + 星标来源 2 项） |

> 探针的删除只回滚**它自己新增**的那一条（记录 `before` 计数），避免对着已有数据的库
> 误删用户来源。

## 5. 已知边界

- 单来源 300 仓封顶；超出部分不参与（避免一次任务把 core 额度拉爆）。starred 取前 300 条，
  `gztxt` 当前 273 条刚好全取；超过 300 的账号只能拿到最近更新的 300 个。
- **starred 冷拉一次 12.5s（3 页）**，直接计入当轮采集耗时；命中缓存（6h）后 20ms。
  这段是串行同步拉取，来源多了会线性叠加——真到 5+ 个大账号时要改成并发或后台预取。
- `github.com/stars/{u}/lists/{name}` 形式的地址只取账号、**忽略 list 名**（按全部星标处理）。
  要做「只取某个 list」得另加接口（当前 GitHub REST 不提供 list 维度）。
- 私有仓依赖 token 的可见范围：`ghsettings` 读到的 token 无该仓权限时，来源会显示
  `失败：HTTP404`，而不是「没有相关项目」。starred 走 `/users/{u}/starred` 公开端点，
  **不含私有星标**（要私有得走 `/user/starred`，只能取 token 本人）。
- 来源仓参与 IDF 统计（`_concept_weights` 的池就是合并后的池）⇒ 加了很多仓会轻微改变
  同批搜索的权重。未加来源时行为逐字不变。
- 同步时机是「下次检索 / 重跑」，不是定时；chips 会明写 `待同步`。
- 自动刷新（30 天到期）也会同步来源，但不付费重排（既有铁律）。

## 5.1 付费重排实测（2026-10-22 00:0x，生产 :3102，卡 `00c660b6167e`）

同一张卡连点三次「允许付费重排 + 重跑」，**只有第 3 次成功**，且三次结论不一致：

| 次 | 付费档 | 免费两档 | 卡状态 |
|---|---|---|---|
| 1 | `TimeoutError` 25024ms | 25s ×2 全超时 | `lexical_fallback` |
| 2 | `degenerate` 6506ms（回 JSON 但三轴全 <0.05） | 25s ×2 全超时 | `lexical_fallback` |
| 3 | ok 5611ms，3 条判定 | 未再试（付费档已成功） | `reranked`，`rerank_paid=true` |

- **同一 prompt 同一模型判词会翻**：离线探针（同网关、同 2072 字 prompt）两次都给
  `HelloGitHub` = read / func 0.8，生产第 3 轮却判 skip（"内容策展平台，非技术实现"）。
  ⇒ 单次判定不可当结论，卡面「判定」列本身带随机性。
- 第 3 轮结论：3 个 impl 候选全 skip ⇒ **实现桶表与「可借鉴点」为空**，只留「未入选」段。
  这不是渲染 bug：`skip` 按 `_judge_rank` 沉底后不进主表，属于「没有参考价值就排后」的正常结果。
- 付费档 cap 是 **25s 不是 60s**：`cap = 60 if model == llm.MODEL else 25`，而
  `llm.MODEL` 现在是 `alibaba/qwen3.8-flash`、付费档是 `agnes/agnes-2.5-flash` ⇒
  永远走 25s 分支，与 `rerank()` docstring「付费 60s」不符（本轮 5.6~6.5s 够用，但网关抖时就卡死）。
- 免费两档（`openrouter/free`、`nemotron-3-super:free`）在本 prompt 上 **每次 25s 占满预算**，
  与 P0 记录一致（免费池被内容审核/只思考占满）。

## 6. 回滚

- 代码：`git revert <commit>`；表可保留（空表无副作用）。
- 只想停用某个来源：UI 点 chip 删除即可（`DELETE /api/ref/sources/{id}`）。
