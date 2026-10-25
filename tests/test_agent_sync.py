#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：agent 四面对齐复验脚本（scripts/agent_sync.py，v0.13.160）的判据。

为什么必须有（2026-10-25 用户令「后续添加 agent 自动注册、同步所有功能、复验现有 agent」）：
判据如果只写在脚本里、只被人看过一次，下一次改 `entries_for` 或 `state_of` 就会静默漂移，
而症状是「界面上少一个按钮」——没人会为此回来看脚本。把六条判据钉成红灯，才不会退回去。

每条用例对应一次真实形态：
1. 完整卡（菜单/模型/界面/终端四面齐）→ 不许报噪音，否则脚本会被无视。
2. `missing_recipe`：既没配方也没标 TUI-only ⇒ 漏登记（v0.13.151 前 dsh 的样子）。
3. `ok` 但按钮没出来 ⇒ v0.13.151 用户报障「dsh 原生界面按钮没有了」。
4. `tui_only` 但没说原因 ⇒ 「静默少按钮」，用户不知道是设计还是坏。
5. 装了却只有「详情」⇒ 卡不可用。
6. 菜单有、模型页没行 ⇒ 模型设置没接入（如实置灰也有行，完全没行才是缺口）。
7. running 且无 start ⇒ 常驻豁免（codebuddy），不该报 WARN。
"""
import json
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "scripts"))

import agent_sync  # noqa: E402


def _card(pid="demo", kind="agent", status="installed", ui_state="ok",
          ui_note="", entries=None):
    return {
        "id": pid, "name": pid, "kind": kind, "status": status,
        "ui_state": ui_state, "ui_note": ui_note,
        "entries": entries if entries is not None else [
            {"type": "embed", "label": "嵌入会话", "url": "http://127.0.0.1:9000",
             "start": {"unit": "demo"}},
            {"type": "open", "label": "新窗口", "url": "http://127.0.0.1:9000",
             "start": {"unit": "demo"}},
            {"type": "term", "label": "终端", "agent": pid},
        ],
    }


class TestMatrix(unittest.TestCase):
    def _codes(self, card, models=None):
        rows = agent_sync.build_matrix([card], models if models is not None
                                       else [{"id": card["id"]}])
        self.assertEqual(len(rows), 1)
        return rows[0]

    def test_full_card_is_clean(self):
        r = self._codes(_card())
        self.assertEqual(r["fails"], [])
        self.assertEqual(r["warns"], [])

    def test_missing_recipe_is_fail(self):
        r = self._codes(_card(ui_state="missing_recipe"))
        self.assertIn("missing_recipe", r["fails"])

    def test_ok_without_buttons_is_fail(self):
        r = self._codes(_card(entries=[{"type": "term", "label": "终端", "agent": "demo"}]))
        self.assertIn("ui_no_button", r["fails"])

    def test_tui_only_without_reason_is_fail(self):
        r = self._codes(_card(ui_state="tui_only", ui_note="",
                             entries=[{"type": "term", "label": "终端", "agent": "demo"}]))
        self.assertIn("tui_no_reason", r["fails"])
        # 写了原因就不许再红 —— 否则「如实标注」也会被罚
        r2 = self._codes(_card(ui_state="tui_only", ui_note="TUI 即原生界面",
                              entries=[{"type": "term", "label": "终端", "agent": "demo"}]))
        self.assertEqual(r2["fails"], [])

    def test_installed_with_only_detail_is_fail(self):
        r = self._codes(_card(entries=[{"type": "detail", "label": "详情"}]))
        self.assertIn("card_unusable", r["fails"])

    def test_model_row_missing_is_warn(self):
        r = self._codes(_card(), models=[])
        self.assertEqual(r["fails"], [])
        self.assertIn("models_missing", r["warns"])

    def test_no_start_on_stopped_is_warn(self):
        entries = [{"type": "embed", "label": "嵌入会话", "url": "http://127.0.0.1:9000"},
                   {"type": "term", "label": "终端", "agent": "demo"}]
        r = self._codes(_card(status="stopped", entries=entries))
        self.assertIn("ui_no_start", r["warns"])

    def test_resident_running_without_start_is_clean(self):
        """常驻豁免（codebuddy）：running 且不带 start 是设计，不是缺口。"""
        entries = [{"type": "embed", "label": "嵌入会话", "url": "http://127.0.0.1:9000"},
                   {"type": "term", "label": "终端", "agent": "demo"}]
        r = self._codes(_card(status="running", entries=entries))
        self.assertEqual(r["warns"], [])

    def test_non_agent_kinds_are_excluded(self):
        rows = agent_sync.build_matrix(
            [_card(pid="svc", kind="service"), _card(pid="demo")], [{"id": "demo"}])
        self.assertEqual([r["id"] for r in rows], ["demo"])

    def test_fails_sort_first(self):
        bad = _card(pid="zzz", ui_state="missing_recipe")
        good = _card(pid="aaa")
        rows = agent_sync.build_matrix([good, bad], [{"id": "aaa"}, {"id": "zzz"}])
        self.assertEqual([r["id"] for r in rows], ["zzz", "aaa"])


class TestRender(unittest.TestCase):
    def test_render_reports_counts(self):
        rows = agent_sync.build_matrix([_card()], [{"id": "demo"}])
        text = agent_sync.render(rows)
        self.assertIn("FAIL 0", text)
        self.assertIn("demo", text)

    def test_json_shape_is_stable(self):
        rows = agent_sync.build_matrix([_card()], [{"id": "demo"}])
        blob = json.loads(json.dumps({"rows": rows}, ensure_ascii=False))
        for key in ("id", "ui_state", "term", "embed", "models", "fails", "warns"):
            self.assertIn(key, blob["rows"][0])


if __name__ == "__main__":
    unittest.main()
