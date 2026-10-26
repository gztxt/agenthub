#!/usr/bin/env python3
"""L0 hermetic 测试：src/skill.py 的纯函数层（技能门面）。

分层口径（见 tests/README.md 与 tests/tiers.py）：
- 本文件属 **L0**：零宿主依赖 —— 不连 TDAI、不发任何网络请求、不读 `~/.claude` 等真实
  技能目录、不 import `src.main`、不起 TestClient、不 fork/pty、**不允许出现 SKIP**。
- 路由级 / 本机形态断言（真 38 个文件、TDAI 注册表 0 行、凭据脱敏端到端）在
  `tests/verify_skill_facade.py`（L2 闸门，60 项）。
- 猴补 `skill.SKILL_DIRS` 到 tmp 目录是本文件的基本手法；`tearDown` 必须还原，
  否则同一进程里后续用例会读到假目录（这是 L0 用例之间唯一的共享状态）。

为什么值得单独一层：skill.py 的价值全在"容错但如实表态"——frontmatter 解析、软链
去重、白名单守卫。这三件事都能用 tmp 目录确定性地造出红向场景，不需要本机形态配合。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))     # skill.py 内部是兄弟绝对导入（import tdai_client）
sys.path.insert(0, str(_REPO))

import skill                                    # noqa: E402
from fastapi import HTTPException               # noqa: E402

FAKE_KEY = "sk-L0FAKEKEY1234567890"


class _TmpSkillCase(unittest.TestCase):
    """提供 tmp 技能根 + SKILL_DIRS 猴补/还原。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0skill-"))
        self._dirs = dict(skill.SKILL_DIRS)
        skill.SKILL_DIRS = {"t": str(self.tmp)}
        self.addCleanup(self._restore)

    def _restore(self):
        skill.SKILL_DIRS = self._dirs
        shutil.rmtree(self.tmp, ignore_errors=True)

    def mk(self, name, body, raw=None):
        """在 tmp 根下造 <name>/SKILL.md，返回其路径。raw 优先（用于造 BOM/坏字节）。"""
        d = self.tmp / name
        d.mkdir(parents=True, exist_ok=True)
        p = d / "SKILL.md"
        if raw is not None:
            p.write_bytes(raw)
        else:
            p.write_text(body, encoding="utf-8")
        return p


