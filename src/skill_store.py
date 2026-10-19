"""SkillHub 商店门面（`/api/skill/store/*`）：搜索、安装到指定发现点。

【与 skill.py 的分工】`skill.py` 的 docstring 立了「只读、不写、不缓存副本」的契约，
本模块**故意独立**：它是唯一会往发现点目录写文件的入口。写侧目标只允许
`skill.SKILL_DIRS` 里的路（`tdai` 无目录、`local` 是只读发现面），且每次请求现算，
不接受客户端传来的路径——路径来自请求体就是把 shell 写权限交给调用方。

【为什么不 `curl | bash` / 不 shell=True】安装器只认本机已装好的
`~/.skillhub/skills_store_cli.py`（2026-10-20 由官方 install.sh `--cli-only
--no-skills --no-self-upgrade` 安装，SHA256 见 `work/skillhub/install.sh` 旁的记录）。
子进程一律用参数数组、固定 PATH、显式超时，**slug 先过白名单正则且禁前导 `-`**
（否则 `skillhub install --force` 这类参数会被当成选项，等于让调用方改命令语义）。

【安装 ≠ 调用】`skillhub run` 是把内容发到第三方服务在线执行，本模块**不接**：
「调用」走本仓既有 `/api/skill/invoke`（读本地 SKILL.md 正文，正文不出机器）。
这条边界写在这里，避免以后有人为了「一体化」顺手把 run 接进来。
"""
from __future__ import annotations

import json
import logging
import os
import re
import shutil
import subprocess
import time
from typing import Any, Dict, List, Tuple

from fastapi import APIRouter, Body, HTTPException, Query

import db
import skill

router = APIRouter(prefix="/api/skill/store", tags=["skill"])
log = logging.getLogger("agenthub.skill_store")

#: 官方安装器把 CLI 放在这里（`~/.local/bin/skillhub`）。允许用环境变量覆盖，
#: 但**不**从 PATH 里随便找一个同名程序——那是另一条「同名不同物」的坑。
CLI = os.getenv("SKILLHUB_BIN") or os.path.expanduser("~/.local/bin/skillhub")

SEARCH_TIMEOUT_S = float(os.getenv("SKILLHUB_SEARCH_TIMEOUT", "12"))
INSTALL_TIMEOUT_S = float(os.getenv("SKILLHUB_INSTALL_TIMEOUT", "240"))

#: slug 白名单：字母数字开头，后续允许 . _ / @（社区 slug 形如 `@user/name`）。
#: 显式排除前导 `-`、空白、`..`、shell 元字符。长度上限 120。
SLUG_RE = re.compile(r"^@?[A-Za-z0-9][A-Za-z0-9._/@-]{0,118}$")

DESC_MAX = 500


class StoreError(RuntimeError):
    """商店操作失败。`status` 直接映射 HTTP——502=上游/CLI 故障，400=请求不合法。"""

    def __init__(self, msg: str, status: int = 502):
        super().__init__(msg)
        self.status = status


def _clean(text: Any, limit: int = DESC_MAX) -> str:
    s = re.sub(r"\s+", " ", str(text or "")).strip()
    return s[:limit]


def _cli() -> str:
    if not os.path.isfile(CLI) or not os.access(CLI, os.X_OK):
        raise StoreError(
            "SkillHub CLI 未安装或不可执行：%s（安装命令见 skillhub.cn/install/skillhub.md）"
            % CLI, status=503)
    return CLI


def _run(args: List[str], timeout: float) -> Tuple[int, str, str]:
    env = dict(os.environ)
    # 固定把 CLI 所在目录放最前：`~/.local/bin` 在部分 systemd 会话里不在 PATH。
    env["PATH"] = os.path.dirname(CLI) + os.pathsep + env.get("PATH", "/usr/bin:/bin")
    try:
        p = subprocess.run([_cli()] + args, capture_output=True, text=True,
                           timeout=timeout, env=env)
    except subprocess.TimeoutExpired:
        raise StoreError("SkillHub 命令超时（%.0fs）：%s" % (timeout, " ".join(args[:3])))
    return p.returncode, p.stdout or "", p.stderr or ""


def _json_of(stdout: str) -> Dict[str, Any]:
    """CLI 的 `--json` 输出是单行 JSON；容错前后可能有提示行。"""
    s = stdout.strip()
    i = s.find("{")
    if i < 0:
        raise StoreError("SkillHub 返回的不是 JSON：%s" % _clean(s, 200))
    try:
        raw = json.loads(s[i:])
    except json.JSONDecodeError as e:
        raise StoreError("SkillHub JSON 解析失败：%s" % e)
    if not isinstance(raw, dict):
        raise StoreError("SkillHub JSON 顶层不是对象")
    return raw


def status() -> Dict[str, Any]:
    """面板用：CLI 是否可用、版本。**不可用时给原因**，不返回空对象。"""
    if not os.path.isfile(CLI):
        return {"available": False, "cli": CLI, "version": "", "error": "CLI 不存在"}
    try:
        rc, out, err = _run(["--skip-self-upgrade", "--version"], 8)
    except StoreError as e:
        return {"available": False, "cli": CLI, "version": "", "error": str(e)}
    ok = rc == 0
    return {"available": ok, "cli": CLI, "version": _clean(out or err, 80),
            "error": "" if ok else _clean(err, 200)}


