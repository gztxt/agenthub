"""配置包导出 / 导入（PT-20260927-15 / P2）。

**要解决的痛**：画像是「代码定义（`profiles.py`）+ 运行时注册（`POST /api/agents`）」的
混合体，另有 MCP ACL 与页面偏好。重装 NAS / 迁移到另一台机时这些全靠手工重做，
而 `profiles.py` 的覆盖项（用户改过的启动命令、端口）压根没有导出通道。

**形态：JSON，一律 JSON**。**不引入 YAML** —— 本机 `requirements.txt` 无 `pyyaml`，
而「零新增第三方依赖」是硬约束（`hubmcp.py`/`githubprojects.py` 都写着这条口径）。
外部评估建议过 YAML 导出，取其诉求、丢其格式。

**导什么、不导什么**（这条边界比代码重要）：

| 进包 | 不进包 | 为什么 |
|---|---|---|
| `custom_agents`（自定义注册） | `profiles.py` 的**代码本体** | 代码在 git 里，跟着机器走；导出它只会制造「改了导出版本、代码没改」的第二真相 |
| `mcp_acl`（工具授权规则） | **MCP server 的 URL/token**（`mcp_servers` 表） | server 地址与凭据是机器事实，不是可迁移配置；导出去等于把内网地址与 token 抄进一个文件 |
| `app_prefs`（页面偏好） | `.env`（含全部凭据） | 同上；且凭据落配置面违反军规 |
| |运行时库（`data/*.db` 主体） | 迁移的是配置不是数据；数据另有备份链 |

**脱敏**：`export_mod.redact_text` 对每个自由文本字段跑一遍。但要清楚它的边界 ——
它按**形态**（`ghp_`/`sk-`…）打码，对「用户把口令写在 description 里」这种没有形态的
明文无效。所以导出件仍**只当敏感件对待**：文件名固定带 `_sensitive`，导入必须过写门。

**导入是幂等 upsert，不是覆盖**：同一个 `id` 重复导入不会产生第二行，也不会清掉
本机独有���条目 —— 「导入一份包」不该顺手删掉目标机上已有的自定义 agent。
删除留给显式动作（用户可以在 UI 里逐个删，或先清库再导）。
"""

from __future__ import annotations

import json
import logging
import os
import time
from typing import Any, Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import PlainTextResponse
from pydantic import BaseModel

import db
import prefs
import sessions_export as export_mod
import writeauth

log = logging.getLogger(__name__)

router = APIRouter(tags=["settings-bundle"])

#: 包格式版本。**只增不改语义**；导入遇到未来版本要显式拒绝而不是尽力解析
#: （尽力解析会把「字段改名」变成静默丢配置，正是本仓反复治理的静默失效）。
BUNDLE_VERSION = 1
BUNDLE_KIND = "agenthub/settings-bundle"

#: 导出文件名固定带这个标记 —— 它会落在用户随手一存的位置。
SENSITIVE_HINT = "_sensitive"

#: 单个自由文本字段的导入侧长度上限（防超长注入，也防把整个日志塞进 description）。
MAX_TEXT = 2000
#: 单包条数上限。导入是**逐条**写库的，没有整包事务 ⇒ 上限是「一次导入最坏多久」的保护。
MAX_ROWS = 5000


class BundleError(ValueError):
    """包本身不合法（结构/版本/条数）。**与写门无关** —— 校验过了才有资格谈写门。"""


# ── 导出 ────────────────────────────────────────────────────────────────────
#: custom_agents 里要导的列。**显式列名，不SELECT \*** —— 表加列时导出件的语义
#: 不该悄悄变（`source`/`auto_restart` 这类后来加的列就是例子）。
_AGENT_COLS = ("id", "name", "type", "command", "args", "working_dir", "env",
               "port", "endpoint", "description", "auto_restart")
_ACL_COLS = ("agent_id", "server_id", "tool_pattern", "allow")


def _redact(v: Any) -> Any:
    """对可能含凭据的自由文本跑既有脱敏器。**只处理字符串**，其余原样。"""
    if isinstance(v, str):
        out, _ = export_mod.redact_text(v)
        return out
    return v


