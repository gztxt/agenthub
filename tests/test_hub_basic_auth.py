#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""机器侧 HTTP Basic 凭据：唯一解析实现的行为闸门。

## 钉住什么

1. **`.env` 压过环境变量**（2026-11-09 实测 401 的根因修复）
   PTY 会话启动时把当时的 `.env` 导出进 shell；用户改口令后 `.env` 已更新、shell 里仍是
   旧快照。旧实现先读 `HUB_SUPERADMIN_PW` 环境变量 ⇒ `intake.sh` 恒 401
   （实测 RC=3，body = `{"detail": "未登录（须 hub 会话 Cookie 或 HTTP Basic）"}`）。
   修复后以 `.env` 为权威，环境变量只兜底，并打一行「不一致」WARN（不打印任何值）。

2. **口令不进 argv**
   `curl -u user:pw` 会让口令出现在 `ps` / 审计日志 / 子代理回显里。判据用 PATH 里的
   假 curl **记录真实 argv**：argv 不许含口令，`--config` 指向的流里必须含口令
   —— 后者防止「为了不泄露而根本没把凭据发出去」的假修。

3. **引号与 CR 要剥**
   dotenv 允许 `KEY="value"`；不剥引号就把引号当密码的一部分，服务端必然拒绝。

## 分层

