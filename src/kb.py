"""联邦检索门面 /api/kb/* —— 把本机**已有**的检索能力并发调起、融合、逐路表态。

【为什么不建 hub 自己的知识库】
记忆的权威副本是 TDAI(:8420，实测 L1 236 / L0 6054)，文档的权威副本是
`/fs/1000/ftp/技术文档` 文件系统本身（turbovec 索引只是它的投影，1.02MB / 2791 chunks）。
hub 再建一份 `knowledge.db` 就是**第二权威副本**——它会腐烂、会与源头不一致，
而且按本机「唯一权威副本」主权原则，那正是要避免的资产形态。所以本模块只做
**门面 + ACL + 审计 + 归并**，一个字都不存。

【四路取证的实测结论（2026-09-24，取证过程见 PENDING-TASKS PT-20260924-10）】
① TDAI L1 语义：`tdai_client` 已在 P0 接通，实测 3.0ms/次 ⇒ 入门面。
② turbovec 技术文档：实测可用（`[0.512] path#chunk7` 命中真实文档），但
   **CLI 没有 JSON 出口**（只有 `info` 是 JSON），且子进程固定开销 **0.25s**
   （numpy + core 导入 + load_meta，RSS 41MB）+ ollama embed 34ms。
   ⇒ 入门面，但**单独给预算**（`TV_TIMEOUT_S`，默认 **2.0s**；生产实测一次 395ms），绝不上会话起始链路（那里是 1.2s 总预算）。
     ⚠ 本行原写「800ms 预算」，与代码默认值 2.0s 不一致，09-24 12:4x 由一次外派只读复核抓出并改正。
     口径：**文档串里的数字必须与常量同源**，否则后人会按错的预算去调参。
③ hub 本地 `memories`：P0 已接（权威度权重 0.2，因为它只有 4 行陈旧便签）。
④ **wigolo 网页缓存：不接**。理由全部来自实测——它的 `url_cache_fts` 建表时
   **没有 `tokenize=` 子句**（默认 unicode61），中文查询「端口」FTS 只召回 **2** 条
   而 `LIKE` 有 **8** 条，「配置项」FTS **0** 条；也就是说 FTS 在中文上**不比 LIKE 强、
   还更绕**。而 LIKE 全表扫 353 行只要 **9.2ms**，根本不需要 FTS。更要紧的是：
   对 `agent-hub` 这个主题 **LIKE 命中 0 条**（内容以 MDN 85 / 知乎 36 / 百度 31 为主），
   而它的最近抓取时间虽然是 09-24 00:02，**却是别的会话为别的任务攒的**。
   ⇒ 收益接近零，代价是跨进程读一个**活动写入中的 60MB 他方 db**（schema_migrations 已 9 版），
     耦合与误写风险都归 hub 担。故 P1 明确不接，判据与数字都写在这里，防止后人再测一遍。

【失败表态纪律（承接 P0）】
每路必须回 `backends[] = {name, ok, count, ms, error}`，任何一路挂了都要能说出
「挂的是哪路、为什么挂、这次结果少了什么」。**禁止 `except: pass`、禁止静默返回空**。
"""
import asyncio
import json
import logging
import os
import re
import time
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request

import tdai_client
import memfed
import ctxmanifest
import db
import memstats
import runlog

log = logging.getLogger("hub.kb")
router = APIRouter(prefix="/api/kb", tags=["kb"])

# ── 配置（全部可 env 覆盖；默认值来自 09-24 实测，不是猜的）────────────
TURBOVEC_PY = os.getenv("KB_TURBOVEC_PY", "/vol1/turbovec-pilot/venv/bin/python")
# 模块**没有装进 venv**，必须在它的包目录下以 `-m` 跑（09-24 实测：换 cwd 即 ModuleNotFoundError）
TURBOVEC_CWD = os.getenv("KB_TURBOVEC_CWD", "/fs/1000/ftp/技术文档/turbovec-mcp")
TURBOVEC_INDEX = os.getenv("KB_TURBOVEC_INDEX", "/vol1/turbovec-pilot/data/techdocs.tvim")
TV_TIMEOUT_S = float(os.getenv("KB_TURBOVEC_TIMEOUT", "2.0"))   # 0.25s 固定开销 + embed + 检索，留足
TDAI_TIMEOUT_S = float(os.getenv("KB_TDAI_TIMEOUT", "0.4"))     # 实测 3ms，400ms 已是很宽的天花板
DEFAULT_K = int(os.getenv("KB_DEFAULT_K", "8"))
CACHE_TTL_S = float(os.getenv("KB_INFO_TTL", "600"))            # 索引重建是人工触发的，10 分钟够新

