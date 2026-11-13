"""L0 · T5 按片加载器的契约闸门（2026-11-13）。

只做静态判据：加载器尚未接入模板（第 2 步，被并行会话占用而停手），
但它的契约必须先钉死——否则接入口那天容易把「顺序 / 去重 / legacy 回退」
三件事悄悄改坏，而那时首屏已经依赖它了。

判据来源：docs/T5-MODULE-GRAPH-20261113.md 的依赖矩阵与修正后 4 步。
"""
import pathlib
import re
import unittest

REPO = pathlib.Path(__file__).resolve().parents[1]
LOADER = REPO / "static" / "hub-loader.js"
PARTS_DIR = REPO / "static" / "hub"

CORE_EXPECT = [
    "01-core-boot.js", "02-nav-and-poll.js",
    "03-agents-cards.js", "05-chat-and-history.js",
]


class TestT5LoaderContract(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.src = LOADER.read_text(encoding="utf-8")

    def test_loader_exists_outside_shard_glob(self):
        """必须落在 static/ 根：进 hub/ 子目录会被 build_hubjs.sh 拼进 hub.js。"""
        self.assertTrue(LOADER.is_file(), "static/hub-loader.js 不存在")
        parts_glob = list(PARTS_DIR.glob("[0-9][0-9]-*.js"))
        self.assertNotIn(LOADER.name, [p.name for p in parts_glob])
        self.assertFalse(LOADER.name.startswith("00-"),
                         "00- 前缀会命中分片拼接轨道")

    def test_is_classic_script_not_module(self):
        """经典脚本：不能出现 import/export（否则 <script src> 会语法错）。"""
        code = re.sub(r"/\*.*?\*/", "", self.src, flags=re.S)
        self.assertNotRegex(code, r"\bimport\s")
        self.assertNotRegex(code, r"\bexport\s")
        self.assertIn("(function (global)", code)

    def test_core_shard_list_is_the_measured_four(self):
        m = re.search(r"var CORE = \[(.*?)\];", self.src, re.S)
        self.assertIsNotNone(m, "找不到 CORE 清单")
        got = re.findall(r"'([0-9][0-9]-[a-z-]+\.js)'", m.group(1))
        self.assertEqual(got, CORE_EXPECT,
                         "首屏 4 片必须与依赖矩阵实测一致（顺序即依赖顺序）")

    def test_sequential_injection_not_parallel(self):
        """CORE.reduce(chain.then) 逐片等待，不能改成 Promise.all 并发。"""
        self.assertIn("CORE.reduce", self.src)
        self.assertIn("chain.then", self.src)
        self.assertNotIn("Promise.all", self.src)

    def test_ensure_shard_dedupes_by_name(self):
        """同名只发一次请求：pending[name] 缓存 + 失败后删除以便重试。"""
        self.assertIn("if (!pending[name])", self.src)
        self.assertIn("pending[name] = inject", self.src)
        self.assertIn("delete pending[name]", self.src)

    def test_legacy_path_preserved(self):
        """?legacy=1 仍走聚合产物 hub.js（长期契约）。"""
        self.assertIn("'/static/hub.js'", self.src)
        self.assertIn("if (legacy)", self.src)

    def test_vtoken_is_optional_not_hardcoded(self):
        """token 从 HUB_VTOKENS 读（由 build_hubjs.sh 同步），不可写死哈希。"""
        self.assertIn("HUB_VTOKENS", self.src)
        self.assertNotRegex(self.src, r"\?v=[0-9a-f]{8}\b")
