"""dsh 带 token 地址必须属于**当前进程**（2026-10-30 报障）。

现象
----
用户点 DSH（或刚被拉起后等界面就绪）看到的不是 dsh 界面，而是它自己那行
`dsh web authentication required; reopen the URL printed by dsh web.`

根因
----
`profiles._dsh_web_url()` 旧实现读 `journalctl -o cat` 最近 200 行，取**最后一条**
带 token 的行。dsh 的 token 是进程级随机（`browser-auth.ts` 的 `randomBytes(32)`），
重启后横幅要等 4~8s 才打印（实测 12:49:17 Started → 12:49:21 才出 token）。这段
窗口里"最后一条"仍是**上一个进程**的 token ⇒ 浏览器拿死 token 换 cookie 必 401，
而 hub 又把这个地址缓存 30s、嵌页 `dataset.src` 不变就不重载 ⇒ 用户只能自己重进。

三条判据
--------
① 只认 `_PID == MainPID` 的横幅；别的进程的 token 一律不外发（宁缺毋滥）。
② 刚重启的那几秒要能被识别为 `pending`（前端据此继续等，而不是把 401 页面嵌进去）。
③ 取不到就 `unknown`（明确降级成裸地址），**不冒充 ok** —— 死 token 比裸地址更坏。
"""
import os
import subprocess
import sys
import unittest
from unittest import mock

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import profiles  # noqa: E402

OLD = "http://192.168.5.102:3080/?token=AAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAAA"
NEW = "http://192.168.5.102:3080/?token=BBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBBB"
BANNER = "dsh web: listening on 192.168.5.102 over plain HTTP; restrict this port to a trusted proxy or network\n"
TOKEN_LINE = "dsh web: %s\n"


def _journal(*lines):
    """[(pid, text)] —— 假 journalctl 按 `_PID=` 过滤，行为与真 journalctl 一致。"""
    return list(lines)


class DshTokenPidTest(unittest.TestCase):
    def setUp(self):
        profiles.invalidate_ui_url("dsh")

    def tearDown(self):
        profiles.invalidate_ui_url("dsh")

    def _run(self, mainpid, journal, age=600.0):
        calls = []

        def fake_run(cmd, **kw):
            calls.append(cmd)
            out = "".join(t for pid, t in journal)
            for a in cmd:
                if isinstance(a, str) and a.startswith("_PID="):
                    want = a.split("=", 1)[1]
                    out = "".join(t for pid, t in journal if str(pid) == want)
            return subprocess.CompletedProcess(cmd, 0, stdout=out, stderr="")

        with mock.patch.object(profiles, "_dsh_main_pid", return_value=mainpid), \
                mock.patch.object(profiles, "_dsh_proc_age_s", return_value=age), \
                mock.patch.object(subprocess, "run", side_effect=fake_run):
            out = profiles._dsh_scan()
        return out, calls

    def test_only_current_process_banner_is_ok(self):
        url, state = self._run(4242, _journal((4242, BANNER), (4242, TOKEN_LINE % NEW)))[0]
        self.assertEqual(state, "ok")
        self.assertEqual(url, NEW)

    def test_banner_window_is_filtered_by_pid_not_by_position(self):
        """日志里只剩**旧 PID** 的行（新进程还没打印）⇒ 必须 pending，绝不外发旧 token。"""
        url, state = self._run(4242, _journal((99, BANNER), (99, TOKEN_LINE % OLD)),
                               age=3.0)[0]
        self.assertIsNone(url, "旧进程的 token 被当成本次地址外发")
        self.assertEqual(state, "pending")

    def test_mixed_history_returns_current_process_token_only(self):
        """新旧横幅同时在（日志窗口有重叠）时，也要挑当前进程那条。"""
        url, state = self._run(4242, _journal((99, TOKEN_LINE % OLD),
                                              (4242, TOKEN_LINE % NEW)))[0]
        self.assertEqual((url, state), (NEW, "ok"))

    def test_pending_expires_so_ui_never_hangs_forever(self):
        """进程起了 120s 还没打印横幅 ⇒ 判 unknown（降级），不能把界面永久显示成死的。"""
        url, state = self._run(4242, _journal((4242, BANNER)),
                               age=profiles._DSH_PENDING_MAX_S + 1)[0]
        self.assertIsNone(url)
        self.assertEqual(state, "unknown")

    def test_unit_not_running_is_unknown(self):
        url, state = self._run(None, _journal((99, BANNER), (99, TOKEN_LINE % OLD)))[0]
        self.assertIsNone(url)
        self.assertEqual(state, "unknown")

    def test_journalctl_failure_degrades_not_raises(self):
        with mock.patch.object(profiles, "_dsh_main_pid", return_value=4242), \
                mock.patch.object(subprocess, "run", side_effect=OSError("boom")):
            self.assertEqual(profiles._dsh_scan(), (None, "unknown"))

    def test_query_filters_by_pid_field(self):
        """判据落在 journalctl 的 `_PID=` 字段匹配上，不是"读最近 N 行再挑"。"""
        _out, calls = self._run(4242, _journal((4242, TOKEN_LINE % NEW)))
        self.assertTrue(calls, "没调 journalctl")
        joined = " ".join(calls[0])
        self.assertIn("_PID=4242", joined)

    def test_pending_is_not_cached_long(self):
        """pending 只缓存 1s：否则重启后要等 30s 才肯认新 token。"""
        self.assertLess(profiles._DSH_URL_TTL_PENDING_S, 5.0)
        self.assertGreaterEqual(profiles._DSH_URL_TTL_S, 10.0)

    def test_web_url_never_returns_dead_token(self):
        url, _state = self._run(4242, _journal((99, TOKEN_LINE % OLD)), age=2.0)[0]
        self.assertIsNone(url)


