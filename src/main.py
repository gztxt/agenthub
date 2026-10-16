"""Agent Hub - 主入口（Agent_Manager 融合版 v0.3.0）

融合自 Zafer-Liu/Agent_Manager (Apache-2.0) 的设计与语义：
- Hook 遥测端点（agent_http.rs → src/hook.py）
- 三层记忆中心（memory 子系统 → src/memory.py）
- 端口管理（ports.rs → src/ports.py，只读，无 kill —— NAS 军规）
- 项目类型自动识别（agent_sources.rs → src/sources.py）
"""
import asyncio
import hmac
import json
import os
import re
import sys
import time
import uuid
from collections import defaultdict, deque
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, Optional

# 必须在导入 config 前加载 .env
from dotenv import load_dotenv
env_path = Path(__file__).parent.parent / ".env"
if env_path.exists():
    load_dotenv(env_path)
    print(f"[Agent Hub] 已加载 .env: {env_path}")

import aiohttp
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import HTMLResponse, JSONResponse, Response, StreamingResponse
from fastapi.staticfiles import StaticFiles
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel

sys.path.insert(0, str(Path(__file__).parent))
import db
import modelcfg
from config import config
from discovery import AgentDiscovery, AgentInfo
from ports import list_listeners, port_in_use
from sources import detect_project
import scanner
from writeauth import write_gate
from registry import build_adapters, get_adapter
import hook as hook_mod
import memory as memory_mod
import memfed as memfed_mod
import kb as kb_mod
import skill as skill_mod
import dataset as dataset_mod
import tasks as tasks_mod
import mcpgw as mcpgw_mod
import cronjobs as cronjobs_mod
import term as term_mod
import term_record
import profiles as profiles_mod
import vitals as vitals_mod
import embed_proxy as embed_proxy_mod
import selfattest
import staticguard
import tdai_client
import gwprobe                      # 上游网关（CCR）连通性 + 模型注册清单的缓存式体检
import healthx                      # /health 派生量的纯函数层（L0 不 import src.main，故抽出来）
import resilience                   # 出站韧性层：退避重试 + 熔断（/health 的 breakers 字段）
import settings_bundle              # 配置包导出/导入（PT-20260927-15，JSON，不引 YAML）
import sessions_export as export_mod
import export_batch                   # 导出取消息的批量 IN（L0 可测的纯函数层，同 staticguard 范式）
import models_cache                   # 模型清单的成功路径缓存（同上，纯函数层）
import writeauth                    # 导出端点按写端点同等鉴权（复用 decide 的 fail-closed）
import audit as audit_mod           # 资产变更审计的只读查询门面（GET /api/audit/list）
import runlog as runlog_mod         # 运行日志：三中心检索留痕 + GET /api/runlog 查询门面
import cloudcli as cloudcli_mod     # CloudCLI 项目直达：项目清单（直读 auth.db）+ 会话启动代理
import localprojects as localprojects_mod  # 本机项目清单（多根 git 扫描 + cloudcli 合并）
import resources as resources_mod          # 资源监控：Agent 进程资源列表 + 一键结束
import githubprojects as github_mod  # GitHub 远端仓库清单 + 即时克隆（本机项目页的远端半程）
import prefs as prefs_mod           # 应用级偏好 KV（v0.13.36）：两项目页收藏/隐藏落服务端
import modelcfg as modelcfg_mod     # 各 Agent 默认模型统一设置（v0.13.41：设置→模型子菜单）
import ghsettings as ghsettings_mod  # GitHub 地址/key/落点（v0.13.42：设置→GitHub 子菜单）
import hublog as hublog_mod        # 日志中心（v0.13.46：设置→日志子菜单，journald + 操作事件聚合）
import api_dataset as api_dataset_mod  # P0-1：数据集/经验库只读门面（无写端点）

# C2（2026-10-12）：领域路由从本文件抽成独立 module —— main 只留组装职责。
import appstate                     # 组装方写、领域方读的共享可变状态（discovery）
from api_agents import router as agents_router, _chat_dispatch   # 启动期 chat_fn 回调仍要它
from api_sessions import router as sessions_router
from api_models import router as models_router
from api_vitals import router as vitals_router, agent_profs_for_vitals
from api_system import router as system_router   # 系统级：前端「需重启」按钮的自重启端点

print(f"[agenthub] 配置: PORT={config.port}, HOST={config.host}")

