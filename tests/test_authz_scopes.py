"""P0-1 · 端点 → scope 声明表的完整性与一致性。

守三件事：
  A 每条真实写路由都在表里（漏一条 = 将来 enforce 时静默放行）
  B 表里没有僵尸条目（路由删了表还在 = 永久多授权）
  C scope 取值合法、豁免路由口径与 writeauth 一致、副作用集合不漂移

本测试不碰数据库、不启服务、不改变鉴权行为。
"""
import ast
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import authz  # noqa: E402
import writeauth  # noqa: E402


def write_routes():
    """AST 扫全仓写路由，返回 [(module, method, path)]。"""
    out = []
    for f in sorted(SRC.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in ("post", "put", "patch", "delete")):
                        assert d.args and isinstance(d.args[0], ast.Constant), \
                            f"{f.name}:{node.name} 的路径不是字面量，无法静态核对"
                        out.append((f.stem, d.func.attr.upper(), d.args[0].value))
    return out


class ScopeTable(unittest.TestCase):
    def setUp(self):
        self.routes = write_routes()
        self.declared = set(authz.WRITE_ROUTE_SCOPES)

    def test_route_count_baseline(self):
        self.assertEqual(len(self.routes), 105,
                         "写路由数变了：新增/删除端点必须同步 authz.WRITE_ROUTE_SCOPES")

    def test_every_write_route_declared(self):
        missing = sorted(set(self.routes) - self.declared)
        self.assertEqual(missing, [], f"以下写路由没有 scope 声明：{missing}")

    def test_no_stale_declaration(self):
        stale = sorted(self.declared - set(self.routes))
        self.assertEqual(stale, [], f"以下声明对不上任何真实路由：{stale}")

    def test_scope_values_are_known(self):
        bad = {k: v for k, v in authz.WRITE_ROUTE_SCOPES.items() if v not in authz.SCOPES}
        self.assertEqual(bad, {}, f"未知 scope：{bad}")

    def test_side_effect_routes_declared_and_counted(self):
        self.assertEqual(len(authz.SIDE_EFFECT_ROUTES), 27,
                         "副作用端点基线是 27 条；增删必须同时改 docs 与本集合")
        self.assertLessEqual(authz.SIDE_EFFECT_ROUTES, self.declared,
                             "副作用端点里有未声明的路由")
        self.assertFalse(authz.SIDE_EFFECT_ROUTES & authz.EXEMPT_WRITE_ROUTES,
                         "豁免路由不该被标成副作用")

    def test_exempt_routes_match_writeauth(self):
        for key in authz.EXEMPT_WRITE_ROUTES:
            _, _, path = key
            self.assertIsNotNone(writeauth.exempt_reason(path),
                                 f"{key} 标为豁免，但 writeauth 不认：口径分叉")
        # 反向：writeauth 豁免的 3 条里，除了 telemetry 都是本表条目
        for key in self.declared:
            _, _, path = key
            if writeauth.exempt_reason(path):
                self.assertIn(key, authz.EXEMPT_WRITE_ROUTES,
                              f"{path} 被 writeauth 豁免，但表里没标 EXEMPT")

    def test_admin_scope_only_on_users_module(self):
        wrong = [k for k, v in authz.WRITE_ROUTE_SCOPES.items()
                 if v == "hub.user.admin" and k[0] != "users"]
        self.assertEqual(wrong, [], f"用户管理 scope 泄漏到别的模块：{wrong}")

    def test_lookup_helpers(self):
        self.assertEqual(authz.scope_for("memory", "put", "/api/memory/l3"),
                         "hub.memory.write")
        self.assertIsNone(authz.scope_for("memory", "PUT", "/api/nope"))
        self.assertTrue(authz.is_side_effect("api_system", "post", "/api/system/restart"))
        self.assertFalse(authz.is_side_effect("memory", "PUT", "/api/memory/l3"))


if __name__ == "__main__":
    unittest.main()
