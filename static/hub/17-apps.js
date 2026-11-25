/* 17-apps.js — 「应用」（CLI Apps）页 · 2026-11-25
 *
 * 数据源：`/api/apps`（目录+已装）、`/api/apps/installed`、`/api/apps/health`。
 * 实现在 hub 的 `src/api_apps.py`，后端是 vendored 的 nanobot 应用模块（字节级副本）。
 *
 * 纪律：
 * ① 只读渲染走 api()；所有写动作（装/卸/更新/测试/运行）都**先弹确认**——
 *    它们会真的跑 npm/pip 装包或起子进程，误点一次就是全机环境变化；
 * ② 装完的「技能桥」结果必须显示出来（`skill_bridge.linked=false` 是**降级不是失败**，
 *    藏起来就等于让 agent 静默找不到这个应用，判例 268）；
 * ③ 契约只有 `/api/apps*` 这几个端点，别在前端拼第二条数据源。
 */
/* 用 var 而不是 let：go()/initSidebar() 在更早的分片里就会读到这三个名字，
   let 会构成 TDZ（tests/test_tdz_order.py 判据），var 提升后是 undefined 而不是崩。 */
var appsLoaded = false;
var appsRows = [];
var appsOnlyInstalled = false;
var appsHealth = null;

function appsEsc(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => (
    { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

async function loadApps(force) {
  const hint = $('appsHint');
  const body = $('appsBody');
  if (!body) return;
  if (force) appsOnlyInstalled = false;
  if (hint) hint.textContent = '加载目录…';
  try {
    const [cat, health] = await Promise.all([
      api(appsOnlyInstalled ? '/api/apps/installed' : '/api/apps'),
      appsHealth ? Promise.resolve(appsHealth) : api('/api/apps/health').catch(() => null),
    ]);
    appsHealth = health;
    appsRows = (cat && cat.apps) || [];
    appsRender(cat);
  } catch (e) {
    if (hint) hint.textContent = '目录加载失败：' + (e && e.message ? e.message : e);
    body.innerHTML = '';
  }
}

function appsRender(cat) {
  const hint = $('appsHint');
  const body = $('appsBody');
  if (!body) return;
  const q = ($('appsQ') && $('appsQ').value || '').trim().toLowerCase();
  const rows = appsRows.filter(r => {
    if (appsOnlyInstalled && !r.installed) return false;
    if (!q) return true;
    return fuzzyMatch([[r.name, 3], [r.display_name, 2], [r.description, 1]], q) > 0;
  });
  const installed = appsRows.filter(r => r.installed).length;
  if (hint) {
    const h = appsHealth || {};
    const notes = (h.notes || []).join('；');
    hint.textContent = (cat && cat.catalog_updated_at ? '目录缓存 ' + cat.catalog_updated_at + ' · ' : '')
      + '共 ' + appsRows.length + ' 条 / 已装 ' + installed
      + (h.yaml === false ? ' · ⚠ PyYAML 缺失（技能用 fallback 模板）' : '')
      + (notes ? ' · ' + notes : '');
  }
  if (!rows.length) {
    body.innerHTML = '<tr><td colspan="5" class="hint">'
      + (q ? '没有匹配「' + appsEsc(q) + '」的应用' : '目录为空：先点「刷新目录」拉一次远程注册表')
      + '</td></tr>';
    return;
  }
  body.innerHTML = rows.map(r => {
    const name = String(r.name || '');
    const ins = !!r.installed;
    const btns = ins
      ? '<button class="btn ghost sm" data-appact="run" data-app="' + appsEsc(name) + '">运行</button>'
        + '<button class="btn ghost sm" data-appact="test" data-app="' + appsEsc(name) + '">测试</button>'
        + '<button class="btn ghost sm" data-appact="update" data-app="' + appsEsc(name) + '">更新</button>'
        + '<button class="btn ghost sm" data-appact="uninstall" data-app="' + appsEsc(name) + '">卸载</button>'
      : '<button class="btn ghost sm" data-appact="install" data-app="' + appsEsc(name) + '">安装</button>';
    return '<tr>'
      + '<td>' + appsEsc(r.display_name || name) + '<div class="hint" style="font-size:11px">' + appsEsc(name) + '</div></td>'
      + '<td>' + appsEsc(r.category || '-') + '</td>'
      + '<td>' + (ins ? '已装' : '-') + '</td>'
      + '<td class="hint" style="max-width:320px">' + appsEsc((r.description || '').slice(0, 110)) + '</td>'
      + '<td style="white-space:nowrap">' + btns + '</td>'
      + '</tr>';
  }).join('');
}

function appsOut(text) {
  const el = $('appsOut');
  if (!el) { toast(text, 'ok'); return; }
  el.style.display = '';
  el.textContent = text;
}

async function appsAct(name, act) {
  if (!name) return;
  if (!window.confirm(appsConfirmText(name, act))) return;
  appsOut('[' + act + '] ' + name + ' …（最长 5 分钟，别关页面）');
  try {
    let data;
    if (act === 'run') {
      const raw = ($('appsArgs') && $('appsArgs').value || '').trim();
      data = await api('/api/apps/' + encodeURIComponent(name) + '/run',
        { method: 'POST', body: JSON.stringify({ args: raw ? raw.split(/\s+/) : [] }) });
      appsOut(data.output || '(无输出)');
      return;
    }
    data = await api('/api/apps/' + encodeURIComponent(name) + '/' + act, { method: 'POST' });
    const bridge = data && data.skill_bridge;
    const msg = (data && data.last_action && data.last_action.message) || (act + ' 完成');
    appsOut(msg + '\n' + (bridge
      ? '技能桥：' + (bridge.linked ? '已链接 ' : '未链接 ') + (bridge.link || '') + ' — ' + (bridge.reason || '')
      : '技能桥：本次响应未包含'));
    await loadApps(false);
  } catch (e) {
    appsOut('[' + act + '] 失败：' + (e && e.message ? e.message : e));
  }
}

function appsConfirmText(name, act) {
  const map = {
    install: '装 ' + name + '：会真的执行 npm/pip 全局安装，改全机环境。继续？',
    uninstall: '卸 ' + name + '：会卸包 + 删技能目录 + 收软链，不可自动回滚。继续？',
    update: '更新 ' + name + ' 到注册表最新版？',
    test: '对 ' + name + ' 跑一次 `--help` 验证可用性？',
    run: '用上方参数运行 ' + name + '？（工作目录锁在工作区内）',
  };
  return map[act] || (act + '：' + name + '？');
}

function appsToggleInstalled() {
  appsOnlyInstalled = !appsOnlyInstalled;
  const b = $('appsOnlyBtn');
  if (b) b.textContent = appsOnlyInstalled ? '看全部' : '只看已装';
  loadApps(false);
}

async function appsBridge() {
  try {
    const d = await api('/api/apps/bridge', { method: 'POST' });
    appsOut('技能桥重扫：' + (d.count || 0) + ' 个已装应用\n'
      + (d.items || []).map(i => i.name + ' → ' + (i.linked ? 'ok' : i.reason)).join('\n'));
    await loadApps(false);
  } catch (e) {
    appsOut('重扫失败：' + (e && e.message ? e.message : e));
  }
}

/* 事件委托：表体按钮与顶栏按钮都走 data-appact，不写 inline onclick
   （侧栏/表单内 inline onclick 会旁路委托，判例 39）。 */
document.addEventListener('click', function (ev) {
  const t = ev.target && ev.target.closest ? ev.target.closest('[data-appact]') : null;
  if (!t) return;
  const act = t.getAttribute('data-appact');
  const name = t.getAttribute('data-app');
  if (act === 'bridge') { appsBridge(); return; }
  if (act === 'refresh') { loadApps(true); return; }
  if (act === 'only') { appsToggleInstalled(); return; }
  appsAct(name, act);
});
