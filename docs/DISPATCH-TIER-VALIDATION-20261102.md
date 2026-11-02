# 派发/档位链路 验证清单与实测结论（2026-11-02）

> 取证环境：agenthub v0.13.188（`/health`）、CCR `127.0.0.1:3456/v1/models` = **9 个 alibaba 模型**。
> 本文所有数字都是本机实测，命令都贴在对应步骤里，可复跑。
> 同批产物：`work/tier-model-probe-20261102.py|.json`（模型能力探针）、`work/leader-model-probe-20261102.py`（拆解能力探针）。

---

## 0. 一句话结论

**派发功能当前 100% 不可用，根因不是编排逻辑，而是配置里的模型 ID 已被上游摘掉**
（`agnes/agnes-2.5-flash` 不在 CCR 的 9 个 ID 里）。链路本身（拆解 → 档位 → 执行 → 归因）设计是好的，
但**缺一道「配置里的模型必须 ∈ 网关清单」的闸门**，所以它坏掉时不报错、只在 `/health` 里留一个没人看的 `watch:false`。

---

## 1. 五条实测发现（按严重度）

### F1（致命）Leader 拆解全挂：`MANAGER_LLM_MODEL=agnes/agnes-2.5-flash` 不在网关清单

```
POST /api/tasks/decompose → 400
{"error":{"message":"All target providers failed. [model_resolution|400]
 Model \"agnes/agnes-2.5-flash\" is not configured for target provider openai.
 Allowed models: qwen3.8-max, qwen3.8-flash, deepseek-v4.1-flash, ..."}}
```
直连复验：同一把 key 打 `alibaba/qwen3.8-max` 与 `alibaba/deepseek-v4.1-flash` 均 **200**，只有 agnes 400。
⇒ 不是凭据/网络问题，是模型 ID 失效。这是 AGENTS.md 记过的第四次同源事故（09-06 `minimax-m3:free`、
09-19 `'ultra'`、09-23 `qwen3.8-flash` 缺前缀），**同一类错误又发生了，说明闸门至今没建**。

### F2（致命，且静默）非会话模型槽同样失效，但**不报错、只返空**

`llm.complete_free()` 的三层都是 agnes：
```
MODEL   = agnes/agnes-2.5-flash     (.env:27 / llm.py:34 / config.py:82)
memory  = agnes/agnes-2.5-flash     (.env:31 / llm.py:51)
fallback= agnes/agnes-2.5-flash     (.env:33 / llm.py:61)
```
实测 `complete_free(...)` → `('', '')`。它把两个模型都试一遍、都失败、然后**返回空串当降级**（`llm.py:101-105`）。
调用方（L2 记忆压缩 `memory.py`、技能重排 `skill_rerank.py`）拿到空串就机械退化。
**这是本仓库最贵的那类故障：全绿、无声、功能已死。**

### F3（观测点的情报没有被用起来）

`GET /health` 里已经写了答案：
```
"watch": {"agnes/agnes-2.5-flash": false}   ← 健康检查早就知道它不在清单
"state": "ok"                                ← 但整体还是绿
```
`gwprobe.py` 的设计意图（注释 21-24 行）就是"清单里没这个 ID 当天就能看出来"，但它**只兑情报、不改状态**
（注释 64 行明确写的），也没有任何代码拿 `watch:false` 去阻断或告警。⇒ 情报有了，闭环没接。

### F4（档位映射：t3 是空档，难任务没有强模型）

`GET /api/tasks/tiers` 实测：`t3 = ["openrouter/openai/gpt-6.1-terra-pro"]`，而清单只有 9 个 alibaba 模型。
```
resolved.difficulty=3 → tier=t3 → model=null, reason="no_tier_model_in_catalog"
```
行为是对的（不猜、不造假），但**结果是最难的任务反而不指定模型**，只把执行者抬到 `claude-code`。
`t1` 的第二成员 `agnes/agnes-2.5-flash`（`tiercfg.py:41`）也是死的——它靠 catalog 过滤兜住了，
所以没炸，但属于同一类死配置。

### F5（归因指标测错了对象）

