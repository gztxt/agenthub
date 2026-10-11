"""领域路由 · 模型清单（C2：从 `main.py` 抽出）。

只依赖 adapter 层与 `models_cache`（纯函数缓存层）；无共享状态。
"""
from typing import Optional

from fastapi import APIRouter, Query

import models_cache
from registry import get_adapter

router = APIRouter()

#: 按 adapter 分桶的成功路径缓存（claude 与 jcode 共用 CCR，但接口各自可换）
_models_cache: dict = {}


@router.get("/api/models")
async def list_models(agent_id: Optional[str] = None,
                      force: bool = Query(default=False)):
    """统一模型列表端点。

    默认拉 claude(CCR:3456) 的 /v1/models 作为"全局可对话模型"
    （v0.10.0 起 hub-self 已移除，claude/jcode 共用同一 CCR 模型表）。
    按 vendor/display_name 去重后返回；带分组（qwen/deepseek/nvidia/openrouter/agnes）。

    2026-10-05 加**成功路径缓存**（`models_cache.py`，60s）：原先每次都新建
    ClientSession 打上游且 `timeout=total=8` 且无缓存 ⇒ CCR 一挂，模型下拉**每次卡 8 秒**。
    `force=true` 沿用 `skill_status`（skill.py:857）的既有惯例绕缓存。
    ⚠ 失败/超时**不写缓存**（models_cache 里那条注释是本改动最要紧的判据）。"""
    adapter_id = agent_id or "claude"
    adapter = get_adapter(adapter_id)
    base = None
    if hasattr(adapter, "base_url"):
        base = adapter.base_url
    if not base:
        return {"models": [], "groups": {}, "error": "no compatible adapter"}

    # 按 adapter 分桶缓存（claude 与 jcode 共用 CCR，但接口各自可换）
    _bucket = _models_cache.setdefault(adapter_id, models_cache.TTLCache())

    async def _fetch() -> dict:
        """⚠ 这个函数**不抛异常**（改前就不抛，语义保持），失败信息装进 `error` 键。

        而 `models_cache.cached_models` 的判据是「`error` 为空才缓存」——
        两者必须配套：若这里把失败**抛**出去，缓存层就拿不到 payload；
        若这里失败却**返回**一个无 error 键的空清单，缓存层就会把失败缓存 60s
        （2026-10-05 影子实测踩过，见 models_cache.cached_models 的 docstring）。
        """
        import aiohttp as _aio
        out: list = []
        try:
            async with _aio.ClientSession() as s:
                async with s.get(f"{base}/v1/models",
                                 headers=adapter.build_headers() if hasattr(adapter, "build_headers") else {},
                                 timeout=_aio.ClientTimeout(total=8)) as r:
                    if r.status == 200:
                        data = await r.json()
                        out = data.get("data") or []
                    else:
                        # 非 200 也要带 error，否则会被缓存层当成「成功但清单为空」
                        return {**_shape_models([], adapter_id),
                                "error": "上游返回 HTTP %d" % r.status}
        except Exception as e:  # noqa: BLE001 —— 语义与改前逐字一致：不抛，回 error 键
            return {"models": [], "groups": {}, "error": str(e)[:200]}
        return _shape_models(out, adapter_id)

    try:
        payload, was_cached = await models_cache.cached_models(_bucket, _fetch, force=force)
    except Exception as e:  # noqa: BLE001 —— 兜底：_fetch 已不抛，这里是最后一道
        return {"models": [], "groups": {}, "error": str(e)[:200]}
    return {**payload, "cached": was_cached}


def _shape_models(out: list, adapter_id: str) -> dict:
    """把上游的原始 model 列表去重 + 分组（判据与缓存无关，故抽成独立纯函数）。"""
    seen = set()
    groups: dict = {}
    cleaned = []
    for m in out:
        mid = m.get("id")
        if not mid or mid in seen:
            continue
        seen.add(mid)
        cleaned.append({"id": mid, "name": m.get("display_name") or mid,
                        "owner": m.get("owned_by") or "?"})
        # 分组键 = mid 第一段（vendor/）
        gk = mid.split("/", 1)[0] if "/" in mid else "default"
        groups.setdefault(gk, []).append(mid)
    return {"models": cleaned, "groups": groups, "count": len(cleaned),
            "source": adapter_id}