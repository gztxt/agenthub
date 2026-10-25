"""catalog 统一编目库（PT-20261025-12）。

分层（方案文档 §3）：
- `schema.py`   DDL / 迁移 / 校验
- `store.py`    连接、事务、批量 upsert（唯一写者：hub 进程）
- `adapter.py`  适配器基类 + 注册表（全部只读）
- `query.py`    统一查询入口（长度路由 + 排序 + 去重）

**唯一写者**：只有 hub 进程写 catalog.db；其它进程/agent 经 HTTP/MCP 只读。
SQLite WAL 允许 N 读 1 写，读不阻塞写。
"""
from . import schema  # noqa: F401

__all__ = ["schema"]
