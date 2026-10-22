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
import math
import os
import re
import subprocess
import time
import urllib.parse
import urllib.request
from datetime import datetime, timedelta, timezone
from typing import Any, Dict, Iterable, List, Optional, Tuple

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

#: 2026-10-22 批 A3：**相对路径**接受判据（与绝对门槛是 OR 关系）。
#:
#: 【为什么单靠绝对值不够】实测（8 中文 + 6 英文真实任务）：中文任务里
#: `norm` 集中在 0.07~0.36、英文在 0.69~1.02。同一条 `LOCAL_NORM_MIN=0.40`
#: 对英文是宽松门槛、对中文是**实质封死** ⇒ `/auto` 中文接受率只有 1/8，
#: 与「中文查询打不中英文技能名」叠加后，自动取用对中文基本不可用。
#:
#: 【为什么不直接把 0.40 降到 0.2】那会把噪声也放进来（全局门槛一降，
#: 所有任务的 top1 都会过线），把一个「阈值偏严」问题换成一个「不分好坏」问题。
#: 所以补一条**结构判据**：绝对分可以低，但必须同时满足
#:   ① norm ≥ 0.18（硬下限：再低就不像任何具体技能）
#:   ② top1 / top2 ≥ 1.8（第一名明显领先：不是一堆技能并列沾边）
#:   ③ 命中必须在 name 或 triggers 上（不能只靠一段长描述的某个泛词）
#: 置信度概念上比纯绝对门槛高：它回答的是「这一条是否明显脱颖而出」，
#: 而不是「它的绝对分数在这个语料里算不算高」（后者随语料变）。
#: 三条阈值均为实测后定值（见 tests/test_skill_retrieval_cn_l0.py 的夹具）。
LOCAL_NORM_FLOOR = 0.18
LOCAL_GAP_MIN = 1.8

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

    2026-10-22 批 C5：`n` 内部放大后按技能名折叠，再截到 `n` ——
    同名多副本（实测 380 行只有 276 名）否则会把 top-3 变成「2 个技能 + 1 个副本」。
    """
    routes = skill.disk_routes()
    scans = await asyncio.gather(*[skill._scan_async(r, skill.route_roots(r)) for r in routes])
    items: List[Dict[str, Any]] = []
    for s in scans:
        items.extend(s.get("items") or [])
    unique, _aliases = skill._dedup(items)
    res = skill_relevance.rank(query, unique, n=max(n, min(20, n * 5)))
    res["items"] = skill_relevance.collapse_by_name(res.get("items") or [], keep=1)[:max(n, 1)]
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

    接受判据两条路取 OR（阈值与理由见 `LOCAL_NORM_FLOOR` / `LOCAL_GAP_MIN` 注释）：
      A) 绝对：`norm ≥ LOCAL_NORM_MIN`（英文场景一直走这条，行为不变）
      B) 相对：`norm ≥ LOCAL_NORM_FLOOR` 且 `top1/top2 ≥ LOCAL_GAP_MIN`
         且**命中在 name 或 triggers 上**（不能只靠描述里的泛词）。
    B 是为了让中文查询（因分词与语料不同语言而绝对分天然偏低）有路可走，
    同时不给「一堆技能并列沾边」的噪声开口子。
    """
    items = res.get("items") or []
    if not items:
        return None, 0.0, False
    top = items[0]
    qt = {t for t in (res.get("query_tokens") or []) if t}
    matched = {t for t in (top.get("matched") or []) if t}
    cov = (len(matched & qt) / len(qt)) if qt else 0.0
    norm = float(res.get("norm") or 0.0)
    top_score = float(top.get("score") or 0.0)
    second = float((items[1].get("score") if len(items) > 1 else 0.0) or 0.0)
    gap = (top_score / second) if second > 0 else (999.0 if top_score > 0 else 0.0)
    strong_field = bool(top.get("matched_in")) or bool(top.get("trig_hit"))
    accepted = norm >= LOCAL_NORM_MIN or (
        norm >= LOCAL_NORM_FLOOR and gap >= LOCAL_GAP_MIN and strong_field)
    # 把判据里的两个量回传：调用方要能回答「为什么没接受」，而不是只看到一个 bool。
    res["gap"] = round(gap, 3)
    res["strong_field"] = strong_field
    return top, round(cov, 3), accepted


def _cand(x: Dict[str, Any]) -> Dict[str, Any]:
    it = x.get("item") or {}
    return {"name": it.get("name"), "route": it.get("route"),
            "score": x.get("score"), "matched": x.get("matched"),
            "matched_in": x.get("matched_in")}


#: 中文任务句里的虚词：切短查询时当分隔符。**只在造查询时用**，不参与相关性/
#: 分词判据——切错只影响召回，不会把「像」判成「不像」。
_CN_STOP = set(
    "的了着过把被给让使叫成并且或与及在和就也都还又很最更从到对向由用为是有无"
    "你我他她它这那些个些请帮将按照然后时后前一二三四五六七八九十百千上下里外"
    "要不要能会可需先再才只仅每各"
)


