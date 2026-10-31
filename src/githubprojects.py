"""GitHub 远端仓库清单 + 即时克隆（v0.13.31）：本机项目页的远端半程。

背景：用户要在「本机项目」同款形态里加一页 GitHub 项目——列出远端全部仓库、
点项目名称选 agent 新建会话；本地没有的仓库，点「新建会话」时先克隆到工作目录
（即时同步）再开会话。用户三裁定：列出全部仓（38 自建 + 34 fork，前端默认隐藏
fork 可开关）；克隆到 /fs/1000/ftp/技术文档（env GITHUB_CLONE_BASE 可改）；
本地匹配**严格按 git remote URL**，不按目录名猜。

设计：
- 列表 GET /api/github/repos：api.github.com /user/repos 分页拉全量，
  5 分钟内存缓存（最坏 ~12 次/小时，限额 5000/h）；
- 本地匹配：localprojects._scan_roots 的仓库集逐个读 .git/config 的 url = 行，
  解析出 owner/name 与远端 full_name 小写对账——同仓异名（本地 techdocs-scripts
  ↔ 远端 scripts）正常命中，同名异仓（本地无 origin 的仓 ↔ 远端同名仓）
  不会误配；
- 克隆 POST /api/github/clone：--depth 1 浅克隆（覆盖「即时同步」），
  180s 上限同步等待，前端按钮禁用 + 「克隆中…」toast；
- stdlib only（urllib，hub venv 无 httpx——hubmcp 同规）。

v0.13.42 去硬编码：API 基址 / git 主机 / 归属 / 克隆落点 / token 全部改读
「设置 → GitHub」（src/ghsettings.py），顺序 **DB → env → 模块常量（默认）**；
模块常量保留为兜底与既有 L0 的 patch 接缝，四个 `api_base()/git_host()/
clone_base()/owner()` 每次现读 ⇒ 改设置即时生效，不用重启。

安全模型：
- token：env GITHUB_TOKEN 优先 → /fs/1000/ftp/技术文档/github.txt 首行
  （config.py env→文件读取链同型），每次现读不缓存值；永不打印、永不进返回值
  （信封只带 token:bool）、永不进审计 detail；上游错误正文过 tdai_client.scrub
  时必须把 token 作第二位置参数——ghp_ 形态不在 _KEY_PATTERNS 里，只按位置替换；
- 克隆目标路径：客户端只给 repo slug（"owner/name"，REPO_RE 全量正则 + 服务端
  白名单双重校验），URL 由服务端现场重构（https://github.com/<slug>.git），
  **客户端永不接触 clone URL**；dest = normpath(join(CLONE_BASE, name)) 过
  containment 检查（skill.py 白名单卫兵同型）+ symlink 拒绝 + 文件占位防御；
- 子进程：argv 列表全仓无 shell=True；-c credential.helper= 关掉机器级凭据
  （NAS 无人值守，凭据助手会挂到超时）+ GIT_TERMINAL_PROMPT=0 让鉴权失败立即死；
  asyncio.create_subprocess_exec 不阻塞事件循环（kb.py 先例）；
- 审计：db.log_asset_event("repo", slug, "create", ...)——action 用冻结枚举
  AUDIT_ACTIONS 里的 "create"（"clone" 不在枚举，会打 action_invalid 标记）；
- 鉴权：GET 只读不拦（与 /api/localprojects 同口径）；POST 走全局 write_gate
  之外再显式 writeauth.decide（cloudcli POST /start 同款保险带）。
- 不埋 runlog（列表防噪声口径同 /api/agents）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request


log = logging.getLogger(__name__)
from pydantic import BaseModel, Field

import db
import ghsettings
import localprojects
import resilience
import tdai_client
import vitals
import writeauth

router = APIRouter(tags=["githubprojects"])

#: 仓库列表的退避口径（PT-20260927-12 A 档）。3 次尝试 = 首发 + 2 退；
#: 4 秒总预算 = 最坏情况 0.5s + 1s 两次退避，超出即放弃并把 429 如实报给用户。
GH_ATTEMPTS = int(os.getenv("HUB_GH_ATTEMPTS", "3"))
GH_SLEEP_BUDGET_S = float(os.getenv("HUB_GH_SLEEP_BUDGET_S", "4"))

#: api.github.com 基址（git 直连与 API 直连本机均实测可达）
API_BASE = "https://api.github.com"

#: token 文件（env GITHUB_TOKEN_FILE 可换位置；真源 600 perms）
TOKEN_FILE = Path(os.getenv("GITHUB_TOKEN_FILE",
                            "/fs/1000/ftp/技术文档/github.txt"))

#: 克隆落点（用户裁定：/fs/1000/ftp/技术文档——即技术文档仓本身，其 .gitignore
#: 默认遮蔽新目录，git status 不会被打爆）
CLONE_BASE = os.path.abspath(os.getenv("GITHUB_CLONE_BASE",
                                       "/fs/1000/ftp/技术文档"))

#: 列表缓存：v0.13.33 起**拉一次永久缓存**（用户裁定「每次拉取就是浪费资源」；
#: 服务重启自然清空，页面「刷新」按钮 = ?force=1 强制重拉）。不再用 TTL。
#: 真正需要新数据的时刻——选中仓库要开会话——走 /sync 单仓核对（HEAD SHA
#: 比对，1 次 API 调用），不整表重拉。
LIST_TTL_S = 0          # 0 = 永不过期（保留常量名：测试与信封口径钉着）

#: 克隆超时（秒）：--depth 1 浅克隆足够；超时 kill + 守卫清理
CLONE_TIMEOUT_S = 180

#: repo slug 形状：owner/name，字符集收紧到 GitHub 实际允许的集合
REPO_RE = re.compile(r"^[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+$")

#: https remote → slug（含 .git 后缀/大写 host/尾斜杠）
_REMOTE_RE = re.compile(
    r"^https?://(?:www\.)?github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    re.I)
#: ssh remote → slug（git@github.com:o/r 与 ssh://git@github.com/o/r）
_SSH_RE = re.compile(
    r"^(?:git@|ssh://git@)github\.com[:/]([^/\s]+/[^/\s]+?)(?:\.git)?/?$",
    re.I)

#: 列表内存缓存（值 + 拉取时刻；模块级单例。token 值绝不入缓存）
_cache: Dict[str, Any] = {"at": 0.0, "repos": None}


# ── v0.13.42：地址/key/落点不再硬编码，改读「设置 → GitHub」────────────────
# 读取顺序 **DB（设置页）→ env → 模块常量（默认值）**。模块常量刻意保留为
# 兜底值：它是既有 L0 用例 patch 的接缝（gp.CLONE_BASE 一类），也是 env 没配时
# 的行为基线。四个函数每次现读（不缓存），改设置即时生效、无需重启。
def _db(key: str) -> str:
    """设置页存的现值；没存/存储异常 ⇒ ''（绝不因为读不到而让页面降级失败）。"""
    try:
        return ghsettings.db_get(key)
    except Exception:  # noqa: BLE001
        return ""


def api_base() -> str:
    """API 基址（含自建 GHES 的 https://host/api/v3）。"""
    return (_db("api_base") or os.getenv("GITHUB_API_BASE", "").strip().rstrip("/")
            or API_BASE)


