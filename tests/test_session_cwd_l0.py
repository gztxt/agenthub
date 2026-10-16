"""P1-7 会话恢复 cwd 校验单测（L0 hermetic，零跳过）。

落地的手册判据（B1）：「恢复会话必须校验工作目录/归属，不过就保持失败，
**不得把空/错目录当恢复成功**」。

被测对象是 `session_cwd_ex()`（带来源）与 `resume_cwd_guard()`（纯函数）——
续聊端点据二者决定「拒启(409)」还是「照旧回落」。本文件不碰任何真实 agent store，
把 `_session_cwd_recorded` 换成可注入值即可，故属 L0。

红向自证（每例等价于一条负向断言）：
  · 删掉 guard 的 fallback 分支 ⇒ test_guard_rejects_vanished_dir 红；
  · 把 source='none' 也当异常 ⇒ test_guard_allows_no_record 红；
  · 用 `cwd != fallback` 代替 source 判据 ⇒ test_recorded_equal_fallback_is_recorded 红；
  · 给 create_session 加请求字段 ⇒ test_no_new_body_field_launch_invariant_holds 红；
  · session_cwd 改了回退语义 ⇒ test_session_cwd_backward_compatible 红。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

os.environ.setdefault("HUB_TEST_MODE", "1")

import sessions_store as ss  # noqa: E402

GONE = "/definitely/not/here/xyz-p1-7"


class _StubRecorded(unittest.TestCase):
    """`_session_cwd_recorded` 可注入 —— 不读真盘、不依赖任何 agent 是否装过。"""

    def setUp(self):
        self._orig = ss._session_cwd_recorded

    def tearDown(self):
        ss._session_cwd_recorded = self._orig

    def _stub(self, value):
        ss._session_cwd_recorded = lambda agent_id, sid: value


class TestSessionCwdEx(_StubRecorded):
    def test_recorded_dir_exists_is_recorded(self):
        with tempfile.TemporaryDirectory() as d:
            self._stub(d)
            got = ss.session_cwd_ex("claude", "sid", "/fallback")
        self.assertEqual(got["source"], "recorded")
        self.assertEqual(got["cwd"], d)
        self.assertEqual(got["recorded"], d)

    def test_vanished_dir_is_fallback_but_records_original(self):
        self._stub(GONE)
        got = ss.session_cwd_ex("claude", "sid", "/fallback")
        self.assertEqual(got["source"], "fallback")
        self.assertEqual(got["cwd"], "/fallback")
        # recorded 原样保留 —— 否则报错信息里说不出「到底哪个目录没了」
        self.assertEqual(got["recorded"], GONE)

    def test_no_record_is_none_not_fallback(self):
        """记录里本就没 cwd（如 cursor 空 cwd 条目）≠ 目录消失，不可混同。"""
        self._stub("")
        got = ss.session_cwd_ex("cursor", "sid", "/fallback")
        self.assertEqual(got["source"], "none")
        self.assertEqual(got["cwd"], "/fallback")

    def test_recorded_equal_fallback_is_recorded(self):
        """记录目录恰好==fallback 且存在 ⇒ recorded。

        专治「用 `cwd != fallback` 反推要不要拒启」的写法：那种写法在这里判不出区别。
        """
        with tempfile.TemporaryDirectory() as d:
            self._stub(d)
            got = ss.session_cwd_ex("claude", "sid", d)
        self.assertEqual(got["source"], "recorded")

    def test_session_cwd_backward_compatible(self):
        """旧调用点语义不变：目录没了仍回退 fallback（不改既有行为）。"""
        self._stub(GONE)
        self.assertEqual(ss.session_cwd("claude", "sid", "/fallback"), "/fallback")


class TestResumeCwdGuard(unittest.TestCase):
    def test_guard_rejects_vanished_dir(self):
        r = ss.resume_cwd_guard({"cwd": "/fb", "recorded": GONE, "source": "fallback"})
        self.assertTrue(r, "记录过的目录消失必须拒启（不得静默换成画像目录）")
        self.assertIn(GONE, r)

    def test_guard_allows_no_record(self):
        self.assertEqual(
            ss.resume_cwd_guard({"cwd": "/fb", "recorded": "", "source": "none"}), "")

    def test_guard_allows_recorded(self):
        self.assertEqual(
            ss.resume_cwd_guard({"cwd": "/x", "recorded": "/x", "source": "recorded"}), "")


class TestRequestContract(unittest.TestCase):
    def test_no_new_body_field_launch_invariant_holds(self):
        """P1-7 **不得**给 `create_session` 加请求字段。

        既有安全不变量（`tests/test_term_launch_guard.py`）：该端点能读到的请求字段
        只许 `agent_id / session_id / cwd`。加 `allow_cwd_fallback` 会给「让 body 影响 cwd」
        开一道新口子，也是本轮实际撞上的闸。故逃生路走「新建会话」。
        """
        import term
        fields = set(term.CreateIn.model_fields)
        self.assertEqual(fields, {"agent_id", "session_id", "cwd"},
                         "create_session 的请求字段集变了：需同步 test_term_launch_guard 并证明无注入面")


if __name__ == "__main__":
    unittest.main()