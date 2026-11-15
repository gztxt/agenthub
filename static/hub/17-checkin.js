/* v0.13.201 · 积分签到页（系统菜单第 11 项）：A/B 两机 AIAutomator 远程管理。
 *
 * 数据面：GET /api/checkin/overview —— 服务端只读投影 tailcat connect-targets.tsv
 * + hub 侧启用态/执行流水；触发走 POST /api/checkin/run（mode=plan 默认 dry）。
 * 前端不直连 Windows：凭据只在各机本地、跨网需 tailcat 隧道、CORS 也挡。
 * 三种「没有」必须分开渲染（静默不可用是本仓最禁的失败形态）：
 *   offline    = ssh/隧道连不上（关机 / tailcat serve 未起）
 *   unreadable = 连上了但 bridge 没吐合法 JSON（文件缺失 / Python 报错）
 *   ok && 空   = 通了、结构正常、确实没数据
 * ⚠ 顶层必须 var：本分片排在 01 之后，go() 启动期就会调到 loadCheckin（TDZ 护栏）。
 */
var ckBound = false;
var ckBusy = false;

function ckStamp(ts) {
  if (!ts) return '—';
  var d = new Date(ts * 1000), p = function (n) { return String(n).padStart(2, '0'); };
  return p(d.getMonth() + 1) + '-' + p(d.getDate()) + ' ' + p(d.getHours()) + ':' + p(d.getMinutes());
}

function initCheckin() {
  if (ckBound) return;
  ckBound = true;
  var root = $('page-checkin');
  if (!root) return;
  // data-ck 委托：唯一出口，页内零 inline onclick
  root.addEventListener('click', function (e) {
    var b = e.target.closest('[data-ck]');
    if (!b) return;
    var act = b.dataset.ck, name = b.dataset.name || '';
    if (act === 'refresh') { loadCheckin(true); return; }
    if (act === 'probe') { ckRun(name, 'probe', 'plan'); return; }
    if (act === 'dry') { ckRun(name, 'run', 'plan'); return; }
    if (act === 'exec') {
      if (confirm('立即在 ' + name + ' 上执行每日签到？接口幂等（已签只提示），但仍会访问各平台官方接口。'))
        ckRun(name, 'run', 'exec');
    }
    if (act === 'accts') { ckAccLoad(name, true); return; }
    if (act === 'acc-send') { ckAccSend(name); return; }
    if (act === 'acc-add') { ckAccAdd(name); return; }
    if (act === 'acc-import') { ckAccImport(name, b.dataset.plat || 'traework'); return; }
    if (act === 'acc-del') {
      if (confirm('删除账号 ' + (b.dataset.label || '') + ' 的本地凭据？删除后该账号不再自动签到。'))
        ckAccDel(name, b.dataset.plat || '', b.dataset.uid || '');
    }
  });
}

function ckReason(r) {
  // overview.last 把 reason 落在 payload JSON 里，流水行直接有 error 前缀
  if (!r) return '';
  if (r.reason) return r.reason;
  try {
    var p = typeof r.payload === 'string' ? JSON.parse(r.payload) : (r.payload || {});
    return p.reason || '';
  } catch (e) { return ''; }
}

function ckResultLine(r) {
  if (!r) return '<span class="hint">尚未在本页执行过</span>';
  var st = r.state, ok = r.ok;
  var badge = ok ? '<span class="s-badge running"></span>' : '<span class="s-badge error"></span>';
  var label = ok ? '正常' : (st === 'offline' ? '连不上（offline）' : '不可判定（unreadable）');
  var reason = ckReason(r);
  var why = reason ? ' · <code>' + escapeHtml(reason) + '</code>' : '';
  var detail = r.error ? '<div class="hint">' + escapeHtml(String(r.error)) + '</div>' : '';
  return badge + escapeHtml(label) + why + ' · ' + escapeHtml(ckStamp(r.ts)) + detail;
}

