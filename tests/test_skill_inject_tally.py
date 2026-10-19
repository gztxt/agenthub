#!/usr/bin/env python3
"""L0 hermetic 测试：技能注入回写记账 + 僵尸榜「账空告警」（判例 103 的修复闸门）。

**为什么必须有这个文件**：2026-10-09 实测生产 `/api/skill/zombies` 返回
`counted=0 zombies=266`，把 `agent-dispatch`（实测被调用 166 次、全机最热）列为
零调用僵尸。根因两层：

  ① **记账层**：`runlog.track` 的 detail 只记 q/limit/routes/channel，**没有 `name`**，
     而 `skill_usage._name_of` 只认 `detail["name"]` ⇒ 写了也记不上 ⇒ `counted` 恒 0。
  ② **呈现层**：`counted===0`（压根没记账）与「有记账但都是 0」（真的没人用）
     在界面上**同形**，用户读成「都没用」就会去删。

两层任一单独修好都不够：只修①则改动不可见（无人验证）；只修②则横幅正确但数据仍空。
⇒ 本文件**两侧都钉**，且每条都带红向自证。

**为什么是 L0**：纯静态断言（只读源文件、零服务零网络），按 `tests/README.md` 的分层口径
归L0，每次提交必过。写成 `verify_*.py` 的话 pre-commit 根本不收（`test_skill_center_ui.py`
开头已记过这个坑）。
"""
import pathlib
import re
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
RUNLOG = _REPO / "src" / "runlog.py"
TERMINAL = _REPO / "static" / "hub" / "04-terminal-ws.js"
SKILL_USAGE = _REPO / "src" / "skill_usage.py"

RUNLOG_SRC = RUNLOG.read_text(encoding="utf-8")
JS = TERMINAL.read_text(encoding="utf-8")
USAGE_SRC = SKILL_USAGE.read_text(encoding="utf-8")


def strip_js_comments(src: str) -> str:
    """剥掉块/行注释，只留真代码。

    必须有它：本文件要断言的是**代码里有没有那个分支**，不是注释里有没有写那段话。
    注释里写「counted 为 0 要告警」而代码没写分支，是本仓吃过的那种静默假绿。
    （范式抄 `test_skill_center_ui.py`，不重复发明。）
    """
    src = re.sub(r"/\*.*?\*/", "", src, flags=re.S)
    src = re.sub(r"(?m)^\s*//.*$", "", src)
    src = re.sub(r"(?<![:'\"\\])//[^'\"\\]*$", "", src, flags=re.M)
    return src


JS_CODE = strip_js_comments(JS)


