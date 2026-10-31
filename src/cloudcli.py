"""CloudCLI 项目直达（v0.13.29）：项目清单 + 点击快速开始会话。

背景：用户在 CloudCLI（Claude 原生 Web 界面，:3010）有 28 个活跃项目，但 hub 里只有
整页 embed——没有项目级入口，「加载本机所有项目、精确显示名称、点击即开始」缺整条链。

链路（2026-09-26 全链实测）：
- **项目清单直读 SQLite**（不走 cloudcli REST——省鉴权链路，只读零风险）：
  /vol1/cloudcli/auth.db 的 projects（project_id/project_path/custom_project_name/
  isStarred/isArchived）+ sessions（project_path 关联，会话数+最近活动一条 GROUP BY）。
- **创建会话 = POST /api/providers/sessions** body {provider:'claude', projectPath,
  initialMessage} → 201 {sessionId, sessionName} → 前端直达 /session/{sessionId}。

安全模型：
- auth.db 只读连接（file:...?mode=ro，与 memfed._ro_connect 同模式）；
- JWT 铸造（知识文档 50 号先例）：app_config.jwt_secret + users 首行 → HS256
  {userId, username, iat, exp}，短时 2h，**secret 只在进程内读绝不入日志**；
- POST /api/cloudcli/start 按写方法判（writeauth.decide）——创建会话是写动作；
- GET /api/cloudcli/projects 不埋 runlog（同 /api/agents 防噪声口径），
  start 埋 cc.start；
- cloudcli 服务挂 → 项目清单照常（直读 db 与服务活死解耦），只有 start 如实报错。
"""
from __future__ import annotations

import base64
import hashlib
import hmac
import json
import logging
import os
import sqlite3
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException, Query, Request
from pydantic import BaseModel, Field

import runlog
import tdai_client
import writeauth

log = logging.getLogger(__name__)

router = APIRouter(prefix="/api/cloudcli", tags=["cloudcli"])

#: auth.db 位置（cloudcli 服务端 WorkingDirectory=/fs/1000/ftp/技术文档/cloudcli，
#: 但认证库历史落 /vol1/cloudcli/——实测在用。env 可覆盖防路径漂移）。
AUTH_DB = Path(os.getenv("CLOUDCLI_AUTH_DB", "/vol1/cloudcli/auth.db"))

#: cloudcli 服务端点（仅 start 转调用；探测端口活性的职责在 discovery，不在此重复）
CLOUDCLI_URL = os.getenv("CLOUDCLI_URL", "http://127.0.0.1:3010")

#: 铸 token 有效期（秒）。短时：探针与「快速开始」都够用，泄露面小。
TOKEN_TTL_S = 2 * 3600

#: 项目列表上限（防 db 异常膨胀拖垮响应；实测 28，128 头部空间）。
PROJECTS_MAX = 128


def _ro_connect() -> sqlite3.Connection:
    """只读连接 auth.db（memfed._ro_connect 同模式：mode=ro，绝不要求写权限）。"""
    if not AUTH_DB.is_file():
        raise FileNotFoundError(f"auth.db 不存在: {AUTH_DB}")
    return sqlite3.connect(f"file:{AUTH_DB}?mode=ro", uri=True, timeout=2.0)


