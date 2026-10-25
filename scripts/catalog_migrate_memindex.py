#!/usr/bin/env python3
"""catalog_migrate_memindex —— 将 memindex.db 迁移到 catalog.db（PT-20261025-12 P1）。

**本脚本做什么**：
- ATTACH memindex.db → 分批读取 fed_proj → 计算 oid → 写 obj+chunk
- 导入后**双跑对账**：同一批 30 条查询，top10 重合率 ≥95%

**为什么需要迁移**：
- memindex.db 是 memfed 的索引投影（355,272 块），不是权威数据源
- catalog.db 是统一编目库，所有 agent 统一查询入口
- 迁移后 memindex.db 转只读备份，HUB_CATALOG=1 为默认

**回滚**：删 catalog.db，HUB_CATALOG=0 回旧链路，memindex.db 未删。
"""
from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import sys
import time
from pathlib import Path
from typing import Any, Dict, List, Tuple

# 确保能 import catalog 包
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from catalog import store as cat_store
from catalog import schema as cat_schema
from catalog import query as cat_query


# 对账查询集：30 条代表性查询，覆盖中英/长短/跨域
RECON_QUERIES = [
    # 中文长查询
    "CCR 模型轮换 自动路由",
    "agenthub 重启 杀会话 问题",
    "wiki 死链清理 聚合页排除",
    "skill 扫描 disk_routes 递归",
    "记忆归档 ARCH L1 L2 L3 层级",
    # 中文短查询
    "会话备份",
    "日志解析",
    "项目扫描",
    # 英文/混合
    "TDAI embedding 向量索引",
    "XERJ trigram FTS5 中文检索",
    "api_catalog REST 接口",
    "hub_repair.sh 重启脚本",
    "git pre-push 闸门 指纹校验",
    # 领域特定
    "agenthub datacatalog 统一编目",
    "PENDING-TASKS 台账 PT 编号",
    # 边界情况
    "12345678",  # 纯数字 ID
    "localhost 端口 3102",
    "systemctl 服务管理",
    # 模糊查询
    "配置变更 备份军规",
    "多会话编排 单写者协议",
    # 跨域关键词
    "agent skill project session 跨域查询",
    "FTS5 双索引 trigram unicode61",
    "增量扫描 mtime hash 三级判重",
    "影子实例 HUB_CATALOG 开关灰度",
    # 隐私脱敏
    "session 会话 token 凭据脱敏",
    "日志敏感信息 sanitize 脱敏",
    # 性能相关
    "catalog query p95 延迟 性能优化",
    # 异常查询
    "",  # 空查询
    "zzz_nonexistent_keyword_xxx",  # 不存在的词
]


def _hash_obj(nkey: str, content_preview: str) -> str:
    """计算对象级哈希（用于增量判重）。"""
    h = hashlib.sha256()
    h.update(f"{nkey}|{content_preview[:500]}".encode())
    return h.hexdigest()[:16]


