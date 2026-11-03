"""派发权重：把历史 `tasks` 行折成「哪个 agent / 哪个模型值得再派」的可解释排名。

## 为什么要有这一层

现在的派发选型只有两条依据：`difficulty` 的写死档位映射 + `TASK_ESCALATE_LADDER`
里手填的 agent 阶梯。两者都是**先验**，没有任何一处读过历史成绩。实测代价（2026-11-03
30 天数据）：`codex` 烧掉全部 token 的 80.8%（top5 任务全是它），而它在「改前端文件」
这类任务上 6 次秒挂（`exit:1: Reading additional input from stdin...`，平均 2.5 秒）；
`claude-code` 同一族任务的 600s 超时 6 次。这些事实**都在库里**，但选型层读不到。

## 三个维度，各自为什么要这么算

1. **质量**：成功率不能直接用 `success/n`。n=2 全对是 100%，跟 n=45 的 100% 不该同权。
   这里用 **Wilson 下界**（95%），它随样本量收紧：小数样本会被自动压低，
   不需要另设「至少 N 条才参与排名」的人为门槛去掩盖不稳定性。
2. **效率**：用 **p50 而非均值**。均值被单次 600s 超时拉爆（实测超时任务平均 103s、
   最长 600s），排名会变成「谁最倒霉」而不是「谁最快」。
3. **消耗**：用 **每成功一次的 token**（`tokens / success`），不是每任务 token。
   否则「干得多但大多失败」的 agent 因为分母大反而显得便宜。

## 明确不做的

- **不改派发行为**。本轮只产出排名与建议位，选型仍然走既有档位层 —— 权重直接接管选型
  需要先跑 A/B（否则「优化」与「回归」不可分辨），这条写进 PENDING-TASKS。
- **不跨任务族混算**。`codex` 在代码改造上差、在长文生成上可能好；把 30 天所有任务
  混成一列会得到「谁都不该用」。所以主表按 `(agent, model)` 分组，并额外给
  `by_kind` 分族（读写代码 / 检索推理 / 汇总），分族判据见 `task_family()`。
"""
from __future__ import annotations

import json
import math
from typing import Any, Dict, List, Optional

import db

#: 参与排名的最小样本量。低于它仍列出来（附 `low_sample=True`），只是不进 `top`。
MIN_SAMPLE = 5

#: 质量下界的置信水平（Wilson）。0.95 是惯例，改动前先读 `wilson_lower` 文档串。
WILSON_Z = 1.96

#: 合成分的权重（质量 : 效率 : 成本）。改这三个数等于改「偏好哪种 agent」，
#: 所以必须留痕（`weights_meta()` 会把当前值回给调用方，界面直接显示）。
WEIGHTS = {"quality": 0.5, "efficiency": 0.3, "cost": 0.2}

FAMILY_CODE = "code"
FAMILY_RESEARCH = "research"
FAMILY_SYNTH = "synth"

#: 任务族判据：先看 `files`（最硬：有文件集就是要读改代码），
#: 其次看 id（汇总节点），最后按 prompt 里的动词兜底。
#: **不按 agent 分族** —— 那会变成「用 codex 的任务都算 codex 族」的循环论证。
_CODE_HINTS = ("改", "修", "实现", "重构", "新增", "删除", "替换", "补", "落盘", "写", "加")
_RESEARCH_HINTS = ("检索", "查找", "定位", "调研", "核对", "比对", "收集", "搜索", "读")


def task_family(row: Dict[str, Any]) -> str:
    """判定任务族（纯函数）。三档：code / research / synth。

    判据顺序即优先级，任一条命中即返回，不再往下看：
      1. id 以 `t_sum` 开头 ⇒ synth（汇总是独立族：它只吃上游产物，不碰文件）
      2. `files` 非空 ⇒ code（声明了文件就是要读改代码，这是最强信号）
      3. prompt 含代码类动词 ⇒ code
      4. prompt 含检索类动词 ⇒ research
      5. 都没有 ⇒ research（保守：不假设它会改文件，避免把只读任务算进代码族）
    """
    tid = str(row.get("task_id") or "")
    if tid.startswith("t_sum"):
        return FAMILY_SYNTH
    files = row.get("files")
    try:
        files = json.loads(files) if isinstance(files, str) else files
    except Exception:  # noqa: BLE001 — 坏 JSON 当空
        files = []
    if files:
        return FAMILY_CODE
    prompt = str(row.get("prompt") or "")
    if any(h in prompt for h in _CODE_HINTS):
        return FAMILY_CODE
    return FAMILY_RESEARCH


