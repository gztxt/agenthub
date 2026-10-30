#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：JSON 定点编辑（src/jsonspan.py，v0.13.181）。

为什么必须存在：
1. **"只改目标 key"是承诺，不是口号**：本模块的价值全在"区间外字节不变"，
   所以每条用例都断言**未触碰的字节逐字相等**，而不是只断言解析后的 dict 相等。
2. **坏文件必须报错**（magpie 的 never read as empty）：把解析失败当空对象，
   下一步就是把整份配置覆盖成只剩我们写的那几个键。
3. **同名键不能串**：`model` 与 `env.model` 同时存在，改后者绝不能碰到前者 ——
   这正是正则式"全局替换"必然出错的形状。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import jsonspan as J  # noqa: E402


SRC = """{
    "env": {
        "MODEL": "a/b",
        "NOTE": "含 // 不是注释，含 \\" 引号"
    },
    "model": "a/b",
    "list": [
        1,
        2
    ]
}
"""


class ANoCommentLoss(unittest.TestCase):
    def test_blank_comments_keeps_length_and_offsets(self):
        src = '{\n  // 行注释\n  "a": "x // 不是注释"\n}\n'
        out = J.blank_comments(src)
        self.assertEqual(len(out), len(src), "抹注释必须等长，否则报错位置全错")
        self.assertEqual(out.count("\n"), src.count("\n"))
        self.assertNotIn("行注释", out)

    def test_string_literal_is_not_treated_as_comment(self):
        src = '{"u": "http://x//y", "v": 1}'
        self.assertIn("http://x//y", J.blank_comments(src))

    def test_validate_raises_on_truncated_json(self):
        with self.assertRaises(J.JsonSpanError):
            J.validate('{"a": {"b": 1}')

    def test_validate_returns_value_on_jsonc(self):
        self.assertEqual(J.validate('{\n // c\n "a": 1\n}'), {"a": 1})


class BSetString(unittest.TestCase):
    def test_only_target_bytes_change(self):
        out, old = J.set_string(SRC, ("env", "MODEL"), "c/d")
        self.assertEqual(old, "a/b")
        self.assertEqual(out, SRC.replace('"MODEL": "a/b"', '"MODEL": "c/d"'))

    def test_same_name_at_other_level_is_untouched(self):
        out, _ = J.set_string(SRC, ("env", "MODEL"), "c/d")
        self.assertIn('    "model": "a/b"', out, "顶层同名键被误改 —— 正则全局替换的典型事故")

    def test_missing_key_raises_keyerror_not_silent(self):
        with self.assertRaises(KeyError):
            J.set_string(SRC, ("env", "NOPE"), "x")

    def test_non_string_value_is_refused(self):
        with self.assertRaises(J.JsonSpanError):
            J.set_string(SRC, ("list",), "x")

    def test_escaped_and_unicode_roundtrip(self):
        # 先把 NOTE 换成带转义/中文的值，再验证它没被别的键的写入波及
        out, old = J.set_string(SRC, ("env", "NOTE"), "新值 \"q\" \\ 尾")
        self.assertEqual(old, '含 // 不是注释，含 " 引号')
        self.assertEqual(J.get_string(out, ("env", "NOTE")), '新值 "q" \\ 尾')
        out2, _ = J.set_string(out, ("model",), "x/y")
        self.assertIn('"NOTE": "新值 \\"q\\" \\\\ 尾"', out2, "相邻键的转义被改动了")
        self.assertEqual(J.get_string(out2, ("env", "NOTE")), '新值 "q" \\ 尾')

    def test_result_still_parses(self):
        out, _ = J.set_string(SRC, ("model",), "x/y")
        J.validate(out)


class CAppendObject(unittest.TestCase):
    def test_multiline_array_appends_at_end_only(self):
        out = J.append_object(SRC, ("list",), {"k": 1})
        self.assertEqual(out.splitlines()[:8], SRC.splitlines()[:8], "已有内容被重排")
        self.assertEqual(J.validate(out)["list"][-1], {"k": 1})

    def test_empty_array(self):
        out = J.append_object('{\n  "a": []\n}\n', ("a",), {"k": 1})
        self.assertEqual(J.validate(out)["a"], [{"k": 1}])

    def test_single_line_array_stays_single_line(self):
        out = J.append_object('{"a":[1]}', ("a",), {"k": 1})
        self.assertNotIn("\n", out, "单行数组不该被硬塞成多行")

    def test_missing_path_raises_keyerror(self):
        with self.assertRaises(KeyError):
            J.append_object(SRC, ("nope",), {"k": 1})

    def test_non_array_is_refused(self):
        with self.assertRaises(J.JsonSpanError):
            J.append_object(SRC, ("model",), {"k": 1})


if __name__ == "__main__":
    unittest.main()
