"""embed_proxy.py — 第三方 Web UI「外框统一」注入代理（用户 09-20 选定方案 b）。

为什么要有这个代理
------------------
QwenPaw 自带 56px 顶栏，而 hub 的嵌入外框已按 pi-web 实测统一成「行1 菜单 36px + 行2 地址 29px」。
跨源（:3102 载 :8088 的 iframe）使父页任何 CSS/JS 注入都被同源策略挡死，官方也没有
hideHeader / minimal / custom_css 之类的钩子（grep 为 0 命中），唯一可用的官方入口
QWENPAW_CONSOLE_STATIC_DIR 要改包内静态目录并重启 qwenpaw —— qwenpaw 是本机 Agent 会话宿主，
不能为个样式去重启它。于是把注入点挪到 hub 自己这一侧：在 hub 进程内起一个本地反代，
HTML 出栈前插一段 <style>，其余流量原样隧道透传。

为什么是「独立端口」而不是 hub 的路径前缀（/embed/qwenpaw/*）
-----------------------------------------------------------
qwenpaw 的前端资源与接口都是根绝对路径（/assets/…、/api/…）。挂路径前缀就得改写 HTML 里的
URL，还得改写在 JS bundle 字符串里拼出来的 /api 与 WebSocket 路径——那是不稳定的；而 hub 自己
已经占了 /api/*，同源命名空间直接打架。放在独立端口上，宿主应用仍然"住在自己的根"，
零改写、零冲突，WS/SSE 也只是普通字节流转发。

工程约束（改动前请复核）
------------------------
1. 只代理，不解释业务：非 HTML 请求（静态资源、JSON、SSE、WebSocket 升级）走字节隧道，
   一个字段都不动。
2. 注入选择器一律用 antd 布局类 + `[class*=...]`，不写 `__hash` 字面量：
   qwenpaw 升到 CSS Modules hash 变了也不会误伤，最坏是"注入不生效、顶栏仍 56px"（降级而非破坏）。
3. 高度是联动的：qwenpaw 的 sider 写死 `height:calc(100vh - 64px)`（窄屏 56px），
   只压顶栏不改 sider 会在侧栏底部留下 28px 死带 —— 两处必须成对改。
4. 上游要 `Accept-Encoding: identity`，否则拿到 br/gz 就没法在字节层面插 style。
   上游无视 identity 仍回压缩体时：**放弃注入、原样透传**（宁可外框不统一，也不破坏页面）。
"""
from __future__ import annotations

import asyncio
import base64
import hashlib
import hmac
import logging
import os
import re
import time
import urllib.parse

log = logging.getLogger("agent-hub.embed-proxy")

# 实测基线（09-20，CDP 在 1440/390 两档量的 QwenPaw 2.2.0 console）：
#   header: h=56 pad=0 28px(窄 0 12px) bg=rgb(249,248,244)  子元素 logoWrapper h=64(本就溢出)、space h=32
#   sider : calc(100vh - 64px)，窄屏 calc(100vh - 56px)
# 目标：与 hub .ebar-menu 同高（桌面 36 / ≤768px 35）。
INJECT_CSS = """<style id="agent-hub-embed-unify">
/* 由 agent-hub :3103 注入：把宿主顶栏压到与 hub 外框行1 同高。见 src/embed_proxy.py 顶部说明。 */
.qwenpaw-layout-header[class*="__header__"], .qwenpaw-layout-header {
  height: 36px !important; min-height: 36px !important; padding: 0 10px !important; }
.qwenpaw-layout-header [class*="logoWrapper"], .qwenpaw-layout-header [class*="logoWrapper"] img,
.qwenpaw-layout-header img { max-height: 24px !important; width: auto !important; }
.qwenpaw-layout-header [class*="space"], .qwenpaw-layout-header [class*="headerActions"] { gap: 4px !important; }
/* sider 与顶栏高度联动：不改这条就会在侧栏底部留下死带 */
.qwenpaw-layout-sider, [class*="__sider__"] { height: calc(100vh - 36px) !important; }
@media (max-width: 768px) {
  /* 选得齐平：基础规则里 `.qwenpaw-layout-header[class*="__header__"]` 是 (0,2,0)，
     窄屏要覆盖它必须用同特异度，否则会被上面的 36px 顶回来（实测就是这样好一像素）。 */
  .qwenpaw-layout-header[class*="__header__"], .qwenpaw-layout-header {
    height: 35px !important; min-height: 35px !important; padding: 0 8px !important; }
  .qwenpaw-layout-sider, [class*="__sider__"] { height: calc(100vh - 35px) !important; }
}
</style>"""

# 跨源嵌页的 ⌘/Ctrl+K 桥（PT-20261101-09）
# ------------------------------------------------
# 为什么必须注入到嵌页里：iframe 是**跨源**（各占一个端口），帧内的 keydown 不会冒泡到
# 父页，父页写多少监听都收不到；用户实测「鼠标点过页面最顶部（父页外框）之后才能唤起」
# 正是这个原因。唯一出路是嵌页自己接键、postMessage 回父页。
# 逐家改 12 个宿主不现实（其中 6 个是第三方二进制），所以复用本代理的注入点：HTML 出栈前
# 插这段 script，宿主仍然"住在自己的根"，零 URL 改写。
# 父页收端见 static/hub/05-chat-and-history.js 的 message 监听（校验 e.source）。
KEYS_JS = """<script id="agent-hub-keys">
(function () {
  if (window.parent === window) return;            // 独立打开时不抢浏览器自己的 ⌘K
  document.addEventListener('keydown', function (e) {
    if (!((e.metaKey || e.ctrlKey) && !e.altKey && (e.key === 'k' || e.key === 'K'))) return;
    if (e.isComposing || e.keyCode === 229) return; // 中文输入法组字中不抢
    e.preventDefault();
    e.stopPropagation();
    try { window.parent.postMessage({ hub: 'cmd-k' }, '*'); } catch (_) {}
  }, true);
})();
</script>"""