def collect() -> Dict[str, Any]:
    """收集当前配置（**不落盘**，返回纯 dict 便于 L0 直接取证）。"""
    agents = []
    for row in db.query("SELECT * FROM custom_agents ORDER BY created_at, id"):
        item = {}
        for c in _AGENT_COLS:
            v = row.get(c)
            # 空值统一成"" —— 不统一的话「导出→清库→导回」得到的形状与原来不同
            #（NULL 变""），往返测试就红，而那个红**不是缺陷**：sqlite 允许两种空表示。
            # 选"" 是因为导出件要给人和别的工具读，null 更费解。
            item[c] = _redact("" if v is None else v)
        agents.append(item)
    acls = [{c: row.get(c) for c in _ACL_COLS}
            for row in db.query("SELECT * FROM mcp_acl ORDER BY id")]
    # 偏好只导白名单内的键（prefs.PREF_KEYS 是判定口径，不在这里另立一套）。
    # 注意 `app_prefs.value` 存的是 **JSON 字符串**（见 prefs._save_pref），不是 dict ——
    # 直接当 dict 用会把 dict 的 repr 当字符串导出去。
    pref_rows = []
    for key in sorted(prefs.PREF_KEYS):
        rows = db.query("SELECT value FROM app_prefs WHERE key=?", (key,))
        if not rows:
            continue
        try:
            parsed = json.loads(rows[0]["value"])
        except (TypeError, ValueError):
            continue                      # 坏行按无偏好处理（同 prefs._load_row 口径）
        if not isinstance(parsed, dict):
            continue
        pref_rows.append({"key": key, "value": _redact(parsed)})
    return {
        "kind": BUNDLE_KIND,
        "version": BUNDLE_VERSION,
        "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
        "counts": {"custom_agents": len(agents), "mcp_acl": len(acls),
                   "prefs": len(pref_rows)},
        "custom_agents": agents,
        "mcp_acl": acls,
        "prefs": pref_rows,
    }


def to_json(bundle: Dict[str, Any]) -> str:
    """确定性序列化：同样的库内容两次导出**逐字节相同**（键排序 + 无随机序）。
    没有这一条，「diff 两个导出件看改了什么」这种最基本的手艺就用不了。"""
    return json.dumps(bundle, ensure_ascii=False, indent=2, sort_keys=True)


# ── 导入 ────────────────────────────────────────────────────────────────────
def _need_str(v: Any, field: str, limit: int = MAX_TEXT) -> str:
    if v is None:
        return ""
    s = str(v)
    if len(s) > limit:
        raise BundleError("%s 超长（%d > %d）" % (field, len(s), limit))
    return s


