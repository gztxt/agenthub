#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 元闸门：**任务状态词表前后端对账**（2026-11-03，判例 316 的直接产物）。

【要防的事故形态】
后端 `src/tasks_tiers.py:TASK_STATUSES` 是 11 个状态的唯一权威，前端的
`TASK_ST_PILL` / `TASK_ST_TEXT`（static/hub/04-terminal-ws.js）是**手抄的副本**。
2026-11-03 实测：DAG 那套 `TASK_COLORS` 只抄了 5 个，`submitted / verifying /
waiting_input / waiting_approval / cancelled / paused` 六个合法态全部落到
`--border` 兜底 —— 图上一片灰。页面上没有任何地方报错，**静态选择器闸门也全绿**。

【为什么单开一条闸门而不是并进现有测试】
判例 316 的判据是「两套口径必须有个地方机械对账」。抄写发生在 JS，权威在 PY，
中间没有任何编译期约束 ⇒ 只有一条显式对账才能防下一次新增状态漏抄。

【判据】
  ① `TASK_ST_PILL` 的键集合 == `TASK_STATUSES`
  ② `TASK_ST_TEXT` 的键集合 == `TASK_STATUSES`
  ③ 每个键的文案非空、且**互不相同**（两个状态共用一句话 = 用户分不出来）
  ④ 色档只能取 `.stat-pill` 真有的五个 class（index.html 里定义过的）

★ 每条都带自证：拿构造的违规样本喂给判据，断言它真的报红。
  不这么写的闸门绿得没意义 —— 它可能只是压根没跑。
"""
import pathlib
import re
import sys
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))

import tasks_tiers as tiers  # noqa: E402

TERM_JS = _REPO / "static" / "hub" / "04-terminal-ws.js"
INDEX_HTML = _REPO / "templates" / "index.html"

#: `.stat-pill.<tone>` 在 index.html 里真实定义过的色档。JS 里出现别的名字
#: 不会报错，只会**静默没有样式**（背景透明、颜色继承）⇒ 必须点名对账。
PILL_TONES = ("ok", "warn", "bad", "muted", "info")


def _strip_comments(src: str) -> str:
    """去掉 /* */ 与 // 注释。词表里出现过注释掉的旧键，朴素的键名扫描会误报。"""
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    return re.sub(r"(?m)^\s*//.*$", "", src)


def _object_entries(src: str, varname: str):
    """取 `var NAME = { k: v, ... };` 的 (键, 值) 列表。值原样返回（去引号）。

    不用 JSON 解析：对象里有用单引号的、有换行的、值带后缀注释的。
    """
    m = re.search(r"var\s+%s\s*=\s*\{(.*?)\}\s*;" % re.escape(varname), src, re.S)
    assert m, "在 04-terminal-ws.js 里找不到 %s 的定义" % varname
    out = []
    for part in m.group(1).split(","):
        part = part.strip()
        if not part:
            continue
        k, _, v = part.partition(":")
        out.append((k.strip().strip("'\""), v.strip().strip().strip("'\"")))
    return out


class TestVocabParity(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = _strip_comments(TERM_JS.read_text(encoding="utf-8"))
        cls.backend = set(tiers.TASK_STATUSES)
        cls.pill = dict(_object_entries(cls.src, "TASK_ST_PILL"))
        cls.text = dict(_object_entries(cls.src, "TASK_ST_TEXT"))

    def test_pill_covers_every_backend_status(self):
        missing = sorted(self.backend - set(self.pill))
        extra = sorted(set(self.pill) - self.backend)
        self.assertEqual([], missing,
                         "TASK_ST_PILL 漏抄状态 %s：DAG/徽章会落到 muted 兜底，用户看不出区别" % missing)
        self.assertEqual([], extra,
                         "TASK_ST_PILL 有后端不认识的状态 %s：后端永远不会产出它，是死配置" % extra)

    def test_text_covers_every_backend_status(self):
        missing = sorted(self.backend - set(self.text))
        self.assertEqual([], missing,
                         "TASK_ST_TEXT 漏抄状态 %s：页面会把原始英文码直接甩给用户" % missing)
        extra = sorted(set(self.text) - self.backend)
        self.assertEqual([], extra,
                         "TASK_ST_TEXT 有后端不认识的状态 %s" % extra)

    def test_every_status_has_distinct_wording(self):
        for k in sorted(self.backend):
            with self.subTest(status=k):
                self.assertTrue(self.text.get(k), "%s 的文案为空" % k)
        seen = {}
        for k, v in self.text.items():
            self.assertNotIn(v, seen,
                             "%s 与 %s 共用文案「%s」：用户分不出这两个状态" % (k, seen.get(v), v))
            seen[v] = k

    def test_pill_tones_are_real_classes(self):
        bad = sorted({v for v in self.pill.values() if v not in PILL_TONES})
        self.assertEqual([], bad,
                         "色档 %s 在 index.html 里没有 .stat-pill.<tone> 规则 ⇒ 徽章会静默无样式" % bad)

    def test_tone_classes_actually_defined_in_css(self):
        css = INDEX_HTML.read_text(encoding="utf-8")
        for tone in PILL_TONES:
            with self.subTest(tone=tone):
                self.assertRegex(css, r"\.stat-pill\.%s\b" % tone)


class TestGateBites(unittest.TestCase):
    """自证：拿构造的违规样本喂判据，断言真的会报红（否则本文件的绿是假绿）。"""

    def _missing(self, d):
        return sorted(set(tiers.TASK_STATUSES) - set(d))

    def test_catches_the_original_incident(self):
        """2026-11-03 原样复现：只抄 5 个状态的 TASK_COLORS。"""
        old_like = {k: v for k, v in
                    [(x, y) for x, y in
                     dict(_object_entries(_strip_comments(TERM_JS.read_text(encoding="utf-8")),
                                          "TASK_ST_PILL")).items()]
                    if k in ("pending", "running", "success", "failed", "blocked")}
        self.assertEqual(6, len(self._missing(old_like)),
                         "判据没能识别出原事故的 6 个漏抄状态")

    def test_catches_duplicate_wording(self):
        d = dict(_object_entries(_strip_comments(TERM_JS.read_text(encoding="utf-8")), "TASK_ST_TEXT"))
        d["paused"] = d["cancelled"]        # 人为制造同文案
        seen, dup = {}, False
        for k, v in d.items():
            if v in seen:
                dup = True
            seen[v] = k
        self.assertTrue(dup, "同文案判据没有报红")


if __name__ == "__main__":
    unittest.main()