def wilson_lower(success: int, n: int, z: float = WILSON_Z) -> float:
    """成功率的下界（Wilson score interval 单侧下界）。n<=0 返回 0.0。

    为什么不用 `success/n`：n=2 时 2/2=100%，n=45 时 35/45=78%，前者会排到后者前面。
    实测样本里大量 agent×模型组合只有 1~3 条记录，直接用比率会让**运气**冒充能力。
    Wilson 下界自带样本量惩罚，且不会像 Laplace 平滑那样在小样本上给固定偏置。
    """
    if n <= 0:
        return 0.0
    p = success / n
    denom = 1.0 + z * z / n
    centre = p + z * z / (2 * n)
    margin = z * math.sqrt(max(0.0, p * (1 - p) / n + z * z / (4 * n * n)))
    return max(0.0, (centre - margin) / denom)


def _pct(vals: List[float], q: float) -> Optional[float]:
    """分位数（线性插值）。空表返回 None —— 不拿 0 冒充「很快」。"""
    vs = sorted(v for v in vals if v is not None)
    if not vs:
        return None
    if len(vs) == 1:
        return float(vs[0])
    pos = q * (len(vs) - 1)
    lo = int(math.floor(pos))
    hi = min(lo + 1, len(vs) - 1)
    return float(vs[lo] + (vs[hi] - vs[lo]) * (pos - lo))


def _rows(days: int) -> List[Dict[str, Any]]:
    sql = ("SELECT task_id,prompt,files,agent_id,model_sent,model,status,attempts,"
           "duration_ms,spent_tokens,last_fail_kind,created_at "
           "FROM tasks WHERE created_at >= datetime('now', ?)")
    out = []
    for r in db.query(sql, ("-%d days" % int(days),)):
        d = dict(r)
        d["family"] = task_family(d)
        out.append(d)
    return out


