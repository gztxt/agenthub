"""回归测（P0-1）：handler 里 `req.<attr>` 必须是该 Pydantic 模型已声明的字段。

缺陷出处（实测取证，不是推断）：
  commit 2cf96fa（v0.10.0，2026-09-20 20:54「界面统一」）把 `tools` / `repair_mode`
  两个字段从 ChatRequest 删掉，但 `_chat_dispatch` 的调用点仍写 `req.tools`
  ⇒ 每次 POST /api/agents/{id}/chat 必 500（AttributeError），
     而 /health 全程 200、vitals 全绿 —— 直连对话框静默不可用三天无人发现。

本测用 AST 静态取证，**不导入 src.main**：导入会触发 lifespan（写生产库 / 起后台
探针任务 / 拉起 embed 代理），单元测试不能碰这些。

跑法：cd ~/agent-hub && venv/bin/python -m unittest tests.test_pydantic_attr_drift -v
"""
import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# pydantic BaseModel 自带成员，不算「字段」，允许 handler 直接调
BASEMODEL_MEMBERS = {
    "dict", "json", "copy", "schema", "schema_json", "construct", "fields",
    "model_dump", "model_dump_json", "model_json_schema", "model_config",
    "model_fields", "model_computed_fields", "model_post_init", "model_rebuild",
    "parse_obj", "parse_raw", "parse_file", "from_orm", "update_forward_refs",
}


def _basemodel_fields(tree: ast.Module) -> dict:
    """收集本模块内直接继承 BaseModel 的类 → 声明字段名集合。"""
    out = {}
    for node in ast.walk(tree):
        if not isinstance(node, ast.ClassDef):
            continue
        bases = {getattr(b, "id", getattr(b, "attr", "")) for b in node.bases}
        if "BaseModel" not in bases:
            continue
        fields = set()
        for stmt in node.body:
            if isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name):
                fields.add(stmt.target.id)
        out[node.name] = fields
    return out


def _first_attr_chain(node):
    """返回 (根变量名, 第一级属性名)，仅当形如 Name.attr 时。"""
    if isinstance(node, ast.Attribute) and isinstance(node.value, ast.Name):
        return node.value.id, node.attr
    return None, None


class TestAttrDrift(unittest.TestCase):
    def _violations(self):
        bad = []
        for f in sorted(SRC.rglob("*.py")):          # rglob 天然不含 *.bak-*（后缀不是 .py）
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            models = _basemodel_fields(tree)
            if not models:
                continue
            for fn in [n for n in ast.walk(tree) if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))]:
                # 找该函数里注解为本地 BaseModel 子类的参数
                for arg in list(fn.args.posonlyargs) + list(fn.args.args) + fn.args.kwonlyargs:
                    ann = getattr(arg.annotation, "id", None)
                    if ann not in models:
                        continue
                    for node in ast.walk(fn):
                        var, attr = _first_attr_chain(node)
                        if var != arg.arg or attr is None:
                            continue
                        if attr in models[ann] or attr in BASEMODEL_MEMBERS:
                            continue
                        bad.append(f"{f.relative_to(SRC.parent)}:{node.lineno} "
                                   f"{fn.name}: {ann} 未声明字段 `{attr}`")
        return bad

    def test_no_undeclared_attribute_access(self):
        bad = self._violations()
        self.assertEqual([], bad, "handler 访问了模型未声明的字段（运行期必 AttributeError→500）：\n"
                                  + "\n".join(bad))

    def test_known_buggy_pattern_is_gone(self):
        """把这次的事故形状单独钉死：ChatRequest 上没有 tools/repair_mode，
        且 `_chat_dispatch` 的**调用点**不得再传这两个 kwarg。
        只认 AST —— 用子串查会在解释性注释上误报（本仓注释里必须写 req.tools 才能说清教训）。

        2026-10-12 C2：ChatRequest / `_chat_dispatch` 已从 main.py 迁到 `src/api_agents.py`
        ⇒ 本测改为**扫全 src**，并加一条「必须找到调用点」的反向断言 —— 否则搬家后
        守卫会静默变成空断言（本仓「文字存在≠已生效」家族）。"""
        fields: set = set()
        calls: list = []
        for f in sorted(SRC.rglob("*.py")):
            tree = ast.parse(f.read_text(encoding="utf-8"), filename=str(f))
            fields |= _basemodel_fields(tree).get("ChatRequest", set())
            rel = f.relative_to(SRC.parent)
            calls += [(rel, c) for c in ast.walk(tree)
                      if isinstance(c, ast.Call) and getattr(c.func, "id", None) == "_chat_dispatch"]
        self.assertTrue(fields, "没解析到 ChatRequest 的字段（模型改名/搬家了？）")
        self.assertNotIn("tools", fields, "ChatRequest 又声明了 tools —— 与调用点必须同步")
        self.assertNotIn("repair_mode", fields)
        self.assertTrue(calls, "没找到 _chat_dispatch 的调用点（改名/搬家了？守卫会静默失效）")
        for rel, call in calls:
            passed = {kw.arg for kw in call.keywords}
            self.assertNotIn("tools", passed,
                             f"{rel}:{call.lineno} 调用点又传 tools= —— 运行期必 500")
            self.assertNotIn("repair_mode", passed,
                             f"{rel}:{call.lineno} 调用点又传 repair_mode= —— 运行期必 500")


if __name__ == "__main__":
    unittest.main(verbosity=2)