def git_host() -> str:
    """git 直连主机名（远端匹配与克隆 URL 都按它生成，不再写死 github.com）。"""
    return (_db("git_host") or os.getenv("GITHUB_HOST", "").strip()
            or ghsettings.derive_git_host(api_base()))


def clone_base() -> str:
    """克隆落点（绝对路径，containment 判据）。env 每次现读（模块常量是
    导入时的快照，只作兜底——patch 它仍是既有 L0 的接缝）。"""
    return os.path.abspath(_db("clone_base")
                           or os.getenv("GITHUB_CLONE_BASE", "").strip() or CLONE_BASE)


def owner() -> str:
    """归属过滤（留空 = 当前账号可见的全部仓库）。"""
    return _db("owner") or os.getenv("GITHUB_OWNER", "").strip()


_HOST_RE_CACHE: Dict[str, Any] = {}


def _host_regex(host: str):
    """按配置主机生成 remote 正则（host 可配 ⇒ 正则不能写死）。"""
    pat = _HOST_RE_CACHE.get(host)
    if pat is None:
        h = re.escape(str(host or "").lower())
        pat = re.compile(
            r"^(?:https?://(?:www\.)?" + h + r"[:/]|git@" + h + r"[:/]|ssh://git@"
            + h + r"[:/])([^/\s]+/[^/\s]+?)(?:\.git)?/?$", re.I)
        _HOST_RE_CACHE[host] = pat
    return pat


