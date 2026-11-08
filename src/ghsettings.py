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

端点（2026-11-07 C 档：四个写端点一律由 writeauth.write_gate 登录闸门保护）：
    GET  /api/settings/github          现值 + 掩码 + 来源 + 清单状态
    POST /api/settings/github/test     试连（不落盘）
    POST /api/settings/github/apply    落库（+ 可选回写文件）
    POST /api/settings/github/clear    清掉 DB 设置，回落到 env/默认
    POST /api/settings/github/refresh  清列表缓存并立即重拉
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import time
import urllib.error
import uuid
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple
from urllib.parse import urlsplit

import db
import outbound    # P2 C5：出站地址准入（与 mcpgw 共用同一份判据）
import resilience
import tdai_client
from fastapi import APIRouter, HTTPException
from pydantic import BaseModel, Field

router = APIRouter(tags=["ghsettings"])

log = logging.getLogger(__name__)

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


def _atomic_write_text(path: Path, text: str, mode: int = 0o600) -> None:
    """原子落盘：同目录临时文件 + fsync + os.replace。
    避免 write_text 直写在崩溃/断电窗口留下半截 .env/token（A5）。"""
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f".{path.name}.tmp-{os.getpid()}-{int(time.time())}")
    try:
        tmp.write_text(text, encoding="utf-8")
        try:
            os.chmod(tmp, mode)
        except OSError:
            pass
        with tmp.open("ab") as fh:
            fh.flush()
            os.fsync(fh.fileno())
        os.replace(tmp, path)
    finally:
        try:
            tmp.unlink(missing_ok=True)  # type: ignore[call-arg]
        except OSError:
            pass


# ── 存储层 ────────────────────────────────────────────────────────────────
def _ensure_table() -> None:
    db.execute("CREATE TABLE IF NOT EXISTS github_settings ("
               "key TEXT PRIMARY KEY, value TEXT NOT NULL, updated_at TEXT NOT NULL)")


#: 仓库源（v0.13.197）：设置页可维护多条「GitHub 地址 + 归属」，项目页按源分组。
#: 一行只描述**一个账号视角**：api_base（api.github.com 或 GHES /api/v3）+
#: git_host（克隆/本地匹配用）+ owner（留空 = 当前 key 可见的全部）。
#: key 仍是全局一份（用户 2026-11-09 裁定 A）；多 GHES 各用各的 key 是后置项。
_SOURCES_DDL = (
    "CREATE TABLE IF NOT EXISTS github_sources ("
    "id TEXT PRIMARY KEY, label TEXT NOT NULL, api_base TEXT NOT NULL, "
    "git_host TEXT NOT NULL, owner TEXT NOT NULL, enabled INTEGER NOT NULL DEFAULT 1, "
    "sort INTEGER NOT NULL DEFAULT 0, created_at TEXT NOT NULL, synced_at TEXT, "
    "repo_count INTEGER NOT NULL DEFAULT 0, last_error TEXT NOT NULL DEFAULT '')")


def _ensure_sources() -> None:
    db.execute(_SOURCES_DDL)
    db.execute("CREATE UNIQUE INDEX IF NOT EXISTS github_sources_uniq "
               "ON github_sources(api_base, owner)")



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