class TestNameTallyInjected(unittest.TestCase):
    """① 记账层：`runlog.track` 必须把返回体里的技能名带进 detail。"""

    def test_pick_names_exists(self):
        self.assertIn("def _pick_names", RUNLOG_SRC,
                      "缺 _pick_names ⇒ skill_usage 永远记不上账（counted 恒 0）")

    def test_pick_names_handles_real_relevant_shape(self):
        r"""必须认得 `/api/skill/relevant` 的**真实**返回形状 bm25.items[].name。

        红向：这个形状是 skill.py:1052 端点的实际产出；只认 `items` 或只认 `names`
        都会让它返回空 —— 而返回空**不报错**，账照旧是空的。
        """
        self.assertIn('data.get("bm25")', RUNLOG_SRC)
        self.assertIn('bm.get("items")', RUNLOG_SRC)

    def test_pick_names_handles_read_single_object(self):
        r"""必须认得 `/api/skill/read` 的单对象形状（顶层 `name`，**不套 items**）。

        红向：`/read`（skill.py:804）回的是**一个**技能对象。漏认它 ⇒ `skill.read` 事件
        永远不带 `name` ⇒ `counts()` 的 reads 桶恒空 ⇒「读技能」在僵尸榜上不可见。
        与 relevant 的 bm25 形状是**两条**独立分支，缺一不可（2026-10-10 补）。
        """
        self.assertIn('isinstance(data.get("name"), str)', RUNLOG_SRC,
                      "缺 /api/skill/read 单对象分支 ⇒ 读技能永远记不上账（判例 103 同族）")
        self.assertIn("items = [data]", RUNLOG_SRC,
                      "单对象分支必须把 data 自身当唯一条目")

    def test_pick_names_is_exhaustive_not_silent(self):
        """逐层降级：bm25.items → items → names → 直接 list。

        钉住「任一层不认识就返回空串而不是抛」——埋点抛异常会带倒业务端点。
        ⚠️ 取样范围必须**限定在 _pick_names 函数体内**（用 `(?=^def |\Z)` 收尾）：
        直接 `def _pick_names.*?except Exception.*?return []` 会跨过函数尾，
        匹配到**后面另一个函数**的 except/return ——本文件第一版就这么假红的。
        """
        for frag in ('data.get("items")', 'data["names"]', "isinstance(data, list)"):
            self.assertIn(frag, RUNLOG_SRC, f"缺降级分支 {frag}")
        body = re.search(r"def _pick_names.*?(?=\n\ndef |\n\n#:|\n\n@|\Z)",
                         RUNLOG_SRC, re.S)
        self.assertIsNotNone(body, "找不到 _pick_names 函数体")
        fn = body.group(0)
        self.assertIn("except Exception", fn, "_pick_names 必须整体兜住异常")
        # 兜底必须返回空列表。行尾允许注释（`except Exception:  # noqa: BLE001`），
        # 所以用 `[^\n]*`跨到行尾再换行——第一版没放行注释、把正确代码判成红。
        self.assertRegex(fn, r"except Exception[^\n]*\n\s*return \[\]",
                         "兜底必须返回空列表（`return []`，不是 `return \"\"`——"
                         "后者是 dict 的兜底形状，抄错就静默记不上账）")
        # 反向：函数体尾不许有游离 return（插桩时最容易留下的暗伤：本批就中过一次——
        # 插入 _pick_names 时把 _pick_routes 末尾的 `return ""` 顶到了这里）。
        tail = fn.rstrip().rsplit("return", 1)[-1]
        self.assertNotRegex(fn, r"return \[\]\s*\n\s*return",
                            "_pick_names 尾部有游离 return：多半是插桩截断了上一个函数")

    def test_track_writes_name_per_row(self):
        """**核心断言**：track 落库时必须逐名写 `name`。

        为什么是「逐名多行」而不是一个数组：`skill_usage.counts()` 每行只取
        `detail["name"]`（`skill_usage.py:71`），塞数组它会整个 str() 成怪串、
        账照样记不上。所以这是「一行一个 name」而非「name: [...]」。
        """
        self.assertIn('"name": nm', RUNLOG_SRC,
                      "track 必须在 detail 里逐技能写 name 字段")
        self.assertRegex(RUNLOG_SRC, r'for\s+\w+\s*,\s*nm\s+in\s+enumerate\(names\)',
                         "必须逐名循环写多行，不能把名字塞成数组")

    def test_no_names_still_logs_one_row(self):
        """取不到名字时**仍要记一行**（带 q/limit 的普通埋点）。

        反面：只在有名字时才记 ⇒ 检索类端点一旦返回形状变了就彻底静默，
        连「端点被调用过」都查不到（判例 92的「假绿」族）。
        """
        self.assertRegex(RUNLOG_SRC, r"if not names:\s*\n\s*_fire\(",
                         "无名字时必须仍走一次 _fire，不能整条埋点消失")

    def test_name_has_length_cap(self):
        """技能名必须截断：detail 要进日志中心文本检索，无上限就是往库里灌长文本。"""
        self.assertRegex(RUNLOG_SRC, r"_NAME_MAX_CHARS\s*=\s*\d+")

    def test_list_imported(self):
        """`List` 必须在 typing 导入里 —— 漏导入是 NameError，
        而它发生在**函数被调用的那一刻**，注解里可能撑到运行时才炸。"""
        self.assertRegex(RUNLOG_SRC, r"from typing import[^\n]*\bList\b")


class TestZombieAlarmVisible(unittest.TestCase):
    """② 呈现层：`counted===0` 必须与「真的是 0」视觉分开。"""

    def test_no_ledger_banner_exists(self):
        self.assertIn("noLedger", JS_CODE, "缺 noLedger 分支")
        self.assertRegex(JS_CODE, r"noLedger\s*=\s*counted\s*===\s*0",
                         "noLedger 必须由 counted===0 驱动，不能由别的条件触发")

    def test_banner_warns_against_deletion(self):
        r"""横幅必须说清「不是零使用」+「据此删会误删」。

        钉死两个关键词是刻意的：只写「数据缺失」用户会问「那删哪些」，
        只写「不能删」又没解释原因 ⇒ 必须同时给**定性**与**后果**。

        ⚠️ 取样范围要放宽到整个 if 块：横幅文案在 `var(--warn)` **之后**，
        写成 `.{0,600}?var\(--warn\)` 会在颜色处停下、恰好取不到文案
        （本文件第一版就这么错的—— 断言范围比被测对象窄也会假红）。
        """
        m = re.search(r"if\s*\(\s*noLedger\s*\)\s*\{(.*?)\n\s*\}", JS_CODE, re.S)
        self.assertIsNotNone(m, "缺 if (noLedger) 分支")
        banner = m.group(1)
        self.assertIn("var(--warn)", banner,
                      "告警必须用 var(--warn)；用不存在的 var() 会静默退化成普通文字"
                      "（同 07-asset-panel.js:69-72 已记的坑）")
        self.assertIn("不能当", banner)
        self.assertIn("删", banner)

    def test_banner_uses_existing_css_var(self):
        """`--warn` 必须是主题里真存在的 token。"""
        theme = (_REPO / "static" / "hub" / "01-core-boot.js").read_text(encoding="utf-8")
        self.assertIn("--warn", theme, "主题里没有 --warn ⇒ var(--warn) 会静默退化")

    def test_counted_still_shown(self):
        """不得为了加横幅就把 `counted` 字段删掉 —— 它是后端自证「账空」的出口。

        断言的是**局部变量 counted**（从 d.counted 取来）被渲染进提示行，
        而不是字面 `(d.counted || 0)`：改用局部变量是为了让 noLedger 分支
        和提示行共用同一个值，字面量会随重构漂移。
        """
        self.assertRegex(JS_CODE, r"const counted\s*=\s*d\.counted\s*\|\|\s*0",
                         "counted 必须仍在提示行里显示；它是「账空 vs 真零」的唯一可断言信号")