VIEWPORT_CSS = """<style id="agent-hub-vh">
/* 老引擎没有 dvh 时的整页高度兜底（2026-11-21，pi web 会话划不动）。
 *
 * 病根：pi-web 的 CSS 只在 @media (display-mode:standalone) 里定义
 * --app-viewport-height:100vh，其余场合一律 var(--app-viewport-height, 100dvh)。
 * 桌面 Chrome 有 dvh ⇒ 正常；Android WebView < Chrome 108 没有 dvh ⇒ 该声明在计算值
 * 阶段失效、height 退回 auto ⇒ 聊天容器高度 = 内容高度（实测 10847px），
 * 永远没有溢出 ⇒ 划不动；body 又是 overflow:hidden ⇒ 输入框被推到屏幕外。
 * 实测（无头 Chromium 抹掉 dvh 后的同一页面）：聊天滚动区 clientHeight=scrollHeight=10847。
 *
 * 只在不支持 dvh 的引擎里生效；新浏览器 @supports not (...) 为假，样式零作用。
 */
@supports not (height: 100dvh) {
  :root { --app-viewport-height: 100vh; }
  html, body { height: 100vh; }
}
</style>"""
COMPAT_JS = """<script id="agent-hub-compat">
/* 老浏览器垫片（2026-11-20 用户报障：pi 对话框「模型错误 e?.throwIfAborted is not a function」）。
 *
 * 病根：pi-web 0.17 的模型加载回调里写 `e?.throwIfAborted()`（上游 page chunk 原文），
 * 而 AbortSignal.throwIfAborted 是 Chrome 100 / Safari 15.4 才有的方法。用户的浏览器
 * （或内嵌 WebView）没有它 ⇒ 该回调抛 TypeError，被 catch 当成模型加载失败，界面上就是
 * 「模型错误」+ 空模型下拉。同一条推理下会缺的还有 AbortSignal.timeout（Chrome 103）、
 * structuredClone（Chrome 98）、crypto.randomUUID（**要求安全上下文**，所以经 http 局域网
 * 访问时连新版 Chrome 都没有 —— 这条与浏览器新旧无关，是这个桥的本职）。
 *
 * 为什么垫在桥里：嵌页只能经桥进入（各上游自身只绑回环），桥已经在每个 HTML 文档
 * `</head>` 前插 ⌘K 脚本，复用同一注入点。只补缺失的原生实现，绝不覆盖已有的。
 * 必须是 ES5 语法：垫片本身要先被老浏览器解析，写成 ES6 就白搭。
 */
(function () {
  var sig = typeof AbortSignal !== 'undefined' ? AbortSignal.prototype : null;
  if (sig && !sig.throwIfAborted) {
    sig.throwIfAborted = function () {
      if (this.aborted) {
        throw this.reason !== undefined ? this.reason
          : new DOMException('This operation was aborted', 'AbortError');
      }
    };
  }
  if (typeof AbortSignal !== 'undefined' && !AbortSignal.timeout) {
    AbortSignal.timeout = function (ms) {
      var c = new AbortController();
      setTimeout(function () {
        c.abort(new DOMException('signal timed out', 'TimeoutError'));
      }, ms);
      return c.signal;
    };
  }
  if (typeof crypto !== 'undefined' && crypto && crypto.getRandomValues && !crypto.randomUUID) {
    crypto.randomUUID = function () {
      var b = crypto.getRandomValues(new Uint8Array(16)), out = '', hex = '0123456789abcdef';
      b[6] = (b[6] & 0x0f) | 0x40;          /* version 4 */
      b[8] = (b[8] & 0x3f) | 0x80;          /* RFC 4122 variant */
      for (var i = 0; i < 16; i++) {
        if (i === 4 || i === 6 || i === 8 || i === 10) out += '-';
        out += hex[b[i] >> 4] + hex[b[i] & 15];
      }
      return out;
    };
  }
  if (typeof structuredClone === 'undefined') {
    window.structuredClone = function (value, seen) {
      if (value === null || typeof value !== 'object') {
        if (typeof value === 'function' || typeof value === 'symbol') {
          throw new DOMException('could not be cloned', 'DataCloneError');
        }
        return value;
      }
      seen = seen || new Map();
      if (seen.has(value)) return seen.get(value);
      var tag = Object.prototype.toString.call(value), out, i, k;
      if (value instanceof Date) return new Date(value.getTime());
      if (value instanceof RegExp) return new RegExp(value.source, value.flags);
      if (value instanceof Map) {
        out = new Map(); seen.set(value, out);
        value.forEach(function (v, kk) { out.set(window.structuredClone(kk, seen), window.structuredClone(v, seen)); });
        return out;
      }
      if (value instanceof Set) {
        out = new Set(); seen.set(value, out);
        value.forEach(function (v) { out.add(window.structuredClone(v, seen)); });
        return out;
      }
      if (value instanceof ArrayBuffer) return value.slice(0);
      if (ArrayBuffer.isView(value)) return new value.constructor(value);
      if (tag === '[object Array]') {
        out = []; seen.set(value, out);
        for (i = 0; i < value.length; i++) out[i] = window.structuredClone(value[i], seen);
        return out;
      }
      out = {}; seen.set(value, out);
      for (k in value) {
        if (Object.prototype.hasOwnProperty.call(value, k)) {
          out[k] = window.structuredClone(value[k], seen);
        }
      }
      return out;
    };
  }

  /* ── 2026-11-21 补第二批：Chrome<83 缺的数组/字符串/对象方法 ──────────────
   * 判据（本地复现，非推测）：在无头 Chromium 里删掉这些方法模拟老 WebView，
   * 打开旧会话直接 `n.findLastIndex is not a function` ⇒ 会话视图整块不渲染
   * （正文 70 字符、无滚动容器）＝用户看到的「模型错误 + 划不动」；把垫片加回去
   * 同一页面正常出正文并恢复滚动（clientHeight 673 / scrollHeight 6049）。
   * findLastIndex/.at 都在 pi-web 的**消息渲染**与**模型加载回调**路径上，
   * 所以新建会话（不渲染历史）正常、点旧会话才炸。
   * 全部只在缺失时定义；defineProperty 保证不可枚举，不会污染 for..in。
   */
  function def(obj, name, fn) {
    if (!obj || obj[name]) return;
    try {
      Object.defineProperty(obj, name, {
        value: fn, writable: true, configurable: true, enumerable: false
      });
    } catch (e) { try { obj[name] = fn; } catch (e2) {} }
  }

  function relIdx(len, n) {
    n = Math.trunc ? Math.trunc(n) : (n < 0 ? Math.ceil(n) : Math.floor(n));
    if (n < 0) n += len;
    return n;
  }

  def(Array.prototype, 'at', function (n) {
    var i = relIdx(this.length, n);
    return (i < 0 || i >= this.length) ? undefined : this[i];
  });
  def(String.prototype, 'at', function (n) {
    var s = String(this);
    var i = relIdx(s.length, n);
    return (i < 0 || i >= s.length) ? undefined : s.charAt(i);
  });
  def(Array.prototype, 'findLast', function (fn, thisArg) {
    for (var i = this.length - 1; i >= 0; i--) {
      if (fn.call(thisArg, this[i], i, this)) return this[i];
    }
    return undefined;
  });
  def(Array.prototype, 'findLastIndex', function (fn, thisArg) {
    for (var i = this.length - 1; i >= 0; i--) {
      if (fn.call(thisArg, this[i], i, this)) return i;
    }
    return -1;
  });
  def(Array.prototype, 'toSorted', function (cmp) {
    return Array.prototype.slice.call(this).sort(cmp);
  });
  def(Array.prototype, 'toReversed', function () {
    return Array.prototype.slice.call(this).reverse();
  });
  def(Array.prototype, 'with', function (i, v) {
    var out = Array.prototype.slice.call(this);
    var k = relIdx(out.length, i);
    if (k < 0 || k >= out.length) throw new RangeError('Invalid index');
    out[k] = v;
    return out;
  });
  def(String.prototype, 'replaceAll', function (from, to) {
    if (from instanceof RegExp) return this.replace(from, to);
    return this.split(String(from)).join(String(to));
  });
  def(Object, 'hasOwn', function (obj, key) {
    return obj !== null && obj !== undefined &&
      Object.prototype.hasOwnProperty.call(obj, key);
  });
  def(Object, 'groupBy', function (items, cb) {
    var out = Object.create(null), i = 0;
    for (var v of items) {
      var k = cb(v, i++);
      if (!Object.prototype.hasOwnProperty.call(out, k)) out[k] = [];
      out[k].push(v);
    }
    return out;
  });
  def(Map, 'groupBy', function (items, cb) {
    var m = new Map(), i = 0;
    for (var v of items) {
      var k = cb(v, i++);
      if (!m.has(k)) m.set(k, []);
      m.get(k).push(v);
    }
    return m;
  });
  if (typeof Promise !== 'undefined') {
    def(Promise, 'any', function (list) {
      return new Promise(function (resolve, reject) {
        var arr = Array.prototype.slice.call(list), n = arr.length;
        var errs = new Array(n), left = n;
        if (!n) return reject(new Error('All promises were rejected'));
        arr.forEach(function (p, i) {
          Promise.resolve(p).then(resolve, function (err) {
            errs[i] = err;
            if (--left === 0) {
              var e = new Error('All promises were rejected');
              e.errors = errs;
              reject(e);
            }
          });
        });
      });
    });
  }
  if (typeof AbortSignal !== 'undefined') {
    def(AbortSignal, 'any', function (signals) {
      var ctl = new AbortController();
      Array.prototype.slice.call(signals).forEach(function (sig) {
        if (ctl.signal.aborted) return;
        if (sig.aborted) { ctl.abort(sig.reason); return; }
        sig.addEventListener('abort', function () { ctl.abort(sig.reason); }, { once: true });
      });
      return ctl.signal;
    });
  }

  /* ResizeObserver loop 垫片（2026-11-21 用户报障：pi-web 会话列表划不动 + 模型错误）。
   * 病根：pi-web 会话树 useLayoutEffect 里 `new ResizeObserver(function(){ setProperty(...);
   * k(clientHeight); setScrollTop() })` —— 回调内改样式/读尺寸又写样式，老引擎（Android 11
   * WebView / Chrome 90）在同一帧同步递归到浏览器上限，抛 `ResizeObserver loop limit
   * exceeded` 并**中断回调**，导致会话树高度没算完 ⇒ 无法滑动；同类回调在模型下拉路径
   * 也有 ⇒ 模型错误。新浏览器把 loop 当良性警告并继续，所以浏览器正常。
   * 修法：包装原生 RO，把回调延迟到下一帧（rAF）并同帧合并，打断同步递归。
   * 保持 instanceof/原型链：ShimRO.prototype 指回原生 prototype。
   *
   * ⚠️ 2026-11-21 20:09 改判：旧实现用 `_oldEngine = !CSS.supports('height','100dvh')`
   * 当门槛，实测垫片根本没装上却仍在报 loop —— hub 日志证明注入已送达
   * （20:07:41 注入 11417B），3 秒后（20:07:54）继续 `ResizeObserver loop limit
   * exceeded`。反推：APP 的 WebView 认识 dvh（Android System WebView 会自更新，
   * 早已不是 Chrome 90）⇒ 门槛 false ⇒ 跳过包装。该 loop-limit 报错各版本浏览器
   * 都会报，不是「老引擎专属」，门槛因此毫无道理 —— 改为**无条件**包装。
   * 诊断标记：window.__hubRoShim（只读布尔，便于端侧确认是否装上）。
   */
  if (typeof ResizeObserver !== 'undefined' && !window.__roShimmed) {
    var _NativeRO = ResizeObserver;
    var ShimRO = function (cb) {
      var raf = 0;                             /* 每个实例独立节流 */
      var last = null;                         /* 本实例最近一次回调，供同帧合并 */
      var seen = 0;                            /* 已投递次数 */
      return new _NativeRO(function (entries, obs) {
        /* 2026-11-28 第三次复发后的改判：**首帧必须同步**。
         * 原垫片（11-21）为了修老 WebView 的同步 loop，把**每一次**回调都延到
         * requestAnimationFrame 里。pi-web 0.19 的会话树在 useLayoutEffect 里
         * 先 setProperty 改样式、紧接着读 clientHeight 算高度 —— 它依赖的是
         * 「布局已稳定」那一刻的读数。原生 RO 的首批通知发生在同一帧内布局之后，
         * 而 rAF 版把它推到**下一帧**：首屏算出的高度是 0 ⇒ 树高 0 ⇒ 白屏；
         * 刷新走增量路径（已有高度）才恢复。这正是「第一次打不开、刷新就好」的形态。
         * 故：第 1 次同步投递（与原生语义一致），后续仍走 rAF 合并
         * （老 WebView 的 loop 修法只在连续 resize 时才需要，首帧不该付这个代价）。
         * 首帧异常用异步重抛：既不让 React 的 commit 链路在这里断掉（白屏），
         * 也保证控制台能看到真错（不吞）。 */
        if (seen++ === 0) {
          try { cb(entries, obs); }
          catch (e) { setTimeout(function () { throw e; }, 0); }
          return;
        }
        last = [entries, obs];
        if (raf) return;                       /* 只合并本实例同帧的多次触发 */
        raf = requestAnimationFrame(function () {
          raf = 0;
          var cur = last; last = null;
          if (!cur) return;
          cb(cur[0], cur[1]);
        });
      });
    };
    ShimRO.prototype = _NativeRO.prototype;    /* 保 instanceof */
    window.ResizeObserver = ShimRO;
    window.__roShimmed = true;
    window.__hubRoShim = 1;                    /* 端侧可查：垫片已装 */
  }
})();
</script>"""

_END = b"\r\n\r\n"
_HDR_SPLIT = re.compile(rb"\r?\n")
_HTML_PATH = re.compile(rb"\.html?$", re.I)
_ENC = re.compile(rb"^content-encoding:\s*(?P<v>[^\r\n]+)\r?$", re.I | re.M)
# 响应侧 Content-Type：只有 HTML 文档才允许注入外框脚本。
# 2026-11-13 判例：filebrowser 打包下载 /api/raw/?files=...&algo=zip 是 window.open
# 顶层导航，Accept 含 text/html 且路径末段无扩展名 ⇒ 被 _is_document 误判成 SPA 文档，
# 42MB 的 application/zip 被读回内存并往里插 528 字节 <style>/<script> ⇒ 解压 CRC 全错、
# 文件不全。判据用上游真实 Content-Type，不靠猜 URL，覆盖所有非 HTML 响应（下载/PDF/图片/API）。
_CT = re.compile(rb"^content-type:\s*(?P<v>[^\r\n]+)\r?$", re.I | re.M)
_STATUS = re.compile(rb"^HTTP/\d(?:\.\d)? (?P<code>\d{3})")

