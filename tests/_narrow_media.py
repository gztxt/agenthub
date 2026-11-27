"""取 `templates/index.html` 里「窄屏档」`@media (max-width: 767px)` 块的正文。

【为什么需要这个模块】2026-11-28 全量 L0 出现 12 条集体红灯，症状都是
「`.sidebar` / `.home-*` / `.gh-link .gh-txt` 不在窄屏块内」——而模板里那些规则**都在**。

真因：模板按**页面作用域**写了多个 `@media (max-width: 767px)` 块
（`#page-tasks` 协同页、`#bbEx` 表格等，分档偏好不变量③只管「断点值唯一」，不禁多块）。
旧闸门一律 `re.search(...)` 取**第一个**块，隐含假设「第一个 767 块 = 全局档」。
v0.13.222 把协同页的窄屏块排到全局块之前后，那个假设失效 ⇒ 14 条断言拿到的是别页的块，
报错信息还指向「规则被删了」，把排查往错误方向带。

【口径】按**选择器**找所属块：谁包含它，就取那块。
① 0 命中 ⇒ 规则真的不在任何窄屏块里（真缺陷）；
② 多命中 ⇒ 同一选择器在两个窄屏块里各写一份（真漂移，必须合并）。
两者都 raise，不再静默取第一个。
"""

import re

BREAKPOINT = 767

_BLOCK_RE = re.compile(r"@media \(max-width[ :]+(\d+)px\) \{(.*?)\n        \}", re.S)


def blocks(html: str):
    """返回 [(断点, 块正文)] —— 只认 767 这一个窄屏档。"""
    return [(int(w), body) for w, body in _BLOCK_RE.findall(html) if int(w) == BREAKPOINT]


def block_containing(html: str, needle: str, *, what: str = ""):
    """返回**包含 needle** 的那个窄屏块正文；0 命中或多命中都抛 AssertionError。"""
    label = what or needle
    hits = [body for _w, body in blocks(html) if needle in body]
    if not hits:
        raise AssertionError(
            "没有任何 %dpx 窄屏块包含 %r（%s）—— 要么规则被删/移出窄屏档，"
            "要么它压根没写进模板" % (BREAKPOINT, needle, label))
    if len(hits) > 1:
        raise AssertionError(
            "有 %d 个窄屏块都含 %r（%s）—— 同一档规则被写了两份，必须合并到一处"
            % (len(hits), needle, label))
    return hits[0]