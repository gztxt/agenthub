#!/usr/bin/env python3
"""L0 hermetic：Hub MCP 出口面收窄（v0.13.172 / PT-20261028-01 D 步）。

为什么需要这一层：把 hub MCP 挂给**别的 agent 会话**（dsh 的 Bot）时，
「默认全给、再想扣哪个」是错的默认值 —— 每上线一个新工具就自动多一个出口，
没人会注意到。这组用例钉住三条不变量：

1. **不静默摘功能**：`hub_dispatch` / `hub_replan` 是已上线能力（PT-20261027-04），
   默认必须还在；只有显式 `HUB_MCP_ALLOW_DISPATCH=0` 才摘。摘掉的场景用
   `removed` 报出来，不靠调用方猜。
2. **白名单是白名单**：设了 `HUB_MCP_ALLOW_TOOLS` 就只留列出的；**列了 dispatch
   也打不开被闸门关掉的工具** —— 安全默认不能被一份手写清单推翻，冲突必须
   显式报 `blocked`。
3. **拼错要说话**：白名单里写了不存在的名字，必须出现在 `unknown` 里。
   静默少一个工具是这类配置最难查的故障（工具「就是不在」，日志什么都没有）。

env 在 import 期读，故每个场景用 `importlib.reload` 重读（复用一个子进程，单场景 <0.3s）。
"""
import json
import os
import pathlib
import subprocess
import sys
import tempfile
import unittest

_REPO = pathlib.Path(__file__).resolve().parents[1]
_SRC = _REPO / "src"

#: 子进程脚本：按场景改 env → reload → 收窄 → 输出摘要（JSON 一行一个场景）
_CHILD = r"""
import importlib, json, os, sys
import hubmcp

out = []
for scen in json.loads(sys.argv[1]):
    for k in list(os.environ):
        if k.startswith("HUB_MCP_ALLOW_"):
            del os.environ[k]
    os.environ.update(scen["env"])
    mod = importlib.reload(hubmcp)
    first = mod.apply_tool_scope()
    second = mod.apply_tool_scope()          # 幂等：重复调用不得抛、不得再变化
    out.append({"name": scen["name"], "scope": first,
                "idempotent": first["kept"] == second["kept"] and not second["removed"]})
print("SCOPE_RESULT " + json.dumps(out, ensure_ascii=False))
"""


class McpToolScope(unittest.TestCase):

    @classmethod
    def setUpClass(cls):
        cls.tmp = pathlib.Path(tempfile.mkdtemp(prefix="l0mcp-scope-"))
        scenarios = [
            {"name": "default", "env": {}},
            {"name": "dispatch_off", "env": {"HUB_MCP_ALLOW_DISPATCH": "0"}},
            {"name": "whitelist", "env": {
                "HUB_MCP_ALLOW_TOOLS": "hub_kb_search,hub_kb_status"}},
            {"name": "whitelist_vs_gate", "env": {
                "HUB_MCP_ALLOW_DISPATCH": "0",
                "HUB_MCP_ALLOW_TOOLS": "hub_kb_search,hub_dispatch"}},
            {"name": "typo", "env": {"HUB_MCP_ALLOW_TOOLS": "hub_kb_search,hub_not_a_tool"}},
        ]
        env = {k: v for k, v in os.environ.items() if not k.startswith("HUB_MCP_")}
        env.update({"PYTHONPATH": str(_SRC), "PORT": "3199",
                    "DATA_DIR": str(cls.tmp), "LOG_DIR": str(cls.tmp / "logs")})
        p = subprocess.run([sys.executable, "-c", _CHILD, json.dumps(scenarios)],
                           capture_output=True, text=True, timeout=120, env=env)
        if p.returncode != 0:
            raise AssertionError("子进程失败：%s" % (p.stderr or p.stdout)[-600:])
        line = [ln for ln in p.stdout.splitlines() if ln.startswith("SCOPE_RESULT ")][-1]
        cls.scopes = {s["name"]: s for s in json.loads(line[len("SCOPE_RESULT "):])}

    def scope(self, name):
        return self.scopes[name]["scope"]

    # ── 1. 默认面：不摘功能 ──────────────────────────────────────────
    def test_dispatch_stays_by_default(self):
        """回归守卫：默认摘掉 dispatch 会让既有 MCP 客户端静默少一个能力。"""
        s = self.scope("default")
        self.assertTrue(s["allow_dispatch"])
        self.assertIn("hub_dispatch", s["kept"])
        self.assertIn("hub_replan", s["kept"])
        self.assertEqual(s["removed"], [])

    def test_read_tools_exist_by_default(self):
        kept = set(self.scope("default")["kept"])
        for name in ("hub_kb_search", "hub_skill_list", "hub_intake", "hub_memory_search"):
            self.assertIn(name, kept)

    # ── 2. 显式关闸 ───────────────────────────────────────────────────
    def test_dispatch_can_be_turned_off_explicitly(self):
        s = self.scope("dispatch_off")
        self.assertFalse(s["allow_dispatch"])
        self.assertNotIn("hub_dispatch", s["kept"])
        self.assertNotIn("hub_replan", s["kept"])
        self.assertEqual(sorted(s["removed"]), ["hub_dispatch", "hub_replan"])

    # ── 3. 白名单 ─────────────────────────────────────────────────────
    def test_whitelist_keeps_only_named_tools(self):
        s = self.scope("whitelist")
        self.assertEqual(sorted(s["kept"]), ["hub_kb_search", "hub_kb_status"])
        self.assertNotIn("hub_dispatch", s["kept"])

    def test_whitelist_cannot_reopen_a_gated_tool(self):
        """闸门优先：白名单里写了 dispatch 也不算放行，且必须报 blocked 而不是静默。"""
        s = self.scope("whitelist_vs_gate")
        self.assertEqual(sorted(s["kept"]), ["hub_kb_search"])
        self.assertEqual(s["blocked"], ["hub_dispatch"])

    def test_typo_in_whitelist_is_reported(self):
        s = self.scope("typo")
        self.assertEqual(s["unknown"], ["hub_not_a_tool"])
        self.assertEqual(s["kept"], ["hub_kb_search"])

    # ── 4. 幂等 ───────────────────────────────────────────────────────
    def test_apply_scope_is_idempotent(self):
        for name in self.scopes:
            self.assertTrue(self.scopes[name]["idempotent"],
                            "%s: 重复调用 apply_tool_scope 改变了工具面" % name)


if __name__ == "__main__":
    unittest.main()
