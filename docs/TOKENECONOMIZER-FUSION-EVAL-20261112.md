# TokenEconomizer 评估与融合报告

- 评估对象：`https://github.com/microsoft/TokenEconomizer`（Apache-2.0，Python，Microsoft Research / UC Berkeley）
- 评估时间：2026-11-12
- 评估方式：源码浅克隆 `/vol1/work/TokenEconomizer-eval`（HEAD `0566b60`，2026-11-11）+ `docs/` 全文 + GitHub API 元数据 + **本机隔离实测**（`TOKENECONOMIZER_HOME` 独立目录、`TE_SHADOW=1`、不装插件、不碰 CCR、不重启 hub）
- 结论口径：只写实测到的；没测的写「未测」
- 体例沿用 `agenthub/docs/T3CODE-FUSION-EVAL-20261109.md`；框架依据 `wiki/concepts/开源项目融合评估框架.md`

---

## 0. 结论（先看这段）

1. **分层不同但同向**：TE 是「单个 coding agent 会话内的黑盒降本插件」，agenthub 是「编目 / 记忆 / 技能 / 派发 / 面板」管理层。与 t3code、Orca、Botboy 同判 —— **并存不合并**。
2. **许可证无障碍**：Apache-2.0，非 copyleft，无 NOTICE 文件。比 ClaudeReports（无 LICENSE、生成器未开源 ⇒ 只借方法不装仓）干净得多，这是本次与历次评估最大的不同。
3. **但不建议直接装**，三条实测硬阻断：
   - **计价口径不兼容**：本机 4 个在役模型 ID 全部触发 `ValueError: no weight entry for billed model`；`tokeneconomizer/pricing.py:169` 对未知 billed id **硬失败**，装上去连它自己的 `ledger.jsonl` 都记不了（§3.1）。
   - **收益口径错位**：本机 99.6% 输入是 cacheRead（`skills/on-demand-execution/SKILL.md:67`），且大量会话跑免费池（`cost_usd=NULL`，`agenthub/src/usage_ledger.py:17-19`）。TE 论文 18.4–30.9% 降本建立在商业模型按 token 计费上；本机省下的大头是「本来不要钱的部分」。
   - **旁路模型调用与网关现状冲突**：judge / redirect / proposer 经 `claude -p` / `pi -p` / `codex exec` 走 CCR；实测 `127.0.0.1:3456/v1/models` 返回 **0 个模型**（网关降级）。默认 `paid_model=""` ⇒ Claude Code 用裸 `haiku`（`sidecall.py:285-302`），而本机 claude 默认是 `openrouter/stealth/crouton-alpha`，调用会失败，且付费调用无逐次确认。
4. **最值得借的是四件设计，不是代码**：① 单源计价（记录口径 ≡ 报告口径，不许漂移）② 改 prompt / 工具清单前等 prompt cache 过期（60 分钟静默窗）③ 证据门槛 + 全可逆 + 永不自动停 turn 的干预三原则 ④ `shadow` 只算不落地。
5. **推荐档位 = B「只借鉴 + 影子试点」**：`TE_SHADOW=1` + 独立 HOME 只读观测，不动任何生产配置；等 CCR 恢复、计价表补齐后再评 A。

**下一步（2 分钟内）**：在 A / B / C 里选一档 ——
- A 并存：装成独立插件，只给 pi/codex 用，`TE_SHADOW=1` + `toolprune=propose` + `microskills=off`（需先 `chmod +x` + 补 `MODEL_WEIGHTS`）；
- **B 只借鉴（推荐，0 生产改动）**：按 §5 的 P0/P1 落规则和机械断言；
- C 深度合并进 agenthub：不建议，理由见 §6。

---

## 1. 项目基本面

