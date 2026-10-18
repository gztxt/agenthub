#!/usr/bin/env python3
"""探针：把「Cmd/Ctrl+C 有选区」改成 return false（不 preventDefault，交给原生 copy 事件）
后，浏览器原生复制链在**真页面**里是否把选区写进 clipboardData？

为什么必须真页面：候选新口径依赖 xterm 自己在 `.xterm` 元素上注册的 copy handler
（vendor 实证：`copyHandler(e,t){e.clipboardData.setData('text/plain',t.selectionText),
e.preventDefault()}`）。它只在浏览器真的派发 copy 事件时才会跑。

判据：
  N0 终端实例就绪、有选区
  N1 Cmd/Ctrl+C 后 copy 事件真的派发到 .xterm（xterm 的 copy handler 在跑）
  N2 冒泡末端读到的 clipboardData 文本 == 终端选区文本（= 浏览器将要写进剪贴板的内容）
  N3 无 preventDefault 的 keydown 之后，xterm 没有把 ^C 送进 pty（term.onData 收不到 \\x03）
  N4 对照（旧口径）：preventDefault + JS 复制同样能写 clipboardData（红向自证两条路可区分）
"""
import json
import sys, os, tempfile, time
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _cdp_min import CDP, launch_chrome, page_target

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
REAL = "--real" in sys.argv
PORT = 9574
FAILED = []


def chk(name, ok, detail=""):
    print(("  PASS " if ok else "  FAIL ") + name + ("  " + detail if detail else ""))
    if not ok:
        FAILED.append(name)


def key(cdp, k, code, vk, modifiers, commands=None):
    p = dict(type="keyDown", key=k, code=code, windowsVirtualKeyCode=vk,
             nativeVirtualKeyCode=vk, modifiers=modifiers)
    if commands:
        p["commands"] = commands
    cdp.send("Input.dispatchKeyEvent", **p)
    cdp.send("Input.dispatchKeyEvent", type="keyUp", key=k, code=code,
             windowsVirtualKeyCode=vk, nativeVirtualKeyCode=vk, modifiers=modifiers)


INSTRUMENT = r"""(function(){
  window.__n = {copyEvt: 0, copyTgt: '', lastClip: null, sent: [], pasteEvt: 0, pasteCalls: []};
  /* .xterm 上的 copy 是 xterm 自己注册的；这里只观测，不改它的行为 */
  var el = document.getElementById('termEl');
  el.addEventListener('copy', function(){ __n.copyEvt++;
    __n.copyTgt = (document.activeElement && document.activeElement.className) || '';
    __n.copyEl = String(window.__n.copyEl || '') + '|' + (event.target && event.target.className);
  });
  /* document 冒泡末端 = 全链最后一个监听器 ⇒ 读到的就是最终 clipboardData */
  document.addEventListener('copy', function(e){
    try { __n.lastClip = e.clipboardData ? e.clipboardData.getData('text/plain') : null; } catch (x) {}
    __n.sawPreventDefault = e.defaultPrevented;
  });
  var od = term.onData(function(d){ __n.sent.push(String(d)); });
  var op = window.termPasteText;
  window.termPasteText = function(t){ __n.pasteCalls.push(String(t).slice(0,40)); return op.apply(this, arguments); };
  term.textarea.addEventListener('paste', function(){ __n.pasteEvt++; }, true);
  return true;
})()"""


def main():
    prof = tempfile.mkdtemp(prefix="copynative")
    proc = launch_chrome(BASE + "/", PORT, prof, 1280, 900)
    try:
        return run(proc)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=8)
        except Exception:
            proc.kill()


