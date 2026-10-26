"""L0：`term.child_env()` 必须把 nvm node bin 前置进子进程 PATH。

钉这条的原因（2026-09-27 实测，不是假想）：用户选 pi 起会话 ⇒ 终端一屏 JS 堆栈，
真因是 `pi` 的 shebang 为 `#!/usr/bin/env node`，而 hub 服务进程 PATH 无 nvm ⇒
内核把系统 node **v20.20.2** 交给 pi v0.85.1，后者 bundle 用了 `node:fs` 的
`globSync`（Node 22+）⇒ `SyntaxError: The requested module 'node:fs' does not
provide an export named 'globSync'`。

`profiles.which()` 的 nvm 兜底只保证 hub 找得到 pi 这个**文件**，管不到 pi 起来
之后自己再找**解释器**——所以必须在子进程环境层面补，两件事缺一不可。

2026-10-24 同族第二例：omp（oh-my-pi）shebang 为 `#!/usr/bin/env bun` ⇒
`~/.bun/bin`（1.3.14）同样前置，排在 nvm 之后。omp 卡另有 /bin/sh 包装脚本
双保险（见 profiles.py 里 omp 卡注释），两条路互为冗余。

本文件属 L0 hermetic：不真 fork pty、不起服务、零网络；用假 nvm/假 bun 目录
断言 PATH 顺序。
"""
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import profiles                                       # noqa: E402
import term                                           # noqa: E402


class TestChildEnvPrependsNvm(unittest.TestCase):
    def setUp(self):
        self.fake = ["/home/someone/.nvm/versions/node/v24.18.0/bin",
                     "/home/someone/.nvm/versions/node/v22.9.0/bin"]
        self.fake_bun = Path("/home/someone/.bun/bin")
        self.patcher = mock.patch.object(profiles, "_nvm_bins",
                                         return_value=[Path(p) for p in self.fake])
        self.patcher.start()
        # 2026-10-24：bun 目录与 nvm 同构 patch 掉 —— 否则真机上有/没有 ~/.bun/bin
        # 会让本文件在不同机器上跑出不同结果（L0 hermetic 要求可复现）。
        self.bun_patcher = mock.patch.object(profiles, "_bun_bin",
                                             return_value=self.fake_bun)
        self.bun_patcher.start()
        self._saved_path = os.environ["PATH"]
        os.environ["PATH"] = "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"

    def tearDown(self):
        self.patcher.stop()
        self.bun_patcher.stop()
        os.environ["PATH"] = self._saved_path

    def test_nvm_dirs_come_first(self):
        env = term.child_env()
        self.assertEqual(env["PATH"].split(os.pathsep)[:2], self.fake)
        # 系统目录仍在，只是排在后面 —— 不是替换 PATH
        self.assertIn("/usr/bin", env["PATH"])

    def test_newer_node_wins(self):
        self.assertEqual(term.child_env()["PATH"].split(os.pathsep)[0], self.fake[0])

    def test_bun_dir_comes_after_nvm(self):
        """bun 目录排 nvm 之后、系统目录之前（2026-10-24 加，omp 的解释器）。

        顺序有意义：nvm node bin 与 bun bin 服务不同 CLI，但都必须早于系统目录 ——
        系统 node v20 / 系统无 bun 正是当年 pi 与今天 omp 两个「本机跑没事、
        hub 里必崩」的根因。"""
        env = term.child_env()
        self.assertEqual(env["PATH"].split(os.pathsep),
                         self.fake + [str(self.fake_bun),
                                      "/usr/local/sbin", "/usr/local/bin",
                                      "/usr/sbin", "/usr/bin", "/sbin", "/bin"])

    def test_term_and_colorterm(self):
        env = term.child_env()
        self.assertEqual(env["TERM"], "xterm-256color")
        self.assertEqual(env["COLORTERM"], "truecolor")

    def test_omp_skip_setup(self):
        """omp 嵌入式终端跳过 setup wizard：否则每次新建会话都会弹 provider/model 选择界面。"""
        env = term.child_env()
        self.assertEqual(env.get("OMP_SKIP_SETUP"), "1")

    def test_no_nvm_means_untouched_path(self):
        """没有 nvm **也没有 bun** 的机器上不得凭空造目录（PATH 里不能出现空串段）"""
        self.patcher.stop()
        self.bun_patcher.stop()
        try:
            with mock.patch.object(profiles, "_nvm_bins", return_value=[]), \
                 mock.patch.object(profiles, "_bun_bin", return_value=None):
                env = term.child_env()
            self.assertEqual(env["PATH"], os.environ["PATH"])
            self.assertEqual(env["PATH"].split(os.pathsep)[0], "/usr/local/sbin")
        finally:
            self.patcher.start()
            self.bun_patcher.start()

    def test_no_nvm_but_bun_still_prepended(self):
        """无 nvm 有 bun：只前置 bun（不造空串段）—— 反过来钉上条的边界"""
        self.patcher.stop()
        try:
            with mock.patch.object(profiles, "_nvm_bins", return_value=[]):
                env = term.child_env()
            self.assertEqual(env["PATH"].split(os.pathsep)[0], str(self.fake_bun))
            self.assertIn("/usr/bin", env["PATH"])
        finally:
            self.patcher.start()

    def test_base_env_not_mutated(self):
        term.child_env()
        self.assertEqual(os.environ["PATH"],
                         "/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin")


if __name__ == "__main__":
    unittest.main()
