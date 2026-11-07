#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：GitHub 项目页（src/githubprojects.py + 前端四件套）。

夹具：网络与子进程全部在接缝处 mock（_fetch_all / localprojects._scan_roots /
_spawn_clone），tmp 夹具走 ~ 下专用目录（P1 把 /tmp 排除、.cache 被 P4 剪——
见 test_localprojects 的 _mktmp）。零宿主依赖：不打真 GitHub、不真克隆。

为什么这些用例必须存在：
1. **slug 解析是安全边界**：非 github.com host 必须解析失败（None），否则
   `https://evil.com/github.com/x` 一类 URL 会让本地匹配误配 + 审计记错账。
2. **token 三律**：env 优先/文件首行/读不到为空——读不到要降级（「查不了」≠
   「没有」）而不是 500；上游错误正文必须过 scrub 且把 token 作位置参数
   （ghp_ 不在 _KEY_PATTERNS，位置替换才杀得掉）。
3. **克隆目标四闸**：REPO_RE 形状 + 白名单成员 + CLONE_BASE containment +
   冲突态——四闸任何一闸漏，POST 就变成「在任意路径写任意目录」。
4. **无 shell**：argv 列表 + credential.helper= + GIT_TERMINAL_PROMPT=0 是
   无人值守 NAS 上克隆不挂死的三件套。
5. **前端四件套**：section / 按钮位置（本机项目与 navTree 之间）/ PAGE_LABELS /
   go() 懒加载钩子，缺一即静默退回总览或页面空白。
