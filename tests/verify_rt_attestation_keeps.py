#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""闸门：一次 L4 失败不得作废已有的 answered 凭据（v0.13.15）

背景（2026-09-24 实测）：claude 走 CCR 在 RT_TIMEOUT=40s 下跑出一例 run_rc=124
"TIMEOUT after 40.0s"，同一个 Agent 稍后又 answered。原实现会把 _rt[aid] 覆盖成
timeout，而之后每轮 L2 sweep 都从 _rt 复原 ⇒ 卡片由「可用」退回「未实测」，
用户实测却能用 —— 瞬时抖动被固化成永久降级。

断言四格：
  A 有旧凭据 + 本轮超时  → 保持 answered，且失败留痕（last_fail）
  B 下一轮纯 L2         → 仍复原为 answered（不被上轮 timeout 污染）
  C 无旧凭据 + 本轮超时  → 如实 timeout（不得有无条件护短）
  D discovery 视角      → attested 必须为 True（绿标不被摘）

用法：cd /fs/1000/ftp/技术文档/agenthub && python3 tests/verify_rt_attestation_keeps.py
退出码 0 = 全绿；非 0 = 有 FAIL。
"""
import os
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "src"))

import vitals  # noqa: E402


def fake_evidence(state, sha):
    """合成一份证据：L1/L2 自检正常，L4 结果由 state 决定"""
    ev = {"agent_id": "probe", "agent_shape": "terminal-cli", "resolved": True,
          "resolved_name": "probe", "resolved_path": "/bin/true", "resolved_at": "/bin/true",
          "version_rc": 0, "version_output": "probe 1.2.3", "help_rc": 0, "help_output": "usage",
          "evidence_sha": sha, "core_sha": sha}
    if state == "answered":
        ev.update(run_rc=0, run_output="好", run_model="m")
    elif state == "timeout":
        ev.update(run_rc=124, run_output="TIMEOUT after %ss" % int(vitals.RT_TIMEOUT),
                  run_model="m")
    return ev


def make_v():
    v = vitals.Vitals.__new__(vitals.Vitals)
    import threading
    v._lock = threading.Lock()
    v._by_sha, v._latest, v._rt = {}, {}, {}
    v.last_sweep = 0
    return v


PROF = {"id": "probe", "kind": "agent", "cli": "probe", "verify_argv": ["probe", "{p}"]}
results = []


def check(name, cond, detail=""):
    results.append((name, bool(cond), detail))
    print("  [%s] %s%s" % ("PASS" if cond else "FAIL", name, ("  — " + detail) if detail else ""))


def main():
    # 打桩：collect 走合成证据；ask_local 不跑本地模型（本测只验状态机，不验判定）
    state = {"rt": "timeout", "sha": "s0"}
    vitals.collect = lambda prof, do_roundtrip=False: fake_evidence(
        state["rt"] if do_roundtrip else "ok", state["sha"])
    vitals.ask_local = lambda ev, key=None: {}
    state["rt"] = "answered"

    print("A) 先建立 answered 凭据（1 小时前）")
    v = make_v()
    state["sha"] = "s-attest"
    rec0 = v.judge(PROF, do_roundtrip=True)
    check("A1 初次 L4 answered → rt_state=answered", rec0.get("rt_state") == "answered",
          str(rec0.get("rt_state")))
    v._rt["probe"]["at"] = time.time() - 3600          # 凭据时刻回拨 1h，仍在 RT_TTL 窗内
    attest_at = v._rt["probe"]["at"]

    print("\nB) 本轮 L4 超时（有旧凭据）")
    state["rt"], state["sha"] = "timeout", "s-fail"
    rec1 = v.judge(PROF, do_roundtrip=True)
    check("B1 凭据保持 answered（不降级）", rec1.get("rt_state") == "answered",
          str(rec1.get("rt_state")))
    check("B2 失败照留痕 last_fail=timeout", v._rt["probe"].get("last_fail") == "timeout",
          str(v._rt["probe"].get("last_fail")))
    check("B3 凭据时刻不被虚增", v._rt["probe"]["at"] == attest_at,
          "kept=%s want=%s" % (v._rt["probe"]["at"], attest_at))
    check("B4 rec 带 rt_note 说明沿用", "沿用" in str(rec1.get("rt_note")), str(rec1.get("rt_note"))[:60])

    print("\nC) 下一轮纯 L2 sweep（不烧 token）")
    state["rt"], state["sha"] = "answered", "s-l2"     # do_roundtrip=False 时 collect 不带 L4
    rec2 = v.judge(PROF, do_roundtrip=False)
    check("C1 复原仍是 answered（未被上轮 timeout 污染）", rec2.get("rt_state") == "answered",
          str(rec2.get("rt_state")))

    print("\nD) discovery 视角：绿标")
    try:
        import discovery
    except Exception as e:  # noqa: BLE001  缺依赖时降级为 SKIP（不得当 PASS）
        view = None
        check("D0 discovery 可导入", False, "SKIP：%s: %s" % (type(e).__name__, str(e)[:80]))
    else:
        # 真实签名：_verdict_view(profile, vitals_record)
        view = discovery._verdict_view(PROF, v.snapshot()["probe"])
        check("D1 找到 discovery 的 vitals 视图函数", view is not None, str(view)[:80])
        if view is not None:
            check("D2 attested=True（卡片不被退回「未实测」）", view.get("attested") is True,
                  "attested=%s usable=%s rt=%s" % (view.get("attested"), view.get("usable"),
                                                   view.get("rt_state")))

    print("\nE) 反向：无旧凭据时超时不得被护短")
    v2 = make_v()
    state["rt"], state["sha"] = "timeout", "s-alone"
    rec3 = v2.judge(PROF, do_roundtrip=True)
    check("E1 首次就超时 → rt_state=timeout（如实）", rec3.get("rt_state") == "timeout",
          str(rec3.get("rt_state")))
    check("E2 且没有伪 answered", v2._rt["probe"].get("rt_state") == "timeout")

    bad = [n for n, ok, _ in results if not ok]
    print("\n" + "=" * 58)
    print("总计 %d 项：PASS %d / FAIL %d" % (len(results), len(results) - len(bad), len(bad)))
    if bad:
        print("FAIL 明细:", ", ".join(bad))
    print("=" * 58)
    return 1 if bad else 0


if __name__ == "__main__":
    sys.exit(main())