`tasks.model` 对 CLI 型执行者写的是 `resolve_model(model)` 的返回值，即**我们注入了什么**
（`cli_agent.py:318,359`；`resolve_model` 只是把请求串原样返回，见 `cli_agent.py:292-300`），
**不是网关实际发了什么**。而 `tasks_runner.py:398-402` 的注释与 `model_tier_effective` 的 note
（"回包模型≠计划模型 ⇒ 网关改写了 ID"）都声称它是观测值。
⇒ 对 claude/codex/pi/grok/hermes/omp 这条通道，这个指标**测不到网关改写**。
另有前缀剥离：直连 CCR 请求 `alibaba/qwen3.8-max`，回包 `model` 是 `qwen3.8-max`（厂商前缀被剥）——
任何拿回包 model 与计划值直接比对的实现，都会把正确路由判成"被改写"。

### F6（次要）清单里有一个死模型

`alibaba/decision-model-preview` 在清单里，但每次调用 400（`All target providers failed`）。
清单准入 ≠ 可用。任何 `first_usable()` 若选中它，任务必挂。

### F7（扩散面）pi 的 `defaultModel` 也是死 ID ⇒ 子 agent 351ms 内静默死掉

`~/.pi/agent/settings.json` 的 `defaultModel = deepseek/deepseek-flash`，实测打网关 **400**（不在 9 个 ID 里）。
症状：任何**不显式指定模型**的 pi 子 agent 立刻返回空——`total_tokens: 0, tool_uses: 0, duration_ms: 351`，
既不报错也不产出。2026-11-02 复现一次（文献调研子 agent `982efefd`），换 `qwen3.8-flash` 后正常启动。
⇒ 死模型 ID 是**机房级**问题，不止 hub 的 3 个槽位：hub .env(3) + hub 代码默认(3) + tiercfg(1) + **各 agent CLI 自身默认模型(N)**。

### F8（工具层）Agent 工具的 `model` 参数写错形式**不报错，静默回落到死默认**

复现三步（2026-11-02）：
1. `model="qwen3.8-flash"`（无 provider 前缀） → 子 agent **167ms / 351ms 空返回**，`total_tokens:0 tool_uses:0`，无任何错误
2. `model="Alibaba/qwen3.8-flash"`（带 provider） → **3.8s 正常回话**
3. 查 `~/.pi/agent/settings.json`：`defaultProvider="ccr-free"`、`defaultModel="deepseek/deepseek-flash"`，直打 CCR 也是 **400**

机制：fuzzy 名字解析失败 → 回落 `defaultModel` → 那个 ID 已死 → 网关 400 → 子 agent 未启动即终止，**错误不进 result**。
⇒ 与 F1/F2 同一个根因（死模型）+ 一个新的失败模式（**错误被吞**）。写子 agent 不指定模型或写裸名，就是抽盲盒。

---

## 2. 链路点数（"全链路修"之前必须能数出来）

死模型 `agnes/agnes-2.5-flash` 这一条根因，链路上共 **11 个点**：

| # | 位置 | 现状 | 处理 |
|---|---|---|---|
| 1 | `.env:27 MANAGER_LLM_MODEL` | agnes | 改（需用户裁定模型） |
| 2 | `.env:31 MEMORY_LLM_MODEL` | agnes | 改 |
| 3 | `.env:33 MEMORY_LLM_FALLBACK_MODEL` | agnes | 改 |
| 4 | `src/config.py:82` 默认值 | agnes | 改（否则 .env 缺失时复发） |
| 5 | `src/llm.py:34` `MODEL` 默认 | agnes | 改 |
| 6 | `src/llm.py:51` `memory_model()` 默认 | agnes | 改 |
| 7 | `src/llm.py:61` `free_fallback_model()` 默认 | agnes | 改 |
| 8 | `src/llm.py:1-31` docstring（"10-29 裁定全部走 agnes"） | 已过期 | 加横幅改判，不删 |
| 9 | `src/tiercfg.py:41` `DEFAULT_TIERS["t1"]` 第二成员 | 死成员 | 换成已验活的模型 |
| 10 | `tests/test_memory_llm_free.py` / `test_gwprobe.py` | 断言 agnes 是默认 | 改断言（否则改完测试红） |
| 11 | **缺失闸门**：配置模型 ∉ 网关清单时应 fail-loud | 不存在 | 新增（这是唯一能防第四次复发的点） |
| 12 | `~/.pi/agent/settings.json` `defaultModel`（属受保护面，需报请） | `deepseek/deepseek-flash` 死 | 报请后改 |
| 13 | `settings.json` `compaction.modelOverrides` 里的 `ccr-free/agnes/*` 键 | 2 个死 ID | 改（否则压缩预算配置匹配不上） |

不改：`docs/`、`CHANGELOG.md` 里 agnes 的历史记录——那是历史事实，按规矩加横幅而不改写。

