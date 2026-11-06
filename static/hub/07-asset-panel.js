/* ══════════════════ P4 资产面板（只读门面聚合）══════════════════
   契约源：src/memory.py（/api/memory/*）、src/kb.py（/api/kb/*）、src/skill.py（/api/skill/*）。
   口径（09-24 架构裁定）：hub 只做**门面 + ACL + 审计 + 前端**，本页零写操作、零"第二权威副本"。

   ★ 为什么这个文件里最讲究的是"四态可区分"而不是好看：
     ok / empty / degraded / down 四态里，只要把后两种渲染成"暂无数据"，
     用户就会以为本机资产是空的 —— 这正是 ccpocket-bridge「active、端口在听、/health ok
     而会话起不来，静默 21 天」那一族故障在前端层的形态。闸门
     tests/test_asset_panel.py 用**抽出来的真函数**喂 degraded 与 404 两种载荷钉死这条。

   ★ 施工约束（09-23 分档五不变量 + 浮层三条）：
     - 不新增 matchMedia（断点唯一真相在 01 的 HUB_NARROW_MQ）；
     - 不裸用 localStorage（一律 lsGet/lsSet）；
     - 侧栏/菜单内禁 inline onclick ⇒ 本页交互全走 data-asset 委托，且入口函数
       initAssetPanel() 由 go('assets') 懒调用（不用顶层 IIFE：v0.13.11 的 TDZ 教训）。 */

/* 顶层状态位故意用 `var`：本分片在 hub.js 里排在 01 之后，而 01 的 go() 会调到
   本文件的函数。`let/const` 在语句执行到之前是 TDZ（v0.13.11 的 `_navHtml` 白屏
   就是这一型），而 `function` 声明整体提升 ⇒ 下面的常量表一律做成函数。 */
var assetsBound = false;   // 事件委托只绑一次（go() 会反复进本页）

function ASSET_SOURCES() {
  return [
  {
    key: 'memory', label: '记忆', box: 'assetMemory', needsQ: true,
    path: q => '/api/memory/search?q=' + encodeURIComponent(q) + '&limit=6',
    items: d => d.memories || [],
    itemText: it => it.content || it.summary || it.text || '',
    itemMeta: it => [it.category || it.type, it.agent || it.source, it.created_at ? String(it.created_at).slice(0, 10) : ''].filter(Boolean).join(' · '),
    status: d => (d.engines && d.engines.memory) || d.engine || '',
  },
  {
    key: 'kb', label: '技术文档', box: 'assetKb', needsQ: true,
    path: q => '/api/kb/search?q=' + encodeURIComponent(q) + '&k=6',
    items: d => d.results || [],
    itemText: it => it.snippet || it.text || it.title || '',
    itemMeta: it => [it.path || it.url, it.chunk ? '#' + it.chunk : ''].filter(Boolean).join(''),
    status: d => d.engine || '',
  },
  {
    key: 'skill', label: '技能', box: 'assetSkill', needsQ: false,
    path: q => '/api/skill/list?limit=6' + (q ? ('&q=' + encodeURIComponent(q)) : ''),
    items: d => d.items || [],
    itemText: it => it.name + (it.description ? (' — ' + it.description) : ''),
    itemMeta: it => [it.route, it.dir ? (it.dir.split('/').slice(-2).join('/')) : ''].filter(Boolean).join(' · '),
    status: d => (d.engine || 'disk'),
  },
  ];
}

/* 第五态 idle：某些门面端点按契约 **缺 q 就回 422**（memory/kb 检索）。
   空查询时发一个注定 422 的请求、再把后端校验错报成「部分后端不可用」，
   技术上诚实但语义误导（用户什么都没输）。idle 单独一态，不发请求。 */
function assetIdleHtml(src) {
  return '<div class="hint">「' + escapeHtml(src.label) + '」这一路需要检索词（端点缺 q 按契约回 422）。'
    + '在上方输入关键词后点「检索」。</div>';
}

/* 四态之一：ok / empty / degraded / down。判定顺序即优先级——坏消息不许被"空"掩盖。 */
function assetStateOf(d, err) {
  if (err) return /HTTP 404/.test(String(err.message)) ? 'down' : 'degraded';
  if (!d) return 'down';
  // 后端自己报的 degraded 与逐路 backends[].ok 取并集（有一路不算账就算降级）
  const bad = (d.backends || []).some(b => !b.ok) || (d.degraded || []).length > 0;
  if (bad) return 'degraded';
  return (d.count || 0) ? 'ok' : 'empty';
}

