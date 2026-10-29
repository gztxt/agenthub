#!/usr/bin/env python3
"""历史会话行「日期右对齐 + 绝对 M-DD」端到端取证（2026-10-30 用户裁定）。

判据（全部可断言，不看截图）：
  ① 行尾日期**每行同一列**：取每个 .hh-ts 的 getBoundingClientRect().right，
     极差 ≤ 1px（含长短标题、含带/不带目录尾名两种行）；
  ② 日期字面一律 MM-DD（今天/昨天也走绝对日期）——直接调页面里真实的 hhTime()，
     不给它任何注入的样本，验的就是线上那份实现；
  ③ 负例自证：把 .hh-t 改回旧样式 `flex:0 1 auto`，同样的行必须量出 >1px 极差
     （否则说明这套判据量不出差异，绿色不算数）。

用法：venv/bin/python tests/verify_hist_date_align.py   （L2，需 3102 已在跑）
门禁：不在 prepush 里自动跑（同 verify_hist_tdz —— 依赖真浏览器 + 活服务）。
"""
import json
import os
import sys
import tempfile
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.join(HERE, "..", "..")
sys.path.insert(0, os.path.join(ROOT, "tests"))
from _cdp_min import CDP, launch_chrome, page_target  # noqa: E402

URL = os.getenv("PROBE_URL", "http://127.0.0.1:3102/")
PORT = int(os.getenv("PROBE_CDP", "9512"))

# 用页面真实的 hhTime 生成日期字面；行的 DOM 结构与 05-chat-and-history.js 的 histHtml 一致
BUILD = """(() => {
  const now = Math.floor(Date.now() / 1000);
  const items = [
    {t: '短标题', cw: '',  ts: now},
    {t: '一个相当长的会话标题会被省略号截断掉一截', cw: 'agenthub', ts: now - 86400},
    {t: '中等长度标题', cw: '', ts: now - 86400 * 9},
    {t: '另一条同样中等', cw: 'techdocs', ts: now - 86400 * 40},
    {t: '', cw: '', ts: now - 86400 * 400}
  ];
  const box = document.createElement('div');
  box.className = 'nav-hist'; box.id = 'probe-hist';
  box.style.cssText = 'position:fixed;left:0;top:0;width:240px;z-index:99999;background:#000';
  box.innerHTML = items.map(it =>
    '<div class="hh-row"><span class="hh-t">' + it.t + '</span>' +
    '<span class="hh-cw">' + it.cw + '</span>' +
    '<span class="hh-ts">' + hhTime(it.ts) + '</span></div>').join('');
  document.body.appendChild(box);
  return {ok: typeof hhTime === 'function'};
})()"""

READ = """(() => {
  const rows = [...document.querySelectorAll('#probe-hist .hh-row')];
  return {
    texts: rows.map(r => r.querySelector('.hh-ts').textContent),
    rights: rows.map(r => Math.round(r.querySelector('.hh-ts').getBoundingClientRect().right * 100) / 100),
    box_right: Math.round(document.getElementById('probe-hist').getBoundingClientRect().right * 100) / 100
  };
})()"""

OLD = """(() => {                       // 负例：还原旧样式，确认判据能报出差异
  const st = document.createElement('style');
  st.textContent = '#probe-hist .hh-t{flex:0 1 auto!important}' +
                    '#probe-hist .hh-ts{margin-left:0!important}';   // 旧实现两条一起还原，只改一条量不出差异
  document.head.appendChild(st);
  return 'ok';
})()"""


def main():
    profile = tempfile.mkdtemp(prefix="probe-histalign-")
    proc = launch_chrome(URL, PORT, profile, 1440, 900)
    try:
        cdp = CDP(page_target(PORT))
        cdp.send("Page.enable")
        time.sleep(2.5)
        built = cdp.eval(BUILD)
        assert built["ok"], "页面里没有 hhTime：hub.js 没加载或被改写"
        new = cdp.eval(READ)
        cdp.eval(OLD)
        time.sleep(0.3)
        old = cdp.eval(READ)

        spread_new = round(max(new["rights"]) - min(new["rights"]), 2)
        spread_old = round(max(old["rights"]) - min(old["rights"]), 2)
        fmt_ok = all(__import__("re").fullmatch(r"(\d{2}-)?\d{2}-\d{2}", t) for t in new["texts"])

        print("日期字面 :", new["texts"])
        print("右边界   :", new["rights"], "容器右边界", new["box_right"])
        print("极差 新/旧: %.2f px / %.2f px" % (spread_new, spread_old))
        fails = []
        if not fmt_ok:
            fails.append("日期字面不是 MM-DD")
        if spread_new > 1.0:
            fails.append("右边界极差 %.2f > 1px" % spread_new)
        if spread_old <= 1.0:
            fails.append("负例没量出差异（判据失效，绿色不可信）")
        for f in fails:
            print("FAIL:", f)
        print("PASS" if not fails else "FAIL")
        return 0 if not fails else 1
    finally:
        proc.terminate()


if __name__ == "__main__":
    sys.exit(main())
