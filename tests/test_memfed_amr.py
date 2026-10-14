#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：AMR（Agent Memory Repo）格式记忆源的接入（v0.13.103 P2）。

分层口径（tests/tiers.py）：零宿主依赖——夹具写在 tmp、不读真盘、不起服务、不打网络、
不 import src.main。**不允许 SKIP**。

为什么这张闸门必须存在：
Cognition 2026-10-04 发布的 AMR 是**外部规范**（MEMORY.md 入口、`[source: url; added: 日期]`
溯源、`[[path]]` 交叉链接），本机此刻**尚无**该形态的仓 ⇒ 把根路径做成「约定位点 + 可覆盖」
后，**唯一能证明它真的能消费该格式的东西就是夹具测试**——真盘上没有样本可断言。

钉住三件事：
1. 仓存在时，AMR 舱按 rg 只读召回，条目带 `source=amr_memory`（能被联邦拿去融合）；
2. 仓**不存在**时逐路报 `ok=False`（不静默返空——那是本仓最忌讳的失效形态）；
3. 该源进得了搜索白名单与融合路径（注册表在、权重在）。
"""
import os
import pathlib
import shutil
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import memfed  # noqa: E402


class TestAmrSource(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="amr-"))
        # 造一个最小 AMR 仓：入口 MEMORY.md + 子目录条目 + 交叉链接
        (self.tmp / "MEMORY.md").write_text(
            "# Memory\n\n"
            "- 本机记忆权威副本是 TDAI(:8420) [source: http://127.0.0.1:3102; added: 2026-10-13]\n"
            "- 支付与站点共享 2026-10-15 上线窗口 [source: http://x/102; added: 2026-09-03]\n\n"
            "## Index\n- [[projects/payments]]\n", encoding="utf-8")
        (self.tmp / "projects").mkdir()
        (self.tmp / "projects" / "payments.md").write_text(
            "# payments\n\n- Priya 负责计费上线定价，见 [[MEMORY]]\n", encoding="utf-8")

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _search(self, root, q="payments"):
        # 直接调 rg 检索实现，把根指到夹具（AMR_ROOT 是模块级常量，测试用函数入参绕开）
        saved = memfed._RG_TARGETS.get("amr_memory")
        memfed._RG_TARGETS["amr_memory"] = ([str(root)], "*.md")
        try:
            return memfed._search_rg_text("amr_memory", q, 10, 5.0)
        finally:
            if saved:
                memfed._RG_TARGETS["amr_memory"] = saved

    def test_registered_and_weighted(self):
        """AMR 源必须在注册表里、enabled、带权重——否则融合层不会带上它。"""
        self.assertIn("amr_memory", memfed.REGISTRY)
        self.assertTrue(memfed.REGISTRY["amr_memory"].enabled)
        self.assertGreater(memfed.fed_weight("amr_memory"), 0.0)
        self.assertIn("amr_memory", memfed.enabled_ids())

    def test_consumes_amr_fixture(self):
        """真仓存在时能召回，且召回内容里带 AMR 的 `[source:]` 溯源串。

        查询词取「权威副本」——它落在夹具 MEMORY.md 那条带 `[source: …]` 的 bullet 上；
        取 "payments" 只会命中 `[[projects/payments]]` 链接行与 payments.md 正文，
        **那两处都不含溯源串**（夹具故意同时放了两类行，以证明断言确有所指）。
        """
        r = self._search(self.tmp, q="权威副本")
        self.assertTrue(r.get("ok"), f"应 ok，实际 {r}")
        self.assertGreaterEqual(r.get("count"), 1)
        self.assertEqual(r["items"][0]["source"], "amr_memory")
        hits = [str(i.get("content") or "") for i in r["items"]]
        self.assertTrue(any("source:" in h for h in hits),
                        f"AMR 的 [source:] 溯源串应随内容召回，实际 {hits}")

    def test_missing_root_reports_not_silent(self):
        """根不存在 ⇒ ok=False 逐路报错（禁静默返空：本仓最忌讳的失效形态）。"""
        ghost = self.tmp / "nope"
        r = self._search(ghost)
        self.assertFalse(r.get("ok"), "根不存在必须 ok=False")
        self.assertEqual(r.get("count"), 0)
        self.assertTrue(r.get("error"), "ok=False 时必须带 error 说明")
        self.assertIn("不存在", str(r.get("error")))

    def test_entries_compatible_with_fusion(self):
        """条目形状必须能被 _rrf_fuse 消费：带 source 与 id（去重键依赖二者）。"""
        r = self._search(self.tmp)
        for it in r["items"]:
            self.assertIn("source", it)
            self.assertIn("id", it)
            self.assertTrue(it["id"])


if __name__ == "__main__":
    unittest.main(verbosity=2)