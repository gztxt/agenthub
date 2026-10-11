"""应用级共享状态（C2：组装与领域路由分离后的唯一可变状态载体）。

为什么要它：领域路由从 `main.py` 抽成独立 module 后，`discovery`（生命周期里建、
请求路径里读）不能再靠 main 的模块级全局 —— 两边必须看见**同一个值**。
放这里而不是放 main：领域模块若 import main 会构成循环依赖。

写得刻意小：这里只放「组装方写、领域方读」的可变句柄，不放逻辑。
"""
from typing import Optional

#: `AgentDiscovery` 实例。由 `main.py` 的 startup 建立；领域路由与后台循环只读。
#: 类型用 `object` 兜底、不 import discovery —— 否则与 main 形成环。
discovery: Optional[object] = None