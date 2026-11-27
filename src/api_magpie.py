"""magpie UI bridge: one place for the hub frontend to open magpie's own pages.

Why this exists (2026-11-20):
- magpie already owns Memory, Sessions, Library (skills/MCP), Usage and the
  Agents model picker. The hub's own copies are being retired; its frontend
  should send the reader to magpie's implementation instead of drawing a
  second one.
- The magpie web page is key-gated. Returning the key here keeps the hub's
  single sign-in; it is only handed to a loopback caller or a signed-in hub
  user, never to an anonymous LAN client.

Endpoints:
  GET /api/magpie/ui?view=memory|sessions|library|usage|agents|providers&tab=...
      -> {"url": "http://<host>:3430/?k=...&view=...", "view": "..."}
"""
from __future__ import annotations

import http.cookiejar
import ipaddress
import json
import os
import time
import urllib.request
from typing import Optional
from urllib.parse import urlsplit, urlunsplit, urlencode

from fastapi import APIRouter, HTTPException, Query, Request

import users as users_mod

router = APIRouter(tags=["magpie"])

VIEWS = {
    "memory": ("library", "memory"),
    "sessions": ("sessions", ""),
    "library": ("library", ""),
    "skills": ("library", "skills"),
    "mcp": ("library", "mcp"),
    "usage": ("usage", ""),
    "agents": ("agents", ""),
    "providers": ("providers", ""),
}


def _loopback(host: str) -> bool:
    try:
        return ipaddress.ip_address(host).is_loopback
    except ValueError:
        return host in ("localhost", "::1")


def _web_base(request: Request) -> str:
    base = os.getenv("MAGPIE_WEB_URL", "http://127.0.0.1:3430").strip()
    sp = urlsplit(base)
    host = request.url.hostname or ""
    # magpie web binds every address; the browser's host is the one that works
    # from the LAN/Tailscale too. Replace only a loopback host in the default.
    if host and (sp.hostname in ("127.0.0.1", "localhost", "::1")):
        netloc = f"[{host}]" if ":" in host else host
        port = sp.port or 3430
        sp = sp._replace(netloc=f"{netloc}:{port}")
    return urlunsplit(sp)


def _key() -> str:
    return os.getenv("MAGPIE_WEB_KEY", "magpie_hub_embed_key_20261117")


@router.get("/api/magpie/ui")
def magpie_ui(request: Request,
              view: str = Query("memory"),
              tab: Optional[str] = Query(None)):
    if view not in VIEWS:
        raise HTTPException(status_code=400, detail=f"unknown view {view!r}")
    host = request.client.host if request.client else ""
    user = None
    try:
        u, _perms, _how = users_mod.identity_of(request)
        user = u.get("username") if u else None
    except Exception:  # noqa: BLE001 — anonymous is the safe answer
        user = None
    if not _loopback(host) and not user:
        raise HTTPException(status_code=403, detail="sign in to the hub first")

    magpie_view, default_tab = VIEWS[view]
    q = [("k", _key()), ("view", magpie_view)]
    t = tab or default_tab
    if t:
        q.append(("tab", t))
    url = _web_base(request) + "/?" + urlencode(q)
    return {"url": url, "view": magpie_view, "tab": t or "", "signed_in": bool(user)}


#: 30s cache: memory/library counts change slowly; the page asks on every open.
_summary_cache: dict[str, tuple[float, dict]] = {}


@router.get("/api/magpie/status")
def magpie_status():
    """magpie 只读互调状态（模型数 / 各 provider 额度 / 被挡原因 / 档位来源）。

    2026-11-27（T4）从 `tasks.py` 迁来：magpie 相关出口只留一个模块，
    避免同一份状态有两个实现各自漂移。
    """
    import magpie_client
    import tiercfg
    out = magpie_client.status()
    out["tiers_source"] = tiercfg.tiers_source()
    # 2026-11-27（波3 S2）：当前谁在写 agent 模型配置。双写者消除的可见面 ——
    # 不显示这个值，「写者到底是 hub 还是 magpie」只能靠读 .env 猜。
    try:
        import modelcfg
        out["model_writer"] = modelcfg.writer_mode()
        out["model_writer_agents"] = sorted(modelcfg.MAGPIE_WRITABLE)
    except Exception as e:  # noqa: BLE001
        out["model_writer"] = "unknown"
        out["model_writer_error"] = "%s: %s" % (type(e).__name__, e)
    return out


def _read_json(base: str, path: str) -> dict:
    """Read a magpie web API with the page key's cookie (two steps: root, then API)."""
    jar = http.cookiejar.CookieJar()
    op = urllib.request.build_opener(urllib.request.HTTPCookieProcessor(jar))
    op.open(base + "/?" + urlencode({"k": _key()}), timeout=4).read()
    with op.open(base + path, timeout=4) as r:
        return json.loads(r.read().decode("utf-8"))


def _summary(base: str) -> dict:
    mem = _read_json(base, "/api/memory")
    lib = _read_json(base, "/api/library")
    rows = mem.get("memories") or []
    active = sum(1 for m in rows if (m.get("status") or "active") == "active")
    archived = sum(1 for m in rows if m.get("status") == "archived")
    return {
        "memory": {"active": active, "archived": archived, "total": len(rows)},
        "library": {"skills": len(lib.get("skills") or []),
                    "mcp": len(lib.get("servers") or lib.get("mcp") or [])},
        "as_of": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
    }


@router.get("/api/magpie/summary")
def magpie_summary(request: Request):
    host = request.client.host if request.client else ""
    user = None
    try:
        u, _p, _h = users_mod.identity_of(request)
        user = u.get("username") if u else None
    except Exception:  # noqa: BLE001
        user = None
    if not _loopback(host) and not user:
        raise HTTPException(status_code=403, detail="sign in to the hub first")
    base = _web_base(request)
    hit = _summary_cache.get(base)
    if hit and time.time() - hit[0] < 30:
        return hit[1]
    try:
        out = _summary(base)
    except Exception as e:  # noqa: BLE001 — the page says why, never a fake 0
        out = {"error": f"magpie 不可读：{type(e).__name__}"}
    _summary_cache[base] = (time.time(), out)
    return out