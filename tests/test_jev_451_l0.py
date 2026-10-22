#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：jev 区域封禁（HTTP 451）的短路与**可区分**报错。

## 为什么要有它（2026-10-22 实测）

本机直连 `api.typesafe.ai` **恒 451**：

```
HTTP 451 {"title":"Typesafe is not available in your region.","status":451}
```

三处后果，本文件逐个钉住：

1. **假绿**：`jev_client.status()` 原先只检查 key 是否存在 ⇒ 报 `enabled: true`，
   而 `last_ok_ts: null`（从未成功过）。「enabled」≠「可用」。
2. **白等**：`vitals.ask_jev` 每轮 sweep 都发一次请求、等 1.1s 再退回规则，
   看起来是「规则优先」，实际是「远程永远失败」。
3. **误诊**：451（区域封禁）与 403（key 没生效）是两件事，混报会让人去改 key。

判据：451 之后在冷却窗内**不再发请求**，且 `status()` 能说出「区域封禁」。
"""
import pathlib
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import jev_client   # noqa: E402


class TestBlockedBookkeeping(unittest.TestCase):
    def setUp(self):
        jev_client._S_BLOCK["until"] = 0.0
        jev_client._S_BLOCK["reason"] = ""

    def tearDown(self):
        jev_client._S_BLOCK["until"] = 0.0
        jev_client._S_BLOCK["reason"] = ""

    def test_not_blocked_by_default(self):
        self.assertEqual(jev_client.blocked_remaining_s(), 0.0)

    def test_mark_blocked_sets_window_and_reason(self):
        jev_client._mark_blocked("HTTP 451 区域封禁：out of region")
        left = jev_client.blocked_remaining_s()
        self.assertGreater(left, 0)
        self.assertLessEqual(left, jev_client.BLOCKED_COOLDOWN_S)
        self.assertIn("451", jev_client._S_BLOCK["reason"])

    def test_status_reports_region_blocked(self):
        jev_client._mark_blocked("HTTP 451 区域封禁")
        st = jev_client.status()
        self.assertTrue(st.get("region_blocked"), "status 没有报出区域封禁（假绿）")
        self.assertGreater(st.get("blocked_remaining_s") or 0, 0)
        self.assertIn("451", st.get("blocked_reason") or "")

    def test_choose_short_circuits_without_network(self):
        """短路期内 `score_candidates` 必须**不发请求**直接抛（省 1.1s/次）。"""
        import asyncio
        jev_client._mark_blocked("HTTP 451 区域封禁")
        with self.assertRaises(jev_client.JevUnavailable) as cm:
            asyncio.run(jev_client.score_candidates("q", [{"name": "a", "description": "d"}]))
        self.assertIn("451", str(cm.exception))
        self.assertIn("短路", str(cm.exception))

    def test_451_is_not_403(self):
        """两个状态码不许混报：451 是区域，403 是 key 没生效（处置完全不同）。"""
        src = (_REPO / "src" / "jev_client.py").read_text(encoding="utf-8")
        self.assertIn("451", src)
        self.assertIn("403", src)
        i451 = src.index("r.status == 451")
        i403 = src.index("r.status == 403")
        self.assertLess(i451, i403, "451 分支必须在 403 之前（否则永远走不到）")

    def test_cooldown_is_configurable(self):
        self.assertGreater(jev_client.BLOCKED_COOLDOWN_S, 0)


class TestVitalsShortCircuit(unittest.TestCase):
    """vitals 侧也必须短路（它有自己的 urllib 调用，不走 jev_client）。"""

    def test_vitals_has_short_circuit(self):
        src = (_REPO / "src" / "vitals.py").read_text(encoding="utf-8")
        self.assertIn("JEV_BLOCKED_COOLDOWN_S", src)
        self.assertIn("def jev_blocked_for", src)
        self.assertIn("e.code == 451", src)
        # 短路判定必须在真正发请求之前
        i_guard = src.index("if jev_blocked_for() > 0")
        i_req = src.index("urllib.request.Request(JEV_URL", i_guard)
        self.assertLess(i_guard, i_req, "短路判定在发请求之后 = 白等还是会等")


if __name__ == "__main__":
    unittest.main()