/* 状态文案与颜色同样做成函数（见上方 TDZ 注释）。
   只用主题里**真存在**的 token（--ok / --muted / --warn / --danger-text）：
   不存在的 var() 会静默退化成继承色 ⇒ 警告条变普通文字，又是新的静默不可用。 */
function assetStateText(state) {
  return ({ ok: '有结果', empty: '确实零命中', idle: '待输入检索词',
            unwired: '按设计未接入',
            degraded: '部分后端不可用', down: '端点不可用' })[state] || state;
}

function assetStateColor(state) {
  return ({ ok: 'var(--ok)', empty: 'var(--muted)', idle: 'var(--muted)',
            unwired: 'var(--muted)',
            degraded: 'var(--warn)', down: 'var(--danger-text)' })[state] || 'var(--muted)';
}

function assetBadge(src, state, note) {
  return '<div class="chip" style="min-width:0">' +
    '<b style="color:' + assetStateColor(state) + '">' + escapeHtml(src.label) +
    '：' + escapeHtml(assetStateText(state)) + '</b>' +
    (note ? '<span class="hint">' + escapeHtml(note) + '</span>' : '') + '</div>';
}

function assetResultsHtml(src, d, state, err) {
  if (state === 'down') {
    return '<span style="color:var(--danger-text)">该门面端点取不到（' +
      escapeHtml(err ? String(err.message).slice(0, 120) : 'HTTP 404') +
      '）。<b>这不是"没有资产"</b>——多半是 hub 代码比进程新（未重启）或后端未启用。</span>' +
      ' <button class="btn ghost sm" data-asset="search">重试</button>';
  }
  if (state === 'degraded') {
    /* ★ 红向修正：降级态以前会落到 empty 分支里说「确实没有命中」，
       那恰好把「查不了」糊成「没有」——就是 09-22 那族静默不可用。 */
    return '<div class="hint" style="color:var(--warn)"><b>部分后端不可用</b>：'
      + '下面的清单<b>不完整</b>，不能当作「没有资产」。 <button class="btn ghost sm" data-asset="search">重试</button></div>'
      + (d && d.note ? '<div class="hint" style="color:var(--warn)">' + escapeHtml(d.note) + '</div>' : '')
      + (d ? backendLedger(d) : '')
      + (d && src.items(d).length ? itemListHtml(src, src.items(d))
                                  : '<div class="hint">可用后端本次未返回条目。</div>');
  }
  if (state === 'empty') {
    return (d && d.note ? '<div class="hint" style="color:var(--warn)">' + escapeHtml(d.note) + '</div>' : '')
      + backendLedger(d) +
      '<div class="hint">后端全部在线，这条改写查询确实没有命中 —— 换个说法再试。</div>';
  }
  const items = d ? src.items(d) : [];
  return (d && d.note ? '<div class="hint" style="color:var(--warn)">' + escapeHtml(d.note) + '</div>' : '')
    + backendLedger(d) + itemListHtml(src, items);
}

/* 逐路 backends[] 台账：路名 + 成败 + 条数 + 耗时 + 错因（错因截 60 字防溦屏） */
function backendLedger(d) {
  const backs = ((d && d.backends) || []).map(b =>
    '<span class="hint" style="margin-right:8px">' + escapeHtml(b.name) +
    (b.ok ? '✓' : '✗') + ' ' + (b.count == null ? '-' : b.count) + '条 ' + (b.ms == null ? '' : b.ms + 'ms') +
    (b.ok ? '' : (' <span style="color:var(--danger-text)">' + escapeHtml(String(b.error || '')).slice(0, 60) + '</span>')) +
    '</span>').join('');
  return backs ? '<div style="margin-bottom:6px">' + backs + '</div>' : '';
}

function itemListHtml(src, items) {
  if (!items || !items.length) return '<div class="hint">本次无条目。</div>';
  return '<div class="mem-list">' + items.map(it =>
    '<div class="mem-item"><p>' + escapeHtml(String(src.itemText(it)).slice(0, 300)) +
    (src.itemMeta(it) ? '<br><span class="hint">' + escapeHtml(src.itemMeta(it)) + '</span>' : '') +
    '</p></div>').join('') + '</div>';
}

