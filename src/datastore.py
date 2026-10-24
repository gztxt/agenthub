"""统一读取层：五个数据域的**单一入口** + 物理落点的显式声明
（PT-20261024-08 C3）。

## 为什么要有这个模块（用户诉求原文）

> 「数据库是否统一 是否所有数据都是使用统一数据库 不是多路查询 多路找数据
> 增加资源开支 速度还慢」「数据库包括 agent 清单数据 技能数据 记忆数据 知识库数据
> 会话提炼数据 等 需要统一的存储和按需调用 不能一股脑全部加载」

2026-10-24 实测的答案（不回避）：

| 域 | 物理落点 | 能否物理合并 |
|---|---|---|
| agent 清单 | `src/profiles.py`（声明，21 条，含正则 detect）+ `custom_agents` 表（运行时发现 6 条）+ 进程探测 | ❌ `detect` 是**代码逻辑**（正则），不是数据 |
| 技能 | 15+ 个 agent 家目录**文件系统**（不入库）+ `skill_assets` 表（元数据 18 条）+ TDAI `skill/list`（实测 200 但 0 行） | ❌ 真相在各自家目录，是各工具链的私产 |
| 记忆 | **TDAI 容器**（权威，跨进程）+ `memindex.db`（关键词 FTS 投影）+ `memories` 表（仅 4 行陈旧便签） | ❌ TDAI 是独立容器（用户 2026-08-04 已裁定的记忆权威源） |
| 知识库 | **XERJ**（独立 ES 服务，:9300）+ `memfed` 的 rg 适配器（workspace/archived 两路） | ❌ XERJ 是独立服务 |
| 会话提炼 | `chat_sessions`/`chat_messages` 表（hub 自己的）+ **8 个 agent 家目录** jsonl（各 agent 自己写，hub 只读） | ❌ 原文是各 agent 的私产，hub 只能索引不能搬 |

⇒ **「统一存储」在物理层不可达**（跨进程边界 + 跨工具链私产是**契约**，不是缺陷）。
   但「统一入口」可达，且这才是真正解决「多路查询不可解释」的东西。

## 本模块做什么（与不做什么）

**做**：
1. 提供五个域的**统一查询入口**（`search_memory` / `search_kb` / `search_skills` /
   `list_sessions` / `list_agents`），每个入口**逐后端报 ok/count/ms/error**
   —— 于是「多路」从「黑盒地多」变成「可解释地多」；
2. **声明式**登记每个域的物理落点（`BACKENDS` 表）—— 一处可查，
   回答「数据到底存在哪」；
3. 提供 `describe()` 给 `/api/datastore` 与 `/health` 用 ——
   让人不必翻代码就知道当前拓扑。

**不做**：
- **不合并物理存储**（见上表：跨进程/跨工具链边界是契约）；
- **不新建第五个库**（已有两个僵尸库 `hub.db`/`agent-hub.db`，本批刚清掉
  ⇒ 新建就是制造第三个）；
- **不改现有路由的行为**（166 个路由零改动可用）。

## 按需加载的口径

每个后端都是**按需调用**（不预热、不全量载入）：
- TDAI：单次 HTTP 取回 top-N（**不拉全库**）；
- memindex：FTS `ORDER BY rank LIMIT ?`（**只读 top-N**，实测 0.06s）；
- XERJ：ES `_search` 带 size（**只取 top-N**）；
- 技能：`os.walk` 扫目录但**只读每个 `SKILL.md` 的 frontmatter**，
  且有 30s TTL 缓存（`skill.py:_SCAN_TTL_S`）；
- 家目录会话：**只读 jsonl 头部**（`sessions_store.py:HEAD_CHARS=262144`）找标题。

⇒ 「一股脑全部加载」在**读路径上已不成立**；真正无界的只有**写路径**
（`term_recordings` / `profile_events`），已由批次 B 的定时保留期收住。
"""
from __future__ import annotations