# ── XERJ（2026-10-18 接替 turbovec）────────────────────────────────
# 文档路的权威实现是 /fs/1000/ftp/技术文档/scripts/docs-search.sh 的 T16 模板；
# 这里**逐字复刻同一 query**（title mss=3 boost2 / body mss=100% / 排除 8 个聚合页），
# 避免「hub 一套排序、recall 另一套」。实测 nDCG@5 0.7907（v6/37 条）。
XERJ_URL = os.getenv("KB_XERJ_URL", "http://127.0.0.1:9300")
XERJ_INDEX = os.getenv("KB_XERJ_INDEX", "axv3-*")
XERJ_TIMEOUT_S = float(os.getenv("KB_XERJ_TIMEOUT", "1.0"))     # 实测 p50 1.3ms，1s 是很宽的天花板
XERJ_AGG_PAGES = ["wiki/index.md", "wiki/concepts/_index.md", "wiki/entities/_index.md",
                  "wiki/log.md", "MEMORY.md", ".skills-index.md", "项目详细清单.md",
                  "PENDING-TASKS.md"]

#: 默认路由：turbovec 已退役（索引/模型 2026-10-18 删除），不再入默认；
#: local 是 2026-09-06 的 4 条基线便签，默认不参与（需显式点名）。
ROUTES = ("tdai", "xerj", "workspace", "archived")
#: 仍接受但明确回 retired，不再起子进程、不再 400（老调用方不被打断）
LEGACY_ROUTES = {"turbovec": "已退役（2026-10-18 索引/模型删除）；文档路改用 xerj",
                 "local": "P0 基线便签（2026-09-06），默认不参与；显式点名才查"}

#: workspace / archived 两路的实现＝转调批1 memfed 的 rg 适配器（文件级全文命中）。
#: 为什么不在 kb.py 重写 rg：memfed 实测时已踩过两个坑（rg 缺 -n 会解析失配；
#: rg 默认尊重 .gitignore → 会话备份被技术文档 .gitignore 排除、只扫到 2/5350），
#: 那套命令行（-F -i -n -m 1 --no-ignore --hidden --max-filesize 20M）就是唯一权威
#: 实现；复用即免重跑一遍坑。权重直接引 REGISTRY 同源值，防止两处数字漂移。
#: ⚠ turbovec 语义索引也是技术文档的投影，workspace 路是它的**实时全文补充**
#: （索引重建是人工触发、有滞后；rg 直扫盘面是新鲜的）——两路并存是有意的。

#: kb 路名 → memfed 源 id 映射（两个名字不同源的丗代裂缝）
_FED_ROUTE_MAP = {"workspace": "workspace_files", "archived": "archived_sessions"}

# turbovec 的输出形态（照抄 cmd_search）：
#   1. [0.512] 相对路径#chunk7
#      <预览，可能自带换行，最长 120 字符>
# 预览里出现换行是常态（Markdown 原文），所以**不能按"一行一条"解析**：
# 以 `N. [score] path#chunkK` 为记录起点，到下一条起点为止的全部内容都归它。
_ROW = re.compile(r"^\s*(\d+)\.\s*\[(-?\d+\.\d+)\]\s*(.+?)#chunk(\d+)\s*$", re.M)


def parse_turbovec_stdout(out: str) -> List[Dict[str, Any]]:
    """把人类可读输出解析成结构化行。解析不出任何一条时返回 []，由调用方表态成 error。"""
    marks = list(_ROW.finditer(out))
    rows: List[Dict[str, Any]] = []
    for i, m in enumerate(marks):
        start = m.end()
        end = marks[i + 1].start() if i + 1 < len(marks) else len(out)
        preview = out[start:end].strip()
        rows.append({
            "score": float(m.group(2)),
            "path": m.group(3).strip(),
            "chunk": int(m.group(4)),
            "preview": preview[:240],
            "source": "turbovec",
        })
    return rows


