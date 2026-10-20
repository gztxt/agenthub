"""模型计费档（免费/付费）判定与两个下拉的呈现闸门。

【为什么这个闸门值得存在】
用户的原话是「保证 CCR 的付费模型不要出现在选择列表，不然很容易误用且扣费」。
第一版解法是**从 CCR 清单里删掉付费模型**，结果把 `~/.claude/settings.json`
的默认模型（`openrouter/anthropic/claude-opus-5.5:batch`）一起删了 ——
会话不报错、回包 model 静默变成免费模型。所以「可用的付费模型」与
「防误点」只能靠**标注 + 确认**共存，而标注的判定必须只有一个来源。

本文件钉三件事：
1. 判定规则在后端 `api_models.model_billing` 且只有一份（前端只读 `billing` 字段）；
2. 「没有免费标记 = 付费」是**故意保守**，不是遗漏 —— 漏报会直接扣费；
3. 取消确认必须**回滚到上一个值**，不能停在下拉里假装没选。
"""
import json
import re
import subprocess
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))

CORE = _REPO / "static" / "hub" / "01-core-boot.js"
HUBJS = _REPO / "static" / "hub.js"

import api_models  # noqa: E402

#: 2026-10-21 CCR `/v1/models` 实测清单里的代表样本（不是全部 38 个，
#: 只取能覆盖规则边界的那些；全集断言在下面的 count 用例里）。
FREE_IDS = [
    "openrouter/openrouter/free",
    "openrouter/nvidia/nemotron-3.5-lightning:free",
    "openrouter/nvidia/nemotron-3.5-content-safety:free",
    "openrouter/minimax/minimax-m3.1-flash:free",
]
PAID_IDS = [
    "openrouter/anthropic/claude-opus-5.5:batch",
    "openrouter/google/gemini-4-argon",
    "openrouter/qwen/qwen3.7-plus:batch",
    "alibaba/qwen3.8-max",
    "alibaba/deepseek-v4.1-flash",
    "deepseek/deepseek-v4-pro",
    "agnes/agnes-3.0-flash",
]


class ModelBillingRuleTest(unittest.TestCase):
    def test_free_markers(self):
        for mid in FREE_IDS:
            self.assertEqual(api_models.model_billing(mid), "free", mid)

    def test_everything_else_is_paid(self):
        for mid in PAID_IDS:
            self.assertEqual(api_models.model_billing(mid), "paid", mid)

    def test_bare_free_id_without_slash(self):
        """`free` 裸名（部分 adapter 只给裸 ID）也要认。"""
        self.assertEqual(api_models.model_billing("free"), "free")

    def test_suffix_must_be_exact(self):
        """`:freestyle` / `x-free` 不是免费标记 —— 子串匹配会漏报扣费。"""
        for mid in ["openrouter/x:freestyle", "openrouter/x-free", "openrouter/xfree"]:
            self.assertEqual(api_models.model_billing(mid), "paid", mid)

    def test_empty_is_paid(self):
        for mid in ["", None]:
            self.assertEqual(api_models.model_billing(mid), "paid")

    def test_shape_models_emits_billing(self):
        raw = [{"id": "openrouter/openrouter/free", "owned_by": "x"},
               {"id": "alibaba/qwen3.8-max", "owned_by": "x"},
               {"id": "openrouter/openrouter/free", "owned_by": "dup"}]
        out = api_models._shape_models(raw, "claude")
        self.assertEqual(out["count"], 2, "重复 ID 没去重")
        by_id = {m["id"]: m["billing"] for m in out["models"]}
        self.assertEqual(by_id["openrouter/openrouter/free"], "free")
        self.assertEqual(by_id["alibaba/qwen3.8-max"], "paid")


# ── 前端：从 01-core-boot.js 抠纯函数，在 node 里跑 ──────────────────────


def _extract(name: str) -> str:
    src = CORE.read_text(encoding="utf-8")
    m = re.search(r"^function %s\(" % re.escape(name), src, re.M)
    assert m, "01-core-boot.js 里找不到 %s" % name
    i = src.index("{", m.start())
    depth, j = 0, i
    while j < len(src):
        c = src[j]
        if c == "{":
            depth += 1
        elif c == "}":
            depth -= 1
            if depth == 0:
                break
        j += 1
    assert depth == 0, "函数 %s 括号不配对，抠出来的是半截" % name
    return src[m.start():j + 1]


