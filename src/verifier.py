"""任务验证器（P0-3）：**执行者不得自证 success** —— 产物是否合格由这里判。

【为什么要有这一层】
`tasks._run_task` 原先直接信执行者回包里的 `success=True` 就把任务置 `success`。
可执行者既是**被测对象**、又握着**判定权** ⇒ 它可以自证。这与本仓反复出事的那类
静默同族：判据（P0-4）已经把「判不了」做成 `insufficient` 一等公民，本层把它复用到
**产物验证**上——三态 `pass` / `fail` / `insufficient`，且 **判不了绝不放行**。

【三态语义】
    pass         产物通过该 kind 的契约
    fail         产物明确不合格（附结构化缺口 + 是否可重试）
    insufficient 判不了（未知 kind / 产物缺失 / 判据缺失）—— **不是** pass

【返回形状（调用方据此决定重跑还是上报，不解析自然语言）】
    {"status", "kind", "gaps":[{"id","detail"}…], "repairable", "detail"}

【kind 与各自判据】
    nonempty    产物去空白后非空
    json_array  产物（剥围栏后）可解析为 JSON 数组
    contains    产物包含 `expect` 子串

【与 evidence.py 的关系】同一口径：`insufficient` 不是「没问题」，是「还没判出来」。
本模块是纯函数，不读盘不打网络不碰 db。
"""
import json
import re
from typing import Dict, List, Optional, Tuple

STATUS_PASS = "pass"
STATUS_FAIL = "fail"
STATUS_INSUFFICIENT = "insufficient"

DEFAULT_KIND = "nonempty"


def _gap(gid: str, detail: str) -> dict:
    return {"id": gid, "detail": detail}


def _verdict(status: str, kind: str, gaps: Optional[List[dict]] = None,
             repairable: bool = False, detail: str = "") -> dict:
    return {"status": status, "kind": kind, "gaps": gaps or [],
            "repairable": repairable, "detail": detail}


def _strip_fence(text: str) -> str:
    return re.sub(r"```(?:json)?|```", "", text).strip()


#: 「我没干活」的显式回包。2026-10-27 实弹取证：jcode 回
#: 「I don't have direct filesystem access… 请把 find 的输出粘贴给我」，
#: 长度足够 ⇒ `nonempty` 判 **pass**，整条派发以「成功」收敛但零产出。
#: 非空只证明「模型说了话」，不证明「活干了」；这层启发式专门堵这个洞。
#: 刻意窄：只认第一人称的「无法访问/需要你粘贴」，避免把正常报告里的
#: 「can't determine」类措辞误判成拒答。
_PUNT_PATTERNS = (
    r"\bi (?:do not|don't) have (?:direct )?(?:filesystem|file[ -]system|access)\b",
    r"\bi (?:cannot|can't|can not) (?:directly )?(?:access|browse|read|run|execute)\b",
    r"(?:请|麻烦)(?:您|你)?(?:先)?(?:粘贴|贴出|提供)(?:一下)?(?:.*?)(?:输出|内容|列表|文件)",
    r"我无法访问",
    r"我(?:没有|不具备)(?:直接)?(?:访问|读取|执行)(?:该|这个)?(?:文件|目录|权限)",
    # 2026-10-29 影子实弹补：上面两条都漏了「我这里没有这个能力」的**中文自然说法**。
    # 实测原文「我无法直接读取您系统的 /etc/hostname 文件，因为我目前没有可用的
    # 文件系统访问工具」⇒ 旧正则不匹配 ⇒ nonempty 判 pass，零产出以 success 收敛。
    # 这不是新洞，是同一条洞的第二种说法；判据必须覆盖说法而不是某一个句子。
    r"我无法(?:直接)?(?:读取|访问|执行|操作|打开|查看)",
    r"我(?:目前|现在)?(?:没有|缺少)(?:可用的?)?[^。，,；;\n]{0,16}(?:工具|权限)",
)
_PUNT_RE = re.compile("|".join(_PUNT_PATTERNS), re.I)

