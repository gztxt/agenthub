"""P3 v1 归一化事件契约（供 headless / 直连适配层使用，不碰 pty 终端链路）。

设计依据：docs/P3-EVENT-CONTRACT-DESIGN-20261109.md §3.1 / §5 / §6。

边界（与设计逐条对齐）：
  · 只 6 类业务事件 + error，不做 t3code 的 14 类全集，不做 fork/rollback；
  · envelope 单一定义，v 直接取 P4 的 meta.PROTOCOL_VERSION（§5.10：冲突以 P4 为准）；
  · seq 由 EventEmitter 单调递增，turn_id 标识一轮；
  · error.reason 收敛到 4 个枚举值，未知 reason 归一成 provider_error 并保留原值
    （禁止静默丢弃，也不让上游的私有枚举把下游契约撑破）；
  · 进程运行登记表只服务 interrupt，不做事件溯源 / projection / outbox（§5.4）。

事件字段：
    {v, seq, ts, sid, turn_id, agent_id, type, ...payload}

type 未知时不抛异常（前向兼容；前端按未知类型忽略即可）。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from datetime import datetime, timezone
from typing import Any, Callable, Dict, Optional

from meta import PROTOCOL_VERSION

#: 事件契约版本。单一版本源在 src/meta.py（P4 能力协商），此处不另立字面量。
EVENT_VERSION = PROTOCOL_VERSION

#: v1 最小事件集合（外加 error）。
EVENT_TYPES = (
    "turn_started",
    "message_delta",
    "tool_call",
    "tool_result",
    "usage",
    "turn_completed",
    "error",
)

#: error.reason 的合法枚举。除这 4 个以外的值一律归一成 provider_error。
ERROR_REASONS = (
    "interrupted",
    "unsupported_steer",
    "timeout",
    "provider_error",
)

#: envelope 固定字段；usage 展开时不得覆盖它们。
ENVELOPE_KEYS = frozenset(("v", "seq", "ts", "sid", "turn_id", "agent_id", "type"))

#: ANSI 转义（CSI + OSC）。事件字段是给结构化前端看的，不能把终端控制码带进去。
_ANSI_RE = re.compile(r"\x1b\[[0-?]*[ -/]*[@-~]|\x1b\][^\x07]*\x07")


def strip_ansi(text) -> str:
    """去掉 ANSI 控制序列；非字符串返回空串（调用方只传文本字段）。"""
    if not isinstance(text, str):
        return ""
    return _ANSI_RE.sub("", text)


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="milliseconds")


def reason_for_error(error: Optional[str]) -> str:
    """把适配器返回的错误串归一到 4 个枚举之一（适配器层错误串格式保持原样）。"""
    s = (error or "").lower()
    if "interrupt" in s:
        return "interrupted"
    if "timeout" in s or "timed out" in s:
        return "timeout"
    return "provider_error"


class EventEmitter:
    """单轮事件发射器：持有 seq / sid / turn_id，输出可直接 JSON 序列化的 dict。"""

    def __init__(self, sid: str, agent_id: str, turn_id: Optional[str] = None):
        self.sid = sid or ""
        self.agent_id = agent_id or ""
        self.turn_id = turn_id or uuid.uuid4().hex[:12]
        self.seq = 0

    def emit(self, type: str, **payload: Any) -> Dict[str, Any]:
        self.seq += 1
        ev: Dict[str, Any] = {
            "v": EVENT_VERSION,
            "seq": self.seq,
            "ts": _now_iso(),
            "sid": self.sid,
            "turn_id": self.turn_id,
            "agent_id": self.agent_id,
            "type": str(type),
        }
        if type == "error":
            reason = payload.get("reason") or "provider_error"
            if reason not in ERROR_REASONS:
                # 不静默丢弃：原始值保留在 reason_raw 里，契约面收敛成 provider_error。
                payload.setdefault("reason_raw", reason)
                reason = "provider_error"
            payload["reason"] = reason
        for key in ("text", "message", "output", "input"):
            if isinstance(payload.get(key), str):
                payload[key] = strip_ansi(payload[key])
        ev.update(payload)
        return ev

    def error(self, reason: str = "provider_error", message: Optional[str] = None,
              **extra: Any) -> Dict[str, Any]:
        if message is not None:
            extra["message"] = message
        extra["reason"] = reason
        return self.emit("error", **extra)

    def usage(self, usage: Optional[dict]) -> Optional[Dict[str, Any]]:
        """把 provider usage 展开成 usage 事件；无数据返回 None（不发空事件）。"""
        if not isinstance(usage, dict) or not usage:
            return None
        body = {k: v for k, v in usage.items() if k not in ENVELOPE_KEYS}
        if not body:
            return None
        return self.emit("usage", **body)


def dumps(ev: Dict[str, Any]) -> str:
    """稳定序列化：字段顺序由 dict 插入序保证，ASCII 不转义（中文原样）。"""
    return json.dumps(ev, ensure_ascii=False, separators=(",", ":"))


def to_sse(ev: Dict[str, Any]) -> str:
    """SSE 帧：只写 data: 一行 + 空行（事件类型在 JSON 的 type 字段里）。"""
    return "data: " + dumps(ev) + "\n\n"


# ── 运行登记表（只服务 interrupt，不做事件持久化）────────────────────────────
_RUNS: Dict[str, Dict[str, Any]] = {}


def register_run(run_id: str, agent_id: str, sid: str = "",
                 interrupt: Optional[Callable[[], Any]] = None,
                 turn_id: Optional[str] = None) -> Dict[str, Any]:
    """登记一条正在跑的事件流。interrupt 为 None = 该 provider 不支持中断。"""
    entry: Dict[str, Any] = {
        "run_id": run_id,
        "agent_id": agent_id,
        "sid": sid or "",
        "turn_id": turn_id,
        "interrupt": interrupt,
        "interrupted": False,
        "started_at": time.time(),
    }
    _RUNS[run_id] = entry
    return entry


def unregister_run(run_id: str) -> None:
    _RUNS.pop(run_id, None)


def get_run(run_id: Optional[str]) -> Optional[Dict[str, Any]]:
    if not run_id:
        return None
    return _RUNS.get(run_id)


def find_run(agent_id: str, sid: Optional[str] = None,
             run_id: Optional[str] = None) -> Optional[Dict[str, Any]]:
    """按 run_id 精确找；否则在「同 agent（+同 sid）」的活动运行里取最新一条。"""
    if run_id:
        return _RUNS.get(run_id)
    cands = [r for r in _RUNS.values()
             if r.get("agent_id") == agent_id and (not sid or r.get("sid") == sid)]
    if not cands:
        return None
    return max(cands, key=lambda r: r.get("started_at") or 0.0)


def active_runs() -> list:
    """当前登记在案的事件运行（观测/测试用，返回副本，不改状态）。"""
    return [dict(v) for v in _RUNS.values()]
