#!/usr/bin/env python3
"""catalog P0 hermetic 闸门：只使用仓内临时夹具，不读宿主数据。"""
from __future__ import annotations

import os
import sqlite3
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

import api_catalog
from catalog import adapter, adapters, query, schema, store
from catalog.adapters import agents as agents_adapter
from catalog.adapters import projects as projects_adapter


class CatalogCase(unittest.TestCase):
    def setUp(self):
        work = Path(__file__).resolve().parents[1] / "work"
        work.mkdir(exist_ok=True)
        self.tmp = tempfile.TemporaryDirectory(prefix="catalog-l0-", dir=work)
        self.path = Path(self.tmp.name) / "catalog.db"
        self.writer = store.CatalogWriter(self.path)
        self.writer.open()
        self.writer.upsert_src("s1", "skill", "file", "fixture://s1", "fixture")

    def tearDown(self):
        self.writer.close()
        self.tmp.cleanup()

    def add(self, native_key="a", title="统一数据存储", chunks=None,
            domain="skill", src_id="s1", hash_="h1", **kw):
        rec = {"domain": domain, "native_key": native_key, "title": title,
               "summary": kw.pop("summary", "编目摘要"),
               "uri": kw.pop("uri", "fixture://%s" % native_key),
               "mtime": kw.pop("mtime", 1000), "size": kw.pop("size", 10),
               "hash": hash_, "ext": kw.pop("ext", {"name": native_key}),
               "chunks": chunks if chunks is not None else ["统一数据存储方案"],
               **kw}
        return self.writer.upsert_objects([rec], src_id)

    def readonly(self):
        return store.connect(self.path, readonly=True)


class TestSchema(CatalogCase):
    def test_schema_is_idempotent_and_versioned(self):
        first = schema.ensure_schema(self.writer.con)
        second = schema.ensure_schema(self.writer.con)
        self.assertFalse(first["created"])
        self.assertFalse(second["created"])
        self.assertEqual(schema.SCHEMA_VERSION, second["version"])
        self.assertEqual("chunk_fts", self.writer.con.execute(
            "SELECT name FROM sqlite_master WHERE name='chunk_fts'"
        ).fetchone()[0])

    def test_schema_does_not_downgrade_newer_version(self):
        self.writer.con.execute(
            "INSERT INTO meta(k,v) VALUES('schema_version','999') "
            "ON CONFLICT(k) DO UPDATE SET v=excluded.v")
        schema.ensure_schema(self.writer.con)
        got = self.writer.con.execute(
            "SELECT v FROM meta WHERE k='schema_version'").fetchone()[0]
        self.assertEqual("999", got)

    def test_oid_is_stable_and_namespace_sensitive(self):
        oid = schema.oid_of("skill", "s1", "a")
        self.assertEqual(oid, schema.oid_of("skill", "s1", "a"))
        self.assertEqual(16, len(oid))
        self.assertNotEqual(oid, schema.oid_of("agent", "s1", "a"))
        self.assertNotEqual(oid, schema.oid_of("skill", "s2", "a"))

    def test_cjk_split_preserves_ascii_words(self):
        self.assertEqual("统 一 数 据 AI-Store 存 储",
                         schema.cjk_split("统一数据 AI-Store 存储"))
        self.assertEqual("", schema.cjk_split(""))

    def test_chunk_text_empty_and_boundaries(self):
        self.assertEqual([], schema.chunk_text("  "))
        self.assertEqual(["1234"], schema.chunk_text("1234", target=4, overlap=1))
        self.assertEqual(["1234", "3456", "56"], schema.chunk_text(
            "123456", target=4, overlap=2))
        self.assertEqual([], schema.chunk_text("abcdef", max_chunks=0))

    def test_chunk_text_rejects_invalid_window(self):
        for kwargs in ({"target": 0}, {"target": 4, "overlap": 4},
                       {"target": 4, "overlap": -1}, {"max_chunks": -1}):
            with self.subTest(kwargs=kwargs), self.assertRaises(ValueError):
                schema.chunk_text("abc", **kwargs)

    def test_integrity_check_empty(self):
        con = self.readonly()
        self.addCleanup(con.close)
        out = schema.integrity_check(con)
        self.assertEqual(0, out["orphan_chunks"])
        self.assertEqual(0, out["dangling_ext"])
        self.assertTrue(out["fts_ok"])


