"""A：产物落地校验（2026-11-28 用户令）——治「回了一句就过」的假绿。

判据全部落在**可证伪的量**上：文件存在性、字节数、mtime 与任务开始时间的关系。
不判「内容对不对」（那需要每个任务的语义判据，不可能通用）。
"""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path

import artifact_verify as av


class TestNormalize(unittest.TestCase):
    def test_json_text_list_and_none(self):
        self.assertEqual(av.normalize('["a.py","b.py"]'), ["a.py", "b.py"])
        self.assertEqual(av.normalize(["a.py", " a.py ", ""]), ["a.py"])
        self.assertEqual(av.normalize(None), [])
        self.assertEqual(av.normalize(""), [])
        self.assertEqual(av.normalize("single.py"), ["single.py"], "老行可能是裸路径")

    def test_bad_json_does_not_raise(self):
        self.assertEqual(av.normalize("[not json"), ["[not json"])


class TestWriteTask(unittest.TestCase):
    def test_write_hint(self):
        for p in ("把文本写入 /tmp/a.txt", "创建 src/x.py", "delete the stale file",
                  "修改这个函数"):
            self.assertTrue(av.is_write_task(p), p)
        for p in ("读一下 src/term.py 说明它做什么", "summarize the README",
                  "核对 tests/README.md 的计数"):
            self.assertFalse(av.is_write_task(p), p)


class TestCheck(unittest.TestCase):
    def setUp(self):
        self.td = tempfile.TemporaryDirectory()
        self.root = Path(self.td.name)

    def tearDown(self):
        self.td.cleanup()

    def _write(self, rel, body=b"x"):
        p = self.root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(body)
        return p

    def test_no_files_is_insufficient_not_pass(self):
        v = av.check(None, cwd=self.root)
        self.assertEqual(v["status"], av.STATUS_INSUFFICIENT)
        self.assertIn("no_files", [g["id"] for g in v["gaps"]])

    def test_missing_file_fails_repairable(self):
        v = av.check(["out.txt"], cwd=self.root, prompt="写入 out.txt")
        self.assertEqual(v["status"], av.STATUS_FAIL)
        self.assertTrue(v["repairable"], "缺文件属可修复 ⇒ 必须允许重试")
        self.assertIn("missing", [g["id"] for g in v["gaps"]])

    def test_empty_file_fails(self):
        self._write("empty.txt", b"")
        v = av.check(["empty.txt"], cwd=self.root)
        self.assertEqual(v["status"], av.STATUS_FAIL)
        self.assertIn("empty", [g["id"] for g in v["gaps"]])

    def test_write_task_requires_a_real_modification(self):
        p = self._write("old.txt", b"content")
        old = time.time() - 3600
        os.utime(p, (old, old))
        v = av.check(["old.txt"], cwd=self.root, since=time.time() - 60,
                     prompt="把内容写入 old.txt")
        self.assertEqual(v["status"], av.STATUS_FAIL)
        self.assertIn("not_modified", [g["id"] for g in v["gaps"]])

    def test_write_task_passes_when_file_touched_during_task(self):
        self._write("new.txt", b"fresh")
        v = av.check(["new.txt"], cwd=self.root, since=time.time() - 30,
                     prompt="写入 new.txt")
        self.assertEqual(v["status"], av.STATUS_PASS)
        self.assertFalse(v["repairable"])

    def test_read_task_does_not_require_modification(self):
        p = self._write("readme.md", b"doc")
        old = time.time() - 3600
        os.utime(p, (old, old))
        v = av.check(["readme.md"], cwd=self.root, since=time.time() - 60,
                     prompt="读一下 readme.md 并总结")
        self.assertEqual(v["status"], av.STATUS_PASS,
                         "只读子任务声明的是读目标，一律要求改动会假红")

    def test_escape_path_is_insufficient(self):
        v = av.check(["../../etc/passwd"], cwd=self.root, prompt="写入 x")
        self.assertEqual(v["status"], av.STATUS_INSUFFICIENT)
        self.assertIn("escape", [g["id"] for g in v["gaps"]])

    def test_directory_counts_as_present(self):
        (self.root / "d").mkdir()
        v = av.check(["d"], cwd=self.root)
        self.assertEqual(v["status"], av.STATUS_PASS)


class TestSwitch(unittest.TestCase):
    def test_default_on_and_off_switch(self):
        old = os.environ.get("HUB_VERIFY_ARTIFACTS")
        try:
            os.environ.pop("HUB_VERIFY_ARTIFACTS", None)
            self.assertTrue(av.enabled(), "默认必须开——否则等于没做这一层")
            os.environ["HUB_VERIFY_ARTIFACTS"] = "off"
            self.assertFalse(av.enabled())
        finally:
            if old is None:
                os.environ.pop("HUB_VERIFY_ARTIFACTS", None)
            else:
                os.environ["HUB_VERIFY_ARTIFACTS"] = old


class TestVerdictShape(unittest.TestCase):
    def test_shape_matches_verifier(self):
        v = av.check(None)
        for k in ("status", "kind", "gaps", "repairable", "detail"):
            self.assertIn(k, v)
        self.assertEqual(v["kind"], "artifacts")


if __name__ == "__main__":
    unittest.main()