def run(proc):
    cdp = CDP(page_target(PORT), on_event=lambda m, p: None)
    cdp.send("Page.enable")
    time.sleep(5.0)
    print("  INFO readyState:", cdp.eval("document.readyState"), "go:", cdp.eval("typeof go"))
    # 前端在缺口令时会 prompt() —— headless 里 prompt 会阻塞渲染线程，Runtime.evaluate 直接无应答。
    # 本探针只测前端复制通道，不需要真会话，塞一个假口令跳过 prompt 即可（后端 403 不影响 xterm 实例）。
    cdp.eval("localStorage.setItem('hub.term.token','probe-dummy')")
    cdp.send("Page.reload", ignoreCache=True); time.sleep(4.0)
    cdp.eval("go('chat')"); time.sleep(0.6)
    # 取第一个 agent 建终端视图（不需要真会话：只测前端复制通道）
    cdp.eval("(function(){var a=(window.AGENTS||[])[0]; if(a) pickChatEntity(a.id); return !!a;})()")
    time.sleep(0.6)
    try:
        cdp.eval("switchMode('term')")
    except Exception:
        cdp.eval("applyChatMode()")
    time.sleep(2.0)
    if not cdp.eval("(typeof term!=='undefined' && !!term)"):
        cdp.eval("ensureTerm()")
    time.sleep(1.0)
    chk("N0 xterm 实例就绪", bool(cdp.eval("(typeof term!=='undefined'&&!!term)")))

    # 真鼠标点一下终端（= 用户取焦路径；文本区不是原生可点元素，必须先有焦点）
    r = cdp.eval("(function(){var e=document.getElementById('termEl').getBoundingClientRect();"
                 "return {x:Math.round(e.left+e.width/2),y:Math.round(e.top+e.height/2)};})()")
    for t in ("mousePressed", "mouseReleased"):
        cdp.send("Input.dispatchMouseEvent", type=t, x=r["x"], y=r["y"], button="left", clickCount=1)
    time.sleep(0.5)
    print("  INFO focus:", cdp.eval("(function(){var a=document.activeElement;return a? (a.tagName+'.'+(a.className||'')) : 'null';})()"))
    # 本地写入一段可辨识文本并选中（不进 pty）
    cdp.eval("term.reset(); term.write('MAC_COPY_SEL_2026\\r\\n'); true"); time.sleep(0.8)
    cdp.eval("term.select(0,0,16); true"); time.sleep(0.3)
    chk("N0b 选区建立", cdp.eval("term.hasSelection()") is True,
        repr(cdp.eval("term.getSelection()")))
    cdp.eval(INSTRUMENT)

    # 模拟「候选新口径」：有选区时 keydown 只 return false，不 preventDefault
    if REAL:
        print("  INFO --real：不替换 handler，直接量生产已部署的 hub.js")
    else:
        cdp.eval("term.attachCustomKeyEventHandler(function(e){"
             "if(e.type==='keydown'&&(e.ctrlKey||e.metaKey)&&!e.altKey){"
             "var k=String(e.key||'').toLowerCase(); if(k==='c'&&term.hasSelection()) "
             "{ window.__n.preventDefaultCalled=false; return false; }"
             "if(k==='v') return false; } return true; }); true")
    cdp.eval("window.__n.sent=[];true")
    key(cdp, "c", "KeyC", 67, 2, commands=["copy"])
    time.sleep(0.8)
    n = cdp.eval("({copyEvt:__n.copyEvt, clip:__n.lastClip, pd:__n.sawPreventDefault, sent:__n.sent, "
                 "sel:term.getSelection()})")
    chk("N1 原生 copy 事件派发（xterm 的 copy handler 会跑）", n["copyEvt"] > 0, "copyEvt=%s" % n["copyEvt"])
    want = cdp.eval("term.getSelection()")
    chk("N2 clipboardData == 终端选区", n["clip"] == want,
        "clip=%r want=%r" % (n["clip"], want))
    chk("N2b copy 事件被 xterm preventDefault（默认动作不会覆盖写入的数据）", n["pd"] is True,
        "defaultPrevented=%s" % n["pd"])
    chk("N3 ^C 没有进 pty", not any("\x03" in s for s in (n["sent"] or [])), "sent=%s" % n["sent"][:3])

    # 粘贴：meta 修饰符，走原生 paste → termPasteText
    cdp.eval("window.__n.pasteEvt=0; window.__n.pasteCalls=[];"
             "term.textarea.focus(); true")
    key(cdp, "v", "KeyV", 86, 4, commands=["paste"])
    time.sleep(0.8)
    p = cdp.eval("({evt:__n.pasteEvt, calls:__n.pasteCalls})")
    chk("N4 Cmd+V 走通原生粘贴通道", p["evt"] > 0 and bool(p["calls"]),
        "pasteEvt=%s calls=%s" % (p["evt"], p["calls"][:2]))

    # N5：无选区 Ctrl+C 必须仍进 pty（SIGINT 语义不许被这次修复破坏）
    cdp.eval("term.clearSelection(); window.__n.sent=[]; term.textarea.focus(); true"); time.sleep(0.3)
    key(cdp, "c", "KeyC", 67, 2)
    time.sleep(0.6)
    sent2 = cdp.eval("window.__n.sent")
    chk("N5 无选区 Ctrl+C 仍进 pty（SIGINT 语义）", any("\u0003" in s for s in (sent2 or [])),
        "sent=%s" % [repr(x)[:10] for x in (sent2 or [])[:3]])

    errs = cdp.eval("(window.__DIAG_LOG__||[]).slice(-3)")
    print("  INFO diag:", json.dumps(errs, ensure_ascii=False)[:300])

    cdp.close()
    print("\n%s" % ("全绿" if not FAILED else "失败：" + ", ".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
