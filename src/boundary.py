"""纳管边界的四维分级（P0-5；对标 AI Agent Handbook C12「强制边界 vs 观测边界」）。

【C12 判据，原文语义】
一个边界只有**同时**满足三条，才可称**强制边界**：
    ① 覆盖全部绕过路径（`covered`）
    ② fail-closed —— 判不了就拒，绝不放行（`fail_closed`）
    ③ 有负向测试证明绕过路径被拒（negative proof）
缺任何一条 ⇒ 文字上只能写「未观察到写操作」，**不得写「只读」**。

【四维 = 强制/观测 × 覆盖/未覆盖】
    enforced_covered    强制且覆盖全路径  → label 按 kind 取（config_write=只读 / exec=受闸）
    enforced_uncovered  强制但覆盖不全    → label 「未观察到写操作」
    observed_covered    纯观测但已知全覆盖 → label 「未观察到写操作」
    observed_uncovered  纯观测且覆盖未知  → label 「未观察到写操作」

【为什么把它机械化（而不是靠人写文案）】
旧账「纳管 ≠ 可用 / 只读未证」的根因就是：文案里写「只读」零成本，于是
「我没看见它写过」经常被写成「它写不了」。本模块把这句话变成一条**降级链**：

    claim(enforced)  --登记表里找不到负向测试-->  observed_uncovered
                     --有负向测试但没证明覆盖全路径-->  enforced_uncovered

唯一的升级通道是 `NEGATIVE_PROOFS`：一张「kind → **逐条枚举的绕过面**」登记表。

【2026-10-15 改判：`covered` / `fail_closed` 不再手填】
旧实现把它们写成 `{"covered": True, "fail_closed": True}` 两个布尔 —— 那正是本模块
要治的形态（「一句话自称覆盖」）。现在改为：登记表里逐条列出**已枚举的绕过路径**
（每条挂一个证明「该路径被拒」的测试节点），`derive_coverage()` 机械导出：

    covered     = 绕过面清单非空 **且** 每条路径的测试节点都真实存在（AST 解析得到）
    fail_closed = `fail_closed_by` 指向的节点真实存在（判不了就拒，不是放行）

⇒ 「覆盖全路径」从**手填布尔**变成**对已枚举绕过面的断言**。清单空 / 任一节点缺失 ⇒
自动不算 covered，一律降级。**枚举本身是否完整仍是人的判断**，但至少不能再一句话自称，
必须逐条列出、每条可对账；且清单被删/改由 `tests/test_boundary.py` 的冻结钉兜住。

【边界种类（agenthub 对 agent 实际存在的纳管面）】
    exec          拉起终端进程（单入口 `term.create_session`，画像白名单 + token 闸）
    config_write  写 agent 配置（走 `writeauth` 集中中间件）
    read          只读探测（发现层；第三方服务路径未知 ⇒ 覆盖不全）
"""
import ast
import functools
import pathlib
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

# ── label（按 kind 取词，契约字符串）────────────────────────────────
#: 纯观测边界只此一个说法：边界有没有被拦，agenthub 没证明 ⇒ 不许多说一个字。
LABEL_OBSERVED = "未观察到写操作"
#: 强制且覆盖全路径时，说法**按边界种类**给（2026-10-15 改判：原先一刀切叫「只读」，
#: 对 `exec` 边界是错的 —— exec 讲的是「拉起进程受单入口闸」，不是「文件只读」）。
#:   config_write ⇒ 只读（写配置这条路被闸住）
#:   exec         ⇒ 受闸（拉起进程这条路走唯一受闸入口）
LABEL_READONLY = "只读"
LABEL_GATED = "受闸"
LABEL_BY_KIND = {"config_write": LABEL_READONLY, "exec": LABEL_GATED}
#: 未登记 kind 的强制边界保守说法（既不冒充「只读」，也不冒充「受闸」）
LABEL_CONTROLLED = "受控"
#: 全部合法 label（测试逐字钉着；多一个未登记的都算改契约）
LABELS = (LABEL_READONLY, LABEL_GATED, LABEL_CONTROLLED, LABEL_OBSERVED)

_REPO_ROOT = pathlib.Path(__file__).resolve().parents[1]

