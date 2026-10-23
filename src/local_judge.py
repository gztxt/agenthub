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

【缓存（2026-10-23 第二批，用户令「同样问题直接判定」）】
键 = `(脚本指纹, 题面 JSON, state)`：脚本一改（提示词/模型都在脚本里）指纹就变，
旧结论自动失配，不会把旧模型的答案套到新提示词上。落盘复用 `judge_cache`
（原子写 + LRU + 损坏当空缓存），TTL 默认 7 天。
**只缓存成功**：超时/脚本缺失/Ollama 不在都是瞬时故障，缓存它等于把「现在不能问」
固化成「永远是这个答案」——这正是模块一直拒绝的事。
命中/未命中都计数并在 `status()` 外显：否则「有缓存」与「缓存从没命中」在外部看起来一样。

【告警与日志（2026-10-23，用户令「判定失败告警和日志」）】
- 每次失败：`logging` WARNING，一行 key=value（kind/ms/consecutive/error），可被 journalctl 过滤。
- 连续失败 ≥ `JUDGE_ALERT_AFTER`（默认 3）⇒ ERROR + `alert=true` + 追加
  `data/judge_alerts.jsonl`（含 ts/kind/ms/error/consecutive）；恢复时记一条 recovery。
- 告警状态进 `status()`，由 `/api/vitals`、`/api/skill/status` 外显；面板/巡检能看到
  「判定坏了」，而不是只看到「最近一次成功」。
