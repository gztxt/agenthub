/* 13-term-touch.js —— 终端移动端触摸层（P1：惯性滚动 / 长按选区 / 双指缩放字号）
 *
 * 为什么有这层：xterm.js 在触摸设备上只做 1:1 跟手，**松手即停、没有惯性**，
 * 选区手柄在部分 WebView 上也不出现 ⇒ 手机上「连得上终端，但选不中文本、滚不动」。
 * 本层只补这三件事，不碰终端协议、不改后端。
 *
 * 设计约束（三条，改动前先读）：
 *  ① **桌面零开销**：`ttIsTouch()` 为假直接返回，桌面端不注册任何监听器。
 *  ② **绝不��� #termEl / .term-body 加 padding/inset**：FitAddon 按父层内容盒算行数，
 *     父层多 1px inset 就会多算一行、底部被 overflow:hidden 裁掉（模板 720 行铁律）。
 *     `touch-action` 不是 inset、不影响尺寸，可以加（见模板 767px 档）。
 *  ③ **字号改了必须重算尺寸**：只改 --term-fs 不同步 term.options.fontSize 会让前端
 *     cell 与 PTY 尺寸脱节 ⇒ 下一帧输入错位。故强制重发 resize。
 *
 * 规格参考：CloudCLI(claudecodeui, AGPL-3.0-or-later) 的 mobileTerminalSelection.ts。
 * 本文件为**独立重写**，不含任何上游代码/常量/表达式，AGPL 风险为零。
 */
'use strict';

/* ── 手感常量 ──────────────────────────────────────────────────────────────
   刻意用 `var` 而非 `let/const`：本分片拼接序在 12（末位），但 01/02 的顶层语句
   （initSidebar() / go()）会同步调到本文件的函数 ⇒ 若这里用 let/const，被调函数读到
   的是尚未初始化的绑定 ⇒ 真 TDZ ReferenceError（tests/test_tdz_order.py 判红）。
   var 提升 + 值在顶层立即赋值，两种顺序都安全。与 09-local-projects.js 的 LP 同款理由。 */
var TT_LONG_PRESS_MS = 600;      // 短于此读成"滑动"，长于此用户以为没反应
var TT_MOVE_TOLERANCE_PX = 10;   // 长按期间允许的抖动；超过即判定为滚动
var TT_FLING_DECEL = 0.94;       // 惯性每 16ms 的速度衰减（半衰期约 11 帧）
var TT_FLING_MIN_PXMS = 40;      // 低于此速度不再甩，否则会无限小步长抖动
var TT_FLING_MAX_MS = 1200;      // 甩动最长时长，防止一甩到底过头
var TT_ZOOM_THROTTLE_MS = 50;    // 捏合节流：高频 touchmove 不节流会发疯
var ttResidPx = 0;               // ttScrollByPx 的跨帧余量（不足一行的位移不许丢，见函数内注释）
var TT_FONT_MIN = 8, TT_FONT_MAX = 48;
var TT_FONT_LS_KEY = 'hub.term.fontsize';

/* ── 状态 ────────────────────────────────────────────────────────────────── */
var ttState = null;   // 非空 = 正在进行手势

/* ── 设备判定 ────────────────────────────────────────────────────────────── */
function ttIsTouch() {
  try {
    if (window.matchMedia && window.matchMedia('(pointer: coarse)').matches) return true;
  } catch (e) { /* 老浏览器无 matchMedia */ }
  return ('ontouchstart' in window) || (navigator.maxTouchPoints > 0);
}

/* ── 尺寸换算 ────────────────────────────────────────────────────────────── */
/* 行高（px）。优先问 xterm 已经算好的（最准），拿不到退回 fontSize×lineHeight。
   term._core 是内部 API，版本升级可能变——所以整段包 try，失败即走回退。 */
function ttCellPx() {
  try {
    const d = term && term._core && term._core._renderService && term._core._renderService.dimensions;
    const h = d && d.css && d.css.cell && d.css.cell.height;
    if (h > 0) return h;
  } catch (e) { /* 内部字段变了，用回退值 */ }
  const fs = (term && term.options && term.options.fontSize) || 14;
  const lh = (term && term.options && term.options.lineHeight) || 1;
  return fs * lh;
}

/* 屏幕上第 y 像素所在的**视口行号**（0 起）。用视口元素的 getBoundingClientRect，
   不需要碰 buffer 内部结构。 */
function ttViewportRow(clientY) {
  try {
    const el = term && term.element;
    const vp = (el && el.querySelector('.xterm-viewport')) || el;
    if (!vp) return 0;
    const top = vp.getBoundingClientRect().top;
    return Math.max(0, Math.floor((clientY - top) / ttCellPx()));
  } catch (e) { return 0; }
}

