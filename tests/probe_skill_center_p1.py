#!/usr/bin/env python3
"""L2 真渲染探针：技能中心 P1 两视图 + 详情抽屉 + 试调用记账（v0.13.127）。

跑法（影子实例 + 探针，一键）：bash work/p1-browser-verify.sh
或手动：
  ① 起隔离实例（env -i + 独立 .env + /vol1 DATA_DIR + PORT=3199），记下 TERM_TOKEN；
  ② venv/bin/python tests/probe_skill_center_p1.py http://127.0.0.1:3199 <TERM_TOKEN>

【它补哪个洞】静态闸门（test_skill_center_ui.py）判的是「机制写对了没有」——
DOM id 在不在、JS 调没调某个端点。判不了**像素与交互**：tab 点了不切换、
抽屉开了是空的、invoke 点了没记账、窄屏 tab 溢出——这些静态全是绿的。
本探针把「量出来的数字」固化进仓（判据都可断言）。

【测什么（八组）】
  A 首屏：技能页可见 + 统计条出数（不是「加载中…」）+ 目录行渲染；
  B 切「调用记录」：视图切换 + 面板出汇总（窗口口径文案在）；
  C 行「查看」→抽屉：五区渲染（元信息/分布路由/调用统计/操作/正文）；
  D 浮层唯一性：抽屉开着时导航 ⇒ 抽屉被清（§4.2 ②导航即清浮层）；
  E 「试调用」：回指令正文 + toast + **影子库多一条 caller=web 的 skill.invoke**；
  F 「复制接入片段」：片段盒可见且含 REST 与 MCP 两种形态；
  G 窄屏 390：无横向溢出 + 抽屉 ≤92vw；
  H 宽屏 1440：无横向溢出。

【测法上踩过的坑，逐条避开】
  1. token 预置进 localStorage（'hub.term.token'）——否则写端点弹 prompt，
     CDP 里 dialog 没人答 ⇒ 页面吊死（_cdp_min 的读线程只分发，不代答）。
  2. 落地页用 localStorage 'hub.page'='skills' 预置，不靠点侧栏（少一个竞态）。
  3. 每次点击后等**目标状态**出现再断言，不睡死时长（睡死照样会假绿/假红）。
  4. 隐藏元素 getBoundingClientRect 恒 0——量宽度前先过可见性。

【退出码】0 = 全部达标；1 = 有不达标项。
"""
import json
import sys
import time
import urllib.request
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

BASE = "http://127.0.0.1:3199"
TOKEN = ""
for a in sys.argv[1:]:
    if str(a).startswith("http"):
        BASE = str(a)
    elif str(a):
        TOKEN = str(a)
if not TOKEN:
    print("用法：probe_skill_center_p1.py <base-url> <TERM_TOKEN>")
    sys.exit(2)

CDP_PORT = 9371
FAILS = []


def ok(msg):
    print("  PASS %s" % msg)


def bad(msg):
    print("  FAIL %s" % msg)
    FAILS.append(msg)


def evaluate(cdp, expr, timeout=15):
    """求值并取 JSON value；异常/超时返回 None（调用方负责判红，不静默）。"""
    r = cdp.send("Runtime.evaluate", expression=expr, returnByValue=True, awaitPromise=True)
    if not r or "exceptionDetails" in r:
        return None
    return (r.get("result") or {}).get("value")


def wait_until(cdp, expr, timeout=15):
    """轮询到表达式为真；返回最后一次求值结果（False ⇒ 超时）。"""
    deadline = time.time() + timeout
    last = None
    while time.time() < deadline:
        last = evaluate(cdp, expr)
        if last:
            return last
        time.sleep(0.25)
    return last


def invoke_count(caller="web"):
    """影子库里 caller=web 的调用流水条数（服务端口径，不靠页面）。"""
    try:
        with urllib.request.urlopen(BASE + "/api/skill/invocations?caller=" + caller +
                                    "&days=1&limit=50", timeout=10) as r:
            d = json.loads(r.read().decode())
        return d.get("matched_total") or 0
    except Exception as e:  # noqa: BLE001 — 取不到数就必须红，不能当成 0 放行
        print("    （查询影子库失败：%s）" % e)
        return -1


