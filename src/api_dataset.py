"""数据集 / 经验库只读路由（P0-1 切片 B/C）。

【只读】本模块**不提供写端点**：数据集入样由 `scripts/dataset_pipeline.py`（离线、确定性）；
经验退役由 `scripts/experience_mine.py`（Dreaming）。这样路由面没有写路径需要鉴权，
也避免把「管线」暴露成 HTTP 写口。

【端点的用处】
    /api/dataset/list              数据集清单（带 train/holdout 计数）
    /api/dataset/{id}/items        样本（可按 split 过滤）
    /api/dataset/{id}/train        优化批次 —— 并**自证**这批不含 holdout
    /api/experience/list           经验库（默认只看活跃）
    /api/experience/search?q=      经验召回（只召回活跃条目，退役的不回来）
"""
from typing import Optional

from fastapi import APIRouter, HTTPException, Query

import dataset

router = APIRouter()


@router.get("/api/dataset/list")
async def list_datasets():
    rows = dataset.list_datasets()
    for r in rows:
        r["counts"] = dataset.counts(r["id"])
    return {"datasets": rows, "count": len(rows)}


@router.get("/api/dataset/{dataset_id}/items")
async def dataset_items(dataset_id: str, split: Optional[str] = Query(default=None)):
    if dataset.get_dataset(dataset_id) is None:
        raise HTTPException(404, "dataset not found")
    rows = dataset.list_items(dataset_id, split=split)
    return {"dataset_id": dataset_id, "split": split, "items": rows, "count": len(rows)}


@router.get("/api/dataset/{dataset_id}/train")
async def dataset_train(dataset_id: str):
    """优化批次 = train 切分。响应里 `holds_no_holdout` 是**当场自证**的结果。"""
    if dataset.get_dataset(dataset_id) is None:
        raise HTTPException(404, "dataset not found")
    rows = dataset.training_items(dataset_id)
    dataset.assert_train_only(dataset_id, [r["id"] for r in rows])   # 掺入即抛
    return {"dataset_id": dataset_id, "items": rows, "count": len(rows),
            "holds_no_holdout": True}


@router.get("/api/experience/list")
async def experience_list(active_only: bool = Query(default=True)):
    rows = dataset.list_experiences(active_only=active_only)
    return {"experiences": rows, "count": len(rows)}


@router.get("/api/experience/search")
async def experience_search(q: str = Query(min_length=1),
                            limit: int = Query(default=20, le=100)):
    hits = dataset.search_experiences(q, limit=limit)
    return {"query": q, "experiences": hits, "count": len(hits)}