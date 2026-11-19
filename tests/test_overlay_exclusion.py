"""浮层唯一性闸门（2026-09-23 事故：窄屏「设置」抽屉盖掉 92% 画面且没人关）。

事故一句话：`#btnSettings` 走 inline `onclick="openSettings()"`，**绕开了侧栏的事件委托**，
而"导航/点击后收抽屉"这件事只写在委托里；`go()` 也从不清 `detailDrawer/settingsDrawer`。
于是手机上点一下设置 → 抽屉 `width:min(400px,92vw)` 在 390px 视口盖掉 359px，
`z-index:60` 把侧栏和正文全压住，唯一关闭路径是抽屉内的关闭按钮和 ESC —— 手机没 ESC
⇒ 用户观感就是「整页白板、完全点不动」。遮罩 `#sideMask` 还是 `z-index:45`，
在它下面，点哪儿都落在抽屉上，连"点空白逃生"都没有。

分层（口径见 tests/README.md）：
  L0 静态不变量：只读 static/hub.js 与 templates/index.html 的真文本，不起服务不用 node。
  L1 host：拿 git 历史里**真的有旁路的那一版**做红基线（内容搜索定位，不钉 HEAD，
      否则修完提交一次就把这条测试永远变绿）。
"""
import re
import subprocess
import sys
import unittest

from _js_min import strip_comments
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import tiers                                          # noqa: E402
from _hub_extract import extract_function, read_hub   # noqa: E402

REPO = Path(__file__).resolve().parents[1]
TPL = REPO / "templates" / "index.html"
# v0.13.27：+skillDocDrawer（技能正文抽屉）——闸门随浮层清单加严
DRAWERS = ["detailDrawer", "skillDocDrawer"]   # v0.13.43：设置抽屉已拆成手风琴 + 正文页


def _html():
    return TPL.read_text(encoding="utf-8")


def _git_blob(rev, path):
    r = subprocess.run(["git", "-C", str(REPO), "show", "%s:%s" % (rev, path)],
                       capture_output=True, text=True)
    return r.stdout if r.returncode == 0 else ""


def _rev_with(path, marker, since="HEAD"):
    """内容搜索：找出** blobs 里含该标记**的最近一次提交（红基线定位用）。"""
    r = subprocess.run(["git", "-C", str(REPO), "log", "--format=%H", "-S", marker,
                        since, "--", path], capture_output=True, text=True)
    for rev in r.stdout.split():
        if marker in _git_blob(rev, path):
            return rev
    return ""


