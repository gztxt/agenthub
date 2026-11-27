"""产物落地校验（2026-11-28，用户令 A：治「说做完了但其实没做」的假绿）。

【它补的是哪一层】
`verifier.py` 只判**回包文本**（nonempty / json_array / contains）——执行者回一句
"已写入 /tmp/x.txt" 就能过。而子任务的 `files` 字段本来声明了「会读或写的文件」，
却从没参与判定。本模块把 `files` 变成**可证伪**的判据：

  · 写类任务（prompt 含写入/创建/修改/删除/生成…）：声明文件必须存在、非空、
    **且至少一个在任务开始后被改过**（mtime >= since-5s）⇒ 否则是"说了没做"。
  · 只读类任务（prompt 无写动词）：只要求声明文件存在（读的目标本来就在）。

【为什么不是「一律要求改动」】
`LEADER_PROMPT` 的 files 语义是「会**读或写**」，纯检索/校验类子任务声明的是读目标。
一律要求改动会把合法的只读子任务判死（假红比假绿更烧钱：它触发重试并升档）。

【安全】
路径解析后必须**留在工作区内**；逃逸（`../..`、绝对路径指到工作区外）⇒
`insufficient` + `escape` gap，绝不 stat 到工作区外。这是判据面，不是权限面：
真正的权限由执行者的 cwd 与 workspace-write 沙箱管。

【与 verifier 的关系】
返回值形状与 `verifier.verify()` 一致（status/kind/gaps/repairable/detail），
`kind="artifacts"`。runner 只在它给出**确定的反证**（fail）时覆盖原 verdict；
`insufficient`（判不了）不覆盖 —— 不拿"没证据"当"失败"。
"""

import os
import re
from pathlib import Path
from typing import Any, Iterable, List, Optional

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INSUFFICIENT = "insufficient"

KIND = "artifacts"

#: 写类动词。宁可少认几个（漏判 = 退回旧 behavior），不硬认（误判 = 假红触发重试）。
WRITE_HINT = re.compile(
    r"(写入|写到|保存到|落盘|创建|新建|生成|修改|改动|改成|改为|删除|移除|重命名|追加"
    r"|write|create|save|append|delete|remove|rename|patch|modif)", re.I)

#: mtime 容差：文件系统粒度 + 任务起跑与写盘之间的亚秒错位（实测需 2~5s）。
_MTIME_SLACK_S = 5.0
#: 非空判据：1 字节以上的文件才算"有产物"（空文件通常是 create 了一半）。
_MIN_BYTES = 1


def _gap(gid: str, detail: str) -> dict:
    return {"id": gid, "detail": detail}


def _verdict(status: str, gaps: Optional[List[dict]], repairable: bool, detail: str) -> dict:
    return {"status": status, "kind": KIND, "gaps": gaps or [],
            "repairable": bool(repairable), "detail": detail}


def is_write_task(prompt: Optional[str]) -> bool:
    return bool(WRITE_HINT.search(str(prompt or "")))


def normalize(files: Any) -> List[str]:
    """`files` 可能是 JSON 字符串、list、或 None（老行）。统一成去重后的路径列表。"""
    import json
    if files is None:
        return []
    if isinstance(files, str):
        raw = files.strip()
        if not raw:
            return []
        try:
            val = json.loads(raw)
        except (ValueError, TypeError):
            val = [raw]
    else:
        val = files
    if not isinstance(val, (list, tuple)):
        return []
    out: List[str] = []
    for item in val:
        p = str(item or "").strip()
        if p and p not in out:
            out.append(p)
    return out


def _inside(root: Path, p: Path) -> bool:
    try:
        p.resolve().relative_to(root.resolve())
        return True
    except (ValueError, OSError):
        return False


def check(files: Any, *, cwd: Optional[str] = None, since: Optional[float] = None,
          prompt: Optional[str] = None) -> dict:
    """校验声明的产物。返回 verifier 形状的 verdict。

    files 为空 ⇒ `insufficient`（没声明就没得判，交回原 verifier）。
    """
    paths = normalize(files)
    if not paths:
        return _verdict(STATUS_INSUFFICIENT, [_gap("no_files", "子任务没声明 files")],
                        False, "没有声明产物，判不了")
    root = Path(cwd or os.getcwd())
    write_task = is_write_task(prompt)
    gaps: List[dict] = []
    touched = 0
    seen = 0
    for rel in paths:
        target = Path(rel)
        if not target.is_absolute():
            target = root / rel
        if not _inside(root, target):
            return _verdict(STATUS_INSUFFICIENT,
                            [_gap("escape", "%s 解析后不在工作区内" % rel)],
                            False, "产物路径越界，判不了")
        if not target.exists():
            gaps.append(_gap("missing", "%s 不存在" % rel))
            continue
        seen += 1
        try:
            size = target.stat().st_size if target.is_file() else 1
            mtime = target.stat().st_mtime
        except OSError as e:  # noqa: BLE001
            gaps.append(_gap("unreadable", "%s 读不到（%s）" % (rel, type(e).__name__)))
            continue
        if size < _MIN_BYTES:
            gaps.append(_gap("empty", "%s 是空文件" % rel))
        if since and mtime + _MTIME_SLACK_S >= since:
            touched += 1
    if write_task and since and seen and touched == 0:
        gaps.append(_gap("not_modified",
                         "写类任务但声明文件在任务开始后一个都没被改过（mtime 全是旧的）"))
    if gaps:
        return _verdict(STATUS_FAIL, gaps, True,
                        "产物未落地：%s" % "; ".join(g["id"] for g in gaps))
    return _verdict(STATUS_PASS, [], False,
                    "产物齐全（%d 个路径，%d 个在任务内更新）" % (seen, touched))


def enabled() -> bool:
    """`HUB_VERIFY_ARTIFACTS=off` 可关（老行为 = 只看回包文本）。默认开。"""
    return (os.getenv("HUB_VERIFY_ARTIFACTS", "on") or "on").strip().lower() not in (
        "0", "false", "no", "off")


def known_kinds() -> Iterable[str]:
    return (KIND,)