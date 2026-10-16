# -*- coding: utf-8 -*-
"""技能资产管理（P1-5 其余四件；手册 A9：Skill 当**软件资产**管）。

五件套里的 **digest** 已由 P1-5 首片落地（`src/skill.py::package_digest`）。
本模块补其余四件 —— **owner / version / scope / 状态机** —— 并**把 digest 接进准入**。

【为什么 digest 与状态机必须合起来才成立】

单有整包摘要，只能**发现**包变了（C8）；单有状态机，只能**声明**它处于哪一态（A9）。
两者分开都拦不住最要命的那种情况：**包被改了、资产还挂着 published** ——
改一个 `scripts/foo.py` 而没重新评审的包，仍在以「已发布」身份被 agent 依赖。
故本模块把二者焊在一起：

    published 记录「发布那一刻的整包 digest」；
    之后每次读资产，都用当前盘上的 digest 与它比 —— 不等 ⇒ `effective_status()='stale'`。
    **stale 不是 published 的修饰，是另一个态**：它不该被依赖，要重新走 Test→Review→Publish。

【状态机】（A9 的 Draft→Test→Review→Publish→Deprecate）

    draft ──▶ tested ──▶ reviewed ──▶ published ──▶ deprecated
      ▲          │           │            │             │
      └──────────┴───────────┴────────────┴─────────────┘
                        revise（重新起草）

规则（`transition_reason()` 逐条给理由，不是一句「非法」）：
  · **只能走 allowed 边**：跳级（draft→published）与跨级回退一律拒；
  · **publish 必须带上 digest**（或给 `path` 由本模块现算）—— 没有指纹的「已发布」
    等于不可追溯，那不是资产、是承诺；
  · `deprecated` 仍可 `revise` 回 draft（弃用≠删除，历史要留）。

【owner / version / scope 的口径】
  · owner：责任人。**空值不允许** —— 无主资产正是「无主副本必删」那条军规的成因；
  · version：`MAJOR.MINOR.PATCH`（可带 `-pre` 后缀），**只许前进**：已发布过的版本不得被
    「改回去」，否则下游对比会静默错位；
  · scope：`global` | `route:<名>` | `agent:<id>`，白名单。作用域是**影响面声明**，
    自由文本等于没有声明。

本模块的判定部分是纯函数 ⇒ L0 可整层测；存储部分走 `db`（幂等建表）。
"""

from __future__ import annotations

import os
import re
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Query
from pydantic import BaseModel, Field

import db

router = APIRouter()

#: 状态机。**显式列边**（不是「除了终态都能到」那种隐式规则）——
#: 隐式规则在加态时必然出错，而这里的每一条边都要能对上手册的那一步。
TRANSITIONS: Dict[str, Tuple[str, ...]] = {
    "draft": ("tested",),
    "tested": ("reviewed", "draft"),
    "reviewed": ("published", "draft"),
    "published": ("deprecated", "draft"),
    "deprecated": ("draft",),
}
STATES: Tuple[str, ...] = tuple(TRANSITIONS)
#: 只有这个态可以被 agent 依赖（其余态必须显式引用，见 A9 的「准入」语义）。
DEPENDABLE = "published"

_VERSION_RE = re.compile(r"\A(\d+)\.(\d+)\.(\d+)(?:[-+][0-9A-Za-z.\-]+)?\Z")
_SCOPE_RE = re.compile(r"\A(?:global|route:[A-Za-z0-9_.\-]{1,40}|agent:[A-Za-z0-9_.\-]{1,64})\Z")


# ── 纯判定 ────────────────────────────────────────────────────────────
def parse_version(v: str) -> Optional[Tuple[int, int, int]]:
    """`MAJOR.MINOR.PATCH` → 三元组；不合法返回 None（**不猜**）。"""
    m = _VERSION_RE.match((v or "").strip())
    return (int(m.group(1)), int(m.group(2)), int(m.group(3))) if m else None


def compare_version(a: str, b: str) -> Optional[int]:
    """比较两个版本；任一方非法返回 None（调用方据此拒绝，而不是当 0 处理）。"""
    pa, pb = parse_version(a), parse_version(b)
    if pa is None or pb is None:
        return None
    return (pa > pb) - (pa < pb)


def validate_scope(scope: str) -> Optional[str]:
    """作用域白名单校验；返回**错误原因**（None = 合法）。"""
    s = (scope or "").strip()
    if not s:
        return "scope 不能为空（必须是 global / route:<名> / agent:<id> 之一）"
    if not _SCOPE_RE.match(s):
        return ("scope 形状非法：%r —— 只接受 global、route:<名>、agent:<id>" % s)
    return None


