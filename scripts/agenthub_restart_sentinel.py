#!/usr/bin/env python3
"""agenthub 重启哨兵 —— 让「绕过 shim 的重启」不再静默。

背景（2026-10-25 PT-20261025-04）：
  ~/.local/bin/systemctl shim 只能拦 PATH 上的调用；写 /usr/bin/systemctl 绝对路径
  可直达真 systemd，且 shim 的审计日志一行不留（实测 restart.log 调用前后均 13 行）。
  本哨兵从 journal 侧观测，与被观测进程是否走 PATH 无关。

判据（每条写一行 JSON 到 data/logs/restart-watch.log）：
  stop_detected     : journal 出现 "Stopping agenthub" ⇒ 快照当刻 systemctl 进程链 + 活会话数
  started           : 服务重新起来 ⇒ 记录停机时长
  violation         : stop 时 term_sessions>0 ⇒ 同时写一行人读摘要，标 VIOLATION
  selftest          : --selftest 自证一次（不触发任何 systemd 动作）

只读：不重启、不 stop、不写 systemd 状态。唯一写盘是审计日志。
"""
from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
import threading
import time
import urllib.request
from datetime import datetime

UNIT = os.environ.get("HUB_SENTINEL_UNIT", "agenthub.service")
HEALTH = "http://127.0.0.1:3102/health"
LOG = os.environ.get("HUB_SENTINEL_LOG", "/fs/1000/ftp/技术文档/agenthub/data/logs/restart-watch.log")
HEARTBEAT = LOG + ".heartbeat"
HZ = os.sysconf("SC_CLK_TCK")
MAX_ANCESTORS = 8


def now_iso() -> str:
    return datetime.now().astimezone().strftime("%Y-%m-%dT%H:%M:%S%z")


def uptime() -> float:
    with open("/proc/uptime") as fh:
        return float(fh.read().split()[0])


def emit(rec: dict) -> None:
    rec.setdefault("ts", now_iso())
    line = json.dumps(rec, ensure_ascii=False)
    with open(LOG, "a", encoding="utf-8") as fh:
        fh.write(line + "\n")
    if rec.get("violation"):
        with open(LOG, "a", encoding="utf-8") as fh:
            fh.write(
                "VIOLATION %s 有 %s 个活终端会话时被 %s 重启（pid=%s cmd=%s）\n"
                % (
                    rec["ts"],
                    rec.get("term_sessions"),
                    rec.get("event"),
                    rec.get("culprit_pid", "-"),
                    (rec.get("culprit_cmd") or "-")[:160],
                )
            )


# ---------------------------------------------------------------- 会话数缓存
class SessionCache:
    """5s 轮询 /health。stop 事件发生时服务可能已在关停，故用最近一次缓存值。"""

    def __init__(self) -> None:
        self.n = -1
        self.state = "init"
        self.at = 0.0
        self._halt = threading.Event()

    def loop(self) -> None:
        while not self._halt.is_set():
            self.n, self.state = self._read()
            self.at = time.time()
            self._halt.wait(5)

    @staticmethod
    def _read() -> tuple[int, str]:
        try:
            with urllib.request.urlopen(HEALTH, timeout=2) as resp:
                data = json.load(resp)
            return int(data.get("term_sessions", -1)), "ok"
        except Exception as exc:  # noqa: BLE001 - 探针失败不得静默早退
            return -1, "unreachable:%s" % type(exc).__name__

    def snapshot(self) -> dict:
        return {
            "term_sessions": self.n,
            "session_probe": self.state,
            "session_age_s": round(time.time() - self.at, 1) if self.at else None,
        }

    def stop(self) -> None:
        self._halt.set()


# ---------------------------------------------------------------- 进程链取证
def _read_argv(pid: int) -> list[str]:
    try:
        with open("/proc/%d/cmdline" % pid, "rb") as fh:
            parts = [p.decode("utf-8", "replace") for p in fh.read().split(b"\x00") if p]
        return parts
    except OSError:
        return []


def _read_cmd(pid: int) -> str:
    return " ".join(_read_argv(pid))


def _read_ppid_start(pid: int) -> tuple[int, float]:
    try:
        with open("/proc/%d/stat" % pid) as fh:
            fields = fh.read().rsplit(")", 1)[1].split()
        ppid = int(fields[1])
        start_s = int(fields[19]) / HZ
        return ppid, start_s
    except (OSError, IndexError, ValueError):
        return 0, 0.0


def _describe(pid: int) -> dict:
    ppid, start_s = _read_ppid_start(pid)
    return {
        "pid": pid,
        "ppid": ppid,
        "age_s": round(uptime() - start_s, 1) if start_s else None,
        "cmd": _read_cmd(pid)[:240],
    }


def _ancestors(pid: int) -> list[dict]:
    chain: list[dict] = []
    seen = {pid}
    ppid, _ = _read_ppid_start(pid)
    while ppid > 1 and len(chain) < MAX_ANCESTORS and ppid not in seen:
        seen.add(ppid)
        chain.append(_describe(ppid))
        ppid, _ = _read_ppid_start(ppid)
    return chain


def find_culprits() -> list[dict]:
    """当刻所有涉及 agent-hub 的 systemctl 客户端进程（含祖先链）。

    不匹配 shim 自身：shim 被 exec 时 argv[0] 已是 /usr/bin/systemctl 或
    脚本路径，两者都算「调用者」，一并记录便于区分是否走了 shim。
    """
    out: list[dict] = []
    for entry in os.listdir("/proc"):
        if not entry.isdigit():
            continue
        pid = int(entry)
        if pid == os.getpid():
            continue
        argv = _read_argv(pid)
        if not argv:
            continue
        # 判据：argv[0] 的 basename 必须是 systemctl（含 shim 路径 /usr/bin/systemctl）；
        # 反例（2026-10-25 自检踩到）：`bash -c "…systemctl restart agenthub…"` 的整条
        # 命令行里含全部关键词，会被宽匹配误记成 caller。
        if "systemctl" not in os.path.basename(argv[0]):
            continue
        if not any("agenthub" in a or "agent-hub" in a for a in argv[1:] + [argv[0]]):
            continue
        cmd = " ".join(argv)
        info = _describe(pid)
        info["ancestors"] = _ancestors(pid)
        out.append(info)
    return out


