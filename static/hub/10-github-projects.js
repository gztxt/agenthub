/* ── GitHub 项目页（v0.13.31）────────────────────────────────────────
 * 分片头注释（军规：分片源，不是 build 产物；产物在 static/hub.js 由
 * scripts/build_hubjs.sh 按字典序拼接，直接改产物会被 test_hubjs_split 判红）。
 *
 * 数据源：GET /api/github/repos（远端清单 + strict remote 本地匹配，
 * 只读不鉴权；token/token 失效由后端信封 errors 点名——「查不了」≠「没有」）。
 * v0.13.33 缓存裁定（用户：「拉取一次后就在本地缓存，每次拉取就是浪费资源；
 * 只有选中后进入编辑状态之前再次拉取同步；总目录是手动刷新」）：
 *   · 列表：服务端内存缓存**拉一次永久有效**（重启自然清空）；进页/过滤/翻看
 *     永不重拉；「刷新」按钮（force=1）是唯一整表重拉入口；
 *   · 选中（ghSelect）→ ghSyncCheck 打一次 /api/github/sync 单仓核对
 *     （1 次 git ls-remote 比对 HEAD），结果只作 #ghMeta 提示，不打断。
 * fork 默认隐藏（34 个 fork 多为别人的仓库），#ghForks 勾选即显。
 * 新建会话（ghStart）两段式：
 *   ① local.found → 与本机项目页同路：POST /api/term/sessions {agent_id, cwd}；
 *   ② 本地无 → POST /api/github/clone {repo}（按钮禁用 + 克隆中 toast）
 *      → 拿 d.path 作 cwd → 走同一条会话链 → gotoChat('term') + termConnect。
 * v0.13.32 行内动作（同 09 分片）：
 *   收藏（★）——ghStars（localStorage，键 = full_name）置顶 + 行首高亮；
 *   隐藏——ghHiddenSet（localStorage）；勾选 #ghHidden「显示隐藏」才回列（半透明）。
 * 状态变量用 var：go()（01 分片，拼接序在前）会经顶层 go(lsGet('hub.page'))
 * 同步走到本分片函数体，let 的 TDZ 静态序风险会被 test_tdz_order 判红——
 * 08/09 分片同教训。localStorage 一律走 lsGet/lsSet 守卫（test_ls_guard R1）。 */

var GH = [];          // 全量远端仓库（过滤前缓存）
var ghSel = null;     // 当前选中索引（null = 未选）
var ghSelName = '';
/* ghLoaded 的声明在 01-core-boot.js（**只此一处**）：理由同 09 分片的 lpLoaded ——
   在此处再写一份 `var ghLoaded = false` 会把 06 顶层 go() 置上的懒加载标志重置回 false。 */

/* v0.13.32 收藏/隐藏状态（同 09 的 _lpSet 形态；解析失败按空集，不打断渲染）。 */
function _ghSet(key) {
  try { return new Set(JSON.parse(lsGet(key) || '[]')); } catch (e) { return new Set(); }
}
var ghStars = _ghSet('hub.gh.stars');
var ghHiddenSet = _ghSet('hub.gh.hidden');

/* v0.13.101 删除墓碑（与 09 的 lpTomb 同型同理由）：并集同步的配套，
   防止「取消收藏」在下次同步时被服务端旧值复活。同 09：墓碑只在本机、不上传。 */
var ghTomb = _ghSet('hub.gh.tomb');

function _ghTombAdd(key) {
  ghTomb.add(key);
  lsSet('hub.gh.tomb', JSON.stringify([...ghTomb]));
}
function _ghTombDrop(key) {
  ghTomb.delete(key);
  lsSet('hub.gh.tomb', JSON.stringify([...ghTomb]));
}
function _ghAdopt(serverSet, localSet) {
  serverSet.forEach(function (k) {
    if (localSet.has(k)) { ghTomb.delete(k); return; }
    if (ghTomb.has(k)) return;
    localSet.add(k);
  });
  lsSet('hub.gh.tomb', JSON.stringify([...ghTomb]));
}

