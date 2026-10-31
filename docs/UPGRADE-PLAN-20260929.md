# Agent Hub 全面升级优化方案（2026-09-29）

> **状态**：已部分执行。截至 2026-10-31，P0 批与 P1/P2-A 多数已落地（见 §9 与正文「复核」标注）；
> 原始方案叙述原样保留作决策留痕。
> **取证基线**：commit `45c780e`（v0.13.57）· 服务端口 3102 · 取证时间 2026-09-29 15:00–16:20
> **方法**：4 路只读代理分头审计（后端 38 模块 / 前端 11 分片——2026-10-31 复核现为 13 分片 / 终端+测试 / 产品体验），
> 主会话对每一条 P0 逐条实弹复核 —— **证伪 2 条、降级 3 条、确认 6 条**（见 §2 复核台账）。
> 代理原始结论不直接采信；本文每条都带 `文件:行号`，且标注复核结论。
> **复核（2026-10-31，HEAD `7f2cb53` / 复核基线 `52cbd5a`）**：事实与数字以
> `docs/UPGRADE-PLAN-REVIEW-20261031.md` §1.2 的实测值为准，本文涉及处已就地标注，下文未标注的
> 数字仍是 2026-09-29 的历史基线（保留，不改写）。
> 该复核的 §1.3 硬错 / §1.4 捏造引用 / §2.3 已完成三张表针对的是 `AgentHub全面升级方案.docx`，
> **本文无对应断言**（逐条对照见 `agenthub/work/dispatch/agenthub-upgrade-20261031/D1-report.md`）；两条竞品引用已核实不存在（2026-10-31 HTTP 404）：
> Karpathy AgentHub、Hermes Web UI —— 本文自始未收录，无需删除。

---

## 0. 一页纸结论

**这个项目的终端与探活已是生产级，导航与告警仍是草稿态。**

- **强项**（不要在升级中破坏）：终端 coalescer 实测 2602→7 帧、尺寸所有权 claim/update、
  写端点统一闸门 fail-closed、`?v=` 内容哈希缓存闭环（`md5(hub.js)` 与提手逐字节相等）、
  vitals 三态裁决（present/roundtrip/block）、端侧自检面板。
- **病灶**（本次要修的）：① 终端四个 P0（心跳续命 TTL / 子进程退出不回收 / EAGAIN 杀连接 /
  token 进日志）；② 事件循环被同步 IO 阻塞（资源页、cloudcli）；③ kill 侧 MainThread 误杀；
  ④ 徽章死代码 + `code_stale` 不上屏 + 三套「可用/异常」口径打架；⑤ 资源页 CSS 变量未定义、
  无窄屏适配；⑥ 绑 IPv4 only 违反工作区军规；⑦ xterm 5.5.0 落后一代且 canvas addon 将被移除
  （2026-10-31 复核：已升 6.0.0，此项闭合）。

**建议节奏**：P0 止血 → P1 修复 → P2 升级（xterm 6 / 跨 Agent 活动指示）→ P3 架构债。
每批独立可回滚，`git revert` 即退。

---

## 1. 取证基线（可复现）

```
服务        agent-hub.service active running，FastAPI :3102
commit      45c780e (v0.13.57)，领先 origin/master 8 个提交（未推送）
工作树      主 checkout 干净（v0.13.57 已由他人提交）
测试        L0 hermetic 785 例全绿 0 skip / L1 host 44 例 2 红
            （2026-10-31 复核：L0 3439 例、0 skip / 0 fail，见 REVIEW §1.2）
依赖        fastapi 0.141.1 · uvicorn 0.52.4 · pydantic 2.13.5 · aiohttp 3.14.3
            xterm.js 5.5.0（2024-04-05；2026-10-31 复核已升 6.0.0）· Python 3.11.2 · 无 uvloop/orjson/httpx
前端产物    md5(static/hub.js) == templates/index.html 里的 ?v=4004ac26（同步 ✅）
```