| 项 | 值 | 取证 |
|---|---|---|
| 定位 | "Experience-Driven Black-Box Cost Control for Coding Agents" | `README.md:1` |
| 出品 / 论文 | Microsoft Research + Microsoft Security + UC Berkeley；arXiv 2610.22732 | `README.md:4-12` |
| License | **Apache-2.0**（无 NOTICE 文件） | GitHub API `license.spdx_id`、仓库 `LICENSE` |
| 语言 / 依赖 | Python 3.10+，**纯标准库**，无第三方依赖 | `docs/ARCHITECTURE.md:5`、`pyproject.toml` |
| 规模 | 248 个 git 文件 / 12MB；core+plugin 17,582 行 Python，experiments 另 18,709 行；39 个测试文件 | `git ls-files \| wc -l`、`wc -l` |
| 热度 / 活跃度 | ★ 5,804 / fork 367 / open issues 29；创建 2026-10-13，最近推送 2026-11-12，HEAD 是 #159 合并 | GitHub API |
| 成熟度 | 建仓仅 1 个月，但 10 个 host 逐版本实测、文档极详；6 个 host 标 experimental | `docs/PLUGIN.md:100-722` |
| 支持的 host | claude / codex / copilot / pi / opencode / cursor / hermes / antigravity / grok / droid（10 个） | `tokeneconomizer/plugin/hosts.py:307-1244` 类名 |
| 状态目录 | `~/.tokeneconomizer`（0700 / 0600），按仓库 `origin` 分库 | `docs/PLUGIN.md:725-727`、`paths.py:23` |
| 三个机制默认 | `microskills=off`、`toolprune=auto`、`loopguard=nudge` | `plugin/config.py:164-203`（实测 `LiveConfig()`） |

> 与本机 6 路主 agent 的覆盖：pi ✓、codex ✓、opencode ✓、grok ✓（**经 claude 插件被动生效**）、codebuddy ✗、jcode ✗；dsh / qwenpaw / omp 也无适配器。

---

## 2. 架构理解（六条承重设计）

1. **机制与 host 完全解耦**：四个接口 `tokeneconomizer.trace`（读会话日志）、`ledger`+`pricing`（成本）、`DomainAdapter`（任务域）、`Stage`（`before_task`/`build_tools`/`run_kwargs`/`after_task`）；新增 agent 只需加一个 trace reader（`docs/ARCHITECTURE.md:3-21`）。`tests/test_plugin_package.py` 用测试钉住「机制不 import plugin、不 import experiments」。
2. **黑盒，不改 agent**：只走 agent 已经在读的通道 —— `SessionStart` 的 `additionalContext` 注入事实、项目本地 settings 的 deny list 摘工具、`PostToolUse` 的 `additionalContext` 给 redirect（`docs/PLUGIN.md:89-91`）。不改模型、不改权重、不新增工具。
3. **hook 全 fail-open、hook 内不等模型**：6 个 hook（SessionStart / UserPromptSubmit / PostToolUse / PostToolUseFailure / Stop / SessionEnd，`plugin/hooks/hooks.json:1-71`），任何异常 → 空输出 + 写 `errors.log`；重活（`te fold`、`te judge`）拆成 detached job，hook 立即返回（`docs/PLUGIN.md:71-75`）。Pi 无 hook 协议，由 `plugin/pi/tokeneconomizer.ts:10-19` 把 5 个 extension 事件翻译成同一个 dispatcher。
4. **单源计价**：`ledger` 与 `pricing` 是成本的唯一定义，插件的 session ledger 与 experiments 的聚合读同一模块，「recorded 与 reported cost 不能漂移」（`docs/ARCHITECTURE.md:10-13`）。`effective_tokens()` 按 **billed model id** 把 cache_read / cache_write / output 折算成 fresh-input 等价量（output 5~8×，cache_read 0.08~0.10×），未知 id **硬失败而非静默误计价**（`pricing.py:1-36,147-172`）。
5. **三机制都是「证据门槛 + 可逆」**：
   - micro-skills：3 个会话确认才注入，后续会话矛盾即退役，免费事实层上限 4 条 / 150 tokens（`plugin/config.py:166-168`）；
   - toolprune：工具 10 个 prompt × ≥2 会话、或 5 个会话未用才提议；`toolprune_quiet_minutes=60` 静默窗后才写 deny list；`propose` 模式只提议不写；`restore` 精确还回自己加的行（`plugin/README.md:769-777`）；
   - loopguard：5 个「正常结束」的 turn 学到 envelope 才生效，judge 置信度 ≥0.7 才 redirect，每 turn 最多 1 次，**永不自动停 turn**，只给一行「怎么停」的提示（`README.md:373-381`）。
