"""系统级路由 · 自重启 / 关机（设置页的落地动作）。

为什么单独成模块（与 C2 同一接缝思路）：main.py 只留组装职责，系统级动作不再塞回它。

自重启的两个要害：
  1. **执行者必须脱离本服务的 cgroup**：`agenthub.service` 是 `KillMode=control-group`，
     restart 的 stop 会把本 cgroup 内所有进程一起杀掉 ⇒ 直接 spawn 的 `systemctl restart`
     子进程可能在请求送达 systemd 之前就被连坐。故用 `systemd-run --user --collect` 起一个
     **瞬态单元**（cgroup 挂在 user@.service 下，不受本服务 stop 影响）去执行 restart。
  2. **默认拒绝「有在跑终端会话」时重启**：与 `agent-hubctl.sh` 的 `guard_live_sessions`
     同口径（shutdown 钩子会 `term.kill_all()` 把会话连坐杀掉）；确要承担再带 `force=1`。

关机走同一条脱离 cgroup 的路径，但执行者换成 `sudo -n loginctl poweroff`：
  · `agenthub.service` 是 **user unit**，没有直接 poweroff 的权限；`systemctl poweroff`
    不在本机 NOPASSWD 白名单里（会卡在密码提示），而 `/usr/bin/loginctl` 在白名单中。
  · 必须 `sudo -n`（绝不交互等密码），失败即单元失败、接口已在失败前返回。
  · 关机是不可逆动作 ⇒ 请求体必须显式带 `confirm=poweroff`；有活终端会话时默认 409，
    带 `force=1` 才执行（同 restart 口径）。

鉴权：两条端点都不在 `writeauth.EXEMPT_PREFIXES` 里 ⇒ 由写闸门覆盖（登录 + 写 scope）。
"""
import logging
import os
import subprocess
import time

from fastapi import APIRouter, HTTPException

import term as term_mod

log = logging.getLogger(__name__)

router = APIRouter()

#: 自重启的目标单元。默认生产单元；影子/异地部署可用 `HUB_RESTART_UNIT` 覆写（测试用，
#: 免得在阴影实例里误重启生产）。
RESTART_UNIT = os.getenv("HUB_RESTART_UNIT", "agenthub.service")

#: 关机执行者。`agenthub` 是 user unit，直接 poweroff 无权限；本机 sudoers 只对
#: `/usr/bin/loginctl` 给了 NOPASSWD（`systemctl poweroff` 不在白名单）。
POWEROFF_CMD = "exec /usr/bin/sudo -n /usr/bin/loginctl poweroff"


def _spawn_transient(prefix: str, cmd: str) -> str:
    """用 systemd 瞬态单元执行命令并返回单元名；spawn 失败抛 500。

    瞬态单元挂 user@.service 下，不进本服务 cgroup ⇒ agenthub 被 stop 时不会连坐。
    """
    unit = "%s-%d-%d" % (prefix, os.getpid(), int(time.time()))
    try:
        subprocess.Popen(
            ["systemd-run", "--user", "--collect", "--unit", unit, "/bin/sh", "-c", cmd],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL, start_new_session=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(500, {"error": "spawn_failed",
                                  "detail": "%s: %s" % (type(e).__name__, str(e)[:160])})
    return unit


@router.post("/api/system/restart")
async def system_restart(force: int = 0):
    """重启 `agenthub.service`，让磁盘上的代码改动生效（前端「需重启」按钮）。

    先回 202/200 再由脱离 cgroup 的瞬态单元执行重启；有活会话且未 force ⇒ 409。
    """
    n = term_mod.alive_count()
    if n and not force:
        raise HTTPException(409, {"error": "has_live_sessions", "term_sessions": n,
                                  "hint": "重启会连坐杀掉在跑的终端会话；确要承担请带 ?force=1"})
    _spawn_transient("ah-selfrestart", "sleep 1; systemctl --user restart %s" % RESTART_UNIT)
    log.info(f"[restart] 已派发重启 {RESTART_UNIT}（term_sessions={n} force={bool(force)}）")
    return {"status": "restarting", "unit": RESTART_UNIT, "term_sessions": n}


@router.post("/api/system/shutdown")
async def system_shutdown(confirm: str = "", force: int = 0):
    """彻底关闭本机（poweroff）。`confirm=poweroff` 是防误触的硬条件。

    与 restart 同口径：有活终端会话且未 force ⇒ 409；通过后由瞬态单元在 1 秒后
    执行 `sudo -n loginctl poweroff`，接口先返回，随后本机断电、页面失联。
    """
    if confirm != "poweroff":
        raise HTTPException(400, {"error": "confirm_required",
                                  "hint": "关机不可逆：须带 ?confirm=poweroff 才执行"})
    n = term_mod.alive_count()
    if n and not force:
        raise HTTPException(409, {"error": "has_live_sessions", "term_sessions": n,
                                  "hint": "关机会中断在跑的终端会话并关闭整机；确要承担请带 ?force=1"})
    unit = _spawn_transient("ah-poweroff", "sleep 1; " + POWEROFF_CMD)
    log.warning(f"[shutdown] 已派发 poweroff（term_sessions={n} force={bool(force)} unit={unit}）")
    return {"status": "powering_off", "unit": unit, "term_sessions": n}
