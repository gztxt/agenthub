"""Botboy（v0.13.170）：DS Bot 团队面板 —— 只读投影，不建第二权威副本。

来源与依据（2026-10-27）：
- 用户要在 hub 左侧「系统」菜单加 Botboy 子菜单，对应 DeepSeek Harness 的
  DS Bot 团队模式（Bot / 群聊 / 任务协作，独立开源项目 liguobao/ds-harness-remote
  所对接的 Fei2411/DS-Bot）。
- **数据结构以对端源码为准，不猜**：`ds-bot@0.2.2` 的 `src/host/store.js` 头注释写明
  「the roster, rooms, and the bot-to-bot exchange log live in one JSON file under
  DSH_HOME」；路径 `store` 侧是 `join(dshHome, 'bot') + '/state.json'`
  （`index.js`：`home = config.home ?? join(dshHome, 'bot')`）。
  记录形状取自 `roster.js::buildBot` / `buildRoom` 与 `delivery.js::deliver`。
- **为什么读文件而不是调 `POST api/bot`**：那条路由要 dsh web 在跑（本机 dsh 是
  按需拉起、常年 inactive），还要浏览器鉴权 token（进程级随机，须从 journal 抠）；
  而 state.json 是同一份数据的落盘面。只读面板读文件 ⇒ 不依赖 dsh 在跑、
  不碰 token、不引入对新插件 route 的版本耦合。
- **只读边界**：本模块不写 state.json、不调任何 mutation。`ds-bot` 自己有
  `state.lock`（home-lock.js）保证同一份数据只有一个写者 —— hub 越权改写会
  直接制造"另一个 dsh 正在编辑"的假锁冲突。要建 Bot / 发消息，去 dsh 原生界面。

两种「没有」必须可分辨（AGENTS.md 沉淀义务 / PT-20261019 静默不可用教训）：
- `found=false`：state.json 不存在 ⇒ 还没装 ds-bot 或还没建过团队，附安装态。
- `ok=false`：文件在但读不了/不是 JSON ⇒ 不可判定，附错误原因，绝不当成空团队。

端点：
- GET /api/botboy/overview?limit=30 —— 读一次 state.json，归一化 bots/rooms/exchanges。
"""

from __future__ import annotations

import json
import os
import re
import time
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Query

router = APIRouter(tags=["botboy"])

#: 单条交接文本截断长度（state.json 里原文可能上万字；概览页只要"说了什么"的头一句）
TEXT_KEPT = 240
#: 概览默认/最多返回的交接条数（文件里最多 600 条，全量下发会把页面拖慢）
DEFAULT_LIMIT = 30
MAX_LIMIT = 200
#: 探测安装态时看的 profile（dsh 的插件是按 profile 装的，本机实测三个）
PROFILES = ("web", "tui", "headless", "desktop")
#: 关心的两个包：ds-bot 是数据生产者，ds-harness-remote 是手机/远程入口
PACKAGES = ("ds-bot", "ds-harness-remote")


def dsh_home() -> Path:
    """DSH 主目录。与 ds-bot `folders.dshHomeOf(process.env.DSH_HOME)` 同口径。"""
    env = os.environ.get("DSH_HOME")
    return Path(env).expanduser() if env else Path.home() / ".dsh"


def state_path() -> Path:
    return dsh_home() / "bot" / "state.json"


def _read_state(p: Path) -> Dict[str, Any]:
    """读 state.json。返回 {ok, found, data|error}，绝不抛（面板坏一个文件不该 500）。"""
    try:
        raw = p.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {"ok": True, "found": False, "data": None}
    except OSError as e:
        return {"ok": False, "found": False, "error": {
            "code": "state-unreadable", "message": "读不到 %s：%s" % (p, e)}}
    try:
        data = json.loads(raw)
    except ValueError as e:
        return {"ok": False, "found": True, "error": {
            "code": "state-invalid-json", "message": "%s 不是合法 JSON：%s" % (p, e)}}
    if not isinstance(data, dict):
        return {"ok": False, "found": True, "error": {
            "code": "state-invalid-shape",
            "message": "%s 顶层不是对象（%s）" % (p, type(data).__name__)}}
    return {"ok": True, "found": True, "data": data}


def _ms(v: Any) -> Optional[int]:
    """epoch 毫秒取整；非数字/负数一律 None（时间戳坏值不许渲染成 1970）。"""
    try:
        n = int(v)
    except (TypeError, ValueError):
        return None
    return n if n > 0 else None


def _text(v: Any, keep: int = TEXT_KEPT) -> str:
    s = "" if v is None else str(v)
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= keep else s[:keep] + "…"


def _installed() -> Dict[str, Any]:
    """按 profile 探测 npm 包装没装（看 node_modules/<pkg>/package.json 是否存在）。"""
    home = dsh_home()
    out: Dict[str, Dict[str, bool]] = {}
    for prof in PROFILES:
        mods = home / "profiles" / prof / "node_modules"
        if not mods.is_dir():
            continue
        out[prof] = {pkg: (mods / pkg / "package.json").is_file() for pkg in PACKAGES}
    return {"profiles": out, "any": any(v for d in out.values() for v in d.values())}


def _names(bots: Dict[str, Any]) -> Dict[str, str]:
    """id → 显示名。**非字典条目也要兜住**：state.json 是别的进程写的，
    坏一条就整页 500 是本仓最禁的失败形态（测试 test_malformed_children 钉这条）。"""
    out: Dict[str, str] = {}
    for bid, b in bots.items():
        name = b.get("name") if isinstance(b, dict) else None
        out[bid] = str(name or bid)
    return out


