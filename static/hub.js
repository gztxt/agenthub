/* Agent Hub v0.3.0 教室视图 — 复刻 Agent_Manager(Dashboard/Manager/Memory/Ports) 交互语义
   v0.10 chat: 动态模型选择器 + 工作目录 + 会话管理（服务 claude/jcode）*/
/* ── 断点唯一真源（不变量 3）─────────────────────────────────────
   全站只允许这一处 `matchMedia('(max-width: 767px)')`。放在 01 是因为 05 的顶层
   语句也要读它，而 `window.isNarrow` 要等 initSidebar 跑起来才被赋值。 */
const HUB_NARROW_MQ = window.matchMedia('(max-width: 767px)');
const hubNarrow = () => HUB_NARROW_MQ.matches;

/* ── 行内 markdown（仅白名单两件套，2026-10-04）─────────────────────────────
 * 技能描述取自 `SKILL.md` 的 frontmatter，那些文件里本来就写着 markdown
 * （如 `**仅当任务落在 wigolo / obscura 覆盖不到的平台时使用**`），
 * 而本仓一律按纯文本渲染 ⇒ 星号原样显示，看着像乱码。
 *
 * 【安全口径：只渲染「行内」且只认两个标记】
 * 支持：`**粗体**`、`` `行内代码` ``。
 * **不支持**：链接 `[x](url)`、图片、标题、列表、表格、原始 HTML。
 * 理由不是「不想要」，是**不能要**：
 *   · 描述来自 **20+ 个发现点**，其中包含第三方仓（mattpocock-skills / hallmark /
 *     Agent-Reach / crawl4ai）—— 它们是**外部内容**，不是本仓自己写的文案；
 *   · hub 的 origin 里有**终端**（能起 pty）。一个能写进 SKILL.md frontmatter 的
 *     恶意描述，一旦渲染成 `<img onerror=…>` 或 `<a href="javascript:…">`，
 *     就是**存储型 XSS** 且能直接摸到终端。所以「渲染 markdown」这件事本身
 *     必须按**处理不可信输入**来做，不是按「显示 nicer 一点」来做。
 *
 * 【为什么先转义再替换，顺序不可颠倒】
 * 先 `escapeHtml` ⇒ 串里不再有裸 `< > & "`，此后再插入的 `<strong>` / `<code>`
 * 是**唯一**由我们放进去的标签。若反序（先按 markdown 切、再转义），
 * 切出来的「标签」会被自己的转义吃掉，且永远想不起该放行哪些。
 *
 * 【为什么不做斜体】`_italic_`（或 `*i*`）会把 `snake_case_name`、`a * b` 这类
 * **标识符与通配符**吃成斜体 —— 技能描述里路径和变量名很常见，是实打实的误伤。
 * 真要用斜体得先定词边界规则，那是另一个决定。本轮只做两个误伤面为零的标记。
 */
const MD_CODE_PH = '\u0001';   // 行内代码的占位符；输入里的同字符会被先剥掉
function mdInline(s) {
  // 占位符冲突防护：输入里若本来就含 \u0001，先剥掉，否则下面的还原正则会错位。
  let t = escapeHtml(String(s == null ? '' : s)).replace(/\u0001/g, '');
  const codes = [];
  // 行内代码**先**摘出来占位：否则 `**` 落在代码里也会被当成粗体切开。
  t = t.replace(/`([^`\n]+)`/g, function (_m, c) {
    codes.push(c); return MD_CODE_PH + (codes.length - 1) + MD_CODE_PH;
  });
  t = t.replace(/\*\*([^*\n]+)\*\*/g, '<strong>$1</strong>');
  t = t.replace(new RegExp(MD_CODE_PH + '(\\d+)' + MD_CODE_PH, 'g'),
                function (_m, i) { return '<code>' + codes[+i] + '</code>'; });
  return t;
}

/* ── 模糊搜索（全站共用，2026-10-03）────────────────────────────────────
 * 放在 01 而不是 04-terminal-ws：本函数被 04/05/09/10 **五个分片**用
 *（技能中心、agents 命令面板、侧栏搜索、端口表、本地项目、GitHub 项目），
 * 挂在 terminal/WS 那一片会让下一个找它的人以为「只有终端用」。
 * 同 01 已有的两处跨片工具（断点唯一真源、localStorage 守卫）一个理由。

 * 【为什么加】搜索框原本一律是纯 `includes()` 子串匹配，于是**一个字符的手误即零命中**：
 * 真名 `crawl4ai`（crawl4ai/skill/crawl4ai/SKILL.md 的 frontmatter）搜 `crawl1ai`
 * （数字 1）得 0 条——而这条技能明明在盘上、已被扫进清单。
 *
 * 【为什么不引库】与后端 `skill_relevance.py` 同一判断：几百条语料、纯前端，
 * 引 rank_bm25/jieba 是拿维护成本换零收益。要的只是「容忍手误 + 可排序」。

 * 【口径：先精确后模糊，且不静默】
 *   · 精确子串命中 → 恒 1000 分，**压倒一切**近似 ⇒ 精确结果不会被近似挤下去；
 *   · 否则算编辑距离，容忍度随长度给（≤4 字不容忍，≥5 字容忍 1，≥8 字容忍 2）。
 *     短词不容忍：`cc` 容忍 1 会把 `crawler` 之类召回一片。
 *   · 字段权重由调用方给（name 高于 desc / path），分高者胜。
 * 返回 0 = 不匹配。调用方拿 <1000 的分当「近似命中」并据此打 `≈近似` 徽标——
 * 面板必须能回答「为什么这条出现在这里」，只给一个排序就答不上
 * （同 `skill_relevance.py` 的 `matched` 可解释性纪律）。
 */

/* Levenshtein 编辑距离，带上限早退（超限即返回 limit+1，不做完整 DP）。
 * 纯 JS、O(len*m)；本规模下开销可忽略。 */
function _levenshtein(a, b, limit) {
  if (a === b) return 0;
  if (Math.abs(a.length - b.length) > limit) return limit + 1;
  const prev = new Array(b.length + 1);
  const cur = new Array(b.length + 1);
  for (let j = 0; j <= b.length; j++) prev[j] = j;
  for (let i = 1; i <= a.length; i++) {
    cur[0] = i;
    let best = cur[0];
    for (let j = 1; j <= b.length; j++) {
      const cost = a[i - 1] === b[j - 1] ? 0 : 1;
      cur[j] = Math.min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost);
      if (cur[j] < best) best = cur[j];
    }
    if (best > limit) return limit + 1;          // 行内已超限 ⇒ 整体必然超限
    for (let j = 0; j <= b.length; j++) prev[j] = cur[j];
  }
  return prev[b.length];
}

/* 查询长度 → 容忍的编辑距离。短词不容忍。 */
function _fuzzyTolerance(q) {
  if (q.length >= 8) return 2;
  if (q.length >= 5) return 1;
  return 0;
}

/* 对**一个字段**打分：0 = 不匹配，>0 = 匹配（越大越靠前）。 */
function _fuzzyFieldScore(fieldText, q, tol) {
  if (!fieldText) return 0;
  let best = 0;
  // 按分隔符切段：连字符/点分名与长描述整串各参与一次，避免大段文本拖慢 DP。
  const parts = fieldText.split(/[\s,，、:：()（）\-_/.]+/).filter(Boolean);
  for (const p of parts) {
    if (Math.abs(p.length - q.length) > tol) continue;
    const d = _levenshtein(q, p, tol);
    if (d <= tol) best = Math.max(best, (tol + 1 - d) * 10);
  }
  const d2 = _levenshtein(q, fieldText, tol);
  if (d2 <= tol) best = Math.max(best, (tol + 1 - d2) * 10);
  return best;
}

/* 全站搜索统一入口。`pairs` = [[文本, 权重], …]（权重大的字段说了算）。
 * 空查询 ⇒ 返回 1（全匹配），保持旧行为：空搜索本就该显示全部。 */
function fuzzyMatch(pairs, qRaw) {
  const q = String(qRaw || '').trim().toLowerCase();
  if (!q) return 1;
  const tol = _fuzzyTolerance(q);
  let best = 0;
  for (const [text, weight] of pairs) {
    const t = String(text || '').toLowerCase();
    if (!t) continue;
    if (t.includes(q)) return 1000;             // 精确子串压倒一切近似
    if (!tol) continue;
    const s = _fuzzyFieldScore(t, q, tol);
    if (s > 0) best = Math.max(best, s * (weight || 1));
  }
  return best;
}

'use strict';

/* ── localStorage 守卫（v0.13.13，2026-09-24）──────────────────────────────────
   为什么要这一层：全站原有 45 处**裸**读写 localStorage，任何一处抛异常都会打断启动链。
   本项已有过两次“顶层语句抛异常 ⇒ 整段 hub.js 当场死亡 ⇒ 端侧看起来随机坏”的事故
   （09-23 TDZ、09-23 响应体被截断）。会抛的三种真实场景：
     ① 隐私模式 / WebView 禁 DOM Storage —— 连取 window.localStorage 本身都抛；
     ② 配额满（QuotaExceededError / NS_ERROR_DOM_QUOTA_REACHED）—— setItem 抛；
     ③ 跨源 iframe 被策略拦下 —— 读写都抛。
   口径：**只加兜底，不改语义**。取不到给 fallback（默认 null，与浏览器原生「键不存在」
   返回值一致）；写失败返回 false，**不假装写进去了**；异常一律计数 + 留最后一条摘要，
   经 window.__lsDiag 上 ?diag=1 面板 ⇒ 端侧能自证“是不是存储被禁了”。
   位置必须在 01 最前面：02/05/06 都有顶层语句直接读存储（let chatPick = …  /
   const navOpenStored = …  /  go(lsGet('hub.page'))），顺序由 tests/test_ls_guard.py 钉住。
   两个计数助手自带初始化：这层存在的理由就是「不许抛」，它自己更不能抛。 */
window.__lsDiag = { fails: 0, ok: 0, lastErr: '' };
function lsDiagStore() {
  return window.__lsDiag || (window.__lsDiag = { fails: 0, ok: 0, lastErr: '' });
}
function lsDiagHit() { lsDiagStore().ok++; }
function lsDiagFail(op, key, e) {
  const d = lsDiagStore();
  d.fails++;
  d.lastErr = op + ' ' + key + ': ' + String((e && (e.name || e.message)) || e).slice(0, 90);
}
function lsGet(key, fallback) {
  try {
    const v = window.localStorage.getItem(key);
    lsDiagHit();
    return v === null ? (fallback === undefined ? null : fallback) : v;
  } catch (e) {
    lsDiagFail('get', key, e);
    return fallback === undefined ? null : fallback;
  }
}
function lsSet(key, value) {
  try {
    window.localStorage.setItem(key, value);
    lsDiagHit();
    return true;
  } catch (e) {
    lsDiagFail('set', key, e);
    return false;      // ★调用方可以选择察觉；写失败不静默假装成功
  }
}
function lsRemove(key) {
  try {
    window.localStorage.removeItem(key);
    lsDiagHit();
    return true;
  } catch (e) {
    lsDiagFail('rm', key, e);
    return false;
  }
}

/* ── v0.7.3 统一图标：全站图形唯一出口 ──────────────────────────────
   sprite 定义在 index.html（24 网格 / stroke=currentColor / 粗细由 CSS 统一）。
   尺寸只允许 xs|sm(默认)|md|lg|xl 五档，任何地方都不要再给图标写 font-size。
   例：ico('server') / ico('plus','xs') / ico('chevron-down',null,'caret') */
function ico(name, size, cls) {
  return '<svg class="i' + (size ? ' ' + size : '') + (cls ? ' ' + cls : '') +
         '" aria-hidden="true"><use href="#i-' + name + '"/></svg>';
}


const $ = id => document.getElementById(id);

/* ── v0.8.0 CSS token 读取：JS 侧不再写死任何颜色 / 字号 ─────────────
   改配色只需动 index.html 的 :root，JS 自动跟随。
   注意：只能读「具体值」的 token。getPropertyValue 返回的是未解析的原始声明，
   读 var() 包装的别名会拿到字符串 "var(--ok)" 而不是颜色。 */
function cssToken(name, fallback) {
  const v = getComputedStyle(document.documentElement).getPropertyValue(name).trim();
  return v || fallback;
}
function cssNum(name, fallback) {
  const v = parseFloat(cssToken(name, ''));
  return Number.isFinite(v) ? v : fallback;
}

let AGENTS = [], PORTS = [], portsLoaded = false, skillsLoaded = false, kbLoaded = false;
/* v0.13.30 本机项目页懒加载标志：声明在 01（拼接序最先）而赋值/读取在 09 分片。
   ⚠ 声明只允许存在于 01 这一处。历史错误（v0.13.32~v0.13.103）：09/10 分片顶层又各
   写了一份 `var lpLoaded = false`，并在注释里断言「同名 var 是刻意冗余、赋值不
   会覆盖」——**该断言是错的**：`var` 的初始化语句在 09/10 顶层执行，晚于 06 顶层
   的 go(lsGet('hub.page'))，会把 go() 刚置上的 true **重置回 false** ⇒ ① 每次
   导航回本机项目都重跑一次多根 git 扫描；② 掩盖了「落地页=本机项目时 agent
   下拉恒空」的竞速故障（再点一次菜单意外自愈）。声明重复由
   tests/test_localprojects.py::test_loaded_flags_declared_once 钉死。 */
var lpLoaded = false;
/* v0.13.31 GitHub 页同型：声明同样只在 01 一处（理由见上）。 */
var ghLoaded = false;
/* v0.13.43 设置三页懒加载标志：go() 在 06 分片顶层就会跑（可能早于 01 里
   settingsModelsLoad 之外的一些初始化），故同样用 var 提升规避 TDZ —— 与
   lpLoaded / ghLoaded 同一条纪律；真值在 go() 里首次进页时才置。 */
var setModelPageLoaded = false;
var setGithubPageLoaded = false;
var setLogsPageLoaded = false;   // v0.13.46：日志页（进页才拉，日志量级大不能每次导航都拉）
var setMemPageLoaded = false;    // v0.13.126：设置·记忆存储（L1/L2/L3 已移出记忆中心）
var resLoaded = false;           // v0.13.52：资源监控页懒加载标志（同 lpLoaded/ghLoaded 纪律）
/* v0.13.32 TDZ 补丁（真事故驱动的修复）：06 顶层 go(lsGet('hub.page')) 在
   09/10 分片顶层初始化**之前**就能调到 loadLocalProjects()/loadGithubRepos()
   （函数声明提升），而 LP/GH/lpStars… 的 `var X = …` 初始化还没跑 ⇒ 函数里
   读 LP.length 直接 TypeError ⇒ promise reject ⇒ 列表永远停在「加载中…」。
   修法与 lpLoaded 同型：状态声明+初始化前置到 01（拼接序最先）。注意这里的前置
   是**必须**的（09/10 顶层赋值晚于 06 的 go()），且不允许在 09/10 再留第二份声明
   —— 第二份声明不会「无害冗余」，它会把 go() 置上的懒加载标志重置回 false。 */
var LP = [], GH = [];
var lpStars = new Set(), lpHiddenSet = new Set();
var ghStars = new Set(), ghHiddenSet = new Set();

/* ── 基础 ─────────────────────────────────────────── */

/* v0.13.6 P1-7：写端点服务端强制凭据。**只有写操作才索取 token** ——
   GET 也 prompt 的话，用户一打开页面就被口令框糊脸（读路径本来不需要）。 */
const WRITE_METHODS = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };
function isWriteMethod(m) { return !!WRITE_METHODS[String(m || 'GET').toUpperCase()]; }
async function api(path, opt) {
  const o = opt || {};
  /* v0.13.44：noToken 的写端点（设置页几个只认 HUB_PASSCODE 的端点，已登记在
     writeauth.EXEMPT_PREFIXES）不再向 termToken() 索 TERM_TOKEN —— 那会先弹一个
     "请输入终端鉴权 TERM_TOKEN" 的窗，跟保存模型毫无关系，用户只会以为保存卡住了。 */
  if (isWriteMethod(o.method) && !o.noToken) {
    const tk = termToken();
    if (tk) o.headers = Object.assign({}, o.headers, { 'x-hub-token': tk });
  }
  let r = await fetch(path, o);
  if (r.status === 401 && isWriteMethod(o.method)) {
    // 存量口令失效（比如刚在设置里换过 token）：清掉再问一次，只重试一次，不循环
    lsRemove('hub.term.token');
    const again = termToken();
    if (again) {
      o.headers = Object.assign({}, o.headers, { 'x-hub-token': again });
      r = await fetch(path, o);
    }
  }
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
  if (!r.ok) {
    const msg = (data && data.detail && (data.detail.error || (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(';') : data.detail))) || ('HTTP ' + r.status);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    /* v0.13.27 additive：把 HTTP 状态码与响应体挂到错误对象上（既有调用方只读
       .message 不受影响）。日志页靠 http===401/503 区分「鉴权态」与「故障态」；
       技能正文 409 靠 detail.candidates 渲染路候选——不再靠正则猜文案。 */
    err.http = r.status;
    err.payload = data;
    throw err;
  }
  return data;
}

function toast(msg, cls) {
  const el = document.createElement('div');
  el.className = cls || '';
  el.textContent = msg;
  $('toast').appendChild(el);
  setTimeout(() => el.remove(), 4200);
  return el;   // v0.13.6：返回节点，供「链路恢复后收掉同一条提示」用（老调用方忽略返回值，行为不变）
}

function escapeHtml(s) {
  return String(s == null ? '' : s).replace(/[&<>"']/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' }[c]));
}

/** P2-D 密钥纵深：把字符串安全地塞进 **JS 字面量**（inline onclick 用）。
 *
 *  为什么需要它：`escapeHtml` 只处理 HTML 上下文。HTML 属性里的 JS 代码
 * （`onclick="f('…')"`）先按 HTML 解码、再按 JS 解析——两道语境，
 * 所以 HTML 实体转义**不足以**让任意 id 安全进 JS 字面量：形如
 * `x&#39;);evil(//` 的 id 经 HTML 解码后就是 `' );evil(//`，能提前闭合参数列表。
 * 当前所有 id 来源（画像白名单 / 系统标识）都不含引号 ⇒ 实测不可利用；
 * 这是"上游现在干净"的性质，不是前端的保证，故在 helper 层补齐。
 *
 *  与 AGENTS.md「侧栏/菜单内的按钮禁止用 inline onclick」不冲突：本函数
 *  服务的是**主内容区**（详情抽屉正文、卡片内按钮），那里 inline 合法
 *  （06 分片 Esc 优先级、01 分片注释原文口径）；侧栏仍一律走 data-* 委托。 */
function jsStr(s) {
  /* JSON.stringify 出的是合法 JS 字面量（含双引号与反斜杠转义）；再把
     < / 与 U+2028/29 转掉：前两个防 </script> 提前收尾与 HTML 解析歧义，
     后两个在 ES2019 前是非法行分隔符。 */
  return JSON.stringify(String(s == null ? '' : s))
    .replace(/</g, '\\u003c').replace(/>/g, '\\u003e')
    .replace(/\u2028/g, '\\u2028').replace(/\u2029/g, '\\u2029');
}

/* ── v0.13.27 三态加载助手（B 统一加载）──────────────────────────────
 * 三中心的 loader 统一走 busy→数据/失败 三态（此前失败只 toast，列表区停旧内容
 * ——用户分不清「没数据」与「还没加载」与「加载挂了」）。遥测页「加载中…」
 * 占位先例（index.html:988-991）从页内文案升格为全站函数。
 * 主内容区 inline onclick 合法（侧栏禁令不涉这里——浮层三条配套红线原文口径）。 */
function boxBusy(id, msg) {
  const el = $(id);
  if (el) el.innerHTML = '<div class="hint">' + escapeHtml(msg || '加载中…') + '</div>';
  return !!el;
}
function boxFail(id, err, retryFn) {
  const el = $(id);
  if (!el) return;
  const retry = retryFn ? ' <button class="btn sm" onclick="' + retryFn + '()">重试</button>' : '';
  el.innerHTML = '<div class="hint" style="color:var(--st-error,var(--danger))">加载失败：' +
    escapeHtml(String(err && err.message || err || '')) + retry + '</div>';
}

/* 三中心健康（供侧栏系统项挂 s-badge 点；loader 完成时写入）。
 * var 声明防 TDZ：renderNav（05 分片，拼接序在前）会读它。 */
var CENTER_HEALTH = { memory: '', skills: '', kb: '' };

/* ── 浮层唯一性（2026-09-23 事故：窄屏「设置」抽屉盖掉 92% 画面且没人关）────────
   三个浮层（侧栏抽屉 / detailDrawer / settingsDrawer）此前各开各的
   （v0.13.43 起设置抽屉已拆成左侧手风琴 + 正文页，剩 detailDrawer / skillDocDrawer 两个）：
   开设置不关侧栏、导航不收抽屉、遮罩只管侧栏 —— 窄屏抽屉宽 min(400px,92vw)，
   一旦残留就把整页压成"白板 + 点不动"。规则钉死三条：
   ① 同一时刻最多一个抽屉是 on（开新的必先清旧的）；
   ② 导航 = 清抽屉（go 里做，不留给调用方自觉）；
   ③ 遮罩只有一个计算出口（06 的 syncOverlayMask），且点它一定关干净 ——
      手机上没有 ESC 键，点空白是唯一逃生路径。 */
const OVERLAY_IDS = ['detailDrawer', 'skillDocDrawer'];   // v0.13.43：设置抽屉已拆，改走正文页
const overlayOpen = id => { const el = $(id); return !!(el && el.classList.contains('on')); };
window.overlayAnyOpen = () => OVERLAY_IDS.some(overlayOpen);

/* ── P1-21（2026-09-30）：抽屉的键盘/无障碍契约 ──────────────────────────────
   改动前的实测缺口（不是"锦上添花"，是三条会真出问题的路）：
   ① Esc 关不干净：全局 Esc 出口（06）只调 closeDetail()，skillDocDrawer 完全没接；
   ② 焦点在抽屉内的输入位时 Esc 整体失效 —— 06 的 keyTargetIsEditing 提前 return，
      用户在详情里选完文本按 Esc 没反应，而抽屉里恰好有 input/select（云CLI 项目搜索）；
   ③ 抽屉打开后焦点仍留在页面上被遮住的元素，键盘用户在抽屉外裸奔（读屏会串页）。
   手机没有 ESC 这条已有（遮罩点击 = 唯一逃生路径，09-23 军规），这里不重复造。
   口径：inert 一次性解决"抽屉外不可聚焦"（比逐个 tabindex=-1 更省事且不漏网），
        焦点进抽屉首个可聚焦元素、关闭后归还给开启者。 */
const overlayFocusables = 'a[href],button:not([disabled]),input:not([disabled]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])';
function overlayFocusablesIn(el) {
  if (!el) return [];
  return Array.from(el.querySelectorAll(overlayFocusables)).filter(
    n => n.offsetWidth > 0 || n.offsetHeight > 0 || n === document.activeElement);
}
let overlayOpener = null;   // 开启抽屉前的焦点，关抽屉时原样还回去
function syncOverlayA11y() {
  const openId = OVERLAY_IDS.find(overlayOpen);
  OVERLAY_IDS.forEach(id => {
    const el = $(id);
    if (!el) return;
    el.setAttribute('aria-modal', openId === id ? 'true' : 'false');
    el.setAttribute('aria-hidden', openId && openId !== id ? 'true' : 'false');
  });
  /* 抽屉开着时把页面其余部分整体 inert：Tab 不再能跑到遮住的元素上，
     读屏也不会串到抽屉外面去。这是 03 里"焦点陷阱"的实现方式。 */
  const page = document.getElementById('mainWrap') || document.body;
  /* 双向写：不是"开着才置 inert"，而是每次都按当前状态**显式赋值**。
     只在开时置 true 会留下永久残留 —— 抽屉全关后 openId 为 undefined，
     旧代码走不进任何分支，detailDrawer.inert 永远卡在 true；此后往那个抽屉里
     塞的任何元素都收不到焦点（真渲染探针 O8a 实测：焦点掉到 BODY），
     而且该抽屉再打开时**整个抽屉点不动**。 */
  OVERLAY_IDS.forEach(id => {
    const el = $(id);
    if (el && el !== page) el.inert = !!(openId && openId !== id);
  });
  if (page && page !== document.body) {
    page.inert = !!openId;
  }
}
function rememberOverlayOpener() {
  const a = document.activeElement;
  if (a && a.nodeType === 1 && !OVERLAY_IDS.includes(a.id)) overlayOpener = a;
}
function closeDrawers() {
  let changed = false;
  OVERLAY_IDS.forEach(id => {
    const el = $(id);
    if (el && el.classList.contains('on')) { el.classList.remove('on'); changed = true; }
  });
  if (changed) {
    syncOverlayA11y();
    if (window.syncOverlayMask) syncOverlayMask();
    /* 焦点归还：原来落在被遮住的页面上，关闭后必须能接回去，
       否则键盘用户会掉回 body 顶（等于"页面没了"）。 */
    if (overlayOpener && document.contains(overlayOpener) && overlayOpener.focus) {
      try { overlayOpener.focus(); } catch (e) { /* 元素已不可聚焦：静默降级 */ }
    }
    overlayOpener = null;
  }
  return changed;
}
function closeOverlay(id) {
  const el = $(id);
  const wasOpen = !!(el && el.classList.contains('on'));
  if (wasOpen) el.classList.remove('on');
  if (wasOpen) {
    syncOverlayA11y();
    if (overlayOpener && document.contains(overlayOpener) && overlayOpener.focus) {
      try { overlayOpener.focus(); } catch (e) { }
      overlayOpener = null;
    }
  }
  if (window.syncOverlayMask) syncOverlayMask();
}
function openOverlay(id) {
  rememberOverlayOpener();
  closeDrawers();                      // ① 只允许一个抽屉在开
  const el = $(id);
  if (el) el.classList.add('on');
  if (window.collapseSidebar) collapseSidebar();   // 窄屏别让侧栏抽屉和它叠着
  if (window.syncOverlayMask) syncOverlayMask();
  syncOverlayA11y();
  /* 焦点进抽屉：落在首个可聚焦元素上（关闭按钮），键盘用户不必先 Tab 一圈找路。
     用 setTimeout 是因为 openOverlay 常在刚写完 innerHTML 后调用，要等渲染。 */
  setTimeout(() => {
    const cur = $(id);
    if (!cur || !cur.classList.contains('on')) return;   // 期间已被关掉就别再抢焦点
    const f = overlayFocusablesIn(cur)[0] || cur;
    if (f && f.focus) { try { f.focus({ preventScroll: true }); } catch (e) { f.focus(); } }
  }, 0);
}
/* 抽屉开着时 Tab 在抽屉内循环（inert 之外的显式兜底：Safari 对 inert 支持不齐，
   且 <11 的 Chromium 也没有；两条一起上，任何一档都能兜住）。 */
document.addEventListener('keydown', e => {
  if (e.key !== 'Tab') return;
  const openId = OVERLAY_IDS.find(overlayOpen);
  if (!openId) return;
  const el = $(openId);
  const list = overlayFocusablesIn(el);
  if (!list.length) { e.preventDefault(); return; }
  const first = list[0], last = list[list.length - 1];
  if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
  else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
});

function go(page) {
  /* 持久化状态必须校验后回退（2026-09-23 事故第二幕）：启动时直接吃
     `localStorage.getItem('hub.page')`，而这个值可能是**跨版本已改名的页名**（09-20
     「界面统一」改过一批）。下面原本只有 `toggle('on', s.id === 'page-' + page)` ⇒
     一旦认不出来就**把所有页面一起关掉** ⇒ 正文整块空白、页内零个可点元素。
     而 localStorage 是按 origin 隔离的 ⇒ 同一份代码在局域网那个源正常、
     在 Tailscale 那个源空白。认不出就退回总览，并留下可取证的 warn。 */
  if (!document.getElementById('page-' + page)) {
    console.warn('go(): 未知页面 "' + page + '"，退回 classroom');
    page = 'classroom';
  }
  curPage = page;
  // ② 导航即清浮层（只在窄屏强制：桌面上抽屉是右侧常驻面板，收掉反而影响操作）
  if (window.isNarrow && window.closeDrawers && isNarrow() && overlayAnyOpen()) closeDrawers();
  document.querySelectorAll('.sidebar button[data-page], .sidebar .side-item[data-sys]').forEach(b => {
    const on = (b.dataset.page || b.dataset.sys) === page;   // 常驻顶栏项用 data-sys，取值要看两个属性
    b.classList.toggle('on', on);
    if (b.getAttribute('role') === 'tab') b.setAttribute('aria-selected', on ? 'true' : 'false');
  });
  document.querySelectorAll('section.page').forEach(s => s.classList.toggle('on', s.id === 'page-' + page));
  lsSet('hub.page', page);  // T9：记忆上次所在页，刷新后回落
  renderNav();            // v0.7：同步左侧手风琴（实体/系统项的选中态）
  renderPageCrumb(page);  // v0.7：系统页面包屑（实体页由 renderModeBar 接管）
  // P1（PT-20261018-08）：每次进页重读一次 vitals 投影（<5ms，带 as_of）。
  // 不再用 memLoaded 闸：整会话只加载一次会让绿点静默变旧，而这份数据本来就是
  // 「哪一刻采的」而不是「现在怎样」——重读成本远低于误导成本。
  if (page === 'memory') memVitals();
  // 技能页：skillsLoaded 只挡「整页重载」。重进页必须补刷调用徽标——
  // 那个闸会让徽标永远停在会话首屏那一刻（2026-10-20 用户报「一直显示 0」）。
  if (page === 'skills') {
    if (!skillsLoaded) { skillsLoaded = true; loadSkills(); loadSkillBudget(); }
    else loadSkillInvBadges(false);
  }
  if (page === 'kb' && !kbLoaded) { kbLoaded = true; loadKbStatus(); kbBrowse(); }
  if (page === 'localprojects' && !lpLoaded) { lpLoaded = true; loadLocalProjects(); }   // v0.13.30 本机项目页（09 分片）
  if (page === 'github' && !ghLoaded) { ghLoaded = true; loadGithubRepos(); }   // v0.13.31 GitHub 项目页（10 分片）
  // v0.13.43 设置三页：与系统页同口径，进页才拉（模型清单 / GitHub 现值）
  if (page === 'settings-model' && !setModelPageLoaded) { setModelPageLoaded = true; settingsModelsLoad(); }
  if (page === 'settings-github' && !setGithubPageLoaded) { setGithubPageLoaded = true; settingsGithubLoad(); }
  if (page === 'settings-logs' && !setLogsPageLoaded) { setLogsPageLoaded = true; settingsLogsLoad(); }
  if (page === 'settings-memory' && !setMemPageLoaded) { setMemPageLoaded = true; loadMemories(); loadDoc('l2'); loadDoc('l3'); }
  if (page === 'ports' && !portsLoaded) { portsLoaded = true; loadPorts(); }
  if (page === 'telemetry') loadTelemetry();
  if (page === 'resources' && !resLoaded) { resLoaded = true; loadResources(); }
  // v0.13.47：原运行日志页的进页钩子随页面一并删除（内容并入设置→日志）
  if (page === 'chat') renderChatSide();
  if (page === 'tasks') { fillAgentSelect($('taskAgent'), true); loadRuns(); }
  if (page === 'jobs') { fillAgentSelect($('jobAgent'), false); loadJobs(); }
  if (page === 'mcp') { loadMcp(); loadAcl(); }
  if (page === 'assets') loadAssets();   // P4 资产面板（只读门面聚合，07-asset-panel.js）
}

function fillAgentSelect(sel, withAuto) {
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = (withAuto ? '<option value="">自动选择执行 Agent</option>' : '<option value="">默认 jcode</option>') +
    AGENTS.map(a => '<option value="' + a.id + '">' + escapeHtml(a.name) + '</option>').join('');
  if (cur) sel.value = cur;
}

/* 上游 loopback→LAN 语义：把 127.0.0.1 换成当前访问主机名 */
function lanUrl(url) {
  if (!url) return url;
  return url.replace(/127\.0\.0\.1|localhost/g, location.hostname);
}

/* ── 教室 ─────────────────────────────────────────── */



let agentFailStreak = 0;
async function loadAgents() {
  /* 2026-10-05：页面隐藏时早退。/api/agents 是全站最重的只读端点（25 个 agent，
   * 每次跑 docker ps + systemctl —— 见 profiles.py 的 TTL 缓存），30s 一次在后台
   * 标签里纯属白烧。守卫放**函数体内**不动 setInterval 注册（06:339）。
   * 语义无副作用：loadAgents 失败时本就「沿用旧值」，早退不改变可见行为。 */
  if (document.hidden) return;
  try {
    const d = await api('/api/agents');
    AGENTS = d.agents || [];
    agentFailStreak = 0;
    renderHomeStats();
    renderNav();   // v0.7.1：Agent / 基础设施的唯一入口是左侧手风琴，随数据刷新
    /* v0.13.103 补画项目页的 agent 下拉（用户 2026-10-11 报障：「本机项目」右侧
       agent 框不能正常显示，点 GitHub 项目菜单后才正常）。根因是**竞速**：
       06 顶层 go(lsGet('hub.page')) 同步跑，落地页=本机项目时 loadLocalProjects()
       立刻发请求，而 AGENTS 由本函数异步填；/api/localprojects 先回来时
       lpRenderAgents()/ghRenderAgents() 把 select 的 option 清成 0 个（框塌成
       46px 空白），此后再无人重画。终态一致性因此放在**数据到位点**：agent
       清单一到就补画一次，谁先谁后都对。
       typeof 守卫：两函数在 09/10 分片（拼接序在后），且分片被裁剪时不该炸。 */
    if (typeof lpRenderAgents === 'function') lpRenderAgents();
    if (typeof ghRenderAgents === 'function') ghRenderAgents();
    /* 2026-10-08（用户裁定，两次）：顶栏的「Agents: N（可用 N · 未实测 N · 待检 N）」
       整块摘除——先删括号明细（顶栏宽度紧张、窄屏被 ellipsis 截断），
       后删整块（明细在左侧手风琴的卡片徽章上已有，顶栏不必复述）。
       本函数原来那三个 filter 与 `const ag` 一并删除，现在只负责异常块。
       口径未变：异常仍由 countBadAgents 单点判定（test_health_verdict_single_source 钉住）。 */
    const errs = countBadAgents(AGENTS);
    $('hErrors').innerHTML = errs ? '<span class="hdot r" title="启动异常的 Agent（vitals 实测结论）">异常 ' + errs + '</span>' : '';
  } catch (e) {
    // 服务重启窗口容忍瞬时失败；连续 ≥2 次才报（避免误报）。
    // 2026-10-08：原先这里的 setHealthDot('r', …) 随健康灯一起摘掉了——
    // 那是 Hub 不可达在顶栏唯一的灯色提示，兜底就是下面这条 toast。
    agentFailStreak++;
    if (agentFailStreak === 2) {
      toast('Hub 数据连续 ' + agentFailStreak + ' 次拉取失败：' + e.message, 'err');
    }
  }
}

/* ── v0.7.1 首页统计摘要（取代 renderSeats：Agent / 基础设施已全收拢至左侧手风琴）── */
function renderHomeStats() {
  const ag = AGENTS.filter(a => a.kind === 'agent');
  const infra = AGENTS.filter(a => a.kind !== 'agent');
  const running = ag.filter(a => a.attested === true).length;   /* 「可用」只统计有实测凭据的 */
  const errs = countBadAgents(AGENTS);   /* P1-20：与顶栏异常块同一判据 */
  const set = (id, v) => { const el = $(id); if (el) el.textContent = v; };
  set('cntAgent', ag.length);
  set('cntInfra', infra.length);
  // v0.13.74：窄屏文案是**另一份 DOM**（.only-narrow 隐藏宽屏那份），
  // 计数器要同样写两份，否则窄屏上会一直显示模板里的占位「–」。
  // 两份文案由 CSS 断点二选一，数字只从这一个地方发 —— 不另起真相源。
  set('cntAgent2', ag.length);
  set('cntInfra2', infra.length);
  set('hsAgent', ag.length);
  set('hsRunning', running);   /* 标签在模板里已改「可用」：取 vitals 结论，无结论时退为在线 */
  set('hsInfra', infra.length);
  set('hsError', errs);
  const errCard = $('hsError');
  if (errCard && errCard.parentElement) errCard.parentElement.classList.toggle('alert', errs > 0);
}

