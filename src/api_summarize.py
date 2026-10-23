# -*- coding: utf-8 -*-
"""领域路由 · 摘要提炼（2026-10-23）。

端点只做参数校验与错误映射，业务在 `src/summarize.py`：
- 正文为空 / 过长 ⇒ 400（可修正的调用错误）
- Ollama 不在 / 超时 / 输出不可用 ⇒ 503（外部依赖不可用）
**不把失败包成空要点**：空列表会被调用方当成「这篇没内容」。
"""
import asyncio

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import summarize as summarize_mod

router = APIRouter()


class SummarizeIn(BaseModel):
    text: str = Field(min_length=1, max_length=summarize_mod.MAX_INPUT)
    points: int = Field(default=3, ge=1, le=summarize_mod.MAX_POINTS)
    style: str = Field(default="", max_length=120)


@router.post("/api/summarize")
async def post_summarize(inp: SummarizeIn):
    """长文 → N 条要点（本机 Ollama）。返回 points/text/chunks/model/elapsed_s/cache。"""
    try:
        return await asyncio.to_thread(summarize_mod.summarize,
                                       inp.text, inp.points, inp.style)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))
    except summarize_mod.SummarizeUnavailable as e:
        raise HTTPException(status_code=503, detail=str(e))


@router.get("/api/summarize/status")
async def get_summarize_status():
    return summarize_mod.status()