function _ghSave(key, set) {
  lsSet(key, JSON.stringify([...set]));
}

/* v0.13.36 收藏/隐藏落服务端（同 09 分片 lpSyncPrefs/lpPushPref 的 gh 对称版）。
   v0.13.101 同步修掉与09 完全同型的两处缺陷（用户 2026-10-09 报障
   「重启后又显示没有收藏」，红向取证 work/probe-lp-sync-once.py）：
     ① 一次性闸门 + 静默 catch ⇒ 一次失败后本页面终身不再拉（成功后才置位）；
     ② 「本地非空就整份推上去」是以过时子集**覆盖**服务端（同型代码必须同型修，
        两页各自一套同步逻辑正是同型缺陷容易漏改的地方）。 */
var ghPrefSynced = false;
var ghPrefErrShown = false;
var ghPrefLastErr = '';

function ghPrefFail(why) {
  ghPrefLastErr = why || '未知原因';
  ghPrefErrShown = true;
}

function ghCountsText() {
  return (ghStars.size ? ' · 收藏 ' + ghStars.size : '') +
    (ghHiddenSet.size ? ' · 已隐藏 ' + ghHiddenSet.size : '') +
    (ghPrefErrShown ? ' · ⚠ 云端同步失败（' + escapeHtml(ghPrefLastErr) + '，仅本机存档）' : '');
}

async function ghSyncPrefs() {
  if (ghPrefSynced) return;
  try {
    const d = await api('/api/prefs/projects.gh');
    ghPrefSynced = true;              /* 成功后才置位（缺陷①修法） */
    ghPrefErrShown = false;
    ghPrefLastErr = '';
    if (d && d.value) {
      const serverStars = new Set(d.value.stars || []);
      const serverHidden = new Set(d.value.hidden || []);
      const before = serverStars.size + serverHidden.size;
      /* 以服务端为基准做并集（缺陷②修法），墓碑剔除本机显式删过的项。 */
      _ghAdopt(serverStars, ghStars);
      _ghAdopt(serverHidden, ghHiddenSet);
      _ghSave('hub.gh.stars', ghStars);
      _ghSave('hub.gh.hidden', ghHiddenSet);
      ghRenderList();
      if (before !== serverStars.size + serverHidden.size
          || ghStars.size !== serverStars.size || ghHiddenSet.size !== serverHidden.size) {
        ghPushPref();
      }
      const hint = $('ghHint');
      if (hint && GH.length) hint.textContent = GH.length + ' 个仓库' + ghCountsText();
    }
  } catch (e) {
    ghPrefFail((e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败'));
    const hint = $('ghHint');
    if (hint && GH.length) hint.textContent = GH.length + ' 个仓库' + ghCountsText();
  }
}

async function ghPushPref() {
  try {
    await api('/api/prefs/projects.gh', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stars: [...ghStars], hidden: [...ghHiddenSet] }) });
    ghPrefErrShown = false;
    ghPrefLastErr = '';
  } catch (e) {
    const why = (e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败');
    const first = !ghPrefErrShown;
    ghPrefFail(why);
    if (first) toast('收藏/隐藏已存本机，云端同步失败（' + why + '）', 'err');
    const hint = $('ghHint');
    if (hint && GH.length) hint.textContent = GH.length + ' 个仓库' + ghCountsText();
  }
}

function ghTime(iso) {
  if (!iso) return '';
  return String(iso).slice(0, 10);
}

