# -*- coding: utf-8 -*-
"""pooling 补标（TREC 式）：把评测 top-k 里**未被标注**的仓逐条取事实，供人工判相关。

为什么必须做：v2 复跑后 45 个 top5 位置里 35 个未标注，而其中 PowerJob/xxl-job/
manticoresearch 这类是明显的同类实现 —— 不补标，「未标注 ≠ 不相关」就会一直被
当成一句免责声明，hit@5 到底是低估还是真实，永远给不出量。

⚠️ pooling bias 必须如实声明：只对「某个系统 top-k 召回过」的仓做判，从未被任何
系统召回的仓**不会**进标注集 ⇒ 补标后的 hit@5 是**上界性质的乐观值**（分母仍不完整）。

判据与 annot_check.py 相同（A 存在 / B 硬过滤 archived-fork-stars / C 语义 /
D 边界），语义判定由人工做，本脚本只负责把事实取齐。

用法：venv/bin/python scripts/annot_check_pool.py work/eval-v2-annot.json
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))
import ghsettings  # noqa: E402


def _tok() -> str:
    return (ghsettings.db_get("token") or ghsettings._read_token_file() or "").strip()


def get(path: str, tok: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "agenthub-annot-pool"})
    with urllib.request.urlopen(req, timeout=20) as fh:
        return json.load(fh)


def days_since(iso: str) -> int | None:
    if not iso:
        return None
    d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("eval_json")
    ap.add_argument("--out", default="work/annot-pool-20261021.json")
    args = ap.parse_args()
    tok = _tok()
    if not tok:
        print("token 不可用")
        return 2

    with open(os.path.join(ROOT, args.eval_json), encoding="utf-8") as fh:
        ev = json.load(fh)
    spec_path = os.path.join(ROOT, "tests", "refscout_eval", "annotations.json")
    with open(spec_path, encoding="utf-8") as fh:
        spec = json.load(fh)
    known = {t["repo"].lower() for m in spec["modules"] for t in m["targets"]}

    out = {}
    for row in ev["rows"]:
        for repo in row["top"]:
            if repo.lower() in known:
                continue
            rec = {"module": row["id"], "repo": repo}
            try:
                d = get(f"/repos/{repo}", tok)
                rec.update({
                    "desc": d.get("description") or "",
                    "topics": d.get("topics") or [],
                    "stars": d.get("stargazers_count"),
                    "pushed_days": days_since(d.get("pushed_at", "")),
                    "archived": bool(d.get("archived")),
                    "fork": bool(d.get("fork")),
                    "lang": d.get("language"),
                    "license": ((d.get("license") or {}).get("spdx_id") or ""),
                    "homepage": d.get("homepage") or "",
                })
                rec["in_pool"] = bool(not rec["archived"] and not rec["fork"]
                                      and (rec["stars"] or 0) >= 20)
            except urllib.error.HTTPError as e:
                rec.update({"desc": f"HTTP {e.code}", "in_pool": False})
            except Exception as e:  # noqa: BLE001
                rec.update({"desc": str(e)[:100], "in_pool": False})
            out[f"{row['id']}|{repo}"] = rec
            print(f"[{row['id']:16s}] {repo:44s} ★{rec.get('stars', '-'):>6} "
                  f"{rec.get('pushed_days', '-'):>5}d  {(rec.get('desc') or '')[:70]}")
            time.sleep(0.15)

    dst = os.path.join(ROOT, args.out)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    print(f"\n共 {len(out)} 个未标注 top5 仓，事实写 {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