# ── 仓库源（设置页可维护多条；项目页按源分组）─────────────────────────────
# 用户 2026-11-09 裁定：A 分组单位 = 地址 + owner；A star 口径 = 该 owner 的公开
# star（/users/<owner>/starred）；A key = 全源共用一个。
def parse_source(value: str) -> Tuple[Dict[str, str], str]:
    """把一个粘贴值归一化成 {api_base, git_host, owner}；第二返回值是提示语。

    接受四种写法（用户原话「添加和维护仓库地址或者 api」）：
      gztxt                                  → 当前 API 基址 + 归属 gztxt
      https://github.com/gztxt               → api.github.com + gztxt
      https://github.com/gztxt/agenthub      → 同上（按账号归组，仓库明细在项目页）
      https://git.example.com/api/v3         → 自建 GHES（归属留空 = key 可见全部）
    """
    raw = (value or "").strip()
    if not raw:
        raise GhSettingsError(400, "仓库源不能为空")
    if raw.lower().startswith("starred:"):
        owner = _check_owner(raw.split(":", 1)[1].strip())
        if not owner:
            raise GhSettingsError(400, "starred: 后面要跟账号名，如 starred:gztxt")
        eff = effective()
        return {"api_base": eff["api_base"], "git_host": eff["git_host"],
                "owner": owner}, "starred: 形式按账号归组"
    if "://" not in raw:
        if "/" in raw:
            raise GhSettingsError(
                400, "只填账号（如 gztxt）或完整 URL；owner/name 请填账号部分")
        eff = effective()
        return {"api_base": eff["api_base"], "git_host": eff["git_host"],
                "owner": _check_owner(raw)}, ""
    u = urlsplit(raw)
    if u.scheme != "https":
        raise GhSettingsError(400, "仓库源地址必须 https")
    if u.username or u.password:
        raise GhSettingsError(400, "地址里不能带用户名/密码（key 请在 Key 一栏填）")
    if u.query or u.fragment:
        raise GhSettingsError(400, "地址不要带 ?query 或 #fragment")
    host = (u.hostname or "").lower()
    if not host:
        raise GhSettingsError(400, "地址解析不出主机名")
    parts = [p for p in (u.path or "").split("/") if p]
    if host in ("github.com", "www.github.com", "api.github.com"):
        note = ""
        if len(parts) >= 2:
            note = f"已按账号归组（{parts[0]}），仓库明细在项目页里看"
        return {"api_base": DEFAULT_API_BASE, "git_host": DEFAULT_GIT_HOST,
                "owner": _check_owner(parts[0] if parts and parts[0].lower() != "api"
                                      else "")}, note
    if "api" in [p.lower() for p in parts]:
        return {"api_base": _check_api_base(raw), "git_host": _check_git_host(host),
                "owner": ""}, "按 API 基址归组；归属留空 = 当前 key 可见的全部"
    return {"api_base": _check_api_base(f"https://{host}/api/v3"),
            "git_host": _check_git_host(host),
            "owner": _check_owner(parts[0]) if parts else ""}, \
        "非 github.com 主机按 GHES 处理，API 基址取 https://<host>/api/v3"


def sources_all() -> List[Dict[str, Any]]:
    """全部源（含禁用）；存储异常返回空表（项目页退回单源兜底）。"""
    try:
        _ensure_sources()
        return [dict(r) for r in db.query(
            "SELECT * FROM github_sources ORDER BY sort, created_at")]
    except Exception:  # noqa: BLE001
        return []


def source_get(sid: str) -> Optional[Dict[str, Any]]:
    for r in sources_all():
        if r["id"] == sid:
            return r
    return None


def sources_enabled() -> List[Dict[str, Any]]:
    """拉取用：只启用项；一条都没有 ⇒ 用现有全局设置合成一条（零迁移成本）。"""
    rows = ensure_default()
    return [r for r in rows if int(r.get("enabled") or 0) == 1]


def ensure_default() -> List[Dict[str, Any]]:
    """空表时把「设置 → GitHub」的现值物化成第一条源（幂等，只写一次）。

    用户此前只配过一套全局值；这里不改变其行为，只是让它成为「源 #1」。
    """
    rows = sources_all()
    if rows:
        return rows
    eff = effective()
    try:
        _ensure_sources()
        db.execute(
            "INSERT INTO github_sources(id,label,api_base,git_host,owner,enabled,"
            "sort,created_at,synced_at,repo_count,last_error) "
            "VALUES(?,?,?,?,?,1,0,?,NULL,0,'')",
            ("default", (eff["owner"] or "默认（当前 key）"), eff["api_base"],
             eff["git_host"] or derive_git_host(eff["api_base"]), eff["owner"], _now()))
    except Exception:  # noqa: BLE001 —— 并发下唯一索引会撞，撞了就再读一次
        pass
    return sources_all()


def source_add(value: str, label: str = "") -> Dict[str, Any]:
    """添加一条源；同 (api_base, owner) 视为重复，返回既有行（deduped=true）。"""
    fields, note = parse_source(value)
    for r in sources_all():
        if (str(r["api_base"]).lower() == fields["api_base"].lower()
                and str(r["owner"] or "").lower() == fields["owner"].lower()):
            return {"deduped": True, "id": r["id"], "note": note,
                    "source": source_view(r)}
    sid = uuid.uuid4().hex[:10]
    rows = sources_all()
    sort = (max(int(r.get("sort") or 0) for r in rows) + 1) if rows else 0
    _ensure_sources()
    db.execute(
        "INSERT INTO github_sources(id,label,api_base,git_host,owner,enabled,sort,"
        "created_at,synced_at,repo_count,last_error) VALUES(?,?,?,?,?,1,?,?,NULL,0,'')",
        (sid, (label or "").strip()[:60] or fields["owner"] or fields["git_host"] or "源",
         fields["api_base"], fields["git_host"], fields["owner"], sort, _now()))
    invalidate_cache()
    return {"deduped": False, "id": sid, "note": note,
            "source": source_view(source_get(sid) or {})}


