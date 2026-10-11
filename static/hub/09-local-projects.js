/* ── 本机项目页（v0.13.30）────────────────────────────────────────
 * 分片头注释（军规：分片源，不是 build 产物；产物在 static/hub.js 由
 * scripts/build_hubjs.sh 按字典序拼接，直接改产物会被 test_hubjs_split 判红）。
 *
 * 数据源：GET /api/localprojects（后端多根 git 扫描 + cloudcli auth.db 合并，
 * 只读不鉴权）。列表三态（busy/数据/失败含重试）；errors 里点名的降级源
 * 用 hint 展示——「查不了」不能糊成「没有」。
 * 新建会话：复用 startAgent 的链路（POST /api/term/sessions），仅多传 cwd；
 * 后端 _cwd_or_none 校验后只进 os.chdir，命令仍出自画像白名单。
 * v0.13.32 行内动作：
 *   收藏（★）——lpStars 集合（localStorage 持久，键 = 项目 path）；收藏行
 *   置顶（多条按原序在前），行首星标高亮，再点取消；
 *   隐藏（👁off 图标）——lpHiddenSet（localStorage 持久）；隐藏行不再出现，
 *   勾选顶部 #lpHidden「显示隐藏」才回列（回列时半透明以示状态）。
 * 状态变量用 var：go()（01 分片，拼接序在前）会经顶层 go(lsGet('hub.page'))
 * 同步走到本分片函数体，let 的 TDZ 静态序风险会被 test_tdz_order 判红——
 * 原 08-runlog 分片同教训（RL_FIRST/RL_CUR，v0.13.47 随运行日志页一并删除）。localStorage 一律走 lsGet/lsSet 守卫
 * （test_ls_guard R1：裸调用判红）。 */

var LP = [];         // 全量项目（过滤前的缓存）
var lpSel = '';      // 当前选中项目 path（'' = 未选）
var lpSelName = '';
/* lpLoaded 的声明在 01-core-boot.js（**只此一处**）：go() 在 06 顶层同步调用，
   若在此处再写一份 `var lpLoaded = false`，其初始化晚于 go() ⇒ 把懒加载标志
   重置回 false（2026-10-11 报障的掩盖因素）。见 01 的说明与静态闸门。 */

/* v0.13.32 收藏/隐藏状态：localStorage 里的 JSON 数组（path 列表）。
   解析失败（旧值形状漂移）按空集处理，绝不让坏存档打断渲染。 */
function _lpSet(key) {
  try { return new Set(JSON.parse(lsGet(key) || '[]')); } catch (e) { return new Set(); }
}
var lpStars = _lpSet('hub.lp.stars');
var lpHiddenSet = _lpSet('hub.lp.hidden');

/* v0.13.101 删除墓碑：并集同步的**必要配套**，不是可选优化。
   并集（服务端 ∪ 本地）解决了「旧子集抹小服务端」，但它自带一个副作用：
   用户在这台设备点「取消收藏」后，本地集合少了它、服务端还留着 ⇒
   下次同步的并集把它**复活**。不配墓碑就是拿一个 bug 换另一个 bug。
   墓碑记「这台设备明确删过哪些 key」，并集时据此把服务端那份也剔除，
   并让本次 PUT 把删除真正落到服务端。墓碑本身**只在本机localStorage**，
   不上传——它表达的是单机意图，别的设备不需要也不该继承它。
   只存 key（path 字符串），不存任何内容，与 lpStars 同口径。 */
var lpTomb = _lpSet('hub.lp.tomb');

function _lpTombAdd(key) {
  lpTomb.add(key);
  lsSet('hub.lp.tomb', JSON.stringify([...lpTomb]));
}
function _lpTombDrop(key) {
  lpTomb.delete(key);
  lsSet('hub.lp.tomb', JSON.stringify([...lpTomb]));
}
/* 服务端有、本地没有、且没有墓碑 ⇒ 真·新增，收进来并清掉可能残留的墓碑。 */
function _lpAdopt(serverSet, localSet) {
  serverSet.forEach(function (k) {
    if (localSet.has(k)) { lpTomb.delete(k); return; }
    if (lpTomb.has(k)) return;      /* 墓碑：这台设备删过，别复活 */
    localSet.add(k);
  });
  lsSet('hub.lp.tomb', JSON.stringify([...lpTomb]));
}

function _lpSave(key, set) {
  lsSet(key, JSON.stringify([...set]));
}

