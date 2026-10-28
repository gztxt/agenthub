#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：`scripts/skill_assets_register.py` —— 技能包批量登记。

这份闸门守的是**四个会静默骗人的点**（每一个都能让「18 条登记好了」成为假绿）：

1. **只到 draft**：脚本**不得**推进状态机。盘上包 15/16 许可证未确认
   （`skills/PROVENANCE.md`），推到 `published`（唯一可依赖态）等于替它盖章。
   判据：跑完 `status` 全为 draft、`digest`/`path` 全空 —— 那两个字段只由 publish 落。
2. **幂等不改状态**：二次执行不得把别人手工推过的资产打回 draft，也不得重写 `created_at`
   （`upsert` 的 ON CONFLICT 本就只该更新 owner/version/scope/deps/models/note/updated_at）。
3. **slug = 目录名**：`agent-browser-cli`(name=agent-browser)、`web-anti-crawl-search`(name=web-search)
   两处 name 与目录不符。slug 必须跟目录走（与 manifest/整包摘要口径一致），差异进 note。
4. **版本不编**：frontmatter 自带 `version:` 的取真值，没有的用占位并**在 note 里写明无出处**
   —— 「1.0.0」不能被读成「上游发布过 1.0.0」。

分层口径（tests/tiers.py）：tmp 目录造两个假包 + tmp sqlite，不碰真实 `技术文档/skills/`。
"""
import importlib.util
import pathlib
import sqlite3
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db              # noqa: E402
import skill_assets as sa  # noqa: E402

_SPEC = importlib.util.spec_from_file_location(
    "skill_assets_register", _REPO / "scripts" / "skill_assets_register.py")
reg = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(reg)


def _pkg(root: pathlib.Path, name: str, body: str) -> pathlib.Path:
    p = root / name
    p.mkdir(parents=True)
    (p / "SKILL.md").write_text(body, encoding="utf-8")
    return p


WITH_VERSION = "---\nname: summarize\nversion: 2.3.4\n---\n\n# x\n"
NO_VERSION = "---\nname: agent-browser\n---\n\n# y\n"


class TestRowFor(unittest.TestCase):
    """`row_for` 是纯函数：登记口径的四个判据都先在这一层钉死。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())

    def test_version_from_frontmatter_wins(self):
        pkg = _pkg(self.tmp, "summarize", WITH_VERSION)
        r = reg.row_for("summarize", pkg, reg._frontmatter(pkg / "SKILL.md"))
        self.assertEqual(r["version"], "2.3.4")
        self.assertEqual(r["warns"], [], "有真版本就不该有版本告警")

    def test_missing_version_is_placeholder_and_says_so(self):
        pkg = _pkg(self.tmp, "agent-browser-cli", NO_VERSION)
        r = reg.row_for("agent-browser-cli", pkg, reg._frontmatter(pkg / "SKILL.md"))
        self.assertEqual(r["version"], reg.PLACEHOLDER_VERSION)
        self.assertTrue(any("版本无出处" in w for w in r["warns"]), r["warns"])
        self.assertIn("版本无出处", r["note"],
                      "占位版本必须写进 note —— 否则 1.0.0 会被当成上游发布过的版本")

    def test_slug_follows_dirname_not_frontmatter_name(self):
        pkg = _pkg(self.tmp, "web-anti-crawl-search",
                   "---\nname: web-search\nversion: 1.0.0\n---\n")
        r = reg.row_for("web-anti-crawl-search", pkg, reg._frontmatter(pkg / "SKILL.md"))
        self.assertEqual(r["slug"], "web-anti-crawl-search")
        self.assertTrue(any("web-search" in w and "不符" in w for w in r["warns"]), r["warns"])

    def test_scope_and_owner_pass_state_machine_validation(self):
        pkg = _pkg(self.tmp, "certbot-ssl", NO_VERSION)
        r = reg.row_for("certbot-ssl", pkg, reg._frontmatter(pkg / "SKILL.md"))
        self.assertIsNone(sa.validate_scope(r["scope"]), r["scope"])
        self.assertIsNotNone(sa.parse_version(r["version"]), r["version"])
        self.assertTrue(r["owner"].strip())

    def test_license_note_present(self):
        pkg = _pkg(self.tmp, "markitdown", NO_VERSION)
        r = reg.row_for("markitdown", pkg, reg._frontmatter(pkg / "SKILL.md"))
        self.assertIn("PROVENANCE.md", r["note"], "许可证台账必须被引用，不然后续无从追")


