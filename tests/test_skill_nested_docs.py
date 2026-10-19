#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：父技能的子章节文档（`nested`）识别 + 预算清单排除。

**为什么必须钉死这一条**：`~/.jcode/skills/ima-skills/{notes,knowledge-base}/SKILL.md`
没有 frontmatter，也不是独立技能——它们是 `ima-skill` 的接口分册，父级 SKILL.md 用
`⛔ Before ANY write ... validate ALL string fields` 这类 MANDATORY RULES 统管它们。

两种错误做法，本文件各钉一条：
1. **把子文档当技能列进名录** ⇒ 模型可能选中 `notes` 而绕过父级的写前校验，
   真实后果是 IMA 笔记乱码（不可逆）。断言：`nested=True`，且不进 full/name_only。
2. **直接把它从清单里抹掉** ⇒ 那是门面自己造静默（本仓最禁的失败形态）。
   断言：`nested_docs` 里必须**说出它的名字**。

夹具全是 tmp 目录：不起服务、不碰宿主技能目录、不打网络。
"""
import asyncio
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db        # noqa: E402
import skill     # noqa: E402


def _mk(d: pathlib.Path, name: str = None, desc: str = None) -> None:
    d.mkdir(parents=True, exist_ok=True)
    if desc is None:                      # 裸正文，模拟 ima 子文档的形态
        (d / "SKILL.md").write_text("# 子章节\n\nAPI base path: `openapi/note/v1`\n",
                                    encoding="utf-8")
    else:
        (d / "SKILL.md").write_text(
            f"---\nname: {name or d.name}\ndescription: {desc}\n---\n正文\n", encoding="utf-8")


class NestedDocTest(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0nested-"))
        self.root = self.tmp / "t"
        self.root.mkdir()
        self._saved = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"t": str(self.root)}
        skill._scan_cache.clear()
        db.init_db(self.tmp / "t.db")
        self.addCleanup(self._restore)

        _mk(self.root / "flat-skill", "flat-skill", "普通技能")
        _mk(self.root / "ima", "ima-skill", "IMA 统一技能")
        _mk(self.root / "ima" / "notes")            # 子章节：无 frontmatter
        _mk(self.root / "loose" / "sub", "sub", "祖先无 SKILL.md 的普通深层技能")

    def _restore(self):
        skill.SKILL_DIRS = self._saved
        skill._scan_cache.clear()
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        shutil.rmtree(self.tmp, ignore_errors=True)

    # ── 判定层 ────────────────────────────────────────────────
    def test_child_of_skill_is_nested(self):
        self.assertTrue(skill._is_nested_doc(str(self.root / "ima" / "notes"), str(self.root)))

    def test_top_level_is_not_nested(self):
        self.assertFalse(skill._is_nested_doc(str(self.root / "flat-skill"), str(self.root)))

    def test_deep_dir_without_parent_skill_md_is_not_nested(self):
        """祖先链上没有 SKILL.md ⇒ 是普通技能，不能因为"在子目录里"就误伤。"""
        self.assertFalse(skill._is_nested_doc(str(self.root / "loose" / "sub"), str(self.root)))

    def test_scan_sets_flag_without_dropping_item(self):
        res = skill._scan_one("t", str(self.root))
        by_name = {i["name"]: i for i in res["items"]}
        self.assertIn("notes", by_name, "子文档必须仍在扫描结果里（不许静默消失）")
        self.assertTrue(by_name["notes"]["nested"])
        self.assertFalse(by_name["flat-skill"]["nested"])

    # ── 预算层 ────────────────────────────────────────────────
    def test_budget_excludes_nested_but_reports_it(self):
        out = asyncio.run(skill.skill_budget(1200, ""))
        names = [x["name"] for x in out["full"]]

        self.assertNotIn("notes", names, "子章节不许出现在可选名录里")
        self.assertNotIn("notes", out["name_only"], "也不许降级成 name-only 白占预算")
        self.assertIn("notes", out["nested_docs"], "排除必须留痕，不许静默")

        # total 只数真技能：flat-skill / ima-skill / sub
        self.assertEqual(out["total"], 3, f"total={out['total']}，子文档被算进名录了")
        self.assertEqual(sorted(out["nested_docs"]), ["notes"])

    def test_nested_absent_from_total_but_desc_present_in_full(self):
        """反向锚：有 description 的父技能必须进 full —— 证明上面的排除不是"整路被跳过"。"""
        out = asyncio.run(skill.skill_budget(1200, ""))
        full = {x["name"]: x for x in out["full"]}
        self.assertIn("ima-skill", full)
        self.assertEqual(full["ima-skill"]["description"], "IMA 统一技能")


if __name__ == "__main__":
    unittest.main(verbosity=2)
