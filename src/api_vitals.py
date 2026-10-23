"""领域路由 · 体检 / 画像（C2：从 `main.py` 抽出）。

只依赖 adapter 层与 db；共享状态走 `appstate`。
`agent_profs_for_vitals()` 被本模块的 sweep 端点、`main.py` 的 vitals_loop 与
`/api/scan/run` 共同使用，故对外公开（不再是 main 的私有 helper）。
"""
import asyncio

from fastapi import APIRouter

import profiles as profiles_mod
import vitals as vitals_mod

router = APIRouter()


def agent_profs_for_vitals():
    """只体检 kind=agent 的画像（gateway/service/tool 不进菜单闸门），
    且**含已被摘掉的候补** —— 不重新探测就会被永久固化，用户装好了也回不来。"""
    return [p for p in profiles_mod.all_profiles(include_blocked=True)
            if p.get("kind") == "agent"]


@router.get("/api/vitals")
async def get_vitals():
    """判定台账（可审计）：每个 agent 的证据 + 裁决 + 来源 + 概率"""
    snap = vitals_mod.vitals.snapshot()
    return {"count": len(snap), "last_sweep": vitals_mod.vitals.last_sweep,
            # 2026-10-23：外部 jev（api.typesafe.ai）已下线，判定改本地模型。
            # 旧载荷的 jev_key_present / jev_blocked_s（key 在 ≠ 可用、451 短路）
            # 随之作废，换成本地判定后端的就绪与真调用状态——自证端点必须能说出
            # 「这一轮的语义判定为什么没跑成」（脚本缺失/Ollama 不在/超时）。
            "judge": vitals_mod.judge_status(),
            # 2026-10-23：告警必须能被面板看到——「最近一次成功」不是健康证据，
            # 连续失败/恢复记录才是（落盘 data/judge_alerts.jsonl，只回最近 5 条）。
            "judge_alerts": vitals_mod.judge_alerts(5),
            "menu_min": vitals_mod.MENU_MIN, "sweep_every_sec": vitals_mod.SWEEP_EVERY,
            "detail": snap}


@router.post("/api/vitals/sweep")
async def post_sweep():
    """跑一轮完整体检：先全量 L1/L2（不碰模型），再对「上次真请求实测已过期」的
    候补补 L4（每人每 24h 最多一次）。VITALS_RT_SWEEP=0 可退成纯廉价轮。"""
    return await asyncio.to_thread(vitals_mod.vitals.sweep, agent_profs_for_vitals())