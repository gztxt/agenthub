#!/usr/bin/env python3
"""v0.13.58（档二）：TTL 判据必须是「无生命迹象」，不能是「客户端静默」。

背景（实弹证据，不是推断）：旧口径 `now - s.last_io > TTL` 在 TERM_IDLE_TTL=45s 的
实例上把「agent 每 2s 仍在产出、idle_s 涨到 40s」的会话照样回收了，另一端重连
直接收到 **4410（已结束）**。用户诉求是「任务状态跨客户端连续」，而旧口径恰好把
「客户端不在」当成死亡信号 —— 换设备/切后台就腰斩正在跑的任务。

判据（三条必须同时成立才回收，缺一即保活）：
  A 有观看者（viewers 非空）⇒ 绝不回收，哪怕 last_io/last_activity 都很旧
  B pty 在产出（last_activity 新）⇒ 绝不回收，哪怕 last_io 很旧（客户端不在）
  C 真无生命迹象（无人看 + 两个时钟都超 TTL）⇒ 必须回收（TTL 不能被改成永生）

外加两条口径一致性：
  D idle_max_s 必须与 _reap 同口径（报 max(last_activity,last_io)），
    否则 /health 会拿「正在被回收的会话」自证「不忙」⇒ 假绿
  E 心跳既不续 last_io 也不续 last_activity（P0-1 语义不被本次改回）
"""
import os
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import term  # noqa: E402


class _S:
    """按本次契约暴露全部判据字段的轻量会话替身。"""

    def __init__(self, sid, alive=True, idle_s=0.0, activity_s=None, viewers=0):
        self.id = sid
        self.alive = alive
        self.last_io = time.time() - idle_s
        self.last_activity = time.time() - (idle_s if activity_s is None else activity_s)
        self.viewers = {"v%d" % i: None for i in range(viewers)}
        self.killed = 0
        self.cleaned = 0

    def kill(self):
        self.killed += 1

    def _cleanup(self):
        self.cleaned += 1

    def poll_exited(self):
        return False


