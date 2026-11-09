#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""连接分级探测（借鉴 t3code connection-runtime 思路）的 L0 闸门。

L0 口径：不 import src.main、不起服务、不打网络。
两层判据，各能被单独打回：
1. 结构不变量：用 Python 读改后 static/hub/03-agents-cards.js 源码做断言
   —— 分级阈值命名常量、pickProbeTimeoutMs 纯函数、visibilitychange 先探测、
   离线守卫、退避 5 分钟上限 + 抖动、保留现有指数退避档位。
2. 数值判据：把源码里 conn-probe:pure-begin / pure-end 之间的纯块抽出来，
   交给 node -e 加载执行，跑 LAN/隧道分级、抖动上限、离线重试预算三组数值断言。
   node 不可用时这些用例 skip 并写明原因，不拿别的替代充当通过。

「纯块可抽取」本身是设计约束：命名常量/纯函数必须能从源码机械定位，便于静态测试。
"""

import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
JS = (ROOT / "static" / "hub" / "03-agents-cards.js").read_text(encoding="utf-8")

PURE_BEGIN = "/* conn-probe:pure-begin */"
PURE_END = "/* conn-probe:pure-end */"

NODE = shutil.which("node")


def _pure_block():
    """抽出纯块（常量 + 纯函数）；缺标记即让测试失败，不静默退化成空串。"""
    assert PURE_BEGIN in JS, "源码丢了 conn-probe:pure-begin 标记"
    assert PURE_END in JS, "源码丢了 conn-probe:pure-end 标记"
    return JS.split(PURE_BEGIN, 1)[1].split(PURE_END, 1)[0]


def _run_node(expr):
    """把纯块 + expr 拼成一段 node -e 代码执行，返回 (returncode, 输出文本)。"""
    code = _pure_block() + "\n" + expr
    p = subprocess.run(
        ["node", "-e", code],
        capture_output=True, text=True, timeout=30,
    )
    return p.returncode, (p.stdout + p.stderr).strip()


class TestConnProbeStructure(unittest.TestCase):
    """结构不变量：纯源码断言，不依赖 node。"""

    def test_pure_block_is_extractable(self):
        block = _pure_block()
        self.assertIn("function pickProbeTimeoutMs", block)
        self.assertIn("function pickBackoffMs", block)
        self.assertIn("function connNextAttempt", block)
        # 纯块不该在加载期读 DOM / 注册监听（否则 node -e 抽出来会炸）
        self.assertNotIn("document.addEventListener", block)
        self.assertNotIn("window.addEventListener", block)

    def test_graded_timeout_constants_and_pure_fn(self):
        self.assertIn("const CONN_PROBE_LAN_MS = 3000;", JS, "缺 LAN 3s 分级常量")
        self.assertIn("const CONN_PROBE_TUNNEL_MS = 15000;", JS, "缺隧道 15s 分级常量")
        self.assertIn("function pickProbeTimeoutMs(host)", JS, "缺按来源选阈值的纯函数")
        self.assertIn("function connIsTunnelHost(host)", JS, "缺连接来源档判据")
        self.assertIn("return connIsTunnelHost(host) ? CONN_PROBE_TUNNEL_MS : CONN_PROBE_LAN_MS;", JS,
                      "pickProbeTimeoutMs 必须按档返回两个命名常量，不许写死单一值")

    def test_backoff_cap_jitter_and_keep_exponential(self):
        self.assertIn("const CONN_RC_MAX_MS = 300000;", JS, "缺 5 分钟退避上限常量")
        self.assertIn("function pickBackoffMs(baseMs, rand)", JS, "缺抖动/封顶纯函数")
        self.assertIn("CONN_RC_JITTER_RATIO", JS, "缺抖动幅度常量")
        # 保留现有指数退避：仍从 TERM_RC_DELAYS 取基数，不推翻重写
        self.assertIn("TERM_RC_DELAYS[Math.min(termRcAttempt, TERM_RC_DELAYS.length - 1)]", JS,
                      "现有指数退避档位被改掉了")
        self.assertIn("const delay = pickBackoffMs(base, Math.random());", JS,
                      "退避延迟未走抖动 + 封顶的纯函数")

    def test_visibilitychange_probes_before_reconnect(self):
        # 回前台监听存在，且走 termProbeThenWake（先探测），不是立刻 termLinkWake 全量重连
        self.assertIn(
            "document.addEventListener('visibilitychange', () => { if (!document.hidden) termProbeThenWake(); });",
            JS, "visibilitychange 未改为先探测")
        self.assertIn("function termProbeThenWake()", JS, "缺前台探测入口")
        # 探测 = 发一帧 hb，超时阈值按连接来源分级
        self.assertIn("pickProbeTimeoutMs(typeof location", JS, "探测超时未按 location 来源分级")
        self.assertIn("termHbSend(ws);", JS, "探测未发 hb 帧")
        # 只有探测超时（无新回执）才走全量重连；回执来了就撤销探测定时器
        self.assertIn("termHbFail(ws, probeMs);", JS, "探测超时未接全量重连出口")
        self.assertIn("let termProbeTimer = null;", JS, "缺唯一探测定时器状态位")
        # online 事件仍走原唤醒路（真实恢复网络不做先探测）
        self.assertIn("window.addEventListener('online', termLinkWake);", JS)

    def test_offline_guard(self):
        # 离线判据是纯块里的 connOnline(nav)：nav.onLine === false
        self.assertIn("function connOnline(nav) { return !(nav && nav.onLine === false); }", JS,
                      "缺 navigator.onLine 离线判据")
        # 运行时取数口把 navigator 喂给纯函数
        self.assertIn("return connOnline(typeof navigator !== 'undefined' ? navigator : null);", JS,
                      "connIsOnline 未取 navigator.onLine")
        self.assertIn("navigator.onLine === false", JS, "源码/注释未标明离线判据")
        # 退避计数推进受在线状态守卫：offline 不累计进退避
        self.assertIn("const online = connIsOnline();", JS)
        self.assertIn("termRcAttempt = connNextAttempt(termRcAttempt, online);", JS,
                      "重试计数未改走离线守卫")
        self.assertIn("function connRetryAllowed(online) { return online !== false; }", JS)

    def test_probe_timer_bounded(self):
        # 探测定时器在心跳停止 / 回执 / 重排时都要清空，避免开出第二个 socket
        self.assertGreaterEqual(
            JS.count("clearTimeout(termProbeTimer); termProbeTimer = null;"), 2,
            "探测定时器缺少至少两处（回执/停止）清理")


@unittest.skipUnless(NODE, "node 不可用：数值判据未验证（非通过）")
class TestConnProbeNumbers(unittest.TestCase):
    """数值判据：抽出纯块交 node -e 执行，缺 node 则整类 skip 并写明原因。"""

    # 纯函数放在 node -e 里跑，任何一条不成立就 process.exit(1)。
    _ASSERTS = r"""
