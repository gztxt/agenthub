async function registerAgent() {
  const dir = $('regDir').value.trim();
  if (!dir) return toast('请输入目录', 'err');
  try {
    const body = { dir: dir };
    if ($('regName').value.trim()) body.name = $('regName').value.trim();
    const d = await api('/api/agents', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast('已注册: ' + d.id, 'ok');
    closeRegister(); loadAgents();
  } catch (e) { toast(JSON.stringify(e.message), 'err'); }
}

/* ── 统一对话（三模式：embed 原生UI / term pty终端 / chat 对话框）── */

let chatPick = lsGet('hub.chat.pick') || 'claude';
/* v0.13.21 换键 hub.chatmode. → hub.chatmode2.：旧键里躺着的是**默认推导**被无差别落盘形成的
   假偏好（gotoChat 每次点击都写）。不换键，就算改了默认形态，已被旧值钉在嵌入页的浏览器仍进不去
   终端 —— 与 09-23「交互结果不得依赖存量」同源。新键只在用户**点名**形态时写。 */
const MODE_KEY = 'hub.chatmode2.';
// 上次**显式**选过的形态；空串=没选过，由 applyChatMode 走 defaultModeOf（唯一真源）
let chatMode = lsGet(MODE_KEY + chatPick) || '';
function sessKey(id) { return 'hub.sess.' + id; }

function entityById(id) { return AGENTS.find(a => a.id === id); }
/* 原生界面形态（统一对话页内，embed 面板）下的「CloudCLI 项目直达」面板。
   用户 2026-10-05 指令：CloudCLI 的独立原生界面从 Claude Code 卡里拆出，
   作为 Agents 菜单下独立子菜单（cloudcli）；claude 终端页保持纯终端，
   但**嵌进 CloudCLI 原生界面里**（本函数负责：渲染 embed 面板后把项目清单追加到
   iframe 下方，点「开始会话」直达对应项目的新会话，不再需要进 Claude Code 详情抽屉）。 */
function renderCloudcliProjects() {
  if (typeof loadCloudcliProjects !== 'function') return;
  const body = document.getElementById('embedBody');
  if (!body) {  // 降级：embedBody 尚未挂到 index.html（旧缓存）→ 走旧抽屉路径
    loadCloudcliProjects();
    return;
  }
  if (body.querySelector('#ccProjects')) return;   // 防重复渲染（切换 agent 多次）
  bindCloudcliDelegation();   // v0.13.131：行/按钮走 data-* 委托（见函数注释）
  const holder = document.createElement('div');
  holder.id = 'ccProjects';
  holder.innerHTML = '<div class="hint" style="margin:8px 10px 2px">CloudCLI 项目（本机全部 · 点「开始会话」直达）</div><div class="hint" style="padding:0 10px">加载中…</div>';
  body.appendChild(holder);
  api('/api/cloudcli/projects').then(d => {
    if (!d.ok) { holder.innerHTML = '<div class="hint" style="margin:8px 10px 2px">CloudCLI 项目</div>' +
      '<div class="hint" style="padding:0 10px;color:var(--st-error,var(--danger))">加载失败：' + escapeHtml(d.error || '?') + '</div>'; return; }
    const rows = (d.projects || []).map(p =>
      '<div class="mem-item" style="gap:6px;padding:6px 10px;border-bottom:1px solid var(--border);cursor:pointer" ' +
      'data-sk-ccstart="' + escapeHtml(p.path) + '">' +
      '<p style="min-width:0;margin:0"><b>' + escapeHtml(p.name) + '</b>' + (p.starred ? ' ★' : '') +
      (p.sessions ? ' <span class="hint">' + p.sessions + ' 会话</span>' : '') +
      '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' +
      escapeHtml(String(p.path).slice(0, 60)) +
      (p.last_activity ? ' · ' + String(p.last_activity).slice(5, 16).replace('T', ' ') : '') + '</span></p>' +
      '<button class="btn sm" style="align-self:center">▶ 开始会话</button></div>').join('');
    holder.innerHTML = '<div class="hint" style="margin:8px 10px 2px">CloudCLI 项目（' + (d.count || 0) + ' 个 · 点行或「开始会话」直达）</div>' +
      '<div class="tscroll" style="max-height:220px;overflow-y:auto">' + (rows || '<div class="hint" style="padding:0 10px">无项目</div>') + '</div>';
  }).catch(e => {
    holder.innerHTML = '<div class="hint" style="margin:8px 10px 2px">CloudCLI 项目</div>' +
      '<div class="hint" style="padding:0 10px;color:var(--st-error,var(--danger))">加载失败：' + escapeHtml(e.message || '') + '</div>';
  });
}
/* v0.13.131（PT-20261019-02）：CloudCLI 项目行/按钮改 data-* + 委托。
 * 原 `onclick="cloudcliStart(<jsStr(path)>)"` 被双引号属性提前截断 ⇒ 静默 SyntaxError。
 * 行与按钮同值 ⇒ closest() 取最近祖先，点按钮也只触发一次，**不再需要 stopPropagation**。
 * #embedBody 是静态容器（holder 每次重渲染不换壳），监听只挂一次。 */
function bindCloudcliDelegation() {
  const el = document.getElementById('embedBody');
  if (!el || el.__ccDeleg) return;
  el.__ccDeleg = true;
  el.addEventListener('click', e => {
    const row = e.target.closest('[data-sk-ccstart]');
    if (row && el.contains(row)) {
      e.stopPropagation();
      cloudcliStart(row.dataset.skCcstart || '');
    }
  });
}

/* 工作台默认形态 —— **唯一真源**（openEntity 也走这里；两处各写一份优先级必然漂移）。
   有原生终端的 Agent 先给终端：它的独立 Web 宿主（claude←cloudcli :3010）自带一套登录，
   嵌进 hub 就是一张要重新登录的白页，而终端页里的 TUI 与本机命令行完全一致。
   宿主界面保留为可切换的第二形态（终端页头部「原生界面」按钮）。
   cloudcli 独立子菜单：无终端卡（纯服务型），默认形态 = embed（原生界面）+ 项目直达面板。
   dsh 默认 embed、但**保留**终端形态（2026-10-24 01:15 改判，见 profiles.py 同名注释）：
   DSH 本体不带终端前端，可 `dsh --profile tui`（外置插件 @tomowang/dsh-tui）就是全屏 TUI。
   裸 `dsh` 仍会变成 `dsh web`（启动器 `case "${1:-web}"`）⇒ 命令由后端画像钉死成
   `dsh --profile tui`，前端只按画像的 term entry 决定是否出「终端」按钮。
   早先的 NO_TERM 结论（点一次多起一个 dsh web、3081/3082 争写 ~/.dsh，判例 252 §四.2）
   对**裸 dsh** 仍然成立，所以机制保留、只是集合里不再有 dsh。 */
const EMBED_FIRST = new Set(['cloudcli', 'dsh']);
/* 没有 CLI 交互会话的实体 —— 「终端」形态对它们必然是死屏（不是"不好用"，是"没有这个东西"）。
   2026-10-24 01:15 起为空集：dsh 已有 TUI（dsh --profile tui）。 */
const NO_TERM = new Set();
function defaultModeOf(a) {
  const es = (a && a.entries) || [];
  const has = t => es.some(e => e.type === t);
  /* v0.13.151：EMBED_FIRST 加 has('embed') 前置。改前它无条件返回 'embed'，
     而 applyChatMode 的「形态不可用回落默认」又会再问一次 defaultModeOf ⇒ 若该实体
     的 embed entry 不在（frame_deny / 入口被过滤 / 画像改动），两步都落到 embed，
     用户点开的是一张 url 为空的空白嵌页，且没有按钮能出去。
     2026-10-25 报障「dsh的原生界面按钮也没有了」同源：后端入口恢复后这里是第二道闸。 */
  if (a && EMBED_FIRST.has(a.id) && has('embed')) return 'embed';
  if (a && !NO_TERM.has(a.id) && a.kind === 'agent' && has('term')) return 'term';
  if (has('embed')) return 'embed';
  if (has('term')) return 'term';
  return 'chat';
}
/* 用户明确的进入动作（点菜单行 / 点侧栏图标 / 点模式按钮）⇒ 允许在嵌入目标未运行时
   **自动拉起**。一次性标志：applyChatMode 消费后即清，后台定时刷新不会顺手起服务。 */
let _embedAutoStartPending = false;
function gotoChat(id, mode) {
  chatPick = id;
  lsSet('hub.chat.pick', id);  // T9：记忆上次实体
  const a = entityById(id);
  chatMode = mode || lsGet(MODE_KEY + id) || defaultModeOf(a) || 'chat';
  if (mode) lsSet(MODE_KEY + id, mode);   // 只有点名了形态才算偏好；推导出来的不写盘
  _embedAutoStartPending = true;
  go('chat');
  renderChatSide();
}

function renderChatSide() {
  const side = $('chatAgents');
  if (!AGENTS.length) { side.innerHTML = '<div class="hint" style="padding:10px">加载…</div>'; loadAgents().then(renderChatSide); return; }
  // 显示有Web UI可嵌入的实体（agent + 有embed entry的实体）
  const list = AGENTS.filter(a => a.kind === 'agent' || (a.entries || []).some(e => e.type === 'embed'));
  side.innerHTML = list.map(a => {
    const hasEmbed = (a.entries || []).some(e => e.type === 'embed');
    const badge = a.status === 'running' ? 'running' : (a.status === 'installed' ? 'installed' : 'stopped');
    const icon = hasEmbed ? ico('monitor', 'xs') : '';
    return '<div class="item' + (a.id === chatPick ? ' on' : '') + '" onclick="pickChatEntity(\'' + a.id + '\')">' +
      '<span>' + escapeHtml(a.name) + '</span>' + (icon ? '<span class="kindtag">' + icon + '</span>' : '') + '<span class="s-badge ' + badge + '" style="position:static"></span></div>';
  }).join('');
  applyChatMode();
}

function pickChatEntity(id) {
  chatPick = id;
  lsSet('hub.chat.pick', id);
  // T9：模式记忆优先——只认用户**显式**选过的形态（新键），没选过就走默认
  chatMode = lsGet(MODE_KEY + id) || defaultModeOf(entityById(id));
  _embedAutoStartPending = true;
  renderChatSide();
}

function applyChatMode(opts) {
  const a = entityById(chatPick);
  renderModeBar(a);
  const en = $('chatEntName');
  if (en) en.textContent = a ? a.name : '';
  if (!a) return;   // 实体已被删除（localStorage 里留着旧 pick）：保持默认面板，不再往下猜模式
  const es = a.entries || [];
  // 形态对该实体不可用（没选过、或 entry 被删/改）：回落默认形态。
  // 这是**推导**，绝不写盘 —— 一写就把默认固化成偏好，改默认也救不回来。
  if (!es.some(e => e.type === chatMode)) chatMode = defaultModeOf(a) || 'chat';
  // NO_TERM 的实体即便本地存着 'term' 偏好也当无效：那个形态本身就是死屏（见 NO_TERM 注释），
  // 尊重它就等于每次点开都在后台多起一个 dsh web —— 偏好不该能把用户带进已知坏形态。
  if (NO_TERM.has(chatPick) && chatMode === 'term') chatMode = defaultModeOf(a) || 'embed';
  // 面板头互切按钮：菜单行内的动作图标自 v0.12.3 起 display:none、模式 tab 也已停用，
  // 站内不留这条出口，embed 与 term 就互相锁死（点进哪个就再也切不到另一个）。
  const toTerm = $('embedToTerm'), toEmbed = $('termToEmbed');
  if (toTerm) toTerm.style.display = (!NO_TERM.has(chatPick) && es.some(e => e.type === 'term')) ? '' : 'none';
  if (toEmbed) toEmbed.style.display = es.some(e => e.type === 'embed') ? '' : 'none';
  $('embedPane').classList.toggle('on', chatMode === 'embed');
  $('termPane').classList.toggle('on', chatMode === 'term');
  $('chatPane').classList.toggle('on', chatMode === 'chat');
  if (chatMode === 'embed') {
    const e = (a.entries || []).find(x => x.type === 'embed');
    const url = entryUrl(e);
    $('embedTitle').textContent = a.name;   // 用户 09-20：删掉「原生界面」后缀（宽屏要把地址并进同一行，标题越短越好）
    // 地址行：#embedUrlHint 现为 <button><span>URL</span><svg/></button>，直接写 textContent 会把图标抹掉
    const hintEl = $('embedUrlHint');
    const hintTxt = hintEl ? hintEl.querySelector('span') : null;
    if (hintTxt) hintTxt.textContent = url; else if (hintEl) hintEl.textContent = url;
    const f = $('embedFrame');
    if (f.dataset.src !== url) { f.src = url; f.dataset.src = url; }
    probeEmbed(url, !!(opts && opts.auto));
    /* 独立 cloudcli 子菜单：embed 面板下挂 CloudCLI 项目直达（渲染到 #embedBody 槽位，
       旧缓存无槽位时 loadCloudcliProjects 兜底走 claude 详情抽屉旧路径）。 */
    if (chatPick === 'cloudcli') renderCloudcliProjects();
  } else if (chatMode === 'term') {
    $('termTitle').textContent = (a.name || chatPick);   // 用户 09-20：行首只留实体名，不加“· 终端会话”后缀，给芯片腾位
    ensureTerm();
    // ★ 修复核心：切换实体时解绑异主会话，画面不再残留上一个 Agent
    if (termSid && termSidAgent && termSidAgent !== chatPick) termDetach();
    termRefreshList().then(list => termAutoAttach(list));   // 清单直接接力给 autoAttach，一次 GET 就够
  } else {
    openChatSession();
    // 对话工具栏三联动：模型 / 工作目录 / 会话列表
    loadChatModels(false);
    chatCwdLoad();
    chatSessLoad();
  }
}
/* 模式切换栏 v0.7：右侧顶栏仅显示实体名，无分割线 */
function renderModeBar(a) {
  const bar = $('chatModeBar'), tabs = $('opTabs'), crumb = $('crumb');
  if (bar) bar.style.display = 'none';
  // 聊天页顶栏本来就是空的（模式 tab 已停用，实体名走 #chatEntName）：
  // 旧代码只在 !a 时清空，导致从其他页切进来时面包屑残留上一页标题（实测残留「总览」）。
  if (tabs) tabs.innerHTML = '';
  if (crumb) crumb.innerHTML = '';
  syncOpBar();
}
function switchMode(m) {
  /* NO_TERM 实体：终端形态不存在。回落到原生界面前先把话说清楚，
     否则点了没反应的按钮比没有按钮更耗人（2026-10-24 报障同款）。 */
  if (m === 'term' && NO_TERM.has(chatPick)) {
    toast('这个 Agent 没有命令行会话，交互都在「原生界面」');
    m = 'embed';
  }
  chatMode = m;
  lsSet(MODE_KEY + chatPick, m);  // T9：按实体记忆模式（这里是用户点名切换 ⇒ 算真偏好）
  /* v0.13.153：用户点「原生界面」＝明确的拉起意图。2026-10-24 起全 agent 按需拉起，
     停着是常态；只挂「目标界面未响应」再让用户点第二次「启动」正是 2026-10-25 报障
     「点击原生界面没有拉起」的原样。这里把意图传下去，死链时直接启动。 */
  applyChatMode({ auto: m === 'embed' });
}

/* 嵌入地址：默认按当前访问名改写（lanUrl），但 `pin_host` 的入口必须原样。
   dsh 只能 bind 具体网卡（192.168.5.102），lanUrl 会把地址改写成 127.0.0.1
   ⇒ 嵌页 ERR_CONNECTION_REFUSED（2026-10-25 影子实测）。改写到"当前访问名"
   只对绑 0.0.0.0 的 agent 成立。 */
function entryUrl(e) {
  if (!e) return '';
  return e.pin_host ? (e.url || '') : lanUrl(e.url);
}
/* 地址变了就跟进（由 loadAgents 在每次拿到新 /api/agents 后调用）。
   存在的理由：`applyChatMode` 只在 `dataset.src` ≠ 新地址时才设 `f.src`，而
   dsh/reasonix 的地址带**进程级 token** —— 服务重启后新地址来了却没人触发
   applyChatMode，帧里就一直停着旧 token 那份 401（用户看到 "dsh web
   authentication required"）。这里把「数据变了」翻译成「界面跟进」。 */
function syncEmbedUrlIfChanged() {
  if (!chatPick || chatMode !== 'embed') return;
  const a = entityById(chatPick);
  const e = a && (a.entries || []).find(x => x.type === 'embed');
  const f = $('embedFrame');
  if (!e || !f) return;
  const url = entryUrl(e);
  if (url && url !== f.dataset.src) applyChatMode();
}
/* 嵌入存活探测：no-cors fetch 失败=目标端口无响应 → 覆盖层引导切换 */
async function _embedAlive(url) {
  try {
    await Promise.race([
      fetch(url, { mode: 'no-cors', cache: 'no-store' }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 4000))
    ]);
    return true;
  } catch (e) { return false; }
}
/* 并发探活序号：applyChatMode 会被 renderChatSide / switchMode / 启动成功后重载
   多次调入，旧实现每次都在 await 之后 append 一层 ⇒ 2026-10-25 实测同一只停着的
   agent 叠出 **2 个 .embed-dead**（同文案两个按钮，上层还挡住下层点击点）。
   只允许最后发起的那次探活挂覆盖层。 */
