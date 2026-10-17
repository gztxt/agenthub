"""外部资产准入六阶段（P2 C7）：把「下载了就能用」改成「检查过才进得来」。

## 为什么需要它

手册 15 章：外部 Skill 会把外部维护者、代码依赖和内容来源带进运行环境；
Agent 会照着自然语言说明去调文件、命令、工具和外部服务，
**一段普通说明也可能间接引导高权限操作**。所以外部内容不能下载后直接进生产。

本机现状：`skill.package_digest()` 能证明「内容是什么、有没有被换过」，
`skill_assets` 的状态机能声明「它处于哪一态」——
但**中间那四道检查根本不存在**：来源是谁、脚本里有没有外连、在隔离环境里跑过没有、
谁批的。没有这四份记录，`published` 只是一个自称。

## 六阶段（手册原文的表，逐条对应）

| # | 阶段 | 主要工作 | 形成的记录 |
|---|---|---|---|
| 1 | `source` 来源核实 | 原仓库/发布地址、发布者、维护状态、许可证、获取时间 | 来源与责任信息 |
| 2 | `integrity` 完整性校验 | 算候选包摘要；来源有签名则验签 | 摘要与签名验证结果 |
| 3 | `static` 静态检查 | 说明/脚本/依赖/配置里的异常命令、敏感路径、外部地址、运行时下载、不当指令 | 检查工具、策略版本、问题清单 |
| 4 | `isolated` 隔离验证 | 无生产数据、无长期凭据的环境里跑样例任务，限制文件/进程/工具/网络 | 运行行为与外部访问记录 |
| 5 | `review` 人工评审 | 使用团队确认业务必要性、输入输出、维护责任、必要权限 | 评审结论与例外事项 |
| 6 | `publish` 内部发布 | 复制到内部 Registry，固定版本与摘要 | 内部版本及完整准入记录 |

## 四条硬口径（都有闸门钉着）

1. **阶段只能顺序推进，不许跳级**：`tested` 掉进 `published` 那种事在本模块不可能发生
   —— 每一阶段的前置是「上一阶段已 `pass`」，而不是「调用方说做过了」。
2. **`fail` 之后必须回 `source` 重来**：不能带着已知问题往下走，
   也不能"修一半"继续 —— 与 `skill_assets` 的 `revise → draft` 同一立场。
3. **跳过 ≠ 通过**：还没跑的阶段是 `pending`，不是 `pass`。
   `overall()` 只在六项全 `pass` 时给 `admitted`；任何一项 `pending` ⇒ `incomplete`，
   **不得**被读成「没问题」（本仓反复禁的「空即无」）。
4. **`waived`（豁免）是显式记录，不进指纹、不做默认**：手册允许人工评审给例外事项，
   但例外必须**留档带理由**，不能靠"这次就不做了"。

## 与 `skill_assets` 状态机的关系（刻意不合并）

`skill_assets` 的四段（`draft→tested→reviewed→published`）是**资产生命周期**：
它在回答「这个资产现在能不能被依赖」。

本模块的六阶段是**外部内容的准入检查链**：它在回答「这个外来包被检查到什么程度」。

两者的状态词看起来像（都有 `review`/`publish`），**语义不同**：
一个生命周期状态可以从 `published` 回 `draft`，而一次准入检查**永远不回退**——
失败就是那一次检查失败，重来是新的一次记录。

⇒ 本模块**只产出记录**，不改 `skill_assets` 的任何行。合并会让「已发布但没检查过」
与「检查过但没发布」这两个完全不同的处境共用一套词。
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

#: 六阶段，**顺序即约束**。改顺序等于改语义，闸门按这个元组校验。
STAGES: Tuple[str, ...] = ("source", "integrity", "static", "isolated", "review", "publish")

#: 单阶段的状态。`pending` 是「还没跑」，**不是**通过与失败之间的中间态。
STAGE_STATES: Tuple[str, ...] = ("pending", "pass", "fail", "waived")

#: 整单结论。`incomplete` 专指「有阶段还没跑」—— 与 `rejected`（跑过且失败）分开。
VERDICTS: Tuple[str, ...] = ("incomplete", "admitted", "rejected")

SCHEMA = "admission/1"


def next_stage(stages: Dict[str, Dict[str, Any]]) -> Optional[str]:
    """当前该做哪一阶段（纯函数）。全 `pass`/`waived` ⇒ `None`。

    规则：
      · 从头扫，**第一个非 pass/waived 的阶段**就是下一站；
      · 若那一站是 `fail` ⇒ 也返回它（**就地重做**，不是往下跳）；
      · 但 `fail` 在非首阶段时，语义上是「带着问题往下走」⇒ 必须回 `source`。
        本函数按**手册口径**返回 `source`，让重做从来源重新核实开始。

    ⚠ 「返回 stage」不等于「允许推进」：推进的判据是 `can_advance()`。
    """
    for i, name in enumerate(STAGES):
        st = (stages.get(name) or {}).get("state", "pending")
        if st in ("pass", "waived"):
            continue
        if st == "fail" and i > 0:
            return "source"          # 失败必须回炉，不许修一半继续
        return name
    return None


def can_advance(stages: Dict[str, Dict[str, Any]], to: str) -> Optional[str]:
    """能否把 `to` 记为 `pass`。返回拒绝理由（None = 允许）。纯函数。

    这是本模块**唯一**的推进闸门 —— 所有写入路径都必须过它。
    """
    if to not in STAGES:
        return "未知阶段 %r（可用 %s）" % (to, " / ".join(STAGES))
    idx = STAGES.index(to)
    for prev in STAGES[:idx]:
        st = (stages.get(prev) or {}).get("state", "pending")
        if st not in ("pass", "waived"):
            return ("不能推进到 %s：前置 %s 当前为 %s（六阶段必须顺序通过，"
                    "跳级与「修一半继续」都不允许）" % (to, prev, st))
    cur = (stages.get(to) or {}).get("state", "pending")
    if cur == "pass":
        return "阶段 %s 已是 pass：重复记为通过不得当成功" % to
    return None


def overall(stages: Dict[str, Dict[str, Any]]) -> str:
    """整单结论（纯函数）。三种，**不可合并**：

    - `rejected`  ：任一阶段 `fail` —— **跑过且不合格**；
    - `incomplete`：任一阶段仍 `pending` —— **还没跑**（不是"没问题"）；
    - `admitted`  ：六项全 `pass`（或显式 `waived`）。
    """
    states = [(stages.get(n) or {}).get("state", "pending") for n in STAGES]
    if "fail" in states:
        return "rejected"
    if any(s == "pending" for s in states):
        return "incomplete"
    return "admitted"


def summary(stages: Dict[str, Dict[str, Any]]) -> Dict[str, Any]:
    """给回包用的紧凑视图。**把「还差几步」写出来**，不让人自己数。"""
    done = [n for n in STAGES if (stages.get(n) or {}).get("state") in ("pass", "waived")]
    pending = [n for n in STAGES if (stages.get(n) or {}).get("state", "pending") == "pending"]
    failed = [n for n in STAGES if (stages.get(n) or {}).get("state") == "fail"]
    return {
        "schema": SCHEMA,
        "verdict": overall(stages),
        "stages": list(STAGES),
        "passed": done,
        "pending": pending,
        "failed": failed,
        "progress": "%d/%d" % (len(done), len(STAGES)),
        "next": next_stage(stages),
        # 显式标注：「没跑」不是「通过」
        "pending_is_not_pass": True,
    }


# ── 静态检查（阶段 3）的纯判据 ─────────────────────────────────────────
#
# 为什么单独抽出来：它是六阶段里**唯一能纯函数化**的检查（不联网、不执行代码）。
# 隔离验证（阶段 4）要真跑沙箱、人工评审（阶段 5）要人签字，都不是本模块能替的。

#: 说明/脚本里出现即**拒绝**的形态。每条都对应一种「说明引导高权限操作」。
_HARD_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\brm\s+-rf\s+/(?:\s|$|\*)", "无条件删除根目录"),
    (r"\bmkfs(?:\.\w+)?\b", "格式化文件系统"),
    (r"\bdd\s+if=.*\bof=/dev/", "直接写裸设备"),
    (r":\(\)\s*\{\s*:\|\s*:\s*&\s*\}\s*;\s*:", "fork 炸弹"),
    (r"\bchmod\s+777\s+/(?:\s|$)", "对根目录放开全部权限"),
    (r"\bcurl[^\n|]*\|\s*(?:ba)?sh\b", "运行时下载并直接执行远程脚本"),
    (r"\bwget[^\n|]*\|\s*(?:ba)?sh\b", "运行时下载并直接执行远程脚本"),
    (r"(?:/etc/(?:passwd|shadow|sudoers)|\.ssh/id_[a-z0-9]+|\.aws/credentials)",
     "触碰凭据/系统账户文件"),
    (r"\b(?:eval|exec)\s*\(\s*(?:base64|b64decode)", "解码后执行（隐藏载荷的经典形态）"),
    (r"\b[A-Za-z0-9+/]{200,}={0,2}\b", "超长 base64 串（疑似隐藏载荷）"),
)

#: 出现即**警告**（不拒）的形态：合法用途常见，但值得看一眼。
_SOFT_PATTERNS: Tuple[Tuple[str, str], ...] = (
    (r"\bsudo\b", "使用 sudo"),
    (r"\bchmod\s+\+x\b", "给文件加可执行位"),
    (r"\bpip\s+install\b(?!.*(?:==|-r\s))", "未锁定版本的 pip 安装"),
    (r"\bnpm\s+install\b(?!.*@[\d.]+)", "未锁定版本的 npm 安装"),
    (r"/dev/tcp/", "bash 伪终端外连"),
    (r"\b(?:nc|ncat|netcat)\s+-[a-z]*[el]", "netcat 监听/反弹"),
)


def static_scan(text: str, *, source_url: str = "",
                allow_private_urls: bool = False) -> Dict[str, Any]:
    """阶段 3 的纯静态检查。**零网络、零执行**，只看文本。

    回包：`{allowed, hard, soft, urls, policy}`。
    - `allowed=False` 当且仅当命中 `hard` 或被 `outbound` 判为出站拒绝；
    - `soft` 只提示，不拦（拦了会把正常技能全毙掉）；
    - `urls` 列出文中的外部地址及各自判定（`outbound.check_url` 同一份判据）。

    口径：**静态检查只认已知形态，不能穷尽自然语言指令**（手册原话）。
    所以 `allowed=True` 的含义是「没命中已知坏形态」，**不是**「安全」。
    这句话必须写在回包里，否则下游会把静态检查当安全结论用。
    """
    import re
    s = text if isinstance(text, str) else ""
    hard: List[Dict[str, str]] = []
    soft: List[Dict[str, str]] = []
    for pat, why in _HARD_PATTERNS:
        m = re.search(pat, s)
        if m:
            hard.append({"pattern": pat, "reason": why,
                         "sample": _snippet(s, m.start(), m.end())})
    for pat, why in _SOFT_PATTERNS:
        m = re.search(pat, s)
        if m:
            soft.append({"pattern": pat, "reason": why,
                         "sample": _snippet(s, m.start(), m.end())})

    urls: List[Dict[str, Any]] = []
    try:
        import outbound
    except Exception:  # noqa: BLE001
        outbound = None
    for u in sorted(set(re.findall(r"https?://[^\s)\]\"'<>]+", s)))[:50]:
        row: Dict[str, Any] = {"url": u, "blocked": None}
        if outbound is not None:
            try:
                row["blocked"] = outbound.check_url(
                    u, allow_private=allow_private_urls)
            except Exception as e:  # noqa: BLE001
                row["blocked"] = "判定失败：%s" % e
        urls.append(row)

    blocked_urls = [r for r in urls if r["blocked"]]
    if source_url and outbound is not None:
        try:
            b = outbound.check_url(source_url, require_https=True)
        except Exception as e:  # noqa: BLE001
            b = "判定失败：%s" % e
        if b:
            hard.append({"pattern": "<source_url>", "reason": "来源地址不合规",
                         "sample": str(source_url)[:120], "detail": b})

    return {
        "allowed": not hard and not blocked_urls,
        "hard": hard,
        "soft": soft,
        "urls": urls,
        # 策略版本进记录：判据改了，旧记录才解释得清（手册要求记「策略版本」）
        "policy": {"hard": len(_HARD_PATTERNS), "soft": len(_SOFT_PATTERNS),
                   "url_judge": "outbound.check_url"},
        "note": ("静态检查只认已知形态，不能穷尽自然语言指令；"
                 "allowed=true 表示「未命中已知坏形态」，不是「安全」"),
    }


def _snippet(s: str, a: int, b: int, pad: int = 20) -> str:
    """命中处上下文（**先脱敏**：样例会进审计库，那是会外流的正文）。"""
    lo, hi = max(0, a - pad), min(len(s), b + pad)
    frag = s[lo:hi].replace("\n", " ")
    try:
        from sessions_export import redact_text
        frag = redact_text(frag)[0]
    except Exception:  # noqa: BLE001
        pass
    return frag[:160]


# ── 存储：一条候选包 = 一份准入单 ────────────────────────────────────────
#
# 为什么不用 `asset_audit` 存：那是 **append-only 的事件流**（"谁改了哪个资产"），
# 准入单是**有状态的实体**（当前走到第几阶段），两者的更新语义相反
# —— 审计不能 UPDATE，准入单必须能 UPDATE。混用就得让审计表可改，那是自毁。

_SCHEMA = """
CREATE TABLE IF NOT EXISTS admission (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    slug        TEXT NOT NULL,
    version     TEXT NOT NULL,
    source_url  TEXT,
    -- 提交时的候选包摘要；**准入全程锁这一个值**（中间被换包必须能看出来）
    digest      TEXT NOT NULL,
    stages      TEXT NOT NULL DEFAULT '{}',
    note        TEXT,
    by_whom     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL,
    UNIQUE(slug, version)
);
"""


def ensure_schema() -> None:
    import db
    db.execute_script(_SCHEMA)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def _load(row: Dict[str, Any]) -> Dict[str, Any]:
    """DB 行 → 回包。

    ⚠ **命名冲突坑（第一版就踩）**：`summary()` 里也有一个 `stages` 键
    （= 阶段名元组，给前端按序渲染用），而记录实体本身的 `stages` 是
    `{阶段: 结果}` 字典。`rec.update(summary(...))` 会把字典**覆盖成元组**，
    下一次 `dict(cur["stages"])` 直接 ValueError。
    ⇒ 实体字段改名 `stage_records`，`stages` 只保留「阶段名单」这一个含义。
    """
    import json
    rec = dict(row)
    try:
        rec["stage_records"] = json.loads(rec.get("stages") or "{}")
    except Exception:  # noqa: BLE001
        # 坏 JSON 不能当空 ⇒ 那会把「记录损坏」读成「还没开始」
        rec["stage_records"] = {}
        rec["stages_corrupt"] = True
    rec.pop("stages", None)           # 原始列只作存储，不对外
    rec.update(summary(rec["stage_records"]))
    return rec


def open_ticket(slug: str, version: str, source_url: str, digest: str, *,
                by: str = "system", note: Optional[str] = None) -> Dict[str, Any]:
    """开一张准入单（阶段 1 的记录落地）。同一 (slug, version) 不得重复开。"""
    import json
    ensure_schema()
    import db
    slug, version = (slug or "").strip(), (version or "").strip()
    if not slug:
        raise ValueError("slug 不能为空")
    if not version:
        raise ValueError("version 不能为空（准入记录必须绑定确定版本）")
    if not (digest or "").strip():
        # 没有摘要的准入单毫无意义：事后无法判断"检查的是不是这一个包"
        raise ValueError("digest 不能为空（准入必须绑定候选包内容摘要）")
    ex = db.query("SELECT * FROM admission WHERE slug=? AND version=?", (slug, version))
    if ex:
        raise ValueError("该版本已有准入单：%s@%s（重复开单会让两条记录各说各话）"
                         % (slug, version))
    now = _now()
    db.execute(
        "INSERT INTO admission(slug,version,source_url,digest,stages,note,by_whom,"
        "created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?)",
        (slug, version, (source_url or "").strip(), digest.strip(), json.dumps({}),
         note, by, now, now))
    return get_ticket(slug, version)  # type: ignore[return-value]


def get_ticket(slug: str, version: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    import db
    rows = db.query("SELECT * FROM admission WHERE slug=? AND version=?", (slug, version))
    return _load(rows[0]) if rows else None


def list_tickets(limit: int = 200) -> List[Dict[str, Any]]:
    ensure_schema()
    import db
    return [_load(r) for r in db.query(
        "SELECT * FROM admission ORDER BY updated_at DESC LIMIT ?", (int(limit),))]


def record_stage(slug: str, version: str, stage: str, state: str, *,
                 by: str = "system", note: Optional[str] = None,
                 evidence: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """记一个阶段的结果。**推进必须过 `can_advance()`** —— 这是唯一的写入口。

    `state` 只能是 `pass`/`fail`/`waived`：`pending` 是「还没记过」，
    把它写成记录会让「没跑」和「跑了但没结果」不可分辨。
    """
    import json
    cur = get_ticket(slug, version)
    if not cur:
        raise KeyError("准入单不存在：%s@%s" % (slug, version))
    state = (state or "").strip()
    if state == "pending":
        raise ValueError("不能用记录把阶段写成 pending（pending 表示从未记录）")
    if state not in ("pass", "fail", "waived"):
        raise ValueError("未知阶段结果 %r（可用 pass / fail / waived）" % state)
    if state == "waived" and not (note or "").strip():
        # 豁免必须留理由：没有理由的例外 = 没有例外，只有漏检
        raise ValueError("waived 必须在 note 里写明理由（无理由豁免等于漏检）")

    stages = dict(cur["stage_records"])
    if state in ("pass", "waived"):
        why = can_advance(stages, stage)
        if why:
            raise ValueError(why)
    else:  # fail
        if stage not in STAGES:
            raise ValueError("未知阶段 %r" % stage)
        if (stages.get(stage) or {}).get("state", "pending") == "pending" \
                and stage != next_stage(stages):
            raise ValueError("只能对当前阶段 %s 记失败（越级记失败会让记录对不上实际进程）"
                             % next_stage(stages))

    stages[stage] = {"state": state, "by": by, "at": _now(),
                     "note": note, "evidence": evidence or {}}
    import db
    db.execute("UPDATE admission SET stages=?, updated_at=?, by_whom=? "
               "WHERE slug=? AND version=?",
               (json.dumps(stages, ensure_ascii=False, sort_keys=True),
                _now(), by, slug, version))
    return get_ticket(slug, version)  # type: ignore[return-value]


# ── 端点 ────────────────────────────────────────────────────────────────
# 路由形状从简：`/api/admission/tickets` + `/api/admission/tickets/{slug}/{version}`
# —— **静态段在前、动态段在最后**，不会再出现 A1 那次「被 {agent_id} 吃掉」。
# （路由顺序坑的判例：`agent-knowledge/198`。这里按它的结论预先避让。）

from fastapi import APIRouter, HTTPException, Query  # noqa: E402
from pydantic import BaseModel  # noqa: E402

router = APIRouter()


class TicketIn(BaseModel):
    slug: str
    version: str
    source_url: str = ""
    digest: str
    note: Optional[str] = None


class StageIn(BaseModel):
    stage: str
    state: str
    note: Optional[str] = None
    evidence: Optional[Dict[str, Any]] = None


class ScanIn(BaseModel):
    text: str
    source_url: str = ""
    allow_private_urls: bool = False


@router.get("/api/admission/tickets")
async def list_admission_tickets(limit: int = Query(default=200, le=500)):
    return {"tickets": list_tickets(limit), "stages": list(STAGES),
            "stage_states": list(STAGE_STATES), "verdicts": list(VERDICTS)}


@router.post("/api/admission/tickets")
async def open_admission_ticket(body: TicketIn):
    try:
        return open_ticket(body.slug, body.version, body.source_url, body.digest,
                           by="api", note=body.note)
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/admission/scan")
async def static_scan_endpoint(body: ScanIn):
    """阶段 3 的**纯检查**（零网络、零执行）。不落库 —— 是否采用由调用方决定。"""
    return static_scan(body.text, source_url=body.source_url,
                       allow_private_urls=body.allow_private_urls)


@router.get("/api/admission/tickets/{slug}/{version}")
async def get_admission_ticket(slug: str, version: str):
    t = get_ticket(slug, version)
    if not t:
        raise HTTPException(404, "准入单不存在：%s@%s" % (slug, version))
    return t


@router.post("/api/admission/tickets/{slug}/{version}/stage")
async def record_admission_stage(slug: str, version: str, body: StageIn):
    try:
        return record_stage(slug, version, body.stage, body.state,
                            by="api", note=body.note, evidence=body.evidence)
    except KeyError as e:
        raise HTTPException(404, str(e))
    except ValueError as e:
        raise HTTPException(409, str(e))