async def _turbovec_search(q: str, k: int) -> Dict[str, Any]:
    """子进程调 turbovec。永不抛：一律回 {ok, items, ms, error}。"""
    t0 = time.perf_counter()
    cmd = [TURBOVEC_PY, "-m", "techdocs_mcp.cli", "search", q, "-k", str(k)]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=TURBOVEC_CWD,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    except (OSError, ValueError) as e:
        return {"ok": False, "items": [], "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"起不来（{type(e).__name__}）：解释器或 cwd 不对？{e}",
                "detail": {"cmd": " ".join(cmd), "cwd": TURBOVEC_CWD}}

    try:
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=TV_TIMEOUT_S)
        ms = round((time.perf_counter() - t0) * 1000, 1)
    except asyncio.TimeoutError:
        try:
            proc.kill()
            await proc.wait()
        except Exception:  # noqa: BLE001 — 超时后收尾失败不该再盖掉超时这个真因
            pass
        return {"ok": False, "items": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"超时 {TV_TIMEOUT_S}s 已杀进程（该路本次弃用；ollama 没常驻时冷启动会偏慢）"}

    out = (out_b or b"").decode("utf-8", "replace")
    if proc.returncode != 0:
        txt = tdai_client.scrub((err_b or out).decode("utf-8", "replace"))[:300]
        return {"ok": False, "items": [], "ms": ms,
                "error": f"退出码 {proc.returncode}：{txt}"}
    rows = parse_turbovec_stdout(out)
    if not rows and "(无结果)" not in out and out.strip():
        # 输出了东西但解析不出一条 ⇒ 上游改了打印格式。这**必须**报错，
        # 不能当成"没有命中"——那正是 P0 修掉的那类静默。
        return {"ok": False, "items": [], "ms": ms,
                "error": "输出无法解析（疑似 CLI 打印格式变更）",
                "detail": {"head": tdai_client.scrub(out)[:200]}}
    return {"ok": True, "items": rows, "ms": ms, "error": None}


_INFO_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}


def _xerj_body(q: str, k: int) -> Dict[str, Any]:
    """与 scripts/docs-search.sh 的 T16 逐字同源（改一处必须同步另一处）。"""
    return {
        "size": k,
        "query": {"bool": {
            "should": [
                {"match": {"title": {"query": q, "boost": 2.0, "minimum_should_match": 3}}},
                {"match": {"body": {"query": q, "minimum_should_match": "100%"}}},
            ],
            "must_not": [{"terms": {"ax_path": XERJ_AGG_PAGES}}],
        }},
        "_source": ["ax_path", "title", "ax_dataset"],
        "highlight": {"fields": {"body": {"fragment_size": 280, "number_of_fragments": 1}}},
    }


async def _xerj_search(q: str, k: int) -> Dict[str, Any]:
    """正查 XERJ（HTTP，无子进程）。永不抛：一律回 {ok, items, ms, error}。"""
    t0 = time.perf_counter()
    try:
        import aiohttp
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": [], "ms": None,
                "error": f"aiohttp 不可用：{type(e).__name__}"}
    try:
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=XERJ_TIMEOUT_S)) as s:
            async with s.post(f"{XERJ_URL}/{XERJ_INDEX}/_search",
                              json=_xerj_body(q, k)) as r:
                body = await r.text()
                if r.status != 200:
                    return {"ok": False, "items": [],
                            "ms": round((time.perf_counter() - t0) * 1000, 1),
                            "error": f"HTTP {r.status}: {tdai_client.scrub(body)[:200]}"}
                d = json.loads(body)
    except asyncio.TimeoutError:
        return {"ok": False, "items": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"超时 {XERJ_TIMEOUT_S}s（systemctl --user status xerj-docs）"}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:200]}

    items: List[Dict[str, Any]] = []
    for h in d.get("hits", {}).get("hits", []) or []:
        src = h.get("_source") or {}
        hl = ((h.get("highlight") or {}).get("body") or [""])[0]
        path = src.get("ax_path") or ""
        items.append({
            "source": "xerj",
            "id": path,
            "path": path,
            "title": src.get("title") or "",
            "content": " ".join(hl.split())[:240],
            "score": h.get("_score") or 0,
            "dataset": src.get("ax_dataset") or "",
        })
    return {"ok": True, "items": items,
            "ms": round((time.perf_counter() - t0) * 1000, 1), "error": None}


