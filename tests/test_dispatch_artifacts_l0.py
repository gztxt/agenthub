"""派发产物落点守卫 L0（PT-20261127-02 / v0.13.223）。

判据三层，每层都要**可断言**（口头约定不算）：
  1. 环境层：子进程 env 的 `TMPDIR/TMP/TEMP` 指向 `work/tmp/<run>/<task>`；
  2. 文本层：派发 prompt 末尾带绝对路径契约（`[产物落点]`）；
  3. 事后层：`/tmp` 顶层新增里，含 run/task 标识的**搬走**，其它**只记账**。

反例（2026-11-27 实测）：agent 把 `/tmp/magpie_models_raw.json` 当产物，重启即失；
当时只有 prompt 里一句「落 work/」 —— 那是声明，没有任何一层能拦住。
"""
import asyncio
import os
import sys
import tempfile
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import dispatch_artifacts as art  # noqa: E402
from adapters import cli_agent  # noqa: E402


class TestScopeAndEnv(unittest.TestCase):
    def test_scope_from_dispatch_tag(self):
        self.assertEqual(("143d8aa483", "t1"),
                         art.scope_from({"tag": "task-143d8aa483-t1"}))
        self.assertEqual(("1a61a07531", "t_sum"),
                         art.scope_from({"tag": "task-1a61a07531-t_sum"}))
        self.assertEqual(("", ""), art.scope_from({}),
                         "交互式终端调用不带 tag ⇒ 守卫必须不启用（行为零变化）")

    def test_scope_explicit_wins(self):
        self.assertEqual(("r1", "t9"),
                         art.scope_from({"run_id": "r1", "task_id": "t9",
                                         "tag": "task-other-t1"}))

    def test_env_points_into_work_and_creates(self):
        e = art.env("runZ", "t2")
        tmp = e["TMPDIR"]
        self.assertIn(os.path.join("work", "tmp", "runZ", "t2"), tmp)
        self.assertTrue(tmp.startswith(art.ROOT + os.sep))
        self.assertTrue(os.path.isdir(tmp), "TMPDIR 必须真的存在，否则工具会回退到 /tmp")
        self.assertTrue(os.path.isdir(e["HUB_TASK_ARTIFACT_DIR"]))
        for k in ("TMP", "TEMP"):
            self.assertEqual(tmp, e[k])

    def test_contract_note_has_absolute_paths(self):
        note = art.contract_note("r1", "t1")
        self.assertIn("[产物落点]", note)
        self.assertIn("work/artifacts/r1/t1", note)
        self.assertIn("/tmp", note, "契约必须点名禁止项本身")
        self.assertIn("r1-t1-", note, "契约要给可归因的文件名前缀（事后层靠它判归属）")


