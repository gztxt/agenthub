"""CCR usage 账本**只读**聚合门面（P0-2）。

定位：agenthub 侧的「单位成功任务成本」度量入口。数据源是 CCR 的账本本体
（`~/.claude-code-router/app-data/usage.sqlite`，2026-10-15 实测 3.7 万行 / 23.7MB），
那是**生命线资产**（CCR 一挂全部 AI 会话停摆）⇒ 本模块**一律以 `?mode=ro` 只读打开**，
绝不写、不建表、不 VACUUM。

三条口径（源自 2026-10-14 取证 + 2026-09-07 判例）：

1. **`logical_model` 不作聚合维度**。它是 CCR 内部逻辑名
   （形如 `provider-alibaba-b600d5a733::openai_chat_completions/deepseek-v4.1-flash`），
   不是客户端 / 模型 ID。2026-09-07 判例：把账本逻辑名当客户端 ID 写进配置 ⇒ jcode 当天不可用。
   故 `GROUP_COLS` 白名单里没有它，`summary()` 传它直接 `ValueError`（**拒绝，不静默忽略**）。
2. **主聚合维度 = `client × model`**（client 辨识调用来源，model 是真实模型 ID）；
   看费用时按 `provider × model` 交叉。时间轴一律走已索引的
   `usage_events(created_at,…)` 复合索引，按 **UTC 日切**（`created_at` 自带 `Z`）。
3. **cost 必须显式区分 NULL**：`COALESCE(cost_usd,0)` 求和，**同时**回 `null_cost` 计数
   与 `cost_sources` 分布。2026-10-15 实测：免费模型（`nvidia/…:free`）落库 `cost_usd=NULL`
   ——把它们静默当 0 会让「成本」看起来比实际小（判例：静默 0 = 谎报）。

归因口径 `ATTRIBUTION`：2026-10-15 单次只读探针实测 **CCR 不透传客户端 `X-Request-Id`**
（探针带 `X-Request-Id: hubprobe-…` 打 `/v1/messages` 得 200，账本落的是 CCR 自生成的
UUIDv4 `19eccbce-…`，查不到探针 id）。⇒ 任务↔成本只能按 `client×model×时间窗` **近似归因**，
回包必须显式标 `attribution`，不得让调用方误以为是精确对齐。

fail-closed：账本缺失 / 打不开 / 聚合异常一律 `{"ok": False, "error": …}`，
**绝不返回 0 当成功**。
"""
from __future__ import annotations

import os
import sqlite3
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple
from urllib.parse import quote

#: 账本默认路径（CCR 生命线资产）。env `CCR_USAGE_DB` 覆盖点：测试与备用部署用。
DEFAULT_DB = Path.home() / ".claude-code-router" / "app-data" / "usage.sqlite"

#: 允许的聚合维度白名单。**故意不含 `logical_model`**（见模块 docstring 口径 1）
#: 与 `id`/`path`/`method`（无聚合语义）。要加维度必须先改这里 + 补一条测试。
GROUP_COLS: Tuple[str, ...] = ("client", "model", "provider", "credential_id")

#: 归因精度。approximate = 按 client×model×时间窗近似（当前实测口径）；
#: exact = 若将来 CCR 支持透传 X-Request-Id，可由 env 改为 exact。
ATTRIBUTION = os.getenv("CCR_USAGE_ATTRIBUTION", "approximate")

MAX_LIMIT = 500
DEFAULT_LIMIT = 100


def db_path() -> str:
    """账本路径（**每次读 env**，测试可逐例覆盖；不在 import 期固化）。"""
    return os.getenv("CCR_USAGE_DB") or str(DEFAULT_DB)


def _connect_ro(path: str) -> sqlite3.Connection:
    """只读连接。`?mode=ro` 是硬约束：哪怕代码有 bug 也写不进账本。

    路径先 percent-encode（账本路径理论上可含 `?`/`#`，未编码会让 URI 解析跑偏）。
    """
    uri = "file:" + quote(str(path)) + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True, timeout=5.0)
    conn.row_factory = sqlite3.Row
    return conn