/* ── 滚动（**唯一**换算入口，符号只在这里定一次） ──────────────────────────
   ⚠️ 为什么用 scrollToLine(绝对行号) 而不是 scrollLines(相对行数)——这是本层
   踩得最深的一个坑，真机闸门 T2/T2b 抓出来的：

   xterm 6.0 的 scrollLines(amount) 内部是 `this._viewport.scrollLines(e)`，
   而后者落到 **DOM 滚动**：`setScrollPosition({scrollTop: cur + e*rowHeight})`。
   当视口已经在最底（或最顶）时，DOM 那条路被 clamp 住 ⇒ **静默不生效**
   （实测：viewportY=376 已是底部时 scrollLines(±n) 全部纹丝不动；滚到 200 中段
   才正常工作）。而"手指往上滑看新内容"恰恰总从底部起步 ⇒ 用户 100% 感觉失效。
   scrollToLine(n) 走的是 buffer 绝对行号，不经 DOM clamp，底部/顶部/中段一致有效
   （实测 376→300、300→0 均生效）。所以这里一律换算成绝对行号再调。

   符号：手指向下拖（deltaY > 0）＝ 要看更早的内容 ＝ viewportY 变小。
   所以目标行号 = 当前 viewportY - deltaY/行高。 */
/* ── 整屏 TUI 的滑动：转成滚轮上报喂回 pty（2026-10-30）─────────────────────
   为什么单独一条路：dsh 这类**差分整屏渲染**的 TUI，内容不在 scrollback 里
   （每帧绝对定位重画，buffer 只有一屏高 ⇒ baseY 恒 0）。上滑时
   `scrollToLine(0)` 是空操作，用户侧就是「手机上怎么滑都不动」；
   而同一个 TUI 的『向上翻看』能力其实存在——它靠滚轮 SGR 上报自滚
   （桌面端这条路一直是通的，见 02-nav-and-poll.js 的 termWheelNow 分层）。
   所以触摸层做同一件事：把「按行折算后的位移」合成为**真实 WheelEvent** 派发到
   xterm 的滚动容器上。不自己拼 SGR 滚轮序列（64/65 号键）的理由：行列坐标、
   1-based/0-based、utf8/utf16 编码全由 xterm 的 coreMouseService 负责，
   自己拼就是第二份会漂的真相。
   派发目标必须是 `.xterm-scrollable-element`（xterm 的 wheel 监听在它身上；
   派到 #termEl 只会冒泡向上，根本到不了它）。
   判据：只对 TERM_ALT_DIFF_AGENTS 且 app 实时声明了鼠标时走这条路；其余照旧走
   scrollback（codex/claude/线性输出仍按原逻辑滚，行为零变化）。 */
function ttWheelToApp(deltaYPx) {
  const cell = ttCellPx();
  const el = term && term.element &&
             (term.element.querySelector('.xterm-scrollable-element') || term.element);
  if (!cell || !el) return false;
  ttResidPx += deltaYPx;
  const rows = Math.trunc(ttResidPx / cell);
  if (!rows) return true;                 // 不足一行：留余量，下一帧补（与 scrollToLine 同款）
  ttResidPx -= rows * cell;
  const n = Math.min(6, Math.abs(rows));  // 一帧最多 6 行：一次大跳会让对端连发重绘、卡顿
  const dir = rows > 0 ? -cell : cell;    // 手指下滑(正) = 看更早 = 滚轮向上(负 deltaY)
  const r = el.getBoundingClientRect();
  for (let i = 0; i < n; i++) {
    try {
      el.dispatchEvent(new WheelEvent('wheel', {
        deltaY: dir, deltaMode: 0,
        clientX: r.left + r.width / 2, clientY: r.top + r.height / 2,
        bubbles: true, cancelable: true
      }));
    } catch (e) { return false; }         // 老浏览器没有 WheelEvent 构造器 ⇒ 退回 scrollToLine
  }
  return true;
}