# 单一版本源：/health、FastAPI 元数据、启动横幅与页脚都取这里
VERSION = "0.13.115"  # 出站韧性层 + 依赖锁版本 + token 取用 + 知识索引 + 配置包导入导出（PT-20260927-12/13/14/15）
#   叙事全文见 CHANGELOG.md 同版本；此处只留「为何这么改」的根因。
#   ① **出站韧性层**（src/resilience.py）：本机8 处裸 urlopen 零重试。借鉴
#      nathangtg/agent-hub 的熔断器 + 限流退避，但**只抄语义**（上游是 async + 硬时钟
#      单例，本机是同步 urlopen + 多线程）。**429 不计熔断失败** —— 限流是对端健康的
#      表现，算失败会在对端最需要被调用时主动切断。**Retry-After 必须封顶**
#      （对端可写 3600，照单全收会挂住线程池 worker 一小时）。**半开只放一个探针**；
#      `_leave` 必须在抛异常那条路也执行，否则半开永不自愈（比一直开闸更糟）。
#      ⚠ `vitals.http_ok` 刻意**零重试**：它在 L4 探活路径上，AGENTS.md 立着「探活最多 2 次」。
#      接了 4 个**只读幂等**调用点；写路径不接。`/health.breakers` 让熔断从黑盒变可观测。
#   ② **依赖锁版本**：9 个裸包名零版本号，又无 pyproject/uv.lock ⇒ 一次 venv 重建就可能
#      装到 breaking 版本而无人察觉（NAS 无 CI 兜底）。版本取自生产 venv `pip freeze` 实测值。
#   ③ **token 取用**（`agent-hubctl.sh token`）：随机 token 只 print 前 4 位到 journald
#      ⇒ 首次接线（手机/另一台机）只能去日志挖全文，而日志会滚走。这是**可用性**改善，
#      不是放宽鉴权（四条终端路由仍全过 `_check_term_token`），故只打 stdout、不写 journald。
#      外部评估把它误判成「没鉴权」——真痛点是取用方式。
#   ④ **README 瘦身 + 知识索引**：228 行里 160 行是逐版本施工流水（迁 `docs/HISTORY-features.md`，
#      原文逐字保留）；更关键的是仓内对 `agent-knowledge`（155+ 篇）**零指针**。
#      新增 `tests/test_knowledge_index_l0.py` 钉「索引里的编号必须解析得到真实文件」——
#      它能验指针有效，**验不了**判据是否仍与现状一致，所以索引节必须同时写明
#      「结论绑定取证日期，以代码与实测为准」。
#   ⑤ **配置包导入导出**（src/settings_bundle.py）：画像=代码定义+运行时注册，重装全靠手工。
#      **不导出 mcp_servers 与 .env**（机器事实+凭据）；自由文本过既有脱敏器；
#      导入**幂等 upsert**且不清本机独有条目；未来版本**显式拒绝**（尽力解析＝字段改名
#      静默丢配置）；导出虽是 GET 但**按写方法判鉴权**（批量读＝数据外流，同 audit.py 口径）。
#      **一律 JSON，不引 YAML**（零新增第三方依赖是硬约束）。
#   ① **P0-a 成本归因**：`adapter.chat` 本就返回 model+usage，`_run_task` 却只取
#      total_tokens 就丢、`model` 一次没读过 ⇒ 任务↔模型↔token 无法 JOIN。
#      取 adapter 回包值而非我方请求值；缺则留 NULL 不兜底；五条落库路径都记 ——
#      验收失败照样花钱，成本不随 verdict 消失。
#   ② **P0-b 验收落库**：P0-3 已实现「执行者不自证」，但三态只折叠进 status/error
#      就丢了。新增 verdict+verify_reason；**NULL 严格区别于 'fail'**（NULL＝未走到验收：
#      等待/预算/自称失败/超时，记成 fail 会虚增失败率）；`insufficient` 是第三态。
#   ③ **潜伏缺陷（非本次引入）**：超时分支把 '超时熔断 %ds' 写成字面量却按 5 个参数传
#      （SQL 只 4 占位符）⇒ ProgrammingError 在**异常处理器内部**抛出，任务永远 running。
#   ④ **标签型产物**（src/outputguard.py）：免费池会把请求路由到审核/分类模型，返回
#      'User Safety: safe' 这类标签而非正文，而 nonempty 只判非空 ⇒ 标签被记成「成功
#      产出内容」。三档 content/label/empty + 恒非空 reason；只认强信号（模型属审核族，
#      或「标签: 值」且值属分类**封闭枚举** —— 靠长度判据拦不住 'User Safety: <中文正文>'）。
#   ⑤ **账本归一口径**（src/ledgernorm.py）：账本是**只读生命线资产** ⇒ 只在读取侧归一。
#      只剥**网关前缀**（反复剥）**不剥厂商前缀**；`:free` 分两轴（成本 canonical /
#      能力 base）；provider 内部逻辑名拆两半 —— 名字合并、**协议形状**必须留。
#      实测 98→91 key、合并 12398 行（33%）。
#   ⑥ **清 test_boundary 既存红**：期望 400 实得 401 —— 测试从没走到它声称在测的那道闸。
#      根因：模块头 `os.environ.setdefault("TERM_TOKEN", 假值)` 是**空操作** ——
#      config.py 的 `load_dotenv(override=True)` 无条件用仓内 .env 覆盖环境。改送
#      `term.TERM_TOKEN` 本身，**先过鉴权**、**再验边界**。守卫见 test_token_fixture_hygiene
#      （子进程先 import config 仍须全绿 ⇒ **顺序无关**已自动盯住）。
#   ⑦ **P0-c 模型边界画像**（src/modelprofile.py）：勘察确认 CCR /v1/models **不给任何
#      能力元数据**、账本**无输出正文** ⇒ 只出**边界**（能否成/多快/多贵/哪些协议形状），
#      语义能力不编。**最关键：429 是负载信号不是质量信号**，须单独计并从质量口径**剔除** ——
#      实测把 `agnes-3.0-flash` 44.0%→99.8%、`qwen3.7-max` 40.2%→93.3% 翻成「近乎完美」，
#      混算就会把这三个永久误判成「一半都在失败」。三态与 verifier 同构：ok /
#      insufficient_sample（不是模型差，是没量够）/ unstable。阈值实测得来。
#   闸门：L0 **1848/0/0**、对账 1913=1913、L1 65/0/0；新测 70 例
#   （task_attribution 14 + outputguard 13 + ledgernorm 22 + token_hygiene 4 + modelprofile 17）。
#   ⑨ **迁移专项验收脚本** tests/verify_migration_p0ab.py（六组判据、一轮取齐、不建 pty）：
#      与 verify_prod_smoke **互补不重叠**（那个查通用健康，这个只管迁移）。
#      A4 钉「存量行 model/verdict 必须 NULL」；A5 钉「/api/audit/list 必须 401」。
#      写它时踩两坑：**① 验收脚本自己制造了要验证的变更** —— 幂等判据原本直连生产库调
#      `ensure_schema`，首跑必然加列 ⇒ 误报 FAIL；已改副本（与账本 mode=ro 同源纪律）。
#      **② 三列没到位时脚本直接崩**（no such column）⇒ 没出报告；已降级为「不适用」并跑完。
#      ⇒ **验收脚本崩掉等于没有报告，它必须能在任何状态下把话说完。**
#   v0.13.113：接 D18。三件都是「让闭环**可比、可追溯**」，不是新功能。
#   ① **D5 轨迹行 schema**：新增 `TRAJECTORY_SCHEMA="trajectory/1"` +
#      `normalize_trajectory()`（补 `seq`/`metrics`，幂等）+ `validate_trajectory()`
#      （回缺口清单，不抛）。管线 `build_items()` 改用它落轨迹 ⇒ 每条轨迹形状一致。
#   ② **D13 版本推进**：新增 `dataset_versions` 快照表 + `bump_version()`/`list_versions()`；
#      `eval_is_stale()` 把「跑完才发现题集被改过」当场判陈旧（拿不到评估行也回 True）。
#   ③ **D16 badcase 回写**：`dataset_items` 幂等 ALTER 补 `last_eval_id/last_verdict/
#      last_score/last_eval_at`（旧行 NULL=未判，不回填）；`record_eval_result()` 三道硬闸
#      （verdict 三态、题必须属该数据集、**陈旧评估不许回写**）；`badcases()` = 回写过且
#      `verdict != pass`（含 insufficient；没跑过的不算）。
#   ④ 只读路由加 `/api/dataset/{id}/versions`、`/api/dataset/{id}/badcases`。
#   闸门：tests/test_dataset_d5d16.py（17 例）。
#   v0.13.112：P0-1 的 **D18**（经验接入记忆联邦）。经验库若只躺在 db 里就不是「记忆」。
#   ① `memfed.REGISTRY` 新起一路 `experiences`（kind=`hub_sqlite`，weight=0.65，enabled）：
#      查的是 hub **自己的** agents.db（同进程连接，毫秒级），不是第二份权威副本。
#      新增 `_search_experiences/_recent_experiences`（`RECENT_Q` 哨兵路）与 `_probe_one` 分支。
#   ② **只召回活跃条**：`retired_at IS NULL`（退役条只能按 id 追溯，不回流）。
#   ③ 进默认集：`memory.FED_FAST_SOURCES` 加 `experiences` ⇒ 默认检索/注入包都含它；
#      `hubmcp.hub_memory_search` 的默认值本就从 `enabled_ids()` 派生，自动跟随。
#   ④ 前端同源：`templates/index.html` 源选多一个 option、`SRC_ABBR` 加 `EX`
#      （`tests/test_center_ui_l0.py` 钉「前端枚举 = REGISTRY」）。
#   闸门：tests/test_experience_fed.py（7 例）。
#   v0.13.111：P0-1 的**收尾**（接切片 A/B/C）。
#   ① **真实轨迹源**：`telemetry_events` 在本机**实测仅 1 行** ⇒ 闭环没有燃料。
#      新增 `hook.build_traces()/traces_from_store()`：从 hub 自己的 `profile_events`
#      （实测 2970 行、438 行带 trace_id）按 **trace_id** 分组成执行轨迹，
#      管线加 `--source profile|telemetry`（默认 profile）；新增 `GET /telemetry/traces`。
#   ② **抽取**：`experience_mine.extract_trajectories()` + `parse_experience()`（解不出就
#      **不入库**，不拿自然语言填结构化列）；`--extract --limit N` 走 hub 自己的 LLM 通道，
#      非会话一律 `openrouter/free`。
#   ③ **验收③ 真实取证**（scratch DB，真数据）：真实轨迹 **65** 条 → 数据集 **40** 样本
#      （train 36 / holdout 4，`assert_train_only` 对 holdout 真抛）；free 模型抽取 3 条轨迹
#      → 入库经验 **1** 条（另 2 条模型没给出可解析经验 ⇒ 按设计丢弃）→
#      召回 `query='list'` **命中 1 条且带真实 trace_id 来源**。
#   ④ 守卫缺口修正：`assert_free_model` 原先只认 `openrouter/free`，会把军规写的
#      **经 CCR 形式 `openrouter/openrouter/free`** 也拒掉 ⇒ 已一并认。
#   v0.13.110：P0-1 的**切片 B/C**（接切片 A 的四表）。
#   ① `scripts/dataset_pipeline.py`（**声明式管线**）：轨迹 → 确定性过滤 → 确定性采样 →
#      按 grain 哈希切分 → **模型在最后**（`enrich()`）。`build_items()` 是纯函数，`output`
#      恒为空；`assign_split(grain_hash,frac)` 只由 grain 哈希决定 ⇒ 重跑不洗牌。
#   ② `src/hook.py` 轨迹级聚合：`build_trajectories()` 按 `(source,session_id)` 分组保序，
#      `trajectories_from_store()` 只读拉事件；新增 `GET /telemetry/trajectories`。
#   ③ `scripts/experience_mine.py`（AMR「Dreaming」）：**合并重复 / 删过期 / 解冲突**，
#      三类 reason `duplicate`/`conflict`/`expired`；只**退役**（`retired_at`+`superseded_by`）
#      不物理删。`plan()` 是纯函数，`apply_plan()` 才落库。
#   ④ `src/api_dataset.py`（只读路由）：`/api/dataset/*` + `/api/experience/*`；
#      `/api/dataset/{id}/train` 当场用 `assert_train_only()` 自证不含 holdout。
#   ⑤ 模型纪律：非会话一律 `openrouter/free`，`openrouter/auto` 直接 `ValueError`
#      （`assert_free_model`，两脚本共用）。
#   闸门：tests/test_dataset.py / test_dataset_pipeline.py / test_experience_mine.py。
#   未做：systemd user timer（unit 属受保护面，待单独授权）⇒ 本批只做端点触发。
#   v0.13.109：P0-1 切片 A。
#   闭环位置：轨迹（hook/profile_events）→ **数据集** → 评估（eval_run）→ **经验库**（experiences）。
#   ① 新增 src/dataset.py：四表 `datasets/dataset_items/eval_runs/experiences`（幂等建表，
#      在 main lifespan 与 tasks 同处创建）。
#   ② **grain 唯一键**：`grain_hash = sha256(grain + 规范化 key_fields)[:32]`，
#      `UNIQUE(dataset_id, grain_hash)` ⇒ 同一逻辑样本重复写入**复用同一行**
#      （否则同一件事会灌进数据集多次、指标失真）。
#   ③ **split 隔离（验收②）**：`holdout` 样本**永不**进 train 批次；`assert_train_only()`
#      是给切片 B 管线用的硬闸（掺入即抛 ValueError）；已入样样本的 split **不可改**。
#   ④ **字段映射显式**（手册 D7）：input/output/trajectory 三列各义，input 空⇒拒；
#      `eval_runs` 记 `dataset_version` 快照（D13）；`experiences{situation,action,
#      precondition,source_trace_ids}`（D17），退役走 `retired_at`（不物理删）。
#   闸门：tests/test_dataset.py（17 例）。切片 B（管线）/C（经验挖掘）/调度载体待做。
#   v0.13.108：P0 施工排序第四项（P0-3）。治的是「**执行者既是被测对象、又握着判定权**」。
#   ① 新增 src/verifier.py（叶子模块，纯函数）：三态 `pass` / `fail` / `insufficient`。
#      **判不了绝不放行**（未知 kind / 产物缺失 / 判据缺失 ⇒ insufficient，永不 pass）——
#      这是 P0-4 `evidence.py` 口径在**产物验证层**的复用。fail 带结构化缺口（id+detail）
#      与 `repairable`，调用方据此决定重跑还是上报，不解析自然语言。
#   ② tasks 状态机扩到十态：新增 `waiting_input|waiting_approval|paused`（**等待态**）
#      与 `verifying`（交产物后、判定前的短暂态）、`cancelled`（终态）。
#      `_run_task` 不再信 `result["success"]` 就置 success：先置 verifying，再由
#      verifier 判产物；未过则 failed（错误里带 verifier 缺口）。
#      执行者回 `{"waiting": "waiting_input"}` ⇒ 任务进等待态、**让出执行资源**、
#      run 挂起等外部输入（不空转、不误判「疑似悬挂」）；下游 pending 保持 pending。
#   ③ 预算：tasks 幂等 ALTER 补 `verify_kind/verify_expect/budget_ms/budget_tokens/
#      spent_ms/spent_tokens`（旧行 budget=NULL=不限、verify_kind='nonempty'）；
#      `budget_exceeded()` 是纯函数（超限一律 failed，不静默放过）。
#      新增 `POST /api/tasks/{run_id}/resume|pause|cancel` + `requeue_waiting()`。
#   ④ 闸门 tests/test_verifier.py(19) + tests/test_task_statemachine.py(12)；
#      其中「回包 success=True 但产物为空 ⇒ **不得**置 success」是 RED→GREEN 的负向主判据。
#   v0.13.107：P0-5 两处**改判**（用户 2026-10-15 裁定，均指向「一句话自称」的余留形态）：
#   ① **label 按 kind 取词**：原先 `enforced_covered` 一律叫「只读」，但 `exec` 边界
#      讲的是「拉起进程走唯一受闸入口」，不是「文件只读」⇒ 对终端型 agent 用词过强。
#      现改为 `LABEL_BY_KIND = {"config_write": "只读", "exec": "受闸"}`，未登记 kind
#      保守叫「受控」；纯观测仍只一个说法「未观察到写操作」。
#   ② **`covered` / `fail_closed` 不再手填布尔**：旧登记表写 `{"covered": True}` 正
#      是 P0-5 要治的形态。现改为逐条枚举**绕过面清单**（每条挂一个证明「该路径被拒」
#      的测试节点）+ `fail_closed_by`，由 `derive_coverage()` 机械导出：
#      `covered` = 清单非空 **且** 每条路径的 AST 节点都真实存在；
#      `fail_closed` = `fail_closed_by` 节点存在。删/改清单或节点 ⇒ 自动不算覆盖。
#      并加冻结钉钉住绕过面 id 集合，防静默缩水（“覆盖全路径”从手填布尔→对已枚举
#      绕过面的断言；枚举是否完整仍是人判，但必须逐条列出、逐条可对账）。
#   ③ 闸门 tests/test_boundary.py 28 例 + 14 subtests（含 RED→GREEN 实阅）。
#   v0.13.106：P0 施工排序第三项（P0-5），治本仓旧账「纳管 ≠ 可用 / 只读未证」
#   （09-23 opencode 首扫假阴性同族）。手册 C12 给的判据是：一个边界只有**同时**
#   满足 ①覆盖全部绕过路径 ②fail-closed（判不了就拒）③有负向测试证明绕过被拒，
#   才可称**强制边界**；缺任一条 ⇒ 文字上只能写「未观察到写操作」，不得写「只读」。
#   ① 新增 src/boundary.py：**纳管四维** = 强制/观测 × 覆盖/未覆盖，恰好四个格子
#      （`enforced_covered` / `enforced_uncovered` / `observed_covered` /
#      `observed_uncovered`）。核心不变量是**只降不升**：`classify_claims` 把
#      强制声明折成四维时，唯一升级通道是 `NEGATIVE_PROOFS` —— 一张
#      「kind → 真实存在的负向测试」登记表；登记表里找不到 ⇒ 自动降级为
#      observed_uncovered，且只有 `enforced_covered` 才配 `label="只读"`。
#   ② 接线：`profiles._enrich` 给**每个画像**挂 `boundary`（`profiles.boundary_claims`
#      数据驱动声明：有 terminal ⇒ exec 声明为强制）；`scanner.add_boundary` 给扫描
#      产物挂 `observed_product()`（第三方服务只能观测，永不 enforced）；
#      `discovery.AgentInfo` 增 `boundary` 字段 ⇒ 四维进 `/api/agents` 契约。
#   ③ tests/test_boundary.py：负向测试登记表的节点用 AST 对账（改名即红）+
#      绕过路径真被拒（无终端入口 agent ⇒ 400、token 错 ⇒ 401；写闸门缺口令 ⇒
#      misconfig、错口令 ⇒ deny，**从不放行**）。
#   v0.13.105：P0 施工排序第二项（P0-4）。要解决的是本仓**反复出事的那一种静默**：
#   「判不了」在二元口径里没有位置 —— 于是它被读成「没问题」。原话是「禁把 SKIP 当 PASS」，
#   写在纸上很久，每次都得靠人记得；本批把它做成**可断言的对象**。
#   ① 新增 src/evidence.py（叶子模块，不读盘不打网络不碰 db）：三层链路
#      `原始事实 facts → 候选信号 signals → 研判 judgments`。**唯一不变量**：
#      证据缺失 / 观测失败（`Fact.ok=False`）/ 规则引用不存在的事实 / 规则抛异常 /
#      规则回 None —— 五种「判不了」**一律** `insufficient`，永不退化成 `dismissed`。
#      两条配套口径：**空证据集不许投票**（零覆盖 ≠ 零问题）；`insufficient`
#      的 `confidence` 留 `None`（不许编假刻度）。
#   ② `summarize()` 的 `decisive` =「禁把 SKIP 当 PASS」的机械形态；
#      `unjudged`（**从来没判过**）与 `insufficient`（判过了、判不了）**分开计数**，
#      但**同样**让 `decisive=False`；`require_decisive()` 就是「insufficient 不走放行」。
#   ③ asset_audit 增 `verdict/severity/confidence` 三列（`init_db` 幂等 ALTER 补建）。
#      语义**严格限定**为「本事件记载的变更在 hub 本地是否确实发生」——每个调用点都在
#      落库**之后**才写审计，所以「行存在」本身就是证据，默认 `confirmed`；
#      **不**声明上游（TDAI / 远端 GitHub）效果，核不了就显式传 `insufficient`。
#      `verdict IS NULL`（迁移前旧行）保持 NULL = **未研判**，**不回填** —— 回填等于替历史编证据。
#   ④ `GET /api/audit/verdicts`：把审计行当事实过一遍三层，回**四字段结果对象**
#      `exec_status + validity + evidence + quality`（`validity` 三态而不是布尔）。
#      与 `/api/audit/list` **共用 `_require_read`**（有 AST 钉子钉住「两个端点都走它」）。
#   ⑤ scripts/run_tier.py 同批拆出四字段产物并以 `[gate] {json}` 一行输出；
#      **L0 出现 skip ⇒ `validity=insufficient` + 退出码 2，永不再报绿**；
#      「0 例」也不再是绿（判据没被扫到 = 发现器坏了）。
#   闸门：tests/test_evidence.py(24) + tests/test_audit_verdicts.py(18) +
#   tests/test_gate_result_fields.py(18) + tests/test_asset_audit.py 新增 10 例。
#   v0.13.104：用户逐条裁决 aliyun《AI Agent Handbook》融合评估的四项待裁定后立项施工
#   （P0-1~P0-5 全批；排序 P0-2 → P0-4 → P0-5 → P0-3 → P0-1，逐项串行），本条是第一名。
#   缺口：runlog 只记「成不成 / 多快」，没有 token / cost ⇒「单位成功任务成本」不可度量。
#   ① 新增 src/usage_ledger.py：CCR 账本 `~/.claude-code-router/app-data/usage.sqlite`
#      的**只读**聚合门面（`?mode=ro`，写不进去有负向测试钉着）。聚合维度白名单
#      `client/model/provider/credential_id`，**故意不含 `logical_model`**
#      （2026-09-07 判例：那是 CCR 内部逻辑名，不是客户端/模型 ID，误用致 jcode 不可用）。
#   ② `COALESCE(cost_usd,0)` 求和**必须同时回 `null_cost` 与 `cost_sources`（含 "" 桶）
#      —— 2026-10-15 实测：免费模型落库 `cost_usd=NULL`，静默当 0 就是把成本报小。
#   ③ 新增 `GET /api/runlog/cost`（与 `/api/runlog` **共用 `_require_read`**，防改一处漏一处）；
#      账本拿不到一律 **503 + ok:false**，绝不回 rows=[]/total=0 那种静默 0。
#   ④ src/llm.py 发 `X-Request-Id`（新 `new_request_id()`）。**单次只读探针实测 CCR 不透传**
#      （带 `hubprobe-…` 打 /v1/messages 得 200，账本落的是 CCR 自生成的 UUIDv4
#      `19eccbce-…`）⇒ 回包 `attribution=approximate`（按 client×model×时间窗近似归因），
#      调用方不得当作精确对齐。同批给 `chat_tools_loop` 加 `request_id` / `usage_sink`
#      两个**关键字**参数（不改 `(answer, steps)` 返回形状，memory.py/tasks.py 零改动）。
#   闸门：tests/test_usage_ledger.py + tests/test_runlog_cost_api.py（共 22 例，全 L0）。
#   余下四项与验收/回滚口径见 docs/AI-AGENT-HANDBOOK-P0-立项施工-20261014.md。
#   v0.13.103：用户要求评估 Cognition「Agent Memory Repo」能否融入本机记忆中心。
#   **规范本身无检索层（纯 grep），不构成替代**；评估反而照出本机记忆中心三个硬缺陷：
#   ① 联邦融合被 TDAI 吃满（W=1.0 + 满额 top10 + 无下限 ⇒ 数学上其余 10 路永不出结果），
#      实测 20 查询里 TDAI 独占 200/200 槽位、逐源单查平均 5.2 路有货却只出 1 路；
#   ② `score` 是 TDAI 原始分直通（距离类、量纲不可比），乱码查询 0.52 > 真命中 0.03；
#   ③ 会话起始注入包无 q 时**根本不查联邦**（fed.chars=0）却仍报 sources=3 ——
#      「计数绿、内容空」，pi 门面正走这条链路。
#   修复见 src/memory.py 的 FUSE_TOP_FRAC/_rank_cap 与 src/memfed.py 的 RECENT_Q。
#   评估全文（含证据边界与 P1/P2 建议）见 docs/AGENT-MEMORY-REPO-评估-20261013.md。
#   v0.13.102：用户「嵌入式中断现在又反应很慢，之前修复一次速度很快，现在又开始慢了；
#   另外 grok 无法正常启动」。两条**互不相干**的根因，都在「一个同步动作挂在
#   **高频路径**上」这个家族里（与 v0.13.100/101 同源），但触发面不同：
#
#   ① grok 起不来（`src/term.py`）：`pty.fork()` 给的 winsize 是 **0×0**，而原实现
#      只在 Session 上填 rows/cols 两个 **Python 属性**、**从不调 ioctl** ⇒ 子进程
#      开局拿到「终端 0 行 0 列」。真 pty 实测（只差这一个变量、各 3 轮一致）：
#        0×0   ⇒ grok 只吐 128 字节就**永久停住**，不发 `Grok Build` 界面
#        120×40 ⇒ 4.3s 正常渲染
#      而且**事后补尺寸救不回来**（先 9s 0×0、再 TIOCSWINSZ 到 120×40 ⇒ 新增 0 字节，
#      grok 已把「没有尺寸」读死，SIGWINCH 唤不醒）。别的画像在 0×0 下照常出界面
#      （claude/codex/opencode/qodercli），是 grok 独有硬失败。
#      `_apply_size` 又拿被硬编码的「80×24」去跟这个**假值**比 ⇒ 前端首帧 resize 若
#      恰是 80×24，短路生效、真 ioctl 永不下发 ⇒ pty 一直停在 0×0（这是「刷新也没用」
#      的第二层：刷新会清屏并让前端重发 resize，但只要尺寸与宣布值相同就仍不下发）。
#      改法：`Session.__init__` fork 后立刻 `_set_winsize(fd, INITIAL_ROWS, INITIAL_COLS)`
#      （抽成函数供 `_apply_size` 共用、也为了可测），宣布值 == 真值从此一致。
#      端到端 A/B（同一探针、只差这份代码）：修复后 4.6s 出界面 / 未修的生产 NEVER。
#      闸门 tests/test_term_size_owner.py::TestInitialSize（含红向自证：把 _set_winsize
#      抹成 no-op，同一断言必须能看出 0×0）。
#
#   ② 「又变慢」（`src/main.py` + `src/db.py`）：SQLite 的**自动**检查点是「每积满
#      wal_autocheckpoint(默认 1000) 页，就在**触发它的那条写语句**里同步 checkpoint」。
#      而 term_record 的逐帧写（每个按键 / 每帧输出一条 INSERT+commit）正落在这条路上
#      ⇒ 每 ~183 帧（1000 页 ÷ ~5.5 页/帧）就有一帧被同步 checkpoint 拖住。100 万行
#      真库副本实测 4000 帧：
#        wal_autocheckpoint=1000（默认）⇒ 尖峰 22 次，单帧 p99 **6.9ms** / max **116ms**
#        wal_autocheckpoint=0（关自动）  ⇒ 尖峰 **0** 次，单帧 p99 **0.17ms** / max 16ms
#      关闭自动检查点后 WAL 仍会增长（实测 4000 帧 → 88MB），所以**必须**有替代触发点：
#      `db.checkpoint()` + `main.wal_checkpoint_loop()`（默认每 120s、走 to_thread、
#      PASSIVE 不抢独占），总 fsync 次数不变，只是换了执行位置。启动期仍保留
#      init_db 的一次 `wal_checkpoint(TRUNCATE)` 做彻底收口。
#      为什么不改 db.py 的 PRAGMA 默认值：那是**连接全局**语义，engine/MCP 侧不该被
#      本模块的取舍顺带改掉；改在 hub 主连接 + 后台循环里，影响面就一个对象。
#      ⚠ 本条的取值取证属本机（NAS 卷、4 核），绝对值不宜外引；方向性结论
#      （自动检查点在逐帧写路径上 ⇒ 尖峰归零）是机制性的。

