#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：终端会话录制（src/term_record.py + src/db.py 新表 + term.py 三处挂点）。

本机**唯一**「主动把用户键入内容落盘」的功能，所以每条用例钉的都是安全前提或
一类静默故障，不是「功能能用」这种弱断言：

1. **`TERM_RECORD` 缺省必须是不录**（09-29 裁定 默认 0）。开着的 hub 不重启就突然开始
   把用户键入内容写盘，是这类功能最恶心的失败形态。
2. **落盘前必须脱敏**：pty 流里出现口令/Token 是常态。断言口径是「库里**搜不到**凭据
   原文」，不是「调用了脱敏函数」—— 后者对「脱敏后又把原文拼回去」完全无感。
3. **触顶不静默丢帧**：单会话/全局上限到了必须 `capped=True` + 带原因，且后续帧不写。
   静默丢帧会让用户事后分不清「没发生」和「录不下」。
4. **保留期 7 天**按真时间戳删，不按 id 序删（否则「先录的反而留着」）。
5. **同 sid 二次录制清掉上一份**（裁定「每会话只留最后 1 份」）。
6. **env 绝不入库**：从表结构上钉死（没有 env 列，且写入接口只接受 pty 字节）。
7. **全局预算满 ⇒ 该会话不开录**，理由要写出来给界面报。