class TestOverlayInvariants(unittest.TestCase):
    """L0：浮层唯一性的五条静态不变量。"""

    def setUp(self):
        self.hub = read_hub()
        self.html = _html()

    # ── ① 同一时刻最多一个抽屉在开 ──────────────────────────────────
    def test_openoverlay_closes_other_drawers_first(self):
        fn = extract_function(self.hub, "openOverlay")
        self.assertTrue(fn, "抽不到 openOverlay —— 抽屉必须经它开，否则这条闸门失去意义")
        self.assertIn("closeDrawers()", fn, "openOverlay 未先 closeDrawers()：两个抽屉可以叠加")
        self.assertLess(fn.index("closeDrawers()"), fn.index("add('on')"),
                        "closeDrawers() 必须排在 add('on') 之前，否则开新的同时旧的还开着")

    def test_no_drawer_opened_outside_openoverlay(self):
        """绕过 openOverlay 直接 add('on') 就等于放弃唯一性 —— 一律禁止。"""
        body = extract_function(self.hub, "openOverlay")
        pat = re.compile(r"\$\('(?:%s)'\)\s*\.classList\.add\('on'\)" % "|".join(DRAWERS))
        hits = [m.group(0) for m in pat.finditer(self.hub)]
        self.assertEqual(hits, [], "抽屉的开启动点散落在 openOverlay 之外: %s" % hits)
        self.assertIn("el.classList.add('on')", body or "")

    # ── ② 导航即清浮层 ────────────────────────────────────────────
    def test_go_clears_drawers(self):
        go = extract_function(self.hub, "go")
        self.assertTrue(go, "抽不到 go()")
        self.assertIn("closeDrawers", go, "go() 不清抽屉 ⇒ 导航后浮层残留（本次事故的正身）")
        self.assertRegex(go.replace("\n", " "), r"isNarrow[\s\S]{0,80}closeDrawers",
                        "go() 收抽屉须以窄屏为条件（桌面上抽屉是常驻面板，无脑收会改行为）")

    # ── ③ 侧栏内不得有绕过委托的 inline onclick ────────────────────
    def test_sidebar_items_have_no_inline_onclick(self):
        bad = []
        for m in re.finditer(r"<button[^>]*class=\"side-item[^\"]*\"[^>]*>", self.html):
            tag = m.group(0)
            if "onclick=" in tag:
                bad.append(re.sub(r"\s+", " ", tag)[:70])
        self.assertEqual(bad, [], "侧栏按钮用 inline onclick 旁路事件委托: %s" % bad)
        # v0.13.43：设置不再是侧栏常驻按钮 + 抽屉，而是手风琴第四组的三个子项
        # （data-sys ⇒ 侧栏委托 ⇒ go(page) ⇒ 正文出页，并顺带"窄屏点完收侧栏"）。
        # 抽屉形态正是 09-23 事故正身（手机上盖掉 92% 且无逃生路径），此处钉死"回不去"。
        self.assertNotIn('data-settings="1"', self.html, "「设置」抽屉入口又回来了")
        self.assertNotIn('id="settingsDrawer"', self.html, "设置抽屉又回来了")
        self.assertIn("settings: '设置'", self.hub, "侧栏缺「设置」手风琴组")
        self.assertIn("SET_PAGES", self.hub, "设置子页清单缺失")
        for sp in ("settings-github", "settings-logs"):
            self.assertIn("'%s'" % sp, self.hub, "设置子页 %s 不在清单里" % sp)
            self.assertIn('id="page-%s"' % sp, self.html, "#page-%s 缺失" % sp)

    # ── ④ 遮罩只有一个计算出口 ────────────────────────────────────
    def test_side_mask_has_single_writer(self):
        hits = re.findall(r"getElementById\('sideMask'\)[\s\S]{0,60}?classList\.toggle\('on'", self.hub)
        self.assertEqual(len(hits), 1, "sideMask 有 %d 个写入点，必须收敛到 syncOverlayMask 一处" % len(hits))
        fn = extract_function(self.hub, "syncOverlayMask") or ""
        self.assertIn("overlayAnyOpen", fn,
                      "遮罩只看侧栏态、不看抽屉态 ⇒ 抽屉盖住全屏时没有遮罩，用户无处可点")

    # ── ⑤ 点遮罩一次关干净（手机侧唯一逃生路径）──────────────────
    def test_mask_click_closes_everything(self):
        m = re.search(r"mask\.addEventListener\('click'[\s\S]{0,200}?\}\)|"
                      r"mask\.addEventListener\('click',[^\n]*\n", self.hub)
        self.assertTrue(m, "找不到 sideMask 的点击处理")
        blk = m.group(0)
        self.assertIn("closeDrawers", blk, "点遮罩只收侧栏、不收抽屉 ⇒ 手机上没有逃生路径")
        self.assertIn("apply(true)", blk, "点遮罩必须把侧栏收回 collapsed")

    # ── ⑥ 断点单一真源（沿用 09-23 侧栏事故的教训，别再造第二个 767）──
    def test_breakpoint_single_source(self):
        """只数 **代码里的 matchMedia 调用点**（注释里引用 CSS 不算第二源）。"""
        # ★承诺"注释里引用 CSS 不算第二源"，实现就必须真去注释（09-23 我在注释里写了
        # 一句 `(max-width: 767px)` 就被数成两源 ⇒ 判据与 docstring 不符属闸门缺陷）。
        js_code = strip_comments(self.hub)
        sites = re.findall(r"""matchMedia\(\s*['\"]\(max-width:\s*767px""", js_code)
        self.assertEqual(len(sites), 1,
                         "JS 里有 %d 个 767 断点定义 ⇒ 与 CSS 会各自漂移" % len(sites))
        self.assertNotIn("innerWidth < 768", self.hub, "又造了一个平行断点判据")
        self.assertIn("@media (max-width: 767px)", self.html, "CSS 断点必须仍是 767")


@tiers.host_only
class TestRedBaselineFromGit(unittest.TestCase):
    """L1：拿"真的有旁路"的那一版当红基线，证明这套不变量拦得住本次事故。"""

    def test_old_html_had_the_bypass(self):
        rev = _rev_with("templates/index.html", 'onclick="openSettings()"')
        if not rev:
            self.skipTest("git 里已找不到旁路版本（红基线已随历史压缩消失）")
        blob = _git_blob(rev, "templates/index.html")
        tag = re.search(r"<button[^>]*id=\"btnSettings\"[^>]*>", blob)
        self.assertTrue(tag, "红基线里找不到 btnSettings")
        self.assertIn("onclick=", tag.group(0), "红基线本应是旁路版，检查定位逻辑")
        self.assertNotIn("data-settings", blob, "红基线不该已有委托属性")
        print("\n  红基线 = %s（inline onclick 旁路，%d 字节 HTML）" % (rev[:8], len(blob)))

    def test_old_hub_had_no_overlay_guard(self):
        rev = _rev_with("static/hub.js", "classList.toggle('on', !c && narrow())")
        if not rev:
            self.skipTest("git 里已找不到平行遮罩写入点的版本")
        blob = _git_blob(rev, "static/hub.js")
        self.assertNotIn("function openOverlay", blob, "红基线不该已有 openOverlay")
        self.assertNotIn("closeDrawers", blob, "红基线里没有任何『导航清浮层』的路径")
        go = extract_function(blob, "go") or ""
        self.assertNotIn("closeDrawers", go, "红基线的 go() 正是残留浮层的那一处")
        print("  红基线 hub.js = %s（无浮层唯一性）" % rev[:8])


if __name__ == "__main__":
    unittest.main(verbosity=2)
