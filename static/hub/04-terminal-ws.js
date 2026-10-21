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
/* 上一个合法值：付费档取消确认时回滚（与设置页 SET_MODEL_PREV 同语义，
   但两个下拉各自记忆 —— 切换 agent 会整表重渲染，共用一个变量会串档）。 */
let CHAT_MODEL_PREV = '';
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
      const label = modelOptionLabel(mid, m);   // 前置 [免费]/[付费]，值不变
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
      html += '<option value="' + escapeHtml(val) + '"' + (isSel ? ' selected' : '') + '>' + escapeHtml(modelOptionLabel(m.id, m)) + '</option>';
    }
    html += '</optgroup>';
  }
  sel.innerHTML = html;
  CHAT_MODEL_PREV = sel.value || '';
}
$('chatModel')?.addEventListener?.('change', e => {
  const sel = e.target;
  const a = SET_AGENTS.find(x => x.id === chatPick) || null;
  const meta = modelMetaFromValue(sel.value, a, CHAT_MODELS);
  /* 「默认（网关路由）」= 空值，不确认（它不指向具体计费模型）。 */
  if (meta && !modelConfirmPaid(meta.id, meta.m)) {
    sel.value = CHAT_MODEL_PREV;
    toast('已取消：' + meta.id + ' 按量计费', 'err');
    return;
  }
  CHAT_MODEL_PREV = sel.value;
  lsSet('hub.model.' + chatPick, sel.value);
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
      '<button class="btn sm" style="align-self:center" data-sk-ccstart="' +
      escapeHtml(p.path) + '" title="在 CloudCLI 开始此项目的新会话">▶ 开始会话</button></div>').join('');
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
 * SKILL_INV       调用流水原始数据（固定近 7 天），只服务目录徽标与统计条；
 *                 与下方 SKILL_INV_VIEW（当前面板窗口）分开，禁止互相覆盖。
 *                 by_skill 映射给目录行徽标与统计条用（页面不存第二个真相）。 */
let SKILL_VIEW = 'catalog', SKILL_DOC = { name: '', route: '' };
/* 调用记录视图的数据与「近 7 天徽标」的数据分开存：
 * 用户把窗口切到 30/90 天时，若复用 SKILL_INV，统计条会在下一次重渲染时
 * 把 30 天数字冒充「近 7 天」（页面上第二个真相）。SKILL_INV 只服务徽标/统计条，
 * SKILL_INV_VIEW 只服务当前调用记录面板。 */
let SKILL_INV_VIEW = null;
let SKILL_INV = null, SKILL_INV_BY_SKILL = {}, SKILL_INV_LOADED = false;
/* 商店视图：targets 来自后端 `_targets()`（只含可写发现点），页面不自己拼路径。 */
let STORE_STATE = { items: [], q: '', busy: false, targets: null, error: '' };
/* 权重榜视图（PT-20261020-01 的展示面）：只读投影，**不在前端重算 score**。
 * 公式在服务端 skill_weights，页面照抄一份必然漂移——那时「同一技能两个分数」
 * 就成了第二个真相。`caller` 过滤也必须打服务端：它是「先过滤再算分」，
 * 前端后过滤会把别人的曝光留在分母里。 */
let SKILL_USAGE = null, SKILL_USAGE_LOADED = false, SKILL_USAGE_BUSY = false;

/* v0.13.69 D7 技能门面：一份数据源、一个扫描口径。
 * 主列表 + 三张诊断卡（自检 / 相关性实验室 / 注入预算）都读同一批后端字段，
 * **页面不存第二个真相**——原先那句写死的「7 路发现点」已于 D1 改成 20 路后变成谎话。
 * 另：零调用榜的 confidence 封顶 medium（各家直读磁盘未取证）必须在界面上显示，
 * 藏起来的 medium 会被读成 high。 */

async function loadSkills() {
  bindSkillTabs();   // 标题行 tab 的点击/键盘委托（幂等；视图可先于数据切换）
  bindUsageFilters();   // 权重榜三个下拉走委托（过滤打服务端，不在前端后过滤）
  bindStoreActions();   // 商店行的安装按钮走委托（数据来自远端，禁 inline onclick）
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
    $('skillRouteHint').textContent = '点行或「查看」读正文（脱敏）';
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

/* v0.13.127 P1：拉一次调用流水汇总，供①目录行调用徽标 ②统计条 ③tab 徽标 用。
 * 只取汇总不取明细：明细是「调用记录」视图的事（switchSkillView 懒加载）。
 * 失败静默降级为「徽标不可用」——徽标是增强信息，不该把主列表拖黑。
 * `rebuildRows=false` 只刷数字不重绘目录行：进页/轮询时行要不要重建是两回事，
 * 每 60 秒重建 375 行会打断正在滚动/选中的用户。 */
async function loadSkillInvBadges(rebuildRows) {
  try {
    const d = await api('/api/skill/invocations?days=7&limit=100');
    SKILL_INV_BY_SKILL = (d.summary && d.summary.by_skill) || {};
    SKILL_INV = d;
  } catch (e) {
    SKILL_INV_BY_SKILL = {};
    SKILL_INV = null;
  }
  renderSkillStats();
  if (rebuildRows !== false) renderSkillList();
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
  let html = '<span>共 <b>' + nSkill + '</b> 个技能 ·</span>' +
    '<span><b>' + nRoute + '</b> 路发现点 ·</span>' +
    '<span>近 7 天调用 <b>' + invTotal + '</b> 次 ·</span>' +
    '<span>（<b>' + nInvoked + '</b> 个技能被调用过）·</span>' +
    '<span>零调用 <b>' + zCount + '</b> 个</span>';
  if (SKILL_TRUNCATED) {
    // 「共 N 个」与「列表里有几条」必须分开说：把截断后的条数写成总数 = 这个 bug 的原始形态
    html += '<span class="sk-stat-warn">本次只显示 ' + SKILLS.length +
      ' 条（单次上限），用搜索缩小范围</span>';
  }
  if (degraded.length) {
    html += '<span class="sk-stat-warn">降级路：' + escapeHtml(degraded.join(', ')) + '</span>';
  }
  if (SKILL_INV && SKILL_INV.bad_detail) {
    html += '<span class="sk-stat-warn">' + SKILL_INV.bad_detail + ' 条坏账已跳过</span>';
  }
  // tab 上的调用数徽标：数据未到不显示；到 0 也显示 0（用户能一眼看出「记录确实为空」，
  // 而不是怀疑 tab 没加载）。
  const tabBadge = $('skillInvTabBadge');
  if (tabBadge) {
    tabBadge.textContent = SKILL_INV ? String(invTotal) : '';
    tabBadge.hidden = !SKILL_INV;
  }
  el.innerHTML = html;
}

/* 视图切换（Grill 裁定 5：技能目录 / 调用记录两视图）。
 * 懒加载：调用记录第一次切到才拉数据——首屏请求数不因它增加。 */
function switchSkillView(v) {
  if (v !== 'catalog' && v !== 'invocations' && v !== 'store' &&
      v !== 'weights' && v !== 'refs') return;
  SKILL_VIEW = v;
  ['catalog', 'invocations', 'store', 'weights', 'refs'].forEach(k => {
    const tab = $('skillTab' + k[0].toUpperCase() + k.slice(1));
    const view = $('skillView' + k[0].toUpperCase() + k.slice(1));
    if (tab) {
      const on = k === v;
      tab.classList.toggle('on', on);
      tab.setAttribute('aria-selected', on ? 'true' : 'false');
      // roving tabindex（WAI-ARIA APG Tabs）：只有当前 tab 在 Tab 序列里，
      // 左右箭头在 tablist 内移动——避免键盘用户被迫逐个 Tab 穿过两个控件。
      tab.tabIndex = on ? 0 : -1;
    }
    if (view) view.style.display = (k === v) ? '' : 'none';
  });
  if (v === 'invocations' && !SKILL_INV_LOADED) {
    SKILL_INV_LOADED = true;
    loadSkillInvocations();
  }
  if (v === 'invocations') {
    renderSkillInvocations();
    // tab 徽标固定近 7 天，视图窗口可切 30/90 天——进这个视图就重取一次汇总，
    // 不拿列表结果冒充徽标（2026-10-20：徽标停在首屏那次的 0）。
    loadSkillInvBadges(false);
  }
  if (v === 'store') {
    // 首进只查 CLI 状态与目标（不自动搜——搜索是用户的显式动作，别擅自发网络请求）。
    loadStoreStatus();
    renderStore();
  }
  if (v === 'weights') {
    // 懒加载：与调用记录同理，首屏请求数不因这个视图增加。
    // 首进必须先拉一次（榜单窗口默认近 30 天，与端点默认一致）。
    if (!SKILL_USAGE_LOADED) { SKILL_USAGE_LOADED = true; loadSkillUsage(); }
    else renderSkillUsage();
  }
  if (v === 'refs') {
    bindRefActions();
    if (!REF_LOADED) { REF_LOADED = true; loadRefCards(); }
    else renderRefCards();
  }
}

/* ── 技能商店（SkillHub 门面）────────────────────────────────────────────
 * 分工：后端只暴露 search / install / status 三个动作；安装目标是后端
 * `skill.SKILL_DIRS` 现算的可写发现点，前端**不自己拼路径**（拼路径＝让浏览器决定写哪）。
 * 「调用」不在本视图里另开一条链路：装完回技能目录，用既有 read/invoke（正文不出机器）。
 * 不接 `skillhub run`（在线执行会把正文发到第三方服务）。 */

function _fillStoreTargets(targets, preferred) {
  const sel = $('storeRoute');
  if (!sel || !targets || !targets.length) return;
  const keep = sel.value;
  sel.innerHTML = targets.map(t =>
    '<option value="' + escapeHtml(t.route) + '"' + (t.writable ? '' : ' disabled') + '>' +
    escapeHtml(t.route) + (t.writable ? '' : '（只读）') + '</option>').join('');
  // 默认选**自动兜底的那个目标**，不是列表第一个。否则面板说「自动兜底 → techdocs」
  // 而下拉停在 claude，用户点安装就把技能写进了另一个 agent 的发现点（实测 2026-10-20）。
  if (keep && targets.some(t => t.route === keep && t.writable)) {
    sel.value = keep;
  } else if (preferred && targets.some(t => t.route === preferred && t.writable)) {
    sel.value = preferred;
  }
}

async function loadStoreStatus() {
  try {
    const d = await api('/api/skill/store/status');
    STORE_STATE.targets = d.targets || [];
    STORE_STATE.available = !!d.available;
    STORE_STATE.error = d.available ? '' : (d.error || 'SkillHub CLI 不可用');
    _fillStoreTargets(STORE_STATE.targets, (d.auto || {}).route);
    _renderStoreMeta(d);
    const hint = $('storeHint');
    if (hint) {
      hint.innerHTML = STORE_STATE.available
        ? '来源 SkillHub（' + escapeHtml(String(d.version || '')) +
          '）· 安装＝把技能文件落到所选发现点 · 调用走本仓 /api/skill/invoke，正文不出机器'
        : '<span class="sk-inv-bad">SkillHub 不可用：' + escapeHtml(STORE_STATE.error) +
          '</span> <span class="hint">（CLI 缺失或不可执行，安装见 skillhub.cn/install/skillhub.md）</span>';
    }
  } catch (e) {
    STORE_STATE.error = e.message || String(e);
  }
}

/* 自动兜底与账号：**只渲染后端字段**（/store/status 的 auto / auth）。
 * 阈值、额度、目标目录都在服务端现算，前端不复制一份——复制就会漂移，
 * 漂移的形态是「界面说还能装，实际 429」。 */
function _renderStoreMeta(d) {
  const auto = (d && d.auto) || {};
  const a = $('storeAutoLine');
  if (a) {
    let text;
    if (auto.enabled) {
      text = '开启 · 目标 ' + escapeHtml(String(auto.route || '')) +
        (auto.route_writable ? '' : '（不可写）') +
        ' · 本小时 ' + escapeHtml(String(auto.used_last_hour || 0)) + '/' +
        escapeHtml(String(auto.max_per_hour || 0));
    } else {
      text = '关闭（SKILL_STORE_AUTO=0）';
    }
    a.innerHTML = '<b>自动兜底</b><span>' + text + '</span>';
  }
  const au = (d && d.auth) || {};
  const b = $('storeAuthLine');
  if (!b) return;
  if (au.logged_in) {
    b.innerHTML = '<b>账号</b><span class="sk-store-token">已登录（' +
      escapeHtml(String(au.detail || '')) + '）' +
      '<button class="btn ghost sm" data-store-logout="1">退出</button></span>';
  } else {
    b.innerHTML = '<b>账号</b><span class="sk-store-token">未登录 · ' +
      '搜索/下载免登录；发布、评论、在线运行需 skh_ Token' +
      '<input id="storeToken" type="password" placeholder="skh_…" autocomplete="off"' +
      ' aria-label="SkillHub API Token">' +
      '<button class="btn sm" data-store-login="1">保存 Token</button></span>';
  }
}

async function storeLogin() {
  const inp = $('storeToken');
  const key = String((inp && inp.value) || '').trim();
  if (!/^skh_[A-Za-z0-9_-]{8,}$/.test(key)) {
    toast('Token 形状不对：应以 skh_ 开头', 'err');
    return;
  }
  const btn = document.querySelector('[data-store-login]');
  if (btn) { btn.disabled = true; btn.textContent = '保存中…'; }
  try {
    await api('/api/skill/store/login', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ key: key }),
    });
    if (inp) inp.value = '';       // 不在页面上留副本
    toast('Token 已保存到 ~/.skillhub（hub 不落库）', 'ok');
    await loadStoreStatus();
  } catch (e) {
    toast('登录失败：' + (e.message || e), 'err');
    if (btn) { btn.disabled = false; btn.textContent = '保存 Token'; }
  }
}

