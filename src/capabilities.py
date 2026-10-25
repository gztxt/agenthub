"""Agent 能力矩阵：**终端 / 原生界面 / 模型设置** 三件套的唯一真相源（v0.13.156）。

## 为什么要有这个模块

2026-10-25 用户第三次报同一件事：「新增 agent 后没有原生界面按钮，设置项也不同步，
每次都是发现问题再手工修」。查下来不是某一处写错，而是**能力声明被拆成四张手工表**：

| 表 | 位置 | 谁维护 |
|---|---|---|
| 静态画像卡 | `profiles.PROFILES` | 手写 |
| 原生界面启动方式 | `profiles.UI_START` | 手写白名单 |
| 模型设置读写器 | `modelcfg.SPECS` / `UNSUPPORTED` | 手写 |
| 扫描注册 | `agentscan` + `custom_agents` | 自动 |

自动那条路径**只填了「终端」**：`agentscan.registered_profiles()` 与
`profiles._dynamic_cli_agents()` 都把 `"ui": None` 写死 ⇒ 凡不是手写静态卡的 agent，
按设计就永远拿不到原生界面按钮，且**不报错、不显示缺口**。用户看到的是「又没有按钮」，
修的时候只能再一次手写一张卡 —— 循环。

本模块把这四张表收敛成**一张**：一个 id 一条记录，缺失的部分必须**显式声明**
（`UI_TUI_ONLY`）或由探测给出结论（`probe_web`），**不允许静默留空**。
闸门 `tests/test_registry_capability_matrix.py` 会把「漏声明」变成红灯。

## 四种状态（互斥，必有 note）

- `ok`           有原生界面：`ui` 有值（string 或 dict），且（非常驻时）`start` 有值
- `tui_only`     明确没有 Web 界面（TUI 即原生界面）—— **必须写进 UI_TUI_ONLY**
- `missing_recipe` 探测到疑似 Web 能力（`--help` 出现 serve/web/--port），但没有启动配方
- `probe_failed` 探测本身失败（文件没了/超时）——如实说，不猜

## 探测为什么只放在登记时

`--help` 最长 4s，而 `/api/agents` 前端几秒轮询一次。所以探测只在 `agentscan.scan()`
（10 分钟 TTL）跑一次，结论落 `custom_agents.caps` 列；读路径纯查库。
"""
from __future__ import annotations

import json
import re
import subprocess
import time
from pathlib import Path
from typing import Dict, List, Optional, Tuple

#: 原生界面配方：一条记录同时回答「界面在哪」「怎么起」「探活打哪」。
#: 键是 agent id；`url` 为空表示地址由画像自己算（dsh 的 token 是进程级随机）。
UI_RECIPES: Dict[str, dict] = {
    "cloudcli": {"url": "http://127.0.0.1:3010", "port": 3010,
                 "start": {"unit": "cloudcli"}, "label": "CloudCLI"},
    "pi":       {"url": "http://127.0.0.1:30141", "port": 30141,
                 "start": {"unit": "pi-web"}, "label": "Pi Web"},
    "dsh":      {"port": 3080, "start": {"unit": "dsh"}, "label": "dsh"},
    # qwenpaw 的「界面地址」是 hub 注入代理 :3103（进程内常驻），**不是**后端。
    # 探活必须打真后端 :8088，否则停着的后端永远显示成活的（判例见 267 卡）。
    "qwenpaw":  {"url": "http://127.0.0.1:3103", "port": 3103,
                 "probe": "http://127.0.0.1:8088",
                 "start": {"unit": "qwenpaw"}, "label": "QwenPaw"},
    "qoder":    {"url": "http://127.0.0.1:19820", "port": 19820,
                 "start": {"unit": "QoderWake"}, "label": "Qoder"},
    # opencode 是唯一「cmd 式」拉起（无 systemd unit）。绑 0.0.0.0 与 pi-web 同口径：
    # LAN/Tailscale 访问 hub 时前端 lanUrl 会把 127.0.0.1 换成当前主机名。
    "opencode": {"url": "http://127.0.0.1:4096", "port": 4096,
                 "start": {"cmd": ["opencode", "web", "--hostname", "0.0.0.0",
                                   "--port", "4096"],
                           "cwd": "/fs/1000/ftp/技术文档", "port": 4096},
                 "label": "OpenCode"},
    # codebuddy：常驻豁免（WorkBuddy 小程序通道依赖它），所以有界面但**没有起法**。
    # resident=True 是给闸门的判据：常驻项允许 start 为空。
    "codebuddy": {"url": "http://127.0.0.1:35431", "port": 35431,
                  "start": None, "resident": True, "label": "CodeBuddy"},
    # reasonix（2026-10-25 实测 `reasonix web --help` + 起进程）：真 Web UI（`/` 回
    # text/html、title=Reasonix），默认 `-addr 127.0.0.1:8787`。但默认 `auth: token`，
    # 且 token 是**进程级随机**、只出现在启动横幅的 `share:` 行里
    # （`http://127.0.0.1:8787/#token=…`）⇒ 地址必须现从日志捞，与 dsh 同一手法。
    # `--no-open` 必带：否则它会在服务器上 attempt 打开浏览器。
    "reasonix": {"url": "http://127.0.0.1:8787", "port": 8787,
                 "start": {"cmd": ["reasonix", "web", "--addr", "0.0.0.0:8787",
                                   "--no-open"],
                           "cwd": "/fs/1000/ftp/技术文档", "port": 8787},
                 "url_token_re": r"#token=([A-Za-z0-9_\-]{8,})",
                 "url_token_log": "ui-start/reasonix.log",
                 "label": "Reasonix"},
}

