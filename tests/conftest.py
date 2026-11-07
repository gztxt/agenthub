"""pytest 直通档的身份替身（2026-11-07）。

真实 `users.identity_of` 只认会话 Cookie 与真实 Basic 密码；L0 夹具的语义是
「带凭据头 = 已登录」。替身实现与约束见 `tests/authfix.py`，生产代码零改动。

run_tests.sh 的 hermetic/host/all 档走 scripts/run_tier.py（在那里 install），
pytest 直通档靠本文件。
"""
import pathlib
import sys

_ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_ROOT / "src"))
sys.path.insert(0, str(_ROOT / "tests"))

import authfix  # noqa: E402

authfix.install()
