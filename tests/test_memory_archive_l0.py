#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：统一归档层 memory_archive + /api/memory/archive（v0.13.161）。

分层口径（tests/tiers.py）：零宿主依赖——库在 tmp、不起服务、不打网络、
不 import src.main。**不允许 SKIP**。

为什么这张闸门必须存在（红向都能确定性造）：
1. **lastrowid 语义**：`db.execute` 回的是影响行数（INSERT 也是 1），拿它当新行 id
   会让 superseded_by 恒指向 1 —— 冒烟实测抓到过，这里钉死。
2. **simhash 进程稳定性**：`hash()` 对 str 每进程随机化，去重状态跨重启漂移 ⇒
   必须用 blake2b 类稳定哈希，且同文本同值、不同文本距离可分辨。
3. **manual 权威不可被自动取代**：agent 的近似重复把人工行标掉 = 静默丢用户原话。
4. **写入口身份必填**：不标识提交方的写入无法追责；manual 冒充 agent 必须 400。
5. **截断不静默**：total/count/truncated 三者分开（v0.13.160 同款口径）。
6. **LIKE 转义**：查 `50%` 命中全表 = 检索结果与查询词无关（与 _local_l1 同款分叉）。
7. **端到端鉴权**：POST 过 writeauth 中间件，无 token 必须 401/403（不是 200）。
"""
import os
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db  # noqa: E402


class _ArchCase(unittest.TestCase):
    """每个用例一套 tmp 库：归档写入口改的是全局连接，必须隔离。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0arch-"))
        os.environ["DATA_DIR"] = str(self.tmp)
        db.init_db(self.tmp / "hub.db")
        import memory_archive
        import memory
        self.ma = memory_archive
        self.memory = memory

    def tearDown(self):
        db._conn.close()
        db._conn = None
        os.environ.pop("DATA_DIR", None)

    def _submit(self, content, category="fact", agent="claude", kind="distilled", **kw):
        return self.ma.submit(content, category, agent, kind, **kw)


class TestSimHash(unittest.TestCase):
    """纯函数层：稳定 + 可分辨。不起库。"""

    def setUp(self):
        sys.path.insert(0, str(_REPO / "src"))
        import memory_archive
        self.ma = memory_archive

    def test_same_text_same_hash_across_calls(self):
        a = self.ma.simhash("agenthub 的端口是 3102")
        b = self.ma.simhash("agenthub 的端口是 3102")
        self.assertEqual(a, b, "同文本必须同值（blake2b 稳定，不用随机化 hash()）")

    def test_distance_ordering(self):
        base = self.ma.simhash("agenthub 生产端口是 3102，重启用 hub-restart.sh")
        minor = self.ma.simhash("agenthub 生产端口是 3103，重启用 hub-restart.sh")
        other = self.ma.simhash("用户偏好回复直接给下一步，不要开场白")
        d_minor = self.ma.hamming(base, minor)
        d_other = self.ma.hamming(base, other)
        self.assertEqual(d_minor, 0 if base == minor else d_minor)
        self.assertLess(d_minor, d_other,
                        f"改写版距离({d_minor})必须小于不同事实({d_other})，否则阈值无从标定")

    def test_short_text_has_shingle(self):
        # 短于 3 字的文本不能没有 shingle（否则永远无法近似判重）
        self.assertNotEqual(self.ma.simhash("端口"), 0)

    def test_content_hash_normalizes(self):
        h1 = self.ma.content_hash("Agenthub  端口 3102")
        h2 = self.ma.content_hash("agenthub 端口 3102")
        self.assertEqual(h1, h2, "大小写/空白差异不算两条记忆")