#   v0.13.100：用户「嵌入式终端会话文字输入非常慢 很不正常」。**根因不在终端**——
#   `Recorder.feed()` 的全局预算检查是 `SELECT SUM(bytes) FROM term_recordings`，
#   EXPLAIN 实测 `SCAN term_recordings`（表上只有 idx_termrec_sid(session_id,seq)，
#   **盖不住全表聚合**），而 feed() 被**同步**调在 asyncio 事件循环的两处：
#   term.py:1221 的 pty 读回调（= 每帧输出）与 :1488 的 WS 收包（= 每次按键）。
#   生产库 44.7 万行 / 157MB（/fs 是 trimafs 卷）时单次 SUM 实测 **110~124ms**
#   ⇒ 整个事件循环被独占上百毫秒，别的会话/WS/HTTP 一起卡。生产真机逐键
#   RTT p50 = 677ms、max = 784ms。
#   改法（src/term_record.py::total_bytes）：进程内缓存 (字节数, 库路径)。首次/换库
#   后首次真查；每次成功 INSERT 按 len(blob)（脱敏后的落盘值）递增且写在 `if ok`
#   里（写失败不许推进）；任何 DELETE（sweep / 同 sid 覆盖）**一律作废**——
#   增量法遇 DELETE 会**永久偏高且不可逆**，故不做减法修正（行数≠字节数）。
#   换库判据用 `db.current_path()`（3µs，比它挡掉的 117ms 便宜四个数量级），
#   **不用连接对象 id**：`init_db` 复用同一模块全局，id 会撞。
#   实测：离线 444,040 行 feed p50 67.92ms → 0.114ms（1405 倍）；端到端 A/B
#   （隔离影子 44 万行预置，两组只差这一个文件）逐键 RTT p50 56.54ms → 0.24ms。
#   闸门 tests/test_term_record_total_cache.py（9 例，红向自证 3 向均转红）；
#   完整取证与集成说明见 docs/INTEGRATE-v0.13.100.md。
#   ⚠ 未治本：`sweep()` 只在会话 close 时跑，表一天长 44 万行。缓存把每帧成本
#   从「随表线性」压成常数，**表本身仍会长**——治表属另一次范围变更。
#   ⚠ 另一处漏网（同一批发现）：一个正确的局部优化，**如果它的失效条件被高频路径
#   命中，等于没优化**——本条的「DELETE 一律作废」正被`Recorder.__init__`
#   每次建会话都命中，所以「每帧」那条路治好之后，「建会话」那条路原样留着。
#   v0.13.101：用户「彻底消除」。上条的 ⚠ 未治本正是本条要收的尾——缓存只把每帧
#   成本从「随表线性」压成常数，**建会话这条路仍然是全表 SUM**：
#   `Recorder.__init__` 先 DELETE（同 sid 覆盖）再立刻算预算，v0.13.100 的
#   「DELETE 一律作废缓存」规则 ⇒ 必然重查 ⇒ 生产库上每次建会话端到端实测
#   **156~210ms**（其中 ~108ms 是这条 SUM），同步独占事件循环。
#   改法：单行计数表 `term_rec_meta(k='total_bytes')` + 两个数据库侧触发器
#   （`trg_termrec_ins` AFTER INSERT 加 / `trg_termrec_del` AFTER DELETE 减），
#   写入与计数更新在同一事务里。
#   `total_bytes()` 变成一次主键点查（实测 3µs），**与表大小无关**，
#   也不再存在「什么时候作废」这个问题。存量库由 startup 的
#   `term_record.ensure_counted()` 幂等播种一次（只在计数行缺失时才做那一次 SUM）。
#   同时给 `ts` 补索引（`idx_termrec_ts`）：`sweep()` 的 `DELETE WHERE ts < ?`
#   原本 SCAN 全表（77~568ms，挂在会话收尾路径上），改 SEARCH 后 0.06ms。
#   实测：录制表 48 万行时total_bytes() 108ms → 0.00ms；每帧 INSERT 代价
#   0.043ms → 0.063ms（+0.02ms，相对 5ms 合并窗口可忽略）。
#   闸门 tests/test_term_record_meta_count.py（计数与 SUM 恒等、DELETE 收敛、
#   无SUM 回流、播种幂等）+ 真浏览器 work/probe-term-render-latency.py。
#   ⚠ 播种这条路上**另有两处竞态**，同批自查发现并修掉（详见 CHANGELOG 四之二）：
#     ① 播种原本是两条语句（先 SELECT SUM、再 SET v=excluded.v 覆盖）——
#        48万行 SUM 的那几十~上百 ms里写入的帧被覆盖抹掉，
#        生产库实测计数少 11251 字节 ⇒ 计数偏低 ⇒ 预算偏松。
#     ② 更严重：播种闸门原判「计数行在不在」，而 trg_termrec_ins 在行不存在时
#        会**顺手创建**它 ⇒ 并发首帧落在「查行」与「写值」之间时闸门误判
#        「已播种」⇒ 跳过 ⇒ 历史全漏算（实测 4200 万字节漏到剩 1.4 万）。
#   修法：播种合成**单条** `INSERT…SELECT … WHERE true … ON CONFLICT`（同一快照），
#   闸门改用独立的 `seeded` 标记（触发器只碰 total_bytes，不会伪造它）。
#   ⚠ 别把播种改回「先插占位行 v=0 再 v=v+SUM」：占位行期间写入的帧已计过一次，
#     再加 SUM 会双计（实测缺口 -1.4亿，比原写法更糟）。
#   ⚠ 别把闸门改回判「计数行在不在」—— 见 ②。**这两处的教训是：单语句化解决的是
#     「读-写窗口」，不解决「判据被被测系统自己伪造」。**
#   v0.13.99：用户「嵌入式终端会话文字输入框无法正常使用键盘的上下左右光标键…输入框只是
#   不需要鼠标焦点 但是键盘的全部原生功能都是需要的」，并明确要求**滚轮向上翻不许受影响**。
#   **先取证再动手**，三轮真机实测（真 chromium + CDP，真鼠标/真键盘事件，pty 侧
#   `stty -echo -icanon` + `cat -A` 逐键对账，work/probe-arrow/ 可复跑）：
#     ① ★ v0.13.83 那行 `textarea.style.pointerEvents='none'`（当时为解决「输入框与内容
#        显示抢鼠标焦点致滚轮不能上翻」而钉）**没有**打断键盘：实测点终端后焦点照样落在
#        `.xterm-helper-textarea`、方向键 `^[[A/B/C/D` 全到 pty。**滚轮与键盘是两条独立
#        通路**（pointer-events 只管命中测试，不管 JS focus()；xterm 的 `focus()` 是
#        `textarea.focus({preventScroll:true})`）⇒ 修键盘**不能**回退那行，否则滚轮回归。
#     ② 真缺口在**焦点离开终端之后**：字符接力器原只放行 `e.key.length === 1`，于是
#        方向键 + Enter/Backspace/Tab/Home/End/PageUp/PageDown/Insert/Delete **全部消失**
#        （pty 收 0 份，浏览器也没拿它做别的 ⇒ 键彻底消失、无任何提示）。
#        改前那句注释「非可打印键一律放行」在本场景下等于「所有键都丢了」。
#     ③ 修法 = 把「编辑键」从「单字符」放宽为**显式映射表**（TERM_EDIT_KEYS），
#        逐个映到标准 VT 序列交给 `term.input()`。**不放开所有非单字符键**：放行等于把
#        语义交还浏览器（方向键变滚动页面、F5 被浏览器吃掉），而终端输入位上这些键的
#        语义**只有一个来源：pty**。Ctrl/Cmd/Alt 组合键与 Escape 仍整体放行。
#   实测（tests/verify_term_editing_keys.py，L2 真机 14/14）：
#     · 滚轮组 R0~R4 全绿：viewportY 379→369→358→**0**（连续上翻到顶、不卡中段），
#       `pointerEvents` computed 仍为 `none` ⇒ 用户点名要保的功能零回归；
#     · 键盘组 K1/K2 13/13：焦点在终端内与**终端外**两种状态下，13 个键实收全部等于
#       期望序列（`^[[A/B/C/D`、`^[[H/F`、`^[[5~/6~/2~/3~`、`^I`、`$`、`^?`）；
#     · K3 搜索框内按方向键/Delete：pty 收 0 份 ⇒ P2-11「不得劫持真输入位」不破。
#   回归：tests/test_term_editing_keys.py（L0，12 例，含**滚轮护栏** 3 例）
#        + tests/verify_term_editing_keys.py（L2 live，14 例）。
#   ⚠ 探针判据本身也踩了三轮坑（cat -v 不转写 Tab/换行 ⇒ 假红；translateToString(false)
#     满宽补空格 ⇒ delta 失效），已用**纯 pty 实验**（不经浏览器）逐键对拍选型出 `cat -A`
#     统一记法。教训：**先证明判据测的是被测对象，再拿它判红**；最强自证是
#     「改前即成立的对照组 K1 也红」⇒ 那两次红是判据自己的问题。
#   v0.13.98：用户「右上角 github 链接只保留图标和版本号」。**减的是「看得见的字」，
#   不是入口本身** —— href / target / rel / title / aria-label 五项全留，外链安全属性
#   与「地址可被抄走」这两条旧裁定都不受影响（tests/test_header_footer_trim.py 仍守）：
#     ① 顶栏 .hstats 是 `flex-wrap:nowrap + overflow:hidden`，右缘先被切的是**最右**那一项。
#        仓库入口刻意放在本组最左（v0.13.74 已为此挨过一次投诉：状态字被视口右缘切断），
#        但它仍常驻 14 字符宽度；换成 `v0.13.98` 后宽屏只占 8 字符，窄屏仍只留图标。
#     ② 版本号走 **Jinja 变量 `{{ version }}`**，不写死字面量 —— 本文件这行是唯一真相源，
#        `/` 路由早就把 `{"version": VERSION}` 传进模板（main.py:1115），此前模板里一个
#        变量都没用过。写死就会出现「/health 报 0.13.99、顶栏还写 0.13.98」的**无告警漂移**
#        （本仓 CHANGELOG 顶部版本漂过 13 版、tests/README 计数漂过两轮，都是同一形态）。
#        闸门 tests/test_header_footer_trim.py 断言模板里是变量而非字面量。
#   回归：tests/test_header_footer_trim.py（图标 / 外链三要素 / 窄屏档 / 版本单一真相源）。
#   v0.13.97：用户「右边终端框上下左右边距缩小60%」，当日复核追加「**所有页面都是这个尺寸**」。
#   **先把「边距」是哪一层取证清楚**（1440×900 截图 + getBoundingClientRect 逐层分解，
#   tests/verify_term_box_geom.py 可复跑），再动手 —— 否则很容易改错层：
#     ① 右侧内容区留白的**唯一来源是 `main` 的 padding**（原上/下 12px、左/右 16px）。
#        `.chat-grid`/`.chat-main` 的 padding 都是 0；`.chat-grid` 的 gap:16px 不产生
#        任何列间距（它已是单列 —— .chat-side 被 display:none 且宽屏规则覆盖成1fr）。
#     ② 黑底**内侧**的 `#termEl` / `.term-body` padding 四向已是 0，且被上一轮定案钉死：
#        FitAddon 按 #termEl 内容盒算行数，父层多1px 就多算一行、底部被裁一行。
#        「终端框边距」很容易被实现成「给黑底加内衬」—— 那是本版最大的坑，闸门守着。
#     ③ ⚠ **范围是全站，不只终端页**。第一版写成 `main:has(> #page-chat.on)` 只收窄终端页，
#        用户当日复核后否掉：「点左边菜单栏的其他菜单时右边页面边距又变了，应该全部统一」。
#        `:has()` 在这里是**多余的第二判据** —— 它让同一份留白出现两种值。
#        故改为直接改基准（4.8/6.4）、删掉整条覆写：用户要同一个值，就只留一处声明。
#        **教训**：用 `:has()` 做「某页特殊化」之前，先确认用户说的是「这一页」还是
#        「所有页」——本例默认答案是「所有页」。
#     ④ 抽 `--main-pad-y/x` 为唯一真相源（此前宽屏裸 `12px 16px`、窄屏裸 `8px` 是两份
#        独立字面量，各改各的）。**两档各自 ×0.4、不是同一绝对值**：窄屏基准是 8/8，
#        照抄宽屏的 4.8/6.4 只缩到27%，不是用户要的 60% ⇒ 窄屏取 3.2/3.2。
#   实测：逐页 18 页 × 2 档，main padding 唯一取值 = 宽屏 (4.8,6.4) / 窄屏 (3.2,3.2)
#   （tests/probe_all_pages_gap.py）；终端页净留白 宽屏 13/17/13/17 → 6/7/6/7、
#   窄屏 9/9/9/9 → 4/4/4/4；黑底与容器尺寸差 0×0（FitAddon 口径未破）。
#   回归：tests/test_term_box_gap.py（6 例，L0）+ tests/probe_all_pages_gap.py（逐页，L2）
#   + tests/verify_term_box_geom.py（几何，L2）。红基线：在第一版上逐页探针报
#   「各页main padding 不一致：[(4.8,6.4),(12,16)]」= 精确复现用户报的现象。
#   ⚠ 真 xterm 实例的 rows×行高是否放得下，本机 headless 量不到（termLoadRenderer 的
#     WebGL addon 在 --disable-gpu 下会挂住 renderer，试 4 版无结论后按军规停手），
#     **该项仍是缺口，待端侧确认**。
#   v0.13.96：用户报「cursor 历史会话记录使用英文」。**根因是取错了源，不是编码/locale**
#   （往编码方向查会跑偏，半天找不出东西）：
#     ① `~/.cursor/chats/<md5(cwd)>/<sid>/meta.json` 有个 `title` 字段，本机 99 条里 5 条
#        有值，**无一例外全是 Cursor 服务端生成的英文**（`Cursor Startup Garbled Characters`…），
#        而这些会话的用户原话是中文 ⇒ 谁优先取谁，就决定整排显示哪种语言；
#     ② 修复＝判据收成**唯一函数 `_cursor_title`**：用户原文优先、服务端标题当**兜底**（D2）。
#        列表侧与 title_for 侧**共用这一份** —— 本仓已因「两处各写一遍优先级」走过两次弯路，
#        这次按教训直接设计成不可漂移，而不是修完这次就留下第二次的入口。
#   v0.13.94 改了仓名/远端/systemd 单元，但**前端与 /health 的展示层还写着旧名** ——
#   靠一次八层健康巡检才发现（判据：把 hub.js 拉下来 grep 旧单元名，命中 1 处）。
#   三处都是「无消费方 / 不报错」类型，所以没有任何自动机制会提醒：
#     ① static/hub/05-chat-and-history.js:330 的降级文案「后端未更新：需重启 X 后生效」
#        —— 这条**在用户排障时给指令**，写旧名等于教用户去重启一个已停用的单元
#        （restart 它既起不来、Restart=always 又会在 journald 里刷重试）；
#     ② src/main.py /health 的 "service" 字段（纯展示，但它是运维第一手看到的标识）；
#     ③ src/main.py 启动横幅 `[Agent Hub]`。
#   为什么这次能一次改对：v0.13.94 已把「三份真值互相比对」固化成
#   TestUnitNameConsistency，本版再加 3 例把**前端源码 / 构建产物 / ?v= 提手**纳入比对：
#     - test_frontend_user_visible_text_names_current_unit：源码不得含旧单元名；
#     - test_built_bundle_is_in_sync_with_sources：产物不得含旧名，且 `?v=` 必须等于
#       产物 md5 前 8 位（手改产物不跑 build 脚本会被这条抓住）；
#     - test_health_service_field_uses_current_name：/health 的 service 字段。
#   红向自证三种漏改形态（源码退旧名 / 产物残留 / 提手脱钩）全部转红，复位转绿。
#   刻意**不改**的三类旧名：注释里的历史叙事与判例原文、下载文件名
#   （agent-hub-sessions.json 等，改会破坏既有用户习惯）、MCP server 名（外部契约）。
#   2026-10-08 用户指令：「重启 agent-hub.service 但是为什么名字还是agent-hub 已经更新名字
#   为agenthub」→「改名并同步更新」。10-08 仓名（目录）与远端已改齐，AGENTS.md 当时明写
#   「服务名不变」⇒ unit 名是**刻意留下的**，本版补上。
#   本版的要害不是 `mv` 文件，是**漏改不报错**：`journalctl -u <不存在的单元>` 返空且
#   退出码 0 ⇒ 「代码里的单元名与真实 systemd 单元脱钩」这种故障全程无异常，表现只是
#   「设置→日志页空白、而 /health 全绿」。本版实测红向对照：新单元近 5min 47 行 vs
#   旧单元 0 行 —— 拿旧名查就长这样。所以必须同批改的三处（前两处承重）：
#     ① src/hublog.py 的 DEFAULT_UNIT（`journalctl -u` 的单元名）：漏改＝日志页静默空白；
#     ② src/scanner.py 的 EXCLUDE_PATTERNS：漏改＝端口扫描把自家 3102 登记成「外部服务」，
#        资源页多一条自己家的条目；它是**前缀**匹配（影子/备份单元也要排除，勿写死全等）；
#     ③ ~/bin/agent-hub-watchdog.sh 的看护清单：漏改的症状最隐蔽 —— 脚本照跑、日志照写，
#        但 `svc_alive agent-hub` 恒 false ⇒ 每 5min 误判掉线并重启旧单元，**真服务无人看护**，
#        而日志里全是「已恢复」。
#   另同步 deploy/ 归档副本、scripts/hublog-cli.py 文案、tests/verify_hist_tdz.py 探针命令、
#   ~/bin/service-health-check.sh 探针标签；**判例与旧会话里的 `journalctl -u agent-hub`
#   命令一律不改**（那是当时的取证记录，改了就失真）。
#   新闸门 tests/test_hublog.py::TestUnitNameConsistency（5 例）把「代码常量 / 仓内归档
#   副本 / 真实 systemd 单元」三份真值互相比对，旧名副本残留判红。
#   切换手法：本会话跑在旧 unit 自己的 cgroup 里（`codebuddy ← python3` 属
#   agent-hub.service），而 `KillMode=control-group` ⇒ stop 会连坐杀掉调用者，故走
#   `systemd-run --user` 的独立 transient unit（work/unit-switch-agenthub.sh）。

