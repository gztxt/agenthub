"""`/api/memory` 旧端点别名的 L0 闸门（PT-20261024-05 A2）。

## 在测什么

1. **带 query 不再 500**（本批修的真 bug）：
   `GET /api/memory?query=x&limit=5` 实测返
   `AttributeError: 'Query' object has no attribute 'split'`。
   根因：别名路由**位置传参** `search_memory(query, limit)`，而 search_memory 的
   签名是 `(request, q, limit, sources, episodic)` ⇒ limit 落到 q 位、
   **sources 落到 limit 位**，拿到 FastAPI 的 `Query(default=…)` 对象而非字符串。
   引入于 v0.13.27~29 加 request 参数时别名未同步。
   ⇒ 断言：转发必须是**关键字传参**，且必须显式带 `request`。
2. **不 query 时仍回 L1 列表**（别名的原语义没被改坏）：`{"memories": [...], "count": N}`。
3. **回归防线**：源码层断言别名路由里**不出现位置传参**
   （`search_memory(query, limit)` 这种形态再出现就直接红）——
   判据是「源码文本里 `search_memory(` 后紧跟的第一个 token 不是 `request=`/`q=`」。
   这条比行为断言更早失败：签名再变一次时，红在这里而不是红在生产。
4. **契约不漂**：`/api/memory` 与 `/api/memory/search` 是同一件事的两个入口，
   别名转发后必须带 `backends[]`（逐路 ok/count/ms）——即它转发的确实是联邦检索，
   不是偷偷退化成单路。

## 为什么用「源码断言」而不是只靠行为断言
行为断言需要真实 TDAI（:8420）才有意义，而 L0 是 hermetic（空 HOME、零外呼）。
签名走样的形态恰恰是「行为在有依赖时才炸」，纯行为断言在 L0 里会假绿。
⇒ 两层：L0 断言调用形态（源码级）+ L1/实盘断言行为（本次改动记录在案）。
"""
from __future__ import annotations

import ast
import pathlib
import re
import sys
import unittest

ROOT = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(ROOT / "src"))

MAIN_PY = ROOT / "src" / "main.py"
MEMORY_PY = ROOT / "src" / "memory.py"


def _alias_fn():
    """返回 AST 里的 get_memory_alias 节点。"""
    tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
    for node in tree.body:                      # 只看模块顶层
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_memory_alias":
            return node
    raise AssertionError("main.py 里找不到 get_memory_alias 函数")


def _alias_code() -> str:
    """别名路由的**代码**（去掉 docstring 与注释）——用于「实参形态」判据。

    为什么需要：docstring 里写着 `search_memory(query, limit)` 这段**反面示例**，
    用 `str.find` 找调用点会命中它（实测命中位置在 docstring 内，比真实调用早 461 字符）。
    ⇒ 用 AST 拿函数的 body，再 unparse 出纯代码。
    """
    fn = _alias_fn()
    # 去掉 docstring（函数体第一个 Expr 节点若是常量字符串）
    body = list(fn.body)
    if body and isinstance(body[0], ast.Expr) and isinstance(body[0].value, ast.Constant) \
            and isinstance(body[0].value.value, str):
        body = body[1:]
    mod = ast.Module(body=body, type_ignores=[])
    ast.fix_missing_locations(mod)
    return ast.unparse(mod)


def _alias_src() -> str:
    """取出 `/api/memory` 别名路由的源码片段。

    ⚠ 切片判据踩过两次坑：
    ① 不能按「下一个 `@app.` / `def `」正则切 —— 别名路由下面还有模块级赋值
       （`_embed_proxy = None`）与后续函数，按「下一个顶层 def」切会把半个模块吞进来。
    ② 不能用 `text.find("\\nasync def ", i)` —— main.py 的**注释块里**就有
       `\nasync def ` 字样（历史根因原文），find 命中的是注释里的那个位置。
    ⇒ 判据：**用 AST 定位**函数体（按函数名），再用行号回原文切。
    """
    text = MAIN_PY.read_text(encoding="utf-8")
    tree = ast.parse(text)
    fn = None
    for node in tree.body:                      # 只看模块顶层
        if isinstance(node, ast.AsyncFunctionDef) and node.name == "get_memory_alias":
            fn = node
            break
    assert fn is not None, "main.py 里找不到 get_memory_alias 函数"
    lines = text.split("\n")
    # 从装饰器行（含）到函数体末行
    start = min([d.lineno for d in fn.decorator_list] + [fn.lineno]) - 1
    return "\n".join(lines[start:fn.end_lineno])


