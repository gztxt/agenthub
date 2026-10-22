"""外部 `jev` 精排客户端 —— `/api/skill/relevant` 的可选增强层。

【定位：这一层永远可以是坏的】
BM25（`skill_relevance.py`）是同步内存打分，永远给得出结果；`jev` 是外部服务，可能
超时、可能 403、可能欠费。所以本模块的契约是：**失败必须可观测、可降级，且降级后
结果依然是完整的一份**，而不是抛异常把整个端点带崩。

【它不在模型路由里】`typesafe-ai` 技能文档已写明：Jev **不在** `~/.pi/agent/models.json`
**也不在** CCR 里，只能走直连端点。所以本模块自己拼 URL、自己带 key，不经任何网关。

【为什么不 shell 出去调 `jev.py`】`技术文档/scripts/jev.py` 是给人用的同步 CLI，它
`die()` 直接 `sys.exit`，接不进 async 服务；而且拉一个子进程只为发一个 POST 是不必要的。
这里照它的**请求契约**重写（见 `_build_payload`），不复制它的代码。

【熔断】连续失败 `MAX_CONSEC_FAIL` 次即在本进程内标记 down，此后直接跳过、不再发请求。
依据：`jev` 单次约 ¥0.0001，熔断只为省掉「明知不通还反复试」的等待，不是为了省钱。
"""
import asyncio
import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

import judge_cache
import tdai_client

log = logging.getLogger("hub.jev")

#: 与 `技术文档/scripts/jev.py:33` 同值；改这里之前先改那份，两边必须一致
JEV_URL = "https://api.typesafe.ai/v1/systemone"
#: 与 `scripts/jev.py:35` 同值（`jev-latest` 是请求侧 model；返回里的 model 是具体版本号）
JEV_MODEL = "jev-latest"

#: 比 CLI 的 60s 短得多：本层在请求路径上，且已经有 BM25 兜底，
#: 等一分钟只为拿一个可有可无的精排是错的
DEFAULT_TIMEOUT_S = 8.0
#: 同一 query 的精排结果复用窗口。精排是「同一个问题问同一批候选」，重问一遍结果几乎一样。
CACHE_TTL_S = float(os.getenv("JEV_CACHE_TTL", "600"))
MAX_CONSEC_FAIL = 2
#: 一次最多送多少个候选给 jev。问题数即 token 数，也即等待时间，不宜无界。
MAX_CANDIDATES = 20

#: 刻度锚点。实测 score 的 criteria **必须是字符串列表**（传 dict 会被 422 拒），
#: 返回里叫 `legend`，按索引对应。3 档而非 5 档：锚点越多模型越容易糊，收益递减。
SCALE = ["高度相关", "部分相关", "不相关"]

_S = {
    "consec_fail": 0,
    "down": False,
    "down_reason": None,
    "last_error": None,
    "last_ok_ts": None,
    "calls": 0,
    "cache": {},   # cache_key -> (ts, {name: {...}})
}


class JevUnavailable(RuntimeError):
    """`jev` 这次用不了。端点捕获它并退回 BM25，同时把 reason 如实回给调用方。"""


def reset_circuit() -> None:
    """只给测试用：把熔断与缓存清回初始态（L0 必须在无副作用状态下可重复跑）。"""
    _S["consec_fail"] = 0
    _S["down"] = False
    _S["down_reason"] = None
    _S["last_error"] = None
    _S["calls"] = 0
    _S["cache"] = {}


#: 区域封禁（HTTP 451）的短路窗口。实测 2026-10-22：本机直连 api.typesafe.ai 恒 451
#:（`{"title":"Typesafe is not available in your region.","status":451}`）。
#: 每次探活白等 1.1s，且 `status()` 只检查 key 是否存在 ⇒ 报 `enabled:true`（假绿）。
#: 所以：451 之后**在一段时间内直接短路**，并把原因写进 status。
BLOCKED_COOLDOWN_S = float(os.environ.get("JEV_BLOCKED_COOLDOWN_S", "900"))
_S_BLOCK: Dict[str, Any] = {"until": 0.0, "reason": ""}


def blocked_remaining_s() -> float:
    """451 短路剩余秒数（0 = 未短路）。"""
    left = float(_S_BLOCK.get("until") or 0.0) - time.monotonic()
    return max(0.0, round(left, 1))


def _mark_blocked(reason: str) -> None:
    _S_BLOCK["until"] = time.monotonic() + BLOCKED_COOLDOWN_S
    _S_BLOCK["reason"] = str(reason)[:160]


