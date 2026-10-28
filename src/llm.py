"""LLM 网关客户端（Anthropic 协议 → 本机 FCC:8082 /v1/messages）

2026-09-06 实测校正：FCC 推理入口只有 /v1/messages(Anthropic) 与
/v1/responses(OpenAI Responses, 仅流式)，**没有** /v1/chat/completions。
FCC→tokenrouter/qwen3.8-flash 的 tool_use 已由 PT-20260905-01 端到端验证。

配置（.env）：
  MANAGER_LLM_BASE_URL   默认 http://127.0.0.1:8082
  MANAGER_LLM_API_KEY    FCC PROXY_AUTH token（Bearer）
  MANAGER_LLM_MODEL      默认 agnes/agnes-2.5-flash
  MEMORY_LLM_MODEL       非会话/后台工具调用的模型（默认 agnes/agnes-2.5-flash）
  MEMORY_LLM_FALLBACK_MODEL  兜底位（默认 agnes/agnes-2.5-flash）

2026-10-29 用户裁定：全部默认走 agnes-2.5-flash，取消 deepseek 与免费模型池。
代码默认值与环境变量同源，`.env` 缺失时也落到 agnes，不再回落到 `openrouter/free`。

⚠️ 两个模型 ID 不可互串（军规：取值一律实测，凭据/ID 不凭记忆）：
  · 经 **CCR**（:3456）要写 provider 前缀 ⇒ `openrouter/openrouter/free`;
  · 直连 **OpenRouter**（distill-knowledge.py 那类）才写 `openrouter/free`。
  · 🚨 任何位置禁 `openrouter/auto` —— 它会自动选最便宜的可用模型，最便宜可能是付费档。
"""
import json
import os
import uuid
from typing import Any, Callable, Dict, List, Optional, Tuple

import aiohttp

import resilience

BASE_URL = os.getenv("MANAGER_LLM_BASE_URL", "http://127.0.0.1:8082").rstrip("/")
API_KEY = os.getenv("MANAGER_LLM_API_KEY", "")
MODEL = os.getenv("MANAGER_LLM_MODEL", "agnes/agnes-2.5-flash")


def memory_model() -> str:
    """非会话 / 后台工具调用使用的模型（2026-10-18 用户裁定）。

    为什么要跟 `MODEL` 分开：`MANAGER_LLM_MODEL` 还服务**交互式**的
    `/api/tasks/decompose`（人点一下才发），而 L2 记忆压缩、后台提炼是**非会话**
    工具调用 —— 两者可分别配模型。2026-10-29 用户裁定：**两者都默认 agnes-2.5-flash**，
    免费池与 deepseek 一并取消（免费池会把请求路由到审核模型并返回标签）。

    读成函数而不是模块常量：`.env` 由 config 在导入后覆盖，常量会把旧值钉死；
    函数每次读当前 env，也让 L0 测试能直接改环境而不必 reload 模块。

    失败语义：不可用时由 `complete_free()` 换兜底并把「不可用」显式返回，
    **不静默切成别的模型**（坏兜底比显式失败更贵）。
    """
    return os.getenv("MEMORY_LLM_MODEL", "agnes/agnes-2.5-flash")


def free_fallback_model() -> str:
    """免费档的兜底位：必须是**具体** `:free`，不能再用路由器。

    2026-10-29 裁定：免费池不稳定（路由到审核模型、429、空正文），
    兜底位改为 `agnes/agnes-2.5-flash`（经 CCR 实测可用，回复正常）。
    通过 MEMORY_LLM_FALLBACK_MODEL 环境变量仍可覆盖。
    """
    return os.getenv("MEMORY_LLM_FALLBACK_MODEL",
                     "agnes/agnes-2.5-flash")


#: 免费档输出「可用」的最小长度。低于此值按失败处理（宁可降级为机械拼接，
#: 也不把空串/标签写进 L2 权威文档）。
_FREE_MIN_CHARS = 20
_FREE_LABEL_PREFIXES = ("user safety", "content safety", "safe", "moderation")


def _usable_llm_text(text: str) -> bool:
    """免费池的「200 但没货」判定：空串 / 审核标签 / 过短都不算成功。"""
    t = (text or "").strip()
    if len(t) < _FREE_MIN_CHARS:
        return False
    low = t.lower()
    return not any(low.startswith(p) for p in _FREE_LABEL_PREFIXES)