/* ── T5：定时拉 /health，驱动顶栏「需重启」标签 ──
 * 2026-10-08（用户裁定）：顶栏的健康灯圆点 + 「Hub 在岗」文字**整块删除** ⇒
 * `setHealthDot` 连同它在 pollHealth 里的两处调用（success 的 g/y、catch 的 r）
 * 一并摘掉——留着一个 target 恒为 null 的写端，就是 P1-18 注释里点名的死调用。
 * pollHealth 本身**保留**：它现在的唯一职责是兑 needs_restart（需重启报警，用户同裁定保留）。
 * ⚠ 代价（已知并接受）：Hub 整体不可达时顶栏不再有灯色提示，只剩 loadAgents 的 toast。 */
async function pollHealth() {
  /* 2026-10-05：页面隐藏时早退（15s 一次的健康轮询在后台标签里白跑）。
   * 守卫放函数体内，不动 06:340 的 setInterval 注册。 */
  if (document.hidden) return;
  try {
    const d = await api('/health');
    // P1-19：把 needs_restart 兑到顶栏。
    // 字段名以 src/selfattest.py 的 snapshot() 为准：needs_restart 才是本体，
    // code_stale 只是兼容别名，且「不可判定」时被压成 False —— 那种情况
    // 另有 code_stale_reason 说明，所以只在明确为 true 时才提示。
    const staleEl = $('hStale');
    if (staleEl) {
      const needRestart = d && d.needs_restart === true;
      if (needRestart) {
        staleEl.innerHTML = '<button type="button" class="stale-tag stale-btn" title="' +
          escapeHtml('运行中的代码与磁盘不一致（启动于 ' + ((d && d.git_sha_boot) || '?') +
                      '，当前磁盘 ' + ((d && d.git_sha_now) || '?') +
                      '）。点此重启 agenthub 让改动生效。') + '">需重启</button>';
        const b = staleEl.querySelector('button');
        if (b) b.addEventListener('click', restartHub);
      } else {
        staleEl.innerHTML = '';
      }
    }
  } catch (e) {
    /* 灯色提示已随「Hub 在岗」删除，这里**不再弹 toast**——pollHealth 每 15s 一次，
     * Hub 真不可达时会连着弹十几条，比没提示更糟。Hub 不可达的兜底提示由
     * loadAgents 的 toast 承担（30s 一次、连续 ≥2 次才报，见其 catch 分支）。 */
  }
}


/* 点「需重启」→ 自重启 agenthub（后端 POST /api/system/restart）。
   有活终端会话时后端回 409，确认后再带 force=1（与 agent-hubctl 的 guard 同口径）。
   重启窗口内 /health 必然不可达 ⇒ 轮询到恢复再刷新页面拿新静态资产。 */
async function restartHub(ev) {
  const btn = ev.currentTarget;
  const old = btn.textContent;
  btn.disabled = true;
  btn.textContent = '重启中…';
  const post = (force) => api('/api/system/restart' + (force ? '?force=1' : ''), { method: 'POST' });
  try {
    try { await post(false); }
    catch (e) {
      if (e.http !== 409) throw e;
      const n = (e.payload && e.payload.detail && e.payload.detail.term_sessions) || '?';
      if (!confirm('当前有 ' + n + ' 个终端会话在跑，重启会中断它们。确认重启 agenthub？')) {
        btn.disabled = false; btn.textContent = old; return;
      }
      await post(true);
    }
  } catch (e) {
    toast('重启请求失败：' + e.message, 'err');
    btn.disabled = false; btn.textContent = old; return;
  }
  let back = false;
  for (let i = 0; i < 90; i++) {                 // 最多 ~90s
    await new Promise((r) => setTimeout(r, 1000));
    try { const d = await api('/health'); if (d && d.status) { back = true; break; } } catch (e) { /* 重启窗口内正常失败 */ }
  }
  if (back) { toast('agenthub 已重启'); setTimeout(() => location.reload(), 600); }
  else { toast('重启后 90s 内未恢复，请检查服务', 'err'); btn.disabled = false; btn.textContent = old; }
}

/* 模型层情报（rt_state）→ 短标。它只影响副标文案，永远不影响「可用」。 */
const RT_LABELS = {
  answered: '已应答', blocked_by_account: '额度/登录', rate_limited: '上游限流',
  model_unsupported: '模型标识', timeout: '应答超时', probe_rejected: '探针缺陷',
  no_output: '无输出', skipped: '未实测'
};
/* ── P1-20（2026-09-30）：全站唯一「这个 Agent 到底算不算异常」判据 ────────────
   修的是**口径打架**：同一份 /api/agents 数据，四处各判各的 ——
     顶栏可用数   a.attested === true          （10/10）
     卡片标签     a.verdict === 'usable'        （6/10）
     异常计数     a.status === 'error'          （恒为 0：vitals 从不把 status 打成 error）
     导航排序     NAV_RANK[a.status]
   用户看到的现象是「顶栏说 10 个可用，列表里有 4 个标着别的东西，异常数永远是 0」，
   而 0 是**结构性假象**（判据取了一个永远不取该值的字段），不是「真没异常」。

   口径按证据强度排，只认 vitals 的实测结论：
     usable   实测通过                       → 正常
     broken   启动异常（实测起不来）           → 异常
     其余     账号受限/未运行/未安装/待体检   → 都不是「异常」，各自保留原标签
   非 agent 类型（网关/服务/工具/记忆）不参与这套裁决：它们的 status 由 systemd
   与 docker 判定，与 vitals 的 agent 结论不是一回事，混进来会把「服务没起」算成 agent 异常。 */
function agentHealth(a) {
  if (!a || a.kind !== 'agent') return { bad: false, verdict: a ? a.verdict : null };
  const v = a.verdict;
  return { bad: v === 'broken', verdict: v || null };
}
/** 异常 Agent 的唯一计数口径：顶栏异常块与首页摘要都调它，不允许各自 filter。 */
function countBadAgents(list) {
  return (list || []).filter(a => agentHealth(a).bad).length;
}

function seatStateOf(a) {
  /* 生死只看 verdict（能否打开窗口 + 能否自检）。额度/限流/超时是账号与上游条件，
     不配把菱形打成「异常」—— 那正是上一版误伤 claude/jcode/grok 的地方。 */
  if (a.kind === 'agent' && a.verdict === 'broken')
    return 'error';
  return a.status === 'running' ? 'running' : (a.status === 'installed' ? 'installed' : (a.status === 'error' ? 'error' : 'stopped'));
}
const SEAT_LABELS = { running: '在线', installed: '可启动', stopped: '离线', error: '异常' };
/* 判定结果给人看的短标：宁可短句，详情进 tooltip 与详情抽屉 */
const VERDICT_LABELS = { usable: '可用', blocked_by_account: '账号受限', broken: '启动异常',
                         stopped: '未运行', not_installed: '未安装', pending: '待体检',
                         unknown: '未判定' };
function seatLabelOf(a) {
  const st = seatStateOf(a);
  // 只有真跑通过应答（或自有端点在应声）才叫「可用」；仅自检正常 → 「未见异常」
  if (a.kind === 'agent' && a.verdict === 'usable') {
    const base = a.attested === false ? '未见异常' : '可用';
    // 模型层有情况就在标签里带一句，但底色仍是正常态（不是异常）
    const r = RT_LABELS[a.rt_state];
    return (a.rt_state && a.rt_state !== 'answered' && r) ? base + '·' + r : base;
  }
  if (a.kind === 'agent' && a.verdict && a.verdict !== 'stopped' && VERDICT_LABELS[a.verdict])
    return VERDICT_LABELS[a.verdict];
  return SEAT_LABELS[st];
}




function showDetail(id) {
  const a = AGENTS.find(x => x.id === id);
  if (!a) return;
  bindDetailDelegation();
  $('detailTitle').textContent = a.name + '（' + (a.type || a.kind || '-') + ' · ' + a.status + '）';
  const rows = [['endpoint', a.endpoint], ['config_path', a.config_path], ['working_dir', a.working_dir], ['description', a.description]];
  let html = rows.filter(([, v]) => v).map(([k, v]) =>
    '<div style="margin:6px 0"><div class="hint">' + k + '</div>' +
    '<div style="font-family:var(--font-mono);font-size:var(--fs-sm);word-break:break-all;color:var(--text-1)">' + escapeHtml(v) + '</div></div>').join('');
  const es = a.entries || [];
  /* vitals 台账：判定是什么、凭什么判的、什么时候判的 —— 可审计，不只是个颜色 */  if (a.kind === 'agent' && a.verdict) {
    html += '<div class="hint" style="margin:10px 0 4px">可用性判定</div>' +
      '<div style="font-size:var(--fs-sm);color:var(--text-1)">' +
      escapeHtml(VERDICT_LABELS[a.verdict] || a.verdict) +
      ' （来源 ' + escapeHtml(a.verdict_source || '-') +
      (a.verdict_confidence != null ? '，置信 ' + a.verdict_confidence.toFixed(2) : '') + '）</div>' +
      '<div class="hint" style="margin-top:2px">' + escapeHtml(a.verdict_reason || '') + '</div>' +
      '<div class="hint" style="margin-top:2px">模型层：' +
      escapeHtml(RT_LABELS[a.rt_state] || a.rt_state || '未实测') +
      (a.rt_model ? '（模型 ' + escapeHtml(a.rt_model) + '）' : '') +
      (a.rt_note ? '<div style="font-family:var(--font-mono);font-size:11px;color:var(--text-3);word-break:break-all;margin-top:2px">' + escapeHtml(a.rt_note) + '</div>' : '') +
      '</div>' +
      '<div class="hint" style="margin-top:2px">' +
      '<button class="act-btn" style="display:inline-block;padding:2px 8px" ' +
      'data-sk-verify="' + escapeHtml(a.id) + '" ' +
      'title="跑一次真实请求（耗 token，只出模型层情报，不改可用性结论）">实测应答</button>' +
      (a.verdict_at ? ' 上次 ' + escapeHtml(String(a.verdict_at).slice(0, 19).replace('T', ' ')) + 'Z' : '') +
      '</div>';
  }
  html += '<div class="hint" style="margin:10px 0 4px">entries</div>' +
    (es.length ? es.map(e => '<span class="tag agent" style="display:inline-block;margin:2px 4px 2px 0;max-width:100%;overflow-wrap:anywhere">' +
      escapeHtml(e.type) + (e.url ? ': ' + escapeHtml(e.url) : '') + '</span>').join('') : '<span class="hint">无</span>');
  $('detailBody').innerHTML = html || '<span class="hint">无附加信息</span>';
  openOverlay('detailDrawer');
  /* v0.13.29：claude 实体的详情抽屉追加「CloudCLI 项目」面板——用户核心诉求
     「加载本机所有项目、精确显示名称、点击快速开始」。列表直读 auth.db 与
     cloudcli 服务活死解耦；点「开始会话」走 cloudcliStart（04 分片）。 */
  if (id === 'claude' && typeof loadCloudcliProjects === 'function') loadCloudcliProjects();
}
function closeDetail() { closeOverlay('detailDrawer'); }

/* v0.13.131（PT-20261019-02）：详情抽屉里的动作按钮改 data-* + 委托。
 *
 * 原形态 `onclick="verifyAgent(<jsStr(id)>)"`：jsStr 产出**双引号**字面量，
 * 嵌在双引号界定的属性里会被 HTML 解析器提前截断成 `verifyAgent(` ⇒
 * 点击静默 SyntaxError（v0.13.59 起 19 天，静态闸门看不出来）。
 * data-* 值走 escapeHtml（`"` → `&quot;`），dataset 读回原值，任意 id 都不断属性。
 *
 * 一个容器两处受益：#1 verifyAgent（showDetail 渲染）与 #4 cloudcliStart
 * （04 分片 loadCloudcliProjects 也渲染进 #detailBody）共用这一个监听。
 * #detailBody 是静态元素，抽屉多次 innerHTML 重写不掉监听；marker 防重复挂。
 */
function bindDetailDelegation() {
  const el = $('detailBody');
  if (!el || el.__detailDeleg) return;
  el.__detailDeleg = true;
  el.addEventListener('click', e => {
    const v = e.target.closest('[data-sk-verify]');
    if (v && el.contains(v)) {
      e.stopPropagation();
      verifyAgent(v.dataset.skVerify || '');
      return;
    }
    const c = e.target.closest('[data-sk-ccstart]');
    if (c && el.contains(c)) {
      e.stopPropagation();
      cloudcliStart(c.dataset.skCcstart || '');
    }
  });
}

/* L4 体检：让 hub 现场跑一次真实一次性请求（耗 token、冷启动可近 60s），完事刷列表 */
async function verifyAgent(id) {
  toast('体检 ' + id + '：正在跑真实请求（模型层实测，最长 40s）…');
  try {
    const d = await api('/api/agents/' + encodeURIComponent(id) + '/verify', { method: 'POST' });
    const ev = d.evidence || {};
    const rts = (d.rt_state || (d.evidence || {}).rt_state || 'skipped');
    toast(id + ' → ' + (VERDICT_LABELS[d.verdict] || d.verdict) +
      '｜模型层 ' + (RT_LABELS[rts] || rts),
      d.verdict === 'usable' ? 'ok' : 'err');
    await loadAgents();
    showDetail(id);
  } catch (e) { toast('体检失败：' + e.message, 'err'); }
}

/* ── 设置（口令保护的 TERM_TOKEN 查看/应用）──
   v0.13.43：设置不再是抽屉。三个子页（模型 / GitHub / 终端口令）是 main 里的
   section.page，由左侧「设置」手风琴组切换 —— 入口从 btnSettings 的 inline onclick
   变成与系统页同口径的 data-sys ⇒ 委托 ⇒ go(page)，顺带继承"窄屏点完自动收侧栏"。 */
/* 口令三源：页内输入框 → 本机缓存 → prompt（最后一源保留但不依赖它）。
   页内框是 v0.13.44 加的：APP 内嵌 WebView 与部分手机浏览器会直接吞掉 prompt()
   （返回 null 且不弹窗），那种环境里"点保存毫无反应"就是这么来的 —— 凡是靠
   prompt 收口令的路径，在端侧都等于静默失败。 */
function passcodeInput() {
  for (const id of ['setPasscode', 'ghPasscode']) {
    const el = $(id);
    if (el && el.value.trim()) return el.value.trim();
  }
  return '';
}
function clearPasscodeInput() {
  ['setPasscode', 'ghPasscode'].forEach(id => { const el = $(id); if (el) el.value = ''; });
}
function settingsPasscode() { return passcodeInput() || lsGet('hub.passcode') || ''; }

async function settingsViewToken() {
  let pc = settingsPasscode();
  if (!pc) {
    pc = prompt('请输入设置口令（HUB_PASSCODE，向 hub 索要；仅存本浏览器）') || '';
    if (!pc) return;
  }
  try {
    const d = await api('/api/settings/term-token', {
      method: 'POST',
      headers: { 'Content-Type': 'application/json', 'X-HUB-PASSCODE': pc },
      body: JSON.stringify({ passcode: pc })
    });
    lsSet('hub.passcode', pc);  // 验证通过才缓存
    const box = $('settingsTokenBox');
    box.style.display = 'block';
    $('settingsTokenVal').dataset.token = d.term_token || '';
    $('settingsTokenVal').textContent = d.set ? (d.term_token || '（空）') : '服务端未配置 TERM_TOKEN（终端鉴权走启动时自动生成的临时 token）';
    $('settingsToggleShow').textContent = '隐藏';
    if (!d.set) toast('服务端未显式配置 TERM_TOKEN', 'err');
  } catch (e) {
    lsRemove('hub.passcode');  // 口令错/失效则不缓存
    $('settingsTokenBox').style.display = 'none';
    toast(e.message, 'err');
  }
}

function settingsToggleShow() {
  const el = $('settingsTokenVal'), btn = $('settingsToggleShow');
  if (el.textContent.indexOf('••') === 0) { el.textContent = el.dataset.token || ''; btn.textContent = '隐藏'; }
  else { el.textContent = '••••••••••••••••••••'; btn.textContent = '显示'; }
}

function settingsCopyToken() {
  const t = $('settingsTokenVal').dataset.token || '';
  if (!t) return toast('无 token 可复制', 'err');
  (navigator.clipboard ? navigator.clipboard.writeText(t) : Promise.reject())
    .then(() => toast('已复制到剪贴板', 'ok'))
    .catch(() => { prompt('请手动复制：', t); });
}

function settingsApplyToken() {
  /* 尾随空白会把「REST 通、WS 挂」的坑写进 localStorage（见 wsUrl 注释），写入口也 trim。 */
  const t = ($('settingsTokenVal').dataset.token || '').trim();
  if (!t) return toast('无 token 可应用', 'err');
  lsSet('hub.term.token', t);
  toast('已应用到终端（本浏览器后续自动携带）', 'ok');
}

function settingsClearPasscode() {
  lsRemove('hub.passcode');
  $('settingsTokenBox').style.display = 'none';
  toast('已清除本机缓存口令，下次查看需重新输入', 'ok');
}

/* ── 设置 → 模型子菜单（v0.13.41）──────────────────────────────────────
   交互两步：先选 agent，再选该 agent 要用的 CCR 模型；保存前给 diff 预览，
   保存时要口令（HUB_PASSCODE，与「终端口令」子页共用一个本机缓存的口令）。
   三条纪律：
     ① **不新增浮层**：v0.13.43 起三个子页是 main 里的 section.page（抽屉已拆），
        切换由左侧手风琴 + go(page) 完成 —— 比"抽屉内切 section"更彻底：压根没有浮层；
     ② **不用 inline onclick**：一切走三个设置页上的同一委托 settingsDelegates()
        （inline 会旁路委托，把"导航即清浮层"那类收场逻辑整段跳过）；
     ③ **档位判定读当前值、不读存档**：进哪个子页由点击决定，不写 localStorage
        （跨 origin 分叉那条不变量的同款要求）。 */
let SET_AGENTS = [];          // /api/settings/models 的 agents 段
let SET_AGENT = '';           // 当前选中的 agent（不落盘）
let SET_MODELS = null;        // /api/models 缓存（60s）
let SET_DIFF = null;          // 最近一次预览结果（保存时复用同一个 model 值）
let SET_DRIFT = [];           // 配置文件漂移体检（v0.13.85：此前后端算了但前端从不显示）
/* v0.13.85：写入模式文案的单一真相源，与后端 WRITE_MODE 一一对应。
   ccr   = 该字段直接吃 CCR 的 provider/model ID
   native= 该字段只认本家原生模型名（qoder 实测拒绝 CCR ID）
   argv  = 无默认配置文件可落，只能靠拉起终端时注入 --model */
const SET_WRITE_MODE = {
  ccr: { label: 'CCR 模型 ID', hint: '此 agent 的模型字段直接接受 CCR 的 provider/model ID。' },
  native: { label: '本家原生模型名', hint: '此 agent 的模型字段只认本家原生名（如 Qwen3.8-Max），' +
    'CCR 模型不写进该字段，只经终端 --model 注入生效。' },
  argv: { label: '仅终端注入', hint: '此 agent 无默认模型配置文件，只有 hub 拉起的终端会带 --model。' },
  none: { label: '不支持设置', hint: '该 agent 不在模型设置范围内。' },
};
/* 设置页 id —— 委托与 go() 懒加载共用这一份清单，别再各处写字面量 */
const SET_PAGE_IDS = ['page-settings-model', 'page-settings-github', 'page-settings-token',
                      'page-settings-memory', 'page-settings-logs'];

async function settingsModelsLoad(force) {
  const box = $('setAgentList');
  if (!box) return;
  if (!SET_AGENTS.length || force) {
    try {
      const d = await api('/api/settings/models');
      SET_AGENTS = d.agents || [];
      SET_DRIFT = d.drift || [];
      settingsRouterRender(d.ccr || {});
    } catch (e) {
      box.innerHTML = '<span class="hint">agent 清单加载失败：' + escapeHtml(e.message) + '</span>';
      return;
    }
  }
  const drifted = new Set(SET_DRIFT.map(x => x.id));
  box.innerHTML = SET_AGENTS.map(a => {
    const off = !a.writable;
    const why = a.reason || a.note || '不可设置';
    /* tip 用成分行拼装：provider 只在它真是"provider"时才说（v0.13.85 前 grok 的
       provider 字段串位显示成模型 ID，tip 就变成了 "provider alibaba/xxx" 这种误导）。 */
    const lines = [];
    if (off) {
      lines.push(why);
    } else {
      const wm = SET_WRITE_MODE[a.write_mode || 'argv'] || SET_WRITE_MODE.argv;
      lines.push((a.name || a.id) + ' · 字段认：' + wm.label);
      lines.push('文件现值：' + (a.current || '（未读到）') +
        (a.provider ? '（provider ' + a.provider + '）' : ''));
      lines.push('hub 侧：' + (a.hub_model || '（未设置）'));
      if (a.files && a.files.length) lines.push(a.files.join('\n'));
      if (drifted.has(a.id)) {
        const d = SET_DRIFT.find(x => x.id === a.id) || {};
        lines.push('⚠ 检测到配置漂移：' + JSON.stringify(d.owned || {}));
      }
    }
    return '<button class="btn sm' + (a.id === SET_AGENT ? ' on' : '') +
      (drifted.has(a.id) ? ' drift' : '') + '" type="button"' +
      ' data-settings-agent="' + escapeHtml(a.id) + '"' +
      (off ? ' aria-disabled="true"' : '') +
      ' title="' + escapeHtml(lines.join('\n')) + '">' + escapeHtml(a.name || a.id) +
      (drifted.has(a.id) ? ' ⚠' : '') + '</button>';
  }).join('') || '<span class="hint">无可用 agent</span>';
  settingsDriftRender();
}

/* v0.13.85：漂移体检结果必须**在页面上出现**。
   此前 /api/settings/models 一直在算 drift（后端 drift_report），前端却一个字都没渲染
   ⇒ 「CCR 重启把 claude 的 env 改回旧模型」这类问题在界面上完全不可见，
   与"体检全绿"无从区分。这里把它摆到明面上；只读，不提供写回按钮
   （写回属共享配置写入，走既有的 repair_drift + 用户授权，不在本页扩张）。 */
function settingsDriftRender() {
  const box = $('setDriftBox');
  if (!box) return;
  if (!SET_DRIFT.length) {
    box.className = 'hint';
    box.textContent = '配置漂移体检：' + SET_AGENTS.length + ' 个 agent 的配置文件现值与 hub 侧一致。';
    return;
  }
  box.className = '';
  box.innerHTML = '<div class="set-drift"><div class="set-diff-hd">⚠ ' + SET_DRIFT.length +
    ' 个 agent 的配置文件现值与 hub 侧不一致（常见原因：CCR 重启会重写 claude 的 env 三兄弟）</div>' +
    SET_DRIFT.map(d => '<div class="set-diff-row"><div class="k">' + escapeHtml(d.id) +
      '（hub 侧 ' + escapeHtml(d.hub_model || '') + '）</div>' +
      Object.keys(d.owned || {}).map(k => '<div class="v">' + escapeHtml(k) + ' = ' +
        escapeHtml((d.owned || {})[k]) + '</div>').join('') + '</div>').join('') +
    '<div class="hint" style="margin-top:6px">只读展示：写回配置属共享配置写入，由后端漂移巡检按 hub 侧现值修复（带时间戳备份）。</div></div>';
}

function settingsRouterRender(ccr) {
  const box = $('setCcrRouter');
  if (!box) return;
  const r = ccr.router || {};
  const keys = Object.keys(r);
  if (!keys.length) { box.textContent = ccr.note || '未读到 CCR 配置'; return; }
  box.innerHTML = '<div class="set-router">' + keys.map(k =>
    '<span class="k">' + escapeHtml(k) + '</span><span class="v">' + escapeHtml(r[k]) + '</span>').join('') +
    '</div><div class="hint" style="margin-top:6px">只读：CCR 属三方互斥保护面，且运行中写 config.json 会被运行态覆盖。</div>';
}

function settingsPickAgent(id) {
  const a = SET_AGENTS.find(x => x.id === id);
  if (!a || !a.writable) return;
  SET_AGENT = id;
  SET_DIFF = null;
  settingsModelsLoad();                       // 重渲染选中态
  $('setModelStep').style.display = 'block';
  $('setDiffBox').style.display = 'none';
  /* v0.13.45：不再在这里把保存按钮置灰 —— 置灰的按钮不派发 click，用户点它
     得不到任何反馈（日志里连请求都没有），正是第二轮报障的形态。 */
  settingsAgentMeta(a);
  settingsModelOptions(a);
}

async function settingsModelOptions(a) {
  const sel = $('setModelSel');
  if (!sel) return;
  const fresh = !SET_MODELS || (Date.now() - (SET_MODELS._ts || 0) > 60000);
  if (fresh) {
    sel.innerHTML = '<option value="">模型清单加载中…</option>';
    try {
      const d = await api('/api/models');
      SET_MODELS = { models: d.models || [], groups: d.groups || {}, _ts: Date.now() };
    } catch (e) {
      sel.innerHTML = '<option value="">模型清单加载失败</option>';
      return;
    }
  }
  // 未设 hub 侧时以 agent 现值预选；两项都没有则「默认（网关路由）」= 空值
  const cur = a.hub_model || a.current || '';
  /* v0.13.86：**选项值必须是该 agent 字段真正要的形状**。
     用户的准则是「原生 + 插入，插入要精确」—— 而"精确"从下拉这一层就要成立：
     后端 CCR 清单给的是裸 ID（alibaba/qwen3.8-max），opencode 的字段收的却是
     `ccr/<裸 ID>`（官方 Models 页：格式即 provider/model；本机 `opencode models ccr`
     逐行皆 `ccr/…`）。改前这里直接拿裸 ID 当 value ⇒ 用户点任何一个都必然被 409 拒
     （报「没有 provider alibaba」，指向也错），而 qoder 那一栏更狠：13 个 CCR ID 全部
     必然 400。根因不是"没做校验"，是**下拉把不可写的值摆成了可选项**。
     现在：value_prefix 由后端随接口透出（单一真相源，前端不自己拼前缀）；
     models 非空的 agent（native 型）改成摆它自己的原生清单，且不再摆 CCR 分组。 */
  const prefix = a.value_prefix || '';
  const native = a.models || [];
  const wm = SET_WRITE_MODE[a.write_mode || 'argv'] || SET_WRITE_MODE.argv;
  /* v0.13.85：空值这一项改名为「撤销 hub 侧默认（不注入 --model）」。
     原名「默认（网关路由）」容易被读成"把该 agent 的配置也改成网关路由"，
     而清空**只**撤销 hub 注入、不动任何配置文件（见后端 preview_clear 的说明）。
     这个语义差别必须写在选项里，否则用户会以为配置被还原了。 */
  let html = '<option value="">撤销 hub 侧默认（不再注入 --model）</option>';
  /* 现值不在清单里（清单外模型 / qoder 那种原生名）时**补一项**：不补的话
     select 会落回第一项"撤销"，于是"当前值"在界面上被抹掉 —— 用户会以为已经清空了。
     补进来只是**显示**，不代表该值可写（写入校验仍在后端）。 */
  let extra = '';
  const known = new Set(native.concat((SET_MODELS.models || []).map(m => prefix + m.id)));
  if (cur && !known.has(cur)) {
    extra = '<optgroup label="当前值（不在可写清单内）">' +
      '<option value="' + escapeHtml(cur) + '" selected>' + escapeHtml(cur) + '</option></optgroup>';
  }
  if (native.length) {
    /* native 型（qoder）：只摆它认的原生名。摆 CCR 清单等于摆一排必然被拒的选项。 */
    html += '<optgroup label="' + escapeHtml(wm.label) + '（本家清单）">' +
      native.map(nm => '<option value="' + escapeHtml(nm) + '"' +
        (nm === cur ? ' selected' : '') + '>' + escapeHtml(nm) + '</option>').join('') +
      '</optgroup>';
  } else {
    const g = SET_MODELS.groups || {};
    Object.keys(g).forEach(gk => {
      html += '<optgroup label="' + escapeHtml(gk) + '">' + g[gk].map(mid => {
        const m = (SET_MODELS.models || []).find(x => x.id === mid);
        const val = prefix + mid;                 // ← 精确：值等于该字段要的形状
        const label = (m && m.name && m.name !== mid) ? (mid + ' — ' + m.name) : mid;
        return '<option value="' + escapeHtml(val) + '"' + (val === cur ? ' selected' : '') + '>' +
          escapeHtml(label) + '</option>';
      }).join('') + '</optgroup>';
    });
    const grouped = new Set(Object.keys(g).flatMap(k => g[k]));
    const rest = (SET_MODELS.models || []).filter(m => !grouped.has(m.id));
    if (rest.length) {
      html += '<optgroup label="其他">' + rest.map(m => {
        const val = prefix + m.id;
        return '<option value="' + escapeHtml(val) + '"' + (val === cur ? ' selected' : '') + '>' +
          escapeHtml(m.name || m.id) + '</option>';
      }).join('') + '</optgroup>';
    }
  }
  /* 顺序：撤销项固定第一（与改前「默认（网关路由）」同位，位置不飘），
     其后才是「当前值不在清单内」的补项，再是各 provider 分组。
     ⚠ 补项必须带 selected 才能被选中 —— 位置不决定选中态，属性才决定。 */
  sel.innerHTML = html + extra;
  settingsModelHint(a);
}

/* v0.13.86：下拉下方的一行口径说明。存在的理由：write_mode 的三类语义里，
   只有 argv 型（claude 之外那些「无配置文件可落」的）需要提醒"只影响 hub 拉起的会话"；
   native 型（qoder）必须说清"CCR 模型不写进该字段、只经 --model 注入"，
   否则用户会以为选了原生名就等于把 qoder 接到了 CCR 上。 */
function settingsModelHint(a) {
  const el = $('setModelHint');
  if (!el) return;
  if (!a) { el.textContent = ''; return; }
  const wm = SET_WRITE_MODE[a.write_mode || 'argv'] || SET_WRITE_MODE.argv;
  const bits = [wm.hint];
  if (a.value_prefix) {
    bits.push('选项值按该 agent 要求的形状拼成 ' + a.value_prefix + '<CCR 模型 ID>（官方格式 provider/model）。');
  }
  if ((a.models || []).length) {
    bits.push('原生清单由 hub 内置（以 `qodercli --list-models` 为准），会随版本变化。');
  }
  el.innerHTML = escapeHtml(bits.join(''));
}

function setSelectedModel() { return $('setModelSel') ? $('setModelSel').value : ''; }

async function settingsPreviewModel() {
  if (!SET_AGENT) return toast('先选一个 agent', 'err');
  /* v0.13.85：空值是合法目标（撤销 hub 侧默认），不再 early-return ——
     原先这里 `if (!model) return toast(...)` 让「撤销」那一项连预览都点不出来。 */
  const model = setSelectedModel();
  const box = $('setDiffBox');
  try {
    SET_DIFF = await api('/api/settings/model/preview?agent_id=' + encodeURIComponent(SET_AGENT) +
      '&model=' + encodeURIComponent(model));
  } catch (e) {
    SET_DIFF = null; box.style.display = 'none';
    settingsRenderError('预览失败', e);
    return toast('预览失败：' + e.message, 'err');
  }
  const wm = SET_WRITE_MODE[SET_DIFF.write_mode || 'argv'] || SET_WRITE_MODE.argv;
  if (SET_DIFF.mode === 'clear') {
    /* 清空必须把「配置文件不动」写在最显眼处：用户点这一项时最常见的预期是
       "把 agent 配置也还原成默认"，而实际语义只是"hub 不再注入 --model"。 */
    box.innerHTML = '<div class="set-diff"><div class="set-diff-hd">将撤销 hub 侧默认模型</div>' +
      '<div class="set-diff-row"><div class="v">hub 侧：' +
      escapeHtml(SET_DIFF.hub_model_from || '（未设置）') + ' → （无）</div>' +
      '<div class="v">' + escapeHtml(SET_DIFF.note || '') + '</div>' +
      '<div class="v">不写任何配置文件，因此没有备份、也不会有 diff。</div></div></div>';
    box.style.display = 'block';
    $('setApplyBtn').disabled = false;
    return;
  }
  const files = SET_DIFF.files || [];
  box.innerHTML = '<div class="set-diff">' +
    '<div class="set-diff-hd">将写入 ' + files.length + ' 个文件（保存时自动时间戳备份）' +
    '｜字段认：' + escapeHtml(wm.label) + '</div>' +
    files.map(f => '<div class="set-diff-row"><div class="k">' + escapeHtml(f.file) + '</div>' +
      (f.changes || []).map(c => '<div class="v">' + escapeHtml(c.where) + '：' +
        escapeHtml(c.from || '（空）') + ' → ' + escapeHtml(c.to) + '</div>').join('') +
      /* v0.13.44：预览就把「这个文件现在写不动」摆在明面上（chattr +i / 只读挂载），
         别等保存时才用一句 4 秒就消失的 toast 告诉用户。 */
      (f.writable === false ? '<div class="v">注意：该文件当前不可写，直接保存会被拒' +
        '（常见是被设了不可变属性，需先解除）</div>' : '') + '</div>').join('') +
    '</div>' + (SET_DIFF.argv && SET_DIFF.argv.length
      ? '<div class="hint" style="margin-top:6px">hub 拉起终端时追加：' + escapeHtml(SET_DIFF.argv.join(' ')) + '</div>'
      : '<div class="hint" style="margin-top:6px">此 agent 不注入 --model（无白名单 flag），' +
        '仅写配置文件。</div>');
  box.style.display = 'block';
  $('setApplyBtn').disabled = false;
}

/* 保存结果必须**常驻在页内**：09-27 报障「保存并不能生效」的真身是——保存其实成功
   了，但页面只闪一条 4 秒的 toast，状态行与下拉全都停在保存前的样子，用户据此判定
   没生效。所以成功要把落盘清单 + 备份路径写出来，失败要把 HTTP 状态与原因写出来，
   两样都不许只靠 toast。 */
function settingsRenderApplied(d) {
  const box = $('setDiffBox');
  if (!box) return;
  const applied = d.applied || [];
  if (d.mode === 'clear') {
    box.innerHTML = '<div class="set-diff"><div class="set-diff-hd">已生效：' +
      escapeHtml(d.agent_id) + ' 的 hub 侧默认模型已撤销</div>' +
      '<div class="set-diff-row"><div class="v">' + escapeHtml(d.note || '') + '</div>' +
      '<div class="v">该 agent 自己的配置文件未被改动（无备份产生）。</div></div></div>';
    box.style.display = 'block';
    return;
  }
  box.innerHTML = '<div class="set-diff"><div class="set-diff-hd">已生效：' +
    escapeHtml(d.agent_id) + ' → ' + escapeHtml(d.model) +
    (d.argv && d.argv.length ? '（新开终端追加 ' + escapeHtml(d.argv.join(' ')) + '）' : '') +
    '</div>' +
    applied.map(a => '<div class="set-diff-row"><div class="k">' + escapeHtml(a.file) + '</div>' +
      (a.changes || []).map(c => '<div class="v">' + escapeHtml(c.where) + '：' +
        escapeHtml(c.from || '（空）') + ' → ' + escapeHtml(c.to) + '</div>').join('') +
      '<div class="v">备份：' + escapeHtml(a.backup) + '</div></div>').join('') +
    '<div class="v">已开着的会话不受影响，新开终端才带这个模型。</div></div>';
  box.style.display = 'block';
}

function settingsRenderError(prefix, e) {
  const box = $('setDiffBox');
  if (!box) return;
  box.innerHTML = '<div class="set-diff"><div class="set-diff-hd">' + escapeHtml(prefix) + '</div>' +
    '<div class="set-diff-row"><div class="v">' + escapeHtml(e.message || String(e)) + '</div>' +
    (e.http ? '<div class="v">HTTP ' + escapeHtml(String(e.http)) + '</div>' : '') +
    '</div></div>';
  box.style.display = 'block';
}