/* reason → 可操作指引（三层分开，别把「控制面通但 22 不通」说成「机器离线」） */
function ckGuide(reason) {
  var g = {
    'data-plane-down': '实测证据链：控制面 ping 通、token 最新、该机 serve/sshd 正常（B→A 通道通），唯独 NAS→该机拨号不通 ⇒ 多为该机 tailcat <code>--allow</code> 白名单缺 NAS nodekey（重新部署后易丢）。在**该机控制台跑一次 [14]**（主动拉取 allowlist 上线自愈），或用 [9] 热更新授权；恢复后 hub 直连即可用。',
    'control-plane-down': 'tailcat ping 不通：机器离线或 DERP 中继不可达，确认该机联网并运行 tailcat 服务。',
    'bridge-missing': '远端没有 aia_bridge.py：该机 AIAutomator 未部署成功（通道已通才会到这一步），需重传分发包并用 Python tarfile 解包。',
    'timeout': '执行超时：单次探测已绕过重试；若仍超时多为中继链路慢，检查 DERP 路径。',
    'spawn-error': '本地命令起不来，检查 hub 侧 tailcat/ssh 是否在 PATH。'
  };
  return g[reason] || '';
}

function ckDate(ts) {
  if (!ts) return '—';
  var d = new Date(ts * 1000), p = function (n) { return String(n).padStart(2, '0'); };
  return d.getFullYear() + '-' + p(d.getMonth() + 1) + '-' + p(d.getDate());
}

function ckDataSummary(r) {
  // 最近一次 run 的平台明细：平台 / 状态 / 本次积分 / 消息 + 执行日期 + 账户总积分。
  // v0.13.203 用户裁定：重复签到（already）本次积分恒显示 0 —— 重复领取不重复计分。
  // 两种入参形状都要接住（实测坑）：机器卡传的是流水 DB 行（数据在 payload JSON 串里），
  // 实时返回才有 r.data —— 只认后者会让明细整块静默消失（正是用户报的「没有显示积分」）。
  if (!r) return '';
  var d = r.data;
  if (!d && typeof r.payload === 'string') {
    try { d = JSON.parse(r.payload).data; } catch (e) { d = null; }
  } else if (!d && r.payload && typeof r.payload === 'object') {
    d = r.payload.data;
  }
  if (!d) return '<div class="hint">本次结果无平台明细（payload 缺 data）</div>';
  var res = (d.data && d.data.results) || [];
  if (!res.length) return '<div class="hint">本次无平台结果（bridge 未返回 results）</div>';
  var rows = res.map(function (x) {
    var st = String(x.status || '');
    // already 强制 0（不信任上游字段）；成功取 credits；失败 0
    var cr = (st === 'already') ? 0 : (Number(x.credits) || 0);
    var label = st === 'already' ? '今日已签' : (st === 'success' ? '签到成功'
               : (st === 'skipped' ? '跳过' : '失败'));
    var crTxt = (x.success || st === 'already') ? ('+' + cr + ' 分') : '0 分';
    return '<div>' + escapeHtml(String(x.platform)) + ' · ' + label +
      ' · <b>' + crTxt + '</b>' +
      (x.message ? ' <span class="hint">' +
        escapeHtml(String(x.message).slice(0, 48)) + '</span>' : '') +
      '</div>';
  }).join('');
  var tot = ((d.data && d.data.totals) || [])
    .filter(function (t) { return t.available && t.total != null; })
    .map(function (t) { return escapeHtml(String(t.platform)) + ' ' + t.total + ' 分'; })
    .join(' · ');
  return '<div class="hint">' + escapeHtml(ckDate(r.ts)) + ' 执行</div>' + rows +
    (tot ? '<div class="hint">总积分：' + tot + '</div>' : '');
}

/* v0.13.204 多账号（用户报「只能签当前登录的账号」的正面修复）：
 * - 账号列表 / 短信添加 / 导入本机登录态 / 删除 全部搬进 hub，不必再到 Windows 命令行；
 * - WorkBuddy = 手机号 + 短信码逐账号 add；TraeWork = 导入本机客户端登录态；
 * - 凭据库为空时明确告警（上游会回退桌面单账号），绝不静默；
 * - DuMate / LobsterAI / MiniMax 上游没有多账号能力，也必须写明，而不是不显示。
 * 读账号要起一次 ssh/隧道进程，按需点击读取（不随进页自动拉）。
 */
