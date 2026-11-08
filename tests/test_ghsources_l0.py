#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：多仓库源（设置里维护 → 拉数据 → 项目页按源分组）。

用户 2026-11-09 裁定（A/A/A）：
  · 分组单位 = 地址 + owner（同 host 不同 owner 各一组）；
  · star 口径 = 该 owner 的公开 star（/users/<owner>/starred）；
  · key 全源共用一个。

为什么这些用例必须存在：
1. **输入归一化是地址闸的前门**：粘贴 gztxt / 仓库 URL / GHES API 基址必须落到
   同一组 (api_base, git_host, owner)；否则「添加源」等于绕过 ghsettings 的
   https / 内网 / 带凭据三闸。
2. **分组计数必须与去重后的实际行数一致**：star 段重复展示自己的仓 = 用户
   看到同名两行、点哪行都开会话（事故形态：列表看起来有 2 个仓）。
3. **本地匹配按 host 分账**：两个 host 上都有 owner/name 时，只能命中同名 host
   的那一条；串账会把 A 站的仓标成 B 站「本地已有」，点开会话进错目录。
4. **默认源是全局设置的镜像**：改「设置 → 归属」必须继续生效（老用户习惯），
   但用户单独改过的源不许被覆盖（否则「我改的又变回去了」）。
5. **克隆的 URL 由源决定**：客户端只点名；host 取该源，绝不取「最后改过的
   全局值」——多 GHES 下那等于把 A 站仓克隆到 B 站地址。
