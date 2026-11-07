"""L0 hermetic：终端拉起面的安全闸（src/term.py + sessions_store.resume_argv）。

这组断言钉的是本项目最要命的一条不变量：
    **客户端永远只能给 agent_id / session_id / cwd，命令只出自画像白名单 + 后端模板。**
docstring 第 4-5 行写着这条，但注释不算判据 —— 今天它可测了。

v0.13.30 增补：cwd 进白名单。安全论证（写死在此，改前先读）：
    body.cwd 只进 Session 子进程的 os.chdir，**绝不进命令拼装**（cmd 仍只出自
    resume_argv / shlex.split(画像)——TestCommandProvenance 两条钉子原样钉着）；
    已持 TERM_TOKEN 的用户本可用 shell 画像（terminal.cmd=bash）cd 到任意目录，
    ⇒ 传 cwd 不构成权限升级。所有读 body.cwd 的代码必须包在 _cwd_or_none(...)
    调用内（绝对路径 + 实盘存在 + 可疑字符三重校验）——TestCwdGate 钉着。

不 fork pty、不起服务、不连网：只用 AST 看源码形状 + 调纯函数。
"""
import ast
import os
import sys
import types
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))        # 与 src/*.py 内部的裸 import 保持一致（见 test_term_reaper）
sys.path.insert(0, str(ROOT / "tests"))      # tiers.py
os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import sessions_store                         # noqa: E402
import term                                   # noqa: E402
import tiers                                  # noqa: E402,F401  (确保分层模块可导入且无副作用)

TERMSRC = ROOT / "src" / "term.py"


def _func(tree, name):
    for n in ast.walk(tree):
        if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef)) and n.name == name:
            return n
    raise AssertionError(f"src/term.py 里找不到函数 {name}（被重构掉了？那这条判据要同步改）")


class TestCreateInShape(unittest.TestCase):
    """请求模型字段清单：多一个能传命令的字段，这条立刻红"""

    FORBIDDEN = ("cmd", "command", "argv", "args", "shell", "binary", "exe", "program", "script")

    def test_only_id_bearing_fields(self):
        names = set(term.CreateIn.model_fields)
        self.assertTrue(names, "CreateIn 没有字段了？")
        bad = {n for n in names if any(t in n.lower() for t in self.FORBIDDEN)}
        self.assertEqual(bad, set(), f"CreateIn 出现了命令类字段 {bad}：等于把 pty 变成 RCE 入口")
        self.assertTrue({"agent_id"} & names, "agent_id 必须还在")

    def test_session_id_still_optional(self):
        f = term.CreateIn.model_fields["session_id"]
        self.assertTrue(f.annotation is not None)


