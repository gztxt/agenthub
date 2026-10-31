function toggleGroup(g) {
  navOpen = (navOpen === g) ? '' : g;   // 单开：展开一个自动收起其他
  lsSet('hub.nav.open', navOpen);
  renderNav();
}
/* 点左侧实体 → 右侧加载该实体工作台（复用既有 gotoChat / showDetail，不另起炉灶）
   形态一律交给 defaultModeOf()：它自己会读「上次显式选过的形态」再回落默认。
   这里以前另写了一份 embed > term > chat 优先级 —— 那份就是 09-25 故障的本体：
   Claude Code 是唯一同时有 embed（cloudcli 宿主端口活 ⇒ discovery 注入）与 term 的 Agent，
   于是点行必进那张要独立登录的 iframe，而站内没有任何按钮能切回终端。 */
function openEntity(id) {
  const a = entityById(id);
  if (!a) return;
  const es = a.entries || [];
  const has = t => es.some(e => e.type === t);
  if (has('embed') || has('term') || has('chat')) return gotoChat(id);
  if (has('detail')) return showDetail(id);
  const o = es.find(e => e.type === 'open');
  if (o) return window.open(lanUrl(o.url), '_blank');
  showDetail(id);
}
/* 右侧操作区：实体工作台的面包屑与形态 tab 由 renderModeBar 写；系统页一律不写。
   v0.13.34 用户裁定（2026-09-26）：两个项目页顶部不要标题和分割线。
   v0.13.40 用户裁定（2026-09-27）：「系统菜单里的子菜单点进去，右边内容框顶部的
   标题和分割线都要删除」⇒ 把那条口径从两个项目页**推广到全部系统页**（含总览）。
   理由同源：侧栏项本身就是入口语义，页内再顶一条「遥测」+ opBar 底边线是重复装饰，
   还白占 37px。清空 crumb/opTabs ⇒ syncOpBar 判 void ⇒ 整条 opBar 收起。
   PAGE_LABELS 保留：它仍是页名→中文名的唯一映射（工具提示/后续复用），且
   tests/test_asset_panel.py 钉着 `assets: '资产'` 这一条。 */
function renderPageCrumb(page) {
  if (page === 'chat') return;   // 实体工作台由 renderModeBar 接管，别互相覆盖
  const crumb = $('crumb'), tabs = $('opTabs');
  if (!crumb) return;
  crumb.innerHTML = '';
  if (tabs) tabs.innerHTML = '';
  syncOpBar();
}
/* ── 多选源 → 芯片行（v0.13.40 视觉重排）───────────────────────────
   问题：memFedSrcs（11 项，size=8）/ kbRoutes（5 项）/ instTo（4 项）都是原生
   <select multiple>，摆在 .toolbar 里做多选。原生 listbox 的行高由 size 撑开，
   于是同一行的检索按钮被挤到中间、卡片下边框被顶穿（1440×1000 截图里
   memFedSrcs 的下沿越出卡片），而 11 项里同时只能看到 8 项。
   口径：**原生 select 仍是唯一数据源**——它的 option 清单、selectedOptions
   都不动（测试钉的源清单、JS 的读法全部零改动），只是被 CSS 隐藏，前面插一行
   可换行的芯片复刻勾选语义：点芯片 = 翻转该 option.selected = 派发 change。
   芯片行高度由内容与换行决定，不再需要 size 撑开，也不再顶穿卡片。
   幂等：重复调用会先摘掉旧芯片行（loadSkills 每次重写 instTo 的 option 后要重挂）。 */
function mountPicks(selId) {
  const sel = $(selId);
  if (!sel) return;
  const old = $(selId + 'Picks');
  if (old) old.remove();
  const wrap = document.createElement('div');
  wrap.className = 'picks';
  wrap.id = selId + 'Picks';
  wrap.setAttribute('role', 'group');
  wrap.setAttribute('aria-label', selId + ' 多选');
  Array.from(sel.options).forEach((o, i) => {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'pick' + (o.selected ? ' on' : '');
    b.dataset.i = String(i);
    b.textContent = o.textContent;
    b.setAttribute('aria-pressed', o.selected ? 'true' : 'false');
    b.addEventListener('click', () => {
      o.selected = !o.selected;            // 芯片只是壳：真值只写回原生 option
      b.classList.toggle('on', o.selected);
      b.setAttribute('aria-pressed', o.selected ? 'true' : 'false');
      sel.dispatchEvent(new Event('change', { bubbles: true }));
    });
    wrap.appendChild(b);
  });
  sel.classList.add('picks-src');
  sel.parentNode.insertBefore(wrap, sel);
}

