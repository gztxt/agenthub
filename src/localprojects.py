"""本机项目清单（v0.13.30）：多根 git 仓库扫描 + 合并 CloudCLI 项目。

背景：用户要在 hub 里「自动检索和加载显示本机所有项目（不限目录）」，点项目名称、
选 agent、新建会话直达终端。仓库里此前唯一的项目清单是 v0.13.29 的
`/api/cloudcli/projects`（只读 cloudcli 自己的 auth.db）——覆盖不到没在 cloudcli
开过会话的目录（如 agent-router、WorkBuddy）。本模块补上**文件系统侧**的发现：
`GET /api/localprojects` 在若干根目录下找 git 仓库，再与 cloudcli 项目合并去重。

v0.13.31 精度收紧（用户裁定「本机根本没有那么多项目，79 个肯定是错的」）：
实测 79 = 56 git 扫描 + 23 cloudcli-only，其中约 40 条是垃圾。四条新规则：
- P1 /tmp 整前缀剔除（扫描与合并共用）：16 条 /tmp 探针目录是本仓测试历史与
  cloudcli 临时会话的残留，不是项目；
- P2 cloudcli 从独立项目源**降级为纯富化**：无 .git 的 cloudcli 行只是「在某个
  目录开过会话」，不是项目。cloudcli-only 条目必须同时满足：非根路径本身、
  不在排除路径内、盘上目录真实存在（杀掉 sr/网络设备合并/wt-01a0db08 这类
  盘上已删的存账幽灵）、且是 .git 目录（杀掉 work/ 临时验证目录与 .npm-global
  里的 npm 包）；
- P3 git worktree 结构性剔除：.git 是文件且 gitdir: 指向另一已收录仓库的 .git/
  内 ⇒ 是该仓库的派生检出（agent-hub-wt-* 与主仓重复成对出现）。**不用目录名
  猜**（用户裁定「严格按证据，不按名字猜」）；gitdir 指向别处的独立 worktree
  保留并标 worktree:True；
- P4 备份/归档目录剪枝：snapshots、git-backups、Hermes-backup、marketplace-cache
  （哈希名缓存仓）、ARCHIVED- 前缀目录（整仓拷贝与归档不是项目）。
预期本机 79 → ≈44，dropped_count/数组在信封里如实报告每条剔除原因——
下一类误报用户自己能看见，无代码也能反馈。

设计：
- 纯 os.walk 手动剪枝（不 spawn git 子进程——term.py 的"全仓唯一拉起路径"纪律
  同样适用于这里，且 walk 比 git ls-projects 免 per-repo fork）；
- 跳过点开头目录（.hermes/.claude/.nvm 等工具内部仓不是"项目"）与
  node_modules/venv 一类依赖目录；深度上限 3（实测三根全量 find maxdepth 3 仅
  93ms，剪枝后同量级）；
- 三个根默认 /home/gztxt、/fs/1000/ftp/技术文档、/vol1/@apphome，
  env LOCALPROJECT_ROOTS 可覆盖（逗号分隔）；
- 根目录不存在/不可读 ⇒ 记入 errors（「查不了」不能糊成「没有」的四态口径，
  与 cloudcli._list_projects 同型）；cloudcli 库读不到时该项目源整体降级，
  git 部分照常。

安全模型：
- GET 只读目录名/路径/时间戳，不读文件内容；不鉴权（与 /api/agents、
  /api/cloudcli/projects 同口径——列表是控制平面入口，写动作才要 token）；
- 不埋 runlog（同 /api/agents 防噪声口径）。
"""
from __future__ import annotations

import datetime
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter

import cloudcli

router = APIRouter(tags=["localprojects"])

#: 扫描根（逗号分隔，env 可覆盖）。默认三根覆盖本机绝大多数项目落点。
ROOTS: List[str] = [r.strip() for r in os.getenv(
    "LOCALPROJECT_ROOTS", "/home/gztxt,/fs/1000/ftp/技术文档,/vol1/@apphome").split(",") if r.strip()]

#: 深度上限：根本身算第 0 层，3 层足够覆盖 <root>/<分组>/<项目> 形态且不扫穿 node_modules 山脉
MAX_DEPTH = 3

