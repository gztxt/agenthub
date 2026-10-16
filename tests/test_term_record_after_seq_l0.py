#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P2 B14 回放**续传点** `after_seq`（src/term_record.frames_page）。

守的判据（手册 B14：「持久通流须有**可恢复续传点**（通道+序号），断连从最后确认位置续接」）：

1. **`after_seq` 是严格大于**（`>` 而非 `>=`）：客户端把 `next_after_seq` 原样回传，
   不得重复拿到边界帧（重复帧在回放里表现为「输出重了一行」）。
2. **`more` 是下界判断**：拿满 `limit` 条就**不敢说「没有了」**（可能还有）。
   说「没有了」需要额外一次探测 —— 宁可让客户端多拉一次空的，也不静默截断。
3. **`next_after_seq` 只在 `more=True` 时给**：给一个永远取不到新帧的游标，
   会让客户端空转轮询（那是把「没有更多」伪装成「等一等还有」）。
4. **兼容**：旧调用 `frames()` 仍返回裸列表（既有调用点与测试不动）。
5. **迭代收敛**：反复用 `next_after_seq` 续接，最终**恰好取完且不重不漏**。

分层口径（tests/tiers.py）：临时 db，不起服务、不连网、零 SKIP。
"""
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db           # noqa: E402
import term_record  # noqa: E402


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0b14-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        self.addCleanup(self._restore)
        for i in range(1, 6):
            self._add(i)

    def _restore(self):
        db._conn = self._saved

    def _add(self, seq, sid="s1"):
        db.execute(
            "INSERT INTO term_recordings(session_id,agent_id,seq,ts,direction,data,bytes)"
            " VALUES(?,?,?,?,?,?,?)",
            (sid, "claude", seq, "2026-10-16T00:00:%02dZ" % seq, "out",
             ("frame-%d" % seq).encode(), 7))


class TestFramesPage(_Base):
    def test_first_page_reports_more_and_cursor(self):
        p = term_record.frames_page("s1", limit=3)
        self.assertEqual([f["seq"] for f in p["frames"]], [1, 2, 3])
        self.assertTrue(p["more"])
        self.assertEqual(p["next_after_seq"], 3)

    def test_after_seq_is_strictly_greater(self):
        p = term_record.frames_page("s1", limit=10, after_seq=3)
        self.assertEqual([f["seq"] for f in p["frames"]], [4, 5],
                         "after_seq 必须是 `>`：等于边界的那条不得重复回包")

    def test_tail_page_has_no_cursor(self):
        p = term_record.frames_page("s1", limit=3, after_seq=3)
        self.assertFalse(p["more"])
        self.assertIsNone(p["next_after_seq"], "没有更多时不得给游标（否则客户端空转轮询）")

    def test_empty_when_caught_up(self):
        p = term_record.frames_page("s1", limit=3, after_seq=5)
        self.assertEqual(p["frames"], [])
        self.assertFalse(p["more"])
        self.assertIsNone(p["next_after_seq"])

    def test_exactly_limit_is_reported_as_more(self):
        """下界判断：拿满 limit 就不敢断言「没有了」。"""
        p = term_record.frames_page("s1", limit=5)
        self.assertEqual(p["count"], 5)
        self.assertTrue(p["more"], "恰好吃满 limit 时必须说「可能还有」")
        self.assertEqual(p["next_after_seq"], 5)

    def test_after_seq_zero_is_same_as_no_cursor(self):
        a = term_record.frames_page("s1", limit=10, after_seq=0)["frames"]
        b = term_record.frames_page("s1", limit=10)["frames"]
        self.assertEqual([f["seq"] for f in a], [f["seq"] for f in b])

    def test_first_last_seq_fields(self):
        p = term_record.frames_page("s1", limit=2)
        self.assertEqual((p["first_seq"], p["last_seq"]), (1, 2))
        e = term_record.frames_page("s1", limit=2, after_seq=99)
        self.assertIsNone(e["first_seq"])
        self.assertIsNone(e["last_seq"])

    def test_unknown_session_is_empty_not_error(self):
        p = term_record.frames_page("nope", limit=3)
        self.assertEqual(p["count"], 0)
        self.assertFalse(p["more"])


class TestIterativeResume(_Base):
    def test_resume_loop_covers_everything_exactly_once(self):
        """反复用 next_after_seq 续接 ⇒ 恰好取完、不重不漏（续传点的定义本身）。"""
        seen, cursor, rounds = [], None, 0
        while True:
            p = term_record.frames_page("s1", limit=2, after_seq=cursor)
            seen.extend(f["seq"] for f in p["frames"])
            rounds += 1
            self.assertLess(rounds, 10, "续接没有收敛")
            if not p["more"]:
                break
            self.assertEqual(p["next_after_seq"], p["last_seq"])
            cursor = p["next_after_seq"]
        self.assertEqual(seen, [1, 2, 3, 4, 5], "不重不漏")


class TestBackCompat(unittest.TestCase):
    def test_frames_still_returns_a_bare_list(self):
        import inspect
        src = inspect.getsource(term_record.frames)
        self.assertIn("frames_page", src,
                      "旧 frames() 必须**委托**实现（不得再抄一份 SQL —— 两份必然漂移）")
        self.assertIn("List", str(inspect.signature(term_record.frames)),
                      "返回注解应仍是 List（裸列表，兼容旧调用点）")


if __name__ == "__main__":
    unittest.main()