async function storeLogout() {
  try {
    await api('/api/skill/store/logout', { method: 'POST' });
    toast('已退出 SkillHub 账号', 'ok');
    await loadStoreStatus();
  } catch (e) {
    toast('退出失败：' + (e.message || e), 'err');
  }
}

async function searchStore(q) {
  const box = $('storeQ');
  const query = String(q != null ? q : (box ? box.value : '')).trim();
  if (!query) { toast('先输入关键词', 'err'); return; }
  STORE_STATE.q = query;
  STORE_STATE.busy = true;
  STORE_STATE.error = '';
  STORE_STATE.items = [];
  renderStore();
  const btn = $('storeSearchBtn');
  if (btn) btn.disabled = true;
  try {
    const d = await api('/api/skill/store/search?q=' + encodeURIComponent(query) + '&limit=12');
    STORE_STATE.items = d.items || [];
    if (d.targets) { STORE_STATE.targets = d.targets; _fillStoreTargets(d.targets); }
  } catch (e) {
    STORE_STATE.error = e.message || String(e);
  } finally {
    STORE_STATE.busy = false;
    if (btn) btn.disabled = false;
    renderStore();
  }
}

function fmtCount(n) {
  const v = Number(n);
  if (!isFinite(v) || v <= 0) return '0';
  if (v >= 10000) return (v / 10000).toFixed(v >= 100000 ? 0 : 1) + '万';
  if (v >= 1000) return (v / 1000).toFixed(1) + 'k';
  return String(v);
}

