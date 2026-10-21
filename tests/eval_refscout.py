# -*- coding: utf-8 -*-
"""参考情报检索评测（P2 ①，PT-20261021-04）。

【为什么单独一个脚本】`tests/run_tests.sh` 是 hermetic 的，不联网；这个评测**必须**
打真实 GitHub。所以它不进 L0 闸门，只在改检索口径/权重后手工跑，用来判断
「这次改动到底有没有变好」——没有它，任何调参都是凭感觉。

【指标口径】标注集见 `tests/refscout_eval/annotations.json`（5 模块 / 13 个目标仓，
2026-10-21 逐条核过 description）。只标注「确定的同问题仓库」，因此：

- `hit@5`（主指标）＝ top5 命中的标注仓数 / 该模块标注仓总数。衡量**能不能找到**。
- `mrr` ＝ 第一个命中仓的名次倒数。衡量**排得够不够前**。
- `prec@5`（只作下界）＝ top5 中标注仓占比。**未标注 ≠ 不相关**，所以这个数偏悲观，
  真实值只能靠人工复核，报告里必须标「下界」。

【默认不跑重排】重排有 LLM 成本且方差大（P0/P1 实测 40~82s）。默认 `do_rerank=False`
得可重复的词法基线；要评重排效果加 `--rerank`（会调网关，可能产生费用）。

用法：
    venv/bin/python tests/eval_refscout.py                 # 全 5 模块，冷缓存
    venv/bin/python tests/eval_refscout.py --only terminal-ws
    venv/bin/python tests/eval_refscout.py --json work/eval.json
"""
from __future__ import annotations

import argparse
import json
import os
import shutil
import sys
import tempfile
import time
from typing import Any, Dict, List

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, os.path.join(ROOT, "src"))


def _load_annotations() -> Dict[str, Any]:
    with open(os.path.join(HERE, "refscout_eval", "annotations.json"), encoding="utf-8") as fh:
        return json.load(fh)


def _prep_env(cold: bool) -> str:
    """评测必须与生产数据隔离：缓存目录指向 work/ 下的临时目录。

    `config` 在 import 时读环境变量，所以**必须先设 env 再 import**。冷缓存是默认，
    否则第二次跑测的是缓存命中率而不是检索质量。
    """
    work = os.path.join(ROOT, "work")
    os.makedirs(work, exist_ok=True)
    data_dir = tempfile.mkdtemp(prefix="refscout-eval-", dir=work) if cold \
        else os.path.join(work, "eval-data")
    os.makedirs(data_dir, exist_ok=True)
    os.environ["DATA_DIR"] = data_dir
    os.environ["LOG_DIR"] = os.path.join(data_dir, "logs")
    return data_dir


