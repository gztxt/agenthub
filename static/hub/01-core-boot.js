/* Agent Hub v0.3.0 教室视图 — 复刻 Agent_Manager(Dashboard/Manager/Memory/Ports) 交互语义
   v0.10 chat: 动态模型选择器 + 工作目录 + 会话管理（服务 claude/jcode）*/
/* ── 断点唯一真源（不变量 3）─────────────────────────────────────
   全站只允许这一处 `matchMedia('(max-width: 767px)')`。放在 01 是因为 05 的顶层
   语句也要读它，而 `window.isNarrow` 要等 initSidebar 跑起来才被赋值。 */
const HUB_NARROW_MQ = window.matchMedia('(max-width: 767px)');
const hubNarrow = () => HUB_NARROW_MQ.matches;

/* 空闲多久算「在忙」（原在 12-activity.js）：会话活着但没动静超过 2 分钟 ⇒
   只亮常点不闪。取 2 分钟照终端心跳（TERM_HB）量级定，人 thinking 一轮通常 <2min。
   ⚠ 必须留在 01：侧栏行渲染（05 的 navItemHtml→activityDotHtml）会被顶层的
   initSidebar()/loadAgents()/go() 同步调到，声明晚于这些语句就是 TDZ（09-23 事故同型）。 */
const ACTIVITY_BUSY_S = 120;

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
/* 2026-10-25：setMemPageLoaded 随「设置 · 记忆存储」页一并删除（并入记忆中心折叠块）。 */
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

/* 2026-11-07 C 档：全站单一认证面。写端点由 write_gate 的登录闸门保护，
   身份只认登录会话 Cookie / HTTP Basic，不再索取任何共享 token。 */
const WRITE_METHODS = { POST: 1, PUT: 1, PATCH: 1, DELETE: 1 };
/* 2026-11-07 修复：29c0e08 重写 api() 时把这一行函数定义一起删掉了，
   但 api() 里三处仍在调用 ⇒ 每次请求都抛
   `ReferenceError: isWriteMethod is not defined`，登录后所有数据拉取全被
   这个异常吃掉，页面只剩骨架（用户报「登录后不正常 / 刷新就白屏」）。
   判据：01-core-boot.js 里 `isWriteMethod(` 的出现次数必须同时有定义。 */
function isWriteMethod(m) { return !!WRITE_METHODS[String(m || 'GET').toUpperCase()]; }
/* 2026-11-07 登录收集器（与 03 分片 openLoginModal 配套）：
   401 时弹账号密码框，成功 ⇒ true；取消/超时 ⇒ false。不再读写任何 token。 */
async function _promptLoginAsync() {
  if (typeof openLoginModal !== 'function') return false;   // 03 分片未加载兜底
  /* 与 maybeAutoLoginPrompt 共用同一个「已弹过」标记：用户在这里点了取消，
     600ms 后的首访自动弹窗就不该再糊他一次。 */
  try { sessionStorage.setItem('hub.login.prompted', '1'); } catch (e) {}
  const race = await Promise.race([
    openLoginModal({ auto: true }).catch(() => false),
    new Promise(res => setTimeout(() => res(false), 90000))
  ]);
  return !!race;
}

/* 2026-11-07 用户授权：启动时问后端 whoami，按 user_perms 隐藏无权限菜单项。
   兼容旧令牌 / 回环匿名 ⇒ user_perms=null（全模块，零行为变化）。 */
let AUTH_PERMS = null;   // null=全模块；set=可见模块；'loading'=还没问完
let AUTH_USER = null;    // 2026-11-07：whoami 返回的登录名（Cookie 会话）；null=未登录
async function loadAuthPerms() {
  try {
    const data = await api('/api/users/whoami');
    AUTH_PERMS = (data && data.user_perms) ? new Set(data.user_perms) : null;
    AUTH_USER = (data && data.username) || null;
    if (AUTH_USER) lsSet('hub.login.user', AUTH_USER);
  } catch (e) { AUTH_PERMS = null; }   // 拿不到 = 不隐藏（fail-open，同旧行为）
  applyAuthToNav();
  renderAuthBox();
  maybeAutoLoginPrompt();
}
function _permOk(mod) { return AUTH_PERMS === null || AUTH_PERMS.has(mod); }
/* 把权限套到顶栏 4 项 + 手风琴 4 组。无权限的节点不渲染（不是灰显——用户例子要的就是“不显示”）。 */
function applyAuthToNav() {
  /* 顶栏 4 项（HTML 直写，data-sys 即模块 id） */
  const top = { classroom: 'btnNavHome', localprojects: 'btnNavProjects', github: 'btnNavGithub', resources: 'btnNavResources' };
  Object.entries(top).forEach(([mod, id]) => {
    const el = $(id); if (el) el.style.display = _permOk(mod) ? '' : 'none';
  });
  /* 手风琴组：renderNav 已渲染时直接隐藏对应行；未渲染则交给 renderNav 下次刷新 */
  if (typeof renderNav === 'function') renderNav();
}
/* 2026-11-07：hub.js 改为登录闸门通过后**动态注入**，那时 DOMContentLoaded 早已触发 ⇒
   只挂事件会永远等不到，顶栏不会出现身份/退出，权限也套不上。按 readyState 分流。 */
if (typeof window !== 'undefined') {
  if (document.readyState === 'loading') window.addEventListener('DOMContentLoaded', () => { loadAuthPerms(); });
  else loadAuthPerms();
}

/* 2026-11-07 登录入口：顶栏常显，不依赖 401 触发。
   之前只有读/写 401 才会弹 openLoginModal，而 LAN 访问走「兼容旧令牌」分支返回 200
   ⇒ 永远不弹窗，用户看到的就是「只显示需要你处理、没有登录框」。
   现在：无 token 常显「登录」；有 token 显示身份 + 退出。
   非回环首次访问自动弹一次（sessionStorage 去重，避免每次刷新糊脸）。 */
function _isLocalHost() {
  const h = (location.hostname || '');
  return h === '127.0.0.1' || h === 'localhost' || h === '::1' || h === '[::1]';
}
function renderAuthBox() {
  const box = $('authBox');
  if (!box) return;
  const user = String(AUTH_USER || lsGet('hub.login.user') || '').trim();
  if (user) {
    /* 2026-11-08 用户要求：「右上角显示当前用户，点击可以注销」。
       身份本身即注销入口（不再依赖旁边的小按钮）；title 写明动作。 */
    box.innerHTML = '<button class="who" type="button" data-authact="logout" title="当前用户：'
      + escapeHtml(user) + ' · 点击退出登录">' + escapeHtml(user) + '<span class="who-sep">·</span>'
      + '<span class="who-out">退出</span></button>';
  } else {
    box.innerHTML = '<button class="btn xs pri" type="button" data-authact="login">登录</button>';
  }
  if (!box.dataset.bound) {
    box.dataset.bound = '1';
    box.addEventListener('click', (e) => {
      const b = e.target.closest('[data-authact]');
      if (!b) return;
      if (b.dataset.authact === 'login') {
        if (typeof openLoginModal === 'function') openLoginModal();
      } else if (b.dataset.authact === 'logout') {
        /* 2026-11-07：退出必须真的吊销服务端会话（HttpOnly Cookie 前端删不掉），
           再 reload 回登录闸门。 */
        fetch('/api/users/logout', { method: 'POST', credentials: 'same-origin' })
          .catch(() => {})
          .then(() => { lsRemove('hub.login.user'); location.reload(); });
      }
    });
  }
}
function maybeAutoLoginPrompt() {
  if (_isLocalHost()) return;
  if (AUTH_USER) return;   // 已有 Cookie 会话
  try {
    if (sessionStorage.getItem('hub.login.prompted')) return;
    sessionStorage.setItem('hub.login.prompted', '1');
  } catch (e) { /* 隐私模式：退化为显示按钮即可 */ }
  setTimeout(() => { if (typeof openLoginModal === 'function') openLoginModal({ auto: true }); }, 600);
}
async function api(path, opt) {
  const o = Object.assign({ credentials: 'same-origin' }, opt || {});
  /* 2026-11-07：body 是字符串时默认补 JSON Content-Type。
     缺它 fetch 按 text/plain 发，FastAPI 把 body 当裸字符串 ⇒ 422
     model_attributes_type（改密码/发 token/吊所有三个按钮就是这么全崩的）。
     已有显式头不覆盖；FormData/Blob 等非字符串 body 不受影响。 */
  if (typeof o.body === 'string') {
    const hs = Object.assign({}, o.headers || {});
    if (!Object.keys(hs).some(k => k.toLowerCase() === 'content-type')) hs['Content-Type'] = 'application/json';
    o.headers = hs;
  }
  /* 2026-11-07 用户裁定：浏览器不再持有任何 token —— 登录态由 HttpOnly Cookie 自动携带。
     401 = 会话缺失/过期 ⇒ 弹登录框，登录成功后只重试一次（不循环）。
     判据：写方法，或 401 体带 read_auth_required（读门拒绝面）。 */
  let r = await fetch(path, o);
  if (r.status === 401 && !o.__retried401) {
    const text0 = await r.text();
    let body0 = null;
    try { body0 = JSON.parse(text0); } catch (e0) { /* 非 JSON 401：原样抛 */ }
    const needLogin = isWriteMethod(o.method) || (body0 && (body0.read_auth_required || body0.login_required));
    if (needLogin) {
      /* 2026-11-07：模态登录框（openLoginModal）异步收集账号密码；
         成功返回 true，用户取消/超时返回 false。 */
      const done = await _promptLoginAsync();
      if (done) {
        o.__retried401 = true;
        r = await fetch(path, o);
        const text1 = await r.text();
        let data1;
        try { data1 = JSON.parse(text1); } catch (e1) { data1 = { raw: text1 }; }
        if (!r.ok) {
          const msg1 = (data1 && data1.detail && (data1.detail.error || (Array.isArray(data1.detail) ? data1.detail.map(d => d.msg).join(';') : data1.detail))) || ('HTTP ' + r.status);
          const err1 = new Error(typeof msg1 === 'string' ? msg1 : JSON.stringify(msg1));
          err1.http = r.status; err1.payload = data1;
          throw err1;
        }
        return data1;
      }
      /* 用户取消 ⇒ 不重试，走既有 401 抛出路径 */
    }
    const data = body0;
    if (!r.ok || !needTok) {
      const msg = (data && data.detail && (data.detail.error || (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(';') : data.detail))) || ('HTTP ' + r.status);
      const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
      err.http = r.status; err.payload = data;
      throw err;
    }
    return data;
  }
  if (r.status === 401 && o.__retried401) {
    const text2 = await r.text();
    let data2;
    try { data2 = JSON.parse(text2); } catch (e2) { data2 = { raw: text2 }; }
    const msg2 = (data2 && data2.detail) || ('HTTP 401');
    const err2 = new Error(typeof msg2 === 'string' ? msg2 : JSON.stringify(msg2));
    err2.http = 401; err2.payload = data2;
    throw err2;
  }
  const text = await r.text();
  let data;
  try { data = JSON.parse(text); } catch (e) { data = { raw: text }; }
  if (!r.ok) {
    const msg = (data && data.detail && (data.detail.error || (Array.isArray(data.detail) ? data.detail.map(d => d.msg).join(';') : data.detail))) || ('HTTP ' + r.status);
    const err = new Error(typeof msg === 'string' ? msg : JSON.stringify(msg));
    err.http = r.status;
    err.payload = data;
    throw err;
  }
  return data;
}