async function settingsApplyModel() {
  /* v0.13.45：保存按钮不再有"未预览就禁用"的状态。09-27 第二轮报障（手机
     192.168.5.99 来访）日志里只有 GET /api/settings/models 与 /api/models，
     **连一条 preview/apply 请求都没有** ⇒ 用户是选完模型直接点保存，而 disabled
     按钮不派发 click ⇒ 委托收不到、toast 也不弹，全程零反馈，"保存不了"就此成立。
     所以：保存自己负责补齐预览（一次点击走完），按钮只在**保存进行中**才禁用。 */
  if (!SET_AGENT) {
    settingsRenderError('保存未执行', new Error('先在上面的列表里选一个 agent'));
    return toast('先选一个 agent', 'err');
  }
  /* v0.13.85：model 允许为空 —— 空值 = 下拉里的「撤销 hub 侧默认（不再注入 --model）」。
     此前这里用 `if (!model)` 直接 return + toast「先选一个模型」，于是那一项**永远存不了**
     （后端 validate_model("") 也会 400）。现在空值是合法动作，由后端 preview_clear/
     clear_model 处理。 */
  const model = setSelectedModel();
  if (!SET_DIFF || SET_DIFF.model !== model) {
    await settingsPreviewModel();              // 自动补预览；失败时里面已渲染 + toast
    if (!SET_DIFF || SET_DIFF.model !== model) return;
  }
  let pc = settingsPasscode();
  if (!pc) {
    /* 不再用 prompt 收口令：APP 内嵌 WebView 与部分手机浏览器会直接吞掉它
       （不弹窗、返回 null），那种环境里这一步等于静默失败。改成指路口令框。 */
    const el = $('setPasscode');
    if (el) el.focus();
    settingsRenderError('保存未执行', new Error(
      '请先在「设置口令」框里填入 HUB_PASSCODE（不再弹窗收集：手机/APP 会吞弹窗）'));
    return toast('请先填设置口令', 'err');
  }
  const btn = $('setApplyBtn');
  if (btn) btn.disabled = true;                // 只防连点，保存完立刻恢复
  try {
    const d = await api('/api/settings/model/apply', {
      method: 'POST',
      noToken: true,                          // 该端点只认 HUB_PASSCODE，别再索 TERM_TOKEN
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: SET_AGENT, model: model, passcode: pc })
    });
    lsSet('hub.passcode', pc);                // 通过了才缓存（与口令页同口径）
    clearPasscodeInput();
    if (d.mode === 'clear') {
      toast(SET_AGENT + '：已撤销 hub 侧默认模型（配置文件未改动）', 'ok');
    } else {
      const nb = (d.applied || []).length;
      toast(SET_AGENT + ' 模型已设为 ' + model + (nb ? '（备份 ' + nb + ' 份）' : '（未改配置文件）'), 'ok');
    }
    SET_AGENTS = [];
    SET_DIFF = null;
    await settingsModelsLoad(true);
    settingsRefreshMeta(model);               // 用重载后的真值刷新状态行（不是拿入参糊一个）
    settingsRenderApplied(d);
  } catch (e) {
    if (/401|口令/.test(e.message)) lsRemove('hub.passcode');
    clearPasscodeInput();
    settingsRenderError('保存失败', e);
    toast('保存失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;            // 保存完必须恢复可点，否则第二次就"点不动"
  }
}

/* 状态行渲染的**唯一出口**（选 agent 时与保存后刷新都走它，别各写一份文案）：
   v0.13.85 起把「这个 agent 的模型字段认什么」也写进来 —— 用户报障的一半是
   "改了不生效"，根因往往是"这个字段根本不吃 CCR ID / 这个 agent 没有配置文件"。 */
function settingsAgentMeta(a) {
  const el = $('setAgentMeta');
  if (!el || !a) return;
  const wm = SET_WRITE_MODE[a.write_mode || 'argv'] || SET_WRITE_MODE.argv;
  const parts = ['字段认：' + wm.label,
                 '当前：' + (a.current || '（未读到）'),
                 'hub 侧：' + (a.hub_model || '未设置')];
  if (a.argv) parts.push('注入口径：' + a.argv + ' <模型>');
  el.innerHTML = escapeHtml(parts.join('｜')) +
    '<div class="hint" style="margin-top:4px">' + escapeHtml(wm.hint) + '</div>' +
    ((a.files || []).length ? '<div class="hint">配置文件：' + escapeHtml(a.files.join(' / ')) + '</div>' : '');
}

function settingsRefreshMeta(model) {
  const a = SET_AGENTS.find(x => x.id === SET_AGENT);
  const el = $('setAgentMeta');
  if (!el) return;
  if (!a) { el.textContent = '已保存：' + model; return; }
  settingsAgentMeta(a);
}

/* ── 设置 → GitHub 子菜单（v0.13.42）──────────────────────────────────────
   把原来写死在 githubprojects.py 里的三样东西（API 基址 / token 文件 / 克隆落点）
   变成运行时可配。三条纪律与模型页同源：
     ① 不新增浮层（第三个设置页，与模型页并列在 main 里）；
     ② 按钮一律 data-settings-act 走同一委托，无 inline onclick；
     ③ key 是**只写**字段：页面只显示掩码与来源，输入框留空 = 不改动（清除走按钮）。
   读取顺序 Hub 设置 → 环境变量 → 内置默认，改完即时生效（服务端每次现读）。 */
let GH_VIEW = null;                 // GET /api/settings/github 的现值

async function settingsGithubLoad(force) {
  try {
    GH_VIEW = await api('/api/settings/github');
  } catch (e) {
    const st = $('ghStatus');
    if (st) { st.style.display = ''; st.textContent = 'GitHub 设置加载失败：' + e.message; }
    return;
  }
  const v = GH_VIEW || {};
  const put = (id, val) => { const el = $(id); if (el && (force || !el.value)) el.value = val || ''; };
  put('ghApiBase', v.api_base);
  put('ghGitHost', v.git_host);
  put('ghOwner', v.owner);
  put('ghCloneBase', v.clone_base);
  const src = v.sources || {};
  const srcLabel = { db: '本页设置', env: '环境变量', default: '内置默认' };
  const stamp = (id, key) => { const el = $(id); if (el) el.textContent = '（' + (srcLabel[src[key]] || '内置默认') + '）'; };
  stamp('ghApiBaseSrc', 'api_base');
  stamp('ghGitHostSrc', 'git_host');
  stamp('ghCloneBaseSrc', 'clone_base');
  const t = v.token || {};
  const ts = $('ghTokenState');
  if (ts) ts.textContent = t.set ? ('已设置 ' + (t.mask || '') + ' · ' + (t.type || '') + ' · 来源 ' +
    (srcLabel[t.source] || t.source || '?')) : '未设置';
  settingsGithubStatus();
}

function settingsGithubStatus(note) {
  const box = $('ghStatus');
  if (!box || !GH_VIEW) return;
  const v = GH_VIEW;
  const L = v.list || {};
  const rows = [
    ['远程地址', v.api_base],
    ['Git 主机', v.git_host || '（按地址派生）'],
    ['归属', v.owner || '（当前账号全部）'],
    ['克隆落点', v.clone_base + (v.clone_base_exists ? '' : '（尚不存在，首次克隆时创建）')],
    ['key', (v.token && v.token.set) ? ((v.token.mask || '') + ' · ' + (v.token.type || '') +
      ' · 来源 ' + (v.token.source || '?')) : '未设置'],
    ['远端清单', L.count + ' 个仓库' + (L.cached ? '（缓存 ' + L.age_s + 's，点刷新重拉）' : '（未拉取）')],
    ['口令门', v.writable ? '已启用（HUB_PASSCODE 已配）' : '未启用：服务端没配 HUB_PASSCODE，保存会被拒']
  ];
  box.innerHTML = rows.map(r => '<div><span class="k">' + escapeHtml(r[0]) + '：</span>' +
    '<span class="v">' + escapeHtml(r[1]) + '</span></div>').join('') +
    (note ? '<div style="margin-top:6px">' + escapeHtml(note) + '</div>' : '');
  box.style.display = '';
}

function ghPayload(extra) {
  const v = (id) => { const el = $(id); return el ? el.value.trim() : ''; };
  const p = { api_base: v('ghApiBase'), git_host: v('ghGitHost'), owner: v('ghOwner'),
    clone_base: v('ghCloneBase') };
  const tok = v('ghToken');
  if (tok) p.token = tok;                     // 留空 = 不改动
  if ($('ghWriteFiles') && $('ghWriteFiles').checked) p.write_files = true;
  return Object.assign(p, extra || {});
}

/* 与模型页同口径（v0.13.45）：不向 prompt 要口令 —— 手机/APP WebView 会吞弹窗，
   那种环境里"点了没反应"就是这么来的。缺口令就指回页面上的口令框并说明原因。 */
function ghRenderError(prefix, e) {
  const box = $('ghStatus');
  if (!box) return;
  box.innerHTML = '<div class="v">' + escapeHtml(prefix + '：' + (e.message || String(e))) +
    (e.http ? '（HTTP ' + escapeHtml(String(e.http)) + '）' : '') + '</div>';
  box.style.display = '';
}

async function ghAskPasscode(what) {
  let pc = settingsPasscode();
  if (!pc) {
    const el = $('ghPasscode');
    if (el) el.focus();
    ghRenderError(what + '未执行', new Error(
      '请先在「设置口令」框里填入 HUB_PASSCODE（不再弹窗收集：手机/APP 会吞弹窗）'));
    return '';
  }
  return pc;
}

function ghPost(path, body) {
  /* noToken：GitHub 四个写端点与模型保存同口径，只认 HUB_PASSCODE（已登记在
     writeauth.EXEMPT_PREFIXES），索 TERM_TOKEN 只会多一个无关弹窗。 */
  return api(path, { method: 'POST', noToken: true,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
}

async function settingsGithubTest() {
  let pc = await ghAskPasscode('测试连接');
  if (!pc) return;
  const p = ghPayload({ passcode: pc, api_base: $('ghApiBase').value.trim() || undefined });
  try {
    const d = await ghPost('/api/settings/github/test', p);
    lsSet('hub.passcode', pc);
    if (!d.ok) return settingsGithubStatus('试连失败：' + (d.error || '未知'));
    const rate = d.rate || {};
    settingsGithubStatus('试连成功：' + (d.login || '?') + ' · key ' + (d.token_type || '') +
      ' ' + (d.token_mask || '') + ' · 配额 ' + (rate.remaining === undefined ? '?' :
        rate.remaining + '/' + rate.limit) + ' · 列仓库 ' + (d.repo_probe && d.repo_probe.ok ? '可用' : '不可用') +
      ' · ' + (d.took_ms || 0) + 'ms');
  } catch (e) {
    if (/401|口令/.test(e.message)) lsRemove('hub.passcode');
    toast('试连失败：' + e.message, 'err');
  }
}

async function settingsGithubApply() {
  let pc = await ghAskPasscode('保存 GitHub 设置');
  if (!pc) return;
  const p = ghPayload({ passcode: pc });
  try {
    const d = await ghPost('/api/settings/github/apply', p);
    lsSet('hub.passcode', pc);
    const box = $('ghDiff');
    if (box) {
      const ch = d.changes || [], fw = d.files_written || [];
      box.innerHTML = '<div class="set-diff"><div class="set-diff-hd">已保存 ' + ch.length +
        ' 项' + (fw.length ? '（回写 ' + fw.length + ' 个文件）' : '（仅 Hub 侧，未落文件）') + '</div>' +
        ch.map(c => '<div class="set-diff-row"><div class="k">' + escapeHtml(c.key) + '</div>' +
          '<div class="v">' + escapeHtml(c.from) + ' → ' + escapeHtml(c.to) + '</div></div>').join('') +
        (d.backups || []).map(b => '<div class="set-diff-row"><div class="v">备份 ' +
          escapeHtml(b) + '</div></div>').join('') + '</div>';
      box.style.display = '';
    }
    $('ghToken').value = '';
    await settingsGithubLoad(true);
    toast('GitHub 设置已保存（' + (d.applied || []).join('、') + '）', 'ok');
  } catch (e) {
    if (/401|口令/.test(e.message)) lsRemove('hub.passcode');
    toast('保存失败：' + e.message, 'err');
  }
}

async function settingsGithubClear() {
  if (!confirm('清除 Hub 侧的 GitHub 设置（地址/key/归属/落点），回落到环境变量与内置默认？')) return;
  let pc = await ghAskPasscode('清除 GitHub 设置');
  if (!pc) return;
  try {
    const d = await ghPost('/api/settings/github/clear', { passcode: pc });
    lsSet('hub.passcode', pc);
    await settingsGithubLoad(true);
    toast('已清除：' + ((d.cleared || []).join('、') || '（本来就是空的）'), 'ok');
  } catch (e) {
    if (/401|口令/.test(e.message)) lsRemove('hub.passcode');
    toast('清除失败：' + e.message, 'err');
  }
}

async function settingsGithubRefresh() {
  let pc = await ghAskPasscode('刷新远端清单');
  if (!pc) return;
  try {
    const d = await ghPost('/api/settings/github/refresh', { passcode: pc });
    lsSet('hub.passcode', pc);
    await settingsGithubLoad(true);
    if (d.errors && d.errors.length) toast('重拉完成但有告警：' + d.errors[0], 'err');
    else toast('已重拉远端清单：' + d.count + ' 个仓库', 'ok');
  } catch (e) {
    if (/401|口令/.test(e.message)) lsRemove('hub.passcode');
    toast('刷新失败：' + e.message, 'err');
  }
}

/* ── 设置 → 日志子菜单（v0.13.46）────────────────────────────────────────
   数据源 GET /api/hublog：journald 服务日志 + profile_events 操作事件，合并按时间倒序。
   三条纪律与另三个设置页同源：① 无浮层（正文出页，筛选控件全在页内）；
   ② 按钮一律 data-settings-act 走同一委托，无 inline onclick；
   ③ **鉴权失败不弹 prompt**（手机/APP WebView 吞弹窗 ⇒ 点了没反应）：原因写进
      页内 #logStats 并 focus 口令框，与模型/GitHub 页 v0.13.45 的口径一致。
   后端按写方法鉴权（GET 也要口令），故 token 由本页自己带（api() 只给写方法带）。
   var 声明而非 let：go()（06 分片顶层就会跑）可能经 settingsLogsLoad 读到它，
   与 setModelPageLoaded 同一条 TDZ 纪律。 */
var LOG_LAST = null;      // 最近一次结果：复制/导出复用，不再为同一次查看打两次后端

function settingsLogsParams() {
  const p = new URLSearchParams();
  const v = id => { const el = $(id); return el && el.value ? el.value : ''; };
  if (v('logSource')) p.set('source', v('logSource'));
  if (v('logLevel')) p.set('level', v('logLevel'));
  if (v('logWindow')) p.set('window', v('logWindow'));
  if (v('logQ')) p.set('q', v('logQ'));
  if (v('logSubject')) p.set('subject', v('logSubject'));
  p.set('limit', '200');
  return p;
}

async function settingsLogsLoad() {
  const body = $('logBody');
  if (!body) return;
  boxBusy('logBody', '加载中…');
  const pc = ($('logPasscode') && $('logPasscode').value.trim()) || settingsPasscode();
  const opt = pc ? { headers: { 'x-hub-token': pc } } : {};
  settingsLogsSyncSubject();
  try {
    const d = await api('/api/hublog?' + settingsLogsParams().toString(), opt);
    LOG_LAST = d;
    settingsLogsRender(d);
  } catch (e) {
    LOG_LAST = null;
    settingsLogsError(e);
  }
}

/* subject 下拉（三中心检索的 subject 枚举，后端随包给清单）：只填一次，
   不覆盖用户已选值；服务日志那一路没有 subject ⇒ 选中时把它藏掉，避免「选了没反应」。 */
function settingsLogsSyncSubject() {
  const sel = $('logSubject'), src = $('logSource');
  if (sel && src) sel.style.display = (src.value === 'journal') ? 'none' : '';
}

function settingsLogsFillSubjects(d) {
  const sel = $('logSubject');
  if (!sel) return;
  const list = (d && d.subjects) || [];
  if (!list.length || sel.options.length > 1) return;
  sel.innerHTML = '<option value="">全部 subject</option>' +
    list.map(s => '<option>' + escapeHtml(s) + '</option>').join('');
}

function settingsLogsRender(d) {
  const body = $('logBody');
  if (!body) return;
  const es = (d && d.entries) || [];
  settingsLogsFillSubjects(d);
  if (!es.length) {
    body.innerHTML = '<div class="hint">该筛选下没有条目 —— 可放宽时间窗、换成「全部来源」，' +
      '或确认单元名（服务端未走 systemd 时 journald 一路为空）。</div>';
  } else {
    /* 复用 .set-diff/.set-diff-row 既有骨架（不新增 CSS 类与色 token）；
       级别只在行头的 .k 上着色，error=--danger / warn=--warn（:root 既有）。 */
    body.innerHTML = '<div class="set-diff">' + es.map(e => {
      const lv = e.level === 'error' ? 'ERR' : e.level === 'warn' ? 'WARN' : 'INFO';
      const col = e.level === 'error' ? 'var(--danger)' : e.level === 'warn' ? 'var(--warn)' : 'var(--muted)';
      return '<div class="set-diff-row"><div class="k" style="color:' + col + '">' +
        escapeHtml((e.ts || '').slice(0, 19) + '  ' + lv + '  ' + e.src + ':' + (e.tag || '')) +
        '</div><div class="v">' + escapeHtml(e.msg || '') + '</div></div>';
    }).join('') + '</div>';
  }
  settingsLogsStats(d);
}

function settingsLogsStats(d) {
  const box = $('logStats');
  if (!box) return;
  const s = (d && d.stats) || {};
  const rows = [
    ['条数', (d && d.count != null ? d.count : 0) + (d && d.truncated ? '（已到上限，可缩小时间窗或加关键字）' : '')],
    ['错误 / 警告', (s.error || 0) + ' / ' + (s.warn || 0)],
    ['来源', ((d && d.sources) || []).map(x => x.label + ' ' + x.count +
      (x.ok ? '' : '（不可用：' + (x.note || '') + ')')).join('；') || '—'],
  ];
  box.innerHTML = rows.map(r => '<div><span class="k">' + escapeHtml(r[0]) + '：</span>' +
    '<span class="v">' + escapeHtml(String(r[1])) + '</span></div>').join('');
  box.style.display = '';
}

function settingsLogsError(e) {
  const box = $('logStats');
  const body = $('logBody');
  const msg = (e && e.message) || String(e);
  if (/401|口令|缺少凭据/.test(msg)) lsRemove('hub.passcode');   // 存量口令失效：清掉再问，不循环
  if (body) body.innerHTML = '';
  if (box) {
    box.innerHTML = '<div class="v" style="color:var(--danger)">读取失败：' + escapeHtml(msg) +
      (e && e.http ? '（HTTP ' + escapeHtml(String(e.http)) + '）' : '') + '</div>' +
      '<div class="hint">在「设置口令」框填 HUB_PASSCODE 再点刷新（不会弹窗）。' +
      'HTTP 503 表示服务端没配 HUB_PASSCODE，填什么都没用。</div>';
    box.style.display = '';
  }
  const el = $('logPasscode');
  if (el) el.focus();
  toast('日志读取失败：' + msg, 'err');
}

function settingsLogsText() {
  if (!LOG_LAST || !LOG_LAST.entries || !LOG_LAST.entries.length) return '';
  return LOG_LAST.entries.map(e => (e.ts || '') + ' [' + String(e.level || '').toUpperCase() + '] ' +
    e.src + ':' + (e.tag || '') + ' ' + (e.msg || '')).join('\n');
}

/* 复制的两级兜底（09-27 实测）：navigator.clipboard 只在**安全上下文**可用，
   而手机走的是 http://192.168.x.x:3102（非 https ⇒ 不是安全上下文）⇒ 直接抛
   "Write permission denied"。所以先 clipboard、再 execCommand，两级都失败才
   明确告诉用户改用「导出 .log」，而不是一句"复制失败"让人以为是页面坏了。
   临时 textarea 挂在视口外 + pointer-events:none，不遮挡、不截点击。 */
async function settingsLogsCopyText(t) {
  try {
    if (navigator.clipboard && navigator.clipboard.writeText) {
      await navigator.clipboard.writeText(t);
      return true;
    }
  } catch (e) { /* 落到 execCommand 兜底 */ }
  try {
    const ta = document.createElement('textarea');
    ta.value = t;
    ta.setAttribute('readonly', '');
    ta.style.cssText = 'position:fixed;top:-1000px;opacity:0;pointer-events:none';
    document.body.appendChild(ta);
    ta.select();
    const ok = document.execCommand('copy');
    ta.remove();
    return !!ok;
  } catch (e) {
    return false;
  }
}

async function settingsLogsCopy() {
  const t = settingsLogsText();
  if (!t) return toast('先刷新出日志再复制', 'err');
  if (await settingsLogsCopyText(t)) {
    toast('已复制 ' + LOG_LAST.entries.length + ' 条', 'ok');
  } else {
    toast('复制失败：浏览器不给剪贴板权限（局域网 http 地址常见）——请改用「导出 .log」', 'err');
  }
}

function settingsLogsExport() {
  const t = settingsLogsText();
  if (!t) return toast('先刷新出日志再导出', 'err');
  /* 走 Blob 而不是 format=text 端点：已在页里的结果不必再打一次后端，
     也省得把口令再塞进 URL（?token= 会被 uvicorn 记进访问行）。 */
  const blob = new Blob([t], { type: 'text/plain;charset=utf-8' });
  const a = document.createElement('a');
  a.href = URL.createObjectURL(blob);
  a.download = 'agent-hub-' + ((LOG_LAST.query && LOG_LAST.query.source) || 'all') + '-' +
    new Date().toISOString().slice(0, 19).replace(/[:T]/g, '') + '.log';
  document.body.appendChild(a);
  a.click();
  a.remove();
  setTimeout(() => URL.revokeObjectURL(a.href), 1000);
  toast('已导出 ' + LOG_LAST.entries.length + ' 条', 'ok');
}

/* 三个设置正文页的**唯一**委托出口：选 agent / 预览 / 保存 / GitHub / 口令 / 日志 */
function settingsDelegates() {
  SET_PAGE_IDS.forEach(id => {
    const page = document.getElementById(id);
    if (!page) return;
    page.addEventListener('click', e => {
    const ag = e.target.closest('[data-settings-agent]');
    if (ag) {
      if (ag.getAttribute('aria-disabled') === 'true') {
        return toast(ag.getAttribute('title') || '该 agent 不支持统一设置模型', 'err');
      }
      return settingsPickAgent(ag.dataset.settingsAgent);
    }
    const act = e.target.closest('[data-settings-act]');
    if (act && act.dataset.settingsAct === 'preview') return settingsPreviewModel();
    if (act && act.dataset.settingsAct === 'apply') return settingsApplyModel();
    if (act) {
      const a = act.dataset.settingsAct;
      if (a === 'gh-test') return settingsGithubTest();
      if (a === 'gh-apply') return settingsGithubApply();
      if (a === 'gh-clear') return settingsGithubClear();
      if (a === 'gh-refresh') return settingsGithubRefresh();
      /* 终端口令页：原先是 inline onclick，随抽屉一起改走委托（同一个出口） */
      if (a === 'token-view') return settingsViewToken();
      if (a === 'token-clear') return settingsClearPasscode();
      if (a === 'token-copy') return settingsCopyToken();
      if (a === 'token-apply') return settingsApplyToken();
      if (a === 'token-toggle') return settingsToggleShow();
      /* 日志页（v0.13.46）：刷新 / 复制 / 导出 */
      if (a === 'log-refresh') return settingsLogsLoad();
      if (a === 'log-copy') return settingsLogsCopy();
      if (a === 'log-export') return settingsLogsExport();
    }
    });
    /* 筛选下拉变了就重拉（关键字框要等敲完 ⇒ 走「刷新」按钮，不逐字符打后端）。
       change 也挂在同一个委托出口里，不另起监听点。 */
    page.addEventListener('change', e => {
      const id = e.target && e.target.id;
      if (id === 'logSource' || id === 'logLevel' || id === 'logWindow' ||
          id === 'logSubject') return settingsLogsLoad();
    });
  });
}
/* 注意：settingsDelegates() 的**调用**放在启动尾（06 分片），不在这里立即执行 ——
   它会经 api() 一路读到 04 分片声明的 `let term`，而 TDZ 闸门（tests/test_tdz_order.py）
   判的是"调用点早于声明点"：在 01 分片里直接调用必然踩线。注册动作本身没有时序要求
   （只是挂监听），放在启动尾零成本。 */
async function delAgent(id) {
  if (!confirm('删除自定义 Agent: ' + id + '？（仅注销视图，不停止其自身服务）')) return;
  try { await api('/api/agents/' + encodeURIComponent(id), { method: 'DELETE' }); toast('已删除', 'ok'); loadAgents(); }
  catch (e) { toast(e.message, 'err'); }
}

function openRegister() { $('regMask').classList.add('on'); }
function closeRegister() { $('regMask').classList.remove('on'); $('regDetect').style.display = 'none'; }
async function detectDir() {
  const dir = $('regDir').value.trim();
  if (!dir) return toast('请输入目录', 'err');
  try {
    const d = await api('/api/agents/detect', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ dir: dir }) });
    $('regDetect').style.display = 'block';
    $('regDetect').textContent = JSON.stringify(d, null, 2);
  } catch (e) { toast(e.message, 'err'); }
}
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
   cloudcli 独立子菜单：无终端卡（纯服务型），默认形态 = embed（原生界面）+ 项目直达面板。 */
function defaultModeOf(a) {
  const es = (a && a.entries) || [];
  const has = t => es.some(e => e.type === t);
  if (a && a.id === 'cloudcli') return 'embed';
  if (a && a.kind === 'agent' && has('term')) return 'term';
  if (has('embed')) return 'embed';
  if (has('term')) return 'term';
  return 'chat';
}
function gotoChat(id, mode) {
  chatPick = id;
  lsSet('hub.chat.pick', id);  // T9：记忆上次实体
  const a = entityById(id);
  chatMode = mode || lsGet(MODE_KEY + id) || defaultModeOf(a) || 'chat';
  if (mode) lsSet(MODE_KEY + id, mode);   // 只有点名了形态才算偏好；推导出来的不写盘
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
  renderChatSide();
}