def _cn_segments(query: str, limit: int = 2) -> List[str]:
    """把中文长句切成 **2~4 字**的短查询（按虚词切段，每段取头/尾，最多 `limit` 条）。

    这是**上游接口特性**逼出来的，不是我们的偏好。2026-10-20 实测同一句任务
    `把播客音频转成带说话人标注的文字稿`（12 条/页，看前 3）：

      | 发出的查询 | 首条结果 | 是否相关 |
      |---|---|---|
      | 整句 | 编程专家Pro.Skill | 否 |
      | `播客 音频 文字稿`（空格分隔） | 编程专家Pro.Skill | 否（空格反而更差） |
      | `播客音频`（4 字） | 播客音频转知识笔记 | **是** |
      | `播客音频转`（5 字） | 播客音频转知识笔记 | 是（边界） |
      | `带说话人标注`（6 字） | 编程专家Pro.Skill | 否 |

    另一批（`语音转文字`/`音频转写`/`文字稿` 均命中，`长视频剪`/`自动加字`/`月度报表`
    全不命中）说明：**上游对中文是词级匹配，长度约 4 字以内**。所以派生查询截到 4 字。

    派生只在首轮无候选时做（见 acquire），不做无条件 fan-out——每次兜底多付网络往返。
    """
    text = str(query or "")
    runs: List[str] = []
    cur: List[str] = []
    for ch in text:
        if ch.isascii():
            if ch.isalnum() and cur:
                cur.append(ch)
            else:
                if cur:
                    runs.append("".join(cur)); cur = []
            continue
        if ch in _CN_STOP or not ch.strip() or not ("\u4e00" <= ch <= "\u9fff"):
            if cur:
                runs.append("".join(cur)); cur = []
            continue
        cur.append(ch)
    if cur:
        runs.append("".join(cur))
    out: List[str] = []
    for r in runs:
        # ≤5 字本身就是「词」，别再截（`语音转文字` 截成 `语音转文` 反而搜不到）；
        # 超过才取头/尾 4 字——实测 6 字短语（`带说话人标注`）上游已经匹配不上。
        for seg in ([r] if len(r) <= 5 else (r[:4], r[-4:])):
            if len(seg) >= 2 and seg not in out:
                out.append(seg)
    out.sort(key=lambda x: -len(x))
    return out[:limit]


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


# ---------------------------------------------------------------- 同类择优
#: 商店搜索 API。CLI 的 `search --json` 只透出 slug/name/description/version，把上游
#: 已经返回的 `downloads` / `stars` / `labels.requires_api_key` / `updated_at` 丢掉了
#: ——而那几项正是「同类技能里选哪个」要用的数据（2026-10-20 实测：`pdf` 一词命中
#: 12 条，名称都像，靠描述分不开，下载量与安全报告才分得开）。
#: 直连同一端点；任何失败都回落 CLI，绝不因为富字段拿不到就不搜。
API_SEARCH = os.getenv("SKILLHUB_SEARCH_API", "https://api.skillhub.cn/api/v1/search")
API_TIMEOUT_S = float(os.getenv("SKILLHUB_API_TIMEOUT", "10"))

#: 安全报告（科恩/云鼎两家）逐 slug 查询，慢（一次子进程），因此只查**入围者**并缓存。
SEC_TIMEOUT_S = float(os.getenv("SKILLHUB_SEC_TIMEOUT", "12"))
SEC_CACHE_TTL_S = float(os.getenv("SKILLHUB_SEC_CACHE", "900"))
SEC_MAX_CHECKS = int(os.getenv("SKILLHUB_SEC_MAX", "3"))
_sec_cache: Dict[str, Tuple[float, Dict[str, Any]]] = {}

#: 质量分权重（六项加起来 = 1.0）。标定见 `work/store-cal/`，**不是拍脑袋**：
#: 相关性给最大权重是因为「不像」的技能装上也用不上；下载量次之——它是本机唯一
#: 能拿到的、跨作者可比的「别人真用过」的证据；安全取二值（可疑直接否决，
#: 权重只影响未被否决者之间的排序）。
W_REL = 0.35
W_SEC = 0.20
W_POP = 0.25
W_FRESH = 0.10
W_VERIFIED = 0.05
W_NO_KEY = 0.05

#: 自动安装的质量底线。低于它宁可回 `none` 让 agent 换词或人工挑选——
#: 「装上一个用不上的技能」比「没装」更糟：它污染发现点，还要人工清理。
STORE_QUALITY_MIN = 0.45

#: 需要自带 Key 的技能：不是否决项（用户可能已经有），但同类有免 Key 的就不选它。
NEEDS_KEY_PENALTY = 0.0  # 已含在 W_NO_KEY 里；留常量名给报告用，不参与计算


def _num(x: Any) -> int:
    try:
        return int(float(x or 0))
    except (TypeError, ValueError):
        return 0


