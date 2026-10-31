"""`hub_skill_inject.py`（UserPromptSubmit hook）的纯函数闸门（2026-10-31）。

背景（用户裁定「尽量自动调用技能，提高效率、减少消耗」）：
- 光给技能名，agent 还要再发一轮工具调用去 Read 正文 ⇒ 那一轮本身就是消耗；
- 但**每轮都塞 6KB 正文**是把「省一轮」换成「每轮多 6KB」，净亏。
⇒ 两条判据同时成立才合格：**命中够强才内联** + **同会话同一技能只内联一次**。

本文件只跑纯函数（不联网）：hook 一旦拖慢或报错，会拦住用户输入。
"""

import importlib.util
import os
import pathlib
import sys
import tempfile
import unittest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SPEC = importlib.util.spec_from_file_location("inj", ROOT / "scripts" / "hub_skill_inject.py")
inj = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(inj)


class TestSessionKey(unittest.TestCase):
    def test_prefers_session_id(self):
        self.assertEqual(inj.session_key('{"session_id":"abc","transcript_path":"/x"}'), "abc")

    def test_falls_back_to_transcript(self):
        self.assertEqual(inj.session_key('{"transcript_path":"/x/y"}'), "/x/y")

    def test_bad_json_does_not_raise(self):
        self.assertTrue(inj.session_key("{not json").startswith("ppid-"))

    def test_empty(self):
        self.assertTrue(inj.session_key("").startswith("ppid-"))


class TestInlineDedupe(unittest.TestCase):
    def setUp(self):
        self._old_dir = inj.CACHE_DIR
        self._tmp = tempfile.mkdtemp()
        inj.CACHE_DIR = self._tmp

    def tearDown(self):
        inj.CACHE_DIR = self._old_dir

    def test_mark_then_seen(self):
        self.assertFalse(inj.already_inlined("s1", "skill-a"))
        inj.mark_inlined("s1", "skill-a")
        self.assertTrue(inj.already_inlined("s1", "skill-a"))

    def test_per_session_and_per_skill(self):
        inj.mark_inlined("s1", "skill-a")
        self.assertFalse(inj.already_inlined("s2", "skill-a"), "换会话应重新内联")
        self.assertFalse(inj.already_inlined("s1", "skill-b"), "换技能应重新内联")

    def test_ttl_expiry(self):
        inj.mark_inlined("s1", "skill-a")
        path = pathlib.Path(self._tmp) / os.listdir(self._tmp)[0]
        os.utime(path, (0, 0))
        self.assertFalse(inj.already_inlined("s1", "skill-a"), "过期后应允许再内联")


class TestInlineGate(unittest.TestCase):
    def test_low_score_never_fetches(self):
        old = inj.INLINE_MIN
        inj.INLINE_MIN = 20
        try:
            self.assertEqual(inj.inline_body({"name": "x", "bm25": 3.0}, 500), "",
                             "低分不得内联（噪声正文塞进上下文比不给更糟）")
        finally:
            inj.INLINE_MIN = old

    def test_off_switch(self):
        old = inj.INLINE_BODY
        inj.INLINE_BODY = False
        try:
            self.assertEqual(inj.inline_body({"name": "x", "bm25": 99}, 500), "")
        finally:
            inj.INLINE_BODY = old


class TestFormatKeepsNamesOnly(unittest.TestCase):
    def test_top3_rendered(self):
        payload = {"bm25": {"items": [
            {"name": "a", "description": "第一句。第二句", "matched": ["x", "y"]},
            {"name": "b", "description": "b 用途"}, {"name": "c", "description": "c 用途"},
            {"name": "d", "description": "d 用途"}]}}
        out = inj.format_skills(payload)
        self.assertIn("· a", out)
        self.assertNotIn("· d", out, "只取 top-3")
        self.assertIn("命中：x y", out)

    def test_malformed_payload_is_empty(self):
        for bad in ({}, {"bm25": None}, {"bm25": {"items": "x"}}):
            self.assertEqual(inj.format_skills(bad), "")