function ttScrollByPx(deltaYPx) {
  if (!term || !deltaYPx) return;
  const cell = ttCellPx();
  if (!cell) return;
  if (termMouseLive && termAltAgent(termSidAgent) && ttWheelToApp(deltaYPx)) return;
  try {
    const buf = term.buffer && term.buffer.active;
    const top = (buf && typeof buf.viewportY === 'number' && buf.viewportY >= 0) ? buf.viewportY : 0;
    const maxTop = Math.max(0, (buf ? buf.baseY : 0));
    /* 跨帧余量（10-02 补）：**不累积就等于把惯性尾巴扔掉**。
       惯性每帧位移 = v·dt/1000，轻甩（v≈-1000px/s）时每帧 ≈16px 不足半行；
       若每帧独立 round(px/cell)，小数当场被抹平、且**没有下一次来补** ⇒ 尾巴整段消失。
       实测（真机闸门 T2b）：v=-1010px/s 只滑 2 行，而按 0.27·v 推算应有 ≈10 行。
       累积后位移有连续性；单次大位移（跟手拖动）的行为与原来**逐位一致**（余量从 0 起算）。*/
    ttResidPx += deltaYPx;
    const rows = Math.round(ttResidPx / cell);
    if (!rows) return;
    ttResidPx -= rows * cell;
    let target = top + rows;
    if (target < 0) { target = 0; ttResidPx = 0; }      // 撞顶：余量作废，否则攒出假位移
    if (target > maxTop) { target = maxTop; ttResidPx = 0; }
    if (target === top) return;
    term.scrollToLine(target);
  } catch (e) { /* 终端已 dispose */ }
}

/* ── 字号 ────────────────────────────────────────────────────────────────── */
function ttFontRead() {
  const v = parseFloat(lsGet(TT_FONT_LS_KEY, ''));
  return Number.isFinite(v) ? Math.min(TT_FONT_MAX, Math.max(TT_FONT_MIN, v)) : null;
}
function ttFontApply(px) {
  const size = Math.min(TT_FONT_MAX, Math.max(TT_FONT_MIN, Math.round(px)));
  try { document.documentElement.style.setProperty('--term-fs', size + 'px'); } catch (e) { /* 无效模式 */ }
  lsSet(TT_FONT_LS_KEY, String(size));
  try {
    if (typeof term !== 'undefined' && term) {
      term.options.fontSize = size;          // 约束③：前后端尺寸必须同步
      if (typeof termRepaint === 'function') termRepaint(true);
    }
  } catch (e) { /* term 还没建好，下轮构造时自然读到新值 */ }
  return size;
}

