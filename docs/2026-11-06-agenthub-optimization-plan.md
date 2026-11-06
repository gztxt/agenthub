# AgentHub 全面评估与优化方案（2026-11-06）

> 范围：功能逻辑 / 系统框架 / UI 设计 / 全部前后端代码；联网对标 GitHub 同类仓库（wigolo 故障，改用 GitHub API）。
> 接单四查：记忆 3 条｜知识库 0 相关｜技能 0 命中｜参考库不可用（degraded）。16 题全选 A 推荐档。

## 落地结果

| 项 | 结论 | 证据 |
|---|---|---|
| A5 原子写 | PASS 已改 | `src/ghsettings.py` 临时文件+fsync+os.replace；`tests/test_ghsettings_atomic.py` 2 条；`test_ghsettings*` 29 passed |
| C2 `.bak` 归档 | PASS | `bak_archive.sh --apply` 归档 140 份/31MB，sha256 140/140，仓内 394→254 |
| U1 断点单源 | PASS 零改动 | `tests/test_narrow_first_paint.py` 钉 767，14 passed |
| U2 终端重连 | PASS 零改动 | `static/hub/03-agents-cards.js:54-144` 心跳+退避+对账重连已完整 |
| B2 bundle 单源 | PASS 零改动 | `scripts/build_hubjs.sh` + `tests/test_hubjs_split.py` 逐字节钉死 |
| 全量回归 | PASS | `run_tests.sh hermetic` 3894 tests OK，0 skip |
| A1 拆 main | **冻结** | 见下 |
| 死模型 `alibaba/deepseek-v4-pro` | 按用户口径跳过 | `model_gate` 已报出（1 次探针实测） |

## A1 冻结理由（实测）

- `main.py` 1500 行：33 路由已在 `api_*.py`，残留 7 端点 + 中间件/装配/生命周期，`health()` 重活已委托 `healthx/selfattest/memview/gwprobe/modelgate`。
- 拆装配不减耦合；生命周期 5 个后台 loop 共享全局态，拆需新容器，净收益 0。
- 动 `/health` 风险面最大（前端 15s 轮询 + 探针 + MCP 消费方），且 L0 不 import main，拆分不解锁新断言。
- 后续若拆：顺序为 `health()` 装配 → `term` 1626 行，而非先拆 main 装配。

## 对标借鉴（只借鉴模式，不引整套框架）

- `cft0808/edict`（17k★）：审计分级 → 映射到 `audit` 只读门面。
- `JPeetz/Hermes-Studio`：审批流 → 映射到 `approvals` 三态。
- `antiv/mate`：RBAC → 映射到 `writeauth` 豁免复核（已选先复核清单）。

## 约束与未闭合

- 生产重启护栏：`term_sessions>0` 禁重启，后续改动走 `hub-restart.sh`。
- 脏改接手：`/api/datastore` + `retention` 字段已随重启上线，保留未拆。
- `main.py` 若日后真拆，须先完成脏改验收并补 L0 闸门。
