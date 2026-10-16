"""L0 hermetic：`src/main.py` 模块级别名一致性闸门（2026-10-16，一次生产崩溃循环后补）。

【为什么会有这道闸门 —— 真实事故，不是假想】
用户报障原句：「agenthub 重启后未正常运行」。根因是一行**别名缺失**：

    src/main.py:67   import settings_bundle            # 绑定名 = settings_bundle
    src/main.py:858  app.include_router(settings_bundle_mod.router)   # 引用不存在的名字

⇒ 模块级 `NameError` ⇒ uvicorn 导入 app 即失败 ⇒ 单元是 `Restart=always` + `RestartSec=3s`，
把它放大成**崩溃循环**（探到时 `NRestarts=27`，修复前累计 56），3102 无任何监听。
引入提交 `a49ef8b`（v0.13.115）：同批新增 `resilience` + `settings_bundle` 两条 import，
后者漏写 `as`。

【为什么 1928 例 L0 与 prepush 六闸全绿却拦不住】
L0 铁律「**不 import `src.main`**」（见 `tests/README.md` 分层表；本仓 20+ 处测试
docstring 明写）。因为一 import 就跑 lifespan：开真库、起后台探针、绑端口。于是既有
闸门只**读 main.py 的文本**（`VERSION` 字面量、注释关键字），从不**求值**它 ⇒
模块级 `NameError` 对整道闸门不可见。这是「代码存在 ≠ 会被执行」的同族：
这里是「**代码可读 ≠ 代码可 import**」。

【覆盖什么 / 不覆盖什么（诚实声明）】
· 覆盖：模块级 `*_mod` 别名的**定义-使用一致性** —— 正是本次事故形态，也是本仓
  26 个 `*_mod` 别名里唯一漏写 `as` 的那个。判据用 AST，零求值、零副作用 ⇒ 能进 L0。
· 不覆盖：非 `_mod` 后缀的未定义名、运行期才暴露的错误、任何需要真求值的问题。
  想真覆盖只能起子进程 import —— 那会跑 lifespan，L0 不允许。故这是**窄判据**：
  只钉**已发生过**的形态，不冒充「能 import 就一定对」。
"""
from __future__ import annotations

import ast
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
MAIN_PY = REPO / "src" / "main.py"

#: 本仓约定：`import x as x_mod` / `from pkg import router as x_mod`（26 处如此）。
#: 闸门只认这个后缀，正因为它是本仓**约定俗成**的模块别名（非任意变量名）。
ALIAS_SUFFIX = "_mod"


def bound_names(tree: ast.AST) -> set:
    """模块里所有**被绑定**的名字（导入 / 赋值 / def / class / 形参 / except / for / with）。

    不求值、只读语法树 ⇒ 可以在 L0 里对一个「import 会起真服务」的模块用。
    """
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.asname or a.name.split(".")[0])
        elif isinstance(node, ast.ImportFrom):
            for a in node.names:
                out.add(a.asname or a.name)
        elif isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            out.add(node.name)
        elif isinstance(node, ast.Name) and isinstance(node.ctx, (ast.Store, ast.Del)):
            out.add(node.id)
        elif isinstance(node, ast.arg):
            out.add(node.arg)
        elif isinstance(node, ast.ExceptHandler) and node.name:
            out.add(node.name)
    return out


def unbound_alias_refs(src: str) -> list:
    """返回 `[(行号, 名字)]`：被**读取**、以 `_mod` 结尾、却不在任何绑定点的名字。"""
    tree = ast.parse(src)
    bound = bound_names(tree)
    bad = []
    for node in ast.walk(tree):
        if (isinstance(node, ast.Name) and isinstance(node.ctx, ast.Load)
                and node.id.endswith(ALIAS_SUFFIX) and node.id not in bound):
            bad.append((node.lineno, node.id))
    return sorted(set(bad))


class TestNoUnboundAliasInMain(unittest.TestCase):
    def test_every_mod_alias_reference_is_bound(self):
        """`src/main.py` 里每个 `*_mod` 引用都必须有绑定 —— 否则服务根本起不来。"""
        bad = unbound_alias_refs(MAIN_PY.read_text(encoding="utf-8"))
        self.assertEqual(
            bad, [],
            "src/main.py 引用了未绑定的 `*_mod` 名字（模块级 NameError ⇒ 服务起不来）：\n  "
            + "\n  ".join("L%d: %s" % (ln, nm) for ln, nm in bad)
            + "\n  改法：给 import 补 `as <名字>`（本仓 26 处别名一律 `import x as x_mod`），"
              "或把引用改成实际绑定名。")

    # ── 判据自证：光「现在绿」不够，得证明它**真的会红** ──────────────────
    def test_detector_flags_an_unbound_alias(self):
        """正样本：复刻本次事故的两行，判据必须报出来。"""
        src = ("import settings_bundle\n"
               "app.include_router(settings_bundle_mod.router)\n")
        self.assertEqual(unbound_alias_refs(src), [(2, "settings_bundle_mod")])

    def test_detector_accepts_the_bound_forms(self):
        """负样本：本仓两种合法写法都不得误报（假红的闸门比没有闸门更坏）。"""
        self.assertEqual(
            unbound_alias_refs("import settings_bundle as settings_bundle_mod\n"
                               "app.include_router(settings_bundle_mod.router)\n"), [])
        self.assertEqual(
            unbound_alias_refs("import settings_bundle\n"
                               "app.include_router(settings_bundle.router)\n"), [])

    def test_detector_ignores_names_bound_later_or_narrower(self):
        """模块级赋值 / def 形参也算绑定，避免把合法写法误报成缺陷。"""
        self.assertEqual(
            unbound_alias_refs("def f():\n"
                               "    return x_mod.router\n"
                               "x_mod = object()\n"), [])
        self.assertEqual(
            unbound_alias_refs("def f(y_mod):\n"
                               "    return y_mod.router\n"), [])

    def test_detector_ignores_lookalikes_in_strings_and_attributes(self):
        """只认 `ast.Name`：注释/字符串里的同名字符串、以及属性访问都不得被误报。"""
        self.assertEqual(unbound_alias_refs("# x_mod 只是注释\n"
                                            "S = 'x_mod'\n"
                                            "obj.x_mod = 1\n"), [])


if __name__ == "__main__":
    unittest.main()