function renderStore() {
  const el = $('storeList');
  if (!el) return;
  const cnt = $('storeCount');
  if (cnt) cnt.textContent = STORE_STATE.q
    ? (STORE_STATE.busy ? '搜索中…' : STORE_STATE.items.length + ' 条结果')
    : '';
  if (STORE_STATE.busy) { el.innerHTML = '<div class="sk-empty">搜索中…</div>'; return; }
  if (STORE_STATE.error) {
    el.innerHTML = '<div class="sk-empty"><b>商店不可用</b><br>' +
      escapeHtml(STORE_STATE.error) + '</div>';
    return;
  }
  if (!STORE_STATE.items.length) {
    el.innerHTML = '<div class="sk-empty">' + (STORE_STATE.q
      ? '<b>没有匹配的技能</b><br>换个关键词（中文/英文都可），或到 skillhub.cn 看分类。'
      : '输入关键词后按回车或点「搜索」。安装会写到你上方选的发现点目录。') + '</div>';
    return;
  }
  el.innerHTML = STORE_STATE.items.map(it => {
    const installed = STORE_STATE.installed && STORE_STATE.installed[it.slug];
    const meta = [it.version ? 'v' + it.version : '', it.namespace || it.source || '']
      .filter(Boolean).map(escapeHtml).join(' · ');
    // 同类技能很多，光看名字分不出该装哪个 ⇒ 把选择依据摆出来（下载量/是否要 Key）。
    // 下载量可能为 null（CLI 兜底路径拿不到）——显示“未知”，不显示 0。
    const sig = [];
    if (it.downloads === 0 || it.downloads) sig.push('下载 ' + fmtCount(it.downloads));
    else sig.push('下载未知');
    if (it.needs_key) sig.push('<span class="sk-inv-bad">需自带 Key</span>');
    if (it.verified) sig.push('已认证');
    return '<div class="mem-item sk-store-row">' +
      '<div class="sk-store-main">' +
        '<div class="sk-inv-head"><span class="sk-inv-name">' + escapeHtml(it.name || it.slug) +
          '</span>' + (meta ? '<span class="sk-inv-route">' + meta + '</span>' : '') + '</div>' +
        '<div class="sk-store-desc">' + escapeHtml(it.description || '（无描述）') + '</div>' +
        '<div class="sk-inv-meta">' + sig.join(' · ') + '</div>' +
        '<div class="sk-store-slug">' + escapeHtml(it.slug) + '</div>' +
      '</div>' +
      '<div class="sk-store-act">' +
        (installed
          ? '<span class="sk-inv-ok">已装到 ' + escapeHtml(installed) + '</span>'
          : '<button class="btn sm" data-store-install="' + escapeHtml(it.slug) +
            '" data-store-name="' + escapeHtml(it.name || it.slug) + '">安装</button>') +
      '</div></div>';
  }).join('');
}

async function installStore(slug, name, force) {
  const sel = $('storeRoute');
  const route = sel ? sel.value : '';
  if (!route) { toast('没有可写的发现点，先确认 SKILL_DIRS 配置', 'err'); return; }
  const btn = Array.from(document.querySelectorAll('[data-store-install]'))
    .find(b => b.dataset.storeInstall === slug);
  if (btn) { btn.disabled = true; btn.textContent = '安装中…'; }
  try {
    const d = await api('/api/skill/store/install', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ slug: slug, route: route,
                             check_security: true, force: !!force }),
    });
    STORE_STATE.installed = STORE_STATE.installed || {};
    STORE_STATE.installed[slug] = d.route;
    const sec = (d.security || {}).verdict;
    toast('已安装 ' + (d.name || name || slug) + ' → ' + d.route +
      '（安全报告：' + (sec === 'benign' ? '通过' : sec === 'suspicious' ? '可疑（你已确认）' : '无公开报告') +
      '）', 'ok');
    // 目录重扫：不刷的话新技能要等 TTL，用户会以为没装上。
    loadSkills();
    loadSkillInvBadges(false);
  } catch (e) {
    const msg = String(e.message || e);
    // 409 = 安全报告可疑被默认拦下。此时必须**把报告摆出来**再问，不能一句「失败」了事；
    // 用户明确知情后仍要装，才带 force 重试（后端只认显式 force）。
    if (msg.indexOf('可疑') >= 0 && !force && confirm('安全报告标记可疑：\n' + msg +
        '\n\n确认知情并仍要安装？')) {
      if (btn) { btn.disabled = false; btn.textContent = '安装'; }
      return installStore(slug, name, true);
    }
    toast('安装失败：' + msg, 'err');
    if (btn) { btn.disabled = false; btn.textContent = '安装'; }
  }
  renderStore();
}

function bindStoreActions() {
  const box = $('storeList');
  if (!box || box.__skStore) return;
  box.__skStore = true;
  box.addEventListener('click', e => {
    const b = e.target.closest('[data-store-install]');
    if (b && box.contains(b)) installStore(b.dataset.storeInstall, b.dataset.storeName);
  });
  // 登录/退出按钮在 `#storeAuthLine`（不在 #storeList 里），故挂在容器外层。
  const meta = $('storeAuthLine');
  if (meta && !meta.__skAuth) {
    meta.__skAuth = true;
    meta.addEventListener('click', e => {
      if (e.target.closest('[data-store-login]')) storeLogin();
      else if (e.target.closest('[data-store-logout]')) storeLogout();
    });
  }
}

/* 顶部「刷新」按当前视图刷新：目录页刷新技能清单与徽标，调用记录页刷新流水**和徽标**。
 * 只留一个刷新入口——原来两个视图各有一个按钮，切视图后按错是常态。
 * 调用记录页必须同时刷徽标：徽标是固定近 7 天汇总，而视图窗口可切 30/90 天，
 * 两者不是同一份数据，不能拿列表结果冒充。（2026-10-20 实测：列表 4 条、徽标仍 3。） */
function refreshSkillView() {
  if (SKILL_VIEW === 'invocations') {
    loadSkillInvocations();
    loadSkillInvBadges();
  } else if (SKILL_VIEW === 'store') {
    // 商店页的「刷新」＝重查状态 + 重跑当前关键词（没搜过就只查状态，不发网络搜索）。
    loadStoreStatus();
    if (STORE_STATE.q) searchStore(STORE_STATE.q);
  } else loadSkills();
}

/* 标题行 tab 的点击 + 键盘（WAI-ARIA APG Tabs pattern）。
 * 自动激活：箭头落到哪个 tab 就切到哪个视图（两个面板都在本地，切换无网络代价）。
 * 交互一律 data-* 委托，不用 inline onclick（§4.2 同族口径）。 */