/* v0.10.1：#opBar 无标题也无 tab 时整条收起，窄屏省 37px，宽屏也不留空带 */
function syncOpBar() {
  const ob = $('opBar'), c = $('crumb'), t = $('opTabs');
  if (!ob) return;
  const empty = !((c && c.textContent.trim()) || (t && t.textContent.trim()));
  ob.classList.toggle('void', empty);
}
/* 模式 tab 点击：embed/term/chat 走既有 switchMode，open/detail 各自直行 */
function navMode(m) {
  if (m === 'detail') { showDetail(chatPick); return; }
  if (m === 'open') {
    const a = entityById(chatPick);
    const e = ((a && a.entries) || []).find(x => x.type === 'open');
    if (e) window.open(lanUrl(e.url), '_blank');
    return;
  }
  switchMode(m);
}

/* ── 侧栏：手风琴导航委托绑定 + 折叠记忆 ───────────── */
/* ── v0.13.7 侧栏抽屉：偏好按视口档位分存 ─────────────────────────────
   事故（2026-09-23）：旧实现用一个**与宽度无关**的全局键 hub.sidebar 存折叠态，
   并在**加载时**就写盘。于是一次宽屏访问就把 "展开(0)" 存成全局偏好；手机再打开时
   stored==='0' ⇒ 不收起 ⇒ CSS @media(max-width:767px) 的 .sidebar:not(.collapsed)
   是 position:fixed / width:236px / z-index:46 的白色覆盖层 —— 390px 屏上盖掉 61%
   视口（实测 overlays: [{id:sidebar,bg:rgb(255,255,255),z:46,pct:61}]），用户看到
   就是「整页被白板糊住」。且 resize 只重画终端，抽屉态永不重算 ⇒ 刷新也不会好。

   三条不变量（本文件里全部可被 tests/test_sidebar_breakpoint.py 打到）：
     1 一档一键，宽屏的偏好永不污染窄屏；
     2 加载不写盘，只有真点过才算偏好（污染路径从根上断掉）；
     3 断点只有一个定义（matchMedia 767px，与 CSS 同值），跨断点必重算。 */
const mqNarrow = HUB_NARROW_MQ;   // 断点唯一真源在 01（不变量 3）
const sidebarPrefKey = () => mqNarrow.matches ? 'hub.sidebar.narrow' : 'hub.sidebar.wide';

/* 纯判定，单列成顶层函数是为了让探针能原样抽出**真代码**跑（手抄即假绿）。
   stored：本档已存偏好；legacy：旧的全局键 hub.sidebar。
   legacy 只在宽屏当一次性迁移用 —— 它几乎必然由宽屏写入；窄屏一律回到默认收起，
   这样存量已被污染的手机（值='0'）首屏即自愈，不需用户清缓存。 */
function sidebarWantCollapsed(narrow, stored, legacy) {
  if (stored !== null) return stored === '1';
  if (!narrow && legacy !== null) return legacy === '1';
  /* 窄档无存档时的默认值 = 收起（48px 图标条）——用户 2026-09-24 裁定。
     本次没改行为：实测（冷 profile + 清 localStorage，390x768）本来就是
     collapsed=true / offsetWidth=48，故只把这行**钉成断言**：
     tests/verify_narrow_default_iconbar.py（附两格灵敏度对照：本档存过 '0' → 必须展开；
     掐 hub.js → collapsed 必须 false，证明闸门能判红、不是 stuck-true 假绿）。
     谁要把这行改成 false，先去看那两格为什么红。 */
  return narrow;
}