_FUNCS = ["modelIsFree", "modelBillingTag", "modelOptionLabel",
          "modelMetaFromValue", "modelConfirmPaid"]


def _run(body: str) -> str:
    """把五个纯函数 + 一份 localStorage 替身拼起来在 node 里跑。

    `modelConfirmPaid` 只认 `lsGet`/`lsSet` 两个全局名字，替身必须**在这里定义**；
    不去抠真实现（它依赖 window.localStorage，测试要的是**确认次数与回滚语义**）。
    """
    prelude = """
const __store = {};
function lsGet(k) { return Object.prototype.hasOwnProperty.call(__store, k) ? __store[k] : null; }
function lsSet(k, v) { __store[k] = String(v); return true; }
let __asks = 0;
function yesAsk(t) { __asks++; return true; }
function noAsk(t) { __asks++; return false; }
"""
    js = prelude + "\n".join(_extract(n) for n in _FUNCS) + "\n" + body
    p = subprocess.run(["node", "-e", js], capture_output=True, text=True, timeout=30)
    assert p.returncode == 0, "node 执行失败：%s\n%s" % (p.stderr[:400], body)
    return p.stdout.strip()


class FrontendBillingTagTest(unittest.TestCase):
    def test_missing_billing_is_paid(self):
        """旧后端 / 旧缓存串没有 `billing` 字段时必须按付费 —— 宁可多问一次。"""
        out = _run("console.log(modelBillingTag({id:'x'}));")
        self.assertEqual(out, "付费")

    def test_labels(self):
        self.assertEqual(_run("console.log(modelOptionLabel('a/b', {billing:'free'}));"), "[免费] a/b")
        self.assertEqual(_run("console.log(modelOptionLabel('a/b', {billing:'paid'}));"), "[付费] a/b")

    def test_label_keeps_display_name(self):
        out = _run("console.log(modelOptionLabel('a/b', {billing:'free', name:'Nice Name'}));")
        self.assertEqual(out, "[免费] a/b — Nice Name")

    def test_label_does_not_duplicate_same_name(self):
        out = _run("console.log(modelOptionLabel('a/b', {billing:'free', name:'a/b'}));")
        self.assertEqual(out, "[免费] a/b")


class MetaFromValueTest(unittest.TestCase):
    """两个下拉的 value 形状不同（opencode 带 `ccr/` 前缀），反查必须认两种。"""

    def test_plain_id(self):
        out = _run("""console.log(JSON.stringify(modelMetaFromValue(
          'alibaba/qwen3.8-max', {}, [{id:'alibaba/qwen3.8-max', billing:'paid'}])));""")
        d = json.loads(out)
        self.assertEqual(d["id"], "alibaba/qwen3.8-max")
        self.assertEqual(d["m"]["billing"], "paid")

    def test_prefixed_id(self):
        out = _run("""console.log(JSON.stringify(modelMetaFromValue(
          'ccr/alibaba/qwen3.8-max', {value_prefix:'ccr/'},
          [{id:'alibaba/qwen3.8-max', billing:'paid'}])));""")
        d = json.loads(out)
        self.assertEqual(d["id"], "alibaba/qwen3.8-max")
        self.assertIsNotNone(d["m"])

    def test_empty_value_is_null_not_error(self):
        self.assertEqual(_run("console.log(modelMetaFromValue('', {}, []));"), "null")

    def test_unknown_model_returns_null_meta(self):
        """清单外模型（清单外补项）也要能拿到 id，只是 m 为 null ⇒ 按付费兜底。"""
        out = _run("console.log(JSON.stringify(modelMetaFromValue('x/y', {}, [])));")
        d = json.loads(out)
        self.assertEqual(d["id"], "x/y")
        self.assertIsNone(d["m"])

    def test_prefix_fallback_when_agent_not_loaded(self):
        """`SET_AGENTS` 为空（首页还没拉设置数据）时拿不到 value_prefix。

        不做后缀兜底就会**匹配不到 ⇒ 按付费兜底 ⇒ 连免费模型也弹扣费确认**，
        用户会以为功能坏了。这条钉的是「兜底方向不能反」。
        """
        out = _run("""console.log(JSON.stringify(modelMetaFromValue(
          'ccr/openrouter/openrouter/free', null,
          [{id:'openrouter/openrouter/free', billing:'free'}])));""")
        d = json.loads(out)
        self.assertEqual(d["id"], "openrouter/openrouter/free")
        self.assertEqual(d["m"]["billing"], "free")

    def test_prefix_fallback_does_not_match_partial_tail(self):
        """后缀兜底必须按 `/` 边界 —— 否则 `x/free` 会被 `somefree` 误配。"""
        out = _run("""console.log(JSON.stringify(modelMetaFromValue(
          'ccr/ns/notfree', null, [{id:'free', billing:'free'}])));""")
        d = json.loads(out)
        self.assertIsNone(d["m"])


