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

import base64
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
TAILCAT_INCOMING = Path.home() / ".config" / "tailcat" / "incoming"
TC_CONNECT = "/fs/1000/ftp/技术文档/tailcat/tailcat-connect.sh"
BRIDGE = r"C:\AIAutomator\contrib\aia_bridge.py"
BEGIN = "===AIA_JSON_BEGIN==="
END = "===AIA_JSON_END==="

#: 命令白名单 —— 只放行 bridge 的只读/幂等子命令，绝不拼自由文本
CMDS = ("probe", "list", "health", "credits", "totals", "run")
#: 机器名：tailcat 目标名形如 DESKTOP-QUU9CJT / WIN-20260130XPI
_NAME_RE = re.compile(r"^[A-Za-z0-9._-]{1,64}$")
_CONNECT_TIMEOUT = 8        # LAN 直连 ssh ConnectTimeout
_PROBE_TIMEOUT = 12         # 探测单次总超时（快速失败，不进 do_connect 重试）
_PING_TIMEOUT = 10          # tailcat ping 控制面探测超时（DERP 中转实测 3.4s）
_SCAN_TIMEOUT = 45          # --scan 静默消费新上报的超时（含 ping 各目标）
#: do_connect 内部 8 次重试 × ConnectTimeout=20 + 7×sleep 3 ≈ 181s；
#: hub 超时必须覆盖它，否则先 kill 会把真实 stderr 吞成 timeout（v0.13.202 修复）
_RUN_TIMEOUT = 200
#: 探测成功标记：probe 测的是「通道能否执行一条命令」，不依赖 bridge 是否已部署
_PROBE_OK = "AIA_PROBE_OK"

#: 种子（来源：connect-targets.tsv + ~/.ssh/config，均为拓扑非凭据）。
#: 只登记「哪台装了 AIAutomator 且归 hub 管」；token/user 仍从 tsv 现读，不落库。
SEEDS = (
    {"name": "DESKTOP-QUU9CJT", "channel": "lan", "host": "192.168.5.99",
     "user": "Mac", "target": "DESKTOP-QUU9CJT", "note": "B 机 · LAN 免密 SSH"},
    {"name": "WIN-20260130XPI", "channel": "relay", "host": "",
     "user": "Administrator", "target": "WIN-20260130XPI",
     "note": "A 机 · 经 B 跳板（NAS→A 的 DERP 数据面不通，B→A 实测通）"},
)

#: v0.13.203 实测定案：NAS→A 的 tailcat 数据面 `Dial: context deadline exceeded`
# （控制面 ping 通、token 最新、A 机 serve/sshd 正常 —— B→A 同款通道 21s 通），
# 唯独 NAS 起的拨号不通 ⇒ A 机改走「经 B 跳板」：外层 LAN SSH 到 B，内层在 B 上
# 用 tailcat ProxyCommand ssh 到 A（EncodedCommand 包内层，避开 Windows cmd 引号嵌套）。
# B 是固定跳板（拓扑非凭据）；A 机 token 只出现在进程参数/b64 里，不落库不回传。
RELAY_VIA = {"host": "192.168.5.99", "user": "Mac"}
_RELAY_CONNECT_TIMEOUT = 30      # B 上 tailcat→A 的握手超时（实测 21s 全链路）

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
def _token_for(m: Dict[str, Any]) -> str:
    """从 tailcat connect-targets.tsv 读该目标 token（只读；不落库、不回传、不进日志）。"""
    try:
        for line in TAILCAT_TSV.read_text(encoding="utf-8").splitlines():
            parts = line.split("\t")
            if len(parts) >= 2 and parts[0] == m.get("target", ""):
                return parts[1].strip()
    except (OSError, UnicodeDecodeError):
        pass
    return ""


def _extract_reported_token(path) -> str:
    """按 tailcat check_incoming 的提取逻辑解析上报文件（可能两行：user 行 + token 行）。

    与 tailcat-connect.sh 同口径：L1 合法用户名且 L2 以 tc 开头 ⇒ token=extract(L2)；
    否则 token=extract(L1)。extract = `tc[A-Za-z0-9_-]{20,}` 首个匹配。
    直接把全文当 token 是错的（实测文件 L1=Administrator / L2=token）。
    """
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except (OSError, UnicodeDecodeError):
        return ""
    first = lines[0] if lines else ""
    second = lines[1] if len(lines) > 1 else ""
    if re.fullmatch(r"[A-Za-z0-9._-]{1,32}", first) and second.startswith("tc"):
        src = second
    else:
        src = first
    m = re.search(r"tc[A-Za-z0-9_-]{20,}", src)
    return m.group(0) if m else ""


