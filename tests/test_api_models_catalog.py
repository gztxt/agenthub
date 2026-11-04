"""模型清单兜底的合并语义（2026-11-04）。

背景：CCR 启动时 CDN 刷新 openrouter 失败会让 /v1/models 只剩静态配置的一小部分，
于是加了 config.sqlite 兜底。首版实现是**整份替换**，而 SQLite 里没有网关动态
配置的 `商汤/*`（实测 7 条）⇒ 兜底一触发，这些模型就从设置页下拉里消失，
用户已设的 `商汤/...` 反被判「不在清单里」。这里钉死：兜底必须**取并集**。
"""
import json
import pathlib
import sys
import unittest
from unittest import mock

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "src"))

import api_models  # noqa: E402
import modelcfg  # noqa: E402


def _resp(ids):
    r = mock.MagicMock()
    r.read.return_value = json.dumps({"data": [{"id": i} for i in ids]}).encode()
    r.__enter__.return_value = r
    return r


class MergeCatalog(unittest.TestCase):
    """_merge_catalog：网关独有 ID 必须保留，SQLite 独有 ID 必须补进来。"""

    def test_union_keeps_gateway_only_and_adds_sqlite_only(self):
        primary = {"models": [{"id": "商汤/x", "name": "x", "owner": "商汤"}],
                   "groups": {"商汤": ["商汤/x"]}, "count": 1, "source": "ccr"}
        fallback = {"models": [{"id": "alibaba/y", "name": "y", "owner": "alibaba"}],
                    "groups": {"alibaba": ["alibaba/y"]}, "count": 1}
        m = api_models._merge_catalog(primary, fallback)
        self.assertEqual(m["count"], 2)
        self.assertEqual([x["id"] for x in m["models"]], ["商汤/x", "alibaba/y"])
        self.assertEqual(m["groups"]["商汤"], ["商汤/x"])
        self.assertEqual(m["groups"]["alibaba"], ["alibaba/y"])

    def test_no_duplicate_when_both_sides_have_same_id(self):
        e = {"id": "alibaba/y", "name": "y", "owner": "alibaba"}
        primary = {"models": [e], "groups": {"alibaba": ["alibaba/y"]}, "count": 1}
        m = api_models._merge_catalog(primary, {"models": [dict(e)], "count": 1})
        self.assertEqual(m["count"], 1, "同 id 只留一条，不许重复")


class FetchCatalogUnion(unittest.TestCase):
    """modelcfg._fetch_catalog_ids 的兜底分支：并集 + 空网关也兜底。"""

    def test_incomplete_gateway_merges_sqlite_instead_of_replacing(self):
        sqlite = {"alibaba/a", "alibaba/b", "alibaba/c", "alibaba/d"}
        with mock.patch.object(modelcfg, "_fetch_catalog_from_sqlite",
                               return_value=(sqlite, "")), \
             mock.patch("urllib.request.urlopen", return_value=_resp(["商汤/x"])):
            ids, names, contexts, err = modelcfg._fetch_catalog_ids()
        self.assertIn("商汤/x", ids, "网关独有的 ID 不许被兜底挤掉")
        self.assertTrue(sqlite.issubset(ids), "SQLite 条目必须补齐")
        self.assertIn("merged sqlite fallback", err)

    def test_empty_gateway_falls_back_to_sqlite(self):
        with mock.patch.object(modelcfg, "_fetch_catalog_from_sqlite",
                               return_value=({"alibaba/a"}, "")), \
             mock.patch("urllib.request.urlopen", return_value=_resp([])):
            ids, names, contexts, err = modelcfg._fetch_catalog_ids()
        self.assertEqual(ids, {"alibaba/a"})
        self.assertIn("sqlite fallback", err)

    def test_complete_gateway_returns_api_ids_untouched(self):
        sqlite = {"alibaba/a", "alibaba/b"}
        with mock.patch.object(modelcfg, "_fetch_catalog_from_sqlite",
                               return_value=(sqlite, "")), \
             mock.patch("urllib.request.urlopen",
                        return_value=_resp(["alibaba/a", "alibaba/b", "商汤/x"])):
            ids, names, contexts, err = modelcfg._fetch_catalog_ids()
        self.assertEqual(err, "")
        self.assertIn("商汤/x", ids)


class CatalogContexts(unittest.TestCase):
    """`_catalog_contexts`：容量真值只从 CCR 自己的元数据取，取不到就不给。

    背景（2026-11-04 用户报障）：dsh 里 deepseek-v4.1-flash 只有 128k，官方 1M。
    根因不是 dsh（它默认就是 1M），也不是 CCR（元数据写着 1000000），而是 hub 的
    sync 给每条目录写死了 131072。这里钉死取值优先级与"宁缺不猜"。
    """

    def setUp(self):
        import tempfile
        self._tmp = tempfile.TemporaryDirectory()
        root = pathlib.Path(self._tmp.name)
        self.db = root / "config.sqlite"
        self.cache = root / "models.json"
        import sqlite3
        conn = sqlite3.connect(self.db)
        conn.execute("CREATE TABLE app_config (key TEXT PRIMARY KEY, value_json TEXT)")
        cfg = {"Providers": [{
            "id": "provider-alibaba-b600d5a733", "name": "alibaba",
            "modelMetadata": {"deepseek-v4.1-flash": {"contextWindow": 1000000},
                              "qwen3.8-max": {"contextWindow": 983616}},
        }, {
            # provider id 没有可解析的 vendor 名 ⇒ 必须用 name 当前缀（`商汤/x`）
            "id": "provider", "name": "商汤", "modelMetadata": None,
        }]}
        conn.execute("INSERT INTO app_config VALUES ('default', ?)", (json.dumps(cfg),))
        conn.commit()
        conn.close()
        self.cache.write_text(json.dumps({"models": [
            {"id": "alibaba/deepseek-v4.1-flash", "limits": {"contextTokens": 111}},
            {"id": "alibaba/deepseek-v4-pro-0813", "limits": {"contextTokens": 1000000}},
            {"id": "商汤/none", "limits": {}},
        ]}), encoding="utf-8")
        self._old = (modelcfg._CCR_CONFIG_DB, modelcfg._CCR_MODELS_CACHE,
                     dict(modelcfg._CTX_CACHE))
        modelcfg._CCR_CONFIG_DB = self.db
        modelcfg._CCR_MODELS_CACHE = self.cache
        modelcfg._CTX_CACHE.update({"key": None, "map": {}})

    def tearDown(self):
        (modelcfg._CCR_CONFIG_DB, modelcfg._CCR_MODELS_CACHE, _old) = self._old
        modelcfg._CTX_CACHE.update(_old)
        self._tmp.cleanup()

    def test_pinned_metadata_wins_over_catalog_cache(self):
        ctx = modelcfg._catalog_contexts()
        self.assertEqual(ctx["alibaba/deepseek-v4.1-flash"], 1000000,
                         "显式钉住的元数据优先于目录缓存")
        self.assertEqual(ctx["alibaba/qwen3.8-max"], 983616)

    def test_catalog_cache_fills_the_gap(self):
        ctx = modelcfg._catalog_contexts()
        self.assertEqual(ctx["alibaba/deepseek-v4-pro-0813"], 1000000)

    def test_unknown_id_is_absent_not_invented(self):
        ctx = modelcfg._catalog_contexts()
        self.assertNotIn("商汤/none", ctx, "两处都没有 ⇒ 不返回，调用方不要猜")


if __name__ == "__main__":
    unittest.main()