def _read_token() -> str:
    """DB（设置页）→ env → 文件首行；读不到返回空串（调用方据此走降级信封）。
    永不打印、永不入返回值。"""
    tok = _db("token") or os.getenv("GITHUB_TOKEN", "")
    if tok:
        return tok.strip()
    try:
        first = TOKEN_FILE.read_text(encoding="utf-8",
                                     errors="ignore").splitlines()
        return first[0].strip() if first else ""
    except OSError:
        return ""


def _remote_slug(url: str) -> Optional[str]:
    """解析 git remote URL → "owner/name"（小写 host 由正则 re.I 保证）。
    只认**当前配置的 git 主机**（v0.13.42 起可配，默认 github.com）：
    https://evil.com/github.com/x 一类 URL 在任何配置下都解析失败。"""
    u = (url or "").strip()
    m = _host_regex(git_host()).match(u)
    if not m:
        return None
    return m.group(1).lower()


def _parse_gitdir(g: Path) -> Optional[str]:
    """读 worktree 的 .git 文件里的 gitdir: 行 → normpath 目标（localprojects 同型）。"""
    try:
        if not g.is_file():
            return None
        first = g.read_text(encoding="utf-8", errors="ignore").splitlines()[0].strip()
    except (OSError, IndexError):
        return None
    if not first.startswith("gitdir:"):
        return None
    target = first[len("gitdir:"):].strip()
    if not target:
        return None
    t = Path(target)
    if not t.is_absolute():
        t = g.parent / t
    return os.path.normpath(str(t))


def _repo_config(p: str) -> Optional[Path]:
    """仓库路径 → .git/config 的路径（兼容 worktree：.git 文件指路）。"""
    d = Path(p)
    g = d / ".git"
    if g.is_dir():
        return g / "config"
    if g.is_file():
        gd = _parse_gitdir(g)
        if gd:
            c = Path(gd) / "config"
            return c if c.is_file() else None
    return None


def _remote_slugs() -> Dict[str, List[str]]:
    """本机各 git 仓的 remote slug → 本地路径列表。
    复用 localprojects._scan_roots 的仓库集（含 v0.13.31 精度收紧后的结果），
    逐仓读 .git/config 找全部 url = 行——strict remote 匹配的证据源。
    根自身是仓库的（技术文档=gztxt/technical-docs）也计入——它不在本机项目
    列表里（root 不列），但在 GitHub 页必须显示「本地已有」。"""
    out: Dict[str, List[str]] = {}
    candidates: List[str] = []
    try:
        projects, _, _ = localprojects._scan_roots(localprojects.ROOTS)
        candidates = [p["path"] for p in projects]
        for r in localprojects.ROOTS:              # 根自身是仓库的补进候选
            root = Path(r).expanduser()
            if (root / ".git").is_dir() or (root / ".git").is_file():
                candidates.append(str(root))
    except Exception:  # noqa: BLE001 —— 扫描面失败不能拖垮整个列表
        return out
    for path in candidates:
        cfg = _repo_config(path)
        if not cfg:
            continue
        try:
            txt = cfg.read_text(encoding="utf-8", errors="ignore")
        except OSError:
            continue
        for m in re.finditer(r"^\s*url\s*=\s*(\S+)", txt, re.M):
            slug = _remote_slug(m.group(1))
            if slug:
                out.setdefault(slug, []).append(path)
    return out


def _fetch_page(url: str, tok: str) -> List[Dict[str, Any]]:
    """一页仓库清单（**裸调用，不含退避**）。只做「发出去 + 解析」，不做错误整形——
    整形留在 `_fetch_all` 的 except 里，这样退避层看到的才是原始异常
    （它要靠 HTTP 状态码判「该不该重试」）。"""
    req = urllib.request.Request(url, headers={
        "Authorization": f"token {tok}",
        "User-Agent": "agent-hub",
    })
    with urllib.request.urlopen(req, timeout=15) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _fetch_all(tok: str) -> Dict[str, Any]:
    """拉全部仓库（分页到空页）。失败返回 {ok:False, error}——降级不抛。

    退避口径（PT-20260927-12 A 档）：**这里是真该退避的地方** ——
    用户在等这一页，且GitHub API 限额 5000/h、次级限流是本机 recurring failure
    （实测 `vitals.RATE_RE` 就在为429 兜底）。给 3 次尝试、总退避上限 4 秒：
    `Retry-After` 若说等 60 秒，超过预算就**如实放弃**并把 429 报给用户，
    而不是让页面转圈 60 秒。
    """
    base = api_base()
    own = owner()
    repos: List[Dict[str, Any]] = []
    page = 1
    while page <= 10:
        # 归属（owner）可配：留空 = 当前账号可见的全部；填了 = 只看该归属的公开/可见仓
        url = ((f"{base}/users/{own}/repos?per_page=100&sort=pushed&page={page}")
               if own else
               (f"{base}/user/repos?per_page=100&sort=pushed"
                f"&affiliation=owner,collaborator,organization_member&page={page}"))
        try:
            batch = resilience.instance.call(
                resilience.breaker_key(url, "gh_repos"),
                lambda u=url: _fetch_page(u, tok),
                attempts=GH_ATTEMPTS, sleep_budget_s=GH_SLEEP_BUDGET_S,
            )
        except resilience.BreakerOpen:
            return {"ok": False,
                    "error": f"GitHub API 熔断中（{resilience.RECOVERY_TIMEOUT_S:.0f}s 冷却）"
                             "——上一轮连续失败，先看网络/凭据再刷新"}
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return {"ok": False,
                    "error": f"GitHub API HTTP {e.code}: "
                             + tdai_client.scrub(body, tok)[:200]}
        except Exception as e:  # noqa: BLE001 —— URLError/超时/解析失败
            return {"ok": False, "error": f"GitHub API 不可达: {type(e).__name__}"}
        if not batch:
            break
        repos.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return {"ok": True, "repos": repos}


