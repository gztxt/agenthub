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

let AGENTS = [], PORTS = [], portsLoaded = false, memLoaded = false, skillsLoaded = false, kbLoaded = false;
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
  if (page === 'memory' && !memLoaded) { memLoaded = true; memOverview(); loadMemories(); loadDoc('l2'); loadDoc('l3'); }
  if (page === 'skills' && !skillsLoaded) { skillsLoaded = true; loadSkills(); loadSkillBudget(); }
  if (page === 'kb' && !kbLoaded) { kbLoaded = true; loadKbStatus(); kbBrowse(); }
  if (page === 'localprojects' && !lpLoaded) { lpLoaded = true; loadLocalProjects(); }   // v0.13.30 本机项目页（09 分片）
  if (page === 'github' && !ghLoaded) { ghLoaded = true; loadGithubRepos(); }   // v0.13.31 GitHub 项目页（10 分片）
  // v0.13.43 设置三页：与系统页同口径，进页才拉（模型清单 / GitHub 现值）
  if (page === 'settings-model' && !setModelPageLoaded) { setModelPageLoaded = true; settingsModelsLoad(); }
  if (page === 'settings-github' && !setGithubPageLoaded) { setGithubPageLoaded = true; settingsGithubLoad(); }
  if (page === 'settings-logs' && !setLogsPageLoaded) { setLogsPageLoaded = true; settingsLogsLoad(); }
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
        staleEl.innerHTML = '<span class="stale-tag" title="' +
          escapeHtml('运行中的代码与磁盘不一致（启动于 ' + ((d && d.git_sha_boot) || '?') +
                      '，当前磁盘 ' + ((d && d.git_sha_now) || '?') +
                      '）。改动要重启 hub 才生效。') + '">需重启</span>';
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
      'onclick="verifyAgent(' + jsStr(a.id) + ')" ' +
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
  const t = $('settingsTokenVal').dataset.token || '';
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
/* 三个设置页的 id —— 委托与 go() 懒加载共用这一份清单，别再各处写字面量 */
const SET_PAGE_IDS = ['page-settings-model', 'page-settings-github', 'page-settings-token',
                      'page-settings-logs'];

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
