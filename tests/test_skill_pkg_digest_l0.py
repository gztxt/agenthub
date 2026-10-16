#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P1-5 首片 —— 技能资产**整包摘要**（C8，src/skill.py::package_digest）。

守的判据（手册 C8：「资产摘要须覆盖**完整能力包**（SKILL.md + scripts + templates +
references），**只算说明文件会漏脚本变化**」）：

1. **确定性**：同一棵树两次算出同一哈希；**与 os.walk 遍历顺序无关**
   （否则同一份包会算抖动哈希，不能当判据）。
2. **对脚本敏感**（C8 的核心）：**不动 SKILL.md、只改 scripts/ 下的文件** ⇒ 摘要必须变。
   这条若红，说明摘要在量错对象 —— 同族：判例 185「门永远不开」。
3. **不含量纲噪声**：改 mtime / 权限不算改能力（仅做 mtime 断言）。
4. **有界**：超上限标 `truncated` 而不是无限扫（一个怪异目录不得把 /list 拖死）。
5. **不伪装**：目录不在 ⇒ `error='not_a_dir'`，**不**报成「空包」。
6. **接线**：`_scan_one` 的条目带 `pkg_*` 字段（写了函数没人调＝没做）。

分层口径（tests/tiers.py）：临时目录树，不起服务、不连网、零 SKIP。
"""
import hashlib
import os
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

os.environ.setdefault("HUB_TEST_MODE", "1")

import skill  # noqa: E402


def _mkpkg(root: pathlib.Path, *, script_body: str = "print(1)", skill_md: str = "# S"):
    root.mkdir(parents=True, exist_ok=True)
    (root / "SKILL.md").write_text(skill_md, encoding="utf-8")
    (root / "scripts").mkdir(parents=True, exist_ok=True)
    (root / "scripts" / "run.py").write_text(script_body, encoding="utf-8")
    (root / "references").mkdir(exist_ok=True)
    (root / "references" / "ref.md").write_text("ref body", encoding="utf-8")
    return root


class TestPackageDigest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0pkg-"))
        self.addCleanup(lambda: __import__("shutil").rmtree(self.tmp, ignore_errors=True))

    def test_deterministic_same_tree(self):
        p = _mkpkg(self.tmp / "a")
        d1 = skill.package_digest(str(p))
        d2 = skill.package_digest(str(p))
        self.assertEqual(d1["digest"], d2["digest"])
        self.assertEqual(d1["files"], 3, "整包 = SKILL.md + scripts + references 共 3 个文件")
        self.assertTrue(d1["digest"])

    def test_creation_order_does_not_matter(self):
        """两张内容相同的包，**建文件顺序不同** ⇒ 摘要必须相同。

        （os.walk 的产出顺序跟文件系统有关；实现里按相对路径排序后再算，
        本用例就是钉这一点。）
        """
        a = self.tmp / "a"
        b = self.tmp / "b"
        a.mkdir(), b.mkdir()
        (a / "SKILL.md").write_text("# S", encoding="utf-8")
        (a / "scripts").mkdir()
        (a / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
        # 反向顺序建同样内容
        (b / "scripts").mkdir()
        (b / "scripts" / "run.py").write_text("print(1)", encoding="utf-8")
        (b / "SKILL.md").write_text("# S", encoding="utf-8")
        self.assertEqual(skill.package_digest(str(a))["digest"],
                         skill.package_digest(str(b))["digest"])

    def test_script_change_without_skill_md_change_alters_digest(self):
        """**C8 的核心判据**：只改脚本、不动 SKILL.md，摘要必须变。"""
        p = _mkpkg(self.tmp / "c", script_body="print(1)")
        before = skill.package_digest(str(p))["digest"]
        (p / "scripts" / "run.py").write_text("print(2)", encoding="utf-8")
        after = skill.package_digest(str(p))["digest"]
        self.assertNotEqual(before, after,
                            "只算 SKILL.md 的实现会在这里判「没变」——那正是 C8 要防的漏")

    def test_skill_md_change_alters_digest(self):
        p = _mkpkg(self.tmp / "d")
        before = skill.package_digest(str(p))["digest"]
        (p / "SKILL.md").write_text("# S changed", encoding="utf-8")
        self.assertNotEqual(before, skill.package_digest(str(p))["digest"])

    def test_mtime_change_alone_is_not_a_change(self):
        """改时间不算改能力（量纲只要内容）——否则每次触碰都报「资产变了」。"""
        p = _mkpkg(self.tmp / "e")
        before = skill.package_digest(str(p))["digest"]
        os.utime(p / "SKILL.md", (1, 1))
        self.assertEqual(before, skill.package_digest(str(p))["digest"])

    def test_skips_junk_dirs(self):
        p = _mkpkg(self.tmp / "f")
        (p / "__pycache__").mkdir()
        (p / "__pycache__" / "x.pyc").write_bytes(b"\x00\x01")
        (p / ".git").mkdir()
        (p / ".git" / "HEAD").write_text("ref: x", encoding="utf-8")
        d = skill.package_digest(str(p))
        self.assertEqual(d["files"], 3, "__pycache__/.git 不算能力包")

    def test_cap_marks_truncated(self):
        p = _mkpkg(self.tmp / "g")
        for i in range(skill.PKG_MAX_FILES + 5):
            (p / "references" / ("r%04d.md" % i)).write_text("x", encoding="utf-8")
        d = skill.package_digest(str(p))
        self.assertTrue(d["truncated"], "超上限必须显式标 truncated")
        self.assertEqual(d["files"], skill.PKG_MAX_FILES)

    def test_missing_dir_is_error_not_empty_package(self):
        d = skill.package_digest(str(self.tmp / "nope"))
        self.assertEqual(d["error"], "not_a_dir")
        self.assertEqual(d["digest"], "", "目录不在不得伪装成一个空包")

    def test_walk_order_independence(self):
        """把 `os.walk` 反向喂进去 ⇒ **若实现不排序，摘要就会变**。

        ⚠ 为什么不用「两次建文件顺序不同」那招：本文件系统上 `os.walk` 本来
        就接近字典序，**拿它当判据会假绿**（本轮实测：去掉排序那招仍转绿）。
        要真正钉住排序，只能主动把遍历顺序打乱。
        """
        p = _mkpkg(self.tmp / "j")
        base = skill.package_digest(str(p))["digest"]
        orig = os.walk
        os.walk = lambda root, **kw: iter(reversed(
            [(cur, dirs, list(reversed(fnames))) for cur, dirs, fnames in orig(root, **kw)]))
        self.addCleanup(lambda: setattr(os, "walk", orig))
        self.assertEqual(base, skill.package_digest(str(p))["digest"],
                         "遍历顺序变了摘要也必须一致（否则同一份包会算出抖动哈希）")

    def test_manifest_carries_per_file_hash(self):
        p = _mkpkg(self.tmp / "h")
        d = skill.package_digest(str(p))
        paths = {m["path"] for m in d["manifest"]}
        self.assertIn("SKILL.md", paths)
        self.assertIn(os.path.join("scripts", "run.py"), paths)
        self.assertTrue(all(len(m["sha256"]) == 64 for m in d["manifest"]))


class TestWiring(unittest.TestCase):
    def test_scan_items_carry_pkg_fields(self):
        """写了函数没人调＝没做。这条钉住 /list 的条目真的带上了整包摘要。"""
        src = (_REPO / "src" / "skill.py").read_text(encoding="utf-8")
        for k in ('"pkg_digest"', '"pkg_files"', '"pkg_bytes"', '"pkg_truncated"', '"pkg_error"'):
            self.assertIn(k, src, "扫描条目缺字段 %s" % k)
        self.assertIn("pkg = package_digest(cur)", src)


if __name__ == "__main__":
    unittest.main()