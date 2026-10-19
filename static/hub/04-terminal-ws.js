function termAuthOk() { termAuthWarned = false; }

async function termRefreshList() {
  try {
    const d = await api('/api/term/sessions', { headers: termHeaders() });
    // 只显示当前选中 agent 的会话，避免显示其他 agent 的终端
    const live = (d.sessions || []).filter(s => s.alive && s.agent_id === chatPick);
    termAuthOk();   // 拿到清单 = 口令可用，重置去重位
    const el = $('termSessList');
    if (!el) return live;
    // 空清单就什么都不画（用户 09-20：拿掉「暂无活会话」占位字）
    el.innerHTML = live.map(termChipHtml).join('');
    // 当前正看的会话已被服务端回收（进程退出/超时）→ 清绑并提示，避免对着幽灵 sid 重连
    if (termSid && !live.some(s => s.id === termSid)) {
      termDetach();
      if (term) term.write('\r\n\x1b[90m[当前会话已结束——点「新会话」重新开始]\x1b[0m');
    }
    return live;
  } catch (e) {
    // 改前 GET 不鉴权，走不到这条路；P1-7 之后这里是 401 的唯一出口。
    // 全吐成 null 就是「静默不可用」—— 这是用户自己能修的一种失败，必须上屏。
    termAuthWarn(e);
    return null;
  }
  // 出错回 null（= 没拿到清单），与「清单为空」分开：空清单才清屏给提示，
  // 取不到清单不能把好好一块画面抹掉
}

function termAutoAttach(live) {
  // 只接本实体的活会话；没有就清屏给提示（确保不残留上一实体画面）
  // live = termRefreshList() 已经拿到的清单，别再为同一件事发第二个 GET
  const got = live ? Promise.resolve(live)
    : api('/api/term/sessions', { headers: termHeaders() })
        .then(d => (d.sessions || []).filter(s => s.agent_id === chatPick && s.alive));
  got.then(mine => {
    if (mine.length) {
      const last = mine[mine.length - 1].id;
      /* 已经接在这条会话上就别拆线重连：重连 = 清屏 + 只回放 ring 尾巴，
         用户看到的「离开当前页再回来就白屏」正是这么来的。补一次重绘即可。 */
      if (termSid === last && termWs && termWs.readyState === 1) { termRepaint(); return; }
      termConnect(last, chatPick);   // 自动挂载：刻意不带 user:true，否则手机一进页面就弹键盘
      return;
    }
    // 确保 detached：切换实体时清除旧绑定，避免显示错误会话
    if (termSid && termSidAgent !== chatPick) termDetach();
    term.clear();
    const a = entityById(chatPick);
    term.write('\x1b[90m' + chatPick + ' · ' + (a?.name || chatPick) + ' 终端\x1b[0m\r\n');
    term.write('\x1b[90m提示：点「新会话」拉起 ' + (a?.name || chatPick) + ' 的原生终端\x1b[0m\r\n');
  }).catch(e => { termAuthWarn(e); });
  // 这条 .catch 不是装饰：termAutoAttach 会为拿不到清单而自己补发一次 GET，
  // 无 catch 就只剩控制台里一条没人认领的 rejection，用户侧表现为「什么都没发生」
}

/* termKill()（「销毁当前」整块按钮）已随 v0.10.1 外框统一拿掉；销毁只保留芯片上的 × = termKillOne。 */
async function termKillOne(sid) {
  // 乐观移除：先摘 DOM 芯片，用户即时看到"删掉了"，再与后端对账
  const el = $('termSessList');
  const chip = el && el.querySelector('.sess-item[data-sid="' + sid + '"]');
  if (chip) chip.remove();
  try {
    await api('/api/term/sessions/' + sid, { method: 'DELETE', headers: termHeaders() });
    toast('会话已销毁', 'ok');
    if (sid === termSid) termDetach();
    termRefreshList().then(list => termAutoAttach(list));   // 一次 GET：重绘芯片并接上剩下的会话
  } catch (e) {
    toast(e.message, 'err');
    termRefreshList();  // 404 等异常：以服务端真实状态重绘，DOM 不骗人
  }
}

async function openChatSession() {
  const box = $('chatMsgs');
  box.innerHTML = '';
  chatSessLoad();
  chatCwdLoad();
  const sid = lsGet(sessKey(chatPick));
  if (!sid) { box.innerHTML = '<div class="hint" style="margin:auto">开始新会话（' + escapeHtml(chatPick) + '）</div>'; return; }
  try {
    const d = await api('/api/sessions/' + encodeURIComponent(sid) + '/messages');
    for (const m of d.messages || []) {
      box.appendChild(renderChatMsg(m));
    }
  } catch (e) { lsRemove(sessKey(chatPick)); }
}

function renderChatMsg(m) {
  const el = document.createElement('div');
  el.className = 'msg ' + (m.role === 'user' ? 'user' : m.role === 'error' ? 'err' : 'assistant');
  el.textContent = m.content || '';
  return el;
}

async function chatSend() {
  const input = $('chatInput');
  const model = $('chatModel')?.value || '';
  const cwd = $('chatCwd')?.value || '';
  const msg = input.value.trim();
  if (!msg) return;
  input.value = '';
  const box = $('chatMsgs');
  const me = document.createElement('div');
  me.className = 'msg user'; me.textContent = msg;
  box.appendChild(me); box.scrollTop = box.scrollHeight;
  let sid = lsGet(sessKey(chatPick));
  if (!sid) { sid = Math.random().toString(36).slice(2, 14); lsSet(sessKey(chatPick), sid); }
  const busy = document.createElement('div');
  busy.className = 'msg assistant'; busy.textContent = '回复中…';
  box.appendChild(busy);
  const body = { message: msg, session_id: sid, model: model, cwd: cwd || null };
  // 统一走 /api/agents/{id}/chat
  try {
    const d = await api('/api/agents/' + encodeURIComponent(chatPick) + '/chat',
      { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    busy.className = 'msg ' + (d.success ? 'assistant' : 'err');
    let txt = (d.response || d.error || JSON.stringify(d.hint || d));
    if (d.model) txt += '\n· model: ' + d.model;
    if (d.usage) txt += '\n· tokens: in ' + (d.usage.prompt_tokens || d.usage.input_tokens || '-') + ' / out ' + (d.usage.completion_tokens || d.usage.output_tokens || '-');
    busy.textContent = txt;
    chatSessLoad();  // 刷新会话列表（title 等信息更新）
  } catch (e) { busy.className = 'msg err'; busy.textContent = '[错误] ' + e.message; }
  box.scrollTop = box.scrollHeight;
}

/* ── 模型选择器（动态拉取 CCR /v1/models）── */
let CHAT_MODELS = [];
async function loadChatModels(force) {
  const sel = $('chatModel');
  if (!sel) return;
  const cached = !force && CHAT_MODELS.length && (Date.now() - (CHAT_MODELS._ts || 0) < 60000);
  if (!cached) {
    try {
      const d = await api('/api/models');
      CHAT_MODELS = (d.models || []).map(m => ({ ...m, _groups: d.groups || {} }));
      CHAT_MODELS._ts = Date.now();
      CHAT_MODELS._src = d.source;
    } catch (e) {
      sel.innerHTML = '<option value="">模型加载失败</option>';
      return;
    }
  }
  // 当前 agent 的 value_prefix（与设置页保持一致：opencode 用 ccr/，其它为空）
  const pa = SET_AGENTS.find(x => x.id === chatPick) || {};
  const prefix = pa.value_prefix || '';
  // 当前 agent 默认 model（localStorage 记忆）
  const saved = lsGet('hub.model.' + chatPick) || '';
  // 按 vendor 分组
  const groups = (CHAT_MODELS._groups) || {};
  const groupedKeys = Object.keys(groups);
  let html = '<option value="">默认（网关路由）</option>';
  for (const gk of groupedKeys) {
    html += '<optgroup label="' + escapeHtml(gk) + '">';
    for (const mid of groups[gk]) {
      const m = CHAT_MODELS.find(x => x.id === mid);
      const label = m && m.name && m.name !== mid ? (mid + ' — ' + m.name) : mid;
      const val = prefix + mid;
      // 选中态：兼容旧存储（裸 ID）与新存储（带前缀）
      const isSel = (val === saved) || (mid === saved);
      html += '<option value="' + escapeHtml(val) + '"' + (isSel ? ' selected' : '') + '>' + escapeHtml(label) + '</option>';
    }
    html += '</optgroup>';
  }
  // 兜底：未分组的也展示
  const grouped = new Set(groupedKeys.flatMap(k => groups[k]));
  const leftovers = CHAT_MODELS.filter(m => !grouped.has(m.id));
  if (leftovers.length) {
    html += '<optgroup label="其他">';
    for (const m of leftovers) {
      const val = prefix + m.id;
      const isSel = (val === saved) || (m.id === saved);
      html += '<option value="' + escapeHtml(val) + '"' + (isSel ? ' selected' : '') + '>' + escapeHtml(m.name || m.id) + '</option>';
    }
    html += '</optgroup>';
  }
  sel.innerHTML = html;
}
$('chatModel')?.addEventListener?.('change', e => {
  lsSet('hub.model.' + chatPick, e.target.value);
});

/* ── 工作目录（CWD 选择器）── */
const DEFAULT_CWDS = ['/fs/1000/ftp/技术文档', '/home/gztxt', '/fs/1000/ftp/技术文档/agenthub', '/vol1/1000/技术文档', '/tmp'];
function chatCwdLoad() {
  const sel = $('chatCwd');
  if (!sel) return;
  // 1) agent 画像默认 cwd（claude/jcode/hermes/bash）
  const a = entityById(chatPick);
  const profCwd = a && a.working_dir;
  // 2) localStorage 历史选择
  const saved = lsGet('hub.cwd.' + chatPick) || '';
  // 3) 合并去重
  const seen = new Set();
  const list = [];
  if (profCwd) { list.push({ v: profCwd, label: profCwd + ' ★画像' }); seen.add(profCwd); }
  if (saved && !seen.has(saved)) { list.push({ v: saved, label: saved + ' ★最近' }); seen.add(saved); }
  for (const d of DEFAULT_CWDS) {
    if (!seen.has(d)) { list.push({ v: d, label: d }); seen.add(d); }
  }
  sel.innerHTML = list.map(x => '<option value="' + escapeHtml(x.v) + '"' +
    (x.v === saved || (!saved && x.v === profCwd) ? ' selected' : '') + '>' + escapeHtml(x.label) + '</option>').join('');
}
function chatCwdCustom() {
  const cur = $('chatCwd')?.value || '';
  const v = prompt('自定义工作目录（绝对路径）', cur);
  if (v && v.trim()) {
    $('chatCwd').value = v.trim();
    lsSet('hub.cwd.' + chatPick, v.trim());
    toast('已设 cwd: ' + v.trim(), 'ok');
  }
}
$('chatCwd')?.addEventListener?.('change', e => {
  lsSet('hub.cwd.' + chatPick, e.target.value);
});

/* ── 会话管理 ── */
let CHAT_SESSIONS = [];
async function chatSessLoad() {
  const sel = $('chatSessList');
  if (!sel) return;
  const cur = lsGet(sessKey(chatPick)) || '';
  try {
    const d = await api('/api/sessions?agent_id=' + encodeURIComponent(chatPick) + '&limit=30');
    CHAT_SESSIONS = d.sessions || [];
    const opts = ['<option value="">当前会话</option>'];
    opts.push('<option value="__new__">新会话</option>');
    for (const s of CHAT_SESSIONS) {
      const title = (s.title || '未命名').slice(0, 24);
      const ts = (s.updated_at || '').slice(5, 16).replace('T', ' ');
      opts.push('<option value="' + escapeHtml(s.id) + '"' + (s.id === cur ? ' selected' : '') + '>' +
        escapeHtml(ts + ' · ' + title) + '</option>');
    }
    sel.innerHTML = opts.join('');
  } catch (e) { /* ignore */ }
}
function chatSessNew() {
  lsRemove(sessKey(chatPick));
  openChatSession();
  toast('已开新会话', 'ok');
}
async function chatSessRename() {
  const sid = lsGet(sessKey(chatPick));
  if (!sid) return toast('当前无会话', 'err');
  const cur = CHAT_SESSIONS.find(s => s.id === sid);
  const title = prompt('新标题', (cur && cur.title) || '');
  if (!title) return;
  try {
    await api('/api/sessions/' + encodeURIComponent(sid), { method: 'PATCH',
      headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ title: title }) });
    chatSessLoad();
    toast('已重命名', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}
$('chatSessList')?.addEventListener?.('change', async e => {
  const v = e.target.value;
  if (v === '__new__') { chatSessNew(); return; }
  if (!v) return;
  lsSet(sessKey(chatPick), v);
  openChatSession();
});
// T7：删除当前会话（后端 DELETE /api/sessions/{id} 连同消息一并清理）
async function chatSessDel() {
  const sid = lsGet(sessKey(chatPick));
  if (!sid) return toast('当前无会话', 'err');
  if (!confirm('删除当前会话 ' + sid.slice(0, 8) + ' 及其全部消息？不可恢复。')) return;
  try {
    await api('/api/sessions/' + encodeURIComponent(sid), { method: 'DELETE' });
    lsRemove(sessKey(chatPick));
    openChatSession();
    toast('会话已删除', 'ok');
  } catch (e) { toast(e.message, 'err'); }
}

/* applyChatMode 已内置：切换到 chat 模式时三联动刷新 */

/* ── 记忆中心 ─────────────────────────────────────── */

/* ── CloudCLI 项目直达（v0.13.29 → v0.13.80 双出口）────────────────────────────
 * v0.13.80（2026-10-05）：cloudcli 拆出为 Agents 菜单独立子菜单，其原生界面
 * （:3010 embed）下方内嵌项目直达面板（renderCloudcliProjects，02-nav-and-poll.js）；
 * 本函数降级为「嵌入面板渲染失败时的兜底」：claude 详情抽屉点入时走这条旧路径。
 * 用户诉求（原始）：「cloudcli 项目检索要完善、无法加载本机所有项目、精确显示项目名称、
 * 点击对应项目快速开始」。
 * 列表：GET /api/cloudcli/projects（直读 auth.db，与 cloudcli 服务活死解耦）；
 * 启动：POST /api/cloudcli/start {path} → {sessionId, url} → iframe 直达
 * /session/{id}（gotoChat('cloudcli') 进 embed 面板后覆写 src——dataset 同步
 * 防 applyChatMode 重置；embed 顶栏地址行同步）。
 * 嵌入 iframe 的鉴权态由 cloudcli 自己的 localStorage 管（跨源但同浏览器持久，
 * 知识文档 50 号已证）；hub 不传 token 不越权。 */
async function loadCloudcliProjects() {
  const body = $('detailBody');
  if (!body) return;
  const holder = document.createElement('div');
  holder.id = 'ccProjects';
  holder.innerHTML = '<div class="hint" style="margin:10px 0 4px">CloudCLI 项目（本机全部）</div><div class="hint">加载中…</div>';
  body.appendChild(holder);
  try {
    const d = await api('/api/cloudcli/projects');
    if (!d.ok) {
      holder.innerHTML = '<div class="hint" style="margin:10px 0 4px">CloudCLI 项目</div>' +
        '<div class="hint" style="color:var(--st-error,var(--danger))">加载失败：' + escapeHtml(d.error || '?') + '</div>';
      return;
    }
    const rows = (d.projects || []).map(p =>
      '<div class="mem-item" style="gap:6px">' +
      '<p style="min-width:0"><b>' + escapeHtml(p.name) + '</b>' + (p.starred ? ' ★' : '') +
      (p.sessions ? ' <span class="hint">' + p.sessions + ' 会话</span>' : '') +
      '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' +
      escapeHtml(String(p.path).slice(0, 48)) +
      (p.last_activity ? ' · ' + String(p.last_activity).slice(5, 16).replace('T', ' ') : '') + '</span></p>' +
      '<button class="btn sm" style="align-self:center" onclick="cloudcliStart(' +
      jsStr(p.path) + ')" title="在 CloudCLI 开始此项目的新会话">▶ 开始会话</button></div>').join('');
    holder.innerHTML = '<div class="hint" style="margin:10px 0 4px">CloudCLI 项目（' + (d.count || 0) + ' 个 · 点「开始会话」直达）</div>' +
      '<div class="tscroll" style="max-height:300px;overflow-y:auto">' + (rows || '<div class="hint">无项目</div>') + '</div>';
  } catch (e) {
    holder.innerHTML = '<div class="hint" style="margin:10px 0 4px">CloudCLI 项目</div>' +
      '<div class="hint" style="color:var(--st-error,var(--danger))">加载失败：' + escapeHtml(e.message || '') + '</div>';
  }
}

async function cloudcliStart(path) {
  toast('CloudCLI 创建会话中…');
  try {
    const d = await api('/api/cloudcli/start', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ path: path })
    });
    const full = 'http://127.0.0.1:3010' + (d.url || '');
    /* v0.13.80：cloudcli 独立子菜单成为原生界面入口。会话创建在 :3010 根域，
       跨源 iframe 里 cloudcli 自己的 localStorage 鉴权态与根页面同源（:3010），
       与 hub 面板里嵌的是否同一 iframe 无关。gotoChat('cloudcli') 进 embed 面板
       再覆写 src 到 /session/{id}（dataset 必须同步，防 applyChatMode 重置回实体
       ui.url）。地址行（embedUrlHint 的 span）同步显示。 */
    if (typeof gotoChat === 'function') gotoChat('cloudcli');
    const f = $('embedFrame');
    if (f) {
      const url = lanUrl(full);
      f.dataset.src = url;
      f.src = url;
      const hintTxt = $('embedUrlHint') ? $('embedUrlHint').querySelector('span') : null;
      if (hintTxt) hintTxt.textContent = url;
    }
    closeOverlay('detailDrawer');
    toast('会话已创建：' + (d.session_name || d.sessionId), 'ok');
  } catch (e) { toast('创建会话失败：' + e.message, 'err'); }
}