function ghRowHtml(r, i) {
  const on = i === ghSel ? ' on' : '';
  const starred = ghStars.has(r.full_name);
  const hidden = ghHiddenSet.has(r.full_name);
  const tags = (r.fork ? '<span class="tag">fork</span>' : '') +
               (r.language ? '<span class="tag agent">' + escapeHtml(r.language) + '</span>' : '') +
               (r.archived ? '<span class="tag">archived</span>' : '');
  const local = (r.local && r.local.found)
    ? '<span class="tag" style="align-self:center">本地已有</span>'
    : '<span class="hint" style="align-self:center">本地无</span>';
  const path = (r.local && r.local.path)
    ? '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' +
      escapeHtml(String(r.local.path).slice(0, 72)) + '</span>' : '';
  const acts = '<span class="nav-acts" style="display:inline-flex;gap:2px;align-self:center">' +
    '<span class="act-btn" style="' + (starred ? 'color:var(--warn,#d90)' : '') + '"' +
    ' data-gh-star="' + i + '"' +
    ' title="' + (starred ? '取消收藏' : '收藏——置顶排序') + '">' + ico('star') + '</span>' +
    '<span class="act-btn" data-gh-hide="' + i + '"' +
    ' title="' + (hidden ? '取消隐藏' : '隐藏——不再显示（可勾选顶部「显示隐藏」找回）') + '">' +
    ico('eye') + '</span></span>';
  return '<div class="mem-item' + on + '" data-i="' + i +
    '" style="gap:6px;cursor:pointer' + (hidden ? ';opacity:.45' : '') + '"' +
    ' data-gh-row="' + i + '"' +
    ' title="' + escapeHtml(r.full_name || '') + '">' +
    '<p style="min-width:0">' + (starred ? '<span style="color:var(--warn,#d90)">★ </span>' : '') +
    '<b>' + escapeHtml(r.name || '') + '</b>' + tags +
    (r.pushed_at ? ' <span class="hint">' + ghTime(r.pushed_at) + '</span>' : '') + path + '</p>' +
    local + acts + '</div>';
}

function ghToggleStar(i) {
  const r = GH[i];
  if (!r) return;
  const fn = r.full_name;
  if (ghStars.has(fn)) { ghStars.delete(fn); _ghTombAdd(fn); }
  else { ghStars.add(fn); _ghTombDrop(fn); }
  _ghSave('hub.gh.stars', ghStars);
  ghRenderList();
  ghPushPref();
}

function ghToggleHide(i) {
  const r = GH[i];
  if (!r) return;
  const fn = r.full_name;
  if (ghHiddenSet.has(fn)) { ghHiddenSet.delete(fn); _ghTombAdd(fn); }
  else {
    ghHiddenSet.add(fn);
    _ghTombDrop(fn);
    if (ghSel === i) { ghSel = null; ghSelName = ''; }   // 隐藏选中项即解除选中
  }
  _ghSave('hub.gh.hidden', ghHiddenSet);
  ghRenderList();
  ghPushPref();
  const hint = $('ghHint');
  if (hint && ghHiddenSet.size) hint.textContent += '（已隐藏 ' + ghHiddenSet.size + ' 项）';
}

function ghSelect(i) {
  const r = GH[i];
  if (!r) return;
  ghSel = i;
  ghSelName = r.full_name || r.name || '';
  const nameEl = $('ghSelName');
  if (nameEl) { nameEl.textContent = ghSelName; nameEl.title = ghSelName; }
  const btn = $('ghStartBtn');
  if (btn) {
    btn.disabled = false;
    btn.lastChild.textContent = (r.local && r.local.found) ? '新建会话' : '克隆并开会话';
  }
  ghRenderList();   // 重画选中态（.on）
  ghSyncCheck(r);   // v0.13.33：选中即核对（用户裁定「进入编辑状态之前才同步」）
}

/* v0.13.33 单仓核对：选中仓库时打一次 /api/github/sync（1 次 git ls-remote，
   绝不整表重拉）。结果只影响 #ghMeta 的一行提示（behind 时提示 git pull），
   不打断、不弹窗——列表本身永远吃缓存。 */