# ── Basic 鉴权面板的「桥内登录」（2026-11-02，PT-20261102 报障）──────────
# 问题：proxy-panel(:8083) 回 `401 + WWW-Authenticate: Basic`，而浏览器**不在跨源 iframe
# 里弹**认证框（只有顶层导航才弹）⇒ 用户点「嵌入会话」只看到上游那句 `auth required`，
# 而浏览器直开 :8083 正常。这就是「3208 面板没有登录框、直接报错」的全部成因。
# 两种错解：① 删掉嵌入入口（用户侧=功能没了）；② 桥里直接补 Authorization（等于把
# 带鉴权的面板经桥变成 LAN 上免密可读，凭空扩攻击面）。所以桥自己做一层**等价的门**：
# 未登录先给本页表单 → POST 校验 → 种 HttpOnly cookie → 此后每个上游请求由服务端补
# Authorization。口令仍只有一份（画像里的 `bridge_secret` 指向的面板口令文件）。
LOGIN_HTML = """<!doctype html><html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1"><title>__TITLE__</title>
<style>
html,body{height:100%;margin:0;font:14px/1.6 system-ui,-apple-system,"Segoe UI",sans-serif;
background:#12141a;color:#e6e8ee;display:flex;align-items:center;justify-content:center}
form{background:#1b1e26;border:1px solid #2c313d;border-radius:10px;padding:22px 24px;
width:min(320px,86vw);box-shadow:0 8px 28px rgba(0,0,0,.45)}
h1{font-size:15px;margin:0 0 14px;font-weight:600}
input{width:100%;box-sizing:border-box;padding:9px 10px;border-radius:7px;border:1px solid #333a48;
background:#0e1015;color:inherit;font-size:14px}
button{margin-top:12px;width:100%;padding:9px;border:0;border-radius:7px;background:#3b6df6;
color:#fff;font-size:14px;cursor:pointer}
button:hover{background:#4b7bff}
.e{color:#ff6b6b;margin:0 0 10px;font-size:13px}
.h{color:#8a91a3;font-size:12px;margin:10px 0 0}
</style></head><body><form method="post" action="/__hub_login">
<h1>__TITLE__</h1>__ERR__
<input type="password" name="p" placeholder="口令" autofocus autocomplete="current-password">
<button type="submit">登录</button>
<p class="h">口令取自该面板的本地口令文件；登录状态只记在本桥端口上。</p>
</form></body></html>"""


def _login_page(title: str, err: str = "") -> bytes:
    import html as _html
    msg = '<p class="e">%s</p>' % _html.escape(err) if err else ""
    return (LOGIN_HTML.replace("__TITLE__", _html.escape(title))
            .replace("__ERR__", msg)).encode()


def _forbidden_page(title: str, module: str) -> bytes:
    """已登录但未被授权该模块：明确 403，不回落上游、不给半截页面。"""
    import html as _html
    t, m = _html.escape(title), _html.escape(module)
    return ("<!doctype html><html lang=\"zh-CN\"><head><meta charset=\"utf-8\">"
            "<meta name=\"viewport\" content=\"width=device-width,initial-scale=1\">"
            "<title>403 · 没有访问权限</title></head>"
            "<body style=\"margin:0;padding:40px 24px;font:14px/1.7 system-ui,sans-serif;"
            "background:#14161a;color:#e6e6e6\">"
            "<h1 style=\"font-size:18px;margin:0 0 12px\">没有访问权限</h1>"
            f"<p style=\"margin:0 0 8px\">当前账号未被授权使用 <b>{t}</b>"
            f"（模块 <code style=\"background:#24262c;padding:1px 5px;border-radius:4px\">{m}</code>）。</p>"
            "<p style=\"margin:0;color:#9aa0a6\">请让管理员在 agenthub「设置 → 用户授权」里勾选该项后重试。</p>"
            "</body></html>").encode()


def _req_line(header: bytes) -> tuple[bytes, bytes]:
    """(method, path) —— 只取请求行，路径去掉 query。"""
    parts = header.split(b"\r\n", 1)[0].split(b" ")
    if len(parts) < 2:
        return b"", b"/"
    return parts[0].upper(), parts[1].split(b"?", 1)[0]


def _cookie_has(header: bytes, name: str, token: str) -> bool:
    for ln in header.split(b"\r\n"):
        if not ln.lower().startswith(b"cookie:"):
            continue
        for part in ln[7:].split(b";"):
            k, _, v = part.strip().partition(b"=")
            if k.decode(errors="replace") == name and hmac.compare_digest(
                    v.decode(errors="replace"), token):
                return True
    return False



def _hub_session_ok(header: bytes):
    """请求是否带着**有效的 hub 登录 Cookie**（`hub_session`）。

    返回 `(ok, perms, who)`：
      - `(False, None, "")` 未登录 / 用户库不可用（fail-closed）；
      - `(True, None, "gztxt")` 管理员（perms=None ⇒ 全放行）；
      - `(True, set, "name")` 普通用户，set 是其在 agenthub「用户授权」里勾选的模块。

    `who` 只用于审计日志（2026-11-11「加桥审计日志」），**绝不回显 token**。

    为什么桥不自己再要一次口令（2026-11-07 用户裁定「在 agenthub 打开无需再登录，
    浏览器直接打开才要验证」）：Cookie 是 host-only、**不按端口隔离**，所以 hub
    (:3102) 登录后种下的 `hub_session` 会随 iframe 请求自动带到各桥端口 —— 桥只需
    校验它，就能把「hub 登录态」当唯一口令。浏览器直接打开桥端口时没有这个
    Cookie（没在 hub 登录过），于是会被送回 hub 登录页。
    校验复用 users.resolve_token（同一张 user_credentials 表，sha256 比对），
    桥自己不签发、不缓存、不落任何凭据。

    ⚠ 2026-11-08：此前 `_perms` 被直接丢弃 ⇒ 任何登录用户都能用全部 agent
    （gztxt1 未授权 pi 仍能打开 :3201）。现在 perms 原样交给 `_serve` 做
    agent 级判定，别再把它丢掉。
    """
    for ln in header.split(b"\r\n"):
        if not ln.lower().startswith(b"cookie:"):
            continue
        for part in ln[7:].split(b";"):
            k, _, v = part.strip().partition(b"=")
            if k.decode(errors="replace") != "hub_session":
                continue
            tok = v.decode(errors="replace")
            if not tok:
                return False, None, ""
            try:
                import users as _users
                row, perms, compat = _users.resolve_token(tok)
                if not (row and not compat):
                    return False, None, ""
                return True, perms, str(row.get("username") or "")
            except Exception:  # noqa: BLE001 —— 用户库不可用一律当未登录（fail-closed）
                return False, None, ""
    return False, None, ""


def _is_iframe_doc(header: bytes) -> bool:
    """请求是否来自 **iframe 内导航**（`sec-fetch-dest: iframe`）。

    只用于「未登录时怎么回」这一个分支：顶层导航照旧 302，iframe 里换成
    顶层跳转页（同一个登录页塞进框里＝用户看到的"打不开"）。
    """
    for ln in header.split(b"\r\n"):
        if ln.lower().startswith(b"sec-fetch-dest:"):
            return ln.split(b":", 1)[1].strip().lower() == b"iframe"
    return False


def _iframe_login_page(loc: str) -> bytes:
    """iframe 内的未登录页：把**顶层**导航去 hub 登录，登录后 next 回到本嵌入页。"""
    import html as _html
    href = _html.escape(loc, quote=True)
    return ("""<!doctype html><meta charset="utf-8"><title>需要登录 AgentHub</title>
<style>body{font:14px/1.6 system-ui,sans-serif;padding:18px;color:#333}
a{color:#2c5f8a}</style>
<p>登录状态已失效，正在跳转到 AgentHub 登录页…</p>
<p>没有自动跳转？<a href="%s" target="_top">点此登录</a></p>
<script>(function(){var u=%r;try{top.location.replace(u)}
catch(e){try{window.parent.location.replace(u)}catch(e2){location.replace(u)}}})();</script>
""" % (href, loc)).encode("utf-8")


def _peer_of(writer) -> str:
    """客户端地址（审计用）。取不到就写 "?"，绝不让日志本身抛异常。"""
    try:
        p = writer.get_extra_info("peername")
        return str(p[0]) if p else "?"
    except Exception:  # noqa: BLE001
        return "?"


def _host_of(header: bytes) -> str:
    """请求头里的 Host（用于把未登录访客送回**同一台机器的** hub 登录页）。"""
    for ln in _HDR_SPLIT.split(header):
        if ln.lower().startswith(b"host:"):
            v = ln[5:].strip().decode(errors="replace")
            return v.split(":", 1)[0].strip("[]")
    return ""


def _is_upgrade(header: bytes) -> bool:
    return b"upgrade:" in header.lower() or b"upgrade" in header.lower().split(b"\r\n", 1)[0]


def _is_document(head_line: bytes, header: bytes) -> bool:
    """SPA 文档请求（顶层导航）才注入。判据：GET + 无扩展名或 .html + Accept 里有 text/html。"""
    if not head_line.upper().startswith(b"GET "):
        return False
    if _is_upgrade(header):
        return False
    if b"text/html" not in header.lower():
        return False
    path = head_line.split(b" ")[1] if len(head_line.split(b" ")) > 1 else b"/"
    path = path.split(b"?")[0]
    if _HTML_PATH.search(path):
        return True                      # 明确要 .html
    last = path.rsplit(b"/", 1)[-1]
    return b"." not in last              # 无扩展名 = SPA 路由，服务端回的是 index.html → 要注入


