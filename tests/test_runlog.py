#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：运行日志（src/runlog.py）——三中心检索留痕 + /api/runlog 查询门面。

为什么这些用例必须存在：
1. **埋点绝不打断业务**：db.execute 炸掉时端点必须照常 200——审计是附加价值不是
   准入条件（本仓纪律：print 不静默，但也绝不 raise）。
2. **失败路径语义不变**：装饰器对 HTTPException 只留痕后原样 re-raise，
   404/409/400 的诊断正文一个字都不许动（hub 刚做好的失败表态不能再剥一次）。
3. **鉴权 fail-closed**：GET /api/runlog 按写方法判（运行日志含查询词可反推意图），
   无凭据 401、未配置 503——照抄 /api/audit/list 先例。
4. **通道归因**：x-hub-channel: mcp 头 → channel=mcp（MCP 工具转调与浏览器直连可区分）。
"""
import asyncio
import json
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

import db       # noqa: E402
import hook     # noqa: E402
import runlog   # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _Req:
    """照抄 test_audit_api.py 的桩：runlog_query 只读 method/path/headers/client。"""

    def __init__(self, token="", channel=""):
        self.method = "GET"
        self.url = types.SimpleNamespace(path="/api/runlog", query="token=" + token if token else "")
        headers = []
        if token:
            headers.append((b"x-hub-token", token.encode()))
        if channel:
            headers.append((b"x-hub-channel", channel.encode()))
        _dict = {h[0].decode(): h[1].decode() for h in headers}
        self.headers = types.SimpleNamespace(raw=headers, get=lambda k, d="": _dict.get(k, d))
        self.client = types.SimpleNamespace(host="127.0.0.1")
        self.state = types.SimpleNamespace(actor="user:term-token")


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0runlog-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        self.addCleanup(self._restore)

    def _restore(self):
        db._conn = self._saved
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _rest_rows(self):
        return db.query("SELECT * FROM profile_events WHERE source='rest' ORDER BY id")


class TestTrackDecorator(_Base):
    """track() 装饰器：成功/失败/DB 炸/通道归因四路径。"""

    def _run(self, fn, *args, **kw):
        return asyncio.run(fn(*args, **kw))

    def test_success_row_has_structured_detail(self):
        @runlog.track("mem.search")
        async def fake(request, q, limit=10, sources="local,tdai"):
            return {"count": 2, "backends": [
                {"name": "local", "ok": True, "count": 2},
                {"name": "tdai", "ok": False}]}

        ret = self._run(fake, _Req(), q="端口", limit=5)
        self.assertEqual(ret["count"], 2, "装饰器必须透传返回值")
        rows = self._rest_rows()
        self.assertEqual(len(rows), 1)
        d = json.loads(rows[0]["detail"])
        self.assertEqual(d["q"], "端口")
        self.assertEqual(d["limit"], 5)
        self.assertEqual(d["routes_ok"], ["local"])
        self.assertEqual(d["degraded"], ["tdai"])
        self.assertEqual(d["count"], 2)
        self.assertEqual(d["channel"], "web")
        self.assertEqual(rows[0]["status"], "success")
        self.assertIsNotNone(rows[0]["duration_ms"])

    def test_mcp_channel_recorded(self):
        @runlog.track("kb.search")
        async def fake(request, q):
            return {"count": 0}

        self._run(fake, _Req(channel="mcp"), q="x")
        d = json.loads(self._rest_rows()[0]["detail"])
        self.assertEqual(d["channel"], "mcp", "x-hub-channel: mcp ⇒ channel=mcp")

    def test_fail_path_reraises_and_logs(self):
        @runlog.track("skill.read")
        async def fake(request, name):
            raise HTTPException(404, "未找到技能")

        with self.assertRaises(HTTPException) as cm:
            self._run(fake, _Req(), name="nope")
        self.assertEqual(cm.exception.status_code, 404, "HTTP 语义必须原样保持")
        rows = self._rest_rows()
        self.assertEqual(rows[0]["status"], "fail")
        d = json.loads(rows[0]["detail"])
        self.assertEqual(d["http"], 404)
        self.assertIn("未找到", d["err"])

    def test_db_blowup_does_not_break_request(self):
        """红向钉子：db.execute 炸 ⇒ 端点必须照常返回（埋点≠准入）。"""
        @runlog.track("kb.browse")
        async def fake(request, sub=""):
            return {"entries": [], "count": 0}

        with mock.patch.object(db, "log_profile_event", side_effect=sqlite_blow("disk full")):
            ret = self._run(fake, _Req())
        self.assertEqual(ret, {"entries": [], "count": 0}, "DB 炸不影响业务返回")

    def test_q_scrubbed_before_persist(self):
        @runlog.track("mem.search")
        async def fake(request, q):
            return {"count": 0}
        self._run(fake, _Req(), q="sk" + "-cV3RYsecretTOKENvalue1234567890")  # 运行时拼接：源码不出现 sk- 连写（推前闸门①），值仍为 sk- 形态以验脱敏
        d = json.loads(self._rest_rows()[0]["detail"])
        self.assertNotIn("secretTOKENvalue", str(d), "查询词落库前必须过 scrub")

    def test_q_truncated_to_cap(self):
        @runlog.track("kb.search")
        async def fake(request, q):
            return {"count": 0}
        self._run(fake, _Req(), q="x" * 500)
        d = json.loads(self._rest_rows()[0]["detail"])
        self.assertLessEqual(len(d["q"]), runlog._Q_MAX_CHARS, "长查询词必须截断")

    def test_non_dict_return_still_logs(self):
        @runlog.track("kb.status")
        async def fake(request):
            return {"turbovec": {"available": True}}
        self._run(fake, _Req())
        self.assertEqual(len(self._rest_rows()), 1, "无 backends 的返回也要留痕（降级为只记 q）")


class sqlite_blow(Exception):
    pass


class TestPickNames(unittest.TestCase):
    """`_pick_names`：从各端点返回体里挖技能名。

    它是 `skill_usage.counts()` 能记账的**唯一前提**——`counts()` 每行只取 `detail["name"]`，
    取不到就丢行。所以「哪个端点的返回形状没被认」=「那个端点在僵尸榜上不可见」（判例 103）。
    五种形状必须各有一条：relevant(bm25) / list(items) / names / 裸 list / **read(单对象)**。
    """

    def test_relevant_bm25_shape(self):
        self.assertEqual(
            runlog._pick_names({"bm25": {"items": [{"name": "a"}, {"name": "b"}]}}),
            ["a", "b"])

    def test_list_items_shape(self):
        self.assertEqual(runlog._pick_names({"items": [{"name": "a"}]}), ["a"])

    def test_names_only_shape(self):
        self.assertEqual(runlog._pick_names({"names": ["a", "b"]}), ["a", "b"])

    def test_bare_list_shape(self):
        self.assertEqual(runlog._pick_names(["a", "b"]), ["a", "b"])

    def test_read_single_object_shape(self):
        r"""/api/skill/read 回**单个对象**（顶层直接是 name，**不套 items**）。

        漏认它 ⇒ `skill.read` 事件永远不带 name ⇒ counts() 的 reads 桶恒空
        ⇒「读技能」这件事在僵尸榜上不可见。这是判例 103 的同族缺口，2026-10-10 补。
        """
        self.assertEqual(
            runlog._pick_names({"name": "agent-dispatch", "description": "d", "content": "body"}),
            ["agent-dispatch"])

    def test_read_shape_is_not_covered_by_older_branches(self):
        """红向对照：read 负载不含 bm25/items/names，也不是 list ⇒ 旧四层全不命中。

        改法是把第⑤层(单对象)去掉，这条会翻红（functional 红向，不是只看字符串）。
        这里直接证明「旧四层的判据对 read 负载一个都不成立」，故第⑤层是承重的。
        """
        payload = {"name": "agent-dispatch", "content": "body"}
        self.assertNotIn("bm25", payload)
        self.assertNotIn("items", payload)
        self.assertNotIn("names", payload)
        self.assertNotIsInstance(payload, list)
        self.assertEqual(runlog._pick_names(payload), ["agent-dispatch"])

    def test_wrapper_object_without_name_is_not_an_item(self):
        """顶层没有 name 的包装对象（relevant/list 的响应壳）不能被误当成单个条目。"""
        for shell in ({"q": "x", "n": 3, "total": 365},
                      {"items": [], "count": 0, "degraded": []}):
            self.assertEqual(runlog._pick_names(shell), [])

    def test_dedup_and_length_cap(self):
        self.assertEqual(runlog._pick_names([{"name": "a"}, {"name": "a"}]), ["a"])
        got = runlog._pick_names([{"name": "x" * 200}])
        self.assertEqual(len(got[0]), runlog._NAME_MAX_CHARS)

    def test_unknown_and_bad_input_return_empty(self):
        """任何形状不认识、任何坏输入都必须返回 []（埋点抛异常会带倒业务端点）。"""
        for bad in (None, 42, "str", {}, {"foo": 1}, {"name": 123}, {"name": ""}):
            self.assertEqual(runlog._pick_names(bad), [], f"{bad!r} 应返回 []")


class TestRunlogQuery(_Base):
    """GET /api/runlog：鉴权三态 + 过滤 + 游标翻页。"""

    def _seed(self, n=3):
        for i in range(n):
            runlog._fire(f"kb.search{i % 2 and '.x' or ''}", "success", 10 + i,
                         {"q": f"q{i}", "channel": "web"})
        runlog._fire("mem.search", "fail", 5, {"q": "bad", "channel": "mcp"})

    def _call(self, token="good", **kw):
        # ⚠ 直调 FastAPI handler 时，未传的参数会保持 `Query(...)` 对象本身（**真值**）⇒
        # 新加的 `outcome` 不传就会走进「取值不在词表」分支而 400。故这里必须逐个显式给。
        args = {"request": _Req(token), "source": None, "subject": None,
                "status": None, "outcome": None, "window": 0, "limit": 100,
                "before_id": None}
        args.update(kw)
        return asyncio.run(runlog.runlog_query(**args))

    def _env(self, term="good", passcode=""):
        return mock.patch.multiple(os, environ={**os.environ,
                                                "TERM_TOKEN": term, "HUB_PASSCODE": passcode})

    def test_unauthenticated_401(self):
        self._seed()
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
            self.assertEqual(cm.exception.status_code, 401)

    def test_misconfigured_503_fail_closed(self):
        self._seed()
        with self._env(term="", passcode=""):
            with self.assertRaises(HTTPException) as cm:
                self._call(token="")
            self.assertEqual(cm.exception.status_code, 503, "未配口令必须 fail-closed 503")

    def test_authenticated_returns_events(self):
        self._seed()
        with self._env():
            d = self._call(token="good")
        self.assertEqual(d["count"], 4)
        self.assertIn("rest", d["sources"])
        self.assertIn("mem.search", d["subjects"])
        for e in d["events"]:
            self.assertEqual(e["source"], "rest")

    def test_subject_filter_and_invalid_rejected(self):
        self._seed()
        with self._env():
            d = self._call(token="good", subject="mem.search")
        self.assertEqual(d["count"], 1)
        with self._env():
            with self.assertRaises(HTTPException) as cm:
                self._call(token="good", subject="nope.x")
            self.assertEqual(cm.exception.status_code, 400)

    def test_status_filter(self):
        self._seed()
        with self._env():
            d = self._call(token="good", status="fail")
        self.assertEqual(d["count"], 1)
        self.assertEqual(d["events"][0]["status"], "fail")

    def test_before_id_cursor_pagination(self):
        self._seed(n=5)
        with self._env():
            first = self._call(token="good", limit=3)
            self.assertIsNotNone(first["next_before_id"], "满页才有游标")
            second = self._call(token="good", limit=3, before_id=first["next_before_id"])
        ids1 = {e["id"] for e in first["events"]}
        ids2 = {e["id"] for e in second["events"]}
        self.assertFalse(ids1 & ids2, "游标翻页不得重叠")

    def test_window_filters_old_rows(self):
        from datetime import datetime, timedelta, timezone
        runlog._fire("mem.search", "success", 1, {"q": "old"})
        stale = (datetime.now(timezone.utc) - timedelta(hours=48)).isoformat()
        db.execute("UPDATE profile_events SET created_at=? WHERE detail LIKE '%old%'", (stale,))
        with self._env():
            d = self._call(token="good", window=24)
        for e in d["events"]:
            self.assertGreaterEqual(e["created_at"], (datetime.now(timezone.utc) -
                                                      timedelta(hours=24)).isoformat())


class TestIntegrationSurface(unittest.TestCase):
    """静态断言：埋点接线与连带项——漏一条就是通道盲区。"""

    def setUp(self):
        self.src = _REPO / "src"

    def test_seven_endpoints_instrumented(self):
        """6+1 个检索端点必须都挂 runlog.track（含 mem.context）。"""
        for fname, subject in (("memory.py", "mem.search"), ("memory.py", "mem.context"),
                               ("kb.py", "kb.search"), ("kb.py", "kb.browse"),
                               ("kb.py", "kb.status"), ("skill.py", "skill.list"),
                               ("skill.py", "skill.read")):
            text = (self.src / fname).read_text(encoding="utf-8")
            self.assertIn(f'@runlog.track("{subject}")', text,
                          f"{fname} 缺 {subject} 埋点")

    def test_hubmcp_sends_channel_header(self):
        text = (self.src / "hubmcp.py").read_text(encoding="utf-8")
        self.assertIn('"x-hub-channel": "mcp"', text, "hubmcp._get 必须带通道头")

    def test_hook_profile_excludes_rest(self):
        """连带项：画像聚合排除 source='rest'，防高频检索霸榜。"""
        text = (self.src / "hook.py").read_text(encoding="utf-8")
        self.assertIn("WHERE source != 'rest'", text)

    def test_schema_has_source_index(self):
        text = (self.src / "db.py").read_text(encoding="utf-8")
        self.assertIn("idx_prof_source", text, "/api/runlog 按 source 过滤需要索引")

    def test_main_mounts_runlog_router(self):
        text = (self.src / "main.py").read_text(encoding="utf-8")
        self.assertIn("runlog_mod.router", text)

    def test_noise_sources_not_instrumented(self):
        """防噪声红线：轮询端点不许埋（埋了画像面板被淹）。"""
        for fname in ("main.py",):
            text = (self.src / fname).read_text(encoding="utf-8")
            self.assertNotIn('@runlog.track', text, "main.py 的轮询端点不埋点")


if __name__ == "__main__":
    unittest.main(verbosity=2)
