#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""批次 C 的 L0 闸门（PT-20261024-08 C3）：统一读取层 + TDAI 查询缓存 + corpus_bytes 口径。

分层口径同 tests/tiers.py：不起服务、不占端口、不打真网络、不 import src.main。
**不允许 SKIP**（出现即分层放错，闸门判 FAIL）。

四条线，每条对着一个**已实测过的故障形态**（不是假想敌）：

1. **datastore 五域登记**（describe）：
   - 五域齐、每域恰好一个 authoritative、每条登记有 id/kind/where/authoritative 四字段；
   - 返回的是**拷贝**：调用方改返回值不得污染模块级 BACKENDS
     （浅拷贝 `list(v)` 只复制 list 不复制内层 dict ⇒ 可被改坏）。
2. **list_sessions 的两个回归**（本批实测抓到，都是静默故障）：
   - 签名漏 `agent_id` 而函数体 4 处引用 ⇒ 一调用就 NameError；
   - 读 `hist.get("sessions")` 而 `list_history` 返回的键是 `items`
     ⇒ 有历史也恒报 0（静默零命中，比 NameError 更贵）。
3. **TDAI 查询缓存**（实测 `tdai_l1` 单路 804ms 占检索总耗时 91%）：
   - 同 query 二次命中且**不再发 HTTP**（record 计数为证）；
   - **失败不缓存**（一次网络抖动不得把「查不到」缓存住 TTL 秒）；
   - TTL 过期真重查；LRU 有界；不同 limit 不串味；
   - 命中返回的是拷贝（改返回值不污染缓存）；
   - `cache_stats` 进 `backend_status`（/health 可观测，不许黑盒）。
4. **corpus_bytes 口径 + meta 落盘**：
   - incremental 时 corpus_bytes 不得退化成「本次变更文件的字节」
     （实测虚低 8.2 倍：30× 膨胀被读成 86×）；
   - meta 必须**commit 后仍可读**（用**新连接**读，不信同一连接的事务视图）
     —— 曾把 `con.commit()` 改丢，files/chunks/corpus_bytes 三条 meta
     在 close 时静默回滚，而 build() 一切正常、notes 全绿。