def _api_search(query: str, limit: int = 12) -> List[Dict[str, Any]]:
    """直连搜索 API 取富字段。**失败一律抛 StoreError**，由调用方回落 CLI。"""
    base = str(API_SEARCH or "").strip()
    if not base:
        raise StoreError("未配置搜索 API（回落 CLI）")
    q = str(query or "").strip()
    if not q:
        raise StoreError("搜索词为空", status=400)
    url = base + "?" + urllib.parse.urlencode({"q": q, "limit": max(1, min(30, int(limit)))})
    req = urllib.request.Request(url, headers={"Accept": "application/json",
                                               "User-Agent": "agenthub-skill-store/1"})
    try:
        with urllib.request.urlopen(req, timeout=API_TIMEOUT_S) as resp:
            raw = json.loads(resp.read().decode("utf-8", "replace"))
    except Exception as e:  # noqa: BLE001 — 网络/解析失败都是「富字段拿不到」，不是「搜不到」
        raise StoreError("搜索 API 失败：%s: %s" % (type(e).__name__, str(e)[:160]))
    if not isinstance(raw, dict):
        raise StoreError("搜索 API 返回非对象")
    out: List[Dict[str, Any]] = []
    for r in (raw.get("results") or [])[:50]:
        if not isinstance(r, dict):
            continue
        ns = r.get("namespace") if isinstance(r.get("namespace"), dict) else {}
        handle = _clean(ns.get("handle"), 80)
        pub = _clean(ns.get("publicSlug") or r.get("slug"), 120)
        labels = r.get("labels") if isinstance(r.get("labels"), dict) else {}
        canon = ("@%s/%s" % (handle, pub)) if handle and pub else _clean(r.get("slug"), 160)
        if not canon:
            continue
        out.append({
            "slug": canon,
            "public_slug": pub,
            "namespace": _clean(ns.get("displayName"), 80),
            "handle": handle,
            "name": _clean(r.get("name") or r.get("displayName"), 200),
            "description": _clean(r.get("description") or r.get("summary")),
            "version": _clean(r.get("version"), 40),
            "source": _clean(r.get("source"), 40),
            "downloads": _num(r.get("downloads")),
            "stars": _num(r.get("stars")),
            "installs": _num(r.get("installs")),
            "needs_key": str(labels.get("requires_api_key")).strip().lower() == "true",
            "verified": bool(r.get("verified")),
            "updated_at": _num(r.get("updated_at") or r.get("updatedAt")),
            "category": _clean(r.get("category"), 60),
            "tags": [_clean(t, 40) for t in (r.get("tags") or []) if t][:8],
        })
    if not out:
        raise StoreError("搜索 API 无结果")
    return out


def _search_enriched(query: str, limit: int = 12) -> Dict[str, Any]:
    """富字段优先、CLI 兜底。返回体统一带 `via`，调用方能看到走的是哪条路。"""
    try:
        items = _api_search(query, limit)
        return {"query": _clean(query, 120), "count": len(items), "items": items, "via": "api"}
    except StoreError as e:
        log.warning("rich search unavailable, fallback to CLI: %s", e)
    res = search(query, limit)
    for it in res["items"]:
        it.setdefault("needs_key", False)
        it.setdefault("downloads", None)      # None＝未知，不是 0：0 会被算成「没人用」
        it.setdefault("stars", None)
        it.setdefault("verified", None)
        it.setdefault("updated_at", None)
    res["via"] = "cli"
    return res


def _security(slug: str, use_cache: bool = True) -> Dict[str, Any]:
    """查一家/两家安全实验室的报告，归成 `benign|suspicious|unknown`。

    **`unknown` 不是 `benign`**：报告 404 / 网络失败 / CLI 不认识该 slug 都算未知，
    评分里按 0.5 计（既不当安全也不当危险），但回给调用方时如实写 `unknown` + 原因。
    这条区分是本模块的底线：把「没查到」说成「安全」，等于拿沉默当背书。
    """
    key = str(slug or "")
    if not key:
        return {"verdict": "unknown", "detail": "空 slug"}
    now = time.time()
    if use_cache:
        hit = _sec_cache.get(key)
        if hit and now - hit[0] < SEC_CACHE_TTL_S:
            return hit[1]
    handle, pub = _split_slug(key)
    if not pub:
        return {"verdict": "unknown", "detail": "slug 形状无法解析"}
    args = ["--skip-self-upgrade", "skill", "reports", pub, "--json",
            "--timeout", str(max(3, int(SEC_TIMEOUT_S)))]
    if handle:
        args += ["--namespace", handle]
    try:
        rc, out, err = _run(args, SEC_TIMEOUT_S + 6)
    except StoreError as e:
        return {"verdict": "unknown", "detail": str(e)[:200]}
    if rc != 0:
        # 404 = 该技能没有公开报告；其余 = 查询失败。两者都是「不知道」，如实分列。
        detail = _clean(err or out, 200)
        return {"verdict": "unknown", "detail": detail or ("rc=%d" % rc)}
    try:
        raw = _json_of(out)
    except StoreError as e:
        return {"verdict": "unknown", "detail": "报告不可解析：%s" % str(e)[:120]}
    reports = raw.get("reports") if isinstance(raw.get("reports"), dict) else {}
    verdicts: Dict[str, str] = {}
    providers: List[Dict[str, Any]] = []
    for name in ("keen", "yunding"):
        r = reports.get(name)
        if not isinstance(r, dict):
            continue
        st = str(r.get("status") or "").strip().lower() or "unknown"
        verdicts[name] = st
        providers.append({"name": _clean(r.get("name") or name, 40), "status": st,
                          "status_text": _clean(r.get("statusText"), 80),
                          "report_url": _clean(r.get("reportUrl"), 400)})
    if "suspicious" in verdicts.values() or "malicious" in verdicts.values():
        verdict = "suspicious"
    elif verdicts and all(v in ("benign", "safe", "pass", "clean") for v in verdicts.values()):
        verdict = "benign"
    else:
        verdict = "unknown"
    res = {"verdict": verdict, "providers": providers,
           "detail": "" if providers else "无公开安全报告"}
    if use_cache:
        _sec_cache[key] = (now, res)
    return res


