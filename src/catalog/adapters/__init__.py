"""catalog.adapters —— 只读适配器集合（PT-20261025-12 P0）。

P0 三域：`agents` / `skills` / `projects`。

**全部只读**：没有任何适配器写回原生位置。
**单一事实来源**：本包只做「归一化 + 落编目库」，扫描逻辑一律**复用现有模块**
（`profiles` / `skill` / `localprojects`），不复制第二份实现——
复制一份就会漂移，本仓反复禁的第二真相。

新增适配器：在 `src/catalog/adapters/` 加一个模块，用 `@register` 装饰类，
并在本包 `__init__` 里 import 一次即可。
"""
from . import agents as _agents  # noqa: F401
from . import skills as _skills  # noqa: F401
from . import projects as _projects  # noqa: F401
from ..adapter import REGISTRY  # noqa: F401

__all__ = ["REGISTRY"]