let _embedProbeSeq = 0;

/* 就绪判定：有启动描述的卡一律问**服务端**（/api/agents/{id}/ui-status，hub 本机双栈探）。
   浏览器侧探活对 IPv6-only 后端是假死（qwenpaw 实测只监听 [::]:8088，其 UI 走 hub 注入
   代理 :3103；客户端探 127.0.0.1/lanUrl 永远失败）。没有启动描述的入口保持浏览器探活。 */
async function _uiReady(a, e, url) {
  if (e && e.start) {
    try {
      const d = await api('/api/agents/' + encodeURIComponent(a.id) + '/ui-status');
      return !!d.ready;
    } catch (x) { /* 端点不可用 ⇒ 退回浏览器探活，不因后端缺端点把界面判死 */ }
  }
  return await _embedAlive(lanUrl((e && e.health_url) || url || (e && e.url) || ''));
}

async function probeEmbed(url, autoWant) {
  const pane = $('embedPane');
  const seq = ++_embedProbeSeq;
  // 存活信号只保留"死时"的那一份：失败会有整屏覆盖层（含切模式/重试按钮），
  // 活着时行内再挂一句「可达」是纯噪声 —— 用户 09-20 要求删掉该字样。
  pane.querySelectorAll('.embed-dead').forEach(n => n.remove());
  const a = entityById(chatPick);
  const entry = (a && (a.entries || []).find(x => x.type === 'embed')) || null;
  if (await _uiReady(a, entry, url)) return;
  if (seq !== _embedProbeSeq) return;   // 期间已有更新的探活接管，别叠第二层
  /* 自动拉起意图在这里**消费**而不是在 applyChatMode 入口消费：go('chat') 与
     renderChatSide() 会连着调两次 applyChatMode，入口消费会让第二次（真正挂覆盖层
     的那次，见上面的 seq）拿到 false ⇒ 自动拉起永不触发（2026-10-25 实测）。 */
  const autoStart = autoWant || _embedAutoStartPending;
  _embedAutoStartPending = false;
  const dead = document.createElement('div');
  dead.className = 'embed-dead';
  dead.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;background:var(--mask);z-index:5';
  /* 按需拉起（2026-10-24 起全 agent 不长驻）⇒ 停着是常态。覆盖层必须给出**拉起**
     的按钮，否则用户只剩自己去终端敲 systemctl（2026-10-25 报障「要使用原生界面
     都没有拉起的地方」）。start 由后端画像声明（profiles.UI_START），未登记的
     TUI-only agent 不出这个按钮。 */
  const canStart = !!(entry && entry.start);
  dead.innerHTML = '<div class="embed-dead-msg" style="font-size:var(--fs-base);color:var(--danger-text)">目标界面未响应（' + escapeHtml(url) + '）</div>' +
    '<div style="display:flex;gap:8px">' +
    (canStart ? '<button class="btn sm" data-act="start" onclick="startNativeUI(this)">启动原生界面</button>' : '') +
    '<button class="btn sm ghost" onclick="switchMode(\'chat\')">改用对话模式</button>' +
    '<button class="btn sm ghost" onclick="switchMode(\'term\')">改用终端</button>' +
    '<button class="btn sm ghost" onclick="embedRefresh()">重试嵌入</button></div>';
  if (getComputedStyle(pane).position === 'static') pane.style.position = 'relative';
  pane.appendChild(dead);
  /* 用户是点「原生界面」进来的（autoStart）就直接拉起，不再让用户点第二次：
     按需拉起后「点开 = 目标停着」是常态，二次确认等于把报障原样保留。 */
  if (canStart && autoStart) {
    const msg = dead.querySelector('.embed-dead-msg');
    if (msg) { msg.style.color = 'var(--text-1)'; msg.textContent = '原生界面未运行，正在拉起…'; }
    startNativeUI(dead.querySelector('[data-act="start"]'));
  }
}

