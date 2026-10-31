# B4 · 大模块切分方案（2026-10-31）

> 状态：**方案，未施工**。本文件只定「切哪里、为什么这么切、怎么证明没切坏、什么不许切」。
> 施工按 §6 的顺序逐模块进行，一个模块一个提交。
>
> 触发：用户 2026-10-31 授权 B4。前置批次（前端 F1 / 后端 B1 / 文档 D1）已合并且生产运行
> （`git_sha_boot=d86cd52`，L0 3498 例全绿）。

## 0. 一句话结论

四个超 1800 行的模块里，**只有 `refscout.py` 值是正的、风险最低**；`skill.py` 被 26 个文件引用，
切它的收益要拿 26 个文件的重跑换，建议**排在最后或不做**；`tasks.py` 的真正病根不是模块大，
而是 `_run_task` 一个函数 277 行——**切函数比切文件收益高**。

## 1. 实测基线（2026-10-31，`d86cd52`）

| 模块 | 行数 | 顶层项 | 带装饰器(路由) | 最大单函数 | 被几个文件引用 |
|---|---|---|---|---|---|
| `src/refscout.py` | 2099 | 81 | 11 | `collect` 249 行 | 7 |
| `src/skill.py` | 2055 | 50 | 13 | `skill_relevant` 158 行 | **26** |
| `src/modelcfg.py` | 1871 | 85 | 6 | `verify_model` 70 行 | 8 |
| `src/tasks.py` | 1848 | 65 | 14 | **`_run_task` 277 行** | 12 |
| `src/term.py` | 1626 | 45 | 8 | `term_ws` 260 行 | —（**本方案不碰**） |
| `src/skill_store.py` | 1288 | 42 | 7 | `acquire` 202 行 | —（本方案不碰） |
| `src/sessions_store.py` | 1276 | 50 | 0 | `_title_of_session` 107 行 | —（本方案不碰） |

判据来源：`ast` 扫 `src/*.py` 顶层 `FunctionDef/AsyncFunctionDef/ClassDef` 的 `lineno/end_lineno`；
「被引用」= `grep -rlE "^import <m>$|^from <m> import"` 在 `src/ static/ tests/ scripts/` 的命中文件数。

## 2. 切分判据（为什么这么切，不是凭行数）

一条硬判据：**切面必须与「依赖方向」重合，不能横切**。

- 切出来的新模块**只许被老模块引用**，不许反过来引用老模块（否则是循环，等于把问题挪个地方）。
- 优先按「**纯函数 / 需要 IO**」分层：纯函数（打分、渲染、形状闸门、frontmatter 解析）先出来，
  因为它们能被 L0 直测；碰 db/网络/pty 的留在原模块。
- **不许为了行数好看而切**：把 `db` 调用和它们的消费者切到两个文件，会让每次改动都要跨文件对齐。

同仓已有成功先例：`src/staticguard.py`（2026-10-12，从 `main.py` 抽出「判据 + 组装 + 发射」，
路由退化成一次调用，golden 矩阵进 L0）。**照这个形状做**——抽出的是**一个深模块**，
不是「把一堆函数平均分到 3 个文件」。

## 3. 四个模块的具体切法

### 3.1 `refscout.py`（2099 → 6 个文件：5 新 + 原，**首选**）

现有章节横幅（行号即锚点）与归属：

| 现状章节 | 行范围 | 去向 | 判据 |
|---|---|---|---|
| 常量 / 基础 | 59–282 | `refscout_base.py` | 被下面所有模块依赖 ⇒ 必须**下沉**，否则新模块要 import 老模块（见下方 ⚠） |
| 来源（自有 GitHub 仓） | 283–535 | `refscout_sources.py` | 只碰 GitHub API + `ghsettings`，无卡片/无渲染 |
| 查询构造 + 打分 | 536–823 | `refscout_rank.py` | **纯函数**（输入字符串/字典，输出分）⇒ 可 L0 直测 |
| 清单展开（awesome→项目） | 824–1248 | `refscout_collect.py` | 网络密集；与 `collect` 同一条流水线 |
| 采集主流程 | 1249–1562 | `refscout_collect.py` | 同上，与展开共用抓取预算/去重 |
| 卡片渲染 | 1563–1717 | `refscout_render.py` | **纯函数**（输入 dict，输出 markdown） |
| 任务层 + HTTP 路由 + 定时 | 1718–2099 | `refscout.py` 保留 | 组装层，唯一知道 router/DB 的地方 |

产出：`refscout.py` ~570 行 + 5 个 150–700 行的深模块。
**依赖方向（单向，无环）**：`refscout_base ← {sources, rank, collect, render} ← refscout`。

