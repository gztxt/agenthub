"""安装态扫描：从文件系统枚举「刚装上的编码 Agent」，登记成可出卡片的条目。

**为什么存在**（2026-10-25 用户令「装了新 agent 不许再手动补」）：
既有链路两端都是白名单 —— 菜单侧 `profiles.CLI_ALIASES` 手写十几条，扫描侧
`scanner._watchlist()` 直接返回同一张表。不在表里的 CLI 装得再好也永远不出现，
表现为「装完发现没有，再回代码里加一行」（判例 `agent-knowledge/41`）。
本模块补的正是「枚举安装态」这一层。

**识别分级（宁缺毋滥）**：
- A 签名命中：可执行名或包名在 `agent_signatures.json` 里 ⇒ 登记
- B 包语义：可执行文件指向 `node_modules/<pkg>`，包的 description/keywords 自述
  是编码 Agent ⇒ 登记（标 `未实测应答`）
- C 其余：只进返回值 `unknown[]`，**不进菜单**。理由见判例 41：`which` 命中
  ≠ 是 Agent，2026-09-21 一口气冒出过 5 张 fcc-* 假卡，才补的 vitals 闸门。

**只对 A/B 两档执行 `<cli> --version`**（3s 超时），用来把版本写进卡片描述供人
核对。任意未知二进制**永不执行** —— 扫描器没有「试一下它是啥」这种权限。

**代价**：一次扫描 = 十来个目录的 readdir + 少量短命子进程；结果落 `custom_agents`
（`kind='agent'`），带 10 分钟 TTL，由 discovery 在后台触发，不阻塞 `/api/agents`。
"""
from __future__ import annotations

import json
import os
import re
import subprocess
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Dict, List, Optional, Tuple

SIGNATURE_PATH = Path(__file__).resolve().parent / "agent_signatures.json"
_SIG_CACHE: Dict[str, object] = {"mtime": None, "items": []}

PROBE_TIMEOUT = 3.0

#: 一眼排除的解释器/包管理器/系统工具。它们即便落在扫描目录里也不是 Agent，
#: 更不该被 `--version` 探测（探它们只增加扫描成本与不确定行为）。
NEVER_AGENT = re.compile(
    r"^(git|gh|node|npm|npx|pnpm|yarn|bun|bunx|deno|python|python3|pip|pip3|uv|uvx|"
    r"docker|podman|kubectl|helm|rg|fd|jq|yq|curl|wget|ssh|scp|rsync|tmux|screen|"
    r"vim|nvim|less|more|tar|gzip|zip|unzip|ffmpeg|ffprobe|convert|magick|"
    r"gcc|g\+\+|make|cmake|go|cargo|rustc|java|mvn|gradle|tsx|tsc|eslint|prettier|"
    r"vitest|jest|oxlint|playwright|chromium|chrome|firefox|code|socat|xray)$",
    re.I)

#: Tier B 的「自述是编码 Agent」判据。刻意从严：必须是「ai/coding/code + agent/assistant」
#: 这种组合，普通 "cli" 不算 —— 否则 @vscode/* 一整家子都会进来。
AGENT_WORDS = re.compile(
    r"(?i)\b(ai|coding|code|terminal|autonomous)[\s_-]*(agent|assistant)\b")

#: 反向判据：自述里出现这些词，说明它是「给 agent 用的服务/检查器」，不是 agent 本体。
#: 实测依据（2026-10-25 本机一轮扫描的 4 条假卡）：
#:   ecc-agentshield  Security auditor for AI agent configurations  → 审计器
#:   @ccpocket/bridge Bridge server that connects ... sessions       → 桥接服务
#:   @agegr/pi-web    Web UI for the pi coding agent                 → 界面
#:   hermes-web-ui    local-first AI workspace ... dashboard         → 工作台
NOT_AGENT_BODY = re.compile(
    r"(?i)\b(auditor|scanner|bridge|proxy|gateway|web ui|dashboard|workspace|"
    r"server|plugin for|sdk for|wrapper for)\b")

#: Tier B 的可执行名后缀：这些形状天然是服务/界面，不是终端 Agent。
SERVICE_SUFFIX = re.compile(
    r"(?i)(-bridge|-mcp|-server|-web|-ui|-studio|-dashboard|-proxy|-gateway|-api)$")


