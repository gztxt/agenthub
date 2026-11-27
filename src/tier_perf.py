"""模型**实测**表现（档内排序的输入，2026-11-28）。

【为什么需要它】
`tiercfg.DEFAULT_TIERS` / magpie 派生的档内顺序都是**声明**（按 prompt 单价升序）。
声明能决定"进哪个档"，但决定不了"这个档里先试谁"——同一档内两个模型的实际首轮成功率
可能差 2~3 倍（本机 audit：d1 72.5% / d2 45.8% / d3 7.7%，差 0.648）。
这份模块把「近 N 天、同一难度域的真实成败」折成一个可排序的分数，由 `tiercfg.first_usable`
在**样本够**时接管顺序；`tiercfg` 本身仍是不读库的纯函数（分数注入）。

【分数口径：Wilson 下界，不是裸成功率】
n=3 里 3 成功（100%）不该排在 n=80 里 60 成功（75%）前面 —— 那是拿小样本的运气换顺序。
Wilson 下界对 n 小惩罚重、n 大趋近真实比率，与本仓 `dispatch_weights` 同一口径。

【边界】
- 样本 < 3 的模型**不入表**（返回空 dict 项）：不足 3 次的比率是噪声。
- 读库失败/表不存在 ⇒ 返回 `{}`，调用方保持原序（「不知道」不伪装成「更差」）。
- 只看终态（success / failed / blocked）；running/pending 不计分母。
"""

import math
from typing import Dict, Optional

MIN_N = 3
DAYS = 30
_Z = 1.96


def wilson_lower(ok: int, n: int) -> float:
    """二项比例的 Wilson 下界（95%），与 dispatch_weights 同口径。"""
    if n <= 0:
        return 0.0
    p = ok / float(n)
    z2 = _Z * _Z
    denom = 1.0 + z2 / n
    center = p + z2 / (2 * n)
    margin = _Z * math.sqrt(max(0.0, p * (1 - p) / n + z2 / (4 * n * n)))
    return max(0.0, (center - margin) / denom)


def model_perf(days: int = DAYS, min_n: int = MIN_N, db=None) -> Dict[str, Dict[str, float]]:
    """近 N 天各模型的首轮成败 → `{model: {n, ok, rate, score}}`。

    「首轮」口径：`attempts` 为空或 0（重试过的任务只算一次、且不算首轮，
    否则重试成功会把一个本来首轮必败的模型抬上去）。
    """
    try:
        import db as _db
    except Exception:  # noqa: BLE001 —— 纯增强，导入失败即不排序
        return {}
    conn = db or _db
    try:
        rows = conn.query(
            "SELECT COALESCE(NULLIF(model_sent,''), NULLIF(model_planned,''), NULLIF(model,'')) m,"
            "       COUNT(*) n,"
            "       SUM(CASE WHEN status='success' THEN 1 ELSE 0 END) ok"
            "  FROM tasks"
            " WHERE created_at > datetime('now', ?)"
            "   AND status IN ('success','failed','blocked')"
            "   AND COALESCE(attempts,0)=0"
            " GROUP BY m",
            ("-%d day" % int(days),))
    except Exception:  # noqa: BLE001
        return {}
    out: Dict[str, Dict[str, float]] = {}
    for r in rows or []:
        m = str(r.get("m") or "").strip()
        n = int(r.get("n") or 0)
        ok = int(r.get("ok") or 0)
        if not m or n < min_n:
            continue
        out[m] = {"n": n, "ok": ok, "rate": ok / float(n),
                  "score": round(wilson_lower(ok, n), 6)}
    return out


def order(models, perf: Optional[Dict[str, Dict[str, float]]] = None):
    """按实测分数降序重排；无实测（或分数并列）的保持原序（稳定排序）。"""
    perf = perf or {}
    return sorted(models, key=lambda m: -((perf.get(m) or {}).get("score", 0.0)))