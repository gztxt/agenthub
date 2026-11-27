"""右侧内容区留白收窄 60%（v0.13.97，用户 2026-10-09「右边终端框上下左右边距缩小 60%」）。

【为什么要有这个文件】这条需求的口语是「边距」，而实测证明**右侧内容的留白
只有一个来源**：`main` 的 padding。逐层分解（1440×900 真渲染，见
`tests/verify_term_box_geom.py`）：

    上(main内) 13px = main padding-top 12 + 内容框边框 1
    左(main内) 17px = main padding-left 16 + 内容框边框 1
    .chat-grid padding 四向 0、gap 16px —— 但它**已是单列**（.chat-side display:none
      且 #page-chat 宽屏规则覆盖成 minmax(0,1fr)）⇒ gap 不产生任何列间距
    #termEl / .term-body padding 四向 0，且被上一轮定案钉死（FitAddon 按 #termEl
      内容盒算行数，父层多 1px 就多算一行、底部被裁）

所以「缩边距」只有一条合法改法：收窄 `main` 的 padding。

【范围：全站，不是只有终端页 —— 这是本版被用户否掉过一次的地方】
第一版写成 `main:has(> #page-chat.on)` 限定在终端工作台。用户当日复核后否掉：
「点左边菜单栏的其他菜单时右边页面边距又变了，应该是全部统一才对」。
`:has()` 在这里是**多余的第二判据**：它让同一份留白出现两种值（全站一种、终端页一种），
而用户要的就是同一个值。**教训**：`:has()` 是有价值的工具，但用它做「某页特殊化」之前
必须先问「用户说的是这一页，还是所有页」—— 本例默认答案是「所有页」。

【本闸门卡的五条，都是「会静默失效」的那类】
 1 **不得存在任何 main 的 padding 覆写**（含 `:has()` / `section.page` 限定）。
   只要有一条覆写，右侧留白就会在不同页面取不同值 —— 正是用户否掉第一版的原因。
 2 宽屏基准 `--main-pad-y/x` 必须是收窄后的 4.8px/6.4px（原 12/16 的 40%）。
 3 窄屏基准必须是 3.2px/3.2px（原 8/8 的 40%）—— **不是同一个绝对值**。
 4 `--main-pad-*` 是**唯一真相源**：main 上不得再有裸 padding 字面量。
 5 **FitAddon 铁律仍在**：#termEl / .term-body 的 padding 必须四向 0。
   这是本次改动最容易误伤的地方 —— 「终端框边距」很容易被实现成「给黑底加内衬」，
   而那会让终端底部被裁掉一行。
"""
import re
import unittest

from _narrow_media import block_containing
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
HTML = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")

BREAKPOINT = 767

#: 收窄后的基准（= 原值 ×0.4）。宽屏原 12/16、窄屏原 8/8。
WIDE = {"y": "4.8px", "x": "6.4px"}
NARROW = {"y": "3.2px", "x": "3.2px"}

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code() -> str:
    """剥掉 HTML 与 CSS/JS 注释后的模板正文。

    必须剥：本文件自己在注释里写了 `main:has(> #page-chat.on)`、裸 padding 字面量、
    「缩边距」等**反例内容**，不剥就是在给自己抓自己（test_narrow_first_paint.py
    已因此假红过两次，见其 docstring）。
    """
    return _CSS_COMMENT.sub("", _HTML_COMMENT.sub("", HTML))


def _rules(selector: str):
    """抽出模板里所有 `selector { … }` 的声明体（按出现顺序）。"""
    return re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", _code())


def _decl(body: str, prop: str):
    m = re.search(re.escape(prop) + r"\s*:\s*([^;]+);", body)
    return m.group(1).strip() if m else None


