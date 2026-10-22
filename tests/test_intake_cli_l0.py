#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L1 host：`scripts/intake.sh` 的 `--json --quiet` 必须真的输出 JSON。

## 实测缺陷（2026-10-22）

```
$ bash scripts/intake.sh "…" --no-escalate --json --quiet | wc -c
0            ← 退出码 0
```

而脚本头部注释写的是「`--json --quiet` **只输出 JSON**，不落盘」。
实现里把两件事写在同一层 `if os.environ.get("QUIET") != "1"` 里 ⇒ 两个开关一起用就
什么也不输出，且 **rc=0**。这是本仓最忌的「假绿」形态：调用方拿到空串 + 成功码，
重试/解析都会静默失败。

## 为什么这条是 L1 而不是 L0

被测对象 `scripts/intake.sh` 在**技术文档仓**（`/fs/1000/ftp/技术文档/scripts/`），
不在本仓 ⇒ 干净 runner 上不存在。按 `tests/README.md` 的分层口径，读本机真实资产
就是 L1。`scripts/prepush.sh` 会跑 `run_tests.sh all`（含 L1），所以它仍然有闸门。

## 测法

**不重写一份判据，而是把脚本尾部那段真 python 抽出来执行**（脚本是 shell + heredoc python）。
只喂 `RESP/OUT/JSON_ONLY/QUIET` 四个环境变量，零网络、零 hub 依赖。
这样「注释里说会输出」与「代码里真的会输出」才分得开 —— 首版若只看 `assertNotIn("QUIET")`
之类，会在代码改成 `if QUIET == "0"` 时仍然全绿（本仓已有多次同型教训）。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))

import tiers  # noqa: E402

SCRIPT = pathlib.Path("/fs/1000/ftp/技术文档/scripts/intake.sh")


def _tail_python() -> str:
    """抽出脚本末尾 `RESP=… python3 - <<'PY' … PY` 那段 python 源码。"""
    src = SCRIPT.read_text(encoding="utf-8")
    lines = src.splitlines()
    start = None
    for i, ln in enumerate(lines):
        if "<<'PY'" in ln and "RESP=" in ln:
            start = i + 1
            break
    if start is None:
        raise AssertionError("未找到脚本尾部的 heredoc python 块（脚本结构变了？）")
    end = start
    while end < len(lines) and lines[end].strip() != "PY":
        end += 1
    return "\n".join(lines[start:end]) + "\n"


@tiers.host_only
class TestIntakeCliJsonQuiet(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        assert SCRIPT.exists(), "找不到 %s" % SCRIPT
        cls.src = _tail_python()
        cls.tmpdir = tempfile.mkdtemp(prefix="intakecli-")
        cls.py = os.path.join(cls.tmpdir, "tail.py")
        with open(cls.py, "w", encoding="utf-8") as f:
            f.write(cls.src)

    def _run(self, payload, json_only, quiet, out=""):
        env = dict(os.environ)
        env.update({"RESP": json.dumps(payload, ensure_ascii=False),
                    "OUT": out, "JSON_ONLY": str(json_only), "QUIET": str(quiet)})
        p = subprocess.run([sys.executable, self.py], env=env,
                           capture_output=True, text=True, timeout=30)
        return p.returncode, p.stdout, p.stderr

    PAYLOAD = {"brief": "# 简报复核\n正文", "plan": {"verdict": "thin"}}

    def test_json_quiet_prints_json(self):
        """核心回归：`--json --quiet` 必须有可解析的 JSON（旧实现输出 0 字节）。"""
        rc, out, err = self._run(self.PAYLOAD, json_only=1, quiet=1)
        self.assertEqual(rc, 0, err)
        self.assertTrue(out.strip(), "`--json --quiet` 仍然零输出（rc=0 假绿）")
        d = json.loads(out)          # 解析失败即使用例失败
        self.assertEqual(d["plan"]["verdict"], "thin")

    def test_json_alone_still_prints_json(self):
        rc, out, _ = self._run(self.PAYLOAD, json_only=1, quiet=0)
        self.assertEqual(rc, 0)
        self.assertEqual(json.loads(out)["brief"], "# 简报复核\n正文")

    def test_quiet_alone_prints_nothing(self):
        """`--quiet` 单独用仍应是安静的（它压的是人读输出，不是 JSON 契约）。"""
        rc, out, _ = self._run(self.PAYLOAD, json_only=0, quiet=1)
        self.assertEqual(rc, 0)
        self.assertEqual(out.strip(), "")

    def test_default_prints_brief(self):
        rc, out, _ = self._run(self.PAYLOAD, json_only=0, quiet=0)
        self.assertEqual(rc, 0)
        self.assertIn("简报复核", out)

    def test_json_mode_never_claims_disk_write(self):
        """`--json` 不落盘：即便 OUT 被给了，也不该打「已落盘」并写文件。"""
        target = os.path.join(self.tmpdir, "should-not-exist.md")
        rc, out, _ = self._run(self.PAYLOAD, json_only=1, quiet=0, out=target)
        self.assertEqual(rc, 0)
        self.assertNotIn("已落盘", out)
        self.assertFalse(os.path.exists(target), "--json 模式写了盘（header 说「不落盘」）")

    def test_bad_payload_fails_loudly(self):
        """响应不是 JSON ⇒ 退出码 3 + stderr 说明，不许静默当成功。"""
        env = dict(os.environ)
        env.update({"RESP": "not-json", "OUT": "", "JSON_ONLY": "1", "QUIET": "1"})
        p = subprocess.run([sys.executable, self.py], env=env,
                           capture_output=True, text=True, timeout=30)
        self.assertEqual(p.returncode, 3)
        self.assertIn("FAIL", p.stderr)


if __name__ == "__main__":
    unittest.main()
