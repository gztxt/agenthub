"""通用 OpenAI 兼容适配器（claude→CCR:3456 与 jcode:3457 共用）

Agent_Manager 的 llm.rs 同款协议：POST {base}/v1/chat/completions
支持非流式与 SSE 流式（stream=true, data: [DONE]）。

2026-10-24（PT-20261024-04）：`chat()` 接入 `resilience` 韧性层（重试 + 指数退避 +
熔断 + attempt 留痕）。实测动机（生产库 profile_events，2026-09-06→10-24）：人工对话
101 次中 25 次失败——18 次 CCR「HTTP 400 All target providers failed」（上游全切的
聚合外壳，状态码不可重试但语义瞬时）、2 次零星 429/超时。**外部契约不变**（仍返回
dict、错误串格式保持 "HTTP {status}: {text}"），调用方零改动；`AGENT_CHAT_ATTEMPTS=1`
可整体关回旧行为。流式路径不接重试（首字节后重试会把已 yield 的内容重复一遍）。
"""
import json
import os
import uuid
from typing import Any, AsyncIterator, Dict, List, Optional

import aiohttp

import events
import resilience  # noqa: E402 —— 平铺导入（src 在 sys.path；adapters/tdai.py 同款）
from .base import BaseAdapter

#: agent_chat 通道的重试上限（**总**尝试次数）与退避总时长预算（秒）。
#: 逐点显式给，与 llm.py 的 LLM_ATTEMPTS/LLM_RETRY_BUDGET_S 同口径
#: （resilience 模块 docstring：「接入是逐点显式决定的，不是全局默认」）。
#: ⚠ 计费口径：每次 attempt 都是发往**同一 CCR 网关、同一 model** 的真实请求；
#: 失败的 attempt 一般不产生计费，但重试成功的那次照常计费 —— 无法确定的一律按扣费处理。
AGENT_CHAT_ATTEMPTS = max(1, int(os.getenv("AGENT_CHAT_ATTEMPTS", "2")))
AGENT_CHAT_RETRY_BUDGET_S = float(os.getenv("AGENT_CHAT_RETRY_BUDGET_S", "2.0"))


class AdapterHTTPError(RuntimeError):
    """上游非 200。**结构化携带** `status`/`headers` —— 与 llm.LLMGatewayError 同构。

    为什么不能继续把状态码只塞进字符串：`resilience` 的分类器（`_http_code`/`_headers`
    鸭子类型）要靠它们把 429 判成 rate_limit、把 5xx 判成 server；降级成猜文本，
    重试判定就只能碰运气。
    """

    def __init__(self, status: int, text: str, headers: Optional[Dict[str, str]] = None):
        super().__init__("HTTP %s: %s" % (status, (text or "")[:500]))
        self.status = int(status)
        self.headers = {str(k).lower(): str(v) for k, v in dict(headers or {}).items()}


#: CCR 网关把「我自己切换过的多个上游全部失败」包在 **HTTP 400** 里返回
#: （body 形如 "All target providers failed."）。400 在 resilience 默认策略里属
#: 「请求本身不合法，重试纯浪费」，但对本机它是**上游瞬时故障的聚合外壳**
#: （2026-10-24 实测占人工对话失败 18/25）⇒ 经 `_decide` 的 `extra_retryable` 参数
#: **补充**进可重试集合（与默认判定取并集，只加不减）。判据收紧在「400 + 该文案」，
#: 不扩大化——普通 400 仍不重试。
_GATEWAY_UPSTREAM_TEXT = "target providers failed"


def _is_gateway_upstream_failure(exc: BaseException) -> bool:
    """调用点补充判定：CCR 400「All target providers failed」算瞬时，可重试。"""
    return getattr(exc, "status", None) == 400 and _GATEWAY_UPSTREAM_TEXT in str(exc).lower()


def _record_attempt(rec: Dict[str, Any]) -> None:
    """把每次尝试落 `profile_events`（source=resilience）。与 llm._record_attempt
    同口径：**记账绝不允许带倒一次真实对话**。"""
    try:
        import db
        db.log_profile_event("resilience", "agent_chat",
                             "ok" if rec.get("ok") else (rec.get("kind") or "fail"),
                             rec.get("elapsed_ms"), trace_id=rec.get("call_id"), detail=rec)
    except Exception:  # noqa: BLE001
        pass


