#!/usr/bin/env python3
"""数据集真实评估（D1/D13/D16）：pin 版本 → 逐题问 free 模型 → verifier 判 → 回写 → bump。

    数据集(holdout) → [pin_eval 钉版本] → [逐题: 模型作答] → [verifier 三态判] →
                      [record_eval_result 回写] → [bump_version 前进版本]

【为什么是脚本而不是端点】评估要调模型（慢、要 key、要花钱/配额）⇒ 属**离线作业**；
端点只读（见 `src/api_dataset.py`）。

【诚实边界（重要）】
本轮数据集来自**真实执行轨迹的摘要**，`output` 是空的 ⇒ **没有 ground truth**。
所以这里的「评估」只测量**链路与产出可用性**（模型能不能给出非空/合规产物），
**不是**「题答得对不对」。要真正的质量分，得先有带标准答案的题集（那是一次独立的数据工程）。
本脚本只保证：结果**留痕、可比、可回捞成 badcase**，且**不假**（判不了就记 insufficient）。

CLI：
    python scripts/dataset_eval.py --dataset <id|name> [--split holdout] [--limit 5]
                                   [--model openrouter/free] [--db path]
"""
import argparse
import asyncio
import hashlib
import json
import sys
from pathlib import Path
from typing import Optional

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import dataset                              # noqa: E402
import db                                   # noqa: E402
from config import config                   # noqa: E402
from dataset_pipeline import assert_free_model   # noqa: E402
import verifier                             # noqa: E402

DEFAULT_MODEL = "openrouter/free"
DEFAULT_SPLIT = "holdout"

#: 评估题面模板。**明确不告诉被评对象「你在被评估」**，避免它改行为去讨好评审。
EVAL_PROMPT = """下面是某次执行任务的描述（来自真实执行轨迹的摘要）。请直接给出你的处理结论\
（中文，200 字以内，可操作）。不要解释你在被评估。 

任务：%s
"""


def build_prompt(item: dict) -> str:
    return EVAL_PROMPT % (item.get("input") or "")


def verdict_for(answer, kind: str = "nonempty"):
    """产物 → 评估三态 + 分数。**直接复用 P0-3 的 verifier**（同一套判据，不另造一套）。"""
    v = verifier.verify(kind, output=answer)
    st = v["status"]
    score = {"pass": 1.0, "fail": 0.0}.get(st)      # insufficient 留 None：不编假刻度
    return st, score


def _config_hash(model: str, kind: str) -> str:
    return hashlib.sha256(("model=%s;kind=%s" % (model, kind)).encode()).hexdigest()[:12]


async def evaluate_items(items: list, *, chat_fn, model: str, kind: str = "nonempty",
                         limit: Optional[int] = None) -> list:
    out = []
    todo = items[:int(limit)] if limit else items
    for it in todo:
        ans = await chat_fn(model, build_prompt(it))
        st, score = verdict_for(ans, kind)
        out.append({"item_id": it["id"], "verdict": st, "score": score,
                    "answer": (ans or "")[:500]})
    return out


async def run_eval(dataset_id: str, *, split: str = DEFAULT_SPLIT, limit: Optional[int] = None,
                   model: str = DEFAULT_MODEL, kind: str = "nonempty", chat_fn=None,
                   ds=dataset, bump: bool = True) -> dict:
    """跑一次评估。**顺序是判据的一部分**：pin → 评估 → 回写 → bump（反了会被自身闸门拒）。"""
    assert_free_model(model)
    if chat_fn is None:
        raise RuntimeError("run_eval 需要 chat_fn")
    obj = ds.get_dataset(dataset_id)
    if obj is None:
        raise KeyError("dataset 不存在: %s" % dataset_id)
    items = ds.list_items(dataset_id, split=split)
    if limit:
        items = items[:int(limit)]
    eid = ds.pin_eval(dataset_id, split=split, model=model,
                      config_hash=_config_hash(model, kind))
    results = await evaluate_items(items, chat_fn=chat_fn, model=model, kind=kind)
    for r in results:                       # 必须**在 bump 之前**回写，否则会因陈旧被拒
        ds.record_eval_result(eid, r["item_id"], verdict=r["verdict"], score=r["score"])
    passed = sum(1 for r in results if r["verdict"] == "pass")
    summary = {
        "eval_id": eid, "dataset_id": dataset_id, "split": split, "model": model,
        "kind": kind, "items": len(results), "passed": passed,
        "failed": sum(1 for r in results if r["verdict"] == "fail"),
        "insufficient": sum(1 for r in results if r["verdict"] == "insufficient"),
        "success_rate": round(100.0 * passed / len(results), 1) if results else None,
        "badcases": [r["item_id"] for r in results if r["verdict"] != "pass"],
        "version": obj["version"],
    }
    if bump:
        summary["version"] = ds.bump_version(
            dataset_id, note="eval %s (%s, n=%d, model=%s)" % (eid, split, len(results), model))
    return summary


def _resolve(ds, token: str) -> str:
    """允许用 id 或 name 点名数据集。"""
    if ds.get_dataset(token):
        return token
    for d in ds.list_datasets():
        if d["name"] == token:
            return d["id"]
    return token


def _model_chat_fn():
    import llm

    async def chat(model, prompt):
        ans, _ = await llm.chat_tools_loop([{"role": "user", "content": prompt}], [], None,
                                           max_rounds=1, model=model)
        return ans
    return chat


def main() -> int:
    ap = argparse.ArgumentParser(description="数据集真实评估（pin → 模型作答 → 判决 → 回写 → bump）")
    ap.add_argument("--dataset", required=True, help="数据集 id 或 name")
    ap.add_argument("--split", default=DEFAULT_SPLIT, choices=("train", "holdout"))
    ap.add_argument("--limit", type=int, default=5, help="评估题数上限（控成本）")
    ap.add_argument("--model", default=DEFAULT_MODEL)
    ap.add_argument("--kind", default="nonempty")
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 config.db_path）")
    ap.add_argument("--no-bump", action="store_true", help="不推进版本（只评估）")
    args = ap.parse_args()

    db.init_db(Path(args.db) if args.db else config.db_path)
    dataset.ensure_schema()
    ds_id = _resolve(dataset, args.dataset)
    summary = asyncio.run(run_eval(ds_id, split=args.split, limit=args.limit,
                                   model=args.model, kind=args.kind,
                                   chat_fn=_model_chat_fn(), bump=not args.no_bump))
    print("[dataset_eval] " + json.dumps(summary, ensure_ascii=False))
    bad = dataset.badcases(ds_id)
    print("[dataset_eval] badcase 池 %d 条：%s" % (len(bad), [r["id"] for r in bad]))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())