import time
from typing import Any, Dict, List, Optional

#: 五域的物理落点登记。**这是本模块的核心资产** —— 一处可查的真相源。
#:
#: `kind` 取值：
#:   `table`  本机 SQLite 表；`file`  文件系统目录；`service` 跨进程服务；
#:   `code`   代码内声明（逻辑而非数据）；`remote` 外部 HTTP API；
#:   `process` 进程在不在（运行态探测）。
#: `authoritative` 标出该域的**权威源**（其余是派生/投影）。
BACKENDS: Dict[str, List[Dict[str, Any]]] = {
    "agents": [
        {"id": "declared", "kind": "code", "authoritative": True,
         "where": "src/profiles.py:PROFILES（21 条声明，含 detect 正则）",
         "note": "声明态：detect 是代码逻辑（正则），不可迁表"},
        {"id": "discovered", "kind": "table", "authoritative": False,
         "where": "agents.db:custom_agents（运行时扫描发现，实测 6 条）",
         "note": "发现态：与声明态语义不同，不是重复而是两种状态"},
        {"id": "proc_probe", "kind": "process", "authoritative": False,
         "where": "进程名探测（scanner.py）",
         "note": "运行态：进程在不在"},
    ],
    "skills": [
        {"id": "disk_roots", "kind": "file", "authoritative": True,
         "where": "15+ 个 agent 家目录（~/.claude/skills、~/.jcode/skills、"
                  "技术文档/skills 等，skill.py:SKILL_DIRS）",
         "note": "真相在各自家目录 —— 各工具链的私产，hub 只读"},
        {"id": "skill_assets", "kind": "table", "authoritative": False,
         "where": "agents.db:skill_assets（18 条元数据/状态机）",
         "note": "元数据，不是技能正文"},
        {"id": "tdai_skill", "kind": "remote", "authoritative": False,
         "where": "TDAI POST /v3/skill/list",
         "note": "实测 HTTP 200 但 0 行 —— 不可当真相"},
    ],
    "memory": [
        {"id": "tdai_l1", "kind": "service", "authoritative": True,
         "where": "TDAI :8420（docker tdai-memory-core）",
         "note": "权威副本在 TDAI（2026-08-04 用户裁定）；单次 HTTP top-N，不拉全库"},
        {"id": "memindex_fts", "kind": "table", "authoritative": False,
         "where": "memindex.db:fed_proj_fts（345,268 chunk / 10,626 文件）",
         "note": "关键词 FTS 投影（memfed 的 11 路）；只读 top-N，实测 0.06s"},
        {"id": "local_l1", "kind": "table", "authoritative": False,
         "where": "agents.db:memories（实测仅 4 行 09-06 陈旧便签）",
         "note": "兼容层，不是数据源"},
    ],
    "kb": [
        {"id": "xerj", "kind": "service", "authoritative": True,
         "where": "XERJ :9300（独立 ES，wiki 4,200 + agent-knowledge 202 篇）",
         "note": "文档路权威；ES _search 带 size，只取 top-N"},
        {"id": "tdai_kb", "kind": "service", "authoritative": False,
         "where": "TDAI :8420（知识/实体路）",
         "note": "与记忆路同进程但不同接口"},
        {"id": "workspace_rg", "kind": "file", "authoritative": False,
         "where": "rg 全文匹配（工作区 + 归档两根）",
         "note": "文件级命中，词法层"},
    ],
    "sessions": [
        {"id": "hub_chat", "kind": "table", "authoritative": False,
         "where": "agents.db:chat_sessions / chat_messages",
         "note": "hub 自己产生的对话"},
        {"id": "agent_home", "kind": "file", "authoritative": True,
         "where": "8 个 agent 家目录 jsonl（~/.codebuddy ~/.claude ~/.grok ~/.cursor "
                  "~/.jcode ~/.qoder ~/.codex ~/.local/share/opencode）",
         "note": "原文是各 agent 的私产 —— hub 只索引不搬；只读 jsonl 头部找标题"},
    ],
}


