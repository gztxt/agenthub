"""产物分类器：判断一条任务输出**能不能当作「模型擅长什么」的证据**。

【为什么需要这个模块】
P0-a 把 `tasks.model` / `tasks.verdict` 接上线之后，下一步 P0-c 要按模型聚合
「哪个模型擅长哪类任务」。但 2026-10-15 的真数据探针撞出一件事：

    model = nvidia/nemotron-3.5-content-safety:free
    output = 'User Safety: safe'
    verdict = pass          ← nonempty 验证器判「通过」

免费池（`openrouter/openrouter/free`）会把请求路由到**审核/分类类模型**，
它们返回的是**标签**而不是正文。而 `nonempty` 验证器只判非空 ⇒ 一条标签
会被记成「这个模型成功产出了内容」。若拿它当画像素材，**「模型擅长什么」
就建立在审核模型的输出上** —— 这是本机 AGENTS.md 军规第 4 条早写过、
本次探针独立复现的老坑。

【所以这里判的不是「真假」，而是「可否用作能力证据」】
一条 `User Safety: safe` 对「审核模型的分类能力」是**真**证据；
但对「这个模型能不能写代码」是**零**信息，且会污染统计。
故本模块的语义是 `evidence_useful`，不是 `is_fake`。

【三条设计红线】
1. **只认强信号，宁漏勿伤**。真实正文被误判成标签 = 丢真证据，
   比漏一条标签更坏（漏的那条 P0-c 还能靠 `verdict` 交叉核对）。
2. **纯函数、stdlib-only**（分层铁律：L0 hermetic 不 import `src.main`），
   不读盘、不打网络、不碰 db —— 与 `healthx.py` / `staticguard.py` 同范式。
3. **每条判定都带 reason**。没有理由的分类器无法被审，被 P0-c 引用时
   等于把黑箱接进决策链。
"""
from __future__ import annotations

import re
from typing import Optional, Tuple

#: 判定档位。`content` 是唯一「可作能力证据」的档。
#: - `content`  正文，可作画像证据
#: - `label`    标签型产物（审核/分类模型的输出），**不可**作生成能力证据
#: - `empty`    无产物（执行失败/超时/预算耗尽），本就不可作证据
CONTENT = "content"
LABEL = "label"
EMPTY = "empty"

VERDICTS = (CONTENT, LABEL, EMPTY)

#: 模型 ID 里出现这些词 ⇒ 该模型属审核/分类/嵌入/重排/语音族，
#: 它的**设计输出就不是正文**。取自 CCR 账本实测出现的模型名
#: （`nvidia/nemotron-3.5-content-safety:free` 是本机唯一确证的分类族，账本 9 次）。
#:
#: 注意 `nemotron-3.5-lightning` / `nemotron-3-super-120b` 等**不在**此列 ——
#: 它们是通用生成模型，只是恰好同属 nemotron 家族。加进来会误伤大量真正文。
_NON_GENERATIVE = (
    "content-safety",
    "safety",
    "moderation",
    "guard",
    "classifier",
    "embedding",
    "rerank",
    "whisper",
    "tts",
)

#: 「标签: 值」形状。**要求整串就是一个 `标签: 单值`**：
#:   标签部分 `^[A-Za-z][A-Za-z _-]{2,30}$`，值部分 `^\S+$`（不含空格）。
#: 这两条约束一起把长句排除掉 —— 实测正文
#: 「Python 是一种简洁易读、应用广泛的高级通用编程语言……」既不含半角冒号，
#: 值部分有空格也过不了。宁可漏掉 `User Safety: unsafe / borderline`，
#: 也不误伤 `Note: 见 README` 这类正常回答。
_LABEL_SHAPE = re.compile(r"^\s*([A-Za-z][A-Za-z _-]{2,30})\s*:\s*(\S+)\s*$")

#: 标签部分出现这些词 ⇒ 确实是分类标签而非正常回答。
_LABEL_WORDS = (
    "safety", "safe", "label", "category", "class", "verdict",
    "type", "risk", "sentiment", "score", "rating", "toxicity",
)

#: 标签**值**必须落在分类器的封闭词表里 —— 这是与「正文」最锋利的分界。
#:
#: 为什么不用「值要短」：实测反例 `'User Safety: 这是一段真正的说明性正文而不是标签'`
#: 只有 18 字，长度判据放它过去（真去加长度上限也拦不住，得不偿失）。
#: 而分类器的返回值本就来自封闭枚举（safe/unsafe/borderline/toxic/low/high…），
#: 正常回答的「值」几乎不可能**恰好**是这些词 —— 这才是强信号。
_LABEL_VALUES = frozenset((
    "safe", "unsafe", "borderline", "clean", "toxic", "spam", "ham",
    "pass", "fail", "passed", "failed", "ok", "error", "warn", "info",
    "yes", "no", "true", "false", "none", "null", "unknown", "other",
    "allow", "deny", "block", "flag", "valid", "invalid",
    "low", "medium", "mid", "high", "critical",
    "positive", "negative", "neutral", "mixed",
    "s", "n", "y", "t", "f", "p",
))


def classify_output(output: Optional[str], model: Optional[str] = None) -> Tuple[str, str]:
    """判定一条产物能否作为「模型能力」的证据。

    返回 `(verdict, reason)`；`verdict` ∈ `VERDICTS`，`reason` 恒非空（可审计）。

    >>> classify_output("User Safety: safe", "nvidia/nemotron-3.5-content-safety:free")
    ('label', '模型属审核/分类/嵌入/重排/语音族(content-safety)')
    >>> classify_output("Rust 是一门注重内存安全的系统编程语言。", "qwen3.8-flash")
    ('content', '非标签形状的正文')
    """
    text = (output or "").strip()

    if not text:
        return EMPTY, "产物为空"

    mdl = (model or "").lower()
    for token in _NON_GENERATIVE:
        if token in mdl:
            return LABEL, "模型属审核/分类/嵌入/重排/语音族(%s)" % token

    m = _LABEL_SHAPE.match(text)
    if m:
        label, value = m.group(1).lower(), m.group(2).lower()
        for word in _LABEL_WORDS:
            if word in label and value in _LABEL_VALUES:
                return LABEL, "产物是「标签: 值」形状且值属分类枚举(%s: %s)" % (
                    m.group(1), m.group(2))

    return CONTENT, "非标签形状的正文"


def is_usable_evidence(output: Optional[str], model: Optional[str] = None) -> bool:
    """便捷判定：这条产物能否进 P0-c 的画像统计。

    只有 `CONTENT` 为真。**空产物也不算数** —— 「模型什么都没产出」
    是关于模型的弱信号，但它属于 `verdict` 维度，不该混进「擅长什么」。
    """
    return classify_output(output, model)[0] == CONTENT