# ── frontmatter 解析 ─────────────────────────────────────────────────────
class TestParseFrontmatter(unittest.TestCase):
    def test_normal_keys_and_body_split(self):
        fm, body = skill.parse_frontmatter("---\nname: x\ndescription: 说明\n---\n正文在此\n")
        self.assertEqual(fm, {"name": "x", "description": "说明"})
        self.assertEqual(body, "正文在此\n")

    def test_no_frontmatter_returns_none_and_keeps_full_text(self):
        """事实③：3 个真实技能没有 frontmatter。正确表态是 fm=None，**不是抛异常**，
        更不是把这条技能丢掉（丢了就是门面自己造静默）。"""
        raw = "裸正文，没有 frontmatter。\n第二行。\n"
        fm, body = skill.parse_frontmatter(raw)
        self.assertIsNone(fm)
        self.assertEqual(body, raw, "无 fm 时正文必须原样保留，不能吃掉首行")

    def test_unterminated_block_treated_as_no_fm(self):
        fm, body = skill.parse_frontmatter("---\nname: x\n没有收尾的 ---\n")
        self.assertIsNone(fm)
        self.assertIn("name: x", body)

    def test_quoted_values_are_unquoted(self):
        """事实④：本机确实存在 name: \"caveman\" 这种带引号写法。"""
        fm, _ = skill.parse_frontmatter('---\nname: "caveman"\ndescription: \'压缩输出\'\n---\n')
        self.assertEqual(fm["name"], "caveman")
        self.assertEqual(fm["description"], "压缩输出")

    def test_quote_like_content_not_over_stripped(self):
        """只剥成对的首尾引号；值内部的引号必须留着（否则描述会被啃掉字符）。"""
        fm, _ = skill.parse_frontmatter('---\nname: a\ndescription: 他说 "好" 就走了\n---\n')
        self.assertEqual(fm["description"], '他说 "好" 就走了')

    def test_folded_gt_dash_joined_into_one_line(self):
        fm, _ = skill.parse_frontmatter(
            "---\nname: f\ndescription: >-\n  第一行\n  第二行\n---\n正文\n")
        self.assertEqual(fm["description"], "第一行 第二行")

    def test_literal_pipe_block_joined(self):
        fm, _ = skill.parse_frontmatter("---\nname: f\ndescription: |\n  甲\n  乙\n---\n")
        self.assertEqual(fm["description"], "甲 乙")

    def test_folded_block_stops_at_next_key(self):
        """折叠块不能吃掉后面的键，否则 description 里会混进 `name:` 之类。"""
        fm, _ = skill.parse_frontmatter(
            "---\ndescription: >-\n  一段\n  两段\nname: real\n---\n")
        self.assertEqual(fm.get("name"), "real")
        self.assertEqual(fm.get("description"), "一段 两段")

    def test_crlf_line_endings(self):
        fm, body = skill.parse_frontmatter("---\r\nname: crlf\r\ndescription: 回车换行\r\n---\r\n正文\r\n")
        self.assertEqual(fm["name"], "crlf")
        self.assertEqual(fm["description"], "回车换行")
        self.assertEqual(body, "正文\r\n")

    def test_bom_does_not_silently_kill_frontmatter(self):
        """回归：utf-8 解码会把 BOM 留成正文首字符 ⇒ 首行不再是 `---` ⇒ fm 被判成"没有"。
        本机今天 0/38 带 BOM，属潜伏面；这条钉住"剥 BOM"的修复不被回退。"""
        fm, body = skill.parse_frontmatter("\ufeff---\nname: bom\ndescription: 带BOM\n---\n正文\n")
        self.assertIsNotNone(fm, "BOM 让 frontmatter 静默失效（修复被回退了）")
        self.assertEqual(fm["name"], "bom")
        self.assertFalse(body.startswith("\ufeff"))

    def test_repeated_bom_still_stripped(self):
        fm, _ = skill.parse_frontmatter("\ufeff\ufeff---\nname: b2\n---\n")
        self.assertEqual((fm or {}).get("name"), "b2")

    def test_line_without_colon_ignored_no_crash(self):
        fm, _ = skill.parse_frontmatter("---\n这不是键值对\nname: ok\n---\n")
        self.assertEqual(fm, {"name": "ok"})

    def test_unknown_keys_are_kept(self):
        """门面不猜语义：除 name/description 外的键（allowed-tools 等）也如实保留，
        这样 /status 的 fm_keys 才能反映"到底解析到了什么"。"""
        fm, _ = skill.parse_frontmatter("---\nname: k\nallowed-tools: read, bash\nlicense: MIT\n---\n")
        self.assertEqual(fm["allowed-tools"], "read, bash")
        self.assertEqual(fm["license"], "MIT")

    def test_empty_value_kept_as_empty_string(self):
        fm, _ = skill.parse_frontmatter("---\nname: e\ndescription:\n---\n")
        self.assertEqual(fm, {"name": "e", "description": ""})


# ── 读文件 ───────────────────────────────────────────────────────────────
class TestReadText(_TmpSkillCase):
    def test_utf8_reported_as_utf8(self):
        p = self.mk("a", "---\nname: a\n---\n中文正文\n")
        text, enc = skill._read_text(str(p))
        self.assertEqual(enc, "utf-8")
        self.assertIn("中文正文", text)

    def test_invalid_bytes_do_not_raise_and_are_labelled(self):
        """非 UTF-8 不许抛、不许丢，必须**如实标注**用了 replace（口径同 sessions_store.py）。"""
        p = self.mk("bad", None, raw=b"---\nname: bad\n---\n\xff\xfe\x80 broken\n")
        text, enc = skill._read_text(str(p))
        self.assertIn("replace", enc)
        self.assertIn("name: bad", text, "坏字节文件的可读部分仍要保住")

    def test_missing_file_raises_oserror(self):
        """读不到就该抛 OSError —— 由 _scan_one 兜住变成结构化 error，不在这里吞。"""
        with self.assertRaises(OSError):
            skill._read_text(str(self.tmp / "nope" / "SKILL.md"))


