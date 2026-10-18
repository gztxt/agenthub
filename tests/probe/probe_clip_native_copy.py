#!/usr/bin/env python3
"""探针：浏览器原生 copy 事件在「无 DOM 选区」时是否会触发？

背景：终端选区是 xterm 自己的（canvas 上画的），DOM 里没有文本选区。
若原生 Cmd+C 仍会派发 copy 事件，则复制可以完全交给浏览器（clipboardData.setData），
不需要 navigator.clipboard / execCommand —— 这条路在 http、Safari、Mac 上都成立。
反之就必须在 keydown 里自己写剪贴板。

判据：
  A 聚焦 textarea + 无 DOM 选区 + copy 命令 → copy 事件是否触发
  B 聚焦 textarea + value 全选 + copy 命令 → copy 事件触发且 clipboardData 有文本
  C copy 监听器 setData 后，粘贴到另一个输入框，值是否为 setData 的内容
  D 以上在 meta 修饰符（Mac 的 Cmd）下同样成立
"""
import sys
import os
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

PORT = 9571
BASE = "data:text/html," + """
<html><body>
<textarea id=ta style="width:300px;height:60px"></textarea>
<textarea id=tb style="width:300px;height:60px"></textarea>
<script>
window.__log = {copy: [], paste: []};
document.addEventListener('copy', e => {
  __log.copy.push({sel: String(document.getSelection()).slice(0,40),
                   taSel: (function(){var a=document.getElementById('ta');
                            return a.selectionStart+'-'+a.selectionEnd;})()});
  if (window.__injectCopy && e.clipboardData) e.clipboardData.setData('text/plain', window.__injectCopy);
});
document.addEventListener('paste', e => {
  __log.paste.push(((e.clipboardData&&e.clipboardData.getData('text'))||'').slice(0,60));
});
</script>
</body></html>
""".replace("\n", "")

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


def main():
    prof = tempfile.mkdtemp(prefix="clipnative")
    proc = launch_chrome(BASE, PORT, prof, 900, 600)
    cdp = CDP(page_target(PORT), on_event=lambda m, p: None)
    cdp.send("Page.enable")
    time.sleep(1.2)

    # A：聚焦 ta，无选区
    cdp.eval("document.getElementById('ta').focus(); document.getElementById('ta').value=''; true")
    time.sleep(0.2)
    cdp.eval("window.__log.copy=[]")
    key(cdp, "c", "KeyC", 67, 2, commands=["copy"])
    time.sleep(0.5)
    a = cdp.eval("window.__log.copy.length")
    chk("A 无 DOM 选区 ⇒ copy 事件", a > 0, "copyEvt=%s（0=浏览器认为无可复制内容）" % a)

    # B：value 全选
    cdp.eval("var t=document.getElementById('ta');t.value='HELLO_NATIVE';"
             "t.focus();t.setSelectionRange(0,t.value.length); true")
    time.sleep(0.2)
    cdp.eval("window.__log.copy=[]")
    key(cdp, "c", "KeyC", 67, 2, commands=["copy"])
    time.sleep(0.5)
    b = cdp.eval("window.__log.copy")
    chk("B 有选区 ⇒ copy 事件带选区", bool(b) and b[0].get("taSel") == "0-12", "log=%s" % b[:1])

    # C：copy 监听器注入口径 → 粘贴到 tb（DOM 选区路径）
    cdp.eval("window.__injectCopy='INJECTED_BY_COPY_EVENT';"
             "var t=document.getElementById('ta');t.focus();t.setSelectionRange(0,1); true")
    key(cdp, "c", "KeyC", 67, 2, commands=["copy"])
    time.sleep(0.4)
    cdp.eval("var b=document.getElementById('tb');b.value='';b.focus();window.__log.paste=[];true")
    key(cdp, "v", "KeyV", 86, 2, commands=["paste"])
    time.sleep(0.5)
    c = cdp.eval("({pb:document.getElementById('tb').value, log:window.__log.paste})")
    chk("C copy 事件 setData 真进了剪贴板", c["pb"] == "INJECTED_BY_COPY_EVENT", "pasteVal=%r" % c["pb"])

    # D：meta（4）修饰符 —— Mac 的 Cmd，其余同上
    cdp.eval("window.__injectCopy='META_INJECTED';"
             "var t=document.getElementById('ta');t.value='XYZ';t.focus();t.setSelectionRange(0,3);true")
    key(cdp, "c", "KeyC", 67, 4, commands=["copy"])
    time.sleep(0.4)
    cdp.eval("var b=document.getElementById('tb');b.value='';b.focus();window.__log.paste=[];true")
    key(cdp, "v", "KeyV", 86, 4, commands=["paste"])
    time.sleep(0.5)
    d = cdp.eval("({pb:document.getElementById('tb').value, log:window.__log.paste})")
    chk("D meta(Cmd) 修饰符下同样成立", d["pb"] == "META_INJECTED", "pasteVal=%r" % d["pb"])

    cdp.close()
    proc.terminate()
    print("\n%s" % ("全绿" if not FAILED else "失败：" + ", ".join(FAILED)))
    return 1 if FAILED else 0


if __name__ == "__main__":
    sys.exit(main())
