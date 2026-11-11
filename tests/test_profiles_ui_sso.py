"""所有原生界面的登录统一到 hub（2026-11-11 用户裁定「把这套规则复用到所有 agent」）。

报障原型（pi）：浏览器直连上游会**连着弹两个登录面** —— 原生 Basic 弹窗
（/api 的 401 + WWW-Authenticate）+ 应用自己的 /login 密码框；两者都不认 hub
账户。收口方式：所有浏览器入口（嵌入 + 新窗口）一律经 hub 桥，桥只认 hub_session。

不变量：
① 每个桥目标都声明 browser_entry ⇒ hub_auth 打开时桥放到 EMBED_PROXY_HOST，
   否则只绑回环的上游会把桥锁死在 127.0.0.1，前端换主机后恒 ERR_CONNECTION_REFUSED；
② 所有 agent 的 string ui：embed / open 都走桥；dict ui（cloudcli）同样两个入口都走桥；
③ 所有非 agent 的 panel：open 也走桥（有桥的 id），且 query 里的 token 不能丢；
④ hub_auth=0（旧排障形态）时不得自动放开监听面 —— 桥自己只认共享口令。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import capabilities  # noqa: E402
import discovery  # noqa: E402
import embed_proxy  # noqa: E402
import profiles  # noqa: E402


class AllUISingleLogin(unittest.TestCase):
    """把 EMBED_PROXY_MAP 灌成「所有桥都已启动」的形态，逐个画像验入口地址。"""

    def setUp(self):
        self._orig = dict(profiles.EMBED_PROXY_MAP)
        profiles.EMBED_PROXY_MAP = {
            t["id"]: "http://127.0.0.1:%d" % (3200 + i)
            for i, t in enumerate(profiles.embed_proxy_targets())
        }

    def tearDown(self):
        profiles.EMBED_PROXY_MAP = self._orig

    def test_every_bridge_target_is_a_browser_entry(self):
        targets = profiles.embed_proxy_targets()
        self.assertTrue(targets)
        for t in targets:
            self.assertTrue(t["browser_entry"], "%s 未声明 browser_entry" % t["id"])

    def test_all_agent_ui_entrances_go_through_bridge(self):
        bridged = set(profiles.EMBED_PROXY_MAP)
        for p in profiles.PROFILES:
            if p.get("kind") != "agent" or not isinstance(p.get("ui"), str):
                continue
            es = {e["type"]: e for e in profiles.entries_for(p, "running")}
            if p["id"] not in bridged:
                # qwenpaw：ui 本身就是 hub 的 hub_auth 注入代理 :3103，语义等价，不另造桥。
                self.assertIn(":3103", es["open"]["url"])
                continue
            expected = profiles.EMBED_PROXY_MAP[p["id"]] + "/"
            for typ in ("embed", "open"):
                self.assertEqual(es[typ]["url"], expected,
                                 "%s 的 %s 入口绕过了 hub 桥" % (p["id"], typ))

    def test_dict_ui_both_entrances_go_through_bridge(self):
        for p in profiles.PROFILES:
            ui = p.get("ui")
            if not isinstance(ui, dict) or p["id"] not in profiles.EMBED_PROXY_MAP:
                continue
            es = {e["type"]: e for e in discovery.host_ui_entries(p, ui)}
            self.assertEqual(set(es), {"embed", "open"})
            for typ in ("embed", "open"):
                self.assertEqual(es[typ]["url"], profiles.EMBED_PROXY_MAP[p["id"]] + "/",
                                 "%s 的 %s 入口绕过了 hub 桥" % (p["id"], typ))

    def test_panel_open_also_goes_through_bridge_and_keeps_query(self):
        for p in profiles.PROFILES:
            panel = p.get("panel")
            if not panel or p["id"] not in profiles.EMBED_PROXY_MAP:
                continue
            es = {e["type"]: e for e in profiles.entries_for(p, "running")}
            self.assertIn(profiles.EMBED_PROXY_MAP[p["id"]], es["open"]["url"],
                          "%s 的面板新窗口绕过了 hub 桥" % p["id"])
            if "?" in panel:
                self.assertIn(panel.split("?", 1)[1], es["open"]["url"],
                              "%s 的 token 在过桥时丢了" % p["id"])

    def test_bridge_listen_wide_only_when_hub_auth_on(self):
        t = {"host": "127.0.0.1", "port": 59999, "start": None,
             "browser_entry": True}
        wide = embed_proxy.listen_host_for_browser(t, "0.0.0.0", hub_auth=True)
        self.assertEqual(wide, "0.0.0.0")
        narrow = embed_proxy.listen_host_for_browser(t, "0.0.0.0", hub_auth=False)
        self.assertEqual(narrow, "127.0.0.1")
        no_flag = embed_proxy.listen_host_for_browser(
            {"host": "127.0.0.1", "port": 59999, "start": None, "browser_entry": False},
            "0.0.0.0", hub_auth=True)
        self.assertEqual(no_flag, "127.0.0.1")

    def test_basic_panel_gotty_is_bridged_with_its_own_user(self):
        t = [x for x in profiles.embed_proxy_targets() if x["id"] == "gotty"]
        self.assertEqual(len(t), 1, "gotty 没进桥 ⇒ 新窗口仍会弹原生 Basic 登录框")
        self.assertEqual(t[0]["basic_user"], "gztxt")
        self.assertTrue(os.path.exists(str(t[0]["secret"])))

    def test_codebuddy_upstream_login_is_preauthenticated_by_bridge(self):
        t = [x for x in profiles.embed_proxy_targets() if x["id"] == "codebuddy"]
        self.assertEqual(len(t), 1)
        f = t[0]["upstream_cookie_file"]
        self.assertTrue(f and os.path.exists(f),
                        "codebuddy 的 gateway_session Cookie 文件缺失 ⇒ 又会弹 Login 页")
        self.assertIn("gateway_session=", open(f, encoding="utf-8").read())

    def test_chromium_is_a_documented_exception(self):
        ids = {t["id"] for t in profiles.embed_proxy_targets()}
        self.assertNotIn("chromium", ids)
        p = [x for x in profiles.PROFILES if x["id"] == "chromium"][0]
        self.assertIn("暂未接入", p["desc"], "例外必须写在卡面上，不能静默漏掉")

    def test_pi_agent_has_no_own_password_and_binds_loopback(self):
        pi = [p for p in profiles.PROFILES if p["id"] == "pi"][0]
        self.assertIn("127.0.0.1", pi["ui"])
        self.assertEqual(capabilities.start_for("pi").get("bind"), "127.0.0.1")
        dropin = "/home/gztxt/.config/systemd/user/pi-web.service.d/70-password.conf"
        if os.path.exists(dropin):
            lines = [l for l in open(dropin, encoding="utf-8").read().splitlines()
                     if l.startswith("EnvironmentFile=") and not l.lstrip().startswith("#")]
            self.assertEqual(lines, [], "pi 又开始加载自己的口令文件了")


if __name__ == "__main__":
    unittest.main()
