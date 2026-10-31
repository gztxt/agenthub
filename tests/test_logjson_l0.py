"""JSON 行日志（v0.13.185）—— L0。

判据：
- 输出是**一行**合法 JSON（journal/文件按行切，多行会截断）
- 必含 ts / level / logger / msg，时间戳是 UTC ISO8601 且带 Z
- `extra` 的标量进顶层；与 LogRecord 内置名冲突的键**不炸**（旧实现会 KeyError）
- 异常带 `exc` 且含 traceback 首行
- setup() 幂等：重复调用不叠加 handler（叠加会让每条日志打两遍）
"""

import io
import json
import logging
import os
import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

import logjson  # noqa: E402


class TestFormatter(unittest.TestCase):
    def _rec(self, msg="hello", level=logging.INFO, **extra):
        r = logging.LogRecord("agenthub.test", level, __file__, 10, msg, (), None)
        for k, v in extra.items():
            setattr(r, k, v)
        return r

    def test_single_line_valid_json(self):
        out = logjson.JsonFormatter().format(self._rec("a\nb"))
        self.assertNotIn("\n", out, "多行 JSON 会被 journal 按行截断")
        d = json.loads(out)
        self.assertEqual(d["msg"], "a\nb")
        self.assertEqual(d["level"], "INFO")
        self.assertEqual(d["logger"], "agenthub.test")
        self.assertEqual(d["svc"], "agenthub")

    def test_ts_is_utc_z(self):
        d = json.loads(logjson.JsonFormatter().format(self._rec()))
        self.assertTrue(d["ts"].endswith("Z"), d["ts"])
        self.assertIn("T", d["ts"])

    def test_extra_scalars_lifted(self):
        d = json.loads(logjson.JsonFormatter().format(
            self._rec("x", mode="report", n=3, ok=True)))
        self.assertEqual(d["mode"], "report")
        self.assertEqual(d["n"], 3)
        self.assertIs(d["ok"], True)

    def test_extra_non_scalar_is_stringified(self):
        d = json.loads(logjson.JsonFormatter().format(self._rec("x", blob={"a": 1})))
        self.assertEqual(d["blob"], "{'a': 1}")

    def test_reserved_names_do_not_crash(self):
        # 直接 setattr 到 record 上模拟 `extra={"module": ...}` 的效果；
        # 真 logging 在 logger 层就会抛，这里守住 formatter 侧不二次抛。
        r = self._rec("x")
        r.module = "shadow"
        r.funcName = "fn"
        d = json.loads(logjson.JsonFormatter().format(r))
        self.assertNotIn("module", d)
        self.assertNotIn("funcName", d)

    def test_exception_included(self):
        try:
            raise ValueError("boom")
        except ValueError:
            r = logging.LogRecord("agenthub.test", logging.ERROR, __file__, 1,
                                  "fail", (), sys.exc_info())
        d = json.loads(logjson.JsonFormatter().format(r))
        self.assertIn("ValueError: boom", d["exc"])
        self.assertIn("Traceback", d["exc"])