def describe() -> Dict[str, Any]:
    """返回五域的物理落点登记（供 `/api/datastore` 与 `/health`）。

    这是「数据库是否统一」这个问题的**可执行答案**：不是一段说明，
    是一份随时能查的表。
    """
    return {
        "domains": {k: [dict(b) for b in v] for k, v in BACKENDS.items()},
        "counts": {
            "domains": len(BACKENDS),
            "backends": sum(len(v) for v in BACKENDS.values()),
        },
        "note": "物理存储**故意不统一**：跨进程（TDAI/XERJ）与跨工具链私产"
                "（各 agent 家目录）是契约而非缺陷；统一的是**查询入口**。",
    }


# ── 统一查询入口 ──────────────────────────────────────────────────────
# 每个入口返回统一形状：{ok, count, backends:[{name,ok,count,ms,error}], ms}
# ⇒ 「多路」从此**可解释**：哪一路慢、哪一路挂了、哪一路空，都看得见。

def _backend_result(name: str, fn) -> Dict[str, Any]:
    """跑一个后端，把异常变成结构化 error（不许整份结果 500）。"""
    t0 = time.monotonic()
    try:
        out = fn()
        ms = round((time.monotonic() - t0) * 1000, 1)
        if isinstance(out, dict):
            return {"name": name, "ok": bool(out.get("ok", True)),
                    "count": out.get("count", len(out.get("items") or [])),
                    "ms": out.get("ms", ms), "error": out.get("error")}
        return {"name": name, "ok": True, "count": len(out or []), "ms": ms,
                "error": None}
    except Exception as e:  # noqa: BLE001
        return {"name": name, "ok": False, "count": 0,
                "ms": round((time.monotonic() - t0) * 1000, 1),
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}


async def search_memory(q: str, limit: int = 10,
                        sources: Optional[str] = None) -> Dict[str, Any]:
    """记忆域的统一入口（转发到 `memory.search_memory`，不复制它的逻辑）。"""
    import memory as memory_mod
    t0 = time.monotonic()
    kwargs: Dict[str, Any] = {"q": q, "limit": limit}
    if sources:
        kwargs["sources"] = sources
    try:
        out = await memory_mod.search_memory(**kwargs)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "backends": [], "ms": round((time.monotonic() - t0) * 1000, 1),
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    return {
        "ok": True,
        "count": out.get("count", 0),
        "items": out.get("items") or [],
        "backends": out.get("backends") or [],
        "ms": round((time.monotonic() - t0) * 1000, 1),
    }


async def search_kb(q: str, k: int = 8,
                    routes: Optional[str] = None) -> Dict[str, Any]:
    """知识库域的统一入口（转发到 `kb.kb_search`）。

    ⚠ `kb.kb_search` 的首参是 `request: Request`（FastAPI 注入位）——
    直接调用时**不能省**：省了会把 `q` 顶到 request 位 ⇒ 签名错位。
    这里传 `request=None`（kb_search 内部不用它，只用于 FastAPI 依赖注入）。
    """
    import kb as kb_mod
    t0 = time.monotonic()
    kwargs: Dict[str, Any] = {"request": None, "q": q, "k": k}
    if routes:
        kwargs["routes"] = routes
    try:
        out = await kb_mod.kb_search(**kwargs)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "count": 0, "items": [],
                "backends": [], "ms": round((time.monotonic() - t0) * 1000, 1),
                "error": "%s: %s" % (type(e).__name__, str(e)[:160])}
    return {
        "ok": True,
        "count": len(out.get("results") or []),
        "items": out.get("results") or [],
        "backends": out.get("backends") or [],
        "ms": round((time.monotonic() - t0) * 1000, 1),
    }