class MainPaddingNarrowedTest(unittest.TestCase):
    def test_no_padding_override_anywhere(self):
        """**不得存在 main 的 padding 覆写** —— 一有覆写，留白就会因页而异。

        这正是第一版被否掉的原因：`main:has(> #page-chat.on)` 让终端页是 4.8/6.4、
        其他页仍是 12/16，用户点别的菜单就看出边距变了。范围是**全站**，
        所以判据是「一条覆写都不许有」，而不是「覆写内容对不对」。
        """
        for sel in ("main:has(> #page-chat.on)", "main:has(section.page.on)",
                    "main:has(#page-chat)", "section.page.on", "section.page"):
            over = [b for b in _rules(sel)
                    if any(_decl(b, p) for p in ("padding", "padding-top",
                                                "padding-right", "padding-bottom",
                                                "padding-left"))]
            self.assertEqual(over, [],
                             "存在含 padding 的 %r 覆写 ⇒ 右侧留白会因页而异，"
                             "而用户要的是全站统一。命中：%s" % (sel, over))

    def test_wide_base_is_narrowed_to_40_percent(self):
        """宽屏基准必须是 4.8px/6.4px（原 12/16 的 40%），且 padding 由变量算出。"""
        bodies = _rules("main")
        base = [b for b in bodies if "--main-pad-y" in b]
        self.assertTrue(base, "找不到带 --main-pad-* 的 main 规则")
        self.assertEqual(_decl(base[0], "--main-pad-y"), WIDE["y"],
                         "宽屏上下留白应是 %s（12×0.4），实际 %r"
                         % (WIDE["y"], _decl(base[0], "--main-pad-y")))
        self.assertEqual(_decl(base[0], "--main-pad-x"), WIDE["x"],
                         "宽屏左右留白应是 %s（16×0.4），实际 %r"
                         % (WIDE["x"], _decl(base[0], "--main-pad-x")))
        self.assertEqual(_decl(base[0], "padding"), "var(--main-pad-y) var(--main-pad-x)",
                         "main 的 padding 必须由变量算出，不要写裸值")

    def test_narrow_base_is_narrowed_proportionally(self):
        """窄屏基准必须是 3.2px/3.2px（原 8/8 的 40%），且落在窄屏块内。

        窄屏原基准是 8px 而非 12/16，所以**不是同一个绝对值** —— 写成 4.8/6.4
        会让窄屏只缩到 40%×(8/12)，不是用户要的 60%。
        """
        # 2026-11-28：按「块里实际含 --main-pad-y」定位，不再取第一个 767 块
        narrow_body = block_containing(HTML, "--main-pad-y", what="窄屏 padding 基准")
        bodies = _rules("main")
        narrow = [b for b in bodies if "--main-pad" in b and b != bodies[0]]
        self.assertTrue(narrow, "窄屏档没有覆盖 --main-pad-*")
        self.assertTrue(any(_decl(b, "--main-pad-y") == NARROW["y"]
                            and _decl(b, "--main-pad-x") == NARROW["x"] for b in narrow),
                        "窄屏基准应是 %s/%s（8×0.4），实际：%s"
                        % (NARROW["y"], NARROW["x"],
                           [(_decl(b, "--main-pad-y"), _decl(b, "--main-pad-x"))
                            for b in narrow]))
        # 且窄屏那条必须在 @media 块内，否则宽屏也会拿到 3.2
        self.assertIn("--main-pad-y: %s" % NARROW["y"], narrow_body,
                      "窄屏基准不在窄屏 @media 块内 ⇒ 宽屏会被误改成 3.2px")

    def test_padding_token_is_single_sourced(self):
        """`--main-pad-*` 是唯一真相源：main 上不得再有裸 padding 字面量。"""
        literals = []
        for b in _rules("main"):
            for prop in ("padding", "padding-top", "padding-right",
                         "padding-bottom", "padding-left"):
                v = _decl(b, prop)
                if v and not v.startswith("var("):
                    literals.append((prop, v))
        self.assertEqual(literals, [],
                         "main 上仍有裸 padding 字面量 %s ⇒ 与 --main-pad-* 是两份真相源"
                         % literals)

    def test_fitaddon_padding_stays_zero(self):
        """**FitAddon 铁律仍在**：#termEl / .term-body 的 padding 必须四向 0。

        「终端框边距」很容易被实现成「给黑底加内衬」，而那会让终端底部被裁掉一行。
        """
        for sel in ("#termEl", ".term-body"):
            bodies = _rules(sel)
            self.assertTrue(bodies, "找不到 %s 的规则" % sel)
            for b in bodies:
                for prop in ("padding", "padding-top", "padding-right",
                             "padding-bottom", "padding-left"):
                    v = _decl(b, prop)
                    if v is None:
                        continue
                    self.assertEqual(v, "0",
                                     "%s 的 %s 变成了 %r ⇒ 踩 FitAddon 行数口径，"
                                     "终端底部会被裁掉一行" % (sel, prop, v))

    def test_breakpoint_value_is_single_sourced(self):
        """断点仍只有 767px 一个取值（分档偏好不变量③，本次不得顺手新增分档）。"""
        pairs = re.findall(r"@media \(max-width[ :]+(\d+)px\) \{(.*?)\n        \}", HTML, re.S)
        main_blocks = [int(n) for n, b in pairs if re.search(r"(^|\s)main\s*\{", b)]
        self.assertTrue(main_blocks, "找不到管辖 main 的 @media 块")
        self.assertEqual(sorted(set(main_blocks)), [BREAKPOINT],
                         "管辖 main 的 @media 断点有多个取值：%s" % sorted(set(main_blocks)))


if __name__ == "__main__":
    unittest.main()