/* v0.13.185：toast 增强 —— 可选动作按钮 + 同文去重。
 *
 * 签名保持 `(msg, cls)` 向后兼容：第三参 opts 缺省时行为与旧版一致（4.2s 自动消失）。
 * 为什么要去重：`loadAgents` 失败每 30s 一条、`pollHealth` 每 15s 一条，Hub 一挂
 * 屏幕 4 分钟内能堆 12 条同样的字 —— 用户读到的是"页面在抖"而不是"服务不可达"。
 * 同 msg + 同 cls 在存活期内再次出现 ⇒ 复用原节点并重置计时（不新增节点）。
 *
 * opts.action = {label, fn}：错误提示带上「重试」入口。fn 抛错不影响关闭
 * （finally 里收节点），否则一次失败的重试会把提示永久钉在屏幕上。
 */
const _toastLive = new Map();     // "cls\u0000msg" -> 节点（同文复用）
function toast(msg, cls, opts = {}) {
  opts = opts || {};   // 显式传 null 也兜住（默认参数只挡 undefined）
  const ttl = Number.isFinite(opts.ttl) ? opts.ttl : 4200;
  const key = (cls || '') + '\u0000' + msg;
  const live = _toastLive.get(key);
  if (live && live.isConnected) {
    if (live._hubT) clearTimeout(live._hubT);
    live._hubT = setTimeout(() => { live.remove(); _toastLive.delete(key); }, ttl);
    return live;
  }
  const el = document.createElement('div');
  el.className = cls || '';
  const body = document.createElement('span');
  body.textContent = msg;
  el.appendChild(body);
  if (opts.action && opts.action.label && typeof opts.action.fn === 'function') {
    const b = document.createElement('button');
    b.type = 'button';
    b.className = 'toast-act';
    b.textContent = opts.action.label;
    b.addEventListener('click', () => {
      try { opts.action.fn(); } finally { el.remove(); _toastLive.delete(key); }
    });
    el.appendChild(b);
  }
  el._hubT = setTimeout(() => { el.remove(); _toastLive.delete(key); }, ttl);
  $('toast').appendChild(el);
  _toastLive.set(key, el);
  return el;   // v0.13.6：返回节点，供「链路恢复后收掉同一条提示」用（老调用方忽略返回值，行为不变）
}

/* F1（2026-10-31）：统一 toast 门面 —— kind ∈ {info, ok, warn, error}。
 *
 * 为什么是**包装**而不是改 toast() 签名：toast(msg, cls) 有 134 处调用，且
 * 03/02 消费它的返回值「链路恢复后收掉同一条提示」（tests/test_toast_a11y.py 钉着）。
 * 这里只做 kind → 既有 CSS 类 的映射，旧调用零改动、零语义变化。
 * 映射表同时是白名单：未知 kind 按 info 处理并留一条 console.warn（不静默吞掉）。
 * `error` 复用既有 .err 类（含 --danger-border/--danger-text token），不新增色值。 */
const TOAST_KIND_CLASS = { info: 'info', ok: 'ok', warn: 'warn', error: 'err' };
function showToast(kind, msg, opts = {}) {
  const k = Object.prototype.hasOwnProperty.call(TOAST_KIND_CLASS, kind) ? kind : 'info';
  if (k !== kind) console.warn('showToast(): 未知 kind "' + kind + '"，按 info 处理');
  return toast(String(msg == null ? '' : msg), TOAST_KIND_CLASS[k], opts);
}
window.showToast = showToast;   // 供其它分片与探针调用（bundle 非模块，显式挂更清楚）

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
  if (!el) return false;
  /* F1：busy 态用骨架占位而不是一行「加载中…」——列表区高度不塌、失败时不会
     因为「空白 → 突然出现内容」而被当成两次不同的加载。文字只留给读屏（aria-label）。
     纯几何骨架，颜色全部来自 :root token（--surface-2 / --hover），不新增色值。 */
  const label = escapeHtml(msg || '加载中…');
  el.innerHTML = '<div class="skel" role="status" aria-label="' + label + '">' +
    '<div class="skel-row"></div><div class="skel-row short"></div><div class="skel-row"></div>' +
    '</div>';
  return true;
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
// v0.13.165：skillCatalogDrawer（L1 目录）也是浮层 ⇒ 必须登记，否则
// 抽屉唯一性 / 导航清收 / Esc / inert 四条红线对它全部失效。
const OVERLAY_IDS = ['detailDrawer', 'skillDocDrawer', 'skillCatalogDrawer', 'usersModal'];   // v0.13.43：设置抽屉已拆；v0.13.196：用户授权模态登记（复用唯一入口/ESC/inert/遮罩）
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
  if (window.closeUserModal) closeUserModal();   // 导航即清用户授权模态（抽屉那条只管窄屏，模态无条件清）
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
  // v0.13.161：矩阵与 vitals **分两个请求**——vitals 是定时器落盘的快照（可陈旧），
  // 矩阵是实时 COUNT（归档计数随写变，10 分钟陈旧会让「刚提交的记忆」在看板上不存在）。
  // P1 判据由此从「进页 1 请求」改为「进页 2 请求、首屏按钮=手动提交框 1 个」：
  // 用户令 PT-20261025-11 明要写入入口可见，折叠它等于没有。
  if (page === 'memory') { memVitals(); memMatrixLoad(); }
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
  /* 2026-11-07 用户授权页：每次进页都重读（用户/token 可能随时被其他 admin 改） */
  if (page === 'settings-users') { usersPanelLoad(); }
  /* 2026-11-07 模型计量页：每次进页都重读（计量数据随调用实时增长） */
  if (page === 'settings-modelcalls') { mclPanelLoad(); }
  /* 2026-10-25：settings-memory 页已并入「记忆中心」（#page-memory 的本机旧层折叠块），
     不再有独立的进页钩子 —— 折叠块展开时才由 mxLegacyToggle() 拉 L1/L2/L3。 */
  if (page === 'ports' && !portsLoaded) { portsLoaded = true; loadPorts(); }
  if (page === 'telemetry') loadTelemetry();
  if (page === 'resources' && !resLoaded) { resLoaded = true; loadResources(); }
  // v0.13.47：原运行日志页的进页钩子随页面一并删除（内容并入设置→日志）
  if (page === 'chat') renderChatSide();
  // 2026-11-03 四框改版：进页只调一个入口（Agent/模型/轮次三框 + 历史 + 汇总）
  if (page === 'tasks') enterTasksPage();
  if (page === 'jobs') { fillAgentSelect($('jobAgent'), false); loadJobs(); }
  if (page === 'mcp') { loadMcp(); loadAcl(); }
  if (page === 'assets') loadAssets();   // P4 资产面板（只读门面聚合，07-asset-panel.js）
  // v0.13.170：Botboy 页每次进页都重读（state.json 是活文件），由 14-botboy.js 负责渲染
  if (page === 'botboy') loadBotboy(false);
}

function fillAgentSelect(sel, withAuto) {
  if (!sel) return;
  const cur = sel.value;
  sel.innerHTML = (withAuto ? '<option value="">自动选择执行 Agent</option>' : '<option value="">默认 jcode</option>') +
    AGENTS.map(a => '<option value="' + a.id + '">' + escapeHtml(a.name) + '</option>').join('');
  if (cur) sel.value = cur;
}

/* 卡片地址 → 当前访问主机（2026-10-24 扩写；原来只换 127.0.0.1）
 *
 * 为什么必须换：服务端给的 URL 里是**某一个**网卡的地址（dsh 甚至只能绑一张网卡：
 * 192.168.5.102:3080），而同一个 hub 会被 局域网 / Tailscale(100.x) / Tailscale IPv6
 * (fd7a::) / MagicDNS 名 访问。不换的后果就是「局域网能开、手机和外面全打不开」。
 *
 * 规则（只改 host 段，路径与 ?token=... 一个字节都不动）：
 *   ① 回环 127.0.0.1 / localhost / ::1 → 当前主机（上游原语义，保留）；
 *   ② 命中 SELF_ADDRS（后端 /api/agents 下发的本机自有地址）→ 当前主机。
 *      只做**整段 host 相等**匹配，不做子串替换 —— 否则 192.168.5.10 会吃掉
 *      192.168.5.102 这种前缀相同的地址。
 *   ③ 其它 host（外站链接、真远程机器）原样返回。
 * SELF_ADDRS 为空（接口未返回 / ip 命令不可用）时退化为只做 ①。
 */
