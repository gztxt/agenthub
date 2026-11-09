#!/usr/bin/env python3
"""L0 hermetic · PT-20261110-01「MCP 服务器源 / 目录 / 一键编排」。

分层口径（tests/tiers.py）：零宿主依赖 —— 不起服务、不打真网络、不 spawn stdio
子进程、不 import `src.main`、db 指向 tmp 文件库、**不允许 SKIP**。
`unittest discover` 不加载 pytest 的 conftest，故本文件不依赖任何夹具。

钉住的不变量（每条都能确定性造红向）：

1. `split_credential_url` 必须把 `?key=xxx` 拆成占位符：否则凭据随
   `mcp_servers.url` 落库并被 `GET /mcp/servers` 回显（outbound.check_url 挡不住）。
2. `isSecret` 只回名字不回值；非敏感 default 才进 env。
3. `oci` / 非法 identifier / deprecated / 非 latest 一律不入可装池。
4. 存量库迁移：9 列补上、旧行新列 NULL、行数不丢、`ensure_schema()` 可重复跑。
5. `_list_tools` 的 http fake 不再 KeyError（旧 cache key 写法）。
6. 目录空 / 无词命中必须给 `why`，不许回一份看着像「没找到」的空表。
7. `/mcp/run` 的 `mode=plan` 是 dry：零写盘、零调用、零进程。
8. 同步失败不清空旧缓存，且 `last_error` / `degraded` 如实回。
"""
import asyncio
import json
import pathlib
import shutil
import sys
import tempfile
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db            # noqa: E402
import mcpgw         # noqa: E402
import mcpregistry   # noqa: E402
from fastapi import HTTPException  # noqa: E402

_V = "/fs/1000/ftp/技术文档/agenthub/venv/bin/python"  # 仅文档用，不执行


