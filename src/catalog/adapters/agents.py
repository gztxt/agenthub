"""agents 适配器 —— agent 名录（PT-20261025-12 P0）。

**三种状态合成一条对象**（对应 `datastore.BACKENDS["agents"]` 的三态）：

| 状态 | 来源 | 权威 |
|---|---|---|
| 声明态 | `profiles.PROFILES`（含 detect 正则） | ✅ 权威 |
| 发现态 | `agents.db:custom_agents`（运行时扫描入库，`kind='agent'`） | 派生 |
| 运行态 | 进程在不在 | **不入编目库**（实时数据，见下） |

**为什么运行态不入库**：进程存活是秒级变化的实时事实，编目库是分钟级投影。
把实时状态写进投影，只会得到一个永远陈旧、还让人误信的字段。
运行态仍由 `/api/agents` / `hub_list_ports` 的进程探测提供——**两个口径各管一段**，
不在编目层造假。

**非 agent 的 custom_agents 行**（`kind='service'`，实测 6 条：Xvfb/socat relay 等）
走 `service` 域/`x_service` 表——它们是服务不是 agent，「不同类型不同表」。
"""
from __future__ import annotations

import os
import sqlite3
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, fingerprint, register


def _agents_db_path() -> Path:
    """原生 `agents.db` 的**真实位置**（发现态来源）。

    ⚠ 这里不能用「编目库所在目录」推导 —— 那是**派生库**的位置。
    编目要读的是原生库：生产在 `$AGENTHUB_HOME/data/agents.db`，
    影子/隔离实例必须能显式指到生产库才能复核发现态。

    解析顺序：`AGENTHUB_NATIVE_DB`（显式，最高）→ `config.db_path`
    （跟随 DATA_DIR，即影子实例自己那份）→ 仓内 `data/agents.db`。
    """
    native = os.getenv("AGENTHUB_NATIVE_DB")
    if native:
        return Path(native)
    try:
        from config import config
        return Path(config.db_path)
    except Exception:  # noqa: BLE001  配置不可用时退回仓内默认
        return Path(__file__).resolve().parents[2] / "data" / "agents.db"


def _discovered(db_path: Path) -> List[Dict[str, Any]]:
    """读 custom_agents 发现态。**只读连接**，表不存在/库不存在都返回空 + 原因。"""
    if not Path(db_path).exists():
        return []
    out: List[Dict[str, Any]] = []
    try:
        con = sqlite3.connect("file:%s?mode=ro" % str(db_path), uri=True,
                              timeout=3.0)
    except sqlite3.Error as exc:
        raise RuntimeError("agents.db 只读连接失败：%s" % exc) from exc
    try:
        con.row_factory = sqlite3.Row
        exists = con.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' "
            "AND name='custom_agents'").fetchone()
        if not exists:
            return []
        rows = con.execute(
            "SELECT * FROM custom_agents ORDER BY updated_at DESC").fetchall()
        out = [dict(r) for r in rows]
    except sqlite3.Error as exc:
        raise RuntimeError("custom_agents 只读扫描失败：%s" % exc) from exc
    finally:
        con.close()
    return out


@register
class AgentsAdapter(BaseAdapter):
    src_id = "agents.registry"
    domain = "agent"
    kind = "table"
    authority = 0
    adapter = "agents_registry"
    scan_mode = "incremental"
    uri = "table://profiles.PROFILES + table://agents.db:custom_agents"

    def probe(self) -> Dict[str, Any]:
        return {"ok": True, "reason": None, "error": None}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        import profiles
        import json

        records: List[Dict[str, Any]] = []
        live: List[str] = []

        # ① 声明态（权威）
        for p in (profiles.PROFILES or []):
            aid = str(p.get("id") or "")
            if not aid:
                continue
            nkey = "declared:%s" % aid
            digest = fingerprint(p)
            if self.skip(states, nkey, None, None, digest):
                live.append(_oid("agent", nkey))
                continue
            records.append({
                "domain": "agent", "type": "agent", "native_key": nkey,
                "uri": "table://profiles:%s" % aid,
                "title": "%s（%s）" % (p.get("name") or aid, aid),
                "summary": (p.get("desc") or "")[:500],
                "mtime": None, "size": None, "hash": digest,
                "ext": {
                    "agent_id": aid, "installed": 1, "native_ui": 1 if p.get("ui") else 0,
                    "ports": json.dumps({"port": p.get("port")}, ensure_ascii=False),
                    "models": None,
                    "states": json.dumps(["declared"], ensure_ascii=False),
                },
                "meta": {"cli": p.get("cli"), "kind": p.get("kind"),
                         "has_terminal": bool(p.get("terminal")),
                         "detect": bool(p.get("detect"))},
                "chunks": [],
            })
            live.append(_oid("agent", nkey))

        # ② 发现态（custom_agents）
        for row in _discovered(_agents_db_path()):
            rid = str(row.get("id") or "")
            if not rid:
                continue
            kind = (row.get("kind") or "agent").strip()
            nkey = "discovered:%s" % rid
            domain = "agent" if kind == "agent" else "service"
            digest = fingerprint(row)
            if self.skip(states, nkey, None, None, digest):
                live.append(_oid(domain, nkey))
                continue
            if domain == "agent":
                ext = {"agent_id": rid, "installed": 1, "native_ui": 0,
                       "ports": json.dumps({"port": row.get("port")},
                                           ensure_ascii=False),
                       "models": None,
                       "states": json.dumps(["discovered"], ensure_ascii=False)}
            else:
                ext = {"name": row.get("name"), "command": row.get("command"),
                       "port": row.get("port"), "endpoint": row.get("endpoint"),
                       "auto_restart": row.get("auto_restart"), "unit": None}
            records.append({
                "domain": domain,
                "type": "agent" if domain == "agent" else "service",
                "native_key": nkey,
                "uri": "table://custom_agents:%s" % rid,
                "title": str(row.get("name") or rid)[:200],
                "summary": (row.get("description") or "")[:500],
                "mtime": None, "size": None, "hash": digest,
                "ext": ext,
                "meta": {"source": row.get("source"), "kind": kind,
                         "type": row.get("type")},
                "chunks": [],
            })
            live.append(_oid(domain, nkey))

        return {"records": records, "live": live, "complete": True,
                "note": "声明 %d + 发现 %d" % (
                    sum(1 for r in records if r["native_key"].startswith("declared:")),
                    sum(1 for r in records if r["native_key"].startswith("discovered:")))}


def _oid(domain: str, native_key: str) -> str:
    from .. import schema
    return schema.oid_of(domain, "agents.registry", native_key)