let SELF_ADDRS = [];
function _selfHost(cur) {
  /* location.hostname 在部分浏览器对 IPv6 带方括号，统一成可拼进 URL 的形式 */
  const bare = String(cur || '').replace(/^\[|\]$/g, '');
  return bare.indexOf(':') >= 0 ? '[' + bare + ']' : bare;
}
function lanUrl(url) {
  if (!url) return url;
  const cur = _selfHost(location.hostname);
  if (!cur) return url;
  return url.replace(/^(https?:\/\/)(\[[0-9a-fA-F:]+\]|[^\/:?#]+)/, function (m, scheme, host) {
    const bare = host.replace(/^\[|\]$/g, '');
    if (bare === '127.0.0.1' || bare === 'localhost' || bare === '::1') return scheme + cur;
    if (SELF_ADDRS.indexOf(bare) >= 0) return scheme + cur;
    return m;
  });
}

/* ── 教室 ─────────────────────────────────────────── */
let HUB_HEALTH = null;  /* P2 工作台（v0.13.193）：pollHealth 每 15s 写入，15-workbench 只读 */



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
    /* 本机自有地址（后端 ip 实测）：卡片地址里的这些字面量在渲染时会被换成
       location.hostname，见 lanUrl。空表时 lanUrl 只做回环替换（旧行为）。 */
    if (Array.isArray(d.self_addrs)) SELF_ADDRS = d.self_addrs;
    agentFailStreak = 0;
    /* 2026-11-07：数据一到就先渲染左侧手风琴，再做可选的增强渲染。
       此前 renderNav() 排在 renderWorkbench() 之后，而后者一旦抛异常
       （v0.13.193 的 x.a 误用）就把整段 try 打断 ⇒ 侧栏永远停在
       「AGENTS 0 加载中…」，用户看到的就是「登录后页面不正常」。
       渲染顺序不该由增强面决定主面是否可见。 */
    renderNav();
    /* 进程级 token 的入口（dsh / reasonix）：服务一重启地址里的 token 就换，
       而 embedFrame 只在 dataset.src 变化时才导航 ⇒ 页面开着不动的话，帧里会一直
       停着旧 token 那份 401。数据到位点跟进一次，不等用户手动「重试嵌入」。
       （2026-10-30 报障「dsh web authentication required」的恢复面。） */
    if (typeof syncEmbedUrlIfChanged === 'function') syncEmbedUrlIfChanged();
    renderHomeStats();
    if (typeof renderWorkbench === 'function') renderWorkbench();  /* P2 工作台：agent 数据到位点重渲 */
    if (typeof loadWbTasks === 'function') loadWbTasks();          /* 任务面异步到位后再重渲一次 */
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
      /* v0.13.185：错误提示带「重试」动作 —— 旧行为是只报错，用户唯一出路是刷新整页
         （会丢掉终端与对话上下文）。action 走 toast 第三参，不改 134 处旧调用。 */
      toast('Hub 数据连续 ' + agentFailStreak + ' 次拉取失败：' + e.message, 'err',
            { ttl: 8000, action: { label: '重试', fn: () => { agentFailStreak = 0; loadAgents(); } } });
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
  /* Task2 补新鲜度：/api/agents 无 vitals 采集时刻字段（后端改造属非目标），
     这里 as_of = 本次渲染时刻（AGENTS 到位点），与 M3/M8 同名口径。 */
  const asOfEl = $('hsAsOf');
  if (asOfEl) asOfEl.textContent = 'as_of ' + new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  /* Task2 钻取：统计卡点击展开左侧对应分组（agents/infra，复用 navOpen 手风琴）。
     「过滤到不可用」无现成过滤器（navMatch 是搜索框专用），故只开组不另起过滤。 */
  const hsGroup = { agents: 'agents', running: 'agents', infra: 'infra', error: 'agents' };
  document.querySelectorAll('#homeStat [data-hs]').forEach(b => {
    b.onclick = () => { const g = hsGroup[b.dataset.hs]; if (!g) return; navOpen = g; lsSet('hub.nav.open', g); renderNav(); };
  });
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
    HUB_HEALTH = d;  /* P2 工作台：陈旧码行数据源 */
    HUB_HEALTH = d;  /* P2 工作台（v0.13.193）：15-workbench.js 读陈旧码行 */
    if (typeof renderWorkbench === 'function') renderWorkbench();
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

/* ── 设置（2026-11-07 C 档：全站单一认证面 = 用户名+密码登录）──
   三个子页（模型 / GitHub / 认证状态）是 main 里的 section.page，由左侧「设置」
   手风琴组切换 —— 入口走 data-sys ⇒ 委托 ⇒ go(page)。
   HUB_PASSCODE / TERM_TOKEN 两套共享口令已废弃，写端点由 writeauth.write_gate
   登录闸门保护，浏览器请求自带 HttpOnly 会话 Cookie，不再收任何口令。 */


/* ── 设置 → 模型子菜单（v0.13.41）──────────────────────────────────────
   交互两步：先选 agent，再选该 agent 要用的 CCR 模型；保存前给 diff 预览，
   保存直接走登录态（HttpOnly Cookie），不再收口令。
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
/* 设置页 select 的「上一个合法值」：付费档取消确认时回滚到这里。
   必须是**渲染后实际选中的值**，不能假设是 agent 的 hub_model —— 清单外补项、
   前缀形状都会让两者不同（v0.13.86 的教训）。 */
let SET_MODEL_PREV = '';
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
                      'page-settings-logs', 'page-settings-users', 'page-settings-modelcalls'];   // 2026-11-07：用户授权页 + 模型计量页

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
      /* v0.13.164：把「会失效的风险」提前到列表里 —— 用户点进去之前就该知道
         这个设置在这个 agent 上到底能不能生效。 */
      const eff = a.effective || {};
      if ((a.argv_effective || []).length) lines.push('实际注入：' + a.argv_effective.join(' '));
      if (eff.catalog && eff.catalog.checked && eff.catalog.ok === false) {
        lines.push('⚠ 该模型已不在 CCR 在册清单（' + eff.catalog.size + ' 条）');
      }
      (eff.warnings || []).forEach(w => lines.push('⚠ ' + w));
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

/* ── 模型计费标记（判定在后端 `api_models.model_billing`，这里只渲染）──
   为什么要有：CCR 清单里免费与付费混在同一个 vendor 分组里，
   `alibaba/qwen3.8-max` 与 `openrouter/nvidia/nemotron-3.5-lightning:free`
   在下拉里长得一模一样，选错就是真扣费。后端给 `billing` 字段，这里只负责
   **让它可见**并在落到付费档时挡一次确认。
   ⚠ 缺 `billing` 字段（旧后端 / 旧缓存串）时按付费处理 —— 宁可多问一次。 */
function modelIsFree(m) { return !!m && m.billing === 'free'; }
function modelBillingTag(m) { return modelIsFree(m) ? '免费' : '付费'; }
function modelOptionLabel(mid, m) {
  const base = (m && m.name && m.name !== mid) ? (mid + ' — ' + m.name) : mid;
  return '[' + modelBillingTag(m) + '] ' + base;
}
/* 付费确认：**同一个模型 ID 只问一次**（记住答案，不每次弹）。
   不用自定义浮层：这里只需要一个是/否，原生 confirm 不引入第二套浮层状态，
   手机端也是系统弹窗 —— 是 §4.2「浮层唯一性」成本最低的满足方式。
   confirmFn 可注入：闸门在 node 里跑，不需要 DOM。 */
function modelConfirmPaid(mid, m, confirmFn) {
  if (!mid || modelIsFree(m)) return true;
  const key = 'hub.paidack.' + mid;
  if (lsGet(key) === '1') return true;
  const ask = confirmFn || (typeof window !== 'undefined' && window.confirm
    ? function (t) { return window.confirm(t); }
    : null);
  if (!ask) return true;          // 宿主没有弹窗能力时不阻塞（标记仍然可见）
  const yes = ask(mid + '\n\n按量计费，使用会计入账单（可能扣费）。确认使用？');
  if (yes) lsSet(key, '1');
  return !!yes;
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
        const label = modelOptionLabel(mid, m);   // 前置 [免费]/[付费]，值不变
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
          escapeHtml(modelOptionLabel(m.id, m)) + '</option>';
      }).join('') + '</optgroup>';
    }
  }
  /* 顺序：撤销项固定第一（与改前「默认（网关路由）」同位，位置不飘），
     其后才是「当前值不在清单内」的补项，再是各 provider 分组。
     ⚠ 补项必须带 selected 才能被选中 —— 位置不决定选中态，属性才决定。 */
  sel.innerHTML = html + extra;
  /* 记下渲染后的合法值：用户选到付费档又拒绝确认时，回滚到这里（§4.2 ③
     「遮罩/浮层单一出口」同理 —— 取消必须真的回到原值，不能停在半选态）。 */
  SET_MODEL_PREV = sel.value || '';
  settingsModelHint(a);
}

/* v0.13.86：下拉下方的一行口径说明。存在的理由：write_mode 的三类语义里，
   只有 argv 型（claude 之外那些「无配置文件可落」的）需要提醒"只影响 hub 拉起的会话"；
   native 型（qoder）必须说清"CCR 模型不写进该字段、只经 --model 注入"，
   否则用户会以为选了原生名就等于把 qoder 接到了 CCR 上。 */
/* 从 select 的**值**反查模型元数据。
   为什么单独成函数：设置页与对话页两个下拉的 value 形状不同（opencode 带
   `ccr/` 前缀），而「值 → 模型」这一步一旦两边各写一遍就会漏掉前缀 ⇒
   计费标记在其中一个页面静默变成「付费」（缺 m 时按 paid 兜底）。
   返回 null = 空值（撤销项），不是错误。 */