def _incoming_token(target: str) -> str:
    """读 tailcat 收件箱里该目标**最新上报**的 token（未消费的）。无则空串。"""
    if not _NAME_RE.match(target or ""):
        return ""
    return _extract_reported_token(TAILCAT_INCOMING / ("%s.token" % target))


def _archived_token(target: str) -> str:
    """读已归档（.processed）的上报 token —— Windows 端 [14] 重新上报后这里的最新。"""
    if not _NAME_RE.match(target or ""):
        return ""
    return _extract_reported_token(TAILCAT_INCOMING / ".processed" / ("%s.token" % target))


def _consume_fresh_token(m: Dict[str, Any]) -> bool:
    """归档里存在与 tsv 不同的该目标 token 时，用 tailcat 既有 `--add` 覆盖注册。

    v0.13.203 实测修正：`check_incoming`/`--scan` 的静默消费**只对「提取出的 token ≠ tsv」
    场景有效**，而首次取证时误把「上报文件全文 hash ≠ tsv token hash」当成 token 过期
    （输入口径不同：文件含 user 行）。真实判据 = 按提取逻辑取 token 串后与 tsv 字符串比对。
    Windows 端重新上报（控制台 [14]）后归档文件是最新权威；tsv 落后时用脚本自带
    `--add <name> <token> <user>` 原子覆盖（save_target 幂等），不绕过 tailcat 机制。
    返回是否真的更新了 tsv。
    """
    target = m.get("target", "")
    fresh = _archived_token(target) or _incoming_token(target)
    if not fresh or fresh == _token_for(m):
        return False
    user = m.get("user") or ""
    if not _NAME_RE.match(user or ""):
        return False
    try:
        subprocess.run(["bash", TC_CONNECT, "--add", target, fresh, user],
                       capture_output=True, text=True, timeout=_SCAN_TIMEOUT,
                       shell=False)
    except (subprocess.TimeoutExpired, OSError):
        return False
    return _token_for(m) == fresh


def _relay_argv(m: Dict[str, Any], remote_cmd: str) -> List[str]:
    """经 B 跳板连目标机：外层 LAN SSH → B 上 tailcat ProxyCommand ssh → 目标。

    内层命令包在 PowerShell EncodedCommand（UTF-16LE）里，避开 Windows cmd 的
    双层引号嵌套（实测路径）。token 只进 b64/进程参数，不落库不回传。
    """
    token = _token_for(m)
    if not token:
        raise ValueError("no tailcat token for relay")
    user = m.get("user") or "Administrator"
    if not _NAME_RE.match(user):
        raise ValueError("bad user")
    target = m.get("target") or "relay"
    inner = ('ssh -o "ProxyCommand=C:\\tailcat\\tailcat.exe %s 22" '
             '-o "HostKeyAlias=tc-%s" -o StrictHostKeyChecking=accept-new '
             '-o BatchMode=yes -o ConnectTimeout=%d %s@127.0.0.1 "%s"'
             % (token, target, _RELAY_CONNECT_TIMEOUT, user, remote_cmd))
    ps = "$ProgressPreference='SilentlyContinue'\n" + inner + "\nexit 0\n"
    b64 = base64.b64encode(ps.encode("utf-16-le")).decode()
    return ["ssh", "-o", "BatchMode=yes", "-o",
            "ConnectTimeout=%d" % _CONNECT_TIMEOUT,
            "%s@%s" % (RELAY_VIA["user"], RELAY_VIA["host"]),
            "powershell -NoProfile -EncodedCommand %s" % b64]


def _wrap_remote(m: Dict[str, Any], remote: str) -> List[str]:
    """把远端命令包进对应通道（LAN SSH / tailcat / relay）。三种通道共用一处。"""
    if m.get("channel") == "relay":
        if not _NAME_RE.match(m.get("target", "")):
            raise ValueError("bad target")
        return _relay_argv(m, remote)
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