function ok(cond, msg) { if (!cond) { console.error('FAIL: ' + msg); process.exit(1); } }

// 分级阈值常量
ok(CONN_PROBE_LAN_MS === 3000, 'LAN 阈值应为 3000ms');
ok(CONN_PROBE_TUNNEL_MS === 15000, '隧道阈值应为 15000ms');
ok(CONN_RC_MAX_MS === 300000, '退避上限应为 300000ms(5min)');

// LAN 档不误判
ok(pickProbeTimeoutMs('localhost') === 3000, 'localhost 应判 LAN');
ok(pickProbeTimeoutMs('127.0.0.1') === 3000, '127.0.0.1 应判 LAN');
ok(pickProbeTimeoutMs('[::1]') === 3000, '[::1] 应判 LAN');
ok(pickProbeTimeoutMs('192.168.1.10') === 3000, '192.168.* 应判 LAN');
ok(pickProbeTimeoutMs('10.0.0.5') === 3000, '10.* 应判 LAN');
ok(pickProbeTimeoutMs('172.20.1.1') === 3000, '172.16-31.* 应判 LAN');
ok(pickProbeTimeoutMs('box.local') === 3000, '.local 应判 LAN');

// 隧道档不误判
ok(pickProbeTimeoutMs('myhost.ts.net') === 15000, '*.ts.net 应判隧道');
ok(pickProbeTimeoutMs('foo.tailnet') === 15000, '*.tailnet 应判隧道');
ok(pickProbeTimeoutMs('100.64.1.2') === 15000, 'Tailscale CGNAT 应判隧道');
ok(pickProbeTimeoutMs('example.com') === 15000, '公网域名应判隧道');

// LAN/隧道切换不误判：纯函数无状态，交错调用结果不串档
ok(pickProbeTimeoutMs('192.168.1.1') === 3000 &&
   pickProbeTimeoutMs('x.ts.net') === 15000 &&
   pickProbeTimeoutMs('192.168.1.1') === 3000, 'LAN/隧道交错不串档');

// 抖动 ±20% 与 5 分钟硬上限
ok(pickBackoffMs(1000, 0) === 800, 'rand=0 应 -20%');
ok(pickBackoffMs(1000, 0.5) === 1000, 'rand=0.5 应不偏');
ok(pickBackoffMs(1000, 1) === 1200, 'rand=1 应 +20%');
ok(pickBackoffMs(999999999, 0.5) === CONN_RC_MAX_MS, '超大基数被 5min 上限截断');
ok(pickBackoffMs(30000, 1) <= CONN_RC_MAX_MS, '现有最大档 + 抖动仍不超上限');

// 离线守卫：拔网（多次失败）不烧预算，恢复后重试 ≤2
ok(connOnline({onLine: false}) === false, 'onLine=false 判离线');
ok(connOnline({onLine: true}) === true, 'onLine=true 判在线');
ok(connRetryAllowed(false) === false && connRetryAllowed(true) === true, '离线不许推进重试');
var a = 0;
for (var i = 0; i < 20; i++) a = connNextAttempt(a, false);  // 离线期间 20 次失败
ok(a === 0, '离线失败不累计进退避计数');
a = connNextAttempt(a, true);  // 恢复后第 1 次
a = connNextAttempt(a, true);  // 第 2 次
ok(a <= 2, '拔网恢复重试 <=2（预算未被离线失败烧高）');

console.log('ALL_NODE_ASSERTIONS_OK');
"""

    def test_node_numeric_assertions(self):
        rc, out = _run_node(self._ASSERTS)
        self.assertEqual(rc, 0, "node 数值断言失败：\n" + out)
        self.assertIn("ALL_NODE_ASSERTIONS_OK", out)


if __name__ == "__main__":
    unittest.main(verbosity=2)
