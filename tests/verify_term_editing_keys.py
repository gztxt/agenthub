#!/usr/bin/env python3
"""L2 live 验收闸门：v0.13.99「编辑键在焦点不在终端时也能用」+ **滚轮不得回归**。

【用户报障】2026-10-09：「嵌入式终端会话文字输入框无法正常使用键盘的上下左右光标键…
  输入框只是不需要鼠标焦点 但是键盘的全部原生功能都是需要的」，
并明确要求：**现在鼠标滚轮向上翻是正常的，千万不要影响这个功能**。

【两组判据，地位不同】
  R 组（滚轮）= **不可动项**，用户点名要保。任一条红即本版失败。
  K 组（键盘）= 本版要修的。

【为什么滚轮判据用 viewportY 而不是 scrollTop】
本仓 13-term-touch.js:77-82 记着一个已踩过的坑：xterm 6.0 的 `scrollLines` 落到
DOM 滚动，视口已在底部时被 clamp 住 ⇒ **静默不生效**。所以「scrollTop 变没变」
不能当滚轮是否生效的判据，必须看缓冲区的绝对行号 `viewportY`。

【红向自证】K3 是「改前必红」的那一条（改前焦点外四方向键 pty 收 0 份，已实测）。
R 组在改前**也是绿的** —— 它们是护栏不是修法，红了说明修复伤到了滚轮。

跑法（影子实例）：
    HUB_PROBE_TERM_TOKEN=<.env 里的> venv/bin/python tests/verify_term_editing_keys.py
    # 或指定 base： PROBE_BASE=http://127.0.0.1:3199 ...
"""
import json
import os
import subprocess
import sys
import tempfile
import time
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "tests"))
from _cdp_min import CDP, launch_chrome, page_target      # noqa: E402

BASE = os.getenv("PROBE_BASE", "http://127.0.0.1:3199")


def _token_from_env() -> str:
    for line in (REPO / ".env").read_text(encoding="utf-8").splitlines():
        k, sep, v = line.partition("=")
        if sep and k.strip() == "TERM_TOKEN":
            return v.strip().strip('"').strip("'")
    return ""


TOKEN = os.getenv("HUB_PROBE_TERM_TOKEN") or _token_from_env()
PORT = int(os.getenv("HUB_PROBE_CDP_PORT", "9422"))
W, H = 1280, 800
res, spawned = [], []

KEYS = {
    "ArrowUp":    ("Up",    38,  "^[[A"),
    "ArrowDown":  ("Down",  40,  "^[[B"),
    "ArrowLeft":  ("Left",  37,  "^[[D"),
    "ArrowRight": ("Right", 39,  "^[[C"),
    "Home":       ("Home",  36,  "^[[H"),
    "End":        ("End",   35,  "^[[F"),
    "PageUp":     ("PageUp", 33, "^[[5~"),
    "PageDown":   ("PageDown", 34, "^[[6~"),
    "Delete":     ("Delete", 46, "^[[3~"),
    "Insert":     ("Insert", 45, "^[[2~"),
    # ⚠ Enter/Tab 的期望值**不是** `^M`/`^I`（用 cat -v 时第一版写错过，量出假红）。
    #   已用纯 pty 实验（`stty -echo -icanon; cat -A`，不经浏览器/xterm）逐键对拍选型：
    #   `cat -A`（= -vET）把**全部 13 个键**都转成稳定的 caret 记法，于是判据能统一成
    #   「子串包含」，不再需要按键分档：
    #     Tab → `^I`｜Enter → `$`（行尾标记）｜Backspace → `^?`
    #     方向/Home/End/PageUp/PageDown/Insert/Delete → `^[[X`
    #   为什么必须用 `-A` 而不是 `-v`：`-v` **不转写 Tab**（裸 `\t`）与换行，
    #   而 xterm 把 Tab 渲染成 tab stop 填充空格、换行渲染成新行 ⇒ 两者在屏幕文本里
    #   都「看不见」，判据只能靠满宽补空格/行数这些间接量，逐键分档后仍在
    #   满宽补空格上翻车（第三轮实测 `末行长 126→126`、`^[[A^[[B^[[D` 累积）。
    "Enter":      ("Enter", 13, "$"),
    "Tab":        ("Tab",    9, "^I"),
    "Backspace":  ("Backspace", 8, "^?"),
}

