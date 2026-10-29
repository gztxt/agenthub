#!/usr/bin/env python3
"""L2 闸门：09-23 自研 APP 事故（`_navHtml` TDZ）的红绿对照。

真判据（手机 22:06 自检面板原文回传）：

    JS异常 @ hub.js:1907:16 Uncaught ReferenceError: Cannot access '_navHtml'
      before initialization      栈顶: at renderNav
    initSidebar跑过(遮罩出口)=undefined   navTree子项=0   侧栏collapsed=false
    但 hub.js dec=122867 == HEAD blob 122867 字节  ⇒ 端侧执行的就是已提交代码

早期路径的门闩是 **histOpen ∈ TERM_HIST_AGENTS ∧ localStorage 有 hub.term.token**
（顶层 IIFE `histBootstrap()` → `histLoad()` → `renderNav()` → 读 `let _navHtml`）。
本机浏览器那份没有 hub.term.token ⇒ 走不到 ⇒ 这才是「浏览器正常/APP 异常」「局域网
正常/Tailscale 异常」的真判据 —— 不是缓存、不是网络、不是 IP 段。

三条本轮真实踩过的坑（都必须留在注释里，否则下次照样假绿）：
① 只注入 localStorage 就宣布绿 = **空转**：服务端 `/api/term/history` 零命中否证了它
   ⇒ 本闸门额外**主动驱动** `histLoad('claude')` 并要求服务端出现该请求。
② 红基线**必须钉具体提交**（RED_SHA），取 HEAD 的话修复一提交 HEAD 就变干净 ⇒ 静默不红。
③ 缓存改造会**打穿自己的闸门**：v0.13.11 起正确提手回 `immutable, max-age=31536000`
   ⇒ 预热导航一旦把 hub.js 缓存下来，后续导航 Chrome **连请求都不发** ⇒ Fetch 无事件
   可拦 ⇒ 红况静默变绿。故：开 `about:blank` → 装拦截 + `Network.setCacheDisabled`
   → 才做首次导航，并把「拦截命中次数 ≥1」本身做成一条判据。
"""
import base64
import re
import shutil
import subprocess
import sys
import tempfile
import time
sys.path.insert(0, "/fs/1000/ftp/技术文档/agenthub/tests")
from _cdp_min import CDP, launch_chrome, page_target     # noqa: E402

ROOT = "/fs/1000/ftp/技术文档/agenthub"
BASE, PORT = "http://127.0.0.1:3102", int(sys.argv[1]) if len(sys.argv) > 1 else 9405
RED_SHA = "8067801"          # 最后一个仍带 TDZ 的 hub.js（md5 0e6e0613）
PHONE_KEYS = {"hub.term.token": "SEED-TOKEN-FOR-ACCIDENT-PATH", "hub.hist": "claude",
              "hub.page": "chat", "hub.nav.open": "agents", "hub.chat.pick": "claude"}
DRIVE = ("(function(){try{window.histLoad('claude');return 'DRIVE-OK';}"
         "catch(e){return 'THREW: '+e.name+': '+e.message;}})()")
PANEL = "document.getElementById('diagBox') ? document.getElementById('diagBox').textContent : ''"
hits, res = [], []
mode = {"red": True}


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


def field(t, k):
    m = re.search(r"(?:^|\n)%s=([^\n]*)" % re.escape(k), t or "")
    return m.group(1).strip() if m else None


RED_SRC = subprocess.run(["git", "show", RED_SHA + ":static/hub.js"], cwd=ROOT,
                         capture_output=True, text=True).stdout
chk("红基线素材可取（钉在 %s）" % RED_SHA, RED_SRC.count("let _navHtml") == 1, "%d 字符" % len(RED_SRC))

PROFILE = tempfile.mkdtemp(prefix="hub_tdz_cold_")
proc = launch_chrome("about:blank", PORT, PROFILE, 392, 768)
time.sleep(2.0)
c = CDP(page_target(PORT))
c.send("Page.enable"); c.send("Runtime.enable")
c.send("Network.enable"); c.send("Network.setCacheDisabled", cacheDisabled=True)


def on_paused(params, cli):
    rid, url = params.get("requestId"), (params.get("request") or {}).get("url", "")
    if "static/hub.js" not in url or not mode["red"]:
        cli.send("Fetch.continueRequest", requestId=rid)
        return
    hits.append(url.split("/", 3)[-1])
    cli.send("Fetch.fulfillRequest", requestId=rid, responseCode=200,
             responseHeaders=[{"name": "content-type", "value": "text/javascript; charset=utf-8"},
                              {"name": "cache-control", "value": "no-store"}],
             body=base64.b64encode(RED_SRC.encode("utf-8")).decode("ascii"))


c.send("Fetch.enable", patterns=[{"urlPattern": "*static/hub.js*"}])
c.on_paused = lambda x: on_paused(x, c)      # ★同一条连接既 enable 又应答（session 作用域）


def load(url=BASE + "/?diag=1", tries=10):
    c.send("Page.navigate", url=url)
    prev, t = None, ""
    for _ in range(tries):
        time.sleep(1.5)
        t = c.eval(PANEL) or ""
        if "脚本实况=" in t and t == prev:
            return t
        prev = t
    return t


