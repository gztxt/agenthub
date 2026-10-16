"""设置 → GitHub 子菜单：远程地址 / key / 归属 / 克隆落点（v0.13.42）。

背景：GitHub 项目页（src/githubprojects.py）原来把三样东西写死在模块常量里——
API 基址 `https://api.github.com`、token 文件 `/fs/1000/ftp/技术文档/github.txt`、
克隆落点 `/fs/1000/ftp/技术文档`。要换账号、换 GitHub Enterprise、换克隆目录
就得改代码重启。本模块把它们变成**运行时可配**：

- 存哪：Hub 服务端 SQLite 表 `github_settings`（DATA_DIR 内，重启仍在）；
  读取顺序 **DB → 环境变量 → 内置默认**（env 链保持原样，命令行/git 不受影响）；
  另提供「同时回写 github.txt / .env」开关（默认关），让重启后也生效——
  落笔前**时间戳备份**并给出 diff（与 modelcfg 同口径）。
- key（token）：**只写不读**。任何接口都只回掩码（`ghp_****abcd` 或 `****abcd`）
  与来源（db/env/file/无），真实值永不进响应、永不进日志、永不进审计 detail；
  上游错误正文一律过 `tdai_client.scrub(text, token)`（ghp_ 不在 _KEY_PATTERNS
  里，只按位置替换才杀得掉——githubprojects 同款教训）。
- 地址自由度（用户 09-27 裁定）：任意 **https** 主机都能填（含自建 GHES
  `https://git.example.com/api/v3`），但**拒绝明文 http、拒绝内网/回环/链路本地
  与带凭据的 URL**——否则「填错一个地址」就等于把 token 明文发到内网任意主机
  （SSRF + 凭据外泄同案）。DNS 名不解析（保持 L0 零网络），只挡字面 IP 与
  localhost/.local/.internal 一类。
- 操作范围（用户 09-27 裁定）：只读 + 克隆。本模块不新增任何远端写操作；
  「测试连接」只打 /user、/rate_limit 与一次 per_page=1 的仓库探针。

端点（四个写端点一律自带 HUB_PASSCODE 校验：错→401、未配→503 fail-closed，
与 /api/settings/term-token、/api/settings/model/apply 同口径）：
    GET  /api/settings/github          现值 + 掩码 + 来源 + 清单状态
    POST /api/settings/github/test     试连（不落盘）
    POST /api/settings/github/apply    落库（+ 可选回写文件）
    POST /api/settings/github/clear    清掉 DB 设置，回落到 env/默认
    POST /api/settings/github/refresh  清列表缓存并立即重拉
"""
from __future__ import annotations

import hmac
import json
import os
import re
import shutil
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import db
import outbound    # P2 C5：出站地址准入（与 mcpgw 共用同一份判据）
import resilience
import tdai_client
from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

router = APIRouter(tags=["ghsettings"])

#: 可配置键（token 单独处理：只写不读）
KEYS = ("api_base", "git_host", "owner", "clone_base")

DEFAULT_API_BASE = "https://api.github.com"
DEFAULT_GIT_HOST = "github.com"
DEFAULT_CLONE_BASE = "/fs/1000/ftp/技术文档"

#: env 兜底（DB 没设时读这些；与 githubprojects 原常量同义）
ENV_FALLBACK = {
    "api_base": "GITHUB_API_BASE",
    "git_host": "GITHUB_HOST",
    "owner": "GITHUB_OWNER",
    "clone_base": "GITHUB_CLONE_BASE",
}

#: token 文件（回写目标；env GITHUB_TOKEN_FILE 可换位置，与 githubprojects 同名 env）
DEFAULT_TOKEN_FILE = Path("/fs/1000/ftp/技术文档/github.txt")

#: hub 自己的 .env（回写地址/落点/归属；属共享配置面 ⇒ 默认不写，要写必备份）
ENV_FILE = Path(__file__).resolve().parent.parent / ".env"