def source_patch(sid: str, label: Optional[str] = None,
                 enabled: Optional[bool] = None) -> Dict[str, Any]:
    if not source_get(sid):
        raise GhSettingsError(404, "仓库源不存在")
    if label is not None:
        db.execute("UPDATE github_sources SET label=? WHERE id=?",
                   ((label or "").strip()[:60] or "源", sid))
    if enabled is not None:
        db.execute("UPDATE github_sources SET enabled=? WHERE id=?",
                   (1 if enabled else 0, sid))
    invalidate_cache()
    return {"id": sid, "source": source_view(source_get(sid) or {})}


def source_delete(sid: str) -> Dict[str, Any]:
    if not source_get(sid):
        raise GhSettingsError(404, "仓库源不存在")
    db.execute("DELETE FROM github_sources WHERE id=?", (sid,))
    invalidate_cache()
    return {"deleted": sid}


def touch_source(sid: str, count: int, err: str = "") -> None:
    """拉取回执写回源卡片（失败不抛：回执是附加价值，不是准入条件）。"""
    try:
        db.execute("UPDATE github_sources SET synced_at=?, repo_count=?, last_error=? "
                   "WHERE id=?", (_now(), int(count), (err or "")[:200], sid))
    except Exception:  # noqa: BLE001
        pass


def source_view(r: Dict[str, Any]) -> Dict[str, Any]:
    return {"id": r.get("id"), "label": r.get("label"),
            "api_base": r.get("api_base"), "git_host": r.get("git_host"),
            "owner": r.get("owner") or "", "enabled": int(r.get("enabled") or 0) == 1,
            "synced_at": r.get("synced_at"),
            "repo_count": int(r.get("repo_count") or 0),
            "last_error": r.get("last_error") or "",
            "virtual": r.get("id") == "default" and r.get("created_at") is None}



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
    _atomic_write_text(ENV_FILE, "\n".join(lines).rstrip("\n") + "\n")
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
    _atomic_write_text(f, tok + "\n")
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
    prev = effective()               # 迁移前值：判断「默认源」是否只是全局设置的镜像
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
    _sync_default_source(prev)
    out["backups"] = backups
    out["files_written"] = files_written
    out["applied"] = sorted(list(fields.keys()))
    log.info(f"[settings] GitHub 设置落笔：{','.join(sorted(fields))}"
             f"（回写文件 {len(files_written)} 个）")
    return out


def _sync_default_source(prev: Dict[str, str]) -> None:
    """「默认源」如果还只是全局设置的镜像（三值未被人为改过），跟着全局改。

    为什么需要：v0.13.197 之前只有一套全局值，老用户的习惯是改这里就生效；
    引入多源后若不同步，改「归属」不会影响项目页（源里还存着旧值）——那是
    「改了没生效」的复现。用户已在源列表里单独改过的源不动（不是镜像）。
    """
    try:
        row = next((r for r in sources_all() if r["id"] == "default"), None)
        if not row:
            return
        if (str(row["api_base"]) != prev["api_base"]
                or str(row["git_host"]) != prev["git_host"]
                or str(row["owner"] or "") != prev["owner"]):
            return
        eff = effective()
        host = eff["git_host"] or derive_git_host(eff["api_base"])
        db.execute("UPDATE github_sources SET api_base=?, git_host=?, owner=? "
                   "WHERE id='default'", (eff["api_base"], host, eff["owner"]))
        invalidate_cache()
    except Exception:  # noqa: BLE001 —— 同步失败不许打挂保存动作
        pass


def clear() -> Dict[str, Any]:
    """清掉 DB 设置 ⇒ 回落到 env/内置默认。token 一并清（DB 层）。"""
    before = {k: _get(k) for k in KEYS}
    before["token"] = _get("token")
    for k in list(KEYS) + ["token"]:
        _del(k)
    cleared = [k for k, v in before.items() if v]
    invalidate_cache()
    log.info(f"[settings] GitHub 设置已清除：{cleared or '（本来就是空的）'}")
    return {"cleared": cleared, "effective": effective(), "token": token_view(), "at": _now()}