#: 依赖/构建产物目录名：下钻即浪费（里面不会有用户项目）
EXCLUDE_DIR_NAMES = {"node_modules", "venv", ".venv", "__pycache__", "target", "dist", ".cache",
                     # v0.13.31 精度收紧 P4：备份/归档/缓存——整仓拷贝与缓存不是项目
                     "snapshots", "git-backups", "session-backups", "Hermes-backup",
                     "marketplace-cache",
}

#: v0.13.31 P4：目录名前缀闸（ARCHIVED-勿用-… 一类归档目录）
#:
#: v0.13.58：夹具前缀**不进这里**。曾试过排 "ghsync-"/"ghlist-" 等，当场 12 例红 ——
#: tests/test_github_projects.py 的 setUp 就是 _mktmp("ghlist-") 然后把 lp.ROOTS 指到它，
#: 指望那些仓被扫描到。按目录名排会把「测试自己造的仓」也排掉，治好了污染、测瞎了扫描。
#: 正确的粒度是**路径**：见 EXCLUDE_PATH_PREFIXES —— 夹具根是一个绝对路径，不是名字模式。
EXCLUDE_NAME_PREFIXES = ("ARCHIVED",)

#: v0.13.178：**后缀式备份目录**（`cloudcli.bak-20261005_141214-升1.37.3前`）。
#:
#: 判据为什么不能并进 EXCLUDE_DIR_NAMES：那是个精确集合（`git-backups`/`snapshots`），
#: 而后缀式备份的名字随时间和说明变化，集合永远跟不上 —— 实测 L1 精度闸 51 > 50，
#: 多出来的第一名就是「升级前的整仓拷贝」。
#: 匹配「bak 作为独立词元」：`foo.bak`、`foo.bak-2026...`、`foo_bak` 命中；
#: `bakery`/`backup-notes` 不命中（避免把真项目名误杀）。
BACKUP_SEG_RE = re.compile(r"(?i)(^|[-_.])bak([-_.]|$)")

#: v0.13.31 P1：绝对路径前缀闸（/tmp 探针与临时会话目录；扫描与 cloudcli 合并共用）
#:
#: v0.13.58：加 L0 夹具根（HUB_L0_TMP 默认 ~/hub-l0test-fixtures）。实测代价是本机
#: 真实项目被算成 176（应为 ≈44）—— 2400 多个带 .git 的夹具仓混进列表，于是
#: L1 精度闸（≤50）与 prepush 闸门一起红。**按路径排才对**：测试把 lp.ROOTS 显式
#: 指到自己的夹具目录时，本地仓扫描仍然生效（那正是扫描用例要测的）。
EXCLUDE_PATH_PREFIXES = ("/tmp",)

#: v0.13.58：L0 夹具根（tests/*.py 的 HUB_L0_TMP 默认值）——**只在扫描侧挡**。
#: 为什么不并进 EXCLUDE_PATH_PREFIXES：那个集合被扫描与 cloudcli 合并共用，而合并侧
#: 收的是「cloudcli 亲口说有、盘上确实是 git 仓」的条目（test_cc_only_real_repo_outside_roots_kept
#: 钉死这条不许杀）。夹具污染是**扫描**的问题（把 2400 个夹具仓当项目），
#: 不是 cloudcli 记录的问题。合成一处会让那条断言当场红 —— 治污染治成了砍真仓。
L0_FIXTURE_ROOT = str(Path.home() / "hub-l0test-fixtures")

#: 返回条目上限（防异常目录树拖垮响应；实测 git 仓约 48 + cloudcli 29 ≈ 60）
LIMIT = 256


def _is_git_dir(d: Path) -> bool:
    """普通仓库 .git 是目录；git worktree 的 .git 是一页文本文件——都算。"""
    g = d / ".git"
    return g.is_dir() or g.is_file()


