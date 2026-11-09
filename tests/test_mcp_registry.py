#!/usr/bin/env python3
"""L0 hermetic：P3「工具注册表门面」的判定层与信封层（src/mcpgw.py）。

分层口径（tests/tiers.py）：本文件零宿主依赖 —— 不起服务、不打网络、不 spawn stdio 子进程、
不 import src.main、db 指向 tmp 文件库、**不允许 SKIP**。

为什么值得单独一层（三件都可确定性造红向）：
1. **ACL 判定不能依赖规则插入顺序**。`mcp_acl` 除主键外零索引，今天靠 rowid 序"看着稳定"；
   一旦有人给 agent_id 建索引，查询计划从 SCAN 翻成 SEARCH USING INDEX，旧写法
   （取第一条匹配行定生死）的判定结果会**当场翻面**（deny 抢先＝原放行的 agent 变 403）。
   故这里用"同一份行、正序与逆序必须同结论"这条不变式钉死，而不是钉某一条规则的次序。
2. **工具清单必须带 inputSchema**。改前只出 4 个键，前端是自由 JSON 文本框 ⇒ agent 只能猜参数名。
3. **单路 server 挂掉不得把整张清单塌成空**，且 degraded/note 必须点名是哪一路。

红向断言（先证旧写法真的会坏）在每条用例的 docstring 里写明。
"""
import asyncio
import json
import pathlib
import shutil
import sqlite3
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import db           # noqa: E402
import mcpgw        # noqa: E402
from fastapi import HTTPException  # noqa: E402