function applyChatMode() {
  const a = entityById(chatPick);
  renderModeBar(a);
  const en = $('chatEntName');
  if (en) en.textContent = a ? a.name : '';
  if (!a) return;   // 实体已被删除（localStorage 里留着旧 pick）：保持默认面板，不再往下猜模式
  const es = a.entries || [];
  // 形态对该实体不可用（没选过、或 entry 被删/改）：回落默认形态。
  // 这是**推导**，绝不写盘 —— 一写就把默认固化成偏好，改默认也救不回来。
  if (!es.some(e => e.type === chatMode)) chatMode = defaultModeOf(a) || 'chat';
  // 面板头互切按钮：菜单行内的动作图标自 v0.12.3 起 display:none、模式 tab 也已停用，
  // 站内不留这条出口，embed 与 term 就互相锁死（点进哪个就再也切不到另一个）。
  const toTerm = $('embedToTerm'), toEmbed = $('termToEmbed');
  if (toTerm) toTerm.style.display = es.some(e => e.type === 'term') ? '' : 'none';
  if (toEmbed) toEmbed.style.display = es.some(e => e.type === 'embed') ? '' : 'none';
  $('embedPane').classList.toggle('on', chatMode === 'embed');
  $('termPane').classList.toggle('on', chatMode === 'term');
  $('chatPane').classList.toggle('on', chatMode === 'chat');
  if (chatMode === 'embed') {
    const e = (a.entries || []).find(x => x.type === 'embed');
    const url = e ? lanUrl(e.url) : '';
    $('embedTitle').textContent = a.name;   // 用户 09-20：删掉「原生界面」后缀（宽屏要把地址并进同一行，标题越短越好）
    // 地址行：#embedUrlHint 现为 <button><span>URL</span><svg/></button>，直接写 textContent 会把图标抹掉
    const hintEl = $('embedUrlHint');
    const hintTxt = hintEl ? hintEl.querySelector('span') : null;
    if (hintTxt) hintTxt.textContent = url; else if (hintEl) hintEl.textContent = url;
    const f = $('embedFrame');
    if (f.dataset.src !== url) { f.src = url; f.dataset.src = url; }
    probeEmbed(url);
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
  chatMode = m;
  lsSet(MODE_KEY + chatPick, m);  // T9：按实体记忆模式（这里是用户点名切换 ⇒ 算真偏好）
  applyChatMode();
}

/* 嵌入存活探测：no-cors fetch 失败=目标端口无响应 → 覆盖层引导切换 */
async function probeEmbed(url) {
  const pane = $('embedPane');
  // 存活信号只保留"死时"的那一份：失败会有整屏覆盖层（含切模式/重试按钮），
  // 活着时行内再挂一句「可达」是纯噪声 —— 用户 09-20 要求删掉该字样。
  let dead = pane.querySelector('.embed-dead');
  if (dead) dead.remove();
  try {
    await Promise.race([
      fetch(url, { mode: 'no-cors', cache: 'no-store' }),
      new Promise((_, rej) => setTimeout(() => rej(new Error('timeout')), 4000))
    ]);
  } catch (e) {
    dead = document.createElement('div');
    dead.className = 'embed-dead';
    dead.style.cssText = 'position:absolute;inset:0;display:flex;flex-direction:column;gap:12px;align-items:center;justify-content:center;background:var(--mask);z-index:5';
    dead.innerHTML = '<div style="font-size:var(--fs-base);color:var(--danger-text)">目标界面未响应（' + escapeHtml(url) + '）</div>' +
      '<div style="display:flex;gap:8px"><button class="btn sm" onclick="switchMode(\'chat\')">改用对话模式</button>' +
      '<button class="btn sm ghost" onclick="switchMode(\'term\')">改用终端</button>' +
      '<button class="btn sm ghost" onclick="embedRefresh()">重试嵌入</button></div>';
    if (getComputedStyle(pane).position === 'static') pane.style.position = 'relative';
    pane.appendChild(dead);
  }
}

async function embedRefresh() { const f = $('embedFrame'); f.src = f.src; probeEmbed(f.dataset.src || ''); }
function embedNewTab() { const a = entityById(chatPick); const e = (a.entries || []).find(x => x.type === 'embed'); if (e) window.open(lanUrl(e.url), '_blank'); }
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
/* 尺寸意图（B2）：claim = 「我是主人，按我的尺寸来」；update = 「我只是几何变了」。
   服务端对非所有者的 update 一律静默忽略 ⇒ 后台手机端偷不走 PTY 尺寸。
   force（连接建立 / 重连 / 点芯片 / 自愈）一律 claim：那是用户主动动作，
   也保证至少有一端能拿到所有权（否则谁都只发 update，尺寸会永远卡在 80×24）。 */
function termSizeIntent(force) {
  if (force) return 'claim';
  if (document.hidden) return 'update';   // 后台标签/切走的手机：不夺权
  return termSizeClaimed ? 'claim' : 'update';
}
function termSendSize(cols, rows, force) {
  if (!termWs || termWs.readyState !== 1) return;
  const intent = termSizeIntent(force);
  try {
    termWs.send(JSON.stringify({ type: 'resize', intent: intent, cols: cols, rows: rows }));
    if (intent === 'claim') termSizeClaimed = true;
  } catch (e) { /* 正在关：交给 onclose */ }
}

/* 几何变化走 debounce，force 走立刻。
   debounce 只压几何变化：连拖窗口时中间那些尺寸一个都不必真的下发，
   ioctl + 整屏 refresh 才是最贵的那部分。 */
let termRepaintTimer = null;
function termRepaint(force) {
  if (!term || !termFit) return;
  if (!termVisible()) { termPaintedAt = ''; return; }
  if (force) {
    if (termRepaintTimer) { clearTimeout(termRepaintTimer); termRepaintTimer = null; }
    termRepaintNow(true);
    return;
  }
  if (termRepaintTimer) clearTimeout(termRepaintTimer);
  termRepaintTimer = setTimeout(() => { termRepaintTimer = null; termRepaintNow(false); },
                                TERM_RESIZE_DEBOUNCE_MS);
}
function termRepaintNow(force) {
  try { termFit.fit(); } catch (e) {}
  const size = term.cols + 'x' + term.rows;
  // pty 那边可能被别的客户端改过尺寸，重连时（force）无条件报一次当前行列
  if ((size !== termPaintedAt || force)) termSendSize(term.cols, term.rows, force);
  termPaintedAt = size;
  try { term.refresh(0, term.rows - 1); } catch (e) {}
  termHealNow();   // 隐藏期间攒下的「回放只剩半屏」在这里补做
}
document.addEventListener('visibilitychange', () => { if (!document.hidden) termRepaint(); });

/* ── 心跳 / 看门狗（每个 socket 最多一条心跳定时器）────────────────────────────
   半开连接（手机切网络的典型形态）表现为 TCP 还在、WS readyState 仍是 1、但永不回数据：
   只有应用层往返能识破它，所以判据是「回执账本」而不是 readyState。 */
function termSockOpen(ws) { return !!ws && ws === termWs && ws.readyState === 1; }
function termIsHb(text) {
  if (!text || text.charCodeAt(0) !== 123) return false;   // 不以 '{' 开头的帧不去解析，省掉每帧 try
  try { return JSON.parse(text).type === 'hb'; } catch (e) { return false; }
}
function termHbStop() {
  if (termHbTimer) { clearInterval(termHbTimer); termHbTimer = null; }
  if (termHbDeadline) { clearTimeout(termHbDeadline); termHbDeadline = null; }
  termHbLastReply = 0; termHbLastSend = 0; termHbBaseline = 0;
}
function termHbSend(ws) {
  termHbLastSend = Date.now();
  try { ws.send('{"type":"hb"}'); } catch (e) { /* 正在关：交给 onclose */ }
}
/* 两个定时器各司其职，而不是一个轮询 tick：发帧按 15s 节流，判死按「回执 +30s」精确定点。
   后台标签页会被浏览器节流，所以判死不能只靠这个定时器：另有一条按账本补算的唤醒路（termLinkWake）。 */
function termHbArm() {
  if (termHbDeadline) clearTimeout(termHbDeadline);
  termHbDeadline = setTimeout(termHbTimeout, TERM_HB_DEAD_MS);
}
function termHbStart(ws) {
  termHbStop();                       // 先收上一条线的，绝不留下第二个定时器
  termHbBaseline = Date.now();        // 还没有回执时，以「本连接建立时刻」为账本基准
  termHbSend(ws);                     // 连上立刻首发：一次往返即确认链路，也尽早把画面判活
  termHbArm();
  termHbTimer = setInterval(termHbSendTick, TERM_HB_SEND_MS);
}
function termHbSendTick() {
  const ws = termWs;
  if (!ws) { termHbStop(); return; }                     // 已解绑：心跳自己收口，不等 close 事件
  if (ws.readyState !== 1) return;                       // closing/closed：由 onclose 进重连
  termHbSend(ws);
}
function termHbTimeout() {
  termHbDeadline = null;
  const ws = termWs;
  if (!ws) { return; }
  if (ws.readyState !== 1) return;                       // 已经断了：close 事件在负责重连
  if (Date.now() - (termHbLastReply || termHbBaseline) < TERM_HB_DEAD_MS) { termHbArm(); return; }  // 回执刚来过：重新武装，不误杀
  termHbFail(ws);
}
function termHbReply(ws) {
  if (!termSockOpen(ws)) return;
  termHbLastReply = Date.now();
  termRcAttempt = 0;        // 退避只在这里清零：真往返过 = 链路确实通了
  termInputWarned = false;
  termToastClear();         // 「正在重连」那条提示到此结案
  termHbArm();              // 每一次回执都把 30s 看门狗拨回原点
}
function termHbFail(ws) {
  termHbStop();
  termNotice('[心跳超时：' + Math.round(TERM_HB_DEAD_MS / 1000) + 's 无回执，判定链路半开——主动断开重连]');
  try { ws.close(); } catch (e) {}
  /* 半开时浏览器可能迟迟不派发 onclose，重连不能干等它；termRcTimer 有则不重复排 */
  termScheduleReconnect(ws);
}

/* ── 退避重连（全仓唯一待触发重连；任何新连接/解绑都先作废它）─────────────────── */
function termRcCancel() { if (termRcTimer) { clearTimeout(termRcTimer); termRcTimer = null; } }

function termScheduleReconnect(ws) {
  if (ws && termWs !== ws) return;        // 迟到的旧事件不驱动重连
  if (termRcTimer) return;                // 同一条线只许排一次（防重入：visibility/online/close 同时进来）
  if (!termSid) return;                   // 已解绑 / 会话已结束 ⇒ 不复活用户已经离开的会话
  const delay = TERM_RC_DELAYS[Math.min(termRcAttempt, TERM_RC_DELAYS.length - 1)];
  termRcAttempt++;
  termNotice('[连接中断——自动重连中，第 ' + termRcAttempt + ' 次（' + Math.round(delay / 1000) + 's 后）]');
  termToast('终端连接中断，正在重连（第 ' + termRcAttempt + ' 次）', 'err');
  termRcTimer = setTimeout(termRcFire, delay);
}

function termRcFire() {
  termRcTimer = null;
  if (!termSid) return;                                   // 等待期间用户已解绑/切实体
  const ws = termWs;
  if (ws && ws.readyState === 1 && termHbTimer) return;   // 期间已被别的入口接活
  /* 重连前先向服务端对一次账。必须对账的实测理由：4404（会话不存在）是服务端在 accept
     之前 close 的，浏览器拿不到那个业务码，只能看到握手被拒→1006（与“链路断了”同签名）。
     不对账就会对着一个已经不存在的 sid 无限重连——正是「陈旧的重试环复活用户已经离开的会话」。
     拿不到清单（live=null，接口错）时不下结论，照旧重连；只有服务端明确说「清单里没它了」才停。 */
  termRefreshList().then(live => {
    if (!termSid) {                     // 对账结果：会话已不在清单（termRefreshList 顺手解了绑）
      termToast('终端会话已结束，请重新打开', 'err');   // 那条路径原本只往缓冲区写一行，补上可见失败
      return;
    }
    termConnect(termSid, termSidAgent, { reconnect: true });
  });
}

/* 回前台 / 网络恢复：iOS 与安卓后台会冻掉定时器，切回前台那一刻按账本补算一次。
   只走唯一的重连出口，且靠 termRcTimer 去重 ⇒ 不会开出第二个 socket。 */
function termLinkWake() {
  if (!termSid) return;
  const ws = termWs;
  if (!ws || ws.readyState === 0) return;                 // 无绑定 / 连接在飞：交给它自己的事件
  if (ws.readyState !== 1) { termScheduleReconnect(ws); return; }
  if (termHbTimer && Date.now() - (termHbLastReply || termHbBaseline) >= TERM_HB_DEAD_MS) termHbFail(ws);
}
window.addEventListener('online', termLinkWake);
document.addEventListener('visibilitychange', () => { if (!document.hidden) termLinkWake(); });

/* 连接中反馈：终端区顶部一条 2px 走马灯。不往缓冲区写字，回放到了自然被盖掉。 */
function termConnecting(on, ws) {
  if (ws && termWs !== ws) return;   // 迟到的旧连接事件不算数
  const pane = $('termPane');
  if (pane) pane.classList.toggle('connecting', !!on);
}

/* ring 是「最近 64KB 原始输出」，对整屏 TUI 往往只剩最后几帧增量：回放完屏幕大半是空的，
   而同尺寸 resize 不会让内核发 SIGWINCH ⇒ TUI 永不重画，白块就一直挂着
   （CDP 实测：换标签 / 离开页面回来后 26 行里 21 行空白，挪一次尺寸立刻满屏）。
   这里在回放之后确认画面确实空了，才把 pty 尺寸挪一行再挪回来逼它重画整屏。
   回放落在隐藏态时先挂着（termHealPending），等 termRepaint() 在重新可见时补做。 */
let termHealPending = false;
function termHealBlank() { termHealPending = true; setTimeout(termHealNow, 250); }

/* ── 终端链接带外通道：登录(auth_url) / 页面(page_url) 两路（v0.13.87）──────
   场景（v0.13.64 原意）：手机上跑 `claude setup-token` / 任何 OAuth 登录，登录 URL
   只出现在 pty 输出里。窄屏上那串 URL 要靠肉眼抄 —— 又长又断行，抄错一个字符就白跑。
   这里把 URL 提成**可点按钮**：一眼可按，按完直接开浏览器。

   ★ v0.13.87 改了两件事（用户报障「嵌入式终端总是提示登录链接，实际所有终端我都
     不需要登录」）：
     ① **文案不许再一律叫「登录」**。旧实现只有一条通道、文案写死「检测到登录链接」，
        于是 agent 印一条文档/仓库地址（`See https://github.com/openai/codex`）也弹
        「检测到登录链接」——纯误报。现在两路各说各话：真相来源是服务端的 `type`
        字段，前端**不猜**（猜 = 第二份会漂的真相）。连画在终端里的那行标记也跟着分：
        登录路 `[登录链接]`，页面路 `[链接]`。同理不再把 URL 写死成洋红/紫色（那颜色
        本身就是「登录」的暗示），统一次要色。
     ② 两条通道各自去重（服务端按 `通道|URL` 分键），故这里不需要额外状态。
   为什么默认不自动开：现代浏览器只允许「用户手势内」window.open，程序性调用会被拦成
   弹窗 ⇒ 用户看到的是"点了没反应"，比不给按钮更糟。服务端只在**登录路**且输出里
   明说 "press enter to open" 之类时才给 auto=true；页面路恒为 false —— 普通链接
   绝不该替用户自动弹浏览器。 */
function termLinkAnno(kind, url, auto) {
  const safe = String(url || '');
  if (!/^https?:\/\//i.test(safe)) return;     // 双保险：只放行 http/https
  const isAuth = kind === 'auth';
  /* 画面上也留一行可复制的纯文本：按钮被拦、或用户想手动拷时仍有出路。
     刻意用 OSC 8 之外的方式（普通可见文本）—— 它要"看得见"，不是隐藏超链接。
     用 [90m（次要色）而非旧版的 [95m（洋红）：颜色不该暗示"这是登录"。 */
  const note = '\r\n\x1b[90m[' + (isAuth ? '登录链接' : '链接') + '] ' + safe + '\x1b[0m\r\n';
  try { if (term) term.write(note); } catch (e) {}
  const open = () => { try { window.open(safe, '_blank', 'noopener'); } catch (e) { toast('请手动复制上面的链接', 'err'); } };
  /* toast() 的签名是 (msg, cls)，**没有** onClick 参数（01-core-boot.js:283）——
     早先这里多传了个 open 当第三参，函数会静默忽略 ⇒ 按钮点不动。这里显式
     把 toast 节点改成可点，而不是给 toast() 硬加参数（那会波及其余 40+ 调用方）。 */
  try {
    const el = toast(isAuth ? '检测到登录链接，点此打开 ↗' : '检测到链接，点此打开 ↗', 'info');
    if (el) {
      el.style.cursor = 'pointer';
      el.style.textDecoration = 'underline';
      el.addEventListener('click', open);
      el.title = safe;
    }
  } catch (e) { open(); }
  if (auto) open();
}
/* 兼容别名：v0.13.64 的旧名。仓内已无调用方（帧处理改走 termLinkAnno），
   保留它是给「端侧仍缓存着旧 hub.js 片段」留一条不断链的路。 */
function termAuthUrl(url, auto) { termLinkAnno('auth', url, auto); }
/* 数「视口内」的空行 —— 必须从 viewportY 起算，不能从缓冲区第 0 行起算。
   buffer.active.getLine(0) 是**绝对坐标**，即 scrollback 的最老一行；
   一旦屏上有历史（输出超过一屏、或用户滚动过），0..rows-1 读到的是早滚出屏幕的旧行，
   与用户此刻看到的画面无关。旧写法在这里空耗两个后果：
     · 画面明明全白，绝对行却有内容 ⇒ blank 偏低 ⇒ 自愈被误抑制（P2-8 的实际症状）
     · 反之画面有内容但顶部历史是空的 ⇒ 会对着不白屏的 pty 乱发 resize 打扰它
   抽成函数是为了能在真浏览器里取证（tests/verify_term_heal_viewport.py 会
   同时算新旧两种口径，红绿放在同一份产物里对比）。 */
function termViewportBlankRows(t) {
  const b = t.buffer.active;
  const rows = t.rows;
  const top = (typeof b.viewportY === 'number' && b.viewportY >= 0) ? b.viewportY : 0;
  let blank = 0;
  for (let i = 0; i < rows; i++) {
    const l = b.getLine(top + i);
    if (!l || !l.translateToString(true).trim()) blank++;
  }
  return blank;
}

function termHealNow() {
  if (!termHealPending || !term || !termWs || termWs.readyState !== 1 || !termVisible()) return;
  termHealPending = false;
  const blank = termViewportBlankRows(term);
  if (blank * 3 < term.rows * 2) return;   // 画面有内容就别去打扰 pty
  const c = term.cols, r = term.rows;
  /* 自愈是**主动干预**（不是被动的几何变化），两帧都必须 claim：
     一是它得能真的改到尺寸，二是顺手把所有权收回本端——白块往往正是被别的端
     改小尺寸压出来的。它有自己的 120ms 定时器，不经过 resize debounce。 */
  termSendSize(c, Math.max(1, r - 1), true);
  setTimeout(() => termSendSize(c, r, true), 120);
}

/* ── 渲染器选择 ────────────────────────────────────────────────────────────────
   实测（@xterm/xterm 6.0.0，grep vendor/xterm.js）：核心只内置 DomRenderer，
   `_createRenderer()` 直接 `createInstance(DomRenderer, ...)` —— 每个字符格子是一个
   DOM <span>，80×24 就 1920 个节点，scrollback 5000 行上限下最多约 40 万节点在 DOM 里。
   原生终端是 GPU 画的位图，这是「跟系统终端差很远」的首要技术原因。
   WebGL 渲染器是**独立包**，必须自己挂：挂不上就留 DOM（慢，但不白屏）。
   必须在 term.open() **之后**挂：渲染器要拿真实 DOM 容器。 */

/* 渲染器偏好：URL `?term=webgl|dom` 优先（并记进 localStorage），其次 localStorage，
   `?term=auto` 清除记忆回到默认。留这个后门是因为「哪个渲染器能用」取决于客户端字体与
   GPU，服务端看不见也测不到 —— 出事时用户能自己一键切，不用等我。
   `canvas` 曾是中间档，xterm 6.0 已移除该 addon（peerDependencies 仍锁 `^5.0.0`，
   取证见 vendor/README.md）⇒ 老链接里的 `?term=canvas` 现在**显式降级 dom 并告警**，
   不静默改写成别的档：留着旧 URL 的人要能看见"这条后门没了"，否则他只会当成
   "改了参数没生效"再来报一次。 */
function termRendererPref() {
  try {
    const q = new URLSearchParams(location.search).get('term');
    if (q === 'auto') { lsRemove('hubTermRenderer'); return ''; }
    if (q === 'canvas') {
      console.warn('[term] ?term=canvas 在 xterm 6.0 已不存在（canvas addon 停止维护且不兼容 6.0）→ 按 dom 处理');
      lsSet('hubTermRenderer', 'dom');
      return 'dom';
    }
    if (/^(webgl|dom)$/.test(q || '')) {
      lsSet('hubTermRenderer', q);
      return q;
    }
    // 走 01 分片的 lsGet 守卫（隐私模式/配额满时不抛，见 tests/test_ls_guard.py）
    const s = lsGet('hubTermRenderer', '');
    if (s === 'canvas') {
      console.warn('[term] 本地记忆的渲染器偏好 canvas 已失效（xterm 6.0 移除该 addon）→ 按 dom 处理');
      lsSet('hubTermRenderer', 'dom');
      return 'dom';
    }
    return /^(webgl|dom)$/.test(s || '') ? s : '';
  } catch (e) { return ''; }
}

/* CJK 字形可用性探测 —— 决定敢不敢用 GPU/Canvas 渲染器。
   为什么需要这道闸：GPU/Canvas 渲染器把每个字形光栅化进一张纹理图集，对「不是来自字体栈
   里点名的那几个字体、而是靠 generic monospace 兜回来的 CJK」处理很差 —— 汉字被画成白色
   方块/空白，同一行的拉丁字母却完全正常（siteboon/claudecodeui#822 同款）。
   DOM 渲染器走浏览器原生文本渲染，字体回退链是完整的，永远不会出这个问题（代价是慢）。
   探测法：用**同一个字体栈**在 canvas 2d 上画「中」，再画一个私用区码点（正常字体必然缺
   该字形，会画成缺字方块）；两者墨迹量接近 ⇒ 「中」画出来的也是缺字方块而不是汉字。
   探测本身失败时返回 true（不阻断，维持原行为）—— 闸门只该在确证有问题时落下。 */
function termCjkUsable() {
  try {
    const cv = document.createElement('canvas');
    cv.width = 48; cv.height = 48;
    const cx = cv.getContext('2d');
    if (!cx) return true;
    // 字体栈里带换行缩进，先压成单行空白再交给 canvas font 解析
    const stack = cssToken('--term-font', 'monospace').replace(/\s+/g, ' ');
    const ink = ch => {
      cx.clearRect(0, 0, 48, 48);
      cx.font = '28px ' + stack;
      cx.fillStyle = '#ffffff';
      cx.fillText(ch, 4, 34);
      const d = cx.getImageData(0, 0, 48, 48).data;
      let n = 0;
      for (let i = 3; i < d.length; i += 4) if (d[i] > 40) n++;
      return n;
    };
    const zh = ink('\u4e2d');       // 中
    if (zh <= 0) return false;       // 汉字一个像素都没画出来
    const tofu = ink('\ue000');      // 私用区：正常字体必然缺字形
    return Math.abs(zh - tofu) > 10;
  } catch (e) { return true; }
}

/* 诊断出口：控制台里 `hubTermDiag()` 可查渲染器/CJK/字体栈，排障不用猜。 */
window.hubTermDiag = function () {
  return {
    renderer: termRendererName,
    pref: termRendererPref(),
    cjkUsable: termCjkUsable(),
    font: cssToken('--term-font', '')
  };
};

/* WebGL 图集显存止血（v0.13.60）：webgl 渲染器把每个用到的字形光栅化进一张纹理
   图集，上游只按 LRU 换页、**从不主动清空**；终端会跑数小时（长会话 / tail -f / 编译进度），
   字形集单调增长 ⇒ 图集页用满后换页开销上升，长期挂着会出现显存占用偏高、GPU 进程吃紧。
   上游 5.5.0 与 6.0.0 都提供公开的 `clearTextureAtlas()`，本项目从未调用过 ⇒ 这是未修态。
   这里定时调它：字形表是**惰性重建**的（清掉后用到哪个字形重新光栅化，多一次几十微秒的工作），
   换来显存不单调涨。放在终端不可见时跳过，不打扰后台标签页。 */
const TERM_ATLAS_SWEEP_MS = 120000;
function termAtlasSweep(addon) {
  const tick = () => {
    if (!termVisible() || document.hidden) return;
    try { addon.clearTextureAtlas(); } catch (e) {
      /* 上游若改签名就安静停掉这条止血线：宁可显存涨，也不能因为清理失败把终端带崩 */
      console.warn('[term] 图集清理失败，已停用定时清理：' + ((e && e.message) || e));
      clearInterval(id);
    }
  };
  const id = setInterval(tick, TERM_ATLAS_SWEEP_MS);
  /* term 是全局单例、整个页面生命周期不 dispose，所以定时器不用随 term 清理 */
  return id;
}

function termLoadRenderer() {
  const pref = termRendererPref();
  if (pref === 'dom') {
    termRendererName = 'dom';
    console.info('[term] 渲染器：dom（按 ?term=dom / localStorage 指定）');
    return;
  }
  if (!termCjkUsable()) {
    /* 中文是硬需求，性能是软需求：宁可慢，不能看不见字。 */
    termRendererName = 'dom';
    console.warn('[term] 字体栈取不到 CJK 字形（汉字会画成方块）→ 放弃 GPU，改走 DOM 渲染');
    return;
  }
  /* 只有 GPU 一档，失败即 DOM：canvas addon 的 peerDependencies 仍锁 @xterm/xterm ^5.0.0，
     与 6.0 不兼容（取证见 vendor/README.md），挂着它只会得到一个白屏的终端。 */
  let tries = [
    ['webgl', window.WebglAddon && window.WebglAddon.WebglAddon]
  ];
  if (pref === 'webgl') tries = tries.filter(t => t[0] === pref);
  for (let i = 0; i < tries.length; i++) {
    const name = tries[i][0], Ctor = tries[i][1];
    if (typeof Ctor !== 'function') continue;
    try {
      const addon = new Ctor();
      term.loadAddon(addon);
      /* WebGL 上下文会被系统回收（GPU 进程崩溃 / 驱动重置 / 标签页后台久了被丢弃）。
         xterm 会自己摘掉渲染器退回 DOM，这里补一条可见日志 + 强制重画把画面补回来。 */
      if (name === 'webgl') {
        if (addon.onContextLoss) addon.onContextLoss(() => {
          termRendererName = 'dom';
          console.warn('[term] WebGL 上下文丢失，已回落 DOM 渲染');
          try { term.refresh(0, term.rows - 1); } catch (e) {}
        });
        if (typeof addon.clearTextureAtlas === 'function') termAtlasSweep(addon);
      }
      termRendererName = name;
      console.info('[term] 渲染器：' + name);
      return;
    } catch (e) {
      console.warn('[term] ' + name + ' 渲染器不可用：' + ((e && e.message) || e));
    }
  }
  termRendererName = 'dom';
  console.warn('[term] 未挂上 WebGL 渲染器，停留在 DOM 渲染（可用但会卡）');
}

function ensureTerm() {
  if (term) return;
  // 字号 / 字族 / 配色全部取自 index.html 的 --term-* token（唯一真值源）。
  // 改前是 fontSize:13 + 'Menlo,Consolas,monospace' + 全灰 ANSI：字号不落在站点音阶内、
  // 缺 CJK 等宽导致中文掉字体、16 色全是灰阶导致 ls/git diff 的着色输出完全看不出区别。
  const T = (k, fb) => cssToken('--term-' + k, fb);
  term = new window.Terminal({
    /* allowProposedApi 必须开：term.unicode（Unicode11 宽字符）是 proposed API，
       不开的话写 `term.unicode.activeVersion='11'` 会抛
       "You must set the allowProposedApi option to true" —— 实测踩到（09-29 CDP 取证）。
       它只解锁 proposed 接口访问，不改变已有行为。 */
    allowProposedApi: true,
    fontSize: cssNum('--term-fs', 14),
    lineHeight: cssNum('--term-lh', 1.5),
    fontFamily: T('font', 'monospace'),
    cursorStyle: 'bar', cursorBlink: true, scrollback: 5000,
    /* v0.13.63 滚轮灵敏度：xterm 6.0 新增 consumeWheelEvent 里有
       `if (|deltaY| < 50) r *= 0.3` 再 `Math.floor` 取整 —— 默认 scrollSensitivity=1
       时，一格标准滚轮(deltaY=120，行高 24px) 只走 120/24*0.3 = 1.5 → 取整 1~2 行。
       实测（CDP 真派发，2000 行 scrollback、24 行视口）：
         sens=1  → 2.1 行/格   sens=3 → 6.2   sens=5 → 10.5   sens=10 → 20.8（严格线性）
       也就是说默认配置下要从底部滚到顶得摇约 940 格，体感就是「无法上翻」。
       5.5.0 没有这段逻辑（A/B 实测两版行为一致），但 5.5 是按 deltaY/行高 走的，
       同样幅度的滚动本来就该是 5 行/格 ⇒ 6.0 的 0.3 折相当于把滚动体验砍到 1/5。
       取 5：与「不按 6.0 打折时的自然值」(5 行/格) 对齐，滚到顶约 390 格，
       且按住 Alt/Ctrl/Shift 仍走 fastScrollSensitivity(=5) 走得更远，不丢快速滚动能力。 */
    scrollSensitivity: 5,
    theme: {
      /* 兜底值与 token 真值同步为深色（黑底白字），token 缺失时也不回浅色 */
      background: T('bg', '#000000'), foreground: T('fg', '#ffffff'),
      cursor: T('cursor', '#ffffff'), cursorAccent: T('bg', '#000000'),
      selectionBackground: T('sel', '#b0d0ff40'),
      black: T('black', '#7f7f7f'), red: T('red', '#cd3131'), green: T('green', '#0dbc79'), yellow: T('yellow', '#e5e510'),
      blue: T('blue', '#2472c8'), magenta: T('magenta', '#bc3fbc'), cyan: T('cyan', '#3b8ea6'), white: T('white', '#e5e5e5'),
      brightBlack: T('bblack', '#666666'), brightRed: T('bred', '#f14c4c'), brightGreen: T('bgreen', '#23d18b'), brightYellow: T('byellow', '#f1f14c'),
      brightBlue: T('bblue', '#3c85cc'), brightMagenta: T('bmagenta', '#d73fd7'), brightCyan: T('bcyan', '#49c2d6'), brightWhite: T('bwhite', '#ffffff')
    }
  });
  /* Unicode11 必须在 open **之前**挂并激活：CJK / emoji 的 cell 宽度判定在渲染器初始化时
     就固化进 cell 尺寸表，之后再改 activeVersion 不会重算已布局的行 ⇒ 中文整体错位。 */
  try {
    if (window.Unicode11Addon && typeof window.Unicode11Addon.Unicode11Addon === 'function') {
      term.loadAddon(new window.Unicode11Addon.Unicode11Addon());
      term.unicode.activeVersion = '11';
    }
  } catch (e) { console.warn('[term] Unicode11 不可用：' + ((e && e.message) || e)); }

  termFit = new window.FitAddon.FitAddon();
  term.loadAddon(termFit);
  term.open($('termEl'));
  /* 渲染器必须紧跟 open()：它要拿真实容器量 cell 尺寸。挂晚了会先以 DOM 渲染一阵子再切换。 */
  termLoadRenderer();

  /* 其余插件统一在这里挂，单个失败不影响别的（vendor 文件缺失 / 版本错都不会拖垮终端）。 */
  try {
    if (window.SearchAddon && typeof window.SearchAddon.SearchAddon === 'function') {
      termSearch = new window.SearchAddon.SearchAddon();
      term.loadAddon(termSearch);
    }
  } catch (e) { console.warn('[term] SearchAddon 挂载失败：' + ((e && e.message) || e)); }
  try {
    /* P4：传自定义 provider（termCopyProvider：navigator.clipboard → execCommand 双路兜底）。
       默认 provider 只认 navigator.clipboard，局域网 http（非安全上下文）下远端 TUI 的
       OSC 52 写剪贴板会静默落空。类型与运行时不一致时 cast，沿用仓内既有的 try 包裹风格。 */
    const clipCtor = window.ClipboardAddon && window.ClipboardAddon.ClipboardAddon;
    if (typeof clipCtor === 'function')
      term.loadAddon(new clipCtor(undefined, termCopyProvider()));
  } catch (e) { console.warn('[term] ClipboardAddon 挂载失败：' + ((e && e.message) || e)); }
  try {
    if (window.WebLinksAddon && typeof window.WebLinksAddon.WebLinksAddon === 'function')
      term.loadAddon(new window.WebLinksAddon.WebLinksAddon());
  } catch (e) { console.warn('[term] WebLinksAddon 挂载失败：' + ((e && e.message) || e)); }

  term.onData(termSend);
  /* v0.13.83：xterm 自带的 helper textarea 也要对鼠标失效（用户裁定：终端页的文字输入
     控件不吃鼠标，鼠标只保留页面/scrollback 滚动）。
     ⚠️ 为什么光靠 vendor CSS 不够（实测读 vendor 源码 + 我第一版就写错了这条）：
     vendor 静态 CSS 里它确实是 `0×0 / left:-9999em / z-index:-5`，**静息态**吃不到鼠标；
     但 xterm 的 `updateCompositionElements()` 在**输入法组字期间**（`_isComposing` 为真、
     compositionstart/update 都会直接调它，不等渲染帧）会改写它的 inline style：
     `style.left/top/width/height` 被设到**光标所在处**并撑到至少 1px×cell 高
     ⇒ 组字那一刻它就是个真实可点的小方块，正好落在用户天天用的中文/IME 输入路径上。
     inline style 压过 vendor 的 class ⇒ 必须在运行时钉死。六个被 xterm 改写的属性里
     **不含 pointer-events**，所以这一行不会被它盖掉；IME 也不需要鼠标点它
     （走 compositionstart/update/end + focus），故关闭鼠标不影响组字。 */
  try { if (term.textarea) term.textarea.style.pointerEvents = 'none'; } catch (e) {}
  /* 用户亲手点到终端 / 焦点落进来 ⇒ 本端主张尺寸所有权（见 termSizeIntent 注释）。
     这是「谁在用谁说了算」：正在操作的那一端永远能拿回尺寸，后台那一端拿不走。 */
  try { term.onFocus(() => { termSizeClaimed = true; }); } catch (e) {}
  const tEl = $('termEl');
  if (tEl && !tEl.dataset.claimBound) {
    tEl.dataset.claimBound = '1';
    tEl.addEventListener('pointerdown', () => { termSizeClaimed = true; });
  }
  /* 回调一律包一层：termRepaint(force) 的形参不能接 addEventListener/ResizeObserver 的事件对象 */
  window.addEventListener('resize', () => termRepaint());
  new ResizeObserver(() => termRepaint()).observe($('termEl'));
  requestAnimationFrame(() => termRepaint());
  setTimeout(() => termRepaint(), 150);
  termFindBind();
  termPasteBind();
  termCopyBind();   /* P1/P2：复制通道（有选区拦 C/V + 容器 copy 兜底），与粘贴通道对称 */
  termCtxBind();    /* v0.13.92：右键菜单（复制/粘贴/全选），替掉 canvas 的「图片另存为」 */
  /* 触摸层最后挂：它要读 term.options（字号/行高）做手势换算，构造完才有意义。
     内部自带 touch 判定，桌面端这行是空操作。 */
  if (typeof termTouchBind === 'function') termTouchBind();
  termMouseResetBind();
  termAltScreenBlock();   /* v0.13.83：从根上吞掉 ?1049h，终端恒在主屏（见函数注释） */
}

/* ── 鼠标跟踪看门狗（v0.13.81）───────────────────────────────────────────────
   用户报障（2026-10-06）：agent-hub 嵌入式终端「向上浏览有时不行、无法复制、
   codex 终端会抢鼠标焦点」；cloudcli 终端（普通 shell→TTY 场景）踩中少所以
   「正常」。实测根因（真 chromium + CDP，探针 /tmp/probe_*）：
   TUI 程序（claude/codex 的交互界面）开启 xterm 鼠标跟踪（DECSET ?1002h/?1003h）
   后，滚轮与拖拽选中会被 xterm 原样吞掉转成 SGR 上报发给 pty 程序 —— xterm
   既把它当「程序内的滚动/点击」重画界面（codex 表现为输入框跟着动、焦点被抢），
   浏览器侧也不再滚 scrollback。TUI 异常退出时没发关闭序列（?1003l），xterm
   内部 mouseTrackingMode 就卡死在 any，只能靠重连（termConnect 写 TERM_MOUSE_OFF）
   复位 —— 这就是「时好时坏」和「打开 cloudcli 后再回来就能滑了」的机制。
   为什么用 capture 阶段监听 + 同步协议复位（试错试出来的最优解）：
     ① xterm 6.0 attachCustomWheelEventHandler 返回 false 不够 —— 事件已被
        preventDefault，浏览器默认滚动被禁，滚轮照样不动（实测 a/b/c）。
     ② passive wheel 监听里只写 term.write(1003l) 是异步的，首格滚轮被吞
        （onProtocolChange 要等下一帧才把 handleMouseWheel 翻回来）。
     ③ 唯一「首事件即恢复」的做法：capture 阶段同步把 coreMouseService
        .activeProtocol 切回 'NONE' —— setter 同步触发 onProtocolChange，
        把 _scrollableElement 的 handleMouseWheel 立刻翻回 true，当次 wheel
        事件就走默认滚动；再异步写 TERM_MOUSE_OFF 到 pty 让对端也退出跟踪态。
   实测数据（probe_final_watchdog.py，1003 跟踪态下）：
     首滚一格 viewportY 50→40（恢复滚动）｜首拖选 sel=12 选中文本（恢复复制）
     纯点击 mode any→none（点击回到浏览器，不再被 TUI 抢焦点）
    设计约束（改这几行前先读）：
      - 只在 mouseTrackingMode !== 'none' 时动手：正常态零开销、不碰任何行为。
      - mousedown 也复位：拖选第一帧（mousedown）就把协议切回 NONE，
        xterm 自带选择才能在这帧启动（实测只挂 wheel 时 sel 选不中）。
        代价：跟踪态下的纯点击也会回到浏览器行为 —— 这正是用户要的「点终端
        不再被 TUI 抢焦点」。
      - ⚠️ v0.13.84 起本复位**不再动 termMouseLive**：那是 app 的意图（want 表
        派生），本端临时摘合装由 mouseup 的 termMouseArm() 负责。旧口径
        「termMouseLive 同步置 false」会把 claude/opencode 这类全屏 TUI 的
        应用内滚动永久掐死（它们的历史不在 scrollback，只有轮子上报喂回 pty
        这一条路 —— 真 PTY 实测与 tests/verify_term_mouse_apps.py 红基线）。
      - 只绑一次（dataset 标记），#termEl 是整页生命周期同一个节点。 */
 let termMouseResetBound = false;
function termMouseResetNow() {
  /* 同步切协议：让当次 wheel/mousedown 事件立刻回到浏览器默认路径 */
  try {
    if (term && term._core && term._core.coreMouseService
        && term._core.coreMouseService.activeProtocol !== 'NONE')
      term._core.coreMouseService.activeProtocol = 'NONE';
  } catch (e) { /* 内部结构升级就退回纯异步复位，不抛 */ }
  if (term && term.modes.mouseTrackingMode !== 'none') {
    try { term.write(TERM_MOUSE_OFF); } catch (e) {}   // 只清本端解析态（见 termMouseArm 注释）
  }
}
/* wheel 分层入口（v0.13.84，v0.13.92 修回装时机）：
   - app 声明要鼠标（live）⇒ 让 xterm 生成本格滚轮上报，termSend 闸门放行
     ⇒ 上报进 pty ⇒ 全屏 TUI 应用内翻历史（用户判据「向上翻看内容」）；
     · v0.13.92：装回跟踪态的时机从 mouseup 挪到这里 —— **有选区就不装**。
       mouseup 时若是拖选，装回会经 xterm `SelectionService.disable()`
       （`clearSelection()`）把刚建好的选区抹掉（2026-10-08「选不中/复制不动」根因，
       见 termMouseArm 注释）。选区在 = 用户要复制，此时滚轮走 scrollback 不打扰它；
       选区清掉后（下一次点击/滚轮）这里把上报通道装回来，应用内滚动照常恢复。
   - 未声明（shell/codex/线性族）⇒ 维持 v0.13.81 自愈复位，当次事件走原生 scrollback。 */
function termWheelNow() {
  if (termMouseLive) {
    if (!term.hasSelection()) termMouseArm();
    return;
  }
  termMouseResetNow();
}
function termMouseResetBind() {
  const el = term && term.element;
  if (!el || el.dataset.mouseResetBound) return;
  el.dataset.mouseResetBound = '1';
  /* capture 阶段 + passive：抢在 xterm 任何内部处理器之前，且不吞事件。
     mousedown 走捕获是为了纯点击也能复位（用户诉求「不抢焦点」）。 */
  el.addEventListener('wheel', termWheelNow, { capture: true, passive: true });
  el.addEventListener('mousedown', termMouseResetNow, { capture: true });
  /* 松手即回装 app 声明的跟踪态：拖选/点击归浏览器的窗口只到 mouseup 为止，
     下一次 wheel 立刻回到「喂给 app」的分层上。绑在 window：选区常越出终端盒，
     element 上的 mouseup 会漏装（实测漏装后 wheel 回不来，只有等 app 重画重新断言
     —— claude 会、opencode 不会，不能赌 app 行为）。
     ⚠️ 必须 bubble + 下一个宏任务：窗口 capture 会抢在 xterm 自己的 mouseup
     （document/element bubble）之前把跟踪态装回去，xterm 的 mouseup 一看
     「鼠标归 app」就把刚建立的选区清了（v0.13.84 第一版实测 B1 双红）。
     bubble 在事件流最后，setTimeout(0) 再让出一步，选区落定后装回才安全。 */
  window.addEventListener('mouseup', function () { setTimeout(termMouseArm, 0); });
}

/* ── 备用屏永久关闭（v0.13.83）──────────────────────────────────────────────
   用户裁定（2026-10-07）：嵌入式终端**只用主屏**，v0.13.82 那套「状态字 + 退出备用屏
   按钮」连同 UI 一并删除，改成从根上不让终端进备用屏。

   为什么值得从根上拦：xterm.js 的备用屏按设计没有 scrollback（vendor 里是
   `new Buffer(!1,…)`），TUI 一进去就永远滚不动；而整页只有一个 xterm 实例，
   一次污染会跨会话传染到所有 agent，直到整页刷新。

   做法：把 DECSET（`?h`）里的 1049 / 1047 / 47 注册成「吞掉」—— 解析器**先**遍历
   用户 handler，全部返回 false 才落 `_csiHandlerFb`（内建）⇒ 返回 true 即拦下，
   内建的 `activateAltBuffer()` 不执行。实测依据（本仓 vendor）：
   `registerCsiHandler({prefix:'?',final:'h'},…)` 的注册表键 = `_collect<<8|final`，
   prefix 已编进 `_collect` ⇒ 与内建 DECSET 共用同一张表，且该表在**内建之前**被咨询。

   ⚠️ **只吞「进入」、不吞「退出」**：内建 1049h = `saveCursor()` + `activateAltBuffer()`，
   而 `restoreCursor()` 实测只恢复 x/y/attr、**不恢复滚动位置**。既然从没进去过，
   就没有什么要还原；万一某个程序仍旧发 `?1049l`，内建会走 `activateNormalBuffer()`
   （本就是 normal，幂等无害）。

   其余 DECSET（鼠标 1000/1002/1003、粘贴 2004、同步块 2026…）一律放行 —— 只点名这三个，
   不是把 `?h` 整个闷掉。
   逃生口（不占 UI）：万一端侧仍卡在异常状态，浏览器控制台执行 `term.reset()` 即可。 */
const TERM_ALT_BLOCKED = new Set(['1049', '1047', '47']);
function termAltScreenBlock() {
  if (!term || !term.parser || typeof term.parser.registerCsiHandler !== 'function') return;
  try {
    term.parser.registerCsiHandler({ prefix: '?', final: 'h' }, p => {
      /* ⚠️ **参数形状实测**（本仓 vendor，CDP 直接打印 handler 实参）：
         handler 收到的是**参数数组本身** —— `p[0] === 1049`、`Array.isArray(p) === true`、
         `p.params === undefined`。我第一版按 `String(p.params[0])` 取参，`undefined[0]`
         当场抛 TypeError，被下面的 catch 吞掉 ⇒ 返回 false ⇒ 照旧进备用屏；
         而验收探针 B3 一句 "写 ?1049h 后仍在主屏" 立刻把它抓红。故此处以参数数组为准、
         `p.params` 只作老签名的兼容兜底。
         ⚠️ 判据从「只看 p[0]」改为「任一参数命中」：DECSET 允许合并（`?1049;1003h`），
         只认 p[0] 时 `?1003;1049h` 会漏吞 ⇒ 照旧进备用屏。代价是这种合并串被**整条**
         吞掉（同串里的 1003 鼠标也就丢了）——但两条已知 TUI（jcode/claude）都分开发，
         而「进了备用屏」是本仓反复报障的形态，两害相权取其轻。 */
      let vals = [];
      try {
        if (p && p.length != null) vals = Array.from(p);
        else if (p && p.params) vals = Array.from(p.params);
      } catch (e) { vals = []; }
      const hasAlt = vals.some(x => TERM_ALT_BLOCKED.has(String(x)));
      if (!hasAlt) {
        /* ★ v0.13.92：**有选区时吞掉 app 的鼠标跟踪 DECSET**（2026-10-08「选不中」第二半根因）。
           为什么只挡不够、还得加这一条：我们在「有选区」时故意把协议留成 NONE
           （termMouseArm 早退，见 02 注释），但 claude/codex 这类 TUI **每帧重画都会重断言**
           `?1000;1002;1003;1006h`。协议一旦 NONE→非 NONE，xterm 的
           `SelectionService.disable()`（`clearSelection()`）就把用户刚拖出的选区抹掉 ——
           于是「agent 正在流式输出时永远选不中」。吞掉重断言，xterm 的跟踪态维持 NONE，
           选区保住；app 侧无感（这是显示方向的数据流，不是发给 pty 的）。
           选区清掉后由 termMouseArm() 按 want 表回装（want 由 02 的原始帧扫描维护，不受本吞影响）。 */
        if (vals.some(x => TERM_MOUSE_MODES.has(String(x))) && term.hasSelection()) return true;
        return false;
      }

      /* ★ v0.13.91 根治「jcode 启动嵌入式终端带入乱码」（2026-10-08 用户报障）——
         病灶：内建 1049h = `saveCursor()` + `activateAltBuffer()`，而 xterm.js 的备用屏是
         **初始空白的新缓冲**。本函数只吞「进入」、从不切缓冲 ⇒ TUI 会在**旧画面上**按绝对
         坐标作画：hub 头部（「jcode · 终端」「提示：点『新会话』…」）与 jcode 自己的
         「Connecting to server...」留在第 0~2 行，和 TUI 首帧叠在一起 —— 用户看到的「乱码」。
         实测（真 chromium + 本仓 vendor xterm，喂 jcode v0.91.0 真首帧 2895B）：
           吞而不清 ⇒ 0-2 行残影（`jcode · 终端` / `Connecting to server...`）＋ TUI 正常；
           吞 + 同步 term.clear() ⇒ 屏面干净、TUI 完好（正是真终端里进备用屏的观感）。
         对策：吞掉的**同一刻**同步清屏，等价于备用屏那块「空白画布」。

         ⚠️ 必须**同步** `term.clear()`，不能写成 `term.write('\\x1b[2J')`：xterm 的 write 是
         异步队列，嵌套 write 会被排到本帧全部字节之后 ⇒ 把刚画好的 TUI 一起抹掉
         （实测那一档：整屏空白、TUI 全丢）。`term.clear()` 直接操作缓冲、同步生效，
         其后同一帧里的 TUI 字节照常落在干净画布上。
         ⚠️ `term.clear()` 连 scrollback 一起清（`buffer.lines.length=1`）。对备用屏类 TUI
         这与真终端「主屏被备用屏遮住」的观感等价；而**要进 scrollback 的 codex 走
         `--no-alt-screen`、根本不发 ?1049h**，不受影响（见 src/profiles.py）。
         ⚠️ 已知与 xterm 的差异（如实登记，不做守卫）：xterm 内建 `activateAltBuffer()` 在
         **已在备用屏时提前返回**（不清），故重复 `?1049h` 不重清；本实现无条件清。
         实测 jcode v0.91.0 一次会话只发 1 次（喂输入 + 59 对同步块重画后仍是 1 次），
         codex 走 --no-alt-screen、claude/opencode 开机各一次 ⇒ 现实里不会命中"重复清"。
         若将来真遇到会重发的 TUI，再加"进入态"守卫（代价是守卫失同步会让残影复发，
         故此刻意不加）。 */
      try { term.clear(); } catch (e) { /* 清屏失败也要吞：宁可留残影，绝不放它进备用屏 */ }
      return true;
    });
  } catch (e) { /* 注册失败：终端仍可用，只是退回「换会话复位」那层兜底 */ }
}

/* ── 粘贴（bracketed paste 安全包装）──────────────────────────────────────────
   接 xterm 自己的 textarea paste（用户 Ctrl/Cmd+V、右键粘贴都走这条路），但**抢在它前面**：
   捕获阶段接下 → 自己做安全处理 → 再交给 term.paste()（term.paste 会按 2004 状态包装）。
   两件 xterm 不做、我们必须做的事：
     ① 内嵌终止序列降级：粘贴内容里若含 ESC[201~，原样包进 bracketed 段会让对端**提前**
        结束粘贴模式，其后字节降级为普通按键被逐条执行（这就是注入面）。
        paseo 同款处理见 terminal-paste.ts:27（replaceAll(BRACKETED_PASTE_END, "[201~")）。
     ② 换行归一：\r\n / \n 统一成 \r（终端的"回车"语义），否则 readline 会把 CRLF 里的
        LF 再解释一次，多出一个空行。
   termBracketed 为 false（对端没声明支持）时**不自己发明包装**：发出去的 ESC[200~ 会被
   当成字面量糊在屏幕上，比不包更糟。此时只做换行归一，行为与改前一致。 */
function termPasteText(txt) {
  if (!term || !txt) return;
  let s = String(txt).replace(/\r\n/g, '\r').replace(/\n/g, '\r');
  if (termBracketed && s.indexOf(TERM_PASTE_END) >= 0) s = s.split(TERM_PASTE_END).join('[201~');
  try { term.paste(s); } catch (e) { /* paste 不可用就退回 input，至少别把内容丢了 */
    try { term.input(s, true); } catch (e2) {}
  }
}
function termPasteBind() {
  const ta = term && term.textarea;
  if (!ta || ta.dataset.pasteBound) return;
  ta.dataset.pasteBound = '1';
  ta.addEventListener('paste', e => {
    const cd = e.clipboardData || window.clipboardData;
    const txt = cd ? cd.getData('text') : '';
    if (!txt) return;
    e.preventDefault();
    e.stopPropagation();   // 别让 xterm 的原生 paste 再处理一遍（那遍不转义内嵌终止序列）
    termPasteText(txt);
  }, true);
}

/* ── 终端复制（P1–P4，2026-10-05 借鉴 cloudcli 的 useShellTerminal 移植）────────────
   病灶：agent-hub 只有粘贴通道（termPasteBind），没有复制通道——xterm 选中文本后
   Ctrl+C 被 term.onData(termSend) 原样灌进 pty（= bash SIGINT），没有任何路径把选区
   写进系统剪贴板，体感就是"终端里选不中/复制不动"。四处改动（按授权 P1–P4）：
   P1 attachCustomKeyEventHandler：仅 hasSelection() 时拦 C（内置复制选区）/V（粘贴），
      否则一律 return true 放行（无选区 Ctrl+C 照旧进 pty，不误伤 SIGINT 语义）；
      ⚠️ v0.13.126 起 C 只 return false、**不** preventDefault（交给浏览器原生 copy 链，
      见 termCopyBind 分支内的端侧取证）；
   P2 #termEl 容器 copy 事件兜底：有选区时 clipboardData.setData 覆盖右键/Cmd+Shift+C
      等原生路径（很多嵌入式/非 HTTPS 环境 navigator.clipboard 不可用）；
   P3 termCopySelection：navigator.clipboard.writeText → 失败退回隐藏 textarea
      execCommand（与 L1324 settingsLogsCopyText 同款两级兜底；局域网 http 必需要）；
   P4 ClipboardAddon 构造传自定义 provider（同款双路兜底），让远端 TUI 的 OSC 52
      写剪贴板在非 HTTPS 下也能成（默认 provider 只认 navigator.clipboard）。 */
function termCopyFallback(text) {
  /* P3 核心：两级兜底写剪贴板（navigator.clipboard → 隐藏 textarea execCommand）。
     局域网 http（非安全上下文）navigator.clipboard 常缺失/被拒，必须有第二路。

     ★ v0.13.126（2026-10-18 用户报障「Mac 下复制/粘贴键盘快捷键失效」）：
     非安全上下文那条路必须**同步**执行，不能排进 Promise 微任务。
     端侧取证：Mac 主机 192.168.5.101 走 `http://192.168.5.102:3102`（生产照常 200），
     即 isSecureContext=false、navigator.clipboard===undefined ⇒ 复制只剩 execCommand
     这一条路；而 WebKit/Safari 的用户手势只在**当前任务**有效，改前把它放在
     `p.then(...)` 里跑，手势已过期 ⇒ execCommand('copy') 返回 false、剪贴板没内容
     ⇒ 紧接着的 Cmd+V 自然「粘不出东西」，看起来就是复制粘贴整块坏掉。
     Chromium 的瞬态激活能跨微任务（Linux 端到端闸门一直绿），所以这个缺陷只在
     Mac/Safari 侧显形 —— 这正是本仓「端侧不可观测」的典型形态。
     修法：安全上下文仍走异步 clipboard API（无手势依赖）；非安全上下文直接同步调
     execCommand，手势必然有效。同步化不改变焦点归还义务（见下面 finally）。 */
  if (!text) return Promise.resolve(false);
  /* 同步写剪贴板：临时 textarea + execCommand。返回 boolean（不返回 Promise），
     便于非安全上下文在**同一个任务**里完成复制。 */
  const viaExec = () => {
    /* ★ 焦点归还（2026-10-07 用户报障「复制粘贴/键盘快捷键全都用不了」的根因）。
       execCommand('copy') 这条路**必须**先把临时 textarea 选中 ⇒ 它会把焦点从
       xterm 的 helper textarea 抢到 body。改前复制完就不还了，而 xterm 的
       键盘通路完全依赖那个 textarea 持有焦点 ⇒ 用户复制一次之后**所有**按键
       都不再进终端（看起来就是"快捷键全哑、连打字都没反应"）。
       实测（真 chromium + CDP，局域网 http）：复制前 focus=termTA，
       复制后 focus=BODY，且此后 Ctrl+V 的 paste 事件计数恒为 0。
       所以这里记下复制前的焦点宿主，复制完原样还回去。 */
    const prev = document.activeElement;
    try {
      const ta = document.createElement('textarea');
      ta.value = text;
      ta.setAttribute('readonly', '');
      ta.style.cssText = 'position:fixed;top:-1000px;opacity:0;pointer-events:none';
      document.body.appendChild(ta);
      ta.select();
      const ok = document.execCommand('copy');
      ta.remove();
      return !!ok;
    } catch (e) { return false; }
    finally {
      /* finally 保证「复制成功/失败/抛异常」三条路都把焦点还回去；
         宿主已从 DOM 摘掉（或本来就不可聚焦）时退回终端，
         终端不在时保持浏览器默认行为，不硬抢。 */
      try {
        if (prev && prev.isConnected && typeof prev.focus === 'function') prev.focus();
        else if (term && term.textarea) term.textarea.focus();
      } catch (e) {}
    }
  };
  /* 安全上下文（https / loopback）优先用标准 API；它失败才退同步 execCommand。
     非安全上下文没有标准 API 可用，**同步**走 execCommand（见上面的端侧取证）。 */
  if (window.isSecureContext && navigator.clipboard && navigator.clipboard.writeText) {
    return navigator.clipboard.writeText(text).then(() => true).catch(() => viaExec());
  }
  return Promise.resolve(viaExec());
}
function termCopySelection() {
  if (!term) return Promise.resolve(false);
  const sel = term.getSelection();
  if (!sel) return Promise.resolve(false);
  return termCopyFallback(sel);
}
function termCopyProvider() {
  /* P4：OSC 52 provider。vendor 的 ClipboardAddon 默认 provider 只走
     navigator.clipboard（非安全上下文 undefined）；换成同一个双路兜底，
     远端 TUI 发 OSC 52 写剪贴板时非 HTTPS 也能落地。 */
  return {
    readText(reg) {
      if (reg !== 'c') return Promise.resolve('');
      if (navigator.clipboard && navigator.clipboard.readText)
        return navigator.clipboard.readText();
      return Promise.resolve('');
    },
    writeText(reg, text) {
      if (reg !== 'c') return Promise.resolve();
      return termCopyFallback(text).then(() => {});
    }
  };
}
function termCopyBind() {
  /* P1：键位拦截。attachCustomKeyEventHandler 的契约：返回 false = 吞掉该键不让
     xterm 处理（但**不**等于阻止浏览器默认行为 —— 那要靠 preventDefault）。
     三档口径（v0.13.90 订正，改前写的是「V 走安全粘贴链」）：
       · 无选区 Ctrl+C → return true：照旧进 pty = SIGINT，bash 语义不能破坏；
       · 有选区 Ctrl/Cmd+C → **只 return false，不 preventDefault**：
         xterm 自己不处理该键（不会把 ^C 送进 pty），浏览器的原生复制链照常跑
         —— `copy` 事件会冒泡到 `.xterm`，xterm 6.0 自带的 copy handler
         （vendor 实证：`copyHandler(e,t){e.clipboardData.setData('text/plain',
         t.selectionText), e.preventDefault()}`）把终端选区写进 clipboardData，
         本文件 #termEl 上那条容器级 copy 兼底再叠一层。v0.13.126 改到这里，
         依据见下面 `if (k === 'c' …)` 分支内的实测。
       · Ctrl/Cmd+V → **一律 return false 且不 preventDefault**：
         让浏览器的原生粘贴落到 xterm 的 helper textarea，由 termPasteBind 接住。
         （改前是「有选区才拦、且 preventDefault + readText」—— 非安全上下文
           没有 navigator.clipboard，那条路把粘贴整个废掉了，见函数内注释。） */
  if (term.attachCustomKeyEventHandler) {
    term.attachCustomKeyEventHandler(e => {
      if (e.type === 'keydown' && (e.ctrlKey || e.metaKey) && !e.altKey) {
        const k = String(e.key || '').toLowerCase();
        if (k === 'c' && term.hasSelection()) {
          /* ★ v0.13.126（2026-10-18 用户报障「Mac 下复制/粘贴键盘快捷键失效」）：
             这里**不再** preventDefault + 自己调 termCopySelection()，改为放给浏览器
             的原生复制链。理由：JS 写剪贴板那条路在端侧只有 `execCommand` 可走
             （局域网 http 无 navigator.clipboard），而 WebKit/Safari 的用户手势
             不跨微任务 —— 一旦排进 Promise 就是“静默失败、剪贴板没内容”，
             紧接着的 Cmd+V 也就“粘不出东西”。原生链不需要任何剪贴板 API，
             也不碰焦点（不建临时 textarea），三平台同一条路。
             实测（真 chromium + CDP，2026-10-18，`work/probe_copy_native_path.py`）：
               点终端取焦 → term.select(0,0,16) → Ctrl+C：
                 `copy` 事件派发到 #termEl（copyEvt=1），事件末端 clipboardData
                 读到 `MAC_COPY_SEL_202`（=终端选区），xterm 已 preventDefault
                 （默认动作不会覆盖写入的数据），pty 零收到 ^C；
                 随后 Cmd+V（meta 修饰符）→ pasteEvt=1、termPasteText 收到同一串
                 ⇒ 复制→粘回去回环成立。
             保留 SIGINT 语义：无选区时仍是 return true，^C 照旧进 pty。 */
          return false;
        }
        /* ★ 粘贴：**不拦**，交给浏览器原生路径（2026-10-07 根治）。
           改前的两个错，实测（真 chromium + CDP，局域网 http）逐条钉过：
             ① 只在 `term.hasSelection()` 时才接管 ⇒ 用户最常用的
                「无选区直接 Ctrl+V」根本没进这条分支；
             ② 就算进了，`preventDefault()` 会**掐断浏览器自己的粘贴**，
                而这条路上我们唯一能拿剪贴板的办法是 navigator.clipboard.readText
                —— 它还只在**安全上下文**存在。局域网 http（用户实际访问方式）
                实测 navigator.clipboard === undefined ⇒ 拦住之后既没读到、
                也没让浏览器粘，等于把粘贴功能整个删掉。
           正确做法：返回 false 只是让 xterm **不要**把 ^V 当输入字节送进 pty；
           不 preventDefault ⇒ 浏览器照常把剪贴板内容粘进 xterm 的 helper
            textarea ⇒ 触发 paste 事件 ⇒ 被 termPasteBind（上方）接住并做
            bracketed-paste 安全包装。这条路**不需要** navigator.clipboard，
           非安全上下文照样通。实测：返回 false 且不 preventDefault ⇒
            pasteEvt 计数 1→2，termPasteText 被调用，pty 收到 ESC[200~…ESC[201~。
           焦点兜底：若此刻焦点已经不在终端上（例如别的代码抢过焦点），
           原生粘贴没有宿主，先补一个 focus 再放行。 */
        if (k === 'v') {
          /* 焦点兜底也走**同一个**谓词（tests/test_term_focus_policy.py 的结构护栏）：
             按 Ctrl+V 是用户主动动作 ⇒ 用 termFocusWanted({ user: true }) 如实表达，
             而不是绕过策略直接抢焦点。只在焦点确实不在终端时才补。 */
          if (term.textarea && document.activeElement !== term.textarea) {
            try { if (termFocusWanted({ user: true })) term.focus(); } catch (err) {}
          }
          return false;
        }
      }
      return true;
    });
    term.dataset_copyBound = 1;
  }
  /* P2：容器级 copy 兜底（原生右键复制 / Cmd+Shift+C 路径带上选区内容）。 */
  const el = $('termEl');
  if (!el || el.dataset_copyBound) return;
  el.dataset_copyBound = '1';
  el.addEventListener('copy', e => {
    if (!term || !term.hasSelection()) return;
    const sel = term.getSelection();
    if (!sel) return;
    e.preventDefault();
    if (e.clipboardData) e.clipboardData.setData('text/plain', sel);
    else void termCopySelection();
  });
}

/* ── 终端右键菜单（v0.13.92）──────────────────────────────────────────────────
   用户报障（2026-10-08）：「agent-hub 嵌入式终端 页面文字无法选择 复制，
   按右键显示是图片」。
   为什么必须自己出菜单：终端是 WebGL 渲染器画在 <canvas> 上的（`?term=webgl` / 默认），
   浏览器眼里那块就是一张图 —— 右键给的是「图片另存为/复制图片」，没有文字的
   「复制」项；而容器上那条原生 `copy` 兜底因此永远等不到触发（那条路只在浏览器
   给出文字的复制菜单项时才走）。所以这里把右键接管过来，给回终端真正该有的三项。
   浮层纪律（AGENTS 4.2）：开启只有 termCtxOpen 一个入口（内部只此一处 add('on')）；
   关闭只有 termCtxClose；document 捕获阶段 pointerdown 关（含触屏点空白逃生）、
   Escape 关、窗口失焦关、终端滚动/尺寸变化关 —— 导航点击同样会先落到 pointerdown。
   三项的可用性在打开时按当时状态定（复制要有选区、粘贴要有可读剪贴板的 API），
   按钮点击即关菜单，不留「菜单开着但点了没反应」的状态。 */
function termCtxClose() {
  const el = $('termCtx');
  if (el && el.classList.contains('on')) el.classList.remove('on');
}
function termCtxOpen(x, y) {
  const el = $('termCtx');
  const host = $('termEl');
  if (!el || !host) return;
  const cp = $('termCtxCopy'), ps = $('termCtxPaste'), sa = $('termCtxSelAll');
  if (cp) cp.disabled = !(term && term.hasSelection());
  /* 只允许一个浮层接收点击（AGENTS 4.2 ①）：右键菜单开时收掉终端内查找条。 */
  if (typeof termFindClose === 'function') termFindClose();
  /* 非安全上下文（局域网 http，用户实际访问方式）没有 navigator.clipboard.readText
     ⇒ 菜单粘贴给不出内容。这时**不隐藏**：点了转「请按 ⌘V / Ctrl+V」的显式提示
     （按平台给对键名 —— Mac 上写 Ctrl+V 等于教错键），
     比留一个静默无效的按钮好（与 v0.13.90 的「显式失败」同一口径）。 */
  if (ps) ps.disabled = false;
  if (sa) sa.disabled = !term;
  el.classList.add('on');
  /* 先加 on 再量尺寸：display:none 时 getBoundingClientRect 全是 0，定位会算飞。 */
  const r = el.getBoundingClientRect(), a = host.getBoundingClientRect();
  let left = x - a.left, top = y - a.top;
  left = Math.max(4, Math.min(left, Math.max(4, a.width - r.width - 4)));
  top = Math.max(4, Math.min(top, Math.max(4, a.height - r.height - 4)));
  el.style.left = left + 'px';
  el.style.top = top + 'px';
  const first = el.querySelector('button:not([disabled])');
  if (first) { try { first.focus({ preventScroll: true }); } catch (e) {} }
}
function termCtxPasteManual() {
  if (term && typeof termFocusWanted === 'function') {
    try { if (termFocusWanted({ user: true })) term.focus(); } catch (e) {}
  }
  if (typeof toast === 'function') toast('此环境不支持读取剪贴板，请按 ' + (/(^|\W)Mac/i.test(String(navigator.userAgent)) ? '⌘V' : 'Ctrl+V') + ' 粘贴', 'err');
}
function termCtxBind() {
  const el = $('termEl');
  if (!el || el.dataset.ctxBound) return;
  el.dataset.ctxBound = '1';
  /* 只接管终端区的右键；菜单自身在 .term-body 内、不在 #termEl 内，不会自触发。 */
  el.addEventListener('contextmenu', e => {
    if (!term) return;
    e.preventDefault();            // 盖掉浏览器对 canvas 的「图片另存为」
    termCtxOpen(e.clientX, e.clientY);
  });
  const cp = $('termCtxCopy'), ps = $('termCtxPaste'), sa = $('termCtxSelAll');
  if (cp) cp.addEventListener('click', () => { termCtxClose(); void termCopySelection(); });
  if (ps) ps.addEventListener('click', () => {
    termCtxClose();
    if (navigator.clipboard && navigator.clipboard.readText) {
      navigator.clipboard.readText().then(t => { if (t) termPasteText(t); },
                                          () => termCtxPasteManual());
    } else termCtxPasteManual();
  });
  if (sa) sa.addEventListener('click', () => {
    termCtxClose();
    if (term && typeof term.selectAll === 'function') { try { term.selectAll(); } catch (e) {} }
  });
  /* 关闭路径（唯一出口）：菜单外 pointerdown（捕获，先于任何业务点击）/ Escape /
     窗口失焦 / 终端滚动与尺寸变化。触屏没有 ESC 也没有右键外的关闭手势 ⇒
     点空白这一条是必需的逃生口（AGENTS 4.2 ③）。 */
  document.addEventListener('pointerdown', e => {
    const m = $('termCtx');
    if (m && m.classList.contains('on') && !m.contains(e.target)) termCtxClose();
  }, true);
  document.addEventListener('keydown', e => { if (e.key === 'Escape') termCtxClose(); });
  window.addEventListener('blur', termCtxClose);
  if (term) {
    try { term.onScroll(termCtxClose); } catch (e) {}
    try { term.onResize(termCtxClose); } catch (e) {}
  }
}

/* ── 终端内查找（Ctrl/Cmd + F）─────────────────────────────────────────────────
   SearchAddon 挂上之后必须给它一个入口，否则只是「插件挂了但用户够不着」。
   keydown 走**捕获阶段**：xterm 会吞掉大部分按键，只有捕获阶段能抢在它前面拦下。
   搜索条是 absolute 浮层，不占 flex 空间 ⇒ 不影响 #termEl 的内容盒（FitAddon 算行数靠它）。 */
let termFindTimer = null;
function termFindNote(s) { const el = $('termFindNote'); if (el) el.textContent = s || ''; }
function termFindOpen() {
  if (!term) return;
  const box = $('termFind');
  if (!box) return;
  /* 单浮层纪律（AGENTS 4.2 ①）：开查找条时收掉右键菜单，两个浮层不并存。 */
  if (typeof termCtxClose === 'function') termCtxClose();
  box.classList.add('on');
  const inp = $('termFindInput');
  if (inp) { try { inp.focus(); inp.select(); } catch (e) {} }
  termFindRun(1);
}
function termFindClose() {
  const box = $('termFind');
  if (box) box.classList.remove('on');
  try { if (termSearch) termSearch.clearDecorations(); } catch (e) {}
  termFindNote('');
  /* 回焦必须走同一谓词（L0 护栏 test_only_guarded_term_focus 盯的就是这一条）：
     关闭查找框是**用户**动作（Ctrl+F 是他自己按的），不是自动挂载/重连那类会偷偷弹软键盘
     的路径 ⇒ 传 user:true 如实表达「这是用户主动」，而不是绕过守卫写裸 focus。 */
  if (term && termFocusWanted({ user: true })) term.focus();
}
function termFindRun(dir) {
  if (!term || !termSearch) { termFindNote('查找不可用'); return; }
  const inp = $('termFindInput');
  const q = inp ? inp.value : '';
  if (!q) { try { termSearch.clearDecorations(); } catch (e) {} termFindNote(''); return; }
  /* incremental:true —— 边输边跳到当前匹配，不等回车；否则用户看不到自己打到哪了 */
  const opt = { caseSensitive: false, wholeWord: false, regex: false, incremental: true };
  try {
    const hit = dir < 0 ? termSearch.findPrevious(q, opt) : termSearch.findNext(q, opt);
    termFindNote(hit ? '有匹配' : '无匹配');
  } catch (e) { termFindNote('查找失败'); }
}
function termFindBind() {
  const inp = $('termFindInput');
  if (inp && !inp.dataset.bound) {
    inp.dataset.bound = '1';
    inp.addEventListener('input', () => {
      if (termFindTimer) clearTimeout(termFindTimer);
      termFindTimer = setTimeout(() => termFindRun(1), 120);   // 节流：别每敲一键扫一遍全部缓冲区
    });
    inp.addEventListener('keydown', e => {
      if (e.key === 'Enter') { e.preventDefault(); termFindRun(e.shiftKey ? -1 : 1); }
      else if (e.key === 'Escape') { e.preventDefault(); termFindClose(); }
      e.stopPropagation();      // 查找框里的按键绝不能漏进 pty
    });
  }
  const bind = (id, fn) => {
    const el = $(id);
    if (el && !el.dataset.bound) { el.dataset.bound = '1'; el.addEventListener('click', fn); }
  };
  bind('termFindPrev', () => termFindRun(-1));
  bind('termFindNext', () => termFindRun(1));
  bind('termFindClose', termFindClose);
}
/* 终端没在显示时不抢 Ctrl+F —— 那时浏览器自己的页内查找才是用户想要的 */
document.addEventListener('keydown', e => {
  if (!(e.ctrlKey || e.metaKey) || e.altKey) return;
  if (e.key !== 'f' && e.key !== 'F') return;
  if (!termVisible()) return;
  e.preventDefault(); e.stopPropagation();
  termFindBind();
  termFindOpen();
}, true);

function termDetach() {
  termRcCancel();          // 用户显式离开 ⇒ 任何在排的重连一律作废，不许把会话拖回来
  termTouchReset();        // 清掉残留手势态（否则新会话第一次滑动就"自己动了"）
  termHbStop();
  termRcAttempt = 0;
  termInputWarned = false;
  termToastClear();
  if (termWs) { try { termWs.close(); } catch (e) {} termWs = null; }
  termSid = null; termSidAgent = null;
  termConnecting(false);   // 解绑后 close 事件会被 termWs!==ws 守卫吃掉，连接中状态在这儿自己收
  termHealPending = false;
}

/* TERM_TOKEN 鉴权（后端强制校验）：首次用终端时 prompt 一次存 localStorage，之后 header+query 双带 */
function termToken() {
  let t = lsGet('hub.term.token');
  if (t) t = String(t).trim();          // 存量脏值自愈：带空白的 token 不再往下传
  if (!t) {
    t = (prompt('请输入终端鉴权 TERM_TOKEN（也可在右上角「设置」查看后一键应用）') || '').trim();
    if (t) lsSet('hub.term.token', t);
  }
  return t;
}
function termHeaders(extra) {
  return Object.assign({ 'X-TERM-TOKEN': termToken() }, extra || {});
}

function wsUrl(path) {
  /* ★ 必须 trim（2026-10-18 实弹取证）：同一个 token 在两条路径上待遇不同 —— HTTP 头里的
     尾随空白会被 HTTP 解析器按 OWS 裁掉，`?token=%20` 不会。于是存量带空白的 token 表现为
     「POST /api/term/sessions 200 + WS 握手 403」：会话建好、pty 活着，xterm 一个字节也收不到，
     终端永久空白（不是 codebuddy 特有，全 agent 同形）。浏览器 localStorage 里的存量脏值
     无法由服务端修（term.py 精确比对是对的，不许放宽），故在读侧自愈。 */
  const t = String(lsGet('hub.term.token') || '').trim();
  const sep = path.includes('?') ? '&' : '?';
  return (location.protocol === 'https:' ? 'wss://' : 'ws://') + location.host + path + (t ? sep + 'token=' + encodeURIComponent(t) : '');
}

/* 是否该抢焦点：只有用户主动动作（新会话 / 点芯片 / 续聊历史）才聚焦。
   自动挂载与退避重连一律不抢 —— 手机上那两条路径每次都无条件弹出软键盘
   （用户描述为「手机一进来键盘就顶着脸」），桌面端则表现为离开页面回来被硬抢焦点。 */
function termFocusWanted(opts) { return !!(opts && opts.user); }

function termConnect(sid, agent, opts) {
  /* opts.reconnect：由 termRcFire 起的自动重连。目前与普通连接同路（都清屏 + 靠 ring 回放补画面），
     留着这个入参是为了「重连场景」与「用户点芯片」在后续分诊时不必再改调用方签名。 */
  const o = opts || {};
  termRcCancel();          // 新连接开始 ⇒ 作废旧的一切实重连计划（防第二个 socket / 防漏定时器）
  termHbStop();            // 心跳定时器全仓唯一，换绑即回收
  termInputWarned = false;
  const oldAlive = !!termWs && termWs.readyState === 1;   // 关旧线之前先记下它还活着
  if (termWs) { try { termWs.close(); } catch (e) {} termWs = null; }
  /* 只有「回到同一条会话且旧 socket 还活着」才保留画面。旧 socket 已死时画面里挂着
     [连接断开] 那行提示，必须清掉——清屏后的空屏由 termHealBlank 逼 pty 重画补回来。 */
  const keepScreen = (sid === termSid) && oldAlive;
  termSid = sid;
  termSidAgent = agent || termSidAgent;
  /* 行1 芯片的「当前」标记跟着走：点芯片回看另一路时 termConnect 不重绘列表，
     不手动改 class 的话 .cur 会停在旧芯片上（实测缺陷：点 first 后 cur 仍在 second）。 */
  const row = $('termSessList');
  if (row) row.querySelectorAll('.sess-item').forEach(x => x.classList.toggle('cur', x.dataset.sid === sid));
   termMouseWantReset();   // 新连接：鼠标意图从零判定（v0.13.84 连 want 表一起清），别继承上一会话
  termBracketed = false;   // 粘贴模式同理：新会话的 2004 要等它自己实时发来才算数
  termHealPending = false; // 上一条会话攒下的补画请求作废，新连接的回放自己会再挂
  if (!o.reconnect) termToastClear();   // 用户主动接的线：收掉「正在重连」提示；自动重连则留到 hb 往返成功才结案
  /* 保留画面时别清屏：清屏 = 先给用户一屏白底，而服务端只回放 ring 里最近 64KB
     （整屏帧早被增量帧挤出去）⇒ 补不满就一直白着，就是用户报的现象。 */
  if (!keepScreen) {
    term.clear();
    /* v0.13.92：换会话时清掉上一会话遗留的选区。不清的话，新会话来声明鼠标模式时
       termMouseArm 会因「有选区」早退（见其注释），滚轮上报通道建不起来
       —— 实测 verify_term_mouse_apps 的 opencode A1 因此 rep=0 转红。新会话 = 新画面，
       旧选区本就无意义。term.clear() 不清选区（vendor 实证：只动 buffer.lines）。 */
    try { term.clearSelection(); } catch (e) {}
  }
  termConnecting(true);
  termDecodeReset();   // 上一连接可能残留半个 UTF-8 字符，别带进新会话
  const ws = new WebSocket(wsUrl('/ws/term/' + sid));
  ws.binaryType = 'arraybuffer';
  /* 连接后的第一帧 = 服务端的历史回放（term.py 里 ring 是整块 send_bytes 出去的，一帧到底）：
     只回显、不复位也不参与「当前是否需要鼠标」的判定——历史里的 TUI 开关是过期状态。
     见文件上方「鼠标上报闸门」与「回放查询闸门」注释。 */
  let replayFrame = true;
  ws.onmessage = ev => {
    if (termWs !== ws) return;
    const raw = typeof ev.data === 'string' ? ev.data : new Uint8Array(ev.data);
    /* 心跳回执只喂看门狗，不进画面、也不占「首帧=回放」那次判定 */
    if (typeof raw === 'string' && termIsHb(raw)) { termHbReply(ws); return; }
    /* v0.13.64 auth_url 旁路（P2）：服务端把登录 URL 以 JSON 文本帧带外送来。
       必须**在写进画面之前**拦掉 —— 否则 {"type":"auth_url",...} 会被 xterm 当正文
       画到屏幕上（这正是旁路通道存在的理由：绝不污染输出流）。
       同理不能占用 replayFrame 那次判定：它是画面帧，不是控制帧。 */
    if (typeof raw === 'string' && raw.charCodeAt(0) === 123 /* { */) {
      let m = null;
      try { m = JSON.parse(raw); } catch (e) { m = null; }
      /* v0.13.87：两种带外链接帧。type 是**服务端**给的真相 —— 前端只做映射，
         绝不自己按 URL 形状猜「这像不像登录」（v0.13.64 的误报就是这么来的）。 */
      if (m && (m.type === 'auth_url' || m.type === 'page_url') && typeof m.url === 'string') {
        termLinkAnno(m.type === 'auth_url' ? 'auth' : 'page', m.url, !!m.auto);
        return;
      }
    }
    if (replayFrame) {
      replayFrame = false;
      termWriteReplay(raw);   // 回放走闸门：历史里的终端查询不许替它作答
      /* 回放帧的鼠标态分两类（v0.13.84）：
         - 非 shell 画像：TUI 本身**就是 pty 进程**，会话活着 ⇔ TUI 活着
           （死了这条线立刻 4410 收口），ring 尾巴里那份鼠标声明就是现势。
           opencode 开机只断言一次、永不重发（真 PTY 实测）—— 重连时若不从这里
           接住，它的应用内滚动永远回不来；顺带这也治了「新会话无回放、首帧
           被当 replayFrame 跳过扫描」的盲区（claude 红基线里 rep=0 有它一半）。
           want 表照扫，xterm 态按 want 回装；
         - shell 画像维持 v0.8.1 口径：历史里的过期开关不算数
           （bash 里跑完 vim 被 SIGKILL 没发 DECRST，回放会把跟踪态焊死在屏上），
           清掉即可，用户随后开 TUI 由实时帧正常接管。 */
      if (termSidAgent && termSidAgent !== 'shell') {
        termScanMouseMode(termDecodeFrame(raw));
        /* ring 尾巴已被增量帧挤掉时（长会话刷新），从 per-sid want 缓存接住。 */
        if (!termMouseWant.size) {
          termWantCacheGet(sid).forEach(n => termMouseWant.add(n));
          termMouseLive = termMouseWant.size > 0;
        }
        termMouseArm();
      } else {
        term.write(TERM_MOUSE_OFF);
      }
      termHealBlank();   // 回放可能只是 64KB 尾巴里的半屏，见函数注释
      return;
    }
    term.write(raw);
    termScanMouseFrame(termDecodeFrame(raw));
  };
  /* 重连成功后必须跑的仍是原来那三件事（收连接中灯 + 聚焦 + force 重绘报行列），
     之后额外挂上这条线自己的心跳。 */
  ws.onopen = () => { termConnecting(false, ws);
    if (termFocusWanted(opts)) term.focus();   // 自动挂载/重连不抢焦点、不弹软键盘
    termRepaint(true); termHbStart(ws); };
  ws.onclose = ev => {
    if (termWs !== ws) return;  // 旧连接的 close 不污染新会话画面
    termConnecting(false, ws);
    termHbStop();               // 本线心跳随本线收尸；重连成功后由新 socket 重新起一条
    const code = ev.code;
    if (code === 4404 || code === 4410) {   // 已退出/不存在 → 明确提示并刷新列表，绝不重连
      /* 会话已死 ⇒ app 的鼠标意图同归零（pty 本体就是 TUI，这条 close 即死讯，
         SIGKILL 也没有「bash 还活着」的残留窗口）：清 want + 缓存。
         没有这步，之后在同一页里 wheel 会因 live 卡在 true 而被当上报喂给空气——
         「死轮」，比报障原文更难看。 */
      termWantCachePut(termSid, null);
      termMouseWantReset();
      termNotice('[该会话已结束或不存在——点行1 芯片重连，或按「新会话」]');
      termToast('终端会话已结束，请重新打开', 'err');
      termDetach(); termRefreshList();
      return;
    }
    if (code === 4401) {                     // 未鉴权：同一个错口令重连只会一直被拒，停手指路
      termNotice('[鉴权失败（4401）——在「设置」里重新应用 TERM_TOKEN 后再打开终端]');
      termToast('终端鉴权失败（TERM_TOKEN 不符），已停止重连', 'err');
      termDetach();
      return;
    }
    // 1005/1006/1011/1012…：链路断了但 pty 多半还在服务端（ring 会回放）⇒ 退避自动重连
    term.write('\r\n\x1b[90m' + '[连接断开——点行1 芯片重连或新建]' + '\x1b[0m');
    termScheduleReconnect(ws);
  };
  termWs = ws;
}

async function termNew() {
  try {
    const d = await api('/api/term/sessions', { method: 'POST', headers: termHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ agent_id: chatPick }) });
    toast('已拉起 ' + chatPick + ' 终端会话', 'ok');
    termConnect(d.session.id, chatPick, { user: true });
    // 即时可见：不等服务端回写，先把新芯片本地插进去（termConnect 已设 termSid，所以自带 .cur）
    const el = $('termSessList');
    if (el && !el.querySelector('.sess-item[data-sid="' + d.session.id + '"]'))
      el.insertAdjacentHTML('beforeend', termChipHtml(Object.assign({ alive: true, title: '' }, d.session)));
    termRefreshList();   // 再拿服务端清单覆写，DOM 不骗人
  } catch (e) { toast(e.message, 'err'); }
}