def _validate(bundle: Any) -> Dict[str, Any]:
    """结构校验。**只管形状，不碰库** —— 校验与写入分开，失败时库不会被改一半。"""
    if not isinstance(bundle, dict):
        raise BundleError("包必须是 JSON 对象")
    if bundle.get("kind") != BUNDLE_KIND:
        raise BundleError("这不是本服务的配置包（kind=%r）" % (bundle.get("kind"),))
    ver = bundle.get("version")
    if not isinstance(ver, int):
        raise BundleError("缺 version")
    if ver > BUNDLE_VERSION:
        # 显式拒绝，不做「尽力解析」：字段改名会变成静默丢配置。
        raise BundleError("包版本 %d 高于本服务支持的 %d —— 请先升级 hub 再导入"
                          % (ver, BUNDLE_VERSION))
    out: Dict[str, List[Dict[str, Any]]] = {"custom_agents": [], "mcp_acl": [], "prefs": []}
    total = 0
    agents = bundle.get("custom_agents") or []
    if not isinstance(agents, list):
        raise BundleError("custom_agents 必须是数组")
    for i, row in enumerate(agents):
        if not isinstance(row, dict):
            raise BundleError("custom_agents[%d] 不是对象" % i)
        aid = _need_str(row.get("id"), "custom_agents[%d].id" % i, 200)
        if not aid:
            raise BundleError("custom_agents[%d] 缺 id（幂等 upsert 靠它定位）" % i)
        out["custom_agents"].append({
            "id": aid,
            "name": _need_str(row.get("name"), "name") or aid,
            "type": _need_str(row.get("type"), "type", 50) or "custom",
            "command": _need_str(row.get("command"), "command"),
            "args": _need_str(row.get("args"), "args"),      # JSON 数组字符串
            "working_dir": _need_str(row.get("working_dir"), "working_dir"),
            "env": _need_str(row.get("env"), "env"),          # JSON 对象字符串
            "port": int(row["port"]) if row.get("port") not in (None, "") else None,
            "endpoint": _need_str(row.get("endpoint"), "endpoint"),
            "description": _need_str(row.get("description"), "description"),
            "auto_restart": 1 if row.get("auto_restart") else 0,
        })
        total += 1
    acls = bundle.get("mcp_acl") or []
    if not isinstance(acls, list):
        raise BundleError("mcp_acl 必须是数组")
    for i, row in enumerate(acls):
        if not isinstance(row, dict):
            raise BundleError("mcp_acl[%d] 不是对象" % i)
        pat = _need_str(row.get("tool_pattern"), "mcp_acl[%d].tool_pattern" % i)
        if not pat:
            raise BundleError("mcp_acl[%d] 缺 tool_pattern" % i)
        sid = row.get("server_id")
        out["mcp_acl"].append({
            "agent_id": _need_str(row.get("agent_id"), "agent_id", 200) or "*",
            "server_id": None if sid in (None, "") else _need_str(sid, "server_id", 200),
            "tool_pattern": pat,
            "allow": 1 if row.get("allow") else 0,
        })
        total += 1
    pr = bundle.get("prefs") or []
    if not isinstance(pr, list):
        raise BundleError("prefs 必须是数组")
    for i, row in enumerate(pr):
        if not isinstance(row, dict):
            raise BundleError("prefs[%d] 不是对象" % i)
        key = _need_str(row.get("key"), "prefs[%d].key" % i, 100)
        # 键白名单是 prefs 的口径，这里**复用**而不是另立一份
        if key not in prefs.PREF_KEYS:
            raise BundleError("prefs[%d] 键 %r 不在白名单内" % (i, key))
        out["prefs"].append({"key": key, "value": row.get("value") or {}})
        total += 1
    if total > MAX_ROWS:
        raise BundleError("包内共 %d 条，超过上限 %d" % (total, MAX_ROWS))
    return out


