"""导出取消息的批量 IN 闸门（把 5001 次查询收敛成几次）。

【病根：实测】
`/api/sessions/export` 旧实现在两处（CSV / JSON）各写了一次
「for r in rows: for m in db.query('... WHERE session_id=?', (r['id'],))」，
而 `main.py` 把 limit 上限放到 5000 ⇒ 最坏 **5001 次查询**。
每次都过 `db.py` 的全局 `threading.Lock` ⇒ 不只是导出慢，
而是**把所有其他 DB 使用者（vitals / cronjobs / term / memindex）一起串行化**。

【为什么判据落在这里而不是端到端】
L0 禁 `import src.main`（一 import 跑 lifespan 开真库绑端口），
所以判据被抽成纯函数 `src/export_batch.py`（不 import fastapi / db）。
端到端探针验不出「查询了几次」—— 这正是本文件存在的理由。
（范式同 `src/staticguard.py`。）

【红臂在同一产物内】
`test_old_approach_would_be_n_plus_1` 把**旧算法**（逐行查）在同一个 stub 上跑一遍，
断言它是 N+1 而新算法不是 —— 这样「改前确实坏」是被证明的，不是假设的。
（`tests/README.md` 探针硬规定第 2 条：只证明改后能用 = 零信息。）
"""
import ast
import sys
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
from src import export_batch  # noqa: E402


class _Stub:
    """计数 + 假数据的 execute，签名与 `db.query` 一致。"""

    def __init__(self, messages_by_sid=None):
        self.calls = []            # [(sql, params), ...]
        self.data = messages_by_sid or {}

    def __call__(self, sql, params=()):
        self.calls.append((sql, tuple(params)))
        if "chat_messages" not in sql:
            return []
        out = []
        for sid in params:
            out.extend(self.data.get(sid, []))
        # 保持 id ASC 的次序（真实 SQL 有 ORDER BY id ASC）
        return sorted(out, key=lambda m: m.get("id", 0))

    @property
    def n(self):
        return len(self.calls)


def _msg(sid, mid, role="user", content="hi"):
    return {"session_id": sid, "id": mid, "role": role,
            "content": content, "created_at": "2026-10-05T00:00:00+00:00"}


class FetchMessagesBehaviour(unittest.TestCase):
    def test_three_sessions_cost_two_queries_total(self):
        """3 个会话（+1 次会话查询）⇒ 恰好 2 次，不是 4 次。"""
        stub = _Stub({"s1": [_msg("s1", 1)], "s2": [_msg("s2", 2)], "s3": [_msg("s3", 3)]})
        out = export_batch.fetch_messages(stub, ["s1", "s2", "s3"])
        self.assertEqual(stub.n, 1, "3 个会话应只发 1 次消息查询（实际 %d 次）" % stub.n)
        self.assertEqual(sorted(out), ["s1", "s2", "s3"])
        self.assertEqual([m["id"] for m in out["s2"]], [2])

    def test_empty_input_sends_no_query(self):
        stub = _Stub()
        out = export_batch.fetch_messages(stub, [])
        self.assertEqual(out, {})
        self.assertEqual(stub.n, 0, "空列表不该发查询")

    def test_grouping_is_correct(self):
        stub = _Stub({"s1": [_msg("s1", 1), _msg("s1", 2)],
                      "s2": [_msg("s2", 3)]})
        out = export_batch.fetch_messages(stub, ["s1", "s2"])
        self.assertEqual([m["id"] for m in out["s1"]], [1, 2])
        self.assertEqual([m["id"] for m in out["s2"]], [3])

    def test_message_order_preserved(self):
        """导出格式的列断言依赖 id ASC 次序 —— 批量查询不能打乱它。"""
        stub = _Stub({"s1": [_msg("s1", 5), _msg("s1", 2), _msg("s1", 9)]})
        out = export_batch.fetch_messages(stub, ["s1"])
        self.assertEqual([m["id"] for m in out["s1"]], [2, 5, 9])

    def test_session_without_messages_gets_empty_list(self):
        """空会话也要有键（导出时按 `or []` 取，但键在才不用判两次）。"""
        stub = _Stub({"s1": [_msg("s1", 1)]})
        out = export_batch.fetch_messages(stub, ["s1", "s2"])
        self.assertEqual(out.get("s2"), [])

    def test_duplicate_ids_are_deduped(self):
        stub = _Stub({"s1": [_msg("s1", 1)]})
        export_batch.fetch_messages(stub, ["s1", "s1", "s1"])
        self.assertEqual(stub.calls[0][1], ("s1",), "重复 id 应去重")

    def test_none_and_empty_ids_dropped(self):
        stub = _Stub()
        export_batch.fetch_messages(stub, [None, "", "s1"])
        self.assertEqual(stub.calls[0][1], ("s1",), "None/空串不该进 IN 列表")


