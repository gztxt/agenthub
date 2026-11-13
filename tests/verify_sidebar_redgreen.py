#!/usr/bin/env python3
"""L2 红-绿对照：同一份"被污染"的窄屏客户端，分别喂**定向篡改**与**当前**的 hub.js。

跑法：venv/bin/python tests/verify_sidebar_redgreen.py [http://127.0.0.1:3102]
需要：服务在跑 + 本机 chromium。生产零影响：不写盘、不重启、不发写请求，
      篡改字节只经 CDP 拦截喂给这一个 headless 客户端。

为什么改红基线（2026-11-13，用户裁定 A）：
原红基线按 git 内容回溯「最近仍写 hub.sidebar 的版本」（09-25 修复前，116 KB）。
生产 DOM 已演进到 4 分组侧栏 + 登录闸，旧 JS 在新 DOM 上根本跑不到当年那个抽屉
展开分支（实测 collapsed=False 但 w=52 / pos=relative）⇒ 红臂假红、闸门丧失灵敏度。
故红臂改为**当前产物 + 末尾追加一段撤销收起的篡改脚本**：红况由篡改稳定制造
（fixed / 236px / 接走视口中心），绿况放行真身（收起 48px、不接走中心）。
红基线从此与当前 DOM 同步演进，不再依赖历史提交；「探针能判红也能判绿」由
「红绿 collapsed 取反」这条独立判据保证，不再靠复现某一年的具体事故。
"""
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp_min import CDP, launch_chrome, page_target   # noqa: E402
from _hub_inject import inject_hub   # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BASE = next((a for a in sys.argv[1:] if a.startswith("http")), "http://127.0.0.1:3102")
PORT = int(os.environ.get("HUB_CDP_PORT", "9342"))

#: 追加在当前 hub.js 末尾、同步执行：把 initSidebar 的窄屏收起撤销，制造「抽屉展开
#: 遮住正文」的红况。显式给 position/width/zIndex，不依赖 CSS 细节；隐藏 sideMask 是
#: 为了 elementFromPoint 命中 sidebar 自身（否则命中的是遮罩，centerCoveredBySidebar=False）。
MUTANT_TAIL = r"""
/* ==== 红对照篡改（非被测代码，只用于制造红况）==== */
(function () {
  var s = document.getElementById('sidebar');
  if (s) {
    s.classList.remove('collapsed');
    s.style.position = 'fixed';
    s.style.width = '236px';
    s.style.zIndex = '9999';
  }
  var m = document.getElementById('sideMask');
  if (m) m.style.display = 'none';
})();
"""

MEASURE = r"""
(() => {
  const sb = document.getElementById('sidebar');
  if (!sb) return {ready:false};
  const r = sb.getBoundingClientRect(), cs = getComputedStyle(sb);
  const hit = document.elementFromPoint(Math.round(innerWidth/2), Math.round(innerHeight/2));
  return { ready:true, vw: innerWidth, collapsed: sb.classList.contains('collapsed'),
           sbW: Math.round(r.width), pos: cs.position, bg: cs.backgroundColor,
           centerHit: hit ? (hit.id ? '#'+hit.id : hit.tagName.toLowerCase()) : 'none',
           centerCoveredBySidebar: !!(hit && (hit === sb || sb.contains(hit))),
           legacy: localStorage.getItem('hub.sidebar') };
})()
"""


def run(serve_mutant, mutant_body):
    state = {"hits": 0}

    def on_paused(prm):
        if "hub.js" not in prm["request"]["url"]:
            c.send("Fetch.continueRequest", requestId=prm["requestId"])
            return
        state["hits"] += 1
        if serve_mutant:
            c.send("Fetch.fulfillRequest", requestId=prm["requestId"], responseCode=200,
                   headers=[{"name": "Content-Type", "value": "application/javascript"},
                            {"name": "Cache-Control", "value": "no-store"}],
                   body=base64.b64encode(mutant_body).decode())
        else:
            c.send("Fetch.continueRequest", requestId=prm["requestId"])

    proc = launch_chrome(BASE + "/", PORT, "/tmp/hub_rg_probe")
    time.sleep(3.0)
    global c
    c = CDP(page_target(PORT), on_paused)
    c.send("Page.enable"); c.send("Runtime.enable")
    c.send("Fetch.enable", patterns=[{"urlPattern": "*hub.js*", "requestStage": "Request"}])
    # 做成"事故后的存量手机"：只有与宽度无关的旧键，值是桌面留下的"展开"
    c.eval("localStorage.clear(); localStorage.setItem('hub.sidebar','0');")
    c.send("Emulation.setDeviceMetricsOverride", width=390, height=844,
           deviceScaleFactor=2, mobile=True)
    state["hits"] = 0
    c.send("Page.reload", ignoreCache=True)
    time.sleep(1.0)
    # 登录闸适配（2026-11-13）：未登录页面不发 hub.js ⇒ 拦截 0 次空转。
    # 注入产生请求：红臂被 fulfill 成篡改体、绿臂放行真身 —— 红绿语义保持。
    inject_hub(c)
    d = None
    for _ in range(24):
        time.sleep(0.5)
        try:
            d = c.eval(MEASURE)
        except Exception:
            continue
        if isinstance(d, dict) and d.get("ready"):
            break
    c.close(); proc.kill(); time.sleep(0.5)
    return d, state["hits"]


def main():
    cur = (REPO / "static" / "hub.js").read_bytes()
    mutant = cur + MUTANT_TAIL.encode("utf-8")
    print("红基线：当前 static/hub.js + 定向篡改（%d + %d bytes，md5 %s）"
          % (len(cur), len(MUTANT_TAIL), hashlib.md5(mutant).hexdigest()[:8]))

    print("\n[红] 篡改体 + 污染值 + 390px —— 抽屉展开遮住视口中心")
    old, h1 = run(True, mutant)
    print("   " + json.dumps(old, ensure_ascii=False))
    print("\n[绿] 真身 + 同一污染值 + 同一 390px")
    new, h2 = run(False, None)
    print("   " + json.dumps(new, ensure_ascii=False))

    fails = []

    def check(name, ok, detail):
        print(("  PASS  " if ok else "  FAIL  ") + name + "   [" + detail + "]")
        if not ok:
            fails.append(name)

    print()
    check("红：篡改体撤销收起后抽屉 fixed 且把视口中心整个接走",
          old.get("collapsed") is False and old.get("centerCoveredBySidebar") is True
          and old.get("pos") == "fixed",
          "collapsed=%s w=%s pos=%s bg=%s centerHit=%s"
          % (old.get("collapsed"), old.get("sbW"), old.get("pos"), old.get("bg"), old.get("centerHit")))
    check("绿：真身同条件收起且不接走中心",
          new.get("collapsed") is True and new.get("centerCoveredBySidebar") is False,
          "collapsed=%s w=%s centerHit=%s" % (new.get("collapsed"), new.get("sbW"), new.get("centerHit")))
    check("红绿灵敏度：同一 collapsed 判据在两臂取反",
          old.get("collapsed") is False and new.get("collapsed") is True,
          "红=%s 绿=%s" % (old.get("collapsed"), new.get("collapsed")))
    check("两次都确实经过了 hub.js 请求（拦截/放行各一次）", h1 > 0 and h2 > 0, "%d / %d" % (h1, h2))
    print("\n" + ("全部通过 ✅" if not fails else "失败 %d 项 ❌：%s" % (len(fails), fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