**并发状态**：`orchestration-check.sh` 判 C10 FAIL（主 checkout 未提交改动，**现已被他人提交消除**）、
C6 判「agent-hub 仓 ahead=8 未推送」。**方案执行必须在 worktree 进行**
（`bash scripts/worktree-new.sh /home/gztxt/agent-hub <会话短ID>`），
且**执行前先 push 现有 8 个提交**（未推送堆积 = 一次故障即永久丢失，有 v0.13.16/17 前科）。

---

## 2. 代理 P0 复核台账（本会话亲验，代理结论不盲信）

| # | 代理指控 | 复核方法 | 结论 |
|---|---|---|---|
| 1 | `db.py` 无 `check_same_thread=False`，并发读抛 ProgrammingError | 读 `src/db.py:139` | ❌ **证伪** —— `sqlite3.connect(str(path), check_same_thread=False)` 已写死，且有 `_lock` |
| 2 | 资源页 kill 会误杀非 CCR 的 MainThread 进程 | 读 `src/resources.py:170-172` vs `:314-320` | ✅ **确认 P0** —— 采集侧有 `.ccr`/cmdline 佐证，kill 侧**没有**，`post` 匹配 `MainThread` 即杀 |
| 3 | 心跳每 15s 续 `last_io`，45min TTL 对在线会话永久失效 | 读 `src/term.py:648` + `static/hub/02-nav-and-poll.js:207` | ✅ **确认 P0** —— 每收到任何帧（含 hb）都 `sess.last_io = time.time()` |
| 4 | 子进程自行退出、无人读 fd ⇒ 永不被 `_reap` 回收 | 读 `src/term.py:512`（`alive=False` 只在 EIO 分支）+ `:456` | ✅ **确认 P0** —— Linux pty master 无未读数据不触发 readable，`_reap` 第一分支进不去 |
| 5 | 非阻塞 pty `os.write` 遇 EAGAIN 直接 `break` 杀连接 | 读 `src/term.py:678` + `issubclass(BlockingIOError, OSError)==True` | ✅ **确认 P0** —— `except OSError: break` 把 EAGAIN 当致命错 |
| 6 | 资源页 XSS（agentId 未转义进 `data-agent` 与 inline onclick） | 读 `src/main.py:474` `agent_id = re.sub(r"[^a-z0-9_-]", "-", ...)` | ⚠️ **降级 P2** —— 注册端点已净化 id 字符集，**当前不可利用**；但前端仍应补转义（纵深） |
| 7 | `--panel-bg/--card-bg/--primary` 三个 CSS 变量未定义 ⇒ 资源页视觉破损 | `grep -c -- "--panel-bg:" templates/index.html` → 0/0/0 | ✅ **确认 P1**（代理列 P0，实为视觉缺陷非安全） |
| 8 | 侧栏行内动作按钮是死 UI（任何状态都 `display:none`） | 读 `templates/index.html:269` + `:325` | ✅ **确认 P1** —— 展开 `display:none`、折叠也 `display:none`，三个 act-btn 不可达 |
| 9 | 徽章 `setBadge` 是死代码（模板 0 个 badge 元素） | `grep -c "badge-" templates/index.html` → 0 | ✅ **确认 P1** |
| 10 | `code_stale` 前端零上屏 | `rg "code_stale\|needs_restart" static/hub/` → 0 命中 | ✅ **确认 P1** —— `/health` 已算出但智管中心自己跑旧代码从不提示 |
| 11 | TERM_TOKEN 明文进 journald | `journalctl -u agent-hub` 查 3 天 | ⚠️ **降级 P2** —— uvicorn `log_level=info` 但 journald 无 access log 行（0 命中）；仍属隐患（换代理/开 access log 即泄漏） |
| 12 | 绑 `0.0.0.0` 违反「HTTP 服务必须绑 `::`」军规 | `ss -ltn` → `LISTEN 0.0.0.0:3102` | ✅ **确认 P1** —— 军规明确，外网/Tailscale 双栈访问受限 |

---

## 3. P0 — 止血批（6 条，建议第一批执行）

