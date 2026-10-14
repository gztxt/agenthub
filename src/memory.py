"""记忆中心（Agent_Manager 三层记忆模型 L1-L3 的轻量移植）

- L1 可检索记忆：结构化条目（fact/decision/constraint/preference），语义检索按需召回
- L2 近30天工作记忆：整文档；content 由 L1 压缩生成（LLM 可用时），manual 用户手写独立保存
- L3 长期 Profile：整文档，人工确认制——与上游一致：只有用户能删改，自动流程不碰 manual 区
- 注入通道：GET /api/memory/context 产出「开局上下文包」（L3+L2+相关 L1）

【v0.13.16 主权收口】记忆的**权威副本在 TDAI（:8420，实测 L1 233 / L0 5967 / L3 persona）**，
不在本模块的 `memories` 表（实测只有 4 行 2026-09-06 的陈旧便签）。因此检索与注入
一律**并联**本地与 TDAI 后 RRF 融合，**不得再写「本地无命中才查权威库」的短路**。
旧实现那一条把路径/方法/两个鉴权头写错、又用 `except: pass` 吞掉，对外只报
`{count:0, engine:"keyword"}` 的 HTTP 200 —— 属「全指标绿而功能层已死」。详见 tdai_client.py 顶部。
"""
import asyncio
import json
import os
import time
from datetime import datetime, timezone, timedelta
from typing import Dict, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import db
import llm
import memfed
import runlog
import tdai_client
import writeauth

router = APIRouter()

CATEGORIES = {"fact", "decision", "constraint", "preference"}

#: RRF 常数。取文献惯例 60：名次差异在前几名被拉开、长尾被压平，
#: 且**不依赖各路原始分**（本地 LIKE 无分、TDAI 是余弦，量纲不可比）。
RRF_K = 60
#: 开局注入包里的 TDAI 调用预算。它坐在会话起始链路上，超了就必须放弃该路，
#: 不能把对话卡在记忆检索后面（v2 设计里唯一被完整采纳的一条约束）。
CONTEXT_TIMEOUT_S = float(os.getenv("MEMORY_CONTEXT_TIMEOUT", "1.2"))
#: 权威 persona 很長（实测数千字），给注入包留固定占比，不要把 L2/L1 挤光。
PROFILE_CHARS = int(os.getenv("MEMORY_PROFILE_CHARS", "1200"))
#: v0.13.65 D1.5：**检索端**默认开启的快路联邦集。取值不是猜的——2026-10-02 实测
#: `/api/memory/search?sources=<全9路>` 单轮并联墙钟 3.2s，逐路 ms：
#: workbuddy 47 / claude_mem 148 / claude_projects 261 / grok 593 / workspace 583
#: / hermes 1961 / pi_sessions 1894(超时) / codex_sessions 2028(超时)
#: / archived_sessions 2506(超时，要扫 5622 个文件)。
#: 为何不含后三路：默认行径上烧 3s 且三路必然超时——它们等 A4 的索引投影，不在这里硬开。
#: 为何 claude_mem 在列：9 路里**只有它有真索引**（sqlite FTS5），其余是每次全树 rg。
FED_FAST_SOURCES = "local,tdai,claude_mem,workbuddy_memory,claude_projects"
#: 注入段里单条记忆的截断长度（与 pi 扩展 fmtMem 的 160 字同口径，跨入口看起来一致）。
FED_ITEM_CHARS = 160


def _local_l1(q: str, limit: int) -> list:
    # `layer='L1'` 不是多余过滤：`list_l1` 一直按 layer='L1' 取数，而检索路此前把全表混入
    # 融合，两个端点口径不一致（同一个库给两个答案）。09-24 独立复核发现。
    # LIKE 的通配符 % / _ 未转义时，用户查 "50%" 会匹配全表、查 "a_b" 会命中 "axb"
    # ⇒ 检索结果与查询词无关。转义 + 显式 ESCAPE 是唯一正解（加 \ 本身也需转义）。
    pat = "%" + q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"
    rows = db.query(
        "SELECT * FROM memories WHERE status='active' AND layer='L1' "
        "AND content LIKE ? ESCAPE '\\' "
        "ORDER BY updated_at DESC LIMIT ?", (pat, limit))
    for r in rows:
        r["source"] = "local"
    return rows


