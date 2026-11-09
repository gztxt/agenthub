"""适配器抽象基类"""
import asyncio
import uuid
from abc import ABC, abstractmethod
from typing import Any, Dict, Optional, AsyncIterator

import events


class BaseAdapter(ABC):
    """Agent 适配器基类"""

    def __init__(self, config):
        self.config = config
        self.agent_id = self.__class__.__name__.lower()

    @abstractmethod
    async def chat(self, message: str, session_id: Optional[str] = None,
                   model: Optional[str] = None, history: Optional[list] = None,
                   cwd: Optional[str] = None, **kwargs) -> Dict[str, Any]:
        """发送消息并获取响应。

        model: 透传 model id（网关/上游路由键）
        history: 最近 N 轮 [{role,content}]
        cwd: 当前工作目录（语义：上下文元数据，由具体适配器决定是否注入 prompt）
        """
        pass

    @abstractmethod
    async def chat_stream(self, message: str, session_id: Optional[str] = None,
                          model: Optional[str] = None, history: Optional[list] = None,
                          cwd: Optional[str] = None, **kwargs) -> AsyncIterator[str]:
        pass

    async def chat_events(self, message: str, session_id: Optional[str] = None,
                          model: Optional[str] = None, history: Optional[list] = None,
                          cwd: Optional[str] = None, **kwargs) -> AsyncIterator[dict]:
        """归一化事件流（P3 v1，可选能力）。**默认实现 = 批式 chat() 的包装**。

        所有适配器零改造成即可产出 begin/正文/结束三类事件；真正逐 token 的
        实现由具体适配器覆盖（codex / openai_compat）。不改 chat()/chat_stream()
        的签名与返回契约 —— 派发链路与对话页仍是它们的既有消费者。
        """
        sid = session_id or ""
        run_id = kwargs.pop("run_id", None) or uuid.uuid4().hex[:12]
        turn_id = kwargs.pop("turn_id", None)
        em = events.EventEmitter(sid, self.agent_id, turn_id)
        events.register_run(run_id, self.agent_id, sid, interrupt=None,
                            turn_id=em.turn_id)
        try:
            yield em.emit("turn_started", model=model)
            try:
                result = await self.chat(message, session_id=session_id, model=model,
                                         history=history, cwd=cwd, **kwargs)
            except asyncio.CancelledError:
                raise
            except Exception as e:  # noqa: BLE001 —— 事件层不许把适配器异常漏成断流
                reason = events.reason_for_error(repr(e))
                yield em.error(reason, "%s: %s" % (type(e).__name__, str(e)[:300]))
                yield em.emit("turn_completed", success=False, reason=reason)
                return
            if result.get("success"):
                text = result.get("response") or ""
                yield em.emit("message_delta", text=text)
                u = em.usage(result.get("usage"))
                if u:
                    yield u
                yield em.emit("turn_completed", success=True,
                              model=result.get("model"))
            else:
                reason = events.reason_for_error(result.get("error"))
                yield em.error(reason, result.get("error"))
                yield em.emit("turn_completed", success=False, reason=reason)
        finally:
            events.unregister_run(run_id)

    @abstractmethod
    async def get_sessions(self, limit: int = 10) -> list:
        """获取会话历史"""
        pass

    @abstractmethod
    async def get_models(self) -> list:
        """获取可用模型列表"""
        pass

    async def health_check(self) -> Dict[str, Any]:
        """健康检查"""
        return {"status": "unknown", "agent": self.agent_id}

    def build_headers(self) -> Dict[str, str]:
        """构建认证头（子类重写）"""
        return {}