"""
import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

import tiers  # noqa: E402

os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import db                        # noqa: E402
import ghsettings as gs          # noqa: E402
import githubprojects as gp      # noqa: E402

_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))


def _mktmp(prefix: str) -> pathlib.Path:
    _L0_TMP.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        d = _L0_TMP / (prefix + str(os.getpid()) + "-" + os.urandom(4).hex())
        try:
            d.mkdir()
            return tiers.l0_fixture_register(d)
        except FileExistsError:
            continue
    raise RuntimeError("造夹具目录失败")


def _repo(fn, fork=False):
    owner, name = fn.split("/")
    return {"full_name": fn, "name": name, "owner": {"login": owner},
            "fork": fork, "private": False, "archived": False,
            "default_branch": "main", "language": "Python", "description": "d",
            "pushed_at": "2026-11-01T10:00:00Z"}


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = _mktmp("ghsrc-")
        db.init_db(self.tmp / "gh.db")
        for k in list(gs.KEYS) + ["token"]:
            gs._del(k)
        try:
            db.execute("DELETE FROM github_sources")
        except Exception:  # noqa: BLE001 —— 表还没建
            pass
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("GITHUB_TOKEN", "GITHUB_API_BASE", "GITHUB_HOST", "GITHUB_OWNER",
                  "GITHUB_CLONE_BASE", "GITHUB_TOKEN_FILE"):
            os.environ.pop(k, None)
        self.addCleanup(self._env.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: gp._cache.update({"repos": None, "at": 0.0, "by": {}}))
        def _clean_sources():
            try:
                db.execute("DELETE FROM github_sources")
            except Exception:  # noqa: BLE001
                pass
        self.addCleanup(_clean_sources)

    def tearDown(self):
        for k in list(gs.KEYS) + ["token"]:
            try:
                gs._del(k)
            except Exception:  # noqa: BLE001
                pass
        super().tearDown()


class TestParseSource(_Base):
    """输入归一化：四种写法 + 拒绝面。"""

    def test_bare_owner(self):
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com"})
        f, note = gs.parse_source("gztxt")
        self.assertEqual(f, {"api_base": "https://api.github.com",
                             "git_host": "github.com", "owner": "gztxt"})
        self.assertEqual(note, "")

    def test_account_url(self):
        f, _n = gs.parse_source("https://github.com/gztxt")
        self.assertEqual((f["api_base"], f["git_host"], f["owner"]),
                         ("https://api.github.com", "github.com", "gztxt"))

    def test_repo_url_groups_by_account(self):
        f, note = gs.parse_source("https://github.com/gztxt/agenthub")
        self.assertEqual(f["owner"], "gztxt")
        self.assertIn("按账号归组", note)

    def test_ghes_api_base(self):
        f, _n = gs.parse_source("https://git.example.com/api/v3")
        self.assertEqual((f["api_base"], f["git_host"], f["owner"]),
                         ("https://git.example.com/api/v3", "git.example.com", ""))

    def test_ghes_web_url_derives_api_v3(self):
        f, note = gs.parse_source("https://git.example.com/acme")
        self.assertEqual(f["api_base"], "https://git.example.com/api/v3")
        self.assertEqual(f["owner"], "acme")
        self.assertIn("GHES", note)

    def test_starred_form(self):
        f, note = gs.parse_source("starred:gztxt")
        self.assertEqual(f["owner"], "gztxt")
        self.assertIn("starred", note)

    def test_rejects_plaintext_private_and_credentials(self):
        for bad in ("http://github.com/gztxt", "https://127.0.0.1/api/v3",
                    "https://localhost/acme", "https://u:p@github.com/gztxt",
                    "gztxt/agenthub", "https://github.com/a/b?x=1"):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs.parse_source(bad)


class TestSourceStore(_Base):
    """源列表：空表物化默认源 / 去重 / 启停 / 删除 / 全局镜像同步。"""

    def test_ensure_default_mirrors_global(self):
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com",
                  "owner": "acme"})
        rows = gs.ensure_default()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["id"], "default")
        self.assertEqual(rows[0]["owner"], "acme")
        self.assertEqual(len(gs.ensure_default()), 1, "ensure_default 必须幂等")

    def test_add_dedupes_same_address_and_owner(self):
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com"})
        a = gs.source_add("gztxt")
        b = gs.source_add("https://github.com/gztxt/agenthub")
        self.assertFalse(a["deduped"])
        self.assertTrue(b["deduped"], "同地址同账号必须复用，不能出现两条")
        self.assertEqual(a["id"], b["id"])

    def test_two_owners_two_rows_and_separate_hosts(self):
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com"})
        gs.source_add("acme")
        gs.source_add("https://git.example.com/api/v3")
        rows = gs.sources_all()
        self.assertEqual(len(rows), 2)
        self.assertEqual([r["git_host"] for r in rows],
                         ["github.com", "git.example.com"])

    def test_disable_and_delete(self):
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com"})
        s = gs.source_add("acme")["source"]
        gs.source_patch(s["id"], enabled=False)
        self.assertEqual(gs.sources_enabled(), [])
        gs.source_delete(s["id"])
        self.assertEqual(gs.sources_all(), [])

    def test_apply_mirrors_default_source_until_user_edits_it(self):
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com",
                  "owner": "acme"})
        self.assertEqual(gs.ensure_default()[0]["owner"], "acme")
        gs.apply({"owner": "globex"})
        self.assertEqual(gs.ensure_default()[0]["owner"], "globex",
                         "默认源只是全局设置的镜像，改全局必须跟着走")
        # 用户单独改过默认源 ⇒ 不再镜射（否则「我改的又变回去了」）
        gs.source_patch("default", label="acme 专用")
        db.execute("UPDATE github_sources SET owner='oldco' WHERE id='default'")
        gs.apply({"owner": "initech"})
        self.assertEqual(gs.ensure_default()[0]["owner"], "oldco")


class TestGrouping(_Base):
    """多源拉取 → 分组信封；star 去重；本地匹配按 host 分账；缓存口径。"""

    def setUp(self):
        super().setUp()
        gs.apply({"api_base": "https://api.github.com", "git_host": "github.com"})
        gs.source_add("acme")
        gs.source_add("https://git.example.com/api/v3")
        self.rows = gs.sources_all()
        self.calls = []

        def own(base, owner_, tok):
            self.calls.append(("own", base, owner_))
            if "example.com" in base:
                return [_repo("beta/tool")], None
            return [_repo("acme/app"), _repo("acme/forked", fork=True),
                    _repo("acme/starred-already")], None

        def star(base, owner_, tok):
            self.calls.append(("star", base, owner_))
            if "example.com" in base:
                return [_repo("other/lib")], None
            return [_repo("acme/starred-already"), _repo("someone/awesome")], None

        self._p = [mock.patch.object(gp, "_read_token", lambda: "ghp_supersecret_abcdef123456"),
                   mock.patch.object(gp, "_fetch_own", own),
                   mock.patch.object(gp, "_fetch_starred", star),
                   mock.patch.object(gp, "_slugs", lambda host: {})]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    def test_groups_counts_and_star_dedup(self):
        d = gp._list_repos(force=True)
        self.assertEqual(len(d["groups"]), 2)
        g0 = next(g for g in d["groups"] if g["git_host"] == "github.com")
        self.assertEqual(g0["counts"]["own"], 2)      # app + starred-already
        self.assertEqual(g0["counts"]["fork"], 1)
        self.assertEqual(g0["counts"]["star"], 1)     # someone/awesome（自己的去重）
        self.assertEqual(g0["counts"]["starred_total"], 2)
        self.assertEqual(d["count"], 4 + 2)
        self.assertEqual(d["counts"]["fork"], 1)

    def test_buckets_and_flat_idx_unique(self):
        d = gp._list_repos(force=True)
        self.assertEqual(sorted({r["bucket"] for r in d["repos"]}),
                         ["fork", "own", "star"])
        self.assertEqual([r["idx"] for r in d["repos"]], list(range(len(d["repos"]))))
        by = {(r["source_label"], r["full_name"]): r for r in d["repos"]}
        self.assertTrue(by[("acme", "acme/starred-already")]["starred"])
        self.assertEqual(by[("acme", "someone/awesome")]["bucket"], "star")

    def test_local_match_is_per_host(self):
        def slugs(host):
            return {"beta/tool": ["/local/beta-tool"]} if host == "git.example.com" else {}
        with mock.patch.object(gp, "_slugs", slugs):
            d = gp._list_repos(force=True)
        tool = next(r for r in d["repos"] if r["full_name"] == "beta/tool")
        self.assertTrue(tool["local"]["found"])
        self.assertEqual(tool["git_host"], "git.example.com")
        self.assertEqual(d["local_total"], 1)

    def test_per_source_cache_and_force(self):
        gp._list_repos(force=True)
        n_own = len([c for c in self.calls if c[0] == "own"])
        d = gp._list_repos()
        self.assertEqual(len([c for c in self.calls if c[0] == "own"]), n_own,
                         "非 force 必须吃缓存")
        self.assertTrue(d["cached"])
        gp._list_repos(force=True)
        self.assertEqual(len([c for c in self.calls if c[0] == "own"]), n_own * 2,
                         "force=1 每个源都要重拉")

    def test_token_missing_is_degraded_per_group(self):
        with mock.patch.object(gp, "_read_token", lambda: ""):
            d = gp._list_repos(force=True)
        self.assertTrue(d["ok"], "查不了 ≠ 没有：信封仍 ok")
        self.assertEqual(d["repos"], [])
        self.assertEqual(len(d["groups"]), 2)
        self.assertTrue(all(not g["ok"] and g["errors"] for g in d["groups"]))
        self.assertTrue(all("token" in " ".join(g["errors"]) for g in d["groups"]))

    def test_token_value_never_in_envelope(self):
        d = gp._list_repos(force=True)
        flat = {"x": d}
        import json as _json
        self.assertNotIn("ghp_supersecret", _json.dumps(flat))


class _Req:
    def __init__(self):
        self.method = "POST"
        self.url = types.SimpleNamespace(path="/api/github/clone", query="")
        self.headers = {}
        self.client = types.SimpleNamespace(host="127.0.0.1")


class TestCloneUsesSourceHost(_Base):
    def setUp(self):
        super().setUp()
        self.rows = [
            {"id": "s1", "label": "acme", "api_base": "https://api.github.com",
             "git_host": "github.com", "owner": "acme"},
            {"id": "s2", "label": "beta", "api_base": "https://git.example.com/api/v3",
             "git_host": "git.example.com", "owner": "beta"},
        ]
        self.base = self.tmp / "clones"
        self.base.mkdir()
        self.argv = []

        async def fake_spawn(argv):
            self.argv.append(argv)
            return 0, "", ""

        self._p = [
            mock.patch.object(gp, "_source_rows", lambda: self.rows),
            mock.patch.object(gp, "_read_token", lambda: "ghp_supersecret_abcdef123456"),
            mock.patch.object(gp, "_fetch_own", lambda base, own, tok: (
                ([_repo("beta/tool")] if own == "beta" else [_repo("acme/app")]), None)),
            mock.patch.object(gp, "_fetch_starred", lambda base, own, tok: ([], None)),
            mock.patch.object(gp, "_resolve_dest",
                              lambda slug: ("x", str(self.base / slug.split("/")[-1]))),
            mock.patch.object(gp, "_spawn_clone", fake_spawn),
            mock.patch.object(gp, "_origin_slug_of", lambda dest, host=None: "beta/tool"),
            mock.patch.object(gp, "writeauth",
                              types.SimpleNamespace(decide_req=lambda req, m: ("allow", "t"),
                                                    actor_of=lambda req: "test")),
            mock.patch.object(gp, "_remote_slugs", lambda host=None: {}),
        ]
        for p in self._p:
            p.start()
            self.addCleanup(p.stop)

    def test_clone_url_uses_that_source_host(self):
        asyncio.run(gp.github_clone(_Req(), gp.CloneIn(repo="beta/tool", source_id="s2")))
        self.assertEqual(self.argv[0][-2], "https://git.example.com/beta/tool.git")

    def test_repo_from_other_source_rejected(self):
        with self.assertRaises(Exception) as cm:
            asyncio.run(gp.github_clone(_Req(), gp.CloneIn(repo="acme/app", source_id="s2")))
        self.assertEqual(cm.exception.status_code, 400)

    def test_unknown_source_rejected(self):
        with self.assertRaises(Exception) as cm:
            asyncio.run(gp.github_clone(_Req(), gp.CloneIn(repo="beta/tool", source_id="nope")))
        self.assertEqual(cm.exception.status_code, 404)


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestFrontendWiring(unittest.TestCase):
    """设置页「仓库源」+ 项目页分组渲染的四件套（HTML id / JS 函数 / 无 inline onclick）。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_settings_source_ids_and_actions(self):
        for dom_id in ("ghSrcInput", "ghSrcList"):
            self.assertIn(f'id="{dom_id}"', self.html, f"#{dom_id} 缺失 ⇒ 添加源无处可点")
        self.assertIn('data-settings-act="gh-src-add"', self.html)
        for act in ("gh-src-add", "gh-src-toggle", "gh-src-del"):
            self.assertIn(f"'{act}'", self.js, f"{act} 没有委托分支 ⇒ 点了没反应")

    def test_loader_functions_exist(self):
        for fn in ("settingsGithubSources", "ghRenderSources",
                   "settingsGithubSourceAdd", "settingsGithubSourceToggle",
                   "settingsGithubSourceDel"):
            self.assertIn(f"function {fn}(", self.js, fn)

    def test_project_page_groups(self):
        self.assertIn("var GH_GROUPS = []", self.js)
        self.assertIn("gh-grp", self.js, "分组容器缺失 ⇒ 仍旧平铺")
        i = self.js.find("function ghRenderList(")
        # v0.13.198：函数里先建 tab 条再出三段，窗口从 2200 放宽到 3000（过窄=假红）
        body = self.js[i:i + 3000]
        for token in ("自建", "fork", "star", "counts"):
            self.assertIn(token, body, f"分组渲染缺少 {token} 段")

    def test_clone_and_sync_carry_source(self):
        i = self.js.find("async function ghStart(")
        self.assertIn("source_id", self.js[i:i + 1600])
        j = self.js.find("async function ghSyncCheck(")
        self.assertIn("source=", self.js[j:j + 900])

    def test_no_inline_onclick_in_source_block(self):
        i = self.html.find('<div class="set-sub">仓库源')
        j = self.html.find('id="ghSrcList"', i)
        self.assertGreater(i, 0)
        self.assertNotIn("onclick=", self.html[i:j],
                         "仓库源按钮必须走 data-settings-act 委托")