class _DbCase(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0mcpsrc-"))
        self._saved = db._conn
        db.init_db(self.tmp / "t.db")
        mcpgw.ensure_schema()
        mcpgw._tool_cache.clear()
        self.addCleanup(self._restore)

    def _restore(self):
        try:
            db._conn.close()
        except Exception:  # noqa: BLE001
            pass
        db._conn = self._saved
        mcpgw._tool_cache.clear()
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _card(self, name="io.github.acme/fs-mcp", **kw):
        c = {"name": name, "version": "1.2.3",
             "description": "filesystem tools for local files",
             "packages": [{"registryType": "npm",
                           "identifier": "@acme/fs-mcp", "version": "1.2.3",
                           "runtimeHint": "npx",
                           "runtimeArguments": [{"type": "positional", "value": "-y"}],
                           "environmentVariables": [
                               {"name": "WORKSPACE_ROOT", "isRequired": True,
                                "description": "root dir"},
                               {"name": "API_TOKEN", "isSecret": True,
                                "description": "token"},
                               {"name": "READONLY", "default": "true"}]}]}
        c.update(kw)
        return c

    def _put_card(self, card, **over):
        row = {"cache_key": over.get("cache_key", "k-" + card["name"]),
               "source_id": over.get("source_id", mcpregistry.DEFAULT_SOURCE_ID),
               "name": card["name"], "version": card.get("version", ""),
               "title": over.get("title", card["name"]),
               "description": over.get("description", card.get("description", "")),
               "status": over.get("status", "active"),
               "is_latest": over.get("is_latest", 1),
               "server_json": json.dumps(card, ensure_ascii=False),
               "transport_hint": over.get("transport_hint", "stdio"),
               "has_secret": over.get("has_secret", 0)}
        db.execute(
            "INSERT OR REPLACE INTO mcp_source_cache"
            "(cache_key,source_id,name,version,title,description,tags,server_json,"
            " status,is_latest,fetched_at,transport_hint,has_secret)"
            " VALUES(?,?,?,?,?,?,'[]',?,?,?,?,?,?)",
            (row["cache_key"], row["source_id"], row["name"], row["version"],
             row["title"], row["description"], row["server_json"], row["status"],
             row["is_latest"], "2026-11-10T00:00:00+00:00",
             row["transport_hint"], row["has_secret"]))
        return row["cache_key"]

    def _stub_request(self):
        state = type("S", (), {"actor": "l0-tester"})()
        return type("R", (), {"state": state})()


# ── 1. 凭据拆分 / 注入 ─────────────────────────────────────────────────────

class TestCredentialUrl(unittest.TestCase):
    def test_amap_key_is_stripped(self):
        url, names = mcpregistry.split_credential_url(
            "https://mcp.amap.com/mcp?key=SECRET_ABC123&city=hangzhou")
        self.assertNotIn("SECRET_ABC123", url)
        self.assertIn("${KEY}", url)
        self.assertIn("city=hangzhou", url)
        self.assertEqual(names, ["KEY"])

    def test_placeholder_is_not_double_wrapped(self):
        url, names = mcpregistry.split_credential_url(
            "https://x.example/mcp?key=${KEY}")
        self.assertEqual(names, [])
        self.assertIn("${KEY}", url)

    def test_plain_url_untouched(self):
        url, names = mcpregistry.split_credential_url(
            "https://mcp.example.com/mcp")
        self.assertEqual(names, [])
        self.assertEqual(url, "https://mcp.example.com/mcp")

    def test_resolve_injects_and_reports_missing(self):
        clean, names = mcpregistry.split_credential_url(
            "https://mcp.amap.com/mcp?key=SECRET")
        resolved, missing = mcpregistry.resolve_url(clean, {"KEY": "a b&c"})
        self.assertEqual(missing, [])
        self.assertNotIn("SECRET", resolved)
        self.assertIn("a%20b%26c", resolved)
        _, missing2 = mcpregistry.resolve_url(clean, {})
        self.assertEqual(missing2, names)


# ── 2. server.json 映射 ────────────────────────────────────────────────────

class TestMapServerJson(unittest.TestCase):
    def test_npm_secret_is_name_only(self):
        spec = mcpregistry.map_server_json(_DbCase._card(None))
        self.assertTrue(spec["installable"])
        self.assertEqual(spec["transport"], "stdio")
        self.assertEqual(spec["command"], "npx")
        self.assertEqual(spec["args"], ["-y", "@acme/fs-mcp"])
        self.assertIn("API_TOKEN", spec["required_secrets"])
        self.assertNotIn("API_TOKEN", spec["env"])
        self.assertNotIn("SECRET", json.dumps(spec))
        self.assertEqual(spec["env"].get("READONLY"), "true")
        self.assertTrue(spec["has_secret"])

    def test_pypi_uses_uvx(self):
        card = {"name": "x/y", "packages": [
            {"registryType": "pypi", "identifier": "my-mcp-server"}]}
        spec = mcpregistry.map_server_json(card)
        self.assertEqual(spec["command"], "uvx")
        self.assertEqual(spec["args"], ["my-mcp-server"])

    def test_oci_not_installable(self):
        card = {"name": "x/y", "packages": [
            {"registryType": "oci", "identifier": "ghcr.io/x/y:1"}]}
        spec = mcpregistry.map_server_json(card)
        self.assertFalse(spec["installable"])
        self.assertIn("oci", spec["reason"])

    def test_bad_identifier_rejected(self):
        card = {"name": "x/y", "packages": [
            {"registryType": "npm", "identifier": "pkg; rm -rf /"}]}
        spec = mcpregistry.map_server_json(card)
        self.assertFalse(spec["installable"])

    def test_remotes_preferred_over_packages(self):
        card = {"name": "x/y",
                "packages": [{"registryType": "npm", "identifier": "pkg"}],
                "remotes": [{"type": "streamable-http",
                             "url": "https://mcp.example.com/mcp?key=SECRET"}]}
        spec = mcpregistry.map_server_json(card)
        self.assertTrue(spec["installable"])
        self.assertEqual(spec["transport"], "http")
        self.assertNotIn("SECRET", spec["url"])
        self.assertEqual(spec["required_secrets"], ["KEY"])

    def test_no_packages_no_remotes_is_loud(self):
        spec = mcpregistry.map_server_json({"name": "x/y"})
        self.assertFalse(spec["installable"])
        self.assertTrue(spec["reason"])


# ── 3. 迁移 ───────────────────────────────────────────────────────────────

class TestMigration(_DbCase):
    def test_columns_added_old_rows_null_and_idempotent(self):
        # 造一行「存量手工注册」记录（只填旧列）
        db.execute(
            "INSERT INTO mcp_servers(id,name,transport,command,args,env,url,"
            "description,created_at,updated_at) VALUES('old1','legacy','stdio',"
            "'echo','[]','{}',NULL,'','2026-01-01','2026-01-01')")
        mcpgw.ensure_schema()          # 重复跑不得挂、不得丢行
        cols = {r["name"] for r in db.query("PRAGMA table_info(mcp_servers)")}
        for col, _ in mcpgw.MCP_SERVER_EXTRA_COLUMNS:
            self.assertIn(col, cols)
        row = db.query("SELECT * FROM mcp_servers WHERE id='old1'")[0]
        self.assertIsNone(row["source_id"])
        self.assertEqual(int(row["use_count"] or 0), 0)
        self.assertEqual(len(db.query("SELECT * FROM mcp_servers")), 1)

    def test_index_created_after_alter(self):
        idx = db.query("SELECT name FROM sqlite_master WHERE type='index'"
                       " AND name='idx_mcp_servers_source'")
        self.assertEqual(len(idx), 1)

    def test_default_source_seeded_once(self):
        n1 = len(mcpregistry.source_list())
        mcpgw.ensure_schema()
        self.assertEqual(len(mcpregistry.source_list()), n1)
        self.assertGreaterEqual(n1, 1)


# ── 4. 源 CRUD 准入 ───────────────────────────────────────────────────────

class TestSourceCrud(_DbCase):
    def test_private_and_http_rejected(self):
        with self.assertRaises(ValueError):
            mcpregistry.source_add("subregistry", "bad",
                                   "http://169.254.169.254/latest/meta-data")
        with self.assertRaises(ValueError):
            mcpregistry.source_add("subregistry", "bad", "http://example.com/v1")

    def test_dedupe(self):
        a = mcpregistry.source_add("subregistry", "A", "https://a.example.com/v1")
        b = mcpregistry.source_add("subregistry", "B", "https://a.example.com/v1")
        self.assertFalse(a["deduped"])
        self.assertTrue(b["deduped"])
        self.assertEqual(len(mcpregistry.source_list()), 2)  # 含默认官方源

    def test_patch_disable_and_delete(self):
        r = mcpregistry.source_add("manual_url", "M", "https://m.example.com/v1")
        sid = r["id"]
        mcpregistry.source_patch(sid, enabled=False)
        self.assertFalse(mcpregistry.source_get(sid) is None)
        self.assertEqual(int(mcpregistry.source_get(sid)["enabled"] or 0), 0)
        mcpregistry.source_delete(sid)
        self.assertIsNone(mcpregistry.source_get(sid))


# ── 5. 目录 / 质量闸门 ────────────────────────────────────────────────────

class TestCatalog(_DbCase):
    def test_empty_catalog_is_loud(self):
        r = mcpregistry.catalog("地图")
        self.assertEqual(r["items"], [])
        self.assertTrue(r["why"])
        self.assertIn("缓存为空", r["why"])

    def test_chinese_alias_expansion(self):
        self._put_card(self._card(name="io.github.acme/map-mcp",
                                  description="geocode and location search"))
        r = mcpregistry.catalog("需要地图功能", n=5)
        self.assertEqual(r["selected"], 1)
        self.assertEqual(r["items"][0]["name"], "io.github.acme/map-mcp")
        self.assertIn("map", r["items"][0]["name"])

    def test_no_query_lists_by_name_with_note(self):
        self._put_card(self._card())
        r = mcpregistry.catalog("", n=5)
        self.assertEqual(r["selected"], 1)
        self.assertIn("未提供任务描述", r["note"])

    def test_high_df_term_falls_back_and_says_so(self):
        """窄语料里查询词出现在几乎每张卡的 name+description ⇒ 共享 build_index
        按字段累计 df 让 idf 变负 ⇒ rank 返回空。必须回退且**说明原因**，
        不许静默回一份「没有相关候选」。"""
        for i in range(8):
            # 名字里也带 filesystem ⇒ 该词同时进 name / triggers(tags) / desc，
            # df 按字段累计会 > 语料数 ⇒ idf 变负（共享实现的边界）。
            self._put_card(self._card(
                name="io.github.acme/filesystem-%d" % i,
                description="filesystem tools for local files"),
                cache_key="fs-%d" % i)
        r = mcpregistry.catalog("filesystem", n=5)
        self.assertGreater(r["selected"], 0)
        self.assertIn("IDF 塌陷", r["note"])
        self.assertIsNone(r["why"])

    def test_deprecated_and_nonlatest_not_installable(self):
        k1 = self._put_card(self._card(), status="deprecated", cache_key="dep")
        k2 = self._put_card(self._card(), is_latest=0, cache_key="old")
        with self.assertRaises(PermissionError):
            mcpregistry.prepare_install(k1)
        with self.assertRaises(PermissionError):
            mcpregistry.prepare_install(k2)

    def test_prepare_install_never_leaks_secret_value(self):
        key = self._put_card(self._card())
        p = mcpregistry.prepare_install(key)
        spec = p["spec"]
        self.assertIn("API_TOKEN", spec["required_secrets"])
        self.assertNotIn("API_TOKEN", spec["env"])
        self.assertNotIn("SECRET", json.dumps(p))

    def test_remote_url_with_key_has_no_plaintext(self):
        card = {"name": "x/amap", "version": "1.0.0",
                "remotes": [{"type": "streamable-http",
                             "url": "https://mcp.amap.com/mcp?key=REALKEY123"}]}
        key = self._put_card(card, transport_hint="http")
        p = mcpregistry.prepare_install(key)
        self.assertNotIn("REALKEY123", p["spec"]["url"])
        self.assertEqual(p["spec"]["required_secrets"], ["KEY"])


# ── 6. 同步失败不清空旧缓存 ───────────────────────────────────────────────

class TestSync(_DbCase):
    def test_failure_keeps_cache_and_reports(self):
        self._put_card(self._card(), cache_key="keepme")
        with mock.patch.object(mcpregistry, "_fetch_page",
                               return_value=([], "", "HTTP 503 upstream")):
            r = mcpregistry.sync_source(mcpregistry.DEFAULT_SOURCE_ID)
        self.assertTrue(r["degraded"])
        self.assertIn("503", r["last_error"])
        self.assertEqual(len(db.query("SELECT * FROM mcp_source_cache")), 1)
        src = mcpregistry.source_get(mcpregistry.DEFAULT_SOURCE_ID)
        self.assertIn("503", src["last_error"])

    def test_success_upserts(self):
        card = self._card()
        entry = {"server": card, "_meta": {
            "io.modelcontextprotocol.registry/official": {
                "status": "active", "isLatest": True}}}
        with mock.patch.object(mcpregistry, "_fetch_page",
                               return_value=([entry], "", "")):
            r = mcpregistry.sync_source(mcpregistry.DEFAULT_SOURCE_ID)
        self.assertFalse(r["degraded"])
        self.assertEqual(r["fetched"], 1)
        rows = db.query("SELECT * FROM mcp_source_cache")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["name"], card["name"])


