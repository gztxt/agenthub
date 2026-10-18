#!/usr/bin/env python3
"""L2 live 真渲染闸门：终端剪贴板「原生 copy 链 + 原生粘贴通道」。

v0.13.126 起键盘复制改判：有选区 Cmd/Ctrl+C 只 `return false`（不 preventDefault），
复制由浏览器原生 `copy` 事件 + xterm 自带的 copyHandler 完成 ⇒ 本闸门不能再拿
`termCopyFallback` 当复制判据，改为**直接量 copy 事件与事件末端的 clipboardData**。
（v0.13.90 那套「JS 写剪贴板 + 焦点归还」仍被覆盖：菜单/OSC 52 走那条路，见 A3/B3。）

跑法（生产或影子实例，真 chromium + CDP）：
    HUB_PROBE_TERM_TOKEN=<token> python tests/verify_term_clipboard.py http://192.168.5.102:3102

为什么必须真渲染：本次两个缺陷都**只在真浏览器里成立**，静态断言证明不了 ——
  · 「复制一次之后所有按键都哑」的机制是 **焦点**（document.activeElement）被
    execCommand 兜底的临时 textarea 抢走；DOM 里没有任何痕迹可静态判。
  · 「Ctrl+V 不 work」的分岔点是 **是否 preventDefault** 导致浏览器原生粘贴被掐断，
    以及 navigator.clipboard 在非安全上下文是否 undefined —— 都只在运行时成立。

判据（全部可断言量，不以截图交差）：
  A 键盘复制不动焦点：Ctrl/Cmd+C 前后 focus 仍=termTA（原生链不建临时 textarea）
  B 原生 copy 事件真的派发（xterm 的 copyHandler 是落地接收方）
  B2 事件末端 clipboardData == 终端选区文本
  A3/B3 菜单/OSC 52 的同步兜底仍能写剪贴板 + 归还焦点
  C 原生粘贴通道可用：真 Ctrl+V ⇒ paste 事件计数增加 + termPasteText 被调用
  D 有选区 Ctrl+V 也走通
  E 无选区 Ctrl+C 仍放行给 pty（不破坏 SIGINT 语义）
  F 全程零 JS 异常
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from _cdp_min import CDP, launch_chrome, page_target   # noqa: E402

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
TOK = os.environ.get("HUB_PROBE_TERM_TOKEN", "")
PORT = int(os.getenv("HUB_PROBE_CDP_PORT", "9457"))

FAILED = []


def chk(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        FAILED.append(name)


def key(cdp, k, code, vk, mods, text=None, commands=None):
    p = dict(type="keyDown", key=k, code=code, windowsVirtualKeyCode=vk,
             nativeVirtualKeyCode=vk, modifiers=mods)
    if text is not None:
        p["text"] = text
    if commands:
        p["commands"] = commands
    cdp.send("Input.dispatchKeyEvent", **p)
    cdp.send("Input.dispatchKeyEvent", type="keyUp", key=k, code=code,
             windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=mods)


INSTRUMENT = r"""(function(){
  window.__r = {copyFallbackResolved: [], pasteEvt: 0, pasteTextCalls: [],
                execCopyPayloads: null, errs: []};
  __r.copyEvt = 0; __r.copyClip = null;
  /* v0.13.126：键盘复制改走浏览器原生 copy 链 ⇒ 闸门必须能看见 copy 事件
     与**事件末端**的 clipboardData（xterm 的 copyHandler 与容器兜底都在中间写入；
     只在 document 冒泡末端才能读到最终值）。 */
  document.addEventListener('copy', function(e){
    __r.copyEvt++;
    try { __r.copyClip = e.clipboardData ? e.clipboardData.getData('text/plain') : null; } catch (x) {}
  });
  /* 判据要能区分「走了哪条路」：安全上下文（loopback/https）走 navigator.clipboard，
     非安全上下文（局域网 http，用户实际访问方式）只能走 execCommand 兜底 ——
     本次缺陷正在兜底那条路上，所以两条都要能被看见。 */
  __r.securePathUsed = false; __r.fallbackPathUsed = false;
  var oc = termCopyFallback;
  termCopyFallback = function(t){
    if (navigator.clipboard && navigator.clipboard.writeText) __r.securePathUsed = true;
    else __r.fallbackPathUsed = true;
    var p = oc.apply(this, arguments);
    if (p && p.then) p.then(function(v){ __r.copyFallbackResolved.push(!!v); });
    return p;
  };
  var op = termPasteText;
  termPasteText = function(t){ __r.pasteTextCalls.push(String(t).slice(0, 30)); return op.apply(this, arguments); };
  term.textarea.addEventListener('paste', function(){ __r.pasteEvt++; }, true);
  window.__r.execCopyPayloads = [];
  var oe = document.execCommand;
  document.execCommand = function(c){
    if (c === 'copy') {
      var a = document.activeElement;
      __r.execCopyPayloads.push(a && a.value ? String(a.value).slice(0, 60) : '');
    }
    return oe.apply(this, arguments);
  };
  window.addEventListener('error', function(e){ __r.errs.push(String(e.message)); });
  return true;
})()"""


def focus_name(cdp):
    return cdp.eval("(function(){var a=document.activeElement;"
                    "if(!a) return 'null';"
                    "if(typeof term!=='undefined'&&term&&a===term.textarea) return 'termTA';"
                    "return a.tagName+'('+(typeof a.className==='string'?a.className:'')+')';})()")


def main():
    prof = tempfile.mkdtemp(prefix="clipver")
    proc = launch_chrome(BASE + "/", PORT, prof, 1280, 900)
    cdp = CDP(page_target(PORT), on_event=lambda m, p: None)
    cdp.send("Page.enable")
    time.sleep(2.0)
    if TOK:
        cdp.eval("localStorage.setItem('hub.term.token', %s)" % json.dumps(TOK))
    cdp.send("Page.reload", ignoreCache=True)
    time.sleep(3.5)
    cdp.eval("go('chat')"); time.sleep(0.4)
    cdp.eval("pickChatEntity('claude')"); time.sleep(1.0)
    cdp.eval("switchMode('term')"); time.sleep(3.0)

    ok_term = cdp.eval("(typeof term!=='undefined'&&!!term)")
    ok_ws = cdp.eval("(typeof termWs!=='undefined'&&termWs&&termWs.readyState===1)")
    chk("V0 终端已就绪（xterm 实例已在；有会话时才要求 ws 连通）", bool(ok_term),
        "term=%s ws=%s" % (ok_term, ok_ws))
    chk("V1 剪贴板路径实况（非安全上下文必走 execCommand 兜底）",
        True, "isSecureContext=%s navigator.clipboard=%s" % (
            cdp.eval("window.isSecureContext"), cdp.eval("!!navigator.clipboard")))

    # 点进终端
    r = cdp.eval("(function(){var e=document.getElementById('termEl').getBoundingClientRect();"
                 "return {x:Math.round(e.left+e.width/2),y:Math.round(e.top+e.height/2)};})()")
    for t in ("mousePressed", "mouseReleased"):
        cdp.send("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"], button="left", clickCount=1)
    time.sleep(0.5)
    if not ok_ws:
        print("  INFO 影子实例尚无终端会话 ⇒ 先拉一条（复制/粘贴通道需要真 pty）")
        cdp.eval("termNew()"); time.sleep(4.0)
        chk("V1b 会话已拉起且 ws 连通",
            cdp.eval("(typeof termWs!=='undefined'&&termWs&&termWs.readyState===1)"))
        for t in ("mousePressed", "mouseReleased"):
            cdp.send("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"], button="left", clickCount=1)
        time.sleep(0.5)
    chk("V2 点击终端后焦点落在 xterm 的 helper textarea 上", focus_name(cdp) == "termTA",
        "focus=%s" % focus_name(cdp))

    cdp.eval(INSTRUMENT)
    # 造一行可复制内容
    cdp.eval("term.input('echo CLIPVERIFY\\r')"); time.sleep(1.8)
    ln = cdp.eval("(function(){var b=term.buffer.active;for(var i=0;i<b.length;i++){"
                  "var l=b.getLine(i);if(l&&l.translateToString(true).indexOf('CLIPVERIFY')>=0)return i;}"
                  "return -1;})()")
    cdp.eval("term.select(0,%d,25)" % max(0, ln)); time.sleep(0.4)
    chk("V3 选区建立", cdp.eval("term.hasSelection()") is True)

    # A/B 复制（v0.13.126：键盘复制走浏览器原生 copy 链，不再由 JS 写剪贴板）
    f0 = focus_name(cdp)
    cdp.eval("window.__r.copyEvt=0; window.__r.copyClip=null; true")
    key(cdp, "c", "KeyC", 67, 2, commands=["copy"]); time.sleep(1.0)
    f1 = focus_name(cdp)
    chk("A 复制不动焦点（原生链不建临时 textarea ⇒ 不会把后续按键弄哑）", f1 == "termTA",
        "复制前=%s 复制后=%s" % (f0, f1))
    evt = cdp.eval("window.__r.copyEvt")
    chk("B 原生 copy 事件真派发（xterm 自带的 copyHandler 是落地接收方）", evt > 0,
        "copyEvt=%s（0 ⇒ 浏览器没把 Cmd/Ctrl+C 当复制命令）" % evt)
    clip = cdp.eval("window.__r.copyClip")
    chk("B2 clipboardData 是终端选区文本（事件末端读值）",
        bool(clip) and "CLIPVERIFY" in clip, "clip=%r" % (clip if clip is None else clip[:40]))

    # A2/B3：右键菜单「复制」/OSC 52 走的那条 JS 路仍要能写剪贴板并归还焦点
    cdp.eval("window.__r.copyFallbackResolved=[]; window.__r.fallbackPathUsed=false;"
             " window.__r.copyEvt=0; window.__r.copyClip=null; void termCopySelection(); true")
    time.sleep(0.8)
    res = cdp.eval("window.__r.copyFallbackResolved")
    chk("B3 菜单/OSC 52 的同步兜底仍能写剪贴板", res == [True], "resolved=%s" % res)
    chk("A3 兜底归还焦点", focus_name(cdp) == "termTA", "focus=%s" % focus_name(cdp))

    # C/D 粘贴（真 Ctrl+V）
    cdp.eval("term.clearSelection()"); time.sleep(0.3)
    e0 = cdp.eval("window.__r.pasteEvt")
    key(cdp, "v", "KeyV", 86, 2, commands=["paste"]); time.sleep(1.2)
    e1 = cdp.eval("window.__r.pasteEvt")
    chk("C 无选区 Ctrl+V 走通原生粘贴通道（改前 pasteEvt 恒 0）", e1 > e0,
        "pasteEvt %s -> %s" % (e0, e1))
    calls = cdp.eval("window.__r.pasteTextCalls")
    chk("C2 粘贴进了 bracketed-paste 安全包装（termPasteText 被调用）", bool(calls),
        "calls=%s" % calls[:3])

    # E 有选区 Ctrl+V 也要通
    cdp.eval("term.select(0,%d,25)" % max(0, ln)); time.sleep(0.3)
    e2 = cdp.eval("window.__r.pasteEvt")
    key(cdp, "v", "KeyV", 86, 2, commands=["paste"]); time.sleep(1.2)
    chk("D 有选区 Ctrl+V 同样走通", cdp.eval("window.__r.pasteEvt") > e2)

    # 无选区 Ctrl+C 必须仍然进 pty（SIGINT 语义）
    cdp.eval("term.clearSelection()"); time.sleep(0.3)
    sent_before = cdp.eval("(window.__sent||[]).length")
    cdp.eval("window.__sent=[]; term.onData(function(d){window.__sent.push(String(d));});")
    key(cdp, "c", "KeyC", 67, 2); time.sleep(0.8)
    sent = cdp.eval("window.__sent")
    chk("E 无选区 Ctrl+C 仍放行给 pty（不破坏 SIGINT）", any("\u0003" in s for s in (sent or [])),
        "sent=%s" % [repr(s)[:12] for s in (sent or [])[:3]])

    errs = cdp.eval("window.__r.errs")
    chk("F 全程零 JS 异常", not errs, "errs=%s" % errs[:2])

    cdp.close(); proc.terminate()
    print("\n%s（%d 项失败）" % ("全绿" if not FAILED else "有 FAIL：" + ", ".join(FAILED), len(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
