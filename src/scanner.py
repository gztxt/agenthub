"""只读发现源扫描器（部署方案 S2 落地：动态探针的"无副作用"版本）

三个来源（全部只读，绝不启动/试探进程）：
1. docker ps        —— 运行中容器的端口/镜像
2. systemd user units —— 运行中服务的 ExecStart/端口解析
3. PATH 名单化 CLI   —— 已知编码 Agent 的可执行文件探测（仅安装清单，不注册成卡片）

去重 ID = 'scan-' + SHA256(name|command|port)[:10]（上游"哈希去重"语义直译）。
内置清单已覆盖的端口与 CCR/FCC 网关家族强制排除，避免双头登记。
"""
import hashlib
import json
import re
import subprocess
from typing import Dict, List

# 画像已覆盖的端口/单元由 profiles 动态派生（不再手工维护常量）
def _known_ports_units():
    import profiles
    ports, units = {3102}, set()
    for p in profiles.all_profiles():
        if p.get("port"):
            ports.add(p["port"])
        ui = p.get("ui")
        if isinstance(ui, dict) and ui.get("port"):
            ports.add(ui["port"])
        for src in (ui if isinstance(ui, str) else None, p.get("panel")):
            if src:
                m = re.search(r":(\d{2,5})", src)
                if m:
                    ports.add(int(m.group(1)))
        units.update(p.get("detect", {}).get("systemd") or [])
    return ports, units


BUILTIN_PORTS, KNOWN_UNITS = _known_ports_units()
# 网关/基础设施家族：由内置项或外部守护管理，不重复登记
# 2026-10-08：`agent-hub` → `agenthub`（unit 改名同批）。漏改的症状隐蔽 ——
# EXCLUDE 认不出自家单元 ⇒ 端口扫描把 3102 登记成「外部服务」，
# 表现为资源页多一条自己家的条目，而不是报错。
EXCLUDE_PATTERNS = re.compile(
    r"^(ccr|fcc|opensquilla|agenthub|xray|tailscale|unattended|user@)", re.I)


def _watchlist():
    """CLI 名单单一真相源在 profiles（B 档动态补卡与本扫描共用）"""
    import profiles
    return profiles.CLI_WATCHLIST
PORT_IN_TEXT = re.compile(r"(?:--port[= ]+|[:=])(\d{4,5})(?![\d])")


def _scan_id(name: str, command: str, port) -> str:
    h = hashlib.sha256(f"{name}|{command}|{port}".encode()).hexdigest()
    return "scan-" + h[:10]


# systemd 的 Description= 常写成整句（"Pi Web LAN relay (8084 -> 30141, replaces tailcat for
# phone access)" 实测 514px），直接当名称会把侧栏菜单撑爆。取第一个补充说明标记之前的部分做名称，
# 全句另存 description（行 hover 与详情卡片都读它，信息不丢）。
NAME_CUT = re.compile(r"[(,:：，]|\s+[-–—]\s+")
NAME_MAX = 28


def _short_name(desc: str, fallback: str) -> str:
    s = NAME_CUT.split(desc or "")[0].strip()
    if len(s) > NAME_MAX:
        s = s[:NAME_MAX].rsplit(" ", 1)[0].rstrip(",.;:")
    return s or fallback


def _sh(cmd: list, timeout: int = 8) -> str:
    try:
        r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return r.stdout if r.returncode == 0 else ""
    except (OSError, subprocess.TimeoutExpired):
        return ""


def scan_docker() -> List[Dict]:
    out = _sh(["docker", "ps", "--format", "{{json .}}"])
    found = []
    for line in out.splitlines():
        try:
            c = json.loads(line)
        except json.JSONDecodeError:
            continue
        name = c.get("Names") or c.get("Name") or ""
        if not name or EXCLUDE_PATTERNS.match(name):
            continue
        port = None
        m = re.search(r":(\d{4,5})->", c.get("Ports", ""))
        if m:
            port = int(m.group(1))
        if not port or port in BUILTIN_PORTS:
            continue
        found.append({
            "id": _scan_id(name, "docker:" + (c.get("Image") or ""), port),
            "name": name, "type": "container",
            "port": port, "endpoint": f"http://127.0.0.1:{port}",
            "command": None, "working_dir": None,
            "description": f"Docker 容器 · 镜像 {c.get('Image','')[:40]} · {c.get('State','')}",
            "source": "scan:docker",
        })
    return found


