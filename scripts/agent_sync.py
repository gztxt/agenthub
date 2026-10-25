#!/usr/bin/env python3
"""agent_sync.py — 新增 agent 接入后的「四面对齐」复验，也是现有 agent 的一键体检。

**为什么有它**（2026-10-25 用户令「后续有添加 agent 可以自动注册、同步所有功能，
也可以复验现有 agent 模块功能」）：
自动注册已经由 `agentscan.py` + `all_profiles()` 做了（装上新 CLI，10 分钟 TTL 内自己
出卡）。但「出了卡」不等于「四个功能面都通了」——今天连着四次报障都出在这条缝里：
dsh 有卡没原生界面按钮、zcode 有卡但扫描判 tui_only、reasonix 有按钮但生产 PATH
找不到 node、qwenpaw 有按钮但探活打的是常驻代理永远显示「活着」。
所以本脚本不复述注册逻辑，只做**对齐判据**：同一批 agent 在四面（菜单 / 模型设置 /
原生界面 / 终端）各自是什么状态，哪一面缺、缺的原因是否已经写在卡面上。

**判据（FAIL 级 = 用户在界面上会遇到「该有的没有」）**
1. `ui_state=missing_recipe` → FAIL。既没配方也没标 TUI-only，属漏登记（不是 TUI-only）。
2. `ui_state=ok` 但 entries 里没有 embed 也没有 open → FAIL。有配方没按钮。
3. `ui_state=tui_only` 但 `ui_note` 为空 → FAIL。静默少按钮，用户不知道原因。
4. 装了（installed/running）却既无 term 也无 embed/open → FAIL。卡不可用。
5. 菜单有卡但 `/api/settings/models` 没这一行 → WARN。模型设置未接入（可能是 TUI 无
   `--model`，如实置灰也行；完全没有行才是缺口）。

**WARN 不是失败**：`ui_state=ok` 且 entries 有 embed/open 但 entry 不带 `start`，
说明点开不会自动拉起（按需拉起政策下等于要用户先手动起服务），报 WARN 不拦推送。

**用法**
    python3 scripts/agent_sync.py                 # 人读矩阵
    python3 scripts/agent_sync.py --json          # 机器可读（CI / 技能里消费）
    python3 scripts/agent_sync.py --probe         # 额外打 ui-status（活体面，不拉起）
    python3 scripts/agent_sync.py --agent zcode   # 只看一张卡
    python3 scripts/agent_sync.py --base-url http://127.0.0.1:3199 --token-file ../.env

退出码：0 全对齐；1 有 FAIL；2 hub 不可达 / 无凭据。
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path
from typing import Dict, List, Optional, Tuple

ROOT = Path(__file__).resolve().parents[1]
UI_ENTRY_TYPES = ("embed", "open")


def _token(path: Optional[str]) -> str:
    """取 TERM_TOKEN：显式文件 > 环境变量 > 仓根 .env。取不到返回空串（只读接口多半仍可用）。"""
    if path:
        cand = Path(path)
    else:
        cand = ROOT / ".env"
    if cand.is_file():
        for line in cand.read_text(encoding="utf-8", errors="replace").splitlines():
            k, sep, v = line.partition("=")
            if sep and k.strip() == "TERM_TOKEN":
                return v.strip().strip('"').strip("'")
    return os.getenv("TERM_TOKEN", "")


def _get(base: str, path: str, token: str, timeout: float = 8.0) -> dict:
    req = urllib.request.Request(base.rstrip("/") + path)
    if token:
        req.add_header("x-hub-token", token)
    with urllib.request.urlopen(req, timeout=timeout) as r:      # noqa: S310 - 只连本机 hub
        return json.loads(r.read().decode("utf-8"))


def _issues_for(p: dict, in_models: bool) -> List[Tuple[str, str]]:
    """单卡判据。返回 [(level, code)]，level ∈ {FAIL, WARN}。"""
    out: List[Tuple[str, str]] = []
    pid = str(p.get("id") or "")
    state = str(p.get("ui_state") or "")
    note = str(p.get("ui_note") or "").strip()
    entries = p.get("entries") or []
    types = {e.get("type") for e in entries if isinstance(e, dict)}
    ui_entries = [e for e in entries if isinstance(e, dict) and e.get("type") in UI_ENTRY_TYPES]
    status = str(p.get("status") or "")

    if state == "missing_recipe":
        out.append(("FAIL", "missing_recipe"))     # ① 漏登记
    if state == "ok" and not ui_entries:
        out.append(("FAIL", "ui_no_button"))       # ② 有配方没按钮
    if state == "tui_only" and not note:
        out.append(("FAIL", "tui_no_reason"))      # ③ 静默少按钮
    if status in ("installed", "running") and "term" not in types and "embed" not in types:
        out.append(("FAIL", "card_unusable"))      # ④ 卡不可用
    if ui_entries and not any(e.get("start") for e in ui_entries) and status != "running":
        # 停着又没有 start ⇒ 点开只会在 iframe 里看到「未响应」。running 的不报：
        # 常驻豁免的卡（codebuddy）本来就不该带 start，报了只是噪音。
        out.append(("WARN", "ui_no_start"))
    if not in_models:
        out.append(("WARN", "models_missing"))     # ⑤ 模型页无此行
    if not pid:
        out.append(("FAIL", "no_id"))
    return out


def build_matrix(agents: List[dict], models: List[dict]) -> List[dict]:
    """纯函数：两个接口的原始返回 → 对齐矩阵。测试直接喂假数据。"""
    model_ids = {str(m.get("id")) for m in models if isinstance(m, dict)}
    rows = []
    for p in agents:
        if p.get("kind") != "agent":
            continue
        pid = str(p.get("id") or "")
        entries = p.get("entries") or []
        types = {e.get("type") for e in entries if isinstance(e, dict)}
        issues = _issues_for(p, pid in model_ids)
        rows.append({
            "id": pid,
            "name": p.get("name") or pid,
            "status": p.get("status"),
            "ui_state": p.get("ui_state"),
            "ui_note": p.get("ui_note") or "",
            "term": "term" in types,
            "embed": "embed" in types,
            "models": pid in model_ids,
            "issues": issues,
            "fails": [c for lv, c in issues if lv == "FAIL"],
            "warns": [c for lv, c in issues if lv == "WARN"],
        })
    rows.sort(key=lambda r: (not r["fails"], r["id"]))   # FAIL 排最前，看表先看见坏的
    return rows


def render(rows: List[dict], probes: Optional[Dict[str, dict]] = None) -> str:
    head = f"{'agent':13} {'status':10} {'界面':10} {'终端':5} {'模型':5} {'判定'}"
    lines = [head, "-" * len(head)]
    for r in rows:
        verdict = "FAIL " + ",".join(r["fails"]) if r["fails"] else (
            "WARN " + ",".join(r["warns"]) if r["warns"] else "ok")
        line = (f"{r['id'][:13]:13} {str(r['status'])[:10]:10} {str(r['ui_state'])[:10]:10} "
                f"{'Y' if r['term'] else '-':5} {'Y' if r['models'] else '-':5} {verdict}")
        if r["ui_state"] == "tui_only" and r["ui_note"]:
            line += f"  〔{r['ui_note'][:36]}〕"
        if probes and r["id"] in probes:
            pr = probes[r["id"]]
            line += f"  probe.ready={pr.get('ready')}"
        lines.append(line)
    n_fail = sum(1 for r in rows if r["fails"])
    n_warn = sum(1 for r in rows if r["warns"] and not r["fails"])
    lines.append("-" * len(head))
    lines.append(f"共 {len(rows)} 张 agent 卡：FAIL {n_fail} / WARN {n_warn} / ok {len(rows) - n_fail - n_warn}")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="agent 四面对齐复验（菜单/模型/界面/终端）")
    ap.add_argument("--base-url", default=os.getenv("HUB_BASE", "http://127.0.0.1:3102"))
    ap.add_argument("--token-file", default=None)
    ap.add_argument("--agent", default=None, help="只看一张卡（id）")
    ap.add_argument("--probe", action="store_true", help="额外打 /ui-status（不拉起服务）")
    ap.add_argument("--json", action="store_true")
    args = ap.parse_args(argv)

    token = _token(args.token_file)
    try:
        agents_raw = _get(args.base_url, "/api/agents", token)
        models_raw = _get(args.base_url, "/api/settings/models", token)
    except (urllib.error.URLError, OSError, ValueError) as e:
        print(f"hub 不可达或返回异常：{e}", file=sys.stderr)
        return 2

    agents = agents_raw if isinstance(agents_raw, list) else agents_raw.get("agents", [])
    models = (models_raw or {}).get("agents", []) if isinstance(models_raw, dict) else []
    if not models:
        print("警告：/api/settings/models 没有 agents 段，模型面判据不可用", file=sys.stderr)

    rows = build_matrix(agents, models)
    if args.agent:
        rows = [r for r in rows if r["id"] == args.agent]
        if not rows:
            print(f"没有这张卡：{args.agent}", file=sys.stderr)
            return 1

    probes: Dict[str, dict] = {}
    if args.probe:
        for r in rows:
            if r["ui_state"] != "ok":
                continue
            try:
                probes[r["id"]] = _get(args.base_url, f"/api/agents/{r['id']}/ui-status", token, 6.0)
            except (urllib.error.URLError, OSError, ValueError) as e:
                probes[r["id"]] = {"ready": None, "error": str(e)[:60]}

    if args.json:
        print(json.dumps({"rows": rows, "probes": probes}, ensure_ascii=False, indent=1))
    else:
        print(render(rows, probes or None))
    return 1 if any(r["fails"] for r in rows) else 0


if __name__ == "__main__":
    sys.exit(main())