# ── 扫描：软链、剪枝、部分失败、越界 ──────────────────────────────────────
class TestScan(_TmpSkillCase):
    def test_lists_skills_and_falls_back_to_dirname(self):
        self.mk("with-fm", "---\nname: 显式名\ndescription: d\n---\n正文\n")
        self.mk("no-fm", "裸正文\n")
        r = skill._scan_one("t", str(self.tmp))
        names = {i["name"] for i in r["items"]}
        self.assertTrue(r["ok"])
        self.assertEqual(names, {"显式名", "no-fm"}, "无 fm 的必须回退目录名，而不是消失")
        by = {i["name"]: i for i in r["items"]}
        self.assertTrue(by["显式名"]["fm"])
        self.assertFalse(by["no-fm"]["fm"])

    def test_nested_dirs_are_found(self):
        self.mk("group/sub/deep", "---\nname: deep\n---\n")
        r = skill._scan_one("t", str(self.tmp))
        self.assertEqual([i["name"] for i in r["items"]], ["deep"])

    def test_skip_dir_names_are_pruned(self):
        """.git / node_modules 里的 SKILL.md 不是技能，必须剪掉（否则计数虚高）。"""
        self.mk(".git/hooks/x", "---\nname: gitnoise\n---\n")
        self.mk("node_modules/pkg", "---\nname: nmnoise\n---\n")
        self.mk("real", "---\nname: real\n---\n")
        r = skill._scan_one("t", str(self.tmp))
        self.assertEqual([i["name"] for i in r["items"]], ["real"])

    def test_symlinked_skill_dir_inside_whitelist_is_followed(self):
        """事实①的本体：`Path.rglob('SKILL.md')` 不跟随软链目录，会漏掉 pi 路的
        agent-dispatch。这里用 tmp 复现同一形态：软链目录指向白名单内的真实目录。"""
        real_root = self.tmp / "realroot"
        (real_root / "linked").mkdir(parents=True)
        (real_root / "linked" / "SKILL.md").write_text("---\nname: linked\n---\n", encoding="utf-8")
        via = self.tmp / "viaroot"
        via.mkdir()
        os.symlink(str(real_root / "linked"), str(via / "linked"))
        r = skill._scan_one("t", str(via))
        got = [i for i in r["items"] if i["name"] == "linked"]
        self.assertEqual(len(got), 1, "软链目录里的技能被漏扫（followlinks 失效）")
        self.assertTrue(got[0]["via_symlink"], "经软链到达必须标出来，不能伪装成真实文件")

    def test_symlink_escaping_whitelist_is_refused_before_read(self):
        """红向：指向白名单外的软链 SKILL.md，必须在**读之前**拒掉。
        解析 frontmatter 需要读内容，所以"读之后再判"已经晚了。"""
        outside = pathlib.Path(tempfile.mkdtemp(prefix="l0outside-"))
        secret = outside / "SKILL.md"
        secret.write_text("---\nname: leaked\n---\nL0SECRET-MARKER-9137\n", encoding="utf-8")
        self.addCleanup(shutil.rmtree, str(outside), True)
        evil = self.tmp / "evil"
        evil.mkdir()
        os.symlink(str(secret), str(evil / "SKILL.md"))
        self.mk("ok", "---\nname: ok\n---\n正文\n")

        r = skill._scan_one("t", str(self.tmp))
        self.assertEqual([i["name"] for i in r["items"]], ["ok"], "越界软链进了清单")
        sk = r.get("skipped_outside") or []
        self.assertEqual(len(sk), 1, "拒读必须留痕，不许静默丢条目")
        self.assertEqual(sk[0]["realpath"], str(secret))
        self.assertTrue(sk[0]["why"])
        self.assertNotIn("L0SECRET-MARKER-9137", json.dumps(r, ensure_ascii=False),
                         "越界文件的内容被读出来了")

    def test_partial_read_failure_keeps_route_ok_and_reports(self):
        """单个文件读不了 ≠ 整路失败：路仍 ok，但 error 要指名道姓（不许静默少条目）。
        用猴补 _read_text 造失败，避免依赖 chmod（以 root 跑时 chmod 000 照样能读）。"""
        self.mk("good", "---\nname: good\n---\n")
        bad = self.mk("bad", "---\nname: bad\n---\n")
        orig = skill._read_text

        def fake(path):
            if pathlib.Path(path).name == "SKILL.md" and "bad" in str(path):
                raise PermissionError(13, "Permission denied")
            return orig(path)

        skill._read_text = fake
        self.addCleanup(setattr, skill, "_read_text", orig)
        r = skill._scan_one("t", str(self.tmp))
        self.assertTrue(r["ok"], "一个文件读不了就把整路判死，会掩盖其余技能")
        self.assertEqual([i["name"] for i in r["items"]], ["good"])
        self.assertIn("PermissionError", r["error"] or "")
        self.assertIn(str(bad), r["error"] or "", "error 必须指明是哪个文件")

    def test_empty_root_config_is_structured_error(self):
        r = skill._scan_one("t", "")
        self.assertFalse(r["ok"])
        self.assertIn("未配置", r["error"] or "")

    def test_missing_root_is_structured_error_not_exception(self):
        r = skill._scan_one("t", str(self.tmp / "does-not-exist"))
        self.assertFalse(r["ok"])
        self.assertIn("目录不存在", r["error"] or "")
        self.assertEqual(r["items"], [])

    def test_ms_is_measured(self):
        self.mk("a", "---\nname: a\n---\n")
        r = skill._scan_one("t", str(self.tmp))
        self.assertIsInstance(r["ms"], float)
        self.assertGreaterEqual(r["ms"], 0.0)


