#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：P2 A10 —— 统一 Action Plane（请求 / 裁决 / 结果 + 生命周期）。

守六个「看着像在做事、其实什么都没保证」的点：

1. **pending ≠ approved**：「已登记」不是「已准许」。合并这两态就会出现
   「记了一行 = 放行了」的读法 —— 那正是本仓反复禁的「写入成功即完成」。
2. **deny 必须带理由**：无理由的拒绝无法申诉，也看不出是规则写错还是真拦对。
3. **unknown ≠ failed**：执行抛异常且拿不到结果 ⇒ 行动「不知道做没做成」。
   记成 failed 会让调用方放心重试，而重试有副作用的行动是最危险的动作。
4. **结论不可覆盖**：同一 action 只能 settle 一次。「第二次 settle 把 failed 改成
   success」就是「执行者自证」的老毛病；改判只能走 unknown→done 的显式复查。
5. **ACL 不搬进来**：本模块只**接收**裁决并落账，不自己算 ACL ——
   两份真相必然漂移（mcpgw 的 docstring 已写明）。故闸门断言 `actionplane`
   **零 import mcpgw**。
6. **参数只留形状**：params 落库只存 `{键: 类型}`，不吃值 —— 否则凭据会被复制进事件表。

分层口径（tests/tiers.py）：临时 db + 纯函数，不起服务、不连网、零 SKIP。
"""
import ast
import asyncio
import pathlib
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import db                # noqa: E402
import actionplane as ap  # noqa: E402


class _DbCase(unittest.TestCase):
    """每个用例一个独立 tmp 库（db 是进程级全局连接 ⇒ 用例内显式 init 一次）。"""

    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp())
        db.init_db(self.tmp / "agents.db")
        ap.ensure_schema()


class TestPureJudgement(unittest.TestCase):
    def test_kinds_are_closed(self):
        self.assertIsNone(ap.validate_request("mcp", "claude", "s1", "read"))
        for bad in ("", "MCP!", "shell", "mcp tool"):
            self.assertIsNotNone(ap.validate_request(bad, "claude", "s1", "read"), bad)

    def test_actor_target_op_required(self):
        for actor, target, op, kw in (("", "s", "o", "actor"), ("a", "", "o", "target"),
                                      ("a", "s", "", "op"), ("a", "  ", "o", "target")):
            why = ap.validate_request("mcp", actor, target, op)
            self.assertIsNotNone(why, kw)
            self.assertIn(kw, why, "拒绝理由必须点名是哪个字段：%r" % why)

    def test_action_key_is_deterministic_and_ignores_params(self):
        a = ap.action_key("mcp", "claude", "s1", "read")
        self.assertEqual(a, ap.action_key("mcp", "claude", "s1", "read"))
        self.assertNotEqual(a, ap.action_key("mcp", "codex", "s1", "read"))
        self.assertEqual(len(a), 16)

    def test_transitions_are_explicit(self):
        self.assertIsNone(ap.transition_reason("pending", "approved"))
        self.assertIsNone(ap.transition_reason("pending", "denied"))
        self.assertIsNone(ap.transition_reason("approved", "done"))
        self.assertIsNone(ap.transition_reason("approved", "unknown"))
        self.assertIsNone(ap.transition_reason("unknown", "done"))
        for frm, to in (("pending", "done"), ("denied", "approved"), ("done", "done"),
                        ("done", "approved")):
            why = ap.transition_reason(frm, to)
            self.assertIsNotNone(why, "%s→%s 必须拒" % (frm, to))
            self.assertEqual(ap.transition_reason(frm, to), why, "理由必须稳定")

    def test_deny_requires_reason(self):
        self.assertIsNone(ap.validate_decision("allow", None))
        self.assertIsNone(ap.validate_decision("ask", None))
        why = ap.validate_decision("deny", "")
        self.assertIsNotNone(why)
        self.assertIn("reason", why, "拒绝必须连理由一起断言（否则读不出被拒的原因）")
        self.assertIsNone(ap.validate_decision("deny", "guest 全拒"))

    def test_outcome_unknown_is_not_failed(self):
        self.assertEqual(ap.outcome_of(True), "success")
        self.assertEqual(ap.outcome_of(False), "failed")
        self.assertEqual(ap.outcome_of(None), "unknown")
        self.assertEqual(ap.outcome_of(None, raised=True, got_result=False), "unknown")
        # 抛了但拿回了「没成功」这个结论 ⇒ failed，不是 unknown
        self.assertEqual(ap.outcome_of(False, raised=True, got_result=True), "failed")


class TestStorage(_DbCase):
    def test_submit_does_not_approve(self):
        rec = ap.submit("mcp", "claude", "s1", "read")
        self.assertEqual(rec["status"], "pending", "登记不得等于放行")
        self.assertFalse(ap.history(rec["id"])["settled"])

    def test_params_store_shape_only(self):
        rec = ap.submit("llm", "claude", "ccr", "chat",
                        params={"api_key": "sk-SECRET-123", "n": 3})
        import json
        p = json.loads(rec["params"])
        self.assertEqual(p, {"api_key": "str", "n": "int"})
        self.assertNotIn("SECRET", rec["params"], "参数值不得落库（凭据会被复制进事件表）")

    def test_decide_allow_then_settle(self):
        rec = ap.submit("mcp", "claude", "s1", "read")
        ap.decide(rec["id"], "allow", reason="规则 * 放行")
        self.assertEqual(ap.get(rec["id"])["status"], "approved")
        ap.settle(rec["id"], "success", ok=True, duration_ms=12)
        h = ap.history(rec["id"])
        self.assertEqual(h["request"]["status"], "done")
        self.assertTrue(h["settled"])
        self.assertEqual(h["result"]["outcome"], "success")
        self.assertEqual(len(h["decisions"]), 1)

    def test_deny_path_is_terminal(self):
        rec = ap.submit("term", "claude", "term-1", "exec")
        ap.decide(rec["id"], "deny", reason="未授权的终端命令")
        self.assertEqual(ap.get(rec["id"])["status"], "denied")
        with self.assertRaises(ValueError):
            ap.settle(rec["id"], "success", ok=True)

    def test_settle_twice_is_refused(self):
        rec = ap.submit("mcp", "claude", "s1", "write")
        ap.decide(rec["id"], "allow", reason="ok")
        ap.settle(rec["id"], "failed", ok=False, error="boom")
        with self.assertRaises(ValueError) as cm:
            ap.settle(rec["id"], "success", ok=True)
        self.assertIn("已 settle", str(cm.exception),
                      "拒绝必须连理由一起断言：笼统 ValueError 会被别的分支兜住而假绿")
        self.assertEqual(ap.history(rec["id"])["result"]["outcome"], "failed",
                         "第二次 settle 不得改掉已有结论")

    def test_shadow_only_on_allow(self):
        rec = ap.submit("mcp", "claude", "s1", "delete")
        with self.assertRaises(ValueError) as cm:
            ap.decide(rec["id"], "deny", reason="x", shadow_would_deny=True)
        self.assertIn("shadow_would_deny", str(cm.exception))

    def test_shadow_annotation_lands_but_does_not_change_state(self):
        rec = ap.submit("mcp", "claude", "s1", "delete")
        ap.decide(rec["id"], "allow", reason="enforce 全放行",
                  shadow_would_deny=True, shadow_rules=[7])
        h = ap.history(rec["id"])
        self.assertEqual(h["request"]["status"], "approved")
        self.assertEqual(h["decisions"][0]["shadow_would_deny"], 1)
        self.assertEqual(h["decisions"][0]["shadow_rules"], "[7]")

    def test_ask_waits_then_can_be_decided(self):
        rec = ap.submit("term", "claude", "term-2", "rm")
        ap.decide(rec["id"], "ask", reason="需人工批准")
        self.assertEqual(ap.get(rec["id"])["status"], "waiting_approval")
        ap.decide(rec["id"], "allow", reason="用户批准")
        self.assertEqual(ap.get(rec["id"])["status"], "approved")
        self.assertEqual(len(ap.history(rec["id"])["decisions"]), 2)

    def test_run_denied_does_not_call_executor(self):
        called = []

        def boom():
            called.append(1)
            return True, {}

        h = asyncio.run(ap.run(boom, kind="mcp", actor="claude", target="s1", op="write",
                               decision={"allowed": False, "reason": "ACL 拒绝"}))
        self.assertEqual(called, [], "被拒的行动绝不许被执行")
        self.assertEqual(h["request"]["status"], "denied")
        self.assertIsNone(h["result"], "拒绝不是执行结果：results 表里不得有行"
                                       "（否则「没执行」与「执行了但被拒」长得一样）")

    def test_run_success_and_exception_becomes_unknown(self):
        h = asyncio.run(ap.run(lambda: (True, {"n": 1}), kind="mcp", actor="a",
                               target="t", op="o"))
        self.assertEqual(h["result"]["outcome"], "success")
        self.assertEqual(h["decisions"][0]["reason"], "no-pdp：调用方未提供策略裁决",
                         "无策略层必须写明，否则事后分不清「策略放行」与「没有策略」")

        async def bad():
            raise RuntimeError("连接被重置")

        h2 = asyncio.run(ap.run(bad, kind="http", actor="a", target="t", op="o"))
        self.assertEqual(h2["result"]["outcome"], "unknown",
                         "抛了且拿不到结果 ⇒ unknown（记 failed 会诱导重试）")
        self.assertIn("RuntimeError", h2["result"]["error"])


class TestNoImportMcpgw(unittest.TestCase):
    """A10 与 C17 撞同一个 `mcpgw.py` 写者面 ⇒ 本模块必须**完全不碰它**。

    这条不是洁癖：一旦 `actionplane` import 了 mcpgw，本项就无法在任何
    `mcpgw.py` 被并发改写的窗口里独立交付；而且 Action Plane 会退化成
    「MCP 专用层」（传输无关是它的前提）。
    """

    def test_actionplane_does_not_import_mcpgw(self):
        src = (_REPO / "src" / "actionplane.py").read_text(encoding="utf-8")
        tree = ast.parse(src)
        names = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                names.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                names.add(node.module.split(".")[0])
        self.assertNotIn("mcpgw", names, "actionplane 不得 import mcpgw（写者面隔离）")
        # 反向证据：连字符串里都不该出现 `import mcpgw` 的写法（防绕过）
        self.assertNotIn("import mcpgw", src.replace(" ", ""))


if __name__ == "__main__":
    unittest.main(verbosity=2)