/* v0.13.36 收藏/隐藏落服务端（跨浏览器/端侧一致）。
   v0.13.101（用户 2026-10-09 报障「重启后又显示没有收藏」）改掉两处，
   根因与红向取证见 work/probe-lp-sync-once.py（A/B/C/D 四段，全 PASS）：

   ★ 缺陷①（用户所见的直接成因）：一次性闸门 + 静默 catch。
     原写法进门即 `lpPrefSynced = true`，GET 失败走**空catch** ⇒ 本页面生命周期内
     **永不再拉**。而 localStorage 按 origin 隔离（AGENTS.md 4.1⑤）⇒ 局域网 IP /
     Tailscale / 手机 WebView 各一套存档；只要那一套是空的（或被清过站点数据），
     一次网络抖动 / 一次重启期间的后端 502，就让它**永远显示零收藏**，
     刷新与页内来回导航都不自愈。
     修法：失败**不置位**（下次进页/重试自然重来），并把原因落到 hint 上——
     静默失败与「真的没有收藏」在屏幕上是同形的，用户分不出就不可能自查。

   ★ 缺陷②：以本地为准 ⇒ 过时子集覆盖服务端。
     原分支注释写「后台静默合并推送（并集）」，**但 lpPushPref 推的是本地全量**，
     是覆盖不是并集。审计时间线实测到多次条数下降（asset_audit projects.lp：
     10-09T06:42:14 stars=17 → 09:57:45 stars=15），即另一台设备的旧子集把
     服务端更大的集合**抹小了**。
     修法：同步时以**服务端为基准**做并集（本地独有补进去），并用
     **删除墓碑**（_lpAdopt + lpTomb）让「取消收藏」不会被并集复活；
     删除意图由 lpToggleStar 自己 PUT 上去。 */
var lpPrefSynced = false;
var lpPrefErrShown = false;
var lpPrefLastErr = '';

/* 同步失败时不置 lpPrefSynced，允许下次重试；错误落 var 供 hint 显示。 */
function lpPrefFail(why) {
  lpPrefLastErr = why || '未知原因';
  lpPrefErrShown = true;
}

function lpCountsText() {
  return (lpStars.size ? ' · 收藏 ' + lpStars.size : '') +
    (lpHiddenSet.size ? ' · 已隐藏 ' + lpHiddenSet.size : '') +
    (lpPrefErrShown ? ' · ⚠ 云端同步失败（' + escapeHtml(lpPrefLastErr) + '，仅本机存档）' : '');
}

async function lpSyncPrefs() {
  if (lpPrefSynced) return;
  try {
    const d = await api('/api/prefs/projects.lp');
    /* 成功后才置位——这是缺陷①的修法：一次失败不许让本页面终身不再拉。 */
    lpPrefSynced = true;
    lpPrefErrShown = false;
    lpPrefLastErr = '';
    if (d && d.value) {
      const serverStars = new Set(d.value.stars || []);
      const serverHidden = new Set(d.value.hidden || []);
      const before = serverStars.size + serverHidden.size;
      /* 缺陷②修法：以服务端为基准做**并集**（原来「本地非空就整份推上去」是覆盖），
         墓碑让本机显式删过的项不被服务端旧值复活。 */
      _lpAdopt(serverStars, lpStars);
      _lpAdopt(serverHidden, lpHiddenSet);
      _lpSave('hub.lp.stars', lpStars);
      _lpSave('hub.lp.hidden', lpHiddenSet);
      lpRenderList();
      /* 合并后与服务端有差异（含墓碑剔除）⇒ 补推，让服务端收敛到同一状态。 */
      if (before !== serverStars.size + serverHidden.size
          || lpStars.size !== serverStars.size || lpHiddenSet.size !== serverHidden.size) {
        lpPushPref();
      }
      const hint = $('lpHint');
      if (hint && LP.length) hint.textContent = LP.length + ' 个项目' + lpCountsText();
    }
  } catch (e) {
    lpPrefFail((e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败'));
    const hint = $('lpHint');
    if (hint && LP.length) hint.textContent = LP.length + ' 个项目' + lpCountsText();
  }
}

async function lpPushPref() {
  try {
    await api('/api/prefs/projects.lp', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stars: [...lpStars], hidden: [...lpHiddenSet] }) });
    lpPrefErrShown = false;
    lpPrefLastErr = '';
  } catch (e) {
    const why = (e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败');
    const first = !lpPrefErrShown;          /* toast 只响一次，hint 每次都更新 */
    lpPrefFail(why);
    if (first) toast('收藏/隐藏已存本机，云端同步失败（' + why + '）', 'err');
    const hint = $('lpHint');
    if (hint && LP.length) hint.textContent = LP.length + ' 个项目' + lpCountsText();
  }
}

function lpTime(la) {
  if (!la) return '';
  return String(la).slice(5, 16).replace('T', ' ');
}