6. **状态与隐私**：状态全本地；只有 judge / redirect / proposer 的模型调用外发，发的是 **digest**（命令模板、编辑文件名、掩码后的错误行，不含文件内容、不含用户 prompt，`docs/PLUGIN.md:780-790`）。开关：`TE_DISABLE`、`TE_SHADOW`、`TE_*_PAID_MODEL`、`TE_MODE`（`plugin/config.py:305-325`）。`shadow=True` 时「每个决策都算并记日志，但一个都不落地」（`plugin/config.py:26`）—— 这是本机试点最有用的开关。

---

## 3. 本机实测（隔离、可复现，见 §8）

环境：Python 3.11.2（≥3.10 ✓）；hub `0.13.200`、`term_sessions=2`、`code_stale=false`；CCR `/v1/models` = **0 个模型**；`~/.tokeneconomizer` 不存在（未安装）；全程未重启 hub、未改 CCR、未写任何共享配置。

### 3.1 计价口径：本机模型 ID 全部硬失败（决定性证据）

| 输入 billed model id | 结果 |
|---|---|
| `openrouter/nvidia/nemotron-3.5-super-vl:free`（codex 默认） | **FAIL** `ValueError: no weight entry for billed model 'nemotron-3.5-super-vl:free'` |
| `openrouter/stealth/crouton-alpha`（claude 默认） | **FAIL** `... 'crouton-alpha'` |
| `deepseek-flash`（hub 判定后端） | **FAIL** `... 'deepseek-flash'` |
| `openrouter/free`（非会话默认） | **FAIL** `... 'free'` |
| `claude-haiku-4-5`（TE 表内） | OK，output ×5.0 |
| 本机形状的 `effective_tokens()` 调用 | **FAIL** 同上 |
| 不带 `model` 字段的兜底行 | OK（claude 4,650 / codex 5,150） |

`resolve_model()` 会剥掉网关前缀（`pricing.py:128-143`），但剥完仍是本机自有的 `:free` / stealth / deepseek id，`MODEL_WEIGHTS`（`pricing.py:53-95`，24 个商业模型快照 2026-07-27）里没有 ⇒ `weights_for()` 抛异常。**结论：TE 的 ledger 在本机开箱不可用，必须扩表或改口径。**

### 3.2 隔离 CLI 与 shadow 开关可用

```
TOKENECONOMIZER_HOME=/vol1/work/te-eval-home TE_SHADOW=1 sh plugin/scripts/te --host claude status
→ repository /vol1/work/TokenEconomizer-eval, state .../repos/tokeneconomizer-e8534d9a38/claude
→ config microskills=off toolprune=auto loopguard=nudge repomap=off mode=auto shadow=True
→ sessions 0 folded, 0 effective tokens, plugin overhead 0
```
纯标准库核心在本机可跑、隔离 HOME 生效、`shadow` 被 status 如实回显。隔离目录只生成 `index.json` / `.index.lock`。

### 3.3 一个本机专属坑：浅克隆落盘执行位被抹成 000

`git ls-files -s` 记录 `plugin/scripts/te`、`te-hook` 为 **100755**，但浅克隆落盘 `stat` 显示 **000**，`./plugin/scripts/te` 直接 `Permission denied`；改用 `sh plugin/scripts/te` 正常。工作区既有脚本是 755/771，所以不是文件系统普遍不支持，而是本次克隆创建的权限问题。**由于 hook 全 fail-open，这个坑不会报错，只会让插件静默完全不生效** —— 装机前必须 `chmod +x plugin/scripts/{te,te-hook}`。

### 3.4 本机配置面（只读取证）