var ckAccBusy = {};
var ckAccName = { workbuddy: 'WorkBuddy', traework: 'TraeWork' };

function ckAccBox(name) {
  return document.querySelector('.ck-acc[data-acc-for="' + name + '"]');
}

function ckAccInner(r) {
  var d = r && r.data;
  return (d && d.data) || null;   // bridge 外层 {ok,data:{...}} → 内层
}

function ckAccStateLine(ma) {
  if (!ma) return '<span class="hint">未知</span>';
  if (ma.error) return '<span class="s-badge error"></span>' + escapeHtml(ma.error);
  var n = ma.accounts || 0;
  var head = ma.effective
    ? '<span class="s-badge running"></span>多账号已开启 · ' + n + ' 个账号'
    : '<span class="s-badge error"></span>多账号未开启（只签当前登录账号）';
  if (ma.effective && n === 0)
    head += ' · <b>凭据库为空，正在回退签当前登录账号</b>';
  return head;
}

function ckAccRow(plat, it, name) {
  var meta = [];
  if (it.label) meta.push(it.label);
  if (it.uid_masked) meta.push('UID ' + it.uid_masked);
  if (it.days_left != null) meta.push('剩余 ' + it.days_left + ' 天');
  if (it.expired) meta.push('已过期');
  if (it.disabled) meta.push('已失效需重登');
  var del = it.uid ? ' <button class="btn sm ghost" data-ck="acc-del" data-name="' +
    escapeHtml(name) + '" data-plat="' + escapeHtml(plat) + '" data-uid="' +
    escapeHtml(it.uid) + '" data-label="' + escapeHtml(it.label || it.uid_masked) +
    '">删除</button>' : '';
  return '<div class="hint">· ' + escapeHtml(meta.join(' · ') || '(无展示名)') + del + '</div>';
}

function ckAccRender(name, r) {
  var box = ckAccBox(name);
  if (!box) return;
  var st = box.querySelector('[data-acc-status]');
  var body = box.querySelector('[data-acc-body]');
  if (!r || !r.ok) {
    if (st) st.innerHTML = '<span class="s-badge error"></span>读取失败';
    if (body) body.innerHTML = '<div class="hint">' + escapeHtml((r && (r.error || r.reason)) || '未知错误') +
      (r && r.reason ? ' <code>' + escapeHtml(r.reason) + '</code>' : '') + '</div>';
    return;
  }
  var inner = ckAccInner(r);
  if (!inner) {
    if (st) st.innerHTML = '<span class="s-badge error"></span>unreadable';
    if (body) body.innerHTML = '<div class="hint">bridge 未返回账号数据（版本漂移或远端未更新到 v0.13.204）</div>';
    return;
  }
  if (st) st.innerHTML = r.cached ? '<span class="hint">缓存 ' + (r.age || 0) + ' 秒前</span>'
    : '<span class="hint">已读取</span>';
  var html = '';
  ['workbuddy', 'traework'].forEach(function (plat) {
    var ma = (inner.multi_account || {})[plat];
    var items = (inner.accounts || {})[plat] || [];
    html += '<div style="margin-top:8px"><div class="hint"><b>' + ckAccName[plat] + '</b> · ' +
      ckAccStateLine(ma) + '</div>';
    if (!items.length) html += '<div class="hint">· 凭据库为空</div>';
    else items.forEach(function (it) { html += ckAccRow(plat, it, name); });
    if (plat === 'workbuddy') {
      // WorkBuddy 登录是微信扫码：扫码在官方客户端完成，hub 只导入扫码后的登录态；
      // 手机号+短信是备用方式（用户要求两条都要）。
      html += '<div class="sp-r" style="margin-top:4px;gap:6px">' +
        '<button class="btn sm" data-ck="acc-import" data-name="' + escapeHtml(name) +
        '" data-plat="workbuddy">导入扫码后的客户端登录态</button>' +
        '<span class="hint">先在客户端用微信扫码登录</span></div>' +
        '<div class="sp-r" style="margin-top:4px;flex-wrap:wrap;gap:4px">' +
        '<input class="inp" data-acc-phone placeholder="手机号（备用）" style="width:120px;padding:4px 8px">' +
        '<button class="btn sm ghost" data-ck="acc-send" data-name="' + escapeHtml(name) + '">发送验证码</button>' +
        '<input class="inp" data-acc-code placeholder="短信验证码" style="width:100px;padding:4px 8px">' +
        '<button class="btn sm" data-ck="acc-add" data-name="' + escapeHtml(name) + '">添加</button></div>' +
        '<div class="hint">扫码账号：在客户端逐个扫码后回这里点导入；手机号是备用方式。凭据库非空后桌面账号不再自动叠加，当前账号也要导入/添加。</div>';
    } else {
      html += '<div class="sp-r" style="margin-top:4px">' +
        '<button class="btn sm ghost" data-ck="acc-import" data-name="' + escapeHtml(name) +
        '" data-plat="traework">导入本机登录态</button></div>' +
        '<div class="hint">只导入本机客户端当前登录账号；其他账号需在该机浏览器登录（端口 17388）。</div>';
    }
    html += '</div>';
  });
  html += '<div class="hint" style="margin-top:8px">DuMate / LobsterAI / MiniMax 上游不支持多账号，只签当前登录账号。</div>';
  if (body) body.innerHTML = html;
}

