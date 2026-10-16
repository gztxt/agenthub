"""L0：会话导出的渲染与脱敏（`src/sessions_export.py`）。

对应 0924 方案档 §三「会话导出（P2.5）」。两个必须钉死的判据：

1) **默认脱敏，且必须递归**。JSON 导出把正文嵌在 `transcript` 里，只扫顶层字符串会
   整层漏掉 —— 而那一层恰恰是唯一带正文的地方。本文件的 `test_nested_transcript_is_redacted`
   就是这条的红对照（改回"只扫顶层"它立刻红）。
   为什么对本项目特别要紧：本工作区已三次被凭据外流打过（备份镜像内 82 个活凭据文件、
   `wiki/log.md` 历史提交含 CCR web token、外发净仓被闸门拦下 3 个抄了真 token 的文档），
   而导出件正是"最容易被顺手 commit / 转发"的产物形态。

2) **不静默改数据**：命中数必须在 meta 里如实回报（`redacted_hits`），
   且 `redact=0` 时字节必须与原文逐字一致 —— 否则用户拿到打码件会当成取证原文。

本文件属 L0 hermetic：纯函数、零网络、零磁盘、不 import `src.main`。
"""
import csv
import io
import json
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import sessions_export as ex  # noqa: E402

TOKEN_GH = "ghp_" + "A" * 36
TOKEN_SK = "sk-" + "B" * 24
TOKEN_AWS = "AKIA" + "C" * 16
TOKEN_KV = "ccr_web_token=abcdEFGH1234567890xyz"
TOKEN_BEARER = "Authorization: Bearer zzZZ1234567890abcd"
PLAIN = "这是一段普通对话正文，没有任何凭据。"


class TestRedaction(unittest.TestCase):
    def test_all_credential_shapes_masked(self):
        for t in (TOKEN_GH, TOKEN_SK, TOKEN_AWS, TOKEN_KV, TOKEN_BEARER):
            out, hits = ex.redact_text(f"前缀 {t} 后缀")
            self.assertGreaterEqual(hits, 1, t)
            self.assertIn(ex.MASK, out)
            for frag in ("A" * 36, "B" * 24, "C" * 16, "abcdEFGH1234567890xyz", "zzZZ1234567890abcd"):
                self.assertNotIn(frag, out)

    def test_key_name_survives_value_dies(self):
        """键值形态只吃值、保留键名 —— 否则读者连"这里原本有个 token"都看不出来。"""
        out, hits = ex.redact_text(TOKEN_KV)
        self.assertIn("ccr_web_token=", out)
        self.assertEqual(hits, 1)

    def test_bare_token_and_json_quoted_forms_are_masked(self):
        """2026-10-16 补的两种漏形态（P1-2 审批预览暴露）。

        · `token=<值>` —— **写门自己的 `?token=` 入口就是这个形态**，此前不在关键词表里；
        · JSON `"api_key":"<值>"` —— 值前多一个引号，而值字符集不含 `"` ⇒ 匹配不到。
        两者都在「消息里出现的凭据」这条线上，不补则**任何**走 redact_text 的路径（会话
        导出、录制落盘、审批预览）都漏。
        """
        secret = "abcdEFGH1234567890xyz"
        for text in ("token=" + secret, '{"api_key":"%s"}' % secret):
            out, hits = ex.redact_text(text)
            self.assertGreaterEqual(hits, 1, text)
            self.assertNotIn(secret, out, text)

    def test_plain_text_untouched(self):
        out, hits = ex.redact_text(PLAIN)
        self.assertEqual(out, PLAIN)
        self.assertEqual(hits, 0)

    def test_none_and_non_str_do_not_raise(self):
        self.assertEqual(ex.redact_text(None), ("", 0))
        out, _ = ex.redact_text(12345)
        self.assertEqual(out, "12345")

    def test_nested_transcript_is_redacted(self):
        """★红对照：正文藏在 dict → list → dict 三层里也必须被打码。"""
        rows = [{"id": "s1", "transcript": [{"role": "user", "content": f"我的 key 是 {TOKEN_SK}"}]}]
        out, hits = ex._redact_rows(rows)
        self.assertEqual(hits, 1)
        self.assertIn(ex.MASK, out[0]["transcript"][0]["content"])
        self.assertNotIn("B" * 24, json.dumps(out, ensure_ascii=False))