#: 「整条回包只是审核标签」——免费池会路由到 content-safety 类模型，返回
#: `User Safety: safe` 而不是正文（判例见 AGENTS.md §1 军规 4）。
#: 2026-10-27 实弹取证：任务「只输出整数」回 `User Safety: safe`，
#: 长度非零 ⇒ nonempty 判 pass，run 以 `complete` 收敛。标签不是产物。
_JUNK_RE = re.compile(
    r"^\s*(?:(?:user\s+)?safety|moderation|content[\s_-]*policy|policy)"
    r"\s*[:：]\s*[\w\-]{1,20}\s*[.。]?\s*$", re.I)


def _verify_nonempty(output: Optional[str], expect=None) -> Tuple[str, List[dict], bool, str]:
    text = str(output or "").strip()
    if not text:
        return (STATUS_FAIL, [_gap("empty_output", "产物去空白后为空")], True,
                "执行者回包为空，不算完成")
    if _JUNK_RE.match(text):
        return (STATUS_FAIL,
                [_gap("not_a_product", "回包是审核/分类标签（%r），不是结果" % text[:40])],
                True, "回包是审核标签，换模型重试")
    m = _PUNT_RE.search(text)
    if m:
        return (STATUS_FAIL,
                [_gap("refusal", "执行者自述未执行（%r），产物不是结果" % m.group(0)[:40])],
                True, "回包是拒答/索要输入，不是产物")
    return (STATUS_PASS, [], False, "产物非空")


def _verify_json_array(output: Optional[str], expect=None) -> Tuple[str, List[dict], bool, str]:
    text = _strip_fence(str(output or ""))
    try:
        data = json.loads(text)
    except Exception:  # noqa: BLE001
        return (STATUS_FAIL, [_gap("invalid_json", "产物不是合法 JSON")], True,
                "无法解析为 JSON")
    if not isinstance(data, list):
        return (STATUS_FAIL, [_gap("not_array", "JSON 顶层不是数组")], True,
                "JSON 顶层不是数组")
    return (STATUS_PASS, [], False, "合法 JSON 数组")


def _verify_contains(output: Optional[str], expect=None) -> Tuple[str, List[dict], bool, str]:
    if expect is None or str(expect) == "":
        return (STATUS_INSUFFICIENT, [_gap("no_expectation", "没有给出要包含的判据")], False,
                "判据缺失，判不了")
    if str(expect) not in str(output or ""):
        return (STATUS_FAIL, [_gap("missing_expectation", "产物不含 %r" % expect)], True,
                "产物缺少要求的子串")
    return (STATUS_PASS, [], False, "含要求子串")


#: kind → 验证函数 (output, expect) -> (status, gaps, repairable, detail)
VERIFIERS: Dict[str, object] = {
    "nonempty": _verify_nonempty,
    "json_array": _verify_json_array,
    "contains": _verify_contains,
}


def known_kinds() -> tuple:
    return tuple(VERIFIERS)


def verify(kind: Optional[str], *, output: Optional[str] = None,
           expect: Optional[str] = None) -> dict:
    """按 kind 验产物。**唯一铁律：判不了（insufficient）绝不放行成 pass。**

    未知 kind / 产物缺失 / 判据缺失 ⇒ `insufficient`，永不 `pass`。
    """
    fn = VERIFIERS.get(kind or "")
    if fn is None:
        return _verdict(STATUS_INSUFFICIENT, kind or "",
                        [_gap("unknown_kind", "没有 %r 的验证判据" % (kind or ""))],
                        False, "未知验证 kind，判不了")
    if output is None:
        return _verdict(STATUS_INSUFFICIENT, kind,
                        [_gap("no_output", "执行者未回产物")], False, "没有可验的产物")
    status, gaps, repairable, detail = fn(output, expect)
    return _verdict(status, kind, gaps, repairable, detail)