/* 芯片模板：行1 内联会话项。v0.13.0 起标签＝历史会话的问题原文（中文），
   取不到标题（刚新建、agent 还没落摘要 / 无 pid 登记表）退显「新会话 MM-DD」。
   字母 sid 只留在 data-sid 里作 DOM 键，用户可见处一律不再出现。 */
/* 芯片点击的具名入口：内联 onclick 只能走全局作用域，所以在这里统一带上 user:true（点击=用户主动，该聚焦） */
function termOpenChip(sid, agent) { termConnect(sid, agent, { user: true }); }

function termChipHtml(s) {
  const label = (s.title && s.title.trim()) ? s.title.trim() : ('新会话 ' + hhTime(Math.floor(s.created)));
  return '<span class="sess-item' + (s.id === termSid ? ' cur' : '') + '" data-sid="' + s.id + '">' +
         '<a href="javascript:void(0)" title="' + escapeHtml(label) + '"' +
         ' onclick="termOpenChip(\'' + s.id + '\',\'' + s.agent_id + '\')"><span class="s-t">' +
         escapeHtml(label) + '</span></a>' +
         '<button class="sess-x" title="关闭此会话" aria-label="关闭此会话" ' +
         'onclick="termKillOne(\'' + s.id + '\')">' + ico('x', 'xs') + '</button></span>';
}