"""
from __future__ import annotations

import json
import logging
import os
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Dict, Optional

import judge_cache

log = logging.getLogger("hub.judge")

#: 单轮判定的硬超时（秒）。一道题 = 一次 CPU 推理，实测单题 1~3s；
#: vitals 一次送 5 题，/relevant 一次 1 题，都由调用方按题量传更小的值。
DEFAULT_TIMEOUT_S = float(os.getenv("JEV_DECISION_TIMEOUT", "60"))

#: 结果缓存 TTL（秒）。默认 7 天：题面 + state 已经是完整输入，证据变了键就变；
#: TTL 只是防「同一份旧证据被永久引用」。0 = 关闭缓存。
CACHE_TTL_S = float(os.getenv("JUDGE_CACHE_TTL_S", str(7 * 86400)))

#: 超过这个长度的题面/state 不进缓存：一是键计算白费内存，二是这种请求本来就不重复。
CACHE_MAX_STATE = int(os.getenv("JUDGE_CACHE_MAX_STATE", "20000"))

#: 连续失败多少轮触发告警。默认 3：单次超时可能是 Ollama 换模型，连续 3 次才是坏了。
ALERT_AFTER = int(os.getenv("JUDGE_ALERT_AFTER", "3"))

#: 默认技能脚本位置：`<技术文档>/skills/@user_36623aa7/jev-decision/scripts/decision.py`。
#: src → agenthub → 技术文档，与 hub 的 techdocs 技能根（`skill.py:_DEFAULT_DIRS`）同源。
DEFAULT_SCRIPT = (Path(__file__).resolve().parents[2] / "skills" / "@user_36623aa7"
                  / "jev-decision" / "scripts" / "decision.py")

_LOCK = threading.Lock()
_STAT: Dict[str, Any] = {
    "calls": 0, "ok": 0, "fail": 0, "ms_sum": 0, "ms_max": 0,
    "cache_hits": 0, "cache_misses": 0,
    "consecutive_fail": 0, "last_fail_ts": None, "last_fail_kind": None,
    "alert": False, "alert_at": None,
    "last_error": None, "last_model": None, "last_ok_ts": None,
}


class LocalJudgeUnavailable(RuntimeError):
    """这次判定没跑成。调用方按既有口径降级（vitals→硬规则；BM25 仍是检索权威）。"""


def script_path() -> Path:
    """判定脚本文本位置（env 可覆盖，供测试/影子实例指向假脚本）。"""
    return Path(os.getenv("JEV_DECISION_SCRIPT") or DEFAULT_SCRIPT)


def alert_path() -> Path:
    """告警落盘位置。跟随 DATA_DIR，影子实例/测试不会写进生产。"""
    root = os.getenv("DATA_DIR")
    base = Path(root) if root else (Path(__file__).resolve().parent.parent / "data")
    return Path(os.getenv("JUDGE_ALERT_PATH") or (base / "judge_alerts.jsonl"))


def _script_sig() -> str:
    """脚本指纹（mtime_ns + size）。提示词/模型改了 ⇒ 缓存自动失效。"""
    try:
        st = script_path().stat()
    except OSError:
        return "missing"
    return "%d:%d" % (st.st_mtime_ns, st.st_size)


def _cache_key(questions: Dict[str, Any], state: str) -> Optional[str]:
    if CACHE_TTL_S <= 0:
        return None
    blob = json.dumps(questions, ensure_ascii=False, sort_keys=True, default=str)
    st = state or ""
    if len(blob) > CACHE_MAX_STATE or len(st) > CACHE_MAX_STATE:
        return None
    return judge_cache.key("local_judge:v1", st + "\x00" + blob,
                           _script_sig(), os.getenv("JUDGE_MODEL_TAG", ""))


def _append_alert(rec: Dict[str, Any]) -> None:
    """追加一行告警。失败只记日志，不影响判定主流程。"""
    try:
        p = alert_path()
        p.parent.mkdir(parents=True, exist_ok=True)
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except OSError as e:
        log.warning("judge_alert_write_failed path=%s err=%s", alert_path(), e)


def _record(ok: bool, ms: int, err: Optional[str] = None,
            model: Optional[str] = None, kind: str = "") -> bool:
    """记一次真调用。返回「本次是否新触发告警」。"""
    now = time.time()
    with _LOCK:
        _STAT["calls"] += 1
        _STAT["ok" if ok else "fail"] += 1
        _STAT["ms_sum"] += int(ms)
        _STAT["ms_max"] = max(int(_STAT["ms_max"] or 0), int(ms))
        if err:
            _STAT["last_error"] = str(err)[:200]
        if ok:
            _STAT["last_ok_ts"] = now
            _STAT["consecutive_fail"] = 0
            if model:
                _STAT["last_model"] = str(model)[:60]
            recovered = bool(_STAT["alert"])
            if recovered:
                _STAT["alert"] = False
                _STAT["alert_at"] = None
            return recovered
        _STAT["consecutive_fail"] += 1
        _STAT["last_fail_ts"] = now
        _STAT["last_fail_kind"] = kind or None
        n = int(_STAT["consecutive_fail"])
        fire = n >= ALERT_AFTER and not _STAT["alert"]
        if fire:
            _STAT["alert"] = True
            _STAT["alert_at"] = now
        return fire


def _log_fail(kind: str, ms: int, err: str, fired: bool) -> None:
    with _LOCK:
        n = int(_STAT["consecutive_fail"])
    log.warning("judge_fail kind=%s ms=%d consecutive=%d alert=%s err=%s",
                kind or "-", ms, n, int(fired), str(err)[:200])
    if fired:
        log.error("judge_alert kind=%s consecutive=%d err=%s", kind or "-", n, str(err)[:200])
        _append_alert({"ts": time.time(), "event": "alert", "kind": kind,
                       "ms": ms, "consecutive": n, "error": str(err)[:300]})


def _log_recovered(kind: str) -> None:
    with _LOCK:
        n = int(_STAT["fail"])
    log.info("judge_recovered kind=%s total_fail=%d", kind or "-", n)
    _append_alert({"ts": time.time(), "event": "recovered", "kind": kind,
                   "total_fail": n})


def _fail(kind: str, ms: int, err: str) -> None:
    fired = _record(False, ms, err, kind=kind)
    _log_fail(kind, ms, err, fired)


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
    hits, misses = int(st.get("cache_hits") or 0), int(st.get("cache_misses") or 0)
    lookup = hits + misses
    cs = judge_cache.stats()
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
        # ── 缓存：命中率必须可观测（否则「有缓存」与「从没命中」长得一样）──
        "cache": {
            "enabled": CACHE_TTL_S > 0,
            "ttl_s": CACHE_TTL_S,
            "hits": hits,
            "misses": misses,
            "hit_rate": round(hits / lookup, 4) if lookup else None,
            "entries": cs.get("entries"),
            "max_entries": cs.get("max_entries"),
            "path": cs.get("path"),
        },
        # ── 告警：连续失败与当前告警态（面板/巡检据此报红）──
        "consecutive_fail": int(st.get("consecutive_fail") or 0),
        "alert": bool(st.get("alert")),
        "alert_at": st.get("alert_at"),
        "alert_after": ALERT_AFTER,
        "last_fail_ts": st.get("last_fail_ts"),
        "last_fail_kind": st.get("last_fail_kind"),
        "alert_path": str(alert_path()),
        "note": ("判定后端=本机 Ollama（jev-decision 技能）；外部 jev 端点已于 2026-10-23 "
                 "下线删除，key/区域封禁那套判据随之作废"),
    }


def reset_stats() -> None:
    """清零计数与告警态（测试用；运维也可在排查后重置）。**不清缓存条目**。"""
    with _LOCK:
        for k in ("calls", "ok", "fail", "ms_sum", "ms_max", "cache_hits", "cache_misses",
                  "consecutive_fail"):
            _STAT[k] = 0
        for k in ("last_error", "last_fail_ts", "last_fail_kind", "alert_at"):
            _STAT[k] = None
        _STAT["alert"] = False
    judge_cache.reset_stats()


def recent_alerts(n: int = 5) -> list:
    """最近 n 条告警（只读取尾部，文件不存在/损坏返回空）。"""
    try:
        lines = alert_path().read_text(encoding="utf-8").splitlines()[-n:]
    except OSError:
        return []
    out = []
    for ln in lines:
        try:
            out.append(json.loads(ln))
        except ValueError:
            continue
    return out


def judge(questions: Dict[str, Any], state: str = "",
          timeout_s: float = DEFAULT_TIMEOUT_S,
          use_cache: bool = True) -> Dict[str, Any]:
    """跑一轮本地判定。失败抛 `LocalJudgeUnavailable`，**不吞、不返回空**。

    返回 `{"answers": {...}, "model": ..., "usage": ...}`——
    `answers` 的每个键对应 `questions` 的同名键（服务端没承诺顺序时按名取，
    不按序取，防静默串位；缺键由调用方按「没判成」处理）。

    `use_cache=True` 时同 `(脚本, 题面, state)` 直接命中磁盘缓存（`cache.hit=true`）。
    """
    if not isinstance(questions, dict) or not questions:
        raise LocalJudgeUnavailable("没有问题可判（questions 为空）")
    script = script_path()
    if not script.is_file():
        _fail("script_missing", 0, "脚本不在：%s" % script)
        raise LocalJudgeUnavailable("判定脚本不在：%s（jev-decision 技能未安装？）" % script)

    ck = _cache_key(questions, state) if use_cache else None
    if ck:
        hit = judge_cache.get(ck)
        if hit:
            with _LOCK:
                _STAT["cache_hits"] += 1
            ans = dict(hit)
            ans.pop("_cache_hits", None)
            ans.pop("_cached_at", None)
            cached_at = float(hit.get("_cached_at") or 0)
            ans["cache"] = {"hit": True, "hits": int(hit.get("_cache_hits") or 0),
                            "cached_at": cached_at,
                            "age_s": round(time.time() - cached_at, 1) if cached_at else None}
            return ans
    with _LOCK:
        _STAT["cache_misses"] += 1 if ck else 0

    payload = json.dumps({"state": state, "questions": questions}, ensure_ascii=False)
    t0 = time.monotonic()
    try:
        r = subprocess.run(  # noqa: S603 — 固定 argv，无 shell；脚本路径来自配置/默认值
            [sys.executable, str(script), "full"],
            input=payload, capture_output=True, text=True, timeout=max(1.0, timeout_s))
    except subprocess.TimeoutExpired:
        ms = int((time.monotonic() - t0) * 1000)
        _fail("timeout", ms, "超时 >%.1fs" % timeout_s)
        raise LocalJudgeUnavailable("本地判定超时 >%.1fs（CPU 推理，Ollama 可能在排队）"
                                    % timeout_s)
    except Exception as e:  # noqa: BLE001 —— 启动失败（权限/解释器不存在）同样是不可用
        ms = int((time.monotonic() - t0) * 1000)
        _fail("spawn", ms, "%s: %s" % (type(e).__name__, e))
        raise LocalJudgeUnavailable("判定进程启动失败：%s: %s" % (type(e).__name__, str(e)[:120]))
    ms = int((time.monotonic() - t0) * 1000)
    if r.returncode != 0:
        tail = (r.stderr or "").strip().splitlines()[-1:] or ["(无 stderr)"]
        _fail("exit", ms, "rc=%d %s" % (r.returncode, tail[0]))
        raise LocalJudgeUnavailable("判定脚本退出 %d：%s" % (r.returncode, tail[0][:160]))
    try:
        out = json.loads((r.stdout or "").strip() or "{}")
    except ValueError as e:
        _fail("parse", ms, "stdout 非 JSON：%s" % e)
        raise LocalJudgeUnavailable("判定输出不是 JSON（%s）；stdout 头部 %r"
                                    % (e, (r.stdout or "")[:80]))
    if not isinstance(out, dict):
        _fail("parse", ms, "stdout 不是对象")
        raise LocalJudgeUnavailable("判定输出不是 JSON 对象")
    if out.get("error"):
        _fail("script_error", ms, str(out["error"]))
        raise LocalJudgeUnavailable("判定脚本报错：%s" % str(out["error"])[:160])
    answers = out.get("answers")
    if not isinstance(answers, dict) or not answers:
        _fail("empty", ms, "answers 缺失/为空")
        raise LocalJudgeUnavailable("判定输出缺少 answers（keys=%s）" % list(out)[:6])

    recovered = _record(True, ms, model=out.get("model"), kind="ok")
    if recovered:
        _log_recovered("ok")
    result = {"answers": answers, "model": out.get("model"), "usage": out.get("usage"),
              "elapsed_ms": ms}
    if ck:
        judge_cache.put(ck, result, CACHE_TTL_S)
    result["cache"] = {"hit": False, "hits": 0, "cached_at": None, "age_s": None}
    return result


def judge_one(question: Dict[str, Any], state: str = "",
              timeout_s: float = 20.0, use_cache: bool = True) -> Dict[str, Any]:
    """单题判定（`/relevant` 用）。返回的就是 `judge()`，题名固定为 `q`。"""
    return judge({"q": question}, state=state, timeout_s=timeout_s, use_cache=use_cache)