def _split_slug(slug: str) -> Tuple[str, str]:
    """`@handle/name` → (handle, name)；`name` → ("", name)。"""
    s = str(slug or "").strip()
    if s.startswith("@") and "/" in s:
        h, _sep, n = s[1:].partition("/")
        return _clean(h, 80), _clean(n, 120)
    return "", _clean(s, 120)


def _pop_score(downloads: Optional[int]) -> float:
    """下载量取对数：1e6 → 1.0，1e3 → 0.5，0 → 0。未知（None）给 0.35。

    未知不能当 0——CLI 兜底路径拿不到下载量，若按 0 计，所有候选都会被同等打压，
    排序就退化成只看关键词，等于白做多维度。
    """
    if downloads is None:
        return 0.35
    n = max(0, int(downloads))
    if n <= 0:
        return 0.0
    return min(1.0, (math.log10(1 + n) / 6.0))


def _fresh_score(updated_ms: Optional[int]) -> float:
    """更新越新越好：≤120 天 1.0，≤1 年 0.7，≤2 年 0.4，更早 0.2。未知 0.4。"""
    if not updated_ms:
        return 0.4
    age = (time.time() * 1000.0 - float(updated_ms)) / 86400000.0
    if age < 0:          # 上游时间戳可能是未来值（时钟/时区问题），按最新算
        return 1.0
    if age <= 120:
        return 1.0
    if age <= 365:
        return 0.7
    if age <= 730:
        return 0.4
    return 0.2


def _rel_scores(query: str, it: Dict[str, Any], alts: Iterable[str] = ()
                ) -> Tuple[float, float, float, float]:
    """`(关键词名称命中, 关键词描述命中, 整句覆盖率, 名称覆盖率)`，全在 0~1。

    `alts` 是**同一任务派生出的短查询**（见 `_cn_segments`）。命中它们里的任何一个
    都算命中这个任务——但只对「覆盖率」类指标生效，ASCII 关键词那两维仍只按原句算
    （短查询本来就是为了在没有 ASCII 词时才派生的）。
    """
    kws = set(_store_keywords(query))
    qt = {t for t in skill_relevance.tokenize(query) if t}
    nt = set(skill_relevance.tokenize(str(it.get("name") or "")))
    dt = set(skill_relevance.tokenize(str(it.get("description") or "")))
    kws_q = kws
    kw_name = (len(kws_q & nt) / len(kws_q)) if kws_q else 0.0
    kw_desc = (len(kws_q & dt) / len(kws_q)) if kws_q else 0.0
    qcov = (len(qt & (nt | dt)) / len(qt)) if qt else 0.0
    qcov_name = (len(qt & nt) / len(qt)) if qt else 0.0
    for alt in alts or ():
        at = {t for t in skill_relevance.tokenize(str(alt)) if t}
        if not at:
            continue
        qcov = max(qcov, len(at & (nt | dt)) / len(at))
        qcov_name = max(qcov_name, len(at & nt) / len(at))
    return round(kw_name, 3), round(kw_desc, 3), round(qcov, 3), round(qcov_name, 3)