def transition_reason(frm: str, to: str) -> Optional[str]:
    """能否从 `frm` 走到 `to`；返回**拒绝原因**（None = 允许）。纯函数。"""
    frm, to = (frm or "").strip(), (to or "").strip()
    if frm not in TRANSITIONS:
        return "未知起始态 %r（可用 %s）" % (frm, ", ".join(STATES))
    if to not in TRANSITIONS:
        return "未知目标态 %r（可用 %s）" % (to, ", ".join(STATES))
    if to == frm:
        return "状态未变化（%s → %s）：重复推进不得当成功" % (frm, frm)
    if to not in TRANSITIONS[frm]:
        return ("不允许 %s → %s（从 %s 只能到 %s —— 状态机是显式列边的，"
                "跳级与跨级回退都要走 revise→draft）"
                % (frm, to, frm, " / ".join(TRANSITIONS[frm])))
    return None


def is_stale(rec: Dict[str, Any], current_digest: Optional[str]) -> bool:
    """**已发布但指纹变了** ⇒ 陈旧。

    两个前提都要成立才判 stale：
      · 资产处于 published（其余态本来就不可依赖，谈不上「陈旧」）；
      · 盘上算得出当前 digest（算不出＝不可判定，**不许**当成 stale —— 判不了就不拦，
        与 P0-4 的 `insufficient` 同口径）。
    """
    if (rec or {}).get("status") != DEPENDABLE:
        return False
    recorded = rec.get("digest")
    if not current_digest or not recorded:
        return False
    return recorded != current_digest


def effective_status(rec: Dict[str, Any], current_digest: Optional[str]) -> str:
    """对外状态：published 且指纹不符 ⇒ `stale`（**它是独立态，不是 published 的修饰**）。"""
    return "stale" if is_stale(rec, current_digest) else (rec or {}).get("status", "draft")


# ── 存储 ──────────────────────────────────────────────────────────────
SCHEMA = """
CREATE TABLE IF NOT EXISTS skill_assets (
    slug        TEXT PRIMARY KEY,
    owner       TEXT NOT NULL,
    version     TEXT NOT NULL,
    scope       TEXT NOT NULL,
    status      TEXT NOT NULL DEFAULT 'draft',
    path        TEXT,
    digest      TEXT,
    deps        TEXT NOT NULL DEFAULT '[]',
    models      TEXT NOT NULL DEFAULT '[]',
    note        TEXT,
    by_whom     TEXT,
    created_at  TEXT NOT NULL,
    updated_at  TEXT NOT NULL
);
"""


def ensure_schema() -> None:
    db.execute_script(SCHEMA)


def _now() -> str:
    from datetime import datetime, timezone
    return datetime.now(timezone.utc).isoformat()


def get(slug: str) -> Optional[Dict[str, Any]]:
    ensure_schema()
    rows = db.query("SELECT * FROM skill_assets WHERE slug=?", (slug,))
    return dict(rows[0]) if rows else None


def list_assets(limit: int = 200) -> List[Dict[str, Any]]:
    ensure_schema()
    return [dict(r) for r in db.query(
        "SELECT * FROM skill_assets ORDER BY updated_at DESC LIMIT ?", (int(limit),))]


def upsert(slug: str, owner: str, version: str, scope: str, *,
           deps: Optional[List[str]] = None, models: Optional[List[str]] = None,
           note: Optional[str] = None, by_whom: str = "system") -> Dict[str, Any]:
    """登记/更新资产的元数据（owner/version/scope）。

    **两条不许悄悄发生的改动**：
      · 已发布资产的 **version 倒退** ⇒ 拒（下游对比会静默错位）；
      · 改 scope ⇒ 允许，但会写进 `note` 的审计尾巴（影响面变化必须留痕）。
    """
    import json as _json
    ensure_schema()
    slug = (slug or "").strip()
    if not slug:
        raise ValueError("slug 不能为空")
    owner = (owner or "").strip()
    if not owner:
        raise ValueError("owner 不能为空（无主资产＝无主副本，军规直接禁止）")
    if parse_version(version) is None:
        raise ValueError("version 必须是 MAJOR.MINOR.PATCH：%r" % version)
    err = validate_scope(scope)
    if err:
        raise ValueError(err)
    cur = get(slug)
    if cur and cur["status"] == DEPENDABLE:
        cmp_ = compare_version(version, cur["version"])
        if cmp_ is not None and cmp_ < 0:
            raise ValueError("已发布资产的版本不得倒退：%s → %s" % (cur["version"], version))
    now = _now()
    trail = note
    if cur and cur["scope"] != scope:
        trail = "%s｜scope 变更 %s → %s（by %s）" % (note or "", cur["scope"], scope, by_whom)
    db.execute(
        "INSERT INTO skill_assets(slug,owner,version,scope,status,path,digest,deps,models,"
        "note,by_whom,created_at,updated_at)"
        " VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)"
        " ON CONFLICT(slug) DO UPDATE SET owner=excluded.owner,version=excluded.version,"
        "scope=excluded.scope,deps=excluded.deps,models=excluded.models,note=excluded.note,"
        "by_whom=excluded.by_whom,updated_at=excluded.updated_at",
        (slug, owner, version, scope, (cur or {}).get("status", "draft"),
         (cur or {}).get("path"), (cur or {}).get("digest"),
         _json.dumps(list(deps or []), ensure_ascii=False),
         _json.dumps(list(models or []), ensure_ascii=False),
         trail, by_whom, (cur or {}).get("created_at") or now, now))
    return get(slug)


