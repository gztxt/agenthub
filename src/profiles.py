"""实体画像注册表（P1：按本机实际运行情况判定，端口只作端点发现不作存在性判据）

kind 语义：
  agent    编码/对话智能体（claude/pi/jcode/hermes/qwenpaw/cloudcli）
  gateway  模型/消息网关（CCR/ccpocket）——非 Agent，无对话按钮
  service  系统服务（xray/proxy-panel/claude-mem/ai_manager/gotty...）——仅快捷方式
  memory   记忆后端（tdai）
  tool     工具容器（chromium/ollama）

检测三路合并（任一命中即 running）：/proc 进程正则、systemd user 单元、docker 容器。
CLI 型 Agent 无进程时按 which/单元存在 → installed（可拉起终端会话）。
"""
import boundary
import capabilities
import os
import re
import shutil
from pathlib import Path
from typing import Dict, List, Optional

LAN_HOST_NOTE = "loopback 地址由前端按访问主机名改写"

# ── 外框统一注入代理（用户 09-20 选定方案 b）──────────────────────
# qwenpaw 自带 56px 顶栏（实测 h=56、sider=calc(100vh-64px)），跨源 iframe 下 hub 注不进
# 任何样式，而官方也没有 hideHeader/custom_css 钩子。hub 在进程内起一个本地反代，
# HTML 出栈前插一段 <style>（见 src/embed_proxy.py）。默认开；想回直连只需在 .env
# 写 EMBED_UNIFY=0 重启 hub，不必改代码。
EMBED_UNIFY = os.getenv("EMBED_UNIFY", "1") == "1"
EMBED_PROXY_PORT = int(os.getenv("EMBED_PROXY_PORT", "3103"))
QWENPAW_UI = ("http://127.0.0.1:%d" % EMBED_PROXY_PORT) if EMBED_UNIFY else "http://127.0.0.1:8088"

# CodeBuddy Code 的 CLI 只在 WorkBuddy 包内，不在 PATH（2026-09-27 实测：
# `which codebuddy` 落空，`shutil.which(<绝对路径>)` 原样返回）。终端入口因此必须写
# 绝对路径，否则 term.py 的 `which(cmd[0])` 判定失败、会话拉不起来。
CODEBUDDY_CLI = os.getenv("CODEBUDDY_CLI",
                          "/opt/WorkBuddy/resources/app.asar.unpacked/cli/bin/codebuddy")

# 进程扫描缓存（一次 discovery 周期复用）
_proc_cache = {"ts": 0.0, "procs": []}

# ── 外部探测缓存（2026-10-05，v0.13.79）──────────────────────────────
# 为什么加：`/api/agents` 实测 57ms 里，`docker ps -a` 独占 16.5ms、`systemctl
# list-units` 独占 5.1ms（主会话 2026-10-05 实测），而前端每 30s 调一次、
# updateBadges 每 60s 又调一次 ⇒ 每分钟白烧 3 次同样的两个子进程。
# 与 `_proc_cache` 同档（3s）：短到用户感知不到状态陈旧，长到足以覆盖一次轮询。
#
# ⚠ **写路径必须绕过**（`force_fresh=True`）：`resources.kill_resource` 用
# `running_systemd_units()` 的结果把关 SIGTERM/SIGKILL（resources.py:325-327）——
# 若那里吃到陈旧缓存，可能**误杀已停的**或**拒杀刚起的**。读路径（列表/面板）
# 无此风险，故默认走缓存、由写路径显式声明要新鲜值。
_UNITSD_TTL_S = float(os.getenv("AGENT_PROBE_TTL", "3"))   # 与 _proc_cache 同档
_unitsds_cache = {"ts": 0.0, "units": None}
_docker_cache = {"ts": 0.0, "states": None}


def _cached(cache: dict, key: str, ttl_s: float, produce):
    """手搓 TTL 缓存（与 _proc_cache 同形）。

    刻意不抽公共类：全仓四种缓存形制（skill/vitals/gwprobe/_gz_cache），
    此刻统一它属于「批 9 延后」的重构范围，先让每处照抄自己那儿的范式。
    """
    import time
    now = time.monotonic()
    if cache[key] is not None and (now - cache["ts"]) < ttl_s:
        return cache[key]
    val = produce()
    cache["ts"] = now
    cache[key] = val
    return val


def list_processes() -> List[dict]:
    """[{pid, comm, cmdline}] —— /proc 只读，cmdline 截 300 字符"""
    import time
    now = time.monotonic()
    if now - _proc_cache["ts"] < 3:
        return _proc_cache["procs"]
    procs = []
    for entry in Path("/proc").iterdir():
        if not entry.name.isdigit():
            continue
        try:
            comm = (entry / "comm").read_text().strip()
            cmd = (entry / "cmdline").read_bytes().replace(b"\0", b" ").decode(errors="ignore")[:300]
            procs.append({"pid": int(entry.name), "comm": comm, "cmdline": cmd})
        except (OSError, ValueError):
            continue
    _proc_cache["ts"] = now
    _proc_cache["procs"] = procs
    return procs


# ── 本机自有地址（2026-10-24）─────────────────────────────────────────
# 为什么需要：卡片地址是**服务端**写死的字面量（dsh 更极端，只能绑一张具体网卡，
# 见 dsh.service 注释），可同一个 hub 会被三种方式访问：局域网 192.168.5.102、
# Tailscale 100.x / fd7a::、MagicDNS 名。服务端不知道浏览器走哪条路，只有前端知道
# `location.hostname`。于是后端把「本机自有地址清单」随 /api/agents 下发，
# 前端把 URL 里命中的自有地址换成当前访问主机名（static/hub/01-core-boot.js:lanUrl）。
# 口径：`ip -o addr show scope global` ⇒ 天然排除回环与 link-local；再排除容器网桥
# （docker0/br-/veth/…）—— 那些地址浏览器到不了，列进来只会制造假候选。
_OWN_ADDRS_CACHE: dict = {"ts": 0.0, "addrs": []}
_OWN_ADDRS_TTL_S = 60.0
_OWN_ADDRS_SKIP_IFACE = ("lo", "docker", "br-", "veth", "virbr", "lxc", "cni", "tun", "flannel")


def own_addrs() -> List[str]:
    """本机对外可达的 IPv4/IPv6 字面量（不含回环 / 链路本地 / 容器网桥）。

    只做「有哪些地址」的事实采集，不判断哪个地址此刻可达 —— 那是前端的事
    （它知道 location.hostname）。取不到就返回空表，前端退回旧行为（不替换）。
    """
    import time
    now = time.monotonic()
    if now - _OWN_ADDRS_CACHE["ts"] < _OWN_ADDRS_TTL_S:
        return _OWN_ADDRS_CACHE["addrs"]
    addrs: List[str] = []
    try:
        import subprocess
        r = subprocess.run(["ip", "-o", "addr", "show", "scope", "global"],
                           capture_output=True, text=True, timeout=3)
        for ln in r.stdout.splitlines():
            parts = ln.split()
            if len(parts) < 4:
                continue
            iface = parts[1].rstrip(":")
            if iface.startswith(_OWN_ADDRS_SKIP_IFACE):
                continue
            # 形如 `2: enp1s0-ovs    inet 192.168.5.102/24 brd … scope global …`
            fam = "inet" if "inet" in parts else ("inet6" if "inet6" in parts else None)
            if fam is None:
                continue
            addr = parts[parts.index(fam) + 1].split("/")[0]
            if addr and addr not in addrs:
                addrs.append(addr)
    except Exception:  # noqa: BLE001  ip 不在 / 超时 ⇒ 空表降级，不让 discovery 挂
        addrs = []
    _OWN_ADDRS_CACHE["ts"] = now
    _OWN_ADDRS_CACHE["addrs"] = addrs
    return addrs