def invalidate_cache() -> None:
    """清 GitHub 列表缓存（改地址/归属/源列表后旧清单立即失效）。延迟导入免循环。"""
    try:
        import githubprojects as gp
        gp._cache["repos"] = None
        gp._cache["at"] = 0.0
        by = gp._cache.get("by")
        if isinstance(by, dict):
            by.clear()
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
        "repo_sources": [source_view(r) for r in ensure_default()],
        "token": token_view(),
        "list": list_status(),
        "defaults": {"api_base": DEFAULT_API_BASE, "git_host": DEFAULT_GIT_HOST,
                     "clone_base": DEFAULT_CLONE_BASE},
        "files": {"token_file": str(token_file()), "env_file": str(ENV_FILE),
                  "env_exists": ENV_FILE.exists()},
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


class TestIn(BaseModel):
    api_base: Optional[str] = None
    token: Optional[str] = None
    owner: Optional[str] = None





def _guard(e: GhSettingsError) -> HTTPException:
    return HTTPException(e.status, e.args[0])


@router.get("/api/settings/github")
async def settings_github_view():
    """设置 → GitHub 子菜单首屏（现值/来源/掩码/清单状态）。token 真值永不出网。"""
    return view()


@router.post("/api/settings/github/test")
async def settings_github_test(body: TestIn):
    """试连：不落盘。地址/key 可用 body 覆盖（先试后存）；缺省用已生效值。"""
    eff = effective()
    api_base = _check_api_base(body.api_base) if body.api_base else eff["api_base"]
    tok = _check_token(body.token) if body.token else (
        _get("token") or os.getenv("GITHUB_TOKEN", "").strip() or _read_token_file())
    if not tok:
        raise HTTPException(400, "没有可用 key：请在 Key 一栏填，或先保存一个")
    owner = _check_owner(body.owner) if body.owner is not None else eff["owner"]
    return probe(api_base, tok, owner)


@router.post("/api/settings/github/apply")
async def settings_github_apply(body: GhIn):
    """落库（+ 可选回写 github.txt/.env）。回执含 diff 与备份路径（落笔证据）。"""
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
async def settings_github_clear(body: GhIn):
    """清掉 DB 设置（回落到 env/默认）。"""
    return clear()


@router.post("/api/settings/github/refresh")
async def settings_github_refresh(body: GhIn):
    """清缓存并立即重拉清单（页面「刷新清单」按钮的后端半程）。"""
    invalidate_cache()
    try:
        import githubprojects as gp
        d = await gp.list_repos_async(force=True)
    except Exception as e:  # noqa: BLE001
        raise HTTPException(502, f"重拉失败：{type(e).__name__}") from e
    return {"ok": True, "count": d.get("count", 0), "cached": False,
            "groups": d.get("groups") or [], "errors": d.get("errors") or [],
            "token": d.get("token"), "took_ms": d.get("took_ms"), "view": view()}


# ── 端点：仓库源（设置页）─────────────────────────────────────────────────
class SourceIn(BaseModel):
    value: str = Field(min_length=1, max_length=300,
                       description="gztxt / https://github.com/gztxt / GHES API 基址")
    label: str = ""


class SourcePatchIn(BaseModel):
    label: Optional[str] = None
    enabled: Optional[bool] = None


@router.get("/api/settings/github/sources")
async def sources_view():
    """源列表（空表自动物化一条「默认」；不改动任何既有全局设置）。"""
    rows = ensure_default()
    return {"ok": True, "count": len(rows),
            "sources": [source_view(r) for r in rows]}


@router.post("/api/settings/github/sources")
async def sources_add(body: SourceIn):
    try:
        return source_add(body.value, body.label)
    except GhSettingsError as e:
        raise _guard(e) from e


@router.patch("/api/settings/github/sources/{sid}")
async def sources_patch(sid: str, body: SourcePatchIn):
    try:
        return source_patch(sid, label=body.label, enabled=body.enabled)
    except GhSettingsError as e:
        raise _guard(e) from e


@router.delete("/api/settings/github/sources/{sid}")
async def sources_delete(sid: str):
    try:
        return source_delete(sid)
    except GhSettingsError as e:
        raise _guard(e) from e

