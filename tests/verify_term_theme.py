"""v0.13.3 终端配色端到端渲染验证（CDP / headless chromium；只读探针，不改任何状态）。

跑法：cd ~/agent-hub && venv/bin/python tests/verify_term_theme.py
      环境变量：HUB_BASE（默认 http://127.0.0.1:3199，即影子实例）、CDP_PORT（默认 9333）
逐条打 PASS/FAIL；退出码非 0 = 有 FAIL。

为什么必须开浏览器，而不是 curl 一下 CSS 就算验过：
  配色真值源是 CSS token，但**消费方是 xterm.js 实例的 theme**（字与底都画在 canvas 上）。
  curl 只能证明 token 文本在场，证明不了 ensureTerm() 把 token 真读进了 theme。
  两个踩过的坑（别再照抄）：
    - 读 `.xterm` 元素的 computed color 会拿到 rgb(17,17,17)——那是全站 --text-1:#111111
      的**继承色**，与终端前景无关；终端真色只能问 `term.options.theme`。
    - `T()` 是 ensureTerm 内的局部箭头函数，全局取不到；要在页面里验兜底就调顶层
      `cssToken()`。
  正确判据两条：真容器 `#termEl` 的 computed background，和 `term.options.theme` 实值。
"""
import json
import os
import subprocess
import sys
import time
import urllib.request
from urllib.parse import quote as _q

PORT = int(os.getenv("CDP_PORT", "9333"))
BASE = os.getenv("HUB_BASE", "http://127.0.0.1:3199")   # 默认影子；打生产需显式覆盖
sys.path.insert(0, "/fs/1000/ftp/技术文档/agenthub/tests")
from _hub_inject import inject_hub   # noqa: E402
FAILS = []

# 期望深底板：templates/index.html 的 --term-* token（2026-09-22 用户裁定，参考 grok 原生观感）
DARK = {
    "bg": "#000000", "fg": "#ffffff", "cursor": "#ffffff", "sel": "#b0d0ff40",
    "black": "#7f7f7f", "red": "#cd3131", "green": "#0dbc79", "white": "#e5e5e5",
    "bwhite": "#ffffff",
}
# 09-22 之前的浅底板，任一出现即回归
LIGHT = {"#fcfcfd", "#24272b", "#2b2f33", "#4a4f55", "#a3342c", "#2f7a4f", "#767d85"}

TOKEN_JS = ("(() => {const s=getComputedStyle(document.documentElement);"
            "const g=n=>s.getPropertyValue(n).trim();return JSON.stringify({"
            + ",".join("'" + k + "':g('--term-" + k + "')" for k in DARK)
            + "});})()")

TERM_EL_JS = ("(() => {const el=document.querySelector('#termEl');"
              "if(!el) return JSON.stringify({ok:false,why:'no #termEl (structure changed?)'});"
              "return JSON.stringify({ok:true,bg:getComputedStyle(el).backgroundColor});})()")

ENSURE_JS = ("(() => {try{"
             "if(typeof ensureTerm!=='function') return JSON.stringify({ok:false,why:'ensureTerm not global'});"
             "ensureTerm();"
             "if(typeof term==='undefined'||!term) return JSON.stringify({ok:false,why:'term instance missing'});"
             "const th=term.options.theme||{};"
             "return JSON.stringify({ok:true,bg:th.background,fg:th.foreground,cur:th.cursor,"
             "sel:th.selectionBackground,black:th.black,red:th.red,green:th.green,white:th.white,"
             "fs:term.options.fontSize,ff:String(term.options.fontFamily||'').slice(0,30)});"
             "}catch(e){return JSON.stringify({ok:false,why:'EXC '+e.message});}})()")

FALLBACK_JS = ("(() => {try{return JSON.stringify({"
               "miss:cssToken('--term-__no_such__','DEEP#000000'),"
               "real:cssToken('--term-bg','#LIGHT-BUG')});"
               "}catch(e){return JSON.stringify({err:e.message});}})()")

READY_JS = ("JSON.stringify({rs:document.readyState,sheets:document.styleSheets.length,"
            "title:document.title,hasEnsure:typeof ensureTerm})")


def check(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + (" :: " + str(detail) if detail else ""))
    if not ok:
        FAILS.append(name)


def http_get(path, timeout=8):
    with urllib.request.urlopen(BASE + path, timeout=timeout) as r:
        return r.read().decode("utf-8", "replace")


def cdp_http(path):
    url = "http://127.0.0.1:%d%s" % (PORT, path)
    req = urllib.request.Request(url, method="PUT" if "/json/new" in path else "GET")
    with urllib.request.urlopen(req, timeout=8) as r:
        return json.loads(r.read().decode())


