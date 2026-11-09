#!/usr/bin/env python3
"""L2 · PT-20261110-01 端到端复核：真 stdio MCP 子进程 + 真映射 + 真调用。

跑法（仓根）：venv/bin/python3 tests/verify_mcp_sources.py
可选 `MCP_VERIFY_NET=1` 追加一次官方 Registry 真抓取（公网只读、未鉴权、单轮 ≤2 次）。

**不占端口、不写生产 db（tmp 文件库）、不重启任何服务、不改任何 agent 配置**。
外件只有本仓自带的 `tests/mcp_demo_server.py`（本地 stdio 子进程，零网络零模型请求）。
必须用真 server 而不是 mock：`inputSchema` 透传、tools/list、call_tool 的成败
只有真握手才验得出来。

为什么这一层不能省：L0 把 `_session_for` 换成了替身，能证明「逻辑对」但证明不了
「mcp SDK 真的连得上、真调一次有回包」。两者都绿才算链路通。
"""
import asyncio
import json
import os
import pathlib
import shutil
import sys
import tempfile

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db            # noqa: E402
import mcpgw         # noqa: E402
import mcpregistry   # noqa: E402
from fastapi import HTTPException  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="verifymcpsrc-"))
db.init_db(_TMP / "agents.db")
mcpgw.ensure_schema()

_PY = "/fs/1000/ftp/技术文档/agenthub/venv/bin/python"
_DEMO = str(_REPO / "tests" / "mcp_demo_server.py")
_RESULTS = []


def check(name, ok, detail=""):
    _RESULTS.append((name, bool(ok), detail))
    print(("  PASS  " if ok else "  FAIL  ") + name + (f"   {detail}" if detail else ""))


def stub_request():
    state = type("S", (), {"actor": "verify"})()
    return type("R", (), {"state": state})()


#: 全程**一个**事件循环。`asyncio.run()` 每次新建并拆掉 loop，而 mcp SDK 的
#: `stdio_client` 用 anyio cancel scope 管子进程，跨 loop 反复拆会抛 CancelledError
#: （脚本层的假红，不是被测链路的故障）。生产本来就是一个长驻 loop。
_LOOP = asyncio.new_event_loop()


def run(coro):
    return _LOOP.run_until_complete(coro)


# ── 1. 真 stdio MCP server：注册 → probe → 聚合 → 真调 → 记账 ──────────────
try:
    res = run(mcpgw.add_server(mcpgw.ServerIn(
        name="demo", transport="stdio", command=_PY, args=[_DEMO],
        description="hub 自带 echo/now demo server"), stub_request()))
    sid = res["id"]
    check("add_server 真注册", res.get("status") == "added", "id=%s" % sid)

    probe = run(mcpgw.probe_server(mcpgw.ServerProbe(
        transport="stdio", command=_PY, args=[_DEMO])))
    names = sorted(t["name"] for t in probe["tools"])
    check("probe 真列工具（stdio）", names == ["echo", "now"], str(names))

    agg = run(mcpgw.aggregated_tools())
    check("aggregated_tools 真聚合", agg["count"] >= 2 and not agg["degraded"],
          "count=%d degraded=%s" % (agg["count"], agg["degraded"]))
    schemas = [t for t in agg["tools"] if t.get("inputSchema") is not None]
    check("inputSchema 真透传", len(schemas) >= 2,
          "%d/%d 带 schema" % (len(schemas), agg["count"]))

    call = run(mcpgw.mcp_call(mcpgw.CallIn(
        server="demo", tool="echo", args={"text": "hello-mcp"},
        agent_id="manager")))
    check("mcp_call 真回包", call.get("ok") and "hello-mcp" in (call.get("content") or ""),
          (call.get("content") or call.get("error") or "")[:80])
    check("幂等键与 call_id 齐", bool(call.get("call_id")) and bool(call.get("idempotency_key")))

    row = db.query("SELECT use_count,last_used_at FROM mcp_servers WHERE id=?", (sid,))[0]
    check("use_count/last_used_at 回写", int(row["use_count"] or 0) == 1
          and bool(row["last_used_at"]),
          "use_count=%s last_used=%s" % (row["use_count"], row["last_used_at"]))
except Exception as e:  # noqa: BLE001
    check("真 stdio 链路", False, "%s: %s" % (type(e).__name__, e))