假 curl + tempfile，零网络、零真盘 ⇒ L0。
`/fs` 下的 `scripts/intake.sh` 静态守卫按分层口径另打 @@tiers.host_only@@（L1）。
"""
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "tests"))

import tiers  # noqa: E402

LIB = _REPO / "scripts" / "hub-basic.sh"
SELFCHECK = _REPO / "scripts" / "selfcheck_intake.sh"
INTAKE = pathlib.Path("/fs/1000/ftp/技术文档/scripts/intake.sh")

_STUB = """#!/usr/bin/env bash
printf '%s\n' "$@" > "$CAP/argv"
for a in "$@"; do
  case "$a" in /dev/fd/*) cat "$a" > "$CAP/cfg" ;; esac
done
"""


class TestHubBasicHelper(unittest.TestCase):
    def setUp(self):
        self.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0-hubbasic-"))
        (self.tmp / "bin").mkdir()
        stub = self.tmp / "bin" / "curl"
        stub.write_text(_STUB, encoding="utf-8")
        stub.chmod(0o755)
        self.cap = self.tmp / "cap"
        self.cap.mkdir()
        # 引号 + CR：dotenv 常见形态，不剥就发错
        self.envf = self.tmp / "env"
        self.envf.write_bytes(b'HUB_SUPERADMIN_PW="FilePw-123"\r\nPORT=3102\r\n')

    def run_bash(self, body, env=None):
        e = dict(os.environ)
        e.update({"HUB_BASIC_LIB": str(LIB), "CAP": str(self.cap)})
        # 凭据不继承宿主环境：否则「缺凭据」用例会被本机 .env 快照悄悄救活（假绿）
        for k in ("HUB_BASIC_PW", "HUB_SUPERADMIN_PW"):
            e.pop(k, None)
        if env:
            e.update(env)
        code = '. "$HUB_BASIC_LIB"\n' + body
        return subprocess.run(["bash", "-c", code], capture_output=True,
                              text=True, timeout=60, env=e)

    def resolved(self, body="hub_basic_resolve \"$ENVF\"\nprintf '%s' \"$HUB_BASIC_PW\"",
                 env=None):
        e = {"ENVF": str(self.envf)}
        if env:
            e.update(env)
        p = self.run_bash(body, e)
        self.assertEqual(p.returncode, 0, p.stderr)
        return p.stdout

    # ── 1. 优先级 ─────────────────────────────────────────────────────
    def test_env_file_wins_over_stale_env_var(self):
        got = self.resolved(env={"HUB_SUPERADMIN_PW": "stale-old-pw"})
        self.assertEqual(got, "FilePw-123",
                         "旧口令环境变量压过 .env 就是 2026-11-09 恒 401 的根因")

    def test_warns_on_mismatch_without_echoing_values(self):
        p = self.run_bash('hub_basic_resolve "$ENVF"\nprintf "%s" "$HUB_BASIC_PW"',
                          {"ENVF": str(self.envf), "HUB_SUPERADMIN_PW": "stale-old-pw"})
        self.assertIn("WARN", p.stderr)
        self.assertNotIn("stale-old-pw", p.stderr)
        self.assertNotIn("FilePw-123", p.stderr)

    def test_env_var_is_fallback_when_file_missing(self):
        got = self.resolved('hub_basic_resolve "$ENVF"\nprintf "%s" "$HUB_BASIC_PW"',
                            {"ENVF": str(self.tmp / "nope.env"),
                             "HUB_SUPERADMIN_PW": "OnlyEnv-Pw"})
        self.assertEqual(got, "OnlyEnv-Pw")

    def test_quotes_and_cr_are_stripped(self):
        got = self.resolved()
        self.assertNotIn('"', got)
        self.assertNotIn("\r", got)
        self.assertEqual(got, "FilePw-123")

    def test_missing_credential_returns_nonzero(self):
        p = self.run_bash('hub_basic_resolve "$ENVF"', {"ENVF": str(self.tmp / "nope.env"),
                                                     "HUB_SUPERADMIN_PW": ""})
        self.assertNotEqual(p.returncode, 0, "取不到凭据必须失败，不能静默发匿名请求")

    # ── 2. argv 不泄露 ────────────────────────────────────────────────
    def test_password_never_in_argv_but_reaches_config(self):
        p = self.run_bash(
            'hub_basic_resolve "$ENVF"\n'
            'PATH="$CAP/../bin:$PATH" hub_basic_curl -s http://127.0.0.1:9/x\n',
            {"ENVF": str(self.envf)})
        self.assertEqual(p.returncode, 0, p.stderr)
        argv = (self.cap / "argv").read_text(encoding="utf-8")
        cfg = (self.cap / "cfg").read_text(encoding="utf-8")
        self.assertNotIn("FilePw-123", argv, "口令进了 argv ⇒ ps/审计可见")
        self.assertIn("--config", argv)
        self.assertIn("FilePw-123", cfg, "凭据必须真的传下去，不能为了不泄露而丢掉")
        self.assertIn("gztxt", cfg)

    # ── 3. 静态守卫：调用点不许退回 -u ─────────────────────────────────
    def test_scripts_do_not_use_curl_u_with_password(self):
        for f in (SELFCHECK,):
            src = f.read_text(encoding="utf-8")
            self.assertNotIn('CURL_AUTH', src, f"{f.name} 退回数组 -u 形态")
            self.assertNotIn('-u "$HUB_USER:$HUB_PW"', src, f"{f.name} 口令进 argv")
            self.assertIn("hub_basic_curl", src, f"{f.name} 未接统一入口")


@tiers.host_only
class TestIntakeScriptHost(unittest.TestCase):
    """`/fs` 下的 intake.sh 不在本仓 ⇒ 按分层口径归 L1。

    不继承 `_Case`：否则 L0 的七条行为断言会在 L1 里被重复收集一遍
    （HUB_HOST_TESTS=0 时全变 SKIP，L0 闸门会误判「分层放错」）。
    """

    def test_intake_uses_shared_helper(self):
        if not INTAKE.is_file():
            self.skipTest("SKIP(host-dependent): scripts/intake.sh 不在本机（换机即跳过）")
        src = INTAKE.read_text(encoding="utf-8")
        self.assertNotIn('CURL_AUTH', src)
        self.assertNotIn('-u "$HUB_USER:$HUB_PW"', src)
        self.assertIn("hub_basic_resolve", src)
        self.assertIn("hub_basic_curl", src)


if __name__ == "__main__":
    unittest.main()