> 判据：会造成数据损坏、进程误杀、连接被杀、资源永不回收、密钥泄漏。

### P0-1 终端：心跳把 TTL 续命，45min 回收对在线会话永久失效
- **位置**：`src/term.py:648`（`sess.last_io = time.time()` 在 `while True: msg = await ws.receive()` 之后**无条件**执行）
- **同款**：`src/term.py:462` `_reap` 用 `time.time() - s.last_io > IDLE_TTL_S`
- **根因**：心跳帧 `{"type":"hb"}` 与用户输入走**同一条** `last_io`。前端每 15s 发 hb（`static/hub/02-nav-and-poll.js:207` `TERM_HB_SEND_MS=15000`）⇒ 开着的终端 `last_io` 永远新鲜。
- **影响**：`TERM_IDLE_TTL` 只对已断连会话有效；`MAX_SESSIONS=8` 靠断链释放；`/health` 的 `term_idle_max_s` 恒近 0（该字段本为自证 TTL 在跑）。
- **修法**：心跳帧**不更新** `last_io`（只更新 `last_hb`）；`last_io` 只在**收到非 hb 帧**或 **PTY 有输出**时更新。TTL 语义回归「无人工输入 45min」。
- **验收**：置 `last_io = now - TTL - 5` → 发一帧 hb → `_reap()` 应仍回收；发一帧真实输入 → 不回收。新增单测 `test_term_idle_ttl.py`。
- **回滚**：`git revert <commit>`。

### P0-2 终端：子进程自行退出、无人读 fd ⇒ 会话永不被回收
- **位置**：`src/term.py:512`（`sess.alive = False` 只在 `on_readable` 的 EIO 分支）、`src/term.py:456`（`_reap`）
- **根因**：`alive=False` 唯一路径是读 fd 撞 EIO。agent 自己 `exit` 后 pty master 无未读数据 ⇒ 不再触发 readable ⇒ `alive` 永不变 False ⇒ `_reap` 第一分支 `if not s.alive` 进不去，第二分支 TTL 又被 P0-1 续命 ⇒ **双重失灵**。
- **影响**：会话条目永久占 `_sessions` 配额；反复开终端最终 429「开不出来」。v0.13.53 修的是「kill 时目标已不存在」，没修这条。
- **修法**：`_reap` 增加**独立存活探测**——对超过 N 秒无输出的会话用 `os.waitpid(pid, WNOHANG)` 探一次（pid 是本进程子进程，可探），探到已退出即 `alive=False` + `_cleanup()`。与 EIO 路径共用 `_cleanup` 幂等守卫。
- **验收**：起一个 `sh -c 'sleep 1; exit'` 会话，不开浏览器 WS，90s 内 `_sessions` 应摘除该 sid。新增单测。
- **回滚**：`git revert`。

### P0-3 终端：非阻塞 pty `os.write` 遇 EAGAIN 直接杀连接
- **位置**：`src/term.py:678` `try: os.write(sess.fd, data) except OSError: break`
- **根因**：`BlockingIOError` 是 `OSError` 子类；pty 缓冲瞬时满（粘贴大段文本 / `cat` 大文件）时 errno=11 落进 `break` ⇒ 整条 WS 断开进退避重连。
- **影响**：输入被截断且表现为「终端自己老掉线」，极难归因。
- **修法**：`except BlockingIOError` 单独捕获 → 短暂 `await asyncio.sleep(0.01)` 重试（带上限，如 3 次 / 50ms），仍失败才记账并**不杀连接**（丢弃该输入块 + 计入 `sess.dropped`，前端已有丢弃提示机制）。其余 `OSError` 才 `break`。
- **验收**：向会话粘贴 200KB 文本，WS 不应断开、输入完整回显。新增单测（mock `os.write` 抛 EAGAIN）。
- **回滚**：`git revert`。

