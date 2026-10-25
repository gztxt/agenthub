"""projects 适配器 —— 本机项目名录（PT-20261025-12 P0）。

复用 `localprojects._list_projects()`（git 扫描 + cloudcli 合并 + 派生 worktree 剔除），
**不复制扫描逻辑**——那套排除闸/根豁免/worktree 判定有 09-26 的判例在，
复制一份就会得到两份不同口径的项目清单。

**没有内容指纹 ⇒ 每轮全量重写**：项目总量 ~50 条（实测 roots 三根），
重写成本可忽略；引入 mtime 代理（`last_activity`）只为了排序与新鲜度展示。
"""
from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, fingerprint, register


def _epoch(value: Any) -> int | None:
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return int(value)
    try:
        return int(datetime.fromisoformat(
            str(value).replace("Z", "+00:00")).timestamp())
    except (TypeError, ValueError, OverflowError, OSError):
        return None


@register
class ProjectsAdapter(BaseAdapter):
    src_id = "projects.local"
    domain = "project"
    kind = "file"
    authority = 1
    adapter = "local_projects"
    scan_mode = "full"
    uri = "file://LOCALPROJECT_ROOTS"

    def probe(self) -> Dict[str, Any]:
        import os
        import localprojects as lp
        roots = list(lp.ROOTS)
        if not roots:
            return {"ok": False, "reason": "no_path",
                    "error": "LOCALPROJECT_ROOTS 为空"}
        exist = [r for r in roots if r and os.path.isdir(r)]
        if not exist:
            return {"ok": False, "reason": "no_dir",
                    "error": "配置的 %d 个项目根均不存在" % len(roots)}
        return {"ok": True, "reason": None, "error": None}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        import localprojects as lp

        res = lp._list_projects()
        projects = res.get("projects") or []
        # 根目录一个都不存在 ⇒ 本轮不许做消失判定（方案 §5）
        complete = bool(projects) or not res.get("errors")

        records: List[Dict[str, Any]] = []
        live: List[str] = []
        for p in projects:
            path = p.get("path") or ""
            if not path:
                continue
            nkey = path
            mtime = _epoch(p.get("last_activity"))
            digest = fingerprint(p)
            if self.skip(states, nkey, mtime, None, digest):
                live.append(_oid(nkey))
                continue
            records.append({
                "domain": "project", "type": "project", "native_key": nkey,
                "uri": "file://%s" % path,
                "title": str(p.get("name") or path)[:200],
                "summary": "%s%s" % (
                    "git 仓库" if p.get("git") else "目录",
                    "；cloudcli 会话 %d" % (p.get("sessions") or 0)
                    if p.get("cloudcli") else ""),
                "mtime": mtime,
                "size": None, "hash": digest,
                "ext": {"root": path, "name": p.get("name"),
                        "branch": None, "dirty": None,
                        "last_commit": None,
                        "worktrees": 1 if p.get("worktree") else 0},
                "meta": {"git": bool(p.get("git")),
                         "cloudcli": bool(p.get("cloudcli")),
                         "sessions": p.get("sessions"),
                         "roots": res.get("roots")},
                "chunks": [],
            })
            live.append(_oid(nkey))
        return {"records": records, "live": live, "complete": complete,
                "note": "项目 %d 条（roots %s，剔除 %d）" % (
                    len(records), len(res.get("roots") or []),
                    res.get("dropped_count") or 0)}


def _oid(native_key: str) -> str:
    from .. import schema
    return schema.oid_of("project", "projects.local", native_key)