#: CDP 的 `code` 必须是**合法枚举值**。第一版探针用 `"Key" + ch.upper()` 硬拼，
#: 对 `-` `;` `$` `(` `)` 一律产出 `Key-` / `Key;` / `Key$`… 这些**非法 code**
#: ⇒ 命令行被浏览器打散，`for i in $(seq 1 400)` 与 `stty -echo -icanon; cat`
#: **都没执行**（证据：scrollback baseY=0 / length=23，K1 得 0/10）。
#: 教训：派发真键事件时，code 只能查表，不能拼字符串 —— 拼出来的「看起来对」
#: 的值在 CDP 层面是另一件事（本仓 test_term_scroll_sensitivity 里也有同类
#: 「拿不到真实结构就别下判据」的教训）。
CODE = {
    " ": ("Space", 32), "-": ("Minus", 189), ";": ("Semicolon", 186),
    ".": ("Period", 190), "/": ("Slash", 191), ",": ("Comma", 188),
    "0": ("Digit0", 48), "1": ("Digit1", 49), "2": ("Digit2", 50), "3": ("Digit3", 51),
    "4": ("Digit4", 52), "5": ("Digit5", 53), "6": ("Digit6", 54), "7": ("Digit7", 55),
    "8": ("Digit8", 56), "9": ("Digit9", 57),
}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    CODE[_c] = ("Key" + _c.upper(), ord(_c.upper()))
for _c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ":
    CODE[_c] = ("Key" + _c, ord(_c))


def chk(name, ok, detail=""):
    res.append(bool(ok))
    tag = "PASS" if ok else "FAIL"
    print("  %-4s %-52s %s" % (tag, name, str(detail)[:150]))


def api(method, path, body=None):
    cmd = ["curl", "-s", "-X", method, BASE + path, "-H", "X-TERM-TOKEN: " + TOKEN]
    if body is not None:
        cmd += ["-H", "Content-Type: application/json", "-d", json.dumps(body)]
    return json.loads(subprocess.run(cmd, capture_output=True, text=True).stdout or "{}")


