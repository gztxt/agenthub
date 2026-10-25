"""原生界面判活（/api/agents/{id}/ui-status）闸门 —— 2026-10-25 dsh 报障。

判据（每条都对应一个真实故障形态，不是为覆盖而覆盖）：

1. **绑定地址必须被探**：dsh 拒绝通配地址，只监听 `192.168.5.102:3080`，回环永远拒连。
   旧实现只探 `127.0.0.1`/`::1` ⇒ `/ui-status` 恒 `ready=false`，前端覆盖层一直盖着
   「原生界面未运行，正在拉起…」，用户看到的是「原生界面起不来」（服务其实 active）。
2. **`probe` 优先于 `url`**：qwenpaw 的 `url` 是 hub 进程内代理 :3103（hub 活着它就
   listening），拿它判活等于永远「活的」；真后端在 `probe`（:8088）。
3. **不能只认死地址**：判活是「任一路通即就绪」，任一路都不通才判未运行。
"""
import sys
import unittest
from unittest import mock

sys.path.insert(0, __file__.rsplit("/tests/", 1)[0] + "/src")

import api_agents    # noqa: E402
import profiles      # noqa: E402


def _fake_port_open(open_set):
    return lambda port, host="127.0.0.1": (host, port) in open_set


class TestUiTargets(unittest.TestCase):
    def test_dsh_probes_its_bind_address_not_only_loopback(self):
        p = profiles.get_profile("dsh")
        spec = profiles.ui_start_for("dsh")
        targets = api_agents._ui_targets("dsh", spec, p)
        self.assertIn(("192.168.5.102", 3080), targets,
                      "dsh 只监听 LAN IP，漏掉它 ⇒ ui-status 恒 false（2026-10-25 报障）")
        # 关键回归：后端只在绑定地址上活着时，must be ready
        with mock.patch.object(api_agents.vitals_mod, "port_open",
                               _fake_port_open({("192.168.5.102", 3080)})):
            ready, host, port = api_agents._ui_ready("dsh", spec, p)
        self.assertTrue(ready)
        self.assertEqual((host, port), ("192.168.5.102", 3080))

    def test_qwenpaw_probe_wins_over_proxy_url(self):
        p = profiles.get_profile("qwenpaw")
        spec = profiles.ui_start_for("qwenpaw")
        targets = api_agents._ui_targets("qwenpaw", spec, p)
        ports = {pt for _h, pt in targets}
        self.assertIn(8088, ports, "真后端 :8088 必须在探活目标里")
        self.assertNotIn(3103, ports,
                         ":3103 是 hub 注入代理，hub 活着它就 listening ⇒ 掺进来会永远判活")
        with mock.patch.object(api_agents.vitals_mod, "port_open",
                               _fake_port_open({("127.0.0.1", 3103)})):
            ready, _h, _p = api_agents._ui_ready("qwenpaw", spec, p)
        self.assertFalse(ready, "只有代理活着而后端停着时必须判未运行")

    def test_loopback_only_agent_unchanged(self):
        p = profiles.get_profile("pi")
        spec = profiles.ui_start_for("pi")
        targets = api_agents._ui_targets("pi", spec, p)
        self.assertIn(("127.0.0.1", 30141), targets)
        with mock.patch.object(api_agents.vitals_mod, "port_open",
                               _fake_port_open({("::1", 30141)})):
            ready, host, _p = api_agents._ui_ready("pi", spec, p)
        self.assertTrue(ready, "IPv6 单栈后端也必须判活（qwenpaw 判例）")
        self.assertEqual(host, "::1")

    def test_url_cache_invalidated_after_start(self):
        """拉起后必须丢掉旧的进程级 token（否则「服务起来了、嵌页 401」）。"""
        import capabilities
        profiles._DSH_URL_CACHE.update({"ts": 1e9, "url": "http://h:3080/?token=old"})
        capabilities._URL_CACHE["dsh"] = (1e9, "http://h:3080/?token=old")
        profiles.invalidate_ui_url("dsh")
        self.assertIsNone(profiles._DSH_URL_CACHE["url"])
        self.assertNotIn("dsh", capabilities._URL_CACHE)


if __name__ == "__main__":
    unittest.main()
