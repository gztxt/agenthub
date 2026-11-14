"""积分签到（v0.13.201）：AIAutomator 远程管理 —— A/B 两台 Windows 机器的只读投影 + 受控触发。

链路与取证（2026-11-14，PT-20261114-01）：
- AIAutomator（github.com/mcvea/AIAutomator, MIT, HEAD bbe47bd）是 **Windows 10/11 专用**：
  登录态来自本机官方桌面客户端文件、调度走 PowerShell 计划任务。本机 Linux 实跑
  `run daily --dry` 5/5 failed（无 Windows 登录态）⇒ **Linux 只作管理面，A/B 作执行面**。
- 机器清单唯一权威 = `~/.config/tailcat/connect-targets.tsv`（tailcat 已登记 A/B）。
  本模块**只读投影**，不复制、不改写（281 卡：只读不是客气，是正确性 —— 另立写者会造漂移）。
  hub 侧只存「哪台启用 + 通道类型 + 备注」这类本机元数据。
- 数据面 = Windows 侧 `contrib/aia_bridge.py`：`engine.run_task()` 已返回结构化 dict
  （上游 cli.py 只 print 成人读文本），bridge 用 sentinel 包 JSON 输出、`ensure_ascii=True`
  绕开 Windows OEM/GBK 控制台乱码。hub 解析 JSON，**不正则解析人读文本**。
- 通道 = D1 裁定「LAN 免密 SSH 主 + tailcat 兜底」：B 直连 192.168.5.99，A 经
  `tailcat-connect.sh --target`。已知边界：`tailcat --serve` 单连接 ⇒ **执行一律串行**（锁）。
- 安全：机器名严格正则、命令白名单、argv 数组 `shell=False`，用户输入绝不进 shell；
  登录态/token 只在 Windows 本机读，hub 不回传、不落库任何凭据。

三种「没有」必须分开（本仓反复判过的静默不可用）：
  offline    = ssh/隧道连不上（机器关机 / tailcat serve 未起）
  unreadable = 连上了但 bridge 没吐合法 JSON（文件缺失 / Python 报错 / 版本漂移）
  ok && 空   = 通了、结构正常、确实没数据

端点：
  GET    /api/checkin/overview   机器清单 + 每台最近一次结果（只读）
  GET    /api/checkin/runs      执行流水（只读）
  POST   /api/checkin/machines   启用/编辑机器元数据
  DELETE /api/checkin/machines/{name}  停用一个机器（不改 tailcat 清单）
  POST   /api/checkin/run        触发远程执行（mode=plan 默认 dry / exec 真跑）
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

import db

router = APIRouter(tags=["checkin"])

TAILCAT_TSV = Path.home() / ".config" / "tailcat" / "connect-targets.tsv"
TC_CONNECT = "/fs/1000/ftp/技术文档/tailcat/tailcat-connect.sh"
BRIDGE = r"C:\AIAutomator\contrib\aia_bridge.py"
BEGIN = "===AIA_JSON_BEGIN==="
END = "===AIA_JSON_END==="

#: 命令白名单 —— 只放行 bridge 的只读/幂等子命令，绝不拼自由文本
CMDS = ("probe", "list", "health", "credits", "totals", "run")
#: 机器名：tailcat 目标名形如 DESKTOP-QUU9CJT / WIN-20260130XPI
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONNECT_TIMEOUT = 8
_RUN_TIMEOUT = 120

#: 种子（来源：connect-targets.tsv + ~/.ssh/config，均为拓扑非凭据）。
#: 只登记「哪台装了 AIAutomator 且归 hub 管」；token/user 仍从 tsv 现读，不落库。
SEEDS = (
    {"name": "DESKTOP-QUU9CJT", "channel": "lan", "host": "192.168.5.99",
     "user": "Mac", "target": "DESKTOP-QUU9CJT", "note": "B 机 · LAN 免密 SSH"},
    {"name": "WIN-20260130XPI", "channel": "tailcat", "host": "",
     "user": "Administrator", "target": "WIN-20260130XPI", "note": "A 机 · tailcat 隧道"},
)

#: 单连接争用（SCRIPT-README §38）：同一时刻只允许一条远程执行
_run_lock = threading.Lock()


# ── 存储层（hub 侧元数据 + 执行流水；不复制机器清单） ──────────────────────────
def _ensure_tables() -> None:
    db.execute("CREATE TABLE IF NOT EXISTS checkin_machines ("
               "name TEXT PRIMARY KEY, channel TEXT NOT NULL, host TEXT NOT NULL DEFAULT '', "
               "user TEXT NOT NULL DEFAULT '', target TEXT NOT NULL DEFAULT '', "
               "enabled INTEGER NOT NULL DEFAULT 1, note TEXT NOT NULL DEFAULT '', "
               "created_at TEXT NOT NULL)")
    db.execute("CREATE TABLE IF NOT EXISTS checkin_runs ("
               "id TEXT PRIMARY KEY, machine TEXT NOT NULL, cmd TEXT NOT NULL, "
               "mode TEXT NOT NULL, state TEXT NOT NULL, ok INTEGER NOT NULL DEFAULT 0, "
               "payload TEXT NOT NULL DEFAULT '', error TEXT NOT NULL DEFAULT '', "
               "ts INTEGER NOT NULL)")


def _seed() -> None:
    """首次进页把 SEEDS 登记为启用（幂等）。不覆盖用户改过的 channel/host/note。"""
    _ensure_tables()
    for s in SEEDS:
        try:
            db.execute("INSERT OR IGNORE INTO checkin_machines"
                       "(name, channel, host, user, target, enabled, note, created_at) "
                       "VALUES(?,?,?,?,?,1,?,?)",
                       (s["name"], s["channel"], s["host"], s["user"],
                        s["target"], s["note"], str(int(time.time()))))
        except Exception:  # noqa: BLE001 — 存储层异常不该让整页 500
            return


def _machines() -> List[Dict[str, Any]]:
    """只读投影：hub 元数据 LEFT JOIN tailcat tsv（后者是机器清单唯一真源）。"""
    _seed()
    tsv: Dict[str, Dict[str, str]] = {}
    try:
        for line in TAILCAT_TSV.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 3 and parts[0]:
                tsv[parts[0]] = {"user": parts[2], "note": parts[3] if len(parts) > 3 else ""}
    except (OSError, UnicodeDecodeError):
        pass
    try:
        rows = db.query("SELECT * FROM checkin_machines ORDER BY name")
    except Exception:  # noqa: BLE001
        rows = []
    out = []
    for r in rows:
        t = tsv.get(r["name"])
        out.append({
            "name": r["name"], "channel": r["channel"], "host": r["host"],
            "user": r["user"], "target": r["target"], "enabled": bool(r["enabled"]),
            "note": r["note"],
            "registered": t is not None,          # 是否仍在 tailcat 清单里
            "tailcat_user": (t or {}).get("user", ""),
        })
    return out


# ── 远程执行层（纯逻辑，可 L0 单测） ────────────────────────────────────────
def build_argv(m: Dict[str, Any], cmd: str, dry: bool) -> List[str]:
    """组装 argv（shell=False）。命令与机器名都已校验，用户输入不进 shell。"""
    if cmd not in CMDS:
        raise ValueError("cmd not allowed: %s" % cmd)
    args = [cmd] + (["--dry"] if (cmd == "run" and dry) else [])
    remote = "python %s %s" % (BRIDGE, " ".join(args))
    if m.get("channel") == "tailcat":
        if not _NAME_RE.match(m.get("target", "")):
            raise ValueError("bad target")
        return ["bash", TC_CONNECT, "--target", m["target"], "--exec", remote]
    host, user = m.get("host", ""), m.get("user", "")
    if not re.match(r"^[A-Za-z0-9._:-]{1,128}$", host or ""):
        raise ValueError("bad host")
    if not _NAME_RE.match(user or ""):
        raise ValueError("bad user")
    return ["ssh", "-o", "BatchMode=yes", "-o",
            "ConnectTimeout=%d" % _CONNECT_TIMEOUT,
            "%s@%s" % (user, host), remote]


def parse_bridge(text: str) -> Dict[str, Any]:
    """从 sentinel 包里取 JSON。三种「没有」在这里分流，绝不把故障吞成空列表。"""
    i, j = text.find(BEGIN), text.find(END)
    if i < 0 or j < 0 or j <= i:
        return {"ok": False, "state": "unreadable",
                "error": "bridge-no-json", "raw_tail": text[-400:]}
    body = text[i + len(BEGIN):j].strip()
    try:
        data = json.loads(body)
    except (ValueError, TypeError) as e:
        return {"ok": False, "state": "unreadable",
                "error": "bridge-bad-json: %s" % e, "raw_tail": body[-400:]}
    if not isinstance(data, dict):
        return {"ok": False, "state": "unreadable", "error": "bridge-not-object"}
    if not data.get("ok"):
        return {"ok": False, "state": "unreadable",
                "error": data.get("error") or "bridge-ok-false", "data": data}
    return {"ok": True, "state": "ok", "data": data}


def run_remote(m: Dict[str, Any], cmd: str, mode: str) -> Dict[str, Any]:
    """串行执行一次，落流水。返回归一信封（ok/state/data/error）。"""
    if cmd not in CMDS:
        raise HTTPException(400, "cmd must be one of %s" % (list(CMDS),))
    if mode not in ("plan", "exec"):
        raise HTTPException(400, "mode must be plan|exec")
    dry = (mode == "plan") or (cmd != "run")
    argv = build_argv(m, cmd, dry)
    with _run_lock:                      # 单连接争用：绝不并发
        try:
            p = subprocess.run(argv, capture_output=True, text=True,
                                timeout=_RUN_TIMEOUT, shell=False)
        except subprocess.TimeoutExpired:
            res = {"ok": False, "state": "offline", "error": "timeout"}
        except OSError as e:
            res = {"ok": False, "state": "offline", "error": "spawn: %s" % e}
        else:
            if p.returncode != 0 and not (p.stdout or "").find(BEGIN) >= 0:
                res = {"ok": False, "state": "offline",
                       "error": "ssh-rc=%d %s" % (p.returncode, (p.stderr or "")[-300:])}
            else:
                res = parse_bridge(p.stdout or "")
    rid = uuid.uuid4().hex[:12]
    try:
        _ensure_tables()
        db.execute("INSERT INTO checkin_runs(id,machine,cmd,mode,state,ok,payload,error,ts) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (rid, m["name"], cmd, mode, res["state"], 1 if res["ok"] else 0,
                    json.dumps(res.get("data") or {}, ensure_ascii=False)[:8000],
                    str(res.get("error") or "")[:500], int(time.time())))
    except Exception:  # noqa: BLE001 — 流水写失败不影响本次结果
        pass
    return {**res, "machine": m["name"], "cmd": cmd, "mode": mode, "dry": dry,
            "run_id": rid, "ts": int(time.time())}


# ── 端点 ──────────────────────────────────────────────────────────────────
def _by_name(name: str) -> Optional[Dict[str, Any]]:
    if not _NAME_RE.match(name or ""):
        raise HTTPException(400, "bad machine name")
    return next((m for m in _machines() if m["name"] == name), None)


@router.get("/api/checkin/overview")
def api_overview():
    """机器清单 + 每台最近一次执行结果（只读，不主动连远程）。"""
    ms = _machines()
    try:
        _ensure_tables()
        rows = db.query("SELECT machine, state, ok, ts FROM checkin_runs ORDER BY ts DESC LIMIT 200")
    except Exception:  # noqa: BLE001
        rows = []
    last: Dict[str, Any] = {}
    for r in rows:
        last.setdefault(r["machine"], r)
    return {"ok": True, "machines": ms, "last": last,
            "tailcat_tsv": str(TAILCAT_TSV), "bridge": BRIDGE,
            "read_only_note": "机器清单读 tailcat connect-targets.tsv；hub 只管启用态与执行流水，不复制清单、不回传登录态",
            "as_of": int(time.time())}


@router.get("/api/checkin/runs")
def api_runs(limit: int = 30):
    limit = max(1, min(200, limit))
    try:
        _ensure_tables()
        rows = db.query("SELECT * FROM checkin_runs ORDER BY ts DESC LIMIT ?", (limit,))
    except Exception:  # noqa: BLE001
        rows = []
    return {"ok": True, "runs": rows}


class MachineIn(BaseModel):
    name: str = Field(..., max_length=64)
    channel: str = Field("lan")
    host: str = Field("")
    user: str = Field("")
    target: str = Field("")
    enabled: bool = Field(True)
    note: str = Field("")


@router.post("/api/checkin/machines")
def api_machine_upsert(body: MachineIn):
    if not _NAME_RE.match(body.name):
        raise HTTPException(400, "bad machine name")
    if body.channel not in ("lan", "tailcat"):
        raise HTTPException(400, "channel must be lan|tailcat")
    _ensure_tables()
    db.execute("INSERT INTO checkin_machines"
              "(name,channel,host,user,target,enabled,note,created_at) VALUES(?,?,?,?,?,?,?,?) "
              "ON CONFLICT(name) DO UPDATE SET channel=excluded.channel, host=excluded.host, "
              "user=excluded.user, target=excluded.target, enabled=excluded.enabled, "
              "note=excluded.note",
              (body.name, body.channel, body.host, body.user, body.target,
               1 if body.enabled else 0, body.note, str(int(time.time()))))
    return {"ok": True, "name": body.name}


@router.delete("/api/checkin/machines/{name}")
def api_machine_disable(name: str):
    if not _NAME_RE.match(name):
        raise HTTPException(400, "bad machine name")
    _ensure_tables()
    db.execute("UPDATE checkin_machines SET enabled=0 WHERE name=?", (name,))
    return {"ok": True, "name": name, "enabled": False}


class RunIn(BaseModel):
    name: str = Field(..., max_length=64)
    cmd: str = Field("run")
    mode: str = Field("plan")       # 默认 plan（dry），exec 才真签到


@router.post("/api/checkin/run")
def api_run(body: RunIn):
    m = _by_name(body.name)
    if not m:
        raise HTTPException(404, "machine not registered: %s" % body.name)
    if not m["enabled"]:
        raise HTTPException(409, "machine disabled: %s" % body.name)
    return run_remote(m, body.cmd, body.mode)