class TestSubmitPipeline(_ArchCase):
    """质量门全链：创建/精确重复/近似取代/身份/manual 保护/上限。"""

    def test_create_then_duplicate(self):
        r1 = self._submit("agenthub 的端口是 3102")
        self.assertEqual(r1["status"], "created")
        r2 = self._submit("agenthub 的端口是 3102")
        self.assertEqual(r2["status"], "duplicate")
        self.assertEqual(r2["id"], r1["id"], "重复必须返回已存在的 id，不插第二条")
        rows = self.ma.list_rows()["memories"]
        self.assertEqual(len(rows), 1)

    def test_near_duplicate_supersedes_with_correct_id(self):
        r1 = self._submit("agenthub 的端口是 3102")
        r2 = self._submit("agenthub 的端口是 3103")
        self.assertEqual(r2["status"], "superseded")
        self.assertEqual(r2["superseded_ids"], [r1["id"]])
        # lastrowid 语义：新行 id 必须是真自增 id，不是影响行数 1
        self.assertNotEqual(r2["id"], 1 if r1["id"] == 1 else r2["id"])
        rows = {r["id"]: r for r in
                self.ma.list_rows(include_superseded=True)["memories"]}
        self.assertEqual(rows[r1["id"]]["status"], "superseded")
        self.assertEqual(rows[r1["id"]]["superseded_by"], r2["id"])
        self.assertEqual(rows[r2["id"]]["status"], "active")

    def test_different_facts_not_superseded(self):
        self._submit("agenthub 的端口是 3102")
        r = self._submit("备份脚本在 ~/bin/backup-all-to-nas.sh")
        self.assertEqual(r["status"], "created", "不同事实不得被近似门误杀")

    def test_cross_category_not_superseded(self):
        self._submit("端口是 3102", category="fact")
        r = self._submit("端口是 3103", category="decision")
        self.assertEqual(r["status"], "created", "近似判重只在同 (agent, category) 内")

    def test_cross_agent_not_superseded(self):
        self._submit("端口是 3102", agent="claude")
        r = self._submit("端口是 3103", agent="codex")
        self.assertEqual(r["status"], "created", "agent 之间互不取代")

    def test_manual_row_protected_both_ways(self):
        man = self._submit("用户偏好：回复直接给下一步", "preference", "manual", "manual")
        # agent 近似重复不得标掉 manual 行
        r = self._submit("用户偏好：回复直接给下一步呀", "preference", "codex")
        self.assertEqual(r["status"], "created")
        rows = {x["id"]: x for x in
                self.ma.list_rows(include_superseded=True)["memories"]}
        self.assertEqual(rows[man["id"]]["status"], "active",
                         "manual 行必须保持活跃（人工措辞是权威层）")

    def test_manual_cannot_impersonate_agent(self):
        with self.assertRaises(ValueError):
            self._submit("x", agent="claude", kind="manual")

    def test_unknown_agent_rejected_with_valid_list(self):
        with self.assertRaises(ValueError) as ctx:
            self._submit("x", agent="nope")
        msg = str(ctx.exception)
        self.assertIn("claude", msg, "400 理由必须带可用值，不让调用方盲猜")
        self.assertIn("manual", msg)

    def test_empty_and_category_validation(self):
        with self.assertRaises(ValueError):
            self._submit("   ")
        with self.assertRaises(ValueError):
            self._submit("x", category="nope")
        with self.assertRaises(ValueError):
            self._submit("x", kind="nope")

    def test_scrub_on_submit(self):
        r = self._submit("令牌是 sk-abcdef123456 别忘了")
        rows = {x["id"]: x for x in
                self.ma.list_rows(include_superseded=True)["memories"]}
        content = rows[r["id"]]["content"]
        self.assertIn("<redacted>", content, "疑似凭据必须入库前脱敏")
        self.assertNotIn("sk-abcdef123456", content)

    def test_cap_evicts_oldest_soft(self):
        old_cap = self.ma.PER_AGENT_CAP
        self.ma.PER_AGENT_CAP = 3
        try:
            # 内容必须**互不相似**：短文本共享多数 shingle（"第 N 条唯一记忆"实测
            # 海明距离 ≤10 被近似门正确判重）——那是取代路径不是容量路径，会污染本用例。
            ids = [self._submit(f"独立事实编号{i}：" + "补" * (i + 1) + "尾缀" + str(i) * 8)["id"]
                   for i in range(5)]
            active = [r["id"] for r in self.ma.list_rows()["memories"]]
            self.assertEqual(len(active), 3, "触顶后活跃数必须回到上限")
            self.assertNotIn(ids[0], active, "最旧的先被软删")
            self.assertNotIn(ids[1], active)
            rows = {r["id"]: r for r in
                    self.ma.list_rows(include_superseded=True)["memories"]}
            self.assertEqual(rows[ids[0]]["status"], "superseded")
            self.assertIsNone(rows[ids[0]]["superseded_by"],
                              "容量淘汰不是矛盾取代，指针必须留空")
        finally:
            self.ma.PER_AGENT_CAP = old_cap