function bindSkillTabs() {
  const bar = $('skillTabs');
  if (!bar || bar.__skTabs) return;
  bar.__skTabs = true;
  const tabs = () => Array.from(bar.querySelectorAll('[role="tab"]'));
  bar.addEventListener('click', e => {
    const t = e.target.closest('[data-skill-view]');
    if (t && bar.contains(t)) switchSkillView(t.dataset.skillView);
  });
  bar.addEventListener('keydown', e => {
    const ts = tabs();
    const i = ts.indexOf(document.activeElement);
    if (i < 0) return;
    let next = null;
    if (e.key === 'ArrowRight') next = (i + 1) % ts.length;
    else if (e.key === 'ArrowLeft') next = (i - 1 + ts.length) % ts.length;
    else if (e.key === 'Home') next = 0;
    else if (e.key === 'End') next = ts.length - 1;
    else return;
    e.preventDefault();          // 横向 tablist 不劫持上下键，页面仍可正常滚动
    ts[next].focus();
    switchSkillView(ts[next].dataset.skillView);
  });
}

function clearSkillFilter() {
  const q = $('skillQ');
  if (!q) return;
  q.value = '';
  renderSkillList();
  q.focus();
}

/* ── 参考情报（PT-20261021-04）──────────────────────────────────────────────
 * 异步任务：POST /api/ref/scout 立即回 job_id；检索完先落「词法卡」，重排完成再原地
 * 升级。所以这里**不阻塞**界面等重排——轮询到 job 有 card 就先渲染，stage=rerank 时
 * 提示「词法版已可用」。
 * 只渲染后端字段：三轴分、判定、许可证、as_of 全部来自 /api/ref/cards，页面不重算。
 * 产物只在 hub 库（不写到项目目录），避免前端决定写哪。
 */
let REF_CARDS = [], REF_LOADED = false, REF_POLL = null, REF_CARD_ID = '';

function refStatusText(st) {
  return ({
    lexical: '仅词法', reranked: '已重排', lexical_fallback: '重排失败·仅词法'
  })[st] || st || '—';
}

function bindRefActions() {
  const box = $('refCards');
  if (box && !box.__refBind) {
    box.__refBind = true;
    box.addEventListener('click', e => {
      const btn = e.target.closest('[data-ref-act]');
      if (!btn || !box.contains(btn)) return;
      const id = btn.dataset.id || '';
      if (btn.dataset.refAct === 'open') openRefCard(id);
      else if (btn.dataset.refAct === 'refresh') refreshRefCard(id);
    });
  }
  const detail = $('refDetailBox');
  if (detail && !detail.__refBind) {
    detail.__refBind = true;
    detail.addEventListener('click', e => {
      const btn = e.target.closest('[data-ref-act="close"]');
      if (btn) { detail.style.display = 'none'; REF_CARD_ID = ''; }
    });
  }
}

async function loadRefCards() {
  const el = $('refCards');
  if (!el) return;
  boxBusy('refCards');
  try {
    const d = await api('/api/ref/cards?limit=50');
    REF_CARDS = d.items || [];
    renderRefCards();
  } catch (e) {
    boxFail('refCards', e, 'loadRefCards');
  }
}

function renderRefCards() {
  const el = $('refCards');
  if (!el) return;
  if (!REF_CARDS.length) {
    el.innerHTML = '<div class="sk-empty"><b>还没有情报卡</b>：在上方填一个功能模块，点「检索」。' +
      '第一次会走 GitHub 多臂检索（约 20~60s），词法版先出，语义重排随后原地升级。</div>';
    return;
  }
  el.innerHTML = REF_CARDS.map(c => {
    const top = c.top || {};
    const deg = (c.degraded || 0) > 0
      ? '<span class="badge" title="有检索臂失败，卡内「逐路表态」列了原因">降级 ' + c.degraded + '</span>' : '';
    return '<div class="mem-item">' +
      '<span class="tag agent" style="align-self:flex-start">' + escapeHtml(c.module) + '</span>' +
      '<p><b>' + escapeHtml(refStatusText(c.status)) + '</b> · ' +
      '<span class="hint">' + escapeHtml(String(c.as_of || '').replace('T', ' ').slice(0, 19)) +
      ' · 候选 ' + (c.candidates || 0) + '（实现 ' + (c.impl_count || 0) + '）</span>' +
      (top.repo ? '<br>最相关：<b>' + escapeHtml(top.repo) + '</b>　功能/实现/设计 ' +
        Number(top.func || 0).toFixed(2) + ' / ' + Number(top.impl || 0).toFixed(2) + ' / ' +
        Number(top.design || 0).toFixed(2) : '') +
      '</p>' + deg +
      '<button class="btn ghost sm" data-ref-act="open" data-id="' + escapeHtml(c.id) + '">查看</button>' +
      '<button class="btn ghost sm" data-ref-act="refresh" data-id="' + escapeHtml(c.id) +
        '" title="重跑沿用上方「允许付费重排」勾选；自动刷新永不付费">重跑</button>' +
      '</div>';
  }).join('');
}

async function submitRefScout() {
  const mod = ($('refModule').value || '').trim();
  const hint = $('refHint');
  if (mod.length < 2) { hint.textContent = '先填功能模块名（至少 2 个字）'; return; }
  const kws = ($('refKeywords').value || '').split(',').map(x => x.trim()).filter(Boolean);
  // 代码标识符臂：在 GitHub 代码里搜这个串，命中它的仓额外加分。限额 10 次/分，
  // 所以后端只取前 2 个（多填的会被静默丢弃，输入框已写明「最多 2 个」）。
  const idents = (($('refIdent') || {}).value || '').split(',').map(x => x.trim()).filter(Boolean).slice(0, 2);
  // 语言先验：只影响打分（`lang_ok` 0.10），不改进查询。实测 cron 模块 python 把
  // apscheduler 从 #14 抬到 #4 —— 不填时该权重对所有人都是 1.0，等于没有区分。
  const lang = (($('refLang') || {}).value || '').trim().toLowerCase();
  const btn = $('refSubmit');
  btn.disabled = true;
  hint.textContent = '提交中…';
  try {
    const d = await api('/api/ref/scout', {
      method: 'POST', headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({
        module: mod, keywords: kws, identifiers: idents, lang: lang,
        allow_paid: !!($('refPaid') && $('refPaid').checked)
      })
    });
    hint.textContent = '已排队：' + d.job_id + (d.deduped ? '（同一模块已有任务，复用）' : '');
    pollRefJob(d.job_id, d.card_id);
  } catch (e) {
    hint.textContent = '提交失败：' + e.message;
    toast('参考情报提交失败：' + e.message, 'err');
  } finally {
    btn.disabled = false;
  }
}

/* 轮询：任务状态是**唯一真相**。卡先出现就渲染（stage=rerank 时已经能看词法结果），
 * job 结束才停——不用固定等待时间猜「应该差不多了」。 */