function modelMetaFromValue(val, a, models) {
  if (!val) return null;
  const list = models || [];
  const prefix = (a && a.value_prefix) || '';
  const mid = (prefix && val.indexOf(prefix) === 0) ? val.slice(prefix.length) : val;
  let m = list.find(x => x.id === mid);
  /* 前缀兜底：`SET_AGENTS` 还没加载时前端拿不到 `value_prefix`（opencode 的
     `ccr/` 就是这么丢的），但**值一定以模型 ID 结尾**。不加这层会走向反面：
     匹配不到 ⇒ `m` 为 null ⇒ 按 paid 兜底 ⇒ 连免费模型也弹「会扣费」，
     用户会以为整个功能坏了。 */
  if (!m) m = list.find(x => val.length > x.id.length && val.endsWith('/' + x.id));
  return { id: m ? m.id : mid, m: m || null };
}

/* 设置页：选到付费档先确认；取消则**真的回到上一个值**（停在下拉里不算取消）。 */
$('setModelSel')?.addEventListener?.('change', function () {
  const sel = $('setModelSel');
  if (!sel) return;
  const a = SET_AGENTS.find(x => x.id === SET_AGENT) || null;
  const meta = modelMetaFromValue(sel.value, a, (SET_MODELS || {}).models);
  if (!meta) { SET_MODEL_PREV = sel.value || ''; settingsModelHint(a); return; }
  if (!modelConfirmPaid(meta.id, meta.m)) {
    sel.value = SET_MODEL_PREV;
    toast('已取消：' + meta.id + ' 按量计费', 'err');
    return;
  }
  SET_MODEL_PREV = sel.value;
  settingsModelHint(a);
  /* Task11：选模型后自动预览 diff（保存前必须看到改了啥） */
  settingsPreviewModel();
});

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
  /* 计费档必须实时跟着下拉走：这是「保存后会一直生效」的设置，
     用户需要在下笔前就看到自己在选免费还是付费。 */
  const meta = modelMetaFromValue(setSelectedModel(), a, (SET_MODELS || {}).models);
  let tag = '';
  if (meta) {
    tag = modelIsFree(meta.m)
      ? '<span class="stale-tag" style="color:var(--muted);border-color:var(--border)">免费</span> '
      : '<span class="stale-tag">付费·按量</span> ';
  }
  el.innerHTML = tag + escapeHtml(bits.join(''));
}

function setSelectedModel() { return $('setModelSel') ? $('setModelSel').value : ''; }
/* password 框显示/隐藏切换（GitHub PAT 等）：原生细节，只改 type 属性，不用新依赖 */
function passEye(btn, id) {
  const inp = $(id);
  if (!inp) return;
  inp.type = inp.type === 'password' ? 'text' : 'password';
  btn.textContent = inp.type === 'password' ? '显示' : '隐藏';
}

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

  const btn = $('setApplyBtn');
  if (btn) btn.disabled = true;                // 只防连点，保存完立刻恢复
  try {
    const d = await api('/api/settings/model/apply', {
      method: 'POST', noToken: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: SET_AGENT, model: model })
    });
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
    /* v0.13.164：保存后立刻做一次静态自证，把「写入成功」升级成「四条判据全过」。
       只报 200 不叫生效 —— 清单里没这个 ID / 该 agent 不注入，界面必须当场说出来。 */
    try {
      const v = await api('/api/settings/model/verify?agent_id=' + encodeURIComponent(SET_AGENT));
      if (d.mode === 'clear') settingsRenderApplied(d); else settingsRenderVerify(v, false, d.applied);
    } catch (e) {
      settingsRenderApplied(d);               // 验证本身失败不推翻保存结果
    }
  } catch (e) {
    settingsRenderError('保存失败', e);
    toast('保存失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;            // 保存完必须恢复可点，否则第二次就"点不动"
  }
}

/* v0.13.164：执行逻辑自证。
   用户反复报"设置好了没用"，而页面原来只有"已生效"三个字 —— 那三个字的唯一依据是
   HTTP 200。现在把四条判据（落点/清单/注入/通道）逐条摆出来，错了就指出哪一条错。
   live=true 会真打一次网关（付费模型计费）。 */
async function settingsVerifyModel(live) {
  if (!SET_AGENT) return toast('先选一个 agent', 'err');

  const box = $('setDiffBox');
  try {
    const v = live
      ? await api('/api/settings/model/verify-live', {
          method: 'POST', noToken: true,
          headers: { 'Content-Type': 'application/json' },
          body: JSON.stringify({ agent_id: SET_AGENT })
        })
      : await api('/api/settings/model/verify?agent_id=' + encodeURIComponent(SET_AGENT));
    settingsRenderVerify(v, live);
  } catch (e) {
    settingsRenderError('验证失败', e);
    toast('验证失败：' + e.message, 'err');
  }
}

function settingsRenderVerify(v, live, applied) {
  const box = $('setDiffBox');
  if (!box) return;
  const bad = v.verdict !== 'ok';
  const rows = (v.checks || []).map(c => {
    const mark = c.ok === true ? '✓' : (c.ok === false ? '✗' : '·');
    const style = c.ok === false ? ' style="color:var(--st-error)"' : '';
    return '<div class="v"' + style + '>' + mark + ' ' + escapeHtml(c.name) + '：' +
      escapeHtml(c.detail || '') + '</div>';
  }).join('');
  const chan = Object.keys(v.channels || {}).map(k =>
    k + (v.channels[k].applies ? ' ✓' : ' ✗')).join('　');
  const warn = (v.warnings || []).length
    ? '<div class="set-diff-row">' + v.warnings.map(w =>
        '<div class="v" style="color:var(--st-error)">⚠ ' + escapeHtml(w) + '</div>').join('') + '</div>'
    : '';
  /* 保存后立刻自证：把刚落盘的备份路径一并带出来，用户不必再去翻日志。 */
  const bak = (applied || []).length
    ? '<div class="set-diff-row">' + applied.map(a =>
        '<div class="v">备份：' + escapeHtml(a.backup || '') + '</div>').join('') + '</div>'
    : '';
  box.innerHTML = '<div class="set-diff">' +
    '<div class="set-diff-hd">' + (bad ? '✗ 验证未通过' : '✓ 验证通过') +
    '：' + escapeHtml(v.agent_id || '') + ' → ' + escapeHtml(v.model || '（未设置）') +
    '　' + escapeHtml(live ? '（含真实调用）' : '（静态）') + '</div>' +
    '<div class="set-diff-row">' + rows +
    '<div class="v">通道：' + escapeHtml(chan) + '</div></div>' + warn + bak +
    '<div class="hint" style="margin-top:6px">静态验证只读；真实调用会按模型计费，' +
    '且只证明"网关此刻认这个 ID"，不代替新开终端里的实际体验。</div></div>';
  box.style.display = 'block';
}

/* 状态行渲染的**唯一出口**（选 agent 时与保存后刷新都走它，别各写一份文案）：
   v0.13.85 起把「这个 agent 的模型字段认什么」写进来；v0.13.164 再把
   「实际会追加的命令」（grok 注入的是 ccr-hub 而不是模型 ID）与「会失效的风险」摆出来。 */
function settingsAgentMeta(a) {
  const el = $('setAgentMeta');
  if (!el || !a) return;
  const wm = SET_WRITE_MODE[a.write_mode || 'argv'] || SET_WRITE_MODE.argv;
  const eff = a.effective || {};
  const parts = ['字段认：' + wm.label,
                 '当前：' + (a.current || '（未读到）'),
                 'hub 侧：' + (a.hub_model || '未设置')];
  const argv = (a.argv_effective || []).join(' ');
  if (argv) parts.push('实际注入：' + argv);
  else if (a.argv) parts.push('注入口径：' + a.argv + ' <模型>');
  el.innerHTML = escapeHtml(parts.join('｜')) +
    '<div class="hint" style="margin-top:4px">' + escapeHtml(wm.hint) + '</div>' +
    ((a.files || []).length ? '<div class="hint">配置文件：' + escapeHtml(a.files.join(' / ')) + '</div>' : '') +
    ((eff.warnings || []).length
      ? eff.warnings.map(w => '<div class="hint" style="color:var(--st-error)">⚠ ' +
          escapeHtml(w) + '</div>').join('')
      : '');
}

function settingsRefreshMeta(model) {
  const a = SET_AGENTS.find(x => x.id === SET_AGENT);
  const el = $('setAgentMeta');
  if (!el) return;
  if (!a) { el.textContent = '已保存：' + model; return; }
  settingsAgentMeta(a);
}

/* ── 撤销上一次保存（v0.13.181，借 magpie 的 disconnect 语义）────────────
   为什么要这个：落盘前有备份，但回滚要人自己去目录里挑 .bak、判断哪份对、
   再手工 cp —— 手机上根本做不了。后端把「刚写的那次」记在 model_writes 表里，
   这里一键还原（还原前它还会再备份一次当前状态，撤错了还能再撤回去）。
   破坏性动作 ⇒ 必须 confirm。 */
async function settingsUndoModel() {
  if (!SET_AGENT) return toast('先选一个 agent', 'err');

  if (!window.confirm('把 ' + SET_AGENT + ' 的配置文件还原到「上一次保存之前」？\n\n' +
      '· 只撤销最近一次保存，更早的不动\n' +
      '· 还原前会再备份一次当前状态\n' +
      '· 已开着的会话不受影响，新开终端才读新配置')) return;
  const btn = $('setUndoBtn');
  if (btn) btn.disabled = true;
  try {
    const d = await api('/api/settings/model/undo', {
      method: 'POST', noToken: true,
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify({ agent_id: SET_AGENT })
    });
    SET_AGENTS = [];
    SET_DIFF = null;
    await settingsModelsLoad(true);
    settingsRenderUndone(d);
    toast(SET_AGENT + '：已还原 ' + (d.restored || []).length + ' 个文件', 'ok');
  } catch (e) {
    settingsRenderError('撤销失败', e);
    toast('撤销失败：' + e.message, 'err');
  } finally {
    if (btn) btn.disabled = false;
  }
}