# ── 以下为 v0.13.93 的根因（保留供追溯，非本版条目）──
#   前八家（grok/claude/qoder/jcode/hermes/codex/opencode/cursor）都在 src/sessions_store.py
#   的 SESSION_STORES 里登记了「盘上仓库 → 条目 → resume argv」三段映射；codebuddy 之前
#   **只在菜单里**（profiles 有终端入口），点开没有历史下拉 —— 与 v0.13.83 修 cursor 前同款。
#   三处与 claude 不同、逐条实测钉死（详见 sessions_store._t_codebuddy 的注释）：
#     ① 行判据是 `type=='message' and role=='user'`（不是 claude 的 `type=='user'`），
#        content 块类型是 `input_text`/`output_text` ⇒ 不能复用 `_first_user_text`；
#     ② 桶名是 `compressPath(realpath(cwd))`（`/ \ :`→`-`、合并连续`-`）而不是 slug；
#     ③ `--resume <id>` **按起 pty 的工作目录定位会话**（dist 里 findExistingSession 调
#        sessionManager.get 不传 cwd ⇒ 只剩当前进程 cwd 那个桶）⇒ 与 cursor 同构，
#        必须自带「桶名对得上才列」的可续判据，否则点了必然 SessionNotFound。

# ── 以下为 v0.13.92 的根因（保留供追溯，非本版条目）──
#   根因（2026-10-08 用户报障：「agent-hub 嵌入式终端 页面文字无法选择 复制，
#   按右键显示是图片」）——两个症状一个源头：终端是 WebGL 渲染器画在 <canvas> 上的。
#   ① **选区在松手瞬间被自己清掉（真缺陷，探针红绿可分）**：
#     xterm 的 CoreMouseService 协议一从 NONE 变非 NONE，就会调
#     `SelectionService.disable()`，其实现是 `clearSelection(); _enabled=false`
#     （vendor/xterm.js 逐字实证）——**把跟踪态装回去 = 当场把刚拖出的选区抹掉**。
#     时序正是体感：拖选（mousedown 复位 ⇒ 选区能建）→ 松手（window mouseup 上挂的
#     `setTimeout(termMouseArm)`）⇒ 选区在用户看到之前没了 ⇒「选不中/复制不动」。
#     真 chromium + CDP 实测（修复前）：程序化建选区后调 termMouseArm()，
#     `term.getSelection()` 由 '❯ echo …' 变 ''；只调 termMouseResetNow() 则保留。
#     修法（三处，缺一不可）：(a) termMouseArm() 在 `term.hasSelection()` 时**不回装**
#     （鼠标此刻归浏览器），回装时机从 mouseup 挪到 wheel（termWheelNow 里、无选区时才装）；
#     (b) 有选区时在 `?h` CSI 处理器里**吞掉 app 的鼠标跟踪 DECSET**（claude/codex 每帧
#     重画都重断言 `?1000;1002;1003;1006h`，不挡的话协议又被翻回非 NONE、选区照样被清）；
#     (c) 换会话时 `term.clearSelection()`（term.clear() 不清选区 ⇒ 残留选区会让新会话的
#     上报通道建不起来，实测 opencode A1 rep=0 转红）。
#   ② **右键给的是「图片另存为」**：浏览器眼里 canvas 就是一张图，没有文字的「复制」项；
#     容器上那条原生 `copy` 兜底因此永远等不到触发。修法：接管 contextmenu、自出菜单
#     （复制 / 粘贴 / 全选）。浮层纪律按 AGENTS 4.2：唯一入口 termCtxOpen、唯一出口
#     termCtxClose、点空白（capture pointerdown）/Esc/失焦/滚动即关，触屏可逃生。
#   闸门：`tests/test_term_select_persist.py`（L0 静态契约 + 红基线）+
#   `tests/verify_term_select_persist.py`（真 chromium 真渲染：选区抗 mouseup / 抗 app
#   重断言、右键菜单出项/复制落盘/点空白关；红绿对照＝打到 v0.13.91 实例 10 FAIL、
#   打到修复态 0 FAIL）。
#   本批为**纯静态前端**（static/hub/02、03 + templates/index.html + 重建 static/hub.js），
#   下次页面加载即生效，不需要重启服务。

