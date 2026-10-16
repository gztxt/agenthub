"""技能门面 /api/skill/* —— 只读列出/读取本机磁盘上**真实在用**的 SKILL.md，并对照 TDAI 注册表。

【为什么不建 hub 自己的技能表】
与 `kb.py` 同一口径：hub 只做**门面 + 归并 + 审计**，一个字都不存。技能的权威副本就是
磁盘上那 37 个 `SKILL.md`（pi/claude 直接读它们），在 hub 里建一张 `skills` 表就是
**第二权威副本**——它会腐烂、会与源头不一致，正是 0924 方案被否决的那条路
（见 /fs/1000/ftp/技术文档/PENDING-TASKS.md PT-20260924-08：「本机记忆/技能/文档索引/嵌入
四项权威实现均已存在，hub 只当门面」）。所以本模块**只读、不写、不缓存副本**，
每次请求都重新走一遍磁盘。37 个文件、总量 ~230KB，实测扫描是毫秒级，不需要缓存。

【磁盘侧的四条实测事实（2026-09-24 13:0x，Python 3.11.2，本会话亲自取证）】
① **`Path.rglob()` 会漏报**：`/home/gztxt/.pi/agent/skills/agent-dispatch` 是**软链目录**
   （→ `/fs/1000/ftp/技术文档/skills/agent-dispatch`），`rglob` 默认不跟随软链 ⇒ 只数到 37；
   `os.walk(followlinks=True)` 数到 **38**。所以本模块一律用 `os.walk(..., followlinks=True)`。
② **38 ≠ 37**：多出来那条就是上面的软链，同一 realpath 在 `pi` 与 `techdocs` 两路各出现一次。
   ⇒ 必须**按 realpath 去重**，但**保留别名**（`aliases[]`），否则用户看不到"pi 路是软链"这个事实。
   规范条目取 `path == realpath` 的那一份（真实文件所在路），软链路只当别名。
③ **3 个文件没有 frontmatter**：`claude/agent-ecosystem`、`claude/system-memory`、
   `claude/system-rules`（都是 800~930B 的裸正文）。⇒ 解析器必须容错：`fm=false`、
   `name` 回退成目录名，**绝不因为解析不出 frontmatter 就把整条丢掉**（丢条目＝另一种静默）。
④ **值形态不统一**：`claude/caveman` 写的是 `name: "caveman"`（带引号），最大文件
   `superpowers/subagent-driven-development` 28077B。⇒ 去引号 + 读取上限保护。

【TDAI 注册表这一路】
`tdai_client.skill_list()` 打 `POST /v3/skill/list`，实测 **HTTP 200 但 0 行**。
本门面把它**如实并列报出**（`ok=true, count=0`），不做 ETL、不灌数据——
「技能权威源到底是磁盘还是 TDAI 注册表」是 PT-20260924-08 的待裁项 **T2-1**，未裁前不动。
口径照抄 `kb.py` 对 wigolo 的处理：**禁止用字段缺失表达"不接/为空"**，必须给出
`available` + `why`/`note`，让面板能一眼看出"注册表是空的"而不是"hub 没查"。

【失败表态纪律（承接 P0/P1）】
每路必回 `backends[] = {name, ok, count, ms, error}`（5 键，与 `kb.py:_backend` 同形）；
任何一路挂了都要说得出「挂的是哪路、为什么挂、这次少了什么」。
**禁止 `except: pass`、禁止静默返回空**。所有对外文本（含 `error`、`content`）一律过
`tdai_client.scrub`——技能正文是**凭据高危面**（技能里写 curl 带 key 是本机常见形态），
脱敏只能有一个实现（见 `memory.py:79-81` 与 `tdai_client.scrub` 的注释）。
"""
import asyncio
import glob
import hashlib
import json
import jev_client
import logging
import os
import re
import skill_relevance
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel

import db
import runlog
import skill_usage
import tdai_client
import writeauth

log = logging.getLogger("hub.skill")
router = APIRouter(prefix="/api/skill", tags=["skill"])

# ── 技能目录白名单（可用 SKILL_DIRS_JSON 整体覆盖；测试靠猴补本字典把根指到 tmp）──
_DEFAULT_DIRS: Dict[str, str] = {
    "claude": "/home/gztxt/.claude/skills",
    "pi": "/home/gztxt/.pi/agent/skills",
    "techdocs": "/fs/1000/ftp/技术文档/skills",
    "superpowers": "/home/gztxt/.pi/agent/git/github.com/obra/superpowers/skills",
    # v0.13.26 批2：56 号文档实测的另外三个技能发现点。~/.agents/skills 是
    # codex 等共享发现点（39 项，含与 claude/superpowers 重名的软链）；
    # ~/.codex/skills 与 ~/.workbuddy/skills 是各自工具链的私有发现点。
    # _dedup 按 realpath 合并同源条目（软链只当别名列），不会重复计数。
    "agents": "/home/gztxt/.agents/skills",
    "codex": "/home/gztxt/.codex/skills",
    "workbuddy": "/home/gztxt/.workbuddy/skills",

    # ── v0.13.66 D1：补齐 13 路实测发现点。条数是 2026-10-03 `find -maxdepth 3` 实测，
    # 登记时**不猜**：「目录在但 0 条」与「目录不存在」由 /status 的 state 四态分开说，
    # 两者都是事实，不允许代码把空目录当正常路、把缺目录当正常路。
    "hermes": "/home/gztxt/.hermes/skills",                      # 110 · config.yaml 声明的是 skills-hot（实测 0 条），实际消费这一路
    "hermes-agent": "/home/gztxt/.hermes/hermes-agent/skills",  # 58 · 上游内置仓，与 hermes 重叠由 _dedup 按 realpath 归一
    "hermes-web": "/home/gztxt/.hermes-web-ui/.ekko/skills",    # 22
    "jcode": "/home/gztxt/.jcode/skills",                        # 56
    "grok": "/home/gztxt/.grok/skills",                          # 1
    "grok-bundled": "/home/gztxt/.grok/bundled/skills",          # 9
    "picoclaw": "/home/gztxt/.picoclaw/workspace/skills",        # 8
    "qoder": "/home/gztxt/.qoder/security/skills",               # 1
    "qoderwake": "/home/gztxt/.qoderwake/resources/builtin-skills",        # 11
    "qoderwake-shadow": "/home/gztxt/.qoderwake/run/shadow-skills",       # 1
    "qoderwake-cli": "/home/gztxt/.qoderwake/qodercli/security-resources/security-scan/skills",  # 1
    # qoder-alpha 的扩展目录名是**内容哈希**（42d23c0fa380），升级即变 ⇒ 写死必然有一天
    # 变成 missing。登记 glob 让它跟着升级走。
    "qoder-alpha": "/home/gztxt/.qoder-alpha/extensions/*/skills",         # 2 · glob，命中随版本变
    "opencode": "/home/gztxt/.config/opencode/skill",             # 2 · 全是软链指回 techdocs 自研目录（find 不带 -L 会报 0，勿再据此下结论）
}

# ── v0.13.66 D1：明确排除的目录。**排除是结论，必须能被面板读到理由**——
# 军规「禁把 SKIP 当 PASS」在目录层的形态就是：不能悄悄少收，得说清少收了什么、为什么。
# 桶只有五类：marketplace 缓存 / 安装暂存 / 备份 / 快照 / 厂商同源副本。
EXCLUDED_DIRS: Dict[str, str] = {
    "/home/gztxt/.codex/.tmp/plugins": "marketplace 缓存：按需安装的来源，不是自动加载发现点（504 项）",
    "/home/gztxt/.workbuddy/connectors-marketplace": "marketplace 缓存（716 项）",
    "/home/gztxt/.codebuddy/plugins/marketplaces": "marketplace 缓存（171 项）",
    "/home/gztxt/.grok/marketplace-cache": "marketplace 缓存（72 项）",
    "/home/gztxt/.claude/plugins/marketplaces": "marketplace 缓存（41 项）",
    "/home/gztxt/.qoderwake/.tmp": "安装暂存（install.* 0700），装完即弃的中间态",
    "/home/gztxt/.qoderwake/runtime-resources": "厂商同源副本：与 resources/builtin-skills 同为 11 条但 realpath 不同，软链去重压不下去，收进来等于把 11 条报成 22 条",
    "/home/gztxt/.qoderwake/runtime-generations": "厂商同源副本：generation 槽 A/B 各 11 条，与 resources/builtin-skills 同源",
    "/home/gztxt/.qoder-alpha/extensions/*/*/_/skills": "扩展内 mcp/sdk 隔离副本（6 份 × 2 条），与同扩展的 skills/ 同源副本，但 realpath 不同",
    "/home/gztxt/.hermes/hermes-agent/optional-skills": "上游可选仓（150 项），默认不自动加载",
    "/home/gztxt/.claude/projects": "会话工程目录，非技能（946 项）",
    "/fs/1000/ftp/技术文档/snapshots": "快照（664 项）",
    "/fs/1000/ftp/技术文档/全量备份": "备份（247 项）",
    "/home/gztxt/Hermes-backup": "备份（125 项）",
    "/home/gztxt/.pi-upgrade-backup": "升级备份（2 项）",
}