### P0-4 资源页 kill 误杀非 CCR 的 MainThread 进程
- **位置**：`src/resources.py:314-320`（kill 侧正则匹配）vs `:170-172`（采集侧有 `.ccr`/`claude-code-router` cmdline 佐证）
- **根因**：画像 ccr 的 `detect.proc` 含 `"MainThread"`（`src/profiles.py:152`）。采集侧对 `rx == "MainThread"` 额外校验 cmdline，kill 侧**漏了这道**。
- **影响**：`POST /api/resources/kill` 命中任一 MainThread 进程即 SIGTERM/SIGKILL——**WorkBuddy 桌面端（Electron）、jcode 等凡 comm=MainThread 的都在射程内**。这是会真实损坏用户其他应用的 bug。
- **修法**：把采集侧的 MainThread 佐证逻辑**抽成共享函数** `match_proc(proc, proc_patterns) -> bool`，采集与 kill 同源调用（消除双真相源）。
- **验收**：单测——给一个 `comm=MainThread, cmdline=/opt/apps/workbuddy/...` 的假进程，kill 侧应判**不匹配**。真机不杀任何进程验证。
- **回滚**：`git revert`。

### P0-5 事件循环被同步 IO 阻塞（资源页 + cloudcli）
- **位置**：`src/resources.py:99`（`list_resources` async 内直调 `_collect_agent_resources()`，内含 `time.sleep(0.15)` + N 次 `subprocess.run(timeout=5)`）、`src/cloudcli.py:175-190`（async 内 `urllib.request.urlopen(timeout=10)`）
- **影响**：单次资源页请求可阻塞 event loop 8s×N，**所有 WS 终端帧停摆**（与仓库已确立的「refresh 禁入请求路径」同型，此处破例）。
- **修法**：`await asyncio.to_thread(_collect_agent_resources)`；cloudcli 换 `aiohttp` 或 `to_thread` 包裹。
- **验收**：单测/探针——并发跑 `/api/resources` 与一个活跃终端，终端帧延迟不随资源页请求抖动。
- **回滚**：`git revert`。

### P0-6 限流表无界增长（内存泄漏）
- **位置**：`src/main.py:178,190-197`（`_api_rate: Dict[str, deque]`，只在**该 IP 自己再次请求**时 popleft，从不 `del`）
- **影响**：LAN/Tailscale 下源 IP 轮换（XFF 场景）⇒ 字典单调增长。`src/mcpgw.py:70` 同型。
- **修法**：加**周期清扫**（复用 reap 循环，每 60s 删掉窗口早于 1 分钟的空 deque）；或改用有界 LRU。
- **验收**：单测——塞 1 万个过期 IP 键，跑一次清扫后字典回落到活跃数。
- **回滚**：`git revert`。

---

## 4. P1 — 正确性与界面修复批（建议第二批）

### 4.1 后端

| # | 问题 | 位置 | 修法要点 |
|---|---|---|---|
| P1-1 | `vitals._save` 固定 `.json.tmp`，并发 sweep/verify 互相覆盖丢状态 | `src/vitals.py:489` | tmp 加 pid+线程后缀，或走 `term` 同款「锁内 tmp→os.replace」 |
| P1-2 | `cronjobs._tick_loop` 串行 `await _fire`，单 job 超时 120s 拖死全部 | `src/cronjobs.py:181-197` | 改 `asyncio.gather(..., return_exceptions=True)` 并发；`except` 补日志 |
| P1-3 | `db.execute_script` 不 commit，DDL 依赖隐式事务 | `src/db.py:210-212` | 补 `commit()`（2026-09-30 已修：`src/db.py:331-336`，`523172c`） |
| P1-4 | `mcpgw.add_acl` `db.query(...)[0]` 无空列表保护 ⇒ IndexError 500 | `src/mcpgw.py:238-240` | 空列表判 404 |
| P1-5 | `main.py` 5 处 `create_task` 无引用 + shutdown 不 cancel | `src/main.py:969-974` | 存进模块级集合，shutdown 统一 cancel |
| P1-6 | `term._norm_intent` 文档与实现相反（未知值 doc 说 update，实现被拒） | `src/term.py:296` vs `:312` | 对齐文档或实现；补未知值单测 |
| P1-7 | EIO 分支 `except QueueFull: pass` 吞掉后续观看者的结束通知 | `src/term.py:526` | 逐队列独立 try，别让首个满队列吞掉其余 |
| P1-8 | `_size_owner` 在 `kill_session` 后残留（WS 未断时） | `src/term.py:449` | kill 时连带清 `_size_owner[sid]` |
| P1-9 | `memory._local_l1` LIKE 未转义 `%`/`_` | `src/memory.py:51-55` | `ESCAPE` 或换 FTS/LIKE 转义 |
| P1-10 | `resources.py` 正则每次 `__import__("re").compile(rx)` 无缓存 | `src/resources.py:170` | 提到模块级 `re.compile` 或 `lru_cache` |
| P1-11 | CORS 默认 origins 硬编码内网 IP，换网静默失效 | `src/main.py:143-146` | 改 env 驱动 + 默认 `*`（配 credentials 时禁止） |

