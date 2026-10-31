"""情报卡「查看」必须真的看得见（2026-10-31 用户报「点击查看无反应」）。

真渲染取证（CDP，1440×900）：详情框原来在 46 张卡**后面**，展开后
`getBoundingClientRect().top=751.5`、`innerHeight=757` ⇒ 只有 6px 露在视口底边，
用户看到的就是「点了没反应」。列表越长越糟。

两条判据，缺一条都会复现：
1. **DOM 顺序**：`#refDetailBox` 必须出现在 `#refCards` 之前（列表再长也不把它顶下去）。
2. **落地滚动 + 封高**：`openRefCard` 要 `scrollIntoView`；`#refDetail` 要 `max-height` + `overflow:auto`
   （6000+ 字符的卡不封高会把整个视图顶走）。
"""

import pathlib
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
TM = (ROOT / "templates" / "index.html").read_text(encoding="utf-8")
JS = (ROOT / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")


class TestDetailPrecedesList(unittest.TestCase):
    def test_detail_box_before_cards(self):
        i_box = TM.index('id="refDetailBox"')
        i_cards = TM.index('id="refCards"')
        self.assertLess(i_box, i_cards,
                        "详情框必须排在卡片列表之前，否则 46 张卡会把它顶到视口外")

    def test_detail_is_height_capped_and_scrolls(self):
        seg = TM[TM.index('id="refDetail"'):]
        seg = seg[:seg.index("</pre>")]
        self.assertIn("max-height", seg, "长卡会顶走整个视图")
        self.assertIn("overflow:auto", seg.replace(" ", ""), "封高后必须自己能滚")


class TestOpenScrollsIntoView(unittest.TestCase):
    def test_open_ref_card_scrolls(self):
        seg = JS[JS.index("async function openRefCard"):]
        seg = seg[:seg.index("\n}\n")]
        self.assertIn("scrollIntoView", seg,
                      "展开后不滚到视口内 ⇒ 用户看到「点了没反应」")

    def test_close_hides_box(self):
        self.assertIn('data-ref-act="close"', TM)
        self.assertIn("detail.style.display = 'none'", JS)
