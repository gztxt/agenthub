"""适配器注册表：agent_id -> adapter 实例（Phase 2：补齐 claude/jcode 直连）

2026-10-29（v0.13.175）扩池：派发池从 2 家扩到 10 家 —— 新增 9 个**带工具环**的
CLI 执行者（claude-code / codex / dsh / pi / codebuddy / grok / hermes / omp / cursor）。
它们与 claude/jcode 的本质差别：那两个是 OpenAI 兼容**端点**（只发 messages、无 tools），
CLI 是**进程**（自带文件/命令工具）。文件类子任务只有后者能真干，前者会编造
（2026-10-29 实测 `/etc/hostname` 真值 `zzst`，端点回 `AGNES-2.5-FLASH` 且判 pass）。
"""
from typing import Dict, List, Optional

from adapters.cli_agent import SPECS as CLI_SPECS, CliAgentAdapter
from adapters.openai_compat import OpenAICompatAdapter
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
    # CLI 执行者：无条件注册，**可用性由 `available()` 在取池时判定** ——
    # 运行期新装/二进制被移走都要如实反映，不能靠启动时快照。
    for aid in CLI_SPECS:
        _adapters[aid] = CliAgentAdapter(config, aid)
    _adapters["tdai"] = TDAIAdapter(config)
    return dict(_adapters)


def get_adapter(agent_id: str) -> Optional[object]:
    return _adapters.get(agent_id)


#: `chat()` 恒返回 `success=False` 的适配器：TDAI 是记忆中心。
#: 它在 `_adapters` 里只是为了让健康检查/会话列表复用同一个实例，
#: **不是派发目标**。pi 已于 v0.13.175 转正为 CLI 执行者（`pi -p` 实测可用），
#: 故从本表移除 —— 留在表里会让「有适配器但永远失败」继续冒充「已接入」。
CHAT_STUBS = ("tdai",)


def adapter_status() -> List[dict]:
    """池内每个 agent 的可用性与原因（给诊断面看，不参与派发决策）。"""
    out = []
    for aid, ad in _adapters.items():
        if aid in CHAT_STUBS:
            continue
        av = getattr(ad, "available", None)
        ok, why = (True, "") if not callable(av) else tuple(av())
        out.append({"id": aid, "available": bool(ok), "reason": why,
                    "display": getattr(ad, "display_name", aid)})
    return out


def dispatchable_ids() -> List[str]:
    """能真正接 `_chat_dispatch` 的 agent id —— **派发选型池的唯一口径**。

    判据与 `api_agents._chat_dispatch` 的解析逐行对齐：适配器在册、不是恒失败桩、
    且 `available()` 为真（二进制/凭据就绪），或 `custom_agents` 登记了 port。

    为什么不能拿 `profiles.all_profiles()` 当池子（2026-10-27 实弹取证）：
    菜单里 20 张 agent 卡，能跑 `chat()` 的只有 claude/jcode 两家。把菜单当派发池
    ⇒ Leader 抽中 dsh/cloudcli 就是一条注定 `无可用适配器` 的失败任务
    （实跑 `239b4d553a`：t1→dsh→failed，0 产出）。**N 个适配器 ≠ N 个能干活的 agent。**

    2026-10-29 补：CLI 适配器**注册不等于可用** —— 二进制不在（或 dsh 缺凭据）时
    必须挡在池外，否则重演上面那条失败任务，只是换了个 agent 名字。
    """
    ids = []
    for a, ad in _adapters.items():
        if a in CHAT_STUBS:
            continue
        av = getattr(ad, "available", None)
        if callable(av):
            ok, _why = av()
            if not ok:
                continue
        ids.append(a)
    try:
        import db   # 延迟导入：registry 被 adapters 依赖，顶层引 db 会做环
        for r in db.query("SELECT id FROM custom_agents "
                          "WHERE port IS NOT NULL AND port != ''"):
            if r["id"] not in ids:
                ids.append(r["id"])
    except Exception:  # noqa: BLE001  DB 未就绪时退回适配器清单，不能把派发池清空
        pass
    return ids