# `sources` 只认白名单里的词。用白名单 + 400 而不是「未知词则忽略」，是因为忽略会造出
# **第三种静默零结果**：`?sources=tdaii`（手一抖）→ 两路全跳 → HTTP 200、
# `backends:[]`、无 error、count=0——与本次要修的缺陷对外表现一模一样。
# v0.13.26 批1：白名单动态扩容进 memfed 注册表（claude_mem / pi_sessions /
# codex_sessions / workspace_files / archived_sessions）。仍保持「local,tdai」为默认值：
# 联邦源是 opt-in，前端（批4）与注入通道（批5）点名后再开，避免默认行径突变。
SOURCE_WHITELIST = ("local", "tdai") + tuple(memfed.enabled_ids())
#: 联邦源路由：memfed 搜索结果 → RRF（routes 顺序即权威度顺序，权重从注册表取）
_FED_TAIL = tuple(memfed.enabled_ids())


def _split_sources(sources: str) -> set:
    want = {s.strip() for s in (sources or "").split(",") if s.strip()}
    bad = sorted(want - set(SOURCE_WHITELIST))
    if bad:
        raise HTTPException(
            400, f"未知 sources: {bad}；可用值 {list(SOURCE_WHITELIST)}")
    if not want:
        raise HTTPException(400, "sources 不能为空")
    return want


# RRF 权重：权威库满权，原始会话略降，本地便签压到 0.2。
# 为何本地不是 0：它记的是「hub 自己看到的本机事实」（端口/部署），TDAI 里没有；
# 为何不是 1.0：只有 4 行且与权威库同权时，`sorted` 的稳定排序会让它**恰好压过
# TDAI 第一名**（rank0 双方同为 1/(K+1)），4 命中 3 条即占 3 席。
# 09-24 独立复核指出 `weights` 形参全仓无人传（死旋钮），已用真代码复现。
W_LOCAL, W_TDAI_L1, W_TDAI_L0 = 0.2, 1.0, 0.6

# ── 单源名次配额（v0.13.103 P0-1，2026-10-13）────────────────────────
#: 融合结果里**单个源最多占多少比例**的槽位（0 = 不限制，回退旧行为）。
#: 为什么必须加：TDAI L1 权重 1.0 且**每次返回满额 top-N、没有相关性下限**，
#: 于是 RRF 里连它第 10 名（1.0/(60+10)=0.0143）都高于其他源第 1 名
#: （其余源权重最高 0.8 ⇒ 0.8/61=0.0131）——**数学上其余 10 路永远进不了结果**。
#: 2026-10-13 实测（20 查询 × top10）：TDAI 独占 200/200 槽位；逐源单查平均
#: 5.2 路有货，融合后只剩 1 路（丢弃 81% 可用证据）。配额把「名次」与「总量」
#: 解耦：权威库仍排最前，但不许吃满，其余源按 RRF 名次补位。
#: 为何按比例而非固定值：limit 会随调用方变（KB_DEFAULT_K/limit 查询参数），
#: 固定 4 在 limit=50 时又变成 TDAI 独占 8 成。
FUSE_TOP_FRAC = float(os.getenv("MEMORY_FUSE_TOP_FRAC", "0.4"))


def _rank_cap(items, frac: float, limit: int):
    """多源融合结果的**轮转配额**：返回 `(out, rescued_sources)`，out 长度 = min(limit, len)。

    做法对齐本仓 `_fed_section` 已有的轮转口径（那里注释写得很清楚：**先 RRF 再截断
    会让某个命中多的源独占名额**）——这里是同一病灶在**主检索路径**上的版本。

    为什么不只是「每源截前 cap 条」：那样 kept 仍按全局 RRF 序排列（TDAI 全在前），
    截到 limit 时**排在后面的源依然一个名额都拿不到**（实测四源场景第 4 源 = 0 席）。
    轮转取（第 i 轮每源各出第 i 条）才能保证「只要该源有货，就轮到它」。

    cap = max(1, int(limit*frac))：单源在一轮里最多贡献 cap 条，防止某源轮次内刷屏。
    超出 cap 的条目按**原全局 RRF 序**回填，保证「没多样性时总量不缩水」。
    frac<=0 ⇒ 原样返回（保留「退回旧行为」开关，便于 A/B 与排障）。

    `rescued_sources` = 轮转后出现、而纯 RRF top-limit 里**没有**的源——这正是修复的
    直接证据（旧版 TDAI 独占时它非空，且非空是常态）。
    """
    if frac <= 0:
        return items[:limit], []
    cap = max(1, int(limit * frac))
    by_src: dict = {}
    order: list = []
    for it in items:
        s = it.get("source") or ""
        if s not in by_src:
            by_src[s] = []
            order.append(s)
        by_src[s].append(it)
    kept: list = []
    for i in range(cap):
        for s in order:                      # order 沿用全局 RRF 序 ⇒ 权重高的源在各轮里靠前
            if i < len(by_src[s]):
                kept.append(by_src[s][i])
    kept_ids = {id(x) for x in kept}
    overflow = [it for it in items if id(it) not in kept_ids]   # 保持全局 RRF 序
    out = (kept + overflow)[:limit]
    pure = {it.get("source") or "" for it in items[:limit]}
    rescued = sorted({it.get("source") or "" for it in out} - pure)
    return out, rescued


