/* static/hub-loader.js — T5 首屏按片加载器（2026-11-13，经典脚本，非 module）。
 *
 * 为什么放在 static/ 根而不是 static/hub/：后者命中 build_hubjs.sh 的
 * `[0-9][0-9]-*.js` 拼接轨道，加载器会被拼进 legacy hub.js，逐字节闸门立刻红。
 * 加载器不是分片，不该进聚合产物。
 *
 * 首屏 4 片依据 docs/T5-MODULE-GRAPH-20261113.md 的实测依赖矩阵：
 * 01 顶层零跨片调用，但 go() 每页调 05 的 renderNav、classroom 靠 03 渲染卡片，
 * 02 提供导航渲染函数 —— 这 4 片是首屏闭包，其余 9 片可延迟。
 *
 * 接入方式（第 2 步，尚未落地）：模板 loadHub() 改为注入本文件并调
 * loadHubShards()；?legacy=1 走聚合产物 hub.js（长期契约）。
 * ⚠ 本文件当前只由 tests/test_t5_loader_l0.py 验证契约；接模板前不得删改。
 */
(function (global) {
  'use strict';
  var BASE = '/static/hub/';
  var LEGACY = '/static/hub.js';
  var CORE = [
    '01-core-boot.js',
    '02-nav-and-poll.js',
    '03-agents-cards.js',
    '05-chat-and-history.js'
  ];
  var pending = {};   // name -> Promise；同名只发一次请求（T6 的 promise 缓存原则）

  function tokenOf(name) {
    var t = global.HUB_VTOKENS || {};
    return t[name] ? '?v=' + t[name] : '';
  }

  function inject(src) {
    return new Promise(function (resolve, reject) {
      var s = document.createElement('script');
      s.src = src;
      s.onload = function () { resolve(src); };
      s.onerror = function () { reject(new Error('shard load failed: ' + src)); };
      (document.head || document.body).appendChild(s);
    });
  }

  /* 按名加载一个分片；重复调用返回同一个 Promise（零重复请求）。 */
  function ensureShard(name) {
    if (!pending[name]) {
      pending[name] = inject(BASE + name + tokenOf(name));
      pending[name].catch(function () { delete pending[name]; });
    }
    return pending[name];
  }

  /* 首屏：按序注入 4 片（顺序即依赖顺序，不能并发）。 */
  function loadHubShards(legacy) {
    if (legacy) return inject(LEGACY + tokenOf('hub.js'));
    return CORE.reduce(function (chain, name) {
      return chain.then(function () { return ensureShard(name); });
    }, Promise.resolve());
  }

  global.ensureShard = ensureShard;
  global.loadHubShards = loadHubShards;
  global.HUB_CORE_SHARDS = CORE.slice();
})(window);
