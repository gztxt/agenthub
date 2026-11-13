#!/usr/bin/env python3
"""L2 闸门：窄屏**无存档**冷启动首屏必须是「48px 图标条」（用户 09-24 裁定）。

判据（可断言，不接受"看了截图"）：
  A 绿 —— 冷 profile + 清 localStorage + 窄档(390x768) 首屏：
        `#sidebar` 含 `collapsed` 类 **且** 实测 offsetWidth == 48 **且** 无可见文字标签
  B 灵敏度对照 1 —— 本档显式存过偏好 `hub.sidebar.narrow='0'` ⇒ collapsed 必须为 false
        （若探针 stuck-true，这一格会红。证明 A 真的在读活的 DOM）
  C 灵敏度对照 2（红）—— 掐断 /static/hub.js（JS 死亡）⇒ collapsed 必须为 false
        （证明本闸门**能够**判红，不是空转）

CDP 纪律：冷 profile 用 tempfile.mkdtemp()；开 Network.setCacheDisabled（09-23 的
"端侧缓存旧体"教训）；挂 on_event 自动应答原生对话框（termToken() 的 prompt 会挂死
renderer，实测栽过）。
"""
import json
import sys
import shutil
import tempfile
import time

sys.path.insert(0, "/fs/1000/ftp/技术文档/agenthub/tests")
from _cdp_min import CDP, launch_chrome, page_target
from _hub_inject import inject_hub

BASE = "http://127.0.0.1:3102"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9417
W, H = 390, 768
ICONBAR_W = 48          # .sidebar.collapsed{width:48px}（index.html:183）
res = []


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


STATE_JS = r"""JSON.stringify((function(){
  var sb = document.getElementById('sidebar');
  var vis = 0, lbls = sb ? sb.querySelectorAll('.lbl') : [];
  for (var i = 0; i < lbls.length; i++) { var b = lbls[i].getBoundingClientRect();
    if (b.width > 2 && b.height > 2) vis++; }
  var cs = sb ? getComputedStyle(sb) : {};
  return {narrow: matchMedia('(max-width: 767px)').matches,
          collapsed: sb ? sb.classList.contains('collapsed') : null,
          w: sb ? Math.round(sb.getBoundingClientRect().width) : null,
          offsetW: sb ? sb.offsetWidth : null,
          pos: cs.position || null,
          visibleLbls: vis,
          sidebarNarrowStore: localStorage.getItem('hub.sidebar.narrow'),
          legacyStore: localStorage.getItem('hub.sidebar'),
          keys: Object.keys(localStorage).sort().join(',')};
})())"""


def st(c):
    try:
        return json.loads(c.eval(STATE_JS) or "{}")
    except Exception as e:
        return {"_err": str(e)}


def main_paused(params, cli, state):
    """只掐 hub.js，其余放行（否则会挂住整页）。同一条连接 enable + 应答。"""
    rid = params.get("requestId")
    url = (params.get("request") or {}).get("url", "")
    try:
        if "static/hub.js" in url:
            state["blocked"] += 1
            cli.send("Fetch.failRequest", requestId=rid, errorReason="Failed")
        else:
            cli.send("Fetch.continueRequest", requestId=rid)
    except Exception as e:
        print("   拦截应答异常:", e)


profile = tempfile.mkdtemp(prefix="hub_narrow_iconbar_")
print("冷 profile：%s" % profile)
proc = launch_chrome(BASE + "/", PORT, profile, W, H)
time.sleep(2.5)
c = CDP(page_target(PORT))


def on_evt(method, params):
    if method == "Page.javascriptDialogOpening":
        try:
            c.send("Page.handleJavaScriptDialog", accept=False)
        except Exception:
            pass


c.on_event = on_evt          # 先挂钩子再 enable
c.send("Page.enable")
c.send("Runtime.enable")
c.send("Network.enable")
c.send("Network.setCacheDisabled", cacheDisabled=True)
c.send("Emulation.setDeviceMetricsOverride", width=W, height=H,
       deviceScaleFactor=2, mobile=True)


def nav(url, wait=3.5):
    c.send("Page.navigate", url=url)
    time.sleep(wait)
    inject_hub(c)          # 登录闸适配（2026-11-13）：导航后重新注入 hub.js；jsdead 阶段的注入会被 Fetch 掐断，blocked 语义不变


def jsdead(url, wait=5.0):
    state = {"blocked": 0}
    c.send("Fetch.enable", patterns=[{"urlPattern": "*static/hub.js*"}])
    c.on_paused = lambda p: main_paused(p, c, state)
    nav(url, wait)
    c.send("Fetch.disable")
    c.on_paused = None
    return state["blocked"]


print("\n== A 绿：窄档 390x768 冷启动、无存档 → 必须是图标条 ==")
nav(BASE + "/")
c.eval("localStorage.clear()")          # 显式清存档（首屏默认态不得依赖任何 origin 存量）
nav(BASE + "/")
a = st(c)
print("   实测：%s" % json.dumps(a, ensure_ascii=False))
chk("A 视口是窄档", a.get("narrow") is True, "narrow=%s 视口=%dx%d" % (a.get("narrow"), W, H))
chk("A 首屏无侧栏存档", a.get("sidebarNarrowStore") is None,
    "hub.sidebar.narrow=%r legacy=%r" % (a.get("sidebarNarrowStore"), a.get("legacyStore")))
chk("A #sidebar 含 collapsed 类", a.get("collapsed") is True, "collapsed=%s" % a.get("collapsed"))
chk("A 实测宽度 == 图标条 48px", a.get("offsetW") == ICONBAR_W, "offsetWidth=%s" % a.get("offsetW"))
chk("A 无可见文字标签", a.get("visibleLbls") == 0, "可见 .lbl=%s" % a.get("visibleLbls"))

print("\n== B 灵敏度对照 1：本档存过 hub.sidebar.narrow='0' → 必须展开 ==")
c.eval("localStorage.setItem('hub.sidebar.narrow','0')")
nav(BASE + "/")
b = st(c)
print("   实测：%s" % json.dumps(b, ensure_ascii=False))
chk("B 存档被读到", b.get("sidebarNarrowStore") == "0", "store=%r" % b.get("sidebarNarrowStore"))
chk("B collapsed 变 false（探针非 stuck-true）",
    b.get("collapsed") is False, "collapsed=%s w=%s" % (b.get("collapsed"), b.get("offsetW")))

print("\n== C 灵敏度对照 2（红）：掐断 hub.js → collapsed 必须为 false ==")
c.eval("localStorage.clear()")
nblock = jsdead(BASE + "/?deadcheck=1")
d = st(c)
print("   实测：%s（拦截 hub.js %d 次）" % (json.dumps(d, ensure_ascii=False), nblock))
chk("C 确实拦到 hub.js", nblock >= 1, "blocked=%d" % nblock)
chk("C collapsed 变 false（闸门能判红）",
    d.get("collapsed") is False, "collapsed=%s w=%s" % (d.get("collapsed"), d.get("offsetW")))

proc.kill()
shutil.rmtree(profile, ignore_errors=True)

bad = res.count(False)
print("\n===== 窄档首屏默认态判定 =====")
print("A 无存档 collapsed = %s   B 存'0' collapsed = %s   C JS死 collapsed = %s"
      % (a.get("collapsed"), b.get("collapsed"), d.get("collapsed")))
print("%s  (%d/%d)" % ("窄档首屏=图标条 成立 ✅" if not bad else "存在 FAIL ❌",
                       res.count(True), len(res)))
sys.exit(1 if bad else 0)