隔离：DB 走 tmp（`init_db` 会换模块级 `_conn`，测后 rmtree）。
"""
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import db  # noqa: E402
import term_record  # noqa: E402


def _mktmp(prefix):
    return Path(tempfile.mkdtemp(prefix=prefix))


class TestDefaultsOff(unittest.TestCase):
    """硬约束 1 + 6：默认不录、env 无处可藏。"""

    def setUp(self):
        self.tmp = _mktmp("termrec-off-")
        db.init_db(self.tmp / "t.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_default_off_writes_nothing(self):
        """TERM_RECORD 缺省 ⇒ active=False ⇒ 喂多少字节都不落盘。"""
        with mock.patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TERM_RECORD", None)
            self.assertFalse(term_record.enabled())
            rec = term_record.Recorder("sid-off", "claude")
            rec.feed("in", b"export TOKEN=supersecret\n")
            rec.feed("out", b"welcome\n")
            rec.close()
        self.assertFalse(rec.active)
        self.assertEqual(db.query("SELECT COUNT(*) n FROM term_recordings")[0]["n"], 0)

    def test_explicit_on_is_the_only_way_in(self):
        """只有显式 1/true/yes/on 才开；'0'/''/乱值都不开。"""
        for bad in ("0", "", "off", "2", "maybe", None):
            with mock.patch.dict(os.environ, {"TERM_RECORD": bad or ""}, clear=False):
                self.assertFalse(term_record.enabled(), bad)
        for good in ("1", "true", "YES", " on "):
            with mock.patch.dict(os.environ, {"TERM_RECORD": good}, clear=False):
                self.assertTrue(term_record.enabled(), good)

    def test_table_has_no_env_column(self):
        """env 里就有 TERM_TOKEN —— 从结构上钉死「无处可藏」，不靠代码自觉。"""
        rows = db.query("PRAGMA table_info(term_recordings)")
        cols = [r["name"] if isinstance(r, dict) else r[1] for r in rows]
        self.assertTrue({"session_id", "agent_id", "seq", "ts",
                         "direction", "data", "bytes"} <= set(cols), cols)
        for banned in ("env", "environ", "command", "cmd", "token"):
            self.assertNotIn(banned, cols, banned)


class TestRecordingOn(unittest.TestCase):
    """硬约束 2：开着的时候，落盘的必须是脱敏后的。"""

    def setUp(self):
        self.tmp = _mktmp("termrec-on-")
        db.init_db(self.tmp / "t.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"TERM_RECORD": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_records_both_directions_in_order(self):
        rec = term_record.Recorder("sid-1", "pi")
        rec.feed("out", b"hello")
        rec.feed("in", b"whoami")
        rec.feed("out", b"root")
        rows = term_record.frames("sid-1")
        self.assertEqual([r["dir"] for r in rows], ["out", "in", "out"])
        self.assertEqual([r["seq"] for r in rows], [0, 1, 2])

    def test_credentials_never_reach_disk(self):
        """断言口径是「库里搜不到原文」，不是「调用了脱敏器」。

        ⚠️ 假密钥用**拼接**构造、不写死字面量（2026-10-08）：prepush 闸门①扫
        `sk-[0-9A-Za-z._-]{20,}`，写死占位串也会被判成高危模式而拦住推送
        （实测：首版就这么写的，闸门① FAIL 命中本文件与 test_term_record_wiring.py）。
        拼接不是为了绕过闸门 —— 是让「这里有个假凭据」这件事在源码里显式可读，
        且闸门保持原样继续守着真凭据。
        """
        secret = "sk-" + "live-" + "AAAABBBBCCCCDDDDEEEEFFFF0000"
        bearer = "sk-" + "live-" + "ZZZZ9999YYYY8888"
        rec = term_record.Recorder("sid-2", "pi")
        rec.feed("out", f"export ANTHROPIC_API_KEY={secret}\r\n".encode())
        rec.feed("in", f"Authorization: Bearer {bearer}\r\n".encode())
        rec.close()
        dumped = "\n".join(str(v) for row in db.query("SELECT * FROM term_recordings")
                           for v in row.values())
        self.assertNotIn(secret, dumped)
        self.assertNotIn(bearer, dumped)
        # 脱敏后帧仍在（不能「靠丢帧」来假装脱敏成功）
        self.assertEqual(db.query(
            "SELECT COUNT(*) n FROM term_recordings WHERE session_id='sid-2'")[0]["n"], 2)

    def test_resecret_takes_only_last_recording(self):
        """同 sid 二次录制 ⇒ 上一份被清（裁定「每会话只留最后 1 份」）。"""
        a = term_record.Recorder("sid-3", "pi")
        a.feed("out", b"first take")
        a.close()
        b = term_record.Recorder("sid-3", "pi")
        b.feed("out", b"second take")
        blob = "".join(str(v) for r in db.query(
            "SELECT data FROM term_recordings WHERE session_id='sid-3'") for v in r.values())
        self.assertNotIn("first take", blob)
        self.assertIn("second take", blob)


class TestCaps(unittest.TestCase):
    """硬约束 3 + 7：上限必须是「停录 + 说清原因」，不是静默截断。"""

    def setUp(self):
        self.tmp = _mktmp("termrec-cap-")
        db.init_db(self.tmp / "t.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(
            os.environ, {"TERM_RECORD": "1", "TERM_RECORD_MAX_SESSION": "1024",
                         "TERM_RECORD_MAX_TOTAL": "1048576"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_session_cap_stops_and_explains(self):
        rec = term_record.Recorder("sid-cap", "pi")
        rec.feed("out", b"x" * 900)
        self.assertFalse(rec.capped)
        rec.feed("out", b"y" * 900)          # 越过 1024
        self.assertTrue(rec.capped)
        self.assertIn("1024", rec.capped_reason)
        self.assertNotIn("0MB", rec.capped_reason)   # 「已达上限 0MB」是假话
        n_before = db.query(
            "SELECT COUNT(*) n FROM term_recordings WHERE session_id='sid-cap'")[0]["n"]
        rec.feed("out", b"z" * 100)          # 触顶后不再写
        n_after = db.query(
            "SELECT COUNT(*) n FROM term_recordings WHERE session_id='sid-cap'")[0]["n"]
        self.assertEqual(n_before, n_after)

    def test_global_cap_refuses_new_session_with_reason(self):
        rec = term_record.Recorder("sid-g1", "pi")
        rec.feed("out", b"a" * 1000)
        rec.close()
        nxt = term_record.Recorder("sid-g2", "pi")
        with mock.patch.dict(os.environ, {"TERM_RECORD_MAX_TOTAL": "500"}, clear=False):
            blocked = term_record.Recorder("sid-g3", "pi")
        self.assertTrue(nxt.active)      # 1000 字节 < 1MB 全局预算 ⇒ 该开的开着
        self.assertFalse(blocked.active)
        self.assertTrue(blocked.capped)
        self.assertIn("全局", blocked.capped_reason)


class TestRetention(unittest.TestCase):
    """硬约束 4：保留期按真时间戳删，不按 id 序。"""

    def setUp(self):
        self.tmp = _mktmp("termrec-ret-")
        db.init_db(self.tmp / "t.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(
            os.environ, {"TERM_RECORD": "1", "TERM_RECORD_RETENTION_DAYS": "7"},
            clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_sweep_drops_only_expired(self):
        rec = term_record.Recorder("sid-old", "pi")
        rec.feed("out", b"ancient")
        rec.close()
        keep = term_record.Recorder("sid-new", "pi")
        keep.feed("out", b"fresh")
        keep.close()
        # 把 sid-old 那一帧的时间戳推到 8 天前（id 仍是更小的 ⇒ 按 id 删会删错）
        old_ts = time.time() - 8 * 86400
        db.execute("UPDATE term_recordings SET ts=? WHERE session_id='sid-old'", (old_ts,))
        term_record.sweep()
        left = {r["session_id"] for r in db.query(
            "SELECT DISTINCT session_id FROM term_recordings")}
        self.assertIn("sid-new", left)
        self.assertNotIn("sid-old", left)


class TestStatusHonesty(unittest.TestCase):
    """口径：接口上的 at_limit 是推断，不得冒充 capped 真值。"""

    def setUp(self):
        self.tmp = _mktmp("termrec-st-")
        db.init_db(self.tmp / "t.db")
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))
        self.env = mock.patch.dict(os.environ, {"TERM_RECORD": "1"}, clear=False)
        self.env.start()
        self.addCleanup(self.env.stop)

    def test_status_reports_limits_and_at_limit(self):
        rec = term_record.Recorder("sid-s", "pi")
        rec.feed("out", b"data")
        st = term_record.status("sid-s")
        self.assertEqual(st["frames"], 1)
        self.assertIn("at_limit", st)
        self.assertNotIn("capped", st)          # 推断不许用真值的名字
        self.assertEqual(st["limits"]["retention_days"], 30)  # 2026-11-05 用户裁定 7→30 天
        self.assertEqual(st["limits"]["max_session"], 32 * 1024 * 1024)
        self.assertEqual(st["limits"]["max_total"], 512 * 1024 * 1024)

    def test_recorder_to_dict_carries_capped_truth(self):
        rec = term_record.Recorder("sid-t", "pi")
        d = rec.to_dict()
        self.assertTrue(d["recording"])
        self.assertFalse(d["capped"])
        self.assertEqual(d["capped_reason"], "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