function initSidebar() {
  const sb = document.getElementById('sidebar'), btn = document.getElementById('btnSideToggle');
  if (!sb || !btn) return;
  const narrow = () => mqNarrow.matches;
  const apply = (c, persist) => {
    sb.classList.toggle('collapsed', c);
    btn.innerHTML = c ? ico('panel-expand', 'xs') : ico('panel-collapse', 'xs') + '<span class="lbl">收起</span>';
    if (persist !== false) lsSet(sidebarPrefKey(), c ? '1' : '0');
    if (window.syncOverlayMask) syncOverlayMask();   // 遮罩不在这里算，统一走下面那个出口
  };
  /* ③ 遮罩的唯一计算出口（浮层唯一性）：窄屏 且（侧栏抽屉展开 或 任一抽屉浮层在开）
     才存遮罩。以前这里是个与 apply() 平行的写入点，开设置/导航都不过它 —— 所以
     设置抽屉盖住 92% 画面时遮罩还是没开，用户点哪儿都落在抽屉上。 */
  function syncOverlayMask() {
    const m = document.getElementById('sideMask');
    if (!m) return;
    m.classList.toggle('on', mqNarrow.matches &&
      (!sb.classList.contains('collapsed') || (window.overlayAnyOpen ? overlayAnyOpen() : false)));
  }
  // 用命名函数而不是 `window.x = () => {}`：后者 tests/_hub_extract 抽不到，闸门会变成假绿
  window.syncOverlayMask = syncOverlayMask;
  window.collapseSidebar = () => { if (mqNarrow.matches && !sb.classList.contains('collapsed')) apply(true); };
  window.isNarrow = () => mqNarrow.matches;   // 断点单一真源：外面只准问这个，不准再写 767
  // 首屏解析档位偏好：persist=false ⇒ 加载本身不再写盘（老代码正是在这一步把宽屏的"展开"存成全局值）
  const resolve = () => apply(sidebarWantCollapsed(narrow(), lsGet(sidebarPrefKey()),
                                                   lsGet('hub.sidebar')), false);
  resolve();
  // v0.13.76（2026-10-04 用户报「窄屏左侧菜单栏展开不正常」后的**回归修复**）：
  // `<head>` 里的 `narrow-rail` 标记是**第一帧专用**的（它让窄屏首屏就是图标条，
  // 而不是 236px 白板盖住 60~74% 视口）。但我当初**打完就没再摘**，它变成永久标记 ⇒
  // `html.narrow-rail .sidebar:not(.collapsed)` 的**特异性高于** `.sidebar:not(.collapsed)`，
  // 用户点“展开”时 JS 确实移除了 `collapsed`，几何却仍被我的规则按回 52px 图标条
  // ⇒ **抽屉永远打不开**，而遮罩照样亮（用户点哪都点不到 = 整页锁死）。
  // 实测：展开后 `class=sidebar` 但 `width=52px / position:relative`（应为 236px/fixed）。
  //
  // 修法：**JS 一接管就摘标记**，语义回到本意「JS 还没跑（或没跑起来）」。
  // 失败模式也是对的：bundle 挂了 → 标记留下 → 窄屏仍是可读图标条，
  // 而不是一块盖住大半屏的白板。
  document.documentElement.classList.remove('narrow-rail');
  btn.onclick = () => apply(!sb.classList.contains('collapsed'));
  // 跨断点（转屏/窗口拖窄/桌面缩放）重新解析本档偏好；老代码只重画终端，抽屉状态永远停在加载那一刻
  const onBreak = () => resolve();   // apply() 自己会带遮罩，不再加一个平行的掩码同步路径
  if (mqNarrow.addEventListener) mqNarrow.addEventListener('change', onBreak);
  else if (mqNarrow.addListener) mqNarrow.addListener(onBreak);   // Safari < 14
  // 事件委托：静态常驻项 + 手风琴动态项（含系统页）统一走这里
  sb.addEventListener('click', e => {
    let el = e.target.closest('button[data-page]');
    if (el) { go(el.dataset.page); if (narrow()) apply(true); return; }
    el = e.target.closest('button[data-sys]');
    if (el) { go(el.dataset.sys); if (narrow()) apply(true); return; }
    el = e.target.closest('.hh-row');                     // 历史条目：续聊，窄屏顺手收抽屉
    if (el) { termResume(el.dataset.agent, el.dataset.sid); if (narrow()) apply(true); return; }
    el = e.target.closest('button[data-entity]');
    if (el) {
      const aid = el.dataset.entity;
      if (TERM_HIST_AGENTS.includes(aid)) {
        if (histOpen === aid) histOpen = '';              // 再点当前行 = 只收起，不离开页面
        else { histOpen = aid; histLoad(aid); }           // 展开新的（自动收起上一个）
        lsSet('hub.hist', histOpen);
        renderNav();
      }
      openEntity(aid);                                    // 进工作台照旧（A：两件事一次点击）
      if (narrow() && !histOpen) apply(true);
      return;
    }
    el = e.target.closest('button[data-group]');
    if (el) {
      // 收起态下点组图标 = 先展开侧栏并定位到该组（否则手风琴体被 display:none，点了没反应）
      if (sb.classList.contains('collapsed')) {
        navOpen = el.dataset.group;
        lsSet('hub.nav.open', navOpen);
        apply(false);
        renderNav();
      } else toggleGroup(el.dataset.group);
    }
  });
  const mask = document.getElementById('sideMask');
  // 点遮罩 = 一次关干净（抽屉 + 侧栏）：手机上没 ESC 键，这是唯一逃生路径
  if (mask) mask.addEventListener('click', () => {
    if (window.closeDrawers) closeDrawers();
    apply(true);
    if (window.syncOverlayMask) syncOverlayMask();
  });
  const si = document.getElementById('navSearch');
  if (si) {
    let _t;
    si.addEventListener('input', () => {
      clearTimeout(_t);
      _t = setTimeout(renderNav, 250);  // 防抖 250ms
    });
  }
}

