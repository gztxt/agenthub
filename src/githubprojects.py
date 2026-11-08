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
import hashlib
import json
import logging
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
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
#: v0.13.197 多源：`by[sid]` = 该源的原始仓库/star 响应（本地匹配每次现算，
#: 「本地已有」不会因为缓存而定格）；`repos` 保持扁平并集（旧前端/状态页兼容）。
_cache: Dict[str, Any] = {"at": 0.0, "repos": None, "by": {}}


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


def _remote_slug(url: str, host: Optional[str] = None) -> Optional[str]:
    """解析 git remote URL → "owner/name"（小写 host 由正则 re.I 保证）。
    只认**指定/当前配置的 git 主机**（v0.13.42 起可配，默认 github.com；
    v0.13.197 起多源 ⇒ 按源自己的 host 解析）：
    https://evil.com/github.com/x 一类 URL 在任何配置下都解析失败。"""
    u = (url or "").strip()
    m = _host_regex(host or git_host()).match(u)
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


def _remote_slugs(host: Optional[str] = None) -> Dict[str, List[str]]:
    """本机各 git 仓的 remote slug → 本地路径列表（按 host 分账，多源不串）。
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
            slug = _remote_slug(m.group(1), host)
            if slug:
                out.setdefault(slug, []).append(path)
    return out


def _slugs(host: str) -> Dict[str, List[str]]:
    """按 host 取本地匹配表；与全局 host 相同时走零参 `_remote_slugs()`，
    保留 v0.13.31 起的 L0 patch 接缝（单测里唯一替换本地扫描的点）。"""
    if not host or host == git_host():
        return _remote_slugs()
    return _remote_slugs(host)


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


#: 同一次拉取内的请求去重（多源常共享同一 endpoint：两个源都是「我自己」时
#: /user/repos 与 /user/starred 完全同 URL ⇒ 不去重就是白打一倍页数）。
#: 每次 list_repos(force=...) 开始前清空，**不跨请求存活**，force 语义不受影响。
_REQ_MEMO: Dict[str, List[Dict[str, Any]]] = {}
#: 单飞锁：两个源并发打同一 URL 会各发一遍（实测触发 GitHub 连接中断
#: `IncompleteRead`，star 列表一源 199 一源 99）。按 URL 串行，第二个直接吃结果。
_REQ_LOCKS: Dict[str, Any] = {}


def _memo_key(url: str, tok: str) -> str:
    return url + "|" + hashlib.sha256(tok.encode()).hexdigest()[:12]


def _lock_for(mk: str):
    lk = _REQ_LOCKS.get(mk)
    if lk is None:
        lk = _REQ_LOCKS.setdefault(mk, threading.Lock())
    return lk


def _gh_pages(url: str, tok: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """分页拉全量（每页 100，最多 10 页）；返回 (items, error)。

    退避口径（PT-20260927-12 A 档）：**这里是真该退避的地方** ——
    用户在等这一页，且 GitHub API 限额 5000/h、次级限流是本机 recurring failure
    （实测 `vitals.RATE_RE` 就在为 429 兜底）。给 3 次尝试、总退避上限 4 秒：
    `Retry-After` 若说等 60 秒，超过预算就**如实放弃**并把 429 报给用户，
    而不是让页面转圈 60 秒。
    """
    mk = _memo_key(url, tok)
    if mk in _REQ_MEMO:
        return list(_REQ_MEMO[mk]), None
    with _lock_for(mk):
        if mk in _REQ_MEMO:                     # 等锁期间别人已经拉完
            return list(_REQ_MEMO[mk]), None
        return _gh_pages_locked(url, tok, mk)


def _gh_pages_locked(url: str, tok: str,
                     mk: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    items: List[Dict[str, Any]] = []
    for page in range(1, 11):
        u = f"{url}{'&' if '?' in url else '?'}per_page=100&page={page}"
        try:
            batch = resilience.instance.call(
                resilience.breaker_key(u, "gh_repos"),
                lambda uu=u: _fetch_page(uu, tok),
                attempts=GH_ATTEMPTS, sleep_budget_s=GH_SLEEP_BUDGET_S,
            )
        except resilience.BreakerOpen:
            return [], (f"GitHub API 熔断中（{resilience.RECOVERY_TIMEOUT_S:.0f}s 冷却）"
                           "——上一轮连续失败，先看网络/凭据再刷新")
        except urllib.error.HTTPError as e:
            body = ""
            try:
                body = e.read().decode("utf-8", "replace")
            except Exception:  # noqa: BLE001
                pass
            return [], f"HTTP {e.code}: " + tdai_client.scrub(body, tok)[:200]
        except Exception as e:  # noqa: BLE001 —— URLError/超时/解析失败
            return [], f"GitHub API 不可达: {type(e).__name__}"
        if not isinstance(batch, list):
            break
        items.extend(batch)
        if len(batch) < 100:
            break
    _REQ_MEMO[mk] = list(items)
    return items, None


#: token 身份缓存（key = 基址 + token 指纹；不存 token 本身）
_LOGIN_CACHE: Dict[str, str] = {}


def _token_login(base: str, tok: str) -> str:
    """token 所属 login（小写）；取不到返回 ''。用于判断「这个 owner 是不是我自己」：
    只有自己才走 /user/repos、/user/starred —— /users/<name> 只回**公开**仓，
    拿它当「我自己的全部仓库」会静默漏掉私有仓（实测 40 vs 9）。"""
    key = base + "|" + hashlib.sha256(tok.encode()).hexdigest()[:12]
    if key in _LOGIN_CACHE:
        return _LOGIN_CACHE[key]
    try:
        code, me, _h = ghsettings._gh_get(base + "/user", tok)
        login = str(me.get("login") or "").lower() if code == 200 and isinstance(me, dict) else ""
    except Exception:  # noqa: BLE001 —— 身份探针失败只影响「私有仓看不看得到」
        login = ""
    if login:
        _LOGIN_CACHE[key] = login
    return login


def _own_url(base: str, own: str) -> str:
    if own:
        return f"{base}/users/{own}/repos?sort=pushed"
    return (f"{base}/user/repos?sort=pushed"
            "&affiliation=owner,collaborator,organization_member")


def _fetch_own(base: str, own: str, tok: str,
               direct: bool = False) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """一个源的「自建 + fork」仓库（同一响应里带 fork 标记）。
    owner 留空 = 当前 key 可见的全部（原 v0.13.31 行为）。

    `direct=False` 且正好是全局设置那一组时走 `_fetch_all` —— 那是 v0.13.31
    起的既有 L0/探针接缝（单仓拉取 mock 的唯一点），保留以免测试与外部脚本失效。
    """
    if not direct and base.rstrip("/") == api_base().rstrip("/") and own == owner():
        d = _fetch_all(tok)
        return ((d.get("repos") or [], None) if d.get("ok")
                else ([], str(d.get("error") or "拉取失败")))
    if own and _token_login(base, tok) == own.lower():
        # 自己的账号：/users/<name> 只列公开仓 ⇒ 换 /user/repos（含私有）
        return _gh_pages(f"{base}/user/repos?sort=pushed&affiliation=owner", tok)
    items, err = _gh_pages(_own_url(base, own), tok)
    if err and own and err.startswith("HTTP 404"):   # 组织账号：/users 404 才回落 /orgs
        items, err = _gh_pages(f"{base}/orgs/{own}/repos?type=all&sort=pushed", tok)
    return items, err


def _fetch_all(tok: str) -> Dict[str, Any]:
    """全局设置这一组的仓库（兼容接缝，v0.13.31~）；多源内部走 direct 路径。"""
    items, err = _fetch_own(api_base(), owner(), tok, direct=True)
    if err:
        return {"ok": False, "error": err}
    return {"ok": True, "repos": items}


def _fetch_starred(base: str, own: str,
                   tok: str) -> Tuple[List[Dict[str, Any]], Optional[str]]:
    """star 口径（用户 2026-11-09 裁定 A）：该 owner 的**公开** star。
    owner 留空 = 当前 key 自己的 star（含私有）。"""
    if own and _token_login(base, tok) != own.lower():
        return _gh_pages(f"{base}/users/{own}/starred?sort=updated&direction=desc", tok)
    # 自己（或归属留空）⇒ /user/starred，私有仓的 star 也在内
    return _gh_pages(f"{base}/user/starred?sort=updated&direction=desc", tok)


def _repo_item(r: Dict[str, Any], row: Dict[str, Any], starred: bool, bucket: str,
               slug_map: Dict[str, List[str]]) -> Dict[str, Any]:
    """远端 API 原始条目 → 前端条目。刻意不含 clone_url/html_url/ssh_url——
    克隆 URL 由服务端从 slug 现场重构（客户端永不接触）。"""
    fn = (r.get("full_name") or "").lower()
    paths = slug_map.get(fn) or []
    return {
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
        "local": {"found": bool(paths), "path": (paths[0] if paths else None)},
        "local_paths": paths,
        # v0.13.197：源与分桶（own / fork / star）
        "source_id": row.get("id"),
        "source_label": row.get("label"),
        "git_host": row.get("git_host"),
        "bucket": bucket,
        "starred": starred,
    }


def _list_one(row: Dict[str, Any], tok: str, force: bool = False) -> Dict[str, Any]:
    """一个源 → 一个分组（自建 / fork / star 三段，star 与自建去重）。"""
    t0 = time.monotonic()
    sid = str(row.get("id") or "default")
    base = str(row.get("api_base") or api_base()).rstrip("/")
    host = str(row.get("git_host") or ghsettings.derive_git_host(base) or git_host())
    own = str(row.get("owner") or "")
    label = str(row.get("label") or own or host)
    meta = {"id": sid, "label": label, "api_base": base, "git_host": host,
            "owner": own, "errors": [], "cached": False, "age_s": 0}
    if not tok:
        meta.update({"ok": False, "repos": [], "counts": _zero_counts(),
                     "errors": ["GitHub token 不可用（设置 → GitHub 的 Key / env "
                                "GITHUB_TOKEN / github.txt 读不到）"]})
        return meta
    memo = (_cache.get("by") or {}).get(sid)
    if memo and not force:
        raw_own, raw_star = memo["own"], memo["star"]
        meta["errors"] = list(memo.get("errors") or [])
        meta["cached"] = True
        meta["age_s"] = round(time.time() - float(memo.get("at") or 0))
    else:
        # 一个源的两路互不依赖 ⇒ 并行；串行时实测两源首次拉取 25s
        with ThreadPoolExecutor(max_workers=2) as ex:
            f_own = ex.submit(_fetch_own, base, own, tok)
            f_star = ex.submit(_fetch_starred, base, own, tok)
            raw_own, e1 = f_own.result()
            raw_star, e2 = f_star.result()
        if e1:
            meta["errors"].append(f"仓库：{e1}")
        if e2:
            meta["errors"].append(f"star：{e2}")
        if not e1 and not e2:
            _cache.setdefault("by", {})[sid] = {
                "at": time.time(), "own": raw_own, "star": raw_star,
                "errors": meta["errors"]}
    slug_map = _slugs(host)
    star_keys = {(r.get("full_name") or "").lower() for r in raw_star}
    items: List[Dict[str, Any]] = []
    seen = set()
    for r in raw_own:
        fn = (r.get("full_name") or "").lower()
        if not fn or fn in seen:
            continue
        seen.add(fn)
        items.append(_repo_item(r, row, fn in star_keys,
                                "fork" if r.get("fork") else "own", slug_map))
    star_items: List[Dict[str, Any]] = []
    for r in raw_star:
        fn = (r.get("full_name") or "").lower()
        if not fn or fn in seen:          # 自己的仓 / 自己的 fork 不在 star 段重复出现
            continue
        seen.add(fn)
        star_items.append(_repo_item(r, row, True, "star", slug_map))
    key = lambda x: (x.get("pushed_at") or "", str(x.get("name") or ""))
    items.sort(key=key, reverse=True)
    star_items.sort(key=key, reverse=True)
    counts = {"own": sum(1 for x in items if x["bucket"] == "own"),
              "fork": sum(1 for x in items if x["bucket"] == "fork"),
              "star": len(star_items), "starred_total": len(raw_star),
              "total": len(items) + len(star_items)}
    meta.update({"ok": not meta["errors"], "repos": items + star_items,
                 "counts": counts, "took_ms": round((time.monotonic() - t0) * 1000, 1)})
    return meta


def _zero_counts() -> Dict[str, int]:
    return {"own": 0, "fork": 0, "star": 0, "starred_total": 0, "total": 0}


def _source_rows() -> List[Dict[str, Any]]:
    """拉取用源列表；存储层不可用时退回「全局设置合成一条」，绝不返回空。"""
    try:
        rows = ghsettings.sources_enabled()
    except Exception:  # noqa: BLE001
        rows = []
    if rows:
        return rows
    return [{"id": "default", "label": owner() or "默认（当前 key）",
             "api_base": api_base(), "git_host": git_host(), "owner": owner()}]


def _assemble(groups: List[Dict[str, Any]], tok: bool, t0: float) -> Dict[str, Any]:
    flat: List[Dict[str, Any]] = []
    errors: List[str] = []
    local_total = 0
    counts = _zero_counts()
    cached_any = False
    for g in groups:
        if g.get("cached"):
            cached_any = True
        for k in counts:
            counts[k] += int((g.get("counts") or {}).get(k) or 0)
        for it in g.get("repos") or []:
            it["idx"] = len(flat)
            flat.append(it)
            if (it.get("local") or {}).get("found"):
                local_total += 1
        errors.extend(g.get("errors") or [])
    _cache["repos"] = flat
    _cache["at"] = time.time()
    return {
        "ok": True,
        "count": len(flat),
        "repos": flat,
        "groups": groups,
        "counts": counts,
        "cached": cached_any,
        "age_s": (round(time.time() - float(_cache["at"])) if cached_any else 0),
        "errors": errors,
        "token": tok,
        "local_total": local_total,
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


def _list_repos(force: bool = False) -> Dict[str, Any]:
    """信封：{ok, count, repos(扁平), groups(按源), counts, cached, errors, token,
    local_total, took_ms}。无 token ⇒ ok:True + 空表 + errors 点名（「查不了」≠「没有」）。"""
    t0 = time.monotonic()
    tok = _read_token()
    if force:
        _REQ_MEMO.clear()
        _REQ_LOCKS.clear()
    groups = [_list_one(r, tok, force) for r in _source_rows()]
    return _assemble(groups, bool(tok), t0)


async def list_repos_async(force: bool = False) -> Dict[str, Any]:
    """异步入口：多源并行（每个源一次 to_thread），不阻塞事件循环。"""
    t0 = time.monotonic()
    tok = _read_token()
    if force:
        _REQ_MEMO.clear()
        _REQ_LOCKS.clear()
    rows = _source_rows()
    groups = await asyncio.gather(
        *[asyncio.to_thread(_list_one, r, tok, force) for r in rows])
    return _assemble(list(groups), bool(tok), t0)



@router.get("/api/github/repos")
async def github_repos(force: int = 0):
    """GitHub 远端仓库清单（含本地匹配；只读不鉴权不埋点）。
    v0.13.33：内存缓存永久有效（拉一次）；force=1 = 页面「刷新」按钮强拉。
    v0.13.197：按源分组（groups），repos 仍是扁平并集（旧调用方/状态页兼容）。"""
    return await list_repos_async(force=bool(force))


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


def _resolve_source(sid: str = "") -> Optional[Dict[str, Any]]:
    """按 id 取源；未指定取第一条启用源（旧调用方零改动）。"""
    rows = _source_rows()
    if sid:
        return next((r for r in rows if str(r.get("id")) == sid), None)
    return rows[0] if rows else None


@router.get("/api/github/sync")
async def github_sync(repo: str, request: Request, source: str = ""):
    """单仓核对（v0.13.33「选中进入编辑前才同步」的后端半程）：
    比对本地 HEAD 与远端 HEAD——SHA 一致 = 已同步；远端新 = behind；
    本地无 = 需克隆。只读，不鉴权（与清单同口径），单仓 1 次 git ls-remote，
    绝不整表重拉。v0.13.197：source=源 id（决定 git 主机）。"""
    slug = (repo or "").strip()
    if not REPO_RE.fullmatch(slug):
        raise HTTPException(400, f"repo 形状非法: {slug!r}")
    row = _resolve_source(source)
    if not row:
        raise HTTPException(404, f"仓库源不存在: {source!r}")
    host = str(row.get("git_host") or git_host())
    slugs = _slugs(host)
    paths = slugs.get(slug.lower()) or []
    tok = _read_token()
    remote_head = _gh_head(f"https://{host}/{slug.lower()}.git", tok)
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
    source_id: str = Field(default="", max_length=64,
                           description="仓库源 id（决定 git 主机；留空取第一条）")


def _clone_argv(slug: str, dest: str, host: str) -> List[str]:
    """argv 列表（无 shell）：浅克隆 + 关凭据助手。URL 从校验过的 slug 与
    **该源配置的 git 主机**现场重构（客户端永不接触 clone URL）。"""
    return ["git", "-c", "credential.helper=", "clone", "--depth", "1",
            "--single-branch", "--no-tags",
            f"https://{host}/{slug}.git", dest]


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


def _origin_slug_of(dest: str, host: Optional[str] = None) -> Optional[str]:
    """克隆完成后校验：dest/.git/config 里首个 remote 的 slug 必须与请求一致。"""
    cfg = _repo_config(dest)
    if not cfg:
        return None
    try:
        txt = cfg.read_text(encoding="utf-8", errors="ignore")
    except OSError:
        return None
    for m in re.finditer(r"^\s*url\s*=\s*(\S+)", txt, re.M):
        slug = _remote_slug(m.group(1), host)
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
    verdict, reason = writeauth.decide_req(request, "POST")
    if verdict not in ("allow", "exempt"):
        log.warning(f"[github] 拒绝 {verdict}：{request.url.path} "
                    f"来源={request.client.host if request.client else '?'} —— {reason}")
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401,
                            detail=reason)

    # ② 形状 + ③ 白名单：客户端只能「点名」，不能「指路」
    slug = (body.repo or "").strip()
    if not REPO_RE.fullmatch(slug):
        raise HTTPException(400, f"repo 形状非法: {slug!r}")
    row = _resolve_source((body.source_id or "").strip())
    if not row:
        raise HTTPException(404, f"仓库源不存在: {body.source_id!r}")
    host = str(row.get("git_host") or git_host())
    g = _list_one(row, _read_token(), False)
    known = {str(r.get("full_name") or "").lower() for r in (g.get("repos") or [])}
    if not known:
        raise HTTPException(503, f"远端仓库清单不可用（{g.get('errors') or 'token/网络'}），稍后再试")
    if slug.lower() not in known:
        raise HTTPException(400, f"仓库 {slug} 不在源「{row.get('label')}」的清单中")

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
            have = _origin_slug_of(dest, host)
            if have == slug.lower():
                return {"ok": True, "path": dest, "repo": slug, "existed": True,
                        "source_id": row.get("id"), "took_ms": 0}
            raise HTTPException(409, f"同名目录已是其它仓库（{have}），拒绝覆盖")
        raise HTTPException(409, f"同名目录已存在且非 git 仓库: {dest}")

    # ⑥ 克隆
    t0 = time.monotonic()
    Path(clone_base()).mkdir(parents=True, exist_ok=True)
    argv = _clone_argv(slug.lower(), dest, host)
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
    got = _origin_slug_of(dest, host)
    if got != slug.lower():
        raise HTTPException(502, f"克隆完成但 origin 校验不符（{got}）")

    # ⑧ 审计（action 用冻结枚举里的 create；detail 无 token 无凭据 URL）
    db.log_asset_event("repo", slug.lower(), "create",
                       writeauth.actor_of(request),
                       {"path": dest, "shallow": True,
                        "took_ms": round((time.monotonic() - t0) * 1000, 1)})
    return {"ok": True, "path": dest, "repo": slug, "existed": False,
            "source_id": row.get("id"),
            "took_ms": round((time.monotonic() - t0) * 1000, 1)}
