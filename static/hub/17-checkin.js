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
  });
}

function ckResultLine(r) {
  if (!r) return '<span class="hint">尚未在本页执行过</span>';
  var st = r.state, ok = r.ok;
  var badge = ok ? '<span class="s-badge running"></span>' : '<span class="s-badge error"></span>';
  var label = ok ? '正常' : (st === 'offline' ? '连不上（offline）' : '不可判定（unreadable）');
  return badge + escapeHtml(label) + ' · ' + escapeHtml(ckStamp(r.ts));
}

function ckDataSummary(r) {
  // 最近一次 run 的 results/healths/totals 摘要（三态：没数据 ≠ 空团队）
  var d = r && r.data;
  if (!d) return '';
  var res = (d.data && d.data.results) || [];
  if (!res.length) return '<div class="hint">本次无平台结果（bridge 未返回 results）</div>';
  var okc = res.filter(function (x) { return x.success; }).length;
  return '<div class="hint">签到 ' + okc + '/' + res.length + ' 成功 · ' +
    res.map(function (x) { return escapeHtml(String(x.platform)) + ':' +
      escapeHtml(String(x.status)); }).join(' · ') + '</div>';
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
    '<div class="sp-r" style="margin-top:6px">' +
      '<button class="btn sm ghost" data-ck="probe" data-name="' + escapeHtml(m.name) + '">探测</button> ' +
      '<button class="btn sm ghost" data-ck="dry" data-name="' + escapeHtml(m.name) + '">试跑(dry)</button> ' +
      '<button class="btn sm" data-ck="exec" data-name="' + escapeHtml(m.name) + '"' +
        (m.enabled ? '' : ' disabled') + '>立即签到</button>' +
    '</div>' + ckDataSummary(r) +
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
      '<span class="s-badge error"></span>' + escapeHtml(r.state) + ': ' + escapeHtml(r.error || '');
    if (status) status.innerHTML = line + ' · ' + escapeHtml(name);
    toast(r.ok ? (name + ' ' + cmd + ' 完成') : (name + ' 执行失败：' + r.state), r.ok ? 'ok' : 'err');
    loadCheckin(false);   // 用真流水重渲染，不靠本地拼状态
  } catch (e) {
    if (status) status.innerHTML = '<span class="s-badge error"></span>触发失败';
    toast('触发失败：' + (e.message || String(e)), 'err');
  }
}