function pollRefJob(jobId, cardId) {
  if (REF_POLL) { clearInterval(REF_POLL); REF_POLL = null; }
  let ticks = 0;
  const hint = $('refHint');
  const tick = async () => {
    ticks += 1;
    try {
      const d = await api('/api/ref/jobs/' + encodeURIComponent(jobId));
      const st = d.status + '/' + (d.stage || '');
      if (d.card) {
        hint.textContent = st + ' · 卡已可用（' + refStatusText(d.card.status) + '）';
        await loadRefCards();
      } else {
        hint.textContent = st + ' …';
      }
      if (d.status === 'done' || d.status === 'failed') {
        clearInterval(REF_POLL); REF_POLL = null;
        hint.textContent = d.status === 'failed'
          ? ('任务失败：' + (d.error || '见卡内逐路表态'))
          : ('完成（' + (d.card ? refStatusText(d.card.status) : '') + '）');
        await loadRefCards();
        if (cardId) openRefCard(cardId);
      }
    } catch (e) {
      hint.textContent = '轮询失败：' + e.message;
      clearInterval(REF_POLL); REF_POLL = null;
    }
    if (ticks > 120 && REF_POLL) {   // 5 分钟硬上限：不无限轮询
      clearInterval(REF_POLL); REF_POLL = null;
      hint.textContent = '任务仍在后台运行，稍后点刷新查看';
    }
  };
  REF_POLL = setInterval(tick, 2500);
  tick();
}

async function openRefCard(id) {
  if (!id) return;
  try {
    const d = await api('/api/ref/cards/' + encodeURIComponent(id));
    REF_CARD_ID = id;
    $('refDetailBox').style.display = '';
    $('refDetailTitle').textContent = d.module + ' · ' + refStatusText(d.status) +
      ' · as of ' + String(d.as_of || '').replace('T', ' ').slice(0, 19) +
      ' · 下次刷新 ' + String(d.next_refresh || '').slice(0, 10);
    $('refDetail').textContent = d.md || '';
  } catch (e) {
    toast('读取情报卡失败：' + e.message, 'err');
  }
}

async function refreshRefCard(id) {
  if (!id) return;
  const hint = $('refHint');
  const paid = !!($('refPaid') && $('refPaid').checked);
  hint.textContent = '正在重跑（' + (paid ? '付费重排' : '仅免费档') + '）…';
  try {
    const d = await api('/api/ref/cards/' + encodeURIComponent(id) +
      '/refresh?allow_paid=' + (paid ? '1' : '0'), { method: 'POST' });
    pollRefJob(d.job_id, id);
  } catch (e) {
    hint.textContent = '重跑失败：' + e.message;
  }
}

/* 徽标保活：停在技能页时，后台 agent 的调用不会自己冒出来。
 * 60s 只重取一次 summary（不重绘 375 行），离开技能页或标签页不可见就跳过。
 * 这不是「实时推送」——真推送需要 SSE，代价远大于收益；这里只保证数字不会
 * 停留在打开页面那一刻（2026-10-20 用户报「一直显示 0」）。 */
setInterval(function () {
  if (typeof curPage !== 'undefined' && curPage === 'skills' &&
      document.visibilityState === 'visible') loadSkillInvBadges(false);
}, 60000);