> ⚠ 这一条是方案第一版写错又改回来的地方：常量若留在 `refscout.py`，4 个新模块就得
> `from refscout import X` ⇒ 反向依赖，`refscout` 一 import 就触发新模块再 import 它自己。
> **判据**：新模块里 `grep -c '^import refscout$'` 必须为 0。

**工时**：2–3 小时（含逐段搬运与 4 次全量 L0）。**风险：低**（7 个引用点，且都在 hub 内部）。

### 3.2 `modelcfg.py`（1871 → 4 个）

| 现状章节 | 行范围 | 去向 |
|---|---|---|
| 模型 ID 形状闸门 | 54–164 | `modelcfg_shape.py`（纯函数） |
| CCR 模型清单 | 165–292 | 留 `modelcfg.py`（要网络 + 缓存） |
| 定点改写工具 | 293–406 | `modelcfg_writers.py`（纯文本/JSON 操作） |
| 各家读写器（claude/codex/…) | 407–984 | `modelcfg_writers.py` |
| argv-only 型 | 985–1058 | `modelcfg_writers.py` |
| Hub 侧 per-agent 模型 | 1059–1120 | 留 `modelcfg.py` |
| 三通道 × 清单有效性判定 | 1121–1436 | `modelcfg_verify.py`（读文件 + 判定，不写） |
| 对外状态/预览/落笔 + 撤销 + HTTP | 1437–1871 | 留 `modelcfg.py` |

风险点：`modelcfg_writers.py` 会到 ~760 行——**这是可接受的**（它是一族同构读写器，
拆成每 agent 一个文件反而让「改一处判据要开 20 个文件」）。**工时 2 小时，风险：中低**。

### 3.3 `tasks.py`（1848 → 4 个 + 拆函数）

**先拆函数，再拆文件**——本模块的病根是 `_run_task` 277 行（`1478–1754`）。

函数级拆（不改行为，纯提取，最容易被测试钉住）：

| 新函数 | 来源段 | 行数 |
|---|---|---|
| `_pick_agent_and_model()` | `_run_task` 里的选型/降级分支 | ~50 |
| `_run_verifier_chain()` | 产物 → verifier → chain_judge | ~70 |
| `_record_task_result()` | 写库 + verdict + 事件 | ~60 |
| `_run_task()` 保留 | 编排骨架 | ~90 |

文件级拆：

| 章节 | 行范围 | 去向 |
|---|---|---|
| 状态枚举 + 预算 + 升级/重试 + 检索块 | 45–502 | `tasks_tiers.py`（纯逻辑，可 L0 直测） |
| DAG 校验 + 拆解 + 模型白名单 | 562–959 | `tasks_dag.py` |
| 重拆闸门 + `_replan_once` | 787–1110 | `tasks_replan.py` |
| runner（`_run_task`/`_execute_run`/`_chat_*`/`sweep`） | 1426–1848 | `tasks_runner.py` |
| run CRUD + 路由 | 1114–1423 | 留 `tasks.py` |

**工时 3–4 小时，风险：中高**（12 个引用点；且这是「执行者不得自证 success」的现场，
搬错一段就等于把验收链搬没了）。**必须在每一段搬运后跑 `tests/test_task_rounds_l0.py`（33 条）。**

### 3.4 `skill.py`（2055 → 5 个，**最后做或不做**）

| 章节 | 行范围 | 去向 |
|---|---|---|
| 目录白名单 / 排除 / 归档根 / 注记 / 项目根 | 71–317 | `skill_roots.py` |
| 扫描缓存 + single-flight | 318–354 | 留 `skill.py`（有状态、被路由直用） |
| frontmatter 解析 | 355–805 | `skill_frontmatter.py`（纯字符串） |
| `q` 的模糊口径 | 806–1187 | `skill_match.py` |
| 统一调用（激活）入口 | 1188–1652 | `skill_invoke.py` |
| 安装管理 + 预算化清单 | 1653–1738 | 留 `skill.py` |
| 相关性检索 | 1739–2055 | `skill_match.py` |

**为什么不建议现在做**：`skill` 被 **26 个文件**引用（`skill_store.py`、`intake.py`、多个 `test_*.py`），
是全仓第三大耦合点。切它的验证成本 ≈ 跑 57 个相关测试文件 + 逐条核对 `dir(skill)`。
**收益（可读性）与成本（一次大面积回归）相比不划算，除非它再长 500 行。**

## 4. 明确不做

1. **不碰 `src/term.py`**（1626 行）：pty/WS 时序类缺陷的定位成本极高（判例 75/83/85/90/91/96），
   拆文件会让「一帧里发生的事」散在多个文件里，**净负收益**。
2. **不碰 `src/writeauth.py`**：鉴权闸门是 fail-closed 的安全边界，与用户 2026-10-31 裁定的
   staticguard 未提交改动同批风险，**不在本方案范围**。
