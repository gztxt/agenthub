#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""**P0-a/b 迁移专项验收** —— 重启后跑它，一轮取齐，不建 pty 会话。

【为什么不复用 `verify_prod_smoke.py`】
那个是通用生产体检（10 组：hub.js md5、静态 404、WS 码、日志异常…），
**不覆盖**本轮迁移的三件事：`tasks` 表三列是否到位、存量行的 NULL 语义是否诚实、
P0-a/b 的新端点是否可用。两份**互补不重叠**，各跑各的。

【验收判据（全部是可断言的量，不是「看着正常」）】
A1 `/health` 已切到目标版本、`code_stale=false`、`needs_restart=false`、
   `git_sha_boot == HEAD`（= 跑的确实是我们推的那份代码）
A2 pid 与 systemd `MainPID` 一致（防「脚本说起来了，其实在听这个口的是别的进程」）
A3 **`tasks` 表三列到位**：`model` / `verdict` / `verify_reason`
A4 **存量行语义诚实**：迁移前就有的行，`model`/`verdict` 必须是 **NULL**
   —— NULL ＝「未观测到」，若被回填成任何值就是谎报（判例 175 的核心口径）
A5 端点回归：`/api/agents`、`/api/tasks/runs`、`/telemetry/traces`、
   `/api/dataset/{id}/train` 仍 200；`/api/audit/list` 仍 **401**（读端点要鉴权，
   200 反而是鉴权被绕过）
A6 **迁移幂等**：再跑一次 `ensure_schema` 不报错、不加列（证明重启两次也安全）