async def xerj_info() -> Dict[str, Any]:
    """索引健康 + 篇数（面板用）。失败不拖垮 /api/kb/status。"""
    t0 = time.perf_counter()
    try:
        import aiohttp
        async with aiohttp.ClientSession(
                timeout=aiohttp.ClientTimeout(total=2.0)) as s:
            async with s.get(f"{XERJ_URL}/_cluster/health") as r:
                health = await r.json()
            body = {"size": 0, "aggs": {"ds": {"terms": {"field": "ax_dataset", "size": 20}}}}
            async with s.post(f"{XERJ_URL}/{XERJ_INDEX}/_search", json=body) as r:
                d = await r.json()
        total = d.get("hits", {}).get("total")
        docs = total.get("value") if isinstance(total, dict) else total
        datasets = {b["key"]: b["doc_count"]
                    for b in (d.get("aggregations", {}).get("ds", {}).get("buckets") or [])}
        return {"available": health.get("status") in ("green", "yellow"),
                "status": health.get("status"), "count": docs, "datasets": datasets,
                "url": XERJ_URL, "index": XERJ_INDEX,
                "ms": round((time.perf_counter() - t0) * 1000, 1), "error": None}
    except Exception as e:  # noqa: BLE001
        return {"available": False, "count": None, "url": XERJ_URL,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:200]}



async def turbovec_info(force: bool = False) -> Dict[str, Any]:
    """索引元信息（`info` 子命令**本来就是 JSON**）。带 TTL 缓存，给面板与新鲜度判据用。"""
    now = time.monotonic()
    if not force and _INFO_CACHE["data"] is not None and (now - _INFO_CACHE["ts"]) < CACHE_TTL_S:
        return {**_INFO_CACHE["data"], "cached": True}
    cmd = [TURBOVEC_PY, "-m", "techdocs_mcp.cli", "info"]
    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd, cwd=TURBOVEC_CWD,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
        out_b, err_b = await asyncio.wait_for(proc.communicate(), timeout=TV_TIMEOUT_S)
    except asyncio.TimeoutError:
        try:
            proc.kill(); await proc.wait()  # noqa: ASYNC110
        except Exception:  # noqa: BLE001
            pass
        return {"ok": False, "error": "info 超时", "cached": False}
    except (OSError, ValueError) as e:
        return {"ok": False, "error": f"info 起不来：{type(e).__name__}", "cached": False}
    if proc.returncode != 0:
        return {"ok": False, "error": tdai_client.scrub(err_b.decode("utf-8", "replace"))[:200],
                "cached": False}
    try:
        data = json.loads(out_b.decode("utf-8", "replace"))
    except ValueError as e:
        return {"ok": False, "error": f"info 不是合法 JSON：{e}", "cached": False}
    data["ok"] = True
    data["cached"] = False
    _INFO_CACHE.update(ts=time.monotonic(), data=data)
    return data


def _backend(name: str, r: Dict[str, Any]) -> Dict[str, Any]:
    return {"name": name, "ok": bool(r.get("ok")), "count": len(r.get("items") or []),
            "ms": r.get("ms"), "error": r.get("error") if not r.get("ok") else None}


