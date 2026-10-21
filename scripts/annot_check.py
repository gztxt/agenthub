# -*- coding: utf-8 -*-
"""标注集核对（PT-20261021-04 续）：对每个候选仓打 /repos/{owner}/{repo}。

判据（四条同时成立才算合格标注，与检索引擎结果无关）：
  A 存在：HTTP 200，full_name 与标注一致（大小写以 API 为准）
  B 在池：archived=false、fork=false、stars>=20
          —— 三条**硬过滤**：archived 在 collect() 里显式 `continue`，
             fork/stars 写进每条查询的 `fork:false archived:false stars:>=N`。
             任一不满足则该仓 hit@5 数学上不可能命中，属标签缺陷而非检索缺陷。
          ⚠️ 新鲜度（pushed_at<=180d）**不是**硬过滤：`fresh_days` 只进 `fresh` 臂
             （`pushed:>cut` + 按 updated 排），core/pair/kw* 臂不带该条件。
             旧版本本脚本把新鲜度误判为硬过滤，会把 gotty(810d) / robfig(835d)
             这类**实际能命中**的标注误删 —— 量错对象比漏量更危险。
  C 语义：description 能一句话说明「与模块解决同一问题」
  D 边界：不是仅"某子组件"的过度延伸（node-pty 是 PTY 绑定、xterm.js 是前端渲染，
          二者都只是终端分享方案的零件，单独作标注会把 hit@5 变成"零件命中率"）

用法：venv/bin/python scripts/annot_check.py [--in <json>] [--out <json>]
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

CANDIDATES = {
    "terminal-ws": ["cs01/termpair", "yudai/gotty", "tsl0922/ttyd",
                    "microsoft/node-pty", "xtermjs/xterm.js"],
    "llm-gateway": ["BerriAI/litellm", "Portkey-AI/gateway", "songquanpeng/one-api"],
    "health-probe": ["louislam/uptime-kuma", "TwiN/gatus",
                     "prometheus/blackbox_exporter", "upptime/upptime"],
    "cron-scheduler": ["agronholm/apscheduler", "robfig/cron", "celery/celery",
                       "quartz-scheduler/quartz", "go-co-op/gocron", "distribworks/dkron"],
    "fulltext-search": ["meilisearch/meilisearch", "typesense/typesense",
                        "quickwit-oss/quickwit", "valeriansaliou/sonic",
                        "blevesearch/bleve", "zincsearch/zincsearch"],
    "log-aggregation": ["grafana/loki", "fluent/fluent-bit", "vectordotdev/vector"],
    "tunnel-proxy": ["fatedier/frp", "cloudflare/cloudflared", "inconshreveable/ngrok"],
    "vector-db": ["milvus-io/milvus", "qdrant/qdrant", "weaviate/weaviate",
                  "chroma-core/chroma", "pgvector/pgvector"],
    "docs-site": ["mkdocs/mkdocs", "facebook/docusaurus", "docsifyjs/docsify",
                  "vuejs/vitepress"],
}


def _tok() -> str:
    return (ghsettings.db_get("token") or ghsettings._read_token_file() or "").strip()


def get(path: str, tok: str) -> dict:
    req = urllib.request.Request(
        "https://api.github.com" + path,
        headers={"Authorization": f"Bearer {tok}",
                 "Accept": "application/vnd.github+json",
                 "X-GitHub-Api-Version": "2022-11-28",
                 "User-Agent": "agenthub-annot-check"})
    with urllib.request.urlopen(req, timeout=20) as fh:
        return json.load(fh)


def days_since(iso: str) -> int | None:
    if not iso:
        return None
    d = datetime.strptime(iso, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=timezone.utc)
    return (datetime.now(timezone.utc) - d).days


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="work/annot-check-20261021.json")
    args = ap.parse_args()
    tok = _tok()
    if not tok:
        print("token 不可用")
        return 2

    out = {}
    for mod, repos in CANDIDATES.items():
        for repo in repos:
            rec = {"module": mod, "asked": repo}
            try:
                d = get(f"/repos/{repo}", tok)
                rec.update({
                    "exists": True,
                    "full_name": d.get("full_name"),
                    "case_ok": d.get("full_name") == repo,
                    "desc": d.get("description") or "",
                    "stars": d.get("stargazers_count"),
                    "pushed_days": days_since(d.get("pushed_at", "")),
                    "archived": bool(d.get("archived")),
                    "fork": bool(d.get("fork")),
                    "topics": d.get("topics") or [],
                    "lang": d.get("language"),
                    "license": ((d.get("license") or {}).get("spdx_id") or ""),
                })
            except urllib.error.HTTPError as e:
                rec.update({"exists": False, "http": e.code})
            except Exception as e:  # noqa: BLE001
                rec.update({"exists": False, "error": str(e)[:120]})
            # 判据 B（硬过滤）：archived / fork / stars 三项决定「能否进池」
            rec["in_pool"] = bool(rec.get("exists") and not rec.get("archived")
                                  and not rec.get("fork")
                                  and (rec.get("stars") or 0) >= 20)
            # 新鲜度只决定「有没有 fresh 臂这次机会」，不作合格性判据
            rec["fresh_arm"] = bool(rec.get("pushed_days") is not None
                                    and rec["pushed_days"] <= 180)
            out[f"{mod}|{repo}"] = rec
            flag = "OK " if rec["in_pool"] else "XX "
            print(f"{flag}{repo:38s} ★{rec.get('stars', '-'):>7} "
                  f"{rec.get('pushed_days', '-'):>5}d arch={rec.get('archived')} "
                  f"{(rec.get('desc') or rec.get('error') or '')[:72]}")
            time.sleep(0.15)

    dst = os.path.join(ROOT, args.out)
    with open(dst, "w", encoding="utf-8") as fh:
        json.dump(out, fh, ensure_ascii=False, indent=2)
    bad = [k for k, v in out.items() if not v["in_pool"]]
    print(f"\n共 {len(out)}，进池 {len(out) - len(bad)}，被池条件排除 {len(bad)}: {bad}")
    print(f"结果写 {dst}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