def _dechunk(body: bytes) -> bytes | None:
    """解 `Transfer-Encoding: chunked`。解析失败返回 None（调用方原样透传，不猜）。

    为什么必须有（2026-11-01 实测）：dsh 的 web 服务（Node）**所有响应都是 chunked**，
    而 _document 会把响应头的 transfer-encoding 剥掉、按 `len(rb)` 重算 Content-Length。
    不解码时 rb 仍是 `929b\\r\\n<html>…\\r\\n0\\r\\n\\r\\n`，浏览器把十六进制块长当正文首行、
    把终止块 `0` 当正文末行 ⇒ 页面左上角显示 `929b`、左下角显示 `0`（用户报障原话）。
    直连 :3080 正常、只有 hub 嵌页异常，正因为只有这条注入路径改了响应头。
    """
    out = []
    i, n = 0, len(body)
    while i < n:
        j = body.find(b"\r\n", i)
        if j < 0:
            return None
        line = body[i:j].split(b";", 1)[0].strip()
        try:
            size = int(line, 16)
        except ValueError:
            return None
        if size == 0:                      # 终止块；后面是 trailer，正文到此为止
            return b"".join(out)
        start = j + 2
        if start + size > n:
            return None
        out.append(body[start:start + size])
        i = start + size + 2               # 跳过块尾 CRLF
    return None


def _inject(body: bytes, extra: bytes) -> bytes:
    """把 extra（<style>/<script>）插到 </head> 之前；没有 <head> 就退回 </body> 之前。"""
    if not extra:
        return body
    low = body.lower()
    i = low.rfind(b"</head>")
    if i < 0:
        i = low.rfind(b"</body>")
    if i < 0:
        return body
    return body[:i] + extra + body[i:]


# ── Chromium <99 的 CSS 兼容：拆掉 @layer 外壳（2026-11-21）──────────────
# 病根（实测用户手机 WebView）：pi-web 0.17/0.18 用 Tailwind v4 出 CSS，**全部布局
# 工具类**（.flex / .h-full / .min-h-0 / .flex-1 / .overflow-y-auto…）都写在
# `@layer utilities{…}` 里。CSS 规范要求「不认识的 at-rule 连整块一起丢弃」，
# 而 @layer 是 Chrome 99 才有 ⇒ 老 WebView 把 utilities 层整段扔掉：
#   聊天记录容器没有 overflow、没有 flex-1/min-h-0，高度由内容撑开；
#   外层 body 又是 overflow:hidden ⇒ **会话内容永远划不动**（用户报障原文），
#   而同一份页面在桌面 Chrome 里完全正常。
# 版本证据（2026-11-20 18:20 本机日志）：页面报 `structuredClone is not defined`
# （Chrome 98 才有）⇒ WebView < 98；2026-11-21 日志里 `interactive-widget` 不被识别
# （Chrome 108 才有）⇒ < 108。两条独立证据都指向没有 @layer 的老引擎。
#
# 修法：只对「确实不支持 @layer」的 Chromium 生效——
#   1) `_bust_css_urls` 给文档里的 /_next/static/css/*.css 加 `?hubcss=1`：
#      Next 对这些资源发的是 `max-age=1y, immutable`，不换 URL 的话浏览器根本
#      不会回来请求，补丁永远到不了（缓存里那份仍是坏的）；
#   2) `_stylesheet` 对带该标记的 CSS 去掉 @layer 外壳（层序 = 源码顺序，
#      与作者写的顺序一致：properties → theme → base → utilities → 自定义 CSS）。
# 新浏览器请求里没有 hubcss=1 ⇒ 一个字节都不经过这里，零回归。
_LAYER_TOKEN = re.compile(rb"@layer\b", re.I)
_CSS_HREF = re.compile(rb'(?i)(href=")(/_next/static/css/[^"?]+\.css)(")')
_CHROME_UA = re.compile(rb"Chrome/(\d+)")


def _needs_layer_flatten(header: bytes) -> bool:
    """请求来自 Chromium <99（没有 @layer）⇒ 需要兼容改写。认不出引擎就不动。"""
    m = _CHROME_UA.search(header)
    if not m:
        return False
    try:
        return int(m.group(1)) < 99
    except ValueError:
        return False


def _bust_css_urls(body: bytes) -> bytes:
    """给 <link href="/_next/static/css/…"> 加 ?hubcss=1（只改这一种路径）。"""
    return _CSS_HREF.sub(rb"\1\2?hubcss=1\3", body)


def _css_skip_string(b: bytes, i: int, n: int) -> int:
    q = b[i:i + 1]
    i += 1
    while i < n:
        ch = b[i:i + 1]
        if ch == b"\\":
            i += 2
            continue
        if ch == q:
            return i + 1
        i += 1
    return n


def _css_skip_comment(b: bytes, i: int, n: int) -> int:
    j = b.find(b"*/", i + 2)
    return n if j < 0 else j + 2


def _css_match_brace(b: bytes, i: int, n: int) -> int:
    """b[i] == '{'，返回配对 '}' 的下标（跳过字符串与注释里的花括号）。"""
    depth = 0
    while i < n:
        if b[i:i + 2] == b"/*":
            i = _css_skip_comment(b, i, n)
            continue
        ch = b[i:i + 1]
        if ch in (b'"', b"'"):
            i = _css_skip_string(b, i, n)
            continue
        if ch == b"{":
            depth += 1
        elif ch == b"}":
            depth -= 1
            if depth == 0:
                return i
        i += 1
    return n


def _flatten_layers(css: bytes) -> bytes:
    """去掉 @layer 外壳，规则按源码顺序保留；`@layer a, b;` 这种声明直接删。

    只删「层」这一层抽象，不动任何选择器/声明——老引擎丢掉的正是这一层。
    幂等：没有 @layer 的 CSS 原样返回（同一对象）。
    """
    if b"@layer" not in css.lower():
        return css
    out = bytearray()
    i, n = 0, len(css)
    while i < n:
        if css[i:i + 2] == b"/*":
            j = _css_skip_comment(css, i, n)
            out += css[i:j]
            i = j
            continue
        ch = css[i:i + 1]
        if ch in (b'"', b"'"):
            j = _css_skip_string(css, i, n)
            out += css[i:j]
            i = j
            continue
        m = _LAYER_TOKEN.match(css, i)
        if m:
            j = m.end()
            # 跳层名（`@layer utilities{` / `@layer a, b;`）：到第一个 { 或 ; 为止。
            while j < n and css[j:j + 1] not in (b"{", b";"):
                j += 1
            if j < n and css[j:j + 1] == b"{":
                end = _css_match_brace(css, j, n)
                out += _flatten_layers(css[j + 1:end])
                i = min(end + 1, n)
                continue
            k = css.find(b";", j)          # 声明式 `@layer a, b;`
            i = n if k < 0 else k + 1
            continue
        out += ch
        i += 1
    return bytes(out)


def _is_compat_stylesheet(head_line: bytes, header: bytes) -> bool:
    """带 ?hubcss=1 的 .css 请求（只有老 Chromium 的文档会被 _bust_css_urls 改成这样）。"""
    if not head_line.upper().startswith(b"GET ") or _is_upgrade(header):
        return False
    parts = head_line.split(b" ")
    if len(parts) < 2:
        return False
    path, _, query = parts[1].partition(b"?")
    return path.lower().endswith(b".css") and b"hubcss=1" in query


def _strip_hop_headers(header: bytes) -> bytes:
    keep = []
    for ln in _HDR_SPLIT.split(header):
        low = ln.lower()
        if not ln.strip():
            continue
        if low.startswith((b"accept-encoding:", b"connection:", b"keep-alive:", b"proxy-connection:",
                           b"transfer-encoding:", b"content-length:", b"host:",
                           # 条件请求头必须拒收：上游一旦回 304（无正文），代理无处插 style，
                           # 浏览器就会拿自己那份未注入的缓存渲染——实测窄屏顶栏打回 56px 的真凶。
                           b"if-none-match:", b"if-modified-since:", b"if-range:", b"if-unmodified-since:",
                           b"max-forwards:")):
            continue
        keep.append(ln)
    return b"\r\n".join(keep)


async def _pipe(reader: asyncio.StreamReader, writer: asyncio.StreamWriter) -> None:
    try:
        while True:
            data = await reader.read(65536)
            if not data:
                break
            writer.write(data)
            await writer.drain()
    except (ConnectionError, asyncio.CancelledError, OSError):
        pass
    finally:
        try:
            if writer.can_write_eof():
                writer.write_eof()
        except Exception:
            pass
        writer.close()


_MAX_HEAD_BYTES = 64 * 1024


def _body_shape(head: bytes) -> "tuple[int, bool]":
    """请求头的正文形态：(Content-Length, 是否 chunked)。无正文返回 (0, False)。"""
    m = re.search(rb"^content-length:\s*(\d+)", head, re.I | re.M)
    n = int(m.group(1)) if m else 0
    te = re.search(rb"^transfer-encoding:\s*([^\r\n]+)", head, re.I | re.M)
    return n, bool(te and b"chunked" in te.group(1).lower())


