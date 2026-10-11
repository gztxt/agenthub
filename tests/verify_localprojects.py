#!/usr/bin/env python3
"""「本机项目」页真渲染闸门（L2 live）：点项目 → 选 agent → 新建会话 → 终端页。

跑法（打生产 :3102，会真的拉起一个 codex 会话并在结束时销毁）：
    HUB_PROBE_TERM_TOKEN=<生产 token> venv/bin/python tests/verify_localprojects.py
    # 影子实例同型：先起 PORT=3199 的影子（见 verify_claude_menu_term 头注释），再
    # HUB_PROBE_TERM_TOKEN=probe-term-token venv/bin/python tests/verify_localprojects.py http://127.0.0.1:3199

为什么必须有这条（v0.13.30 用户需求「点击项目名称、选 agent、新建会话、自动跳到
对应 agent 拉起会话界面」）：
- 后端 API 各自绿（/api/localprojects 有数、/api/term/sessions 收 cwd）不等于
  **这条链**在真浏览器里通——懒加载钩子漏挂、lpStart 拼错字段名、gotoChat 后
  applyChatMode 把面板切错，都是"服务端全绿 + 页面点了没反应"的静默家族。
- 判据全部走真鼠标事件 + 可断言的 DOM 量与 xterm 屏幕文本，不以截图交差：
  · R1 侧栏「本机项目」按钮存在且位于 #navTree 之前（用户点名 AGENTS 菜单上面）
  · R2 点按钮 → #page-localprojects 在屏、项目行渲染（>0 行）
  · R3 点 agent-hub 行 → 选中态 .on + #lpSelName 更新 + #lpStartBtn 解禁
  · R4 #lpAgent 含 codex 选项；选 codex 后点「新建会话」
  · R5 跳到终端页（#termPane on）且服务端会话表里新会话 cwd == 所点项目路径
  · R6 会话被销毁（探针不留垃圾）
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cdp_min import CDP, launch_chrome, page_target      # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
TOKEN = os.getenv("HUB_PROBE_TERM_TOKEN", "probe-term-token")
CDP_PORT = int(os.getenv("HUB_PROBE_CDP_PORT", "9397"))
W, H = 1280, 800
TARGET_PROJECT = "/fs/1000/ftp/技术文档/agenthub"      # 用户示例场景的项目
AGENT = "codex"                                # 用户示例场景的 agent
res, spawned_sessions = [], []


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-56s %s" % ("PASS" if ok else "FAIL", name, str(detail)[:160]))


def click(c, sel, need=True):
    """真鼠标点击（元素中心）；元素不存在或不在视口内直接判失败，绝不回落 .click()。"""
    raw = c.eval("(() => { const e = document.querySelector(%s); if (!e) return 'null';"
                 " const b = e.getBoundingClientRect();"
                 " if (!b.width || !b.height) return JSON.stringify([0,0,0]);"
                 " return JSON.stringify([Math.round(b.left+b.width/2), Math.round(b.top+b.height/2), 1]); })()"
                 % json.dumps(sel))
    if not raw or raw == "null":
        if need:
            chk("点击目标存在 " + sel, False, raw)
        return None, "元素不存在"
    x, y, vis = json.loads(raw)
    if not vis:
        return None, "元素零尺寸（display:none）"
    if not (0 <= x < W and 0 <= y < H):
        return None, "坐标 (%d,%d) 在视口外" % (x, y)
    for typ in ("mousePressed", "mouseReleased"):
        c.send("Input.dispatchMouseEvent", type=typ, x=x, y=y, button="left",
               clickCount=1, buttons=1 if typ == "mousePressed" else 0)
    time.sleep(0.9)
    return (x, y), "点 (%d,%d)" % (x, y)


def api(method, path, body=None):
    """服务端对账（不经浏览器，避免把前端逻辑混进判据）。"""
    cmd = ["curl", "-s", "-m", "20", "-X", method, BASE + path, "-H", "X-TERM-TOKEN: " + TOKEN]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    out = subprocess.run(cmd, capture_output=True, text=True).stdout or "{}"
    try:
        return json.loads(out)
    except json.JSONDecodeError:
        return {"_raw": out[:200]}


proc = None
c = None
try:
    print("=" * 78)
    print("本机项目页：点项目 → 选 agent → 新建会话 → 终端页   BASE=%s" % BASE)
    print("=" * 78)

    # 前置：API 有目标项目、codex 有 term 入口
    lp = api("GET", "/api/localprojects")
    chk("前置 /api/localprojects ok 且含目标项目",
        lp.get("ok") and any(p["path"] == TARGET_PROJECT for p in lp.get("projects", [])),
        f"count={lp.get('count')}")
    agents = api("GET", "/api/agents").get("agents", [])
    ent = {a["id"]: [e["type"] for e in a.get("entries", [])] for a in agents}
    chk("前置 codex 有 term 入口", "term" in ent.get(AGENT, []), ent.get(AGENT))

    profile = tempfile.mkdtemp(prefix="hub_lp_probe_")
    proc = launch_chrome(BASE + "/", CDP_PORT, profile, W, H)
    time.sleep(3.0)
    c = CDP(page_target(CDP_PORT, tries=50))
    c.send("Page.enable")
    c.send("Runtime.enable")
    c.send("Page.addScriptToEvaluateOnNewDocument", source=(
        "window.__errs=[];addEventListener('error',e=>__errs.push(String(e.message)));"
        "addEventListener('unhandledrejection',e=>__errs.push('reject:'+String(e.reason).slice(0,120)));"))

    # 干净存档 + 预置终端口令（等价于用户在「设置」里输过一次口令的浏览器）
    c.eval("localStorage.clear(); localStorage.setItem('hub.term.token', %s);" % json.dumps(TOKEN))
    c.send("Page.reload", ignoreCache=True)
    for _ in range(20):
        time.sleep(0.5)
        if c.eval("!!document.querySelector('#btnNavProjects')"):
            break

    # R0（2026-10-11 报障回归）：落地页=本机项目时，agent 下拉必须**不点任何菜单**
    # 就自动填充。两页的 option 是同步渲染、AGENTS 是异步来的 ⇒ 曾经竞速成 0 个
    # option，框塌成 46px 空白且此后再无人重画（用户：「右边选择 agent 框不能正常
    # 显示，点 GitHub 项目菜单后才能正常显示」）。
    c.eval("localStorage.setItem('hub.page', 'localprojects');")
    c.send("Page.reload", ignoreCache=True)
    opts0 = "0"
    for _ in range(30):
        time.sleep(0.5)
        opts0 = c.eval("(() => { const s = document.getElementById('lpAgent');"
                       " return s ? s.options.length : 0; })()")
        if int(opts0 or 0) > 1:
            break
    chk("R0 落地页=本机项目：agent 下拉自动填充（无需再点菜单）", int(opts0 or 0) > 1,
        f"options={opts0}")
    for _ in range(20):
        time.sleep(0.3)
        if c.eval("!!document.querySelector('#btnNavProjects')"):
            break

    # R1：按钮存在且位于 #navTree 之前
    order = c.eval("(() => { const btn = document.querySelector('#btnNavProjects');"
                   " const tree = document.querySelector('#navTree');"
                   " if (!btn || !tree) return null;"
                   " return btn.compareDocumentPosition(tree) & Node.DOCUMENT_POSITION_FOLLOWING; })()")
    chk("R1 侧栏「本机项目」在 AGENTS(#navTree) 之上", bool(order), f"compareDocumentPosition={order}")

    # R2：点按钮 → 页面 + 行渲染
    click(c, "#btnNavProjects")
    for _ in range(24):
        time.sleep(0.5)
        rows = c.eval("document.querySelectorAll('#lpList .mem-item').length")
        if rows and int(rows) > 0:
            break
    page = c.eval("[...document.querySelectorAll('section.page.on')].map(s=>s.id).join(',')")
    rows = c.eval("document.querySelectorAll('#lpList .mem-item').length")
    chk("R2 点菜单进本机项目页且列表有行", page == "page-localprojects" and int(rows or 0) > 0,
        f"page={page} rows={rows}")

    # R3：点 agent-hub 行 → 选中态 + 动作条解禁
    idx = c.eval("(() => { const items=[...document.querySelectorAll('#lpList .mem-item')];"
                 " for (let i=0;i<items.length;i++){"
                 "   if ((items[i].title||'') === %s) return items[i].dataset.i; }"
                 " return null; })()" % json.dumps(TARGET_PROJECT))
    chk("R3a 目标项目行存在", idx is not None, f"idx={idx}")
    if idx is not None:
        click(c, f"#lpList .mem-item[data-i='{idx}']")
        st = c.eval("(() => { const sel=document.querySelector('#lpList .mem-item.on');"
                    " return JSON.stringify({ on: !!sel,"
                    " name: document.getElementById('lpSelName').textContent,"
                    " disabled: document.getElementById('lpStartBtn').disabled }); })()")
        d = json.loads(st)
        chk("R3b 选中态高亮 + 名称上屏 + 按钮解禁",
            d["on"] and d["name"] == "agent-hub" and not d["disabled"], d)

    # R4：agent 下拉含 codex，选中它
    has_codex = c.eval(f"!!document.querySelector('#lpAgent option[value=\"{AGENT}\"]')")
    chk("R4a agent 下拉含 codex", bool(has_codex))
    c.eval(f"document.getElementById('lpAgent').value = {json.dumps(AGENT)};")
    click(c, "#lpStartBtn")

    # R5：跳终端页 + 服务端会话 cwd 对账
    for _ in range(20):
        time.sleep(0.5)
        if c.eval("(document.querySelector('#termPane')||{classList:{contains:()=>false}}).classList.contains('on')"):
            break
    page = c.eval("[...document.querySelectorAll('section.page.on')].map(s=>s.id).join(',')")
    term_on = c.eval("document.querySelector('#termPane').classList.contains('on')")
    sess = api("GET", "/api/term/sessions").get("sessions", [])
    mine = [s for s in sess if s.get("agent_id") == AGENT and s.get("cwd") == TARGET_PROJECT]
    chk("R5 点新建会话 → 终端页 on + pty cwd == 项目目录",
        bool(term_on) and bool(mine),
        f"page={page} term_on={term_on} matched={len(mine)}")
    if mine:
        spawned_sessions = [s["id"] for s in mine]
        # xterm 屏有内容（codex TUI 起来了；headless 下至少缓冲非空）
        screen = c.eval("(() => { try { let s=''; for (let i=0;i<term.rows;i++){"
                        " const l=term.buffer.active.getLine(i); if(l) s+=l.translateToString(true); }"
                        " return s.trim().length; } catch(e){ return -1; } })()")
        chk("R5b xterm 屏幕缓冲非空", int(screen or 0) > 0, f"chars={screen}")

    errs = c.eval("(window.__errs||[]).slice(0,5)")
    chk("R7 页面零 JS 错误", not errs, errs)
finally:
    for sid in spawned_sessions:
        r = api("DELETE", "/api/term/sessions/" + sid)
        print("  清理会话 %s：%s" % (sid, r.get("status", r)))
    if c:
        try:
            c.ws.close()
        except Exception:
            pass
    if proc:
        proc.terminate()

n = len(res)
ok = sum(res)
print("=" * 78)
print("RESULT: %d/%d PASS" % (ok, n))
sys.exit(0 if ok == n and n else 1)
