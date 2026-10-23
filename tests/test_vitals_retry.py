"""v0.13.4 L4 探活预算闸门单测：一轮最多 RT_MAX_TRIES(默认 2) 次即停。

来源：2026-09-23 用户裁定「探活测试 2 次即结束，不要反复频繁探测」。
改前的缺陷：失败态（timeout / rate_limited / model_unsupported / no_output）完全不设
保鲜，于是每轮 sweep（900s ⇒ 96 轮/天）都重烧一次真请求 —— 实测 09-22 16:45→09-23 07:37
单 claude 一家连烧 68 次，单次峰值 RSS 270MB，而本机 swap 已用 90%。

跑法：cd ~/agent-hub && venv/bin/python -m unittest tests.test_vitals_retry -v
全 mock，不起子进程、不打 CCR、不打外部判定端点、不碰生产 data/vitals.json。
"""
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
# vitals.py 内部是裸导入（`import profiles` 等），生产上由 uvicorn 把 src/ 放进 sys.path；
# 单测自己补上这一条，不能依赖服务的启动方式
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from src import vitals as V  # noqa: E402

# 外部条件态（应当计预算的四种）
FLAKY = ("timeout", "rate_limited", "model_unsupported", "no_output")

# 让 rule_verdict 走 usable、且满足 needs_rt 前置的取证快照
GOOD_EV = {"agent_shape": "terminal-cli", "resolved": True,
           "version_output": "1.2.3", "version_rc": 0, "help_rc": 0,
           "help_output": "usage: x"}

PROF = {"id": "t-cli", "verify_argv": ["x", "-p", "{p}"]}


def needs(v, rt):
    """把一条 rt 记录塞进去，问 needs_rt。"""
    with v._lock:
        v._rt[PROF["id"]] = rt
    return v.needs_rt(PROF, dict(GOOD_EV))