def scan_systemd() -> List[Dict]:
    running = set()
    out = _sh(["systemctl", "--user", "list-units", "--type=service",
               "--state=running", "--plain", "--no-legend"])
    for line in out.splitlines():
        cols = line.split(None, 1)
        if cols:
            running.add(cols[0].removesuffix(".service"))
    found = []
    units_dir = __import__("pathlib").Path.home() / ".config" / "systemd" / "user"
    for unit in sorted(units_dir.glob("*.service")):
        uname = unit.name.removesuffix(".service")
        # 画像已收录的单元（cloudcli/fcc/ccr 家族等）不再重复登记
        if uname not in running or EXCLUDE_PATTERNS.match(uname) or uname in KNOWN_UNITS:
            continue
        try:
            text = unit.read_text(errors="ignore")
        except OSError:
            continue
        exec_m = re.search(r"^ExecStart=(.+)$", text, re.MULTILINE)
        desc_m = re.search(r"^Description=(.+)$", text, re.MULTILINE)
        command = exec_m.group(1).strip() if exec_m else ""
        # %h/%t 展开为家目录近似值（仅展示用途）
        command_disp = command.replace("%h", str(__import__("pathlib").Path.home()))
        port = None
        for src in (command, text):
            m = PORT_IN_TEXT.search(src)
            if m:
                port = int(m.group(1))
                break
        if port and port in BUILTIN_PORTS:
            continue
        full_desc = desc_m.group(1).strip() if desc_m else ""
        name = _short_name(full_desc, uname)
        desc = f"systemd(user) · {uname}"
        if full_desc and full_desc != name:
            desc += f" · {full_desc}"
        found.append({
            "id": _scan_id(uname, command, port),
            "name": name,
            "type": "systemd-service",
            "port": port,
            "endpoint": f"http://127.0.0.1:{port}" if port else "",
            "command": command_disp[:300],
            "working_dir": None,
            "description": desc,
            "source": "scan:systemd",
        })
    return found


def scan_cli_installed() -> List[Dict]:
    """仅报告安装状态（不注册）：卡片补发由 profiles._dynamic_cli_agents 负责（B 档）

    2026-09-21：改用 profiles.which 与卡片补发同口径 —— 服务进程 PATH 只有
    /usr/local/bin:/usr/bin...，shutil.which 对 ~/.local/bin（fcc-* 家族）与
    ~/.npm-global/bin（grok/qodercli）全部落空，扫描报告会漏掉大半已装 CLI。"""
    import profiles
    installed = []
    for cli in _watchlist():
        p = profiles.which(cli)
        if p:
            installed.append({"name": cli, "path": p})
    return installed

def add_boundary(prod: Dict) -> Dict:
    """给扫描产物挂 P0-5 的纳管边界四维（原地补字段并返回同一对象）。

    扫描候选来自 docker/systemd 等第三方服务 —— agenthub 只读探测、**无拦截能力**
    ⇒ 四维永远是 `observed_*`，文字上只能说「未观察到写操作」，不得写「只读」。"""
    import boundary
    prod["boundary"] = boundary.observed_product()
    return prod


def scan_cli_discovered(db=None, register: bool = False) -> Dict:
    """枚举式安装态扫描（v0.13.155）：不依赖名单，发现名单外新装的 CLI Agent。

    与 `scan_cli_installed()` 的区别是**判据**：那个按 `CLI_WATCHLIST` 逐个 `which`，
    只回答「表里的东西在不在」；本函数枚举安装目录，回答「这台机器上装了什么」。
    用户令（2026-10-25）的落点就在这条 —— 装了新 agent 不该再回代码里加白名单。
    """
    import agentscan
    try:
        return agentscan.scan(db=db, register=register)
    except Exception as e:  # noqa: BLE001  扫描失败只降级成空结果，不能毁掉整轮
        return {"found": [], "added": [], "unknown": [], "dirs": 0, "error": f"{type(e).__name__}: {e}"}


def run_scan(auto_register: bool = False, db=None) -> Dict:
    candidates = scan_docker() + scan_systemd()
    # 候选间去重（同名同端口）
    seen = set()
    deduped = []
    for c in candidates:
        key = (c["name"], c.get("port"))
        if key in seen:
            continue
        seen.add(key)
        deduped.append(c)
    for c in deduped:                 # 每条候选都带四维（P0-5：纳管边界进契约）
        add_boundary(c)
    result = {"found": deduped, "added": [], "skipped_existing": [],
              "cli_installed": scan_cli_installed()}
    # 安装态扫描（v0.13.155）：auto_register 时直接登记，回包多两条口径 ——
    # cli_registered = 本轮新登记/更新的 agent id；cli_unknown = 有可执行文件但
    # 没有判据说是 Agent 的清单（只上报，绝不入菜单）。
    disc = scan_cli_discovered(db=db, register=bool(auto_register and db is not None))
    result["cli_discovered"] = [{"name": i["cli"], "path": i["command"],
                                 "id": i["id"]} for i in disc.get("found", [])]
    result["cli_registered"] = disc.get("added", [])
    result["cli_unknown"] = disc.get("unknown", [])[:30]
    if auto_register and db is not None:
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc).isoformat()
        existing = {r["id"] for r in db.query("SELECT id FROM custom_agents")}
        for c in deduped:
            if c["id"] in existing:
                result["skipped_existing"].append(c["id"])
                continue
            db.execute(
                """INSERT INTO custom_agents(id,name,type,command,args,working_dir,
                     env,port,endpoint,description,source,created_at,updated_at)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     port=excluded.port,endpoint=excluded.endpoint,
                     description=excluded.description,updated_at=excluded.updated_at""",
                (c["id"], c["name"], c["type"], c["command"], "[]", None, "{}",
                 c["port"], c["endpoint"], c["description"], c["source"], now, now))
            result["added"].append(c["id"])
    return result