function setBadge(page, n) {
  const el = document.getElementById('badge-' + page);
  if (el) el.textContent = (n > 0 ? String(n) : '');
}
/* 徽章只取现有接口的现成数据，不新增后端 */
async function updateBadges() {
  /* 2026-10-05：页面隐藏时早退（本函数一次 3 个 fetch，60s 一轮）。 */
  if (document.hidden) return;
  /* 2026-10-05：agent 徽章改读全局 AGENTS，不再自己 fetch /api/agents。
   * 理由（实测）：loadAgents 每 30s 拉一次 /api/agents 灌进 AGENTS（01:520），
   * 而本函数每 60s 又拉同一 URL ⇒ 每 30s 两次、每 60s 三次重复请求。
   * 而 /api/agents 是全站最重的只读端点（25 个 agent，每次跑 docker ps + systemctl）。
   * 顺带消掉一个竞态：两条路径可能用**不同快照**渲染出不一致的徽章计数。
   * /mcp/servers 与 /api/jobs 保留 fetch —— 那是确属不同的数据，AGENTS 里没有。 */
  /* 2026-10-08 用户要求删掉「总览」后面的数字 ⇒ setBadge('classroom', …) 随之删除
     （挂载点 #badge-classroom 已从 templates/index.html 摘掉；只删一边会留下永远
     el=null 的死调用）。all 仍被下面的 chat 徽章用着，AGENTS 这条读取保留。 */
  try {
    const all = AGENTS || [];
    setBadge('chat', all.length);
  } catch (e) { /* 静默：徽章是增强，失败不影响主流程 */ }
  try {
    const d = await (await fetch('/mcp/servers')).json();
    setBadge('mcp', (d.servers || []).length);
  } catch (e) {}
  try {
    const d = await (await fetch('/api/jobs')).json();
    setBadge('jobs', (d.jobs || []).filter(j => j.enabled).length);
  } catch (e) {}
}
initSidebar();
updateBadges();
setInterval(updateBadges, 60000);
$('regMask').addEventListener('click', ev => { if (ev.target === $('regMask')) closeRegister(); });
// T8：chip 键盘可达（Enter/Space 等价点击）
const HOME_CHIPS = $('homeChips');   // 首页快捷问句 chip 键盘可达（原 infraGrid，随座位矩阵一并移除）
if (HOME_CHIPS) HOME_CHIPS.addEventListener('keydown', e => {
  if ((e.key === 'Enter' || e.key === ' ') && e.target.classList && e.target.classList.contains('chip')) {
    e.preventDefault(); e.target.click();
  }
});
// T8：命令面板输入/键盘（v0.13.185：↑↓ 选择 + Enter 走选中项）
$('cmdInput').addEventListener('input', e => renderCmdList(e.target.value.trim()));
$('cmdInput').addEventListener('keydown', e => {
  if (e.key === 'ArrowDown') { e.preventDefault(); cmdMove(1); return; }
  if (e.key === 'ArrowUp') { e.preventDefault(); cmdMove(-1); return; }
  if (e.key === 'Enter') {
    e.preventDefault();
    /* 空查询按 Enter = 关闭，不跳转（原口径保留：⌘K 之后反悔的路径）。
       有查询时走当前高亮项；高亮未建立则退回第一条。 */
    if (!e.target.value.trim()) { closeCmd(); return; }
    cmdSubmit();
  } else if (e.key === 'Escape') closeCmd();
});
/* 快捷键不许在"正在输入"的地方劫持按键（改前实测的两种破坏）：
   · 终端里敲 `/`（路径分隔符，一天几百次）→ preventDefault + 焦点跳搜索框，
     之后所有输入都进了搜索框，画面看起来像"打字没反应"；
   · bash 的 Ctrl+K（kill-line）→ 被拿去开命令面板；
   · vim 的 Esc → 归 pty 的用，不能顺手去关我的浮层。
   规则：输入位（含 xterm 自己的 textarea）里只放行"搜索框的 Esc 清空"这一条，
   其余一律 return；非输入位维持原有行为。 */