def status() -> Dict[str, Any]:
    """健康与熔断状态，供 `/api/skill/status` 回显。字段与 `kb.py:_backend` 同形。"""
    key = os.environ.get("TYPESAFE_API_KEY")
    return {
        "ok": bool(key) and not _S["down"],
        "enabled": bool(key),
        "down": bool(_S["down"]),
        "why": (None if (key and not _S["down"]) else
                ("未设置环境变量 TYPESAFE_API_KEY（真源 ~/.pi/agent/env.typesafe，禁触面，"
                 "本模块只读环境变量、不读那个文件）" if not key else
                 "已熔断：%s" % (_S["down_reason"] or "连续失败"))),
        "consec_fail": _S["consec_fail"],
        "calls": _S["calls"],
        "cached": len(_S["cache"]),
        "last_error": tdai_client.scrub(_S["last_error"])[:200] if _S["last_error"] else None,
        "last_ok_ts": _S["last_ok_ts"],
        "timeout_s": DEFAULT_TIMEOUT_S,
        "cache_ttl_s": CACHE_TTL_S,
        "disk_cache": judge_cache.stats().get("entries"),
        "model": JEV_MODEL,
        # 2026-10-22：区域封禁必须**可分辨**。只报 `enabled:true` 是假绿
        # （key 在、但每个请求都 451）；这三个字段让调用方一眼看出「不是 key 的问题」。
        "region_blocked": blocked_remaining_s() > 0,
        "blocked_remaining_s": blocked_remaining_s(),
        "blocked_reason": _S_BLOCK.get("reason") or None,
    }


def _record_failure(reason: str) -> None:
    """记一次失败并在连续 `MAX_CONSEC_FAIL` 次后熔断。

    单独抽出来是为了可测：熔断逻辑藏在网络 except 块里就只能靠真发请求来验，
    而 L0 不允许联网。调用方只管把原因交进来。
    """
    _S["consec_fail"] += 1
    _S["last_error"] = reason
    if _S["consec_fail"] >= MAX_CONSEC_FAIL:
        _S["down"] = True
        _S["down_reason"] = "连续 %d 次失败（最近一次：%s）" % (_S["consec_fail"], reason)


def _record_success() -> None:
    """成功一次就把连续失败计数清零，熔断不复位。

    注意：熔断一旦打开**不**由成功复位。要复位得重启进程——因为已经连续失败两次的
    端点，再给它一次机会的意思是让它继续拖慢每个请求。
    """
    _S["consec_fail"] = 0
    _S["last_error"] = None
    _S["last_ok_ts"] = time.time()


def _cache_key(query: str, names: List[str]) -> str:
    return json.dumps([query.strip(), names], ensure_ascii=False, sort_keys=True)


def _build_payload(query: str, candidates: List[Dict[str, Any]]) -> Dict[str, Any]:
    """按 `scripts/jev.py` 的契约拼请求：`{"state", "model", "questions"}`。

    每道题的 `instructions` **自带完整语义**（技能文档：题面里要包含完整意思，不要
    让模型去猜代词指谁）。所以「任务描述 + 候选技能名」都写进题面，`state` 只作背景。
    """
    state = {
        "任务描述": query,
        "候选技能": [{"名称": c.get("name") or "", "描述": str(c.get("description") or "")[:200]}
                     for c in candidates],
    }
    questions: Dict[str, Any] = {}
    for i, c in enumerate(candidates):
        name = str(c.get("name") or "")
        questions["s%d" % i] = {
            "type": "score",
            "instructions": "任务描述：%s\n判断候选技能「%s」对完成该任务的帮助程度。"
                            % (query.strip()[:400], name),
            "criteria": list(SCALE),
        }
    return {"state": state, "model": JEV_MODEL, "questions": questions}