class TestSelfVsOtherVisibility(_Base):
    """自有账号走 /user/*（含私有仓）；别人账号走 /users/<name>/*（仅公开）。"""

    def _calls(self, owner_, login):
        calls = []

        def pages(url, tok):
            calls.append(url)
            return [], None

        with mock.patch.object(gp, "_read_token", lambda: "tok"), \
                mock.patch.object(gp, "_token_login", lambda base, tok: login), \
                mock.patch.object(gp, "_gh_pages", pages):
            gp._fetch_own("https://api.github.com", owner_, "tok")
            gp._fetch_starred("https://api.github.com", owner_, "tok")
        return calls

    def test_self_uses_user_endpoints(self):
        calls = self._calls("gztxt", "gztxt")
        self.assertIn("/user/repos", calls[0], "自己的账号必须带私有仓")
        self.assertIn("/user/starred", calls[1])

    def test_other_uses_public_endpoints(self):
        calls = self._calls("acme", "gztxt")
        self.assertIn("/users/acme/repos", calls[0])
        self.assertIn("/users/acme/starred", calls[1])

    def test_login_probe_failure_falls_back_to_public(self):
        calls = self._calls("acme", "")
        self.assertIn("/users/acme/repos", calls[0], "探不到身份按公开面处理，不猜")


