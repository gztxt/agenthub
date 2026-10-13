"""适配器注册表：agent_id -> adapter 实例（Phase 2：补齐 claude/jcode 直连）"""
from typing import Dict, Optional

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