def _list_repos(force: bool = False) -> Dict[str, Any]:
    """信封：{ok, count, repos, cached, age_s, errors, token, local_total, took_ms}。
    无 token ⇒ ok:True + 空表 + errors 点名（「查不了」≠「没有」）。
    repo 字段刻意不含 clone_url/html_url/ssh_url——克隆 URL 服务端从 slug 现场重构。"""
    t0 = time.monotonic()
    errors: List[str] = []
    tok = _read_token()
    repos: List[Dict[str, Any]] = []
    cached = False
    if _cache.get("repos") is not None and not force:
        repos = _cache["repos"]
        cached = True
    elif not tok:
        errors.append("GitHub token 不可用（env GITHUB_TOKEN 或 "
                      f"{TOKEN_FILE} 读不到）——远端列表降级")
    else:
        d = _fetch_all(tok)
        if not d.get("ok"):
            errors.append(str(d.get("error") or "GitHub API 失败"))
        else:
            repos = d.get("repos") or []
            _cache["repos"] = repos
            _cache["at"] = time.time()
    # 本地匹配：strict remote slug 对账（同仓异名命中；同名异仓不误配）
    slugs: Dict[str, List[str]] = _remote_slugs()
    local_total = 0
    items: List[Dict[str, Any]] = []
    for r in repos:
        fn = (r.get("full_name") or "").lower()
        paths = slugs.get(fn) or []
        if paths:
            local_total += 1
        items.append({
            "full_name": r.get("full_name"),
            "name": r.get("name"),
            "owner": (r.get("owner") or {}).get("login"),
            "fork": bool(r.get("fork")),
            "private": bool(r.get("private")),
            "archived": bool(r.get("archived")),
            "default_branch": r.get("default_branch"),
            "language": r.get("language"),
            "description": (r.get("description") or "")[:120] or None,
            "pushed_at": (r.get("pushed_at") or "")[:10] or None,
            "local": {"found": bool(paths),
                      "path": (paths[0] if paths else None)},
            "local_paths": paths,
        })
    items.sort(key=lambda x: (x.get("pushed_at") or "", str(x.get("name") or "")),
               reverse=True)
    return {
        "ok": True,
        "count": len(items),
        "repos": items,
        "cached": cached,
        "age_s": (round(time.time() - _cache["at"]) if cached else 0),
        "errors": errors,
        "token": bool(tok),
        "local_total": local_total,
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


@router.get("/api/github/repos")
async def github_repos(force: int = 0):
    """GitHub 远端仓库清单（含本地匹配；只读不鉴权不埋点）。
    v0.13.33：内存缓存永久有效（拉一次）；force=1 = 页面「刷新」按钮强拉。"""
    return _list_repos(force=bool(force))


def _gh_head(url: str, tok: str) -> Optional[str]:
    """git ls-remote 取远端 HEAD SHA（1 次 git 调用，免 API）。失败 None。"""
    try:
        out = subprocess.run(
            ["git", "ls-remote", url, "HEAD"],
            capture_output=True, text=True, timeout=20,
            env={**vitals.launch_env(), "GIT_TERMINAL_PROMPT": "0"}).stdout
        return (out.split()[0] if out.split() else None)
    except Exception:  # noqa: BLE001 —— 超时/无 git 均降级为「不可比对」
        return None


def _local_head(path: str) -> Optional[str]:
    """本地仓 HEAD SHA（直接读 .git/HEAD 解 ref，不起 git）。失败 None。"""
    g = Path(path) / ".git"
    cfg = _repo_config(path)
    gitdir = g if g.is_dir() else (Path(_parse_gitdir(g)) if _parse_gitdir(g) else None)
    if gitdir is None:
        return None
    try:
        head = (gitdir / "HEAD").read_text(encoding="utf-8", errors="ignore").strip()
    except OSError:
        return None
    if head.startswith("ref:"):
        ref = head[4:].strip()
        try:
            sha = (gitdir / ref).read_text(encoding="utf-8", errors="ignore").strip()
            return sha or None
        except OSError:
            return None
    return head or None


@router.get("/api/github/sync")
async def github_sync(repo: str, request: Request):
    """单仓核对（v0.13.33「选中进入编辑前才同步」的后端半程）：
    比对本地 HEAD 与远端 HEAD——SHA 一致 = 已同步；远端新 = behind；
    本地无 = 需克隆。只读，不鉴权（与清单同口径），单仓 1 次 git ls-remote，
    绝不整表重拉。"""
    slug = (repo or "").strip()
    if not REPO_RE.fullmatch(slug):
        raise HTTPException(400, f"repo 形状非法: {slug!r}")
    slugs = _remote_slugs()
    paths = slugs.get(slug.lower()) or []
    tok = _read_token()
    remote_head = _gh_head(f"https://{git_host()}/{slug.lower()}.git", tok)
    if not paths:
        return {"ok": True, "repo": slug, "state": "absent",
                "local_head": None, "remote_head": remote_head,
                "note": "本地无此仓库（点新建会话即克隆）"}
    local_head = _local_head(paths[0])
    if remote_head is None:
        return {"ok": True, "repo": slug, "state": "unknown", "local_head": local_head,
                "remote_head": None, "path": paths[0],
                "note": "远端 HEAD 取不到（网络），无法比对"}
    if local_head == remote_head:
        return {"ok": True, "repo": slug, "state": "synced", "local_head": local_head,
                "remote_head": remote_head, "path": paths[0], "note": "本地与远端一致"}
    # SHA 不同但**无法从单值比对判断谁新**（本地可能是未 push 的新提交，如 agent-hub
    # 今天的 28dd764 > 远端 b11c67c）——报 diverged + 中性提示，绝不误指 git pull。
    return {"ok": True, "repo": slug, "state": "diverged", "local_head": local_head,
            "remote_head": remote_head, "path": paths[0],
            "note": "本地与远端 HEAD 不同（进入会话后可 pull / push 对齐）"}


class CloneIn(BaseModel):
    repo: str = Field(min_length=3, max_length=200,
                      description="owner/name（须在服务端远端清单内）")


def _clone_argv(slug: str, dest: str) -> List[str]:
    """argv 列表（无 shell）：浅克隆 + 关凭据助手。URL 从校验过的 slug 与
    **当前配置的 git 主机**现场重构（客户端永不接触 clone URL）。"""
    return ["git", "-c", "credential.helper=", "clone", "--depth", "1",
            "--single-branch", "--no-tags",
            f"https://{git_host()}/{slug}.git", dest]


def _resolve_dest(slug: str) -> Tuple[str, str]:
    """slug → (name, dest)。dest 必须**严格落在 CLONE_BASE 之下**且父链是目录；
    违规一律 ValueError（400）。"""
    base = clone_base()          # v0.13.42：落点可配（DB → env → 模块常量）
    name = slug.split("/")[-1]
    if not name or name in (".", ".."):
        raise ValueError("非法仓库名")
    dest = os.path.normpath(os.path.join(base, name))
    if dest == base or not dest.startswith(base + os.sep):
        raise ValueError("克隆目标越出工作目录")
    if os.path.exists(base) and not os.path.isdir(base):
        raise ValueError("工作目录不是目录")
    return name, dest


def _is_git_dir_path(p: str) -> bool:
    g = Path(p) / ".git"
    return g.is_dir() or g.is_file()


def _origin_slug_of(dest: str) -> Optional[str]:
    """克隆完成后校验：dest/.git/config 里首个 remote 的 slug 必须与请求一致。"""
    cfg = _repo_config(dest)
    if not cfg:
        return None
    try:
        txt = cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for m in re.finditer(r"^\s*url\s*=\s*(\S+)", txt, re.M):
        slug = _remote_slug(m.group(1))
        if slug:
            return slug
    return None


async def _spawn_clone(argv: List[str]) -> Tuple[int, str, str]:
    """起 git 子进程并等它结束。返回 (rc, stdout, stderr)。
    asyncio 而非 subprocess.run：不阻塞事件循环（kb.py turbovec 同型）。"""
    env = dict(vitals.launch_env())
    env["GIT_TERMINAL_PROMPT"] = "0"     # 鉴权失败立即死，绝不挂起等输入
    proc = await asyncio.create_subprocess_exec(
        *argv, env=env,
        stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.PIPE)
    try:
        out, err = await asyncio.wait_for(proc.communicate(), CLONE_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        return 124, "", f"克隆超时（{CLONE_TIMEOUT_S}s），进程已终止"
    return (proc.returncode or 0,
            out.decode("utf-8", "replace") if out else "",
            err.decode("utf-8", "replace") if err else "")


@router.post("/api/github/clone")
async def github_clone(request: Request, body: CloneIn):
    """把白名单内的 GitHub 仓库浅克隆到 CLONE_BASE，返回本地路径。
    「本地没有的远端仓库，点新建会话即时同步」的后端半程。"""
    # ① 写闸（cloudcli POST /start 同款保险带；全局 write_gate 之外再显式判一次）
    verdict, reason = writeauth.decide(
        "POST", request.url.path,
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        log.warning(f"[github] 拒绝 {verdict}：{request.url.path} "
                    f"来源={request.client.host if request.client else '?'} —— {reason}")
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401,
                            detail=reason)

    # ② 形状 + ③ 白名单：客户端只能「点名」，不能「指路」
    slug = (body.repo or "").strip()
    if not REPO_RE.fullmatch(slug):
        raise HTTPException(400, f"repo 形状非法: {slug!r}")
    d = _list_repos()
    known = {str(r.get("full_name") or "").lower() for r in (d.get("repos") or [])}
    if not known:
        raise HTTPException(503, "远端仓库清单不可用（token/网络），稍后再试")
    if slug.lower() not in known:
        raise HTTPException(400, f"仓库 {slug} 不在 GitHub 清单中")

    # ④ 目标路径：containment + 占位防御
    try:
        _name, dest = _resolve_dest(slug.lower())
    except ValueError as e:
        raise HTTPException(400, str(e)) from e

    # ⑤ 冲突态
    if os.path.islink(dest):
        raise HTTPException(400, "目标位置是符号链接，拒绝写入")
    if os.path.exists(dest):
        if not os.path.isdir(dest):
            raise HTTPException(409, f"目标已存在且不是目录: {dest}")
        if _is_git_dir_path(dest):
            have = _origin_slug_of(dest)
            if have == slug.lower():
                return {"ok": True, "path": dest, "repo": slug,
                        "existed": True, "took_ms": 0}
            raise HTTPException(409, f"同名目录已是其它仓库（{have}），拒绝覆盖")
        raise HTTPException(409, f"同名目录已存在且非 git 仓库: {dest}")

    # ⑥ 克隆
    t0 = time.monotonic()
    Path(clone_base()).mkdir(parents=True, exist_ok=True)
    argv = _clone_argv(slug.lower(), dest)
    rc, out, err = await _spawn_clone(argv)
    if rc != 0:
        # 只清理「本次创建且带 .git」的半成品——normpath 对账防误删既有目录
        if os.path.isdir(dest) and (Path(dest) / ".git").exists() and \
                os.path.normpath(dest) == dest:
            shutil.rmtree(dest, ignore_errors=True)
        raise HTTPException(
            502, f"克隆失败（rc={rc}）: "
                 + tdai_client.scrub((err or out)[-400:], _read_token())[:300])

    # ⑦ 克隆后校验（schema 惊讶：如实报错，不删用户可见状态）
    got = _origin_slug_of(dest)
    if got != slug.lower():
        raise HTTPException(502, f"克隆完成但 origin 校验不符（{got}）")

    # ⑧ 审计（action 用冻结枚举里的 create；detail 无 token 无凭据 URL）
    db.log_asset_event("repo", slug.lower(), "create",
                       writeauth.actor_of(request),
                       {"path": dest, "shallow": True,
                        "took_ms": round((time.monotonic() - t0) * 1000, 1)})
    return {"ok": True, "path": dest, "repo": slug, "existed": False,
            "took_ms": round((time.monotonic() - t0) * 1000, 1)}
