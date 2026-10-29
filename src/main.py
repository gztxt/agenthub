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
from fastapi.encoders import jsonable_encoder
from fastapi.exceptions import RequestValidationError
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
from registry import build_adapters, get_adapter, dispatchable_ids
import hook as hook_mod
import memory as memory_mod
import memfed as memfed_mod
import kb as kb_mod
import skill as skill_mod
import skill_store as skill_store_mod
import skill_direct as skill_direct_mod   # 第二数据源：从会话转录采集真实技能调用
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
import memview                     # 内存三口径可观测（本体/托管子进程/cgroup 记账）
import retention                    # 保留期定时清理（term_recordings + profile_events）
import resilience                   # 出站韧性层：退避重试 + 熔断（/health 的 breakers 字段）
import settings_bundle as settings_bundle_mod  # 配置包导出/导入（PT-20260927-15，JSON，不引 YAML）
import approvals as approvals_mod   # P1-2：审批三态（ASK + Preview-Approve-Commit-Verify）
import skill_assets as skill_assets_mod   # P1-5 其余四件：技能资产（owner/version/scope/状态机）
import actionplane as actionplane_mod     # P2 A10：统一 Action Plane（请求/裁决/结果 + 生命周期）
import admission as admission_mod         # P2 C7：外部资产准入六阶段
import sessions_export as export_mod
import export_batch                   # 导出取消息的批量 IN（L0 可测的纯函数层，同 staticguard 范式）
import models_cache                   # 模型清单的成功路径缓存（同上，纯函数层）
import writeauth                    # 导出端点按写端点同等鉴权（复用 decide 的 fail-closed）
import audit as audit_mod           # 资产变更审计的只读查询门面（GET /api/audit/list）
import refscout as refscout_mod     # PT-20261021-04：参考情报（GitHub 借鉴源采集 + 三轴打分）
import intake as intake_mod         # PT-20261022-01：接单前置（四查一简报）
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
import botboy as botboy_mod         # v0.13.170：DS Bot 团队只读概览（读 ~/.dsh/bot/state.json）

# C2（2026-10-12）：领域路由从本文件抽成独立 module —— main 只留组装职责。
import appstate                     # 组装方写、领域方读的共享可变状态（discovery）
from api_agents import router as agents_router, _chat_dispatch   # 启动期 chat_fn 回调仍要它
from api_sessions import router as sessions_router
from api_models import router as models_router
from api_vitals import router as vitals_router, agent_profs_for_vitals
from api_summarize import router as summarize_router   # 2026-10-23：本机摘要提炼
from api_system import router as system_router   # 系统级：前端「需重启」按钮的自重启端点

print(f"[agenthub] 配置: PORT={config.port}, HOST={config.host}")

# 单一版本源：/health、FastAPI 元数据、启动横幅与页脚都取这里
VERSION = "0.13.176"

# v0.13.176（2026-10-29）：难度→档位（**只决定起点，只升不降**）。
# 联网调研后刻意没做成"难度表直接当路由器"（那是 router 文献里的"信心剧场"）：
#   · scixa《Model Tier Switching》把「用模型自评的 difficulty 做路由信号」列为头号病害；
#     arXiv 2605.24299（20 模型×6 基准）实测置信度与表现基本无关 ⇒ 自评只能当起点先验。
#   · TokenTrim 在 RouterArena 的消融：把难度信号整条拿掉，成本-准确率前沿几乎不动
#     ⇒ 难度不产生收益，收益必须由 verdict 与升档承担。
# 所以本版只做两件**能验证**的事：
#   ① 模型档位：t1/t2/t3（`tiercfg.py`），只填空、不覆盖 Leader 显式选择、失败按档位上一档。
#   ② 执行者档位：`promote_agent()` 在难度 3 时抬到工具环最完整的 a3（claude-code/codex），
#      **只升不降**——
#      降级要结果证据，自评不配。汇总节点（自动补的与 Leader 自写的）一律钉顶层。
# ③ 同时暴露一个会打脸的指标 `model_tier_effective`：本机实测 5 个模型 ID 全被
#    网关改写为 agnes-2.5-flash ⇒ 模型档位当前**不影响能力**，只有执行者档位有效。
#    这个数不报，"档位映射"就是自我感觉良好。

# v0.13.175（2026-10-29）：派发扩池 2 → 11 + 失败重试改升档（cascade）。
# ① 扩池：新增 9 个带工具环的 CLI 执行者（claude-code/codex/dsh/pi/codebuddy/grok/
#    hermes/omp/cursor），全部走 `adapters/cli_agent.py` 的无头调用。**注册不等于可用**：
#    `dispatchable_ids()` 逐家过 `available()`（二进制/凭据），不可用的挡在池外。
#    判据来源：此前 `chat()` 不带 tools ⇒ 文件类子任务会编造（/etc/hostname 真值 zzst，
#    端点回 AGNES-2.5-FLASH 且 verdict=pass）。实测 claude-code/codex/omp 均能把真值取回。
# ② 无头模式的假绿：CLI 遇工具调用会停下要审批、退出码仍为 0 ⇒ 现在带各家免审批旗标
#    （`TASK_CLI_YOLO=0` 可关），并把「请允许…/需要授权」判成 `needs_approval` 失败。
# ③ 升档重试：第 N 次重试换一档 —— 模型优先（同域才换），否则换 agent（默认阶梯
#    claude-code,codex）；换不动写 `no_escalation_path`，不假装升过。
#    升档来源落 `escalated_from_agent/model` 两列，复盘才分得清「升档后又败」与「同档又败」。
# ④ pi 从「恒失败桩」转正（`pi -p --model` 实测可用；默认模型返回空响应，故内置兜底）。