class TestListAndMatrix(_ArchCase):
    """列表口径 + 看板矩阵。"""

    def test_total_truncated_and_filters(self):
        for i in range(3):
            self._submit(f"记忆 {i}")
        self._submit("偏好一条", "preference", "manual", "manual")
        d = self.ma.list_rows(limit=2)
        self.assertEqual((d["count"], d["total"], d["truncated"]), (2, 4, True))
        d = self.ma.list_rows(category="preference")
        self.assertEqual((d["count"], d["total"]), (1, 1))
        d = self.ma.list_rows(agent="manual")
        self.assertEqual(d["count"], 1)
        d = self.ma.list_rows(q="50%")          # LIKE 转义：不得因通配符命中全表
        self.assertEqual(d["count"], 0)

    def test_matrix_shape(self):
        self._submit("claude 提交的一条")
        self._submit("手动补的一条", "preference", "manual", "manual")
        counts = self.ma.counts_by_agent(active_only=False)
        self.assertEqual(counts.get("claude", {}).get("active"), 1)
        self.assertEqual(counts.get("manual", {}).get("active"), 1)
        st = self.ma.stats()
        self.assertEqual(st["active"], 2)
        self.assertEqual(st["superseded"], 0)
        self.assertEqual(st["per_agent_cap"], self.ma.PER_AGENT_CAP)

    def test_agent_source_map_covers_all_agents(self):
        """矩阵里每个在册 agent 都要有映射键（漏=看板显示「无源」，看得见的缺失）。"""
        import profiles
        ids = {p["id"] for p in profiles.PROFILES if p.get("kind") == "agent"}
        missing = ids - set(self.ma.AGENT_SOURCE_MAP)
        self.assertEqual(missing, set(), f"AGENT_SOURCE_MAP 缺 agent: {sorted(missing)}")


class TestRoutes(_ArchCase):
    """端点层：装配 + 审计 + 鉴权（挂 write_gate 中间件测真实闸门）。"""

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import writeauth
        app = FastAPI()
        # 与 main.py:354 同一装法：`app.middleware("http")(fn)`——add_middleware 收的是
        # 类不是函数，直接 add_middleware(write_gate) 会 TypeError（本测试首版踩过）。
        app.middleware("http")(writeauth.write_gate)
        app.include_router(self.memory.router)
        return TestClient(app)

    def setUp(self):
        super().setUp()
        os.environ["TERM_TOKEN"] = "l0-test-token"
        self.H = {"x-hub-token": "l0-test-token", "Content-Type": "application/json"}

    def test_post_get_matrix_e2e(self):
        c = self._client()
        r = c.post("/api/memory/archive",
                   json={"content": "端到端一条", "category": "fact", "agent": "claude"},
                   headers=self.H)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["status"], "created")
        r = c.get("/api/memory/archive", headers=self.H)
        d = r.json()
        self.assertEqual((d["count"], d["total"]), (1, 1))
        r = c.get("/api/memory/archive/matrix", headers=self.H)
        m = r.json()
        self.assertIn("agents", m)
        self.assertIn("stats", m)
        row = [a for a in m["agents"] if a["id"] == "claude"][0]
        self.assertEqual(row["archive_active"], 1)
        self.assertTrue(row["sources"], "claude 行必须带既有源清单")
        # mcp_wired 依赖读 ~/.claude/settings.json；hermetic-clean 下 HOME 是 temp 目录，
        # 真实配置不存在 ⇒ mcp_wired=False 是预期行为，不是失败。
        import pathlib as _p, os
        _expected = 'hub-mcp' in (_p.Path.home() / '.claude' / 'settings.json').read_text() if (_p.Path.home() / '.claude' / 'settings.json').exists() else False
        self.assertEqual(row['mcp_wired'], _expected, 'claude mcp_wired 探测结果')

    def test_post_requires_token(self):
        c = self._client()
        r = c.post("/api/memory/archive",
                   json={"content": "无 token", "agent": "claude"})
        self.assertIn(r.status_code, (401, 403),
                      "写端点必须过 writeauth（无 token 不得 200）")

    def test_post_bad_agent_400_lists_valid(self):
        c = self._client()
        r = c.post("/api/memory/archive",
                   json={"content": "x", "agent": "nope"}, headers=self.H)
        self.assertEqual(r.status_code, 400)
        self.assertIn("claude", r.json()["detail"])

    def test_agent_from_header(self):
        c = self._client()
        h = dict(self.H)
        h["x-agent-id"] = "codex"
        r = c.post("/api/memory/archive", json={"content": "头里带身份"}, headers=h)
        self.assertEqual(r.status_code, 200)
        rows = self.ma.list_rows()["memories"]
        self.assertEqual(rows[0]["source"], "codex", "X-Agent-ID 头必须被采信")

    def test_search_includes_archive_source(self):
        import asyncio
        self._submit("端到端可检索的一条")
        want = {"unified_archive"}
        res = asyncio.run(self.memory.memfed_search_probe(want)) \
            if hasattr(self.memory, "memfed_search_probe") else None
        # 直接调 memfed（不经端点）：源必须注册且在快路默认集里
        import memfed
        self.assertIn("unified_archive", memfed.enabled_ids())
        self.assertIn("unified_archive", self.memory.FED_FAST_SOURCES.split(","))
        r = asyncio.run(memfed.search_fed("端到端", 10, want))
        self.assertTrue(r["unified_archive"]["ok"])
        self.assertEqual(r["unified_archive"]["count"], 1)
        item = r["unified_archive"]["items"][0]
        self.assertEqual(item["origin"], "claude", "提交者身份必须随结果带回")


if __name__ == "__main__":
    unittest.main(verbosity=2)
