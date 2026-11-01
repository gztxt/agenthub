"""docs 适配器 —— wiki + 顶层文档扫描（PT-20261025-12 P1）。

扫描 `wiki/` 和顶层 `*.md` 文件，提取标题、正文、字数，分块索引。
跳过聚合页（wiki/index.md 等）—— 这些是全站链接清单，FTS 命中会天然霸榜。
"""
from __future__ import annotations

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
        paths = []
        for base in ["/fs/1000/ftp/技术文档/wiki", "/fs/1000/ftp/技术文档"]:
            p = Path(base)
            if p.exists():
                paths.extend(list(p.rglob("*.md")) + list(p.glob("*.md")))
        total = len(paths)
        excluded = sum(1 for p in paths if _is_agg_page(str(p)))
        return {"ok": True, "reason": None, "error": None,
                "total": total, "excluded": excluded,
                "indexable": total - excluded}

    def collect(self, states: Dict[str, Tuple[Any, Any, Any]]
                ) -> Dict[str, Any]:
        records: List[Dict[str, Any]] = []
        live: List[str] = []
        notes: List[str] = []
        excluded_count = 0

        for base in ["/fs/1000/ftp/技术文档/wiki", "/fs/1000/ftp/技术文档"]:
            try:
                root = Path(base)
                if not root.exists():
                    continue
                for filepath in sorted(root.rglob("*.md")):
                    fp_str = str(filepath)

                    # 跳过聚合页
                    if _is_agg_page(fp_str):
                        excluded_count += 1
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
            "note": f"文档 {len(records)} 条（聚合页排除 {excluded_count}）" +
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