def _under_excluded(path: str, under: Optional[List[str]] = None,
                   extra_prefixes: Tuple[str, ...] = ()) -> bool:
    """v0.13.31 精度闸：路径前缀（/tmp、夹具根）/任一段命中备份目录名/段名 ARCHIVED- 前缀。
    扫描收录与 cloudcli 合并共用——排除口径必须一处定义，两处各写必漂移。

    ``under`` 是**点名要扫的根**（ROOTS）。排除闸治的是「默认扫到的东西不该混进来」，
    不是「用户点名要的目录」：所以显式指定的根及其全部后代一律豁免，豁免逻辑放在**这里**
    而不是各调用点各打一个补丁 —— 实测正是两处口径不一致造成的（扫描侧只豁免根自身、
    合并侧完全不豁免，于是夹具根下的仓「扫描收了、合并又排掉」，TestMerge 10 例红）。
    """
    p = os.path.normpath(path)
    roots = [os.path.normpath(r) for r in (under or [])]

    def _inside(x: str, pref: str) -> bool:
        return x == pref or x.startswith(pref + os.sep)

    # v0.13.131：**硬排除先于点名根豁免**。反例（PT-20261019-04）：扫描根含
    # ``/home/gztxt`` 时，旧写法里 ``under`` 的 HOME 先把 ``~/hub-l0test-fixtures/**``
    # 整支豁免掉 ⇒ 168 个 L0 夹具仓被当真实项目（212 > 阈值 50），L1 精度闸恒红。
    # 但豁免不能一刀切：测试会把 ROOTS 直接指到自己的夹具目录来验扫描逻辑
    # （test_github_projects 的 setUp 等 12 例依赖）——那种情况点名根**落在**排除前缀
    # 之内，是要看的对象，必须继续豁免。判据：p 在硬排除前缀内，且**没有任何点名根**
    # 也落在同一前缀内 ⇒ 排除；否则交给下面的根豁免。
    for pref in tuple(EXCLUDE_PATH_PREFIXES) + tuple(extra_prefixes):
        pref = os.path.normpath(pref)
        if not _inside(p, pref):
            continue
        if any(_inside(r, pref) for r in roots):
            continue
        return True

    # v0.13.178：派生备份目录不是项目。与 EXCLUDE_DIR_NAMES 同语义，只是判据从
    # 「段名 ∈ 精确集合」扩成「段名形如后缀式备份」。**必须排在点名根豁免之前**：
    # 扫描是在 `<根>/<备份目录>` 上调用本函数的，根豁免会把这支整片放过
    # （v0.13.131 的 L0 夹具污染就是同一个次序错误）。
    segs = p.split(os.sep)
    if any(BACKUP_SEG_RE.search(seg) for seg in segs):
        if not any(_inside(r, p) for r in roots):
            return True

    for r in roots:
        if _inside(p, r):
            rel = os.path.relpath(p, r)
            parts = [] if rel == "." else rel.split(os.sep)
            # v0.13.178：`<根>/work/…` 是施工中间产物区（AGENTS.md §2：产物进
            # `<项目>/work/`，不落 /tmp），里面的克隆与发布副本不是独立项目。
            # 只认「根的直接子目录 work」，不按任意段名排 —— 否则 `~/code/work/foo`
            # 这类真项目会被误杀，也保住了「点名根指到 work 内部仍可扫」的语义。
            if len(parts) >= 2 and parts[0] == "work":
                return True
            return False
    for seg in p.split(os.sep):
        if seg in EXCLUDE_DIR_NAMES:
            return True
        if any(seg.startswith(n) for n in EXCLUDE_NAME_PREFIXES):
            return True
    return False


def _parse_gitdir_file(d: Path) -> Optional[str]:
    """`.git` 是文件时读 `gitdir: <路径>` 首行；相对值按 d 解析。返回 normpath 目标，
    非文件/形状不对/读不到一律 None——解析失败不能挡收录（宁可多列不能误杀真仓）。"""
    g = d / ".git"
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
        t = d / t
    return os.path.normpath(str(t))


def _is_derived_worktree(gitdir_target: Optional[str], git_root_paths) -> bool:
    """P3：gitdir 指向另一已收录仓库的 <P>/.git/ 内 ⇒ 派生检出（重复条目）。

    另含**死 gitdir**（v0.13.93 补）：gitdir 指向的路径整条已不存在 ⇒ 该 `.git`
    文件是仓库搬走/改名后没跟上的残留（实测 2026-10-08 目录改名 `agent-hub` →
    `agenthub` 后，家目录 14 个 `agent-hub-wt-*` 全部命中，git 进去直接
    `fatal: not a git repository`，却因「不在已收录仓库的 .git/ 内」而被放行，
    混进项目列表把 L1 精度闸顶到 60>50）。它连仓库都不是，留着只会误导用户，
    按证据（路径不存在）剔除，不按目录名猜。
    """
    if not gitdir_target:
        return False
    if not os.path.exists(gitdir_target):
        return True
    for p in git_root_paths:
        if gitdir_target == os.path.normpath(os.path.join(p, ".git")) or \
                gitdir_target.startswith(os.path.normpath(os.path.join(p, ".git")) + os.sep):
            return True
    return False