def _iso_z(dt: datetime) -> str:
    """账本口径的 UTC ISO 串（`…T…Z`）。

    **不能用 `datetime.isoformat()`**：它给 `+00:00`，而账本存的是 `Z`。
    `'Z' > '+'` ⇒ 两串直接字典序比较会整体错位（`created_at>=?` 会漏掉/多收一整天）。
    """
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    dt = dt.astimezone(timezone.utc)
    return dt.strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def window_bounds(window_hours: Optional[int]) -> Tuple[Optional[str], Optional[str]]:
    """把「最近 N 小时」翻成账本边界 `(since, until)`；`0`/负/None = 不限（全量）。"""
    if not window_hours or int(window_hours) <= 0:
        return None, None
    now = datetime.now(timezone.utc)
    return _iso_z(now - timedelta(hours=int(window_hours))), _iso_z(now)


def _bad_group_by(cols: Sequence[str]) -> List[str]:
    return [c for c in cols if c not in GROUP_COLS]


def available(path: Optional[str] = None) -> Tuple[bool, str]:
    """账本可用性（供端点做 fail-closed 判定，不抛异常）。"""
    p = path or db_path()
    if not os.path.exists(p):
        return False, f"usage ledger not found: {p}"
    try:
        conn = _connect_ro(p)
        try:
            conn.execute("SELECT 1 FROM usage_events LIMIT 1").fetchone()
        finally:
            conn.close()
        return True, "ok"
    except sqlite3.Error as e:  # noqa: BLE001
        return False, f"cannot read usage ledger: {type(e).__name__}: {e}"


