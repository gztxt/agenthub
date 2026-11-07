#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：设置 → GitHub 子菜单（src/ghsettings.py + githubprojects 去硬编码）。

夹具：DB 走 tmp（db.init_db），token 文件与 .env 全部重定向到 tmp；网络只在
`ghsettings._gh_get` 这一个接缝上 mock——零真请求、零真凭据、零宿主依赖。

为什么这些用例必须存在（每条都对应一次真实事故或一类静默故障）：
1. **地址闸是凭据闸**：地址能填 ⇒ 就能把 key 明文发到任意主机。http 明文、
   127/10/192.168/169.254/[::1]、localhost/*.local、URL 里带 user:pass —— 任何
   一条漏，设置页就变成 SSRF + 凭据外泄入口（用户 09-27 裁定的正面）。
2. **key 只写不读**：掩码/来源可以出，真值一处都不能出——响应、日志、diff、
   审计、备份文件名都不行。写回 github.txt 的 diff 里也必须只有掩码。
3. **DB → env → 默认 三级**：改完必须即时生效（githubprojects 每次现读），
   清掉必须干净回落；层级错 = "改了没生效" 或 "清了还在"。
4. **回写文件是可选且必备份**：默认不写；要写则 .env 是共享配置面，无备份
   落笔等同不可逆（军规铁律）。
5. **口令门 fail-closed**：未配 HUB_PASSCODE ⇒ 503（不是放行）；错 ⇒ 401。
"""
import asyncio
import json
import os
import pathlib
import shutil
import sys
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))
sys.path.insert(0, str(_REPO / "tests"))

import tiers  # noqa: E402

os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import db                            # noqa: E402
import ghsettings as gs              # noqa: E402
import githubprojects as gp          # noqa: E402

_L0_TMP = pathlib.Path(os.getenv("HUB_L0_TMP",
                                 pathlib.Path.home() / "hub-l0test-fixtures"))

TOK = "ghp_fake_token_1234567890abcdef"


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


class _Req:
    def __init__(self, path="/api/settings/github/apply"):
        self.method = "POST"
        self.url = types.SimpleNamespace(path=path, query="")
        self.headers = types.SimpleNamespace(raw=[])
        self.client = types.SimpleNamespace(host="127.0.0.1")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = _mktmp("ghset-")
        db.init_db(self.tmp / "gh.db")
        for k in list(gs.KEYS) + ["token"]:
            gs._del(k)
        self.tokfile = self.tmp / "github.txt"
        self.envfile = self.tmp / ".env"
        self.envfile.write_text('HOME=/home/x\nGITHUB_API_BASE="https://old.example.com/api/v3"\n',
                                encoding="utf-8")
        self._patch = mock.patch.multiple(
            gs, ENV_FILE=self.envfile,
            DEFAULT_TOKEN_FILE=self.tokfile)
        self._patch.start()
        self.addCleanup(self._patch.stop)
        self._env = mock.patch.dict(os.environ, {}, clear=False)
        self._env.start()
        for k in ("GITHUB_TOKEN", "GITHUB_API_BASE", "GITHUB_HOST", "GITHUB_OWNER",
                  "GITHUB_CLONE_BASE", "GITHUB_TOKEN_FILE"):
            os.environ.pop(k, None)
        os.environ["GITHUB_TOKEN_FILE"] = str(self.tokfile)
        self.addCleanup(self._env.stop)
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.addCleanup(lambda: gp._cache.update({"repos": None, "at": 0.0}))

    def tearDown(self):
        """DB 是进程内共享的（init_db 指向 tmp）：token 行必须清干净，否则
        下一个用例的 _read_token() 会读到上一用例的 key（假绿/假红同源于此）。"""
        for k in list(gs.KEYS) + ["token"]:
            try:
                gs._del(k)
            except Exception:  # noqa: BLE001
                pass
        super().tearDown()


class TestHostGate(_Base):
    """地址闸：https 强制 + 内网/回环/明文/带凭据 一律拒。"""

    def test_accepts_public_https(self):
        for ok in ("https://api.github.com", "https://api.github.com/",
                   "https://git.example.com/api/v3", "https://github.company.com:8443/api/v3"):
            self.assertTrue(gs._check_api_base(ok).startswith("https://"), ok)

    def test_rejects_plaintext_and_scheme_abuse(self):
        for bad in ("http://api.github.com", "ftp://api.github.com",
                    "api.github.com", "//api.github.com"):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs._check_api_base(bad)

    def test_rejects_credentials_in_url(self):
        with self.assertRaises(gs.GhSettingsError):
            gs._check_api_base("https://user:tok@api.github.com")
        with self.assertRaises(gs.GhSettingsError):
            gs._check_api_base("https://api.github.com/?token=abc")

    def test_rejects_private_and_loopback(self):
        for bad in ("https://127.0.0.1/api/v3", "https://localhost/api/v3",
                    "https://10.1.2.3/api/v3", "https://192.168.1.10/api/v3",
                    "https://172.16.0.1/api/v3", "https://169.254.169.254/latest",
                    "https://[::1]/api/v3", "https://0.0.0.0/api/v3",
                    "https://nas.local/api/v3", "https://box.internal/api/v3"):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs._check_api_base(bad)

    def test_git_host_forms(self):
        self.assertEqual(gs._check_git_host("github.com"), "github.com")
        for bad in ("https://github.com", "github.com/x", "127.0.0.1", "local host"):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs._check_git_host(bad)

    def test_derive_git_host(self):
        self.assertEqual(gs.derive_git_host("https://api.github.com"), "github.com")
        self.assertEqual(gs.derive_git_host("https://git.example.com/api/v3"),
                         "git.example.com")

    def test_clone_base_and_owner_gate(self):
        d = self.tmp / "clones"
        self.assertEqual(gs._check_clone_base(str(d)), str(d))     # 不存在也允许（首次克隆创建）
        d.mkdir()
        self.assertEqual(gs._check_clone_base(str(d)), str(d))
        for bad in ("relative/path", "/", "/a/../../b", "/x\ny", ""):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs._check_clone_base(bad)
        self.assertEqual(gs._check_owner(""), "")
        self.assertEqual(gs._check_owner("gztxt"), "gztxt")
        for bad in ("own repo", "-lead", "a/b"):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs._check_owner(bad)

    def test_token_shape(self):
        self.assertEqual(gs._check_token(TOK), TOK)
        for bad in ("short", "has space in it", "x" * 301):
            with self.assertRaises(gs.GhSettingsError, msg=bad):
                gs._check_token(bad)


class TestTokenNeverLeaves(_Base):
    """key 只写不读：掩码可出，真值一处都不许出。"""

    def test_mask_hides_middle(self):
        m = gs.mask(TOK)
        self.assertNotIn("fake_token", m)
        self.assertTrue(m.startswith("ghp_"))
        self.assertTrue(m.endswith("cdef"))

    def test_view_has_no_raw_value(self):
        gs._set("token", TOK)
        flat = json.dumps(gs.view(), ensure_ascii=False)
        self.assertNotIn(TOK, flat)
        self.assertNotIn("fake_token", flat)
        self.assertTrue(gs.view()["token"]["set"])

    def test_diff_and_file_diff_only_masked(self):
        d = gs.diff({"token": TOK, "api_base": "https://api.github.com"}, write_files=True)
        flat = json.dumps(d, ensure_ascii=False)
        self.assertNotIn(TOK, flat)
        self.assertNotIn("fake_token", flat)
        self.assertTrue(any(c["key"] == "token" for c in d["changes"]))

    def test_probe_error_scrubs_token(self):
        """上游 401 正文里带 token：必须按位置洗掉（ghp_ 不在 _KEY_PATTERNS）。
        mock 打在 urlopen（不是 _gh_get）——scrub 就在 _gh_get 里，替掉它就测不到。"""
        import urllib.error as _ue

        class _Err(_ue.HTTPError):
            def __init__(self):
                super().__init__("https://api.github.com/user", 401, "Unauthorized", None, None)

            def read(self):
                return ("authorization failed for token " + TOK).encode()

        with mock.patch.object(gs.urllib.request, "urlopen",
                               lambda req, timeout=None: (_ for _ in ()).throw(_Err())):
            d = gs.probe("https://api.github.com", TOK)
        self.assertFalse(d["ok"])
        self.assertNotIn(TOK, json.dumps(d, ensure_ascii=False))
        self.assertIn("<redacted>", d["error"])


class TestResolutionChain(_Base):
    """DB → env → 默认：githubprojects 每次现读，改完即时生效。"""

    def test_defaults_without_anything(self):
        self.assertEqual(gp.api_base(), gs.DEFAULT_API_BASE)
        self.assertEqual(gp.git_host(), "github.com")
        self.assertEqual(gp.clone_base(), os.path.abspath(gp.CLONE_BASE))
        self.assertEqual(gs.sources()["api_base"], "default")

    def test_env_layer(self):
        with mock.patch.dict(os.environ, {"GITHUB_API_BASE": "https://git.example.com/api/v3",
                                          "GITHUB_HOST": "git.example.com",
                                          "GITHUB_OWNER": "acme",
                                          "GITHUB_CLONE_BASE": str(self.tmp)}):
            self.assertEqual(gp.api_base(), "https://git.example.com/api/v3")
            self.assertEqual(gp.git_host(), "git.example.com")
            self.assertEqual(gp.owner(), "acme")
            self.assertEqual(gp.clone_base(), str(self.tmp))
            self.assertEqual(gs.sources()["owner"], "env")

    def test_db_wins_and_takes_effect_immediately(self):
        base = self.tmp / "clones"
        gs.apply({"api_base": "https://git.example.com/api/v3",
                  "git_host": "git.example.com", "owner": "acme",
                  "clone_base": str(base)}, write_files=False)
        self.assertEqual(gp.api_base(), "https://git.example.com/api/v3")
        self.assertEqual(gp.git_host(), "git.example.com")
        self.assertEqual(gp.owner(), "acme")
        self.assertEqual(gp.clone_base(), str(base))
        self.assertEqual(gs.sources()["api_base"], "db")
        # 落点改了 ⇒ 克隆目标必须跟着走（containment 用生效落点，不是老常量）
        _n, dest = gp._resolve_dest("acme/tool")
        self.assertTrue(dest.startswith(str(base) + os.sep))
        # 主机改了 ⇒ 克隆 URL 与远端匹配都按新主机
        self.assertIn("https://git.example.com/acme/tool.git", gp._clone_argv("acme/tool", dest))
        self.assertEqual(gp._remote_slug("https://git.example.com/acme/tool.git"), "acme/tool")
        self.assertIsNone(gp._remote_slug("https://github.com/acme/tool"),
                          "换了主机后 github.com 的 remote 不该再匹配")

    def test_token_db_layer_wins(self):
        self.tokfile.write_text("file-tok-first-line\n", encoding="utf-8")
        os.environ["GITHUB_TOKEN"] = "env-tok-value"
        self.assertEqual(gp._read_token(), "env-tok-value")
        gs._set("token", TOK)
        self.assertEqual(gp._read_token(), TOK)

    def test_clear_falls_back(self):
        gs.apply({"api_base": "https://git.example.com/api/v3", "token": TOK})
        out = gs.clear()
        self.assertIn("api_base", out["cleared"])
        self.assertEqual(gp.api_base(), gs.DEFAULT_API_BASE)
        self.assertEqual(gs._get("token"), "")


class TestFileWriteBack(_Base):
    """回写 github.txt / .env：默认不写；要写必备份 + 值可预期。"""

    def test_default_does_not_touch_files(self):
        before = self.envfile.read_text()
        gs.apply({"api_base": "https://api.github.com", "token": TOK})
        self.assertEqual(self.envfile.read_text(), before)
        self.assertFalse(self.tokfile.exists())

    def test_write_files_backs_up_and_writes(self):
        self.tokfile.write_text("old-token-value\n", encoding="utf-8")   # 有旧值 ⇒ 必有备份
        out = gs.apply({"api_base": "https://git.example.com/api/v3",
                        "git_host": "git.example.com", "owner": "acme",
                        "clone_base": str(self.tmp), "token": TOK},
                       write_files=True)
        self.assertEqual(len(out["backups"]), 2, ".env 与 github.txt 各一份备份")
        for b in out["backups"]:
            self.assertTrue(pathlib.Path(b).is_file(), f"备份 {b} 必须落地")
        self.assertTrue(self.tokfile.read_text(encoding="utf-8").startswith(TOK))
        env = self.envfile.read_text(encoding="utf-8")
        self.assertIn('GITHUB_API_BASE="https://git.example.com/api/v3"', env)
        self.assertIn('GITHUB_HOST="git.example.com"', env)
        self.assertIn('GITHUB_OWNER="acme"', env)
        self.assertIn("HOME=/home/x", env, "无关行必须原样保留")
        self.assertNotIn("https://old.example.com", env, "旧值必须被替换而不是追加")
        # diff 里绝不能出现真 token
        self.assertNotIn(TOK, json.dumps(out, ensure_ascii=False))

    def test_env_file_created_when_missing(self):
        self.envfile.unlink()
        gs.apply({"owner": "acme"}, write_files=True)
        self.assertTrue(self.envfile.is_file())
        self.assertIn('GITHUB_OWNER="acme"', self.envfile.read_text(encoding="utf-8"))


class TestEndpointsGate(_Base):
    """2026-11-07 C 档：四个写端点不再自带 HUB_PASSCODE 校验，统一由 write_gate 登录闸门保护。"""

    def _run(self, coro):
        return asyncio.run(coro)

    def test_no_passcode_gate_in_source(self):
        src = (_REPO / "src" / "ghsettings.py").read_text(encoding="utf-8")
        self.assertNotIn("_check_passcode", src, "口令校验函数又出现了")
        self.assertNotIn("passcode", src, "入参里又混进 passcode 字段了")

    def test_request_models_have_no_passcode(self):
        for cls in (gs.TestIn, gs.GhIn):
            self.assertNotIn("passcode", cls.model_fields)

    def test_apply_ok_and_refresh_invalidates_cache(self):
        gp._cache["repos"] = [{"full_name": "a/b"}]
        gp._cache["at"] = 1e12
        with mock.patch.object(gp, "_fetch_all", lambda tok: {"ok": True, "repos": []}):
            out = self._run(gs.settings_github_apply(
                gs.GhIn(api_base="https://api.github.com")))
            self.assertTrue(out["applied"])
            self.assertIsNone(gp._cache["repos"], "改地址后旧清单必须立即失效")
            r = self._run(gs.settings_github_refresh(gs.GhIn()))
            self.assertTrue(r["ok"])

    def test_probe_success_shape(self):
        def fake(url, tok):
            if url.endswith("/user"):
                return 200, {"login": "gztxt", "html_url": "https://github.com/gztxt"}, \
                    {"x-oauth-scopes": "repo, read:user"}
            if url.endswith("/rate_limit"):
                return 200, {"resources": {"core": {"limit": 5000, "remaining": 4998,
                                                    "reset": 1700000000}}}, {}
            return 200, [{"full_name": "gztxt/x"}], {}
        with mock.patch.object(gs, "_gh_get", fake):
            d = gs.probe("https://api.github.com", TOK)
        self.assertTrue(d["ok"])
        self.assertEqual(d["login"], "gztxt")
        self.assertEqual(d["scopes"], ["repo", "read:user"])
        self.assertEqual(d["rate"]["remaining"], 4998)
        self.assertTrue(d["repo_probe"]["ok"])
        self.assertNotIn(TOK, json.dumps(d, ensure_ascii=False))


class TestFrontendWiring(unittest.TestCase):
    """静态断言：第三个子页 + 面板 + 委托出口（无 inline onclick）。"""

    def setUp(self):
        self.html = (_REPO / "templates" / "index.html").read_text(encoding="utf-8")
        self.js = (_REPO / "static" / "hub.js").read_text(encoding="utf-8")

    def test_github_settings_page(self):
        """v0.13.43：GitHub 设置从"抽屉里的第三个 tab"改成正文页 #page-settings-github。"""
        self.assertIn('id="page-settings-github"', self.html)
        self.assertNotIn('id="setPanelGithub"', self.html, "抽屉面板已拆，不该还有")
        self.assertIn("SET_PAGE_IDS", self.js, "委托清单缺设置三页")
        self.assertIn("'settings-github'", self.js, "go() 懒加载缺 GitHub 设置页")
        for dom in ("ghApiBase", "ghGitHost", "ghOwner", "ghToken", "ghCloneBase",
                    "ghWriteFiles", "ghStatus", "ghDiff", "ghTokenState"):
            self.assertIn(f'id="{dom}"', self.html, f"#{dom} 缺失 ⇒ JS 取值 null")

    def test_actions_go_through_delegate(self):
        for act in ("gh-test", "gh-apply", "gh-clear", "gh-refresh"):
            self.assertIn(f'data-settings-act="{act}"', self.html, f"{act} 按钮缺失")
        for fn in ("settingsGithubLoad", "settingsGithubTest", "settingsGithubApply",
                   "settingsGithubClear", "settingsGithubRefresh", "settingsGithubStatus"):
            self.assertIn(f"function {fn}(", self.js, f"{fn} 缺失 ⇒ 点了没反应")
        i = self.js.find("function settingsDelegates(")
        body = self.js[i:i + 1400]
        for act in ("gh-test", "gh-apply", "gh-clear", "gh-refresh"):
            self.assertIn(act, body, f"委托没接管 {act}")

    def test_password_input_for_key(self):
        self.assertIn('id="ghToken" type="password"', self.html,
                      "key 输入框必须是 password（肩窥与截屏会泄）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