def apply_bundle(bundle: Any, actor: str = "unknown") -> Dict[str, Any]:
    """导入（幂等 upsert）。**先全量校验，再逐条写** —— 校验不过则库一个字节都不动。

    `actor` 进审计（`db.log_asset_event`），走既有记账三件套，不另造日志。
    """
    v = _validate(bundle)
    applied = {"custom_agents": 0, "mcp_acl": 0, "prefs": 0, "skipped": 0}
    now = time.strftime("%Y-%m-%dT%H:%M:%S")

    for row in v["custom_agents"]:
        db.execute(
            "INSERT INTO custom_agents(id,name,type,command,args,working_dir,env,"
            "port,endpoint,description,source,auto_restart,created_at,updated_at) "
            "VALUES(?,?,?,?,?,?,?,?,?,?,'import',?,?,?) "
            "ON CONFLICT(id) DO UPDATE SET name=excluded.name, type=excluded.type, "
            "command=excluded.command, args=excluded.args, "
            "working_dir=excluded.working_dir, env=excluded.env, port=excluded.port, "
            "endpoint=excluded.endpoint, description=excluded.description, "
            "auto_restart=excluded.auto_restart, source=excluded.source, "
            "updated_at=excluded.updated_at",
            (row["id"], row["name"], row["type"], row["command"], row["args"],
             row["working_dir"], row["env"], row["port"], row["endpoint"],
             row["description"], row["auto_restart"], now, now))
        applied["custom_agents"] += 1
        db.log_asset_event("custom_agent", row["id"], "import", actor,
                           {"name": row["name"], "via": "settings_bundle"})

    for row in v["mcp_acl"]:
        # ACL 表没有业务唯一键（只有自增 id）⇒ 幂等靠「同内容已存在则跳过」，
        # 不做 UPDATE —— 更新一条规则的动作语义是「先删后加」，那是 UI 的活。
        dup = db.query(
            "SELECT id FROM mcp_acl WHERE agent_id=? AND "
            "IFNULL(server_id,'')=IFNULL(?,'') AND tool_pattern=? AND allow=?",
            (row["agent_id"], row["server_id"], row["tool_pattern"], row["allow"]))
        if dup:
            applied["skipped"] += 1
            continue
        db.execute(
            "INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow) VALUES(?,?,?,?)",
            (row["agent_id"], row["server_id"], row["tool_pattern"], row["allow"]))
        applied["mcp_acl"] += 1
        db.log_asset_event("mcp_acl", str(row["agent_id"]), "import", actor,
                           {"tool_pattern": row["tool_pattern"],
                            "allow": row["allow"], "via": "settings_bundle"})

    for row in v["prefs"]:
        # 走 prefs 自己的口径（`normalize_value` 收 `PrefValue` 而非 dict）——
        # 去空/去重/截断/封顶都在那里，**这里不复制一份**。重复实现的那次教训是
        # 「两套各自漂移」，判例见知识库（列表与校验必须同源）。
        # 只取 PrefValue 认识的键：包里的未知键不该让整个导入炸掉
        # （pydantic 对多余键是抛 ValidationError 的）。
        raw = row["value"] if isinstance(row["value"], dict) else {}
        pv = prefs.PrefValue(stars=raw.get("stars") or [],
                             hidden=raw.get("hidden") or [])
        val = prefs.normalize_value(pv)
        db.execute(
            "INSERT INTO app_prefs(key,value,updated_at) VALUES(?,?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value, "
            "updated_at=excluded.updated_at",
            (row["key"], json.dumps(val, ensure_ascii=False), now))
        applied["prefs"] += 1

    db.log_asset_event("settings_bundle", "import", "import", actor, applied)
    return applied


def suggested_filename() -> str:
    """导出件的建议文件名。**带 `_sensitive`** —— 它含自定义 agent 的命令与环境。"""
    return "agenthub-settings_%s%s.json" % (
        time.strftime("%Y%m%d-%H%M%S"), SENSITIVE_HINT)


# ── HTTP 面 ────────────────────────────────────────────────────────────────
def _require_auth(request: Request) -> None:
    """**导出按写方法判**（尽管它是 GET）。

    与 `audit.py::_require_read` 同一口径，理由相同：批量读＝数据外流。
    配置包含全部自定义 agent 的启动命令与环境变量 —— 那是**执行意图**，
    拿到它的人就能在本机以外复现一整套调用。`GET` 不走 `write_gate`
    （它只拦写方法），所以这里必须显式判一次，否则就是一个无鉴权导出通道。
    """
    verdict, reason = writeauth.decide(
        "POST", request.url.path,          # 强制按写方法判
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        # 只打 verdict/path/来源，绝不打凭据
        log.warning(f"[settings-bundle] 拒绝 {verdict}：{request.url.path} "
                    f"来源={request.client.host if request.client else '?'} —— {reason}")
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401,
                            detail=reason)


class ImportBody(BaseModel):
    bundle: Dict[str, Any]


@router.get("/api/settings/export")
async def export_settings(request: Request):
    """导出配置包（JSON 文本）。**必须走鉴权**，理由见 `_require_auth`。"""
    _require_auth(request)
    body = to_json(collect())
    return PlainTextResponse(
        body, media_type="application/json",
        headers={"Content-Disposition":
                 'attachment; filename="%s"' % suggested_filename(),
                 # 导出件含命令与环境：禁中间层与浏览器缓存
                 "Cache-Control": "no-store", "X-Content-Type-Options": "nosniff"})


@router.post("/api/settings/import")
async def import_settings(body: ImportBody, request: Request):
    """导入配置包（幂等 upsert，不清本机独有条目）。"""
    _require_auth(request)          # POST 会被全局 write_gate 拦，这里是保险带
    try:
        applied = apply_bundle(body.bundle, writeauth.actor_of(request))
    except BundleError as e:
        raise HTTPException(400, str(e))
    return {"ok": True, "applied": applied}