async def complete_free(system: str, user: str,
                        call: Optional[Callable] = None) -> Tuple[str, str]:
    """非会话工具调用的统一入口：免费档优先，坏输出换兜底，全失败则显式返空。

    返回 `(text, model_used)`；两者都不可用时返回 `("", "")`，由调用方降级
    （L2 压缩的降级是「机械拼接」，不是抛错）。

    为什么要这一层：免费路由器会返回 200 + 空正文（实测 2026-10-18），
    只判断「有没有抛异常」会把空串当成成功写进 L2，**权威文档被静默清空**。
    """
    caller = call or chat_tools_loop
    models = [memory_model()]
    fb = free_fallback_model()
    if fb and fb not in models:
        models.append(fb)
    for m in models:
        try:
            out, _steps = await caller(
                [{"role": "system", "content": system},
                 {"role": "user", "content": user}],
                tools=[], max_rounds=1, model=m)
        except Exception:  # noqa: BLE001 —— 免费档失败不可抛给用户
            continue
        if _usable_llm_text(out):
            return out.strip(), m
    return "", ""
TIMEOUT = aiohttp.ClientTimeout(total=240)
MAX_TOKENS = int(os.getenv("MANAGER_LLM_MAX_TOKENS", "4096"))

# ── P1-6 接线①：LLM 路径的**重试 + 熔断 + attempt 留痕**（2026-10-16）────────────
# 旧现状：`_create_message` 单发一次，非 200 就抛**纯 RuntimeError**（状态码只存在字符串里）
# ⇒ 429/5xx 既分类不出、也没有重试，也没有熔断；一次限流就是一次用户可见的失败。
# 两个上限必须**逐点显式给**（军规口径）：总尝试次数与退避总时长预算 ——
# 退避不许把一次工具调用拖成用户可见的卡顿。
LLM_ATTEMPTS = max(1, int(os.getenv("LLM_ATTEMPTS", "2")))
LLM_RETRY_BUDGET_S = float(os.getenv("LLM_RETRY_BUDGET_S", "2.0"))


class LLMGatewayError(RuntimeError):
    """上游非 200。**结构化携带** `status`/`headers` —— 否则分类器只能去猜字符串。

    为什么不能继续用纯 RuntimeError：`resilience.classify_error` 判「限流/可重试」
    靠的是状态码与 `Retry-After`；把它们塞进异常文本里，等于把「协议信号」降级成「猜文本」。
    """

    def __init__(self, status: int, text: str, headers: Optional[Dict[str, str]] = None):
        super().__init__("LLM 网关 HTTP %s: %s" % (status, (text or "")[:600]))
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}


def _record_attempt(rec: Dict[str, Any]) -> None:
    """把每次尝试落 `profile_events`（source=resilience）。**记账绝不允许带倒一次真实调用**。"""
    try:
        import db
        db.log_profile_event("resilience", "llm_call",
                             "ok" if rec.get("ok") else (rec.get("kind") or "fail"),
                             rec.get("elapsed_ms"), trace_id=rec.get("call_id"), detail=rec)
    except Exception:  # noqa: BLE001
        pass


def _llm_breaker_key() -> str:
    return resilience.breaker_key(BASE_URL, "llm")


def breaker_state() -> Dict[str, Any]:
    """只读暴露 LLM 通道的熔断状态（供调用方**如实报**「现在是被熔断挡住的」）。

    为什么要有它：`complete_free()` 在熔断打开时会静默返回 `("", "")` —— 那与
    「模型返回空正文」长得一模一样。2026-10-22 实测踩到：skill 重排只看返回空串，
    误判成「模型不给 JSON」，真相是**上游熔断中**（`CancelledError` 触发的）。
    判据必须能分开这两件事（同族旧例：外部 jev 的 `enabled` ≠ 可用——
    端点已 2026-10-23 下线删除，判据保留）。
    """
    try:
        key = _llm_breaker_key()
        return {"key": key, "state": resilience.instance.state_of(key),
                "cooldown_s": resilience.instance.cooldown_remaining_s(key)}
    except Exception as e:  # noqa: BLE001 —— 状态查询失败不许带倒调用方
        return {"error": "%s: %s" % (type(e).__name__, e)}


# v0.5.4 sub-α Fix 1：Nemotron/FCC 走 Anthropic-compatible 协议时，模型把 tool_call
# 当 text 块直出 raw XML（<tool_call><function=...><parameter>...</parameter></tool_call>）。
# 这里把这种块整段剥掉，避免污染 answer 字段。
import re

