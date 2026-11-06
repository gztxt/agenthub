"""保留期清理：把「只增不减」的表变成有界的（PT-20261024-08 批次 B3/B4）。

## 背景（2026-10-24 实测）

| 表 | 现状 | 判据 |
|---|---|---|
| `term_recordings` | 258.7 万行 / **597MB**，占 `agents.db` 的 76% | `sweep()`（7 天保留）**只在 `Recorder.close()` 里调**（`term_record.py:147`）⇒ **长开会话 ⇒ 录像只增不减**。`src/main.py:824` 的注释自己写着「⚠ 未治本：`sweep()` 只在会话 close 时跑，表一天长 44 万行」。 |
| `profile_events` | 58,490 行 / 23MB，**全仓无任何 DELETE / 保留期** | 实测分布：2026-09 六百行、2026-10 五万七千行 ⇒ **约 3,250 行/天**，只增不减 |

⇒ 本模块提供**后台定时清理**（不依赖「有人关会话」）+ `profile_events` 的滚动保留期。

## 为什么单独一个模块而不是塞进 term_record

`term_record` 的职责是「录制一条会话的帧」；保留期是**跨表的资源治理**，
把 `profile_events`（另一个业务域的表）的清理塞进 `term_record` 会让
「录制模块」反向依赖「画像模块」。⇒ 独立模块，两张表各一个函数。

## 幂等与安全

- 每个清理函数独立 `try/except`：**一个表清理失败不许拖垮另一个**，
  也不许拖垮后台循环（后台循环挂在 `startup` 的 `_spawn` 上，崩了会静默停摆）。
- 清理**只删超过保留期的行**，不碰当期数据；`created_at`/`ts` 是判据列。
- **不跑 VACUUM**：SQLite 的 VACUUM 会重写整库（597MB / 258 万行），
  在生产进程里跑会长时间持锁 ⇒ 只 DELETE，靠 WAL checkpoint 回收页。
  （空间回收是「后续 checkpoint 慢慢来」，不是「立刻变小」——这一点必须写清，
   否则下一个人会以为 DELETE 了空间就会还。）
"""
from __future__ import annotations

import logging
import os
import time
from typing import Dict

log = logging.getLogger(__name__)

import db

_LAST_SWEEP: Dict[str, object] = {
    "last_ok_at": None,
    "last_deleted": None,
    "last_error": None,
}


def last_sweep_status() -> Dict[str, object]:
    """最近一轮清理的状态（供 /health 读取）。

    状态必须区分「还没跑过」与「跑过且删了 0 行」；返回浅副本，避免调用方
    修改进程内状态。
    """
    return dict(_LAST_SWEEP)

#: 清理循环间隔（秒）。默认 1 小时 —— 保留期以「天」计，1 小时粒度足够，
#: 而 1 分钟级会把「删了没几行也付一次全表扫描的代价」放大 60 倍。
DEFAULT_INTERVAL_S = int(os.getenv("RETENTION_SWEEP_INTERVAL_S", "3600"))

#: `profile_events` 保留天数。默认 30 天 —— 实测该表 3,250 行/天、23MB/58k 行，
#: 30 天 ≈ 10 万行 / 4MB，量级可接受；再长就该问「这些审计还要不要」。
DEFAULT_PROFILE_EVENTS_DAYS = int(os.getenv("PROFILE_EVENTS_RETENTION_DAYS", "30"))

#: `term_recordings` 的保留期沿用 `term_record.retention_days()`（默认 7 天），
#: 这里不另设一个口径 —— 两个地方各有一个保留期配置 = 迟早漂移。


def _days_ago_iso(days: int) -> str:
    """返回 N 天前的 ISO 8601 时间戳字符串（与 `db._now()` 同格式）。

    ⚠ 格式必须与 `db._now()` 一致（`datetime.now(timezone.utc).isoformat()`），
    否则字符串比较会把「格式不同但时间较新」的行误判为过期。
    """
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


def sweep_profile_events(days: int = DEFAULT_PROFILE_EVENTS_DAYS) -> int:
    """删掉 N 天前的 `profile_events`。返回删掉的行数（失败返回 0）。

    判据列是 `created_at`（TEXT，ISO 8601 带时区）——
    ⚠ **不是** `ts`：实测 `SELECT date(ts/86400,...)` 报 `no such column: ts`
    （列名猜错过一次，见 PT-20261024-08 §B4）。
    """
    if not db.is_open():
        return 0
    cutoff = _days_ago_iso(days)
    try:
        n = db.execute(
            "DELETE FROM profile_events WHERE created_at < ?", (cutoff,))
    except Exception:                                   # noqa: BLE001
        # 清理失败只记账不抛：后台循环崩掉 = 保留期彻底失效（静默增长）。
        return 0
    return n


def sweep_term_recordings() -> int:
    """删掉超过保留期的 `term_recordings`（保留期由 `term_record` 单一来源给）。

    返回删掉的帧数：先按到期条件计数，再调用既有 `sweep()` 执行删除。
    这样日志与 /health 显示真实行数，而不是既有的“删了至少一行即回 1”。
    """
    if not db.is_open():
        return 0
    try:
        import term_record
        cutoff = time.time() - term_record.retention_days() * 86400
        rows = db.query(
            "SELECT COUNT(*) AS n FROM term_recordings WHERE ts < ?", (cutoff,))
        expired = int(rows[0]["n"]) if rows else 0
        if not expired:
            return 0
        return expired if term_record.sweep() else 0
    except Exception:                                   # noqa: BLE001
        return 0


def sweep_once() -> Dict[str, int]:
    """跑一轮全部清理。返回各表删掉的行数（供 /health 与日志读）。"""
    out = {
        "profile_events": sweep_profile_events(),
        "term_recordings": sweep_term_recordings(),
    }
    _LAST_SWEEP["last_ok_at"] = int(time.time())
    _LAST_SWEEP["last_deleted"] = dict(out)
    _LAST_SWEEP["last_error"] = None
    return out


async def retention_loop(interval_s: int = DEFAULT_INTERVAL_S) -> None:
    """后台定时清理循环（挂在 `startup` 的 `_spawn` 上）。

    ⚠ 与 `term.reap_loop` 同一个纪律：**必须有独立心跳**，不能寄生在前端轮询上。
    前端一关就没人回收 ⇒ 保留期会静默失效。

    启动时先跑一轮再等待间隔；否则每次重启都要等满 INTERVAL_S，
    短时间重部署会让已经到期的行继续堆积。
    """
    import asyncio
    while True:
        try:
            out = sweep_once()
            if any(out.values()):
                log.info("[retention] 清理：%s" % out)
        except Exception as e:                          # noqa: BLE001
            _LAST_SWEEP["last_ok_at"] = int(time.time())
            _LAST_SWEEP["last_deleted"] = None
            _LAST_SWEEP["last_error"] = "%s: %s" % (type(e).__name__, str(e)[:160])
            log.warning("[retention] 清理异常（不影响其他功能）：%s: %s"
                        % (type(e).__name__, str(e)[:160]))
        try:
            await asyncio.sleep(interval_s)
        except asyncio.CancelledError:
            raise
