"""docs 适配器 —— wiki + 顶层文档扫描（PT-20261025-12 P1）。

扫描 `wiki/` 和顶层 `*.md` 文件，提取标题、正文、字数，分块索引。
跳过聚合页（wiki/index.md 等）—— 这些是全站链接清单，FTS 命中会天然霸榜。
"""
from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any, Dict, List, Tuple

from ..adapter import BaseAdapter, register
from ..schema import chunk_text, oid_of


# 聚合页黑名单：正文 >5 万字的链接清单，天然霸榜，必须排除
AGGREGATION_PAGES = {
    "wiki/index.md",
    "wiki/concepts/_index.md",
    "wiki/entities/_index.md",
    "wiki/log.md",
    "MEMORY.md",
    ".skills-index.md",
    "项目详细清单.md",
    "PENDING-TASKS.md",
}


def _is_agg_page(filepath: str) -> bool:
    """判断是否为聚合页（链接清单）。"""
    return filepath in AGGREGATION_PAGES or any(
        filepath.endswith(suffix) for suffix in [
            "/_index.md", "/_index.MD",
        ]
    )


#: 工作区根：接口只收这个根的「权威原文」，不收备份盘/派生物/依赖树。
WORKSPACE = "/fs/1000/ftp/技术文档"

#: 目录名命中即整棵子树跳过。
#: `.mnt-jishu` 是另一台系统的备份盘（AGENTS.md 明令不读）；其余是依赖与中间产物。
SKIP_DIR_NAMES = {
    ".mnt-jishu", ".git", "venv", "node_modules", "__pycache__",
    "_stale", ".skill_backup", "worktrees", "会话备份",
}

#: 相对工作区的路径前缀命中即跳过。
#: `会话备份/` 归 `sessions.archived`（同一批 md 若两处都收，查询里一条会话出现两次）。
SKIP_REL_PREFIXES = ("会话备份/", "agenthub/work/")


def _iter_markdown(root: str):
    """遍历 markdown，**就地剪枝**（不进 .mnt-jishu / node_modules / venv）。

    旧实现用 `rglob`，会真的走进备份盘与依赖树再逐个丢弃：2026-11-04 实测
    一次 docs 扫描走了 15 分钟（.mnt-jishu 是另一台机器的挂载，node_modules
    有 1.5 万个 md）。剪枝后同一个根只走内容目录。
    """
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = sorted(d for d in dirnames if d not in SKIP_DIR_NAMES)
        for name in sorted(filenames):
            if name.endswith(".md"):
                yield Path(dirpath) / name


def _skip_path(filepath: str) -> str:
    """返回跳过原因（空串 = 收录）。

    `docs.filesystem` 是**权威原文**源；把备份副本、`.bak-*`、venv 一起收进来，
    同一条查询会返回同一篇文档的 2~3 个副本（2026-11-04 实测 `统一编目` 前两条
    就是 `.mnt-jishu` 与源端各一份），既虚增库体又污染排序。
    """
    p = Path(filepath)
    rel = p.relative_to(WORKSPACE).as_posix() if filepath.startswith(
        WORKSPACE + "/") else p.as_posix()
    if any(part in SKIP_DIR_NAMES for part in p.parts):
        return "skip_dir"
    if rel.startswith(SKIP_REL_PREFIXES):
        return "derived_owner"
    if ".bak-" in p.name:
        return "backup_copy"
    return ""


def _extract_title(content: str) -> str:
    """从 markdown 内容提取一级标题。"""
    match = re.search(r'^#\s+(.+)$', content, re.MULTILINE)
    if match:
        return match.group(1).strip()[:200]
    return ""


def _strip_tables(content: str) -> str:
    """去除表格行，减少噪声。"""
    lines = []
    for line in content.split('\n'):
        if re.match(r'^\|[-\s|:]+\|$', line.strip()):
            continue  # 分隔线
        if line.strip().startswith('|') and '---' not in line:
            continue  # 表格数据行
        lines.append(line)
    return '\n'.join(lines)


