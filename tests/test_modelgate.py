"""L0：配置模型 ⊂ 网关清单的闸门（`src/modelgate.py`）—— /health 的 `model_gate` 情报源。

为什么值得钉（2026-11-02 实测驱动）：
  · 死模型 ID 的事故已经**第四次**同源复发：09-06 `minimax-m3:free`、09-19 `'ultra'`、
    09-23 `qwen3.8-flash` 缺前缀、11-02 `agnes/*` 整族被摘。前三次都只改那一行，
    所以第四次照发；本模块是"让它自己报出来"的那一层。
  · 11-02 的形态最贵：`MANAGER_LLM_MODEL` 400 ⇒ 拆解 100% 失败；`MEMORY_LLM_*` 三层同填
    死 ID ⇒ `complete_free()` 返回 `('', '')` 当降级，后台压缩静默变质；而 `/health` 全绿。

本文件属 L0 hermetic：**零网络**（catalog 一律显式传入）、不 import `src.main`。
"""
import os
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import modelgate  # noqa: E402

# ★ 2026-11-02 23:10 实测 `GET http://127.0.0.1:3456/v1/models`（11 个 ID）。
#   这里取其中的 9 个 chat 档，剔掉 `qwen-image-3.0-pro` / `qwen-audio-3.0-asr-flash` 两个非 chat。
#   ⚠ 上一版把已下架的 `deepseek-v4-flash-0731` 当成在册 = 拿过期清单钉断言，
#   正是本模块要治的病，故改成实测清单。
CATALOG_9 = [
    "alibaba/auto", "alibaba/decision-model-preview",
    "alibaba/deepseek-v4-pro", "alibaba/deepseek-v4-pro-0813",
    "alibaba/deepseek-v4.1-flash", "alibaba/deepseek-v4.1-flash-1017",
    "alibaba/glm-5.3", "alibaba/qwen3.8-flash", "alibaba/qwen3.8-max",
]
# 同一批真实 ID 的 opencode / omp 写法（带 ccr/ 前缀）—— 归一化要能对上。
CATALOG_9_PREFIXED = ["ccr/" + i for i in CATALOG_9]


class TestNormalize(unittest.TestCase):
    def test_strip_ccr_prefix(self):
        self.assertEqual(modelgate.normalize("ccr/alibaba/qwen3.8-flash"),
                         "alibaba/qwen3.8-flash")

    def test_bare_id_untouched(self):
        self.assertEqual(modelgate.normalize("alibaba/qwen3.8-flash"),
                         "alibaba/qwen3.8-flash")

    def test_whitespace_and_none(self):
        self.assertEqual(modelgate.normalize("  alibaba/x  "), "alibaba/x")
        self.assertEqual(modelgate.normalize(None), "")


class TestCheck(unittest.TestCase):
    def test_all_in_catalog_is_ok(self):
        res = modelgate.check(catalog=CATALOG_9,
                              configured={"manager": "alibaba/qwen3.8-flash",
                                          "memory": "alibaba/deepseek-v4.1-flash"})
        self.assertEqual(res["state"], "ok")
        self.assertTrue(res["ok"])
        self.assertEqual(res["total"], 2)
        self.assertEqual(res["catalog_size"], 9)
        self.assertEqual(res["missing"], {})

    def test_dead_id_reported_by_label(self):
        """死 ID 必须按**标签**报出来（只报"有 1 个缺失"等于没说）。"""
        res = modelgate.check(catalog=CATALOG_9,
                              configured={"manager": "alibaba/qwen3.8-flash",
                                          "memory": "agnes/agnes-2.5-flash"})
        self.assertEqual(res["state"], "missing")
        self.assertFalse(res["ok"])
        self.assertEqual(res["missing"], {"memory": "agnes/agnes-2.5-flash"})
        # 活的那个不许被连带判死
        self.assertNotIn("manager", res["missing"])

    def test_prefixed_client_form_is_not_false_positive(self):
        """opencode/omp 存 `ccr/alibaba/x`，清单给裸 ID ⇒ 不能报假阳。"""
        res = modelgate.check(catalog=CATALOG_9,
                              configured={"tier/t2/0": "ccr/alibaba/qwen3.8-max"})
        self.assertEqual(res["state"], "ok")

    def test_catalog_from_prefixed_form_also_matches(self):
        res = modelgate.check(catalog=CATALOG_9_PREFIXED,
                              configured={"tier/t2/0": "alibaba/qwen3.8-max"})
        self.assertEqual(res["state"], "ok")

    def test_unknown_catalog_is_unknown_not_missing(self):
        """「取不到清单」与「清单里没有」必须分开 —— 前者不判死（intake count=0 同源）。"""
        res = modelgate.check(catalog=[], configured={"manager": "alibaba/qwen3.8-flash"})
        self.assertEqual(res["state"], "unknown")
        self.assertFalse(res["checked"])
        self.assertFalse(res["ok"])
        self.assertEqual(res["missing"], {})
        self.assertIn("catalog", res["error"])

    def test_unknown_prefix_separated_from_missing(self):
        """前缀不认识 ⇒ 可能走别的 provider，单列不判死（但要可见）。"""
        res = modelgate.check(catalog=CATALOG_9,
                              configured={"judge": "openai/gpt-6.1-terra-pro",
                                          "manager": "agnes/agnes-2.5-flash"})
        self.assertEqual(res["state"], "missing")
        self.assertEqual(list(res["missing"]), ["manager"])
        self.assertEqual(list(res["unknown_prefix"]), ["judge"])


class TestStatusAndSwitch(unittest.TestCase):
    def test_status_disabled_by_env(self):
        old = os.environ.get("HUB_MODEL_GATE")
        os.environ["HUB_MODEL_GATE"] = "0"
        try:
            res = modelgate.status(catalog=CATALOG_9)
        finally:
            if old is None:
                os.environ.pop("HUB_MODEL_GATE", None)
            else:
                os.environ["HUB_MODEL_GATE"] = old
        self.assertEqual(res["state"], "disabled")

    def test_status_never_raises(self):
        """情报字段不得把 /health 打挂：内部炸了也要回 unknown 而不是抛。"""
        orig = modelgate.check

        def _boom(*a, **kw):
            raise RuntimeError("boom")

        modelgate.check = _boom
        try:
            res = modelgate.status(catalog=CATALOG_9)
        finally:
            modelgate.check = orig
        self.assertEqual(res["state"], "unknown")
        self.assertIn("boom", res["error"])
        self.assertEqual(res["missing"], {})
        self.assertIn("state", res)
        self.assertIn("missing", res)


class TestConfiguredModels(unittest.TestCase):
    """真去收集 9 类 16 处写死模型 —— 零网络（只读配置/常量）。"""

    def test_collects_and_has_no_collection_errors(self):
        got = modelgate.configured_models()
        errs = modelgate.configured_errors()
        self.assertTrue(got, "至少应收到 manager/memory/fallback 三槽")
        for need in ("manager", "memory", "memory_fallback"):
            self.assertIn(need, got)
        # 收集失败 = 闸门有盲区，必须显式红，不能当成"没配置"
        self.assertEqual(errs, {}, f"收集失败项：{errs}")

    def test_tier_members_are_collected(self):
        got = modelgate.configured_models()
        tiers = [k for k in got if k.startswith("tier/")]
        self.assertGreaterEqual(len(tiers), 4, f"档位成员收集不全会留盲区，实测 {tiers}")


if __name__ == "__main__":
    unittest.main(verbosity=2)