# ── v0.13.71：**归档根**。发现点（`_DEFAULT_DIRS`）是「各 CLI 自己会读哪」，
# 归档根是「我们自研/已评估过的技能，其权威副本落在哪」——两者是**不同的问题**，
# 所以必须是两张表而不是一张：发现点决定「从哪读」，归档根只决定「软链的 realpath 落在哪算合法」。
#
# 为什么必须放宽（2026-10-03 实测，PT-20261002-13 的「62 条」缺口由此定案）：
# `_inside()` 按 realpath 判，而 09-25 装的三仓（hallmark / mattpocock-skills / Agent-Reach）
# 加既有的 crawl4ai，其 `SKILL.md` 都在 `技术文档/<仓>/…` 下；从各发现点软链过去时
# realpath 落在 `_DEFAULT_DIRS` 的任何一根之外 ⇒ 被当成「软链越界」拒读。
# 实测**现网真被拒 62 条**（路由级）/ 28 个不同 realpath，与 PT-13 记的数字逐条对上。
#
# **口径依据是归档军规**：「源码唯一权威副本必须落在 `/fs/1000/ftp/技术文档/<项目名>/`」。
# 承认 `技术文档/` 为权威归档根 = 这些仓的副本位置本身就是合法的，不该被自己的防越界闸门拒掉。
#
# 【为什么不直接把 `技术文档/` 整个当白名单根】那会把 `snapshots/`（664 项）、
# `全量备份/`（247 项）、`.orca-audit/`、`Hermes/`（88 项）一并放进来，
# 而这四类在 `EXCLUDED_DIRS` 里**都是已定的排除结论** ⇒ 放宽必须**逐仓枚举**，
# 不能用「整个归档根」一刀切，否则等于用一条 FAIL 换掉另一条已定的结论。
ARCHIVE_ROOTS: Dict[str, str] = {
    "/fs/1000/ftp/技术文档/mattpocock-skills": "25 条 · 09-25 用户点名评估并安装的技能仓",
    "/fs/1000/ftp/技术文档/crawl4ai": "1 条 · 自研抓页技能（c4ai），早于本表已装，软链长期被拒",
    "/fs/1000/ftp/技术文档/hallmark": "1 条 · 09-25 用户点名评估并安装的技能仓",
    "/fs/1000/ftp/技术文档/Agent-Reach": "1 条 · 09-25 用户点名评估并安装的技能仓",
}


def expand_roots(pattern: str) -> List[str]:
    """把可能含 `*` 的路径展开成真实存在的根列表。**非 glob 原样返回**（含不存在的路径）。

    为什么需要：厂商把技能目录放在**内容哈希**或**运行期槽位**里（qoder-alpha 的
    extensions/42d23c0fa380、qoderwake 的 runtime-generations/A|B），写死路径必然过期。
    一个都不命中时退回原 pattern，让 state 如实报 missing，而不是整路凭空消失。
    """
    if not pattern or "*" not in pattern:
        return [pattern]
    return sorted(glob.glob(pattern)) or [pattern]


def route_roots(route: str) -> List[str]:
    """一路对应的真实根（已展开 glob）。白名单与扫描共用它，避免两处口径。"""
    pat = SKILL_DIRS.get(route, "")
    roots = [r for r in expand_roots(pat) if r and os.path.isdir(r)]
    return roots or ([pat] if pat else [""])


def _load_dirs() -> Dict[str, str]:
    raw = os.getenv("SKILL_DIRS_JSON")
    if not raw:
        return dict(_DEFAULT_DIRS)
    try:
        d = json.loads(raw)
    except ValueError as e:
        # 不静默回退：日志里必须留下"我忽略了你的覆盖"，否则运维会以为覆盖生效了
        log.warning("SKILL_DIRS_JSON 不是合法 JSON（%s），已忽略，用内置默认目录", e)
        return dict(_DEFAULT_DIRS)
    if not (isinstance(d, dict) and d
            and all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())):
        log.warning("SKILL_DIRS_JSON 形状不对（要 {route: path} 且全字符串），已忽略")
        return dict(_DEFAULT_DIRS)
    return d


def _load_archive_roots() -> Dict[str, str]:
    """归档根，可用 `ARCHIVE_ROOTS_JSON` 覆盖（形状同 `SKILL_DIRS_JSON`）。

    【为什么要注入口】L0 hermetic 层的定义是「干净机器/CI 上可跑、无宿主依赖」。
    `ARCHIVE_ROOTS` 里的路径是**本机绝对路径**，若写死不可覆盖，则：
    ① 现有 L0 用例 `test_allowed_roots_follows_monkeypatched_dirs_and_drops_missing`
    猴补 `SKILL_DIRS` 后断言白名单**恰好**等于那个临时目录，而本机存在的 4 个归档根
    会一并混进来 ⇒ 该用例在开发机必红、在干净 runner 上必绿 = **同码两态**；
    ② 更坏的是反向：换个跑测环境，归档根在不在会改变白名单内容，测试跟着漂。
    给了注入口，L0 既能钉成空集保持与宿主无关，生产又不受影响。
    """
    raw = os.getenv("ARCHIVE_ROOTS_JSON")
    if not raw:
        return dict(ARCHIVE_ROOTS)
    try:
        d = json.loads(raw)
    except ValueError as e:
        log.warning("ARCHIVE_ROOTS_JSON 不是合法 JSON（%s），已忽略，用内置归档根", e)
        return dict(ARCHIVE_ROOTS)
    if not (isinstance(d, dict) and d
            and all(isinstance(k, str) and isinstance(v, str) for k, v in d.items())):
        log.warning("ARCHIVE_ROOTS_JSON 形状不对（要 {path: 理由} 且全字符串），已忽略")
        return dict(ARCHIVE_ROOTS)
    return d


SKILL_DIRS: Dict[str, str] = _load_dirs()
#: 生效中的归档根（可能被 `ARCHIVE_ROOTS_JSON` 覆盖；**现算**，不固化）
ARCHIVE_ROOTS_ACTIVE: Dict[str, str] = _load_archive_roots()

TDAI_ROUTE = "tdai"
SCAN_TIMEOUT_S = float(os.getenv("SKILL_SCAN_TIMEOUT", "2.0"))     # 38 个文件实测毫秒级，2s 已极宽
READ_MAX_BYTES = int(os.getenv("SKILL_READ_MAX_BYTES", str(256 * 1024)))  # 实测最大 28077B
SKIP_DIR_NAMES = frozenset({".git", "node_modules", "__pycache__", ".venv"})
_INFO_TTL_S = float(os.getenv("SKILL_STATUS_TTL", "60"))
_STATUS_CACHE: Dict[str, Any] = {"ts": 0.0, "data": None}

# ── 按路由的扫描缓存 + single-flight（2026-10-05，v0.13.79）────────────────
# 为什么加（实测，生产 :3102）：`/api/skill/list` 177ms、`/api/skill/budget` 159ms，
# 而**每一次**都重跑 4 路全盘 `os.walk(followlinks=True)`（`/status` 早有 60s TTL，
# `:212-213`，只是没被这四条路由复用）。技能文件是人工编辑的低频变更 ⇒ 短 TTL 足够。
#
# 为什么还要 single-flight（第二层，不加就等于没加）：
# `_STATUS_CACHE` 是**裸 dict 的「读-判-写」**。N 个并发首请求会**全部 miss**，
# 各自启动 20 路 os.walk 打进 `asyncio.to_thread`（`asyncio` 默认线程池
# `min(32, cpu+4)`）—— 而「打爆线程池」与「多个请求各自重扫」是同一件事的两面。
# 加按路由的锁后，第二个调用方 await 第一个的扫描结果，而不是自己开一份。
#
# 缓存放在 `_scan_async`（`:428`）而不是各端点：它是**全部 5 处调用的唯一收口**
# （:644 / :803 / :975 / :1003 / :1080）⇒ 改一处即覆盖所有路由，不会漏。
_SCAN_TTL_S = float(os.getenv("SKILL_SCAN_TTL", "30"))
_scan_cache: Dict[str, Dict[str, Any]] = {}
_scan_locks: Dict[str, "asyncio.Lock"] = {}
# ⚠ 模块级构造 asyncio.Lock 在 Python 3.10+ 是安全的（不再绑 loop），
# 本机 3.11.2 实测。若哪天降到 3.8/3.9，这两行要改成惰性创建。


