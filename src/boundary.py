"""纳管边界的四维分级（P0-5；对标 AI Agent Handbook C12「强制边界 vs 观测边界」）。

【C12 判据，原文语义】
一个边界只有**同时**满足三条，才可称**强制边界**：
    ① 覆盖全部绕过路径（`covered`）
    ② fail-closed —— 判不了就拒，绝不放行（`fail_closed`）
    ③ 有负向测试证明绕过路径被拒（negative proof）
缺任何一条 ⇒ 文字上只能写「未观察到写操作」，**不得写「只读」**。

【四维 = 强制/观测 × 覆盖/未覆盖】
    enforced_covered    强制且覆盖全路径  → label 「只读」
    enforced_uncovered  强制但覆盖不全    → label 「未观察到写操作」
    observed_covered    纯观测但已知全覆盖 → label 「未观察到写操作」
    observed_uncovered  纯观测且覆盖未知  → label 「未观察到写操作」

【为什么把它机械化（而不是靠人写文案）】
旧账「纳管 ≠ 可用 / 只读未证」的根因就是：文案里写「只读」零成本，于是
「我没看见它写过」经常被写成「它写不了」。本模块把这句话变成一条**降级链**：

    claim(enforced)  --登记表里找不到负向测试-->  observed_uncovered
                     --有负向测试但没证明覆盖全路径-->  enforced_uncovered

唯一的升级通道是 `NEGATIVE_PROOFS`：一张「kind → 真实存在的负向测试」登记表。
换不出负向测试的强制声明只能降级 —— 这就是 `tests/test_boundary.py` 钉住的不变量。

【边界种类（agenthub 对 agent 实际存在的纳管面）】
    exec          拉起终端进程（单入口 `term.create_session`，画像白名单 + token 闸）
    config_write  写 agent 配置（走 `writeauth` 集中中间件）
    read          只读探测（发现层；第三方服务路径未知 ⇒ 覆盖不全）
"""
from typing import Dict, Optional

# ── 四维枚举 ─────────────────────────────────────────────────────────
MODE_ENFORCED = "enforced"
MODE_OBSERVED = "observed"
COVERAGE_COVERED = "covered"
COVERAGE_UNCOVERED = "uncovered"

CLASS_ENFORCED_COVERED = "enforced_covered"
CLASS_ENFORCED_UNCOVERED = "enforced_uncovered"
CLASS_OBSERVED_COVERED = "observed_covered"
CLASS_OBSERVED_UNCOVERED = "observed_uncovered"

#: 四维全集（契约：恰好四个，多一个少一个都算改契约）
BOUNDARY_CLASSES = (CLASS_ENFORCED_COVERED, CLASS_ENFORCED_UNCOVERED,
                    CLASS_OBSERVED_COVERED, CLASS_OBSERVED_UNCOVERED)

#: 强度次序（越小越弱）；聚合一条 agent 的多个边界时取**最弱**那条。
_RANK = {
    CLASS_OBSERVED_UNCOVERED: 0,
    CLASS_OBSERVED_COVERED: 1,
    CLASS_ENFORCED_UNCOVERED: 2,
    CLASS_ENFORCED_COVERED: 3,
}

_MODE_OF = {
    CLASS_ENFORCED_COVERED: MODE_ENFORCED,
    CLASS_ENFORCED_UNCOVERED: MODE_ENFORCED,
    CLASS_OBSERVED_COVERED: MODE_OBSERVED,
    CLASS_OBSERVED_UNCOVERED: MODE_OBSERVED,
}
_COVERAGE_OF = {
    CLASS_ENFORCED_COVERED: COVERAGE_COVERED,
    CLASS_ENFORCED_UNCOVERED: COVERAGE_UNCOVERED,
    CLASS_OBSERVED_COVERED: COVERAGE_COVERED,
    CLASS_OBSERVED_UNCOVERED: COVERAGE_UNCOVERED,
}

#: 契约字符串（`tests/test_boundary.py` 逐字钉着，改前先读那条判据）
LABEL_READONLY = "只读"
LABEL_OBSERVED = "未观察到写操作"

# ── 负向测试登记表（强制的唯一兑换券）─────────────────────────────────
#: kind → 证明「绕过被拒」的真实测试节点。
#:   covered     ：该测试证明的路径**覆盖了全部已知绕过面**
#:   fail_closed ：判不了时是拒（不是放行）
#: 没有登记 / 登记了却没写全这两条 ⇒ 强制声明自动降级（见 `classify_claims`）。
#: `tests/test_boundary.py::TestProofRegistryGate` 会到盘上解析 `test` 的 class::method，
#: 登记的测试被删名/改名即红。
NEGATIVE_PROOFS: Dict[str, dict] = {
    "exec": {
        "test": "tests/test_boundary.py::TestExecBoundaryNegative::test_unlisted_agent_is_rejected",
        "covered": True,
        "fail_closed": True,
        "what": "term.create_session 是拉起进程的**唯一入口**：agent 无画像终端入口⇒400、"
                "命令只出自画像模板（tests/test_term_launch_guard.py 另有 AST 钉子）、"
                "token 校验失败⇒401（无 token 即拒，非放行）。",
    },
    "config_write": {
        "test": "tests/test_boundary.py::TestWriteBoundaryNegative::test_missing_secret_fails_closed",
        "covered": True,
        "fail_closed": True,
        "what": "writeauth.decide 集中中间件 + 路由清单静态护栏：未配口令⇒misconfig(503)、"
                "错口令⇒deny(401)，**从不放行**。",
    },
}