### 4.2 绑定与军规

| # | 问题 | 位置 | 修法要点 |
|---|---|---|---|
| P1-12 | **绑 `0.0.0.0`（IPv4 only）违反「HTTP 服务必须绑 `::`」军规** | `.env:3` `HOST=0.0.0.0`；`ss` 实测 `LISTEN 0.0.0.0:3102` | 改 `HOST=::`（双栈）。**属 `.env` 受保护面**（军规「共享配置变更」），改前时间戳备份 + 改后端到端验证 |
| P1-13 | `run_tier.py` 缺 shebang/执行位，文档口径与 `run_tests.sh` 不一致 | `scripts/run_tier.py` | 补 shebang + 统一文档口径 |

### 4.3 前端 / 界面（红线与视觉）

| # | 问题 | 位置 | 修法要点 |
|---|---|---|---|
| P1-14 | 资源页用 3 个**未定义** CSS 变量 ⇒ 底色透明/边框消失；`.btn.xs` 类不存在 | `static/hub/11-resources.js:57-67,95` | 改用 `:root` 已有 token（`--card`/`--panel`/`--primary`→实名核对）；补 `.btn.xs` 或改用 `.btn.sm` |
| P1-15 | 资源页硬编码 `#6b7280`/`#8b5cf6` 绕过 `:root` 唯一色板 | `static/hub/11-resources.js:58,60,61` | 收敛到语义 token |
| P1-16 | 资源页**无任何窄屏适配**，390px 必横向溢出 | `static/hub/11-resources.js:93`（表格 `max-width:400px` 写死） | 加 `@media(max-width:767px)`（与 `01:6` 同源断点）|
| P1-17 | 侧栏三个 act-btn 任何状态都 `display:none`（死 UI），且用 inline onclick 违反红线 2 | `templates/index.html:269,325`；`static/hub/05-chat-and-history.js:211-218` | 改 `data-*` 走 `#sidebar` 委托（该 root 已有委托）；修 CSS 让按钮在合适档位可见 |
| P1-18 | 徽章 `setBadge` 死代码（模板 0 个 badge 元素） | `static/hub/06-manager-tasks.js:209-227` | 补徽章 DOM 或删死代码 |
| P1-19 | `code_stale`/`needs_restart` 已算出但前端零上屏 | `/health` 有字段；`static/hub/` 零命中 | 顶栏加「需重启」提示（价值高/成本极低） |
| P1-20 | 三套「可用/异常」口径打架（顶栏 `usable` 10/10 vs 总览 `attested` 6/10；异常块恒 0） | `static/hub/02-nav-and-poll.js` + cards | 统一为一条规则应用到异常块/导航排序/顶栏/卡片色 |
| P1-21 | 抽屉无 Esc 关闭、无焦点陷阱、无 `aria-modal` | `templates/index.html:1835,1844` | 加 Esc + `aria-modal` + 焦点陷阱 |
| P1-22 | `hlMatch` 用原文偏移切已转义串 ⇒ 含 `&<>"'` 时高亮错位 | `static/hub/05-chat-and-history.js:193` | 先转义再匹配，或用 `indexOf` 于转义后串 |
| P1-23 | 资源页 `killProc` 失败不复原 `disabled` ⇒ 一行永久变灰 | `static/hub/11-resources.js:121-130` | catch 里恢复按钮态 |
| P1-24 | 资源页错误提示绕过统一加载助手（`boxFail`），后端 message 直吐 | `static/hub/11-resources.js:35,39` | 走 `boxBusy/boxFail` 统一口径 |

