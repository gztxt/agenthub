function jobKindChange() {
  const k = $('jobKind').value;
  $('jobAgent').style.display = k === 'agent_prompt' ? '' : 'none';
  $('jobPayload').placeholder = k === 'shell' ? '白名单命令，如 echo hello' : k === 'agent_prompt' ? '发给 Agent 的 Prompt' : 'http://127.0.0.1:3102/health';
}
async function loadJobs() {
  try {
    const d = await api('/api/jobs');
    $('jobAllow').textContent = 'shell 白名单: ' + (d.shell_allow || []).join(', ');
    $('jobsBody').innerHTML = (d.jobs || []).map(j =>
      '<tr><td><b>' + escapeHtml(j.name) + '</b><br><span class="hint">' + j.id + '</span></td>' +
      '<td style="font-family:var(--font-mono)">' + j.cron + '</td><td>' + j.kind + '</td>' +
      '<td><button class="btn sm ' + (j.enabled ? '' : 'ghost') + '" onclick="toggleJob(\'' + j.id + '\',' + (j.enabled ? 0 : 1) + ')">' + (j.enabled ? '✔ 启用' : '‖ 停用') + '</button></td>' +
      '<td class="hint">' + (j.last_run || '').slice(5, 16).replace('T', ' ') + '</td>' +
      '<td>' + (j.last_status === 'success' ? ico('check-circle', 'xs') : j.last_status === 'fail' ? ico('alert', 'xs') : '-') + '</td>' +
      '<td style="max-width:220px"><details><summary class="hint">' + escapeHtml((j.last_result || '').slice(0, 30)) + '</summary><pre style="font-size:var(--fs-sm);white-space:pre-wrap">' + escapeHtml(j.last_result || '') + '</pre></details></td>' +
      '<td style="white-space:nowrap"><button class="btn sm ghost" onclick="runJobNow(\'' + j.id + '\')">' + ico('play', 'xs') + '立即</button> ' +
      '<button class="btn sm danger" onclick="delJob(\'' + j.id + '\')">' + ico('x') + '</button></td></tr>').join('') ||
      '<tr><td colspan="8" class="hint">暂无任务</td></tr>';
  } catch (e) { toast(e.message, 'err'); }
}
async function toggleJob(id, en) { try { await api('/api/jobs/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !!en }) }); loadJobs(); } catch (e) { toast(e.message, 'err'); } }
async function runJobNow(id) { toast('执行中…'); try { const d = await api('/api/jobs/' + id + '/run', { method: 'POST' }); toast(d.status === 'success' ? '执行成功: ' + (d.result || '').slice(0, 60) : '执行失败: ' + (d.result || ''), d.status === 'success' ? 'ok' : 'err'); loadJobs(); } catch (e) { toast(e.message, 'err'); } }
async function delJob(id) { if (!confirm('删除任务 ' + id + '？')) return; try { await api('/api/jobs/' + id, { method: 'DELETE' }); loadJobs(); } catch (e) { toast(e.message, 'err'); } }

/* ── S4 MCP 网关 ──────────────────────────────────── */

let mcpToolSel = null;

async function addServer() {
  const body = { name: $('mcName').value.trim(), transport: $('mcTransport').value };
  const argsRaw = $('mcArgs').value.trim();
  if (!body.name) return toast('名称必填', 'err');
  if (body.transport === 'stdio') {
    if (!$('mcCmd').value.trim()) return toast('stdio 需 command', 'err');
    body.command = $('mcCmd').value.trim();
    body.args = argsRaw ? argsRaw.split(/\s+/) : [];
  } else {
    if (!argsRaw) return toast('http 需 URL', 'err');
    body.url = argsRaw;
  }
  try { await api('/mcp/servers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); toast('已注册，聚合刷新中…', 'ok'); $('mcName').value = $('mcCmd').value = $('mcArgs').value = ''; loadMcp(); } catch (e) { toast(e.message, 'err'); }
}
async function probeServer() {
  const cmd = $('mcCmd').value.trim();
  if (!cmd) return toast('先填 command', 'err');
  $('mcProbe').style.display = 'block'; $('mcProbe').textContent = '连接探测…';
  try {
    const d = await api('/mcp/servers/probe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: cmd, args: $('mcArgs').value.trim() ? $('mcArgs').value.trim().split(/\s+/) : [] }) });
    $('mcProbe').textContent = JSON.stringify(d.tools, null, 1);
  } catch (e) { $('mcProbe').textContent = '探测失败: ' + e.message; }
}
async function delServer(id) {
  if (!confirm('删除 MCP server: ' + id + '？（聚合工具列表将随之刷新）')) return;
  try { await api('/mcp/servers/' + id, { method: 'DELETE' }); loadMcp(); } catch (e) { toast(e.message, 'err'); }
}
async function loadMcp() {
  bindChatDelegation();   // v0.13.131：工具行/命令项走 data-* 委托（幂等）
  try {
    const s = await api('/mcp/servers');
    $('mcServers').innerHTML = '<table><thead><tr><th>名称</th><th>传输</th><th>目标</th><th></th></tr></thead><tbody>' +
      (s.servers || []).map(x => '<tr><td><b>' + escapeHtml(x.name) + '</b></td><td>' + x.transport + '</td>' +
        /* v0.13.40：补 white-space:nowrap —— 只写 overflow/text-overflow 而没 nowrap
           时省略号不生效，长 command 会把行撑成三行（1440 截图实测）。父级 .tscroll
           已给横向滚动，兜住超长值。 */
        '<td style="font-family:var(--font-mono);font-size:var(--fs-sm);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(x.transport === 'stdio' ? (x.command || '') + ' ' + (x.args || []).join(' ') : x.url || '') + '">' + escapeHtml(x.transport === 'stdio' ? (x.command || '') + ' ' + (x.args || []).join(' ') : x.url || '') + '</td>' +
        '<td><button class="btn sm danger" onclick="delServer(\'' + x.id + '\')">' + ico('x') + '</button></td></tr>').join('') +
      '</tbody></table>';
    $('mcTools').innerHTML = '聚合工具中（stdio 会临时拉起进程）…';
    const t = await api('/mcp/tools');
    const errs = Object.entries(t.errors || {});
    $('mcTools').innerHTML = (t.tools || []).map(x =>
      // v0.13.78：① 描述改走 mdInline（与技能描述同口径）；
      //            ② **工具名 `x.name` 原先既没转义、也没过 jsStr** ——
      //               它同时进了 HTML 正文（`<b>` 内）与 `onclick` 的 JS 字面量，
      //               两个语境都不设防。工具名来自 MCP server（外部注册），
      //               本仓 `jsStr` 的注释早写明「escapeHtml 只处理 HTML 上下文，
      //               HTML 实体转义不足以让任意 id 安全进 JS 字面量」，此处却没用。
      //               现补：`escapeHtml` 管 HTML 语境、`jsStr` 管 JS 字面量语境。
      '<div class="mem-item" style="cursor:pointer" data-sk-pt-server="' + escapeHtml(x.server) + '" data-sk-pt-name="' + escapeHtml(x.name) + '" id="mt_' + escapeHtml(x.server) + '_' + escapeHtml(x.name) + '"><span class="tag agent">' + escapeHtml(x.server_name) + '</span><p><b>' + escapeHtml(x.name) + '</b> <span class="hint">' + mdInline(x.description) + '</span></p></div>').join('') ||
      '<div class="hint">无工具——注册 server 后此处聚合</div>' +
      (errs.length ? '<div class="hint" style="color:var(--danger-text)">异常 server: ' + errs.map(x => x[0] + '(' + x[1].slice(0, 40) + ')').join('; ') + '</div>' : '');
  } catch (e) { $('mcTools').innerHTML = '<span style="color:var(--danger-text)">' + e.message + '</span>'; }
}
/* v0.13.131（PT-20261019-02）：MCP 工具行与 ⌘K 命令项改 data-* + 委托。
 *
 * 原形态 `onclick="pickTool(<jsStr>…)"` / `onclick="cmdGo(<jsStr>)"`：jsStr 出双引号
 * 字面量，被双引号界定的属性提前截断 ⇒ 点击静默 SyntaxError（只有控制台可见）。
 * data-* 值走 escapeHtml，dataset 读回原值。#mcTools / #cmdList 都是静态容器，
 * innerHTML 反复重写不掉监听，故 marker 防重复挂。
 * pickTool 双参数拆两个 dataset（工具名来自外部 MCP server，两个语境都不设防过）。 */
function bindChatDelegation() {
  const tools = $('mcTools');
  if (tools && !tools.__chatDeleg) {
    tools.__chatDeleg = true;
    tools.addEventListener('click', e => {
      const t = e.target.closest('[data-sk-pt-server]');
      if (t && tools.contains(t)) pickTool(t.dataset.skPtServer || '', t.dataset.skPtName || '');
    });
  }
  const cmd = $('cmdList');
  if (cmd && !cmd.__cmdDeleg) {
    cmd.__cmdDeleg = true;
    cmd.addEventListener('click', e => {
      const c = e.target.closest('[data-sk-cmdgo]');
      if (c && cmd.contains(c)) cmdGo(c.dataset.skCmdgo || '');
    });
  }
}

function pickTool(server, tool) {
  mcpToolSel = { server: server, tool: tool };
  document.querySelectorAll('[id^=mt_]').forEach(el => el.style.background = '');
  const el = document.getElementById('mt_' + server + '_' + tool);
  if (el) el.style.background = 'var(--surface-2)';
}

/* ── T3 MCP ACL 管理（契约见 src/mcpgw.py：POST {agent_id, tool_pattern, server_id?, allow}；GET {rules:[{id,agent_id,server_id,tool_pattern,allow}]}；DELETE /mcp/acl/{id}）── */
async function loadAcl() {
  const el = $('aclList');
  if (!el) return;
  try {
    const d = await api('/mcp/acl');
    el.innerHTML = (d.rules || []).map(r =>
      '<div class="mem-item"><span class="tag ' + (r.allow ? 'fact' : 'constraint') + '">' + (r.allow ? 'allow' : 'deny') + '</span>' +
      '<p><b>' + escapeHtml(r.agent_id) + '</b> · <code>' + escapeHtml(r.tool_pattern) + '</code>' +
      (r.server_id ? ' · server=' + escapeHtml(r.server_id) : ' · 任意server') + '</p>' +
      '<button class="btn sm danger" aria-label="删除规则 ' + r.id + '" onclick="delAcl(' + r.id + ')">' + ico('x') + '</button></div>').join('') ||
      '<div class="hint">暂无规则——<b>零规则行时</b>所有 agent 默认放行（首次接入零摩擦）；'
      + '但只要存在任何一条适用规则（<b>含 <code>*</code> 通配行</b>），即进入白名单语义：未覆盖即拒，且 deny 优先。</div>';
  } catch (e) { el.innerHTML = '<span style="color:var(--danger-text)">' + escapeHtml(e.message) + '</span>'; }
}
async function addAcl() {
  const body = {
    agent_id: $('aclAgent').value.trim(),
    tool_pattern: $('aclPattern').value.trim(),
    allow: $('aclAllow').value === '1'
  };
  const sid = $('aclServer').value.trim();
  if (sid) body.server_id = sid;  // 后端按 id 或 name 解析（mcpgw.add_acl）
  if (!body.agent_id || !body.tool_pattern) return toast('agent_id 与工具模式必填', 'err');
  try {
    await api('/mcp/acl', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast('ACL 规则已添加', 'ok');
    $('aclAgent').value = $('aclPattern').value = $('aclServer').value = '';
    loadAcl();
  } catch (e) { toast(e.message, 'err'); }
}
async function delAcl(id) {
  try { await api('/mcp/acl/' + id, { method: 'DELETE' }); toast('规则已删除', 'ok'); loadAcl(); }
  catch (e) { toast(e.message, 'err'); }
}
async function callToolSel() {
  if (!mcpToolSel) return toast('先点击选择一个工具', 'err');
  let args = {};
  const raw = $('mcCallArgs').value.trim();
  if (raw) { try { args = JSON.parse(raw); } catch (e) { return toast('args 需为 JSON', 'err'); } }
  $('mcCallOut').style.display = 'block'; $('mcCallOut').textContent = '调用中…';
  try {
    const d = await api('/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ server: mcpToolSel.server, tool: mcpToolSel.tool, args: args, agent_id: 'manager' }) });
    $('mcCallOut').textContent = JSON.stringify(d, null, 1);
  } catch (e) { $('mcCallOut').textContent = '失败: ' + e.message; }
}

/* ── T8 命令面板（⌘K/Ctrl+K）：搜实体直达工作台 ── */
function openCmd() {
  bindChatDelegation();   // v0.13.131：⌘K 列表项走 data-* 委托（幂等）
  $('cmdMask').classList.add('on');
  const i = $('cmdInput');
  i.value = '';
  renderCmdList('');
  i.focus();
}
function closeCmd() { $('cmdMask').classList.remove('on'); }
function renderCmdList(q) {
  const box = $('cmdList');
  // 模糊：派发时 agent 名与 id 最常被手误（`claud`/`opencode-e`）。
  const items = AGENTS.map(a => ({ a, score: fuzzyMatch([[a.id, 3], [a.name, 2]], q) }))
    .filter(x => !q || x.score > 0)
    .sort((x, y) => (y.score - x.score) || String(x.a.name || '').localeCompare(String(y.a.name || '')))
    .slice(0, 12).map(x => x.a);
  box.innerHTML = items.map(a =>
    '<div class="cmd-item" data-sk-cmdgo="' + escapeHtml(a.id) + '"><span>' + escapeHtml(a.name) + '</span><span class="hint">' + escapeHtml(a.id) + '</span></div>').join('') ||
    '<div class="hint" style="padding:8px">无匹配实体</div>';
}
function cmdGo(id) { closeCmd(); gotoChat(id); }

/* ── 启动 ─────────────────────────────────────────── */

/* ── v0.7 左侧手风琴导航：单开模式 + 搜索 + 展开态持久化 ──
   20 个实体全部收拢进左栏（AGENTS 8 / 基础设施 12），系统功能仍走 go(page) ── */
const NAV_GROUPS = { agents: 'AGENTS', infra: '基础设施', system: '系统', settings: '设置' };
const NAV_ICONS = { agents: 'hubmark', infra: 'server', system: 'sliders', settings: 'settings' };   // 收起成图标条时仍可辨认（sprite id）
const NAV_ORDER = ['agents', 'infra', 'system', 'settings'];
const NAV_SUB_KINDS = [['gateway', '网关'], ['service', '服务'], ['tool', '工具'], ['memory', '记忆']];
const SYS_PAGES = [['ports', '端口', 'share'], ['telemetry', '遥测', 'activity'], ['memory', '记忆中心', 'database'],
                   ['skills', '技能中心', 'zap'], ['kb', '知识库', 'book'],
                   ['mcp', '工具', 'wrench'], ['jobs', '定时', 'clock'], ['tasks', '协同', 'flow'],
                   ['assets', '资产', 'layers']];   // v0.13.47：运行日志页删除，内容并入设置→日志
const MODE_LABEL = { embed: '嵌入', term: '终端', chat: '对话', detail: '详情', open: '新窗口' };
/* v0.13.43 设置子菜单：与系统页同形态（data-sys → go(page) ⇒ 正文出页、窄屏自动收侧栏），
   只是单独成组挂在「系统」之下；这三项此前是右侧抽屉里的三个 tab。 */
const SET_PAGES = [['settings-model', '模型', 'cpu'], ['settings-github', 'GitHub', 'globe'],
                   ['settings-token', '终端口令', 'terminal'], ['settings-logs', '日志', 'activity']];
/* 2026-10-25：「设置 · 记忆存储」并入「系统 · 记忆中心」（同一份本机记忆的读写入口
   不该分居两个菜单，设置组也不该为 4 条便签单占一个一级项）。 */
const PAGE_LABELS = { classroom: '总览', chat: '统一对话', tasks: '协同', jobs: '定时',
                      memory: '记忆中心', skills: '技能中心', kb: '知识库', mcp: '工具', ports: '端口', telemetry: '遥测',
                      assets: '资产', localprojects: '本机项目', github: 'GitHub 项目', resources: '资源监控',
                      'settings-model': '设置 · 模型', 'settings-github': '设置 · GitHub',
                      'settings-token': '设置 · 终端口令',
                      'settings-logs': '设置 · 日志' };
const navOpenStored = lsGet('hub.nav.open');
let navOpen = navOpenStored === null ? 'agents' : navOpenStored;   // 首屏默认展开 AGENTS；'' = 用户主动全收起
let curPage = '';
/* 排序：异常置顶 > running > installed > stopped，同级按名称。
   P1-20：异常档改用 agentHealth()（vitsals 实测 verdict）而不是 a.status。
   旧口径 NAV_RANK.error 这一档实际永远命中不到 —— vitals 不把 status 打成
   'error'，于是"启动异常"的 Agent 照样按 running 排在中间，异常项被埋在列表里，
   与顶栏那个恒为 0 的异常计数是同一个病根（判据取了一个永远不取该值的字段）。 */
const NAV_RANK = { running: 1, installed: 2, stopped: 3 };
function navRank(a) {
  if (typeof agentHealth === 'function' && agentHealth(a).bad) return 0;
  return NAV_RANK[a.status] == null ? 9 : NAV_RANK[a.status];
}
function navMatch(a, q) {
  // 侧栏搜索：与全站其他搜索框同一口径（fuzzyMatch：精确恒 1000 压倒近似）。
  return fuzzyMatch([[a.name, 3], [a.id, 2], [a.port, 1]], q) > 0;
}
// 搜索词高亮
/* P1-22（2026-09-30）：高亮改在**转义之后**的串上匹配。
   旧写法拿原文下标 idx 去切 escapeHtml(text)：
     text = 'a & b'，查 '&' → 原文 idx=2，切转义串得到 'a &' 之类错位，
   含 & < > " ' 时高亮会盖错字符（轻则高亮到半个实体，重则切出 < 破坏 HTML）。
   正确做法：先转义成最终 HTML，再在这份 HTML 上匹配 —— 但那样查询词里的
   & 也要跟着转义才搜得到，所以两边各自转义后再比。 */
function hlMatch(text, q) {
  if (!q) return escapeHtml(text);
  const escaped = escapeHtml(text);
  const needle = escapeHtml(q);
  if (!needle) return escaped;
  const hay = escaped.toLowerCase(), nd = needle.toLowerCase();
  let result = '', last = 0, idx = hay.indexOf(nd);
  while (idx !== -1) {
    result += escaped.slice(last, idx) + '<mark>' + escaped.slice(idx, idx + needle.length) + '</mark>';
    last = idx + needle.length;
    idx = hay.indexOf(nd, last);
  }
  return result + escaped.slice(last);
}
function navItemHtml(a) {
  const st = seatStateOf(a);
  const on = (curPage === 'chat' && a.id === chatPick) ? ' on' : '';
  // 窄栏里名称会截断，title 里给全量信息（名称 · 状态 · 端口 · 描述）
  const tip = a.name + ' · ' + seatLabelOf(a) + (a.port ? ' · :' + a.port : '') +
    (a.verdict_reason ? '\n' + a.verdict_reason : '') +
    (a.description ? '\n' + a.description : '');
  // 搜索高亮：名称用 hlMatch，ID 和端口保持原样
  const nameHtml = hlMatch(a.name || a.id, ($('navSearch') ? $('navSearch').value.trim() : ''));
  // 操作按钮：按 entries 类型渲染（最多3个）
  const es = a.entries || [];
  const actBtns = [];
  if (es.some(e => e.type === 'embed')) actBtns.push('<span class="act-btn" onclick="event.stopPropagation();gotoChat(\'' + a.id + '\',\'embed\')" title="嵌入会话">' + ico('monitor') + '</span>');
  if (es.some(e => e.type === 'term')) actBtns.push('<span class="act-btn" onclick="event.stopPropagation();gotoChat(\'' + a.id + '\',\'term\')" title="终端">' + ico('terminal') + '</span>');
  /* v0.10.1：删掉「对话」图标。智管对话（hub 自带会话页）已在 v0.10 移除，profile 里残留的
     chat entry 实测为死入口：claude / jcode 两个实体 POST /api/agents/{id}/chat 均 HTTP 500。
     留着它 = 坏入口（比没入口更糟），且让无 Web UI 的 Agent 行多出第三个图标。 */
  // 启动按钮：installed/stopped 状态的 agent
  if (a.kind === 'agent' && (st === 'installed' || st === 'stopped') && es.some(e => e.type === 'term')) {
    actBtns.push('<span class="act-btn start-btn" onclick="event.stopPropagation();startAgent(\'' + a.id + '\')" title="启动">' + ico('play') + '</span>');
  }
  const actsHtml = actBtns.length ? '<span class="nav-acts">' + actBtns.join('') + '</span>' : '';
  /* P2-B：行尾忙碌点。有活着的终端会话才亮（12-activity 的 ACTIVITY 口径），
     无会话返回空串 ⇒ 零会话时侧栏与改动前逐像素一致。 */
  const busyDot = (typeof activityDotHtml === 'function') ? activityDotHtml(a) : '';
  return '<button class="nav-item' + on + '" data-entity="' + escapeHtml(a.id) + '"' +
    ' title="' + escapeHtml(tip) + '" aria-label="' + escapeHtml(a.name + ' ' + seatLabelOf(a)) + '">' +
    '<span class="s-badge ' + st + '"></span>' +
    '<span class="lbl">' + nameHtml + '</span>' +
    (a.port ? '<span class="nav-port">:' + a.port + '</span>' : '') +
    busyDot + actsHtml + '</button>';
}
/* v0.12.3：行内状态文字（.nav-st）与其横向滚动窗（rollNavStatus / st-roll）已按用户要求整体删除。
   行内只留 .s-badge 方块表达在线/离线；完整状态串在上面的 tip（hover）与工作台卡片里给。 */
// 启动 agent：创建新的终端会话
async function startAgent(id) {
  try {
    const d = await api('/api/term/sessions', { method: 'POST', headers: termHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ agent_id: id }) });
    toast('已拉起 ' + id + ' 终端', 'ok');
    gotoChat(id, 'term');
    setTimeout(() => termConnect(d.session.id, id, { user: true }), 100);
  } catch (e) { toast(e.message, 'err'); }
}
/* ── v0.13.0 左侧历史下拉：同一时刻只展开一个 agent（与 navOpen 手风琴同构，D3）。
   histOpen 进 localStorage；数据缓存在 HIST —— 30s loadAgents 重绘时不闪空白。 ── */
const HIST_LIMIT = 8;                                   // v0.13.49：每 agent 8 条（原 5 条）
/* ⚠ 声明位置是硬约束，不许往下挪：下面 histBootstrap() 是**顶层 IIFE**，会同步走
histLoad() → renderNav() → 读本变量。09-23 自研 APP 事故就是顺序被破坏：手机那份
localStorage 有 hub.term.token ⇒ 早期路径被激活，而 let 声明在 renderNav 之后 ⇒
抛 ReferenceError: Cannot access '_navHtml' before initialization（hub.js:1907），
hub.js 当场死亡 ⇒ 菜单空白 + initSidebar 从未执行 + 抽屉停在展开态遮住正文。
浏览器那份没有 hub.term.token，走不到这条路 ⇒ 这就是「局域网正常/Tailscale 异常」
的真判据（不是缓存、不是网络、不是 origin 的 IP 段）。
闸门：tests/test_tdz_order.py（静态扫同类顺序违规；红基线取修复前的 git 版本）。 */
let _navHtml = '';   // 上一次渲染的菜单 HTML，用于跳过无变化的重写
const TERM_HIST_AGENTS = ['grok', 'claude', 'jcode', 'hermes', 'codex', 'qoder', 'opencode', 'cursor', 'codebuddy', 'dsh'];   // 与后端 SESSION_STORES 同集合（tests/test_sessions_store.py 对账）
// ★窄屏首屏**不恢复**上次的历史展开项。`hub.hist` 是按 origin 隔离的存量，一旦参与
// 首屏判定，同一个动作在不同入口（局域网 IP / Tailscale IP）就会走出不同结果：
// 09-23 23:3x 四格实测 —— hist 空 ⇒ 点 agent 名称只展开列表、侧栏不收起；
// hist='claude' ⇒ 点名称即收起侧栏。两个 origin 各自一致、彼此不同 ⇒ 差异纯属存量，
// 与网络/Tailscale 无关。闸门：tests/verify_collapse_symmetry.py。
let histOpen = hubNarrow() ? '' : (lsGet('hub.hist') || '');
const HIST = {};                                        // agent_id -> {items,note,loading,err}

function hhTime(ts) {                                   // 绝对日期：相对时间每轮变化会破 DOM diff
  // v0.13.49：**不再带 HH:MM**。侧栏只有 240px 可用宽，时间占的那 5 个字符直接从
  // 摘要里抢走一小半可视长度（实测标题被挤到 0 宽）；挑哪条续聊也不靠天数内的钟点，
  // 日期足够定位。精确到分钟的信息没丢——它还在 title 悬停里。
  if (!ts) return '';
  const d = new Date(ts * 1000), p = n => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate());
}

/* 跳目录之后，同名会话可能来自不同工程：只在「不属于画像目录」时补一个目录尾名，
   画像目录本身不标（多数条目都是它，标了反而吵）。 */
function hhDir(cwd, home) {
  if (!cwd || !home || cwd === home) return '';
  const seg = String(cwd).replace(/\/+$/, '').split('/').filter(Boolean);
  return seg.length ? seg[seg.length - 1] : '';
}

function histHtml(aid) {
  const h = HIST[aid];
  if (!h || h.loading) return '<div class="nav-hist"><div class="hh-note">读取历史…</div></div>';
  if (h.err) return '<div class="nav-hist"><div class="hh-note">历史读取失败：' + escapeHtml(h.err) + '</div></div>';
  const rows = (h.items || []).map(it =>
    '<div class="hh-row" data-agent="' + escapeHtml(aid) + '" data-sid="' + escapeHtml(it.id) + '"' +
    ' title="' + escapeHtml(it.title) + (it.cwd ? ' ｜ ' + escapeHtml(it.cwd) : '') + '">' +
    '<span class="hh-t">' + escapeHtml(it.title) + '</span>' +
    '<span class="hh-cw">' + hhDir(it.cwd, h.home) + '</span>' +
    '<span class="hh-ts">' + hhTime(it.ts) + '</span></div>').join('');
  /* v0.13.49：删掉「历史会话（N）」标题行。
     侧栏 240px 里它白白吃掉一行高度，而且它宣告的信息（这是历史、共几条）
     从「行本身就是会话条目 + 一共就这几行」已经自己说清了；条数还能从行尾日期
     与滚动位置看出来。留着它只会把 8 条记录往下顶。 */
  // note 在“已经有行”时也要显：截断/降级被吞掉的话，残缺结果看着就像完整清单
  // （09-22 jcode 只显 1 条那次，正是“扫描窗口用尽”的 note 没人看见）。
  const tail = (rows && h.note) ? '<div class="hh-note">' + escapeHtml(h.note) + '</div>' : '';
  return '<div class="nav-hist">' +
         (rows || '<div class="hh-note">' + escapeHtml(h.note || '该目录暂无可续会话') + '</div>') + tail + '</div>';
}

function histLoad(aid) {
  HIST[aid] = { items: [], note: '', loading: true, err: '' };
  renderNav();
  api('/api/term/history/' + encodeURIComponent(aid) + '?limit=' + HIST_LIMIT, { headers: termHeaders() })
    .then(d => { HIST[aid] = { items: d.items || [], note: d.note || '', home: d.cwd || '', loading: false, err: '' }; })
    .catch(e => { const m = String((e && e.message) || e);
      // 新前端 + 未重启的旧后端＝路由不存在（FastAPI 回 Not Found）。说人话，别抛生涩 404。
      // ⚠️ 单元名 2026-10-08 起是 agenthub.service；写旧名的后果是教用户去重启一个
      // 已停用的单元（restart 它既起不来又会在 journald 里刷重试），等于给错药方。
      HIST[aid] = { items: [], note: '', loading: false,
                    err: /not\s*found|404/i.test(m) ? '后端未更新：需重启 agenthub.service 后生效' : m }; })
    .then(() => renderNav());                            // 无 finally 依赖：老 Safari 也走得到
}

/* 刷新后 histOpen 会从 localStorage 复原，但 HIST 缓存是空的——不补一次拉取，下拉就
   永远停在「读取历史…」（实测 reload 必现）。没存过 token 时干脆收起：留个展开空壳更误导，
   而且 termToken() 会在每次刷新都弹一次口令框。 */
(function histBootstrap() {
  if (!histOpen) return;
  if (!TERM_HIST_AGENTS.includes(histOpen) || !lsGet('hub.term.token')) {
    histOpen = '';
    lsRemove('hub.hist');
    return;
  }
  histLoad(histOpen);
})();

async function termResume(agentId, sid) {
  try {
    const d = await api('/api/term/sessions', { method: 'POST',
      headers: termHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ agent_id: agentId, session_id: sid }) });
    toast('已在终端里续聊该历史会话', 'ok');
    gotoChat(agentId, 'term');
    termConnect(d.session.id, agentId, { user: true });
    termRefreshList();
  } catch (e) { toast('续聊失败：' + e.message, 'err'); }
}

/* 菜单行 = 实体行本体 + （命中展开项时）历史块。renderNav 的三分支 map 统一走这里。 */
function navRow(a) { return navItemHtml(a) + (a.id === histOpen ? histHtml(a.id) : ''); }

function renderNav() {
  const box = $('navTree');
  if (!box) return;
  const q = ($('navSearch') ? $('navSearch').value.trim() : '').toLowerCase();
  const all = AGENTS.filter(a => a.kind === 'agent');
  const infra = AGENTS.filter(a => a.kind !== 'agent');
  const lists = {
    agents: q ? all.filter(a => navMatch(a, q)) : all.slice().sort((x, y) => navRank(x) - navRank(y) || String(x.name||'').localeCompare(String(y.name||''), 'zh')),
    infra: q ? infra.filter(a => navMatch(a, q)) : infra.slice().sort((x, y) => navRank(x) - navRank(y) || String(x.name||'').localeCompare(String(y.name||''), 'zh')),
    system: q ? [] : SYS_PAGES,
    settings: q ? [] : SET_PAGES,   // v0.13.43：设置组（模型 / GitHub / 终端口令）
  };
  // 搜索结果计数
  let totalMatch = 0;
  if (q) {
    totalMatch = all.filter(a => navMatch(a, q)).length + infra.filter(a => navMatch(a, q)).length;
  }
  const html = NAV_ORDER.map(g => {
    const list = lists[g];
    if (q && !list.length) return '';
    const open = q ? true : navOpen === g;
    let body = '';
    if (g === 'infra') {
      NAV_SUB_KINDS.forEach(([k, label]) => {
        const sub = list.filter(a => a.kind === k);
        if (sub.length) body += '<div class="nav-sub">' + label + '</div>' + sub.map(navRow).join('');
      });
      const rest = list.filter(a => !NAV_SUB_KINDS.some(([k]) => k === a.kind));
      if (rest.length) body += '<div class="nav-sub">其他</div>' + rest.map(navRow).join('');
    } else if (g === 'system' || g === 'settings') {
      /* 设置组与系统页同渲染（data-sys ⇒ 侧栏委托 go(page)），只是不挂健康点。 */
      /* v0.13.27：三中心（memory/skills/kb）行尾挂健康点（CENTER_HEALTH，
         loader 完成时写入；空=未加载不显示，ok/warn/err 对应 s-badge 色族）。 */
      const hlth = p => (typeof CENTER_HEALTH !== 'undefined' && CENTER_HEALTH[p]) ?
        '<span class="s-badge ' + (CENTER_HEALTH[p] === 'ok' ? 'running' : CENTER_HEALTH[p] === 'warn' ? 'installed' : 'error') + '"></span>' : '';
      const HLTH_PAGE = { memory: 1, skills: 1, kb: 1 };
      /* P1-18：补 #badge-<page> 挂载点。setBadge 一直在写这两个元素的 textContent，
         此前模板里不存在 ⇒ 每次都 el 为 null 直接 return，MCP 服务器数 / 启用中的
         定时任务数算了但永远显示不出来。空内容靠 CSS :empty 不占位。 */
      const badge = p => '<span class="nav-badge" id="badge-' + p + '"></span>';
      body = list.map(([p, label, ic]) =>
        // v0.13.74：补 title/aria-label。收起态（窄屏 48~52px 图标条）下 .lbl 是
        // display:none，**手机上又没有 hover** ⇒ 这些图标此前既没有可见文字、
        // 也没有可访问名，屏幕阅读器只能读出「按钮」。label 直接复用现成的变量，
        // 不新建映射（PAGE_LABELS 才是页名→中文名的唯一真源）。
        '<button class="nav-item' + (curPage === p ? ' on' : '') + '" data-sys="' + p + '"' +
        ' title="' + escapeHtml(label) + '" aria-label="' + escapeHtml(label) + '">' +
        ico(ic) + '<span class="lbl">' + label + '</span>' + (HLTH_PAGE[p] ? hlth(p) : '') +
        badge(p) + '</button>').join('');
    } else {
      body = list.map(navRow).join('');
    }
    if (!body) body = '<div class="nav-empty">' + (AGENTS.length ? '无匹配' : '加载中…') + '</div>';
    return '<div class="nav-acc' + (open ? ' open' : '') + '">' +
      // v0.13.74：同上，收起态下手风琴头也只有图标，补 title/aria-label。
      '<button class="nav-acc-head" data-group="' + g + '" aria-expanded="' + (open ? 'true' : 'false') + '"' +
      ' title="' + escapeHtml(NAV_GROUPS[g]) + '" aria-label="' + escapeHtml(NAV_GROUPS[g]) + '">' +
      ico(open ? 'chevron-down' : 'chevron-right', null, 'caret') +
      ico(NAV_ICONS[g], 'md') +
      '<span class="lbl">' + NAV_GROUPS[g] + '</span>' +
      '<span class="badge">' + list.length + '</span></button>' +
      '<div class="nav-acc-body">' + body + '</div></div>';
  }).join('');
  /* 内容没变就不重写 DOM —— loadAgents 每 30s 刷一次，重写会把 #navTree 的滚动位置弹回顶部 */
  if (html !== _navHtml) { box.innerHTML = html; _navHtml = html; }
  // 搜索结果计数提示
  const countEl = $('navSearchCount');
  if (countEl) countEl.textContent = q ? ' 共 ' + totalMatch + ' 项' : '';
}
