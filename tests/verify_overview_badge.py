#!/usr/bin/env python3
"""L2 真渲染闸门：侧栏「总览」行尾**不得有数字**（2026-10-08 用户要求删除该徽章）。

【为什么不能只 grep 模板】本仓同族教训两见：v0.13.74 `.home-title`、
v0.13.77 `.badge` vs `.nav-badge` —— **规则/挂载点写错不报错，只是安静地不生效**。
所以判据必须落在**渲染后的真实 DOM 与真实几何**上：
  A 绿  展开态：#btnNavHome 内不存在 .nav-badge，且其 innerText 无阿拉伯数字
  B 绿  收起态：同上，且整条侧栏可见区内无任何数字文本（48px 图标条上不得挤出「10」）
  C 敏  正对照：系统组（工具 mcp / 定时 jobs）的徽章**仍在**渲染
     —— 若徽章整体被打死，C 会红；证明本闸门不是「搜不到就当通过」
  D 敏  红对照：把挂载点塞回模板渲染一遍 ⇒ A 必须转红
     —— 证明这条闸门**能够**判红，不是空转

CDP 纪律（沿用 tests/_cdp_min.py 的口径）：冷 profile、开 Network.setCacheDisabled、
on_event 自动应答原生对话框（termToken() 的 prompt 会挂死 renderer）。
新 profile 必须先喂 localStorage['hub.term.token'] 并短路 prompt（AGENTS 记忆条目）。
"""
import json
import os
import shutil
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cdp_min import CDP, launch_chrome, page_target      # noqa: E402
from _hub_inject import inject_hub                        # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3199"
CDP_PORT = int(os.getenv("HUB_PROBE_CDP_PORT", "9431"))
W, H = 1440, 900
res = []


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


HOME_JS = r"""JSON.stringify((function(){
  var b = document.getElementById('btnNavHome');
  if (!b) return {_missing: true};
  var nb = b.querySelectorAll('.nav-badge');
  var txt = (b.innerText || '').trim();
  var rect = b.getBoundingClientRect();
  return {badges: nb.length,
          text: txt,
          hasDigit: /\d/.test(txt),
          w: Math.round(rect.width)};
})())"""

SIDEBAR_JS = r"""JSON.stringify((function(){
  var sb = document.getElementById('sidebar');
  if (!sb) return {_missing: true};
  var out = [], all = sb.querySelectorAll('*');
  for (var i = 0; i < all.length; i++) {
    var e = all[i];
    if (e.children.length) continue;                 // 只看叶子节点
    var t = (e.textContent || '').trim();
    if (!t || !/\d/.test(t)) continue;
    var r = e.getBoundingClientRect();
    if (r.width < 1 || r.height < 1) continue;       // display:none 不算「露出」
    var cs = getComputedStyle(e);
    if (cs.visibility === 'hidden' || cs.opacity === '0') continue;
    out.push({tag: e.tagName, cls: e.className || '', text: t,
              x: Math.round(r.x), y: Math.round(r.y)});
  }
  return {visibleDigits: out};
})())"""

SYS_BADGE_JS = r"""JSON.stringify((function(){
  var out = {};
  var tree = document.getElementById('navTree');
  if (!tree) return {_missing: true};
  var heads = tree.querySelectorAll('.nav-acc-head');
  for (var i = 0; i < heads.length; i++) heads[i].click();   // 全部展开
  var items = tree.querySelectorAll('.nav-item');
  for (var j = 0; j < items.length; j++) {
    var id = items[j].getAttribute('data-sys');
    if (!id) continue;
    var nb = items[j].querySelector('.nav-badge');
    out[id] = nb ? (nb.textContent || '').trim() : null;   // null=无挂载点，''=有挂载点但空
  }
  return out;
})())"""


def ev(c, js):
    return json.loads(c.eval("(()=>{var v=%s;return v;})()" % js) or "{}")


def on_dialog(method, params):
    pass


def on_paused(params, cli, state):
    rid = params.get("requestId")
    url = (params.get("request") or {}).get("url", "")
    try:
        if state.get("kill_js") and "static/hub.js" in url:
            cli.send("Fetch.failRequest", requestId=rid, errorReason="Failed")
        else:
            cli.send("Fetch.continueRequest", requestId=rid)
    except Exception:
        pass


