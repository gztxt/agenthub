"""回归测：终端尺寸所有权（B2 / paseo 融合）——后台那一端不许偷走 PTY 尺寸。

借鉴出处（paseo v0.10.0 实证）：
  packages/server/src/terminal/terminal-size-ownership.ts（全文 38 行）
  docs/terminal-performance.md:31 原话：lets an owning pane follow splits and
    keyboard insets without allowing an idle phone or browser to steal the PTY size.

要防的真问题：桌面正开着 vim（120×40），手机端同一会话的页面在后台被 ResizeObserver
或 visibilitychange 唤醒发来 80×24 ⇒ 桌面 vim 被压扁。用户看到的是
「我什么都没做，终端自己乱了」，且极难归因。

本测钉死五件事：
  ① 非所有者的 update 被静默忽略（尺寸不变）；
  ② claim 无条件夺权，且**同尺寸也要转移所有权**（paseo 测试同名用例）；
  ③ 老客户端没有 intent 字段 ⇒ 缺省按 claim（否则前后端版本错配会让尺寸永远改不动）；
  ④ 尺寸未变不下 ioctl（等价于 paseo 的服务端短路）；
  ⑤ 非法/越界行列只忽略、绝不抛 —— 高频几何事件炸掉 WS 收包循环
     ⇒ 「拖一下窗口」变成「终端断开」。

用 openpty 拿真 fd（不 fork 进程）：ioctl 走的是真实路径，不是 mock 出来的假绿。
跑法：cd ~/agent-hub && venv/bin/python -m unittest tests.test_term_size_owner -v
"""
import os
import pty
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import term  # noqa: E402


class _FakeSess:
    """只要 _apply_size 真正用到的那几个字段；fd 用真 pty slave。"""

    def __init__(self, fd):
        self.id = "sess-test"
        self.rows = 24
        self.cols = 80
        self.fd = fd


class _SizeOwnerTest(unittest.TestCase):
    def setUp(self):
        self.mfd, self.sfd = pty.openpty()
        self.addCleanup(self._close)
        self.sess = _FakeSess(self.sfd)
        term._size_owner.pop(self.sess.id, None)   # 用例之间不串味

    def _close(self):
        for fd in (self.mfd, self.sfd):
            try:
                os.close(fd)
            except OSError:
                pass

    def _apply(self, vid, rows, cols, intent):
        return term._apply_size(self.sess, vid, rows, cols, intent)


class TestUpdate(_SizeOwnerTest):
    def test_non_owner_update_is_ignored(self):
        """这就是要防的那件事：后台端发尺寸，PTY 尺寸纹丝不动。"""
        self.assertTrue(self._apply("desk", 40, 120, "claim"))
        self.assertEqual((self.sess.rows, self.sess.cols), (40, 120))
        self.assertFalse(self._apply("phone", 24, 80, "update"), "非所有者的 update 不该生效")
        self.assertEqual((self.sess.rows, self.sess.cols), (40, 120), "桌面尺寸被偷走了")

    def test_owner_update_works(self):
        self._apply("desk", 40, 120, "claim")
        self.assertTrue(self._apply("desk", 50, 132, "update"), "所有者的 update 应该生效")
        self.assertEqual((self.sess.rows, self.sess.cols), (50, 132))

    def test_update_without_any_owner_is_ignored(self):
        """没人 claim 过 ⇒ update 一律无效（避免任何闲杂连接改尺寸）。"""
        self.assertFalse(self._apply("whoever", 30, 100, "update"))
        self.assertEqual((self.sess.rows, self.sess.cols), (24, 80))


class TestClaim(_SizeOwnerTest):
    def test_claim_takes_over_even_at_same_size(self):
        """paseo 明确的反直觉细节：claim 即使尺寸相同也要转移所有权
        （terminal-size-ownership.test.ts:48-59）。少这一条，端切换后尺寸就再也改不动。"""
        self._apply("desk", 40, 120, "claim")
        self.assertFalse(self._apply("phone", 40, 120, "claim"), "尺寸相同 ⇒ 不下 ioctl")
        self.assertEqual(term._size_owner[self.sess.id], "phone", "同尺寸 claim 也必须转移所有权")
        self.assertTrue(self._apply("phone", 30, 90, "update"), "夺权后 update 应当生效")

    def test_claim_from_other_client_steals_ownership(self):
        self._apply("desk", 40, 120, "claim")
        self._apply("phone", 30, 90, "claim")
        self.assertFalse(self._apply("desk", 40, 120, "update"), "旧所有者已失去所有权")


