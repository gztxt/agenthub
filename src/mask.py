"""出站可逆脱敏：把密钥换成占位符出门，回包再把真值填回去（v0.13.181）。

为什么要有它（借 magpie 的 masking 设计，但只取**必需的那一半**）：

本机的 hub 会把任务原文、文件片段、命令输出直接发给远端模型（`MANAGER_LLM_BASE_URL`
指向网关后的真模型）。这些文本里**天然**夹着密钥：用户在会话里贴 `.env`、日志里有
`Authorization: Bearer …`、diff 里有私钥块。原有的
`src/sessions_export.py:52` 那套打码**只作用于导出/审计文本**，出站请求正文一个字节
都不处理 —— 也就是说「为了不看而打码」和「真正发出去」是两条路。

magpie 的三条硬约束，本模块全照抄（这三条才是它值得借的部分）：
  ① **同值同符**：同一个值永远得到同一个占位符（由值哈希派生）⇒ 多轮对话、
     prompt cache、thinking 签名都不会因为"这次换了个随机占位符"而失效；
  ② **只在内存**：映射表不进日志、不落盘（返回值由调用方持有，用完即弃）；
  ③ **不可用即不发**：掩码开启但处理失败时，调用方应放弃发送而不是发明文
     （本模块自己不抛网络错，纯字符串处理，失败面只有正则本身）。

⚠ 它与 magpie 的差异（本机刻意不做）：不脱敏人名/地址/邮箱 —— 那需要本地模型，
而且「模型看不到真值」会让一批正常任务失效（magpie README 自己列了这条代价）。
本机只掩**凭据类**高置信模式，误伤率接近零。

开关：`HUB_MASK_OUTBOUND=0`（或 `off`/`false`）关闭；缺省开启。
"""
from __future__ import annotations

import hashlib
import os
import re
from typing import Any, Dict, Tuple

__all__ = ["enabled", "mask_text", "unmask_text", "mask_obj", "unmask_obj",
           "PLACEHOLDER_RE"]

#: 占位符形状：`{{SK_1a2b3c4d}}`。刻意用「双花括号 + 全大写类型 + 8 位十六进制」，
#: 既不会被误当成正文，也能被 `unmask` 精确定位（不靠模糊匹配）。
PLACEHOLDER_RE = re.compile(r"\{\{[A-Z]{2,6}_[0-9a-f]{8}\}\}")

#: (类型标签, 正则)。**只收凭据类**，且每条都要能被"人工一眼看出是密钥"。
_RULES = [
    ("PRIVKEY", re.compile(
        r"-----BEGIN [A-Z ]{0,20}PRIVATE KEY-----.*?-----END [A-Z ]{0,20}PRIVATE KEY-----",
        re.S)),
    ("SK", re.compile(r"\bsk-[A-Za-z0-9_\-]{16,}")),                  # OpenAI / Anthropic / DeepSeek
    ("GH", re.compile(r"\b(?:ghp|gho|ghu|ghs|ghr)_[A-Za-z0-9]{20,}")),
    ("GHPAT", re.compile(r"\bgithub_pat_[A-Za-z0-9_]{20,}")),
    ("AWS", re.compile(r"\bAKIA[0-9A-Z]{16}\b")),
    ("XOX", re.compile(r"\bxox[baprs]-[A-Za-z0-9\-]{10,}")),
    ("BEARER", re.compile(r"(?<=Bearer )[A-Za-z0-9\-._~+/]{20,}=*")),
    ("GGL", re.compile(r"\bAIza[0-9A-Za-z_\-]{30,}")),               # Google API key
]


def enabled() -> bool:
    v = (os.getenv("HUB_MASK_OUTBOUND") or "").strip().lower()
    return v not in ("0", "off", "false", "no")


def _placeholder(tag: str, value: str) -> str:
    """由**值本身**派生占位符 ⇒ 同值同符（跨请求、跨进程都稳定）。"""
    h = hashlib.sha256(value.encode("utf-8", "replace")).hexdigest()[:8]
    return "{{%s_%s}}" % (tag, h)


def mask_text(text: str) -> Tuple[str, Dict[str, str]]:
    """返回 (掩码后文本, {占位符: 真值})。无命中时映射为空字典。"""
    if not text:
        return text, {}
    mapping: Dict[str, str] = {}
    out = text
    for tag, rx in _RULES:
        def _sub(m: "re.Match[str]", _tag: str = tag) -> str:
            val = m.group(0)
            ph = _placeholder(_tag, val)
            mapping[ph] = val
            return ph
        out = rx.sub(_sub, out)
    return out, mapping


def unmask_text(text: str, mapping: Dict[str, str]) -> str:
    if not text or not mapping:
        return text
    # 长的先换：占位符定长，但真值可能包含别的占位符形状，先换不会互相吃掉
    for ph in sorted(mapping, key=len, reverse=True):
        if ph in text:
            text = text.replace(ph, mapping[ph])
    return text


def mask_obj(obj: Any) -> Tuple[Any, Dict[str, str]]:
    """递归掩码 dict/list/str（其余类型原样）。返回 (新对象, 映射)。"""
    if isinstance(obj, str):
        return mask_text(obj)
    if isinstance(obj, dict):
        merged: Dict[str, str] = {}
        out: Dict[Any, Any] = {}
        for k, v in obj.items():
            nv, mp = mask_obj(v)
            out[k] = nv
            merged.update(mp)
        return out, merged
    if isinstance(obj, (list, tuple)):
        merged = {}
        items = []
        for v in obj:
            nv, mp = mask_obj(v)
            items.append(nv)
            merged.update(mp)
        return (items if isinstance(obj, list) else tuple(items)), merged
    return obj, {}


def unmask_obj(obj: Any, mapping: Dict[str, str]) -> Any:
    if not mapping:
        return obj
    if isinstance(obj, str):
        return unmask_text(obj, mapping)
    if isinstance(obj, dict):
        return {k: unmask_obj(v, mapping) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        items = [unmask_obj(v, mapping) for v in obj]
        return items if isinstance(obj, list) else tuple(items)
    return obj
