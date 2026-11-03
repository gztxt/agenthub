#!/usr/bin/env python3
"""协同页「状态可读性」真渲染探针（CDP，v0.13.190 入库）。

  venv/bin/python tests/probe_collab_board.py [port] [base-url]

【为什么需要它】
2026-11-03 用户报「协同页非常混乱，不能直观了解任务状态 / 历史任务 / 失败项」。
当时的状态是：`templates/index.html` 里已经有 `<div id="statsAgents">` 和 `.stat-pill`
全套 CSS，但 **JS 从没渲染过它** —— 静态闸门（pytest 查选择器存在）全绿，页面上
那一块却是空白。静态测试永远判不了「渲染了没有」，所以这里断言**渲染后的 DOM**。

【判据（都是可断言的量，不是"我看了截图"）】
  A `#statsAgents .run-item` ≥ 1：汇总卡主体有内容（修前是 0，整块空白）。
  B 每个 agent 行头含「成功率」徽章 + `n=` 样本量（比率必须带样本）。
  C 展开一行后 `.run-subs` 非空，且同时只有一行 `open`（手风琴语义）。
  D 状态筛选芯片的计数已落到数字（不是初始的占位 `–`）。
  E 协同历史每行有状态徽章 `.stat-pill`，失败/阻塞数量单独成词（不再只有 `0/5`）。
  F 协同历史展开后，**每条子任务**都有状态徽章（2026-11-03 需求①：任务级状态可见）。
  G 状态字段缺失/未登记码时回退「未知」或原码，不抛错、不渲染空白。
  H DAG 节点第三行是中文文案（不是 success/running 原码），且没有节点落 --border 兜底色。

【已知边界】
不判像素、不判配色。视觉好坏属于设计判断，这里只钉「有没有、对不对、互相干不干扰」。
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
    """CDP.eval 已经把 Runtime.evaluate 的 result.value 拆出来了（_cdp_min.py:110）。
    注意 send() 的第一个位置参数是 timeout，不能拿 params dict 顶上去。"""
    return cdp.eval(expr)


def main():
    prof = tempfile.mkdtemp(prefix="cdp-collab-")
    proc = launch_chrome(BASE + "/", PORT, prof, width=1440, height=900)
    cdp = None
    fails = []
    try:
        cdp = CDP(page_target(PORT))
        cdp.send("Page.enable")
        cdp.send("Runtime.enable")
        time.sleep(2.0)
        # 走真实入口：go('tasks') 才会触发 enterTasksPage() 拉数据
        ev(cdp, "go('tasks')")
        # 等渲染：轮询到有内容或超时（探活 ≤2 次口径 → 这里是一次轮询，不算重试）
        for _ in range(40):
            n = ev(cdp, "document.querySelectorAll('#statsAgents .run-item').length")
            if n:
                break
            time.sleep(0.25)
        n_items = ev(cdp, "document.querySelectorAll('#statsAgents .run-item').length")
        # 行头（标题 + 元信息 + 徽章）整体取文本：实现把徽章放在 .run-acts 里，
        # 只读 .run-title 会把「成功率 n=45」漏掉 —— 判据不该依赖具体格子。
        head_txt = ev(cdp, "(document.querySelector('#statsAgents .run-hd')||{}).textContent||''")
        meta_txt = ev(cdp, "(document.querySelector('#statsAgents .run-meta')||{}).textContent||''")
        chip_failed = ev(cdp, "(document.querySelector('[data-chip-n=\"failed\"]')||{}).textContent||''")
        run_pills = ev(cdp, "document.querySelectorAll('#runsBody .run-hd .stat-pill').length")
        run_rows = ev(cdp, "document.querySelectorAll('#runsBody .run-item').length")

        print("[A] #statsAgents .run-item =", n_items)
        if n_items < 1:
            fails.append("A 汇总卡主体仍为空（#statsAgents 没有渲染出任何 agent 行）")
        print("[B] 首行标题 =", head_txt.strip()[:60])
        print("    首行元信息 =", " ".join(meta_txt.split())[:120])
        if "% 成功" not in head_txt or "n=" not in head_txt:
            fails.append("B agent 行头缺「成功率百分比 + n=样本量」（比率必须带分母）")
        print("[E] 协同历史行数 =", run_rows, "带徽章行头 =", run_pills)
        if run_rows and run_pills < run_rows:
            fails.append("E 协同历史行头有 %d/%d 行没有状态徽章" % (run_rows - run_pills, run_rows))

        # 展开第一行，验证副标题渲染 + 手风琴单开
        ev(cdp, "document.querySelector('#statsAgents .run-hd').click()")
        time.sleep(0.4)
        open_n = ev(cdp, "document.querySelectorAll('#statsAgents .run-item.open').length")
        subs_len = ev(cdp, "(document.querySelector('#statsAgents .run-item.open .run-subs')||{}).textContent.length||0")
        aria = ev(cdp, "document.querySelector('#statsAgents .run-item .run-hd').getAttribute('aria-expanded')")
        print("[C] 展开后 open 行数 =", open_n, "副标题字数 =", subs_len, "aria-expanded =", aria)
        if open_n != 1:
            fails.append("C 手风琴单开失效：open 行数 = %s" % open_n)
        if subs_len < 20:
            fails.append("C 展开区内容为空/过短（%s 字）" % subs_len)
        if aria != "true":
            fails.append("C aria-expanded 没跟着变（读屏拿不到展开态）")

        print("[D] 失败芯片计数 =", chip_failed.strip())
        if not chip_failed.strip() or chip_failed.strip() in ("–", "-"):
            fails.append("D 状态芯片计数没落到数字（仍是占位符）")

        # F 协同历史展开后的**每个子任务**都要带状态徽章（2026-11-03 需求①）。
        # 展开时懒加载 GET /api/tasks/{runId}，所以要等 .run-sub 真的渲染出来再数。
        # 只数 .run-sub（带 task 行），不数 .run-sub-hint（"加载中/没有拆解任务"占位）。
        ev(cdp, "document.querySelector('#runsBody .run-hd').click()")
        sub_n = 0
        for _ in range(40):
            sub_n = ev(
                cdp,
                "((document.querySelector('#runsBody .run-item.open [data-run-subs]')||{}).querySelectorAll"
                " ? document.querySelectorAll('#runsBody .run-item.open [data-run-subs] .run-sub:not(.run-sub-hint)').length : 0)",
            )
            if sub_n:
                break
            time.sleep(0.25)
        sub_pills = ev(
            cdp,
            "document.querySelectorAll('#runsBody .run-item.open [data-run-subs] .run-sub:not(.run-sub-hint) .stat-pill').length",
        )
        sub_hint = ev(
            cdp,
            "((document.querySelector('#runsBody .run-item.open [data-run-subs] .run-sub-hint')||{}).textContent||'').trim()",
        )
        print("[F] 展开子任务数 =", sub_n, "带状态徽章 =", sub_pills,
              "占位文案 =", sub_hint[:40] or "(无)")
        if sub_n and sub_pills < sub_n:
            fails.append("F 展开的子任务有 %d/%d 条没有状态徽章" % (sub_n - sub_pills, sub_n))
        if not sub_n and not sub_hint:
            fails.append("F 展开后既没有子任务行也没有占位说明（渲染断层）")
        # 徽章里得有字：空徽章等于没标（颜色是辅助，文案才是信息）
        sub_pill_txt = ev(
            cdp,
            "Array.prototype.map.call("
            "document.querySelectorAll('#runsBody .run-item.open [data-run-subs] .run-sub:not(.run-sub-hint) .stat-pill'),"
            "function(e){return e.textContent.trim()}).join(',')",
        )
        print("    子任务徽章文案 =", sub_pill_txt[:80] or "(空)")
        if sub_n and not sub_pill_txt.strip():
            fails.append("F 子任务徽章全是空文案")

        # G 状态缺失/未知不能崩、也不能渲染成空白：taskStatePill 是唯一出口，
        # 直接拿 undefined / '' / 未登记的码喂它，看回退是否落在「未知/原码」而不是抛错。
        fb = ev(
            cdp,
            "(function(){try{return [taskStatePill(undefined),taskStatePill(''),taskStatePill('no_such_state')].join('|')}"
            "catch(e){return 'THROW:'+e.message}})()",
        )
        print("[G] 缺失状态回退 =", fb)
        if fb.startswith("THROW:"):
            fails.append("G 状态缺失时渲染抛错：%s" % fb)
        elif "未知" not in fb:
            fails.append("G 状态缺失没回退到「未知」：%s" % fb)

        # H DAG 节点第三行必须是**中文文案**，不是原始英文码。
        # 2026-11-03 前这里印的是 t.status 原码（success/running/...），与徽章口径不一致。
        # 判据：节点 <text> 末行既不含 "success" 之类原码，也不该整片同色（漏抄兜底）。
        ev(cdp, "document.querySelector('#runsBody [data-run-act=\"open\"]').click()")
        dag_txt, dag_colors = "", 0
        for _ in range(40):
            dag_txt = ev(
                cdp,
                "Array.prototype.map.call(document.querySelectorAll('#dagSvg svg text'),"
                "function(e){return e.textContent.trim()}).join('|')",
            )
            # 兜底色 = --border。任何节点落到它，说明该状态没进色档表（2026-11-03 六个合法态全灰的那个 bug）。
            dag_colors = ev(
                cdp,
                "Array.prototype.filter.call(document.querySelectorAll('#dagSvg svg rect'),"
                "function(e){return e.getAttribute('stroke')===cssToken('--border','#8a8a8a')}).length",
            )
            if dag_txt:
                break
            time.sleep(0.25)
        print("[H] DAG 文本片段 =", dag_txt[:100] or "(空)", "· 落兜底色节点数 =", dag_colors)
        if not dag_txt:
            fails.append("H DAG 没渲染出任何节点文本")
        else:
            # 节点第三行形如「成功 · 1.2s」；第一行「task_id · agent_id」也含 "·"，
            # 所以只判 "·" 前半段是否正好是英文原码，避免误伤 id 里的词。
            RAW = {"success", "running", "failed", "blocked", "pending", "verifying",
                   "submitted", "cancelled", "paused", "waiting_input", "waiting_approval"}
            bad = [ln.strip() for ln in dag_txt.split("|")
                   if "·" in ln and ln.split("·")[0].strip() in RAW]
            if bad:
                fails.append("H DAG 仍印原始英文状态码：%s" % bad[:3])
            if dag_colors:
                fails.append("H 有 %d 个 DAG 节点落到 --border 兜底色（状态没进色档表）" % dag_colors)
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
    print("PASS 协同看板七条判据全过")
    return 0


if __name__ == "__main__":
    sys.exit(main())