class PaidConfirmTest(unittest.TestCase):
    def test_free_never_asks(self):
        out = _run("""
          const r = modelConfirmPaid('a/b', {billing:'free'}, yesAsk);
          console.log(r + ' ' + __asks);""")
        self.assertEqual(out, "true 0")

    def test_paid_asks_once_then_remembers(self):
        out = _run("""
          const m = {billing:'paid'};
          const r1 = modelConfirmPaid('a/b', m, yesAsk);
          const r2 = modelConfirmPaid('a/b', m, noAsk);   // 已确认过 ⇒ 不再问
          console.log([r1, r2, __asks].join(' '));""")
        self.assertEqual(out, "true true 1", "同一模型被问了第二次，或没记住")

    def test_declined_returns_false_and_does_not_remember(self):
        out = _run("""
          const m = {billing:'paid'};
          const r1 = modelConfirmPaid('a/b', m, noAsk);
          const r2 = modelConfirmPaid('a/b', m, yesAsk);  // 上次拒绝不算确认
          console.log([r1, r2, __asks].join(' '));""")
        self.assertEqual(out, "false true 2")

    def test_empty_value_never_asks(self):
        out = _run("console.log(modelConfirmPaid('', null, yesAsk) + ' ' + __asks);")
        self.assertEqual(out, "true 0")

    def test_missing_confirm_host_does_not_block(self):
        """宿主没有 confirm（无头/受限 webview）时不阻塞，但标记仍然可见。"""
        out = _run("""
          Object.defineProperty(globalThis, 'window', {value: undefined, configurable: true});
          console.log(modelConfirmPaid('a/b', {billing:'paid'}, null));""")
        self.assertEqual(out, "true")


class WiringTest(unittest.TestCase):
    """两个下拉都必须走同一套标注/确认 —— 只改一处是最容易漏的错法。"""

    def test_both_selects_use_label_helper(self):
        core = CORE.read_text(encoding="utf-8")
        chat = (_REPO / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
        self.assertIn("modelOptionLabel(mid, m)", core, "设置页没走统一 label")
        self.assertIn("modelOptionLabel(mid, m)", chat, "对话页没走统一 label")
        self.assertIn("modelConfirmPaid", core, "设置页没接确认")
        self.assertIn("modelConfirmPaid", chat, "对话页没接确认")

    def test_rollback_targets_exist(self):
        core = CORE.read_text(encoding="utf-8")
        chat = (_REPO / "static" / "hub" / "04-terminal-ws.js").read_text(encoding="utf-8")
        self.assertRegex(core, r"SET_MODEL_PREV\s*=\s*sel\.value",
                         "设置页没记录渲染后的合法值 ⇒ 取消无处可回滚")
        self.assertRegex(chat, r"CHAT_MODEL_PREV\s*=\s*sel\.value",
                         "对话页没记录渲染后的合法值 ⇒ 取消无处可回滚")

    def test_hubjs_rebuilt(self):
        """改了分片却忘了 build_hubjs.sh ⇒ 线上仍跑旧拼接（这条闸门抓过一次）。"""
        built = HUBJS.read_text(encoding="utf-8")
        self.assertIn("function modelConfirmPaid(", built,
                      "static/hub.js 里没有新函数：忘记跑 scripts/build_hubjs.sh")


if __name__ == "__main__":
    unittest.main()
