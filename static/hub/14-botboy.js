/* v0.13.170 · Botboy 页（系统菜单第 10 项）：DS Bot 团队的只读投影。
 *
 * 数据面唯一来源：GET /api/botboy/overview（服务端读 ~/.dsh/bot/state.json）。
 * 为什么不在前端直连 dsh：① dsh 是按需拉起、本机常 inactive；② dsh web 的鉴权 token
 * 是进程级随机值，前端拿不到也不该拿；③ 跨源 fetch 会被 CORS 挡。读落盘文件是同一份数据。
 *
 * 三种"没有"必须分开渲染（静默不可用是本仓最禁的失败形态）：
 *   ok=false          → 文件在但读不了/不是 JSON：把 code+message 原样打出来，别当空团队
 *   found=false       → 还没有 state.json：附安装态与建团队入口
 *   ok && found && 0  → 真·空团队（文件在、结构正常、一条都没有）
 */
/* ⚠ 必须是 var：本分片排在 01-core-boot.js 之后，而 go()/initSidebar() 在启动期就会
   走到 loadBotboy()。`let` 声明在拼接产物里晚于调用点 ⇒ TDZ（ReferenceError），
   这正是 tests/test_tdz_order.py 抓的那一类（red baseline 是 _navHtml）。 */
var bbBound = false;
var bbBusy = false;

function bbAgo(ms) {
  if (!ms) return '';
  const s = Math.max(0, Math.floor((Date.now() - ms) / 1000));
  if (s < 60) return s + ' 秒前';
  if (s < 3600) return Math.floor(s / 60) + ' 分钟前';
  if (s < 86400) return Math.floor(s / 3600) + ' 小时前';
  return Math.floor(s / 86400) + ' 天前';
}

