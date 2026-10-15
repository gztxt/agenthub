#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：注入通道接联邦（v0.13.62 D1）+ 默认行径（D1.5）。

分层口径同 tests/tiers.py：不起服务、不占端口、不打网络、不 import src.main
（那会带起 vitals_loop 去烧 CLI 探活，见 PT-20260923-05）；db 指向临时空库 ⇒
任何命中只可能来自夹具，生产 agents.db 全程不碰。**不允许 SKIP**。

为什么这张闸门必须存在（红向都能确定性造）：

1. **G1 = 本次修复的病灶本体**。改前 `/api/memory/context` 收下 `sources` 里的
   联邦 id、过完白名单校验（**不报 400**）、然后在函数体里被静默丢弃——回包
   `backends` 只有 `tdai_profile`/`local`，HTTP 200、无异常、无告警。
   本仓自己管这叫「全指标绿而功能层已死」。任何人日后再动这段而没碰这条断言，
   病灶就会原样回来且没人会发现。
2. **G3 = 墙钟不能连坐**。预算机制最容易写坏的形式是「超时把已完成的也一起丢掉」
   （`wait_for` 包 gather 正是这个病）。所以断言必须是：慢源记 `skipped_budget`
   **且**快源照常有结果——只断言前者的话，`return {}` 也能过。
3. **G4/G5 = 分离「能力」与「默认」**。D1 只接通能力不动默认，D1.5 才翻默认；
   两步分开才可以在出问题时二分定位是通道坏了还是默认值漂了。G4 钉住注入默认
   不得被顺手翻开，G5 钉住检索默认**必须**已翻（含「慢三路不在其中」这半句——
   只钉「已翻」的话，有人把 2.5s 的 archived_sessions 也加进去照样绿）。
4. **G6 = 只读**。联邦源全是别的 agent 的私有资产，这段代码有了写能力就是越界。

关于 G3 的夹具手法：把 `memfed._search_rg_text` **与** `memfed._search_proj` 各包一层
`time.sleep` 后**再调真实现**，不伪造返回值——被测的仍是真 `search_fed` 的墙钟逻辑与真适配器的
检索结果。

