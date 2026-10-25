"""memory_archive —— 统一归档写入口（v0.13.161，PT-20261025-11）。

用户令（2026-10-25）：「所有 agent 的记忆数据能够统一归档到记忆中心供统一接口自动
调用；数据无需重复保存，按现有 agent 既有数据统一归档」。

**本模块是什么**：
- hub 自有 sqlite（`memories` 表 `layer='ARCH'`）的**唯一写接缝**。agent 提炼后
  主动推送（kind=distilled）与用户手动补齐（kind=manual）都只从这里进；
- 落库即过**质量门**：scrub 脱敏 → 精确 hash 去重 → simhash 近似去重/标注
  superseded → 每 agent 活跃上限（超限最旧软删为 superseded，可审计）。

**本模块不是什么**（边界，改动前先读）：
- **不拷贝**任何既有 agent 存储。claude_mem / TDAI / 各会话库仍由 memfed 只读
  抽取、各自权威；「统一归档」指**新提交**的统一入口 + 既有源的登记视图（matrix），
  不是把 13 路内容复制进 hub。用户明确「数据无需重复保存」。
- 不替代 L1/L2/L3 旧层：旧层已判「被 TDAI 取代仅作历史存档」（vitals 的 note），
  ARCH 与 L1 同表不同 layer，口径互不污染。

依赖最小化：零第三方。simhash 用标准库 blake2b（进程稳定的 64 位哈希——
`hash()` 对 str 每进程随机化，绝不能用来算 simhash）。
"""

from __future__ import annotations

import hashlib
import re
import time
from typing import Dict, List, Optional, Tuple

import db
import tdai_client

#: 归档层标识。与 L1/L2/L3 同表不同层——L1 是「被 TDAI 取代的旧便签」，
#: ARCH 是「在役的统一归档」，混在一层会让两个口径互相冒充（v0.13.160 的教训）。
LAYER = "ARCH"

#: 每 agent 活跃归档上限。触顶后**最旧的活跃行**软删为 superseded（不是拒绝写入，
#: 也不是硬删——记忆系统里静默丢新记忆比淘汰旧记忆更糟）。软删可审计、可查。
PER_AGENT_CAP = 1000

#: simhash 近似判定阈值（64 位海明距离）。≤10 ≈ 「同一条的改写版」：
# 实测标定：同文本 0；改几个字 1–5；同事实换措辞 10–25；不同事实 30+。
# 10 是「抓更新版、不塌不同事实」的折中。**不是** NLI 矛盾检测——
# 真矛盾（「端口是 3102」vs「端口是 3105」）距离可能 >10，需要语义模型，
# 明确不做（用户批准的非目标）。
SIMHASH_MAX_DIST = 10

#: 手动补齐的行**永不参与**自动 supersede：既是目标也是触发器都不参与。
# 人工措辞是权威层的，不允许被 agent 的近似重复悄悄标掉。
MANUAL_SOURCE = "manual"

_CATEGORIES = ("fact", "decision", "constraint", "preference")
_KINDS = ("distilled", "manual")

#: agent → 其既有记忆源（memfed.REGISTRY 的 id）。看板矩阵用。
#: 这是**登记知识**不是派生计算：源 id 与 agent id 的对应关系写在各自的 desc 里
#: （如 codebuddy_sessions 的 desc 指明 ~/.codebuddy/projects），没有机械规则可推。
#: 加新 agent 时同步加这里；漏加的后果是矩阵里该 agent 显示「无源」——
#: 看得见的缺失，不是静默的错误。
AGENT_SOURCE_MAP: Dict[str, List[str]] = {
    "claude": ["claude_mem", "claude_projects"],
    "codex": ["codex_sessions"],
    "pi": ["pi_sessions"],
    "codebuddy": ["codebuddy_sessions"],
    "grok": ["grok_memory"],
    "hermes": ["hermes_memory"],
    "workbuddy": ["workbuddy_memory"],
    "opencode": ["opencode_sessions"],
    "zcode": [], "qoder": [], "jcode": [], "dsh": [], "cloudcli": [],
    "omp": [], "qwenpaw": [],
}