function settingsRenderUndone(d) {
  const box = $('setDiffBox');
  if (!box) return;
  box.innerHTML = '<div class="set-diff"><div class="set-diff-hd">已撤销：' +
    escapeHtml(d.agent_id) + ' 的模型写入已还原</div>' +
    (d.restored || []).map(r => '<div class="set-diff-row"><div class="k">' +
      escapeHtml(r.file) + '</div><div class="v">还原自备份：' +
      escapeHtml(r.from_backup) + '</div></div>').join('') +
    '<div class="v">hub 侧模型现在是：' + escapeHtml(d.hub_model_to || '（未设置）') +
    '。开新终端前请确认这就是你想要的。</div></div>';
  box.style.display = 'block';
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
  await settingsGithubSources();
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
  ];
  box.innerHTML = rows.map(r => '<div><span class="k">' + escapeHtml(r[0]) + '：</span>' +
    '<span class="v">' + escapeHtml(r[1]) + '</span></div>').join('') +
    (note ? '<div style="margin-top:6px">' + escapeHtml(note) + '</div>' : '');
  box.style.display = '';
}

/* ── 仓库源列表（v0.13.197）：设置页维护，项目页按源分组 ──────────────── */
var GH_SOURCES = [];

async function settingsGithubSources() {
  try {
    const d = await api('/api/settings/github/sources');
    GH_SOURCES = (d && d.sources) || [];
  } catch (e) {
    GH_SOURCES = [];
  }
  ghRenderSources();
}

function ghRenderSources() {
  const box = $('ghSrcList');
  if (!box) return;
  if (!GH_SOURCES.length) {
    box.innerHTML = '<div class="v">（还没有仓库源：上面添加一条，或保存「远程 / 归属」后会自动生成一条默认源）</div>';
    box.style.display = '';
    return;
  }
  box.innerHTML = GH_SOURCES.map(s => '<div class="set-diff-row">' +
    '<div class="k">' + escapeHtml(s.label || s.owner || s.git_host || '') + '</div>' +
    '<div class="v">' + escapeHtml(s.api_base || '') +
    (s.owner ? ' · ' + escapeHtml(s.owner) : ' · 当前 key 可见全部') +
    ' · ' + (s.repo_count || 0) + ' 仓' +
    (s.enabled ? '' : ' · 已停用') +
    (s.last_error ? ' · ⚠ ' + escapeHtml(s.last_error) : '') + '</div>' +
    '<button class="btn ghost sm" data-settings-act="gh-src-toggle" data-sid="' +
    escapeHtml(s.id) + '">' + (s.enabled ? '停用' : '启用') + '</button>' +
    '<button class="btn ghost sm" data-settings-act="gh-src-del" data-sid="' +
    escapeHtml(s.id) + '">删除</button></div>').join('');
  box.style.display = '';
}

async function settingsGithubSourceAdd() {
  const el = $('ghSrcInput');
  const val = el ? el.value.trim() : '';
  if (!val) { toast('先填账号或地址', 'err'); return; }
  try {
    const d = await ghPost('/api/settings/github/sources', { value: val });
    if (el) el.value = '';
    await settingsGithubSources();
    await settingsGithubLoad(true);
    toast(d.deduped ? '这个源已经存在' : ('已添加仓库源' + (d.note ? '：' + d.note : '')), 'ok');
  } catch (e) {
    toast('添加失败：' + e.message, 'err');
  }
}

async function settingsGithubSourceToggle(sid, on) {
  try {
    await api('/api/settings/github/sources/' + encodeURIComponent(sid), {
      method: 'PATCH', body: JSON.stringify({ enabled: !!on }) });
    await settingsGithubSources();
    await settingsGithubLoad(true);
    toast(on ? '已启用该源' : '已停用该源', 'ok');
  } catch (e) {
    toast('切换失败：' + e.message, 'err');
  }
}

async function settingsGithubSourceDel(sid) {
  if (!confirm('删除这个仓库源？（只影响本页清单，不删本地仓库、不删 key）')) return;
  try {
    await api('/api/settings/github/sources/' + encodeURIComponent(sid), { method: 'DELETE' });
    await settingsGithubSources();
    await settingsGithubLoad(true);
    toast('已删除仓库源', 'ok');
  } catch (e) {
    toast('删除失败：' + e.message, 'err');
  }
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

/* 2026-11-07 C 档：错误直接呈现，不再有任何口令输入环节。 */
function ghRenderError(prefix, e) {
  const box = $('ghStatus');
  if (!box) return;
  box.innerHTML = '<div class="v">' + escapeHtml(prefix + '：' + (e.message || String(e))) +
    (e.http ? '（HTTP ' + escapeHtml(String(e.http)) + '）' : '') + '</div>';
  box.style.display = '';
}


function ghPost(path, body) {
  /* 2026-11-07 C 档：四个写端点走 write_gate 登录闸门，请求自带 HttpOnly Cookie。 */
  return api(path, { method: 'POST', noToken: true,
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body) });
}

async function settingsGithubTest() {
  const p = ghPayload({ api_base: $('ghApiBase').value.trim() || undefined });
  try {
    const d = await ghPost('/api/settings/github/test', p);
    if (!d.ok) return settingsGithubStatus('试连失败：' + (d.error || '未知'));
    const rate = d.rate || {};
    settingsGithubStatus('试连成功：' + (d.login || '?') + ' · key ' + (d.token_type || '') +
      ' ' + (d.token_mask || '') + ' · 配额 ' + (rate.remaining === undefined ? '?' :
        rate.remaining + '/' + rate.limit) + ' · 列仓库 ' + (d.repo_probe && d.repo_probe.ok ? '可用' : '不可用') +
      ' · ' + (d.took_ms || 0) + 'ms');
  } catch (e) {
    toast('试连失败：' + e.message, 'err');
  }
}

async function settingsGithubApply() {
  const p = ghPayload({});
  try {
    const d = await ghPost('/api/settings/github/apply', p);
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
    toast('保存失败：' + e.message, 'err');
  }
}

async function settingsGithubClear() {
  if (!confirm('清除 Hub 侧的 GitHub 设置（地址/key/归属/落点），回落到环境变量与内置默认？')) return;
  try {
    const d = await ghPost('/api/settings/github/clear', {});
    await settingsGithubLoad(true);
    toast('已清除：' + ((d.cleared || []).join('、') || '（本来就是空的）'), 'ok');
  } catch (e) {
    toast('清除失败：' + e.message, 'err');
  }
}

async function settingsGithubRefresh() {
  try {
    const d = await ghPost('/api/settings/github/refresh', {});
    await settingsGithubLoad(true);
    if (d.errors && d.errors.length) toast('重拉完成但有告警：' + d.errors[0], 'err');
    else toast('已重拉远端清单：' + d.count + ' 个仓库', 'ok');
  } catch (e) {
    toast('刷新失败：' + e.message, 'err');
  }
}

/* ── 设置 → 日志子菜单（v0.13.46）────────────────────────────────────────
   数据源 GET /api/hublog：journald 服务日志 + profile_events 操作事件，合并按时间倒序。
   三条纪律与另三个设置页同源：① 无浮层（正文出页，筛选控件全在页内）；
   ② 按钮一律 data-settings-act 走同一委托，无 inline onclick；
   ③ **鉴权失败不弹 prompt**（手机/APP WebView 吞弹窗 ⇒ 点了没反应）：原因写进页内 #logStats。
   2026-11-07 C 档：读端点身份就是会话 Cookie（api() 同源自动带），不再拼 x-hub-token、
   不再二次输入口令；未登录 401 时提示重新登录。
   var 声明而非 let：go()（06 分片顶层就会跑）可能经 settingsLogsLoad 读到它，
   与 setModelPageLoaded 同一条 TDZ 纪律。 */
var LOG_LAST = null;      // 最近一次结果：复制/导出复用，不再为同一次查看打两次后端
var logPage = 0;          // Task11 日志分页（JS 变量，每页 100 行）
var logQTimer = null;
function logQDebounced() {
  if (logQTimer) clearTimeout(logQTimer);
  logQTimer = setTimeout(function () { logPage = 0; settingsLogsLoad(); }, 300);
}
function logPageGo(d) {
  logPage = Math.max(0, logPage + d);
  if (LOG_LAST) settingsLogsRender(LOG_LAST);
}

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

/* ── 用户授权面板（2026-11-07；2026-11-08 UI 收口）──
   读 /api/users 渲染；非 admin 只渲染自己的概览。写操作需 admin，被后端 403 时
   由模态内错误位说明原因。
   浮层：登记进 OVERLAY_IDS，复用 openOverlay/closeOverlay 唯一入口（§4.2 三条）；
   本页所有弹窗走 openUserModal 一个出口，不再用原生 prompt/confirm。 */
const USERS_MODAL_ID = 'usersModal';
let _usersCache = null;
let _usersModalCfg = null;
let _usersModalOpenedAt = 0;

/* 本页唯一浮层出口：表单 / 确认 / 结果三种形态共用一个壳。
   cfg = { title, fields?, text?, confirmText?, danger?, onSubmit? }
     fields: [{id,label,type?,placeholder?,value?,required?,min?,help?,autocomplete?}]
     onSubmit(vals, m, setErr) → Promise；resolve 即关闭，抛错则把 message 写进错误位；
       返回 false 表示调用方已自行接管（例如切到结果态），不自动关闭。
     confirmText 为空且无 onSubmit ⇒ 结果态（只留「关闭」）。 */