# ── 以下为 v0.13.91 的根因（本版摘要：吞备用屏时同步清屏 —— jcode 启动期残影），保留供追溯，非本版条目 ──
#   ⚠️ **范围更正（2026-10-08 自审）**：不得写成「根治」。已证并已修的是「客户端 resize
#   之前那段 245B 真实回放里的残影」；用户报的「乱码」是否就是它，**无端侧证据**
#   （稳态跑起来改前改后都干净，jcode 自己的 2J 会自愈）。详见 CHANGELOG v0.13.91 顶部横幅。
#   根因（2026-10-08 用户报障：「agent-hub 的 jcode 启动嵌入式终端时会带入乱码」）：
#   ① **报障形态与病灶**：v0.13.83 为治 codex「终端滚不动」，把 DECSET 的 1049/1047/47
#   注册成**吞掉**（termAltScreenBlock，内建 activateAltBuffer 不执行），终端恒在主屏。
#   内建 `?1049h` 的语义是 `saveCursor + 进备用屏`，而 xterm.js 的备用屏是**初始空白的新缓冲**
#   —— 只吞、不切缓冲 ⇒ 全屏 TUI 会在**旧画面**上按绝对坐标作画：hub 写在第 0~1 行的头部
#   （「jcode · 终端」「提示：点『新会话』…」）与 jcode 的 `Connecting to server...` 留在屏上，
#   和 TUI 首帧叠在一起 —— 用户看到的「乱码」就是这团叠加，真终端里它会被备用屏的空白画布盖掉。
#   ② **修法与两条硬判据**：吞掉的**同一刻同步** term.clear()，等价于那块空白画布。
#      (a) 必须**同步**调用：`term.write('\x1b[2J')` 会被 xterm 的异步 write 队列排到本帧
#      之后、把刚画好的 TUI 一起抹掉（实测那一档：整屏空白、TUI 全丢）；term.clear()
#      直接操作缓冲、同步生效，同帧其后的 TUI 字节照常落在干净画布上。
#      (b) 判据从「只看 p[0]」改为「任一参数命中」：DECSET 允许合并（`?1049;1003h`），
#      只认 p[0] 时 `?1003;1049h` 会漏吞 ⇒ 照旧进备用屏。
#   ③ **取证与闸门**（真 chromium + 本仓 vendor xterm + 真 hub 影子 ring 原始字节）：
#     吞而不清 ⇒ 第 0~2 行残影（`jcode · 终端` / `Connecting to server...`）+ TUI 正常；
#     吞 + 同步 term.clear() ⇒ 屏面干净、TUI 完好。
#     ⚠️ 稳态会自愈：jcode 在客户端 resize 后会自己发一次 `\x1b[2J` 整屏重画（全量 ring
#     3824B 里 2J ×1）⇒ 改前稳态也干净 —— 报障的「乱码」是**客户端 resize 之前**那段
#     真实回放（实测 245B，含 `Connecting to server...` + `?1049h`）停留的窗口。
#   ④ **已知差异（如实登记）**：重复 `?1049h` 本实现会重清，xterm 内建不重清；
#   实测 jcode 一次会话只发 1 次（喂输入 + 59 对同步块重画后仍 1 次），codex 走
#   --no-alt-screen 不发、claude/opencode 开机各一次 ⇒ 现实里不会命中。
#   闸门：`tests/test_term_altclear.py`（5 例静态契约，含分片/产物一致）+
#   `tests/verify_term_altclear.py`（喂真实 245B 前缀，3 项真渲染，
#   `HUB_ALT_CLEAR_DISABLE=1` 红向自证）+
#   `tests/verify_term_altclear_live.py`（真 hub+真 jcode 稳态/不回归 7 项；其文件头写清
#   它不能区分修复前后，别当红绿判据）。
#   本批为**纯静态前端**（static/hub/03-agents-cards.js + 重建 static/hub.js），
#   下次页面加载即生效，不需要重启服务。

# ── 以下为 v0.13.90 的根因（本版摘要：会话计数不再依赖终端口令 + 终端复制粘贴（焦点归还 / 原生粘贴通道）），保留供追溯，非本版条目 ──
#   根因（2026-10-07 用户同日两项报障）：
#   ① **顶栏「会话数」一时显示一时不显示**：该指示读的是 localStorage 里的
#      `hub.term.token`，没口令就直接放弃渲染。而 localStorage **按 origin 隔离** ——
#      同一个页面从局域网 IP、Tailscale 名、loopback 三个源进来各有一份存储，
#      只在其中一个源上配过口令时，另外两个源的顶栏就整块空白；口令过期被清、
#      或用户换了访问方式，同样会静默消失。更糟的是「读不到」与「真的零会话」
#      在屏幕上长得**一模一样**，用户无法区分。修法＝新增**免 token** 的只读聚合
#      端点 api/term/activity（只回 alive 计数与每 agent 条数，不含 sid/cmd/cwd，
#      故不绕开 P1-7 对 /api/term/sessions 的口令闸门），前端改读它；口径同时按
#      用户要求补齐为「在跑 N · 会话 M」两个数都给（N=有活会话的 Agent 数、
#      M=活会话总条数）。
#      A/B 实测（同一 profile、同一时刻、都无 token）：生产旧版 busy=""，
#      新版 busy="在跑 1 · 会话 3"。
#   ② **嵌入式终端无法复制粘贴、连键盘快捷键都用不了**：两条独立的腿。
#      (a) 复制兜底 `termCopyFallback` 走 execCommand 时会把临时 textarea `select()`
#          ⇒ **焦点被抢到 body**，而 xterm 的键盘通路完全依赖它的 helper textarea
#          持焦 ⇒ 用户复制一次之后所有按键都不再进终端（"快捷键全哑"）。实测：
#          复制前 focus=termTA、复制后 focus=BODY，此后 pasteEvt 恒 0。
#          修法＝记下复制前的焦点宿主并在 finally 里原样归还。
#      (b) Ctrl+V 被 xterm preventDefault ⇒ 浏览器原生粘贴被掐断；而改前那条 JS
#          路径依赖 `navigator.clipboard.readText`，它在**非安全上下文**
#          （局域网 http，正是用户实际访问方式）压根不存在 ⇒ 粘贴整个不可用。
#          修法＝Ctrl/Cmd+V 一律 return false 但**不** preventDefault：只让 xterm 别把
#          ^V 当字节送进 pty，浏览器照常把剪贴板粘进 helper textarea ⇒ 触发 paste
#          事件 ⇒ 由既有的 termPasteBind 接住做 bracketed-paste 安全包装。
#          这条路不需要 navigator.clipboard，非安全上下文照样通。
#      红向自证：撤掉 (a) 的焦点归还 ⇒ A/C/C2/D/E 五条同时变红（正是用户的完整症状）；
#      只撤掉 (b) ⇒ C/C2/D 红而 A 仍绿，两条腿互相独立。
#   ③ 闸门：L0 hermetic **1332 例 0 跳过**；hermetic-clean 同数字全绿；L1 host 57/58
#      （唯一红是 grok 会话标题解析的**既有**失败，主树复现，与本批无关）。
#      新增 `tests/test_term_clipboard_handoff.py`（6 例静态契约）+
#      `tests/verify_term_clipboard.py`（13 项真渲染，局域网非安全上下文全绿）+
#      `tests/test_activity_indicator.py` 补 4 例（免 token / 不漏句柄 / 两个数都给）。
#   ④ 不动 pty 输出、不动 xterm 版本、不改任何凭据：本次只动一个只读聚合端点与
#      前端三处行为，重启即可全部生效（无迁移、无数据面改动）。

