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
import sys
import unittest
from pathlib import Path

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
    def setUp(self):
        self.logger = logging.getLogger("agenthub")
        self._handlers = list(self.logger.handlers)
        self._prop = self.logger.propagate
        logjson._setup_done = False

    def tearDown(self):
        for h in list(self.logger.handlers):
            if h not in self._handlers:
                self.logger.removeHandler(h)
        if len(self.logger.handlers) != len(self._handlers):
            self.logger.handlers[:] = self._handlers
        self.logger.propagate = self._prop
        logjson._setup_done = False

    def test_idempotent_handlers(self):
        logjson.setup()
        n = len(self.logger.handlers)
        logjson.setup()
        self.assertEqual(len(self.logger.handlers), n, "重复 setup 叠加 handler = 每条日志打两遍")

    def test_no_propagation_to_root(self):
        logjson.setup()
        self.assertFalse(self.logger.propagate, "冒泡到 root 会与 uvicorn handler 重复输出")

    def test_handler_emits_json_line(self):
        buf = io.StringIO()
        logjson.setup()
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
