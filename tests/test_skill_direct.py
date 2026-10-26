#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：技能调用第二数据源（会话转录采集）。

守的是「根因修复」而不是「文案修复」：
1. **七种转录格式都能认出真实调用**（codebuddy function_call / claude tool_use /
   pi toolCall / codex 同 message.content 分支 / grok tool_calls / jcode 单文件 JSON /
   opencode SQLite part）；
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
                      ("codebuddy", "claude", "pi", "codex", "grok")}
        for p in self.roots.values():
            os.makedirs(p, exist_ok=True)
        self.json_roots = {"jcode": os.path.join(self.tmp, "jcode")}
        os.makedirs(self.json_roots["jcode"], exist_ok=True)
        self.sqlite_db = os.path.join(self.tmp, "opencode.db")
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
        return skill_direct.scan(
            roots=self.roots, json_roots=self.json_roots,
            sqlite_sources={"opencode": self.sqlite_db} if os.path.isfile(self.sqlite_db) else {},
            state_path=self.state, days=3650, dry_run=dry)

    def _opencode(self, rows):
        """造一个最小 opencode 库：真实 schema 里 part 只有 rowid/data 是关键。"""
        import sqlite3
        con = sqlite3.connect(self.sqlite_db)
        try:
            con.execute("CREATE TABLE IF NOT EXISTS part("
                        "rowid INTEGER PRIMARY KEY, id TEXT, message_id TEXT, "
                        "session_id TEXT, time_created INTEGER, time_updated INTEGER, data TEXT)")
            for rowid, data in rows:
                con.execute("INSERT INTO part(rowid,id,message_id,session_id,"
                            "time_created,time_updated,data) VALUES(?,?,?,?,?,?,?)",
                            (rowid, "p%d" % rowid, "m1", "s1", 1790180884000,
                             1790180884000, json.dumps(data, ensure_ascii=False)))
            con.commit()
        finally:
            con.close()

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

    # ── 7. 半行（agent 正在写）不吞事件 ─────────────────────────────
    def test_partial_last_line_is_retried(self):
        """活跃会话的最后一行可能是半截 JSON。不能因为解析失败就把 offset 推过去——
        推过去这条调用就永久丢了。"""
        p = os.path.join(self.roots["pi"], "half.jsonl")
        full = json.dumps({"type": "message", "message": {"content": [
            {"type": "toolCall", "id": "h1", "name": "read",
             "arguments": {"file_path": "/x/skills/half/SKILL.md"}}]}})
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(full[:20])          # 没有换行 = 半行
        self.assertEqual(self._scan()["events"], 0)
        with open(p, "w", encoding="utf-8") as fh:
            fh.write(full + "\n")       # 写完整
        self.assertEqual(self._scan()["events"], 1)
        self.assertEqual([e["detail"]["name"] for e in self.rec.events], ["half"])

    # ── 8. 坏 JSON 行不打断整文件 ───────────────────────────────────
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

    # ── 9. grok：assistant.tool_calls + cd 后读裸 SKILL.md ───────────
    def test_grok_tool_calls(self):
        self._write("grok", "chat_history.jsonl", [
            {"type": "assistant", "content": "", "tool_calls": [
                {"id": "g1", "name": "read_file",
                 "arguments": json.dumps({"path": "/x/skills/grokfoo/SKILL.md"})}]},
            # 实测写法：先 cd 进技能目录，再读裸文件名（路径不连续）
            {"type": "assistant", "content": "", "tool_calls": [
                {"id": "g2", "name": "run_terminal_command",
                 "arguments": json.dumps(
                     {"command": "cd /home/x/.agents/skills/grokcd 2>/dev/null && "
                                 "head -30 SKILL.md"})}]},
        ])
        r = self._scan()
        self.assertEqual(r["events"], 2, self.rec.events)
        self.assertEqual(sorted(e["detail"]["name"] for e in self.rec.events),
                         ["grokcd", "grokfoo"])
        ev = {e["detail"]["name"]: e["detail"]["evidence"] for e in self.rec.events}
        self.assertEqual(ev["grokfoo"], "read")
        self.assertEqual(ev["grokcd"], "bash")

    def test_grok_only_scans_chat_history(self):
        """同目录的 events.jsonl 会带同一次调用 ⇒ 只扫 chat_history，否则记两遍。"""
        call = {"type": "assistant", "content": "", "tool_calls": [
            {"id": "same", "name": "read_file",
             "arguments": json.dumps({"path": "/x/skills/dup/SKILL.md"})}]}
        self._write("grok", "chat_history.jsonl", [call])
        self._write("grok", "events.jsonl", [call])
        r = self._scan()
        self.assertEqual(r["events"], 1, "grok events.jsonl 不该被当成第二份证据")

    def test_grok_grep_is_not_a_read(self):
        self._write("grok", "chat_history.jsonl", [
            {"type": "assistant", "content": "", "tool_calls": [
                {"id": "g3", "name": "run_terminal_command",
                 "arguments": json.dumps(
                     {"command": "cd /x/skills/searchonly && grep -n foo SKILL.md"})}]},
        ])
        self.assertEqual(self._scan()["events"], 0, self.rec.events)

    # ── 10. jcode：单文件 JSON，消息数组 ─────────────────────────────
    def test_jcode_json_messages(self):
        p = os.path.join(self.json_roots["jcode"], "session_x.json")
        with open(p, "w", encoding="utf-8") as fh:
            json.dump({"id": "x", "messages": [
                {"role": "user", "content": [{"type": "text", "text": "hi"}]},
                {"role": "assistant", "content": [
                    {"type": "text", "text": "ok"},
                    {"type": "tool_use", "id": "j1", "name": "read",
                     "input": {"file_path": "/x/skills/jcodefoo/SKILL.md"}}]},
            ]}, fh)
        r = self._scan()
        self.assertEqual(r["events"], 1, self.rec.events)
        self.assertEqual(self.rec.events[0]["detail"]["name"], "jcodefoo")
        # 幂等：文件没变再扫 0 条
        self.assertEqual(self._scan()["events"], 0)
        self.assertEqual(len(self.rec.events), 1)

    def test_jcode_last_message_completion_is_seen(self):
        """流式写入会改写末条消息（先 text，后补 tool_use）⇒ 必须重读末条。"""
        p = os.path.join(self.json_roots["jcode"], "session_y.json")
        base = {"id": "y", "messages": [
            {"role": "assistant", "content": [{"type": "text", "text": "看"}]}]}
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(base, fh)
        self.assertEqual(self._scan()["events"], 0)
        base["messages"][0]["content"].append(
            {"type": "tool_use", "id": "j2", "name": "read",
             "input": {"file_path": "/x/skills/late/SKILL.md"}})
        with open(p, "w", encoding="utf-8") as fh:
            json.dump(base, fh)
        self.assertEqual(self._scan()["events"], 1, self.rec.events)
        self.assertEqual(self.rec.events[0]["detail"]["name"], "late")

    # ── 11. opencode：SQLite part 表 ───────────────────────────────
    def test_opencode_sqlite_parts(self):
        self._opencode([
            (1, {"type": "tool", "tool": "read",
                 "callID": "oc1",
                 "state": {"input": {"filePath": "/x/skills/ocfoo/SKILL.md"}}}),
            (2, {"type": "tool", "tool": "bash",
                 "callID": "oc2",
                 "state": {"input": {"command": "grep -rn x /x/skills/neg/SKILL.md"}}}),
            (3, {"type": "text", "text": "SKILL.md 只是提到，不是工具调用"}),
        ])
        r = self._scan()
        self.assertEqual(r["events"], 1, self.rec.events)
        self.assertEqual(self.rec.events[0]["detail"]["name"], "ocfoo")
        self.assertEqual(self.rec.events[0]["detail"]["evidence"], "read")
        # rowid 水位：再扫不重复
        self.assertEqual(self._scan()["events"], 0)

    def test_opencode_new_rows_only(self):
        self._opencode([(1, {"type": "tool", "tool": "read", "callID": "a",
                             "state": {"input": {"filePath": "/x/skills/a/SKILL.md"}}})])
        self._scan()
        self._opencode([(2, {"type": "tool", "tool": "read", "callID": "b",
                             "state": {"input": {"filePath": "/x/skills/b/SKILL.md"}}})])
        r = self._scan()
        self.assertEqual(r["events"], 1)
        self.assertEqual([e["detail"]["name"] for e in self.rec.events], ["a", "b"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