# ── realpath 去重 ─────────────────────────────────────────────────────────
class TestDedup(unittest.TestCase):
    @staticmethod
    def _it(route, name, path, rp, mtime=1.0):
        return {"route": route, "name": name, "path": path, "realpath": rp,
                "mtime": mtime, "description": "", "via_symlink": path != rp,
                "fm": True, "fm_keys": [], "bytes": 1, "encoding": "utf-8", "source": "disk"}

    def test_same_realpath_merged_once_with_both_routes(self):
        """事实②的本体：pi 路经软链指到 techdocs 的同一个文件 ⇒ 只能列一次。"""
        items = [self._it("pi", "agent-dispatch", "/pi/agent-dispatch/SKILL.md", "/real/SKILL.md"),
                 self._it("techdocs", "agent-dispatch", "/real/SKILL.md", "/real/SKILL.md")]
        out, aliases = skill._dedup(items)
        self.assertEqual(len(out), 1)
        self.assertEqual(sorted(out[0]["routes"]), ["pi", "techdocs"])
        self.assertEqual(len(aliases), 1)
        self.assertEqual(aliases[0]["name"], "agent-dispatch")
        # v0.13.165：逐路事实（哪一路是软链）必须随条目回给前端，
        # 否则 DELETE /remove 在页面上无从判断「这一路能不能卸」。
        links = {l["route"]: l["via_symlink"] for l in out[0]["links"]}
        self.assertEqual(links, {"pi": True, "techdocs": False})

    def test_canonical_entry_prefers_the_real_file_route(self):
        """规范条目必须是"真实文件那一路"，软链路只当别名 —— 否则门面会指着一个
        随时可能被删的软链当权威路径。"""
        items = [self._it("pi", "s", "/pi/s/SKILL.md", "/real/s/SKILL.md"),
                 self._it("techdocs", "s", "/real/s/SKILL.md", "/real/s/SKILL.md")]
        out, _ = skill._dedup(items)
        self.assertEqual(out[0]["route"], "techdocs")
        self.assertEqual(out[0]["path"], "/real/s/SKILL.md")
        self.assertFalse(out[0]["via_symlink"])

    def test_same_name_different_files_are_not_merged(self):
        """同名但 realpath 不同 = 两个技能，绝不能因为名字一样就合并掉一个。"""
        items = [self._it("claude", "dup", "/a/dup/SKILL.md", "/a/dup/SKILL.md"),
                 self._it("pi", "dup", "/b/dup/SKILL.md", "/b/dup/SKILL.md")]
        out, aliases = skill._dedup(items)
        self.assertEqual(len(out), 2)
        self.assertEqual(aliases, [], "不同文件不该被记成别名")

    def test_walked_minus_unique_equals_aliases(self):
        """/list 的 dedup 账目必须自洽：walked - unique == len(aliases)。"""
        items = [self._it("pi", "s", "/pi/s/SKILL.md", "/real/s/SKILL.md"),
                 self._it("techdocs", "s", "/real/s/SKILL.md", "/real/s/SKILL.md"),
                 self._it("claude", "other", "/c/other/SKILL.md", "/c/other/SKILL.md")]
        out, aliases = skill._dedup(items)
        self.assertEqual(len(items) - len(out), len(aliases))

    def test_missing_realpath_falls_back_to_path(self):
        it = self._it("claude", "x", "/c/x/SKILL.md", "/c/x/SKILL.md")
        it.pop("realpath")
        out, aliases = skill._dedup([it])
        self.assertEqual(len(out), 1)
        self.assertEqual(aliases, [])

    def test_output_is_deterministically_sorted(self):
        items = [self._it("techdocs", "b", "/t/b/SKILL.md", "/t/b/SKILL.md"),
                 self._it("claude", "z", "/c/z/SKILL.md", "/c/z/SKILL.md"),
                 self._it("claude", "a", "/c/a/SKILL.md", "/c/a/SKILL.md")]
        out, _ = skill._dedup(items)
        self.assertEqual([(i["route"], i["name"]) for i in out],
                         [("claude", "a"), ("claude", "z"), ("techdocs", "b")])