"""
from __future__ import annotations

import asyncio
import inspect
import json
import os
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import time
import types
import unittest
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")   # noqa: N816

import datastore                                                     # noqa: E402
import db                                                             # noqa: E402
import memindex                                                       # noqa: E402
import tdai_client                                                    # noqa: E402
from memindex import Spec, build                                      # noqa: E402


def run(coro):
    """跑一个协程并**关掉 loop**——不然在 unittest discover 里会污染成全局 ResourceWarning。"""
    loop = asyncio.new_event_loop()
    try:
        return loop.run_until_complete(coro)
    finally:
        loop.close()


def _w(p: pathlib.Path, text: str) -> pathlib.Path:
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# ══ 1. datastore 五域登记 ═══════════════════════════════════════════════

class TestDescribe(unittest.TestCase):
    def test_five_domains_present_with_counts(self):
        d = datastore.describe()
        self.assertEqual(set(d["domains"]),
                         {"agents", "skills", "memory", "kb", "sessions"})
        self.assertEqual(d["counts"]["domains"], 5)
        self.assertEqual(d["counts"]["backends"],
                         sum(len(v) for v in d["domains"].values()))

    def test_every_backend_entry_has_four_fields(self):
        d = datastore.describe()
        for domain, entries in d["domains"].items():
            self.assertGreater(len(entries), 0, f"{domain} 一条登记都没有")
            for b in entries:
                for f in ("id", "kind", "where", "authoritative"):
                    self.assertIn(f, b, f"{domain}/{b.get('id')!r} 缺字段 {f}")
                self.assertIsInstance(b["authoritative"], bool,
                                      f"{domain}/{b['id']} authoritative 必须是布尔")
                self.assertTrue(b["where"].strip(),
                                f"{domain}/{b['id']} where 为空 ⇒ 登记失去意义")

    def test_kinds_from_declared_vocabulary(self):
        allowed = {"table", "file", "service", "code", "remote", "process"}
        for domain, entries in datastore.describe()["domains"].items():
            for b in entries:
                self.assertIn(b["kind"], allowed,
                              f"{domain}/{b['id']} kind={b['kind']!r} 不在声明表内")

    def test_exactly_one_authoritative_per_domain(self):
        """每域必须恰好一个权威源：0 个 ⇒ 「数据在哪」没有答案；
        多个 ⇒ 权威分裂，与「统一入口」的立意相悖。"""
        for domain, entries in datastore.describe()["domains"].items():
            auth = [b["id"] for b in entries if b["authoritative"]]
            self.assertEqual(len(auth), 1,
                             f"{domain} 的 authoritative 应恰好一个，实得 {auth}")

    def test_returns_copies_not_aliases(self):
        """回归：describe() 曾用 `list(v)` 浅拷贝 —— list 是新的，内层 dict 还是
        模块级 BACKENDS 的那些 ⇒ 调用方（路由/测试/前端序列化前的加工）改返回值
        会直接改坏登记表，且**下一次 describe() 看到的就是被改过的**。"""
        d = datastore.describe()
        d["domains"]["memory"][0]["where"] = "POLLUTED"
        d["domains"]["memory"].append({"id": "injected"})
        again = datastore.describe()
        self.assertNotEqual(again["domains"]["memory"][0]["where"], "POLLUTED",
                            "describe() 返回值与 BACKENDS 共享内层 dict ⇒ 登记可被污染")
        self.assertEqual(len(again["domains"]["memory"]),
                         len(datastore.BACKENDS["memory"]))


# ══ 2. list_sessions：两个静默故障的回归 ════════════════════════════════

class TestListSessions(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="datastore-ls-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        db.init_db(self.tmp / "agents.db")        # 隔离 db：绝不碰生产 agents.db

    def test_signature_has_agent_id(self):
        """回归：签名漏 agent_id 而函数体 4 处引用 ⇒ 一调用就 NameError。
        上一轮改这个函数时正是把参数改丢了。"""
        sig = inspect.signature(datastore.list_sessions)
        self.assertIn("agent_id", sig.parameters)
        self.assertIn("cwd", sig.parameters)
        self.assertIn("limit", sig.parameters)

    def test_no_agent_id_does_not_raise(self):
        """默认调用（无 agent_id）必须走通，且只报 hub_chat 一路。"""
        out = run(datastore.list_sessions())
        self.assertTrue(out["ok"], msg=str(out))
        self.assertEqual(out["count"], 0)
        self.assertEqual([b["name"] for b in out["backends"]], ["hub_chat"])

    def test_hub_chat_rows_come_back(self):
        db.execute(
            "INSERT INTO chat_sessions(id, agent_id, title, created_at, updated_at) "
            "VALUES(?,?,?,?,?)",
            ("sess-1", "codebuddy", "标题一", "2026-10-24T00:00:00Z",
             "2026-10-24T00:00:00Z"))
        out = run(datastore.list_sessions())
        hub = [b for b in out["backends"] if b["name"] == "hub_chat"]
        self.assertEqual(len(hub), 1)
        self.assertTrue(hub[0]["ok"], msg=str(hub[0]))
        self.assertEqual(hub[0]["count"], 1, "hub 自己的会话没查出来")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["title"], "标题一")

    def test_agent_home_history_surfaced_not_silent_zero(self):
        """回归：datastore 曾读 `hist.get("sessions")`，而 list_history 返回的键
        是 `items` ⇒ 有历史也恒报 0（静默零命中：ok=True、count=0、无 error）。"""
        fake = {"items": [{"id": "h1", "title": "历史会话"}],
                "cwd": "", "note": ""}
        with mock.patch("sessions_store.list_history", return_value=fake):
            out = run(datastore.list_sessions(agent_id="grok"))
        home = [b for b in out["backends"] if b["name"] == "agent_home"]
        self.assertEqual(len(home), 1, "给了 agent_id 却没有 agent_home 一路")
        self.assertTrue(home[0]["ok"], msg=str(home[0]))
        self.assertEqual(home[0]["count"], 1,
                         "list_history 有 items 却报 0 ⇒ sessions/items 键名又错了")
        self.assertEqual(out["count"], 1)
        self.assertEqual(out["items"][0]["id"], "h1")

    def test_agent_home_failure_is_structured_not_crash(self):
        """家目录读取炸了 ⇒ 该路 ok=False + error，hub_chat 那路照常。"""
        with mock.patch("sessions_store.list_history",
                        side_effect=OSError("boom")):
            out = run(datastore.list_sessions(agent_id="grok"))
        home = [b for b in out["backends"] if b["name"] == "agent_home"]
        self.assertEqual(len(home), 1)
        self.assertFalse(home[0]["ok"])
        self.assertIn("OSError", home[0]["error"])
        self.assertEqual(home[0]["count"], 0)
        # hub_chat 不受影响
        hub = [b for b in out["backends"] if b["name"] == "hub_chat"]
        self.assertTrue(hub[0]["ok"])

    def test_hub_chat_failure_is_structured(self):
        """db.query 炸了（库坏/锁死）⇒ hub_chat 路报错，不整份 500。"""
        with mock.patch.object(db, "query", side_effect=sqlite3.OperationalError("locked")):
            out = run(datastore.list_sessions())
        hub = [b for b in out["backends"] if b["name"] == "hub_chat"]
        self.assertFalse(hub[0]["ok"])
        self.assertIn("OperationalError", hub[0]["error"])


# ══ 3. TDAI 查询缓存 ═══════════════════════════════════════════════════

FAKE_KEY = "sk" + "-FAKE-DO-NOT-LEAK-0123456789"   # 运行时拼接：源码不出现 sk- 连写


class _Resp:
    def __init__(self, status=200, body=None, text_body=None):
        self.status = status
        self._body = body
        self._text = text_body if text_body is not None else json.dumps(body or {})

    async def text(self):
        return self._text

    async def json(self, content_type=None):
        if self._body is None:
            raise ValueError("not json")
        return self._body

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False


class ClientError(Exception):
    pass


class ClientConnectorError(ClientError):
    pass


class _Session:
    def __init__(self, timeout=None, **kw):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *a):
        return False

    def _go(self, verb, url, **kw):
        _STATE["record"].append({"verb": verb, "url": url, **kw})
        if isinstance(_STATE["script"], Exception):
            raise _STATE["script"]
        return _Resp(status=_STATE["script"].get("status", 200),
                     body=_STATE["script"].get("body"),
                     text_body=_STATE["script"].get("text"))

    def post(self, url, **kw):
        return self._go("POST", url, **kw)

    def get(self, url, **kw):
        return self._go("GET", url, **kw)


class ClientTimeout:
    def __init__(self, total=None):
        self.total = total


_STATE = {"script": None, "record": None}


def _install_fake_aiohttp():
    """把假 aiohttp 装进 sys.modules，返回**原模块**供 tearDown 还原。

    为什么在 setUp/tearDown 里装而不是模块级：tdai_client 是**函数内**
    `import aiohttp`（call-time 解析），而同仓 tests/test_tdai_client.py
    在**模块级**装它自己的假。两者都常驻 sys.modules 会互相顶掉
    （谁的测试跑着取决于装载顺序，不是本文件的意愿）⇒ 用安装/还原把
    生效窗口严格圈在本类用例内。"""
    m = types.ModuleType("aiohttp")
    m.ClientTimeout = ClientTimeout
    m.ClientSession = _Session
    m.ClientError = ClientError
    m.ClientConnectorError = ClientConnectorError
    m.ServerTimeoutError = ClientError
    sys.modules["aiohttp"] = m


def _ok_body(n_items=1):
    return {"code": 0, "message": "ok", "request_id": "req-cache",
            "data": {"items": [{"record_id": f"m_{i}", "content": f"命中{i}",
                                "type": "fact", "score": 0.5,
                                "created_at": "2026-10-24T00:00:00Z"}
                               for i in range(n_items)]}}


class TestTdaiQueryCache(unittest.TestCase):
    def setUp(self):
        self._saved_aiohttp = sys.modules.get("aiohttp")
        _install_fake_aiohttp()
        self.addCleanup(self._restore_aiohttp)
        _STATE["record"] = []
        _STATE["script"] = {"body": _ok_body()}
        tdai_client.clear_query_cache()
        self._env = dict(os.environ)
        os.environ["TDAI_URL"] = "http://127.0.0.1:8420"
        os.environ["TDAI_API_KEY"] = FAKE_KEY
        os.environ["TDAI_SERVICE_ID"] = "default"
        self.addCleanup(self._restore_env)

    def _restore_aiohttp(self):
        if self._saved_aiohttp is None:
            sys.modules.pop("aiohttp", None)
        else:
            sys.modules["aiohttp"] = self._saved_aiohttp

    def _restore_env(self):
        os.environ.clear()
        os.environ.update(self._env)

    # ── 命中 / 不命中 ──────────────────────────────────────────────────

    def test_second_same_query_hits_cache_and_sends_no_http(self):
        r1 = run(tdai_client.search_memories("备份"))
        r2 = run(tdai_client.search_memories("备份"))
        self.assertTrue(r1["ok"], msg=str(r1))
        self.assertNotEqual(r1.get("cached"), True, "首次不该标 cached")
        self.assertEqual(r2.get("cached"), True, "二次同 query 未命中缓存")
        self.assertEqual(len(_STATE["record"]), 1,
                         "缓存命中了还发 HTTP ⇒ 缓存白做（804ms 一点没省）")
        self.assertEqual(r1["items"], r2["items"], "缓存改变了结果内容")

    def test_cached_result_is_a_copy(self):
        """命中返回**深拷贝**：浅拷贝下 items 列表与缓存共享，调用方改
        items[0] 会污染后续每一次命中（缓存是进程级全局的，污染面是所有人）。
        直连 `_post` 才验得到这条——经 search_memories 的返回值已被
        `_norm` 加工成新 dict，天然隔离，测不出共享。"""
        body = {"query": "备份", "limit": 10}
        run(tdai_client._post("/v2/atomic/search", body, use_cache=True))
        r2 = run(tdai_client._post("/v2/atomic/search", body, use_cache=True))
        self.assertEqual(r2.get("cached"), True)
        r2["items"][0]["content"] = "POLLUTED"
        r3 = run(tdai_client._post("/v2/atomic/search", body, use_cache=True))
        self.assertNotEqual(r3["items"][0]["content"], "POLLUTED",
                            "缓存与返回值共享 items dict ⇒ 可被加工方改坏")

    def test_core_read_is_cached(self):
        """core_read（L3 长期画像）body 恒为 {} —— 多个会话同时开局会打同一
        请求，缓存把它折叠成一次。"""
        _STATE["script"] = {"body": {"code": 0, "data": {"content": "persona 文本"}}}
        r1 = run(tdai_client.core_read())
        r2 = run(tdai_client.core_read())
        self.assertTrue(r1["ok"], msg=str(r1))
        self.assertEqual(r1["content"], "persona 文本")
        self.assertEqual(r2.get("cached"), True)
        self.assertEqual(len(_STATE["record"]), 1)
        # 缓存命中后 content 仍要能加工出来（_post 之后才补的字段）
        self.assertEqual(r2["content"], "persona 文本")

    def test_different_query_not_false_hit(self):
        run(tdai_client.search_memories("备份"))
        r2 = run(tdai_client.search_memories("端口"))
        self.assertNotEqual(r2.get("cached"), True)
        self.assertEqual(len(_STATE["record"]), 2)

    def test_different_limit_not_false_hit(self):
        """同 q 不同 limit 是不同请求：串味会把「要 50 条」缓存成「10 条」。"""
        run(tdai_client.search_memories("备份", limit=10))
        r2 = run(tdai_client.search_memories("备份", limit=50))
        self.assertNotEqual(r2.get("cached"), True)
        self.assertEqual(len(_STATE["record"]), 2)
        self.assertEqual(_STATE["record"][1]["json"]["limit"], 50)

    def test_different_path_not_false_hit(self):
        """记忆与会话两条路径 body 可能同形，path 不进 key 就会互串。"""
        run(tdai_client.search_memories("备份"))
        r2 = run(tdai_client.search_conversations("备份"))
        self.assertNotEqual(r2.get("cached"), True)
        self.assertEqual(len(_STATE["record"]), 2)

    # ── 失败不缓存 ─────────────────────────────────────────────────────

    def test_failure_not_cached(self):
        """一次网络抖动不得把「查不到」缓存住 TTL 秒——那会制造
        「明明有却查不到」的假空，且没有任何错误信号。"""
        _STATE["script"] = ClientConnectorError("refused")
        r1 = run(tdai_client.search_memories("备份"))
        r2 = run(tdai_client.search_memories("备份"))
        self.assertFalse(r1["ok"])
        self.assertFalse(r2["ok"])
        self.assertEqual(len(_STATE["record"]), 2,
                         "失败被缓存了 ⇒ 网络抖动会冻结成「查不到」")
        self.assertNotEqual(r2.get("cached"), True)

    def test_business_error_not_cached(self):
        """HTTP 200 + code!=0 也是失败（TDAI 契约在信封里）。"""
        _STATE["script"] = {"body": {"code": 400, "message": "bad"}}
        run(tdai_client.search_memories("备份"))
        run(tdai_client.search_memories("备份"))
        self.assertEqual(len(_STATE["record"]), 2)

    def test_success_after_failure_is_cached(self):
        """失败不缓存 ⇒ 恢复后第一次真查成功，第二次才走缓存。"""
        _STATE["script"] = ClientConnectorError("refused")
        run(tdai_client.search_memories("备份"))
        _STATE["script"] = {"body": _ok_body()}
        r2 = run(tdai_client.search_memories("备份"))
        r3 = run(tdai_client.search_memories("备份"))
        self.assertTrue(r2["ok"])
        self.assertNotEqual(r2.get("cached"), True)
        self.assertEqual(r3.get("cached"), True)
        self.assertEqual(len(_STATE["record"]), 2)

    # ── TTL 与 LRU ─────────────────────────────────────────────────────

    def test_ttl_expiry_refetches(self):
        with mock.patch.object(tdai_client, "QUERY_CACHE_TTL_S", 0.05):
            run(tdai_client.search_memories("备份"))
            run(tdai_client.search_memories("备份"))
            self.assertEqual(len(_STATE["record"]), 1, "TTL 内第二次就该命中")
            time.sleep(0.08)
            r3 = run(tdai_client.search_memories("备份"))
        self.assertNotEqual(r3.get("cached"), True, "TTTL 过了还命中 ⇒ 缓存永不过期")
        self.assertEqual(len(_STATE["record"]), 2)

    def test_lru_evicts_oldest(self):
        with mock.patch.object(tdai_client, "QUERY_CACHE_MAX", 2):
            for q in ("查询甲", "查询乙", "查询丙"):
                run(tdai_client.search_memories(q))
            st = tdai_client.cache_stats()
            self.assertEqual(st["entries"], 2, "超过 max 未淘汰 ⇒ 无界增长")
            # 最早的那条已被淘汰 ⇒ 再查必须真发 HTTP
            n_before = len(_STATE["record"])
            run(tdai_client.search_memories("查询甲"))
            self.assertEqual(len(_STATE["record"]), n_before + 1,
                             "最早的条目没被 LRU 淘汰")

    def test_cache_stats_shape(self):
        st = tdai_client.cache_stats()
        for f in ("entries", "live", "ttl_s", "max"):
            self.assertIn(f, st)
        self.assertEqual(st["entries"], 0)
        run(tdai_client.search_memories("备份"))
        st2 = tdai_client.cache_stats()
        self.assertEqual(st2["entries"], 1)
        self.assertEqual(st2["live"], 1, "刚写入的条目不算 live ⇒ live 口径错了")

    def test_clear_query_cache_works(self):
        run(tdai_client.search_memories("备份"))
        self.assertEqual(tdai_client.cache_stats()["entries"], 1)
        tdai_client.clear_query_cache()
        self.assertEqual(tdai_client.cache_stats()["entries"], 0)
        r = run(tdai_client.search_memories("备份"))
        self.assertNotEqual(r.get("cached"), True)

    def test_backend_status_exposes_query_cache(self):
        """/health 的 tdai 段必须带 query_cache——缓存是新的静默层，
        不进 /health 就没法回答「刚才那次检索是真的还是缓存」。"""
        st = tdai_client.backend_status()
        self.assertIn("query_cache", st)
        for f in ("entries", "live", "ttl_s", "max"):
            self.assertIn(f, st["query_cache"])

    def test_no_credential_in_cache_stats(self):
        """cache_stats 进 /health ⇒ 不得夹带任何凭据形态的串。"""
        run(tdai_client.search_memories("备份"))
        blob = json.dumps(tdai_client.cache_stats(), ensure_ascii=False)
        self.assertNotIn(FAKE_KEY, blob)


# ══ 4. corpus_bytes 口径 + meta 落盘 ═══════════════════════════════════

class MemIndexCorpusFixture(unittest.TestCase):
    """真文件 + 真 SQLite。db 一律临时目录 ⇒ 生产 data/memindex.db 全程不碰。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="memindex-cb-"))
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.db = self.tmp / "db" / "memindex.db"
        # 测试跑在 HOME 下夹具目录，磁盘水位判据用真阈值会把建库拒了（与
        # tests/test_memindex_proj.py 同因）：压低阈值，**生产默认 8G/3G 不许改**。
        for attr in ("DISK_LOW_GB", "DISK_CRITICAL_GB"):
            mock.patch.object(memindex, attr, 1e-9).start()
        self.addCleanup(mock.patch.stopall)
        # 三个大小已知的文本文件（都含可检索词）
        self.f1 = _w(self.tmp / "srcA" / "a.md", "alpha_needle 甲\n" * 40)
        self.f2 = _w(self.tmp / "srcA" / "b.md", "alpha_needle 乙\n" * 80)
        self.f3 = _w(self.tmp / "srcB" / "c.md", "alpha_needle 丙\n" * 120)
        self.specs = [Spec("srcA", (str(self.tmp / "srcA"),), "*.md"),
                      Spec("srcB", (str(self.tmp / "srcB"),), "*.md")]

    def _expected_corpus_bytes(self):
        """库内实测口径的期望值：DISTINCT realpath 后 sum(size)。
        只数**仍存在**的文件（删除场景的期望值必须跟着降）。"""
        sizes = {}
        for f in (self.f1, self.f2, self.f3):
            if not f.exists():
                continue
            rp = os.path.realpath(str(f))
            sizes[rp] = f.stat().st_size
        return sum(sizes.values())

    def _meta(self):
        """用**新连接**读 meta —— 不信同一连接的事务视图。
        缺 commit 的 bug 只有这样才能抓到（同连接能读到未提交的）。"""
        con = sqlite3.connect(str(self.db))
        try:
            return {k: v for k, v in con.execute("SELECT k,v FROM meta")}
        finally:
            con.close()

    def _build(self, scope="full"):
        return build(scope, db_path=self.db, specs=self.specs)