def main():
    from websockets.sync.client import connect

    chrome = subprocess.Popen(
        ["/usr/bin/chromium", "--headless=new", "--no-sandbox", "--disable-gpu",
         "--disable-dev-shm-usage", "--remote-debugging-port=%d" % PORT,
         "--window-size=1280,800", "--user-data-dir=/tmp/cdp-profile-%d" % PORT],
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    try:
        for _ in range(40):
            try:
                cdp_http("/json/version")
                break
            except Exception:
                time.sleep(0.25)
        else:
            print("FAIL: chromium CDP 未起（需 /usr/bin/chromium）")
            return 2

        pages = cdp_http("/json")
        page = next((t for t in pages if t.get("type") == "page"),
                    None) or cdp_http("/json/new?url=" + _q("about:blank"))

        with connect(page["webSocketDebuggerUrl"], max_size=20_000_000) as ws:
            mid = [0]

            def send(method, params=None):
                mid[0] += 1
                ws.send(json.dumps({"id": mid[0], "method": method, "params": params or {}}))
                while True:
                    m = json.loads(ws.recv())
                    if m.get("id") == mid[0]:
                        if "error" in m:
                            return {"_err": m["error"].get("message")}
                        return m.get("result", {})

            def ev(expr):
                r = send("Runtime.evaluate", {"expression": expr,
                                              "returnByValue": True, "awaitPromise": True})
                if "_err" in r:
                    return None
                return r.get("result", {}).get("value")

            def evj(expr):
                """evaluate 后按 JSON 解；拿不到就把原值塞进 why，绝不让探针自己炸。"""
                v = ev(expr)
                if isinstance(v, str) and v.startswith("{"):
                    try:
                        return json.loads(v)
                    except ValueError:
                        pass
                return {"why": str(v)[:90]}

            send("Page.enable")
            send("Runtime.enable")
            send("Page.navigate", {"url": BASE + "/"})

            # 就绪用实测判据，不用固定 sleep：readyState complete + 样式表已解析
            ready = {}
            for _ in range(40):
                time.sleep(0.5)
                ready = evj(READY_JS)
                if ready.get("rs") == "complete" and ready.get("sheets", 0) > 0:
                    break
            # 登录闸适配（2026-11-13）：未登录页面不注入 hub.js ⇒ ensureTerm 不存在。
            # 用 _hub_inject.inject_hub（gate 隐藏 + token URL + 轮询就绪），等价已登录页面。
            # 该探针自管 CDP：send(method, params) 签名与 _cdp_min 的 c.eval 不同，用 shim 适配。
            class _EvShim:
                eval = staticmethod(ev)
            ok = inject_hub(_EvShim(), wait_ms=15000,
                            ready_js="typeof window.ensureTerm==='function'")
            print("   [dbg] inject_hub=%r ensureTerm=%r scripts=%r gate=%r" % (
                ok, ev("typeof window.ensureTerm"),
                ev("document.querySelectorAll('script[src*=hub]').length"),
                ev("!!document.getElementById('loginGate')")))
            if ok:
                ready = evj(READY_JS)   # 注入后重取：注入前的快照里 ensureTerm 尚不存在
            check("V0 页面真加载（非空白页，hub.js 已就位）",
                  ready.get("rs") == "complete" and ready.get("sheets", 0) > 0
                  and ready.get("hasEnsure") == "function",
                  "readyState=%s sheets=%s title=%r ensureTerm=%s" % (
                      ready.get("rs"), ready.get("sheets"),
                      ready.get("title"), ready.get("hasEnsure")))
            if FAILS:
                print("\nRESULT: FAIL -> 页面未就绪，后续判据无意义")
                return 1

            print("== V1 浏览器内 computed --term-* token ==")
            tok = evj(TOKEN_JS)
            for k, want in DARK.items():
                check("V1 --term-%s = %s" % (k, want), tok.get(k) == want,
                      "实得 %r" % tok.get(k))
            check("V1 无 09-22 前浅底板残留",
                  not any(v in LIGHT for v in tok.values()),
                  "值=%s" % list(tok.values()))

            print("== V2 真容器 + xterm 实例真 theme（配色的唯一有效判据）==")
            host = evj(TERM_EL_JS)
            check("V2a #termEl（--term-bg 落点）背景 = rgb(0, 0, 0)",
                  host.get("bg") == "rgb(0, 0, 0)",
                  "backgroundColor=%r %s" % (host.get("bg"), host.get("why", "")))

            made = evj(ENSURE_JS)
            check("V2b xterm theme.background = #000000", made.get("bg") == "#000000",
                  "theme=%s %s" % (json.dumps({k: made.get(k) for k in
                                               ("bg", "fg", "cur", "sel", "red", "white")}),
                                   made.get("why", "")))
            check("V2b xterm theme.foreground = #ffffff", made.get("fg") == "#ffffff",
                  "fg=%r cursor=%r sel=%r" % (made.get("fg"), made.get("cur"), made.get("sel")))
            check("V2b ANSI 16 色为深底板（black=#7f7f7f red=#cd3131 green=#0dbc79 white=#e5e5e5）",
                  made.get("black") == "#7f7f7f" and made.get("red") == "#cd3131"
                  and made.get("green") == "#0dbc79" and made.get("white") == "#e5e5e5",
                  "black=%r red=%r green=%r white=%r" % (made.get("black"), made.get("red"),
                                                         made.get("green"), made.get("white")))
            check("V2b 终端字号/字族走 token（fs=14 且等宽族含 CJK 兜底）",
                  made.get("fs") == 14 and "JetBrains Mono" in str(made.get("ff")),
                  "fontSize=%r fontFamily=%r" % (made.get("fs"), made.get("ff")))

            print("== V3 兜底路径：token 缺失时也不得回浅色 ==")
            fb = evj(FALLBACK_JS)
            check("V3 cssToken 缺失时返回传入兜底（不会读错值）",
                  fb.get("miss") == "DEEP#000000",
                  "-> %r %s" % (fb.get("miss"), fb.get("err", "")))
            check("V3 cssToken 真值仍是深色（兜底不误伤真值）",
                  fb.get("real") == "#000000", "real=%r" % fb.get("real"))
            check("V3 线上 hub.js 的 T() 兜底对与 token 真值一致",
                  "T('bg', '#000000'), foreground: T('fg', '#ffffff')" in http_get("/static/hub.js"),
                  "在 /static/hub.js 实吐内容中匹配 'bg','#000000' + 'fg','#ffffff' 兜底对")
    finally:
        chrome.terminate()
        try:
            chrome.wait(timeout=5)
        except Exception:
            chrome.kill()

    print()
    if FAILS:
        print("RESULT: FAIL -> " + ", ".join(FAILS))
        return 1
    print("RESULT: ALL PASS")
    return 0


if __name__ == "__main__":
    sys.exit(main())