| 项 | 现值 | 含义 |
|---|---|---|
| `~/.claude/settings.json` | `model=openrouter/stealth/crouton-alpha`，`hooks` 仅 `UserPromptSubmit:1` | TE 装成 Claude 插件后 hook 不写 settings.json，但 **grok 会复用 `~/.claude/plugins`** ⇒ 一次安装波及两家 |
| `~/.codex/config.toml:2-5` | `model_provider=claude-code-router`、`model=openrouter/nvidia/...:free`、`base_url=127.0.0.1:3456/v1` | 旁路调用走 CCR |
| `~/.pi/agent/settings.json:3` | `defaultProvider=ccr-free` | 同上 |
| `~/.pi/agent/extensions/hub-facade.ts` | input 钩子注入 hub 记忆 + top-3 技能（会话内去重 TTL 2h） | TE 的 pi 扩展**每个 prompt 都重设** `<tokeneconomizer>` 系统段（`docs/PLUGIN.md:303-305`），两路注入叠加 |

---

## 4. 「能不能融合」逐项裁定

| 路径 | 能否 | 依据 | 裁定 |
|---|---|---|---|
| 把代码合并进 agenthub | **否** | 17.6k 行 core + 18.7k 行 benchmark harness，层不同（插件 vs FastAPI 面板）；`AGENTS.md` 白名单制 | 不合并 |
| 直接装到全部 6 路 agent | **否** | 仅 4 路覆盖，且 codebuddy/jcode 无适配器；grok 还会被 claude 插件**被动**带上 | 不全装 |
| 用它的 ledger 做本机计量 | **否** | §3.1 实测 4/4 硬失败 | 不用 |
| `toolprune=auto` 自动写 settings | **否** | 默认 auto 会写项目本地 `.claude/settings.local.json` / `.codex/config.toml` / `.github/copilot/settings.local.json`（`plugin/README.md:79-81`），与共享配置军规「先读现值→备份→才许写」「白名单制」冲突 | 只许 `propose` 或 `shadow` |
| `microskills=on` 自动注入事实 | **否** | 本机 `AGENTS.md` 是唯一权威正文、14 份副本由 `sync-adhd-rules.sh` 同步；再来一个自动事实源＝第二真理源 | 不许开 |
| `loopguard` 自动 redirect | **可借鉴，不可直接装** | 本机已有确定性规则「连续三轮还是坏的 ⇒ 停迭代、只问一个诊断问题」；TE 的 judge 是概率判据（0.7），且本机「开发 shell 的绿是假绿」要求生产同款 A/B 复验 | 先借原则 |
| 借四件设计（§5） | **是** | 与本机既有 `usage_ledger` / `on-demand-execution` / 探活纪律同向且互补 | **P0/P1** |
| `TE_SHADOW=1` 影子试点 | **是** | §3.2 实测可跑；只算不落地、独立 HOME、不动共享配置 | 推荐 |
| 许可证 | **无障碍** | Apache-2.0 非 copyleft，无 NOTICE；引入须按 `AGENTS.md` §3.4 记来源/版本/理由并保留 LICENSE | 通过 |

---

## 5. 建议借鉴清单（按性价比）