/* ── v0.13.28 源标识与逐路徽标（记忆页/kb 页共用渲染器）────────────────
 * SRC_ABBR：源名缩写（结果卡 tag 位宽恒定，不因长源名换行）。
 * srcTag()：三变体 .tag.src-doc/.src-session/.src-index（明度区分，不彩虹——
 *   v0.9 设计系统「色相只出现在语义上」的约束；裸拼 `tag turbovec` 无 CSS 定义
 *   静默灰底的根因修复）。kb 路名短别名也入表（tdai/workspace/archived）。 */
const SRC_ABBR = {
  claude_mem: 'CM', claude_projects: 'CP', pi_sessions: 'PI', codex_sessions: 'CX',
  codebuddy_sessions: 'CB',
  grok_memory: 'GK', hermes_memory: 'HM', workbuddy_memory: 'WB',
  workspace_files: 'WS', archived_sessions: 'AR', amr_memory: 'AM',
  experiences: 'EX',
  turbovec: 'TV', xerj: 'XJ', tdai_l1: 'TD', tdai: 'TD', local: 'L1'
};
function srcTag(srcName) {
  const key = String(srcName || '');
  const abbr = SRC_ABBR[key] || (key.slice(0, 2).toUpperCase() || '??');
  const cls = (key === 'turbovec' || key === 'xerj' || key === 'local') ? 'src-index'
    : (key.indexOf('session') >= 0 || key === 'claude_mem') ? 'src-session'
    : 'src-doc';
  return '<span class="tag src-' + cls + '" title="' + escapeHtml(key) + '" style="align-self:flex-start">' +
    escapeHtml(abbr) + '</span>';
}
/* fedBadges：逐路健康徽标行（绿=ok 有命中 / 黄=ok 零命中 / 红=挂了点名+错误 tooltip）。
 * 记忆页与 kb 页两处同构消费——从 memFedSearch 内联抽出（DRY，两页口径归一）。 */
function fedBadges(d) {
  return (d.backends || []).map(b => {
    const col = b.ok ? (b.count ? 'var(--st-running)' : 'var(--st-installed)') : 'var(--st-error)';
    const nm = b.ok ? b.name : b.name + ' 挂了';
    return '<span style="display:inline-flex;align-items:center;gap:3px;margin-right:10px">' +
      '<span class="hdot" style="background:' + col + '"></span>' +
      '<span class="hint">' + escapeHtml(nm) + (b.ok && b.count != null ? ' ' + b.count : '') +
      (b.error ? ' <span title="' + escapeHtml(String(b.error).slice(0, 100)) + '">?</span>' : '') + '</span></span>';
  }).join('');
}

/* ── 联邦检索（v0.13.27 E 缺口）：6 路 RRF，逐路 backends 徽标（四态口径
 *   与资产面板一致：ok/空/降级/端点挂，不把「挂了」渲染成「没有」）。
 *   按需触发，进页不自动跑。 ─────────────────────────────────────────── */
async function memFedSearch() {
  const q = ($('memFedQ') ? $('memFedQ').value : '').trim();
  if (!q) { toast('输入检索词', 'err'); return; }
  const sources = Array.from($('memFedSrcs').selectedOptions).map(o => o.value).join(',');
  const hint = $('memFedHint');
  if (hint) hint.textContent = '检索中…';
  boxBusy('memFedResults', '联邦检索中…');
  const badges = $('memFedBadges');
  try {
    /* v0.13.65 D1.5：全不选时的兜底从 local,tdai 升到快路联邦集，与后端
     memory.FED_FAST_SOURCES 逐字一致（workbuddy 47 / claude_mem 148 / claude_projects
     261ms 三个实测快源）；慢三路（pi/codex/archived）不默认开——实测 rg 超时
     1.9~2.5s，默认带上等于每次检索都等 3s。等 A4 索引投影落地后再扩。 */
    const d = await api('/api/memory/search?q=' + encodeURIComponent(q) + '&sources=' + encodeURIComponent(sources || 'local,tdai,claude_mem,workbuddy_memory,claude_projects'));
    if (hint) hint.textContent = (d.count || 0) + ' 条 · ' + (d.took_ms || '?') + 'ms · ' + (d.engine || 'none');
    /* 逐路徽标：绿=ok 有命中 / 黄=ok 零命中 / 红=挂了（点名，不糊成绿） */
    if (badges) badges.innerHTML = fedBadges(d);
    renderQueryBar('memFedQueryBar', q, d.count || 0, 'memFedClear');
    $('memFedResults').innerHTML = (d.memories || []).map(m =>
      '<div class="mem-item"><span class="tag ' + (m.category || m.type || 'fact') + '" style="align-self:flex-start">' +
      escapeHtml(m.category || m.type || m.layer || '?') + '</span>' +
      '<p>' + escapeHtml(String(m.content || '').slice(0, 300)) +
      '<br><span class="hint">' + escapeHtml(m.source || m.id || '') + '</span></p></div>').join('') ||
      '<div class="hint">零命中。' + ((d.degraded || []).length ? '降级路：' + d.degraded.join(',') + '（清单不完整，不代表没资产）' : '全部路可用且零命中（真没有）') + '</div>';
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
    boxFail('memFedResults', e, 'memFedSearch');
  }
}

/* v0.13.28 批3：检索回退。用户痛点「搜索完成后无法回退到记忆主界面」——
 * 检索结果覆写结果区后没有清空出口，只能刷新页面。设计三条：
 * ① QueryBar（当前检索 q · N 条 · 清空↺）由检索成功尾部渲染，失败态也挂（可撤销失败展示）；
 * ② Clear 还原初始文案+清 hint/badges/输入框——结果区回到「输入关键词开始检索」；
 * ③ **不碰 CENTER_HEALTH**：侧栏健康点是三中心体检态（上次加载成败的记录），
 *   与「用户清空本次检索结果」语义无关，清空不许把 ok 洗成未知。 */
function renderQueryBar(id, q, n, clearFn) {
  const bar = $(id);
  if (!bar) return;
  bar.style.display = '';
  bar.innerHTML = '<span class="hint">当前检索「<b>' + escapeHtml(q) + '</b>」· ' + n + ' 条</span>' +
    '<button class="btn ghost sm" onclick="' + clearFn + '()">清空 ↺</button>';
}
function memFedClear() {
  const bar = $('memFedQueryBar');
  if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
  const results = $('memFedResults');
  if (results) results.innerHTML = '输入关键词开始联邦检索';
  const badges = $('memFedBadges');
  if (badges) badges.innerHTML = '';
  const hint = $('memFedHint');
  if (hint) hint.textContent = '';
  const input = $('memFedQ');
  if (input) input.value = '';
  /* 注意：CENTER_HEALTH 故意不清（见函数头注释③） */
}

async function loadMemories() {
  const p = new URLSearchParams();
  if ($('memQ').value.trim()) p.set('q', $('memQ').value.trim());
  if ($('memCat').value) p.set('category', $('memCat').value);
  boxBusy('memList');
  try {
    const d = await api('/api/memory/l1?' + p.toString());
    CENTER_HEALTH.memory = 'ok';
    $('memList').innerHTML = (d.memories || []).map(m =>
      '<div class="mem-item"><span class="tag ' + m.category + '" style="align-self:flex-start">' + m.category + '</span>' +
      '<p>' + escapeHtml(m.content) + '<br><span class="hint">' + escapeHtml(m.source || '') + ' · ' + (m.created_at || '').slice(0, 10) + '</span></p>' +
      '<button class="btn sm danger" onclick="delMemory(' + m.id + ')">' + ico('x') + '</button></div>').join('') ||
      '<div class="hint">空空如也。可手动添加，或由指挥官/外部 Hook 写入。</div>';
    memStaleNote(d.memories || []);   // C：用实测口径刷新「本机旧层档案」陈旧标识
  } catch (e) {
    CENTER_HEALTH.memory = 'err';
    boxFail('memList', e, 'loadMemories');   // 失败上屏（不再只 toast——列表区停旧内容=分不清挂没挂）
  }
}
async function addMemory() {
  const c = $('memNew').value.trim();
  if (!c) return;
  try {
    await api('/api/memory/l1', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ content: c, category: 'fact', source: 'web' }) });
    $('memNew').value = ''; toast('已保存', 'ok'); loadMemories();
  } catch (e) { toast(e.message, 'err'); }
}
async function delMemory(id) {
  if (!confirm('删除这条记忆？')) return;
  try { await api('/api/memory/l1/' + id, { method: 'DELETE' }); loadMemories(); } catch (e) { toast(e.message, 'err'); }
}

async function loadDoc(lv) {
  try {
    const d = await api('/api/memory/' + lv);
    $(lv + 'content').value = d.content || '';
    $(lv + 'manual').value = d.manual || '';
    memStaleNote(window.__mxL1Rows || []);   // C：L2/L3 也参与陈旧口径
  } catch (e) { /* 首次为空 */ }
}

