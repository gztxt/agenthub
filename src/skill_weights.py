"""技能权重（PT-20261020-01）：把「曝光」与「使用」分成两个量，再合成一个可分桶追溯的分数。

【为什么不能直接数调用次数】
`skill.relevant` 是注入链的留痕：agent 每次输入都会拿到 top-3 候选，**没人点名任何技能**。
把它的次数与 `skill.invoke` 相加，等于把「被推给 agent」当成「agent 用了」——
全机最热会变成「每轮都被推但没人读」的那几个，与「常用技能」的语义正相反。

【公式】
    使用量 use = 2×explicit + 1×read
    曝光量 auto = skill.relevant 中 mode=auto_inject 的条数
    score = use² / (use + auto)      （use+auto=0 时为 0）

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

#: 与 `skill_usage._SOURCE` 同义：hub 自己产生的门面事件。
SOURCE = "rest"

#: 计入权重的 subject。`skill.list` 是浏览目录，不是使用；`skill.inject` 目前无生产者。
SUBJECTS = ("skill.read", "skill.invoke", "skill.relevant")

#: 使用强度。invoke 拿走指令正文（要执行），read 只是查看。
W_EXPLICIT = 2
W_READ = 1

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
    """把一个事件归到 `(bucket, name)`。bucket ∈ explicit/read/auto/query/legacy/skip。

    判据只有一条：**auto 必须由调用方显式声明**（`mode=auto_inject` 或旧写法 `via=hook`）。
    缺声明时宁可归 legacy，也不猜——猜错的代价是权重被系统性抬高。
    """
    name = str(det.get("name") or det.get("first_name") or "").strip()
    if subject == "skill.invoke":
        return "explicit", name
    if subject == "skill.read":
        return "read", name
    if subject == "skill.relevant":
        mode = str(det.get("mode") or "").strip().lower()
        via = str(det.get("via") or "").strip().lower()
        if mode == "auto_inject" or (not mode and via == "hook"):
            return "auto", name
        if mode == "query":
            return "query", name
        return "legacy", name
    return "skip", name


def _new_row(name: str, route: str) -> Dict[str, Any]:
    return {
        "name": name, "route": route,
        "explicit": 0, "read": 0, "auto": 0, "query": 0, "legacy": 0,
        "by_caller": {"explicit": {}, "read": {}, "auto": {}},
        "evidence": {"explicit": [], "read": [], "auto": []},
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
    use = W_EXPLICIT * row["explicit"] + W_READ * row["read"]
    s, conv = score(use, row["auto"])
    row["use"] = use
    row["score"] = s
    row["conversion"] = conv
    for b in ("explicit", "read", "auto"):
        row["by_caller"][b] = dict(sorted(row["by_caller"][b].items(), key=lambda kv: (-kv[1], kv[0])))
    ev = row["evidence"]
    # 总数按桶计数（每事件在该技能下只落一行），不按 evidence 列表长度——
    # 后者被 EVIDENCE_CAP 截断，用它当「共几次」会把 200 次说成 20 次。
    row["evidence_total"] = {"explicit": row["explicit"], "read": row["read"], "auto": row["auto"]}
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
          now: Optional[datetime] = None) -> Dict[str, Any]:
    """把 `profile_events` 行折成 `(name, route)` 维度的权重表。**纯函数，不读库**。

    `only_caller` 非空时只统计该调用方的事件（**先过滤再算分**）：
    「codex 的常用技能榜」与「全机常用技能榜」是两个问题，不能靠对全机榜单做后过滤得到
    （分母会带上别人的曝光）。
    """
    want = (only_caller or "").strip().lower()
    per: Dict[Tuple[str, str], Dict[str, Any]] = {}
    counts = {"rows": 0, "bad_detail": 0, "no_name": 0, "filtered_out": 0,
              "explicit": 0, "read": 0, "auto": 0, "query": 0, "legacy": 0, "skip": 0}
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
        counts[bucket] = counts.get(bucket, 0) + 1
        if not name:
            counts["no_name"] += 1
            continue
        counts["rows"] += 1
        route = str(det.get("route") or "").strip()
        row = per.setdefault((name, route), _new_row(name, route))
        ev_id = r.get("id")
        ts = str(r.get("created_at") or "")
        if bucket in ("explicit", "read", "auto"):
            # 同一事件按名逐行落库（runlog 的既有形状），id 可重复指向同一次调用；
            # evidence 保留首次出现的顺序即可，计数在 *_total 里如实反映。
            if ev_id is not None and len(row["evidence"][bucket]) < EVIDENCE_CAP * 5:
                row["evidence"][bucket].append(ev_id)
            _bump(row["by_caller"][bucket], caller)
            if bucket in ("explicit", "read") and ts > row["last_used_at"]:
                row["last_used_at"] = ts
        row[bucket] += 1

    items = [_finalize(dict(v, by_caller={"explicit": dict(v["by_caller"]["explicit"]),
                                          "read": dict(v["by_caller"]["read"]),
                                          "auto": dict(v["by_caller"]["auto"])},
                            evidence={k: list(v) for k, v in v["evidence"].items()}), now=now)
             for v in per.values()]
    items.sort(key=lambda x: (-x["score"], -x["use"], -x["auto"], x["name"], x["route"]))

    by_name: Dict[str, Dict[str, Any]] = {}
    for it in items:
        n = by_name.setdefault(it["name"], {
            "name": it["name"], "routes": [], "explicit": 0, "read": 0,
            "auto": 0, "query": 0, "legacy": 0, "by_caller": {"explicit": {}, "read": {}, "auto": {}},
            "evidence": {"explicit": [], "read": [], "auto": []}, "last_used_at": "",
        })
        if it["route"] and it["route"] not in n["routes"]:
            n["routes"].append(it["route"])
        for k in ("explicit", "read", "auto", "query", "legacy"):
            n[k] += it[k]
        for b in ("explicit", "read", "auto"):
            for c, v in it["by_caller"][b].items():
                n["by_caller"][b][c] = n["by_caller"][b].get(c, 0) + v
        for b in ("explicit", "read", "auto"):
            if len(n["evidence"][b]) < EVIDENCE_CAP:
                n["evidence"][b].extend(it["evidence"][b][:EVIDENCE_CAP - len(n["evidence"][b])])
        if it["last_used_at"] > n["last_used_at"]:
            n["last_used_at"] = it["last_used_at"]
    names = [_finalize(n, now=now) for n in by_name.values()]
    names.sort(key=lambda x: (-x["score"], -x["use"], x["name"]))

    # `rows` 只数「带技能名、能进榜单」的事件，而各 bucket 数的是全部事件
    # （含无名的注入回包）⇒ 两者相加必然对不上。把口径显式写进 totals，
    # 让「为什么 654 行却有 666 个 bucket 计数」在响应里自答，不靠读代码。
    counts["events"] = counts["explicit"] + counts["read"] + counts["auto"] \
        + counts["query"] + counts["legacy"]
    counts["named"] = counts["rows"]
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
    rows = db.query(
        "SELECT id, subject, detail, created_at FROM profile_events "
        "WHERE source = ? AND subject IN (%s) AND created_at >= ? ORDER BY id DESC" % holders,
        (SOURCE,) + SUBJECTS + (since,))
    now = datetime.now(timezone.utc)
    rep = build(rows, only_caller=caller, now=now)
    if route.strip():
        want = route.strip()
        rep["items"] = [x for x in rep["items"] if x["route"] == want]
        rep["by_name"] = [x for x in rep["by_name"] if want in x["routes"]]
    lim = max(1, min(MAX_LIMIT, int(limit)))
    rep["items"] = rep["items"][:lim]
    rep["by_name"] = rep["by_name"][:lim]
    rep.update({
        "days": d,
        "identity": "(name, route)",
        "filters": {"route": route.strip() or None, "caller": (caller or "").strip().lower() or None},
        "weights": {"explicit": W_EXPLICIT, "read": W_READ, "auto": "denominator-only"},
        "formula": "use = 2*explicit + read; score = use^2 / (use + auto)",
        "scope": "仅 hub 通道（source=rest）；agent 直读磁盘的旁路不计入 ⇒ 置信度封顶 medium",
        "legacy_note": "mode/via 缺失的 skill.relevant 记 legacy，不进 use 也不进 auto",
        "stale_after_days": STALE_AFTER_DAYS,
        "usage_classes": {
            "used": "窗口内有 explicit/read，且最近一次使用在 stale_after_days 内",
            "stale": "有使用记录，但最近一次已超过 stale_after_days（只作观察，不是删除依据）",
            "exposed_only": "只被自动推过、从未被点名或阅读 ⇒ score=0",
            "unknown": "只有 legacy/query 事件，无法判定用途",
        },
        "totals_note": "events=各桶事件之和（含无技能名的注入回包）；named=能进榜单的事件数；"
                       "两者不等是口径差异，不是丢数据",
    })
    return rep