# ---------------------------------------------------------------- journal 观测
def _msg(rec: dict) -> str:
    raw = rec.get("MESSAGE", "")
    if isinstance(raw, list):
        raw = b"".join(x if isinstance(x, bytes) else str(x).encode() for x in raw)
    if isinstance(raw, bytes):
        return raw.decode("utf-8", "replace")
    return str(raw)


def watch(cache: SessionCache) -> None:
    cmd = [
        "journalctl",
        "--user",
        "-u",
        UNIT,
        "-f",
        "-o",
        "json",
        "--no-pager",
        "--since",
        "now",
    ]
    proc = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
    assert proc.stdout is not None
    print("[sentinel] tailing journal pid=%d log=%s" % (proc.pid, LOG), flush=True)
    consume(proc.stdout, cache)


def handle(rec: dict, cache: SessionCache, last_stop: dict | None, source: str = "live") -> dict | None:
    msg = _msg(rec)
    if msg.startswith("Stopping " + UNIT) or msg.startswith("Stopping Agent Hub"):
        snap = cache.snapshot()
        culprits = find_culprits()
        primary = culprits[0] if culprits else {}
        out = {
            "event": "stop_detected",
            "source": source,
            "unit": UNIT,
            "job_type": rec.get("JOB_TYPE"),
            "pid": rec.get("_PID"),
            "message": msg,
            "violation": (snap["term_sessions"] or 0) > 0,
            "culprit_pid": primary.get("pid"),
            "culprit_cmd": primary.get("cmd"),
            "culprits": culprits,
        }
        out.update(snap)
        emit(out)
        print(
            "[sentinel] stop detected sessions=%s culprits=%d"
            % (snap["term_sessions"], len(culprits)),
            flush=True,
        )
        return out
    if msg.startswith("Started " + UNIT) or msg.startswith("Started Agent Hub"):
        emit(
            {
                "event": "started",
                "source": source,
                "unit": UNIT,
                "job_type": rec.get("JOB_TYPE"),
                "downtime_s": (
                    round(
                        datetime.now().timestamp()
                        - datetime.strptime(last_stop["ts"][:19], "%Y-%m-%dT%H:%M:%S").timestamp(),
                        1,
                    )
                    if last_stop
                    else None
                ),
                "previous_stop_had_sessions": (last_stop or {}).get("term_sessions"),
            }
        )
        return None
    return last_stop


def consume(lines, cache: SessionCache, source: str = "live") -> None:
    """逐行处理 journal JSON。心跳文件用于自证「真的在读这个单元的流」。"""
    last_stop: dict | None = None
    seen = 0
    beat_at = 0.0
    for line in lines:
        line = line.strip()
        if not line:
            continue
        seen += 1
        try:
            rec = json.loads(line)
        except json.JSONDecodeError:
            continue
        last_stop = handle(rec, cache, last_stop, source)
        if time.time() - beat_at >= 15:
            beat_at = time.time()
            with open(HEARTBEAT, "w", encoding="utf-8") as fh:
                json.dump({"ts": now_iso(), "lines_seen": seen, "last_msg": _msg(rec)[:200]}, fh, ensure_ascii=False)


def main() -> int:
    ap = argparse.ArgumentParser(description="agenthub 重启哨兵（只读审计）")
    ap.add_argument("--selftest", action="store_true", help="对当前进程表跑一次取证并写标记行后退出")
    ap.add_argument("--once", action="store_true", help="只探测一次会话数并打印，不进入观测")
    ap.add_argument("--replay", metavar="FILE", help="回放已抓取的 journal JSON 行（验解析路径，不碰 systemd）")
    args = ap.parse_args()

    os.makedirs(os.path.dirname(LOG), exist_ok=True)

    if args.replay:
        cache = SessionCache()
        cache.n, cache.state = cache._read()
        cache.at = time.time()
        with open(args.replay, encoding="utf-8") as fh:
            consume(fh, cache, source="replay")
        return 0

    if args.once:
        cache = SessionCache()
        cache.n, cache.state = cache._read()
        cache.at = time.time()
        print(json.dumps(cache.snapshot(), ensure_ascii=False))
        return 0

    if args.selftest:
        cache = SessionCache()
        cache.n, cache.state = cache._read()
        cache.at = time.time()
        snap = cache.snapshot()
        culprits = find_culprits()
        rec = {"event": "selftest", "source": "selftest", "unit": UNIT, "violation": False, "culprits_found": len(culprits)}
        rec.update(snap)
        rec["culprits"] = culprits
        emit(rec)
        print("[selftest] " + json.dumps(rec, ensure_ascii=False)[:900])
        return 0

    cache = SessionCache()
    t = threading.Thread(target=cache.loop, daemon=True)
    t.start()
    while True:
        try:
            watch(cache)
        except KeyboardInterrupt:
            cache.stop()
            return 0
        except Exception as exc:  # noqa: BLE001 - journalctl 由 systemd 重启，这里只记一次
            print("[sentinel] watcher error: %r, retry in 5s" % (exc,), flush=True)
            time.sleep(5)


if __name__ == "__main__":
    sys.exit(main())
