#!/usr/bin/env python3
"""P2-9 取证：跨帧的 UTF-8 字符与跨帧的 DECSET，共用解码器 + 扫描尾巴能不能救回来。
   需要本机 chromium（不需要服务）。

为什么这不是"性能优化"：每帧 `new TextDecoder()` 拿不到"半个字符"的跨帧状态，
所以中文/制表符/emoji 只要恰好被 WS 帧劈开，断点处就固定吐 U+FFFD —— 是**画面错误**。
同理 `\x1b[?1003h` 跨帧时两帧都匹配不上全局正则 ⇒ 实时 TUI 开了鼠标跟踪却没被记下，
上报被闸门吃掉，表现为"vim 里滚轮没反应"。

四段断言（新旧口径同场对比，缺一红）：
  1 对照组·旧解码：逐帧 new TextDecoder() ⇒ 出现 U+FFFD（证明确有此缺陷）
  2 实验组·真代码：termDecodeReset()+termDecodeFrame() ⇒ 字符串与原文完全相等
  3 对照组·旧扫描：逐帧单独 termScanMouseMode() ⇒ termMouseLive 仍 false（漏记）
  4 实验组·真代码：termScanMouseFrame() 带尾巴 ⇒ termMouseLive 变 true
被测函数全部由 tests/_hub_extract.py 从 static/hub.js **原样抽取**，手抄不算数。
"""
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import _hub_extract as HX                                     # noqa: E402

REPO = Path(__file__).resolve().parents[1]
VENDOR = REPO / "static" / "vendor"
CHROME = (os.environ.get("HUB_CHROME") or shutil.which("chromium")
          or shutil.which("chromium-browser") or shutil.which("google-chrome-stable"))

WANT = [
    ("line", "let termMouseLive = false;"),
    ("line", "const termMouseWant = new Set();"),
    ("line", "let termDecoder = new TextDecoder();"),
    ("line", "let termScanTail = '';"),
    ("line", "const TERM_MOUSE_MODES ="),
    ("line", "const TERM_DECSET_RE ="),
    ("fn", "termScanMouseMode"),
    ("fn", "termScanMouseFrame"),
    ("fn", "termDecodeReset"),
    ("fn", "termDecodeFrame"),
]

# 被测函数的外部依赖桩：这些是运行时全局状态/缓存副作用，不属于被测代码本身。
# termSid 置空 ⇒ termScanMouseMode 的缓存分支短路（本探针只测扫描/解码，不测缓存）。
# 被测函数仍由 _hub_extract 原样抽取，桩只是让它们能在最小页里独立执行。
STUBS = r"""
var termSid = '';
var termSidAgent = '';
var termBracketed = false;
function termWantCachePut() {}
"""

BODY = r"""
function bytesOf(s) { return new TextEncoder().encode(s); }
(async () => {
  const TEXT = '中文终端输出█▓ 断点在这里';
  const by = bytesOf(TEXT);
  // 找一个落在多字节字符中间的切点（'中' 的第 2 个字节处）
  let cut = 1; for (let i = 1; i < by.length; i++) { if ((by[i] & 0xC0) === 0x80) { cut = i + 1; break; } }
  const p1 = by.slice(0, cut), p2 = by.slice(cut);

  /* 1 对照：旧写法，每帧一个新解码器 */
  const oldOut = new TextDecoder().decode(p1) + new TextDecoder().decode(p2);
  /* 2 实验：真代码（抽出来的），一条连接内共用 */
  termDecodeReset();
  const newOut = termDecodeFrame(p1) + termDecodeFrame(p2);

  /* 3 对照：DECSET 跨帧，逐帧单独扫（改前的行为） */
  termMouseLive = false; termScanTail = '';
  const seq = '\x1b[?1003h reset';
  const sb = new TextEncoder().encode(seq.slice(0, 6)), eb = new TextEncoder().encode(seq.slice(6));
  termScanMouseMode(new TextDecoder().decode(sb));
  termScanMouseMode(new TextDecoder().decode(eb));
  const oldLive = termMouseLive;
  /* 4 实验：走 termScanMouseFrame（带尾巴） */
  termMouseLive = false; termScanTail = '';
  const d = new TextDecoder();
  termScanMouseFrame(d.decode(sb, { stream: true }));
  termScanMouseFrame(d.decode(eb, { stream: true }));
  const newLive = termMouseLive;

  document.getElementById('out').textContent = 'RESULT ' + JSON.stringify({
    cut, TEXT, oldOut, newOut, oldLive, newLive,
    oldHasFFFD: oldOut.indexOf('\uFFFD') >= 0, newHasFFFD: newOut.indexOf('\uFFFD') >= 0
  });
})();
"""


def main():
    if not CHROME:
        print("  环境不满足：找不到 chromium（HUB_CHROME=/path 可指定）")
        return 2
    src = HX.read_hub()
    parts, miss = [], []
    for kind, what in WANT:
        got = HX.extract_line(src, what) if kind == "line" else HX.extract_function(src, what)
        (parts if got else miss).append(got or what)
    if miss:
        print("  环境不满足：hub.js 里抽不到这些片段（被改名/删除？逐条列出）")
        for m_ in miss:
            print("     ", m_[:60])
        return 2
    html = HX.build_page("\n".join(parts) + "\n" + STUBS, BODY)
    with tempfile.TemporaryDirectory() as td:
        shutil.copytree(VENDOR, Path(td) / "vendor")
        page = Path(td) / "sd.html"
        page.write_text(html, encoding="utf-8")
        cmd = [CHROME, "--headless=new", "--disable-gpu", "--no-sandbox",
               "--disable-dev-shm-usage", "--virtual-time-budget=6000",
               "--dump-dom", "file://" + str(page)]
        try:
            out = subprocess.run(cmd, capture_output=True, text=True, timeout=120,
                                 errors="replace").stdout
        except Exception as e:
            print(f"  chromium 调用失败：{type(e).__name__}: {e}")
            return 2
    m = re.search(r"RESULT (\{.*\})</pre>", out, re.S)
    if not m:
        print("  没拿到结果，页面尾部：\n" + out[-500:])
        return 2
    d = json.loads(m.group(1))
    fails = 0

    def check(name, ok, detail=""):
        nonlocal fails
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
        if not ok:
            fails += 1

    print("  切点在第 %s 字节（劈在多字节字符中间）" % d["cut"])
    print("  旧解码结果：%r" % d["oldOut"])
    print("  新解码结果：%r" % d["newOut"])
    check("对照：逐帧 new TextDecoder() 确实产生乱码（缺陷真实存在）",
          d["oldHasFFFD"], "含 U+FFFD")
    check("真代码路径：跨帧共用解码器后与原文逐字相等",
          d["newOut"] == d["TEXT"] and not d["newHasFFFD"], repr(d["newOut"][:24]))
    check("对照：DECSET 跨帧逐帧扫确实漏记（改前 termMouseLive 不更新）",
          d["oldLive"] is False)
    check("真代码路径：termScanMouseFrame 带尾巴后认出 1003h",
          d["newLive"] is True, f"termMouseLive={d['newLive']}")
    print("\n" + ("全部通过 ✅" if fails == 0 else f"失败 {fails} 项 ❌"))
    return 0 if fails == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