3. **不改任何行为**：本方案全程是「搬运 + 重新导出」，禁止顺手改逻辑、改判据、改名对外函数。
4. **不合并小模块**：反向操作不在本轮。
5. **不动 `src/staticguard.py`**（用户裁定的未提交改动仍在主树）。

## 5. 怎么证明没切坏（每步必跑，缺一即不算完成）

1. **路由指纹不变量**（最强判据）：
   ```bash
   curl -sS localhost:3102/openapi.json | python3 -c "import json,sys;d=json.load(sys.stdin);print('\n'.join(sorted(f'{m.upper()} {p}' for p,v in d['paths'].items() for m in v)))" | sort > /tmp/routes-after.txt
   diff <(sort work/dispatch/agenthub-upgrade-20261031/routes-before.txt) /tmp/routes-after.txt && echo ROUTES-IDENTICAL
   ```
   改前先存基线：`... > work/dispatch/agenthub-upgrade-20261031/routes-before.txt`。
2. **对外符号不变量**（防「搬走了但没人能 import」）：
   ```bash
   ./venv/bin/python -c "import sys;sys.path.insert(0,'src');import refscout;print(len([n for n in dir(refscout) if not n.startswith('_')]))"
   ```
   改前后数字必须相等；不等就要在 `refscout.py` 里显式 `from refscout_rank import *`（**不要**，
   改成逐个具名 re-export）并说明为什么少了哪个。
3. **L0 全量**：`bash scripts/run_tests.sh hermetic` —— 用例数**不许下降**（当前 3498），
   `skipped=0`，`failures=0`。
4. **目标模块的专项测试**：`refscout` → `tests/test_refscout_eval_l0.py`；`tasks` → `tests/test_task_rounds_l0.py`；`modelcfg` → `tests/test_modelcfg.py`。
5. **推前闸门**：`bash scripts/prepush.sh` 六道全绿（含 ⑦ 指纹闸门）。

> 「搬完发现全绿」不等于没切坏——**路由指纹与符号计数就是为这条准备的**：
> 把某个路由函数整段删掉，L0 也可能全绿（它不 import `src.main`）。

## 6. 施工顺序与总工时

| 序 | 模块 | 工时 | 风险 | 前置 |
|---|---|---|---|---|
| 1 | `refscout.py` → 6 个文件 | 2–3h | 低 | 先存路由基线 |
| 2 | `modelcfg.py` → 4 个 | 2h | 中低 | 1 完成 |
| 3 | `tasks.py`：先拆 `_run_task`，再拆文件 | 3–4h | 中高 | 2 完成 |
| 4 | `skill.py` → 5 个 | 4–5h | 高 | **待用户二次确认** |

合计 7–9 小时（不含 4）。**每模块独立提交**，提交信息带「纯搬运，无行为变更 + 路由指纹一致」。

## 7. 交给 codebuddy 时的分片（写域互不重叠）

- 片 1：`src/refscout*.py` + 对应测试（**只跑 `test_refscout_*`，不许改别的模块**）
- 片 2：`src/modelcfg*.py`（`src/modelcfg.py` 单写者）
- 片 3：`src/tasks*.py`（**必须本人/主会话做**——验收链现场，见 §3.3 风险）

每片验收线 = §5 的 1/2/4 三条 + 主会话补跑 §5 的 3/5 两条。

## 8. 已存的验证基线（切分前，2026-10-31 `d86cd52`）

```
work/dispatch/agenthub-upgrade-20261031/routes-before.txt          # 182 条路由（方法 + 路径，已排序）
work/dispatch/agenthub-upgrade-20261031/public-symbols-before.txt  # refscout 102 / skill 70 / modelcfg 68 / tasks 85
```

切完后逐项比对，**数字必须逐字相等**；不等即回滚该片。

## 9. 执行结果（2026-11-01 收口）

| 片 | 结 果 | 证据 |
|---|---|---|
| 1 `refscout` | ✅ 2099 → 6 文件（base/sources/rank/collect/render/门面） | `2997a11` |
| 2 `modelcfg` | ✅ 1871 → 4 文件（shape/writers/verify/门面） | `34d5d59` |
| 3 `tasks` | ✅ 函数级 `_run_task` 277→79；文件级 tiers/runner/dag/replan 四刀 | `1be0443`、`29c2884`、`22a8862`、见台账追加二 |
| 4 `skill` | ⛔ **不做**（本方案 §3.4 的结论：26 引用点、验证成本 > 收益） | 台账 PT-20261031-02 |

切完后实际规模：`tasks.py` **1971 → 487 行**；五个文件合计 2110 行（含新增的模块 docstring 与门面）。
判据全过：路由集合逐字相同、`dir(tasks)` 零丢失、L0 全绿零跳过、L1 全绿。
翻案条件（片4）：`skill.py` ≥2550 行，或出现「改一处要开 5 个函数」的实际事故。