def disk_routes() -> Tuple[str, ...]:
    """磁盘路名单。**每次调用时现算**，不在 import 期固化成常量——
    否则闸门/测试猴补 `SKILL_DIRS` 之后，白名单还是旧的，400 断言会假绿。
    （`kb.py` 的 `ROUTES` 是静态的因为它那三路不含可变目录；这里含。）"""
    return tuple(SKILL_DIRS)


def all_routes() -> Tuple[str, ...]:
    return disk_routes() + (TDAI_ROUTE,)


# ── frontmatter 解析（容错，见模块 docstring 事实③④）──────────────────────
_FM_RE = re.compile(r"\A---[ \t]*\r?\n(.*?)\r?\n---[ \t]*(?:\r?\n|\Z)", re.S)
_KEY_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):[ \t]*(.*)$")
_FOLDED = {"|", ">", "|-", ">-", "|+", ">+", "|-", ">-"}


def parse_frontmatter(text: str) -> Tuple[Optional[Dict[str, str]], str]:
    """返回 (frontmatter 字典 或 None, 正文)。

    刻意容错而不是抛异常：37 个文件里有 3 个根本没有 frontmatter，解析失败的正确表态是
    `fm=false` + 名字回退目录名，**不是把这条技能从清单里抹掉**（那就是门面自己造静默）。
    支持：CRLF、引号包裹的值、`|`/`>` 折叠块标量（收后续更深缩进的行）、前导 BOM。
    """
    # BOM 必须先剥：utf-8 解码会把 BOM 留成正文首字符，于是首行不再是 `---`，
    # frontmatter 就被判成「没有」—— 元数据静默丢失，正是本项目最禁的那种失败形态。
    # 本机今天 0/38 带 BOM（实测），所以这是潜伏面而非活故障；修它是为了不给未来留静默。
    text = text.lstrip("\ufeff")
    m = _FM_RE.match(text)
    if not m:
        return None, text
    block, body = m.group(1), text[m.end():]
    fm: Dict[str, str] = {}
    lines = block.split("\n")
    i = 0
    while i < len(lines):
        ln = lines[i].rstrip("\r")
        km = _KEY_RE.match(ln)
        if not km:
            i += 1
            continue
        key, val = km.group(1), km.group(2).strip()
        i += 1
        if val in _FOLDED:                      # 折叠/字面块标量：正文在后续缩进行里
            buf: List[str] = []
            while i < len(lines):
                nxt = lines[i].rstrip("\r")
                if nxt.strip() and not nxt.startswith((" ", "\t")):
                    break
                buf.append(nxt.strip())
                i += 1
            val = " ".join(x for x in buf if x).strip()
        if len(val) >= 2 and val[0] == val[-1] and val[0] in "\"'":
            val = val[1:-1]                     # 实测存在 name: "caveman" 这种写法
        fm[key] = val
    return (fm or None), body


def _read_text(path: str) -> Tuple[str, str]:
    """读文件；返回 (文本, 编码说明)。非 UTF-8 不抛、不丢，用 replace 兜住并**如实标注**——
    口径同 `sessions_store.py`：只读、errors 兜底、绝不写/删/chmod。"""
    with open(path, "rb") as f:                 # 显式关闭：`open().read()` 这种写法在 unittest 下
        raw = f.read()                          # 会刷 ResourceWarning（实测一次跑刷 45 条）；
                                                # noqa 只压得住 linter，压不住解释器
    try:
        return raw.decode("utf-8"), "utf-8"
    except UnicodeDecodeError:
        return raw.decode("utf-8", "replace"), "utf-8(replace：含非 UTF-8 字节)"


def _allowed_roots() -> List[str]:
    """白名单根的 realpath。**每次调用现算**（与 disk_routes() 同理：猴补 SKILL_DIRS 后必须跟着变）。

    白名单 = 发现点根 ∪ 归档根。前者是「从哪读」，后者是「realpath 落哪算合法」
    （见 `ARCHIVE_ROOTS` 注释：62 条拒读的来龙去脉）。

    【放宽不等于撤防】这仍是防「软链 SKILL.md 指向 `/etc/passwd`」的唯一闸门，
    所以任何不在发现点也不在归档根里的 realpath —— `/etc/passwd`、`~/.ssh`、快照与备份子树 ——
    依然拒读。`tests/test_archive_roots.py` 的负向用例是这个判据的守卫。
    """
    roots = [r for route in SKILL_DIRS for r in route_roots(route) if r and os.path.isdir(r)]
    roots += [r for r in ARCHIVE_ROOTS_ACTIVE if os.path.isdir(r)]
    return [os.path.realpath(r) for r in roots]


def _inside(rp: str, allowed: List[str]) -> bool:
    """realpath 是否落在白名单根之内。用 realpath 而不是 normpath：
    技能目录里本来就允许软链（事实②，pi 路经软链指到 techdocs），
    所以判据必须是「解完软链后还在白名单里」，而不是「字面路径看着像」。"""
    return any(rp == a or rp.startswith(a + os.sep) for a in allowed)


#: 整包摘要（C8）的上限：超限就**停止扫**并标 `truncated`，不让一个怪异目录把 /list 拖死。
PKG_MAX_FILES = int(os.getenv("SKILL_PKG_MAX_FILES", "400"))
PKG_MAX_BYTES = int(os.getenv("SKILL_PKG_MAX_BYTES", str(8 * 1024 * 1024)))


def package_digest(root: str) -> Dict[str, Any]:
    """**整包**摘要（手册 C8）：对技能目录下**所有**文件（SKILL.md + scripts/ +
    templates/ + references/ …）算一个 sha256，并给出文件数与总字节。

    为何不能只算 SKILL.md —— C8 的原话是「只算说明文件会漏脚本变化」：技能的**行为**
    等价性由整包决定。改一个 `scripts/foo.py` 而不动 SKILL.md 时，只比说明文件会判
    「没变」，于是「资产变更准入」那道闸门形同虚设。同族教训：**判据量错了对象 ⇒ 门永
    远不开**（判例 185）。

    确定性口径：按**相对路径排序**后逐行拼 `relpath\\0sha256`，再对整串算一次 sha256
    ⇒ 与 `os.walk` 的遍历顺序无关（否则同一份包会算出抖动哈希，无法当判据）。
    **不含** mtime / 权限：改时间不算改能力，改内容才算。

    任何失败都不抛：返回 `digest=""` + `error`。摘要失败不得把 /list 打挂。
    返回 `{digest, files, bytes, truncated, manifest, error}`；manifest 只给前 200 条。
    """
    out: Dict[str, Any] = {"digest": "", "files": 0, "bytes": 0, "truncated": False,
                           "manifest": [], "error": None}
    if not os.path.isdir(root):
        # 显式表态：空摘要 + error，**不**把「目录不在」伪装成「空包」（那是两种事）。
        out["error"] = "not_a_dir"
        return out
    files: List[tuple] = []
    total = 0
    hit_cap = False
    try:
        for cur, dirs, fnames in os.walk(root, followlinks=True):
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
            for fn in sorted(fnames):
                p = os.path.join(cur, fn)
                try:
                    if not os.path.isfile(p):
                        continue
                    st = os.stat(p)
                    h = hashlib.sha256()
                    with open(p, "rb") as f:
                        for chunk in iter(lambda: f.read(65536), b""):
                            h.update(chunk)
                except OSError:
                    continue
                files.append((os.path.relpath(p, root), st.st_size, h.hexdigest()))
                total += st.st_size
                if len(files) >= PKG_MAX_FILES or total >= PKG_MAX_BYTES:
                    hit_cap = True
                    break
            if hit_cap:
                break
    except OSError as e:  # noqa: BLE001
        out["error"] = "%s: %s" % (type(e).__name__, tdai_client.scrub(str(e))[:120])
    files.sort(key=lambda t: t[0])
    acc = hashlib.sha256()
    for rel, _sz, hx in files:
        acc.update(rel.encode("utf-8", "surrogateescape"))
        acc.update(b"\0")
        acc.update(hx.encode())
        acc.update(b"\n")
    out.update({"digest": acc.hexdigest(), "files": len(files), "bytes": total,
                "truncated": hit_cap,
                "manifest": [{"path": r, "bytes": s, "sha256": h} for r, s, h in files[:200]]})
    return out