def main():
    proc = launch_chrome("about:blank", CDP_PORT, "/tmp/hub_p1_probe", 1440, 900)
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

    # 预置：落地页 + token（坑 1/2）。新增文档脚本在导航前注入。
    cdp.send("Page.addScriptToEvaluateOnNewDocument", source=(
        "try{localStorage.setItem('hub.page','skills');"
        "localStorage.setItem('hub.term.token',%r);}catch(e){}" % TOKEN))
    cdp.send("Page.navigate", url=BASE + "/")

    print("== A 首屏：技能页 + 统计条 + 目录行")
    wait_until(cdp, "document.querySelector('.page.on#page-skills') && "
                    "document.getElementById('skillStats') && "
                    "document.getElementById('skillStats').textContent.indexOf('加载中')<0")
    stats = evaluate(cdp, "document.getElementById('skillStats').textContent")
    rows = evaluate(cdp, "document.querySelectorAll('#skillList .mem-item').length")
    if stats and "个技能" in stats and "路发现点" in stats:
        ok("统计条出数：%s" % stats.replace("\n", " ")[:110])
    else:
        bad("统计条未出数：%r" % stats)
    if isinstance(rows, int) and rows > 0:
        ok("目录行 %d 条" % rows)
    else:
        bad("目录行为空：%r" % rows)
    # 徽标/指纹渲染（P1 新增面）：至少一条行带 sk-fp 指纹或调用徽标
    fp = evaluate(cdp, "document.querySelectorAll('#skillList .sk-fp').length")
    if isinstance(fp, int) and fp > 0:
        ok("digest 指纹渲染 %d 处" % fp)
    else:
        bad("digest 指纹未渲染：%r" % fp)
    # 2026-10-19 用户裁定：两个 tab 必须跟在「技能中心」文字后面（同一行），
    # 不许再被 .sp-tools 挤到下一行。量几何：tab 左边在标题右边之后、两者 top 差 ≤12px。
    geom = evaluate(cdp, "(()=>{const h=document.querySelector('#page-skills .sp-hd h3').getBoundingClientRect();"
                         "const t=document.getElementById('skillTabCatalog').getBoundingClientRect();"
                         "return {ht:Math.round(h.top),tt:Math.round(t.top),hr:Math.round(h.right),tl:Math.round(t.left)};})()")
    if geom and geom["tl"] >= geom["hr"] - 2 and abs(geom["tt"] - geom["ht"]) <= 12:
        ok("tab 与标题同一行（h3.right=%d, tab.left=%d, top 差=%d）" %
           (geom["hr"], geom["tl"], abs(geom["tt"] - geom["ht"])))
    else:
        bad("tab 未与标题同行：%r" % geom)

    print("== B 切「调用记录」：视图切换 + 汇总")
    evaluate(cdp, "document.getElementById('skillTabInvocations').click()")
    inv_ready = wait_until(cdp, "document.getElementById('skillViewInvocations').style.display!=='none'")
    if not inv_ready:
        bad("调用记录视图未显示")
    sum_txt = wait_until(cdp, "(()=>{const e=document.getElementById('skillInvSummary');"
                              "return e && e.textContent.indexOf('加载中')<0 ? e.textContent : false;})()")
    if sum_txt and ("窗口内" in sum_txt):
        ok("汇总渲染（窗口口径）：%s" % sum_txt.replace("\n", " ")[:110])
    else:
        bad("汇总未渲染：%r" % sum_txt)
    if sum_txt and "不是全历史" in sum_txt:
        ok("汇总 scope 声明在（不是全历史）")
    else:
        bad("汇总缺 scope 声明（会被读成全历史）")
    # 0 条时必须解释「为什么是 0」——用户 2026-10-19 报障「调用记录一直显示 0」。
    if sum_txt and "窗口内 0 条" in sum_txt:
        empty_txt = evaluate(cdp, "document.getElementById('skillInvocations').textContent")
        if empty_txt and "agent 直接读磁盘" in empty_txt and "试调用" in empty_txt:
            ok("0 条空状态解释了直读旁路 + 试调用出口")
        else:
            bad("0 条空状态缺少解释：%r" % (empty_txt or "")[:120])
    # WAI-ARIA Tabs 键盘：焦点在「技能目录」时按 → 应切到「调用记录」。
    evaluate(cdp, "document.getElementById('skillTabCatalog').click()")
    evaluate(cdp, "document.getElementById('skillTabCatalog').focus()")
    evaluate(cdp, "document.getElementById('skillTabCatalog').dispatchEvent("
                  "new KeyboardEvent('keydown',{key:'ArrowRight',bubbles:true}))")
    kb = wait_until(cdp, "document.getElementById('skillViewInvocations').style.display!=='none'", 3)
    if kb:
        ok("ArrowRight 从目录切到调用记录")
    else:
        bad("tab 键盘 ArrowRight 未切换")
    evaluate(cdp, "document.getElementById('skillTabCatalog').click()")

    print("== C 行「查看」→ 详情抽屉五区")
    opened = wait_until(cdp, "document.querySelectorAll('#skillList .mem-item .btn').length")
    if isinstance(opened, int) and opened > 0:
        # 2026-10-19：点行的主文本（不是右侧小按钮）验证整行可点
        evaluate(cdp, "document.querySelector('#skillList .mem-item .tag').click()")
    drawer = wait_until(cdp, "document.getElementById('skillDocDrawer').classList.contains('on')")
    if not drawer:
        bad("抽屉未打开")
    else:
        ok("整行点击经委托打开抽屉")
        meta = wait_until(cdp, "(()=>{const e=document.getElementById('skillDocMeta');"
                               "return e && e.textContent.indexOf('读取中')<0 && e.textContent.indexOf('整包')>=0 "
                               "? e.textContent : false;})()")
        if meta:
            ok("元信息区：%s" % meta.replace("\n", " ")[:90])
        else:
            bad("元信息区未渲染：%r" % meta)
        body = wait_until(cdp, "(()=>{const e=document.getElementById('skillDocBody');"
                               "return e && e.textContent.indexOf('读取中')<0 && e.textContent.length>20 "
                               "? e.textContent.length : false;})()")
        if body:
            ok("正文区载入 %s 字符" % body)
        else:
            bad("正文区未载入：%r" % body)
        usage = evaluate(cdp, "document.getElementById('skillDocUsage').textContent")
        if usage and "读取中" not in usage:
            ok("调用统计区：%s" % usage.replace("\n", " ")[:90])
        else:
            bad("调用统计区未渲染：%r" % usage)

    print("== D 浮层唯一性：窄屏导航即清抽屉")
    # 契约口径（go() 注释有据）：导航清浮层**只在窄屏强制**——桌面抽屉是右侧
    # 常驻面板，收掉反而影响操作。所以本组先切窄屏再验；在桌面上验「清」
    # 等于测一个不存在的契约（第一版就这么错的，已改）。
    cdp.send("Emulation.setDeviceMetricsOverride", width=390, height=844,
             deviceScaleFactor=1, mobile=True)
    time.sleep(0.5)
    if not evaluate(cdp, "document.getElementById('skillDocDrawer').classList.contains('on')"):
        evaluate(cdp, "document.querySelector('#skillList .mem-item .btn').click()")
        wait_until(cdp, "document.getElementById('skillDocDrawer').classList.contains('on')")
    evaluate(cdp, "go('kb')")   # 导航函数（06 顶层注册）
    cleared = wait_until(cdp, "!document.getElementById('skillDocDrawer').classList.contains('on')", 5)
    if cleared:
        ok("窄屏导航后抽屉已清")
    else:
        bad("窄屏导航后抽屉残留（§4.2 ②）")
    evaluate(cdp, "go('skills')")

    print("== E 「试调用」：回指令 + 影子库 caller=web 记账")
    before = invoke_count()
    evaluate(cdp, "document.querySelector('#skillList .mem-item .btn').click()")
    wait_until(cdp, "document.getElementById('skillDocDrawer').classList.contains('on')")
    evaluate(cdp, "skillInvokeCur()")
    invoked = wait_until(cdp, "(()=>{const e=document.getElementById('skillDocBody');"
                              "return e && e.textContent.indexOf('读取中')<0 && e.textContent.indexOf('调用中')<0 "
                              "&& e.textContent.length>20 ? e.textContent.length : false;})()")
    if invoked:
        ok("试调用回指令正文 %s 字符" % invoked)
    else:
        bad("试调用未回正文：%r" % invoked)
    time.sleep(1.0)   # 记账落库窗口（runlog.track 同步写，留一秒余量）
    after = invoke_count()
    if before >= 0 and after > before:
        ok("影子库新增 caller=web 调用 %d → %d" % (before, after))
    else:
        bad("影子库未见新增调用（before=%r after=%r）" % (before, after))
    # 成功记账后，tab 上的数量徽标也必须 0→1（不让用户再怀疑「记录了但界面不动」）
    badge = wait_until(cdp, "(()=>{const e=document.getElementById('skillInvTabBadge');"
                            "return e && e.textContent==='1';})()", 5)
    if badge:
        ok("调用记录 tab 徽标 0 → 1")
    else:
        bad("调用记录 tab 徽标未随记账更新")

    print("== F 「复制接入片段」")
    evaluate(cdp, "skillCopySnippet()")
    snip = wait_until(cdp, "(()=>{const e=document.getElementById('skillDocSnippet');"
                           "return e && e.style.display!=='none' && e.textContent.indexOf('curl')>=0 "
                           "? e.textContent : false;})()")
    if snip and "/api/skill/invoke" in snip and "hub_skill_invoke" in snip:
        ok("片段含 REST + MCP 两种形态")
    else:
        bad("接入片段缺失：%r" % (snip or "")[:120])

    print("== G/H 两档无横向溢出")
    for w, name in ((390, "窄屏"), (1440, "宽屏")):
        cdp.send("Emulation.setDeviceMetricsOverride", width=w, height=844,
                 deviceScaleFactor=1, mobile=(w < 768))
        time.sleep(0.6)   # 重排 + change 监听重算（跨断点必重算，§4.1 ④）
        ov = evaluate(cdp, "(()=>{const de=document.documentElement;"
                           "return {sw:de.scrollWidth, iw:window.innerWidth};})()")
        if ov and ov.get("sw") <= ov.get("iw") + 1:
            ok("%s %d 无横向溢出（scrollWidth=%d）" % (name, w, ov["sw"]))
        else:
            bad("%s %d 横向溢出：%r" % (name, w, ov))
        # 同档再量一次「tab 是否仍与标题同行」——1440 绿不代表 390 不换行。
        g2 = evaluate(cdp, "(()=>{const h=document.querySelector('#page-skills .sp-hd h3').getBoundingClientRect();"
                           "const t=document.getElementById('skillTabCatalog').getBoundingClientRect();"
                           "return {ht:Math.round(h.top),tt:Math.round(t.top),hr:Math.round(h.right),tl:Math.round(t.left)};})()")
        if g2 and g2["tl"] >= g2["hr"] - 2 and abs(g2["tt"] - g2["ht"]) <= 14:
            ok("%s %d tab 仍与标题同行" % (name, w))
        else:
            bad("%s %d tab 被挤到下一行：%r" % (name, w, g2))
    # 抽屉窄屏宽度 ≤ 92vw（§4.2 ③触屏逃生的前提：点得到遮罩）
    cdp.send("Emulation.setDeviceMetricsOverride", width=390, height=844,
             deviceScaleFactor=1, mobile=True)
    time.sleep(0.4)
    dw = evaluate(cdp, "(()=>{const d=document.getElementById('skillDocDrawer');"
                       "const r=d.getBoundingClientRect();"
                       "return {w:Math.round(r.width), vw:window.innerWidth};})()")
    if dw and dw["w"] <= dw["vw"] * 0.93:
        ok("窄屏抽屉宽 %d ≤ 92vw(%d)" % (dw["w"], dw["vw"]))
    else:
        bad("窄屏抽屉超宽：%r" % dw)

    if dialogs["n"]:
        bad("出现 %d 个未答对话框（token 预置失败？）" % dialogs["n"])
    else:
        ok("全程零对话框（token 预置生效）")

    print("─── 结论：%s（FAIL=%d）" % ("ALL-PASS" if not FAILS else "HAS-FAIL", len(FAILS)))
    try:
        proc.terminate()
    except Exception:  # noqa: BLE001
        pass
    sys.exit(0 if not FAILS else 1)


if __name__ == "__main__":
    main()
