"""`loguru` 的最小替身（hub venv 不装 loguru）。

上游 `nanobot/apps/cli/service.py` 只用到三处 `logger.info("a {} b {}", x, y)`，
所以这里只需要「花括号占位 + 忽略多余字段」。**不追求 loguru 全兼容**：
真需要 sink/rotation 时应该引 loguru 本体，而不是往这里加油。

日志去向：hub 自己的 logger（stdout 由 systemd 收），前缀 `cli-apps`。
"""
from __future__ import annotations

import logging
from typing import Any

_std = logging.getLogger("hub.cli_apps")


def _fmt(msg: str, args: tuple[Any, ...]) -> str:
    if not args:
        return msg
    try:
        return msg.format(*args)
    except (IndexError, KeyError, ValueError):
        return msg + " " + " ".join(str(a) for a in args)


class _Logger:
    """只实现 service.py 用到的方法面。"""

    def debug(self, msg: str, *a: Any, **kw: Any) -> None: _std.debug(_fmt(msg, a))
    def info(self, msg: str, *a: Any, **kw: Any) -> None: _std.info(_fmt(msg, a))
    def warning(self, msg: str, *a: Any, **kw: Any) -> None: _std.warning(_fmt(msg, a))
    def error(self, msg: str, *a: Any, **kw: Any) -> None: _std.error(_fmt(msg, a))
    def exception(self, msg: str, *a: Any, **kw: Any) -> None: _std.exception(_fmt(msg, a))
    def bind(self, **kw: Any) -> "_Logger": return self


logger = _Logger()


def add(*a: Any, **kw: Any):
    """占位：真需要 sink 时请显式引 loguru，别让这里假装成功。"""
    raise NotImplementedError("vendored loguru shim 不支持 add()；要配置 sink 请安装 loguru 本体")


def remove(*a: Any, **kw: Any):
    raise NotImplementedError("vendored loguru shim 不支持 remove()")
