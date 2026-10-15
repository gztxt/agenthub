#!/usr/bin/env python3
"""经验挖掘（P0-1 切片 C）：AMR「Dreaming」三件事 —— 合并重复 / 删过期 / 解冲突。

    轨迹 → 数据集 → [经验抽取(模型，free)] → **经验库** → [Dreaming：本脚本] → 更干净的经验库

【为什么是「退役」而不是「删除」】
经验库只进不出会退化成又一份只增不减的日志；但**物理删**会丢掉「为什么这条被顶替」。
所以本脚本只把旧条目标 `retired_at` + `superseded_by`（见 `dataset.retire_experience`），
`list_experiences(active_only=True)` 自然不再召回它，而追溯链完整。

【三类 reason（互不混同）】
    duplicate  同 `norm_key`（情况+适用条件）且 action 相同 ⇒ 重复
    conflict   同 `norm_key` 但 action 不同     ⇒ 冲突，保留最新
    expired    创建时间早于 `expire_days`       ⇒ 过期

每组只保留**最新**那条（按 `created_at`，再按 id）。**已退役的条目不参与**。

模型只用于「从轨迹抽取经验」这一步，且**非会话一律 `openrouter/free`**（复用管线的守卫）。

CLI：`python scripts/experience_mine.py`（`--dry-run` 只出计划不落库）。
"""
import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "scripts"))

import dataset                              # noqa: E402
import db                                   # noqa: E402
from config import config                   # noqa: E402
from dataset_pipeline import assert_free_model  # noqa: E402

DEFAULT_MODEL = "openrouter/free"
EXPIRE_DAYS = 180


def _norm(s) -> str:
    return re.sub(r"\s+", " ", str(s or "").strip().lower())


def norm_key(exp: dict) -> str:
    """「情况 + 适用条件」的规范化键（空白/大小写无关）。"""
    return "%s||%s" % (_norm(exp.get("situation")), _norm(exp.get("precondition")))


def _parse(ts):
    try:
        return datetime.fromisoformat(str(ts))
    except Exception:  # noqa: BLE001
        return None


def _newest(group: list) -> dict:
    return max(group, key=lambda e: (str(e.get("created_at") or ""), str(e.get("id") or "")))


def plan(experiences: list, now: str = None, expire_days: int = EXPIRE_DAYS) -> dict:
    """出**计划**（纯函数）：哪些条目该退役、被谁顶替、为什么。不改库。"""
    now_dt = _parse(now) or datetime.now(timezone.utc)
    retire = []
    groups = {}
    for e in experiences or []:
        if e.get("retired_at"):            # 已退役：不参与
            continue
        groups.setdefault(norm_key(e), []).append(e)

    for _, group in groups.items():
        expired = set()
        for e in group:                    # 规则一：过期
            created = _parse(e.get("created_at"))
            if created is not None and (now_dt - created).days > expire_days:
                retire.append({"id": e["id"], "superseded_by": None, "reason": "expired"})
                expired.add(e["id"])
        live = [e for e in group if e["id"] not in expired]
        if len(live) < 2:                  # 规则二/三：去重与解冲突
            continue
        keep = _newest(live)
        actions = {_norm(e.get("action")) for e in live}
        reason = "duplicate" if len(actions) == 1 else "conflict"
        for e in live:
            if e["id"] == keep["id"]:
                continue
            retire.append({"id": e["id"], "superseded_by": keep["id"], "reason": reason})
    return {"retire": retire,
            "groups": {k: sorted(e["id"] for e in v) for k, v in groups.items()}}


def apply_plan(plan_dict: dict, ds=dataset) -> int:
    n = 0
    for r in plan_dict.get("retire", []):
        ds.retire_experience(r["id"], superseded_by=r.get("superseded_by"))
        n += 1
    return n


def mine(ds=dataset, now: str = None, expire_days: int = EXPIRE_DAYS) -> dict:
    """读全量 → 出计划 → 落库（退役）。返回计划。"""
    exps = ds.list_experiences(active_only=False)
    p = plan(exps, now=now, expire_days=expire_days)
    apply_plan(p, ds=ds)
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description="经验库 Dreaming：合并重复 / 删过期 / 解冲突")
    ap.add_argument("--dry-run", action="store_true", help="只出计划，不落库")
    ap.add_argument("--expire-days", type=int, default=EXPIRE_DAYS)
    ap.add_argument("--db", default=None, help="SQLite 路径（默认 config.db_path）")
    args = ap.parse_args()
    db.init_db(Path(args.db) if args.db else config.db_path)
    dataset.ensure_schema()
    if args.dry_run:
        p = plan(dataset.list_experiences(active_only=False), expire_days=args.expire_days)
        print(json.dumps(p, ensure_ascii=False, indent=2))
        return 0
    p = mine(expire_days=args.expire_days)
    print("[experience_mine] 退役 %d 条：%s"
          % (len(p["retire"]), json.dumps(p["retire"], ensure_ascii=False)))
    print("[experience_mine] 活跃 %d 条" % len(dataset.list_experiences()))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())