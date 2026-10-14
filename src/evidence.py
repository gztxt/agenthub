"""证据分层与研判原语（P0-4）：把 PASS/FAIL 二元升级为「不可判定」一等公民。

三层链路（方法论来自 aliyun《AI Agent Handbook》对标的证据分层，本仓落地为可断言原语）：

    原始事实 facts    —— 可复述的观测：哪个来源、什么时候、看到了什么、**观测本身成没成**
    候选信号 signals  —— 由事实按规则推出的**待判命题**（还没判，只是候选）
    研判 judgments    —— 对每条信号的裁定：`confirmed` / `dismissed` / `insufficient`

**本模块唯一必须记住的不变量**（其余都是它的推论）：

> 证据缺失、观测失败、规则引用了不存在的事实、规则抛异常、规则明确返回「不知道」
> —— 这五种情况**一律** `insufficient`，**永不**退化成 `dismissed`（=「没发现问题」）
> 或 `confirmed`（=「已确认」）。

本仓的原话是「**禁把 SKIP 当 PASS**」。那句话在纸面上已经写过很多次，但每次都要靠人
记得；本模块把它变成一个可以断言的对象：`summarize()` 的 `decisive` 就是那句原话，
`require_decisive()` 就是「insufficient **不走放行**」的机械形态。

三个刻意设计（都有负向测试钉着，见 `tests/test_evidence.py`）：
  1. `Fact.ok=False` 表示**观测没跑成**，不是「没发现问题」。把探针失败读成「一切正常」
     是本机反复出现的形态（`tdai_client` 五重错静默、ccpocket-bridge 21 天静默不可用）。
  2. `Signal.facts` 为空 ⇒ `insufficient`。**空证据集不许投票**——否则「零覆盖」会被
     读成「零问题」，正是「收集器收不到就报全绿」的那类假绿。
  3. 规则返回 `None` 是**合法**的第三种答案（「按现有事实判不了」），不是异常。
     把它当 False 处理就是静默降级，本仓在这上面栽过（vital 判定、`vitals_loop` 死代码）。

不做什么：本模块**不读盘、不打网络、不碰 db**。它只做纯函数研判，事实从哪来由调用方负责
（数据库行、AST 扫描、HTTP 探针都一样）。
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any, Callable, Dict, Iterable, List, Optional, Sequence, Tuple

#: 研判三态。**顺序即语义**：前两个是「判定了」，第三个是「判不了」。
VERDICTS: Tuple[str, ...] = ("confirmed", "dismissed", "insufficient")

#: 严重度。只影响排序与展示，**不影响判定**（低严重度的 insufficient 同样是不可判定）。
SEVERITIES: Tuple[str, ...] = ("info", "warn", "high")

#: 规则签名：拿信号与它引用的事实，返回 True/False/None。None = 「按现有事实判不了」。
Evaluator = Callable[["Signal", Dict[str, "Fact"]], Optional[bool]]


@dataclass(frozen=True)
class Fact:
    """一条**原始事实**：可复述的观测。

    `ok=False` 的语义必须记牢：**观测本身没跑成**（探针超时、文件读不到、端点拒答），
    这**不是**「观测到没有问题」。判据里把两者混起来，就会得到「探针全挂 = 系统全绿」。
    """
    key: str
    value: Any = None
    source: str = ""
    at: str = ""
    ok: bool = True
    note: str = ""

    def to_dict(self) -> Dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class Signal:
    """一条**候选信号**：还**没判**的命题。

    `facts` 引用的是 `Fact.key`；引用了不存在的事实 ⇒ 该信号 `insufficient`
    （不是「不满足」，是不敢说）。
    """
    key: str
    claim: str
    facts: Tuple[str, ...] = ()
    severity: str = "info"
    rule: str = ""


@dataclass(frozen=True)
class Judgment:
    """对一条信号的裁定。`reasons` 是人读的**依据**（为什么这么判），不是装饰。"""
    key: str
    claim: str
    verdict: str
    severity: str = "info"
    confidence: Optional[float] = None
    reasons: Tuple[str, ...] = ()
    facts: Tuple[str, ...] = ()
    rule: str = ""

    def to_dict(self) -> Dict[str, Any]:
        d = asdict(self)
        d["reasons"] = list(self.reasons)
        d["facts"] = list(self.facts)
        return d


class InsufficientEvidence(RuntimeError):
    """本批证据不足以放行。**调用方不得捕获后当成功继续**（那是把不可判定读成通过）。"""

    def __init__(self, what: str, summary: Dict[str, Any]):
        self.what = what
        self.summary = summary
        super().__init__(
            "%s：证据不足，不可判定（insufficient=%s, unjudged=%s, blocking=%s）"
            % (what, summary.get("insufficient"), summary.get("unjudged"),
               summary.get("blocking")))


def default_evaluator(signal: Signal, facts: Dict[str, Fact]) -> Optional[bool]:
    """缺省规则：引用的事实全部为真 ⇒ confirmed，否则 dismissed。

    只用在「事实本身就是布尔观测」的场景（例如 `audited=True`）。
    语义更复杂的信号请显式传 `evaluator` —— 缺省规则**不做任何推理**，
    免得把「事实值恰好为 0/空」误判成否定。
    """
    return all(bool(f.value) for f in facts.values())


def judge(signal: Signal, facts_by_key: Dict[str, Fact],
          evaluator: Optional[Evaluator] = None) -> Judgment:
    """裁定一条信号。**任何「判不了」的情形都回 insufficient，不回 dismissed。**"""
    sev = signal.severity if signal.severity in SEVERITIES else "info"
    base = {"key": signal.key, "claim": signal.claim, "severity": sev,
            "facts": signal.facts, "rule": signal.rule}

    if not signal.facts:
        return Judgment(verdict="insufficient", confidence=None,
                        reasons=("规则未引用任何事实 ⇒ 空证据集不许投票",), **base)

    missing = [k for k in signal.facts if k not in facts_by_key]
    if missing:
        return Judgment(verdict="insufficient", confidence=None,
                        reasons=("引用了不存在的事实 %s ⇒ 不可判定（不是「未发现问题」）"
                                 % missing,), **base)

    failed = [k for k in signal.facts if not facts_by_key[k].ok]
    if failed:
        return Judgment(verdict="insufficient", confidence=None,
                        reasons=("观测失败 %s ⇒ 不可判定（探针失败 ≠ 否定）" % failed,), **base)

    ev = evaluator or default_evaluator
    try:
        got = ev(signal, {k: facts_by_key[k] for k in signal.facts})
    except Exception as e:  # noqa: BLE001 —— 规则自己的 bug 也只能判「不可判定」
        return Judgment(verdict="insufficient", confidence=None,
                        reasons=("规则抛异常 %s: %s ⇒ 不可判定" % (type(e).__name__, e),), **base)

    if got is None:
        return Judgment(verdict="insufficient", confidence=None,
                        reasons=("规则明确返回 None（按现有事实判不了）⇒ 不可判定",), **base)

    ok = bool(got)
    return Judgment(
        verdict="confirmed" if ok else "dismissed",
        # 规则是确定性的 ⇒ 置信度是「规则给出的确定度」不是「事实的可信度」。
        # 事实的可信度已经由 Fact.source/ok 承载；**不要**在这里编一个主观刻度
        # （工作区裁定：主观刻度上量置信会掉到 0.3，不可当阈值）。
        confidence=1.0,
        reasons=("规则 %s 依据 %d 条事实判定" % (signal.rule or "<内联>", len(signal.facts)),),
        **base)


def summarize(judgments: Sequence[Judgment], unjudged: int = 0) -> Dict[str, Any]:
    """汇总。`decisive` 就是「禁把 SKIP 当 PASS」的机械形态。

    `unjudged` = **从未被研判过**的条目数（例如迁移前的旧行 `verdict IS NULL`）。
    它与 `insufficient`（判过了、但判不了）**分开计数**，但**同样**让 `decisive=False`
    —— 两者都意味着「这台机器上没人敢说它没问题」。
    """
    counts: Dict[str, int] = {v: 0 for v in VERDICTS}
    sev: Dict[str, int] = {}
    blocking: List[str] = []
    for j in judgments:
        counts[j.verdict] = counts.get(j.verdict, 0) + 1
        if j.verdict == "insufficient":
            blocking.append(j.key)
        if j.verdict != "dismissed":
            sev[j.severity] = sev.get(j.severity, 0) + 1
    un = max(0, int(unjudged or 0))
    return {
        "total": len(judgments),
        **counts,
        "unjudged": un,
        "decisive": counts["insufficient"] == 0 and un == 0,
        "blocking": blocking,
        "severity": sev,
    }


def require_decisive(summary: Dict[str, Any], what: str = "本批"):
    """「insufficient 不走放行」的机械形态：不 decisive 就抛，绝不返回一个可真值的结果。"""
    if not summary.get("decisive"):
        raise InsufficientEvidence(what, summary)
    return summary


def layer(facts: Iterable[Fact], signals: Iterable[Signal],
          evaluator: Optional[Evaluator] = None, unjudged: int = 0) -> Dict[str, Any]:
    """一次跑完三层：`{facts, signals, judgments, summary}`。

    返回**纯 JSON 可序列化**的 dict —— 各处的闸门产物要能直接进回包/落盘。
    """
    facts = list(facts)
    signals = list(signals)
    fbk = {f.key: f for f in facts}
    if len(fbk) != len(facts):
        raise ValueError("Fact.key 重复 —— 同名的两条观测会让「引用了哪条」不可复述")
    judgments = [judge(s, fbk, evaluator) for s in signals]
    return {
        "facts": [f.to_dict() for f in facts],
        "signals": [asdict(s) for s in signals],
        "judgments": [j.to_dict() for j in judgments],
        "summary": summarize(judgments, unjudged=unjudged),
    }