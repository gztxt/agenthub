#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""技能检索评测（2026-10-22，PT-20261022-11）。

    python3 scripts/eval-skill-retrieval.py                  # 打本机 3102
    python3 scripts/eval-skill-retrieval.py --base http://127.0.0.1:3199
    python3 scripts/eval-skill-retrieval.py --auto            # 另测 /auto 的接受率
    python3 scripts/eval-skill-retrieval.py --verify-targets  # 只核对标签里的技能真的存在

## 为什么单独写一个，而不是复用 `scripts/eval-q.py`

`eval-q.py` 评的是 **XERJ 文档检索**（BM25 + 模板参数），输入是 md 路径；
这里评的是 **技能检索**（BM25F 三字段 + 接受判据），输入是技能名。
两者唯一的共同点是「都有 rank」，但**标签口径、语料、可达上限完全不同**，
混用一套评测脚本只会让「哪个指标属于哪条链路」再次失真。

## 指标口径（报数时必须同时报类别与集大小，本仓已因此翻车三次）

- `rank1`：top1 就是目标（或兄弟技能）。兄弟技能单独计 `sibling` —— 本机存在
  同能力多副本（`agent-dispatch` ≈ `dispatching-parallel-agents`），把兄弟判成错
  会低估系统、把兄弟判成对会高估系统，所以**单列**。
- `mrr@3` / `recall@3`：目标（含兄弟）落在 top-3 的名次倒数 / 是否命中。
- **X 类单列**：跨词表查询，词法检索原理上难命中，**不计入可达上限**；
  但仍要报，因为它测的是「会不会误报到别的技能」。
- 退出码：0 = 达标；1 = 未达标（`--gate` 时才生效，默认只报数）。

⚠ 本脚本**只读**：只打 GET `/api/skill/relevant`。
加 `--auto` 才会打 POST `/api/skill/auto`，那会写 `skill.invoke(mode=auto_invoke)`
记账事件 —— 会真实影响权重榜，故默认关闭，且 caller 固定为 `skill-eval`。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Dict, List, Tuple

HERE = os.path.dirname(os.path.abspath(__file__))
CASES = os.path.join(HERE, "skill-eval-cases.txt")

#: 达标线（D+P+S 合并口径）。定值的依据：改动前实测 rank1 中文 1/8（≈0.13），
#: 改动后 8 条样本 rank1 4/8；本集 28 条 D/P/S 的期望是「显著高于旧值」，
#: 不是「接近 1.0」——把线定成 0.9 只会逼着以后往集里塞简单样本。
GATE_RANK1 = 0.60


def load_cases(path: str) -> List[Dict[str, Any]]:
    out: List[Dict[str, Any]] = []
    with open(path, encoding="utf-8") as fh:
        for ln, line in enumerate(fh, 1):
            s = line.strip()
            if not s or s.startswith("#"):
                continue
            parts = s.split("|")
            if len(parts) < 3:
                raise SystemExit("第 %d 行格式不对（要 query|类别|目标|兄弟|备注）：%r" % (ln, s))
            q, cat, target = parts[0].strip(), parts[1].strip().upper(), parts[2].strip()
            sib = [x.strip() for x in (parts[3] if len(parts) > 3 else "").split(",") if x.strip()]
            note = (parts[4] if len(parts) > 4 else "").strip()
            if cat not in ("D", "P", "S", "X"):
                raise SystemExit("第 %d 行类别 %r 不认识" % (ln, cat))
            out.append({"q": q, "cat": cat, "target": target, "siblings": sib, "note": note})
    return out


