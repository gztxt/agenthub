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
        # 通用命令（非 probe）走 tailcat-connect.sh --target；v0.13.202 起 probe 改走单次探测
        a = ck.build_argv({"channel": "tailcat", "target": "WIN-20260130XPI"}, "health", False)
        self.assertEqual(a[:3], ["bash", ck.TC_CONNECT, "--target"])
        self.assertIn("WIN-20260130XPI", a)
        self.assertIn("health", a[-1])

    def test_probe_is_lightweight_echo_not_bridge(self):
        # 探测不依赖 bridge（A 机未部署时也该能测通道）
        a = ck.build_argv(self.LAN, "probe", False)
        self.assertTrue(a[-1].endswith("echo " + ck._PROBE_OK))
        self.assertNotIn("aia_bridge.py", a[-1])

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


class ProbePath(unittest.TestCase):
    """v0.13.202：探测走单次轻量 ssh，绕过 do_connect 8 次重试。"""
    LAN = {"name": "B", "channel": "lan", "host": "192.168.5.99", "user": "Mac"}
    TAIL = {"name": "A", "channel": "tailcat", "target": "WIN-20260130XPI",
            "user": "Administrator"}

    def test_lan_probe_single_ssh(self):
        a = ck.build_probe_argv(self.LAN)
        self.assertEqual(a[0], "ssh")
        self.assertIn("Mac@192.168.5.99", a)
        self.assertTrue(a[-1].endswith("echo " + ck._PROBE_OK))

    def test_tailcat_probe_single_ssh_via_proxycommand(self):
        a = ck.build_probe_argv(self.TAIL, token="tcFAKE_TOKEN_FOR_TEST_ONLY")
        self.assertEqual(a[0], "ssh")
        self.assertTrue(any("ProxyCommand=tailcat" in x for x in a))
        self.assertIn("Administrator@127.0.0.1", a)
        self.assertTrue(any("HostKeyAlias=tc-WIN-20260130XPI" in x for x in a))
        # 关键：不经过 tailcat-connect.sh（它内部 8 次重试会把探测拖到 181s）
        self.assertNotIn(ck.TC_CONNECT, a)
        self.assertNotIn("bash", a)

    def test_probe_never_runs_bridge(self):
        for m in (self.LAN, self.TAIL):
            a = ck.build_probe_argv(m, token="tcFAKE")
            self.assertNotIn("aia_bridge.py", a[-1])


class Accounts(unittest.TestCase):
    """v0.13.204 多账号：命令固定枚举 + 敏感参数只走 stdin，绝不进 argv。"""
    LAN = {"name": "B", "channel": "lan", "host": "192.168.5.99", "user": "Mac"}
    RELAY = {"name": "A", "channel": "relay", "target": "WIN-20260130XPI",
             "user": "Administrator"}

    def test_lan_account_argv_shape(self):
        a = ck.build_account_argv(self.LAN, "account-add", "workbuddy")
        self.assertEqual(a[0], "ssh")
        self.assertIn("Mac@192.168.5.99", a)
        self.assertTrue(a[-1].endswith("account-add --platform workbuddy --stdin"))
        # 手机号/验证码不在 argv（走 stdin）
        blob = " ".join(a)
        self.assertNotIn("phone", blob)
        self.assertNotIn("code", blob)

    def test_accounts_list_argv_has_no_stdin(self):
        a = ck.build_account_argv(self.LAN, "accounts", "traework")
        self.assertTrue(a[-1].endswith("accounts --platform traework"))
        self.assertNotIn("--stdin", a[-1])

    def test_action_and_platform_whitelist(self):
        with self.assertRaises(ValueError):
            ck.build_account_argv(self.LAN, "account-add; rm -rf /", "workbuddy")
        with self.assertRaises(ValueError):
            ck.build_account_argv(self.LAN, "account-add", "dumate")
        with self.assertRaises(ValueError):
            ck.build_account_argv(self.LAN, "accounts", "workbuddy; id")

    def test_relay_wraps_account_command(self):
        import base64
        a = ck.build_account_argv(self.RELAY, "account-send", "workbuddy")
        b64 = next(x.split()[-1] for x in a if "EncodedCommand" in x)
        inner = base64.b64decode(b64).decode("utf-16-le")
        self.assertIn("account-send", inner)
        self.assertIn("--stdin", inner)

    def test_phone_sms_uid_validators(self):
        for ok in ("13800138000", "19912345678"):
            self.assertTrue(ck._PHONE_RE.match(ok))
        for bad in ("1234", "1380013800", "138001380001", "abc", "13800138000; id"):
            self.assertFalse(ck._PHONE_RE.match(bad))
        for ok in ("1234", "123456", "12345678"):
            self.assertTrue(ck._SMS_RE.match(ok))
        for bad in ("", "12", "123456789", "12ab"):
            self.assertFalse(ck._SMS_RE.match(bad))
        for ok in ("abcdEF12", "u_1.a-b", "1234567890"):
            self.assertTrue(ck._UID_RE.match(ok))
        for bad in ("../etc/passwd", "a/b", "a b", "a;b", ""):
            self.assertFalse(ck._UID_RE.match(bad))


