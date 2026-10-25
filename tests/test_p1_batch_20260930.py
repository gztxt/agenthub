"""L0 hermetic：2026-09-30 P1 修复批的行为锁（PT-20260930-01）。

覆盖本批 8 项后端修复中**可离线断言**的部分。每条都写明「原症状」，
避免后来者只看到断言看不到为什么要它：

  P1-1  vitals._save 固定名 .json.tmp ⇒ 并发 sweep 互踩，落盘 JSON 偶发残缺
  P1-2  cronjobs 串行 await _fire ⇒ 一个 job 抛错拖住本轮其余 job，且异常无日志
  P1-3  db.execute_script 不 commit ⇒ 脚本执行结果对后续连接/重启不可见
  P1-4  mcpgw 加 ACL 时 server 不存在 ⇒ [0] IndexError 500（语义应是 404）
  P1-5  main.py 裸 create_task 不持引用 ⇒ 可被 GC 回收且 shutdown 无法 cancel
  P1-9  memory 检索 LIKE 未转义 ⇒ 查 "50%" 匹配全表、查 "a_b" 命中 "axb"
  P1-10 resources 每次遍历 __import__("re").compile ⇒ 纯浪费（改为 lru_cache）
  P1-11 CORS 默认值写死内网 IP ⇒ 换网静默失效（改为 env 驱动 + 仅回环默认）
"""
import ast
import re
import sys
import unittest
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO / "src"))
sys.path.insert(0, str(REPO))
import tiers  # noqa: E402,F401  （L0 档位登记）


def _src(name: str) -> str:
    return (REPO / "src" / name).read_text(encoding="utf-8")


class TestP1VitalsSave(unittest.TestCase):
    def test_tmp_name_is_per_thread_not_fixed(self):
        s = _src("vitals.py")
        self.assertNotIn('STATE_PATH.with_suffix(".json.tmp")', s,
                         "固定名 tmp 仍在：并发 sweep 会互相 truncate/write")
        self.assertIn("get_ident()", s, "tmp 名应带线程 id")
        self.assertIn("os.replace(tmp, STATE_PATH)", s, "落盘仍须走 os.replace 原子替换")

    def test_replace_still_under_guard(self):
        """tmp 名唯一化之后，原子性完全依赖 os.replace，不能被改回 write_text(STATE_PATH)"""
        s = _src("vitals.py")
        self.assertNotRegex(s, r"write_text\([^)]*STATE_PATH\s*[,)]",
                            "直接写目标文件会失去原子性")


class TestP1CronjobsIsolate(unittest.TestCase):
    def test_fire_uses_gather_with_return_exceptions(self):
        s = _src("cronjobs.py")
        self.assertIn("asyncio.gather", s, "触发应并发且异常隔离")
        self.assertIn("return_exceptions=True", s, "一个 job 失败不许连坐其它 job")
        self.assertNotRegex(s, r"^\s*await _fire\(job\)\s*$",
                            "串行 await 仍在：一个 job 抛错会拖住本轮其余调度")

    def test_failure_is_logged(self):
        s = _src("cronjobs.py")
        self.assertRegex(s, r"\[cron\].*失败", "失败必须留日志，否则事后无从定位是哪个 job 没跑")


class TestP1DbExecuteScript(unittest.TestCase):
    def test_commits(self):
        s = _src("db.py")
        body = s.split("def execute_script", 1)[1].split("\ndef ", 1)[0]
        self.assertIn("commit()", body, "execute_script 不 commit ⇒ 脚本效果对后续连接/重启不可见")


class TestP1McpgwAcl404(unittest.TestCase):
    def test_empty_rows_raises_404_not_indexerror(self):
        s = _src("mcpgw.py")
        seg = s.split("async def add_acl", 1)[1].split("\n@router", 1)[0]
        self.assertIn("HTTPException(404", seg, "server 不存在应是 404，不是 [0] 的 IndexError→500")
        self.assertNotIn('(body.server_id, body.server_id))[0]["id"]', seg,
                         "空列表 [0] 仍在：会抛 IndexError")

    def test_http_exception_imported(self):
        s = _src("mcpgw.py")
        m = re.search(r"^from fastapi import (.+)$", s, re.M)
        self.assertIsNotNone(m)
        self.assertIn("HTTPException", m.group(1))