function clearInvFilters() {
  ['invName', 'invCaller'].forEach(id => { const el = $(id); if (el) el.value = ''; });
  renderSkillInvocations();
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

/* 排序用的两个派生值：都取页面已有的后端数据，不另发请求、不存第二个真相。 */
function skillInvCount(s) { return (SKILL_INV_BY_SKILL && SKILL_INV_BY_SKILL[s.name]) || 0; }
function skillIdleDays(s) {
  const z = SKILL_ZOMBIES[s.name];
  return z ? (Number(z.days_idle) || 0) : -1;   // 不在僵尸榜 = 有调用痕迹，排最后
}

/* 清除目录视图的全部筛选（搜索词/发现点/排序/只看零调用）——空状态里的出口。 */
function resetSkillFilters() {
  ['skillQ', 'skillRoute', 'skillSort'].forEach(id => { const el = $(id); if (el) el.value = ''; });
  SKILL_ZOMBYE_ONLY = false;
  const chip = $('skillZombieOnly');
  if (chip) chip.classList.remove('on');
  renderSkillList();
}

function renderSkillList() {
  const q = ($('skillQ').value || '').toLowerCase();
  const route = $('skillRoute').value;
  const sort = ($('skillSort') && $('skillSort').value) || '';
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
  // 排序（默认保持上面的相关性/名称顺序；只在筛选后才需要另一种视角）
  if (sort === 'calls') {
    zrows.sort((a, b) => (skillInvCount(b) - skillInvCount(a)) ||
      String(a.name || '').localeCompare(String(b.name || '')));
  } else if (sort === 'idle') {
    zrows.sort((a, b) => (skillIdleDays(b) - skillIdleDays(a)) ||
      String(a.name || '').localeCompare(String(b.name || '')));
  }
  const el = $('skillList');
  if (!el) return;
  // 结果计数 + 搜索清除钮：输入框有自己的「清除」出口，不用整页重置。
  const total = SKILL_TOTAL || SKILLS.length;
  const cnt = $('skillCount');
  if (cnt) cnt.textContent = (SKILL_ZOMBYE_ONLY ? '零调用 ' : (q ? '命中 ' : '显示 ')) +
    zrows.length + ' / ' + total + ' 条';
  const clearBtn = $('skillQClear');
  if (clearBtn) clearBtn.hidden = !q;
  if (SKILL_ZOMBYE_ONLY && !Object.keys(SKILL_ZOMBIES).length) {
    el.innerHTML = '<div class="sk-empty"><b>零调用榜为空</b>（可能记账尚未建立）。' +
      '这不是「所有技能都没人用」——先跑一次相关性实验室建立记账，或' +
      '<button class="btn ghost sm" onclick="resetSkillFilters()">查看全部技能</button></div>';
    return;
  }
  const nearHint = (q && nearCount)
    ? '<div class="hint" style="padding:8px 10px 0">其中 <b>' + nearCount + '</b> 条为<b>近似匹配</b>（容忍手误，标 ≈近似）——无精确命中项。</div>'
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
    // 整行可点（data-sk-row）：查看详情不必精准命中右侧小按钮；
    // 右侧「查看」仍是独立按钮，供键盘/读屏直接聚焦。
    return '<div class="mem-item sk-row" data-sk-row="1" data-name="' + escapeHtml(s.name) +
      '" data-route="' + escapeHtml(rt == null ? '' : rt) + '" title="点击查看详情">' +
    '<span class="tag agent" style="align-self:flex-start">' + escapeHtml(s.name) + '</span>' +
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
  }).join('') || ('<div class="sk-empty"><b>没有匹配的技能</b>（当前 ' + zrows.length + ' / ' +
    total + ' 条）。' + (q ? '已启用模糊匹配仍无结果 ⇒ 换关键词，或到「诊断 → 发现点自检」查该目录是否被扫到。' :
      '当前筛选条件下没有条目。') +
    '<br><button class="btn ghost sm" onclick="resetSkillFilters()">清除全部筛选</button></div>');
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
  ['skillList', 'skillInvocations', 'skillDocDrawer'].forEach(id => {
    const el = $(id);
    if (!el || el.__skDeleg) return;
    el.__skDeleg = true;
    el.addEventListener('click', e => {
      const b = e.target.closest('[data-sk-view]');
      if (b && el.contains(b)) {
        e.stopPropagation();
        skillRead(b.dataset.name || '', b.dataset.route || '');
        return;
      }
      // 目录行整行可点（2026-10-19）：右侧「查看」仍是独立按钮；
      // 点在行内其它位置同样打开详情，减少「必须精准点小按钮」的操作成本。
      const row = e.target.closest('[data-sk-row]');
      if (row && el.contains(row)) {
        e.stopPropagation();
        skillRead(row.dataset.name || '', row.dataset.route || '');
      }
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
      toast('同名技能有多路，先在正文区选一路', 'err');
    } else if (e.http === 401 && body) {
      // 2026-10-19 实测：用户点「试调用」后调用记录仍为 0，生产日志是
      // `[writegate] 401 ... 凭据不匹配`。旧 token 被 api() 清空但用户不知道，
      // 必须把「为什么没记账 + 下一步」讲清楚，否则只会读成「功能坏了」。
      body.textContent = '调用失败（401 未授权）：浏览器保存的 TERM_TOKEN 与服务端不一致。\n' +
        '旧凭据已被自动清空。请再点一次「试调用」，在弹窗里输入当前 TERM_TOKEN。\n\n服务端返回：' +
        (e.message || e);
      toast('调用未记账：TERM_TOKEN 已失效，已清空旧凭据；请重试并输入当前 token', 'err');
    } else if (body) {
      body.textContent = '调用失败：' + (e.message || e);
      toast('调用失败：' + (e.message || e), 'err');
    } else {
      toast('调用失败：' + (e.message || e), 'err');
    }
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
    SKILL_INV_VIEW = d;
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
  if (!SKILL_INV_VIEW) { el.innerHTML = '<div class="hint">加载中…</div>'; return; }
  const wantName = (($('invName') || {}).value || '').trim();
  const wantCaller = (($('invCaller') || {}).value || '').trim();
  const all = SKILL_INV_VIEW.items || [];
  const items = all.filter(x =>
    (!wantName || fuzzyMatch([[x.name, 3]], wantName) > 0) &&
    (!wantCaller || fuzzyMatch([[x.caller, 3]], wantCaller) > 0));
  const cnt = $('invCount');
  if (cnt) cnt.textContent = '显示 ' + items.length + ' / ' + all.length + ' 条';
  if (sumEl) {
    const byCaller = (SKILL_INV_VIEW.summary && SKILL_INV_VIEW.summary.by_caller) || {};
    const byRoute = (SKILL_INV_VIEW.summary && SKILL_INV_VIEW.summary.by_route) || {};
    const bySkill = (SKILL_INV_VIEW.summary && SKILL_INV_VIEW.summary.by_skill) || {};
    const fmt = o => Object.keys(o).map(k => escapeHtml(k) + ' × ' + o[k]).join('，') || '无';
    const hot = Object.keys(bySkill).slice(0, 3).map(k => escapeHtml(k) + ' × ' + bySkill[k]).join('，') || '无';
    // 两列网格：标签列 max-content ⇒ 每个值的起点同一 x（旧写法是 flex 流式，
    // 标签与值被拆成并列项，折行位置随内容漂移，用户看到的就是「没对齐」）。
    const rows = [
      ['窗口内', '<b>' + (SKILL_INV_VIEW.matched_total != null ? SKILL_INV_VIEW.matched_total : 0) + '</b> 条'],
      ['按调用方', fmt(byCaller)],
      ['按技能来源', fmt(byRoute)],
      ['最热', hot],
    ];
    // 自动下发单独一组：这是「agent 在任务开始时被推了哪些技能」（subject=skill.relevant），
    // 与上面的「有人点名调用」是两种证据。混成一桶就是把曝光当使用，权重会失真。
    const auto = SKILL_INV_VIEW.auto;
    if (auto) {
      const un = auto.unknown_caller ? '（其中 ' + auto.unknown_caller + ' 次未自报调用方）' : '';
      // query/legacy 单列：它们是「同端点的另一种用途」，混进自动下发会把
      // 人工检索和旧账算成曝光，权重分母凭空变大（656 那次就是这么来的）。
      const other = [];
      if (auto.query) other.push('人工检索 ' + auto.query + ' 条');
      if (auto.legacy) other.push('旧事件未自报 ' + auto.legacy + ' 条');
      rows.push(['自动下发', '<b>' + (auto.total || 0) + '</b> 次 · 按调用方 ' +
        fmt(auto.by_caller || {}) + un +
        (other.length ? '（另有 ' + other.join('、') + '，不计入）' : '') +
        (auto.error ? ' <span class="sk-inv-bad">读取失败：' + escapeHtml(String(auto.error)) +
          '</span>' : '')]);
    }
    sumEl.innerHTML = rows.map(([k, v]) =>
      '<span class="sk-k">' + k + '</span><span class="sk-v">' + v + '</span>').join('') +
      '<span class="sk-note hint">汇总口径＝本次抓取窗口（' +
      (SKILL_INV_VIEW.window_fetched || 0) + ' 条，上限 ' + (SKILL_INV_VIEW.fetch_cap || 0) +
      '），不是全历史' +
      (SKILL_INV_VIEW.bad_detail ? '；' + SKILL_INV_VIEW.bad_detail + ' 条坏账已跳过' : '') +
      '</span>';
  }
  el.innerHTML = items.map(x => {
    // 运行日志的成功状态是 'success'（旧写法 'ok' 也兼容）：别把成功渲染成警告色。
    const ok = (x.status === 'ok' || x.status === 'success');
    const st = ok ? '<span class="sk-inv-ok">成功</span>'
      : '<span class="sk-inv-bad">' + escapeHtml(String(x.status || '?')) + '</span>';
    const who = String(x.name || '');
    const route = String(x.route || '');
    // 行内「查看」复用目录页同一条 read 链路（不记账）；从流水直接跳到技能详情，
    // 不用切回目录再搜一次。**必须带 route**：同名多路时不带会 409 或看错那一路。
    const viewBtn = who
      ? '<button class="btn sm" data-sk-view="1" data-name="' + escapeHtml(who) +
        '" data-route="' + escapeHtml(route) + '" title="查看该技能详情（read 不记账）">查看</button>'
      : '';
    // 时间只显示「月-日 时:分:秒」，完整时间戳进 title：右列定宽后逐行对齐，
    // 不再因为秒的小数位不同而参差。
    const ts = String(x.created_at || '');
    const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2}:\d{2})/);
    const tsShort = m ? m[2] + '-' + m[3] + ' ' + m[4] : ts;
    return '<div class="mem-item sk-inv-row">' +
      '<div class="sk-inv-main">' +
        '<div class="sk-inv-head">' +
          '<span class="sk-inv-name">' + escapeHtml(who || '（无名次）') + '</span>' +
          (route ? '<span class="sk-inv-route">' + escapeHtml(route) + '</span>' : '') +
          '<span class="sk-inv-caller">由 <b>' + escapeHtml(String(x.caller || 'anonymous')) +
            '</b> 调用' + (x.caller_reported ? '（自报）' : '') + '</span>' + st +
        '</div>' +
        (x.task ? '<div class="sk-inv-task">' + mdInline(String(x.task).slice(0, 120)) + '</div>' : '') +
        '<div class="sk-inv-meta">' +
          '<span class="sk-inv-time" title="' + escapeHtml(ts) + '">' + escapeHtml(tsShort) + '</span>' +
          '<span class="sk-inv-dur">' + (x.duration_ms != null ? x.duration_ms + ' ms' : '—') + '</span>' +
        '</div>' +
      '</div>' +
      '<div class="sk-inv-side">' + viewBtn + '</div></div>';
  }).join('') || ('<div class="sk-empty"><b>这个窗口内没有调用记录</b>（近 ' +
    (SKILL_INV_VIEW.days || 7) + ' 天）。<br>' +
    '这里只统计 <b>hub 通道</b>的显式调用（POST /api/skill/invoke 或 MCP hub_skill_invoke）；' +
    'agent 直接读磁盘的 SKILL.md 不经过 hub，因此不会出现在这里。' +
    (wantName || wantCaller ? '<br>当前有过滤条件，也可能是被过滤掉了。' : '') +
    '<br>想验证记账正常：到「技能目录」点任一技能，在抽屉里点「试调用（记账）」。' +
    '<br><span class="hint">若刚点过试调用仍为 0，多半是浏览器 TERM_TOKEN 与服务端不一致（401）——' +
    '旧凭据会被自动清空，重试时按提示输入当前 token 即可。</span>' +
    '<div style="margin-top:8px;display:flex;gap:8px;flex-wrap:wrap">' +
    (wantName || wantCaller ? '<button class="btn ghost sm" onclick="clearInvFilters()">清空过滤</button>' : '') +
    '<button class="btn ghost sm" onclick="switchToCatalog()">去技能目录</button></div></div>');
}