def _parse(payload: Dict[str, Any], candidates: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """把 `{"answers":{"s0":{...}}}` 映射回候选技能名。

    - 按键名对齐，**不按顺序**：服务端没承诺返回顺序，按序取会静默串位。
    - 缺答案/类型不对的候选直接跳过（宁缺勿错），跳过数记进 `missing`。
    """
    answers = payload.get("answers")
    if not isinstance(answers, dict):
        raise JevUnavailable("响应缺少 answers 字典（keys=%s）" % list(payload)[:6])
    out: Dict[str, Dict[str, Any]] = {}
    for i, c in enumerate(candidates):
        a = answers.get("s%d" % i)
        if not isinstance(a, dict) or a.get("type") != "score":
            continue
        val = a.get("score")
        if not isinstance(val, (int, float)):
            continue
        out[str(c.get("name") or "")] = {
            "score": float(val),
            "confidence": a.get("confidence") if isinstance(a.get("confidence"), (int, float)) else None,
            "legend": a.get("legend") if isinstance(a.get("legend"), dict) else None,
        }
    if not out:
        raise JevUnavailable("answers 里没有可用的 score（收到 %d 条）" % len(answers))
    return out


async def score_candidates(query: str, candidates: List[Dict[str, Any]],
                           timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Dict[str, Any]]:
    """一次请求、每候选一道 score 题。返回 `{技能名: {score, confidence, legend}}`。

    失败一律抛 `JevUnavailable`——**本函数不吞异常**，由端点决定怎么降级。
    这里若静默返回 {}，调用方无法区分「jev 认为都不相关」与「jev 挂了」，那是要消灭的形态。
    """
    if not os.environ.get("TYPESAFE_API_KEY"):
        raise JevUnavailable("未设置 TYPESAFE_API_KEY")
    if blocked_remaining_s() > 0:
        raise JevUnavailable("区域封禁（HTTP 451）短路中，剩 %.0fs" % blocked_remaining_s())
    if _S["down"]:
        raise JevUnavailable("已熔断：%s" % (_S["down_reason"] or "连续失败"))
    cand = list(candidates)[:MAX_CANDIDATES]
    if not cand:
        raise JevUnavailable("没有候选技能")

    name_list = [str(c.get("name") or "") for c in cand]
    key = _cache_key(query, name_list)
    hit = _S["cache"].get(key)
    if hit and (time.time() - hit[0]) < CACHE_TTL_S:
        return dict(hit[1], cached=True)
    # 2026-10-22：**磁盘层**。内存层重启即丢（实测：进程一重启同一问题就重算），
    # 而 jev 判定结论很稳 ⇒ 按 `(query, 候选集指纹)` 落盘，TTL 24h 可调。
    # 注意：这里**不缓存失败**（451/超时/403）—— 缓存可用性问题会把「现在问不了」
    # 固化成「永远别问」，那是比重复花钱严重得多的错。
    dk = judge_cache.key("jev", query, judge_cache.fingerprint(cand, ("name", "description")))
    disk = judge_cache.get(dk)
    if disk is not None and isinstance(disk.get("scores"), dict):
        _S["cache"][key] = (time.time(), dict(disk["scores"]))
        return dict(disk["scores"], cached=True, cached_disk=True)

    import aiohttp  # 局部导入：本模块被 L0 导入时不该拖起 aiohttp 的 import 副作用

    body = _build_payload(query, cand)
    url, hdr = JEV_URL, {"Content-Type": "application/json",
                         "Authorization": "Bearer %s" % os.environ["TYPESAFE_API_KEY"]}
    try:
        async with aiohttp.ClientSession() as sess:
            async with sess.post(url, json=body, headers=hdr,
                                 timeout=aiohttp.ClientTimeout(total=timeout_s)) as r:
                if r.status == 451:
                    # 区域封禁：与 403（key 没生效）**不是同一件事**，不能混报
                    body = (await r.text())[:200]
                    _mark_blocked("HTTP 451 区域封禁：%s" % body)
                    _record_failure("451 区域封禁")
                    raise JevUnavailable("区域封禁（HTTP 451），%d 秒内不再尝试"
                                         % int(BLOCKED_COOLDOWN_S))
                if r.status == 403:
                    # 与 scripts/jev.py:158-160 同口径：403 是 key 没生效，不是代码问题
                    raise JevUnavailable("HTTP 403 —— key 没生效，检查 TYPESAFE_API_KEY 是否已 source")
                if r.status != 200:
                    raise JevUnavailable("HTTP %s（%s）" % (r.status, (await r.text())[:120]))
                payload = await r.json(content_type=None)
    except JevUnavailable as e:
        _record_failure(str(e))
        raise
    except asyncio.TimeoutError:
        _record_failure("超时 >%.1fs" % timeout_s)
        raise JevUnavailable("超时 >%.1fs" % timeout_s)
    except Exception as e:  # 网络层什么都可能抛：DNS、SSL、连接重置
        _record_failure("%s: %s" % (type(e).__name__, e))
        raise JevUnavailable("%s: %s" % (type(e).__name__, str(e)[:120]))

    parsed = _parse(payload, cand)
    _record_success()
    _S["calls"] += 1
    _S["cache"][key] = (time.time(), parsed)
    return parsed