async def _pump_rewritten(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                          first: bytes, up_w: asyncio.StreamWriter,
                          up_r: asyncio.StreamReader, authority: bytes) -> None:
    """keep-alive 连接上**逐个请求**改写 Host，然后再双向泵。

    为什么不能只改第一个（2026-11-04 实锤）：浏览器会复用同一条 socket 连发多个
    请求，旧实现改完首个请求就进 `_tunnel` 原样透传 ⇒ 第 2 个起带着客户端 Host
    (`172.16.3.227:3202`) 到 dsh，围栏 403。实测同一连接三次 POST（带同 cookie）：
    `req1=200 / req2=403 / req3=403` —— 用户侧就是「首屏能出、会话列表空、刷新即丢」。
    请求边界按 Content-Length / chunked 切，不能拿 `\\r\\n\\r\\n` 在字节流里乱找：
    正文里出现空行会把后续帧当成新请求头改写。
    """
    async def client_to_upstream() -> None:
        buf = first
        while True:
            while b"\r\n\r\n" not in buf:
                if len(buf) > _MAX_HEAD_BYTES:
                    log.warning("[embed] 请求头超长，放弃改写转原样透传")
                    up_w.write(buf)
                    await up_w.drain()
                    await _pipe(reader, up_w)
                    return
                chunk = await reader.read(65536)
                if not chunk:
                    if buf:
                        up_w.write(buf)
                        await up_w.drain()
                    return
                buf += chunk
            idx = buf.index(b"\r\n\r\n") + 4
            head, buf = buf[:idx], buf[idx:]
            up_w.write(_rewrite_authority(head, authority))
            await up_w.drain()
            n, chunked = _body_shape(head)
            if chunked:
                while True:
                    while b"\r\n" not in buf:
                        chunk = await reader.read(65536)
                        if not chunk:
                            return
                        buf += chunk
                    eol = buf.index(b"\r\n")
                    try:
                        size = int(buf[:eol].split(b";")[0], 16)
                    except ValueError:
                        # 帧格式不认识就别装懂：剩余字节原样送出，后续交给裸泵
                        up_w.write(buf)
                        await up_w.drain()
                        await _pipe(reader, up_w)
                        return
                    need = eol + 2 + size + 2
                    while len(buf) < need:
                        chunk = await reader.read(65536)
                        if not chunk:
                            return
                        buf += chunk
                    up_w.write(buf[:need])
                    await up_w.drain()
                    buf = buf[need:]
                    if size == 0:
                        break
            elif n:
                remain = n
                while remain:
                    if buf:
                        take, buf = buf[:remain], buf[remain:]
                        up_w.write(take)
                        remain -= len(take)
                    else:
                        chunk = await reader.read(min(65536, remain))
                        if not chunk:
                            return
                        up_w.write(chunk)
                        remain -= len(chunk)
                    await up_w.drain()

    await asyncio.gather(client_to_upstream(), _pipe(up_r, writer))
    up_w.close()


async def _tunnel(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  pre: bytes, up_w: asyncio.StreamWriter, up_r: asyncio.StreamReader) -> None:
    """非 HTML：原样转发 + 双向泵。WS 升级、SSE、静态资源、/api JSON 都走这里。"""
    try:
        up_w.write(pre)
        await up_w.drain()
    except (ConnectionError, OSError):
        writer.close()
        return
    await asyncio.gather(_pipe(up_r, writer), _pipe(reader, up_w))
    up_w.close()


async def _document(writer: asyncio.StreamWriter, head_line: bytes, header: bytes,
                    up_w: asyncio.StreamWriter, up_r: asyncio.StreamReader,
                    pre_body: bytes, authority: bytes, extra: bytes = b"") -> None:
    """HTML：整份读回 → 插 <style> → 重算 Content-Length。上游强制 identity + close。

    Host 用**请求里现有的值**：默认是客户端原值；`rewrite_authority` 打开时上面
    已把它换成上游 authority，这里只管原样带过去。

    ⚠️ 历史口径（2026-11-01 → 11-04 已改判）：当时定的是「两侧统一用客户端 Host」，
    理由是 dsh 用 Host 签发 cookie 的 `authority` 并在每次校验时比对当前 Host，而
    HTML 改写上游 Host、隧道却透传客户端 Host ⇒ 两侧不等 ⇒ `/api/remote.mux` 每次
    401、前端无限重连。**统一到客户端 Host 只解决了「自洽」，没解决「可信」**：
    客户端 Host（Oray VPN 的 `172.16.3.227:3202`）不在 dsh 的 trusted-host 名单里，
    `/api` 围栏直接 403 ⇒ 同样的无限重连，只是错误码从 401 变成 403。
    现在统一到**上游 authority**（见 `_rewrite_authority`）：既自洽又落在围栏白名单内。
    """
    mh = re.search(rb"^host:\s*(?P<v>[^\r\n]+)", header, re.I | re.M)
    host_hdr = mh.group("v").strip() if mh else authority
    req = (head_line + b"\r\n" + _strip_hop_headers(header) +
           b"\r\nHost: " + host_hdr +
           b"\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n" + pre_body)
    up_w.write(req)
    await up_w.drain()
    chunks = []
    while True:
        d = await up_r.read(65536)
        if not d:
            break
        chunks.append(d)
    up_w.close()
    raw = b"".join(chunks)
    if _END not in raw:
        writer.close()
        return
    rh, rb = raw.split(_END, 1)
    # ── 2026-11-13 通用护栏：只有 HTML 文档才允许注入外框脚本 ──
    # _is_document 只看请求（GET + Accept 含 text/html + 路径末段无扩展名），而下载用的
    # window.open 顶层导航，请求头与 SPA 文档几乎一样 ⇒ 请求侧无法可靠区分。旧逻辑把
    # 上游回的 application/zip（42MB）整份读回再 _inject，往二进制里插 528 字节
    # <style>/<script> ⇒ 中央目录偏移全错、解压 CRC 失败、文件不全（journal 铁证：
    # 「[embed] 文档注入 … 42632954 bytes (注入 528)」）。
    # 判据改用上游**真实 Content-Type**：非 text/html 一律原样透传完整 raw（保留 chunked
    # 帧 / Content-Length / Content-Disposition），不注入、不解帧、不重算头，字节零改动。
    # 覆盖所有上游的下载 / zip / tar / PDF / 图片 / API JSON，不是某个产品的特例补丁。
    m_ct = _CT.search(rh)
    if not m_ct or b"text/html" not in m_ct.group("v").lower():
        log.info("[embed] 非 HTML 响应(%s)原样透传(不注入): %s",
                 (m_ct.group("v").decode(errors="replace").strip()
                  if m_ct else "无 content-type"),
                 head_line.decode(errors="replace")[:48])
        writer.write(raw)   # raw 已含头+正文与结尾空行，别补 _END
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return
    if re.search(rb"^transfer-encoding:\s*chunked", rh, re.I | re.M):
        # 剥头前先解帧：下面会把 transfer-encoding 摘掉并回 Content-Length，
        # 不解码就等于把 chunk 长度行当正文交给浏览器（见 _dechunk 注释）。
        dec = _dechunk(rb)
        if dec is None:
            log.warning("[embed] chunked 解码失败，原样透传（保留上游 transfer-encoding 头）")
            writer.write(raw)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        rb = dec
    m = _ENC.search(rh)
    if m and m.group("v").strip().lower() != b"identity":
        # 上游无视 identity：放弃注入，原样吐回（宁可不统一外框，也不破坏页面）
        log.warning("上游返回 %s 编码，跳过 style 注入（原样透传）", m.group("v").decode(errors="replace"))
        writer.write(raw)   # 注意：raw 已含结尾空行，别再补 _END，否则多一个 CRLF
    else:
        # 老 Chromium 不认 @layer：先给样式表换 URL，_stylesheet 才有机会拆层。
        if _needs_layer_flatten(header):
            rb = _bust_css_urls(rb)
        rb = _inject(rb, extra)
        log.info("[embed] 文档注入: %s -> body %d bytes (注入 %d)" % (
            head_line.decode(errors="replace")[:38], len(rb), len(extra)))
        lines = [ln for ln in _HDR_SPLIT.split(rh)
                 if ln.strip()
                 and not _ENC.match(ln)
                 and not re.match(rb"^content-length:", ln, re.I)
                 and not re.match(rb"^transfer-encoding:", ln, re.I)
                 and not re.match(rb"^connection:", ln, re.I)
                 # 响应侧同理：不能把 ETag / Last-Modified 递回浏览器，否则它就拿着验证器来要 304。
                 # 正文已被我们改过（多一段 <style>），任何跟上游原文对的缓存校验都是错的。
                 and not re.match(rb"^(etag|last-modified|age):", ln, re.I)]
        # pi-web 0.19.0（2026-11-27 升级）起上游发 frame-ancestors 'self' +
        # X-Frame-Options: SAMEORIGIN；桥端口与 hub 跨端口即跨源，浏览器据此拒渲染 iframe
        # ⇒ 用户在 agenthub 里打开 pi 看到空白。桥是受控嵌入代理（hub 自带 CSP/X-Frame-Options
        # 把门），上游的"禁止被嵌"头在这里必须剥掉，否则桥失去存在意义。
        _clean = []
        for ln in lines:
            low = ln.strip().lower()
            if low.startswith(b"x-frame-options:"):
                continue
            if low.startswith(b"content-security-policy"):
                k, _, v = ln.partition(b":")
                parts = [p.strip() for p in v.split(b";")
                         if p.strip() and not p.strip().lower().startswith(b"frame-ancestors")]
                if parts:
                    _clean.append(k + b": " + b"; ".join(parts))
                continue
            _clean.append(ln)
        lines = _clean
        # 注：剥验证器与上游的禁止嵌入头，不动 status 行与其他头；no-store 由上游 cache-control 保证。
        writer.write(b"\r\n".join(lines) + b"\r\n"
                     + b"Cache-Control: no-store\r\n"
                     + b"Content-Length: " + str(len(rb)).encode()
                     + b"\r\nConnection: close\r\n\r\n" + rb)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    writer.close()