class TestStore(CatalogCase):
    def test_upsert_insert_skip_update_counts(self):
        self.assertEqual({"added": 1, "updated": 0, "skipped": 0, "healed": 0},
                         self.add())
        self.assertEqual({"added": 0, "updated": 0, "skipped": 1, "healed": 0},
                         self.add())
        self.assertEqual({"added": 0, "updated": 1, "skipped": 0, "healed": 0},
                         self.add(title="新标题", hash_="h2"))

    def test_same_native_key_is_isolated_by_source(self):
        self.writer.upsert_src("s2", "skill", "file", "fixture://s2", "fixture")
        self.add(src_id="s1")
        self.add(src_id="s2")
        count = self.writer.con.execute(
            "SELECT COUNT(*) FROM obj WHERE native_key='a'").fetchone()[0]
        self.assertEqual(2, count)

    def test_missing_requires_complete_namespace(self):
        self.add()
        oid = schema.oid_of("skill", "s1", "a")
        self.assertEqual(0, self.writer.mark_missing("s1", [], False))
        self.assertEqual("ok", self.writer.con.execute(
            "SELECT status FROM obj WHERE oid=?", (oid,)).fetchone()[0])
        self.assertEqual(1, self.writer.mark_missing("s1", [], True))
        self.assertEqual("missing", self.writer.con.execute(
            "SELECT status FROM obj WHERE oid=?", (oid,)).fetchone()[0])

    def test_missing_object_reappears_on_next_upsert(self):
        self.add()
        oid = schema.oid_of("skill", "s1", "a")
        self.writer.mark_missing("s1", [], True)
        self.add()
        self.assertEqual("ok", self.writer.con.execute(
            "SELECT status FROM obj WHERE oid=?", (oid,)).fetchone()[0])

    def test_no_body_metadata_is_searchable(self):
        self.add(chunks=[], title="统一编目 agent", summary="数据存储")
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "数据")
        self.assertEqual(1, result["count"])
        self.assertEqual("统一编目 agent", result["items"][0]["title"])

    def test_long_document_uni_index_is_capped(self):
        self.add(chunks=["开头" + "甲" * 4100, "尾词仅在后块"])
        con = self.readonly()
        self.addCleanup(con.close)
        self.assertEqual(1, con.execute(
            "SELECT COUNT(*) FROM chunk_uni").fetchone()[0])
        self.assertEqual(0, query.query(con, "尾词")["count"])
        self.assertTrue(schema.integrity_check(con)["fts_ok"])

    def test_extension_rows_follow_domain(self):
        self.add(domain="agent", ext={"agent_id": "a", "installed": 1})
        row = self.writer.con.execute(
            "SELECT agent_id, installed FROM x_agent").fetchone()
        self.assertEqual(("a", 1), tuple(row))

    def test_foreign_key_rejects_orphan_object(self):
        with self.assertRaises(sqlite3.IntegrityError):
            self.writer.con.execute(
                "INSERT INTO obj(oid,domain,type,src_id,native_key,"
                "first_seen,last_seen) VALUES('x','skill','skill','bad','a','n','n')")

    def test_scan_run_records_success(self):
        with self.writer.scan_run("s1", "incremental") as stats:
            stats["added"] = 3
        row = self.writer.con.execute(
            "SELECT status,added,errors FROM scan_run").fetchone()
        self.assertEqual(("ok", 3, 0), tuple(row))

    def test_scan_run_records_failure(self):
        with self.assertRaisesRegex(ValueError, "boom"):
            with self.writer.scan_run("s1", "incremental"):
                raise ValueError("boom")
        row = self.writer.con.execute(
            "SELECT status,errors,note FROM scan_run").fetchone()
        self.assertEqual("error", row[0])
        self.assertEqual(1, row[1])
        self.assertIn("boom", row[2])

    def test_readonly_connection_rejects_write(self):
        con = self.readonly()
        self.addCleanup(con.close)
        with self.assertRaises(sqlite3.OperationalError):
            con.execute("INSERT INTO meta(k,v) VALUES('x','y')")

    def test_default_path_uses_hub_config_data_dir(self):
        fake = types.SimpleNamespace(config=types.SimpleNamespace(
            data_dir=Path("/isolated/data")))
        with mock.patch.dict(os.environ, {}, clear=True), \
                mock.patch.dict(sys.modules, {"config": fake}):
            self.assertEqual(Path("/isolated/data/catalog.db"), store.default_path())

    def test_default_path_agenthub_home_has_priority(self):
        with mock.patch.dict(os.environ, {"AGENTHUB_HOME": "/portable"}, clear=True):
            self.assertEqual(Path("/portable/data/catalog.db"), store.default_path())