def skill_state(scan: Dict[str, Any], n_entries: int) -> str:
    """一路的**四态**。ok=有条目 / empty=目录在但 0 条 / missing=目录不在 / error=配错或读不动。

    为什么要四态而不是 ok 布尔：`available=False` 把「这家没装」和「我们配错了」混成一句话，
    面板上只能显示成灰色，前者无罪后者是 bug。D1 把它们拆开，`opencode` 那种「目录在、内容空」
    才不会被误报成故障（PT-20261002-11 记 1 项、本轮 0 条就是这么混的）。
    """
    if scan.get("ok"):
        return "empty" if not n_entries else "ok"
    return {"no_dir": "missing", "no_path": "error"}.get(scan.get("reason"), "error")


def _scan_many(route: str, roots: List[str]) -> Dict[str, Any]:
    """一路多根（glob 展开）。**部分根失败不算整路失败**——只要有一根读得出条目就报 ok，
    读不出的根逐条进 error。与 `_scan_one` 里「部分文件读不了 ≠ 整路失败」同一条纪律。"""
    t0 = time.perf_counter()
    items: List[Dict[str, Any]] = []
    skipped: List[Dict[str, Any]] = []
    errs: List[str] = []
    ok_count = 0
    for one in roots:
        r = _scan_one(route, one)
        items.extend(r.get("items") or [])
        skipped.extend(r.get("skipped_outside") or [])
        if r.get("ok"):
            ok_count += 1
            if r.get("error"):
                errs.append(str(r["error"]))
        else:
            errs.append("%s: %s" % (one, r.get("error")))
    if not roots:
        # 空根列表 = 这一路一个根都没有，但**这不等于扫描失败**：
        # ok=True / 0 条 / reason=None，交给 skill_state 报成 empty。
        return {"ok": True, "items": [], "skipped_outside": [], "reason": None,
                "ms": round((time.perf_counter() - t0) * 1000, 1), "error": None}
    if not ok_count:
        reason = "no_dir" if all(e.startswith("/") for e in errs) else "read_error"
        return {"ok": False, "items": items, "skipped_outside": skipped, "reason": reason,
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": tdai_client.scrub("; ".join(errs))[:400]}
    return {"ok": True, "items": items, "skipped_outside": skipped, "reason": None,
            "ms": round((time.perf_counter() - t0) * 1000, 1),
            "error": tdai_client.scrub("; ".join(errs))[:400] or None}


def _scan_one(route: str, root) -> Dict[str, Any]:
    """扫一个技能目录（`root` 可为 str 或 list[str] —— glob 路会展开成多根）。同步。任何失败都变成结构化 error，不抛。"""
    if isinstance(root, (list, tuple)):
        return _scan_many(route, list(root))
    t0 = time.perf_counter()

    def ms() -> float:
        return round((time.perf_counter() - t0) * 1000, 1)

    # reason 三态（D1）：no_path=没配目录 / no_dir=目录不在或读不了 / read_error=目录在但走不下去。
    # 三者都报 ok=False，但**必须有机器可判的 reason**——否则 /status 只能靠解析中文错误串，
    # 那是把「事实」写进「文案」，改一次文案就改一次事实。
    if not root:
        return {"ok": False, "items": [], "ms": ms(), "reason": "no_path",
                "error": "该路未配置目录（SKILL_DIRS 里是空串）"}
    if not os.path.isdir(root):
        return {"ok": False, "items": [], "ms": ms(), "reason": "no_dir",
                "error": tdai_client.scrub(f"目录不存在或不可读：{root}")[:200]}

    items: List[Dict[str, Any]] = []
    errs: List[str] = []
    skipped: List[Dict[str, Any]] = []
    allowed = _allowed_roots()
    try:
        for cur, dirs, files in os.walk(root, followlinks=True):   # ← followlinks 是关键，见事实①
            dirs[:] = [d for d in dirs if d not in SKIP_DIR_NAMES]
            if "SKILL.md" not in files:
                continue
            p = os.path.join(cur, "SKILL.md")
            rp = os.path.realpath(p)
            # 越界判定必须在**读之前**：解析 frontmatter 需要读文件内容，一个指向
            # /etc/passwd 的软链 SKILL.md 会让门面读到白名单外的文件（/list 虽不回正文，
            # 但「读」这个动作已经发生）。拒读的条目如实列出，不静默丢——
            # 「没看见」与「看见但拒读」是两件必须能区分的事。
            if not _inside(rp, allowed):
                skipped.append({"dir": os.path.basename(cur), "path": tdai_client.scrub(p),
                                "realpath": tdai_client.scrub(rp),
                                "why": "realpath 落在技能目录白名单外，拒读（防软链越界）"})
                continue
            try:
                text, enc = _read_text(p)
                st = os.stat(p)
            except OSError as e:
                errs.append(f"{tdai_client.scrub(p)}: {type(e).__name__}")
                continue
            fm, _body = parse_frontmatter(text)
            dirname = os.path.basename(cur)
            # C8：**整包**摘要。只算 SKILL.md 会漏脚本变化 ⇒ 资产变更准入那道闸门虚设。
            pkg = package_digest(cur)
            items.append({
                "name": tdai_client.scrub((fm or {}).get("name") or dirname),
                "description": tdai_client.scrub((fm or {}).get("description") or ""),
                "route": route,
                "path": p,
                "realpath": rp,
                # path != realpath ⇒ 这一份是经软链到达的（事实②的去重依据）
                "via_symlink": p != rp,
                "fm": fm is not None,            # false 也要出现在清单里（事实③）
                "fm_keys": sorted(fm) if fm else [],
                "encoding": enc,
                "bytes": st.st_size,
                "mtime": round(st.st_mtime, 1),
                "source": "disk",
                "pkg_digest": pkg["digest"],
                "pkg_files": pkg["files"],
                "pkg_bytes": pkg["bytes"],
                "pkg_truncated": pkg["truncated"],
                "pkg_error": pkg["error"],
            })
    except OSError as e:                          # 整个根走不下去（权限/断链）
        return {"ok": False, "items": items, "ms": ms(), "skipped_outside": skipped,
                "reason": "read_error",
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:200]}

    err = None
    if errs:
        # 部分文件读不了 ≠ 整路失败：路仍报 ok，但把读不了的文件逐条说出来（不许静默少条目）
        err = tdai_client.scrub("部分文件读取失败：" + "; ".join(errs))[:400]
    return {"ok": True, "items": items, "ms": ms(), "error": err, "reason": None,
            "skipped_outside": skipped}


