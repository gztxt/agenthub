"""内存口径可观测：把「hub 本体」与「hub 托管的」分开报（PT-20261024-08 批次 B5）。

## 为什么需要

2026-10-24 实测（判例 `agent-knowledge/259`）：**同一个服务的三个口径差 7 倍**：

| 口径 | 实测 |
|---|---|
| 进程 RSS（`ps -eo rss`） | **232 MB** |
| cgroup 内进程 RSS 合计 | **753 MB**（本体 232 + 托管的 codebuddy CLI 485 + …） |
| cgroup `MemoryCurrent` | **1.4~2.0 GB**（比进程合计还大 ⇒ 差额是 **page cache**：读 `memindex.db` 3.6GB / `agents.db` 780MB 留下的文件缓存，**也记在服务账上**） |

⇒ 只报「hub 占多少内存」会**同时误导两个方向**：
按进程口径看会低估（漏掉托管的 agent CLI 与文件缓存），
只看 cgroup 总量又看不出「是本体大还是被托管的进程大」。

## 三个字段的口径

- `rss_self_kb`：本进程 RSS（**进程口径**，与 `ps -eo rss` 一致）
- `cgroup_current_bytes`：cgroup `memory.current`（**记账口径**，含 page cache）
- `cgroup_children_rss_kb`：cgroup 内**除自己**的进程 RSS 合计（**托管口径**）

三者一起看，才能答出「本体多大 / 托管了多少 / 文件缓存多少」。

## 口径纪律（判例 259 的直接教训）

- **不报单次读数当结论**：本模块的每个字段都是**读数**，不是常量。
  `cgroup_current_bytes` 在 agent CLI 启停时会剧烈波动（实测区间 1.4~2.0GB）。
  ⇒ `/health` 的字段名带 `_bytes`/`_kb` 后缀（**明确是读数**），
  且前端/告警若要用它做判据，**必须先量自己的方差**（轮间极差 < |两读数之差|）。
- **拿不到就报 `null`，不报 0**：0 与「测不到」是两件事（把「测不到」写成 0
  = 宣称「内存占用为零」，是本仓反复出现的「全绿而事实已死」形态）。
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Optional


def _self_rss_kb() -> Optional[int]:
    """本进程 RSS（KB）。取不到报 None。"""
    try:
        with open("/proc/self/statm", "r", encoding="utf-8") as f:
            pages = int(f.read().split()[1])
        # Linux 的 statm 第二列是**页数**，页大小通常 4KB
        return pages * (os.sysconf("SC_PAGE_SIZE") // 1024)
    except Exception:                                   # noqa: BLE001
        return None


def _cgroup_dir() -> Optional[Path]:
    """定位本进程所属的 cgroup 目录（v2 口径）。

    优先 `/sys/fs/cgroup/<path from /proc/self/cgroup>`；
    找不到或不是 v2 ⇒ 报 None（**不猜**）。
    """
    try:
        txt = Path("/proc/self/cgroup").read_text(encoding="utf-8")
    except Exception:                                   # noqa: BLE001
        return None
    for line in txt.splitlines():
        # v2 格式：0::<相对路径>
        if line.startswith("0::"):
            rel = line[3:].strip()
            p = Path("/sys/fs/cgroup") / rel.lstrip("/")
            return p if p.is_dir() else None
    return None


def _cgroup_current_bytes() -> Optional[int]:
    """cgroup `memory.current`（字节）。v1 则读 `memory.usage_in_bytes`。"""
    cg = _cgroup_dir()
    if cg is None:
        return None
    for name in ("memory.current", "memory.usage_in_bytes"):
        f = cg / name
        try:
            if f.is_file():
                return int(f.read_text().strip())
        except Exception:                               # noqa: BLE001
            continue
    return None


def _cgroup_children_rss_kb() -> Optional[int]:
    """cgroup 内**除自己**的进程 RSS 合计（KB）。

    读 `cgroup.procs` 逐个查 `/proc/<pid>/statm`。
    ⚠ 竞态：进程可能在读的过程中退出 ⇒ 单个 pid 读不到就**跳过**（不当作 0）。
    """
    cg = _cgroup_dir()
    if cg is None:
        return None
    procs = cg / "cgroup.procs"
    try:
        if not procs.is_file():
            return None
        pids = procs.read_text().split()
    except Exception:                                   # noqa: BLE001
        return None
    total = 0
    self_pid = str(os.getpid())
    page_kb = os.sysconf("SC_PAGE_SIZE") // 1024
    for pid in pids:
        if pid == self_pid:
            continue
        try:
            with open("/proc/%s/statm" % pid, "r", encoding="utf-8") as f:
                total += int(f.read().split()[1]) * page_kb
        except Exception:                               # noqa: BLE001
            continue                                    # 进程已退出 ⇒ 跳过
    return total


def memory_snapshot() -> dict:
    """返回三个口径的读数。拿不到的字段是 `None`（**不是 0**）。"""
    return {
        "rss_self_kb": _self_rss_kb(),
        "cgroup_current_bytes": _cgroup_current_bytes(),
        "cgroup_children_rss_kb": _cgroup_children_rss_kb(),
    }
