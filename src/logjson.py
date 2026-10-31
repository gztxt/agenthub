"""JSON 行日志（v0.13.185）。

现状：`src/` 下 73 处 `print()`，只有 7 个模块 `import logging`，日志格式三套并存
（纯文本 print / hublog 落盘行 / uvicorn 默认格式）。跨模块追一次请求只能靠 grep 时间戳。

**本模块只做新增通道，不迁移既有 print**：那 73 处里有 systemd journal 依赖的可读文本、
有测试断言的字符串、有启动横幅 —— 一次性改格式的收益远小于「用户侧日志突然看不懂」的代价。
迁移策略是「新代码用 logger，老代码跟着 print 一直活到被重写」。

用法：
    from logjson import get_logger
    log = get_logger("agenthub.csp")
    log.info("csp applied", extra={"mode": "report", "path": "/health"})

输出（一行一条，journal/文件都按行切）：
    {"ts":"2026-10-31T02:55:01.123Z","level":"INFO","logger":"agenthub.csp",
     "msg":"csp applied","mode":"report","path":"/health"}

`extra` 里的保留字段（与 LogRecord 内置名冲突的）会被丢弃 —— 否则 `logging` 自己在
`record.__dict__` 上就抛 KeyError，日志点变成异常点。
"""

from __future__ import annotations

import json
import logging
import os
import sys
from datetime import datetime, timezone
from typing import Any, Dict, Iterable

#: LogRecord 自带的键，`extra` 与它们重名会直接抛 KeyError，必须跳过
_RESERVED = frozenset(
    "name msg args levelname levelno pathname filename module exc_info exc_text "
    "stack_info lineno funcName created msecs relativeCreated thread threadName "
    "processName process taskName".split()
)

#: 允许进 JSON 的标量类型（其余转 str，避免 json.dumps 直接炸）
_SCALAR = (str, int, float, bool, type(None))


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds").replace("+00:00", "Z")


class JsonFormatter(logging.Formatter):
    """把 LogRecord 压成一行 JSON；异常走 `exc` 字段（保留完整 traceback 文本）。"""

    def __init__(self, service: str = "agenthub") -> None:
        super().__init__()
        self.service = service

    def format(self, record: logging.LogRecord) -> str:
        out: Dict[str, Any] = {
            "ts": _now_iso(),
            "level": record.levelname,
            "logger": record.name,
            "msg": record.getMessage(),
        }
        if self.service and record.name.startswith(self.service):
            out["svc"] = self.service
        for k, v in record.__dict__.items():
            if k in _RESERVED or k.startswith("_") or k in out:
                continue
            out[k] = v if isinstance(v, _SCALAR) else str(v)
        if record.exc_info:
            out["exc"] = self.formatException(record.exc_info)
        if record.stack_info:
            out["stack"] = self.formatStack(record.stack_info)
        return json.dumps(out, ensure_ascii=False, default=str)


class JsonStreamHandler(logging.StreamHandler):
    """StreamHandler 的 `emit` 失败时不能抛（logging 内部会产生 "--- Logging error ---"
    并污染 stderr）。这里把落盘失败降级为静默一次 —— 日志不能反过来打断服务。"""

    def emit(self, record: logging.LogRecord) -> None:  # pragma: no cover - 失败路径
        try:
            super().emit(record)
        except Exception:
            pass


_setup_done: bool = False


def setup(level: str = "INFO", service: str = "agenthub",
          namespaces: Iterable[str] = ()) -> logging.Logger:
    """幂等配置 `agenthub` logger：JSON 输出、不向 root 冒泡。

    不接管 root logger —— uvicorn 自己装了 handler，接管会把它的访问日志也改成 JSON，
    然后 `journalctl -u agenthub | grep GET` 这类既有用法全部失效。
    """
    global _setup_done
    log = logging.getLogger(service)
    if _setup_done:
        return log
    lvl = getattr(logging, (os.getenv("HUB_LOG_LEVEL") or level).upper(), logging.INFO)
    h = JsonStreamHandler(sys.stderr)
    h.setFormatter(JsonFormatter(service))
    log.addHandler(h)
    log.setLevel(lvl)
    log.propagate = False        # 防与 root/uvicorn handler 重复输出
    for ns in namespaces:
        sub = logging.getLogger(ns)
        sub.setLevel(lvl)
        sub.propagate = True     # 冒到 agenthub 这一层再统一切格式
    _setup_done = True
    return log


def get_logger(name: str) -> logging.Logger:
    """取子 logger；未调用 setup 时也能用（走 logging 默认行为，不会报错）。"""
    if not name.startswith("agenthub"):
        name = "agenthub." + name
    return logging.getLogger(name)
