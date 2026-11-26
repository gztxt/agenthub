"""网关 URL 归一（2026-11-27 互调修复）。

## 为什么需要它

本机有**两个**网关 base 环境变量，写法约定不同：

    CLAUDE_CCR_URL=http://127.0.0.1:3425        # 裸主机（modelcfg 一族按裸主机拼 /v1/...）
    MANAGER_LLM_BASE_URL=http://127.0.0.1:3425/v1  # 已含 /v1（hub LLM / judge 用）

任何一处「base + "/v1/xxx"」只要拿到的是后者，就拼成 `/v1/v1/xxx` ⇒ 网关 404。
这不是理论风险：2026-11-27 P0 实测，hub 的 remote judge 一直在打
`/v1/v1/chat/completions` 并抛 `RemoteJudgeUnavailable`（journal 可见 `judge_fail kind=chain`），
refscout 的 `_llm_text()` 同样拼错。

## 已有两处各自归一，各自为政

- `llm.messages_url()`：认 /v1（2026-11-20 修过）
- `gwprobe.models_url()`：认三种写法

本模块把它们抽成唯一实现，新代码一律用它；已工作的地方不强行改写（避免动高频文件）。

## 判据

`url("http://h:3425/v1", "/v1/chat/completions") == "http://h:3425/v1/chat/completions"`
`url("http://h:3425",     "/v1/chat/completions") == "http://h:3425/v1/chat/completions"`
"""
from typing import Optional

#: 这些前缀出现两次就是重复，需要剥掉一次。
_VERSION_PREFIXES = ("/v1beta", "/v1")


def url(base: Optional[str], path: str) -> str:
    """把 `path` 安全拼到 `base`。base 为空 ⇒ 返回空串（调用方按不可用处理）。

    - base 末尾已含 `/v1`（或 `/v1beta`）且 path 也以同前缀开头 ⇒ 不重复拼；
    - base 含 `/v1` 而 path 是相对路径 (`chat/completions`) ⇒ 补 `/` 后直接拼。
    """
    b = (base or "").rstrip("/")
    p = "/" + (path or "").lstrip("/")
    if not b:
        return ""
    for vp in _VERSION_PREFIXES:
        if b.endswith(vp):
            if p.startswith(vp + "/") or p == vp:
                return b[: -len(vp)] + p
            return b + p
    return b + p


def chat_completions(base: Optional[str]) -> str:
    """OpenAI 兼容 chat 口。"""
    return url(base, "/v1/chat/completions")


def messages(base: Optional[str]) -> str:
    """Anthropic 兼容 messages 口。"""
    return url(base, "/v1/messages")


def models(base: Optional[str]) -> str:
    """模型清单口。"""
    return url(base, "/v1/models")