function openUserModal(cfg) {
  let m = $(USERS_MODAL_ID);
  if (!m) {
    m = document.createElement('div');
    m.id = USERS_MODAL_ID;
    m.className = 'modal-mask';
    m.style.zIndex = '95';        // 高于抽屉(60)，与 #tokenModal 同档
    m.innerHTML =
      '<div class="modal" role="dialog" aria-modal="true" aria-labelledby="usersModalTitle">' +
      '<h3 id="usersModalTitle"></h3>' +
      '<div id="usersModalBd"></div>' +
      '<div id="usersModalErr" class="hint" role="status" style="color:var(--danger-text);min-height:16px;margin:4px 0"></div>' +
      '<div class="row" style="justify-content:flex-end;margin-top:10px">' +
      '<button class="btn ghost" data-ux-close>取消</button>' +
      '<button class="btn" data-ux-ok></button></div></div>';
    document.body.appendChild(m);
    let downOnMask = false;
    m.addEventListener('pointerdown', e => { downOnMask = (e.target === m); });
    m.addEventListener('click', e => {
      if (e.target !== m || !downOnMask) return;
      downOnMask = false;
      if (Date.now() - _usersModalOpenedAt < 400) return;   // 幽灵点击守卫（照 03:1157）
      closeUserModal();
    });
    m.querySelector('[data-ux-close]').addEventListener('click', () => closeUserModal());
    m.querySelector('[data-ux-ok]').addEventListener('click', () => _usersModalSubmit());
    m.addEventListener('keydown', e => {
      if (e.key === 'Escape') { e.stopPropagation(); closeUserModal(); }
      else if (e.key === 'Enter' && e.target.tagName === 'INPUT') { e.preventDefault(); _usersModalSubmit(); }
    });
    /* 菜单授权树的组头/叶子联动：整个弹窗只此一个 change 监听点。 */
    m.querySelector('#usersModalBd').addEventListener('change', _usersTreeCascade);
  }
  const bd = m.querySelector('#usersModalBd');
  const fields = cfg.fields || [];
  m.querySelector('#usersModalTitle').textContent = cfg.title || '';
  m.querySelector('#usersModalErr').textContent = '';
  bd.innerHTML = (cfg.text || '') + fields.map(f =>
    '<div class="row" style="align-items:center">' +
    '<label for="uf-' + f.id + '" style="min-width:100px;font-size:var(--fs-sm);color:var(--text-2)">' + escapeHtml(f.label) + '</label>' +
    /* input 作为 flex item 默认 min-width:auto，会顶住固有宽度把窄屏 .modal 撑出边
       （375 实测 input 右缘 364 > 模态 360）；min-width:0 才让 .row input{flex:1} 真收缩。 */
    '<input id="uf-' + f.id + '" type="' + (f.type || 'text') + '"' +
    (f.type === 'checkbox' ? ' style="flex:0 0 auto;width:auto"' : ' style="min-width:0"') + '></div>' +
    (f.help ? '<div class="hint" style="margin:-4px 0 8px 108px">' + escapeHtml(f.help) + '</div>' : '')
  ).join('');
  fields.forEach(f => {
    const el = bd.querySelector('#uf-' + f.id);
    if (!el) return;
    if (f.placeholder) el.placeholder = f.placeholder;
    if (f.value != null) el.value = f.value;
    if (f.autocomplete) el.autocomplete = f.autocomplete;
  });
  const ok = m.querySelector('[data-ux-ok]');
  const cancel = m.querySelector('[data-ux-close]');
  const result = !cfg.onSubmit;
  ok.style.display = result ? 'none' : '';
  ok.disabled = false;
  ok.textContent = cfg.confirmText || '确认';
  ok.className = 'btn' + (cfg.danger ? ' danger' : '');
  cancel.textContent = result ? '关闭' : '取消';
  _usersModalCfg = cfg;
  _usersModalOpenedAt = Date.now();
  openOverlay(USERS_MODAL_ID);      // 唯一入口：清其它浮层 + inert + 焦点 + 遮罩
  if (!result) setTimeout(() => { const f = bd.querySelector('input'); if (f) f.focus(); }, 30);
}
function closeUserModal() {
  closeOverlay(USERS_MODAL_ID);
  _usersModalCfg = null;
}
async function _usersModalSubmit() {
  const cfg = _usersModalCfg;
  if (!cfg || !cfg.onSubmit) return;
  const m = $(USERS_MODAL_ID);
  if (!m) return;
  const err = m.querySelector('#usersModalErr');
  const ok = m.querySelector('[data-ux-ok]');
  const vals = {};
  (cfg.fields || []).forEach(f => {
    const el = m.querySelector('#uf-' + f.id);
    vals[f.id] = el ? (el.value || '').trim() : '';
  });
  for (const f of (cfg.fields || [])) {
    if (f.required && !vals[f.id]) { err.textContent = '请填写「' + f.label + '」'; return; }
    if (f.min && vals[f.id].length < f.min) { err.textContent = '「' + f.label + '」至少 ' + f.min + ' 位'; return; }
  }
  ok.disabled = true;
  err.textContent = '处理中…';
  try {
    const r = await cfg.onSubmit(vals, m, s => { err.textContent = s; });
    if (r === false) return;      // 调用方已切到结果态，别把新弹窗关掉
    closeUserModal();
  } catch (e) {
    err.textContent = (e && e.message) || '操作失败，请重试';
    ok.disabled = false;
  }
}

async function usersPanelLoad() {
  const box = $('usersPanel');
  if (!box) return;
  if (!boxBusy('usersPanel', '正在加载用户')) return;
  try {
    const d = await api('/api/users');
    _usersCache = d;
    usersPanelRender(d);
  } catch (e) {
    box.innerHTML = '<div class="hint" style="color:var(--danger-text)">加载失败：' +
      escapeHtml(e.message) + '。请检查网络后重试。' +
      ' <button class="btn sm" data-settings-act="users-load">重试</button></div>';
  }
}
function usersPanelRender(d) {
  const box = $('usersPanel');
  if (!box) return;
  if (!d.rows || !d.rows.length) {
    box.innerHTML = '<div class="hint">还没有用户。点「新建用户」创建第一个（仅管理员可操作）。</div>';
    return;
  }
  const isAdmin = !!d.admin;
  const meName = String(lsGet('hub.login.user') || '').trim();
  const btn = (act, name, label, cls) =>
    '<button class="btn xs ghost' + (cls ? ' ' + cls : '') + '" data-uact="' + act + '" data-uname="' +
    escapeHtml(name) + '" aria-label="' + escapeHtml(label + '：' + name) + '" title="' +
    escapeHtml(label + '：' + name) + '">' + escapeHtml(label) + '</button>';
  box.innerHTML = d.rows.map(u => {
    const name = u.username;
    const ops = isAdmin
      ? btn('token', name, '签发令牌') + btn('revoke', name, '吊销令牌') +
        btn('perms', name, '编辑授权') + btn('del', name, '删除', 'danger')
      : '';
    /* 2026-11-07 用户裁定：所有用户都能改自己的密码，超管也能改自己的/替别人改。 */
    const pw = (isAdmin || meName === name) ? btn('pw', name, '修改密码') : '';
    return '<div class="sp-card" style="margin-bottom:10px">' +
      '<div class="sp-hd"><b>' + escapeHtml(name) +
      (u.is_admin ? ' <span class="hint">（管理员）</span>' : '') + '</b>' +
      '<span class="hint">' + (u.live_tokens || 0) + ' 个有效令牌 · ' +
      (u.perm_count || 0) + ' 项授权</span></div>' +
      '<div class="sp-bd" style="display:flex;gap:6px;flex-wrap:wrap">' + pw + ops + '</div></div>';
  }).join('');
  /* 卡片按钮不再逐元素绑定：统一由 settingsDelegates 的 data-* 出口接管（§4.2）。 */
}
/* 改密码：所有用户可改自己的，admin 可替别人改（2026-11-07 裁定）。
   自己改须带旧密码；改完后端会吊销该用户全部会话 ⇒ 必须明确引导重新登录，
   不让 toast 在 location.reload() 前一闪而过。 */