@router.get("/search")
@runlog.track("kb.search")
async def kb_search(request: Request,
                    q: str = Query(min_length=1),
                    k: int = Query(default=DEFAULT_K, le=30),
                    routes: str = Query(default=",".join(ROUTES)),
                    doc_k: int = Query(default=5, le=20, description="turbovec 取几条")):
    """联邦检索：本机已有检索面并发调起 → RRF 融合 → 逐路表态。

    `routes` 走白名单，未知值直接 400。这不是防攻击，是防**手一抖打出静默空结果**
    （P0 里 `sources=tdaii` 就是这样把"两路全跳"伪装成"没查到"的）。
    """
    t0 = time.monotonic()
    want = {x.strip() for x in (routes or "").split(",") if x.strip()}
    bad = sorted(want - set(ROUTES) - set(LEGACY_ROUTES))
    if bad:
        raise HTTPException(400, f"未知 routes: {bad}；可用值 {list(ROUTES)}（兼容旧值 {list(LEGACY_ROUTES)}）")
    if not want:
        raise HTTPException(400, "routes 不能为空")

    tasks: List[Any]
    names: List[str] = []
    if "tdai" in want:
        tasks = [tdai_client.search_memories(q, k, timeout_s=TDAI_TIMEOUT_S)]
        names.append("tdai_l1")
    else:
        tasks = []
        names = []
    if "xerj" in want:
        tasks.append(_xerj_search(q, doc_k))
        names.append("xerj")
    if "local" in want:
        # 本地那路复用 P0 的口径：只有 4 行陈旧便签，所以低权
        import memory
        tasks.append(_local_async(memory, q, k))
        names.append("local")
    for route, sid in _FED_ROUTE_MAP.items():
        if route in want:
            tasks.append(_fed_async(sid, q, k))
            names.append(route)

    res = await asyncio.gather(*tasks, return_exceptions=True)
    backends: List[Dict[str, Any]] = []
    rankings: List[List[Dict[str, Any]]] = []
    weights: List[float] = []
    engine_items: List[tuple] = []          # P1-1：逐路实际返回的条目（建 manifest 用）
    # 权重口径与 memory.py 同源：文档语义命中（真内容）满权，便签 0.2。
    # ⚠ workspace/archived 在 kb 语境下是**文档全文路**，不是记忆权威度维度——
    # 实测 09-26：若沿用 memfed 的 0.7/0.4 权重，tdai 满权池会把它们全部挤出融合
    # 前列（k=20 时融合分布仍是 tdai 100%），两路变成「backends 绿但结果不可见」的
    # 假接入。kb 里它们与 turbovec 同层：满权，靠 RRF_K 摊平名次差。
    W = {"tdai_l1": 1.0, "xerj": 1.0, "turbovec": 1.0, "local": 0.2,
         "workspace": 1.0, "archived": 1.0}
    for name, r in zip(names, res):
        if isinstance(r, Exception):
            r = {"ok": False, "items": [], "ms": None,
                 "error": tdai_client.scrub(f"{type(r).__name__}: {r}")[:200]}
        backends.append(_backend(name, r))
        if r.get("ok"):
            rankings.append(r.get("items") or [])
            weights.append(W[name])
            engine_items.append((name, r.get("items") or []))

    # 旧路名：不静默、不 400、不起子进程——逐路表态（与其它路的 ok/error 同格式）
    for legacy in ("turbovec", "local"):
        if legacy in want:
            backends.append({"name": legacy, "ok": False, "count": 0, "ms": 0.0,
                             "error": LEGACY_ROUTES[legacy]})

    # 文档路优先级：XERJ 命中则 workspace 降为不参与（防止同一批文档被两套排序重复计分）；
    # XERJ 零命中/不可达时 workspace 作为实时全扫兜底——这正是原来的设计意图。
    xerj_hit = any(b["name"] == "xerj" and b["ok"] and b["count"] for b in backends)
    if xerj_hit:
        keep = [(n, it) for n, it in engine_items if n != "workspace"]
        if len(keep) != len(engine_items):
            # 权重与 rankings 同序重建（前面是 zip 顺序，这里不能直接下标删）
            new_rank: List[List[Dict[str, Any]]] = []
            new_w: List[float] = []
            for n, it in keep:
                new_rank.append(it)
                new_w.append(W[n])
            rankings, weights = new_rank, new_w
            engine_items = keep
            for b in backends:
                if b["name"] == "workspace":
                    b["skipped"] = "xerj 已命中，本路不参与融合（避免同一批文档两套排序重复计分）"

    import memory as _m
    fused = _m._rrf_fuse(rankings, weights=weights, limit=k)
    engines = [n for n, _ in engine_items]
    degraded = [b["name"] for b in backends if not b["ok"]]
    # ── P1-1：Context Manifest（检索面）──
    # 条目 = 每一路**实际返回的条目**（不是候选全集）；排除项 = 本次挂掉的几路
    # （含未启用/无结果返回的联邦源，它们的 error 里已经写清了原因）。
    kparts: List[Dict[str, Any]] = []
    for name, items in engine_items:
        if not items:
            continue
        kparts.append(ctxmanifest.entry(
            name,
            "\n".join(str(it.get("content") or it.get("text") or "") for it in items),
            name,
            ids=[str(it.get("id") or it.get("path") or it.get("doc") or "")
                 for it in items if (it.get("id") or it.get("path") or it.get("doc"))],
            as_of=max((str(it.get("as_of") or it.get("updated_at") or "")
                       for it in items if (it.get("as_of") or it.get("updated_at"))),
                      default=None)))
    kexcl = [ctxmanifest.excluded(b["name"], b.get("error") or "该路未成功返回")
             for b in backends if not b["ok"]]
    manifest = ctxmanifest.build_manifest(kparts, budget_chars=0, truncated=False,
                                          excluded_items=kexcl)
    # 紧凑摘要落 profile_events（P1-1「哈希写进记录」）；**落库失败不得影响检索结果**。
    try:
        db.log_profile_event("ctx_manifest", "kb_search",
                             "degraded" if degraded else "ok",
                             int((time.monotonic() - t0) * 1000),
                             detail=ctxmanifest.summarize(manifest))
    except Exception:  # noqa: BLE001
        pass
    return {
        "results": fused,
        "count": len(fused),
        "engine": "+".join(engines) + "|rrf" if engines else "none",
        "backends": backends,
        "degraded": degraded,
        # 「count=0」到底是"真没有"还是"该路挂了"——这两件事必须能区分，
        # 否则这个门面就又造了一次静默不可用。
        "note": ("全部可用路都返回零命中" if not engines and not degraded else
                 None if not degraded else f"{degraded} 本次弃用，结果不完整"),
        "manifest": manifest,
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


async def _local_async(memory_mod, q: str, limit: int) -> Dict[str, Any]:
    """本地 SQLite 是同步的，放子线程跑，别把事件循环卡在 4ms 的库锁上。"""
    t0 = time.perf_counter()
    try:
        rows = await asyncio.to_thread(memory_mod._local_l1, q, limit)
        return {"ok": True, "items": rows, "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": None}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:200]}