---

## 3. 验证步骤清单（分 5 组，A→E 递增成本）

判定口径：每步都要能回答 PASS / FAIL，**不准拿 SKIP 当 PASS**；同一判据最多重试 2 次。

### A 组：静态一致性（0 成本，秒级）

**A1 配置里的模型是否都在网关清单里**
```bash
cd /fs/1000/ftp/技术文档/agenthub
KEY=$(grep '^MANAGER_LLM_API_KEY=' .env | cut -d= -f2)
curl -s http://127.0.0.1:3456/v1/models -H "Authorization: Bearer $KEY" | python3 -c 'import json,sys;print("\n".join(m["id"] for m in json.load(sys.stdin)["data"]))' | sort > /tmp/catalog.txt
grep -hoE '^(MANAGER_LLM_MODEL|MEMORY_LLM_MODEL|MEMORY_LLM_FALLBACK_MODEL)=.*' .env | cut -d= -f2 | sort -u > /tmp/used.txt
comm -23 /tmp/used.txt /tmp/catalog.txt
```
- 预期：**无输出**
- 判断点：有输出 ⇒ F1/F2 未修完，停在这里别往下跑

**A2 档位表里每个模型是否都在清单里**
```bash
curl -s http://127.0.0.1:3102/api/tasks/tiers | python3 -c '
import json,sys,subprocess
d=json.load(sys.stdin)
cat=set(open("/tmp/catalog.txt").read().split())
for t,ms in (d.get("tiers") or {}).items():
    for m in ms: print(("OK  " if m in cat else "DEAD"), t, m)
print("catalog_size", (d.get("catalog") or {}))'
```
- 预期：全部 `OK`，`catalog_size` 的 `size` = 9
- 判断点：出现 `DEAD` ⇒ 改 `tiercfg.py:41`（t1 第二成员）

**A3 档位表是否覆盖 t1/t2/t3 三档都有活模型**
```bash
curl -s http://127.0.0.1:3102/api/tasks/tiers | python3 -c '
import json,sys;d=json.load(sys.stdin)
r={x.get("difficulty"):x for x in d.get("resolved",[])}
for k in sorted(r, key=lambda v:(v is None, v)):
    x=r[k]; print("difficulty", k, "->", x.get("tier"), x.get("model"), x.get("reason"), "agent", x.get("agent"))'
```
- 预期：difficulty 1/2/3 的 `model` 都非 null
- 判断点：`difficulty 3 → model null / no_tier_model_in_catalog` ⇒ F4 未修，需要给 t3 指定一个清单内的强模型

### B 组：单测（30 秒）

**B1 档位决议单测**
```bash
cd /fs/1000/ftp/技术文档/agenthub && venv/bin/python3 -m pytest tests/test_tiers.py -q 2>&1 | tail -3
```
- 预期：`26 passed`（改断言后可能条数不变）
- 判断点：红了先看是不是第 10 点（测试里把 agnes 当默认）没跟着改

**B2 归因/状态机单测**
```bash
venv/bin/python3 -m pytest tests/test_task_attribution.py tests/test_task_statemachine.py -q 2>&1 | tail -3
```

### C 组：配置生效验证（10 秒，改完 .env 后必做）

**C1 进程内实际读到的值**（不是文件里写的值）
```bash
cd /fs/1000/ftp/技术文档/agenthub/src && env $(grep -E '^(MANAGER_LLM|MEMORY_LLM)' ../.env | xargs) ../venv/bin/python3 -c '
import llm; print("MODEL", llm.MODEL); print("memory", llm.memory_model()); print("fallback", llm.free_fallback_model())'
```
- 判断点：三个都不是 agnes

**C2 后台模型真能出正文**
```bash
cd /fs/1000/ftp/技术文档/agenthub/src && env $(grep -E '^(MANAGER_LLM|MEMORY_LLM)' ../.env | xargs) ../venv/bin/python3 -c '
import asyncio, llm
print(asyncio.run(llm.complete_free("你是压缩器","把这句话压成一句：派发功能的验证今天要做完。")))'
```
- 预期：`('…正文…', '模型ID')`
- 判断点：返回 `('', '')` ⇒ 仍在静默失败（F2 未修）

### D 组：端到端拆解 dry_run（1 次 Leader 调用，约 3-9 秒）