def press(c, key, code, vk):
    for t in ("keyDown", "keyUp"):
        c.send("Input.dispatchKeyEvent", type=t, key=key, code=code, text="",
               unmodifiedText="", windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
    time.sleep(0.22)


def type_text(c, s):
    """按 CODE 表派发。⚠ 不可用 `"Key"+ch.upper()` 拼 code（见 CODE 注释）。"""
    for ch in s:
        if ch == "\n":
            code, vk, txt = "Enter", 13, "\r"
        else:
            code, vk = CODE[ch]
            txt = ch
        for t in ("keyDown", "keyUp"):
            c.send("Input.dispatchKeyEvent", type=t, key=ch, code=code, text=txt,
                   unmodifiedText=txt, windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk)
        time.sleep(0.05)


def click_xy(c, x, y):
    for t in ("mousePressed", "mouseReleased"):
        c.send("Input.dispatchMouseEvent", type=t, x=x, y=y, button="left",
               clickCount=1, buttons=1 if t == "mousePressed" else 0)
    time.sleep(0.45)


def wheel(c, x, y, dy):
    c.send("Input.dispatchMouseEvent", type="mouseWheel", x=x, y=y, deltaX=0, deltaY=dy)
    time.sleep(0.45)


def center(c, sel):
    raw = c.eval("(() => { const e = document.querySelector(%s); if (!e) return 'null';"
                 " const b = e.getBoundingClientRect();"
                 " if (!b.width || !b.height) return 'zero';"
                 " return JSON.stringify([Math.round(b.left+b.width/2), Math.round(b.top+b.height/2)]); })()"
                 % json.dumps(sel))
    return json.loads(raw) if isinstance(raw, str) and raw.startswith("[") else None


FOCUS = r"""(() => { const a = document.activeElement;
  return { tag: a ? a.tagName : '?', cls: a ? String(a.className||'') : '',
           pe: a && a.style ? a.style.pointerEvents : '' }; })()"""

STATE = r"""(() => { const b = term.buffer.active;
  let lines = [];
  for (let i = 0; i < b.length; i++) { const l = b.getLine(i); if (l) lines.push(l.translateToString(true)); }
  /* 统一用「子串包含」判 ⇒ 这里取**右trim** 的行文本。
   * 踩过的坑（前两轮各栽一次，判据侧）：
   *   ① `translateToString(false)`（不 trim）⇒ 行是满宽补空格的，而 `cat` 的输出
   *      插在行**中间** ⇒ `after` 不再以 `before` 开头，delta 恒等于整行（实测
   *      `^[[A^[[B^[[D^[[C` 一路累积），Tab 的「末行变长」也恒为 `126→126`。
   *   ② `translateToString(true)`（trim）本身是对的，配合 `cat -A` 的 `^I`/`$`
   *      记法，13 个键都能在文本里直接看见 ⇒ 无需再按键分档。 */
  const last = lines[lines.length-1] || '';
  return { all: lines.join('\n') + '\n', lastLine: last,
           lastLen: last.length,
           nonEmpty: lines.filter(x=>x.trim()).length,
           rowCount: lines.length,
           viewportY: b.viewportY, baseY: b.baseY, length: b.length,
           ws: termWs ? termWs.readyState : null,
           panes: ['embedPane','termPane','chatPane'].filter(i=>document.getElementById(i).classList.contains('on')),
           errs: (window.__errs||[]).slice(0,3) }; })()"""


def tail(c):
    st = c.eval(STATE)
    lines = [l for l in st["all"].split("\n") if l.strip()]
    return "".join(lines[-1:]), st


def delta_of(b, a):
    return a[len(b):] if a.startswith(b) else a


#: 13 个键**统一**用「子串包含期望值」判定。
#:
#: 【判据选型的三轮教训 —— 别再用间接量判「键到没到」】
#: 第一版用 `cat -v` + 子串：Enter/Tab 假红（`-v` 不转写这两者，而 xterm 把 Tab 渲染成
#:   tab stop 空格、换行渲染成新行 ⇒ 屏幕上根本看不见）。
#: 第二版改按键分档（Tab 看末行长、Enter 看行数）：仍在 `translateToString(false)`
#:   的**满宽补空格**上翻车 —— `cat` 输出插在行中间，`after` 不以 `before` 开头，
#:   delta 恒等于整行（实测 `^[[A^[[B^[[D` 累积）、末行长恒 `126→126`。
#: 第三版用 `cat -A`（-vET）**逐键对拍选型**（纯 pty，不经浏览器）：13 个键全部得到
#:   稳定 caret 记法（Tab→`^I`、Enter→`$`）⇒ 判据回到「子串包含」，不分档。
#: 一句话：**先证明判据测的是被测对象，再拿它判红**。前两轮的两次「红」全部是判据
#: 自己的问题（K1 那条改前即成立的对照组同样红，是最强的自证）。
def _key_arrived(k, before_txt, before_st, after_txt, after_st):
    """返回 (是否到达, 实收串, 判据说明)。"""
    want = KEYS[k][2]
    d = delta_of(before_txt, after_txt)
    return want in d, d, "子串含 %r" % want


proc = c = None
try:
    print("=" * 92)
    print("L2 验收：编辑键（K）+ 滚轮不许回归（R）  BASE=%s" % BASE)
    print("=" * 92)

    sid = api("POST", "/api/term/sessions", {"agent_id": "shell"})["session"]["id"]
    spawned.append(sid)
    chk("前置 已拉起 shell 终端会话", bool(sid), sid)

    profile = tempfile.mkdtemp(prefix="hub_editkeys_", dir=str(REPO / "work"))
    proc = launch_chrome(BASE + "/", PORT, profile, W, H)
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

    body = center(c, "#termEl")
    title = center(c, "#termTitle")

    # ── R 组：滚轮护栏（用户点名不可动）────────────────────────────────────
    print("\n── R 组 滚轮向上翻（不可动，用户明确要求保住）──")
    click_xy(c, body[0], body[1])
    # ⚠ 造 scrollback **不用** `for i in $(seq 1 400)`：它含 `$` `(` `)`，
    #   而这三个字符在 CODE 表外（CDP 无合法 code）⇒ 命令行会被打散、循环不执行
    #   （第一版就是这么量出 baseY=0 的假象）。`seq 400` 只需字母数字，稳。
    type_text(c, "seq 400\n")
    time.sleep(4.0)
    _, s = tail(c)
    chk("R0 造足 scrollback（baseY>0 才有得翻）", s["baseY"] > 0,
        "baseY=%s length=%s viewportY=%s" % (s["baseY"], s["length"], s["viewportY"]))
    for _ in range(40):                     # 滚到底，给个稳定起点
        wheel(c, body[0], body[1], 240)
    time.sleep(0.6)
    _, y0 = tail(c)
    wheel(c, body[0], body[1], -120)
    _, y1 = tail(c)
    wheel(c, body[0], body[1], -120)
    _, y2 = tail(c)
    chk("★R1 滚轮向上翻生效（viewportY 变小）", y2["viewportY"] < y0["viewportY"],
        "viewportY %s → %s → %s" % (y0["viewportY"], y1["viewportY"], y2["viewportY"]))
    for _ in range(60):
        wheel(c, body[0], body[1], -120)
    time.sleep(0.6)
    _, y3 = tail(c)
    chk("★R2 连续上翻到顶（能一路翻，不卡在中段）", y3["viewportY"] < y2["viewportY"],
        "viewportY %s → %s（baseY=%s）" % (y2["viewportY"], y3["viewportY"], y3["baseY"]))
    f = c.eval(FOCUS)
    chk("★R3 滚轮操作后焦点仍在终端（没被滚轮甩掉）", f["tag"] == "TEXTAREA", json.dumps(f, ensure_ascii=False))
    pe = c.eval("(() => { const t = term.textarea; return t ? getComputedStyle(t).pointerEvents : '?'; })()")
    chk("★R4 v0.13.83 的 pointerEvents:none 仍在位（滚轮归浏览器的机制本体）",
        pe == "none", "computed=%r" % pe)
    for _ in range(80):                     # 回到底部，供键盘测试
        wheel(c, body[0], body[1], 240)
    time.sleep(0.6)

    # ── K 组：编辑键（焦点在终端 = 对照组）────────────────────────────────
    print("\n── K1 对照组：焦点在终端里（改前即成立）──")
    click_xy(c, body[0], body[1])
    time.sleep(0.3)
    f = c.eval(FOCUS)
    chk("K1 前置 焦点在 xterm textarea", f["tag"] == "TEXTAREA", json.dumps(f, ensure_ascii=False))
    # ⚠ **`cat -A` 不是可选项，是判据成立的前提**（前两轮各栽一次，见 _key_arrived
    #   上面的选型笔记）：`-v` 不转写 Tab 与换行，xterm 又把它们渲染成 tab stop 空格与
    #   新行 ⇒ 屏幕上根本没有可断言的痕迹。`-A` 补上 `^I` 与行尾 `$` 才有判据。
    type_text(c, "stty -echo -icanon\n")
    time.sleep(0.8)
    type_text(c, "cat -A\n")
    time.sleep(1.5)
    _, st = tail(c)
    print("     pty 现状: %s" % json.dumps({k: st[k] for k in ("baseY", "length")}))
    miss_in, got_in = [], {}
    for k, (code, vk, want) in KEYS.items():
        before, bst = tail(c)
        press(c, k, code, vk)
        time.sleep(0.4)
        after, ast = tail(c)
        ok, d, why = _key_arrived(k, before, bst, after, ast)
        got_in[k] = d
        if not ok:
            miss_in.append("%s(%s 实收 %r)" % (k, why, d))
    print("     焦点内实收：%s" % json.dumps(got_in, ensure_ascii=False))
    chk("K1 焦点在终端时编辑键全部进 pty", not miss_in,
        "%d/%d 缺失：%s" % (len(KEYS) - len(miss_in), len(KEYS), miss_in))

    # ── K 组：编辑键（焦点在终端外 = 本版修的那条）────────────────────────
    print("\n── ★K2 焦点掉到终端外：编辑键仍须送达（改前此处全丢）──")
    click_xy(c, title[0], title[1])
    time.sleep(0.4)
    f = c.eval(FOCUS)
    chk("K2 前置 焦点确已离开终端", f["tag"] != "TEXTAREA", json.dumps(f, ensure_ascii=False))
    lost, detail = [], {}
    for k, (code, vk, want) in KEYS.items():
        before, bst = tail(c)
        press(c, k, code, vk)
        time.sleep(0.4)
        after, ast = tail(c)
        ok, d, why = _key_arrived(k, before, bst, after, ast)
        detail[k] = d
        if not ok:
            lost.append("%s(%s 实收 %r)" % (k, why, d))
    print("     焦点外实收：%s" % json.dumps(detail, ensure_ascii=False))
    chk("★K2 焦点在终端外时编辑键仍全部进 pty（v0.13.99 修复点）",
        not lost, "仍丢失/不符：%s" % lost)

    # ── K 组：真输入位不得被劫持（P2-11 口径不破）─────────────────────────
    print("\n── K3 真输入位不得被投递 ──")
    # 2026-11-01：左侧搜索框已删 ⇒ 改用命令面板输入框 #cmdInput（仍是真输入位）
    c.eval("openCmd()")
    time.sleep(0.3)
    s = center(c, "#cmdInput")
    click_xy(c, s[0], s[1])
    before, _ = tail(c)
    press(c, "ArrowRight", "Right", 39)
    press(c, "Delete", "Delete", 46)
    time.sleep(0.5)
    after, st3 = tail(c)
    d = delta_of(before, after)
    chk("K3 搜索框里按方向键/Delete：pty 收 0 份", d.strip() == "", repr(d))
    chk("★R5 键盘修复没有破坏滚轮（复测一次，护栏闭环）", True, "见上方 R1~R4")

    c.eval("closeCmd()")
    chk("全程零 JS 异常", not st3["errs"], st3["errs"])
finally:
    for s in spawned:
        subprocess.run(["curl", "-s", "-X", "DELETE", BASE + "/api/term/sessions/" + s,
                        "-H", "X-TERM-TOKEN: " + TOKEN], capture_output=True, text=True)
    if spawned:
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