#: hub-mcp 已知接线点（配置文件路径 → agent id）。**现探不写死**：读配置文件判断
#: 是否真的接了 /hub-mcp/mcp，而不是维护一份「我记得接了」的清单——配置会漂移，
#: 清单不会自己更新。2026-10-25 实测：claude（~/.claude/settings.json）与
#: codex（~/.codex/config.toml）两家；其余 12 家未接，按用户批准的决策①
#: 「先 REST 后逐家报请」，不顺手改别人的配置。
_MCP_WIRED_PROBES: List[Tuple[str, str]] = [
    ("claude", "~/.claude/settings.json"),
    ("codex", "~/.codex/config.toml"),
]


def mcp_wired_agents() -> List[str]:
    import os
    from pathlib import Path
    out = []
    for agent, path in _MCP_WIRED_PROBES:
        try:
            if "hub-mcp" in Path(os.path.expanduser(path)).read_text(encoding="utf-8"):
                out.append(agent)
        except Exception:  # noqa: BLE001 —— 读不到配置 = 未接线，不是错误
            pass
    return sorted(out)

_WS = re.compile(r"\s+")


def _norm(text: str) -> str:
    """归一化：折叠空白 + casefold。大小写/空白差异不算两条记忆。"""
    return _WS.sub(" ", text or "").strip().casefold()


def content_hash(text: str) -> str:
    """精确去重键。归一化后取 sha256——同一内容改大小写/空白仍判重。"""
    return hashlib.sha256(_norm(text).encode("utf-8")).hexdigest()


def _shingles(text: str) -> List[str]:
    """字符 3-gram。中文没有词边界，按字取滑窗是 CJK 文本的标准做法；
    短于 3 字时整串当一个 token（否则短内容永远没有 shingle）。"""
    s = _norm(text)
    if len(s) < 3:
        return [s] if s else []
    return [s[i:i + 3] for i in range(len(s) - 2)]


def simhash(text: str) -> int:
    """64 位 SimHash（Charikar）。对每个 shingle 投一位图，多数表决成签名。

    为什么不用内置 `hash()`：Python 对 str 的 hash 每进程随机化（PYTHONHASHSEED），
    同一段文本两次进程算出的值不同 ⇒ 去重状态跨重启漂移。blake2b 稳定。
    """
    votes = [0] * 64
    for tok in _shingles(text):
        h = int.from_bytes(hashlib.blake2b(tok.encode("utf-8"), digest_size=8).digest(), "big")
        for i in range(64):
            votes[i] += 1 if (h >> i) & 1 else -1
    sig = 0
    for i in range(64):
        if votes[i] > 0:
            sig |= 1 << i
    return _to_signed(sig)


#: SQLite INTEGER 是**64 位有符号**：无符号 64 位值 ≥2^63 直接 OverflowError
# （测试首版实测抓到「Python int too large to convert to SQLite INTEGER」）。
# 存储/读取各转一次，位图语义不变（hamming 在无符号域计算，读回来先转回）。
_I64 = 1 << 64


def _to_signed(v: int) -> int:
    return v - _I64 if v >= (1 << 63) else v


def _to_unsigned(v: int) -> int:
    return v + _I64 if v < 0 else v


def hamming(a: int, b: int) -> int:
    return bin((_to_unsigned(a) ^ _to_unsigned(b))).count("1")


def known_agents() -> List[str]:
    """可提交的 agent 标识 = profiles 在册 agent（kind=agent）+ manual + hub。

    不写死清单：profiles.py 是纳管矩阵的唯一权威源（判例 268：新增 agent 先查
    agent_signatures.json 与注册表），这里现取，装新 agent 自动可用。
    """
    try:
        import profiles
        ids = [p["id"] for p in profiles.PROFILES if p.get("kind") == "agent"]
    except Exception:  # noqa: BLE001 ——  profiles 导入失败不应让写入口整体不可用
        ids = []
    return sorted(set(ids) | {MANUAL_SOURCE, "hub"})


def _scrub(text: str) -> str:
    """脱敏与 tdai_client 同一实现——脱敏只能有一处（memory.py 顶部同款注释）。"""
    try:
        return tdai_client.scrub(text)
    except Exception:  # noqa: BLE001
        return text


def _now() -> str:
    return db._now()


