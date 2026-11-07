"""L0 · 写端点鉴权闸门的判定与**清单覆盖**（空 HOME 可跑，零网络零服务）。

三层各守一种失效：
  A `decide()` 真值表      —— 守判定本身（含 fail-closed、双头名、query 口径、不回显凭据）
  B 路由清单覆盖           —— 守"漏一条"：src 下每条写路由必须被闸门覆盖或**显式豁免**；
                              反向还要守"豁免不许躺死条目"（白名单里有一条对不上任何路由＝永久盲点）
  C main.py 里真的注册了   —— 守"写对了模块没接上"（py_compile 与单测都抓不到这类，见 09-23 事故）
"""
import ast
import re
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import writeauth  # noqa: E402


def write_routes():
    """AST 扫全仓写类路由，返回 [(method, path, module)]。"""
    out = []
    for f in sorted(SRC.glob("*.py")):
        tree = ast.parse(f.read_text(encoding="utf-8"))
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                for d in node.decorator_list:
                    if (isinstance(d, ast.Call) and isinstance(d.func, ast.Attribute)
                            and d.func.attr in ("post", "put", "patch", "delete")):
                        path = d.args[0].value if d.args and isinstance(d.args[0], ast.Constant) else None
                        self_err = f"{f.name} 里 {node.name} 的路径不是字面量，闸门清单无法静态核对"
                        assert path is not None, self_err
                        out.append((d.func.attr.upper(), path, f.name[:-3]))
    return out


def covered(path):
    return writeauth.exempt_reason(path) is None


class ADecideTruthTable(unittest.TestCase):
    """2026-11-07 用户裁定后口径：准入只认「是否已登录」。
    凭据只有两种 —— 浏览器 Cookie 会话 / 机器 HTTP Basic；共享 token 兼容面已删除。"""

    def test_reads_never_gated(self):
        for m in ("GET", "HEAD", "OPTIONS"):
            self.assertEqual(writeauth.decide(m, "/api/memory/l2", False, True)[0], "allow")

    def test_write_without_identity_denied(self):
        v, r = writeauth.decide("PUT", "/api/memory/l3", False, True)
        self.assertEqual(v, "deny")
        self.assertIn("未登录", r)

    def test_write_with_identity_allowed(self):
        for m in ("POST", "PUT", "PATCH", "DELETE"):
            self.assertEqual(writeauth.decide(m, "/api/scan/run", True, True)[0], "allow", m)

    def test_deny_reason_never_echoes_credential(self):
        v, r = writeauth.decide("DELETE", "/api/jobs/x", False, True)
        self.assertEqual(v, "deny")
        self.assertNotIn("token", r.lower(), "拒绝原因不该再引导用户去用 token")
        self.assertNotIn("secret", r.lower())

    def test_users_db_unavailable_fails_closed(self):
        v, r = writeauth.decide("POST", "/api/memory/l1", False, False)
        self.assertEqual(v, "misconfig", "无法判定身份时必须是 503 而不是放行")

    def test_no_token_channel_left(self):
        """共享 token 通道必须彻底消失：模块内不得再有取头/读 env 口令的函数。"""
        for name in ("provided_token", "secrets_from_env", "credential_name", "TOKEN_HEADERS"):
            self.assertFalse(hasattr(writeauth, name), f"{name} 仍在 —— 兼容面没删干净")

    def test_exempt_prefixes_each_have_reason(self):
        for p, why in writeauth.EXEMPT_PREFIXES.items():
            self.assertTrue(why and len(why) > 12, f"{p} 的豁免理由没写清：{why!r}")