# v0.13.174（2026-10-29）：派发三补——强制汇总节点 / 超时口径统一 / 拒答识别补说法。
# 为什么：影子实弹 5 条跑出两处「结构上就不该靠提示词」的洞。
# ① 汇总节点：Leader 提示词只说「聚合类任务要 deps 列全上游」，**没有任何机制保证它写**⇒
#    多叶子全 success、run 收敛、却没有结论。现在由 `ensure_synthesis` 在代码里补，并给
#    Leader 的条数上限预留一格（`LEADER_MAXN = MAX_TASKS - 1`）。
# ② 超时口径：adapter 写死 120s 而外层闸门是 600s ⇒ 长任务一律记成「执行者失败」，
#    归因错一层。现在 `timeout_s` 由调用方传，取值 = TASK_TIMEOUT_S + 30（外层先触发）。
# ③ 拒答说法：「我无法直接读取…没有可用的文件系统访问工具」不被旧正则覆盖，判 pass。
#    补两条说法级正则；同批钉死「第二人称的操作建议不算拒答」。
# ④ 口令桥首行加机器键 `[hub:<run>/<task>]`：C 的幂等键，不让它去解析人话。

# v0.13.173（2026-10-29）：技能中心**归档根**补两仓 —— show-me 与 frontend-design。
# 为什么需要：两个技能按归档军规 clone 在 `技术文档/<仓>/`，软链进四个发现点；
# 而 `_inside()` 按 realpath 判越界，仓不在 `ARCHIVE_ROOTS` 里 ⇒ 四个发现点全部
# **静默拒读**，技能中心搜不到、`hub_skill_invoke` 也点不到（与 10-27 `i-have-adhd`
# 同一种失败形态：文件在盘上、路也在扫，只是被自己的闸门挡掉，且不报错）。
# 逐个仓登记而不是把 `技术文档/` 整体放宽：后者会把 snapshots / 全量备份 /
# EXCLUDED_DIRS 里的四个桶一起放进来，等于用一条 FAIL 换一条已定结论。
# 判据：`tests/test_archive_roots.py` 的集合断言 + `_allowed_roots()` 逐根 subTest。

# v0.13.172（2026-10-28）：Hub MCP **出口面**收窄 —— 白名单 + 派发闸门（PT-20261028-01 D 步）。
# 为什么需要：把 hub MCP 挂给别的 agent 会话（dsh 的 Bot）后，「默认全给、再想扣哪个」是错的
# 默认值 —— 每上线一个新工具就自动多一个出口，没人会发现。`HUB_MCP_ALLOW_TOOLS` 把默认反过来：
# 设了就只留列出的名字，新工具上线不会自动外泄。
# 派发类（`hub_dispatch`/`hub_replan`）**默认仍开**：它是已上线能力，默认摘掉＝静默少功能；
# 要硬关用 `HUB_MCP_ALLOW_DISPATCH=0`，关掉的场景必须出现在审计行的「扣掉」里。
# 两道闸的顺序是**先扣派发、再套白名单**：反过来的话「白名单里写了 dispatch」就能把它顶回来，
# 安全默认被一份手写清单推翻。冲突记 `blocked`、拼错记 `unknown` —— 静默少一个工具最难查。
# 这一刀防的是「Bot → hub → 多 agent → 回 Bot」的**自指闭环**（且能顺带烧钱）。

# v0.13.171（2026-10-28）：口令桥 —— hub 子任务 → DS Bot 的**人工搬运**（不自动 send）。
# 为什么只做复制：hub 的 chat() 是纯文本无工具，DS Bot 的 Bot 才有文件/命令能力；但 `send`
# 是**不可重试**的 mutation（ds-harness-remote protocol.md §16.4），超时重发就是**重复派活**。
# 文本不含 bot 名：本机未装 ds-bot，团队不存在，写死名字就是假信息。
# 子任务正文取 `/api/tasks/{run}` 的 `prompt`（SELECT * 里本来就有），缺失时显式说明，
# 不许把 undefined 渲染给用户。按钮走 `data-bb-copy` 委托（禁 inline onclick），
# 并明写「hub 不会代发」，否则用户以为点了就派发出去了。

# v0.13.170（2026-10-27）：Botboy —— DS Bot 团队的**只读投影**（系统菜单第十项）。
# 为什么读文件而不调 `POST api/bot`：dsh 按 10-24 裁定按需拉起、常态 inactive，而 dsh web
# 的鉴权 token 是进程级随机值；ds-bot 自己把 roster / rooms / exchange 写在
# `$DSH_HOME/bot/state.json`（其 store.js 头注释明写）。
# 不写盘是硬边界：ds-bot 用 `state.lock` 保证同一份数据只有一个写者，hub 越权改写
# 会制造「另一个 dsh 正在编辑」的假锁冲突 ⇒ 建 Bot / 发消息回 dsh 原生界面。
# 三种「没有」分开渲染：`ok=false`（文件在但坏 = 不可判定）/ `found=false`（没装没建）/
# 真·空团队 —— 前两种被渲染成空团队，用户会以为团队被清空（本仓最禁的静默不可用）。

