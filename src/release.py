"""Agent Release manifest（P2 A1）：把「这个 agent 现在是什么」聚成一个**带 hash 的发布单元**。

## 为什么需要它

手册 A1：发布单元 = 模型 + 参数 + 预算 + Harness 配置 + Prompt/Skill/Knowledge 版本 +
Tool 权限 + 评估基线，**绑定可追溯**。本机这些信息**分散在六个地方**，各读各的：

| 维度 | 来源 | 现状 |
|---|---|---|
| 模型 / 参数 | `modelcfg.agent_state()` | 各 agent 配置文件（claude/jcode/codex/pi/grok） |
| 画像 / 终端 | `profiles.PROFILES` | 代码常量 |
| Skill 版本 | `skill_assets.list_assets()` | P1-5 状态机表 |
| 知识库 | `kb` 索引 `built_at` | turbovec meta |
| Tool 权限 | `mcpgw` 的 `mcp_acl` | ACL 规则 |
| 执行语义 | `execsem` 词表版本 | 代码常量 |

⇒ **没有「这个 agent 现在是哪个版本」这个概念**。改一个模型、换一版 skill、动一条 ACL，
从界面上看**什么都没变**；出问题回滚时也**说不清回到哪个组合**。
本模块只做**聚合 + 指纹**，不新增任何可写状态。

## 三条硬口径

1. **只读、零写入**：本模块不写库、不改配置、不落缓存。manifest 每次现算
   （文件读盘 + sqlite 只读查询，实测量级见 `_MEASURE_NOTE`）。
2. **`release_id` 只由「语义字段」算出，**不吃时间戳/耗时/绝对路径**：
   否则同一配置十分钟后再算就换了 id，「版本」这个概念当场作废
   （与 `ctxmanifest.manifest_hash`、`resilience.idempotency_key` 同一条纪律）。
   文件路径进 `files` 只作展示，**不进 hash** —— 换目录不是换版本。
3. **不可判定就报 `unknown`，不猜也不留空**：某个源读失败（配置文件丢了、表不存在）
   时该维度记 `{"state": "unknown", "reason": ...}`，并且 **`degraded=True`**。
   把「读不到」写成 `{}` 会让下游把「不知道」读成「没有」（本仓反复禁的静默降级）。
"""
from __future__ import annotations

import hashlib
import json
from typing import Any, Dict, List, Optional

#: manifest 的模式版本。**结构变了就升它** —— 否则旧 manifest 会被按新结构解读。
RELEASE_SCHEMA = "release/1"

#: 实测备注：`build_manifest` 单次耗时（本机 NAS，配置读盘 + 1 次 sqlite 只读查询）
#: 在毫秒级；`/api/agents` 每次请求调用一次**可接受**，故不做缓存
#: （缓存会引入「改了配置但 manifest 还是旧的」这一类假绿，得不偿失）。
_MEASURE_NOTE = "ms 级；不缓存"


def _canon(obj: Any) -> str:
    """确定性 JSON：`sort_keys` + 紧凑分隔符 + `ensure_ascii=False`。

    与 `settings_bundle` 的「两次导出逐字节相同」同源：**指纹要先能稳定序列化**。
    """
    return json.dumps(obj, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def fingerprint(payload: Dict[str, Any]) -> str:
    """`payload` 的 sha256 前 16 位（短到能放进 UI）。**纯函数**。

    取前 16 位不是安全用途，是「人能念出来对账」的长度；碰撞概率对版本标识足够
    （不是密码学身份）。
    """
    return hashlib.sha256(_canon(payload).encode("utf-8")).hexdigest()[:16]


def _dim_model(agent_id: str) -> Dict[str, Any]:
    """模型维度。**取 `current` 值本身，不 hash 配置文件内容。**

    为什么不 hash 配置文件内容：`modelcfg` 的读函数已经给出了「该 agent 当前生效的
    模型」这个**语义值**；hash 整个配置文件会把注释/顺序调整也算成换版本（噪声）。
    语义值才是发布单元的一部分。

    ⚠ `files` 在这里返回，但 **`_semantic_view` 会把它剥掉再进指纹**。
    第一版没剥 ⇒ 本模块 docstring 写着「换目录不是换版本」，实测却换目录就换 id
    —— **文档与实现相反，闸门当场打红**（判例 198）。路径是**去哪读**，不是
    **发布的是什么**；把它放进指纹等于「迁移一次家目录就发一次新版本」。
    """
    try:
        import modelcfg
        st = modelcfg.agent_state(agent_id)
    except Exception as e:  # noqa: BLE001 —— 读不到必须显式 unknown
        return {"state": "unknown", "reason": "%s: %s" % (type(e).__name__, e)}
    if not st.get("supported"):
        return {"state": "unsupported",
                "reason": st.get("reason") or "不在模型设置白名单内",
                "current": st.get("current") or ""}
    return {"state": "known", "current": st.get("current") or "",
            "provider": st.get("provider") or "",
            # 路径只展示，**不进指纹**（见 `_semantic_view` 的剥离）
            "files": list(st.get("files") or [])}


def _dim_profile(agent_id: str) -> Dict[str, Any]:
    """画像维度：只取**影响执行语义**的字段（kind/cli/port/terminal.cmd/adapter）。

    `desc` 是文案、`detect` 是探测规则 —— 两者改了不改变「发布出去的是什么」。
    把它们算进指纹会让「改个描述文字」也变成新版本（版本通胀）。
    """
    try:
        import profiles
        p = profiles.get_profile(agent_id)
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "reason": "%s: %s" % (type(e).__name__, e)}
    if not p:
        return {"state": "unknown", "reason": "画像不存在（可能已下线或未注册）"}
    term = p.get("terminal") or {}
    chat = p.get("chat") or {}
    return {"state": "known", "kind": p.get("kind"), "cli": p.get("cli"),
            "port": p.get("port"), "terminal_cmd": term.get("cmd"),
            "chat_adapter": chat.get("adapter")}


