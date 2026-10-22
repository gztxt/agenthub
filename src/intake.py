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
"""
from __future__ import annotations

import asyncio
import json
import logging
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

#: 命中这些词 ⇒ 任务属于"要动手做/要比对"，参考库值一查。只做**词面**判定，
# 不调模型：接单这一步不该再引入一个不确定延迟。
DEEP_TRIGGERS = ("新增", "实现", "选型", "对比", "对标", "参考实现", "重构",
                 "架构", "设计", "迁移", "接入", "替换", "性能")


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
    return {"ok": True, "count": int(d.get("count") or 0),
            "items": [{"source": r.get("source"), "path": r.get("path"),
                       "category": r.get("category") or r.get("type"),
                       "content": (r.get("content") or r.get("preview") or "")[:280],
                       "score": r.get("score")}
                      for r in (d.get("results") or [])[:k]],
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
    out = [f"# 接单前置简报：{task}", "",
           f"- as of：{res['as_of']}　用时：{res['elapsed_s']}s　调用方：{res['caller'] or '—'}",
           "- ⚠️ 本简报只给**线索与出处**，不是结论；引用前回原文核对。", ""]
    m = res["memory"]
    if not m.get("ok"):
        out.append(f"## 1. 记忆：⚠️ 检索不可用（{m.get('error')}）")
    else:
        deg = f"　降级：{'、'.join(m['degraded'])}" if m.get("degraded") else ""
        out.append(f"## 1. 记忆：{m['count']} 条{deg}")
        if not m["count"]:
            out.append("- **0 命中**（不是「没有记忆」，而是这一组源里没匹配；"
                       "要扩大范围请加 mem_sources）")
        for it in m["items"]:
            out.append(f"- [{it.get('layer') or '?'}/{it.get('source') or '?'}] "
                       f"{it['content']}")
    out.append("")
    k = res["kb"]
    if not k.get("ok"):
        out.append(f"## 2. 知识库：⚠️ 检索不可用（{k.get('error')}）")
    else:
        deg = f"　降级：{'、'.join(k['degraded'])}" if k.get("degraded") else ""
        out.append(f"## 2. 知识库：{k['count']} 条{deg}")
        if not k["count"]:
            out.append("- **0 命中**（同理，先看 degraded 再下「没有资料」的结论）")
        for it in k["items"]:
            src = it.get("path") or it.get("source") or "?"
            out.append(f"- {src}：{it['content']}")
    out.append("")
    s = res["skill"]
    if s.get("error"):
        out.append(f"## 3. 技能：⚠️ 检索不可用（{s['error']}）")
    elif not s.get("ok"):
        out.append(f"## 3. 技能：未命中（{s.get('reason') or s.get('error') or '—'}）")
    else:
        out.append(f"## 3. 技能：`{s.get('name')}`（{s.get('route')}，"
                   f"命中 {s.get('matched')} @ {s.get('matched_in')}）")
        out.append(f"- 取正文：{s.get('how_to_get')}")
    out.append("")
    r = res["ref"]
    why = res["deep_reason"]
    if why == "off":
        out.append("## 4. 参考库：未查（deep=off；要查请传 deep=true）")
    elif not r.get("ok"):
        out.append(f"## 4. 参考库：⚠️ 未完成（{why}／{r.get('error') or '无结果'}）")
    else:
        note = "" if r.get("status") == "done" else "（未等到重排，先给词法卡）"
        out.append(f"## 4. 参考库：卡 `{r['card_id']}`　状态 {r.get('status')}"
                   f"　查询原因 {why}{note}")
        for x in r["rows"]:
            out.append(f"- [{x['repo']}](https://github.com/{x['repo']}) ★{x['stars']} "
                       f"功能{x['func']}／实现{x['impl']} 判定{x.get('judge') or '—'}"
                       + (f"：{x['gist']}" if x.get("gist") else ""))
        out.append(f"- 看全卡：`hub_ref_get(card_id=\"{r['card_id']}\")`")
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
    res["brief"] = brief_md(task, res)
    return res


@router.post("/task")
async def api_intake(body: IntakeIn):
    """四查一简报。返回结构化四段 + `brief`（markdown，可直接贴进对话）。"""
    return await run(body)
