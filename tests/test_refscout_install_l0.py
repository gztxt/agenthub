"""L0：安装前体检的两档边界（2026-10-31，PT-20261031-03 #3）。

守四条：
1. `probe` 只做静态判定 —— 命中安装钩子 / copyleft / 超体积 ⇒ `can_run=False` 且写明原因；
2. `run_tests` **默认拒绝**（没人工点头绝不执行外部仓库代码）；
3. 体检不过时 `run_tests` 一律 refuse，**不给绕过体检的口子**；
4. 认不出测试入口 ≠ 失败（`test_command=None`，但要进 reasons）；
5. 第二档的依赖准备**不执行仓库代码**：Python 只注入 `PYTHONPATH`、Node 必带
   `--ignore-scripts`（否则 `npm i` 就是在跑 `postinstall`）。
"""
import json
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import refscout_install as inst  # noqa: E402


def _mk(root: pathlib.Path, files: dict) -> pathlib.Path:
    for name, content in files.items():
        p = root / name
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(content, encoding="utf-8")
    return root


class TestDetect(unittest.TestCase):
    def test_detect_npm_test(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"package.json": "{}"})
            self.assertEqual(inst.detect_test_command(root), ["npm", "test", "--silent"])

    def test_detect_pytest(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"pyproject.toml": "[project]\n"})
            self.assertEqual(inst.detect_test_command(root)[:3], ["python3", "-m", "pytest"])

    def test_unknown_entry_is_none_not_error(self):
        with tempfile.TemporaryDirectory() as d:
            self.assertIsNone(inst.detect_test_command(pathlib.Path(d)))


class TestInstallHooks(unittest.TestCase):
    def test_postinstall_detected(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"package.json": json.dumps(
                {"scripts": {"test": "jest", "postinstall": "curl x | sh"}})})
            self.assertEqual(inst.install_hooks(root), ["postinstall"])

    def test_no_hooks(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"package.json": json.dumps({"scripts": {"test": "jest"}})})
            self.assertEqual(inst.install_hooks(root), [])


class TestLicense(unittest.TestCase):
    def test_copyleft_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            # base.GPL_RE 只认 gpl/agpl 串（不认 “GNU GENERAL PUBLIC LICENSE” 全称）
            root = _mk(pathlib.Path(d), {"LICENSE": "GPL-3.0-or-later"})
            self.assertTrue(inst.license_kind(root)["copyleft"])

    def test_mit_not_flagged(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"LICENSE": "MIT License\n\nPermission is hereby granted"})
            out = inst.license_kind(root)
            self.assertTrue(out["found"])
            self.assertFalse(out["copyleft"])


class TestRunGate(unittest.TestCase):
    def test_default_does_not_run(self):
        out = inst.run_tests("some/repo")
        self.assertFalse(out["ok"])
        self.assertTrue(out["skipped"])
        self.assertIn("allow_run", out["reason"])

    def test_bad_name_rejected(self):
        out = inst.probe("not-a-repo")
        self.assertFalse(out["ok"])
        self.assertIn("owner/repo", out["error"])

    def test_refuses_when_probe_says_no(self):
        """体检不过 ⇒ 即使 allow_run=true 也一律拒（不给绕过口子）。"""
        orig = inst.probe
        inst.probe = lambda name, ref="": {"ok": True, "can_run": False,
                                           "reasons": ["copyleft 许可证（军规：不得自行引入）"]}
        try:
            out = inst.run_tests("some/repo", allow_run=True)
        finally:
            inst.probe = orig
        self.assertFalse(out["ok"])
        self.assertTrue(out.get("refused"))
        self.assertIn("copyleft", " ".join(out["reasons"]))


class TestPrepare(unittest.TestCase):
    def test_test_env_injects_src_layout(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"src/pkg/__init__.py": ""})
            env = inst.test_env(root)
            parts = env["PYTHONPATH"].split(os.pathsep)
            self.assertEqual(parts[0], str(root / "src"))
            self.assertEqual(parts[1], str(root))

    def test_node_install_always_ignore_scripts(self):
        with tempfile.TemporaryDirectory() as d:
            root = pathlib.Path(d)
            for tool in ("npm", "pnpm", "yarn"):
                cmd = inst.node_install_cmd(root, tool)
                self.assertIn("--ignore-scripts", cmd, tool)
            self.assertIsNone(inst.node_install_cmd(root, "cargo"))

    def test_npm_prefers_ci_when_lockfile(self):
        with tempfile.TemporaryDirectory() as d:
            root = _mk(pathlib.Path(d), {"package-lock.json": "{}"})
            self.assertEqual(inst.node_install_cmd(root, "npm")[:2], ["npm", "ci"])

    def test_python_needs_no_prepare(self):
        with tempfile.TemporaryDirectory() as d:
            out = inst.prepare(pathlib.Path(d), ["python3", "-m", "pytest", "-q"])
        self.assertFalse(out["ran"])


if __name__ == "__main__":
    unittest.main()