def _ensure_db() -> None:
    """保证 `db` 有连接。**只在没连接时初始化一次**。

    为什么需要：`best-effort.skill_assets`/`mcpgw._acl_rows` 都读模块级全局 `_conn`，
    而它只由 `main.init_db()` 在服务启动时设置。本模块会被**进程外**调用（冒烟脚本、
    测试、将来的 CLI `agenthub release`），那里没有 `_conn` ⇒ 直接
    `AttributeError: 'NoneType' object has no attribute 'execute'`。
    实测就是这么撞的：`skills`/`acl` 两维双 unknown，而库里 `mcp_acl` 明明有 4 条。

    判据用 `db.is_open()` 而不是无条件 `init_db()` —— 后者会把全局连接**重指**到
    `config.db_path`，在进程内测试里等于把 tmp 库悄悄换成生产库（09-24 已实际污染过一次，
    见 `db.is_open` 的 docstring）。这里只是"补一次"，绝不"重指"。
    """
    import db
    if db.is_open():
        return
    import config
    db.init_db(config.Config().db_path)


def _dim_skills(limit: int = 200) -> Dict[str, Any]:
    """Skill 维度：**只取已发布的** slug→version，并带 `effective_status`。

    为什么要带 `effective_status`：P1-5 的设计是「发布时记 digest，读盘不等则 stale」。
    一个 stale 的 skill 说明**磁盘上的东西已经不是发布出去的那个**——
    这恰恰是发布单元最该暴露的偏差，不带它就等于把「skill 悄悄变了」藏起来。

    ⚠ 两个曾经写错的点（都被真数据/闸门抓过）：
      ① **`current_digest` 不能传 `None`**：`is_stale(rec, None)` 恒 False
         （"判不了就不拦"），于是 stale **永远算不出来** ⇒ 判据恒假、闸门空转
         （判例 198）。这里走 `skill_assets._current_digest(rec, True)`，与
         `/api/skill-assets` 同一口径。
      ② **不收 `stale`/`degraded`**：stale 资产仍**在役**（它只是与发布时不符），
         把它排除会让「skill 悄悄变了」从 manifest 里消失 —— 正好倒过来。
    """
    try:
        _ensure_db()
        import skill_assets
        rows = skill_assets.list_assets(limit=limit)
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "reason": "%s: %s" % (type(e).__name__, e)}
    out = []
    for r in rows or []:
        try:
            cur = skill_assets._current_digest(r, True)
            eff = skill_assets.effective_status(r, cur)
        except Exception:  # noqa: BLE001
            eff = "unknown"
        if eff in ("draft", "deprecated"):
            continue          # 未发布的 skill 不是发布单元的一部分
        out.append({"slug": r.get("slug"), "version": r.get("version"),
                    "status": eff, "digest": r.get("digest") or ""})
    out.sort(key=lambda x: str(x.get("slug")))
    return {"state": "known", "skills": out, "count": len(out)}


def _dim_acl(agent_id: str) -> Dict[str, Any]:
    """Tool 权限维度：该 agent 命中的 ACL 规则（含 `*` 通配），按 (server, tool) 排序。

    **只取判定相关字段**（server_id/tool_pattern/allow/effect），不取 id/时间
    —— 规则重排不该产生新版本，但 `effect` 必须进（enforce↔shadow 是**行为变化**，
    P2 C2 刚把它分开）。

    ⚠ 两个都翻过车的点：
      ① **列名是 `tool_pattern`，不是 `tool`**（第一版写 `r.get("tool")`）——
         `.get` 静默返 None，于是每条规则都长成 `{"tool": null}`，
         指纹看着正常、字段看着填了，实际**把「限哪个工具」丢干净了**。
         没有 KeyError 提醒，因为它查的是一个"不存在的键"而不是"不存在的列"。
      ② **不为了 6 行表 `import mcpgw`**：`mcpgw` 顶层拉 `mcp` SDK + `resilience`
         + `outbound`，实测**冷导入 894ms**（一次性，但首发请求就吃这个）。
         ACL 读的就是一张表，直接走 `db.query` —— 判据只有"该 agent 命中的行"这一条，
         不必为了复用一句 SQL 把整个 MCP 栈拖进来。
         `effect` 的折算口径必须与 `mcpgw._acl_effect` **一致**（缺列/空值= enforce），
         故此处逐字复刻那两条规则，并在测试里对照两边的结论。
    """
    try:
        _ensure_db()
        import db
        rows = db.query(
            "SELECT * FROM mcp_acl WHERE agent_id IN (?, '*') ORDER BY id",
            (agent_id,))
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "reason": "%s: %s" % (type(e).__name__, e)}
    rules: List[Dict[str, Any]] = []
    for r in rows or []:
        eff = (r.get("effect") or "").strip().lower()
        rules.append({"server": r.get("server_id"),
                      "tool": r.get("tool_pattern"),   # ← 列名是 tool_pattern
                      "allow": bool(r.get("allow")),
                      "effect": "shadow" if eff == "shadow" else "enforce"})
    rules.sort(key=lambda x: (str(x["server"]), str(x["tool"])))
    return {"state": "known", "rules": rules, "count": len(rules)}


