"""pi 的登录统一到 hub（2026-11-11 用户裁定「不要搞两套登录」）。

报障形态：浏览器打开 pi 会**连着弹两个登录面** —— 先是原生 Basic 弹窗
（用户名+密码，pi 的 /api 回 401 + WWW-Authenticate 触发），再是 /login 的
纯密码框。两者都不是 hub 的登录，且输哪个都不通。

不变量：
① pi 画像声明 bridge_public ⇒ 桥监听面显式对外（否则锁回环，手机/局域网
   从桥进不来，只能回落到 pi 自己的登录页）；
② 「嵌入会话」与「新窗口」两个浏览器入口都必须指向 hub 桥，不得再给 :30141
   直连（直连 = pi 自带的那两套登录）；
③ capabilities 的起法声明 bind 与 unit 实际绑定一致（127.0.0.1）。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import capabilities  # noqa: E402
import profiles  # noqa: E402


class PiSSOTest(unittest.TestCase):
    def test_pi_bridge_is_public(self):
        pi = [t for t in profiles.embed_proxy_targets() if t["id"] == "pi"]
        self.assertEqual(len(pi), 1)
        self.assertTrue(pi[0]["bridge_public"],
                        "pi 的桥必须显式对外，否则 LAN/手机打开恒 ERR_CONNECTION_REFUSED")

    def test_both_browser_entries_go_through_bridge(self):
        p = [x for x in profiles.PROFILES if x["id"] == "pi"][0]
        old = profiles.EMBED_PROXY_MAP.get("pi")
        profiles.EMBED_PROXY_MAP["pi"] = "http://127.0.0.1:3201"
        try:
            es = {e["type"]: e for e in profiles.entries_for(p, "running")}
        finally:
            if old is None:
                profiles.EMBED_PROXY_MAP.pop("pi", None)
            else:
                profiles.EMBED_PROXY_MAP["pi"] = old
        for t in ("embed", "open"):
            self.assertEqual(es[t]["url"], "http://127.0.0.1:3201/",
                             "%s 入口必须走 hub 桥，直连 30141 会弹 pi 自己的两套登录" % t)

    def test_pi_start_recipe_binds_loopback(self):
        r = capabilities.start_for("pi")
        self.assertIsNotNone(r)
        self.assertEqual(r.get("bind"), "127.0.0.1",
                         "pi-web 已只绑回环，声明必须同步，否则桥会被判成对外扩面")


if __name__ == "__main__":
    unittest.main()
