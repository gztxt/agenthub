"""L0 · 积分签到远程管理（v0.13.201）：纯逻辑层，零网络零服务零 DB。

判据覆盖 AGENTS.md §2「全链路修复」与 281 卡三种「没有」：
- parse_bridge 五态：正常 / 无 sentinel / 坏 JSON / 非对象 / ok=false —— 绝不把故障吞成空列表
- build_argv 命令白名单 + 注入防护：用户输入绝不进 shell（OWASP 命令注入）
- 两种通道（LAN SSH / tailcat）argv 形状、dry 默认
- SEEDS 登记 A/B 两台且不落任何凭据
"""
import sys
import unittest
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"
sys.path.insert(0, str(SRC))
import checkinmgr as ck  # noqa: E402


def _wrapped(cmd="probe", host="DESKTOP-QUU9CJT", ok=True):
    import json
    inner = {"ok": ok, "cmd": cmd, "host": host, "ts": 1794648000,
            "data": {"results": [{"platform": "WorkBuddy", "success": True, "status": "already"}]}}
    return "noise\r\n" + ck.BEGIN + "\r\n" + json.dumps(inner) + "\r\n" + ck.END + "\r\n"


class ParseBridge(unittest.TestCase):
    def test_normal_ok(self):
        r = ck.parse_bridge(_wrapped())
        self.assertTrue(r["ok"])
        self.assertEqual(r["state"], "ok")
        self.assertEqual(r["data"]["data"]["results"][0]["status"], "already")

    def test_no_sentinel_is_unreadable_not_empty(self):
        for raw in ("", "ssh: connection refused", ck.BEGIN + "\n{}", "x\n" + ck.END):
            r = ck.parse_bridge(raw)
            self.assertFalse(r["ok"])
            self.assertEqual(r["state"], "unreadable")

    def test_bad_json_is_unreadable(self):
        r = ck.parse_bridge(ck.BEGIN + "\n{not json}\n" + ck.END)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "unreadable")
        self.assertIn("bad-json", r["error"])

    def test_non_object_is_unreadable(self):
        r = ck.parse_bridge(ck.BEGIN + "\n[1,2]\n" + ck.END)
        self.assertFalse(r["ok"])
        self.assertEqual(r["error"], "bridge-not-object")

    def test_ok_false_is_unreadable_with_reason(self):
        import json
        # 直接构造：_wrapped(ok=False) 生成的是 "ok": false，靠 replace 找 "ok": true 不会命中
        inner = {"ok": False, "error": "engine boom"}
        r = ck.parse_bridge(ck.BEGIN + "\n" + json.dumps(inner) + "\n" + ck.END)
        self.assertFalse(r["ok"])
        self.assertEqual(r["state"], "unreadable")
        self.assertEqual(r["error"], "engine boom")

    def test_ascii_escaped_chinese_roundtrips(self):
        # Windows 控制台 GBK：bridge 用 ensure_ascii=True；JSON 解析后中文无损
        import json
        inner = {"ok": True, "data": {"display": "\u767e\u5ea6\u642d\u5b50 DuMate"}}
        r = ck.parse_bridge(ck.BEGIN + "\n" + json.dumps(inner) + "\n" + ck.END)
        self.assertEqual(r["data"]["data"]["display"], "百度搭子 DuMate")


class BuildArgv(unittest.TestCase):
    LAN = {"name": "B", "channel": "lan", "host": "192.168.5.99", "user": "Mac"}

    def test_lan_ssh_shape(self):
        a = ck.build_argv(self.LAN, "run", dry=True)
        self.assertEqual(a[0], "ssh")
        self.assertIn("Mac@192.168.5.99", a)
        self.assertTrue(a[-1].endswith("run --dry"))
        self.assertNotIn("--dry", a[-1].replace("run --dry", ""))  # 只出现一次

    def test_exec_no_dry(self):
        a = ck.build_argv(self.LAN, "run", dry=False)
        self.assertTrue(a[-1].endswith("run"))
        self.assertNotIn("--dry", a[-1])

    def test_tailcat_channel(self):
        a = ck.build_argv({"channel": "tailcat", "target": "WIN-20260130XPI"}, "probe", False)
        self.assertEqual(a[:3], ["bash", ck.TC_CONNECT, "--target"])
        self.assertIn("WIN-20260130XPI", a)
        self.assertIn("probe", a[-1])

    def test_cmd_whitelist(self):
        self.assertIn("run", ck.CMDS)
        self.assertNotIn("run; rm -rf /", ck.CMDS)
        with self.assertRaises(ValueError):
            ck.build_argv(self.LAN, "run; echo pwned", False)

    def test_injection_bad_host_user_target_rejected(self):
        for bad in (
            {"channel": "lan", "host": "1.1.1.1; rm -rf /", "user": "Mac"},
            {"channel": "lan", "host": "192.168.5.99", "user": "Mac && curl evil"},
            {"channel": "tailcat", "target": "WIN; shutdown"},
        ):
            with self.assertRaises(ValueError):
                ck.build_argv(bad, "probe", False)

    def test_shell_is_false_by_construction(self):
        # argv 是 list，远程命令是最后一个参数，不含任何用户可控自由文本
        a = ck.build_argv(self.LAN, "health", False)
        self.assertIsInstance(a, list)
        self.assertNotIn("shell", [str(x) for x in a])


class Seeds(unittest.TestCase):
    def test_two_machines_registered(self):
        self.assertEqual(len(ck.SEEDS), 2)
        self.assertEqual({s["name"] for s in ck.SEEDS},
                         {"DESKTOP-QUU9CJT", "WIN-20260130XPI"})

    def test_no_credentials_in_seeds(self):
        blob = repr(ck.SEEDS).lower()
        for word in ("token", "password", "secret", "app_secret", "api_key", "auth.json"):
            self.assertNotIn(word, blob)


if __name__ == "__main__":
    unittest.main()