class TestP1BackgroundTaskRefs(unittest.TestCase):
    """P1-5：裸 create_task 不持强引用 ⇒ 可被 GC 静默回收。"""

    def test_no_bare_create_task_in_startup(self):
        tree = ast.parse(_src("main.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "startup")
        bare = [n for n in ast.walk(fn) if isinstance(n, ast.Call)
                and isinstance(n.func, ast.Attribute) and n.func.attr == "create_task"]
        self.assertEqual(bare, [], "startup 里仍有裸 asyncio.create_task：应走 _spawn()")

    def test_spawn_keeps_strong_ref(self):
        s = _src("main.py")
        self.assertRegex(s, r"def _spawn\(.*?\n(?:.*\n)*?\s*_bg_tasks\.add\(t\)",
                         "_spawn 必须把 task 存进强引用集合")

    def test_shutdown_cancels_background_tasks(self):
        tree = ast.parse(_src("main.py"))
        fn = next(n for n in ast.walk(tree)
                  if isinstance(n, (ast.AsyncFunctionDef, ast.FunctionDef)) and n.name == "shutdown")
        src = ast.unparse(fn)
        self.assertIn("_bg_tasks", src, "shutdown 必须收掉后台任务，否则退出要等事件循环超时")
        self.assertIn("cancel()", src)


class TestP1MemoryLikeEscape(unittest.TestCase):
    """P1-9：LIKE 的 % / _ 未转义 ⇒ 查询词与结果无关。

    v0.13.160 起转义逻辑抽成 `_like_pat()`（两个入口共用，避免再次分叉），断言跟着
    落点走：① 转义本体在 helper 里；② **两个**调用点都真的走 helper 且 SQL 带 ESCAPE。
    """

    def test_helper_escapes_wildcards(self):
        s = _src("memory.py")
        seg = s.split("def _like_pat", 1)[1].split("\ndef ", 1)[0]
        self.assertRegex(seg, r'replace\("%"', "% 未转义")
        self.assertRegex(seg, r'replace\("_"', "_ 未转义")
        # 反斜杠本身必须先转义，否则 50\% 会被二次转义吃掉
        self.assertIn('q.replace("\\\\", "\\\\\\\\")', seg, "反斜杠未先转义")

    def test_both_entrypoints_use_helper_with_escape_clause(self):
        s = _src("memory.py")
        for fn in ("def _local_l1", "async def list_l1"):
            self.assertIn(fn, s)
            seg = s.split(fn, 1)[1].split("\ndef ", 1)[0].split("\nasync def ", 1)[0]
            self.assertIn("_like_pat(", seg, f"{fn} 未走 _like_pat()（口径会再次分叉）")
            self.assertIn("ESCAPE", seg, f"{fn} 必须显式声明 ESCAPE 字符")


class TestP1ResourcesReCache(unittest.TestCase):
    def test_no_inline_re_import_compile(self):
        # 剔除注释与字符串字面量后再判：本文件 docstring 里为解释成因引用了原写法，
        # 那是历史说明不是活代码，混进来会让断言永真。
        tree = ast.parse(_src("resources.py"))
        lit = {n.value for n in ast.walk(tree)
               if isinstance(n, ast.Constant) and isinstance(n.value, str)}
        code = _src("resources.py")
        for frag in lit:
            code = code.replace(frag, "")
        self.assertNotIn('__import__("re").compile', code, "热循环里逐次 compile 仍在")

    def test_proc_re_is_cached(self):
        s = _src("resources.py")
        m = re.search(r"@lru_cache[^\n]*\ndef _proc_re\(rx: str\):\n(?:.*\n)*?\s*return re\.compile\(rx\)", s)
        self.assertIsNotNone(m, "_proc_re 应是 lru_cache 包装的预编译入口")

    def test_both_match_sites_use_helper(self):
        s = _src("resources.py")
        self.assertEqual(s.count("pat = _proc_re(rx)"), 2,
                         "采集侧与 kill 侧两处都应改用预编译入口")


class TestP1CorsDefault(unittest.TestCase):
    """P1-11：默认值写死内网 IP，换网后 CORS 静默失效。"""

    def test_default_has_no_lan_ip(self):
        s = _src("main.py")
        seg = s.split("def _parse_cors_origins", 1)[1].split("\n\n\n", 1)[0]
        self.assertNotRegex(seg, r"192\.168\.\d+\.\d+", "默认值里仍有硬编码 LAN IP")
        self.assertNotRegex(seg, r"100\.\d+\.\d+\.\d+", "默认值里仍有硬编码 Tailscale IP")

    def test_default_is_loopback_only(self):
        s = _src("main.py")
        seg = s.split("def _parse_cors_origins", 1)[1].split("\n\n\n", 1)[0]
        self.assertIn("CORS_ORIGINS", seg, "须 env 驱动")
        self.assertIn("127.0.0.1:3102", seg, "回环默认须保留")
        import re as _re
        default = _re.search(r'os\.getenv\("CORS_ORIGINS",\s*"([^"]*)"\)', seg)
        self.assertIsNotNone(default, "找不到默认值字面量")
        self.assertNotIn("*", default.group(1),
                         "allow_credentials=True 时通配来源会被浏览器直接拒绝")

    def test_fallback_still_loopback(self):
        """解析失败时退化为回环（既有行为，本批只改默认值，不能顺手放松）"""
        s = _src("main.py")
        self.assertIn("退化为仅回环来源", s)


if __name__ == "__main__":
    unittest.main()