class TestTtlIsAboutActivityNotClientSilence(unittest.TestCase):
    def setUp(self):
        self._saved = dict(term._sessions)
        term._sessions.clear()

    def tearDown(self):
        term._sessions.clear()
        term._sessions.update(self._saved)

    def _reap(self):
        term._reap()
        return set(term._sessions)

    # ── A：有观看者 ⇒ 永不回收（哪怕客户端一动不动）────────────────
    def test_watched_session_never_reaped_despite_client_silence(self):
        """用户正看着终端、手不动（想让它自己跑）⇒ 不能被回收。

        这是旧口径最刺眼的误伤：人在看、agent 在跑，只因客户端没发交互帧就死了。
        """
        s = _S("watched", idle_s=term.IDLE_TTL_S + 100, activity_s=term.IDLE_TTL_S + 100,
               viewers=1)
        term._sessions["watched"] = s
        self.assertIn("watched", self._reap())
        self.assertEqual(0, s.killed, "有人看着的会话被杀了")

    # ── B：无观看者但 pty 在产出 ⇒ 永不回收（换设备/切后台的正身）────
    def test_unwatched_but_active_pty_not_reaped(self):
        """核心回归：客户端静默 + 无观看者，但 agent 仍在产出 ⇒ 必须活着。

        旧口径在这里会 kill ⇒ 换端重连收到 4410。last_activity 新、last_io 很旧。
        """
        s = _S("cross-device", idle_s=term.IDLE_TTL_S + 100, activity_s=1, viewers=0)
        term._sessions["cross-device"] = s
        self.assertIn("cross-device", self._reap(), "跨客户端连续性被腰斩")
        self.assertEqual(0, s.killed, "pty 还在产出却被当空闲杀掉")

    def test_activity_alone_saves_session_even_when_io_dead(self):
        """last_io 彻底停摆、只有 pty 产出 ⇒ 仍然保活（两个时钟取并集的用意）。"""
        s = _S("agent-running", idle_s=10 * term.IDLE_TTL_S, activity_s=2, viewers=0)
        term._sessions["agent-running"] = s
        self.assertIn("agent-running", self._reap())
        self.assertEqual(0, s.killed)

    # ── C：真无生命迹象 ⇒ 必须回收（TTL 不是摆设）───────────────────
    def test_truly_dead_session_is_reaped(self):
        """没人看 + pty 完全静默 + 两个时钟都超 TTL ⇒ 必须 kill（配额要能释放）。"""
        s = _S("ghost", idle_s=term.IDLE_TTL_S + 50,
               activity_s=term.IDLE_TTL_S + 50, viewers=0)
        term._sessions["ghost"] = s
        self.assertIn("ghost", self._reap(), "kill 是异步升级，本轮不直接摘除")
        self.assertEqual(1, s.killed, "真无生命迹象却没回收 ⇒ TTL 被改成永生了")

    def test_one_live_watcher_blocks_reap_regardless_of_activity(self):
        s = _S("watched2", idle_s=term.IDLE_TTL_S + 999,
               activity_s=term.IDLE_TTL_S + 999, viewers=2)
        term._sessions["watched2"] = s
        self._reap()
        self.assertEqual(0, s.killed)

    def test_borderline_not_reaped(self):
        """刚过 TTL 一小会儿也不杀：TTL 判据看 max(时钟)，别在边界抖动。

        ⚠ 2026-10-24（PT-20261024-08 B2）：边界值改用 `term.effective_ttl(s)`
        而不是硬编码 `IDLE_TTL_S` —— 两级 TTL 后（无人观看有更短的
        `UNWATCHED_TTL_S`），拿 `IDLE_TTL_S - 5` 当边界会**远超**有效阈值 ⇒ 误判红。
        `effective_ttl()` 是 `_reap()` 与测试共用的**单一真相源**。
        """
        s = _S("edge", idle_s=term.IDLE_TTL_S - 5, activity_s=term.IDLE_TTL_S - 5,
               viewers=0)
        ttl = term.effective_ttl(s)
        s.idle_s = ttl - 5          # 刚过**有效** TTL 一小会儿
        s.last_io = time.time() - (ttl - 5)
        s.last_activity = time.time() - (ttl - 5)
        term._sessions["edge"] = s
        self._reap()
        self.assertEqual(0, s.killed)

    # ── D：idle_max_s 与 _reap 同口径（自证字段不许假绿）────────────
    def test_idle_max_s_uses_activity_clock(self):
        """客户端静默很久但 pty 在产出 ⇒ 报的值必须小（否则 /health 假绿）。"""
        s = _S("busy", idle_s=term.IDLE_TTL_S + 100, activity_s=2, viewers=0)
        term._sessions["busy"] = s
        got = term.idle_max_s()
        self.assertLessEqual(got, 5, f"idle_max_s 报了客户端静默时长 {got}s ⇒ 与 _reap 口径分裂")


class TestHeartbeatStillDoesNotTouchAnything(unittest.TestCase):
    """E：P0-1 的语义不被本次改动推翻 —— 心跳两个时钟都不续。"""

    class _T:
        def __init__(self):
            self.last_io = 1000.0
            self.last_activity = 1000.0

    def test_hb_frame_touches_neither_clock(self):
        t = self._T()
        term._touch(t, '{"type":"hb"}')
        self.assertEqual(1000.0, t.last_io)
        self.assertEqual(1000.0, t.last_activity, "心跳续了 last_activity ⇒ TTL 又被架空")

    def test_resize_touches_both_clocks(self):
        t = self._T()
        term._touch(t, '{"type":"resize","cols":100,"rows":30}')
        self.assertGreater(t.last_io, 1000.0)
        self.assertGreater(t.last_activity, 1000.0)

    def test_binary_input_touches_both_clocks(self):
        t = self._T()
        term._touch_bytes(t)
        self.assertGreater(t.last_io, 1000.0)
        self.assertGreater(t.last_activity, 1000.0)


if __name__ == "__main__":
    unittest.main()