def build_argv(m: Dict[str, Any], cmd: str, dry: bool) -> List[str]:
    """组装 argv（shell=False）。命令与机器名都已校验，用户输入不进 shell。"""
    if cmd not in CMDS:
        raise ValueError("cmd not allowed: %s" % cmd)
    # probe 测「通道能否跑一条命令」→ echo，不依赖 bridge 是否已部署
    # （旧版 probe 跑 python aia_bridge.py，A 机未部署时必然失败并被超时吞掉）。
    remote = ("echo %s" % _PROBE_OK) if cmd == "probe" else (
        "python %s %s" % (BRIDGE, " ".join(
            [cmd] + (["--dry"] if cmd == "run" and dry else []))))
    return _wrap_remote(m, remote)


# ── 多账号管理（v0.13.204）：WorkBuddy 短信 / TraeWork 桌面导入 ──────────────
ACCOUNT_PLATFORMS = ("workbuddy", "traework")
ACCOUNT_ACTIONS = ("accounts", "account-send", "account-add", "account-import",
                   "account-remove")
#: 敏感参数（手机号 / 短信码 / UID）只走 stdin，不上命令行 —— 远端进程列表也看不到。
_ACCOUNT_TIMEOUT = 90
_acct_cache: Dict[str, tuple] = {}          # machine → (ts, data)；列表 30s 内复用
_ACCOUNT_CACHE_TTL = 30
_sms_last: Dict[str, int] = {}              # machine|phone → ts；60s 冷却防连点


def build_account_argv(m: Dict[str, Any], action: str, platform: str = "") -> List[str]:
    """多账号命令 argv。action 与 platform 都来自固定枚举，用户输入绝不进 shell。"""
    if action not in ACCOUNT_ACTIONS:
        raise ValueError("account action not allowed: %s" % action)
    if action == "accounts":
        if platform and platform not in ACCOUNT_PLATFORMS:
            raise ValueError("bad platform")
    elif platform not in ACCOUNT_PLATFORMS:
        raise ValueError("bad platform")
    remote = "python %s %s" % (BRIDGE, action)
    if platform:
        remote += " --platform " + platform
    if action != "accounts":
        remote += " --stdin"
    return _wrap_remote(m, remote)


def _log_account_run(m: Dict[str, Any], action: str, res: Dict[str, Any]) -> None:
    """账号操作也入流水（审计）；payload 只含脱敏数据，手机号/验证码绝不落库。"""
    rid = uuid.uuid4().hex[:12]
    try:
        _ensure_tables()
        db.execute("INSERT INTO checkin_runs(id,machine,cmd,mode,state,ok,payload,error,ts) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (rid, m["name"], action, "account", res["state"], 1 if res["ok"] else 0,
                    json.dumps({"data": res.get("data") or {}, "reason": res.get("reason")},
                               ensure_ascii=False)[:8000],
                    ("[%s] %s" % (res.get("reason"), res.get("error") or ""))[:500],
                    int(time.time())))
    except Exception:  # noqa: BLE001
        pass