async function ckAccLoad(name, refresh) {
  if (ckAccBusy[name]) return;
  ckAccBusy[name] = true;
  var box = ckAccBox(name);
  var st = box && box.querySelector('[data-acc-status]');
  if (st) st.innerHTML = '<span class="hint">读取中（要起一次远程进程）…</span>';
  try {
    var r = await api('/api/checkin/accounts?name=' + encodeURIComponent(name) +
      (refresh ? '&refresh=1' : ''));
    ckAccRender(name, r);
  } catch (e) {
    if (st) st.innerHTML = '<span class="s-badge error"></span>读取失败';
    var body = box && box.querySelector('[data-acc-body]');
    if (body) body.innerHTML = '<div class="hint">' + escapeHtml(e.message || String(e)) + '</div>';
  } finally {
    ckAccBusy[name] = false;
  }
}

function ckAccNote(name, html) {
  var box = ckAccBox(name);
  var st = box && box.querySelector('[data-acc-status]');
  if (st) st.innerHTML = html;
}

function ckAccCtx(name) {
  var box = ckAccBox(name);
  return {
    phone: box && box.querySelector('[data-acc-phone]'),
    code: box && box.querySelector('[data-acc-code]')
  };
}

function ckAccBridgeMsg(r, fallback) {
  var d = r && r.data && r.data.data;
  return (d && (d.message || (d.account && d.account.label))) || fallback;
}

async function ckAccSend(name) {
  var c = ckAccCtx(name);
  var phone = (c.phone && c.phone.value || '').trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) { toast('请输入 11 位大陆手机号', 'err'); return; }
  ckAccNote(name, '<span class="hint">发送验证码中…</span>');
  try {
    var r = await api('/api/checkin/accounts/send-code', { method: 'POST',
      body: JSON.stringify({ name: name, platform: 'workbuddy', phone: phone }) });
    if (!r.ok) throw new Error(r.error || r.reason || '发送失败');
    var msg = ckAccBridgeMsg(r, '验证码已发送');
    ckAccNote(name, '<span class="s-badge running"></span>' + escapeHtml(msg));
    toast(msg, 'ok');
  } catch (e) {
    ckAccNote(name, '<span class="s-badge error"></span>发送失败：' + escapeHtml(e.message || String(e)));
    toast('发送失败：' + (e.message || String(e)), 'err');
  }
}

