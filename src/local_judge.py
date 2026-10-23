# -*- coding: utf-8 -*-
"""本地模型判定（2026-10-23，替代已下线的外部 jev）。

【来由】
外部端点 `api.typesafe.ai` 先被区域封禁（HTTP 451，2026-10-22 实测），随后整体下线。
用户裁定「判定现在使用本地模型判定」；记录层清理与本地判定技能的落地已在
2026-10-23 01:1x~01:4x 完成（判例 `agent-knowledge/244` §5.2），本模块补上**代码层**：
判定改由本机 Ollama（`qwen2.5:1.5b`，CPU、零 API key、零外网）承担，入口是技能
`@user_36623aa7/jev-decision` 的 `scripts/decision.py` —— stdin 进 JSON、
stdout 出 JSON、stderr 出人话。

【为什么走子进程而不是 import】
技能是**用户侧安装的代码**（技能商店安装、本机适配过），hub 只做调用方：
子进程隔离它的异常与依赖，hub 进程不因技能内部错误而崩；代价是每次判定多一次
python 启动（~150ms），而判定本身 1~5s，占比可忽略。
技能目录可用 `JEV_DECISION_SCRIPT` 覆盖（测试与影子实例靠它指向假脚本）。

【契约】
`judge(questions, state)` → `{"answers": {name: {...}}, "model", "usage"}`。
`questions` 的每个值是 `{"type": noul|choice|score, "instructions", "criteria"}`
（与外部 jev 时代的题面契约一致，vitals 的 QUESTIONS 原样可用）。
任何失败（脚本缺失 / Ollama 不在 / 超时 / 输出不可解析）一律抛
`LocalJudgeUnavailable` —— 本模块**不吞异常**：调用方必须能区分
「判定完了」与「这次没判成」；把后者静默当成前者，正是外部 jev
`enabled:true` 假绿三年的成因（判例 242 §1）。

【不缓存】
判定缓存在调用方：vitals 有 `by_sha` 证据缓存，skill 侧有 `judge_cache`。
本模块只负责「跑一次、解析、如实报」—— 同一结论两套 TTL 必然打架。
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

#: 单轮判定的硬超时（秒）。一道题 = 一次 CPU 推理，实测单题 1~3s；
#: vitals 一次送 5 题，/relevant 一次 1 题，都由调用方按题量传更小的值。
DEFAULT_TIMEOUT_S = float(os.getenv("JEV_DECISION_TIMEOUT", "60"))

#: 默认技能脚本位置：`<技术文档>/skills/@user_36623aa7/jev-decision/scripts/decision.py`。
#: src → agenthub → 技术文档，与 hub 的 techdocs 技能根（`skill.py:_DEFAULT_DIRS`）同源。
DEFAULT_SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "@user_36623aa7"
                  / "jev-decision" / "scripts" / "decision.py")

_LOCK = threading.Lock()
_STAT: Dict[str, Any] = {"calls": 0, "ok": 0, "fail": 0, "ms_sum": 0, "ms_max": 0,
                         "last_error": None, "last_model": None, "last_ok_ts": None}


class LocalJudgeUnavailable(RuntimeError):
    """这次判定没跑成。调用方按既有口径降级（vitals→硬规则；BM25 仍是检索权威）。"""


def script_path() -> Path:
    """判定脚本文本位置（env 可覆盖，供测试/影子实例指向假脚本）。"""
    return Path(os.getenv("JEV_DECISION_SCRIPT") or DEFAULT_SCRIPT)


def status() -> Dict[str, Any]:
    """只读状态，供 `/api/skill/status`、`/api/vitals`、`/health` 回显。

    `ready` 只检查**脚本在不在**（纯 stat，不发请求、不等模型）——
    「enabled ≠ 可用」那条判据（242 §1）在这里缩窄成最小可判面；
    模型能不能答，看 `last_error` / `last_ok_ts` 这些**真调用过**的字段。
    """
    p = script_path()
    with _LOCK:
        st = dict(_STAT)
    calls, ok = int(st.get("calls") or 0), int(st.get("ok") or 0)
    return {
        "backend": "ollama-local",
        "ready": p.is_file(),
        "script": str(p),
        "model": st.get("last_model"),
        "calls": calls,
        "ok": ok,
        "fail": int(st.get("fail") or 0),
        "ok_rate": round(ok / calls, 4) if calls else None,
        "ms_avg": int((st.get("ms_sum") or 0) / calls) if calls else None,
        "ms_max": int(st.get("ms_max") or 0),
        "last_error": st.get("last_error"),
        "last_ok_ts": st.get("last_ok_ts"),
        "note": ("判定后端=本机 Ollama（jev-decision 技能）；外部 jev 端点已于 2026-10-23 "
                 "下线删除，key/区域封禁那套判据随之作废"),
    }


def _bump(ok: bool, ms: int, err: Optional[str] = None, model: Optional[str] = None) -> None:
    with _LOCK:
        _STAT["calls"] += 1
        _STAT["ok" if ok else "fail"] += 1
        _STAT["ms_sum"] += int(ms)
        _STAT["ms_max"] = max(int(_STAT["ms_max"] or 0), int(ms))
        if err:
            _STAT["last_error"] = str(err)[:200]
        if ok:
            _STAT["last_ok_ts"] = time.time()
            if model:
                _STAT["last_model"] = str(model)[:60]


def judge(questions: Dict[str, Any], state: str = "",
          timeout_s: float = DEFAULT_TIMEOUT_S) -> Dict[str, Any]:
    """跑一轮本地判定。失败抛 `LocalJudgeUnavailable`，**不吞、不返回空**。

    返回 `{"answers": {...}, "model": ..., "usage": ...}`——
    `answers` 的每个键对应 `questions` 的同名键（服务端没承诺顺序时按名取，
    不按序取，防静默串位；缺键由调用方按「没判成」处理）。
    """
    if not isinstance(questions, dict) or not questions:
        raise LocalJudgeUnavailable("没有问题可判（questions 为空）")
    script = script_path()
    if not script.is_file():
        raise LocalJudgeUnavailable("判定脚本不在：%s（jev-decision 技能未安装？）" % script)
    payload = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
    t0 = time.monotonic()
    try:
        r = subprocess.run(  # noqa: S603 — 固定 argv，无 shell；脚本路径来自配置/默认值
            [sys.executable, str(script), "full"],
            input=payload, capture_output=True, text=True, timeout=max(1.0, timeout_s))
    except subprocess.TimeoutExpired:
        _bump(False, int((time.monotonic() - t0) * 1000), "超时 >%.1fs" % timeout_s)
        raise LocalJudgeUnavailable("本地判定超时 >%.1fs（CPU 推理，Ollama 可能在排队）"
                                    % timeout_s)
    except Exception as e:  # noqa: BLE001 —— 启动失败（权限/解释器不存在）同样是不可用
        _bump(False, int((time.monotonic() - t0) * 1000), "%s: %s" % (type(e).__name__, e))
        raise LocalJudgeUnavailable("判定进程启动失败：%s: %s" % (type(e).__name__, str(e)[:120]))
    ms = int((time.monotonic() - t0) * 1000)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["(无 stderr)"]
        _bump(False, ms, "rc=%d %s" % (r.returncode, tail[0]))
        raise LocalJudgeUnavailable("判定脚本退出 %d：%s" % (r.returncode, tail[0][:160]))
    try:
        out = json.loads((r.stdout or "").strip() or "{}")
    except ValueError as e:
        _bump(False, ms, "stdout 非 JSON：%s" % e)
        raise LocalJudgeUnavailable("判定输出不是 JSON（%s）；stdout 头部 %r"
                                    % (e, (r.stdout or "")[:80]))
    if not isinstance(out, dict):
        _bump(False, ms, "stdout 不是对象")
        raise LocalJudgeUnavailable("判定输出不是 JSON 对象")
    if out.get("error"):
        _bump(False, ms, str(out["error"]))
        raise LocalJudgeUnavailable("判定脚本报错：%s" % str(out["error"])[:160])
    answers = out.get("answers")
    if not isinstance(answers, dict) or not answers:
        _bump(False, ms, "answers 缺失/为空")
        raise LocalJudgeUnavailable("判定输出缺少 answers（keys=%s）" % list(out)[:6])
    _bump(True, ms, model=out.get("model"))
    return {"answers": answers, "model": out.get("model"), "usage": out.get("usage")}


def judge_one(question: Dict[str, Any], state: str = "",
              timeout_s: float = 20.0) -> Dict[str, Any]:
    """单题判定（`/relevant` 用）。返回的就是 `judge()`，题名固定为 `q`。"""
    return judge({"q": question}, state=state, timeout_s=timeout_s)