---

## 5. P2 — 升级批（真正的新能力）

### P2-A 终端栈升级：xterm.js 5.5.0 → 6.0.0（**需谨慎，影响面大**）
- **状态（2026-10-31 复核）：已完成** —— `static/vendor/README.md:12-19` 逐文件登记 `@xterm/xterm` **6.0.0**
  + 6 个 addon（fit 0.11.0 / webgl 0.19.0 / search 0.16.0 / unicode11 0.9.0 / web-links 0.12.0 /
  clipboard 0.2.0）版本与 md5，提交 `a32c8fa`（v0.13.60）。以下为 09-29 立项时的分析，原样保留。
- **现状**：`static/vendor/xterm.js` 是 `@xterm/xterm@5.5.0`（2024-04-05），已落后一代。（2026-10-31 复核：已升 6.0.0）
- **为什么现在做**：v0.13.57 刚引入 WebGL/Canvas 渲染器，**正是 6.0.0 改进的方向**（6.0.0 修复/重做了 WebGL 图集与 CJK 处理）。而 6.0.0 **移除了 canvas addon**（本机无物理 GPU，canvas 是实际主力回落路径之一）——迁移必须同步调整回落链。
- **影响面**：xterm API 面很小（只用了 `write/clear/resize/loadAddon/refresh/paste/focus/unicode`），迁移可控；但要同步 6 个 addon 版本、补 LICENSE（vendor 无 LICENSE、addon 无版本标记）。
- **建议**：独立一批、单独 worktree、先做「vendor 换新 + 渲染器回落链调整 + 终端冒烟」三件事，不夹带其它修复。**验收**：终端起得来、CJK 不方块、webgl/canvas/dom 三档可切、Unicode11 生效。
- **可延后**：若风险预算紧，5.5.0 继续用也可用（当前 CJK 已由 v0.13.57 字体栈 + 渲染器自愈兜住），6.0 迁移属「消除已知上游缺陷」而非「修当前 bug」。
- **本机 vendor 实测复核（2026-09-29）**：
  - `static/vendor/xterm.js` = 5.5.0；addon 版本混装且无版本标记（`fit.js` 0.10.0 / `addon-clipboard.js` 3.7.7 / 其余 grep 不到版本串）⇒ **迁移时无法按版本对齐，必须整包换 + 逐个登记版本号**。（2026-10-31 复核：升级后已由 `static/vendor/README.md` 逐文件登记，此项闭合）
  - `addon-webgl.js` **已实现 `clearTextureAtlas()` + `onContextLoss`**，但项目代码（`03-agents-cards.js:294-299`）只挂了 `onContextLoss`，**`clearTextureAtlas` 一次都没调用** ⇒ 上游「显存持续增长 / atlas 页面耗尽后画面花掉」的已知问题在本项目处于**未修状态**。这是 P2-A 里**成本最低、收益最直接**的一条：迁移前后各加一次定时 `clearTextureAtlas()` 即可（建议先做，等价于 5.5.0 上的止血）。
  - `03-agents-cards.js:275-281` 的回落链是 `webgl → canvas → dom`，且已用 `termCjkUsable()` 做 CJK 前置判断（这条设计正确，保留）。6.0.0 移除 canvas addon 后回落链变 `webgl → dom`，**需同步改 `03-agents-cards.js:282` 的 `canvas` 分支**，否则白屏且无提示。
  - 迁移风险点：WebGL 的 `clearTextureAtlas` 在**无 GPU 的软件渲染环境**下是否可用未验证 ⇒ 须带 try/catch 且失败静默降级（与现有 `onContextLoss` 兜底同风格）。


