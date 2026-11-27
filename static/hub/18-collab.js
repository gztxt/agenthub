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
  /* 2026-11-27 实测（390×844）：窄屏下左栏在上、右栏 top=928 已在视口 844 之外
     ⇒ 用户点了「历史」仍看到空，主观就是「没有历史记录」。
     切 tab 时把右栏滚进视口；宽屏两栏并排，不需要也不该动滚动位置。 */
  cbScrollToRight();
  /* 切到「汇总」才拉统计（近 30 天聚合，首屏不需要） */
  if (cbTabKey === 'stats') {
    if (typeof loadTaskStats === 'function') loadTaskStats();
    cbLoadCostPerf();
  }
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


/* 窄屏把右栏（结果/历史）滚进视口。宽屏两栏并排，不需要也不该动滚动位置。 */
function cbScrollToRight() {
  try {
    if (typeof HUB_NARROW_MQ === 'undefined' || !HUB_NARROW_MQ.matches) return;
    var right = document.querySelector('#page-tasks .cb-right');
    var cb = document.querySelector('#page-tasks .cb');
    if (!right || !cb) return;
    var off = right.getBoundingClientRect().top - cb.getBoundingClientRect().top;
    cb.scrollTop = Math.max(0, cb.scrollTop + off - 6);
  } catch (e) { /* 滚动只是便利，失败不影响内容 */ }
}

/* 窄屏左栏折叠（2026-11-27：参数占 490px，把结果挤出视口） */
function cbBindLeftToggle() {
  var hd = $('cbLeftToggle'), left = $('cbLeft');
  if (!hd || !left || hd.dataset.bound) return;
  hd.dataset.bound = '1';
  hd.addEventListener('click', function () {
    var on = !left.classList.contains('open');
    left.classList.toggle('open', on);
    hd.setAttribute('aria-expanded', on ? 'true' : 'false');
    var h = $('cbLeftHint');
    if (h) h.textContent = on ? '点收起' : '点开展开参数';
    if (on) { var t = $('taskGoal'); if (t) t.focus(); }
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



/* ── 执行结果摘要条（2026-11-27 用户令）─────────────────────────
   「任务执行情况摘要不够明显，例如哪些执行成功、哪些执行失败，
     执行失败的后面有继续执行按钮，即可重新加入待执行任务列表」
   判据：
   ① 四格计数一眼可读（成功/失败/运行中/待执行），颜色编码；
   ② 失败项逐条列出（task_id + 原因），每条带「继续执行」；
   ③ 「继续执行」= POST /api/tasks/{run}/retry 带 task_id（后端已支持单条重试），
      点完即回 pending ⇒ 由 runner 逐步执行，不重跑整条 run。 */
var cbOutcomeBound = false;

function cbRenderOutcome(runId, tasks) {
  var host = $('cbOutcome');
  if (!host) return;
  var n = { success: 0, failed: 0, running: 0, pending: 0, blocked: 0 };
  var fails = [];
  (tasks || []).forEach(function (t) {
    var st = t.status || '';
    if (st === 'success') n.success++;
    else if (st === 'failed') { n.failed++; fails.push(t); }
    else if (st === 'running' || st === 'verifying' || st === 'submitted') n.running++;
    else if (st === 'blocked') { n.blocked++; fails.push(t); }
    else n.pending++;
  });
  var html = ''
    + '<span class="cb-oc ok">✓ 成功 ' + n.success + '</span>'
    + '<span class="cb-oc bad">✗ 失败 ' + n.failed + '</span>'
    + (n.blocked ? '<span class="cb-oc bad">⛔ 阻塞 ' + n.blocked + '</span>' : '')
    + '<span class="cb-oc run">▶ 运行中 ' + n.running + '</span>'
    + '<span class="cb-oc wait">⧗ 待执行 ' + n.pending + '</span>';
  if (fails.length) {
    html += '<div class="cb-fails">' + fails.map(function (t) {
      var why = (t.error || t.verdict_reason || t.verify_reason || '无原因记录');
      if (why.length > 90) why = why.slice(0, 90) + '…';
      return '<div class="cb-fail"><span class="tid">' + escapeHtml(t.task_id) + '</span>'
        + '<span class="why" title="' + escapeHtml(t.error || t.verdict_reason || '') + '">'
        + escapeHtml(why) + '</span>'
        + '<button class="btn ghost sm" data-cb-retry="' + escapeHtml(t.task_id) + '"'
        + ' data-cb-run="' + escapeHtml(runId) + '">继续执行</button></div>';
    }).join('') + '</div>';
  }
  host.innerHTML = html;
  cbBindOutcome();
}

function cbBindOutcome() {
  var host = $('cbOutcome');
  if (!host || cbOutcomeBound) return;
  cbOutcomeBound = true;
  host.addEventListener('click', function (ev) {
    var b = ev.target.closest('[data-cb-retry]');
    if (!b) return;
    ev.stopPropagation();
    cbRetryTask(b.getAttribute('data-cb-run'), b.getAttribute('data-cb-retry'), b);
  });
}

async function cbRetryTask(runId, taskId, btn) {
  if (!runId || !taskId) return;
  if (btn) { btn.disabled = true; btn.textContent = '加入中…'; }
  try {
    await api('/api/tasks/' + runId + '/retry', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ task_id: taskId })
    });
    toast(taskId + ' 已重新加入待执行', 'ok');
    /* 立刻重绘：状态已变 pending，按钮消失；不用等 6s 轮询 */
    runSigLast = '';
    if (typeof openRun === 'function') openRun(runId);
    if (typeof refreshTasks === 'function') refreshTasks();
  } catch (e) {
    toast('重新加入失败：' + e.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = '继续执行'; }
  }
}