class RetryBudgetGate(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # STATE_PATH 全程钉到临时件：_load/_save 都不许碰生产 data/vitals.json
        patcher = mock.patch.object(V, "STATE_PATH", Path(self.tmp.name) / "vitals.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.v = V.Vitals()
        self.now = time.time()

    # ── 决策面：needs_rt 什么时候才允许花一次真请求 ────────────────
    def test_first_probe_allowed(self):
        self.assertTrue(needs(self.v, {}), "无历史记录时应允许首次探测")

    def test_one_try_left_still_probes(self):
        rt = {"rt_state": "model_unsupported", "tries": 1, "at": self.now}
        self.assertTrue(needs(self.v, rt), "预算未用完（1/2）应允许再试一次")

    def test_budget_exhausted_stops(self):
        """核心：用完 2 次即停 —— 这就是「不要反复频繁探测」的落点。"""
        rt = {"rt_state": "model_unsupported", "tries": 2, "at": self.now}
        self.assertFalse(needs(self.v, rt), "已试满 2 次必须停手，不得每 15min 继续重烧")

    def test_all_four_external_states_share_budget(self):
        for st in FLAKY:
            with self.subTest(state=st):
                rt = {"rt_state": st, "tries": 2, "at": self.now}
                self.assertFalse(needs(self.v, rt), "%s 也应受 2 次预算约束" % st)

    def test_window_expiry_reopens_budget(self):
        """停手不等于永久放弃：过了保鲜窗重新给一整轮，自愈不断线。"""
        rt = {"rt_state": "model_unsupported", "tries": 2, "at": self.now - V.Vitals.RT_TTL - 1}
        self.assertTrue(needs(self.v, rt), "保鲜窗过期后应重开一轮")

    def test_answered_not_reprobed(self):
        self.assertFalse(needs(self.v, {"rt_state": "answered", "tries": 0, "at": self.now}),
                         "成功态应吃保鲜，不该再烧")

    def test_answered_reprobed_after_ttl(self):
        self.assertTrue(needs(self.v, {"rt_state": "answered", "tries": 0,
                                       "at": self.now - V.Vitals.RT_TTL - 1}))

    def test_account_blocked_never_spins(self):
        """额度耗尽属账号侧，既不重烧也不该被预算逻辑放大。"""
        self.assertFalse(needs(self.v, {"rt_state": "blocked_by_account", "tries": 0,
                                        "at": self.now}))

    def test_missing_tries_field_is_compatible(self):
        """老 state 文件没有 tries 字段：不得 KeyError/TypeError，按 0 处理并继续允许。"""
        self.assertTrue(needs(self.v, {"rt_state": "timeout", "at": self.now - 60}))

    def test_budget_configurable(self):
        """预算走 env（VITALS_RT_MAX_TRIES），改 1 就 1 次即停 —— 不写死在代码里。"""
        rt = {"rt_state": "timeout", "tries": 1, "at": self.now}
        with mock.patch.object(V, "RT_MAX_TRIES", 1):
            self.assertFalse(needs(self.v, rt), "上限配成 1 时试满 1 次即停")
        with mock.patch.object(V, "RT_MAX_TRIES", 5):
            self.assertTrue(needs(self.v, rt), "上限配成 5 时仍有余量")

    def test_fake_card_spends_nothing(self):
        """假卡/坏卡连首次都不给（改前改后一致，防回归）。"""
        ev = dict(GOOD_EV, version_rc=127)
        with self.v._lock:
            self.v._rt[PROF["id"]] = {}
        self.assertFalse(self.v.needs_rt(PROF, ev), "未安装的不该花 token")


class RetryCountingInJudge(unittest.TestCase):
    """记账面：judge(do_roundtrip=True) 必须把 tries 算对，否则闸门无数据可依。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        # 同上：judge 会 _save，必须把落盘导向临时件，不得污染生产状态
        patcher = mock.patch.object(V, "STATE_PATH", Path(self.tmp.name) / "vitals.json")
        patcher.start()
        self.addCleanup(patcher.stop)
        self.v = V.Vitals()

    def run_once(self, state, prof=PROF):
        """跑一次 L4。走 verify() 而不是裸 judge()：judge() 不落盘，
        生产上落盘发生在 verify()/sweep() 末尾——要验“重启不失忆”必须走带 _save 的真实路径。"""
        ev = dict(GOOD_EV, agent_id=prof["id"],
                  evidence_sha="sha-%f" % time.time(), core_sha="sha-%f" % time.time(),
                  run_rc=1 if state != "answered" else 0,
                  run_output="boom" if state != "answered" else "好",
                  run_note="note", run_model="m")
        with mock.patch.object(V, "collect", lambda p, d=False: dict(ev)), \
                mock.patch.object(V, "ask_local", lambda s, key=None: {}), \
                mock.patch.object(V, "classify_rt", lambda rc, out: state):
            return self.v.verify(prof)

    def test_two_failures_then_gate_closes(self):
        """一轮内失败真请求恰为 2 次 —— 本条整体等价于「2 次即结束」的行为契约。"""
        self.run_once("model_unsupported")
        self.assertEqual(1, self.v._rt[PROF["id"]]["tries"])
        self.assertTrue(self.v.needs_rt(PROF, dict(GOOD_EV)))

        self.run_once("model_unsupported")
        self.assertEqual(2, self.v._rt[PROF["id"]]["tries"])
        self.assertFalse(self.v.needs_rt(PROF, dict(GOOD_EV)),
                         "第二次失败后闸门必须关闭")

        # 闸门关着时就算被强跑也不该推进成风暴：验证不会被误开
        self.assertFalse(self.v.needs_rt(PROF, dict(GOOD_EV)))
        self.assertEqual(2, self.v._rt[PROF["id"]]["tries"])

    def test_answer_resets_budget(self):
        self.run_once("model_unsupported")
        self.run_once("timeout")
        self.assertEqual(2, self.v._rt[PROF["id"]]["tries"])
        self.run_once("answered")
        rec = self.v._rt[PROF["id"]]
        self.assertEqual(0, rec["tries"], "成功即归零，别把历史失败算进下一轮")
        self.assertEqual("answered", rec["rt_state"])
        self.assertFalse(self.v.needs_rt(PROF, dict(GOOD_EV)), "成功后转保鲜，不再烧")

    def test_new_window_recounts_from_one(self):
        self.run_once("timeout")
        self.run_once("timeout")
        self.assertEqual(2, self.v._rt[PROF["id"]]["tries"])
        with self.v._lock:
            self.v._rt[PROF["id"]]["at"] = time.time() - V.Vitals.RT_TTL - 5
        self.assertTrue(self.v.needs_rt(PROF, dict(GOOD_EV)))
        self.run_once("timeout")
        self.assertEqual(1, self.v._rt[PROF["id"]]["tries"], "跨窗后是新的一轮，应从 1 重计")

    def test_per_agent_budget_isolated(self):
        """预算按 agent 独立：一家打不通不该把别家饿死。"""
        other = {"id": "t-other", "verify_argv": ["y", "-p", "{p}"]}
        self.run_once("timeout", PROF)
        self.run_once("timeout", PROF)
        self.assertFalse(self.v.needs_rt(PROF, dict(GOOD_EV)))
        self.run_once("timeout", other)
        self.assertEqual(1, self.v._rt["t-other"]["tries"])
        self.assertTrue(self.v.needs_rt(other, dict(GOOD_EV)))

    def test_state_roundtrip_persists_tries(self):
        """tries 必须落盘，否则 hub 重启即失忆 ⇒ 重启后又是一轮风暴。"""
        self.run_once("rate_limited")
        self.run_once("rate_limited")
        self.assertTrue(Path(V.STATE_PATH).exists(), "前置：确认真的发生过落盘")
        v2 = V.Vitals()
        self.assertEqual(2, v2._rt[PROF["id"]]["tries"], "重启后仍应记得已烧过 2 次")
        self.assertFalse(v2.needs_rt(PROF, dict(GOOD_EV)))

    def test_flaky_flag_still_present_for_ui(self):
        """外部条件的展示语义（rt_flaky）不因预算改动而丢失。"""
        self.run_once("timeout")
        self.run_once("timeout")
        with self.v._lock:
            self.v._rt[PROF["id"]]["at"] = time.time() - 5
        rec = self.run_once("timeout")
        self.assertTrue(rec.get("rt_flaky") or rec.get("rt_state") == "timeout",
                        "失败态仍要能被识别成外部条件，而非本机故障")


if __name__ == "__main__":
    unittest.main(verbosity=2)