def transition(slug: str, to: str, *, path: Optional[str] = None,
               digest: Optional[str] = None, by: str = "system",
               note: Optional[str] = None) -> Dict[str, Any]:
    """推进状态机。非法转移抛 ValueError（带逐条理由）。

    `to == 'published'` 时**必须能确定整包指纹**：给了 `path` 就现算
    （`skill.package_digest`），或直接给 `digest`。两者都没有 ⇒ 拒 ——
    「发布了但不知道发布的是什么」不可接受（那是承诺不是资产）。
    """
    rec = get(slug)
    if not rec:
        raise KeyError(slug)
    why = transition_reason(rec["status"], to)
    if why:
        raise ValueError(why)
    new_digest = rec.get("digest")
    if to == DEPENDABLE:
        if not digest:
            if not path:
                raise ValueError("publish 必须给 path（现算整包摘要）或 digest —— "
                                 "没有指纹的「已发布」不可追溯")
            import skill
            d = skill.package_digest(path)
            if d.get("error") or not d.get("digest"):
                raise ValueError("整包摘要算不出来（%s），拒绝 publish"
                                 % (d.get("error") or "空摘要"))
            digest = d["digest"]
            new_digest = d["digest"]
            path = path
        else:
            new_digest = digest
    now = _now()
    db.execute(
        "UPDATE skill_assets SET status=?,digest=COALESCE(?,digest),path=COALESCE(?,path),"
        "note=?,by_whom=?,updated_at=? WHERE slug=?",
        (to, new_digest if to == DEPENDABLE else None, path, note or rec.get("note"),
         by, now, slug))
    return get(slug)


# ── 端点 ──────────────────────────────────────────────────────────────
class AssetIn(BaseModel):
    owner: str = Field(min_length=1, max_length=120)
    version: str = Field(min_length=5, max_length=64)
    scope: str = Field(min_length=1, max_length=80)
    deps: Optional[List[str]] = None
    models: Optional[List[str]] = None
    note: Optional[str] = Field(default=None, max_length=500)


class TransitionIn(BaseModel):
    to: str = Field(min_length=1, max_length=32)
    path: Optional[str] = Field(default=None, max_length=500)
    digest: Optional[str] = Field(default=None, max_length=64)
    note: Optional[str] = Field(default=None, max_length=500)


def _current_digest(rec: Dict[str, Any], check_fresh: bool) -> Optional[str]:
    """读盘上当前整包摘要（用于判 stale）。算不出 ⇒ None（不可判定，不当 stale）。"""
    if not check_fresh or not rec.get("path"):
        return None
    try:
        import skill
        return skill.package_digest(rec["path"]).get("digest") or None
    except Exception:  # noqa: BLE001
        return None


@router.get("/api/skill-assets")
async def list_skill_assets(check_fresh: int = Query(default=1, ge=0, le=1),
                            limit: int = Query(default=200, le=500)):
    """资产清单。`check_fresh=1`（默认）时逐条读盘比对整包摘要 —— 代价是有界 IO，
    所以 `limit` 有上限；不需要新鲜度就传 0（纯读库）。"""
    out = []
    for rec in list_assets(limit):
        cur = _current_digest(rec, bool(check_fresh))
        item = dict(rec)
        item["current_digest"] = cur
        item["effective_status"] = effective_status(rec, cur)
        item["stale"] = is_stale(rec, cur)
        item["dependable"] = item["effective_status"] == DEPENDABLE
        out.append(item)
    return {"assets": out, "count": len(out), "states": list(STATES),
            "transitions": {k: list(v) for k, v in TRANSITIONS.items()}}


@router.get("/api/skill-assets/{slug}")
async def get_skill_asset(slug: str, check_fresh: int = Query(default=1, ge=0, le=1)):
    rec = get(slug)
    if not rec:
        raise HTTPException(404, "资产未登记：%s" % slug)
    cur = _current_digest(rec, bool(check_fresh))
    item = dict(rec)
    item["current_digest"] = cur
    item["effective_status"] = effective_status(rec, cur)
    item["stale"] = is_stale(rec, cur)
    return item


@router.post("/api/skill-assets/{slug}")
async def upsert_skill_asset(slug: str, body: AssetIn):
    try:
        return upsert(slug, body.owner, body.version, body.scope, deps=body.deps,
                      models=body.models, note=body.note, by_whom="api")
    except ValueError as e:
        raise HTTPException(400, str(e))


@router.post("/api/skill-assets/{slug}/transition")
async def transition_skill_asset(slug: str, body: TransitionIn):
    try:
        return transition(slug, body.to, path=body.path, digest=body.digest,
                          by="api", note=body.note)
    except KeyError:
        raise HTTPException(404, "资产未登记：%s" % slug)
    except ValueError as e:
        raise HTTPException(409 if "不允许" in str(e) or "状态未变化" in str(e) else 400, str(e))