var ghSyncBusy = false;
async function ghSyncCheck(r) {
  if (ghSyncBusy || !r || !r.full_name) return;
  ghSyncBusy = true;
  const meta = $('ghMeta');
  const orig = meta ? meta.textContent : '';
  try {
    const d = await api('/api/github/sync?repo=' + encodeURIComponent(r.full_name));
    if (meta && d && d.note) {
      const mark = d.state === 'synced' ? '✓ ' : (d.state === 'behind' ? '⚠ ' : '');
      meta.textContent = mark + d.note + (orig ? ' ｜ ' + orig : '');
    }
  } catch (e) { /* 核对失败静默：提示不是关键路径 */ }
  ghSyncBusy = false;
}

function ghFilter() {
  /* 纯函数（可被探针抽出真代码跑）：fork 默认隐藏，勾选即显；
     隐藏行仅在 #ghHidden 勾选时回列。 */
  const showForks = !!($('ghForks') && $('ghForks').checked);
  const showHidden = !!($('ghHidden') && $('ghHidden').checked);
  return GH.map((r, i) => [r, i]).filter(([r]) =>
    (!r.fork || showForks) && (!ghHiddenSet.has(r.full_name) || showHidden));
}

function ghRenderList() {
  const box = $('ghList');
  if (!box) return;
  const q = ($('ghQ') ? $('ghQ').value.trim() : '').toLowerCase();
  /* 顺序：收藏置顶（保持原相对序）→ 未收藏。 */
  const pairs = ghFilter();
  const ordered = pairs.filter(([r]) => ghStars.has(r.full_name))
    .concat(pairs.filter(([r]) => !ghStars.has(r.full_name)));
  const idx = ordered.filter(([r]) =>
    !q || fuzzyMatch([[r.name, 3], [r.full_name, 1]], q) > 0);
  box.innerHTML = idx.map(([r, i]) => ghRowHtml(r, i)).join('') ||
    '<div class="hint" style="padding:10px">' + (q ? '无匹配仓库' : '无仓库') + '</div>';
  const hint = $('ghHint');
  if (hint) hint.textContent = '显示 ' + idx.length + ' / ' + GH.length + ' 个仓库' + ghCountsText();
}

function ghRenderAgents() {
  const sel = $('ghAgent');
  if (!sel) return;
  const cur = sel.value;
  /* 同 09 分片：NO_TERM 实体排除（它的 term entry 起的是 Web 服务器，不是会话）。 */
  const items = AGENTS.filter(a => a.kind === 'agent' && !NO_TERM.has(a.id)
    && (a.entries || []).some(e => e.type === 'term'));
  /* v0.13.103：同 09 分片lpRenderAgents——AGENTS 未到时早退保占位，不清空 option。 */
  if (!items.length) return;
  sel.innerHTML = items.map(a =>
    '<option value="' + escapeHtml(a.id) + '">' + escapeHtml(a.name) + '</option>').join('');
  if (cur && items.some(a => a.id === cur)) sel.value = cur;   // 轮询重绘不覆盖用户选择
  else if (!cur && items.length) sel.value = items[0].id;
}