/* 行内动作图标：act-btn（既有 CSS）。stopPropagation 防触发行选中。 */
function lpRowHtml(p, i) {
  const on = p.path === lpSel ? ' on' : '';
  const starred = lpStars.has(p.path);
  const hidden = lpHiddenSet.has(p.path);
  const src = (p.git ? '<span class="tag agent">git</span>' : '') +
              (p.cloudcli ? '<span class="tag">cc</span>' : '');
  const acts = '<span class="nav-acts" style="display:inline-flex;gap:2px;align-self:center">' +
    '<span class="act-btn" style="' + (starred ? 'color:var(--warn,#d90)' : '') + '"' +
    ' data-lp-star="' + i + '"' +
    ' title="' + (starred ? '取消收藏' : '收藏——置顶排序') + '">' + ico('star') + '</span>' +
    '<span class="act-btn" data-lp-hide="' + i + '"' +
    ' title="' + (hidden ? '取消隐藏' : '隐藏——不再显示（可勾选顶部「显示隐藏」找回）') + '">' +
    ico('eye') + '</span></span>';
  return '<div class="mem-item' + on + (hidden ? ' lp-hidden-row' : '') + '" data-i="' + i +
    '" style="gap:6px;cursor:pointer' + (hidden ? ';opacity:.45' : '') + '"' +
    ' data-lp-row="' + i + '"' +
    ' title="' + escapeHtml(p.path) + '">' +
    '<p style="min-width:0">' + (starred ? '<span style="color:var(--warn,#d90)">★ </span>' : '') +
    '<b>' + escapeHtml(p.name) + '</b>' + src +
    (p.sessions ? ' <span class="hint">' + p.sessions + ' 会话</span>' : '') +
    (p.last_activity ? ' <span class="hint">' + escapeHtml(lpTime(p.last_activity)) + '</span>' : '') +
    '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' +
    escapeHtml(String(p.path).slice(0, 72)) + '</span></p>' + acts + '</div>';
}

function lpToggleStar(i) {
  const p = LP[i];
  if (!p) return;
  if (lpStars.has(p.path)) {
    lpStars.delete(p.path);
    _lpTombAdd(p.path);       /* v0.13.101：取消收藏记墓碑，否则同步的并集会把它复活 */
  } else {
    lpStars.add(p.path);
    _lpTombDrop(p.path);      /* 重新收藏 ⇒ 撤墓碑，否则它永远进不来 */
  }
  _lpSave('hub.lp.stars', lpStars);
  lpRenderList();
  lpPushPref();
}

function lpToggleHide(i) {
  const p = LP[i];
  if (!p) return;
  if (lpHiddenSet.has(p.path)) {
    lpHiddenSet.delete(p.path);
    _lpTombAdd(p.path);
  } else {
    lpHiddenSet.add(p.path);
    _lpTombDrop(p.path);
    if (lpSel === p.path) { lpSel = ''; lpSelName = ''; }   // 隐藏选中项即解除选中
  }
  _lpSave('hub.lp.hidden', lpHiddenSet);
  lpRenderList();
  lpPushPref();
  const hint = $('lpHint');
  if (hint && lpHiddenSet.size) hint.textContent += '（已隐藏 ' + lpHiddenSet.size + ' 项）';
}

function lpSelect(i) {
  const p = LP[i];
  if (!p) return;
  lpSel = p.path;
  lpSelName = p.name;
  const nameEl = $('lpSelName');
  if (nameEl) { nameEl.textContent = p.name; nameEl.title = p.path; }
  const btn = $('lpStartBtn');
  if (btn) btn.disabled = false;
  lpRenderList();   // 重画选中态（.on）
}

function lpRenderList() {
  const box = $('lpList');
  if (!box) return;
  const q = ($('lpQ') ? $('lpQ').value.trim() : '').toLowerCase();
  const showHidden = !!($('lpHidden') && $('lpHidden').checked);
  /* 顺序：收藏置顶（保持原相对序）→ 未收藏；隐藏行仅在勾选「显示隐藏」时出现。 */
  const visible = LP.filter(p => !lpHiddenSet.has(p.path) || showHidden);
  const ordered = visible.filter(p => lpStars.has(p.path))
    .concat(visible.filter(p => !lpStars.has(p.path)));
  /* 2026-10-05：原 `LP.indexOf(p)` 在 map 里对每个项目做一次线性查找 ⇒ O(n²)。
   * 改一次性建 **path→下标** 的 Map。用 path 而不是对象引用作键：LP 来自后端
   * JSON 数组（`d.projects`），同一次渲染内对象引用稳定，但 path 才是项目的
   * 唯一标识（lpStars / lpHiddenSet 也都以 path 为键，口径一致）。
   * 若真出现重复 path，Map 保留**最后**一个下标，而 indexOf 给**第一个** ——
   * 这类重复本就不该存在（后端按目录枚举），且只影响列表里那一行的序号显示。 */
  const order = new Map(LP.map((p, i) => [p.path, i]));
  const idx = ordered.map(p => [p, order.get(p.path)])
    .filter(([p]) => !q || fuzzyMatch([[p.name, 3], [p.path, 1]], q) > 0);
  box.innerHTML = idx.map(([p, i]) => lpRowHtml(p, i)).join('') ||
    '<div class="hint" style="padding:10px">' + (q ? '无匹配项目' : '无项目') + '</div>';
}