def _exc(e: BaseException) -> str:
    """异常 → 可外发的短字符串。**必须过 scrub**：上游异常文本里可能带 URL/头片段，
    直接 `str()` 进 `backends[].error` 是一条绕过脱敏的出口（09-24 复核发现的真漏 1）。
    与 `tdai_client` 共用同一个 scrub 实现，脱敏只能有一处。"""
    return tdai_client.scrub(f"{type(e).__name__}: {e}")[:160]


def _rrf_fuse(rankings, weights=None, limit=10):
    """Reciprocal Rank Fusion：score = Σ weight/(RRF_K + rank + 1)。

    为什么不用原分加权：本地那路是 LIKE，根本没有可比的分数；一旦拿
    `0.76 > 1` 这种比较去排序，就是在拿两侧量纲开源。只用名次。
    去重键带 source，避免本地整型 id 与 TDAI 的 `m_xxx` 字符串 id 撞车。
    """
    pool: dict = {}
    n = len(rankings)
    w = weights or [1.0] * n
    for ri, lst in enumerate(rankings):
        for rank, it in enumerate(lst or []):
            key = f"{it.get('source','')}|{it.get('id') or (it.get('content') or '')[:80]}"
            e = pool.get(key)
            if e is None:
                e = dict(it)
                e["rrf"] = 0.0
                e["from"] = []
                pool[key] = e
            e["rrf"] += w[ri] / (RRF_K + rank + 1)
            src = it.get("source")
            if src and src not in e["from"]:
                e["from"].append(src)
    out = sorted(pool.values(), key=lambda x: -x["rrf"])[:limit]
    for o in out:
        o["rrf"] = round(o["rrf"], 5)
    return out


def _backend(name: str, r: dict) -> dict:
    """把一次各路调度的结果压成可读诊断字段。**ok=false 时必须带 error**。"""
    return {"name": name, "ok": bool(r.get("ok")), "count": int(r.get("count") or 0),
            "ms": r.get("ms"), "error": r.get("error") if not r.get("ok") else None}


class MemoryIn(BaseModel):
    content: str = Field(min_length=1, max_length=8000)
    category: str = "fact"
    source: str = "manual"
    session_id: Optional[str] = None


class MemoryBatchIn(BaseModel):
    items: list[MemoryIn] = Field(max_length=200)


class DocIn(BaseModel):
    content: Optional[str] = None
    manual: Optional[str] = None


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── L1 ────────────────────────────────────────────────────────────────

@router.get("/api/memory/l1")
async def list_l1(category: Optional[str] = None, q: Optional[str] = None,
                  limit: int = Query(default=50, le=200)):
    sql = "SELECT * FROM memories WHERE status='active' AND layer='L1'"
    params: list = []
    if category:
        sql += " AND category=?"
        params.append(category)
    if q:
        sql += " AND content LIKE ?"
        params.append(f"%{q}%")
    sql += " ORDER BY id DESC LIMIT ?"
    params.append(limit)
    rows = db.query(sql, tuple(params))
    return {"memories": rows, "count": len(rows)}