| 优先级 | 能力 | 本机现状 | 抄什么 | 工作量 |
|---|---|---|---|---|
| **P0** | 单源计价的**机械断言** | 已有 70%：`agenthub/src/usage_ledger.py`（只读 CCR 账本、`null_cost` 显式、fail-closed）、`ledgernorm.py`（网关前缀归一）、`modelcalls.py`；但栽过「两套并行实现接了空的那套」（`modelcalls.py:1-11`） | 加一条断言：**记录侧与报告侧必须读同一函数**，像 `tests/test_plugin_package.py` 钉边界那样钉住；不做「记录一张表、报表另一张表」 | 0.5 天 |
| **P0** | 改 prompt / 工具清单前等 cache 空闲 | `skills/on-demand-execution/SKILL.md:67` 已实测「99.6% 输入是 cacheRead，砍它等于砍本来不要钱的部分」；但没有**变更窗口**概念 | 只抄设计不抄代码：任何会改系统 prompt / 工具清单的动作，须等到该会话 cache 已过期（TE 用 60 分钟静默窗，`docs/PLUGIN.md:438-460`），否则「恢复的会话会把整段历史重写进 cache」，省了 tokens 反而更贵 | 1 天（规则/派发层） |
| **P1** | 干预三原则 | 已有「探活最多 2 次即结束」「连续三轮停迭代」「失败即止损」，但散在各处 | 收成一条可断言的不变量：**证据门槛才动作 / 每步可逆 / 永不自动停 turn 只给人提示**（`docs/PLUGIN.md:14-22,89-98`）；写进 `AGENTS.md` 的施工纪律段 | 1 天（规则） |
| **P1** | shadow 只算不落地 | hub 有 `--dry-run` 类能力但无统一影子开关 | 派发 / 判定新增 `SHADOW=1`：决策全算、落日志、`events.jsonl` 记一条，但不写配置、不发 redirect、不停 turn | 1–2 天 |
| **P2** | fact 生命周期 | `intake` 四查一简报、repo-memory、wiki/agent-knowledge、MEMORY.md 都是**人工/提炼**、全局 | 3 次确认→生效、矛盾→退役、4 条 / 150 tokens 硬上限；**须先裁「本机是否允许第二事实源」**，否则与 `AGENTS.md` 唯一权威正文冲突 | 2 天，待裁 |

合计 **约 5–6 人天**（P0+P1 3.5 天），与 t3code 评估同量级，全部可增量落地。

**不建议抄**：`MODEL_WEIGHTS` 价格表（本机 ID 全不在表内）、detached judge 的模型旁路（会额外触发付费/免费池调用）、`history_import`（全量读 `~/.claude/projects/`，隐私面大）、`experiments/`（18.7k 行 benchmark harness，与本机无关）。

---

## 6. 为什么「直接装 / 深度合并」不划算

1. **层不同**：唯一交集是「本机 CLI 进程」，用进程边界共存即可，不必共享代码。
2. **收益口径错位**：免费池 + 99.6% cacheRead，TE 的 18–31% 是**商业模型按 token 计费**口径；本机不可比，不能拿论文数字当预期收益。
3. **计价硬失败**（§3.1）：装了连自身 ledger 都记不了；扩 `MODEL_WEIGHTS` 又要维护一张随 CCR 轮换变动的表，与「CCR 12:00 轮换免费池」的既有教训冲突。
4. **爆炸半径**：装 Claude 插件 ⇒ grok 被动生效（grok 复用 claude hook 配置）；pi 侧 `hub-facade.ts` 每轮注入技能、TE pi 扩展每 prompt 重设系统段，两路叠加正撞本机「每轮多 6KB 净亏」判据（`AGENTS.md` 输出风格段）。
5. **与本机硬规则冲突**：共享配置军规（备份/禁全局替换/端到端验证）、白名单制、`term_sessions>0 严禁重启`（当前 =2）、`openrouter/auto` 全面禁用、付费项二次确认。TE 的 `auto` 写文件与无逐次确认的旁路模型调用都不满足。

---

## 7. 风险与红线

1. **许可证**：Apache-2.0 可用；引入须在 README 记来源 URL / 版本 / 理由，保留 `LICENSE`；仓库无 NOTICE 文件，无需额外归属文件。
2. **`openrouter/auto` 禁令**：TE 仓库内 **0 命中**，无违规。但 `paid_model` 若指向付费模型，judge 每 5 个工具调用可能触发一次、无逐次确认 ⇒ 试点必须把 `TE_PAID_MODEL` 指向已实测「输出可解析」的免费兜底模型，并按军规实打一次 `/v1/models` 验 200。免费路由器返 `"User Safety: safe"` 这类标签会让 judge 判词解析失败，是实测过的坑。
3. **网关降级**：CCR `/v1/models` = 0；**不得为试点改 CCR**（共享生命线）。
4. **执行位**：§3.3，装机前 `chmod +x plugin/scripts/{te,te-hook}`，否则 fail-open 静默不生效。
5. **隐私**：proposer 读最近 8 个会话 digest，`history_import=on` 全量读 `~/.claude/projects/`；本机 `code-security-scan` 对内网 IP / 手机号 / 明文口令有红线，digest 虽掩码路径数字，仍须先审一遍再开。
6. **单写者**：`PENDING-TASKS.md` 今日 17:37 已被他会话写过，按 `AGENTS.md` 文件级单写者规则**未登记新 PT**；裁定后由集成者补 `PT-20261112-02`（取号依据：现有最大 `PT-20261112-01`）。
7. **不重启 agenthub**：`term_sessions=2`，本次评估零重启、零配置写入。