function keyTargetIsEditing(e) {
  const t = e && e.target;
  if (!t || !t.closest) return false;
  if (t.closest('.xterm')) return true;
  const tag = t.tagName;
  return tag === 'INPUT' || tag === 'TEXTAREA' || tag === 'SELECT' || t.isContentEditable === true;
}
document.addEventListener('keydown', e => {
  const editing = keyTargetIsEditing(e);
  if (e.key === 'Escape') {
    /* P1-21 优先级：抽屉 > 命令面板 > 搜索框 > 终端/输入位。
       改前两处真缺口：
       · 只调 closeDetail()，skillDocDrawer 没接 ⇒ 技能正文抽屉键盘关不掉；
       · editing 分支在最前面 return，于是**焦点在抽屉里的输入位时 Esc 整体失效**
         （详情抽屉的云CLI 项目搜索框就在里面），用户只能去点遮罩 —— 手机能点、
         键盘不能，等于"没有关闭"。
       终端里的 Esc 仍原样给 pty（bash/vim 不能被抢），命令面板自己的 keydown
       在 input 上先冒泡认领 closeCmd，这里不重复。 */
    if (e.target && e.target.closest && e.target.closest('.xterm')) return;   // 终端 Esc 归 pty
    if (window.overlayAnyOpen && overlayAnyOpen()) { closeDrawers(); return; }   // 单一出口：关干净所有抽屉
    if ($('cmdMask') && $('cmdMask').classList.contains('on')) { closeCmd(); return; }
    const si = $('navSearch');
    if (si && si.value) { si.value = ''; renderNav(); return; }
    if (editing) return;
    return;
  }
  if (editing) return;
  if ((e.metaKey || e.ctrlKey) && (e.key === 'k' || e.key === 'K')) {
    e.preventDefault();
    $('cmdMask').classList.contains('on') ? closeCmd() : openCmd();
  } else if (e.key === '/') {
    const si = $('navSearch');
    if (si) { e.preventDefault(); si.focus(); si.select(); }
  }
});
/* 终端页不许把按键丢在地上（2026-09-25 用户报障「Grok 会话窗口空格不能用，一按就出现重复的文字内容」）。
   实测：焦点一旦落到终端外（手机上点过标题/会话芯片、桌面上点过页面任意处），敲进去的字符
   既不进 pty 也没有任何提示 —— 影子实例里把 pty 设成 `-echo -icanon` 交给 cat 逐字对账，
   焦点=BODY 时敲 'c'+空格+'d'，pty 实收 0 份。用户的下一步必然是点一下终端再敲，而 Grok TUI
   在主屏缓冲区里反复重画整屏（首帧无 ?1049h 备用屏）⇒ 同一份文字在 xterm 里出现两遍，
   于是现象被报成"空格一按就重复"。
   规则：终端页可见 + 焦点不在任何输入位（含 xterm 自己的 helper textarea，那条路本来通）
   ⇒ 把键交给终端，并 preventDefault 挡掉浏览器把空格当翻页。

   ★ v0.13.99（用户 2026-10-09 报障「嵌入式终端输入框无法使用上下左右光标键，
     输入框只是不需要鼠标焦点，但键盘的全部原生功能都是需要的」）：
   **把「编辑键」从「单字符」放宽为一张显式映射表。**

   【根因取证，勿凭推断改动】先量了三轮（work/probe-arrow/probe_focus_chain.py，
   真 chromium + CDP，真鼠标/真键盘事件，pty 侧 `stty -echo -icanon` 交给 cat 逐字对账）：
     ① v0.13.83 那行 `textarea.style.pointerEvents='none'`（本仓为解决「输入框与内容
        抢鼠标焦点致滚轮不能向上翻」而钉的）**没有**打断键盘：实测点终端后焦点照样落在
        `.xterm-helper-textarea` 上，方向键 `^[[A/B/C/D` 全到 pty。
        ⇒ **滚轮与键盘是两条独立通路，本次一行滚轮代码都不动。**
        （pointer-events 只管命中测试，不管 JS focus()；xterm 的 `focus()` 是
          `textarea.focus({preventScroll:true})`，两者互不干涉。）
     ② 真正的缺口在**焦点离开终端之后**：实测焦点=BODY 时，方向键与
        Enter/Backspace/Tab/Home/End/PageUp/Delete **全部消失**（pty 收 0 份，
        浏览器也没拿它做别的）—— 因为本函数在 `e.key.length !== 1` 处 return，
        只接力可打印单字符。改前那句「非可打印键一律放行」在本场景下等于
        **「全部键都丢了」**，因为焦点不在终端时 xterm 根本收不到它们。
     ③ 用户那句「之前按→会自动填入」是 TUI 的 ghost-text 功能（走 SS3 `ESC O C`），
        与本条同形：都是「键必须到达 pty」而不是「键被谁拦下」。

   【为什么用显式映射表，而不是放开所有非单字符键】
     放行 = 把语义交还给浏览器，于是方向键变成**滚动页面**（实测焦点在 BODY 时
     doc/viewport scrollTop 均 0 —— 键彻底消失，连滚动都没有），
     F5/Ctrl+W 等还会被浏览器吃掉。终端输入位上这些键的语义**只有一个来源：pty**。
     故逐个映射到标准 VT 序列，交给 `term.input()`（其下游 onData 与真实按键
     同一条路，xterm 侧不做特殊处理）。
     ⚠ 只列**终端行编辑**真正认的键。功能键/媒体键/浏览器保留键一律仍放行 ——
        那是浏览器语义，不发明新行为（P2-11「快捷键不得劫持输入位」原样保留）。
   ⚠ **Escape 刻意不在表内**：它归 pty（bash/vim 极常用），且上面的 Esc 分支
     已明确把终端内的 Esc 让给 pty。这里若补上就与那条分支打架。
   ⚠ Ctrl/Cmd/Alt 组合键整体仍在函数开头 return：那是终端与浏览器各自的语义
     （Ctrl+C=SIGINT、Ctrl+K=kill-line），不在本次范围。 */