"""
import asyncio
import json
import os
import pathlib
import re
import shutil
import sqlite3
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
import githubprojects as gp     # noqa: E402
import localprojects as lp      # noqa: E402

_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))


def _mktmp(prefix: str) -> pathlib.Path:
    _L0_TMP.mkdir(parents=True, exist_ok=True)
    for _ in range(8):
        d = _L0_TMP / (prefix + str(os.getpid()) + "-" + os.urandom(4).hex())
        try:
            d.mkdir()
            # v0.13.58：登记到进程级清单，退出时统一删。此前造完从不删，
            # 实测累积 230MB/2402 个目录 ⇒ 被项目扫描当真实项目（176 vs 应 ≈44）。
            return tiers.l0_fixture_register(d)
        except FileExistsError:
            continue
    raise RuntimeError("造夹具目录失败")


def _mkrepo(root: pathlib.Path, name: str, origin_url: str) -> pathlib.Path:
    d = root / name
    (d / ".git").mkdir(parents=True, exist_ok=True)
    (d / ".git" / "config").write_text(
        f"[remote \"origin\"]\n\turl = {origin_url}\n", encoding="utf-8")
    return d


class _Req:
    """照抄 test_cloudcli/test_runlog 的桩：method/path/headers/client。"""

    def __init__(self, token="good", path="/api/github/clone"):
        self.method = "POST"
        self.url = types.SimpleNamespace(path=path, query="")
        self.headers = types.SimpleNamespace(
            raw=[(b"x-hub-token", token.encode())] if token else [])
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="user:term-token")


class TestSlugParsing(unittest.TestCase):
    """remote URL → slug 的形态面 + 非 github host 拒绝。"""

    def test_https_forms(self):
        self.assertEqual(gp._remote_slug("https://github.com/gztxt/agent-hub"),
                         "gztxt/agent-hub")
        self.assertEqual(gp._remote_slug("https://github.com/gztxt/agent-hub.git"),
                         "gztxt/agent-hub")
        self.assertEqual(gp._remote_slug("https://GitHub.com/gztxt/agent-hub/"),
                         "gztxt/agent-hub")
        self.assertEqual(gp._remote_slug("http://www.github.com/gztxt/x"), "gztxt/x")

    def test_ssh_forms(self):
        self.assertEqual(gp._remote_slug("git@github.com:gztxt/agent-hub.git"),
                         "gztxt/agent-hub")
        self.assertEqual(gp._remote_slug("ssh://git@github.com/gztxt/x"),
                         "gztxt/x")

    def test_non_github_host_rejected(self):
        for bad in ("https://evil.com/github.com/gztxt/x.git",
                    "https://gitee.com/gztxt/x", "git@gitlab.com:gztxt/x",
                    "file:///tmp/x", "/local/path", ""):
            self.assertIsNone(gp._remote_slug(bad), f"{bad!r} 必须解析失败")


class TestToken(unittest.TestCase):
    def test_env_wins_over_file(self):
        tmp = _mktmp("ghtok-")
        f = tmp / "github.txt"
        f.write_text("from-file\n", encoding="utf-8")
        with mock.patch.object(gp, "TOKEN_FILE", f), \
                mock.patch.dict(os.environ, {"GITHUB_TOKEN": "from-env"}):
            self.assertEqual(gp._read_token(), "from-env")

    def test_file_first_line(self):
        tmp = _mktmp("ghtok-")
        f = tmp / "github.txt"
        f.write_text("tok-A\ntok-B\n", encoding="utf-8")
        with mock.patch.object(gp, "TOKEN_FILE", f), \
                mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("GITHUB_TOKEN", None)
            self.assertEqual(gp._read_token(), "tok-A")

    def test_missing_file_is_empty(self):
        with mock.patch.object(gp, "TOKEN_FILE", pathlib.Path("/nonexistent/x.txt")):
            self.assertEqual(gp._read_token(), "")

    def test_token_never_in_envelope(self):
        """信封只带 token:bool——值永不外泄。"""
        with mock.patch.object(gp, "_read_token", lambda: "ghp_supersecret1234567890"):
            d = gp._list_repos(force=True)
        flat = json.dumps(d, ensure_ascii=False)
        self.assertNotIn("ghp_supersecret", flat)
        self.assertIn("token", d)


class TestListEnvelope(unittest.TestCase):
    """_list_repos 信封形态 + strict remote 本地匹配。"""

    def setUp(self):
        self.tmp = _mktmp("ghlist-")
        self.root = self.tmp / "root"
        self.root.mkdir()
        # 同仓异名命中 + 无 remote 仓不匹配 + 非 github remote 不匹配
        _mkrepo(self.root, "scripts", "https://github.com/gztxt/techdocs-scripts.git")
        _mkrepo(self.root, "lonely", "https://example.com/other.git")
        (self.root / "noremote").mkdir()
        (self.root / "noremote" / ".git").mkdir()
        (self.root / "noremote" / ".git" / "config").write_text(
            "[core]\n", encoding="utf-8")
        self._saved_roots = lp.ROOTS
        lp.ROOTS = [str(self.root)]
        self.addCleanup(self._restore)

    def _restore(self):
        lp.ROOTS = self._saved_roots
        gp._cache["repos"] = None
        gp._cache["at"] = 0.0
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_root_itself_repo_matched(self):
        """根自身是仓库（技术文档=gztxt/technical-docs 一类）不在本机项目列表
        里（root 不列），但 GitHub 页必须显示「本地已有」——否则点它会去克隆
        一个目录已存在的仓。"""
        _mkrepo(self.root.parent, "rootrepo", "https://github.com/gztxt/rootrepo.git")
        # rootrepo 作为 ROOT 自身（扫描不列 root，但 slug 映射必须收）
        lp.ROOTS = [str(self.root.parent / "rootrepo"), str(self.root)]
        with mock.patch.object(gp, "_read_token", lambda: "tok"), \
                mock.patch.object(gp, "_fetch_all", lambda tok: {"ok": True, "repos": [
                    {"full_name": "gztxt/rootrepo", "name": "rootrepo",
                     "owner": {"login": "gztxt"}, "fork": False, "private": False,
                     "archived": False, "default_branch": "main", "language": None,
                     "description": None, "pushed_at": "2026-09-26T10:00:00Z"}]}):
            gp._cache["repos"] = None
            d = gp._list_repos(force=True)
        r0 = d["repos"][0]
        self.assertTrue(r0["local"]["found"], "根仓库必须匹配本地已有")
        self.assertEqual(r0["local"]["path"], str(self.root.parent / "rootrepo"))

    def _fake_repos(self):
        return [
            {"full_name": "gztxt/techdocs-scripts", "name": "techdocs-scripts",
             "owner": {"login": "gztxt"}, "fork": False, "private": False,
             "archived": False, "default_branch": "main", "language": "Shell",
             "description": "d", "pushed_at": "2026-09-26T10:00:00Z"},
            {"full_name": "gztxt/lonely", "name": "lonely",
             "owner": {"login": "gztxt"}, "fork": True, "private": False,
             "archived": False, "default_branch": "main", "language": None,
             "description": None, "pushed_at": "2026-09-20T10:00:00Z"},
        ]

    def test_envelope_shape_and_local_join(self):
        with mock.patch.object(gp, "_read_token", lambda: "tok"), \
                mock.patch.object(gp, "_fetch_all", lambda tok: {"ok": True, "repos": self._fake_repos()}):
            gp._cache["repos"] = None
            d = gp._list_repos(force=True)
        for k in ("ok", "count", "repos", "cached", "age_s", "errors",
                  "token", "local_total", "took_ms"):
            self.assertIn(k, d)
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["local_total"], 1)
        by_fn = {r["full_name"]: r for r in d["repos"]}
        # strict remote：本地 scripts ↔ 远端 techdocs-scripts（同仓异名）命中
        self.assertTrue(by_fn["gztxt/techdocs-scripts"]["local"]["found"])
        self.assertEqual(by_fn["gztxt/techdocs-scripts"]["local"]["path"],
                         str(self.root / "scripts"))
        # 本地 lonely 的 remote 是 example.com——与远端 gztxt/lonely 不是同仓
        self.assertFalse(by_fn["gztxt/lonely"]["local"]["found"])
        # 克隆 URL 不下发
        flat = json.dumps(d)
        self.assertNotIn("clone_url", flat)
        self.assertNotIn("ssh_url", flat)

    def test_degraded_when_token_unavailable(self):
        with mock.patch.object(gp, "_read_token", lambda: ""):
            gp._cache["repos"] = None
            d = gp._list_repos(force=True)
        self.assertTrue(d["ok"], "token 不可用是降级不是失败（「查不了」≠「没有」）")
        self.assertEqual(d["repos"], [])
        self.assertTrue(any("token" in e for e in d["errors"]))

    def test_upstream_error_scrubbed(self):
        """_fetch_all 的 HTTPError 路径：错误正文里的 token 必须被 scrub
        （ghp_ 不在 _KEY_PATTERNS，位置参数替换才杀得掉）。"""
        import urllib.error as _ue
        tok = "ghp_fake_token_1234567890abcdef"

        class _Err(_ue.HTTPError):
            def __init__(self):
                super().__init__("https://api.github.com", 401, "Unauthorized",
                                 None, None)

            def read(self):
                return ("authorization failed for token " + tok).encode()

        def fake_urlopen(req, timeout=None):
            raise _Err()

        with mock.patch.object(gp, "_read_token", lambda: tok), \
                mock.patch.object(gp.urllib.request, "urlopen", fake_urlopen):
            gp._cache["repos"] = None
            d = gp._list_repos(force=True)
        joined = "\n".join(d["errors"])
        self.assertNotIn(tok, joined, "上游错误正文必须把 token 洗掉")
        self.assertIn("<redacted>", joined)

    def test_cache_forever_until_force(self):
        """v0.13.33 用户裁定「拉取一次后本地缓存」：内存缓存**永不过期**——
        重复调用吃缓存（fetch 计数不涨）；只有 force=1（页面「刷新」按钮）
        才重拉。以前是 5 分钟 TTL，用户点名「每次拉取就是浪费资源」。"""
        calls = {"n": 0}

        def fake_fetch(tok):
            calls["n"] += 1
            return {"ok": True, "repos": self._fake_repos()}

        with mock.patch.object(gp, "_read_token", lambda: "tok"), \
                mock.patch.object(gp, "_fetch_all", fake_fetch):
            gp._cache["repos"] = None
            gp._list_repos(force=True)
            d2 = gp._list_repos()                    # 永久缓存：吃缓存
            d3 = gp._list_repos()                    # 再来一次：还是缓存
            self.assertTrue(d2["cached"] and d3["cached"])
            self.assertEqual(calls["n"], 1, "非 force 调用绝不重拉")
            # 龄期再大也不过期（旧 TTL 语义的对照：这里曾经会重拉）
            gp._cache["at"] = 0.0
            gp._list_repos()
            self.assertEqual(calls["n"], 1, "龄期不影响缓存（永久有效）")
            # force=1 = 唯一重拉入口
            gp._list_repos(force=True)
            self.assertEqual(calls["n"], 2, "force=1 必须重拉")


class TestCloneGuard(unittest.TestCase):
    """POST /api/github/clone 的四闸 + 冲突态 + writeauth fail-closed。"""

    def setUp(self):
        self.tmp = _mktmp("ghclone-")
        self.base = self.tmp / "clones"
        self.base.mkdir()
        self._saved = (gp.CLONE_BASE, lp.ROOTS, gp._cache["repos"], gp._cache["at"])
        gp.CLONE_BASE = str(self.base)
        lp.ROOTS = []
        gp._cache["repos"] = [{"full_name": "gztxt/wantrepo", "name": "wantrepo"}]
        gp._cache["at"] = 1e12                       # 缓存永不过期
        self.addCleanup(self._restore)

    def _restore(self):
        gp.CLONE_BASE, lp.ROOTS, c_repos, c_at = self._saved
        gp._cache["repos"], gp._cache["at"] = c_repos, c_at
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _env(self, term="good", passcode=""):
        return mock.patch.multiple(os, environ={**os.environ,
                                                "TERM_TOKEN": term,
                                                "HUB_PASSCODE": passcode})

    def test_repo_re_rejects(self):
        """形状闸只拒「结构非法」（多段/空格/整条 URL）；「.」段留给
        _resolve_dest 的名字闸（gztxt/.. 的 name=.. 在那里 400）。"""
        for bad in ("a/b/c", "own repo", "gztxt/x\n",
                    "https://github.com/a/b", "a//b", "/a/b", "a/b/", ""):
            self.assertIsNone(gp.REPO_RE.fullmatch(bad), f"{bad!r} 必须被形状闸拒绝")

    def test_dotdot_name_rejected_by_resolve(self):
        with self.assertRaises(ValueError):
            gp._resolve_dest("gztxt/..")
        with self.assertRaises(ValueError):
            gp._resolve_dest("gztxt/.")

    async def _call(self, repo, token="good"):
        with mock.patch.object(gp, "_spawn_clone",
                               lambda argv: asyncio.sleep(0, result=(0, "", ""))):
            return await gp.github_clone(_Req(token), gp.CloneIn(repo=repo))

    def test_membership_whitelist(self):
        with self._env():
            with self.assertRaises(Exception) as cm:
                asyncio.run(self._call("gztxt/not-in-list"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_clone_target_containment(self):
        """真实防线：name=.. / name=. 的路径穿越在 _resolve_dest 400。
        .git 之类段名不用拦——白名单先要求它是远端真仓全名，实际不可达。"""
        with self.assertRaises(ValueError):
            gp._resolve_dest("gztxt/..")
        with self.assertRaises(ValueError):
            gp._resolve_dest("gztxt/.")
        # 正常 name 必须落在 base 之下
        _n, dest = gp._resolve_dest("gztxt/agent-hub")
        self.assertTrue(dest.startswith(gp.CLONE_BASE + os.sep))

    def test_collision_same_repo_idempotent(self):
        d = self.base / "wantrepo"
        (d / ".git").mkdir(parents=True)
        (d / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://github.com/gztxt/wantrepo.git\n',
            encoding="utf-8")
        with self._env():
            r = asyncio.run(self._call("gztxt/wantrepo"))
        self.assertTrue(r["existed"], "同仓已存在必须幂等返回")
        self.assertEqual(r["path"], str(d))

    def test_collision_foreign_repo_409(self):
        d = self.base / "wantrepo"
        (d / ".git").mkdir(parents=True)
        (d / ".git" / "config").write_text(
            '[remote "origin"]\n\turl = https://github.com/gztxt/other.git\n',
            encoding="utf-8")
        with self._env():
            with self.assertRaises(Exception) as cm:
                asyncio.run(self._call("gztxt/wantrepo"))
        self.assertEqual(cm.exception.status_code, 409)

    def test_collision_plain_dir_409(self):
        (self.base / "wantrepo").mkdir()
        with self._env():
            with self.assertRaises(Exception) as cm:
                asyncio.run(self._call("gztxt/wantrepo"))
        self.assertEqual(cm.exception.status_code, 409)

    def test_collision_symlink_400(self):
        real = self.tmp / "realdir"
        real.mkdir()
        os.symlink(str(real), self.base / "wantrepo")
        with self._env():
            with self.assertRaises(Exception) as cm:
                asyncio.run(self._call("gztxt/wantrepo"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_writeauth_fail_closed(self):
        with self._env():
            with self.assertRaises(Exception) as cm:
                asyncio.run(self._call("gztxt/wantrepo", token=""))
            # 无身份必须拒：用户库可用=401，不可用=503（fail-closed，二者都不是放行）
            self.assertIn(cm.exception.status_code, (401, 503))
        with mock.patch.object(db, "is_open", lambda: False):
            with self.assertRaises(Exception) as cm2:
                asyncio.run(self._call("gztxt/wantrepo", token=""))
            self.assertEqual(cm2.exception.status_code, 503, "用户库不可用 fail-closed")

    def test_clone_failure_cleans_partial(self):
        """克隆 rc≠0：半成品（带 .git）必须被清走，绝不留残目录骗下一轮。"""
        async def fake_spawn(argv):
            # 模拟 git 克隆到一半失败：目录已带 .git
            pathlib.Path(argv[-1], ".git").mkdir(parents=True, exist_ok=True)
            return 128, "", "fatal: repository not found"
        with self._env(), mock.patch.object(gp, "_spawn_clone", fake_spawn):
            with self.assertRaises(Exception) as cm:
                asyncio.run(self._call("gztxt/wantrepo"))
        self.assertEqual(cm.exception.status_code, 502)
        self.assertFalse((self.base / "wantrepo").exists(),
                         "失败必须清理半成品目录")


class TestCloneDiscipline(unittest.TestCase):
    """argv 形态 + 全仓无 shell=True + 环境变量。"""

    def test_argv_shape(self):
        argv = gp._clone_argv("gztxt/agent-hub", "/tmp/dest")
        self.assertIsInstance(argv, list)
        self.assertTrue(all(isinstance(x, str) for x in argv))
        self.assertEqual(argv[0], "git")
        self.assertIn("credential.helper=", argv)
        self.assertIn("--depth", argv)
        self.assertEqual(argv[-2], "https://github.com/gztxt/agent-hub.git")
        self.assertEqual(argv[-1], "/tmp/dest")

    def test_no_shell_true_anywhere(self):
        """全仓纪律：git 子进程永不走 shell。只查代码（AST）——docstring 里
        写「无 shell=True」不该把自己判红。"""
        import ast
        tree = ast.parse((_REPO / "src" / "githubprojects.py").read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, ast.Call) and isinstance(node.func, ast.Name) \
                    and node.func.id == "Popen":
                for kw in node.keywords:
                    self.assertNotEqual(kw.arg, "shell", "Popen(shell=…) 禁用")

    def test_git_terminal_prompt_env_present(self):
        src = (_REPO / "src" / "githubprojects.py").read_text(encoding="utf-8")
        self.assertIn("GIT_TERMINAL_PROMPT", src,
                      "鉴权失败必须立即死（无人值守 NAS 不能挂起等输入）")

    def test_audit_call_shape(self):
        """克隆成功必须落审计（action=create，冻结枚举内）；detail 不带 token。
        匹配缩进后的真实调用点（docstring 里的提及不算）。"""
        import ast
        tree = ast.parse((_REPO / "src" / "githubprojects.py").read_text(encoding="utf-8"))
        calls = [n for n in ast.walk(tree)
                 if isinstance(n, ast.Call) and isinstance(n.func, ast.Attribute)
                 and n.func.attr == "log_asset_event"]
        self.assertTrue(calls, "clone handler 必须真的调 log_asset_event")
        c = calls[0]
        args = [getattr(a, "value", a) for a in c.args]
        flat = ast.dump(c)
        self.assertIn("repo", flat)
        self.assertIn("create", flat, "action 必须用冻结枚举里的 create")
        self.assertIn("actor_of", flat)
        # detail 字面量字典里不得出现 token 标识
        for kw in c.keywords:
            if kw.arg == "detail":
                self.assertNotIn("token", ast.dump(kw.value).lower())

    def test_constants_pin_user_decisions(self):
        self.assertEqual(gp.CLONE_BASE, os.path.abspath(
            os.getenv("GITHUB_CLONE_BASE", "/fs/1000/ftp/技术文档")))
        # v0.13.33 裁定：列表缓存永久（0 = 不过期；刷新按钮 force=1 才重拉）
        self.assertEqual(gp.LIST_TTL_S, 0)


class TestSyncEndpoint(unittest.TestCase):
    """GET /api/github/sync：单仓核对（「选中进入编辑前才同步」的后端半程）。
    只比对 HEAD，绝不整表重拉——1 次 git ls-remote 是唯一外部成本。"""

    def setUp(self):
        self.tmp = _mktmp("ghsync-")
        self.repo = _mkrepo(self.tmp, "myrepo",
                            "https://github.com/gztxt/myrepo.git")
        # 写一个本地 HEAD（main ref）
        gitdir = self.repo / ".git"
        (gitdir / "refs" / "heads").mkdir(parents=True, exist_ok=True)
        (gitdir / "HEAD").write_text("ref: refs/heads/main\n", encoding="utf-8")
        (gitdir / "refs" / "heads" / "main").write_text(
            "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111\n", encoding="utf-8")
        self._saved = lp.ROOTS
        lp.ROOTS = [str(self.tmp)]
        self.addCleanup(self._restore)

    def _restore(self):
        lp.ROOTS = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    async def _call(self, repo):
        return await gp.github_sync(repo, _Req(path="/api/github/sync"))

    def test_absent_state(self):
        with mock.patch.object(gp, "_remote_slugs", lambda: {}):
            d = asyncio.run(self._call("gztxt/myrepo"))
        self.assertEqual(d["state"], "absent")
        self.assertIsNone(d["local_head"])

    def test_synced_state(self):
        with mock.patch.object(gp, "_remote_slugs",
                               lambda: {"gztxt/myrepo": [str(self.repo)]}), \
                mock.patch.object(gp, "_gh_head",
                                  lambda url, tok: "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111"):
            d = asyncio.run(self._call("gztxt/myrepo"))
        self.assertEqual(d["state"], "synced")
        self.assertEqual(d["local_head"], "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")

    def test_diverged_state(self):
        """SHA 不同 ⇒ diverged（单值比对无法判谁新——本地可能是未 push 的
        新提交；绝不误指 git pull）。"""
        with mock.patch.object(gp, "_remote_slugs",
                               lambda: {"gztxt/myrepo": [str(self.repo)]}), \
                mock.patch.object(gp, "_gh_head",
                                  lambda url, tok: "bbbb2222bbbb2222bbbb2222bbbb2222bbbb2222"):
            d = asyncio.run(self._call("gztxt/myrepo"))
        self.assertEqual(d["state"], "diverged")
        self.assertIn("对齐", d["note"])

    def test_remote_unreachable_is_unknown_not_error(self):
        with mock.patch.object(gp, "_remote_slugs",
                               lambda: {"gztxt/myrepo": [str(self.repo)]}), \
                mock.patch.object(gp, "_gh_head", lambda url, tok: None):
            d = asyncio.run(self._call("gztxt/myrepo"))
        self.assertEqual(d["state"], "unknown", "取不到远端 = 不可比对，不是失败")

    def test_bad_repo_shape_400(self):
        with self.assertRaises(Exception) as cm:
            asyncio.run(self._call("bad shape"))
        self.assertEqual(cm.exception.status_code, 400)

    def test_local_head_reads_ref_without_git(self):
        """_local_head 纯文件读（.git/HEAD → refs/heads/main），不起 git。"""
        self.assertEqual(gp._local_head(str(self.repo)),
                         "aaaa1111aaaa1111aaaa1111aaaa1111aaaa1111")


class TestFrontendQuartet(unittest.TestCase):
    """静态断言：section / 按钮位置 / PAGE_LABELS / 懒加载钩子 / DOM ids。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_section_exists(self):
        self.assertIn('id="page-github"', self.html,
                      "page-github section 缺失 ⇒ go() 会静默退回总览")

    def test_sidebar_button_between_localprojects_and_navtree(self):
        """顺序：总览 → 本机项目 → GitHub 项目 → AGENTS（用户点名两页都在
        agents 菜单上面）。"""
        btn = self.html.find('data-sys="github"')
        lpbtn = self.html.find('data-sys="localprojects"')
        navtree = self.html.find('id="navTree"')
        self.assertGreater(btn, 0, "侧栏「GitHub 项目」按钮缺失")
        self.assertLess(lpbtn, btn, "GitHub 按钮必须在「本机项目」之后")
        self.assertLess(btn, navtree, "GitHub 按钮必须在 AGENTS（#navTree）之上")

    def test_page_label(self):
        self.assertIn("github: 'GitHub 项目'", self.js)

    def test_lazy_hook_paired(self):
        self.assertIn("page === 'github'", self.js)
        self.assertIn("var ghLoaded = false", self.js)

    def test_loader_functions_defined(self):
        for fn in ("loadGithubRepos", "ghRenderList", "ghSelect", "ghStart",
                   "ghRenderAgents", "ghFilter", "ghRowHtml"):
            self.assertIn(f"function {fn}(", self.js, f"{fn} 缺失 ⇒ 面板点了没反应")

    def test_dom_ids_used_by_js_exist_in_html(self):
        for dom_id in ("ghQ", "ghForks", "ghHidden", "ghHint", "ghList", "ghSelName",
                       "ghAgent", "ghStartBtn", "ghMeta"):
            self.assertIn(f'id="{dom_id}"', self.html,
                          f"#{dom_id} 在 JS 里被引用但 HTML 缺失 ⇒ null 崩溃")

    def test_start_two_paths(self):
        """ghStart 必须两段式：先 /api/github/clone（无本地时），再
        /api/term/sessions 带返回 path 作 cwd——「即时同步」的实现本体。"""
        i = self.js.find("async function ghStart(")
        self.assertGreater(i, 0)
        body = self.js[i:i + 1600]
        self.assertIn("/api/github/clone", body)
        self.assertIn("/api/term/sessions", body)
        self.assertIn("cwd: cwd", body)
        self.assertIn("{ user: true }", body)

    def test_inline_handlers_reference_real_functions(self):
        for m in re.finditer(r'onclick="(?!if\()([a-zA-Z_$][\w$]*)\(', self.html):
            fn = m.group(1)
            self.assertIn(f"function {fn}(", self.js,
                          f"HTML 引用 {fn}() 但 hub.js 未定义（点击即报错）")