async def _fed_async(sid: str, q: str, limit: int) -> Dict[str, Any]:
    """转调 memfed 的 rg 适配器（workspace/archived 两路，批3）。

    单源一调（不是把两源合一个 search_fed）：kb 的 backends 表态粒度是逐路一条，
    合并调用会把两路的 ok/error 糊到一起，违反「每路都能说清楚自己」的失败表态纪律。
    """
    res = await memfed.search_fed(q, limit, {sid})
    r = res.get(sid)
    if r is None:
        # 源被关（REGISTRY enabled=False）或 want 集合被清空：如实表态，不静默空过
        return {"ok": False, "items": [], "ms": None,
                "error": f"{sid} 未启用或无结果返回（查 memfed REGISTRY）"}
    return r


KB_BROWSE_MAX = int(os.getenv("KB_BROWSE_MAX", "200"))   # 顶层条目硬顶，防巨型目录把面板打爆


@router.get("/browse")
@runlog.track("kb.browse")
async def kb_browse(request: Request, sub: str = Query(default="", max_length=120)):
    """文档树浏览（批4 前端「知识库中心」右下栏）：workspace 四根的顶层条目。

    只读门面；根定义与 memfed._RG_TARGETS["workspace_files"] 同源（不另抄一份目录
    清单，防止两处漂移）。`sub` 只允许**单层相对名**（白名单根内防穿越），
    不支持 `/`/`..`/绝对路径——这不是防攻击，是防手滑跳出工作区读到无关目录。
    """
    roots = memfed._RG_TARGETS["workspace_files"][0]   # tuple(list_of_roots, glob)
    out: List[Dict[str, Any]] = []
    errs: List[str] = []
    if sub:
        if not re.fullmatch(r"[A-Za-z0-9_\-\u4e00-\u9fff]+", sub) or not sub.strip("."):
            raise HTTPException(400, "sub 只能是单层目录名（不带 / .. 绝对路径）")
        # sub 必须匹配某个根的名字（不是拼路径——那是穿越的口子），列的是根内部
        hit = [r for r in roots if os.path.isdir(r) and os.path.basename(r.rstrip("/")) == sub]
        if not hit:
            raise HTTPException(404, f"{sub!r} 不是知识库根（可用：{[os.path.basename(r.rstrip('/')) for r in roots if os.path.isdir(r)]}）")
        root = hit[0]
        try:
            for e in sorted(os.scandir(root), key=lambda x: (not x.is_dir(), x.name.lower()))[:KB_BROWSE_MAX]:
                out.append({"name": e.name, "dir": e.is_dir(),
                            "size": (e.stat().st_size if not e.is_dir() else None)})
        except OSError as e:
            errs.append(f"{sub}: {e}")
    else:
        for r in roots:
            if not os.path.exists(r):
                errs.append(f"根缺失: {r}")
                continue
            if os.path.isfile(r):
                out.append({"name": os.path.basename(r), "dir": False,
                            "size": os.path.getsize(r)})
                continue
            n_files = 0
            for _, _, fs in os.walk(r):
                n_files += len(fs)
            out.append({"name": os.path.basename(r) or r, "dir": True, "files": n_files})
    return {"entries": out, "count": len(out), "errors": errs or None}