def running_systemd_units(*, force_fresh: bool = False) -> set:
    """在跑的 systemd user 单元名集合（去 .service 后缀）。

    `force_fresh=True` 绕过缓存 —— **只有写路径该用**（见 `_unitsds_cache` 上方的
    警告：kill 路径拿陈旧值会误杀/拒杀）。读路径别传，保持零成本。
    """
    def _produce() -> set:
        import subprocess
        try:
            r = subprocess.run(["systemctl", "--user", "list-units", "--type=service",
                                "--state=running", "--plain", "--no-legend"],
                               capture_output=True, text=True, timeout=8)
            return {ln.split()[0].removesuffix(".service") for ln in r.stdout.splitlines() if ln.strip()}
        except Exception:  # noqa: BLE001
            return set()

    if force_fresh:
        return _produce()
    return _cached(_unitsds_cache, "units", _UNITSD_TTL_S, _produce)


# ── dsh Web UI 的带 token 地址（2026-10-23）─────────────────────────
# dsh web 的浏览器鉴权 token 是**进程级随机**：`packages/client/connection/src/browser-auth.ts`
# 里 `processLaunchToken()` 用 `randomBytes(32)` 生成，只在启动横幅打印一次，前端用
# `?token=` 换一个 HttpOnly、SameSite=Strict、最长 1 天的 cookie。推论三条：
#   ① token 不能写进本文件 —— 既是「凭据不落配置」，也会在每次 restart 后立刻失效；
#   ② 只能每次从服务日志取**最新一条**（每次启动都会多一条）；
#   ③ 取不到就退化为裸地址（服务没跑 / journal 滚掉），卡片仍出但打开会 401 ——
#      明确降级，不做「看起来能用其实打不开」。
# 直接读 journal 而不调 dshctl --url：后者要跑一遍完整脚本（实测 1~3s），
# 而 discovery 是每请求路径；缓存 30s，与 _UNITSD_TTL_S 同量级。
_DSH_URL_CACHE = {"ts": 0.0, "url": None}
_DSH_URL_TTL_S = 30.0


def _dsh_web_url() -> Optional[str]:
    import subprocess
    import time
    now = time.monotonic()
    if now - _DSH_URL_CACHE["ts"] < _DSH_URL_TTL_S:
        return _DSH_URL_CACHE["url"]
    url = None
    try:
        out = subprocess.run(
            ["journalctl", "--user", "-u", "dsh.service", "-n", "200",
             "--no-pager", "-o", "cat"],
            capture_output=True, text=True, timeout=5,
        ).stdout or ""
        hits = re.findall(r"https?://\S+\?token=[A-Za-z0-9._-]+", out)
        if hits:
            url = hits[-1]
    except Exception:  # noqa: BLE001  取不到只降级，不能让整条 discovery 挂掉
        url = None
    _DSH_URL_CACHE["ts"] = now
    _DSH_URL_CACHE["url"] = url
    return url


def invalidate_ui_url(agent_id: str) -> None:
    """进程级 token 的地址缓存作废（dsh 的 30s 缓存 + capabilities 的 10s 缓存）。"""
    if agent_id == "dsh":
        _DSH_URL_CACHE["ts"] = 0.0
        _DSH_URL_CACHE["url"] = None
    try:
        capabilities.invalidate_url(agent_id)
    except Exception:  # noqa: BLE001  缓存失效失败不该拦住启动流程
        pass


def docker_states(*, force_fresh: bool = False) -> Dict[str, str]:
    """{容器名: running|exited}"""
    def _produce() -> Dict[str, str]:
        import json as _json
        import subprocess
        out: Dict[str, str] = {}
        try:
            r = subprocess.run(["docker", "ps", "-a", "--format", "{{json .}}"],
                               capture_output=True, text=True, timeout=10)
            for ln in r.stdout.splitlines():
                try:
                    c = _json.loads(ln)
                    out[c.get("Names") or c.get("Name") or "?"] = \
                        "running" if "running" in (c.get("State") or "").lower() else "exited"
                except _json.JSONDecodeError:
                    continue
        except Exception:  # noqa: BLE001
            pass
        return out
    if force_fresh:
        return _produce()
    return _cached(_docker_cache, "states", _UNITSD_TTL_S, _produce)


# ── 原生界面启动器（v0.13.152；v0.13.156 起改由能力矩阵派生）──────────
# 为什么需要：2026-10-24「所有 agent 按需拉起」之后，各卡的原生界面常态是停着的。
# v0.13.151 把按钮复还了，但点开只有「目标界面未响应」覆盖层 —— **没有拉起的地方**，
# 用户只能自己去终端敲 systemctl（2026-10-25 报障「要使用原生界面 都没有拉起的地方」）。
#
# ⚠ v0.13.156：**这张表不再手工维护**。地址/端口/探针/起法收敛到
# `capabilities.UI_RECIPES` 一条记录，因为「手写 ui」「手写 UI_START」「手写 modelcfg」
# 三张表任何一张漏写都只表现为「又少一个按钮」，且不报错 —— 用户 10-25 第三次报同一事
# 就是这个机制。这里保留 `UI_START` 这个名字只为兼容旧调用点（api_agents / 测试）。
UI_START: Dict[str, dict] = {
    pid: r["start"] for pid, r in capabilities.UI_RECIPES.items() if r.get("start")
}


def ui_start_for(pid: str) -> Optional[dict]:
    """该 agent 的原生界面怎么起（无 Web UI / 常驻豁免的 agent 返回 None）。"""
    return capabilities.start_for(pid)


