"""L0：GET /api/meta 协议能力协商（只读端点契约）。

口径：用全新 `FastAPI()` 只挂 meta.router，**不 import src.main**（会起 vitals
后台循环）。本仓 L0 标准层用 unittest 收集器 ⇒ 一律 `unittest.TestCase` 方法，
不用 pytest.fixture / 模块级 test 函数（test_tier_collector_parity 会判红）。

contract-only（无生产中间件）：只证端点契约，不证生产准入。生产 read_gate 的
放行由主会话在 writeauth.py 处理，并由 tests/test_scope_enforce.py::DMetaPublicRead
挂**真实中间件**验证非回环匿名可达。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import meta  # noqa: E402

from fastapi import FastAPI  # noqa: E402
from fastapi.testclient import TestClient  # noqa: E402

# 给全新 app 一个可辨识的版本，证明 meta 的 version 取自 app.version
# （生产即单一版本源 VERSION），而不是 meta.py 里自造的字面量。
_L0_VERSION = "9.9.9-l0test"


class TestMetaContract(unittest.TestCase):
    def setUp(self):
        app = FastAPI(version=_L0_VERSION)
        app.include_router(meta.router)
        self.client = TestClient(app)
        self.client.__enter__()

    def tearDown(self):
        self.client.__exit__(None, None, None)

    def test_meta_returns_200(self):
        r = self.client.get("/api/meta")
        self.assertEqual(r.status_code, 200, r.text)

    def test_meta_fields_and_types(self):
        d = self.client.get("/api/meta").json()
        self.assertIsInstance(d["protocol"], int)
        self.assertIsInstance(d["capabilities"], list)
        self.assertTrue(d["capabilities"], "capabilities 不得为空")
        self.assertTrue(all(isinstance(x, str) and x for x in d["capabilities"]))
        self.assertIsInstance(d["version"], str)
        self.assertTrue(d["version"])
        # 主会话 2026-11-09 裁定：本机前端无独立版本号 ⇒ None=未启用（不回填假值）。
        # 字段须存在且可 JSON 序列化；将来真赋了值则须是非空字符串。
        self.assertIn("min_client", d)
        self.assertTrue(d["min_client"] is None or
                        (isinstance(d["min_client"], str) and d["min_client"]))

    def test_meta_version_tracks_app(self):
        d = self.client.get("/api/meta").json()
        self.assertEqual(d["version"], _L0_VERSION,
                         "version 必须取自 app.version，不得自造字面量")


if __name__ == "__main__":
    unittest.main(verbosity=2)