#: 试连的退避口径（PT-20260927-12 A 档）。理由见 `_gh_get` docstring。
GH_ATTEMPTS = int(os.getenv("HUB_GH_ATTEMPTS", "3"))
GH_SLEEP_BUDGET_S = float(os.getenv("HUB_GH_SLEEP_BUDGET_S", "4"))

#: token 形态前缀（只用于掩码与「类型」提示，不做校验——GitHub 前缀会演进）
_TOKEN_PREFIXES = ("github_pat_", "ghp_", "gho_", "ghu_", "ghs_", "ghr_")

_HOSTNAME_RE = re.compile(r"^[A-Za-z0-9]([A-Za-z0-9.-]{0,251}[A-Za-z0-9])?$")
_OWNER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.-]{0,98}[A-Za-z0-9]$|^[A-Za-z0-9]$")
#: ⚠ `_BAD_NAME_SUFFIXES` 已随判定一起迁到 `outbound.BAD_NAME_SUFFIXES`（P2 C5），
#: 本模块不再自留一份 —— 两份后缀表必然漂移。


class GhSettingsError(Exception):
    """带 HTTP 状态码的领域错误（与 modelcfg.ModelCfgError 同型）。"""

    def __init__(self, status: int, msg: str):
        super().__init__(msg)
        self.status = status


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def _backup(path: Path, tag: str) -> str:
    """写前时间戳备份（军规铁律）。返回备份路径字符串；源文件不存在则返回 ''。"""
    if not path.exists():
        return ""
    bak = path.with_name(f"{path.name}.bak-"
                         f"{datetime.now().strftime('%Y%m%d_%H%M%S')}-{tag}")
    shutil.copy2(path, bak)
    return str(bak)


# ── 存储层 ────────────────────────────────────────────────────────────────
def _ensure_table() -> None:
    db.execute("CREATE TABLE IF NOT EXISTS github_settings ("
               "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")


def _get(key: str) -> str:
    """DB 现值；读不到（表没建/异常）返回 ''——设置页不许被存储层拖垮。"""
    try:
        _ensure_table()
        rows = db.query("SELECT value FROM github_settings WHERE key=?", (key,))
    except Exception:  # noqa: BLE001
        return ""
    return str(rows[0]["value"]) if rows else ""


def _set(key: str, value: str) -> None:
    _ensure_table()
    db.execute("INSERT INTO github_settings(key, value, updated_at) VALUES(?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
               "updated_at=excluded.updated_at", (key, value, _now()))


def _del(key: str) -> None:
    _ensure_table()
    db.execute("DELETE FROM github_settings WHERE key=?", (key,))


def db_get(key: str) -> str:
    """给 githubprojects 用的 DB 层读取（"设置页存了什么"），没存 = ''。
    公开而非 _get：跨模块调用只走这一条缝，便于 L0 对账。"""
    return _get(key)


# ── 解析层：DB → env → 默认 ────────────────────────────────────────────────
def effective() -> Dict[str, str]:
    """当前生效值 + 来源标签（source: db/env/default）。"""
    out: Dict[str, str] = {}
    for k in KEYS:
        v = _get(k)
        if v:
            out[k] = v
            continue
        ev = os.getenv(ENV_FALLBACK[k], "").strip()
        out[k] = ev if ev else {
            "api_base": DEFAULT_API_BASE,
            "git_host": DEFAULT_GIT_HOST,
            "owner": "",
            "clone_base": DEFAULT_CLONE_BASE,
        }[k]
    return out


def sources() -> Dict[str, str]:
    out: Dict[str, str] = {}
    for k in KEYS:
        out[k] = "db" if _get(k) else (
            "env" if os.getenv(ENV_FALLBACK[k], "").strip() else "default")
    return out


def token_file() -> Path:
    return Path(os.getenv("GITHUB_TOKEN_FILE", str(DEFAULT_TOKEN_FILE)))