# v0.13.169（2026-10-27）：v0.13.168 的**实弹复验** —— 三处「测试全绿、线上必挂」的假绿点。
# ① 装配层调 `AgentDiscovery.all_configs()`（该类从无此方法）⇒ 每次拆解 500。单测全程
#    注入假 ctx，`agent_ids_fn=` 那一行从未被执行 ⇒ 33 例全绿也照样线上必挂。
#    现改为 `registry.dispatchable_ids()`，并新增「拿真类校验源码引用的属性」闸门。
# ② 派发池原本取全量画像（20 张 agent 卡），而真正能跑 `chat()` 的只有 claude/jcode
#    （pi 需 Web UI、TDAI 不是对话 Agent、其余无适配器）⇒ Leader 抽中 dsh/cloudcli 就是
#    一条注定「无可用适配器」的失败任务（实跑 239b4d553a：0 产出）。
# ③ 验收 `nonempty` 把「我没权限，你把输出粘贴给我」与「User Safety: safe」（免费池路由
#    到审核模型）都判 **pass** ⇒ 零产出以 `complete` 收敛。verifier 增 `refusal` /
#    `not_a_product` 两类 FAIL：非空只证明模型说了话，不证明活干了。

# v0.13.168（2026-10-27）：全链路派发 —— 拆解 → 选型 → 派发 → 验收 → **最多 2 轮**。
# 此前链路三分五裂：技能 `agent-dispatch` 只有手册无编排；`tasks.py` 有 DAG 但**只选
# agent 不选模型、没有第二轮**；`hubmcp.py` 零 tasks/dispatch 入口。
# 新增：① DAG 项 `difficulty`(1~3) + `model`（白名单校验，非法降级不报错）；
# ② `task_runs` 表（轮次/血缘/stop_reason/child_run_id）；③ `replan_gate()` 六态闸门
# （unfinished/complete/max_rounds/budget/same_failure/go）；④ `/replan` + `auto_rounds`；
# ⑤ MCP `hub_dispatch`（默认 dry_run 不花钱）/`hub_run_status`/`hub_replan`；
# ⑥ 技能 §1.4 把五段、停止条件与三条诚实边界写死。
# 口径分离：`model_choice`=拆解意图 vs `model`=回包实发；`round`=整条 run 重拆 vs
# `attempts`=同一子任务重跑。闸门 `tests/test_task_rounds_l0.py`（33 例）。

# v0.13.167（2026-10-27）：设置 → 模型的**执行逻辑**。此前"已生效"的唯一依据是
# HTTP 200 —— 那只证明写进去了，不证明会用上、更不证明那个模型现在还存在。
# 新增：① `catalog_ids()`（网关在册清单，同步+TTL）；② `effective_state()` 把
# 「会用什么/从哪来/哪几条通道生效/有什么风险」算成一份可渲染的结构；
# ③ `verify_model()` 四条判据 + 可选真实调用；④ 保存前用清单拦死 ID（409）；
# ⑤ 页面加「验证生效 / 真实验证」两个按钮，保存后自动自证。

# v0.13.166（2026-10-27 重启后验收发现）：第二数据源只接在了没人看的那个函数上。
# 症状：scan 事件已落库（7 天 180 条 `skill.invoke`，agent-dispatch 单名 42 条），
#   `/api/skill/usage` 仍报 explicit=0——**页面上的技能调用榜完全看不到 agent 直读**。
# 根因：`skill_usage.counts()` 已改 `source IN ('rest','scan')`，但该函数只被 `/zombies`
#   用；前端 `hub.js` 读的 `/api/skill/usage` 走的是 `skill_weights.load()`，那里
#   硬编码 `WHERE source = 'rest'`（`SOURCE` 常量）。**两套并行实现，只有一套被接了新源。**
# 修复：`skill_weights.SOURCES = ('rest','scan')`；`classify()` 识别 scan 的
#   `mode=direct_read`——只有 `evidence=skill_tool` 才算「拿走正文去执行」(explicit=2)，
#   读 SKILL.md / 命令行命中按 read=1，否则 180 条读盘会被当 360 分计。
# 闸门：`tests/test_skill_weights.py::test_scan_source_counted_and_direct_read_is_read`。

# v0.13.165（2026-10-27 用户复查三缺口）：补三家转录 + 补一个 subject 生产者 + 补四个 UI 入口。
# ① skill_direct 扩到 **7 家**：grok（chat_history.jsonl 的 assistant.tool_calls[]）、
#    jcode（~/.jcode/sessions/*.json 的 messages[].content[].tool_use）、
#    opencode（opencode.db 的 part.data type=tool）。三种存储形态各自的增量口径：
#    JSONL 字节 offset / JSON 消息数（末条重读）/ SQLite rowid 水位；共用去重键。
#    grok 只扫 chat_history.jsonl——events/updates 会重复记同一次调用。
# ② `skill.inject` 从此**有生产者**：注入链 `/api/skill/relevant?via=hook` 且有候选时，
#    runlog 改落 skill.inject（以前落 skill.relevant+auto_inject ⇒ inject 恒 0，
#    只剩桶标签）。人工查询仍落 skill.relevant，同一请求只落一种，不双记。
#    usage / 权重榜 / `/invocations.auto` 同步认这个 subject。
# ③ 四个「后端有、页面零入口」的端点接上：`GET /api/skill/catalog`（L1 目录抽屉）、
#    `DELETE /api/skill/remove`（抽屉逐路「卸载软链」，判据来自新增的 item.links）、
#    `POST /api/skill/store/acquire`（按任务自动取技能，默认 dry_run 预演）、
#    `GET /api/skill/store/auth`（刷新账号态，can_check=false 显示原因）。
# 闸门：tests/test_skill_direct.py（15 例）、test_skill_center_ui（四入口）、
#       test_skill_invoke（subject 改判）、test_skill_weights（inject 计入曝光）。

