#!/usr/bin/env python3
"""终端页空格的验收闸门（L2 live，真键盘事件）——「Grok 会话窗口空格不能用、一按就出现重复文字」。

跑法（影子实例，与生产 DATA_DIR 隔离）：
    cd ~/agent-hub-wt-<会话短ID> && mkdir -p work/probe/data
    ( DATA_DIR=$PWD/work/probe/data PORT=3199 HOST=127.0.0.1 TERM_TOKEN=probe-term-token \
      VITALS_RT_SWEEP=0 VITALS_JUDGE=0 VITALS_STATE=$PWD/work/probe/vitals.json \
      ../agent-hub/venv/bin/python -m uvicorn src.main:app --host 127.0.0.1 --port 3199 & )
    sleep 4
    HUB_PROBE_TERM_TOKEN=probe-term-token ../agent-hub/venv/bin/python tests/verify_term_key_relay.py
    # 合并后可打生产：HUB_PROBE_TERM_TOKEN=<生产 token> venv/bin/python tests/verify_term_key_relay.py http://127.0.0.1:3102

为什么需要它（2026-09-25 用户报障）：
实测口径先立住 —— hub 的输入链路**不会把空格发两遍**：把 pty 设成 `-echo -icanon` 后
逐键比对，`hello`→`hello`、1 个空格→`' '`、3 个空格→`'   '`，IME 上屏 `中`+空格也各一份。
真正会丢键的是**焦点**：焦点一旦落到终端外（手机上点过标题/芯片、桌面上点过页面任意处），
按键既不进 pty、也没有任何提示 —— 实测 `焦点=BODY` 时敲空格，pty 收到 0 份、屏幕不动。
用户接着就会点一下终端再敲，而 Grok TUI 在主屏缓冲区里反复重画整屏（首帧没有 ?1049h 备用屏），
于是同一份文字在 xterm 里出现两遍 ⇒ 报上来的现象正是"空格不能用 + 出现重复的文字内容"。

判据（全部真事件 + 可断言量，不以截图交差）：
  A 焦点在终端里：敲 `a`+空格+`b` ⇒ pty 实收恰为 `a b`（不多不少，钉住"不重复发"）
  B 焦点在终端外：敲 `c`+空格+`d` ⇒ pty 实收恰为 `c d`（改前收 0 份 ⇒ 本条必红）
  C 真输入位不劫持：点搜索框敲 `e f` ⇒ pty 收 0 份、搜索框里确实是 `e f`
  D 空格不得被当成翻页：B 场景下文档 scrollTop 与 xterm 视口 scrollTop 均不变
  E 全程零 JS 异常
红向自证：B 是"改前必红"的那一条；A/C/D 在改前也成立 ⇒ 证明 B 的红来自缺陷本身，
而不是判据写死成"永远要求收到键"。
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
PORT = int(os.getenv("HUB_PROBE_CDP_PORT", "9413"))
W, H = 1280, 800
res, spawned = [], []

CODE = {" ": ("Space", 32), "-": ("Minus", 189), ";": ("Semicolon", 186),
        "0": ("Digit0", 48), "1": ("Digit1", 49), "2": ("Digit2", 50), "3": ("Digit3", 51),
        "4": ("Digit4", 52), "5": ("Digit5", 53), "6": ("Digit6", 54), "7": ("Digit7", 55),
        "8": ("Digit8", 56), "9": ("Digit9", 57)}


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-58s %s" % ("PASS" if ok else "FAIL", name, str(detail)[:150]))


def api(method, path, body=None):
    cmd = ["curl", "-s", "-X", method, BASE + path, "-H", "X-TERM-TOKEN: " + TOKEN]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout or "{}")


def press(c, ch):
    if ch == "\n":
        code, vk, txt = "Enter", 13, "\r"
    else:
        code, vk = CODE.get(ch, ("Key" + ch.upper(), ord(ch.upper())))
        txt = ch
    for t in ("keyDown", "keyUp"):
        c.send("Input.dispatchKeyEvent", type=t, key=ch, code=code, text=txt,
               unmodifiedText=txt, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    time.sleep(0.07)


def type_text(c, s):
    for ch in s:
        press(c, ch)


def click_xy(c, x, y):
    for t in ("mousePressed", "mouseReleased"):
        c.send("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left",
               clickCount=1, buttons=1 if t == "mousePressed" else 0)
    time.sleep(0.45)


def center(c, sel, extra=""):
    raw = c.eval("(() => { const e = document.querySelector(%s); if (!e) return 'null';"
                 " %s const b = e.getBoundingClientRect();"
                 " if (!b.width || !b.height) return 'zero';"
                 " return JSON.stringify([Math.round(b.left+b.width/2), Math.round(b.top+b.height/2)]); })()"
                 % (json.dumps(sel), extra))
    return json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else None


STATE = r"""(() => { const b = term.buffer.active; const vp = document.querySelector('.xterm-viewport');
  const de = document.documentElement;
  let all = '';
  for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l) all += l.translateToString(true) + '\n'; }
  return { all: all, docScroll: Math.round(de.scrollTop || document.body.scrollTop),
           vpScroll: vp ? Math.round(vp.scrollTop) : -1,
           focus: document.activeElement ? (document.activeElement.className || document.activeElement.tagName) : null,
           search: (document.getElementById('navSearch') || {}).value || '',
           ws: termWs ? termWs.readyState : null, sid: termSid,
           panes: ['embedPane','termPane','chatPane'].filter(i => document.getElementById(i).classList.contains('on')),
           errs: (window.__errs || []).slice(0, 3) }; })()"""


def tail(c, n=1):
    """取缓冲区最后 n 行（cat 在 -icanon 下逐字回写 ⇒ 末尾就是 pty 实收）。"""
    st = c.eval(STATE)
    lines = [l for l in st["all"].split("\n") if l.strip()]
    return "".join(lines[-n:]), st


proc = c = None
try:
    print("=" * 78)
    print("终端页空格/焦点接力闸门  BASE=%s" % BASE)
    print("=" * 78)

    sid = api("POST", "/api/term/sessions", {"agent_id": "shell"})["session"]["id"]
    spawned.append(sid)
    chk("前置 已拉起 bash 终端会话", bool(sid), sid)

    proc = launch_chrome(BASE + "/", PORT, tempfile.mkdtemp(prefix="hub_relay_"), W, H)
    time.sleep(3.0)
    c = CDP(page_target(PORT, tries=50))
    c.send("Page.enable")
    c.send("Runtime.enable")
    c.send("Page.addScriptToEvaluateOnNewDocument", source=(
        "window.__errs=[];addEventListener('error',e=>__errs.push(String(e.message)));"
        "addEventListener('unhandledrejection',e=>__errs.push('reject:'+String(e.reason).slice(0,120)));"))
    c.eval("localStorage.clear(); localStorage.setItem('hub.term.token', %s);"
           " localStorage.setItem('hub.chat.pick','shell');" % json.dumps(TOKEN))
    c.send("Page.reload", ignoreCache=True)
    time.sleep(3.0)

    # 走真实路径进终端页并接上会话
    for _ in range(5):
        raw = c.eval("""(() => { const row=document.querySelector('button.nav-item[data-entity="shell"]');
          if(!row) return 'norow'; const acc=row.closest('.nav-acc');
          const h=acc&&acc.querySelector('.nav-acc-head');
          if(h&&h.getAttribute('aria-expanded')!=='true'){h.scrollIntoView({block:'center'});
            const b=h.getBoundingClientRect();
            return JSON.stringify(['head',Math.round(b.left+b.width/2),Math.round(b.top+b.height/2)]);}
          row.scrollIntoView({block:'center'}); const b=row.getBoundingClientRect();
          return JSON.stringify(['row',Math.round(b.left+b.width/2),Math.round(b.top+b.height/2)]); })()""")
        raw = json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else raw
        if not isinstance(raw, list):
            time.sleep(0.4)
            continue
        click_xy(c, raw[1], raw[2])
        if raw[0] == "row":
            break
        time.sleep(0.6)
    time.sleep(1.5)
    chip = center(c, "#termSessList .sess-item")
    if chip:
        click_xy(c, chip[0], chip[1])
    time.sleep(1.5)
    st = c.eval(STATE)
    chk("前置 终端页在且 ws 已连", st["panes"] == ["termPane"] and st["ws"] == 1,
        "%s ws=%s" % (st["panes"], st["ws"]))

    # 把 pty 设成「不回显、不缓冲」，再交给 cat：屏幕上出现的每个字符就等于程序真正收到的字节
    type_text(c, "stty -echo -icanon; cat\n")
    time.sleep(1.5)
    _, st = tail(c)
    chk("前置 cat 已接管（无异常）", not st["errs"], st["errs"])

    print("\n── A. 焦点在终端里（对照组：改前也成立）──")
    body = center(c, "#termEl")
    click_xy(c, body[0], body[1])
    type_text(c, "a b")
    time.sleep(0.8)
    got, st = tail(c)
    chk("A pty 实收恰为 'a b'（空格不多发）", got.endswith("a b"), repr(got[-12:]))

    print("\n── B. 焦点掉到终端外（用户现场：改前按键直接丢）──")
    # 点终端面板标题（非输入位、不可聚焦 ⇒ activeElement 落回 BODY）＝用户"点了别处"的现场
    out = center(c, "#termTitle")
    click_xy(c, out[0], out[1])
    before, st0 = tail(c)
    chk("B 前置 焦点确已离开终端", "xterm-helper" not in str(st0["focus"]), st0["focus"])
    type_text(c, "c d")
    time.sleep(0.8)
    after, st1 = tail(c)
    delta = after[len(before):] if after.startswith(before) else after
    chk("★B 焦点在终端外敲 'c'+空格+'d' 仍送达 pty 且只一份", delta == "c d", repr(delta))
    chk("D 空格没被当成翻页（文档与终端视口都没滚）",
        st1["docScroll"] == st0["docScroll"] and st1["vpScroll"] == st0["vpScroll"],
        "doc %s→%s / vp %s→%s" % (st0["docScroll"], st1["docScroll"], st0["vpScroll"], st1["vpScroll"]))

    print("\n── C. 真输入位不得被劫持（P2-11 口径不破）──")
    s = center(c, "#navSearch")
    click_xy(c, s[0], s[1])
    before, st2 = tail(c)
    type_text(c, "e f")
    time.sleep(0.6)
    after, st3 = tail(c)
    delta = after[len(before):] if after.startswith(before) else after
    chk("C 搜索框里敲 'e f'：pty 收 0 份", delta == "", repr(delta))
    chk("C 搜索框里确实是 'e f'", st3["search"] == "e f", repr(st3["search"]))

    chk("E 全程零 JS 异常", not st3["errs"], st3["errs"])
finally:
    for s in spawned:
        subprocess.run(["curl", "-s", "-X", "DELETE", BASE + "/api/term/sessions/" + s,
                        "-H", "X-TERM-TOKEN: " + TOKEN], capture_output=True, text=True)
    print("清理：已销毁本次探针拉起的终端会话 %s" % spawned)
    if c:
        c.close()
    if proc:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()

n = sum(res)
print("\n%d/%d PASS" % (n, len(res)))
sys.exit(0 if n == len(res) and res else 1)