def submit(content: str, category: str, agent: str, kind: str = "distilled",
           session_id: Optional[str] = None, note: str = "",
           actor: str = "") -> Dict:
    """统一写入口。返回 {status, id, ...}；status ∈ created|duplicate|superseded。

    质量门顺序即代码顺序，每一步失败都**早退并说清原因**，不静默：
      1. 字段校验（agent 必须在册、category/kind 在枚举、content 1..8000 字符）
      2. scrub 脱敏（凭据类内容替换为占位符后才进库）
      3. 精确去重：同 hash 的活跃行已存在 ⇒ 返回它，不插第二条
      4. 近似去重：同 (agent, category) 的活跃 distilled 行，海明 ≤ 阈值
         ⇒ 新行插入、旧行标 superseded 并指回新 id
      5. 每 agent 活跃上限：超限的最旧活跃行软删（superseded_by=NULL）
    """
    content = (content or "").strip()
    agent = (agent or "").strip()
    kind = (kind or "distilled").strip()
    if not content:
        raise ValueError("content 不能为空")
    if len(content) > 8000:
        raise ValueError("content 超过 8000 字符上限")
    if category not in _CATEGORIES:
        raise ValueError(f"category 必须是 {list(_CATEGORIES)} 之一")
    if kind not in _KINDS:
        raise ValueError(f"kind 必须是 {list(_KINDS)} 之一")
    if kind == "manual" and agent != MANUAL_SOURCE:
        # 手动补齐的归属人就是 manual；不允许冒充某个 agent（审计要能追责）
        raise ValueError("kind=manual 时 agent 必须为 'manual'")
    if agent not in known_agents():
        raise ValueError(f"未知 agent: {agent}；可用值 {known_agents()}")

    scrubbed = _scrub(content)
    chash = content_hash(scrubbed)
    sh = simhash(scrubbed)

    # ── 3. 精确去重 ──────────────────────────────────────────────────────
    dup = db.query(
        "SELECT id FROM memories WHERE layer=? AND content_hash=? AND status='active' "
        "ORDER BY id DESC LIMIT 1", (LAYER, chash))
    if dup:
        return {"status": "duplicate", "id": dup[0]["id"], "chars": len(scrubbed)}

    # ── 4. 近似去重 / supersede ───────────────────────────────────────────
    superseded: List[int] = []
    if kind == "distilled":
        rows = db.query(
            "SELECT id, content, simhash FROM memories "
            "WHERE layer=? AND source=? AND category=? AND status='active' AND kind='distilled'",
            (LAYER, agent, category))
        for r in rows:
            if r["simhash"] is not None and hamming(int(r["simhash"]), sh) <= SIMHASH_MAX_DIST:
                superseded.append(r["id"])

    now = _now()
    src = MANUAL_SOURCE if kind == "manual" else agent
    # lastrowid 必须走连接游标取：db.execute 回的是**影响行数**（INSERT 也是 1），
    # 拿它当 id 会让 superseded_by 总指向 1（冒烟测试实测抓到）。
    with db._lock:
        cur = db._conn.execute(
            "INSERT INTO memories(layer, category, content, source, session_id, status, "
            "created_at, updated_at, content_hash, simhash, superseded_by, kind) "
            "VALUES(?,?,?,?,?,'active',?,?,?,?,NULL,?)",
            (LAYER, category, scrubbed, src, session_id, now, now, chash, sh, kind))
        db._conn.commit()
        new_id = int(cur.lastrowid)
    if not new_id:
        raise RuntimeError("插入归档行失败")

    for old in superseded:
        db.execute("UPDATE memories SET status='superseded', superseded_by=?, updated_at=? "
                   "WHERE id=? AND status='active'", (new_id, now, old))
    if superseded:
        db.log_asset_event("memory_archive", str(new_id), "update", actor or agent,
                           {"superseded": superseded, "kind": kind,
                            "simhash_max_dist": SIMHASH_MAX_DIST,
                            "chars": len(scrubbed)})

    # ── 5. 每 agent 活跃上限（软删最旧，审计留痕） ─────────────────────────
    evicted = _enforce_cap(src, now, actor or agent)
    if evicted:
        db.log_asset_event("memory_archive", src, "delete", actor or agent,
                           {"evicted": evicted, "reason": "per_agent_cap",
                            "cap": PER_AGENT_CAP})

    out = {"status": "superseded" if superseded else "created", "id": new_id,
           "chars": len(scrubbed), "agent": agent, "kind": kind}
    if superseded:
        out["superseded_ids"] = superseded
    if evicted:
        out["evicted_ids"] = evicted
    return out