**D1 拆解 + 档位决议**
```bash
cd /fs/1000/ftp/技术文档/agenthub
TOK=$(grep '^TERM_TOKEN=' .env | cut -d= -f2)
curl -s -m 180 -X POST http://127.0.0.1:3102/api/tasks/decompose \
 -H 'Content-Type: application/json' -H "x-hub-token: $TOK" -d '{
 "goal":"统计 agenthub 下 .py 文件数量；对比 src/tasks.py 与 src/tiercfg.py 职责；给出难度→模型映射的取舍建议",
 "dry_run":true, "verify_kind":"nonempty"}' | python3 -c '
import json,sys;d=json.load(sys.stdin)
assert d.get("tasks"), d
for t in d["tasks"]:
    print(t.get("id"),"d=",t.get("difficulty"),"tier=",t.get("tier"),"model=",t.get("model_planned"),
          "agent=",t.get("agent_id"),"reason=",t.get("tier_reason"),"deps=",t.get("deps"))
print("tiers_note:",json.dumps(d.get("tiers"),ensure_ascii=False))'
```
- 预期：3-5 个 task，`difficulty` 覆盖 1~3，每个 `model` 非 null（codebuddy/cursor 除外，它们 `native`），
  `tier_reason` 为 `explicit` 或 `pinned:*`
- 判断点（**三个都要看**）：
  1. `difficulty=3` 的 task 有没有 model？没有 ⇒ F4
  2. `agent_id` 是否都落在 `dispatchable_ids()` 里（不能出现 `scan-*`）
  3. 每个 task 的 `model` 是否带 `alibaba/` 前缀

**D2 确认拆解没有越域**（Leader 不许给 native 执行者塞 CCR ID）
```bash
curl -s http://127.0.0.1:3102/api/tasks/tiers | python3 -c 'import json,sys;d=json.load(sys.stdin);print(json.dumps(d.get("mapping"),ensure_ascii=False),json.dumps(d.get("agent_tiers"),ensure_ascii=False))'
```
- 判断点：`codebuddy`/`cursor` 的 task `model_planned` 必须是空或 native，不能是 `alibaba/...`

### E 组：真实跑一轮（花钱、耗时 2-10 分钟，**最后做**）

**E1 起一个真 run**
```bash
TOK=$(grep '^TERM_TOKEN=' .env | cut -d= -f2)
curl -s -X POST http://127.0.0.1:3102/api/tasks/decompose -H 'Content-Type: application/json' -H "x-hub-token: $TOK" -d '{
 "goal":"在 agenthub 仓库里统计 .py 文件数量并把数字写进 /tmp/pycount.txt",
 "dry_run":false, "auto_run":true, "verify_kind":"nonempty"}' | python3 -c 'import json,sys;print(json.load(sys.stdin)["run_id"])'
```
记下 run_id。

**E2 跟踪到终态**
```bash
watch -n 5 "curl -s http://127.0.0.1:3102/api/tasks/<run_id> | python3 -c 'import json,sys;d=json.load(sys.stdin);[print(t[\"task_id\"],t[\"status\"],t.get(\"agent_id\"),t.get(\"tier\"),t.get(\"model_planned\"),t.get(\"model\")) for t in d[\"tasks\"]]'"
```
- 判断点（这一屏就是验收表）：
  1. 每行 `model` 非空（除 native 执行者写 `native`）
  2. **不同 difficulty 的 task 用了不同 model** —— 这是整个功能的唯一价值主张，若全是同一个模型则档位空转
  3. `status` 终态为 success/submitted，不能停在 running（历史事故：超时处理器里 SQL 参数错配导致永久 running）

**E3 归因核对（F5 的验证）**
```bash
sqlite3 data/agents.db "SELECT task_id,difficulty,agent_id,model_planned,model,attempts,spent_tokens FROM tasks WHERE run_id='<run_id>';"
```
- 判断点：`model_planned ≠ model` 的行，逐个解释是"升档改写"还是"网关改写"。
  **注意 F5**：对 CLI 型执行者，`model` 只证明"注入了什么"，不能当"网关发了什么"的证据；
  要验网关侧，必须查 CCR 账本或直连打一次。

### 可选 G 组：能力矩阵（决定 t1/t2/t3 该放谁）

`work/tier-model-probe-20261102.py` 已跑过一轮，结论（2026-11-02）：
- 7 个候选 6 个可 chat、全部支持 `tool_calls`
- `alibaba/decision-model-preview` 400 死
- 时延：`deepseek-v4.1-flash` 844ms < `qwen3.8-flash` 883ms < `deepseek-v4.1-flash-1017` 871ms
  < `deepseek-v4-flash-0731` 1269ms < `qwen3.8-max` 1513ms < `deepseek-v4-pro-0813` 2076ms