def _mint_token() -> str:
    """铸短时 JWT（HS256）。payload {userId, username, iat, exp}——与 cloudcli
    auth.middleware.generateToken 逐字段对齐；secret 每次现读（cloudcli 重装换
    secret 后无需重启 hub），读不到如实抛。"""
    con = _ro_connect()
    try:
        secret_row = con.execute(
            "SELECT value FROM app_config WHERE key='jwt_secret'").fetchone()
        user = con.execute(
            "SELECT id, username FROM users ORDER BY id LIMIT 1").fetchone()
    finally:
        con.close()
    if not secret_row or not secret_row[0]:
        raise RuntimeError("app_config.jwt_secret 缺失（cloudcli 认证库未初始化）")
    if not user:
        raise RuntimeError("users 表为空（无可用账户）")

    def b64u(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    now = int(time.time())
    header = b64u(json.dumps({"alg": "HS256", "typ": "JWT"}).encode())
    payload = b64u(json.dumps({
        "userId": user[0], "username": user[1],
        "iat": now, "exp": now + TOKEN_TTL_S,
    }).encode())
    signing = f"{header}.{payload}"
    sig = b64u(hmac.new(str(secret_row[0]).encode(), signing.encode(),
                        hashlib.sha256).digest())
    return f"{signing}.{sig}"


def _list_projects() -> Dict[str, Any]:
    """全部活跃项目：名称精确（custom_project_name 优先，回落 basename）+ 会话数
    + 最近活动。一条 JOIN 拿齐，读不到会话表字段就降级（列级容错防升级换 schema）。"""
    t0 = time.monotonic()
    try:
        con = _ro_connect()
    except FileNotFoundError as e:
        return {"ok": False, "projects": [], "count": 0, "error": str(e)}
    try:
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT p.project_id, p.project_path, p.custom_project_name, p.isStarred,"
            " COALESCE(s.n, 0) AS sessions, s.latest"
            " FROM projects p"
            " LEFT JOIN (SELECT project_path, COUNT(*) n, MAX(updated_at) latest"
            "            FROM sessions WHERE isArchived=0 GROUP BY project_path) s"
            "   ON s.project_path = p.project_path"
            " WHERE p.isArchived=0"
            " ORDER BY p.isStarred DESC, COALESCE(s.latest, '') DESC, p.project_path"
            f" LIMIT {PROJECTS_MAX}").fetchall()
        projects: List[Dict[str, Any]] = []
        for r in rows:
            path = r["project_path"] or ""
            name = (r["custom_project_name"]
                    or os.path.basename(path.rstrip("/")) or path)
            projects.append({
                "project_id": r["project_id"],
                "name": name,                      # 精确名称：自定义名 > basename
                "path": path,
                "starred": bool(r["isStarred"]),
                "sessions": int(r["sessions"] or 0),
                "last_activity": (str(r["latest"])[:19] if r["latest"] else None),
            })
        return {"ok": True, "projects": projects, "count": len(projects),
                "took_ms": round((time.monotonic() - t0) * 1000, 1)}
    except sqlite3.Error as e:
        return {"ok": False, "projects": [], "count": 0,
                "error": f"auth.db schema 不兼容: {e}"}
    finally:
        con.close()


@router.get("/projects")
async def cloudcli_projects():
    """CloudCLI 项目清单（直读 auth.db，与 cloudcli 服务活死解耦）。"""
    return _list_projects()


class StartIn(BaseModel):
    path: str = Field(min_length=1, max_length=500,
                      description="项目绝对路径（来自 /projects 的 path 字段）")
    initial_message: str = Field(default="", max_length=2000)


@router.post("/start")
@runlog.track("cc.start")
async def cloudcli_start(request: Request, body: StartIn):
    """在 CloudCLI 创建会话并返回直达 URL——「点击项目快速开始」的后端半程。

    按写方法判（writeauth）：创建会话是写动作；铸 JWT 转调 cloudcli REST。
    cloudcli 服务不可达 ⇒ 如实报错（项目清单不受影响——两条链路解耦）。
    """
    verdict, reason = writeauth.decide(
        "POST", request.url.path,
        writeauth.provided_token(request.headers.raw, request.url.query),
        writeauth.secrets_from_env())
    if verdict not in ("allow", "exempt"):
        log.warning(f"[cloudcli] 拒绝 {verdict}：{request.url.path} "
                    f"来源={request.client.host if request.client else '?'} —— {reason}")
        raise HTTPException(status_code=503 if verdict == "misconfig" else 401, detail=reason)

    try:
        token = _mint_token()
    except (FileNotFoundError, RuntimeError) as e:
        raise HTTPException(503, f"CloudCLI 认证库不可用：{e}")

    # 转调 cloudcli 创建会话（provider 固定 claude：cloudcli 是 Claude Code 的 Web 宿主）
    req = urllib.request.Request(
        f"{CLOUDCLI_URL}/api/providers/sessions",
        data=json.dumps({"provider": "claude", "projectPath": body.path,
                         "initialMessage": body.initial_message}).encode(),
        headers={"Authorization": f"Bearer {token}",
                 "Content-Type": "application/json"},
        method="POST")
    try:
        with urllib.request.urlopen(req, timeout=10) as resp:
            data = json.loads(resp.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        detail = ""
        try:
            detail = tdai_client.scrub(e.read().decode("utf-8", "replace"))[:300]
        except Exception:  # noqa: BLE001 —— 取不到正文不该盖掉 HTTP 码
            pass
        raise HTTPException(502, f"CloudCLI 拒绝创建会话（HTTP {e.code}）: {detail}")
    except Exception as e:  # noqa: BLE001 —— URLError/超时/解析失败
        raise HTTPException(502, f"CloudCLI 服务不可达: {type(e).__name__}")

    sid = (data.get("data") or {}).get("sessionId")
    if not sid:
        raise HTTPException(502, "CloudCLI 返回 201 但无 sessionId（schema 漂移？）")
    return {"sessionId": sid,
            "session_name": (data.get("data") or {}).get("sessionName"),
            "url": f"/session/{sid}",
            "cloudcli_base": CLOUDCLI_URL}
