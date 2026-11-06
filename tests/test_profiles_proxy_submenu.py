"""侧栏代理子菜单（2026-11-07 用户要求）。

判据三件：① xray / openrung 都声明 parent=proxy-panel；② 各自 panel 指到真实端口
（xray→8083、openrung→18082），port 不是面板口；③ 两者都声明 bridge_secret ——
Basic 面板若缺这个字段，entries_for 不会出 embed，侧栏点进去只有新窗口，
「在基础设施里直接打开」就落空。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import profiles  # noqa: E402


class ProxySubmenuTest(unittest.TestCase):
    def setUp(self):
        self.by_id = {p["id"]: p for p in profiles.all_profiles()}

    def test_children_point_at_parent(self):
        for cid in ("xray", "openrung"):
            self.assertIn(cid, self.by_id)
            self.assertEqual(self.by_id[cid].get("parent"), "proxy-panel")

    def test_panels_are_the_real_ports(self):
        self.assertEqual(profiles.panel_port(self.by_id["xray"]), 8083)
        self.assertEqual(profiles.panel_port(self.by_id["openrung"]), 18082)
        # port 是服务端口不是面板端口，二者混用会让 nav 的 :port 标签说谎
        self.assertEqual(self.by_id["openrung"]["port"], 18081)

    def test_basic_panels_bridge_into_embed(self):
        for cid in ("xray", "openrung"):
            p = self.by_id[cid]
            self.assertEqual(p.get("panel_auth"), "basic")
            self.assertTrue(p.get("bridge_secret"), cid + " 缺 bridge_secret ⇒ 不会出 embed")
            types = [e["type"] for e in profiles.entries_for(p, "running")]
            self.assertIn("embed", types)
            self.assertIn("open", types)

    def test_bridge_targets_cover_both(self):
        ids = {t["id"] for t in profiles.embed_proxy_targets()}
        self.assertIn("xray", ids)
        self.assertIn("openrung", ids)


if __name__ == "__main__":
    unittest.main()
