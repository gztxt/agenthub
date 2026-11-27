/* 18-collab.js — 协同工作台（2026-11-27 T6，方案 docs/MAGPIE-INTEROP-2-20261127.md）
 *
 * 本文件接管 #page-tasks 的交互，替换 04-terminal-ws.js 里 S3 的旧入口。
 * 旧渲染器（runsBody / taskBody / statsAgents / runPanel）**保持不动** ——
 * 只换容器与切换方式，一次改一点，坏了能定位到具体一层。
 *
 * 三条判据（都有实测来源）：
 * ① 一屏数据只发一次请求：顶部状态条读 /api/tasks/overview，不再 4 个端点拼；
 * ② 参数与结果分左右：改参数时结果仍在视野内（窄屏 <768px 折叠单栏）；
 * ③ 显式选择必须作数：选了 Agent 就带 `force_agents=1` 下发 —— 旧实现里
 *    `--agent pi` 被 Leader 静默改成 claude-code（run 908a6cd0ae 实证）。
 */

/* ── 状态条 ─────────────────────────────────────────── */
function cbChip(id, text, tone) {
  var el = $(id);
  if (!el) return;
  el.textContent = text;
  el.classList.toggle('bad', tone === 'bad');
  el.classList.toggle('warn', tone === 'warn');
}

async function cbLoadStatus() {
  var d;
  try { d = await api('/api/tasks/overview?days=7'); }
  catch (e) { cbChip('cbModels', '网关不可读：' + e.message, 'bad'); return; }
  cbApplyStatus(d);
}

/* ── Agent 多选芯片 ──────────────────────────────────── */
var cbAgentSel = [];           /* 选中顺序即 spread 轮转顺序 */

function cbRenderAgents(pool) {
  var host = $('cbAgents');
  if (!host) return;
  var agents = (pool && pool.agents) || [];
  if (!agents.length) { host.innerHTML = '<span class="hint">读不到 agent 池</span>'; return; }
  host.innerHTML = agents.map(function (a) {
    var id = a.id || '';
    var ok = !!a.available;
    var why = escapeHtml(a.reason || '');
    var cls = 'cb-ag' + (cbAgentSel.indexOf(id) >= 0 ? ' on' : '');
    return '<button type="button" class="' + cls + '" data-ag="' + escapeHtml(id) + '"'
      + (ok ? '' : ' disabled title="' + why + '"')
      + ' title="' + escapeHtml((a.display || id) + (ok ? '' : '：' + a.reason)) + '">'
      + escapeHtml(id) + '</button>';
  }).join('');
  cbSyncAgentsHint();
}

function cbSyncAgentsHint() {
  var h = $('cbAgentsHint');
  if (!h) return;
  if (!cbAgentSel.length) { h.textContent = '未选 = 自动'; return; }
  h.textContent = '已选 ' + cbAgentSel.length + ' 个（' + (($('cbMode') || {}).value === 'spread' ? '轮转' : '硬指定') + '）';
}

function cbBindAgents() {
  var host = $('cbAgents');
  if (!host || host.dataset.bound) return;
  host.dataset.bound = '1';
  host.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-ag]');
    if (!b || b.disabled) return;
    var id = b.getAttribute('data-ag');
    var i = cbAgentSel.indexOf(id);
    if (i >= 0) cbAgentSel.splice(i, 1);
    else cbAgentSel.push(id);
    b.classList.toggle('on', i < 0);
    cbSyncAgentsHint();
    cbFillTaskModelSelect();
  });
}

/* ── Tab 切换（状态只活在内存，不写 localStorage）──────── */
var cbTabKey = 'live';

function cbTab(name) {
  cbTabKey = name || 'live';
  [].slice.call(document.querySelectorAll('#cbTabs .cb-tab')).forEach(function (b) {
    b.classList.toggle('on', b.getAttribute('data-ctab') === cbTabKey);
  });
  [].slice.call(document.querySelectorAll('#page-tasks .cb-view')).forEach(function (p) {
    p.classList.toggle('on', p.getAttribute('data-cpanel') === cbTabKey);
  });
  /* 切到「汇总」才拉统计（近 30 天聚合，首屏不需要） */
  if (cbTabKey === 'stats' && typeof loadTaskStats === 'function') loadTaskStats();
}

function cbBindTabs() {
  var tabs = $('cbTabs');
  if (!tabs || tabs.dataset.bound) return;
  tabs.dataset.bound = '1';
  tabs.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-ctab]');
    if (b) cbTab(b.getAttribute('data-ctab'));
  });
}

/* ── 请求体：显式选择即硬约束 ─────────────────────────── */
function cbTaskBodyFromUi() {
  var body = { goal: ($('taskGoal') || {}).value || '', auto_run: true };
  body.goal = String(body.goal).trim();
  if (cbAgentSel.length) {
    body.agents = cbAgentSel.slice();
    body.force_agents = true;                       /* 用户选了就必须作数 */
    body.assign_mode = (($('cbMode') || {}).value === 'spread') ? 'spread' : 'auto';
  }
  var m = $('taskModel');
  if (m && m.value) body.model = m.value;
  var r = $('taskRounds');
  if (r && r.value) body.rounds = parseInt(r.value, 10);
  var b = $('cbBudget');
  var budget = b && b.value ? parseInt(b.value, 10) : 0;
  if (budget > 0) body.budget_tokens = budget;
  return body;
}