# ── 负向测试登记表（强制的唯一兑换券）─────────────────────────────────
#: kind → **绕过面清单**。
#:   surface        ：已枚举的绕过路径，每条挂 `test`（证明「该路径被拒」的测试节点）
#:   fail_closed_by ：证明「判不了就拒（不是放行）」的测试节点
#: `derive_coverage()` 由这两项**机械导出** covered / fail_closed；
#: 清单空 / 任一节点 AST 解析不到 ⇒ 自动不算 covered ⇒ 强制声明降级。
#: `tests/test_boundary.py::TestProofRegistryGate` 会到盘上解析 `Class::method`，
#: 登记的测试被删名/改名即红；另有一条冻结钉钉住「绕过面 id 集合」，防静默缩水。
NEGATIVE_PROOFS: Dict[str, dict] = {
    "exec": {
        "surface": [
            {"id": "agent_without_terminal_entry",
             "test": "tests/test_boundary.py::TestExecBoundaryNegative::test_unlisted_agent_is_rejected",
             "what": "无画像终端入口的 agent（如 ccr）拉起 ⇒ 400，不进 pty"},
            {"id": "unknown_agent",
             "test": "tests/test_boundary.py::TestExecBoundaryNegative::test_unknown_agent_is_rejected",
             "what": "未知 agent ⇒ 400"},
            {"id": "bad_token",
             "test": "tests/test_boundary.py::TestExecBoundaryNegative::test_bad_token_is_rejected",
             "what": "token 校验失败 ⇒ 401（无 token 即拒，非放行）"},
        ],
        "fail_closed_by": "tests/test_boundary.py::TestExecBoundaryNegative::test_bad_token_is_rejected",
        "what": "term.create_session 是拉起进程的**唯一入口**：agent 无画像终端入口⇒400、"
                "命令只出自画像模板（tests/test_term_launch_guard.py 另有 AST 钉子）、"
                "token 校验失败⇒401（无 token 即拒，非放行）。",
    },
    "config_write": {
        "surface": [
            {"id": "missing_secret",
             "test": "tests/test_boundary.py::TestWriteBoundaryNegative::test_missing_secret_fails_closed",
             "what": "未配口令 ⇒ misconfig(503)，不放行"},
            {"id": "wrong_secret",
             "test": "tests/test_boundary.py::TestWriteBoundaryNegative::test_wrong_secret_denied",
             "what": "错口令 ⇒ deny(401)，不放行"},
            {"id": "empty_secret",
             "test": "tests/test_boundary.py::TestWriteBoundaryNegative::test_empty_secret_is_not_a_secret",
             "what": "空口令不算口令 ⇒ 不放行"},
        ],
        "fail_closed_by": "tests/test_boundary.py::TestWriteBoundaryNegative::test_missing_secret_fails_closed",
        "what": "writeauth.decide 集中中间件 + 路由清单静态护栏：未配口令⇒misconfig(503)、"
                "错口令⇒deny(401)，**从不放行**。",
    },
}


@functools.lru_cache(maxsize=256)
def test_node_exists(test_id: str) -> bool:
    """`tests/xxx.py::Class::method`（或裸 `tests/xxx.py`）在盘上是否解析得到。

    **失败方向是安全的**：任何异常 / 文件缺失 ⇒ False ⇒ `covered` 不成立 ⇒ 只降不升。
    """
    try:
        rel, *rest = str(test_id).split("::")
        f = _REPO_ROOT / rel
        if not f.is_file():
            return False
        if not rest:
            return True
        classes: Dict[str, set] = {}
        for node in ast.walk(ast.parse(f.read_text(encoding="utf-8"))):
            if isinstance(node, ast.ClassDef):
                classes[node.name] = {n.name for n in node.body
                                      if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef))}
        return len(rest) >= 2 and rest[0] in classes and rest[1] in classes[rest[0]]
    except Exception:  # noqa: BLE001  判不了 ⇒ 不算覆盖（安全方向）
        return False


def derive_coverage(kind: str, proofs: Optional[Dict[str, dict]] = None) -> dict:
    """由绕过面清单**机械导出** covered / fail_closed（不再手填布尔）。

    返回 {covered, fail_closed, surface:[{id,test,ok}], missing:[id…]}。
    """
    live = NEGATIVE_PROOFS if proofs is None else proofs
    spec = live.get(kind) or {}
    surface = spec.get("surface") or []
    paths = [{"id": e.get("id"), "test": e.get("test"),
              "ok": test_node_exists(e.get("test") or "")} for e in surface]
    covered = bool(surface) and all(p["ok"] for p in paths)
    fail_closed = test_node_exists(spec.get("fail_closed_by") or "")
    return {"covered": covered, "fail_closed": fail_closed, "surface": paths,
            "missing": [p["id"] for p in paths if not p["ok"]]}