### P2-B 跨 Agent 活动指示（融合计划 P2-2，此前评为高难度）
- **价值**：这是「多 Agent 统一管理中心」最核心的未兑现承诺——现在要看谁在跑必须逐个点开。
- **低成本路径**：复用 `/api/term/sessions` + vitals，给每个侧栏行加 running 忙碌点 + 顶栏在跑计数。**不依赖服务端 pyte**。
- **注**：融合计划的 P2-1（服务端 pyte 真值，解决重连回放 64KB 裸字节导致 TUI 错位）**成本高、风险大**，建议本次**不做**，登记备查。

### P2-C 测试补盲（当前 0 覆盖）
- `src/cronjobs.py`（211 行，0 引用）、`src/embed_proxy.py`（286 行，0 引用）、`src/scanner.py`（201 行，0 引用）零测试。
- L1 host 已有 2 例真红（`test_localprojects.py:434` 176>50、`test_sessions_store.py:217` grok title 空）——断言本机数据形态，无隔离夹具，随日常使用变红。
- 无 CI（无 `.github/workflows`）。分层设计反复写「干净 runner 结论必须一样」，实际从未验证。

### P2-D 其它
> **2026-10-31 复核**：本小节多数已于 `f3eaf82`（v0.13.59）闭合 —— 供应商 LICENSE/版本登记、
> CHANGELOG 补 52~58 断档、DESIGN 过期订正、README kill 口径四项均已落地；下行原文保留。
- 供应商 LICENSE 补齐（`static/vendor/` 无 LICENSE、6 个 addon 无版本标记）。
- `CHANGELOG.md` 断档：顶部停在 v0.13.51，v0.13.52~57 六个版本无记录（当日提交纪律断档）。
- `DESIGN.md` 严重过期：列已不存在的 `chat.py`/`scheduler.py`、API 表过时、称前端是单文件、**写绑 `0.0.0.0`**（与 P1-12 军规矛盾）。
- README 称端口「只读无 kill」，但 `/api/resources/kill` 存在——文档与实现不一致。
- 密钥纵深：`data-agent`/inline onclick 处补 `escapeHtml`（当前靠后端净化，不 exploitable 但应纵深）；`?token=` 不走 query（改 header/首帧 token）避免进任何代理日志。
- `_api_rate` 之外，`/api/sessions/export` 强制按 POST 判鉴权但端点是 GET，且 `?token=` 凭据落 access log/Referer。

---

## 6. P3 — 架构债（本次只登记，不动）

- **真相源三裂**：模型值同时在 `.env` / hub SQLite / 各 agent 配置文件，靠 300s 巡检对齐——这是本仓多次「静默不可用」的根因。
- **`db` 是 20+ 模块直接 import 的全局单例**，无连接池/上下文，`init_db` 重指全局连接；缺 repository 层。
- **错误处理三风格并存**：`raise HTTPException` / 返回 `{"ok":False}` / `print` 后 continue，调用方逐个记。
- **前端全局作用域**：13 分片（2026-10-31 复核；09-29 审计时为 10 个，新增 `14-botboy.js`）拼接后仍是单一全局作用域，顺序即契约（TDZ 事故、`var` 冗余的必然成本）；无单一 store。
- **真相源散落**：`.env`（`override=True` 强制覆盖）/ SQLite / 模块硬编码常量（`memfed.WORKSPACE`、`kb.TURBOVEC_CWD`）三处。
- `sessions_store` 的 `_grok_read` 假定 `[models].default` 是 profile 键，本机 grok 写成模型 ID ⇒ 当前值恒空（PT-20260928-01 遗留）。

---

## 7. 执行编排（授权后）

