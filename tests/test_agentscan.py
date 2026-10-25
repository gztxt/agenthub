#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""L0 hermetic：安装态扫描（src/agentscan.py，v0.13.155）。

为什么这些用例必须存在（每条都对应一次真实误判或用户报障）：
1. **名字不是证据**：本机 `~/.local/bin/goose` 与 Goose Agent 同名，但 `--version`
   只回 `1.47.0`。只按名字登记 = 多一张永远起不来的假卡（2026-09-21 的 fcc-* 假卡同族）。
   ⇒ 名字命中也必须过 `verify_re` 自证。
2. **给 agent 用的工具不是 agent**：`ecc-agentshield`（审计器）、`@ccpocket/bridge`
   （桥接服务）、`@agegr/pi-web`（Web UI）自述里都有 "AI agent"，第一版实现把它们
   全登记了。⇒ 「不是本体」的反向词表必须有，且服务形状的名字要挡。
3. **未知二进制不执行**：扫描器只有「按签名核对」的权限，没有「试一下它是啥」的权限。
   ⇒ prober 只对命中的候选调用，未命中一律不调。
4. **人工登记不被覆盖**：`source='manual'` 的行是人写的（cwd/参数比扫描准），
   扫描只补缺口。
"""
import json
import os
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO))

import agentscan  # noqa: E402

SIGS = [{"id": "zcode", "display": "ZCode", "names": ["zcode"],
         "verify_re": "(?i)\\bzcode\\b", "desc": "Z.AI 编码 Agent"}]


class _Base(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)

    def tearDown(self):
        self._tmp.cleanup()

    def exe(self, rel: str, body: str = "#!/bin/sh\nexit 0\n") -> str:
        p = self.tmp / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(body)
        os.chmod(p, 0o755)
        return str(p)

    def pkg(self, rel: str, name: str, desc: str, keywords=None) -> str:
        d = self.tmp / rel
        d.mkdir(parents=True, exist_ok=True)
        (d / "package.json").write_text(json.dumps({
            "name": name, "description": desc, "keywords": keywords or []}))
        return self.exe(str(d / "cli.js"))


class TestClassify(_Base):
    def test_name_and_selfproof_registers(self):
        path = self.exe("bin/zcode")
        item = agentscan.classify("zcode", path, SIGS,
                                  prober=lambda p: "zcode 0.17.0\nUsage:\n  zcode")
        self.assertEqual(item["id"], "zcode")
        self.assertTrue(item["verified"])
        self.assertEqual(item["command"], path)

    def test_name_collision_without_selfproof_is_rejected(self):
        """同名但自证不过 ⇒ 不登记（构造：一个 `--version` 只回版本号的同名工具）。

        现实依据：`goose` 在 Go 生态里是数据库迁移工具，与 Goose Agent 同名；
        `--version` 只回 `1.47.0`，光看名字与版本串分辨不出来。本机装的确实是
        Agent（`--help` 首行 "An AI agent"），但判据不能建立在「名字对上了」上。
        """
        path = self.exe("bin/goose")
        sigs = [{"id": "goose", "display": "Goose", "names": ["goose"],
                 "verify_re": "(?i)goose.{0,80}(agent|ai)"}]
        item = agentscan.classify("goose", path, sigs,
                                  prober=lambda p: "1.47.0\nUsage: goose [OPTIONS] COMMAND")
        self.assertIsNone(item)

    def test_service_shaped_name_is_rejected(self):
        """@agegr/pi-web: 'Web UI for the pi coding agent' —— 是界面不是 agent。"""
        path = self.pkg("node_modules/pi-web", "@agegr/pi-web",
                        "Web UI for the pi coding agent")
        self.assertIsNone(agentscan.classify("pi-web", path, SIGS, prober=lambda p: ""))

    def test_auditor_package_is_rejected(self):
        """ecc-agentshield: 'Security auditor for AI agent configurations'。"""
        path = self.pkg("node_modules/agentshield", "ecc-agentshield",
                        "Security auditor for AI agent configurations.", ["ai-agent"])
        self.assertIsNone(agentscan.classify("agentshield", path, SIGS, prober=lambda p: ""))

    def test_bridge_package_is_rejected(self):
        path = self.pkg("node_modules/bridge", "@ccpocket/bridge",
                        "Bridge server that connects sessions to mobile devices",
                        ["coding-agent"])
        self.assertIsNone(agentscan.classify("ccpocket-bridge", path, SIGS,
                                             prober=lambda p: ""))

    def test_real_coding_agent_package_registers(self):
        """reasonix: 'Cache-first DeepSeek coding agent for the terminal' —— 真 agent。"""
        path = self.pkg("node_modules/reasonix", "reasonix",
                        "Cache-first DeepSeek coding agent for the terminal.",
                        ["deepseek", "agent", "coding-assistant"])
        item = agentscan.classify("reasonix", path, SIGS, prober=lambda p: "")
        self.assertIsNotNone(item)
        self.assertEqual(item["id"], "reasonix")
        self.assertFalse(item["verified"])      # 未实测应答，卡片上要标出来

    def test_plain_tool_is_not_an_agent(self):
        path = self.pkg("node_modules/ntfs", "ntfs-read",
                        "Read NTFS volumes from userspace.")
        self.assertIsNone(agentscan.classify("ntfs-read", path, SIGS, prober=lambda p: ""))


class TestScan(_Base):
    def test_unknown_binaries_are_never_executed(self):
        """扫描器不试跑未知二进制：prober 只该被命中的那一个调用。"""
        zdir = self.tmp / "bin"
        self.exe("bin/zcode")
        for n in ("randomtool", "foo-agent-ish", "git"):
            self.exe(f"bin/{n}")
        calls = []

        def prober(p):
            calls.append(p)
            return "zcode 0.17.0"

        # 必须屏蔽「已在册」过滤：本条判的是**未知二进制不执行**，与画像里有哪些卡无关。
        # 2026-10-25 反例：zcode 一进 PROFILES 静态卡，_covered() 就把它当已覆盖，
        # found 变空、这条闸门红在「样本选得脆」上，而不是被测行为坏了。
        with mock.patch.object(agentscan, "_covered", return_value=(set(), set())):
            r = agentscan.scan(dirs=[zdir], register=False, prober=prober)
        self.assertEqual([i["id"] for i in r["found"]], ["zcode"])
        self.assertEqual(calls, [str(zdir / "zcode")])
        self.assertIn("randomtool", [u["name"] for u in r["unknown"]])
        self.assertNotIn("git", [u["name"] for u in r["unknown"]])

    def test_no_dup_when_already_covered(self):
        """已在画像里的名字不该再被扫描登记（same CLI 两处写会漂）。"""
        zdir = self.tmp / "bin"
        self.exe("bin/claude")
        r = agentscan.scan(dirs=[zdir], register=False, prober=lambda p: "claude 2.0")
        self.assertNotIn("claude", [i["id"] for i in r["found"]])


class TestRegister(_Base):
    def setUp(self):
        super().setUp()
        import db
        db.init_db(self.tmp / "agents.db")
        self.db = db

    def test_registers_as_agent_kind(self):
        added = agentscan.register_items([{
            "id": "zcode", "name": "ZCode", "command": "/x/zcode",
            "desc": "Z.AI", "verified": True, "version": "0.17.0"}], self.db)
        self.assertEqual(added, ["zcode"])
        row = self.db.query("SELECT * FROM custom_agents WHERE id='zcode'")[0]
        self.assertEqual(row["kind"], "agent")
        self.assertEqual(row["source"], "scan:cli")
        self.assertIn("0.17.0", row["description"])

    def test_manual_row_is_not_overwritten(self):
        now = "2026-10-25T00:00:00+00:00"
        self.db.execute(
            """INSERT INTO custom_agents(id,name,type,kind,command,args,working_dir,env,
                 port,endpoint,description,source,created_at,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            ("myagent", "My Agent", "agent", "agent", "/opt/my", "[]",
             "/srv", "{}", None, None, "人写的", "manual", now, now))
        added = agentscan.register_items([{
            "id": "myagent", "name": "扫描猜的", "command": "/other/my",
            "desc": "x", "verified": True, "version": ""}], self.db)
        self.assertEqual(added, [])
        row = self.db.query("SELECT * FROM custom_agents WHERE id='myagent'")[0]
        self.assertEqual(row["name"], "My Agent")
        self.assertEqual(row["command"], "/opt/my")


if __name__ == "__main__":
    unittest.main()