/* ═══ 记忆中心 P1（PT-20261018-08）：只读 /api/memory/vitals ═══════════════
 * 判据（方案 §4）：进页 1 个请求、首屏 0 个 <button>、计数与投影分组逐源一致。
 * 页面**不做现场探测**：探测由 agenthub-memvitals.timer 每 10 分钟跑一次并落盘，
 * 这里只读文件。旧版进页现场探测 1.9s，且 `memLoaded` 闸让整个会话只加载一次，
 * 绿点会静默变旧 —— 现在每次进页都重读（4ms），`as_of` 明说是哪一刻的状态。
 *
 * ⚠ verdict=unknown 有两种含义，必须分开渲染：
 *   · 文件不存在 ⇒ timer 从没跑过（不是「一切正常」）；
 *   · 文件太旧   ⇒ stale=true，前端不得把它画成现在。
 */
const MX_FAST_SOURCES = 'local,tdai,claude_mem,workbuddy_memory,claude_projects';
function mxSet(id, v) { const e = $(id); if (e) e.textContent = v; }
function mxMs(v) { return (v === null || v === undefined) ? '—' : (Math.round(v * 10) / 10) + ' ms'; }
const MV_VERDICT = { ok: '可用', warn: '降级', error: '异常', unknown: '未知' };

function mvAgo(iso) {
  if (!iso) return '—';
  const t = Date.parse(iso);
  if (isNaN(t)) return '—';
  const s = Math.max(0, (Date.now() - t) / 1000);
  if (s < 90) return Math.round(s) + ' 秒前';
  if (s < 5400) return Math.round(s / 60) + ' 分钟前';
  if (s < 172800) return Math.round(s / 3600) + ' 小时前';
  return Math.round(s / 86400) + ' 天前';
}
function mvHHMM(dt) {
  return String(dt.getHours()).padStart(2, '0') + ':' + String(dt.getMinutes()).padStart(2, '0');
}
function mvDur(ms) {
  if (ms === null || ms === undefined) return '—';
  return ms < 1000 ? Math.round(ms) + 'ms' : (Math.round(ms / 100) / 10) + 's';
}

async function memVitals() {
  try {
    const d = await api('/api/memory/vitals');
    mvRender(d);
    CENTER_HEALTH.memory = d.verdict === 'error' ? 'err' : (d.verdict === 'ok' ? 'ok' : 'warn');
  } catch (e) {
    const strip = $('mvStrip'); if (strip) strip.dataset.verdict = 'error';
    mxSet('mvVerdict', '读取失败');
    mxSet('mvFacts', '');
    const asof = $('mvAsOf'); if (asof) asof.textContent = e.message;
    CENTER_HEALTH.memory = 'err';
  }
}

function mvRender(d) {
  const strip = $('mvStrip'); if (!strip) return;
  strip.dataset.verdict = d.verdict || 'unknown';
  const s = d.summary || {};
  mxSet('mvVerdict', MV_VERDICT[d.verdict] || d.verdict || '未知');
  mxSet('mvFacts', d.verdict === 'unknown' ? '' :
    '源 ' + (s.sources_ok || 0) + '/' + (s.sources_total || 0) +
    (s.sources_off ? '（停用 ' + s.sources_off + '）' : '') +
    ' · 探针 ' + (s.probes_ok || 0) + '/' + (s.probes_total || 0));

  // as_of：不给「刚刚」这种模糊话——本次数据是几点采的、下次几点采，都写出来
  const asof = $('mvAsOf');
  if (asof) {
    if (!d.as_of) {
      asof.textContent = d.reason || '尚无探测结果（检查 agenthub-memvitals.timer）';
    } else {
      const now = new Date();
      const next = new Date(now.getTime());
      next.setSeconds(0, 0); next.setMinutes(Math.floor(now.getMinutes() / 10) * 10 + 10);
      asof.textContent = '探测于 ' + mvHHMM(new Date(d.as_of)) + '（' + mvAgo(d.as_of) + '）' +
        (d.stale ? ' · 数据已过期，定时器可能没在跑' : ' · 下次 ' + mvHHMM(next));
    }
  }

  // 异常清单：0 条只占一行
  const box = $('mvAnoms');
  if (box) {
    const anoms = d.anomalies || [];
    const notes = d.notes || [];
    box.innerHTML = anoms.map(a => {
      const lvl = a.status === 'warn' ? 'warn' : 'error';
      return '<div class="mv-anom" data-level="' + lvl + '">' +
        '<b>' + escapeHtml(a.name || a.id || '') + '</b> ' +
        '<span class="mv-why">' + escapeHtml(a.reason || '') + '</span>' +
        (a.evidence ? '<details><summary>证据</summary><pre>' +
          escapeHtml(JSON.stringify(a.evidence, null, 1)) + '</pre></details>' : '') +
        '</div>';
    }).join('') || '<div class="hint">无异常。</div>';
    // notes：可见但不染色 ⇒ 用 muted 左边框，不参与 verdict
    if (notes.length) {
      box.innerHTML += notes.map(n => '<div class="mv-anom" data-level="note">' +
        '<b>说明</b> <span class="mv-why">' + escapeHtml(n.name || '') + '：' +
        escapeHtml(n.reason || '') + '</span></div>').join('');
    }
  }

  // 分源表：条数来自 fed_proj 分组（P0 已修「三行同数」），off 源划掉但仍列出
  const srcs = d.sources || [];
  const okN = srcs.filter(x => x.enabled && x.ok).length;
  const enN = srcs.filter(x => x.enabled).length;
  mxSet('mvSrcHint', '启用 ' + enN + ' 路 · 可用 ' + okN + ' 路 · 停用 ' +
    srcs.filter(x => !x.enabled).length + ' 路');
  const list = $('mvSrcs');
  if (list) {
    const head = '<div class="mx-src mx-hd">' +
      '<span></span><span class="mx-src-n">源</span>' +
      '<span class="mx-src-k">最新</span><span class="mx-src-c">条数</span>' +
      '<span class="mx-src-t">耗时</span></div>';
    list.innerHTML = head + (srcs.map(x => {
      const cls = !x.enabled ? 'off' : (x.ok ? 'ok' : 'bad');
      const cnt = (!x.enabled) ? '—'
        : ((typeof x.count === 'number') ? x.count.toLocaleString('zh-CN') + ' 条' : '—');
      const fresh = x.fresh_at ? mvAgo(x.fresh_at) : '—';
      let tail;
      if (!x.enabled) tail = '停用';
      else if (!x.ok) tail = '不可达';
      else if (typeof x.ms === 'number') tail = Math.round(x.ms) + 'ms';
      else tail = 'ok';
      const title = [x.reason, x.note].filter(Boolean).join(' · ');
      return '<div class="mx-src' + (x.enabled ? '' : ' off') + '" title="' + escapeHtml(title) + '">' +
        '<span class="mx-dot ' + cls + '"></span>' +
        '<span class="mx-src-n">' + escapeHtml(x.label || x.id || '') + '</span>' +
        '<span class="mx-src-k">' + escapeHtml(fresh) + '</span>' +
        '<span class="mx-src-c">' + cnt + '</span>' +
        '<span class="mx-src-t">' + escapeHtml(tail) + '</span>' +
        '</div>';
    }).join('') || '<div class="hint">无源</div>');
  }

  // 探针明细（折叠区的载荷）
  const hint = $('mvProbeHint');
  if (hint) hint.textContent = (s.probes_ok || 0) + '/' + (s.probes_total || 0) +
    ' ok · 采集 ' + mvDur(s.took_ms) + ' · L0 可达 · L1 新鲜 · L2 语义 · L3 一致性';
  const pbox = $('mvProbes');
  if (pbox) {
    pbox.innerHTML = (d.checks || []).map(c => {
      const cls = c.status === 'ok' ? 'ok' : (c.status === 'info' ? '' : 'bad');
      const why = c.reason ? ' · ' + c.reason : '';
      return '<div class="mv-chk">' +
        '<span class="mx-dot ' + cls + '"></span>' +
        '<span class="mv-chk-l">' + escapeHtml(c.layer || '') + '</span>' +
        '<span class="mv-chk-n">' + escapeHtml(c.name || c.id || '') + escapeHtml(why) + '</span>' +
        '<span class="mv-chk-r">' + escapeHtml(c.status || '') + '</span>' +
        '</div>';
    }).join('') || '<div class="hint">无检查项</div>';
  }
}

/* 工具区展开才拉注入包 ⇒ 首屏请求数保持 1（判据①）。
   L1/L2/L3 已按裁定①移到「设置 · 记忆存储」，此页不再碰旧层数据。 */
function mvToolsToggle(el) {
  if (!el.open || el.dataset.loaded) return;
  el.dataset.loaded = '1';
  memCtxPreview();
}

async function memCtxPreview() {
  const body = $('ctxBody');
  if (!body) return;
  try {
    const d = await api('/api/memory/context?sources=' + encodeURIComponent(MX_FAST_SOURCES));
    /* 实测形状（2026-10-03）：fed.sources 是**数字**不是数组（对数字取 .length ⇒ undefined）；
       总字符数在顶层 d.chars，fed.chars 只是联邦段贡献量（可为 0）；
       d.context 才是正文（无 d.text）。三处都按实测取，不猜。 */
    const fed = d.fed || {};
    const nSrc = (typeof fed.sources === 'number') ? fed.sources : ((fed.sources || []).length || 0);
    const failed = fed.failed || [];
    const head = '快路请求 ' + MX_FAST_SOURCES.split(',').length + ' 路 · 命中 ' + nSrc + ' 路 · 注入 ' +
      (typeof d.chars === 'number' ? d.chars : '—') + ' 字符（联邦段 ' +
      (typeof fed.chars === 'number' ? fed.chars : '—') + '）· 墙钟 ' + mxMs(d.took_ms) +
      (failed.length ? '\n注意：本次失败 ' + failed.length + ' 路 → ' + failed.join(', ') : '') +
      ((d.degraded || []).length ? '\n注意：降级 → ' + d.degraded.join(', ') : '') +
      '\n\n';
    body.textContent = head + (d.context || JSON.stringify(d).slice(0, 4000));
  } catch (e) {
    body.textContent = '注入包预检失败：' + e.message;
  }
}

/* C：陈旧标识 —— 只报实测到的量，绝不写「已更新」这类无据结论 */
function memStaleNote(rows) {
  window.__mxL1Rows = rows;
  const el = $('mxStale'); if (!el) return;
  const l2 = ($('l2content') && $('l2content').value || '').trim();
  const l3 = ($('l3content') && $('l3content').value || '').trim();
  const dates = rows.map(r => String(r.created_at || '').slice(0, 10)).filter(Boolean).sort();
  const newest = dates.length ? dates[dates.length - 1] : '无';
  el.classList.toggle('is-stale', dates.length > 0 && dates[dates.length - 1] < MX_STALE_BEFORE);
  el.innerHTML = '本机旧层与在役联邦记忆是<b>两套独立存储</b>：L1 共 <b>' + rows.length + '</b> 条，最新 <b>' +
    newest + '</b>；L2 自动内容 <b>' + l2.length + '</b> 字符（仅由 L1 压缩而来）；L3 人工确认区 <b>' + l3.length +
    '</b> 字符。在役记忆以上方「源健康 / 注入上下文包 / 联邦检索」为准 —— 点「用 LLM 重建」只会重新压缩这 ' +
    rows.length + ' 条旧便签，不会更新此处显示。';
}
const MX_STALE_BEFORE = '2026-09-07';
async function saveDoc(lv) {
  const body = {};
  if (lv === 'l3') body.content = $(lv + 'content').value;
  body.manual = $(lv + 'manual').value;
  try { await api('/api/memory/' + lv, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); toast(lv.toUpperCase() + ' 已保存', 'ok'); }
  catch (e) { toast(e.message, 'err'); }
}
async function rebuildL2() {
  toast('LLM 压缩近30天 L1 → L2，请稍候…');
  try { const d = await api('/api/memory/l2/rebuild', { method: 'POST' }); toast('L2 重建完成（' + d.items + ' 条，LLM: ' + d.llm + '）', 'ok'); loadDoc('l2'); }
  catch (e) { toast(e.message, 'err'); }
}
async function previewCtx() {
  try {
    const d = await api('/api/memory/context?q=' + encodeURIComponent($('memQ').value.trim()));
    $('ctxPanel').style.display = '';   // v0.13.40：卡是 flex 列，显隐只切 inline 值，别写死 block
    $('ctxBody').textContent = d.context || '（空）';
  } catch (e) { toast(e.message, 'err'); }
}


/* ── 技能中心（v0.13.26 批4）────────────────────────── */

let SKILLS = [], SKILL_ROUTES = [], SKILL_STATUS = null, SKILL_ZOMBIES = {}, SKILL_ZOMBYE_ONLY = false;
/* v0.13.128（PT-20261019-03）：total 是**过滤后**的全量条数，SKILLS 是本次拿回的那一页。
 * 两者分开存——只存 SKILLS.length 的话，截断时统计条会把「显示到的」当成「本机有的」，
 * 正是这个 bug 最初骗过探针的方式。 */
let SKILL_TOTAL = 0, SKILL_TRUNCATED = false;
/* v0.13.127 P1（PT-20261019-01 Grill 裁定 5）三份新状态：
 * SKILL_VIEW      当前视图（'catalog' | 'invocations'）——**只存变量不写盘**：
 *                 分档偏好五条之②「加载不写盘」；视图开关不是设备分档，无需记忆。
 * SKILL_DOC       抽屉里正在看的技能（name/route），试调用/复制片段/安装都取它。
 * SKILL_INV       调用流水原始数据（/api/skill/invocations），汇总与徽标同源；
 *                 by_skill 映射给目录行徽标与统计条用（页面不存第二个真相）。 */
