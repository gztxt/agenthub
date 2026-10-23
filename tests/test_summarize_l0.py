#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：本地摘要提炼（`src/summarize.py`）。

不联网、不依赖 Ollama：`_post` 全部打桩。测的是**分块不丢内容、归并、缓存、失败语义**，
不是模型质量（模型质量由 `work/judge-quality-20261023/probe.py` 的活体探针负责）。
"""
import os
import pathlib
import sys
import tempfile
import unittest
from unittest import mock

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import summarize  # noqa: E402

_LONG = ("第一段：判定后端迁移要保证契约同构。" * 40 + "\n\n" +
         "第二段：超时余量必须按有负载实测。" * 40 + "\n\n" +
         "第三段：缓存只缓存成功，失败不固化。" * 40)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0sum-"))
        self._saved = os.environ.get("JUDGE_CACHE_PATH")
        os.environ["JUDGE_CACHE_PATH"] = str(self.tmp / "cache.json")
        self.addCleanup(self._restore)

    def _restore(self):
        if self._saved is None:
            os.environ.pop("JUDGE_CACHE_PATH", None)
        else:
            os.environ["JUDGE_CACHE_PATH"] = self._saved


class TestSplit(_Base):
    def test_chunks_cover_all_content(self):
        chunks = summarize._split(_LONG, 300)
        self.assertGreater(len(chunks), 1)
        joined = "".join(chunks).replace("\n", "").replace(" ", "")
        src = _LONG.replace("\n", "").replace(" ", "")
        self.assertEqual(joined, src, "分块丢了内容")

    def test_single_short_text_is_one_chunk(self):
        self.assertEqual(len(summarize._split("一句话。", 1600)), 1)


class TestBullets(_Base):
    def test_parses_dash_and_numbered(self):
        raw = "- 甲\n2. 乙\n• 丙\n```\n"
        self.assertEqual(summarize._bullets(raw, 3), ["甲", "乙", "丙"])

    def test_dedupes(self):
        self.assertEqual(summarize._bullets("- 同一条\n- 同一条。", 3), ["同一条"])


class TestSummarize(_Base):
    def _patch(self, responses):
        it = iter(responses)
        return mock.patch.object(summarize, "_post",
                                 side_effect=lambda *a, **k: {"response": next(it)})

    def test_single_chunk_and_cache(self):
        with self._patch(["- 要点一\n- 要点二\n- 要点三"]):
            out = summarize.summarize("很短的一段正文。", points=3)
        self.assertEqual(out["chunks"], 1)
        self.assertEqual(len(out["points"]), 3)
        self.assertFalse(out["cache"]["hit"])
        with mock.patch.object(summarize, "_post",
                               side_effect=AssertionError("命中缓存后不该再调模型")):
            again = summarize.summarize("很短的一段正文。", points=3)
        self.assertTrue(again["cache"]["hit"])
        self.assertEqual(again["points"], out["points"])

    def test_long_text_chunks_then_merges(self):
        calls = {"n": 0}

        def fake(prompt, timeout):
            calls["n"] += 1
            if "合并成最终" in prompt:
                return {"response": "- 归并一\n- 归并二\n- 归并三"}
            return {"response": "- 段内一\n- 段内二"}

        with mock.patch.object(summarize, "_post", side_effect=fake):
            out = summarize.summarize(_LONG, points=3)
        self.assertGreater(out["chunks"], 1)
        self.assertEqual(out["points"][0], "归并一")
        self.assertEqual(calls["n"], out["chunks"] + 1, "应为「每块一次 + 归并一次」")

    def test_ollama_failure_propagates(self):
        with mock.patch.object(summarize, "_post",
                               side_effect=summarize.SummarizeUnavailable("Ollama 不在")):
            with self.assertRaises(summarize.SummarizeUnavailable):
                summarize.summarize("正文", points=3)

    def test_empty_and_oversize_rejected(self):
        with self.assertRaises(ValueError):
            summarize.summarize("   ", points=3)
        with self.assertRaises(ValueError):
            summarize.summarize("字" * (summarize.MAX_INPUT + 1), points=3)

    def test_empty_model_output_is_failure_not_empty_list(self):
        with self._patch(["   "]):
            with self.assertRaises(summarize.SummarizeUnavailable):
                summarize.summarize("正文", points=3)


class TestEndpoint(_Base):
    """端点只做参数校验与错误映射：失败必须是 4xx/5xx，不是 200 + 空要点。"""

    def setUp(self):
        super().setUp()
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        import api_summarize
        app = FastAPI()
        app.include_router(api_summarize.router)
        self.client = TestClient(app)

    def test_ok_and_503_mapping(self):
        with mock.patch.object(summarize, "summarize",
                               return_value={"points": ["甲"], "chunks": 1}):
            r = self.client.post("/api/summarize", json={"text": "正文", "points": 2})
        self.assertEqual(r.status_code, 200)
        self.assertEqual(r.json()["points"], ["甲"])

        with mock.patch.object(summarize, "summarize",
                               side_effect=summarize.SummarizeUnavailable("Ollama 不在")):
            r = self.client.post("/api/summarize", json={"text": "正文", "points": 2})
        self.assertEqual(r.status_code, 503)
        self.assertIn("Ollama", r.json()["detail"])

    def test_validation_rejects_empty_text(self):
        r = self.client.post("/api/summarize", json={"text": "", "points": 2})
        self.assertEqual(r.status_code, 422)


if __name__ == "__main__":
    unittest.main()