async function ckAccAdd(name) {
  var c = ckAccCtx(name);
  var phone = (c.phone && c.phone.value || '').trim();
  var code = (c.code && c.code.value || '').trim();
  if (!/^1[3-9]\d{9}$/.test(phone)) { toast('请输入 11 位大陆手机号', 'err'); return; }
  if (!/^\d{4,8}$/.test(code)) { toast('请输入短信验证码', 'err'); return; }
  ckAccNote(name, '<span class="hint">登录并保存凭据中…</span>');
  try {
    var r = await api('/api/checkin/accounts/add', { method: 'POST',
      body: JSON.stringify({ name: name, platform: 'workbuddy', phone: phone, code: code }) });
    if (!r.ok) throw new Error(r.error || r.reason || '添加失败');
    if (c.code) c.code.value = '';
    ckAccNote(name, '<span class="s-badge running"></span>已添加，正在刷新…');
    toast('账号已添加', 'ok');
    ckAccLoad(name, true);
  } catch (e) {
    ckAccNote(name, '<span class="s-badge error"></span>添加失败：' + escapeHtml(e.message || String(e)));
    toast('添加失败：' + (e.message || String(e)), 'err');
  }
}

async function ckAccImport(name, plat) {
  ckAccNote(name, '<span class="hint">读取本机客户端登录态…</span>');
  try {
    var r = await api('/api/checkin/accounts/import', { method: 'POST',
      body: JSON.stringify({ name: name, platform: plat }) });
    if (!r.ok) throw new Error(r.error || r.reason || '导入失败');
    ckAccNote(name, '<span class="s-badge running"></span>已导入，正在刷新…');
    toast('已导入本机登录态', 'ok');
    ckAccLoad(name, true);
  } catch (e) {
    ckAccNote(name, '<span class="s-badge error"></span>导入失败：' + escapeHtml(e.message || String(e)));
    toast('导入失败：' + (e.message || String(e)), 'err');
  }
}

async function ckAccDel(name, plat, uid) {
  ckAccNote(name, '<span class="hint">删除凭据中…</span>');
  try {
    var r = await api('/api/checkin/accounts/remove', { method: 'POST',
      body: JSON.stringify({ name: name, platform: plat, uid: uid }) });
    if (!r.ok) throw new Error(r.error || r.reason || '删除失败');
    ckAccNote(name, '<span class="s-badge running"></span>已删除，正在刷新…');
    toast('账号已删除', 'ok');
    ckAccLoad(name, true);
  } catch (e) {
    ckAccNote(name, '<span class="s-badge error"></span>删除失败：' + escapeHtml(e.message || String(e)));
    toast('删除失败：' + (e.message || String(e)), 'err');
  }
}

function ckMachineCard(m, last) {
  var reg = m.registered ? '' : '<span class="hint">（tailcat 清单里已无此机）</span>';
  var dis = m.enabled ? '' : '<span class="hint">（已停用）</span>';
  var meta = [m.channel === 'tailcat' ? 'tailcat 隧道' : 'LAN SSH', m.host || m.target]
    .filter(Boolean).map(escapeHtml).join(' · ');
  var r = last && last[m.name];
  return '<div class="sp-cell">' +
    '<div class="sp-sub">' + escapeHtml(m.name) + ' ' + reg + dis + '</div>' +
    '<div class="hint">' + meta + (m.note ? ' · ' + escapeHtml(m.note) : '') + '</div>' +
    '<div class="hint">' + ckResultLine(r) + '</div>' +
    (r && !r.ok && ckGuide(ckReason(r))
      ? '<div class="hint">处理建议：' + ckGuide(ckReason(r)) + '</div>' : '') +
    '<div class="sp-r" style="margin-top:6px">' +
      '<button class="btn sm ghost" data-ck="probe" data-name="' + escapeHtml(m.name) + '">探测</button> ' +
      '<button class="btn sm ghost" data-ck="dry" data-name="' + escapeHtml(m.name) + '">试跑(dry)</button> ' +
      '<button class="btn sm" data-ck="exec" data-name="' + escapeHtml(m.name) + '"' +
        (m.enabled ? '' : ' disabled') + '>立即签到</button>' +
    '</div>' + ckDataSummary(r) +
    '<div class="ck-acc" data-acc-for="' + escapeHtml(m.name) + '">' +
      '<div class="sp-r" style="margin-top:8px;gap:6px">' +
        '<button class="btn sm ghost" data-ck="accts" data-name="' + escapeHtml(m.name) + '">多账号</button>' +
        '<span class="hint" data-acc-status>未读取</span>' +
      '</div>' +
      '<div data-acc-body></div>' +
    '</div>' +
    '</div>';
}