class TestRedDirection(unittest.TestCase):
    """红向自证：把修复逐项撤掉，对应断言必须转红。

    没有这一节，上面的断言可能因写错而恒绿（判例 102 第五节的教训：
    **恒绿判据不算判据**，恒红也一样）。
    """

    def _assert_goes_red(self, mutated_src: str, pattern: str, why: str):
        self.assertNotRegex(mutated_src, pattern, why)

    def test_red_without_name_field(self):
        """撤掉 `"name": nm` ⇒ test_track_writes_name_per_row 应转红。"""
        mutated = RUNLOG_SRC.replace('"name": nm', '"skill_name": nm')
        self._assert_goes_red(mutated, r'"name": nm', "撤掉 name 后断言未转红")

    def test_red_without_bm25_branch(self):
        """撤掉 bm25 分支 ⇒ 真实返回形状取不到名 ⇒ 账仍恒空。"""
        mutated = RUNLOG_SRC.replace('data.get("bm25")', 'None')
        self._assert_goes_red(mutated, r'data\.get\("bm25"\)', "撤掉 bm25 后断言未转红")

    def test_red_without_single_object_branch(self):
        """撤掉单对象分支 ⇒ `/api/skill/read` 的形状取不到名 ⇒ reads 桶恒空。"""
        mutated = RUNLOG_SRC.replace('isinstance(data.get("name"), str)', "False")
        self._assert_goes_red(mutated, r'isinstance\(data\.get\("name"\), str\)',
                              "撤掉单对象分支后断言未转红")

    def test_red_without_no_ledger_branch(self):
        """撤掉 noLedger 横幅 ⇒ 账空时又与「真零」同形。"""
        mutated = JS_CODE.replace("noLedger", "neverSet")
        self._assert_goes_red(mutated, r"noLedger", "撤掉 noLedger 后断言未转红")


class TestNoRegressOnReadPath(unittest.TestCase):
    """不回归：`skill_usage.py` 的口径不许被这次改动顺手改掉。"""

    def test_second_source_still_declared_missing(self):
        """第二数据源**确实**仍未实现（各家直读磁盘）——
        这次补的是 hub 通道的名字，不是第二源。别把它悄悄改成 high。"""
        self.assertIn('DIRECT_SOURCE = "not-implemented"', USAGE_SRC)
        self.assertIn('"confidence": "medium"', USAGE_SRC)

    def test_never_auto_delete(self):
        """模块的「绝不自动删技能」原则必须还在（判例 103 的执行依据）。"""
        self.assertIn("绝不自动删技能", USAGE_SRC)
        self.assertNotIn("os.remove", USAGE_SRC, "僵尸榜模块不得含删除动作")


class TestInjectionDeclaresIdentity(unittest.TestCase):
    """注入链必须自报 `caller` 与 `via=hook`。

    没有这两个参数，`/relevant` 的埋点只能记成「一条没有身份的查询」：
    既答不出「哪个 agent 自动拿的候选」，也无法与人工查询分桶（runlog._mode_of
    只认 via=hook）。用户 2026-10-20 的原话：「agent 是否是在执行任务过程中
    自动调用技能，不是手动指定，这个也很重要」。
    """

    def test_claude_hook_declares_caller_and_via(self):
        src = (_REPO / "scripts" / "hub_skill_inject.py").read_text(encoding="utf-8")
        self.assertRegex(src, r'"caller"\s*:\s*CALLER')
        self.assertRegex(src, r'"via"\s*:\s*"hook"')
        self.assertRegex(src, r'CALLER\s*=\s*"claude"')


if __name__ == "__main__":
    unittest.main()