def _observed(mode: str, coverage: str, *, detail: str,
              proof: Optional[str] = None) -> dict:
    cls = (CLASS_ENFORCED_COVERED if mode == MODE_ENFORCED and coverage == COVERAGE_COVERED
           else CLASS_ENFORCED_UNCOVERED if mode == MODE_ENFORCED
           else CLASS_OBSERVED_COVERED if coverage == COVERAGE_COVERED
           else CLASS_OBSERVED_UNCOVERED)
    return {
        "mode": mode,
        "coverage": coverage,
        "boundary_class": cls,
        "label": LABEL_READONLY if cls == CLASS_ENFORCED_COVERED else LABEL_OBSERVED,
        "proof": proof,
        "detail": detail,
    }


def classify_claims(claims: Dict[str, dict],
                    proofs: Optional[Dict[str, dict]] = None) -> dict:
    """把「声明的边界」折成四维 —— 核心是**只降不升**。

    claims: {kind: {"mode": enforced|observed, "coverage": covered|uncovered}}
    proofs: 可注入（测试用）；默认 `NEGATIVE_PROOFS`。

    返回 {mode, coverage, boundary_class, label, proof, detail, kinds{…}}；
    `kinds` 是逐 kind 的四维，聚合值取**最弱**那条（一条链的强度＝最弱环）。
    """
    live_proofs = NEGATIVE_PROOFS if proofs is None else proofs
    kinds: Dict[str, dict] = {}
    for kind, claim in (claims or {}).items():
        mode = (claim or {}).get("mode", MODE_OBSERVED)
        coverage = (claim or {}).get("coverage", COVERAGE_UNCOVERED)
        if mode == MODE_ENFORCED:
            proof = live_proofs.get(kind)
            if not proof:
                kinds[kind] = _observed(
                    MODE_OBSERVED, COVERAGE_UNCOVERED,
                    detail="声明强制但登记表里没有该 kind 的负向测试 ⇒ 降级为观测："
                           "只能说「未观察到写操作」，不得写「只读」。")
                continue
            if proof.get("covered") and proof.get("fail_closed"):
                kinds[kind] = _observed(
                    MODE_ENFORCED, COVERAGE_COVERED, proof=proof.get("test"),
                    detail="负向测试已证绕过被拒且覆盖全路径（fail-closed）。")
            else:
                kinds[kind] = _observed(
                    MODE_ENFORCED, COVERAGE_UNCOVERED, proof=proof.get("test"),
                    detail="有负向测试，但未同时证明「覆盖全路径」与「fail-closed」"
                           "⇒ 强制但未覆盖，判据上仍只能说「未观察到写操作」。")
            continue
        kinds[kind] = _observed(
            MODE_OBSERVED, coverage,
            detail="纯观测边界：没有拦截能力，只能说「未观察到写操作」。")

    if not kinds:
        agg = _observed(MODE_OBSERVED, COVERAGE_UNCOVERED, detail="无边界声明。")
    else:
        weakest = min(kinds, key=lambda k: _RANK[kinds[k]["boundary_class"]])
        agg = dict(kinds[weakest])
        agg["detail"] = "聚合取最弱边界（%s）：%s" % (weakest, agg["detail"])

    agg["kinds"] = kinds
    return agg


def claims_for_profile(p: dict) -> Dict[str, dict]:
    """画像 → 声明的边界。**数据驱动**：不手抄 agent 名单，随画像形态自动同步。

    有终端入口 ⇒ 拉起进程这条边界声明为强制（能否真算强制由负向测试兑换）；
    其余（gateway/service/memory/tool，或纯 Web 宿主）⇒ 只观测。
    """
    if p.get("terminal"):
        return {"exec": {"mode": MODE_ENFORCED, "coverage": COVERAGE_COVERED}}
    return {"read": {"mode": MODE_OBSERVED, "coverage": COVERAGE_UNCOVERED}}


def cell_for_profile(p: dict) -> dict:
    """画像 → 聚合成一条四维（供 `profiles._enrich` 挂 `boundary` 字段）。"""
    return classify_claims(claims_for_profile(p))


def observed_product() -> dict:
    """第三方扫描产物 / 自定义服务的四维：只能观测（永不 enforced）。"""
    return _observed(
        MODE_OBSERVED, COVERAGE_UNCOVERED,
        detail="外部发现源：agenthub 只读探测，无拦截能力 ⇒ 只能说「未观察到写操作」。")