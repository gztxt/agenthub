"""能力矩阵闸门（v0.13.156）。

用户口径（2026-10-25 第三次报同一件事）：「新增 agent 后没有原生界面按钮，设置项也不同步，
每次都是发现问题再修」。机制上就一条：**能力声明可以静默缺失**。这个测试把它变成红灯。

判据分五组：

1. **静态画像必须表态**：`PROFILES` 里每个 kind=agent 的条目，`ui_state` 只能是
   `ok` 或 `tui_only`。出现 `missing_recipe` / `probe_failed` 即红 —— 那意味着
   「加了一张卡，但没决定它有没有原生界面」。
2. **ok 必须真能拉起**：有 `ui`、有 `start`（常驻豁免除外）、`url` 带 http、
   `recipe.port` 与 url 端口一致。
3. **按钮必须出得来**：`entries_for(p, "stopped")` 含 embed/open，且都带 `start`
   —— 「停了才是按需常态」，所以按 stopped 测，不按 running 测。
4. **反向**：有 `ui` 但没 recipe ⇒ 红（这正是 v0.13.152 之前「点开没处拉起」的形态）。
5. **扫描路径不许把「探测失败」当「没有界面」**：`_caps_for` 对不存在的命令必须是
   `probe_failed`；`registered_profiles` 必须把 caps 透传成 ui_state/ui_note。
"""
import sys
import unittest

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] + "/src")

import agentscan      # noqa: E402
import capabilities   # noqa: E402
import profiles       # noqa: E402


def _static_agents():
    """只取手写画像（不含 CLI_ALIASES 动态卡与 DB 注册项）。"""
    return [p for p in profiles.PROFILES if p.get("kind") == "agent"]


class TestStaticProfilesDeclareCapability(unittest.TestCase):
    def test_every_static_agent_is_ok_or_tui_only(self):
        bad = []
        for p in _static_agents():
            state, note = capabilities.state_of(p)
            if state not in ("ok", "tui_only"):
                bad.append((p["id"], state, note))
        self.assertFalse(bad, "静态画像未决定「有没有原生界面」（加卡时必须二选一）：%s" % bad)

    def test_tui_only_set_only_contains_known_agents(self):
        """声明是按 id 做的，静态卡与 CLI_ALIASES 动态卡共用同一份声明。"""
        ids = {p["id"] for p in _static_agents()} | set(profiles.CLI_ALIASES)
        stale = capabilities.UI_TUI_ONLY - ids
        self.assertFalse(stale, "UI_TUI_ONLY 里有已不存在的 agent（删卡后没清声明）：%s" % stale)

    def test_ui_implies_recipe(self):
        """有 ui 却没配方 = 「有按钮、没处拉起」的旧形态。"""
        for p in _static_agents():
            if p.get("ui"):
                self.assertIsNotNone(
                    capabilities.recipe_for(p["id"]),
                    f"{p['id']} 有 ui 但没有 UI_RECIPES 条目（点开没有拉起的地方）")

    def test_recipe_with_url_is_not_naked(self):
        for pid, r in capabilities.UI_RECIPES.items():
            if r.get("url"):
                self.assertTrue(str(r["url"]).startswith("http"),
                                f"{pid} 的 url 不是 http(s)：{r.get('url')}")
                if r.get("port"):
                    self.assertIn(f":{r['port']}", str(r["url"]),
                                  f"{pid} 的 port 与 url 不一致")
            if not r.get("resident"):
                self.assertTrue(r.get("start"),
                                f"{pid} 不是常驻项，却没有 start（点了起不来）")


class TestEntriesCarryStart(unittest.TestCase):
    def test_ok_agents_emit_embed_and_open_with_start(self):
        """string-ui 的入口由 entries_for 直接给；dict-ui（cloudcli）由 discovery 注入，
        这里只校验前者 —— 后者另有 discovery 侧用例，避免同一判据两处维护。"""
        for p in _static_agents():
            if not isinstance(p.get("ui"), str):
                continue
            resident = bool((capabilities.recipe_for(p["id"]) or {}).get("resident"))
            es = profiles.entries_for(profiles._enrich(p), "stopped")
            kinds = [e["type"] for e in es]
            self.assertIn("open", kinds, f"{p['id']} stopped 时没有「新窗口」入口")
            self.assertIn("embed", kinds, f"{p['id']} stopped 时没有「嵌入会话」入口")
            for e in es:
                if e["type"] not in ("embed", "open"):
                    continue
                if resident:
                    continue
                self.assertIn("start", e, f"{p['id']} 的 {e['type']} 入口没有拉起方式")

    def test_gap_agents_carry_visible_note(self):
        """真缺口（缺配方/探测失败）必须把原因写在 desc 上 —— 不许静默少一个按钮。

        `tui_only` 不写 desc：终端就是它的原生界面，给每个 TUI agent 挂一句
        「没有 Web 界面」会把真正的缺口淹掉。
        """
        for p in _static_agents():
            q = capabilities.annotate(dict(p))
            self.assertTrue(q.get("ui_note"), f"{p['id']} 缺能力结论说明")
            if q["ui_state"] in ("missing_recipe", "probe_failed"):
                self.assertIn("原生界面", str(q.get("desc") or ""),
                              f"{p['id']} 的 desc 没写出缺口")