class TestDiscover(unittest.TestCase):
    def test_dir_without_skill_md_is_not_a_package(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        _pkg(tmp, "has", NO_VERSION)
        (tmp / "nope").mkdir()
        (tmp / "nope" / "README.md").write_text("x", encoding="utf-8")
        got = [s for s, _p, _f in reg.discover(tmp)]
        self.assertEqual(got, ["has"], "无 SKILL.md 的目录不得被当成技能包登记")

    def test_sorted_and_stable(self):
        tmp = pathlib.Path(tempfile.mkdtemp())
        for n in ("b", "a", "c"):
            _pkg(tmp, n, NO_VERSION)
        self.assertEqual([s for s, _p, _f in reg.discover(tmp)], ["a", "b", "c"])


class TestEndToEnd(unittest.TestCase):
    """真跑 main()：两遍 apply，钉住「只到 draft」与「幂等不打回状态」。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        self.root = self.tmp / "skills"
        self.root.mkdir()
        _pkg(self.root, "summarize", WITH_VERSION)
        _pkg(self.root, "nover", NO_VERSION)
        self.db_path = self.tmp / "agents.db"
        # 全程走 `_register()` 而不是 `main()`：`main()` 拒绝在已有连接时重指全局
        # （`release._ensure_db` 的同一条理由），而 pytest 进程里前一个用例可能已开过。
        # 本类第一个用例把全局连接指到自己的 tmp 库；后续用例因为测试隔离
        # **各自建库**，故统一显式 init 一次 —— 这里路径换的是 tmp→tmp，不碰生产。
        db.init_db(self.db_path)

    def _run(self):
        out = reg._register(self.db_path, self.root)
        self.assertFalse([r for r in out if r["pkg_error"]],
                         "整包摘要必须全部算得出来：%s" % [r["slug"] for r in out if r["pkg_error"]])
        return 0

    def _rows(self):
        c = sqlite3.connect(str(self.db_path))
        c.row_factory = sqlite3.Row
        try:
            return {r["slug"]: dict(r) for r in c.execute("SELECT * FROM skill_assets")}
        finally:
            c.close()

    def test_stops_at_draft_and_never_stamps_digest(self):
        self.assertEqual(self._run(), 0)
        rows = self._rows()
        self.assertEqual(sorted(rows), ["nover", "summarize"])
        for slug, r in rows.items():
            self.assertEqual(r["status"], "draft", slug)
            self.assertIsNone(r["digest"], "%s 不该有 digest（只有 publish 才落）" % slug)
            self.assertIsNone(r["path"], "%s 不该有 path（只有 publish 才落）" % slug)

    def test_second_run_is_idempotent_and_keeps_created_at(self):
        self.assertEqual(self._run(), 0)
        first = self._rows()
        self.assertEqual(self._run(), 0)       # 同进程第二次：全局连接仍是同一个
        second = self._rows()
        self.assertEqual(sorted(first), sorted(second), "二次执行不得新增行")
        for slug in first:
            self.assertEqual(first[slug]["created_at"], second[slug]["created_at"],
                             "%s 的 created_at 被重写了" % slug)

    def test_manual_advance_is_not_reverted_by_rerun(self):
        """别人把资产推到 tested 后，登记脚本再跑一遍不得把它打回 draft。"""
        self.assertEqual(self._run(), 0)
        sa.transition("summarize", "tested", by="test")
        self.assertEqual(sa.get("summarize")["status"], "tested")
        self.assertEqual(self._run(), 0)
        self.assertEqual(sa.get("summarize")["status"], "tested",
                         "登记脚本只该补元数据，不得动状态机")


class TestRealTreeContract(unittest.TestCase):
    """对真实目录的**只读**契约：不写任何库，只验「当前包数 + 两根异常 name」。

    包数随盘上新增而变（2026-10-19 16:54 加 aidev 四件套 ⇒ 18→22；
    2026-10-25 加本地技能 agent-onboard ⇒ 22→23；
    2026-10-28 11:53 加 oss-audit-lookup ⇒ 23→24）。这个断言是
    **哨兵不是常量**：盘上变了就该红一次、催人同步，同步时只改这个数与本文档。
    """

    def test_real_skills_tree_shape(self):
        if not reg.SKILLS_ROOT.is_dir():
            self.skipTest("本机无 技术文档/skills（运行器不在宿主机上）")
        items = reg.discover()
        self.assertEqual(len(items), 24,
                         "盘上技能包数变了 ⇒ 台账与缺口注释要同步改（只改本数与本类 docstring）")
        names = {s: (f.get("name") or "") for s, _p, f in items}
        self.assertEqual(names.get("agent-browser-cli"), "agent-browser")
        self.assertEqual(names.get("web-anti-crawl-search"), "web-search")


if __name__ == "__main__":
    unittest.main(verbosity=2)