#: 包名里出现就要警惕的「不是 agent」前缀（工具链/运行时）
NON_AGENT_PKG = re.compile(
    r"(?i)^(@(types|typescript-eslint|vscode|esbuild|swc|babel|rollup|playwright|"
    r"biomejs|oxc|tauri-apps|capacitor|electron|sentry|prisma|vercel|next|nuxt)/|"
    r"(eslint|prettier|webpack|vite|rollup|typescript|ts-node|tsx|nodemon|"
    r"core-js|rxjs|lodash|axios|express|fastify|koa|react|vue|svelte|chalk|"
    r"commander|yargs|dotenv|uuid|zod|ajv|semver|glob|rimraf|chokidar)$)")


def signatures() -> List[dict]:
    """签名表（按 mtime 缓存）：加新 Agent 只需往 JSON 里加一条，不必重启。"""
    try:
        mtime = SIGNATURE_PATH.stat().st_mtime
    except OSError:
        return []
    if _SIG_CACHE["mtime"] == mtime:
        return _SIG_CACHE["items"]  # type: ignore[return-value]
    try:
        items = json.loads(SIGNATURE_PATH.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    items = [i for i in items if isinstance(i, dict) and i.get("id")]
    _SIG_CACHE.update(mtime=mtime, items=items)
    return items


def scan_dirs() -> List[Path]:
    """要扫的目录：PATH 全量 + 服务进程 PATH 够不着、但 CLI 实际装在这里的那几个。

    为什么不能只信 `PATH`：hub 跑在 systemd 用户单元里，`PATH` 只有
    `/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin`，而 `~/.local/bin`
    （cursor-agent）、`~/.npm-global/bin`（grok/qodercli）、nvm bins（pi）全在 PATH 外
    —— 这正是 `profiles.which()` 要兜底的那批目录（见其注释）。
    """
    raw: List[Path] = []
    for part in (os.getenv("PATH") or "").split(os.pathsep):
        if part:
            raw.append(Path(part))
    raw += [Path.home() / ".local/bin", Path.home() / ".npm-global/bin",
            Path.home() / "bin", Path.home() / ".bun/bin"]
    try:
        import profiles
        raw += [Path(d) for d in profiles.extra_path_dirs()]
    except Exception:  # noqa: BLE001  画像层异常不能把扫描搞瘫
        pass
    for base in Path("/vol1").glob("*/npm-global/bin"):
        raw.append(base)
    out: List[Path] = []
    seen = set()
    for d in raw:
        try:
            key = str(d.resolve())
        except OSError:
            key = str(d)
        if key in seen:
            continue
        seen.add(key)
        if d.is_dir():
            out.append(d)
    return out


def _covered() -> Tuple[set, set]:
    """已在册的 id 与可执行名 —— 扫描只补**没被任何一条链路覆盖**的缺口。"""
    ids: set = set()
    names: set = set()
    try:
        import profiles
        for p in profiles.PROFILES:
            if p.get("id"):
                ids.add(str(p["id"]).lower())
            if p.get("cli"):
                names.add(str(p["cli"]).lower())
        ids |= {str(i).lower() for i in profiles.CLI_ALIASES}
        names |= {str(n).lower() for n in profiles.CLI_WATCHLIST}
    except Exception:  # noqa: BLE001
        pass
    return ids, names


def _db_covered(db) -> Tuple[set, set]:
    if db is None:
        return set(), set()
    try:
        rows = db.query("SELECT id, command FROM custom_agents")
    except Exception:  # noqa: BLE001
        return set(), set()
    ids = {str(r["id"]).lower() for r in rows}
    names = {Path(str(r["command"])).name.lower() for r in rows if r.get("command")}
    return ids, names


def _package_meta(path: str) -> Optional[dict]:
    """可执行文件背后的 npm 包元数据（真实路径向上找 package.json，最多 4 层）。

    `~/.npm-global/bin/foo` 是软链到 `../lib/node_modules/<pkg>/...`，`realpath`
    之后父目录里就有 `package.json`。二进制自己编译的（grok-native 之类）没有这层，
    返回 None，交给签名表。
    """
    try:
        real = Path(os.path.realpath(path))
    except OSError:
        return None
    cur = real.parent
    for _ in range(4):
        cand = cur / "package.json"
        if cand.is_file():
            try:
                meta = json.loads(cand.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return None
            if isinstance(meta, dict) and meta.get("name"):
                return meta
            return None
        if cur.parent == cur:
            break
        cur = cur.parent
    return None


def _looks_like_agent_pkg(meta: dict) -> bool:
    name = str(meta.get("name") or "")
    if NON_AGENT_PKG.search(name):
        return False
    text = " ".join([
        name,
        str(meta.get("description") or ""),
        " ".join(str(k) for k in (meta.get("keywords") or []) if isinstance(k, str)),
    ])
    if not AGENT_WORDS.search(text):
        return False
    return not NOT_AGENT_BODY.search(text)


def _pkg_id(meta: dict) -> str:
    raw = str(meta.get("name") or "").split("/")[-1]
    return re.sub(r"[^a-z0-9._-]+", "-", raw.lower()).strip("-") or "agent"


def _run(path: str, args: List[str]) -> str:
    try:
        p = subprocess.run([path, *args], capture_output=True, text=True,
                           timeout=PROBE_TIMEOUT, stdin=subprocess.DEVNULL,
                           env={**os.environ, "NO_COLOR": "1", "TERM": "dumb"})
    except (OSError, subprocess.SubprocessError):
        return ""
    return ((p.stdout or "") + (p.stderr or ""))[:4000]


def probe_text(path: str) -> str:
    """`--version` + `--help` 的合并输出（各 3s 超时）。

    为什么两个都要：`zcode --version` 只回 `0.17.0`（不含名字），签名核对得靠
    `--help` 首行 `zcode 0.17.0`；反过来 `reasonix --version` 自己就带名字。
    只用一个参数会各有盲区。
    """
    return _run(path, ["--version"]) + "\n" + _run(path, ["--help"])


def _version_line(path: str) -> str:
    text = _run(path, ["--version"])
    line = next((ln.strip() for ln in text.splitlines() if ln.strip()), "")
    return line[:80]


#: 能力探测口径的版本号。**改判据就 +1** —— 否则老行的旧结论会被当成新结论一直用：
#: 2026-10-25 实测踩过：`WEB_HINTS` 去掉了 `server`，但 zcode 的 caps 早已落库，
#: 界面仍显示「--help 出现 server」，回填只补 NULL 行，永远修不到已判过的行。
CAPS_VERSION = 2


def _caps_for(cmd: str, prober=None) -> dict:
    """登记时把「有没有 Web 原生界面」问一次，结论随条目落库。

    为什么不在读路径探：`/api/agents` 前端几秒轮询一次，而 `--help` 最坏 4s。
    探测只在扫描（10 分钟 TTL）或 `POST /api/scan/run` 时跑，读路径纯查库。
    问不出来（超时/文件没了）如实记 `probe_failed`，**绝不默认成 TUI-only** ——
    默认成没界面，正是「新增 agent 又没有按钮」这个循环的起点。
    """
    try:
        import capabilities
        state, note = capabilities.probe_web(cmd)
    except Exception as e:  # noqa: BLE001  探测层坏了也不能毁掉整轮扫描
        return {"ui_state": "probe_failed", "ui_note": f"能力探测异常：{type(e).__name__}",
                "v": CAPS_VERSION}
    return {"ui_state": state, "ui_note": note, "v": CAPS_VERSION}


def _item(sig: dict, path: str, *, verified: bool) -> dict:
    return {
        "id": str(sig["id"]).lower(),
        "name": str(sig.get("display") or sig["id"]),
        "command": path,
        "cli": Path(path).name,
        "desc": str(sig.get("desc") or "自动发现的编码 Agent"),
        "verified": verified,
        "source": "scan:cli",
    }


def classify(name: str, path: str, sigs: List[dict], prober=None) -> Optional[dict]:
    """单个可执行文件 → 登记条目；认不出返回 None（调用方记进 unknown）。

    拆成纯函数是为了 L0 可测：不扫目录，`prober` 可注入假实现。

    **名字命中不算证据**：`zcode --version` 只回 `0.17.0`、`goose --version` 只回
    `1.47.0` —— 版本串里没有身份信息；而 `amp`/`crush`/`goose` 这类短名在别的生态里
    也有同名工具（Go 生态就有一个 goose 迁移工具）。只按名字登记就会多一张永远
    起不来的假卡（同族：2026-09-21 那批 fcc-* 假卡）。签名条目必须再过
    `verify_re`（`--version`+`--help` 自证）或包名核对，才许登记。
    """
    low = name.lower()
    meta = _package_meta(path)
    pkg_name = str((meta or {}).get("name") or "").lower()
    for sig in sigs:
        if low not in {str(n).lower() for n in (sig.get("names") or [])}:
            continue
        if pkg_name and pkg_name in {str(n).lower() for n in (sig.get("package_names") or [])}:
            return _item(sig, path, verified=True)
        rex = sig.get("verify_re")
        if rex:
            try:
                if re.search(str(rex), (prober or probe_text)(path)):
                    return _item(sig, path, verified=True)
            except re.error:
                pass
        return None                     # 同名但自证不过 —— 宁可漏，不出假卡
    if not meta:
        return None
    for sig in sigs:
        if pkg_name and pkg_name in {str(n).lower() for n in (sig.get("package_names") or [])}:
            return _item(sig, path, verified=True)
    if SERVICE_SUFFIX.search(name):
        return None
    if not _looks_like_agent_pkg(meta):
        return None
    return {
        "id": _pkg_id(meta),
        "name": _pkg_id(meta).replace("-", " ").title(),
        "command": path,
        "cli": name,
        "desc": (str(meta.get("description") or "")[:180]
                 or "自动发现的编码 Agent（未实测应答）"),
        "verified": False,
        "source": "scan:cli",
    }


def scan(*, db=None, dirs: Optional[List[Path]] = None,
         register: bool = True, probe: bool = True, prober=None) -> dict:
    """扫一遍安装目录，命中就登记（`db` 为空则只报告）。

    返回 `{"found": [...], "added": [...], "unknown": [...], "dirs": n}`。
    `unknown` 是「有可执行文件、但没有一条判据说它是 Agent」的清单 —— 只上报，
    绝不自动进菜单（假卡代价见模块头注释）。
    """
    sigs = signatures()
    ids_cov, names_cov = _covered()
    db_ids_cov, db_names_cov = _db_covered(db)
    ids_cov |= db_ids_cov
    names_cov |= db_names_cov

    found: Dict[str, dict] = {}
    unknown: List[dict] = []
    seen_paths: set = set()
    dirs = dirs if dirs is not None else scan_dirs()

    for d in dirs:
        try:
            entries = list(os.scandir(d))
        except OSError:
            continue
        for e in entries:
            name = e.name
            if not name or name.startswith("."):
                continue
            low = name.lower()
            if NEVER_AGENT.match(low):
                continue
            if low in names_cov or low in ids_cov:
                continue
            try:
                if not e.is_file(follow_symlinks=True):
                    continue
            except OSError:
                continue
            path = e.path
            try:
                real = os.path.realpath(path)
            except OSError:
                real = path
            if real in seen_paths:
                continue
            seen_paths.add(real)
            if not os.access(path, os.X_OK):
                continue
            item = classify(name, path, sigs, prober=prober)
            if item is None:
                unknown.append({"name": name, "path": path,
                                "package": (_package_meta(path) or {}).get("name")})
                continue
            if item["id"] in ids_cov:
                continue
            item["version"] = _version_line(path) if probe else ""
            if probe:
                item["caps"] = _caps_for(path)
            found[item["id"]] = item

    added = register_items(list(found.values()), db) if (register and db is not None) else []
    return {"found": list(found.values()), "added": added,
            "unknown": unknown[:200], "unknown_total": len(unknown),
            "dirs": len(dirs)}


def registered_profiles(db=None) -> List[dict]:
    """扫描登记的 agent（`kind='agent'`）→ 画像形状。

    **一份形状，三个消费者**：菜单出卡（discovery）、终端拉起（profiles.get_profile）、
    模型设置（modelcfg 只要 id）。三处各写一份必然漂 —— 判例：两套真相。
    """
    if db is None:
        try:
            import db as dbmod
            db = dbmod
        except Exception:  # noqa: BLE001
            return []
    try:
        rows = db.query(
            "SELECT * FROM custom_agents "
            "WHERE COALESCE(kind,'service')='agent' ORDER BY created_at")
    except Exception:  # noqa: BLE001
        return []
    out: List[dict] = []
    for r in rows:
        cid = str(r.get("id") or "")
        cmd = str(r.get("command") or "")
        if not cid or not cmd:
            continue
        bin_name = Path(cmd).name
        caps: dict = {}
        raw_caps = r.get("caps")
        if raw_caps:
            try:
                caps = json.loads(raw_caps) if isinstance(raw_caps, str) else dict(raw_caps)
            except (ValueError, TypeError):
                caps = {}
        out.append({
            "id": cid, "name": r.get("name") or cid, "kind": "agent",
            "detect": {"proc": [rf"(^|/){re.escape(bin_name)}( |$)"]},
            "cli": bin_name, "port": None, "ui": None,
            # 能力矩阵（v0.13.156）：`ui` 不再在这里写死 None 就算完 —— 有配方的 agent
            # 由 capabilities.annotate() 按 recipe 补 ui/探针/起法；没有配方的必须带上
            # ui_state/ui_note，让「缺什么」在卡面上写出来。
            "ui_state": caps.get("ui_state"),
            "ui_note": caps.get("ui_note"),
            # 终端入口用**绝对路径**而不是裸名：`child_env()` 只前置 nvm/bun 目录，
            # 不含 `~/.local/bin`（扫描的主战场），裸名在 pty 里会 FileNotFound。
            # 画像里的裸名靠 PATH 兜底，扫描项没有这个前提，就直接给绝对路径。
            "terminal": {"cmd": cmd,
                         "cwd": r.get("working_dir") or "/fs/1000/ftp/技术文档"},
            "chat": None, "dynamic_scan": True,
            "desc": r.get("description") or f"自动发现的编码 Agent（{cmd}）",
        })
    return out


def register_items(items: List[dict], db) -> List[str]:
    """写 `custom_agents`（`kind='agent'`）。已存在且来源是 manual 的**不覆盖** ——
    人工登记的 cwd/参数比扫描猜的准，扫描只补缺口。"""
    if not items or db is None:
        return []
    now = datetime.now(timezone.utc).isoformat()
    existing: Dict[str, dict] = {}
    try:
        for r in db.query("SELECT id, source FROM custom_agents"):
            existing[str(r["id"]).lower()] = r
    except Exception:  # noqa: BLE001
        return []
    added: List[str] = []
    for it in items:
        cid = str(it["id"]).lower()
        row = existing.get(cid)
        if row and str(row.get("source") or "") == "manual":
            continue
        desc = it["desc"]
        if it.get("version"):
            desc = f"{desc}；{it['version']}"
        if not it.get("verified", True):
            desc = f"{desc}（自动发现，未实测应答）"
        try:
            caps = it.get("caps")
            db.execute(
                """INSERT INTO custom_agents(id,name,type,kind,command,args,working_dir,
                     env,port,endpoint,description,source,created_at,updated_at,caps)
                   VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                   ON CONFLICT(id) DO UPDATE SET name=excluded.name,
                     kind=excluded.kind, command=excluded.command,
                     description=excluded.description, source=excluded.source,
                     caps=COALESCE(excluded.caps, custom_agents.caps),
                     updated_at=excluded.updated_at""",
                (cid, it["name"], "agent", "agent", it["command"], "[]",
                 "/fs/1000/ftp/技术文档", "{}", None, None, desc,
                 "scan:cli", now, now,
                 json.dumps(caps, ensure_ascii=False) if caps else None))
        except Exception:  # noqa: BLE001  单条失败不能毁掉整轮扫描
            continue
        added.append(cid)
    return added


_last_scan: Dict[str, float] = {"ts": 0.0}


def backfill_caps(db, limit: int = 50) -> List[str]:
    """给**已登记但没能力结论**的 agent 补一次探测（v0.13.156）。

    为什么必须单独有这一条：`scan()` 里 `ids_cov |= db_ids_cov` 会跳过**已登记**的 id，
    所以老条目永远不会被重新 upsert ⇒ 没有这条回填，历史卡片（zcode/goose/reasonix）
    的 caps 永远是 NULL，卡面上永远显示「未声明」。
    只在扫描轮次里跑（10 分钟一次，单条 `--help` 最坏 4s），不进读路径。
    **口径升级也走这里**：`caps.v < CAPS_VERSION` 的行按未判定处理（见 CAPS_VERSION 注释）。
    """
    if db is None:
        return []
    try:
        rows = db.query(
            "SELECT id, command, caps FROM custom_agents "
            "WHERE COALESCE(kind,'service')='agent' "
            "ORDER BY created_at LIMIT %d" % int(limit))
    except Exception:  # noqa: BLE001
        return []
    done: List[str] = []
    for r in rows:
        cmd = str(r.get("command") or "")
        if not cmd:
            continue
        prev = {}
        try:
            prev = json.loads(r.get("caps") or "{}") or {}
        except (ValueError, TypeError):
            prev = {}
        if prev.get("ui_state") and int(prev.get("v") or 0) >= CAPS_VERSION:
            continue
        try:
            db.execute("UPDATE custom_agents SET caps=? WHERE id=?",
                       (json.dumps(_caps_for(cmd), ensure_ascii=False), r["id"]))
            done.append(str(r["id"]))
        except Exception:  # noqa: BLE001  单条失败不影响其余
            continue
    return done


def scan_if_due(db, ttl_s: float = 600.0) -> dict:
    """TTL 节流版：discovery 每次轮询都会调它，实际扫盘 10 分钟最多一次。

    用户口径是「装了就该自己出现」，所以不能只靠按钮；但 `/api/agents` 前端几秒
    轮询一次，无节流会把 readdir 变成热路径。
    """
    now = time.monotonic()
    if now - _last_scan["ts"] < ttl_s:
        return {"found": [], "added": [], "unknown": [], "dirs": 0, "skipped": True}
    _last_scan["ts"] = now
    out = scan(db=db)
    out["caps_backfilled"] = backfill_caps(db)
    return out