class TestQuery(CatalogCase):
    def test_short_chinese_uses_uni_without_degraded(self):
        self.add()
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "数据")
        self.assertEqual(["chunk_ftsu"], result["routing"])
        self.assertIsNone(result["degraded"])
        self.assertEqual(1, result["count"])

    def test_long_chinese_uses_trigram(self):
        self.add()
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "数据存储")
        self.assertEqual(["chunk_fts"], result["routing"])
        self.assertEqual(1, result["count"])

    def test_mixed_query_uses_both_indexes(self):
        self.add(chunks=["统一数据存储 AI 编目"])
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "数据存储 AI")
        self.assertEqual(["chunk_fts", "chunk_ftsu"], result["routing"])
        self.assertEqual(2, len(result["backends"]))

    def test_domain_and_type_filters(self):
        self.add(native_key="skill")
        self.add(native_key="agent", domain="agent", type="agent")
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "数据", domains="agent", types="agent")
        self.assertEqual(1, result["count"])
        self.assertEqual("agent", result["items"][0]["domain"])

    def test_since_filter(self):
        self.add(native_key="old", mtime=100)
        self.add(native_key="new", mtime=200)
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "数据", since=150)
        self.assertEqual(["new"], [x["native_key"] for x in result["items"]])

    def test_unfiltered_result_has_domain_diversity_cap(self):
        for key in ("a", "b", "c"):
            self.add(native_key=key)
        con = self.readonly()
        self.addCleanup(con.close)
        self.assertEqual(2, query.query(con, "数据", k=3)["count"])
        self.assertEqual(3, query.query(con, "数据", domains="skill", k=3)["count"])

    def test_title_fallback_is_case_insensitive(self):
        self.add(native_key="cb", title="CodeBuddy", chunks=["无关正文"])
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, "codebuddy")
        self.assertEqual(1, result["count"])
        self.assertIn("obj_title", result["routing"])

    def test_like_wildcards_are_not_query_syntax(self):
        self.add(title="普通标题", chunks=["无关正文"])
        con = self.readonly()
        self.addCleanup(con.close)
        self.assertEqual(0, query.query(con, "%_")["count"])

    def test_empty_query_is_structured_error(self):
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, " ")
        self.assertFalse(result["ok"])
        self.assertEqual("empty_query", result["degraded"])

    def test_malformed_fts_input_does_not_crash(self):
        self.add()
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.query(con, '"')
        self.assertTrue(result["ok"])
        self.assertIn("items", result)

    def test_get_returns_extension_chunks_and_neighbors(self):
        self.add()
        oid = schema.oid_of("skill", "s1", "a")
        other = schema.oid_of("skill", "s1", "b")
        self.writer.con.execute(
            "INSERT INTO edge(src_oid,dst_oid,rel) VALUES(?,?,?)",
            (oid, other, "related"))
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.get(con, oid)
        self.assertTrue(result["ok"])
        self.assertEqual("a", result["ext"]["name"])
        self.assertEqual(1, result["chunks"])
        self.assertEqual(other, result["neighbors"][0]["dst_oid"])

    def test_get_missing_oid(self):
        con = self.readonly()
        self.addCleanup(con.close)
        self.assertFalse(query.get(con, "missing")["ok"])

    def test_status_reports_source_and_lag(self):
        with self.writer.scan_run("s1", "incremental"):
            pass
        con = self.readonly()
        self.addCleanup(con.close)
        result = query.status(con)
        self.assertTrue(result["ok"])
        self.assertEqual("s1", result["sources"][0]["src_id"])
        self.assertIsNotNone(result["sources"][0]["lag_s"])


