#!/usr/bin/env python3
"""L0 hermetic：纳管边界的四维分级（P0-5；对标手册 C12「强制边界 vs 观测边界」）。

【这条判据治什么】
旧账「纳管 ≠ 可用 / 只读未证」：文案里写「只读」零成本，于是
「我没看见它写过」经常被写成「它写不了」。手册给的可检验判据是：
只有**同时**满足 ① 覆盖全部绕过路径 ② fail-closed ③ 有负向测试证明绕过被拒，
才可称**强制边界**；缺任何一条 ⇒ 文字上只能写「未观察到写操作」。

【本文件守四条】
1. 四维枚举恰好四个（强制/观测 × 覆盖/未覆盖），一个不多一个不少。
2. **未过负向测试的项只能写「未观察到写操作」，不得写「只读」** ——
   把 enforced 声明的 proof 抽掉，classify 必须降级且 label 不含「只读」。
3. 每条 enforced 声明都挂着一个**真实存在**（AST 可解析到 class::method）的负向测试；
   强制的四维只能由负向测试换取，不能靠人写一个字面量。
4. 绕过路径真被拒（调被测代码，不是读注释）：
     exec 边界：无终端入口的 agent → `term.create_session` 400；
     config_write 边界：未配口令 → `writeauth.decide` misconfig、错口令 → deny，**从不放行**。

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
import scanner   # noqa: E402
import writeauth  # noqa: E402

FOUR = {"enforced_covered", "enforced_uncovered",
        "observed_covered", "observed_uncovered"}


def _classes_in(path: Path) -> dict:
    """{类名: {方法名…}} —— 用于证明 proof 登记的测试节点真实存在。"""
    tree = ast.parse(path.read_text(encoding="utf-8"))
    out = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.ClassDef):
            out[node.name] = {n.name for n in node.body
                              if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
    return out


def _proof_node_exists(test_id: str) -> bool:
    """`tests/xxx.py::Class::method` 或 `tests/xxx.py` 是否解析得到。"""
    parts = test_id.split("::")
    f = _REPO / parts[0] if (Path(parts[0]).is_absolute()) else _REPO / parts[0]
    if not f.is_file():
        return False
    if len(parts) == 1:
        return True
    classes = _classes_in(f)
    return parts[1] in classes and parts[2] in classes[parts[1]]


class TestFourDimEnum(unittest.TestCase):
    """四维枚举是契约：少一维 = 有格子被吞；多一维 = 有非法分类能混进来。"""

    def test_exactly_four_cells(self):
        self.assertEqual(set(boundary.BOUNDARY_CLASSES), FOUR)

    def test_labels_are_exactly_the_two_contract_strings(self):
        self.assertEqual(boundary.LABEL_READONLY, "只读")
        self.assertEqual(boundary.LABEL_OBSERVED, "未观察到写操作")


class TestLabelGate(unittest.TestCase):
    """核心不变量：只读只能由负向测试换取。"""

    def test_enforced_without_proof_downgrades_and_never_says_readonly(self):
        claims = {"some_unproven_kind": {"mode": "enforced", "coverage": "covered"}}
        out = boundary.classify_claims(claims)
        cell = out["kinds"]["some_unproven_kind"]
        self.assertEqual(cell["boundary_class"], "observed_uncovered")
        self.assertEqual(cell["label"], boundary.LABEL_OBSERVED)
        self.assertNotIn("只读", out["label"])
        self.assertIsNone(cell["proof"], "没有负向测试就不该有 proof 可挂")

    def test_enforced_with_full_proof_becomes_readonly(self):
        kind = "config_write"          # 登记表里有 covered+fail_closed 的真实负向测试
        claims = {kind: {"mode": "enforced", "coverage": "covered"}}
        out = boundary.classify_claims(claims)
        cell = out["kinds"][kind]
        self.assertEqual(cell["boundary_class"], "enforced_covered")
        self.assertEqual(cell["label"], boundary.LABEL_READONLY)
        self.assertEqual(cell["proof"], boundary.NEGATIVE_PROOFS[kind]["test"])

    def test_enforced_with_partial_proof_stays_uncovered(self):
        """proof 在，但没证明「覆盖全部绕过路径」⇒ 强制但未覆盖 ⇒ 不许说只读。"""
        kind = "config_write"
        partial = {kind: dict(boundary.NEGATIVE_PROOFS[kind], covered=False)}
        out = boundary.classify_claims(
            {kind: {"mode": "enforced", "coverage": "covered"}}, proofs=partial)
        cell = out["kinds"][kind]
        self.assertEqual(cell["boundary_class"], "enforced_uncovered")
        self.assertNotEqual(cell["label"], boundary.LABEL_READONLY)

    def test_observed_covered_is_not_readonly(self):
        """纯观测即便覆盖全部已知路径，也只是「未观察到写操作」，不是「只读」。"""
        out = boundary.classify_claims(
            {"read": {"mode": "observed", "coverage": "covered"}})
        self.assertEqual(out["kinds"]["read"]["boundary_class"], "observed_covered")
        self.assertEqual(out["label"], boundary.LABEL_OBSERVED)

    def test_all_four_cells_reachable(self):
        got = set()
        got.add(boundary.classify_claims(
            {"config_write": {"mode": "enforced", "coverage": "covered"}}
        )["boundary_class"])
        got.add(boundary.classify_claims(
            {"config_write": {"mode": "enforced", "coverage": "covered"}},
            proofs={"config_write": dict(
                boundary.NEGATIVE_PROOFS["config_write"], covered=False)},
        )["boundary_class"])
        got.add(boundary.classify_claims(
            {"read": {"mode": "observed", "coverage": "covered"}})["boundary_class"])
        got.add(boundary.classify_claims(
            {"read": {"mode": "observed", "coverage": "uncovered"}})["boundary_class"])
        self.assertEqual(got, FOUR)


class TestProofRegistryGate(unittest.TestCase):
    """负向测试登记表自身要可对账：每条 enforced 声明都必须换到一条真测试。"""

    def test_every_proof_points_at_a_real_test_node(self):
        for kind, spec in boundary.NEGATIVE_PROOFS.items():
            with self.subTest(kind=kind):
                self.assertTrue(spec.get("test"), "%s 的 proof 没写 test" % kind)
                self.assertTrue(_proof_node_exists(spec["test"]),
                                "%s 登记的负向测试 %s 在盘上找不到节点"
                                % (kind, spec["test"]))

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
    """绕过路径真被拒：没有终端入口的 agent 拉不起来。"""

    def _call(self, agent_id):
        import term
        req = types.SimpleNamespace(
            headers={"x-term-token": "unit-test-token-not-production"},
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
            for k in ("mode", "coverage", "boundary_class", "label"):
                self.assertIn(k, b)

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