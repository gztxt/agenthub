#!/usr/bin/env python3
"""数据集管线（P0-1 切片 B）：**声明式** + 确定性优先，模型只准在最后一步。

    轨迹 → [过滤(确定性)] → [采样(确定性)] → [切分(确定性)] → [enrich(模型，最后)]

【为什么把模型关在最后】
调模型 = 不可复现 + 花钱。地基若掺进模型，同一份轨迹会产出不同的数据集，
`eval_run` 之间的比较就没有共同基线了（手册 D1/D13）。所以：

  · `build_items()` 是**纯函数**：过滤/采样/切分全可复现，产物 `output` 恒为空；
  · `enrich()` 才调模型，且**非会话一律 `openrouter/free`**（`openrouter/auto` 直接拒）。

【切分稳定性】
`assign_split(grain_hash, frac)` 只用 **grain 的哈希**决定 split ⇒ 同一逻辑样本**永远**
落同一 split。若按「每次运行随机分」，重跑会把 holdout 洗进 train，隔离就成了摆设。

CLI：`python scripts/dataset_pipeline.py --dataset trajectories`（需先有轨迹来源）。
"""
import argparse
import hashlib
import json
import os
import sys
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import dataset          # noqa: E402
import db               # noqa: E402
from config import config  # noqa: E402
import hook             # noqa: E402

#: 非会话调模默认：agnes/agnes-2.5-flash（2026-10-29：免费池不稳定，改稳质模型）
#    仍可通过 --model openrouter/free 或 MEMORY_LLM_MODEL 环境变量覆盖。
DEFAULT_MODEL = "agnes/agnes-2.5-flash"

#: 轨迹 → 样本的默认声明（可被 spec 覆盖）。
DEFAULT_SPEC = {
    "sources": None,          # None=不限来源；列表=白名单
    "require_events": True,   # 无事件的轨迹不算样本
    "limit": 200,
    "seed": 0,
    "holdout_frac": 0.2,
    "goal_chars": 200,
}


def assert_free_model(model) -> None:
    """非会话调模准入检查（2026-10-29 更新）。

    允许：
    · 免费模型：`openrouter/free`、`openrouter/openrouter/free`、`*:free`（兜底用）
    · 稳质模型：`agnes/agnes-*`（本机 CCR 本地代理，不经过 OpenRouter 免费池）

    **严禁 `openrouter/auto`**（军规第4条）：语义是「自动挑最便宜的」，最便宜可能是付费档。
    """
    m = str(model or "")
    if m in ("openrouter/free", "openrouter/openrouter/free") or m.endswith(":free"):
        return
    if m.startswith("agnes/"):
        return  # agnes 是本机 CCR 本地代理，不扣费
    raise ValueError("非会话调模只允许 openrouter/free / *:free / agnes/*，得到 %r（严禁 openrouter/auto）" % model)


def _sha(s: str) -> str:
    return hashlib.sha256(s.encode("utf-8")).hexdigest()


def assign_split(grain_hash_hex: str, holdout_frac: float) -> str:
    """按 grain 哈希切分：同一 grain 永远同一 split（重跑不洗牌）。"""
    frac = max(0.0, min(1.0, float(holdout_frac or 0.0)))
    bucket = int(str(grain_hash_hex)[:12], 16) % 1000
    return "holdout" if bucket < int(round(frac * 1000)) else "train"


def _passes(traj: dict, spec: dict) -> bool:
    if spec.get("require_events") and not (traj.get("events") or []):
        return False
    sources = spec.get("sources")
    if sources and traj.get("source") not in sources:
        return False
    return True


def _goal(traj: dict) -> str:
    """确定性「题面」（不调模型）：来源 + cwd + 事件数 + 首个事件名。"""
    evs = traj.get("events") or []
    first = (evs[0].get("event") if evs else "") or ""
    return ("session=%s source=%s cwd=%s events=%d first=%s"
            % (traj.get("session_id"), traj.get("source"), traj.get("cwd") or "",
               len(evs), first))


