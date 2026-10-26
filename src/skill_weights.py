"""技能权重（PT-20261020-01）：把「曝光」与「使用」分成两个量，再合成一个可分桶追溯的分数。

【为什么不能直接数调用次数】
`skill.relevant` 是注入链的留痕：agent 每次输入都会拿到 top-3 候选，**没人点名任何技能**。
把它的次数与 `skill.invoke` 相加，等于把「被推给 agent」当成「agent 用了」——
全机最热会变成「每轮都被推但没人读」的那几个，与「常用技能」的语义正相反。

【公式】
    使用量 use = 2×explicit + 1×read + 1×auto_use
    曝光量 auto = skill.inject（注入链，2026-10-27 起）
                + skill.relevant 中 mode=auto_inject 的旧账
    score = use² / (use + auto)      （use+auto=0 时为 0）

`auto_use`（v0.13.138）＝ `/api/skill/auto` 自己按任务描述选中并**激活**的技能。
它既不是「同一批候选的曝光」（那个是 auto），也不是「有人点名」（那个是 explicit）：
机器选完把说明书交出去了，技能**确实被用了**，但不该与指名调用同权（少一分信任），
更不该只当曝光（那样自动取用会被算成 0 分，等于「越自动越吃亏」）。

三个性质是可验证的，不是修辞：

1. **从未曝光**（auto=0）⇒ score = use。手工调用/阅读不受惩罚。
2. **只曝光不使用**（use=0）⇒ score = 0。「每次都被推、从未被打开」不会凭空得分。
3. **曝光只作分母**：auto 增大时 score 单调不增；它永远不是加分项。

为什么 explicit 记 2 分：`invoke` 返回的是拿走去执行的指令正文，`read` 只是看了一眼。
两者都是使用证据，但强度不同；这个 2:1 是**可改的常量**（`W_EXPLICIT/W_READ`），
改之前先想清楚「权重」是给谁用的——排序建议与自动注入预算用的不是同一个刻度。

【身份】
技能身份 = `(name, route)`。同名技能可存在于多个发现点（v0.13.131 已确立按 route 区分），
把两路的账合并会掩盖「这一路是死链、那一路在用」。同时给 `by_name` 汇总（routes 并列），
供只看技能名的场景使用——但**分数在各自层级重算**，不是把子分数相加。

【不可回溯的旧账】
2026-10-20 之前 `skill.relevant` 没有 `mode`/`via`：既可能是 UI 里人工查的，也可能是注入链打的，
服务端当时没记。这类事件一律记 `legacy`，**不进 auto 也不进 use**，并在 totals 里显式报数。
按时间或技能名反推它们属于哪个 agent，就是造数据。
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

import db

#: 计入权重的两个事件通道：`rest`＝hub 自己产生的门面事件；`scan`＝转录采集
#: （`skill_direct`，agent 直读磁盘/调 Skill 工具，不经过 hub）。
#: **只读 rest 会让扫描到的使用在页面榜上完全不可见**（v0.13.165 实测：7 天 180 条
#: scan 事件、agent-dispatch 42 条，`/api/skill/usage` 仍报 explicit=0）。
SOURCES = ("rest", "scan")

#: 计入权重的 subject。`skill.list` 是浏览目录，不是使用。
#: `skill.inject` = 注入链真把候选塞进了上下文（2026-10-27 起由 `/relevant?via=hook`
#: 落账）；`skill.relevant` 只剩人工检索。两者都是曝光，进同一桶。
SUBJECTS = ("skill.read", "skill.invoke", "skill.relevant", "skill.inject")

#: 使用强度。点名 invoke 拿走指令正文（要执行）＝2；read 只是查看＝1；
#: auto_use 是服务端选出来并激活（不是人点的）＝1，与 read 同权而已——它比 read
#: 强（交付了正文），但比 explicit 弱（没人许诺要用它）。
W_EXPLICIT = 2
W_READ = 1
W_AUTO_USE = 1

#: 每条明细最多回带多少个 event id（可追溯性 vs 响应体积）。超出只报数。
EVIDENCE_CAP = 20

#: 「多久没被点名用过」算冷。**只作观察，绝不据此建议删除**（台账 PT-20261020-01 第 4 条：
#: 阈值定稿前不得用于清理；skill_usage 的既有纪律也是绝不自动删）。
STALE_AFTER_DAYS = 30

MAX_DAYS = 365
DEFAULT_LIMIT = 100
MAX_LIMIT = 500


def _clamp_days(days: int) -> int:
    try:
        d = int(days)
    except (TypeError, ValueError):
        return 1
    return max(1, min(MAX_DAYS, d))


def score(use: int, auto: int) -> Tuple[float, float]:
    """返回 `(score, conversion)`；`conversion = use/(use+auto)`。

    conversion 单独给出，是因为「score 低」有两种完全不同的原因：
    用得少（use 小），或者被推得多却没打开（auto 大）。只报一个数看不出是哪一种。
    """
    use = max(0, int(use))
    auto = max(0, int(auto))
    denom = use + auto
    if denom <= 0:
        return 0.0, 0.0
    conv = use / denom
    return round(use * conv, 3), round(conv, 4)


def _parse_detail(raw: Any) -> Optional[Dict[str, Any]]:
    try:
        det = json.loads(raw) if isinstance(raw, str) else raw
    except Exception:  # noqa: BLE001 — 坏行跳过并计数，绝不带倒整表
        return None
    return det if isinstance(det, dict) else None


def classify(subject: str, det: Dict[str, Any]) -> Tuple[str, str]:
    """把一个事件归到 `(bucket, name)`。bucket ∈ explicit/read/auto_use/auto/query/legacy/skip。

    判据只有一条：**auto 必须由调用方显式声明**（`mode=auto_inject` 或旧写法 `via=hook`）。
    缺声明时宁可归 legacy，也不猜——猜错的代价是权重被系统性抬高。
    """
    name = str(det.get("name") or det.get("first_name") or "").strip()
    if subject == "skill.invoke":
        mode = str(det.get("mode") or "").strip().lower()
        via = str(det.get("via") or "").strip().lower()
        if mode in ("auto_invoke", "auto") or (not mode and via in ("auto", "hook")):
            return "auto_use", name
        if mode == "direct_read":
            # 转录采集（`skill_direct`）统一写 mode=direct_read、真实取证在 evidence：
            # 只有真调了 Skill 工具才是「拿走正文去执行」(explicit=2)，
            # 读 SKILL.md / 命令行里出现技能名只是「看了一眼」(read=1)。
            # 不区分会在接入 scan 源后把读盘按 2 分计，系统性抬高榜单。
            ev = str(det.get("evidence") or "").strip().lower()
            return ("explicit" if ev == "skill_tool" else "read"), name
        return "explicit", name
    if subject == "skill.read":
        return "read", name
    if subject == "skill.inject":
        # 只有注入链会产出这个 subject，语义就是「自动下发」⇒ 直接进曝光桶。
        # 仍允许 detail 显式写 mode=query（未来若有人手工回写注入记录），按声明走。
        if str(det.get("mode") or "").strip().lower() == "query":
            return "query", name
        return "auto", name
    if subject == "skill.relevant":
        mode = str(det.get("mode") or "").strip().lower()
        via = str(det.get("via") or "").strip().lower()
        if mode == "auto_inject" or (not mode and via == "hook"):
            return "auto", name
        if mode == "query":
            return "query", name
        return "legacy", name
    return "skip", name


#: 需要按调用方/证据分桶的**使用**桶（auto 曝光是另一个量，见模块 docstring）。
_USE_BUCKETS = ("explicit", "read", "auto_use")
_EVIDENCE_BUCKETS = ("explicit", "read", "auto_use", "auto")
#: 所有会计数的桶（不含 skip）。`legacy` / `query` 不进权重，但必须在 totals 里可见。
_BUCKET_KEYS = ("explicit", "read", "auto_use", "auto", "query", "legacy")


def _new_row(name: str, route: str) -> Dict[str, Any]:
    return {
        "name": name, "route": route,
        "explicit": 0, "read": 0, "auto_use": 0, "auto": 0, "query": 0, "legacy": 0,
        "by_caller": {b: {} for b in _EVIDENCE_BUCKETS},
        "evidence": {b: [] for b in _EVIDENCE_BUCKETS},
        "last_used_at": "",
    }


def _bump(m: Dict[str, int], k: str) -> None:
    if k:
        m[k] = m.get(k, 0) + 1


def _parse_ts(raw: Any) -> Optional[datetime]:
    """解析 `profile_events.created_at`。格式不保证统一（ISO / 空格分隔 / Z 结尾），
    解析不了就返回 None——**不猜时间**，`idle_days` 宁可为 null。"""
    s = str(raw or "").strip()
    if not s:
        return None
    try:
        dt = datetime.fromisoformat(s.replace("Z", "+00:00").replace(" ", "T"))
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)


def _finalize(row: Dict[str, Any], now: Optional[datetime] = None) -> Dict[str, Any]:
    use = (W_EXPLICIT * row["explicit"] + W_READ * row["read"]
           + W_AUTO_USE * row["auto_use"])
    s, conv = score(use, row["auto"])
    row["use"] = use
    row["score"] = s
    row["conversion"] = conv
    for b in _EVIDENCE_BUCKETS:
        row["by_caller"][b] = dict(sorted(row["by_caller"][b].items(), key=lambda kv: (-kv[1], kv[0])))
    ev = row["evidence"]
    # 总数按桶计数（每事件在该技能下只落一行），不按 evidence 列表长度——
    # 后者被 EVIDENCE_CAP 截断，用它当「共几次」会把 200 次说成 20 次。
    row["evidence_total"] = {b: row[b] for b in _EVIDENCE_BUCKETS}
    row["evidence"] = {k: v[:EVIDENCE_CAP] for k, v in ev.items()}
    # 冷热分层：把「没人用」的两种成因分开——从没被用过 vs 被推过但没人打开。
    # 这不是删除依据（STALE_AFTER_DAYS 是可改常量，只作观察）。
    last = _parse_ts(row.get("last_used_at"))
    ref = now or datetime.now(timezone.utc)
    idle = None if last is None else max(0, (ref - last).days)
    row["idle_days"] = idle
    if use > 0:
        row["usage_class"] = "stale" if (idle is not None and idle > STALE_AFTER_DAYS) else "used"
    elif row["auto"] > 0:
        row["usage_class"] = "exposed_only"
    elif row["legacy"] or row["query"]:
        row["usage_class"] = "unknown"
    else:
        row["usage_class"] = "unused"
    return row


def build(rows: Iterable[Dict[str, Any]], only_caller: str = "",
          now: Optional[datetime] = None, only_route: str = "") -> Dict[str, Any]:
    """把 `profile_events` 行折成 `(name, route)` 维度的权重表。**纯函数，不读库**。

    `only_caller` / `only_route` 非空时只统计这些事件（**先过滤再算分**）：
    「codex 的常用技能榜」「techdocs 这一路的榜」与「全机榜」是三个问题，
    不能靠对全机榜单做后过滤得到（分母会带上别人的曝光，且 `totals` 会留在全机口径上
    ——2026-10-20 实测：`?route=techdocs` 只筛了 `items`，顶部汇总仍报全机
    「点名 20」，同一个屏幕里两份真相）。
    """
    want = (only_caller or "").strip().lower()
    want_route = (only_route or "").strip()
    per: Dict[Tuple[str, str], Dict[str, Any]] = {}
    counts = {"rows": 0, "bad_detail": 0, "no_name": 0, "filtered_out": 0,
              "explicit": 0, "read": 0, "auto_use": 0, "auto": 0,
              "query": 0, "legacy": 0, "skip": 0}
    # 逐桶拆「进了榜单的」与「没有技能名的」。只报 no_name 总数不够：读的人想知道
    # 「点名 26 而榜里只有 24」差的是哪 2 条，汇总一个 421 解释不了任何一桶。
    named_by_bucket = {b: 0 for b in _BUCKET_KEYS}
    unnamed_by_bucket = {b: 0 for b in _BUCKET_KEYS}
    for r in rows or []:
        det = _parse_detail(r.get("detail"))
        if det is None:
            counts["bad_detail"] += 1
            continue
        subject = str(r.get("subject") or "")
        bucket, name = classify(subject, det)
        if bucket == "skip":
            counts["skip"] += 1
            continue
        caller = str(det.get("caller") or "").strip().lower()
        if want and caller != want:
            counts["filtered_out"] += 1
            continue
        if want_route and str(det.get("route") or "").strip() != want_route:
            counts["filtered_out"] += 1
            continue
        counts[bucket] = counts.get(bucket, 0) + 1
        if not name:
            counts["no_name"] += 1
            unnamed_by_bucket[bucket] += 1
            continue
        named_by_bucket[bucket] += 1
        counts["rows"] += 1
        route = str(det.get("route") or "").strip()
        row = per.setdefault((name, route), _new_row(name, route))
        ev_id = r.get("id")
        ts = str(r.get("created_at") or "")
        if bucket in _EVIDENCE_BUCKETS:
            # 同一事件按名逐行落库（runlog 的既有形状），id 可重复指向同一次调用；
            # evidence 保留首次出现的顺序即可，计数在 *_total 里如实反映。
            if ev_id is not None and len(row["evidence"][bucket]) < EVIDENCE_CAP * 5:
                row["evidence"][bucket].append(ev_id)
            _bump(row["by_caller"][bucket], caller)
            if bucket in _USE_BUCKETS and ts > row["last_used_at"]:
                row["last_used_at"] = ts
        row[bucket] += 1

    items = [_finalize(dict(v, by_caller={b: dict(v["by_caller"][b])
                                          for b in _EVIDENCE_BUCKETS},
                            evidence={k: list(v) for k, v in v["evidence"].items()}), now=now)
             for v in per.values()]
    items.sort(key=lambda x: (-x["score"], -x["use"], -x["auto"], x["name"], x["route"]))

    by_name: Dict[str, Dict[str, Any]] = {}
    for it in items:
        n = by_name.setdefault(it["name"], {
            "name": it["name"], "routes": [], "explicit": 0, "read": 0,
            "auto_use": 0, "auto": 0, "query": 0, "legacy": 0,
            "by_caller": {b: {} for b in _EVIDENCE_BUCKETS},
            "evidence": {b: [] for b in _EVIDENCE_BUCKETS}, "last_used_at": "",
        })
        if it["route"] and it["route"] not in n["routes"]:
            n["routes"].append(it["route"])
        for k in ("explicit", "read", "auto_use", "auto", "query", "legacy"):
            n[k] += it[k]
        for b in _EVIDENCE_BUCKETS:
            for c, v in it["by_caller"][b].items():
                n["by_caller"][b][c] = n["by_caller"][b].get(c, 0) + v
            if len(n["evidence"][b]) < EVIDENCE_CAP:
                n["evidence"][b].extend(it["evidence"][b][:EVIDENCE_CAP - len(n["evidence"][b])])
        if it["last_used_at"] > n["last_used_at"]:
            n["last_used_at"] = it["last_used_at"]
    names = [_finalize(n, now=now) for n in by_name.values()]
    names.sort(key=lambda x: (-x["score"], -x["use"], x["name"]))

    # `rows` 只数「带技能名、能进榜单」的事件，而各 bucket 数的是全部事件
    # （含无名的注入回包）⇒ 两者相加必然对不上。把口径显式写进 totals，
    # 让「为什么 654 行却有 666 个 bucket 计数」在响应里自答，不靠读代码。
    counts["events"] = counts["explicit"] + counts["read"] + counts["auto_use"] \
        + counts["auto"] + counts["query"] + counts["legacy"]
    counts["named"] = counts["rows"]
    # 逐桶对账（2026-10-20 实测：点名 26 而榜里只有 24、legacy 1276 而榜里只有 858）：
    # 任何一桶都必须满足 totals[b] == named_by_bucket[b] + unnamed_by_bucket[b]，
    # 读的人不必再猜「差的是无名事件还是被 limit 截掉」。
    counts["named_by_bucket"] = dict(named_by_bucket)
    counts["unnamed_by_bucket"] = dict(unnamed_by_bucket)
    return {
        "items": items,
        "by_name": names,
        "totals": counts,
    }


def load(days: int = 30, route: str = "", caller: str = "",
         limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """读库 + 聚合。**读不到就抛**——与 `/invocations` 同口径：查询失败不许装成空榜单。

    空榜单的语义是「这个窗口内确实没人用」；把 DB 故障也渲染成空榜，用户会去删技能。
    """
    d = _clamp_days(days)
    since = (datetime.now(timezone.utc) - timedelta(days=d)).isoformat()
    holders = ",".join("?" * len(SUBJECTS))
    src_holders = ",".join("?" * len(SOURCES))
    rows = db.query(
        "SELECT id, subject, detail, created_at FROM profile_events "
        "WHERE source IN (%s) AND subject IN (%s) AND created_at >= ? ORDER BY id DESC"
        % (src_holders, holders),
        SOURCES + SUBJECTS + (since,))
    now = datetime.now(timezone.utc)
    # route 与 caller 同口径：**先过滤再统计**。原先 route 是在 build 之后筛 items，
    # 于是列表是这一路的、顶部 totals 是全机的（同一屏两份真相）。
    rep = build(rows, only_caller=caller, now=now, only_route=route)
    lim = max(1, min(MAX_LIMIT, int(limit)))
    all_items = rep["items"]
    rep["items"] = all_items[:lim]
    rep["by_name"] = rep["by_name"][:lim]
    # 分页也是「同一屏两份真相」的一个来源：`totals` 是聚合前的全量，`items` 是
    # limit 后的子集。2026-10-20 实测 limit=100 时 legacy 汇总 1276 而列表只有 630
    # （其中 418 是无名事件、228 是被 limit 截掉的行）。两者差别必须能在响应里
    # 各自归因，否则读的人只能二选一：信汇总（看不见的 228 行）或信列表（少算）。
    tot = rep["totals"]
    tot["listed_items"] = len(rep["items"])
    tot["dropped_by_limit"] = len(all_items) - len(rep["items"])
    tot["listed_by_bucket"] = {
        b: sum(int(it.get(b) or 0) for it in rep["items"]) for b in _BUCKET_KEYS}
    rep.update({
        "days": d,
        "identity": "(name, route)",
        "filters": {"route": route.strip() or None, "caller": (caller or "").strip().lower() or None},
        "weights": {"explicit": W_EXPLICIT, "read": W_READ, "auto_use": W_AUTO_USE,
                    "auto": "denominator-only"},
        "formula": "use = 2*explicit + read + auto_use; score = use^2 / (use + auto)",
        "scope": "两源合并：hub 通道（source=rest）+ 会话转录采集（source=scan）；"
                 "agent 直读磁盘的写盘与纯提及仍不计 ⇒ 置信度封顶 medium",
        "legacy_note": "mode/via 缺失的 skill.relevant 记 legacy，不进 use 也不进 auto",
        "stale_after_days": STALE_AFTER_DAYS,
        "usage_classes": {
            "used": "窗口内有 explicit/read/auto_use，且最近一次使用在 stale_after_days 内",
            "stale": "有使用记录，但最近一次已超过 stale_after_days（只作观察，不是删除依据）",
            "exposed_only": "只被自动推过、从未被点名或阅读 ⇒ score=0",
            "unknown": "只有 legacy/query 事件，无法判定用途",
        },
        "totals_note": "events=各桶事件之和（含无技能名的注入回包）；named=能进榜单的事件数；"
                       "对账三式：totals[b] = named_by_bucket[b] + unnamed_by_bucket[b]；"
                       "listed_by_bucket 是本次 limit 后真正返回的行；"
                       "dropped_by_limit>0 时 named_by_bucket[b] - listed_by_bucket[b] 就是被分页拿掉的行；"
                       "两者不等是口径差异，不是丢数据",
    })
    return rep