def _read_token_file() -> str:
    try:
        for line in token_file().read_text(encoding="utf-8", errors="ignore").splitlines():
            s = line.strip()
            if s:
                return s
    except OSError:
        return ""
    return ""


def mask(tok: str) -> str:
    """掩码：保留前缀族与末 4 位，中间全星。空值返回 ''。"""
    t = (tok or "").strip()
    if not t:
        return ""
    for p in _TOKEN_PREFIXES:
        if t.startswith(p):
            body = t[len(p):]
            return f"{p}{'*' * min(len(body) - 4, 12)}{body[-4:]}" if len(body) > 4 \
                else f"{p}{'*' * len(body)}"
    return f"{'*' * 8}{t[-4:]}" if len(t) > 4 else "*" * len(t)


def token_type(tok: str) -> str:
    for p, label in (("github_pat_", "fine-grained PAT"), ("ghp_", "classic PAT"),
                     ("gho_", "OAuth"), ("ghu_", "user-to-server"),
                     ("ghs_", "server-to-server"), ("ghr_", "refresh")):
        if (tok or "").startswith(p):
            return label
    return "未知形态"


def token_view() -> Dict[str, Any]:
    """只出不进：{set, source, mask, len, type, file}。真值永不外泄。"""
    db_tok, env_tok, file_tok = _get("token"), os.getenv("GITHUB_TOKEN", "").strip(), _read_token_file()
    val = db_tok or env_tok or file_tok
    src = "db" if db_tok else ("env" if env_tok else ("file" if file_tok else ""))
    return {"set": bool(val), "source": src or None, "mask": mask(val),
            "len": len(val) if val else 0, "type": token_type(val) if val else "",
            "file": str(token_file())}


# ── 校验层 ────────────────────────────────────────────────────────────────
def _reject_private_host(host: str) -> None:
    """字面 IP 与 localhost 家族一律拒（防把 token 发到内网/本机）。

    判定已抽到 `outbound`（P2 C5）：**一份判据两处用** —— mcpgw 的出站同样过它。
    两份判据必然漂移，而漂移是**静默**的（漏一处＝那条出站路径没护栏）。
    本函数保留只为抛 ghsettings 自己的错误类型（调用点与既有测试都不动）。
    """
    if not (host or "").strip():
        raise GhSettingsError(400, "主机名不能为空")
    why = outbound.blocked_reason(host, allow_private=False)
    if why:
        raise GhSettingsError(400, why)


def _check_api_base(v: str) -> str:
    """https 基址；拒明文、拒带凭据、拒内网。归一化去掉尾斜杠。"""
    s = (v or "").strip()
    if not s:
        raise GhSettingsError(400, "远程地址不能为空")
    u = urlsplit(s)
    if u.scheme != "https":
        raise GhSettingsError(400, "远程地址必须 https（明文 http 会把 key 裸发上链路）")
    if u.username or u.password:
        raise GhSettingsError(400, "远程地址里不能带用户名/密码（key 请填在 Key 一栏）")
    if u.query or u.fragment:
        raise GhSettingsError(400, "远程地址不要带 ?query 或 #fragment")
    if not u.hostname:
        raise GhSettingsError(400, "远程地址解析不出主机名")
    _reject_private_host(u.hostname)
    if u.port is not None and not (1 <= u.port <= 65535):
        raise GhSettingsError(400, "端口号非法")
    return s.rstrip("/")


def _check_git_host(v: str) -> str:
    s = (v or "").strip().rstrip("/")
    if not s:
        return ""
    if "://" in s or "/" in s:
        raise GhSettingsError(400, "Git 主机只填主机名（如 github.com），不要带协议或路径")
    if not _HOSTNAME_RE.match(s):
        raise GhSettingsError(400, "Git 主机名含非法字符")
    _reject_private_host(s)
    return s