function ckRender(d) {
  var status = $('ckStatus'), box = $('ckMachines'), hint = $('ckHint');
  if (!d || !d.ok) {
    if (status) status.innerHTML = '<span class="s-badge error"></span>端点不可用';
    if (hint) hint.textContent = 'GET /api/checkin/overview 失败；若刚升级，多半是后端还没重启到本版本。';
    return;
  }
  var ms = d.machines || [], last = d.last || {};
  if (status) status.innerHTML = '<span class="s-badge running"></span>' + ms.length +
    ' 台机器 · 清单源 <code>' + escapeHtml(d.tailcat_tsv || '') + '</code>';
  if (hint) hint.textContent = d.read_only_note || '';
  if (box) box.innerHTML = ms.length
    ? ms.map(function (m) { return ckMachineCard(m, last); }).join('')
    : '<div class="hint">还没有登记机器（首次进页会自动从 tailcat 清单播种 A/B 两台）。</div>';
}

async function loadCheckin(force) {
  initCheckin();
  if (ckBusy) return;
  ckBusy = true;
  var status = $('ckStatus');
  if (status) status.innerHTML = '<span class="hint">读取中…</span>';
  try {
    var d = await api('/api/checkin/overview');
    ckRender(d);
    var runs = await api('/api/checkin/runs?limit=10');
    var rb = $('ckRuns');
    if (rb) rb.innerHTML = (runs.runs || []).length
      ? runs.runs.map(function (r) {
          return '<tr><td class="hint">' + escapeHtml(ckStamp(r.ts)) + '</td>' +
            '<td>' + escapeHtml(r.machine) + '</td><td>' + escapeHtml(r.cmd) + '</td>' +
            '<td>' + escapeHtml(r.mode) + '</td><td>' + ckResultLine(r) + '</td></tr>';
        }).join('')
      : '<tr><td colspan="5" class="hint">暂无执行流水</td></tr>';
    if (force) toast('已刷新积分签到概览', 'ok');
  } catch (e) {
    if (status) status.innerHTML = '<span class="s-badge error"></span>端点不可用';
    var h = $('ckHint');
    if (h) h.innerHTML = '读取失败：' + escapeHtml(e.message || String(e)) +
      '<br>若刚升级：多半是后端还没重启到本版本（<code>GET /api/checkin/overview</code> 404）。';
  } finally {
    ckBusy = false;
  }
}

async function ckRun(name, cmd, mode) {
  var status = $('ckStatus');
  if (status) status.innerHTML = '<span class="hint">正在 ' + escapeHtml(name) + ' 上执行 ' +
    escapeHtml(cmd) + '（' + escapeHtml(mode) + '）… 远程执行串行，请稍候</span>';
  try {
    var r = await api('/api/checkin/run', { method: 'POST',
      body: JSON.stringify({ name: name, cmd: cmd, mode: mode }) });
    var line = r.ok ? '<span class="s-badge running"></span>成功' :
      '<span class="s-badge error"></span>' + escapeHtml(r.state) +
      (r.reason ? ' · <code>' + escapeHtml(r.reason) + '</code>' : '') +
      ': ' + escapeHtml(r.error || '');
    if (status) status.innerHTML = line + ' · ' + escapeHtml(name) +
      (r.ok ? '' : '<div class="hint">处理建议：' + (ckGuide(r.reason) || '查看下方流水 stderr 明细') + '</div>');
    toast(r.ok ? (name + ' ' + cmd + ' 完成') :
      (name + ' 执行失败：' + r.reason + ' · ' + (r.error || r.state)), r.ok ? 'ok' : 'err');
    loadCheckin(false);   // 用真流水重渲染，不靠本地拼状态
  } catch (e) {
    if (status) status.innerHTML = '<span class="s-badge error"></span>触发失败';
    toast('触发失败：' + (e.message || String(e)), 'err');
  }
}