**前置（不可省）**：
1. `cd /home/gztxt/agent-hub && git push` —— 先推现有 8 个提交（未推送堆积=永久丢失，有 v0.13.16/17 前科）。
2. `bash scripts/worktree-new.sh /home/gztxt/agent-hub <会话短ID>` —— 施工进 worktree，主 checkout 只给集成者。

**批次**（每批独立可回滚，`bash scripts/prepush.sh` 推前闸 + L0/L1 测试 + 生产实弹复验）：

| 批 | 内容 | 风险 | 预计 |
|---|---|---|---|
| **A 止血** | P0-1~6 | 低（都是修 bug） | 1 批 |
| **B 正确性+军规** | P1-1~13（含 `HOST=::`，属受保护面需单独声明） | 中（`.env` 变更 + 重启） | 1-2 批 |
| **C 界面** | P1-14~24（资源页重建、侧栏委托、徽章/重启提示、口径统一、可访问性） | 低-中 | 1-2 批 |
| **D 升级** | P2-A xterm 6 / P2-B 活动指示 / P2-C 测试补盲 | xterm 迁移中-高 | 独立 |
| **E 文档** | CHANGELOG 补 52~57、DESIGN.md 重写、README 修正 | 无 | 1 批 |

**每批收尾（军规强制）**：写 `agent-knowledge/` + 登记台账 `PT-YYYYMMDD-NN` + 当天 `git commit`。
**汇报门槛**：全绿才报「完成」；任一步失败如实报失败点 + 回滚命令。

---

## 8. 方案自检（对照工作区红线）

- ✅ **先备份再改**：P1-12（`.env`）等受保护面改动均要求时间戳备份 + 端到端验证。
- ✅ **施工进 worktree**：§7 前置第 2 条。
- ✅ **未推送先推**：§7 前置第 1 条。
- ✅ **不用 CCR/FCC 配置**：本次不碰任何共享网关配置。
- ✅ **证据驱动**：每条带 `文件:行号`；代理 P0 已逐条复核（证伪 2/降级 3/确认 6，§2）。
- ✅ **不投机设计**：P3 明确「本次只登记不动」；xterm 6 迁移标注「可延后」。
- ⚠️ **本方案文档自身也受工作区军规管辖**：它落在 agent-hub 仓内、属未跟踪文件；**授权后第一步应连同代码一起 `git add` 并当天 commit**，否则又是一份 `/tmp` 式易失产物（09-24 事故：未提交堆积 = 一次事故清零）。

---

## 9. 已上线（勿重复立项）

> 取证：2026-10-31，HEAD `7f2cb53`（复核报告基线 `52cbd5a`）。以下五项在同期《AgentHub 全面升级方案》
> 中被列为待做/缺口；本文件虽未单列，一并登记为**已上线**，不得再按新能力立项 —— 要改按缺陷修。
> 逐条证据均在本仓当前代码中，非引自报告。

| 项 | 落地证据 | 判 |
|---|---|---|
| MCP 客户端 | `src/mcpgw.py:23-34`（`ClientSession` / `stdio_client` / `streamable_http_client`）、`:186-199` 会话工厂；`requirements.txt:14` `mcp==2.1.1` | ✅ |
| Skill 共享库 / 商店 | `src/skill_store.py`（1,288 行；`install()` :166、`store_install()` :1187） | ✅ |
| 移动端触摸层 | `static/hub/13-term-touch.js:1`（惯性滚动 / 长按选区 / 双指缩放） | ✅ |
| WAL 与 checkpoint | `src/db.py:212` `PRAGMA journal_mode=WAL`；`init_db()` 内启动 `wal_checkpoint(TRUNCATE)` + 定期 checkpoint | ✅ |
| Agent 自动注册 | `src/profiles.py:784` `all_profiles()`（画像合并点，v0.13.155） | ✅ |

另：本文 P1-3（`db.execute_script` 补 commit，`src/db.py:331-336`，`523172c`/v0.13.59）与
P2-A（xterm 5.5.0→6.0.0，`static/vendor/README.md:12-19`，`a32c8fa`/v0.13.60）亦已落地。