_TOOL_CALL_BLOCK_RE = re.compile(
    r"<\s*tool_call\s*>.*?<\s*/\s*tool_call\s*>", re.DOTALL
)
_TOOL_CALL_OPEN_RE = re.compile(
    r"<\s*tool_call\s*>.*", re.DOTALL
)
_FUNCTION_BLOCK_RE = re.compile(
    r"<\s*function\s*=[^>]*>.*?<\s*/\s*function\s*>", re.DOTALL
)


def _strip_tool_xml(text: str) -> str:
    """从 text 块里剥掉 raw XML tool_call 残留。

    处理三种情况：
    1. 完整闭合 <tool_call>...</tool_call>
    2. 只剥开标签后所有内容（LLM 经常不闭合）
    3. <function=...>...</function> 单段（部分上游变体）

    返回 strip 后的剩余文本；空字符串由调用方判定为"被剥光"。
    """
    if not text:
        return ""
    cleaned = _TOOL_CALL_BLOCK_RE.sub("", text)
    cleaned = _TOOL_CALL_OPEN_RE.sub("", cleaned)
    cleaned = _FUNCTION_BLOCK_RE.sub("", cleaned)
    return cleaned.strip()


def _is_ok_result(content: Any) -> bool:
    """Fix 6 helper：判定工具结果是否成功。与 _is_err_result 互补。"""
    return not _is_err_result_static(content)


async def _force_empty_answer_fallback(answer: str, steps: List[dict],
                                 on_step) -> Tuple[str, List[dict]]:
    """Fix 6：answer 为空 / 是占位符 + steps 里没有任何 answer → 强制构造用户可见的兜底答复。

    场景：模型整个走 tool_call 模拟（raw XML 被 Fix 1 剥光）/ 软熔断后空返 /
    max_rounds 耗尽且最后一轮只回了 raw XML 或占位符（"（达到最大工具调用轮数）"）。
    返回前会向 steps 追加一条 forced_by=empty_answer_sanitize 的 answer 步骤，
    确保总结卡片一定有内容。
    """
    PLACEHOLDER = "（达到最大工具调用轮数）"
    has_answer = any(s.get("kind") == "answer" for s in steps)
    # 占位符不算有效 answer → 必须替换
    is_placeholder = answer.strip() == PLACEHOLDER
    if answer.strip() and has_answer and not is_placeholder:
        return answer, steps  # 已有正常答案，啥也不做

    ok_steps = [s for s in steps
                if s.get("kind") == "toolresult"
                and _is_ok_result(s.get("content", ""))]
    fail_steps = [s for s in steps
                  if s.get("kind") == "toolresult"
                  and not _is_ok_result(s.get("content", ""))]
    tool_set = sorted({s.get("tool") for s in steps if s.get("kind") == "toolcall"
                       and s.get("tool")})
    tool_list_str = (", ".join(tool_set[:5]) + (" 等" if len(tool_set) > 5 else ""))

    fallback = (
        f"我尝试用 {len(tool_set)} 个工具（{tool_list_str}）"
        f"调用 {len(steps)} 步，成功 {len(ok_steps)} 步、失败 {len(fail_steps)} 步，"
        f"但未生成自然语言答复（模型直出 tool_call XML 而非结构化 tool_calls）。\n\n"
        f"建议：1) 换个更具体的问题；2) 告诉我你想做什么，我用最直接的工具做。"
    )
    new_answer = (fallback if is_placeholder
                  else (answer.strip() or fallback))
    # 若 steps 还没 answer（end_turn/兜底分支追加的那条通常 in 前面），这里补一条
    if not has_answer:
        s_fb = {"kind": "answer", "content": new_answer,
                "forced_by": "empty_answer_sanitize"}
        steps.append(s_fb)
        if on_step:
            await on_step(s_fb)
    return new_answer, steps


def _is_err_result_static(r: Any) -> bool:
    """_is_err_result 的纯函数版本（供 _is_ok_result 调用，也兼容 None/非 dict/非 str）。"""
    if r is None:
        return False
    if isinstance(r, dict):
        if "error" in r:
            return True
        if "ok" in r and not r.get("ok"):
            return True
        return False
    if isinstance(r, str):
        try:
            j = json.loads(r)
            if isinstance(j, dict):
                return _is_err_result_static(j)
        except Exception:
            pass
        return "error" in r.lower()
    return False