const TERM_EDIT_KEYS = {
  /* 方向键：CSI 形态。应用光标键模式（DECCKM）下真实按键会发 SS3（ESC O X），
     而 xterm 只在**自己收到焦点**时才会按模式切换；本函数是「焦点不在」时的
     兜底通路，此时解析态由 pty 侧序列决定，映射成 CSI 是绝大多数 TUI 的兼容形态
     （claude/codex 的 readline 系都两档都收）。 */
  ArrowUp: '\x1b[A', ArrowDown: '\x1b[B', ArrowRight: '\x1b[C', ArrowLeft: '\x1b[D',
  /* 行首/行尾：CSI H / CSI F。Home 也可能是 `ESC[1~`，但 CSI H 是 readline 通用形态。 */
  Home: '\x1b[H', End: '\x1b[F',
  /* 翻页：CSI 5~ / 6~。与方向键不同，PageUp/PageDown 没有 CSI 字母形态。 */
  PageUp: '\x1b[5~', PageDown: '\x1b[6~',
  /* 插入/删除：CSI 2~ / 3~。Delete 尤其重要 —— 焦点不在时它是编辑输入的常用键。 */
  Insert: '\x1b[2~', Delete: '\x1b[3~',
  /* 回车/制表/退格：三个最常用的行编辑键，缺任何一个都会让「打字能进、编辑不能」。 */
  Enter: '\r', Tab: '\t', Backspace: '\x7f',
};
document.addEventListener('keydown', e => {
  if (e.defaultPrevented || e.isComposing || e.ctrlKey || e.metaKey || e.altKey) return;
  if (keyTargetIsEditing(e)) return;
  const pg = $('page-chat'), tp = $('termPane');
  if (!pg || !tp || !pg.classList.contains('on') || !tp.classList.contains('on')) return;
  if (!term || !termWs || termWs.readyState !== 1) return;   // 没接上线就别假装送达（termSend 那条路会自己报警）
  /* 两条投递口径，按 e.key 形状分：单字符原样送；编辑键送映射后的 VT 序列。
     ⚠ 分派前先算 seq —— 下面 preventDefault 之前必须知道「有没有命中」，
     否则会拦掉一个我们并不投递的键（那才是真的劫持）。 */
  const seq = (typeof e.key === 'string' && e.key.length === 1) ? e.key : TERM_EDIT_KEYS[e.key];
  if (seq === undefined) return;          // 表外的键（功能键/媒体键/浏览器保留键）一律放行
  e.preventDefault();
  const opts = { user: true };   // 用户亲手敲的键＝主动意图，走同一条焦点策略（P2-10：只跟"用户主动"）
  if (termFocusWanted(opts)) term.focus();
  term.input(seq);
});

