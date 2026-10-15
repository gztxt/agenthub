#!/usr/bin/env python3
"""L0 hermetic：纳管边界的四维分级（P0-5；对标手册 C12「强制边界 vs 观测边界」）。

【这条判据治什么】
旧账「纳管 ≠ 可用 / 只读未证」：文案里写「只读」零成本，于是
「我没看见它写过」经常被写成「它写不了」。手册给的可检验判据是：
只有**同时**满足 ① 覆盖全部绕过路径 ② fail-closed ③ 有负向测试证明绕过被拒，
才可称**强制边界**；缺任何一条 ⇒ 文字上只能写「未观察到写操作」。

【本文件守六条】
1. 四维枚举恰好四个（强制/观测 × 覆盖/未覆盖），一个不多一个不少。
2. **未过负向测试的项只能写「未观察到写操作」，不得写强断言** ——
   把 enforced 声明的绕过面抽掉，classify 必须降级。
3. **`covered` / `fail_closed` 不是手填布尔**（2026-10-15 改判）：它们由绕过面清单
   机械导出，清单空 / 任一节点 AST 解析不到 ⇒ 自动不算 covered。
4. **label 按 kind 取词**：`config_write`⇒「只读」、`exec`⇒「受闸」，
   不再对 exec 边界冒充「只读」。
5. 每条 enforced 声明都挂着**真实存在**（AST 可解析到 class::method）的负向测试；
   且绕过面 id 集合**冻结**，静默增删即红。
6. 绕过路径真被拒（调被测代码，不是读注释）。

分层口径（tests/tiers.py）：不 fork pty、不起服务、不连网、**不允许 SKIP**。
"""
import ast
import asyncio
import os
import sys
import types
import unittest
from pathlib import Path

_REPO = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(_REPO / "src"))
sys.path.insert(0, str(_REPO / "tests"))
os.environ.setdefault("TERM_TOKEN", "unit-test-token-not-production")

import boundary  # noqa: E402
import profiles  # noqa: E402
import scanner  # noqa: E402
import writeauth  # noqa: E402

FOUR = {"enforced_covered", "enforced_uncovered",
        "observed_covered", "observed_uncovered"}

#: 绕过面 id 的冻结集（静默增删绕过路径会在这里红）
FROZEN_SURFACE_IDS = {
    "exec": {"agent_without_terminal_entry", "unknown_agent", "bad_token"},
    "config_write": {"missing_secret", "wrong_secret", "empty_secret"},
}