def _score(ranked: List[Dict[str, Any]], targets: List[str], k: int = 5):
    """`target_ranks` 区分两种失败：**没入池**（查询召回不到 ⇒ 改检索式/扩词）
    与**入池但排在 k 名之外**（排序问题 ⇒ 改权重）。两者处置完全不同，不能混。"""
    order = {c["repo"]: i + 1 for i, c in enumerate(ranked)}
    top = [c["repo"] for c in ranked[:k]]
    hits = [r for r in top if r in targets]
    ranks = [i + 1 for i, r in enumerate(top) if r in targets]
    return {
        "top": top,
        "hits": hits,
        "target_ranks": {r: order.get(r) for r in targets},
        "hit@k": round(len(hits) / len(targets), 3) if targets else 0.0,
        "prec@k": round(len(hits) / k, 3),
        "mrr": round(1.0 / min(ranks), 3) if ranks else 0.0,
    }


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--only", default="", help="只跑某个 module id")
    ap.add_argument("--k", type=int, default=5)
    ap.add_argument("--rerank", action="store_true", help="含 LLM 重排（有成本、有方差）")
    ap.add_argument("--allow-paid", action="store_true")
    ap.add_argument("--json", default="", help="把结果写到这个路径（相对仓根或绝对）")
    ap.add_argument("--keep-cache", action="store_true", help="复用 work/eval-data 缓存")
    ap.add_argument("--assume-lang", default="",
                    help="对未填 lang 的模块统一按该语言加权（只改打分，不改查询）；"
                         "用于量「语言先验」的净效果，与不填组并列报告")
    args = ap.parse_args()

    data_dir = _prep_env(cold=not args.keep_cache)
    import refscout  # noqa: E402  —— 必须在 DATA_DIR 设好之后

    spec = _load_annotations()
    mods = [m for m in spec["modules"] if not args.only or m["id"] == args.only]
    if not mods:
        print(f"没有匹配的模块：{args.only}")
        return 2

    rows = []
    v1_rows = []
    for m in mods:
        t0 = time.monotonic()
        lang = m.get("lang", "") or args.assume_lang
        got = refscout.collect(m["module"], m["keywords"], lang,
                               int(m.get("min_stars", 20)), int(m.get("fresh_days", 180)),
                               [], bool(args.allow_paid), do_rerank=bool(args.rerank))
        targets = [t["repo"] for t in m["targets"]]
        s = _score(got["candidates"], targets, args.k)
        # v1 子集：只留第一批（无 added 字段）的模块与标注。
        # 为什么要单独报：v2 把分母从 13 扩到 35，跨版本比 hit@5 是拿两个不同的
        # 平均数相减。只有在 v1 子集上重算，才能回答「检索变好了没有」。
        v1_targets = [t["repo"] for t in m["targets"] if not t.get("added")]
        v1_mod = not m.get("added") and bool(v1_targets)
        if v1_mod:
            v1_rows.append({"n": len(v1_targets), **_score(got["candidates"], v1_targets, args.k)})
        rows.append({
            "id": m["id"], "module": m["module"], "targets": targets,
            "pool": got["meta"]["pool_size"], "elapsed_s": got["elapsed"],
            "status": got["status"], "arms": [(a["name"], a["count"]) for a in got["meta"]["arms"]],
            **s,
            "missed": [t for t in targets if t not in s["top"]],
        })
        print(f"\n== {m['module']}  ({got['elapsed']}s, 池 {got['meta']['pool_size']})")
        for i, repo in enumerate(s["top"], 1):
            mark = "✓" if repo in targets else " "
            print(f"  {i}. [{mark}] {repo}")
        miss = []
        for t in rows[-1]["missed"]:
            rk = s["target_ranks"][t]
            miss.append(f"{t}（未入池）" if rk is None else f"{t}（池内 #{rk}）")
        print(f"  hit@{args.k}={s['hit@k']}  mrr={s['mrr']}  prec@{args.k}={s['prec@k']}(下界)"
              f"  未中: {'; '.join(miss) or '无'}")

    n = len(rows)
    agg = {
        "modules": n,
        "hit@5": round(sum(r["hit@k"] for r in rows) / n, 3),
        "mrr": round(sum(r["mrr"] for r in rows) / n, 3),
        "prec@5_lower_bound": round(sum(r["prec@k"] for r in rows) / n, 3),
        "total_labels": sum(len(r["targets"]) for r in rows),
        "found": sum(len(r["hits"]) for r in rows),
        "elapsed_s": round(sum(r["elapsed_s"] for r in rows), 1),
        "rerank": bool(args.rerank),
        "assume_lang": args.assume_lang,
        "annotations_version": spec["version"],
        "as_of": spec["as_of"],
    }
    if v1_rows:
        agg["v1_subset"] = {
            "modules": len(v1_rows),
            "hit@5": round(sum(r["hit@k"] for r in v1_rows) / len(v1_rows), 3),
            "mrr": round(sum(r["mrr"] for r in v1_rows) / len(v1_rows), 3),
            "total_labels": sum(r["n"] for r in v1_rows),
            "found": sum(len(r["hits"]) for r in v1_rows),
        }
    print(f"\n== 汇总（{n} 模块 / {agg['total_labels']} 个标注仓）")
    print(f"  hit@5={agg['hit@5']}  mrr={agg['mrr']}  prec@5={agg['prec@5_lower_bound']}(下界)"
          f"  命中 {agg['found']}/{agg['total_labels']}  用时 {agg['elapsed_s']}s"
          f"  重排={'开' if args.rerank else '关'}")
    if v1_rows:
        v = agg["v1_subset"]
        print(f"  └ v1 子集（可比口径）：{v['modules']} 模块 / {v['found']} 命中"
              f"  hit@5={v['hit@5']}  mrr={v['mrr']}")

    if args.json:
        out = args.json if os.path.isabs(args.json) else os.path.join(ROOT, args.json)
        with open(out, "w", encoding="utf-8") as fh:
            json.dump({"summary": agg, "rows": rows}, fh, ensure_ascii=False, indent=2)
        print(f"  结果已写 {out}")

    if not args.keep_cache:
        shutil.rmtree(data_dir, ignore_errors=True)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
