"""顶栏右侧「在跑 N · 会话 M」的**间距不变量**（2026-10-09 用户报「间距太大」）。

【报障与实测】用户只有一句「在跑 1·会话 1 间距太大」。真 chromium + CDP 逐节点量 rect
（探针 `work/probe_hbusy_gap2.py` / `probe_hbusy_shot.py`，注入真实 innerHTML 后量），改前：

| 量点 | 改前 | 改后 |
|---|---|---|
| 蓝点 →「在跑」 | 5px | 4px |
| 「在跑 1」→「·」 | 7px | 1px |
| 「·」→「会话 1」 | 7px | 1px |
| #hBusy 整块宽 | 115.2px | 102.2px |
| 版本号右缘 →「在跑」左缘 | **28px**（gh-link 1219 → hBusy 1247） | 14px（与别处一致） |

【两处成因都不是"数字写错了"，而是两条默认行为的副作用】
 ① `gap` 是**每两个相邻子节点**之间的间距，不是"块与块"。`#hBusy` 的内容是
    「匿名文本 + span + 匿名文本 + span」，于是 `gap:5px` 落在**每一段**之间，
    再叠 `.hbusy-sep` 自己的 `margin: 0 2px` ⇒ 分隔符两侧 7px。
 ② **零宽度的 flex item 照样吃两侧 gap**。`#hStale` / `#hErrors` 常态为空，却仍各占
    一个 gap 位 ⇒ 版本号与「在跑」之间凭空多出一整份 gap（宽屏 14px，窄屏 6px）。

【为什么用静态闸门，而不是只留 L2 探针】L2 探针要真 chromium + 在跑的服务，改 CSS 的人
不会顺手跑；而这两条的失效方式都**安静**——数字全对，只是看着散。故静态门钉住**机制**：
①空挂载点不吃 gap；②#hBusy 内部不留 gap（间距由蓝点自己的 margin 单独给）。

【正对照】同族假绿：「搜不到就当通过」。若后人把 `#hStale`/`#hErrors` 挂载点整个删掉，
断言①会**自动变绿**（没有空 span 就没有空态问题）——故配一条「三块挂载点仍在」的存在性
断言。又：`#hBusy` 的间距若被改成"给分隔符加 margin"而不是"归零 gap"，断言②与④
会一起红，正是要拦住这种"换个地方把间距加回来"的等价回退。

【红向自证怎么复跑】把改前的模板当红基线（本批留了
`templates/index.html.bak-20261009_150139-pre-hbusy-gap`）：造一棵 `templates/` + `tests/`
两目录的假仓、把 `.bak` 复制成 `templates/index.html`、本文件复制进 `tests/`，
在那棵树里跑本用例 ⇒ 应为 **4 红 1 绿**（绿的是 `test_mount_points_still_exist`）。
"""
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
HTML_RAW = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")

#: 分隔符两侧允许的最大水平 margin（v0.13.90 起它是"不抢眼"的省略件，不是装饰）。
SEP_MARGIN_MAX = 1.0
#: 蓝点与「在跑」之间允许的最大间距（太小=糊在一起，太大=用户报的"散"）。
DOT_OFFSET_MAX = 6.0

_HTML_COMMENT = re.compile(r"<!--.*?-->", re.S)
_CSS_COMMENT = re.compile(r"/\*.*?\*/", re.S)


def _code() -> str:
    """剥掉 HTML 与 CSS 注释后的模板正文。

    必须剥：本文件自己的 docstring 与本批加的 CSS 注释里逐字写了 `margin: 0 2px`、
    `gap:5px` 这些**反例内容**，不剥就是拿反例当正例抓（test_term_box_gap.py 同款坑）。
    """
    return _CSS_COMMENT.sub("", _HTML_COMMENT.sub("", HTML_RAW))


def _rules(selector: str):
    """抽出模板里所有 `selector { … }` 的声明体（按出现顺序）。"""
    return re.findall(re.escape(selector) + r"\s*\{([^}]*)\}", _code())


def _decl(body: str, prop: str):
    m = re.search(r"(?:^|;)\s*" + re.escape(prop) + r"\s*:\s*([^;]+)", body)
    return m.group(1).strip() if m else None


def _px(v: str):
    m = re.fullmatch(r"(-?\d+(?:\.\d+)?)px", (v or "").strip())
    return float(m.group(1)) if m else None