/* ── 权重榜视图（PT-20261020-01 的展示面，v0.13.140）────────────────────
 * 端点 `/api/skill/usage` 2026-10-20 就有了、14 例测试也齐，但页面**零入口**
 * ⇒ 「数据有了没人看」，与「登记不等于入役」同型。这里补渲染。
 *
 * 三条口径（写死在此，勿在渲染里另算）：
 *  ① 分数只在服务端算：前端重算必然与 `skill_weights` 漂移，漂移后「同一技能
 *     两个分数」就是第二个真相；
 *  ② 过滤打服务端：`caller` 是**先过滤再算分**，前端后过滤会把别人的曝光留在
 *     分母里（分数虚低），所以三个下拉一律触发重新请求；
 *  ③ 失败态与空态分开：空榜的语义是「这个窗口内没人用」，不能与 500 同形。 */
async function loadSkillUsage() {
  if (SKILL_USAGE_BUSY) return;
  SKILL_USAGE_BUSY = true;
  boxBusy('skillUsageList');
  const days = (($('usageDays') || {}).value || '30');
  const route = (($('usageRoute') || {}).value || '');
  const caller = (($('usageCaller') || {}).value || '');
  let qs = 'days=' + encodeURIComponent(days) + '&limit=200';
  if (route) qs += '&route=' + encodeURIComponent(route);
  if (caller) qs += '&caller=' + encodeURIComponent(caller);
  try {
    const d = await api('/api/skill/usage?' + qs);
    SKILL_USAGE = d || { items: [] };
    SKILL_USAGE.error = '';
    fillUsageFacets(SKILL_USAGE);
    renderSkillUsage();
  } catch (e) {
    SKILL_USAGE = { items: [], error: String((e && e.message) || e || '未知错误'), days: days };
    renderSkillUsage();
  } finally {
    SKILL_USAGE_BUSY = false;
  }
}

/* 下拉选项由**本次返回**聚合而来，不写死 agent 名单：
 * caller 是自报字段，写死清单会让新接入的 agent 永远筛不出来。 */
function fillUsageFacets(d) {
  const routes = new Set(), callers = new Set();
  (d.items || []).forEach(it => {
    if (it.route) routes.add(it.route);
    ['explicit', 'read', 'auto_use', 'auto'].forEach(b => {
      const m = (it.by_caller || {})[b] || {};
      Object.keys(m).forEach(c => callers.add(c));
    });
  });
  const fill = (id, label, set) => {
    const sel = $(id);
    if (!sel) return;
    const cur = sel.value;
    sel.innerHTML = '<option value="">' + label + '</option>' +
      Array.from(set).sort().map(x =>
        '<option value="' + escapeHtml(x) + '">' + escapeHtml(x) + '</option>').join('');
    if (cur && set.has(cur)) sel.value = cur;   // 刷新时保留用户的选择
  };
  fill('usageRoute', '全部发现点', routes);
  fill('usageCaller', '全部 agent', callers);
}

