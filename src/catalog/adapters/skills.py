"""skills 适配器 —— 本机技能名录（PT-20261025-12 P0）。

**复用 `skill` 模块的扫描**（`disk_routes()` / `route_roots()` / `_scan_many()` /
`_dedup()`），**不复制第二份实现**——那份实现带着一整套安全逻辑
（realpath 白名单防软链越界、部分根失败不算整路失败、嵌套文档识别），
复制一遍就等于把这些判据重新实现一遍，必然漂移。

**只读 frontmatter，正文按需分块**：`description` + `triggers` + frontmatter 键名
进分块索引（约 200~600 字/技能），正文全文不进——技能正文可能几 KB，
而检索场景 99% 是「按描述/触发词找技能」。
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register


@register
class SkillsAdapter(BaseAdapter):
    src_id = "skills.disk"
    domain = "skill"
    kind = "file"
    authority = 1          # 技能真相在各 agent 家目录，本 src 即权威入口
    adapter = "disk_skills"
    scan_mode = "incremental"
    uri = "file://SKILL_DIRS（15+ 个 agent 家目录）"

    def probe(self) -> Dict[str, Any]:
        import os
        import skill
        roots = []
        for r in skill.disk_routes():
            roots.extend(skill.route_roots(r))
        exist = [r for r in roots if r and os.path.isdir(r)]
        if not roots:
            return {"ok": False, "reason": "no_path",
                    "error": "SKILL_DIRS 未配置任何技能目录"}
        if not exist:
            return {"ok": False, "reason": "no_dir",
                    "error": "配置的 %d 个技能目录均不存在" % len(roots)}
        return {"ok": True, "reason": None, "error": None}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        import skill

        items: List[Dict[str, Any]] = []
        complete = True
        notes: List[str] = []
        for route in skill.disk_routes():
            res = skill._scan_many(route, skill.route_roots(route))
            items.extend(res.get("items") or [])
            if not res.get("ok"):
                # 部分根失败不算整路失败（skill 模块自己的口径），但 complete 要降级：
                # 这一路有可能漏条目 ⇒ 本轮不许做消失判定。
                complete = False
                notes.append("%s:%s" % (route, res.get("reason")))
        unique, aliases = skill._dedup(items)

        records: List[Dict[str, Any]] = []
        live: List[str] = []
        for it in unique:
            if it.get("nested"):
                # 嵌套子章节归父级管，不进可选名录（与 /api/skill/list 同口径）
                continue
            rp = it.get("realpath") or it.get("path")
            nkey = rp
            if self.skip(states, nkey, it.get("mtime"), it.get("bytes"),
                         it.get("pkg_digest")):
                live.append(_oid(nkey))
                continue
            # 分块内容：描述 + 触发词 + frontmatter 键（可检索面，非全文）
            text = " ".join(x for x in [
                it.get("name") or "", it.get("description") or "",
                it.get("triggers") or "", " ".join(it.get("fm_keys") or []),
                rp,
            ] if x).strip()
            records.append({
                "domain": "skill", "type": "skill", "native_key": nkey,
                "uri": "file://%s" % (it.get("path") or rp),
                "title": str(it.get("name") or "")[:200],
                "summary": (it.get("description") or "")[:500],
                "lang": "zh" if any("\u4e00" <= c <= "\u9fff"
                                   for c in text[:200]) else "en",
                "mtime": it.get("mtime"), "size": it.get("bytes"),
                "hash": it.get("pkg_digest"),
                "ext": {"name": it.get("name"), "route": it.get("route"),
                        "dir": it.get("path"),
                        "description": (it.get("description") or "")[:500],
                        "tokens": None, "enabled": 1},
                "meta": {"via_symlink": bool(it.get("via_symlink")),
                         "fm": bool(it.get("fm")),
                         "pkg_files": it.get("pkg_files"),
                         "routes": it.get("routes"),
                         "aliases": len(aliases)},
                "chunks": [text] if text else [],
            })
            live.append(_oid(nkey))
        return {"records": records, "live": live, "complete": complete,
                "note": "技能 %d 条（别名 %d）%s" % (
                    len(records), len(aliases),
                    ("；部分根未完整扫：" + ",".join(notes)) if notes else "")}


def _oid(native_key: str) -> str:
    from .. import schema
    return schema.oid_of("skill", "skills.disk", native_key)
