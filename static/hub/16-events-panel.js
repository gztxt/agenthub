/* 当前会话的结构化事件面板（P3 v1，自动关联）。 */
let evTimer = null;
let evLastSig = '';

function evToggle(show) {
  const termBody = document.querySelector('#termPane .term-body');
  const box = $('evBody');
  if (!termBody || !box) return;
  const want = (typeof show === 'boolean') ? show : (box.style.display === 'none');
  box.style.display = want ? 'flex' : 'none';
  termBody.style.display = want ? 'none' : '';
  if (want) { evLastSig = ''; evLoad(); if (!evTimer) evTimer = setInterval(evLoad, 2000); }
  else { if (evTimer) { clearInterval(evTimer); evTimer = null; } if (typeof termRepaint === 'function') termRepaint(true); }
}

function evItem(cls, key) {
  const list = $('evList'); if (!list) return null;
  const d = document.createElement('div');
  d.className = 'ev-item' + (cls ? ' ' + cls : '');
  if (key) { const k = document.createElement('span'); k.className = 'ev-k'; k.textContent = key; d.appendChild(k); }
  list.appendChild(d); return d;
}

function evScroll() { const l = $('evList'); if (l) l.scrollTop = l.scrollHeight; }
function _evTime(ts) { const m = /T(\d{2}:\d{2}:\d{2})/.exec(String(ts || '')); return m ? m[1] : ''; }

function evRender(ev) {
  if (!ev || typeof ev !== 'object') return;
  const t = ev.type, tm = _evTime(ev.ts), head = (tm ? tm + ' · ' : '') + t;
  if (t === 'turn_started') {
    const d = evItem('', head + ' · 第 ' + (ev.turn || '?') + ' 轮');
    if (ev.message) { const p = document.createElement('div'); p.className = 'ev-text'; p.textContent = ev.message; d.appendChild(p); }
  } else if (t === 'message_delta') {
    const d = evItem('ev-text', head); const b = document.createElement('div'); b.textContent = ev.text || ''; d.appendChild(b);
  } else if (t === 'tool_call' || t === 'tool_result') {
    const d = evItem('', head + (ev.name ? ' · ' + ev.name : '') + (ev.call_id ? ' · ' + String(ev.call_id).slice(0, 12) : ''));
    const det = document.createElement('details'), sum = document.createElement('summary');
    sum.textContent = (t === 'tool_call') ? '查看入参' : ('查看结果' + (ev.status ? ' · ' + ev.status : ''));
    const pre = document.createElement('pre'); const val = (t === 'tool_call') ? ev.input : ev.output;
    pre.textContent = (typeof val === 'string') ? val : JSON.stringify(val == null ? '' : val, null, 2);
    det.appendChild(sum); det.appendChild(pre); d.appendChild(det);
  } else if (t === 'usage') {
    const parts = []; Object.keys(ev).forEach(function (k) { if (['v','seq','ts','sid','turn_id','agent_id','type'].indexOf(k) < 0) parts.push(k + '=' + ev[k]); });
    evItem('', head + ' · ' + parts.join(' '));
  } else if (t === 'turn_completed') {
    evItem('', head + ' · success=' + !!ev.success + (ev.ms != null ? ' · ' + ev.ms + 'ms' : '') + (ev.reason ? ' · ' + ev.reason : ''));
  } else if (t === 'error') {
    const d = evItem('ev-error', head + ' · ' + (ev.reason || ''));
    if (ev.message) { const m = document.createElement('div'); m.textContent = ev.message; d.appendChild(m); }
  } else {
    const d = evItem('', head); const pre = document.createElement('pre'); pre.textContent = JSON.stringify(ev, null, 2); d.appendChild(pre);
  }
}

function evSummary(d) {
  const el = $('evSum'); if (!el) return;
  const s = d.summary || {}; const bits = [];
  if (d.session_id) bits.push('会话 ' + String(d.session_id).slice(0, 8));
  if (d.transcript) bits.push('转录 ' + String(d.transcript).slice(0, 8));
  if (s.turns != null) bits.push(s.turns + ' 轮');
  if (s.messages != null) bits.push(s.messages + ' 条回复');
  if (s.tool_calls != null) bits.push(s.tool_calls + ' 次工具');
  if (s.tokens != null) bits.push('上下文 ' + s.tokens + ' tokens');
  if (s.duration_ms != null) bits.push('时长 ' + Math.round(s.duration_ms / 1000) + 's');
  bits.push(d.live ? '● 进行中' : '已结束');
  el.textContent = bits.join(' · '); el.className = 'ev-sum' + (d.live ? ' live' : '');
}

function evRenderAll(d) {
  evSummary(d);
  const hint = $('evSidHint'); if (hint) hint.textContent = d.agent_id || '';
  if (d.supported === false) {
    const list = $('evList'); if (list) { list.textContent = ''; const x = document.createElement('div'); x.className = 'hint'; x.textContent = d.note || '该会话类型暂不支持结构化事件。'; list.appendChild(x); }
    return;
  }
  const sig = JSON.stringify([d.transcript, d.total_events, d.live, d.summary && d.summary.updated]);
  if (sig === evLastSig) return; evLastSig = sig;
  const list = $('evList'); if (!list) return; list.textContent = '';
  const evs = d.events || [];
  if (!evs.length) { const x = document.createElement('div'); x.className = 'hint'; x.textContent = d.note || '当前会话还没有可展示的事件。'; list.appendChild(x); return; }
  evs.forEach(evRender); evScroll();
}

function evLoad() {
  const hint = $('evSidHint');
  if (!termSid) {
    if (hint) hint.textContent = '无活动会话';
    const list = $('evList'); if (list) { list.textContent = ''; const x = document.createElement('div'); x.className = 'hint'; x.textContent = '先在左侧打开一个终端会话，事件会随该会话自动展示。'; list.appendChild(x); }
    return;
  }
  const sid = termSid;
  fetch('/api/term/sessions/' + encodeURIComponent(sid) + '/events?limit=300', { credentials: 'same-origin', cache: 'no-store' })
    .then(function (r) { if (!r.ok) throw new Error('HTTP ' + r.status); return r.json(); })
    .then(function (d) { if (sid !== termSid) return; evRenderAll(d); })
    .catch(function (e) { const el = $('evSum'); if (el) el.textContent = '事件读取失败：' + e.message; });
}

function evInterrupt() {
  if (!termSid) { toast('当前没有活动会话', 'err'); return; }
  if (typeof termSend !== 'function') { toast('终端未连接', 'err'); return; }
  termSend('\x03'); toast('已向当前会话发送 Ctrl+C', 'ok');
}