async def _stylesheet(writer: asyncio.StreamWriter, head_line: bytes, header: bytes,
                     up_w: asyncio.StreamWriter, up_r: asyncio.StreamReader,
                     pre_body: bytes, authority: bytes) -> None:
    """CSS：整份读回 → 拆 @layer → 重算 Content-Length（只处理 ?hubcss=1）。

    与 `_document` 同一套取舍：上游强制 identity + close；拿不到明文 / 解析不了
    chunked / 不是 text/css ⇒ 原样透传，绝不猜。正文改过就不许把 ETag /
    Last-Modified 递回去（否则浏览器拿旧验证器来要 304，补丁等于没打）。
    """
    mh = re.search(rb"^host:\s*(?P<v>[^\r\n]+)", header, re.I | re.M)
    host_hdr = mh.group("v").strip() if mh else authority
    req = (head_line + b"\r\n" + _strip_hop_headers(header) +
           b"\r\nHost: " + host_hdr +
           b"\r\nAccept-Encoding: identity\r\nConnection: close\r\n\r\n" + pre_body)
    up_w.write(req)
    await up_w.drain()
    chunks = []
    try:
        # 上游必须回 Connection: close；万一某家不认，15s 后按已收字节继续（不挂住样式表）。
        async with asyncio.timeout(15):
            while True:
                d = await up_r.read(65536)
                if not d:
                    break
                chunks.append(d)
    except TimeoutError:
        log.warning("CSS 上游 15s 未收完，按已收 %d 字节继续", sum(map(len, chunks)))
    up_w.close()
    raw = b"".join(chunks)
    if _END not in raw:
        writer.close()
        return
    rh, rb = raw.split(_END, 1)
    m_ct = _CT.search(rh)
    if not m_ct or b"text/css" not in m_ct.group("v").lower():
        writer.write(raw)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return
    if re.search(rb"^transfer-encoding:\s*chunked", rh, re.I | re.M):
        dec = _dechunk(rb)
        if dec is None:
            writer.write(raw)
            try:
                await writer.drain()
            except (ConnectionError, OSError):
                pass
            writer.close()
            return
        rb = dec
    m = _ENC.search(rh)
    if m and m.group("v").strip().lower() not in (b"identity", b""):
        log.warning("CSS 上游仍回 %s 编码，跳过 @layer 兼容（原样透传）",
                    m.group("v").decode(errors="replace"))
        writer.write(raw)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return
    flat = _flatten_layers(rb)
    if flat == rb:                      # 没有 @layer（或只在注释里）：原样透传，字节零改动
        writer.write(raw)
        try:
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        writer.close()
        return
    log.info("[embed] @layer 兼容: %s -> %d bytes (原 %d)",
             head_line.decode(errors="replace")[:52], len(flat), len(rb))
    lines = [ln for ln in _HDR_SPLIT.split(rh)
             if ln.strip()
             and not _ENC.match(ln)
             and not re.match(rb"^content-length:", ln, re.I)
             and not re.match(rb"^transfer-encoding:", ln, re.I)
             and not re.match(rb"^connection:", ln, re.I)
             and not re.match(rb"^(etag|last-modified|age|cache-control):", ln, re.I)]
    writer.write(b"\r\n".join(lines) + b"\r\n"
                 + b"Cache-Control: no-store\r\n"
                 + b"Content-Length: " + str(len(flat)).encode()
                 + b"\r\nConnection: close\r\n\r\n" + flat)
    try:
        await writer.drain()
    except (ConnectionError, OSError):
        pass
    writer.close()



def _upstream_candidates(host: str) -> list[str]:
    """回环上游在 v4/v6 互为备胎：uvicorn/asyncio 绑 `::` 时强制 IPV6_V6ONLY=1，
    2026-09-21 qwenpaw 2.2.1 起 127.0.0.1 直连被 RST，需要回落 ::1。"""
    alt = {"127.0.0.1": "::1", "::1": "127.0.0.1"}.get(host)
    return [host, alt] if alt else [host]


def _authority(host: str, port: int) -> bytes:
    # IPv6 写进 Host 头必须加方括号，否则 "::1:8088" 是歧义地址
    return ("[%s]:%d" % (host, port) if ":" in host else "%s:%d" % (host, port)).encode()


def _rewrite_authority(header: bytes, authority: bytes) -> bytes:
    """把客户端 Host 改写成上游 authority，并摘掉 Origin / Sec-Fetch-Site。

    为什么需要（2026-11-04 报障「dsh 原生界面一直连接中」）：
    dsh 的 `/api` 围栏（`packages/client/connection/src/api-request-trust.ts`）只放行
    ① 回环地址 ② 它自己 `--host` 绑的地址 ③ `--trusted-host` 显式声明的 authority。
    hub 的嵌页桥原先按客户端 Host 原样透传，用户经 Oray VPN 打开 hub 时 Host 是
    `172.16.3.227:3202` —— 不在名单里：HTML 文档照常下发（文档不走 `/api` 围栏），
    但 `/api/remote.mux`（WebSocket）与其余 `/api` **一律 403**，前端于是无限重连，
    用户看到的就是「一直连接中 + 会话列表空 + 登录失败」三连。
    改写成上游 authority 后 Host 恒为绑定地址（围栏白名单内），且 document 与
    tunnel 两条路径同一份值 ⇒ dsh 签发 cookie 时记的 `authority` 与之后校验时的
    Host 自洽（11-01 的 401 死循环正是「只改文档不改隧道」的不对称造成的）。

    Origin 一并摘除：围栏在无 Origin 时改以 Host 为准；留着它等于拿客户端的
    authority 去比对改写后的 Host，必 403（`sec-fetch-site: cross-site` 同理）。

    只给**按 Host 判来源**的上游开（profiles 的 `rewrite_host`）：别家（qwenpaw /
    pi-web / cloudcli）会用 Host 拼绝对跳转地址，改写后会把浏览器送到上游地址。
    """
    head, sep, body = header.partition(_END)
    lines = head.split(b"\r\n")
    out = [lines[0]] if lines else [b""]
    for ln in lines[1:]:
        low = ln.lower()
        if low.startswith(b"host:"):
            continue
        if low.startswith(b"origin:") or low.startswith(b"sec-fetch-site:"):
            continue
        out.append(ln)
    out.append(b"Host: " + authority)
    return b"\r\n".join(out) + sep + body


async def _handle(reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                  upstream: tuple[str, int], extra: bytes = b"",
                  pre_header: bytes | None = None, auth: bytes = b"",
                  rewrite_authority: bool = False, cookie: bytes = b"") -> None:
    if pre_header is None:
        try:
            header = await reader.readuntil(_END)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
    else:
        header = pre_header
    parts = header.split(_END, 1)
    head_line = parts[0].split(b"\r\n", 1)[0]
    rest = parts[0][len(head_line) + 2:]
    # 桥内登录成功后由服务端补 Basic（客户端自带的 Authorization 不覆盖）：
    # 浏览器在跨源 iframe 里拿不到表单，也弹不出上游的认证框，只能由桥代持。
    if auth and not re.search(rb"^authorization:", rest, re.I | re.M):
        rest = rest + b"\r\nAuthorization: Basic " + auth
        header = head_line + b"\r\n" + rest + _END
    # 上游只认文档级 Cookie 时（pi-web：Basic 仅对 /api 生效，"/" 仍跳 /login），
    # 由桥补一枚服务端算出的会话 Cookie；客户端已有 Cookie 就追加，不覆盖。
    if cookie:
        m = re.search(rb"^cookie:[ \t]*(.*?)\r?$", rest, re.I | re.M)
        if m:
            rest = rest[:m.start(1)] + m.group(1) + b"; " + cookie + rest[m.end(1):]
        else:
            rest = rest + b"\r\nCookie: " + cookie
        header = head_line + b"\r\n" + rest + _END
    # 若客户端在同一连接上已带正文（PUT/POST 的 body 紧跟头），已读到的部分要一起转发
    pre_body = parts[1] if len(parts) > 1 else b""
    host, port = upstream
    up_r = up_w = None
    last_err: OSError | None = None
    for try_host in _upstream_candidates(host):
        try:
            up_r, up_w = await asyncio.open_connection(try_host, port)
            host = try_host
            break
        except OSError as e:
            last_err = e
    if up_w is None:
        msg = b"HTTP/1.1 502 Bad Gateway\r\nContent-Length: 0\r\nConnection: close\r\n\r\n"
        try:
            writer.write(msg)
            await writer.drain()
        except Exception:
            pass
        log.debug("上游不可达 %s:%s (%s)", upstream[0], port, last_err)
        writer.close()
        return
    if rewrite_authority:
        # 上游 authority 要在**连上之后**取：回环上游可能回落到 ::1（见 _upstream_candidates），
        # 两端（document / tunnel）必须用同一个值，否则 dsh 的 cookie authority 又不自洽。
        header = _rewrite_authority(header, _authority(host, port))
        parts = header.split(_END, 1)
        head_line = parts[0].split(b"\r\n", 1)[0]
        rest = parts[0][len(head_line) + 2:]
    doc = _is_document(head_line, rest)   # 逐请求埋点已摘（会把 hub 日志刷满）
    if not doc and head_line.upper().startswith(b"GET ") and b"text/html" in rest.lower():
        log.info("[embed] GET+html Accept 但判为非文档，走隧道: %s" % head_line.decode(errors="replace")[:48])
    if doc:
        await _document(writer, head_line, rest, up_w, up_r, pre_body,
                        _authority(host, port), extra)
    elif _is_compat_stylesheet(head_line, rest):
        await _stylesheet(writer, head_line, rest, up_w, up_r, pre_body,
                          _authority(host, port))
    elif rewrite_authority and b"upgrade:" not in rest.lower():
        # 非升级请求走逐请求改写；WS 升级后是全双工裸字节流，不能再按 HTTP 解析
        await _pump_rewritten(reader, writer, header + pre_body, up_w, up_r,
                              _authority(host, port))
    else:
        await _tunnel(reader, writer, header + pre_body, up_w, up_r)


def _lan_ip() -> str:
    """本机默认路由出口 IP（UDP connect 不发包，只让内核选路）。取不到返回空串。"""
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    try:
        s.connect(("8.8.8.8", 53))
        return s.getsockname()[0]
    except OSError:
        return ""
    finally:
        s.close()


#: 回环/未声明地址的字面量（比较前统一 strip("[]").lower()）
_LOOPBACK_HOSTS = {"", "127.0.0.1", "localhost", "::1"}
#: 起法里表达「监听地址」的旗标（opencode --hostname / zcode --host / reasonix --addr）
_BIND_FLAGS = ("--host", "--hostname", "--host-ip", "--addr", "--address",
               "--bind", "--listen")