/* 启动原生界面（按需拉起）：POST /api/agents/{id}/ui-start → 轮询真实后端 → 重载嵌页。
   服务冷启动实测数秒（dsh/qwenpaw），所以轮询 2s × 10 次；成功前按钮保持禁用，
   失败要**如实报**并恢复可点，不静默吞掉。 */
async function startNativeUI(btn) {
  const a = entityById(chatPick);
  if (!a) return;
  const e = (a.entries || []).find(x => x.type === 'embed');
  if (!e || !e.start) return;
  const old = btn ? btn.textContent : '';
  if (btn) { btn.disabled = true; btn.textContent = '启动中…'; }
  try {
    /* 凭据只交给 api() 注入 x-hub-token（写方法它本来就会问一次并带对头）。
       调用处**不要**再传 termHeaders()：2026-10-25 实测在缺 token 的浏览器上
       这会先弹一次口令框，api() 再弹一次；两次都取消即回 401「缺少凭据」，
       用户看到的就是「启动失败：缺少凭据…」——即本次报障。 */
    const d = await api('/api/agents/' + encodeURIComponent(a.id) + '/ui-start',
                        { method: 'POST' });
    toast(d.status === 'already_running' ? '原生界面已在运行' : '已派发启动，等待界面就绪…', 'ok');
    for (let i = 0; i < 10; i++) {
      await new Promise(r => setTimeout(r, 2000));
      if (await _uiReady(a, e, e.url)) {
        await loadAgents();          // dsh 的 token/端口可能随启动变化，先取新 entries
        applyChatMode();             // 重建 embed（含新 URL）并重新探活
        /* 同一 URL 不会被重新导航（applyChatMode 只在 dataset.src != url 时才赋值），
           而**启动前那一次失败加载的嵌页会一直白在那儿**——2026-10-25 实测 qwenpaw
           自动拉起成功后 iframe 仍是空白，必须显式重载一次。 */
        const f = $('embedFrame');
        if (f) f.src = f.src;
        return;
      }
    }
    toast('已启动，但界面仍未响应；稍后可点「重试嵌入」', 'err');
    if (btn) { btn.disabled = false; btn.textContent = old; }
  } catch (err) {
    toast(err.http === 401
      ? '启动需要写凭据：请在「设置 → 口令」应用 TERM_TOKEN 后重试'
      : '启动失败：' + err.message, 'err');
    if (btn) { btn.disabled = false; btn.textContent = old; }
  }
}