def _get(url: str, timeout: float = 30.0) -> Tuple[int, Any]:
    req = urllib.request.Request(url, headers={"x-hub-channel": "skill-eval"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return e.code, {"error": e.read().decode("utf-8", "replace")[:200]}
    except Exception as e:  # noqa: BLE001
        return -1, {"error": "%s: %s" % (type(e).__name__, e)}


def relevant(base: str, q: str, n: int = 3, llm_rerank: bool = False,
             llm_timeout: float = 8.0) -> Tuple[List[str], Dict[str, Any]]:
    params = {"q": q, "n": n, "rerank": "false", "caller": "skill-eval", "via": "query"}
    if llm_rerank:
        params.update({"llm_rerank": "true", "llm_timeout": llm_timeout})
    url = base + "/api/skill/relevant?" + urllib.parse.urlencode(params)
    st, d = _get(url, timeout=60)
    if st != 200:
        return [], {"error": "HTTP %s" % st, "body": d}
    items = ((d.get("bm25") or {}).get("items")) or []
    return [str(x.get("name") or "") for x in items], (d.get("llm_rerank") or {})


def auto_accept(base: str, q: str) -> Dict[str, Any]:
    """打一次 `/auto`（**会记账**，见模块 docstring）。返回 ok/name/norm。"""
    body = json.dumps({"task": q, "caller": "skill-eval", "allow_store": False}).encode()
    tok = os.getenv("HUB_TOKEN") or ""
    hdr = {"Content-Type": "application/json", "x-hub-channel": "skill-eval"}
    if tok:
        hdr["x-hub-token"] = tok
    req = urllib.request.Request(base + "/api/skill/auto", data=body, headers=hdr, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            d = json.loads(r.read().decode("utf-8", "replace"))
    except urllib.error.HTTPError as e:
        return {"ok": False, "error": "HTTP %s" % e.code}
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "error": "%s: %s" % (type(e).__name__, e)}
    return {"ok": bool(d.get("ok")), "name": d.get("name"),
            "norm": (d.get("local") or {}).get("norm"), "source": d.get("source")}


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base", default="http://127.0.0.1:3102")
    ap.add_argument("--cases", default=CASES)
    ap.add_argument("--n", type=int, default=3)
    ap.add_argument("--auto", action="store_true", help="另测 /auto 接受率（会记账）")
    ap.add_argument("--llm-rerank", action="store_true",
                    help="开启免费模型重排（PT-20261022-13），并统计其延迟/成功率")
    ap.add_argument("--llm-timeout", type=float, default=8.0)
    ap.add_argument("--verify-targets", action="store_true", help="只核对标签技能是否存在")
    ap.add_argument("--gate", action="store_true", help="rank1 低于阈值时退出码 1")
    args = ap.parse_args()

    cases = load_cases(args.cases)

    if args.verify_targets:
        st, d = _get(args.base + "/api/skill/list?limit=1000")
        if st != 200:
            print("FAIL 取不到技能清单：%s" % d)
            return 2
        names = {x.get("name") for x in d.get("items") or []}
        missing = sorted({c["target"] for c in cases if c["target"] not in names} |
                         {s for c in cases for s in c["siblings"] if s not in names})
        print("标签技能 %d 个，缺失 %d 个：%s" % (len(names), len(missing), missing or "—"))
        return 0 if not missing else 1

    print("集：%s（%d 条）　base=%s　n=%d" % (os.path.basename(args.cases), len(cases), args.base, args.n))
    print("%-34s %-3s %-30s %s" % ("query", "cat", "top1", "结果"))
    per_cat: Dict[str, Dict[str, Any]] = {}
    auto_stat = {"called": 0, "ok": 0, "right": 0, "wrong": 0}
    rr_stat = {"called": 0, "ok": 0, "reordered": 0, "fixed": 0, "broke": 0, "ms": []}
    for c in cases:
        names, rr = relevant(args.base, c["q"], args.n, args.llm_rerank, args.llm_timeout)
        if args.llm_rerank:
            base_names, _ = relevant(args.base, c["q"], args.n, False)
            rr_stat["called"] += 1
            if rr.get("ok"):
                rr_stat["ok"] += 1
                rr_stat["ms"].append(int(rr.get("ms") or 0))
                if rr.get("reordered"):
                    rr_stat["reordered"] += 1
                    was = bool(base_names) and base_names[0] in (c["target"],) or (
                        base_names and base_names[0] in c["siblings"])
                    now = bool(names) and (names[0] == c["target"] or names[0] in c["siblings"])
                    if now and not was:
                        rr_stat["fixed"] += 1
                    elif was and not now:
                        rr_stat["broke"] += 1
        top1 = names[0] if names else "(空)"
        rank = 0
        for i, nm in enumerate(names, 1):
            if nm == c["target"]:
                rank = i
                break
        # 兄弟技能（同能力多副本，如 agent-dispatch ≈ dispatching-parallel-agents）：
        # 目标未命中时找兄弟；**目标排在兄弟后面**时也单列（top1 是兄弟不算系统错）。
        sibling = ""
        sib_top1 = bool(names) and names[0] in c["siblings"] and names[0] != c["target"]
        if rank == 0:
            for i, nm in enumerate(names, 1):
                if nm in c["siblings"]:
                    rank, sibling = i, "sibling"
                    break
        if rank == 1 and not sibling:
            verdict = "rank1"
        elif sib_top1 and rank:
            verdict = "rank%d(sibling-top1)" % rank
        elif rank:
            verdict = "rank%d" % rank
        else:
            verdict = "MISS"
        print("%-34s %-3s %-30s %s" % (c["q"][:34], c["cat"], top1[:30], verdict))
        s = per_cat.setdefault(c["cat"], {"n": 0, "rank1": 0, "hit3": 0, "mrr": 0.0, "sib": 0})
        s["n"] += 1
        if rank == 1 and not sibling:
            s["rank1"] += 1
        if rank:
            s["hit3"] += 1
            s["mrr"] += 1.0 / rank
            if sibling:
                s["sib"] += 1
        if args.llm_rerank and rr_stat["called"]:
            pass
        if args.auto:
            a = auto_accept(args.base, c["q"])
            auto_stat["called"] += 1
            if a.get("ok"):
                auto_stat["ok"] += 1
                if a.get("name") == c["target"] or a.get("name") in c["siblings"]:
                    auto_stat["right"] += 1
                else:
                    auto_stat["wrong"] += 1
    print()
    print("%-4s %4s %8s %8s %8s %6s" % ("cat", "n", "rank1", "recall@%d" % args.n, "mrr", "sib"))
    tot = {"n": 0, "rank1": 0, "hit3": 0, "mrr": 0.0}
    for cat in ("D", "P", "S", "X"):
        s = per_cat.get(cat)
        if not s:
            continue
        print("%-4s %4d %7.2f%% %7.2f%% %8.3f %6d"
              % (cat, s["n"], 100.0 * s["rank1"] / s["n"], 100.0 * s["hit3"] / s["n"],
                 s["mrr"] / s["n"], s["sib"]))
        if cat != "X":                       # X 不计入可达上限
            for k in ("n", "rank1", "hit3", "mrr"):
                tot[k] += s[k]
    if tot["n"]:
        print("%-4s %4d %7.2f%% %7.2f%% %8.3f"
              % ("D+P+S", tot["n"], 100.0 * tot["rank1"] / tot["n"],
                 100.0 * tot["hit3"] / tot["n"], tot["mrr"] / tot["n"]))
        print("\n可达上限口径 = D+P+S（X 单列，不计入）")
        if args.llm_rerank and rr_stat["called"]:
            pass
        if args.auto:
            print("/auto：调用 %d，接受 %d（%.0f%%），其中选对 %d、选错 %d"
                  % (auto_stat["called"], auto_stat["ok"],
                     100.0 * auto_stat["ok"] / max(1, auto_stat["called"]),
                     auto_stat["right"], auto_stat["wrong"]))
        if args.llm_rerank and rr_stat["called"]:
            ms = sorted(rr_stat["ms"])
            p50 = ms[len(ms) // 2] if ms else 0
            p95 = ms[min(len(ms) - 1, int(len(ms) * 0.95))] if ms else 0
            print("/llm_rerank：调用 %d，成功 %d（%.0f%%），触发重排 %d，"
                  "修正 %d，弄坏 %d，延迟 p50 %dms / p95 %dms"
                  % (rr_stat["called"], rr_stat["ok"],
                     100.0 * rr_stat["ok"] / max(1, rr_stat["called"]),
                     rr_stat["reordered"], rr_stat["fixed"], rr_stat["broke"], p50, p95))
        if args.gate:
            got = tot["rank1"] / tot["n"]
            ok = got >= GATE_RANK1
            print("闸门：rank1 %.3f %s %.2f ⇒ %s"
                  % (got, ">=" if ok else "<", GATE_RANK1, "PASS" if ok else "FAIL"))
            return 0 if ok else 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
