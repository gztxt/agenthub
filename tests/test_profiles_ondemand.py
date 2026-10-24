"""全 agent 按需拉起（2026-10-24 用户令「所有 agent 参考 omp 不长驻 按需拉起」）

画像层不变量：
① cloudcli/pi/dsh/qwenpaw/qoder 五卡 desc 必须带启动命令（零代码按需入口）；
   codebuddy 卡必须带豁免注记（workbuddy 两单元用户明示保留常驻）；
② agent 的 string ui 的 embed/open **不带 probe_port**、stopped 时仍在 ——
   v0.13.151 改判：按需拉起下 stopped 是常态，v0.13.149「服务停了按钮要消失」
   （probe_port 扩展到 agent ui）与同日的按需政策直接冲突，把每个 Web UI
   agent 的原生界面按钮在正常状态下删掉（2026-10-25 用户报障「dsh的原生界面
   按钮也没有了」）。probe_port 只保留给 panel 类入口（services 的面板）；
③ _entry_alive：loopback 双栈之外，URL 端口==probe_port 时按 URL host 补探
   （面板入口活性判据，机制本身不变）；
④ 终端入口不受影响：pi/dsh 停了仍给出 term 入口（按需形态的本体）；
⑤ qoder 静态卡必须压掉 B 档动态卡（同 id ⇒ covered），否则一张 agent 两张卡。
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
        """按需卡的 desc 必须自带启动命令（用户裁定零代码入口）。"""
        for pid in ("cloudcli", "pi", "dsh", "qwenpaw", "qoder"):
            desc = profiles.get_profile(pid).get("desc", "")
            self.assertIn("systemctl --user start", desc,
                          f"{pid} 卡 desc 必须写启动命令（零代码按需入口）")
            self.assertIn("不长驻", desc,
                          f"{pid} 卡 desc 必须声明不长驻口径")

    def test_codebuddy_exemption_note(self):
        """codebuddy 卡必须带豁免注记（workbuddy 两单元 2026-10-24 用户裁定保留）。"""
        desc = profiles.get_profile("codebuddy").get("desc", "")
        self.assertIn("豁免", desc, "codebuddy 卡 desc 必须注明 workbuddy 豁免")

    def test_agent_ui_entries_survive_stopped(self):
        """agent 的 embed/open 停了也必须在，且**不得**带 probe_port。

        v0.13.151 改判（2026-10-25 用户报障「dsh的原生界面按钮也没有了」）：
        v0.13.149 给 agent string-ui 入口带 probe_port 并由 discovery 按端口
        活性过滤 ⇒ 按需拉起政策下 dsh/pi 等常态 stopped，原生界面按钮被删。
        反证：破坏方向有两个——① 给入口加回 probe_port（过滤会删按钮）；
        ② entries_for 按 status 跳过 embed（status='stopped' 时断言变红）。
        停着的服务由前端 probeEmbed 出「目标界面未响应」覆盖层，不关按钮的事。
        """
        for pid in ("pi", "dsh", "qwenpaw", "codebuddy", "qoder"):
            p = profiles.get_profile(pid)
            for st in ("running", "stopped"):
                es = profiles.entries_for(p, st)
                types = [e["type"] for e in es]
                for t in ("embed", "open"):
                    self.assertIn(t, types,
                                  f"{pid} status={st} 时必须给出 {t} 入口"
                                  f"（按需拉起下 stopped 是常态，按钮不能消失）")
                for e in es:
                    if e["type"] in ("embed", "open"):
                        self.assertNotIn("probe_port", e,
                                         f"{pid} 的 agent ui 入口不得带 probe_port"
                                         f"（v0.13.151 起只 panel 类入口才按端口过滤）")

    def test_panel_entries_still_carry_probe_port(self):
        """panel 类入口（services 面板）仍带 probe_port —— 「死面板不出按钮」
        doctrine 只在这一层保留，与 agent 按需政策无关。"""
        p = {"id": "svc", "name": "S", "kind": "service",
             "panel": "http://127.0.0.1:18083", "terminal": None}
        es = profiles.entries_for(p, "running")
        for e in es:
            if e["type"] in ("embed", "open"):
                self.assertEqual(e.get("probe_port"), 18083,
                                 "panel 入口必须带 probe_port（死面板过滤的判据）")

    def test_qoder_static_card_suppresses_dynamic(self):
        """qoder 静态卡（id=qoder）必须让 B 档动态发现不出第二张 qoder 卡。

        动态卡由 CLI watchlist 生成（qodercli 已安装 ⇒ 不压就会出「Qoder CLI」
        终端卡 + 「QoderWake」静态卡两张）。covered 集合含 PROFILES 的 id/cli。
        """
        dyn_ids = {c["id"] for c in profiles._dynamic_cli_agents(gate=False)}
        self.assertNotIn("qoder", dyn_ids,
                         "静态卡已收录 qoder，动态发现不得再出一张同 id 卡")
        self.assertIn("qoder", {p["id"] for p in profiles.PROFILES},
                      "qoder 静态卡必须存在（QoderWake daemon 的登记入口）")

    def test_term_entries_survive_stopped(self):
        """停了也要给终端入口——按需形态的本体就在这。"""
        for pid in ("pi", "dsh"):
            p = profiles.get_profile(pid)
            types = [e["type"] for e in profiles.entries_for(p, "stopped")]
            self.assertIn("term", types, f"{pid}  stopped 时仍必须给出 term 入口")


class UiStartTest(unittest.TestCase):
    """原生界面拉起（v0.13.152）：按钮之外必须有「怎么起」。

    2026-10-25 用户报障「要使用原生界面 都没有拉起的地方」——按需拉起下停着是常态，
    只有按钮没有启动动作 ⇒ 用户只能自己去终端敲 systemctl。
    """

    def test_every_web_ui_agent_declares_start(self):
        """登记了 ui 的 agent 必须能拉起原生界面。

        cloudcli 的 ui 是 dict（独立宿主），入口由 discovery 注入 ⇒ 这里只钉
        UI_START 存在；其余 string-ui 的 agent 在 entries_for 层直接断言带 start。
        """
        for pid in ("cloudcli", "pi", "dsh", "qwenpaw", "qoder", "opencode"):
            self.assertIsNotNone(profiles.ui_start_for(pid),
                                 f"{pid} 有 Web UI 但未登记 UI_START（没有拉起的地方）")
        for pid in ("pi", "dsh", "qwenpaw", "qoder", "opencode"):
            p = profiles.get_profile(pid)
            for st in ("running", "stopped"):
                es = profiles.entries_for(p, st)
                ui_es = [e for e in es if e["type"] in ("embed", "open")]
                self.assertTrue(ui_es, f"{pid} 必须有原生界面入口")
                for e in ui_es:
                    self.assertIn("start", e,
                                  f"{pid} status={st} 的 {e['type']} 入口必须带 start")

    def test_tui_only_agents_have_no_start(self):
        """TUI-only agent 不得登记启动器——终端就是它们的原生界面。"""
        for pid in ("claude", "omp", "jcode", "codex", "hermes", "grok", "cursor"):
            self.assertIsNone(profiles.ui_start_for(pid),
                              f"{pid} 没有 Web UI，不该有 UI_START")

    def test_opencode_static_card_has_ui(self):
        """opencode 上游自带 web 界面 ⇒ 必须有原生界面入口，且压掉动态终端卡。"""
        p = profiles.get_profile("opencode")
        self.assertEqual(p.get("ui"), "http://127.0.0.1:4096")
        self.assertEqual(p.get("port"), 4096)
        types = [e["type"] for e in profiles.entries_for(p, "stopped")]
        self.assertIn("embed", types)
        self.assertIn("open", types)
        self.assertNotIn("opencode", {c["id"] for c in profiles._dynamic_cli_agents(gate=False)},
                         "静态卡已收录 opencode，动态发现不得再出一张同 id 卡")

    def test_qwenpaw_health_url_points_to_real_backend(self):
        """qwenpaw 的 ui 是注入代理 :3103 ⇒ 入口必须带 health_url 指向真后端 :8088。"""
        p = profiles.get_profile("qwenpaw")
        for e in profiles.entries_for(p, "stopped"):
            if e["type"] == "embed":
                self.assertEqual(e.get("health_url"), "http://127.0.0.1:8088",
                                 "探活必须打真实后端，代理常驻会把停着的服务显示成活的")

    def test_ui_start_targets_are_server_side_constants(self):
        """start 描述里只有 unit/cmd 两种形态，单元名非空——不接受请求体拼命令。"""
        for pid, spec in profiles.UI_START.items():
            self.assertTrue(spec.get("unit") or spec.get("cmd"),
                            f"{pid} 的 UI_START 必须给 unit 或 cmd")
            if spec.get("cmd"):
                self.assertIsInstance(spec["cmd"], list,
                                      f"{pid} 的 cmd 必须是 argv 数组（不经过 shell）")


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