async function loadAssetStatus() {
  const box = $('assetStatus');
  if (!box) return;
  const probes = [
    { label: '技能', path: '/api/skill/status', kind: 'status' },
    { label: '知识', path: '/api/kb/status', kind: 'status' },
    { label: '记忆', path: '/health', kind: 'health' },
  ];
  const settled = await Promise.allSettled(probes.map(p => api(p.path)));
  const chips = settled.map((r, i) => {
    const p = probes[i];
    if (r.status === 'rejected') {
      const st = /HTTP 404/.test(String(r.reason.message)) ? 'down' : 'degraded';
      return assetBadge({ label: p.label }, st, String(r.reason.message).slice(0, 60));
    }
    const d = r.value || {};
    if (p.kind === 'health') {
      const mb = d.memory_backend || {};
      const st = mb.state === 'ok' ? 'ok' : (mb.state ? 'degraded' : 'down');
      return assetBadge({ label: '记忆' }, st,
        mb.state ? (mb.state + ' L1=' + mb.l1_total + ' L0=' + mb.l0_total + (mb.stale ? ' stale' : '')) : '字段缺失（进程旧于代码）');
    }
    /* ★ 关键区分：`available:false` 开 **带 why** 的是设汁裁定（如 kb 的 wigolo、skill 的 TDAI 注册表），
       不是故障；带 error 的才是真挂了。把前者涂成黄色告警，首屏就是一屏假故障——
       那会把真故障埋掉（09-22 “静默不可用”的反面：吼叫式假告警）。 */
    const off = Object.values(d).filter(v => v && typeof v === 'object' && v.available === false);
    const unwired = off.filter(v => v.why && !v.error);
    const broken = off.filter(v => !(v.why && !v.error));
    const st = broken.length ? 'degraded' : (unwired.length ? 'unwired' : 'ok');
    const why = (broken.length ? broken.map(v => v.error || '未说明') : unwired.map(v => v.why))
      .join('；').slice(0, 90);
    return assetBadge({ label: p.label }, st, off.length ? ((broken.length ? '不可用：' : '原因：') + why) : '');
  });
  /* v0.13.40：align-items:flex-start —— 三个徽标的 note 长短差很多（kb 那条 90+ 字），
     不拉伸才能让每块只占自己内容的高度，否则短的那块（技能）下面拖一大片空白。 */
  box.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start">' + chips.join('') + '</div>';
}

async function runAssetSearch() {
  const q = (($('assetQ') || {}).value || '').trim();
  const hint = $('assetHint');
  if (hint) { hint.textContent = '检索中…'; }
  const settled = await Promise.allSettled(ASSET_SOURCES().map(s => {
    // 空查询时，需要 q 的那两路**根本不发请求**（而是置 idle）
    if (!q && s.needsQ) return Promise.resolve({ idle: true });
    return api(s.path(q));
  }));
  let degraded = 0;
  settled.forEach((r, i) => {
    const src = ASSET_SOURCES()[i];
    const box = $(src.box);
    if (!box) return;
    let state, d = null, err = null;
    if (r.status === 'fulfilled' && r.value && r.value.idle) {
      state = 'idle'; box.innerHTML = assetIdleHtml(src);
    } else if (r.status === 'rejected') {
      err = r.reason; state = assetStateOf(null, err);
      box.innerHTML = assetResultsHtml(src, null, state, err);
    } else {
      d = r.value; state = assetStateOf(d, null);
      box.innerHTML = assetResultsHtml(src, d, state, null);
    }
    if (state === 'degraded' || state === 'down') degraded++;
  });
  if (hint) {
    if (!q) {
      hint.textContent = '未带检索词：记忆/技术文档两路置为待输入（它们缺 q 会回 422），技能路已列前 6 条';
    } else {
      hint.textContent = degraded
        ? ('完成：' + degraded + '/3 路有降级，见对应列的红/黄字')
        : '完成：三路均正常';
    }
  }
}

function initAssetPanel() {
  if (assetsBound) return;
  assetsBound = true;
  const root = $('page-assets');
  if (!root) return;
  // data-asset 委托：唯一出口，侧栏/菜单内零 inline onclick（浮层三条之配套红线）
  root.addEventListener('click', e => {
    const b = e.target.closest('[data-asset]');
    if (!b) return;
    if (b.dataset.asset === 'search') runAssetSearch();
    else if (b.dataset.asset === 'refresh') loadAssets();
  });
  const inp = $('assetQ');
  if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') runAssetSearch(); });
}

async function loadAssets() {
  initAssetPanel();
  const sb = $('assetStatus');
  if (sb) sb.innerHTML = '<span class="hint">门面健康自检中…</span>';
  await loadAssetStatus();
  runAssetSearch();
}
