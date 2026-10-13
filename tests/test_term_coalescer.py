"""回归测：终端输出合并器（B1 / paseo 融合）——合并的是**帧数**，一个字节都不许丢。

借鉴出处（paseo v0.10.0 实证）：
  packages/server/src/terminal/terminal-output-coalescer.ts:19,39-64 的 5ms 前后沿节流
  上游 docs/terminal-performance.md:22 原话（摘引见 docs/PASEO-FUSION-PLAN-20260929.md:140）：只留后沿（普通 debounce）会给每次按键回显
    平白加一个完整窗口的延迟 —— 所以前沿「空闲后第一块立刻发」不是优化，是正确性。

本测钉死四件事：
  ① 前沿：空闲后的第一块**立刻**刷出，不给按键回显加延迟；
  ② 后沿：突发期间攒着，一趟刷出（帧数从 N 降到 1）；
  ③ 内容守恒：合并前后字节完全相等（合并的是帧，绝不能合并掉内容）；
  ④ 保序：带外消息（退出提示）发送前 flush，否则会插到最后一段输出之前；
     close() 必须掐掉在飞的定时器，不然已离开的观看者还被 loop 攥着。

全纯内存，不起服务、不 fork pty。
跑法：cd ~/agent-hub && venv/bin/python -m unittest tests.test_term_coalescer -v
"""
import asyncio
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import term  # noqa: E402


def _run(coro):
    return asyncio.run(coro)


class TestLeadingEdge(unittest.TestCase):
    def test_first_chunk_flushes_immediately(self):
        """空闲后第一块立刻发：按键回显不能等一个窗口。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=20)
            c.handle(b"a")
            return out                      # 不 await 任何东西：必须同步就到
        self.assertEqual(_run(main()), [b"a"])

    def test_idle_again_after_window_flushes_immediately(self):
        """隔了一个窗口再来一块，仍然立刻发（前沿靠的是「距上次刷出」的间隔）。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=20)
            c.handle(b"a")
            await asyncio.sleep(0.05)        # > 20ms，重回空闲
            c.handle(b"b")
            return list(out)                 # 第二块在 sleep 之前就该已入列
        self.assertEqual(_run(main()), [b"a", b"b"])


class TestTrailingEdge(unittest.TestCase):
    def test_burst_collapses_into_one_flush(self):
        """突发：50 块攒成一趟。帧数从 50 降到 1 —— 这正是要消掉的 WS 帧洪水。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=30)
            c.handle(b"a")                   # 前沿立刻
            for _ in range(50):
                c.handle(b"x")
            mid = list(out)                  # 窗口未到：后续 50 块都还在缓冲里
            await asyncio.sleep(0.08)
            return mid, out
        mid, out = _run(main())
        self.assertEqual(mid, [b"a"], "窗口未到就刷出来了 ⇒ 后沿没生效")
        self.assertEqual(len(out), 2, f"突发应只多出一帧，实得 {len(out)} 帧")
        self.assertEqual(out[1], b"x" * 50, "攒的内容不对")

    def test_bytes_are_conserved(self):
        """合并的是帧数，不是内容：进出必须逐字节相等。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=20)
            chunks = [b"hello ", b"world", b"\x1b[31m", "中文".encode(), b"\r\n"]
            for ch in chunks:
                c.handle(ch)
            c.flush()
            await asyncio.sleep(0.05)
            return b"".join(out), b"".join(chunks)
        got, want = _run(main())
        self.assertEqual(got, want, "合并后字节数/内容变了 ⇒ 丢了数据")


class TestOutOfBandOrdering(unittest.TestCase):
    def test_flush_before_out_of_band_message(self):
        """带外消息（退出提示）必须排在最后一段输出之后：先 flush 再入队。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=50)
            c.handle(b"tail-of-output")      # 前沿立刻刷
            c.handle(b"buffered")            # 攒着
            c.flush()                        # ← 带外消息前的强制 flush
            out.append(b"[process exited]")  # 带外消息自己入队
            await asyncio.sleep(0.08)
            return out
        out = _run(main())
        self.assertEqual(out[-1], b"[process exited]", "退出提示没排到最后 ⇒ 顺序错乱")
        self.assertIn(b"buffered", out[:-1], "被攒的输出没在退出提示之前发出去")


class TestLifecycle(unittest.TestCase):
    def test_close_cancels_pending_timer(self):
        """观看者已经走了，定时器不能再把它刷出来（也别攥着 loop 引用）。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=30)
            c.handle(b"a")                   # 前沿立刻
            c.handle(b"b")                   # 攒（定时器已武装）
            c.close()
            await asyncio.sleep(0.08)
            return out
        self.assertEqual(_run(main()), [b"a"], "close 后仍在刷 ⇒ 定时器没掐掉")

    def test_flush_is_idempotent(self):
        """重复 flush 不许刷出空帧（空帧会让前端收到一帧没意义的 WS 消息）。"""
        async def main():
            out = []
            c = term._OutputCoalescer(out.append, delay_ms=30)
            c.flush()
            c.flush()
            c.handle(b"a")
            c.flush()
            c.flush()
            return out
        self.assertEqual(_run(main()), [b"a"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