def sample(items: list, limit, seed) -> list:
    """确定性采样：按 `(seed, grain_hash)` 的 sha 排序取前 N —— 与输入顺序无关。"""
    limit = len(items) if limit is None else int(limit)
    if limit >= len(items):
        return sorted(items, key=lambda it: it["grain_hash"])
    return sorted(items, key=lambda it: _sha("%s\n%s" % (seed, it["grain_hash"])))[:limit]


def build_items(trajectories: list, spec: dict = None) -> list:
    """轨迹 → 样本（**纯函数**：过滤 → 采样 → 切分；不调模型，`output` 恒为空）。"""
    spec = {**DEFAULT_SPEC, **(spec or {})}
    kept = [t for t in (trajectories or []) if _passes(t, spec)]
    items = []
    for t in kept:
        gh = _sha("session\n%s\n%s" % (t.get("session_id"), t.get("source")))[:32]
        items.append({
            "grain_hash": gh,
            "input": _goal(t)[:int(spec.get("goal_chars") or 200)],
            "output": "",                      # 模型在最后一步才填
            "trajectory": dataset.dumps_trajectory(t),      # D5：统一 schema（trajectory/1）
            "trace_id": t.get("session_id"),
            "session_id": t.get("session_id"),
            "source_ref": "trajectory:%s" % t.get("session_id"),
        })
    items = sample(items, spec.get("limit"), spec.get("seed"))
    for it in items:
        it["split"] = assign_split(it["grain_hash"], spec.get("holdout_frac"))
    return items


async def enrich(items: list, *, model: str = DEFAULT_MODEL, chat_fn=None) -> list:
    """**最后一步**：对每条样本调模型补 `output`。非会话模型必须 free。"""
    assert_free_model(model)
    if chat_fn is None:
        raise RuntimeError("enrich 需要 chat_fn（本模块不自行建模型连接）")
    out = []
    for it in items:
        ans = await chat_fn(model, it["input"])
        row = dict(it)
        row["output"] = ans or ""
        out.append(row)
    return out


def _load_trajectories(source: str = "profile") -> list:
    """轨迹来源：`profile` = hub 自己的执行账本（实测有真数据）；`telemetry` = 外部推送。"""
    if source == "telemetry":
        return hook.trajectories_from_store()
    return hook.traces_from_store()


def main() -> int:
    ap = argparse.ArgumentParser(description="轨迹 → 数据集（确定性管线，模型最后）")
    ap.add_argument("--dataset", default="trajectories", help="数据集名（同名幂等）")
    ap.add_argument("--source", choices=("profile", "telemetry"), default="profile",
                    help="轨迹来源（profile=profile_events 真数据；telemetry=外部推送）")
    ap.add_argument("--grain", default="session", help="唯一键口径说明")
    ap.add_argument("--limit", type=int, default=DEFAULT_SPEC["limit"])
    ap.add_argument("--seed", type=int, default=DEFAULT_SPEC["seed"])
    ap.add_argument("--holdout-frac", type=float, default=DEFAULT_SPEC["holdout_frac"])
    ap.add_argument("--dry-run", action="store_true", help="只打印，不落库")
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 config.db_path）")
    args = ap.parse_args()

    db.init_db(Path(args.db) if args.db else config.db_path)
    dataset.ensure_schema()

    trajs = _load_trajectories(args.source)
    spec = {**DEFAULT_SPEC, "limit": args.limit, "seed": args.seed,
            "holdout_frac": args.holdout_frac}
    items = build_items(trajs, spec)
    print("[pipeline] 轨迹 %d → 样本 %d（过滤/采样/切分均为确定性）" % (len(trajs), len(items)))
    if args.dry_run:
        print(json.dumps(items[:3], ensure_ascii=False, indent=2))
        return 0
    ds = dataset.create_dataset(args.dataset, grain=args.grain)
    for it in items:
        dataset.add_item(ds, split=it["split"], input=it["input"],
                         output=it["output"], trajectory=it["trajectory"],
                         trace_id=it["trace_id"], session_id=it["session_id"],
                         source_ref=it["source_ref"],
                         grain_key={"grain_hash": it["grain_hash"]})
    print("[pipeline] 落库 dataset=%s counts=%s" % (ds, dataset.counts(ds)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())