def overview(limit: int = DEFAULT_LIMIT) -> Dict[str, Any]:
    """把 state.json 投影成页面要的形状。纯函数式：只依赖磁盘。"""
    p = state_path()
    read = _read_state(p)
    base: Dict[str, Any] = {
        "state_path": str(p),
        "dsh_home": str(dsh_home()),
        "install": _installed(),
    }
    if not read["ok"]:
        return {**base, "ok": False, "found": read["found"], "error": read["error"],
                "counts": {"bots": 0, "mains": 0, "rooms": 0, "exchanges": 0},
                "bots": [], "rooms": [], "exchanges": []}
    if not read["found"]:
        return {**base, "ok": True, "found": False,
                "counts": {"bots": 0, "mains": 0, "rooms": 0, "exchanges": 0},
                "bots": [], "rooms": [], "exchanges": [],
                "hint": "还没有 %s —— 要么没装 ds-bot，要么还没建过团队" % p}

    data = read["data"]
    bots_raw = data.get("bots") if isinstance(data.get("bots"), dict) else {}
    rooms_raw = data.get("rooms") if isinstance(data.get("rooms"), dict) else {}
    ex_raw = data.get("exchanges") if isinstance(data.get("exchanges"), list) else []
    main_ids = [str(x) for x in (data.get("mainBotIds") or []) if isinstance(x, str)]
    name_of = _names(bots_raw)

    # 每个 Bot 的最近活跃时间与实际交接条数（页面按"谁还在干活"排序用）
    last: Dict[str, int] = {}
    seen: Dict[str, int] = {}
    for e in ex_raw:
        if not isinstance(e, dict):
            continue
        t = _ms(e.get("time"))
        for side in (e.get("from"), e.get("to")):
            if not isinstance(side, str):
                continue
            seen[side] = seen.get(side, 0) + 1
            if t and t > last.get(side, 0):
                last[side] = t

    bots: List[Dict[str, Any]] = []
    for bid, b in bots_raw.items():
        if not isinstance(b, dict):
            continue
        avatar = b.get("avatar") if isinstance(b.get("avatar"), dict) else {}
        bots.append({
            "id": bid,
            "name": name_of.get(bid, bid),
            "role": _text(b.get("role"), 60),
            "main": bid in main_ids,
            "model": b.get("model") or "",
            "effort": b.get("effort") or "",
            "workDir": b.get("workDir") or "",
            "color": b.get("color") or "",
            "avatar": {k: avatar.get(k) for k in ("shape", "image") if avatar.get(k)},
            "createdAt": _ms(b.get("createdAt")),
            "createdBy": b.get("createdBy") or "",
            "lastActive": last.get(bid) or None,
            "exchanges": seen.get(bid, 0),
        })
    # Main Bot 置顶，其余按最近活跃倒序，再按创建时间倒序（稳定、无随机）
    bots.sort(key=lambda x: (not x["main"], -(x["lastActive"] or 0),
                             -(x["createdAt"] or 0), x["name"]))

    rooms: List[Dict[str, Any]] = []
    for rid, r in rooms_raw.items():
        if not isinstance(r, dict):
            continue
        members = [str(m) for m in (r.get("members") or []) if isinstance(m, str)]
        room_last = 0
        for e in ex_raw:
            if isinstance(e, dict) and e.get("roomId") == rid:
                room_last = max(room_last, _ms(e.get("time")) or 0)
        admin = r.get("admin") or ""
        rooms.append({
            "id": rid,
            "name": _text(r.get("name"), 80) or rid,
            "named": bool(r.get("named")),
            "members": [{"id": m, "name": name_of.get(m, m)} for m in members],
            "memberCount": len(members),
            "admin": admin,
            "adminName": name_of.get(admin, admin) if admin else "",
            "mode": r.get("mode") or "",
            "notice": _text(r.get("notice"), 120),
            "createdAt": _ms(r.get("createdAt")),
            "lastActive": room_last or None,
        })
    rooms.sort(key=lambda x: (-(x["lastActive"] or 0), -(x["createdAt"] or 0), x["name"]))

    ex: List[Dict[str, Any]] = []
    for e in ex_raw[-limit:][::-1] if isinstance(ex_raw, list) else []:
        if not isinstance(e, dict):
            continue
        frm, to = e.get("from"), e.get("to")
        ex.append({
            "id": e.get("id") or "",
            "time": _ms(e.get("time")),
            "from": frm or "",
            "fromName": name_of.get(frm, frm or "用户") if frm else "用户",
            "to": to or "",
            "toName": name_of.get(to, to) if to else "",
            "role": e.get("role") or "",
            "roomId": e.get("roomId") or "",
            "text": _text(e.get("text")),
        })

    try:
        mtime = int(p.stat().st_mtime * 1000)
    except OSError:
        mtime = None

    return {
        **base,
        "ok": True,
        "found": True,
        "updated_at": mtime,
        "revision": data.get("revision") if isinstance(data.get("revision"), int) else None,
        "counts": {"bots": len(bots), "mains": len(main_ids),
                   "rooms": len(rooms), "exchanges": len(ex_raw)},
        "bots": bots,
        "rooms": rooms,
        "exchanges": ex,
        "readOnly": True,
        "as_of": int(time.time() * 1000),
    }


@router.get("/api/botboy/overview")
def api_botboy_overview(limit: int = Query(DEFAULT_LIMIT, ge=1, le=MAX_LIMIT)):
    """DS Bot 团队只读概览。与 /api/resources 同口径：只读不鉴权。"""
    return overview(limit)