def run_account(m: Dict[str, Any], action: str, platform: str = "",
                payload: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """执行一次账号操作，归一信封（ok/state/reason/error/data）。"""
    argv = build_account_argv(m, action, platform)
    timeout = _RELAY_CONNECT_TIMEOUT + _ACCOUNT_TIMEOUT if m.get("channel") == "relay" \
        else _ACCOUNT_TIMEOUT
    control = _ping_control_plane(m) if m.get("channel") in ("tailcat", "relay") else None
    body = json.dumps(payload or {}, ensure_ascii=False)
    with _run_lock:                      # 单连接争用：与签到执行共用一把锁
        try:
            p = subprocess.run(argv, input=body, capture_output=True, text=True,
                               timeout=timeout, shell=False)
        except subprocess.TimeoutExpired as e:
            raw = e.stderr or ""
            if isinstance(raw, bytes):
                raw = raw.decode("utf-8", "replace")
            res = {"ok": False, "state": "offline", "reason": "timeout",
                   "error": "账号操作超时 %ds" % timeout, "stderr": str(raw)[-300:]}
        except OSError as e:
            res = {"ok": False, "state": "offline", "reason": "spawn-error",
                   "error": "spawn: %s" % e}
        else:
            err = (p.stderr or "").strip()[-400:]
            if p.returncode != 0 and BEGIN not in (p.stdout or ""):
                reason, human = _classify_failure(p.returncode, p.stderr, p.stdout, control)
                res = {"ok": False, "state": "offline", "reason": reason,
                       "error": human, "stderr": err}
            else:
                res = parse_bridge(p.stdout or "")
                if err:
                    res["stderr"] = err
    res["control"] = control
    res.setdefault("reason", "ok" if res["ok"] else "unknown")
    _log_account_run(m, action, res)
    return {**res, "machine": m["name"], "action": action, "platform": platform,
            "ts": int(time.time())}


def build_probe_argv(m: Dict[str, Any], token: str = "") -> List[str]:
    """探测专用：单次轻量 ssh，**绕过 tailcat-connect.sh 的 8 次重试**。

    do_connect 每次 ConnectTimeout=20 × 8 次会让一次探测拖到 ~181s；探测只需
    一次握手就知道通不通。LAN 直连；tailcat 走 ProxyCommand 单次（DERP 实测 ~11s 出结论）。
    """
    remote = "echo %s" % _PROBE_OK
    if m.get("channel") == "relay":
        if not _NAME_RE.match(m.get("target", "")):
            raise ValueError("bad target")
        return _relay_argv(m, remote)
    if m.get("channel") not in ("tailcat", "relay"):
        host, user = m.get("host", ""), m.get("user", "")
        if not re.match(r"^[A-Za-z0-9._:-]{1,128}$", host or ""):
            raise ValueError("bad host")
        if not _NAME_RE.match(user or ""):
            raise ValueError("bad user")
        return ["ssh", "-o", "BatchMode=yes", "-o",
                "ConnectTimeout=%d" % _CONNECT_TIMEOUT,
                "%s@%s" % (user, host), remote]
    target = m.get("target", "")
    if not _NAME_RE.match(target):
        raise ValueError("bad target")
    token = token or _token_for(m)
    if not token:
        raise ValueError("no tailcat token for probe")
    user = m.get("user") or "Administrator"
    if not _NAME_RE.match(user):
        raise ValueError("bad user")
    known_hosts = str(Path.home() / ".config" / "tailcat" / "known_hosts")
    alias = "tc-%s" % target       # 每目标独立 host key 条目，避免串味（SCRIPT-README §36）
    return ["ssh", "-o", "ProxyCommand=tailcat %s 22" % token,
            "-o", "HostKeyAlias=%s" % alias,
            "-o", "StrictHostKeyChecking=accept-new",
            "-o", "UserKnownHostsFile=%s" % known_hosts,
            "-o", "BatchMode=yes", "-o", "ConnectTimeout=%d" % _PROBE_TIMEOUT,
            "%s@127.0.0.1" % user, remote]


def _ping_control_plane(m: Dict[str, Any]) -> Dict[str, Any]:
    """控制面探测：tailcat ping（DERP/直连），与 ssh 数据面**解耦**。

    v0.13.202 实测：A 机 ping 3.4s via DERP 成功，ssh/22 却 Dial timeout。
    ping 通只说明「机器在线」，不代表 22 端口可转发 —— 两层必须分开报，
    否则一个笼统 offline 无法定位（用户报「探测显示失败」却无从下手）。
    """
    token = _token_for(m)
    if not token:
        return {"ok": False, "detail": "connect-targets.tsv 无此目标 token"}
    try:
        p = subprocess.run(
            ["tailcat", "ping", "-timeout", "%ds" % _PING_TIMEOUT, token],
            capture_output=True, text=True, timeout=_PING_TIMEOUT + 5, shell=False)
    except subprocess.TimeoutExpired:
        return {"ok": False, "detail": "ping 超时 %ds" % _PING_TIMEOUT}
    except OSError as e:
        return {"ok": False, "detail": "ping 起不来: %s" % e}
    out = ((p.stdout or "") + (p.stderr or "")).strip()
    ok = p.returncode == 0 and "pong" in out.lower()
    return {"ok": ok, "detail": out[-240:] or ("ping rc=%d" % p.returncode)}


def _classify_failure(rc: int, stderr: str, stdout: str,
                      control: Optional[Dict[str, Any]]) -> tuple:
    """把底层 stderr 归成**可操作**的 reason，而不是一个笼统 offline。"""
    s, so = (stderr or "").lower(), (stdout or "").lower()
    if any(k in s + so for k in ("can't open file", "no such file", "not found", "找不到")):
        return "bridge-missing", "远端未找到 aia_bridge.py（该机 AIAutomator 未部署/路径不符）"
    if any(k in s for k in ("dial:", "kex_exchange_identification", "banner exchange",
                            "unknown port 65535", "connection closed", "connection refused",
                            "connection timed out")):
        return "data-plane-down", "ssh/22 数据面不可达：tailcat 隧道 ping 通，但 22 端口未转发或 sshd 未起"
    if control is not None and not control.get("ok"):
        return "control-plane-down", "tailcat ping 不通（机器离线/中继不可达）"
    return "ssh-failed", "ssh 退出码 %d" % rc


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
    """串行执行一次，落流水。返回归一信封（ok/state/reason/error/control/data）。

    v0.13.202 分层修复：先判控制面（tailcat ping），再走单次轻量探测或带重试的执行；
    超时分层（探测短、执行覆盖 do_connect 重试）；失败按 reason 归类并回传底层 stderr，
    不再把「控制面通但 22 端口不通」压成一个笼统的 timeout。
    """
    if cmd not in CMDS:
        raise HTTPException(400, "cmd must be one of %s" % (list(CMDS),))
    if mode not in ("plan", "exec"):
        raise HTTPException(400, "mode must be plan|exec")
    dry = (mode == "plan") or (cmd != "run")
    control = _ping_control_plane(m) if m.get("channel") in ("tailcat", "relay") else None
    # 探测走单次轻量 argv（绕过 do_connect 8 次重试），其余走通用 argv
    argv = build_probe_argv(m) if cmd == "probe" else build_argv(m, cmd, dry)
    timeout = (_RELAY_CONNECT_TIMEOUT + 8 if m.get("channel") == "relay" else _PROBE_TIMEOUT + 6) if cmd == "probe" else _RUN_TIMEOUT
    refreshed = False
    with _run_lock:                      # 单连接争用：绝不并发
        for attempt in (1, 2):
            try:
                p = subprocess.run(argv, capture_output=True, text=True,
                                    timeout=timeout, shell=False)
            except subprocess.TimeoutExpired as e:
                raw = e.stderr or ""
                if isinstance(raw, bytes):
                    raw = raw.decode("utf-8", "replace")
                res = {"ok": False, "state": "offline", "reason": "timeout",
                       "error": "执行超时 %ds（探测单次 / 执行需覆盖 do_connect 重试）" % timeout,
                       "stderr": str(raw)[-300:]}
            except OSError as e:
                res = {"ok": False, "state": "offline", "reason": "spawn-error",
                       "error": "spawn: %s" % e}
                break
            else:
                err = (p.stderr or "").strip()[-400:]
                if cmd == "probe":
                    ok = p.returncode == 0 and _PROBE_OK in (p.stdout or "")
                    if ok:
                        res = {"ok": True, "state": "ok", "reason": "reachable",
                               "data": {"probe": _PROBE_OK, "channel": m.get("channel"),
                                        "via_control": (control or {}).get("detail", "")[:120]}}
                    else:
                        reason, human = _classify_failure(p.returncode, p.stderr, p.stdout, control)
                        res = {"ok": False, "state": "offline", "reason": reason,
                               "error": human, "stderr": err}
                elif p.returncode != 0 and BEGIN not in (p.stdout or ""):
                    reason, human = _classify_failure(p.returncode, p.stderr, p.stdout, control)
                    res = {"ok": False, "state": "offline", "reason": reason,
                           "error": human, "stderr": err}
                else:
                    res = parse_bridge(p.stdout or "")
                    if err:
                        res["stderr"] = err
            # v0.13.203 自愈：连接失败且归档里存在与 tsv 不同的 token（Windows 端
            # 重新上报后 tsv 落后）⇒ 用 tailcat 既有 --add 覆盖注册并重试一次。
            if res.get("ok") or m.get("channel") not in ("tailcat", "relay") or attempt == 2:
                break
            if _consume_fresh_token(m):
                refreshed = True
                argv = build_probe_argv(m) if cmd == "probe" else build_argv(m, cmd, dry)
                continue
            break
    if refreshed:
        res["token_refreshed"] = True
    res["control"] = control
    res.setdefault("reason", "ok" if res["ok"] else "unknown")
    rid = uuid.uuid4().hex[:12]
    try:
        _ensure_tables()
        db.execute("INSERT INTO checkin_runs(id,machine,cmd,mode,state,ok,payload,error,ts) "
                   "VALUES(?,?,?,?,?,?,?,?,?)",
                   (rid, m["name"], cmd, mode, res["state"], 1 if res["ok"] else 0,
                    json.dumps({"data": res.get("data") or {}, "reason": res["reason"],
                                "control": control}, ensure_ascii=False)[:8000],
                    ("[%s] %s" % (res["reason"], res.get("error") or ""))[:500],
                    int(time.time())))
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
        rows = db.query("SELECT machine, state, ok, ts, error, payload FROM checkin_runs "
                        "ORDER BY ts DESC LIMIT 200")
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


# ── 多账号端点 ────────────────────────────────────────────────────────────
_PHONE_RE = re.compile(r"^1[3-9]\d{9}$")
_SMS_RE = re.compile(r"^\d{4,8}$")
_UID_RE = re.compile(r"^[0-9A-Za-z_.@-]{1,128}$")


def _account_machine(name: str) -> Dict[str, Any]:
    m = _by_name(name)
    if not m:
        raise HTTPException(404, "machine not registered: %s" % name)
    if not m["enabled"]:
        raise HTTPException(409, "machine disabled: %s" % name)
    return m


def _platform(v: str) -> str:
    if v not in ACCOUNT_PLATFORMS:
        raise HTTPException(400, "platform must be one of %s" % (list(ACCOUNT_PLATFORMS),))
    return v


@router.get("/api/checkin/accounts")
def api_accounts(name: str, platform: str = "", refresh: int = 0):
    """账号清单（脱敏）。列表要起一次远程进程，30s 内复用缓存；refresh=1 强刷。"""
    m = _account_machine(name)
    if platform:
        _platform(platform)
    now = int(time.time())
    hit = _acct_cache.get(name)
    if not refresh and hit and now - hit[0] < _ACCOUNT_CACHE_TTL:
        return {**hit[1], "cached": True, "age": now - hit[0]}
    res = run_account(m, "accounts", platform)
    if res.get("ok"):
        _acct_cache[name] = (now, res)
    return {**res, "cached": False}


class AccountSendIn(BaseModel):
    name: str = Field(..., max_length=64)
    platform: str = Field("workbuddy")
    phone: str = Field(..., max_length=20)


@router.post("/api/checkin/accounts/send-code")
def api_account_send(body: AccountSendIn):
    m = _account_machine(body.name)
    platform = _platform(body.platform)
    phone = body.phone.strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "手机号格式不对（11 位大陆手机号）")
    key = "%s|%s" % (body.name, phone)
    now = int(time.time())
    last = _sms_last.get(key) or 0
    if now - last < 60:
        raise HTTPException(429, "验证码刚发过，请 %d 秒后再试" % (60 - (now - last)))
    res = run_account(m, "account-send", platform, {"phone": phone})
    if res.get("ok"):
        _sms_last[key] = now
    return res