- 拆解能力（`work/leader-model-probe-20261102.py`，各 3 次）：`qwen3.8-flash` 3.2/7.2/6.2s 全部出合法 JSON 数组；
  `qwen3.8-max` 7.3/9.2/7.9s 同样全部合法；**`deepseek-v4.1-flash` 出不了 JSON 数组**（不适合作 Leader）

---

## 4. 改造建议（按外部证据 + 本机实测）

| 槽位 | 现状 | 建议 | 依据 |
|---|---|---|---|
| `MANAGER_LLM_MODEL`（Leader 拆解） | agnes（死） | `alibaba/qwen3.8-flash` | 3/3 出合法 DAG，比 max 快 1.5-2s |
| `MEMORY_LLM_MODEL`（后台压缩） | agnes（死） | `alibaba/deepseek-v4.1-flash` | 844ms 最快，只做压缩不需 JSON |
| `MEMORY_LLM_FALLBACK_MODEL` | agnes（死） | `alibaba/deepseek-v4-flash-0731` | 与主槽不同源，避免两个槽同时死 |
| `tiers.t3`（难任务） | `openrouter/openai/gpt-6.1-terra-pro`（不在清单） | `alibaba/qwen3.8-max` | 清单内最强、工具可用、tool_calls 正常 |
| `tiers.t1` 第二成员 | agnes（死） | `alibaba/qwen3.8-flash` | 清单内、快、可作降级位 |

**新增闸门（防第四次复发）**：启动时 + `/health` 里断言
`{MANAGER_LLM_MODEL, MEMORY_LLM_MODEL, MEMORY_LLM_FALLBACK_MODEL} ∪ tiers 全部成员 ⊂ catalog`，
不满足时 `/health` 的 `status` 降级并在 `watch` 里显式列出，**而不是只写 `false` 没人看**。

---

## 5. 本清单不覆盖的（避免误读为已验）

- 执行者真实能力对比（哪个 agent 干哪类活更好）——本次只验"配置对不对、拆解成不成、模型有没有被用上"
- 多模型并行的**收益**（成本/质量）——需要 ≥10 个 run 的样本，当前 audit 只有 n=7/n=5，样本不足
- `POST /api/tasks/{id}/replan` —— 与 decompose 同一条 `llm.MODEL`，F1 修完后需单独复验
---

## 6. 第二轮续做（2026-11-02 23:05~23:20，pi 会话）

上一轮停在「写 `src/modelgate.py`」，闸门**写完但没有接线**。本轮把它接上并跑完 A/B/C/D 四组。

### 6.1 上一轮遗漏的 4 个死 ID（由新闸门自己抓出来的）

`modelgate.check()` 一旦接线，立刻在 14 个配置点上抓出上一轮没发现的死 ID —— **它们都不在 `.env` 前 3 行**：

| 标签 | 值 | 后果 |
|---|---|---|
| `memory_fallback` | `alibaba/deepseek-v4-flash-0731` | 兜底槽也死了 ⇒ 主槽失守时直接 `('','')` |
| `vitals_rt` | `agnes/agnes-2.5-flash` | L4 真请求探针**恒红**，而 `/health` 的 `watch` 只写 `false` |
| `judge` | `agnes/agnes-2.5-flash` | 判定后端每条调用都失败 |
| `tier/t1/1` | `alibaba/deepseek-v4-flash-0731` | t1 降级位死（catalog 过滤兜住了，没炸） |

> 判据价值就在这里：上一轮靠"人眼扫 `.env` 前 3 行"只找到 3 个点，闸门一跑出来 4 个。
> 这正是"链路点数说不全 = 没修完"的实证。

### 6.2 又一个静默降级（`_FREE_MIN_CHARS = 20`）

`.env` 全改活之后 `complete_free()` 仍返回 `('', '')`。逐个模型直调发现**两个模型都正常出话**，
但输出是 13 字中文 → 被 `_FREE_MIN_CHARS = 20` 判成"不可用"。

```
直调 chat_tools_loop: '今天必须完成派发功能验证。'  ✅
complete_free:        ('', '')                     ❌ ← 20 字门槛把合法结论丢掉
```

⇒ 20 是英文量级；中文的完整结论常年 10~15 字，压缩类任务**天然短**。
改为 6，空串与审核标签仍由前缀表挡。改后实测：`('派发功能验证今日须完成。', 'alibaba/deepseek-v4.1-flash')`。