def _start_binds_wide(start) -> bool:
    """起法里是否声明了「上游对局域网开放」。

    两条来源，按可信度排：
    ① `start["bind"]` —— 给 unit 式起法的显式声明。unit 的 ExecStart 在画像里读不到
       （pi-web 实际就是 `--hostname 0.0.0.0 --port 30141`），不声明就只能退回运行时
       探测；而按需拉起的 agent 在 hub 启动那一刻通常停着 ⇒ 探测必假死 ⇒ 桥永久锁
       回环（2026-11-02 pi 报障的同因，dsh 已于 v0.13.18x 改判）。
    ② `start["cmd"]` 里的监听旗标：`--host 0.0.0.0` / `--addr 0.0.0.0:8787` /
       `--host=192.168.5.102` 都算「上游自己就要对局域网开放」。
    """
    start = start or {}
    if _host_is_wide(str(start.get("bind") or "")):
        return True
    cmd = start.get("cmd") or []
    for i, a in enumerate(cmd):
        s = a if isinstance(a, str) else ""
        val = ""
        for flag in _BIND_FLAGS:
            if s == flag and i + 1 < len(cmd):
                val = str(cmd[i + 1])
            elif s.startswith(flag + "="):
                val = s.split("=", 1)[1]
        if val and _host_is_wide(val):
            return True
    return False


def _host_is_wide(val: str) -> bool:
    """`0.0.0.0` / `::` / `*` / 具体网卡（`192.168.x.x`、`host:port`）⇒ True。"""
    raw = (val or "").strip().lower()
    if raw.startswith("["):               # [::] / [::]:8080 / [::1]:8080
        raw = raw[1:].split("]", 1)[0]
    elif raw not in {"::"}:
        raw = raw.split(":", 1)[0]        # host:port 只取主机段
    if raw in {"0.0.0.0", "::", "*"}:
        return True
    return bool(raw) and raw not in _LOOPBACK_HOSTS


def listen_host_for(port: int, default: str = "0.0.0.0", *,
                    upstream_host: str = "", start=None) -> str:
    """桥监听地址 = 上游**会**有的可达面（不外扩暴露面）。

    2026-11-02 改判：旧实现只看「此刻从本机 LAN IP 连不连得上上游」，而按需拉起的
    agent 常态是停着的 —— hub 启动那一刻连不上就把桥**永久**锁在回环。实测后果：
    hub 于 2026-11-01 23:51 启动（dsh 已停），桥 :3202 绑 127.0.0.1，局域网访问
    `http://192.168.5.102:3202/` 恒 ERR_CONNECTION_REFUSED，而 `/ui-status` 判的是
    上游 3080（活着）⇒ 前端认为「就绪」、不挂覆盖层，用户看到的是一张裸的拒绝连接页。
    同批被绑死的还有 opencode/zcode/qoder/cloudcli（都是启动时停着的）。

    所以静态可达面优先、运行时探测兜底：
    ① 声明地址是具体网卡/通配（dsh 写死 192.168.5.102）⇒ 上游本来就是 LAN 可达；
    ② 起法里显式 `--host 0.0.0.0` 之类（opencode/zcode/reasonix）⇒ 同上；
    ③ 两者都没有（claude-mem / qoder 这类只绑回环的面板）⇒ 才连一次实测，
       连不上就保持 127.0.0.1，绝不把无鉴权面板经桥抬到局域网。
    探测拿不到 LAN IP 一律按回环处理：宁可 LAN 用不了，也不静默扩面。
    """
    host = (upstream_host or "").strip().strip("[]").lower()
    if host in {"0.0.0.0", "::", "*"}:
        return default
    if host not in _LOOPBACK_HOSTS:
        return default
    if _start_binds_wide(start):
        return default
    # 静态监听面优先于运行时探测：上游 socket 绑在通配地址 ⇒ 它本来就对 LAN 开放，
    # 与「此刻 LAN IP 是否就绪」无关。探测只作最后兜底。
    # 反例（2026-11-06 报障「基础设施里面板端口变 3208 且打不开」）：主机 22:33:59 重启、
    # hub 22:34:26 起，比网络就绪早 ⇒ `_lan_ip()` 返空 ⇒ proxy-panel(:8083, 实绑 `*:8083`)
    # 被判成只绑回环，桥 :3208 永久锁 127.0.0.1，而前端把回环换成当前主机
    # （static/hub.js 的 lanUrl）⇒ 局域网访问恒 ERR_CONNECTION_REFUSED。
    if _upstream_binds_wide(port):
        return default
    return default if _upstream_lan_reachable(port) else "127.0.0.1"


def listen_host_for_browser(target: dict, default: str, hub_auth: bool) -> str:
    """浏览器入口的桥监听面（2026-11-11 用户裁定「所有原生界面复用 hub 登录」）。

    只要 **hub 会话把门**（hub_auth=1），就把桥放到 default（EMBED_PROXY_HOST，
    本机是 0.0.0.0）——否则独立浏览器/手机从桥进不来，只能回落到上游自己的登录面，
    等于把「一套登录」的承诺作废。上游原本只绑回环（qoder/claude-mem 等）也照放：
    此时唯一的入口是带 hub 鉴权的桥，反而比裸端口更安全。

    ⚠ hub_auth=0（旧排障形态）时桥自己只认一个共享口令，绝不能跟着对外，
    退回 listen_host_for 的保守判据。
    """
    if hub_auth and target.get("browser_entry"):
        return default
    return listen_host_for(int(target["port"]), default,
                           upstream_host=str(target.get("host") or ""),
                           start=target.get("start"))


def _listen_addrs(port: int) -> set:
    """该端口此刻处于 LISTEN 的本地地址（/proc/net/tcp{,6} 的十六进制原文）。"""
    addrs = set()
    for path in ("/proc/net/tcp", "/proc/net/tcp6"):
        try:
            with open(path, encoding="ascii", errors="replace") as fh:
                next(fh, None)
                for line in fh:
                    fields = line.split()
                    # sl local_address rem_address st ...
                    if len(fields) < 4 or fields[3] != "0A":
                        continue
                    addr, _, raw_port = fields[1].partition(":")
                    try:
                        if int(raw_port, 16) != port:
                            continue
                    except ValueError:
                        continue
                    addrs.add(addr.upper())
        except OSError:
            continue
    return addrs


def _upstream_binds_wide(port: int) -> bool:
    """上游是否绑在通配地址（0.0.0.0 / ::）。

    读内核监听表而不是连一次：监听面是 socket 的静态属性，开机早期网络栈未就绪
    也照样能读准。这条判据只回答「上游本来会不会被 LAN 访问到」，不放宽任何面板。
    """
    return bool(_listen_addrs(port) & {"00000000", "0" * 32})


def _upstream_lan_reachable(port: int) -> bool:
    """此刻从本机 LAN IP 能不能连上上游端口（只给 listen_host_for 的兜底分支用）。"""
    ip = _lan_ip()
    if not ip:
        return False
    import socket
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((ip, port))
        return True
    except OSError:
        return False
    finally:
        s.close()