let SKILL_VIEW = 'catalog', SKILL_DOC = { name: '', route: '' };
let SKILL_INV = null, SKILL_INV_BY_SKILL = {}, SKILL_INV_LOADED = false;

/* v0.13.69 D7 技能门面：一份数据源、一个扫描口径。
 * 主列表 + 三张诊断卡（自检 / 相关性实验室 / 注入预算）都读同一批后端字段，
 * **页面不存第二个真相**——原先那句写死的「7 路发现点」已于 D1 改成 20 路后变成谎话。
 * 另：零调用榜的 confidence 封顶 medium（各家直读磁盘未取证）必须在界面上显示，
 * 藏起来的 medium 会被读成 high。 */

async function loadSkills() {
  boxBusy('skillList');
  try {
    // v0.13.128（PT-20261019-03）：显式要上限 1000。旧代码不传 limit ⇒ 后端默认 200，
    // 371 条里 12 路整路不可见，而界面把 200 当总量（探针原话「200 技能 / 21 路」）。
    // 后端仍可能截断（>1000 时）⇒ 读 total/truncated 并在统计条上明示，不静默。
    const d = await api('/api/skill/list?limit=1000');
    CENTER_HEALTH.skills = (d.degraded || []).length ? 'warn' : 'ok';
    SKILLS = d.items || d || [];
    SKILL_TOTAL = (d.total != null) ? d.total : SKILLS.length;
    SKILL_TRUNCATED = !!d.truncated;
    // 发现点下拉（过滤器 + 安装选择器共用）
    SKILL_ROUTES = (d.backends ? d.backends.filter(b => b.name !== 'tdai').map(b => b.name) : []) || [];
    const opt = SKILL_ROUTES.map(r => '<option value="' + r + '">' + r + '</option>').join('');
    $('skillRoute').innerHTML = '<option value="">全部发现点</option>' + opt;
    $('instFrom').innerHTML = opt;
    $('instTo').innerHTML = opt;
    mountPicks('instTo');   // v0.13.40：option 每次重写 ⇒ 芯片壳跟着重挂（函数幂等）
    $('instName').innerHTML = SKILLS.map(s => '<option value="' + escapeHtml(s.name) + '">' + escapeHtml(s.name) + '</option>').join('');
    $('skillHint').textContent = SKILL_TOTAL + ' 个技能 · ' + SKILL_ROUTES.length + ' 路发现点';
    if (SKILL_TRUNCATED) $('skillHint').textContent += '（本次只显示 ' + SKILLS.length + ' 条，单次有上限）';
    if ((d.degraded || []).length) $('skillHint').textContent += ' · 降级路：' + d.degraded.join(',');
    // 工具条那句同样不再写死（它与 skillHint 同源，不然后面会再次漂移）
    $('skillRouteHint').textContent = SKILL_ROUTES.length + ' 路发现点 · 只读门面：点「查看」读正文（脱敏）';
  } catch (e) {
    CENTER_HEALTH.skills = 'err';
    boxFail('skillList', e, 'loadSkills');
  }
  // 诊断数据与主列表**分开拉**：自检/零调用失败不该把主列表一起拖黑。
  loadSkillStatus();
  loadSkillZombies();
  // v0.13.127 P1：调用徽标/统计条的数据源（/api/skill/invocations 的 summary.by_skill）。
  // 同样分开拉：它失败只掉徽标，不掉列表。
  loadSkillInvBadges();
  // 「查看」委托绑定（幂等；渲染器每次重建行，绑定只挂一次）
  bindSkillViewDelegation();
}

/* v0.13.127 P1：拉一次调用流水汇总，供①目录行调用徽标 ②统计条 用。
 * 只取汇总不取明细：明细是「调用记录」视图的事（switchSkillView 懒加载）。
 * 失败静默降级为「徽标不可用」——徽标是增强信息，不该把主列表拖黑。 */
async function loadSkillInvBadges() {
  try {
    const d = await api('/api/skill/invocations?days=7&limit=100');
    SKILL_INV_BY_SKILL = (d.summary && d.summary.by_skill) || {};
    SKILL_INV = d;
  } catch (e) {
    SKILL_INV_BY_SKILL = {};
    SKILL_INV = null;
  }
  renderSkillStats();
  renderSkillList();
}

/* 统计条：每个数字都来自后端字段（list 的 items/backends、invocations 的 summary、
 * zombies 的 counted）。**不许在页面上写死任何数**——本仓吃过「7 路发现点」的亏。
 * 降级路从 /status 的 disk[*].available 现算（该端点无顶层 degraded 字段，
 * 别读一个不存在的键然后当成「没有降级」）。 */
function renderSkillStats() {
  const el = $('skillStats');
  if (!el) return;
  const nSkill = SKILL_TOTAL || SKILLS.length;
  const nRoute = SKILL_ROUTES.length;
  const invTotal = SKILL_INV ? (SKILL_INV.matched_total != null ? SKILL_INV.matched_total : 0) : 0;
  const nInvoked = Object.keys(SKILL_INV_BY_SKILL || {}).length;
  const zCount = Object.keys(SKILL_ZOMBIES || {}).length;
  const degraded = [];
  const disk = (SKILL_STATUS && SKILL_STATUS.disk) || {};
  Object.keys(disk).forEach(k => { if (disk[k] && disk[k].available === false) degraded.push(k); });
  let html = '共 <b>' + nSkill + '</b> 个技能 · <b>' + nRoute + '</b> 路发现点' +
    ' · 近 7 天调用 <b>' + invTotal + '</b> 次（<b>' + nInvoked + '</b> 个技能被调用过）' +
    ' · 零调用 <b>' + zCount + '</b> 个';
  if (SKILL_TRUNCATED) {
    // 「共 N 个」与「列表里有几条」必须分开说：把截断后的条数写成总数 = 这个 bug 的原始形态
    html += ' · <span class="sk-stat-warn">本次只显示 ' + SKILLS.length +
      ' 条（单次上限），用搜索缩小范围</span>';
  }
  if (degraded.length) {
    html += ' · <span class="sk-stat-warn">降级路：' + escapeHtml(degraded.join(', ')) + '</span>';
  }
  if (SKILL_INV && SKILL_INV.bad_detail) {
    html += ' · <span class="sk-stat-warn">' + SKILL_INV.bad_detail + ' 条坏账已跳过</span>';
  }
  el.innerHTML = html;
}

/* 视图切换（Grill 裁定 5：技能目录 / 调用记录两视图）。
 * 懒加载：调用记录第一次切到才拉数据——首屏请求数不因它增加。 */
function switchSkillView(v) {
  if (v !== 'catalog' && v !== 'invocations') return;
  SKILL_VIEW = v;
  ['catalog', 'invocations'].forEach(k => {
    const tab = $('skillTab' + k[0].toUpperCase() + k.slice(1));
    const view = $('skillView' + k[0].toUpperCase() + k.slice(1));
    if (tab) { tab.classList.toggle('on', k === v); tab.setAttribute('aria-selected', k === v ? 'true' : 'false'); }
    if (view) view.style.display = (k === v) ? '' : 'none';
  });
  if (v === 'invocations' && !SKILL_INV_LOADED) {
    SKILL_INV_LOADED = true;
    loadSkillInvocations();
  }
  if (v === 'invocations') renderSkillInvocations();
}

/* 技能中心的匹配字段表：**必须与 `src/skill.py` 的 `_Q_FIELDS` 同表同权重**。
 * 加 `realpath`（权重 1）是为了能按来源仓名找技能——`path` 是软链那一侧，
 * `mattpocock-skills` 这类仓名只出现在 `realpath` 里，不加就搜不到。
 * 两边只要有一处漏改，`tests/test_skill_list_q_parity.py` 就会红（分数逐条比对）。
 * 别在这里「顺手简化」成只有 name+desc。 */
function skillFields(s) {
  return [[s.name, 3], [s.description, 1], [s.path, 1], [s.route, 1], [s.realpath, 1]];
}

function toggleSkillZombieOnly() {
  SKILL_ZOMBYE_ONLY = !SKILL_ZOMBYE_ONLY;
  $('skillZombieOnly').classList.toggle('on', SKILL_ZOMBYE_ONLY);
  renderSkillList();
}

function renderSkillList() {
  const q = ($('skillQ').value || '').toLowerCase();
  const route = $('skillRoute').value;
  // 模糊打分 + 排序：精确命中（1000 分）在前，近似命中在后且按接近度排。
  const scored = SKILLS
    .filter(s => (!route || (s.routes || []).includes(route) || s.route === route))
    .map(s => ({ s, score: fuzzyMatch(skillFields(s), q) }))
    .filter(x => !q || x.score > 0)
    .sort((a, b) => (b.score - a.score) || String(a.s.name || '').localeCompare(String(b.s.name || '')));
  const rows = scored.map(x => x.s);
  const nearCount = q ? scored.filter(x => x.score < 1000).length : 0;
  // 「只看零调用」是**后端给的名单**，不是前端自己数出来的：零调用要看的是
  // 注入/读取两个通道的记账，而不是「页面上没点过查看」。
  const zrows = SKILL_ZOMBYE_ONLY ? rows.filter(s => SKILL_ZOMBIES[s.name]) : rows;
  const el = $('skillList');
  if (SKILL_ZOMBYE_ONLY && !Object.keys(SKILL_ZOMBIES).length) {
    el.innerHTML = '<div class="hint">零调用榜为空（可能记账尚未建立）——先用相关性实验室跑一次检索。</div>';
    return;
  }
  const nearHint = (q && nearCount)
    ? '<div class="hint">其中 <b>' + nearCount + '</b> 条为<b>近似匹配</b>（容忍手误，标 ≈近似）——无精确命中项。</div>'
    : '';
  el.innerHTML = nearHint + zrows.map(s => {
    const rt = (s.routes && s.routes.length) ? s.routes[0] : s.route;   // 首路作为查看默认 route
    const z = SKILL_ZOMBIES[s.name];
    const used = z
      ? '<span class="badge" title="建议：' + escapeHtml(String(z.suggested_action || '')) + '（只是建议，不自动删）">' + escapeHtml(String(z.days_idle)) + ' 天零调用</span>'
      : '';
    // 近似徽标：这条是靠编辑距离进来的，不是精确子串。**必须标出来**，
    // 否则用户看到一条「搜不完全对」的记录却不知道为什么。
    const sc = fuzzyMatch(skillFields(s), q);
    const near = (q && sc > 0 && sc < 1000)
      ? '<span class="badge" title="无精确子串命中，这条靠模糊匹配（容忍手误）召回">≈近似</span>'
      : '';
    // v0.13.127 P1：调用次数徽标（Grill 裁定 5「调用次数徽标」）。数据来自
    // /api/skill/invocations 的 summary.by_skill —— **hub 通道口径**（各家 agent
    // 直读磁盘的旁路未接入，title 里写清，别让人读成「总调用次数」）。
    const nInv = (SKILL_INV_BY_SKILL && SKILL_INV_BY_SKILL[s.name]) || 0;
    const inv = nInv
      ? '<span class="badge sk-inv" title="近 7 天被显式调用 ' + nInv + ' 次（/api/skill/invoke，hub 通道口径；agent 直读磁盘的旁路未接入）">调用 ' + nInv + '</span>'
      : '';
    // digest 指纹：整包摘要前 8 位（hover 看全文）——「技能是否被改过」的比对依据。
    const fp = s.pkg_digest
      ? '<span class="sk-fp" title="整包摘要（SKILL.md + 同目录脚本，' + (s.pkg_files || 0) + ' 个文件 ' + (s.pkg_bytes || 0) + 'B）：' + escapeHtml(String(s.pkg_digest)) + '">指纹 ' + escapeHtml(String(s.pkg_digest).slice(0, 8)) + '</span>'
      : '';
    const invLine = nInv
      ? '近 7 天调用 <b>' + nInv + '</b> 次'
      : (z ? '末次调用：无（从未调用）' : '近 7 天无 hub 通道调用记录');
    return '<div class="mem-item"><span class="tag agent" style="align-self:flex-start">' + escapeHtml(s.name) + '</span>' +
    // 描述走 mdInline（行内白名单 + 先转义），不再 escapeHtml —— 见 01-core-boot 的口径注释
    '<p>' + mdInline(String(s.description || '').slice(0, 160)) +
    '<br><span class="hint">' + escapeHtml((s.routes || [s.route]).join(', ')) +
    ' · ' + invLine + (fp ? ' · ' + fp : '') + '</span></p>' +
    '<span style="align-self:flex-start;display:flex;gap:4px">' + used + inv + near +
    // v0.13.127 P1：「查看」走 data-* 委托，**不再 inline onclick + jsStr**——
    // jsStr 出双引号字面量，嵌在双引号界定的 onclick 属性里会被 HTML 解析器提前截断
    // （v0.13.59 引入，渲染出的属性值只剩函数名+左括号 ⇒ 点击静默 SyntaxError，见
    // bindSkillViewDelegation 注释）。属性值走 escapeHtml（含 " 转义），dataset 读回原值。
    '<button class="btn sm" data-sk-view="1" data-name="' + escapeHtml(s.name) +
    '" data-route="' + escapeHtml(rt == null ? '' : rt) + '" title="读全文（脱敏）">查看</button></span></div>';
  }).join('') ||
    '<div class="hint">没有匹配的技能（' + zrows.length + ' / ' + (SKILL_TOTAL || SKILLS.length) + ' 总数' +
    (q ? '；已启用模糊匹配仍无结果 ⇒ 换关键词或查发现点自检' : '') + '）</div>';
}