# v0.13.164：技能调用记账补上第二数据源（2026-10-27 用户令「调用记录没显示所有技能调用，
# 每次都只修被点出来的那个错」）。根因：记账点只挂 hub 三个显式路由，而真实使用发生在
# agent 自己 Read 磁盘的 SKILL.md——那一步不经过 hub；注入链只推名字、只打 skill.relevant。
# 判例 228 补的是 401 文案与空状态，**数据生产者一个字没动**，所以看起来「修过了还是 0」。
# ① skill_direct：扫 pi/claude/codebuddy/codex 会话转录，认 Skill 工具与「读 SKILL.md」，
#   落 source='scan' 的 skill.invoke；grep/ls/find/写盘一律不计（防把检索当使用）。
# ② 按文件增量（byte offset）+ 去重键(caller+call_id) ⇒ 幂等，文件截断重写也不重复记。
# ③ skill_usage 口径改 source IN ('rest','scan')，DIRECT_SOURCE=transcript；
#   置信度**仍封顶 medium**（覆盖只到读取，写盘与纯提及仍不计）。
# ④ runlog.SUBJECTS 补 skill.invoke（原先在运行日志页筛这个 subject 直接 400）。
# 闸门：tests/test_skill_direct.py、tests/test_skill_usage.py、test_execsem_p2_c3_l0。