/* P1-7：清单类 GET 的 401 只能报一次 —— termRefreshList 是轮询调用，不去重就会
   反复弹同款错误把界面活埋。拿到过清单就重置，允许下次再错时重新报。 */
let termAuthWarned = false;
function termAuthWarn(e) {
  const m = String((e && e.message) || e || '');
  if (!/401|token/i.test(m)) return;      // 网络错/5xx 不归因到「口令」
  if (termAuthWarned) return;
  termAuthWarned = true;
  toast('终端清单需要 TERM_TOKEN：在「设置」里应用口令后重试', 'err');
}
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
  if (v !== 'catalog' && v !== 'invocations' && v !== 'store' && v !== 'weights') return;
  SKILL_VIEW = v;
  ['catalog', 'invocations', 'store', 'weights'].forEach(k => {
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
function jobKindChange() {
  const k = $('jobKind').value;
  $('jobAgent').style.display = k === 'agent_prompt' ? '' : 'none';
  $('jobPayload').placeholder = k === 'shell' ? '白名单命令，如 echo hello' : k === 'agent_prompt' ? '发给 Agent 的 Prompt' : 'http://127.0.0.1:3102/health';
}
async function loadJobs() {
  try {
    const d = await api('/api/jobs');
    $('jobAllow').textContent = 'shell 白名单: ' + (d.shell_allow || []).join(', ');
    $('jobsBody').innerHTML = (d.jobs || []).map(j =>
      '<tr><td><b>' + escapeHtml(j.name) + '</b><br><span class="hint">' + j.id + '</span></td>' +
      '<td style="font-family:var(--font-mono)">' + j.cron + '</td><td>' + j.kind + '</td>' +
      '<td><button class="btn sm ' + (j.enabled ? '' : 'ghost') + '" onclick="toggleJob(\'' + j.id + '\',' + (j.enabled ? 0 : 1) + ')">' + (j.enabled ? '✔ 启用' : '‖ 停用') + '</button></td>' +
      '<td class="hint">' + (j.last_run || '').slice(5, 16).replace('T', ' ') + '</td>' +
      '<td>' + (j.last_status === 'success' ? ico('check-circle', 'xs') : j.last_status === 'fail' ? ico('alert', 'xs') : '-') + '</td>' +
      '<td style="max-width:220px"><details><summary class="hint">' + escapeHtml((j.last_result || '').slice(0, 30)) + '</summary><pre style="font-size:var(--fs-sm);white-space:pre-wrap">' + escapeHtml(j.last_result || '') + '</pre></details></td>' +
      '<td style="white-space:nowrap"><button class="btn sm ghost" onclick="runJobNow(\'' + j.id + '\')">' + ico('play', 'xs') + '立即</button> ' +
      '<button class="btn sm danger" onclick="delJob(\'' + j.id + '\')">' + ico('x') + '</button></td></tr>').join('') ||
      '<tr><td colspan="8" class="hint">暂无任务</td></tr>';
  } catch (e) { toast(e.message, 'err'); }
}
async function toggleJob(id, en) { try { await api('/api/jobs/' + id, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ enabled: !!en }) }); loadJobs(); } catch (e) { toast(e.message, 'err'); } }
async function runJobNow(id) { toast('执行中…'); try { const d = await api('/api/jobs/' + id + '/run', { method: 'POST' }); toast(d.status === 'success' ? '执行成功: ' + (d.result || '').slice(0, 60) : '执行失败: ' + (d.result || ''), d.status === 'success' ? 'ok' : 'err'); loadJobs(); } catch (e) { toast(e.message, 'err'); } }
async function delJob(id) { if (!confirm('删除任务 ' + id + '？')) return; try { await api('/api/jobs/' + id, { method: 'DELETE' }); loadJobs(); } catch (e) { toast(e.message, 'err'); } }

/* ── S4 MCP 网关 ──────────────────────────────────── */

let mcpToolSel = null;

async function addServer() {
  const body = { name: $('mcName').value.trim(), transport: $('mcTransport').value };
  const argsRaw = $('mcArgs').value.trim();
  if (!body.name) return toast('名称必填', 'err');
  if (body.transport === 'stdio') {
    if (!$('mcCmd').value.trim()) return toast('stdio 需 command', 'err');
    body.command = $('mcCmd').value.trim();
    body.args = argsRaw ? argsRaw.split(/\s+/) : [];
  } else {
    if (!argsRaw) return toast('http 需 URL', 'err');
    body.url = argsRaw;
  }
  try { await api('/mcp/servers', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) }); toast('已注册，聚合刷新中…', 'ok'); $('mcName').value = $('mcCmd').value = $('mcArgs').value = ''; loadMcp(); } catch (e) { toast(e.message, 'err'); }
}
async function probeServer() {
  const cmd = $('mcCmd').value.trim();
  if (!cmd) return toast('先填 command', 'err');
  $('mcProbe').style.display = 'block'; $('mcProbe').textContent = '连接探测…';
  try {
    const d = await api('/mcp/servers/probe', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ command: cmd, args: $('mcArgs').value.trim() ? $('mcArgs').value.trim().split(/\s+/) : [] }) });
    $('mcProbe').textContent = JSON.stringify(d.tools, null, 1);
  } catch (e) { $('mcProbe').textContent = '探测失败: ' + e.message; }
}
async function delServer(id) {
  if (!confirm('删除 MCP server: ' + id + '？（聚合工具列表将随之刷新）')) return;
  try { await api('/mcp/servers/' + id, { method: 'DELETE' }); loadMcp(); } catch (e) { toast(e.message, 'err'); }
}
async function loadMcp() {
  bindChatDelegation();   // v0.13.131：工具行/命令项走 data-* 委托（幂等）
  try {
    const s = await api('/mcp/servers');
    $('mcServers').innerHTML = '<table><thead><tr><th>名称</th><th>传输</th><th>目标</th><th></th></tr></thead><tbody>' +
      (s.servers || []).map(x => '<tr><td><b>' + escapeHtml(x.name) + '</b></td><td>' + x.transport + '</td>' +
        /* v0.13.40：补 white-space:nowrap —— 只写 overflow/text-overflow 而没 nowrap
           时省略号不生效，长 command 会把行撑成三行（1440 截图实测）。父级 .tscroll
           已给横向滚动，兜住超长值。 */
        '<td style="font-family:var(--font-mono);font-size:var(--fs-sm);max-width:240px;overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(x.transport === 'stdio' ? (x.command || '') + ' ' + (x.args || []).join(' ') : x.url || '') + '">' + escapeHtml(x.transport === 'stdio' ? (x.command || '') + ' ' + (x.args || []).join(' ') : x.url || '') + '</td>' +
        '<td><button class="btn sm danger" onclick="delServer(\'' + x.id + '\')">' + ico('x') + '</button></td></tr>').join('') +
      '</tbody></table>';
    $('mcTools').innerHTML = '聚合工具中（stdio 会临时拉起进程）…';
    const t = await api('/mcp/tools');
    const errs = Object.entries(t.errors || {});
    $('mcTools').innerHTML = (t.tools || []).map(x =>
      // v0.13.78：① 描述改走 mdInline（与技能描述同口径）；
      //            ② **工具名 `x.name` 原先既没转义、也没过 jsStr** ——
      //               它同时进了 HTML 正文（`<b>` 内）与 `onclick` 的 JS 字面量，
      //               两个语境都不设防。工具名来自 MCP server（外部注册），
      //               本仓 `jsStr` 的注释早写明「escapeHtml 只处理 HTML 上下文，
      //               HTML 实体转义不足以让任意 id 安全进 JS 字面量」，此处却没用。
      //               现补：`escapeHtml` 管 HTML 语境、`jsStr` 管 JS 字面量语境。
      '<div class="mem-item" style="cursor:pointer" data-sk-pt-server="' + escapeHtml(x.server) + '" data-sk-pt-name="' + escapeHtml(x.name) + '" id="mt_' + escapeHtml(x.server) + '_' + escapeHtml(x.name) + '"><span class="tag agent">' + escapeHtml(x.server_name) + '</span><p><b>' + escapeHtml(x.name) + '</b> <span class="hint">' + mdInline(x.description) + '</span></p></div>').join('') ||
      '<div class="hint">无工具——注册 server 后此处聚合</div>' +
      (errs.length ? '<div class="hint" style="color:var(--danger-text)">异常 server: ' + errs.map(x => x[0] + '(' + x[1].slice(0, 40) + ')').join('; ') + '</div>' : '');
  } catch (e) { $('mcTools').innerHTML = '<span style="color:var(--danger-text)">' + e.message + '</span>'; }
}
/* v0.13.131（PT-20261019-02）：MCP 工具行与 ⌘K 命令项改 data-* + 委托。
 *
 * 原形态 `onclick="pickTool(<jsStr>…)"` / `onclick="cmdGo(<jsStr>)"`：jsStr 出双引号
 * 字面量，被双引号界定的属性提前截断 ⇒ 点击静默 SyntaxError（只有控制台可见）。
 * data-* 值走 escapeHtml，dataset 读回原值。#mcTools / #cmdList 都是静态容器，
 * innerHTML 反复重写不掉监听，故 marker 防重复挂。
 * pickTool 双参数拆两个 dataset（工具名来自外部 MCP server，两个语境都不设防过）。 */
function bindChatDelegation() {
  const tools = $('mcTools');
  if (tools && !tools.__chatDeleg) {
    tools.__chatDeleg = true;
    tools.addEventListener('click', e => {
      const t = e.target.closest('[data-sk-pt-server]');
      if (t && tools.contains(t)) pickTool(t.dataset.skPtServer || '', t.dataset.skPtName || '');
    });
  }
  const cmd = $('cmdList');
  if (cmd && !cmd.__cmdDeleg) {
    cmd.__cmdDeleg = true;
    cmd.addEventListener('click', e => {
      const c = e.target.closest('[data-sk-cmdgo]');
      if (c && cmd.contains(c)) cmdGo(c.dataset.skCmdgo || '');
    });
  }
}

function pickTool(server, tool) {
  mcpToolSel = { server: server, tool: tool };
  document.querySelectorAll('[id^=mt_]').forEach(el => el.style.background = '');
  const el = document.getElementById('mt_' + server + '_' + tool);
  if (el) el.style.background = 'var(--surface-2)';
}

/* ── T3 MCP ACL 管理（契约见 src/mcpgw.py：POST {agent_id, tool_pattern, server_id?, allow}；GET {rules:[{id,agent_id,server_id,tool_pattern,allow}]}；DELETE /mcp/acl/{id}）── */
async function loadAcl() {
  const el = $('aclList');
  if (!el) return;
  try {
    const d = await api('/mcp/acl');
    el.innerHTML = (d.rules || []).map(r =>
      '<div class="mem-item"><span class="tag ' + (r.allow ? 'fact' : 'constraint') + '">' + (r.allow ? 'allow' : 'deny') + '</span>' +
      '<p><b>' + escapeHtml(r.agent_id) + '</b> · <code>' + escapeHtml(r.tool_pattern) + '</code>' +
      (r.server_id ? ' · server=' + escapeHtml(r.server_id) : ' · 任意server') + '</p>' +
      '<button class="btn sm danger" aria-label="删除规则 ' + r.id + '" onclick="delAcl(' + r.id + ')">' + ico('x') + '</button></div>').join('') ||
      '<div class="hint">暂无规则——<b>零规则行时</b>所有 agent 默认放行（首次接入零摩擦）；'
      + '但只要存在任何一条适用规则（<b>含 <code>*</code> 通配行</b>），即进入白名单语义：未覆盖即拒，且 deny 优先。</div>';
  } catch (e) { el.innerHTML = '<span style="color:var(--danger-text)">' + escapeHtml(e.message) + '</span>'; }
}
async function addAcl() {
  const body = {
    agent_id: $('aclAgent').value.trim(),
    tool_pattern: $('aclPattern').value.trim(),
    allow: $('aclAllow').value === '1'
  };
  const sid = $('aclServer').value.trim();
  if (sid) body.server_id = sid;  // 后端按 id 或 name 解析（mcpgw.add_acl）
  if (!body.agent_id || !body.tool_pattern) return toast('agent_id 与工具模式必填', 'err');
  try {
    await api('/mcp/acl', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(body) });
    toast('ACL 规则已添加', 'ok');
    $('aclAgent').value = $('aclPattern').value = $('aclServer').value = '';
    loadAcl();
  } catch (e) { toast(e.message, 'err'); }
}
async function delAcl(id) {
  try { await api('/mcp/acl/' + id, { method: 'DELETE' }); toast('规则已删除', 'ok'); loadAcl(); }
  catch (e) { toast(e.message, 'err'); }
}
async function callToolSel() {
  if (!mcpToolSel) return toast('先点击选择一个工具', 'err');
  let args = {};
  const raw = $('mcCallArgs').value.trim();
  if (raw) { try { args = JSON.parse(raw); } catch (e) { return toast('args 需为 JSON', 'err'); } }
  $('mcCallOut').style.display = 'block'; $('mcCallOut').textContent = '调用中…';
  try {
    const d = await api('/mcp/call', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ server: mcpToolSel.server, tool: mcpToolSel.tool, args: args, agent_id: 'manager' }) });
    $('mcCallOut').textContent = JSON.stringify(d, null, 1);
  } catch (e) { $('mcCallOut').textContent = '失败: ' + e.message; }
}

/* ── T8 命令面板（⌘K/Ctrl+K）：搜实体直达工作台 ── */
function openCmd() {
  bindChatDelegation();   // v0.13.131：⌘K 列表项走 data-* 委托（幂等）
  $('cmdMask').classList.add('on');
  const i = $('cmdInput');
  i.value = '';
  renderCmdList('');
  i.focus();
}
function closeCmd() { $('cmdMask').classList.remove('on'); }
function renderCmdList(q) {
  const box = $('cmdList');
  // 模糊：派发时 agent 名与 id 最常被手误（`claud`/`opencode-e`）。
  const items = AGENTS.map(a => ({ a, score: fuzzyMatch([[a.id, 3], [a.name, 2]], q) }))
    .filter(x => !q || x.score > 0)
    .sort((x, y) => (y.score - x.score) || String(x.a.name || '').localeCompare(String(y.a.name || '')))
    .slice(0, 12).map(x => x.a);
  box.innerHTML = items.map(a =>
    '<div class="cmd-item" data-sk-cmdgo="' + escapeHtml(a.id) + '"><span>' + escapeHtml(a.name) + '</span><span class="hint">' + escapeHtml(a.id) + '</span></div>').join('') ||
    '<div class="hint" style="padding:8px">无匹配实体</div>';
}
function cmdGo(id) { closeCmd(); gotoChat(id); }

/* ── 启动 ─────────────────────────────────────────── */

/* ── v0.7 左侧手风琴导航：单开模式 + 搜索 + 展开态持久化 ──
   20 个实体全部收拢进左栏（AGENTS 8 / 基础设施 12），系统功能仍走 go(page) ── */
const NAV_GROUPS = { agents: 'AGENTS', infra: '基础设施', system: '系统', settings: '设置' };
const NAV_ICONS = { agents: 'hubmark', infra: 'server', system: 'sliders', settings: 'settings' };   // 收起成图标条时仍可辨认（sprite id）
const NAV_ORDER = ['agents', 'infra', 'system', 'settings'];
const NAV_SUB_KINDS = [['gateway', '网关'], ['service', '服务'], ['tool', '工具'], ['memory', '记忆']];
const SYS_PAGES = [['ports', '端口', 'share'], ['telemetry', '遥测', 'activity'], ['memory', '记忆中心', 'database'],
                   ['skills', '技能中心', 'zap'], ['kb', '知识库', 'book'],
                   ['mcp', '工具', 'wrench'], ['jobs', '定时', 'clock'], ['tasks', '协同', 'flow'],
                   ['assets', '资产', 'layers']];   // v0.13.47：运行日志页删除，内容并入设置→日志
const MODE_LABEL = { embed: '嵌入', term: '终端', chat: '对话', detail: '详情', open: '新窗口' };
/* v0.13.43 设置子菜单：与系统页同形态（data-sys → go(page) ⇒ 正文出页、窄屏自动收侧栏），
   只是单独成组挂在「系统」之下；这三项此前是右侧抽屉里的三个 tab。 */
const SET_PAGES = [['settings-model', '模型', 'cpu'], ['settings-github', 'GitHub', 'globe'],
                   ['settings-memory', '记忆存储', 'database'],
                   ['settings-token', '终端口令', 'terminal'], ['settings-logs', '日志', 'activity']];
const PAGE_LABELS = { classroom: '总览', chat: '统一对话', tasks: '协同', jobs: '定时',
                      memory: '记忆中心', skills: '技能中心', kb: '知识库', mcp: '工具', ports: '端口', telemetry: '遥测',
                      assets: '资产', localprojects: '本机项目', github: 'GitHub 项目', resources: '资源监控',
                      'settings-model': '设置 · 模型', 'settings-github': '设置 · GitHub',
                      'settings-token': '设置 · 终端口令', 'settings-memory': '设置 · 记忆存储',
                      'settings-logs': '设置 · 日志' };
const navOpenStored = lsGet('hub.nav.open');
let navOpen = navOpenStored === null ? 'agents' : navOpenStored;   // 首屏默认展开 AGENTS；'' = 用户主动全收起
let curPage = '';
/* 排序：异常置顶 > running > installed > stopped，同级按名称。
   P1-20：异常档改用 agentHealth()（vitsals 实测 verdict）而不是 a.status。
   旧口径 NAV_RANK.error 这一档实际永远命中不到 —— vitals 不把 status 打成
   'error'，于是"启动异常"的 Agent 照样按 running 排在中间，异常项被埋在列表里，
   与顶栏那个恒为 0 的异常计数是同一个病根（判据取了一个永远不取该值的字段）。 */
const NAV_RANK = { running: 1, installed: 2, stopped: 3 };
function navRank(a) {
  if (typeof agentHealth === 'function' && agentHealth(a).bad) return 0;
  return NAV_RANK[a.status] == null ? 9 : NAV_RANK[a.status];
}
function navMatch(a, q) {
  // 侧栏搜索：与全站其他搜索框同一口径（fuzzyMatch：精确恒 1000 压倒近似）。
  return fuzzyMatch([[a.name, 3], [a.id, 2], [a.port, 1]], q) > 0;
}
// 搜索词高亮
/* P1-22（2026-09-30）：高亮改在**转义之后**的串上匹配。
   旧写法拿原文下标 idx 去切 escapeHtml(text)：
     text = 'a & b'，查 '&' → 原文 idx=2，切转义串得到 'a &' 之类错位，
   含 & < > " ' 时高亮会盖错字符（轻则高亮到半个实体，重则切出 < 破坏 HTML）。
   正确做法：先转义成最终 HTML，再在这份 HTML 上匹配 —— 但那样查询词里的
   & 也要跟着转义才搜得到，所以两边各自转义后再比。 */
function hlMatch(text, q) {
  if (!q) return escapeHtml(text);
  const escaped = escapeHtml(text);
  const needle = escapeHtml(q);
  if (!needle) return escaped;
  const hay = escaped.toLowerCase(), nd = needle.toLowerCase();
  let result = '', last = 0, idx = hay.indexOf(nd);
  while (idx !== -1) {
    result += escaped.slice(last, idx) + '<mark>' + escaped.slice(idx, idx + needle.length) + '</mark>';
    last = idx + needle.length;
    idx = hay.indexOf(nd, last);
  }
  return result + escaped.slice(last);
}
function navItemHtml(a) {
  const st = seatStateOf(a);
  const on = (curPage === 'chat' && a.id === chatPick) ? ' on' : '';
  // 窄栏里名称会截断，title 里给全量信息（名称 · 状态 · 端口 · 描述）
  const tip = a.name + ' · ' + seatLabelOf(a) + (a.port ? ' · :' + a.port : '') +
    (a.verdict_reason ? '\n' + a.verdict_reason : '') +
    (a.description ? '\n' + a.description : '');
  // 搜索高亮：名称用 hlMatch，ID 和端口保持原样
  const nameHtml = hlMatch(a.name || a.id, ($('navSearch') ? $('navSearch').value.trim() : ''));
  // 操作按钮：按 entries 类型渲染（最多3个）
  const es = a.entries || [];
  const actBtns = [];
  if (es.some(e => e.type === 'embed')) actBtns.push('<span class="act-btn" onclick="event.stopPropagation();gotoChat(\'' + a.id + '\',\'embed\')" title="嵌入会话">' + ico('monitor') + '</span>');
  if (es.some(e => e.type === 'term')) actBtns.push('<span class="act-btn" onclick="event.stopPropagation();gotoChat(\'' + a.id + '\',\'term\')" title="终端">' + ico('terminal') + '</span>');
  /* v0.10.1：删掉「对话」图标。智管对话（hub 自带会话页）已在 v0.10 移除，profile 里残留的
     chat entry 实测为死入口：claude / jcode 两个实体 POST /api/agents/{id}/chat 均 HTTP 500。
     留着它 = 坏入口（比没入口更糟），且让无 Web UI 的 Agent 行多出第三个图标。 */
  // 启动按钮：installed/stopped 状态的 agent
  if (a.kind === 'agent' && (st === 'installed' || st === 'stopped') && es.some(e => e.type === 'term')) {
    actBtns.push('<span class="act-btn start-btn" onclick="event.stopPropagation();startAgent(\'' + a.id + '\')" title="启动">' + ico('play') + '</span>');
  }
  const actsHtml = actBtns.length ? '<span class="nav-acts">' + actBtns.join('') + '</span>' : '';
  /* P2-B：行尾忙碌点。有活着的终端会话才亮（12-activity 的 ACTIVITY 口径），
     无会话返回空串 ⇒ 零会话时侧栏与改动前逐像素一致。 */
  const busyDot = (typeof activityDotHtml === 'function') ? activityDotHtml(a) : '';
  return '<button class="nav-item' + on + '" data-entity="' + escapeHtml(a.id) + '"' +
    ' title="' + escapeHtml(tip) + '" aria-label="' + escapeHtml(a.name + ' ' + seatLabelOf(a)) + '">' +
    '<span class="s-badge ' + st + '"></span>' +
    '<span class="lbl">' + nameHtml + '</span>' +
    (a.port ? '<span class="nav-port">:' + a.port + '</span>' : '') +
    busyDot + actsHtml + '</button>';
}
/* v0.12.3：行内状态文字（.nav-st）与其横向滚动窗（rollNavStatus / st-roll）已按用户要求整体删除。
   行内只留 .s-badge 方块表达在线/离线；完整状态串在上面的 tip（hover）与工作台卡片里给。 */
// 启动 agent：创建新的终端会话
async function startAgent(id) {
  try {
    const d = await api('/api/term/sessions', { method: 'POST', headers: termHeaders({ 'Content-Type': 'application/json' }), body: JSON.stringify({ agent_id: id }) });
    toast('已拉起 ' + id + ' 终端', 'ok');
    gotoChat(id, 'term');
    setTimeout(() => termConnect(d.session.id, id, { user: true }), 100);
  } catch (e) { toast(e.message, 'err'); }
}
/* ── v0.13.0 左侧历史下拉：同一时刻只展开一个 agent（与 navOpen 手风琴同构，D3）。
   histOpen 进 localStorage；数据缓存在 HIST —— 30s loadAgents 重绘时不闪空白。 ── */
const HIST_LIMIT = 8;                                   // v0.13.49：每 agent 8 条（原 5 条）
/* ⚠ 声明位置是硬约束，不许往下挪：下面 histBootstrap() 是**顶层 IIFE**，会同步走
histLoad() → renderNav() → 读本变量。09-23 自研 APP 事故就是顺序被破坏：手机那份
localStorage 有 hub.term.token ⇒ 早期路径被激活，而 let 声明在 renderNav 之后 ⇒
抛 ReferenceError: Cannot access '_navHtml' before initialization（hub.js:1907），
hub.js 当场死亡 ⇒ 菜单空白 + initSidebar 从未执行 + 抽屉停在展开态遮住正文。
浏览器那份没有 hub.term.token，走不到这条路 ⇒ 这就是「局域网正常/Tailscale 异常」
的真判据（不是缓存、不是网络、不是 origin 的 IP 段）。
闸门：tests/test_tdz_order.py（静态扫同类顺序违规；红基线取修复前的 git 版本）。 */
let _navHtml = '';   // 上一次渲染的菜单 HTML，用于跳过无变化的重写
const TERM_HIST_AGENTS = ['grok', 'claude', 'jcode', 'hermes', 'codex', 'qoder', 'opencode', 'cursor', 'codebuddy'];   // 与后端 SESSION_STORES 同集合
// ★窄屏首屏**不恢复**上次的历史展开项。`hub.hist` 是按 origin 隔离的存量，一旦参与
// 首屏判定，同一个动作在不同入口（局域网 IP / Tailscale IP）就会走出不同结果：
// 09-23 23:3x 四格实测 —— hist 空 ⇒ 点 agent 名称只展开列表、侧栏不收起；
// hist='claude' ⇒ 点名称即收起侧栏。两个 origin 各自一致、彼此不同 ⇒ 差异纯属存量，
// 与网络/Tailscale 无关。闸门：tests/verify_collapse_symmetry.py。
let histOpen = hubNarrow() ? '' : (lsGet('hub.hist') || '');
const HIST = {};                                        // agent_id -> {items,note,loading,err}

function hhTime(ts) {                                   // 绝对日期：相对时间每轮变化会破 DOM diff
  // v0.13.49：**不再带 HH:MM**。侧栏只有 240px 可用宽，时间占的那 5 个字符直接从
  // 摘要里抢走一小半可视长度（实测标题被挤到 0 宽）；挑哪条续聊也不靠天数内的钟点，
  // 日期足够定位。精确到分钟的信息没丢——它还在 title 悬停里。
  if (!ts) return '';
  const d = new Date(ts * 1000), p = n => String(n).padStart(2, '0');
  return p(d.getMonth() + 1) + '-' + p(d.getDate());
}

/* 跳目录之后，同名会话可能来自不同工程：只在「不属于画像目录」时补一个目录尾名，
   画像目录本身不标（多数条目都是它，标了反而吵）。 */
function hhDir(cwd, home) {
  if (!cwd || !home || cwd === home) return '';
  const seg = String(cwd).replace(/\/+$/, '').split('/').filter(Boolean);
  return seg.length ? seg[seg.length - 1] : '';
}

function histHtml(aid) {
  const h = HIST[aid];
  if (!h || h.loading) return '<div class="nav-hist"><div class="hh-note">读取历史…</div></div>';
  if (h.err) return '<div class="nav-hist"><div class="hh-note">历史读取失败：' + escapeHtml(h.err) + '</div></div>';
  const rows = (h.items || []).map(it =>
    '<div class="hh-row" data-agent="' + escapeHtml(aid) + '" data-sid="' + escapeHtml(it.id) + '"' +
    ' title="' + escapeHtml(it.title) + (it.cwd ? ' ｜ ' + escapeHtml(it.cwd) : '') + '">' +
    '<span class="hh-t">' + escapeHtml(it.title) + '</span>' +
    '<span class="hh-cw">' + hhDir(it.cwd, h.home) + '</span>' +
    '<span class="hh-ts">' + hhTime(it.ts) + '</span></div>').join('');
  /* v0.13.49：删掉「历史会话（N）」标题行。
     侧栏 240px 里它白白吃掉一行高度，而且它宣告的信息（这是历史、共几条）
     从「行本身就是会话条目 + 一共就这几行」已经自己说清了；条数还能从行尾日期
     与滚动位置看出来。留着它只会把 8 条记录往下顶。 */
  // note 在“已经有行”时也要显：截断/降级被吞掉的话，残缺结果看着就像完整清单
  // （09-22 jcode 只显 1 条那次，正是“扫描窗口用尽”的 note 没人看见）。
  const tail = (rows && h.note) ? '<div class="hh-note">' + escapeHtml(h.note) + '</div>' : '';
  return '<div class="nav-hist">' +
         (rows || '<div class="hh-note">' + escapeHtml(h.note || '该目录暂无可续会话') + '</div>') + tail + '</div>';
}

function histLoad(aid) {
  HIST[aid] = { items: [], note: '', loading: true, err: '' };
  renderNav();
  api('/api/term/history/' + encodeURIComponent(aid) + '?limit=' + HIST_LIMIT, { headers: termHeaders() })
    .then(d => { HIST[aid] = { items: d.items || [], note: d.note || '', home: d.cwd || '', loading: false, err: '' }; })
    .catch(e => { const m = String((e && e.message) || e);
      // 新前端 + 未重启的旧后端＝路由不存在（FastAPI 回 Not Found）。说人话，别抛生涩 404。
      // ⚠️ 单元名 2026-10-08 起是 agenthub.service；写旧名的后果是教用户去重启一个
      // 已停用的单元（restart 它既起不来又会在 journald 里刷重试），等于给错药方。
      HIST[aid] = { items: [], note: '', loading: false,
                    err: /not\s*found|404/i.test(m) ? '后端未更新：需重启 agenthub.service 后生效' : m }; })
    .then(() => renderNav());                            // 无 finally 依赖：老 Safari 也走得到
}

/* 刷新后 histOpen 会从 localStorage 复原，但 HIST 缓存是空的——不补一次拉取，下拉就
   永远停在「读取历史…」（实测 reload 必现）。没存过 token 时干脆收起：留个展开空壳更误导，
   而且 termToken() 会在每次刷新都弹一次口令框。 */
(function histBootstrap() {
  if (!histOpen) return;
  if (!TERM_HIST_AGENTS.includes(histOpen) || !lsGet('hub.term.token')) {
    histOpen = '';
    lsRemove('hub.hist');
    return;
  }
  histLoad(histOpen);
})();

async function termResume(agentId, sid) {
  try {
    const d = await api('/api/term/sessions', { method: 'POST',
      headers: termHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ agent_id: agentId, session_id: sid }) });
    toast('已在终端里续聊该历史会话', 'ok');
    gotoChat(agentId, 'term');
    termConnect(d.session.id, agentId, { user: true });
    termRefreshList();
  } catch (e) { toast('续聊失败：' + e.message, 'err'); }
}

/* 菜单行 = 实体行本体 + （命中展开项时）历史块。renderNav 的三分支 map 统一走这里。 */
function navRow(a) { return navItemHtml(a) + (a.id === histOpen ? histHtml(a.id) : ''); }

