"""派发产物落点守卫（PT-20261127-02 → v0.13.223）。

## 为什么要有这一层

2026-11-27 派发实测：agent 把中间文件写进 `/tmp/magpie_models_raw.json`，而 `/tmp`
在重启后即失。当时只有**口头约定**（prompt 里写「产物落 work/」），agent 可忽略 ——
口头约定不是强制，判据见 `agent-knowledge/879` 同族教训（规则只存在文本里 = 没生效）。

## 三层强制（从软到硬，任一层命中即算拦住）

1. **环境层**：把子进程的 `TMPDIR`/`TMP`/`TEMP` 指到 `work/tmp/<run>/<task>/`。
   凡尊重 POSIX 临时目录约定的工具（`mktemp`、Python `tempfile`、多数构建工具）
   自动落对位置，**不改 agent 任何代码**。
2. **文本层**：在派发 prompt 末尾追加落点契约（绝对路径）。这是**声明**，不是强制，
   所以必须有第 3 层兜底。
3. **事后层**：任务结束扫 `/tmp` 顶层的**新增**条目：
   - 名字含 `run_id` / `task_id` ⇒ **搬到** `work/artifacts/<run>/<task>/`（真删掉 /tmp 副本）；
   - 其它新增 ⇒ **只记事件** `tmp_leak`（不搬：并发会话/别的进程的文件不能动）。

## 纪律

- 任何一步失败都不许带倒派发（守卫是增强层，失败只 print 一行）。
- 只处理 `/tmp` **顶层**的**普通文件**，不动目录/软链（避免把别人的工作区搬走）。
- `since` 用「任务开始时刻 − 5s」：文件系统时间戳精度与时钟偏差都留余量。
"""
from __future__ import annotations

import os
import re
import shutil
import threading
import time
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

#: 仓库根（本文件在 src/ 下）——`work/` 与它同级，且已被 .gitignore 排除。
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
WORK = os.path.join(ROOT, "work")

#: /tmp 顶层快照的进程内锁：并发任务会同时 listdir，虽然结果幂等，但搬文件时要串行。
_TMP_LOCK = threading.Lock()

_SAFE = re.compile(r"[^0-9A-Za-z._-]")


def _seg(value: Any) -> str:
    """run_id/task_id 只允许 `[0-9A-Za-z._-]`；其它字符替换成 `_`（防路径穿越）。"""
    s = _SAFE.sub("_", str(value or "").strip())
    return s[:64] or "unknown"


def task_tmp(run_id: Any, task_id: Any) -> str:
    return os.path.join(WORK, "tmp", _seg(run_id), _seg(task_id))


def task_artifacts(run_id: Any, task_id: Any) -> str:
    return os.path.join(WORK, "artifacts", _seg(run_id), _seg(task_id))


def env(run_id: Any, task_id: Any) -> Dict[str, str]:
    """给子进程的环境变量：临时目录 + 产物目录（都是绝对路径）。"""
    tmp = task_tmp(run_id, task_id)
    art = task_artifacts(run_id, task_id)
    try:
        os.makedirs(tmp, exist_ok=True)
        os.makedirs(art, exist_ok=True)
    except OSError:
        pass          # 建不出来也照样给路径：子进程自己会再建一次
    return {"TMPDIR": tmp, "TMP": tmp, "TEMP": tmp, "HUB_TASK_ARTIFACT_DIR": art}


def file_prefix(run_id: Any, task_id: Any) -> str:
    """本次派发的产物文件名前缀（可归因）。"""
    return "%s-%s-" % (_seg(run_id), _seg(task_id))


def contract_note(run_id: Any, task_id: Any) -> str:
    """追加到派发 prompt 末尾的落点契约（含绝对路径，agent 不需要推导）。"""
    return ("\n\n[产物落点] 中间文件与最终产物一律写到 %s/（hub 已把 TMPDIR 指向 %s/）。\n"
            "文件名请以 `%s` 开头，便于归属核算。\n"
            "禁止写 /tmp：任务结束后 /tmp 顶层新增且含本次 run 标识的文件会被搬到 %s/，\n"
            "其它新增会被记为 tmp_leak 事件。重启会清空 /tmp，写那里等于产物丢失。"
            % (task_artifacts(run_id, task_id), task_tmp(run_id, task_id),
               file_prefix(run_id, task_id), task_artifacts(run_id, task_id)))


