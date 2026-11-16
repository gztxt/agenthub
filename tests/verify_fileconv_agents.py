#!/usr/bin/env python3
"""闸门：「文件转换」实体必须在运行中的 /api/agents 里完整在册且走 hub 桥。

为什么不能只 grep profiles.py：本仓同族教训两见——规则/挂载点写错不报错，只是安静地
不生效（v0.13.74 .home-title、v0.13.77 .badge vs .nav-badge）。且 agent_sync.py 的
rows 只收 kind=agent，基础设施实体根本不在它的覆盖面（实测 rows=18、fileconv 缺席），
所以必须另有一道落在**真实回包**上的断言：
  A 在册：id=fileconv / kind=convert / port=8085 / endpoint 只绑回环 / auth_required=False
  B 入口：entries 同时有 open 与 embed，URL 指向 hub 桥端口（>=3200、不是裸 8085）
          —— 直连裸 8085 会绕过 hub 会话，等于出现第二套登录面
  C 正对照：同一次回包里能读到 filebrowser，证明查询不是「搜不到就当通过」
  D 红对照：拿 filebrowser 冒充、以及不存在的 id，都必须判红，证明闸门能 FAIL
  E 活体：:8085/health 回 ok=true（转换服务真在跑）
形态照 tests/verify_*.py：chk() / PASS-FAIL / n/n PASS / exit 0|1，__main__ 独立跑，
不带模块级 test_ 函数 ⇒ 不会被 run_tier.py 收进 L0（hermetic 无需活 hub）。

用法：venv/bin/python3 tests/verify_fileconv_agents.py [http://127.0.0.1:3102]
"""
import json
import sys
import urllib.request

BASE = sys.argv[1] if len(sys.argv) > 1 else "http://127.0.0.1:3102"
BRIDGE_MIN = 3200
res = []


def chk(name, ok, detail=""):
    res.append(bool(ok))
    print("  %-4s %-46s %s" % ("PASS" if ok else "FAIL", name, detail))


def get(url):
    with urllib.request.urlopen(urllib.request.Request(url), timeout=8) as r:
        return json.loads(r.read().decode("utf-8"))


def probe(entity):
    """同一套判据施加到任意一张卡：返回 (ok, 明细)，供正例与红对照共用。"""
    if entity is None:
        return False, "卡不存在"
    entries = entity.get("entries") or []
    urls = [e.get("url", "") for e in entries if e.get("type") in ("open", "embed")]
    bridged = []
    for u in urls:
        try:
            port = int(u.split(":")[2].split("/")[0])
        except (IndexError, ValueError):
            continue
        bridged.append(port >= BRIDGE_MIN and port != 8085)
    ok = (entity.get("kind") == "convert"
          and entity.get("port") == 8085
          and str(entity.get("endpoint", "")).startswith("http://127.0.0.1:8085")
          and entity.get("auth_required") is False
          and any(e.get("type") == "open" for e in entries)
          and any(e.get("type") == "embed" for e in entries)
          and len(bridged) == 2 and all(bridged))
    return ok, "kind=%s port=%s urls=%s" % (entity.get("kind"), entity.get("port"), urls)


def main():
    agents = get(BASE + "/api/agents")
    rows = agents if isinstance(agents, list) else (agents.get("agents") or [])
    by_id = {a.get("id"): a for a in rows}

    f = by_id.get("fileconv")
    ok, detail = probe(f)
    chk("A fileconv 在册且字段正确", ok, detail)
    urls = [e.get("url", "") for e in (f.get("entries") or [])
            if e.get("type") in ("open", "embed")] if f else []
    ports = []
    for u in urls:
        try:
            ports.append(int(u.split(":")[2].split("/")[0]))
        except (IndexError, ValueError):
            ports.append(0)
    chk("B open/embed 都指向 hub 桥端口", len(ports) == 2 and all(
        p >= BRIDGE_MIN and p != 8085 for p in ports), str(urls))
    chk("C 正对照：同批回包含 filebrowser", "filebrowser" in by_id,
        "回包共 %d 张卡" % len(rows))
    red1, d1 = probe(by_id.get("filebrowser"))
    red2, d2 = probe(by_id.get("__fileconv_missing__"))
    chk("D 红对照：冒充卡/不存在 id 必须判红", (red1 is False and red2 is False),
        "filebrowser=%s missing=%s" % (red1, red2))
    try:
        h = get("http://127.0.0.1:8085/health")
        chk("E 活体：:8085/health ok=true", h.get("ok") is True, json.dumps(h))
    except Exception as e:
        chk("E 活体：:8085/health ok=true", False, str(e)[:100])

    print("\n%d/%d PASS" % (sum(res), len(res)))
    return 0 if all(res) else 1


if __name__ == "__main__":
    sys.exit(main())