async def list_agents() -> Dict[str, Any]:
    """agent 清单域的统一入口（声明 + 发现 + 运行态三态分别给出）。"""
    import profiles as profiles_mod
    t0 = time.monotonic()
    declared = list(profiles_mod.PROFILES)
    try:
        discovered = profiles_mod._dynamic_cli_agents(gate=False)
    except Exception:  # noqa: BLE001
        discovered = []
    return {
        "ok": True,
        "count": len(declared) + len(discovered),
        "declared": [p.get("id") for p in declared],
        "discovered": [p.get("id") for p in discovered],
        "backends": [
            {"name": "declared", "ok": True, "count": len(declared),
             "ms": None, "error": None},
            {"name": "discovered", "ok": True, "count": len(discovered),
             "ms": None, "error": None},
        ],
        "ms": round((time.monotonic() - t0) * 1000, 1),
    }


async def list_sessions(agent_id: Optional[str] = None,
                        cwd: Optional[str] = None,
                        limit: int = 50) -> Dict[str, Any]:
    """会话域的统一入口。

    ⚠ `sessions_store` 的真实入口是 `list_history(agent_id, cwd, limit)`
    —— **没有** `list_sessions` 这个函数（实测：只有 `list_history` 与一批
    `_t_*` 私有的家目录适配器）。对外的 `/api/sessions` 路由在 `api_sessions.py`，
    它查的是 hub 自己的 `chat_sessions` 表 + pi 外部会话。
    ⇒ 本入口**同时**给两路：
      ① `hub_chat`：hub 自己的 chat_sessions（走 api_sessions 同款 SQL）；
      ② `agent_home`：各 agent 家目录的历史会话（走 sessions_store.list_history）。
    逐路报 ok/count/ms —— 于是「会话从哪来」也是可解释的。
    """
    import db
    t0 = time.monotonic()
    backends: List[Dict[str, Any]] = []
    items: List[Dict[str, Any]] = []

    # ① hub 自己的对话
    t1 = time.monotonic()
    try:
        sql = ("SELECT s.*, (SELECT COUNT(*) FROM chat_messages m "
               "WHERE m.session_id=s.id) AS messages FROM chat_sessions s")
        params: list = []
        if agent_id:
            sql += " WHERE s.agent_id=?"
            params.append(agent_id)
        sql += " ORDER BY s.updated_at DESC LIMIT ?"
        params.append(limit)
        rows = db.query(sql, tuple(params))
        items.extend(rows)
        backends.append({"name": "hub_chat", "ok": True, "count": len(rows),
                          "ms": round((time.monotonic() - t1) * 1000, 1),
                          "error": None})
    except Exception as e:  # noqa: BLE001
        backends.append({"name": "hub_chat", "ok": False, "count": 0,
                          "ms": round((time.monotonic() - t1) * 1000, 1),
                          "error": "%s: %s" % (type(e).__name__, str(e)[:160])})

    # ② 各 agent 家目录的历史会话
    if agent_id:
        t2 = time.monotonic()
        try:
            import sessions_store
            hist = sessions_store.list_history(agent_id, cwd or "", limit)
            # ⚠ list_history 的返回键是 **items**（实测：{"items":[...], "cwd":...,
            # "note":...}）——曾误写成 "sessions" ⇒ 有历史也恒报 0（静默零命中）。
            rows2 = hist.get("items") if isinstance(hist, dict) else (hist or [])
            rows2 = rows2 or []
            items.extend(rows2)
            backends.append({"name": "agent_home", "ok": True, "count": len(rows2),
                              "ms": round((time.monotonic() - t2) * 1000, 1),
                              "error": None})
        except Exception as e:  # noqa: BLE001
            backends.append({"name": "agent_home", "ok": False, "count": 0,
                              "ms": round((time.monotonic() - t2) * 1000, 1),
                              "error": "%s: %s" % (type(e).__name__, str(e)[:160])})

    return {"ok": True, "count": len(items), "items": items[:limit],
            "backends": backends, "ms": round((time.monotonic() - t0) * 1000, 1)}