---

## 8. 复现命令

```bash
# 1) 浅克隆（勿装进 ~/.claude/plugins）
git clone --depth 1 https://github.com/microsoft/TokenEconomizer.git /vol1/work/TokenEconomizer-eval
cd /vol1/work/TokenEconomizer-eval
chmod +x plugin/scripts/te plugin/scripts/te-hook      # 见 §3.3

# 2) 隔离计价实测（纯标准库，不联网、不碰 CCR）
TOKENECONOMIZER_HOME=/vol1/work/te-eval-home python3 - <<'PY'
import sys; sys.path.insert(0,'.')
from tokeneconomizer import pricing
ids=[("openrouter/nvidia/nemotron-3.5-super-vl:free","codex"),
     ("openrouter/stealth/crouton-alpha","claude"),
     ("deepseek-flash","claude"),("openrouter/free","pi"),
     ("claude-haiku-4-5","claude")]
for m,a in ids:
    try: print("OK  ",m,pricing.weights_for({"model":m,"agent":a})["output"])
    except Exception as e: print("FAIL",m,type(e).__name__,str(e)[:90])
PY

# 3) 影子状态（只算不落地，独立 HOME）
TOKENECONOMIZER_HOME=/vol1/work/te-eval-home TE_SHADOW=1 ./plugin/scripts/te --host claude status

# 4) 网关现状（只读，不改）
curl -sS --noproxy '*' http://127.0.0.1:3456/v1/models | python3 -c "import sys,json;print(len(json.load(sys.stdin).get('data',[])))"
curl -sS --noproxy '*' http://127.0.0.1:3102/health | python3 -c "import sys,json;d=json.load(sys.stdin);print(d.get('version'),d.get('term_sessions'))"
```

清理：`rm -rf /vol1/work/te-eval-home /vol1/work/TokenEconomizer-eval`（12MB，无系统级残留）。

---

## 9. 未测清单（如实标注）

1. 真实装上插件后在一个 agent 会话里的端到端行为（未装，避免写 `~/.claude/plugins` 与 pi 扩展）；
2. judge / proposer 的真实模型调用（CCR 当前 0 模型，按军规未动网关）；
3. 10 个 host 的版本兼容性（只核了本机 9 个 agent 命令有路径、`grok` 为 shell 别名，未逐个比对 TE 验证过的版本号）；
4. 论文 18.4–30.9% 降本未在本机复现（口径不同，不可比，也不该比）；
5. `toolprune` 真实写 deny list 后对 prompt cache 的影响（只做了 `shadow` 状态查询，未 apply）；
6. `history_import` 对本机 `~/.claude/projects/` 的实际读取量（隐私面，未开）。

---

## 附：接单四段计数

记忆 **5** 条（4 条列出，均为历次融合评估判例）｜知识库 **5** 条（`已有融合评估案例参考` / `Omnigent-功能融合评估` / `BitNet-融合评估结论` / `Botboy-派发融合评估` / `开源项目融合评估框架`；前 2 条已按 §0 回读原文）｜技能 **未命中**（local_only 最高归一化分 0.196 < 0.40；已换 3 组词重查仍 0）｜参考库 **0**（未查，`deep=off` 未命中触发词，单次 20~90s 且吃 GitHub 额度；本次仓库基本面改由 GitHub API + 源码直读取证，非参考库卡片）。

*本报告只读评估：未修改 agenthub 任何代码/配置/服务端状态，未安装插件，未重启生产单元，未触碰 CCR。*
