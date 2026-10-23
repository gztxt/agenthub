#!/usr/bin/env python3
"""菜单点「Claude Code」应落到**终端页**的真渲染闸门（L2 live）。

跑法（先起影子实例：worktree 里没有 .env，端口/数据/token 全与生产隔离）：
    cd ~/agent-hub-wt-<会话短ID>
    mkdir -p work/probe/data
    ( DATA_DIR=$PWD/work/probe/data PORT=3199 HOST=127.0.0.1 \
      TERM_TOKEN=probe-term-token VITALS_RT_SWEEP=0 VITALS_JUDGE=0 \
      VITALS_STATE=$PWD/work/probe/vitals.json \
      ../agent-hub/venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 3199 & )
    sleep 4
    HUB_PROBE_TERM_TOKEN=probe-term-token ../agent-hub/venv/bin/python tests/verify_claude_menu_term.py
    # 合并回生产后可直接打生产（会真的拉起一个 claude 会话并在结束时销毁）：
    HUB_PROBE_TERM_TOKEN=<生产 token> venv/bin/python tests/verify_claude_menu_term.py http://127.0.0.1:3102

为什么必须有这条（2026-09-25 用户报障「菜单栏 Claude Code 启动故障，应该是终端页面」）：
Claude Code 是**唯一**同时带 `embed`（discovery 见 cloudcli :3010 端口活就注入「原生会话」）
和 `term` 两个入口的 Agent，而 `openEntity()` 与 `defaultModeOf()` 各自写了一份
「embed > term」优先级 ⇒ 点行必进 iframe。那张 iframe 是 CloudCLI 自己的登录页
（实测 `http://127.0.0.1:3010` 返回 "Welcome Back / Your session expired"），
在 hub 里渲染成一块白页。更要紧的是 v0.12.3 把菜单行内的动作图标 `display:none` 了
（templates/index.html:237），模式 tab 也早已停用（renderModeBar 无条件隐藏 #chatModeBar）
⇒ **站内没有任何按钮能从嵌入页切回终端页**，用户只剩「刷新」和「新窗口」。
判据全部走真鼠标事件 + 可断言的 DOM 量与 xterm 屏幕文本，不以截图交差：
  · R3 点行后 #termPane 在、#embedPane 不在（核心诉求）；且这是在**旧键被污染成 embed**
        的存档下测的 —— 用户那些浏览器就是这个状态，只改默认值救不回他们
  · R5 终端页点「+ 新会话」后，xterm 屏幕缓冲里真的出现 Claude Code TUI 首屏
  · R6 对照组：Pi Agent（有 embed 无 term）仍进 embed —— 证明 R3 不是恒绿
  · R7 嵌入入口没被删：终端页头部有「原生界面」按钮，点了能切过去
  · R4 默认推导不写盘（分档偏好不变量②），R8 显式选择才被记住
红向自证：本探针在修复前的 HEAD（fa14a0d）上必红 —— R3/R5/R7 三条同时 FAIL。
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

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3199"
TOKEN = os.getenv("HUB_PROBE_TERM_TOKEN", "probe-term-token")
CDP_PORT = int(os.getenv("HUB_PROBE_CDP_PORT", "9396"))
W, H = 1280, 800
MODE_KEY = "hub.chatmode2.claude"
res, spawned = [], []


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


STATE = r"""(() => {
  const on = id => { const e = document.getElementById(id); return !!(e && e.classList.contains('on')); };
  let screen = '';
  try {
    const b = term.buffer.active;
    for (let i = 0; i < term.rows; i++) { const l = b.getLine(b.viewportY + i); if (l) screen += l.translateToString(true) + '\n'; }
  } catch (err) { screen = 'ERR:' + err.message; }
  return { page: [...document.querySelectorAll('section.page.on')].map(s=>s.id).join(','),
           pick: (typeof chatPick !== 'undefined') ? chatPick : null,
           mode: (typeof chatMode !== 'undefined') ? chatMode : null,
           embed: on('embedPane'), term: on('termPane'), chat: on('chatPane'),
           mem: localStorage.getItem('%s'),
           screen: screen.slice(0, 4000),
           errs: (window.__errs || []).slice(0, 3) };
})()""" % MODE_KEY


def state(c):
    return c.eval(STATE)


def api(method, path, body=None):
    """服务端对账（不经浏览器，避免把前端逻辑混进判据）。"""
    cmd = ["curl", "-s", "-X", method, BASE + path, "-H", "X-TERM-TOKEN: " + TOKEN]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout or "{}")


proc = None
c = None
try:
    print("=" * 78)
    print("菜单点 Claude Code → 终端页 真渲染闸门   BASE=%s" % BASE)
    print("=" * 78)

    # 前置：服务端画像里 claude 必须同时有 embed 与 term（否则本探针测的不是这个形态）
    agents = api("GET", "/api/agents").get("agents", [])
    byid = {a["id"]: a for a in agents}
    ent = {a["id"]: [e["type"] for e in a.get("entries", [])] for a in agents}
    chk("前置 claude 同时具备 embed 与 term 入口",
        {"embed", "term"} <= set(ent.get("claude", [])), ent.get("claude"))
    chk("前置 pi 有 embed 无 term（对照组成立）",
        "embed" in ent.get("pi", []) and "term" not in ent.get("pi", []), ent.get("pi"))

    profile = tempfile.mkdtemp(prefix="hub_claude_menu_")
    proc = launch_chrome(BASE + "/", CDP_PORT, profile, W, H)
    time.sleep(3.0)
    c = CDP(page_target(CDP_PORT, tries=50))
    c.send("Page.enable")
    c.send("Runtime.enable")
    c.send("Page.addScriptToEvaluateOnNewDocument", source=(
        "window.__errs=[];addEventListener('error',e=>__errs.push(String(e.message)));"
        "addEventListener('unhandledrejection',e=>__errs.push('reject:'+String(e.reason).slice(0,120)));"))

    # 干净存档 + 预置终端口令（等价于用户在「设置」里输过一次口令的浏览器）。
    # 同时把**旧键**污染成 'embed'：用户那些浏览器里躺着的就是这个值（旧代码把默认推导
    # 当偏好写进了 hub.chatmode.<id>）。新实现必须不读旧键 ⇒ R3 才是「存量浏览器也能自愈」。
    c.eval("localStorage.clear();"
           " localStorage.setItem('hub.term.token', %s);"
           " localStorage.setItem('hub.chatmode.claude', 'embed');"
           " localStorage.setItem('hub.chatmode2.pi', 'chat');" % json.dumps(TOKEN))
    c.send("Page.reload", ignoreCache=True)
    d = None
    for _ in range(20):
        time.sleep(0.5)
        # 等菜单行真的画出来（AGENTS 是异步加载的），别用 chatPick —— 它开机就有值
        if c.eval("!!document.querySelector('button.nav-item[data-entity=\"claude\"]')"):
            d = state(c)
            break
    if d is None:
        raise SystemExit("菜单里等不到 Claude Code 行——页面没起来或 /api/agents 不通")
    chk("R1 首屏无 JS 异常", not d["errs"], d["errs"])
    chk("R2 菜单里有 Claude Code 行",
        c.eval("!!document.querySelector('button.nav-item[data-entity=\"claude\"]')") is True)

    # ── 核心：真鼠标点菜单行 ────────────────────────────────────────────
    _, why = click(c, 'button.nav-item[data-entity="claude"]')
    d = state(c)
    chk("★R3 点 Claude Code → 终端页在、嵌入页不在",
        d["term"] and not d["embed"] and d["mode"] == "term",
        "%s mode=%s embed=%s term=%s page=%s" % (why, d["mode"], d["embed"], d["term"], d["page"]))
    chk("R4 默认推导不写偏好（不变量②：只有真实交互才算偏好）",
        d["mem"] is None, d["mem"])

    # ── 终端页真能启动 Claude Code ─────────────────────────────────────
    before = {s["id"] for s in api("GET", "/api/term/sessions").get("sessions", [])}
    click(c, '#termPane .ebar-btn[onclick="termNew()"]')
    screen = ""
    for _ in range(12):
        time.sleep(1.0)
        screen = state(c)["screen"]
        if "Claude Code" in screen:
            break
    chk("★R5 点「+ 新会话」→ xterm 屏幕出现 Claude Code TUI 首屏",
        "Claude Code" in screen, repr(screen[:150]))
    live = [s for s in api("GET", "/api/term/sessions").get("sessions", [])
            if s["id"] not in before]
    spawned.extend(s["id"] for s in live)
    chk("R5b 服务端确实多出一条 claude 活会话（不是前端自嗨）",
        len(live) == 1 and live[0]["agent_id"] == "claude",
        [(s["id"], s["agent_id"]) for s in live])

    # ── 对照组：判据可证伪（有 embed 无 term 的实体仍进 embed） ─────────
    click(c, 'button.nav-item[data-entity="pi"]')
    d = state(c)
    chk("R6 对照：点 Pi Agent → 嵌入页在、终端页不在",
        d["embed"] and not d["term"] and d["mode"] == "embed",
        "mode=%s embed=%s term=%s" % (d["mode"], d["embed"], d["term"]))
    # 预置的 hub.chatmode2.pi='chat' 对 pi 是无效形态（它没有 chat entry）：
    # 回落必须是**推导**，不许把推导结果写回存档（不变量②）
    chk("R6b 无效存量形态会回落默认且不回写（加载不写盘）",
        c.eval("localStorage.getItem('hub.chatmode2.pi')") == "chat",
        c.eval("localStorage.getItem('hub.chatmode2.pi')"))

    # ── 嵌入入口没被删：终端页头部按钮可切回 ───────────────────────────
    click(c, 'button.nav-item[data-entity="claude"]')
    d = state(c)
    chk("R7a 再点 Claude Code 仍回终端页（与档位/存档无关）",
        d["term"] and not d["embed"], "mode=%s" % d["mode"])
    sel = '#termPane .ebar-btn[onclick*="switchMode"][onclick*="embed"]'
    chk("★R7b 终端页头部有「原生界面」按钮（嵌入入口不丢）",
        c.eval("!!document.querySelector(%s)" % json.dumps(sel)) is True)
    xy, why = click(c, sel)
    d = state(c)
    chk("★R7c 点它 → 切到嵌入页", bool(xy) and d["embed"] and not d["term"] and d["mode"] == "embed",
        "%s mode=%s embed=%s" % (why, d["mode"], d["embed"]))
    chk("R8 显式选择才被记住（偏好写盘）", d["mem"] == "embed", d["mem"])

    chk("R9 全程无新增 JS 异常", not (state(c) or {}).get("errs"), (state(c) or {}).get("errs"))
finally:
    for sid in spawned:
        subprocess.run(["curl", "-s", "-X", "DELETE", BASE + "/api/term/sessions/" + sid,
                        "-H", "X-TERM-TOKEN: " + TOKEN], capture_output=True, text=True)
    if spawned:
        print("清理：已销毁本次探针拉起的终端会话 %s" % spawned)
    if c:
        c.close()
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except subprocess.TimeoutExpired:
            proc.kill()

n = sum(res)
print("\n%d/%d PASS" % (n, len(res)))
sys.exit(0 if n == len(res) and res else 1)