def snapshot_tmp(path: str = "/tmp") -> Set[str]:
    """`/tmp` 顶层条目名快照（只看名字，读不到就返回空集）。"""
    try:
        return set(os.listdir(path))
    except OSError:
        return set()


def _is_relocate(name: str, run_id: str, task_id: str) -> bool:
    """归属判据：文件名里出现本次 **run_id** 才允许搬。

    为什么不认 `task_id`：`t1`/`t2` 这种两字符串在 /tmp 里遍地都是（`pytest-1`、
    `test1`…），拿它判归属等于把别人的文件搬走。run_id 是 10 位 hex，撞名概率可忽略；
    `run-task-` 前缀是契约里要求 agent 使用的命名（更强的归属信号）。
    """
    n = name or ""
    if not run_id:
        return False
    if run_id in n:
        return True
    return bool(task_id and ("%s-%s-" % (run_id, task_id)) in n)


def sweep_run(run_id: Any, task_id: Any, before: Optional[Iterable[str]] = None,
              since: Optional[float] = None, path: str = "/tmp",
              max_items: int = 200) -> List[Dict[str, str]]:
    """任务结束后清理归属本任务的 /tmp 产物；返回动作清单。

    返回 `[{"name":..., "action":"moved|flagged", "to":...}]`，**不抛异常**。
    """
    out: List[Dict[str, str]] = []
    prev = set(before or ())
    t0 = float(since) - 5.0 if since else 0.0
    rid, tid = _seg(run_id), _seg(task_id)
    with _TMP_LOCK:
        try:
            names = os.listdir(path)
        except OSError:
            return out
        # ⚠ 2026-11-27 实测：把 `names[:max_items]` 截断会在 /tmp 有 3,422 项时漏掉
        # 目标文件（本次产物排在第 1 但也可能任意位置）⇒ **必须扫全集**，
        # max_items 只限制返回条数。
        for name in names:
            if len(out) >= max_items:
                break
            full = os.path.join(path, name)
            try:
                if os.path.islink(full) or not os.path.isfile(full):
                    continue
                st = os.stat(full)
                if prev and name in prev:
                    continue                       # 任务开始前就有的，不是本次产物
                if since and st.st_mtime < t0:
                    continue                       # 时间窗外的旧文件
            except OSError:
                continue
            if _is_relocate(name, rid, tid):
                dest_dir = task_artifacts(run_id, task_id)
                try:
                    os.makedirs(dest_dir, exist_ok=True)
                    dest = os.path.join(dest_dir, name)
                    if os.path.exists(dest):
                        dest = os.path.join(dest_dir, "%d-%s" % (int(time.time()), name))
                    shutil.move(full, dest)
                    out.append({"name": name, "action": "moved", "to": dest})
                    continue
                except OSError:
                    pass                            # 搬不动就退化为「记录」
            out.append({"name": name, "action": "flagged", "to": ""})
    return out


def scope_from(kwargs: Dict[str, Any]) -> Tuple[str, str]:
    """从 adapter 的 kwargs 取 (run_id, task_id)。

    首选显式 `run_id`/`task_id`；否则解析派发 tag `task-<run>-<task>`
    （`tasks_runner._call_chat` 的既有格式）。都取不到 ⇒ `("","")` = 不启用守卫
    （交互式终端调用不能被当成派发任务）。
    """
    rid = str(kwargs.get("run_id") or "").strip()
    tid = str(kwargs.get("task_id") or "").strip()
    tag = str(kwargs.get("tag") or "").strip()
    if not (rid and tid) and tag.startswith("task-"):
        body = tag[len("task-"):]
        # run_id 是 10 位 hex 前缀，task_id 是 t1/t2/t_sum…：从右边切最后一个 '-'
        if "-" in body:
            rid2, tid2 = body.rsplit("-", 1)
            rid = rid or rid2
            tid = tid or tid2
    return rid, tid