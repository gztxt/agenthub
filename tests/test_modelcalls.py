# -*- coding: utf-8 -*-
"""modelcalls 模型调用计量的 L0 测试（空 HOME、不连网，同 users/approvals 形态）。

钉住四条（写错任一 = 计量面失真或带倒真实 LLM 调用）：
  1. record 正常落库（成功 + 失败两态，token 缺失补 0 不丢行）；
  2. 未知场景落 "other"（不静默丢弃，与 asset_audit append-only 同纪律）；
  3. overview 聚合正确（按模型/场景分桶、total 求和）；
  4. db 未初始化时 record/ensure_schema 静默跳过（**绝不带倒调用方**——计量是附加价值，
     不是准入条件，同 writeauth.actor_of 的「取不到不抛」口径）。
"""
import os
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "src"))
os.environ["HOME"] = tempfile.mkdtemp()

import db  # noqa: E402
import modelcalls  # noqa: E402


def _fresh_db():
    fd, p = tempfile.mkstemp(suffix=".db")
    os.close(fd)
    db.init_db(Path(p))
    modelcalls.ensure_schema()


class ModelCallsL0(unittest.TestCase):

    def test_01_record_success_with_tokens(self):
        _fresh_db()
        modelcalls.record("alibaba/qwen3.8-flash", {"input_tokens": 100, "output_tokens": 50},
                          scene="manager", duration_ms=844, request_id="r1")
        rows = db.query("SELECT model, provider, scene, status, input_tokens, output_tokens,"
                        " total_tokens, duration_ms FROM model_call_logs")
        self.assertEqual(len(rows), 1)
        r = rows[0]
        self.assertEqual(r["model"], "alibaba/qwen3.8-flash")
        self.assertEqual(r["provider"], "alibaba")
        self.assertEqual(r["scene"], "manager")
        self.assertEqual(r["status"], "success")
        self.assertEqual(r["input_tokens"], 100)
        self.assertEqual(r["output_tokens"], 50)
        self.assertEqual(r["total_tokens"], 150)
        self.assertEqual(r["duration_ms"], 844)

    def test_02_record_error_keeps_row_zero_tokens(self):
        _fresh_db()
        modelcalls.record("openrouter/free", {}, scene="memory",
                          status="error", error="429 rate limit")
        rows = db.query("SELECT status, total_tokens, error FROM model_call_logs")
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["status"], "error")
        self.assertEqual(rows[0]["total_tokens"], 0)
        self.assertEqual(rows[0]["error"], "429 rate limit")

    def test_03_unknown_scene_falls_to_other(self):
        _fresh_db()
        modelcalls.record("x/y", {}, scene="bogus-scene")
        rows = db.query("SELECT scene FROM model_call_logs")
        self.assertEqual(rows[0]["scene"], "other")

    def test_04_overview_aggregates_by_model_and_scene(self):
        _fresh_db()
        modelcalls.record("a/m", {"input_tokens": 10, "output_tokens": 5}, scene="kb")
        modelcalls.record("a/m", {"input_tokens": 20, "output_tokens": 10}, scene="kb")
        modelcalls.record("b/n", {}, scene="memory", status="error")
        ov = modelcalls.overview(7)
        self.assertEqual(ov["total_calls"], 3)
        self.assertEqual(ov["total_tokens"], 45)
        self.assertEqual(ov["by_scene"], {"kb": 2, "memory": 1})
        m_a = next(m for m in ov["by_model"] if m["model"] == "a/m")
        self.assertEqual(m_a["calls"], 2)
        self.assertEqual(m_a["total_tokens"], 45)

    def test_05_record_noop_when_db_closed(self):
        # 连接未 init ⇒ record/ensure_schema 静默跳过，绝不抛（计量不打断调用方）
        global _conn  # noqa
        import db as _dbmod
        saved = _dbmod._conn
        _dbmod._conn = None
        try:
            modelcalls.ensure_schema()   # 不应抛
            modelcalls.record("x/y", {})  # 不应抛
            modelcalls.overview(7)        # 空数据合法返回，不应抛
        finally:
            _dbmod._conn = saved

    def test_06_provider_split(self):
        self.assertEqual(modelcalls._split_provider("alibaba/qwen3.8"), "alibaba")
        self.assertEqual(modelcalls._split_provider("nvidia/nemotron"), "nvidia")
        self.assertEqual(modelcalls._split_provider("no-slash-model"), "")
        self.assertEqual(modelcalls._split_provider(""), "")

    def test_07_overview_empty_db_returns_zero(self):
        _fresh_db()
        ov = modelcalls.overview(7)
        self.assertEqual(ov["total_calls"], 0)
        self.assertEqual(ov["total_tokens"], 0)
        self.assertEqual(ov["by_model"], [])

    def test_08_build_router_returns_two_read_endpoints(self):
        _fresh_db()
        r = modelcalls.build_router()
        paths = {getattr(x, "path", "") for x in r.routes}
        self.assertIn("/api/monitor/model-calls/overview", paths)
        self.assertIn("/api/monitor/model-calls", paths)


if __name__ == "__main__":
    unittest.main()