class TestScanPathDoesNotLie(unittest.TestCase):
    def test_probe_failure_is_not_tui_only(self):
        caps = agentscan._caps_for("/nonexistent/definitely-not-here")
        self.assertEqual(caps["ui_state"], "probe_failed", caps)
        self.assertTrue(caps["ui_note"])

    def test_registered_profiles_pass_caps_through(self):
        class _DB:
            def query(self, sql, *a):
                return [{"id": "zzz", "name": "ZZZ", "command": "/bin/true",
                         "description": "", "working_dir": "/tmp",
                         "caps": '{"ui_state": "missing_recipe", "ui_note": "有 --port"}'}]

        rows = agentscan.registered_profiles(_DB())
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["ui_state"], "missing_recipe")
        self.assertIn("--port", rows[0]["ui_note"])

    def test_caps_null_stays_undecided(self):
        """caps 为 NULL 时不许假装 tui_only —— 必须留「未声明」，等回填。"""
        class _DB:
            def query(self, sql, *a):
                return [{"id": "yyy", "name": "YYY", "command": "/bin/true",
                         "description": "", "working_dir": "/tmp", "caps": None}]

        p = agentscan.registered_profiles(_DB())[0]
        p = capabilities.annotate(dict(p))
        self.assertEqual(p["ui_state"], "missing_recipe")
        self.assertIn("未声明", p["ui_note"])


class TestTokenUrl(unittest.TestCase):
    """进程级 token 的地址拼装（reasonix `auth: token`）。"""

    def _with_log(self, text):
        import pathlib
        import tempfile
        from config import config
        td = tempfile.mkdtemp()
        d = pathlib.Path(td) / "ui-start"
        d.mkdir()
        (d / "reasonix.log").write_text(text, encoding="utf-8")
        old = config.log_dir
        config.log_dir = pathlib.Path(td)
        capabilities._URL_CACHE.clear()
        return old, pathlib.Path(td)

    def test_token_is_appended_from_log(self):
        old, _ = self._with_log("share: http://[::]:8787/#token=AbCdEf123456\n")
        from config import config
        try:
            u = capabilities.url_for("reasonix")
        finally:
            config.log_dir = old
            capabilities._URL_CACHE.clear()
        self.assertEqual(u, "http://127.0.0.1:8787#token=AbCdEf123456")

    def test_missing_log_degrades_to_bare_url(self):
        old, _ = self._with_log("no share line here\n")
        from config import config
        try:
            u = capabilities.url_for("reasonix")
        finally:
            config.log_dir = old
            capabilities._URL_CACHE.clear()
        self.assertEqual(u, "http://127.0.0.1:8787",
                         "取不到 token 时必须降级成裸地址（打开 401），不许编一个")


class TestSpawnEnv(unittest.TestCase):
    """拉起原生界面时的 PATH 口径（2026-10-25 线上 reasonix binary_not_found 回归）。"""

    def test_nvm_bins_are_prepended(self):
        import os
        import api_agents
        import profiles
        env = api_agents._ui_spawn_env()
        extra = profiles.extra_path_dirs()
        self.assertTrue(extra, "本机应能解析出 nvm/bun 目录；解析不到说明 extra_path_dirs 坏了")
        parts = env["PATH"].split(os.pathsep)
        self.assertEqual(parts[:len(extra)], extra,
                         "extra_path_dirs 必须**前置**：这些 CLI 的 shebang 是 "
                         "#!/usr/bin/env node，node 不在 PATH 就会拿系统 node 跑 Node22+ 包")

    def test_dev_and_user_bins_present(self):
        import os
        from pathlib import Path
        import api_agents
        parts = api_agents._ui_spawn_env()["PATH"].split(os.pathsep)
        for d in (str(Path.home() / ".npm-global" / "bin"),
                  str(Path.home() / ".local" / "bin"), "/usr/local/bin"):
            self.assertIn(d, parts, f"拉起原生界面的 PATH 缺 {d}")


if __name__ == "__main__":
    unittest.main()
