#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L2 真渲染探针：6 处 inline onclick + jsStr 的**修复后行为**（PT-20261019-02）。

跑法：
    venv/bin/python tests/probe_inline_onclick.py [base-url]
默认 `http://127.0.0.1:3102`（生产）。**本探针零写操作**：
所有动作函数（cloudcliStart / verifyAgent / gotoChat）与 `api()` 都在页面里
被替换成桩，服务端一条写请求都不会发出，也不会真起 CloudCLI 会话或耗 token。

【它补哪个洞】`tests/test_inline_onclick_shards.py` 只判源码里不再有
`onclick=` + `jsStr(` 的组合。判不了三件事：
  ① 渲染出的属性值是否**完整**（旧 bug 的形态是属性被截断成 `f(`）；
  ② `escapeHtml` 进属性后 `dataset` 是否能**原样读回**（含引号的路径不会裂）；
  ③ 委托真的把**正确的参数**送到了动作函数（写了 data-* 没人绑 = 死按钮）。
本探针把三条都变成可断言的值。

【做法】用**敌意数据**（路径/工具名里带 `"` 与 `onclick=`）走真实模板串渲染，
再真点一次，比对桩收到的参数是否与原值逐字符相等。故意用带引号的值：
若还残留 `jsStr` 进双引号属性的写法，渲染态会立刻裂成半个函数调用。

【六个点位】
  #1 agents 抽屉「实测应答」  → data-sk-verify   → verifyAgent(id)
  #2 CloudCLI 行（embed 面板）→ data-sk-ccstart  → cloudcliStart(path)
  #3 同行「▶ 开始会话」按钮   → 无属性，靠 closest 命中行 → 只触发一次
  #4 claude 抽屉 CloudCLI 按钮→ data-sk-ccstart  → cloudcliStart(path)
  #5 MCP 工具行               → data-sk-pt-{server,name} → pickTool(server, tool)
  #6 ⌘K 命令项                → data-sk-cmdgo    → cmdGo(id) → gotoChat(id)

