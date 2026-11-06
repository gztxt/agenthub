"""Proxy Panel 单一入口（2026-11-07 用户裁定：两个面板融合）。

判据三件：① xray / openrung 不再单独出卡，也不再有 parent 子菜单；
② proxy-panel 指 :8083，带 bridge_secret（Basic 面板缺它就不出 embed，
侧栏点进去只有新窗口）；③ embed_proxy_targets 只含 proxy-panel。
"""
import os
import sys
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))

import profiles  # noqa: E402


class ProxyPanelTest(unittest.TestCase):
    def setUp(self):
        self.profiles = profiles.all_profiles()
        self.by_id = {p["id"]: p for p in self.profiles}

    def test_no_standalone_proxy_cards(self):
        self.assertNotIn("xray", self.by_id)
        self.assertNotIn("openrung", self.by_id)

    def test_no_children_left_in_nav(self):
        kids = [p["id"] for p in self.profiles if p.get("parent")]
        self.assertEqual(kids, [], "代理子菜单已删除，不应再有 parent 声明")

    def test_panel_points_at_8083_and_bridges_into_embed(self):
        p = self.by_id["proxy-panel"]
        self.assertEqual(profiles.panel_port(p), 8083)
        self.assertEqual(p.get("panel_auth"), "basic")
        self.assertTrue(p.get("bridge_secret"), "缺 bridge_secret ⇒ 不会出 embed")
        types = [e["type"] for e in profiles.entries_for(p, "running")]
        self.assertIn("embed", types)
        self.assertIn("open", types)

    def test_embed_targets_only_merged_panel(self):
        ids = {t["id"] for t in profiles.embed_proxy_targets()}
        self.assertIn("proxy-panel", ids)
        self.assertNotIn("xray", ids)
        self.assertNotIn("openrung", ids)


if __name__ == "__main__":
    unittest.main()
