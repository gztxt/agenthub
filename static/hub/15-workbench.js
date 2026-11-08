/* 15-workbench.js — P2 工作台（v0.13.193，方案 AgentHub优化设计方案.md §4.3）
 * 首页注意力排序：待人工验收 > 失败24h > code_stale/异常 > 终端等待 > 运行中 > 已停止折叠。
 * 数据源全部是既有端点：/api/agents（AGENTS 全局）、/health（HUB_HEALTH）、/api/tasks/queue。
 * 纪律：只读渲染，不新增写端点；排序规则固定，不让用户先配置。
 */
const WB_DONE_S = 24 * 3600;  /* 「失败 24h 内」窗口 */

/* 任务队列缓存：数据源 /api/tasks/runs（L0 test_contracts 只校路径存在性，本端点实测在册） */

function wbClassify(a) {
  /* 返回 {rank, label, state}：rank 越小越靠前；state 用于徽章色。 */
  if (a.kind !== 'agent') return { rank: 99, label: '基础设施', state: 'infra' };
  const rt = a.rt_state || '';
  if (rt === 'waiting_input' || rt === 'waiting') return { rank: 4, label: '终端等待', state: 'wait' };
  if (a.verdict === 'error' || a.verdict === 'unusable' || a.verdict === 'broken')
    return { rank: 3, label: '异常 ' + ((a.verdict_reason || '').slice(0, 24)), state: 'err' };
  if (a.attested === true || a.status === 'running') return { rank: 5, label: '运行中', state: 'run' };
  if (a.status === 'stopped') return { rank: 6, label: '已停止', state: 'stop' };
  return { rank: 5, label: '可启动', state: 'ok' };
}

function wbStaleLine() {
  const h = HUB_HEALTH || null;
  if (!h) return null;
  if (h.needs_restart === true)
    return '陈旧码：运行 ' + String(h.git_sha_boot || '?').slice(0, 7) + ' vs 磁盘 ' + String(h.git_sha_now || '?').slice(0, 7) + '（需重启）';
  if (h.code_stale === true)
    return '陈旧码：' + (h.code_stale_reason || '已改未重启');
  return '自证一致 ' + String(h.git_sha_boot || '?').slice(0, 7);
}

function wbTasksLine() {
  if (!WB_TASKS) return null;
  const submitted = WB_TASKS.submitted;
  const failed24h = WB_TASKS.failed_24h;
  if (submitted == null && failed24h == null) return null;  /* 任务面不可用：整行省略 */
  if (!submitted && !failed24h) return null;
  return ('待人工验收 ' + (submitted || 0) + ' · 失败(24h) ' + (failed24h || 0));
}