function renderNav() {
  const box = $('navTree');
  if (!box) return;
  const q = ($('navSearch') ? $('navSearch').value.trim() : '').toLowerCase();
  const all = AGENTS.filter(a => a.kind === 'agent');
  const infra = AGENTS.filter(a => a.kind !== 'agent');
  const lists = {
    agents: q ? all.filter(a => navMatch(a, q)) : all.slice().sort((x, y) => navRank(x) - navRank(y) || String(x.name||'').localeCompare(String(y.name||''), 'zh')),
    infra: q ? infra.filter(a => navMatch(a, q)) : infra.slice().sort((x, y) => navRank(x) - navRank(y) || String(x.name||'').localeCompare(String(y.name||''), 'zh')),
    system: q ? [] : SYS_PAGES,
    settings: q ? [] : SET_PAGES,   // v0.13.43：设置组（模型 / GitHub / 终端口令）
  };
  // 搜索结果计数
  let totalMatch = 0;
  if (q) {
    totalMatch = all.filter(a => navMatch(a, q)).length + infra.filter(a => navMatch(a, q)).length;
  }
  const html = NAV_ORDER.map(g => {
    const list = lists[g];
    if (q && !list.length) return '';
    const open = q ? true : navOpen === g;
    let body = '';
    if (g === 'infra') {
      NAV_SUB_KINDS.forEach(([k, label]) => {
        const sub = list.filter(a => a.kind === k);
        if (sub.length) body += '<div class="nav-sub">' + label + '</div>' + sub.map(navRow).join('');
      });
      const rest = list.filter(a => !NAV_SUB_KINDS.some(([k]) => k === a.kind));
      if (rest.length) body += '<div class="nav-sub">其他</div>' + rest.map(navRow).join('');
    } else if (g === 'system' || g === 'settings') {
      /* 设置组与系统页同渲染（data-sys ⇒ 侧栏委托 go(page)），只是不挂健康点。 */
      /* v0.13.27：三中心（memory/skills/kb）行尾挂健康点（CENTER_HEALTH，
         loader 完成时写入；空=未加载不显示，ok/warn/err 对应 s-badge 色族）。 */
      const hlth = p => (typeof CENTER_HEALTH !== 'undefined' && CENTER_HEALTH[p]) ?
        '<span class="s-badge ' + (CENTER_HEALTH[p] === 'ok' ? 'running' : CENTER_HEALTH[p] === 'warn' ? 'installed' : 'error') + '"></span>' : '';
      const HLTH_PAGE = { memory: 1, skills: 1, kb: 1 };
      /* P1-18：补 #badge-<page> 挂载点。setBadge 一直在写这两个元素的 textContent，
         此前模板里不存在 ⇒ 每次都 el 为 null 直接 return，MCP 服务器数 / 启用中的
         定时任务数算了但永远显示不出来。空内容靠 CSS :empty 不占位。 */
      const badge = p => '<span class="nav-badge" id="badge-' + p + '"></span>';
      body = list.map(([p, label, ic]) =>
        // v0.13.74：补 title/aria-label。收起态（窄屏 48~52px 图标条）下 .lbl 是
        // display:none，**手机上又没有 hover** ⇒ 这些图标此前既没有可见文字、
        // 也没有可访问名，屏幕阅读器只能读出「按钮」。label 直接复用现成的变量，
        // 不新建映射（PAGE_LABELS 才是页名→中文名的唯一真源）。
        '<button class="nav-item' + (curPage === p ? ' on' : '') + '" data-sys="' + p + '"' +
        ' title="' + escapeHtml(label) + '" aria-label="' + escapeHtml(label) + '">' +
        ico(ic) + '<span class="lbl">' + label + '</span>' + (HLTH_PAGE[p] ? hlth(p) : '') +
        badge(p) + '</button>').join('');
    } else {
      body = list.map(navRow).join('');
    }
    if (!body) body = '<div class="nav-empty">' + (AGENTS.length ? '无匹配' : '加载中…') + '</div>';
    return '<div class="nav-acc' + (open ? ' open' : '') + '">' +
      // v0.13.74：同上，收起态下手风琴头也只有图标，补 title/aria-label。
      '<button class="nav-acc-head" data-group="' + g + '" aria-expanded="' + (open ? 'true' : 'false') + '"' +
      ' title="' + escapeHtml(NAV_GROUPS[g]) + '" aria-label="' + escapeHtml(NAV_GROUPS[g]) + '">' +
      ico(open ? 'chevron-down' : 'chevron-right', null, 'caret') +
      ico(NAV_ICONS[g], 'md') +
      '<span class="lbl">' + NAV_GROUPS[g] + '</span>' +
      '<span class="badge">' + list.length + '</span></button>' +
      '<div class="nav-acc-body">' + body + '</div></div>';
  }).join('');
  /* 内容没变就不重写 DOM —— loadAgents 每 30s 刷一次，重写会把 #navTree 的滚动位置弹回顶部 */
  if (html !== _navHtml) { box.innerHTML = html; _navHtml = html; }
  // 搜索结果计数提示
  const countEl = $('navSearchCount');
  if (countEl) countEl.textContent = q ? ' 共 ' + totalMatch + ' 项' : '';
}
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
// T8：命令面板输入/键盘
$('cmdInput').addEventListener('input', e => renderCmdList(e.target.value.trim()));
$('cmdInput').addEventListener('keydown', e => {
  if (e.key === 'Enter') {
    e.preventDefault();
    const q = e.target.value.trim();
    const first = $('cmdList').querySelector('.cmd-item');
    if (first && !q) closeCmd(); else if (first) first.click();
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
/* ══════════════════ P4 资产面板（只读门面聚合）══════════════════
   契约源：src/memory.py（/api/memory/*）、src/kb.py（/api/kb/*）、src/skill.py（/api/skill/*）。
   口径（09-24 架构裁定）：hub 只做**门面 + ACL + 审计 + 前端**，本页零写操作、零"第二权威副本"。

   ★ 为什么这个文件里最讲究的是"四态可区分"而不是好看：
     ok / empty / degraded / down 四态里，只要把后两种渲染成"暂无数据"，
     用户就会以为本机资产是空的 —— 这正是 ccpocket-bridge「active、端口在听、/health ok
     而会话起不来，静默 21 天」那一族故障在前端层的形态。闸门
     tests/test_asset_panel.py 用**抽出来的真函数**喂 degraded 与 404 两种载荷钉死这条。

   ★ 施工约束（09-23 分档五不变量 + 浮层三条）：
     - 不新增 matchMedia（断点唯一真相在 01 的 HUB_NARROW_MQ）；
     - 不裸用 localStorage（一律 lsGet/lsSet）；
     - 侧栏/菜单内禁 inline onclick ⇒ 本页交互全走 data-asset 委托，且入口函数
       initAssetPanel() 由 go('assets') 懒调用（不用顶层 IIFE：v0.13.11 的 TDZ 教训）。 */

/* 顶层状态位故意用 `var`：本分片在 hub.js 里排在 01 之后，而 01 的 go() 会调到
   本文件的函数。`let/const` 在语句执行到之前是 TDZ（v0.13.11 的 `_navHtml` 白屏
   就是这一型），而 `function` 声明整体提升 ⇒ 下面的常量表一律做成函数。 */
var assetsBound = false;   // 事件委托只绑一次（go() 会反复进本页）

function ASSET_SOURCES() {
  return [
  {
    key: 'memory', label: '记忆', box: 'assetMemory', needsQ: true,
    path: q => '/api/memory/search?q=' + encodeURIComponent(q) + '&limit=6',
    items: d => d.memories || [],
    itemText: it => it.content || it.summary || it.text || '',
    itemMeta: it => [it.category || it.type, it.agent || it.source, it.created_at ? String(it.created_at).slice(0, 10) : ''].filter(Boolean).join(' · '),
    status: d => (d.engines && d.engines.memory) || d.engine || '',
  },
  {
    key: 'kb', label: '技术文档', box: 'assetKb', needsQ: true,
    path: q => '/api/kb/search?q=' + encodeURIComponent(q) + '&k=6',
    items: d => d.results || [],
    itemText: it => it.snippet || it.text || it.title || '',
    itemMeta: it => [it.path || it.url, it.chunk ? '#' + it.chunk : ''].filter(Boolean).join(''),
    status: d => d.engine || '',
  },
  {
    key: 'skill', label: '技能', box: 'assetSkill', needsQ: false,
    path: q => '/api/skill/list?limit=6' + (q ? ('&q=' + encodeURIComponent(q)) : ''),
    items: d => d.items || [],
    itemText: it => it.name + (it.description ? (' — ' + it.description) : ''),
    itemMeta: it => [it.route, it.dir ? (it.dir.split('/').slice(-2).join('/')) : ''].filter(Boolean).join(' · '),
    status: d => (d.engine || 'disk'),
  },
  ];
}

/* 第五态 idle：某些门面端点按契约 **缺 q 就回 422**（memory/kb 检索）。
   空查询时发一个注定 422 的请求、再把后端校验错报成「部分后端不可用」，
   技术上诚实但语义误导（用户什么都没输）。idle 单独一态，不发请求。 */
function assetIdleHtml(src) {
  return '<div class="hint">「' + escapeHtml(src.label) + '」这一路需要检索词（端点缺 q 按契约回 422）。'
    + '在上方输入关键词后点「检索」。</div>';
}

/* 四态之一：ok / empty / degraded / down。判定顺序即优先级——坏消息不许被"空"掩盖。 */
function assetStateOf(d, err) {
  if (err) return /HTTP 404/.test(String(err.message)) ? 'down' : 'degraded';
  if (!d) return 'down';
  // 后端自己报的 degraded 与逐路 backends[].ok 取并集（有一路不算账就算降级）
  const bad = (d.backends || []).some(b => !b.ok) || (d.degraded || []).length > 0;
  if (bad) return 'degraded';
  return (d.count || 0) ? 'ok' : 'empty';
}

/* 状态文案与颜色同样做成函数（见上方 TDZ 注释）。
   只用主题里**真存在**的 token（--ok / --muted / --warn / --danger-text）：
   不存在的 var() 会静默退化成继承色 ⇒ 警告条变普通文字，又是新的静默不可用。 */
function assetStateText(state) {
  return ({ ok: '有结果', empty: '确实零命中', idle: '待输入检索词',
            unwired: '按设计未接入',
            degraded: '部分后端不可用', down: '端点不可用' })[state] || state;
}

function assetStateColor(state) {
  return ({ ok: 'var(--ok)', empty: 'var(--muted)', idle: 'var(--muted)',
            unwired: 'var(--muted)',
            degraded: 'var(--warn)', down: 'var(--danger-text)' })[state] || 'var(--muted)';
}

function assetBadge(src, state, note) {
  return '<div class="chip" style="min-width:0">' +
    '<b style="color:' + assetStateColor(state) + '">' + escapeHtml(src.label) +
    '：' + escapeHtml(assetStateText(state)) + '</b>' +
    (note ? '<span class="hint">' + escapeHtml(note) + '</span>' : '') + '</div>';
}

function assetResultsHtml(src, d, state, err) {
  if (state === 'down') {
    return '<span style="color:var(--danger-text)">该门面端点取不到（' +
      escapeHtml(err ? String(err.message).slice(0, 120) : 'HTTP 404') +
      '）。<b>这不是"没有资产"</b>——多半是 hub 代码比进程新（未重启）或后端未启用。</span>';
  }
  if (state === 'degraded') {
    /* ★ 红向修正：降级态以前会落到 empty 分支里说「确实没有命中」，
       那恰好把「查不了」糊成「没有」——就是 09-22 那族静默不可用。 */
    return '<div class="hint" style="color:var(--warn)"><b>部分后端不可用</b>：'
      + '下面的清单<b>不完整</b>，不能当作「没有资产」。</div>'
      + (d && d.note ? '<div class="hint" style="color:var(--warn)">' + escapeHtml(d.note) + '</div>' : '')
      + (d ? backendLedger(d) : '')
      + (d && src.items(d).length ? itemListHtml(src, src.items(d))
                                  : '<div class="hint">可用后端本次未返回条目。</div>');
  }
  if (state === 'empty') {
    return (d && d.note ? '<div class="hint" style="color:var(--warn)">' + escapeHtml(d.note) + '</div>' : '')
      + backendLedger(d) +
      '<div class="hint">后端全部在线，这条改写查询确实没有命中 —— 换个说法再试。</div>';
  }
  const items = d ? src.items(d) : [];
  return (d && d.note ? '<div class="hint" style="color:var(--warn)">' + escapeHtml(d.note) + '</div>' : '')
    + backendLedger(d) + itemListHtml(src, items);
}

/* 逐路 backends[] 台账：路名 + 成败 + 条数 + 耗时 + 错因（错因截 60 字防溦屏） */
function backendLedger(d) {
  const backs = ((d && d.backends) || []).map(b =>
    '<span class="hint" style="margin-right:8px">' + escapeHtml(b.name) +
    (b.ok ? '✓' : '✗') + ' ' + (b.count == null ? '-' : b.count) + '条 ' + (b.ms == null ? '' : b.ms + 'ms') +
    (b.ok ? '' : (' <span style="color:var(--danger-text)">' + escapeHtml(String(b.error || '')).slice(0, 60) + '</span>')) +
    '</span>').join('');
  return backs ? '<div style="margin-bottom:6px">' + backs + '</div>' : '';
}

function itemListHtml(src, items) {
  if (!items || !items.length) return '<div class="hint">本次无条目。</div>';
  return '<div class="mem-list">' + items.map(it =>
    '<div class="mem-item"><p>' + escapeHtml(String(src.itemText(it)).slice(0, 300)) +
    (src.itemMeta(it) ? '<br><span class="hint">' + escapeHtml(src.itemMeta(it)) + '</span>' : '') +
    '</p></div>').join('') + '</div>';
}

async function loadAssetStatus() {
  const box = $('assetStatus');
  if (!box) return;
  const probes = [
    { label: '技能', path: '/api/skill/status', kind: 'status' },
    { label: '知识', path: '/api/kb/status', kind: 'status' },
    { label: '记忆', path: '/health', kind: 'health' },
  ];
  const settled = await Promise.allSettled(probes.map(p => api(p.path)));
  const chips = settled.map((r, i) => {
    const p = probes[i];
    if (r.status === 'rejected') {
      const st = /HTTP 404/.test(String(r.reason.message)) ? 'down' : 'degraded';
      return assetBadge({ label: p.label }, st, String(r.reason.message).slice(0, 60));
    }
    const d = r.value || {};
    if (p.kind === 'health') {
      const mb = d.memory_backend || {};
      const st = mb.state === 'ok' ? 'ok' : (mb.state ? 'degraded' : 'down');
      return assetBadge({ label: '记忆' }, st,
        mb.state ? (mb.state + ' L1=' + mb.l1_total + ' L0=' + mb.l0_total + (mb.stale ? ' stale' : '')) : '字段缺失（进程旧于代码）');
    }
    /* ★ 关键区分：`available:false` 开 **带 why** 的是设汁裁定（如 kb 的 wigolo、skill 的 TDAI 注册表），
       不是故障；带 error 的才是真挂了。把前者涂成黄色告警，首屏就是一屏假故障——
       那会把真故障埋掉（09-22 “静默不可用”的反面：吼叫式假告警）。 */
    const off = Object.values(d).filter(v => v && typeof v === 'object' && v.available === false);
    const unwired = off.filter(v => v.why && !v.error);
    const broken = off.filter(v => !(v.why && !v.error));
    const st = broken.length ? 'degraded' : (unwired.length ? 'unwired' : 'ok');
    const why = (broken.length ? broken.map(v => v.error || '未说明') : unwired.map(v => v.why))
      .join('；').slice(0, 90);
    return assetBadge({ label: p.label }, st, off.length ? ((broken.length ? '不可用：' : '原因：') + why) : '');
  });
  /* v0.13.40：align-items:flex-start —— 三个徽标的 note 长短差很多（kb 那条 90+ 字），
     不拉伸才能让每块只占自己内容的高度，否则短的那块（技能）下面拖一大片空白。 */
  box.innerHTML = '<div style="display:flex;flex-wrap:wrap;gap:8px;align-items:flex-start">' + chips.join('') + '</div>';
}

async function runAssetSearch() {
  const q = (($('assetQ') || {}).value || '').trim();
  const hint = $('assetHint');
  if (hint) { hint.textContent = '检索中…'; }
  const settled = await Promise.allSettled(ASSET_SOURCES().map(s => {
    // 空查询时，需要 q 的那两路**根本不发请求**（而是置 idle）
    if (!q && s.needsQ) return Promise.resolve({ idle: true });
    return api(s.path(q));
  }));
  let degraded = 0;
  settled.forEach((r, i) => {
    const src = ASSET_SOURCES()[i];
    const box = $(src.box);
    if (!box) return;
    let state, d = null, err = null;
    if (r.status === 'fulfilled' && r.value && r.value.idle) {
      state = 'idle'; box.innerHTML = assetIdleHtml(src);
    } else if (r.status === 'rejected') {
      err = r.reason; state = assetStateOf(null, err);
      box.innerHTML = assetResultsHtml(src, null, state, err);
    } else {
      d = r.value; state = assetStateOf(d, null);
      box.innerHTML = assetResultsHtml(src, d, state, null);
    }
    if (state === 'degraded' || state === 'down') degraded++;
  });
  if (hint) {
    if (!q) {
      hint.textContent = '未带检索词：记忆/技术文档两路置为待输入（它们缺 q 会回 422），技能路已列前 6 条';
    } else {
      hint.textContent = degraded
        ? ('完成：' + degraded + '/3 路有降级，见对应列的红/黄字')
        : '完成：三路均正常';
    }
  }
}

function initAssetPanel() {
  if (assetsBound) return;
  assetsBound = true;
  const root = $('page-assets');
  if (!root) return;
  // data-asset 委托：唯一出口，侧栏/菜单内零 inline onclick（浮层三条之配套红线）
  root.addEventListener('click', e => {
    const b = e.target.closest('[data-asset]');
    if (!b) return;
    if (b.dataset.asset === 'search') runAssetSearch();
    else if (b.dataset.asset === 'refresh') loadAssets();
  });
  const inp = $('assetQ');
  if (inp) inp.addEventListener('keydown', e => { if (e.key === 'Enter') runAssetSearch(); });
}

async function loadAssets() {
  initAssetPanel();
  const sb = $('assetStatus');
  if (sb) sb.innerHTML = '<span class="hint">门面健康自检中…</span>';
  await loadAssetStatus();
  runAssetSearch();
}
/* ── 本机项目页（v0.13.30）────────────────────────────────────────
 * 分片头注释（军规：分片源，不是 build 产物；产物在 static/hub.js 由
 * scripts/build_hubjs.sh 按字典序拼接，直接改产物会被 test_hubjs_split 判红）。
 *
 * 数据源：GET /api/localprojects（后端多根 git 扫描 + cloudcli auth.db 合并，
 * 只读不鉴权）。列表三态（busy/数据/失败含重试）；errors 里点名的降级源
 * 用 hint 展示——「查不了」不能糊成「没有」。
 * 新建会话：复用 startAgent 的链路（POST /api/term/sessions），仅多传 cwd；
 * 后端 _cwd_or_none 校验后只进 os.chdir，命令仍出自画像白名单。
 * v0.13.32 行内动作：
 *   收藏（★）——lpStars 集合（localStorage 持久，键 = 项目 path）；收藏行
 *   置顶（多条按原序在前），行首星标高亮，再点取消；
 *   隐藏（👁off 图标）——lpHiddenSet（localStorage 持久）；隐藏行不再出现，
 *   勾选顶部 #lpHidden「显示隐藏」才回列（回列时半透明以示状态）。
 * 状态变量用 var：go()（01 分片，拼接序在前）会经顶层 go(lsGet('hub.page'))
 * 同步走到本分片函数体，let 的 TDZ 静态序风险会被 test_tdz_order 判红——
 * 原 08-runlog 分片同教训（RL_FIRST/RL_CUR，v0.13.47 随运行日志页一并删除）。localStorage 一律走 lsGet/lsSet 守卫
 * （test_ls_guard R1：裸调用判红）。 */

var LP = [];         // 全量项目（过滤前的缓存）
var lpSel = '';      // 当前选中项目 path（'' = 未选）
var lpSelName = '';
/* lpLoaded 的声明在 01-core-boot.js（**只此一处**）：go() 在 06 顶层同步调用，
   若在此处再写一份 `var lpLoaded = false`，其初始化晚于 go() ⇒ 把懒加载标志
   重置回 false（2026-10-11 报障的掩盖因素）。见 01 的说明与静态闸门。 */

/* v0.13.32 收藏/隐藏状态：localStorage 里的 JSON 数组（path 列表）。
   解析失败（旧值形状漂移）按空集处理，绝不让坏存档打断渲染。 */
function _lpSet(key) {
  try { return new Set(JSON.parse(lsGet(key) || '[]')); } catch (e) { return new Set(); }
}
var lpStars = _lpSet('hub.lp.stars');
var lpHiddenSet = _lpSet('hub.lp.hidden');

/* v0.13.101 删除墓碑：并集同步的**必要配套**，不是可选优化。
   并集（服务端 ∪ 本地）解决了「旧子集抹小服务端」，但它自带一个副作用：
   用户在这台设备点「取消收藏」后，本地集合少了它、服务端还留着 ⇒
   下次同步的并集把它**复活**。不配墓碑就是拿一个 bug 换另一个 bug。
   墓碑记「这台设备明确删过哪些 key」，并集时据此把服务端那份也剔除，
   并让本次 PUT 把删除真正落到服务端。墓碑本身**只在本机localStorage**，
   不上传——它表达的是单机意图，别的设备不需要也不该继承它。
   只存 key（path 字符串），不存任何内容，与 lpStars 同口径。 */
var lpTomb = _lpSet('hub.lp.tomb');

function _lpTombAdd(key) {
  lpTomb.add(key);
  lsSet('hub.lp.tomb', JSON.stringify([...lpTomb]));
}
function _lpTombDrop(key) {
  lpTomb.delete(key);
  lsSet('hub.lp.tomb', JSON.stringify([...lpTomb]));
}
/* 服务端有、本地没有、且没有墓碑 ⇒ 真·新增，收进来并清掉可能残留的墓碑。 */
function _lpAdopt(serverSet, localSet) {
  serverSet.forEach(function (k) {
    if (localSet.has(k)) { lpTomb.delete(k); return; }
    if (lpTomb.has(k)) return;      /* 墓碑：这台设备删过，别复活 */
    localSet.add(k);
  });
  lsSet('hub.lp.tomb', JSON.stringify([...lpTomb]));
}

function _lpSave(key, set) {
  lsSet(key, JSON.stringify([...set]));
}

/* v0.13.36 收藏/隐藏落服务端（跨浏览器/端侧一致）。
   v0.13.101（用户 2026-10-09 报障「重启后又显示没有收藏」）改掉两处，
   根因与红向取证见 work/probe-lp-sync-once.py（A/B/C/D 四段，全 PASS）：

   ★ 缺陷①（用户所见的直接成因）：一次性闸门 + 静默 catch。
     原写法进门即 `lpPrefSynced = true`，GET 失败走**空catch** ⇒ 本页面生命周期内
     **永不再拉**。而 localStorage 按 origin 隔离（AGENTS.md 4.1⑤）⇒ 局域网 IP /
     Tailscale / 手机 WebView 各一套存档；只要那一套是空的（或被清过站点数据），
     一次网络抖动 / 一次重启期间的后端 502，就让它**永远显示零收藏**，
     刷新与页内来回导航都不自愈。
     修法：失败**不置位**（下次进页/重试自然重来），并把原因落到 hint 上——
     静默失败与「真的没有收藏」在屏幕上是同形的，用户分不出就不可能自查。

   ★ 缺陷②：以本地为准 ⇒ 过时子集覆盖服务端。
     原分支注释写「后台静默合并推送（并集）」，**但 lpPushPref 推的是本地全量**，
     是覆盖不是并集。审计时间线实测到多次条数下降（asset_audit projects.lp：
     10-09T06:42:14 stars=17 → 09:57:45 stars=15），即另一台设备的旧子集把
     服务端更大的集合**抹小了**。
     修法：同步时以**服务端为基准**做并集（本地独有补进去），并用
     **删除墓碑**（_lpAdopt + lpTomb）让「取消收藏」不会被并集复活；
     删除意图由 lpToggleStar 自己 PUT 上去。 */
var lpPrefSynced = false;
var lpPrefErrShown = false;
var lpPrefLastErr = '';

/* 同步失败时不置 lpPrefSynced，允许下次重试；错误落 var 供 hint 显示。 */
function lpPrefFail(why) {
  lpPrefLastErr = why || '未知原因';
  lpPrefErrShown = true;
}

function lpCountsText() {
  return (lpStars.size ? ' · 收藏 ' + lpStars.size : '') +
    (lpHiddenSet.size ? ' · 已隐藏 ' + lpHiddenSet.size : '') +
    (lpPrefErrShown ? ' · ⚠ 云端同步失败（' + escapeHtml(lpPrefLastErr) + '，仅本机存档）' : '');
}

async function lpSyncPrefs() {
  if (lpPrefSynced) return;
  try {
    const d = await api('/api/prefs/projects.lp');
    /* 成功后才置位——这是缺陷①的修法：一次失败不许让本页面终身不再拉。 */
    lpPrefSynced = true;
    lpPrefErrShown = false;
    lpPrefLastErr = '';
    if (d && d.value) {
      const serverStars = new Set(d.value.stars || []);
      const serverHidden = new Set(d.value.hidden || []);
      const before = serverStars.size + serverHidden.size;
      /* 缺陷②修法：以服务端为基准做**并集**（原来「本地非空就整份推上去」是覆盖），
         墓碑让本机显式删过的项不被服务端旧值复活。 */
      _lpAdopt(serverStars, lpStars);
      _lpAdopt(serverHidden, lpHiddenSet);
      _lpSave('hub.lp.stars', lpStars);
      _lpSave('hub.lp.hidden', lpHiddenSet);
      lpRenderList();
      /* 合并后与服务端有差异（含墓碑剔除）⇒ 补推，让服务端收敛到同一状态。 */
      if (before !== serverStars.size + serverHidden.size
          || lpStars.size !== serverStars.size || lpHiddenSet.size !== serverHidden.size) {
        lpPushPref();
      }
      const hint = $('lpHint');
      if (hint && LP.length) hint.textContent = LP.length + ' 个项目' + lpCountsText();
    }
  } catch (e) {
    lpPrefFail((e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败'));
    const hint = $('lpHint');
    if (hint && LP.length) hint.textContent = LP.length + ' 个项目' + lpCountsText();
  }
}

async function lpPushPref() {
  try {
    await api('/api/prefs/projects.lp', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stars: [...lpStars], hidden: [...lpHiddenSet] }) });
    lpPrefErrShown = false;
    lpPrefLastErr = '';
  } catch (e) {
    const why = (e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败');
    const first = !lpPrefErrShown;          /* toast 只响一次，hint 每次都更新 */
    lpPrefFail(why);
    if (first) toast('收藏/隐藏已存本机，云端同步失败（' + why + '）', 'err');
    const hint = $('lpHint');
    if (hint && LP.length) hint.textContent = LP.length + ' 个项目' + lpCountsText();
  }
}

function lpTime(la) {
  if (!la) return '';
  return String(la).slice(5, 16).replace('T', ' ');
}

/* 行内动作图标：act-btn（既有 CSS）。stopPropagation 防触发行选中。 */
function lpRowHtml(p, i) {
  const on = p.path === lpSel ? ' on' : '';
  const starred = lpStars.has(p.path);
  const hidden = lpHiddenSet.has(p.path);
  const src = (p.git ? '<span class="tag agent">git</span>' : '') +
              (p.cloudcli ? '<span class="tag">cc</span>' : '');
  const acts = '<span class="nav-acts" style="display:inline-flex;gap:2px;align-self:center">' +
    '<span class="act-btn" style="' + (starred ? 'color:var(--warn,#d90)' : '') + '"' +
    ' data-lp-star="' + i + '"' +
    ' title="' + (starred ? '取消收藏' : '收藏——置顶排序') + '">' + ico('star') + '</span>' +
    '<span class="act-btn" data-lp-hide="' + i + '"' +
    ' title="' + (hidden ? '取消隐藏' : '隐藏——不再显示（可勾选顶部「显示隐藏」找回）') + '">' +
    ico('eye') + '</span></span>';
  return '<div class="mem-item' + on + (hidden ? ' lp-hidden-row' : '') + '" data-i="' + i +
    '" style="gap:6px;cursor:pointer' + (hidden ? ';opacity:.45' : '') + '"' +
    ' data-lp-row="' + i + '"' +
    ' title="' + escapeHtml(p.path) + '">' +
    '<p style="min-width:0">' + (starred ? '<span style="color:var(--warn,#d90)">★ </span>' : '') +
    '<b>' + escapeHtml(p.name) + '</b>' + src +
    (p.sessions ? ' <span class="hint">' + p.sessions + ' 会话</span>' : '') +
    (p.last_activity ? ' <span class="hint">' + escapeHtml(lpTime(p.last_activity)) + '</span>' : '') +
    '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' +
    escapeHtml(String(p.path).slice(0, 72)) + '</span></p>' + acts + '</div>';
}

function lpToggleStar(i) {
  const p = LP[i];
  if (!p) return;
  if (lpStars.has(p.path)) {
    lpStars.delete(p.path);
    _lpTombAdd(p.path);       /* v0.13.101：取消收藏记墓碑，否则同步的并集会把它复活 */
  } else {
    lpStars.add(p.path);
    _lpTombDrop(p.path);      /* 重新收藏 ⇒ 撤墓碑，否则它永远进不来 */
  }
  _lpSave('hub.lp.stars', lpStars);
  lpRenderList();
  lpPushPref();
}

function lpToggleHide(i) {
  const p = LP[i];
  if (!p) return;
  if (lpHiddenSet.has(p.path)) {
    lpHiddenSet.delete(p.path);
    _lpTombAdd(p.path);
  } else {
    lpHiddenSet.add(p.path);
    _lpTombDrop(p.path);
    if (lpSel === p.path) { lpSel = ''; lpSelName = ''; }   // 隐藏选中项即解除选中
  }
  _lpSave('hub.lp.hidden', lpHiddenSet);
  lpRenderList();
  lpPushPref();
  const hint = $('lpHint');
  if (hint && lpHiddenSet.size) hint.textContent += '（已隐藏 ' + lpHiddenSet.size + ' 项）';
}

function lpSelect(i) {
  const p = LP[i];
  if (!p) return;
  lpSel = p.path;
  lpSelName = p.name;
  const nameEl = $('lpSelName');
  if (nameEl) { nameEl.textContent = p.name; nameEl.title = p.path; }
  const btn = $('lpStartBtn');
  if (btn) btn.disabled = false;
  lpRenderList();   // 重画选中态（.on）
}

function lpRenderList() {
  const box = $('lpList');
  if (!box) return;
  const q = ($('lpQ') ? $('lpQ').value.trim() : '').toLowerCase();
  const showHidden = !!($('lpHidden') && $('lpHidden').checked);
  /* 顺序：收藏置顶（保持原相对序）→ 未收藏；隐藏行仅在勾选「显示隐藏」时出现。 */
  const visible = LP.filter(p => !lpHiddenSet.has(p.path) || showHidden);
  const ordered = visible.filter(p => lpStars.has(p.path))
    .concat(visible.filter(p => !lpStars.has(p.path)));
  /* 2026-10-05：原 `LP.indexOf(p)` 在 map 里对每个项目做一次线性查找 ⇒ O(n²)。
   * 改一次性建 **path→下标** 的 Map。用 path 而不是对象引用作键：LP 来自后端
   * JSON 数组（`d.projects`），同一次渲染内对象引用稳定，但 path 才是项目的
   * 唯一标识（lpStars / lpHiddenSet 也都以 path 为键，口径一致）。
   * 若真出现重复 path，Map 保留**最后**一个下标，而 indexOf 给**第一个** ——
   * 这类重复本就不该存在（后端按目录枚举），且只影响列表里那一行的序号显示。 */
  const order = new Map(LP.map((p, i) => [p.path, i]));
  const idx = ordered.map(p => [p, order.get(p.path)])
    .filter(([p]) => !q || fuzzyMatch([[p.name, 3], [p.path, 1]], q) > 0);
  box.innerHTML = idx.map(([p, i]) => lpRowHtml(p, i)).join('') ||
    '<div class="hint" style="padding:10px">' + (q ? '无匹配项目' : '无项目') + '</div>';
}

function lpRenderAgents() {
  const sel = $('lpAgent');
  if (!sel) return;
  const cur = sel.value;
  const items = AGENTS.filter(a => a.kind === 'agent' && (a.entries || []).some(e => e.type === 'term'));
  /* v0.13.103：AGENTS 异步来（loadAgents），本函数在 loadLocalProjects 数据一到
     就同步跑 —— 清单未到时**早退保占位**，不把 innerHTML 清成 0 个 option
     （清空会让 select 塌成 46px 空白，且此后无人重画：2026-10-11 报障）。
     真实清单由 01 分片 loadAgents 的补画钩子补上。 */
  if (!items.length) return;
  sel.innerHTML = items.map(a =>
    '<option value="' + escapeHtml(a.id) + '">' + escapeHtml(a.name) + '</option>').join('');
  if (cur && items.some(a => a.id === cur)) sel.value = cur;   // 轮询重绘不覆盖用户选择
  else if (!cur && items.length) sel.value = items[0].id;
}

async function loadLocalProjects(force) {
  const box = $('lpList');
  const hint = $('lpHint');
  if (!box) return;
  if (force) LP = [];
  if (!LP.length) box.innerHTML = '<div class="hint" style="padding:10px">扫描本机项目中…</div>';
  if (hint) hint.textContent = '加载中…';
  try {
    const d = await api('/api/localprojects');
    LP = d.projects || [];
    lpRenderList();
    lpRenderAgents();
    if (hint) hint.textContent = (d.count || 0) + ' 个项目' + lpCountsText();
    lpSyncPrefs();
    const meta = $('lpMeta');
    if (meta) meta.textContent = (d.roots || []).join(' · ') +
      ((d.errors || []).length ? ' ｜ 降级源：' + d.errors.join('；') : '') +
      (d.took_ms != null ? ' ｜ ' + d.took_ms + 'ms' : '');
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
    box.innerHTML = '<div class="hint" style="padding:10px">加载失败：' + escapeHtml(e.message || '') +
      ' <button class="btn sm" onclick="loadLocalProjects(true)">重试</button></div>';
  }
}

/* 核心：选中项目 + agent → 拉起 pty 会话 → 跳该 agent 终端工作台。
   逐字仿 startAgent（05 分片），仅多传 cwd。 */
async function lpStart() {
  const agent = $('lpAgent') ? $('lpAgent').value : '';
  if (!lpSel || !agent) { toast('先选项目与 Agent', 'err'); return; }
  try {
    const d = await api('/api/term/sessions', { method: 'POST',
      headers: termHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ agent_id: agent, cwd: lpSel }) });
    toast('已在 ' + lpSelName + ' 拉起 ' + agent + ' 终端会话', 'ok');
    gotoChat(agent, 'term');
    setTimeout(() => termConnect(d.session.id, agent, { user: true }), 100);
  } catch (e) { toast('新建会话失败：' + e.message, 'err'); }
}

/* ── P1-17（2026-09-30）：行内动作按钮改事件委托，撤掉 inline onclick ──────────
   为什么必须改：inline `onclick` 旁路事件委托（浮层唯一性红线第 2 条配套条款）。
   行内按钮一旦自己 onclick，整段「点完收场」逻辑就被跳过 —— 抽屉/浮层开着的
   情况下点收藏或隐藏，浮层留在原地，第二个浮层会把正文和第一个一起压住。
   委托是**唯一出口**：所有分支都从这里过，stopPropagation 也在这一处统一做，
   不依赖每个渲染点记得写。 */
(function () {
  var box = document.getElementById('lpList');
  if (!box) return;
  box.addEventListener('click', function (ev) {
    var t = ev.target;
    while (t && t !== box) {
      var ds = t.dataset || {};
      if (ds.lpStar != null) { ev.stopPropagation(); lpToggleStar(Number(ds.lpStar)); return; }
      if (ds.lpHide != null) { ev.stopPropagation(); lpToggleHide(Number(ds.lpHide)); return; }
      if (ds.lpRow != null)  { lpSelect(Number(ds.lpRow)); return; }
      t = t.parentNode;
    }
  });
})();

/* ── GitHub 项目页（v0.13.31）────────────────────────────────────────
 * 分片头注释（军规：分片源，不是 build 产物；产物在 static/hub.js 由
 * scripts/build_hubjs.sh 按字典序拼接，直接改产物会被 test_hubjs_split 判红）。
 *
 * 数据源：GET /api/github/repos（远端清单 + strict remote 本地匹配，
 * 只读不鉴权；token/token 失效由后端信封 errors 点名——「查不了」≠「没有」）。
 * v0.13.33 缓存裁定（用户：「拉取一次后就在本地缓存，每次拉取就是浪费资源；
 * 只有选中后进入编辑状态之前再次拉取同步；总目录是手动刷新」）：
 *   · 列表：服务端内存缓存**拉一次永久有效**（重启自然清空）；进页/过滤/翻看
 *     永不重拉；「刷新」按钮（force=1）是唯一整表重拉入口；
 *   · 选中（ghSelect）→ ghSyncCheck 打一次 /api/github/sync 单仓核对
 *     （1 次 git ls-remote 比对 HEAD），结果只作 #ghMeta 提示，不打断。
 * fork 默认隐藏（34 个 fork 多为别人的仓库），#ghForks 勾选即显。
 * 新建会话（ghStart）两段式：
 *   ① local.found → 与本机项目页同路：POST /api/term/sessions {agent_id, cwd}；
 *   ② 本地无 → POST /api/github/clone {repo}（按钮禁用 + 克隆中 toast）
 *      → 拿 d.path 作 cwd → 走同一条会话链 → gotoChat('term') + termConnect。
 * v0.13.32 行内动作（同 09 分片）：
 *   收藏（★）——ghStars（localStorage，键 = full_name）置顶 + 行首高亮；
 *   隐藏——ghHiddenSet（localStorage）；勾选 #ghHidden「显示隐藏」才回列（半透明）。
 * 状态变量用 var：go()（01 分片，拼接序在前）会经顶层 go(lsGet('hub.page'))
 * 同步走到本分片函数体，let 的 TDZ 静态序风险会被 test_tdz_order 判红——
 * 08/09 分片同教训。localStorage 一律走 lsGet/lsSet 守卫（test_ls_guard R1）。 */

var GH = [];          // 全量远端仓库（过滤前缓存）
var ghSel = null;     // 当前选中索引（null = 未选）
var ghSelName = '';
/* ghLoaded 的声明在 01-core-boot.js（**只此一处**）：理由同 09 分片的 lpLoaded ——
   在此处再写一份 `var ghLoaded = false` 会把 06 顶层 go() 置上的懒加载标志重置回 false。 */

/* v0.13.32 收藏/隐藏状态（同 09 的 _lpSet 形态；解析失败按空集，不打断渲染）。 */
function _ghSet(key) {
  try { return new Set(JSON.parse(lsGet(key) || '[]')); } catch (e) { return new Set(); }
}
var ghStars = _ghSet('hub.gh.stars');
var ghHiddenSet = _ghSet('hub.gh.hidden');

/* v0.13.101 删除墓碑（与 09 的 lpTomb 同型同理由）：并集同步的配套，
   防止「取消收藏」在下次同步时被服务端旧值复活。同 09：墓碑只在本机、不上传。 */
var ghTomb = _ghSet('hub.gh.tomb');

function _ghTombAdd(key) {
  ghTomb.add(key);
  lsSet('hub.gh.tomb', JSON.stringify([...ghTomb]));
}
function _ghTombDrop(key) {
  ghTomb.delete(key);
  lsSet('hub.gh.tomb', JSON.stringify([...ghTomb]));
}
function _ghAdopt(serverSet, localSet) {
  serverSet.forEach(function (k) {
    if (localSet.has(k)) { ghTomb.delete(k); return; }
    if (ghTomb.has(k)) return;
    localSet.add(k);
  });
  lsSet('hub.gh.tomb', JSON.stringify([...ghTomb]));
}

function _ghSave(key, set) {
  lsSet(key, JSON.stringify([...set]));
}

/* v0.13.36 收藏/隐藏落服务端（同 09 分片 lpSyncPrefs/lpPushPref 的 gh 对称版）。
   v0.13.101 同步修掉与09 完全同型的两处缺陷（用户 2026-10-09 报障
   「重启后又显示没有收藏」，红向取证 work/probe-lp-sync-once.py）：
     ① 一次性闸门 + 静默 catch ⇒ 一次失败后本页面终身不再拉（成功后才置位）；
     ② 「本地非空就整份推上去」是以过时子集**覆盖**服务端（同型代码必须同型修，
        两页各自一套同步逻辑正是同型缺陷容易漏改的地方）。 */
var ghPrefSynced = false;
var ghPrefErrShown = false;
var ghPrefLastErr = '';

function ghPrefFail(why) {
  ghPrefLastErr = why || '未知原因';
  ghPrefErrShown = true;
}

function ghCountsText() {
  return (ghStars.size ? ' · 收藏 ' + ghStars.size : '') +
    (ghHiddenSet.size ? ' · 已隐藏 ' + ghHiddenSet.size : '') +
    (ghPrefErrShown ? ' · ⚠ 云端同步失败（' + escapeHtml(ghPrefLastErr) + '，仅本机存档）' : '');
}

async function ghSyncPrefs() {
  if (ghPrefSynced) return;
  try {
    const d = await api('/api/prefs/projects.gh');
    ghPrefSynced = true;              /* 成功后才置位（缺陷①修法） */
    ghPrefErrShown = false;
    ghPrefLastErr = '';
    if (d && d.value) {
      const serverStars = new Set(d.value.stars || []);
      const serverHidden = new Set(d.value.hidden || []);
      const before = serverStars.size + serverHidden.size;
      /* 以服务端为基准做并集（缺陷②修法），墓碑剔除本机显式删过的项。 */
      _ghAdopt(serverStars, ghStars);
      _ghAdopt(serverHidden, ghHiddenSet);
      _ghSave('hub.gh.stars', ghStars);
      _ghSave('hub.gh.hidden', ghHiddenSet);
      ghRenderList();
      if (before !== serverStars.size + serverHidden.size
          || ghStars.size !== serverStars.size || ghHiddenSet.size !== serverHidden.size) {
        ghPushPref();
      }
      const hint = $('ghHint');
      if (hint && GH.length) hint.textContent = GH.length + ' 个仓库' + ghCountsText();
    }
  } catch (e) {
    ghPrefFail((e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败'));
    const hint = $('ghHint');
    if (hint && GH.length) hint.textContent = GH.length + ' 个仓库' + ghCountsText();
  }
}

async function ghPushPref() {
  try {
    await api('/api/prefs/projects.gh', { method: 'PUT',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ stars: [...ghStars], hidden: [...ghHiddenSet] }) });
    ghPrefErrShown = false;
    ghPrefLastErr = '';
  } catch (e) {
    const why = (e && e.http) ? ('HTTP ' + e.http) : ((e && e.message) || '网络失败');
    const first = !ghPrefErrShown;
    ghPrefFail(why);
    if (first) toast('收藏/隐藏已存本机，云端同步失败（' + why + '）', 'err');
    const hint = $('ghHint');
    if (hint && GH.length) hint.textContent = GH.length + ' 个仓库' + ghCountsText();
  }
}

function ghTime(iso) {
  if (!iso) return '';
  return String(iso).slice(0, 10);
}

function ghRowHtml(r, i) {
  const on = i === ghSel ? ' on' : '';
  const starred = ghStars.has(r.full_name);
  const hidden = ghHiddenSet.has(r.full_name);
  const tags = (r.fork ? '<span class="tag">fork</span>' : '') +
               (r.language ? '<span class="tag agent">' + escapeHtml(r.language) + '</span>' : '') +
               (r.archived ? '<span class="tag">archived</span>' : '');
  const local = (r.local && r.local.found)
    ? '<span class="tag" style="align-self:center">本地已有</span>'
    : '<span class="hint" style="align-self:center">本地无</span>';
  const path = (r.local && r.local.path)
    ? '<br><span class="hint" style="font-family:var(--font-mono);font-size:var(--fs-xs)">' +
      escapeHtml(String(r.local.path).slice(0, 72)) + '</span>' : '';
  const acts = '<span class="nav-acts" style="display:inline-flex;gap:2px;align-self:center">' +
    '<span class="act-btn" style="' + (starred ? 'color:var(--warn,#d90)' : '') + '"' +
    ' data-gh-star="' + i + '"' +
    ' title="' + (starred ? '取消收藏' : '收藏——置顶排序') + '">' + ico('star') + '</span>' +
    '<span class="act-btn" data-gh-hide="' + i + '"' +
    ' title="' + (hidden ? '取消隐藏' : '隐藏——不再显示（可勾选顶部「显示隐藏」找回）') + '">' +
    ico('eye') + '</span></span>';
  return '<div class="mem-item' + on + '" data-i="' + i +
    '" style="gap:6px;cursor:pointer' + (hidden ? ';opacity:.45' : '') + '"' +
    ' data-gh-row="' + i + '"' +
    ' title="' + escapeHtml(r.full_name || '') + '">' +
    '<p style="min-width:0">' + (starred ? '<span style="color:var(--warn,#d90)">★ </span>' : '') +
    '<b>' + escapeHtml(r.name || '') + '</b>' + tags +
    (r.pushed_at ? ' <span class="hint">' + ghTime(r.pushed_at) + '</span>' : '') + path + '</p>' +
    local + acts + '</div>';
}

function ghToggleStar(i) {
  const r = GH[i];
  if (!r) return;
  const fn = r.full_name;
  if (ghStars.has(fn)) { ghStars.delete(fn); _ghTombAdd(fn); }
  else { ghStars.add(fn); _ghTombDrop(fn); }
  _ghSave('hub.gh.stars', ghStars);
  ghRenderList();
  ghPushPref();
}

function ghToggleHide(i) {
  const r = GH[i];
  if (!r) return;
  const fn = r.full_name;
  if (ghHiddenSet.has(fn)) { ghHiddenSet.delete(fn); _ghTombAdd(fn); }
  else {
    ghHiddenSet.add(fn);
    _ghTombDrop(fn);
    if (ghSel === i) { ghSel = null; ghSelName = ''; }   // 隐藏选中项即解除选中
  }
  _ghSave('hub.gh.hidden', ghHiddenSet);
  ghRenderList();
  ghPushPref();
  const hint = $('ghHint');
  if (hint && ghHiddenSet.size) hint.textContent += '（已隐藏 ' + ghHiddenSet.size + ' 项）';
}

function ghSelect(i) {
  const r = GH[i];
  if (!r) return;
  ghSel = i;
  ghSelName = r.full_name || r.name || '';
  const nameEl = $('ghSelName');
  if (nameEl) { nameEl.textContent = ghSelName; nameEl.title = ghSelName; }
  const btn = $('ghStartBtn');
  if (btn) {
    btn.disabled = false;
    btn.lastChild.textContent = (r.local && r.local.found) ? '新建会话' : '克隆并开会话';
  }
  ghRenderList();   // 重画选中态（.on）
  ghSyncCheck(r);   // v0.13.33：选中即核对（用户裁定「进入编辑状态之前才同步」）
}

/* v0.13.33 单仓核对：选中仓库时打一次 /api/github/sync（1 次 git ls-remote，
   绝不整表重拉）。结果只影响 #ghMeta 的一行提示（behind 时提示 git pull），
   不打断、不弹窗——列表本身永远吃缓存。 */
var ghSyncBusy = false;
async function ghSyncCheck(r) {
  if (ghSyncBusy || !r || !r.full_name) return;
  ghSyncBusy = true;
  const meta = $('ghMeta');
  const orig = meta ? meta.textContent : '';
  try {
    const d = await api('/api/github/sync?repo=' + encodeURIComponent(r.full_name));
    if (meta && d && d.note) {
      const mark = d.state === 'synced' ? '✓ ' : (d.state === 'behind' ? '⚠ ' : '');
      meta.textContent = mark + d.note + (orig ? ' ｜ ' + orig : '');
    }
  } catch (e) { /* 核对失败静默：提示不是关键路径 */ }
  ghSyncBusy = false;
}

function ghFilter() {
  /* 纯函数（可被探针抽出真代码跑）：fork 默认隐藏，勾选即显；
     隐藏行仅在 #ghHidden 勾选时回列。 */
  const showForks = !!($('ghForks') && $('ghForks').checked);
  const showHidden = !!($('ghHidden') && $('ghHidden').checked);
  return GH.map((r, i) => [r, i]).filter(([r]) =>
    (!r.fork || showForks) && (!ghHiddenSet.has(r.full_name) || showHidden));
}

function ghRenderList() {
  const box = $('ghList');
  if (!box) return;
  const q = ($('ghQ') ? $('ghQ').value.trim() : '').toLowerCase();
  /* 顺序：收藏置顶（保持原相对序）→ 未收藏。 */
  const pairs = ghFilter();
  const ordered = pairs.filter(([r]) => ghStars.has(r.full_name))
    .concat(pairs.filter(([r]) => !ghStars.has(r.full_name)));
  const idx = ordered.filter(([r]) =>
    !q || fuzzyMatch([[r.name, 3], [r.full_name, 1]], q) > 0);
  box.innerHTML = idx.map(([r, i]) => ghRowHtml(r, i)).join('') ||
    '<div class="hint" style="padding:10px">' + (q ? '无匹配仓库' : '无仓库') + '</div>';
  const hint = $('ghHint');
  if (hint) hint.textContent = '显示 ' + idx.length + ' / ' + GH.length + ' 个仓库' + ghCountsText();
}

function ghRenderAgents() {
  const sel = $('ghAgent');
  if (!sel) return;
  const cur = sel.value;
  const items = AGENTS.filter(a => a.kind === 'agent' && (a.entries || []).some(e => e.type === 'term'));
  /* v0.13.103：同 09 分片lpRenderAgents——AGENTS 未到时早退保占位，不清空 option。 */
  if (!items.length) return;
  sel.innerHTML = items.map(a =>
    '<option value="' + escapeHtml(a.id) + '">' + escapeHtml(a.name) + '</option>').join('');
  if (cur && items.some(a => a.id === cur)) sel.value = cur;   // 轮询重绘不覆盖用户选择
  else if (!cur && items.length) sel.value = items[0].id;
}

async function loadGithubRepos(force) {
  const box = $('ghList');
  const hint = $('ghHint');
  if (!box) return;
  if (force) GH = [];
  if (!GH.length) box.innerHTML = '<div class="hint" style="padding:10px">拉取 GitHub 仓库清单…</div>';
  if (hint) hint.textContent = '加载中…';
  try {
    const d = await api('/api/github/repos' + (force ? '?force=1' : ''));
    GH = d.repos || [];
    ghSel = null;                     // 重拉后旧选中索引失效
    ghRenderList();
    ghRenderAgents();
    if (hint) hint.textContent = (d.token === false ? 'token 不可用 · ' : '') +
      (d.count || 0) + ' 个仓库 · 本地已有 ' + (d.local_total || 0) + ' 个' + ghCountsText();
    ghSyncPrefs();
    const meta = $('ghMeta');
    /* v0.13.33 用户裁定「拉取一次后本地缓存，每次拉取就是浪费资源」：
       服务端内存缓存永久有效（重启才清），只有「刷新」按钮（force=1）强拉。
       此处如实显示缓存年龄，不催促重新拉取。 */
    if (meta) meta.textContent =
      (d.cached ? '缓存（' + (ageText(d.age_s)) + '前拉取，点「刷新」强制更新）' : '首次拉取') +
      ((d.errors || []).length ? ' ｜ ' + d.errors.join('；') : '') +
      (d.took_ms != null ? ' ｜ ' + d.took_ms + 'ms' : '');
  } catch (e) {
    if (hint) hint.textContent = '加载失败';
    box.innerHTML = '<div class="hint" style="padding:10px">加载失败：' + escapeHtml(e.message || '') +
      ' <button class="btn sm" onclick="loadGithubRepos(true)">重试</button></div>';
  }
}

function ageText(sec) {
  const s = Number(sec) || 0;
  if (s < 90) return s + 's ';
  if (s < 5400) return Math.round(s / 60) + ' 分钟 ';
  return Math.round(s / 3600) + ' 小时 ';
}

/* 核心：选中仓库 + agent → 拉起 pty 会话 → 跳该 agent 终端工作台。
   本地已有：lpStart 同路；本地无：先 POST /api/github/clone（即时同步），
   拿返回 path 作 cwd 再开会话。 */
async function ghStart() {
  const agent = $('ghAgent') ? $('ghAgent').value : '';
  const r = GH[ghSel];
  if (ghSel == null || !r || !agent) { toast('先选仓库与 Agent', 'err'); return; }
  const btn = $('ghStartBtn');
  let cwd = (r.local && r.local.found) ? r.local.path : null;
  try {
    if (!cwd) {
      if (btn) btn.disabled = true;
      toast('克隆中：' + (r.full_name || r.name) + '（首次可能较慢）…');
      const d = await api('/api/github/clone', {
        method: 'POST',
        headers: termHeaders({ 'Content-Type': 'application/json' }),
        body: JSON.stringify({ repo: r.full_name })
      });
      cwd = d.path;
      if (d.existed) toast('目录已存在（同仓），直接开会话', 'ok');
    }
    const s = await api('/api/term/sessions', { method: 'POST',
      headers: termHeaders({ 'Content-Type': 'application/json' }),
      body: JSON.stringify({ agent_id: agent, cwd: cwd }) });
    toast('已在 ' + (r.name || cwd) + ' 拉起 ' + agent + ' 终端会话', 'ok');
    gotoChat(agent, 'term');
    setTimeout(() => termConnect(s.session.id, agent, { user: true }), 100);
  } catch (e) {
    toast('新建会话失败：' + (e.message || ''), 'err');
  } finally {
    if (btn && ghSel != null && GH[ghSel]) btn.disabled = false;
  }
}

/* ── P1-17（2026-09-30）：行内动作按钮改事件委托，撤掉 inline onclick ──────────
   为什么必须改：inline `onclick` 旁路事件委托（浮层唯一性红线第 2 条配套条款）。
   行内按钮一旦自己 onclick，整段「点完收场」逻辑就被跳过 —— 抽屉/浮层开着的
   情况下点收藏或隐藏，浮层留在原地，第二个浮层会把正文和第一个一起压住。
   委托是**唯一出口**：所有分支都从这里过，stopPropagation 也在这一处统一做，
   不依赖每个渲染点记得写。 */
(function () {
  var box = document.getElementById('ghList');
  if (!box) return;
  box.addEventListener('click', function (ev) {
    var t = ev.target;
    while (t && t !== box) {
      var ds = t.dataset || {};
      if (ds.ghStar != null) { ev.stopPropagation(); ghToggleStar(Number(ds.ghStar)); return; }
      if (ds.ghHide != null) { ev.stopPropagation(); ghToggleHide(Number(ds.ghHide)); return; }
      if (ds.ghRow != null)  { ghSelect(Number(ds.ghRow)); return; }
      t = t.parentNode;
    }
  });
})();

/** 资源监控页（v0.13.52）——列出运行中 Agent 的进程资源，支持 Kill。
 *
 *  v0.13.54（2026-09-29 报障「资源页面还是无法加载」的真身）：删掉原第 5 行
 *  `if (resLoaded && !force) return;`。go()（01 分片）写的是
 *  `if (page === 'resources' && !resLoaded) { resLoaded = true; loadResources(); }`
 *  —— **先置位、后调用**，所以首次进页这道内部闸门必然命中，函数直接空返回：
 *  既不发请求也不写 hint，页面就永远停在「加载中…」，而且**控制台零报错**
 *  （没抛异常，什么都没发生）。probe 实测量到的正是：page_on=true、
 *  resLoaded=true、cards=0、hint=""。
 *  同型的 lpLoaded / ghLoaded 两个页面没炸，是因为 loadLocalProjects /
 *  loadGithubRepos 内部**没有**这道闸门 —— 「进页只由 go() 一处把关」
 *  是本仓既定纪律，资源页是唯一一个在加载函数里又关了一道的。 */
var resLoaded = false;

async function loadResources(force = false) {
    const listEl = document.getElementById("resList");
    const hintEl = document.getElementById("resHint");
    const summaryEl = document.getElementById("resSummary");
    if (!listEl) return;

    hintEl && (hintEl.textContent = "采样中…");
    listEl.innerHTML = '<div class="hint" style="padding:10px">采样中…</div>';

    try {
        const resp = await api("/api/resources");
        if (!resp.ok) throw new Error(resp.error || "请求失败");
        renderResources(resp);
        hintEl && (hintEl.textContent = "共 " + resp.total_agents + " 个 Agent · 总 CPU " + resp.total_cpu + "% · 总内存 " + resp.total_rss_mb + " MB");
        if (summaryEl) summaryEl.textContent = "总计：" + resp.total_agents + " 个 Agent · CPU " + resp.total_cpu + "% · 内存 " + resp.total_rss_mb + " MB";
        resLoaded = true;
    } catch (e) {
        console.error("[Resources] load failed:", e);
        hintEl && (hintEl.textContent = "加载失败");
        /* P1-24：此前直吐 e.message 进 innerHTML —— 后端 message 里带 &<>"' 就破版，
           且与全站其它页的 boxFail 口径不一致（少了 escapeHtml 与「重试」按钮）。
           统一走 boxFail：同一个渲染 + 同一个转义 + 同一条重试路径。 */
        /* 重试入口用显式函数名：onclick="loadResources()" 会把事件对象当 force 传进去
           （隐式 truthy），重试语义恰好也该强制刷新，但不该靠这个巧合成立。 */
        boxFail("resList", e, "resourcesRetry");
    }
}

/** 资源页「重试」按钮的显式入口（不依赖 onclick 传参的隐式 truthy）。 */
function resourcesRetry() { loadResources(true); }

function renderResources(data) {
    const listEl = document.getElementById("resList");
    if (!listEl) return;

    if (!data.agents || data.agents.length === 0) {
        listEl.innerHTML = '<div class="hint" style="padding:20px;text-align:center">当前没有运行中的 Agent 进程</div>';
        return;
    }

    let html = "";
    for (const agent of data.agents) {
        const agentId = agent.agent_id;
        const agentName = agent.agent_name;
        const kind = agent.kind;
        const summary = agent.summary;
        const processes = agent.processes;

        // 根据 kind 给不同颜色标记
        const kindBadge = {
            agent: '<span class="s-badge running"></span>',
            gateway: '<span class="s-badge" style="background:var(--accent);color:var(--on-accent)">网关</span>',
            // 三个色值全走 :root 实名 token。此前 service 用 var(--primary)、
            // tool/memory 写字面色，而这三个变量在 :root 里**定义数为 0**
            // ⇒ var() 解析失败回退到初始值（透明），服务徽章看起来「没上色」。
            // 语义映射：service/工具/记忆都是「非交互的分类标识」，
            // 用灰阶 --text-2 / --muted 表达层级差，不与 --accent（可点击主色）抢语义。
            service: '<span class="s-badge" style="background:var(--text-2);color:var(--on-accent)">服务</span>',
            tool: '<span class="s-badge" style="background:var(--muted);color:var(--on-accent)">工具</span>',
            memory: '<span class="s-badge" style="background:var(--accent);color:var(--on-accent)">记忆</span>'
        }[kind] || '<span class="s-badge"></span>';

        /* P2-D：agent_id / pid 一律 escapeHtml + data-*，不再拼进 inline onclick。
           两层理由（任一层单独成立就该改）：
           ① 纪律层——inline onclick 旁路事件委托（本批 P1-17 已把 09/10 两个页面
              改成 data-* 走委托；inline 会跳过「点完收场」逻辑）。
           ② 纵深层——id 直接进属性字符串，一个含引号的 id 就能破出属性、加第二个
              onclick。实测不可利用（agent_id 来自画像白名单、后端输出经净化），
              但「不可利用」是后端当前的性质，不是前端的保证：纵深该在前端补，
              否则哪天画像来源放宽（自定义 Agent 名 / 扫到奇怪进程名）就是真漏洞。 */
        html +=
        '<div class="agent-card" data-agent="' + escapeHtml(agentId) + '" style="border:1px solid var(--divider);border-radius:8px;margin-bottom:8px;background:var(--bg);overflow:hidden">' +
            '<div class="agent-header" data-toggle="1" data-agent="' + escapeHtml(agentId) + '" style="display:flex;align-items:center;gap:8px;padding:10px 12px;background:var(--surface-2);cursor:pointer;border-bottom:1px solid var(--divider)">' +
                kindBadge +
                '<span class="agent-name" style="flex:1;font-weight:500">' + escapeHtml(agentName) + '</span>' +
                '<span class="agent-meta" style="font-size:12px;color:var(--text-2)">' +
                    'CPU <b>' + summary.cpu_percent + '%</b> · 内存 <b>' + summary.rss_mb + ' MB</b> · <b>' + summary.count + '</b> 进程' +
                '</span>' +
                '<svg class="i xs chevron" aria-hidden="true" style="transition:transform .15s;flex:none"><use href="#i-chevron-down"/></svg>' +
            '</div>' +
            '<div class="agent-procs" id="procs-' + agentId + '" style="display:none;padding:8px 12px;max-height:300px;overflow-y:auto">' +
                '<table style="width:100%;border-collapse:collapse;font-size:12px">' +
                    '<thead>' +
                        '<tr style="position:sticky;top:0;background:var(--surface-2);z-index:1">' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">PID</th>' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">CPU%</th>' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">内存</th>' +
                            '<th style="text-align:left;padding:4px 8px;border-bottom:1px solid var(--divider)">命令行</th>' +
                            '<th style="text-align:center;padding:4px 8px;border-bottom:1px solid var(--divider);width:80px">操作</th>' +
                        '</tr>' +
                    '</thead>' +
                    '<tbody>';

        for (const proc of processes) {
            html +=
                        '<tr data-pid="' + escapeHtml(proc.pid) + '">' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider)">' + proc.pid + '</td>' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider)">' + proc.cpu_percent + '%</td>' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider)">' + proc.rss_mb + ' MB</td>' +
                            '<td class="res-cmd" style="padding:4px 8px;border-bottom:1px solid var(--divider);overflow:hidden;text-overflow:ellipsis;white-space:nowrap" title="' + escapeHtml(proc.cmdline) + '">' + escapeHtml(proc.cmdline) + '</td>' +
                            '<td style="padding:4px 8px;border-bottom:1px solid var(--divider);text-align:center">' +
                                '<button class="btn sm danger" data-kill="SIGTERM" data-agent="' + escapeHtml(agentId) + '" data-pid="' + escapeHtml(proc.pid) + '" title="优雅结束 (SIGTERM)">结束</button>' +
                                '<button class="btn sm danger" style="margin-left:4px" data-kill="SIGKILL" data-agent="' + escapeHtml(agentId) + '" data-pid="' + escapeHtml(proc.pid) + '" title="强制结束 (SIGKILL)">强杀</button>' +
                            '</td>' +
                        '</tr>';
        }

        html +=
                    '</tbody>' +
                '</table>' +
            '</div>' +
        '</div>';
    }
    listEl.innerHTML = html;
    bindResourceActions(listEl);
}

