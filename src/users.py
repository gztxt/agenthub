# -*- coding: utf-8 -*-
"""用户与按功能授权模块（v0.13.x，2026-11-07 用户裁定新增）。

背景：此前全 hub 只有两把共享 token（TERM_TOKEN / HUB_PASSCODE，在 .env），
2026-11-07 C 档已把两把共享 token 全部废弃，全站只认本模块的用户登录态。本模块引入：

- 表 users / user_credentials / user_perms（db.init_db 的 SCHEMA 幂等建表）
  users:            用户主档（username 唯一、is_admin）
  user_credentials: 每用户多 token（只存 sha256，绝不存明文/可逆）
  user_perms:       每用户按「菜单节点 id」授权（module = NAV 节点，见 MODULES）

鉴权口径（writeauth 集成点）：
- token 校验顺序：先查 user_credentials（sha256 匹配）→ 命中则该请求身份 =
  user:<username>，perms = 该用户 user_perms 集合；
- 查不到即视为未登录（共享 token 回落已删除；任何残留调用点都是 bug）。
- perms 注入 request.state（actor 保持既有「凭据名」口径不变，另加 state.user /
  state.user_perms，**绝不回显 token 值**）。

模块清单（授权粒度 = 左侧菜单树，2026-11-08 用户裁定：授权页直接呈现菜单树、
含子菜单，勾选即「显示」、取消即「整项不显示」）：
  顶栏: classroom / localprojects / github / resources（4 个叶子）
  分组: group:agents / group:infra / group:system / group:settings（组头本身也可授权）
  AGENTS: 每个 agent（kind=agent）动态一条（module = agent.id）
  基础设施: 每个非 agent 实体一条（module = 实体 id，含 parent 子项）
  系统: SYS_PAGES 10 项
  设置: SET_PAGES 6 项
前端菜单树由 05-chat-and-history.js 的 authMenuTreeHtml() 从**实时菜单源**
（AGENTS / SYS_PAGES / SET_PAGES / NAV_*）生成，保证与真实侧栏逐项一致；
后端只负责白名单校验，不再维护一份平行的「授权名单」清单。

admin 用户：绕过 perms 过滤（全模块可见）；普通用户按 user_perms。
路由面（全部走 write_gate，无豁免）：
  GET  /api/users                     列表（admin 才可见全量；普通用户只见自己概览）
  POST /api/users                     建用户（admin）
  POST /api/users/{name}/credentials  发 token（admin；token 只在回包出现一次）
  POST /api/users/{name}/revoke       吊销某 token（admin）
  PUT  /api/users/{name}/perms        整体替换该用户的模块授权（admin）
  DELETE /api/users/{name}            删用户（admin；有未吊销 token 时拒）
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import secrets as _secrets
import time as _time
from typing import Dict, List, Optional, Tuple

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel, Field

import db

router = APIRouter()

#: 登录限速（进程内；单机自用，重启清零——同 approvals 存储纪律）
_login_fail: Dict[str, list] = {}

MODULE_TOP = ["classroom", "localprojects", "github", "resources"]
MODULE_SYS = ["ports", "telemetry", "memory", "skills", "kb", "mcp", "jobs", "tasks",
              "assets", "botboy"]
MODULE_SET = ["settings-model", "settings-github", "settings-token", "settings-logs",
              "settings-users", "settings-modelcalls"]   # 2026-11-07：与前端 SET_PAGES 六项对齐
#: 2026-11-08：左侧手风琴的分组本身也可授权（勾组头 = 整组可见）。
#: 前缀 `group:` 与菜单叶子 id 不会撞（叶子 id 均无冒号）。
MODULE_GROUPS = ["group:agents", "group:infra", "group:system", "group:settings"]
# ── 2026-11-07 menu_items seed 常量（与前端 05-chat-and-history.js 的 SYS_PAGES/SET_PAGES
#    逐条对齐，图标取已验证存在过的 sprite id）──
TOP_ICONS = {"classroom": "school", "localprojects": "folder", "github": "globe", "resources": "boxes"}
SYS_ICONS = {"ports": "share", "telemetry": "activity", "memory": "database", "skills": "zap",
             "kb": "book", "mcp": "wrench", "jobs": "clock", "tasks": "flow",
             "assets": "layers", "botboy": "message"}
SET_ICONS = {"settings-model": "cpu", "settings-github": "globe", "settings-token": "terminal",
             "settings-logs": "activity", "settings-users": "eye", "settings-modelcalls": "radar"}
_LABELS = {"classroom": "课堂", "localprojects": "本地项目", "github": "GitHub", "resources": "资源",
           "ports": "端口", "telemetry": "遥测", "memory": "记忆中心", "skills": "技能中心",
           "kb": "知识库", "mcp": "工具", "jobs": "定时", "tasks": "协同", "assets": "资产",
           "botboy": "Botboy", "settings-model": "模型", "settings-github": "GitHub",
           "settings-token": "认证状态", "settings-logs": "日志", "settings-users": "用户授权",
           "settings-modelcalls": "模型计量"}


def _sha(t: str) -> str:
    return hashlib.sha256(t.encode()).hexdigest()


def _user_rows() -> List[dict]:
    return db.query(
        "SELECT u.id, u.username, u.display, u.is_admin, u.created_at, u.updated_at,"
        " (SELECT COUNT(*) FROM user_credentials c WHERE c.user_id=u.id AND c.revoked_at IS NULL) AS live_tokens,"
        " (SELECT COUNT(*) FROM user_perms p WHERE p.user_id=u.id) AS perm_count"
        " FROM users u ORDER BY u.username")


def _perm_set(user_id: int) -> set:
    return {r["module"] for r in db.query("SELECT module FROM user_perms WHERE user_id=?", (user_id,))}


def _require_admin(request: Request) -> dict:
    """写端点必须 admin。普通用户/匿名一律 403（读端点另判）。"""
    actor = getattr(request.state, "actor", "system")
    u = getattr(request.state, "user", None)
    if not (u and u["is_admin"]):
        raise HTTPException(403, "用户管理仅 admin 可写")
    return u


# ── 纯判定：把一次 provided token 解析成 (user 行 or None, perms or None, compat) ──
def resolve_token(provided: str) -> Tuple[Optional[dict], Optional[set], bool]:
    """优先用户 token；命中返回 (user 行, perms, False)；未命中返回 (None, None, True)
    ⇒ 调用方走 .env 兼容口令（全模块）。绝不把 token 值写进日志。"""
    if not provided:
        return None, None, True
    rows = db.query("SELECT u.id, u.username, u.display, u.is_admin,"
                    " c.id AS cred_id FROM user_credentials c JOIN users u ON u.id=c.user_id"
                    " WHERE c.token_hash=? AND c.revoked_at IS NULL", (_sha(provided),))
    if rows:
        r = rows[0]
        perms = None if r["is_admin"] else _perm_set(r["id"])
        return r, perms, False
    return None, None, True


# ── 浏览器会话 Cookie（2026-11-07 用户裁定：Web 端全量用户名+密码认证）──────
#: 会话 Cookie 名。HttpOnly ⇒ JS 读不到，XSS 也偷不走；SameSite=Lax ⇒ 跨站表单
#: 提交不带会话（CSRF 最低门槛），本机/局域网同站请求正常携带。
SESSION_COOKIE = "hub_session"
SESSION_TTL_S = 7 * 24 * 3600


def issue_session(user_row: dict) -> str:
    """签发一个浏览器会话：随机串只进 Cookie，DB 只存 sha256（同 token 存储纪律）。
    cred_type 复用 user_credentials 表，name 前缀 session- 便于改密时整批吊销。"""
    tok = _secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO user_credentials(user_id, token_hash, name, created_at) VALUES(?,?,?,?)",
        (user_row["id"], _sha(tok), "session-" + _secrets.token_hex(2), db._now()))
    return tok


def cookie_session_token(request: Request) -> str:
    """从 Cookie 头取会话值。不用 request.cookies 是为了与 writeauth 的
    raw-header 口径一致（L0 测试里 request 是裸对象，没有 .cookies）。"""
    raw = ""
    try:
        raw = request.headers.get("cookie", "")
    except Exception:  # noqa: BLE001
        return ""
    for part in (raw or "").split(";"):
        k, _, v = part.strip().partition("=")
        if k == SESSION_COOKIE:
            return v
    return ""


def identity_of(request: Request) -> Tuple[Optional[dict], Optional[set], str]:
    """请求身份解析：① Cookie 会话 → ② HTTP Basic（用户名+密码）。
    返回 (user 行, perms, 来源)；来源 ∈ cookie / basic / anonymous。
    这里**不接受** x-hub-token：浏览器面必须走 Cookie，机器面走 Basic。
    旧共享口令的兼容面单独留在 writeauth 里判定，不在这里重新长回 token 路径。"""
    tok = cookie_session_token(request)
    if tok:
        r, p, compat = resolve_token(tok)
        if r and not compat:
            return r, p, "cookie"
        # Cookie 失效不直接判死：继续看 Basic（脚本可能同时带旧 Cookie）
    auth = ""
    try:
        auth = request.headers.get("authorization", "")
    except Exception:  # noqa: BLE001
        return None, None, "anonymous"
    if auth[:6].lower() == "basic ":
        try:
            raw = base64.b64decode(auth[6:].strip()).decode("utf-8")
            name, pw = raw.split(":", 1)
            rows = db.query("SELECT * FROM users WHERE username=?", (name,))
            if rows and rows[0].get("pw_hash") and verify_password(pw, rows[0]["pw_hash"]):
                r = rows[0]
                perms = None if r["is_admin"] else _perm_set(r["id"])
                return r, perms, "basic"
        except Exception:  # noqa: BLE001 —— 坏 Basic 头按匿名处理，不回显任何内容
            pass
    return None, None, "anonymous"



# ── 路由 ──
class UserIn(BaseModel):
    username: str = Field(min_length=2, max_length=32, pattern=r"^[a-z0-9_-]+$")
    display: str = ""
    is_admin: bool = False
    perms: List[str] = []


class PermsIn(BaseModel):
    modules: List[str] = []



# ── 2026-11-07 密码登录（pbkdf2_sha256，无第三方依赖）────────────────────
# 为什么用 pbkdf2 而不是裸 sha256：token 是随机高熵串（sha256 够用），
# 但**用户密码是低熵的**（可能撞字典）⇒ 必须加盐慢哈希。格式
# `pbkdf2_sha256$<iters>$<salt_b64>$<hash_b64>`，整串存 users.pw_hash。
_PBKDF2_ITERS = 200_000


def hash_password(pw: str, iters: int = _PBKDF2_ITERS) -> str:
    salt = _secrets.token_bytes(16)
    h = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, iters)
    return "pbkdf2_sha256$%d$%s$%s" % (iters,
                                        base64.b64encode(salt).decode(),
                                        base64.b64encode(h).decode())


def verify_password(pw: str, stored: str) -> bool:
    """stored 非本格式（含空串=未设密码）⇒ 直接 False，不抛。"""
    try:
        scheme, iters, salt_b, hash_b = stored.split("$")
        if scheme != "pbkdf2_sha256":
            return False
        salt = base64.b64decode(salt_b)
        expect = base64.b64decode(hash_b)
        got = hashlib.pbkdf2_hmac("sha256", pw.encode(), salt, int(iters))
        return hmac.compare_digest(got, expect)
    except Exception:  # noqa: BLE001
        return False


# 超管种子（2026-11-07 用户裁定：内置超级用户 = 系统用户 gztxt）。
# 密码不写进代码/配置（凭据不落盘军规）——首次启动时若 gztxt 用户不存在，
# 按**环境变量** HUB_SUPERADMIN_PW 建；未设则建一个「强制首登改密」占位
# （pw_hash=空串，登录端点拒绝空密码，须先用现有 .env token 走改密端点设一次）。
SUPERADMIN_USER = "gztxt"


def ensure_superadmin() -> None:
    """启动时幂等保证超管存在。无 DB 连接则跳过（startup 在 init_db 后调用）。"""
    import db as _db
    if not _db.is_open():
        return
    rows = _db.query("SELECT id, pw_hash FROM users WHERE username=?", (SUPERADMIN_USER,))
    import os as _os
    pw = _os.getenv("HUB_SUPERADMIN_PW", "")
    if rows:
        # 2026-11-07 修：首次启动若 env 尚未加载，会留下 pw_hash='' 的占位行；
        # 旧实现直接 return ⇒ 之后即使 .env 有密码也永远不会补，登录恒 401。
        if not rows[0].get("pw_hash") and pw:
            _db.execute("UPDATE users SET pw_hash=?, updated_at=? WHERE id=?",
                        (hash_password(pw), _db._now(), rows[0]["id"]))
            _db.log_asset_event("user", SUPERADMIN_USER, "update", "system",
                                {"detail": "superadmin pw backfilled from HUB_SUPERADMIN_PW"})
        return
    pw_hash = hash_password(pw) if pw else ""   # 未设 env ⇒ 空串=强制首登设密
    _db.execute(
        "INSERT INTO users(username, display, is_admin, pw_hash, created_at, updated_at)"
        " VALUES(?,?,1,?, ?, ?)",
        (SUPERADMIN_USER, "超级用户", pw_hash, _db._now(), _db._now()))
    _db.log_asset_event("user", SUPERADMIN_USER, "create", "system",
                        {"detail": "bootstrap superadmin (pw_hash empty = force first-login set)" if not pw else "pw set from HUB_SUPERADMIN_PW"})


class LoginIn(BaseModel):
    username: str
    password: str


class PasswordIn(BaseModel):
    old_password: str = ""
    new_password: str = Field(min_length=8, max_length=128)


@router.post("/api/users/login")
async def login(body: LoginIn, request: Request):
    """密码登录（2026-11-07）。成功 ⇒ 签发 HttpOnly 会话 Cookie（7 天），**不回 token**。
    浏览器之后每个请求由 Cookie 自动携带；脚本/机器客户端走 HTTP Basic（见 identity_of）。
    失败 ⇒ 401（不回显「用户名错/密码错」差异，防用户枚举——统一 401 文案）。"""
    import db as _db
    if not _db.is_open():
        raise HTTPException(503, "用户库未初始化")
    rows = _db.query("SELECT * FROM users WHERE username=?", (body.username,))
    k = request.client.host if request.client else "?"   # 2026-11-07：成功路径也要用来清失败计数
    if not rows or not rows[0].get("pw_hash") or not verify_password(body.password, rows[0]["pw_hash"]):
        # 限速：同 (ip,user) 连续失败 5 次锁 60s（防爆破；进程内，重启清零——
        # 单机自用口径，同 approvals 的进程内存储纪律）
        now = _time.time()
        _login_fail[k] = [t for t in _login_fail.get(k, []) if now - t < 3600]
        _login_fail[k].append(now)
        if len(_login_fail[k]) >= 5 and now - _login_fail[k][0] < 60:
            raise HTTPException(429, "登录失败次数过多，60 秒内请稍后再试")
        raise HTTPException(401, "用户名或密码错误")
    _login_fail.pop(k, None)
    # 签发浏览器会话：值只进 HttpOnly Cookie，DB 只存 sha256；**不回 token**。
    tok = issue_session(rows[0])
    perms = None if rows[0]["is_admin"] else sorted(_perm_set(rows[0]["id"]))
    from starlette.responses import JSONResponse
    resp = JSONResponse({"ok": True, "username": body.username,
                         "is_admin": bool(rows[0]["is_admin"]), "user_perms": perms})
    resp.set_cookie(SESSION_COOKIE, tok, max_age=SESSION_TTL_S, httponly=True,
                    samesite="lax", path="/", secure=(request.url.scheme == "https"))
    _db.log_asset_event("user", body.username, "update", "user:" + body.username,
                        {"detail": "login ok; session cookie issued"})
    return resp


@router.post("/api/users/logout")
async def logout(request: Request):
    """退出登录：吊销当前 Cookie 对应会话并清 Cookie（幂等，未登录也返回 ok）。
    2026-11-07：浏览器面不再持有任何 token ⇒ 退出只需删会话，不涉及本地凭据。"""
    tok = cookie_session_token(request)
    if tok:
        db.execute("UPDATE user_credentials SET revoked_at=? WHERE token_hash=? AND revoked_at IS NULL",
                   (db._now(), _sha(tok)))
    from starlette.responses import JSONResponse
    resp = JSONResponse({"ok": True})
    resp.delete_cookie(SESSION_COOKIE, path="/")
    return resp


@router.post("/api/users/{name}/password")
async def change_password(name: str, request: Request, body: PasswordIn):
    """改密码（**所有用户都可改自己的**，含超管 —— 2026-11-07 用户裁定）。
    自己改：带 old_password 校验；admin 替别人改：old_password 留空即可。"""
    import db as _db
    me = getattr(request.state, "user", None)
    rows = _db.query("SELECT * FROM users WHERE username=?", (name,))
    if not rows:
        raise HTTPException(404, "用户不存在")
    target = rows[0]
    is_self = bool(me and me["username"] == name)
    is_admin = bool(me and me["is_admin"])
    if not (is_self or is_admin):
        raise HTTPException(403, "只能改自己的密码（admin 可替用户改）")
    if is_self:
        # 自己改必须验旧密码（除非还没设过——首设）
        if target.get("pw_hash") and not verify_password(body.old_password, target["pw_hash"]):
            raise HTTPException(401, "旧密码错误")
    _db.execute("UPDATE users SET pw_hash=?, updated_at=? WHERE id=?",
                (hash_password(body.new_password), _db._now(), target["id"]))
    _db.execute(
        "UPDATE user_credentials SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL AND name LIKE 'session-%'",
        (_db._now(), target["id"]))   # 改密后吊销该用户既有会话 token（防旧会话续用）
    _db.log_asset_event("user", name, "update", ("user:%s" % (me["username"] if me else "system")),
                        {"detail": "password changed; session tokens revoked"})
    return {"ok": True, "username": name}


@router.post("/api/users/{name}/password-clear")
async def clear_password(name: str, request: Request):
    """清掉密码（回退到「只能走 token」形态）。仅 admin。
    场景：某用户忘了想重置到 token 登录，或超管想把自己切回 token。"""
    import db as _db
    me = _require_admin(request)
    rows = _db.query("SELECT id FROM users WHERE username=?", (name,))
    if not rows:
        raise HTTPException(404, "用户不存在")
    _db.execute("UPDATE users SET pw_hash='', updated_at=? WHERE id=?",
                (_db._now(), rows[0]["id"]))
    return {"ok": True, "username": name, "note": "该用户现在只能走 token 登录"}


def _validate_perms(perms: List[str], entity_ids: List[str]) -> List[str]:
    """白名单过滤：未知模块静默丢弃（防前端漂移打出假节点）。
    entity_ids = 在册实体 id（agent + 基础设施），2026-11-08 起两者都可授权——
    此前只收 kind='agent'，基础设施永远无法授权，普通用户侧栏里那一组恒为空。"""
    db_modules = _menu_modules()
    known = set(MODULE_TOP + MODULE_SYS + MODULE_SET + MODULE_GROUPS + entity_ids + db_modules)
    return [m for m in perms if m in known]


def _entity_ids() -> List[str]:
    """可授权的实体节点 = 侧栏全部实体（内置画像 + 注册表），与 /api/agents 同源。
    ⚠️ 不能用 custom_agents 表：内置 agent（claude/pi/dsh…）与基础设施不在该表里，
    拿它当白名单会把它们整组挡掉（2026-11-08 实测：只授 group:agents+claude 时
    perms 回包里 claude 被静默丢弃）。"""
    try:
        import profiles as _profiles
        return [p["id"] for p in _profiles.all_profiles(include_blocked=True) if p.get("id")]
    except Exception:  # 极早期/画像不可读
        return []


def seed_menu_items() -> int:
    """首次把硬编码菜单清单灌进 menu_items 表（幂等：只插不存在的行，不覆盖已改过的行）。
    表有数据时自动跳过全部插入（不重刷，防覆盖人工改过的 label/order）。"""
    import db as _db
    existing = set(_menu_modules())
    now = _db._now()
    n = 0
    for grp, mods, icons in (("top", MODULE_TOP, TOP_ICONS),
                             ("system", MODULE_SYS, SYS_ICONS),
                             ("settings", MODULE_SET, SET_ICONS)):
        for i, m in enumerate(mods):
            if m in existing:
                continue
            _db.execute(
                "INSERT OR IGNORE INTO menu_items(module, label, icon, grp, order_no, enabled,"
                " created_at, updated_at) VALUES(?,?,?,?,?,1,?,?)",
                (m, _LABELS.get(m, m), icons.get(m, ""), grp, i, now, now))
            n += 1
    return n


def _menu_modules() -> List[str]:
    """menu_items 表里已启用的模块 id。表不存在/空 ⇒ 返回 []（调用方回落硬编码）。"""
    try:
        return [r["module"] for r in db.query(
            "SELECT module FROM menu_items WHERE enabled=1 ORDER BY order_no, module")]
    except Exception:  # 表尚未建（极早期）
        return []


@router.get("/api/users")
async def list_users(request: Request):
    """admin 见全量；普通用户见自己的概览（不含别人的 token）。
    2026-11-08：不再回 `modules` 平铺清单 —— 授权页改为直接渲染左侧菜单树
    （前端 authMenuTreeHtml 从实时菜单源生成），后端只做白名单校验。"""
    u = getattr(request.state, "user", None)
    if u and not u["is_admin"]:
        mine = [r for r in _user_rows() if r["username"] == u["username"]]
        return {"rows": mine, "self": u["username"], "admin": False}
    return {"rows": _user_rows(), "admin": True}


@router.post("/api/users")
async def create_user(request: Request, body: UserIn):
    me = _require_admin(request)
    if db.query("SELECT 1 FROM users WHERE username=?", (body.username,)):
        raise HTTPException(409, f"用户名 {body.username} 已存在")
    agents = _entity_ids()
    perms = _validate_perms(body.perms, agents)
    rid = db.execute_insert(
        "INSERT INTO users(username, display, is_admin, created_at, updated_at) VALUES(?,?,?,?,?)",
        (body.username, body.display or body.username, 1 if body.is_admin else 0,
         db._now(), db._now()))
    if perms:
        db.execute_script(";\n".join(
            f"INSERT OR IGNORE INTO user_perms(user_id, module) VALUES({rid}, '{m}')" for m in perms) + ";")
    db.log_asset_event("user", body.username, "create", f"user:{me['username']}",
                       {"is_admin": bool(body.is_admin), "perms": perms})
    return {"ok": True, "id": rid, "username": body.username, "perms": perms}


@router.get("/api/users/whoami")
async def whoami(request: Request):
    """前端启动时调用：当前凭据解析出的用户 + 可见模块清单（全模块 = null）。
    2026-11-07：Cookie 会话由 read_gate 注入 state.user；若上游没注入
    （例如回环匿名面），这里再自行解析一次，保证闸门能拿到真实身份。"""
    u = getattr(request.state, "user", None)
    up = getattr(request.state, "user_perms", None)
    if u is None:
        try:
            u, up, _how = identity_of(request)
        except Exception:  # noqa: BLE001
            u, up = None, None
    if u is None:
        return {"username": None, "is_admin": False, "user_perms": None}
    return {"username": u["username"], "is_admin": bool(u.get("is_admin", 0)),
            "user_perms": sorted(up) if up is not None else None}


@router.get("/api/users/{name}")
async def user_detail(name: str, request: Request):
    u = getattr(request.state, "user", None)
    rows = [r for r in _user_rows() if r["username"] == name]
    if not rows:
        raise HTTPException(404, "用户不存在")
    if not (u and u["is_admin"]):
        # 普通用户只能查自己
        if not (u and u["username"] == name):
            raise HTTPException(403, "只能查看自己的授权")
    pid = rows[0]["id"]
    perms = _perm_set(pid)
    creds = db.query("SELECT id, name, created_at FROM user_credentials WHERE user_id=? AND revoked_at IS NULL", (pid,))
    return {"username": name, "is_admin": bool(rows[0]["is_admin"]), "perms": sorted(perms),
            "live_tokens": [{"id": c["id"], "name": c["name"], "created_at": c["created_at"]} for c in creds]}


@router.post("/api/users/{name}/credentials")
async def issue_token(name: str, request: Request):
    me = _require_admin(request)
    rows = [r for r in _user_rows() if r["username"] == name]
    if not rows:
        raise HTTPException(404, "用户不存在")
    tok = _secrets.token_urlsafe(32)
    db.execute(
        "INSERT INTO user_credentials(user_id, token_hash, name, created_at) VALUES(?,?,?,?)",
        (rows[0]["id"], _sha(tok), f"api-{_secrets.token_hex(2)}", db._now()))
    db.log_asset_event("user", name, "create", f"user:{me['username']}",
                       {"detail": "issued credential (plaintext only in this response)"})
    return {"ok": True, "token": tok,
            "note": "仅此一次可见；请让用户妥善保管（写入各工具链客户端配置），重发只能先吊销再新建"}


@router.post("/api/users/{name}/revoke")
async def revoke(name: str, request: Request, body: Optional[PermsIn] = None):
    me = _require_admin(request)
    rows = [r for r in _user_rows() if r["username"] == name]
    if not rows:
        raise HTTPException(404, "用户不存在")
    pid = rows[0]["id"]
    n = db.execute("UPDATE user_credentials SET revoked_at=? WHERE user_id=? AND revoked_at IS NULL",
                   (db._now(), pid))
    db.log_asset_event("user", name, "update", f"user:{me['username']}", {"revoked": n})
    return {"ok": True, "revoked": n}


@router.put("/api/users/{name}/perms")
async def set_perms(name: str, request: Request, body: PermsIn):
    me = _require_admin(request)
    rows = [r for r in _user_rows() if r["username"] == name]
    if not rows:
        raise HTTPException(404, "用户不存在")
    agents = _entity_ids()
    perms = _validate_perms(body.modules, agents)
    db.execute("DELETE FROM user_perms WHERE user_id=?", (rows[0]["id"],))
    if perms:
        db.execute_script(";\n".join(
            f"INSERT OR IGNORE INTO user_perms(user_id, module) VALUES({rows[0]['id']}, '{m}')"
            for m in perms) + ";")
    db.log_asset_event("user", name, "update", f"user:{me['username']}", {"perms": perms})
    return {"ok": True, "perms": perms}


@router.delete("/api/users/{name}")
async def delete_user(name: str, request: Request):
    me = _require_admin(request)
    rows = [r for r in _user_rows() if r["username"] == name]
    if not rows:
        raise HTTPException(404, "用户不存在")
    if rows[0]["live_tokens"]:
        raise HTTPException(409, f"该用户还有 {rows[0]['live_tokens']} 个未吊销 token，先吊销再删")
    db.execute("DELETE FROM user_credentials WHERE user_id=?", (rows[0]["id"],))
    db.execute("DELETE FROM user_perms WHERE user_id=?", (rows[0]["id"],))
    db.execute("DELETE FROM users WHERE id=?", (rows[0]["id"],))
    db.log_asset_event("user", name, "delete", f"user:{me['username']}")
    return {"ok": True}
