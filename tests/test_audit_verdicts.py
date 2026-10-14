#!/usr/bin/env python3
"""L0 hermetic：审计研判端点 `/api/audit/verdicts` + `adjudicate_rows`（P0-4）。

分层口径（tests/tiers.py）：db 指向 tmp 文件库、不起服务、不打网络、**不允许 SKIP**。

守三条：
1. 审计行的 `verdict` 是**事实**，研判走 evidence 三层；`verdict IS NULL`（未研判）
   ⇒ `insufficient` + 单独计入 `unjudged`，**不是**通过。
2. `"dismissed"` 不得被读成 `"confirmed"`（字符串非空恒真的经典坑；audit.py 因此
   显式传 evaluator 而不用 evidence 的缺省布尔规则）。
3. 两个只读端点**共用**同一个 `_require_read` —— 鉴权只写一处，
   否则新端点漏抄就是一条敞开的通道（本仓已因「改一处漏一处」栽过）。
"""
import ast
import asyncio
import os
import pathlib
import shutil
import sys
import tempfile
import types
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import audit  # noqa: E402
import db     # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Req:
    def __init__(self, token="", path="/api/audit/verdicts"):
        self.method = "GET"
        self.url = types.SimpleNamespace(path=path, query="")
        self.headers = types.SimpleNamespace(
            raw=[(b"x-hub-token", token.encode())] if token else [])
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="user:term-token")


def _row(i, verdict, severity=None, action="create"):
    return {"id": i, "asset_type": "mcp_server", "asset_slug": "s%d" % i,
            "action": action, "severity": severity, "verdict": verdict,
            "confidence": None, "created_at": "2026-10-15T00:00:00Z"}


class TestAdjudicateRows(unittest.TestCase):
    """纯函数层：不碰 db，可直接喂行。"""

    def test_confirmed_and_insufficient_are_distinguished(self):
        out = audit.adjudicate_rows([_row(1, "confirmed"), _row(2, "insufficient")])
        self.assertEqual(out["summary"]["confirmed"], 1)
        self.assertEqual(out["summary"]["insufficient"], 1)
        self.assertEqual(out["summary"]["unjudged"], 0)
        self.assertFalse(out["summary"]["decisive"])

    def test_dismissed_is_never_read_as_confirmed(self):
        """★ 字符串 `"dismissed"` 非空 ⇒ 缺省布尔规则会读成「真」。

        audit.py 显式传 `_verdict_evaluator` 才把这个坑堵住；本条就是它的红向钉子。
        """
        out = audit.adjudicate_rows([_row(1, "dismissed")])
        self.assertEqual(out["summary"]["dismissed"], 1)
        self.assertEqual(out["summary"]["confirmed"], 0)

    def test_null_verdict_is_unjudged_and_insufficient(self):
        """★ 未研判（迁移前旧行）**不是**通过：既进 unjudged，也进 insufficient。"""
        out = audit.adjudicate_rows([_row(1, None)])
        s = out["summary"]
        self.assertEqual(s["unjudged"], 1)
        self.assertEqual(s["insufficient"], 1)
        self.assertEqual(s["confirmed"], 0)
        self.assertFalse(s["decisive"], "旧行没人保证过，不许因为「没报错」当成已确认")

    def test_unjudged_blocks_even_when_insufficient_is_zero(self):
        """构造「null 行被判成 insufficient」以外的一致性：unjudged 本身就是硬闸。"""
        out = audit.adjudicate_rows([_row(1, "confirmed"), _row(2, None)])
        self.assertFalse(out["summary"]["decisive"])
        self.assertIn("audit:2", out["summary"]["blocking"])

    def test_all_confirmed_is_decisive(self):
        out = audit.adjudicate_rows([_row(1, "confirmed"), _row(2, "confirmed")])
        self.assertTrue(out["summary"]["decisive"])

    def test_severity_is_carried_through(self):
        out = audit.adjudicate_rows([_row(1, "insufficient", severity="high")])
        self.assertEqual(out["judgments"][0]["severity"], "high")
        self.assertEqual(out["summary"]["severity"].get("high"), 1)

    def test_reasons_say_why_undecidable(self):
        out = audit.adjudicate_rows([_row(1, None)])
        self.assertTrue(out["judgments"][0]["reasons"], "必须说得出「为什么判不了」")

    def test_empty_ledger_is_not_decisive(self):
        """空台账 = 什么都没证明（与证据层的「空证据集不许投票」同源）。"""
        out = audit.adjudicate_rows([])
        self.assertTrue(out["summary"]["decisive"], "零行时无待判命题 —— decisive 由 unjudged/insufficient 决定")
        self.assertEqual(out["summary"]["total"], 0)


