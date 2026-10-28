"""L0 hermetic：dsh 工作区联动（src/dshws.py + term.py 的接线）。

钉三条不变量：
  ① **联动是 best-effort**：dsh web 没开 / 超时 / RPC 报错 / 地址形状不对，
     一律返回 False 且**不抛异常** —— 会话创建不能被联动拖垮；
  ② **只认显式选的目录**：create_session 里调用点必须同时满足
     `prof["id"] == "dsh"` 与 `req_cwd`（本机项目/GitHub 项目页的「选了项目」
     信号）；裸「新会话」用画像默认目录，不得顺手注册；
  ③ **cwd 只进 JSON body**：请求里出现 token/cwd 以外的可疑字段拼接一律红
     （与 TestCommandProvenance 同口径，防 RPC 面变成注入面）。

零网络：urllib 全部 mock；零宿主：不读 journalctl（web_url 显式注入）。
"""
import ast
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))
sys.path.insert(0, str(ROOT / "tests"))
os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import dshws                                    # noqa: E402
import term                                     # noqa: E402

TERMSRC = ROOT / "src" / "term.py"
WEB = "http://192.168.5.102:3080/?token=dummy-token-for-test"


class _FakeResponse:
    """opener.open 的返回：索引交换用 .read() -less 调用，RPC 要 context manager。"""

    def __init__(self, payload: bytes = b"{}"):
        self._payload = payload

    def read(self) -> bytes:
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class _FakeOpener:
    """记录两次 open 的 URL；RPC 那次按脚本返回。"""

    def __init__(self, rpc_payload: bytes):
        self.urls = []
        self._rpc_payload = rpc_payload

    def open(self, url, timeout=None):          # noqa: A002 - 与 urllib 同名
        target = getattr(url, "full_url", url)   # RPC 那步传的是 Request 对象
        self.urls.append(target)
        if "/api/" in target:
            return _FakeResponse(self._rpc_payload)
        return _FakeResponse(b"")               # ?token= 换 cookie 那步


def _rpc_envelope(ok: bool, value=None, code="x", message="m") -> bytes:
    import json
    if ok:
        return json.dumps({"type": "server-response", "rpcId": "r",
                           "result": {"ok": True, "value": value or {}}}).encode()
    return json.dumps({"type": "server-response", "rpcId": "r",
                       "result": {"ok": False, "error": {"code": code, "message": message,
                                                          "details": {}}}}).encode()


class TestSplitWebUrl(unittest.TestCase):
    def test_valid_with_token(self):
        base, tok = dshws._split_web_url(WEB)
        self.assertEqual(base, "http://192.168.5.102:3080")
        self.assertEqual(tok, "dummy-token-for-test")

    def test_no_token_returns_none(self):
        self.assertIsNone(dshws._split_web_url("http://192.168.5.102:3080/"))

    def test_non_http_returns_none(self):
        self.assertIsNone(dshws._split_web_url("ftp://192.168.5.102:3080/?token=a"))
        self.assertIsNone(dshws._split_web_url("not a url"))

    def test_base_path_preserved(self):
        base, _ = dshws._split_web_url("http://h:3080/sub/?token=a")
        self.assertEqual(base, "http://h:3080/sub")


class TestPostRpc(unittest.TestCase):
    def _run(self, payload: bytes):
        opener = _FakeOpener(payload)
        with mock.patch.object(dshws.urllib.request, "build_opener", return_value=opener):
            value = dshws._post_rpc("http://h:3080", "tok", "workspace/create",
                                    {"request": {"path": "/p"}})
        return opener, value

    def test_two_step_shape(self):
        opener, value = self._run(_rpc_envelope(True, {"workspace": {"path": "/p"}, "created": True}))
        # 第一步必须是 ?token= 换 cookie，第二步才是 RPC
        self.assertEqual(len(opener.urls), 2)
        self.assertIn("token=tok", opener.urls[0])
        self.assertTrue(opener.urls[1].endswith("/api/workspace/create"))
        self.assertEqual(value.get("created"), True)

    def test_rpc_failure_raises(self):
        with self.assertRaises(RuntimeError):
            self._run(_rpc_envelope(False, code="gateway/internal", message="boom"))