# ── 以下为 v0.13.83 的根因（本版摘要：终端恒在主屏（吞 ?1049h）+ 输入控件去鼠标 + cursor 历史会话接入），保留供追溯，非本版条目 ──
#   根因与修复（三条，2026-10-07 用户裁定）：
#   ① **备用屏从根上禁止**：v0.13.82 是「换会话那帧补写退出序列 + 手点逃生按钮」，
#      用户裁定改为嵌入式终端**只用主屏** —— 在解析层把 DECSET 的 1049/1047/47 注册成
#      「吞掉」（返回 true，内建 activateAltBuffer 不执行），其余 DECSET（鼠标/粘贴/
#      同步块）一律放行。实现见 static/hub/03-agents-cards.js 的 termAltScreenBlock()。
#      v0.13.82 的 TERM_ALT_OFF/TERM_STATE_RESET、缓冲区状态字
#      #termBufChip 与「退出备用屏」#termAltOut 全部删除（备用屏不会发生，它们已是死码）。
#      实测依据：registerCsiHandler({prefix:'?',final:'h'}) 与内建 DECSET 共用一张表，
#      且该表在建内建处理之前被咨询。⚠️ handler 收到的是**参数数组本身**（`p[0]===1049`），
#      不是 `{params:[…]}` —— 按 p.params 取参会抛 TypeError 被 catch 吞掉、静默失效
#      （第一版就是这样，靠验收探针 B3 抓红）。
#   ② **输入控件关闭鼠标功能，只保留页面滚动**（用户判据：Windows 自带终端里滚轮就是
#      向上翻页面内容，嵌入式终端也该如此，两边不抢鼠标焦点）。终端页有两个文字输入控件：
#      查找框 #termFindInput 按**开/关**分档 —— 关闭时（.term-find:not(.on)，即日常所有时刻）
#      整条浮层 pointer-events:none，鼠标完全穿透、滚轮不被截走；打开时恢复 auto，
#      Ctrl+F 期间鼠标照常可用（初版一律 none，等于顺手把 Ctrl+F 拆了半边，用户当日追认收紧）。
#      xterm 自带的 .xterm-helper-textarea 只靠 vendor CSS 不够 —— 它静息态是 0×0/left:-9999em，
#      但组字期间 updateCompositionElements 会把 inline left/top/width/height 改写到
#      光标处并撑开（inline 压过 class）⇒ 运行时钉 style.pointerEvents='none'。
#      滚轮因此只滚 scrollback。鼠标上报的转发逻辑**不动**（TUI 主动请求鼠标时照旧转发）。
#   ③ **cursor 接入历史会话**：cursor 之前只出现在菜单里（CLI_ALIASES 有、SESSION_STORES 没有）
#      ⇒ 前端 TERM_HIST_AGENTS 不含它，点开菜单行没有历史下拉。新增 cursor_json 适配器：
#      以 ~/.cursor/chats/<md5(cwd)>/<id>/meta.json 为主表（有 cwd 与毫秒时间戳、有
#      hasConversation），转录 ~/.cursor/projects/*/agent-transcripts/<id>/<id>.jsonl 只供
#      标题（首句用户提问）；录制的行形状是 role 键而非各家的 type 键，故单设 _cursor_first_user。
#   ⚠️ 已知残留（不在本次收口）：上游 open（#14277/#10331/#20063/#23651）指出 codex 在
#   普通屏整屏重画时仍可能丢 scrollback（\x1b[2J 把 viewportY 拽回底部，xterm.js#5801 未修）
#   —— 本次只解决**备用屏**这一条腿。

# ── 以下为 v0.13.81 的根因备忘（保留原文，不删；与 v0.13.82 是**两条不同的腿**）──
#   根因：TUI 程序开启 xterm 鼠标跟踪（1003h）后滚轮/拖选被吞，异常退出不发
#   ?1003l 时 mouseTrackingMode 卡 any 只有重连才复位 ⇒「时好时坏、无法复制」。
#   修复：capture 阶段 wheel/mousedown 看门狗，检测到跟踪态同步切
#   coreMouseService.activeProtocol='NONE' 并异步写 TERM_MOUSE_OFF 到 pty。
#   ① **闸门自己会说谎，所以先修它**：`FORCE_COLOR=3` 让 node 把 `console.log(数字)`
#     染成 ANSI ⇒ `int('\x1b[33m60\x1b[39m')` 直接 ValueError ⇒ 22 条用例**假红**。
#     而「全绿/全红」是本轮一切判据的前提 —— 判据本身不可信时，后面八批的
#     「已验证」全是自欺。`NO_COLOR` 在 `FORCE_COLOR` 存在时**无效**（node 自己
#     警告后忽略），唯一可靠解是从 `run_tier.py` 里摘掉变量。这类**假红比没有
#     闸门更坏**：它逼人改断言求绿。本仓的假红禁令见 `tests/tiers.py:107-114`。
#   ② **性能主症是缓存缺失，不是算法慢**：`/api/skill/list` 174ms 每次全盘重扫、
#     `/api/agents` 57ms 里 `docker ps` 独占 16.5ms、`/api/kb/status` 冷启动 1.49s ——
#     而 `/status` 早有 60s TTL 范式，只是没被其余四条路由复用。**逐字搬运**式的
#     「抽取而非编写」也用在这里：历史根因搬进 CHANGELOG 是同一手法。
#   ③ **闸门盲区下的漏网比缺陷本身更值得记**：`renderTaskTable` 四处裸拼 innerHTML
#     （LLM 返回的 `task_id` 无字符集校验直达 DOM ⇒ 存储型 XSS），**而同一函数
#     下一格有 `escapeHtml`** ⇒ 证明是漏网非有意。三处同型，所以新闸门的主判据是
#     **形状**（同行两种写法）而不是逐点列举 —— 后者修完就忘。
#     同类教训已在册 6 次（TDAI 透传三错、chat 端点每请求 500、WS 双消费者偷字节…）。
#   ④ **只缓存成功路径**：模型清单那次我先写成「fetch 没抛异常 ⇒ 成功」，影子实测
#     证明错 —— `async with s.get()` 连接失败时**不抛异常**，只回空清单 ⇒ 缓存了失败
#     （用户看到「没有模型」，真因是上游连不上）。判据必须是 `payload["error"]` 为空。
#   ── v0.13.70 及更早的逐版根因已于 2026-10-05 归档至 ──
#   ── CHANGELOG.md（脚本 scripts/extract_changelog.py 逐字搬运）──
#   此处**只保留当前版本**的根因，避免两份会各自漂移的副本。
#   （上一版 v0.13.78 的根因随本批归档至 CHANGELOG.md §v0.13.78）原三条：
                      #   ① **收起态图标条第 4、5 个形状完全一样**（用户 2026-10-04 报「分不清是什么」）。
                      #     真因是 `cpu` 一次被用了**三处**：静态「资源」、`NAV_ICONS.agents`、
                      #     `SET_PAGES` 的「模型」—— 收起态里前两处**并排可见**，直接造成误读。
                      #     一次清完三处：「资源」cpu → **`monitor`**（它监视 CPU+内存+进程，
                      #     cpu 这个隐喻本来就偏窄）；「AGENTS」cpu → **`hubmark`**（hub 中心 +
                      #     4 个 agent 节点 + 辐条，语义正对）；「模型」保留 cpu（AI 模型常见隐喻）。
                      #     两处**顺带查过** `monitor`/`hubmark` 是否已被导航占用 —— 只在
                      #     agent 卡片徽标与页眉 logo 用，不在图标条，避免「改了 A 又造出 B 的撞车」。
                      #   ② **`/list?q=` 与 MCP 工具描述口径统一**：MCP `tools/list` 的
                      #     `description` 原走 `escapeHtml` ⇒ 星号原样显示。现与技能描述同走 `mdInline`。
                      #   ③ **顺带修一处 XSS 洞（超出用户点名范围，必须报）**：MCP 工具行里
                      #     `<b>' + x.name + '</b>` 与 `onclick="pickTool('' + x.name + '')"`
                      #     **既没转义、也没过 `jsStr`** —— 同一个串同时进 **HTML 正文**与
                      #     **onclick 的 JS 字面量**两个语境，两处都不设防。工具名来自 MCP server
                      #     （外部注册）。本仓 `jsStr` 的注释早就写明「`escapeHtml` 只处理 HTML
                      #     上下文，HTML 实体转义**不足以**让任意串安全进 JS 字面量」，此处却没用。
                      #     现补：HTML 语境 `escapeHtml`、JS 字面量语境 `jsStr`。
                      # ── v0.13.70 及更早的逐版根因已于 2026-10-05 归档至 ──
                      # ── CHANGELOG.md（脚本 scripts/extract_changelog.py 逐字搬运）──
                      # 此处**只保留当前版本**的根因，避免两份会各自漂移的副本。

#: 常驻后台任务统一登记处。裸 create_task 不持引用 ⇒ 事件循环只持弱引用，
#: GC 可能在任意时刻回收掉这些循环任务（表现为「跑着跑着某功能静默停摆」），
#: 且 shutdown 时无法 cancel，进程退出要等事件循环超时。
_bg_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


app = FastAPI(title="Agent Hub", version=VERSION)

#: `/api/models` 的成功缓存，按 adapter 分桶（claude / jcode / …）。
#: 模块级 = 跨请求存活，这是缓存的本意；不随 app 重启清空是刻意的
#: （模型清单重启后立刻重取一次即可，不需要持久化）。


def _parse_cors_origins() -> list:
    """CORS_ORIGINS 逗号分隔解析；异常/为空时退化为仅回环来源（不放松默认安全）"""
    # 原默认值写死了三个内网 IP。换个网段（Tailscale 重新分配、或搬到别的 LAN）就静默失效：
    # 页面能开但所有写端点 400 CORS，用户只会看到「按钮点了没反应」。
    # 改为 env 驱动、**默认只给回环**（更严，且本机自用不受影响），跨网访问显式配 CORS_ORIGINS。
    # 绝不放宽到 "*"：allow_credentials=True 时浏览器会直接拒绝通配来源。
    raw = os.getenv("CORS_ORIGINS", "http://127.0.0.1:3102,http://localhost:3102,http://[::1]:3102")
    try:
        origins = [o.strip() for o in raw.split(",") if o.strip()]
        for o in origins:
            if not (o.startswith("http://") or o.startswith("https://")):
                raise ValueError(f"invalid origin scheme: {o}")
        if not origins:
            raise ValueError("empty CORS_ORIGINS")
        return origins
    except Exception as e:  # noqa: BLE001
        print(f"[Agent Hub] CORS_ORIGINS 解析失败（{e}），退化为仅回环来源")
        return ["http://127.0.0.1:3102", "http://localhost:3102", "http://[::1]:3102"]


app.add_middleware(CORSMiddleware, allow_origins=_parse_cors_origins(),
                   allow_methods=["*"], allow_headers=["*"], allow_credentials=False)

# ── 非 MCP 写路径按 IP 滑动窗口限流（风格对齐 mcpgw._check_rate）──────
API_RATE_PER_MIN = int(os.getenv("API_RATE_PER_MIN", "60"))
# ⚙设置口令：查看 TERM_TOKEN 等敏感配置时要求提供（为空则设置页不可用）
HUB_PASSCODE = os.getenv("HUB_PASSCODE", "")
_api_rate: Dict[str, deque] = defaultdict(deque)
_RATE_EXCLUDED_PREFIXES = ("/telemetry/events/", "/health", "/mcp")
_RATE_WRITE_METHODS = ("POST", "PUT", "PATCH", "DELETE")
#: P0-6：_api_rate 是进程级字典，每个见过的 IP 留一个 deque **永不删除**。
#: 唯一来源是内网/反代，但 IP 会随设备休眠、DHCP 续租、容器重建不断变化 ⇒ 长时间运行
#: 内存单调增长（mcpgw 同型缺陷）。上限按「记住多少个 IP 还在限流窗口内」取，
#: 超出就整条丢弃（该 IP 下次请求会以 0 计数重新开始，等价于放过一次，超了仍会 429）。
_RATE_MAX_IPS = int(os.getenv("API_RATE_MAX_IPS", "4096"))


def _rate_prune(now: float) -> None:
    """回收 _api_rate 里已经过窗口的 IP 条目（每个 IP 至少来过一次才会被记）。"""
    if len(_api_rate) <= _RATE_MAX_IPS:
        return
    cut = now - 60
    stale = [ip for ip, w in _api_rate.items() if not w or w[-1] < cut]
    for ip in stale:
        _api_rate.pop(ip, None)
    # 极端情况：所有 IP 都在窗口内（真实 DDoS 或超大 NAT）⇒ 超额部分按最旧的整条丢，
    # 保证表本身有硬上界，不因来客太多而无限长。
    while len(_api_rate) > _RATE_MAX_IPS:
        oldest = min(_api_rate, key=lambda ip: _api_rate[ip][-1])
        _api_rate.pop(oldest, None)


