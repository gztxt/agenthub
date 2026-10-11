#!/usr/bin/env python3
"""L0 · `scripts/run_changed.py` 的选测纯函数闸门（2026-10-12）。

为什么值得有（该工具是**开发迭代用**，但它自己错起来是安静的）：
  · 选多了 ⇒ 只是慢，无所谓；
  · **选漏了** ⇒ 开发者以为"子集绿=没坏"，而漏掉的那条恰恰是承重闸门。
    与 `test_tier_collector_parity` 防的是同一个病：**代码存在 ≠ 会被执行**。
  故这里钉两条性质：① 命中按"测试正文提及模块"判定；② **映射不到必须显式报出**
  （`unmapped` 非空），绝不静默当"没有测试要跑"。

不导入 src（导入 src 会触发 lifespan/真库，L0 不许），只 import 那个纯函数。
"""
import importlib.util
import sys
import unittest
from pathlib import Path

_SCRIPTS = Path(__file__).resolve().parents[1] / "scripts"


def _load():
    spec = importlib.util.spec_from_file_location("run_changed", _SCRIPTS / "run_changed.py")
    mod = importlib.util.module_from_spec(spec)
    sys.modules["run_changed"] = mod
    spec.loader.exec_module(mod)
    return mod


run_changed = _load()


class TestSelect(unittest.TestCase):
    INDEX = {
        "test_a.py": "import db\n\ndef test_x():\n    pass\n",
        "test_b.py": "from memory import l1\n",
        "test_c.py": "const s = 'hub.js'\n",
        "test_none.py": "import json\n",
    }

    def test_src_module_hits_tests_that_mention_it(self):
        picked, unmapped = run_changed.select(["src/db.py"], self.INDEX)
        self.assertEqual(picked, ["test_a.py"])
        self.assertEqual(unmapped, [])

    def test_from_import_form_is_hit(self):
        picked, _ = run_changed.select(["src/memory.py"], self.INDEX)
        self.assertIn("test_b.py", picked)

    def test_frontend_file_matched_by_name_in_body(self):
        picked, _ = run_changed.select(["static/hub.js"], self.INDEX)
        self.assertEqual(picked, ["test_c.py"])

    def test_test_file_selects_itself(self):
        picked, unmapped = run_changed.select(["tests/test_a.py"], self.INDEX)
        self.assertEqual(picked, ["test_a.py"])
        self.assertEqual(unmapped, [])

    def test_unmapped_is_reported_never_silently_dropped(self):
        """★ 本文件的核心判据：映射不到必须报出来（子集不完整要可见）。"""
        picked, unmapped = run_changed.select(["docs/README.md", "src/nope.py"], self.INDEX)
        self.assertEqual(picked, [])
        self.assertEqual(sorted(unmapped), ["docs/README.md", "src/nope.py"])

    def test_no_false_positive_from_substring(self):
        """`db` 不该命中 `dbx`（用 \\b 边界，否则映射会假宽）。"""
        idx = {"test_z.py": "import dbx\n"}
        picked, unmapped = run_changed.select(["src/db.py"], idx)
        self.assertEqual(picked, [])
        self.assertEqual(unmapped, ["src/db.py"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
