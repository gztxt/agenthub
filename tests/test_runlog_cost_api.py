#!/usr/bin/env python3
"""L0 hermetic：`GET /api/runlog/cost` 的鉴权 / 入参校验 / fail-closed（src/runlog.py）。

这个端点读的是 **CCR 账本**（不是 hub 的 profile_events）⇒ 三道钉子：
  ① 鉴权与 /api/runlog 同口径（GET 但按写方法判；账单＝数据外流面），
     且**无凭据不回显凭据**、服务端没配口令必须 503（fail-closed，不是放行）；
  ② `group_by` 只收白名单，`logical_model` 明确 400（2026-09-07 判例）；
  ③ 账本拿不到必须 **503 + ok:false**，绝不回 `{rows:[],total:0}` 那种「静默 0」——
     免费模型 cost_usd 本就是 NULL，静默 0 会让成本看着比实际小。
"""
import asyncio
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db       # noqa: E402
import runlog   # noqa: E402
from fastapi import HTTPException  # noqa: E402

SCHEMA = """
CREATE TABLE usage_events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    created_at TEXT NOT NULL,
    request_id TEXT NOT NULL DEFAULT '',
    client TEXT NOT NULL DEFAULT 'unknown',
    method TEXT NOT NULL,
    path TEXT NOT NULL,
    model TEXT NOT NULL DEFAULT 'unknown',
    logical_model TEXT NOT NULL DEFAULT '',
    provider TEXT NOT NULL DEFAULT 'unknown',
    credential_id TEXT NOT NULL DEFAULT '',
    status_code INTEGER NOT NULL DEFAULT 0,
    duration_ms INTEGER NOT NULL DEFAULT 0,
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cache_read_tokens INTEGER NOT NULL DEFAULT 0,
    cache_write_tokens INTEGER NOT NULL DEFAULT 0,
    total_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL,
    cost_source TEXT NOT NULL DEFAULT ''
);
"""


class _Req:
    def __init__(self, token="", path="/api/runlog/cost"):
        self.method = "GET"
        self.url = types.SimpleNamespace(path=path, query="")
        self.headers = types.SimpleNamespace(
            raw=[(b"x-hub-token", token.encode())] if token else [])
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="user:term-token")


class TestRunlogCostApi(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0runlogcost-"))
        self.ledger = self.tmp / "usage.sqlite"
        conn = sqlite3.connect(str(self.ledger))
        conn.executescript(SCHEMA)
        conn.executemany(
            "INSERT INTO usage_events(created_at,request_id,client,model,provider,"
            "method,path,total_tokens,cost_usd,cost_source) VALUES(?,?,?,?,?,?,?,?,?,?)",
            [("2026-10-14T10:00:00.000Z", "r1", "API Key 1", "deepseek-v4.1-flash",
              "alibaba", "POST", "/v1/messages", 100, 0.5, "models.dev"),
             ("2026-10-14T11:00:00.000Z", "r2", "Local Gateway", "m:free",
              "openrouter", "POST", "/v1/messages", 20, None, "")])
        conn.commit()
        conn.close()
        # db 只被 runlog 的查询端点用，这里给个干净 tmp 库，避免沾到生产主库
        self._saved_conn = db._conn
        db.init_db(self.tmp / "hub.db")
        self._saved_env = {k: os.environ.get(k) for k in ("CCR_USAGE_DB",)}
        os.environ["CCR_USAGE_DB"] = str(self.ledger)
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved_conn
        for k, v in self._saved_env.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, token="good", **kw):
        args = {"request": _Req(token), "group_by": "client,model", "window": 0, "limit": 100}
        args.update(kw)
        return asyncio.run(runlog.runlog_cost(**args))

    def _env(self, term="good", passcode=""):
        return mock.patch.dict(os.environ, {"TERM_TOKEN": term, "HUB_PASSCODE": passcode})

    # ① 鉴权 --------------------------------------------------------------
    def test_no_token_is_401_and_silent(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
        self.assertEqual(cm.exception.status_code, 401)
        self.assertNotIn("good", str(cm.exception.detail))

    def test_wrong_token_is_401(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="bad")
        self.assertEqual(cm.exception.status_code, 401)

    def test_unconfigured_is_503_not_allowed(self):
        with self._env(term="", passcode=""):
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
        self.assertEqual(cm.exception.status_code, 503, "fail-closed：没设口令不等于不用口令")

    # 正常路径 ------------------------------------------------------------
    def test_authorized_returns_aggregate(self):
        with self._env():
            d = self._call()
        self.assertTrue(d["ok"], d)
        self.assertEqual(d["mode"], "ro")
        self.assertEqual(d["attribution"], runlog.usage_ledger.ATTRIBUTION)
        self.assertEqual(d["total"]["requests"], 2)
        self.assertEqual(d["total"]["null_cost"], 1, "NULL cost 必须显式回")
        self.assertEqual(d["group_by"], ["client", "model"])

    def test_group_by_is_whitelisted(self):
        with self._env():
            d = self._call(group_by="provider")
        self.assertEqual(d["group_by"], ["provider"])
        self.assertEqual(d["count"], 2)

    # ② 入参校验 ----------------------------------------------------------
    def test_logical_model_group_by_is_400(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(group_by="logical_model")
        self.assertEqual(cm.exception.status_code, 400)
        self.assertIn("logical_model", str(cm.exception.detail))

    def test_empty_group_by_is_400(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(group_by=" , ")
        self.assertEqual(cm.exception.status_code, 400)

    # ③ fail-closed -------------------------------------------------------
    def test_missing_ledger_is_503_not_zeros(self):
        os.environ["CCR_USAGE_DB"] = str(self.tmp / "nope.sqlite")
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call()
        self.assertEqual(cm.exception.status_code, 503)
        self.assertIn("unavailable", str(cm.exception.detail))

    def test_existing_runlog_still_authorized_by_shared_helper(self):
        """/api/runlog 与 /api/runlog/cost 共用 _require_read —— 防「改一处漏一处」。"""
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                asyncio.run(runlog.runlog_query(
                    **{"request": _Req("", path="/api/runlog"), "source": None,
                       "subject": None, "status": None, "window": 0,
                       "limit": 10, "before_id": None}))
        self.assertEqual(cm.exception.status_code, 401)


if __name__ == "__main__":
    unittest.main(verbosity=2)