class TestAdapters(CatalogCase):
    def test_fingerprint_stable_and_change_sensitive(self):
        value = {"b": 2, "a": 1}
        self.assertEqual(adapter.fingerprint(value),
                         adapter.fingerprint({"a": 1, "b": 2}))
        self.assertNotEqual(adapter.fingerprint(value),
                            adapter.fingerprint({"a": 1, "b": 3}))

    def test_base_adapter_skip_rules(self):
        base = adapter.BaseAdapter()
        states = {"a": (1, 2, "h")}
        self.assertTrue(base.skip(states, "a", 1, 2, "h"))
        self.assertFalse(base.skip(states, "a", 2, 2, "h"))
        self.assertFalse(base.skip(states, "a", 1, 2, "different"))
        self.assertFalse(base.skip(states, "missing", 1, 2, "h"))

    def test_run_adapter_counts_preskipped_records(self):
        self.add()
        oid = schema.oid_of("skill", "s1", "a")

        class Fake(adapter.BaseAdapter):
            src_id = "s1"
            domain = "skill"
            uri = "fixture://s1"

            def collect(self, states):
                return {"records": [], "live": [oid], "complete": True}

        result = adapter.run_adapter(self.writer, Fake())
        self.assertEqual(1, result["skipped"])
        self.assertEqual(0, result["deleted"])

    def test_probe_failure_does_not_mark_existing_missing(self):
        self.add()

        class Broken(adapter.BaseAdapter):
            src_id = "s1"
            domain = "skill"

            def probe(self):
                return {"ok": False, "reason": "no_dir", "error": "gone"}

        with self.assertRaisesRegex(RuntimeError, "no_dir"):
            adapter.run_adapter(self.writer, Broken())
        self.assertEqual("ok", self.writer.con.execute(
            "SELECT status FROM obj WHERE native_key='a'").fetchone()[0])

    def test_projects_epoch_accepts_iso_and_rejects_bad_value(self):
        self.assertEqual(0, projects_adapter._epoch(0))
        self.assertEqual(0, projects_adapter._epoch("1970-01-01T00:00:00+00:00"))
        self.assertIsNone(projects_adapter._epoch("not-a-time"))
        self.assertIsNone(projects_adapter._epoch(None))

    def test_agents_discovered_missing_table_is_empty(self):
        db = Path(self.tmp.name) / "empty.db"
        sqlite3.connect(db).close()
        self.assertEqual([], agents_adapter._discovered(db))

    def test_agents_discovered_bad_database_raises(self):
        db = Path(self.tmp.name) / "bad.db"
        db.write_bytes(b"not sqlite")
        with self.assertRaisesRegex(RuntimeError, "只读"):
            agents_adapter._discovered(db)

    def test_registry_has_p0_and_p1_sources(self):
        """P0+P1 全部适配器注册检查。"""
        expected = {"agents.registry", "projects.local", "skills.disk",
                    "sessions.archived", "docs.filesystem",
                    "memory.archive", "logs.hub"}
        self.assertEqual(expected, set(adapters.REGISTRY))