function bbStamp(ms) {
  if (!ms) return '—';
  const d = new Date(ms), p = n => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function initBotboy() {
  if (bbBound) return;
  bbBound = true;
  const root = $('page-botboy');
  if (!root) return;
  // data-bb 委托：唯一出口，页内零 inline onclick（浮层/菜单三条的配套红线）
  root.addEventListener('click', e => {
    const b = e.target.closest('[data-bb]');
    if (b && b.dataset.bb === 'refresh') loadBotboy(true);
  });
}

function bbBotCard(b) {
  const main = b.main ? '<span class="s-badge running" title="Main Bot"></span><b>Main</b> ' : '';
  const meta = [b.model, b.effort, b.workDir ? '工作目录 ' + b.workDir : '']
    .filter(Boolean).map(escapeHtml).join(' · ');
  const active = b.lastActive
    ? '活跃 ' + escapeHtml(bbStamp(b.lastActive)) + '（' + escapeHtml(bbAgo(b.lastActive)) + '）· ' + b.exchanges + ' 条交接'
    : '暂无交接记录';
  return '<div class="sp-cell">' +
    '<div class="sp-sub">' + main + escapeHtml(b.name) + '</div>' +
    (b.role ? '<div class="hint">' + escapeHtml(b.role) + '</div>' : '') +
    (meta ? '<div class="hint">' + meta + '</div>' : '') +
    '<div class="hint">' + active + '</div>' +
    (b.createdAt ? '<div class="hint">建于 ' + escapeHtml(bbStamp(b.createdAt)) + '</div>' : '') +
    '</div>';
}

function bbRoomCard(r) {
  const members = r.members.map(m => escapeHtml(m.name)).join('、') || '（无成员）';
  const admin = r.adminName ? '主持 ' + escapeHtml(r.adminName) : '';
  const active = r.lastActive
    ? '活跃 ' + escapeHtml(bbStamp(r.lastActive)) + '（' + escapeHtml(bbAgo(r.lastActive)) + '）'
    : '暂无发言';
  return '<div class="sp-cell">' +
    '<div class="sp-sub">' + escapeHtml(r.name) + '</div>' +
    '<div class="hint">' + members + '</div>' +
    '<div class="hint">' + [r.memberCount + ' 人', admin, r.mode ? '模式 ' + escapeHtml(r.mode) : '']
      .filter(Boolean).join(' · ') + '</div>' +
    '<div class="hint">' + active + '</div>' +
    (r.notice ? '<div class="hint">公告：' + escapeHtml(r.notice) + '</div>' : '') +
    '</div>';
}

function bbRender(d) {
  const status = $('bbStatus'), hint = $('bbHint');
  const botsBox = $('bbBots'), roomsBox = $('bbRooms');
  const botCount = $('bbBotCount'), roomCount = $('bbRoomCount'), exBody = $('bbEx');

  // ① 读不了：原样给错误，不渲染成"空团队"
  if (!d.ok) {
    const err = d.error || {};
    if (status) status.innerHTML = '<span class="s-badge error"></span>state.json 不可读';
    if (hint) hint.innerHTML = '<b>' + escapeHtml(err.code || 'unknown') + '</b>：' +
      escapeHtml(err.message || '未知错误') + '<br>路径 <code>' + escapeHtml(d.state_path || '') + '</code>';
    if (botsBox) botsBox.innerHTML = '<div class="hint">不可判定（文件存在但解析失败）</div>';
    if (roomsBox) roomsBox.innerHTML = '<div class="hint">不可判定</div>';
    if (exBody) exBody.innerHTML = '<tr><td colspan="3" class="hint">不可判定</td></tr>';
    if (botCount) botCount.textContent = '—';
    if (roomCount) roomCount.textContent = '—';
    return;
  }

  // ② 还没装 / 还没建：说清楚下一步，别留白
  if (!d.found) {
    const inst = d.install || {}, profs = inst.profiles || {};
    const lines = Object.keys(profs).map(p => {
      const v = profs[p] || {};
      return p + '：' + Object.keys(v).map(k => k + (v[k] ? ' 已装' : ' 未装')).join(' · ');
    });
    if (status) status.innerHTML = '<span class="s-badge installed"></span>还没有团队文件';
    if (hint) hint.innerHTML = escapeHtml(d.hint || '') +
      (lines.length ? '<br>安装态 —— ' + lines.map(escapeHtml).join('；') : '') +
      '<br>装法：<code>dsh plugin --profile web add -w ds-bot</code>（远程入口另装 ' +
      '<code>ds-harness-remote@0.5.20</code>）；装好后在 dsh 里建 Main Bot，本页自动出现。' +
      '<br>路径 <code>' + escapeHtml(d.state_path || '') + '</code>';
    if (botsBox) botsBox.innerHTML = '<div class="hint">0 个 Bot（文件不存在，不是读取失败）</div>';
    if (roomsBox) roomsBox.innerHTML = '<div class="hint">0 个群聊</div>';
    if (exBody) exBody.innerHTML = '<tr><td colspan="3" class="hint">暂无交接</td></tr>';
    if (botCount) botCount.textContent = '0 个';
    if (roomCount) roomCount.textContent = '0 个';
    return;
  }

  // ③ 正常
  const c = d.counts || {};
  if (status) {
    status.innerHTML = '<span class="s-badge running"></span>' +
      'state.json 最后写入 ' + escapeHtml(bbStamp(d.updated_at)) +
      '（' + escapeHtml(bbAgo(d.updated_at)) + '）· revision ' + escapeHtml(String(d.revision)) +
      ' · ' + (c.bots || 0) + ' Bot（' + (c.mains || 0) + ' Main） / ' + (c.rooms || 0) + ' 群聊 / ' +
      (c.exchanges || 0) + ' 条交接';
  }
  if (hint) {
    const inst = d.install || {}, profs = inst.profiles || {};
    const has = Object.keys(profs).some(p => (profs[p] || {})['ds-bot']);
    hint.innerHTML = (has ? '' : '<b>未在 node_modules 里检测到 ds-bot</b>（本页只按文件投影，' +
      '数据可能来自另一个 DSH_HOME）。') +
      '路径 <code>' + escapeHtml(d.state_path || '') + '</code>';
  }

  if (botsBox) {
    botsBox.innerHTML = (d.bots || []).length
      ? d.bots.map(bbBotCard).join('')
      : '<div class="hint">团队是空的：state.json 存在但没有 Bot。</div>';
  }
  if (roomsBox) {
    roomsBox.innerHTML = (d.rooms || []).length
      ? d.rooms.map(bbRoomCard).join('')
      : '<div class="hint">还没有群聊（群聊由 dsh 侧创建，至少要两个 Bot）。</div>';
  }
  if (botCount) botCount.textContent = (d.bots || []).length + ' 个（' + (c.mains || 0) + ' Main）';
  if (roomCount) roomCount.textContent = (d.rooms || []).length + ' 个';
  if (exBody) {
    const list = d.exchanges || [];
    exBody.innerHTML = list.length
      ? list.map(e => '<tr>' +
          '<td class="hint">' + escapeHtml(bbStamp(e.time)) + '</td>' +
          '<td>' + escapeHtml(e.fromName || '用户') + ' → ' + escapeHtml(e.toName || '—') +
            (e.roomId ? ' <span class="hint">（群聊）</span>' : '') + '</td>' +
          '<td>' + escapeHtml(e.text || '') + '</td>' +
          '</tr>').join('')
      : '<tr><td colspan="3" class="hint">暂无交接</td></tr>';
  }
}

async function loadBotboy(force) {
  initBotboy();
  /* 不做"整会话只拉一次"的门：state.json 是活文件（dsh 那边随时在写），
     进页就该看到当前值。只挡并发重入。 */
  if (bbBusy) return;
  bbBusy = true;
  const hint = $('bbHint'), status = $('bbStatus');
  if (status) status.innerHTML = '<span class="hint">读取中…</span>';
  if (hint) hint.textContent = '';
  try {
    const d = await api('/api/botboy/overview?limit=30');
    bbRender(d);
    if (force) toast('已重读 DS Bot 团队文件', 'ok');
  } catch (e) {
    // 请求层失败（服务未重启到本版本 / 网络）与"文件坏"是两件事，分开说
    if (status) status.innerHTML = '<span class="s-badge error"></span>端点不可用';
    if (hint) hint.innerHTML = '读取失败：' + escapeHtml(e.message || String(e)) +
      '<br>若刚升级：多半是后端还没重启到本版本（<code>GET /api/botboy/overview</code> 404）。';
  } finally {
    bbBusy = false;
  }
}
