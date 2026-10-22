# -*- coding: utf-8 -*-
"""接单前置（Intake）：一次调用做四查，产出一份可复核的简报。

【它解决什么】本机四类"前期资料"分散在四个入口：记忆（`/api/memory/search`）、
知识库（`/api/kb/search`）、技能（`/api/skill/auto`）、项目参考库（`/api/ref/*`）。
agent 接到任务后**没有任何一个动作**要求它把这四样都过一遍 —— 于是常见形态是
「凭印象开工，出问题才回头翻」，而翻到的往往是早就写过的判例。
本模块只做组合与呈现：**不新增任何检索能力，也不替调用方下结论。**

【为什么组合在服务端，而不是写四个客户端脚本】四个入口的默认参数（记忆源集合、
检索面、技能命中门槛）都会各自演化；客户端各抄一份必然漂移，漂移的形态是
"同一个任务在两个工具链里看到不同的前置资料"。这里全部走**本进程已有的函数/端点**，
唯一真源仍在各自模块。

【口径（避免"看起来查过了"）】
1. 每一路都要自报 `count` 与 `degraded`/`backends`：**0 命中**与**那一路挂了**
   在简报里必须是两种写法，不能都写成"没有相关资料"。
2. 参考库默认**不查**：单次 21.8~56.8s 且吃 GitHub 额度，接单阶段多数任务不需要。
   传 `deep=true` 或任务命中触发词（新增/实现/选型/对标…）才查，且在简报里写明
   "为什么查了"（manual / trigger:<词>）。
3. 简报只给**线索**（片段 + 出处），不是结论。引用前必须回原文 —— 本机既有判例里
   已有多次"照着摘要写错"的事故。

【整理/分析/评估（2026-10-22 二次迭代）】
四条检索原样堆在一起有两个毛病：跨路重复（记忆的 `tdai_l1` 与知识库 `tdai` 是同一批条目）、
以及**弱命中与强命中同权呈现**。这里加一层纯词面的整理：

- **去重**：按正文归一化指纹跨路合并，后面那路记 `dup_of` 只留计数；
- **定级**：任务词元（CJK 二元组 + 拉丁词）在条目里的覆盖率 ⇒ `直接 / 相关 / 弱`，
  弱相关折叠进「噪声 N 条」而不再占屏；
- **提炼**：把带「必须/禁止/不要/一律/不得…」的命中抽成**开工前约束**，把知识库路径抽成
  **需要回读的原文**，其余归入可复用资产；
- **评估**：`verdict` 只有三档 —— `ready`（有可用线索）/ `thin`（四路都通但无线索）/
  `degraded`（有路挂了）。三档互斥，避免把"查到了但没用"说成"没资料"。

⚠️ 定级是**词面重合**，不是语义判断：`弱` 不等于"无关"，只是"没有共同用词"。
所以弱命中只是折叠、不丢弃（总数与 `noise` 计数都留在简报里）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
import urllib.parse
import urllib.request
from datetime import datetime, timezone
from typing import Any, Dict, List

from fastapi import APIRouter
from pydantic import BaseModel, Field

import refscout
import skill_store
import memory as memory_mod
from config import config as _cfg

log = logging.getLogger("agenthub.intake")
router = APIRouter(prefix="/api/intake", tags=["intake"])

#: 自指 URL（与 hubmcp.HUB_URL 同口径：从 config.port 推导，**不写死 3102**）。
SELF_URL = "http://127.0.0.1:%d" % _cfg.port
TIMEOUT_S = 12.0

#: 默认只查快路：`workspace`/`archived` 是逐文件 rg 全扫（实测 1.9~2.5s/路），
# 接单阶段把预算烧在这里，用户看到的就是"卡了几秒然后给了一堆不相关文件"。
FAST_KB_ROUTES = "tdai,xerj"

ROUTE_LABEL = {"memory": "记忆", "kb": "知识库", "skill": "技能", "ref": "参考库"}

#: 命中这些词 ⇒ 任务属于"要动手做/要比对"，参考库值一查。只做**词面**判定，
# 不调模型：接单这一步不该再引入一个不确定延迟。
DEEP_TRIGGERS = ("新增", "实现", "选型", "对比", "对标", "参考实现", "重构",
                 "架构", "设计", "迁移", "接入", "替换", "性能")

#: 做词面重合判定时剔除的虚词／高频字（只影响"相关度"标注，**不影响任何检索**）。
_STOP = set("的了是在和与及或就都而这那有为对把被从到会可要我们你他她它个些没不很更最")

#: XERJ 纯标题命中时正文为空，简报里用这个前缀显式标注。下游（抽约束）要按它排除。
_TITLE_ONLY = "（仅标题命中"

#: 命中即视为"开工前约束"的语气词。抽出来单列，是因为这些句子就是**执行判据**，
#: 混在十条摘要里最容易被跳过，而跳过的代价正是"照摘要写错"。
_RULE_WORDS = ("必须", "禁止", "不得", "不要", "一律", "只能", "先用", "先读", "先查",
               "务必", "严禁", "不许", "不能", "应当", "须先")

#: 覆盖率 → 定级阈值。**刻意宽松**：弱相关只折叠不丢弃，宁可多列也不假装精确。
REL_STRONG, REL_WEAK = 0.5, 0.25


def _tokens(text: str) -> set:
    """任务词元：CJK 二元组 + 长度 ≥3 的拉丁词/标识符（小写）。

    为什么用二元组：本机检索本身是词法的（BM25），用同一量纲估"这条命中到底沾不沾边"
    才是可比的；换成 embedding 会把"看起来相关"和"检索命中"混成一个数。
    """
    t = (text or "").lower()
    out = set(re.findall(r"[a-z0-9_][a-z0-9_.\-]{2,}", t))
    for run in re.findall(r"[\u4e00-\u9fff]+", t):
        for i in range(len(run) - 1):
            bg = run[i:i + 2]
            if bg[0] in _STOP or bg[1] in _STOP:
                continue
            out.add(bg)
    return out


def _coverage(text: str, toks: set) -> float:
    """任务词元在条目里的覆盖率；**分母封顶 6**。

    为什么封顶：长任务（十几二十个词元）会把 2~3 个真实命中稀释成 0.1，导致"明明沾边"
    被判成弱命中后折叠掉。分母封顶等价于"超过 6 个词元就不再要求全中"，
    与检索侧 BM25 的饱和直觉一致，且不需要语料统计。
    """
    if not toks:
        return 0.0
    low = (text or "").lower()
    hit = sum(1 for t in toks if t in low)
    return hit / min(len(toks), 6)


def _rel(text: str, toks: set) -> str:
    c = _coverage(text, toks)
    if c >= REL_STRONG:
        return "直接"
    if c >= REL_WEAK:
        return "相关"
    return "弱"


def _sig(text: str) -> str:
    """去重指纹：去掉全部非文字字符后取前 60 字。

    跨路重复的形态是**同一条正文**（tdai 在记忆与知识库各出一份），只是 source 不同；
    按"来源+路径"去重抓不到它，按正文才抓得到。
    """
    return re.sub(r"\W+", "", (text or ""))[:60]


#: YAML front matter。XERJ 的 `highlight` 常整段落在文件头（`--- type: concept title: …`），
# 既占屏又对"这条要不要读"零信息量，进简报前剥掉。
# ⚠️ 不能用「按行」的写法：`/api/kb/search` 返回的 `content` **换行已被压成空格**
# （实测 `--- type: concept ... --- # 标题 > 正文`），所以用 `\s` 代替换行。
_FM_RE = re.compile(r"\A\s*---\s+.*?\s---\s+", re.S)
_TAG_RE = re.compile(r"</?em>")


def _body(text: str) -> str:
    return _FM_RE.sub("", _TAG_RE.sub("", text or "")).strip()


def _flatten(task: str, res: Dict[str, Any]) -> List[Dict[str, Any]]:
    """把四路的命中摊平成同一种行：{route, text, rel, src, path, ...}，并按指纹去重。"""
    toks = _tokens(task)
    rows: List[Dict[str, Any]] = []
    seen: Dict[str, Dict[str, Any]] = {}
    for it in (res.get("memory") or {}).get("items") or []:
        text = _body(it.get("content") or "")
        rows.append({"route": "记忆", "text": text, "rel": _rel(text, toks),
                     "src": it.get("layer") or it.get("source") or "?",
                     "path": "", "dup_of": "", "cov": _coverage(text, toks)})
    for it in (res.get("kb") or {}).get("items") or []:
        text = _body(it.get("content") or "") or (it.get("title") or "")
        rows.append({"route": "知识库", "text": text, "rel": _rel(text, toks),
                     "src": it.get("source") or "?", "path": it.get("path") or "",
                     "title": it.get("title") or "", "dup_of": "",
                     "cov": _coverage(text, toks)})
    for r in rows:
        sig = _sig(r["text"])
        if not sig:
            continue
        if sig in seen:
            r["dup_of"] = f'{seen[sig]["route"]}:{seen[sig]["src"]}'
        else:
            seen[sig] = r
    return rows


def _constraints(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """把带强语气的命中抽成"开工前约束"。只收 `直接/相关` —— 弱命中里的语气词多半是噪声。

    按 `cov` 降序：同一条规则常被多份记忆分别记过（措辞不同、覆盖面不同），
    先把"与本次任务最贴"的那条排前，读第一行就能开工；不做近似合并 ——
    实测这几条两两 Jaccard 最高只有 0.275，硬合会误并真正不同的规则。
    """
    cand = [r for r in rows
            if not r["dup_of"] and r["rel"] != "弱"
            # 「（仅标题命中：X）」是 XERJ 无正文时的回退串，里面没有判据可抽——
            # 硬抽会得到「约束：必须备份」这种只有标题、没有"在哪、做什么"的空规则。
            and not r["text"].startswith(_TITLE_ONLY)
            and any(w in r["text"] for w in _RULE_WORDS)]
    cand.sort(key=lambda r: -float(r.get("cov") or 0))
    out: List[Dict[str, str]] = []
    for r in cand:
        out.append({"text": r["text"][:200], "src": f'{r["route"]}/{r["src"]}',
                    "path": r["path"], "cov": round(float(r.get("cov") or 0), 2)})
        if len(out) >= 5:
            break
    return out


def _must_read(rows: List[Dict[str, Any]]) -> List[Dict[str, str]]:
    """需要回读的原文：有路径、且不是弱命中。同一路径只留第一条（命中可能多段）。"""
    out: List[Dict[str, str]] = []
    seen = set()
    cand = [r for r in rows if r["path"] and not r["dup_of"] and r["rel"] != "弱"]
    cand.sort(key=lambda r: -float(r.get("cov") or 0))
    for r in cand:
        if r["path"] in seen:
            continue
        seen.add(r["path"])
        # XERJ 的 highlight 段常常是文件头 front matter（`--- type: concept t<em>i</em>tle:`），
        # 对"为什么要读"零信息量；有标题就用标题，没有才退回正文首段。
        why = (r.get("title") or r["text"] or "").strip()[:100]
        out.append({"path": r["path"], "why": why})
        if len(out) >= 4:
            break
    return out


def assess(task: str, res: Dict[str, Any]) -> Dict[str, Any]:
    """整理 + 评估：返回 `{verdict, coverage, rows, noise, dups, constraints, must_read, gaps}`。

    `verdict` 三档互斥：`degraded`（有路挂）> `thin`（四路都通但无线索）> `ready`。
    """
    rows = _flatten(task, res)
    live = [r for r in rows if not r["dup_of"]]
    useful = [r for r in live if r["rel"] in ("直接", "相关")]

    m, k = res.get("memory") or {}, res.get("kb") or {}
    s, rf = res.get("skill") or {}, res.get("ref") or {}
    ref_state = ("skipped" if res.get("deep_reason") == "off"
                 else "hit" if rf.get("ok") else "down")
    route_state = {
        "memory": "hit" if m.get("count") else ("down" if not m.get("ok") else "zero"),
        "kb": "hit" if k.get("count") else ("down" if not k.get("ok") else "zero"),
        "skill": "hit" if s.get("ok") else ("down" if s.get("error") else "zero"),
        "ref": ref_state,
    }
    downs = [n for n, v in route_state.items() if v == "down"]
    gaps: List[str] = []
    if downs:
        gaps.append("以下路**不可用**（不是 0 命中）：" + "、".join(ROUTE_LABEL[d] for d in downs))
    for name in ("memory", "kb"):
        if route_state[name] == "zero":
            gaps.append(ROUTE_LABEL[name] + " 0 命中")
    if not useful and not downs:
        gaps.append("四路都通但**没有一条与任务词面重合** ⇒ 需要换词重查或直接问用户")
    if not gaps:
        gaps.append("四路均通，且至少有一条可直接引用的线索")
    verdict = "degraded" if downs else ("ready" if useful else "thin")
    return {
        "task": task, "verdict": verdict, "routes": route_state,
        "rows": rows, "useful": len(useful), "noise": len(live) - len(useful),
        "dups": len(rows) - len(live),
        "constraints": _constraints(live), "must_read": _must_read(live), "gaps": gaps,
        "coverage": f'{sum(1 for v in route_state.values() if v != "skipped")}/4',
    }



class IntakeIn(BaseModel):
    task: str = Field(min_length=2, max_length=2000)
    deep: bool = False
    auto_deep: bool = True
    caller: str = Field(default="", max_length=64)
    mem_sources: str = Field(default=memory_mod.FED_FAST_SOURCES, max_length=400)
    kb_routes: str = Field(default=FAST_KB_ROUTES, max_length=200)
    ref_wait_s: int = Field(default=90, ge=0, le=180)
    lang: str = Field(default="", max_length=20)


def deep_reason(task: str, deep: bool, auto_deep: bool) -> str:
    """返回 `manual` / `trigger:<词>` / `off` —— 必须能写进简报。

    "查了但没说为什么查"会让下一次调用方以为这是默认行为，于是把它当常态成本。
    """
    if deep:
        return "manual"
    if auto_deep:
        for w in DEEP_TRIGGERS:
            if w in (task or ""):
                return f"trigger:{w}"
    return "off"


def _self_get(path: str, **params) -> Dict[str, Any]:
    """GET 本进程 REST；失败返回 {'error':...} 而不抛（与 hubmcp._get 同口径）。"""
    qs = urllib.parse.urlencode({k: v for k, v in params.items() if v not in (None, "")})
    url = f"{SELF_URL}{path}" + (f"?{qs}" if qs else "")
    try:
        req = urllib.request.Request(url, headers={"x-hub-channel": "intake"})
        with urllib.request.urlopen(req, timeout=TIMEOUT_S) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as e:  # noqa: BLE001 —— 单路失败不该打断整份简报
        return {"error": f"{type(e).__name__}: {e}", "path": path}


def _mem(task: str, sources: str, limit: int = 5) -> Dict[str, Any]:
    d = _self_get("/api/memory/search", q=task, limit=limit, sources=sources)
    if "error" in d:
        return {"ok": False, "count": 0, "items": [], "error": d["error"]}
    return {"ok": True, "count": int(d.get("count") or 0),
            "items": [{"layer": m.get("layer"), "source": m.get("source"),
                       "category": m.get("category") or m.get("type"),
                       "content": (m.get("content") or "")[:280]}
                      for m in (d.get("memories") or [])[:limit]],
            "degraded": d.get("degraded") or [], "took_ms": d.get("took_ms")}


def _kb(task: str, routes: str, k: int = 6) -> Dict[str, Any]:
    d = _self_get("/api/kb/search", q=task, k=k, routes=routes)
    if "error" in d:
        return {"ok": False, "count": 0, "items": [], "error": d["error"]}
    items = []
    for r in (d.get("results") or [])[:k]:
        # XERJ 的 content 来自 highlight：**只在正文字段命中时才有**，纯标题命中会是空串
        # （旧版简报因此打出「wiki/x.md：」这种空行）。退回标题并显式标注，别留空。
        body = (r.get("content") or r.get("preview") or "").strip()
        body = re.sub(r"</?em>", "", body)   # XERJ highlight 标签，不是内容
        title = (r.get("title") or "").strip()
        items.append({"source": r.get("source"), "path": r.get("path"), "title": title,
                      "category": r.get("category") or r.get("type"),
                      "content": body or (f"{_TITLE_ONLY}：{title}）" if title else "（无摘要）"),
                      "score": r.get("score")})
    return {"ok": True, "count": int(d.get("count") or 0), "items": items,
            "degraded": d.get("degraded") or [], "took_ms": d.get("took_ms")}


async def _skill(task: str, caller: str) -> Dict[str, Any]:
    """本地优先、**不装新技能**（`allow_install=False`）。

    接单阶段自动装包会把"查一下有没有现成技能"变成"往盘里写东西"，
    那是 `/auto` 的默认行为、不是接单该有的副作用。
    """
    try:
        got = await skill_store.acquire(task, caller=caller or "intake", task=task,
                                        allow_install=False, local_only=True)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    return {"ok": bool(got.get("ok")), "name": got.get("name"),
            "route": got.get("route"), "reason": got.get("reason"),
            "matched": got.get("matched"), "matched_in": got.get("matched_in"),
            "took_ms": got.get("took_ms"),
            "how_to_get": (f'hub_skill_invoke(name="{got.get("name")}")'
                           if got.get("ok") else
                           '未命中时换任务描述用词，或 hub_list_skills(...) 人工挑')}


def _ref(task: str, wait_s: int, lang: str) -> Dict[str, Any]:
    """提交参考库检索并按需等待；超时就把已有词法卡的结果交出去并**写明没等到重排**。"""
    if not refscout.token():
        return {"ok": False, "error": "GitHub token 不可用（设置页 / GITHUB_TOKEN / github.txt）"}
    try:
        job = refscout.submit(refscout.ScoutIn(module=task[:200], lang=lang,
                                               allow_paid=False, trigger="manual"))
    except Exception as e:  # noqa: BLE001 —— 含 HTTPException(503)
        return {"ok": False, "error": f"{type(e).__name__}: {e}"}
    job_id, card_id = job.get("job_id"), job.get("card_id")
    deadline = time.monotonic() + max(0, wait_s)
    status = "queued"
    while time.monotonic() < deadline:
        try:
            v = refscout.job_view(job_id)
        except Exception:  # noqa: BLE001
            break
        status = str(v.get("status") or "")
        if status in ("done", "failed"):
            break
        time.sleep(1.0)
    rows: List[Dict[str, Any]] = []
    try:
        card = refscout.card_view(card_id)
        for c in (card.get("candidates") or []):
            if c.get("bucket") != "impl":
                continue
            rows.append({"repo": c.get("repo"), "stars": c.get("stars"),
                         "func": c.get("func"), "impl": c.get("impl"),
                         "judge": c.get("judge"),
                         "gist": (c.get("readme_gist") or "")[:120]})
            if len(rows) >= 5:
                break
    except Exception:  # noqa: BLE001
        pass
    return {"ok": bool(rows), "job_id": job_id, "card_id": card_id, "status": status,
            "waited_s": wait_s, "rows": rows}


def brief_md(task: str, res: Dict[str, Any]) -> str:
    plan = res.get("plan") or assess(task, res)
    m, k, s, r = res["memory"], res["kb"], res["skill"], res["ref"]
    why = res["deep_reason"]
    verdict_cn = {"ready": "可用", "thin": "线索不足", "degraded": "有路不可用"}[plan["verdict"]]
    out = [f"# 接单前置简报：{task}", "",
           f"- as of：{res['as_of']}　用时：{res['elapsed_s']}s　调用方：{res['caller'] or '—'}",
           f"- 已查 {plan['coverage']} 路　判定 **{plan['verdict']}**（{verdict_cn}）"
           f"　可直接引用 {plan['useful']} 条／折叠弱命中 {plan['noise']} 条"
           f"／跨路重复 {plan['dups']} 条",
           "- ⚠️ 本简报只给**线索与出处**，不是结论；引用前回原文核对。", "",
           f"## 0. 开工前先看（约束 {len(plan['constraints'])} 条／回读 {len(plan['must_read'])} 篇）", ""]
    # 这里**列全**，后面各节不再重复同一句 —— 旧版 §0 只截前 3 条、§2 又列一遍，
    # 同一句话在简报里出现两次，读的人反而要先去对"这两处是不是一回事"。
    step: List[str] = []
    for c in plan["constraints"]:
        # 覆盖率分母封顶 6，故数值可 >1（长任务里命中词元多于 6 个）；对外按 1.00 封顶显示，
        # 排序仍用原始分值 —— 否则"最贴的那条"会和其它 1.00 并列，看不出优先级。
        step.append(f"- **约束**（{c['src']}·相关 {min(float(c['cov']), 1.0):.2f}）：{c['text']}")
    for x in plan["must_read"]:
        step.append(f"- **回读**：`{x['path']}` —— {x['why']}")
    if s.get("ok"):
        step.append(f"- **技能**：`{s.get('name')}` ⇒ {s.get('how_to_get')}")
    if r.get("ok") and r.get("rows"):
        x = r["rows"][0]
        step.append(f"- **参考实现**：[{x['repo']}](https://github.com/{x['repo']}) "
                    f"★{x['stars']}（卡 `{r['card_id']}`）")
    if not step and plan["useful"]:
        step.append(f"- 有 {plan['useful']} 条沾边命中，但都不构成约束、也没有可回读的路径"
                    " —— 明细见 §2")
    out += step or ["- ⚠️ **本次没有可直接照做的线索** —— 看 §4 的缺口，换词重查或直接问用户。"]
    out.append("")

    # ── §1 四路盘点：先把"哪一路是什么状态"摆清楚，再谈内容 ──
    def _cell(name: str) -> tuple:
        if name == "memory":
            st = "命中" if m.get("count") else ("不可用" if not m.get("ok") else "0 命中")
            note = m.get("error") or "、".join(m.get("degraded") or [])
            return st, m.get("count") or 0, note or "—"
        if name == "kb":
            st = "命中" if k.get("count") else ("不可用" if not k.get("ok") else "0 命中")
            note = k.get("error") or "、".join(k.get("degraded") or [])
            return st, k.get("count") or 0, note or "—"
        if name == "skill":
            st = "命中" if s.get("ok") else ("不可用" if s.get("error") else "未命中")
            return st, 1 if s.get("ok") else 0, (s.get("reason") or s.get("error") or "—")
        if why == "off":
            return "未查", 0, "deep=off"
        return ("命中" if r.get("ok") else "未完成"), len(r.get("rows") or []), why

    out += ["## 1. 四路盘点", "", "| 路 | 状态 | 命中 | 与任务沾边 | 备注 |",
            "|---|---|---|---|---|"]
    for key in ("memory", "kb", "skill", "ref"):
        st, n, note = _cell(key)
        useful = len([x for x in plan["rows"] if x["route"] ==
                      {"memory": "记忆", "kb": "知识库"}.get(key) and x["rel"] != "弱"])
        out.append(f"| {ROUTE_LABEL[key]} | {st} | {n} | {useful if key in ('memory', 'kb') else '—'} "
                   f"| {note} |")
    if not m.get("ok"):
        out.append(f"\n> 记忆：检索不可用（{m.get('error')}）—— 这不等于「本机没记过」。")
    if not k.get("ok"):
        out.append(f"\n> 知识库：检索不可用（{k.get('error')}）—— 同理。")
    if m.get("ok") and not m.get("count"):
        out.append("\n> 记忆 **0 命中**（不是「没有记忆」，是这一组源里没匹配；要扩源加 mem_sources）。")
    if k.get("ok") and not k.get("count"):
        out.append("\n> 知识库 **0 命中**（先看备注里的降级项，再下「没有资料」的结论）。")
    out.append("")

    out.append(f"## 2. 命中明细（按正文去重；弱相关 {plan['noise']} 条已折叠、"
               f"重复 {plan['dups']} 条只留计数）")
    for route, items in (("记忆", [x for x in plan["rows"] if x["route"] == "记忆"]),
                         ("知识库", [x for x in plan["rows"] if x["route"] == "知识库"])):
        shown = [x for x in items if not x["dup_of"] and x["rel"] != "弱"]
        out.append(f"### {route}：{len(items)} 条命中／{len(shown)} 条列出")
        for x in shown:
            head = f"{x['path']}［{x['rel']}］" if x["path"] else \
                   f"[{x.get('src')}·{x['rel']}]"
            out.append(f"- {head}：{x['text']}")
        if not shown:
            weak = [x for x in items if not x["dup_of"] and x["rel"] == "弱"]
            if weak:
                # 全折叠会让 thin 变成"看着像没查"。留一行原样标题，供判断要不要换词。
                out.append("- 弱命中（仅列标题，供换词参考）："
                           + "；".join((x.get("title") or x["text"])[:28] for x in weak[:6]))
            else:
                out.append("- （无可列条目）")
    out.append("")

    out.append("## 3. 技能与参考库")
    if s.get("error"):
        out.append(f"- 技能：⚠️ 检索不可用（{s['error']}）")
    elif not s.get("ok"):
        out.append(f"- 技能：未命中（{s.get('reason') or '—'}）；"
                   f"换任务描述用词，或 hub_list_skills(...) 人工挑")
    else:
        out.append(f"- 技能：`{s.get('name')}`（{s.get('route')}，"
                   f"命中 {s.get('matched')} @ {s.get('matched_in')}）⇒ {s.get('how_to_get')}")
    if why == "off":
        out.append("- 参考库：未查（deep=off；要查请传 deep=true）")
    elif not r.get("ok"):
        out.append(f"- 参考库：⚠️ 未完成（{why}／{r.get('error') or '无结果'}）")
    else:
        note = "" if r.get("status") == "done" else "（未等到重排，先给词法卡）"
        out.append(f"- 参考库：卡 `{r['card_id']}`　状态 {r.get('status')}"
                   f"　查询原因 {why}{note}")
        for x in r["rows"]:
            out.append(f"  - [{x['repo']}](https://github.com/{x['repo']}) ★{x['stars']} "
                       f"功能{x['func']}／实现{x['impl']} 判定{x.get('judge') or '—'}"
                       + (f"：{x['gist']}" if x.get("gist") else ""))
        out.append(f"  - 看全卡：`hub_ref_get(card_id=\"{r['card_id']}\")`")
    out.append("")

    out.append("## 4. 缺口与口径")
    out += [f"- {g}" for g in plan["gaps"]]
    out.append("- 定级是**词面重合**（CJK 二元组 + 拉丁词），不是语义判断；"
               "`弱` 只是「没有共同用词」，不代表无关。")
    out.append("- `count=0` 与「那一路挂了」是两件事：前者换词/扩源，后者去修服务。")
    return "\n".join(out) + "\n"


async def run(inp: IntakeIn) -> Dict[str, Any]:
    t0 = time.monotonic()
    task = inp.task.strip()
    why = deep_reason(task, inp.deep, inp.auto_deep)
    caller = (inp.caller or "").strip()

    async def guard(name: str, factory) -> Dict[str, Any]:
        """任何一路抛异常 ⇒ 降级成该路的 error 段，**不许整份简报 500**。

        工厂函数而不是现成协程：`_skill(...)` 这类在**调用时**就可能抛（参数/依赖问题），
        直接传协程会在 gather 之前就炸掉，等于「一路挂 = 全查不到」。
        """
        try:
            return await factory()
        except Exception as e:  # noqa: BLE001
            log.warning("intake: %s 失败 %s", name, e)
            return {"ok": False, "count": 0, "items": [],
                    "error": f"{type(e).__name__}: {e}"}

    mem, kb, skill = await asyncio.gather(
        guard("memory", lambda: asyncio.to_thread(_mem, task, inp.mem_sources)),
        guard("kb", lambda: asyncio.to_thread(_kb, task, inp.kb_routes)),
        guard("skill", lambda: _skill(task, caller)),
    )
    ref: Dict[str, Any] = {"ok": False, "error": "未查"}
    if why != "off":
        ref = await guard("ref", lambda: asyncio.to_thread(
            _ref, task, inp.ref_wait_s, inp.lang))
    res = {
        "task": task, "caller": caller, "deep_reason": why,
        "as_of": datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S+00:00"),
        "elapsed_s": round(time.monotonic() - t0, 1),
        "memory": mem, "kb": kb, "skill": skill, "ref": ref,
    }
    res["plan"] = assess(task, res)
    res["brief"] = brief_md(task, res)
    return res


@router.post("/task")
async def api_intake(body: IntakeIn):
    """四查 + 整理 + 评估 ⇒ 一份简报。

    返回 `memory/kb/skill/ref` 四段原始命中、`plan`（verdict/覆盖率/约束/回读清单/缺口）
    与 `brief`（markdown，可直接贴进对话）。
    """
    return await run(body)
