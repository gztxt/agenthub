"""toast 的无障碍闸门（2026-10-05）。

【为什么值得单独一个文件】
`toast()` 是全站**唯一**的错误/成功通知通道（`01-core-boot.js:283`，**134 处调用**），
而改前 `templates/index.html` 的 `<div id="toast">` **没有任何** `aria-live` / `role`
⇒ 屏幕阅读器用户完全听不到「加载失败」「已保存」「已连接」这类关键提示。
这不是「锦上添花的 a11y」，而是**唯一的用户反馈通道对一部分人静默**。

【⚠ 本闸门要守的不只是「加了属性」，还有「没顺手改坏别的东西」】
`toast()` 的返回值**被消费**：`01-core-boot.js:290` 明确 `return el`，注释写
「供『链路恢复后收掉同一条提示』用」，实际消费方在 `03-agents-cards.js:184`
与 `02-nav-and-poll.js:221`。而 `03:180-182` 的注释警告过：不要给 toast 加参数，
会波及 40+ 调用方。
⇒ 所以本批**只改容器属性**。闸门把「签名不许变」「返回值仍被消费」一并钉住，
否则下一次「顺手优化」很容易把这两个一起改掉。

【为什么刻意不加 aria-atomic="true"】
加了它，每次追加新 toast 都会**重播整个容器里的全部内容**（多条同时出现时
体验反而更差）。`polite` + 不设 atomic 才是「只念新增的那条」。
"""
import re
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))
from _js_min import strip_comments  # noqa: E402

TPL = _REPO / "templates" / "index.html"
CORE = _REPO / "static" / "hub" / "01-core-boot.js"
ACT = _REPO / "static" / "hub" / "03-agents-cards.js"
NAV = _REPO / "static" / "hub" / "02-nav-and-poll.js"


def _toast_el() -> str:
    m = re.search(r"<div id=\"toast\"[^>]*>", TPL.read_text(encoding="utf-8"))
    assert m, "模板里找不到 #toast 容器"
    return m.group(0)


class ToastIsALiveRegion(unittest.TestCase):
    """#toast 必须是 live region，否则屏幕阅读器听不到任何提示。"""

    def test_container_has_role_status(self):
        el = _toast_el()
        self.assertRegex(el, r"role=[\"']status[\"']",
                         "#toast 缺 role=\"status\"（或 role=alert）")

    def test_container_has_aria_live(self):
        el = _toast_el()
        self.assertRegex(el, r"aria-live=[\"']polite[\"']",
                         "#toast 缺 aria-live=\"polite\"")

    def test_container_does_not_use_alert(self):
        """⚠ 刻意**不用** role="alert"/aria-live="assertive"。

        alert 会**打断**屏幕阅读器当前的朗读。toast 是高频通道（成功/失败/进度
        都走它）， assertive 会让用户在一条错误后连着被 5 条成功提示打断。
        错误要抢话的话，正确做法是在 toast() 里按 cls 路由到另一个容器 ——
        但那要改 134 个调用方能看到的热路径，留作后续批。
        """
        el = _toast_el()
        self.assertNotRegex(el, r"aria-live=[\"']assertive[\"']",
                            "toast 容器不该用 assertive（会打断当前朗读）")
        self.assertNotRegex(el, r"role=[\"']alert[\"']",
                            "toast 容器不该用 role=alert（见 docstring）")

    def test_aria_atomic_is_not_true(self):
        """⚠ 加 aria-atomic="true" 会让每次追加**重播整个堆栈** —— 反而更差。"""
        el = _toast_el()
        self.assertNotRegex(el, r"aria-atomic=[\"']true[\"']",
                            "aria-atomic=true 会重播整堆 toast，体验更差")


class ToastSignatureUnchanged(unittest.TestCase):
    """⚠ 返回值被消费，签名不许动（134 个调用方）。"""

    def setUp(self):
        self.src = strip_comments(CORE.read_text(encoding="utf-8"))
        m = re.search(r"^function toast\(.*?\n\}", self.src, re.M | re.S)
        self.assertIsNotNone(m, "找不到 toast()")
        self.body = m.group(0)

    def test_signature_is_two_required_plus_optional_tail(self):
        """v0.13.185：允许**可选**第三参 opts（动作按钮/同文去重）。

        约束从"只能两个参数"放宽为"前两参不许动 + 第 3 参起必须带默认值"：
        134 个旧调用只传 (msg, cls)，只要新增参数可选就零波及；而加必填参数
        会让全站提示变成 undefined（这才是原测试真正要拦的东西）。
        """
        m = re.search(r"^function toast\(([^)]*)\)", self.body, re.M)
        self.assertIsNotNone(m)
        params = [p.strip() for p in m.group(1).split(",") if p.strip()]
        names = [p.split("=")[0].strip() for p in params]
        self.assertEqual(names[:2], ["msg", "cls"],
                         "toast() 前两参必须是 msg, cls，实际 %s"
                         "（03-agents-cards.js:180-182 的注释警告过：改名/加必填会波及 40+ 调用方）"
                         % names)
        for extra in params[2:]:
            self.assertIn("=", extra,
                          "第 3 参起必须带默认值：%r —— 旧调用不传会得到 undefined" % extra)

    def test_still_returns_the_node(self):
        self.assertRegex(self.body, r"return\s+el\b",
                         "toast() 必须仍返回节点：03:184 / 02:221 消费它来"
                         "「链路恢复后收掉同一条提示」")

    def test_callers_still_consume_return_value(self):
        """反向自证：消费方还在用返回值（否则「不许改签名」这条约束就失去对象）。"""
        found = False
        for p in (ACT, NAV):
            src = strip_comments(p.read_text(encoding="utf-8"))
            for m in re.finditer(r"=\s*toast\(", src):
                found = True
        self.assertTrue(found,
                        "没有任何地方消费 toast() 的返回值 ⇒ 「签名不许动」已失去意义，"
                        "请复核这几处是不是被改成了普通调用")

    def test_container_id_unchanged(self):
        """id 不能变：toast() 与 CSS 都按 #toast 定位。"""
        self.assertRegex(self.body, r"\$\(['\"]toast['\"]\)",
                         "toast() 不再按 #toast 定位（id 被改了？）")


class OtherA11yBaseline(unittest.TestCase):
    """顺带钉住「两个抽屉已有 aria-modal」这一既有基线不被回退。

    为什么放这里：`#toast` 与两个抽屉是同一页上的浮层类元素。抽屉那套
    （role=dialog + aria-modal + tabindex）已由 `tests/test_overlay_a11y.py`
    16 例钉着；这里只做一条交叉断言，防止「给 toast 加 a11y」时误改抽屉。
    """

    def test_drawers_keep_their_dialog_semantics(self):
        src = TPL.read_text(encoding="utf-8")
        for did in ("detailDrawer", "skillDocDrawer"):
            i = src.find('id="%s"' % did)
            self.assertNotEqual(i, -1, "找不到抽屉 %s" % did)
            seg = src[max(0, i - 300):i + 300]
            self.assertIn('role="dialog"', seg, "%s 丢了 role=dialog" % did)
            self.assertIn('aria-modal', seg, "%s 丢了 aria-modal" % did)


if __name__ == "__main__":
    unittest.main(verbosity=2)