async function embedRefresh() { const f = $('embedFrame'); f.src = f.src; probeEmbed(f.dataset.src || ''); }
function embedNewTab() { const a = entityById(chatPick); const e = (a.entries || []).find(x => x.type === 'embed'); if (e) window.open(entryUrl(e), '_blank'); }
/* 地址行点击复制（非安全上下文无 navigator.clipboard，降级用 execCommand）*/
function embedCopyUrl() {
  const raw = $('embedFrame').dataset.src || $('embedUrlHint').textContent || '';
  const uu = raw.trim();
  if (!uu) return;
  const ok = () => toast('已复制：' + uu);
  const fb = () => {
    const ta = document.createElement('textarea');
    ta.value = uu; ta.style.cssText = 'position:fixed;left:-9999px;top:0';
    document.body.appendChild(ta); ta.select();
    let done = false;
    try { done = document.execCommand('copy'); } catch (err) { done = false; }
    ta.remove();
    if (done) { ok(); return; }
    // 降级失败就不假装成功：把地址显式选中，提示手动复制
    const el = $('embedUrlHint');
    try { const rg = document.createRange(); rg.selectNodeContents(el);
          const sl = getSelection(); sl.removeAllRanges(); sl.addRange(rg); } catch (err) {}
    toast('未能自动复制，地址已选中，请手动复制', 'err');
  };
  if (navigator.clipboard && window.isSecureContext) navigator.clipboard.writeText(uu).then(ok, fb); else fb();
}