def summary(group_by: Sequence[str] = ("client", "model"),
            since: Optional[str] = None, until: Optional[str] = None,
            limit: int = DEFAULT_LIMIT, path: Optional[str] = None) -> Dict[str, Any]:
    """按 `group_by` 聚合 `usage_events`（默认 client × model）。

    返回 `{"ok":True, rows:[…], total:{…}, attribution:…}`；
    任何失败返回 `{"ok":False,"error":…}`（**不抛、不静默 0**，端点据此回 503）。

    `group_by` 含白名单外维度 ⇒ `ValueError`（编程错误要炸在测试里，不是吞掉）。
    """
    cols = tuple(str(c).strip() for c in group_by if str(c).strip())
    if not cols:
        raise ValueError("group_by 不能为空")
    bad = _bad_group_by(cols)
    if bad:
        raise ValueError(
            f"group_by 只允许 {list(GROUP_COLS)}，不支持 {bad}"
            "（尤其 logical_model：它是 CCR 内部逻辑名，不是客户端/模型 ID）")

    p = path or db_path()
    base: Dict[str, Any] = {"db": p, "mode": "ro", "group_by": list(cols),
                            "since": since, "until": until,
                            "attribution": ATTRIBUTION}
    if not os.path.exists(p):
        return {**base, "ok": False, "error": f"usage ledger not found: {p}",
                "rows": [], "total": None}

    lim = max(1, min(int(limit or DEFAULT_LIMIT), MAX_LIMIT))
    try:
        conn = _connect_ro(p)
    except sqlite3.Error as e:  # noqa: BLE001
        return {**base, "ok": False, "error": f"cannot open usage ledger (ro): {e}",
                "rows": [], "total": None}

    try:
        conds: List[str] = []
        params: List[Any] = []
        if since:
            conds.append("created_at>=?")
            params.append(str(since))
        if until:
            conds.append("created_at<?")
            params.append(str(until))
        where = (" WHERE " + " AND ".join(conds)) if conds else ""
        sel = ", ".join(cols)

        rows_sql = (
            f"SELECT {sel}, COUNT(*) AS requests,"
            " COALESCE(SUM(input_tokens),0) AS input_tokens,"
            " COALESCE(SUM(output_tokens),0) AS output_tokens,"
            " COALESCE(SUM(cache_read_tokens),0) AS cache_read_tokens,"
            " COALESCE(SUM(cache_write_tokens),0) AS cache_write_tokens,"
            " COALESCE(SUM(total_tokens),0) AS total_tokens,"
            " COALESCE(SUM(cost_usd),0) AS cost_usd,"
            " SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS null_cost"
            f" FROM usage_events{where}"
            f" GROUP BY {sel}"
            " ORDER BY total_tokens DESC, requests DESC LIMIT ?")
        rows = [dict(r) for r in conn.execute(rows_sql, (*params, lim))]

        tot_sql = (
            "SELECT COUNT(*) AS requests,"
            " COALESCE(SUM(total_tokens),0) AS total_tokens,"
            " COALESCE(SUM(cost_usd),0) AS cost_usd,"
            " SUM(CASE WHEN cost_usd IS NULL THEN 1 ELSE 0 END) AS null_cost"
            f" FROM usage_events{where}")
        total = dict(conn.execute(tot_sql, params).fetchone())

        cs_sql = (
            "SELECT COALESCE(cost_source,'') AS cost_source, COUNT(*) AS n"
            f" FROM usage_events{where} GROUP BY cost_source ORDER BY n DESC")
        # 空 cost_source 的条目**显式留一个 "" 键**（不许悄悄并进别的桶）
        total["cost_sources"] = {r["cost_source"]: r["n"] for r in conn.execute(cs_sql, params)}

        codes_sql = ("SELECT status_code, COUNT(*) AS n"
                     f" FROM usage_events{where} GROUP BY status_code ORDER BY n DESC LIMIT 10")
        total["status_codes"] = {int(r["status_code"]): r["n"] for r in conn.execute(codes_sql, params)}
    except sqlite3.Error as e:  # noqa: BLE001
        return {**base, "ok": False, "error": f"usage ledger query failed: {type(e).__name__}: {e}",
                "rows": [], "total": None}
    finally:
        conn.close()

    return {**base, "ok": True, "rows": rows, "count": len(rows), "total": total}


def by_request(request_ids: Iterable[str], path: Optional[str] = None,
               limit: int = 200) -> Dict[str, Any]:
    """按 `request_id` 精确取行（探针取证 / 将来精确归因用）。

    ⚠ 当前 `ATTRIBUTION=approximate`（CCR 不透传客户端 id）⇒ **hub 自己造不出**这些 id，
    本函数只在「拿到 CCR 侧 id」时有用，别把它当任务↔成本的主路径。
    """
    ids = [str(i).strip() for i in request_ids if str(i).strip()]
    if not ids:
        return {"ok": True, "rows": [], "count": 0}
    p = path or db_path()
    if not os.path.exists(p):
        return {"ok": False, "error": f"usage ledger not found: {p}", "rows": [], "count": 0}
    lim = max(1, min(int(limit or 200), MAX_LIMIT))
    try:
        conn = _connect_ro(p)
    except sqlite3.Error as e:  # noqa: BLE001
        return {"ok": False, "error": f"cannot open usage ledger (ro): {e}", "rows": [], "count": 0}
    try:
        ph = ",".join("?" for _ in ids)
        sql = ("SELECT id,created_at,request_id,client,model,provider,status_code,"
               "duration_ms,total_tokens,cost_usd,cost_source"
               f" FROM usage_events WHERE request_id IN ({ph})"
               " ORDER BY created_at LIMIT ?")
        rows = [dict(r) for r in conn.execute(sql, (*ids, lim))]
    except sqlite3.Error as e:  # noqa: BLE001
        return {"ok": False, "error": f"usage ledger query failed: {e}", "rows": [], "count": 0}
    finally:
        conn.close()
    return {"ok": True, "rows": rows, "count": len(rows)}