class TestEnsureWorkspace(unittest.TestCase):
    CWD = "/fs/1000/ftp/技术文档/agenthub"

    def test_happy_path(self):
        with mock.patch.object(dshws, "_post_rpc",
                               return_value={"workspace": {"path": self.CWD}, "created": True}) as m:
            self.assertTrue(dshws.ensure_workspace(self.CWD, WEB))
        m.assert_called_once()
        self.assertEqual(m.call_args[0][2], "workspace/create")
        self.assertEqual(m.call_args[0][3], {"request": {"path": self.CWD}})

    def test_connection_failure_returns_false(self):
        import urllib.error
        with mock.patch.object(dshws, "_post_rpc",
                               side_effect=urllib.error.URLError("refused")):
            self.assertFalse(dshws.ensure_workspace(self.CWD, WEB))

    def test_rpc_failure_returns_false(self):
        with mock.patch.object(dshws, "_post_rpc", side_effect=RuntimeError("boom")):
            self.assertFalse(dshws.ensure_workspace(self.CWD, WEB))

    def test_bad_json_returns_false(self):
        with mock.patch.object(dshws, "_post_rpc", side_effect=ValueError("bad json")):
            self.assertFalse(dshws.ensure_workspace(self.CWD, WEB))

    def test_tokenless_url_skips_http(self):
        with mock.patch.object(dshws, "_post_rpc") as m:
            self.assertFalse(dshws.ensure_workspace(self.CWD, "http://192.168.5.102:3080/"))
        m.assert_not_called()

    def test_url_lookup_from_profiles_when_not_given(self):
        with mock.patch.object(dshws, "_post_rpc", return_value={}) as m, \
                mock.patch("profiles._dsh_web_url", return_value=WEB):
            self.assertTrue(dshws.ensure_workspace(self.CWD))
        m.assert_called_once()

    def test_no_web_url_at_all_returns_false(self):
        with mock.patch.object(dshws, "_post_rpc") as m, \
                mock.patch("profiles._dsh_web_url", return_value=None):
            self.assertFalse(dshws.ensure_workspace(self.CWD))
        m.assert_not_called()

    def test_empty_cwd_returns_false(self):
        with mock.patch.object(dshws, "_post_rpc") as m:
            self.assertFalse(dshws.ensure_workspace("", WEB))
        m.assert_not_called()


class TestTermWiring(unittest.TestCase):
    """create_session 里的联动接线：存在、且判据是 dsh + req_cwd。"""

    @classmethod
    def setUpClass(cls):
        tree = ast.parse(TERMSRC.read_text(encoding="utf-8"))
        cls.fn = None
        for n in ast.walk(tree):
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == "create_session":
                cls.fn = n
        assert cls.fn is not None, "create_session 不见了？"

    def _call_src(self) -> str:
        return ast.unparse(self.fn)

    def test_ensure_workspace_called_in_create_session(self):
        src = self._call_src()
        self.assertIn("dshws.ensure_workspace", src,
                      "create_session 里没有 dshws.ensure_workspace 调用（联动被删了？）")

    def test_gate_is_dsh_and_req_cwd(self):
        src = self._call_src()
        self.assertIn("prof['id'] == 'dsh' and req_cwd", src,
                      "联动判据必须是 prof id==dsh 且 req_cwd（显式选目录才注册）")

    def test_call_is_offloaded_to_thread(self):
        src = self._call_src()
        self.assertIn("asyncio.to_thread(dshws.ensure_workspace", src,
                      "联动 HTTP 必须走 asyncio.to_thread（不占事件循环）")

    def test_no_other_module_imports_dshws_at_import_time(self):
        """dshws 只被 term.py 依赖（单一接线点）；多处接线必漂移。

        判据取 **AST import 节点**而非全文 grep：版本注释/文档里提模块名是正常
        记载（main.py 的 v0.13.148 注释块就写了模块路径），不算接线；只有真的
        `import dshws`（含函数内延迟导入）才是耦合点。
        """
        import ast
        hits = []
        for py in (ROOT / "src").glob("*.py"):
            if py.name in ("dshws.py",) or py.name.endswith(".bak"):
                continue
            try:
                tree = ast.parse(py.read_text(encoding="utf-8"))
            except (OSError, SyntaxError):
                continue
            for node in ast.walk(tree):
                if isinstance(node, ast.Import) and any(
                    a.name == "dshws" for a in node.names
                ):
                    hits.append(py.name)
                elif isinstance(node, ast.ImportFrom) and node.module == "dshws":
                    hits.append(py.name)
        self.assertEqual(hits, ["term.py"], f"dshws 被多处 import：{hits}")


if __name__ == "__main__":
    unittest.main()