/* ── pty 终端（xterm.js + WebSocket）── 修复：会话按实体隔离，切换即换绑 ── */

let term = null, termFit = null, termWs = null, termSid = null, termSidAgent = null;
/* 渲染器与搜索插件的实际挂载结果（v0.13.3x：xterm 默认 DomRenderer 是每字符一个 DOM span，
   大输出/滚屏时与原生终端差一个数量级 —— 换 GPU 渲染器是「不好用」的首要解药）。
   termRendererName ∈ webgl|dom，dom 表示 webgl addon 没挂上（此时只是慢，不会白屏）。 */
let termRendererName = 'dom', termSearch = null;

/* ── 终端链路自愈（v0.13.6 P1-1）：应用层心跳 + 退避重连 + 显式失败 ──────────────
   实测缺陷（09-23 取证，非推断）：termConnect() 只有一个 new WebSocket，全仓零重连、
   零心跳、零用户可见失败——手机切网络 / hub 重启后 socket 以 1006 静默死透，画面冻在
   最后一帧，唯一恢复手段是手动点行1 芯片。这就是本仓库反复被烧的「静默不可用」。
   规矩：任何带重试的组件必须 心跳 + 超时 + 显式失败，三者齐了才算自愈。

   协议（服务端 term.py 已实现，09-23 工作树）：
     客户端 → {"type":"hb"}   服务端 → {"type":"hb","t":<epoch float>}
     任何 hb 回执都重置看门狗；回执本身不进画面（display 一律忽略）。
   关闭码分诊：4404/4410 = 会话在服务端已不存在 ⇒ 永不重连（重连只会复活用户已经离开的会话）；
     4401 = 未鉴权 ⇒ 停手并给出「重新应用口令」的路径（拿同一个错口令重连只会永远 4401）；
     1005/1006/1011/1012 等传输码 = 链路断了但 pty 多半还活着 ⇒ 自动重连，
     服务端会保留会话到 idle TTL 并回放 ring（最近 64KB），所以重连后画面自己就回来了。
   退避 1/2/4/8/16/30s 封顶后每 30s 继续试，绝不静默放弃；只有 hb 真往返成功才回到 1s 档。 */
/* resize 去抖（B2 / paseo 融合）：拖窗口、分屏动画、手机键盘弹起会在几十毫秒里
   打出几十上百次几何事件，每一次都是一次 TIOCSWINSZ ioctl + xterm 整屏 refresh。
   100ms 是 paseo 的取值（terminal-pane.tsx:95）。注意它只作用于**几何变化**：
   连接建立 / 重连 / 用户点芯片 / 白块自愈这些必须立刻发，走 force 路径不受它延迟。 */
const TERM_RESIZE_DEBOUNCE_MS = 100;
const TERM_HB_SEND_MS = 15000;    // 每 15s 发一帧 {"type":"hb"}
const TERM_HB_DEAD_MS = 30000;    // 距上一次 hb 回执 ≥30s ⇒ 判定半开，主动断开进重连
const TERM_RC_DELAYS = [1000, 2000, 4000, 8000, 16000, 30000];   // 封顶 30s，之后一直 30s
let termHbTimer = null;           // 全仓唯一「发帧」interval（同一时刻最多 1 个）
let termHbDeadline = null;        // 全仓唯一「判死」一次性定时器（每次回执重新武装）
let termHbLastReply = 0, termHbLastSend = 0, termHbBaseline = 0;
let termRcTimer = null;           // 待触发的重连（同一时刻最多 1 个）
let termRcAttempt = 0;            // 退避档位；仅 hb 往返成功后清零
let termInputWarned = false;      // 「输入没送达」只提示一次，不按 keystroke 刷屏
let termToastEl = null;           // 本模块自己那条 toast，链路恢复时收掉

/* 链路提示走既有 toast()（不改 index.html/CSS 的前提下唯一可见出口）；
   同一时刻只留最新一条，重连成功即清除，不留「正在重连」的僵尸提示。 */
function termToast(msg, cls) {
  termToastClear();
  const el = toast(msg, cls);
  if (el && el.nodeType) termToastEl = el;
}
function termToastClear() {
  if (termToastEl) { try { termToastEl.remove(); } catch (e) {} termToastEl = null; }
}
/* 缓冲区里也留一行：toast 4.2s 自动消失，画面历史必须能事后追责 */
function termNotice(s) { if (term) term.write('\r\n\x1b[90m' + s + '\x1b[0m'); }

/* ── 鼠标上报闸门（v0.8.1）───────────────────────────────────────────────
   症状：挂上嵌入式终端后，鼠标在终端里划过就在屏幕上刷出一串 35;29;1m35;26;3m35;22;4m… 的乱码。
   根因（实测，非推断）：WS 建立瞬间服务端会 send_bytes(sess.ring) 回放最近 64KB 输出
   （term.py:238「回放最近输出（重连不白屏）」）。这段历史里若含某个 TUI 的 \x1b[?1003h
   （any-event 鼠标跟踪），重挂后的新 xterm 实例会把它当成"当前状态"重新进入鼠标跟踪——
   可那个 TUI 早退出了，pty 那头现在是 bash，于是每动一下鼠标就生成一条 SGR 上报
   \x1b[<35;x;yM 灌进 pty，被回显/被 readline 打散成字面量，屏幕立刻刷成乱码。
   实测证据：CDP 派发 5 次真实 mousemove → 该 WS 出站 12 帧，其中
     {"data":"\u001b[<35;66;10M"} {"data":"\u001b[<35;67;11M"} {"data":"\u001b[<0;71;11m"}
   （Cb=35 即"无按键按下时的移动"，正是 1003 模式的产物。）
   对策：只认「实时输出」里的鼠标开关指令，回放帧一律不算；未开启时鼠标上报不发给 pty。
   正在跑的 TUI 会自己重新发 \x1b[?1003h（连上时我们已发过 resize，它会重画）→ 届时照常放行。 */
 let termMouseLive = false;