async function usersChangePw(name) {
  const meName = String(lsGet('hub.login.user') || '').trim();
  const isSelf = meName === name;
  const fields = [];
  if (isSelf) fields.push({ id: 'old', label: '当前密码', type: 'password',
    autocomplete: 'current-password', help: '首次设置可留空。' });
  fields.push({ id: 'np', label: '新密码', type: 'password', required: true, min: 8,
    autocomplete: 'new-password', help: '至少 8 位。' });
  fields.push({ id: 'np2', label: '确认新密码', type: 'password', required: true,
    autocomplete: 'new-password' });
  const text = isSelf ? '' :
    '<div class="hint" style="margin-bottom:10px">正在为「' + escapeHtml(name) +
    '」设置新密码，无需其原密码。</div>';
  openUserModal({
    title: isSelf ? '修改我的密码' : '设置 ' + name + ' 的密码',
    text, fields, confirmText: '保存新密码',
    onSubmit: async (v, m) => {
      if (v.np !== v.np2) throw new Error('两次输入的新密码不一致，请重新输入');
      await api('/api/users/' + encodeURIComponent(name) + '/password', {
        method: 'POST', body: JSON.stringify({ old_password: v.old || '', new_password: v.np })
      });
      if (isSelf) {
        openUserModal({
          title: '密码已更新',
          text: '<div class="hint" style="line-height:1.7">你的登录会话已失效，请用<b>新密码</b>重新登录。<br>点「重新登录」会刷新页面并清除本地登录态。</div>',
          confirmText: '重新登录',
          onSubmit: async () => {
            lsRemove('hub.login.user'); lsRemove('hub.term.token');
            location.reload();
          }
        });
        return false;             // 已切到结果态，别把新弹窗关掉
      }
      toast('已更新 ' + name + ' 的密码', 'ok');
      usersPanelLoad();
    }
  });
}
async function usersIssue(name) {
  openUserModal({
    title: '签发令牌：' + name,
    text: '<div class="hint" style="line-height:1.7">将签发一枚新的访问令牌。<b>明文只显示这一次</b>，请立即复制保存。</div>',
    confirmText: '签发',
    onSubmit: async (v, m) => {
      const d = await api('/api/users/' + encodeURIComponent(name) + '/credentials',
        { method: 'POST', body: '{}' });
      if (!d.token) throw new Error(d.detail || '签发失败，请重试');
      openUserModal({
        title: '令牌已签发',
        text: '<div class="token-box" id="usersNewToken">' + escapeHtml(d.token) + '</div>' +
              '<div class="hint">请立即复制保存，关闭后不再显示。</div>',
        confirmText: '复制令牌',
        onSubmit: async () => {
          try { await navigator.clipboard.writeText(d.token); toast('已复制令牌', 'ok'); }
          catch (e) { throw new Error('浏览器未给剪贴板权限，请长按选中上面的令牌手动复制'); }
        }
      });
      return false;
    }
  });
}
async function usersRevoke(name) {
  openUserModal({
    title: '吊销令牌：' + name,
    text: '<div class="hint" style="line-height:1.7">将吊销「' + escapeHtml(name) +
      '」的全部有效令牌，旧令牌立即失效，使用该令牌的客户端需重新登录。</div>',
    confirmText: '吊销', danger: true,
    onSubmit: async () => {
      const d = await api('/api/users/' + encodeURIComponent(name) + '/revoke',
        { method: 'POST', body: '{}' });
      toast('已吊销 ' + (d.revoked || 0) + ' 个令牌', 'ok');
      usersPanelLoad();
    }
  });
}
async function usersPerms(name) {
  let detail;
  try {
    detail = await api('/api/users/' + encodeURIComponent(name));
  } catch (e) { toast('读取授权失败：' + e.message, 'err'); return; }
  const cur = detail.perms || [];
  /* 2026-11-08 用户裁定：授权 = 左侧菜单栏（含子菜单）的显示/隐藏。
     旧「平铺模块名单」已删除 —— 名字与 id 对不上真实侧栏，用户看不懂。
     现在直接渲染菜单树，数据源与 renderNav 同源（AGENTS/SYS_PAGES/SET_PAGES/NAV_*）。 */
  const tree = (typeof authMenuTreeHtml === 'function')
    ? authMenuTreeHtml(cur)
    : '<div class="hint">菜单树不可用：前端菜单分片未加载。</div>';
  openUserModal({
    title: '编辑授权：' + name,
    text: '<div class="hint" style="margin-bottom:4px">勾选 = 该用户左侧菜单<b>显示</b>此项；取消 = <b>不显示</b>。' +
          '勾组头 = 整组同开同关；组内只勾部分项时组头显示为半选。管理员默认可见全部，无需逐项勾选。</div>' +
          tree,
    confirmText: '保存修改',
    onSubmit: async (v, m) => {
      /* 叶子 + 整组（组头勾满时才存 group:xxx，避免「有授权但整组空白」） */
      const picked = [];
      m.querySelectorAll('[data-umod]:checked').forEach(n => picked.push(n.dataset.umod));
      m.querySelectorAll('[data-ugroup]:checked').forEach(n => picked.push(n.dataset.ugroup));
      await api('/api/users/' + encodeURIComponent(name) + '/perms',
        { method: 'PUT', body: JSON.stringify({ modules: picked }) });
      toast('已保存 ' + name + ' 的菜单授权', 'ok');
      usersPanelLoad();
    }
  });
  if (typeof syncAuthTreeGroups === 'function') syncAuthTreeGroups($(USERS_MODAL_ID));
}
/* 组头三态联动：组头 ⇒ 整组同开同关；叶子 ⇒ 回写组头 checked/indeterminate。
   绑在弹窗正文的 change 上（唯一监听点，禁 inline onclick，§4.2）。 */
function _usersTreeCascade(e) {
  const bd = e.currentTarget, t = e.target;
  if (!bd || !t || t.type !== 'checkbox') return;
  if (t.dataset.ugroup) {
    const sel = '[data-umod][data-ugrp="' + t.dataset.ugroup + '"]';
    bd.querySelectorAll(sel).forEach(n => { n.checked = t.checked; });
    t.indeterminate = false;
    return;
  }
  if (t.dataset.umod && t.dataset.ugrp) {
    const g = bd.querySelector('[data-ugroup="' + t.dataset.ugrp + '"]');
    if (!g) return;
    const kids = Array.from(bd.querySelectorAll(
      '[data-umod][data-ugrp="' + t.dataset.ugrp + '"]'));
    const on = kids.filter(n => n.checked).length;
    g.checked = kids.length > 0 && on === kids.length;
    g.indeterminate = on > 0 && on < kids.length;
  }
}
async function usersDel(name) {
  openUserModal({
    title: '删除用户：' + name,
    text: '<div class="hint" style="line-height:1.7">将永久删除「' + escapeHtml(name) +
      '」及其授权。<br>若仍有有效令牌，后端会拒绝删除 —— 请先「吊销令牌」。</div>',
    confirmText: '删除', danger: true,
    onSubmit: async () => {
      await api('/api/users/' + encodeURIComponent(name), { method: 'DELETE' });
      toast('已删除 ' + name, 'ok');
      usersPanelLoad();
    }
  });
}
async function usersPanelNew() {
  openUserModal({
    title: '新建用户',
    text: '<div class="hint" style="margin-bottom:8px">创建后在列表里点「编辑授权」，按左侧菜单树勾选可见项。</div>',
    fields: [
      { id: 'name', label: '用户名', required: true, placeholder: '小写字母 / 数字 / - / _',
        help: '创建后不可修改。' },
      { id: 'display', label: '显示名', placeholder: '可留空，默认同用户名' },
      { id: 'admin', label: '管理员', type: 'checkbox',
        help: '管理员可见全部菜单，无需逐项授权。' }
    ],
    confirmText: '创建',
    onSubmit: async (v, m) => {
      const ad = m.querySelector('#uf-admin');
      await api('/api/users', {
        method: 'POST',
        body: JSON.stringify({ username: v.name, display: v.display || v.name,
          is_admin: !!(ad && ad.checked), perms: [] })
      });
      toast('已创建 ' + v.name, 'ok');
      usersPanelLoad();
    }
  });
}

/* ── 模型调用计量面板（2026-11-07 上线，2026-11-08 改为行级明细 + 各列排序）──
   表格一行 = UTC 日 × agent(client) × model，列：Agent / 模型 / 日期 / 调用 /
   用量(tokens) / 均耗时 / 总耗时。排序纯前端内存排序，一次取数，不重复请求。
   数据源 = /api/monitor/model-calls/overview（CCR usage_events 只读聚合）。 */
let mclRows = [];
let mclSort = { key: 'total_tokens', dir: -1 };
const MCL_COLS = [
  { key: 'agent_name',   label: 'Agent',        type: 'str' },
  { key: 'model',        label: '模型',          type: 'str' },
  { key: 'date',         label: '日期 (UTC)',    type: 'str' },
  { key: 'requests',     label: '调用',          type: 'num' },
  { key: 'total_tokens', label: '用量 (tokens)', type: 'num' },
  { key: 'avg_ms',       label: '均耗时 (ms)',   type: 'num' },
  { key: 'total_ms',     label: '总耗时 (s)',    type: 'num' },
];

async function mclPanelLoad() {
  const box = $('mclPanel');
  if (!box) return;
  box.textContent = '加载中…';
  const win = ($('mclWindow') ? $('mclWindow').value : '7');
  try {
    const d = await api('/api/monitor/model-calls/overview?window_days=' + win);
    mclRender(d);
  } catch (e) {
    box.innerHTML = '<span class="hint">加载失败：' + escapeHtml(e.message) + '</span>';
  }
}
/* 稳定排序：数值列做减法、字符串列 localeCompare；相等时按 日期→模型→Agent
   做确定性 tiebreak，避免同分行每次点击顺序漂移。 */
function mclSorted(rows, sort) {
  const col = MCL_COLS.find(c => c.key === sort.key) || MCL_COLS[4];
  const dir = sort.dir || -1;
  return rows.slice().sort((a, b) => {
    let c;
    if (col.type === 'num') c = Number(a[col.key] || 0) - Number(b[col.key] || 0);
    else c = String(a[col.key] || '').localeCompare(String(b[col.key] || ''), 'zh');
    if (c === 0) c = String(a.date || '').localeCompare(String(b.date || '')) ||
                    String(a.model || '').localeCompare(String(b.model || '')) ||
                    String(a.agent_name || a.client || '').localeCompare(
                      String(b.agent_name || b.client || ''));
    return c * dir;
  });
}
function mclSortKey(key) {
  const col = MCL_COLS.find(c => c.key === key);
  if (!col) return;
  if (mclSort.key === key) mclSort = { key: key, dir: -mclSort.dir };
  else mclSort = { key: key, dir: col.type === 'num' ? -1 : 1 };
  mclRenderTable();
}
function mclNum(v) { return Number(v || 0).toLocaleString(); }
/* 耗时列固定 1 位小数 + 千分位：不能 `mclNum(x.toFixed(1))`——那会把字符串
   再 Number() 一次，27.0 又变回 27，列内小数位不齐。 */