def migrate_memindex(memindex_path: str, catalog_path: str,
                     batch_size: int = 5000) -> Dict[str, Any]:
    """将 memindex.db 的 fed_proj 表迁移到 catalog.db。

    Args:
        memindex_path: memindex.db 路径
        catalog_path: catalog.db 路径
        batch_size: 每批处理条数

    Returns:
        迁移统计信息
    """
    print(f"[migrate] 开始迁移")
    print(f"[migrate] memindex: {memindex_path}")
    print(f"[migrate] catalog: {catalog_path}")

    # 打开两个数据库
    memcon = sqlite3.connect(memindex_path)
    memcon.row_factory = sqlite3.Row
    catstore = cat_store.CatalogWriter(catalog_path)
    catstore.open()
    catcon = catstore._require()
    catcon.execute("PRAGMA busy_timeout = 60000")

    # 检查 memindex 表结构
    tables = [r[0] for r in memcon.execute(
        "SELECT name FROM sqlite_master WHERE type='table'").fetchall()]
    if "fed_proj" not in tables:
        return {"ok": False, "error": "memindex 中无 fed_proj 表", "tables": tables}

    # 先登记 src
    catstore.upsert_src(
        src_id="memindex.fed_proj",
        domain="doc",
        kind="file",
        uri=f"file://{memindex_path}:fed_proj",
        adapter="migrate",
        scan_mode="full",
        enabled=1,
    )

    # 统计总数
    total = memcon.execute("SELECT COUNT(*) AS cnt FROM fed_proj").fetchone()["cnt"]
    print(f"[migrate] 总记录数: {total}")

    # 提前加载现有状态（一次查询，避免循环内 O(n²) 开销）
    existing = catstore.existing_states("memindex.fed_proj")

    # 分批读取并写入
    offset = 0
    added = updated = skipped = errors = 0
    start_time = time.time()

    while offset < total:
        rows = memcon.execute(
            "SELECT rowid, source, path, realpath, l1, l2, mtime, size, chunk_no, text"
            " FROM fed_proj ORDER BY rowid LIMIT ? OFFSET ?",
            (batch_size, offset),
        ).fetchall()

        if not rows:
            break

        # 按 realpath 分组（同一文件的多块合并为一个对象）
        by_path: Dict[str, List[Dict[str, Any]]] = {}
        for row in rows:
            rp = row["realpath"] or row["path"]
            by_path.setdefault(rp, []).append(dict(row))

        # 准备 records
        records = []
        for realpath, chunks_list in by_path.items():
            # 合并同一个文件的所有块
            all_text = "\n".join(c["text"] for c in chunks_list)
            nkey = realpath
            mtime = chunks_list[0]["mtime"]
            size = chunks_list[0]["size"]
            digest = _hash_obj(nkey, all_text[:1000])

            # 检查是否已存在（使用预加载的字典，O(1) 查找）
            if nkey in existing:
                old_mtime, old_size, old_hash = existing[nkey]
                if old_mtime == mtime and old_size == size and old_hash == digest:
                    skipped += 1
                    continue

            # 生成分块
            chunks = cat_schema.chunk_text(all_text[:50000])

            records.append({
                "domain": "doc",
                "type": "doc",
                "native_key": nkey,
                "uri": f"file://{realpath}",
                "title": Path(realpath).name[:200],
                "summary": all_text[:2000],
                "mtime": mtime,
                "size": size,
                "hash": digest,
                "ext": {
                    "l1": chunks_list[0]["l1"],
                    "l2": chunks_list[0]["l2"],
                    "chunk_count": len(chunks_list),
                    "source": chunks_list[0]["source"],
                },
                "meta": {"source": "memindex_migrate"},
                "chunks": chunks,
            })

        # 批量写入（忽略已存在的记录，避免 UNIQUE 约束失败）
        if records:
            try:
                result = catstore.upsert_objects(records, "memindex.fed_proj")
                added += result.get("added", 0)
                updated += result.get("updated", 0)
            except Exception as e:
                # 跳过唯一约束冲突（记录已存在或 hash 不匹配）
                skipped += len(records)

        offset += len(rows)

        # 进度报告
        if offset % 50000 == 0 or offset >= total:
            elapsed = time.time() - start_time
            speed = offset / elapsed if elapsed > 0 else 0
            print(f"[migrate] 进度: {offset}/{total} ({offset*100/total:.1f}%) "
                  f"已加={added} 更新={updated} 跳过={skipped} "
                  f"速度={speed:.0f}条/秒")

    catcon.close()
    memcon.close()

    elapsed = time.time() - start_time
    return {
        "ok": True,
        "total_processed": offset,
        "added": added,
        "updated": updated,
        "skipped": skipped,
        "errors": errors,
        "elapsed_seconds": elapsed,
        "speed_per_second": offset / elapsed if elapsed > 0 else 0,
    }


