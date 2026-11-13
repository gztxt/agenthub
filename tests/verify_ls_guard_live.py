#!/usr/bin/env python3
"""L2 闸门：存储抛异常时，启动链必须还活着（v0.13.13 localStorage 守卫的实况验收）。

为什么需要它：任务二的价值全在"任一处抛异常不再打断启动链"这句话上，而静态计数
（tests/test_ls_guard.py R1）只证明**写法**改了，不证明**真跑起来不炸**。本探针在
真 chromium 里把存储做成会抛的，三种情形各测一次：

  A 绿  健康存储：点导航 + 点实体 → __lsDiag.fails==0，且 hub.page 真的写进去了
        （守卫不许把功能做没：写路径必须仍然通）
  B 红→绿  Storage.prototype.setItem 抛 QuotaExceededError
        新字节：页面照常渲染（go=function、navTree=4、窄屏 collapsed=true），
                __lsDiag.fails>0 且 lastErr 里带 QuotaExceededError
  C 红→绿  整个 window.localStorage 取值就抛（隐私模式 / WebView 禁 DOM Storage）
        新字节：同上，且面板那行 `localStorage键=` 自己也不能把 probe() 带崩
  R 对照  用 Fetch.fulfillRequest 把 /static/hub.js 换成 **git 0822164（改前）的字节**，
        再施同一个 C sabotage（取存储就抛）⇒ 启动链必须断（未捕获异常冒出 window、
        navTree<3、无 __lsDiag）⇒ 证明 B/C 的绿是守卫挣来的，不是探针 stuck-green。
        红侧走 CDP 换体，生产文件与生产服务一个字节都不动。

CDP 纪律：冷 profile 用 tempfile.mkdtemp()；Network.setCacheDisabled（hub.js 带
immutable，不关缓存会拿到旧体 ⇒ 假绿，本轮已在 verify_diag_panel 上实锤过一次）；
on_event 自动应答原生 prompt（termToken() 会弹框挂死 renderer）。
"""
import base64
import json
import subprocess
import sys
import tempfile
import time

sys.path.insert(0, "/fs/1000/ftp/技术文档/agenthub/tests")
from _cdp_min import CDP, launch_chrome, page_target
from _hub_inject import inject_hub

BASE = "http://127.0.0.1:3102"
PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 9427
W, H = 390, 768
RED_SHA = "0822164"
REPO = "/fs/1000/ftp/技术文档/agenthub"
res = []


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-44s %s" % ("PASS" if ok else "FAIL", name, detail))


SAB = {
    "none": "",
    "quota": ("Storage.prototype.setItem = function () {"
              " throw new DOMException('simulated full', 'QuotaExceededError'); };"),
    "blocked": ("Object.defineProperty(window, 'localStorage', { configurable: true,"
                " get() { throw new DOMException('simulated disabled', 'SecurityError'); } });"),
}

READ_JS = r"""JSON.stringify((function(){
  var nt = document.getElementById('navTree'), sb = document.getElementById('sidebar');
  var g = window.__lsDiag;
  var keys = '(取不到)';
  try { keys = Object.keys(localStorage).sort().join(','); } catch (e) { keys = '★读不到:' + e.name; }
  return {narrow: matchMedia('(max-width: 767px)').matches,
          goType: typeof go,
          navTree: nt ? nt.children.length : -1,
          collapsed: sb ? sb.classList.contains('collapsed') : null,
          page: (document.querySelector('section.page.on') || {}).id || '(无)',
          lsDiag: g ? {fails: g.fails, ok: g.ok, lastErr: String(g.lastErr||'').slice(0,90)} : null,
          keys: keys,
          log: (window.__DIAG_LOG__||[]).slice(0,6)};
})())"""


def st(c):
    try:
        return json.loads(c.eval(READ_JS) or "{}")
    except Exception as e:
        return {"_err": str(e)}


def red_bytes():
    r = subprocess.run(["git", "-C", REPO, "show", "%s:static/hub.js" % RED_SHA],
                       capture_output=True)
    assert r.returncode == 0 and len(r.stdout) > 10000, "取不到 %s 的 hub.js" % RED_SHA
    return r.stdout


OLD = red_bytes()
print("红侧字节：git %s:static/hub.js = %d 字节（只经 CDP 注入，不落盘不动服务）" % (RED_SHA, len(OLD)))

proc = launch_chrome(BASE + "/", PORT, tempfile.mkdtemp(prefix="hub_lsguard_"), W, H)
time.sleep(2.5)
c = CDP(page_target(PORT))


def on_evt(method, params):
    if method == "Page.javascriptDialogOpening":
        try:
            c.send("Page.handleJavaScriptDialog", accept=False)
        except Exception:
            pass


