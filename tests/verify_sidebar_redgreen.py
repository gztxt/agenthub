#!/usr/bin/env python3
"""L2 红-绿对照：同一份"被污染"的窄屏客户端，分别喂**改前**与**改后**的 hub.js。

跑法：venv/bin/python tests/verify_sidebar_redgreen.py [http://127.0.0.1:3102]
需要：服务在跑 + 本机 chromium + 本仓 git 历史。生产零影响：不写盘、不重启、不发写请求，
      改前字节只经 CDP 拦截喂给这一个 headless 客户端。

为什么单独一只（而不是只留 verify_sidebar_narrow.py 的绿灯）：
只看"现在好了"证明不了"当时是它坏的"。红基线取不到，绿灯就只是自证。
旧实现**按内容从 git 回溯**（找最近一个仍写与宽度无关键 `setItem('hub.sidebar',` 的版本），
不钉 HEAD、不读 *.bak-*（.gitignore 排除且随时可清）⇒ 半年后仍跑得动。
"""
import base64
import hashlib
import json
import os
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp_min import CDP, launch_chrome, old_hub_bytes, page_target   # noqa: E402
from _hub_inject import inject_hub   # noqa: E402

REPO = Path(__file__).resolve().parents[1]
BASE = next((a for a in sys.argv[1:] if a.startswith("http")), "http://127.0.0.1:3102")
PORT = int(os.environ.get("HUB_CDP_PORT", "9342"))

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


def run(serve_old, old_body):
    state = {"hits": 0}

    def on_paused(prm):
        if "hub.js" not in prm["request"]["url"]:
            c.send("Fetch.continueRequest", requestId=prm["requestId"])
            return
        state["hits"] += 1
        if serve_old:
            c.send("Fetch.fulfillRequest", requestId=prm["requestId"], responseCode=200,
                   headers=[{"name": "Content-Type", "value": "application/javascript"},
                            {"name": "Cache-Control", "value": "no-store"}],
                   body=base64.b64encode(old_body).decode())
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
    # 注入产生请求：红臂被 fulfill 成旧字节、绿臂放行真身 —— 原语义完全保持。
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
    old_body, rev = old_hub_bytes(REPO)
    if not old_body:
        print("  环境不满足：git 里找不到仍写 hub.sidebar 的旧版本（红基线取不到）")
        return 2
    print("红基线来源：git %s 的 static/hub.js（%d bytes，md5 %s）"
          % (rev, len(old_body), hashlib.md5(old_body).hexdigest()[:8]))

    print("\n[红] 改前 hub.js + 污染值 + 390px —— 用户报的现象必须在这里复现")
    old, h1 = run(True, old_body)
    print("   " + json.dumps(old, ensure_ascii=False))
    print("\n[绿] 改后 hub.js + 同一污染值 + 同一 390px")
    new, h2 = run(False, None)
    print("   " + json.dumps(new, ensure_ascii=False))

    fails = []

    def check(name, ok, detail):
        print(("  PASS  " if ok else "  FAIL  ") + name + "   [" + detail + "]")
        if not ok:
            fails.append(name)

    print()
    check("红：改前窄屏首屏抽屉是 fixed 白底且把视口中心整个接走",
          old.get("collapsed") is False and old.get("centerCoveredBySidebar") is True
          and old.get("pos") == "fixed",
          "collapsed=%s w=%s pos=%s bg=%s centerHit=%s"
          % (old.get("collapsed"), old.get("sbW"), old.get("pos"), old.get("bg"), old.get("centerHit")))
    check("绿：改后同条件收起且不接走中心",
          new.get("collapsed") is True and new.get("centerCoveredBySidebar") is False,
          "collapsed=%s w=%s centerHit=%s" % (new.get("collapsed"), new.get("sbW"), new.get("centerHit")))
    check("两次都确实经过了 hub.js 请求（拦截/放行各一次）", h1 > 0 and h2 > 0, "%d / %d" % (h1, h2))
    print("\n" + ("全部通过 ✅" if not fails else "失败 %d 项 ❌：%s" % (len(fails), fails)))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
