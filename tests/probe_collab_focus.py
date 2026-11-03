#!/usr/bin/env python3
"""协同页「运行详情抢焦点 / 点外部收起」行为探针（CDP，2026-11-03）。

  venv/bin/python tests/probe_collab_focus.py [port] [base-url]

【病（用户原话）】
「点击查看 弹出详细信息 但是页面的鼠标焦点就会总是被抢或者说自动跳到详细信息
 点击其他地方 也会突然跳到详细信息」
「操作逻辑应该是点击查看 弹出详细信息 鼠标点击其他地方 详细信息应该也是要自动收起」

【根因（修复前）】
① 06-manager-tasks.js 的 6s 轮询无条件 `if (currentRun) openRun(currentRun)`，
   而 openRun 会 `setTaskAcc('run')` ⇒ 用户正在看协同历史时每 6s 被切到详情卡。
② 详情卡没有「点击外部收起」路径，只有点卡头 / 删 run 才收。

【判据（都是可断言的量）】
  A 点「查看」后详情卡展开（taskAccKey === 'run'），且是四卡里唯一 .open 的。
  B 点页面**外部**（这里点卡头/空白）后详情卡收起 + #runPanel display:none。
  C 收起后 7s 内轮询不会把详情卡重新弹开（修复①的核心：焦点不被抢）。
  D 收起后回落到「协同历史」卡（用户进来前的那张），不是四张全关的空页。
  E 「查看」按钮本身不触发「点外部收起」（否则刚点查看就被顶掉）。
  F 6s 轮询重打 openRun 时，若详情数据没变，#taskBody 不被重建（焦点不被冲掉）。
"""
import json
import os
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

PORT = int(sys.argv[1]) if len(sys.argv) > 1 else 3199
BASE = sys.argv[2] if len(sys.argv) > 2 else "http://127.0.0.1:3102"


def ev(cdp, expr):
    return cdp.eval(expr)


def wait_acc(cdp, key, tries=40):
    for _ in range(tries):
        if ev(cdp, "taskAccKey") == key:
            return True
        time.sleep(0.25)
    return False


