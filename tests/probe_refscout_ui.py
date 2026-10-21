#!/usr/bin/env python3
"""L2 真渲染探针：技能中心「参考情报」视图（PT-20261021-04）。

静态闸门只能判「DOM id 在不在、函数调没调」，判不了：tab 点了不切换、列表渲染成
空壳、详情展开是空白、窄屏溢出。本探针在真 Chromium 里点一遍。

跑法：venv/bin/python tests/probe_refscout_ui.py http://127.0.0.1:3212 <TERM_TOKEN>

退出码 0 = 全部达标；1 = 有不达标项。
"""
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

BASE = "http://127.0.0.1:3212"
TOKEN = ""
for a in sys.argv[1:]:
    if str(a).startswith("http"):
        BASE = str(a)
    elif str(a):
        TOKEN = str(a)
if not TOKEN:
    print("用法：probe_refscout_ui.py <base-url> <TERM_TOKEN>")
    sys.exit(2)

CDP_PORT = 9379
FAILS = []


def ok(msg):
    print("  PASS %s" % msg)


def bad(msg):
    print("  FAIL %s" % msg)
    FAILS.append(msg)


def evaluate(cdp, expr):
    r = cdp.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
    if not r or "exceptionDetails" in r:
        return None
    return (r.get("result") or {}).get("value")


def wait_until(cdp, expr, timeout=20):
    end = time.time() + timeout
    last = None
    while time.time() < end:
        last = evaluate(cdp, expr)
        if last:
            return last
        time.sleep(0.25)
    return last


