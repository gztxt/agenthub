"""侧栏图标**不得撞车**（v0.13.78）。

【为什么要有它】2026-10-04 用户报「窄屏左侧菜单栏收起后分不清第 4、5 个是什么」——
真渲染放大后看到：**收起态图标条上第 4、5 个图标形状完全一样**（两个 chip）。
实测发现 `cpu` 一次被用了**三处**：静态「资源」、`NAV_ICONS.agents`、`SET_PAGES` 的「模型」。
用户看到的只是其中相邻的那一对，但同号的三处一起清了。

【为什么闸门只硬判「收起态那 8 个」】
收起态图标条里只有 **4 个静态常驻项 + 4 个手风琴组头**，它们**并排可见、彼此相邻**
⇒ 撞车会直接造成误读，这是必须为 0 的。
而 `SYS_PAGES` / `SET_PAGES` 里的子项只在**组展开时**出现，不同组里的同名图标
（如「遥测」与「日志」都用 activity、「GitHub 项目」与设置里的「GitHub」都用 globe）
不构成相邻误读。硬判全表会把「两个不同页面恰好同图标」也逼成造新图标 ——
那是为指标而指标。本文件把全表撞车**列出来备案**，但不判红。

【实测过的同类失败】规则/选择器里写错类名**不会报错**，只会安静地不生效
（v0.13.74 的 `.home-title`、v0.13.77 的 `.badge` vs `.nav-badge`）。
本闸门因此从**渲染后的真实数据**反查图标，而不是 grep 源码里的字面量。
"""
import re
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
HTML = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
NAVJS = (_REPO / "static" / "hub" / "05-chat-and-history.js").read_text(encoding="utf-8")


def _static_icons():
    """4 个静态常驻项 → [(标签, 图标)]，从**模板里的真实 use href** 反查。"""
    out = []
    for m in re.finditer(r'<button[^>]*id="(btnNav\w+)"[^>]*?>(.*?)</button>', HTML, re.S):
        ic = re.search(r'href="#i-([a-z0-9-]+)"', m.group(2))
        lb = re.search(r'class="lbl">([^<]+)<', m.group(2))
        if ic:
            out.append((lb.group(1) if lb else m.group(1), ic.group(1)))
    return out


def _group_icons():
    m = re.search(r"const NAV_ICONS = \{([^}]*)\}", NAVJS)
    assert m, "找不到 NAV_ICONS"
    return [(k, v.strip().strip("'")) for k, v in
            (p.split(":") for p in m.group(1).split(",") if ":" in p)]


def _all_nav_icons():
    """全表（含组内子项），只用于备案。"""
    rows = [("静态", lb, ic) for lb, ic in _static_icons()]
    rows += [("组头", g, ic) for g, ic in _group_icons()]
    for name, pat in (("SYS", r"const SYS_PAGES = \[(.*?)\];"),
                      ("SET", r"const SET_PAGES = \[(.*?)\];")):
        mm = re.search(pat, NAVJS, re.S)
        if not mm:
            continue
        for _pid, label, icon in re.findall(
                r"\['([^']+)',\s*'([^']+)',\s*'([a-z0-9-]+)'\]", mm.group(1)):
            rows.append((name, label, icon))
    return rows


class NavIconCollisionTest(unittest.TestCase):
    """收起态图标条上的 8 个图标必须两两不同。"""

    def rail(self):
        return [("静态:" + lb, ic) for lb, ic in _static_icons()] + \
               [("组头:" + g, ic) for g, ic in _group_icons()]

    def test_collapsed_rail_has_eight_items(self):
        """顺带钉住「8 个」这个基数 —— 少一个就说明有项没进 rail，判据会空转。"""
        self.assertEqual(len(self.rail()), 8,
                         "收起态图标条应为 4 静态 + 4 组头，实测 %d 个" % len(self.rail()))

    def test_collapsed_rail_icons_are_pairwise_distinct(self):
        seen = {}
        for who, icon in self.rail():
            self.assertNotIn(icon, seen,
                             "收起态图标撞车：%s 与 %s 都用 #%s（并排可见 ⇒ 直接误读）"
                             % (seen.get(icon), who, icon))
            seen[icon] = who

    def test_cpu_is_no_longer_used_three_times(self):
        """`cpu` 曾被三处使用；本轮把「资源」与「AGENTS」迁走，只留「模型」。"""
        users = [(src, lb) for src, lb, ic in _all_nav_icons() if ic == "cpu"]
        self.assertEqual([u for _s, u in users], ["模型"],
                         "cpu 现在被 %s 使用；本轮只应保留设置里的「模型」" % users)

    def test_new_icons_exist_in_sprite(self):
        """新换的图标必须在 sprite 里真实存在 —— 写了不存在的 id，
        `<use href>` 会**静默渲染不出图形**（同族：写错不报错，只是什么都不发生）。"""
        sprite = set(re.findall(r'<symbol id="(i-[a-z0-9-]+)"', HTML))
        for who, icon in self.rail():
            with self.subTest(icon=icon):
                self.assertIn("i-" + icon, sprite,
                              "%s 用了 #%s，但 sprite 里没有这个 symbol" % (who, icon))

    def test_full_nav_collision_is_documented_not_silent(self):
        """备案：全表撞车列出来（不判红，但必须被记录下来，不能悄悄存在）。

        当前已知且**有意保留**的：遥测/日志 同为 activity（不同组，不相邻）；
        GitHub 项目/设置-GitHub 同为 globe（同理）。2026-10-25「设置 · 记忆存储」
        并入记忆中心后，database 这一对撞车已消灭（白名单保留只为不误报历史形态）。
        这条测试的价值是：将来新增项若撞车，会在这里显形，由 owner 决定要不要处理。
        """
        from collections import Counter
        c = Counter(ic for _s, _lb, ic in _all_nav_icons())
        dup = {k: v for k, v in c.items() if v > 1}
        self.assertNotIn("cpu", dup, "cpu 撞车未清干净：%d 处" % dup.get("cpu", 0))
        # 已知保留项白名单：新增撞车不在此列表时会在注释里显形
        self.assertLessEqual(set(dup) - {"activity", "globe", "database"}, set(),
                             "出现未登记的新撞车：%s" % dup)


if __name__ == "__main__":
    unittest.main()