class TestApi(CatalogCase):
    def setUp(self):
        super().setUp()
        self.patch = mock.patch.object(api_catalog.store, "default_path",
                                       return_value=self.path)
        self.patch.start()

    def tearDown(self):
        self.patch.stop()
        super().tearDown()

    @staticmethod
    def search(**kw):
        args = {"q": "数据", "domains": "", "types": "", "k": 10, "since": ""}
        args.update(kw)
        return api_catalog.search(**args)

    @staticmethod
    def get(**kw):
        args = {"oid": "", "domain": "", "native_key": "", "src_id": ""}
        args.update(kw)
        return api_catalog.get(**args)

    def test_api_query_and_get_by_native_key(self):
        self.add()
        result = self.search()
        self.assertTrue(result["ok"])
        detail = self.get(domain="skill", native_key="a")
        self.assertEqual("a", detail["item"]["native_key"])

    def test_api_rejects_bad_domain_and_since(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self.search(domains="unknown")
        self.assertEqual(400, caught.exception.status_code)
        with self.assertRaises(HTTPException):
            self.search(since="yesterday")

    def test_api_status_unavailable_is_explicit(self):
        self.writer.close()
        self.path.unlink()
        result = api_catalog.status()
        self.assertFalse(result["ok"])
        self.assertFalse(result["available"])
        self.assertIn("编目库不可用", result["error"])

    def test_api_native_key_ambiguity_requires_source(self):
        from fastapi import HTTPException
        self.writer.upsert_src("s2", "skill", "file", "fixture://s2", "fixture")
        self.add(src_id="s1")
        self.add(src_id="s2")
        with self.assertRaises(HTTPException) as caught:
            self.get(domain="skill", native_key="a")
        self.assertEqual(409, caught.exception.status_code)
        result = self.get(domain="skill", native_key="a", src_id="s2")
        self.assertEqual("s2", result["item"]["src_id"])

    def test_api_get_requires_a_locator(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as caught:
            self.get()
        self.assertEqual(400, caught.exception.status_code)


class TestIndexCompleteness(CatalogCase):
    """索引完整性闸门：哈希相同**不代表**索引完整。

    缺陷背景：早期版本对「无正文对象」只写 obj 行、不写 chunk；
    此后每轮增量都跳过它们 ⇒ 永远没有 chunk ⇒ FTS 永远搜不到
    （实测 "claude" 30 条命中全在 skill 域，agent/project 全部缺席）。
    """

    def test_states_report_missing_chunk_as_unmatched(self):
        self.add(native_key="a", hash_="h1")
        states = self.writer.existing_states("s1")
        self.assertEqual(("a",), tuple(states))
        # 有 chunk ⇒ hash 正常带出
        self.assertEqual("h1", states["a"][2])

    def test_states_null_hash_when_object_has_no_chunk(self):
        self.add(native_key="a", hash_="h1")
        self.writer.con.execute("DELETE FROM chunk WHERE oid IN "
                                "(SELECT oid FROM obj WHERE native_key='a')")
        states = self.writer.existing_states("s1")
        self.assertIsNone(states["a"][2])

    def test_upsert_heals_missing_chunk_on_skip(self):
        self.add(native_key="a", hash_="h1")
        oid = schema.oid_of("skill", "s1", "a")
        self.writer.con.execute("DELETE FROM chunk WHERE oid=?", (oid,))
        self.writer.con.execute("DELETE FROM chunk_uni WHERE oid=?", (oid,))
        stats = self.add(native_key="a", hash_="h1")
        self.assertEqual(1, stats["skipped"])
        self.assertEqual(1, stats["healed"])
        n = self.writer.con.execute(
            "SELECT COUNT(*) FROM chunk WHERE oid=?", (oid,)).fetchone()[0]
        self.assertEqual(1, n)

    def test_bodyless_object_is_searchable_after_heal(self):
        """无正文对象（只有标题/摘要）也必须能被 FTS 命中。"""
        self.add(native_key="agent-x", title="CodeBuddy agent",
                 domain="agent", chunks=[], hash_="h1")
        oid = schema.oid_of("agent", "s1", "agent-x")
        self.writer.con.execute("DELETE FROM chunk WHERE oid=?", (oid,))
        self.writer.con.execute("DELETE FROM chunk_uni WHERE oid=?", (oid,))
        self.add(native_key="agent-x", title="CodeBuddy agent",
                 domain="agent", chunks=[], hash_="h1")
        con = self.readonly()
        try:
            result = query.query(con, "CodeBuddy")
            self.assertGreaterEqual(result["count"], 1)
            self.assertEqual("agent", result["items"][0]["domain"])
        finally:
            con.close()


if __name__ == "__main__":
    unittest.main()