# ── 2. server.json 映射 → install（真落 mcp_servers，凭据零落库）────────────
try:
    card = {
        "name": "com.pulsemcp/remote-filesystem", "version": "0.1.5",
        "description": "MCP server for remote filesystem operations on cloud storage",
        "packages": [{
            "registryType": "npm", "identifier": "remote-filesystem-mcp-server",
            "version": "0.1.5", "runtimeHint": "npx",
            "transport": {"type": "stdio"},
            "runtimeArguments": [{"type": "positional", "value": "-y"}],
            "environmentVariables": [
                {"name": "GCS_BUCKET", "isRequired": True},
                {"name": "GCS_PRIVATE_KEY", "isSecret": True},
                {"name": "GCS_MAKE_PUBLIC", "default": "false"}]}]}
    spec = mcpregistry.map_server_json(card)
    check("npm 映射真形状", spec["command"] == "npx"
          and spec["args"] == ["-y", "remote-filesystem-mcp-server"], str(spec["args"]))
    check("isSecret 零明文", "GCS_PRIVATE_KEY" in spec["required_secrets"]
          and "GCS_PRIVATE_KEY" not in spec["env"]
          and "PRIVATE_KEY" not in json.dumps(spec["env"]))
    ck = "verify-npm"
    db.execute(
        "INSERT INTO mcp_source_cache(cache_key,source_id,name,version,title,description,"
        "tags,server_json,status,is_latest,fetched_at,transport_hint,has_secret)"
        " VALUES(?,?,?,?,?,?,'[]',?,'active',1,?,'stdio',1)",
        (ck, mcpregistry.DEFAULT_SOURCE_ID, card["name"], card["version"],
         card["name"], card["description"], json.dumps(card),
         "2026-11-10T00:00:00+00:00"))
    inst = run(mcpgw.install_server(mcpgw.InstallIn(cache_key=ck), stub_request()))
    check("install 真落库", inst.get("installed") is True, "id=%s" % inst.get("id"))
    srow = db.query("SELECT * FROM mcp_servers WHERE id=?", (inst["id"],))[0]
    check("来源列真持久化", srow["registry_name"] == card["name"]
          and srow["trust"] == "official" and srow["version"] == "0.1.5")
    check("env 列无凭据值", "GCS_PRIVATE_KEY" not in (srow["env"] or "{}")
          and "false" in (srow["env"] or "{}"))
    dedup = run(mcpgw.install_server(mcpgw.InstallIn(cache_key=ck), stub_request()))
    check("install 幂等", dedup.get("status") == "deduped")
    # 这张卡来自真实 npm 包；本层不联网，故装完立刻停用（= 「已装但未探测通过」），
    # 否则后面 /mcp/run → aggregated_tools() 会真去 `npx -y` 拉进程，把 hermetic 层
    # 变成一次外网安装。生产上这一步应由 probe 结果决定 enabled。
    db.execute("UPDATE mcp_servers SET enabled=0 WHERE id=?", (inst["id"],))
except Exception as e:  # noqa: BLE001
    check("映射/install 链路", False, "%s: %s" % (type(e).__name__, e))

# ── 3. 高德 `?key=` 真拆解（凭据不进 url 列）─────────────────────────────
try:
    clean, names = mcpregistry.split_credential_url(
        "https://mcp.amap.com/mcp?key=REAL_SECRET_123&city=hangzhou")
    check("高德 key 真拆占位", "REAL_SECRET_123" not in clean
          and "${KEY}" in clean and "city=hangzhou" in clean, clean)
    resolved, missing = mcpregistry.resolve_url(clean, {"KEY": "abc"})
    check("运行时注入真替换", not missing and "abc" in resolved
          and "REAL_SECRET_123" not in resolved, resolved)
except Exception as e:  # noqa: BLE001
    check("凭据拆解", False, str(e))

# ── 4. /mcp/run：plan 零写盘、exec 真调 ──────────────────────────────────
try:
    before = len(db.query("SELECT * FROM mcp_servers"))
    plan = run(mcpgw.mcp_run(mcpgw.RunIn(task="需要回声工具", mode="plan"), stub_request()))
    check("run plan 零写盘", plan["mode"] == "plan" and not plan["calls"]
          and not plan["installed"]
          and len(db.query("SELECT * FROM mcp_servers")) == before
          and "dry" in plan["note"])
    exe = run(mcpgw.mcp_run(mcpgw.RunIn(
        task="回声", mode="exec", server="demo", tool="echo",
        call_args={"text": "run-e2e"}, agent_id="manager"), stub_request()))
    hit = exe["calls"][0] if exe["calls"] else {}
    check("run exec 真调回结果", hit.get("ok") is True
          and "run-e2e" in (hit.get("result", {}).get("content") or ""),
          (hit.get("result", {}).get("content") or hit.get("error") or "")[:80])
    check("run 门面五段齐", all(k in exe for k in
          ("discovered", "installed", "selected", "calls", "degraded")))
except Exception as e:  # noqa: BLE001
    check("/mcp/run 链路", False, "%s: %s" % (type(e).__name__, e))

# ── 5. 可选：官方 Registry 真抓一次（单轮 ≤2 次，公网只读）────────────────
if os.getenv("MCP_VERIFY_NET") == "1":
    try:
        r = mcpregistry.sync_source(mcpregistry.DEFAULT_SOURCE_ID,
                                    search="filesystem", force=True)
        rows = db.query("SELECT * FROM mcp_source_cache ORDER BY fetched_at DESC LIMIT 3")
        check("官方 Registry 真抓取", r["fetched"] > 0 and not r["degraded"],
              "fetched=%d pages=%d err=%s" % (r["fetched"], r["pages"], r["last_error"]))
        check("真卡落缓存且带 server.json", all(
            (x["server_json"] or "").startswith("{") for x in rows))
        cat = mcpregistry.catalog("filesystem", n=3)
        check("真语料可检索", cat["selected"] > 0, "considered=%d" % cat["considered"])
    except Exception as e:  # noqa: BLE001
        check("官方 Registry 真抓取", False, "%s: %s" % (type(e).__name__, e))
else:
    print("  SKIP  官方 Registry 真抓取（MCP_VERIFY_NET 未设；本层保持 hermetic）")

# ── 汇总 ─────────────────────────────────────────────────────────────────
_LOOP.close()
try:
    db._conn.close()
except Exception:  # noqa: BLE001
    pass
shutil.rmtree(_TMP, ignore_errors=True)
bad = [n for n, ok, _ in _RESULTS if not ok]
print("\n%d 项，%d 通过，%d 失败" % (len(_RESULTS), len(_RESULTS) - len(bad), len(bad)))
if bad:
    print("失败项：" + ", ".join(bad))
sys.exit(1 if bad else 0)