class TestStarHideQuartet(unittest.TestCase):
    """v0.13.32 收藏/隐藏（GitHub 页，键 = full_name）+ 去面板大标题。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def _panel(self):
        i = self.html.find('id="page-github"')
        j = self.html.find('id="page-memory"')
        return self.html[i:j if j > i else len(self.html)]

    def test_row_action_functions_defined(self):
        for fn in ("ghToggleStar", "ghToggleHide"):
            self.assertIn(f"function {fn}(", self.js, f"{fn} 缺失 ⇒ 图标点了没反应")

    def test_state_persisted_via_guarded_ls(self):
        self.assertIn("'hub.gh.stars'", self.js)
        self.assertIn("'hub.gh.hidden'", self.js)
        self.assertNotIn("localStorage.setItem", self.js.replace(
            "window.localStorage.setItem", ""))       # 守卫内部那处除外

    def test_star_first_and_hidden_in_filter(self):
        i = self.js.find("function ghFilter(")
        body = self.js[i:i + 900]
        self.assertIn("ghHiddenSet.has", body, "ghFilter 必须过滤隐藏行")
        self.assertIn("ghHidden", body, "过滤必须受「显示隐藏」开关控制")
        r = self.js.find("function ghRenderList(")
        rbody = self.js[r:r + 1300]
        self.assertIn("ghStars.has", rbody, "渲染必须按收藏分流置顶")
        self.assertIn("concat", rbody)

    def test_panel_title_removed(self):
        gh_panel = self._panel()
        self.assertNotIn("<h3>", gh_panel, "GitHub 面板不应再有 <h3> 大标题")


if __name__ == "__main__":
    unittest.main(verbosity=2)