function renderSkillUsage() {
  const el = $('skillUsageList'), sum = $('skillUsageSummary');
  if (!el) return;
  const d = SKILL_USAGE;
  if (!d) { el.innerHTML = '<div class="hint">加载中…</div>'; return; }
  if (d.error) {
    // 失败与空分开：这里必须给出错误原文，否则「0 条」会被读成「没人用」。
    el.innerHTML = '<div class="hint" style="color:var(--st-error,var(--danger))">' +
      '读取权重失败：' + escapeHtml(d.error) + '</div>';
    if (sum) sum.innerHTML = '';
    return;
  }
  const items = d.items || [];
  const t = d.totals || {};
  if (sum) {
    /* 逐桶对账（v0.13.141）：`totals` 是全量聚合、`items` 是 limit 后的子集，
     * 无名事件又不进榜单 ⇒ 同一个「点名」有两个数。差额拆成两份、逐桶报出，
     * 而不是只给一个全局 no_name（那个数解释不了任何一桶）。 */
    const ub = t.unnamed_by_bucket || {};
    const lb = t.listed_by_bucket || {};
    const nb = t.named_by_bucket || {};
    // 静态文件是**即时生效**的，而 API 要等进程重启才有新字段 ⇒ 老后端 + 新前端
    // 会让 `lb[b]` 恒 0，把每一桶都说成「榜内 0」。没有 listed_by_bucket 就不显示差额，
    // 而不是显示一个假的 0。
    const hasListed = t.listed_by_bucket && typeof t.listed_items === 'number';
    const cell = (label, b) => {
      const total = t[b] || 0, listed = lb[b] || 0, off = hasListed ? total - listed : 0;
      const pre = label ? label + ' ' : '';
      if (off <= 0) return pre + '<b>' + total + '</b>';
      const cut = (nb[b] || 0) - listed;
      // 单位必须写清：total / listed / cut 数的是**事件条数**，而 listed_items /
      // dropped_by_limit 数的是**技能行数**。一行可含多条同名事件（如一次会话
      // 里点名 6 次），混着写成「拿掉 N 行」会让人以为少渲染了 N 个技能。
      // ⚠ `t.named` 是**有名事件数**不是行数（实测 927 vs 138），拿它当「共 M 行」
      // 会再犯一次同款量纲错；总行数只能用 listed_items + dropped_by_limit。
      const rowsTotal = (t.listed_items || 0) + (t.dropped_by_limit || 0);
      return pre + '<b>' + total + '</b><span class="hint" title="差额 ' + off +
        ' 条事件 = 无技能名 ' + (ub[b] || 0) + ' 条 + 榜外技能行占 ' + cut +
        ' 条（榜单渲染 ' + (t.listed_items || 0) + ' 行 / 共 ' + rowsTotal +
        ' 行）">（榜内 ' + listed + ' 条）</span>';
    };
    const rows = [
      ['窗口', '<b>' + escapeHtml(String(d.days || 30)) + '</b> 天'],
      ['使用', cell('点名', 'explicit') + ' · ' + cell('自动激活', 'auto_use') +
        ' · ' + cell('读取', 'read')],
      ['曝光', cell('', 'auto') + ' 次（只作分母，不计分）'],
      ['技能身份', escapeHtml(String(d.identity || '(name, route)')) +
        ' · 同名多路分开计'],
    ];
    const notes = [escapeHtml(String(d.formula || ''))];
    if (t.no_name) notes.push('另有 ' + t.no_name + ' 条事件没有技能名（旧账/计划态），未计入榜单');
    if (t.dropped_by_limit) {
      notes.push('榜单只渲染前 ' + (t.listed_items || 0) + ' 行，按分数排在后面的 ' +
        t.dropped_by_limit + ' 行未显示（汇总仍含它们）');
    }
    sum.innerHTML = rows.map(([k, v]) =>
      '<span class="sk-k">' + k + '</span><span class="sk-v">' + v + '</span>').join('') +
      '<span class="sk-note hint">' + notes.join('　·　') + '</span>';
  }
  const cnt = $('usageCount');
  if (cnt) {
    cnt.textContent = '显示 ' + items.length + ' 个技能' +
      (t.dropped_by_limit ? '（共 ' + (items.length + t.dropped_by_limit) + ' 个，其余被 limit 截断）' : '');
  }
  if (!items.length) {
    el.innerHTML = '<div class="sk-empty"><b>这个窗口内还没有可计分的技能</b>（近 ' +
      escapeHtml(String(d.days || 30)) + ' 天）。<br>' +
      '只有 <b>点名调用</b>（/api/skill/invoke）、<b>自动激活</b>（/api/skill/auto）与' +
      '<b>读正文</b>才计分；注入曝光只进分母。<br>' +
      '<span class="hint">若刚调用过仍是 0：先看「调用记录」页有没有这条流水——' +
      '流水有而这里空，说明事件缺 name 字段。</span></div>';
    return;
  }
  el.innerHTML = items.map(it => {
    const cls = String(it.usage_class || '');
    // 三类证据各自给数字，不做「加权后只剩一个总分」——那样没人能解释分数怎么来的。
    const parts = [
      '点名 <b>' + (it.explicit || 0) + '</b>',
      '自动激活 <b>' + (it.auto_use || 0) + '</b>',
      '读取 <b>' + (it.read || 0) + '</b>',
      '曝光 <b>' + (it.auto || 0) + '</b>',
    ];
    const byCaller = it.by_caller || {};
    const who = ['explicit', 'auto_use', 'read']
      .map(b => Object.keys(byCaller[b] || {}).map(c => c + '×' + byCaller[b][c]).join('、'))
      .filter(Boolean).join('；');
    const ev = it.evidence_total || {};
    const evAll = (ev.explicit || 0) + (ev.read || 0) + (ev.auto_use || 0);
    const ts = String(it.last_used_at || '');
    const m = ts.match(/^(\d{4})-(\d{2})-(\d{2})[T ](\d{2}:\d{2})/);
    const tsShort = m ? m[2] + '-' + m[3] + ' ' + m[4] : (ts || '—');
    return '<div class="mem-item sk-inv-row">' +
      '<div class="sk-inv-main">' +
        '<div class="sk-inv-head">' +
          '<span class="sk-inv-name">' + escapeHtml(String(it.name || '（无名）')) + '</span>' +
          (it.route ? '<span class="sk-inv-route">' + escapeHtml(String(it.route)) + '</span>' : '') +
          (cls ? '<span class="sk-inv-route">' + escapeHtml(cls) + '</span>' : '') +
          '<span class="sk-inv-caller">' + parts.join(' · ') + '</span>' +
        '</div>' +
        (who ? '<div class="sk-inv-meta"><span class="sk-inv-time">按调用方 ' +
          escapeHtml(who) + '</span></div>' : '') +
        '<div class="sk-inv-meta">' +
          '<span class="sk-inv-time" title="' + escapeHtml(ts) + '">最近 ' + escapeHtml(tsShort) + '</span>' +
          '<span class="sk-inv-dur">存续 ' + (it.idle_days != null ? it.idle_days + ' 天' : '—') + '</span>' +
          '<span class="sk-inv-dur" title="可追溯到 profile_events 的具体行 id">证据 ' +
            evAll + ' 条</span>' +
        '</div>' +
      '</div>' +
      '<div class="sk-inv-side"><b style="font-size:18px">' +
        escapeHtml(String(it.score != null ? it.score : '—')) + '</b>' +
        '<div class="hint">score</div></div>' +
      '</div>';
  }).join('');
}

/* 三个下拉共用一条委托：切换即重新请求（口径②：过滤必须打服务端）。
 * 用 data-usage-filter 而不是 inline onchange——与 §4.2 的委托口径一致。 */
function bindUsageFilters() {
  const bar = $('skillViewWeights');
  if (!bar || bar.__skUsage) return;
  bar.__skUsage = true;
  bar.addEventListener('change', e => {
    const t = e.target;
    if (!t || !t.dataset || t.dataset.usageFilter === undefined) return;
    loadSkillUsage();
  });
}

/* 空状态的出口：切回目录视图（不需要加载新数据）。 */
function switchToCatalog() { switchSkillView('catalog'); }

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
    const failed = (d.routes_failed || []).length
      ? ' · <b>' + d.routes_failed.length + ' 路扫描失败</b>（' + d.routes_failed.map(f => escapeHtml(f.route)).join(',') + '，total 偏低）'
      : '';
    // 子章节不是技能：`_is_nested_doc` 已把它们排除在名录外（否则模型可能选中分册、
    // 绕过父技能的强制规则）。排除必须可见，否则面板会变成「总数对不上却查不出为什么」。
    const nested = (d.nested_docs || []).length
      ? ' · ' + d.nested_docs.length + ' 条子文档已排除（' + d.nested_docs.map(n => escapeHtml(n)).join(',') + '，属父技能分册）'
      : '';
    /* 「为什么只剩 N 条」必须能回答（v0.13.141）。三段占用都由服务端给数，
     * 前端不重算：full_cap / full_used_est / name_used_est / remaining_est。
     * 注意算法是 `break`——下一个名字装不下就停，之后按名排序的整段一起丢，
     * 所以被裁必然是一段连续的尾部，而不是「随机丢了 308 条」。 */
    const why = (d.truncated && d.full_cap != null)
      ? '<br><span class="hint">为什么被裁 <b>' + d.truncated + '</b> 条：' +
        '全条目档位上限 ' + d.full_cap + ' tok（预算 70%）装了 ' + full.length + ' 条（' +
        d.full_used_est + ' tok）；其余 ' + (d.total - full.length) + ' 条降级为仅名，' +
        '仅名区可用 ' + Math.max(0, d.budget - d.full_used_est) + ' tok，装了 ' + names.length +
        ' 个（' + d.name_used_est + ' tok）；剩 ' + d.remaining_est + ' tok 装不下下一个名字' +
        (d.truncated_head ? '「' + escapeHtml(d.truncated_head) + '」' : '') +
        ' ⇒ 从它起按名排序其后 ' + d.truncated + ' 条全部丢弃（不是随机少）。</span>'
      : '';
    $('skillBudget').innerHTML =
      '范围 ' + escapeHtml(scope) + ' · 预算 ' + d.budget + ' tok · 实际约 ' + d.used_est + '（' + pct + '%） · 全条目 ' + full.length + ' / 仅名 ' + names.length +
      (d.truncated ? ' / <b>被裁 ' + d.truncated + '</b>' : '') + '（共 ' + d.total + '）' + unknown + failed + nested + why + '<br>' +
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