def configured() -> bool:
    return bool(BASE_URL)


def new_request_id() -> str:
    """生成本次 LLM 调用的关联 id（P0-2），随 `X-Request-Id` 发给网关。

    ⚠ 2026-10-15 单次只读探针实测：CCR **不透传**客户端 `X-Request-Id`
    （探针带 `hubprobe-…` 打 `/v1/messages` 得 200，账本落的是 CCR 自生成的 UUIDv4）。
    所以现在发这个头**不产生**任务↔成本对齐——真正的对齐要么等 CCR 支持透传，
    要么退化到 `usage_ledger` 的 client×model×时间窗近似归因（`attribution=approximate`）。
    仍然发：零成本、前向兼容（上游一旦支持就自动生效），且出问题时日志里能对着看。
    """
    return uuid.uuid4().hex


def _headers(request_id: Optional[str] = None) -> Dict[str, str]:
    h = {"Content-Type": "application/json", "anthropic-version": "2023-06-01"}
    if API_KEY:
        h["Authorization"] = f"Bearer {API_KEY}"
    if request_id:
        h["X-Request-Id"] = str(request_id)
    return h


def _openai_tools_to_anthropic(tools: List[dict]) -> List[dict]:
    """把 OpenAI function 风格 tools 转成 Anthropic tools"""
    out = []
    for t in tools:
        fn = t.get("function", t)
        out.append({"name": fn["name"], "description": fn.get("description", ""),
                    "input_schema": fn.get("parameters") or {"type": "object", "properties": {}}})
    return out


async def _create_message(system: Optional[str], messages: List[dict],
                          tools: Optional[List[dict]] = None,
                          model: Optional[str] = None,
                          request_id: Optional[str] = None,
                          usage_sink: Optional[List[dict]] = None) -> Dict[str, Any]:
    """单次上游调用。

    `usage_sink`（P0-2）非 None 时，把每次调用的 `{model, request_id, usage}` 追加进去。
    用**调用方传入的 list**而不是返回值：`chat_tools_loop` 的 `(answer, steps)`
    返回形状被 memory.py / tasks.py 与前端步骤渲染同时依赖，改形状的爆炸半径远大于收益。
    追加整体包 try：账务断言绝不允许带倒一次真实 LLM 调用。
    """
    body: Dict[str, Any] = {"model": model or MODEL, "max_tokens": MAX_TOKENS,
                            "messages": messages}
    if system:
        body["system"] = system
    if tools:
        body["tools"] = _openai_tools_to_anthropic(tools)
        body["tool_choice"] = {"type": "auto"}
    data = await resilience.instance.acall(
        _llm_breaker_key(),
        lambda: _post_once(body, request_id),
        attempts=LLM_ATTEMPTS, sleep_budget_s=LLM_RETRY_BUDGET_S,
        record=_record_attempt, call_id=resilience.new_call_id())
    if usage_sink is not None:
        try:
            usage_sink.append({"model": body["model"], "request_id": request_id,
                               "usage": data.get("usage") or {}})
        except Exception:  # noqa: BLE001
            pass
    return data


async def _post_once(body: Dict[str, Any], request_id: Optional[str]) -> Dict[str, Any]:
    """单次 POST（**独立成函数作为测试接缝**：重试编排可被替身验证，而不必真连网关）。"""
    async with aiohttp.ClientSession() as s:
        async with s.post(f"{BASE_URL}/v1/messages", headers=_headers(request_id),
                          json=body, timeout=TIMEOUT) as resp:
            if resp.status != 200:
                text = (await resp.text())[:600]
                raise LLMGatewayError(resp.status, text,
                                      dict(getattr(resp, "headers", {}) or {}))
            return await resp.json()


