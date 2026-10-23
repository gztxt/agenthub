# -*- coding: utf-8 -*-
"""远端判定后端：DeepSeek-Flash（CCR `deepseek/deepseek-flash`），2026-10-23。

## 为什么是它

本机 Ollama 与 `jev-decision` 技能已于 2026-10-23 删除，判定能力归零。对照实测
（`agent-knowledge/250`、`work/judge-paid-20261023/REPORT.md`，同一 16 项中文 +
4 项英文 noul）：

| 后端 | noul CN/EN | choice 两序一致 | 摘要命中 | 单次均值 |
|---|---|---|---|---|
| `deepseek/deepseek-flash` | 4/4 · 4/4 | 4/4 | 7/10 | **3.3s** |
| `nvidia/nemotron-3-ultra:free` | 4/4 · 4/4 | 4/4 | 5/10 | 11.5s |
| `deepseek/deepseek-v4-pro` | 4/4 · 4/4 | 4/4 | 7/10 | 7.3s |
| 本机 qwen2.5:1.5b | 2/4 · 2/4 | 1/4 | 3/10 | 6.4s |

**质量在远端三档之间已饱和，唯一有区分度的是延迟**：flash 5 题 ≈17s，装得进
`vitals` 的 180s / `/relevant` 的 45s；免费档同质量但 5 题 ≈58s，且路由档会
把请求换成审核模型（实测 8 次中 2 次 `nemotron-3.5-content-safety`）。故钉死
**具体模型**，不用路由器 `openrouter/free`（`AGENTS.md` 军规：会话/门禁用具体
模型，路由只用于非会话后台）。

## 契约（与 `local_judge` 的 `judge()` 完全一致）

`judge(questions, state, timeout_s)` → `{"answers", "model", "usage"}`；
`questions` 的每项是 `{"type": noul|score|choice, "instructions", "criteria"}`。
**任何失败都抛 `RemoteJudgeUnavailable`（带 `kind`）**，不返回空答案——
「判完了」与「没判成」必须可区分（判例 242 §1：`enabled:true` 假绿三年）。

## 三个刻意的取舍

1. **prompt 与解析照抄已测版本**（`zerocost_jev._ask` 的三条），只换 HTTP 出口。
   改一个字都可能让上面那张表作废，所以另写一套 prompt 的收益为零。
2. **不并发**：一次最多 5 题串行，3.3s×5≈17s，远低于超时；并发争的是网关配额，
   而且失败时更难归因。`MAX_PARALLEL` 留作以后按需开。
3. **截断即失败**：`finish_reason=="length"` 说明 JSON 被切断，此时「解析出来
   半个对象」比解析失败更危险（概率字段缺失会被 `_clamp01` 补成 0，读起来像
   「模型判 0」）。所以长度截断直接抛。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request
from typing import Any, Dict, List, Optional, Tuple

#: 与 `llm.py` 共用同一个网关与凭据（`.env` 的 MANAGER_LLM_*），零新增凭据。
BASE_URL = os.getenv("MANAGER_LLM_BASE_URL", "http://127.0.0.1:3456").rstrip("/")
API_KEY = os.getenv("MANAGER_LLM_API_KEY", "")

#: 钉死具体模型。经 CCR 需 provider 前缀；禁写 `openrouter/auto`（会自动选最便宜的
#: 可用模型，最便宜可能是付费档）。
MODEL = os.getenv("JUDGE_MODEL", "deepseek/deepseek-flash")

#: 单题硬超时（秒）。实测均值 3.3s、长尾 11s；30s 给冷启动与排队留余量，
#: 又能在 5 题 × 30s 内被 vitals 的 180s 兜住。
TIMEOUT_S = float(os.getenv("JUDGE_TIMEOUT", "30"))

#: 单题**判定**输出上限。判定只要一个 ~100 字 JSON；给 1200 是防 reasoning 模型
#: 把思考过程也吐出来（`deepseek-flash` 会带 `reasoning_content`）。
MAX_TOKENS = int(os.getenv("JUDGE_MAX_TOKENS", "1200"))

#: 通用文本生成（摘要 / 长文）的上限。与 PT-08 对照口径一致（4000）——
#: 实测 1200 会把中文摘要截断，`finish_reason=length` 时调用方按失败处理。
GEN_MAX_TOKENS = int(os.getenv("JUDGE_GEN_MAX_TOKENS", "4000"))

_JSON_HINT = "只输出一个 JSON 对象，不要输出任何其他文字、不要用 markdown 代码围栏。"


class RemoteJudgeUnavailable(RuntimeError):
    """这次判定没跑成。`kind` 用于告警分桶（http/timeout/parse/truncated/empty）。"""

    def __init__(self, msg: str, kind: str = "remote"):
        super().__init__(msg)
        self.kind = kind


def configured() -> bool:
    """网关地址非空即视为「配置在位」。**不代表可用**——可用看真调用后的状态。"""
    return bool(BASE_URL)


def _post(body: Dict[str, Any], timeout_s: float) -> Dict[str, Any]:
    """打一次 CCR（独立成函数，作为 L0 测试的替身接缝）。"""
    headers = {"Content-Type": "application/json"}
    if API_KEY:
        headers["Authorization"] = "Bearer " + API_KEY
    req = urllib.request.Request(BASE_URL + "/v1/chat/completions",
                                 data=json.dumps(body, ensure_ascii=False).encode("utf-8"),
                                 headers=headers)
    with urllib.request.urlopen(req, timeout=max(1.0, timeout_s)) as resp:
        return json.loads(resp.read().decode("utf-8"))


def generate(prompt: str, timeout_s: float = TIMEOUT_S,
             max_tokens: Optional[int] = None) -> Tuple[str, Dict[str, Any]]:
    """纯文本生成（`chain_judge` 的 classify / pick、摘要链复用）。返回 `(text, usage)`。

    `max_tokens` 缺省用 `GEN_MAX_TOKENS`（4000，与对照口径一致）；**判定**必须显式
    传小的 `MAX_TOKENS`，否则 reasoning 模型的长输出会白烧配额。
    """
    body = {
        "model": MODEL, "stream": False, "temperature": 0.1,
        "max_tokens": int(max_tokens or GEN_MAX_TOKENS),
        "messages": [{"role": "user", "content": prompt}],
    }
    t0 = time.monotonic()
    try:
        data = _post(body, timeout_s)
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = e.read().decode("utf-8", "replace")[:300]
        except Exception:  # noqa: BLE001 —— 读不到正文不该改变错误性质
            pass
        raise RemoteJudgeUnavailable("网关 HTTP %s: %s" % (e.code, detail), kind="http")
    except (urllib.error.URLError, TimeoutError, OSError) as e:
        raise RemoteJudgeUnavailable("网关不可达/超时: %s: %s"
                                     % (type(e).__name__, str(e)[:160]), kind="timeout")
    ms = int((time.monotonic() - t0) * 1000)
    choices = data.get("choices") or [{}]
    ch = choices[0] or {}
    msg = ch.get("message") or {}
    text = (msg.get("content") or "").strip()
    if not text and (msg.get("reasoning_content") or "").strip():
        # 极少数情况下正文落在 reasoning 通道；宁可多这一次兜底，
        # 也不要把它算成「模型没答」（口径与 `bench_or.py` 一致）。
        text = str(msg["reasoning_content"]).strip()
    usage = {
        "prompt_tokens": (data.get("usage") or {}).get("prompt_tokens") or 0,
        "completion_tokens": (data.get("usage") or {}).get("completion_tokens") or 0,
        "ms": ms,
        "upstream_model": data.get("model"),
        "cost": (data.get("usage") or {}).get("cost"),
        "finish": ch.get("finish_reason"),
    }
    if ch.get("finish_reason") == "length":
        raise RemoteJudgeUnavailable(
            "输出被 max_tokens 截断（completion_tokens=%s），JSON 不可信"
            % usage["completion_tokens"], kind="truncated")
    if not text:
        raise RemoteJudgeUnavailable("上游返回 200 但正文为空（finish=%s）"
                                     % ch.get("finish_reason"), kind="empty")
    return text, usage


def _extract_json(text: str) -> Optional[Dict[str, Any]]:
    """从模型输出里抠 JSON 对象：剥围栏 → 直接 loads → 正则捞首个 {...}。"""
    t = (text or "").strip()
    t = re.sub(r"^```(?:json)?\s*|\s*```$", "", t, flags=re.MULTILINE).strip()
    try:
        obj = json.loads(t)
        return obj if isinstance(obj, dict) else None
    except json.JSONDecodeError:
        pass
    m = re.search(r"\{.*\}", t, flags=re.DOTALL)
    if not m:
        return None
    try:
        obj = json.loads(m.group(0))
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) else None


def _clamp01(x: Any) -> float:
    try:
        return max(0.0, min(1.0, float(x)))
    except (TypeError, ValueError):
        return 0.0


def _normalize_choices(criteria: Any) -> Dict[str, Any]:
    """choice 选项规整：dict 原样、list/tuple 转 `{项: 项}`、其它给空（由调用方报错）。"""
    if isinstance(criteria, dict):
        return dict(criteria)
    if isinstance(criteria, (list, tuple)):
        return {str(x): str(x) for x in criteria}
    return {}


def ask_one(question: Dict[str, Any], state: str = "",
            timeout_s: float = TIMEOUT_S) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """单题判定。返回 `(answer, usage)`；失败抛 `RemoteJudgeUnavailable`。"""
    qtype = str(question.get("type") or "").strip()
    instr = str(question.get("instructions") or "").strip()
    if not instr:
        raise RemoteJudgeUnavailable("题面 instructions 为空", kind="bad_question")
    state_txt = state or "（未提供）"

    if qtype == "noul":
        prompt = (
            "你是结构化决策引擎，执行「是否」判定（Noul）。\n"
            "当前状态：%s\n"
            "判定问题：%s\n"
            "%s\n"
            '格式：{"p": 0到1的概率, "confidence": 0到1, "reason": "不超过40字"}'
            % (state_txt, instr, _JSON_HINT))
        text, usage = generate(prompt, timeout_s, max_tokens=MAX_TOKENS)
        obj = _extract_json(text)
        if obj is None:
            raise RemoteJudgeUnavailable("noul 输出不可解析：%s" % text[:120], kind="parse")
        return ({"type": "noul", "noul": _clamp01(obj.get("p")),
                 "confidence": _clamp01(obj.get("confidence")),
                 "reasoning": str(obj.get("reason", ""))[:200]}, usage)

    if qtype == "score":
        scale = question.get("criteria") or ["低", "中", "高"]
        n = max(1, len(scale) - 1)
        prompt = (
            "你是结构化决策引擎，执行刻度评分（Score）。\n"
            "当前状态：%s\n"
            "评分对象：%s\n"
            "刻度（从低到高）：%s\n"
            "%s\n"
            '格式：{"score": 0到1的归一化分值(0=最低档,%d=最高档), '
            '"label": "最匹配的刻度名", "confidence": 0到1, "reason": "不超过40字"}'
            % (state_txt, instr, json.dumps(scale, ensure_ascii=False), _JSON_HINT, n))
        text, usage = generate(prompt, timeout_s, max_tokens=MAX_TOKENS)
        obj = _extract_json(text)
        if obj is None:
            raise RemoteJudgeUnavailable("score 输出不可解析：%s" % text[:120], kind="parse")
        ans = {"type": "score", "score": _clamp01(obj.get("score")),
               "confidence": _clamp01(obj.get("confidence")),
               "reasoning": str(obj.get("reason", ""))[:200]}
        if obj.get("label"):
            ans["label"] = str(obj["label"])[:40]
        return ans, usage

    if qtype == "choice":
        crit = _normalize_choices(question.get("criteria"))
        keys = list(crit.keys())
        if not keys:
            # 题面缺选项时问模型只会得到像模像样的假答案 —— 必须显式报错。
            raise RemoteJudgeUnavailable("choice 题缺少选项：criteria 不能为空",
                                         kind="bad_question")
        lines = "\n".join("  %s: %s" % (k, crit[k]) for k in keys)
        example = "{" + ", ".join('"%s": 概率' % k for k in keys) + "}"
        prompt = (
            "你是结构化决策引擎，执行选项选择（Choice）。\n"
            "当前状态：%s\n"
            "决策问题：%s\n"
            "可选动作：\n%s\n"
            "%s\n"
            '格式：{"probabilities": ' % (state_txt, instr, lines, _JSON_HINT)
            + example + ', "confidence": 0到1, "reason": "不超过40字"}')
        text, usage = generate(prompt, timeout_s, max_tokens=MAX_TOKENS)
        obj = _extract_json(text)
        if obj is None:
            raise RemoteJudgeUnavailable("choice 输出不可解析：%s" % text[:120], kind="parse")
        raw = obj.get("probabilities") or {}
        probs = {k: _clamp01(raw.get(k)) for k in keys}
        total = sum(probs.values()) or 1.0
        probs = {k: round(v / total, 4) for k, v in probs.items()}
        chosen = max(probs, key=probs.get) if any(probs.values()) else None
        return ({"type": "choice", "probabilities": probs, "choice": chosen,
                 "confidence": _clamp01(obj.get("confidence")),
                 "reasoning": str(obj.get("reason", ""))[:200]}, usage)

    raise RemoteJudgeUnavailable("未知题型 %r（只支持 noul/score/choice）" % qtype,
                                 kind="bad_question")


def judge(questions: Dict[str, Any], state: str = "",
          timeout_s: float = TIMEOUT_S) -> Dict[str, Any]:
    """一轮多题判定（串行）。任**一题**失败即整轮抛 —— 缺一题的答案集不能当成功。"""
    if not isinstance(questions, dict) or not questions:
        raise RemoteJudgeUnavailable("没有问题可判（questions 为空）", kind="bad_question")
    answers: Dict[str, Any] = {}
    usage = {"prompt_tokens": 0, "completion_tokens": 0, "ms": 0, "calls": 0,
             "upstream_model": None, "cost": None}
    t0 = time.monotonic()
    for name, q in questions.items():
        if not isinstance(q, dict):
            raise RemoteJudgeUnavailable("题 %r 不是对象" % name, kind="bad_question")
        ans, u = ask_one(q, state, timeout_s)
        answers[name] = ans
        usage["prompt_tokens"] += int(u.get("prompt_tokens") or 0)
        usage["completion_tokens"] += int(u.get("completion_tokens") or 0)
        usage["ms"] += int(u.get("ms") or 0)
        usage["calls"] += 1
        usage["upstream_model"] = usage["upstream_model"] or u.get("upstream_model")
        if u.get("cost") is not None:
            usage["cost"] = (usage["cost"] or 0) + float(u["cost"])
    usage["elapsed_ms"] = int((time.monotonic() - t0) * 1000)
    return {"answers": answers, "model": MODEL, "usage": usage}


def status() -> Dict[str, Any]:
    """只读配置面（不联网）。真可用性看 `local_judge.status()` 的 last_ok/last_error。"""
    return {"backend": "remote", "configured": configured(), "base_url": BASE_URL,
            "model": MODEL, "timeout_s": TIMEOUT_S,
            "max_tokens": MAX_TOKENS, "gen_max_tokens": GEN_MAX_TOKENS,
            "auth": bool(API_KEY)}