#: 探测说「疑似有界面」，但**人工判定过后不是可浏览 Web UI** 的，写在这里（带理由）。
#: 与 `UI_TUI_ONLY` 的区别：这些工具的 `--help` 确实带 serve/web 字样，靠猜分不出来，
#: 必须有人看一眼再定 —— 判过就记在这里，别让它每轮扫描都重新喊一次缺口。
UI_DECIDED: Dict[str, Tuple[str, str]] = {
    "goose": ("tui_only",
              "`goose serve` 是 ACP 协议端点（Start ACP server over HTTP and WebSocket），"
              "实测无 SECRET_KEY 直接拒启、`/` 不出 HTML；Goose 的图形界面是桌面端 App，"
              "不在本机也不可由 hub 拉起"),
}

#: 明确判为「无 Web 原生界面」的 agent（TUI 即原生界面）。
#: **每个静态画像都必须在这里出现或进 UI_RECIPES**，否则闸门红 ——
#: 这就是「新增 agent 别忘了做决定」的机械保证。
UI_TUI_ONLY = {
    "claude", "omp", "jcode", "codex", "hermes", "grok", "cursor",
}

#: `--help` 里出现这些词 ⇒ 疑似自带 Web 界面（足以让「没有按钮」变成显式缺口）。
#: ⚠ 故意**不含 `server`**：2026-10-25 实测三家里两家命中，但 zcode 命中的是
#: `app-server … stdio app server`（stdio 协议，没有 Web 界面）⇒ 单看 `server`
#: 会把「有 MCP/language server」的 CLI 全判成疑似有界面，喊狼来了等于没喊。
#: `serve` 保留：goose 的 `serve Start ACP server over HTTP and WebSocket` 是真命中。
WEB_HINTS = re.compile(
    r"(?<![\w-])(serve|web|webui|web-ui|dashboard|--port|--listen|--host|--addr)(?![\w-])",
    re.IGNORECASE)

_STATES = ("ok", "tui_only", "missing_recipe", "probe_failed")


def recipe_for(agent_id: str) -> Optional[dict]:
    return UI_RECIPES.get(str(agent_id or ""))


def start_for(agent_id: str) -> Optional[dict]:
    """原生界面的拉起方式（前端「启动原生界面」按钮与 POST /ui-start 共用）。"""
    r = recipe_for(agent_id)
    return (r or {}).get("start")


#: 进程级 token 的 URL 缓存（与 dsh 同口径：token 重启即变，缓存只为别每次读盘）。
_URL_CACHE: Dict[str, tuple] = {}
_URL_TTL_S = 10.0


def url_for(agent_id: str) -> Optional[str]:
    """该 agent 原生界面的**可用地址**：静态 url，或 url + 进程级 token。

    为什么要现取：reasonix `auth: token` 的 token 每个进程随机，只印在启动横幅的
    `share:` 行里；写死 `http://…:8787` 会让嵌页稳定 401。取不到就回裸地址 ——
    打开会 401，属**明确降级**，比凭空猜一个 token 强（判例 267 的 v0.13.149 版本
    就是「判活/标题全绿、用户看到一片白」）。
    """
    r = recipe_for(agent_id)
    if not r:
        return None
    url = r.get("url")
    rex = r.get("url_token_re")
    if not url or not rex:
        return url
    now = time.monotonic()
    hit = _URL_CACHE.get(agent_id)
    if hit and now - hit[0] < _URL_TTL_S:
        return hit[1]
    out = url
    log_rel = r.get("url_token_log")
    if log_rel:
        try:
            # ⚠ `config` 是**模块**，实例在 `config.config`（api_agents 用 `from config
            # import config`）—— 写 `import config; config.log_dir` 会 AttributeError，
            # 而这里被 try/except 包着 ⇒ 静默降级成裸地址，表现就是「拉起了但嵌页 401」。
            from config import config as _cfg
            p = Path(_cfg.log_dir) / log_rel
            tail = p.read_text(encoding="utf-8", errors="replace")[-65536:]
            hits = re.findall(rex, tail)
            if hits:
                sep = "&" if "?" in url else "#"
                out = f"{url}{sep}token={hits[-1]}"
        except Exception:  # noqa: BLE001  取不到只降级
            out = url
    _URL_CACHE[agent_id] = (now, out)
    return out