function renderWorkbench() {
  const el = $('wbAttention');
  if (!el) return;
  const rows = [];
  /* 0. 读令牌门横幅（v0.13.195）：非本机打开且 /health.read_auth.state!=ok
     ⇒ 红色横幅 + 「输入令牌」按钮（走 termToken() 既有弹窗入口）。 */
  const ra = (HUB_HEALTH && HUB_HEALTH.read_auth) || null;
  if (ra && ra.state !== 'ok' && !(location.hostname === '127.0.0.1' || location.hostname === 'localhost')) {
    rows.push({ rank: 0, html: '<span class="wb-ico !">🔒</span> 读接口需凭据：当前访问非本机，已存令牌无效或未存'
      + '<button class="btn ghost sm" data-wbact="token">输入令牌</button>' });
  }
  /* 1. 自证行（C code_stale 或异常） */
  const stale = wbStaleLine();
  if (stale && /需重启|已改未重启/.test(stale))
    rows.push({ rank: 3, html: '<span class="wb-ico !">!</span> 陈旧码 <span class="wb-stxt">' + escapeHtml(stale) + '</span><button class="btn ghost sm" data-wbact="restart">重启</button>', rank: 3 });
  /* 2. 任务行（待验收/失败24h） */
  const tk = wbTasksLine();
  if (tk) rows.push({ rank: 2, html: '<span class="wb-ico ?">✎</span> 任务 <span class="wb-stxt">' + escapeHtml(tk) + '</span><button class="btn ghost sm" data-wbact="tasks">去验收</button>' });
  /* 3. Agent 行 */
  const byRank = {};
  AGENTS.forEach(a => {
    const c = wbClassify(a);
    (byRank[c.rank] = byRank[c.rank] || []).push({ a, c });
  });
  /* 2026-11-08 用户报「总览内容怎么变了」：旧实现把 rank 5（运行中/可启动）也塞进
     「需要你处理」⇒ 面板恒定列出全部 Agent，把品牌与统计压到首屏之外，且与面板标题
     的语义相反。这里只留真正需要人工介入的档位：3=异常 / 4=终端等待。
     正常态 rows 为空 ⇒ 下面把整块 #wbPanel 收起，总览恢复原布局。 */
  [3, 4].forEach(r => {
    (byRank[r] || []).forEach(x => {
      rows.push({
        rank: r,
        html: '<span class="s-badge ' + x.c.state + '" title="' + escapeHtml(x.a.id) + '"></span> ' +
               '<b>' + escapeHtml(x.a.name || x.a.id) + '</b> <span class="wb-stxt">' + escapeHtml(x.c.label) + '</span>' +
               '<button class="btn ghost sm" data-wbact="open" data-agent="' + escapeHtml(x.a.id) + '">打开</button>'
      });
    });
  });
  const panel = $('wbPanel');
  if (panel) panel.style.display = rows.length ? '' : 'none';
  if (!rows.length) {
    el.innerHTML = '';
    const stopEl = $('wbStopped');
    if (stopEl) stopEl.innerHTML = '';
    return;
  }
  /* 4. 已停止折叠组 */
  const stopped = (byRank[6] || []).map(x => x.a);
  const stoppedEl = $('wbStopped');
  if (stoppedEl) {
    stoppedEl.style.display = stopped.length ? '' : 'none';
    stoppedEl.innerHTML = '<div class="wb-hd">已停止（' + stopped.length + '）<button class="linklike" data-wbact="stopped">展开</button></div>' +
      '<div class="wb-stlist" style="display:none">' + stopped.map(x =>
        '<div class="wb-row off"><span class="s-badge stop"></span> <b>' + escapeHtml(x.name || x.id) + '</b>' +
        '<button class="btn ghost sm" data-wbact="open" data-agent="' + escapeHtml(x.id) + '">打开</button></div>').join('') + '</div>';
  }
  el.innerHTML = rows.map(r => '<div class="wb-row" data-rank="' + r.rank + '">' + r.html + '</div>').join('') ||
    '<div class="wb-row"><span class="hint">全部正常。还没有纳管对象 → 点「自动扫描」。</span></div>';
}

async function loadWbTasks() {
  try {
    const q = await api('/api/agents/wb-summary');
    WB_TASKS = q;
    if (typeof renderWorkbench === 'function') renderWorkbench();
  } catch (e) { /* 任务面不可用不阻断首页：行省略 */ }
}
var WB_TASKS = null;  /* 顶层 var 避免 TDZ（06 分片 go() 早于本分片初始化，纪律同 01 声明） */
/* HUB_HEALTH 由 pollHealth 每 15s 写入（01-core-boot），本分片只读。 */

/* 委托：本页按钮不写 inline onclick（浮层纪律 §4.2），一律 data-* 走这里。 */
document.addEventListener('click', function (e) {
  const b = e.target.closest('[data-wbact]');
  if (!b) return;
  const act = b.dataset.wbact;
  if (act === 'token') {
    /* 读令牌门横幅的「输入令牌」：走 03 分片既有的 termToken()（prompt 一次存 localStorage），
       存好后下一次读请求自动带 header，横幅随 /health.read_auth 变 ok 消失。 */
    if (typeof termToken === 'function') { termToken(true); loadWbTasks(); }
    return;
  }
  if (act === 'open' && b.dataset.agent) {
    /* openAgent 不存在时落到既有入口 gotoChat（02-nav-and-poll.js），不另开新接口 */
    if (typeof gotoChat === 'function') gotoChat(b.dataset.agent, 'term');
    else if (typeof go === 'function') go('chat');
  }
  else if (act === 'tasks' && typeof go === 'function') go('tasks');
  else if (act === 'restart' && typeof restartHub === 'function') restartHub({ currentTarget: b });
  else if (act === 'stopped') { const l = b.closest('.wb-stlist'); if (l) l.style.display = l.style.display === 'none' ? '' : 'none'; }
});