class TestTabbedGroups(unittest.TestCase):
    """v0.13.198：用户要求「设置 github 和 github 项目都要是标签页分组，tab 名＝仓库名 gztxt」。

    判据三件（少一件就是「还是竖排清单」）：
      ① 两处都有 tab 条容器（设置 #ghSrcTabs / 项目页 tabstrip 渲染进 #ghList）；
      ② tab 名取归属账号（owner → label → host → 默认），两处同一口径；
      ③ 点击走既有事件委托（设置 data-settings-act=gh-src-tab，项目页 data-gh-tab），
         不许 inline onclick；也不许把当前 tab 写进 localStorage（偏好不改交互分支）。
    """

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_css_and_settings_container(self):
        self.assertIn(".tabstrip", self.html, "缺 tab 条样式 ⇒ 两个 tab 会挤成一坨")
        self.assertIn('id="ghSrcTabs"', self.html, "设置页缺 tab 条宿主")
        i = self.html.find('id="ghSrcTabs"')
        self.assertIn("tabstrip", self.html[i:i + 80], "tab 条宿主必须带 tabstrip 类")

    def test_tab_name_is_owner_first(self):
        for fn in ("function ghGroupTabName(", "function ghSourceTabName("):
            self.assertIn(fn, self.js, fn + " 缺失 ⇒ tab 名无从取名")
        i = self.js.find("function ghGroupTabName(")
        body = self.js[i:i + 200]
        self.assertIn("owner", body)
        self.assertLess(body.find("owner"), body.find("label"), "owner 必须优先于 label")

    def test_both_pages_render_tabstrip(self):
        i = self.js.find("function ghRenderList(")
        body = self.js[i:i + 1600]
        self.assertIn("tabstrip", body, "项目页没渲染 tab 条 ⇒ 仍是一屏堆所有源")
        self.assertIn("ghGroupTab", body)
        self.assertIn("data-gh-tab", body)
        j = self.js.find("function ghRenderSources(")
        sbody = self.js[j:j + 1800]
        self.assertIn("ghSrcTabs", sbody, "设置页没渲染 tab 条")
        self.assertIn("gh-src-tab", sbody)

    def test_tab_click_goes_through_delegate(self):
        self.assertIn("ds.ghTab != null", self.js, "项目页 tab 点击没有委托分支 ⇒ 点了没反应")
        self.assertIn("'gh-src-tab'", self.js, "设置页 tab 点击没有委托分支")
        # tab 条本身不许出现 inline onclick（委托是唯一出口）
        for anchor in ("function ghRenderList(", "function ghRenderSources("):
            k = self.js.find(anchor)
            seg = self.js[k:k + 1800]
            self.assertNotIn("onclick=", seg, anchor + " 内出现 inline onclick")

    def test_active_tab_not_persisted(self):
        for key in ("hub.gh.tab", "hub.ghsrc.tab", "ghGroupTab", "ghSrcTab"):
            self.assertNotIn("lsSet('" + key, self.js)
            self.assertNotIn("lsGet('" + key, self.js)