def score_candidates(query: str, items: List[Dict[str, Any]],
                     sec_by_slug: Optional[Dict[str, Dict[str, Any]]] = None,
                     alts: Iterable[str] = ()
                     ) -> List[Dict[str, Any]]:
    """给每个候选打六维分并给出**可读的结论**（纯函数，不联网、不写盘）。

    维度：相关性（关键词/整句覆盖）、安全（报告）、流行度（下载量）、
    维护度（更新时间）、认证（平台 verified）、免 Key。
    产出的 `reasons` 是给人看的判据来源——面板上要能回答「为什么选它不选它」。
    """
    sec_by_slug = sec_by_slug or {}
    kws = sorted(set(_store_keywords(query)))
    rows: List[Dict[str, Any]] = []
    for it in items or []:
        kw_name, kw_desc, qcov, qcov_name = _rel_scores(query, it, alts)
        # 相关性两路取大，不取加权和：`kw_name` 只对 **ASCII 关键词**有定义，中文任务
        # 的查询里没有 ASCII 词 ⇒ 该通道结构性恒 0，`rel` 上限被压到 0.35，而 ASCII
        # 任务的同类候选上限是 1.0 ⇒ 中文技能光因为「名字是中文」就永远进不了门槛
        # （实测 2026-10-20：`播客音频转知识笔记` 六维合计 0.4355 < 0.45 被否决）。
        # `qcov_name` 是同一件事的中文可达形式（名字的二字组覆盖）。取大不取和：
        # 两者都是「像不像」的**同一个判断**，相加会重复计数。
        name_fit = max(kw_name, qcov_name) if (kw_name or qcov_name) else 0.0
        rel = round(0.65 * name_fit + 0.35 * qcov, 3)
        sec = sec_by_slug.get(str(it.get("slug") or "")) or {"verdict": "unknown"}
        verdict = str(sec.get("verdict") or "unknown")
        sec_score = {"benign": 1.0, "unknown": 0.5, "suspicious": 0.0}.get(verdict, 0.5)
        pop = round(_pop_score(it.get("downloads")), 3)
        fresh = _fresh_score(it.get("updated_at"))
        verified = 1.0 if it.get("verified") else 0.0
        needs_key = bool(it.get("needs_key"))
        quality = round(W_REL * rel + W_SEC * sec_score + W_POP * pop
                        + W_FRESH * fresh + W_VERIFIED * verified
                        + W_NO_KEY * (0.0 if needs_key else 1.0), 3)
        # 「整句像不像」与「派生短词像不像」必须分开判：前者才允许自动安装。
        # 派生短词（`_cn_segments`）是把长句截成词，命中它只说明「这条技能和任务里
        # 某个词有关」——`播客音频` 能命中「播客音频生成」，那是**合成音频**，与
        # 「转成文字稿」相反。拿它自动装就是装了个反向技能。
        _m, _d, qcov_main, _mn = _rel_scores(query, it) if alts else (kw_name, kw_desc, qcov, qcov_name)
        matched_main = bool((kws and (kw_name >= STORE_KEYWORD_NAME_MIN
                                      or kw_desc >= STORE_QUERY_COVERAGE_MIN))
                            or qcov_main >= STORE_QUERY_COVERAGE_MIN)
        matched = bool((kws and (kw_name >= STORE_KEYWORD_NAME_MIN
                                 or kw_desc >= STORE_QUERY_COVERAGE_MIN))
                       or qcov >= STORE_QUERY_COVERAGE_MIN)
        veto = ""
        if not matched:
            veto = "与查询不够像（关键词名称 %.2f / 描述 %.2f / 整句 %.2f）" % (kw_name, kw_desc, qcov)
        elif not matched_main:
            veto = ("整句覆盖 %.2f 未达门槛 %.2f，只靠派生短词命中；"
                    "不自动安装（短词命中的可能是反向技能），需人工看一眼再装"
                    % (qcov_main, STORE_QUERY_COVERAGE_MIN))
        elif verdict == "suspicious":
            veto = "安全报告标记可疑（%s）" % "；".join(
                "%s:%s" % (p["name"], p["status_text"] or p["status"]) for p in sec.get("providers") or [])
        elif quality < STORE_QUALITY_MIN:
            veto = "质量分 %.2f 低于底线 %.2f" % (quality, STORE_QUALITY_MIN)
        rows.append({
            "slug": it.get("slug"), "name": it.get("name"),
            "version": it.get("version"), "downloads": it.get("downloads"),
            "needs_key": needs_key, "verified": bool(it.get("verified")),
            "security": verdict, "security_providers": sec.get("providers") or [],
            "dims": {"rel": rel, "kw_name": kw_name, "kw_desc": kw_desc, "qcov": qcov,
                     "qcov_name": qcov_name,
                     "sec": sec_score, "pop": pop, "fresh": fresh,
                     "verified": verified, "no_key": 0.0 if needs_key else 1.0},
            "quality": quality, "matched": matched, "matched_main": matched_main,
            "veto": veto,
            "item": it,
        })
    rows.sort(key=lambda r: (0 if r["veto"] else 1, r["quality"]), reverse=True)
    return rows