class OpenAICompatAdapter(BaseAdapter):
    """任何 OpenAI 兼容端点；auth_style: bearer | x_api_key | none"""

    def __init__(self, config, agent_id: str, base_url: str,
                 auth_token: str = "", auth_style: str = "bearer",
                 default_model: str = "", display_name: str = ""):
        super().__init__(config)
        self.agent_id = agent_id
        self.base_url = base_url.rstrip("/")
        self.auth_token = auth_token
        self.auth_style = auth_style
        self.default_model = default_model
        self.display_name = display_name or agent_id

    def build_headers(self) -> Dict[str, str]:
        headers = {"Content-Type": "application/json"}
        if self.auth_token:
            if self.auth_style == "x_api_key":
                headers["x-api-key"] = self.auth_token
            elif self.auth_style == "ccr":
                headers["x-ccr-core-auth"] = self.auth_token
            else:
                headers["Authorization"] = f"Bearer {self.auth_token}"
        return headers

    async def _post_once(self, url: str, body: Dict[str, Any],
                         total_s: float = 120.0) -> Dict[str, Any]:
        """单次 POST（**独立成函数作为测试接缝**：重试编排可被替身验证，不必真连网关）。

        非 200 抛 `AdapterHTTPError`（结构化带 status/headers）——由 `chat()` 在外层
        转回旧 dict 契约；ClientError/超时原样抛出，交 resilience 分类。
        """
        async with aiohttp.ClientSession() as s:
            async with s.post(url, headers=self.build_headers(), json=body,
                              timeout=aiohttp.ClientTimeout(total=total_s)) as resp:
                if resp.status != 200:
                    text = (await resp.text())[:500]
                    raise AdapterHTTPError(resp.status, text,
                                           dict(getattr(resp, "headers", {}) or {}))
                return await resp.json()

    async def chat(self, message: str, session_id: Optional[str] = None,
                   model: Optional[str] = None, history: Optional[List[dict]] = None,
                   cwd: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        messages = list(history or [])
        # hub-self / 智管自身对话：cwd 作为上下文元数据注入 prompt（参考 pi Agent 工作目录传参）
        if cwd and self.agent_id == "hub-self" and not any(
                m.get("role") == "system" and "工作目录" in (m.get("content") or "")
                for m in messages):
            messages = [{"role": "system",
                         "content": f"当前工作目录：{cwd}。回答与文件/路径相关的问题时优先基于此目录。"}] + messages
        messages = messages + [{"role": "user", "content": message}]
        body = {"model": model or self.default_model or "default",
                "messages": messages, "stream": False}
        url = f"{self.base_url}/v1/chat/completions"
        # 超时口径统一（2026-10-29）：默认 120s 是**单轮对话**的合理值，但派发链路的
        # 外层闸门是 `TASK_TIMEOUT_S=600`（tasks.py）。写死 120 会让外层闸门永不触发，
        # 长任务一律表现成 `success=False`（"执行者失败"），而真实原因是客户端先断了 ——
        # 归因直接错一层。调用方能传就按它传，传不了才退默认。
        # 重试接进来（2026-10-24 PT-20261024-04）后口径不变：**每次 attempt 都用同一个
        # total_s**，不是「整串共享一个超时」——后者会让「第一次吃满 120s」直接耗尽预算。
        try:
            total_s = float(kwargs.get("timeout_s") or 120.0)
        except (TypeError, ValueError):
            total_s = 120.0
        key = resilience.breaker_key(self.base_url, "agent_chat")
        try:
            data = await resilience.instance.acall(
                key, lambda: self._post_once(url, body, total_s),
                attempts=AGENT_CHAT_ATTEMPTS, sleep_budget_s=AGENT_CHAT_RETRY_BUDGET_S,
                record=_record_attempt, call_id=resilience.new_call_id(),
                extra_retryable=_is_gateway_upstream_failure)
        except resilience.BreakerOpen:
            # 熔断挡住的调用**没有发出去**，与「上游拒绝」必须可区分（llm.py:139
            # breaker_state() 同族判据：把熔断当普通失败会看不出「对端正在被保护」）。
            cd = resilience.instance.cooldown_remaining_s(key)
            return {"success": False, "agent": self.agent_id,
                    "error": f"网关熔断中（请求未发出，约 {cd:.0f}s 后放行探针）"}
        except AdapterHTTPError as e:
            return {"success": False, "agent": self.agent_id, "error": str(e)}
        except aiohttp.ClientError as e:
            return {"success": False, "agent": self.agent_id, "error": str(e)}
        except Exception as e:  # noqa: BLE001
            return {"success": False, "agent": self.agent_id, "error": repr(e)}
        choice = (data.get("choices") or [{}])[0]
        return {
            "success": True,
            "agent": self.agent_id,
            "response": (choice.get("message") or {}).get("content", ""),
            "model": data.get("model"),
            "usage": data.get("usage"),
        }

    async def chat_stream(self, message: str, session_id: Optional[str] = None,
                          model: Optional[str] = None, history: Optional[List[dict]] = None,
                          **kwargs) -> AsyncIterator[str]:
        messages = (history or []) + [{"role": "user", "content": message}]
        body = {"model": model or self.default_model or "default",
                "messages": messages, "stream": True}
        # 事件通道（P3）需要 usage：显式 opt-in（/chat/stream 的既有消费者不受影响）。
        want_usage = bool(kwargs.get("include_usage"))
        if want_usage:
            body["stream_options"] = {"include_usage": True}
        url = f"{self.base_url}/v1/chat/completions"
        try:
            async with aiohttp.ClientSession() as s:
                async with s.post(url, headers=self.build_headers(), json=body,
                                  timeout=aiohttp.ClientTimeout(total=300)) as resp:
                    if resp.status != 200:
                        text = (await resp.text())[:300]
                        yield f"data: {json.dumps({'error': f'HTTP {resp.status}: {text}'}, ensure_ascii=False)}\n\n"
                        return
                    async for raw in resp.content:
                        line = raw.decode("utf-8", "ignore").strip()
                        if not line.startswith("data:"):
                            continue
                        payload = line[5:].strip()
                        if payload == "[DONE]":
                            yield "data: [DONE]\n\n"
                            return
                        try:
                            chunk = json.loads(payload)
                            delta = (chunk.get("choices") or [{}])[0] \
                                .get("delta", {}).get("content")
                            if delta:
                                yield f"data: {json.dumps({'delta': delta}, ensure_ascii=False)}\n\n"
                            u = chunk.get("usage")
                            if u and want_usage:
                                yield f"data: {json.dumps({'usage': u}, ensure_ascii=False)}\n\n"
                        except json.JSONDecodeError:
                            continue
        except Exception as e:  # noqa: BLE001
            yield f"data: {json.dumps({'error': repr(e)}, ensure_ascii=False)}\n\n"

    async def chat_events(self, message: str, session_id: Optional[str] = None,
                          model: Optional[str] = None, history: Optional[List[dict]] = None,
                          cwd: Optional[str] = None, **kwargs) -> AsyncIterator[dict]:
        """把现有 SSE 的 delta / usage 包成 v1 事件（chat_stream 的对外契约不变）。"""
        sid = session_id or ""
        run_id = kwargs.pop("run_id", None) or uuid.uuid4().hex[:12]
        em = events.EventEmitter(sid, self.agent_id, kwargs.pop("turn_id", None))
        events.register_run(run_id, self.agent_id, sid, interrupt=None,
                            turn_id=em.turn_id)
        parts: List[str] = []
        got_error = False
        try:
            yield em.emit("turn_started", model=model or self.default_model or "default")
            async for raw in self.chat_stream(message, session_id=session_id, model=model,
                                              history=history, cwd=cwd,
                                              include_usage=True):
                line = (raw or "").strip()
                if not line.startswith("data:"):
                    continue
                payload = line[5:].strip()
                if payload == "[DONE]":
                    break
                try:
                    obj = json.loads(payload)
                except json.JSONDecodeError:
                    continue
                if obj.get("delta"):
                    parts.append(obj["delta"])
                    yield em.emit("message_delta", text=obj["delta"])
                elif obj.get("usage"):
                    u = em.usage(obj["usage"])
                    if u:
                        yield u
                elif obj.get("error"):
                    got_error = True
                    msg = str(obj["error"])
                    yield em.error(events.reason_for_error(msg), msg[:400])
            if got_error:
                yield em.emit("turn_completed", success=False, reason="provider_error")
            else:
                yield em.emit("turn_completed", success=True, chars=len("".join(parts)))
        finally:
            events.unregister_run(run_id)

    async def get_models(self) -> list:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.base_url}/v1/models",
                                 headers=self.build_headers(),
                                 timeout=aiohttp.ClientTimeout(total=10)) as resp:
                    if resp.status == 200:
                        data = await resp.json()
                        return [m.get("id") for m in data.get("data", []) if m.get("id")]
        except Exception:  # noqa: BLE001
            pass
        return []

    async def get_sessions(self, limit: int = 10) -> list:
        return []  # 网关型端点无会话存储，统一对话记录在 hub 本地 SQLite

    async def health_check(self) -> Dict[str, Any]:
        try:
            async with aiohttp.ClientSession() as s:
                async with s.get(f"{self.base_url}/health",
                                 timeout=aiohttp.ClientTimeout(total=5)) as resp:
                    ok = resp.status == 200
                    return {"status": "ok" if ok else "error", "agent": self.agent_id}
        except Exception:  # noqa: BLE001
            return {"status": "error", "agent": self.agent_id}