/* ── 入口 ────────────────────────────────────────────────────────────────── */
function termTouchBind() {
  const el = $('termEl');
  if (!el || !ttIsTouch()) return;
  if (el.dataset.touchBound === '1') return;   // #termEl 全生命周期同一个节点 ⇒ 只绑一次
  el.dataset.touchBound = '1';

  const saved = ttFontRead();                 // 上次捏合留下的字号，touch 设备启动即生效
  if (saved) { try { document.documentElement.style.setProperty('--term-fs', saved + 'px'); } catch (e) {} }

  let lpTimer = null;
  let pinch = null;

  function cancelLP() { if (lpTimer) { clearTimeout(lpTimer); lpTimer = null; } }

  el.addEventListener('touchstart', e => {
    if (!term || !termVisible()) return;
    cancelLP();
    if (e.touches.length !== 1) return;                     // 双指留给捏合
    const t = e.touches[0];
    ttResidPx = 0;                 // 新手势从零起算，别把上一次剩下的半行带进来
    ttState = { mode: 'press', x: t.clientX, y: t.clientY, moved: 0,
                lastY: t.clientY, lastT: Date.now(), v: 0 };
    lpTimer = setTimeout(() => {
      lpTimer = null;
      /* 长按成立的前提是**没怎么动**：动过就是在滚动，别把人硬拽进选区。 */
      if (!ttState || ttState.moved > TT_MOVE_TOLERANCE_PX) return;
      ttState.mode = 'longpress';
      /* 选中手指所在的那**一整行**，并**记住行号**。
         两个踩过的坑（都是真机闸门 T3 抓出来的）：
         ① 用 selectLines(start,end) 而不是 select(col,row,len)：后者第三参是
            「长度」不是终点，且行号是 buffer 绝对坐标 ⇒ select(0,row) 选中为空。
         ② **光选中不够**：xterm 自己在 document 上注册了 touchstart/touchend
            （非 passive，见 vendor/xterm.js 的 TouchGestureSource.onTouchStart），
            松手时它的 onTouchEnd 会 clearSelection ⇒ 选区在用户看到之前就被抹掉。
            实测：长按 1s 时 getSelection()='L15'，touchend 后 0.3s 变成 ''。
            对策：把行号存进 ttState.lpRow，等 touchend 之后再补一次 selectLines。 */
        const base = (term.buffer && term.buffer.active && term.buffer.active.viewportY) || 0;
        const row = base + ttViewportRow(ttState.y);
        ttState.lpRow = row;
        try { term.selectLines(row, row); } catch (e) { /* 老版本无 selectLines */ }
    }, TT_LONG_PRESS_MS);
  }, { passive: true });

  el.addEventListener('touchmove', e => {
    if (!ttState) return;

    if (e.touches.length === 2) {                            // ── 捏合：缩字号
      cancelLP();
      const a = e.touches[0], b = e.touches[1];
      const dist = Math.hypot(a.clientX - b.clientX, a.clientY - b.clientY);
      if (!pinch) {
        pinch = { dist: dist, font: parseFloat(cssToken('--term-fs', '14')) || 14, last: 0 };
        ttState.mode = 'pinch';
        /* 捏合期间禁掉浏览器页面级缩放：否则整个页面（含顶栏、侧栏）跟着一起变形。
           类打在 #termEl 上，与模板 767px 档的 `tterm-pinch` 规则配对。 */
        try { e.target.classList.add('tterm-pinch'); } catch (err) {}
        return;
      }
      const now = Date.now();
      if (now - pinch.last < TT_ZOOM_THROTTLE_MS) return;
      pinch.last = now;
      ttFontApply(pinch.font * (dist / Math.max(1, pinch.dist)));
      e.preventDefault();
      return;
    }
    if (e.touches.length !== 1 || !term) return;

    const t = e.touches[0];
    const dy = t.clientY - ttState.y;
    /* 采样瞬时速度（px/s）供松手后起惯性：只取最近一段，整段平均会把「先慢后快」
       稀释掉，甩不动。 */
    const now = Date.now();
    const dt = now - ttState.lastT;
    if (dt > 0) ttState.v = ((t.clientY - ttState.lastY) / dt) * 1000;
    ttState.lastY = t.clientY; ttState.lastT = now;

    ttState.moved += Math.abs(dy);
    if (ttState.moved > TT_MOVE_TOLERANCE_PX) {
      cancelLP();
      ttState.mode = 'scroll';
      ttScrollByPx(dy);       // 1:1 跟手
    }
    ttState.y = t.clientY;
  }, { passive: false });

  el.addEventListener('touchend', e => {
    cancelLP();
    pinch = null;
    try { el.classList.remove('tterm-pinch'); } catch (err) {}
    const s = ttState;
    ttState = null;

    /* 长按选区的**补刀**。
       实测时序（CDP 逐 tick 取证，别凭猜）：
         touchend 捕获/bubble 各阶段 → selection 仍是 'L15'
         +0ms → ''        ← xterm 的手势收尾在这之后清掉
         我的补刀 setTimeout(0) 确实执行了（selectLines 被调到 '14-14'）
         但**结果仍是空** —— 因为 xterm 的清理排在更晚一拍。
       所以补刀必须**晚于**它：0ms 会被追平，改 60ms（实测 '+60ms' 时 xterm 已收工）。
       为什么不是同步补：本监听器在 #termEl，xterm 的在 document 冒泡，同步补必被抹。 */
    if (s && s.mode === 'longpress' && typeof s.lpRow === 'number') {
      const row = s.lpRow;
      setTimeout(() => {
        try {
          if (term && termVisible()) term.selectLines(row, row);
        } catch (e) { /* 终端已换会话 */ }
      }, 60);
    }

    if (!s || s.mode !== 'scroll' || !term || !termVisible()) return;
    /* 松手接惯性：速度够快、且刚动过不久（久按停住后松手不该甩）。 */
    const idle = Date.now() - s.lastT;
    if (Math.abs(s.v) < TT_FLING_MIN_PXMS || idle > 150) return;
    let v = s.v, last = performance.now();
    const t0 = last;
    (function frame(now) {
      if (!term || !termVisible()) return;
      if (now - t0 > TT_FLING_MAX_MS || now - last < 8) { requestAnimationFrame(frame); return; }
      const dt = now - last; last = now;
      v *= Math.pow(TT_FLING_DECEL, dt / 16);
      if (Math.abs(v) < TT_FLING_MIN_PXMS) return;
      ttScrollByPx(v * dt / 1000);          // v(px/s)·dt(ms) ⇒ 位移 px
      requestAnimationFrame(frame);
    })(last);
  }, { passive: true });

  el.addEventListener('touchcancel', () => {
    cancelLP(); pinch = null; ttState = null;
    try { el.classList.remove('tterm-pinch'); } catch (err) {}
  }, { passive: true });
}

/* 换绑/离开时清手势态。
   只清状态、**不摘监听器**，且是刻意为之：#termEl 是整页生命周期里同一个 DOM 节点
   （换会话不换节点），监听器只需绑一次（dataset 闸门保证），不存在泄漏。
   反过来"解绑"才会踩坑：清了 dataset 但监听器还在 ⇒ 下次 ensureTerm 重新绑 ⇒ 两套手势。 */
function termTouchReset() {
  ttState = null;
}