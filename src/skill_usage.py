"""D3：技能调用记账与「零调用僵尸榜」。

**两源合并**：hub 通道（`source='rest'`）+ 会话转录采集（`source='scan'`，见
`skill_direct`），subject ∈ {`skill.read`, `skill.inject`, `skill.relevant`, `skill.invoke`}。

军规落地（禁把 SKIP 当 PASS）：**第二数据源只覆盖「读取 SKILL.md / Skill 工具调用」**，
写盘与纯提及仍不计 ⇒ 置信度**仍封顶 `medium`**，不因有了第二源就报 `high`。
`DIRECT_SOURCE = "transcript"` 把「第二源是什么、覆盖到哪」变成返回值里可断言的字段。

**绝不自动删技能**：只出 `suggested_action`。理由同设计书 §7——hub 是唯一权威门面，
自动退役等于让「没被我的仪表盘看见」直接等价于「该删」，而第二源缺席时这个等价是错的。

实现注意（照抄前先核过，避免留下虚构 API）：
- 读库走 `db.query(sql, params) -> list[dict]`（`db.fetchall` 不存在，计划书那段是虚构的）。
- `created_at` 落盘形如 `2026-10-03T03:13:41.975138+00:00`，与 `datetime.now(timezone.utc).isoformat()`
  同为「ISO + 微秒 + +00:00」，**同一时区同一格式 ⇒ 字符串比较即时间比较**（存的都是 UTC）。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List

#: 第二数据源：各家 agent 会话转录（`skill_direct.scan` 采集，落 `source='scan'`）。
#: 2026-10-27 之前这里是 "not-implemented" —— 面板上「调用记录」只有 hub 自己那三个
#: 显式路由，agent 真读 SKILL.md 的用法一条都看不到（判例 228 只改了文案没改生产者）。
DIRECT_SOURCE = "transcript"

#: 计入记账的 subject。读库用 IN 展开，故须是 tuple 而非集合（sqlite3 接收序列）。
#:
#: 【为什么含 `skill.relevant`（2026-10-10 补）】注入链——pi 的 `hub-facade.ts`（D4）与
#: claude 的 `scripts/hub_skill_inject.py`（D5）——**只打 `/api/skill/relevant`**，它俩
#: 注入的正是该端点返回的 top-N。也就是说：**「注入」这道动作在服务端唯一可见的留痕
#: 就是 `skill.relevant` 事件**。原先只认 read/inject ⇒ 注入被静默丢弃 ⇒ 生产 `counted`
#: 恒为 0，把全机最热的 `agent-dispatch`（实测被用 166 次）都列成僵尸（判例 103/104）。
#:
#: 【`skill.inject` 的生产者（2026-10-27 补）】注入链（pi `hub-facade.ts` / claude
#: `hub_skill_inject.py`）打 `/api/skill/relevant?via=hook` 时，服务端把**实际返回的
#: 候选**逐条落成 `skill.inject`（`runlog._subject_for`）。此前这件事被记成
#: `skill.relevant` ⇒ 「注入」在账上不存在，`skill.inject` 恒 0。现在两者互斥：
#: 注入链 = inject，人工检索 = relevant，同一请求不会双记。
#:
#: 【为什么含 `skill.invoke`（2026-10-19 PT-20261019-01 补）】统一调用入口
#: （POST /api/skill/invoke + MCP hub_skill_invoke）是**显式激活**：agent/用户点名
#: 要某个技能并拿走了指令正文。这是比 read 更强的使用证据——不计数的话，
#: 「天天被 invoke 的技能」反而会上零调用榜，与榜单的立身目的正相反。
#: 单独记 `invokes` 桶（不并入 reads）：调用流水面板要能分开展示
#: 「被看过」与「被调用」，混一桶则面板退化成猜。
_SUBJECTS = ("skill.read", "skill.inject", "skill.relevant", "skill.invoke")

#: hub 自身事件通道。`hook.py:131` 的 agent 画像聚合是按 `source != 'rest'` 排除本通道的，
#: 也就是说 'rest' 正是「hub 自己干的事」——技能门面的读写走这里。
_SOURCE = "rest"

#: 计入统计的两个 source：hub 显式通道 + 转录采集通道（`skill_direct`）。
#: 两者必须分开保留 provider 字段（`via`），否则「机器记账」与「事后扫描」混成一桶，
#: 出问题时无法判断是记账坏了还是采集坏了。
_SOURCES = ("rest", "scan")

MAX_DAYS = 365


def _clamp(days: int) -> int:
    """把 days 收进 [1, 365]。0/负数/None 全部回落到 1——宁可窗口最小，也不让 SQL 变成全表。"""
    try:
        d = int(days)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_DAYS, d))


def counts(days: int = 7) -> Dict[str, Dict[str, Any]]:
    """近 N 天各技能被读/被注入的次数。

    **DB 读不到就返回空 dict，绝不抛**——仪表盘必须能开；L0 hermetic 下 MEMINDEX_DB
    被钉到不存在的路径，这里正好走「读不到 ⇒ 空表 ⇒ 全部技能都算零调用」这条分支。
    """
    since = (datetime.now(timezone.utc) - timedelta(days=_clamp(days))).isoformat()
    out: Dict[str, Dict[str, Any]] = {}
    # 占位符按 _SUBJECTS 长度现算：写死 `(?, ?)` 会在增 subject 时静默错位
    #（sqlite3 参数个数不符会抛，但「少写一个 ? 却多传一个值」这类迟早发生）。
    holders = ",".join("?" * len(_SUBJECTS))
    sholders = ",".join("?" * len(_SOURCES))
    try:
        import db
        rows = db.query(
            "SELECT source, subject, detail, created_at FROM profile_events "
            "WHERE source IN (%s) AND subject IN (%s) AND created_at >= ? "
            "ORDER BY id" % (sholders, holders),
            _SOURCES + _SUBJECTS + (since,))
    except Exception:
        # 面板要能开：查不到记账就当没人被调用过，僵尸榜会偏大但**方向是保守的**
        #（多列嫌疑而不是漏列嫌疑）。宁可多提醒也不漏提醒。
        return out

    for row in rows or []:
        name = _name_of(row.get("detail"))
        if not name:
            continue
        e = out.setdefault(name, {"reads": 0, "injects": 0, "invokes": 0, "last_at": "", "via": []})
        subj = row.get("subject")
        if subj == "skill.read":
            e["reads"] += 1
        elif subj == "skill.invoke":
            # v0.13.127 PT-20261019-01：显式调用单列一桶（面板要分「看过」与「调用」）
            e["invokes"] += 1
        else:
            # skill.inject / skill.relevant 都归「注入」桶：relevant 是注入链的选人动作，
            # 对僵尸榜而言「被选进候选并被推给 agent」与「被显式注入」是同一件事
            #（`zombies()` 只判 reads/injects/invokes 是否全零，桶标签只影响展示）。
            e["injects"] += 1
        at = str(row.get("created_at") or "")
        if at > e["last_at"]:
            e["last_at"] = at
        # via 用真实来源标注：hub 通道 vs 转录采集。混成一个 'hub' 会让
        # 「记账坏了」和「扫描没跑」在面板上长得一模一样。
        via = "scan" if row.get("source") == "scan" else "hub"
        if via not in e["via"]:
            e["via"].append(via)
    return out


def _name_of(detail: Any) -> str:
    """从事件 detail 里取技能名。**坏 JSON 不许炸掉整张榜**——单条脏数据不能带倒面板。"""
    if not detail:
        return ""
    if isinstance(detail, str):
        try:
            detail = json.loads(detail)
        except Exception:
            return ""
    if not isinstance(detail, dict):
        return ""
    return str(detail.get("name") or "").strip()


def _action_for(item: Dict[str, Any], routes: List[str]) -> str:
    """给零调用技能一条**建议**（不是命令）。

    判据顺序是「先补可发现性，再谈退役」：
    - 只在一路可见 ⇒ 先扩大可见性（它可能压根没机会被选中）
    - 有描述但零调用 ⇒ 描述没写出触发词，补 triggers
    - 描述也空或多路可见仍零调用 ⇒ 才值得进退役评估
    """
    if len(routes) <= 1:
        return "widen_visibility"
    if not str(item.get("description") or "").strip():
        return "add_triggers"
    return "retire_review"


def zombies(items: Iterable[Dict[str, Any]], counts_map: Dict[str, Dict[str, Any]],
            days: int = 7) -> List[Dict[str, Any]]:
    """列出近 N 天零调用的技能。置信度**恒为 medium**（第二源未取证，不得报 high）。"""
    d = _clamp(days)
    rows: List[Dict[str, Any]] = []
    seen = set()
    for it in items or []:
        name = str(it.get("name") or "").strip()
        if not name or name in seen:
            continue
        c = counts_map.get(name) or {}
        if (c.get("reads") or 0) or (c.get("injects") or 0) or (c.get("invokes") or 0):
            continue
        seen.add(name)
        routes = [str(x) for x in (it.get("routes") or []) if str(x)]
        rows.append({
            "name": name,
            "routes": routes,
            "days_idle": d,
            "last_at": None,
            "confidence": "medium",          # ← 封顶值。报 high 需要第二源，本批没有
            "direct_source": DIRECT_SOURCE,
            "suggested_action": _action_for(it, routes),
        })
    rows.sort(key=lambda r: r["name"].lower())
    return rows


def snapshot(items: Iterable[Dict[str, Any]], days: int = 7) -> Dict[str, Any]:
    """榜单体。`counted` 显式回显**查到了多少个技能的记账**——0 就说明账是空的，
    此时 `zombies` 数≈`total` 是必然的，前端必须能区分「真的没人用」和「压根没记账」。"""
    items = list(items or [])
    cmap = counts(days)
    z = zombies(items, cmap, days)
    return {
        "days": _clamp(days),
        "total": len(items),
        "zombies": z,
        "zombies_count": len(z),
        "counted": len(cmap),          # 账里有痕迹的技能数；0 = 无记账
        "confidence": "medium",
        "direct_source": DIRECT_SOURCE,
        "note": "两源合并：hub 通道（rest）+ 会话转录采集（scan）；"
                "写盘/纯提及仍不计 ⇒ 置信度封顶 medium",
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }
