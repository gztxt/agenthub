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

import asyncio
import json
import logging
import os
import re
import shutil
import subprocess
import time
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, Body, HTTPException, Query

import db
import runlog
import skill
import skill_relevance
import tdai_client

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
        "name": _clean(parsed.get("name") or name, 200),
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
        "ok": True, "slug": name, "name": _clean(parsed.get("name") or name, 200),
        "version": _clean(parsed.get("version"), 40),
        "route": want, "dir": tgt["dir"],
        "target_dir": _clean(parsed.get("targetDir"), 400),
        "took_ms": took, "scan_invalidated": True,
    }


# ── 自动兜底（v0.13.135）：本地没有 → 搜商店 → 装 → 把指令正文交回调用方 ────────
#
# 用户裁定（2026-10-20）：商店不是「人工逛的货架」，而是 agent 在技能中心找不到
# 对应技能时的**自动补货通道**。所以入口收的是「一段任务描述」，不是「一个 slug」——
# slug 是商店侧概念，调用方（agent）手里只有任务。
#
# 三道闸，缺一不可：
#   ① **本地优先**：BM25 命中够强就不联网、不装东西，直接回本地正文。
#   ② **商店候选也要够像**：搜索接口对任何词都会回一堆结果，不设相似度门槛等于
#      「随便装一个」；门槛用查询词元覆盖率，与本地同一套分词（skill_relevance）。
#   ③ **装机预算**：自动装是无人在场的写盘，按小时限次（默认 6），超了 429 并说清。
#      计数读 `profile_events` 的 skill.store_auto 成功行——不另建状态表，账与审计同源。
#
# 安全边界（写在这里，避免以后有人「顺手打通」）：
#   · 只装到 `_targets()` 里的可写发现点，自动模式只认 `AUTO_TARGET_ROUTE`（默认
#     techdocs），**不接受请求体传路径**；
#   · 商店技能是第三方文本，返回带 `trust=community` 与 `warning`；hub 只把它当
#     说明书交回，不执行、不代跑脚本；
#   · 不覆盖同名：目标目录已存在就跳过安装（`already_present`），避免把本机改过的
#     技能静默换掉。

AUTO_ENABLED = os.getenv("SKILL_STORE_AUTO", "1").strip().lower() not in ("0", "false", "no", "off")
AUTO_TARGET_ROUTE = (os.getenv("SKILL_STORE_AUTO_ROUTE") or "techdocs").strip()
AUTO_MAX_PER_HOUR = max(1, int(os.getenv("SKILL_STORE_AUTO_MAX", "6") or 6))

#: 本地接受门槛：**归一化 BM25**（顶层分数 ÷ 查询词 IDF 之和）。
#: 绝对分随语料规模与查询长度漂移（同一强命中在 375 条语料上是 17.9，在 2 条语料上
#: 不到 1），拿绝对阈值等于把「语料今天多大」写进判据。2026-10-20 在 375 条真实语料
#: 上标定 8 条查询：真命中 0.41~1.51，弱命中/无关 0.10~0.27 ⇒ 取 0.40（两侧各留 0.13）。
#: 语料量级明显变化时须重标（`work/store-cal/` 有标定脚本）。
LOCAL_NORM_MIN = 0.40

#: 商店接受门槛（两条任一成立）：
#:   ① ASCII 关键词在**候选名称**里的占比 ≥0.5；
#:   ② 原始查询词在候选名称+描述里的覆盖率 ≥0.5。
#: 标定反例（2026-10-20）：「把 PDF 拆成图片」的 12 条结果里，只按覆盖率会把
#: 「视频…全能创作」（只命中「图片」一个词，0.2）判成候选；改成「先用 ASCII 关键词
#: 重搜 + 名称命中」后，选中的是名称就叫 `pdf` 的那条（1.0）。
STORE_QUERY_COVERAGE_MIN = 0.5
STORE_KEYWORD_NAME_MIN = 0.5

#: 抽取商店搜索关键词时丢掉的英文虚词（技能名/描述里出现它们不构成匹配证据）。
STORE_TRUST_NOTE = ("第三方社区技能：正文按说明书使用，hub 不执行其脚本；"
                    "首次使用前建议人工过一遍 SKILL.md")

#: 抽取商店搜索关键词时丢掉的英文虚词（技能名/描述里出现它们不构成匹配证据）。
_ASCII_STOP = {"a", "an", "the", "and", "or", "for", "to", "of", "in", "on",
               "with", "my", "me", "is", "it", "this", "that", "how", "what"}


def _coverage(q_tokens: List[str], text: str) -> float:
    """查询词元被 `text` 覆盖的比例（0~1）。空查询/空文本一律 0，不返回 NaN。"""
    qt = {t for t in (q_tokens or []) if t}
    if not qt:
        return 0.0
    return len(qt & set(skill_relevance.tokenize(text))) / len(qt)


async def _local_rank(query: str, n: int = 3) -> Tuple[Dict[str, Any], int]:
    """本地全部发现点扫描 + BM25 排序 + 归一化分数（语料无关的接受判据）。

    归一化分母取**查询词 IDF 之和**（绝对值之和，避免 2 条语料的测试盘里 IDF 为负
    把分母抵成 0）；未知词按语料最大 IDF 计——查询里出现语料从未见过的词，
    不该因为「分母算不到它」而显得更相关。
    """
    routes = skill.disk_routes()
    scans = await asyncio.gather(*[skill._scan_async(r, skill.route_roots(r)) for r in routes])
    items: List[Dict[str, Any]] = []
    for s in scans:
        items.extend(s.get("items") or [])
    unique, _aliases = skill._dedup(items)
    res = skill_relevance.rank(query, unique, n=n)
    try:
        _docs, idf, _avg = skill_relevance.build_index(unique)
        dmax = max(idf.values()) if idf else 1.0
        qt = {t for t in (res.get("query_tokens") or []) if t}
        denom = sum(abs(idf.get(t, dmax)) for t in qt) or 1.0
        top_score = float(((res.get("items") or [{}])[0] or {}).get("score") or 0.0)
        res["norm"] = round(top_score / denom, 3)
    except Exception as e:  # noqa: BLE001 — 归一化算不出来按 0 处理（宁可不认）
        log.warning("local norm failed: %s", e)
        res["norm"] = 0.0
    return res, len(unique)


def _local_verdict(res: Dict[str, Any]) -> Tuple[Optional[Dict[str, Any]], float, bool]:
    """`(top, coverage, accepted)`。没有命中时 top=None、accepted=False，**不编造**。

    接受判据只有一条归一化分数；`coverage` 仍回传给调用方做解释，
    但它**不单独决定**接受（纯中文长查询的覆盖率天然被虚词拉低）。
    """
    items = res.get("items") or []
    if not items:
        return None, 0.0, False
    top = items[0]
    qt = {t for t in (res.get("query_tokens") or []) if t}
    matched = {t for t in (top.get("matched") or []) if t}
    cov = (len(matched & qt) / len(qt)) if qt else 0.0
    accepted = float(res.get("norm") or 0.0) >= LOCAL_NORM_MIN
    return top, round(cov, 3), accepted


def _cand(x: Dict[str, Any]) -> Dict[str, Any]:
    it = x.get("item") or {}
    return {"name": it.get("name"), "route": it.get("route"),
            "score": x.get("score"), "matched": x.get("matched"),
            "matched_in": x.get("matched_in")}


def _store_keywords(query: str) -> List[str]:
    """从查询里抽 ASCII 关键词（技能名以英文为主的现实决定的）。

    中文长句直接丢给商店搜索会被切成二字组，上游排序基本靠「哪个描述里凑巧有
    『生成』『图片』」——2026-10-20 实测「把 PDF 拆成图片」返回的第一条是视频创作技能。
    抽 `pdf` 这类英文词重搜一次，才拿得到名称就叫 `pdf` 的条目。
    """
    words = [w.lower() for w in re.findall(r"[A-Za-z][A-Za-z0-9_.+-]{1,}", str(query or ""))]
    out: List[str] = []
    for w in words:
        w = w.strip("._+-")
        if len(w) < 2 or w in _ASCII_STOP or w in out:
            continue
        out.append(w)
    return out


def _search_candidates(query: str) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """两路搜索并合并：`关键词`（有 ASCII 词时）+ `原句`。按 slug 去重保序。

    返回 `(items, queries)`；`queries` 里逐条记实际发出的词与命中数，
    失败也留痕（调用方能看到「哪一路没返回」而不是只看到一个总数）。
    """
    kws = _store_keywords(query)
    plan: List[str] = []
    if kws:
        plan.append(" ".join(kws[:3]))
    if query not in plan:
        plan.append(query)
    seen = set()
    items: List[Dict[str, Any]] = []
    trace: List[Dict[str, Any]] = []
    for sq in plan:
        try:
            r = search(sq, 12)
        except StoreError as e:
            if not items and sq == plan[-1]:
                raise
            trace.append({"query": sq, "error": str(e)})
            continue
        trace.append({"query": sq, "count": r["count"]})
        for it in r["items"]:
            slug = str(it.get("slug") or "")
            if slug and slug not in seen:
                seen.add(slug)
                items.append(it)
    return items, trace


def _pick_store_item(query: str, items: List[Dict[str, Any]]
                     ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """挑最像的一条，返回 `(item, metrics)`；`metrics["accepted"]` 是门槛结论。

    排序键 `(关键词名称覆盖率, 查询覆盖率, 关键词描述覆盖率)`：名称命中永远比
    「描述里凑巧出现同一个词」可信（与 `skill_relevance.W_NAME` 同一判断）。
    """
    kws = set(_store_keywords(query))
    qt = {t for t in skill_relevance.tokenize(query) if t}
    best: Optional[Tuple[Tuple[float, float, float], Dict[str, Any], Dict[str, Any]]] = None
    for it in items or []:
        name = str(it.get("name") or "")
        desc = str(it.get("description") or "")
        nt = set(skill_relevance.tokenize(name))
        dt = set(skill_relevance.tokenize(desc))
        kw_name = (len(kws & nt) / len(kws)) if kws else 0.0
        kw_desc = (len(kws & dt) / len(kws)) if kws else 0.0
        qcov = (len(qt & (nt | dt)) / len(qt)) if qt else 0.0
        key = (kw_name, qcov, kw_desc)
        if best is None or key > best[0]:
            best = (key, it, {"kw_name": round(kw_name, 3), "kw_desc": round(kw_desc, 3),
                              "qcov": round(qcov, 3), "keywords": sorted(kws)})
    if best is None:
        return None, {"kw_name": 0.0, "kw_desc": 0.0, "qcov": 0.0,
                      "keywords": sorted(kws), "accepted": False}
    _key, it, metrics = best
    metrics["accepted"] = bool(
        (kws and (metrics["kw_name"] >= STORE_KEYWORD_NAME_MIN
                  or metrics["kw_desc"] >= STORE_QUERY_COVERAGE_MIN))
        or metrics["qcov"] >= STORE_QUERY_COVERAGE_MIN)
    return it, metrics


def _auto_used_last_hour() -> int:
    """近一小时成功自动安装次数。读账（profile_events），不维护第二份状态。"""
    since = (datetime.now(timezone.utc) - timedelta(hours=1)).isoformat()
    try:
        rows = db.query(
            "SELECT COUNT(*) AS n FROM profile_events"
            " WHERE source=? AND subject='skill.store_auto' AND status='success'"
            " AND created_at >= ?", (runlog.SOURCE, since))
        return int((rows[0] or {}).get("n") or 0) if rows else 0
    except Exception as e:  # noqa: BLE001 — 计数坏了按「已用满」处理，宁可不装
        log.warning("auto budget count failed: %s", e)
        return AUTO_MAX_PER_HOUR


def auto_policy() -> Dict[str, Any]:
    """面板用：自动兜底的开关、目标、额度。**每一项都是现算**，不写死在前端。"""
    used = _auto_used_last_hour()
    tgt = None
    for t in _targets():
        if t["route"] == AUTO_TARGET_ROUTE:
            tgt = t
    return {
        "enabled": AUTO_ENABLED,
        "route": AUTO_TARGET_ROUTE,
        "route_writable": bool(tgt and tgt["writable"]),
        "dir": (tgt or {}).get("dir", ""),
        "max_per_hour": AUTO_MAX_PER_HOUR,
        "used_last_hour": used,
        "remaining": max(0, AUTO_MAX_PER_HOUR - used),
        "local_norm_min": LOCAL_NORM_MIN,
        "store_query_coverage_min": STORE_QUERY_COVERAGE_MIN,
        "store_keyword_name_min": STORE_KEYWORD_NAME_MIN,
        "note": ("agent 在技能中心找不到技能时，自动搜索商店并按门槛安装到该发现点；"
                 "装完仍走本仓 /api/skill/invoke 读正文（不接在线 run）"),
    }


async def _find_installed(route: str, slug_dir: str) -> Optional[Dict[str, Any]]:
    """在目标发现点里找刚装下的那条技能（按目录前缀认，不靠名字猜）。"""
    want = os.path.realpath(slug_dir)
    scan = await skill._scan_async(route, skill.route_roots(route))
    for it in scan.get("items") or []:
        p = os.path.realpath(str(it.get("path") or ""))
        if p == want or p.startswith(want + os.sep):
            return it
    return None


def _auto_target() -> Dict[str, Any]:
    for t in _targets():
        if t["route"] == AUTO_TARGET_ROUTE:
            if not t["writable"]:
                raise StoreError("自动兜底目标不可写：%s（%s）"
                                 % (t["route"], t["dir"]), status=409)
            return t
    raise StoreError("自动兜底目标不在可写发现点里：%r（可用：%s）"
                     % (AUTO_TARGET_ROUTE, ", ".join(t["route"] for t in _targets())),
                     status=409)


async def acquire(query: str, caller: str = "", task: str = "",
                  allow_install: bool = True, dry_run: bool = False) -> Dict[str, Any]:
    """本地优先找技能；不够像就搜商店、按门槛装到自动目标，再回正文。

    **失败一律 200 + `ok=false` + `reason`**（只有参数错/额度满/CLI 坏才抛
    StoreError）：对 agent 来说「没找到」是正常结果，不该用 404 逼它猜是路径错
    还是真没有。装成功/本地命中都回顶层 `name`/`route`/`content`，与 `/invoke` 同形，
    调用方不必为两条链路写两套解析。
    """
    t0 = time.monotonic()
    q = _clean(query, 300)
    if not q:
        raise StoreError("query 为空", status=400)
    out: Dict[str, Any] = {
        "ok": False, "query": q, "caller": _clean(caller, 64),
        "task": tdai_client.scrub(_clean(task, 500)),
        "source": "none", "local": {}, "store": {}, "took_ms": 0,
    }

    # ① 本地优先
    try:
        res, corpus = await _local_rank(q)
    except Exception as e:  # noqa: BLE001 — 扫描失败如实报，不装作「本地没有」
        raise StoreError("本地技能扫描失败：%s: %s" % (type(e).__name__, e), status=500)
    top, cov, accepted = _local_verdict(res)
    out["local"] = {
        "corpus": corpus,
        "candidates": [_cand(x) for x in (res.get("items") or [])],
        "top_score": (top or {}).get("score"),
        "norm": res.get("norm"),
        "coverage": cov,
        "accepted": accepted,
        "why": res.get("why") or "",
    }
    if accepted and top:
        it = top.get("item") or {}
        got = await skill._resolve_skill(str(it.get("name") or ""), it.get("route"), True)
        out.update({
            "ok": True, "source": "local", "trust": "workspace",
            "name": got.get("name"), "route": got.get("route"),
            "content": got.get("content"), "truncated": got.get("truncated"),
            "bytes_total": got.get("bytes_total"), "path": got.get("path"),
            "matched": top.get("matched"), "matched_in": top.get("matched_in"),
            "took_ms": round((time.monotonic() - t0) * 1000, 1),
        })
        return out

    # ② 商店搜索
    if not AUTO_ENABLED and allow_install:
        out["store"] = {"ok": False, "reason": "自动安装已关闭（SKILL_STORE_AUTO=0）"}
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    try:
        cand_items, traces = _search_candidates(q)
    except StoreError as e:
        out["store"] = {"ok": False, "reason": str(e)}
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    picked, metrics = _pick_store_item(q, cand_items)
    store: Dict[str, Any] = {
        "ok": False, "query": q, "searched": len(cand_items),
        "queries": traces,
        "metrics": metrics,
        "candidates": cand_items[:5],
        "picked": picked,
    }
    out["store"] = store
    if not picked:
        store["reason"] = "商店没有返回结果"
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    if not metrics.get("accepted"):
        store["reason"] = ("候选与查询不够像（关键词名称 %.2f / 描述 %.2f / 查询覆盖 %.2f，"
                           "门槛 %.2f/%.2f），未自动安装；换更具体的词或到商店页人工挑"
                           % (metrics.get("kw_name", 0), metrics.get("kw_desc", 0),
                              metrics.get("qcov", 0), STORE_KEYWORD_NAME_MIN,
                              STORE_QUERY_COVERAGE_MIN))
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out

    slug = str(picked.get("slug") or "")
    tgt = _auto_target()
    dest = os.path.join(tgt["dir"], slug)

    # ③ 安装（已存在则跳过；dry_run / 不允许安装只报计划）
    if os.path.isdir(dest):
        inst: Dict[str, Any] = {"ok": True, "skipped": True, "already_present": True,
                               "route": tgt["route"], "dir": dest}
    elif dry_run:
        inst = {"ok": False, "skipped": True, "reason": "dry_run：只报告将装到哪",
                "route": tgt["route"], "dir": dest}
    elif not allow_install:
        inst = {"ok": False, "skipped": True, "reason": "allow_install=false",
                "route": tgt["route"], "dir": dest}
    else:
        used = _auto_used_last_hour()
        if used >= AUTO_MAX_PER_HOUR:
            raise StoreError("自动安装额度已用满（近一小时 %d/%d）；稍后再试或人工在商店页安装"
                             % (used, AUTO_MAX_PER_HOUR), status=429)
        inst = install(slug, tgt["route"])
        try:
            db.log_profile_event(runlog.SOURCE, "skill.store_auto", "success",
                                 int((time.monotonic() - t0) * 1000),
                                 detail={"q": q, "slug": slug, "route": tgt["route"],
                                         "caller": out["caller"], "name": picked.get("name")})
        except Exception as e:  # noqa: BLE001 — 审计写失败不许把装好的东西说成失败
            log.warning("store_auto audit failed: %s", e)
    store["install"] = inst
    if not inst.get("ok"):
        store["reason"] = inst.get("reason") or "安装未执行"
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out

    # ④ 装完立刻解析正文（不缓存副本：正文始终从磁盘现读）
    item = await _find_installed(tgt["route"], dest)
    if item is None:
        raise StoreError("已安装但扫描不到 SKILL.md：%s（商店包可能不含技能文件）" % dest,
                         status=502)
    got = await skill._resolve_skill(str(item.get("name") or ""), tgt["route"], True)
    out.update({
        "ok": True, "source": "store", "trust": "community", "warning": STORE_TRUST_NOTE,
        "name": got.get("name"), "route": got.get("route"),
        "content": got.get("content"), "truncated": got.get("truncated"),
        "bytes_total": got.get("bytes_total"), "path": got.get("path"),
        "slug": slug, "version": picked.get("version"),
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    })
    return out


def auth_status() -> Dict[str, Any]:
    """账号态：**搜索与下载不需要登录**（2026-10-20 实测匿名可用），
    需要 skh_ Token 的只有发布 / 评论 / 在线 run / 身份查询。

    这三项本仓都不接（`run` 会把正文发到第三方执行，见模块 docstring 的边界），
    所以面板把它当**可选入口**展示：没登录不影响自动兜底，登录后可去社区发布/评论。
    """
    if not os.path.isfile(CLI):
        return {"logged_in": False, "detail": "CLI 不存在", "can_check": False,
                "login_required_for": ["publish", "comment", "run", "whoami"],
                "login_not_required_for": ["search", "install"]}
    try:
        rc, out, err = _run(["--skip-self-upgrade", "auth", "whoami"], 10)
    except StoreError as e:
        return {"logged_in": False, "detail": str(e), "can_check": False,
                "login_required_for": ["publish", "comment", "run", "whoami"],
                "login_not_required_for": ["search", "install"]}
    text = _clean(out or err, 300)
    return {"logged_in": rc == 0, "detail": text, "can_check": True,
            "login_required_for": ["publish", "comment", "run", "whoami"],
            "login_not_required_for": ["search", "install"]}


@router.get("/status")
async def store_status() -> Dict[str, Any]:
    """CLI 可用性与可安装目标。`available=false` 时面板必须显示原因，不许静默。"""
    st = status()
    st["targets"] = _targets()
    st["cli_present"] = bool(shutil.which("skillhub"))
    st["auto"] = auto_policy()
    st["auth"] = auth_status()
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


@router.post("/acquire")
@runlog.track("skill.invoke")
async def store_acquire(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """**自动取技能**：给一段任务描述，本地有就直接给，没有就搜商店装好再给。

    这是 agent 侧的「补货」入口，与商店页的「搜索→安装」是同一套后端能力，
    差别只在**门槛与预算**：机器发起，所以没找到就明确说没找到，不装不像的东西。

    请求体：`{query, caller?, task?, allow_install=true, dry_run=false}`
      · `allow_install=false` 只做本地 + 商店检索，报告「将会装什么」；
      · `dry_run=true` 同理且明确标注 dry_run。
    返回：顶层 `name/route/content/...`（与 `/api/skill/invoke` 同形）+ `source`
    （local/store/none）+ `local`/`store` 两段判据（分数、覆盖率、候选）。
    """
    try:
        return await acquire(str(payload.get("query") or ""),
                             caller=str(payload.get("caller") or ""),
                             task=str(payload.get("task") or ""),
                             allow_install=bool(payload.get("allow_install", True)),
                             dry_run=bool(payload.get("dry_run", False)))
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:400])


@router.get("/auth")
async def store_auth() -> Dict[str, Any]:
    """账号态。`logged_in=false` 不影响搜索/安装——那两项匿名可用。"""
    return auth_status()


@router.post("/login")
async def store_login(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """保存 SkillHub 个人 API Token（`skh_...`），供发布 / 评论 / 在线 run 使用。

    ⚠ **凭据处理**：key 只在本次请求内存里，交给官方 CLI 写入
    `~/.skillhub/credentials.json`（CLI 自己的存储），hub **不落库、不写日志、不回显**。
    审计只记「有一次登录动作 + 成功与否」。CLI 必须收 `--key` 参数，故 key 会短暂
    出现在本机进程表里（`ps` 可见）——这是该 CLI 的既有接口限制，不是本模块的选择。
    """
    key = str(payload.get("key") or "").strip()
    if not re.match(r"^skh_[A-Za-z0-9_-]{8,}$", key):
        raise HTTPException(400, "Token 形状不对：应以 skh_ 开头且长度 ≥ 12")
    t0 = time.monotonic()
    try:
        rc, out, err = _run(["--skip-self-upgrade", "login", "--key", key], 20)
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:300])
    ok = rc == 0
    msg = _clean(out or err, 300) if not ok else "已保存 Token"
    try:
        db.log_profile_event(runlog.SOURCE, "skill.store_login", "success" if ok else "fail",
                             int((time.monotonic() - t0) * 1000),
                             detail={"has_key": True, "error": "" if ok else _clean(err or out, 200)})
    except Exception as e:  # noqa: BLE001
        log.warning("store_login audit failed: %s", e)
    if not ok:
        raise HTTPException(502, msg)
    st = auth_status()
    st["saved"] = True
    return st


@router.post("/logout")
async def store_logout() -> Dict[str, Any]:
    """清除本机保存的个人 Token（调用官方 `auth logout`）。"""
    try:
        rc, out, err = _run(["--skip-self-upgrade", "auth", "logout"], 15)
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:300])
    if rc != 0:
        raise HTTPException(502, _clean(out or err, 300))
    return auth_status()