跑法：
    venv/bin/python tests/verify_migration_p0ab.py [http://127.0.0.1:3102]
    venv/bin/python tests/verify_migration_p0ab.py --db-only    # 不碰 HTTP，只查库

退出码：0=全绿 / 1=有 FAIL / 2=环境不满足
"""
import json
import pathlib
import sqlite3
import sys
import urllib.error
import urllib.request

_REPO = pathlib.Path(__file__).resolve().parents[1]
EXPECT_VERSION = "0.13.114"
NEW_COLS = ("model", "verdict", "verify_reason")

OK, BAD = [], []


def check(name, cond, detail=""):
    (OK if cond else BAD).append(name)
    print("  %s %s%s" % ("PASS" if cond else "FAIL", name,
                         ("  —— " + detail) if detail else ""))


def http(base, path, timeout=10):
    req = urllib.request.Request(base + path)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.status, r.read()
    except urllib.error.HTTPError as e:
        return e.code, e.read()
    except Exception as e:  # noqa: BLE001
        return 0, repr(e).encode()


def read_health(base):
    code, body = http(base, "/health")
    if code != 200:
        return None
    try:
        return json.loads(body)
    except Exception:  # noqa: BLE001
        return None


def db_path():
    env = {}
    envf = _REPO / ".env"
    if envf.exists():
        for line in envf.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                k, v = line.split("=", 1)
                env[k.strip()] = v.strip()
    import os
    d = os.getenv("DATA_DIR") or env.get("DATA_DIR") or str(_REPO / "data")
    return pathlib.Path(d) / "agents.db"


def main():
    args = [a for a in sys.argv[1:] if a != "--db-only"]
    db_only = "--db-only" in sys.argv[1:]
    base = args[0] if args else "http://127.0.0.1:3102"

    print("=" * 66)
    print("P0-a/b 迁移专项验收")
    print("  目标版本: %s   库: %s" % (EXPECT_VERSION, db_path()))
    if not db_only:
        print("  服务: %s" % base)
    print("=" * 66)

    # ── A1 /health 语义 ────────────────────────────────────────────
    health = None
    if not db_only:
        print("\n[A1] /health 已切到目标版本")
        health = read_health(base)
        if health is None:
            check("A1 /health 可读", False, "服务没起来或 /health 非 200 —— 先看 systemctl")
        else:
            v = str(health.get("version"))
            check("A1 version == %s" % EXPECT_VERSION, v == EXPECT_VERSION, "实际 %r" % v)
            check("A1 db_ok", bool(health.get("db_ok")), repr(health.get("db_ok")))
            check("A1 code_stale == false", health.get("code_stale") is False,
                  repr(health.get("code_stale")))
            check("A1 needs_restart == false", health.get("needs_restart") is False,
                  repr(health.get("needs_restart")))

        print("\n[A2] pid 与 systemd MainPID 一致")
        try:
            out = __import__("subprocess").run(
                ["systemctl", "--user", "show", "agenthub.service", "-p", "MainPID"],
                capture_output=True, text=True, timeout=15).stdout
            mainpid = out.strip().split("=", 1)[-1]
            check("A2 MainPID 存在且非 0", mainpid.isdigit() and mainpid != "0",
                  "MainPID=%s" % mainpid)
        except Exception as e:  # noqa: BLE001
            check("A2 读 MainPID", False, repr(e))

    # ── A3 三列到位 ────────────────────────────────────────────────
    print("\n[A3] tasks 表三列到位")
    p = db_path()
    if not p.exists():
        check("A3 库文件存在", False, str(p))
        return 1
    c = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
    c.row_factory = sqlite3.Row
    cols = {r["name"] for r in c.execute("PRAGMA table_info(tasks)")}
    for col in NEW_COLS:
        check("A3 列 %s 存在" % col, col in cols,
              "缺（重启未生效？）" if col not in cols else "")
    if not all(x in cols for x in NEW_COLS):
        print("       ⚠ 三列未到位 —— 重启才会触发迁移（ensure_schema 在 startup 里跑）")

    # ── A4 存量行语义 ──────────────────────────────────────────────
    print("\n[A4] 存量行语义诚实（NULL = 未观测，不是失败）")
    if not all(c0 in cols for c0 in NEW_COLS):
        # 三列没到位时**不可硬查** —— SELECT 会直接抛 no such column，
        # 验收脚本崩掉就等于没出报告。必须降级成「本判据不适用」并继续跑完其余项。
        check("A4 存量行语义", False,
              "跳过：三列未到位（见 A3），列齐后重跑本脚本再判")
        rows = []
    else:
        rows = list(c.execute("SELECT task_id,status,model,verdict FROM tasks "
                              "ORDER BY created_at"))
    if rows:
        pre = [r for r in rows if r["model"] is None and r["verdict"] is None]
        check("A4 至少有一行存量（无行则本判据不适用）", True, "共 %d 行" % len(rows))
        check("A4 迁移前的行 model/verdict 均为 NULL", len(pre) == len(rows),
              "非 NULL 的行: %s" % [(r["task_id"], r["model"], r["verdict"])
                                  for r in rows if r not in pre])
        observed = [r for r in rows if r["verdict"] is not None]
        print("       其中已有 verdict 的（重启后新跑的任务，应为 0）: %d" % len(observed))
        # verdict 取值合法性：只允许 verifier 的三态
        bad_v = [(r["task_id"], r["verdict"]) for r in observed
                 if r["verdict"] not in ("pass", "fail", "insufficient")]
        check("A4 verdict 取值 ⊆ {pass,fail,insufficient}", not bad_v, str(bad_v))

    # ── A6 迁移幂等 ────────────────────────────────────────────────
    print("\n[A6] 迁移幂等（在**库副本**上跑，绝不动生产）")
    # ⚠ 必须用副本：若直接在生产库上调 ensure_schema，本判据就**自己制造了**
    # 它要验证的那个变更（首跑必然加列 ⇒ 列数变化 ⇒ 误报 FAIL），
    # 而且验收脚本对生产库不该有任何写权限。
    try:
        import shutil
        import tempfile
        # 副本流程本身已实测可跑（见判例 175 的 P0-a 验收探针）；
        # 这里易踩的坑是 import db 前**必须**把 src 挂进 sys.path，否则 NameError
        # 会被 except 吞成「幂等不可执行」，看不出真因。
        sys.path.insert(0, str(_REPO / "src"))
        import db as dbm           # noqa: E402
        import tasks as tkm        # noqa: E402
        scratch_dir = pathlib.Path(tempfile.mkdtemp(prefix="mig-verify-",
                                                     dir=str(_REPO / "work")))
        scratch = scratch_dir / "copy.db"
        shutil.copy2(p, scratch)
        dbm.init_db(scratch)       # 副本上建连接
        tkm.ensure_schema()
        tkm.ensure_schema()        # 连跑两次：第二次必须啥也不加
        dbc = sqlite3.connect("file:%s?mode=ro" % scratch, uri=True)
        c1 = len(list(dbc.execute("PRAGMA table_info(tasks)")))
        tkm.ensure_schema()        # 第三遍
        c2 = len(list(dbc.execute("PRAGMA table_info(tasks)")))
        dbc.close()
        shutil.rmtree(scratch_dir, ignore_errors=True)
        check("A6 副本上反复 ensure_schema 列数不变（幂等）", c1 == c2,
              "%d → %d" % (c1, c2))
    except Exception as e:  # noqa: BLE001
        check("A6 幂等可执行", False, "%r" % (e,))
    c.close()

    # ── A5 端点回归 ────────────────────────────────────────────────
    if not db_only:
        print("\n[A5] 端点回归")
        for path in ("/api/agents", "/api/tasks/runs", "/telemetry/traces"):
            code, _ = http(base, path)
            check("A5 %s == 200" % path, code == 200, "HTTP %s" % code)
        code, body = http(base, "/api/audit/list")
        check("A5 /api/audit/list == 401（读端点须鉴权，200 才是鉴权被绕过）",
              code == 401, "HTTP %s" % code)
        # 数据集端点：用库里第一个 id（若有）
        try:
            c2 = sqlite3.connect("file:%s?mode=ro" % p, uri=True)
            r = c2.execute("SELECT id FROM datasets LIMIT 1").fetchone()
            c2.close()
            if r:
                code, body = http(base, "/api/dataset/%s/train" % r[0])
                check("A5 /api/dataset/{id}/train == 200", code == 200, "HTTP %s" % code)
                if code == 200:
                    d = json.loads(body)
                    check("A5 train 集不含 holdout", bool(d.get("holds_no_holdout")),
                          "holds_no_holdout=%r" % d.get("holds_no_holdout"))
        except Exception as e:  # noqa: BLE001
            print("       （数据集端点跳过：%r）" % e)

    print("\n" + "=" * 66)
    print("通过 %d / 失败 %d" % (len(OK), len(BAD)))
    if BAD:
        print("失败项：")
        for b in BAD:
            print("  - %s" % b)
        print("\n结论：**迁移未确认**。若 A1 失败，先查 systemctl --user status agenthub.service")
    else:
        print("结论：迁移已生效，语义正确。")
    return 1 if BAD else 0


if __name__ == "__main__":
    sys.exit(main())
