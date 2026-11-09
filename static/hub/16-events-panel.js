/* P3 v1 结构化事件面板（与 xterm 终端并列、可切换）。
   硬约束：事件只走 POST /api/agents/{id}/chat/events 的 SSE，绝不 term.write()
   （那条教训见 03-agents-cards.js：JSON 帧写进终端会被当正文画出来）。
   中断按钮调 POST /api/agents/{id}/interrupt：200 已发 / 404 无活动运行 / 409 不支持。 */
let evSid = '';
let evCtrl = null;
let evRunning = false;
let evTextEl = null;

function evToggle(show) {
  const termBody = document.querySelector('#termPane .term-body');
  const box = $('evBody');
  if (!termBody || !box) return;
  const want = (typeof show === 'boolean') ? show : (box.style.display === 'none');
  box.style.display = want ? 'flex' : 'none';
  termBody.style.display = want ? 'none' : '';
  /* 回到终端必须强制 fit：隐藏期间 FitAddon 量不到高度，不重排会只剩半屏。 */
  if (!want && typeof termRepaint === 'function') termRepaint(true);
}

function evBusy(on) {
  evRunning = !!on;
  const send = $('evSendBtn'), stop = $('evInterrupt');
  if (send) { send.disabled = !!on; send.textContent = on ? '流式中…' : '发送事件流'; }
  if (stop) stop.disabled = !on;
}

function evItem(cls, key) {
  const list = $('evList');
  if (!list) return null;
  const d = document.createElement('div');
  d.className = 'ev-item' + (cls ? ' ' + cls : '');
  if (key) {
    const k = document.createElement('span');
    k.className = 'ev-k';
    k.textContent = key;
    d.appendChild(k);
  }
  list.appendChild(d);
  return d;
}

function evScroll() {
  const list = $('evList');
  if (list) list.scrollTop = list.scrollHeight;
}

function evRender(ev) {
  if (!ev || typeof ev !== 'object') return;
  const t = ev.type;
  if (t === 'turn_started') {
    evTextEl = null;
    evItem('', 'turn_started · sid=' + (ev.sid || '') + ' · turn=' + (ev.turn_id || '') +
               (ev.model ? ' · model=' + ev.model : ''));
  } else if (t === 'message_delta') {
    if (!evTextEl) {
      evTextEl = evItem('ev-text', 'message_delta');
      const body = document.createElement('div');
      evTextEl.appendChild(body);
      evTextEl._body = body;
    }
    evTextEl._body.textContent += (ev.text || '');
  } else if (t === 'tool_call' || t === 'tool_result') {
    const d = evItem('', t + (ev.name ? ' · ' + ev.name : '') +
                        (ev.call_id ? ' · ' + ev.call_id : ''));
    const det = document.createElement('details');
    const sum = document.createElement('summary');
    sum.textContent = '详情';
    const pre = document.createElement('pre');
    const val = (t === 'tool_call') ? ev.input : ev.output;
    pre.textContent = (typeof val === 'string') ? val
      : JSON.stringify(val == null ? '' : val, null, 2);
    det.appendChild(sum);
    det.appendChild(pre);
    d.appendChild(det);
  } else if (t === 'usage') {
    const parts = [];
    Object.keys(ev).forEach(function (k) {
      if (['v', 'seq', 'ts', 'sid', 'turn_id', 'agent_id', 'type'].indexOf(k) < 0) {
        parts.push(k + '=' + ev[k]);
      }
    });
    evItem('', 'usage · ' + parts.join(' '));
  } else if (t === 'turn_completed') {
    evItem('', 'turn_completed · success=' + !!ev.success +
               (ev.reason ? ' · reason=' + ev.reason : '') +
               (ev.ms != null ? ' · ' + ev.ms + 'ms' : ''));
  } else if (t === 'error') {
    const d = evItem('ev-error', 'error · reason=' + (ev.reason || ''));
    if (d && ev.message) {
      const m = document.createElement('div');
      m.textContent = ev.message;
      d.appendChild(m);
    }
  } else {
    const d = evItem('', String(t || 'unknown'));
    if (d) {
      const pre = document.createElement('pre');
      pre.textContent = JSON.stringify(ev, null, 2);
      d.appendChild(pre);
    }
  }
  evScroll();
}

function _evAgent() {
  return (typeof chatPick === 'string') ? chatPick : '';
}

async function evSend() {
  if (evRunning) { toast('上一轮还在跑：先点「中断」或等它结束', 'err'); return; }
  const agent = _evAgent();
  if (!agent) { toast('先在左侧选择一个 Agent', 'err'); return; }
  const ta = $('evInput');
  const msg = ta ? (ta.value || '').trim() : '';
  if (!msg) return;
  const model = ($('chatModel') && $('chatModel').value) || '';
  const cwd = ($('chatCwd') && $('chatCwd').value) || '';
  evSid = 'ev-' + Date.now().toString(36) + Math.random().toString(36).slice(2, 6);
  evTextEl = null;
  const list = $('evList');
  if (list) list.textContent = '';
  const hint = $('evSidHint');
  if (hint) hint.textContent = evSid;
  if (ta) ta.value = '';
  evBusy(true);
  evCtrl = new AbortController();
  try {
    const r = await fetch('/api/agents/' + encodeURIComponent(agent) + '/chat/events', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ message: msg, session_id: evSid, model: model, cwd: cwd }),
      signal: evCtrl.signal
    });
    if (!r.ok) {
      let tail = '';
      try { tail = await r.text(); } catch (e) { /* 体读不到就只报状态码 */ }
      evRender({ type: 'error', reason: 'provider_error',
                 message: 'HTTP ' + r.status + (tail ? ' · ' + tail.slice(0, 200) : '') });
      return;
    }
    const rd = r.body.getReader();
    const dec = new TextDecoder();
    let buf = '';
    while (true) {
      const step = await rd.read();
      if (step.done) break;
      buf += dec.decode(step.value, { stream: true });
      let idx = buf.indexOf('\n\n');
      while (idx >= 0) {
        const frame = buf.slice(0, idx);
        buf = buf.slice(idx + 2);
        frame.split('\n').forEach(function (line) {
          if (line.indexOf('data:') !== 0) return;
          try { evRender(JSON.parse(line.slice(5).trim())); } catch (e) { /* 坏帧跳过 */ }
        });
        idx = buf.indexOf('\n\n');
      }
    }
  } catch (e) {
    if (!e || e.name !== 'AbortError') {
      evRender({ type: 'error', reason: 'provider_error',
                 message: String((e && e.message) || e) });
    }
  } finally {
    evBusy(false);
    evCtrl = null;
  }
}

async function evInterrupt() {
  const agent = _evAgent();
  if (!agent) return;
  try {
    const r = await fetch('/api/agents/' + encodeURIComponent(agent) + '/interrupt', {
      method: 'POST',
      credentials: 'same-origin',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ session_id: evSid })
    });
    let d = null;
    try { d = await r.json(); } catch (e) { d = null; }
    if (r.ok) { toast('已发送中断', 'ok'); return; }
    if (r.status === 404) { toast('没有活动的事件运行', 'err'); return; }
    if (r.status === 409) {
      const why = (d && d.error && d.error.reason) || 'unsupported_steer';
      toast('该 provider 不支持中断（' + why + '）', 'err');
      return;
    }
    toast('中断失败 HTTP ' + r.status, 'err');
  } catch (e) {
    toast('中断请求失败：' + ((e && e.message) || e), 'err');
  }
}
