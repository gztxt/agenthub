#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：技能调用第二数据源（会话转录采集）。

守的是「根因修复」而不是「文案修复」：
1. **四种转录格式都能认出真实调用**（codebuddy function_call / claude tool_use /
   pi toolCall / codex 同 message.content 分支）；
2. **只认读取，不认检索**：`grep SKILL.md`、`ls`、写文件都不许记成一次使用；
3. **幂等**：重复扫同一份文件不得重复记账；文件被截断重写也不得重复；
4. **增量**：追加一行只多记一条，不重扫全文件。

不打网络、不写生产库：`db.log_profile_event` 被替换成收集器，state 与转录全落 tmp。
"""
import json
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import skill_direct  # noqa: E402


class _Recorder:
    def __init__(self):
        self.events = []

    def __call__(self, source, subject, status, duration_ms, trace_id=None, detail=None):
        self.events.append({"source": source, "subject": subject, "detail": detail})


class SkillDirectScanTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="skill-direct-")
        self.roots = {k: os.path.join(self.tmp, k) for k in
                      ("codebuddy", "claude", "pi", "codex")}
        for p in self.roots.values():
            os.makedirs(p, exist_ok=True)
        self.state = os.path.join(self.tmp, "state.json")
        import db
        self.db = db
        self._orig = db.log_profile_event
        self.rec = _Recorder()
        db.log_profile_event = self.rec

    def tearDown(self):
        self.db.log_profile_event = self._orig
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _write(self, caller, name, lines):
        p = os.path.join(self.roots[caller], name)
        with open(p, "w", encoding="utf-8") as fh:
            for ln in lines:
                fh.write(json.dumps(ln, ensure_ascii=False) + "\n")
        return p

    def _scan(self, dry=False):
        return skill_direct.scan(roots=self.roots, state_path=self.state,
                                 days=3650, dry_run=dry)

    # ── 1. 四格式识别 ────────────────────────────────────────────────
    def test_four_formats(self):
        self._write("codebuddy", "cb.jsonl", [
            {"type": "function_call", "callId": "cb1", "name": "Read",
             "arguments": json.dumps({"file_path": "/x/skills/foo/SKILL.md"})},
            {"type": "function_call", "callId": "cb2", "name": "Bash",
             "arguments": json.dumps({"command": "cat /x/skills/bar/SKILL.md"})},
        ])
        self._write("claude", "cl.jsonl", [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "cl1", "name": "Skill",
                 "input": {"skill": "baz"}}]}},
        ])
        self._write("pi", "pi.jsonl", [
            {"type": "message", "message": {"content": [
                {"type": "toolCall", "id": "pi1", "name": "read",
                 "arguments": {"file_path": "/x/skills/qux/SKILL.md"}}]}},
        ])
        self._write("codex", "cx.jsonl", [
            {"type": "message", "message": {"content": [
                {"type": "tool_use", "id": "cx1", "name": "Read",
                 "input": {"file_path": "/x/skills/codex-skill/SKILL.md"}}]}},
        ])
        r = self._scan()
        names = sorted(e["detail"]["name"] for e in self.rec.events)
        self.assertEqual(names, ["bar", "baz", "codex-skill", "foo", "qux"])
        self.assertEqual(r["events"], 5)
        self.assertTrue(all(e["detail"]["via"] == "transcript" for e in self.rec.events))
        self.assertTrue(all(e["subject"] == "skill.invoke" for e in self.rec.events))
        self.assertTrue(all(e["source"] == "scan" for e in self.rec.events))

    # ── 2. 检索/写盘不算使用 ──────────────────────────────────────────
    def test_ignores_search_and_writes(self):
        self._write("codebuddy", "neg.jsonl", [
            {"type": "function_call", "callId": "n1", "name": "Bash",
             "arguments": json.dumps({"command": "grep -rn 'foo' /x/skills/foo/SKILL.md"})},
            {"type": "function_call", "callId": "n2", "name": "Bash",
             "arguments": json.dumps({"command": "ls /x/skills/foo/SKILL.md"})},
            {"type": "function_call", "callId": "n3", "name": "Write",
             "arguments": json.dumps({"file_path": "/x/skills/foo/SKILL.md", "content": "x"})},
            {"type": "function_call", "callId": "n4", "name": "Edit",
             "arguments": json.dumps({"file_path": "/x/skills/foo/SKILL.md"})},
        ])
        r = self._scan()
        self.assertEqual(r["events"], 0, self.rec.events)
        self.assertEqual(self.rec.events, [])

    # ── 3. 幂等：重复扫不重复记 ──────────────────────────────────────
    def test_idempotent(self):
        self._write("pi", "s.jsonl", [
            {"type": "message", "message": {"content": [
                {"type": "toolCall", "id": "a1", "name": "read",
                 "arguments": {"file_path": "/x/skills/foo/SKILL.md"}}]}}])
        self._scan()
        self.assertEqual(len(self.rec.events), 1)
        r2 = self._scan()
        self.assertEqual(r2["events"], 0)
        self.assertEqual(len(self.rec.events), 1)

    # ── 4. 增量：追加一条只多记一条 ─────────────────────────────────
    def test_incremental_append(self):
        p = self._write("pi", "inc.jsonl", [
            {"type": "message", "message": {"content": [
                {"type": "toolCall", "id": "i1", "name": "read",
                 "arguments": {"file_path": "/x/skills/a/SKILL.md"}}]}}])
        self._scan()
        with open(p, "a", encoding="utf-8") as fh:
            fh.write(json.dumps({"type": "message", "message": {"content": [
                {"type": "toolCall", "id": "i2", "name": "read",
                 "arguments": {"file_path": "/x/skills/b/SKILL.md"}}]}}) + "\n")
        r = self._scan()
        self.assertEqual(r["events"], 1)
        self.assertEqual([e["detail"]["name"] for e in self.rec.events], ["a", "b"])

    # ── 5. 文件被截断重写：去重键兜住 ───────────────────────────────
    def test_truncated_rewrite_no_dup(self):
        line = {"type": "message", "message": {"content": [
            {"type": "toolCall", "id": "t1", "name": "read",
             "arguments": {"file_path": "/x/skills/foo/SKILL.md"}}]}}
        p = self._write("pi", "tr.jsonl", [line])
        self._scan()
        # 同一条记录但文件整体重写（size 变化）
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(json.dumps(line) + "\n" + json.dumps({"type": "noise"}) + "\n")
        r = self._scan()
        self.assertEqual(r["events"], 0, "同 callId 不得重复记账")
        self.assertEqual(len(self.rec.events), 1)

    # ── 6. dry_run 不落库但仍解析 ───────────────────────────────────
    def test_dry_run(self):
        self._write("claude", "d.jsonl", [
            {"type": "assistant", "message": {"content": [
                {"type": "tool_use", "id": "d1", "name": "Skill",
                 "input": {"skill": "foo"}}]}}])
        r = self._scan(dry=True)
        self.assertEqual(r["events"], 1)
        self.assertEqual(self.rec.events, [])

    # ── 7. 坏 JSON 行不打断整文件 ───────────────────────────────────
    def test_bad_json_line(self):
        p = os.path.join(self.roots["pi"], "bad.jsonl")
        with open(p, "w", encoding="utf-8") as fh:
            fh.write("{not json\n")
            fh.write(json.dumps({"type": "message", "message": {"content": [
                {"type": "toolCall", "id": "b1", "name": "read",
                 "arguments": {"file_path": "/x/skills/ok/SKILL.md"}}]}}) + "\n")
        r = self._scan()
        self.assertEqual(r["errors"], 0)
        self.assertEqual(r["events"], 1)


if __name__ == "__main__":
    unittest.main(verbosity=2)