/* ── 汇总：成本 × 成功率（T6 收尾，2026-11-27）───────────────
   为什么要两块数据 join：`/api/tasks/stats` 有成功率没有成本，
   `/api/tasks/costs` 有成本没有成功率。按模型做 key 合并，各取各的真源，
   不在前端重算任何一边的比率。 */
async function cbLoadCostPerf() {
  var host = $('statsCostPerf');
  if (!host) return;
  var days = ($('statsDays') || {}).value || '30';
  var q = days === '0' ? '' : '?days=' + encodeURIComponent(days);
  var perf = {}, cost = {};
  try {
    var d = await api('/api/tasks/stats' + (q ? q + '&' : '?') + 'days=' + encodeURIComponent(days));
    (d.combos || []).forEach(function (c) {
      var k = c.model_eff || c.model || '';
      if (!k) return;
      var slot = perf[k] || (perf[k] = { n: 0, ok: 0 });
      slot.n += Number(c.n || 0);
      slot.ok += Number(c.ok || 0);
    });
  } catch (e) { /* 成功率取不到就只显示成本，不编造 */ }
  try {
    var c2 = await api('/api/tasks/costs?group_by=model');
    (c2.rows || []).forEach(function (r) {
      var k = r.k || '';
      if (k) cost[k] = { credits: Number(r.credits || 0), n: Number(r.n || 0) };
    });
  } catch (e) { /* 同理 */ }
  var keys = Object.keys(perf).concat(Object.keys(cost).filter(function (k) { return !perf[k]; }));
  if (!keys.length) { host.innerHTML = '<tr><td colspan="6" class="hint">暂无数据</td></tr>'; return; }
  var rows = keys.map(function (k) {
    var p = perf[k] || { n: 0, ok: 0 };
    var c = cost[k] || { credits: 0, n: 0 };
    var rate = p.n ? Math.round(p.ok / p.n * 100) + '%' : '—';
    var per = c.n ? (Math.round(c.credits / c.n * 1000) / 1000) : '—';
    return { k: k, n: p.n, ok: p.ok, rate: rate, credits: Math.round(c.credits * 1000) / 1000, per: per,
             sortKey: c.credits };
  });
  rows.sort(function (a, b) { return b.sortKey - a.sortKey; });
  host.innerHTML = rows.map(function (r) {
    return '<tr><td style="font-family:var(--font-mono);font-size:var(--fs-xs)">' + escapeHtml(r.k) +
      '</td><td>' + r.n + '</td><td>' + r.ok + '</td><td>' + r.rate + '</td><td>' +
      r.credits + '</td><td>' + r.per + '</td></tr>';
  }).join('');
}

/* ── 进页入口（覆盖 04 的同名函数：旧的读 taskAgent 单选，已不存在）── */
async function cbEnterTasksPage() {
  cbBindTabs();
  cbTab(cbTabKey);
  cbBindAgents();
  cbBindLeftToggle();
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
  cbAutoOpenRunning();
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
    cbScrollToRight();
    var empty = $('cbLiveEmpty');
    if (empty) empty.style.display = 'none';
    var p = orig.apply(this, arguments);
    /* 04 的 renderTaskTable 会把逐条任务塞进 currentRunTasks；
       这里等它渲染完再画摘要条，数据同源、不多发请求。 */
    Promise.resolve(p).then(function () {
      var byId = window.currentRunTasks || {};
      var list = Object.keys(byId).map(function (k) {
        /* currentRunTasks 里是 {task_id,status,...}，但 04 存的是 t 本身 */
        var t = byId[k] || {};
        if (!t.task_id) t = Object.assign({ task_id: k }, t);
        return t;
      });
      /* 兜底：04 的缓存可能只存了 task_id→task，缺 status 时从 DOM 读不到，
         那就再拉一次详情（只在条数>0 时）。 */
      if (!list.length || list.every(function (t) { return !t.status; })) {
        return api('/api/tasks/' + id).then(function (d) {
          cbRenderOutcome(id, d.tasks || []);
        }).catch(function () {});
      }
      cbRenderOutcome(id, list);
    });
    return p;
  };
}
/* 2026-11-27 实测：写成 `addEventListener('DOMContentLoaded', …)` **永不执行** ——
   hub.js 的同步脚本执行完毕时 DOMContentLoaded 已经发生过（实测 cbOpenRunPatched
   恒为 false、window.openRun 仍是原始实现，摘要条因此一直空白）。
   本文件在 hub.js 末尾，此时 04 已定义 openRun ⇒ 直接调用。 */
if (document.readyState === 'loading') {
  document.addEventListener('DOMContentLoaded', cbPatchOpenRun);
} else {
  cbPatchOpenRun();
}

/* ── 进页自动打开最近一条运行中的 run（T6 收尾）──────────────
   为什么做：旧流程要点「历史 → 查看」才能看到活的那条，用户的用法是
   「跑着的时候盯进度」，那一步是纯摩擦。只在**没有已选 run** 且确有运行中时触发，
   不抢用户已经打开的详情。 */
async function cbAutoOpenRunning() {
  if (currentRun) return;
  try {
    /* 不能只查 state=running：刚拆解还没开跑时状态是 `pending`（实测 run 5e9a6c2a18），
       只按 running 查会「有活任务但页面空白」。取最近 5 条，挑第一条非终态的。 */
    var d = await api('/api/tasks/runs?limit=5');
    var terminal = { success: 1, failed: 1, partial: 1, cancelled: 1 };
    var live = (d.runs || []).filter(function (r) { return !terminal[r.state]; });
    if (!live.length) return;
    var id = live[0].run_id;
    if (!id) return;
    if (typeof openRun === 'function') openRun(id);
  } catch (e) { /* 自动打开失败不影响手动路径 */ }
}