def _horizontal_margin(v: str):
    """margin 简写的 1/2/3/4 值形式 → 左右两个值（px，取不到返回 None）。"""
    parts = (v or "").split()
    if len(parts) == 1:
        l = r = parts[0]
    elif len(parts) == 2:
        l = r = parts[1]
    elif len(parts) == 3:
        l = r = parts[1]
    else:
        l, r = parts[1], parts[3]
    return _px(l), _px(r)


class HeaderBusyGapTest(unittest.TestCase):
    def test_mount_points_still_exist(self):
        """正对照：三块状态挂载点必须还在（否则下面那条空态断言会假绿）。"""
        for mid in ("hStale", "hBusy", "hErrors"):
            self.assertIn('id="%s"' % mid, HTML_RAW,
                          '#%s 挂载点不见了 ⇒ 空态那条断言变成"搜不到就当通过"' % mid)

    def test_empty_slots_do_not_eat_gap(self):
        """空挂载点不得占 flex 位：`:empty` 必须 display:none。

        零宽度的 flex item 照样吃两侧 gap —— 这正是「版本号到在跑 28px」的成因。
        """
        bodies = [b for b in _rules(".hstats > span:empty") if _decl(b, "display")]
        self.assertTrue(bodies,
                        "缺 `.hstats > span:empty { display: none }` ⇒ 空挂载点会在两侧"
                        "各吃一个 gap（宽屏 14px×2、窄屏 6px×2），版本号与「在跑」之间凭空变宽")
        self.assertTrue(any(_decl(b, "display") == "none" for b in bodies),
                        "`.hstats > span:empty` 存在但没把它 display:none")

    def test_busy_inner_gap_is_zero(self):
        """#hBusy 内部不留 gap —— 间距只允许由蓝点那一条 margin 提供。

        父级 `.hstats > span { gap:5px }` 会落到 #hBusy 的每个子节点（含匿名文本）上，
        实测「在跑 1」→「·」被撑到 7px。归零后由 .hdot 的 margin 单独给点与字之间的间距。
        """
        bodies = _rules(".hstats #hBusy")
        self.assertTrue(bodies, "找不到 `#hBusy` 的间距规则")
        gaps = [_decl(b, "gap") for b in bodies if _decl(b, "gap")]
        self.assertIn("0", gaps,
                      "#hBusy 的 gap 不是 0 ⇒ 父级 5px 会再次落到「在跑 N」「·」「会话 M」"
                      "每一段之间（用户报的就是这个「间距太大」）")

    def test_dot_keeps_its_own_offset(self):
        """蓝点与「在跑」之间的间距必须由它自己的 margin 给，且收在 DOT_OFFSET_MAX 内。"""
        bodies = _rules(".hstats #hBusy .hdot")
        self.assertTrue(bodies, "找不到 #hBusy 内 .hdot 的 margin 规则")
        vals = []
        for b in bodies:
            l, r = _horizontal_margin(_decl(b, "margin-right") or _decl(b, "margin") or "")
            vals.append(r)
        vals = [v for v in vals if v is not None]
        self.assertTrue(vals, "#hBusy 内 .hdot 没有可解析的水平 margin ⇒ gap 归零后点会贴着「在跑」")
        self.assertLessEqual(max(vals), DOT_OFFSET_MAX,
                             "蓝点与「在跑」的间距 %s px 超过 %s —— 这正是用户报的「散」"
                             % (max(vals), DOT_OFFSET_MAX))

    def test_separator_margins_are_tight(self):
        """「·」两侧水平 margin ≤ 1px，且必须仍在淡化（v0.13.90：它不抢眼、可被省略）。"""
        bodies = _rules(".hstats .hbusy-sep")
        self.assertTrue(bodies, "分隔符规则不见了 ⇒ 两个数会粘成「在跑 1会话 1」（v0.13.90 反例）")
        self.assertTrue(any(_decl(b, "opacity") for b in bodies),
                        "分隔符不再淡化 ⇒ 它开始抢眼（v0.13.90 的裁定）")
        worst = None
        for b in bodies:
            l, r = _horizontal_margin(_decl(b, "margin") or "")
            for v in (l, r):
                if v is None:
                    continue
                worst = v if worst is None else max(worst, v)
        self.assertIsNotNone(worst, "分隔符的 margin 取不到水平值（写法换了？）")
        self.assertLessEqual(worst, SEP_MARGIN_MAX,
                             "「·」两侧水平 margin %s px > %s ⇒ 两个数字看起来分家了"
                             % (worst, SEP_MARGIN_MAX))


if __name__ == "__main__":
    unittest.main()