class TestAliasCallShape(unittest.TestCase):
    """源码层：转发必须是关键字传参（签名走样的最早防线）。"""

    def test_alias_forwards_with_keyword_args(self):
        """转发调用必须是关键字传参（`request=` / `q=`）。"""
        src = _alias_src()
        # ⚠ 不能用 src.find("search_memory(") —— docstring 里就有这个字样
        #   （实测命中位置 338 落在 docstring 内，而真实调用在 799）。
        # ⇒ 从「去掉 docstring 的源码」里找。
        code = _alias_code()
        i = code.find("search_memory(")
        self.assertGreater(i, 0, "别名路由里找不到 search_memory 调用")
        tail = code[i + len("search_memory("):].lstrip()
        self.assertTrue(
            tail.startswith("request=") or tail.startswith("q="),
            "别名路由必须用关键字传参调 search_memory；实测形态：%r" % tail[:60],
        )

    def test_alias_passes_request_explicitly(self):
        """request 必须显式带 —— 漏了它 FastAPI 拿不到 Request 对象。"""
        src = _alias_src()
        self.assertIn("request=request", src)

    def test_no_positional_search_memory_call(self):
        """回归防线：任何位置传参调用 `search_memory(...)` 都判红。

        ⚠ 判据用 **AST** 而不是正则扫全文：正则会命中**本文件自己的 docstring**
        里那段「`search_memory(query, limit)`」示例文字（实测踩过：全文正则扫到
        1 处命中，正是 docstring 里的示例）。
        ⇒ 走 AST 找真实 Call 节点，看它的实参形态。
        """
        tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        bad = []
        for node in ast.walk(tree):
            if not isinstance(node, ast.Call):
                continue
            f = node.func
            name = getattr(f, "attr", None) or getattr(f, "id", None)
            if name != "search_memory":
                continue
            if node.args:                       # 有位置实参 ⇒ 判红
                bad.append((node.lineno, ast.dump(node)[:60]))
        self.assertEqual(
            [], bad,
            "main.py 里出现了 search_memory 的位置传参调用 %d 处（行 %s）；"
            "签名 (request, q, limit, sources, episodic) 变了就会炸"
            % (len(bad), [b[0] for b in bad]),
        )

    def test_search_memory_call_uses_all_keywords(self):
        """本仓的 search_memory 调用点必须**全部**是关键字传参。"""
        tree = ast.parse(MAIN_PY.read_text(encoding="utf-8"))
        calls = []
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                f = node.func
                name = getattr(f, "attr", None) or getattr(f, "id", None)
                if name == "search_memory":
                    calls.append(node)
        self.assertTrue(calls, "main.py 里没有 search_memory 调用点 —— alias 路由被删了？")
        for node in calls:
            self.assertEqual(
                [], node.args,
                "main.py L%d 的 search_memory 调用有位置实参" % node.lineno,
            )
            kws = {k.arg for k in node.keywords}
            self.assertIn("q", kws, "main.py L%d 的 search_memory 调用缺 q=" % node.lineno)

    def test_search_memory_signature_unchanged(self):
        """签名是本批的隐含前提：若上游再改签名，本文件须同步更新。"""
        text = MEMORY_PY.read_text(encoding="utf-8")
        tree = ast.parse(text)
        fn = None
        for node in ast.walk(tree):
            if isinstance(node, ast.AsyncFunctionDef) and node.name == "search_memory":
                fn = node
                break
        self.assertIsNotNone(fn, "memory.py 里找不到 search_memory")
        args = [a.arg for a in fn.args.args]
        for kw in ("request", "q", "limit", "sources", "episodic"):
            self.assertIn(kw, args, "search_memory 签名少了 %s（现有：%s）"
                          % (kw, args))
        # 参数顺序也是契约：request 必须在首位（FastAPI 靠它注入 Request）
        self.assertEqual("request", args[0],
                         "search_memory 的首个参数必须是 request（FastAPI 注入位）")


class TestAliasNoQueryPath(unittest.TestCase):
    """无 query 时仍回 L1 列表 —— 别名的原语义没被改坏。"""

    def test_source_keeps_memories_branch(self):
        src = _alias_src()
        self.assertIn("FROM memories WHERE status='active'", src)
        self.assertIn('"memories"', src)
        self.assertIn('"count"', src)

    def test_no_query_branch_is_before_forward(self):
        """「无 query 回列表」必须在转发之前，否则无 query 也会走联邦。"""
        code = _alias_code()
        i_branch = code.find("if not query")
        i_fwd = code.find("search_memory(")
        # ⚠ 用 `>= 0` 而不是 `> 0` —— 去掉 docstring 后 `if not query` 就在开头（位置 0），
        #   用 `> 0` 会把「分支存在且在最前」误判成「分支不存在」（实测踩过）。
        self.assertGreaterEqual(i_branch, 0, "找不到 if not query 分支")
        self.assertGreaterEqual(i_fwd, 0, "找不到 search_memory 转发调用")
        self.assertLess(i_branch, i_fwd, "转发发生在无 query 分支之前")


if __name__ == "__main__":
    unittest.main()