def _agg(rows: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """按 (agent, model) 聚合。model 取 `model_sent`（我们下发的）而不是 `model`（回包观测）。"""
    buckets: Dict[tuple, List[Dict[str, Any]]] = {}
    for r in rows:
        key = (r.get("agent_id") or "?", r.get("model_sent") or r.get("model") or "(默认)")
        buckets.setdefault(key, []).append(r)

    out = []
    for (agent, model), rs in buckets.items():
        n = len(rs)
        ok = sum(1 for r in rs if r.get("status") == "success")
        blocked = sum(1 for r in rs if r.get("status") == "blocked")
        failed = sum(1 for r in rs if r.get("status") == "failed")
        toks = sum(int(r.get("spent_tokens") or 0) for r in rs)
        durs = [r.get("duration_ms") for r in rs if r.get("status") == "success"]
        retried = sum(1 for r in rs if int(r.get("attempts") or 0) > 0)
        out.append({
            "agent_id": agent, "model": model, "n": n, "success": ok, "failed": failed,
            "blocked": blocked,
            "success_rate": round(ok / n, 4) if n else 0.0,
            "quality": round(wilson_lower(ok, n), 4),
            "p50_ms": _pct(durs, 0.5),
            "retry_rate": round(retried / n, 4) if n else 0.0,
            "tokens_total": toks,
            # 每成功一次的成本：干得多但大多失败的组合，不该因为分母大显得便宜
            "tokens_per_success": int(toks / ok) if ok else None,
            "low_sample": n < MIN_SAMPLE,
            "fail_kinds": _fail_kinds(rs),
        })
    return out


def _fail_kinds(rs: List[Dict[str, Any]]) -> Dict[str, int]:
    kinds: Dict[str, int] = {}
    for r in rs:
        if r.get("status") == "success":
            continue
        k = (r.get("last_fail_kind") or "").strip() or ("上游阻塞" if r.get("status") == "blocked" else "未知")
        kinds[k] = kinds.get(k, 0) + 1
    return dict(sorted(kinds.items(), key=lambda kv: -kv[1])[:5])


def _normalize(vals: Dict[str, Optional[float]], higher_better: bool) -> Dict[str, float]:
    """min-max 归一到 0~1；None 记 0（「没数据」不该被当成「最好」）。全等时返回 0.5。"""
    real = [v for v in vals.values() if v is not None]
    if not real:
        return {k: 0.0 for k in vals}
    lo, hi = min(real), max(real)
    if hi == lo:
        return {k: (0.5 if v is not None else 0.0) for k, v in vals.items()}
    return {k: (0.0 if v is None else ((v - lo) / (hi - lo) if higher_better
                                      else (hi - v) / (hi - lo))) for k, v in vals.items()}


def _normalize_log(vals: Dict[str, Optional[float]]) -> Dict[str, float]:
    """**越低越好**的量的归一，先取 log1p 再 min-max。

    为什么必须取对数：实测每成功一次 token 从 194 到 364,811（跨 3 个数量级）、
    p50 耗时从 17s 到 295s。线性 min-max 下「87,514 token」会拿到 0.76 的分
    （因为最大值 364,811 把标尺拉满），于是 100 倍的差距被读成「差一点」——
    排名结果就是「贵的排前面」。这不是偏好问题，是量纲问题。
    """
    logs = {k: (None if v is None else math.log1p(max(0.0, v))) for k, v in vals.items()}
    return _normalize(logs, higher_better=False)


def compute(days: int = 30) -> Dict[str, Any]:
    """主入口：返回 {rows, top, by_family, meta}。

    `score = 0.5*quality + 0.3*efficiency + 0.2*cost`，三项都先在同一批 (agent,model)
    内做 min-max 归一 —— 分数只用于**同批比较**，不是绝对值，跨批次不可比。
    """
    rows = _agg(_rows(days))
    if rows:
        eff = _normalize_log({r["agent_id"] + "|" + r["model"]: r["p50_ms"] for r in rows})
        cost = _normalize_log({r["agent_id"] + "|" + r["model"]: r["tokens_per_success"]
                               for r in rows})
        for r in rows:
            k = r["agent_id"] + "|" + r["model"]
            r["score"] = round(WEIGHTS["quality"] * r["quality"]
                               + WEIGHTS["efficiency"] * eff.get(k, 0.0)
                               + WEIGHTS["cost"] * cost.get(k, 0.0), 4)
        rows.sort(key=lambda r: (-r["score"], -r["n"]))
    eligible = [r for r in rows if not r["low_sample"]]
    return {
        "rows": rows,
        "top": ["%s|%s" % (r["agent_id"], r["model"]) for r in eligible[:5]],
        "by_family": _by_family(days),
        "meta": {
            "days": int(days), "weights": dict(WEIGHTS), "min_sample": MIN_SAMPLE,
            "wilson_z": WILSON_Z,
            "note": ("质量=成功率 Wilson 下界；效率=p50 毫秒（只统计成功任务）；"
                     "成本=每成功一次 token。质量用线性 min-max，效率与成本**取 log1p 后**再归一"
                     "（这两项跨 2~3 个数量级，线性归一会把 100 倍读成「差一点」），"
                     "分数只用于同批比较，跨批次不可比。"),
        },
    }


def _by_family(days: int) -> Dict[str, List[Dict[str, Any]]]:
    """分族排名：同一族内重算一次，避免「跨族混算 → 谁都不该用」。"""
    raw = _rows(days)
    out: Dict[str, List[Dict[str, Any]]] = {}
    for fam in (FAMILY_CODE, FAMILY_RESEARCH, FAMILY_SYNTH):
        sub = _agg([r for r in raw if r["family"] == fam])
        sub.sort(key=lambda r: (-r["quality"], -r["n"]))
        out[fam] = sub[:5]
    return out


def suggest(agent_pool, family: str = FAMILY_CODE, days: int = 30,
            by_family: Optional[Dict[str, List[Dict[str, Any]]]] = None) -> Optional[Dict[str, Any]]:
    """给某个任务族推荐一个 agent（**只读建议，不改派发**）。

    返回 None 表示「没有够格的证据」—— 这时调用方必须回落到既有选型，
    而不是拿空结果当「随便挑一个」。

    `by_family` 可注入：L0 测试不碰库（生产路径传 None 才真读）。
    """
    pool = {str(a) for a in (agent_pool or set())}
    table = by_family if by_family is not None else _by_family(days)
    for r in table.get(family) or []:
        if r["agent_id"] in pool and not r["low_sample"] and r["quality"] > 0:
            return r
    return None