def _scan_roots(roots: List[str]) -> Tuple[List[Dict[str, Any]], List[str], Dict[str, int]]:
    """在根列表下找 git 仓库。返回 (projects, errors, stats)——stats 记剔除量
    （v0.13.31 精度收紧：excluded/worktrees 两类），信封透出给用户看。"""
    projects: List[Dict[str, Any]] = []
    errors: List[str] = []
    stats = {"excluded": 0, "worktrees": 0}
    for raw in roots:
        root = Path(raw).expanduser()
        if not root.is_dir():
            errors.append(f"根目录不存在: {root}")
            continue
        try:
            it = os.walk(str(root), followlinks=False)
            for dirpath, dirnames, filenames in it:
                cur = Path(dirpath)
                # P4：收录前过排除闸（含 ARCHIVED- 目录本身——深度剪枝够不到它的孩子）
                # 根豁免要**递归**：指定根之下的一切都算「点名要扫的」。
                # 旧写法只豁免根自身那一次，于是指定根落在排除前缀里时，它的**孩子**
                # 仍被剪掉 ⇒ 「根被扫了却一个仓都没收到」（实测 TestMerge：夹具根下的
                # gitonly 不见了，merged 是空数组）。与 _merge_cloudcli 的豁免同口径。
                if _under_excluded(str(cur), [str(root)],
                                   extra_prefixes=(L0_FIXTURE_ROOT,)):
                    stats["excluded"] += 1
                    dirnames[:] = []
                    continue
                if _is_git_dir(cur):
                    rel = cur.relative_to(root)
                    if len(rel.parts) > 0:          # 根自身是仓库时由去重层决定归属
                        g = cur / ".git"
                        try:
                            ts = os.path.getmtime(g)
                        except OSError:
                            ts = 0.0
                        projects.append({
                            "name": cur.name,
                            "path": str(cur),
                            "git": True,
                            "last_ts": ts,
                            "worktree": g.is_file(),   # P3：.git 文件 = worktree 形态
                        })
                # 剪枝：点目录（.git 内部、工具隐藏仓）、依赖目录、超深
                depth = len(cur.relative_to(root).parts)
                dirnames[:] = [
                    d for d in dirnames
                    if not d.startswith(".")
                    and d not in EXCLUDE_DIR_NAMES
                    and not any(d.startswith(n) for n in EXCLUDE_NAME_PREFIXES)
                ] if depth < MAX_DEPTH else []
        except OSError as e:
            errors.append(f"根目录不可读: {root}（{type(e).__name__}）")
    # P3 二次解析：git-dir 仓（agent-hub-wt-*，主仓可能晚于 worktree 被走到）
    # 先收集全量 .git 目录形态的根，再对 .git 文件条目判派生。
    git_root_paths = [p["path"] for p in projects if not p.get("worktree")]
    for p in list(projects):
        if p.get("worktree"):
            target = _parse_gitdir_file(Path(p["path"]))
            if _is_derived_worktree(target, git_root_paths):
                projects.remove(p)
                stats["worktrees"] += 1
    return projects, errors, stats