/* 模型下拉：只有**单选一个** Agent 时才列具体模型（多选时「钉死一个模型」与
   「让不同 agent 各自最合适」互斥，列出来就是误导）。 */
async function cbFillTaskModelSelect() {
  var sel = $('taskModel');
  if (!sel) return;
  var hint = $('taskPreview');
  if (cbAgentSel.length > 1) {
    sel.innerHTML = '<option value="">模型：自动（多 agent 各自最合适）</option>';
    sel.value = '';
    if (hint) hint.textContent = '已选多个 Agent：模型按各自模型域自动选，不钉死。';
    return;
  }
  var agent = cbAgentSel[0] || '';
  sel.innerHTML = '<option value="">模型：自动（t1/t2/t3 档位）</option>';
  sel.value = '';
  if (!agent) {
    if (hint) hint.textContent = '未指定 Agent = 自动混排：模型由难度→档位决定（失败自动升档）。';
    return;
  }
  try {
    var d = await api('/api/models?agent_id=' + encodeURIComponent(agent));
    var list = (d && d.models) || [];
    if (!list.length) {
      if (hint) hint.textContent = '该 Agent 没有可列模型（模型域不在网关）。钉死模型对它不生效，请用「自动」。';
      return;
    }
    sel.innerHTML += list.map(function (m) {
      var tag = m.billing === 'free' ? ' · 免费' : '';
      return '<option value="' + escapeHtml(m.id) + '">' + escapeHtml(m.id) + tag + '</option>';
    }).join('');
    if (hint) hint.textContent = '已指定 ' + agent + '：可钉死具体模型，留空则按档位自动选。';
  } catch (e) {
    if (hint) hint.textContent = '模型清单拉取失败：' + e.message + '（不影响「自动」）';
  }
}

/* ── 进页入口（覆盖 04 的同名函数：旧的读 taskAgent 单选，已不存在）── */
async function cbEnterTasksPage() {
  cbBindTabs();
  cbTab(cbTabKey);
  cbBindAgents();
  var pool = null;
  try { pool = await api('/api/tasks/pool'); } catch (e) { pool = null; }
  if (pool && pool.rounds) fillRoundSelect(pool.rounds);
  else fillRoundSelect(null);
  cbRenderAgents(pool);
  /* 状态条数据优先用 pool 里已内联的 magpie 段（省一次请求），缺了再单拉 overview */
  if (pool && pool.magpie && !pool.magpie.error) {
    try { var ov = await api('/api/tasks/overview?days=7'); cbApplyStatus(ov); }
    catch (e) { cbLoadStatus(); }
  } else {
    cbLoadStatus();
  }
  cbFillTaskModelSelect();
  if (typeof bindTaskAcc === 'function') bindTaskAcc();
  if (typeof bindRunPanelOutside === 'function') bindRunPanelOutside();
  if (typeof bindRunActions === 'function') bindRunActions();
  refreshTasks();
}

/* 把 overview 的响应喂给状态条（与 cbLoadStatus 同一渲染，避免两套口径） */
function cbApplyStatus(d) {
  if (!d) return;
  var m = d.magpie || {};
  if (m.error) cbChip('cbModels', '网关：' + m.error, 'bad');
  else cbChip('cbModels', '网关 ' + (m.models == null ? '?' : m.models) + ' 个模型');
  var provs = m.providers || {}, blocked = m.blocked || {};
  var names = Object.keys(provs);
  var txt = names.map(function (p) {
    var st = (provs[p] || {}).state, rem = (provs[p] || {}).remaining_pct;
    return p + (st === 'ok' ? '' : '(' + st + ')') + (rem == null ? '' : ' ' + rem + '%');
  }).join(' · ') || 'provider 未知';
  var bad = Object.keys(blocked);
  cbChip('cbProviders', txt, bad.length ? (bad.length === names.length ? 'bad' : 'warn') : '');
  if (bad.length) $('cbProviders').title = bad.map(function (p) { return p + '：' + blocked[p]; }).join('\n');
  var c = d.cost || {};
  cbChip('cbCredits', '近 7 天 ' + (c.credits == null ? '?' : c.credits) + ' credits'
    + (c.unmatched ? '（' + c.unmatched + ' 条未回填）' : ''));
  var f = (d.flash || {}).observed || {}, p = (d.flash || {}).planned || {};
  cbChip('cbFlash', 'flash 实测 ' + (f.ratio == null ? '?' : Math.round(f.ratio * 100) + '%')
    + ' / 档位 ' + (p.ratio == null ? '?' : Math.round(p.ratio * 100) + '%'),
    f.ratio != null && f.ratio < 0.5 ? 'warn' : '');
  var r = d.runs || {};
  cbChip('cbRunning', '运行中 ' + (r.running || 0) + ' · 近 7 天 ' + (r.window || 0) + ' 条');
}

/* 运行详情打开时切到「运行中」tab（旧实现靠手风琴单开，现在靠 tab） */
var cbOpenRunPatched = false;
function cbPatchOpenRun() {
  if (cbOpenRunPatched || typeof window.openRun !== 'function') return;
  cbOpenRunPatched = true;
  var orig = window.openRun;
  window.openRun = function (id) {
    cbTab('live');
    var empty = $('cbLiveEmpty');
    if (empty) empty.style.display = 'none';
    return orig.apply(this, arguments);
  };
}
document.addEventListener('DOMContentLoaded', cbPatchOpenRun);