# ── 白名单守卫 ────────────────────────────────────────────────────────────
class TestWhitelistGuard(_TmpSkillCase):
    def test_inside_rejects_sibling_prefix_trap(self):
        """/a/skills2 不是 /a/skills 的子目录。少了 os.sep 就会被前缀误判成"在里面"。"""
        self.assertFalse(skill._inside("/a/skills2/x/SKILL.md", ["/a/skills"]))
        self.assertTrue(skill._inside("/a/skills/x/SKILL.md", ["/a/skills"]))
        self.assertTrue(skill._inside("/a/skills", ["/a/skills"]), "根本身算在内")

    def test_allowed_roots_follows_monkeypatched_dirs_and_drops_missing(self):
        skill.SKILL_DIRS = {"a": str(self.tmp), "ghost": "/nonexistent-l0-ghost"}
        # 2026-10-03：归档根也得钉住。`ARCHIVE_ROOTS` 是本机绝对路径，
        # 写死在模块里而用例不钉 ⇒ 开发机上多出 4 条白名单、干净 runner 上是 0 条，
        # **同码两态**（本机必红 / CI 必绿）。与 SKILL_DIRS 同样猴补，与宿主解耦。
        saved_archive = skill.ARCHIVE_ROOTS_ACTIVE
        skill.ARCHIVE_ROOTS_ACTIVE = {}
        try:
            roots = skill._allowed_roots()
        finally:
            skill.ARCHIVE_ROOTS_ACTIVE = saved_archive
        self.assertEqual(roots, [os.path.realpath(str(self.tmp))])

    def test_guard_raises_400_for_outside_path(self):
        with self.assertRaises(HTTPException) as cm:
            skill._guard_inside_whitelist("/etc/passwd")
        self.assertEqual(cm.exception.status_code, 400)

    def test_guard_returns_realpath_for_inside_path(self):
        p = self.mk("ok", "---\nname: ok\n---\n")
        self.assertEqual(skill._guard_inside_whitelist(str(p)), os.path.realpath(str(p)))

    def test_guard_allows_symlink_pointing_inside_whitelist(self):
        """合法软链（pi→techdocs 形态）不能被这道闸门误杀，否则事实②直接坏掉。"""
        real = self.mk("real", "---\nname: real\n---\n")
        link_dir = self.tmp / "linkdir"
        link_dir.mkdir()
        link = link_dir / "SKILL.md"
        os.symlink(str(real), str(link))
        self.assertEqual(skill._guard_inside_whitelist(str(link)), os.path.realpath(str(real)))

    def test_guard_rejects_symlink_pointing_outside(self):
        outside = pathlib.Path(tempfile.mkdtemp(prefix="l0out2-"))
        self.addCleanup(shutil.rmtree, str(outside), True)
        target = outside / "SKILL.md"
        target.write_text("x", encoding="utf-8")
        link_dir = self.tmp / "esc"
        link_dir.mkdir()
        link = link_dir / "SKILL.md"
        os.symlink(str(target), str(link))
        with self.assertRaises(HTTPException):
            skill._guard_inside_whitelist(str(link))