class _DbCase(unittest.TestCase):
    """把 db 换到 tmp 文件库并建 mcpgw 的两张表；tearDown 必须还原全局连接。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0mcp-"))
        self._saved_conn = db._conn
        db.init_db(self.tmp / "t.db")
        mcpgw.ensure_schema()
        mcpgw._tool_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = self._saved_conn
        mcpgw._tool_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def acl(self, *rules):
        """rules: (agent_id, server_id, tool_pattern, allow) 按插入顺序落库。"""
        for aid, sid, pat, allow in rules:
            db.execute("INSERT INTO mcp_acl(agent_id,server_id,tool_pattern,allow)"
                       " VALUES(?,?,?,?)", (aid, sid, pat, 1 if allow else 0))

    def rows(self, agent):
        return mcpgw._acl_rows(agent)


# ══════════════════ 1. ACL 判定：唯一真相、且与插入顺序无关 ══════════════════

class AclSemantics(_DbCase):

    def test_no_rules_means_allow(self):
        """零规则行的 agent 默认放行（首次接入零摩擦）。这是既有语义，钉住不许漂。"""
        ok, reason, by = mcpgw._resolve_acl(self.rows("ghost"), "srv1", "read_file")
        self.assertTrue(ok)
        self.assertIn("首次接入", reason)
        self.assertEqual(by, [])

    def test_allow_rule_covers(self):
        self.acl(("manager", "ekko", "*", True))
        ok, reason, by = mcpgw._resolve_acl(self.rows("manager"), "ekko", "anything")
        self.assertTrue(ok, reason)
        self.assertEqual(len(by), 1)

    def test_deny_rule_blocks_with_rule_id(self):
        """红向：若实现忘了 deny 分支，这里必红。理由里必须带规则 id，否则运维无法定位。"""
        self.acl(("guest", None, "*", False))
        ok, reason, by = mcpgw._resolve_acl(self.rows("guest"), "any", "t")
        self.assertFalse(ok)
        self.assertIn("deny", reason)
        self.assertTrue(by, "拒绝理由必须点名是哪条规则")

    def test_whitelist_when_rules_exist_but_uncovered(self):
        """有规则但未覆盖本工具 ⇒ 拒（白名单语义）。旧注释把它写成"默认放行"，是错的。"""
        self.acl(("manager", "ekko", "read_*", True))
        ok, reason, _ = mcpgw._resolve_acl(self.rows("manager"), "ekko", "delete_all")
        self.assertFalse(ok, "有 allow 规则的 agent 必须落白名单语义")
        self.assertIn("未覆盖", reason)

    def test_star_deny_pushes_named_agent_into_whitelist(self):
        """本机生产实测形态：`*` 上挂一条 deny，则**所有** agent（含未配置者）都进白名单语义。
        红向：旧口径「无规则的 agent 默认放行」在这种库里是假的。"""
        self.acl(("*", "ekko", "*", False), ("manager", "ekko", "use", True))
        rows = self.rows("brand_new_agent")   # 只命中 `*` 那条
        ok, _, _ = mcpgw._resolve_acl(rows, "ekko", "use")
        self.assertFalse(ok, "`*` deny 已存在时，新 agent 不可能'默认放行'")

    def test_order_independence_invariant(self):
        """★ 核心不变式：同一份规则，**任意排列**（模拟索引改变行序）判定结果必须逐字相同。

        红向：旧写法 `for r in rows: 首条匹配即定生死` 在 [allow, deny] 与 [deny, allow]
        两种顺序下给出相反结论 ⇒ 本用例对它必红。真实触发路径是给 agent_id 建索引
        （实测：建索引后查询计划从 SCAN 翻成 SEARCH USING INDEX，行序真的会变）。
        """
        base = [{"id": 1, "agent_id": "manager", "server_id": "ekko", "tool_pattern": "use", "allow": 1},
                {"id": 2, "agent_id": "manager", "server_id": "other", "tool_pattern": "*", "allow": 0},
                {"id": 3, "agent_id": "manager", "server_id": None, "tool_pattern": "read_*", "allow": 1}]
        import itertools
        probes = [("ekko", "use"), ("ekko", "read_x"), ("ekko", "write_x"),
                  ("other", "read_x"), ("other", "use")]
        seen = {}
        for perm in itertools.permutations(base):
            snap = {}
            for srv, tool in probes:
                ok, _, by = mcpgw._resolve_acl(list(perm), srv, tool)
                snap[(srv, tool)] = (ok, tuple(sorted(by)))
            # 不变式：所有排列的快照必须完全一致
            for k, v in snap.items():
                self.assertEqual(seen.setdefault(k, v), v,
                                 f"{k} 的判定随规则行序变了（{v} ≠ {seen[k]}）⇒ deny 优先被绕过")
        # 具体期望：四个分支各钉一个，漏哪个都会被上面的排列扫描抓到
        self.assertEqual(seen[("ekko", "use")], (True, (1,)), "仅 allow 覆盖 → 放行")
        self.assertEqual(seen[("ekko", "read_x")], (True, (3,)), "NULL server 作用域的 allow 跨 server 生效")
        self.assertEqual(seen[("ekko", "write_x")], (False, ()), "有规则但不覆盖 → 白名单拒")
        self.assertEqual(seen[("other", "read_x")], (False, (2,)),
                         "allow(3) 与 deny(2) 同时覆盖 ⇒ deny 优先（旧写法在此处会随行序翻面）")
        self.assertEqual(seen[("other", "use")], (False, (2,)),
                         "other 上的 `*` deny 覆盖任何工具名 → 拒（带规则 id）")

    def test_deny_wins_over_allow_same_tool(self):
        self.acl(("manager", "ekko", "use", True), ("manager", "ekko", "use", False))
        ok, _, by = mcpgw._resolve_acl(self.rows("manager"), "ekko", "use")
        self.assertFalse(ok, "deny 必须优先于 allow，与插入先后无关")
        self.assertEqual(by, [2])

    def test_server_scoped_allow_not_leaked(self):
        self.acl(("manager", "ekko", "*", True))
        ok, _, _ = mcpgw._resolve_acl(self.rows("manager"), "other_srv", "x")
        self.assertFalse(ok, "server_id 限定的 allow 不得波及其他 server")

    def test_check_raises_403_and_anon_folding(self):
        """`_acl_check` 的两条对外行为：缺 agent_id 折成 anon 受 `*` 约束；拒绝时报 403。"""
        self.acl(("*", "ekko", "*", False))
        with self.assertRaises(HTTPException) as ctx:
            mcpgw._acl_check(None, "ekko", "use")
        self.assertEqual(ctx.exception.status_code, 403)
        self.assertIn("anon", ctx.exception.detail, "缺省身份必须折算 anon，不许免检")
        with self.assertRaises(HTTPException):
            mcpgw._acl_check("   ", "ekko", "use")   # 纯空白也算缺省身份

    def test_check_allows_when_uncovered_none(self):
        self.acl(("hub-self", "ekko", "*", True))
        mcpgw._acl_check("hub-self", "ekko", "any")   # 不该抛


# ══════════════════ 2. 工具形状：schema 透传与截断留痕 ══════════════════

class _FakeTool:
    """只带 name/description 的畸形对象（模拟上游不给 schema）。model_dump 故意不存在。"""

    def __init__(self, name, description=""):
        self.name = name
        self.description = description


class _PydTool:
    """模拟 mcp SDK 的 Tool：属性名 input_schema，别名 inputSchema。"""

    def __init__(self, name, description="", schema=None):
        self.name = name
        self.description = description
        self._schema = schema

    def model_dump(self, by_alias=False):
        key = "inputSchema" if by_alias else "input_schema"
        return {"name": self.name, "description": self.description, key: self._schema}


class ToolShape(unittest.TestCase):

    def test_schema_passthrough_by_alias(self):
        """红向：旧实现只出 4 个键，agent 拿不到参数 schema（前端是自由 JSON 文本框）。"""
        t = _PydTool("run", "do it", {"type": "object", "properties": {"cmd": {"type": "string"}}})
        d = mcpgw._tool_shape(t)
        self.assertEqual(d["inputSchema"]["properties"]["cmd"]["type"], "string")
        self.assertNotIn("schema_missing", d)

    def test_missing_schema_is_declared_not_silenced(self):
        """拿不到 schema 必须明说。给个空 dict 当"没有参数"＝骗 agent。"""
        d = mcpgw._tool_shape(_FakeTool("x", "desc"))
        self.assertEqual(d["inputSchema"], {})
        self.assertTrue(d.get("schema_missing"), "缺 schema 必须显式表态")

    def test_truncation_marked_with_original_length(self):
        long = "字" * (mcpgw.DOC_CHARS_MAX + 37)
        d = mcpgw._tool_shape(_FakeTool("y", long))
        self.assertEqual(len(d["description"]), mcpgw.DOC_CHARS_MAX)
        self.assertTrue(d["description_truncated"])
        self.assertEqual(d["description_chars"], mcpgw.DOC_CHARS_MAX + 37)

    def test_short_description_no_truncation_flag(self):
        d = mcpgw._tool_shape(_FakeTool("y", "short"))
        self.assertNotIn("description_truncated", d)

    def test_none_description_survives(self):
        self.assertEqual(mcpgw._tool_shape(_FakeTool("y", None))["description"], "")

    def test_model_dump_explosion_falls_back(self):
        class Boom(_PydTool):
            def model_dump(self, by_alias=False):
                raise RuntimeError("upstream pydantic changed")
        d = mcpgw._tool_shape(Boom("z", "d", {"a": 1}))
        self.assertTrue(d.get("schema_missing"), "model_dump 抛错时不得伪装成有 schema")


# ══════════════════ 3. /mcp/tools 与 /mcp/registry 的信封 ══════════════════

class _Stack:
    async def aclose(self):
        return None


class _Session:
    def __init__(self, tools):
        self._tools = tools

    async def list_tools(self):
        class R:
            pass
        r = R()
        r.tools = self._tools
        return r


def _patch_servers(servers):
    """db 里的 mcp_servers 由 servers 决定；_list_tools 走猴补（绝不 spawn 子进程）。"""
    for s in servers:
        db.execute("INSERT INTO mcp_servers(id,name,transport,command,args,env,url,"
                   "description,created_at,updated_at) VALUES(?,?,?,?,?,?,?,?,?,?)",
                   (s["id"], s["name"], "stdio", s["id"], "[]", "{}", None, "",
                     "2026-01-01T00:00:00+00:00", "2026-01-01T00:00:00+00:00"))


class Endpoints(_DbCase):

    def _run(self, coro):
        return asyncio.run(coro)

    def test_envelope_with_one_dead_backend(self):
        """一路 server 起不来 ⇒ 整体 200、清单只剩活的那路、degraded 点名、note 非空。
        红向：旧实现只回 errors dict，前端无法区分"真没工具"与"探测失败"（静默不可用家族）。"""
        _patch_servers([{"id": "s1", "name": "good"}, {"id": "s2", "name": "bad"}])

        async def fake_list(server, use_cache=True):
            if server["name"] == "bad":
                raise RuntimeError("spawn failed: no such file")
            return [_PydTool("t1", "hello", {"type": "object"})]

        orig = mcpgw._list_tools
        mcpgw._list_tools = fake_list
        try:
            resp = self._run(mcpgw.aggregated_tools())
        finally:
            mcpgw._list_tools = orig
        self.assertEqual(resp["count"], 1)
        self.assertEqual([b["name"] for b in resp["backends"]], ["good", "bad"])
        self.assertEqual(resp["degraded"], ["bad"])
        self.assertTrue(resp["note"], "有降级路时 note 必须点名")
        self.assertIn("bad", resp["errors"], "旧键 errors 必须保留（前端在吃）")
        t = resp["tools"][0]
        for k in ("server", "server_name", "name", "description"):
            self.assertIn(k, t, f"旧键 {k} 被删会让 loadMcp()/pickTool() 塌")
        self.assertEqual(t["inputSchema"], {"type": "object"})
        self.assertIsInstance(resp["took_ms"], int)
        self.assertTrue(all("ms" in b and b["count"] is not None for b in resp["backends"]))

    def test_envelope_all_ok_has_empty_note(self):
        _patch_servers([{"id": "s1", "name": "good"}])

        async def fake_list(server, use_cache=True):
            return [_PydTool("a", "x", {}), _PydTool("b", "y", {})]

        orig = mcpgw._list_tools
        mcpgw._list_tools = fake_list
        try:
            resp = self._run(mcpgw.aggregated_tools())
        finally:
            mcpgw._list_tools = orig
        self.assertEqual(resp["degraded"], [])
        self.assertEqual(resp["note"], "")
        self.assertEqual(resp["count"], 2)

    def test_registry_decisions_same_source_as_check(self):
        """★ registry 的 allowed 必须与 _acl_check 的生死**同源**（都走 _resolve_acl）。
        红向：若 registry 另写一份判定，两份真相必然漂移——那正是 09-24 侦察抓出的
        「前端写着默认放行、库里其实是白名单」的成因。"""
        _patch_servers([{"id": "ekko", "name": "ekko"}])
        self.acl(("manager", "ekko", "use", True), ("*", "ekko", "*", False))

        async def fake_list(server, use_cache=True):
            return [_FakeTool("use"), _FakeTool("wipe")]

        orig = mcpgw._list_tools
        mcpgw._list_tools = fake_list
        try:
            reg = self._run(mcpgw.tool_registry(agent="manager"))
        finally:
            mcpgw._list_tools = orig
        got = {t["name"]: t["allowed"] for s in reg["servers"] for t in s["tools"]}
        self.assertEqual(got, {"use": False, "wipe": False},
                         "`*` deny 覆盖一切 ⇒ registry 不得报 use 可用（与 _acl_check 同源）")
        for s in reg["servers"]:
            for t in s["tools"]:
                self.assertTrue(t["reason"], "每条判定都要带可读理由")
        self.assertIn("deny", reg["semantics"])
        self.assertEqual(reg["agent"], "manager")
        self.assertEqual(len(reg["rules"]), 2, "回显规则须含自身与 `*`")

    def test_registry_anon_when_agent_blank(self):
        _patch_servers([{"id": "s1", "name": "s1"}])

        async def fake_list(server, use_cache=True):
            return []

        orig = mcpgw._list_tools
        mcpgw._list_tools = fake_list
        try:
            reg = self._run(mcpgw.tool_registry(agent=""))
        finally:
            mcpgw._list_tools = orig
        self.assertEqual(reg["agent"], "anon")

    def test_probe_bypasses_cache(self):
        """probe 的语义是"改完 server 代码后预览"，吃 60s 缓存会静默给旧工具表。"""
        calls = {"n": 0}

        async def fake_session(server):
            calls["n"] += 1

            class S(_Session):
                async def list_tools(self):
                    class R:
                        pass
                    r = R()
                    r.tools = [_FakeTool(f"t{calls['n']}")]
                    return r
            return _Stack(), S(None)

        orig = mcpgw._session_for
        mcpgw._session_for = fake_session
        try:
            server = {"id": "px", "command": "px", "transport": "stdio",
                      "args": "[]", "env": "{}", "url": None}
            a = self._run(mcpgw._list_tools(dict(server), use_cache=True))
            b = self._run(mcpgw._list_tools(dict(server), use_cache=True))
            self.assertEqual(a[0].name, b[0].name, "第二次应命中缓存")
            self.assertEqual(calls["n"], 1)
            c = self._run(mcpgw._list_tools(dict(server), use_cache=False))
            self.assertEqual(c[0].name, "t2", "use_cache=False 必须真打上游")
            self.assertEqual(calls["n"], 2)
        finally:
            mcpgw._session_for = orig
            mcpgw._tool_cache.clear()


# ══════════════════ 4. 文档串与实现同源（防"注释说谎"）══════════════════

class DocTruth(unittest.TestCase):

    def test_module_docstring_matches_resolver(self):
        """模块顶部若还写"无规则的 agent 默认放行"而不提 `*` 通配，就是错的措辞。"""
        src = pathlib.Path(_REPO / "src" / "mcpgw.py").read_text(encoding="utf-8")
        head = src[:src.index("import asyncio")]
        self.assertIn("deny", head, "顶部口径必须写明 deny 优先")
        self.assertNotIn("无规则的 agent 默认放行", head)

    def test_no_duplicate_acl_implementation(self):
        """全文件只允许一处 fnmatch 判定入口（唯一真相），否则必然漂移。"""
        src = pathlib.Path(_REPO / "src" / "mcpgw.py").read_text(encoding="utf-8")
        self.assertEqual(src.count("fnmatch.fnmatch("), 1,
                         "ACL 匹配逻辑被抄第二份了 ⇒ 与 _resolve_acl 会漂移")

    def test_acl_rows_uses_order_by(self):
        src = pathlib.Path(_REPO / "src" / "mcpgw.py").read_text(encoding="utf-8")
        self.assertIn("FROM mcp_acl WHERE agent_id IN (?, '*') ORDER BY id", src)

    def test_mcp_call_never_rebinds_global_db(self):
        """★ 红向：调用实现里丌得再出现无条件 `db.init_db(...)`。
        旧写法会在全局已连着 tmp 库时把它重指到生产库 ⇒ 一发 `/mcp/call`，
        测试的写入就落在真库上（本机 09-24 实际造成过一次 mcp_servers/mcp_acl 污染）。
        同时 P0 往 init_db 里加了 wal_checkpoint(TRUNCATE) ⇒ 按次重进就是按次抢锁。
        PT-20261110-01：调用体抽到 `_call_one`（`/mcp/call` 与 `/mcp/run` 同源），
        判据跟着指向那一份唯一实现，不能因为换了函数名就放过。"""
        src = pathlib.Path(_REPO / "src" / "mcpgw.py").read_text(encoding="utf-8")
        body = src[src.index("async def _call_one"):]
        seg = body[:900]
        self.assertIn("if not db.is_open():", seg,
                      "init_db 没被“已连接则跳过”守住 ⇒ 全局连接可被单次调用重指")
        self.assertNotIn("\n    try:\n        db.init_db(", seg,
                      "又回到了无条件 init_db（每次工具调用抢一次 WAL checkpoint）")


if __name__ == "__main__":
    unittest.main(verbosity=2)