@app.middleware("http")
async def api_rate_limit(request: Request, call_next):
    # 只拦写方法；GET（前端 30s 轮询）与外部推送/健康检查/MCP（自带限流）不拦
    if request.method in _RATE_WRITE_METHODS:
        path = request.url.path
        if not any(path.startswith(p) for p in _RATE_EXCLUDED_PREFIXES):
            ip = request.client.host if request.client else "unknown"
            _rate_prune(time.monotonic())
            window = _api_rate[ip]
            cut = time.monotonic() - 60
            while window and window[0] < cut:
                window.popleft()
            if len(window) >= API_RATE_PER_MIN:
                print(f"[rate] 429：{ip} 超过 {API_RATE_PER_MIN}/min（{request.method} {path}）")
                return JSONResponse(
                    {"detail": f"API 限流：超过 {API_RATE_PER_MIN}/min"}, status_code=429)
            window.append(time.monotonic())
    return await call_next(request)


# ── P1-7 扩展：写端点鉴权闸门（实测 34 条写路由里 32 条此前不设防，6 条对匿名写回 200）。
#    Starlette 里后注册的中间件在最外层 ⇒ 本闸门先于 api_rate_limit：被拒的请求既不该占限流预算，
#    更不该走到 handler 里产生副作用（rebuild 重写记忆就是这类副作用）。 ──
app.middleware("http")(write_gate)

templates_dir = Path(__file__).parent.parent / "templates"
static_path = Path(__file__).parent.parent / "static"
templates = Jinja2Templates(directory=str(templates_dir))

# v0.5.2.7 自定义静态资源路由（替代原 StaticFiles mount）；v0.12.4 改口径：
# 原来是 no-store ⇒ 浏览器每进一次终端页都要重下 290KB 的 vendor/xterm.js，而且从不压缩。
# 现在 no-cache（每次仍回源校验）+ 自己处理 If-None-Match ⇒ 文件没变只回 304 空响应，
# 文件一改 ETag 就变 ⇒ 拿不到旧 JS（当初写 no-store 就是怕这个，304 同样防得住）。
if static_path.exists():
    @app.get("/static/{file_path:path}")
    async def _static_no_cache(file_path: str, request: Request):
        # 整条策略（判据 + 组装 + 发射）2026-10-12 已收进 src/staticguard.py —— 此前只抽了判据、
        # 组装内联在这里，⇒「静态资源怎么正确地服务」的 interface 横跨两处且组装不可测。
        # 现在路由退化为一次调用；行为逐字节冻结，不变量见 staticguard.serve 的 docstring，
        # golden 矩阵见 tests/test_staticguard_serve.py（L0）与 verify_static_headers_matrix.py（L2）。
        plan = staticguard.serve(
            static_path, file_path,
            (request.query_params.get("v") or "").strip(),
            request.headers.get("accept-encoding") or "",
            request.headers.get("if-none-match") or "",
        )
        if plan.status == 404:
            raise HTTPException(404)
        return staticguard.to_response(plan)
    # 不再 mount StaticFiles；自定义路由接管 /static/

# 子路由（Hook / 记忆 / 指挥官）
app.include_router(hook_mod.router)
app.include_router(memory_mod.router)
app.include_router(memfed_mod.router)
app.include_router(kb_mod.router)
app.include_router(skill_mod.router)
app.include_router(tasks_mod.router)
app.include_router(mcpgw_mod.router)
app.include_router(cronjobs_mod.router)
app.include_router(term_mod.router)
app.include_router(audit_mod.router)
app.include_router(api_dataset_mod.router)   # P0-1：/api/dataset/* + /api/experience/*（只读）
app.include_router(runlog_mod.router)
app.include_router(cloudcli_mod.router)
app.include_router(localprojects_mod.router)
app.include_router(resources_mod.router)
app.include_router(github_mod.router)
app.include_router(prefs_mod.router)
app.include_router(modelcfg_mod.router)
app.include_router(ghsettings_mod.router)
app.include_router(settings_bundle_mod.router)   # PT-20260927-15：/api/settings/export|import
app.include_router(hublog_mod.router)

# D2：Hub MCP Server —— 把本机事实源以 MCP 暴露给 Hermes 等外部 Agent。
# 端点为 /hub-mcp/mcp（streamable_http_app 自带 /mcp 子路由，故挂在 /hub-mcp 下，避免与 mcpgw 的 /mcp/* REST 冲突）。
# 用 try 包裹：挂载失败不得影响主服务启动。
try:
    import contextlib

    import hubmcp

    # session_manager 是懒创建的：必须先 build_app() 再取。
    _mcp_app = hubmcp.build_app()
    _mcp_sm = hubmcp.server.session_manager

    # Starlette 不会自动运行 mount 子应用的 lifespan，需手动并入主应用 lifespan，
    # 否则报 "Task group is not initialized. Make sure to use run()."
    _orig_lifespan = app.router.lifespan_context

    @contextlib.asynccontextmanager
    async def _hub_lifespan(app_):
        async with _mcp_sm.run():
            async with _orig_lifespan(app_):
                yield

    app.router.lifespan_context = _hub_lifespan
    app.mount("/hub-mcp", _mcp_app)
    print("[Agent Hub] Hub MCP server 已挂载：/hub-mcp/mcp（lifespan 已并入）")
except Exception as e:  # noqa: BLE001
    print(f"[Agent Hub] Hub MCP server 挂载失败（主服务不受影响）：{type(e).__name__}: {e}")