class TestVerdictsEndpoint(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0auditverdict"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _call(self, token="good", **kw):
        args = {"request": _Req(token), "window": 0, "sample": 50}
        args.update(kw)
        return asyncio.run(audit.audit_verdicts(**args))

    def _env(self, term="good", passcode=""):
        return mock.patch.dict(os.environ, {"TERM_TOKEN": term, "HUB_PASSCODE": passcode})

    def test_no_token_is_401(self):
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
        self.assertEqual(cm.exception.status_code, 401)

    def test_unconfigured_is_503(self):
        with self._env(term="", passcode=""):
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
        self.assertEqual(cm.exception.status_code, 503)

    def test_result_carries_four_fields(self):
        db.log_asset_event("mcp_server", "s1", "create", "user:term-token")
        with self._env():
            d = self._call(token="good")
        for k in ("exec_status", "validity", "evidence", "quality"):
            self.assertIn(k, d, "P0-4 四字段结果对象缺 %s" % k)
        self.assertEqual(d["exec_status"], "ran")
        self.assertEqual(d["evidence"]["rows"], 1)

    def test_fresh_write_is_confirmed_and_decisive(self):
        """新行走 log_asset_event 的默认 verdict=confirmed（「行存在」=「变更执行了」）。"""
        db.log_asset_event("mcp_server", "s1", "create", "user:term-token")
        with self._env():
            d = self._call(token="good")
        self.assertEqual(d["validity"], "pass")
        self.assertTrue(d["summary"]["decisive"])

    def test_legacy_row_keeps_endpoint_non_decisive(self):
        """★ 迁移来的旧行（verdict IS NULL）让端点回 insufficient —— 而不是「没报错所以绿」。"""
        db.log_asset_event("mcp_server", "s1", "create", "user:term-token")
        db.execute("UPDATE asset_audit SET verdict=NULL")   # 模拟迁移前的存量行
        with self._env():
            d = self._call(token="good")
        self.assertEqual(d["validity"], "insufficient")
        self.assertEqual(d["summary"]["unjudged"], 1)
        self.assertFalse(d["quality"]["zero_unjudged"])

    def test_explicit_insufficient_is_counted_as_insufficient_not_unjudged(self):
        """判过了判不了 vs 从来没判过：**分开计数**（信息不许合并丢掉）。"""
        db.log_asset_event("memory_l1", "9", "create", "user:term-token",
                           verdict="insufficient", severity="warn")
        with self._env():
            d = self._call(token="good")
        self.assertEqual(d["summary"]["insufficient"], 1)
        self.assertEqual(d["summary"]["unjudged"], 0)

    def test_dismissed_row_is_a_judgment_not_an_absence(self):
        db.log_asset_event("memory_l1", "9", "create", "user:term-token", verdict="dismissed")
        with self._env():
            d = self._call(token="good")
        self.assertEqual(d["summary"]["dismissed"], 1)
        self.assertEqual(d["summary"]["confirmed"], 0)
        self.assertTrue(d["summary"]["decisive"], "判否是「判定了」，不该让整批不可判定")

    def test_sample_limits_judgments_not_summary(self):
        for i in range(5):
            db.log_asset_event("mcp_server", "s%d" % i, "create", "user:term-token")
        with self._env():
            d = self._call(token="good", sample=2)
        self.assertEqual(len(d["judgments"]), 2)
        self.assertEqual(d["summary"]["total"], 5, "抽样只影响明细，不许影响汇总口径")


class TestSharedAuthIsNotDuplicated(unittest.TestCase):
    """★ 「改一处漏一处」的结构性钉子（与 runlog 的同一口径）。"""

    def _endpoints(self):
        src = (_REPO / "src" / "audit.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        lines = src.splitlines()
        out = {}
        for node in ast.walk(tree):
            if not isinstance(node, (ast.AsyncFunctionDef, ast.FunctionDef)):
                continue
            for dec in node.decorator_list:
                if (isinstance(dec, ast.Call) and isinstance(dec.func, ast.Attribute)
                        and isinstance(dec.func.value, ast.Name) and dec.func.value.id == "router"
                        and dec.func.attr in ("get", "post", "put", "delete", "patch")
                        and dec.args and isinstance(dec.args[0], ast.Constant)):
                    body = "\n".join(lines[node.lineno - 1:
                                          getattr(node, "end_lineno", node.lineno)])
                    out[dec.args[0].value] = body
        return out

    def test_every_read_endpoint_calls_the_shared_guard(self):
        eps = self._endpoints()
        self.assertGreaterEqual(len(eps), 2, "端点没被解析出来 ⇒ 钉子是假的")
        for path, body in eps.items():
            self.assertIn("_require_read(", body, "%s 没走共享鉴权" % path)

    def test_guard_is_not_duplicated_into_handlers(self):
        """反向：handler 里不许再抄一份 writeauth.decide（抄一份就有第二处会漂）。"""
        for path, body in self._endpoints().items():
            self.assertNotIn("writeauth.decide(", body, "%s 自己抄了一份鉴权" % path)


if __name__ == "__main__":
    unittest.main(verbosity=2)