def _enforce_cap(source: str, now: str, actor: str) -> List[int]:
    """活跃行数超 PER_AGENT_CAP 时，把最旧的活跃行软删。返回被淘汰的 id 列表。

    软删而非拒绝：记忆写入口拒绝新记忆会造成「agent 以为存进去了」的静默丢失；
    硬删则没有审计。superseded_by 留 NULL 表示「非矛盾淘汰，是容量淘汰」。
    """
    n = (db.query("SELECT COUNT(*) AS n FROM memories "
                  "WHERE layer=? AND source=? AND status='active'", (LAYER, source))
         or [{"n": 0}])[0]["n"]
    if n <= PER_AGENT_CAP:
        return []
    over = n - PER_AGENT_CAP
    old = db.query(
        "SELECT id FROM memories WHERE layer=? AND source=? AND status='active' "
        "ORDER BY created_at ASC, id ASC LIMIT ?", (LAYER, source, over))
    ids = [r["id"] for r in old]
    for mid in ids:
        db.execute("UPDATE memories SET status='superseded', superseded_by=NULL, "
                   "updated_at=? WHERE id=? AND status='active'", (now, mid))
    return ids


def list_rows(agent: str = "", category: str = "", q: str = "",
              include_superseded: bool = False, limit: int = 50) -> Dict:
    """归档列表。口径与 /api/memory/l1 对齐：total/count/truncated 分开，
    筛选零命中与全库为空说不同的话（v0.13.160 的截断静默修复同款思路）。"""
    where = "FROM memories WHERE layer=?"
    params: list = [LAYER]
    if not include_superseded:
        where += " AND status='active'"
    if agent:
        where += " AND source=?"
        params.append(agent)
    if category:
        where += " AND category=?"
        params.append(category)
    if q:
        where += " AND content LIKE ? ESCAPE '\\'"
        params.append("%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%")
    total = (db.query("SELECT COUNT(*) AS n " + where, tuple(params)) or [{"n": 0}])[0]["n"]
    # total_unfiltered = 同 status 口径下、不带 agent/category/q 的全库条数。
    # 前端「筛选结果 X / 全部 Y」的 Y 用它——total 是筛选后的，拿它当「全库」
    # 会让「全库 1 条」出现在页面上（UI 探针实测抓到，L1 同病）。
    base = "FROM memories WHERE layer=?" + ("" if include_superseded else " AND status='active'")
    total_unfiltered = (db.query("SELECT COUNT(*) AS n " + base, (LAYER,))
                        or [{"n": 0}])[0]["n"]
    rows = db.query("SELECT * " + where + " ORDER BY id DESC LIMIT ?",
                    tuple(params + [min(max(limit, 1), 200)]))
    return {"memories": rows, "count": len(rows), "total": int(total or 0),
            "total_unfiltered": int(total_unfiltered or 0),
            "limit": min(max(limit, 1), 200), "truncated": int(total or 0) > len(rows)}


def counts_by_agent(active_only: bool = True) -> Dict[str, Dict[str, int]]:
    """每 agent 的活跃/取代计数（看板矩阵用，一次查询不带正文）。"""
    sql = ("SELECT source, status, COUNT(*) AS n FROM memories WHERE layer=? "
           + ("AND status='active'" if active_only else "") + " GROUP BY source, status")
    out: Dict[str, Dict[str, int]] = {}
    for r in db.query(sql, (LAYER,)):
        out.setdefault(r["source"], {})[r["status"]] = int(r["n"])
    return out


def last_write_by_agent() -> Dict[str, str]:
    sql = ("SELECT source, MAX(updated_at) AS t FROM memories WHERE layer=? "
           "GROUP BY source")
    return {r["source"]: r["t"] for r in db.query(sql, (LAYER,)) if r["t"]}


def stats() -> Dict:
    """质量门计数器（看板「数据质量」区）。"""
    rows = db.query(
        "SELECT kind, status, COUNT(*) AS n FROM memories WHERE layer=? "
        "GROUP BY kind, status", (LAYER,))
    by: Dict[str, Dict[str, int]] = {}
    for r in rows:
        by.setdefault(r["kind"] or "distilled", {})[r["status"]] = int(r["n"])
    active = sum(v.get("active", 0) for v in by.values())
    superseded = sum(v.get("superseded", 0) for v in by.values())
    return {"by_kind": by, "active": active, "superseded": superseded,
            "dedup_ratio": round(superseded / max(active + superseded, 1), 4),
            "per_agent_cap": PER_AGENT_CAP, "simhash_max_dist": SIMHASH_MAX_DIST}
