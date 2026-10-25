"""统一编目只读门面 /api/catalog/*（PT-20261025-12 P0）。

路由只由 main 在 HUB_CATALOG=1 时注册；本模块不写库、不触发扫描。
"""
from __future__ import annotations

import sqlite3
from datetime import datetime

from fastapi import APIRouter, HTTPException, Query

from catalog import query as catalog_query
from catalog import schema, store

router = APIRouter(prefix="/api/catalog", tags=["catalog"])


def _connect(readonly: bool = True) -> sqlite3.Connection:
    try:
        return store.connect(readonly=readonly)
    except (OSError, sqlite3.Error) as exc:
        raise HTTPException(
            503, "编目库不可用：先运行 scripts/hub-catalog-scan.sh（%s）" % exc)


def _domains(value: str) -> str:
    items = [x.strip() for x in (value or "").split(",") if x.strip()]
    bad = sorted(set(items) - set(schema.DOMAIN_TABLE))
    if bad:
        raise HTTPException(400, "未知 domains=%s；可用：%s" % (
            ",".join(bad), ",".join(sorted(schema.DOMAIN_TABLE))))
    return ",".join(items)


def _since(value: str) -> int:
    raw = (value or "").strip()
    if not raw:
        return 0
    try:
        result = int(raw)
    except ValueError:
        try:
            result = int(datetime.fromisoformat(
                raw.replace("Z", "+00:00")).timestamp())
        except (ValueError, OverflowError, OSError):
            raise HTTPException(400, "since 须为 epoch 秒或 ISO-8601 时间")
    if result < 0:
        raise HTTPException(400, "since 不能为负数")
    return result


def _native_oid(con: sqlite3.Connection, domain: str, native_key: str,
                src_id: str) -> str:
    sql = "SELECT oid, src_id FROM obj WHERE domain=? AND native_key=?"
    args = [domain, native_key]
    if src_id:
        sql += " AND src_id=?"
        args.append(src_id)
    rows = con.execute(sql, args).fetchall()
    if not rows:
        raise HTTPException(404, "对象不存在")
    if len(rows) > 1:
        raise HTTPException(409, {
            "error": "对象跨源重名，请补 src_id 或直接用 oid",
            "candidates": [dict(r) for r in rows],
        })
    return rows[0]["oid"]


@router.get("/status")
def status(domain: str = "") -> dict:
    domain = _domains(domain)
    try:
        con = _connect()
    except HTTPException as exc:
        return {"ok": False, "available": False, "error": exc.detail,
                "sources": [], "domains": []}
    try:
        out = catalog_query.status(con)
        if domain:
            # 一个源可产出多域（agents.registry ⇒ agent + service）⇒ 按**对象域**过滤
            out["sources"] = [s for s in out["sources"]
                              if domain in (s.get("object_domains") or {})]
            out["domains"] = [domain]
        out["available"] = True
        return out
    finally:
        con.close()


@router.get("/query")
def search(q: str = Query(min_length=1, max_length=200),
           domains: str = Query(default="", max_length=200),
           types: str = Query(default="", max_length=200),
           k: int = Query(default=10, ge=1, le=100),
           since: str = Query(default="", max_length=64)) -> dict:
    con = _connect()
    try:
        return catalog_query.query(con, q, domains=_domains(domains),
                                   types=types, k=k, since=_since(since))
    finally:
        con.close()


@router.get("/get")
def get(oid: str = Query(default="", max_length=128),
        domain: str = Query(default="", max_length=32),
        native_key: str = Query(default="", max_length=1000),
        src_id: str = Query(default="", max_length=100)) -> dict:
    con = _connect()
    try:
        if not oid:
            if not domain or not native_key:
                raise HTTPException(400, "传 oid，或同时传 domain+native_key")
            oid = _native_oid(con, _domains(domain), native_key, src_id)
        out = catalog_query.get(con, oid)
        if not out.get("ok"):
            raise HTTPException(404, out.get("error") or "对象不存在")
        return out
    finally:
        con.close()