class TestCommandProvenance(unittest.TestCase):
    """create_session 里 cmd 的来源必须只有两处：画像白名单 / 后端模板"""

    @classmethod
    def setUpClass(cls):
        cls.fn = _func(ast.parse(TERMSRC.read_text(encoding="utf-8")), "create_session")

    def _assignments(self):
        out = []
        for n in ast.walk(self.fn):
            if isinstance(n, ast.Assign):
                targets = n.targets
            elif isinstance(n, ast.AnnAssign):
                targets = [n.target]
            else:
                continue
            names = [t.id for t in targets if isinstance(t, ast.Name)]
            if "cmd" in names and getattr(n, "value", None) is not None:
                out.append(n)
        return out

    def test_cmd_value_shape_comes_only_from_whitelist_or_template(self):
        """cmd 的每一次赋值，右值只能是：
             ① sessions_store.resume_argv(...)  —— 后端模板（内部再过形状正则 + 实盘双校验）
             ② shlex.split(<画像里的命令串>)      —— 且入参不得引用 body
           出现任何其他来源（尤其直接从 body 拿）即红。"""
        self.assertGreaterEqual(len(self._assignments()), 1, "没找到 cmd 赋值，判据已失效")
        for a in self._assignments():
            v = a.value
            self.assertIsInstance(v, ast.Call,
                                  f"第 {a.lineno} 行 cmd 右值不是函数调用，来源不明：{ast.dump(v)[:80]}")
            fn = v.func
            attr = getattr(fn, "attr", None)
            self.assertIn(attr, ("resume_argv", "split"),
                          f"第 {a.lineno} 行 cmd 来自未预期函数 {attr}")
            if attr == "split":
                for sub in ast.walk(v):
                    if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                            and sub.value.id == "body":
                        self.fail(f"第 {a.lineno} 行把 body.{sub.attr} 当命令串去 split 了")

    def test_only_id_bearing_body_fields_are_read(self):
        """整个 create_session 能读到的请求字段，只许 agent_id / session_id / cwd。
           cwd（v0.13.30 本机项目页）只进 os.chdir 不进命令拼装，且必须过
           _cwd_or_none 校验（见 TestCwdGate）；多读任何其他字段就红。"""
        read = set()
        for sub in ast.walk(self.fn):
            if isinstance(sub, ast.Attribute) and isinstance(sub.value, ast.Name) \
                    and sub.value.id == "body":
                read.add(sub.attr)
        self.assertTrue(read, "create_session 不再读 body 任何字段？判据需复核")
        self.assertLessEqual(read, {"agent_id", "session_id", "cwd"},
                             f"读到了多余字段 {read - {'agent_id', 'session_id', 'cwd'}}")

    def test_no_subprocess_in_module_outside_session_class(self):
        """term.py 只应有一处进程拉起（Session.__init__ 的 fork+execvpe）。
           出现 subprocess/os.system/eval 说明有人抄近路绕开 pty 白名单。"""
        tree = ast.parse(TERMSRC.read_text(encoding="utf-8"))
        hits = []
        for n in ast.walk(tree):
            if isinstance(n, ast.Call):
                f = n.func
                nm = getattr(f, "attr", None) or (getattr(f, "id", "") if isinstance(f, ast.Name) else "")
                if nm in {"system", "popen", "run", "Popen", "check_call",
                          "check_output", "eval", "exec", "spawnl", "spawnv"}:
                    hits.append(f"{nm}@{n.lineno}")
        self.assertEqual(hits, [], f"term.py 出现第二条拉起路径：{hits}")


class TestCwdGate(unittest.TestCase):
    """v0.13.30 body.cwd 的两道闸：
    ① create_session 里读 body.cwd 必须包在 _cwd_or_none(...) 里（防绕过校验直用）；
    ② _cwd_or_none 纯函数：空→None、相对/不存在/可疑字符→ValueError。"""

    def test_body_cwd_reads_wrapped_in_guard_call(self):
        """AST：create_session 内每一个 body.cwd 属性访问，其父链必须落在
           _cwd_or_none(…) 的调用实参里。出现裸 `body.cwd` 即红。"""
        fn = _func(ast.parse(TERMSRC.read_text(encoding="utf-8")), "create_session")
        guarded_positions = set()
        for n in ast.walk(fn):
            if isinstance(n, ast.Call) and getattr(n.func, "id", None) == "_cwd_or_none":
                for sub in ast.walk(n):
                    if isinstance(sub, ast.Attribute) and sub.attr == "cwd" \
                            and isinstance(sub.value, ast.Name) and sub.value.id == "body":
                        guarded_positions.add((sub.lineno, sub.col_offset))
        bare = []
        for n in ast.walk(fn):
            if isinstance(n, ast.Attribute) and n.attr == "cwd" \
                    and isinstance(n.value, ast.Name) and n.value.id == "body":
                if (n.lineno, n.col_offset) not in guarded_positions:
                    bare.append(f"@{n.lineno}:{n.col_offset}")
        self.assertEqual(bare, [], f"body.cwd 存在校验函数外的裸读取 {bare}：等于把 chdir 面敞开")

    def test_guard_fn_exists_and_pure_shape(self):
        self.assertTrue(hasattr(term, "_cwd_or_none"), "_cwd_or_none 被改名/删除？判据需同步")
        for blank in (None, "", "   "):
            self.assertIsNone(term._cwd_or_none(blank))
        import tempfile, pathlib
        with tempfile.TemporaryDirectory() as td:
            self.assertEqual(term._cwd_or_none(td), td)
            with self.assertRaises(ValueError):
                term._cwd_or_none(str(pathlib.Path(td) / "nope"))
        with self.assertRaises(ValueError):
            term._cwd_or_none("relative/path")
        for bad in ("/a;b", "/a|b", "/a&b", "/a$b", "/a`b", "/a\nb", "/a\x00b"):
            with self.assertRaises(ValueError):
                term._cwd_or_none(bad)