async def _scan_one_uncached(route: str, root) -> Dict[str, Any]:
    """磁盘 IO 放子线程 + 超时闸门。超时按"该路本次弃用"表态，不拖垮整个请求。"""
    t0 = time.perf_counter()
    try:
        return await asyncio.wait_for(asyncio.to_thread(_scan_one, route, root),
                                      timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        return {"ok": False, "items": [], "reason": "read_error",
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": f"扫描超时 {SCAN_TIMEOUT_S}s（该路本次弃用）"}
    except Exception as e:  # noqa: BLE001 — 兜底也要表态，绝不静默
        return {"ok": False, "items": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:200]}


def _invalidate_scan_cache(route: str = None) -> None:
    """清扫描缓存。**装/删技能后必须调** —— 否则新技能最多 30s 才出现在列表里。

    与 ttl 等 30s 相比，主动失效更好：用户装完技能立刻去刷新列表是常态，
    让他看到「还没出现」再去排查，是本仓反复强调的「静默形态」之一。
    调用点：`skill_install` / `skill_remove`（见下）。
    """
    if route is None:
        _scan_cache.clear()
    else:
        _scan_cache.pop(route, None)


async def _scan_async(route: str, root) -> Dict[str, Any]:
    """按路由的 TTL 缓存 + single-flight；未命中才真扫。

    ⚠ **缓存命中时 `ms` 必须重算、`cached` 置 True**（2026-10-05）：
    缓存里的 `ms` 是**上次扫描**的耗时。若原样返回，面板会显示「这一路 177ms」
    而请求实际只花了 0.1ms —— 那是**误导性诊断数据**，正是本仓「全指标绿而
    功能层已死」同族的静默形态。改成「本次命中缓存的耗时 + cached 标记」后，
    面板仍能看出「这次没重扫」，且 ms 始终代表本次请求的真实成本。
    """
    now = time.monotonic()
    hit = _scan_cache.get(route)
    if hit is not None and (now - hit["ts"]) < _SCAN_TTL_S:
        return {**hit["data"], "ms": round((time.perf_counter() - now) * 1000, 2),
                "cached": True}

    # single-flight：同路由的并发首请求，第二个起 await 第一个的结果，
    # 而不是自己再开一份 os.walk（那正是「打爆线程池」的成因）。
    lock = _scan_locks.get(route)
    if lock is None:
        lock = _scan_locks[route] = asyncio.Lock()
    async with lock:
        # 等锁期间可能已被别人扫完 ⇒ 再查一次（double-check）
        now = time.monotonic()
        hit = _scan_cache.get(route)
        if hit is not None and (now - hit["ts"]) < _SCAN_TTL_S:
            return {**hit["data"], "ms": round((time.perf_counter() - now) * 1000, 2),
                    "cached": True}
        data = await _scan_one_uncached(route, root)
        # ⚠ 只缓存成功结果：失败/超时**不写缓存**，否则一次磁盘抖动会被
        # 缓存成「这一路没有技能」并持续 30s —— 把瞬时故障固化成稳定错误。
        if data.get("ok"):
            _scan_cache[route] = {"ts": time.monotonic(), "data": data}
        return data


async def _tdai_async() -> Dict[str, Any]:
    """TDAI 技能注册表这一路。`skill_list()` 已把字段收敛过，这里只做门面侧再归一。"""
    t0 = time.perf_counter()
    try:
        r = await tdai_client.skill_list(limit=100)
    except Exception as e:  # noqa: BLE001
        return {"ok": False, "items": [],
                "ms": round((time.perf_counter() - t0) * 1000, 1),
                "error": tdai_client.scrub(f"{type(e).__name__}: {e}")[:200]}
    if not r.get("ok"):
        return {"ok": False, "items": [], "ms": r.get("ms"),
                "error": tdai_client.scrub(r.get("error") or "未知失败")[:200]}
    items = [{"name": tdai_client.scrub(h.get("name") or h.get("id") or "(无名)"),
              "description": tdai_client.scrub(h.get("description") or ""),
              "route": TDAI_ROUTE, "path": None, "realpath": None, "via_symlink": False,
              "fm": None, "fm_keys": [], "encoding": None, "bytes": None, "mtime": None,
              "id": h.get("id"), "version": h.get("version"), "status": h.get("status"),
              "owner_agent_id": h.get("owner_agent_id"), "source": "tdai"}
             for h in (r.get("items") or [])]
    return {"ok": True, "items": items, "ms": r.get("ms"), "error": None,
            "total": r.get("total")}


def _backend(name: str, r: Dict[str, Any]) -> Dict[str, Any]:
    """与 `kb.py:_backend` **同形 5 键**。⚠ count 的算法必须与各路返回的形状配套：
    本模块各路回 `items`（不回 `count`），所以这里数 `len(items)`——
    抄 `memory.py:116` 那种 `int(r.get("count") or 0)` 会让 count 恒为 0，
    于是 `engines` 空、`engine="none"`、HTTP 200 + 空结果，正是要消灭的静默形态。"""
    return {"name": name, "ok": bool(r.get("ok")), "count": len(r.get("items") or []),
            "ms": r.get("ms"),
            "error": tdai_client.scrub(r.get("error"))[:300] if r.get("error") else None}


def _dedup(items: List[Dict[str, Any]]) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]]]:
    """按 realpath 去重，返回 (去重后清单, 别名表)。见模块 docstring 事实②。"""
    by_rp: Dict[str, Dict[str, Any]] = {}
    for it in items:
        rp = it.get("realpath") or it.get("path") or f"__nopath__{it.get('name')}"
        cur = by_rp.get(rp)
        if cur is None:
            by_rp[rp] = dict(it, routes=[it["route"]])
            continue
        if it["route"] not in cur["routes"]:
            cur["routes"].append(it["route"])
        # 规范条目优先取「真实文件那一路」（path == realpath），软链路只当别名
        if it.get("path") == rp and cur.get("path") != rp:
            by_rp[rp] = dict(it, routes=cur["routes"])
    out = sorted(by_rp.values(), key=lambda x: (x.get("route") or "", x.get("name") or ""))
    aliases = [{"name": x["name"], "canonical_route": x["route"], "routes": x["routes"],
                "realpath": x.get("realpath"),
                "why": "同一 realpath 经软链在多个技能目录出现，只列一次，别名如实保留"}
               for x in out if len(x.get("routes") or []) > 1]
    return out, aliases


# ── `q` 的模糊口径（v0.13.73）───────────────────────────────────────────────────
# 【病根】`q` 此前是纯子串：搜 `crawl1ai`（数字 1）找不到 `crawl4ai`。
# 而前端搜索框在 v0.13.72 已改模糊 ⇒ **同一台机器、同一份数据，两套口径**：
# UI 里搜得到，MCP 门面（`hubmcp.py`）与资产面板三路检索（`07-asset-panel.js`）
# 走 `/api/skill/list?q=` 却搜不到。而这两条链路才是 pi / Claude 注入时真正用的那条。
#
# 【为什么不引库 / 为什么不复用 skill_relevance】那套 BM25F 是给
# `/api/skill/relevant`（相关性实验室、注入排序）用的，语义是「说一段任务描述→找回技能」，
# 与 `q` 的「我大致记得这个名字」不是一件事。而模糊匹配的口径必须与**前端那套逐字一致**，
# 否则两边又不同源。故此处手写一份与 `static/hub/01-core-boot.js` 的 `fuzzyMatch`
# **逐条对齐**的实现（容忍度、字段权重、精确快路都相同），
# 再由 `tests/test_skill_list_q_parity.py` 把两侧跑**同一批夹具**、逐条比对结果——
# 两份实现可以各写各的，但**漂移会被闸门当场抓住**，而不是等用户在 UI 与 API 之间
# 各搜出不同结果时才发现。
#
# 【为什么不把四字段拼成一个大串再算距离】拼接后编辑距离会把分隔符算进失配：
# `crawl1ai` 对 `'crawl4ai some description'` 会被后面无关的词抵掉。
# 必须**逐字段**算，再取最优（同前端 `renderPorts` 的注释）。

#: 查询长度 → 容忍的编辑距离。与前端 `_fuzzyTolerance` 逐字一致。
def _fuzzy_tolerance(q: str) -> int:
    if len(q) >= 8:
        return 2
    if len(q) >= 5:
        return 1
    return 0