PROFILES: List[dict] = [
    # ── Agents ────────────────────────────────────────────────
    {"id": "claude", "name": "Claude Code", "kind": "agent",
     "detect": {"proc": [r"(^|/)claude( |$)", r"claude\.js", "@anthropic-ai/claude-code"]},
     "cli": "claude", "port": None,
     "ui": None,
     "terminal": {"cmd": "claude", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": {"adapter": "claude"},
     "desc": "Anthropic 编码 Agent（CLI/TUI）；对话经 CCR；"
              "（v0.13.80 起 CloudCLI 独立原生界面已拆出为 cloudcli 子菜单，"
              "claude 卡保留纯终端+对话）"},
    {"id": "cloudcli", "name": "CloudCLI", "kind": "agent",
     "detect": {"systemd": ["cloudcli"]},
     "cli": None, "port": 3010,
     "ui": {"url": "http://127.0.0.1:3010", "port": 3010, "unit": "cloudcli", "label": "CloudCLI"},
     "terminal": None, "chat": None,
     # 2026-10-24 用户令「所有 agent 参考 omp 不长驻 按需拉起」：cloudcli 四单元
     # （cloudcli + lan/ts4/ts6 三个 socat 转发）已 stop+disable。停着时 dict ui 的
     # 端口探测不出按钮（discovery 既有机制）；启动命令写在这里当零代码入口。
     # 项目清单直读 auth.db 与服务活死解耦，照常可用。
     "desc": "Claude Code 的 Web 原生宿主 :3010（按需拉起，不长驻）：启动 "
             "systemctl --user start cloudcli；手机/Tailscale 访问另起 "
             "cloudcli-lan/ts4/ts6 三个转发单元；会话列表+项目直达面板读 auth.db，"
             "与服务活死解耦"},
    {"id": "pi", "name": "Pi Agent", "kind": "agent",
     "detect": {"proc": [r"next-server", r"(^|/)pi( |$)"], "systemd": ["pi-web"]},
     "cli": None, "port": 30141, "ui": "http://127.0.0.1:30141",
     "terminal": {"cmd": "pi", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     # 2026-10-24 按需拉起：pi-web + pi-web-relay 两单元 stop+disable，
     # watchdog（crontab 每 5 分钟）看护清单已摘除 ⇒ 停了不会自动回来。
     # CLI 终端入口本就是按需形态（hub pty 拉 TUI），不受影响。
     "desc": "Pi Coding Agent：CLI（v0.85.1，默认交互 TUI）按需拉起终端会话；"
             "原生 Web 界面 :30141 按需启动 systemctl --user start pi-web"
             "（2026-10-24 起不长驻，watchdog 已摘除看护）"},
    # oh-my-pi（omp）：Pi 的 coding-first 分支。2026-10-24 用户令接入（按需拉起、不常驻）。
    # 与现役 pi 三处全不同 ⇒ 可并存零迁移：包名 `@oh-my-pi/pi-coding-agent`（现役是
    # `@earendil-works/pi-coding-agent`）、配置根 `~/.omp/`（现役 `~/.pi/`）、命令 `omp`。
    # 终端入口必须**绝对路径**：`~/.local/bin/omp` 是 /bin/sh 包装脚本（exec 绝对路径的
    # bun 1.3.14）—— omp 本体 shebang `#!/usr/bin/env bun`，服务 PATH 里没有 bun，
    # 与 pi 当年 `#!/usr/bin/env node` 撞系统 node v20 同族（见 child_env 注释）；
    # 包装后解释器不依赖 PATH。安装态 `/vol1/omp/npm-global`（独立 npm 前缀）。
    # detect 两条并存：包装脚本直调（`(^|/)omp`）+ 经 bun 跑 dist/cli.js
    # （`@oh-my-pi/pi-coding-agent`）。⚠ 不能只写 `pi-coding-agent`：pi-web 的包名是
    # `@earendil-works/pi-coding-agent`，漏了厂商前缀会把 pi-web 进程误判成 omp 在跑。
    # 无 ui/port：omp 不带 Web UI（有 RPC/ACP 模式，但 hub 终端卡只拉 TUI）；
    # 无常驻进程/unit，单进程 RSS 约 379 MB ⇒ 只按需拉起。
    # ★ 2026-10-24 用户令「所有 agent 参考 omp 不长驻 按需拉起」——本卡是全军口径的
    #   样板：终端入口 + 无 resident backend。cloudcli/pi/dsh/qwenpaw 四卡已按此整改，
    #   codebuddy 的 workbuddy 两单元经用户明示豁免（小程序通道+桌面 Electron 依赖）。
    {"id": "omp", "name": "oh-my-pi (omp)", "kind": "agent",
     "detect": {"proc": [r"(^|/)omp( |$)", r"@oh-my-pi/pi-coding-agent"]},
     "cli": None, "port": None, "ui": None,
     "terminal": {"cmd": "/home/gztxt/.local/bin/omp", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "oh-my-pi：Pi 的 coding-first 分支（v18.12.6），TUI 按需拉起；模型走 "
             "~/.omp/agent/models.yml（provider ccr → CCR :3456）。增量能力："
             "LSP/DAP、浏览器/桌面自动化、子代理治理"},
    # dsh 自带 Web UI（上游 v1.6.0 起 dshctl 可托管为 systemd 单元）。
    # 2026-10-23：原来只有 B 档动态卡（ui 硬编码 None）⇒ 只能开终端，
    # 用户要"原生界面"又只能在嵌入式终端里手敲 `dsh web`，两次会撞 3080。
    # 现改为静态卡：ui 走 loopback，entries 里再用 _dsh_web_url() 换成带 token 的实时地址。
    # 动态发现那边不会重复出卡 —— _dynamic_cli_agents 的 covered 含本卡 id。
    # 2026-10-24 01:15（用户第三次报障，指出「调研显示可以使用终端」后**改判**）：
    # 上次的「没有 CLI 交互会话」只对了半句 —— DSH **本体不带**终端前端，但终端前端是
    # **外置插件形态**：`dsh plugin --profile tui add @tomowang/dsh-tui` 装好后
    # `dsh --profile tui` 就是全屏 TUI（MIT；peerDeps 钉 ^0.2.1-rc.2，与本机 0.2.1-rc.2 同版）。
    # 实测 2026-10-24 01:14：TUI 起界面 → bash 工具跑 `date -u +%FT%TZ` →
    # 回来 `2026-10-23T17:14:09Z`（文本+真工具调用双证据）。
    # ⇒ 恢复 term entry，命令**必须**是 `dsh --profile tui`：
    #   · 裸 `dsh` 仍会变成 `dsh web`（启动器 `case "${1:-web}"`）⇒ 抢 3080 后的端口、
    #     争写 `~/.dsh`（判例 252 §四.2），这条禁令不变；
    #   · 用绝对路径：服务 PATH 不保证含 ~/.local/bin（同 codex/CODEBUDDY_CLI 的做法）。
    # 前端 NO_TERM 同步清空（判例 253 的机制保留，只去掉 dsh 成员）。
    {"id": "dsh", "name": "DSH", "kind": "agent",
     "detect": {"systemd": ["dsh"], "proc": [r"(^|/)dsh( |$)"]},
     "cli": "dsh", "port": 3080, "ui": "http://192.168.5.102:3080",
     # ui_pin_host：地址里的主机名**不许**被前端 lanUrl() 换成"当前访问名"。
     # dsh 拒绝通配地址，只 bind `DSH_HOST=192.168.5.102`；访问 hub 用 127.0.0.1
     # 时 lanUrl 会把卡片地址改写成 http://127.0.0.1:3080 ⇒ 嵌页 ERR_CONNECTION_REFUSED
     # （2026-10-25 影子实例实测）。其余 agent 都绑 0.0.0.0，改写无害，故只标这一张卡。
     "ui_pin_host": True,
     "terminal": {"cmd": "/home/gztxt/.local/bin/dsh --profile tui",
                  "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "DeepSeek Harness：Web UI :3080 按需启动 systemctl --user start dsh"
             "（2026-10-24 起不长驻；dsh-ts4/ts6 两个 Tailscale 转发单元同步停用，"
             "需要时一起 start）；token 进程级随机，由卡片实时注入，取不到时打开会 401"
             "+ 外置 TUI 终端（dsh --profile tui，@tomowang/dsh-tui）按需拉起"},
    {"id": "jcode", "name": "JCode", "kind": "agent",
     "detect": {"proc": [r"(^|/)jcode( |$)"]},
     "cli": "jcode", "port": None, "ui": None,
     "terminal": {"cmd": "jcode", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": {"adapter": "jcode"},
     "desc": "编码 Agent（CLI/TUI），可终端拉起或经 CCR 对话"},
    {"id": "codex", "name": "Codex CLI", "kind": "agent",
     "detect": {"proc": [r"(^|/)codex( |$)"]},
     "cli": "codex", "port": None, "ui": None,
     # v0.13.82：必须带 --no-alt-screen。codex 的 TUI 默认发 \x1b[?1049h 进备用屏，
     # 而 xterm.js 的备用屏**按设计没有 scrollback** ⇒ 它自己的窗口永远没法向上滚动
     # 查看历史。pty 字节级实测（TERM=xterm-256color，codex 0.160.0）：
     # 默认 ?1049h 命中 1 次；加此开关后归零（同时鼠标捕获序列也一并消失）。
     # 官方 `codex --help` 原文："Disable alternate screen mode. Runs the TUI in
     # inline mode, preserving terminal scrollback history."
     # ⚠️ 已知不足（上游仍 open，故不能靠它单独收口）：#14277/#10331/#20063/#23651
     # 报告即便关掉备用屏，codex 在普通屏做整屏重画时仍可能丢 scrollback。
     # v0.13.83：前端改为**从根上吞掉 ?1049h**（03-agents-cards.js 的 termAltScreenBlock），
     # 备用屏这件事已不靠这个 flag 收口。此处**继续保留**——它让 codex 自己的输出落进主屏
     # scrollback（官方 flag 原文就是这句），且少一层无谓的「发了又被吞」。上面那条
     # 上游不足依然成立，与本 flag 无关。
     "terminal": {"cmd": "codex --no-alt-screen", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "OpenAI Codex CLI（TUI），经 CCR :3456（~/.codex/config.toml 托管 profile）→ 原生终端会话"},
    {"id": "hermes", "name": "Hermes", "kind": "agent",
     "detect": {"proc": [r"(^|/)hermes( |$)", "hermes"]},
     "cli": "hermes", "port": None, "ui": None,
     "terminal": {"cmd": "hermes", "cwd": "/home/gztxt"},
     "chat": None,
     "desc": "TUI Agent（无 Web UI/本地 API）→ 原生终端会话"},
    {"id": "grok", "name": "Grok CLI", "kind": "agent",
     "detect": {"proc": [r"(^|/)grok( |$)", r"(^|/)grok-native( |$)"]},
     "cli": "grok", "port": None, "ui": None,
     "terminal": {"cmd": "grok", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "xAI Grok CLI（native TUI）；默认模型经本机 CCR :3456" +
             "（~/.grok/config.toml [models].default）→ 原生终端会话"},
    {"id": "qwenpaw", "name": "QwenPaw", "kind": "agent",
     "detect": {"proc": [r"qwenpaw app"]},
     "cli": None, "port": 8088, "ui": QWENPAW_UI,
     # 探活真实后端（不是注入代理 :3103）：前端「未响应」覆盖层与启动按钮
     # 都靠它判活，代理常驻会把停着的 qwenpaw 显示成活的。
     "ui_probe": "http://127.0.0.1:8088",
     "terminal": None, "chat": None,
     # 2026-10-24 按需拉起：qwenpaw + qwenpaw-ipv4（IPv4 socat 前端）两单元
     # stop+disable。ui 探针用画像 port 8088 而非注入代理 3103——代理在 hub
     # 进程内常驻，后端停了仍能连上，拿它判活会把死服务显示成活的。
     "desc": "QwenPaw 助理框架（本 Agent 宿主），WebUI :8088 按需启动 "
             "systemctl --user start qwenpaw（2026-10-24 起不长驻；"
             "IPv4 前端另起 qwenpaw-ipv4）" +
             ("；经 hub 注入代理 :%d 统一外框" % EMBED_PROXY_PORT if EMBED_UNIFY else "；直连（注入已关）")},
    {"id": "codebuddy", "name": "CodeBuddy Code", "kind": "agent",
     "detect": {"proc": [r"(^|/)codebuddy( |$)"]},
     "cli": None, "port": 35431, "ui": "http://127.0.0.1:35431",
     # 终端入口用**绝对路径**：该 CLI 只在 WorkBuddy 包内、不在 PATH，`which("codebuddy")`
     # 必然落空；shutil.which() 对带目录分隔符的路径原样返回（实测通过），
     # term.py 的 `which(cmd[0])` 因此走得通 ⇒ 可在项目目录拉起 TUI 会话。
     "terminal": {"cmd": CODEBUDDY_CLI, "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "WorkBuddy 包内捆绑 CLI（`codebuddy --serve`）的遥控 Web 界面 :35431" +
             "（" + LAN_HOST_NOTE + "）→ 原生会话；CLI 亦可在项目目录拉起终端会话；"
             "⚠ workbuddy-daemon/headless 两单元 2026-10-24 用户裁定**豁免**"
             "「不长驻」（小程序通道与桌面 Electron 依赖它们，停了手机端断）"},
    # QoderWake：Qoder 云端 Agent 的本地宿主。2026-10-24 用户报「不是所有 agent 都
    # 按需拉起」后实测抓出：`qoderwake __daemon`（PID 1359，845MB）+
    # `qodercli-wake --sdk`（PID 16085，205MB）两个进程长驻，QoderWake.service
    # enabled + Restart=always ⇒ 本仓画像此前**零登记**，10-24 全军整改没覆盖它。
    # 本卡按方案2补登：静态卡 id 取 `qoder`（压掉 B 档 CLI watchlist 的同 id
    # 动态卡，covered 机制见 _dynamic_cli_agents）；AGENT_PROBE 里 qoder 的
    # verify_argv（qodercli -p）会自动注入，无需重写。
    # detect 双路：unit 名大小写敏感（QoderWake 精确匹配，见 detect_status）；
    # proc 两条覆盖 daemon 与 SDK 唤醒进程（daemon 拉起的子进程，unit 停了它也没了）。
    # 终端入口绝对路径：qodercli 在 ~/.local/bin，服务 PATH 不保证含（同 dsh 做法）。
    {"id": "qoder", "name": "QoderWake", "kind": "agent",
     "detect": {"systemd": ["QoderWake"],
                "proc": [r"(^|/)qoderwake( |$)", "qodercli-wake"]},
     "cli": "qodercli", "port": 19820, "ui": "http://127.0.0.1:19820",
     "terminal": {"cmd": "/home/gztxt/.local/bin/qodercli",
                  "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "Qoder 云端 Agent 本地宿主：Web UI :19820 按需启动 "
             "systemctl --user start QoderWake（2026-10-24 起不长驻；原 "
             "enabled+Restart=always，daemon 845MB + qodercli-wake 205MB "
             "长驻且 hub 无登记）；CLI qodercli（v1.1.65）终端按需拉起；"
             "云端 agent API 走 openapi.qoder.sh"},

    # opencode：上游自带 Web 界面（`opencode web`），但此前只有 B 档动态终端卡
    # （ui 硬编码 None）⇒ 卡片上没有原生界面按钮（2026-10-25 用户报障
    # 「应该是有原生界面的都要显示原生界面按钮」横扫时补登）。改为静态卡：
    # ui/port 钉 :4096（命令见 UI_START，绑定 0.0.0.0 与 pi-web 同口径）。
    # 动态卡由 covered 机制压掉（id/cli 同名）。
    {"id": "opencode", "name": "OpenCode", "kind": "agent",
     "detect": {"proc": [r"(^|/)opencode( |$)", r"opencode web"]},
     "cli": "opencode", "port": 4096, "ui": "http://127.0.0.1:4096",
     "terminal": {"cmd": "opencode", "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "OpenCode：TUI 终端按需拉起；原生 Web 界面 :4096 由卡片「启动原生界面」"
             "按需起（`opencode web --hostname 0.0.0.0 --port 4096`，不长驻）"},

    # ZCode（2026-10-25 用户报「有原生界面就要修复」）：官方 CLI 发行包自带
    # `zcode --web`（Web client + Agent 同一命令）；本机 `~/.local/bin/zcode` 是
    # **源码 CLI 入口** dist/zcode.cjs，不含 --web 分流 ⇒ 扫描判 tui_only，卡片只剩终端。
    # 修复用源码仓自带的官方 runner 按发行包布局搭出 dist/zcode-web（web/server/agent
    # 三个链接 + 官方 bin/zcode.mjs），实测 `--web` 起服务后 / 返 200 text/html。
    # 静态卡压掉扫描登记的同 id 卡（_registry_profiles 的 seen 含 PROFILES id）。
    # detect 双路：runner（UI 起）与 zcode.cjs（TUI 会话）都算 running。
    {"id": "zcode", "name": "ZCode", "kind": "agent",
     "detect": {"proc": [r"zcode-web/bin/zcode\.mjs", r"zcode\.cjs"]},
     "cli": "zcode", "port": 3030, "ui": "http://127.0.0.1:3030",
     "terminal": {"cmd": "/home/gztxt/.local/bin/zcode",
                  "cwd": "/fs/1000/ftp/技术文档"},
     "chat": None,
     "desc": "ZCode（Z.AI 编码 Agent）：原生 Web 界面 :3030 由卡片「启动原生界面」"
             "按需起（官方 `zcode --web` 等价入口，源码仓 runner，不长驻）；"
             "TUI 终端同命令按需拉起；模型走自定义 provider"},

    # ── Gateways（非 Agent，仅快捷方式）──────────────────────
    {"id": "ccr", "name": "CCR Gateway", "kind": "gateway",
     "detect": {"proc": [r"claude-code-router", r"ccr( |$)", "MainThread"]},
     "proc_cwd_hint": "/home/gztxt/.ccr",
     "cli": None, "port": 3456, "ui": None, "panel": "http://192.168.5.102:3458/?ccr_web_token=ccr-web-fixed-token-gztxt-2026",
     "desc": "Claude↔多模型路由网关 :3456/3457/3458（生命线，只观测）"},
    {"id": "ccpocket", "name": "CCPocket Bridge", "kind": "gateway",
     "detect": {"systemd": ["ccpocket-bridge"]},
     "cli": None, "port": 8765, "ui": None, "panel": None,
     "desc": "手机桥接 WebSocket :8765（无独立Web面板）"},

    # ── Services / Memory / Tools（仅快捷方式）────────────────
    {"id": "xray", "name": "Xray Proxy", "kind": "service",
     "detect": {"proc": [r"(^|/)xray run", r"(^|/)xray( |$)"]},
     "cli": None, "port": 7890, "ui": None, "panel": None,
     "desc": "科学上网代理 :7890/7891（proxy-panel 管理）"},
    {"id": "proxy-panel", "name": "Proxy Panel", "kind": "service",
     "detect": {"proc": [r"proxy-panel/server.py"], "systemd": ["proxy-panel"]},
     "cli": None, "port": 8083, "ui": None, "panel": "http://127.0.0.1:8083",
     "desc": "xray 订阅/配置面板 :8083"},
    {"id": "claude-mem", "name": "claude-mem", "kind": "service",
     "detect": {"systemd": ["claude-mem"], "proc": [r"claude-mem"]},
     "cli": None, "port": 37700, "ui": None, "panel": "http://127.0.0.1:37700",
     "desc": "Claude 记忆压缩 worker :37700（自带 Web viewer，可嵌入）"},
    {"id": "gotty", "name": "GoTTY Terminal", "kind": "tool",
     "detect": {"proc": [r"gotty"]},
     "cli": None, "port": 12700, "ui": None,
     "panel": "http://127.0.0.1:12700", "panel_auth": "basic",
     "desc": "系统级 Web 终端 :12700（Basic 鉴权→iframe 弹不出登录框，仅新窗口）"},
    {"id": "tdai", "name": "TDAI Memory", "kind": "memory",
     "detect": {"docker": ["tdai-memory-core"], "proc": []},
     "cli": None, "port": 8420, "ui": None, "panel": None,
     "desc": "向量记忆核心 :8420（API only，无 Web UI）"},
    {"id": "ollama", "name": "Ollama", "kind": "tool",
     "detect": {"docker": ["ollama"], "proc": [r"(^|/)ollama( |$)"]},
     "cli": None, "port": 11434, "ui": None, "panel": None,
     "desc": "本地模型运行时 :11434"},
    {"id": "chromium", "name": "Chromium (容器)", "kind": "tool",
     "detect": {"docker": ["chromium"]},
     "cli": None, "port": 3000, "ui": None,
     "panel": "http://127.0.0.1:3000", "panel_auth": "basic",
     "desc": "无头浏览器服务 :3000（nginx Basic 鉴权→仅新窗口）"},
    {"id": "ai_manager", "name": "AI Manager (Trim)", "kind": "service",
     "detect": {"proc": [r"/usr/trim/bin/ai_manager"]},
     "cli": None, "port": None, "ui": None, "panel": None,
     "desc": "Trim NAS 系统自带 AI 管理服务（root）"},
]

# 系统 Shell 终端卡（①"使用原生终端"；env 可关）
SHELL_PROFILE = {"id": "shell", "name": "系统终端 (bash)", "kind": "tool",
                 "detect": {"always_running": True},
                 "cli": None, "port": None, "ui": None,
                 "terminal": {"cmd": "bash", "cwd": "/home/gztxt"},
                 "chat": None,
                 "desc": "hub 原生 pty 终端（白名单=bash，可用 TERM_ALLOW_BASH=0 关闭）"}


# ── B 档动态发现（2026-09-14；2026-09-21 改多候选名）──────────────
# CLI watchlist 单一真相源（scanner.py 引用此处）；已安装但未进 PROFILES
# 的 CLI 自动补 agent 卡片（终端入口）。
#
# 2026-09-21 修正：原扁平名单与本机实际安装名系统性错配 —— 实装的是
# qodercli / fcc-opencode / fcc-aider / fcc-muse ...，而名单写 qoder / opencode /
# aider，which() 每次落空（grok 也是这样，装了却永不出现）。改为
# 「id → 候选名列表」：同一 Agent 允许多个可执行名（含 fcc- 前缀的
# free-claude-code 包装），逐个 which，首个命中即用；names[0] 作为卡片 id。
# 注意：不做 ~/.local/bin/fcc-* 盲枚举 —— fcc-server / fcc-desktop 等不是
# 【2026-09-25 注】free-claude-code 包已卸载（PT-20260924-15），下方 aliases 里的 `fcc-*` 入口壳
#   在本机已不存在，保留仅为历史命名兼容（不影响检测：`which` 打不到即跳过）。
# Agent，盲枚举会把网关和桌面端也变成卡片。
CLI_ALIASES: Dict[str, dict] = {
    "claude":   {"display": "Claude Code", "names": ["claude"]},
    "codex":    {"display": "Codex CLI",   "names": ["codex", "fcc-codex"]},
    "pi":       {"display": "Pi Agent",    "names": ["pi", "fcc-pi"]},
    "jcode":    {"display": "JCode",       "names": ["jcode"]},
    "hermes":   {"display": "Hermes",      "names": ["hermes", "fcc-hermes"]},
    "openclaw": {"display": "OpenClaw",    "names": ["openclaw"]},
    "qoder":    {"display": "Qoder CLI",   "names": ["qodercli", "qoder"]},
    "opencode": {"display": "OpenCode",    "names": ["opencode", "fcc-opencode"]},
    "aider":    {"display": "Aider",       "names": ["aider", "fcc-aider"]},
    "muse":     {"display": "Muse",        "names": ["muse", "fcc-muse"]},
    "cline":    {"display": "Cline",       "names": ["cline", "fcc-cline"]},
    "dsh":      {"display": "DSH",         "names": ["dsh", "fcc-dsh"]},
    "gemini":   {"display": "Gemini CLI",  "names": ["gemini"]},
    "kimi":     {"display": "Kimi CLI",    "names": ["kimi"]},
    # 实装于 ~/.local/bin/cursor-agent（软链到 ~/.local/share/cursor-agent/versions/<ver>），
    # 不在服务进程 PATH 里 —— 靠 profiles.which() 的 ~/.local/bin 兜底才命中（实测）。
    # 用 cid "cursor" + names 以实装名 cursor-agent 打头（cwd 走动态候选默认，未单独写终端的会自动取项目目录）。
    "cursor":   {"display": "Cursor Agent", "names": ["cursor-agent", "cursor"]},
}

# 扁平候选名（scanner.py 的 cli_installed 报告用，保持同名兼容）
CLI_WATCHLIST = [n for s in CLI_ALIASES.values() for n in s["names"]]
CLI_DISPLAY = {s["names"][0]: s["display"] for s in CLI_ALIASES.values()}


# ── 探针声明（vitals.py 消费；2026-09-21 加）─────────────────────
# aliases      ：该 Agent 在本机可能的可执行名（含 fcc-* 入口壳），逐个 which
# verify_argv  ：一次性真实请求（{p} 换成短提示）。**烧 token**，只在显式「体检」时跑；
#                慢周期只跑 --version/--help 这类不碰模型的探针。
# 服务型 Agent（pi :30141、qwenpaw :8088）不在此表 —— 它们的存在性证据是自有端口应声。
# 未声明 verify_argv 的 Agent 不做 L4，停在 L2（可用但标「未实测应答」）。
#
# {m} = 本次真请求指定的模型（留空则不注入）。2026-09-22 用户口径：测试模型统一用
# Agnes-2.0-flash，但只有走 CCR 的两个 CLI 认它（实测 claude 14s 真答「好」、jcode 9s 出字）；
# grok/qodercli 传 agnes 直接报 "unknown model id"，codex 报 429 —— 它们的 agnes 路由在本机
# 根本不存在，硬塞只会把「模型标识不对」读成「Agent 坏了」。故这三个保留各自默认模型。
AGENT_PROBE: Dict[str, dict] = {
    "claude": {"verify_argv": ["claude", "-p", "{p}", "--model", "{m}"]},
    "jcode":  {"verify_argv": ["jcode", "run", "--model", "{m}", "{p}"]},
    "codex":  {"aliases": ["codex", "fcc-codex"],
               # --skip-git-repo-check 必需：cwd（技术文档根）不在 codex 信任目录里，
               # 缺这个参数 codex 会 rc=1 报「Not inside a trusted directory」——
               # 那是探针自摆一道，不是 Agent 坏了（2026-09-21 实测踩到并已修正）。
               "verify_argv": ["codex", "exec", "--skip-git-repo-check", "{p}"]},
    "hermes": {"aliases": ["hermes", "fcc-hermes"],
               "verify_argv": ["hermes", "-z", "{p}", "--cli"]},
    "grok":   {"aliases": ["grok", "fcc-grok"], "verify_argv": ["grok", "-p", "{p}"]},
    # qoder 卡片 id 是 qoder，实际可执行名 qodercli（WorkBuddy 21:51 的多候选名修正）
    "qoder":  {"aliases": ["qodercli", "qoder"], "verify_argv": ["qodercli", "-p", "{p}"]},
    # opencode：一次性非交互真请求 `opencode run -m <provider/model> "<提示>"`。
    # rt_model 必须带 **provider 前缀** 且用 CCR 的斜杠形式（本机全局配置里 provider id = ccr）；
    # 不能沿用默认 RT_MODEL=Agnes-2.0-flash —— 那是 claude/jcode 才认的写法。
    # 选 stable 非 :free 模型：CCR 每日 12:00 轮换会摘掉 429/403 成员，写死免费模型必然中午失效。
    "opencode": {"aliases": ["opencode", "fcc-opencode"],
                 "verify_argv": ["opencode", "run", "-m", "{m}", "{p}"],
                 "rt_model": "ccr/deepseek/deepseek-v4-flash"},
}


def boundary_claims(p: dict) -> Dict[str, dict]:
    """画像声明的**纳管边界**（P0-5）：有终端入口 ⇒ `exec` 声明为强制，其余只观测。

    声明不等于成立 —— 强制的真伪由 `boundary.NEGATIVE_PROOFS` 里登记的负向测试兑换；
    换不出负向测试的 enforced 声明会被 `boundary.classify_claims` 自动降级为观测，
    文字上只能说「未观察到写操作」。这里只做**数据驱动**的声明，不手抄 agent 名单。"""
    return boundary.claims_for_profile(p)


def _enrich(p: dict) -> dict:
    """把 AGENT_PROBE + CLI_ALIASES 的候选名/探针注入画像（不新增条目，只补字段），
    并挂上 P0-5 的纳管边界四维（`boundary`）——四维必须**每个画像都有**（含无探针的）。"""
    spec = dict(AGENT_PROBE.get(p.get("id"), {}))
    alias = CLI_ALIASES.get(p.get("id"))
    if alias:
        spec.setdefault("aliases", alias["names"])
    out = dict(p)
    for k, v in spec.items():
        out.setdefault(k, v)
    out.setdefault("boundary", boundary.cell_for_profile(out))
    # 能力矩阵（v0.13.156）：终端/原生界面/模型的声明在**这里**统一贴到画像上，
    # 三条注册路径（静态卡 / CLI_ALIASES 动态卡 / agentscan 注册表）共用同一个出口。
    return capabilities.annotate(out)


_cli_dyn_cache: Dict[bool, dict] = {}


def _dynamic_cli_agents(gate: bool = True) -> List[dict]:
    """CLI_ALIASES 中实测已安装（候选名逐个 which）且注册表未收录的 CLI → 动态 agent 画像。

    2026-09-21 第二道闸门：`which()` 命中只代表「有个同名文件能执行」，不代表该 Agent
    真的存在。fcc-* 入口壳会 rc=0 地打印「Could not find X command: muse」后退出，
    单靠 which 一口气冒出 5 张假卡。改为再问 vitals：判为 not_installed 的不进菜单。
    vitals 没结论（首次启动、探针未跑完）时**保留卡片**，宁多不误删。
    结果缓存 30s，避免每次 discovery 都扫盘。"""
    import time
    now = time.monotonic()
    cache = _cli_dyn_cache.setdefault(gate, {"ts": 0.0, "items": []})
    if now - cache["ts"] < 30:
        return cache["items"]
    covered = {p["id"] for p in PROFILES}
    covered |= {p.get("cli") for p in PROFILES if p.get("cli")}
    out: List[dict] = []
    for cid, spec in CLI_ALIASES.items():
        if cid in covered:
            continue
        hit = None
        for name in spec["names"]:
            if name in covered:
                continue
            path = which(name)
            if path:
                hit = (name, path)
                break
        if not hit:
            continue
        name, path = hit
        if gate:                                  # vitals ↔ profiles 互引，延迟导入断开环
            try:
                import vitals
                if not vitals.vitals.show_in_menu(cid):
                    continue
            except Exception:  # noqa: BLE001  判定层挂了不能把菜单清空
                pass
        out.append({
            "id": cid, "name": spec["display"],
            "kind": "agent",
            "detect": {"proc": [rf"(^|/){re.escape(name)}( |$)"]},
            "cli": name, "port": None, "ui": None,
            "terminal": {"cmd": name, "cwd": "/fs/1000/ftp/技术文档"},
            "chat": None, "dynamic": True,
            "desc": f"CLI Agent（自动发现：{path}）→ 原生终端会话"})
    cache["ts"] = now
    cache["items"] = out
    return out


def invalidate_cli_cache() -> None:
    """强制下一次重算候补卡（清掉 _dynamic_cli_agents 的 30s 缓存）。

    判定刚翻案（not_installed → usable）时，30s 缓存会把「刚装好」再压 30 秒，
    表现为「点了扫描还是没出来」。只在重判后调用，不改变缓存本身的降频意图。"""
    _cli_dyn_cache.clear()


def _registry_profiles() -> List[dict]:
    """扫描登记进 `custom_agents(kind='agent')` 的条目 → 画像（v0.13.155）。

    **为什么并进 all_profiles 而不是让 discovery 自己拼接**：菜单、模型设置、体检、
    发布清单、资源页全部消费 `all_profiles()`。注册表并在这里，新装的 agent 才算
    「所有模块一起对齐」（用户 2026-10-25 口径）；并在下游任何一处都是漏一处。
    形状的唯一实现是 `agentscan.registered_profiles()`，这里只做去重与异常兜底。
    """
    try:
        import agentscan
        items = agentscan.registered_profiles()
    except Exception:  # noqa: BLE001  DB 未就绪/注册表坏了都不能把菜单搞空
        return []
    seen = {str(p.get("id")) for p in PROFILES} | set(CLI_ALIASES)
    return [p for p in items if p.get("id") not in seen]


def all_profiles(include_blocked: bool = False) -> List[dict]:
    """include_blocked=True：被判定为假卡的候补也要给出。
    体检必须覆盖它们，否则「今日判为未安装」会被永久固化：被摘掉的卡再也不会被
    重新探测，用户装好了也不会自动回来。菜单（discovery）走带门版的默认值。"""
    out = list(PROFILES) + _dynamic_cli_agents(gate=not include_blocked) + _registry_profiles()
    if os.getenv("TERM_ALLOW_BASH", "1") != "0":
        out.append(SHELL_PROFILE)
    return [_enrich(p) for p in out]


def get_profile(pid: str) -> Optional[dict]:
    for p in all_profiles(include_blocked=True):   # 体检/终端拉起床都可能点名一张被摘的卡
        if p["id"] == pid:
            return p
    return None


def _nvm_bins() -> List[Path]:
    """`~/.nvm/versions/node/*/bin`（多版本按目录名倒序，新版优先）。

    为什么必须补这一路（2026-09-27 实测，不是假想）：hub 跑在 systemd 用户单元里，
    其 `PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin` —— 没有
    nvm。而 `pi`（v0.85.1）这个 npm 全局 CLI 只存在于 `~/.nvm/versions/node/v24.18.0/bin`，
    于是生产进程里 `which('pi')` 落空 ⇒ vitals 判 not_installed ⇒ **整张卡被拦**，
    本机项目页候选框里当然也没有它（交互 shell 里能 which 到，纯属 PATH 假象）。
    只补搜索目录、不改进程 PATH，不影响其它工具链。"""
    root = Path.home() / ".nvm" / "versions" / "node"
    if not root.is_dir():
        return []
    return sorted((d / "bin" for d in root.iterdir() if (d / "bin").is_dir()),
                  key=lambda p: p.parent.name, reverse=True)


def extra_path_dirs() -> List[str]:
    """终端子进程 PATH 需要**前置**的目录（目前 = nvm node bin，新版优先 + bun bin）。

    为什么光有 which() 兜底还不够（2026-09-27 实测）：`which` 只解决"hub 自己找
    不找得到这个 CLI"，解决不了"CLI 起来之后自己再找解释器"。`pi` 的 shebang 是
    `#!/usr/bin/env node`，服务 PATH 里没有 nvm ⇒ 内核把它交给**系统 node v20.20.2**，
    而 pi v0.85.1 的 bundle 用了 `node:fs` 的 `globSync`（Node 22+ 才有）⇒ 启动就
    `SyntaxError: ... does not provide an export named 'globSync'`，终端里是一屏 JS
    堆栈。交互 shell 里 PATH 含 nvm（v24.18.0）⇒ 一切正常，所以「本机跑没事、
    hub 里必崩」。前置目录而不是改 node 软链、也不改 systemd 单元配置。

    2026-10-24 补 bun（同族第二例）：omp（oh-my-pi）shebang 是 `#!/usr/bin/env bun`。
    omp 卡当前用 /bin/sh 包装脚本绕过了这层（见 PROFILES 里 omp 卡注释），这里仍补：
    终端里手敲 `bun`/`bunx`、以及将来任何 bun 系 CLI 都受益。
    ⚠ 只加 `~/.bun/bin`（本机 1.3.14，满足 omp `engines.bun>=1.3.14`）；
      `/var/apps/bunjs/target/bin` 那枚 1.3.9 **不加**——它会抢答案。"""
    dirs = [str(d) for d in _nvm_bins()]
    bun = _bun_bin()
    if bun:
        dirs.append(str(bun))
    return dirs


def _bun_bin() -> Optional[Path]:
    """`~/.bun/bin` 存在才返回（2026-10-24：omp 等 bun 系 CLI 的解释器目录）。

    与 `_nvm_bins()` 同构成函数而非内联 `Path.home()`：L0 测试要能 patch 掉它
    才能钉「没有 bun 的机器上 PATH 原样」这条不变量（见 test_term_child_env.py）。
    只认 `~/.bun/bin`（本机 bun 1.3.14，满足 omp `engines.bun>=1.3.14`）——
    `/var/apps/bunjs/target/bin` 的 1.3.9 不满足，故意不收。"""
    bun_bin = Path.home() / ".bun" / "bin"
    return bun_bin if bun_bin.is_dir() else None


def which(name: str) -> Optional[str]:
    """which + 常见用户 bin 目录兜底（服务进程 PATH 可能不含 ~/.local/bin、~/.npm-global/bin、nvm）"""
    p = shutil.which(name)
    if p:
        return p
    for d in (Path.home() / ".local/bin", Path.home() / ".npm-global/bin",
              Path("/usr/local/bin"), Path.home() / "bin") + tuple(_nvm_bins()):
        f = d / name
        if f.is_file() and os.access(f, os.X_OK):
            return str(f)
    return None


def detect_status(p: dict, procs: List[dict], units: set, dockers: Dict[str, str]) -> str:
    """running / installed / stopped —— 进程、systemd、docker 三路 + CLI 存在性"""
    det = p.get("detect", {})
    if det.get("always_running"):
        return "running"
    for rx in det.get("proc", []):
        pat = re.compile(rx)
        for pr in procs:
            # MainThread 这类通用 comm 需 cmdline 佐证
            if rx == "MainThread":
                if ".ccr" in pr["cmdline"] or "claude-code-router" in pr["cmdline"]:
                    return "running"
                continue
            if pat.search(pr["comm"]) or pat.search(pr["cmdline"]):
                return "running"
    for u in det.get("systemd", []):
        if u in units:
            return "running"
    for d in det.get("docker", []):
        if dockers.get(d) == "running":
            return "running"
    # 未运行 → 是否有"可拉起/已安装"证据
    if p.get("cli") and which(p["cli"]):
        return "installed"
    if det.get("systemd") or det.get("docker"):
        return "stopped"
    if p.get("terminal"):
        return "stopped"
    return "stopped"


def panel_port(p: dict) -> Optional[int]:
    """面板 URL 里的端口（discovery 活性验证用）"""
    src = p.get("panel")
    if isinstance(src, str):
        m = re.search(r":(\d{2,5})", src)
        if m:
            return int(m.group(1))
    return None


def entries_for(p: dict, status: str) -> List[dict]:
    """卡片按钮 = 画像驱动（②服务/工具仅快捷方式；Agent 才有 term/chat）
    dict 形态的 ui（独立宿主如 cloudcli）由 discovery 注入 embed。
    面板类入口带 probe_port，由 discovery 按端口活性过滤（死面板不出按钮）。
    ⚠ agent 的 string ui **不带** probe_port（v0.13.151 改判，见下）：按需拉起下
    stopped 是常态而非故障，端口探活过滤会把每个 Web UI agent 的原生界面按钮
    在正常状态下删掉（2026-10-25 用户报障「dsh的原生界面按钮也没有了」）。"""
    es = []
    kind = p["kind"]
    if kind == "agent":
        ui = p.get("ui")
        # 原生界面拉起（v0.13.152）：start 交给前端「目标界面未响应」覆盖层里的
        # 「启动原生界面」按钮；health_url 用于**探活真实后端**——qwenpaw 的 ui 是
        # hub 注入代理 :3103（进程内常驻），拿代理探活会把停着的后端显示成活的。
        start = ui_start_for(p["id"])
        health = p.get("ui_probe")

        def _ui_entry(etype: str, label: str, url: str) -> dict:
            e = {"type": etype, "label": label, "url": url}
            if start:
                e["start"] = start
            if health:
                e["health_url"] = health
            # 绑定具体网卡的 agent（dsh）：地址原样给出，前端不得按访问名改写。
            if p.get("ui_pin_host"):
                e["pin_host"] = True
            return e
        if isinstance(ui, str):
            # dsh 的访问 token 是进程级随机、只在启动横幅出现一次（见 _dsh_web_url）：
            # 每次现取最新一条，取不到则保留裸地址（打开会 401，属明确降级）。
            if p["id"] == "dsh":
                ui = _dsh_web_url() or ui
            # frame_deny = 控制台自设 X-Frame-Options: DENY，iframe 必被浏览器拒 → 仅新窗口
            # v0.13.151 改判（2026-10-25）：v0.13.149 曾给这两个入口带 probe_port、
            # 由 discovery 按端口活性过滤（「死面板不出按钮」扩展到 agent ui）。它与
            # 同日「全 agent 按需拉起」直接冲突：服务停了才是按需态，过滤后 dsh/pi 等
            # 卡只剩终端，用户点不到原生界面（判例：用户报障 + 快照 10-24 23:1x 那条
            # 「不是故障，是设计态」的结论被本次报障推翻）。停着的服务由前端
            # probeEmbed 出「目标界面未响应」覆盖层（含切模式/重试），按钮本身必须在。
            # probe_port 只保留给 panel 类入口（services 的面板，与 agent 按需政策无关）。
            if not p.get("frame_deny"):
                es.append(_ui_entry("embed", "嵌入会话", ui))
            es.append(_ui_entry("open", "新窗口", ui))
        if p.get("terminal") and status in ("running", "installed", "stopped"):
            es.append({"type": "term", "label": "终端", "agent": p["id"]})
        if p.get("chat"):
            es.append({"type": "chat", "label": "对话", "agent": p["id"]})
    else:
        if p.get("panel"):
            es.append({"type": "open", "label": "打开面板", "url": p["panel"],
                       "probe_port": panel_port(p)})
            # Basic 鉴权面板不生成 embed：浏览器禁止跨源 iframe 内弹认证框，嵌入必然 401 白屏
            if not p.get("panel_auth"):
                es.append({"type": "embed", "label": "嵌入会话", "url": p["panel"],
                           "probe_port": panel_port(p)})
        if p.get("terminal"):
            es.append({"type": "term", "label": "终端", "agent": p["id"]})
    es.append({"type": "detail", "label": "详情"})
    return es