def _targets() -> List[Dict[str, Any]]:
    """可写安装目标：只从 `skill.SKILL_DIRS` 现算。

    `local` 路（项目本地根）**不在** SKILL_DIRS 里 ⇒ 天然排除：那是只读发现面，
    往别人的项目里写技能等于绕过软链铺设流程（见 skill.disk_routes docstring）。
    """
    out: List[Dict[str, Any]] = []
    for route in skill.SKILL_DIRS:
        roots = [r for r in skill.route_roots(route) if os.path.isdir(r)]
        if not roots:
            continue
        root = roots[0]
        out.append({
            "route": route, "dir": root,
            "writable": os.access(root, os.W_OK),
        })
    return out


def search(q: str, limit: int = 12) -> Dict[str, Any]:
    query = _clean(q, 120)
    if not query:
        raise StoreError("搜索词为空", status=400)
    # `--skip-self-upgrade` 是**顶层**参数：放在子命令后面会被 argparse 拒（实测 usage 报错，
    # 而 `head` 管道会把非零 rc 掩盖成 0——所以参数顺序由本函数固定，不交给调用方拼）。
    rc, out, err = _run(["--skip-self-upgrade", "search", query, "--json",
                         "--search-limit", str(max(1, min(30, int(limit))))], SEARCH_TIMEOUT_S)
    if rc != 0:
        raise StoreError("搜索失败（rc=%d）：%s" % (rc, _clean(err or out, 240)))
    raw = _json_of(out)
    items = []
    for r in (raw.get("results") or [])[:50]:
        if not isinstance(r, dict):
            continue
        ns = r.get("namespace") if isinstance(r.get("namespace"), dict) else {}
        items.append({
            "slug": _clean(r.get("slug"), 160),
            "public_slug": _clean(r.get("publicSlug"), 160),
            "name": _clean(r.get("name"), 200),
            "description": _clean(r.get("description")),
            "version": _clean(r.get("version"), 40),
            "source": _clean(r.get("source"), 40),
            "namespace": _clean(ns.get("displayName"), 80),
        })
    return {"query": query, "count": len(items), "items": items}


def install(slug: str, route: str) -> Dict[str, Any]:
    name = str(slug or "").strip()
    if not SLUG_RE.match(name) or ".." in name:
        raise StoreError("slug 不合法：%r（只允许字母数字与 . _ / @ -，且不能以 - 开头）"
                         % name[:80], status=400)
    want = str(route or "").strip()
    allowed = {t["route"]: t for t in _targets()}
    if want not in allowed:
        raise StoreError("目标发现点不可写：%r；可用：%s"
                         % (want, ", ".join(sorted(allowed)) or "无"), status=400)
    tgt = allowed[want]
    if not tgt["writable"]:
        raise StoreError("目标目录不可写：%s" % tgt["dir"], status=409)

    t0 = time.monotonic()
    rc, out, err = _run(["--skip-self-upgrade", "install", name,
                         "--dir", tgt["dir"], "--json"], INSTALL_TIMEOUT_S)
    took = int((time.monotonic() - t0) * 1000)
    parsed: Dict[str, Any] = {}
    if rc == 0:
        try:
            parsed = _json_of(out)
        except StoreError:
            # 装成功但输出不可解析：如实报「装到了但说不清版本」，不回滚用户已得的文件。
            parsed = {}
    ok = rc == 0 and parsed.get("success", True)
    detail = {
        "slug": name, "route": want, "dir": tgt["dir"], "ok": bool(ok),
        "name": _clean(parsed.get("name"), 200),
        "version": _clean(parsed.get("version"), 40),
        "target_dir": _clean(parsed.get("targetDir"), 400),
        "error": "" if ok else _clean(err or out, 300),
    }
    try:
        db.log_profile_event("rest", "skill.store_install", "success" if ok else "fail",
                             took, detail=detail)
    except Exception as e:  # noqa: BLE001 — 审计写失败不许把已完成的安装说成失败
        log.warning("store_install audit failed: %s", e)
    if not ok:
        raise StoreError("安装失败（rc=%d）：%s" % (rc, _clean(err or out, 300)))
    # 扫描缓存必须失效：否则新技能要等 TTL 才出现在目录里，用户会以为没装上。
    try:
        skill._scan_cache.clear()
        skill._STATUS_CACHE.update(ts=0.0, data=None)
    except Exception:  # noqa: BLE001
        pass
    return {
        "ok": True, "slug": name, "name": _clean(parsed.get("name"), 200),
        "version": _clean(parsed.get("version"), 40),
        "route": want, "dir": tgt["dir"],
        "target_dir": _clean(parsed.get("targetDir"), 400),
        "took_ms": took, "scan_invalidated": True,
    }


@router.get("/status")
async def store_status() -> Dict[str, Any]:
    """CLI 可用性与可安装目标。`available=false` 时面板必须显示原因，不许静默。"""
    st = status()
    st["targets"] = _targets()
    st["cli_present"] = bool(shutil.which("skillhub"))
    return st


@router.get("/search")
async def store_search(q: str = Query(min_length=1, max_length=120),
                       limit: int = Query(default=12, ge=1, le=30)) -> Dict[str, Any]:
    """按关键词搜商店。返回里带 `targets`，前端不必再查一次状态。"""
    try:
        res = search(q, limit)
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:300])
    res["targets"] = _targets()
    return res


@router.post("/install")
async def store_install(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """把商店技能装到某个发现点。**只写 SKILL_DIRS 里的目录**（见模块 docstring）。"""
    try:
        return install(str(payload.get("slug") or ""), str(payload.get("route") or ""))
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:300])