@register
class DocsAdapter(BaseAdapter):
    src_id = "docs.filesystem"
    domain = "doc"
    kind = "file"
    authority = 1  # wiki/docs 在本仓即权威
    adapter = "filesystem_docs"
    scan_mode = "incremental"
    uri = "file://wiki/** + 技术文档/*.md"

    def probe(self) -> Dict[str, Any]:
        root = Path(WORKSPACE)
        if not root.exists():
            return {"ok": False, "reason": "no_dir", "error": "工作区不存在"}
        # 只走一个根：`wiki/` 是它的子目录，两个根会把每篇 wiki 收两遍
        uniq = list(_iter_markdown(str(root)))
        agg = sum(1 for p in uniq if _is_agg_page(str(p)))
        skipped = sum(1 for p in uniq
                      if not _is_agg_page(str(p)) and _skip_path(str(p)))
        total = len(uniq)
        return {"ok": True, "reason": None, "error": None,
                "total": total, "excluded": agg, "skipped": skipped,
                "indexable": total - agg - skipped}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        records: List[Dict[str, Any]] = []
        live: List[str] = []
        notes: List[str] = []
        excluded_count = 0
        skipped: Dict[str, int] = {}

        for base in [WORKSPACE]:
            try:
                root = Path(base)
                if not root.exists():
                    continue
                for filepath in _iter_markdown(str(root)):
                    fp_str = str(filepath)

                    # 跳过聚合页
                    if _is_agg_page(fp_str):
                        excluded_count += 1
                        continue

                    # 跳过备份副本 / 派生物 / 依赖树（见 _skip_path）
                    skip_reason = _skip_path(fp_str)
                    if skip_reason:
                        skipped[skip_reason] = skipped.get(skip_reason, 0) + 1
                        continue

                    try:
                        mtime = int(filepath.stat().st_mtime)
                        size = filepath.stat().st_size
                        content = filepath.read_text(encoding="utf-8", errors="replace")
                    except OSError as e:
                        notes.append(f"{filepath}: {e}")
                        continue

                    nkey = fp_str
                    digest = self._hash(nkey, mtime, size, content[:500])
                    if self.skip(states, nkey, mtime, size, digest):
                        live.append(_oid(nkey))
                        continue

                    title = _extract_title(content) or filepath.stem[:200]
                    clean_content = _strip_tables(content)
                    summary = clean_content[:3000]
                    word_count = len(clean_content.split())
                    chunks = chunk_text(clean_content)

                    records.append({
                        "domain": "doc",
                        "type": "doc",
                        "native_key": nkey,
                        "uri": f"file://{filepath}",
                        "title": title,
                        "summary": summary,
                        "mtime": mtime,
                        "size": size,
                        "hash": digest,
                        "ext": {
                            "section": None,
                            "slug": filepath.stem,
                            "words": word_count,
                            "tags": None,
                            "filepath": str(filepath),
                        },
                        "meta": {"source": "wiki" if "wiki/" in fp_str else "root"},
                        "chunks": chunks,
                    })
                    live.append(_oid(nkey))

            except Exception as e:
                notes.append(f"{base}: {e}")

        return {
            "records": records,
            "live": live,
            "complete": True,
            "note": f"文档 {len(records)} 条（聚合页排除 {excluded_count}；"
                    f"非权威/派生物跳过 {sum(skipped.values())}"
                    + (f"={skipped}" if skipped else "") + "）" +
                    (f"；错误：{','.join(notes)}" if notes else ""),
        }

    @staticmethod
    def _hash(nkey: str, mtime: int, size: int, preview: str) -> str:
        import hashlib
        h = hashlib.sha256()
        h.update(f"{nkey}|{mtime}|{size}|{preview}".encode())
        return h.hexdigest()[:16]


def _oid(native_key: str) -> str:
    return oid_of("doc", "docs.filesystem", native_key)