@app.get("/health")
async def health():
    """自证端点（P0-2）：不只看活没活，还要能看出「跑的是哪份代码」。

    旧口径只回 {status,service,version,port} ⇒ 版本漂移（进程报 0.13.2 / HEAD 已 0.13.3）
    与「对话端点必 500」这类**静默不可用**全都看不见。
    新增字段全 additive；前端 pollHealth 只读 status，不会被改坏。
    code_stale 只兑情报、不改 status：代码改了没重启不等于服务坏了。
    """
    try:
        db.query("SELECT 1 FROM sqlite_master LIMIT 1")
        db_ok = True
    except Exception as e:  # noqa: BLE001
        db_ok = False
        print(f"[health] db 自检失败：{type(e).__name__}: {str(e)[:120]}", flush=True)
    out = {"status": "ok", "service": "agenthub", "version": VERSION, "port": config.port,
           "db_ok": db_ok,
           "term_sessions": term_mod.alive_count(),
           "term_idle_max_s": term_mod.idle_max_s()}
    out.update(selfattest.snapshot())
    # 记忆后端体检（P0-6）：权威库在 TDAI，它挂不挂必须从 /health 能看出来。
    # 旧态是「/api/memory/search 永远回 count:0 且无任何错误字段」——全绿而功能层已死。
    # 纯读缓存不起网络（见 tdai_client.backend_status 注释），且**不改 status**：
    # 记忆后端不可用不等于 hub 坏了，同 code_stale 只兑情报的设计意图。
    out["memory_backend"] = tdai_client.backend_status()
    # 上游网关（CCR）连通性 + 模型注册清单 —— 0924 方案档 §三「health 增强（运维 P3→P2）」收口。
    # 为什么必须有：本机三次同源事故都是**模型 ID 失效而 /health 全绿**（09-06 `minimax-m3:free`
    # HTTP 400、09-19 `'ultra'` 无效、09-23 `qwen3.8-flash` 缺 provider 前缀）——即 09-22 定名的
    # 「静默不可用」家族。现在 watch 里的每个写死 ID 是否仍在册，直接是 /health 的可断言字段。
    # 纯读缓存（gwprobe 自己 stale-while-revalidate，TTL 300s），**不改 status**：
    # 上游网关不可达不等于 hub 坏了，与 code_stale / memory_backend 同一设计意图。
    out["ccr_gateway"] = gwprobe.status()
    # 出站熔断器状态（PT-20260927-12 A 档）：只列**开着的**闸 + 冷却剩余秒。
    # 为什么必须有：熔断一开，那些调用点会在**毫秒内**返回（不发网络）。
    # 没有这个字段，用户看到的是「GitHub 项目页空了」而没有任何原因字段——
    # 正是本仓反复治理的「静默不可用」家族，且它比网络故障更难自查。
    # 纯读、不改 status（熔断 ≠ hub 坏了），异常不得把 /health 打挂。
    try:
        out["breakers"] = resilience.instance.snapshot()
    except Exception as e:  # noqa: BLE001  # 情报字段不得把 /health 打挂
        out["breakers"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
    # 画像最近检测时间（同属「health 增强」的另一半：CCR 连通性 + 画像检测时间 + DB 状态）。
    # 只给 last_sweep 会被「新一轮扫了 6 家、漏了第 7 家」骗过 ⇒ 必须给最坏值 oldest_check_age_s
    # 与 unchecked（在册却从没被扫到的家数，正是 09-22「在册却静默不可用 21 天」的形态）。
    try:
        out["profiles_last_check"] = healthx.profiles_last_check(
            vitals_mod.vitals.snapshot(), vitals_mod.vitals.last_sweep, vitals_mod.SWEEP_EVERY)
    except Exception as e:  # noqa: BLE001  # 情报字段不得把 /health 打挂
        out["profiles_last_check"] = {"state": "error",
                                     "error": f"{type(e).__name__}: {str(e)[:120]}"}
    if not db_ok:
        out["status"] = "degraded"
    return out


# ── 领域路由（C2：已从本文件抽成独立 module，见 src/api_*.py）──
app.include_router(agents_router)
app.include_router(vitals_router)
app.include_router(sessions_router)
app.include_router(models_router)
app.include_router(system_router)


# ── ⚙设置（口令保护的敏感配置查看）────────────────────────

class PasscodeRequest(BaseModel):
    passcode: str = ""


@app.post("/api/settings/term-token")
async def settings_term_token(request: Request, req: PasscodeRequest):
    """口令正确时返回 TERM_TOKEN。

    有意用 POST 而非 GET：现有 api_rate_limit 只拦写方法，
    口令爆破会被 429 限流拦住（60 次/分钟/IP）。
    """
    if not HUB_PASSCODE:
        raise HTTPException(503, "未设置 HUB_PASSCODE，请先在 .env 配置后再使用设置页")
    if not hmac.compare_digest(req.passcode, HUB_PASSCODE):
        ip = request.client.host if request.client else "?"
        print(f"[settings] 口令错误：{ip}")
        raise HTTPException(401, "口令错误")
    tok = os.getenv("TERM_TOKEN", "")
    return {"term_token": tok, "set": bool(tok)}


# ── 端口管理 ──────────────────────────────────────────────────────────

# ── 只读发现扫描（S2）────────────────────────────────

class ScanIn(BaseModel):
    auto_register: bool = True


@app.post("/api/scan/run")
async def scan_run(body: ScanIn):
    """发现源扫描（docker/systemd/CLI 名单，全部只读探测）

    2026-09-24：扫描收尾补一次「定向重判」。原实现只跑 scanner.run_scan，
    而它「仅报告安装状态（不注册）」（见 scanner.py 顶部注释），卡片补发由
    profiles._dynamic_cli_agents 负责，其闸门读的是**上一轮 vitals 结论**
    ⇒ 点「自动扫描」永远刷不动一枚陈旧 not_installed，新装 CLI 出不来（opencode 实例）。
    重判只走 L1/L2（which / --version / --help），不碰模型、不烧 token。"""
    result = await asyncio.to_thread(scanner.run_scan, body.auto_register, db)
    rejudge: dict = {}
    try:
        rejudge = await asyncio.to_thread(
            vitals_mod.vitals.rejudge_stale, agent_profs_for_vitals())
    except Exception as e:  # noqa: BLE001  重判挂了不能把扫描本身弄失败
        rejudge = {"error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    result["vitals_rejudge"] = rejudge
    need_reload = bool(rejudge.get("changed")) or bool(
        body.auto_register and result.get("added"))
    if need_reload and appstate.discovery:
        if rejudge.get("changed"):
            profiles_mod.invalidate_cli_cache()   # 30s 候补缓存不得压住刚翻案的卡
        appstate.discovery.reload()
    return result


@app.get("/api/ports")
async def api_ports():
    rows = list_listeners()
    # 标注归属已知 Agent
    known = {}
    if appstate.discovery:
        for a in await appstate.discovery.discover_all():
            if a.port:
                known[a.port] = a.id
    for r in rows:
        r["agent"] = known.get(r["port"])
    return {"listeners": rows, "count": len(rows)}


@app.get("/api/ports/{port}")
async def api_port_detail(port: int):
    return {"port": port, "in_use": port_in_use(port)}


# ── Dashboard ─────────────────────────────────────────────────────────

@app.get("/", response_class=HTMLResponse)
async def dashboard(request: Request):
    resp = templates.TemplateResponse(request, "index.html", {"version": VERSION})
    # 2026-09-26：HTML 原先不带任何缓存头/验证器 ⇒ 浏览器与已开标签页长期不自愈，
    # 部署新版后用户看到的仍是旧 UI。no-cache = 可存，但每次导航必须回源校验。
    resp.headers["Cache-Control"] = "no-cache"
    return resp


# ── 兼容旧端点：/api/memory（别名到 L1 列表）──────────────────────────

@app.get("/api/memory")
async def get_memory_alias(query: Optional[str] = None, limit: int = 10):
    if not query:
        rows = db.query("SELECT * FROM memories WHERE status='active' ORDER BY id DESC LIMIT ?",
                        (limit,))
        return {"memories": rows, "count": len(rows)}
    return await memory_mod.search_memory(query, limit)


_embed_proxy = None      # 外框注入代理句柄（EMBED_UNIFY=0 时保持 None）


async def prov_loop():
    """代码溯源刷新：每 30s 一次 git status（走线程，不进任何请求路径）。

    为什么不搭 vitals 的慢拍：VITALS_SWEEP_SEC 默认 900s，而“工作区改了没”是
    健康台账里最想要分钟级响应的字段；搭慢拍会让它在 15 分钟里拿着旧值讲现测。
    """
    while True:
        try:
            await asyncio.to_thread(selfattest.refresh)
        except Exception as e:  # noqa: BLE001
            print("[prov] 溯源刷新异常（下轮重试）%s: %s" % (
                type(e).__name__, str(e)[:160]), flush=True)
        await asyncio.sleep(float(os.getenv("HUB_PROV_SEC", "30")))


#: WAL 周期检查点（2026-10-11）。默认 120s；置 0 关闭本条后台循环。
#: 背景：SQLite 的**自动**检查点是「每积满 wal_autocheckpoint（默认 1000）页，就在
#: 触发它的那条写语句里同步 checkpoint」。而 term_record 的逐帧写（每个按键 / 每帧
#: 输出一条 INSERT+commit）正落在这条路上 ⇒ 每 ~180 帧就有一帧被同步 checkpoint
#: 拖住。本机实测（100 万行真库副本，4000 帧）：
#:   wal_autocheckpoint=1000（默认）⇒ 每 183 帧一次尖峰，单帧 p99 6.9ms / max 116ms
#:   wal_autocheckpoint=0（关自动）  ⇒ 尖峰 0 次，单帧 p99 0.17ms / max 16ms
#: 而 checkpoint 本身**不是正确性前提**（WAL 本就是崩溃安全的），它只是防止 -wal
#: 无限增长。所以把它从「逐帧同步」挪到「后台定时」：事件循环不再被逐帧路径独占，
#: 总 fsync 次数不变（只是换了执行位置），WAL 也不会失控。
#: 为何不把默认值写进 db.py 的 PRAGMA：那是**全局**连接语义，别的库/别的连接
#: （engine 侧、MCP 侧）不该被本模块的取舍顺带改掉；改在 hub 自己的后台循环里，
#: 影响面就是「hub 主连接」这一个对象。成本 / 收益与 term_record 的预算修复同源：
#: 都是「一个同步动作挂在高频写路径上」。
HUB_WAL_CHECKPOINT_SEC = int(os.getenv("HUB_WAL_CHECKPOINT_SEC", "120"))


async def wal_checkpoint_loop():
    """定期在 to_thread 里 checkpoint（见 HUB_WAL_CHECKPOINT_SEC 的取证注释）。

    异常不打死循环：checkpoint 是优化，失败（被写者占着）下一轮再来。
    """
    if HUB_WAL_CHECKPOINT_SEC <= 0:
        return
    while True:
        await asyncio.sleep(HUB_WAL_CHECKPOINT_SEC)
        try:
            await asyncio.to_thread(db.checkpoint)
        except Exception as e:  # noqa: BLE001
            print(f"[db] WAL 检查点异常（下轮重试）{type(e).__name__}: {str(e)[:160]}", flush=True)


MODEL_DRIFT_SWEEP_SEC = int(os.getenv("HUB_MODEL_DRIFT_SWEEP_SEC", "300"))
#: 漂移写回开关（用户 09-28 授权默认开；置 0/false/no ⇒ 退回 v0.13.50 的只读告警）
MODEL_DRIFT_REPAIR = os.getenv("HUB_MODEL_DRIFT_REPAIR", "1") not in ("0", "false", "no")


def _drift_tick() -> dict:
    """一轮「体检 +（授权时）写回」，返回值即证据。纯同步，跑在 to_thread 里。"""
    out: dict = {"repaired": [], "skipped": [], "drift": modelcfg.drift_report()}
    if MODEL_DRIFT_REPAIR:
        out.update(modelcfg.repair_drift())
    return out


def _drift_log(out: dict, where: str) -> None:
    for _d in out.get("drift") or []:
        _bad = "，".join(f"{k}={v}" for k, v in (_d.get("owned") or {}).items())
        print(f"[modelcfg] {where} 配置漂移：{_d['id']} 持久化={_d['hub_model']} 但文件里 {_bad}",
              flush=True)
    for _x in out.get("repaired") or []:
        print(f"[modelcfg] {where} 漂移已写回：{_x['id']} → {_x['model']}"
              + (f"（备份 {len(_x['backups'])} 份）" if not _x.get("dry_run") else "（dry-run）"),
              flush=True)
    for _x in out.get("skipped") or []:
        print(f"[modelcfg] {where} 漂移未写回：{_x['id']} —— {_x['reason']}", flush=True)


async def model_drift_loop():
    """配置漂移巡检。为什么不能只靠启动那一次：CCR 与 hub 都在开机时起来（今日实测
    同秒），CCR 落笔改写 claude 的 env 三兄弟可能**晚于** hub 的启动修复 ⇒ 修完又被
    盖回去。所以按 MODEL_DRIFT_SWEEP_SEC（默认 300s）定期复检并写回。异常不打死循环。"""
    while True:
        await asyncio.sleep(MODEL_DRIFT_SWEEP_SEC)
        try:
            _drift_log(await asyncio.to_thread(_drift_tick), "巡检")
        except Exception as e:  # noqa: BLE001
            print(f"[modelcfg] 巡检异常（下轮重试）{type(e).__name__}: {str(e)[:160]}", flush=True)


async def vitals_loop():
    """可用心跳慢周期：首轮延后 2s（先让 hub 开接请求），之后每 VITALS_SWEEP_SEC 一轮。
    只跑 L1/L2（which / 文件头 / --version / --help / 端点探活），不碰模型；
    任何异常都不打死循环（否则一次偶发就把菜单永久冻在旧结论上）。"""
    await asyncio.sleep(2)
    while True:
        try:
            r = await asyncio.to_thread(vitals_mod.vitals.sweep, agent_profs_for_vitals())
            print("[vitals] sweep %s" % r, flush=True)
        except Exception as e:  # noqa: BLE001
            print("[vitals] sweep 异常（下轮重试）%s: %s" % (
                type(e).__name__, str(e)[:160]), flush=True)
        await asyncio.sleep(vitals_mod.SWEEP_EVERY)


@app.on_event("startup")
async def startup():
    global _embed_proxy
    db.init_db(config.db_path)
    # 关掉 hub 主连接的**自动**检查点，改由 wal_checkpoint_loop 定时做（见该循环注释：
    # 自动检查点每 1000 页就在「触发它的那条写语句」里同步 fsync，而 term_record 的
    # 逐帧写正是高频写路径 —— 实测每 183 帧一次 ~90ms 尖峰，全部落在事件循环里）。
    # 放在这一行是因为它必须紧跟 init_db：PRAGMA 是连接级状态，换连接就失效。
    try:
        db.execute("PRAGMA wal_autocheckpoint=0")
    except Exception as e:  # noqa: BLE001 —— 关不掉只是保留旧行为（有尖峰），不该拖垮启动
        print(f"[db] 关闭自动检查点失败（保留默认行为）：{type(e).__name__}: {str(e)[:120]}", flush=True)
    # v0.13.101：录制预算的计数表播种（存量库升级后计数从0 起跳会让预算判据失真）。
    # 幂等且只在计数行缺失时才做那一次全表 SUM；播种失败不许拖垮启动——
    # 计数表缺失时 total_bytes() 返回 0，代价是预算偏松（多录一点），
    # 而代价反过来（启动失败）要大得多。
    try:
        term_record.ensure_counted()
    except Exception as e:  # noqa: BLE001
        print(f"[term_record] 计数表播种跳过：{type(e).__name__}: {str(e)[:160]}", flush=True)
    appstate.discovery = AgentDiscovery(config, db=db)
    build_adapters(config)
    # v0.13.50 起的漂移体检 + v0.13.51 的授权写回：启动先修一轮（CCR 可能还没改写，
    # 所以另有定期巡检兜底）。写回属共享配置写入 ⇒ 落笔前时间戳备份，见 repair_drift。
    try:
        _drift_log(_drift_tick(), "启动")
    except Exception as e:  # noqa: BLE001 —— 体检/写回出岔子不许拖垮整个启动
        print(f"[modelcfg] 启动漂移处理异常：{type(e).__name__}: {str(e)[:160]}", flush=True)
    # 上游网关注入（/health 的 ccr_gateway 情报源）。watch 放两个「写死在配置里的模型 ID」：
    # manager 用的那个 + vitals L4 探针用的那个。上游一旦改名/下架，watch.<id>=false 当天可见，
    # 不必等探活烧一轮 token 才发现（09-23 的 M1 阻塞「拿不到在线清单」就此长期解除）。
    gwprobe.configure(config.manager_llm_base, config.manager_llm_key,
                      watch=[config.manager_llm_model, vitals_mod.RT_MODEL])
    tasks_mod.ensure_schema()
    # P0-1：数据集/评估/经验库四表（幂等建表，切片 A）
    dataset_mod.ensure_schema()
    tasks_mod.set_context(
        chat_fn=lambda a, m, s=None, mo=None, tr=None: _chat_dispatch(a, m, s, mo, tr),
        agent_ids_fn=lambda: [c["id"] for c in appstate.discovery.all_configs()])
    _spawn(tasks_mod.sweep_stale_tasks())
    _spawn(vitals_loop())
    _spawn(model_drift_loop())   # 配置漂移巡检（CCR 重启会把模型改回旧值）
    _spawn(prov_loop())   # 代码溯源（工作区脏度）刷新
    # P0-4：终端会话回收必须有独立心跳，不能寄生在前端轮询上
    _spawn(term_mod.reap_loop())
    # PT-20260929-02 补做：零输出即死的子进程没有任何 IO 事件可监听，
    # 只能靠定期 waitpid 探活发现。缺这条快车道，reap_loop 的 60s 间隔
    # 就是「前端空白最长 60 秒」的同义词。
    _spawn(term_mod.fast_reap_loop())
    _spawn(wal_checkpoint_loop())   # WAL 周期检查点（把自动检查点从逐帧写路径挪走）
    mcpgw_mod.ensure_schema()
    cronjobs_mod.ensure_schema()
    cronjobs_mod.set_context(
        chat_fn=lambda a, m, s=None, mo=None, tr=None: _chat_dispatch(a, m, s, mo, tr))
    cronjobs_mod.start_engine()
    # 外框统一注入代理（用户 09-20 方案 b）：:3103 → qwenpaw :8088，HTML 出栈前插 <style>。
    # 起不来也不能影响 hub 本体：裹 try/except，顶多外框不统一（嵌入视图仍直连可用）。
    if profiles_mod.EMBED_UNIFY:
        try:
            qp = next((p for p in profiles_mod.PROFILES if p.get("id") == "qwenpaw"), None)
            if qp and qp.get("port"):
                _embed_proxy = embed_proxy_mod.EmbedProxy(
                    "QwenPaw", "127.0.0.1", qp["port"],
                    listen_host=os.getenv("EMBED_PROXY_HOST", config.host),
                    listen_port=profiles_mod.EMBED_PROXY_PORT)
                await _embed_proxy.start()
        except Exception as e:
            print(f"[Agent Hub] 注入代理启动失败（不影响其他功能）：{type(e).__name__}: {e}")
            _embed_proxy = None
    sa = selfattest.boot()   # 记下启动那一刻的 sha，供 /health 判 code_stale
    print(f"[Agent Hub] 启动完成 v{VERSION} sha={sa['git_sha_boot'] or '?'}，"
          f"监听 {config.host}:{config.port}")
    print(f"[Agent Hub] CCR: {config.ccr_url} | pi: {config.pi_url} | "
          f"jcode: {config.jcode_url} | TDAI: {config.tdaI_url}")
    print(f"[Agent Hub] LLM（记忆 L2 重建 / DAG 拆解）: {config.manager_llm_base} "
          f"model={config.manager_llm_model}")


@app.on_event("shutdown")
async def shutdown():
    for t in list(_bg_tasks):
        t.cancel()
    if _bg_tasks:
        await asyncio.gather(*_bg_tasks, return_exceptions=True)
        _bg_tasks.clear()
    if _embed_proxy is not None:
        await _embed_proxy.stop()
    term_mod.kill_all()


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("src.main:app", host=config.host, port=config.port, log_level="info")