async def chat_tools_loop(messages: List[dict], tools: List[dict],
                          dispatch_tool=None, max_rounds: int = 10,
                          model: Optional[str] = None,
                          on_step=None,
                          request_id: Optional[str] = None,
                          usage_sink: Optional[List[dict]] = None) -> Tuple[str, List[dict]]:
    """使用指定 model（默认用全局 MODEL）

    多轮工具环（上游 mcp_agent.rs 收敛策略）：
    stop_reason=tool_use → 执行 → tool_result 回填 → 继续；end_turn 终止。
    messages 为 OpenAI 风格 {role,content}，内部转 Anthropic 结构。
    返回 (final_text, steps)。

    v0.5 增量回调：on_step(step) 在每个 thought/toolcall/toolresult 完成后被调用一次，
    用来支持 SSE 流式渲染（前端可看到思考过程，不显假死）。可选，不传 = 老行为。

    v0.5.3 软熔断：检测到连续 3 次同类型工具错误指纹，提前 end_turn 给用户答复；
    max_rounds 仅作兜底，正常情况 3 轮就停。指纹 = tool_name:error_type:field:first50。

    P0-2 新增两个**关键字参数**（都不改返回形状）：
    - `request_id`：本次调用链的关联 id，落到 `X-Request-Id`（当前 CCR 不透传，见
      `new_request_id` 注释；发了不亏，上游一旦支持即自动生效）。
    - `usage_sink`：调用方传进来的 list，每轮上游调用追加 `{model,request_id,usage}`，
      供上层记账（token/成本）。不传 = 老行为，一个字节不变。
    """
    active_model = model or MODEL
    system = None
    anon_msgs = []
    for m in messages:
        if m["role"] == "system":
            system = m["content"]
        else:
            anon_msgs.append({"role": m["role"], "content": m["content"]})
    convo: List[dict] = [{"role": m["role"], "content": m["content"]} for m in anon_msgs]
    steps: List[dict] = []
    # v0.5.3 软熔断：最近 3 个 tool 调用的指纹；元素 = "name:err_type:field:first50"
    recent_signatures: List[str] = []

    def _is_err_result(r: Any) -> bool:
        """判定工具结果是否为错误。兼容 dict {error:...} / {ok:False,...} / 字符串含 'error'。"""
        if isinstance(r, dict):
            if "error" in r:
                return True
            if "ok" in r and not r.get("ok"):
                return True
            return False
        if isinstance(r, str):
            try:
                j = json.loads(r)
                if isinstance(j, dict):
                    return _is_err_result(j)
            except Exception:
                pass
            return "error" in r.lower()
        return False

    def _signature(tool_name: str, r: Any) -> str:
        """提取指纹。成功结果 = '<name>:ok'，失败 = '<name>:err_type:field:first50'。"""
        if not _is_err_result(r):
            return f"{tool_name}:ok"
        # 取结构化字段（如果 result 是 dict 或可解 JSON 字符串）
        obj: Dict[str, Any] = {}
        if isinstance(r, dict):
            obj = r
        elif isinstance(r, str):
            try:
                obj = json.loads(r)
            except Exception:
                obj = {}
        err_type = str(obj.get("error_type", "unknown")) if isinstance(obj, dict) else "unknown"
        field = str(obj.get("field", "")) if isinstance(obj, dict) else ""
        # 错误主文本：error 或 message 或 detail
        msg = ""
        if isinstance(obj, dict):
            msg = str(obj.get("error", "") or obj.get("message", "") or obj.get("detail", ""))
        elif isinstance(r, str):
            msg = r
        first50 = msg[:50]
        return f"{tool_name}:{err_type}:{field}:{first50}"

    for _ in range(max_rounds):
        data = await _create_message(system, convo, tools or None, model=active_model,
                                     request_id=request_id, usage_sink=usage_sink)
        content_blocks = data.get("content") or []
        # v0.5.4 sub-α Fix 1：text 剥离 raw tool_call XML。
        # 部分上游（Nemotron/FCC Anthropic-compatible）把 tool_call 当 text 块直出，
        # 之前会把整段 XML 当 answer 拼出去污染前端。剥掉，保持真正的人话文本。
        raw_texts = [b.get("text", "") for b in content_blocks if b.get("type") == "text"]
        cleaned_pairs = [(t, _strip_tool_xml(t)) for t in raw_texts]
        texts = [c for _o, c in cleaned_pairs if c.strip()]
        xml_leftover = [o for o, c in cleaned_pairs if not c.strip() and o.strip()]
        if xml_leftover:
            # LLM 想调工具却没用结构化 tool_use，留个 step 让前端能看到。
            s_xml = {
                "kind": "thought",
                "content": (
                    "[模型直出 tool_call raw XML，已剥离；该请求应通过结构化"
                    " tool_use 而非文本模拟]"
                ),
                "stripped_xml_chars": sum(len(t) for t in xml_leftover),
            }
            steps.append(s_xml)
            if on_step:
                await on_step(s_xml)
        thoughts = [b.get("thinking", "") for b in content_blocks if b.get("type") == "thinking"]
        for th in thoughts:
            if th.strip():
                s = {"kind": "thought", "content": th[:800]}
                steps.append(s)
                if on_step: await on_step(s)
        tool_uses = [b for b in content_blocks if b.get("type") == "tool_use"]
        if not tool_uses:
            # FCC 上游把正文切成多个 text block（夹 thinking），直接拼接不加换行
            answer = "".join(texts).strip()
            # v0.5.4 sub-α Fix 6：end_turn 但 answer 空 → 强制构造 fallback。
            # 场景：模型整个返回都是 raw XML tool_call 模拟 + 一个 end_turn。
            if not answer:
                answer, steps = await _force_empty_answer_fallback(answer, steps, on_step)
            s = {"kind": "answer", "content": answer}
            steps.append(s)
            if on_step: await on_step(s)
            return answer, steps
        # 记录 assistant 块原样回填
        convo.append({"role": "assistant", "content": content_blocks})
        tool_results = []
        for tu in tool_uses:
            name = tu.get("name", "")
            args = tu.get("input") or {}
            s = {"kind": "toolcall", "tool": name, "tool_input": args,
                 "content": f"调用 {name}"}
            steps.append(s)
            if on_step: await on_step(s)
            try:
                out = await dispatch_tool(name, args)
            except Exception as e:  # noqa: BLE001
                out = {"error": repr(e)}
            payload = out if isinstance(out, str) else json.dumps(out, ensure_ascii=False)
            s = {"kind": "toolresult", "tool": name, "content": payload[:2000]}
            steps.append(s)
            if on_step: await on_step(s)
            tool_results.append({"type": "tool_result", "tool_use_id": tu.get("id", ""),
                                 "content": payload[:8000]})

            # v0.5.3 软熔断：记录指纹 + 判定最近 3 次是否完全相同
            sig = _signature(name, out)
            recent_signatures.append(sig)
            if (len(recent_signatures) >= 3
                    and len(set(recent_signatures[-3:])) == 1
                    and not recent_signatures[-1].endswith(":ok")):
                # 强制 end_turn：构造 fallback answer 步骤（用户能看到"我停了，原因如下"）
                ok_count = sum(
                    1 for st in steps
                    if st.get("kind") == "toolresult"
                    and not _is_err_result(_try_parse_payload(st.get("content", "")))
                )
                ok_summary = (f"已成功收集到 {ok_count} 个工具结果"
                              if ok_count else "无成功结果")
                fallback_answer = (
                    f"我已连续 3 次尝试相同的工具调用但都失败（{sig}），"
                    f"按智管熔断规则提前结束这轮工具调用。{ok_summary}。"
                    f"建议：1) 换用别的工具；2) 重新描述任务；"
                    f"3) 直接告诉用户当前能力边界。"
                )
                s_fb = {"kind": "answer", "content": fallback_answer,
                        "forced_by": "soft_breaker", "signature": sig}
                steps.append(s_fb)
                if on_step:
                    await on_step(s_fb)
                # Fix 6：sanitize 兜底（理论上 fallback_answer 不会空，但守一遍）
                fallback_answer, steps = await _force_empty_answer_fallback(
                    fallback_answer, steps, on_step
                )
                return fallback_answer, steps

        convo.append({"role": "user", "content": tool_results})

    # 轮数耗尽：无 tools 强制收尾
    data = await _create_message(system, convo, None, model=active_model,
                                 request_id=request_id, usage_sink=usage_sink)
    # 同样先剥 raw XML 再用
    raw_texts2 = [b.get("text", "") for b in (data.get("content") or [])
                  if b.get("type") == "text"]
    cleaned2 = [_strip_tool_xml(t) for t in raw_texts2]
    cleaned_text = "".join(c for c in cleaned2 if c).strip()
    answer = cleaned_text or "（达到最大工具调用轮数）"
    # Fix 6：sanitize（万一是 raw XML 满屏）
    answer, steps = await _force_empty_answer_fallback(answer, steps, on_step)
    s = {"kind": "answer", "content": answer}
    steps.append(s)
    if on_step: await on_step(s)
    return answer, steps


def _try_parse_payload(content: Any) -> Any:
    """toolresult.content 可能是 JSON 字符串；尝试解 dict 用来判定是否错误。
    解析失败/非 JSON 时原样返回（避免误判）。"""
    if not isinstance(content, str):
        return content
    s = content.strip()
    if not s or s[0] not in "{[":
        return content
    try:
        import json as _json
        return _json.loads(s)
    except Exception:
        return content