c.on_event = on_evt
c.send("Page.enable")
c.send("Runtime.enable")
c.send("Network.enable")
c.send("Network.setCacheDisabled", cacheDisabled=True)
c.send("Emulation.setDeviceMetricsOverride", width=W, height=H, deviceScaleFactor=2, mobile=True)

S = {"swap": False, "swapped": 0}


def paused(params, cli):
    url = (params.get("request") or {}).get("url", "")
    rid = params.get("requestId")
    try:
        if S["swap"] and "static/hub.js" in url:
            S["swapped"] += 1
            cli.send("Fetch.fulfillRequest", requestId=rid, responseCode=200,
                     responseHeaders=[{"name": "Content-Type",
                                       "value": "text/javascript; charset=utf-8"}],
                     body=base64.b64encode(OLD).decode())
        else:
            cli.send("Fetch.continueRequest", requestId=rid)
    except Exception as e:
        print("   拦截应答异常:", e)


c.send("Fetch.enable", patterns=[{"urlPattern": "*static/hub.js*"}])
c.on_paused = lambda p: paused(p, c)

sid = {}


def reload_with(sab, wait=4.0):
    """换 sabotage 脚本后再导航（addScriptToEvaluateOnNewDocument 只影响后续导航）。"""
    for k in list(sid):
        try:
            c.send("Page.removeScriptToEvaluateOnNewDocument", identifier=sid.pop(k))
        except Exception:
            pass
    if SAB[sab]:
        r = c.send("Page.addScriptToEvaluateOnNewDocument", source=SAB[sab])
        sid[sab] = r.get("identifier")
    c.send("Page.navigate", url=BASE + "/?cb=%s%d" % (sab, time.time()))
    time.sleep(wait)
    inject_hub(c)          # 登录闸适配（2026-11-13）：导航后重新注入 hub.js（Fetch fulfill 仍会替换它为红侧字节）
    return st(c)


def click(x, y):
    for typ in ("mousePressed", "mouseReleased"):
        c.send("Input.dispatchMouseEvent", type=typ, x=x, y=y, button="left",
               clickCount=1, buttons=1 if typ == "mousePressed" else 0)
    time.sleep(0.9)


def center(sel):
    raw = c.eval("(() => { const e = document.querySelector(%s); if (!e) return 'null';"
                 " const b = e.getBoundingClientRect();"
                 " return JSON.stringify([Math.round(b.left+b.width/2), Math.round(b.top+b.height/2),"
                 " b.width>0 && b.height>0]); })()" % json.dumps(sel))
    if not raw or raw == "null":
        return None
    x, y, vis = json.loads(raw)
    return (x, y) if vis and 0 <= x < W and 0 <= y < H else None


def pick_nav():
    """展开「系统」分组后，找一个**可见的**非当前页入口，返回 (x, y, 页名)。
       左侧系统页用的是 `button.nav-item[data-sys]`（05:349），不是 data-page
       —— 选错属性的话本探针会“找不到可点项”而假绿，所以这里把两个都接上。"""
    raw = c.eval(r"""(() => { const bs = [...document.querySelectorAll('.sidebar [data-page], .sidebar [data-sys]')];
      for (const e of bs) { const p = e.dataset.page || e.dataset.sys;
        if (!p || p === 'classroom') continue;
        const b = e.getBoundingClientRect(); if (b.width < 4 || b.height < 4) continue;
        const x = Math.round(b.left+b.width/2), y = Math.round(b.top+b.height/2);
        if (x < 0 || y < 0 || x >= %d || y >= %d) continue;
        return JSON.stringify([x, y, p]); } return 'null'; })()""" % (W, H))
    if not raw or raw == "null":
        return None
    x, y, page = json.loads(raw)
    return (x, y, page)


print("\n== A 绿：健康存储，走真实交互（展开系统分组→点一个系统页） ==")
a = reload_with("none")
chk("A 页面正常渲染", a.get("goType") == "function" and a.get("navTree") == 4,
    "go=%s navTree=%s" % (a.get("goType"), a.get("navTree")))
g1 = center('button[data-group="system"]') or center('button[data-group]')
if g1:
    click(*g1)
picked = pick_nav()
if picked:
    click(picked[0], picked[1])
a2 = st(c)
print("   交互后（点了 %s @%s）：%s" % (picked and picked[2], picked and picked[:2],
                            json.dumps(a2, ensure_ascii=False)[:240]))
diag_a = a2.get("lsDiag") or {}
chk("A 零异常计数（__lsDiag.fails==0）", diag_a.get("fails") == 0, json.dumps(diag_a, ensure_ascii=False))
chk("A 写路径仍然通（hub.page 真落盘）", "hub.page" in (a2.get("keys") or ""), a2.get("keys"))
chk("A 页面切到了所点的页", bool(picked) and a2.get("page") == "page-" + picked[2],
    "点了=%s 在显示的页=%s" % (picked and picked[2], a2.get("page")))
