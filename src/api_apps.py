"""`/api/apps/*`：CLI Apps（应用功能模块）的 REST 面。

数据与行为一律走 `cliapps`（vendored nanobot 应用模块的适配层），本文件只做三件事：
参数校验、鉴权保险带、把子进程类动作甩进线程池。

## 为什么每个写端点都显式 `writeauth.decide_req`

hub 有全局 write_gate 中间件，但历史判例（cloudcli /start、githubprojects /clone、
prefs PUT）都在这之上再判一次：**中间件被 shadow 实例、测试直调、或某天配置错位时，
端点自己还站着**。安装应用 = 往机器上装全局 npm/pip 包，风险面比改偏好高一档，
没有理由比它们更松。

## 为什么全用 `asyncio.to_thread`

一次 `install` 最长 300s、`run` 最长 600s（上游硬上限）。同步 handler 会把 uvicorn
的事件循环连 /health 一起堵住——term_sessions=0 时 watchdog 会判死并触发重启，
等于「点一下装应用，全站断一次」。
"""
from __future__ import annotations

import asyncio
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Request
from pydantic import BaseModel

import cliapps
import writeauth

router = APIRouter(tags=["apps"])


def _gate(request: Request, method: str) -> None:
    verdict, reason = writeauth.decide_req(request, method)
    if verdict not in ("allow", "exempt"):
        raise HTTPException(401 if verdict != "misconfig" else 503, reason)


def _fail(e: Exception) -> HTTPException:
    """把上游/依赖的异常翻成 HTTP。CliAppError 带自己的 status（400/500）。"""
    name = type(e).__name__
    status = getattr(e, "status", None)
    if name == "CliAppError":
        return HTTPException(int(status or 400), str(getattr(e, "message", e)))
    if isinstance(e, cliapps.AppsUnavailable):
        return HTTPException(503, str(e))
    return HTTPException(500, f"{name}: {e}")


class RunBody(BaseModel):
    args: List[str] = []
    json_output: bool = False
    working_dir: Optional[str] = None
    timeout: Optional[int] = None


@router.get("/api/apps/health")
async def apps_health():
    """自检：vendored 可 import / data 可写 / skills 根在不在 / PyYAML 在不在。

    与 `/health` 分开是有意的：`/health` 是服务存活判据，不该因为一个可选模块
    缺依赖而变红——但缺依赖必须**能被看见**，所以这里如实列 notes。"""
    return await asyncio.to_thread(cliapps.health)


@router.get("/api/apps")
async def apps_catalog(refresh: int = 0, cache_only: int = 0):
    """目录（远程注册表 + 本地缓存）。`cache_only=1` 只读本地缓存，不出网。"""
    try:
        return await asyncio.to_thread(
            cliapps.catalog, force_refresh=bool(refresh), cache_only=bool(cache_only))
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.get("/api/apps/installed")
async def apps_installed():
    try:
        return await asyncio.to_thread(cliapps.installed)
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.post("/api/apps/{name}/install")
async def apps_install(name: str, request: Request):
    _gate(request, "POST")
    try:
        return await asyncio.to_thread(cliapps.install, name)
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.post("/api/apps/{name}/update")
async def apps_update(name: str, request: Request):
    _gate(request, "POST")
    try:
        return await asyncio.to_thread(cliapps.update, name)
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.post("/api/apps/{name}/uninstall")
async def apps_uninstall(name: str, request: Request):
    _gate(request, "POST")
    try:
        return await asyncio.to_thread(cliapps.uninstall, name)
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.post("/api/apps/{name}/test")
async def apps_test(name: str, request: Request):
    _gate(request, "POST")
    try:
        return await asyncio.to_thread(cliapps.test_install, name)
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.post("/api/apps/{name}/run")
async def apps_run(name: str, request: Request, body: Optional[RunBody] = None):
    """跑一个已安装应用；回 `{ok, output}`。

    与 MCP 的 `hub_apps_run` 同一实现，区别只是传输层。`working_dir` 一律
    `restrict_to_workspace=True`（`cliapps.run` 里写死），传工作区外的路径会被 400 拒。"""
    _gate(request, "POST")
    b = body or RunBody()
    try:
        out = await asyncio.to_thread(
            cliapps.run, name, b.args, json_output=b.json_output,
            working_dir=b.working_dir, timeout=b.timeout)
        return {"ok": True, "app": name, "output": out}
    except Exception as e:  # noqa: BLE001
        raise _fail(e)


@router.post("/api/apps/bridge")
async def apps_bridge(request: Request):
    """把已安装应用的技能软链进 `技能目录`（幂等修复入口）。

    用途：磁盘上应用已装、但某次安装时软链失败（权限/同名目录），agent 找不到技能。
    这不是「再装一次」，只重做桥接。"""
    _gate(request, "POST")
    try:
        rows = await asyncio.to_thread(cliapps.bridge_all)
        return {"count": len(rows), "items": rows}
    except Exception as e:  # noqa: BLE001
        raise _fail(e)
