"""全 agent 按需拉起（2026-10-24 用户令「所有 agent 参考 omp 不长驻 按需拉起」）

画像层不变量：
① cloudcli/pi/dsh/qwenpaw 四卡 desc 必须带启动命令（零代码按需入口）；
   codebuddy 卡必须带豁免注记（workbuddy 两单元用户明示保留常驻）；
② agent 的 string ui 的 embed/open 入口必须带 probe_port == 画像 port——
   服务停了按钮要消失（「死面板不出按钮」doctrine 从 panel 类扩展到 agent ui）；
③ _entry_alive：loopback 双栈之外，URL 端口==probe_port 时按 URL host 补探
   （dsh 只绑 192.168.5.102）；URL 端口≠probe_port（qwenpaw 注入代理）不补探；
④ 终端入口不受影响：pi/dsh 停了仍给出 term 入口（按需形态的本体）。
"""
import os
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import discovery  # noqa: E402
import profiles  # noqa: E402
import vitals  # noqa: E402


class OnDemandProfilesTest(unittest.TestCase):
    def test_ondemand_cards_document_start_command(self):
        """四张按需卡的 desc 必须自带启动命令（用户裁定零代码入口）。"""
        for pid in ("cloudcli", "pi", "dsh", "qwenpaw"):
            desc = profiles.get_profile(pid).get("desc", "")
            self.assertIn("systemctl --user start", desc,
                          f"{pid} 卡 desc 必须写启动命令（零代码按需入口）")
            self.assertIn("不长驻", desc,
                          f"{pid} 卡 desc 必须声明不长驻口径")

    def test_codebuddy_exemption_note(self):
        """codebuddy 卡必须带豁免注记（workbuddy 两单元 2026-10-24 用户裁定保留）。"""
        desc = profiles.get_profile("codebuddy").get("desc", "")
        self.assertIn("豁免", desc, "codebuddy 卡 desc 必须注明 workbuddy 豁免")

    def test_string_ui_entries_carry_probe_port(self):
        """string ui 的 embed/open 必须带 probe_port == 画像 port。"""
        for pid in ("pi", "dsh", "qwenpaw", "codebuddy"):
            p = profiles.get_profile(pid)
            es = [e for e in profiles.entries_for(p, "running")
                  if e["type"] in ("embed", "open")]
            self.assertTrue(es, f"{pid} 应给出 embed/open 入口")
            for e in es:
                self.assertEqual(e.get("probe_port"), p.get("port"),
                                 f"{pid} 的 {e['type']} 入口 probe_port 必须等于画像 port"
                                 f"（真实服务端口，不是注入代理端口）")

    def test_term_entries_survive_stopped(self):
        """停了也要给终端入口——按需形态的本体就在这。"""
        for pid in ("pi", "dsh"):
            p = profiles.get_profile(pid)
            types = [e["type"] for e in profiles.entries_for(p, "stopped")]
            self.assertIn("term", types, f"{pid}  stopped 时仍必须给出 term 入口")


class EntryAliveTest(unittest.TestCase):
    def _d(self):
        return discovery.AgentDiscovery(config=None)

    def test_loopback_alive(self):
        d = self._d()
        with mock.patch.object(vitals, "port_open",
                               side_effect=lambda port, host="127.0.0.1": host == "127.0.0.1"):
            self.assertTrue(d._entry_alive({"probe_port": 30141,
                                            "url": "http://127.0.0.1:30141"}))

    def test_dsh_lan_host_probe(self):
        """dsh 只绑 192.168.5.102：loopback/::1 全拒，URL host 补探必须救命。"""
        d = self._d()
        with mock.patch.object(vitals, "port_open",
                               side_effect=lambda port, host="127.0.0.1": host == "192.168.5.102"):
            self.assertTrue(d._entry_alive(
                {"probe_port": 3080, "url": "http://192.168.5.102:3080?token=abc"}))

    def test_dead_when_all_probes_fail(self):
        d = self._d()
        with mock.patch.object(vitals, "port_open", return_value=False):
            self.assertFalse(d._entry_alive(
                {"probe_port": 3080, "url": "http://192.168.5.102:3080?token=abc"}))

    def test_qwenpaw_proxy_port_not_probed(self):
        """qwenpaw：ui 是注入代理 :3103 ≠ probe_port :8088 ⇒ 不补探；
        代理活着但后端停了，必须判死（否则停着的服务显示成活的）。

        ⚠ 2026-10-24 红向自证补强：本断言最初只有这一条回环 URL 用例，破坏
        「端口相等」条件后它**仍然全绿**——回环排除先把补探挡了，端口相等条件
        没被检验到（判据恒假）。一般形式见下一条（非回环代理 host）。
        """
        d = self._d()
        with mock.patch.object(vitals, "port_open",
                               side_effect=lambda port, host="127.0.0.1": port == 3103):
            self.assertFalse(d._entry_alive(
                {"probe_port": 8088, "url": "http://127.0.0.1:3103"}))

    def test_proxy_with_mismatched_port_not_probed(self):
        """端口相等条件的一般形式：URL 指向非回环 host 但端口≠probe_port
        （LAN 上的代理）⇒ 不得拿 probe_port 去那个 host 探——那不是服务本身。

        构造：LAN host 上 8088 开着（假探针），但 URL 是 :3103 的代理地址。
        有端口相等条件 ⇒ 不补探 ⇒ 判死；去掉该条件 ⇒ 补探命中 ⇒ 误判活。
        """
        d = self._d()
        with mock.patch.object(
                vitals, "port_open",
                side_effect=lambda port, host="127.0.0.1": host == "192.168.5.102" and port == 8088):
            self.assertFalse(d._entry_alive(
                {"probe_port": 8088, "url": "http://192.168.5.102:3103"}))

    def test_loopback_url_host_not_reprobed(self):
        """URL host 是回环时不补探（双栈已经探过），行为不退化。"""
        d = self._d()
        calls = []

        def fake(port, host="127.0.0.1"):
            calls.append(host)
            return False
        with mock.patch.object(vitals, "port_open", side_effect=fake):
            self.assertFalse(d._entry_alive(
                {"probe_port": 30141, "url": "http://127.0.0.1:30141"}))
        self.assertEqual(calls, ["127.0.0.1", "::1"],
                         "回环 URL 不得触发第三次补探")

    def test_no_url_entry(self):
        d = self._d()
        with mock.patch.object(vitals, "port_open", return_value=False):
            self.assertFalse(d._entry_alive({"probe_port": 30141}))


if __name__ == "__main__":
    unittest.main()
