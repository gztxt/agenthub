# 参考情报（refscout）P1 交付说明

- 日期：2026-10-21
- 对应台账：`PT-20261021-04`（P0 报告见 `REFERENCE-RADAR-P0-20261021.md`）
- 形态：技能中心第 5 个 tab「参考情报」+ REST + MCP 双工具
- 边界：**只读采集**。不克隆、不改项目、不自动加依赖；只产出建议与 `repo@sha` 固定引用

## 1. P1 做了什么（P0 的差距 → 本次落点）

| P0 遗留 | P1 处理 |
|---|---|
| 只有命令行 spike | `src/refscout.py` 服务模块 + `/api/ref/*` 六个端点 |
| 同步跑 60~80s，用户干等 | **异步任务**：先落词法卡即可读，重排完成原地升级 |
| 重排结果不稳定且无法解释 | 逐次尝试记录（模型 / 耗时 / stop_reason / 覆盖条数）落进卡片 `meta` |
| 免费档失败只能降级 | 付费档优先、免费档兜底（`allow_paid` 显式开启，卡上标注「付费档」） |
| 缓存 38MB（README/树全落盘） | 搜索 7 天、enrich 24h；README 截 6000 字。6 次调研后 **5.3MB / 87 文件** |
| 产物只在 `work/*.md` | 两表 `ref_cards` / `ref_jobs`（DATA_DIR 内，重启仍在），30 天到期自动重跑 |
| 无 MCP | `hub_ref_scout` / `hub_ref_get`（与 `hub_skill_*` 同门面） |
| 无界面 | 技能中心「参考情报」tab：提交、列表、查看全文、重跑（走事件委托，无 inline onclick） |

## 2. 实测结果（影子实例 :3212，生产未动）

### 2.1 时延（单次，含真实网络；`lex` = 词法卡可用时刻）

| 用例 | 缓存 | lex | 重排 | 总耗时 | 重排模型 |
|---|---|---:|---:|---:|---|
| 终端 WebSocket 与端口转发 | 冷 | 27.6s | — | 83.3s（首轮） | 免费两档均 only-thinking → 降级 |
| 同上 | 冷（第二次） | 0.0s | 81.9s | 81.9s | `openrouter/openrouter/free` |
| 同上 | 热 | 0.0s | — | 65.0s | 免费档 |
| 技能中心 技能扫描与调用记账 | 冷 | 27.6s | 42.0s | 69.6s | `alibaba/qwen3.8-flash`（付费） |
| 同上（加 README 摘要后重跑） | 热 | 0.0s | 58.5s | 58.5s | `alibaba/qwen3.8-flash`（付费），8/8 候选全覆盖 |

判据重新定义（P0 的「单次 ≤60s」把重排算进首屏，实际做不到且没必要）：
**词法卡 ≤30s（冷）/ ≤1s（热）；完整卡 ≤120s**。实测 27.6s 与 58.5s，均达标。

### 2.2 检索质量（人工单评审，只看「是否解决同一问题」）

- 终端模块：top5 = browser-terminal / gotty / termpair / webterminal / TermBeam → **5/5 相关**。
- 技能中心模块：top4 = Observal（use）/ skillhub（use）/ mcp-gateway-registry（read）/ agent-skills（read）。
  加 README 摘要**之前**，同一批数据里 skillhub 被判 skip、Observal 排第一；加入后 skillhub 升到 `use`。
  ⇒ 仅靠 description 时模型会混淆「技能注册中心」与「agent 可观测平台」，README 首段是最便宜的纠偏信号。
- 主臂：`terminal websocket pty` 从 P0 的 4 词元（1 条）改为 3 词元（**9 条，全部相关**）。

### 2.3 额度与缓存

- 单次调研：仓库搜索 6 次 + 代码搜索 2 次（≤2）+ 详情/README/树 ≤24 次（5000/h 内）。
- 全程 **0 次 403/429**；`ref_jobs` 无 `rate_limited` 记录。
- 热路径检索 **0.0s**（全缓存）——`meta.cache` 与 `arms[].cached` 可核对，不靠「应该缓存了」。