@router.get("/status")
@runlog.track("kb.status")
async def kb_status(request: Request):
    """资产面板用：各路是否可用、索引多新、库有多大。全实测，不猜。"""
    xinfo = await xerj_info()

    async def _fed_status(sid: str) -> Dict[str, Any]:
        """workspace/archived 路的健康段：走 list_fed_sources（复用其 TTL 探测缓存，不重扫）。"""
        try:
            data = await memfed.list_fed_sources()
            for s in data.get("sources") or []:
                if s.get("id") == sid:
                    p = s.get("probe") or {}
                    return {"available": bool(p.get("ok")),
                            "count": p.get("count"), "ms": p.get("ms"),
                            "note": p.get("note"), "error": p.get("error")}
            return {"available": False, "count": None, "ms": None, "note": None,
                    "error": f"{sid} 不在联邦源注册表（被移除了？）"}
        except Exception as e:              # noqa: BLE001
            return {"available": False, "count": None, "ms": None, "note": None,
                    "error": f"{type(e).__name__}: {e}"[:160]}

    tdai = tdai_client.backend_status()
    # 本地记忆便签的 staleness（件 3）。**失败不拖垮整个状态端点**：逐路表态是本项目
    # 的立身口径（kb 四路联邦每一路都必须回 ok/error），一路炸了不许把其余路一起糊掉。
    try:
        mem = memstats.collect()
    except Exception as e:                            # noqa: BLE001
        mem = {"state": "unavailable", "rows": None,
               "reason": "%s: %s" % (type(e).__name__, str(e)[:160])}
    # 年龄不解析上游字符串：XERJ 用节点健康 + 各语料篇数代替（uilt_at 型无时区坑已在 2026-09-24 踩过）
    return {
        "xerj": xinfo,
        "turbovec": {"available": False, "retired": True,
                     "why": "2026-10-18 索引/模型删除；文档路已由 xerj 接替",
                     "index_path": TURBOVEC_INDEX},
        "tdai": tdai,
        "local_memory": {**mem, "role": "P0 基线便签（2026-09-06，4 条）；默认不参与检索，需显式 routes=local"},
        "workspace": await _fed_status("workspace_files"),
        "archived": await _fed_status("archived_sessions"),
        "wigolo": {"available": False,
                   "why": "P1 判定不接入：FTS 无 tokenize 子句、中文召回 2<LIKE 8、"
                          "LIKE 全表扫仅 9.2ms，且 agent-hub 主题命中 0 条。详见 src/kb.py 顶部"},
    }