class TestSetup(unittest.TestCase):
    """setup() 的开关与 handler 契约。

    v0.13.186 起 handler 挂在 **root**（配 SrcOnlyFilter），不再是 agenthub logger
    自挂一份 —— 因为迁移后的模块用 `logging.getLogger(__name__)`，名字是 `config`/
    `term` 这类顶层名，自挂 agenthub 收不到它们。清理必须同时覆盖 root。
    """

    def setUp(self):
        self.logger = logging.getLogger("agenthub")
        self.root = logging.getLogger()
        self._handlers = list(self.logger.handlers)
        self._root_handlers = list(self.root.handlers)
        self._prop = self.logger.propagate
        self._root_level = self.root.level
        logjson._setup_done = False

    def tearDown(self):
        for h in list(self.logger.handlers):
            if h not in self._handlers:
                self.logger.removeHandler(h)
        for h in list(self.root.handlers):
            if h not in self._root_handlers:
                self.root.removeHandler(h)
        self.logger.propagate = self._prop
        self.root.setLevel(self._root_level)
        logjson._setup_done = False

    def _install(self, env=None, **kw):
        """装到 StringIO 上：显式 stream 让断言不依赖真实 stdout/stderr。"""
        buf = io.StringIO()
        with mock.patch.dict(os.environ, env or {}, clear=False):
            if not env:
                os.environ.pop(logjson.LOG_JSON_ENV, None)
            logjson.setup(stream=buf, **kw)
        return buf

    def test_idempotent_handlers(self):
        self._install()
        n = len(self.root.handlers)
        logjson._setup_done = False          # 模拟“忘了已装过”的第二次调用
        logjson.setup()
        self.assertEqual(len(self.root.handlers), n,
                         "重复 setup 叠加 handler = 每条日志打两遍（root 标记位应拦住）")

    def test_default_is_plain_text_not_json(self):
        buf = self._install()
        logjson.get_logger("probe").info("hello")
        self.assertEqual(buf.getvalue(), "hello\n",
                         "默认模式必须与 print 逐字一致：无时间戳、无级别前缀、换行结尾")
        self.assertIsInstance(
            logjson._installed(self.root).formatter, logjson.PlainFormatter)

    def test_json_env_switches_formatter(self):
        buf = self._install(env={"HUB_LOG_JSON": "1"})
        logjson.get_logger("probe").warning("boom", extra={"k": 1})
        line = buf.getvalue().strip()
        d = json.loads(line)
        self.assertEqual(d["msg"], "boom")
        self.assertEqual(d["level"], "WARNING")
        self.assertEqual(d["k"], 1)
        self.assertIsInstance(
            logjson._installed(self.root).formatter, logjson.JsonFormatter)

    def test_falsey_env_keeps_plain(self):
        for v in ("0", "", "false", "no"):
            logjson._setup_done = False
            for h in list(self.root.handlers):
                if getattr(h, logjson._HANDLER_TAG, False):
                    self.root.removeHandler(h)
            buf = self._install(env={"HUB_LOG_JSON": v})
            logjson.get_logger("probe").info("plain")
            self.assertEqual(buf.getvalue(), "plain\n", f"HUB_LOG_JSON={v!r} 不该启用 JSON")

    def test_third_party_records_are_filtered_out(self):
        buf = self._install()
        # pathname 指向本测试文件（tests/ 不在 src/ 下）⇒ 必须被 SrcOnlyFilter 丢掉，
        # 否则接上 root 就等于把 aiohttp/uvicorn 的日志也改了格式。
        logging.getLogger("some.thirdparty").warning("library noise")
        self.assertEqual(buf.getvalue(), "", "非 src/ 模块的记录穿过了过滤器")

    def test_filter_accepts_agenthub_namespace(self):
        buf = self._install()
        logjson.get_logger("csp").info("probe")
        self.assertEqual(buf.getvalue(), "probe\n")

    def test_propagates_to_root_handler(self):
        self._install()
        self.assertTrue(self.logger.propagate,
                        "handler 在 root 上，agenthub logger 必须冒泡才收得到")

    def test_handler_emits_json_line(self):
        buf = io.StringIO()
        self._install()
        h = logjson.JsonStreamHandler(buf)
        h.setFormatter(logjson.JsonFormatter())
        self.logger.addHandler(h)
        logjson.get_logger("csp").info("probe", extra={"path": "/health"})
        line = buf.getvalue().strip().splitlines()[-1]
        d = json.loads(line)
        self.assertEqual(d["logger"], "agenthub.csp")
        self.assertEqual(d["path"], "/health")

    def test_get_logger_prefixes_namespace(self):
        self.assertEqual(logjson.get_logger("csp").name, "agenthub.csp")
        self.assertEqual(logjson.get_logger("agenthub.x").name, "agenthub.x")


if __name__ == "__main__":
    unittest.main()