class EmbedProxy:
    """一个实例 = 一个本地端口 = 一个上游。多宿主时各起一个实例。

    `css` 只为外框统一（qwenpaw 顶栏）；`keys=True` 给每个嵌页注入 ⌘/Ctrl+K 桥。
    两个注入项都走同一条字节级插入路径，互不依赖：CSS 空而 KEYS 在也照常工作。
    """

    def __init__(self, name: str, upstream_host: str, upstream_port: int,
                 listen_host: str = "0.0.0.0", listen_port: int = 3103,
                 css: str = "", keys: bool = True,
                 auth_secret: str = "", title: str = "",
                 rewrite_authority: bool = False,
                 hub_auth: bool = True, hub_port: int = 3102,
                 perm: str = "", basic_user: str = "hub", session_cookie: str = "",
                 upstream_cookie_file: str = ""):
        self.name = name
        self.upstream = (upstream_host, int(upstream_port))
        self.listen = (listen_host, int(listen_port))
        self.server: asyncio.base_events.Server | None = None
        # COMPAT_JS 与 css/keys 无关：它是「浏览器缺 API」的垫片，不是 ⌘K 功能的一部分。
        # 只挂在 keys 上会让 keys=False 的桥（以及将来新加的桥）重新踩同一个坑。
        self.extra = ((css.encode() if css else b"")
                      + VIEWPORT_CSS.encode()
                      + COMPAT_JS.encode()
                      + (KEYS_JS.encode() if keys else b""))
        # 非空 ⇒ 这个桥代持上游 Basic（面板口令文件）。**不再**拿它当用户口令：
        # 用户只要在 hub 登录过，桥就自动补上（2026-11-07 用户裁定）。
        self.auth_secret = (auth_secret or "").strip()
        # Basic 的用户名由上游决定：面板/CCR 用 hub，pi-web 的代码里硬编码只认 "pi"。
        self.basic_user = (basic_user or "hub").strip() or "hub"
        # 非空 ⇒ 桥按该 Cookie 名自行铸造上游会话（pi-web：文档页只认 Cookie）。
        self.session_cookie = (session_cookie or "").strip()
        # 上游自带登录、但口令可离线铸造会话（codebuddy :35431 的 gateway_session）：
        # 文件里放完整 `name=value`，桥逐请求代发 ⇒ 用户只登 hub，不碰上游登录页。
        self.upstream_cookie_file = (upstream_cookie_file or "").strip()
        # 非空 ⇒ 这个桥要求登录用户在 agenthub 里被授权该模块（agent 级鉴权）。
        # 管理员 perms=None 直接放行；普通用户 perms 不含该键 ⇒ 403（2026-11-08）。
        self.perm = (perm or "").strip()
        self.title = title or name
        # 按 Host 判来源的上游（首指 dsh）：请求出桥前把 Host 换成上游 authority，
        # 并摘 Origin —— 详见 _rewrite_authority 的报障记录。
        self.rewrite_authority = bool(rewrite_authority)
        # hub_auth=True ⇒ 桥的门 = hub 登录态（Cookie hub_session）。
        # 未登录的文档请求 302 回 hub 登录页，其余 401。关掉即回落到「桥自己出口令表单」
        # 的旧形态（EMBED_HUB_AUTH=0，仅供排障）。
        self.hub_auth = bool(hub_auth)
        self.hub_port = int(hub_port)
        self._secret_cache: tuple[float | None, str] = (None, "")

    # ── 口令读取（按 mtime 缓存：面板轮换口令后无需重启 hub）──
    def _secret(self) -> str:
        if not self.auth_secret:
            return ""
        try:
            mtime = os.stat(self.auth_secret).st_mtime
        except OSError:
            return ""
        if self._secret_cache[0] != mtime:
            try:
                with open(self.auth_secret, encoding="utf-8") as fh:
                    self._secret_cache = (mtime, fh.read().strip())
            except OSError:
                return ""
        return self._secret_cache[1]

    def _cookie_token(self) -> str:
        s = self._secret()
        return hashlib.sha256(s.encode()).hexdigest()[:32] if s else ""

    def _cookie_name(self) -> str:
        return "hub_bridge_%d" % self.listen[1]

    def _auth_header(self) -> bytes:
        s = self._secret()
        return base64.b64encode(("%s:%s" % (self.basic_user, s)).encode()) if s else b""

    def _upstream_cookie(self) -> bytes:
        """pi-web 会话 Cookie：`v1.<exp>.<rand>.<HMAC(password, "pi-web-session:"+前缀)>`。

        与 .next 里 `p(a,b)` / `web-auth/route` 的算法逐字一致（30 天有效）。
        只在 `session_cookie` 非空时生成；口令缺失则不发，宁失败也不放行。
        """
        if self.upstream_cookie_file:
            try:
                with open(self.upstream_cookie_file, encoding="utf-8") as fh:
                    return fh.read().strip().encode()
            except OSError:
                # 文件缺失 ⇒ 不发，宁可让上游弹它自己的登录页也不伪造值
                return b""
        name, s = self.session_cookie, self._secret()
        if not name or not s:
            return b""
        base = "v1.%d.%s" % (int(time.time()) + 2592_000, os.urandom(16).hex())
        mac = hmac.new(s.encode(), ("pi-web-session:" + base).encode(),
                       hashlib.sha256).hexdigest()
        return ("%s=%s.%s" % (name, base, mac)).encode()

    async def _serve(self, reader: asyncio.StreamReader,
                     writer: asyncio.StreamWriter) -> None:
        """门 = hub 登录态；需要时由桥代补上游 Basic（用户零输入）。"""
        if not self.hub_auth and not self.auth_secret:
            await _handle(reader, writer, self.upstream, self.extra,
                          rewrite_authority=self.rewrite_authority)
            return
        try:
            header = await reader.readuntil(_END)
        except (asyncio.IncompleteReadError, ConnectionError, asyncio.LimitOverrunError):
            writer.close()
            return
        method, path = _req_line(header)
        if self.hub_auth:
            _res = _hub_session_ok(header)          # 3 元组；旧桩只回 2 元组也兼容
            _ok, _perms = _res[0], _res[1]
            _who = _res[2] if len(_res) > 2 else ""
            # 审计（2026-11-11 用户要求「加桥审计日志」）：只记对端、用户名、方法与路径，
            # **绝不记 Cookie/token**。放行只记文档/顶层请求，静态资源不刷屏；拒绝按面记。
            _peer = _peer_of(writer)
            _p = path.decode(errors="replace") or "/"
            doc_like = (method == b"GET" and (b"text/html" in header.lower()
                                              or path == b"/" or bool(_HTML_PATH.search(path))))
            # agent 级授权：管理员 perms=None 放行；普通用户必须被勾选了该模块。
            # 未授权一律 403（页面/接口都不给半截），别回落到上游（2026-11-08）。
            if _ok and self.perm and _perms is not None and self.perm not in _perms:
                log.info("[%s] 403 未授权 user=%s peer=%s %s %s（缺模块 %s）",
                         self.name, _who or "?", _peer, method.decode(errors="replace"),
                         _p, self.perm)
                if doc_like and not _is_upgrade(header):
                    await self._send(writer, 403, _forbidden_page(self.title, self.perm))
                else:
                    await self._send(writer, 403, b'{"error":"forbidden","module":"'
                                     + self.perm.encode() + b'"}')
                return
            if not _ok:
                # 未登录：文档/顶层导航送回 hub 登录页（登录后浏览器带着 Cookie 回来），
                # 其余（XHR/静态/WS）一律 401 —— 不给半截页面，也不偷偷放行数据面。
                if doc_like and not _is_upgrade(header):
                    host = _host_of(header) or "127.0.0.1"
                    nxt = urllib.parse.quote("http://%s:%d%s" % (host, self.listen[1],
                                                                 path.decode(errors="replace") or "/"))
                    loc = "http://%s:%d/?next=%s" % (host, self.hub_port, nxt)
                    # 2026-11-28：**iframe 内不许出现"登录页套娃"**。
                    # 用户报「pi web 在 agenthub 里打不开，刷新一下又可以」，2026-11-28
                    # 08:38 日志给出机制：桥 302 回 hub 登录页 —— 而这个 302 发生在
                    # **iframe 里**（顶栏仍是 hub），登录页被塞进 768px 的框里，
                    # 用户看到的就是"打不开"；等顶栏登录完成后再刷新，Cookie 到位才恢复。
                    # 处置：iframe 文档请求不返回 302，而是返回一张**把顶层导航到登录页**
                    # 的小页面（登录后 next 直接回到嵌入地址）；无 JS 时给出可点的链接。
                    # 判据 `sec-fetch-dest: iframe`（Chrome 76+ 全量支持，缺省按顶层处理）。
                    if _is_iframe_doc(header):
                        await self._send(writer, 200, _iframe_login_page(loc))
                        log.info("[%s] 200 未登录(iframe) peer=%s %s %s -> 顶层跳登录 %s",
                                 self.name, _peer, method.decode(errors="replace"), _p, loc)
                        return
                    await self._write(writer, (
                        "HTTP/1.1 302 Found\r\nLocation: %s\r\n"
                        "Content-Length: 0\r\nCache-Control: no-store\r\n"
                        "Connection: close\r\n\r\n" % loc).encode())
                else:
                    if _p.startswith("/api/"):
                        log.info("[%s] 401 未登录 peer=%s %s %s", self.name, _peer,
                                 method.decode(errors="replace"), _p)
                    else:
                        log.debug("[%s] 401 未登录(静态) peer=%s %s", self.name, _peer, _p)
                    await self._send(writer, 401, b"login required (AgentHub)")
                    return
                log.info("[%s] 302 未登录 peer=%s %s %s -> hub 登录页",
                         self.name, _peer, method.decode(errors="replace"), _p)
                return
            if doc_like:
                log.info("[%s] 放行 user=%s peer=%s %s %s -> 上游 %s:%s",
                         self.name, _who or "?", _peer, method.decode(errors="replace"),
                         _p, self.upstream[0], self.upstream[1])
            await _handle(reader, writer, self.upstream, self.extra,
                          pre_header=header, auth=self._auth_header(),
                          rewrite_authority=self.rewrite_authority,
                          cookie=self._upstream_cookie())
            return
        # 旧形态（EMBED_HUB_AUTH=0 排障用）：桥自己出口令表单
        token = self._cookie_token()
        if method == b"POST" and path == b"/__hub_login":
            await self._login(reader, writer, header, token)
            return
        if token and _cookie_has(header, self._cookie_name(), token):
            await _handle(reader, writer, self.upstream, self.extra,
                          pre_header=header, auth=self._auth_header(),
                          rewrite_authority=self.rewrite_authority,
                          cookie=self._upstream_cookie())
            return
        # 未登录：文档请求给表单；其余（XHR/静态资源）一律 401，别把半截页面喂给前端。
        # 文档判据与 _is_document 同向（GET + .html 或 Accept 含 text/html），另加「无扩展名
        # 路径」——iframe 顶层导航不保证带 Accept，别让用户看到裸的 login required。
        doc_like = (method == b"GET" and (b"text/html" in header.lower()
                                          or path == b"/" or bool(_HTML_PATH.search(path))))
        if doc_like:
            await self._send(writer, 200, _login_page(self.title + " 需要登录"))
        else:
            await self._send(writer, 401, b"login required")

    async def _login(self, reader: asyncio.StreamReader, writer: asyncio.StreamWriter,
                     header: bytes, token: str) -> None:
        m = re.search(rb"^content-length:\s*(\d+)", header, re.I | re.M)
        n = min(int(m.group(1)), 8192) if m else 0
        body = b""
        if n:
            try:
                body = await reader.readexactly(n)
            except (asyncio.IncompleteReadError, ConnectionError):
                body = b""
        got = (urllib.parse.parse_qs(body.decode("utf-8", "replace")).get("p") or [""])[0]
        want = self._secret()
        if want and got and hmac.compare_digest(got.encode(), want.encode()):
            head = ("HTTP/1.1 303 See Other\r\nLocation: /\r\n"
                    "Set-Cookie: %s=%s; Path=/; HttpOnly; SameSite=Lax; Max-Age=604800\r\n"
                    "Content-Length: 0\r\nConnection: close\r\n\r\n"
                    % (self._cookie_name(), token)).encode()
            await self._write(writer, head)
            return
        await self._send(writer, 401, _login_page(self.title + " 需要登录", "口令不正确"))

    async def _send(self, writer: asyncio.StreamWriter, code: int, body: bytes) -> None:
        _reason = {200: "OK", 401: "Unauthorized", 403: "Forbidden", 404: "Not Found",
                   429: "Too Many Requests"}.get(code, "Error")
        head = ("HTTP/1.1 %d %s\r\nContent-Type: text/html; charset=utf-8\r\n"
                "Content-Length: %d\r\nCache-Control: no-store\r\nConnection: close\r\n\r\n"
                % (code, _reason, len(body))).encode()
        await self._write(writer, head + body)

    async def _write(self, writer: asyncio.StreamWriter, data: bytes) -> None:
        try:
            writer.write(data)
            await writer.drain()
        except (ConnectionError, OSError):
            pass
        try:
            writer.close()
        except Exception:
            pass

    async def start(self) -> bool:
        if self.server is not None:
            return True
        try:
            self.server = await asyncio.start_server(self._serve, *self.listen)
        except OSError as e:
            log.warning("[%s] 注入代理监听 %s:%s 失败：%s —— 嵌入视图将回落直连", self.name, *self.listen, e)
            return False
        log.info(f"[Agent Hub] {self.name} 外框注入代理：{self.listen[0]}:{self.listen[1]} → "
                 f"{self.upstream[0]}:{self.upstream[1]}")
        return True

    async def stop(self) -> None:
        if self.server is not None:
            self.server.close()
            try:
                await self.server.wait_closed()
            except Exception:
                pass
            self.server = None