print("\n== 红：拦截先于首次导航，把仍带 TDZ 的版本喂进页面 ==")
# 冷启动判据：此刻还在 about:blank（该 origin 根本无权访问 localStorage）⇒ 证明
# 这是全新档案，红况看到的 hub.js 一定来自本次拦截、而非任何历史缓存。
chk("冷档案：首次导航前停在 about:blank",
    str(c.eval("location.href")).startswith("about:blank"), str(c.eval("location.href"))[:40])
c.send("Page.navigate", url=BASE + "/")          # 同上下文，只为取得 origin
time.sleep(2.0)
seeded = c.eval("Object.entries(%s).forEach(([k,v])=>localStorage.setItem(k,v));"
                "Object.keys(localStorage).filter(k=>k.indexOf('hub.')===0).sort().join(',')"
                % {k: v for k, v in PHONE_KEYS.items()})
print("  注入手机那份键:", seeded)
tr = load()
for ln in tr.splitlines():
    if ln.startswith(("JS异常", "栈顶", "navTree子项", "侧栏collapsed", "符号阶梯", "initSidebar")):
        print("   | " + ln[:98])
raw = tr.replace("\n", " ")
chk("★拦截真的命中 hub.js（红况成立的前提）", len(hits) >= 1, "命中 %d 次" % len(hits))
chk("红况复现 `_navHtml` TDZ（与手机同一条）",
    "Cannot access '_navHtml' before initialization" in raw, "")
mred = re.search(r"hub\.js:(\d+):(\d+)", tr)
chk("红况抛点 = 手机回传的 1907", bool(mred) and mred.group(1) == "1907",
    "实测 hub.js:%s" % (mred.group(1) if mred else "?"))
chk("红况菜单空白（navTree子项=0）", field(tr, "navTree子项") == "0", field(tr, "navTree子项") or "")
chk("红况 initSidebar 从未跑过", field(tr, "initSidebar跑过(遮罩出口)") == "undefined",
    field(tr, "initSidebar跑过(遮罩出口)") or "")
# 判据只认「阶梯里出现了 :0」，**不钉具体符号名**：原版写死「缺 syncOverlayMask」，而那份
# 清单本身会随重构过期（v0.13.179 前的 syncOverlayMask/tick/loadAgents 已非顶层函数），
# 钉名字的判据会在清单过期的同一天变成误报。红基线（RED_SHA）里 mdInline/settingsDelegates/
# termTouchBind/bbRender 都不存在 ⇒ 仍有 :0，「探针能发现缺失符号」这件事被如实验证。
chk("红况阶梯检出缺失符号（有 :0）", ":0" in (field(tr, "符号阶梯") or ""),
    (field(tr, "符号阶梯") or "")[:80])
chk("红况抽屉停在展开态（collapsed=false ⇒ 遮正文）", field(tr, "侧栏collapsed") == "false",
    field(tr, "侧栏collapsed") or "")

print("\n== 绿：撤掉拦截，用磁盘上修好的产物重跑同一事故路径 ==")
mode["red"] = False
tg = load(BASE + "/?diag=1&green=1")
rawg = tg.replace("\n", " ")
chk("绿况无任何 JS 异常", "JS异常" not in rawg and "_navHtml" not in rawg, "")
chk("绿况 initSidebar 跑过（遮罩出口=function）",
    field(tg, "initSidebar跑过(遮罩出口)") == "function", field(tg, "initSidebar跑过(遮罩出口)") or "")
chk("绿况菜单已渲染", (field(tg, "navTree子项") or "0") != "0", "子项=%s" % field(tg, "navTree子项"))
chk("绿况符号阶梯全 1", ":0" not in (field(tg, "符号阶梯") or ""), (field(tg, "符号阶梯") or "")[:80])
chk("绿况窄屏侧栏收起", field(tg, "侧栏collapsed") == "true", field(tg, "侧栏collapsed") or "")
chk("绿况 hub.js 提手为 8 位内容哈希",
    bool(re.match(r"/static/hub\.js\?v=[0-9a-f]{8}$", field(tg, "hub.js标签") or "")),
    field(tg, "hub.js标签") or "")
drv = c.eval(DRIVE)
print("  主动驱动事故路径:", drv)
chk("★主动调 histLoad('claude') 不抛（路径确被执行过）", drv == "DRIVE-OK", str(drv))
n = subprocess.run(["bash", "-c",
                    "journalctl --user -u agenthub.service --since '-3 min' --no-pager -o cat "
                    "| grep -c 'term/history' || true"], capture_output=True, text=True).stdout.strip()
chk("服务端确有 /api/term/history 命中（不靠面板自证）", n not in ("", "0"), "近 3 分钟命中=%s" % n)

c.send("Fetch.disable")
proc.kill()
shutil.rmtree(PROFILE, ignore_errors=True)
bad = res.count(False)
print("\n%s  (%d/%d)" % ("全部 PASS ✅" if not bad else "存在 FAIL ❌", res.count(True), len(res)))
sys.exit(1 if bad else 0)