/* 零调用榜：/api/skill/zombies。**confidence 封顶 medium**——只接了 hub 通道
 * （profile_events 的 skill.read/skill.inject），各家直读磁盘的旁路本批未实现。
 * 界面上必须把它写出来，否则 medium 会被读成 high。
 *
 * ★判例 103：`counted===0` 必须与「有记账但都是 0」**视觉上分开**。
 *   改之前这里只写「记账覆盖 0 条」，与「真的一个都没被用」同形 ——
 *   而 2026-10-09 实测就是这样把 `agent-dispatch`（真被调用 166 次、全机最热）
 *   摆进了僵尸榜。**账空时整榜不可用于删除决策**，所以要横幅，不能只写进note。 */
async function loadSkillZombies() {
  try {
    const d = await api('/api/skill/zombies?days=7');
    SKILL_ZOMBIES = {};
    (d.zombies || []).forEach(z => { SKILL_ZOMBIES[z.name] = z; });
    const c = d.confidence || 'medium';
    const counted = d.counted || 0;
    const noLedger = counted === 0;
    let html = '零调用榜：' + (d.zombies_count || 0) + ' / ' + (d.total || 0) +
      ' 条 · 记账覆盖 <b>' + counted + '</b> 条 · 置信度 <b>' + escapeHtml(c) + '</b>' +
      (d.direct_source === 'not-implemented' ? '（第二数据源未接入，置信度封顶，不封顶就是自欺）' : '');
    if (noLedger) {
      /* 措辞与 07-asset-panel.js 的「部分后端不可用」同型（判例 103）：
         明确说「下面这份不是零使用」，否则用户读成「都没用」就会去删。 */
      html = '<div class="hint" style="color:var(--warn)"><b>没有记账数据，不能当「零使用」看</b>：' +
        '下面 ' + (d.zombies_count || 0) + ' 条只是「账本里查不到痕迹」，' +
        '不是「确认无人调用」。<b>据此删技能会误删</b>（2026-10-09 实测：' +
        '本机最热的 agent-dispatch 被用 166 次，却出现在这份榜里）。</div>' + html;
    }
    $('skillBudgetHint').innerHTML = html;
  } catch (e) {
    $('skillBudgetHint').innerHTML = '<span class="hint">零调用榜不可用：' + escapeHtml(e.message || e) + '</span>';
  }
  renderSkillList();
  renderSkillStats();
}

/* 发现点自检：/api/skill/status。四态 + 排除段 + 白名单外拒读数。
 * 排除段必须在界面上公开理由（D1 的硬要求），否则「为什么这条没进来」无从回答。 */
async function loadSkillStatus() {
  const el = $('skillSelfCheck');
  if (!el) return;
  try {
    const d = await api('/api/skill/status');
    SKILL_STATUS = d;
    const LABEL = { ok: '正常', empty: '目录空', missing: '未安装', error: '读失败' };
    const rows = Object.keys(d.disk || {}).sort().map(k => {
      const s = d.disk[k] || {};
      const st = LABEL[s.state] || s.state || '?';
      const skip = (s.skipped_outside || []).length;
      return '<div class="mem-item"><span class="tag agent" style="align-self:flex-start">' + escapeHtml(k) + '</span>' +
        '<p>' + escapeHtml(st) + ' · <b>' + (s.entries || 0) + '</b> 条' +
        (skip ? ' · <span class="badge">' + skip + ' 条白名单外拒读</span>' : '') +
        ((s.fm_missing || []).length ? ' · ' + s.fm_missing.length + ' 条缺 frontmatter' : '') +
        '<br><span class="hint">' + escapeHtml((s.roots || []).join('  ')) + '</span>' +
        (s.error ? '<br><span class="hint">' + escapeHtml(String(s.error).slice(0, 140)) + '</span>' : '') +
        '</p></div>';
    });
    const dd = d.dedup || {};
    const exc = d.excluded || {};
    const excRows = Object.keys(exc).map(p => '<div class="mem-item"><span class="tag agent" style="align-self:flex-start">排除</span>' +
      '<p>' + escapeHtml(p) + '<br><span class="hint">' + escapeHtml(String(exc[p])) + '</span></p></div>');
    el.innerHTML = rows.join('') +
      '<div class="hint" style="padding:8px 10px">去重前走 ' + (dd.walked || 0) + ' 条 → 去重后 ' +
      (dd.unique || 0) + ' 条 · 合并 ' + ((dd.aliases || []).length) + ' 个同源副本</div>' +
      (excRows.length ? '<div class="hint" style="padding:8px 10px">排除 ' + excRows.length + ' 项（不算发现点，但理由公开）</div>' + excRows.join('') : '');
  } catch (e) {
    boxFail('skillSelfCheck', e, 'loadSkillStatus');
  }
  renderSkillStats();
}

/* 相关性实验室：/api/skill/relevant。
 * **默认 rerank=false 是硬要求**：实测 rerank=true 时该端点 took_ms=1064.5ms，
 * 而 hub-facade 的 input 钩子预算是 600ms（见 src/main.py 0.13.68 版本注释）。
 * 界面上给开关，但默认关；BM25 层实测 12–53ms。 */
async function skillLabSearch() {
  const q = ($('skillLabQ').value || '').trim();
  const el = $('skillLab'), hint = $('skillLabHint');
  if (!el) return;
  if (!q) { if (hint) hint.textContent = '先输入一句话'; return; }
  boxBusy('skillLab');
  const useJev = $('skillLabJev') && $('skillLabJev').checked;
  try {
    const d = await api('/api/skill/relevant?q=' + encodeURIComponent(q) +
      '&n=5&rerank=' + (useJev ? 'true' : 'false'));
    const b = d.bm25 || {};
    const items = b.items || [];
    if (hint) hint.textContent = '命中 ' + items.length + ' / 语料 ' + (d.total || 0) +
      ' · ' + (d.took_ms != null ? d.took_ms + 'ms' : '?') + (useJev ? '（jev 已开，会慢 ~1s）' : '');
    el.innerHTML = items.map(x => {
      const j = x.jev;
      const jevTxt = j ? ' · jev ' + escapeHtml(String(j.score)) + '（置信 ' + escapeHtml(String(j.confidence)) + '）' : '';
      return '<div class="mem-item"><span class="tag agent" style="align-self:flex-start">' +
        escapeHtml(String(x.name)) + '</span><p><b>' + escapeHtml(String(x.bm25)) + '</b> 分' + jevTxt +
        '<br><span class="hint">命中词：' + escapeHtml((x.matched || []).join(' ')) +
        (x.matched_in && x.matched_in.length ? '（' + escapeHtml(x.matched_in.join(' ')) + '）' : '') +
        ' · ' + escapeHtml((x.routes || [x.route]).join(', ')) +
        ' · 约 ' + (x.tokens_est || 0) + ' tok</span>' +
        '<br>' + mdInline(String(x.description || '').slice(0, 120)) + '</p></div>';
    }).join('') || '<div class="hint">无命中</div>' +
      '<div class="hint" style="padding:8px 10px">注入时 hub 取 top-3 全描述、其余仅名字；' +
      '被预算裁掉的条目 = agent 根本看不到它。</div>';
  } catch (e) {
    boxFail('skillLab', e, 'skillLabSearch');
  }
}

/* v0.13.127 P1：「查看」入口的委托绑定（§4.2 口径：交互一律 data-* 走委托）。
 *
 * 【为什么必须换掉 inline onclick】v0.13.59 为防双语境注入给三处 inline onclick
 * 换了 jsStr()，但 jsStr 出的是**双引号** JS 字面量，嵌在双引号界定的属性里会被
 * HTML 解析器提前截断 ⇒ 属性值只剩「函数名+左括号」⇒ 点击即静默 SyntaxError。
 * 实测（本批 P1 探针抓到）：技能行「查看」自 2026-09-30 起点了没反应。
 * 同族还有 4 处（agents 的 verifyAgent / 本地项目的 cloudcliStart / 工具选择的
 * pickTool / 命令面板的 cmdGo）——不在本 PT 范围，已报请并登记 PT，不顺手修。
 *
 * data-* + escapeHtml 进属性（" 被转成 &quot;，任何名字都不断属性），dataset 读回原值。
 * 绑定幂等（loadSkills 每次刷新都调；marker 防重复挂——重复挂会一点开两个抽屉调用）。 */
function bindSkillViewDelegation() {
  ['skillList', 'skillDocDrawer'].forEach(id => {
    const el = $(id);
    if (!el || el.__skDeleg) return;
    el.__skDeleg = true;
    el.addEventListener('click', e => {
      const b = e.target.closest('[data-sk-view]');
      if (!b || !el.contains(b)) return;
      e.stopPropagation();
      skillRead(b.dataset.name || '', b.dataset.route || '');
    });
  });
}

/* 技能详情抽屉（v0.13.127 P1，Grill 裁定 5）：行上「查看」入口。
 * 打开抽屉 → 载入元信息/分布路由/调用统计 → 载入正文（/read，**不记账**）。
 * 409 多路冲突时读 err.payload.detail.candidates 渲染候选按钮（api() 已挂 payload）。
 * 抽屉只经 openOverlay 开（浮层三条红线：禁止裸 classList.add('on')）。
 *
 * 与 /invoke 的分钮（裁定 3）：本函数走 /read 不记账；抽屉里「试调用」走
 * /invoke 记账（caller=web 自报）。两条链路的口径在服务端单一出处
 * （_resolve_skill），前端只负责把「查看」与「操作」分成两个按钮。 */
async function skillRead(name, route) {
  SKILL_DOC = { name: name, route: route || '' };
  const title = $('skillDocTitle');
  if (title) title.textContent = name + (route ? ' · ' + route : '');
  const meta = $('skillDocMeta'), routesEl = $('skillDocRoutes'), usage = $('skillDocUsage');
  if (meta) meta.innerHTML = '读取中…';
  if (routesEl) routesEl.innerHTML = '读取中…';
  if (usage) usage.innerHTML = '读取中…';
  openOverlay('skillDocDrawer');
  skillRenderDocMeta();
  skillLoadDocUsage();
  await skillReadDoc();
  skillFillInstTo(route);
}

/* 抽屉内「查看正文」：只重取正文（脱敏）。查看不记账——与「试调用」分钮。 */
async function skillReadDoc() {
  const body = $('skillDocBody');
  if (!body) return;
  body.textContent = '读取中…';
  try {
    const d = await api('/api/skill/read?name=' + encodeURIComponent(SKILL_DOC.name) +
      (SKILL_DOC.route ? '&route=' + encodeURIComponent(SKILL_DOC.route) : ''));
    let txt = d.content || '';
    if (d.truncated) {
      txt += '\n\n[正文 ' + (d.bytes_total || '?') + 'B 超过读取上限已截断；需要全文请直接读磁盘路径 ' + (d.realpath || '?') + ']';
    }
    body.textContent = txt;
  } catch (e) {
    const cands = (e.payload && e.payload.detail && e.payload.detail.candidates) || [];
    if (e.http === 409 && cands.length) {
      body.innerHTML = '<div class="hint">同名多路且指向不同文件，请选一路：</div>' +
        cands.map(c => '<div class="mem-item" style="cursor:pointer" data-sk-view="1" data-name="' +
          escapeHtml(SKILL_DOC.name) + '" data-route="' + escapeHtml(String(c.route || '')) + '">' +
          '<b>' + escapeHtml(String(c.route || '')) + '</b> <span class="hint">' +
          escapeHtml(String(c.path || '')) + (c.via_symlink ? '（软链）' : '') + '</span></div>').join('');
    } else {
      body.textContent = '读取失败：' + (e.message || e);
    }
  }
}

/* 抽屉元信息 + 分布路由：全部取自 /api/skill/list 已拉回的 SKILLS（**不发新请求**，
 * 页面不存第二个真相）。找不到该项（竞态删了）就如实说，不编。 */
function skillRenderDocMeta() {
  const meta = $('skillDocMeta'), routesEl = $('skillDocRoutes');
  if (!meta || !routesEl) return;
  const s = (SKILLS || []).find(x => x.name === SKILL_DOC.name);
  if (!s) {
    meta.innerHTML = '<span class="hint">列表中已找不到该技能（可能刚被卸载）——正文仍可按名读取。</span>';
    routesEl.innerHTML = '<span class="hint">—</span>';
    return;
  }
  const fmtB = n => (n == null ? '—' : (n > 1024 ? (n / 1024).toFixed(1) + ' KB' : n + ' B'));
  meta.innerHTML =
    kv('整包大小', fmtB(s.pkg_bytes) + '（' + (s.pkg_files || 0) + ' 个文件）') +
    kv('SKILL.md', fmtB(s.bytes) + (s.fm ? '' : ' · <b>缺 frontmatter</b>')) +
    kv('指纹', '<span class="sk-fp" title="' + escapeHtml(String(s.pkg_digest || '')) + '">' +
      escapeHtml(String(s.pkg_digest || '—').slice(0, 12)) + '</span>') +
    kv('到达方式', s.via_symlink ? '软链' : '实体目录') +
    kv('编码', escapeHtml(String(s.encoding || '—')));
  const routes = s.routes || [s.route];
  routesEl.innerHTML = routes.map(r => {
    const kind = (r === 'local') ? '项目本地' : '全局';
    return kv(escapeHtml(String(r)), kind + (s.via_symlink ? ' · 软链到达' : ''));
  }).join('') || '<span class="hint">—</span>';
  function kv(k, v) { return '<div>' + k + '：<b>' + v + '</b></div>'; }
}