def _label_for(kind: str, cls: str) -> str:
    if cls != CLASS_ENFORCED_COVERED:
        return LABEL_OBSERVED
    return LABEL_BY_KIND.get(kind, LABEL_CONTROLLED)


def _cell(kind: str, mode: str, coverage: str, *, proof, detail: str) -> dict:
    cls = (CLASS_ENFORCED_COVERED if mode == MODE_ENFORCED and coverage == COVERAGE_COVERED
           else CLASS_ENFORCED_UNCOVERED if mode == MODE_ENFORCED
           else CLASS_OBSERVED_COVERED if coverage == COVERAGE_COVERED
           else CLASS_OBSERVED_UNCOVERED)
    return {
        "kind": kind,
        "mode": mode,
        "coverage": coverage,
        "boundary_class": cls,
        "label": _label_for(kind, cls),
        "proof": proof,
        "detail": detail,
    }


def classify_claims(claims: Dict[str, dict],
                    proofs: Optional[Dict[str, dict]] = None) -> dict:
    """把「声明的边界」折成四维 —— 核心是**只降不升**。

    claims: {kind: {"mode": enforced|observed, "coverage": covered|uncovered}}
    proofs: 可注入（测试用）；默认 `NEGATIVE_PROOFS`。

    返回 {kind, mode, coverage, boundary_class, label, proof, detail, kinds{…}}；
    `kinds` 是逐 kind 的四维，聚合值取**最弱**那条（一条链的强度＝最弱环）。
    """
    live_proofs = NEGATIVE_PROOFS if proofs is None else proofs
    kinds: Dict[str, dict] = {}
    for kind, claim in (claims or {}).items():
        mode = (claim or {}).get("mode", MODE_OBSERVED)
        coverage = (claim or {}).get("coverage", COVERAGE_UNCOVERED)
        if mode == MODE_ENFORCED:
            spec = live_proofs.get(kind)
            if not spec:
                kinds[kind] = _cell(
                    kind, MODE_OBSERVED, COVERAGE_UNCOVERED, proof=None,
                    detail="声明强制但登记表里没有该 kind 的绕过面 ⇒ 降级为观测："
                           "只能说「未观察到写操作」，不得写强断言。")
                continue
            derived = derive_coverage(kind, live_proofs)
            test_ids = [p["test"] for p in derived["surface"] if p.get("test")]
            if derived["covered"] and derived["fail_closed"]:
                kinds[kind] = _cell(
                    kind, MODE_ENFORCED, COVERAGE_COVERED, proof=test_ids,
                    detail="绕过面 %d 条逐条可对账且 fail-closed ⇒ 强制且覆盖全路径。"
                           % len(derived["surface"]))
            else:
                why = []
                if derived["missing"]:
                    why.append("绕过面节点缺失 %s" % derived["missing"])
                if not derived["surface"]:
                    why.append("绕过面清单为空")
                if not derived["fail_closed"]:
                    why.append("fail-closed 测试节点缺失")
                kinds[kind] = _cell(
                    kind, MODE_ENFORCED, COVERAGE_UNCOVERED, proof=test_ids or None,
                    detail="有绕过面登记但未能证明「覆盖全路径 + fail-closed」（%s）"
                           "⇒ 判据上仍只能说「未观察到写操作」。" % "；".join(why))
            continue
        kinds[kind] = _cell(
            kind, MODE_OBSERVED, coverage, proof=None,
            detail="纯观测边界：没有拦截能力，只能说「未观察到写操作」。")

    if not kinds:
        agg = _cell("", MODE_OBSERVED, COVERAGE_UNCOVERED, proof=None, detail="无边界声明。")
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
    return _cell("", MODE_OBSERVED, COVERAGE_UNCOVERED, proof=None,
                 detail="外部发现源：agenthub 只读探测，无拦截能力 ⇒ 只能说「未观察到写操作」。")