class RelayChannel(unittest.TestCase):
    """v0.13.203：A 机走「经 B 跳板」通道 —— NAS→A 的 DERP 数据面不通而 B→A 实测通。"""
    RELAY = {"name": "A", "channel": "relay", "target": "WIN-20260130XPI",
             "user": "Administrator"}

    def test_relay_wraps_inner_ssh_in_encodedcommand(self):
        a = ck.build_argv(self.RELAY, "health", False)
        self.assertEqual(a[0], "ssh")
        self.assertIn("Mac@192.168.5.99", a)          # 外层跳板 = B 机
        self.assertTrue(any("EncodedCommand" in x for x in a))
        # 内层在 b64 里：解码后应含 tailcat ProxyCommand 与目标用户
        import base64
        b64 = next(x.split()[-1] for x in a if "EncodedCommand" in x)
        inner = base64.b64decode(b64).decode("utf-16-le")
        self.assertIn("tailcat.exe", inner)
        self.assertIn("Administrator@127.0.0.1", inner)
        self.assertIn("health", inner)

    def test_relay_probe_uses_echo_not_bridge(self):
        a = ck.build_probe_argv(self.RELAY, token="tcFAKE")
        import base64
        b64 = next(x.split()[-1] for x in a if "EncodedCommand" in x)
        inner = base64.b64decode(b64).decode("utf-16-le")
        self.assertNotIn("aia_bridge.py", inner)
        self.assertIn("echo " + ck._PROBE_OK, inner)

    def test_relay_without_token_rejected(self):
        import tempfile, unittest.mock as um
        with um.patch.object(ck, "_token_for", return_value=""):
            with self.assertRaises(ValueError):
                ck.build_argv(self.RELAY, "health", False)

    def test_seeds_a_is_relay(self):
        a = next(s for s in ck.SEEDS if s["name"] == "WIN-20260130XPI")
        self.assertEqual(a["channel"], "relay")

    def test_bad_target_or_user_rejected(self):
        with self.assertRaises(ValueError):
            ck.build_probe_argv({"channel": "tailcat", "target": "WIN; rm"},
                                token="tcFAKE")
        with self.assertRaises(ValueError):
            ck.build_probe_argv({"channel": "tailcat", "target": "WIN",
                                 "user": "Admin && evil"}, token="tcFAKE")


class ClassifyFailure(unittest.TestCase):
    """三种失败按 reason 归类，不再一律 offline+timeout。"""

    def test_data_plane_down(self):
        # A 机实测 stderr：ping 通但 22 端口不通
        reason, human = ck._classify_failure(
            255, "Dial: context deadline exceeded\nkex_exchange_identification: "
                 "Connection closed by remote host\nConnection closed by UNKNOWN port 65535",
            "", {"ok": True, "detail": "pong via DERP"})
        self.assertEqual(reason, "data-plane-down")
        self.assertIn("ssh/22", human)

    def test_control_plane_down(self):
        reason, _ = ck._classify_failure(255, "some other error", "",
                                         {"ok": False, "detail": "ping timeout"})
        self.assertEqual(reason, "control-plane-down")

    def test_bridge_missing(self):
        reason, human = ck._classify_failure(
            1, "", "python: can't open file 'C:\\AIAutomator\\contrib\\aia_bridge.py'", None)
        self.assertEqual(reason, "bridge-missing")
        self.assertIn("未部署", human)

    def test_plain_ssh_failure(self):
        reason, _ = ck._classify_failure(1, "mystery", "", None)
        self.assertEqual(reason, "ssh-failed")


class TimeoutAlignment(unittest.TestCase):
    """hub 超时必须覆盖 do_connect 8 次重试（8×20+7×3≈181s），否则先 kill 吞错误。"""

    def test_run_timeout_covers_retry_budget(self):
        self.assertGreaterEqual(ck._RUN_TIMEOUT, 185)

    def test_probe_timeout_is_short(self):
        self.assertLess(ck._PROBE_TIMEOUT, 20)
        self.assertLess(ck._PING_TIMEOUT, ck._PROBE_TIMEOUT + 5)


if __name__ == "__main__":
    unittest.main()