# ── 7. http probe / 编排 dry ──────────────────────────────────────────────

class _FakeSession:
    async def list_tools(self):
        return type("R", (), {"tools": []})()

    async def initialize(self):
        return None


class TestProbeAndRun(_DbCase):
    def test_http_probe_no_keyerror(self):
        """旧 cache key `server["command"]` 在 http fake 上必 KeyError。"""
        async def fake_session(_server, _env=None):
            class _Stack:
                async def aclose(self):
                    return None
            return _Stack(), _FakeSession()

        with mock.patch.object(mcpgw, "_session_for", fake_session):
            tools = asyncio.run(mcpgw._list_tools(
                {"transport": "http", "url": "https://x.example/mcp",
                 "command": None, "args": "[]", "env": "{}"},
                use_cache=False))
        self.assertEqual(tools, [])

    def test_run_plan_is_dry(self):
        self._put_card(self._card())
        before = len(db.query("SELECT * FROM mcp_servers"))
        r = asyncio.run(mcpgw.mcp_run(
            mcpgw.RunIn(task="需要文件功能", mode="plan"),
            self._stub_request()))
        self.assertEqual(r["mode"], "plan")
        self.assertEqual(r["calls"], [])
        self.assertEqual(r["installed"], [])
        self.assertIn("dry", r["note"])
        self.assertEqual(len(db.query("SELECT * FROM mcp_servers")), before)
        self.assertEqual(len(db.query("SELECT * FROM profile_events"
                                      " WHERE source='mcp_call'")), 0)

    def test_run_exec_no_target_is_loud(self):
        r = asyncio.run(mcpgw.mcp_run(
            mcpgw.RunIn(task="anything", mode="exec"),
            self._stub_request()))
        self.assertEqual(r["calls"], [])
        self.assertTrue(any("没有可调用的目标工具" in x for x in r["degraded"]))

    def test_run_rejects_bad_mode(self):
        with self.assertRaises(HTTPException):
            asyncio.run(mcpgw.mcp_run(
                mcpgw.RunIn(task="x", mode="bogus"),
                self._stub_request()))


if __name__ == "__main__":
    unittest.main()