def _dim_execsem() -> Dict[str, Any]:
    """执行语义词表维度：P2 C3 的 `SOURCES`/`OUTCOMES` 是跨源公共词表，
    改它等于改「事件怎么被解读」⇒ 属于发布单元。指纹取排序后的词表本体。"""
    try:
        import execsem
        sources = sorted(execsem.SOURCES)
        outcomes = sorted(execsem.OUTCOMES)
    except Exception as e:  # noqa: BLE001
        return {"state": "unknown", "reason": "%s: %s" % (type(e).__name__, e)}
    return {"state": "known", "sources": sources, "outcomes": outcomes}


def _semantic_view(agent_id: str) -> Dict[str, Any]:
    """**进指纹的那部分**：只有语义维度，不含路径/时间/耗时。

    `model` 在这里**剥掉 `files`**：路径是「去哪读配置」，不是「发布的是什么」。
    这是本模块 docstring 第 2 条口径的落点 —— 声明了口径却没有落点，
    等于没声明（第一版就漏了这步，闸门把「第 2 条」打红）。
    """
    model = dict(_dim_model(agent_id))
    model.pop("files", None)      # 只展示，不进指纹
    return {
        "agent_id": agent_id,          # 身份是语义的一部分
        "model": model,
        "profile": _dim_profile(agent_id),
        "skills": _dim_skills(),
        "acl": _dim_acl(agent_id),
        "execsem": _dim_execsem(),
    }


def build_manifest(agent_id: str) -> Dict[str, Any]:
    """为单个 agent 生成 release manifest（**只读**，每次现算）。

    回包：`release_id` / `degraded` / `dimensions` / `schema`。
    `degraded=True` ⇒ 至少一个维度是 `unknown`：**这个 release_id 不完整**，
    调用方不得把它当作「已绑定」。这与 `evidence.insufficient` 同一立场。
    """
    dims = _semantic_view(agent_id)
    unknown = sorted(k for k, v in dims.items()
                     if isinstance(v, dict) and v.get("state") == "unknown")
    rid = fingerprint(dims)
    return {
        "schema": RELEASE_SCHEMA,
        "agent_id": agent_id,
        "release_id": rid,
        "degraded": bool(unknown),
        "unknown_dimensions": unknown,
        # 指纹只覆盖语义维度；展示层可额外带路径等（此处已含在 dimensions.model.files）
        "dimensions": dims,
        # 显式告诉调用方「id 怎么来的」—— 否则没人能判断改什么会换 id
        "release_id_covers": ["model.current", "profile(kind/cli/port/terminal_cmd/chat_adapter)",
                              "skills(published/reviewed/degraded)", "acl(server/tool/allow/effect)",
                              "execsem(sources/outcomes)"],
        "release_id_excludes": ["文件路径", "时间戳", "耗时", "描述文案", "detect 规则"],
    }


def manifests_for(agent_ids: Optional[List[str]] = None) -> Dict[str, Any]:
    """批量生成（供 `/api/agents` 一次列出）。

    `agent_ids` 缺省 ⇒ 取 `profiles.all_profiles()` 的全量 id。
    某个 agent 计算失败**不影响其它**（逐个 try），失败项记 `degraded` 且带原因。
    """
    ids = agent_ids
    if ids is None:
        try:
            import profiles
            ids = [p.get("id") for p in profiles.all_profiles() if p.get("id")]
        except Exception:  # noqa: BLE001
            ids = []
    out: Dict[str, Any] = {}
    for aid in ids:
        try:
            out[aid] = build_manifest(aid)
        except Exception as e:  # noqa: BLE001
            out[aid] = {"schema": RELEASE_SCHEMA, "agent_id": aid, "release_id": None,
                        "degraded": True, "unknown_dimensions": ["*"],
                        "error": "%s: %s" % (type(e).__name__, e)}
    return {"schema": RELEASE_SCHEMA, "count": len(out), "manifests": out,
            "note": _MEASURE_NOTE}