【退出码】0 = 全达标；1 = 有不达标项。
"""
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
CDP_PORT = 9373
FAILS = []

# 敌意值：引号会截断旧的 onclick 属性；onmouseover 用来验证不会凭空多出事件属性
HOSTILE = '/srv/x" onmouseover="alert(1)'


def ok(msg):
    print("  PASS %s" % msg)


def bad(msg):
    print("  FAIL %s" % msg)
    FAILS.append(msg)


def evaluate(cdp, expr, timeout=20):
    r = cdp.send("Runtime.evaluate", expression=expr, returnByValue=True,
                 awaitPromise=True, timeout=timeout)
    if not r or "exceptionDetails" in r:
        return None
    return (r.get("result") or {}).get("value")


def wait_until(cdp, expr, timeout=15):
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = evaluate(cdp, expr)
        if last:
            return last
        time.sleep(0.25)
    return last


# 一次注入跑完六组：桩 + 渲染 + 真点击 + 读回
SCRIPT = r"""
(async () => {
  const out = {calls: {}, err: []};
  const sleep = ms => new Promise(r => setTimeout(r, ms));
  const HOSTILE = %(hostile)s;
  const realApi = window.api;
  const calls = out.calls;

  // ── 桩：api 按路径返回敌意数据；三个动作函数只记参数 ──
  window.api = function (u, o) {
    const s = String(u);
    if (s.indexOf('/api/cloudcli/projects') >= 0)
      return Promise.resolve({ok: true, count: 2, projects: [
        {name: '正常', path: '/srv/normal', sessions: 3},
        {name: '恶意', path: HOSTILE}]});
    if (s.indexOf('/mcp/servers') >= 0)
      return Promise.resolve({servers: [{name: 'srv1', transport: 'stdio', command: 'x'}]});
    if (s.indexOf('/mcp/tools') >= 0)
      return Promise.resolve({tools: [{server: 'srv1', server_name: 'S1',
                                       name: 'tool"1', description: 'd'}], errors: {}});
    return realApi(u, o);
  };
  window.cloudcliStart = p => { (calls.cc = calls.cc || []).push(p); };
  window.verifyAgent = id => { calls.verify = id; };
  window.gotoChat = id => { calls.goto = id; };

  const attr = (el, n) => el ? el.getAttribute(n) : 'MISSING';

  // ── #1 + #4：同一个 #detailBody，一次渲染两处受益 ──
  showDetail('claude');
  await sleep(500);
  const vb = document.querySelector('#detailBody [data-sk-verify]');
  const cc4 = Array.from(document.querySelectorAll('#detailBody [data-sk-ccstart]'));
  out.s1 = {found: !!vb, onclick: attr(vb, 'onclick'), val: vb && vb.dataset.skVerify};
  if (vb) vb.click();
  out.s1.call = calls.verify;

  out.s4 = {found: cc4.length, onclick: cc4.map(b => attr(b, 'onclick')),
            vals: cc4.map(b => b.dataset.skCcstart)};
  if (cc4[1]) cc4[1].click();
  out.s4.call = (calls.cc || []).slice(-1)[0];

  // ── #2 + #3：embed 面板（另一个渲染器，同一 data 属性家族）──
  const embed = document.getElementById('embedBody');
  if (embed) embed.innerHTML = '';           // 清掉可能的旧 holder，强制重渲染
  if (typeof renderCloudcliProjects === 'function') renderCloudcliProjects();
  await sleep(500);
  const rows = Array.from(document.querySelectorAll('#embedBody [data-sk-ccstart]'));
  out.s2 = {found: rows.length, onclick: rows.map(r => attr(r, 'onclick')),
            vals: rows.map(r => r.dataset.skCcstart)};
  if (rows[1]) rows[1].click();            // 点「恶意」那行（引号路径）
  out.s2.call = (calls.cc || []).slice(-1)[0];

  out.s3 = {found: 0, call: null, once: null};
  if (rows[1]) {
    const btn = rows[1].querySelector('button');
    const before = (calls.cc || []).length;
    if (btn) { btn.click(); }
    out.s3.found = btn ? 1 : 0;
    out.s3.call = (calls.cc || []).slice(-1)[0];
    out.s3.once = (calls.cc || []).length - before;   // 必须是 1（行+按钮不双触发）
  }

  // ── #5：MCP 工具行 ──
  await loadMcp();
  await sleep(200);
  const mt = document.querySelector('#mcTools [data-sk-pt-server]');
  out.s5 = {found: !!mt, onclick: attr(mt, 'onclick'),
            server: mt && mt.dataset.skPtServer, name: mt && mt.dataset.skPtName};
  if (mt) mt.click();
  // mcpToolSel 是顶层 let（不是 window 属性），只能按词法名读
  out.s5.call = (typeof mcpToolSel !== 'undefined' && mcpToolSel) ? mcpToolSel : null;

  // ── #6：⌘K 命令项 ──
  openCmd();
  await sleep(200);
  const ci = document.querySelector('#cmdList [data-sk-cmdgo]');
  out.s6 = {found: !!ci, onclick: attr(ci, 'onclick'), id: ci && ci.dataset.skCmdgo};
  if (ci) ci.click();
  out.s6.call = calls.goto || null;
  closeCmd();

  // ── 全页渲染态：不许存在「半个函数调用」的 onclick（旧 bug 的签名）──
  out.truncated = Array.from(document.querySelectorAll('[onclick]'))
    .map(e => e.getAttribute('onclick') || '')
    .filter(v => /\(\s*$/.test(v))
    .slice(0, 10);

  // 还原（同一 tab 后续不受影响）
  window.api = realApi;
  return out;
})()
""" % {"hostile": json.dumps(HOSTILE)}


def main():
    proc = launch_chrome("about:blank", CDP_PORT, "/tmp/hub_onclick_probe", 1440, 900)
    cdp = CDP(page_target(CDP_PORT))
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    cdp.send("Page.navigate", url=BASE + "/")

    if not wait_until(cdp, "typeof AGENTS !== 'undefined' && AGENTS.length > 0 && "
                           "typeof showDetail === 'function'", timeout=25):
        print("  FAIL 页面未就绪（AGENTS/showDetail 不可用）")
        print("─── 结论：FAIL（1 项）")
        return 1

    r = evaluate(cdp, SCRIPT, timeout=40)
    if not isinstance(r, dict):
        print("  FAIL 探针脚本异常（求值为空）")
        print("─── 结论：FAIL（1 项）")
        return 1

    calls = r.get("calls") or {}

    # #1
    s1 = r.get("s1") or {}
    if not s1.get("found"):
        bad("#1 未渲染出 [data-sk-verify]（详情抽屉没有「实测应答」）")
    elif s1.get("onclick") is not None:
        bad("#1 仍有 onclick=%r" % s1.get("onclick"))
    else:
        ok("#1 属性存在且 onclick 已移除（data-sk-verify=%r）" % s1.get("val"))
    if s1.get("call") and s1.get("call") == s1.get("val"):
        ok("#1 点击送达 verifyAgent(%r)" % s1.get("call"))
    else:
        bad("#1 点击未送达或参数不符：桩收到 %r / 期望 %r" % (s1.get("call"), s1.get("val")))

    # #2
    s2 = r.get("s2") or {}
    if s2.get("found") != 2:
        bad("#2 embed 面板应有 2 行，实得 %r" % s2.get("found"))
    elif any(v is not None for v in (s2.get("onclick") or [])):
        bad("#2 仍有 onclick=%r" % s2.get("onclick"))
    else:
        ok("#2 两行均无 onclick，dataset 原样读回敌意路径（含引号）")
    if s2.get("call") == HOSTILE:
        ok("#2 点击送达 cloudcliStart，路径逐字符一致")
    else:
        bad("#2 点击参数不符：桩收到 %r" % (s2.get("call"),))

    # #3
    s3 = r.get("s3") or {}
    if not s3.get("found"):
        bad("#3 行内没有「▶ 开始会话」按钮")
    elif s3.get("call") != HOSTILE:
        bad("#3 按钮点击参数不符：桩收到 %r" % (s3.get("call"),))
    elif s3.get("once") != 1:
        bad("#3 按钮点击触发 %r 次（应为 1，行+按钮不得双触发）" % s3.get("once"))
    else:
        ok("#3 按钮点击经 closest 命中行，只触发 1 次且参数正确")

    # #4
    s4 = r.get("s4") or {}
    if s4.get("found") != 2:
        bad("#4 claude 抽屉应有 2 个 data-sk-ccstart，实得 %r" % s4.get("found"))
    elif any(v is not None for v in (s4.get("onclick") or [])):
        bad("#4 仍有 onclick=%r" % s4.get("onclick"))
    elif s4.get("call") != HOSTILE:
        bad("#4 点击参数不符：桩收到 %r" % (s4.get("call"),))
    else:
        ok("#4 claude 抽屉按钮点击送达 cloudcliStart，参数逐字符一致")

    # #5
    s5 = r.get("s5") or {}
    if not s5.get("found"):
        bad("#5 未渲染出 [data-sk-pt-server]")
    elif s5.get("onclick") is not None:
        bad("#5 仍有 onclick=%r" % s5.get("onclick"))
    elif (s5.get("call") or {}).get("tool") != 'tool"1':
        bad("#5 pickTool 参数不符：%r" % (s5.get("call"),))
    else:
        ok("#5 工具行点击送达 pickTool（工具名含引号，dataset 未裂）")

    # #6
    s6 = r.get("s6") or {}
    if not s6.get("found"):
        bad("#6 ⌘K 列表没有 [data-sk-cmdgo]")
    elif s6.get("onclick") is not None:
        bad("#6 仍有 onclick=%r" % s6.get("onclick"))
    elif s6.get("call") != s6.get("id"):
        bad("#6 cmdGo 参数不符：桩收到 %r / 期望 %r" % (s6.get("call"), s6.get("id")))
    else:
        ok("#6 命令项点击送达 cmdGo(%r) → gotoChat" % s6.get("call"))

    # 全页
    trunc = r.get("truncated") or []
    if trunc:
        bad("全页仍有被截断的 onclick（以 ( 结尾）：%r" % trunc)
    else:
        ok("全页 [onclick] 无「半个函数调用」形态")

    print("─── 结论：%s（FAIL=%d）" % ("ALL-PASS" if not FAILS else "FAIL", len(FAILS)))
    try:
        cdp.close()
    finally:
        pass
    proc.terminate()
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
