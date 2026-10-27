"""适配器注册表：agent_id -> adapter 实例（Phase 2：补齐 claude/jcode 直连）"""
from typing import Dict, List, Optional

from adapters.openai_compat import OpenAICompatAdapter
from adapters.pi import PiAdapter
from adapters.tdai import TDAIAdapter

_adapters: Dict[str, object] = {}


def build_adapters(config) -> Dict[str, object]:
    """按 config 构建全部适配器。CCR/jcode 共用 OpenAI 兼容协议 3456 口
    （3457 实测仅 CCR 运行时健康口，不接受 chat 流量）。只调用，不改 CCR。"""
    _adapters.clear()
    _adapters["claude"] = OpenAICompatAdapter(
        config, "claude", base_url=config.ccr_url,
        auth_token=config.ccr_openai_key, auth_style="bearer",
        default_model=getattr(config, "claude_model", "") or "qwen3.8-flash",
        display_name="Claude Code (CCR)")
    _adapters["jcode"] = OpenAICompatAdapter(
        config, "jcode", base_url=config.jcode_url,
        auth_token=config.ccr_openai_key, auth_style="bearer",
        default_model=getattr(config, "jcode_model", "") or "qwen3.8-flash",
        display_name="JCode (CCR)")
    _adapters["pi"] = PiAdapter(config)
    _adapters["tdai"] = TDAIAdapter(config)
    return dict(_adapters)


def get_adapter(agent_id: str) -> Optional[object]:
    return _adapters.get(agent_id)


#: `chat()` 恒返回 `success=False` 的适配器：pi 需走 Web UI、TDAI 是记忆中心。
#: 它们在 `_adapters` 里只是为了让健康检查/会话列表复用同一个实例，
#: **不是派发目标**。
CHAT_STUBS = ("pi", "tdai")


def dispatchable_ids() -> List[str]:
    """能真正接 `_chat_dispatch` 的 agent id —— **派发选型池的唯一口径**。

    判据与 `api_agents._chat_dispatch` 的解析逐行对齐：适配器在册且不是恒失败桩，
    或 `custom_agents` 登记了 port（走 OpenAI 兼容直连）。

    为什么不能拿 `profiles.all_profiles()` 当池子（2026-10-27 实弹取证）：
    菜单里 20 张 agent 卡，能跑 `chat()` 的只有 claude/jcode 两家。把菜单当派发池
    ⇒ Leader 抽中 dsh/cloudcli 就是一条注定 `无可用适配器` 的失败任务
    （实跑 `239b4d553a`：t1→dsh→failed，0 产出）。**N 个适配器 ≠ N 个能干活的 agent。**
    """
    ids = [a for a in _adapters if a not in CHAT_STUBS]
    try:
        import db   # 延迟导入：registry 被 adapters 依赖，顶层引 db 会做环
        for r in db.query("SELECT id FROM custom_agents "
                          "WHERE port IS NOT NULL AND port != ''"):
            if r["id"] not in ids:
                ids.append(r["id"])
    except Exception:  # noqa: BLE001  DB 未就绪时退回适配器清单，不能把派发池清空
        pass
    return ids
