#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 守卫：**L0 的通过与否不得依赖仓内 `.env` 能否覆盖环境**。

【为什么要有这个文件】
`src/config.py` 的 `load_dotenv(env_path, override=True)` 会用仓内 `.env` 无条件
覆盖 `os.environ`，而 `TERM_TOKEN` **不在** `ENV_OVERRIDABLE` 白名单
（只有 DATA_DIR/LOG_DIR/PORT/HOST）。由此产生两条真规则：

1. `os.environ.setdefault("TERM_TOKEN", 假值)` 在本仓是**空操作** ——
   想让夹具生效必须用**无条件 `=`**，且要在所有 import 之后
   （写闸 `secrets_from_env()` 是**调用时**读 `os.getenv`）。
2. 凡是把 token 放进请求头的用例都必须**自钉凭据**，不能靠导入顺序侥幸。
   参照 `tests/test_prefs.py::_force_test_token()`：维护者 09-26 就撞过
   「同进程全量跑时其它用例改写 TERM_TOKEN ⇒ 401 假红」。

【为什么是行为判据而不是静态 lint】
2026-10-16 先写过一版静态检查（扫源码找「只有 setdefault 没有无条件赋值」的模块），
结果**误报 3 个**：`test_boundary`（合法地用 `term.TERM_TOKEN` 本身，判例 179 的修法）、
`test_ghsettings` / `test_github_projects`（字面量只出现在 setdefault 那一行，
压根没被使用）。静态判断「这个用例依不依赖环境 token」建模不了 ⇒ 改测**行为**。

【本文件守两条】
A1 **顺序无关**：`config` 先加载（= `.env` 已覆盖环境）时，
   token 相关的三个模块仍须全绿。这是判例 179 那类假红的真正防线。
A2 **`TERM_TOKEN` 不得进 `ENV_OVERRIDABLE`** —— 进去就意味着有人指望
   `setdefault` 能生效，而那会把两套来源搅在一起。

分层口径：纯 subprocess + 静态断言，不起服务不连网、零 SKIP。
子进程只跑三个 token 相关模块（秒级），不是全量套件。
"""
import pathlib
import subprocess
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import config as config_mod  # noqa: E402

#: 会真的把 token 放进请求头的模块（其余模块里的 setdefault 是未被使用的死代码）。
TOKEN_MODULES = (
    "tests/test_prefs.py",        # 用无条件赋值自钉（_force_test_token）
    "tests/test_boundary.py",     # 用 term.TERM_TOKEN 本身（判例 179 修法）
    "tests/test_github_projects.py",  # 桩自带 token，不依赖环境
)


class TestOrderIndependence(unittest.TestCase):
    """A1：`.env` 先加载时，token 相关用例仍须全绿。"""

    def test_token_modules_pass_with_env_loaded_first(self):
        """把 config 放在最前导入，模拟最坏导入顺序。"""
        py = _REPO / "venv" / "bin" / "python"
        if not py.exists():
            self.skipTest("venv 不存在")     # 换机场景；L0 零跳过闸门在别处兜
        code = (
            "import sys; sys.path.insert(0, 'src');"
            "import config;"
            "import pytest;"
            f"sys.exit(pytest.main({list(TOKEN_MODULES)!r} + "
            "['-q', '--no-header', '-p', 'no:cacheprovider']))"
        )
        proc = subprocess.run([str(py), "-c", code], cwd=str(_REPO),
                              capture_output=True, text=True, timeout=300)
        tail = (proc.stdout or "").strip().splitlines()[-3:]
        self.assertEqual(0, proc.returncode,
                         "`.env` 先加载时 token 用例转红 ⇒ 夹具靠导入顺序侥幸通过。\n"
                         "输出尾部：%s\n%s" % ("\n".join(tail), (proc.stderr or "")[-500:]))

    def test_same_modules_pass_in_isolation(self):
        """对照组：正常顺序（模块自己 import）也须绿 —— 两边都绿才说明真的无关。"""
        py = _REPO / "venv" / "bin" / "python"
        if not py.exists():
            self.skipTest("venv 不存在")
        proc = subprocess.run(
            [str(py), "-m", "pytest", *TOKEN_MODULES, "-q", "--no-header",
             "-p", "no:cacheprovider"],
            cwd=str(_REPO), capture_output=True, text=True, timeout=300)
        self.assertEqual(0, proc.returncode,
                         "隔离运行即红，与 `.env` 无关，是用例自身坏了：\n%s"
                         % (proc.stdout or "")[-500:])


class TestEnvOverrideWhitelist(unittest.TestCase):
    """A2：TERM_TOKEN 不得进隔离白名单。"""

    def test_term_token_not_overridable(self):
        self.assertNotIn("TERM_TOKEN", config_mod.ENV_OVERRIDABLE,
                         "TERM_TOKEN 进白名单 ⇒ setdefault 夹具看似生效，"
                         "实为两套来源打架（判例 179 同族坑）")

    def test_whitelist_stays_credential_free(self):
        for k in config_mod.ENV_OVERRIDABLE:
            self.assertNotRegex(k.upper(), r"TOKEN|SECRET|PASS|KEY",
                                "白名单疑似含凭据类键：%s" % k)


if __name__ == "__main__":
    unittest.main(verbosity=2)