/* 抽屉调用统计：/api/skill/invocations?name=…（hub 通道口径，title/文案写清）。
 * 失败不炸抽屉——统计区单独表态，正文照常可看。 */
async function skillLoadDocUsage() {
  const usage = $('skillDocUsage');
  if (!usage) return;
  usage.innerHTML = '读取中…';
  try {
    const d = await api('/api/skill/invocations?name=' + encodeURIComponent(SKILL_DOC.name) + '&days=30&limit=20');
    const n = d.matched_total != null ? d.matched_total : 0;
    const byCaller = (d.summary && d.summary.by_caller) || {};
    const callers = Object.keys(byCaller).map(c => escapeHtml(c) + ' × ' + byCaller[c]).join('，') || '无';
    const last = (d.items && d.items.length) ? d.items[0].created_at : null;
    usage.innerHTML = n
      ? '近 30 天被显式调用 <b>' + n + '</b> 次 · 调用方：' + callers +
        (last ? ' · 最近一次 ' + escapeHtml(String(last)) : '') +
        '<br><span class="hint">口径：hub 通道（/api/skill/invoke）遥测；各家 agent 直读磁盘的旁路未接入 ⇒ 这是**下限**不是总量。</span>'
      : '<span class="hint">近 30 天无 hub 通道调用记录（=下限为 0，不等于没人用）。</span>';
  } catch (e) {
    usage.innerHTML = '<span class="hint">调用统计不可用：' + escapeHtml(e.message || e) + '</span>';
  }
}

/* 抽屉「试调用」（v0.13.127 P1，Grill 裁定 1/3）：POST /api/skill/invoke。
 * caller=web 是**自报**（服务端落 caller_reported=true），task 固定文案说明来源。
 * 成功：回指令正文直接渲染进正文区（用户要的就是「拿到指令」），并刷新统计与徽标。 */
async function skillInvokeCur() {
  if (!SKILL_DOC.name) { toast('先点一条技能', 'err'); return; }
  const body = $('skillDocBody');
  if (body) body.textContent = '调用中…';
  try {
    const d = await api('/api/skill/invoke', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: SKILL_DOC.name, route: SKILL_DOC.route || '',
        caller: 'web', task: 'web 面板试调用（用户点击抽屉「试调用」）' }) });
    let txt = d.content || d.instructions || '';
    if (d.truncated) {
      txt += '\n\n[正文 ' + (d.bytes_total || '?') + 'B 超过读取上限已截断；需要全文请直接读磁盘路径 ' + (d.realpath || '?') + ']';
    }
    if (body) body.textContent = txt || '（技能无正文）';
    toast('已激活并记账：' + SKILL_DOC.name + '（caller=web，subject=skill.invoke）', 'ok');
    skillLoadDocUsage();
    loadSkillInvBadges();
    if (SKILL_VIEW === 'invocations') loadSkillInvocations();
  } catch (e) {
    const cands = (e.payload && e.payload.detail && e.payload.detail.candidates) || [];
    if (e.http === 409 && cands.length && body) {
      body.innerHTML = '<div class="hint">同名多路且指向不同文件，请选一路：</div>' +
        cands.map(c => '<div class="mem-item" style="cursor:pointer" data-sk-view="1" data-name="' +
          escapeHtml(SKILL_DOC.name) + '" data-route="' + escapeHtml(String(c.route || '')) + '">' +
          '<b>' + escapeHtml(String(c.route || '')) + '</b> <span class="hint">' +
          escapeHtml(String(c.path || '')) + (c.via_symlink ? '（软链）' : '') + '</span></div>').join('');
    } else if (body) {
      body.textContent = '调用失败：' + (e.message || e);
    }
    toast('调用失败：' + (e.message || e), 'err');
  }
}

/* 复制接入片段（裁定 2「统一调用接口」的落地面）：给 REST 与 MCP 两种形态。
 * 片段同时**显示**在抽屉里（#skillDocSnippet）——复制失败（局域网 http 非安全上下文
 * 常见）时用户还能手动选学。复制走 settingsLogsCopyText 的两级兜底（clipboard →
 * execCommand），不新造第三份实现。 */
async function skillCopySnippet() {
  if (!SKILL_DOC.name) { toast('先点一条技能', 'err'); return; }
  const nm = SKILL_DOC.name, rt = SKILL_DOC.route || '';
  const base = location.origin;
  const rest = [
    'curl -s -X POST ' + base + '/api/skill/invoke \\',
    "  -H 'x-hub-token: <TERM_TOKEN>' \\",
    "  -H 'Content-Type: application/json' \\",
    '  -d \'{"name": "' + nm + '", "caller": "my-agent", "task": "<要干什么>"}\'',
  ].join('\n');
  const mcp = 'MCP 工具 hub_skill_invoke(name="' + nm + '", caller="my-agent", task="<要干什么>")';
  const txt = '# REST\n' + rest + '\n\n# MCP（hub MCP server）\n' + mcp +
    '\n\n# 返回 SKILL.md 指令正文；执行由你（agent 侧）完成，hub 不代跑代码。';
  const box = $('skillDocSnippet');
  if (box) {
    box.style.display = '';
    box.innerHTML = '<div class="sp-sub">接入片段（REST / MCP）</div>' + escapeHtml(txt);
  }
  if (await settingsLogsCopyText(txt)) {
    toast('接入片段已复制（REST + MCP）', 'ok');
  } else {
    toast('复制失败：浏览器不给剪贴板权限（局域网 http 常见）——片段已显示在抽屉里，可手动选择复制', 'err');
  }
}

/* 抽屉内安装：目标路下拉（skInstTo）→ POST /api/skill/install（软链，不动本体）。
 * local 路是只读发现面，install 不接受它——下拉里就不该出现。 */
function skillFillInstTo(route) {
  const sel = $('skInstTo');
  if (!sel) return;
  const opts = (SKILL_ROUTES || []).filter(r => r !== route && r !== 'local' && r !== 'tdai')
    .map(r => '<option value="' + escapeHtml(r) + '">' + escapeHtml(r) + '</option>').join('');
  sel.innerHTML = opts || '<option value="">（无可用目标）</option>';
}
async function skillInstallFromDrawer() {
  const to = $('skInstTo') && $('skInstTo').value;
  if (!SKILL_DOC.name || !SKILL_DOC.route) { toast('缺少技能或源路', 'err'); return; }
  if (!to) { toast('没有可选目标发现点', 'err'); return; }
  try {
    const d = await api('/api/skill/install', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: SKILL_DOC.name, from_route: SKILL_DOC.route, targets: [to] }) });
    toast('已软链到 ' + to + '：' + ((d.created || []).join(', ') || '（幂等跳过，已存在）'), 'ok');
    loadSkills();
  } catch (e) { toast(e.message, 'err'); }
}

/* ── 调用记录视图（v0.13.127 P1，裁定 3）────────────────────────────────────
 * 数据源 GET /api/skill/invocations：profile_events 里 subject='skill.invoke' 的
 * 只读投影（零新表）。坏 detail 行由服务端跳过并计数（bad_detail）——面板照常渲染。
 * 汇总口径是**抓取窗口**（window_fetched），界面必须照抄这个 scope，不许写成「全部历史」。 */
async function loadSkillInvocations() {
  boxBusy('skillInvocations');
  const days = ($('invDays') && $('invDays').value) || '7';
  try {
    const d = await api('/api/skill/invocations?days=' + encodeURIComponent(days) + '&limit=100');
    SKILL_INV = d;
    SKILL_INV_LOADED = true;
    renderSkillInvocations();
  } catch (e) {
    boxFail('skillInvocations', e, 'loadSkillInvocations');
  }
}

/* 客户端二次过滤（名称/调用方）：与目录视图同一口径——**也必须走 fuzzyMatch**
 * （v0.13.71 起全仓搜索框统一；test_search_fuzzy 反向扫「小写化 + 子串判断」旧写法）。
 * 调用流水里按 caller 找人名/按 name 找技能，手误同样要能容忍。 */
function renderSkillInvocations() {
  const sumEl = $('skillInvSummary'), el = $('skillInvocations');
  if (!el) return;
  if (!SKILL_INV) { el.innerHTML = '<div class="hint">加载中…</div>'; return; }
  const wantName = (($('invName') || {}).value || '').trim();
  const wantCaller = (($('invCaller') || {}).value || '').trim();
  const items = (SKILL_INV.items || []).filter(x =>
    (!wantName || fuzzyMatch([[x.name, 3]], wantName) > 0) &&
    (!wantCaller || fuzzyMatch([[x.caller, 3]], wantCaller) > 0));
  if (sumEl) {
    const byCaller = (SKILL_INV.summary && SKILL_INV.summary.by_caller) || {};
    const bySkill = (SKILL_INV.summary && SKILL_INV.summary.by_skill) || {};
    const callerTxt = Object.keys(byCaller).map(c => escapeHtml(c) + ' × ' + byCaller[c]).join('，') || '无';
    const hot = Object.keys(bySkill).slice(0, 3).map(k => escapeHtml(k) + ' × ' + bySkill[k]).join('，') || '无';
    sumEl.innerHTML = '窗口内 <b>' + (SKILL_INV.matched_total != null ? SKILL_INV.matched_total : 0) +
      '</b> 条 · 按调用方：' + callerTxt + ' · 最热：' + hot +
      '<br><span class="hint">汇总口径＝本次抓取窗口（' + (SKILL_INV.window_fetched || 0) + ' 条，上限 ' +
      (SKILL_INV.fetch_cap || 0) + '），不是全历史' +
      (SKILL_INV.bad_detail ? '；' + SKILL_INV.bad_detail + ' 条坏账已跳过' : '') + '</span>';
  }
  el.innerHTML = items.map(x => {
    const st = x.status === 'ok' ? '' : '<span class="badge" style="color:var(--warn)">' + escapeHtml(String(x.status || '?')) + '</span>';
    return '<div class="mem-item"><span class="tag agent" style="align-self:flex-start">' +
      escapeHtml(String(x.name || '（无名次）')) + '</span>' +
      '<p>caller <b>' + escapeHtml(String(x.caller || 'anonymous')) + '</b>' +
      (x.caller_reported ? ' <span class="hint">（自报）</span>' : '') + st +
      (x.task ? '<br>' + mdInline(String(x.task).slice(0, 120)) : '') +
      '<br><span class="hint">' + escapeHtml(String(x.created_at || '')) +
      ' · ' + (x.duration_ms != null ? x.duration_ms + 'ms' : '') + '</span></p></div>';
  }).join('') || '<div class="hint">窗口内没有调用记录' +
    (wantName || wantCaller ? '（已被过滤掉；清空过滤框再看）' : '——还没有任何 agent/用户通过 hub 调用过技能') + '</div>';
}

function closeSkillDoc() { closeOverlay('skillDocDrawer'); }

async function skillInstall() {
  const name = $('instName').value;
  const from = $('instFrom').value;
  const to = Array.from($('instTo').selectedOptions).map(o => o.value).filter(t => t !== from);
  if (!name || !from || !to.length) { toast('选择技能、源与至少一个目标', 'err'); return; }
  try {
    const d = await api('/api/skill/install', { method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ name: name, from_route: from, targets: to }) });
    toast('已软链：' + (d.created || []).join(', ') + (d.existed && d.existed.length ? '（幂等跳过 ' + d.existed.join(', ') + '）' : ''), 'ok');
    loadSkills();
  } catch (e) { toast(e.message, 'err'); }
}

async function loadSkillBudget() {
  const tok = parseInt($('skillTok').value, 10) || 1200;
  const route = ($('skillBudgetRoute') && $('skillBudgetRoute').value) || '';
  boxBusy('skillBudget');
  try {
    const d = await api('/api/skill/budget?max_tokens=' + tok + (route ? '&route=' + encodeURIComponent(route) : ''));
    /* 下拉选项由后端 routes_available 填充——不写死路名，磁盘加一路就自动多一项；
       dataset.filled 防重复追加（面板会反复刷）。 */
    const sel = $('skillBudgetRoute');
    if (sel && !sel.dataset.filled && (d.routes_available || []).length) {
      (d.routes_available || []).forEach(r => {
        const o = document.createElement('option');
        o.value = r; o.textContent = r; sel.appendChild(o);
      });
      sel.dataset.filled = '1';
      if (route) sel.value = route;
    }
    const full = (d.full || []).map(f => escapeHtml(f.name) + (f.description ? ' — ' + mdInline(f.description.slice(0, 80)) : ''));
    const names = (d.name_only || []).map(n => escapeHtml(n));
    // 三档分区：full（全描述）/ name_only（仅名）/ truncated（被裁）
    const pct = Math.min(100, Math.round((d.used_est || 0) * 100 / Math.max(1, d.budget || 1)));
    const bar = $('skillBudgetBar'), fill = $('skillBudgetFill');
    if (bar && fill) {
      fill.style.width = pct + '%';
      bar.setAttribute('aria-valuenow', String(pct));
      bar.setAttribute('aria-label', '注入预算占用 ' + pct + '%');
      bar.classList.toggle('over', (d.used_est || 0) > (d.budget || 1));
    }
    const scope = (d.route_filter && d.route_filter.length) ? d.route_filter.join(',') : '全部路';
    const unknown = (d.route_unknown || []).length ? ' · <b>未知路 ' + d.route_unknown.map(escapeHtml).join(',') + '</b>' : '';
    $('skillBudget').innerHTML =
      '范围 ' + escapeHtml(scope) + ' · 预算 ' + d.budget + ' tok · 实际约 ' + d.used_est + '（' + pct + '%） · 全条目 ' + full.length + ' / 仅名 ' + names.length +
      (d.truncated ? ' / <b>被裁 ' + d.truncated + '</b>' : '') + '（共 ' + d.total + '）' + unknown + '<br>' +
      full.concat(names).map(s => '· ' + s).join('<br>') +
      '<br><span class="hint">被裁 = 预算不够时连名字都留不下 ⇒ 注入时 agent <b>看不到</b>它。' +
      '本面板只预览清单；agent 实际注入走 /api/skill/relevant（BM25 top-3），不受此预算约束。</span>';
  } catch (e) { boxFail('skillBudget', e, 'loadSkillBudget'); }
}