const TERM_MOUSE_MODES = new Set(['9', '1000', '1001', '1002', '1003', '1005', '1006', '1007', '1015', '1016']);
/* ── 鼠标模式分层（v0.13.84）──────────────────────────────────────────────
   termMouseLive 过去一身二职：既表「app 声明要鼠标」又被 v0.13.81 看门狗
   在 wheel/mousedown 瞬间置 false。对线性主屏一族（codex/cursor/shell：轮子
   归 scrollback）这是对的治疗；对全屏重画一族（claude/opencode）它把两条滚动
   路径同时掐死：它们的历史不在 scrollback（每帧全屏绝对定位重画，主屏 buffer
   只有一屏高、viewportY 恒 0 —— 真 PTY 实测），「向上翻看」只能把滚轮 SGR
   上报喂回 pty 由 app 自滚（实测：wheel-up ×12 能把 claude 翻回问句、
   opencode 钳在顶）。
   分层后：
     - termMouseWant = app 在**实时帧**里声明着的模式全集（DECSET h 加、l 删）
       —— app 的真实意图，只有实时帧与回放信任路径能改写；
     - termMouseLive = want 非空 —— termSend 上报转发口径，看门狗不再碰它；
     - xterm 的跟踪态才由 mousedown 摘（拖选/复制归浏览器），mouseup 立即回装。 */
const termMouseWant = new Set();
function termMouseWantReset() { termMouseWant.clear(); termMouseLive = false; }
/* want 缓存（v0.13.84）：per-sid 记「该会话的 TUI 声明过哪些鼠标模式」。
   为什么必须有它：opencode 开机只断言一次、resize/打字都不重发（真 PTY 实测
   /tmp/mouse_probe3~7），而 ring 只留最近 64KB —— 会话跑久之后整页刷新，
   回放尾巴里已经没有那份 DECSET，客户端无从接住 ⇒ 滚轮又回不到 app。
   可信度：非 shell 画像里 TUI 就是 pty 进程本体，pty 死 ⇒ 会话死 ⇒ ws 收 4410，
   本表同步清条目 —— 不存在「bash 里 vim 被 SIGKILL」那种残留窗口（shell 画像
   也不写本表，v0.8.1 的乱码教训原样保留）。 */
const TERM_WANT_KEY = 'hub.term.mousewant';
function termWantCachePut(sid, modes) {
  if (!sid) return;
  let m = {};
  try { m = JSON.parse(lsGet(TERM_WANT_KEY, '{}') || '{}'); } catch (e) { m = {}; }
  if (modes && modes.length) m[sid] = modes; else delete m[sid];
  const ks = Object.keys(m);
  for (let i = 0; i < ks.length - 48; i++) delete m[ks[i]];
  lsSet(TERM_WANT_KEY, JSON.stringify(m));
}
function termWantCacheGet(sid) {
  try { return JSON.parse(lsGet(TERM_WANT_KEY, '{}') || '{}')[sid] || []; } catch (e) { return []; }
}
/* 把 app 声明的模式装回 xterm 解析器。⚠️ term.write 是**显示数据流**（pty→画面方向），
   不是发给 pty —— app 从没感知被摘过，它内部的鼠标状态一直是开的（实测 claude
   只在重画时重新断言、opencode 开机断言一次就再不重发；不靠回装的话，一次拖选
   之后 opencode 的滚轮永远回不到 app 手里）。want 为空则什么都不做。

   ★ v0.13.92：**有选区时不回装**（2026-10-08 用户报障「嵌入式终端文字无法选择、复制」）。
   机制（vendor/xterm.js 5.5/6.0 逐字实证）：
     CoreMouseService 协议一从 NONE 变非 NONE，xterm 就调
     `SelectionService.disable()`，而它的实现是 `clearSelection(); _enabled=false`
     —— **回装 = 当场把刚拖出来的选区抹掉**。时序正是用户的体感：
       拖选（mousedown 复位 ⇒ 选区能建）→ 松开鼠标（window 上挂的 mouseup
       ⇒ setTimeout(termMouseArm)）⇒ 选区在用户看到之前就没了 ⇒「选不中/复制不动」。
     真 chromium + CDP 实测（本机修复前）：程序化建选区后调 termMouseArm()，
     `term.getSelection()` 由 '❯ echo …' 变 ''；只调 termMouseResetNow() 则保留。
   所以鼠标此刻归浏览器：等选区清掉（下一次点击/滚轮）再回装，见 termWheelNow。 */
function termMouseArm() {
  if (!term || !termMouseWant.size) return;
  if (term.hasSelection()) return;
  try { term.write('\x1b[?' + Array.from(termMouseWant).join(';') + 'h'); } catch (e) {}
}

/* ── 粘贴闸门（bracketed paste, DECSET 2004）────────────────────────────────
   痛点（方案 P1-3，属**正确性**问题不是锦上添花）：多行脚本粘进终端时，readline 把
   第一行当命令立刻执行、其余行当垃圾逐条报错——多行 prompt / 多行命令会被打散执行。
   对端开 2004 后 xterm 会自动把粘贴包成 ESC[200~ … ESC[201~（vendor/xterm.js 里
   `decPrivateModes.bracketedPasteMode` 分支，6.0.0 升版后重新实测仍在），shell 侧就不再逐行解释。
   Hub 自己要补的是 xterm **没做**的那一半：粘贴内容里若夹着终止序列 ESC[201~，
   xterm 原样包进去 ⇒ 对端提前结束粘贴模式，剩下的字节被当普通按键执行（注入面）。
   paseo 的处理见 terminal-paste.ts:27 —— 把内嵌的终止序列降级成字面量 `[201~`。
   状态只从**实时帧**判定（回放帧不扫，与鼠标模式同口径）：历史里的 2004 是过期状态。 */