@router.post("/api/memory/l1")
async def create_l1(body: MemoryIn, request: Request):
    if body.category not in CATEGORIES:
        raise HTTPException(400, f"category must be one of {sorted(CATEGORIES)}")
    mid = db.add_memory(body.content, body.category, body.source, body.session_id)
    # detail 不记正文（正文已在 memories 表；重复记 = 体积翻倍且多一处凭据面）
    db.log_asset_event("memory_l1", str(mid), "create", writeauth.actor_of(request),
                       {"category": body.category, "source": body.source,
                        "chars": len(body.content or ""), "session_id": body.session_id})
    return {"id": mid, "status": "created"}


@router.post("/api/memory/l1/batch")
async def create_l1_batch(body: MemoryBatchIn, request: Request):
    ids = []
    for item in body.items:
        cat = item.category if item.category in CATEGORIES else "fact"
        ids.append(db.add_memory(item.content, cat, item.source or "extract", item.session_id))
    db.log_asset_event("memory_l1", "batch", "create", writeauth.actor_of(request),
                       {"count": len(ids), "ids": ids[:50]})   # 汇总一行；ids 截 50 防单行爆体积
    return {"ids": ids, "count": len(ids)}


@router.delete("/api/memory/l1/{mid}")
async def delete_l1(mid: int, request: Request):
    n = db.execute("UPDATE memories SET status='deleted', updated_at=? WHERE id=?", (_now(), mid))
    if not n:
        raise HTTPException(404, "memory not found")
    db.log_asset_event("memory_l1", str(mid), "delete", writeauth.actor_of(request),
                       {"soft": True})          # 软删（status='deleted'），不是物理删除
    return {"status": "deleted", "id": mid}