class DshTokenCacheTest(unittest.TestCase):
    """缓存命中也要复核 PID —— 否则重启后最多 30s 还在吐旧进程的地址。"""

    def setUp(self):
        profiles.invalidate_ui_url("dsh")

    def tearDown(self):
        profiles.invalidate_ui_url("dsh")

    def test_cache_hit_is_dropped_when_mainpid_changes(self):
        seq = [111]
        with mock.patch.object(profiles, "_dsh_main_pid", side_effect=lambda: seq[0]), \
                mock.patch.object(profiles, "_dsh_proc_age_s", return_value=600.0), \
                mock.patch.object(profiles, "_dsh_scan",
                                  side_effect=lambda pid=None: (
                                      "http://h:3080/?token=P%s" % pid, "ok")):
            u1, _ = profiles._dsh_token()
            seq[0] = 222                      # 服务重启
            u2, _ = profiles._dsh_token()     # 缓存 TTL 内，但 PID 变了
        self.assertEqual(u1, "http://h:3080/?token=P111")
        self.assertEqual(u2, "http://h:3080/?token=P222",
                         "PID 换了还吐旧地址 ⇒ 浏览器必然 401")


class UiStatusWaitsForTokenTest(unittest.TestCase):
    """`/ui-status` 不能只看端口：横幅还没打印时嵌进去必 401。"""

    def _status(self, state):
        import asyncio
        import api_agents
        with mock.patch.object(api_agents, "_ui_ready", return_value=(True, "h", 3080)), \
                mock.patch.object(profiles, "dsh_token_state", return_value=state):
            return asyncio.run(api_agents.agent_ui_status("dsh"))

    def test_pending_is_not_ready(self):
        d = self._status("pending")
        self.assertFalse(d["ready"], "token 还属于已退出进程时报 ready ⇒ 前端把 401 页面嵌进去")
        self.assertEqual(d["token_state"], "pending")

    def test_ok_is_ready(self):
        d = self._status("ok")
        self.assertTrue(d["ready"])

    def test_unknown_degrades_instead_of_hanging(self):
        """journal 被 vacuum / 格式变了 ⇒ 必须照常就绪（裸地址），不能把能用的界面判死。"""
        d = self._status("unknown")
        self.assertTrue(d["ready"])


class EmbeddedUrlFollowsDataTest(unittest.TestCase):
    """地址变了要跟进（`dataset.src` 不变则 iframe 不导航 ⇒ 一直停着旧 token 的 401）。"""

    ROOT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..")

    def _js(self, name):
        with open(os.path.join(self.ROOT, "static", "hub", name), encoding="utf-8") as f:
            return f.read()

    def test_defined_and_called_on_agents_refresh(self):
        nav = self._js("02-nav-and-poll.js")
        boot = self._js("01-core-boot.js")
        self.assertIn("function syncEmbedUrlIfChanged", nav)
        self.assertIn("syncEmbedUrlIfChanged()", boot,
                      "loadAgents 拿到新 /api/agents 后没有跟进地址 ⇒ 重启后只能手动重试")

    def test_built_bundle_is_in_sync(self):
        """hub.js 由 static/hub/NN-*.js 拼接（build_hubjs.sh），漏重建就是「源码改了、线上没改」。"""
        with open(os.path.join(self.ROOT, "static", "hub.js"), encoding="utf-8") as f:
            bundle = f.read()
        self.assertIn("function syncEmbedUrlIfChanged", bundle)


if __name__ == "__main__":
    unittest.main()