# v0.13.163: pi/omp 历史会话接入
# 根因：SESSION_STORES 与 TERM_HIST_AGENTS 不同步，新增 agent 必须两边一起改
# SESSION_STORES / TERM_HIST_AGENTS / pi_jsonl / omp_jsonl / 同构
#   v0.13.161：记忆中心统一化（2026-10-25 用户令「所有 agent 能够使用统一接口自动调用
#   记忆中心记忆数据；所有 agent 的记忆数据统一归档；数据无需重复保存，按既有数据统一
#   归档；前端 UI 重设计为看板 + 手动记忆提交框」）。
#   ① **统一写入口** `POST /api/memory/archive` + MCP `hub_memory_archive`：agent 身份
#      必填（body.agent 或 X-Agent-ID），过质量门——scrub 脱敏 → 精确 hash 去重 →
#      simhash 近似 supersede（64 位海明 ≤10，同 agent 同 category）→ 每 agent 活跃
#      上限 1000（超限最旧软删可审计）。零第三方依赖（blake2b 稳定哈希）。
#   ② **不重复保存**：新提交落 hub sqlite 的 layer='ARCH' 单一存储；既有 13 路 agent
#      记忆源仍由 memfed 只读抽取、各自权威。ARCH 登记为第 14 个联邦源（weight 0.7，
#      manual 行源内优先），自动进 FED_FAST_SOURCES / 注入包 / 白名单。
#   ③ **看板矩阵** `GET /api/memory/archive/matrix`：agent ×（既有源/读写状态/归档
#      计数/MCP 接线现探）零内容拷贝；`GET /api/memory/archive` 列表回 total/truncated。
#   ④ **UI 重设计**：记忆中心改看板——运行逻辑状态（源健康/接入矩阵/投影/质量门计数）
#      + 统一检索 + 手动补齐框；旧层 L1/L2/L3 收进归档抽屉。P1 判据不变（首屏 0 按钮）。
#   ⑤ 取证：性能基线（影子 :3210 共享生产投影只读）search P50 16.4ms / vitals 23.9ms /
#      context 83.2ms；改后同法 A/B 配对差复测。
#   闸门：tests/test_memory_archive_l0.py、scripts/build_hubjs.sh。
#   v0.13.160：记忆存储并入记忆中心（2026-10-25 用户令「设置菜单中的记忆存储模块合并融合
#   到系统的记忆中心；优化完善操作逻辑、完善功能、优化前端 UI」）。
#   ① **入口合并**：「设置 · 记忆存储」整页移入 #page-memory 的「本机旧层」折叠块，侧栏
#      设置组不再有这一项。读（vitals/联邦检索）与写（L1/L2/L3）同页，折叠态保住
#      P1 判据「进页 1 个请求、首屏 0 个按钮」，展开时才拉旧层数据。
#   ② **操作逻辑**：L1 支持就地编辑（新增 `PUT /api/memory/l1/{id}`；旧版改一个字要删了
#      重加，而删除是软删 ⇒ 审计上看起来像"删一条又加一条"）；新增时可选分类（旧版写死
#      fact）；列表回 `total/truncated`（旧版只回 count，50 条上限截断得静默）；保存/重建
#      按钮带进行中态；`loadMemories` 不再写 CENTER_HEALTH（体检态只归 memVitals，旧写法
#      会把侧栏健康点洗成"上次列表查询是否成功"）。
#   ③ **顺带修掉的既有缺陷**（都是"改到这里才发现，且都能确定性复现"）：
#      · `list_l1` 的 LIKE 未转义 —— 查 `50%` 命中全表，与 `_local_l1` 的转义口径分叉；
#      · `previewCtx` 引用早已删除的 `#ctxPanel` —— 点「注入预览」必抛 TypeError；
#      · `loadDoc` 的 catch 注释是「首次为空」—— 读失败与真空看起来一样，用户保存就覆盖；
#      · `.sp-card` 是 display:flex，Chrome 对这类 `<details>` 不再隐藏闭合内容 ⇒
#        「折叠」只是视觉错觉（实测 mvToolsBox 的 16 个按钮仍有布局、可聚焦）。
#   闸门：`tests/test_center_ui_l0.py::TestMemoryLegacyMerged`、`scripts/build_hubjs.sh`。
#   v0.13.158：dsh 原生界面「起不来」（2026-10-25 用户报「之前都能正常启动运行，现在故障」）。
#   两层根因，都在 v0.13.153 引入的 `ui-status` 上：
#   ① **判活漏了绑定地址**：dsh 拒绝通配地址，只 `--host 192.168.5.102`，其 3080
#      **不监听回环**；而 `_ui_port_open()` 只探 `127.0.0.1`/`::1` ⇒ 恒 `ready=false`
#      ⇒ 覆盖层永远盖着「原生界面未运行，正在拉起…」，服务其实 active、iframe 也是 200。
#      修法：`_ui_targets()` 先按**画像/配方声明的绑定地址**探，再回环双栈兜底；配方里的
#      `probe` 优先于 `url`（qwenpaw 的 url 是 hub 注入代理 :3103，拿它判活永远假活）。
#   ② **token 缓存不随进程失效**：`_dsh_web_url()` 有 30s 缓存，拉起后前端复取 entries
#      仍是旧 token ⇒ 嵌页 401 白屏。修法：`POST /ui-start` 成功后立刻
#      `profiles.invalidate_ui_url()`（dsh 的 30s + capabilities 的 10s 一起丢）。
#   另：新增 `ui_pin_host`（dsh 专用）——前端 `lanUrl()` 会把卡片地址改写成"当前访问名"，
#      用 127.0.0.1 打开 hub 时就变成 `http://127.0.0.1:3080` ⇒ 连接被拒（影子实测）。
#      绑定具体网卡的入口一律原样给出。闸门 `tests/test_ui_probe_targets.py`。
#   v0.13.157：v0.13.156 上线后复验，线上点 reasonix「启动原生界面」报 `binary_not_found`。
#   根因：ui-start 只补 `~/.npm-global/bin`、`~/.local/bin`，而 reasonix 装在 **nvm** 的
#   `~/.nvm/versions/node/*/bin`；影子实例从交互 shell 起、PATH 自带 nvm 所以一直是绿的
#   （**开发环境假绿**），只有 systemd 拉起的生产进程才暴露。修法：`_ui_spawn_env()`
#   把 `profiles.extra_path_dirs()`（nvm 新版优先 + bun）**前置**进子进程 PATH，再补
#   用户级 bin，与 `term.child_env()` 同口径；`exe` 用 `profiles.which()` 解析成绝对路径。
#   必须前置而非追加：这类 CLI 的 shebang 是 `#!/usr/bin/env node`，子进程自己还要再
#   找 node，PATH 尾部同样会拿系统 node 跑 Node22+ 的 bundle。
#   v0.13.156：**能力矩阵**（2026-10-25 用户第三次报同一事：「新增 agent 后没有原生界面
#   按钮，设置项也不同步；每次都是发现问题再修」）。机制上不是某处写错，而是空白的
#   `ui 写死 None`：`agentscan.registered_profiles()` 与 `profiles._dynamic_cli_agents()`
#   两条自动注册路径都写 `"ui": None` ⇒ 凡不是手写静态卡的 agent 永远没有原生界面按钮，
#   且**不报错**（配合手工 `UI_START` 白名单与手工 `modelcfg.SPECS`，一共四张表）。
#   修法：新增 `capabilities.py` 作唯一真相源 —— `UI_RECIPES`（地址/端口/探针/起法）、
#   `UI_TUI_ONLY`（显式判无界面）、`UI_DECIDED`（探测命中但人工判定不是网页）、
#   `probe_web()`（`--help` 三态：missing_recipe / tui_only / probe_failed）。
#   结论随扫描落 `custom_agents.caps` 列（带 `v` 口径版本，改判据就 +1 重探）；
#   读路径纯查库。闸门 `tests/test_registry_capability_matrix.py`：静态卡必须
#   `ok|tui_only` 二选一，缺声明即红；`探测失败` **不许当没有界面**（记 probe_failed）。
#   顺带：`reasonix web` 实测是真 Web UI（`/` text/html），token 进程级随机 ⇒ 地址
#   从 ui-start 日志捞 `#token=`（与 dsh 同手法）；`goose serve` 是 ACP 端点，判 tui_only。
#   v0.13.154：拉起成功后嵌页仍是**空白**（2026-10-25 复验 v0.13.153 时实测）。
#   根因：`applyChatMode()` 只在 `dataset.src != url` 时才给 iframe 赋值，而启动前后
#   URL 没变 ⇒ 启动前那次「连接被拒」的失败导航一直留在框里；探活与标题都显示活的，
#   用户看到的还是一片白。修法：就绪分支里显式 `f.src = f.src` 重载一次
#   （dsh 那种 token 随启动变化的场景，applyChatMode 已换新 URL，多刷一次无害）。
#   同批：`cmd` 式拉起不回收子进程，停掉 opencode 后进程表留 [defunct] 僵尸
#   （hub 自己不退、没人 wait）⇒ 加 daemon 线程 wait + 关日志句柄。
#   v0.13.153：**点「原生界面」没有拉起，只弹「目标界面未响应 / 启动失败：缺少凭据」**
#   （2026-10-25 用户报「有 web ui 原生界面的 agent 点击原生界面应该是正常拉起并运行，
#   但是现在点击原生界面没有拉起 显示错误」）。三层根因：
#   ① 只给按钮不给动作：v0.13.152 的「启动原生界面」要用户点第二次，点「原生界面」
#     先看到的是错误覆盖层 ⇒ 改为**点开即拉起**：用户明确的进入动作（点菜单行/模式
#     按钮）置一次性 `_embedAutoStartPending`，由本次探活消费；后台轮询不会顺手起服务。
#   ② 凭据弹两次 + 原始 401：调用处传 `termHeaders()` 会先 prompt 一次，`api()` 再
#     prompt 一次；两次取消即回 401「缺少凭据」⇒ 用户看到的就是「启动失败」。
#     改为只由 `api()` 注入 `x-hub-token`，401 给可操作提示（去设置应用 TERM_TOKEN）。
#   ③ 判活口径错两层：浏览器侧探活对 IPv6-only 后端永远假死（qwenpaw 只监听
#     `[::]:8088`，UI 走 hub 注入代理 :3103）；服务端单栈 `port_open` 同样判死。
#     新增 `GET /api/agents/{id}/ui-status`，由 hub **双栈**判定就绪，前端只读结论。
#   另两处必修：hub 的 PATH 只有 /usr/*bin（实测 /proc/<pid>/environ），`opencode`
#   装在 ~/.npm-global/bin ⇒ `cmd` 式拉起直接 FileNotFoundError，按用户级 bin 补 PATH；
#   并发探活会在 await 后各 append 一层，实测叠出**两个 .embed-dead**（同文案两个
#   按钮）⇒ 加探活序号闸，只让最后一次挂覆盖层。
#   v0.13.152：**设置子菜单「上下布局」根因＝模板多一个 </div>**（用户 2026-10-25
#   报「设置菜单的子菜单点开后应该是左右布局，不是上下布局；所有子菜单页面
#   布局应该是同一套，只是右边内容不一样」）。
#   真渲染取证：点「设置→模型」后 #page-settings-model 的父节点是 <body> 而不是
#   #mainWrap，坐标 [0,404,1440,…] —— 渲染在菜单**下方**、整宽压住侧栏。
#   根因：2026-10-20 的 37fdd382 在技能中心 .sp 收尾处多写了一行 </div>，浏览器
#   解析时把 <main> 提前关闭 ⇒ 该行之后的知识库/资产/设置五页全部掉到 body 下
#   （共 7 页错位 5 天）。删掉这一行即恢复：7 页全部回到 main 内，桌面在菜单
#   右侧、窄屏在图标条右侧（同一套 .sp/.sp-card 骨架，只换内容）。
#   为什么一直没被发现：HTML 不报错，既有测试只读字符串/接口/点击态，没有一条
#   断言父子关系。新增 tests/test_dom_structure.py（标准库 html.parser 模拟可选
#   结束标签），对旧版实测 4 条结构错误 + 7 个 section.page 在 main 外，修复后全绿。
#   v0.13.151：**按需拉起下原生界面按钮复还 + 模型设置自动发现**（用户 2026-10-25
#   报两事：「dsh的原生界面按钮也没有了」「模型设置页面也没有自动发现并添加
#   dsh omp 等agent，应该是自动发现并添加」）。
#   ① 按钮消失的根因是 v0.13.149 与 v0.13.149 的按需令**自相矛盾**：前者给
#     agent string-ui 的 embed/open 带 probe_port、按端口活性过滤（「死面板不出
#     按钮」扩展到 agent ui），后者把全部 Web UI agent 改成常态 stopped ⇒ 每个
#     Web UI agent 的原生界面按钮在**正常状态**下被删。改判：agent 入口不再带
#     probe_port（只 panel 类保留），停着的服务由前端 probeEmbed 出「目标界面
#     未响应」覆盖层（含切模式/重试），按钮本身必须在。同因修 dict-ui 注入
#     （cloudcli 原先端口死连原生会话都不注入）。
#   ② defaultModeOf 的 EMBED_FIRST 加 has('embed') 前置：改前它无条件返回
#     'embed'，而 applyChatMode 回落默认时又问一次它 ⇒ embed entry 缺失时两步
#     都落 embed，用户点开的是一张 url 为空的空白嵌页且出不去（同源第二道闸）。
#   ③ 模型设置页（modelcfg）原只列 SPECS+UNSUPPORTED 两张硬编码表 ⇒ 画像里
#     新登记的 agent 在页面上零存在感。改为合并 profiles 注册表自动发现；
#     omp 入 SPECS（argv-only：实测 `omp --help` 有 --model、配置根是 sqlite
#     无明文模型字段；且实测 --model 收 `ccr/<id>` 前缀形状，裸 ID 会被解析成
#     provider=openrouter 报 No API key ⇒ VALUE_PREFIX 补 omp）；dsh 入
#     UNSUPPORTED（实测 `dsh --profile tui --help` 仅 --resume/--agent-preset）。
#   红向自证三破坏点各自精确变红：加回 probe_port / 去掉 has('embed') /
#   去掉注册表合并段。
#   v0.13.150：**补登 QoderWake 长驻缺口**（用户 2026-10-24 报「不是所有 agent
#   都按需拉起，还有几个在长驻」）。实测：`qoderwake __daemon`（845MB）+
#   `qodercli-wake --sdk`（205MB）两进程长驻，QoderWake.service enabled+
#   Restart=always，而本仓画像零登记 ⇒ v0.13.149 的全军整改没覆盖它。
#   本版只改画像层一件事：profiles.py 加 qoder 静态卡（kind=agent，
#   ui :19820 + qodercli 终端入口，desc 写按需启动命令）。
#   宿主侧配套（随用户裁定执行，不在本仓）：QoderWake.service stop+disable。
#   两个实现陷阱：
#   ① 静态卡 id 必须取 `qoder` —— CLI watchlist 已有同 id 动态卡（qodercli
#      已安装），covered 机制靠 id 相同才压得住；test 钉「动态卡不得含 qoder」。
#   ② detect.systemd 单元名大小写敏感（`u in units` 精确匹配）⇒ 必须写
#      `QoderWake` 不能写 `qoderwake`；proc 两条兜底 daemon/SDK 两进程。
# ── v0.13.148 及更早的逐版根因已于 2026-10-24 归档至 ──
# ── CHANGELOG.md（脚本 scripts/extract_changelog.py + scripts/extract_changelog_newfmt.py 逐字搬运）──
# 此处**只保留当前版本**的根因，避免两份会各自漂移的副本。
_bg_tasks: set = set()