class AccountAddIn(BaseModel):
    name: str = Field(..., max_length=64)
    platform: str = Field("workbuddy")
    phone: str = Field(..., max_length=20)
    code: str = Field(..., max_length=12)


@router.post("/api/checkin/accounts/add")
def api_account_add(body: AccountAddIn):
    m = _account_machine(body.name)
    platform = _platform(body.platform)
    phone, code = body.phone.strip(), body.code.strip()
    if not _PHONE_RE.match(phone):
        raise HTTPException(400, "手机号格式不对（11 位大陆手机号）")
    if not _SMS_RE.match(code):
        raise HTTPException(400, "验证码格式不对（4~8 位数字）")
    res = run_account(m, "account-add", platform, {"phone": phone, "code": code})
    if res.get("ok"):
        _acct_cache.pop(body.name, None)
    return res


class AccountImportIn(BaseModel):
    name: str = Field(..., max_length=64)
    platform: str = Field("traework")


@router.post("/api/checkin/accounts/import")
def api_account_import(body: AccountImportIn):
    m = _account_machine(body.name)
    platform = _platform(body.platform)
    res = run_account(m, "account-import", platform)
    if res.get("ok"):
        _acct_cache.pop(body.name, None)
    return res


class AccountRemoveIn(BaseModel):
    name: str = Field(..., max_length=64)
    platform: str = Field("workbuddy")
    uid: str = Field(..., max_length=128)


@router.post("/api/checkin/accounts/remove")
def api_account_remove(body: AccountRemoveIn):
    m = _account_machine(body.name)
    platform = _platform(body.platform)
    uid = body.uid.strip()
    if not _UID_RE.match(uid):
        raise HTTPException(400, "bad uid")
    res = run_account(m, "account-remove", platform, {"uid": uid})
    if res.get("ok"):
        _acct_cache.pop(body.name, None)
    return res


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