def _merge_cloudcli(git_projects: List[Dict[str, Any]],
                    errors: List[str]) -> Tuple[List[Dict[str, Any]], List[str]]:
    """并入 cloudcli 项目（精确名/会话数/最近活动），按 normpath 去重。
    cloudcli 库读不到（ok:false）⇒ 整源降级，errors 点名，git 部分照常。

    v0.13.31 P2 裁定：cloudcli 从独立项目源**降级为纯富化**——cloudcli-only 行
    只是「在某个目录开过会话」，不是项目。四条全过才收：非根路径本身 / 不在排除
    路径内 / 盘上目录真实存在（杀存账幽灵）/ 是 .git 目录且非派生 worktree。
    每条剔除记 dropped（路径+规则名），信封透出。返回 (merged, dropped)。"""
    cc = cloudcli._list_projects()
    if not cc.get("ok"):
        errors.append("CloudCLI 项目源不可用：" + str(cc.get("error") or "?"))
        cc_projects: List[Dict[str, Any]] = []
    else:
        cc_projects = cc.get("projects") or []

    root_set = {os.path.normpath(r) for r in ROOTS}
    by_path: Dict[str, Dict[str, Any]] = {}
    for p in git_projects:
        key = os.path.normpath(p["path"])
        by_path[key] = dict(p, git=True, cloudcli=False, sessions=0,
                            last_activity=None)

    dropped: List[str] = []

    def _drop(key: str, why: str) -> None:
        dropped.append(f"{why}: {key[:110]}")

    for p in cc_projects:
        key = os.path.normpath(p.get("path") or "")
        if not key:
            continue
        la = p.get("last_activity")
        la_ts = 0.0
        if la:
            try:
                la_ts = datetime.datetime.fromisoformat(la).timestamp()
            except ValueError:
                la_ts = 0.0
        cur = by_path.get(key)
        if cur:                       # 双源命中：cloudcli 的精确名与活跃度胜出
            cur["name"] = p.get("name") or cur["name"]
            cur["cloudcli"] = True
            cur["sessions"] = int(p.get("sessions") or 0)
            cur["last_activity"] = la
            cur["last_ts"] = max(cur.get("last_ts") or 0.0, la_ts)
            continue
        # P2 四闸（cloudcli-only 才走这里；git 行上面已富化完）
        if key in root_set:
            _drop(key, "根目录自身")
            continue
        # 根豁免：与 _scan_roots（:161）同一口径 —— 排除闸只管「默认扫到的东西」，
        # 不管「点名要扫的根」。测试把 lp.ROOTS 指到 ~/hub-l0test-fixtures/xxx/root 后，
        # 其下的仓在扫描侧被收（:161 对 root 本身有豁免），若合并侧不豁免就会凭空消失
        # ⇒ 同一个路径「扫描收了、合并排了」，实测 TestMerge 12 例红。
        # 豁免粒度是**指定根的后代**，不是根自身：root_set 存的是根，key 是项目目录。
        if _under_excluded(key, list(root_set)):
            _drop(key, "排除路径")
            continue
        d = Path(key)
        if not d.is_dir():
            _drop(key, "盘上已不存在")
            continue
        if not _is_git_dir(d):
            _drop(key, "非 git 目录")
            continue
        if d.joinpath(".git").is_file() and \
                _is_derived_worktree(_parse_gitdir_file(d), list(by_path)):
            # v0.13.36 修正：对照集合必须是**已收录 git 仓**（by_path），不能用
            # ROOTS——CloudCLI 在 worktree 里开过会话后，该路径会以 cc-only 形态
            # 走到这里，而它的 gitdir 指向 /home/gztxt/agent-hub/.git/…，只对
            # ROOTS 比对永远不命中 ⇒ 派生 worktree 漏回主列表（09-26 L1 红定案）。
            _drop(key, "派生 worktree")
            continue
        by_path[key] = {
            "name": p.get("name") or os.path.basename(key),
            "path": p.get("path") or key,
            "git": True,
            "cloudcli": True,
            "sessions": int(p.get("sessions") or 0),
            "last_activity": la,
            "last_ts": la_ts,
            "worktree": False,
        }
    return list(by_path.values()), dropped


def _list_projects() -> Dict[str, Any]:
    """git 扫描 + cloudcli 合并去重，按最近活动倒序。"""
    t0 = time.monotonic()
    errors: List[str] = []
    git_projects, errors, stats = _scan_roots(ROOTS)
    merged, dropped = _merge_cloudcli(git_projects, errors)
    merged.sort(key=lambda p: (-(p.get("last_ts") or 0.0), str(p.get("name") or "")))
    out = merged[:LIMIT]
    return {
        "ok": True,
        "count": len(out),
        "projects": [{k: p.get(k) for k in
                      ("name", "path", "git", "cloudcli", "sessions", "last_activity",
                       "worktree")}
                     for p in out],
        "roots": list(ROOTS),
        "errors": errors,
        "dropped_count": len(dropped) + stats["excluded"] + stats["worktrees"],
        "dropped": (dropped + [f"扫描剔除: {stats['excluded']} 条（排除路径/备份归档）",
                               f"worktree 剔除: {stats['worktrees']} 条（派生检出）"])[:64],
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


@router.get("/api/localprojects")
async def local_projects():
    """本机项目清单（git 扫描 + cloudcli 合并；直读，不鉴权不埋点）。
    单资源端点：路径就是 /api/localprojects，前端 api('/api/localprojects') 一步到位。"""
    return _list_projects()
