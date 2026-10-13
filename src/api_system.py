"""系统级路由 · 自重启（前端「需重启」按钮的落地动作）。

为什么单独成模块（与 C2 同一接缝思路）：main.py 只留组装职责，系统级动作不再塞回它。

自重启的两个要害：
  1. **执行者必须脱离本服务的 cgroup**：`agenthub.service` 是 `KillMode=control-group`，
     restart 的 stop 会把本 cgroup 内所有进程一起杀掉 ⇒ 直接 spawn 的 `systemctl restart`
     子进程可能在请求送达 systemd 之前就被连坐。故用 `systemd-run --user --collect` 起一个
     **瞬态单元**（cgroup 挂在 user@.service 下，不受本服务 stop 影响）去执行 restart。
  2. **默认拒绝「有在跑终端会话」时重启**：与 `agent-hubctl.sh` 的 `guard_live_sessions`
     同口径（shutdown 钩子会 `term.kill_all()` 把会话连坐杀掉）；确要承担再带 `force=1`。

鉴权：`/api/system/restart` 不在 `writeauth.EXEMPT_PREFIXES` 里 ⇒ 由写闸门覆盖，需口令。
"""
import os
import subprocess
import time

from fastapi import APIRouter, HTTPException

import term as term_mod

router = APIRouter()

#: 自重启的目标单元。默认生产单元；影子/异地部署可用 `HUB_RESTART_UNIT` 覆写（测试用，
#: 免得在阴影实例里误重启生产）。
RESTART_UNIT = os.getenv("HUB_RESTART_UNIT", "agenthub.service")


@router.post("/api/system/restart")
async def system_restart(force: int = 0):
    """重启 `agenthub.service`，让磁盘上的代码改动生效（前端「需重启」按钮）。

    先回 202/200 再由脱离 cgroup 的瞬态单元执行重启；有活会话且未 force ⇒ 409。
    """
    n = term_mod.alive_count()
    if n and not force:
        raise HTTPException(409, {"error": "has_live_sessions", "term_sessions": n,
                                  "hint": "重启会连坐杀掉在跑的终端会话；确要承担请带 ?force=1"})
    unit = "ah-selfrestart-%d-%d" % (os.getpid(), int(time.time()))
    cmd = "sleep 1; systemctl --user restart %s" % RESTART_UNIT
    try:
        subprocess.Popen(
            ["systemd-run", "--user", "--collect", "--unit", unit, "/bin/sh", "-c", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, {"error": "spawn_failed",
                                  "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
    print(f"[restart] 已派发重启 {RESTART_UNIT}（term_sessions={n} force={bool(force)}）", flush=True)
    return {"status": "restarting", "unit": RESTART_UNIT, "term_sessions": n}