let termBracketed = false;
const TERM_PASTE_END = '\x1b[201~';
const TERM_DECSET_RE = /\x1b\[\?([0-9;]+)([hl])/g;
/* 三种鼠标编码：SGR(\x1b[<b;x;yM|m) / X10(\x1b[M + 3 字节) / 1015(\x1b[b;x;yM|m) */
const TERM_MOUSE_REPORT_RE = /\x1b\[(?:<[0-9]+;[0-9]+;[0-9]+[Mm]|M[\s\S]{3}|[0-9]+;[0-9]+;[0-9]+[Mm])/g;
/* 回放结束后就地复位鼠标跟踪：xterm 处于跟踪态时会吞掉拖拽选中，界面像是"选不中文字" */
const TERM_MOUSE_OFF = '\x1b[?9l\x1b[?1000l\x1b[?1001l\x1b[?1002l\x1b[?1003l'
                     + '\x1b[?1005l\x1b[?1006l\x1b[?1015l\x1b[?1016l';

/* ── 备用屏：v0.13.82 的「退出序列」已于 v0.13.83 删除 ──────────────────────
   历史（保留，因为它是「终端不能向上滚动」那次报障的完整根因，半年后还有人要查）：
   跑过一次 codex 的嵌入式终端后**所有** agent 都滚不动。根因是 codex 的 TUI 开机发
   `\x1b[?1049h` 进「备用屏」，而 **xterm.js 的备用屏按设计没有 scrollback**
   （vendor 里是 `new Buffer(!1, …)`）；整页只有一个 xterm 实例，切会话走的
   `term.clear()` 只清当前缓冲区的行、**不退出备用屏** ⇒ 一次污染跨会话传染，
   直到整页刷新。cloudcli 一直正常，因为它走普通 shell→TTY，从不发 ?1049h。

   v0.13.82 的对策是「换会话那一帧补写退出序列」（`TERM_ALT_OFF`/`TERM_STATE_RESET`）
   外加一个手点按钮。**v0.13.83 用户裁定改为从根上禁止**：直接在解析层把
   `?1049h` 吞掉（见 03-agents-cards.js 的 `termAltScreenBlock()`），于是终端
   恒定主屏，退出序列与按钮都没有存在意义，已一并删除。
   ⚠️ 这条路径**不要**再退回「按滚轮触发退出序列」：实测 codex 的 ?1049h 开机只发一次、
   不随重画重发，在 TUI 活着时把它踢出备用屏会让画面停在不含 TUI 输出的缓冲区上，
   看起来像「冻住」且回不去。吞进入序列没有这个问题（从未进入 ⇒ 无需退出）。 */

/* ── 回放查询闸门（v0.12.4）───────────────────────────────────────────────
   ring 里除了画面字节，还夹着上一个 TUI 开机时发过的终端查询：\x1b[c（设备属性）、
   \x1b[6n / \x1b[5n（光标位置 / 状态）、\x1b]10;? 之类（配色）。xterm 会替终端**自动作答**，
   答案顺着 onData 灌进 pty，shell 不认这些字节就原样回显成
   `?1;2c` `1;1R` `0n` `]10;rgb:2424/2727/2b2b` —— 正是用户报的「白色遮挡时还有一串数字字母乱码」。
   实测（CDP 直查 xterm 5.5）：DA1 / DSR6 / DSR5 / OSC 10 / OSC 11 / OSC 4 六类全部会作答。
   对策：只在回放那一帧的解析期间把这几类注册成「吞掉不答」，写完 dispose 交还默认实现
   ⇒ 正在跑的 TUI 现场提问照样能得到答案，只有历史里的过期提问被静音。 */
const TERM_QUERY_OSC = [4, 10, 11, 12, 52];
function termWriteReplay(raw) {
  const gate = [];
  try {
    /* CSI 的私有前缀走独立派发表（注册 id 用 prefix 字段，写成 params 不生效——实测踩过）：
       \x1b[c \x1b[>c \x1b[?c（DA1/DA2/**DA3**）、\x1b[6n \x1b[5n \x1b[?6n（DSR）。
       实测漏掉 `?6n` 就会往 pty 吐一串 `?26;118R`。
       DA3（\x1b[?c）顺手也注册了，但要说清：grep 本仓 vendor 的 xterm 5.5，
       final:"c" 只注册了 primary 与 secondary 两个（{prefix:\"?\",final:\"c\"} **不存在**），
       所以在这个版本上 DA3 查询不会被作答 —— 这一条是**版本升级保险**，
       不是本次乱码的根因（原计划把它当根因，实测证伪）。 */
    for (const id of [{ final: 'c' }, { prefix: '>', final: 'c' }, { prefix: '?', final: 'c' },
                      { final: 'n' }, { prefix: '?', final: 'n' }])
      gate.push(term.parser.registerCsiHandler(id, () => true));
    /* DCS 这一类是本地实测补上的：原先整个闸门只管 CSI/OSC，漏了 DECRQPS。
       `ESC P $ q "q ESC \`（保护属性）与 `ESC P $ qr ESC \`（滚动区）都会被作答。
       实测（tests/verify_replay_gate.py，真代码抽取 + chromium，xterm 5.5 本仓 vendor）：
         不设防 ⇒ onData 收到 "\x1bP1$r0\"q" 与 "\x1bP1$r1;10r" 两个包
         注册后 ⇒ 两包消失，而对照组 \x1b[?6n 照常作答（不是把解析器整个闷掉）
       这些应答灌进 pty 后回显成 `P1$r0"q` / `P1$r1;10r`，与用户报的
       「白屏时夹带一串数字字母乱码」同形 —— 是 v0.12.4 那道闸门没覆盖完的分支。
       线序陷阱：中间码只有 `$`。多一个空格 intermediates 就变成 "$ "，压根匹配不上
       处理器，会得出"DECRQPS 不会作答"的假结论（本探针第一版就是这么错的）。 */
    gate.push(term.parser.registerDcsHandler({ intermediates: '$', final: 'q' }, () => true));
    // OSC 只在「是查询」时吞（带 ? 才是问，带颜色值是设色，不能误杀）
    for (const code of TERM_QUERY_OSC)
      gate.push(term.parser.registerOscHandler(code, s => String(s).includes('?')));
  } catch (e) { /* 注册失败就退回普通写入：宁可多几行乱码，也不能不显示画面 */ }
  term.write(raw, () => { while (gate.length) { try { gate.pop().dispose(); } catch (e) {} } });
}

/* 每帧 new TextDecoder() 是**正确性**问题，不只是浪费：
   UTF-8 多字节字符（中文、box-drawing、emoji）常被 WS 帧从中间劈开，
   独立解码器无法携带"半个字符"的状态 ⇒ 断点处固定吐出 U+FFFD。
   共用一个实例 + {stream:true} 才能把尾巴留到下一帧。
   每条新连接重置一次：上一连接残留的半个字符不该污染新会话的画面。 */
let termDecoder = new TextDecoder();
let termScanTail = '';
function termDecodeReset() { termDecoder = new TextDecoder(); termScanTail = ''; }
function termDecodeFrame(raw) {
  if (typeof raw === 'string') return raw;
  return termDecoder.decode(raw, { stream: true });
}

/* DECSET 扫描的两个便宜招：
   ① 预筛 —— 绝大多数帧里没有 `\x1b[?`，不必动那条全局正则；
   ② 带尾巴 —— `\x1b[?1003h` 恰好跨帧时（旧实现两帧都匹配不上，
      结果实时 TUI 开了鼠标跟踪却没被记下来，滚轮上报被闸门吃掉），
      把上一帧尾部 24 字符接在当前帧前面一起扫；同一条被扫到两次只是
      重复赋同一个值，幂等。 */
function termScanMouseFrame(text) {
  const chunk = termScanTail + text;
  termScanTail = text.slice(-24);
  if (chunk.indexOf('\x1b[?') < 0) return;
  termScanMouseMode(chunk);
}

function termScanMouseMode(text) {
  let m;
  TERM_DECSET_RE.lastIndex = 0;
  while ((m = TERM_DECSET_RE.exec(text))) {
    /* v0.13.84 分层：逐模式记账（h 加 l 删），live 由 want 派生。
       旧实现一条 `termMouseLive = (m[2]==='h')` 会被同帧里后到的非鼠标 DECSET
       无视、也被看门狗改写 —— app 意图和本端临时态必须分家，见文件上方分层注释。 */
    for (const n of m[1].split(';')) {
      if (!TERM_MOUSE_MODES.has(n)) continue;
      if (m[2] === 'h') termMouseWant.add(n); else termMouseWant.delete(n);
    }
    /* 2004 与鼠标同批扫描：不同 Set 是因为语义不同（一个是上报开关，一个是粘贴模式开关），
       混进 TERM_MOUSE_MODES 会让「鼠标开关」的判定多认一个不属于它的模式。 */
    if (m[1].split(';').indexOf('2004') >= 0) termBracketed = (m[2] === 'h');
  }
  termMouseLive = termMouseWant.size > 0;
  /* 非 shell 画像才落缓存：shell 里 TUI 的开关是「bash 存活期间的过客态」，
     缓存它 = 给 v0.8.1 乱码开回放后门。 */
  if (termSid && termSidAgent && termSidAgent !== 'shell')
    termWantCachePut(termSid, Array.from(termMouseWant));
}

/* 输入不许静默丢弃：现在的做法是「socket 没开就把 keystroke 吃掉」，用户对着冻屏打字
   而毫无反馈——正是静默不可用的另一半。改成显式失败 + 立刻确认重连在排队。
   不跨重连排队回放：半途敲下的一行被打进另一个上下文（比如重连后已经是别的程序）比丢更糟。 */
function termInputLost() {
  /* 输入进不去通常意味着 onclose 已排过重连；这里只补漏（连接在飞时不打扰它） */
  if (termSid && !termRcTimer && termWs && termWs.readyState !== 0) termScheduleReconnect(termWs);
  if (termInputWarned) return;
  termInputWarned = true;
  termToast('终端未连接，输入不会被送达（不排队重放，避免打进错误上下文）', 'err');
}

function termSend(data) {
  if (!termWs || termWs.readyState !== 1) { termInputLost(); return; }
  let payload = data;
  if (!termMouseLive) {
    payload = data.replace(TERM_MOUSE_REPORT_RE, '');
    if (!payload) return;   // 整帧都是鼠标上报 → 丢弃，别污染 pty
  }
  termWs.send(JSON.stringify({ data: payload }));
}

/* ── 重新可见 / 尺寸变化的统一出口 ─────────────────────────────────────────
   两条实测出来的坑：
   ① 容器被 display:none 藏起来时宽高为 0，此时 fit 会算出 1 行的怪尺寸 ⇒ 一律先判可见；
   ② pane 从 none→flex 重新显示后，xterm 的 DOM 渲染层不会自己补画 ⇒ 屏幕留白块
      （用户报的「切页面 / 换会话标签后有白色遮挡」）。重新可见时把全部行 refresh 一遍。
   ResizeObserver 在 0→实际尺寸那一刻自动进来；浏览器标签页切回来走 visibilitychange。 */
let termPaintedAt = '';
/* 本客户端是否主张终端尺寸所有权（B2 / paseo 融合）。
   场景：桌面正开着 vim（120×40），手机端同一个会话的页面在后台被 ResizeObserver
   或 visibilitychange 唤醒，发来一个 80×24 ⇒ 桌面的 vim 被压扁。用户看到的是
   「我什么都没做，终端自己乱了」，极难归因。
   规则：只有**前台且用户主动**的那一端才 claim；后台端只 update（服务端会静默忽略
   非所有者的 update）。claim 一次即置位，之后本端的几何变化继续以主人身份更新。 */
let termSizeClaimed = false;
function termVisible() {
  const el = $('termEl');
  return !!(el && el.clientWidth && el.clientHeight);
}
