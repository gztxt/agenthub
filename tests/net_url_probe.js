// net_url_probe.js — 把 static/hub/01-core-boot.js 里的 lanUrl 真实跑一遍。
//
// 为什么不用正则断言「代码里写了 SELF_ADDRS」：那种断言在函数被改坏时照样绿
// （判例 39/253：判据量错对象）；这里直接把**源文件里的函数**取出来执行，
// 输入输出逐条比对 —— 改坏了必红。
//
// 用法: node net_url_probe.js <shard.js> '<json cases>'
//   cases: [{self:[...], host:"...", url:"...", want:"..."}]
// 输出: [{url,want,got,ok}] + 退出码（有任一不 ok 即 1）
'use strict';
const fs = require('fs');

const [file, casesJson] = process.argv.slice(2);
if (!file || !casesJson) {
  console.error('usage: node net_url_probe.js <shard.js> <json-cases>');
  process.exit(2);
}
const src = fs.readFileSync(file, 'utf8');

function grab(name) {
  const i = src.indexOf('function ' + name + '(');
  if (i < 0) throw new Error('function ' + name + ' not found in ' + file);
  let depth = 0;
  const start = src.indexOf('{', i);
  for (let k = start; k < src.length; k++) {
    if (src[k] === '{') depth++;
    else if (src[k] === '}') {
      depth--;
      if (depth === 0) return src.slice(i, k + 1);
    }
  }
  throw new Error('unbalanced braces in ' + name);
}

const body = grab('_selfHost') + '\n' + grab('lanUrl');
// SELF_ADDRS 在源文件里是顶层 let；这里用同名参数注入，语义等价（同一份函数体）。
const factory = new Function(
  'SELF_ADDRS', 'location',
  body + '\nreturn lanUrl;'
);

const cases = JSON.parse(casesJson);
const out = [];
for (const c of cases) {
  const fn = factory(c.self || [], { hostname: c.host });
  const got = fn(c.url);
  out.push({ url: c.url, host: c.host, want: c.want, got: got, ok: got === c.want });
}
process.stdout.write(JSON.stringify(out, null, 2));
process.exit(out.every(r => r.ok) ? 0 : 1);