class TestTermAuthGate(unittest.TestCase):
    """_check_term_auth：没有 Cookie/Basic 身份必须 401；共享 token 面已删除。"""

    def test_no_credential_is_401(self):
        req = types.SimpleNamespace(headers={})
        with self.assertRaises(Exception) as cm:
            term._check_term_auth(req, "unit")
        self.assertEqual(getattr(cm.exception, "status_code", None), 401)

    def test_identity_passes(self):
        req = types.SimpleNamespace(headers={"authorization": "Basic bDAtdGVzdDp4"})
        term._check_term_auth(req, "unit")     # 不抛即通过

    def test_shared_token_channel_is_gone(self):
        """兼容面确已删除：函数与 query 入口都不许回来。"""
        self.assertFalse(hasattr(term, "_check_term_token"),
                         "_check_term_token 又回来了 —— 共享 token 面没删干净")
        src = TERMSRC.read_text(encoding="utf-8")
        self.assertNotIn("x-term-token", src.lower(),
                         "term.py 仍出现 x-term-token")
        self.assertNotIn("hmac.compare_digest", src,
                         "term.py 仍在做共享 token 比较")


class TestResumeArgvHostile(unittest.TestCase):
    """六家 agent 的 id 注入面：形状正则 + 实盘存在双校验，一道都不许松"""

    HOSTILE = ["x; rm -rf /", "--resume=evil", "$(id)", "`id`", "../etc/passwd",
               "a" * 300, "", " ", "\n", "01a0c91d-eb84-7130-9767-479821ef336c; echo",
               "session_x' or '1'='1"]

    def test_every_agent_rejects_every_hostile_id(self):
        for agent in sorted(sessions_store.SESSION_STORES):
            for bad in self.HOSTILE:
                with self.subTest(agent=agent, bad=bad[:18]):
                    with self.assertRaises(ValueError):
                        sessions_store.resume_argv(agent, bad, tiers.repo_root())

    def test_absent_but_wellformed_id_rejected(self):
        """形状合法但盘上没有 —— 必须拒（这条决定"续聊"不能被拿来拉起任意命令）"""
        for agent, sid in (("grok", "00000000-0000-4000-8000-000000000000"),
                           ("codex", "00000000-0000-4000-8000-000000000000")):
            with self.subTest(agent=agent):
                with self.assertRaises(ValueError):
                    sessions_store.resume_argv(agent, sid, "/tmp")

    def test_unknown_agent_rejected(self):
        with self.assertRaises(ValueError):
            sessions_store.resume_argv("pi", "01a0c91d-eb84-7130-9767-479821ef336c", "/tmp")

    def test_argv_is_always_a_list_of_str(self):
        """返回形状：pty execvpe 要求全 str，混进 None/路径对象会直接崩在子进程"""
        for agent in sorted(sessions_store.SESSION_STORES):
            for bad in ("nope", ""):
                try:
                    argv = sessions_store.resume_argv(agent, bad, "/tmp")
                except ValueError:
                    continue
                self.assertIsInstance(argv, list)
                self.assertTrue(all(isinstance(x, str) for x in argv), f"{agent} argv 含非 str")


if __name__ == "__main__":
    unittest.main()