def main():
    prof = tempfile.mkdtemp(prefix="cdp-collab-focus-")
    proc = launch_chrome(BASE + "/", PORT, prof, width=1440, height=900)
    cdp = None
    fails = []
    try:
        cdp = CDP(page_target(PORT))
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        time.sleep(2.0)
        ev(cdp, "go('tasks')")
        for _ in range(40):
            if ev(cdp, "document.querySelectorAll('#runsBody [data-run-act=\"open\"]').length"):
                break
            time.sleep(0.25)
        n_open_btn = ev(cdp, "document.querySelectorAll('#runsBody [data-run-act=\"open\"]').length")
        print("[前置] 协同历史可「查看」的行数 =", n_open_btn)
        if not n_open_btn:
            print("SKIP 没有 run 可点（库里为空）；本探针只判交互，不判数据")
            return 0

        # A 点「查看」→ 详情卡展开
        ev(cdp, "document.querySelector('#runsBody [data-run-act=\"open\"]').click()")
        ok = wait_acc(cdp, 'run')
        open_cards = ev(cdp, "document.querySelectorAll('#page-tasks .sp-card.open').length")
        print("[A] 点查看后 taskAccKey =", ev(cdp, "taskAccKey"), "· 展开卡数 =", open_cards)
        if not ok:
            fails.append("A 点「查看」后详情卡没展开（taskAccKey != 'run'）")
        elif open_cards != 1:
            fails.append("A 展开卡数 != 1（手风琴单开被破坏）：%s" % open_cards)

        # E 先验「点查看按钮本身不触发收起」：再点另一行查看，详情应仍在
        if n_open_btn > 1:
            ev(cdp, "document.querySelectorAll('#runsBody [data-run-act=\"open\"]')[1].click()")
            time.sleep(1.2)
            st = ev(cdp, "taskAccKey")
            print("[E] 点第二行查看后 taskAccKey =", st)
            if st != 'run':
                fails.append("E 点「查看」按钮本身触发了收起（taskAccKey=%s）" % st)

        # F 详情数据没变时，轮询重打 openRun 不重建 #taskBody
        ev(cdp, "window.__fb = 0; new MutationObserver(function(){window.__fb++})"
                ".observe(document.getElementById('taskBody'), {childList:true})")
        time.sleep(7.5)
        rebuilt = ev(cdp, "window.__fb")
        print("[F] 7.5s 内 #taskBody 被重建次数 =", rebuilt, "（数据没变应 = 0）")
        if rebuilt:
            fails.append("F 详情数据未变时 #taskBody 被 %d 次重建（焦点被 6s 轮询冲掉）" % rebuilt)

        # B 点外部 → 详情收起（两条口径都判：点别的卡头=收起成卡头；点页面空白=整卡隐藏）
        ev(cdp, "document.querySelector('[data-task-acc-head=\"new\"]').click()")
        time.sleep(0.8)
        body_disp = ev(cdp, "getComputedStyle(document.querySelector('#runPanel > .sp-scroll')).display")
        acc = ev(cdp, "taskAccKey")
        print("[B1] 点别的卡头后 taskAccKey =", acc, "· 详情卡体 display =", body_disp)
        if acc == 'run':
            fails.append("B1 点击外部（别的卡头）后详情卡没收起")
        if body_disp != 'none':
            fails.append("B1 点击外部后详情卡体仍展开（display=%s）" % body_disp)

        # B2 点页面空白（不落在任何卡/卡头）→ 整张详情卡隐藏 + 回落到上一张卡
        ev(cdp, "document.querySelector('#runsBody [data-run-act=\"open\"]').click()")
        if not wait_acc(cdp, 'run'):
            fails.append("B2 前置：再次点「查看」详情卡没展开")
        time.sleep(0.6)
        ev(cdp, "document.getElementById('page-tasks').click()")
        time.sleep(0.8)
        panel_disp = ev(cdp, "getComputedStyle(document.getElementById('runPanel')).display")
        acc_b2 = ev(cdp, "taskAccKey")
        print("[B2] 点页面空白后 taskAccKey =", acc_b2, "· runPanel display =", panel_disp)
        if panel_disp != 'none':
            fails.append("B2 点击页面空白后 #runPanel 仍可见（display=%s）" % panel_disp)
        # 回落的是「点查看之前正在看的那张」（这里是 B1 点开的新建协同），不是硬编码 hist
        print("[B2] 回落卡 =", acc_b2, "· 展开卡数 =",
              ev(cdp, "document.querySelectorAll('#page-tasks .sp-card.open').length"))
        if acc_b2 != 'new':
            fails.append("B2 没回落到点查看前那张卡「新建协同」（taskAccKey=%s）" % acc_b2)

        # C 收起后 7s 内轮询不会把详情重新弹开（这是「焦点被抢」的主根因）
        time.sleep(7.5)
        acc2 = ev(cdp, "taskAccKey")
        print("[C] 再等 7.5s 后 taskAccKey =", acc2)
        if acc2 == 'run':
            fails.append("C 收起后 6s 轮询把详情卡又弹开了（焦点被抢）")

        # D 回落卡：点击外部时正在看的是「新建协同」⇒ 收起后应仍留在它，不是空页
        print("[D] 回落卡 =", acc2, "（应为 new，且展开卡数 =",
              ev(cdp, "document.querySelectorAll('#page-tasks .sp-card.open').length"), "）")
        if acc2 not in ('new', 'hist'):
            fails.append("D 收起详情后没回落到用户进来前那张卡（taskAccKey=%s）" % acc2)

        # 再开一次「查看」，验证 prevAcc 追踪与再次外部收起仍成立
        ev(cdp, "document.querySelector('#runsBody [data-run-act=\"open\"]').click()")
        if not wait_acc(cdp, 'run'):
            fails.append("复验：再次点「查看」详情卡没展开")
        ev(cdp, "document.querySelector('[data-task-acc-head=\"hist\"]').click()")
        time.sleep(0.8)
        acc3 = ev(cdp, "taskAccKey")
        body_disp3 = ev(cdp, "getComputedStyle(document.querySelector('#runPanel > .sp-scroll')).display")
        print("[复验] 再开→点历史卡头收起后 taskAccKey =", acc3, "· 详情卡体 display =", body_disp3)
        if acc3 != 'hist':
            fails.append("复验：收起后没回落到「协同历史」（taskAccKey=%s）" % acc3)
        if body_disp3 != 'none':
            fails.append("复验：详情卡体没收起（display=%s）" % body_disp3)
    finally:
        try:
            if cdp:
                cdp.close()
        except Exception:
            pass
        proc.terminate()

    print()
    if fails:
        for f in fails:
            print("FAIL", f)
        return 1
    print("PASS 协同页焦点/收起六条判据全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())