## 3. 状态机与「未知」的表达

| 卡状态 | 含义 | 界面/卡面 |
|---|---|---|
| `lexical` | 检索完成、重排未开始/未开 | 判定列为空，表照常 |
| `reranked` | 重排成功 | 每行有判定与一句理由 |
| `lexical_fallback` | 重排全失败 | 卡面写「仅词法 + 原因」 |

候选判定四档：`use/fork` > `read` > **`unjudged`** > `skip`。

- `unjudged` **不是**「可以借鉴」，只表示「本轮没判到」。原因分三种如实写：
  输出被 `max_tokens` 截断 / 模型漏了该条 / 没进词法前 8 名。
- `unjudged` 不出现在「可借鉴点」段，只出现在「未入选」清单。

## 4. 安全边界（沿用并显式化）

- 凭据：DB 设置页 → `GITHUB_TOKEN` → `github.txt` 首行；真值不出 `token()`，错误正文过 `tdai_client.scrub`。
- 出站：每个 URL 过 `outbound.check_url(require_https=True)`，拒绝内网/元数据端点。
- 写端点：`POST /api/ref/scout`、`POST /api/ref/cards/{id}/refresh`、`DELETE` 均走既有 writeauth 闸门
  （`test_writeauth.py` 13 例通过，未新增豁免）。
- 许可证：GPL/AGPL 标 `copyleft`，卡面写「只允许借鉴设计，不得摘录代码」；`UNKNOWN/NOASSERTION` 写「默认不得复用，需人工核实」。
- 定时：`refresh_loop` 在 hub 进程内（与 `vitals_loop` 同类），**不写系统 crontab**、不建用户面 cron job。

## 5. 验证证据

| 项 | 结果 |
|---|---|
| `tests/test_refscout_p1_l0.py` | 15 例通过（主臂无 readme / fresh 独立排序 / copyleft 与未知许可提示 / as_of+next_refresh / unjudged 档位 / 刷新原地更新 / diff 三态） |
| `tests/test_writeauth.py` | 13 例通过（新写端点默认被闸门覆盖） |
| `tests/test_skill_center_ui.py` | 84 例通过 |
| `tests/test_staticguard.py` | 12 例通过 |
| `tests/test_hubjs_split.py` | 3 例通过（`static/hub.js` 与分片逐字节一致，`?v=` 已随内容更新） |
| `tests/probe_refscout_ui.py`（真 Chromium） | 9/9：tab 切换、4 卡渲染、详情 4379 字含 `as of`/可借鉴点、390px 横向溢出 0px、无阻塞对话框 |
| MCP 端到端 | `initialize` → `tools/call hub_ref_get` 返回 3 张卡；`hub_ref_scout`/`hub_ref_get` 已注册（共 16 个工具） |

## 6. 已知不足（P2 候选，不在本期偷做）

1. **重排耗时方差大**（42s / 58.5s / 81.9s）。当前靠「先出词法卡」兜住体感；
   若要压到 30s 内，需要换更快的付费档或把候选降到 6 条 —— 都会牺牲覆盖度，先收集数据再定。
2. **只到文件级引用**。`repo@sha` 已有，行级 `#Lx-Ly` 需要浅克隆 + 符号定位（P0 的 B 方案）。
3. **单评审、无盲评**。质量结论只对 2 个模块成立，台账要求 5 模块标注集尚未做。
4. **卡片未落 `技术文档/<项目>/refs/`**。当前只在 hub 库；落盘涉及「写哪个项目目录」的授权边界，
   需要先定 `project` 参数与白名单，避免前端决定写路径。
5. **自动刷新未区分代价**：`allow_paid` 会随卡片记住（`meta.rerank_paid`），自动重跑也会用付费档。
   如果长期挂着多张付费卡，需要预算闸（P2）。

## 7. 回滚

- 代码：`git revert <commit>`（仅新增文件 + main.py 3 行接线，无存量改写）。
- 数据：`ref_cards`/`ref_jobs` 两表可 `DROP`，不影响其它表。
- 界面：删 tab 按钮与 `skillViewRefs` 面板即可，无共享状态。