class BRouteInventory(unittest.TestCase):
    def test_every_write_route_is_covered_or_exempt(self):
        routes = write_routes()
        self.assertGreaterEqual(len(routes), 34, "写路由数比实测基线还少？清单被截断了")
        # 原来这里写了个"未覆盖的都必须在豁免里"的 for 循环 —— 那是同义反复
        # （covered() 的定义就是"不在豁免前缀里"），永远为真。换成一条硬比对：
        # 未覆盖集合 必须 **逐字等于** 命中豁免前缀的路由集合。
        # 豁免的每条是否**真自带鉴权**由 L2 探针逐条取证（tests/verify_write_gate.py），
        # 静态层只能保证"没有第三条路偷偷免检"。
        uncovered = [r for r in routes if not covered(r[1])]
        pre = tuple(writeauth.EXEMPT_PREFIXES)
        self.assertEqual(sorted(p for _, p, _ in uncovered),
                         sorted(p for _, p, _ in routes if p.startswith(pre)),
                         "未被闸门覆盖的路由与豁免名单不吻合")
        # 2026-11-07 C 档：设置写端点（term-token / model / github）已全部收回登录闸门，
        # 原 HUB_PASSCODE 自校验与对应豁免一并删除。豁免只剩 telemetry / login / logout
        # 三条，都是「不豁免就自锁死」；上限由 11 收紧为 3。
        self.assertLessEqual(len(uncovered), 3,
                             f"豁免口子涨到 {len(uncovered)} 条了，每加一条都要在这里交代清楚")

    def test_no_dead_exempt_entry(self):
        """白名单里躺一条对不上任何路由的前缀＝永久盲点（将来复用该前缀就静默免检）。"""
        routes = [p for _, p, _ in write_routes()]
        for pre in writeauth.EXEMPT_PREFIXES:
            self.assertTrue(any(r.startswith(pre) for r in routes),
                            f"豁免前缀 {pre} 当前匹配不到任何写路由，请删掉它")

    def test_telemetry_exempt_premise_holds_hook_enforces_auth(self):
        """豁免理由写的是「hook.py 自带方案」——必须真接上，否则豁免＝免检洞。

        2026-10-12 实测：`hook._check_auth` 只定义、全仓零调用 ⇒ 该豁免当时**没有依据**。
        """
        s = (SRC / "hook.py").read_text(encoding="utf-8")
        self.assertIn("def _check_auth", s, "方案函数没了，豁免必须重写理由")
        body = s[s.index("async def push_event"):]
        cut = body.find("\n@router.")
        if cut > 0:
            body = body[:cut]
        self.assertIn("_check_auth(request)", body,
                      "/telemetry/events/ 的豁免以 hook.py 自带鉴权为前提；push_event 必须调用 _check_auth")

    def test_term_control_plane_stays_gated(self):
        """P1-7 原先只在 handler 里守的两条，现在被中间件多守一层 —— 不许被误豁免。"""
        for p in ("/api/term/sessions", "/api/scan/run", "/mcp/call", "/api/memory/l3"):
            self.assertTrue(covered(p), f"{p} 不该出现在豁免名单里")


class CRegistration(unittest.TestCase):
    def test_gate_registered_in_main(self):
        s = (SRC / "main.py").read_text(encoding="utf-8")
        self.assertIn("from writeauth import write_gate", s, "没导入")
        self.assertIn('app.middleware("http")(write_gate)', s, "导入了但没注册进 app")

    def test_registration_after_rate_limit_so_gate_is_outermost(self):
        """Starlette 后注册＝最外层。闸门必须排在 api_rate_limit 之后，
           否则被拒请求先占限流预算、且顺序推断会随重构悄悄变。"""
        s = (SRC / "main.py").read_text(encoding="utf-8")
        # 上一版写的是 s.index("async def api_rate_limit")：子串匹配，把函数改名成
        # api_rate_limit_zz 也照样命中 ⇒ 顺序断言其实是空的（变异检验当场抓到）。
        # 现在要求真带左括号，并且比的是"定义位置 < 注册位置"这个**语义**顺序。
        d = re.search(r"async def api_rate_limit\(", s)
        self.assertIsNotNone(d, "限流中间件的定义形状变了，顺序断言无法定位")
        r = s.index('app.middleware("http")(write_gate)')
        self.assertLess(d.start(), r, "闸门注册排在限流之前了（Starlette 后注册＝最外层）")

    def test_acl_never_short_circuits_on_missing_agent(self):
        """mcpgw 的 ACL 绕过必须堵住：缺 agent_id 不许再 `return` 放行。"""
        s = (SRC / "mcpgw.py").read_text(encoding="utf-8")
        body = s[s.index("def _acl_check"):s.index("def _acl_check") + 900]
        self.assertNotIn("if not agent_id:\n        return", body, "agent_id 留空即免检的绕过又回来了")
        self.assertIn('"anon"', body, "缺省身份没折算成 anon")


if __name__ == "__main__":
    unittest.main(verbosity=2)