/* ── 知识库中心（v0.13.26 批4）──────────────────── */

async function kbSearch() {
  const q = $('kbQ').value.trim();
  if (!q) { toast('输入检索词', 'err'); return; }
  const routes = Array.from($('kbRoutes').selectedOptions).map(o => o.value).join(',');
  $('kbHint').textContent = '检索中…';
  boxBusy('kbResults');
  try {
    const d = await api('/api/kb/search?q=' + encodeURIComponent(q) + '&routes=' + encodeURIComponent(routes) + '&k=12');
    CENTER_HEALTH.kb = 'ok';
    $('kbHint').textContent = d.count + ' 条 · ' + d.took_ms + 'ms · ' + (d.engine || '');
    /* v0.13.28 批3/批4：逐路徽标（与记忆页 fedBadges 同构——替换只 toast 的半吊子表态） */
    const badges = $('kbBadges');
    if (badges) badges.innerHTML = fedBadges(d);
    renderQueryBar('kbQueryBar', q, d.count || 0, 'kbClear');
    $('kbResults').innerHTML = (d.results || []).map(r => {
      /* v0.13.28 批4：源徽标走 SRC_ABBR + src-* 三变体（替换裸拼 `tag turbovec`——
         CSS 无定义静默灰底的根因）；路径行可溯源，workspace/archived 命中且首段
         是 KB 四根之一 ⇒ 下钻按钮（复用 kbBrowse 单层能力，零后端改动）。 */
      const srcName = String(r.source || r.from || '');
      const path = String(r.path || (r.id || '').replace(/:\d+$/, '') || '');
      const seg = path.split('/').filter(Boolean)[0] || '';
      const KB_ROOTS = ['MEMORY.md', 'memory', 'agent-knowledge', 'digest'];
      const drill = (srcName === 'workspace_files' || srcName === 'archived_sessions') &&
                    KB_ROOTS.indexOf(seg) >= 0
        ? ' <button class="btn ghost sm" onclick="kbBrowse(\'' + escapeHtml(seg) + '\')" title="在文档树中查看">↖ ' + escapeHtml(seg) + '</button>' : '';
      return '<div class="mem-item">' + srcTag(srcName) +
      '<p>' + escapeHtml(String(r.title || r.path || r.id || '').slice(0, 100)) +
      '<br><span class="hint">' + escapeHtml(String(r.content || r.preview || '').slice(0, 200)) + '</span>' +
      (path ? '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' + escapeHtml(path.slice(0, 120)) + '</span>' + drill : '') +
      '</p></div>';
    }).join('') ||
      '<div class="hint">零命中。' + (d.note || '') + '</div>';
    // 逐路表态：哪路挂了要说出来（不许全绿假装没挂）——徽标行已表达，toast 保留作主动提醒
    const bad = (d.backends || []).filter(b => !b.ok);
    if (bad.length) {
      CENTER_HEALTH.kb = 'warn';
      toast('降级路：' + bad.map(b => b.name).join(', '), 'err');
    }
  } catch (e) {
    CENTER_HEALTH.kb = 'err';
    boxFail('kbResults', e, 'kbSearch');
  }
}
function kbClear() {
  const bar = $('kbQueryBar');
  if (bar) { bar.style.display = 'none'; bar.innerHTML = ''; }
  const results = $('kbResults');
  if (results) results.innerHTML = '输入关键词开始检索';
  const badges = $('kbBadges');
  if (badges) badges.innerHTML = '';
  const hint = $('kbHint');
  if (hint) hint.textContent = '';
  const input = $('kbQ');
  if (input) input.value = '';
  /* CENTER_HEALTH 故意不清（与 memFedClear 同口径） */
}

async function loadKbStatus() {
  boxBusy('kbStatus');
  try {
    const d = await api('/api/kb/status');
    const seg = (name, v) => '<div><b>' + name + '</b>：' +
      (v.available ? '✅ 可用' : '❌ 不可用') +
      (v.count != null ? ' · ' + v.count : '') +
      (v.chunks ? ' chunks' : '') +
      (v.index_mtime_age_days != null ? ' · 索引 ' + v.index_mtime_age_days + ' 天前' : '') +
      (v.ms != null ? ' · ' + v.ms + 'ms' : '') +
      (v.error ? ' · <span style="color:var(--red)">' + escapeHtml(String(v.error).slice(0, 120)) + '</span>' : '') + '</div>';
    /* local_memory 用 state（fresh/stale/empty）而非 available——旧行硬套
       available 三元判 undefined ⇒ 永远渲染 ❌ 不可用，把「陈旧便签」演成「挂了」
       （2026-09-26 用户误报记忆模块不可用的直接观感来源）。如实三态：
       fresh=✅ / stale=⚠ 陈旧（不参与决策，非故障） / empty=⭕ 空。 */
    const segState = (name, v) => {
      const st = v.state || 'unknown';
      const icon = st === 'fresh' ? '✅' : st === 'stale' ? '⚠️' : '⭕';
      const tag = st === 'stale' ? ' 陈旧便签（非故障，主源在 TDAI）' : st === 'empty' ? ' 空' : '';
      return '<div><b>' + name + '</b>：' + icon + st + tag +
        (v.rows != null ? ' · ' + v.rows + ' 行' : '') +
        (v.authoritative_source ? ' · 主源 ' + v.authoritative_source : '') +
        (v.reason ? ' · <span class="hint">' + escapeHtml(String(v.reason).slice(0, 100)) + '</span>' : '') + '</div>';
    };
    $('kbStatus').innerHTML =
      seg('TDAI L1', d.tdai || {}) +
      seg('XERJ 全文', d.xerj || { available: false, error: '后端未重载（重启 agenthub 后生效）' }) +
      seg('workspace 全文', d.workspace || {}) +
      seg('archived 会话备份', d.archived || {}) +
      segState('local 便签', d.local_memory || {});
  } catch (e) { boxFail('kbStatus', e, 'loadKbStatus'); }
}

/* 文档树浏览：sub=根名单层下钻（后端 kb.py 白名单校验，只支持一层——
 * 根内子目录如实渲染为不可点+hint，不扩后端）。面包屑 KB_SUB 记当前层。
 * var 防外层引用时的 TDZ 静态序风险（test_tdz_order 钉着）。 */
var KB_SUB = '';
async function kbBrowse(sub) {
  KB_SUB = sub || '';
  boxBusy('kbBrowse');
  try {
    const d = await api('/api/kb/browse' + (KB_SUB ? '?sub=' + encodeURIComponent(KB_SUB) : ''));
    const crumb = $('kbCrumb');
    if (crumb) crumb.innerHTML = KB_SUB
      ? '<button class="btn ghost sm" onclick="kbBrowse()">知识库根</button> ▸ ' + escapeHtml(KB_SUB)
      : '<span class="hint">知识库根（点目录名下钻一层）</span>';
    $('kbBrowse').innerHTML = (d.entries || []).map(e => {
      const meta = e.files != null ? e.files + ' 文件' : Math.round((e.size || 0) / 1024) + 'KB';
      // 顶层（无 sub）目录条目可点下钻；进了 sub 之后，后端只支持单层——如实说
      const clickable = !KB_SUB && e.dir;
      return clickable
        ? '<div class="mem-item" style="cursor:pointer" onclick="kbBrowse(\'' + escapeHtml(e.name) + '\')" title="下钻 ' + escapeHtml(e.name) + '">' +
          '📁 <b>' + escapeHtml(e.name) + '</b> <span class="hint">' + meta + '</span></div>'
        : '<div class="mem-item">' + (e.dir ? '📁' : '📄') + ' ' + escapeHtml(e.name) +
          ' <span class="hint">' + meta + (KB_SUB && e.dir ? '（暂只支持下钻一层）' : '') + '</span></div>';
    }).join('') || '空目录';
    if (d.errors && d.errors.length) {
      $('kbBrowse').innerHTML += '<div class="hint" style="color:var(--st-error,var(--danger))">' +
        d.errors.map(escapeHtml).join('<br>') + '</div>';
    }
  } catch (e) { boxFail('kbBrowse', e, 'kbBrowse'); }
}

/* ── 端口 ─────────────────────────────────────────── */

async function loadPorts() {
  try { const d = await api('/api/ports'); PORTS = d.listeners || []; renderPorts(); }
  catch (e) { toast(e.message, 'err'); }
}
function renderPorts() {
  const f = ($('portFilter') ? $('portFilter').value.trim() : '').toLowerCase();
  // 模糊：端口号与进程名分开打分，不用「拼成一个大串」——串起来后编辑距离会把
  // 分隔符也算进失配，`80` 搜 `8080` 会被四个无关空格抵掉（实测 `includes` 反而能用，
  // 故此处保留精确快路：fuzzyMatch 内部先判子串，只有子串不中才走编辑距离）。
  const rows = PORTS.filter(r => !f || fuzzyMatch([
    [r.port, 3], [r.process, 2], [r.address, 1], [r.agent, 1]], f) > 0);
  $('portsBody').innerHTML = rows.map(r =>
    '<tr><td>' + r.proto + '</td><td><b>' + r.port + '</b></td><td style="font-family:var(--font-mono);font-size:var(--fs-sm)">' + escapeHtml(r.address) + '</td>' +
    '<td>' + (r.pid || '-') + '</td><td>' + escapeHtml(r.process || '-') + '</td>' +
    '<td>' + (r.agent ? '<span class="tag agent">' + escapeHtml(r.agent) + '</span>' : '') + '</td></tr>').join('');
}

/* ── 遥测 ─────────────────────────────────────────── */

async function loadTelemetry() {
  $('curlExample').textContent =
    '# Claude/pi 等 Agent 会话结束后上报用量（同 session 重报=覆盖）\n' +
    "curl -X POST http://" + location.hostname + ":3102/telemetry/events/claude \\\n" +
    "  -H 'Content-Type: application/json' \\\n" +
    "  -d '{\"session_id\":\"abc123\",\"event\":\"session_usage\",\"cwd\":\"/path/proj\",\n" +
    "       \"usage\":{\"input_tokens\":1088,\"output_tokens\":256,\"cached_tokens\":1024},\n" +
    "       \"usage_scope\":\"session\"}'";
  try {
    const u = await api('/telemetry/usage/summary');
    const rows = Object.entries(u.by_source || {});
    $('usageTable').innerHTML = rows.length ?
      '<table><thead><tr><th>来源</th><th>会话</th><th>输入</th><th>输出</th><th>缓存命中</th></tr></thead><tbody>' +
      rows.map(([k, v]) => '<tr><td><b>' + k + '</b></td><td>' + v.sessions + '</td><td>' + v.input_tokens + '</td><td>' + v.output_tokens + '</td><td>' + v.cached_tokens + '</td></tr>').join('') +
      '</tbody></table>' : '<div class="hint">暂无 session_usage 数据</div>';
    const prof = u.profile || [];
    $('profTable').innerHTML = prof.length ?
      '<table><thead><tr><th>来源</th><th>对象</th><th>次数</th><th>成功率</th><th>均耗时</th><th>峰值</th></tr></thead><tbody>' +
      prof.map(p => '<tr><td>' + p.source + '</td><td><b>' + escapeHtml(p.subject) + '</b></td><td>' + p.calls +
        '</td><td>' + (p.success_rate >= 99 ? ico('check-circle', 'xs') : p.success_rate >= 80 ? ico('circle', 'xs') : ico('alert', 'xs')) + ' ' + p.success_rate + '%</td>' +
        '<td>' + p.avg_ms + 'ms</td><td>' + (p.max_ms || '-') + 'ms</td></tr>').join('') + '</tbody></table>'
      : '<div class="hint">暂无画像数据（对话/指挥官/DAG/定时执行后自动生成）</div>';
    const e = await api('/telemetry/events?limit=20');
    $('eventTable').innerHTML = '<table><thead><tr><th>时间</th><th>来源</th><th>会话</th><th>事件</th></tr></thead><tbody>' +
      (e.events || []).map(x => '<tr><td>' + (x.created_at || '').slice(5, 16).replace('T', ' ') + '</td><td>' + escapeHtml(x.source) + '</td><td style="font-family:var(--font-mono);font-size:var(--fs-sm)">' + escapeHtml((x.session_id || '').slice(0, 14)) + '</td><td>' + escapeHtml(x.event) + '</td></tr>').join('') +
      '</tbody></table>';
  } catch (err) { toast(err.message, 'err'); }
}

/* ── S2 自动扫描 ──────────────────────────────────── */