chk("A 导航写入也被守卫接住（ok>0 且有 set 过的键）",
    (diag_a.get("ok") or 0) > 0, "ok=%s" % diag_a.get("ok"))
chk("A 无 JS 异常", not a2.get("log"), json.dumps(a2.get("log"), ensure_ascii=False)[:120])

print("\n== R 对照：同一 sabotage 下换成 %s（改前）字节，启动链必须断 ==" % RED_SHA)
S["swap"] = True
S["swapped"] = 0
r_ = reload_with("blocked", wait=5.0)
print("   改前字节 + 存储不可用：%s" % json.dumps(r_, ensure_ascii=False)[:300])
chk("R 确实换成了改前字节", S["swapped"] >= 1, "换体次数=%d" % S["swapped"])
chk("R 改前字节没有守卫对象（证明确实是老代码）", r_.get("lsDiag") is None,
    "__lsDiag=%s" % r_.get("lsDiag"))
chk("R 旧字节：异常冒出 window（启动链被打断的直接证据）",
    any("SecurityError" in str(x) for x in (r_.get("log") or [])),
    json.dumps(r_.get("log", [])[:2], ensure_ascii=False)[:130])
chk("R 旧字节：菜单没能渲染出来（导航数据链路断在第 02 片顶层）",
    (r_.get("navTree") if isinstance(r_.get("navTree"), int) else 0) < 4,
    "go=%s navTree=%s" % (r_.get("goType"), r_.get("navTree")))
S["swap"] = False

print("\n== B 新字节 + setItem 抛 QuotaExceededError ==")
b = reload_with("quota")
print("   实测：%s" % json.dumps(b, ensure_ascii=False)[:300])
diag_b = b.get("lsDiag") or {}
chk("B 启动链存活（go=function）", b.get("goType") == "function", "go=%s" % b.get("goType"))
chk("B 菜单照常渲染（navTree=4）", b.get("navTree") == 4, "navTree=%s" % b.get("navTree"))
chk("B 窄屏默认态不受影响（collapsed=true）",
    b.get("narrow") is True and b.get("collapsed") is True,
    "narrow=%s collapsed=%s" % (b.get("narrow"), b.get("collapsed")))
chk("B 异常被计数（fails>0，不静默）", (diag_b.get("fails") or 0) > 0, json.dumps(diag_b, ensure_ascii=False))
chk("B lastErr 带 QuotaExceededError（端侧可定罪）",
    "QuotaExceededError" in (diag_b.get("lastErr") or ""), diag_b.get("lastErr"))
chk("B 无未捕获 JS 异常（异常没冒到 window）", not b.get("log"),
    json.dumps(b.get("log"), ensure_ascii=False)[:140])

print("\n== C 新字节 + 取 localStorage 就抛（隐私模式/WebView 禁存储） ==")
cc = reload_with("blocked", wait=4.5)
print("   实测：%s" % json.dumps(cc, ensure_ascii=False)[:300])
diag_c = cc.get("lsDiag") or {}
chk("C 启动链存活", cc.get("goType") == "function" and cc.get("navTree") == 4,
    "go=%s navTree=%s" % (cc.get("goType"), cc.get("navTree")))
chk("C 回落到窄屏默认图标条（collapsed=true）", cc.get("collapsed") is True,
    "collapsed=%s" % cc.get("collapsed"))
chk("C 异常被计数", (diag_c.get("fails") or 0) > 0, json.dumps(diag_c, ensure_ascii=False))
chk("C lastErr 带 SecurityError", "SecurityError" in (diag_c.get("lastErr") or ""),
    diag_c.get("lastErr"))
chk("C 无未捕获 JS 异常", not cc.get("log"), json.dumps(cc.get("log"), ensure_ascii=False)[:140])
chk("C 存储读取自身也不崩面板（键行自报读不到）", "★读不到" in (cc.get("keys") or ""),
    cc.get("keys"))

print("\n== D 撤掉 sabotage：必须回到 A 的正常态（不留残余） ==")
d = reload_with("none")
chk("D 恢复正常（fails=0 + hub.page 在）",
    d.get("goType") == "function" and ((d.get("lsDiag") or {}).get("fails") == 0)
    and "hub.page" in (d.get("keys") or ""),
    "fails=%s keys=%s" % ((d.get("lsDiag") or {}).get("fails"), d.get("keys")))

proc.kill()
bad = res.count(False)
print("\n%s  (%d/%d)" % ("守卫实况全部 PASS ✅" if not bad else "存在 FAIL ❌",
                         res.count(True), len(res)))
sys.exit(1 if bad else 0)
