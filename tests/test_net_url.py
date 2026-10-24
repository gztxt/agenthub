"""卡片地址按访问网络自适应（2026-10-24）。

背景：服务端给的卡片地址只能写一个字面量（dsh 甚至只绑一张网卡 192.168.5.102:3080），
用户从 Tailscale 访问时那个地址不可达 —— 实测 tailscale 侧 000、局域网 401。

两层判据，缺一不可：
1. 后端 `profiles.own_addrs()` 实测本机地址，且**不把容器/回环地址**混进来
   （混进来会让前端把 172.17.0.1 当成可替换候选，产生假候选）。
2. 前端 `lanUrl()` 真的把命中地址换掉 —— 用 tests/net_url_probe.js 执行**源文件里的函数**
   比对输入输出，而不是 grep 代码里有没有某个字符串。

⚠ 本文件在标准测试层（L0），必须写成 `unittest.TestCase`：本仓有
`tests/test_tier_collector_parity.py` 对账 pytest 与 unittest discover 的收集数，
写成 pytest 函数会「pytest 跑得到、unittest 收不到」，换入口就静默少跑。
"""
import json
import os
import shutil
import subprocess
import sys
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "src"))

import profiles  # noqa: E402


class OwnAddrsTest(unittest.TestCase):
    def test_excludes_loopback_and_bridges(self):
        addrs = profiles.own_addrs()
        self.assertTrue(addrs, "ip -o addr 解析为空（探针自身失效，不是'没有地址'）")
        for a in addrs:
            self.assertFalse(a.startswith("127."), f"回环混进清单: {a}")
            self.assertNotEqual(a, "::1", f"回环 v6 混进清单: {a}")
            self.assertFalse(a.startswith("172.17."), f"docker 网桥混进清单: {a}")
            self.assertFalse(a.startswith("169.254."), f"link-local 混进清单: {a}")

    def test_covers_lan_and_tailscale(self):
        """本机实测有局域网 192.168.5.102 与 Tailscale 100.x —— 两族都要在清单里，
        否则「从局域网进 / 从 Tailscale 进」必有一边换不到地址。"""
        addrs = profiles.own_addrs()
        self.assertTrue(any(a.startswith("192.168.") for a in addrs), addrs)
        self.assertTrue(any(a.startswith("100.") for a in addrs), addrs)


CASES = [
    # ① 核心故障形态：从 Tailscale 访问，卡片写的是局域网地址
    {"self": ["192.168.5.102", "100.117.232.62"],
     "host": "100.117.232.62",
     "url": "http://192.168.5.102:3080/?token=abc-123",
     "want": "http://100.117.232.62:3080/?token=abc-123"},
    # ② 从局域网访问：同一张卡不该被改写（否则会把局域网用户推去走隧道）
    {"self": ["192.168.5.102", "100.117.232.62"],
     "host": "192.168.5.102",
     "url": "http://192.168.5.102:3080/?token=abc-123",
     "want": "http://192.168.5.102:3080/?token=abc-123"},
    # ③ 回环语义保留（cloudcli / pi / workbuddy 的 ui 都是 127.0.0.1）
    {"self": ["192.168.5.102", "100.117.232.62"],
     "host": "100.117.232.62",
     "url": "http://127.0.0.1:3010/",
     "want": "http://100.117.232.62:3010/"},
    # ④ Tailscale IPv6（浏览器 location.hostname 可能带方括号，也可能不带）
    {"self": ["192.168.5.102", "fd7a:115c:a1e0::2c37:e83f"],
     "host": "fd7a:115c:a1e0::2c37:e83f",
     "url": "http://192.168.5.102:3080/",
     "want": "http://[fd7a:115c:a1e0::2c37:e83f]:3080/"},
    {"self": ["192.168.5.102", "fd7a:115c:a1e0::2c37:e83f"],
     "host": "[fd7a:115c:a1e0::2c37:e83f]",
     "url": "http://192.168.5.102:3080/",
     "want": "http://[fd7a:115c:a1e0::2c37:e83f]:3080/"},
    # ⑤ 前缀相同不得误伤（192.168.5.10 vs 192.168.5.102）
    {"self": ["192.168.5.10"], "host": "100.117.232.62",
     "url": "http://192.168.5.102:3080/", "want": "http://192.168.5.102:3080/"},
    # ⑥ 外站链接不动
    {"self": ["192.168.5.102"], "host": "192.168.5.102",
     "url": "https://github.com/gztxt/agenthub", "want": "https://github.com/gztxt/agenthub"},
    # ⑦ 路径/端口/查询串原样保留（反代与 token 依赖）
    {"self": ["192.168.5.102"], "host": "100.117.232.62",
     "url": "http://192.168.5.102:3458/?ccr_web_token=x#frag",
     "want": "http://100.117.232.62:3458/?ccr_web_token=x#frag"},
]


class LanUrlRewriteTest(unittest.TestCase):
    def setUp(self):
        self.probe = os.path.join(ROOT, "tests", "net_url_probe.js")
        self.shard = os.path.join(ROOT, "static", "hub", "01-core-boot.js")
        if shutil.which("node") is None:
            self.skipTest("node 不在 PATH")

    def test_rewrites_by_current_network(self):
        r = subprocess.run(
            ["node", self.probe, self.shard, json.dumps(CASES)],
            capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(r.returncode, 0, f"lanUrl 行为不符：\n{r.stdout}\n{r.stderr}")
        rows = json.loads(r.stdout)
        self.assertEqual(len(rows), len(CASES))
        bad = [x for x in rows if not x["ok"]]
        self.assertEqual(bad, [])


if __name__ == "__main__":
    unittest.main()
