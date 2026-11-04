"""编目扫描调度（PT-20261025-12 P1 遗留项）。

**为什么必须有**：P1 上线后扫描只有手动入口，生产实测 10 天没人跑
（`src.last_ok_at` 停在 2026-10-25，`lag_s` 86 万），统一编目就成了「旧快照」——
用户看到的检索结果与现状不符，而且没有任何告警。

设计：
- 到期判据复用 `verify.LAG_SLO`（会话 15min / 技能 6h / 文档·日志 24h），
  不另设一套阈值（两套阈值必然漂移）。
- 每个到期源**单独起子进程**跑 `scripts/hub-catalog-scan.sh`：扫描是 CPU/IO 重的，
  放进 hub 进程会占 GIL、且崩溃会连坐 API。
- 串行执行，单源超时 30 分钟；失败只记录，不重试风暴（下一 tick 自然再到期）。
- `HUB_CATALOG_SCAN=0` 可整段关掉；`HUB_CATALOG_SCAN_TICK_S` 调心跳（默认 600s）。
"""
from __future__ import annotations

import asyncio
import logging
import os
import subprocess
from pathlib import Path
from typing import List

from . import store
from .verify import LAG_SLO

log = logging.getLogger(__name__)

ROOT = Path(__file__).resolve().parents[2]
SCRIPT = ROOT / "scripts" / "hub-catalog-scan.sh"
#: 允许子进程直接清掉 missing 对象（跳过即出库）。
PURGE_SOURCES = {"docs.filesystem", "sessions.archived", "logs.hub"}
#: 迁移源没有周期入口，调度跳过。
MANUAL_ADAPTERS = {"migrate"}


def due_sources(now: float | None = None) -> List[str]:
    """返回已过 SLO 的源（按 lag 从大到小）。读不了库就返回空。"""
    import time
    import datetime as _dt
    now = now or time.time()
    try:
        con = store.connect(readonly=True)
    except Exception:  # noqa: BLE001
        return []
    out = []
    try:
        for row in con.execute(
                "SELECT src_id, domain, last_ok_at, adapter FROM src"):
            if (row["adapter"] or "") in MANUAL_ADAPTERS:
                continue
            slo = LAG_SLO.get(row["domain"], 24 * 3600)
            if not row["last_ok_at"]:
                out.append((slo + 1, row["src_id"]))
                continue
            try:
                ts = _dt.datetime.fromisoformat(
                    row["last_ok_at"].replace("Z", "+00:00")).timestamp()
            except (ValueError, TypeError):
                out.append((slo + 1, row["src_id"]))
                continue
            lag = now - ts
            if lag > slo:
                out.append((lag, row["src_id"]))
    except Exception:  # noqa: BLE001
        return []
    finally:
        con.close()
    out.sort(reverse=True)
    return [src for _, src in out]


async def _run_source(src: str, timeout_s: int = 1800) -> None:
    argv = ["bash", str(SCRIPT), "--source", src]
    if src in PURGE_SOURCES:
        argv.append("--purge")
    try:
        proc = await asyncio.create_subprocess_exec(
            *argv, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        out, err = await asyncio.wait_for(proc.communicate(), timeout_s)
        if proc.returncode == 0:
            log.info("[catalog] 扫描完成 %s: %s", src, out.decode()[-300:])
        else:
            log.warning("[catalog] 扫描失败 %s rc=%s: %s", src,
                        proc.returncode, err.decode()[-300:])
    except asyncio.TimeoutError:
        log.warning("[catalog] 扫描超时 %s（>%ss），保留上轮结果", src, timeout_s)
        try:
            proc.kill()
        except Exception:  # noqa: BLE001
            pass


async def scan_loop() -> None:
    tick = int(os.getenv("HUB_CATALOG_SCAN_TICK_S", "600"))
    log.info("[catalog] 扫描调度启动，心跳 %ss", tick)
    while True:
        await asyncio.sleep(tick)
        try:
            due = await asyncio.to_thread(due_sources)
        except Exception as exc:  # noqa: BLE001
            log.warning("[catalog] 到期判定失败：%s", exc)
            continue
        for src in due:
            await _run_source(src)