@router.get("/api/memory/search")
@runlog.track("mem.search")
async def search_memory(request: Request,
                        q: str = Query(min_length=1), limit: int = Query(default=10, le=50),
                        sources: str = Query(default=FED_FAST_SOURCES),
                        episodic: int = Query(default=0, ge=0, le=20)):
    """记忆检索：本地 L1（LIKE）与 TDAI 权威库（语义）**并联**后 RRF 融合。

    为什么不保留 v0.13.15 及以前的「本地无命中才打 TDAI」短路：
    本地 `memories` 实测只有 4 行 09-06 陈旧便签，一旦它命中就会把权威库整个屏蔽；
    更重的是原实现的 TDAI 分支路径/方法/两个鉴权头全错且被 `except: pass` 吞掉，
    导致对外只报 `{count:0, engine:"keyword"}` 的 HTTP 200——「全指标绿而功能层已死」。

    `episodic>0` 时额外并一路 L0 原始会话检索（TDAI）。
    失败不抛错、不静默：`backends[]` 逐路报 ok/count/ms/error，前端与闸门都能断言。
    """
    t0 = time.monotonic()
    want = _split_sources(sources)
    backends = []
    routes = []                      # [(name, items, weight)] 顺序即权威度顺序

    local = _local_l1(q, limit) if "local" in want else []
    if "local" in want:
        routes.append(("local", local, W_LOCAL))
        backends.append({"name": "local", "ok": True, "count": len(local),
                         "ms": None, "error": None})

    tdai_hits = []
    if "tdai" in want:
        tasks = [tdai_client.search_memories(q, limit, timeout_s=CONTEXT_TIMEOUT_S * 2.5)]
        if episodic:
            tasks.append(tdai_client.search_conversations(q, episodic,
                                                          timeout_s=CONTEXT_TIMEOUT_S * 2.5))
        res = await asyncio.gather(*tasks, return_exceptions=True)
        c = res[0]
        if isinstance(c, Exception):        # gather 兼容：客户端本不应抛，抛了也不能择掉
            c = {"ok": False, "count": 0, "error": _exc(c)}
        tdai_hits = c.get("items") or []
        backends.append(_backend("tdai_l1", c))
        # L1 先入 rankings、L0 后入：旧写法把 L0 排在 L1 前面，同一名次下
        # **原始会话会压过结构化记忆**，方向反了（09-24 独立复核）。
        routes.append(("tdai_l1", tdai_hits, W_TDAI_L1))
        if episodic:
            e = res[1] if len(res) > 1 else {"ok": False, "count": 0, "error": "未执行"}
            if isinstance(e, Exception):
                e = {"ok": False, "count": 0, "error": _exc(e)}
            backends.append(_backend("tdai_l0", e))
            if e.get("ok"):
                routes.append(("tdai_l0", e.get("items") or [], W_TDAI_L0))

    # 联邦外部源（claude-mem / 各 CLI 会话 / 工作区文件 / 归档）：与 TDAI 并联、
    # 各自独立超时、失败逐路报 degraded。权重低、粒度是「文件/观察级」，
    # 在 RRF 里天然排在结构化记忆之后——不压过权威库，但不再缺席。
    fed_want = want & set(_FED_TAIL)
    fed_results = await memfed.search_fed(q, limit, fed_want) if fed_want else {}
    for sid in _FED_TAIL:
        if sid not in fed_want:
            continue
        r = fed_results.get(sid) or {"ok": False, "count": 0, "items": [],
                                     "error": "未执行"}
        backends.append(_backend(sid, r))
        if r.get("ok") and r.get("items"):
            routes.append((sid, r["items"], memfed.fed_weight(sid)))

    # 先按 limit 取「配额后仍可能补齐」的候选池，再做单源配额，最后截到 limit。
    # 为何取大池：若先截 limit 再配额，被砍掉的源腾出的空位没有后备可补，
    # 结果总数会缩水（用户看到 10 条变 6 条）。池子够大才能「砍掉一路、补上另一路」。
    pool_cap = max(limit, limit * 4)
    ranked = _rrf_fuse([r[1] for r in routes],
                       weights=[r[2] for r in routes], limit=pool_cap)
    fused, capped_src = _rank_cap(ranked, FUSE_TOP_FRAC, limit)
    engines = [b["name"] for b in backends if b["ok"] and b["count"]]
    degraded = [b["name"] for b in backends if not b["ok"]]
    #: 配额生效的直接证据：轮转救回、而纯 RRF top-limit 里没有的源。
    #: 不静默——旧版「TDAI 吃满 200/200」在回包里是**看不出来**的。
    return {
        "memories": fused,
        "count": len(fused),
        # 保留 `engine` 与 `tdai` 两个旧字段：hubmcp.hub_memory_search 还在读。
        # 旧实现里它拿 `bool(data.get("tdai"))` 把 TDAI 结果整个丢弃，所以即使透传通了
        # 也会回 count=0；现在 TDAI 条目已入 fused，count 自然对了。
        "engine": "+".join(engines) + "|rrf" if engines else "none",
        "tdai": bool(tdai_hits),
        "backends": backends,
        "degraded": degraded,
        "cap": {"frac": FUSE_TOP_FRAC, "cap_n": max(1, int(limit * FUSE_TOP_FRAC)),
                "rescued_sources": capped_src,
                "sources_in_result": sorted({m.get("source") for m in fused if m.get("source")})},
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


# ── L2 / L3 文档 ──────────────────────────────────────────────────────

def _get_doc(layer: str) -> dict:
    rows = db.query("SELECT * FROM memory_docs WHERE layer=?", (layer,))
    if rows:
        return rows[0]
    return {"layer": layer, "content": "", "manual": "", "updated_at": None}


def _put_doc(layer: str, content: Optional[str], manual: Optional[str]) -> dict:
    cur = _get_doc(layer)
    new_content = content if content is not None else cur["content"]
    new_manual = manual if manual is not None else cur["manual"]
    db.execute(
        """INSERT INTO memory_docs(layer,content,manual,updated_at) VALUES(?,?,?,?)
           ON CONFLICT(layer) DO UPDATE SET content=excluded.content,
             manual=excluded.manual, updated_at=excluded.updated_at""",
        (layer, new_content, new_manual, _now()))
    return _get_doc(layer)


def _audit_doc(layer: str, body: "DocIn", request) -> None:
    """L2/L3 变更审计。touched 必须点名动了哪个字段 —— `manual` 是用户手写补充，
    09-23 曾被 rebuild 静默覆盖过（见 writeauth.py docstring 记录的事故），它被动过要单独留痕。
    只记字符数不记正文：正文在 memory_docs 表里，审计不是第二份副本。"""
    touched = [k for k, v in (("content", body.content), ("manual", body.manual))
               if v is not None]
    db.log_asset_event("memory_doc", layer, "update", writeauth.actor_of(request),
                       {"touched": touched,
                        "content_chars": len(body.content) if body.content is not None else None,
                        "manual_chars": len(body.manual) if body.manual is not None else None})



@router.get("/api/memory/l2")
async def get_l2():
    return _get_doc("L2")


@router.put("/api/memory/l2")
async def put_l2(body: DocIn, request: Request):
    _audit_doc("L2", body, request)
    return _put_doc("L2", body.content, body.manual)


@router.get("/api/memory/l3")
async def get_l3():
    return _get_doc("L3")


@router.put("/api/memory/l3")
async def put_l3(body: DocIn, request: Request):
    _audit_doc("L3", body, request)
    return _put_doc("L3", body.content, body.manual)


@router.post("/api/memory/l2/rebuild")
async def rebuild_l2(request: Request):
    """用近 30 天 L1 压缩生成 L2.content 草稿（LLM 不可用时返回降级拼接）"""
    since = (datetime.now(timezone.utc) - timedelta(days=30)).isoformat()
    rows = db.query(
        "SELECT category,content,created_at FROM memories "
        "WHERE status='active' AND layer='L1' AND created_at>=? ORDER BY id DESC LIMIT 300",
        (since,))
    if not rows:
        raise HTTPException(400, "近30天无 L1 记忆可压缩")
    raw = "\n".join(f"- [{r['category']}] {r['content']} ({r['created_at'][:10]})" for r in rows)
    llm_used = False
    if llm.configured():
        try:
            content, _ = await llm.chat_tools_loop(
                [{"role": "system", "content":
                  "你是记忆整理器。把下面的记忆条目压缩为按主题分组的中文要点文档（Markdown，"
                  "去重合并，保留精确数值/路径/ID），不要添加条目中没有的内容。直接输出文档正文。"},
                 {"role": "user", "content": raw}],
                tools=[], max_rounds=1)
            llm_used = True
        except Exception:  # noqa: BLE001
            content = f"# 近30天工作记忆（机械压缩 {len(rows)} 条）\n\n{raw}"
    else:
        content = f"# 近30天工作记忆（机械压缩 {len(rows)} 条）\n\n{raw}"
    doc = _put_doc("L2", content, None)  # manual 不动
    db.log_asset_event("memory_doc", "L2", "rebuild", writeauth.actor_of(request),
                       {"items": len(rows), "llm": llm_used,
                        "manual_untouched": True})   # _put_doc(…, None) ⇒ manual 一字未动，如实记

    return {"status": "rebuilt", "items": len(rows), "llm": llm_used,
            "updated_at": doc["updated_at"]}


def _fed_section(fed_results: Dict[str, Dict], fed_want: set,
                 top: int, max_chars: int):
    """把联邦结果压成注入包的第 4 段。返回 (lines, done, skipped_budget, failed, chars)。

    为什么**轮转**取而不是先 RRF 再截断：先 RRF 会让某个命中多的源（archived_sessions
    权重 0.4 但扫 5622 文件、极易多命中）独占名额，其他路一条露不出脸；轮转保证每路
    都按注册表序（= 权重降序）轮到，预算不够时也是「均匀地被砍」而不是「整路消失」。
    """
    done: list = []
    skipped: list = []
    failed: list = []
    pools: list = []
    for sid in _FED_TAIL:
        if sid not in fed_want:
            continue
        r = fed_results.get(sid) or {}
        if r.get("ok"):
            done.append(sid)
            items = r.get("items") or []
            if items:
                pools.append((sid, items))
        elif r.get("error") == memfed.SKIPPED_BUDGET:
            skipped.append(sid)
        else:
            failed.append(sid)

    labels = {s.id: s.label for s in memfed.REGISTRY.values()}
    lines: list = []
    used = 0
    if pools:
        depth = max(len(items) for _, items in pools)
        stop = False
        for i in range(min(top, depth)):
            for sid, items in pools:
                if i >= len(items):
                    continue
                it = items[i] if isinstance(items[i], dict) else {}
                text = str(it.get("content") or it.get("title") or "")[:FED_ITEM_CHARS]
                if not text.strip():
                    continue
                line = f"- [{labels.get(sid, sid)}] {text}"
                if used + len(line) + 1 > max_chars:
                    stop = True
                    break
                lines.append(line)
                used += len(line) + 1
            if stop:
                break
    # 预算与故障都**写在段里**：模型看到「只有 2 路」时得知道另外 7 路的下落，
    # 否则它会以为「这就是全部」——这正是本次修复要消灭的静默丢弃的换皮。
    if skipped:
        lines.append(f"- （另有 {len(skipped)} 路未在时间预算内返回：{', '.join(skipped)}）")
    if failed:
        lines.append(f"- （另有 {len(failed)} 路检索失败：{', '.join(failed)}）")
    return lines, done, skipped, failed, used


# ── 注入通道 ──────────────────────────────────────────────────────────

@router.get("/api/memory/context")
@runlog.track("mem.context")
async def injection_context(request: Request, q: Optional[str] = None,
                            max_chars: int = Query(default=6000, le=20000),
                            sources: str = Query(default="local,tdai"),
                            scenes: int = Query(default=0, ge=0, le=20),
                            fed_chars: int = Query(default=2000, ge=0, le=4000),
                            fed_top: int = Query(default=3, ge=1, le=10)):
    """SessionStart 注入包：L3 + L2(content+manual) + 相关 L1（本地与权威库融合）。

    为什么 L3 要回落到 TDAI：hub 自己的 `memory_docs` 实测只有 L2 一行，**L3 为空**，
    所以旧版返回的开局包里根本不会出现「## 长期 Profile」段——实测只有 357 字、
    且是 18 天前的 L2 旧稿。权威 L3（persona）在 TDAI `/v2/core/read`。

    时间预算：本函数坐在会话起始链路上，TDAI 各路给 `MEMORY_CONTEXT_TIMEOUT`（默认 1.2s），
    超时即**弃该路并记入 degraded**，绝不把对话卡在记忆检索后面。
    """
    t0 = time.monotonic()
    want = _split_sources(sources)
    l3 = _get_doc("L3")
    l2 = _get_doc("L2")

    # 一开始就把所有 TDAI 调用并发发出去，不串行等
    calls = {}
    if "tdai" in want and not (l3["content"] or l3["manual"]):
        calls["profile"] = tdai_client.core_read(timeout_s=CONTEXT_TIMEOUT_S)
    if "tdai" in want and q:
        calls["l1"] = tdai_client.search_memories(q, 10, timeout_s=CONTEXT_TIMEOUT_S)
    if "tdai" in want and scenes:
        calls["scenes"] = tdai_client.scenario_ls(timeout_s=CONTEXT_TIMEOUT_S)
    results = dict(zip(calls.keys(), await asyncio.gather(*calls.values(),
                                                         return_exceptions=True))) \
        if calls else {}
    backends = []
    for name, r in results.items():
        if isinstance(r, Exception):
            r = {"ok": False, "count": 0, "error": _exc(r)}
            results[name] = r
        if name == "profile" and r.get("ok"):
            # /v2/core/read 的契约里没有 items，不补这一刀会出现
            # 「ok:true 但 count:0」这个歧义信号——而歧义信号正是本次要消除的东西。
            r["count"] = 1 if (r.get("content") or "").strip() else 0
        backends.append(_backend(f"tdai_{name}", r))
    if "local" in want:
        backends.append({"name": "local", "ok": True,
                         "count": int(bool(l3["content"] or l3["manual"]))
                         + int(bool(l2["content"] or l2["manual"])),
                         "ms": None, "error": None})

    # ── 联邦外部源（v0.13.65 D1）──
    # 改前这里**没有这段**：`sources` 里的联邦 id 过完 _split_sources 白名单就被丢掉，
    # 回包 backends 只有 tdai_profile/local，HTTP 200、无异常、无告警——agent 的开局包
    # 永远拿不到任何其他 agent 的记忆。现在接上，并给墙钟（CONTEXT_TIMEOUT_S）：
    # 坐在会话起始链路上，宁可少给一路，也不能把开局卡死在记忆检索后面。
    fed_want = want & set(_FED_TAIL)
    fed_results: Dict[str, Dict] = {}
    fed_skip_reason: Optional[str] = None
    if fed_want:
        if fed_chars <= 0:
            # 旧版这里把 fed_chars=0 静默当成「没配联邦」，回包仍报 sources=N，
            # 于是「计数绿、内容空」。现在把原因说清楚。
            fed_skip_reason = f"未执行：fed_chars={fed_chars}（≤0 表示明确关闭联邦段）"
        elif not q:
            # **无查询词 ⇒ 取各源最新条目**（P0-3，2026-10-13 修）。
            # 旧版直接跳过联邦 ⇒ 会话起始注入的「其他 Agent 记忆」段永远是 0 字符，
            # 而回包仍报 `fed.sources=N`——这正是本仓库反复警惕的「指标绿、功能死」。
            # 会话开始时没有查询词是**常态**（pi 门面就是无 q 调这个端点），
            # 此时「最近各 agent 在做什么」恰恰是有用的开场上下文；用 __recent__ 哨兵召回。
            fed_results = await memfed.search_fed(memfed.RECENT_Q, 20, fed_want,
                                                  wall_s=CONTEXT_TIMEOUT_S)
        else:
            fed_results = await memfed.search_fed(q, 20, fed_want, wall_s=CONTEXT_TIMEOUT_S)
        for sid in _FED_TAIL:
            if sid not in fed_want:
                continue
            backends.append(_backend(sid, fed_results.get(sid) or {
                "ok": False, "count": 0, "items": [],
                "error": fed_skip_reason or "未执行（无 q 或 fed_chars=0）"}))
    fed_meta = {"sources": len(fed_want), "done": [], "skipped_budget": [],
                "failed": [], "chars": 0}
    if fed_skip_reason:
        fed_meta["reason"] = fed_skip_reason

    parts = []
    if l3["content"] or l3["manual"]:
        parts.append(f"## 长期 Profile\n{l3['content']}\n{l3['manual']}")
    else:
        pr = results.get("profile") or {}
        if pr.get("ok") and pr.get("content"):
            parts.append("## 长期 Profile（TDAI 权威源）\n"
                         + pr["content"][:PROFILE_CHARS])
    if l2["content"] or l2["manual"]:
        parts.append(f"## 近30天工作记忆\n{l2['content']}\n{l2['manual']}")

    if q:
        local_rows = _local_l1(q, 10)
        tdai_rows = (results.get("l1") or {}).get("items") or []
        # 注入包同样过单源配额（P0-1）：否则 TDAI 满额 top10 会把本地/联邦挤光，
        # 开局「相关记忆」段等于只有权威库一家。
        fused = _rrf_fuse([local_rows, tdai_rows],
                          weights=[W_LOCAL, W_TDAI_L1], limit=30)
        fused = _rank_cap(fused, FUSE_TOP_FRAC, 10)[0]
        if fused:
            parts.append("## 相关记忆\n" + "\n".join(
                f"- [{m.get('category') or m.get('type') or 'l1'}] {m.get('content','')}"
                for m in fused))

    if scenes:
        ent = (results.get("scenes") or {}).get("entries") or []
        if ent:
            parts.append("## 场景索引（TDAI L2）\n"
                         + "\n".join(f"- {e.get('path','')}" for e in ent[:scenes]))

    if fed_want:
        fed_lines, done, skipped, failed, fed_used = _fed_section(
            fed_results, fed_want, fed_top, fed_chars)
        fed_meta = {"sources": len(fed_want), "done": done,
                    "skipped_budget": skipped, "failed": failed, "chars": fed_used}
        # 用「召回到的条目数」而非「跑了几路」判断联邦段是否真的有用：跑通 3 路
        # 但一条没召回（如各源都空），对模型而言与「没跑」等价，必须能区分。
        fed_meta["matched"] = sum(len((fed_results.get(s) or {}).get("items") or [])
                                  for s in fed_want)
        if fed_skip_reason:
            fed_meta["reason"] = fed_skip_reason
        if fed_lines:
            parts.append("## 其他 Agent 记忆（联邦）\n" + "\n".join(fed_lines))

    text = "\n\n".join(parts)
    truncated = len(text) > max_chars
    body = text[:max_chars]
    degraded = [b["name"] for b in backends if not b["ok"]]
    # 「绿而空」是一种必须说出口的故障：联邦源声明了几路、却一条没召回
    # （或压根没执行），回包形状看着成功。这里把它翻成显式 degraded 标记。
    if fed_want and not (fed_meta.get("matched") or fed_meta.get("chars")):
        fed_meta["degraded"] = True
    return {"context": body, "truncated": truncated, "chars": len(body),
            "backends": backends, "degraded": degraded, "fed": fed_meta,
            "took_ms": round((time.monotonic() - t0) * 1000, 1)}
