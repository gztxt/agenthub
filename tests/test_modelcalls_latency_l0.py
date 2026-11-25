#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：`_create_message` 未带 request_id 时也必须留下延迟与关联 id。

【这条判据治什么】
生产 `data/agents.db.model_call_logs` 23 行 duration_ms/request_id **全 NULL**。
链路：调用方 request_id=None → `_llm_meter.start(None)` 因 `if request_id:` 不埋点
→ `record_success()` 的 t0=None ⇒ ms=None → 落库两列皆空 → `usage_ledger.avg_ms` 恒 0。
本测试钉住「缺省 request_id 时由计量层自造一个」，让延迟列不再恒空。

零真网络：替身 `_post_once` + 不真睡的熔断器；临时 DB，不碰生产账本。
"""
import asyncio
import os
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db  # noqa: E402
import llm  # noqa: E402
import modelcalls  # noqa: E402
import resilience  # noqa: E402


class TestCreateMessageLatency(unittest.TestCase):
    def setUp(self):
        self._saved_inst = resilience.instance
        self._saved_post = llm._post_once
        self._saved_mcalls = os.environ.get("HUB_MODEL_CALLS")
        os.environ["HUB_MODEL_CALLS"] = "1"
        fd, p = tempfile.mkstemp(suffix=".db")
        os.close(fd)
        db.init_db(pathlib.Path(p))
        modelcalls.ensure_schema()
        self._db_path = p

        async def post(body, request_id):
            return {"content": [{"type": "text", "text": "hi"}],
                    "usage": {"input_tokens": 1, "output_tokens": 2}}
        llm._post_once = post
        resilience.instance = resilience.CircuitBreakers(
            clock=lambda: 0.0, sleep=lambda s: None, rand=lambda: 0.0)
        self.addCleanup(self._restore)

    def _restore(self):
        resilience.instance = self._saved_inst
        llm._post_once = self._saved_post
        if self._saved_mcalls is None:
            os.environ.pop("HUB_MODEL_CALLS", None)
        else:
            os.environ["HUB_MODEL_CALLS"] = self._saved_mcalls
        try:
            os.unlink(self._db_path)
        except OSError:
            pass

    def test_missing_request_id_still_records_non_null_latency_and_id(self):
        # 调用方不传 request_id（这正是生产 23 行的实际形态）
        asyncio.run(llm._create_message(None, [{"role": "user", "content": "x"}]))
        rows = db.query("SELECT request_id, duration_ms FROM model_call_logs")
        self.assertEqual(1, len(rows))
        rid, ms = rows[0]["request_id"], rows[0]["duration_ms"]
        self.assertTrue(rid, "request_id 不得为 NULL/空——否则无法把明细关联到调用")
        self.assertIsNotNone(ms, "duration_ms 不得为 NULL——否则 avg_ms 恒 0")
        self.assertGreaterEqual(ms, 0)


if __name__ == "__main__":
    unittest.main()