### 6.3 闸门接线（第 11 点落地）

- `/health` 新增 `model_gate` 字段：拿 `ccr_gateway.models`（gwprobe 缓存，**不额外发网络**）与
  14 个配置点逐条比对；`checked=false`（清单取不到）报 `unknown` 不判死，`missing` 逐条列标签+ID。
- 启动时 `modelgate.log_summary(...)` 打一行结论，缺项逐条 `log.error` —— 因为"只写进 /health"
  的事实已经证明没人看（`watch:false` 在册多日无人处理）。
- 启动时追加 `gwprobe.status()` 预热，否则首次 `/health` 的 `model_gate` 恒 `unknown`。
- **不改 `status`**：网关不可达 ≠ hub 坏了（与 `code_stale` / `memory_backend` 同一意图）。

### 6.4 本轮实测结果（对照 §3 的 A/B/C/D）

| 组 | 判据 | 结果 |
|---|---|---|
| A1 | 配置模型 ⊂ 清单 | **PASS** 14/14 在册（清单 11 个 ID） |
| A2 | 档位成员 ⊂ 清单 | **PASS** t1/t2/t3 六成员全在 |
| A3 | 三档都有活模型 | **PASS** d1→`deepseek-v4.1-flash`、d2→`qwen3.8-flash`、d3→`qwen3.8-max` |
| B | 单测 | **PASS** 96 passed（含新增 `tests/test_modelgate.py` 13 例）+ 全仓 3740 passed |
| C1 | 进程内读到的值 | **PASS** 五槽全是 `alibaba/*`，无 agnes |
| C2 | 后台模型真出正文 | **PASS**（修 20 字门槛后）`('派发功能验证今日须完成。', 'alibaba/deepseek-v4.1-flash')` |
| D1 | 拆解 + 档位决议 | **PASS（模型侧）** 4 个 task，d=1/2/3 拿到**三个不同模型**；`tier_reason` = ok/pinned:ok |
| D1 | 执行者档位 | **不可判定**：独立进程里 `dispatchable_ids()` = 空（没跑 discovery），agent_id 全落 jcode |
| E | 真实跑一轮 | **未执行**：要 hub 重启 + 花 token，留给用户点头 |

D1 的实跑输出（`dry_run=true`，未落库）：

```
t1 d=1 tier=t1 model=alibaba/deepseek-v4.1-flash  agent=jcode reason=ok
t2 d=2 tier=t2 model=alibaba/qwen3.8-flash        agent=jcode reason=ok
t3 d=2 tier=t2 model=alibaba/qwen3.8-flash        agent=jcode reason=ok
t4 d=3 tier=t3 model=alibaba/qwen3.8-max          agent=jcode reason=pinned:ok deps=[t1,t2,t3]
```

⇒ **"不同难度 → 不同模型"这条唯一价值主张，第一次拿到端到端证据。**

### 6.5 仍未闭合

1. **hub 未重启** ⇒ 以上 `.env` / `tiercfg` / `model_gate` 全部只在磁盘上，生产进程仍是旧配置
   （`term_sessions=2`，`hub-restart.sh` 拒；要用户关会话或签字 `HUB_RESTART_FORCE=1`）。
2. **E 组（真 run）没跑**：档位是否真把不同模型发出去、`tasks.model` 归因是否可信（F5），要真 run 才验得到。
3. **`alibaba/decision-model-preview` 在册但 400**（F6 未修）：闸门只能证明"ID 在清单里"，
   证不出"能出话"。真正的准入要在写入时打一发 `max_tokens=1`，本轮未做。
4. **pi `settings.json` 的 `compaction.modelOverrides`**：23 个键里 **15 个对着已下架的 ID**
   （按 `Alibaba/x → alibaba/x` 归一后与 11 个清单 ID 比对，2026-11-02 23:15 实测：`ccr-free/*` 10 个、
   `Alibaba/*` 5 个，含 `glm-5.2`/`qwen3.6-flash`/`qwen3.7-*`/`deepseek-v4-flash-0731`）。
   当前默认模型 `Alibaba/deepseek-v4.1-flash` **有对应键且活着**，所以压缩预算配置仍匹配得上 ——
   死键是噪音不是故障。属受保护面（`~/.pi/agent/**`），未动。
5. **`defaultModel` 已于 22:44 由上一轮会话改成 `Alibaba/deepseek-v4.1-flash`**（带备份），F7 已闭合。
