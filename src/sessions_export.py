"""会话导出的**纯渲染层**（JSON / CSV）+ 落盘即脱敏。

对应 0924 方案档 §三「会话导出（P2.5）：历史会话仅显示最近 5 条，无导出全部会话的
按钮或 API」。本模块只负责"行 → 字节"，取数由 main.py 的端点做（那边才有 db/会话存储），
这样 L0 能在空 HOME 下把 CSV 表头、转义、脱敏全部钉死，不必起服务。

两个设计决定（都写在这里，避免后人重新踩）：
  1) **默认脱敏**（`redact=1`）。本工作区已三次被凭据外流打过：备份镜像内含 82 个活凭据文件、
     `wiki/log.md` 历史提交含 CCR web token、外发净仓被闸门拦下 3 个抄了真 token 的文档。
     批量导出会话正文正是"最容易被顺手 commit/转发"的产物形态，所以默认打码，
     并在 meta 里回 `redacted_hits` 计数 —— **不静默改数据**，要原始字节须显式 `redact=0`。
  2) **文件名只用 ASCII**。Content-Disposition 里放中文要 RFC5987 编码，
     不同浏览器/下载器行为不一（本项目手机端 WebView 是主要消费者），
     所以文件名固定 `agent-hub-<kind>-<UTC时间戳>.<ext>`，中文标题留在正文里。
"""

from __future__ import annotations

import csv
import io
import json
import re
import time
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

SESSION_COLUMNS: Sequence[str] = ("id", "agent_id", "title", "created_at", "updated_at", "messages")
MESSAGE_COLUMNS: Sequence[str] = ("session_id", "role", "created_at", "content")
HISTORY_COLUMNS: Sequence[str] = ("agent_id", "session_id", "cwd", "title", "when", "source")

# 与 scripts/export-public-repo.sh 的导出侧脱敏同一套形态（那边管文档，这边管会话正文）
_PATTERNS = [
    re.compile(r"ghp_[0-9A-Za-z]{36}"),
    re.compile(r"github_pat_[0-9A-Za-z_]{20,}"),
    re.compile(r"xox[baprs]-[0-9A-Za-z-]{10,}"),
    re.compile(r"AKIA[0-9A-Z]{16}"),
    re.compile(r"sk-[0-9A-Za-z]{16,}"),
    # 2026-10-08 补：厂商密钥常带**分段前缀**（sk-live- / sk-proj- / sk-ant-…），
    # 原规则要求 `sk-` 后紧跟 16 位字母数字 ⇒ 这类形态整条漏过。
    # 它不是录制功能引入的问题：凡走 redact_text 的路径（会话导出、录制落盘）都漏。
    # ⚠️ 收紧判据（实测踩过的坑）：第一版写 `sk-[0-9A-Za-z]+(-[0-9A-Za-z]+)*` 会误伤
    # `task-sk-update-config` / `mask-sk-8` / 裸 `sk-live` 这类正常文本——
    # 「有连字符分段」不等于「是密钥」。改用**总长**作闸门：连字符分段形态要求
    # 去掉分隔符后仍有 ≥20 位字母数字（真密钥 ≥32 位），短slug 一律放过。
    re.compile(r"sk-(?=[0-9A-Za-z-]{24,}[0-9A-Za-z]{0,})[0-9A-Za-z]+(?:-[0-9A-Za-z]+)*"),
    re.compile(r"(?i)\b((?:ccr_web_token|api[_-]?key|access[_-]?token|refresh[_-]?token|token"
               r"|secret|password|passwd)\s*[\"']?\s*[:=]\s*[\"']?)([A-Za-z0-9_./+-]{16,})"),
    re.compile(r"(?i)\b(bearer\s+)([A-Za-z0-9_.\-]{16,})"),
]
MASK = "<REDACTED-导出脱敏>"


def redact_text(text: Any) -> Tuple[str, int]:
    """打码高置信凭据形态，返回 (结果, 命中数)。非字符串原样转 str，不抛。"""
    if text is None:
        return "", 0
    s = text if isinstance(text, str) else str(text)
    hits = 0
    for p in _PATTERNS:
        def _sub(m: "re.Match") -> str:
            nonlocal hits
            hits += 1
            groups = m.groups()
            # 带前缀捕获组的（key=value / Bearer xxx）保留键名，只吃掉值
            return (groups[0] + MASK) if groups else MASK
        s = p.sub(_sub, s)
    return s, hits


def _redact_any(v: Any) -> Tuple[Any, int]:
    """**递归**脱敏。为什么必须递归：JSON 导出会把会话正文嵌在 `transcript` 里，
    只扫顶层字符串会整个漏掉嵌套正文——而那一层恰恰是唯一带正文的地方。"""
    if isinstance(v, str):
        return redact_text(v)
    if isinstance(v, dict):
        out, hits = {}, 0
        for k, sub in v.items():
            nv, h = _redact_any(sub)
            out[k] = nv
            hits += h
        return out, hits
    if isinstance(v, (list, tuple)):
        out_l, hits = [], 0
        for sub in v:
            nv, h = _redact_any(sub)
            out_l.append(nv)
            hits += h
        return out_l, hits
    return v, 0


def _redact_rows(rows: Iterable[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], int]:
    out, total = [], 0
    for r in rows:
        nr, h = _redact_any(dict(r or {}))
        out.append(nr)
        total += h
    return out, total


def to_csv(rows: Iterable[Dict[str, Any]], columns: Sequence[str]) -> str:
    """按给定列序渲染 CSV（缺列留空、多列丢弃 ⇒ 表头恒定，下游可断言）。"""
    buf = io.StringIO()
    w = csv.writer(buf)                       # 默认 \r\n 行尾，Excel/Numbers 都认
    w.writerow(list(columns))
    for r in rows:
        r = r or {}
        w.writerow(["" if r.get(c) is None else r.get(c) for c in columns])
    return buf.getvalue()


def filename(kind: str, fmt: str, ts: Optional[float] = None) -> str:
    ext = "csv" if fmt == "csv" else "json"
    stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(time.time() if ts is None else ts))
    safe = re.sub(r"[^A-Za-z0-9_.-]", "-", str(kind or "export"))
    return "agent-hub-%s-%s.%s" % (safe, stamp, ext)


def render(rows: Iterable[Dict[str, Any]], columns: Sequence[str], fmt: str = "json",
           kind: str = "sessions", meta: Optional[Dict[str, Any]] = None,
           redact: bool = True, ts: Optional[float] = None) -> Tuple[str, str, str, Dict[str, Any]]:
    """渲染导出体。返回 (body, content_type, filename, meta)。

    meta 里必带 `count` / `redacted_hits` / `redacted`：导出是取证产物，
    "我改了几个字"必须自己说出来，否则用户拿到打码件会当成原文。
    """
    rows = list(rows or [])
    rows, hits = _redact_rows(rows) if redact else (rows, 0)
    m: Dict[str, Any] = {"count": len(rows), "redacted": bool(redact), "redacted_hits": hits,
                         "kind": kind, "format": fmt,
                         "generated_at": time.strftime("%Y-%m-%dT%H:%M:%SZ",
                                                       time.gmtime(time.time() if ts is None else ts))}
    if meta:
        m.update(meta)
    if fmt == "csv":
        body = to_csv(rows, columns)
        return body, "text/csv; charset=utf-8", filename(kind, "csv", ts), m
    payload = {"meta": m, "columns": list(columns), "rows": rows}
    return (json.dumps(payload, ensure_ascii=False, indent=1),
            "application/json; charset=utf-8", filename(kind, "json", ts), m)