class TestSweep(unittest.TestCase):
    def setUp(self):
        self.d = tempfile.mkdtemp(prefix="tmpguard-")
        self.addCleanup(lambda: __import__("shutil").rmtree(self.d, ignore_errors=True))

    def _touch(self, name: str, age_s: float = 0.0) -> str:
        p = os.path.join(self.d, name)
        Path(p).write_text("x", encoding="utf-8")
        ts = time.time() - age_s
        os.utime(p, (ts, ts))
        return p

    def test_moves_task_owned_file(self):
        p = self._touch("magpie_models_runABC_t1.json")
        out = art.sweep_run("runABC", "t1", before=set(), since=time.time() - 1,
                            path=self.d)
        actions = {x["name"]: x["action"] for x in out}
        self.assertEqual("moved", actions.get("magpie_models_runABC_t1.json"))
        self.assertFalse(os.path.exists(p), "归属本任务的 /tmp 副本必须真被搬走")
        moved_to = art.task_artifacts("runABC", "t1")
        self.assertTrue(os.path.isfile(os.path.join(moved_to, "magpie_models_runABC_t1.json")))
        # 用例产物不留仓（work/ 虽被 gitignore，也不该越跑越多）
        __import__("shutil").rmtree(os.path.join(art.WORK, "artifacts", "runABC"),
                                    ignore_errors=True)
        __import__("shutil").rmtree(os.path.join(art.WORK, "tmp", "runABC"),
                                    ignore_errors=True)

    def test_task_id_alone_is_not_ownership(self):
        """`t1` 这种短标识满 /tmp 都是（pytest-1 / test1），不得据此搬别人的文件。"""
        p = self._touch("t1-scratch.txt")
        out = art.sweep_run("runABC", "t1", before=set(), since=time.time() - 1,
                            path=self.d)
        self.assertEqual("flagged", out[0]["action"])
        self.assertTrue(os.path.exists(p))

    def test_flags_unowned_but_does_not_move(self):
        p = self._touch("someone_else.txt")
        out = art.sweep_run("runABC", "t1", before=set(), since=time.time() - 1,
                            path=self.d)
        self.assertEqual("flagged", out[0]["action"])
        self.assertTrue(os.path.exists(p), "别人的文件不许搬")

    def test_preexisting_and_old_files_ignored(self):
        kept = self._touch("old.log", age_s=3600)
        pre = self._touch("preexisting_runABC_t1.txt")
        out = art.sweep_run("runABC", "t1", before={"preexisting_runABC_t1.txt"},
                            since=time.time() - 1, path=self.d)
        self.assertEqual([], out)
        self.assertTrue(os.path.exists(kept) and os.path.exists(pre))

    def test_directories_ignored(self):
        os.makedirs(os.path.join(self.d, "runABC-t1-dir"))
        out = art.sweep_run("runABC", "t1", before=set(), since=time.time() - 1,
                            path=self.d)
        self.assertEqual([], out, "只搬普通文件，目录不动")

    def test_scans_when_tmp_is_large(self):
        """3k+ 项时仍要扫到目标：`names[:N]` 截断曾让真实产物漏搬（2026-11-27 实测）。"""
        for i in range(260):
            self._touch("filler-%03d.txt" % i, age_s=3600)
        target = self._touch("runBIG-t1-probe.txt")
        out = art.sweep_run("runBIG", "t1", before=set(), since=time.time() - 1,
                            path=self.d)
        self.assertTrue(any(x["name"] == "runBIG-t1-probe.txt" and x["action"] == "moved"
                            for x in out), out)
        self.assertFalse(os.path.exists(target))
        __import__("shutil").rmtree(os.path.join(art.WORK, "artifacts", "runBIG"),
                                    ignore_errors=True)

    def test_missing_path_never_raises(self):
        self.assertEqual([], art.sweep_run("r", "t", path="/no/such/dir-for-tmpguard"))


class _FakeProc:
    def __init__(self, stdout: bytes, rc: int = 0):
        self.pid = os.getpid()
        self.returncode = rc
        self._out = stdout

    async def communicate(self):
        return self._out, b""

    async def wait(self):
        return self.returncode


class TestAdapterWiring(unittest.TestCase):
    """真接线：派发调用必须带上 TMPDIR 与落点契约；无 tag 时逐字不变。"""

    def setUp(self):
        self._saved = asyncio.create_subprocess_exec
        self._saved_bin = None
        self.seen = {}
        a = cli_agent.CliAgentAdapter({}, "codex")

        async def fake_exec(*argv, **kw):
            self.seen["argv"] = list(argv)
            self.seen["env"] = dict(kw.get("env") or {})
            return _FakeProc(b'{"type":"item.completed","item":{"type":"agent_message","text":"ok"}}')

        asyncio.create_subprocess_exec = fake_exec
        self.adapter = a

    def tearDown(self):
        asyncio.create_subprocess_exec = self._saved

    def test_dispatch_call_gets_tmpdir_and_contract(self):
        r = asyncio.run(self.adapter.chat("写点东西", tag="task-runQ-t3"))
        self.assertTrue(r.get("success"), r.get("error"))
        self.assertIn(os.path.join("work", "tmp", "runQ", "t3"),
                      self.seen["env"].get("TMPDIR", ""))
        self.assertIn("[产物落点] ", " ".join(self.seen["argv"]))
        self.assertIn("work/artifacts/runQ/t3", " ".join(self.seen["argv"]))

    def test_plain_chat_env_untouched(self):
        asyncio.run(self.adapter.chat("普通聊天"))
        self.assertNotIn("TMPDIR", self.seen["env"],
                         "交互式聊天不许被注入派发用的 TMPDIR")
        self.assertNotIn("[产物落点]", " ".join(self.seen["argv"]))


def tearDownModule():
    """用例创建过的 work 子目录一律回收（work/ 虽 gitignore，接口目录不该越跑越多）。"""
    import shutil
    for sub in ("tmp", "artifacts"):
        for name in ("r1", "runZ", "runQ", "runABC", "t9", "t3"):
            shutil.rmtree(os.path.join(art.WORK, sub, name), ignore_errors=True)


if __name__ == "__main__":
    unittest.main()