async function runScan() {
  toast('扫描发现源（docker / systemd / CLI 名单）…');
  try {
    const d = await api('/api/scan/run', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{"auto_register":true}' });
    toast('发现 ' + d.found.length + '，新增 ' + d.added.length + '，已存在 ' + d.skipped_existing.length, 'ok');
    loadAgents();
  } catch (e) { toast(e.message, 'err'); }
}

/* ── S3 协同 DAG ──────────────────────────────────── */

let currentRun = null;

async function decomposeRun() {
  const goal = $('taskGoal').value.trim();
  if (!goal) return toast('请输入目标', 'err');
  const btn = $('btnDecompose'); btn.disabled = true; btn.textContent = '拆解中…';
  try {
    const body = { goal: goal, auto_run: true };
    if ($('taskAgent').value) body.default_agent = $('taskAgent').value;
    const d = await api('/api/tasks/decompose', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    currentRun = d.run_id;
    toast('已拆解 ' + d.tasks.length + ' 个子任务并启动', 'ok');
    loadRuns(); openRun(d.run_id);
  } catch (e) { toast(e.message, 'err'); }
  btn.disabled = false; btn.textContent = '拆解并启动';
}

async function loadRuns() {
  try {
    const d = await api('/api/tasks/runs');
    $('runsBody').innerHTML = (d.runs || []).map(r =>
      '<tr><td style="font-family:var(--font-mono)">' + r.run_id + '</td>' +
      '<td style="max-width:260px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(r.goal || '') + '">' + escapeHtml((r.goal || '').slice(0, 40)) + '</td>' +
      '<td>' + ({ running: ico('play', 'xs') + '执行中', success: ico('check-circle', 'xs') + '成功', failed: ico('alert', 'xs') + '失败', partial: ico('circle', 'xs') + '部分', pending: ico('clock', 'xs') + '待跑' }[r.state] || r.state) + '</td>' +
      '<td>' + r.success + '/' + r.total + '</td><td class="hint">' + (r.created_at || '').slice(5, 16).replace('T', ' ') + '</td>' +
      '<td><button class="btn sm ghost" onclick="openRun(\'' + r.run_id + '\')">查看</button></td></tr>').join('') ||
      '<tr><td colspan="6" class="hint">尚无协同任务</td></tr>';
  } catch (e) { /* ignore */ }
}

async function openRun(runId) {
  currentRun = runId;
  try {
    const d = await api('/api/tasks/' + runId);
    $('runPanel').style.display = '';   // v0.13.40：卡是 flex 列，显隐只切 inline 值
    $('runTitle').textContent = 'Run ' + runId + ' · ' + (d.goal || '').slice(0, 60);
    renderTaskTable(d.tasks);
    renderDag(d.tasks);
  } catch (e) { toast(e.message, 'err'); }
}

// 色值必须是具体十六进制：下面 fill = 描边色 + '22' 透明度派生，拿到 "var(--x)" 会变成非法色值
const TASK_COLORS = {
  pending:  cssToken('--muted', '#6b6b6b'),
  running:  cssToken('--busy', '#2c5f8a'),
  success:  cssToken('--ok', '#2f7a4f'),
  failed:   cssToken('--danger', '#a3342c'),
  blocked:  cssToken('--warn', '#8a5a00')
};

function renderTaskTable(tasks) {
  $('taskBody').innerHTML = tasks.map(t =>
    '<tr><td><b>' + escapeHtml(t.task_id) + '</b></td><td>' + escapeHtml(t.agent_id || '-') + '</td>' +
    '<td>' + escapeHtml((t.deps || []).join(',')) + '</td><td>' + escapeHtml(t.status) + '</td>' +
    '<td>' + (t.duration_ms != null ? t.duration_ms + 'ms' : '-') + '</td>' +
    '<td><details><summary class="hint">' + escapeHtml((t.output || t.error || '').slice(0, 50)) + '</summary><pre style="white-space:pre-wrap;font-size:var(--fs-sm);max-height:220px;overflow:auto">' + escapeHtml(t.output || t.error || '') + '</pre></details></td></tr>').join('');
}

function renderDag(tasks) {
  const byId = {};
  tasks.forEach(t => byId[t.task_id] = t);
  const depthMemo = {};
  function depth(id, guard) {
    guard = guard || new Set();
    if (depthMemo[id] != null) return depthMemo[id];
    if (guard.has(id)) return 0;
    guard.add(id);
    const t = byId[id];
    const dv = (t && t.deps && t.deps.length) ? 1 + Math.max(...t.deps.map(d => depth(d, guard))) : 0;
    depthMemo[id] = dv;
    return dv;
  }
  const layers = {};
  tasks.forEach(t => { const L = depth(t.task_id); (layers[L] = layers[L] || []).push(t); });
  const NW = 170, NH = 56, GX = 220, GY = 86;
  const maxRows = Math.max(1, ...Object.values(layers).map(l => l.length));
  const W = (Math.max(...Object.keys(layers).map(Number), 0) + 1) * GX + 40;
  const H = maxRows * GY + 20;
  const pos = {};
  Object.entries(layers).forEach(([L, list]) => list.forEach((t, i) => { pos[t.task_id] = { x: 30 + L * GX, y: 20 + i * GY }; }));
  let svg = '<svg width="' + W + '" height="' + H + '" style="min-width:' + W + 'px">';
  const DAG_LINE = cssToken('--muted', '#6b6b6b');
  const DAG_ST = n => cssNum(n, 13);
  svg += '<defs><marker id="arw" viewBox="0 0 8 8" refX="7" refY="4" markerWidth="6" markerHeight="6" orient="auto"><path d="M0,0L8,4L0,8z" fill="' + DAG_LINE + '"/></marker></defs>';
  tasks.forEach(t => (t.deps || []).forEach(dp => {
    const a = pos[dp], b = pos[t.task_id];
    if (a && b) svg += '<line x1="' + (a.x + NW) + '" y1="' + (a.y + NH / 2) + '" x2="' + b.x + '" y2="' + (b.y + NH / 2) + '" stroke="' + DAG_LINE + '" stroke-width="1.5" marker-end="url(#arw)"/>';
  }));
  tasks.forEach(t => {
    const p = pos[t.task_id];
    svg += '<g><rect x="' + p.x + '" y="' + p.y + '" width="' + NW + '" height="' + NH + '" rx="10" fill="' + (TASK_COLORS[t.status] || cssToken('--border')) + '22" stroke="' + (TASK_COLORS[t.status] || cssToken('--border')) + '" stroke-width="1.5"/>' +
      '<text x="' + (p.x + 8) + '" y="' + (p.y + 18) + '" fill="' + cssToken('--text-1') + '" font-size="' + DAG_ST('--fs-xs') + '" font-weight="bold">' + escapeHtml(t.task_id) + ' · ' + escapeHtml(t.agent_id || '') + '</text>' +
      '<text x="' + (p.x + 8) + '" y="' + (p.y + 34) + '" fill="' + cssToken('--text-2') + '" font-size="' + DAG_ST('--fs-xs') + '">' + escapeHtml((t.prompt || '').slice(0, 18)) + '</text>' +
      '<text x="' + (p.x + 8) + '" y="' + (p.y + 48) + '" fill="' + cssToken('--info') + '" font-size="' + DAG_ST('--fs-xs') + '">' + escapeHtml(t.status) + (t.duration_ms != null ? ' · ' + Math.round(t.duration_ms / 100) / 10 + 's' : '') + '</text></g>';
  });
  svg += '</svg>';
  $('dagSvg').innerHTML = svg;
}

async function startRun(id) { if (!id) return; try { await api('/api/tasks/' + id + '/start', { method: 'POST' }); toast('已调度', 'ok'); setTimeout(() => openRun(id), 1200); } catch (e) { toast(e.message, 'err'); } }
async function retryRun(id) { if (!id) return; try { await api('/api/tasks/' + id + '/retry', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: '{}' }); toast('失败项已重置', 'ok'); setTimeout(() => openRun(id), 1200); } catch (e) { toast(e.message, 'err'); } }
async function delRun(id) { if (!id || !confirm('删除 run ' + id + '？')) return; try { await api('/api/tasks/' + id, { method: 'DELETE' }); currentRun = null; $('runPanel').style.display = 'none'; loadRuns(); } catch (e) { toast(e.message, 'err'); } }

/* ── S4 定时任务 ──────────────────────────────────── */

async function createJob() {
  const body = { name: $('jobName').value.trim(), cron: $('jobCron').value.trim(), kind: $('jobKind').value, payload: $('jobPayload').value.trim() };
  if (!$('jobName').value.trim() || !body.cron || !body.payload) return toast('名称/cron/载荷必填', 'err');
  if ($('jobAgent').value) body.agent_id = $('jobAgent').value;
  try {
    await api('/api/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast('已创建', 'ok'); $('jobName').value = $('jobCron').value = $('jobPayload').value = '';
    loadJobs();
  } catch (e) { toast(e.message, 'err'); }
}

/* ── 会话导出（件 1 / spec §3）──────────────────────────────────────────
   三个实测出来的坑，决定了这里为什么长这样：
   ① 不能走 api()：01-core-boot.js 的 isWriteMethod() 只给 POST/PUT/PATCH/DELETE 带 token，
      而 /api/sessions/export 是 **GET 却要写级鉴权**（后端显式 writeauth.decide("POST",…)）⇒ 必 401。
   ② 不能用 api() 取体：它会 JSON.parse 成对象，CSV/JSON **文件字节**就毁了 ⇒ 必须 blob。
   ③ token 不走 ?token=：那会进服务端访问日志与浏览器历史（本工作区三次凭据外流前例）⇒ 只走头。
   文案四态互斥（照抄 07-asset-panel.js 的红向口径）：**被拒绝不许说成"没有会话"**。
   窄屏口径：只加 1 个图标按钮，不做 format/redact 的一排开关（chrome 单行化优先级更高）；
   CSV 与 redact=0 走 API 参数，不在本轮做 UI（YAGNI，且给原文出口做 UI 需单独裁定）。 */
function exportStateOf(status, count, detail) {
  if (status === 503) return 'misconfig';
  if (status === 401) return /缺少凭据/.test(String(detail || '')) ? 'need-token' : 'bad-token';
  if (status !== 200) return 'error';
  return (count > 0) ? 'ok' : 'empty';
}

function exportStateText(state, count, extra) {
  const n = (count == null || count < 0) ? '?' : String(count);
  const code = (extra && extra.status) ? String(extra.status) : '?';
  const hits = (extra && typeof extra.hits === 'number') ? extra.hits : 0;
  if (state === 'ok') {
    return '已导出 ' + n + ' 条会话（默认脱敏，命中 ' + hits + ' 处' +
           (hits > 0 ? '，正文已打码）' : '）');
  }
  if (state === 'empty') return '导出成功，但这个范围内确实是 0 条会话（文件是空的，不是被拒）';
  if (state === 'need-token') return '导出被拒：未提供凭据 —— 不是没有会话。请在「设置」里应用 TERM_TOKEN 后重试';
  if (state === 'bad-token') return '导出被拒：凭据不匹配 —— 不是没有会话。存量口令可能已换过，已清除，请重新输入';
  if (state === 'misconfig') return '导出被拒：服务端未配置口令（fail-closed）—— 不是没有会话';
  // 'error' 必须是**显式分支**而不是掉进兜底：闸门按状态名逐个点名（缺一个即红），
  // 而兜底留给"将来新增状态忘了配文案"这种情况 —— 那时也要说清不是没有会话。
  if (state === 'error') return '导出失败（HTTP ' + code + '）—— 不是没有会话';
  return '导出失败（未知状态 ' + state + '）—— 不是没有会话';
}

async function chatSessExport() {
  const tk = termToken();
  if (!tk) { toast(exportStateText('need-token', -1, {}), 'err'); return; }
  const p = new URLSearchParams({ format: 'json', limit: '1000',
                                  with_messages: '1', redact: '1' });
  if (chatPick) p.set('agent_id', chatPick);
  let r;
  try {
    r = await fetch('/api/sessions/export?' + p.toString(),
                    { headers: { 'X-TERM-TOKEN': tk } });   // 头，不是 URL
  } catch (e) {
    toast('导出失败：网络不可达（' + e.message + '）—— 不是没有会话', 'err');
    return;
  }
  const cnt = parseInt(r.headers.get('X-Export-Count') || '-1', 10);
  const hits = parseInt(r.headers.get('X-Export-Redacted-Hits') || '0', 10);
  if (r.status !== 200) {
    let detail = '';
    try { const d = await r.json(); detail = (d && d.detail) || ''; } catch (e) { detail = ''; }
    const st = exportStateOf(r.status, cnt, detail);
    if (st === 'bad-token') lsRemove('hub.term.token');   // 存量失效口令：清掉，下次重新问
    toast(exportStateText(st, cnt, { status: r.status, hits: hits }), 'err');
    return;
  }
  let body;
  try { body = await r.blob(); } catch (e) {
    toast('导出失败：读不到响应体 —— 不是没有会话', 'err'); return;
  }
  const cd = r.headers.get('Content-Disposition') || '';
  const m = cd.match(/filename="?([^";]+)"?/);
  const name = (m && m[1]) || 'agent-hub-sessions.json';   // 后端保证纯 ASCII 文件名
  const obj = URL.createObjectURL(body);
  const a = document.createElement('a');
  a.href = obj;
  a.download = name;
  document.body.appendChild(a);
  a.click();
  setTimeout(function () { URL.revokeObjectURL(obj); a.remove(); }, 0);
  toast(exportStateText(exportStateOf(200, cnt, ''), cnt, { hits: hits }), 'ok');
}

/* data-export 委托：新按钮一律走委托，不用 inline onclick（AGENTS.md 浮层配套红线，口径取最严）。
   挂在 document 上而不是某个面板里 ⇒ 工具条重渲染后不必重新绑定。 */
document.addEventListener('click', function (e) {
  const b = e.target && e.target.closest ? e.target.closest('[data-export]') : null;
  if (!b) return;
  if (b.getAttribute('data-export') === 'sessions') chatSessExport();
});
