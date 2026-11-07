#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 测试替身：把「带凭据头的请求」判为已登录。

为什么需要
----------
2026-11-07 用户裁定删除服务端共享 token 兼容面后，真实 `users.identity_of()`
只认两样东西：HttpOnly 会话 Cookie（`hub_session`）和 HTTP Basic 的**真实
用户名+密码**（查 users 表 + pbkdf2 校验）。

L0 是空 HOME / 临时 DB 环境，既不会有会话行，也不该让每个用例去建用户 +
跑一次 pbkdf2（那是 200k 迭代，单例 60ms 起）。而 150 条既有用例的夹具本来就是
「带一个凭据头 = 已登录」的语义。

所以这里只做一件事：**在测试进程内**替换 `users.identity_of`，把
`Authorization` / `x-hub-token` / `x-term-token` 任一头的存在判为已登录，
其余照旧匿名。生产代码一行不改，也不引入任何 env 开关 —— 替身只活在
`tests/` 目录里，被测试模块显式 import 才生效。

⚠ 只 import 不 install，需要在模块级调用 `install()`；
   `install()` 幂等，重复调用无副作用。
"""
from __future__ import annotations

USER = {"id": 1, "username": "l0-test", "display": "L0 测试用户",
        "is_admin": 1, "created_at": "", "updated_at": ""}

_CRED_HEADS = ("authorization", "x-hub-token", "x-term-token")
_installed = False
_REAL = None


def real_identity_of(request):
    """未打替身的真实解析器（给「兼容面确已删除」这类用例取证用）。"""
    if _REAL is None:
        raise RuntimeError("authfix 未 install()，没有保存真实 identity_of")
    return _REAL(request)


def _has_cred(request) -> bool:
    """请求是否带了任一凭据头（raw 列表 / dict / Starlette Headers 三种形态）。"""
    headers = getattr(request, "headers", None)
    if headers is None:
        return False
    raw = getattr(headers, "raw", None)
    if raw:
        for k, _v in raw:
            name = k.decode("latin-1") if isinstance(k, (bytes, bytearray)) else str(k)
            if name.lower() in _CRED_HEADS:
                return True
    if isinstance(headers, dict):
        return any(str(k).lower() in _CRED_HEADS for k in headers)
    try:
        return any(str(k).lower() in _CRED_HEADS for k in headers.keys())
    except Exception:  # noqa: BLE001
        return False


def identity_of(request):
    """替身：有凭据头 → 已登录；否则匿名。返回值形状与 users.identity_of 一致。"""
    if _has_cred(request):
        return dict(USER), None, "basic"
    return None, None, "anonymous"


def install() -> None:
    """把替身装进 users 模块（幂等）。测试模块 import 时调用。"""
    global _installed, _REAL
    if _installed:
        return
    import users as _users
    _REAL = _users.identity_of
    _users.identity_of = identity_of
    _installed = True