/* 2026-10-08（用户裁定）：底部状态栏删除 ⇒ tick() 与它的 setInterval(tick,1000)
   **整体删除**。tick 的唯一消费者是 #ftTime（页脚时钟），挂载点已摘；
   只删挂载点留写端＝每1000ms 往null 上写一次（P1-18 注释点名的死调用形态）。
   顶部 hTime 早于本批就已移除，故此处一并了结，页面上不再有任何走秒显示。 */
setInterval(loadAgents, 30000);  // T4：8s→30s（左栏手风琴与首页摘要随 loadAgents 一起刷新，无需高频）
pollHealth(); setInterval(pollHealth, 15000);  // T5：健康灯独立于 agent 列表轮询
/* 2026-10-05：回到可见时立刻补一次。loadAgents / pollHealth / updateBadges 都加了
   `if (document.hidden) return` 的隐藏守卫（省后台流量），但守卫只省「隐藏时」的，
   若没有唤醒路径，笔记本合盖再打开就会先看到一份过期快照直到下个周期。
   这里一次补齐三路；`loadActivity` 的唤醒在 12-activity.js 里（各自就近）。 */
document.addEventListener('visibilitychange', () => {
  if (document.hidden) return;
  loadAgents(); updateBadges(); pollHealth();
});
setInterval(() => {
  if (document.getElementById('page-tasks').classList.contains('on')) { loadRuns(); if (currentRun) openRun(currentRun); }
  if (document.getElementById('page-jobs').classList.contains('on')) loadJobs();
  // 终端面板可见时轮询会话记录：进程退出/超时/TTL 回收都会让死条目自动消失，无需手动点「会话」
  // 只看 termPane 的 on 不够：整块 page-chat 被 display:none 藏起来时它仍是 on，白轮询
  if (document.getElementById('page-chat').classList.contains('on')
      && document.getElementById('termPane').classList.contains('on')) termRefreshList();
}, 6000);
loadAgents();
/* v0.13.40：两个多选源的 option 是模板里写死的（memFedSrcs / kbRoutes），boot 期
   一次性挂芯片行即可；instTo 的 option 由 loadSkills 每次重写 ⇒ 在那边重挂。
   hub.js 在 </body> 前加载，此刻 DOM 已就绪。 */
['memFedSrcs', 'kbRoutes'].forEach(mountPicks);
/* v0.13.41：设置抽屉的委托在这里挂（函数定义在 01 分片，调用点必须晚于 04 分片的
   `let term` —— 见 01 里 settingsDelegates 上方的注释：TDZ 闸门判的是调用点顺序）。 */
settingsDelegates();
go(lsGet('hub.page') || 'classroom');  // T9：默认落点 = 上次所在页（chatPick/chatMode 已在声明处恢复）