async function loadGithubRepos(force) {
  const box = $('ghList');
  const hint = $('ghHint');
  if (!box) return;
  if (force) GH = [];
  if (!GH.length) box.innerHTML = '<div class="hint" style="padding:10px">拉取 GitHub 仓库清单…</div>';
  if (hint) hint.textContent = '加载中…';
  try {
    const d = await api('/api/github/repos' + (force ? '?force=1' : ''));
    GH = d.repos || [];
    ghSel = null;                     // 重拉后旧选中索引失效
    ghRenderList();
    ghRenderAgents();
    if (hint) hint.textContent = (d.token === false ? 'token 不可用 · ' : '') +
      (d.count || 0) + ' 个仓库 · 本地已有 ' + (d.local_total || 0) + ' 个' + ghCountsText();
    ghSyncPrefs();
    const meta = $('ghMeta');
    /* v0.13.33 用户裁定「拉取一次后本地缓存，每次拉取就是浪费资源」：
       服务端内存缓存永久有效（重启才清），只有「刷新」按钮（force=1）强拉。
       此处如实显示缓存年龄，不催促重新拉取。 */
    if (meta) meta.textContent =
      (d.cached ? '缓存（' + (ageText(d.age_s)) + '前拉取，点「刷新」强制更新）' : '首次拉取') +
      ((d.errors || []).length ? ' ｜ ' + d.errors.join('；') : '') +
      (d.took_ms != null ? ' ｜ ' + d.took_ms + 'ms' : '');
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
    box.innerHTML = '<div class="hint" style="padding:10px">加载失败：' + escapeHtml(e.message || '') +
      ' <button class="btn sm" onclick="loadGithubRepos(true)">重试</button></div>';
  }
}

function ageText(sec) {
  const s = Number(sec) || 0;
  if (s < 90) return s + 's ';
  if (s < 5400) return Math.round(s / 60) + ' 分钟 ';
  return Math.round(s / 3600) + ' 小时 ';
}

/* 核心：选中仓库 + agent → 拉起 pty 会话 → 跳该 agent 终端工作台。
   本地已有：lpStart 同路；本地无：先 POST /api/github/clone（即时同步），
   拿返回 path 作 cwd 再开会话。 */
async function ghStart() {
  const agent = $('ghAgent') ? $('ghAgent').value : '';
  const r = GH[ghSel];
  if (ghSel == null || !r || !agent) { toast('先选仓库与 Agent', 'err'); return; }
  const btn = $('ghStartBtn');
  let cwd = (r.local && r.local.found) ? r.local.path : null;
  try {
    if (!cwd) {
      if (btn) btn.disabled = true;
      toast('克隆中：' + (r.full_name || r.name) + '（首次可能较慢）…');
      const d = await api('/api/github/clone', {
        method: 'POST',
        headers: termHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ repo: r.full_name })
      });
      cwd = d.path;
      if (d.existed) toast('目录已存在（同仓），直接开会话', 'ok');
    }
    const s = await api('/api/term/sessions', { method: 'POST',
      headers: termHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ agent_id: agent, cwd: cwd }) });
    toast('已在 ' + (r.name || cwd) + ' 拉起 ' + agent + ' 终端会话', 'ok');
    gotoChat(agent, 'term');
    setTimeout(() => termConnect(s.session.id, agent, { user: true }), 100);
  } catch (e) {
    toast('新建会话失败：' + (e.message || ''), 'err');
  } finally {
    if (btn && ghSel != null && GH[ghSel]) btn.disabled = false;
  }
}

/* ── P1-17（2026-09-30）：行内动作按钮改事件委托，撤掉 inline onclick ──────────
   为什么必须改：inline `onclick` 旁路事件委托（浮层唯一性红线第 2 条配套条款）。
   行内按钮一旦自己 onclick，整段「点完收场」逻辑就被跳过 —— 抽屉/浮层开着的
   情况下点收藏或隐藏，浮层留在原地，第二个浮层会把正文和第一个一起压住。
   委托是**唯一出口**：所有分支都从这里过，stopPropagation 也在这一处统一做，
   不依赖每个渲染点记得写。 */
(function () {
  var box = document.getElementById('ghList');
  if (!box) return;
  box.addEventListener('click', function (ev) {
    var t = ev.target;
    while (t && t !== box) {
      var ds = t.dataset || {};
      if (ds.ghStar != null) { ev.stopPropagation(); ghToggleStar(Number(ds.ghStar)); return; }
      if (ds.ghHide != null) { ev.stopPropagation(); ghToggleHide(Number(ds.ghHide)); return; }
      if (ds.ghRow != null)  { ghSelect(Number(ds.ghRow)); return; }
      t = t.parentNode;
    }
  });
})();