/** P2-D：卡片展开 / 结束 / 强杀三条动作的**唯一出口**（事件委托）。
 *
 *  为什么不是 inline onclick：
 *   - inline 属性里的 JS 字符串要求 id 必须是「安全的 JS 字面量」，任何引号都要
 *     转义层级，转义错了就是 XSS；data-* 只是属性值，escapeHtml 一层就够。
 *   - 委托是本批 P1-17 定的纪律（inline 旁路收场逻辑），三处动作保持同一出口。
 *  bind 幂等：容器上打标记，重渲染（采样轮询）不会重复绑。 */
function bindResourceActions(listEl) {
    if (!listEl || listEl.dataset.resBound === "1") return;
    listEl.dataset.resBound = "1";
    listEl.addEventListener("click", function (e) {
        const killBtn = e.target.closest("[data-kill]");
        if (killBtn) {
            /* stopPropagation 收拢到委托这一处统一做：kill 按钮在可展开的卡片头语义
               之外，必须不冒泡到头部的展开动作，否则点「结束」会顺手把卡片展开。 */
            e.stopPropagation();
            killProc(killBtn.dataset.agent, parseInt(killBtn.dataset.pid, 10),
                     killBtn.dataset.kill);
            return;
        }
        const head = e.target.closest(".agent-header[data-toggle]");
        if (head) toggleAgentProcs(head.dataset.agent);
    });
}

function toggleAgentProcs(agentId) {
    const procEl = document.getElementById("procs-" + agentId);
    const chevron = document.querySelector('[data-agent="' + agentId + '"] .chevron');
    if (!procEl) return;
    const isHidden = procEl.style.display === "none";
    procEl.style.display = isHidden ? "block" : "none";
    if (chevron) chevron.style.transform = isHidden ? "rotate(180deg)" : "";
}

async function killProc(agentId, pid, signal) {
    if (!confirm("确定要 " + (signal === "SIGTERM" ? "结束" : "强制结束") + " 进程 PID " + pid + " 吗？")) return;

    const rowEl = document.querySelector('#procs-' + agentId + ' tr[data-pid="' + pid + '"]');
    const killBtns = rowEl ? rowEl.querySelectorAll('button') : [];
    killBtns.forEach(b => { b.disabled = true; b.style.opacity = '0.5'; });

    try {
        const resp = await api("/api/resources/kill", {
            method: "POST",
            headers: { 'Content-Type': 'application/json' },
            body: JSON.stringify({ agent_id: agentId, pid: pid, signal_name: signal })
        });

        if (!resp.ok) {
            const failedPids = resp.failed || [pid];
            const killedPids = resp.killed || [];
            let msg = "";
            if (killedPids.length && failedPids.length) {
                msg = "部分成功：PID " + killedPids.join(',') + " 已结束；PID " + failedPids.join(',') + " 失败";
            } else if (failedPids.length) {
                msg = "失败：PID " + failedPids.join(',') + " 未能结束";
            } else {
                msg = resp.error || "Kill 失败";
            }
            throw new Error(msg);
        }

        toast("PID " + pid + " " + (signal === "SIGTERM" ? "已结束" : "已强杀"), "ok");

        // 实时移除该进程行
        if (rowEl) {
            rowEl.style.transition = "opacity 0.2s, height 0.2s";
            rowEl.style.opacity = "0";
            rowEl.style.height = "0";
            setTimeout(() => rowEl.remove(), 200);
        }

        // 更新 Agent 汇总信息
        updateAgentSummary(agentId, -1);

        // 若该 Agent 已无进程，移除整张卡片
        checkAndRemoveEmptyAgent(agentId);

    } catch (e) {
        console.error("[Resources] kill failed:", e);
        killBtns.forEach(b => { b.disabled = false; b.style.opacity = ''; });
        toast("操作失败：" + e.message, "err");
    }
}

function updateAgentSummary(agentId, deltaCount) {
    const cardEl = document.querySelector('[data-agent="' + agentId + '"]');
    if (!cardEl) return;
    const metaEl = cardEl.querySelector('.agent-meta');
    if (!metaEl) return;
    /* P1-24：此前读的是 textContent（纯文本，形如 "2 进程 · 120MB"），
       却拿它去跑 /<b>(\d+)<\/b>/ 这种**只可能匹配 innerHTML** 的正则 ——
       永远匹配不上，计数更新是死代码；即便某次碰巧匹配上，把 textContent
       的结果塞回 innerHTML 也会把实体（&<>）重新解释成标签。
       改成对纯文本做正则，输出也走 textContent：不碰解析器就没有二次解释。 */
    const text = metaEl.textContent || '';
    const m = text.match(/(\d+)\s*进程/);
    if (m) {
        const newCount = Math.max(0, parseInt(m[1], 10) + deltaCount);
        metaEl.textContent = text.replace(/\d+\s*进程/, newCount + ' 进程');
    }
}

function checkAndRemoveEmptyAgent(agentId) {
    const procsEl = document.getElementById('procs-' + agentId);
    if (!procsEl) return;
    const rows = procsEl.querySelectorAll('tbody tr');
    if (rows.length === 0) {
        const cardEl = document.querySelector('[data-agent="' + agentId + '"]');
        if (cardEl) {
            cardEl.style.transition = "opacity 0.2s, height 0.2s, margin 0.2s";
            cardEl.style.opacity = "0";
            cardEl.style.height = "0";
            cardEl.style.margin = "0";
            setTimeout(() => {
                cardEl.remove();
                checkEmptyList();
            }, 200);
        }
    }
}

function checkEmptyList() {
    const listEl = document.getElementById("resList");
    if (!listEl) return;
    const cards = listEl.querySelectorAll('.agent-card');
    if (cards.length === 0) {
        listEl.innerHTML = '<div class="hint" style="padding:20px;text-align:center">当前没有运行中的 Agent 进程</div>';
    }
}

/* P2-D：此处原有的 escapeHtml **副本**已删除，统一用 01-core-boot.js 的那份。
   两个理由：
     ① 同一语义两份实现＝迟早漂（副本里转义表用 \u0026 写码点、正本用字面量，
        读的人得逐个解码才知道它们等价）；本仓已因「异常判据四处各判各的」吃过
        一次同型亏（P1-20）。
     ② 副本有真 bug：`(s || "")` 对 0 / false 会返回空串——pid=0、计数 0
        都会被渲染成空白。正本用 `String(s == null ? '' : s)`，无此问题。 */

// 供外部调用（如从其它页面跳转）
window.loadResources = loadResources;
/* ── P1-16（2026-09-30）：资源页窄屏档 ─────────────────────────────────
   症状：命令行列写死 max-width:400px，加 PID/CPU/RSS/操作四列后在 390px 视口
   必然横向溢出（手机上表现为整页左右拖、右侧「结束/强杀」按钮点不到）。
   修法：宽度交给 CSS 表格布局按视口分配。min-width:0 是关键 —— 表格单元格默认
   min-width:auto，内容多宽就撑多宽，text-overflow 永远不生效。
   断点 767px 与 01-core-boot.js 的 HUB_NARROW_MQ / templates/index.html 的
   @media(max-width:767px) 同源同值（分档偏好不变量第 3 条：断点只允许一处定义）。
   注意：本文件是 JS 分片，裸 CSS 文本不会被解析，必须 insertRule 真注入。 */
(function () {
    var CSS = [
        ".res-cmd { max-width: 400px; }",
        "@media (max-width: 767px) {",
        "  .res-cmd { max-width: none; }",
        "  #resList .agent-card table { table-layout: fixed; width: 100%; }",
        "  #resList .agent-card td, #resList .agent-card th { padding: 4px 6px; }",
        "  #resList .agent-card td.res-cmd { word-break: break-all; white-space: normal; }",
        "}"
    ].join("");
    if (typeof CSSStyleSheet === "undefined" || !CSSStyleSheet.prototype.insertRule) return;
    var sheet = new CSSStyleSheet();
    sheet.replaceSync(CSS);
    document.adoptedStyleSheets = document.adoptedStyleSheets.concat([sheet]);
})();
/* ── P2-B（2026-09-30）：跨 Agent 活动指示 ──────────────────────────────────────
   兑现「多 Agent 统一管理中心」最核心的那条承诺：**现在要看谁在跑必须逐个点开**。
   本批走计划里的低成本路径 —— 复用既有 /api/term/sessions（不依赖服务端 pyte，
   融合计划的 P2-1 真值方案成本高风险大，本批不做，另登记）。

   两处上屏：
     ① 侧栏每个实体行尾一个忙碌点：有 alive 终端会话 ⇒ 亮；无 ⇒ 不占位
        （用 :empty 语义，零会话时侧栏与改动前逐像素一致）。
     ② 顶栏「在跑 N」：**在跑 = 有活着的终端会话的 Agent 数**，与「可用数」是两个
        概念，刻意分开 —— Agent 服务在跑（usable）不等于此刻有人在用它。

   口径纪律（与 P1-20 同一个教训）：判据只认 /api/term/sessions 的 alive，
   不用 last_seen / 进程表去猜「有没有人在用」—— 那些是「服务活着」不是「人在用」。
   拉取失败一律**保持上一次的值**并 console.warn，不清零：指示消失比指示滞后更糟。 */
var ACTIVITY = {};          // agent_id → { n, latest, active_s }
var activityLoaded = false;
var activityFailed = false;

/* 空闲多久算「在忙」：会话还活着但没动静超过 2 分钟 ⇒ 只亮常点不闪。
   取 2 分钟是照终端心跳（TERM_HB）量级定的：人 thinking 一轮通常 <2min。 */
const ACTIVITY_BUSY_S = 120;

function activityMap(perAgent) {
  /* 输入是 /api/term/activity 的 per_agent（[{agent_id, n}]）。
     口径不变：只认**活着的**会话数，一个 Agent 多条就累加。
     改前吃的是 /api/term/sessions 的整份清单（含 sid/activity_s），
     那样必须带口令；现在服务端已经在服务端把 alive 过滤掉了，
     所以这里不再重复 alive 判断 —— 但要**显式拒绝零值**，
     免得服务端将来多回一条 n=0 的占位就在这里亮出一个假忙碌点。 */
  const m = {};
  (perAgent || []).forEach(x => {
    if (!x || !x.agent_id || !(x.n > 0)) return;
    m[x.agent_id] = { n: x.n, active_s: x.activity_s || 0 };
  });
  return m;
}

async function loadActivity() {
  /* 2026-10-05：页面隐藏时早退（别在后台标签里白拉）。
   * ⚠ 守卫放在**函数体内**而不是 `setInterval` 注册处 ——
   * tests/test_activity_indicator.py:114 断言的字面量是
   * `setInterval(loadActivity,\s*\d+)`，动注册会撞红。
   * 语义无副作用：停表期间不更新 ACTIVITY，而「拉不到就保持旧值」本来就是
   * 本函数的既定失败语义（见下面 catch），所以早退不改变可见行为。 */
  if (document.hidden) return;
  try {
    /* 走**免 token** 的只读聚合端点 /api/term/activity（v0.13.90）。
       改前这里读 lsGet('hub.term.token')，没 token 就直接放弃 ——
       用户报的「会话数一时显示一时不显示」就是它：localStorage 按 origin 隔离，
       局域网那个源没存过口令（或口令过期被清）时整块指示直接消失，
       而屏幕上「读不到」与「真的零会话」长得一模一样。
       现在既不读口令也不弹口令框（那条铁律原样保留：被动展示绝不逼人交密码），
       端点本身也不需要凭据 —— 它只回聚合计数，不含任何 sid/cmd/cwd。 */
    const d = await api('/api/term/activity');
    ACTIVITY = activityMap(d && d.per_agent, d && d.alive);
    activityLoaded = true;
    activityFailed = false;
    renderActivity();
  } catch (e) {
    /* 拉不到就保持旧值：宁可指示滞后，也不要让「在跑 N」闪成 0 再闪回来。
       首次就失败则标记出来，让顶栏不假装自己知道。 */
    activityFailed = true;
    if (!activityLoaded) ACTIVITY = {};
    console.warn('[activity] 拉取终端会话失败，沿用上次值：', e && e.message);
    renderActivity();
  }
}

/** 侧栏行尾忙碌点（只亮不闪，hover 给具体会话数）。 */
function activityDotHtml(a) {
  const st = ACTIVITY[a.id];
  if (!st) return '';
  const busy = (st.active_s || 0) <= ACTIVITY_BUSY_S;
  return '<span class="nav-busy' + (busy ? ' busy' : '') + '" title="' +
    escapeHtml(st.n > 1 ? st.n + ' 个终端会话在跑' : '终端会话在跑') +
    '" aria-label="' + (st.n > 1 ? st.n + ' 个终端会话在跑' : '终端会话在跑') + '"></span>';
}

/** 顶栏「在跑 N」+ 侧栏点。只在有会话时出现，零会话时不占位。 */
function renderActivity() {
  const ids = Object.keys(ACTIVITY);
  const n = ids.length;
  const el = $('hBusy');
  if (el) {
    if (activityFailed && !activityLoaded) {
      el.innerHTML = '';
    } else if (n) {
      /* 口径（用户 2026-10-07 追认）：在跑 = 有活会话的 **Agent 数**；
         会话 N = 活会话 **总条数**（每行一个条数，不是每行一条）。
         两者永远是 会话数 ≥ Agent 数，且**同时**给出来 —— 改前只在
         sess > n 时才附「（N 会话）」，单会话的 agent 看不出总条数。 */
      const sess = ids.reduce((s, id) => s + (ACTIVITY[id].n || 0), 0);
      el.innerHTML = '<span class="hdot b" title="有活着的终端会话的 Agent"></span>在跑 ' + n +
        '<span class="hbusy-sep">·</span>会话 ' + sess;
    } else {
      el.innerHTML = '';
    }
  }
  /* 侧栏点：renderNav 有「内容没变就不重写 DOM」的省写逻辑，直接重渲即可，
     但为免打断滚动位置，这里只在状态确实变了时才让 nav 重绘。 */
  const sig = ids.map(id => id + ':' + ACTIVITY[id].n).sort().join(',');
  if (sig !== renderActivity._sig) {
    renderActivity._sig = sig;
    if (typeof renderNav === 'function') renderNav();
  }
}

/* 轮询节奏：活动变化比实体状态快得多（人开终端是秒级，实体启动是分钟级），
   故独立 8s 一次，比 loadAgents 的 30s 密。终端在跑时 xterm 已有自己的心跳，
   这里只读不写，不与 TERM_HB 抢。 */
loadActivity();
setInterval(loadActivity, 8000);
/* 回到可见时**立刻补一次**。否则笔记本合盖唤醒后，指示会停在休眠前的快照
   直到下一个 8s 周期 —— 用户第一眼看到的是过期数据。
   （这一条闸门没有约束，是我按「隐藏守卫必须有唤醒路径」补的；
    闸门真正约束的只有上面那条 setInterval 字面量。） */
document.addEventListener('visibilitychange', () => { if (!document.hidden) loadActivity(); });
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
function ttScrollByPx(deltaYPx) {
  if (!term || !deltaYPx) return;
  const cell = ttCellPx();
  if (!cell) return;
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