function mclFixed1(v) {
  return Number(v || 0).toLocaleString(undefined,
    { minimumFractionDigits: 1, maximumFractionDigits: 1 });
}
function mclRenderTable() {
  const t = $('mclTable');
  if (!t) return;
  const rows = mclSorted(mclRows, mclSort);
  const th = MCL_COLS.map(c => {
    const on = c.key === mclSort.key;
    const arrow = on ? (mclSort.dir < 0 ? ' ▼' : ' ▲') : '';
    return '<th data-mcl-sort="' + c.key + '" style="text-align:' +
           (c.type === 'str' ? 'left' : 'right') +
           ';padding:5px 6px;cursor:pointer;white-space:nowrap;user-select:none" ' +
           'title="点击按此列排序">' + escapeHtml(c.label) + arrow + '</th>';
  }).join('');
  let body = '';
  rows.forEach(r => {
    const secs = Number(r.total_ms || 0) / 1000;
    /* Agent 列显示 **agents 清单里的名字**（后端按 profiles 映射）。
       未匹配的行显示「未归属」，原始凭据名进 title —— 旧后端（没有 agent_name）
       退回显示 client，不假装已归因。 */
    const known = typeof r.agent_name === 'string';
    const an = known ? r.agent_name : (r.client || '—');
    const raw = r.client_raw || r.client || '';
    const at = !known ? ('CCR 来源：' + raw)
      : (r.agent_id ? ('CCR 凭据：' + raw)
                    : ('该凭据未匹配 agents 清单（原始来源：' + raw + '）'));
    body += '<tr>' +
      '<td style="padding:4px 6px" title="' + escapeHtml(at) + '">' +
        escapeHtml(an) +
        (known && !r.agent_id && raw
          ? ' <span class="hint" style="opacity:.7">(' + escapeHtml(raw) + ')</span>' : '') +
        '</td>' +
      '<td style="padding:4px 6px;font-family:monospace">' + escapeHtml(r.model || '—') + '</td>' +
      '<td style="padding:4px 6px;white-space:nowrap">' + escapeHtml(r.date || '—') + '</td>' +
      '<td style="padding:4px 6px;text-align:right">' + mclNum(r.requests) + '</td>' +
      '<td style="padding:4px 6px;text-align:right">' + mclNum(r.total_tokens) + '</td>' +
      '<td style="padding:4px 6px;text-align:right">' + mclFixed1(r.avg_ms) + '</td>' +
      '<td style="padding:4px 6px;text-align:right">' + mclFixed1(secs) + '</td>' +
      '</tr>';
  });
  t.innerHTML = '<table class="tbl" style="width:100%;border-collapse:collapse">' +
    '<thead><tr style="border-bottom:1px solid var(--line,#ddd)">' + th + '</tr></thead>' +
    '<tbody>' + (body || '<tr><td colspan="' + MCL_COLS.length +
      '" class="hint" style="padding:8px">窗口内无明细行。</td></tr>') + '</tbody></table>';
}
function mclRender(d) {
  const box = $('mclPanel');
  if (!box) return;
  if (d.note) { box.innerHTML = '<span class="hint">' + escapeHtml(d.note) + '</span>'; return; }
  if (!d.total_calls) { box.innerHTML = '<span class="hint">窗口内暂无计量记录（' + (d.window_days) + ' 天）。</span>'; return; }
  const hasRows = Array.isArray(d.rows) && d.rows.length > 0;
  /* 旧后端（代码已落盘但未重启）没有 rows：用 by_model 退化成「无日期维度」的行。
     排序照样生效，日期/总耗时显 —，并在表上方点明原因，不假装数据完整。 */
  mclRows = hasRows ? d.rows : (d.by_model || []).map(m => ({
    client: '—', model: m.model, date: '—', requests: m.calls,
    total_tokens: m.total_tokens, avg_ms: m.avg_ms, total_ms: 0
  }));
  let h = '<div class="hint" style="margin-bottom:8px">共 <b>' + d.total_calls +
          '</b> 次调用 · <b>' + (d.total_tokens || 0).toLocaleString() + '</b> tokens（窗口 ' +
          d.window_days + ' 天，' + (d.as_of || '').slice(0, 16).replace('T', ' ') + ' UTC）· ' +
          mclRows.length + ' 行明细</div>';
  if (!hasRows) {
    h += '<div class="hint" style="margin-bottom:8px">当前后端未提供按天明细（需重启 agenthub 生效）；' +
         '下表暂按「模型」聚合，日期与总耗时不可用，但各列排序已生效。</div>';
  }
  h += '<div id="mclTable"></div>';
  h += '<div class="hint" style="margin-top:8px">点击表头按该列排序（再点一次反向）。' +
       '用量 = total_tokens；均耗时 = 该组 AVG(duration_ms)；总耗时 = SUM(duration_ms)。</div>';
  /* 归因口径必须写在页面上：Agent 列的名取自 agents 清单（profiles 注册表），
     而 CCR 账本只记**凭据名**。同一把 key 被多个 agent 共用时账本层面分不出，
     这些行会显示「未归属」并保留原始凭据名 —— 不能让读的人以为是某某个 agent。 */
  h += '<div class="hint" style="margin-top:4px">Agent 列取自 agents 清单；' +
       '「未归属」= CCR 凭据名无法对应到任何 agent（悬停行首列可看原始凭据名）。' +
       '每种 agent 用独立 CCR 凭据时才能逐 agent 精确归因。</div>';
  if (d.by_agent && Object.keys(d.by_agent).length) {
    h += '<div class="hint" style="margin-top:6px">Agent 调用分布：' +
         Object.entries(d.by_agent).map(([s, n]) => escapeHtml(s) + '=' + n).join(' · ') + '</div>';
  }
  if (d.by_scene && Object.keys(d.by_scene).length) {
    h += '<div class="hint" style="margin-top:6px">CCR 凭据分布（非 agent）：' +
         Object.entries(d.by_scene).map(([s, n]) => escapeHtml(s) + '=' + n).join(' · ') + '</div>';
  }
  box.innerHTML = h;
  mclRenderTable();
}

async function settingsLogsLoad() {
  const body = $('logBody');
  if (!body) return;
  boxBusy('logBody', '加载中…');
  // 2026-11-07：日志读端点的身份就是浏览器会话 Cookie（api() 同源自动带），
  // 不再让用户在这里二次输入口令 / 不再拼 x-hub-token。
  settingsLogsSyncSubject();
  try {
    const d = await api('/api/hublog?' + settingsLogsParams().toString());
    LOG_LAST = d;
    logPage = 0;
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
    /* Task11 日志分页：每页 100 行，滚动位置由浏览器保留（不另存） */
    const per = 100;
    const pages = Math.max(1, Math.ceil(es.length / per));
    if (logPage >= pages) logPage = pages - 1;
    const pageEs = es.slice(logPage * per, (logPage + 1) * per);
    body.innerHTML = (pages > 1 ? '<div class="toolbar" style="margin-bottom:6px"><button class="btn ghost sm" onclick="logPageGo(-1)"' + (logPage === 0 ? ' disabled' : '') + '>上一页</button><span class="hint">第 ' + (logPage + 1) + '/' + pages + ' 页（共 ' + es.length + ' 条）</span><button class="btn ghost sm" onclick="logPageGo(1)"' + (logPage >= pages - 1 ? ' disabled' : '') + '>下一页</button></div>' : '') +
      '<div class="set-diff">' + pageEs.map(e => {
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
  if (body) body.innerHTML = '';
  if (box) {
    box.innerHTML = '<div class="v" style="color:var(--danger)">读取失败：' + escapeHtml(msg) +
      (e && e.http ? '（HTTP ' + escapeHtml(String(e.http)) + '）' : '') + '</div>' +
      '<div class="hint">本页身份是登录会话 Cookie：若会话已过期，重新登录后刷新即可。' +
      'HTTP 401 = 未登录；503 = 用户库不可用（C 档后不再有口令可填）。</div>';
    box.style.display = '';
  }
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
     也免得把凭据塞进 URL（?token= 会进 uvicorn 访问行）—— C 档后走会话 Cookie，无需拼串。 */
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

/* 三个设置正文页的**唯一**委托出口：选 agent / 预览 / 保存 / GitHub / 日志 */
function settingsDelegates() {
  SET_PAGE_IDS.forEach(id => {
    const page = document.getElementById(id);
    if (!page) return;
    page.addEventListener('click', e => {
    /* 模型计量：表头排序走同一委托出口，禁 inline onclick */
    const mth = e.target.closest('[data-mcl-sort]');
    if (mth) return mclSortKey(mth.dataset.mclSort);
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
    if (act && act.dataset.settingsAct === 'undo') return settingsUndoModel();
    if (act && act.dataset.settingsAct === 'verify') return settingsVerifyModel(false);
    if (act && act.dataset.settingsAct === 'verify-live') return settingsVerifyModel(true);
    if (act) {
      const a = act.dataset.settingsAct;
      if (a === 'gh-test') return settingsGithubTest();
      if (a === 'gh-apply') return settingsGithubApply();
      if (a === 'gh-clear') return settingsGithubClear();
      if (a === 'gh-refresh') return settingsGithubRefresh();
      if (a === 'gh-src-add') return settingsGithubSourceAdd();
      if (a === 'gh-src-toggle') return settingsGithubSourceToggle(
        act.dataset.sid, (act.textContent || '').trim() === '启用');
      if (a === 'gh-src-del') return settingsGithubSourceDel(act.dataset.sid);
      /* 日志页（v0.13.46）：刷新 / 复制 / 导出 */
      if (a === 'log-refresh') return settingsLogsLoad();
      if (a === 'log-copy') return settingsLogsCopy();
      if (a === 'log-export') return settingsLogsExport();
      /* 用户授权页（2026-11-07） */
      if (a === 'users-load') return usersPanelLoad();
      if (a === 'users-new') return usersPanelNew();
      /* 模型计量页（2026-11-07，借鉴 LangChat） */
      if (a === 'mcl-load') return mclPanelLoad();
    }
    /* 用户授权页：卡片按钮统一走 data-uact 委托（禁 inline onclick，§4.2）。
       刻意放在既有分支之后：test_ghsettings.py 用固定 1400 字符窗口截本函数开头查 gh-，
       插在开头会把 gh-test 挤出窗口 ⇒ L0 假红。窗口缺陷属报请项，不顺手改测试。 */
    const ua = e.target.closest('[data-uact]');
    if (ua) {
      const n = ua.dataset.uname || '';
      switch (ua.dataset.uact) {
        case 'pw': return usersChangePw(n);
        case 'token': return usersIssue(n);
        case 'revoke': return usersRevoke(n);
        case 'perms': return usersPerms(n);
        case 'del': return usersDel(n);
      }
      return;
    }
    });
    /* 筛选下拉变了就重拉（关键字框要等敲完 ⇒ 走「刷新」按钮，不逐字符打后端）。
       change 也挂在同一个委托出口里，不另起监听点。 */
    page.addEventListener('change', e => {
      const id = e.target && e.target.id;
      if (id === 'logSource' || id === 'logLevel' || id === 'logWindow' ||
          id === 'logSubject') return settingsLogsLoad();
    });
    /* Task11 日志关键字防抖：挂在同一委托出口（input 事件），不写 inline */
    page.addEventListener('input', e => {
      if (e.target && e.target.id === 'logQ') logQDebounced();
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
