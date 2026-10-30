# -*- coding: utf-8 -*-
"""判定后端统一入口（2026-10-23；2026-10-23 晚接 DeepSeek-Flash）。

【当前形态 · 先读这段】
`JUDGE_BACKEND` 决定真后端，默认 **remote**（`src/remote_judge.py`，钉死
`agnes/agnes-2.5-flash`；2026-10-29 用户裁定：取消 deepseek、取消免费模型）。本机 Ollama 与 `jev-decision` 技能已在 2026-10-23 删除，
`ollama` 分支只是保留给「某天真的把本机推理装回来」——它现在跑起来必然报
`script_missing`，这是如实失败，不是配置错。

选择后端的依据是实测（`agent-knowledge/250`）：远端三档质量全饱和，差别只在延迟
（flash 3.3s vs 免费 11.5s），而 5 题要装进 `/relevant` 的 45s 超时。
缓存 / 计数 / 连续失败告警 / `status()` **仍是本模块唯一真相**，`remote_judge`
只负责「把一个 prompt 变成一段文字」。

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
import remote_judge

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

#: 判定后端：`remote`（CCR agnes-2.5-flash，默认）/ `ollama`（本机技能，已删除）。
#: 切换后**必须**重启 hub：模块常量在导入时定型（与其它开关一致）。
BACKEND = os.getenv("JUDGE_BACKEND", "remote").strip().lower()

#: 判定总开关。默认值按后端给：remote 默认开（用户 2026-10-23 裁定接远端模型，
#: 2026-10-29 换成 agnes-2.5-flash），ollama 默认关（技能已删，开着只会每轮刷 script_missing 告警）。
#: `LOCAL_JUDGE=0` 仍是**一键关**——关掉后 judge() 立即抛 Unavailable，不起请求、
#: 不计失败、不写缓存，运维看到的是「按设计关闭」而不是「判定一直坏」。
ENABLED = os.getenv("LOCAL_JUDGE", "1" if BACKEND == "remote" else "0").strip().lower() in (
    "1", "true", "on", "yes")

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


def _backend_sig() -> str:
    """缓存指纹：后端 + 模型/脚本。换模型或改提示词后旧结论自动失配。

    remote 用**模型 ID**（prompt 在代码里，随部署更新）；ollama 用脚本指纹
    （提示词住在技能脚本里）。两者都进同一把键，切后端不会读到对方的缓存。
    """
    if BACKEND == "remote":
        return "remote:%s:%s" % (remote_judge.MODEL, remote_judge.MAX_TOKENS)
    return "ollama:%s" % _script_sig()


def _cache_key(questions: Dict[str, Any], state: str) -> Optional[str]:
    if CACHE_TTL_S <= 0:
        return None
    blob = json.dumps(questions, ensure_ascii=False, sort_keys=True, default=str)
    st = state or ""
    if len(blob) > CACHE_MAX_STATE or len(st) > CACHE_MAX_STATE:
        return None
    return judge_cache.key("local_judge:v2", st + "\x00" + blob,
                           _backend_sig(), os.getenv("JUDGE_MODEL_TAG", ""))


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


def record(ok: bool, ms: int, err: Optional[str] = None, kind: str = "chain") -> None:
    """外部调用点（`chain_judge`）写回统计的**公开入口**。

    为什么需要它：主链判定直接打 Ollama（技能自带的 `choice` 原语实测有位置偏置，
    见 `chain_judge` 模块头），但「失败要告警、状态要在 `/api/vitals` 看见」是同一件事——
    不允许出现两套计数，否则「判定到底跑了几次/坏没坏」会有两个真相。
    """
    if ok:
        if _record(True, int(ms)):
            _log_recovered(kind)
    else:
        _fail(kind, int(ms), str(err or "chain_fail"))


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
    remote = BACKEND == "remote"
    return {
        # backend 是**给人看的后端名**，跟 JUDGE_MODEL 走：写死字面量会在换模型后
        # 与 `/api/vitals` 的 model 字段互相矛盾（2026-10-29 换 agnes 时正是如此）。
        "backend": (remote_judge.MODEL.rsplit("/", 1)[-1] if remote else "ollama-local"),
        "backend_id": BACKEND,
        "enabled": ENABLED,
        # ready 只判「配置在位」：remote 看网关地址，ollama 看脚本在不在。
        # **不表示模型能答** —— 那要看 last_ok_ts / last_error / alert。
        "ready": remote_judge.configured() if remote else p.is_file(),
        "script": None if remote else str(p),
        "model": st.get("last_model") or (remote_judge.MODEL if remote else None),
        "remote": remote_judge.status() if remote else None,
        # 概率闸阈值：改了要重启（模块常量导入时定型），面板据此解释 decided=false。
        "min_p": remote_judge.MIN_P,
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
        "note": ("判定后端=%s；外部 jev 端点已于 2026-10-23 下线删除，本机 Ollama 与"
                 " jev-decision 技能同批删除。remote 后端钉死 %s（不用路由器/免费池："
                 "路由会把请求换成审核模型）" % (BACKEND, remote_judge.MODEL)),
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


def _judge_remote(questions: Dict[str, Any], state: str, timeout_s: float,
                  ck: Optional[str]) -> Dict[str, Any]:
    """远端后端（DeepSeek-Flash）的一轮判定。失败与子进程路径同口径：记账 + 抛。"""
    t0 = time.monotonic()
    try:
        out = remote_judge.judge(questions, state=state, timeout_s=timeout_s)
    except remote_judge.RemoteJudgeUnavailable as e:
        ms = int((time.monotonic() - t0) * 1000)
        _fail(getattr(e, "kind", "remote") or "remote", ms, str(e))
        raise LocalJudgeUnavailable("远端判定失败：%s" % str(e)[:200])
    ms = int((time.monotonic() - t0) * 1000)
    answers = out.get("answers")
    if not isinstance(answers, dict) or not answers:
        _fail("empty", ms, "answers 缺失/为空")
        raise LocalJudgeUnavailable("远端判定输出缺少 answers")
    recovered = _record(True, ms, model=out.get("model"), kind="ok")
    if recovered:
        _log_recovered("ok")
    result = {"answers": answers, "model": out.get("model"), "usage": out.get("usage"),
              "elapsed_ms": ms}
    if ck:
        judge_cache.put(ck, result, CACHE_TTL_S)
    result["cache"] = {"hit": False, "hits": 0, "cached_at": None, "age_s": None}
    return result


def judge(questions: Dict[str, Any], state: str = "",
          timeout_s: float = DEFAULT_TIMEOUT_S,
          use_cache: bool = True) -> Dict[str, Any]:
    """跑一轮判定，**并过概率闸**。`answers` 每题额外带 `max_p/decided/gate`。

    闸在**这一层**（唯一汇聚点）：`_judge_raw` 的原样答案先落缓存，读出来再逐次过闸。
    于是 ① remote/ollama 两个后端自动同权；② 改 `JUDGE_MIN_P` 重启后旧缓存按新阈值
    重新裁决，**不需要清缓存**；③ `remote_judge.ask_one` 保持「原始解析层」语义
    （测试与排查要看模型到底分布长什么样，不希望被闸改过）。

    语义：`decided=False` 不是失败（不抛、不计 fail），是「问到了但没把握」——
    调用方须自己决定退回硬规则。想区分「判完了/判不出/没判成」看三处：
    `decided` / `decided=False` / 异常 `LocalJudgeUnavailable`。
    """
    res = _judge_raw(questions, state=state, timeout_s=timeout_s, use_cache=use_cache)
    answers = res.get("answers") if isinstance(res, dict) else None
    if not isinstance(answers, dict):
        # 结构不对是后端违约（`_judge_raw` 本该抛）——原样返回，别在闸里编答案。
        return res
    out = dict(res)
    out["answers"] = remote_judge.apply_gate(answers)
    return out


def _judge_raw(questions: Dict[str, Any], state: str = "",
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
    if not ENABLED:
        # 不计失败、不告警：这是「按设计关闭」，不是故障。记录会污染连续失败计数。
        raise LocalJudgeUnavailable(
            "判定已关闭（LOCAL_JUDGE=0）；当前后端=%s。要恢复设 LOCAL_JUDGE=1 并重启"
            % BACKEND)
    script = script_path()
    if BACKEND != "remote" and not script.is_file():
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

    if BACKEND == "remote":
        return _judge_remote(questions, state, timeout_s, ck)

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