def main():
    prof = str(Path(__file__).resolve().parents[1] / "work" / "refscout-probe-profile")
    proc = launch_chrome("about:blank", CDP_PORT, prof, 1440, 900)
    cdp = CDP(page_target(CDP_PORT))
    dialogs = {"n": 0}

    def on_evt(method, params):
        if method == "Page.javascriptDialogOpening":
            dialogs["n"] += 1
            try:
                cdp.send("Page.handleJavaScriptDialog", accept=False)
            except Exception:  # noqa: BLE001
                pass

    cdp.on_event = on_evt
    cdp.send("Page.enable")
    cdp.send("Runtime.enable")
    cdp.send("Page.addScriptToEvaluateOnNewDocument", source=(
        "try{localStorage.setItem('hub.page','skills');"
        "localStorage.setItem('hub.term.token',%r);}catch(e){}" % TOKEN))
    cdp.send("Page.navigate", url=BASE + "/")

    print("== A tab 存在且可切换")
    wait_until(cdp, "document.querySelector('.page.on#page-skills') && "
                    "document.getElementById('skillTabRefs')")
    if evaluate(cdp, "!!document.getElementById('skillTabRefs')"):
        ok("技能中心出现「参考情报」tab")
    else:
        bad("tab 未渲染")
        return finish(cdp, proc)
    evaluate(cdp, "document.getElementById('skillTabRefs').click()")
    shown = wait_until(cdp, "document.getElementById('skillViewRefs').style.display !== 'none' && "
                            "document.getElementById('skillViewRefs').offsetHeight > 0")
    if shown:
        ok("点击后视图可见")
    else:
        bad("视图未显示（tab 切换失效）")
    if evaluate(cdp, "document.getElementById('skillTabRefs').getAttribute('aria-selected')") == "true":
        ok("aria-selected=true")
    else:
        bad("aria-selected 未同步")

    print("== B 来源增删（自有仓，多来源）")
    if evaluate(cdp, "!!document.getElementById('refSources') && "
                     "!!document.getElementById('refSrcInput') && typeof addRefSource === 'function'"):
        ok("来源工具条与函数存在")
    else:
        bad("来源工具条/函数缺失")
        return finish(cdp, proc)
    before = evaluate(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]').length") or 0
    evaluate(cdp, "(function(){var i=document.getElementById('refSrcInput');"
                  "i.value='gztxt';addRefSource();return 1;})()")
    added = wait_until(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]').length > %d"
                       % before)
    label = evaluate(cdp, "(document.querySelector('#refSources [data-ref-src-del]')||{}).textContent||''")
    if added and "gztxt" in (label or ""):
        ok("添加后出现来源 chip：%s" % (label or "").strip())
    else:
        bad("添加来源后 chip 未出现（before=%d）" % before)
    # 只回滚**本探针新增**的那一条：影子库通常为空，但生产探针绝不能删掉用户既有来源。
    now = evaluate(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]').length") or 0
    if now > before:
        evaluate(cdp, "(document.querySelector('#refSources [data-ref-src-del]')||{}).click && "
                      "document.querySelector('#refSources [data-ref-src-del]').click()")
        gone = wait_until(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]').length === %d"
                          % before)
        (ok if gone else bad)("点 chip 删除后回到 %d 条" % before)
    else:
        ok("来源已存在，跳过删除（不动既有数据）")

    print("== B2 星标来源（kind=starred 走同一条归一化）")
    n0 = evaluate(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]').length") or 0
    evaluate(cdp, "(function(){var k=document.getElementById('refSrcKind');"
                  "if(k)k.value='starred';var i=document.getElementById('refSrcInput');"
                  "i.value='gztxt';addRefSource();return 1;})()")
    added = wait_until(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]').length > %d" % n0)
    star_label = evaluate(cdp, "(function(){var b=[].slice.call("
                              "document.querySelectorAll('#refSources [data-ref-src-del]'))"
                              ".filter(function(x){return /star:gztxt/.test(x.textContent)});"
                              "return b.length?b[0].textContent:'';})()") or ""
    if added and star_label:
        ok("星标来源 chip：%s" % star_label.strip())
    else:
        bad("星标来源未生成（label=%r n0=%d）" % (star_label, n0))
    gone_star = evaluate(cdp, "(function(){var b=[].slice.call("
                             "document.querySelectorAll('#refSources [data-ref-src-del]'))"
                             ".filter(function(x){return /star:gztxt/.test(x.textContent)});"
                             "if(b.length){b[0].click();return 1}return 0;})()")
    if gone_star:
        (ok if wait_until(cdp, "document.querySelectorAll('#refSources [data-ref-src-del]')"
                               ".length === %d" % n0) else bad)("星标 chip 可删除")

    print("== C 卡片列表渲染（数据来自 /api/ref/cards）")
    wait_until(cdp, "document.querySelectorAll('#refCards .mem-item').length > 0")
    n = evaluate(cdp, "document.querySelectorAll('#refCards .mem-item').length")
    if n and n > 0:
        ok("渲染 %d 张卡" % n)
    else:
        bad("列表为空（接口无数据或渲染失败）")
        return finish(cdp, proc)

    print("== D 点「查看」展开详情")
    evaluate(cdp, "(document.querySelector('#refCards [data-ref-act=open]')||{}).click && "
                  "document.querySelector('#refCards [data-ref-act=open]').click()")
    opened = wait_until(cdp, "document.getElementById('refDetailBox').style.display !== 'none' && "
                             "(document.getElementById('refDetail').textContent||'').length > 200")
    if opened:
        text = evaluate(cdp, "document.getElementById('refDetail').textContent") or ""
        (ok if "as of" in text else bad)("详情正文 %d 字，含 as of=%s" % (len(text), "as of" in text))
        if "可借鉴点" in text:
            ok("含「可借鉴点」段")
        else:
            bad("缺「可借鉴点」段")
    else:
        bad("详情未展开或正文为空")

    print("== E 窄屏 390：无横向溢出")
    cdp.send("Emulation.setDeviceMetricsOverride", width=390, height=844,
             deviceScaleFactor=2, mobile=True)
    time.sleep(0.6)
    over = evaluate(cdp, "(function(){var p=document.getElementById('page-skills');"
                         "return p ? p.scrollWidth - p.clientWidth : -1;})()")
    if over is not None and 0 <= over <= 2:
        ok("横向溢出 %dpx" % over)
    else:
        bad("横向溢出异常：%r" % over)

    if dialogs["n"]:
        bad("出现 %d 个未处理对话框（交互被阻塞）" % dialogs["n"])
    else:
        ok("无阻塞对话框")
    return finish(cdp, proc)


def finish(cdp, proc):
    try:
        cdp.close()
    except Exception:  # noqa: BLE001
        pass
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    print("\n结果：%s" % ("全部达标" if not FAILS else "%d 项不达标" % len(FAILS)))
    return 0 if not FAILS else 1


if __name__ == "__main__":
    sys.exit(main())