def _levenshtein(a: str, b: str, limit: int) -> int:
    """带上限早退的编辑距离；超限即返回 ``limit + 1``（不做完整 DP）。"""
    if a == b:
        return 0
    if abs(len(a) - len(b)) > limit:
        return limit + 1
    prev = list(range(len(b) + 1))
    for i in range(1, len(a) + 1):
        cur = [i] + [0] * len(b)
        for j in range(1, len(b) + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        if min(cur) > limit:
            return limit + 1
        prev = cur
    return prev[len(b)]


#: 分隔符类。**必须与 JS 的 `/[\s,，、:：()（）\-_/.]+/` 逐字相同**——
#: 少一个 `，`/`、` 不会报错，只会安静地少切词 ⇒ 编辑距离算在更长的整段上 ⇒ 少召回。
#: 2026-10-03 对账时真抓到过这个漂移（JS 有、Python 无），已补 `，`、`、` 并在
#: `tests/test_skill_list_q_parity.py` 加了带中文标点的夹具钉住它。
_SEP_RE = re.compile(r"[\s,，、;:：()（）\-_/.]+")


def _fuzzy_field_score(field: str, q: str, tol: int) -> int:
    """单字段得分，与 JS `_fuzzyFieldScore` **逐条对齐**。

    对齐点是「先切段逐段比、再整串比一次」这个顺序，不是公式——
    顺序反了会在短段命中时把整串的长失配也带进来。
    """
    best = 0
    for part in (p for p in _SEP_RE.split(field) if p):
        if abs(len(part) - len(q)) > tol:
            continue
        d = _levenshtein(q, part, tol)
        if d <= tol:
            best = max(best, (tol + 1 - d) * 10)
    d2 = _levenshtein(q, field, tol)
    if d2 <= tol:
        best = max(best, (tol + 1 - d2) * 10)
    return best


#: `q` 参与匹配的字段 → 权重。**权重与前端的调用点一一对应**：
#: 技能中心传的是同一张表，故两侧分数可直接比对（不是只比「命不命中」）。
#: `realpath` 是 v0.13.74 补的：不加它就搜不到「按来源仓名找技能」
#: （`q=mattpocock` 命中 0，因为 `path` 是**软链那一侧**，`mattpocock-skills`
#: 只出现在 `realpath` 里）。
_Q_FIELDS = (("name", 3), ("description", 1), ("path", 1), ("route", 1), ("realpath", 1))


def _q_score(it: Dict[str, Any], ql: str) -> int:
    """`q` 对一条技能的得分：0 = 不命中，>0 = 命中（越大越靠前）。

    与 JS `fuzzyMatch(pairs, q)` **逐行对齐**：空查询返 1（表示「全匹配」），
    任一字段精确子串命中即返 1000（压倒一切近似），否则取「各字段近似分 × 权重」的最大值。

    【为什么返回分数而不只是 bool】v0.13.74 起 `/list` 按分数排序（精确在前、近似在后），
    与前端一致；只返 bool 就得在上层再算一遍，两处算法又要分家。
    """
    q = str(ql or "").strip().lower()
    if not q:
        return 1
    tol = _fuzzy_tolerance(q)
    best = 0
    for key, weight in _Q_FIELDS:
        t = str(it.get(key) or "").lower()
        if not t:
            continue
        if q in t:
            return 1000                      # 精确子串压倒一切近似（同前端）
        if not tol:
            continue
        s = _fuzzy_field_score(t, q, tol)
        if s > 0:
            best = max(best, s * weight)
    return best


def _match_q(it: Dict[str, Any], ql: str) -> bool:
    """`q` 的命中判定（保留布尔出口，供只需要「在不在结果里」的地方用）。"""
    return _q_score(it, ql) > 0


@router.get("/list")
@runlog.track("skill.list")
async def skill_list(request: Request,
                     q: str = Query(default="", max_length=200,
                                   description="对 name/description/path/route 做不区分大小写匹配；"
                                               "先精确子串，容忍手误则按编辑距离（口径与前端搜索框一致）"),
                     limit: int = Query(default=200, ge=1, le=500),
                     routes: Optional[str] = Query(
                         default=None,
                         description="逗号分隔；不传＝全部路。传空串＝400（与 /api/kb 同口径）")):
    """列出本机技能资产：磁盘四路（真实在用）+ TDAI 注册表一路（当前 0 行），逐路表态。

    `routes` 走白名单，未知值直接 400 —— 这不是防攻击，是防**手一抖打出静默空结果**
    （P0 的 `sources=tdaii`、P1 的 `routes=nope` 都是这个形态）。
    """
    t0 = time.monotonic()
    allowed = set(all_routes())
    if routes is None:
        want = set(allowed)
    else:
        want = {x.strip() for x in routes.split(",") if x.strip()}
        bad = sorted(want - allowed)
        if bad:
            raise HTTPException(400, f"未知 routes: {bad}；可用值 {sorted(allowed)}")
        if not want:
            raise HTTPException(400, "routes 不能为空")

    names: List[str] = []
    tasks: List[Any] = []
    for r in disk_routes():
        if r in want:
            names.append(r)
            tasks.append(_scan_async(r, route_roots(r)))
    if TDAI_ROUTE in want:
        names.append(TDAI_ROUTE)
        tasks.append(_tdai_async())

    res = await asyncio.gather(*tasks, return_exceptions=True)
    backends: List[Dict[str, Any]] = []
    disk_items: List[Dict[str, Any]] = []
    tdai_items: List[Dict[str, Any]] = []
    skipped_all: List[Dict[str, Any]] = []
    walked = 0
    for name, r in zip(names, res):
        if isinstance(r, Exception):
            r = {"ok": False, "items": [], "ms": None,
                 "error": tdai_client.scrub(f"{type(r).__name__}: {r}")[:200]}
        backends.append(_backend(name, r))
        walked += len(r.get("items") or [])
        for s in (r.get("skipped_outside") or []):
            skipped_all.append({"route": name, **s})
        if r.get("ok"):
            (tdai_items if name == TDAI_ROUTE else disk_items).extend(r.get("items") or [])

    unique, aliases = _dedup(disk_items)
    ql = (q or "").strip().lower()
    merged = [x for x in unique + tdai_items if _match_q(x, ql)]
    # v0.13.74：排序与前端口径对齐（精确在前、近似按接近度在后）。
    # **为什么现在才改**：之前只做「在不在结果里」，排序留的是 `(route, name)` 稳定序，
    # 于是 MCP 门面搜 `crawl1ai` 会把近似命中和精确命中混在一起排，用户看不出哪个更相关。
    # 同分时退回原稳定序 ⇒ 排序是**全序且可复现**的，不会因为同分而抖动。
    # 无 `q` 时**不重排**（score 恒 1，等于保持原序）—— 免得把「列出全部」的既有次序打乱。
    if ql:
        merged = sorted(merged, key=lambda x: (-_q_score(x, ql),
                                               str(x.get("route") or ""), str(x.get("name") or "")))
    merged = merged[:limit]

    engines = [b["name"] for b in backends if b["ok"] and b["count"]]
    degraded = [b["name"] for b in backends if not b["ok"]]
    notes: List[str] = []
    if degraded:
        notes.append(f"{degraded} 本次弃用，结果不完整")
    if not engines and not degraded:
        notes.append("全部可用路都返回零命中")
    if ql and not merged and not degraded:
        notes.append(f"过滤词 {q!r} 未命中任何技能（不是路挂了）")
    # 注册表空但磁盘有货 ⇒ 必须说出来，否则面板会读成"hub 没查到技能"
    tdai_b = next((b for b in backends if b["name"] == TDAI_ROUTE), None)
    if tdai_b and tdai_b["ok"] and tdai_b["count"] == 0 and len(unique):
        notes.append(f"TDAI 技能注册表 0 行，而磁盘有 {len(unique)} 条在用："
                     "权威源未裁（PT-20260924-08 T2-1），本门面只读不灌")
    if skipped_all:
        notes.append(f"{len(skipped_all)} 个 SKILL.md 因 realpath 越界被拒读（明细见 skipped_outside）")
    return {
        "items": merged,
        "count": len(merged),
        "engine": "+".join(engines) if engines else "none",
        "backends": backends,
        "degraded": degraded,
        "note": "；".join(notes) if notes else None,
        "dedup": {"walked": walked, "unique": len(unique), "aliases": aliases,
                  "why": "os.walk(followlinks=True) 会把软链目录里的技能重复数到，按 realpath 归一"},
        "skipped_outside": skipped_all,
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


def _find_disk(name: str, route: Optional[str]) -> List[Dict[str, Any]]:
    """在磁盘路上按技能名找候选（同步；调用方负责放子线程）。"""
    cands: List[Dict[str, Any]] = []
    for r, root in SKILL_DIRS.items():
        if route and r != route:
            continue
        if not root or not os.path.isdir(root):
            continue
        scan = _scan_one(r, root)
        for it in scan.get("items") or []:
            if (it.get("name") or "").lower() == name.strip().lower():
                cands.append(it)
    return cands


def _guard_inside_whitelist(path: str) -> str:
    """路径穿越闸门：realpath 必须落在白名单目录之内，否则 400。

    为什么必须有：`/api/skill/read` 会把**文件全文**回出去，比 kb 的 preview 截断危险得多；
    而技能目录里本来就允许软链（事实②），软链是可以指向任何地方的。
    判据用 realpath（不是 normpath），这样"软链指向白名单内"合法、"软链指向 /etc"被拒。
    """
    rp = os.path.realpath(path)
    if not _inside(rp, _allowed_roots()):
        raise HTTPException(400, "路径越界：realpath 不在任何技能目录内（拒绝读取）")
    return rp


@router.get("/read")
@runlog.track("skill.read")
async def skill_read(request: Request,
                     name: str = Query(min_length=1, max_length=200),
                     route: Optional[str] = Query(default=None),
                     with_body: bool = Query(default=True, description="false 则只回元信息不回正文")):
    """读一个技能的全文（脱敏后）。同名多路 ⇒ 409 报候选，**不静默挑一个**。"""
    t0 = time.monotonic()
    if route is not None and route not in SKILL_DIRS:
        raise HTTPException(400, f"未知 route: {route!r}；可用值 {sorted(SKILL_DIRS)}")
    try:
        cands = await asyncio.wait_for(asyncio.to_thread(_find_disk, name, route),
                                       timeout=SCAN_TIMEOUT_S)
    except asyncio.TimeoutError:
        raise HTTPException(504, f"扫描超时 {SCAN_TIMEOUT_S}s，请重试或缩小 route")

    uniq = {}
    for c in cands:
        uniq.setdefault(c["realpath"], c)
    if not uniq:
        raise HTTPException(404, f"未找到技能 {name!r}（route={route or '全部'}）；可用名见 /api/skill/list")
    if len(uniq) > 1:
        raise HTTPException(409, {
            "error": f"技能名 {name!r} 在多个路下指向不同文件，必须带 route 指明",
            "candidates": [{"route": c["route"], "path": c["path"], "realpath": c["realpath"],
                            "via_symlink": c["via_symlink"], "bytes": c["bytes"]}
                           for c in uniq.values()]})

    it = next(iter(uniq.values()))
    rp = _guard_inside_whitelist(it["path"])
    out: Dict[str, Any] = {k: v for k, v in it.items()}
    out["realpath"] = rp
    if with_body:
        try:
            text, enc = await asyncio.to_thread(_read_text, rp)
        except OSError as e:
            raise HTTPException(500, tdai_client.scrub(f"读取失败：{type(e).__name__}: {e}")[:200])
        raw = text.encode("utf-8")
        truncated = len(raw) > READ_MAX_BYTES
        body = raw[:READ_MAX_BYTES].decode("utf-8", "replace") if truncated else text
        out["content"] = tdai_client.scrub(body)
        out["encoding"] = enc
        out["truncated"] = truncated
        if truncated:
            # 截断必须显式说出来（bytes_total 给全量大小），否则就是又一次静默少内容
            out["bytes_total"] = len(raw)
            out["note"] = (f"正文 {len(raw)}B 超过读取上限 {READ_MAX_BYTES}B，已截断；"
                           "需要全文请直接读磁盘路径")
    out["took_ms"] = round((time.monotonic() - t0) * 1000, 1)
    return out


@router.get("/status")
async def skill_status(force: bool = Query(default=False)):
    """资产面板用：每路是否可用、条目数、frontmatter 缺失清单、软链别名、注册表空不空。

    全实测不猜；带 60s TTL 缓存（技能文件是人工编辑的，不需要每次请求都重扫磁盘）。
    返回形状是**按后端名分组的 dict**（与 `/api/kb/status` 同族），不是 `backends[]`——
    面板要按路取字段，别让它自己去数组里找。
    """
    now = time.monotonic()
    if not force and _STATUS_CACHE["data"] is not None and (now - _STATUS_CACHE["ts"]) < _INFO_TTL_S:
        return {**_STATUS_CACHE["data"], "cached": True}

    t0 = time.monotonic()
    names = list(disk_routes()) + [TDAI_ROUTE]
    tasks = [_scan_async(r, route_roots(r)) for r in disk_routes()] + [_tdai_async()]
    res = await asyncio.gather(*tasks, return_exceptions=True)

    disk_out: Dict[str, Any] = {}
    all_items: List[Dict[str, Any]] = []
    tdai_raw: Dict[str, Any] = {}
    for name, r in zip(names, res):
        if isinstance(r, Exception):
            r = {"ok": False, "items": [], "ms": None,
                 "error": tdai_client.scrub(f"{type(r).__name__}: {r}")[:200]}
        if name == TDAI_ROUTE:
            tdai_raw = r
            continue
        items = r.get("items") or []
        all_items.extend(items)
        disk_out[name] = {
            "available": bool(r.get("ok")),
            "state": skill_state(r, len(items)),
            "dir": tdai_client.scrub(SKILL_DIRS.get(name, "")),
            "roots": [tdai_client.scrub(r) for r in route_roots(name)],
            "entries": len(items),
            "fm_missing": sorted(i["name"] for i in items if not i.get("fm")),
            "via_symlink": sorted(i["name"] for i in items if i.get("via_symlink")),
            "skipped_outside": r.get("skipped_outside") or [],
            "total_bytes": sum(int(i.get("bytes") or 0) for i in items),
            "newest_mtime": max([i.get("mtime") or 0 for i in items], default=None),
            "ms": r.get("ms"),
            "error": _backend(name, r)["error"],
        }
    unique, aliases = _dedup(all_items)
    tdai_items = tdai_raw.get("items") or []
    data = {
        "disk": disk_out,
        "dedup": {"walked": len(all_items), "unique": len(unique), "aliases": aliases},
        "excluded": dict(EXCLUDED_DIRS),
        "tdai": {
            "available": bool(tdai_raw.get("ok")),
            "rows": len(tdai_items),
            "total": tdai_raw.get("total"),
            "ms": tdai_raw.get("ms"),
            "error": _backend(TDAI_ROUTE, tdai_raw)["error"],
            "backend": tdai_client.backend_status(),
            "why": ("注册表 HTTP 200 但 0 行：技能实际以磁盘 SKILL.md 形态在用，"
                    "hub 不做 ETL（权威源未裁，PT-20260924-08 T2-1）"
                    if tdai_raw.get("ok") and not tdai_items else
                    "注册表有数据" if tdai_items else "注册表这一路本次不可用，原因见 error"),
        },
        "gap": {
            "disk_unique": len(unique),
            "tdai_rows": len(tdai_items),
            "note": ("磁盘 %d 条在用 vs 注册表 %d 行；两者未打通是**已知待裁项**，不是故障。"
                     "本门面只读、不写任何一侧，故不构成第二权威副本。"
                     % (len(unique), len(tdai_items))),
        },
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
        "cached": False,
    }
    _STATUS_CACHE.update(ts=time.monotonic(), data=data)
    return data


# ── v0.13.26 批2：技能安装管理（软链双发现点）+ 预算化清单 ───────────────
#
# 设计依据（56 号文档实测）：各 CLI 的技能发现点互不相通（claude 只读 ~/.claude/skills，
# codex 等共享 ~/.agents/skills，workbuddy 读 ~/.workbuddy/skills）。让一个技能对多个
# agent 可见的唯一手段＝把**权威副本目录**软链到各发现点——本机已有 39 条这样的软链。
#
# 两条安全铁律：
# 1. **只建/删软链，不复制文件**：同源唯一副本（09-19「唯一权威副本」主权原则），
#    重复副本必然漂移；
# 2. **remove 只删软链**（islink 才动手）：真目录是权威本体，从 HTTP 端点删本体
#    属不可逆破坏，一律 409 拒绝（无主副本处置另行裁定，不在本端点顺手做）。
# 鉴权：POST/DELETE 自动过 writeauth 全局中间件（x-hub-token）；审计入 asset_audit
# （action=bind/unbind，asset_type=skill）。

_NAME_RE = re.compile(r"\A[A-Za-z0-9][A-Za-z0-9_.-]{0,80}\Z")


class InstallRequest(BaseModel):
    name: str
    from_route: str                      # 源发现点（软链指向它的真实目录）
    targets: List[str]                    # 目标发现点列表


@router.post("/install")
async def skill_install(req: InstallRequest, request: Request):
    """把 from_route 发现点的技能软链到 targets 各发现点（幂等：同 realpath 已存在则 no-op）。"""
    if not _NAME_RE.match(req.name):
        raise HTTPException(400, f"技能名不合法（只允许字母数字._-，且不以 . 开头）：{req.name!r}")
    if req.from_route not in SKILL_DIRS:
        raise HTTPException(400, f"未知 from_route: {req.from_route!r}；可用 {sorted(SKILL_DIRS)}")
    bad = [t for t in req.targets if t not in SKILL_DIRS]
    if bad:
        raise HTTPException(400, f"未知目标发现点: {bad}；可用 {sorted(SKILL_DIRS)}")
    if req.from_route in req.targets:
        raise HTTPException(400, "from_route 不能同时是目标（自链无意义）")

    src_root = SKILL_DIRS[req.from_route]
    src = os.path.join(src_root, req.name)
    if not os.path.isdir(src):
        raise HTTPException(404, f"源技能不存在：{req.from_route}/{req.name}")
    if not os.path.isfile(os.path.join(src, "SKILL.md")):
        raise HTTPException(400, f"{req.from_route}/{req.name} 没有 SKILL.md，不是可安装技能")
    src_rp = os.path.realpath(src)

    created: List[str] = []
    existed: List[str] = []
    for t in req.targets:
        dst = os.path.join(SKILL_DIRS[t], req.name)
        if os.path.lexists(dst):
            if os.path.realpath(dst) == src_rp:
                existed.append(t)          # 幂等：同源软链已就位
                continue
            raise HTTPException(409, f"目标已存在且指向不同的副本：{t}/{req.name} → "
                                     f"{os.path.realpath(dst)}（拒绝覆盖，先人工处置）")
        os.symlink(src_rp, dst)
        created.append(t)
    db.log_asset_event("skill", req.name, "bind", writeauth.actor_of(request), {
        "from_route": req.from_route, "created": created, "existed": existed,
        "src_realpath": src_rp})
    _invalidate_scan_cache(req.from_route)   # 新装的要立刻可见，不等 30s TTL
    return {"status": "installed", "name": req.name, "created": created,
            "existed": existed, "src": src_rp}


@router.delete("/remove")
async def skill_remove(name: str = Query(min_length=1, max_length=100),
                       route: str = Query(min_length=1, max_length=40),
                       request: Request = None):
    """删指定发现点上的**软链**（islink 才删）。真目录=权威本体，409 拒绝。"""
    if route not in SKILL_DIRS:
        raise HTTPException(400, f"未知 route: {route!r}；可用 {sorted(SKILL_DIRS)}")
    if not _NAME_RE.match(name):
        raise HTTPException(400, f"技能名不合法：{name!r}")
    target = os.path.join(SKILL_DIRS[route], name)
    if not os.path.lexists(target):
        raise HTTPException(404, f"{route}/{name} 不存在")
    if not os.path.islink(target):
        raise HTTPException(409, f"{route}/{name} 是真实目录（权威副本），不从本端点删除；"
                                 f"只删软链（软链的删除不伤本体）")
    os.unlink(target)
    db.log_asset_event("skill", name, "unbind", writeauth.actor_of(request), {
        "route": route, "realpath": os.path.realpath(target)})
    _invalidate_scan_cache(route)   # 删掉的要立刻消失，不等 30s TTL
    return {"status": "removed", "name": name, "route": route}


# ── 相关性检索（D2）──────────────────────────────────────────────────
#: jev 精排在请求路径上的**上限秒数**。BM25 已经能出结果，jev 只是增强，
#: 而本端点会被注入通道调用（那条链路有自己的预算），所以宁可超时降级也不能拖住它。
JEV_RERANK_TIMEOUT_S = 5.0


def _rel_row(x: Dict[str, Any]) -> Dict[str, Any]:
    """把 `skill_relevance` 的打分结果整成对外行。`matched`/`matched_in` 是给
    「相关性实验室」看命中依据用的——只给分数就答不出「为什么这条排第一」。"""
    it = x["item"]
    return {"name": it.get("name"), "description": it.get("description"),
            "route": it.get("route"), "routes": it.get("routes"),
            "bm25": x["score"], "matched": x["matched"], "matched_in": x["matched_in"],
            "jev": None, "tokens_est": 0}


@router.get("/zombies")
async def skill_zombies(days: int = Query(default=7, ge=1, le=365)):
    """近 N 天零调用的技能榜。

    **置信度封顶 `medium`**：本批只接了 hub 通道（`profile_events` 里
    `source='rest'` 的 `skill.read` / `skill.inject`），各家 agent 直接读自己技能目录的
    旁路统计**未实现**。设计书 §7 的口径是「两源皆零 → high」，而第二源不存在时那条口径
    不成立——照抄会得到一个看起来很确定、实际是仪表盘盲区自欺的榜。
    `direct_source` 与 `counted` 两个字段就是让这个盲区在返回值里可断言。
    """
    routes = disk_routes()
    scans = await asyncio.gather(*[_scan_async(r, route_roots(r)) for r in routes])
    items: List[Dict[str, Any]] = []
    for s in scans:
        items.extend(s.get("items") or [])
    unique, _aliases = _dedup(items)
    return skill_usage.snapshot(unique, days)


@router.get("/relevant")
@runlog.track("skill.relevant")
async def skill_relevant(request: Request,
                         q: str = Query(min_length=1, max_length=2000),
                         n: int = Query(default=5, ge=1, le=20),
                         max_tokens: int = Query(default=0, ge=0, le=8000),
                         rerank: bool = Query(default=True)):
    """按一段自然语言任务描述，从**真实在用**的技能里排最相关的 n 条。

    **返回值刻意把 `bm25` 与 `jev` 分开报，不合成一个数字。**
    - `bm25.items[]` 永远有值：同步内存打分，不依赖任何外部服务、不联网；
    - `jev` 为 null = 没调；为 `{"ok": false, ...}` = 调了但用不了，`why` 说明原因。

    **jev 只做增强，不改写排序。** 依据 `typesafe-ai` 技能的实测：choice 在中文上
    置信常 1.00，而 score 用于主观刻度时置信会掉到 0.3 左右。「技能对某个任务的相关
    程度」正是主观刻度，用一个 0.3 置信的分数去改写排序不成立。所以本端点把 jev 的
    分数与置信度逐行回传，**排序权威仍是 BM25**，等拿到可比数据再决定是否换权。
    """
    t0 = time.monotonic()
    routes = disk_routes()
    scans = await asyncio.gather(*[_scan_async(r, route_roots(r)) for r in routes])
    items: List[Dict[str, Any]] = []
    backends: List[Dict[str, Any]] = []
    for r, s in zip(routes, scans):
        items.extend(s.get("items") or [])
        backends.append(_backend(r, s))
    unique, _aliases = _dedup(items)

    res = skill_relevance.rank(q, unique, n=n)
    rows = [_rel_row(x) for x in res["items"]]
    for r in rows:
        r["tokens_est"] = int(len("%s: %s" % (r["name"], r["description"] or "")) / _CHARS_PER_TOKEN) + 1

    jev_block: Optional[Dict[str, Any]] = None
    if rerank and rows:
        try:
            async with asyncio.timeout(JEV_RERANK_TIMEOUT_S):
                scored = await jev_client.score_candidates(q, [x["item"] for x in res["items"]])
            for r in rows:
                hit = scored.get(r["name"] or "")
                if hit:
                    r["jev"] = hit
            jev_block = dict(jev_client.status(), ok=True)
        except jev_client.JevUnavailable as e:
            jev_block = dict(jev_client.status(), ok=False, why=str(e))
        except asyncio.TimeoutError:
            jev_block = dict(jev_client.status(), ok=False,
                             why="超过 %.1fs 上限，已放弃精排（BM25 结果仍完整）" % JEV_RERANK_TIMEOUT_S)
    elif not rerank:
        jev_block = dict(jev_client.status(), ok=False, why="调用方显式关闭精排（rerank=false）")

    truncated = 0
    if max_tokens and rows:
        used, keep = 0, []
        for r in rows:
            if used + r["tokens_est"] <= max_tokens:
                keep.append(r)
                used += r["tokens_est"]
            else:
                truncated += 1
        rows, total_est = keep, used
    else:
        total_est = sum(r["tokens_est"] for r in rows)

    return {
        "q": q, "n": n, "total": len(unique),
        "bm25": {"items": rows,
                 "query_tokens": res["query_tokens"],
                 "short_query": res["short_query"],
                 "total_hits": res.get("total_hits"),
                 "weights": res.get("weights"),
                 "why": res.get("why")},
        "jev": jev_block,
        "backends": backends,
        "max_tokens": max_tokens or None,
        "tokens_est": total_est,
        "truncated": truncated,
        "took_ms": round((time.monotonic() - t0) * 1000, 1),
    }


#: 预算化清单的 token 估算：CJK 与英文混排取字符/2.5 的粗估。这个数只影响装填顺序，
#: 不影响正确性（超估=保守装填，宁少勿膨胀）。
_CHARS_PER_TOKEN = 2.5


@router.get("/budget")
async def skill_budget(max_tokens: int = Query(default=800, ge=50, le=8000)):
    """按 token 预算裁剪的技能清单（批5 注入通道 hub-facade 的数据源）。

    56 号文档结论：把全部技能描述注入上下文会吃掉可观预算（39 技能 desc 全量约数
    千 token）；agent 侧需要的是「花 N 个 token 知道有哪些技能」。策略：
    - 全条目（name+desc）贪心装填到预算的 70%；
    - 装不下的降级为 name-only（一行一个名字，最便宜）；
    - name-only 也装不下则截断，返回 truncated 与 total——如实说「被裁了」。
    """
    t0 = time.monotonic()
    tasks = [_scan_async(r, route_roots(r)) for r in disk_routes()]
    scans = await asyncio.gather(*tasks)
    items: List[Dict[str, Any]] = []
    for s in scans:
        items.extend(s.get("items") or [])
    unique, _aliases = _dedup(items)

    def est(s: str) -> int:
        return int(len(s) / _CHARS_PER_TOKEN) + 1

    full_cap = int(max_tokens * 0.7)
    full, dropped, used = [], [], 0
    for it in sorted(unique, key=lambda x: (x.get("name") or "").lower()):
        name = str(it.get("name") or "")
        desc = str(it.get("description") or "").strip()
        c = est(f"{name}: {desc}")
        if used + c <= full_cap:
            full.append({"name": name, "description": desc, "routes": it.get("routes"),
                         "est_tokens": c})
            used += c
        else:
            dropped.append(name)
    name_only: List[str] = []
    for n in dropped:
        c = est(n)
        if used + c <= max_tokens:
            name_only.append(n)
            used += c
        else:
            break
    return {"budget": max_tokens, "used_est": used, "full": full,
            "name_only": name_only,
            "truncated": len(dropped) - len(name_only),
            "total": len(unique), "took_ms": round((time.monotonic() - t0) * 1000, 1)}