function lpRenderAgents() {
  const sel = $('lpAgent');
  if (!sel) return;
  const cur = sel.value;
  const items = AGENTS.filter(a => a.kind === 'agent' && (a.entries || []).some(e => e.type === 'term'));
  /* v0.13.103：AGENTS 异步来（loadAgents），本函数在 loadLocalProjects 数据一到
     就同步跑 —— 清单未到时**早退保占位**，不把 innerHTML 清成 0 个 option
     （清空会让 select 塌成 46px 空白，且此后无人重画：2026-10-11 报障）。
     真实清单由 01 分片 loadAgents 的补画钩子补上。 */
  if (!items.length) return;
  sel.innerHTML = items.map(a =>
    '<option value="' + escapeHtml(a.id) + '">' + escapeHtml(a.name) + '</option>').join('');
  if (cur && items.some(a => a.id === cur)) sel.value = cur;   // 轮询重绘不覆盖用户选择
  else if (!cur && items.length) sel.value = items[0].id;
}

async function loadLocalProjects(force) {
  const box = $('lpList');
  const hint = $('lpHint');
  if (!box) return;
  if (force) LP = [];
  if (!LP.length) box.innerHTML = '<div class="hint" style="padding:10px">扫描本机项目中…</div>';
  if (hint) hint.textContent = '加载中…';
  try {
    const d = await api('/api/localprojects');
    LP = d.projects || [];
    lpRenderList();
    lpRenderAgents();
    if (hint) hint.textContent = (d.count || 0) + ' 个项目' + lpCountsText();
    lpSyncPrefs();
    const meta = $('lpMeta');
    if (meta) meta.textContent = (d.roots || []).join(' · ') +
      ((d.errors || []).length ? ' ｜ 降级源：' + d.errors.join('；') : '') +
      (d.took_ms != null ? ' ｜ ' + d.took_ms + 'ms' : '');
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
    box.innerHTML = '<div class="hint" style="padding:10px">加载失败：' + escapeHtml(e.message || '') +
      ' <button class="btn sm" onclick="loadLocalProjects(true)">重试</button></div>';
  }
}

/* 核心：选中项目 + agent → 拉起 pty 会话 → 跳该 agent 终端工作台。
   逐字仿 startAgent（05 分片），仅多传 cwd。 */
async function lpStart() {
  const agent = $('lpAgent') ? $('lpAgent').value : '';
  if (!lpSel || !agent) { toast('先选项目与 Agent', 'err'); return; }
  try {
    const d = await api('/api/term/sessions', { method: 'POST',
      headers: termHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ agent_id: agent, cwd: lpSel }) });
    toast('已在 ' + lpSelName + ' 拉起 ' + agent + ' 终端会话', 'ok');
    gotoChat(agent, 'term');
    setTimeout(() => termConnect(d.session.id, agent, { user: true }), 100);
  } catch (e) { toast('新建会话失败：' + e.message, 'err'); }
}

/* ── P1-17（2026-09-30）：行内动作按钮改事件委托，撤掉 inline onclick ──────────
   为什么必须改：inline `onclick` 旁路事件委托（浮层唯一性红线第 2 条配套条款）。
   行内按钮一旦自己 onclick，整段「点完收场」逻辑就被跳过 —— 抽屉/浮层开着的
   情况下点收藏或隐藏，浮层留在原地，第二个浮层会把正文和第一个一起压住。
   委托是**唯一出口**：所有分支都从这里过，stopPropagation 也在这一处统一做，
   不依赖每个渲染点记得写。 */
(function () {
  var box = document.getElementById('lpList');
  if (!box) return;
  box.addEventListener('click', function (ev) {
    var t = ev.target;
    while (t && t !== box) {
      var ds = t.dataset || {};
      if (ds.lpStar != null) { ev.stopPropagation(); lpToggleStar(Number(ds.lpStar)); return; }
      if (ds.lpHide != null) { ev.stopPropagation(); lpToggleHide(Number(ds.lpHide)); return; }
      if (ds.lpRow != null)  { lpSelect(Number(ds.lpRow)); return; }
      t = t.parentNode;
    }
  });
})();