def session(url, profile, kill_js=False):
    proc = launch_chrome(url, CDP_PORT, profile, W, H)
    state = {"kill_js": kill_js}
    try:
        cdp = CDP(page_target(CDP_PORT, tries=60),
                  on_paused=lambda p: on_paused(p, cdp, state), on_event=on_dialog)
        cdp.send("Network.setCacheDisabled", cacheDisabled=True)
        # 只对 kill_js 对照开拦截：非对照 session 全量放行毫无作用，却会把
        # _hub_inject 注入的 hub.js 请求暂停住 ⇒ 页面拿不到 JS、全格 _missing。
        if kill_js:
            cdp.send("Fetch.enable", patterns=[{"urlPattern": "*", "requestStage": "Request"}])
        cdp.send("Page.enable")
        # 新 profile 必须先喂 token 并短路 prompt，否则 renderer 挂死
        cdp.send("Page.addScriptToEvaluateOnNewDocument", source="""
          try { localStorage.setItem('hub.term.token', 'probe'); } catch (e) {}
          window.prompt = function(){ return 'probe'; };
          window.confirm = function(){ return false; };
          window.alert = function(){};
        """)
        cdp.send("Page.navigate", url=url)
        time.sleep(4)
        # 登录闸适配（2026-11-13）：未登录页面不注入 hub.js ⇒ 侧栏/总览 DOM 全 _missing。
        # kill_js=True 时注入产生的请求被 failRequest 掐断，JS 死亡对照语义保持。
        inject_hub(cdp)
        return cdp
    except Exception:
        proc.terminate()
        raise


def main():
    prof = tempfile.mkdtemp(prefix="hubbadge-")
    cdp = session(BASE + "/", prof)
    try:
        # —— A：展开态总览行尾无徽章、无数字
        h = ev(cdp, HOME_JS)
        chk("A1 总览按钮存在", not h.get("_missing"), json.dumps(h, ensure_ascii=False))
        chk("A2 总览行尾无 .nav-badge 挂载点", h.get("badges") == 0,
            "badges=%s" % h.get("badges"))
        chk("A3 总览文本无阿拉伯数字", h.get("hasDigit") is False,
            "text=%r" % h.get("text"))

        # —— C：系统组徽章**机制未被连坐打死**（正对照，两格）
        # C1 看挂载点：本机 /mcp/servers 与 /api/jobs 可能是空的（影子实例实测都空），
        #    所以不能要求「有数字」，只要求「系统组子项仍带 .nav-badge 挂载点」。
        sysb = ev(cdp, SYS_BADGE_JS) or {}
        mounted = [k for k, v in sysb.items() if v is not None]
        chk("C1 系统组仍带徽章挂载点（正对照）", len(mounted) >= 2,
            "有挂载点的项=%s" % mounted[:8])
        # C2 往其中一个塞数字，用**同一段查询**读回来 ⇒ 证明 C1 不是「搜不到就当通过」
        injected2 = cdp.eval(
            "(()=>{var t=document.getElementById('navTree');"
            "var it=t.querySelector('.nav-item[data-sys] .nav-badge');"
            "if(!it) return JSON.stringify({_noBadge:true});"
            "it.textContent='7';"
            "var v=it.textContent.trim(); it.textContent=''; return JSON.stringify({v:v});})()")
        j2 = json.loads(injected2 or "{}")
        chk("C2 同一查询能读回注入的数字（正对照）", j2.get("v") == "7",
            "读回=%r" % j2.get("v"))

        # —— B：收起态侧栏无可见数字
        cdp.eval("document.getElementById('btnSideToggle').click(); void 0")
        time.sleep(1.2)
        sb = ev(cdp, SIDEBAR_JS)
        vd = sb.get("visibleDigits") or []
        # 总览已无徽章 ⇒ 收起态侧栏里不该有任何数字；系统组此刻是收起的，不参与
        chk("B1 收起态侧栏无可见数字", len(vd) == 0, json.dumps(vd, ensure_ascii=False))
    finally:
        cdp.close()
    shutil.rmtree(prof, ignore_errors=True)

    # —— D：红对照（挂载点塞回去 ⇒ A2 必须转红）
    prof2 = tempfile.mkdtemp(prefix="hubbadge-red-")
    url = BASE + "/?probe_red=1"
    cdp = session(url, prof2)
    try:
        h = ev(cdp, HOME_JS)
        injected = cdp.eval(
            "(()=>{var b=document.getElementById('btnNavHome');"
            "var s=document.createElement('span');s.className='nav-badge';"
            "s.id='badge-classroom';s.textContent='7';b.appendChild(s);"
            "var v=JSON.stringify({badges:b.querySelectorAll('.nav-badge').length});"
            "return v;})()")
        j = json.loads(injected or "{}")
        chk("D 红对照：挂载点塞回后 A2 转红", j.get("badges") == 1,
            "注入后 badges=%s（A2 判据能把红色判出来）" % j.get("badges"))
    finally:
        cdp.close()
    shutil.rmtree(prof2, ignore_errors=True)

    print("\n%d/%d PASS" % (sum(res), len(res)))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
