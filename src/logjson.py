"""JSON 行日志（v0.13.185）。

现状：`src/` 下 73 处 `print()`，只有 7 个模块 `import logging`，日志格式三套并存
（纯文本 print / hublog 落盘行 / uvicorn 默认格式）。跨模块追一次请求只能靠 grep 时间戳。

**输出格式由 `HUB_LOG_JSON` 决定，默认关闭**：
  · 未设置 / 非真值 ⇒ `PlainFormatter`，只输出消息正文（`%(message)s`），写到 stdout。
    与原来的 `print()` 逐字一致（无时间戳、无 `INFO:module:` 前缀），journald 解析与
    `grep '^\\[tag\\]'` 的既有用法不受影响。
  · `HUB_LOG_JSON=1|true|yes|on` ⇒ `JsonFormatter`，一行 JSON，写到 stderr。

`src/` 下的诊断性 print 已按上面第二种口径换成 `logging.getLogger(__name__).info/warning`；
面向用户的 CLI 输出（argparse 结果、脚本 `__main__`）与启动引导横幅保留 print。
handler 挂在 **root** 上并加 `SrcOnlyFilter`（按 `record.pathname` 判 `src/` 目录）：
  · 这样模块用 `logging.getLogger(__name__)` 即可，无需逐模块登记命名空间；
  · uvicorn/aiohttp 等第三方记录不在 src 下 ⇒ 不会被换成 JSON、也不会多打一遍；
  · root 上重复调用 setup 凭 handler 上的标记位幂等，不会叠加。

用法：
    from logjson import get_logger
    log = get_logger("agenthub.csp")
    log.info("csp applied", extra={"mode": "report", "path": "/health"})

JSON 输出（一行一条，journal/文件都按行切）：
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
from pathlib import Path
from typing import Any, Dict, Iterable

#: 开关名与真值口径（与仓库里其它布尔环境变量一致：1/true/yes/on）
LOG_JSON_ENV = "HUB_LOG_JSON"
_TRUTHY = ("1", "true", "yes", "on")
#: handler 上的私有标记，用于 root 上的幂等判断（`_setup_done` 只是快速路径）
_HANDLER_TAG = "_agenthub_jsonlog"

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


class PlainFormatter(logging.Formatter):
    """默认模式：只输出消息正文，与原来的 `print()` 逐字一致。

    为什么不加 `%(levelname)s %(name)s:` 前缀：默认关闭 JSON 的意义就是
    「不开就跟改动前一样」。既有的 journald 解析（`src/hublog.py` 按 `[tag]` 判级别）
    与人工 `grep '\\[term\\]'` 都依赖正文里自带的前缀，多一层 logging 前缀会全部错位。
    """

    def format(self, record: logging.LogRecord) -> str:
        msg = record.getMessage()
        if record.exc_info:
            msg += "\n" + self.formatException(record.exc_info)
        return msg


class SrcOnlyFilter(logging.Filter):
    """只放行 `src/` 下模块（或 `agenthub*` 命名空间）产生的记录。

    判 `pathname` 而不是登记一堆命名空间：模块改名/新增时零配置自动纳入，
    漏登记＝那条日志静默消失，而 pathname 不会漏。第三方库（uvicorn/aiohttp）
    的 `__file__` 不在 src 下 ⇒ 既不会被 JSON 化，也不会被多打一遍。
    """

    def __init__(self, base_dir: str) -> None:
        super().__init__()
        self.base = str(Path(base_dir).resolve()) + os.sep

    def filter(self, record: logging.LogRecord) -> bool:
        if record.name == "agenthub" or record.name.startswith("agenthub."):
            return True
        path = getattr(record, "pathname", "") or ""
        try:
            return str(Path(path).resolve()).startswith(self.base)
        except OSError:                    # 路径解析失败 ⇒ 宁可丢这条，也不炸 handler
            return False


def json_enabled() -> bool:
    """当前进程是否启用 JSON 输出（每次调用现读环境，测试可 monkeypatch）。"""
    return (os.getenv(LOG_JSON_ENV) or "").strip().lower() in _TRUTHY


_setup_done: bool = False


def _installed(root: logging.Logger):
    for h in root.handlers:
        if getattr(h, _HANDLER_TAG, False):
            return h
    return None


def setup(level: str = "INFO", service: str = "agenthub",
          namespaces: Iterable[str] = (), stream=None) -> logging.Logger:
    """幂等配置 root handler：JSON 或纯文本，由 `HUB_LOG_JSON` 决定。

    handler 挂 root + `SrcOnlyFilter`，所以 `logging.getLogger(__name__)` 的模块
    无需逐个登记就能被同一套格式接管。uvicorn 的 logger 自带 `propagate=False`
    且路径不在 src 下，两条都挡住它 —— 访问日志格式不变。

    `namespaces` 保留兼容旧调用：显式列出的命名空间置 level 并允许冒泡。
    """
    global _setup_done
    log = logging.getLogger(service)
    root = logging.getLogger()
    if _setup_done or _installed(root) is not None:
        _setup_done = True
        return log
    lvl = getattr(logging, (os.getenv("HUB_LOG_LEVEL") or level).upper(), logging.INFO)
    is_json = json_enabled()
    # JSON 走 stderr（单行供 journal/采集器按行解析）；默认模式走 stdout ——
    # 原 print() 就是 stdout，换流会让 `cmd > file` 这类既有用法拿不到日志。
    h = JsonStreamHandler(stream or (sys.stderr if is_json else sys.stdout))
    h.setFormatter(JsonFormatter(service) if is_json else PlainFormatter())
    h.addFilter(SrcOnlyFilter(os.path.dirname(os.path.abspath(__file__))))
    h.setLevel(lvl)
    setattr(h, _HANDLER_TAG, True)
    root.addHandler(h)
    if root.level == logging.NOTSET or root.level > lvl:
        # root 默认 WARNING 会把 INFO 挡在 handler 之前；只放宽不收紧
        root.setLevel(lvl)
    log.setLevel(lvl)
    log.propagate = True         # 冒到 root 的 handler（不再自挂一份，避免双打）
    for ns in namespaces:
        sub = logging.getLogger(ns)
        sub.setLevel(lvl)
        sub.propagate = True
    _setup_done = True
    return log


def get_logger(name: str) -> logging.Logger:
    """取子 logger；未调用 setup 时也能用（走 logging 默认行为，不会报错）。"""
    if not name.startswith("agenthub"):
        name = "agenthub." + name
    return logging.getLogger(name)
