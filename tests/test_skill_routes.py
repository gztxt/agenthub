"""D1 闸门：21 路发现点（+条件 local 路）+ 排除清单 + state 四态 + glob 路。

设计书：docs/superpowers/specs/2026-10-03-skill-center-auto-invoke-design.md §4
计划：docs/superpowers/plans/2026-10-03-skill-center-auto-invoke.md Task 1

分层（tests/README.md）：
- L0 纯逻辑：路由表内容、排除清单形状、glob 展开、多根部分失败、_scan_one 的 reason 三态、
  **dedup 机制**（在 tempfile 里造软链重叠，不拿宿主形态当断言）
- L1 @host_only：真盘条数下限（防「把路指到错目录 ⇒ 静默 0 条」）、哈希路 glob 仍能解析

⚠ 三条曾经记错、已由本闸门抓出的事实（勿再改回去）：
  1. `find` **不跟随软链**（无 -L）⇒ opencode 明明 3 个软链、find 却报 0 条。真值 2 条。
     量技能条数只能用 hub 自己的 `_scan_one`（os.walk followlinks=True）。
  2. `qoderwake/runtime-generations/{A,B}` 与 `resources/builtin-skills` 是 **realpath 不同的
     同源副本**（各 11 条）⇒ 按软链去重压不下去，收进来等于把 11 条报成 44 条。已列入排除。
  3. `qoder-alpha/extensions/42d23c0fa380/` 是**内容哈希**目录，升级即变 ⇒ 必须走 glob。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))
sys.path.insert(0, str(_REPO))

import tiers  # noqa: E402

import skill  # noqa: E402


#: 21 路 = 原有 7 + D1 新增 13 + PT-20261019-01 补 1（codebuddy 只读路由）。
#: **逐个点名**，多一个少一个都判 FAIL（防悄悄加白名单）。
#: ⚠ 项目本地 `local` 路**不在此表**：它动态出现（有根才在 disk_routes() 里），
#: 由 tests/test_skill_invoke.py 的本地根用例专测。
EXPECTED_ROUTES = {
    # 原有 7 路
    "claude", "pi", "techdocs", "superpowers", "agents", "codex", "workbuddy",
    # D1 新增 13 路
    "hermes", "hermes-agent", "hermes-web", "jcode", "grok", "grok-bundled",
    "picoclaw", "qoder", "qoderwake", "qoderwake-shadow", "qoderwake-cli",
    "qoder-alpha", "opencode",
    # v0.13.127 PT-20261019-01：codebuddy（目录在、当前 0 条；禁改面禁写，只读路由）
    "codebuddy",
}

#: 各新路的**半量下限**（2026-10-03 `_scan_one` 实测值的一半，向下取整）。
#: 判 `>=` 而非判等：判等会在上游正常增删时变成噪声告警，半量下限只抓
#: 「指错目录 / 扫描崩了 / 被截断」这三类真故障。
FLOORS = {
    "hermes": 60,             # 实测 120
    "hermes-agent": 29,       # 实测 58
    "hermes-web": 11,         # 实测 22
    "jcode": 29,              # 实测 58
    "grok": 1,                # 实测 3
    "grok-bundled": 4,        # 实测 9
    "picoclaw": 4,            # 实测 8
    "qoder": 1,               # 实测 1
    "qoderwake": 5,           # 实测 11
    "qoderwake-shadow": 1,    # 实测 1
    "qoderwake-cli": 1,       # 实测 1
    "qoder-alpha": 1,         # 实测 2（glob 路）
    "opencode": 1,            # 实测 3（全软链；B3 证伪 opencode 不读它，但 hub 可见性仍在）
    # v0.13.127 PT-20261019-01：codebuddy 目录在、当前 0 条（禁改面禁写 ⇒ 空是预期形态）。
    # floor=0 不断言条数；「路在且 state=empty」由 test_codebuddy_route_reports_empty_not_missing 钉。
    "codebuddy": 0,
}


def _write_skill(root: Path, name: str) -> Path:
    d = root / name
    d.mkdir(parents=True, exist_ok=True)
    (d / "SKILL.md").write_text(
        f"---\nname: {name}\ndescription: 临时夹具 {name}\n---\n\n正文\n", encoding="utf-8")
    return d / "SKILL.md"


class _Whitelist:
    """把 tempfile 目录挂进 `_allowed_roots`。

    为什么要这个：`_scan_one` 有**硬白名单**（不在 21 路根（+local 路）之内的 realpath 一律计入
    `skipped_outside` 丢弃），这是既有安全设计不是缺陷。所以 L0 想在临时目录造夹具，
    只能临时把该目录加进白名单——**不为此改生产代码**。
    """

    def __init__(self, *dirs: str):
        self.dirs = [os.path.realpath(d) for d in dirs]
        self._orig = None

    def __enter__(self):
        self._orig = skill._allowed_roots
        skill._allowed_roots = lambda: list(self.dirs)
        return self

    def __exit__(self, *exc):
        skill._allowed_roots = self._orig
        return False


class TestRoutesL0(unittest.TestCase):
    """L0：只读本模块的字典，不碰真盘。"""

    def test_21_routes_exact(self):
        self.assertEqual(set(skill.SKILL_DIRS), EXPECTED_ROUTES)

    def test_21_routes_count(self):
        self.assertEqual(len(skill.SKILL_DIRS), 21)

    def test_local_route_absent_when_no_local_roots(self):
        """v0.13.127：项目本地 `local` 路**动态出现**——本地根一个都没有时，
        disk_routes() 不含它（不制造永久 missing 噪声行）；有根时才挂上。
        """
        import unittest.mock as mock
        with mock.patch.dict(os.environ, {"SKILL_LOCAL_ROOTS_JSON": "[]"}):
            self.assertEqual(skill.local_roots(), [])
            self.assertNotIn("local", skill.disk_routes())
        with tempfile.TemporaryDirectory() as td:
            (Path(td) / "skills").mkdir()
            with mock.patch.dict(os.environ,
                                 {"SKILL_LOCAL_ROOTS_JSON": json.dumps([td + "/skills"])}):
                self.assertEqual(skill.local_roots(), [td + "/skills"])
                self.assertIn("local", skill.disk_routes())

    def test_every_route_path_is_absolute(self):
        for r, p in skill.SKILL_DIRS.items():
            self.assertIsInstance(p, str, r)
            self.assertTrue(p.startswith("/"), r)

    def test_no_duplicate_route_paths(self):
        """同一目录登记成两路 ⇒ 列表里同一技能出现两次（dedup 之前），属配置错误。"""
        paths = [p for p in skill.SKILL_DIRS.values()]
        self.assertEqual(len(paths), len(set(paths)), "有路指向同一目录")

    def test_qoder_alpha_uses_glob_not_baked_hash(self):
        """哈希路径（extensions/42d23c0fa380）写死 ⇒ 升级当天变 missing。必须带 glob。"""
        pat = skill.SKILL_DIRS["qoder-alpha"]
        self.assertIn("*", pat, f"qoder-alpha 路径写死了版本哈希：{pat}")
        self.assertNotRegex(pat, r"/extensions/[0-9a-f]{8,}/")


class TestExcludedL0(unittest.TestCase):
    """L0：排除清单的形状。**排除项必须能被面板读到，不静默**。"""

    def test_excluded_has_reasons(self):
        self.assertGreaterEqual(len(skill.EXCLUDED_DIRS), 11)
        for path, why in skill.EXCLUDED_DIRS.items():
            self.assertTrue(path.startswith("/"), path)
            self.assertIsInstance(why, str)
            self.assertTrue(why.strip(), path)

    def test_excluded_disjoint_from_routes(self):
        """排除项与在册路不得重叠——重叠会让「为什么没收录」变成无法回答的问题。"""
        for path in skill.EXCLUDED_DIRS:
            self.assertNotIn(path, set(skill.SKILL_DIRS.values()), path)

    def test_excluded_reasons_name_the_bucket(self):
        """理由必须落在已知桶里（市场缓存/安装暂存/备份/快照/同源副本/非技能），免得写成「不重要」。"""
        buckets = ("marketplace", "安装暂存", "备份", "快照", "同源副本", "非技能", "可选仓")
        for path, why in skill.EXCLUDED_DIRS.items():
            self.assertTrue(any(k in why for k in buckets),
                            f"{path} 的排除理由没说清是哪一类：{why}")

    def test_vendor_duplicate_copies_are_excluded_with_reason(self):
        """事实②：qoderwake 的 4 份同源副本必须进排除表，否则 11 条会被报成 44 条。"""
        for frag in ("runtime-generations", "runtime-resources"):
            hit = [p for p in skill.EXCLUDED_DIRS if frag in p]
            self.assertEqual(len(hit), 1, f"{frag} 应当在排除表里恰好一条")


class TestGlobL0(unittest.TestCase):
    """L0：glob 展开。**用 tempfile 造夹具**，不拿宿主目录当断言。"""

    def test_non_glob_returned_as_is(self):
        self.assertEqual(skill.expand_roots("/a/b/c"), ["/a/b/c"])

    def test_missing_non_glob_still_returned(self):
        """不存在的普通路径要原样返回，state 才报得出 missing，而不是整路消失。"""
        self.assertEqual(skill.expand_roots("/nonexistent-zzz-d1"), ["/nonexistent-zzz-d1"])

    def test_glob_expands_sorted(self):
        with tempfile.TemporaryDirectory() as td:
            for h in ("zz9", "aa1", "mm5"):
                os.makedirs(os.path.join(td, h, "skills"), exist_ok=True)
            got = skill.expand_roots(os.path.join(td, "*", "skills"))
            self.assertEqual(got, sorted(got))
            self.assertEqual(len(got), 3)

    def test_glob_no_match_falls_back_to_pattern(self):
        """一个都不命中时退回原 pattern —— 让 state 报 missing，而不是静默变 0 路。"""
        pat = "/nonexistent-zzz-d1/*/skills"
        self.assertEqual(skill.expand_roots(pat), [pat])

    def test_route_roots_drops_missing_roots(self):
        with tempfile.TemporaryDirectory() as td:
            os.makedirs(os.path.join(td, "v1", "skills"), exist_ok=True)
            pat = os.path.join(td, "*", "skills")
            skill.SKILL_DIRS["__tmp_glob_route__"] = pat
            try:
                self.assertEqual(len(skill.route_roots("__tmp_glob_route__")), 1)
            finally:
                skill.SKILL_DIRS.pop("__tmp_glob_route__", None)


class TestScanManyL0(unittest.TestCase):
    """L0：多根部分失败。**一根坏的不等于整路坏**，但要逐条说出来。"""

    def test_partial_failure_still_ok(self):
        with tempfile.TemporaryDirectory() as td:
            good = os.path.join(td, "good")
            os.makedirs(good)
            _write_skill(Path(good), "g1")
            with _Whitelist(td):
                r = skill._scan_many("t", [good, os.path.join(td, "nope")])
            self.assertTrue(r["ok"])
            self.assertEqual(len(r["items"]), 1)
            self.assertIn("nope", str(r["error"]))

    def test_all_missing_is_not_ok(self):
        r = skill._scan_many("t", ["/nonexistent-a/x", "/nonexistent-b/x"])
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_dir")

    def test_empty_list_is_ok_zero(self):
        r = skill._scan_many("t", [])
        self.assertTrue(r["ok"])
        self.assertEqual(r["items"], [])


class TestScanReasonL0(unittest.TestCase):
    """L0：_scan_one 的 reason 三态。只用 tempfile 与不存在的路径，不读真盘。"""

    def test_empty_root_is_no_path(self):
        r = skill._scan_one("x", "")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_path")

    def test_nonexistent_dir_is_no_dir(self):
        r = skill._scan_one("x", "/nonexistent-zzz-d1/skills")
        self.assertFalse(r["ok"])
        self.assertEqual(r["reason"], "no_dir")

    def test_existing_empty_dir_is_ok_with_zero_items(self):
        """目录在但 0 条 ⇒ ok=True / items=[] —— 与「目录不存在」严格分开。"""
        with tempfile.TemporaryDirectory() as td:
            with _Whitelist(td):
                r = skill._scan_one("x", td)
            self.assertTrue(r["ok"])
            self.assertEqual(r["items"], [])
            self.assertIsNone(r["reason"])

    def test_state_derivation(self):
        """state 四态映射：ok / empty / missing / error。"""
        for raw, want in (({"ok": True, "reason": None, "items": [1]}, "ok"),
                          ({"ok": True, "reason": None, "items": []}, "empty"),
                          ({"ok": False, "reason": "no_dir"}, "missing"),
                          ({"ok": False, "reason": "no_path"}, "error"),
                          ({"ok": False, "reason": "read_error"}, "error")):
            self.assertEqual(skill.skill_state(raw, len(raw.get("items") or [])), want, raw)


class TestDedupMechanismL0(unittest.TestCase):
    """L0：dedup **机制**（按 realpath 合并软链重叠）。

    初版这里写的是「hermes 120 与 hermes-agent 58 高度重叠」——实测 **零重叠**
    （178 = 120+58），断言是假的。改在 tempfile 里造出真实重叠，测机制本身。
    """

    def test_symlinked_duplicate_collapses(self):
        with tempfile.TemporaryDirectory() as td:
            real = Path(td) / "real"
            link = Path(td) / "link"
            _write_skill(real, "dup")
            link.mkdir()
            os.symlink(real / "dup", link / "dup")
            with _Whitelist(td):
                a = skill._scan_one("r1", str(real))["items"]
                b = skill._scan_one("r2", str(link))["items"]
                uniq, aliases = skill._dedup(a + b)
            self.assertEqual(len(a), 1)
            self.assertEqual(len(b), 1)
            self.assertEqual(len(uniq), 1, "同 realpath 必须合成一条")
            self.assertEqual(sorted(uniq[0]["routes"]), ["r1", "r2"])
            self.assertEqual(len(aliases), 1)

    def test_distinct_realpaths_do_not_collapse(self):
        """**这是初版那条假断言的正确版本**：内容同名但 realpath 不同 ⇒ 各算一条。"""
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "a"
            b = Path(td) / "b"
            _write_skill(a, "same")
            _write_skill(b, "same")
            with _Whitelist(td):
                uniq, aliases = skill._dedup(
                    skill._scan_one("r1", str(a))["items"]
                    + skill._scan_one("r2", str(b))["items"])
            self.assertEqual(len(uniq), 2)
            self.assertEqual(aliases, [])


@tiers.host_only
class TestRoutesL1(unittest.TestCase):
    """L1：读本机真盘。**抓的是「路指错了却静默 0 条」**——那是 R1 可见性缺口的复发形态。"""

    def test_every_route_dir_resolves(self):
        """每路至少要有一个真实根（glob 路也要能解析出目录，不能全落空）。"""
        bad = [r for r, p in skill.SKILL_DIRS.items()
               if not [x for x in skill.route_roots(r) if os.path.isdir(x)]]
        self.assertEqual(bad, [], f"登记了但解析不出任何真实根：{bad}")

    def test_new_routes_have_entries_over_floor(self):
        for route, floor in sorted(FLOORS.items()):
            with self.subTest(route=route):
                r = skill._scan_one(route, skill.route_roots(route))
                self.assertTrue(r["ok"], f"{route} 扫描失败：{r.get('error')}")
                self.assertGreaterEqual(len(r["items"]), floor,
                                        f"{route} 条数 {len(r['items'])} < 下限 {floor}，"
                                        f"疑似路径指错或扫描被截断")

    def test_codebuddy_route_reports_empty_not_missing(self):
        """v0.13.127：codebuddy 路必须「在且可扫」，状态只允许 ok/empty——**不许 missing**。

        判据不是「0 条」：本机实测该目录有 1 条 `crawl4ai` 软链（→ 技术文档/crawl4ai，
        经 ARCHIVE_ROOTS 进白名单）。真正的红线是两头：
        ① 路被摘掉 ⇒ codebuddy 装技能时 hub 静默看不见（盲区）；
        ② 状态报成 missing/error ⇒ 把「目录在」说成「没装」。
        另钉一条形态事实：该路内容**只经软链到达**（禁改面禁写，hub 不往里放本地副本）。
        """
        self.assertEqual(skill.SKILL_DIRS.get("codebuddy"), "/home/gztxt/.codebuddy/skills")
        r = skill._scan_one("codebuddy", skill.route_roots("codebuddy"))
        self.assertTrue(r["ok"], r.get("error"))
        state = skill.skill_state(r, len(r["items"]))
        self.assertIn(state, ("ok", "empty"), f"codebuddy 状态应是 ok/empty，实际 {state}")
        for it in r["items"]:
            self.assertTrue(it.get("via_symlink"),
                            f"{it['name']} 不是经软链到达 ⇒ 有人往禁改面写了本地副本？")

    def test_opencode_entries_arrive_via_symlink(self):
        """事实①：opencode 全是软链指回 techdocs 自研目录 ⇒ 扫得到，但 `find`（无 -L）报 0。"""
        r = skill._scan_one("opencode", skill.route_roots("opencode"))
        self.assertTrue(r["ok"])
        self.assertTrue(r["items"], "opencode 应有条目（3 个软链）")
        self.assertTrue(any(i.get("via_symlink") for i in r["items"]),
                        "opencode 的条目应当经软链到达")

    def test_qoder_alpha_glob_hit_is_concrete_hash_dir(self):
        """哈希路 glob 必须解析出**带哈希的**真实目录，且不止一个层级猜测命中。"""
        roots = [r for r in skill.route_roots("qoder-alpha") if os.path.isdir(r)]
        self.assertTrue(roots, "qoder-alpha glob 没解析出真实根")
        for r in roots:
            self.assertRegex(r, r"/extensions/[0-9a-f]{6,}/skills$", r)


if __name__ == "__main__":
    unittest.main()