# ── backends 信封 / 过滤 / 路由名单 ───────────────────────────────────────
class TestBackendAndRoutes(_TmpSkillCase):
    def test_backend_shape_is_five_keys(self):
        """与 kb.py:_backend 同形 5 键：外部 agent 靠这个判断"哪路挂了"。"""
        b = skill._backend("claude", {"ok": True, "items": [{}, {}], "ms": 3.5, "error": None})
        self.assertEqual(sorted(b), ["count", "error", "ms", "name", "ok"])
        self.assertEqual(b["count"], 2, "count 必须数 items —— 抄 memory.py 那种读 r['count'] 会恒为 0")

    def test_backend_error_is_scrubbed(self):
        b = skill._backend("tdai", {"ok": False, "items": [], "ms": 1.0,
                                    "error": f"上游 401：{FAKE_KEY}"})
        self.assertFalse(b["ok"])
        self.assertNotIn(FAKE_KEY, b["error"])
        self.assertIn("<redacted>", b["error"])

    def test_backend_tolerates_missing_items(self):
        b = skill._backend("x", {"ok": False, "ms": None})
        self.assertEqual(b["count"], 0)

    def test_match_q_is_case_insensitive_and_covers_fields(self):
        it = {"name": "Wigolo-Search", "description": "网页检索", "path": "/p/SKILL.md", "route": "claude"}
        self.assertTrue(skill._match_q(it, ""))
        self.assertTrue(skill._match_q(it, "wigolo"))
        self.assertTrue(skill._match_q(it, "网页"))
        self.assertTrue(skill._match_q(it, "claude"))
        self.assertFalse(skill._match_q(it, "不存在"))

    def test_disk_routes_and_all_routes(self):
        skill.SKILL_DIRS = {"a": "/x", "b": "/y"}
        self.assertEqual(skill.disk_routes(), ("a", "b"))
        self.assertEqual(skill.all_routes(), ("a", "b", "tdai"))

    def test_default_dirs_route_names_are_pinned(self):
        """400 报错里回显的可用值依赖这些名字；改名会连带打破闸门与前端。
        v0.13.26 批2：4→7 路（新增 agents/codex/workbuddy 三个实测发现点，56 号文档）。
        v0.13.66 D1：7→20 路（补齐 hermes×3/jcode/grok×2/picoclaw/qoder×3+alpha/opencode）。
        v0.13.127 PT-20261019-01：20→21 路（补 codebuddy 只读路由；该目录当前 0 条，
        不登记＝「装了技能 hub 静默看不见」的盲区。项目本地 `local` 路**动态出现**，
        不在此表——它一个根都没有时压根不出现，见 disk_routes 注释）。
        2026-10-19：21→22 路（补 qwenpaw；`~/.qwenpaw/config.json:1074` 的 skillPaths
        明写该目录，同属「不登记＝静默看不见」那一类）。
        路线细节、条数与排除理由由 tests/test_skill_routes.py 逐项钉；本闸门只守**名字**。"""
        self.assertEqual(sorted(skill._DEFAULT_DIRS),
                         ["agents", "claude", "codebuddy", "codex", "grok", "grok-bundled",
                          "hermes", "hermes-agent", "hermes-web", "jcode", "opencode",
                          "pi", "picoclaw", "qoder", "qoder-alpha", "qoderwake",
                          "qoderwake-cli", "qoderwake-shadow", "qwenpaw", "superpowers",
                          "techdocs", "workbuddy"])

    def test_load_dirs_env_override_takes_effect(self):
        os.environ["SKILL_DIRS_JSON"] = json.dumps({"only": "/tmp/x"})
        self.addCleanup(os.environ.pop, "SKILL_DIRS_JSON", None)
        self.assertEqual(skill._load_dirs(), {"only": "/tmp/x"})

    def test_load_dirs_bad_input_falls_back_and_says_so(self):
        """非法覆盖必须回退默认**并留警告**：静默回退会让运维以为覆盖生效了。
        所以两件事都得断言 —— 只查回退值等于没查「不许静默」。"""
        self.addCleanup(os.environ.pop, "SKILL_DIRS_JSON", None)
        for label, bad in (("不是 JSON", "{不是 JSON"),
                           ("不是字典", json.dumps(["不是", "字典"])),
                           ("值不是字符串", json.dumps({"k": 123})),
                           ("空字典", json.dumps({}))):
            with self.subTest(bad=label):
                os.environ["SKILL_DIRS_JSON"] = bad
                with self.assertLogs(skill.log, level="WARNING") as lg:
                    self.assertEqual(skill._load_dirs(), dict(skill._DEFAULT_DIRS))
                self.assertIn("SKILL_DIRS_JSON", " ".join(lg.output),
                              "回退了却没说为何回退，等于静默")


if __name__ == "__main__":
    unittest.main(verbosity=2)
