# 参考情报：自有来源（方案 A）落地记录 — 2026-10-21

> PT-20261021-04 追加项。用户当日裁定：**方案 A + 支持多来源可增删**——
> 即把「自己的 GitHub 仓（含 fork）」当成候选池的**一路来源**，而不是只给候选贴一个标签。

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
| 表 | `ref_sources(id, kind, value, label, enabled, created_at, synced_at, repo_count, last_error)`，`value` 唯一 |
| 取值 | `kind=owner`（账号或组织，含 fork）/ `kind=repo`（单仓） |
| 拉取 | owner：`GET /users/{v}/repos?type=owner&sort=updated`，404 才回落 `/orgs/{v}/repos`；repo：`GET /repos/{v}` |
| 缓存 | 6h（core API 5000/h，不吃 search 的 30/min）；单来源上限 300 仓（3 页） |
| 并入 | 池内同键合并，候选带 `sources: ["gztxt", ...]` 与 `src_bonus` |
| 卡面 | 头部列「来源（自有仓，含 fork，不占搜索额度）」+ 候选表新增「来源」列 + 独立「自有来源命中」段 |
| REST | `GET/POST /api/ref/sources`、`DELETE /api/ref/sources/{id}`（`POST` 幂等） |
| UI | 参考情报视图「来源」工具条：chip 显示 `已同步 N 仓 / 待同步 / 失败`，点 chip 即删；输入框支持 `gztxt`、`gztxt/agenthub`、完整 URL 与 `/tree/...` 尾巴 |

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

## 4. 实测

| 项 | 结果 |
|---|---|
| 真实拉取 `gztxt` | 43 仓 / 953ms（冷）、1ms（6h 缓存命中）；`gztxt/agenthub` 1 仓 / 563ms |
| 真实采集（终端 WebSocket 与端口转发，`do_rerank=false`） | 冷 29.3s、热 0.2s；池 110；`gztxt/agenthub` 进候选（func 0.515 / cov 0.559） |
| 无关自有仓 | `free-programming-books`（cov 0）被第 3 节口径挡住，不再进卡 |
| 单元测试 | `tests/test_refscout_sources_l0.py` 12 例 |
| 全量 L0 | `run_tests.sh hermetic`：**2612 例 / 0 跳过 / 0 失败**（含新增 12 例） |
| 真 Chromium 探针 | `tests/probe_refscout_ui.py` 于影子 :3219 **12/12**（原 9 项 + 来源工具条 3 项：存在 / 添加出 chip / 点删回退） |

> 探针的删除只回滚**它自己新增**的那一条（记录 `before` 计数），避免对着已有数据的库
> 误删用户来源。

## 5. 已知边界

- 单来源 300 仓封顶；超出部分不参与（避免一次任务把 core 额度拉爆）。
- 私有仓依赖 token 的可见范围：`ghsettings` 读到的 token 无该仓权限时，来源会显示
  `失败：HTTP404`，而不是「没有相关项目」。
- 来源仓参与 IDF 统计（`_concept_weights` 的池就是合并后的池）⇒ 加了很多仓会轻微改变
  同批搜索的权重。未加来源时行为逐字不变。
- 同步时机是「下次检索 / 重跑」，不是定时；chips 会明写 `待同步`。
- 自动刷新（30 天到期）也会同步来源，但不付费重排（既有铁律）。

## 6. 回滚

- 代码：`git revert <commit>`；表可保留（空表无副作用）。
- 只想停用某个来源：UI 点 chip 删除即可（`DELETE /api/ref/sources/{id}`）。