class TestCsv(unittest.TestCase):
    def test_header_is_constant_and_ordered(self):
        got = ex.to_csv([{"id": "s1"}], ex.SESSION_COLUMNS)
        header = next(csv.reader(io.StringIO(got)))
        self.assertEqual(header, list(ex.SESSION_COLUMNS))

    def test_missing_cols_empty_extra_cols_dropped(self):
        got = ex.to_csv([{"id": "s1", "bogus": "x"}], ("id", "title"))
        rows = list(csv.reader(io.StringIO(got)))
        self.assertEqual(rows[1], ["s1", ""])

    def test_none_renders_empty_not_literal_none(self):
        got = ex.to_csv([{"id": None}], ("id",))
        self.assertEqual(list(csv.reader(io.StringIO(got)))[1], [""])

    def test_comma_and_newline_escaped(self):
        got = ex.to_csv([{"content": 'a,b\nc"d'}], ("content",))
        self.assertEqual(list(csv.reader(io.StringIO(got)))[1], ['a,b\nc"d'])


class TestRender(unittest.TestCase):
    def test_json_shape_and_meta(self):
        body, ctype, fname, meta = ex.render([{"id": "s1", "title": "标题"}],
                                             ex.SESSION_COLUMNS, "json", "sessions", ts=0)
        self.assertIn("application/json", ctype)
        payload = json.loads(body)
        self.assertEqual(payload["columns"], list(ex.SESSION_COLUMNS))
        self.assertEqual(payload["rows"][0]["title"], "标题")
        self.assertEqual(meta["count"], 1)
        self.assertTrue(meta["redacted"])
        self.assertEqual(meta["redacted_hits"], 0)
        self.assertTrue(fname.endswith(".json"))

    def test_csv_content_type_and_ext(self):
        body, ctype, fname, _ = ex.render([{"id": "s1"}], ex.SESSION_COLUMNS, "csv", "sessions", ts=0)
        self.assertIn("text/csv", ctype)
        self.assertTrue(fname.endswith(".csv"))
        self.assertIn("id,", body.splitlines()[0])

    def test_redact_off_is_byte_faithful(self):
        """redact=0 必须给原文（取证场景），且 meta 要如实说"我没打码"。"""
        rows = [{"content": TOKEN_SK}]
        body, _, _, meta = ex.render(rows, ("content",), "json", "sessions", redact=False, ts=0)
        self.assertIn("B" * 24, body)
        self.assertFalse(meta["redacted"])
        self.assertEqual(meta["redacted_hits"], 0)

    def test_redact_on_reports_hit_count(self):
        body, _, _, meta = ex.render([{"content": TOKEN_SK}, {"content": TOKEN_GH}],
                                     ("content",), "json", "sessions", ts=0)
        self.assertEqual(meta["redacted_hits"], 2)
        self.assertNotIn("B" * 24, body)

    def test_filename_is_ascii_only(self):
        """Content-Disposition 里放中文要 RFC5987，端侧 WebView 行为不一 ⇒ 文件名一律 ASCII。"""
        _, _, fname, _ = ex.render([], ex.SESSION_COLUMNS, "json", "会话/导出 测试", ts=0)
        self.assertTrue(fname.isascii(), fname)
        self.assertNotIn(" ", fname)

    def test_unknown_format_falls_back_to_json(self):
        _, ctype, fname, meta = ex.render([], ex.SESSION_COLUMNS, "xlsx", "sessions", ts=0)
        self.assertIn("application/json", ctype)
        self.assertTrue(fname.endswith(".json"))
        self.assertEqual(meta["format"], "xlsx")      # 如实记录请求的格式，不假装


if __name__ == "__main__":
    unittest.main(verbosity=2)
