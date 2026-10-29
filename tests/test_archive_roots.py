"""归档根白名单（`skill.ARCHIVE_ROOTS`）的正负向闸门。

【为什么必须有这个文件】`_allowed_roots()` 是防「软链 `SKILL.md` 指向 `/etc/passwd`」的
**唯一**闸门，而 2026-10-03 为了清 PT-20261002-13 的「62 条第三方技能不可见」缺口，
给白名单加了 4 个归档根。PT-20261002-14 明写：「任何放宽必须同时给越界软链负向用例，
**不得只放宽不放测**」——所以这个文件不是可选项，是放宽的**对价**。

两条判据各测各的，别混：
  · 正向＝4 个归档根的 realpath 必须在白名单里（放宽确实生效了）；
  · 负向＝`/etc/passwd`、`~/.ssh`、快照与备份子树**必须仍被拒**（闸门没被顺手拆掉）。
只测正向等于没测——那正是本文件存在的理由。
"""
import os
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import skill

#: 本组用例的**双档**约定（与 `test_skill_facade` 同因）：
#: L0 hermetic 层的定义是「无宿主依赖」。`ARCHIVE_ROOTS` 是本机绝对路径，
#: 若在 L0 里断言它们存在，就是把「这台机器长这样」写进测试：
#: 开发机绿、干净 runner 靠 skipTest 赖过（L0 要求零 skip ⇒ 会红）。
#: ⇒ 本组**先钉住归档根**再做白名单形状断言（与宿主无关，L0 恒真），
#: 「本机 4 个归档根是否真在」属 L1，那里本就该问宿主形态。
_ARCHIVE = dict(skill.ARCHIVE_ROOTS)


class ArchiveRootsTest(unittest.TestCase):
    def setUp(self):
        # 钉成**假根**（本机不存在 ⇒ 必被 `isdir` 过滤掉），于是白名单只剩发现点，
        # 与宿主无关；正向/负向断言仍全部有效。
        # ⚠ v0.13.127 修一个**取证污染 bug**（PT-20261019-01 顺带抓出）：原写法先赋值、
        #   再 `addCleanup(setattr, ..., skill.ARCHIVE_ROOTS_ACTIVE)`——cleanup 捕获的是
        #   **赋值后的假根**，于是本类跑完假根泄漏给同进程后续所有测试 ⇒ L1 host 层的
        #   扫描结果取决于 L0 有没有先跑（同码两态）。实测受害例：codebuddy 路的
        #   crawl4ai 软链在 `all` 模式下被假白名单拒读报 0 条、`host` 单独跑报 1 条。
        #   修法：**先捕获原值再覆写**。
        _saved_archive = skill.ARCHIVE_ROOTS_ACTIVE
        skill.ARCHIVE_ROOTS_ACTIVE = {"/nonexistent-archive-root-a": "x",
                                       "/nonexistent-archive-root-b": "y"}
        self.addCleanup(setattr, skill, "ARCHIVE_ROOTS_ACTIVE", _saved_archive)
        self.allowed = skill._allowed_roots()

    def _inside(self, rp: str, allowed=None) -> bool:
        return any(rp == a or rp.startswith(a + os.sep) for a in (allowed or self.allowed))

    # ── 正向：放宽生效 ──
    def test_every_archive_root_is_allowed(self):
        """归档根逐个断言。少一个就红——PT-13 的缺口是 62 条，缺一个都算没修完。

        2026-10-27 新增 `i-have-adhd`：它的软链在 `~/.claude/skills`，realpath 落在
        本归档根，未登记时被越界闸门拒读（技能中心搜不到该技能）。原测试名里的
        「four」随集合增长已不成立，改成不含数量的名字，避免下次加根又要改函数名。
        """
        self.assertEqual(set(_ARCHIVE), {
            "/fs/1000/ftp/技术文档/mattpocock-skills",
            "/fs/1000/ftp/技术文档/crawl4ai",
            "/fs/1000/ftp/技术文档/hallmark",
            "/fs/1000/ftp/技术文档/Agent-Reach",
            "/fs/1000/ftp/技术文档/i-have-adhd",
            # 2026-10-29 用户点名装的两个第三方技能仓（show-me MIT / frontend-design Apache-2.0）
            "/fs/1000/ftp/技术文档/show-me",
            "/fs/1000/ftp/技术文档/anthropic-skills",
        }, "归档根集合变了：新增/删除都要在本文件与 CHANGELOG 里说清理由")
        # 用 monkeypatch 把假根换成真路径，验证「进了表就会被放进白名单」
        skill.ARCHIVE_ROOTS_ACTIVE = dict(_ARCHIVE)
        for root in _ARCHIVE:
            with self.subTest(root=root):
                self.assertTrue(skill._inside(os.path.realpath(root) + "/SKILL.md", skill._allowed_roots()),
                                "%s 未进白名单 ⇒ 该仓技能仍被当成软链越界拒读" % root)

    def test_archive_root_is_not_a_blanket_techdocs(self):
        """**不能**把整个 `技术文档/` 当白名单根。

        `snapshots/`、`全量备份/`、`.orca-audit/`、`Hermes-backup/` 在 `EXCLUDED_DIRS`
        里都是**已定的排除结论**。一刀切放开归档根等于用一条 FAIL 换掉四条已定结论。
        """
        skill.ARCHIVE_ROOTS_ACTIVE = dict(_ARCHIVE)
        allowed = skill._allowed_roots()
        self.assertFalse(skill._inside("/fs/1000/ftp/技术文档/some-other-repo/SKILL.md", allowed),
                         "整个 技术文档/ 进了白名单 ⇒ 排除结论被架空")

    # ── 负向：闸门没被拆（这才是本文件的主判据）──
    def test_system_paths_still_refused(self):
        for p in ("/etc/passwd", "/etc/shadow", "/home/gztxt/.ssh/id_ed25519",
                  "/home/gztxt/.bashrc"):
            with self.subTest(path=p):
                self.assertFalse(self._inside(p), "系统/密钥路径被放进白名单了：%s" % p)

    def test_excluded_subtrees_still_refused(self):
        """`EXCLUDED_DIRS` 里的归档根子树必须仍拒读（快照/备份是明确排除的）。"""
        for p in ("/fs/1000/ftp/技术文档/snapshots/x/SKILL.md",
                  "/fs/1000/ftp/技术文档/全量备份/x/SKILL.md",
                  "/home/gztxt/Hermes-backup/current/skills/dogfood/SKILL.md",
                  "/fs/1000/ftp/技术文档/.orca-audit/skills/orchestration/SKILL.md"):
            with self.subTest(path=p):
                self.assertFalse(self._inside(p), "已排除子树被放行：%s" % p)

    def test_discovery_roots_still_allowed(self):
        """发现点根照旧在白名单里——本改动是**并集**，不能把原来那份挤掉。"""
        for route, pat in skill.SKILL_DIRS.items():
            roots = [r for r in skill.route_roots(route) if r and os.path.isdir(r)]
            for r in roots:
                with self.subTest(route=route):
                    self.assertTrue(self._inside(os.path.realpath(r) + "/SKILL.md"),
                                    "发现点被挤出白名单：%s" % r)

    def test_missing_archive_root_is_dropped_not_crashed(self):
        """归档根目录不存在时**跳过**而不是崩——与 `route_roots()` 同一口径。"""
        self.assertNotIn("/nonexistent-archive-root-a", self.allowed,
                         "不存在的归档根应被 isdir 过滤掉")


if __name__ == "__main__":
    unittest.main()