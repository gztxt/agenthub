"""`httpx` 的名字转接（hub venv 里装的是 `httpx2`，没有 `httpx` 这个名字）。

为什么不是「改名 import」：`nanobot/apps/cli/service.py` 是**逐字节上游副本**，
里面写死了 `import httpx`。改它就不再是字节级，所以把名字这一层放在外面。

`httpx2` 与 httpx 同源同 API（实测 `get` / `AsyncClient` / `HTTPError` 都在），
这里直接把 `sys.modules["httpx"]` 指过去，等于**零封装**——多包一层只会造出
「两套异常类型」这种难查的坑。
"""
from __future__ import annotations

import sys

try:
    import httpx2 as _impl
except ImportError as exc:  # 部署面缺包时显式失败，不静默降级
    raise ImportError(
        "vendored CLI Apps 需要 httpx2（hub venv 已装）；先用 "
        "`venv/bin/python -c \"import httpx2\"` 验，再重启 hub。"
    ) from exc

sys.modules[__name__] = _impl