def derive_git_host(api_base: str) -> str:
    """api.github.com → github.com；GHES（/api/v3 一类）→ 主机本身。"""
    try:
        host = (urlsplit(api_base).hostname or "").lower()
    except ValueError:
        return ""
    if host == "api.github.com":
        return DEFAULT_GIT_HOST
    return host


def _check_clone_base(v: str) -> str:
    s = (v or "").strip()
    if not s:
        raise GhSettingsError(400, "克隆落点不能为空")
    if not s.startswith("/"):
        raise GhSettingsError(400, "克隆落点必须是绝对路径")
    if any(ch in s for ch in ("\n", "\r", "\x00")):
        raise GhSettingsError(400, "克隆落点含控制字符")
    seg = [x for x in s.split("/") if x]
    if not seg or ".." in seg or "." in seg:
        raise GhSettingsError(400, "克隆落点不能含 . / .. 段")
    p = os.path.normpath(s)
    if p == "/":
        raise GhSettingsError(400, "克隆落点不能是根目录")
    if os.path.exists(p) and not os.path.isdir(p):
        raise GhSettingsError(400, f"克隆落点已存在且不是目录：{p}")
    return p


def _check_owner(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return ""
    if not _OWNER_RE.match(s):
        raise GhSettingsError(400, "归属（owner）含非法字符")
    return s


def _check_token(v: str) -> str:
    s = (v or "").strip()
    if not s:
        return ""                      # 空 = 不改动（清除走 clear）
    if len(s) < 10 or len(s) > 300:
        raise GhSettingsError(400, "key 长度应在 10–300 之间")
    if not s.isascii() or not s.isprintable():
        raise GhSettingsError(400, "key 含不可打印/非 ASCII 字符")
    if any(ch.isspace() for ch in s):
        raise GhSettingsError(400, "key 不能含空格/换行（会弄坏 Authorization 头）")
    return s


def normalize(payload: Dict[str, Any]) -> Tuple[Dict[str, str], List[str]]:
    """把传入字段（None = 不改动）校验并归一化。返回 (字段dict, 警告)。"""
    out: Dict[str, str] = {}
    warn: List[str] = []
    if payload.get("api_base") is not None:
        out["api_base"] = _check_api_base(payload["api_base"])
    if payload.get("git_host") is not None:
        gh = _check_git_host(payload["git_host"])
        # 未给 git_host 但给了 api_base ⇒ 自动派生（GHES 与 github.com 同规）
        if not gh and payload.get("api_base") is not None:
            gh = derive_git_host(out.get("api_base", ""))
        if gh:
            out["git_host"] = gh
        elif payload.get("git_host"):
            warn.append("Git 主机留空 ⇒ 按远程地址派生")
    if payload.get("owner") is not None:
        out["owner"] = _check_owner(payload["owner"])
    if payload.get("clone_base") is not None:
        cb = _check_clone_base(payload["clone_base"])
        out["clone_base"] = cb
        if not os.path.isdir(cb):
            warn.append(f"克隆落点尚不存在：{cb}（首次克隆时创建）")
    if payload.get("token") is not None:
        out["token"] = _check_token(payload["token"])
    return out, warn


# ── 文件回写（可选；默认不写）──────────────────────────────────────────────
def _env_mapping(fields: Dict[str, str]) -> Dict[str, str]:
    m = {}
    if "api_base" in fields:
        m["GITHUB_API_BASE"] = fields["api_base"]
    if "git_host" in fields:
        m["GITHUB_HOST"] = fields["git_host"]
    if "owner" in fields:
        m["GITHUB_OWNER"] = fields["owner"]
    if "clone_base" in fields:
        m["GITHUB_CLONE_BASE"] = fields["clone_base"]
    return m


def _env_file_diff(mapping: Dict[str, str]) -> Optional[Dict[str, Any]]:
    """预览 .env 会怎么改（值本身不敏感：地址/落点/归属）。"""
    if not mapping:
        return None
    text = ENV_FILE.read_text(encoding="utf-8", errors="ignore") if ENV_FILE.exists() else ""
    changes = []
    for k, v in mapping.items():
        cur = ""
        for ln in text.splitlines():
            s = ln.strip()
            if s.startswith(k + "="):
                cur = s.split("=", 1)[1].strip().strip('"')
                break
        changes.append({"key": k, "from": cur or "（未设置）", "to": v,
                        "op": "改" if cur else "新增"})
    return {"file": str(ENV_FILE), "changes": changes}


def _token_file_diff(new_tok: str) -> Optional[Dict[str, Any]]:
    if not new_tok:
        return None
    cur = _read_token_file()
    return {"file": str(token_file()),
            "changes": [{"key": "首行", "from": mask(cur) or "（空）",
                         "to": mask(new_tok), "op": "改" if cur else "新增"}]}


def _write_env(mapping: Dict[str, str]) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not mapping:
        return None, None
    bak = _backup(ENV_FILE, "hub-ghsettings") if ENV_FILE.exists() else ""
    text = ENV_FILE.read_text(encoding="utf-8", errors="ignore") if ENV_FILE.exists() else ""
    lines = text.splitlines()
    for k, v in mapping.items():
        hit = False
        for i, ln in enumerate(lines):
            if ln.strip().startswith(k + "="):
                lines[i] = f'{k}="{v}"'
                hit = True
                break
        if not hit:
            lines.append(f'{k}="{v}"')
    ENV_FILE.parent.mkdir(parents=True, exist_ok=True)
    ENV_FILE.write_text("\n".join(lines).rstrip("\n") + "\n", encoding="utf-8")
    try:
        os.chmod(ENV_FILE, 0o600)
    except OSError:
        pass
    return bak, {"file": str(ENV_FILE), "backups": [bak] if bak else [],
                 "changes": _env_file_diff(mapping)["changes"]}


def _write_token_file(tok: str) -> Tuple[Optional[str], Optional[Dict[str, Any]]]:
    if not tok:
        return None, None
    f = token_file()
    bak = _backup(f, "hub-ghsettings")
    f.parent.mkdir(parents=True, exist_ok=True)
    f.write_text(tok + "\n", encoding="utf-8")
    try:
        os.chmod(f, 0o600)
    except OSError:                                  # 挂载卷不支持 chmod 时不致命
        pass
    return bak, {"file": str(f), "backups": [bak] if bak else [],
                 "changes": _token_file_diff(tok)["changes"]}


# ── 业务动作 ──────────────────────────────────────────────────────────────
def diff(fields: Dict[str, str], write_files: bool = False) -> Dict[str, Any]:
    """不落笔的变更预览（token 只出掩码）。"""
    cur = effective()
    changes = []
    for k in KEYS:
        if k in fields and fields[k] != cur[k]:
            changes.append({"key": k, "from": cur[k] or "（空）", "to": fields[k],
                            "op": "改" if cur[k] else "新增"})
    files = []
    if write_files:
        d = _token_file_diff(fields.get("token", ""))
        if d:
            files.append(d)
        e = _env_file_diff(_env_mapping(fields))
        if e:
            files.append(e)
    if fields.get("token"):
        changes.append({"key": "token", "from": mask(_get("token")) or "（沿用 env/文件）",
                        "to": mask(fields["token"]), "op": "改"})
    return {"changes": changes, "files": files,
            "write_files": bool(write_files), "at": _now()}


def apply(fields: Dict[str, str], write_files: bool = False) -> Dict[str, Any]:
    """落库（+ 可选回写文件）。返回 diff 与备份路径清单（落笔证据）。"""
    out = diff(fields, write_files=write_files)
    backups: List[str] = []
    files_written: List[Dict[str, Any]] = []
    if write_files:
        b1, f1 = _write_token_file(fields.get("token", ""))
        if b1:
            backups.append(b1)
        if f1:
            files_written.append(f1)
        b2, f2 = _write_env(_env_mapping(fields))
        if b2:
            backups.append(b2)
        if f2:
            files_written.append(f2)
    for k in KEYS:
        if k in fields:
            _set(k, fields[k])
    if fields.get("token"):
        _set("token", fields["token"])
    out["backups"] = backups
    out["files_written"] = files_written
    out["applied"] = sorted(list(fields.keys()))
    print(f"[settings] GitHub 设置落笔：{','.join(sorted(fields))}"
          f"（回写文件 {len(files_written)} 个）", flush=True)
    return out


def clear() -> Dict[str, Any]:
    """清掉 DB 设置 ⇒ 回落到 env/内置默认。token 一并清（DB 层）。"""
    before = {k: _get(k) for k in KEYS}
    before["token"] = _get("token")
    for k in list(KEYS) + ["token"]:
        _del(k)
    cleared = [k for k, v in before.items() if v]
    invalidate_cache()
    print(f"[settings] GitHub 设置已清除：{cleared or '（本来就是空的）'}", flush=True)
    return {"cleared": cleared, "effective": effective(), "token": token_view(), "at": _now()}


def invalidate_cache() -> None:
    """清 GitHub 列表缓存（改地址/归属后旧清单立即失效）。延迟导入免循环。"""
    try:
        import githubprojects as gp
        gp._cache["repos"] = None
        gp._cache["at"] = 0.0
    except Exception:  # noqa: BLE001 —— 没挂 githubprojects 时只是少一步
        pass


def list_status() -> Dict[str, Any]:
    """仓库清单的当前状态（不触发拉取：count/cached/age_s）。"""
    try:
        import githubprojects as gp
        repos = gp._cache.get("repos") or []
        return {"count": len(repos), "cached": bool(repos),
                "age_s": round(time.time() - float(gp._cache.get("at") or 0)) if repos else 0}
    except Exception:  # noqa: BLE001
        return {"count": 0, "cached": False, "age_s": 0}


def view() -> Dict[str, Any]:
    """设置页首屏：现值 + 来源 + token 掩码 + 清单状态 + 文件落点。"""
    eff = effective()
    cb = eff["clone_base"]
    return {
        "api_base": eff["api_base"],
        "git_host": eff["git_host"] or derive_git_host(eff["api_base"]),
        "owner": eff["owner"],
        "clone_base": cb,
        "clone_base_exists": os.path.isdir(cb),
        "sources": sources(),
        "token": token_view(),
        "list": list_status(),
        "defaults": {"api_base": DEFAULT_API_BASE, "git_host": DEFAULT_GIT_HOST,
                     "clone_base": DEFAULT_CLONE_BASE},
        "files": {"token_file": str(token_file()), "env_file": str(ENV_FILE),
                  "env_exists": ENV_FILE.exists()},
        "writable": bool(os.getenv("HUB_PASSCODE", "")),
    }


# ── 试连（不落盘）─────────────────────────────────────────────────────────
def _gh_get_raw(url: str, tok: str) -> Tuple[int, Any, Dict[str, str]]:
    """裸 GET（**不含退避、不做错误整形**）。整形留给 `_gh_get` 的 except，
    这样退避层看到的是原始异常——它要靠状态码判该不该重试。"""
    req = urllib.request.Request(url, headers={
        "Authorization": f"Bearer {tok}",
        "Accept": "application/vnd.github+json",
        "X-GitHub-Api-Version": "2022-11-28",
        "User-Agent": "agent-hub",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        raw = resp.read().decode("utf-8", "replace")
        hdrs = {k.lower(): v for k, v in resp.headers.items()}
        try:
            return resp.status, json.loads(raw), hdrs
        except ValueError:
            return resp.status, {"_raw": raw[:200]}, hdrs


def _gh_get(url: str, tok: str) -> Tuple[int, Any, Dict[str, str]]:
    """退避口径同 `githubprojects`：用户在「设置 → GitHub → 试连」里等这个结果，
    且探的是 `/user` + `/rate_limit`——**这一条恰恰是用来判断配额还剩多少的**，
    撞上 429/5xx 立刻重试是最伤的（把刚恢复的额度又打下去）。3 次 / 4 秒预算封顶。

    P1-6：每次 attempt **落库**（`profile_events`，source=`resilience`）—— 此前只看得到
    最终异常，看不到「第 1 次 429、等 2s、第 2 次 200」这种过程，归因全靠猜。"""
    cid = resilience.new_call_id()

    def _rec(rec: Dict[str, Any]) -> None:
        db.log_profile_event("resilience", "gh_settings",
                             "ok" if rec.get("ok") else str(rec.get("kind")),
                             int(rec.get("elapsed_ms") or 0), trace_id=cid, detail=rec)

    try:
        return resilience.instance.call(
            resilience.breaker_key(url, "gh_settings"),
            lambda: _gh_get_raw(url, tok),
            attempts=GH_ATTEMPTS, sleep_budget_s=GH_SLEEP_BUDGET_S,
            record=_rec, call_id=cid,
        )
    except resilience.BreakerOpen:
        return 0, {"error": f"熔断中（{resilience.RECOVERY_TIMEOUT_S:.0f}s 冷却）"
                            "——上一轮试连连续失败，先看网络再试"}, {}
    except urllib.error.HTTPError as e:
        body = ""
        try:
            body = e.read().decode("utf-8", "replace")
        except Exception:  # noqa: BLE001
            pass
        return e.code, {"error": tdai_client.scrub(body, tok)[:300]}, \
            {k.lower(): v for k, v in (e.headers.items() if e.headers else [])}
    except Exception as e:  # noqa: BLE001 —— URLError/超时/DNS
        return 0, {"error": f"{type(e).__name__}: {tdai_client.scrub(str(e), tok)[:160]}"}, {}


def probe(api_base: str, token: str, owner: str = "") -> Dict[str, Any]:
    """试连：/user（身份 + scope 头）+ /rate_limit（配额）+ 一次 per_page=1 仓库探针。
    失败返回 {ok:False, error}——正文过 scrub，token 位置替换。"""
    t0 = time.monotonic()
    base = api_base.rstrip("/")
    code, me, hdrs = _gh_get(base + "/user", token)
    if code != 200:
        why = me.get("error") if isinstance(me, dict) else ""
        hint = ("401：key 无效或已撤销" if code == 401 else
                "403：key 权限不足或触发登录限频" if code == 403 else
                f"HTTP {code}")
        return {"ok": False, "api_base": base, "error": f"{hint} {why}".strip(),
                "took_ms": round((time.monotonic() - t0) * 1000, 1)}
    scopes = [s.strip() for s in (hdrs.get("x-oauth-scopes") or "").split(",") if s.strip()]
    rate: Dict[str, Any] = {}
    rc, rl, _h = _gh_get(base + "/rate_limit", token)
    if rc == 200 and isinstance(rl, dict):
        core = (rl.get("resources") or {}).get("core") or rl.get("rate") or {}
        rate = {"limit": core.get("limit"), "remaining": core.get("remaining"),
                "reset_at": datetime.fromtimestamp(int(core["reset"]), timezone.utc)
                .isoformat(timespec="seconds") if core.get("reset") else None}
    probe_url = (f"{base}/users/{owner}/repos?per_page=1" if owner
                 else f"{base}/user/repos?per_page=1&affiliation=owner,collaborator,"
                      "organization_member")
    pc, pr, _ = _gh_get(probe_url, token)
    repos = {"ok": pc == 200,
             "count": (len(pr) if isinstance(pr, list) else None),
             "note": "" if pc == 200 else f"列仓库探针 HTTP {pc}"}
    return {"ok": True, "api_base": base, "login": me.get("login"),
            "name": me.get("name"), "html_url": me.get("html_url"),
            "scopes": scopes, "rate": rate, "repo_probe": repos,
            "token_type": token_type(token), "token_mask": mask(token),
            "took_ms": round((time.monotonic() - t0) * 1000, 1)}


# ── 端点 ──────────────────────────────────────────────────────────────────
class GhIn(BaseModel):
    """None = 不改动；token 留空 = 不改动（清除走 /clear）。"""
    api_base: Optional[str] = None
    git_host: Optional[str] = None
    owner: Optional[str] = None
    clone_base: Optional[str] = None
    token: Optional[str] = None
    write_files: bool = False
    passcode: str = ""


class TestIn(BaseModel):
    api_base: Optional[str] = None
    token: Optional[str] = None
    owner: Optional[str] = None
    passcode: str = ""


def _check_passcode(pc: str, request: Request, what: str) -> None:
    want = os.getenv("HUB_PASSCODE", "")
    if not want:
        raise HTTPException(503, "未设置 HUB_PASSCODE，GitHub 设置已停用（fail-closed）")
    if not hmac.compare_digest(pc or "", want):
        ip = request.client.host if request and request.client else "?"
        print(f"[settings] GitHub 设置口令错误（{what}）：{ip}", flush=True)
        raise HTTPException(401, "口令错误")


def _guard(e: GhSettingsError) -> HTTPException:
    return HTTPException(e.status, e.args[0])


@router.get("/api/settings/github")
async def settings_github_view():
    """设置 → GitHub 子菜单首屏（现值/来源/掩码/清单状态）。token 真值永不出网。"""
    return view()


@router.post("/api/settings/github/test")
async def settings_github_test(body: TestIn, request: Request):
    """试连：不落盘。地址/key 可用 body 覆盖（先试后存）；缺省用已生效值。"""
    _check_passcode(body.passcode, request, "test")
    eff = effective()
    api_base = _check_api_base(body.api_base) if body.api_base else eff["api_base"]
    tok = _check_token(body.token) if body.token else (
        _get("token") or os.getenv("GITHUB_TOKEN", "").strip() or _read_token_file())
    if not tok:
        raise HTTPException(400, "没有可用 key：请在 Key 一栏填，或先保存一个")
    owner = _check_owner(body.owner) if body.owner is not None else eff["owner"]
    return probe(api_base, tok, owner)


@router.post("/api/settings/github/apply")
async def settings_github_apply(body: GhIn, request: Request):
    """落库（+ 可选回写 github.txt/.env）。回执含 diff 与备份路径（落笔证据）。"""
    _check_passcode(body.passcode, request, "apply")
    try:
        fields, warn = normalize(body.model_dump())
    except GhSettingsError as e:
        raise _guard(e) from e
    if not fields:
        raise HTTPException(400, "没有任何字段要改")
    try:
        out = apply(fields, write_files=bool(body.write_files))
    except GhSettingsError as e:
        raise _guard(e) from e
    except OSError as e:
        raise HTTPException(500, f"回写文件失败：{e}") from e
    invalidate_cache()               # 地址/归属变了 ⇒ 旧清单立即失效
    out["warnings"] = warn
    out["view"] = view()
    return out


@router.post("/api/settings/github/clear")
async def settings_github_clear(body: GhIn, request: Request):
    """清掉 DB 设置（回落到 env/默认）。"""
    _check_passcode(body.passcode, request, "clear")
    return clear()


@router.post("/api/settings/github/refresh")
async def settings_github_refresh(body: GhIn, request: Request):
    """清缓存并立即重拉清单（页面「刷新清单」按钮的后端半程）。"""
    _check_passcode(body.passcode, request, "refresh")
    invalidate_cache()
    try:
        import githubprojects as gp
        d = gp._list_repos(force=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"重拉失败：{type(e).__name__}") from e
    return {"ok": True, "count": d.get("count", 0), "cached": False,
            "errors": d.get("errors") or [], "token": d.get("token"),
            "took_ms": d.get("took_ms"), "view": view()}