为什么**两个都要包**（2026-10-02 踩过的坑，PT-20261002-11）：A4 把 pi_sessions /
codex_sessions / archived_sessions 三路慢源改走投影层，`_build_tasks` 的派发是
`fn = _search_proj if sid in _PROJ_TARGETS else _search_rg_text`。只补 rg 适配器 ⇒ 这三路的
补丁被**绕开** ⇒ 源不慢不炸 ⇒ 墙钟断言假红（当时 4 条红就是这么来的，而生产逻辑实测是对的）。
更阴的是 `run_tier.py` 把 `MEMINDEX_DB` 钉到不存在路径，`_search_proj` 必走 fallback 回 rg，
补丁才「碰巧」生效 ⇒ 同一个缺陷在两种跑法下两张面孔（独立跑红 / L0 跑绿）。
所以统一走 `_patch_search()` 补两条路径，并加 `TestA4RoutingGuard` 钉死派发表达式本身。
"""
import ast
import asyncio
import contextlib
import inspect
import pathlib
import re
import shutil
import sqlite3
import sys
import tempfile
import time
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db  # noqa: E402

_TMP = pathlib.Path(tempfile.mkdtemp(prefix="l0inject-"))
db.init_db(_TMP / "agents.db")          # 空库：本地 0 行，任何命中都只能来自夹具

from fastapi import FastAPI                      # noqa: E402
from starlette.testclient import TestClient      # noqa: E402
import memfed                                    # noqa: E402
import memory                                    # noqa: E402
import tdai_client                               # noqa: E402

_app = FastAPI()
_app.include_router(memory.router)
C = TestClient(_app)


def _strip_doc_and_comments(src: str) -> str:
    """只判**去注释后**的代码：本仓大量「绝不 DELETE」的自律声明写在 docstring 里，
    不过滤会把自律当违规。"""
    code = re.sub(r'"""[\s\S]*?"""', "", src)
    return "\n".join(l for l in code.splitlines() if not l.strip().startswith("#"))


def _make_claude_mem_db(path: pathlib.Path):
    """claude_mem 夹具（结构照 tests/test_memfed.py 的同名夹具）。"""
    con = sqlite3.connect(path)
    con.execute("CREATE TABLE observations(id INTEGER PRIMARY KEY, title TEXT, "
                "subtitle TEXT, facts TEXT, type TEXT, created_at TEXT)")
    con.execute("CREATE VIRTUAL TABLE observations_fts USING fts5(ot)")
    con.execute("CREATE TABLE session_summaries(id INTEGER PRIMARY KEY, request TEXT, "
                "investigated TEXT, learned TEXT, created_at TEXT)")
    con.execute("CREATE VIRTUAL TABLE session_summaries_fts USING fts5(st)")
    con.execute("INSERT INTO observations(id,title,subtitle,facts,type,created_at) "
                "VALUES (1,'注入通道改造','联邦源静默丢弃','注入通道 必须 能召回 claude_mem 观察',"
                "'discovery','2026-10-02T00:00:00Z')")
    con.execute("INSERT INTO observations_fts(rowid,ot) VALUES "
                "(1,'注入通道 改造 联邦源 静默 丢弃 注入通道 必须 能召回 claude_mem 观察')")
    con.execute("INSERT INTO session_summaries(id,request,investigated,learned,created_at) "
                "VALUES (1,'修注入通道', '查了回包', '联邦源被丢了', '2026-10-02T01:00:00Z')")
    con.execute("INSERT INTO session_summaries_fts(rowid,st) VALUES "
                "(1,'修 注入通道 查了 回包 联邦源 被 丢了')")
    con.commit()
    con.close()


def _make_pi_session(tmp: pathlib.Path) -> pathlib.Path:
    d = tmp / "pi-sessions"
    d.mkdir(parents=True, exist_ok=True)
    (d / "s1.jsonl").write_text(
        '{"role":"user","content":"注入通道 联邦源 静默丢弃"}\n', encoding="utf-8")
    return d


# ── TDAI 离群夹具：本文件不打网络，但必须打到「ok/不 ok」两条路 ──────────

async def _fake_search_memories(q, limit, timeout_s=None):
    return {"ok": True, "count": 1,
            "items": [{"id": "m1", "content": "TDAI 结构化记忆：" + q,
                       "category": "fact"}]}


async def _fake_core_read(timeout_s=None):
    return {"ok": True, "content": "假 Profile（夹具）", "count": 1}


async def _fake_scenario_ls(timeout_s=None):
    return {"ok": True, "entries": [{"path": "scene_blocks/x.md"}]}


@contextlib.contextmanager
def _patch_search(make_wrapper):
    """同时补 `_search_proj` 与 `_search_rg_text`（两条路径都要）。

    `make_wrapper(real)` 收到**该路径自己的**真实现，返回包了它的替身；
    退出时两个都还原。补错函数名会让补丁静默失效，而断言只在「源没慢」时红，
    很容易被误读成生产缺陷——所以这个 helper 是本文件唯一的补入口，别再手写单路径。
    """
    saved = {k: getattr(memfed, k) for k in ("_search_proj", "_search_rg_text")}
    try:
        memfed._search_proj = make_wrapper(saved["_search_proj"])
        memfed._search_rg_text = make_wrapper(saved["_search_rg_text"])
        yield
    finally:
        for k, v in saved.items():
            setattr(memfed, k, v)


class TestA4RoutingGuard(unittest.TestCase):
    """守**夹具**本身（AST 级，与本仓 `vitals_loop` 那条同类护栏同形）。

    为什么要守它：跑一遍是绿的**证明不了**派发形态没变——L0 钉死 `MEMINDEX_DB` 时，
    `_search_proj` 必走 fallback，任何「补丁补错函数名」的夹具都会绿。2026-10-02 那 4 条红
    就是这么藏起来的。所以这里钉表达式：派发必须仍是
    `_search_proj if sid in _PROJ_TARGETS else _search_rg_text`。
    派发形态一旦变（例如再加一路投影、或改成字典查表），这里先红，提示夹具要重审。
    """

    def test_build_tasks_dispatches_proj_targets_to_search_proj(self):
        tree = ast.parse(inspect.getsource(memfed._build_tasks))
        pairs = [(n.body.id, n.orelse.id) for n in ast.walk(tree)
                 if isinstance(n, ast.IfExp) and isinstance(n.body, ast.Name)
                 and isinstance(n.orelse, ast.Name)]
        self.assertIn(
            ("_search_proj", "_search_rg_text"), pairs,
            "派发形态变了（现在 %r）——夹具的 _patch_search 要跟着重审，"
            "否则墙钟/降级这 4 条会静默退化成 A4-OFF 路径的绿" % (pairs,))
        self.assertTrue(set(memfed._PROJ_TARGETS), "_PROJ_TARGETS 空了：投影路名存实亡")


class _Case(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0inject-case-"))
        self._saved_targets = dict(memfed._RG_TARGETS)
        self._saved_db = memfed._CLAUDE_MEM_DB
        memfed._CLAUDE_MEM_DB = self.tmp / "claude-mem.db"
        _make_claude_mem_db(self.tmp / "claude-mem.db")
        memfed._RG_TARGETS["pi_sessions"] = ([str(_make_pi_session(self.tmp))], "*.jsonl")
        # 把「本机真实路径」从联邦里摘掉：闸门要绿是因为夹具绿，不是因为本机恰好有货
        for sid in ("claude_projects", "grok_memory", "hermes_memory",
                    "workbuddy_memory", "workspace_files", "archived_sessions",
                    "codex_sessions"):
            memfed._RG_TARGETS[sid] = ([str(self.tmp / "none")], "*")

        self._saved_tdai = (tdai_client.search_memories, tdai_client.core_read,
                            tdai_client.scenario_ls, tdai_client.search_conversations)
        tdai_client.search_memories = _fake_search_memories
        tdai_client.core_read = _fake_core_read
        tdai_client.scenario_ls = _fake_scenario_ls
        async def _no_l0(*a, **k):
            return {"ok": False, "count": 0, "error": "夹具：不打 L0"}
        tdai_client.search_conversations = _no_l0
        self.addCleanup(self._restore)

    def _restore(self):
        (tdai_client.search_memories, tdai_client.core_read,
         tdai_client.scenario_ls, tdai_client.search_conversations) = self._saved_tdai
        memfed._RG_TARGETS = self._saved_targets
        memfed._CLAUDE_MEM_DB = self._saved_db
        shutil.rmtree(self.tmp, ignore_errors=True)


# ── G1 病灶本体：联邦源必须真的进得了注入包 ─────────────────────────────

class TestG1FedReachesInjection(_Case):
    def test_fed_source_appears_in_backends(self):
        r = C.get("/api/memory/context",
                  params={"q": "注入通道", "sources": "local,tdai,claude_mem"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        names = [b["name"] for b in d["backends"]]
        self.assertIn("claude_mem", names,
                      "联邦源过了白名单却被丢弃——回退读法就是病灶复发：%s" % names)
        self.assertEqual(d["fed"]["sources"], 1)
        self.assertIn("claude_mem", d["fed"]["done"])

    def test_fed_hits_land_in_context_text(self):
        d = C.get("/api/memory/context",
                  params={"q": "注入通道", "sources": "local,tdai,claude_mem"}).json()
        self.assertIn("其他 Agent 记忆（联邦）", d["context"])
        self.assertIn("claude-mem 观察库", d["context"])   # 必须带源标签，可溯源
        self.assertGreater(d["fed"]["chars"], 0)

    def test_per_source_cap_and_no_spurious_notice(self):
        r = C.get("/api/memory/context",
                  params={"q": "注入通道", "sources": "local,tdai,claude_mem,pi_sessions",
                          "fed_top": 1, "fed_chars": 4000})
        self.assertEqual(r.status_code, 200)
        body = r.json()["context"]
        seg = body.split("## 其他 Agent 记忆（联邦）\n")[-1]
        n_items = sum(1 for l in seg.splitlines() if l.startswith("- ["))
        # fed_top=1 ⇒ 每路最多 1 条；两路 ⇒ 最多 2 条
        self.assertLessEqual(n_items, 2, "fed_top=1 时每路只应给 1 条：%r" % seg)
        # 两路都成功 ⇒ 不得凭空出现「另有 N 路」告示（否则模型会以为有东西没拿到）
        self.assertFalse(any("另有" in l for l in seg.splitlines()),
                         "两路都返回了还发告示 = 假降级信号：%r" % seg)

    def test_unreturned_sources_are_announced_in_segment(self):
        """真没拿到的源必须**写在段里**：模型看到只有 2 路时得知道另外几路的下落，
        否则它会以为「这就是全部」——这正是本次要消灭的静默丢弃的换皮。"""
        # CONTEXT_TIMEOUT_S 默认 1.2s < 1.6s ⇒ pi_sessions 必进预算外
        with _patch_search(lambda real: (lambda *a, **k: (time.sleep(1.6), real(*a, **k))[1])):
            r = C.get("/api/memory/context",
                      params={"q": "注入通道", "sources": "claude_mem,pi_sessions"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("pi_sessions", d["fed"]["skipped_budget"])
        self.assertIn("claude_mem", d["fed"]["done"])
        self.assertIn("pi_sessions", d["degraded"])
        self.assertIn("未在时间预算内返回", d["context"])

    def test_unknown_source_still_400(self):
        r = C.get("/api/memory/context", params={"q": "x", "sources": "local,ghost"})
        self.assertEqual(r.status_code, 400)
        self.assertIn("ghost", r.text)


# ── G3 墙钟：慢源记预算外，快源不受牵连 ───────────────────────────────

class TestG3WallClock(_Case):
    def test_slow_source_marked_skipped_budget_fast_one_survives(self):
        def make_slow(real):
            def slow(sid, q, limit, timeout_s):
                time.sleep(0.6)                   # 只加慢，结果仍由真实现产出
                return real(sid, q, limit, timeout_s)
            return slow

        with _patch_search(make_slow):
            out = asyncio.run(memfed.search_fed(
                "注入通道", 5, {"claude_mem", "pi_sessions"}, wall_s=0.15))
        self.assertEqual(out["pi_sessions"]["error"], memfed.SKIPPED_BUDGET)
        self.assertFalse(out["pi_sessions"]["ok"])
        self.assertEqual(out["pi_sessions"]["count"], 0)
        self.assertTrue(out["claude_mem"]["ok"], "墙钟不得连坐已完成的快源")
        self.assertGreater(out["claude_mem"]["count"], 0)

    def test_no_wall_means_no_skipped_budget(self):
        """默认路径（wall_s=None）必须与引入墙钟之前逐字同语义——既有 27 例钉的是这个。"""
        out = asyncio.run(memfed.search_fed("注入通道", 5, {"claude_mem", "pi_sessions"}))
        self.assertTrue(out["claude_mem"]["ok"])
        self.assertTrue(out["pi_sessions"]["ok"])
        for r in out.values():
            self.assertNotEqual(r.get("error"), memfed.SKIPPED_BUDGET)

    def test_injection_endpoint_never_raises_on_slow_sources(self):
        with _patch_search(lambda real: (lambda *a, **k: (time.sleep(2.5), real(*a, **k))[1])):
            r = C.get("/api/memory/context",
                      params={"q": "注入通道", "sources": "claude_mem,pi_sessions"})
        self.assertEqual(r.status_code, 200, "预算超时不得变成 5xx")
        d = r.json()
        self.assertIn("pi_sessions", d["degraded"])
        self.assertIn("claude_mem", [b["name"] for b in d["backends"]])

    def test_one_source_raising_does_not_sink_others(self):
        def boom(*a, **k):
            raise RuntimeError("夹具炸源")

        with _patch_search(lambda real: boom):
            r = C.get("/api/memory/context",
                      params={"q": "注入通道", "sources": "local,tdai,claude_mem,pi_sessions"})
        self.assertEqual(r.status_code, 200)
        d = r.json()
        self.assertIn("pi_sessions", d["degraded"])
        self.assertIn("claude_mem", d["fed"]["done"])


# ── G4/G5 能力与默认分离 ───────────────────────────────────────────────

class TestDefaults(unittest.TestCase):
    @staticmethod
    def _param_default(path, name):
        """从 OpenAPI 读参数默认值。FastAPI 把 Query 的默认值放在 schema 子对象里。"""
        schema = C.get("/openapi.json").json()
        params = schema["paths"][path]["get"]["parameters"]
        got = [p.get("schema", {}).get("default", p.get("default"))
               for p in params if p["name"] == name]
        assert got and got[0] is not None, "参数 %s 在 OpenAPI 里没有默认值" % name
        return got[0]

    def test_g4_injection_default_not_flipped(self):
        """D1 只接通能力。注入默认仍是 local,tdai（D2 才随 A4 一起翻）。"""
        self.assertEqual(self._param_default("/api/memory/context", "sources"), "local,tdai")

    def test_g5_search_default_is_fast_fed_set(self):
        dv = self._param_default("/api/memory/search", "sources")
        self.assertEqual(dv, memory.FED_FAST_SOURCES)
        for sid in ("claude_mem", "workbuddy_memory", "claude_projects"):
            self.assertIn(sid, dv)
        for sid in ("pi_sessions", "codex_sessions", "archived_sessions"):
            self.assertNotIn(sid, dv,
                             "慢路进了默认行径：实测 rg 超时 1.9~2.5s，"
                             "默认带上等于每次检索都等 3s")

    def test_fast_set_entries_are_all_enabled_and_cheap(self):
        for sid in memory.FED_FAST_SOURCES.split(","):
            if sid in ("local", "tdai"):
                continue
            src = memfed.REGISTRY[sid]
            self.assertTrue(src.enabled, "%s 不该出现在默认行径（未启用）" % sid)
            # 允许的 kind：两种外部句柄 + `hub_sqlite`（hub **自己**的 SQLite，同进程连接，
            # 毫秒级）—— D18 把经验库接进来时新增的第三种。判据的本意是「默认行径里不许有
            # 需要扫全树的慢源」，hub_sqlite 不扫树，符合本意。
            self.assertIn(src.kind, ("sqlite_fts", "rg_text", "hub_sqlite"))


# ── G6 只读：联邦源是别的 agent 的私有资产，这段代码不许有写能力 ────────

class TestMcpToolFedOpening(unittest.TestCase):
    """G7/G8：MCP 工具面的「口子」—— 2026-10-02 实测发现 claude/codex 的开局包
    fed.sources=0（端点默认 local,tdai + 工具无 sources 参数）⇒ D1 对两个主力接入方
    零收益。这里把「口子存在」与「默认口径取唯一真源」钉成闸门：日后若有人改回纯
    权威库，不会报错，只会静默少掉别家 agent 的记忆——正是最难归因的那种坏。"""

    def test_g7_context_tool_exposes_sources_param(self):
        import hubmcp
        params = inspect.signature(hubmcp.hub_memory_context).parameters
        self.assertIn("sources", params, "hub_memory_context 必须暴露 sources 口子")
        self.assertEqual(params["sources"].default, "",
                         "默认必须是空串⇒取端点 FED_FAST_SOURCES，不得抄一份字符串")

    def test_g8_default_and_explicit_sources_are_forwarded(self):
        import hubmcp
        seen = []
        orig = hubmcp._get
        hubmcp._get = lambda path, **kw: (seen.append((path, kw)), {})[1]
        try:
            hubmcp.hub_memory_context()               # 不传 ⇒ 快路联邦集
            hubmcp.hub_memory_context("local,tdai")   # 显式传 ⇒ 逐字转发
        finally:
            hubmcp._get = orig
        self.assertEqual(seen[0][0], "/api/memory/context")
        self.assertEqual(seen[0][1]["sources"], memory.FED_FAST_SOURCES)
        self.assertEqual(seen[1][1]["sources"], "local,tdai")


class TestInjectionIsReadOnly(unittest.TestCase):
    _FORBIDDEN = ("db.execute", "writeauth.", "open(", ".write(", "os.remove",
                  "unlink", "rmtree", "INSERT INTO", "UPDATE ", "DELETE FROM")

    def test_fed_section_has_no_write_capability(self):
        code = _strip_doc_and_comments(inspect.getsource(memory._fed_section))
        for kw in self._FORBIDDEN:
            self.assertNotIn(kw, code, "注入路径出现写能力：%s" % kw)

    def test_memfed_module_has_no_write_capability(self):
        code = _strip_doc_and_comments((_REPO / "src" / "memfed.py").read_text(encoding="utf-8"))
        for kw in self._FORBIDDEN:
            self.assertNotIn(kw, code, "联邦适配器出现写能力：%s" % kw)


if __name__ == "__main__":
    try:
        unittest.main(verbosity=2, exit=True)
    finally:
        shutil.rmtree(_TMP, ignore_errors=True)