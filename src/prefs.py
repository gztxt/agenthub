"""应用级偏好 KV（v0.13.36）：两项目页的收藏/隐藏状态落服务端。

背景（09-26 会话中断点续做）：收藏/隐藏此前只写浏览器 localStorage——
localStorage 按 origin 隔离，局域网 IP / Tailscale / 手机 WebView 各一套存档，
桌面标了收藏手机看不到，用户在多端用 hub 时状态必然分叉。落服务端后
各端一致；localStorage 保留为**离线兜底缓存**（后端没升到 0.13.36 时静默沿用）。

设计：
- 键白名单 PREF_KEYS 只有两枚（projects.lp / projects.gh），不是自由 KV——
  自由 KV 迟早被当成无鉴权配置面用（writeauth 红基线的教训同源）；
- 值形状 PrefValue：{"stars":[str], "hidden":[str]}，normalize 时逐项截断
  （单串 512 / 条数 2000），解析失败按空处理——坏数据不许打断渲染；
- GET 只读不拦（与 /api/localprojects 同口径）；PUT 走全局 write_gate 之外
  再显式 writeauth.decide（github clone 同款保险带）；
- 审计：log_asset_event("setting", key, "update", actor)——asset_type 枚举里
  本就有 "setting"，detail 只记条数不记内容（路径列表没必要进审计）。
"""
from __future__ import annotations

import json
from datetime import datetime, timezone
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import db
import writeauth

router = APIRouter(tags=["prefs"])

#: 键白名单——新增键必须在这里登记理由，否则 404
PREF_KEYS = {
    "projects.lp": "本机项目页收藏/隐藏（09-26 用户裁定的行内动作）",
    "projects.gh": "GitHub 项目页收藏/隐藏（同上，远端半程）",
}

#: 值尺寸硬顶：路径串 512、条数 2000、命中白名单后仍不放开体积
MAX_STR = 512
MAX_ITEMS = 2000


class PrefValue(BaseModel):
    stars: List[str] = []
    hidden: List[str] = []


def normalize_value(v: PrefValue) -> dict:
    """去空、去重、逐项截断、条数封顶——形状漂移的旧客户端也不能写坏表。"""

    def clean(seq: List[str]) -> List[str]:
        out: List[str] = []
        for s in seq[:MAX_ITEMS]:
            s = str(s or "")[:MAX_STR]
            if s and s not in out:
                out.append(s)
        return out

    return {"stars": clean(v.stars), "hidden": clean(v.hidden)}


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _load_row(key: str) -> Optional[dict]:
    rows = db.query("SELECT value FROM app_prefs WHERE key=?", (key,))
    if not rows:
        return None
    try:
        v = json.loads(rows[0]["value"])
        return v if isinstance(v, dict) else None
    except Exception:  # noqa: BLE001
        return None          # 坏行按无偏好处理，绝不 500 打断列表渲染


def _save_pref(key: str, body: dict, actor: str) -> dict:
    db.execute(
        "INSERT INTO app_prefs(key,value,updated_at) VALUES(?,?,?)"
        " ON CONFLICT(key) DO UPDATE SET value=excluded.value,"
        " updated_at=excluded.updated_at",
        (key, json.dumps(body, ensure_ascii=False), _now()))
    db.log_asset_event("setting", key, "update", actor,
                       {"stars": len(body["stars"]), "hidden": len(body["hidden"])})
    return body


@router.get("/api/prefs/{key}")
def get_pref(key: str):
    if key not in PREF_KEYS:
        raise HTTPException(404, "未知偏好键")
    return {"key": key, "value": _load_row(key)}


@router.put("/api/prefs/{key}")
def put_pref(key: str, request: Request, v: PrefValue):
    # 同步 handler：体量极小且 db.execute 自带锁，进线程池反而省事件循环
    if key not in PREF_KEYS:
        raise HTTPException(404, "未知偏好键")
    # 保险带：全局 write_gate 之外再显式判一次（githubprojects POST /clone 同款）
    verdict, reason = writeauth.decide_req(request, "PUT")
    if verdict not in ("allow", "exempt"):
        raise HTTPException(401 if verdict != "misconfig" else 503, reason)
    # 审计身份按本次解析出的用户算（write_gate 中间件设的 state.actor 在
    # 直调/测试态不存在 ⇒ 回落到 identity_of 再解析一次；只记用户名，不记任何凭据值）
    _u = getattr(request.state, "user", None)
    if not _u:
        try:
            import users as _users
            _u, _p, _how = _users.identity_of(request)
        except Exception:  # noqa: BLE001 —— 取不到身份不该把写操作打挂
            _u = None
    actor = "user:" + (_u["username"] if _u else "reader")
    return {"key": key, "value": _save_pref(key, normalize_value(v), actor)}