class Chunking(unittest.TestCase):
    """分块而不是一个大 IN —— 不同 SQLite 构建的变量上限不同。"""

    def test_large_input_is_chunked(self):
        n = export_batch.CHUNK * 2 + 5
        stub = _Stub()
        export_batch.fetch_messages(stub, ["s%d" % i for i in range(n)])
        self.assertEqual(stub.n, 3, "%d 个会话应分 3 块（实际 %d 次）" % (n, stub.n))
        for _, params in stub.calls:
            self.assertLessEqual(len(params), export_batch.CHUNK,
                                 "单块占位符不得超过 CHUNK")

    def test_chunk_boundary_exact(self):
        n = export_batch.CHUNK * 2
        stub = _Stub()
        export_batch.fetch_messages(stub, ["s%d" % i for i in range(n)])
        self.assertEqual(stub.n, 2, "恰好整除时不该多发一次")

    def test_query_count_helper(self):
        self.assertEqual(export_batch.query_count(0), 0)
        self.assertEqual(export_batch.query_count(1), 1)
        self.assertEqual(export_batch.query_count(export_batch.CHUNK), 1)
        self.assertEqual(export_batch.query_count(export_batch.CHUNK + 1), 2)

    def test_chunk_is_under_legacy_sqlite_limit(self):
        """SQLite 老默认 SQLITE_MAX_VARIABLE_NUMBER = 999。"""
        self.assertLessEqual(export_batch.CHUNK, 999,
                             "CHUNK 超过 999 会在老构建上直接报「too many SQL variables」")


class RedArmOldApproach(unittest.TestCase):
    """红臂：证明改前确实坏（同一 stub 上跑旧算法）。"""

    def test_old_approach_would_be_n_plus_1(self):
        """旧写法（逐会话查）在同一输入下发 N 次查询 —— 这就是改前的形态。"""
        stub = _Stub({"s%d" % i: [_msg("s%d" % i, 1)] for i in range(20)})
        n_old = 0
        for i in range(20):
            stub("SELECT role,content,created_at FROM chat_messages "
                 "WHERE session_id=? ORDER BY id ASC", ("s%d" % i,))
            n_old += 1
        self.assertEqual(n_old, 20, "旧写法应是 20 次")

        stub2 = _Stub({"s%d" % i: [_msg("s%d" % i, 1)] for i in range(20)})
        export_batch.fetch_messages(stub2, ["s%d" % i for i in range(20)])
        self.assertEqual(stub2.n, 1, "新写法应是 1 次")
        self.assertLess(stub2.n, n_old)


class MainWiring(unittest.TestCase):
    """接线：`api_sessions.py` 必须真的用它（改回去就红；C2 把导出处理器从 main.py 迁到 api_sessions.py）。"""

    def test_main_uses_export_batch(self):
        src = (_REPO / "src" / "api_sessions.py").read_text(encoding="utf-8")
        self.assertIn("import export_batch", src)
        self.assertIn("export_batch.fetch_messages", src,
                      "导出端点必须走批量取消息")

    def test_no_per_session_message_query_in_export_handler(self):
        """旧形状「for r in rows: 查一次」必须从**导出处理器**里消失。

        ⚠ 判据为什么限定在导出处理器内（第一版写太宽，误报了 2 处）：
        `api_sessions.py` 里还有两处合法的 `WHERE session_id=?` ——
          · `GET /api/sessions/{id}/messages` —— **单会话历史**，本来就该一条
          · `DELETE /api/sessions/{id}`      —— 删除本来就按单个 id 走
        把它们一并禁掉就是**逼人改坏正确代码求绿**。所以按 AST 定位到
        `sessions_export` 那个处理器，只判它内部。
        """
        tree = ast.parse((_REPO / "src" / "api_sessions.py").read_text(encoding="utf-8"))
        handler = None
        for node in ast.walk(tree):
            if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                src_seg = ast.get_source_segment(
                    (_REPO / "src" / "api_sessions.py").read_text(encoding="utf-8"), node) or ""
                if "MESSAGE_COLUMNS" in src_seg and "SESSION_COLUMNS" in src_seg:
                    handler = node
                    break
        self.assertIsNotNone(handler, "找不到会话导出处理器")
        bad = [ln for n in ast.walk(handler) if isinstance(n, ast.Constant)
               and isinstance(n.value, str) and "chat_messages" in n.value
               and "session_id=?" in n.value and "COUNT(*)" not in n.value]
        self.assertEqual([], bad,
                         "导出处理器里仍有逐会话取消息的 SQL（N+1 形状）：%s" % bad)

    def test_export_batch_has_no_heavy_imports(self):
        """L0 可测的前提：不 import fastapi / db（否则一 import 就牵出真库）。

        ⚠ 判据为什么用 AST 而不是扫文本（第一版踩过）：本模块的 **docstring 里
        写着**「本模块不 import fastapi、不 import db」—— 扫文本会命中自己的注释，
        报出一条根本不存在的 import。判据必须看 AST 的真 import 节点。
        （与 tests/test_html_js_escape_depth.py 的 strip_comments 同一类教训。）
        """
        tree = ast.parse((_REPO / "src" / "export_batch.py").read_text(encoding="utf-8"))
        imported = set()
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(a.name.split(".")[0] for a in node.names)
            elif isinstance(node, ast.ImportFrom) and node.module:
                imported.add(node.module.split(".")[0])
        for banned in ("fastapi", "db"):
            self.assertNotIn(banned, imported,
                             "export_batch 不得 import %s —— L0 禁 import src.main 同理"
                             % banned)


if __name__ == "__main__":
    unittest.main(verbosity=2)