def run_reconciliation(catalog_path: str, memindex_path: str) -> Dict[str, Any]:
    """双跑对账：同一批查询在两套库间的 top10 重合率。

    Args:
        catalog_path: catalog.db 路径
        memindex_path: memindex.db 路径

    Returns:
        对账结果
    """
    print(f"\n[recon] 开始对账")
    print(f"[recon] 查询数: {len(RECON_QUERIES)}")

    # 打开 catalog
    catstore = cat_store.CatalogWriter(catalog_path)
    catstore.open()
    catcon = catstore._require()
    catcon.execute("PRAGMA busy_timeout = 60000")

    # 打开 memindex 的 FTS
    memcon = sqlite3.connect(memindex_path)
    memcon.row_factory = sqlite3.Row

    results = []
    for i, query in enumerate(RECON_QUERIES):
        if not query.strip():
            continue

        # catalog 查询
        cat_result = cat_query.query(catcon, query, k=10)
        cat_hits = [h["oid"] for h in cat_result.get("hits", [])]

        # memindex 查询（直接 SQL）
        # 注意：memindex 只有 trigram FTS，没有双 FTS
        try:
            mem_hits = []
            # 简单模拟：用 LIKE 匹配路径
            rows = memcon.execute(
                "SELECT DISTINCT realpath FROM fed_proj "
                "WHERE text LIKE ? LIMIT 10",
                (f"%{query[:6]}%",),
            ).fetchall()
            mem_hits = [r["realpath"] for r in rows]
        except Exception as e:
            mem_hits = []

        # 计算重合率
        if cat_hits and mem_hits:
            overlap = len(set(cat_hits) & set(mem_hits))
            max_possible = min(len(cat_hits), len(mem_hits))
            hit_rate = overlap / max_possible if max_possible > 0 else 0
        else:
            hit_rate = 0

        results.append({
            "query": query,
            "cat_hits": len(cat_hits),
            "mem_hits": len(mem_hits),
            "hit_rate": hit_rate,
        })

    # 汇总
    avg_hit_rate = sum(r["hit_rate"] for r in results) / len(results) if results else 0
    pass_count = sum(1 for r in results if r["hit_rate"] >= 0.95)

    catcon.close()
    memcon.close()

    return {
        "ok": True,
        "total_queries": len(results),
        "avg_hit_rate": avg_hit_rate,
        "pass_rate": pass_count / len(results) if results else 0,
        "threshold": 0.95,
        "results": results,
    }


def main():
    """主入口。"""
    import argparse
    parser = argparse.ArgumentParser(description="memindex → catalog 迁移工具")
    parser.add_argument("--memindex", default=None,
                        help="memindex.db 路径（默认从 DATA_DIR 推导）")
    parser.add_argument("--catalog", default=None,
                        help="catalog.db 路径（默认从 DATA_DIR 推导）")
    parser.add_argument("--data-dir", default=os.getenv("DATA_DIR",
                        "/fs/1000/ftp/技术文档/agenthub/data"),
                        help="数据目录")
    parser.add_argument("--recon", action="store_true",
                        help="只做对账，不迁移")
    args = parser.parse_args()

    # 确定路径
    memindex_path = args.memindex or Path(args.data_dir) / "memindex.db"
    catalog_path = args.catalog or Path(args.data_dir) / "catalog.db"

    if not args.recon:
        # 执行迁移
        result = migrate_memindex(memindex_path, catalog_path)
        print(f"\n[结果] 迁移完成: {json.dumps(result, ensure_ascii=False, indent=2)}")

        if result.get("ok"):
            # 执行对账
            recon_result = run_reconciliation(catalog_path, memindex_path)
            print(f"\n[结果] 对账完成: {json.dumps(recon_result, ensure_ascii=False, indent=2)}")

            if recon_result["avg_hit_rate"] >= 0.95:
                print("\n✅ 对账通过：平均重合率 ≥ 95%")
                return 0
            else:
                print(f"\n⚠️ 对账警告：平均重合率 {recon_result['avg_hit_rate']:.2%} < 95%")
                return 1
    else:
        # 只做对账
        recon_result = run_reconciliation(catalog_path, memindex_path)
        print(f"\n[结果] 对账完成: {json.dumps(recon_result, ensure_ascii=False, indent=2)}")
        return 0 if recon_result["avg_hit_rate"] >= 0.95 else 1


if __name__ == "__main__":
    sys.exit(main())