def _classes_in(path: Path) -> dict:
    """{类名: {方法名…}} —— 用于独立证明 proof 登记的测试节点真实存在。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out[node.name] = {n.name for n in node.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return out


def _proof_node_exists(test_id: str) -> bool:
    """`tests/xxx.py::Class::method` 或 `tests/xxx.py` 是否解析得到（本文件独立实现，
    不复用 boundary 的同名函数，避免自证）。"""
    parts = test_id.split("::")
    f = _REPO / parts[0]
    if not f.is_file():
        return False
    if len(parts) == 1:
        return True
    classes = _classes_in(f)
    return len(parts) >= 2 and parts[1] in classes and parts[2] in classes[parts[1]]


def _all_test_ids(kind: str) -> list:
    spec = boundary.NEGATIVE_PROOFS[kind]
    ids = [e["test"] for e in spec.get("surface", [])]
    if spec.get("fail_closed_by"):
        ids.append(spec["fail_closed_by"])
    return ids


class TestFourDimEnum(unittest.TestCase):
    """四维枚举是契约：少一维 = 有格子被吞；多一维 = 有非法分类能混进来。"""

    def test_exactly_four_cells(self):
        self.assertEqual(set(boundary.BOUNDARY_CLASSES), FOUR)

    def test_contract_labels(self):
        self.assertEqual(boundary.LABEL_READONLY, "只读")
        self.assertEqual(boundary.LABEL_GATED, "受闸")
        self.assertEqual(boundary.LABEL_OBSERVED, "未观察到写操作")
        self.assertEqual(set(boundary.LABELS),
                         {"只读", "受闸", "受控", "未观察到写操作"})

    def test_label_is_taken_by_kind(self):
        """label 按 kind 取词：exec 不得冒充「只读」。"""
        self.assertEqual(boundary.LABEL_BY_KIND["config_write"], "只读")
        self.assertEqual(boundary.LABEL_BY_KIND["exec"], "受闸")


class TestLabelGate(unittest.TestCase):
    """核心不变量：强断言只能由负向测试（绕过面）换取。"""

    def test_enforced_without_proof_downgrades_and_never_overclaims(self):
        claims = {"some_unproven_kind": {"mode": "enforced", "coverage": "covered"}}
        out = boundary.classify_claims(claims)
        cell = out["kinds"]["some_unproven_kind"]
        self.assertEqual(cell["boundary_class"], "observed_uncovered")
        self.assertEqual(cell["label"], boundary.LABEL_OBSERVED)
        self.assertNotIn("只读", out["label"])
        self.assertNotIn("受闸", out["label"])
        self.assertIsNone(cell["proof"], "没有负向测试就不该有 proof 可挂")

    def test_config_write_full_surface_says_readonly(self):
        kind = "config_write"
        out = boundary.classify_claims({kind: {"mode": "enforced", "coverage": "covered"}})
        cell = out["kinds"][kind]
        self.assertEqual(cell["boundary_class"], "enforced_covered")
        self.assertEqual(cell["label"], boundary.LABEL_READONLY)
        self.assertEqual(set(cell["proof"]), set(_all_test_ids(kind)))

    def test_exec_full_surface_says_gated_not_readonly(self):
        kind = "exec"
        out = boundary.classify_claims({kind: {"mode": "enforced", "coverage": "covered"}})
        cell = out["kinds"][kind]
        self.assertEqual(cell["boundary_class"], "enforced_covered")
        self.assertEqual(cell["label"], boundary.LABEL_GATED)
        self.assertNotIn("只读", cell["label"], "exec 边界不是「文件只读」")

    def test_missing_surface_node_downgrades(self):
        """绕过面里有条路径的测试节点不存在 ⇒ 不算覆盖全路径 ⇒ 强制但未覆盖。"""
        kind = "config_write"
        spec = boundary.NEGATIVE_PROOFS[kind]
        partial = {kind: {
            "surface": list(spec["surface"]) + [
                {"id": "ghost_path", "test": "tests/test_boundary.py::Nope::test_nope"}],
            "fail_closed_by": spec["fail_closed_by"]}}
        out = boundary.classify_claims({kind: {"mode": "enforced", "coverage": "covered"}},
                                       proofs=partial)
        cell = out["kinds"][kind]
        self.assertEqual(cell["boundary_class"], "enforced_uncovered")
        self.assertEqual(cell["label"], boundary.LABEL_OBSERVED)

    def test_empty_surface_is_not_covered(self):
        kind = "config_write"
        empty = {kind: {"surface": [],
                        "fail_closed_by": boundary.NEGATIVE_PROOFS[kind]["fail_closed_by"]}}
        out = boundary.classify_claims({kind: {"mode": "enforced", "coverage": "covered"}},
                                       proofs=empty)
        self.assertEqual(out["kinds"][kind]["boundary_class"], "enforced_uncovered")

    def test_missing_fail_closed_node_downgrades(self):
        kind = "config_write"
        spec = boundary.NEGATIVE_PROOFS[kind]
        nc = {kind: {"surface": list(spec["surface"]),
                     "fail_closed_by": "tests/test_boundary.py::Nope::test_nope"}}
        out = boundary.classify_claims({kind: {"mode": "enforced", "coverage": "covered"}},
                                       proofs=nc)
        cell = out["kinds"][kind]
        self.assertEqual(cell["boundary_class"], "enforced_uncovered")
        self.assertNotEqual(cell["label"], boundary.LABEL_READONLY)

    def test_observed_covered_is_not_a_strong_label(self):
        """纯观测即便覆盖全部已知路径，也只是「未观察到写操作」，不是「只读」。"""
        out = boundary.classify_claims({"read": {"mode": "observed", "coverage": "covered"}})
        self.assertEqual(out["kinds"]["read"]["boundary_class"], "observed_covered")
        self.assertEqual(out["label"], boundary.LABEL_OBSERVED)

    def test_all_four_cells_reachable(self):
        spec = boundary.NEGATIVE_PROOFS["config_write"]
        got = set()
        got.add(boundary.classify_claims(
            {"config_write": {"mode": "enforced", "coverage": "covered"}}
        )["boundary_class"])
        got.add(boundary.classify_claims(
            {"config_write": {"mode": "enforced", "coverage": "covered"}},
            proofs={"config_write": {"surface": [], "fail_closed_by": spec["fail_closed_by"]}},
        )["boundary_class"])
        got.add(boundary.classify_claims(
            {"read": {"mode": "observed", "coverage": "covered"}})["boundary_class"])
        got.add(boundary.classify_claims(
            {"read": {"mode": "observed", "coverage": "uncovered"}})["boundary_class"])
        self.assertEqual(got, FOUR)


class TestDerivedCoverage(unittest.TestCase):
    """`covered` / `fail_closed` 由绕过面机械导出（不是手填布尔）。"""

    def test_derived_from_registry_is_complete_for_real_kinds(self):
        for kind in boundary.NEGATIVE_PROOFS:
            with self.subTest(kind=kind):
                d = boundary.derive_coverage(kind)
                self.assertTrue(d["covered"], "%s 的绕过面未全部对账" % kind)
                self.assertTrue(d["fail_closed"], "%s 缺 fail-closed 节点" % kind)
                self.assertEqual([], d["missing"])

    def test_ghost_node_makes_covered_false(self):
        d = boundary.derive_coverage("config_write", proofs={"config_write": {
            "surface": [{"id": "x", "test": "tests/test_boundary.py::Nope::test_nope"}],
            "fail_closed_by": boundary.NEGATIVE_PROOFS["config_write"]["fail_closed_by"]}})
        self.assertFalse(d["covered"])
        self.assertEqual(["x"], d["missing"])

    def test_registry_has_no_hand_set_covered_boolean(self):
        """旧形态 `{"covered": True}` 不得再出现在登记表里 —— 那就是被治的形态。"""
        for kind, spec in boundary.NEGATIVE_PROOFS.items():
            with self.subTest(kind=kind):
                self.assertNotIn("covered", spec,
                                 "%s 又变回手填 covered 布尔了" % kind)
                self.assertNotIn("fail_closed", spec,
                                 "%s 又变回手填 fail_closed 布尔了" % kind)


class TestProofRegistryGate(unittest.TestCase):
    """负向测试登记表自身要可对账。"""

    def test_every_surface_and_fail_closed_node_exists(self):
        for kind in boundary.NEGATIVE_PROOFS:
            for test_id in _all_test_ids(kind):
                with self.subTest(kind=kind, test=test_id):
                    self.assertTrue(_proof_node_exists(test_id),
                                    "%s 登记的负向测试 %s 在盘上找不到节点" % (kind, test_id))

    def test_surface_ids_are_frozen(self):
        """静默增删绕过路径 = 悄悄改变「覆盖全路径」的口径 ⇒ 冻结。"""
        for kind, expected in FROZEN_SURFACE_IDS.items():
            with self.subTest(kind=kind):
                got = {e["id"] for e in boundary.NEGATIVE_PROOFS[kind]["surface"]}
                self.assertEqual(expected, got,
                                 "%s 的绕过面清单变了；若确需增删，请同步冻结钉并说明理由" % kind)

    def test_every_enforced_claim_has_a_registered_proof(self):
        """画像里任何 enforced 声明，其 kind 必须在登记表里 —— 否则会静默降级。"""
        missing = []
        for p in profiles.all_profiles(include_blocked=True):
            for kind, claim in profiles.boundary_claims(p).items():
                if claim.get("mode") == "enforced" and kind not in boundary.NEGATIVE_PROOFS:
                    missing.append("%s:%s" % (p["id"], kind))
        self.assertEqual([], missing,
                         "这些 enforced 声明没有负向测试背书（会降级为观测）：%s" % missing)

    def test_scan_products_are_never_enforced(self):
        """扫描产物是第三方服务，agenthub 只能观测 ⇒ 四维里永不出现 enforced_*。"""
        out = boundary.observed_product()
        self.assertTrue(out["boundary_class"].startswith("observed_"),
                        "扫描产物的四维不能是强制：%s" % out["boundary_class"])
        self.assertEqual(out["label"], boundary.LABEL_OBSERVED)


class TestExecBoundaryNegative(unittest.TestCase):
    """绕过路径真被拒：没有终端入口的 agent 拉不起来。

    ⚠ 2026-10-16 修：这两个用例原先送**硬编码占位 token**，期望撞边界闸的 400，
    实际撞的是 token 闸的 401 —— 从来没走到被测的边界判定。
    根因是本文件模块头的 `os.environ.setdefault("TERM_TOKEN", ...)` **是空操作**：
    `src/config.py` 顶部 `load_dotenv(env_path, override=True)` 无条件用 `.env` 覆盖环境，
    `scripts/run_tier.py` 也不清 `TERM_TOKEN` ⇒ 模块级占位值设不进去。
    修法：**用 `term.TERM_TOKEN` 本身**送进去 —— 先过鉴权、再验边界，
    且不再依赖任何环境/顺序假设。token 闸本身由 `test_bad_token_is_rejected` 单独钉。
    """

    def _call(self, agent_id):
        import term
        req = types.SimpleNamespace(
            headers={"x-term-token": term.TERM_TOKEN},
            query_params={})
        return asyncio.run(term.create_session(term.CreateIn(agent_id=agent_id), req))

    def test_unlisted_agent_is_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._call("ccr")                      # gateway 无 terminal 入口
        self.assertEqual(cm.exception.status_code, 400)

    def test_unknown_agent_is_rejected(self):
        from fastapi import HTTPException
        with self.assertRaises(HTTPException) as cm:
            self._call("no-such-agent-xyz")
        self.assertEqual(cm.exception.status_code, 400)

    def test_bad_token_is_rejected(self):
        import term
        from fastapi import HTTPException
        req = types.SimpleNamespace(headers={"x-term-token": "wrong"},
                                    query_params={})
        with self.assertRaises(HTTPException) as cm:
            asyncio.run(term.create_session(term.CreateIn(agent_id="claude"), req))
        self.assertEqual(cm.exception.status_code, 401)


class TestWriteBoundaryNegative(unittest.TestCase):
    """绕过路径真被拒：写端点集中闸门 fail-closed，缺口气/错口气都不放行。"""

    PATH = "/api/tasks/1/retry"

    def test_missing_secret_fails_closed(self):
        verdict, reason = writeauth.decide("POST", self.PATH, "", [])
        self.assertNotEqual(verdict, "allow")
        self.assertEqual(verdict, "misconfig")
        self.assertTrue(reason)

    def test_wrong_secret_denied(self):
        verdict, _ = writeauth.decide("POST", self.PATH, "wrong", ["real"])
        self.assertNotEqual(verdict, "allow")
        self.assertEqual(verdict, "deny")

    def test_empty_secret_is_not_a_secret(self):
        verdict, _ = writeauth.decide("POST", self.PATH, "", ["", None])
        self.assertNotEqual(verdict, "allow")


class TestWiring(unittest.TestCase):
    """四维字段确实接进契约：画像、扫描产物、/api/agents 的 AgentInfo。"""

    def test_profile_carries_boundary_four_dim(self):
        for p in profiles.all_profiles(include_blocked=True):
            b = p.get("boundary")
            self.assertIsNotNone(b, "%s 画像没有 boundary 四维" % p.get("id"))
            self.assertIn(b["boundary_class"], FOUR)
            self.assertIn(b["label"], boundary.LABELS)
            for k in ("mode", "coverage", "boundary_class", "label"):
                self.assertIn(k, b)

    def test_terminal_profiles_are_labelled_gated_or_readonly_not_observed(self):
        """终端型 agent 的强断言按 kind 取词，且绝不落回「未观察到写操作」。"""
        for p in profiles.all_profiles(include_blocked=True):
            if p.get("terminal") and p.get("id") != "shell":
                self.assertEqual(p["boundary"]["boundary_class"], "enforced_covered",
                                 "%s 的 exec 边界未兑换到强制" % p["id"])
                self.assertIn(p["boundary"]["label"], ("受闸", "只读"))

    def test_scan_product_carries_boundary_four_dim(self):
        prod = {"name": "x", "port": 1234}
        scanner.add_boundary(prod)
        self.assertIn(prod["boundary"]["boundary_class"], FOUR)
        self.assertTrue(prod["boundary"]["boundary_class"].startswith("observed_"))

    def test_agent_info_contract_has_boundary(self):
        import discovery
        info = discovery.AgentInfo(
            id="x", name="X", kind="service", status="running",
            endpoint="", port=None, auth_required=False,
            boundary=boundary.observed_product())
        d = info.to_dict()
        self.assertIn("boundary", d)
        for k in ("mode", "coverage", "boundary_class", "label"):
            self.assertIn(k, d["boundary"])


if __name__ == "__main__":
    unittest.main()