def _pick_scored(rows: List[Dict[str, Any]]
                 ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """从评分行里挑一条可自动安装的。返回 `(row, metrics)`。

    选择顺序：先排除 `veto`，再**优先免 Key**（同类有免 Key 的就不装要配 Key 的，
    否则装完 agent 还得停下来要凭据），最后比质量分。全被否决时返回 `(None, metrics)`
    并给 `reason`，绝不「矮子里拔将军」。
    """
    live = [r for r in rows if not r["veto"]]
    metrics: Dict[str, Any] = {
        "considered": len(rows), "viable": len(live),
        "threshold": STORE_QUALITY_MIN,
        "weights": {"rel": W_REL, "sec": W_SEC, "pop": W_POP,
                    "fresh": W_FRESH, "verified": W_VERIFIED, "no_key": W_NO_KEY},
    }
    if not live:
        worst = rows[0] if rows else None
        metrics["reason"] = (worst or {}).get("veto") or "商店没有可用的候选"
        return None, metrics
    free = [r for r in live if not r["needs_key"]]
    pool = free or live
    best = max(pool, key=lambda r: r["quality"])
    metrics.update({
        "picked_quality": best["quality"], "picked_dims": best["dims"],
        "needs_key": best["needs_key"],
        "why": ("免 Key 组内质量最高" if free else "没有免 Key 候选，退而选质量最高（需自带 Key）"),
    })
    return best, metrics


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
            r = _search_enriched(sq, 12)
        except StoreError as e:
            if not items and sq == plan[-1]:
                raise
            trace.append({"query": sq, "error": str(e)})
            continue
        trace.append({"query": sq, "count": r["count"], "via": r.get("via")})
        for it in r["items"]:
            slug = str(it.get("slug") or "")
            if slug and slug not in seen:
                seen.add(slug)
                items.append(it)
    return items, trace


def _rank_row(r: Dict[str, Any]) -> Dict[str, Any]:
    """排行行投影：只留解释得清、面板画得下的字段（不塞整条描述）。"""
    return {
        "slug": r["slug"], "name": r["name"], "version": r["version"],
        "downloads": r["downloads"], "needs_key": r["needs_key"],
        "verified": r["verified"], "security": r["security"],
        "quality": r["quality"], "dims": r["dims"], "veto": r["veto"],
    }


def _pick_store_item(query: str, items: List[Dict[str, Any]]
                     ) -> Tuple[Optional[Dict[str, Any]], Dict[str, Any]]:
    """兼容入口：不带安全报告时按同一评分器挑一条（完整链路见 `acquire`）。

    保留它是因为「只想看像不像、不查安全」的调用方（标定脚本、单元测试）不该被迫
    为一次判断付 N 次子进程。判据与 `acquire` 完全同一套，不另写一份。
    """
    rows = score_candidates(query, items)
    row, metrics = _pick_scored(rows)
    metrics["keywords"] = sorted(set(_store_keywords(query)))
    if row:
        metrics.update({"kw_name": row["dims"]["kw_name"], "kw_desc": row["dims"]["kw_desc"],
                        "qcov": row["dims"]["qcov"], "accepted": True})
        return row["item"], metrics
    metrics["accepted"] = False
    metrics.setdefault("kw_name", 0.0)
    metrics.setdefault("kw_desc", 0.0)
    metrics.setdefault("qcov", 0.0)
    return None, metrics


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


async def _rank_store(query: str, items: List[Dict[str, Any]],
                      alts: Iterable[str] = (), with_security: bool = True
                     ) -> Tuple[List[Dict[str, Any]], Optional[Dict[str, Any]], Dict[str, Any]]:
    """排序 + 安全核查 + 挑选。**保证中选者一定查过安全报告**。

    只给初筛前 `SEC_MAX_CHECKS` 名查报告（每条一次子进程），但如果重排后中选的
    是没查过的那条，就补查它并重排（最多两轮）——否则「没查过」会被当成「未知
    即 0.5 分」混进冠军位，等于让未核查者靠运气赢。未知只在查过/查不到时才算未知。
    """
    sec_map: Dict[str, Dict[str, Any]] = {}
    pre = score_candidates(query, items, alts=alts)
    # `with_security=False`：**这一批候选根本不可能被自动装**（派生短词补搜，
    # 见 acquire）时仍然查报告，等于为一次「只展示给人看」的排序付 3 次子进程
    # （实测 2026-10-20：整条兜底 8.6s，其中约 4s 花在这里）。
    for r in (pre if with_security else [])[:max(1, SEC_MAX_CHECKS)]:
        slug = str(r["slug"] or "")
        sec_map[slug] = await asyncio.to_thread(_security, slug)
    rows = score_candidates(query, items, sec_map, alts=alts)
    row, metrics = _pick_scored(rows)
    for _round in range(2):
        if row is None or str(row["slug"]) in sec_map:
            break
        sec_map[str(row["slug"])] = await asyncio.to_thread(_security, str(row["slug"]))
        rows = score_candidates(query, items, sec_map, alts=alts)
        row, metrics = _pick_scored(rows)
    return rows, row, metrics


async def acquire(query: str, caller: str = "", task: str = "",
                  allow_install: bool = True, dry_run: bool = False,
                  local_only: bool = False) -> Dict[str, Any]:
    """本地优先找技能；不够像就搜商店、按门槛装到自动目标，再回正文。

    **失败一律 200 + `ok=false` + `reason`**（只有参数错/额度满/CLI 坏才抛
    StoreError）：对 agent 来说「没找到」是正常结果，不该用 404 逼它猜是路径错
    还是真没有。装成功/本地命中都回顶层 `name`/`route`/`content`，与 `/invoke` 同形，
    调用方不必为两条链路写两套解析。

    `local_only=True`：本地不像就**直接回未命中**，不联网、不写盘。给
    `/api/skill/auto` 的 `allow_store=false` 与短预算注入链用——`allow_install=false`
    的语义是「只报告将会装什么」，它**仍会打商店搜索**，两者不是一回事。
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
            "bytes": got.get("bytes"), "bytes_total": got.get("bytes_total"), "path": got.get("path"),
            "matched": top.get("matched"), "matched_in": top.get("matched_in"),
            "took_ms": round((time.monotonic() - t0) * 1000, 1),
        })
        return out

    # ② 商店搜索
    if local_only:
        # 不能靠 allow_install=false 表达「别联网」：那个开关只阻止落盘，
        # 搜索照打。两者混用会让「本地没找到 ⇒ 静默等了一次网络超时」。
        out["reason"] = ("只允许本地技能（local_only），最高归一化分 %.3f < 门槛 %.2f"
                         % (float(res.get("norm") or 0.0), LOCAL_NORM_MIN))
        out["store"] = {"ok": False, "searched": 0,
                        "reason": "调用方要求只用本地，未联网"}
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    if not AUTO_ENABLED and allow_install:
        out["store"] = {"ok": False, "reason": "自动安装已关闭（SKILL_STORE_AUTO=0）"}
        out["reason"] = out["store"]["reason"]
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    try:
        cand_items, traces = _search_candidates(q)
    except StoreError as e:
        out["store"] = {"ok": False, "reason": str(e)}
        out["reason"] = str(e)
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    if not cand_items:
        store = {"ok": False, "query": q, "searched": 0, "queries": traces,
                 "reason": "商店没有返回结果"}
        out["store"] = store
        out["reason"] = store["reason"]
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    # 先按不用联网的维度排一次，只给**入围者**查安全报告（一次子进程，必须限条数）；
    # 拿到报告后重排——安全是唯一能一票否决的维度，放最后一步查才不至于为 12 条
    # 候选各付一次子进程。
    rows, picked_row, metrics = await _rank_store(q, cand_items)

    # 首轮无中选者 ⇒ 用「派生短词」再搜一轮，只用来**补候选**，不降低准入门槛。
    # 为什么要有这一轮：上游对中文是词级匹配，整句（尤其带虚词）搜不到东西——
    # 实测整句返回 12 条「编程专家」，而 `播客音频` 能把播客类技能顶到第一。
    # 为什么不能拿它直接装：见 score_candidates 的 `matched_main` —— 短词命中的
    # 可能是反向技能（`播客音频生成` vs `播客音频转文字`），必须人工确认。
    if picked_row is None:
        segs = [x for x in _cn_segments(q) if x not in {t.get("query") for t in traces}]
        if segs:
            merged = list(cand_items)
            have = {str(x.get("slug") or "") for x in merged}
            # 两路并发：串行各 1~2s，兜底本来就已经在 miss 路径上了。
            got = await asyncio.gather(
                *[asyncio.to_thread(_search_enriched, sq, 12) for sq in segs[:2]],
                return_exceptions=True)
            for sq, r in zip(segs[:2], got):
                if isinstance(r, Exception):
                    traces.append({"query": sq, "error": str(r), "derived": True})
                    continue
                traces.append({"query": sq, "count": r["count"], "via": r.get("via"),
                               "derived": True})
                for it in r["items"]:
                    slug = str(it.get("slug") or "")
                    if slug and slug not in have:
                        have.add(slug); merged.append(it)
            if len(merged) > len(cand_items):
                cand_items = merged
                # 这批只用于展示（`matched_main` 已把它们全部否决），不查安全报告。
                rows, picked_row, metrics = await _rank_store(q, cand_items, alts=segs,
                                                              with_security=False)

    picked = picked_row["item"] if picked_row else None
    store = {
        "ok": False, "query": q, "searched": len(cand_items),
        "queries": traces,
        "metrics": metrics,
        "ranking": [_rank_row(r) for r in rows[:5]],
        "picked": picked,
    }
    out["store"] = store
    if not picked:
        store["reason"] = (metrics.get("reason") or "没有通过门槛的候选") + \
            "；未自动安装——换更具体的词，或到商店页看完分数人工挑"
        # 顶层也要有 reason：只写在 store 里，调用方读顶层会看到「没有原因」。
        # 与 v0.13.137 修 dry_run 的那处同型（`ok=false` 却没有 reason 就是半截话）。
        out["reason"] = store["reason"]
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out
    if picked_row["needs_key"]:
        store["warning"] = ("该技能需要自带 API Key（商店标注 requires_api_key=true）："
                            "装好不会自动可用，仍需在对应客户端配置凭据")
    store["security"] = {"verdict": picked_row["security"],
                         "providers": picked_row["security_providers"]}

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
        # **已选出候选只是没落盘** ≠ 「什么都没找到」。只回 ok=false 会让调用方
        # 把「再调一次带 allow_install=true 就能装」误读成「商店没有这个技能」。
        # 实测（2026-10-20）：dry_run 时顶层没有 reason、source=none，与真·未命中同形。
        out["reason"] = store["reason"]
        out["would_install"] = {
            "slug": slug, "name": picked.get("name"), "version": picked.get("version"),
            "route": inst.get("route"), "dir": inst.get("dir"),
            "quality": picked_row["quality"], "needs_key": picked_row["needs_key"],
            "security": picked_row["security"],
            "how_to_proceed": ("确认要装就再调一次 allow_install=true"
                               if inst.get("reason", "").startswith(("dry_run", "allow_install"))
                               else "已存在同名目录，无需再装"),
        }
        out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
        return out

    # ④ 装完立刻解析正文（不缓存副本：正文始终从磁盘现读）
    item = await _find_installed(tgt["route"], inst.get("target_dir") or dest)
    if item is None:
        raise StoreError("已安装但扫描不到 SKILL.md：%s（商店包可能不含技能文件）" % dest,
                         status=502)
    got = await skill._resolve_skill(str(item.get("name") or ""), tgt["route"], True)
    store["ok"] = True        # 装成功时 store 块也要翻绿：只翻顶层会让「store.ok=false」
    #                          与「真的装好了」同时出现在一个响应里（实测 2026-10-20）
    out.update({
        "ok": True, "source": "store", "trust": "community", "warning": STORE_TRUST_NOTE,
        "name": got.get("name"), "route": got.get("route"),
        "content": got.get("content"), "truncated": got.get("truncated"),
        "bytes": got.get("bytes"), "bytes_total": got.get("bytes_total"), "path": got.get("path"),
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
    # `shutil.which` 查的是**本进程 PATH**，systemd 下不含 ~/.local/bin ⇒ 恒 false，
    # 而 `available` 用的是绝对路径 CLI 常量（真的能跑）。两个字段自相矛盾会让面板
    # 一边说「可用」一边说「缺失」。判据统一成「CLI 这个文件可执行」。
    st["cli_present"] = bool(CLI and os.path.isfile(CLI) and os.access(CLI, os.X_OK))
    st["auto"] = auto_policy()
    st["auth"] = auth_status()
    return st


@router.get("/search")
async def store_search(q: str = Query(min_length=1, max_length=120),
                       limit: int = Query(default=12, ge=1, le=30)) -> Dict[str, Any]:
    """按关键词搜商店。带 `targets`（前端不必再查状态）+ 富字段（下载量/是否要 Key）。

    **搜索列表刻意不查安全报告**：一次报告 = 一次子进程，12 条候选要 12 次，
    面板点一下要等十几秒。安全在「真要装」时才查（`/install` 与自动兜底）。
    """
    try:
        res = await asyncio.to_thread(_search_enriched, q, limit)
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:300])
    res["targets"] = _targets()
    return res


@router.post("/install")
async def store_install(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """把商店技能装到某个发现点。**只写 SKILL_DIRS 里的目录**（见模块 docstring）。

    装前查一次安全报告（可 `check_security=false` 跳过）：
    `suspicious` 时**默认拒绝**，要装必须显式 `force=true`——「用户点了安装」这个动作
    本身不构成「已了解风险」，拒一次并把报告摆出来才是。
    """
    slug = str(payload.get("slug") or "")
    sec: Dict[str, Any] = {"verdict": "unknown", "detail": "已跳过检查"}
    if payload.get("check_security", True):
        try:
            sec = await asyncio.to_thread(_security, slug)
        except Exception as e:  # noqa: BLE001 — 查不了不等于危险，放行但要说明
            sec = {"verdict": "unknown", "detail": "检查失败：%s" % str(e)[:160]}
    if sec.get("verdict") == "suspicious" and not payload.get("force"):
        names = "；".join("%s:%s" % (p.get("name"), p.get("status_text") or p.get("status"))
                         for p in sec.get("providers") or [])
        raise HTTPException(409, "安全报告标记可疑（%s）。确认要用请带 force=true 重试"
                            % (names or "未提供细节"))
    try:
        res = install(slug, str(payload.get("route") or ""))
    except StoreError as e:
        raise HTTPException(e.status, str(e)[:300])
    res["security"] = sec
    return res


@router.post("/acquire")
# `ok=false` = 没交付任何正文（dry_run / allow_install=false / 没找到）。
# 记成 skill.invoke 会让调用记录多出一条空技能名的使用流水，权重随之虚增。
@runlog.track("skill.invoke", skip_when=lambda d: not (isinstance(d, dict) and d.get("ok")))
async def store_acquire(payload: Dict[str, Any] = Body(...)) -> Dict[str, Any]:
    """**自动取技能**：给一段任务描述，本地有就直接给，没有就搜商店装好再给。

    这是 agent 侧的「补货」入口，与商店页的「搜索→安装」是同一套后端能力，
    差别只在**门槛与预算**：机器发起，所以没找到就明确说没找到，不装不像的东西。

    请求体：`{query, caller?, task?, allow_install=true, dry_run=false}`
      · `allow_install=false` 只做本地 + 商店检索，报告「将会装什么」；
      · `dry_run=true` 同理且明确标注 dry_run。
    返回：顶层 `name/route/content/...`（与 `/api/skill/invoke` 同形）+ `source`
    （local/store/none）+ `local`/`store` 两段判据。`store.ranking` 是六维打分后的
    候选排行（含被否决的理由），`store.metrics` 是门槛与中选者的`why`。
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