def _spawn(coro) -> asyncio.Task:
    t = asyncio.create_task(coro)
    _bg_tasks.add(t)
    t.add_done_callback(_bg_tasks.discard)
    return t


app = FastAPI(title="Agent Hub", version=VERSION)


@app.exception_handler(RequestValidationError)
async def _intake_validation_hint(request: Request, exc: RequestValidationError):
    """422 原文是 pydantic 的 `{"type": "string_too_short", "min_length": 2}`，
    MCP / 非 MCP 消费方拿到它只知道"参数错了"，不知道"该怎么办"（2026-10-22 实测：
    `hub_intake(task="")` 把 pydantic 错误体原样透给调用方）。

    只给 `/api/intake*` 补一句人话（`ok`/`error`），`detail` 原样保留 ⇒ 既有按
    `detail` 解析的调用方不受影响；其它端点的 422 形状一个字不动（避免面太大）。
    口径与文案在 `intake.validation_body()`（可单测，不必拉起整个 app）。
    """
    content = intake_mod.validation_body(request.url.path, jsonable_encoder(exc.errors()))
    return JSONResponse(status_code=422, content=content)


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
app.include_router(skill_store_mod.router)   # PT-20261020-02：商店搜索/安装（写侧独立模块）
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
app.include_router(botboy_mod.router)          # v0.13.170：/api/botboy/overview（只读）
app.include_router(github_mod.router)
app.include_router(prefs_mod.router)
app.include_router(modelcfg_mod.router)
app.include_router(ghsettings_mod.router)
app.include_router(settings_bundle_mod.router)   # PT-20260927-15：/api/settings/export|import
app.include_router(approvals_mod.router)         # P1-2：/api/approvals（ASK 审批队列与决定端）
app.include_router(skill_assets_mod.router)      # P1-5：/api/skill-assets（资产登记与状态机）
app.include_router(actionplane_mod.router)       # P2 A10：/api/actions（行动台账，只读为主）
app.include_router(admission_mod.router)        # P2 C7：/api/admission（外部资产准入六阶段）
app.include_router(hublog_mod.router)
app.include_router(refscout_mod.router)          # PT-20261021-04：/api/ref/*（情报卡）
app.include_router(intake_mod.router)            # PT-20261022-01：/api/intake/task（四查一简报）
if os.getenv("HUB_CATALOG", "0") == "1":
    import api_catalog as api_catalog_mod
    app.include_router(api_catalog_mod.router)    # PT-20261025-12：统一编目只读门面

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
    # v0.13.172：出口面审计一行入日志 —— 谁被扣、白名单拼错、dispatch 是否开
    _scope = hubmcp.TOOL_SCOPE
    print("[Agent Hub] Hub MCP 出口：%d 个工具%s%s%s" % (
        len(_scope.get("kept") or []),
        "，扣掉 %s" % ",".join(_scope["removed"]) if _scope.get("removed") else "",
        "，dispatch 闸门挡下 %s" % ",".join(_scope["blocked"]) if _scope.get("blocked") else "",
        "，白名单里不存在 %s" % ",".join(_scope["unknown"]) if _scope.get("unknown") else "",
    ))
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
    # 内存三口径（PT-20261024-08 B5）：**本体 / 托管子进程 / cgroup 记账**。
    # 为什么必须分开：2026-10-24 实测同一服务三个口径差 7 倍（判例 agent-knowledge/259）
    # ——进程 RSS 232MB、cgroup 进程合计 753MB、cgroup 记账 1.4~2.0GB（差额是 page cache）。
    # 只报一个数必然误导：按进程口径低估（漏掉托管的 agent CLI 与文件缓存），
    # 只看 cgroup 总量又看不出「是本体大还是被托管的进程大」。
    # ⚠ 三个字段都是**读数**不是常量；用它们做告警判据前必须先量自己的方差
    #   （判例 259：轮间极差 < |两读数之差| 才是「有差异」的前提）。
    # ⚠ 拿不到一律是 null（不是 0）：0 与「测不到」是两件事。
    try:
        out["memory"] = memview.memory_snapshot()
    except Exception as e:  # noqa: BLE001
        out["memory"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
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
        # 2026-10-22 PT-13：免费模型重排的成功率必须可观测。
        # 免费池实测成功率 34~44%（会把请求路由到审核模型），不报出来就只能靠人工复现。
        try:
            import skill_rerank
            out["rerank"] = skill_rerank.stats()
            # 2026-10-22：判定缓存必须可观测 —— 否则「有缓存」与「缓存从没命中」长得一样。
            import judge_cache
            out["judge_cache"] = judge_cache.stats()
        except Exception as e:  # noqa: BLE001 — 自证端点不许被情报字段打挂
            out["rerank"] = {"error": "%s: %s" % (type(e).__name__, str(e)[:120])}
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
app.include_router(summarize_router)
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
async def get_memory_alias(request: Request, query: Optional[str] = None, limit: int = 10):
    """旧端点别名。**无 query** 时回本地 L1 列表；**有 query** 时转发到 /api/memory/search。

    ⚠ 2026-10-24 修一处现网 500：`GET /api/memory?query=x&limit=5` 实测返
    `AttributeError: 'Query' object has no attribute 'split'`。
    根因：本函数**位置传参** `search_memory(query, limit)`，而 search_memory 的签名是
    `(request, q, limit, sources, episodic)`（src/memory.py）⇒ query 落到 request 位、
    limit 落到 q 位、**sources 落到 limit 位**，拿到的是 FastAPI 的 `Query(default=…)`
    对象而不是字符串 ⇒ `_split_sources(sources)` 调 `.split()` 炸。
    引入于 v0.13.27~29 给 search_memory 加 request 参数时 alias 未同步；
    之所以一直没被发现，是因为**没有任何测试覆盖本 alias 的带 query 路径**
    （`grep -rn 'api/memory"' tests/*.py` 空）。
    修法：**改关键字传参**，并显式带 request。
    """
    if not query:
        rows = db.query("SELECT * FROM memories WHERE status='active' ORDER BY id DESC LIMIT ?",
                        (limit,))
        return {"memories": rows, "count": len(rows)}
    return await memory_mod.search_memory(request=request, q=query, limit=limit)


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


#: 技能调用转录采集周期（秒）。默认 600；≤0 关闭本条循环。
#: 为什么不做成「打开页面时现扫」：首次全扫 800+ 个转录文件要几秒到十几秒，
#: 挂在 GET 上等于用一个仪表盘拖住主线程；后台慢拍 + 页面读现成账，互不干扰。
HUB_SKILL_SCAN_SEC = int(os.getenv("HUB_SKILL_SCAN_SEC", "600"))


async def skill_scan_loop():
    """第二数据源：从 pi/claude/codebuddy/codex 的会话转录里补采真实技能调用。

    背景（根因）：hub 只在自己那三个显式路由上记账，而 agent 真实用技能的动作是
    「自己 Read SKILL.md」——完全不经过 hub。只补文案不改生产者，面板永远只有测试
    caller（判例 228）。本循环按文件增量扫，只认读取类动作与 Skill 工具调用。

    首轮延后 30s：先让 hub 开接请求；异常不打死循环（否则一次偶发采集永久停摆）。
    """
    if HUB_SKILL_SCAN_SEC <= 0:
        return
    await asyncio.sleep(30)
    while True:
        try:
            r = await asyncio.to_thread(skill_direct_mod.scan)
            if r.get("events"):
                print("[skill-scan] 采到 %s 条调用 %s（扫 %s 文件，跳过 %s）" % (
                    r.get("events"), r.get("by_caller"),
                    r.get("files"), r.get("skipped")), flush=True)
        except Exception as e:  # noqa: BLE001
            print(f"[skill-scan] 采集异常（下轮重试）{type(e).__name__}: {str(e)[:160]}", flush=True)
        await asyncio.sleep(HUB_SKILL_SCAN_SEC)


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
    # P1-5：技能资产表（幂等建表；与 skill.py 的整包摘要一起构成 A9 的资产准入）
    skill_assets_mod.ensure_schema()
    # P2 A10：行动三表（requests/decisions/results），幂等建表
    actionplane_mod.ensure_schema()
    # PT-20261021-04：情报卡与采集任务两表（幂等建表）
    refscout_mod.ensure_schema()
    tasks_mod.set_context(
        chat_fn=lambda a, m, s=None, mo=None, tr=None: _chat_dispatch(a, m, s, mo, tr),
        # 派发目标取**可执行池**而非菜单：① `AgentDiscovery` 从来没有
        # `all_configs()`（2026-10-27 实弹 500 的根因，单测因注入假 ctx 全绿）；
        # ② 菜单里 20 张卡只有 claude/jcode 能真跑 chat()，把菜单当池子
        # ⇒ Leader 抽中 dsh/cloudcli 就是必失败任务（同日实跑 239b4d553a 取证）。
        agent_ids_fn=dispatchable_ids)
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
    _spawn(skill_scan_loop())   # 技能调用第二数据源（转录采集，补 hub 看不到的直读）
    _spawn(refscout_mod.refresh_loop())   # 情报卡到期自动重跑（30 天/次，REF_AUTO_REFRESH=0 可关）
    # PT-20261024-08 B3/B4：保留期清理必须有独立心跳。
    # 为什么必须有（实测）：`term_recordings` 258.7 万行 / 597MB，而 `sweep()`
    # **只在 `Recorder.close()` 里调** ⇒ 长开会话 ⇒ 录像只增不减
    # （src/main.py 原注释自己写着「⚠ 未治本：sweep() 只在会话 close 时跑，
    #   表一天长 44 万行」）；`profile_events` 3,250 行/天且**全仓无 DELETE**。
    # ⇒ 不寄生在任何会话生命周期上，自带 1 小时心跳。
    _spawn(retention.retention_loop())
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