class TestCorpusBytesAndMetaCommit(MemIndexCorpusFixture):
    def test_full_build_meta_persisted_after_close(self):
        """回归：`con.commit()` 曾被改丢 ⇒ files/chunks/corpus_bytes 三条 meta
        在 close 时静默回滚，而 build() 照常返回 ok、notes 全绿。
        判据 = **新连接**读得到。"""
        r = self._build()
        self.assertTrue(r.ok, msg=str(r.notes))
        meta = self._meta()
        for k in ("files", "chunks", "corpus_bytes", "schema_version", "last_build"):
            self.assertIn(k, meta, f"meta 缺 {k} ⇒ 没提交就被 close 了")

    def test_full_build_corpus_bytes_is_in_db_measurement(self):
        r = self._build()
        self.assertTrue(r.ok, msg=str(r.notes))
        expected = self._expected_corpus_bytes()
        self.assertGreater(expected, 0)
        self.assertEqual(int(self._meta()["corpus_bytes"]), expected,
                         "corpus_bytes 与库内实测不符")
        self.assertEqual(r.bytes_corpus, expected,
                         "br.bytes_corpus 与 meta 口径不一致")

    def test_incremental_corpus_bytes_not_shrunk_to_changed_only(self):
        """回归（PT-20261024-08 C3 的核心）：incremental 时 corpus_bytes 原写
        `br.bytes_corpus` = **本次变更文件的字节** ⇒ 实测虚低 8.2 倍，
        「30× 膨胀」被读成「86×」，索引瘦身评估整个跑偏。
        判据：改一个文件后 incremental，corpus_bytes 必须仍是**全库**口径。"""
        self._build()
        before = int(self._meta()["corpus_bytes"])
        self.assertEqual(before, self._expected_corpus_bytes())
        # 改一个文件（内容变长 ⇒ size/mtime 都变 ⇒ 一定进 changed）
        self.f1.write_text(self.f1.read_text(encoding="utf-8") + "alpha_needle 追加\n",
                           encoding="utf-8")
        expected = self._expected_corpus_bytes()
        self.assertNotEqual(expected, before, "夹具没造出变化，断言会假绿")
        r = self._build("incremental")
        self.assertTrue(r.ok, msg=str(r.notes))
        after = int(self._meta()["corpus_bytes"])
        self.assertEqual(after, expected,
                         "incremental 后 corpus_bytes 不是全库口径")
        # 红向钉死旧行为：只算变更文件时会恰好等于 f1 的新 size
        self.assertNotEqual(after, self.f1.stat().st_size,
                            "corpus_bytes 退化成「本次变更文件的字节」了")
        self.assertGreater(after, before)

    def test_incremental_removed_file_shrinks_corpus_bytes(self):
        """反向口径：文件真没了，corpus_bytes 必须**降**——只升不降同样是假口径。"""
        self._build()
        before = int(self._meta()["corpus_bytes"])
        self.f3.unlink()
        r = self._build("incremental")
        self.assertTrue(r.ok, msg=str(r.notes))
        after = int(self._meta()["corpus_bytes"])
        self.assertLess(after, before, "文件删了 corpus_bytes 不降 ⇒ 只读不快照")
        self.assertEqual(after, self._expected_corpus_bytes())

    def test_chunks_and_files_are_in_db_measurements(self):
        """files/chunks 与 corpus_bytes 同源（库内实测），不是本次枚举量——
        这条是 C3 修 corpus_bytes 时顺带钉住的同族口径。"""
        self._build()
        self.f2.write_text(self.f2.read_text(encoding="utf-8") + "alpha_needle 追加\n",
                           encoding="utf-8")
        r = self._build("incremental")
        self.assertTrue(r.ok, msg=str(r.notes))
        meta = self._meta()
        con = sqlite3.connect(str(self.db))
        try:
            n_files = con.execute(
                "SELECT COUNT(DISTINCT realpath) FROM fed_proj").fetchone()[0]
            n_chunks = con.execute("SELECT COUNT(*) FROM fed_proj").fetchone()[0]
        finally:
            con.close()
        self.assertEqual(int(meta["files"]), n_files)
        self.assertEqual(int(meta["chunks"]), n_chunks)
        # incremental 只碰 1 个文件，files 不得掉到 1
        self.assertEqual(n_files, 3, "incremental 把 files 写成本次枚举量了")


if __name__ == "__main__":
    unittest.main(verbosity=2)
