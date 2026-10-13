#!/usr/bin/env python3
"""P1-7 扩展 · 写端点闸门活体探针。

   两种模式，差别就是"敢不敢在生产上跑"：

   A `--safe`（**生产可跑**）：只打**无凭据**那一腿。401 由中间件在 handler 之前返回，
     可证明没执行任何业务代码 ⇒ 零副作用。带凭据去逐条打写端点**绝不在这条模式里做**：
     09-23 就是这么把用户手写的 L2 记忆覆盖掉的（`POST /api/memory/l2/rebuild` 回 200）。
   B 默认（影子树，独立空库）：无凭据 401 + **带凭据非 401** 两腿都打。
     缺了后者就是假绿 —— 一个"把所有请求都返 401"的闸门也能让 A 全绿。

   红基线（09-23 生产实测，改动前）：34 条写路由 {422:16, 404:8, 400:2, 200:6, 401:2}
   ⇒ 只有 2 条拒了，6 条对匿名写直接办成。
"""
import argparse
import ast
import json
import re
import subprocess
import sys
import urllib.error
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO / "tests"))
import writeauth                                        # noqa: E402


def write_routes():
    out = []
    for f in sorted((REPO / "src").glob("*.py")):
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in ("post", "put", "patch", "delete")):
                        if d.args and isinstance(d.args[0], ast.Constant):
                            out.append((d.func.attr.upper(), d.args[0].value, f.name[:-3]))
    return sorted(out, key=lambda r: r[1])


def hit(base, method, path, token=None, timeout=10):
    path = re.sub(r"\{[^}]+\}", "zznope", path)          # 占位换成不存在的值
    h = {"Content-Type": "application/json"}
    if token:
        h["x-hub-token"] = token
    req = urllib.request.Request(base + path, method=method, data=b"{}", headers=h)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status
    except urllib.error.HTTPError as e:
        return e.code
    except Exception:                                    # noqa: BLE001
        return 0


# 豁免端点的"自带鉴权"预期：它们不归本闸门，但必须自己拒
# 豁免端点的"自带鉴权"预期。503 也算通过：`/api/settings/term-token` 在未配置
# HUB_PASSCODE 时**主动拒绝服务**（fail-closed），影子树正是这种情况 —— 把它记成
# 失败就会诱导后人"为了让探针绿而去给影子塞口令"，那是把正确的保守行为改坏。
EXPECT_OWN = {"/api/settings/term-token": (401, 503), "/telemetry/events/": (401, 422, 400, 403)}

# 带凭据那一腿只打"空 body 必然进不去业务逻辑"的安全端点（影子树里也一样保守）
SAFE_WITH_TOKEN = ["/api/agents", "/api/memory/l1", "/mcp/call", "/mcp/servers",
                   "/api/jobs", "/api/tasks/decompose", "/api/agents/zznope/chat"]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("base", nargs="?", default="http://127.0.0.1:3102")
    ap.add_argument("--safe", action="store_true", help="只打无凭据腿（生产可用）")
    ap.add_argument("--token", default="", help="影子模式必给，用于带凭据对照腿")
    a = ap.parse_args()
    routes = write_routes()
    gated = [r for r in routes if writeauth.exempt_reason(r[1]) is None]
    exempt = [r for r in routes if writeauth.exempt_reason(r[1]) is not None]
    print(f"  目标 {a.base}   写路由 {len(routes)} 条：闸门覆盖 {len(gated)}，豁免 {len(exempt)}")
    fails = []

    def check(name, ok, detail=""):
        print(("  PASS  " if ok else "  FAIL  ") + name + (f"   [{detail}]" if detail else ""))
        if not ok:
            fails.append(name)

    print("\n[1] 无凭据 ⇒ 必须 401（中间件在 handler 之前短路，可证明零副作用）")
    codes = {}
    for m, p, mod in gated:
        c = hit(a.base, m, p)
        codes[c] = codes.get(c, 0) + 1
        if c != 401:
            fails.append(f"{m} {p} → {c}")
    print(f"    覆盖 {len(gated)} 条，状态码分布 {codes}")
    check(f"全部 {len(gated)} 条被闸门拒（改前实测：34 条里只有 2 条拒、6 条匿名办成）",
          not [f for f in fails if "→" in f],
          "" if not [f for f in fails if "→" in f] else str([f for f in fails if "→" in f][:4]))

    print("\n[2] 豁免端点必须**自带**鉴权（否则豁免＝免检洞）")
    for m, p, mod in exempt:
        c = hit(a.base, m, p)
        want = next((w for pre, w in EXPECT_OWN.items() if p.startswith(pre)), (401,))
        # /telemetry/events/{source} 在 hook.py 里"未设 HOOK_AUTH_TOKEN 时只允许回环"⇒ 本机探测可能 422
        check(f"{m} {p} 由 {mod} 自拒（期望 {want}）", c in want, f"实得 {c}")

    if a.safe:
        print("\n[3] 带凭据对照腿：--safe 模式**刻意跳过**（会在真实数据上产生副作用）")
        print("    需要它请对影子树跑默认模式（独立空库）。")
    else:
        if not a.token:
            print("  影子模式必须给 --token（否则对照腿没意义）"); return 2
        print("\n[3] 带凭据 ⇒ 必须**非 401**（证明闸门不是一刀切拒所有请求）")
        for m, p, mod in routes:
            if p not in SAFE_WITH_TOKEN:
                continue
            c = hit(a.base, m, p, token=a.token)
            check(f"{m} {p} 带凭据后进到业务层（非 401）", c != 401, f"实得 {c}")

    print("\n" + ("全部通过 ✅" if not fails else f"失败 {len(fails)} 项 ❌：{fails[:6]}"))
    return 0 if not fails else 1


if __name__ == "__main__":
    sys.exit(main())