class TestCompatAndRobustness(_SizeOwnerTest):
    def test_missing_intent_defaults_to_claim(self):
        """老客户端（无 intent 字段）必须照旧工作 ⇒ 缺省 claim。"""
        self.assertEqual(term._norm_intent(None), "claim")
        self.assertEqual(term._norm_intent(""), "claim")
        self.assertTrue(self._apply("legacy", 40, 120, term._norm_intent(None)))
        self.assertEqual(term._size_owner[self.sess.id], "legacy")

    def test_unknown_intent_is_conservative(self):
        """看不懂的意图按 update 处理（要所有权），绝不能旁路所有权检查。"""
        for raw in ("CLAIMX", "steal", "0", 123):
            with self.subTest(raw=raw):
                self.assertEqual(term._norm_intent(raw), "update")
        self._apply("desk", 40, 120, "claim")
        self.assertFalse(self._apply("phone", 24, 80, term._norm_intent("nonsense")),
                         "未知意图竟然能改尺寸 ⇒ 所有权检查被旁路")
        self.assertEqual((self.sess.rows, self.sess.cols), (40, 120))

    def test_same_size_is_a_noop(self):
        self.assertFalse(self._apply("a", 24, 80, "claim"), "尺寸未变不该打扰 pty")
        self.assertEqual(term._size_owner[self.sess.id], "a")

    def test_unparsable_values_are_ignored_not_raised(self):
        """解析不了的只忽略：高频几何事件炸掉 WS 收包循环
        ⇒ 「拖一下窗口」变成「终端断开」，比不改尺寸糟得多。"""
        for bad in (None, "abc", "", [], {}):
            with self.subTest(bad=bad):
                self.assertFalse(self._apply("a", bad, 100, "claim"), f"{bad!r} 竟然被接受")
                self.assertFalse(self._apply("a", 30, bad, "claim"), f"{bad!r} 竟然被接受")
        self.assertEqual((self.sess.rows, self.sess.cols), (24, 80), "脏输入把尺寸改了")

    def test_out_of_range_is_clamped_not_rejected(self):
        """越界值收敛到合法区间（而不是拒绝）：pty 尺寸是 struct.pack("HHHH")，
        直接把 99999 交给它会抛 struct.error 打断收包循环。"""
        self.assertTrue(self._apply("a", 100000, 100000, "claim"))
        self.assertLessEqual(self.sess.rows, term._ROWS_RANGE[1])
        self.assertLessEqual(self.sess.cols, term._COLS_RANGE[1])
        self._apply("a", -5, 0, "claim")
        self.assertGreaterEqual(self.sess.rows, term._ROWS_RANGE[0])
        self.assertGreaterEqual(self.sess.cols, term._COLS_RANGE[0])


if __name__ == "__main__":
    unittest.main(verbosity=2)


class TestInitialSize(_SizeOwnerTest):
    """新建 PTY 的初始尺寸（2026-10-11，grok 卡死根因）。

    ★ 为什么这条必须测：pty.fork()/openpty() 给的 winsize 是 **0×0**，而原实现
      只在 Session 上填了 rows/cols 这两个 Python 属性、**从不调 ioctl** ⇒ 子进程
      开局拿到的「终端」是 0 行 0 列。grok 的 TUI 读到 0 尺寸会永久卡在启动
      （真 pty 实测：128 字节后再无输出，且事后 SIGWINCH 唤不醒）。
      而 _apply_size 又拿被硬编码的 80×24 去跟这个**假值**比 ⇒ 前端首帧 resize
      若恰是 80×24，短路生效、真 ioctl 永不下发 ⇒ pty 一直停在 0×0。

    判据用 TIOCGWINSZ 直读 pty，不看 Session 上的属性 —— 属性是「宣布值」，
    本用例要钉的是「真值」，两者在缺陷形态下正好不一致。
    """

    def _winsz(self, fd):
        import fcntl, struct, termios
        return struct.unpack("HHHH", fcntl.ioctl(fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]

    def test_fresh_pty_reports_zero_zero(self):
        """前提事实：不设尺寸的新 pty 就是 0×0（钉住这条，修法才有的放矢）。"""
        self.assertEqual(self._winsz(self.sfd), (0, 0), "openpty 的 winsize 不再是 0×0，本用例前提已变")

    def test_set_winsize_writes_real_pty(self):
        """_set_winsize 必须真的写进 pty，而不是只记在会话对象上。"""
        self.assertTrue(term._set_winsize(self.sfd, 24, 80))
        self.assertEqual(self._winsz(self.sfd), (24, 80))

    def test_set_winsize_rejects_unpackable(self):
        """越界行列只返回 False、绝不抛（高频路径不许被 struct.error 打断）。"""
        self.assertFalse(term._set_winsize(self.sfd, 70000, 80))
        self.assertFalse(term._set_winsize(self.sfd, 24, 70000))

    def test_initial_constants_are_positive(self):
        """初始尺寸常量必须是正数；0 就是本次根因本身。"""
        self.assertGreater(term.INITIAL_ROWS, 0)
        self.assertGreater(term.INITIAL_COLS, 0)

    def test_new_session_pty_is_not_zero_sized(self):
        """★ 本版核心判据：新建 Session 的真 PTY 尺寸不再是 0×0。

        直接构造真 Session（不 mock），用 TIOCGWINSZ 从**master fd 读回真值**
        —— 属性 self.rows/cols 是「宣布值」，本用例要的是「pty 真值」。
        """
        import fcntl, signal, struct, termios
        sess = term.Session("t-init", "shell", ["/bin/cat"], "/tmp")
        self.addCleanup(lambda: (sess._signal_group(signal.SIGKILL), sess._cleanup()))
        got = struct.unpack("HHHH", fcntl.ioctl(sess.fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        self.assertEqual(got, (term.INITIAL_ROWS, term.INITIAL_COLS),
                         "新会话的 pty 是 %r —— 0×0 会让 grok 之类 TUI 永久卡在启动" % (got,))
        self.assertNotEqual(got, (0, 0))

    def test_gate_is_discriminating(self):
        """红向自证：把 _set_winsize 抹成 no-op，同一个断言必须**能**看出 0×0。

        没有这条，上一例只是「碰巧绿」——无法区分「修法生效」与「判据根本不看这事」。
        """
        import fcntl, signal, struct, termios
        real = term._set_winsize
        term._set_winsize = lambda fd, r, c: False      # 模拟修复前的行为
        self.addCleanup(lambda: setattr(term, "_set_winsize", real))
        sess = term.Session("t-init-red", "shell", ["/bin/cat"], "/tmp")
        self.addCleanup(lambda: (sess._signal_group(signal.SIGKILL), sess._cleanup()))
        got = struct.unpack("HHHH", fcntl.ioctl(sess.fd, termios.TIOCGWINSZ, b"\0" * 8))[:2]
        self.assertEqual(got, (0, 0), "判据不区分有无初始尺寸 ⇒ 上一例是假绿")