def invalidate_url(agent_id: str) -> None:
    """丢掉该 agent 的 URL 缓存 —— 进程刚被拉起时必须调用。

    token 是**进程级随机**：服务重启后旧 token 立即失效，而缓存 TTL 还有几十秒。
    不失效就会出现「点了启动、服务确实起来了、嵌页却 401 白屏」
    （2026-10-25 实测：shadow 里 POST ui-start 200 → 前端复取 entries 仍是旧 token）。
    """
    _URL_CACHE.pop(str(agent_id or ""), None)


def probe_web(cmd: str, timeout: float = 4.0) -> Tuple[str, str]:
    """跑一次 `<cmd> --help`，判「有没有 Web 界面」。返回 (state, note)。

    只回答**有没有界面入口**，不猜端口也不猜命令 —— 有入口但没配方就是
    `missing_recipe`，让人去补一条 recipe，而不是让 hub 去拼一串猜出来的 argv。
    """
    try:
        cp = subprocess.run([cmd, "--help"], capture_output=True, timeout=timeout,
                            text=True, errors="replace", check=False)
    except subprocess.TimeoutExpired:
        return "probe_failed", f"`{cmd} --help` 超时 {timeout:g}s，未能判定"
    except OSError as e:
        return "probe_failed", f"`{cmd} --help` 起不来（{type(e).__name__}）"
    text = (cp.stdout or "") + "\n" + (cp.stderr or "")
    m = WEB_HINTS.search(text)
    if m:
        return ("missing_recipe",
                f"`--help` 出现 `{m.group(0)}`，疑似自带 Web 界面但尚无启动配方")
    return "tui_only", "CLI/TUI 型：`--help` 未见 serve/web/--port 等界面入口"


def state_of(p: dict) -> Tuple[str, str]:
    """画像 → (状态, 说明)。**纯函数、不探测**，供 annotate 与闸门共用。

    优先级：配方 > 人工判定（UI_DECIDED）> 显式 TUI-only > 扫描时的探测结论 > 未声明。
    人工判定排在探测前：`--help` 只能看出「有 serve 这个词」，看不出那是协议端点还是
    网页；判过的人不该被探测每轮推翻。
    """
    pid = str(p.get("id") or "")
    r = recipe_for(pid)
    if p.get("ui") or r:
        note = (r or {}).get("note") or "有原生界面"
        return "ok", note
    if pid in UI_DECIDED:
        return UI_DECIDED[pid]
    if pid in UI_TUI_ONLY:
        return "tui_only", "TUI 即原生界面（无 Web 界面）"
    if p.get("ui_state") in _STATES:          # 扫描登记时探测出来的结论
        return str(p["ui_state"]), str(p.get("ui_note") or "")
    return ("missing_recipe",
            "未声明：既没有原生界面配方，也没标进 UI_TUI_ONLY —— 属漏登记，不是 TUI-only")


def annotate(p: dict) -> dict:
    """把能力矩阵贴到画像上（`_enrich` 的唯一调用点）。幂等。

    副作用只有三个字段：`ui`（配方里给了地址且画像没写时补上）、`ui_state`、`ui_note`。
    `ui_state != ok` 时把原因**追加进 desc** —— 卡面上看得见，不用点详情才知道缺什么。
    """
    if p.get("kind") != "agent":
        return p
    pid = str(p.get("id") or "")
    r = recipe_for(pid)
    if r and not p.get("ui"):
        # 地址走 url_for：静态 url 或 url+进程级 token（reasonix）。**不要**直接读
        # r["url"] —— 那会把带 token 的嵌页写成稳定 401。
        u = url_for(pid)
        if u:
            p["ui"] = u
            if r.get("port") and not p.get("port"):
                p["port"] = r["port"]
    if r and r.get("probe"):
        # 探活地址**总是**取配方：画像里那条 ui 可能是 hub 注入代理（qwenpaw :3103），
        # 拿它探活永远成功，停着的后端会显示成活的（判例 267）。
        p["ui_probe"] = r["probe"]
    state, note = state_of(p)
    p["ui_state"] = state
    p["ui_note"] = note
    if state in ("missing_recipe", "probe_failed"):
        # 只有**真缺口**才写进 desc：tui_only 是正常形态（终端即原生界面），
        # 给每个 TUI agent 都挂一句"没有 Web 界面"是噪声，会淹掉真正的缺口。
        tag = "（原生界面：%s）" % note
        desc = str(p.get("desc") or "")
        if tag not in desc:
            p["desc"] = (desc + tag) if desc else note
    return p


def matrix() -> List[dict]:
    """全量能力矩阵（诊断/闸门用）：每个 agent 一行。"""
    import profiles
    rows = []
    for p in profiles.all_profiles(include_blocked=True):
        if p.get("kind") != "agent":
            continue
        state, note = state_of(p)
        rows.append({"id": p["id"], "name": p.get("name"),
                     "state": state, "note": note,
                     "ui": p.get("ui"), "start": start_for(p["id"]),
                     "terminal": bool(p.get("terminal")),
                     "dynamic": bool(p.get("dynamic") or p.get("dynamic_scan"))})
    return rows
