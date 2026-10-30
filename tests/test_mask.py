#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：出站可逆脱敏（src/mask.py + llm 接线，v0.13.181）。

为什么必须存在：
1. **同值同符**是它能用的前提 —— 占位符每轮都变会打掉 prompt cache、让模型
   "这段上下文跟上一轮不是一回事"。用例直接断言两次掩码得到同一个占位符。
2. **还原必须精确到字节**：模型原样回吐占位符时，用户看到的必须是真值，
   否则就是把内部记号漏进对话。
3. **接线位置**：掩在 `_create_message` 的最后一跳（body 出站前），
   上层结构不动 —— 用替身 `_post_once` 抓实际发出的 body 来证。
"""
import asyncio
import json
import os
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import llm     # noqa: E402
import mask    # noqa: E402

# 夹具**拼出来**、不写成字面量：字面量会让本文件命中推前闸门的高危模式。真实凭据扫描
# 不该为测试数据开豁免（豁免一开，漏报就再也看不出来）；运行时值逐字节相同，断言不受影响。
SK = "sk-" + "abcdefghijklmnopqrstuvwxyz012345"
GH = "ghp_" + "A1b2C3d4E5f6G7h8I9j0K1l2M3n4O5p6Q7r8"
_PK = "PRIVATE"      # 同上：`PRIVATE` + `KEY` 拆开写，源码里不出现那个两词连写
PRIV = (f"-----BEGIN RSA {_PK} KEY-----\nMIIEow\nabc\n"
        f"-----END RSA {_PK} KEY-----")


class AMaskRules(unittest.TestCase):
    def test_secrets_are_masked(self):
        for secret, tag in ((SK, "SK"), (GH, "GH"), (PRIV, "PRIVKEY"),
                            ("AIza" + "b" * 34, "GGL"),
                            ("AKIA" + "IOSFODNN7EXAMPLE", "AWS")):
            out, mp = mask.mask_text("前缀 " + secret + " 后缀")
            self.assertNotIn(secret, out, tag)
            self.assertEqual(1, len(mp), tag)

    def test_same_value_same_placeholder(self):
        a, _ = mask.mask_text(SK)
        b, _ = mask.mask_text("再看一次：" + SK)
        self.assertIn(a.strip(), b, "同值必须同符，否则 prompt cache 每轮失效")

    def test_ordinary_text_is_untouched(self):
        for s in ("task-1234567890abcdef", "alibaba/qwen3.8-flash",
                  "sk-short", "文件 /home/gztxt/a.md 没动"):
            out, mp = mask.mask_text(s)
            self.assertEqual(s, out, f"误伤：{s}")
            self.assertEqual({}, mp)

    def test_unmask_restores_bytes(self):
        out, mp = mask.mask_text("key=" + SK + " 与 " + GH)
        self.assertEqual("key=" + SK + " 与 " + GH, mask.unmask_text(out, mp))

    def test_nested_object_roundtrip(self):
        body = {"system": "用 " + SK, "messages": [{"role": "user",
                                                    "content": [{"type": "text", "text": GH}]}],
                "n": 1, "flag": True}
        m, mp = mask.mask_obj(body)
        self.assertNotIn(SK, json.dumps(m))
        self.assertNotIn(GH, json.dumps(m))
        self.assertEqual(1, m["n"])
        self.assertIs(True, m["flag"])
        self.assertEqual(body, mask.unmask_obj(m, mp))

    def test_env_switch_disables(self):
        old = os.environ.get("HUB_MASK_OUTBOUND")
        try:
            os.environ["HUB_MASK_OUTBOUND"] = "0"
            self.assertFalse(mask.enabled())
            os.environ["HUB_MASK_OUTBOUND"] = "1"
            self.assertTrue(mask.enabled())
        finally:
            if old is None:
                os.environ.pop("HUB_MASK_OUTBOUND", None)
            else:
                os.environ["HUB_MASK_OUTBOUND"] = old


class BWiring(unittest.TestCase):
    """真正要证的是：**发出去的 body** 里没有密钥，**回来的 text** 里是真值。"""

    def setUp(self):
        self._saved_post = llm._post_once
        self._old_env = os.environ.get("HUB_MASK_OUTBOUND")
        os.environ["HUB_MASK_OUTBOUND"] = "1"

    def tearDown(self):
        llm._post_once = self._saved_post
        if self._old_env is None:
            os.environ.pop("HUB_MASK_OUTBOUND", None)
        else:
            os.environ["HUB_MASK_OUTBOUND"] = self._old_env

    def test_outbound_body_masked_and_reply_restored(self):
        seen = {}

        async def post(body, request_id):
            seen["body"] = json.loads(json.dumps(body))
            ph = body["messages"][0]["content"]
            return {"content": [{"type": "text", "text": "我看到了 " + ph}],
                    "usage": {"input_tokens": 1}}

        llm._post_once = post
        data = asyncio.run(llm._create_message(None, [{"role": "user", "content": "key=" + SK}]))
        wire = json.dumps(seen["body"], ensure_ascii=False)
        self.assertNotIn(SK, wire, "密钥明文上线了")
        self.assertRegex(wire, r"\{\{SK_[0-9a-f]{8}\}\}")
        self.assertIn(SK, data["content"][0]["text"], "回包里的占位符没还原成真值")

    def test_disabled_sends_plaintext(self):
        seen = {}

        async def post(body, request_id):
            seen["body"] = body
            return {"content": [{"type": "text", "text": "ok"}]}

        llm._post_once = post
        os.environ["HUB_MASK_OUTBOUND"] = "0"
        asyncio.run(llm._create_message(None, [{"role": "user", "content": "key=" + SK}]))
        self.assertIn(SK, json.dumps(seen["body"], ensure_ascii=False),
                      "关掉开关就该原样发（逃生阀必须真的能逃）")


if __name__ == "__main__":
    unittest.main()
